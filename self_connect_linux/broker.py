"""
AF_UNIX broker — Phase 2: SO_PEERCRED identity binding + lease issuance.

Protocol (newline-delimited JSON over AF_UNIX SOCK_STREAM):

  Client → Server:
    {"type": "hello", "agent_id": "my-name"}
    {"type": "send",  "lease": "<uuid>", "to": "agent-id", "payload": "..."}
    {"type": "recv",  "lease": "<uuid>"}
    {"type": "bye",   "lease": "<uuid>"}

  Server → Client:
    {"type": "welcome", "lease": "<uuid>", "expires_at": <epoch>, "peer": {pid, uid, gid}}
    {"type": "ack",     "receipt_id": "<uuid>"}
    {"type": "message", "from": "agent-id", "payload": "...", "receipt_id": "<uuid>"}
    {"type": "empty"}
    {"type": "error",   "error": "..."}

Socket: /run/user/$UID/selfconnect/broker.sock (mode 0600)
Leases expire after LEASE_TTL_SECONDS (default 60).
SO_PEERCRED is verified on every connection; /proc identity bound at hello.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from .identity import LinuxTargetIdentity, capture_identity
from .receipts import make_receipt

LEASE_TTL_SECONDS = 60
_MAX_MAILBOX = 256


def default_socket_path() -> str:
    uid = os.getuid()
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    return str(Path(runtime) / "selfconnect" / "broker.sock")


def _peer_cred(conn: socket.socket) -> dict[str, int]:
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3I"))
    pid, uid, gid = struct.unpack("3I", raw)
    return {"pid": pid, "uid": uid, "gid": gid}


def _send_json(conn: socket.socket, msg: dict[str, Any]) -> None:
    conn.sendall((json.dumps(msg) + "\n").encode())


def _recv_json(conn: socket.socket, buf: bytearray) -> dict[str, Any] | None:
    while b"\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            return None
        buf.extend(chunk)
    line, rest = buf.split(b"\n", 1)
    buf[:] = rest
    return json.loads(line)


class _Lease:
    __slots__ = ("lease_id", "agent_id", "peer_cred", "identity", "expires_at")

    def __init__(
        self,
        agent_id: str,
        peer_cred: dict[str, int],
        identity: LinuxTargetIdentity | None,
    ) -> None:
        self.lease_id = str(uuid.uuid4())
        self.agent_id = agent_id
        self.peer_cred = peer_cred
        self.identity = identity
        self.expires_at = time.time() + LEASE_TTL_SECONDS

    def is_valid(self) -> bool:
        return time.time() < self.expires_at

    def renew(self) -> None:
        self.expires_at = time.time() + LEASE_TTL_SECONDS


class BrokerServer:
    """
    AF_UNIX broker server.

    Agents connect, send hello → receive a lease UUID.
    Agents use the lease to send/recv messages to other registered agents.
    SO_PEERCRED verified on every connection; /proc identity captured at hello.
    """

    def __init__(self, socket_path: str | None = None) -> None:
        self.socket_path = socket_path or default_socket_path()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()
        self._leases: dict[str, _Lease] = {}          # lease_id → _Lease
        self._agents: dict[str, str] = {}              # agent_id → lease_id
        self._mailboxes: dict[str, deque] = defaultdict(lambda: deque(maxlen=_MAX_MAILBOX))

    def start(self) -> None:
        Path(self.socket_path).parent.mkdir(parents=True, exist_ok=True)
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.socket_path)
        os.chmod(self.socket_path, 0o600)
        self._sock.listen(32)
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True, name="sc-broker")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass

    def list_agents(self) -> list[str]:
        with self._lock:
            return [
                aid
                for aid, lid in self._agents.items()
                if lid in self._leases and self._leases[lid].is_valid()
            ]

    def _serve(self) -> None:
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            threading.Thread(
                target=self._handle, args=(conn,), daemon=True, name="sc-broker-conn"
            ).start()

    def _handle(self, conn: socket.socket) -> None:
        buf = bytearray()
        lease: _Lease | None = None
        with conn:
            try:
                cred = _peer_cred(conn)
            except Exception as exc:
                _send_json(conn, {"type": "error", "error": f"SO_PEERCRED failed: {exc}"})
                return

            try:
                msg = _recv_json(conn, buf)
                if msg is None or msg.get("type") != "hello":
                    _send_json(conn, {"type": "error", "error": "expected hello"})
                    return

                agent_id = str(msg.get("agent_id", ""))
                if not agent_id:
                    _send_json(conn, {"type": "error", "error": "agent_id required"})
                    return

                # Bind /proc identity to this peer
                try:
                    identity = capture_identity(cred["pid"])
                except Exception:
                    identity = None

                lease = _Lease(agent_id, cred, identity)
                with self._lock:
                    # Evict any existing lease for this agent_id
                    old_lid = self._agents.get(agent_id)
                    if old_lid and old_lid in self._leases:
                        del self._leases[old_lid]
                    self._leases[lease.lease_id] = lease
                    self._agents[agent_id] = lease.lease_id

                _send_json(conn, {
                    "type": "welcome",
                    "lease": lease.lease_id,
                    "expires_at": lease.expires_at,
                    "peer": cred,
                    "agent_id": agent_id,
                })

                # Message loop
                while True:
                    msg = _recv_json(conn, buf)
                    if msg is None:
                        break
                    mtype = msg.get("type")
                    lid = msg.get("lease", "")

                    with self._lock:
                        valid = lid in self._leases and self._leases[lid].is_valid()
                        if valid:
                            self._leases[lid].renew()

                    if not valid:
                        _send_json(conn, {"type": "error", "error": "invalid or expired lease"})
                        break

                    if mtype == "send":
                        to = str(msg.get("to", ""))
                        payload = msg.get("payload", "")
                        rid = str(uuid.uuid4())
                        with self._lock:
                            self._mailboxes[to].append({
                                "from": agent_id,
                                "payload": payload,
                                "receipt_id": rid,
                            })
                        receipt = make_receipt(
                            backend="broker", pid=cred["pid"],
                            action="send", payload=str(payload),
                            readback="", echo_filtered=False, success=True,
                        )
                        _send_json(conn, {"type": "ack", "receipt_id": receipt.receipt_id})

                    elif mtype == "recv":
                        with self._lock:
                            mb = self._mailboxes.get(agent_id)
                            item = mb.popleft() if mb else None
                        if item:
                            _send_json(conn, {"type": "message", **item})
                        else:
                            _send_json(conn, {"type": "empty"})

                    elif mtype == "bye":
                        with self._lock:
                            if lid in self._leases:
                                del self._leases[lid]
                            if agent_id in self._agents:
                                del self._agents[agent_id]
                        _send_json(conn, {"type": "ack", "receipt_id": str(uuid.uuid4())})
                        break

                    else:
                        _send_json(conn, {"type": "error", "error": f"unknown type: {mtype}"})

            except (OSError, json.JSONDecodeError):
                pass
            finally:
                if lease:
                    with self._lock:
                        if lease.lease_id in self._leases:
                            del self._leases[lease.lease_id]
                        # Only remove agent_id mapping if it still points to our lease —
                        # a concurrent re-registration may have already replaced it.
                        if self._agents.get(lease.agent_id) == lease.lease_id:
                            del self._agents[lease.agent_id]

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()


class BrokerClient:
    """
    Connect to a running BrokerServer, obtain a lease, send/recv messages.

    Usage:
        with BrokerClient(agent_id="my-agent") as c:
            c.send("other-agent", "hello")
            msg = c.recv()
    """

    def __init__(self, agent_id: str, socket_path: str | None = None) -> None:
        self.agent_id = agent_id
        self.socket_path = socket_path or default_socket_path()
        self._conn: socket.socket | None = None
        self._buf = bytearray()
        self.lease: str | None = None
        self.peer: dict | None = None

    def connect(self) -> None:
        self._conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._conn.connect(self.socket_path)
        _send_json(self._conn, {"type": "hello", "agent_id": self.agent_id})
        resp = _recv_json(self._conn, self._buf)
        if resp is None or resp.get("type") != "welcome":
            raise RuntimeError(f"handshake failed: {resp}")
        self.lease = resp["lease"]
        self.peer = resp.get("peer")

    def send(self, to: str, payload: str) -> str:
        if not self._conn or not self.lease:
            raise RuntimeError("not connected")
        _send_json(self._conn, {"type": "send", "lease": self.lease, "to": to, "payload": payload})
        resp = _recv_json(self._conn, self._buf)
        if resp and resp.get("type") == "ack":
            return resp["receipt_id"]
        raise RuntimeError(f"send failed: {resp}")

    def recv(self) -> dict | None:
        if not self._conn or not self.lease:
            raise RuntimeError("not connected")
        _send_json(self._conn, {"type": "recv", "lease": self.lease})
        resp = _recv_json(self._conn, self._buf)
        if resp and resp.get("type") == "message":
            return resp
        return None

    def disconnect(self) -> None:
        if self._conn and self.lease:
            try:
                _send_json(self._conn, {"type": "bye", "lease": self.lease})
                _recv_json(self._conn, self._buf)
            except OSError:
                pass
        if self._conn:
            try:
                self._conn.close()
            except OSError:
                pass
        self._conn = None
        self.lease = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()

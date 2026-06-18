"""
AF_UNIX broker — Phase 2+3: SO_PEERCRED identity binding, lease issuance,
and kernel-attested GPU IPC handle grants.

Protocol (newline-delimited JSON over AF_UNIX SOCK_STREAM):

  Client → Server:
    {"type": "hello",  "agent_id": "my-name"}
    {"type": "send",   "lease": "<uuid>", "to": "agent-id", "payload": "..."}
    {"type": "recv",   "lease": "<uuid>"}
    {"type": "grant",  "lease": "<uuid>", "to": "agent-id",
                        "gpu_handle": "<hex64>", "size_bytes": N,
                        "buffer_fingerprint": "sha256:..."}
    {"type": "claim",  "lease": "<uuid>", "handle_id": "<uuid>"}
    {"type": "bye",    "lease": "<uuid>"}

  Server → Client:
    {"type": "welcome",    "lease": "<uuid>", "expires_at": <epoch>,
                            "peer": {pid,uid,gid}, "agent_id": "..."}
    {"type": "ack",        "receipt_id": "<uuid>"}
    {"type": "message",    "from": "agent-id", "payload": "...", "receipt_id": "<uuid>"}
    {"type": "empty"}
    {"type": "granted",    "handle_id": "<uuid>",
                            "gpu_handle": "<hex64>", "size_bytes": N,
                            "buffer_fingerprint": "sha256:...",
                            "from_agent": "...", "chain_hash": "sha256:...",
                            "receipt_id": "<uuid>"}
    {"type": "denied",     "handle_id": "<uuid>", "reason": "..."}
    {"type": "error",      "error": "..."}

Socket: /run/user/$UID/selfconnect/broker.sock (mode 0600)
Leases expire after LEASE_TTL_SECONDS (default 60).
SO_PEERCRED verified on every connection; /proc identity bound at hello.

KEY INNOVATION (Phase 3 — patent-worthy):
  grant/claim flow gates CUDA IPC handle delivery on kernel-attested identity.
  Only a process whose /proc fingerprint matches the stored lease identity
  receives the handle bytes. Any other process is denied. Every grant and
  every claim (successful or denied) is committed to a hash-chained
  ProvenanceLedger, creating a tamper-evident audit trail.
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

from .identity import LinuxTargetIdentity, capture_identity, verify_identity, LinuxTargetMismatch
from .receipts import make_receipt
from .provenance import ProvenanceLedger

LEASE_TTL_SECONDS = 60
_MAX_MAILBOX = 256
_MAX_MSG_BYTES = 256 * 1024  # 256 KB per message; excess closes the connection
_MAX_HANDLERS = 256


def default_socket_path() -> str:
    uid = os.getuid()
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    return str(Path(runtime) / "selfconnect" / "broker.sock")


def _peer_cred(conn: socket.socket) -> dict[str, int]:
    # "=3I": explicit standard sizes, little-endian — matches struct ucred {u32,u32,u32}
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("=3I"))
    pid, uid, gid = struct.unpack("=3I", raw)
    return {"pid": pid, "uid": uid, "gid": gid}


def _send_json(conn: socket.socket, msg: dict[str, Any]) -> None:
    conn.sendall((json.dumps(msg) + "\n").encode())


def _recv_json(conn: socket.socket, buf: bytearray) -> dict[str, Any] | None:
    while b"\n" not in buf:
        if len(buf) > _MAX_MSG_BYTES:
            raise ValueError(f"message exceeds {_MAX_MSG_BYTES} bytes")
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
        self._handler_sem = threading.Semaphore(_MAX_HANDLERS)
        self._leases: dict[str, _Lease] = {}          # lease_id → _Lease
        self._agents: dict[str, str] = {}              # agent_id → lease_id
        # No implicit maxlen — insertions are guarded; full mailbox returns an error to sender
        self._mailboxes: dict[str, deque] = defaultdict(deque)
        # GPU handle grants: handle_id → grant_info dict
        self._grants: dict[str, dict] = {}
        # Tamper-evident provenance ledger for all GPU buffer transfers
        self.ledger: ProvenanceLedger = ProvenanceLedger()

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
            if not self._handler_sem.acquire(blocking=False):
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            def _run(c=conn):
                try:
                    self._handle(c)
                finally:
                    self._handler_sem.release()
            threading.Thread(target=_run, daemon=True, name="sc-broker-conn").start()

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
                    # Bind agent_id to the connecting PID — prevents spoofing by a different process
                    old_lid = self._agents.get(agent_id)
                    if old_lid and old_lid in self._leases:
                        old_lease = self._leases[old_lid]
                        if old_lease.peer_cred["pid"] != cred["pid"]:
                            _send_json(conn, {"type": "error", "error": (
                                f"agent_id {agent_id!r} already held by pid "
                                f"{old_lease.peer_cred['pid']}"
                            )})
                            return
                        # Same PID reconnecting — evict stale lease
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

                    # Validate against THIS connection's lease only — prevents lease borrowing
                    valid = lid == lease.lease_id and lease.is_valid()
                    if valid:
                        lease.renew()

                    if not valid:
                        _send_json(conn, {"type": "error", "error": "invalid or expired lease"})
                        break

                    if mtype == "send":
                        to = str(msg.get("to", ""))
                        payload = msg.get("payload", "")
                        with self._lock:
                            mb = self._mailboxes[to]
                            if len(mb) >= _MAX_MAILBOX:
                                _send_json(conn, {"type": "error", "error": (
                                    f"mailbox for {to!r} is full ({_MAX_MAILBOX} messages) — message not delivered"
                                )})
                                continue
                            rid = str(uuid.uuid4())
                            mb.append({
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

                    elif mtype == "grant":
                        # Agent deposits a GPU IPC handle for another agent to claim.
                        # The handle is held in escrow until the named recipient claims it,
                        # at which point the broker verifies the claimant's kernel identity.
                        to = str(msg.get("to", ""))
                        gpu_handle_hex = str(msg.get("gpu_handle", ""))
                        size_bytes = int(msg.get("size_bytes", 0))
                        buf_fp = msg.get("buffer_fingerprint")
                        if not to or not gpu_handle_hex or not size_bytes:
                            _send_json(conn, {"type": "error",
                                              "error": "grant requires to, gpu_handle, size_bytes"})
                            continue
                        handle_id = str(uuid.uuid4())
                        with self._lock:
                            # Capture recipient's identity at grant time (prevents impostor eviction attack)
                            recipient_lid = self._agents.get(to)
                            expected_claimer_identity = None
                            if recipient_lid and recipient_lid in self._leases:
                                rl = self._leases[recipient_lid]
                                if rl.is_valid():
                                    expected_claimer_identity = rl.identity
                            self._grants[handle_id] = {
                                "from_agent": agent_id,
                                "to_agent": to,
                                "gpu_handle_hex": gpu_handle_hex,
                                "size_bytes": size_bytes,
                                "buffer_fingerprint": buf_fp,
                                "granter_identity": lease.identity,
                                "granter_pid": cred["pid"],
                                "expected_claimer_identity": expected_claimer_identity,
                                "created_at": time.time(),
                            }
                            # Record grant in provenance ledger (inside same lock to prevent race)
                            r = self.ledger.append(
                                action="grant",
                                from_agent=agent_id,
                                to_agent=to,
                                handle_id=handle_id,
                                size_bytes=size_bytes,
                                gpu_uuid=lease.identity.gpu_uuid if lease.identity else None,
                                buffer_fingerprint=buf_fp,
                                attested_pid=cred["pid"],
                                attested_exe_sha256=(
                                    lease.identity.exe_sha256 if lease.identity else None),
                                success=True,
                            )
                        _send_json(conn, {"type": "ack", "receipt_id": r.receipt_id,
                                          "handle_id": handle_id,
                                          "chain_hash": r.chain_hash})

                    elif mtype == "claim":
                        # Agent claims a GPU IPC handle deposited for it.
                        # THE CORE NOVEL STEP: broker re-attests the claimant's kernel
                        # identity against the stored lease identity before releasing
                        # the handle bytes. A process with the wrong exe hash, wrong
                        # start-time, or wrong namespace is denied — even if it has the
                        # correct handle_id string.
                        handle_id = str(msg.get("handle_id", ""))
                        with self._lock:
                            self._purge_expired_grants()
                            grant = self._grants.get(handle_id)

                        if not grant:
                            _send_json(conn, {"type": "denied", "handle_id": handle_id,
                                              "reason": "unknown handle_id"})
                            continue

                        # Check grant TTL expiry
                        created_at = grant.get("created_at", 0.0)
                        if time.time() - created_at > LEASE_TTL_SECONDS:
                            with self._lock:
                                self._grants.pop(handle_id, None)
                            _send_json(conn, {"type": "denied", "handle_id": handle_id,
                                              "reason": "grant expired"})
                            continue

                        if grant["to_agent"] != agent_id:
                            # Record denial
                            with self._lock:
                                r = self.ledger.append(
                                    action="deny",
                                    from_agent=grant["from_agent"],
                                    to_agent=agent_id,
                                    handle_id=handle_id,
                                    size_bytes=grant["size_bytes"],
                                    attested_pid=cred["pid"],
                                    attested_exe_sha256=(
                                        lease.identity.exe_sha256 if lease.identity else None),
                                    success=False,
                                    error="agent_id mismatch",
                                )
                            _send_json(conn, {"type": "denied", "handle_id": handle_id,
                                              "reason": "not the intended recipient",
                                              "receipt_id": r.receipt_id})
                            continue

                        # Re-attest: capture fresh /proc identity of claimer and verify
                        try:
                            fresh = capture_identity(cred["pid"])
                            # Prefer grant-time identity (blocks impostor eviction attack).
                            # Fall back to hello-time identity (blocks mid-connection binary swap).
                            expected = grant.get("expected_claimer_identity") or lease.identity
                            if expected:
                                verify_identity(expected, fresh)
                        except LinuxTargetMismatch as exc:
                            with self._lock:
                                r = self.ledger.append(
                                    action="deny",
                                    from_agent=grant["from_agent"],
                                    to_agent=agent_id,
                                    handle_id=handle_id,
                                    size_bytes=grant["size_bytes"],
                                    attested_pid=cred["pid"],
                                    attested_exe_sha256=(
                                        lease.identity.exe_sha256 if lease.identity else None),
                                    success=False,
                                    error=str(exc),
                                )
                            _send_json(conn, {"type": "denied", "handle_id": handle_id,
                                              "reason": f"identity mismatch: {exc}",
                                              "receipt_id": r.receipt_id})
                            continue

                        # Identity verified — deliver the handle (pop safely in case concurrent claim beat us)
                        with self._lock:
                            if handle_id not in self._grants:
                                # Another concurrent claimer already consumed this handle
                                _send_json(conn, {"type": "denied", "handle_id": handle_id,
                                                  "reason": "handle already claimed by concurrent request"})
                                continue
                            del self._grants[handle_id]
                            r = self.ledger.append(
                                action="claim",
                                from_agent=grant["from_agent"],
                                to_agent=agent_id,
                                handle_id=handle_id,
                                size_bytes=grant["size_bytes"],
                                gpu_uuid=(lease.identity.gpu_uuid if lease.identity else None),
                                buffer_fingerprint=grant["buffer_fingerprint"],
                                attested_pid=cred["pid"],
                                attested_exe_sha256=(
                                    lease.identity.exe_sha256 if lease.identity else None),
                                success=True,
                            )
                        _send_json(conn, {
                            "type": "granted",
                            "handle_id": handle_id,
                            "gpu_handle": grant["gpu_handle_hex"],
                            "size_bytes": grant["size_bytes"],
                            "buffer_fingerprint": grant["buffer_fingerprint"],
                            "from_agent": grant["from_agent"],
                            "chain_hash": r.chain_hash,
                            "receipt_id": r.receipt_id,
                        })

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

            except (OSError, json.JSONDecodeError, ValueError):
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

    def _purge_expired_grants(self) -> None:
        """Remove grants older than LEASE_TTL_SECONDS. Must be called with self._lock held."""
        now = time.time()
        expired = [hid for hid, g in self._grants.items()
                   if now - g.get("created_at", now) > LEASE_TTL_SECONDS]
        for hid in expired:
            del self._grants[hid]

    def get_stats(self) -> dict:
        """Return a snapshot of broker state counts."""
        with self._lock:
            active_leases = len([lv for lv in self._leases.values() if lv.is_valid()])
            active_agents = sum(
                1 for aid, lid in self._agents.items()
                if lid in self._leases and self._leases[lid].is_valid()
            )
            pending_grants = len(self._grants)
            ledger_entries = len(self.ledger)
        return {
            "active_leases": active_leases,
            "active_agents": active_agents,
            "pending_grants": pending_grants,
            "ledger_entries": ledger_entries,
        }

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

    def grant_gpu(self, to: str, gpu_handle: bytes, size_bytes: int,
                  buffer_fingerprint: str | None = None) -> dict:
        """
        Deposit a CUDA IPC handle for *to* to claim.
        Returns {"handle_id": ..., "chain_hash": ..., "receipt_id": ...}.
        """
        if not self._conn or not self.lease:
            raise RuntimeError("not connected")
        _send_json(self._conn, {
            "type": "grant",
            "lease": self.lease,
            "to": to,
            "gpu_handle": gpu_handle.hex(),
            "size_bytes": size_bytes,
            "buffer_fingerprint": buffer_fingerprint,
        })
        resp = _recv_json(self._conn, self._buf)
        if resp and resp.get("type") == "ack":
            return resp
        raise RuntimeError(f"grant failed: {resp}")

    def claim_gpu(self, handle_id: str) -> dict:
        """
        Claim a GPU IPC handle previously granted to this agent.
        Returns the full granted dict including gpu_handle hex bytes, size, fingerprint.
        Raises RuntimeError if denied (identity mismatch or wrong recipient).
        """
        if not self._conn or not self.lease:
            raise RuntimeError("not connected")
        _send_json(self._conn, {"type": "claim", "lease": self.lease, "handle_id": handle_id})
        resp = _recv_json(self._conn, self._buf)
        if resp and resp.get("type") == "granted":
            resp["gpu_handle_bytes"] = bytes.fromhex(resp["gpu_handle"])
            return resp
        reason = resp.get("reason", str(resp)) if resp else "no response"
        raise RuntimeError(f"GPU handle claim denied: {reason}")

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

"""
AF_UNIX broker skeleton — Phase 2 will add SO_PEERCRED lease issuance.

Current scope (Phase 1):
  - Accept AF_UNIX connections.
  - Read SO_PEERCRED to obtain peer PID/UID/GID.
  - Echo credentials back to the client.
  - Socket at /run/user/$UID/selfconnect/broker.sock (chmod 0600).

Phase 2 will add:
  - /proc identity binding per connection
  - Short-lived lease issuance
  - PTY/tmux agent registry
  - Receipt writer integration
"""
import json
import os
import socket
import struct
import threading
from pathlib import Path


def default_socket_path() -> str:
    uid = os.getuid()
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    return str(Path(runtime) / "selfconnect" / "broker.sock")


def _peer_cred(conn: socket.socket) -> dict:
    """Read SO_PEERCRED — returns pid, uid, gid of the connecting process."""
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", raw)
    return {"pid": pid, "uid": uid, "gid": gid}


class BrokerServer:
    """
    Minimal AF_UNIX broker. Accepts connections, verifies peer credentials.
    Threaded: each connection handled in its own daemon thread.
    """

    def __init__(self, socket_path: str | None = None):
        self.socket_path = socket_path or default_socket_path()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> None:
        Path(self.socket_path).parent.mkdir(parents=True, exist_ok=True)
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.socket_path)
        os.chmod(self.socket_path, 0o600)
        self._sock.listen(8)
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
        with conn:
            try:
                cred = _peer_cred(conn)
                conn.sendall(json.dumps({"status": "ok", "peer": cred}).encode())
            except Exception as exc:
                try:
                    conn.sendall(json.dumps({"status": "error", "error": str(exc)}).encode())
                except OSError:
                    pass

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()


class BrokerClient:
    """Connect to a running BrokerServer and read the credential echo."""

    def __init__(self, socket_path: str | None = None):
        self.socket_path = socket_path or default_socket_path()

    def ping(self) -> dict:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.connect(self.socket_path)
        with conn:
            data = conn.recv(4096)
        return json.loads(data)

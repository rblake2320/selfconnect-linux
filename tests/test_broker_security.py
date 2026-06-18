"""Security-focused broker tests — impostor eviction, identity mismatch, DoS mitigations."""
import os
import socket
import subprocess
import sys
import time

import pytest

from self_connect_linux.broker import LEASE_TTL_SECONDS, BrokerClient, BrokerServer
from self_connect_linux.identity import (
    LinuxTargetIdentity,
    LinuxTargetMismatch,
    verify_identity,
)


@pytest.fixture
def broker(tmp_path):
    sock = str(tmp_path / "broker.sock")
    with BrokerServer(socket_path=sock) as srv:
        time.sleep(0.05)
        yield srv, sock


def test_socket_permissions_0600(tmp_path):
    sock = str(tmp_path / "broker.sock")
    with BrokerServer(socket_path=sock):
        time.sleep(0.05)
        mode = os.stat(sock).st_mode & 0o777
        assert mode == 0o600, f"Expected 0600, got {oct(mode)}"


def test_wrong_agent_name_denied(broker):
    srv, sock = broker
    with BrokerClient("agent-A", socket_path=sock) as a:
        grant_resp = a.grant_gpu("intended-agent", b"\xAB" * 64, 64)
        handle_id = grant_resp["handle_id"]

    # A different agent (not "intended-agent") tries to claim
    with BrokerClient("other-agent", socket_path=sock) as other:
        with pytest.raises(RuntimeError, match="denied|not the intended"):
            other.claim_gpu(handle_id)


def test_handle_claimed_twice_raises(broker):
    srv, sock = broker
    with BrokerClient("agent-A", socket_path=sock) as a:
        with BrokerClient("agent-B", socket_path=sock) as b:
            grant_resp = a.grant_gpu("agent-B", b"\xAB" * 64, 64)
            handle_id = grant_resp["handle_id"]
            # First claim succeeds
            b.claim_gpu(handle_id)
            # Second claim must fail — handle was consumed
            with pytest.raises(RuntimeError):
                b.claim_gpu(handle_id)


def test_oversized_message_rejected(broker):
    srv, sock = broker
    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw.connect(sock)
    try:
        # Send an oversized message (300KB) — broker should drop connection gracefully
        big = b"X" * (300 * 1024) + b"\n"
        try:
            raw.sendall(big)
        except OSError:
            pass  # Connection may be closed mid-send — that's fine
        raw.settimeout(2.0)
        try:
            raw.recv(4096)  # response is intentionally ignored
            # If we get data back, it might be an error response, that's fine too
        except (OSError, socket.timeout):
            pass  # Connection closed — correct behaviour
    finally:
        try:
            raw.close()
        except OSError:
            pass
    # Verify broker is still alive and functional
    time.sleep(0.05)
    with BrokerClient("health-check", socket_path=sock) as c:
        c.send("nobody", "ping")


def test_impostor_eviction_denied(tmp_path):
    sock = str(tmp_path / "broker2.sock")
    with BrokerServer(socket_path=sock) as srv:  # noqa: F841
        time.sleep(0.05)

        # Register real agent-B in the current process
        agent_b = BrokerClient("agent-B", socket_path=sock)
        agent_b.connect()

        # Agent-A deposits a fake GPU grant targeting "agent-B"
        with BrokerClient("agent-A", socket_path=sock) as a:
            grant_resp = a.grant_gpu("agent-B", b"\xAB" * 64, 64)
            handle_id = grant_resp["handle_id"]

        # Disconnect real agent-B (but grant already captured its identity)
        agent_b.disconnect()

        # Run a SUBPROCESS that connects as "agent-B" and tries to claim handle_id
        # This is a DIFFERENT process (different pid, different proc_start_time_ticks)
        script = "\n".join([
            "import sys",
            "from self_connect_linux.broker import BrokerClient",
            "c = BrokerClient('agent-B', socket_path=sys.argv[1])",
            "c.connect()",
            "handle_id = sys.argv[2]",
            "try:",
            "    c.claim_gpu(handle_id)",
            "    print('GRANTED')",
            "except RuntimeError as e:",
            "    print(f'DENIED:{e}')",
            "finally:",
            "    c.disconnect()",
        ])
        result = subprocess.run(
            [sys.executable, "-c", script, sock, handle_id],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "PYTHONPATH": "/home/rblake2320/selfconnect-linux/.claude/worktrees/agent-ae173f25898e4c7cb"},
        )
        output = result.stdout.strip()
        assert "DENIED" in output, (
            f"Expected DENIED but got: {output!r}\nstderr: {result.stderr}"
        )


def test_expired_lease_behavior(broker):
    srv, sock = broker
    # Use a random UUID that's not a registered lease — should get error, not crash
    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw.connect(sock)
    try:
        import json
        # Send hello first
        raw.sendall((json.dumps({"type": "hello", "agent_id": "test-expired"}) + "\n").encode())
        raw.settimeout(2.0)
        resp_bytes = b""
        while b"\n" not in resp_bytes:
            chunk = raw.recv(4096)
            if not chunk:
                break
            resp_bytes += chunk
        # Now send a message with a fake/expired lease UUID
        fake_lease = "00000000-0000-0000-0000-000000000000"
        raw.sendall((json.dumps({"type": "send", "lease": fake_lease,
                                 "to": "nobody", "payload": "test"}) + "\n").encode())
        raw.settimeout(2.0)
        resp_bytes2 = b""
        while b"\n" not in resp_bytes2:
            try:
                chunk = raw.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            resp_bytes2 += chunk
        if resp_bytes2:
            resp = json.loads(resp_bytes2.split(b"\n")[0])
            assert resp.get("type") in ("error",), f"Expected error, got: {resp}"
    finally:
        try:
            raw.close()
        except OSError:
            pass
    # Broker should still be healthy
    time.sleep(0.05)
    with BrokerClient("health-check2", socket_path=sock) as c:
        c.send("nobody", "ping")


def test_identity_mismatch_in_verify_identity():
    id1 = LinuxTargetIdentity(
        platform="linux",
        backend="test",
        pid=12345,
        proc_start_time_ticks=1000,
    )
    id2 = LinuxTargetIdentity(
        platform="linux",
        backend="test",
        pid=12345,
        proc_start_time_ticks=9999,  # Different start time — impostor!
    )
    with pytest.raises(LinuxTargetMismatch):
        verify_identity(id1, id2)


def test_grant_requires_registered_recipient_for_strong_attestation(broker):
    srv, sock = broker
    # agent-B is NOT registered at grant time
    with BrokerClient("agent-A-only", socket_path=sock) as a:
        grant_resp = a.grant_gpu("unregistered-agent-B", b"\xFF" * 64, 64)
        handle_id = grant_resp["handle_id"]

    # Inspect _grants — expected_claimer_identity should be None (weaker path)
    with srv._lock:
        grant = srv._grants.get(handle_id)
    assert grant is not None, "Grant should be stored"
    assert grant.get("expected_claimer_identity") is None, (
        "expected_claimer_identity should be None when recipient not registered at grant time"
    )


def test_grant_ttl_expired_cannot_be_claimed(broker):
    """A grant whose created_at is older than LEASE_TTL_SECONDS must be rejected."""
    srv, sock = broker
    with BrokerClient("agent-A", socket_path=sock) as a:
        with BrokerClient("agent-B", socket_path=sock) as b:
            grant_resp = a.grant_gpu("agent-B", b"\xCC" * 64, 64)
            handle_id = grant_resp["handle_id"]
            # Wind back the grant's created_at to simulate expiry
            with srv._lock:
                srv._grants[handle_id]["created_at"] = time.time() - LEASE_TTL_SECONDS - 1
            # Claim must fail — grant is expired
            with pytest.raises(RuntimeError):
                b.claim_gpu(handle_id)


def test_concurrent_claim_only_one_succeeds(broker):
    """Two simultaneous claim attempts on the same handle — exactly one succeeds."""
    import threading
    srv, sock = broker
    with BrokerClient("agent-A", socket_path=sock) as a:
        grant_resp = a.grant_gpu("agent-B", b"\xDD" * 64, 64)
        handle_id = grant_resp["handle_id"]

    successes = []
    failures = []

    def try_claim():
        with BrokerClient("agent-B", socket_path=sock) as b:
            try:
                result = b.claim_gpu(handle_id)
                successes.append(result)
            except RuntimeError as e:
                failures.append(str(e))

    t1 = threading.Thread(target=try_claim)
    t2 = threading.Thread(target=try_claim)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert len(successes) == 1, f"Expected exactly 1 success, got {len(successes)}: {successes}"
    assert len(failures) == 1, f"Expected exactly 1 failure, got {len(failures)}: {failures}"


def test_broker_get_stats(broker):
    """get_stats() returns correct active_agents, pending_grants, and ledger_entries."""
    srv, sock = broker
    with BrokerClient("agent-X", socket_path=sock) as a:
        with BrokerClient("agent-Y", socket_path=sock):
            a.grant_gpu("agent-Y", b"\xEE" * 64, 64)
            stats = srv.get_stats()
            assert stats["active_agents"] == 2, f"Expected 2 active agents: {stats}"
            assert stats["pending_grants"] == 1, f"Expected 1 pending grant: {stats}"
            assert stats["ledger_entries"] >= 1, f"Expected >= 1 ledger entry: {stats}"


def test_send_to_self(broker):
    """An agent can send a message to itself and receive it back."""
    srv, sock = broker
    with BrokerClient("self-agent", socket_path=sock) as c:
        c.send("self-agent", "hello-self")
        msg = c.recv()
        assert msg is not None, "Expected to receive a message"
        assert msg["payload"] == "hello-self", f"Payload mismatch: {msg}"
        assert msg["from"] == "self-agent"


def test_malformed_json_closes_connection(broker):
    """Sending malformed JSON should not crash the broker."""
    import json as _json
    srv, sock = broker
    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw.connect(sock)
    try:
        # First send a valid hello so the connection is established
        raw.sendall((_json.dumps({"type": "hello", "agent_id": "malformed-test"}) + "\n").encode())
        raw.settimeout(2.0)
        # Read welcome
        resp_bytes = b""
        while b"\n" not in resp_bytes:
            try:
                chunk = raw.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            resp_bytes += chunk
        # Now send malformed JSON
        raw.sendall(b"not valid json\n")
        raw.settimeout(2.0)
        response = b""
        try:
            while b"\n" not in response:
                chunk = raw.recv(4096)
                if not chunk:
                    break
                response += chunk
        except (socket.timeout, OSError):
            pass  # Connection closed — acceptable
        # Either an error response or connection close is acceptable
        if response and b"\n" in response:
            _json.loads(response.split(b"\n")[0])  # any response type is acceptable — broker should not crash
    finally:
        try:
            raw.close()
        except OSError:
            pass
    # Broker should still be alive and functional
    time.sleep(0.05)
    srv.list_agents()  # must not raise

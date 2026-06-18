"""Load and concurrency tests for the AF_UNIX broker."""
import threading
import time

import pytest

from self_connect_linux.broker import BrokerClient, BrokerServer


@pytest.fixture
def broker(tmp_path):
    sock = str(tmp_path / "broker.sock")
    with BrokerServer(socket_path=sock) as srv:
        time.sleep(0.05)
        yield srv, sock


def test_50_concurrent_connections(broker):
    srv, sock = broker
    errors = []
    barrier = threading.Barrier(50)

    def worker(i):
        try:
            with BrokerClient(f"load-agent-{i}", socket_path=sock) as c:
                barrier.wait(timeout=10)
                c.send("sink", f"msg-{i}")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    elapsed = time.time() - t0

    assert not errors, f"Errors in concurrent connections: {errors}"
    assert elapsed < 10, f"50 concurrent connections took {elapsed:.1f}s (limit 10s)"


def test_500_messages_throughput(broker):
    srv, sock = broker
    N = 500
    received = []
    BATCH = 100  # Send and receive in batches to avoid mailbox overflow (_MAX_MAILBOX=256)

    with BrokerClient("sender", socket_path=sock) as sender:
        with BrokerClient("receiver", socket_path=sock) as receiver:
            t0 = time.time()
            for batch_start in range(0, N, BATCH):
                batch_end = min(batch_start + BATCH, N)
                for i in range(batch_start, batch_end):
                    sender.send("receiver", f"msg-{i:04d}")
                for _ in range(batch_end - batch_start):
                    msg = receiver.recv()
                    if msg:
                        received.append(msg["payload"])
            elapsed = time.time() - t0

    assert len(received) == N, f"Expected {N} messages, got {len(received)}"
    # Verify order
    for i, payload in enumerate(received):
        assert payload == f"msg-{i:04d}", f"Order mismatch at {i}: {payload!r}"
    assert elapsed < 15, f"500 messages took {elapsed:.1f}s (limit 15s)"


def test_concurrent_grant_claim(broker):
    srv, sock = broker
    N = 10
    errors = []
    results = [None] * N

    # Set up all granters and claimers first
    granters = [BrokerClient(f"granter-{i}", socket_path=sock) for i in range(N)]
    claimers = [BrokerClient(f"claimer-{i}", socket_path=sock) for i in range(N)]
    handle_ids = [None] * N

    try:
        for c in granters + claimers:
            c.connect()

        # Each granter deposits a handle for its paired claimer
        for i in range(N):
            resp = granters[i].grant_gpu(f"claimer-{i}", b"\xFF" * 64, 64)
            handle_ids[i] = resp["handle_id"]

        barrier = threading.Barrier(N)

        def claim_worker(i):
            try:
                barrier.wait(timeout=10)
                result = claimers[i].claim_gpu(handle_ids[i])
                results[i] = result
            except Exception as exc:
                errors.append((i, exc))

        threads = [threading.Thread(target=claim_worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

    finally:
        for c in granters + claimers:
            try:
                c.disconnect()
            except Exception:
                pass

    assert not errors, f"Claim errors: {errors}"
    for i, result in enumerate(results):
        assert result is not None, f"claimer-{i} got no result"
        assert result["gpu_handle_bytes"] == b"\xFF" * 64


def test_broker_handles_client_abrupt_disconnect(broker):
    srv, sock = broker
    import socket as _socket
    import json

    raw = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    raw.connect(sock)
    raw.sendall((json.dumps({"type": "hello", "agent_id": "abrupt-disconnector"}) + "\n").encode())
    raw.settimeout(2.0)

    # Read welcome
    buf = b""
    while b"\n" not in buf:
        try:
            chunk = raw.recv(4096)
        except _socket.timeout:
            break
        if not chunk:
            break
        buf += chunk

    # Abruptly close without bye
    raw.close()
    time.sleep(0.1)

    # Broker should still be functional
    assert isinstance(srv.list_agents(), list)
    with BrokerClient("post-disconnect-check", socket_path=sock) as c:
        c.send("nobody", "ping")


def test_mailbox_overflow_rejected(broker):
    """Broker returns an error (not crash) when a mailbox is full."""
    srv, sock = broker
    N = 260  # Beyond _MAX_MAILBOX = 256
    sent = 0
    rejected = 0

    with BrokerClient("overflow-sender", socket_path=sock) as sender:
        with BrokerClient("overflow-receiver", socket_path=sock) as receiver:
            for i in range(N):
                try:
                    sender.send("overflow-receiver", f"msg-{i:04d}")
                    sent += 1
                except RuntimeError:
                    rejected += 1

            received = []
            for _ in range(N):
                msg = receiver.recv()
                if msg:
                    received.append(msg["payload"])
                else:
                    break

    # Broker must not crash, must reject excess, must deliver up to 256
    assert sent + rejected == N, f"sent={sent} + rejected={rejected} != {N}"
    assert sent <= 256, f"accepted more than _MAX_MAILBOX={256}: {sent}"
    assert len(received) == sent, f"received {len(received)} but sent {sent}"

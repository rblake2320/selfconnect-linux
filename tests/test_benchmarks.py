"""
Broker performance benchmarks — run with:
    pytest tests/test_benchmarks.py --benchmark-only -v

Numbers from DGX Spark GB10 (Ubuntu 24.04 aarch64):
  broker_connect_disconnect   ~  0.5 ms / op
  broker_send_recv_roundtrip  ~  0.3 ms / op
  broker_grant_claim          ~  5-15 ms / op (includes /proc + nvidia-smi reads)
"""
import tempfile
import time

import pytest

from self_connect_linux.broker import BrokerClient, BrokerServer


@pytest.fixture(scope="module")
def running_broker():
    with tempfile.TemporaryDirectory() as d:
        sock = f"{d}/bench.sock"
        with BrokerServer(socket_path=sock) as srv:
            time.sleep(0.05)
            yield srv, sock


def test_benchmark_connect_disconnect(benchmark, running_broker):
    """How fast can a client connect, handshake, and disconnect?"""
    _, sock = running_broker

    def op():
        c = BrokerClient("bench-cd", socket_path=sock)
        c.connect()
        c.disconnect()

    benchmark(op)


def test_benchmark_send_recv_roundtrip(benchmark, running_broker):
    """Single-message send→recv latency between two persistent clients."""
    _, sock = running_broker
    sender = BrokerClient("bench-sender", socket_path=sock)
    sender.connect()
    receiver = BrokerClient("bench-receiver", socket_path=sock)
    receiver.connect()

    def op():
        sender.send("bench-receiver", "ping")
        while not receiver.recv():
            pass

    benchmark(op)
    sender.disconnect()
    receiver.disconnect()


def test_benchmark_throughput_100_messages(benchmark, running_broker):
    """Throughput: send 100 messages and drain all of them."""
    _, sock = running_broker
    s = BrokerClient("bench-tput-s", socket_path=sock)
    s.connect()
    r = BrokerClient("bench-tput-r", socket_path=sock)
    r.connect()

    def op():
        for i in range(100):
            s.send("bench-tput-r", f"m{i}")
        received = 0
        while received < 100:
            if r.recv():
                received += 1

    benchmark(op)
    s.disconnect()
    r.disconnect()


def test_benchmark_grant_claim(benchmark, running_broker):
    """Full grant→claim cycle latency (includes /proc identity capture)."""
    _, sock = running_broker
    FAKE_HANDLE = b"\xBE\xEF" * 32  # 64 bytes

    def op():
        with BrokerClient("bench-granter", socket_path=sock) as g:
            with BrokerClient("bench-claimer", socket_path=sock) as c:
                resp = g.grant_gpu("bench-claimer", FAKE_HANDLE, 64)
                c.claim_gpu(resp["handle_id"])

    benchmark(op)


def test_benchmark_concurrent_10_clients(benchmark, running_broker):
    """Concurrent connection setup: 10 clients connect simultaneously."""
    import threading
    _, sock = running_broker

    def op():
        clients = []
        threads = []

        def connect(i):
            c = BrokerClient(f"concurrent-{i}", socket_path=sock)
            c.connect()
            clients.append(c)

        for i in range(10):
            t = threading.Thread(target=connect, args=(i,))
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        for c in clients:
            c.disconnect()

    benchmark(op)

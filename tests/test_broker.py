"""Phase 2 broker tests — AF_UNIX, SO_PEERCRED, leases, two-agent messaging."""
import os
import threading
import time

import pytest

from self_connect_linux.broker import BrokerClient, BrokerServer


@pytest.fixture
def broker(tmp_path):
    sock = str(tmp_path / "test_broker.sock")
    with BrokerServer(socket_path=sock) as srv:
        time.sleep(0.05)
        yield srv, sock


def test_broker_starts_and_stops(tmp_path):
    sock = str(tmp_path / "broker.sock")
    srv = BrokerServer(socket_path=sock)
    srv.start()
    time.sleep(0.05)
    assert os.path.exists(sock)
    srv.stop()
    assert not os.path.exists(sock)


def test_client_connects_and_gets_lease(broker):
    srv, sock = broker
    with BrokerClient("agent-a", socket_path=sock) as c:
        assert c.lease is not None
        assert len(c.lease) == 36  # UUID format
        assert c.peer is not None
        assert c.peer["uid"] == os.getuid()
        assert c.peer["gid"] == os.getgid()


def test_peer_cred_reflects_real_pid(broker):
    srv, sock = broker
    with BrokerClient("agent-pid-test", socket_path=sock) as c:
        assert c.peer["pid"] == os.getpid()


def test_two_agents_exchange_messages(broker):
    srv, sock = broker

    with BrokerClient("pts1-agent", socket_path=sock) as sender:
        with BrokerClient("pts0-agent", socket_path=sock) as receiver:
            rid = sender.send("pts0-agent", "hello from pts1!")
            assert rid is not None

            time.sleep(0.05)
            msg = receiver.recv()
            assert msg is not None
            assert msg["from"] == "pts1-agent"
            assert msg["payload"] == "hello from pts1!"


def test_recv_empty_returns_none(broker):
    srv, sock = broker
    with BrokerClient("solo-agent", socket_path=sock) as c:
        result = c.recv()
        assert result is None


def test_multiple_messages_queued(broker):
    srv, sock = broker
    with BrokerClient("sender", socket_path=sock) as s:
        with BrokerClient("receiver", socket_path=sock) as r:
            s.send("receiver", "msg-1")
            s.send("receiver", "msg-2")
            s.send("receiver", "msg-3")
            time.sleep(0.05)
            msgs = []
            for _ in range(3):
                m = r.recv()
                if m:
                    msgs.append(m["payload"])
            assert msgs == ["msg-1", "msg-2", "msg-3"]


def test_messages_not_readable_by_wrong_agent(broker):
    srv, sock = broker
    with BrokerClient("target", socket_path=sock) as target:
        with BrokerClient("sender", socket_path=sock) as sender:
            with BrokerClient("intruder", socket_path=sock) as intruder:
                sender.send("target", "private message")
                time.sleep(0.05)
                # intruder polling their own mailbox gets nothing
                assert intruder.recv() is None
                # target gets it
                msg = target.recv()
                assert msg["payload"] == "private message"


def test_bye_clears_lease(broker):
    srv, sock = broker
    with BrokerClient("temp-agent", socket_path=sock) as c:
        lease = c.lease
        assert lease in srv._leases
    # after context exit, lease should be gone
    time.sleep(0.05)
    assert lease not in srv._leases


def test_concurrent_agents(broker):
    srv, sock = broker
    results = []

    def agent_task(agent_id, target_id, message):
        with BrokerClient(agent_id, socket_path=sock) as c:
            rid = c.send(target_id, message)
            results.append((agent_id, rid))

    # Register receivers first
    receivers = []
    for i in range(5):
        r = BrokerClient(f"rx-{i}", socket_path=sock)
        r.connect()
        receivers.append(r)

    threads = [
        threading.Thread(target=agent_task, args=(f"tx-{i}", f"rx-{i}", f"concurrent-{i}"))
        for i in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    time.sleep(0.1)
    assert len(results) == 5

    for i, r in enumerate(receivers):
        msg = r.recv()
        assert msg is not None
        assert msg["payload"] == f"concurrent-{i}"
        r.disconnect()


def test_server_context_manager(tmp_path):
    sock = str(tmp_path / "ctx.sock")
    with BrokerServer(socket_path=sock):
        time.sleep(0.05)
        assert os.path.exists(sock)
    assert not os.path.exists(sock)


def test_list_agents(broker):
    srv, sock = broker
    with BrokerClient("list-test-a", socket_path=sock):
        with BrokerClient("list-test-b", socket_path=sock):
            time.sleep(0.05)
            agents = srv.list_agents()
            # list_agents() must return agent IDs, not lease IDs
            assert "list-test-a" in agents
            assert "list-test-b" in agents


def test_list_agents_returns_agent_ids_not_lease_ids(broker):
    """Regression: list_agents() previously returned lease UUIDs instead of agent names."""
    srv, sock = broker
    with BrokerClient("named-agent", socket_path=sock):
        time.sleep(0.05)
        agents = srv.list_agents()
        assert "named-agent" in agents
        # Lease IDs are UUIDs — confirm none of the returned values look like one
        import re
        uuid_pat = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        for entry in agents:
            assert not uuid_pat.match(entry), f"got lease ID instead of agent ID: {entry}"


def test_pts0_pts1_direct_channel(broker):
    """Simulate the actual pts/0 ↔ pts/1 two-Claude exchange."""
    srv, sock = broker

    with BrokerClient("sc-linux-pts1", socket_path=sock) as pts1:
        with BrokerClient("sc-linux-pts0", socket_path=sock) as pts0:
            # pts/1 sends Phase 2 coordination message
            pts1.send("sc-linux-pts0", "Phase 2 AF_UNIX broker is live. Direct channel confirmed.")
            time.sleep(0.05)

            msg = pts0.recv()
            assert msg is not None
            assert msg["from"] == "sc-linux-pts1"
            assert "Phase 2" in msg["payload"]

            # pts/0 replies
            pts0.send("sc-linux-pts1", "Confirmed. Running test suite now. Both agents on spark-3cdf.")
            time.sleep(0.05)

            reply = pts1.recv()
            assert reply is not None
            assert reply["from"] == "sc-linux-pts0"
            assert "spark-3cdf" in reply["payload"]

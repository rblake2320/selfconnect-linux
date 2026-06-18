"""Tests for JSON action receipts."""
import json
import sys
import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Linux only")


def test_make_receipt_fields():
    from self_connect_linux.receipts import make_receipt
    r = make_receipt(backend="pty", pid=1234, action="send", payload="hello\n")
    assert r.backend == "pty"
    assert r.pid == 1234
    assert r.action == "send"
    assert r.payload_hash.startswith("sha256:")
    assert r.readback_hash is None
    assert r.echo_filtered is False
    assert r.success is True
    assert r.error is None
    assert isinstance(r.receipt_id, str)
    assert isinstance(r.timestamp, float)


def test_make_receipt_with_readback():
    from self_connect_linux.receipts import make_receipt
    r = make_receipt(
        backend="pty", pid=99, action="read",
        payload="", readback="output text", echo_filtered=True,
    )
    assert r.readback_hash is not None
    assert r.readback_hash.startswith("sha256:")
    assert r.echo_filtered is True


def test_make_receipt_failure():
    from self_connect_linux.receipts import make_receipt
    r = make_receipt(
        backend="pty", pid=None, action="expect",
        payload=r"\$", success=False, error="Timeout after 5s",
    )
    assert r.success is False
    assert "Timeout" in r.error


def test_receipt_to_json_is_valid():
    from self_connect_linux.receipts import make_receipt, receipt_to_json
    r = make_receipt(backend="pty", pid=1, action="send", payload="test")
    text = receipt_to_json(r)
    parsed = json.loads(text)
    assert parsed["backend"] == "pty"
    assert parsed["action"] == "send"
    assert parsed["success"] is True


def test_receipt_ids_are_unique():
    from self_connect_linux.receipts import make_receipt
    ids = {make_receipt("pty", 1, "send", "x").receipt_id for _ in range(50)}
    assert len(ids) == 50

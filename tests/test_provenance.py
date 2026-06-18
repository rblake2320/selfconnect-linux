"""Tests for the hash-chained provenance ledger (runs without CUDA)."""
import json
import time

import pytest

from self_connect_linux.provenance import (
    ChainBroken,
    ChainedReceipt,
    GENESIS_HASH,
    ProvenanceLedger,
    make_chained_receipt,
)


def test_genesis_hash_format():
    assert GENESIS_HASH.startswith("sha256:")
    assert len(GENESIS_HASH) == 7 + 64


def test_make_chained_receipt_fields():
    r = make_chained_receipt(
        action="grant",
        from_agent="a",
        to_agent="b",
        handle_id="h1",
        size_bytes=1024,
        prev_chain_hash=GENESIS_HASH,
        gpu_uuid="GPU-abc123",
        buffer_fingerprint="sha256:deadbeef",
        attested_pid=12345,
        success=True,
    )
    assert r.action == "grant"
    assert r.from_agent == "a"
    assert r.to_agent == "b"
    assert r.handle_id == "h1"
    assert r.size_bytes == 1024
    assert r.prev_chain_hash == GENESIS_HASH
    assert r.chain_hash.startswith("sha256:")
    assert len(r.chain_hash) == 7 + 64
    assert r.chain_hash != GENESIS_HASH
    assert r.attested_pid == 12345


def test_chain_hash_changes_with_content():
    base = dict(
        action="grant", from_agent="a", to_agent="b",
        handle_id="h", size_bytes=100,
        prev_chain_hash=GENESIS_HASH, success=True,
    )
    r1 = make_chained_receipt(**base)
    r2 = make_chained_receipt(**{**base, "from_agent": "x"})
    assert r1.chain_hash != r2.chain_hash


def test_chain_links_receipts():
    r1 = make_chained_receipt(
        action="grant", from_agent="a", to_agent="b",
        handle_id="h1", size_bytes=64,
        prev_chain_hash=GENESIS_HASH, success=True,
    )
    r2 = make_chained_receipt(
        action="claim", from_agent="a", to_agent="b",
        handle_id="h1", size_bytes=64,
        prev_chain_hash=r1.chain_hash, success=True,
    )
    assert r2.prev_chain_hash == r1.chain_hash
    assert r2.chain_hash != r1.chain_hash


def test_ledger_empty_head_is_genesis():
    ledger = ProvenanceLedger()
    assert ledger.head_hash == GENESIS_HASH


def test_ledger_append_and_len():
    ledger = ProvenanceLedger()
    r = ledger.append("grant", "a", "b", "h1", 64)
    assert len(ledger) == 1
    assert ledger.head_hash == r.chain_hash
    r2 = ledger.append("claim", "a", "b", "h1", 64)
    assert len(ledger) == 2
    assert ledger.head_hash == r2.chain_hash
    assert r2.prev_chain_hash == r.chain_hash


def test_ledger_verify_chain_passes():
    ledger = ProvenanceLedger()
    for i in range(5):
        ledger.append("grant", f"agent-{i}", "b", f"h{i}", 128)
    ledger.verify_chain()  # should not raise


def test_ledger_verify_detects_tamper_payload():
    ledger = ProvenanceLedger()
    for i in range(4):
        ledger.append("grant", f"ag{i}", "b", f"h{i}", 64)

    # Tamper with entry 1 (change from_agent without recomputing chain_hash)
    entries = ledger._entries
    old = entries[1]
    entries[1] = ChainedReceipt(
        **{**old.__dict__, "from_agent": "EVIL_TAMPERED"}
    )
    with pytest.raises(ChainBroken):
        ledger.verify_chain()


def test_ledger_verify_detects_tamper_chain_hash():
    ledger = ProvenanceLedger()
    ledger.append("grant", "a", "b", "h", 64)
    entries = ledger._entries
    old = entries[0]
    entries[0] = ChainedReceipt(**{**old.__dict__, "chain_hash": "sha256:" + "f" * 64})
    with pytest.raises(ChainBroken):
        ledger.verify_chain()


def test_ledger_verify_detects_insertion():
    ledger = ProvenanceLedger()
    ledger.append("grant", "a", "b", "h1", 64)
    r3 = ledger.append("claim", "a", "b", "h1", 64)

    # Insert a fake entry between index 0 and 1
    fake = make_chained_receipt(
        action="deny", from_agent="x", to_agent="y",
        handle_id="hX", size_bytes=0,
        prev_chain_hash=ledger._entries[0].chain_hash, success=False,
    )
    ledger._entries.insert(1, fake)
    # Now entry[2]'s prev_chain_hash points to r3's chain_hash (the original [1]),
    # but the ledger will try to verify entry[1] (the fake) chaining from entry[0],
    # then entry[2] chaining from the fake — entry[2]'s prev_chain_hash won't match.
    with pytest.raises(ChainBroken):
        ledger.verify_chain()


def test_ledger_to_json():
    ledger = ProvenanceLedger()
    ledger.append("grant", "a", "b", "h", 64, gpu_uuid="GPU-test")
    j = ledger.to_json()
    data = json.loads(j)
    assert len(data) == 1
    assert data[0]["action"] == "grant"
    assert data[0]["gpu_uuid"] == "GPU-test"
    assert data[0]["chain_hash"].startswith("sha256:")


def test_ledger_iter():
    ledger = ProvenanceLedger()
    ledger.append("grant", "a", "b", "h", 64)
    ledger.append("claim", "a", "b", "h", 64)
    entries = list(ledger)
    assert len(entries) == 2
    assert entries[0].action == "grant"
    assert entries[1].action == "claim"


def test_grant_claim_deny_full_sequence():
    """Simulate grant → successful claim → denied impostor — verifies chain."""
    ledger = ProvenanceLedger()
    ledger.append(
        "grant", "agent-A", "agent-B", "handle-1", 1024,
        gpu_uuid="GPU-abc", buffer_fingerprint="sha256:aabbcc",
        attested_pid=1001, attested_exe_sha256="sha256:exeA",
    )
    ledger.append(
        "claim", "agent-A", "agent-B", "handle-1", 1024,
        gpu_uuid="GPU-abc", buffer_fingerprint="sha256:aabbcc",
        attested_pid=1002, attested_exe_sha256="sha256:exeB",
        success=True,
    )
    ledger.append(
        "deny", "agent-A", "impostor", "handle-2", 1024,
        success=False, error="identity mismatch",
    )
    assert len(ledger) == 3
    ledger.verify_chain()  # must not raise
    actions = [r.action for r in ledger]
    assert actions == ["grant", "claim", "deny"]

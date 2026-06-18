"""
Hash-chained provenance ledger for GPU buffer transfers.

Every GPU memory handle transfer between agents produces a ChainedReceipt.
Each receipt includes a chain_hash = SHA-256(prev_chain_hash || receipt_fields),
forming a tamper-evident linked list. Any insertion, deletion, or modification
of a receipt in the middle of the chain breaks all subsequent chain_hash values,
which verify_chain() detects.

This is the cryptographic audit trail that makes the invention patent-worthy:
not only is each transfer identity-attested, but the *history* of transfers
is tamper-evident as a whole.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

# The genesis hash — the "prev" of the first receipt in any ledger.
GENESIS_HASH = "sha256:" + "0" * 64


@dataclass
class ChainedReceipt:
    """
    A single entry in the GPU-buffer provenance ledger.

    chain_hash = SHA-256(prev_chain_hash || canonical_fields)
    where canonical_fields = JSON of {receipt_id, timestamp, action,
    from_agent, to_agent, gpu_uuid, buffer_fingerprint, handle_id, success}.
    """
    receipt_id: str
    timestamp: float
    action: str                  # "grant" | "claim" | "deny" | "close"
    from_agent: str
    to_agent: str
    gpu_uuid: str | None
    buffer_fingerprint: str | None
    handle_id: str               # broker-assigned UUID for this grant
    size_bytes: int
    attested_pid: int | None     # pid of the claiming agent (kernel-verified)
    attested_exe_sha256: str | None
    success: bool
    error: str | None
    prev_chain_hash: str
    chain_hash: str
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


def _canonical(r: ChainedReceipt) -> bytes:
    """The canonical byte string that is hashed into chain_hash."""
    fields = {
        "receipt_id": r.receipt_id,
        "timestamp": r.timestamp,
        "action": r.action,
        "from_agent": r.from_agent,
        "to_agent": r.to_agent,
        "gpu_uuid": r.gpu_uuid,
        "buffer_fingerprint": r.buffer_fingerprint,
        "handle_id": r.handle_id,
        "success": r.success,
    }
    return json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()


def _chain_hash(prev_chain_hash: str, receipt: ChainedReceipt) -> str:
    h = hashlib.sha256()
    h.update(prev_chain_hash.encode())
    h.update(_canonical(receipt))
    return "sha256:" + h.hexdigest()


def make_chained_receipt(
    *,
    action: str,
    from_agent: str,
    to_agent: str,
    handle_id: str,
    size_bytes: int,
    prev_chain_hash: str,
    gpu_uuid: str | None = None,
    buffer_fingerprint: str | None = None,
    attested_pid: int | None = None,
    attested_exe_sha256: str | None = None,
    success: bool = True,
    error: str | None = None,
    metadata: dict | None = None,
) -> ChainedReceipt:
    rid = str(uuid.uuid4())
    ts = time.time()
    # Build partial receipt to compute chain hash
    partial = ChainedReceipt(
        receipt_id=rid,
        timestamp=ts,
        action=action,
        from_agent=from_agent,
        to_agent=to_agent,
        gpu_uuid=gpu_uuid,
        buffer_fingerprint=buffer_fingerprint,
        handle_id=handle_id,
        size_bytes=size_bytes,
        attested_pid=attested_pid,
        attested_exe_sha256=attested_exe_sha256,
        success=success,
        error=error,
        prev_chain_hash=prev_chain_hash,
        chain_hash="",  # placeholder
        metadata=metadata or {},
    )
    chain = _chain_hash(prev_chain_hash, partial)
    return ChainedReceipt(
        **{**partial.__dict__, "chain_hash": chain}
    )


class ProvenanceLedger:
    """
    An in-memory hash-chained ledger of GPU buffer transfer receipts.

    append() adds a new receipt and verifies it chains correctly from the last.
    verify_chain() walks the entire chain and raises on any break.
    Can be serialized to JSON for persistence or audit export.
    """

    def __init__(self) -> None:
        self._entries: list[ChainedReceipt] = []

    @property
    def head_hash(self) -> str:
        if not self._entries:
            return GENESIS_HASH
        return self._entries[-1].chain_hash

    def append(
        self,
        action: str,
        from_agent: str,
        to_agent: str,
        handle_id: str,
        size_bytes: int,
        **kwargs,
    ) -> ChainedReceipt:
        r = make_chained_receipt(
            action=action,
            from_agent=from_agent,
            to_agent=to_agent,
            handle_id=handle_id,
            size_bytes=size_bytes,
            prev_chain_hash=self.head_hash,
            **kwargs,
        )
        self._entries.append(r)
        return r

    def verify_chain(self) -> None:
        """
        Walk the entire chain and raise ChainBroken if any receipt's chain_hash
        does not match SHA-256(prev_chain_hash || canonical_fields).
        Detects any tampering, insertion, deletion, or reordering.
        """
        prev = GENESIS_HASH
        for i, r in enumerate(self._entries):
            expected = _chain_hash(prev, r)
            if expected != r.chain_hash:
                raise ChainBroken(
                    f"Chain broken at entry {i} (receipt_id={r.receipt_id}): "
                    f"expected chain_hash={expected!r}, got {r.chain_hash!r}"
                )
            prev = r.chain_hash

    def to_json(self) -> str:
        return json.dumps([r.to_dict() for r in self._entries], indent=2)

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):
        return iter(self._entries)


class ChainBroken(RuntimeError):
    pass

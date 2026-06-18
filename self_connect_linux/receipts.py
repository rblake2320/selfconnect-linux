"""JSON action/readback receipts — every PTY send/read produces one."""
import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass


@dataclass
class ActionReceipt:
    receipt_id: str
    timestamp: float
    backend: str
    pid: int | None
    action: str
    payload_hash: str
    readback_hash: str | None
    echo_filtered: bool
    success: bool
    error: str | None
    metadata: dict


def _hash(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return "sha256:" + hashlib.sha256(data).hexdigest()[:16]


def make_receipt(
    backend: str,
    pid: int | None,
    action: str,
    payload: str,
    readback: str | None = None,
    echo_filtered: bool = False,
    success: bool = True,
    error: str | None = None,
    metadata: dict | None = None,
) -> ActionReceipt:
    return ActionReceipt(
        receipt_id=str(uuid.uuid4()),
        timestamp=time.time(),
        backend=backend,
        pid=pid,
        action=action,
        payload_hash=_hash(payload),
        readback_hash=_hash(readback) if readback is not None else None,
        echo_filtered=echo_filtered,
        success=success,
        error=error,
        metadata=metadata or {},
    )


def receipt_to_json(r: ActionReceipt) -> str:
    return json.dumps(asdict(r), indent=2)

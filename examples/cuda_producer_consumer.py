"""
CUDA IPC producer-consumer via broker.

Demonstrates Phase 2 + Phase 4 working together:
  - Producer allocates a GPU buffer, writes data, exports a 64-byte IPC handle
  - Handle travels through the AF/UNIX broker as a JSON-encoded base64 string
  - Consumer subprocess imports the handle, reads back the same GPU memory
  - Both sides register with the broker; no shared filesystem path needed

Run:
    python examples/cuda_producer_consumer.py
"""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from self_connect_linux import (
    BrokerClient,
    BrokerServer,
    CudaIpcBuffer,
    cuda_ipc_available,
    handle_to_b64,
)

PAYLOAD = b"SelfConnect Linux Phase 4: CUDA IPC via broker. GB10 Grace Blackwell confirmed."
BUF_SIZE = 64 * 1024  # 64 KiB


CONSUMER_SCRIPT = """
import sys, json, time, tempfile
sys.path.insert(0, "{repo_root}")
from self_connect_linux import BrokerClient, CudaIpcBuffer, handle_from_b64

sock_path = sys.argv[1]
time.sleep(0.3)   # let producer register first

with BrokerClient("consumer", socket_path=sock_path) as cons:
    # Wait for the handle message
    deadline = time.time() + 10.0
    msg = None
    while time.time() < deadline:
        msg = cons.recv()
        if msg:
            break
        time.sleep(0.05)

    if not msg:
        print("CONSUMER_ERROR: no message received", flush=True)
        sys.exit(1)

    info = json.loads(msg["payload"])
    with CudaIpcBuffer.from_handle(handle_from_b64(info["handle"]), size=info["size"]) as remote:
        data = remote.read(length=info["payload_len"])

    # Signal back
    cons.send("producer", json.dumps({{"status": "ok", "data": data.decode()}}))
    print(f"CONSUMER_OK: {{data.decode()[:40]}}", flush=True)
"""


def main() -> bool:
    if not cuda_ipc_available():
        print("SKIP: no CUDA GPU available on this host")
        return True

    repo_root = str(Path(__file__).parent.parent)
    sock_path = tempfile.mktemp(suffix="_cuda_demo.sock")

    import os
    with BrokerServer(socket_path=sock_path) as broker:
        time.sleep(0.05)

        # Start consumer subprocess
        consumer_code = CONSUMER_SCRIPT.format(repo_root=repo_root)
        proc = subprocess.Popen(
            [sys.executable, "-c", consumer_code, sock_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        with BrokerClient("producer", socket_path=sock_path) as prod:
            with CudaIpcBuffer.alloc(size=BUF_SIZE) as buf:
                # Write payload to GPU
                buf.write(PAYLOAD)
                handle_b64 = handle_to_b64(buf.export_handle())
                print(f"  GPU buffer allocated: {BUF_SIZE // 1024} KiB on device {buf.device}")
                print(f"  IPC handle: {handle_b64[:24]}...")

                # Send handle to consumer via broker
                prod.send("consumer", json.dumps({
                    "handle": handle_b64,
                    "size": BUF_SIZE,
                    "payload_len": len(PAYLOAD),
                }))
                print(f"  Sent handle to consumer via broker")

                # Wait for consumer acknowledgment
                deadline = time.time() + 15.0
                reply = None
                while time.time() < deadline:
                    reply = prod.recv()
                    if reply:
                        break
                    time.sleep(0.05)

        proc.wait(timeout=10)
        stdout = proc.stdout.read()
        stderr = proc.stderr.read()

    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass  # BrokerServer.stop() already removed it

    if not reply:
        print("FAIL: no reply from consumer")
        return False

    result = json.loads(reply["payload"])
    if result["status"] != "ok":
        print(f"FAIL: consumer reported error")
        return False

    received = result["data"].encode()
    match = received == PAYLOAD
    print(f"  Consumer read: {received[:50]}...")
    print(f"  Data matches:  {match}")
    print(f"  Consumer log:  {stdout.strip()}")

    return match


if __name__ == "__main__":
    print("SelfConnect Linux — CUDA IPC Producer-Consumer via Broker")
    print("Phase 2 (AF/UNIX broker) + Phase 4 (CUDA IPC)\n")
    ok = main()
    print(f"\n{'─'*50}")
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)

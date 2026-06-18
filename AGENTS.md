# SelfConnect Linux — Agent Integration Guide

For AI agents (Claude, local models, orchestrators) running on DGX Spark or any
Linux host and using SelfConnect Linux to spawn, control, or coordinate other agents.

---

## Phase 1 — PTY agent lane

The core primitive. Spawn any CLI process as a PTY subprocess; control its
stdin/stdout directly via the master FD, no terminal emulator required.

```python
from self_connect_linux import spawn_pty_agent, capture_identity, verify_identity

# Spawn a bash worker
with spawn_pty_agent(["/bin/bash", "--norc"]) as agent:
    ident = capture_identity(agent.pid)   # snapshot at spawn time

    agent.send("echo READY\n")
    text, receipt = agent.expect(r"READY", timeout=5.0)

    # Verify identity before any destructive action
    verify_identity(ident, capture_identity(agent.pid))
    agent.send("rm /tmp/stale_lock\n")

    response, receipt = agent.read(timeout=3.0)
    print("Receipt:", receipt.receipt_id, "pid:", receipt.pid)
```

**Spawning a Claude Code session:**
```python
with spawn_pty_agent(["claude", "--dangerously-skip-permissions"]) as agent:
    agent.expect(r"›", timeout=30.0)
    agent.send("Summarize README.md in 3 bullets.\n")
    response, receipt = agent.expect(r"›", timeout=120.0)
    print(response)
```

**Spawning a local model (Ollama):**
```python
with spawn_pty_agent(["ollama", "run", "llama3.2"]) as agent:
    agent.expect(r">>>", timeout=60.0)
    agent.send("Explain CUDA IPC in one sentence.\n")
    response, _ = agent.expect(r">>>", timeout=30.0)
    print(response)
```

**tmux (optional durability layer):**
```python
from self_connect_linux import tmux_agent

if tmux_agent.is_available():
    tmux_agent.new_session("worker-1", cmd="claude --dangerously-skip-permissions")
    # human can attach: tmux attach -t worker-1
```

---

## Phase 2 — AF/UNIX broker

Agent-to-agent messaging with identity-verified leases. Each connected agent gets
a UUID lease backed by SO_PEERCRED + /proc identity. Messages route through per-agent
mailboxes; no shared memory or file system required.

```python
from self_connect_linux import BrokerServer, BrokerClient

# Start the broker (one per host or container, long-lived)
with BrokerServer() as broker:  # socket: /run/user/$UID/selfconnect/broker.sock

    # Agent A
    with BrokerClient("orchestrator") as orch:
        # Agent B (in a subprocess or thread)
        with BrokerClient("worker-1") as w1:
            orch.send("worker-1", "process batch_042")

            import time; time.sleep(0.05)
            msg = w1.recv()
            print(msg["from"], "→", msg["payload"])

            w1.send("orchestrator", "batch_042 done")
            reply = orch.recv()
            print(reply["payload"])
```

**Broker transport for CUDA IPC handles:**
```python
import json
from self_connect_linux import BrokerClient, CudaIpcBuffer, handle_to_b64, handle_from_b64

# Producer agent
with BrokerClient("producer") as prod:
    with CudaIpcBuffer.alloc(size=4 * 1024 * 1024) as buf:
        buf.write(embedding_bytes)
        prod.send("consumer", json.dumps({
            "type": "cuda_buffer",
            "handle": handle_to_b64(buf.export_handle()),
            "size": buf.size,
        }))
        # keep buf alive until consumer signals done

# Consumer agent (different process)
with BrokerClient("consumer") as cons:
    msg = cons.recv()
    info = json.loads(msg["payload"])
    with CudaIpcBuffer.from_handle(handle_from_b64(info["handle"]), info["size"]) as remote:
        data = remote.read()
```

**SO_PEERCRED is verified on every connection.** The broker records each client's
pid, uid, gid, proc_start_time, exe_path, exe_sha256, and namespace IDs at hello
time. A reconnecting agent with the same name but a different process identity gets
a new lease — the old client's messages are not accessible.

---

## Phase 3 — memfd/eventfd zero-copy IPC

For large payloads (token streams, embeddings, images) that must cross process
boundaries with no copy. The fd pair can be sent over a socketpair or the broker.

```python
from self_connect_linux import MemfdChannel, EventfdChannel, send_fds, recv_fds
import socket

# Create a shared region and a signal channel
ch = MemfdChannel.create(size=16 * 1024 * 1024)   # 16 MiB
sig = EventfdChannel.create()

# Sender: write payload, then signal
ch.write(large_token_stream)
ch.seal()       # no further writes — receiver can map read-only
sig.signal(1)   # wake the receiver

# Pass the FDs to another process
a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
send_fds(a, [ch.fd, sig.fd])

# Receiver process: wait for signal, then read
fds = recv_fds(b, 2)
r_ch = MemfdChannel(fds[0], size=16 * 1024 * 1024, owner=False)
r_sig = EventfdChannel(fds[1], owner=False)

r_sig.wait()          # blocks until sender calls sig.signal()
data = r_ch.read()    # zero-copy: maps the same physical pages
```

**Integrating with the broker (no socketpair needed):**
```python
# In practice, pass the FDs alongside a broker message:
# 1. Producer creates ch + sig, sends their FDs over an AF_UNIX socketpair
#    that both agents share (established at startup).
# 2. Producer sends a broker message with metadata (size, offset, schema).
# 3. Consumer waits on sig, reads ch.
```

---

## Phase 4 — CUDA IPC

Zero-copy GPU buffer sharing between processes on the same host. No nvlink or
GPU-direct networking required — works for any two processes on the same DGX Spark.

```python
from self_connect_linux import CudaIpcBuffer, cuda_ipc_available, handle_to_b64

if not cuda_ipc_available():
    raise RuntimeError("No CUDA GPU available")

# Producer
with CudaIpcBuffer.alloc(size=512 * 1024 * 1024, device=0) as buf:
    buf.write(model_weights_bytes)
    handle = handle_to_b64(buf.export_handle())   # 88-char string, safe to JSON
    # ... send handle to consumer via broker, file, or any channel ...
    # keep buf alive until consumer signals it has finished mapping

# Consumer (subprocess or separate process)
from self_connect_linux import handle_from_b64
with CudaIpcBuffer.from_handle(handle_from_b64(handle), size=512 * 1024 * 1024) as remote:
    weights = remote.read()   # cudaMemcpy D→H
    # or pass remote._ptr directly to a CUDA kernel
```

**Key constraints (confirmed on GB10 Grace Blackwell):**
- `from_handle()` is cross-process only — same-process open returns CUDA error 201.
- The exporting process must keep the buffer alive until the consumer is done.
- Handles serialize cleanly to base64 and pass through JSON broker messages.

---

## Phase 5 — X11 input + AT-SPI accessibility tree

For agents that control GUI applications on hosts with an X11 display (GNOME,
XFCE, VS Code, Chromium, terminal emulators).

**Finding and typing into windows:**
```python
from self_connect_linux import x11_available, find_window, focus_window, type_text, send_key

if not x11_available():
    raise RuntimeError("No X11 display available")

# Find a terminal window
win = find_window(wm_class="gnome-terminal")
if win:
    focus_window(win.window_id)
    type_text(win.window_id, "echo hello from agent\n")

# Or send a specific key
send_key(win.window_id, "Return")
```

**Reading the accessibility tree (AT-SPI):**
```python
from self_connect_linux import at_spi_available
from self_connect_linux import at_spi

if at_spi_available():
    apps = at_spi.list_applications()
    terminal = at_spi.find_application("gnome-terminal-server")
    if terminal:
        text = at_spi.get_text(terminal)
        print("Terminal text:", text[:200])
```

---

## Identity verification before acting

Always capture identity at spawn time. Verify before destructive sends.

```python
from self_connect_linux import spawn_pty_agent, capture_identity, verify_identity, LinuxTargetMismatch

with spawn_pty_agent(["/bin/bash"]) as agent:
    ident = capture_identity(agent.pid)

    # ... time passes ...

    try:
        verify_identity(ident, capture_identity(agent.pid))
    except LinuxTargetMismatch as e:
        raise RuntimeError(f"Target changed — refusing to send: {e}")

    agent.send("critical_operation.sh\n")
```

`verify_identity()` is fail-closed: if an expected field (pid, exe_sha256, namespace)
differs in the observed snapshot, it raises immediately. If `/proc` is unreadable
and the expected field was set, it also raises — never silently passes.

---

## Multi-agent orchestration pattern

```python
import threading
from self_connect_linux import spawn_pty_agent, BrokerServer, BrokerClient, capture_identity

WORKERS = ["worker-a", "worker-b", "worker-c"]
TASKS = ["task_1", "task_2", "task_3"]

with BrokerServer() as broker:
    # Spawn PTY workers
    agents = {}
    for name in WORKERS:
        a = spawn_pty_agent(["/bin/bash", "--norc"])
        agents[name] = {"agent": a, "ident": capture_identity(a.pid)}

    # Orchestrator connects to broker
    with BrokerClient("orchestrator") as orch:
        # Worker threads connect and process messages
        def worker_loop(name, agent_ctx):
            with BrokerClient(name) as wc:
                while True:
                    msg = wc.recv()
                    if msg is None:
                        import time; time.sleep(0.1); continue
                    task = msg["payload"]
                    if task == "STOP":
                        break
                    agent_ctx["agent"].send(f"echo {task}_DONE\n")
                    out, _ = agent_ctx["agent"].expect(r"DONE", timeout=10.0)
                    wc.send("orchestrator", f"{task} completed")

        threads = [threading.Thread(target=worker_loop, args=(n, agents[n]), daemon=True)
                   for n in WORKERS]
        for t in threads: t.start()

        # Distribute tasks
        for task, worker in zip(TASKS, WORKERS):
            orch.send(worker, task)

        # Collect results
        results = []
        for _ in TASKS:
            import time
            for _ in range(100):
                msg = orch.recv()
                if msg: results.append(msg["payload"]); break
                time.sleep(0.05)

        for worker in WORKERS:
            orch.send(worker, "STOP")

    for ctx in agents.values():
        ctx["agent"].close()

print("Results:", results)
```

---

## Receipt audit trail

Every PTY send/read/expect and every broker send produces an `ActionReceipt`.

```python
from self_connect_linux import receipt_to_json

receipts = []
with spawn_pty_agent(["/bin/bash"]) as agent:
    r1 = agent.send("echo audit_test\n")
    text, r2 = agent.read(timeout=3.0)
    receipts.extend([r1, r2])

for r in receipts:
    print(receipt_to_json(r))
# {"receipt_id": "...", "timestamp": 1750..., "backend": "pty",
#  "pid": 12345, "action": "send", "payload_hash": "abc123...",
#  "readback_hash": "def456...", "echo_filtered": true, "success": true}
```

---

## Platform capability check

```python
from self_connect_linux import capabilities, cuda_ipc_available, shm_available
from self_connect_linux import x11_available, at_spi_available

caps = capabilities()
# spark-3cdf: pty=True, tmux=True, memfd_create=True, eventfd=True,
#             x11=True, wayland=False, dbus=True, docker=True, nvidia_ctk=True

if caps["pty"]:          # spawn PTY agents
if caps["memfd_create"]: # use MemfdChannel
if cuda_ipc_available(): # use CudaIpcBuffer
if x11_available():      # use find_window / type_text
if at_spi_available():   # use at_spi.list_applications
```

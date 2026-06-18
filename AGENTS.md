# SelfConnect Linux — Agent Integration Guide

This document is for AI agents (Claude, Codex, local models) running on DGX Spark
and using SelfConnect Linux to control other agents or processes.

---

## What you can do here

SelfConnect Linux gives you a governed channel to:

1. **Spawn a CLI agent or model process under a PTY** — you control its stdin/stdout via the master FD, no terminal window needed
2. **Send prompts and read responses** — without screenshots, OCR, or screen capture
3. **Verify target identity before acting** — fail-closed if the process has changed or restarted
4. **Record a receipt for every action** — proof of what was sent, to what target, and what came back
5. **Optionally attach tmux** — for human-visible sessions and persistent recovery across disconnects

---

## Agent spawning — the fast path

```python
from pty_agent import spawn_pty_agent

# Spawn a local Claude Code session under a PTY
with spawn_pty_agent(["claude", "--dangerously-skip-permissions"]) as agent:

    # Wait for the ready prompt
    text, _ = agent.expect(r"›", timeout=30.0)

    # Send a task
    agent.send("Read README.md and summarize it in 3 bullets.\n")

    # Read the response (accumulate until prompt returns)
    response, receipt = agent.expect(r"›", timeout=120.0)
    print(response)
    print("Receipt:", receipt.receipt_id, "success:", receipt.success)
```

The receipt proves: what was sent (`payload_hash`), what came back (`readback_hash`), and the PID of the process that produced it.

---

## Identity verification before acting

Always capture identity at spawn time. Verify before destructive sends.

```python
from pty_agent import spawn_pty_agent
from identity import capture_identity, verify_identity, LinuxTargetMismatch

with spawn_pty_agent(["/bin/bash"]) as agent:
    # Snapshot identity right after spawn
    ident = capture_identity(agent.pid)

    # ... time passes, other work happens ...

    # Verify target is still the same process before acting
    try:
        verify_identity(ident, capture_identity(agent.pid))
    except LinuxTargetMismatch as e:
        raise RuntimeError(f"Target changed — refusing to send: {e}")

    agent.send("rm important_file.txt\n")
```

`verify_identity()` checks: pid, uid, gid, proc_start_time_ticks, exe_path, exe_sha256, cgroup, namespaces. Any mismatch raises `LinuxTargetMismatch` before the action runs.

---

## Approval gate pattern

When orchestrating agents that run other agents, use the expect + pattern approach to detect approval prompts:

```python
APPROVAL_PATTERNS = [
    r"Do you want to proceed\?",
    r"\[y/N\]",
    r"Allow this tool",
    r"Press Enter to confirm",
]

import re

def handle_approval(agent, auto_approve: bool = False):
    text, _ = agent.read(timeout=2.0)
    for pat in APPROVAL_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            if auto_approve:
                agent.send("y\n")
                return True
            else:
                print(f"Approval required:\n{text}")
                choice = input("Approve? [y/n]: ").strip().lower()
                agent.send(choice + "\n")
                return choice == "y"
    return None  # no approval prompt found
```

Never blindly send "y". Always check what tool is being approved.

---

## Running a model process (Ollama / llama.cpp / vllm)

```python
from pty_agent import spawn_pty_agent

# Ollama (blocking interactive session)
with spawn_pty_agent(["ollama", "run", "llama3.2"]) as agent:
    agent.expect(r">>>", timeout=60.0)          # wait for model ready
    agent.send("Explain PTY in one sentence.\n")
    response, _ = agent.expect(r">>>", timeout=30.0)
    print(response)

# llama.cpp server (non-interactive, use subprocess instead)
import subprocess
proc = subprocess.Popen(
    ["./llama-server", "-m", "model.gguf", "--port", "8080"],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
)
```

For server-mode models, use HTTP directly. PTY is for interactive sessions.

---

## tmux — optional durability layer

Use tmux when you want:
- Human-visible sessions (you can attach with `tmux attach`)
- Persistent sessions that survive your agent's disconnect
- Multiple panes (orchestrator + worker side by side)

```python
from tmux_agent import is_available, new_session, send_keys, capture_pane, kill_session

if not is_available():
    print("tmux not installed — using raw PTY instead")
else:
    new_session("worker-1", cmd="claude --dangerously-skip-permissions")
    import time; time.sleep(2.0)

    send_keys("worker-1", "echo WORKER_READY", enter=True)
    time.sleep(0.5)

    text, _ = capture_pane("worker-1")
    print(text)

    kill_session("worker-1")
```

tmux is never required. If absent, all core PTY functionality still works.

---

## Multi-agent mesh pattern (PTY)

Orchestrate multiple agents from a single controller:

```python
from pty_agent import spawn_pty_agent
from identity import capture_identity

agents = {}

# Spawn three workers
for name in ["worker-a", "worker-b", "worker-c"]:
    agent = spawn_pty_agent(["/bin/bash", "--norc"])
    agents[name] = {
        "agent": agent,
        "identity": capture_identity(agent.pid),
    }

import time; time.sleep(0.3)

# Broadcast a task
for name, ctx in agents.items():
    ctx["agent"].send(f"echo {name.upper()}_ONLINE\n")

# Collect responses
for name, ctx in agents.items():
    text, receipt = ctx["agent"].read(timeout=5.0)
    print(f"{name}: {text.strip()!r}  receipt={receipt.receipt_id[:8]}")

# Clean up
for ctx in agents.values():
    ctx["agent"].close()
```

---

## Container agent pattern (Docker + NVIDIA runtime)

For GPU-accelerated agents:

```python
import subprocess

# Spawn a container agent with GPU access
proc = subprocess.run([
    "docker", "run", "--rm", "--gpus", "all",
    "--name", "sc-gpu-agent",
    "-i",                          # keep stdin open
    "nvcr.io/nvidia/cuda:13.0-runtime-ubuntu24.04",
    "/bin/bash",
], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

# Or via PTY for interactive sessions:
from pty_agent import spawn_pty_agent

with spawn_pty_agent([
    "docker", "run", "--rm", "--gpus", "all", "-it",
    "nvcr.io/nvidia/cuda:13.0-runtime-ubuntu24.04", "/bin/bash"
]) as agent:
    agent.expect(r"root@", timeout=30.0)
    agent.send("nvidia-smi\n")
    text, _ = agent.expect(r"\$", timeout=10.0)
    print(text)
```

---

## Receipt audit trail

Every send/read/expect call produces an `ActionReceipt`. Log them for audit:

```python
import json
from receipts import receipt_to_json

receipts = []

with spawn_pty_agent(["/bin/bash"]) as agent:
    r1 = agent.send("echo audit_test\n")
    text, r2 = agent.read(timeout=3.0)
    receipts.extend([r1, r2])

for r in receipts:
    print(receipt_to_json(r))
```

Each receipt contains:
- `receipt_id` — UUID
- `timestamp` — Unix float
- `backend` — "pty" / "tmux"
- `pid` — target process PID
- `action` — "send" / "read" / "expect"
- `payload_hash` — sha256 of what was sent
- `readback_hash` — sha256 of what came back
- `echo_filtered` — whether injected text was stripped
- `success` / `error`

---

## What comes next (Phase 2–4)

### Phase 2 — AF_UNIX lease broker

The broker at `broker.py` already has the socket path and SO_PEERCRED call. Phase 2 adds:

```python
from broker import BrokerClient
client = BrokerClient()
cred = client.ping()
# → {"status": "ok", "peer": {"pid": 12345, "uid": 1000, "gid": 1000}}
```

Phase 2 will extend this to issue short-lived leases backed by full /proc identity.

### Phase 3 — memfd/eventfd IPC bus

Fast agent-to-agent messaging without file I/O:

```python
# Not yet implemented — Phase 3
from shm import MemfdChannel
ch = MemfdChannel(size=4 * 1024 * 1024)
ch.write(b"large token stream...")
data = ch.read()
```

### Phase 4 — CUDA IPC

Share GPU tensors between agents on the same DGX Spark without CPU copy:

```python
# Not yet implemented — Phase 4
from cuda_ipc import export_handle, import_handle
handle = export_handle(device_ptr, size)
# pass handle over AF_UNIX to another process
ptr = import_handle(handle)
```

---

## Platform capability check

Always check capabilities before using optional features:

```python
from platform import capabilities
caps = capabilities()

if caps["pty"]:
    # spawn PTY agents
    pass

if caps["tmux"]:
    # use tmux adapter
    pass

if caps["memfd_create"]:
    # Phase 3: memfd IPC available
    pass

if caps["nvidia_ctk"]:
    # Phase 4: NVIDIA container runtime available
    pass
```

On `spark-3cdf`:
```
pty=True, tmux=True, memfd_create=True, eventfd=True,
x11=True, wayland=False, dbus=True, docker=True, nvidia_ctk=True
```

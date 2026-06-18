# SelfConnect Linux

Linux-native agent control layer for DGX Spark (Ubuntu 24.04, aarch64, NVIDIA Grace Blackwell).

The Windows peer of this library is [selfconnect-alt](https://github.com/rblake2320/selfconnect-alt). That repo implements eight Win32 optimizations (UIA, ConPTY, DXGI, SharedMemIPC, etc.) for Windows desktop AI agents. This repo is the Linux equivalent — built on Linux-native primitives, not a port of Win32 concepts.

---

## What this is

SelfConnect Linux gives AI agents a governed, identity-verified channel to:

- Spawn and drive other agents as PTY processes (the Linux equivalent of ConPTY)
- Read and write agent I/O without screenshots or screen capture
- Verify target identity from `/proc` before every action
- Record cryptographic receipts linking caller, target, action, and readback
- Run entirely without a GUI — optimized for DGX Spark headless/compute workloads

The design priority for DGX Spark is:

```
PTY / tmux / process-native        ← primary: this package, Phase 1
AF_UNIX broker + /proc identity    ← identity layer, Phase 2
memfd / eventfd / epoll            ← fast IPC, Phase 3
CUDA IPC / NCCL / NVSHMEM         ← GPU data plane, Phase 4
AT-SPI / X11 / Wayland portals    ← GUI control when needed, Phase 5+
```

GUI control is one surface. PTY and process control is the core.

---

## Current status — Phase 0 + Phase 1

Confirmed working on `spark-3cdf` (DGX Spark, Ubuntu 24.04.4 LTS, aarch64, NVIDIA GB10, CUDA 13.0, Python 3.13.11):

| Capability | Status |
|---|---|
| `os.openpty` PTY lane | Working |
| `spawn_pty_agent()` — spawn any CLI process | Working |
| `send()` / `read()` / `expect()` — PTY master I/O | Working |
| Echo-filter — strip injected text from readback | Working |
| `capture_identity(pid)` — /proc snapshot | Working |
| `verify_identity()` — fail-closed target guard | Working |
| `ActionReceipt` — JSON receipt per action | Working |
| `capabilities()` — runtime feature detection | Working |
| tmux adapter (optional, skips cleanly if absent) | Working |
| AF_UNIX broker skeleton (SO_PEERCRED) | Skeleton, Phase 2 |
| memfd / eventfd IPC | Phase 3 |
| CUDA IPC GPU bus | Phase 4 |
| AT-SPI / X11 / Wayland | Phase 5 |

---

## Quick start

```bash
git clone https://github.com/rblake2320/selfconnect-linux.git
cd selfconnect-linux
pip install -e .[dev]
```

```python
from pty_agent import spawn_pty_agent

# Spawn bash, send a command, read the output
with spawn_pty_agent(["/bin/bash", "--norc"]) as agent:
    agent.send("echo hello from DGX\n")
    text, receipt = agent.read(timeout=5.0)
    print(text)          # "hello from DGX"
    print(receipt)       # ActionReceipt with payload_hash + readback_hash
```

```python
from identity import capture_identity, verify_identity

# Snapshot a process identity from /proc
ident = capture_identity(pid=12345)
print(ident.exe_path)              # /usr/bin/python3
print(ident.proc_start_time_ticks) # kernel clock ticks at spawn
print(ident.cgroup_path)           # /user.slice/...

# Verify it hasn't changed (fail-closed)
verify_identity(ident, capture_identity(12345))  # raises LinuxTargetMismatch if stale
```

```python
from platform import capabilities
print(capabilities())
# {'pty': True, 'tmux': True, 'memfd_create': True, 'eventfd': True,
#  'x11': True, 'wayland': False, 'dbus': True, 'docker': True, 'nvidia_ctk': True}
```

---

## Package layout

```
selfconnect-linux/
  __init__.py         Public API exports
  platform.py         Runtime capability detection (no side effects)
  identity.py         LinuxTargetIdentity, capture_identity(), verify_identity()
  pty_agent.py        spawn_pty_agent(), PtyAgent — core PTY lane
  tmux_agent.py       Optional tmux adapter (skips cleanly if absent)
  receipts.py         ActionReceipt, make_receipt(), receipt_to_json()
  broker.py           AF_UNIX broker skeleton (SO_PEERCRED — Phase 2)
  tests/
    test_identity.py
    test_platform.py
    test_pty_agent.py
    test_receipts.py
    test_tmux_agent.py
```

---

## Running tests

```bash
# All tests — no GUI, no root, no CUDA required
python -m pytest tests/ -v

# PTY tests only
python -m pytest tests/test_pty_agent.py -v
```

All 34 tests pass on DGX Spark. 2 skip cleanly (tmux-absent paths — only triggered when tmux is not installed).

---

## The Linux action hierarchy

For DGX Spark, use the safest semantic lane first:

```
1. PTY master write          ← send text to agent stdin directly
2. tmux send-keys            ← durable PTY with human-visible session
3. container exec            ← docker exec / NVIDIA runtime
4. AF_UNIX socket            ← structured agent-to-agent messaging (Phase 2)
5. AT-SPI action             ← semantic GUI control (Phase 5)
6. X11 XTEST                 ← synthetic input on X11 (Phase 5)
7. Wayland portal            ← RemoteDesktop + PipeWire (Phase 5)
8. uinput fallback           ← last resort only
```

On DGX, most agent control never leaves the top 4.

---

## Linux vs Windows feature map

| selfconnect-alt (Win32) | selfconnect-linux |
|---|---|
| ConPTY own-pipe | `os.openpty()` + `spawn_pty_agent()` |
| WriteConsoleInput | `PtyAgent.send()` |
| ReadConsoleOutput | `PtyAgent.read()` |
| HWND target guard | `LinuxTargetIdentity` + `verify_identity()` |
| SID-bound pipe lease | `AF_UNIX SO_PEERCRED` + `/proc` (Phase 2) |
| SharedMemIPC | `memfd_create` + `eventfd` + `epoll` (Phase 3) |
| DXGI capture | PipeWire ScreenCast portal (Phase 5) |
| UIA CacheRequest | AT-SPI2 D-Bus tree (Phase 5) |
| SendInput batching | XTEST / uinput (Phase 5) |
| (no equivalent) | CUDA IPC same-host GPU buffers (Phase 4) |
| (no equivalent) | NCCL / NVSHMEM multi-Spark data plane (Phase 4) |

---

## DGX Spark capability probe

Run this to inventory your DGX Spark before deploying agents:

```bash
python - <<'EOF'
from platform import capabilities
import json
print(json.dumps(capabilities(), indent=2))
EOF
```

A full shell probe is included in `sc_linux_probe_spark-3cdf_20260617_194300.log`.

---

## Phase roadmap

| Phase | What | When |
|---|---|---|
| 0 | Platform split — Linux package, no Win32 imports | Done |
| 1 | PTY agent lane — spawn/send/read/expect/receipts | Done |
| 2 | AF_UNIX broker — SO_PEERCRED, /proc leases, registry | Next |
| 3 | memfd/eventfd/epoll — fast agent-to-agent IPC bus | After Phase 2 |
| 4 | CUDA IPC + NCCL — DGX GPU data plane | After Phase 3 |
| 5 | AT-SPI, X11, Wayland portals, PipeWire | After Phase 4 |

---

## Related

- [selfconnect-alt](https://github.com/rblake2320/selfconnect-alt) — Windows peer (Win32, ConPTY, DXGI, UIA)

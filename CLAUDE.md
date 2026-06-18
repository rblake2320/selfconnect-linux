# SelfConnect Linux — Claude Instructions

## What this repo is

Linux-native SelfConnect agent control layer for DGX Spark (Ubuntu 24.04 aarch64,
NVIDIA GB10 Grace Blackwell, CUDA 13.0).

This is NOT a port of Win32 automation. Do not reach for Win32 concepts here.

The Windows peer is `selfconnect-alt` (github.com/rblake2320/selfconnect-alt).
It uses ConPTY, HWND, UIA, DXGI, WriteConsoleInput, ReadConsoleOutput. None of
those exist on Linux. Use the Linux equivalents documented below.

---

## Machine context

- Host: `spark-3cdf`
- OS: Ubuntu 24.04.4 LTS, kernel 6.17.0-1008-nvidia
- Arch: aarch64 (ARM64 — Grace Blackwell)
- GPU: NVIDIA GB10, CUDA 13.0, Driver 580.126.09, 128GB unified memory
- Python: 3.13.11 (Miniconda) — primary; /usr/bin/python3 (3.12, system) for gi/AT-SPI
- Session: X11 (DISPLAY=:1), GNOME 44
- PTY: /dev/ptmx, max 4096 PTYs
- tmux: 3.4 installed (optional — never a hard dependency)
- memfd_create: True (Python 3.13, Linux 6.17)
- eventfd: True
- XTEST: True (python-xlib 0.33 in Miniconda)
- AT-SPI: True (/usr/bin/python3 + python3-gi, system packages)
- Docker: 29.2.1 + NVIDIA Container Runtime 1.19.1
- nvidia-ctk: 1.19.1

---

## Architecture rules

### PTY is the primary agent control lane

```python
spawn_pty_agent(["/bin/bash"]) → PtyAgent
agent.send("command\n")        → write to PTY master
agent.read(timeout=5.0)        → read from PTY master
agent.expect(r"\$")            → wait for pattern
agent.close()                  → SIGTERM + cleanup
```

Do not drive CLI agents through screenshots. Do not require a terminal emulator
to be open. Use the PTY master FD directly.

### tmux is optional, never required

`tmux_agent.is_available()` returns True on this machine (tmux 3.4), but:
- Core PTY tests must pass without tmux
- `tmux_agent` functions raise `RuntimeError("tmux not found")` when tmux is absent
- Tests that need tmux use `@pytest.mark.skipif(not shutil.which("tmux"), ...)`

### Identity is always /proc, never HWND

```python
ident = capture_identity(pid)
# → LinuxTargetIdentity with:
#   pid, uid, gid, proc_start_time_ticks, exe_path, exe_sha256,
#   cgroup_path, pid_namespace, mount_namespace, net_namespace

verify_identity(expected, capture_identity(pid))  # raises LinuxTargetMismatch if stale
```

Never skip `verify_identity()` before a destructive PTY send.

### Every action produces a receipt

```python
receipt = agent.send("rm -rf /\n")
# receipt.payload_hash    — sha256 of what was sent
# receipt.receipt_id      — uuid
# receipt.backend         — "pty"
# receipt.pid             — target pid
# receipt.success         — True/False
```

Receipts are how you prove what was sent, to what target, and what came back.

### AT-SPI requires system Python

The Miniconda Python (3.13) does not have `gi.repository`. AT-SPI queries run
through a subprocess bridge calling `/usr/bin/python3`. Use `at_spi_available()`
before any AT-SPI call. X11 input (XTEST) uses python-xlib which IS available
in Miniconda — no subprocess bridge needed.

---

## File layout

```
self_connect_linux/
  __init__.py     Public API — import from here
  platform.py     capabilities() — no side effects
  identity.py     LinuxTargetIdentity, capture_identity(), verify_identity()
  pty_agent.py    spawn_pty_agent(), PtyAgent — core PTY primitive
  tmux_agent.py   Optional tmux adapter
  receipts.py     ActionReceipt, make_receipt(), receipt_to_json()
  broker.py       AF_UNIX broker — SO_PEERCRED leases + agent mailbox (Phase 2)
  shm.py          MemfdChannel, EventfdChannel, send_fds/recv_fds (Phase 3)
  cuda_ipc.py     CudaIpcBuffer, handle_to_b64/from_b64 (Phase 4)
  x11_input.py    Window discovery + XTEST keyboard/mouse injection (Phase 5)
  at_spi.py       AT-SPI accessibility tree access via subprocess bridge (Phase 5)
  tests/          All tests — no GUI, no root needed
examples/
  two_agent_task.py           PTY workers + broker coordination
  cuda_producer_consumer.py   CUDA IPC handle handoff via broker
```

---

## What NOT to do

- Do not import `ctypes.windll`, `pywin32`, `comtypes`, or any Win32 module
- Do not use `tkinter` or GUI frameworks as the agent control path
- Do not require tmux for core PTY functionality
- Do not use screenshots as the primary readback channel for CLI agents
- Do not call `os.system()` or `subprocess.run()` where PTY write would work
- Do not hardcode `/dev/tty` paths — use `os.openpty()`
- Do not make the broker depend on an HTTP server — it is AF_UNIX only
- Do not skip `verify_identity()` before acting on a target
- Do not import `gi` or `dbus` at top level — gate on `at_spi_available()`
- Do not assume X11 availability in tests — skip with `@pytest.mark.skipif(not x11_available(), ...)`

---

## Running tests

```bash
# Full suite (no root, no CUDA needed for non-GPU tests)
python -m pytest tests/ -v

# By phase
python -m pytest tests/test_pty_agent.py -v          # Phase 1
python -m pytest tests/test_broker.py -v             # Phase 2
python -m pytest tests/test_shm.py -v                # Phase 3
python -m pytest tests/test_cuda_ipc.py -v           # Phase 4 (needs GPU)
python -m pytest tests/test_x11_input.py -v          # Phase 5 (needs DISPLAY)
python -m pytest tests/test_at_spi.py -v             # Phase 5 (needs AT-SPI)

# With coverage
python -m pytest tests/ --cov=self_connect_linux --cov-report=term-missing
```

Expected on spark-3cdf: all pass, 2 skipped (tmux absent guards).
CUDA tests skip on machines without a GPU. X11/AT-SPI tests skip without a display.

---

## Phase map

| Phase | Scope | Status |
|---|---|---|
| 0 | Platform split — no Win32 imports on Linux | Done |
| 1 | PTY agent lane, identity, receipts, tmux adapter | Done |
| 2 | AF/UNIX broker — SO_PEERCRED, /proc leases, agent mailbox | Done |
| 3 | memfd/eventfd zero-copy IPC bus + FD passing | Done |
| 4 | CUDA IPC — cross-process GPU buffer sharing | Done |
| 5 | X11 input injection + AT-SPI accessibility tree | Done |
| 6 | NCCL metadata, multi-GPU coordination | Not started |
| 7 | Container/namespace isolation, cgroup control | Not started |

---

## Linux primitive quick reference

| Need | Use |
|---|---|
| Spawn agent and control its stdin/stdout | `spawn_pty_agent()` → PTY master |
| Make agent session persistent + human-visible | `tmux_agent.new_session()` |
| Verify target is the same process before acting | `capture_identity()` + `verify_identity()` |
| Agent-to-agent message passing (JSON, local) | `BrokerServer` / `BrokerClient` (Phase 2) |
| Fast zero-copy large payload transfer | `MemfdChannel` + `EventfdChannel` (Phase 3) |
| Signal between processes without busy-wait | `EventfdChannel.signal()` / `.wait()` (Phase 3) |
| Pass FDs across process boundaries | `send_fds()` / `recv_fds()` (Phase 3) |
| Share GPU tensor between agents (same host) | `CudaIpcBuffer` (Phase 4) |
| Find and focus a GUI window | `find_window()` / `focus_window()` (Phase 5) |
| Type text into a GUI application | `type_text()` (Phase 5) |
| Read GUI widget content via accessibility tree | `at_spi.get_text()` (Phase 5) |
| Credential-verified IPC lease | AF_UNIX + SO_PEERCRED (Phase 2) |

# SelfConnect Linux — Claude Instructions

## What this repo is

Linux-native SelfConnect agent control layer for DGX Spark (Ubuntu 24.04 aarch64, NVIDIA GB10 Grace Blackwell, CUDA 13.0).

This is NOT a port of Win32 automation. Do not reach for Win32 concepts here.

The Windows peer is `selfconnect-alt` (github.com/rblake2320/selfconnect-alt). It uses ConPTY, HWND, UIA, DXGI, WriteConsoleInput, ReadConsoleOutput. None of those exist on Linux. Use the Linux equivalents documented below.

---

## Machine context

- Host: `spark-3cdf`
- OS: Ubuntu 24.04.4 LTS, kernel 6.17.0-1008-nvidia
- Arch: aarch64 (ARM64 — Grace Blackwell)
- GPU: NVIDIA GB10, CUDA 13.0, Driver 580.126.09
- Python: 3.13.11 (Anaconda)
- Session: X11 (DISPLAY=:1), GNOME
- PTY: Available — /dev/ptmx, max 4096 PTYs
- tmux: 3.4 installed (optional — never a hard dependency)
- memfd_create: True (Python 3.13, Linux 6.17)
- eventfd: True
- Docker: 29.2.1 + NVIDIA Container Runtime 1.19.1
- nvidia-ctk: 1.19.1

---

## Architecture rules

### PTY is the primary agent control lane

```
spawn_pty_agent(["/bin/bash"]) → PtyAgent
agent.send("command\n")        → write to PTY master
agent.read(timeout=5.0)        → read from PTY master
agent.expect(r"\$")            → wait for pattern
agent.close()                  → SIGTERM + cleanup
```

Do not drive CLI agents through screenshots. Do not require a terminal emulator to be open. Use the PTY master FD directly.

### tmux is optional, never required

`tmux_agent.is_available()` returns True on this machine (tmux 3.4), but:
- Core PTY tests must pass without tmux
- `tmux_agent` functions raise `RuntimeError("tmux not found")` when tmux is absent
- Tests that need tmux use `@pytest.mark.skipif(not shutil.which("tmux"), ...)`

### Identity is always /proc, never HWND

The Linux equivalent of HWND + SID check:

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

---

## File layout

```
selfconnect-linux/
  __init__.py     Public API — import from here
  platform.py     capabilities() — no side effects
  identity.py     LinuxTargetIdentity, capture_identity(), verify_identity()
  pty_agent.py    spawn_pty_agent(), PtyAgent (core primitive)
  tmux_agent.py   Optional tmux adapter
  receipts.py     ActionReceipt, make_receipt(), receipt_to_json()
  broker.py       AF_UNIX broker skeleton (SO_PEERCRED)
  tests/          All tests — no GUI, no root needed
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
- Do not assume X11 availability — headless DGX runs have no display

---

## Running tests

```bash
# All tests (no root, no GUI, no CUDA needed)
python -m pytest tests/ -v

# PTY lane only
python -m pytest tests/test_pty_agent.py -v

# With coverage
python -m pytest tests/ --cov=. --cov-report=term-missing
```

Expected: 34 passed, 2 skipped (tmux-absent guard paths).

---

## Phase map (v0.4.0)

| Phase | Scope | Status |
|---|---|---|
| 0 | Platform split — no Win32 imports on Linux | **Done** |
| 1 | PTY agent lane, identity, receipts, tmux adapter | **Done** |
| 2 | AF_UNIX broker — SO_PEERCRED, /proc leases, messaging | **Done** (`broker.py`) |
| 3 | memfd/eventfd zero-copy IPC bus | **Done** (`shm.py`) |
| 4 | CUDA IPC — zero-copy GPU buffer sharing, tested on GB10 | **Done** (`cuda_ipc.py`) |
| 5 | AT-SPI, X11, Wayland portals, PipeWire | Not started |

92 tests pass, 2 skip (tmux-absent guard paths). All tests run without root, GUI, or CUDA.

---

## Linux primitive quick reference

| Need | Use |
|---|---|
| Spawn agent and control its stdin/stdout | `spawn_pty_agent()` → PTY master |
| Make agent session persistent + human-visible | `tmux_agent.new_session()` |
| Verify target is the same process before acting | `capture_identity()` + `verify_identity()` |
| OS-verified agent-to-agent identity | `BrokerClient` + AF_UNIX `SO_PEERCRED` (`broker.py`) |
| Fast zero-copy agent-to-agent payload | `MemfdChannel` + `EventfdChannel` (`shm.py`) |
| Share GPU tensor between agents, zero CPU copy | `CudaIpcBuffer` + CUDA IPC handle (`cuda_ipc.py`) |
| Semantic GUI tree access | AT-SPI2 / D-Bus (Phase 5 — not started) |
| Screen capture on X11 | XComposite / XShm (Phase 5 — not started) |
| Screen capture on Wayland | PipeWire ScreenCast portal (Phase 5 — not started) |
| Synthetic keyboard input | XTEST (X11) / uinput fallback (Phase 5 — not started) |

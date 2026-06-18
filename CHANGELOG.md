# Changelog

## [0.9.2] - 2026-06-18
### Added
- `examples/aihangout_live_demo.py` — 11-step non-headless browser demo running fully visible on DISPLAY=:1. Registers an AI Agent account, logs in, browses the feed, reads and posts a problem, visits profile, and explores the Knowledge Hub. Credentials via env vars (`AIHANGOUT_USER`, `AIHANGOUT_EMAIL`, `AIHANGOUT_PASS`).

### Changed
- `browser.py` `fill()` — uses `HTMLInputElement.prototype` / `HTMLTextAreaElement.prototype` native value setter instead of direct `.value =`. React controlled inputs compare the internal fiber value and silently skip `onChange` on direct assignment; the native setter bypasses that check and causes React to fire the synthetic event normally.

### Validated on aihangout.ai (Cloudflare + Next.js + React)
- `aihangout.ai/problem/260` — posted live by `selfconnect-dgx1` (🤖 AI Agent on GB10 Grace Blackwell)
- React form techniques confirmed working:
  - Input/textarea: `Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, v)` + dispatch `input`/`change`
  - Select: same pattern via `HTMLSelectElement.prototype`
  - CDP coordinate click: `scrollIntoView` → `getBoundingClientRect` → `Input.dispatchMouseEvent` (mousePressed + mouseReleased) — required because Cloudflare Bot Management only passes trusted events with real coordinates
- URL discovery: real paths are `/create-problem`, `/profile/<username>`, `/learning` — discovered by reading `<a href>` at runtime, not assumed

## [0.9.1] - 2026-06-18
### Fixed (cross-machine compatibility — found by running test suite on both spark-3cdf and spark-3173)
- `nccl.py` `generate_unique_id()`: cupy 13.x returns `tuple[int,...]`, cupy 14.x returns `bytes` — normalize to `bytes`
- `nccl.py` `NcclComm.init()`: inverse — cupy 13.x `NcclCommunicator` expects `tuple` back; probe and convert at init time
- `test_container.py`: hardcoded assertions `len(gpu_containers()) > 0` with hostname literal `spark-3cdf` failed on any other host — replaced with `pytest.skip()` when no containers present
- `browser.py` CDP target selection: snap Chromium prepends `chrome-extension://` background targets before real page tabs in `/json/list`; fixed by filtering for `type == "page"` first
- `browser.py` launch flags: initially added `--disable-gpu --disable-software-rasterizer` as a workaround for snap Chromium xcb/ANGLE failures in SSH sessions. **This was wrong.** After researching Ubuntu bug [#1959416](https://bugs.launchpad.net/bugs/1959416) and Chromium Ozone docs, the correct fix is `--ozone-platform=headless` which uses Chromium's native headless display abstraction (no X11/xcb dependency, GPU not disabled). The `--disable-gpu` commits are preserved in git history at `59d0701` and `ba25cec` for reference.

## [0.9.0] - 2026-06-18
### Added
- Phase 8: Browser control (`browser.py`) — SelfConnect spawns Chromium as a PTY subprocess, captures `/proc` identity, then drives it via a raw CDP WebSocket built from Python stdlib only. No Playwright. No Selenium. No external framework.
- `BrowserSession`: `goto()`, `click()`, `fill()`, `press()`, `type_text()`, `get_text()`, `screenshot()`, `evaluate()`, `wait_for()` — every mutation returns an `ActionReceipt`
- `_CdpSocket`: minimal RFC 6455 WebSocket client over raw `socket.socket` — zero external dependencies
- `browser_available()` capability gate
- 18 new tests in `test_browser.py` including real internet navigation to example.com and GitHub API

### Design note
SelfConnect is aware Playwright exists. The point is SelfConnect does not need it.
The browser is just another agent process: spawned via subprocess, identity-captured
via `/proc`, driven via the same local-socket primitives as the rest of the stack.

## [0.8.0] - 2026-06-18
### Added
- Phase 6: NCCL coordination layer (`nccl.py`) — rank negotiation via broker, UniqueId exchange, `NcclComm` wrapper for allreduce/broadcast on NVIDIA GB10
- Phase 7: Container/namespace isolation (`container.py`) — `list_containers()`, `gpu_containers()`, `cgroup_info()`, `container_identity()` with cgroup v2 resource limit reads
- 30 new tests: `test_nccl.py` (15 NCCL tests including two-rank negotiate) and `test_container.py` (15 container/cgroup tests)
- `make_chained_receipt` exported from top-level package

### Changed
- `__version__` bumped to 0.8.0
- All 8 phases complete: 197 tests pass, 2 skipped (tmux guards)

## [0.6.0] - 2026-06-18
### Security
- Fixed impostor eviction attack: broker now stores recipient identity at grant time; impostors that evict and re-register are denied because their proc_start_time_ticks differ
- Added 256 KB per-message size limit to prevent memory exhaustion via oversized messages
- Added 256-handler semaphore cap to prevent thread exhaustion DoS
- Added grant TTL expiry: grants are purged after LEASE_TTL_SECONDS to prevent stale handle accumulation
- Socket permissions enforced to 0o600 (verified in test_socket_permissions_0600)
- Added gpu_uuid to identity verification for all processes (not just self-process)

### Added
- 13 new tests: test_broker_security.py (8 tests), test_broker_load.py (5 tests)
- BrokerServer.get_stats() method for observability
- SECURITY.md vulnerability disclosure policy

### Changed
- __version__ bumped to 0.6.0
- ProvenanceLedger, ChainedReceipt and related classes exported from top-level package
- examples/attested_gpu_share.py updated to use CudaIpcBuffer (ctypes) API

## [0.5.0] - 2026-06-17
### Added
- Kernel-attested CUDA IPC: OS kernel identity as authorization gate for zero-copy GPU memory sharing
- cuda_ipc.py: CudaIpcBuffer class (ctypes-based, no cupy dependency for core operations)
- provenance.py: hash-chained ProvenanceLedger with tamper detection
- broker.py: grant/claim protocol with SO_PEERCRED attestation
- identity.py: gpu_uuid, cuda_context_id fields populated
- PATENT.md: provisional-ready patent disclosure with 12 claims (3 independent)
- examples/attested_gpu_share.py: reduction-to-practice demo

## [0.4.0] - 2026-06-16
### Added
- Phase 3: memfd/eventfd zero-copy IPC bus (shm.py)
- Phase 5: X11 input injection (x11_input.py) and AT-SPI accessibility tree (at_spi.py)
- AF_UNIX broker Phase 2: full SO_PEERCRED lease issuance, agent mailboxes

## [0.1.0] - 2026-06-14
### Added
- Phase 0/1: PTY agent lane, /proc identity, receipts, tmux adapter
- Initial selfconnect-linux package

# Changelog

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

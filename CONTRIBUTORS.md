# Contributors

## Human

**Rob Blake** ([@rblake2320](https://github.com/rblake2320))
- Project owner and architect
- DGX Spark hardware access and validation
- Design direction, requirements, and review

## AI Co-Author

**Claude Sonnet 4.6** (Anthropic)
- Co-authored all phases of this codebase via [Claude Code](https://claude.com/claude-code)
- Attributed in every commit as `Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>`

Phases built in live collaboration on `spark-3cdf` (NVIDIA GB10 Grace Blackwell):

| Phase | Scope | Primary author |
|---|---|---|
| 0 | Platform split — no Win32 imports on Linux | Claude (pts/1) |
| 1 | PTY agent lane, /proc identity, receipts, tmux adapter | Claude (pts/1) |
| 2 | AF/UNIX broker — SO_PEERCRED leases + agent mailbox | Claude (pts/0) |
| 3 | memfd/eventfd zero-copy IPC bus + FD passing | Claude (pts/1) |
| 4 | CUDA IPC — cross-process GPU buffer sharing | Claude (pts/1) |

Two Claude Code sessions (pts/0 and pts/1) ran concurrently on the same DGX Spark
host and coordinated work via PTY writes and shared files — using the very primitives
this library implements.

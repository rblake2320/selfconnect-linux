# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.6.0   | Yes (current) |
| < 0.6.0 | No |

## Reporting a Vulnerability

Email **rob47595@gmail.com** with the subject line:

```
selfconnect-linux security
```

Please include:
- A description of the vulnerability
- Steps to reproduce
- Affected version(s)
- Any suggested mitigations

**Response time:** 72-hour acknowledgment; 14-day patch target.

Do not open a public GitHub issue for security vulnerabilities.

## Scope

The following are **in scope**:

- The broker's `SO_PEERCRED` identity verification (process identity attestation via Linux kernel)
- CUDA IPC handle gating (authorization checks before sharing GPU memory handles)
- Provenance ledger integrity (hash-chain tamper detection in `ProvenanceLedger`)

## Out of Scope

- Issues requiring **physical access** to the GPU host
- Vulnerabilities in third-party dependencies (report those upstream)

## Security Model

**Attacker model:** Any process on the same Linux host that can reach the `AF_UNIX` socket.

**Defenses:**

- **SO_PEERCRED kernel attestation** — the Linux kernel fills `ucred` (pid, uid, gid) on every
  connection; the broker validates these against `/proc/<pid>` state including
  `proc_start_time_ticks` to defeat PID-reuse attacks.
- **0o600 socket permissions** — the broker socket is created with `chmod 0o600` so only the
  owning user can connect (verified in `test_socket_permissions_0600`).
- **Impostor eviction protection** — the broker records the recipient's identity at grant time;
  a process that evicts the original recipient and re-registers is denied because its
  `proc_start_time_ticks` differs from the stored identity.
- **Per-message size limit** — messages are capped at 256 KB to prevent memory exhaustion via
  oversized payloads.
- **Semaphore cap** — a 256-handler semaphore limits concurrent connections to prevent thread
  exhaustion DoS.
- **Grant TTL expiry** — grants are purged after `LEASE_TTL_SECONDS` to prevent stale handle
  accumulation.

## PGP

No PGP key is published yet. Use the email address above.

# PATENT DISCLOSURE — PROVISIONAL FILING REFERENCE

**Title:** Kernel-Attested Zero-Copy GPU Memory Sharing for AI Agents with Hash-Chained Tensor Provenance

**Inventor:** Robert Blake  
**Contact:** rob47595@gmail.com  
**First Public Reduction to Practice:** 2026-06-17  
**Repository:** https://github.com/rblake2320/selfconnect-linux  
**Platform:** NVIDIA DGX Spark GB10 Grace Blackwell, CUDA 13.0, Ubuntu 24.04 aarch64

> ⚠️ **US Patent Law Notice:** This public disclosure starts a 12-month grace period to file a US non-provisional patent (35 U.S.C. § 102(b)(1)(A)). In most non-US jurisdictions, public disclosure may forfeit patent rights without a prior filing. Consult a patent attorney promptly.

---

## Field of the Invention

The invention relates to inter-process communication security and AI agent orchestration. More specifically, it relates to a method and system for authorizing zero-copy GPU device-memory sharing between AI agent processes using OS-kernel-attested process identity as the access-control gate, with every transfer committed to a tamper-evident hash-chained provenance ledger.

---

## Background

### The Problem

Modern AI inference workloads run as multiple cooperating processes — AI agents — that must exchange large tensor data. The dominant method for high-throughput tensor sharing within a single host is **CUDA Inter-Process Communication (IPC)**: one process exports a 64-byte opaque handle (`cudaIpcGetMemHandle`) that another process uses to map the same physical GPU device memory with zero data movement (`cudaIpcOpenMemHandle`).

**Critical security gap:** CUDA IPC provides no access control. Any process that possesses the 64-byte handle can map the GPU memory. There is no mechanism to verify that the process mapping the memory is the intended recipient, or that it has not been replaced by a malicious or compromised process.

This gap has three practical consequences for multi-agent AI systems:

1. **Impersonation:** An attacker process that intercepts a handle (via IPC eavesdrop, memory snooping, or compromised message bus) can map and mutate another agent's GPU buffers.
2. **No auditability:** There is no cryptographic record of which processes accessed which GPU buffers, making compliance, debugging, and post-incident forensics impossible.
3. **Silent corruption:** A compromised agent that maps a shared buffer and injects adversarial data cannot be detected after the fact.

### Prior Art Landscape

| Technology | What it does | Why it does NOT cover this invention |
|---|---|---|
| NVIDIA CUDA IPC (`cudaIpcGetMemHandle` / `cudaIpcOpenMemHandle`) | Zero-copy GPU buffer sharing between same-host processes | No access control — any process with the handle bytes can map the memory. No identity verification. |
| SO_PEERCRED (Linux, used in D-Bus, systemd, Polkit) | Verify the pid/uid/gid of the peer process on an AF_UNIX socket | Used for IPC service authorization only. Never applied to GPU memory authorization or AI agents. No chained provenance. |
| SPIFFE / SPIRE | Cryptographic workload identity for services (X.509 SVIDs via kernel primitives) | Issues cryptographic certificates for service-level identity. Does not gate CUDA IPC or per-tensor provenance. |
| Amazon US10298577B1 | Digital signature-based credential vending for processes | Digital signatures issued by a vending service; not kernel-derived at claim time. No GPU or CUDA component. No chained provenance. |
| NVIDIA Confidential Computing / H100 TEE | Remote attestation of GPU hardware state to a remote relying party | Hardware → remote relying party. Not peer-process attestation for local buffer sharing. |
| IETF draft-sharif-agent-audit-trail-00 | Hash-chained audit trail for AI agent actions | Action-level audit only (no GPU). No identity attestation. No CUDA component. |
| NCCL / NVSHMEM / MPI | Collective tensor communication between training ranks | All ranks trusted equally. No per-transfer identity verification, no access control, no provenance ledger. |
| C2PA (content authenticity) | Provenance of AI-generated content files | File / media artifacts, not live GPU memory transfers. No kernel attestation. |

**Confirmed white space:** No located prior art covers the combination of (1) OS-kernel-derived process identity as the authorization gate for (2) CUDA IPC GPU buffer sharing between AI agent processes, with (3) a hash-chained per-transfer provenance ledger.

---

## Summary of the Invention

The invention introduces a **kernel-attested GPU memory broker** that interposes between AI agent processes, using Linux OS primitives as a trust anchor:

1. **Export:** An agent process (the *exporter*) allocates a GPU device buffer and exports a CUDA IPC handle. It deposits the handle with the broker along with the identity of the intended *importer* agent.

2. **Attestation gate:** When an agent process claims the handle, the broker uses `SO_PEERCRED` (kernel-verified peer credentials over an AF_UNIX socket) to read the claimant's PID without trusting any value the claimant provides. It then reads `/proc/<pid>/exe`, `/proc/<pid>/stat` (process start time), `/proc/<pid>/cgroup`, Linux namespace IDs (PID, mount, network namespaces from `/proc/<pid>/ns/`), executable SHA-256, and GPU UUID — all without cooperation from the claimant process. This assembled identity is compared against the identity bound at lease registration. If any field mismatches, the claim is denied.

3. **Zero-copy transfer:** Only after kernel attestation succeeds is the CUDA IPC handle delivered to the claimant, which calls `cudaIpcOpenMemHandle` to map the same physical GPU memory without any data movement.

4. **Hash-chained provenance:** Every handle grant and claim is committed to a tamper-evident ledger. Each entry includes: action type, exporter and importer agent names, GPU UUID, buffer fingerprint (SHA-256 of GPU memory contents), attested PID and executable hash, timestamp, and a `chain_hash = SHA-256(prev_chain_hash || canonical_entry_fields)`. This structure makes any post-hoc insertion, deletion, modification, or reordering of ledger entries detectable.

---

## Detailed Description

### System Architecture

The system comprises five cooperating components:

```
 ┌─────────────────────────────────────────────────────────────┐
 │                    AI Agent Host Process                    │
 │                                                             │
 │  ┌──────────────┐    AF_UNIX socket    ┌──────────────────┐ │
 │  │   Agent A    │ ◄──────────────────► │  Kernel-Attested │ │
 │  │  (exporter)  │   grant{handle,fp}   │  GPU Broker      │ │
 │  │              │                      │                  │ │
 │  │  cupy tensor │   claim{handle_id}   │  SO_PEERCRED     │ │
 │  │  GPU memory  │ ◄──────────────────► │  /proc identity  │ │
 │  └──────────────┘                      │  verify_identity │ │
 │         ▲                              │  ProvenanceLedger│ │
 │         │  zero-copy GPU IPC           └──────────────────┘ │
 │         │  (same physical VRAM)                ▲            │
 │  ┌──────┴───────┐    AF_UNIX socket    ─────────┘            │
 │  │   Agent B    │ ◄──────────────────►  claim{handle_id}     │
 │  │  (importer)  │   granted{handle}                          │
 │  │              │                                            │
 │  └──────────────┘                                            │
 └─────────────────────────────────────────────────────────────┘
```

**Component 1 — AF_UNIX Broker (`broker.py: BrokerServer`):**  
An AF_UNIX stream socket server that accepts connections from agent processes. On every connection, `getsockopt(SO_PEERCRED)` is called immediately — before any application data — to obtain the kernel-verified `(pid, uid, gid)` of the connecting process. A `hello` handshake binds the connection to a named agent ID and captures the process identity via `/proc` at hello time (a `LinuxTargetIdentity`). UUID-keyed leases expire after 60 seconds.

**Component 2 — Process Identity Capture (`identity.py: capture_identity`):**  
Reads the following fields from `/proc/<pid>/` without trusting any value provided by the target process:
- `exe` symlink → executable path and SHA-256 hash
- `stat` → process start time in clock ticks (survives PID reuse)
- `cgroup` → Linux cgroup path
- `/proc/<pid>/ns/pid`, `/proc/<pid>/ns/mnt`, `/proc/<pid>/ns/net` → namespace inode numbers
- `nvidia-smi --query-gpu=gpu_uuid` → GPU device UUID bound to the process's device

**Component 3 — CUDA IPC primitives (`cuda_ipc.py`):**  
Thin wrappers over `cupy.cuda.runtime.ipcGetMemHandle` (export) and `cupy.cuda.runtime.ipcOpenMemHandle` (import). The 64-byte IPC handle token is treated as an opaque byte string, transmitted through the broker as hex-encoded JSON. Critically, the handle is **never delivered** to a process that failed kernel attestation.

**Component 4 — Grant/Claim protocol (`broker.py: grant_gpu / claim_gpu`):**  

*Grant (exporter → broker):*
```json
{
  "type": "grant",
  "lease": "<uuid>",
  "to": "agent-B",
  "gpu_handle": "<64-byte handle as hex>",
  "size_bytes": 32,
  "buffer_fingerprint": "sha256:<hex>"
}
```

*Claim (importer → broker):*
```json
{
  "type": "claim",
  "lease": "<uuid>",
  "handle_id": "<broker-assigned uuid>"
}
```

The broker, on receiving a claim:
1. Reads the claimant's PID via `SO_PEERCRED` (not from the message).
2. Calls `capture_identity(cred.pid)` — reads `/proc` fields fresh at claim time.
3. Calls `verify_identity(lease.identity, fresh_identity)` — raises `LinuxTargetMismatch` on any field mismatch.
4. Only if attestation passes: delivers the CUDA IPC handle and appends a `claim` receipt to the ledger.
5. If attestation fails: sends a `denied` message and appends a `deny` receipt to the ledger.

**Component 5 — Hash-Chained Provenance Ledger (`provenance.py: ProvenanceLedger`):**  

Each `ChainedReceipt` contains:
- `receipt_id` (UUID), `timestamp`, `action` (grant/claim/deny)
- `from_agent`, `to_agent`
- `gpu_uuid` (attested GPU device binding)
- `buffer_fingerprint` (SHA-256 of device memory contents at grant time)
- `handle_id` (broker-assigned grant UUID)
- `attested_pid`, `attested_exe_sha256` (kernel-verified identity of claimant)
- `prev_chain_hash`, `chain_hash`

The chain hash is computed as:
```
chain_hash = SHA-256(
    prev_chain_hash ||
    JSON(receipt_id, timestamp, action, from_agent, to_agent,
         gpu_uuid, buffer_fingerprint, handle_id, success)
)
```

`ProvenanceLedger.verify_chain()` recomputes each entry's `chain_hash` from its predecessor and raises `ChainBroken` on any discrepancy, detecting tampering, insertion, deletion, or reordering.

### Reduction to Practice

The working demonstration (`examples/attested_gpu_share.py`) executed on a DGX Spark GB10 Grace Blackwell (CUDA 13.0, Ubuntu 24.04 aarch64) on 2026-06-17 produced the following verified results:

```
✓ Zero-copy GPU share confirmed:  True    (Agent A saw Agent B's mutation)
✓ Impostor denied:                True    (Wrong handle_id rejected by broker)
✓ Provenance chain intact:        True    (2-entry chain verified)
✓ Ledger entries:                 2
✅ ALL PASS — PATENT CLAIM PROVEN
```

The GPU tensor allocated by Agent A (`cupy.arange(8, dtype=float32) * 10.0`) was mapped zero-copy by Agent B after passing kernel attestation. Agent B wrote `999.0` to element `[0]`. Agent A, reading its own array after receiving Agent B's done-signal, observed `arr[0] == 999.0` — proving the two processes share the same physical GPU device memory without any data copy.

---

## Claims

### Independent Claims

**Claim 1.** A computer-implemented method for authorizing shared access to GPU device memory between AI agent processes, the method comprising:

- maintaining, by a broker process, an AF_UNIX stream socket server on which the broker calls `getsockopt(SO_PEERCRED)` on every accepted connection to obtain the kernel-verified process ID of the connecting agent process without relying on any agent-supplied value;
- receiving, from a first agent process, a GPU IPC handle token and a designation of a second agent process authorized to import the GPU device memory referenced by said handle token;
- receiving, from a second agent process, a request to claim said GPU IPC handle token;
- capturing, by the broker process, the process identity of the second agent process by reading one or more of: the executable path from `/proc/<pid>/exe`, the process start time from `/proc/<pid>/stat`, the executable SHA-256 hash, Linux namespace inode numbers from `/proc/<pid>/ns/`, the cgroup path from `/proc/<pid>/cgroup`, and a GPU device UUID — all without trusting any value supplied by the second agent process;
- comparing the captured process identity of the second agent process against a previously recorded identity;
- transmitting the GPU IPC handle token to the second agent process if and only if the captured process identity matches the previously recorded identity, and denying the request otherwise; and
- recording each grant, claim, and denial as an entry in a hash-chained ledger wherein each entry's chain hash is a cryptographic hash of the previous entry's chain hash concatenated with a canonical representation of the entry's fields.

**Claim 2.** A system for kernel-attested GPU memory sharing between AI agent processes, the system comprising:

- a broker process executing on a computing host and maintaining an AF_UNIX socket server;
- a first agent process that allocates a GPU device buffer, exports a CUDA Inter-Process Communication (IPC) handle identifying said buffer, and transmits said handle to the broker process along with an identifier of a second agent process designated as the authorized importer;
- the broker process configured to, upon receiving a claim request from a second agent process: (a) obtain the kernel-verified process ID of the second agent process from the Linux kernel via `SO_PEERCRED` without accepting any process-supplied value; (b) read process identity fields from the Linux `/proc` filesystem for said kernel-verified process ID; (c) compare the read fields against identity fields recorded at lease time; and (d) transmit the CUDA IPC handle to the second agent process only upon successful comparison, enabling the second agent process to call `cudaIpcOpenMemHandle` to map the same physical GPU device memory without data movement; and
- a tamper-evident provenance ledger recording each handle grant, successful claim, and denied claim as a hash-chained entry binding the attested identities of the participating processes to the GPU buffer fingerprint and the handle identifier.

**Claim 3.** A non-transitory computer-readable medium storing instructions that, when executed by a processor, implement a method for authorizing GPU device memory sharing between processes, the method comprising:

- intercepting, at a trusted broker process, a GPU IPC handle export from a first process;
- storing a binding between said GPU IPC handle, the identity of the first process as derived from the Linux kernel's `/proc` filesystem, and an authorized importer process identifier;
- receiving a claim request from a second process;
- obtaining the process identifier of said second process from the Linux kernel via `SO_PEERCRED`;
- deriving a real-time process identity for said second process from the Linux kernel's `/proc` filesystem using said kernel-verified process identifier;
- releasing the GPU IPC handle to the second process only if the derived real-time identity matches the authorized importer identity; and
- appending to a hash-chained ledger a record of the transaction including a fingerprint of the GPU buffer contents and the attested process identities.

### Dependent Claims

**Claim 4.** The method of Claim 1, wherein capturing the process identity further comprises reading one or more Linux namespace inode numbers from `/proc/<pid>/ns/pid`, `/proc/<pid>/ns/mnt`, and `/proc/<pid>/ns/net`, and comparing said inode numbers as part of the identity verification step.

**Claim 5.** The method of Claim 1, wherein the GPU device UUID is obtained by querying the NVIDIA System Management Interface (nvidia-smi) and is included in the recorded identity and in the ledger entries.

**Claim 6.** The method of Claim 1, wherein the GPU buffer fingerprint is a cryptographic hash (SHA-256) of the device memory contents computed on the GPU device, obtained by transferring device memory to host memory solely for the purpose of computing said fingerprint, and wherein said fingerprint is stored in the provenance ledger entry.

**Claim 7.** The method of Claim 1, wherein the hash-chained ledger further enables offline integrity verification by a third party that recomputes each chain hash from public fields and detects any insertion, deletion, modification, or reordering of entries.

**Claim 8.** The system of Claim 2, wherein the broker process enforces a lease expiry time after which the GPU IPC handle is removed from broker state and cannot be claimed.

**Claim 9.** The system of Claim 2, wherein the identity fields read from the Linux `/proc` filesystem include the process start time in kernel clock ticks, providing resistance to PID reuse attacks in which a newly-spawned malicious process acquires the PID of a previously-authorized process.

**Claim 10.** The system of Claim 2, wherein the second agent process is a machine-learning inference process and the GPU device buffer contains a tensor produced by a first machine-learning inference process, and wherein the zero-copy sharing enables the second inference process to operate on said tensor without a host-device memory copy.

**Claim 11.** The method of Claim 1, further comprising: when the identity comparison fails, transmitting a denial message to the requesting process and recording a denial entry in the hash-chained ledger with the mismatched identity fields and a failure reason, enabling forensic analysis of unauthorized access attempts.

**Claim 12.** A method according to Claim 1, wherein the AF_UNIX socket uses `SOCK_STREAM` and `SO_PEERCRED` is called immediately upon `accept()`, before any application-layer data is read, ensuring that the kernel-verified credentials cannot be influenced by any subsequent action of the connecting process.

---

## Abstract

A method, system, and computer-readable medium for authorizing zero-copy GPU device-memory sharing between AI agent processes on a Linux host. A trusted broker process uses the Linux kernel's `SO_PEERCRED` socket option to obtain the kernel-verified process ID of each connecting agent without relying on agent-supplied values. At GPU buffer claim time, the broker reads process identity fields directly from the `/proc` filesystem — including executable SHA-256, process start time, Linux namespace inode numbers, and GPU UUID — and compares them against identity fields recorded at lease time. The GPU IPC handle is released only to a process whose kernel-derived identity matches the authorized importer. Every handle grant, successful claim, and denied claim is committed to a tamper-evident hash-chained provenance ledger binding the attested identities of the participating processes to the GPU buffer fingerprint and a broker-assigned handle identifier. The chain structure enables offline detection of any post-hoc insertion, deletion, modification, or reordering of ledger entries.

---

## Figure Descriptions

**Figure 1 — System Overview:** Block diagram showing Agent A (exporter), Broker Server, and Agent B (importer) connected via AF_UNIX sockets. Arrows show the grant flow (A → Broker), the kernel attestation step (Broker → `/proc`), the claim flow (B → Broker → B), and the zero-copy GPU memory mapping (A's GPU buffer ← B maps same physical VRAM). The Provenance Ledger is shown attached to the Broker with hash-chain links between entries.

**Figure 2 — Grant/Claim Protocol Sequence:** Sequence diagram with swimlanes for Agent A, Broker, Linux Kernel, and Agent B. Shows: (1) A calls `ipcGetMemHandle`, (2) A sends `grant{handle_hex, size, fingerprint, to="B"}` to Broker, (3) Broker issues `handle_id` lease, (4) B sends `claim{handle_id}` to Broker, (5) Broker calls `getsockopt(SO_PEERCRED)` → Kernel returns `(pid, uid, gid)`, (6) Broker reads `/proc/<pid>/exe`, `/stat`, `/ns/*` → assembles `LinuxTargetIdentity`, (7) Broker calls `verify_identity(expected, fresh)`, (8a) if match: Broker sends `granted{handle_hex}` to B, B calls `ipcOpenMemHandle` → maps GPU buffer; (8b) if mismatch: Broker sends `denied` to B.

**Figure 3 — Hash-Chained Provenance Ledger:** Diagram showing linked `ChainedReceipt` nodes. Genesis block contains `prev=sha256:000...`. Each node shows: `receipt_id`, `action`, `from_agent`, `to_agent`, `buffer_fingerprint`, `attested_pid`, and `chain_hash = SHA-256(prev_chain_hash || canonical_fields)`. An arrow from a tampered node shows how `verify_chain()` detects the break when the recomputed hash diverges from the stored hash.

**Figure 4 — Identity Fields from `/proc`:** Table showing the Linux `/proc` fields read by `capture_identity(pid)` and their security properties: `exe` (binary path), `exe_sha256` (prevents swapped binary), `proc_start_time_ticks` (prevents PID reuse), `pid_namespace`, `mount_namespace`, `net_namespace` (container isolation), `cgroup_path` (workload scheduler binding), `gpu_uuid` (hardware binding).

---

*End of Patent Disclosure*

*First public reduction to practice: git commit on https://github.com/rblake2320/selfconnect-linux, 2026-06-17*  
*Inventor: Robert Blake, rob47595@gmail.com*

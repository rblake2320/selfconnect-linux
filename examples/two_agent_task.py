"""
Two-agent task distribution via PTY + broker.

Demonstrates the core orchestration pattern:
  - Orchestrator spawns N PTY bash workers
  - Distributes tasks via the AF/UNIX broker
  - Collects results with receipts

Run:
    python examples/two_agent_task.py
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from self_connect_linux import (
    BrokerClient,
    BrokerServer,
    capture_identity,
    spawn_pty_agent,
    verify_identity,
)

WORKER_NAMES = ["worker-alpha", "worker-beta"]
TASKS = [
    "echo TASK_1_hostname=$(hostname)",
    "echo TASK_2_date=$(date +%s)",
    "echo TASK_3_pid=$$",
    "echo TASK_4_cuda=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)",
]


def worker_loop(name: str, bash_agent, broker_sock: str, results: list, lock: threading.Lock):
    """Worker thread: connects to broker, processes tasks, sends results back."""
    with BrokerClient(name, socket_path=broker_sock) as wc:
        while True:
            msg = wc.recv()
            if msg is None:
                time.sleep(0.05)
                continue
            task = msg["payload"]
            if task == "__STOP__":
                break
            bash_agent.send(task + "\n")
            output, receipt = bash_agent.expect(r"TASK_\d+_", timeout=10.0)
            # Extract the echo result
            line = next(
                (l for l in output.splitlines() if l.startswith("TASK_")),
                output.strip()
            )
            with lock:
                results.append({
                    "worker": name,
                    "task": task,
                    "result": line,
                    "receipt_id": receipt.receipt_id,
                })
            wc.send("orchestrator", line)


def main():
    import tempfile, os
    sock_path = tempfile.mktemp(suffix=".sock")

    with BrokerServer(socket_path=sock_path) as broker:
        time.sleep(0.05)  # let accept loop start

        # Spawn PTY bash workers
        agents = {}
        for name in WORKER_NAMES:
            a = spawn_pty_agent(["/bin/bash", "--norc"])
            a.expect(r"\$", timeout=3.0)  # wait for prompt
            agents[name] = {
                "agent": a,
                "ident": capture_identity(a.pid),
            }

        results = []
        lock = threading.Lock()
        threads = []
        for name, ctx in agents.items():
            t = threading.Thread(
                target=worker_loop,
                args=(name, ctx["agent"], sock_path, results, lock),
                daemon=True,
            )
            t.start()
            threads.append(t)

        time.sleep(0.1)  # let workers register

        with BrokerClient("orchestrator", socket_path=sock_path) as orch:
            # Distribute tasks round-robin
            for i, task in enumerate(TASKS):
                worker = WORKER_NAMES[i % len(WORKER_NAMES)]
                orch.send(worker, task)
                print(f"  → dispatched to {worker}: {task[:50]}")

            # Collect results
            collected = []
            deadline = time.time() + 15.0
            while len(collected) < len(TASKS) and time.time() < deadline:
                msg = orch.recv()
                if msg:
                    collected.append(msg["payload"])
                    print(f"  ← {msg['from']}: {msg['payload']}")
                else:
                    time.sleep(0.05)

            # Stop workers
            for name in WORKER_NAMES:
                orch.send(name, "__STOP__")

        for t in threads:
            t.join(timeout=3.0)
        for ctx in agents.values():
            ctx["agent"].close()

    print(f"\n{'─'*50}")
    print(f"Completed {len(collected)}/{len(TASKS)} tasks")
    with lock:
        for r in results:
            print(f"  {r['worker']}: {r['result']}  [{r['receipt_id'][:8]}]")

    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass  # BrokerServer.stop() already removed it
    return len(collected) == len(TASKS)


if __name__ == "__main__":
    print("SelfConnect Linux — Two-Agent Task Distribution")
    print("PTY workers + AF/UNIX broker\n")
    ok = main()
    sys.exit(0 if ok else 1)

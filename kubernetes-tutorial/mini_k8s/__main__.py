"""Start a long-running MiniK8s development cluster.

    python -m mini_k8s --port 8080 --nodes 2

The API server listens on the requested port.  In another terminal:

    python -m mini_k8s.cli --server http://127.0.0.1:8080 get nodes
"""

from __future__ import annotations

import argparse
import signal
import time

from mini_k8s.cluster import ADMIN_TOKEN, MiniCluster


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m mini_k8s")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--nodes", type=int, default=1)
    parser.add_argument("--node-cpu", default="4")
    parser.add_argument("--node-memory", default="8Gi")
    parser.add_argument("--node-gpus", type=int, default=0)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--provision-root", default="/tmp/mini-k8s-volumes")
    args = parser.parse_args()

    cluster = MiniCluster(host=args.host, port=args.port, node_count=args.nodes,
                          node_cpu=args.node_cpu, node_memory=args.node_memory,
                          node_gpus=args.node_gpus,
                          verbose=args.verbose, storage_provision_root=args.provision_root)
    cluster.start()
    print(f"MiniK8s API server: {cluster.url}", flush=True)
    print(f"Admin token:        {ADMIN_TOKEN}", flush=True)
    print(f"Nodes:              {', '.join(cluster.kubelets)}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    stopping = False

    def handle_signal(signum, frame):  # pragma: no cover - interactive path
        nonlocal stopping
        stopping = True

    try:
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
    except (ValueError, AttributeError):
        pass
    try:
        while not stopping:
            time.sleep(0.25)
    finally:
        print("Stopping MiniK8s...")
        cluster.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

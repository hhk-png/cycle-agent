"""Start an in-process server, use the remote client, then shut it down."""

from __future__ import annotations

import threading

from _common import RUNTIME, print_json, unique_experiment

from mini_mlflow.client import MiniClient
from mini_mlflow.config import MiniConfig
from mini_mlflow.server import create_server


def main() -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    config = MiniConfig(
        backend_store_uri=f"sqlite:///{(RUNTIME / 'server-demo.db').as_posix()}",
        artifact_root=str(RUNTIME / "server-artifacts"),
        port=0,
        auth_token="demo-token",
        cors_origin="https://example.test",
    )
    server = create_server(config, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    client = MiniClient(base, auth_token="demo-token")
    try:
        experiment = unique_experiment(client, "remote-demo")
        run = client.start_run(experiment_id=experiment.experiment_id, run_name="remote-run")
        client.log_param(run.info.run_id, "transport", "http")
        client.log_metric(run.info.run_id, "latency_ms", 12.5)
        client.log_text(run.info.run_id, "uploaded over HTTP", "notes.txt")
        client.end_run(run.info.run_id, "FINISHED")
        stored = client.get_run(run.info.run_id)
        print_json(
            {
                "server": base,
                "health": client.health(),
                "experiment": experiment.name,
                "run_id": stored.info.run_id,
                "status": stored.info.status,
                "metrics": stored.data.metrics,
                "artifacts": [item.path for item in client.list_artifacts(stored.info.run_id)],
            }
        )
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()

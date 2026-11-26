"""Serve a saved model and send a scoring request over HTTP."""

from __future__ import annotations

import json
import threading
import urllib.request

from _common import RUNTIME, print_json

from mini_mlflow.models import LinearRegression, save_model
from mini_mlflow.scoring import create_scoring_server


def main() -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    model_path = RUNTIME / "serving-model"
    model = LinearRegression().fit(
        [{"floor": 2.0}, {"floor": 5.0}, {"floor": 8.0}],
        [2.2, 4.8, 7.9],
    )
    save_model(model_path, model, input_example=[{"floor": 6.0}], overwrite=True)

    server = create_scoring_server(
        str(model_path),
        host="127.0.0.1",
        port=0,
        auth_token="demo-token",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/invocations",
            data=json.dumps(
                {"dataframe_split": {"columns": ["floor"], "data": [[6.0], [9.0]]}}
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer demo-token",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
        print_json({"model": str(model_path), "response": payload})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()

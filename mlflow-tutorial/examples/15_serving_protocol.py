"""Exercise the scoring contract: health, payload shapes, auth and errors.

Run from the tutorial root:

    python examples/15_serving_protocol.py

The example starts the MiniMLflow scoring server on a random localhost port,
sends one request per protocol shape, and asserts the status code and
prediction count.  It is intentionally offline; no tracking server is needed.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

from _common import RUNTIME, print_json

from mini_mlflow.models import LinearRegression, infer_signature, save_model
from mini_mlflow.scoring import create_scoring_server

TOKEN = "serving-contract-token"


def _http(
    base: str,
    path: str,
    *,
    payload: dict | None = None,
    raw: bytes | None = None,
    token: str | None = TOKEN,
    method: str | None = None,
) -> tuple[int, dict]:
    body = raw if raw is not None else (json.dumps(payload).encode("utf-8") if payload is not None else None)
    headers: dict[str, str] = {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        base + path,
        data=body,
        headers=headers,
        method=method or ("POST" if body is not None else "GET"),
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = response.read()
            return response.status, json.loads(data) if data else {}
    except urllib.error.HTTPError as exc:
        data = exc.read()
        try:
            parsed = json.loads(data) if data else {}
        except ValueError:
            parsed = {"raw": data.decode("utf-8", errors="replace")}
        return exc.code, parsed


def _assert_predictions(case: str, body: dict, expected: list[float]) -> None:
    predictions = body.get("predictions")
    assert isinstance(predictions, list), f"{case}: missing predictions: {body}"
    assert len(predictions) == len(expected), f"{case}: length {len(predictions)} != {len(expected)}"
    for actual, wanted in zip(predictions, expected):
        assert abs(float(actual) - wanted) < 1e-9, f"{case}: {actual} != {wanted}"


def main() -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    model_path = RUNTIME / "serving-protocol-model"
    rows = [
        {"x": 1.0, "y": 2.0},
        {"x": 2.0, "y": 1.0},
        {"x": 3.0, "y": 4.0},
        {"x": 4.0, "y": 3.0},
    ]
    targets = [2.0 * row["x"] + 3.0 * row["y"] + 1.0 for row in rows]
    model = LinearRegression().fit(rows, targets)
    signature = infer_signature(rows, model.predict(rows))
    save_model(
        model_path,
        model,
        signature=signature,
        input_example=rows[:2],
        overwrite=True,
    )

    server = create_scoring_server(
        str(model_path),
        host="127.0.0.1",
        port=0,
        auth_token=TOKEN,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    cases: list[dict] = []

    def check(
        name: str,
        path: str,
        *,
        payload: dict | None = None,
        raw: bytes | None = None,
        token: str | None = TOKEN,
        expected_status: int = 200,
        expected: list[float] | None = None,
    ) -> dict:
        status, body = _http(base, path, payload=payload, raw=raw, token=token)
        assert status == expected_status, f"{name}: expected {expected_status}, got {status}: {body}"
        if expected is not None:
            _assert_predictions(name, body, expected)
        cases.append({"case": name, "status": status, "predictions": body.get("predictions")})
        return body

    try:
        check("ping", "/ping")
        health = check("health", "/health")
        version = check("version", "/version")
        check("unknown-path", "/predict", expected_status=404)
        check("missing-token", "/invocations", payload={"instances": [{"x": 1.0, "y": 2.0}]}, token=None, expected_status=401)

        check(
            "dataframe_split",
            "/invocations",
            payload={"dataframe_split": {"columns": ["x", "y"], "data": [[1.0, 2.0], [3.0, 4.0]]}},
            expected=[9.0, 19.0],
        )
        check(
            "dataframe_records",
            "/invocations",
            payload={"dataframe_records": [{"x": 1.0, "y": 2.0}, {"x": 4.0, "y": 3.0}]},
            expected=[9.0, 18.0],
        )
        check(
            "instances-dict",
            "/invocations",
            payload={"instances": [{"x": 2.0, "y": 1.0}]},
            expected=[8.0],
        )
        check(
            "instances-rows-with-signature",
            "/invocations",
            payload={"instances": [[2.0, 1.0], [3.0, 4.0]]},
            expected=[8.0, 19.0],
        )
        check(
            "inputs-records",
            "/invocations",
            payload={"inputs": [{"x": 1.0, "y": 2.0}]},
            expected=[9.0],
        )
        check(
            "inputs-columnar",
            "/invocations",
            payload={"inputs": {"x": [1.0, 2.0], "y": [2.0, 1.0]}},
            expected=[9.0, 8.0],
        )
        check(
            "malformed-json",
            "/invocations",
            raw=b"{this is not json",
            expected_status=400,
        )
        check(
            "missing-column",
            "/invocations",
            payload={"dataframe_records": [{"x": 1.0}]},
            expected_status=400,
        )
        check(
            "wrong-type",
            "/invocations",
            payload={"dataframe_records": [{"x": "not-a-number", "y": 2.0}]},
            expected_status=400,
        )

        print_json(
            {
                "model": str(model_path),
                "health": health,
                "version": version,
                "cases": cases,
                "summary": {
                    "total": len(cases),
                    "ok": sum(1 for item in cases if item["status"] == 200),
                    "client_errors": sum(1 for item in cases if 400 <= item["status"] < 500),
                },
            }
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()

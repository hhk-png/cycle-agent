from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from mini_mlflow.errors import InvalidParameter
from mini_mlflow.models import (
    ColumnSpec,
    LinearRegression,
    ModelSignature,
    PyFuncModel,
    infer_signature,
    save_model,
)
from mini_mlflow.scoring import create_scoring_server, records_from_payload

TOKEN = "test-serving-token"


def _http(
    base: str,
    path: str,
    *,
    payload: dict | None = None,
    raw: bytes | None = None,
    token: str | None = TOKEN,
) -> tuple[int, dict]:
    body = raw if raw is not None else (json.dumps(payload).encode("utf-8") if payload is not None else None)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        base + path,
        data=body,
        headers=headers,
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = response.read()
            return response.status, json.loads(data) if data else {}
    except urllib.error.HTTPError as exc:
        data = exc.read()
        return exc.code, json.loads(data) if data else {}


def _fitted_model() -> LinearRegression:
    return LinearRegression().fit(
        [{"x": 1.0, "y": 2.0}, {"x": 2.0, "y": 1.0}, {"x": 3.0, "y": 4.0}],
        [9.0, 8.0, 19.0],
    )


def _serve(tmp_path: Path):
    model = _fitted_model()
    rows = [{"x": 1.0, "y": 2.0}]
    signature = infer_signature(rows, model.predict(rows))
    save_model(tmp_path / "model", model, signature=signature, input_example=rows, overwrite=True)
    server = create_scoring_server(str(tmp_path / "model"), host="127.0.0.1", port=0, auth_token=TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    return server, thread, base


def test_records_from_payload_shapes() -> None:
    model = _fitted_model()
    signature = ModelSignature(inputs=(ColumnSpec("x", "double"), ColumnSpec("y", "double")))
    wrapped = PyFuncModel(model=model, signature=signature)

    assert records_from_payload(
        {"dataframe_split": {"columns": ["x", "y"], "data": [[1.0, 2.0]]}}, wrapped
    ) == [{"x": 1.0, "y": 2.0}]
    assert records_from_payload({"dataframe_records": [{"x": 1.0, "y": 2.0}]}, wrapped) == [
        {"x": 1.0, "y": 2.0}
    ]
    assert records_from_payload({"instances": [{"x": 1.0, "y": 2.0}]}, wrapped) == [
        {"x": 1.0, "y": 2.0}
    ]
    assert records_from_payload({"inputs": {"x": [1.0], "y": [2.0]}}, wrapped) == {
        "x": [1.0],
        "y": [2.0],
    }
    assert records_from_payload({"columns": ["x", "y"], "data": [[1.0, 2.0]]}, wrapped) == [
        {"x": 1.0, "y": 2.0}
    ]


def test_instance_rows_need_signature_or_feature_names() -> None:
    with pytest.raises(InvalidParameter):
        records_from_payload({"instances": [[1.0, 2.0]]}, PyFuncModel(model=object()))

    model = _fitted_model()
    signature = ModelSignature(inputs=(ColumnSpec("x", "double"), ColumnSpec("y", "double")))
    assert records_from_payload(
        {"instances": [[1.0, 2.0], [3.0, 4.0]]},
        PyFuncModel(model=model, signature=signature),
    ) == [{"x": 1.0, "y": 2.0}, {"x": 3.0, "y": 4.0}]
    # A fitted flavor may also expose feature names even without a signature.
    assert records_from_payload({"instances": [[1.0, 2.0]]}, PyFuncModel(model=model)) == [
        {"x": 1.0, "y": 2.0}
    ]


def test_scoring_health_and_version(tmp_path: Path) -> None:
    server, thread, base = _serve(tmp_path)
    try:
        assert _http(base, "/ping")[0] == 200
        health = _http(base, "/health")[1]
        assert health["status"] == "ok"
        assert _http(base, "/version")[0] == 200
        assert _http(base, "/predict")[0] == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_scoring_payload_shapes_over_http(tmp_path: Path) -> None:
    server, thread, base = _serve(tmp_path)
    try:
        status, body = _http(
            base,
            "/invocations",
            payload={"dataframe_records": [{"x": 1.0, "y": 2.0}]},
        )
        assert status == 200
        assert body["predictions"] == pytest.approx([9.0])

        status, body = _http(
            base,
            "/invocations",
            payload={"inputs": {"x": [2.0], "y": [1.0]}},
        )
        assert status == 200
        assert body["predictions"] == pytest.approx([8.0])

        status, body = _http(
            base,
            "/invocations",
            payload={"instances": [[3.0, 4.0]]},
        )
        assert status == 200
        assert body["predictions"] == pytest.approx([19.0])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_scoring_auth_and_validation_errors(tmp_path: Path) -> None:
    server, thread, base = _serve(tmp_path)
    try:
        assert _http(base, "/invocations", payload={"instances": [{"x": 1.0, "y": 2.0}]}, token=None)[0] == 401
        assert _http(base, "/invocations", raw=b"{bad json")[0] == 400
        assert _http(base, "/invocations", payload={"dataframe_records": [{"x": 1.0}]})[0] == 400
        assert _http(
            base,
            "/invocations",
            payload={"dataframe_records": [{"x": "bad", "y": 2.0}]},
        )[0] == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_scoring_rejects_oversized_body(tmp_path: Path) -> None:
    server, thread, base = _serve(tmp_path)
    server.max_request_bytes = 32
    try:
        status, _ = _http(
            base,
            "/invocations",
            payload={"dataframe_records": [{"x": 1.0, "y": 2.0}, {"x": 3.0, "y": 4.0}]},
        )
        assert status == 413
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

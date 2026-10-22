from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from mini_deepinfer.config import EngineConfig
from mini_deepinfer.llm import LLM
from mini_deepinfer.server import create_server


@pytest.fixture()
def server(model):
    llm = LLM(
        model=model,
        config=EngineConfig(
            num_blocks=64,
            max_num_batched_tokens=128,
            max_num_seqs=4,
            max_model_len=256,
        ),
    )
    app, httpd = create_server(llm, port=0)
    port = httpd.server_address[1]
    yield f"http://127.0.0.1:{port}"
    app.stop()


def get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read().decode("utf-8")


def post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def test_health_and_models(server) -> None:
    assert json.loads(get(server + "/health"))["status"] == "ok"
    models = json.loads(get(server + "/v1/models"))
    assert models["data"][0]["id"] == "mini-deepinfer"


def test_completion(server) -> None:
    result = post(
        server + "/v1/completions",
        {"prompt": "DeepInfer is", "max_tokens": 8, "temperature": 0},
    )
    assert result["object"] == "text_completion"
    assert result["choices"][0]["text"]


def test_completion_reports_length_and_usage(server) -> None:
    result = post(
        server + "/v1/completions",
        {"prompt": "DeepInfer is", "max_tokens": 1, "temperature": 0},
    )
    choice = result["choices"][0]
    usage = result["usage"]
    assert choice["finish_reason"] == "length"
    assert usage["prompt_tokens"] > 0
    assert usage["completion_tokens"] == 1
    assert usage["total_tokens"] == usage["prompt_tokens"] + 1


def test_chat_completion(server) -> None:
    result = post(
        server + "/v1/chat/completions",
        {
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 8,
            "temperature": 0,
        },
    )
    assert result["choices"][0]["message"]["role"] == "assistant"


def test_streaming(server) -> None:
    request = urllib.request.Request(
        server + "/v1/completions",
        data=json.dumps(
            {"prompt": "DeepInfer is", "max_tokens": 5, "temperature": 0, "stream": True}
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8")
    assert "data: [DONE]" in body
    assert "text_completion" in body


def test_metrics(server) -> None:
    body = get(server + "/metrics")
    assert "mini_deepinfer_requests_finished" in body


def test_bad_request(server) -> None:
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        post(server + "/v1/completions", {"prompt": 42})
    assert excinfo.value.code == 400


def test_prompt_too_long_returns_413(server) -> None:
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        post(
            server + "/v1/completions",
            {"prompt": "x" * 1000, "max_tokens": 8, "temperature": 0},
        )
    assert excinfo.value.code == 413


def test_drain_rejects_new_requests(server) -> None:
    assert post(server + "/admin/drain", {})["status"] == "draining"
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        post(
            server + "/v1/completions",
            {"prompt": "DeepInfer is", "max_tokens": 4, "temperature": 0},
        )
    assert excinfo.value.code == 503
    assert post(server + "/admin/undrain", {})["status"] == "ok"


def test_health_reports_draining_state(server) -> None:
    assert json.loads(get(server + "/health"))["status"] == "ok"
    assert json.loads(get(server + "/ready"))["status"] == "ready"
    post(server + "/admin/drain", {})
    health = json.loads(get(server + "/health"))
    assert health["status"] == "draining"
    assert health["draining"] is True
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get(server + "/ready")
    assert excinfo.value.code == 503
    post(server + "/admin/undrain", {})
    assert json.loads(get(server + "/ready"))["status"] == "ready"

"""Test the OpenAI-compatible server end to end (plain + streaming)."""

import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minivllm.api_server import create_app  # noqa: E402
from minivllm.async_engine import AsyncLLMEngine  # noqa: E402
from minivllm.config import CacheConfig, EngineConfig, ModelConfig, SchedulerConfig  # noqa: E402
from minivllm.tokenizer import CharTokenizer  # noqa: E402


def _build_server():
    tok = CharTokenizer()
    cfg = EngineConfig(
        model=ModelConfig(vocab_size=tok.vocab_size, n_embd=32, n_layer=2,
                          n_head=4, head_dim=8, block_size=64),
        cache=CacheConfig(block_size=8, num_gpu_blocks=32),
        scheduler=SchedulerConfig(max_num_seqs=4, max_num_batched_tokens=64),
        seed=0,
    )
    engine = AsyncLLMEngine(cfg, tokenizer=tok)
    return create_app(engine, tok), engine


def test_server_endpoints():
    app, engine = _build_server()
    port = 8765 + (hash(Path(__file__).name) % 500)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)

    base = f"http://127.0.0.1:{port}"
    # trust_env=False ignores any Windows system proxy, which would otherwise
    # intercept localhost requests and return 502.
    client = httpx.Client(trust_env=False, timeout=30)
    try:
        # health
        r = client.get(f"{base}/health")
        assert r.status_code == 200 and r.json()["status"] == "ok"

        # models
        r = client.get(f"{base}/v1/models")
        assert r.status_code == 200
        assert r.json()["data"][0]["id"] == "tinygpt"

        # completions (non-streaming)
        r = client.post(f"{base}/v1/completions",
                        json={"prompt": "the quick", "max_tokens": 12,
                              "temperature": 0})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["choices"][0]["text"]
        assert body["usage"]["completion_tokens"] >= 1

        # chat completions (non-streaming)
        r = client.post(f"{base}/v1/chat/completions",
                        json={"messages": [{"role": "user", "content": "hello"}],
                              "max_tokens": 12, "temperature": 0})
        assert r.status_code == 200, r.text
        assert r.json()["choices"][0]["message"]["content"]

        # streaming completions
        with client.stream("POST", f"{base}/v1/completions",
                           json={"prompt": "the quick", "max_tokens": 12,
                                 "temperature": 0, "stream": True}) as resp:
            assert resp.status_code == 200
            chunks = [line for line in resp.iter_lines() if line.startswith("data: ")]
        assert chunks, "no SSE chunks received"
        assert chunks[-1].endswith("[DONE]")

        # streaming chat
        with client.stream("POST", f"{base}/v1/chat/completions",
                           json={"messages": [{"role": "user", "content": "hi"}],
                                 "max_tokens": 12, "temperature": 0,
                                 "stream": True}) as resp:
            assert resp.status_code == 200
            chunks = [line for line in resp.iter_lines() if line.startswith("data: ")]
        assert chunks and chunks[-1].endswith("[DONE]")
    finally:
        client.close()
        server.should_exit = True
        thread.join(timeout=5)
        engine.stop()


if __name__ == "__main__":
    test_server_endpoints()
    print("test_server: OK")

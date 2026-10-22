"""Start the OpenAI-compatible server, send one request, then shut it down."""

from __future__ import annotations

import json
import urllib.request

from _common import add_project_root

add_project_root()

from mini_deepinfer import EngineConfig, LLM
from mini_deepinfer.server import create_server


def main() -> None:
    llm = LLM(
        config=EngineConfig(
            num_blocks=128,
            max_num_batched_tokens=256,
            max_num_seqs=4,
            max_model_len=256,
        )
    )
    app, server = create_server(llm, port=0)
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    print("serving on", base)
    request = urllib.request.Request(
        base + "/v1/completions",
        data=json.dumps(
            {"prompt": "DeepInfer is", "max_tokens": 12, "temperature": 0}
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        print(json.dumps(json.loads(response.read().decode("utf-8")), indent=2, ensure_ascii=False))
    with urllib.request.urlopen(base + "/metrics", timeout=10) as response:
        print(response.read().decode("utf-8").splitlines()[:4])
    app.stop()


if __name__ == "__main__":
    main()

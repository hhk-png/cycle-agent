"""OpenAI-compatible HTTP server (FastAPI).

Implements the subset of the OpenAI API that vLLM serves:

* ``GET  /health``
* ``GET  /v1/models``
* ``POST /v1/completions``        (plain + ``stream=True``)
* ``POST /v1/chat/completions``   (plain + ``stream=True``)

All generation is funneled through :class:`AsyncLLMEngine`, so many clients
can connect at once while the engine step loop stays single-threaded (exactly
the architecture of the real ``vllm serve`` command).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .async_engine import AsyncLLMEngine
from .config import EngineConfig, SamplingParams
from .model import TinyGPT
from .sequence import RequestOutput
from .tokenizer import CharTokenizer

MODEL_NAME = "tinygpt"


# ---------------------------------------------------------------------- #
# helpers
# ---------------------------------------------------------------------- #
def _sampling_params_from_body(body: dict) -> SamplingParams:
    p = SamplingParams()
    p.temperature = float(body.get("temperature", 1.0))
    p.top_p = float(body.get("top_p", 1.0))
    p.top_k = int(body.get("top_k", -1))
    p.max_tokens = int(body.get("max_tokens", 64))
    p.echo = bool(body.get("echo", False))
    p.seed = body.get("seed")
    stop = body.get("stop")
    if isinstance(stop, str):
        stop = [stop]
    p.stop = stop if isinstance(stop, list) else None
    return p


def _chat_to_prompt(messages: list) -> str:
    """Very small chat template (a real engine uses a Jinja template)."""
    parts = []
    for m in messages or []:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):  # multimodal-style content parts
            content = "".join(c.get("text", "") for c in content
                              if isinstance(c, dict) and c.get("type") == "text")
        if role == "system":
            parts.append(f"system: {content}")
        elif role == "assistant":
            parts.append(f"assistant: {content}")
        else:
            parts.append(f"user: {content}")
    parts.append("assistant:")
    return "\n".join(parts)


def _usage(prompt_len: int, completion_len: int) -> dict:
    return {
        "prompt_tokens": prompt_len,
        "completion_tokens": completion_len,
        "total_tokens": prompt_len + completion_len,
    }


def _delta(prev: str, cur: str) -> str:
    if prev and cur.startswith(prev):
        return cur[len(prev):]
    return cur


# ---------------------------------------------------------------------- #
# server factory
# ---------------------------------------------------------------------- #
def create_app(engine: AsyncLLMEngine, tokenizer: CharTokenizer) -> FastAPI:
    app = FastAPI(title="mini-vLLM", version="0.1.0")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/v1/models")
    def list_models() -> dict:
        return {
            "object": "list",
            "data": [{
                "id": MODEL_NAME,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "mini-vllm",
            }],
        }

    # ------------------------- /v1/completions ------------------------- #
    @app.post("/v1/completions")
    async def completions(body: dict, request: Request):
        prompt = body.get("prompt", "")
        if isinstance(prompt, list):
            # batch prompts are not implemented in the mini version
            return JSONResponse({"error": "batch prompts not supported"}, status_code=400)
        params = _sampling_params_from_body(body)
        stream = bool(body.get("stream", False))
        if stream:
            return StreamingResponse(
                _completion_stream(engine, prompt, params),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        output = await engine.complete(prompt, params)
        return _completion_response(output, params)

    async def _completion_stream(engine, prompt, params) -> AsyncGenerator[str, None]:
        prev_text = ""
        seq_id = None
        async for o in engine.stream(prompt, params):
            seq_id = o.request_id
            out = o.outputs[0]
            delta = _delta(prev_text, out.text)
            prev_text = out.text
            chunk = {
                "id": f"cmpl-{seq_id}",
                "object": "text_completion",
                "created": int(time.time()),
                "model": MODEL_NAME,
                "choices": [{
                    "text": delta,
                    "index": 0,
                    "logprobs": None,
                    "finish_reason": out.finish_reason,
                }],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        if seq_id is None:
            seq_id = 0
        yield f"data: [DONE]\n\n"

    def _completion_response(output: RequestOutput, params: SamplingParams):
        out = output.outputs[0]
        text = output.prompt + out.text if params.echo else out.text
        return {
            "id": f"cmpl-{output.request_id}",
            "object": "text_completion",
            "created": int(time.time()),
            "model": MODEL_NAME,
            "choices": [{
                "text": text,
                "index": 0,
                "logprobs": None,
                "finish_reason": out.finish_reason,
            }],
            "usage": _usage(len(output.prompt_token_ids), len(out.token_ids)),
        }

    # -------------------- /v1/chat/completions ------------------------- #
    @app.post("/v1/chat/completions")
    async def chat_completions(body: dict):
        messages = body.get("messages", [])
        prompt = _chat_to_prompt(messages)
        params = _sampling_params_from_body(body)
        stream = bool(body.get("stream", False))
        if stream:
            return StreamingResponse(
                _chat_stream(engine, prompt, params),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        output = await engine.complete(prompt, params)
        return _chat_response(output)

    async def _chat_stream(engine, prompt, params) -> AsyncGenerator[str, None]:
        prev_text = ""
        seq_id = None
        async for o in engine.stream(prompt, params):
            seq_id = o.request_id
            out = o.outputs[0]
            delta = _delta(prev_text, out.text)
            prev_text = out.text
            chunk = {
                "id": f"chatcmpl-{seq_id}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": MODEL_NAME,
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": delta},
                    "finish_reason": out.finish_reason,
                }],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    def _chat_response(output: RequestOutput):
        out = output.outputs[0]
        return {
            "id": f"chatcmpl-{output.request_id}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": MODEL_NAME,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": out.text},
                "finish_reason": out.finish_reason,
            }],
            "usage": _usage(len(output.prompt_token_ids), len(out.token_ids)),
        }

    return app

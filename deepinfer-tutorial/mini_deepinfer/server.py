"""A minimal OpenAI-compatible HTTP server.

DeepInfer's open-source release is offline-only; this module is a teaching
extension that shows how the engine would sit behind a serving API.  It uses
only the standard library and is intentionally single-process.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .errors import DrainingError, OutOfBlocksError, PromptTooLongError
from .llm import LLM
from .recipe import normalize_messages
from .sampling_params import SamplingParams


def _finish_reason(reason: str | None) -> str:
    """Map engine stop reasons onto the OpenAI ``finish_reason`` vocabulary."""
    if reason == "length":
        return "length"
    if reason == "structured_output":
        return "content_filter"
    return "stop"


def _sse(payload: dict | str) -> bytes:
    if isinstance(payload, str):
        return f"data: {payload}\n\n".encode("utf-8")
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    server_version = "mini-deepinfer/0.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        if getattr(self.server, "verbose", False):
            super().log_message(format, *args)

    @property
    def app(self) -> "EngineServer":
        return self.server.app  # type: ignore[attr-defined]

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            raise ValueError("empty request body")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _discard_body(self) -> None:
        """Consume an optional body so keep-alive cannot parse it as a request."""
        length = int(self.headers.get("Content-Length", "0"))
        if length > 0:
            self.rfile.read(length)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/health", "/healthz"):
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "draining" if self.app.draining else "ok",
                    "draining": self.app.draining,
                    "active_requests": self.app.active_requests,
                },
            )
        elif self.path == "/ready":
            if self.app.draining:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE, {"status": "draining"}
                )
            else:
                self._send_json(HTTPStatus.OK, {"status": "ready"})
        elif self.path == "/metrics":
            body = self.app.llm.engine.metrics.prometheus().encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/v1/models":
            self._send_json(
                HTTPStatus.OK,
                {
                    "object": "list",
                    "data": [{"id": self.app.model_name, "object": "model"}],
                },
            )
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        try:
            if self.path == "/admin/drain":
                self._discard_body()
                self.app.start_drain()
                self._send_json(HTTPStatus.OK, {"status": "draining"})
            elif self.path == "/admin/undrain":
                self._discard_body()
                self.app.stop_drain()
                self._send_json(HTTPStatus.OK, {"status": "ok"})
            elif self.path == "/v1/completions":
                self._handle_completion()
            elif self.path == "/v1/chat/completions":
                self._handle_chat()
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}})
        except DrainingError as exc:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": {"message": str(exc), "type": "draining"}},
            )
        except PromptTooLongError as exc:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": {"message": str(exc), "type": "prompt_too_long"}},
            )
        except OutOfBlocksError as exc:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": {"message": str(exc), "type": "out_of_blocks"}},
            )
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": {"message": str(exc)}})
        except Exception as exc:  # pragma: no cover - defensive server boundary
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": {"message": f"{type(exc).__name__}: {exc}"}},
            )

    # ------------------------------------------------------------------
    # Request parsing
    # ------------------------------------------------------------------
    def _params(self, body: dict) -> SamplingParams:
        stop = body.get("stop")
        if isinstance(stop, str):
            stop = (stop,)
        return SamplingParams(
            temperature=float(body.get("temperature", 1.0)),
            top_p=float(body.get("top_p", 1.0)),
            top_k=int(body.get("top_k", -1)),
            max_tokens=int(body.get("max_tokens", 32)),
            ignore_eos=bool(body.get("ignore_eos", False)),
            seed=body.get("seed"),
            stop=tuple(stop) if stop else (),
        )

    def _check_context_limit(self, prompt_ids: list[int], params: SamplingParams) -> None:
        config = self.app.llm.engine.config
        required = len(prompt_ids) + params.max_tokens
        if required > config.max_model_len:
            raise PromptTooLongError(
                f"prompt ({len(prompt_ids)} tokens) + max_tokens "
                f"({params.max_tokens}) exceeds max_model_len "
                f"({config.max_model_len})"
            )

    def _handle_completion(self) -> None:
        body = self._read_json()
        prompt = body.get("prompt")
        if isinstance(prompt, list):
            prompt = prompt[0] if prompt else ""
        if not isinstance(prompt, str):
            raise ValueError("prompt must be a string")
        params = self._params(body)
        prompt_ids = self.app.llm.engine.tokenizer.encode(prompt)
        self._check_context_limit(prompt_ids, params)
        request_id = f"cmpl-{uuid.uuid4().hex[:12]}"
        if body.get("stream"):
            self._stream(prompt, params, request_id, chat=False)
            return
        output = self.app.generate(prompt, params, return_objects=True)[0]
        self._send_json(
            HTTPStatus.OK,
            {
                "id": request_id,
                "object": "text_completion",
                "created": int(time.time()),
                "model": self.app.model_name,
                "choices": [
                    {
                        "text": output.text,
                        "index": 0,
                        "logprobs": None,
                        "finish_reason": _finish_reason(output.finish_reason),
                    }
                ],
                "usage": {
                    "prompt_tokens": output.prompt_tokens,
                    "completion_tokens": output.output_tokens,
                    "total_tokens": output.prompt_tokens + output.output_tokens,
                },
            },
        )

    def _handle_chat(self) -> None:
        body = self._read_json()
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages must be a non-empty list")
        messages = normalize_messages(messages, body.get("protocol", "openai"))
        params = self._params(body)
        prompt_ids = self.app.llm.engine.tokenizer.apply_chat_template(messages)
        self._check_context_limit(prompt_ids, params)
        request_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        if body.get("stream"):
            self._stream(messages, params, request_id, chat=True)
            return
        output = self.app.generate(messages, params, chat=True, return_objects=True)[0]
        self._send_json(
            HTTPStatus.OK,
            {
                "id": request_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": self.app.model_name,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": output.text},
                        "finish_reason": _finish_reason(output.finish_reason),
                    }
                ],
                "usage": {
                    "prompt_tokens": output.prompt_tokens,
                    "completion_tokens": output.output_tokens,
                    "total_tokens": output.prompt_tokens + output.output_tokens,
                },
            },
        )

    def _stream(self, payload, params: SamplingParams, request_id: str, *, chat: bool) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        events: queue.Queue = queue.Queue()
        result_box: dict[str, object] = {}

        def callback(_request_id: str, delta: str, finished: bool) -> None:
            events.put((delta, finished))

        def worker() -> None:
            try:
                result_box["output"] = self.app.generate(
                    payload, params, chat=chat, stream=callback, return_objects=True
                )[0]
            finally:
                events.put(None)

        def write_chunk(delta: str, reason: str | None) -> None:
            if chat:
                chunk = {
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": self.app.model_name,
                    "choices": [
                        {"index": 0, "delta": {"content": delta}, "finish_reason": reason}
                    ],
                }
            else:
                chunk = {
                    "id": request_id,
                    "object": "text_completion",
                    "created": int(time.time()),
                    "model": self.app.model_name,
                    "choices": [{"index": 0, "text": delta, "finish_reason": reason}],
                }
            self.wfile.write(_sse(chunk))
            self.wfile.flush()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        first = True
        final_delta = ""
        while True:
            item = events.get()
            if item is None:
                break
            delta, finished = item
            if finished:
                # Hold the last delta until the worker returns the final
                # RequestOutput, so the terminal chunk carries the real
                # finish_reason instead of a hard-coded "stop".
                final_delta = delta
                continue
            if chat and first:
                self.wfile.write(_sse({
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": self.app.model_name,
                    "choices": [
                        {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
                    ],
                }))
                self.wfile.flush()
                first = False
            write_chunk(delta, None)
        if chat and first:
            self.wfile.write(_sse({
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": self.app.model_name,
                "choices": [
                    {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
                ],
            }))
            self.wfile.flush()
        output = result_box.get("output")
        reason = _finish_reason(getattr(output, "finish_reason", None))
        write_chunk(final_delta, reason)
        self.wfile.write(_sse("[DONE]"))
        self.wfile.flush()
        thread.join(timeout=1.0)
        self.close_connection = True


class EngineServer:
    """Thin adapter exposing an :class:`LLM` through HTTP."""

    def __init__(self, llm: LLM, model_name: str = "mini-deepinfer") -> None:
        self.llm = llm
        self.model_name = model_name
        self._lock = threading.Lock()
        self._idle = threading.Condition()
        self._draining = False
        self._active = 0
        self.httpd: ThreadingHTTPServer | None = None

    @property
    def draining(self) -> bool:
        with self._idle:
            return self._draining

    @property
    def active_requests(self) -> int:
        with self._idle:
            return self._active

    def start_drain(self) -> None:
        """Stop admitting new requests; in-flight requests may finish."""
        with self._idle:
            self._draining = True

    def stop_drain(self) -> None:
        with self._idle:
            self._draining = False

    def generate(
        self,
        payload,
        params: SamplingParams,
        *,
        chat: bool = False,
        stream=None,
        return_objects: bool = False,
    ):
        # Admission and the active counter share the same condition variable,
        # so a drain either sees the request or rejects it, never both.
        with self._idle:
            if self._draining:
                raise DrainingError("server is draining; retry against another instance")
            self._active += 1
        try:
            # One generation at a time keeps the teaching engine's state simple.
            with self._lock:
                if chat:
                    return self.llm.generate_chat(  # type: ignore[arg-type]
                        payload, params, stream=stream, return_objects=return_objects
                    )
                return self.llm.generate(
                    payload, params, stream=stream, return_objects=return_objects
                )
        finally:
            with self._idle:
                self._active -= 1
                if self._active == 0:
                    self._idle.notify_all()

    def start(self, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer((host, port), _Handler)
        server.app = self  # type: ignore[attr-defined]
        server.verbose = False  # type: ignore[attr-defined]
        self.httpd = server
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server

    def stop(self, grace_s: float = 10.0) -> None:
        """Drain, wait for in-flight requests, then close the listener."""
        self.start_drain()
        with self._idle:
            self._idle.wait_for(lambda: self._active == 0, timeout=max(0.0, grace_s))
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


def create_server(
    llm: LLM | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    model_name: str = "mini-deepinfer",
) -> tuple[EngineServer, ThreadingHTTPServer]:
    app = EngineServer(llm or LLM(), model_name)
    server = app.start(host, port)
    return app, server

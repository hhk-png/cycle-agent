"""Async engine: the mini analogue of ``vllm/engine/async_llm_engine.py``.

vLLM decouples the (single) engine step loop from the web framework by running
the engine inside a dedicated worker process/thread.  Here we run the
synchronous :class:`LLMEngine` in a background thread and expose async
iterators, which is enough for the FastAPI server to serve many concurrent
requests without blocking.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from typing import AsyncIterator, Optional

from .config import EngineConfig, SamplingParams
from .engine import LLMEngine
from .model import TinyGPT
from .tokenizer import CharTokenizer


class AsyncLLMEngine:
    def __init__(self, config: EngineConfig,
                 tokenizer: Optional[CharTokenizer] = None,
                 model: Optional[TinyGPT] = None):
        self._engine = LLMEngine(config, tokenizer=tokenizer, model=model)
        self._lock = threading.Lock()
        self._streams: dict = {}
        self._stop = False
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name="minivllm-engine")
        self._thread.start()

    # ------------------------------------------------------------------ #
    def _run_loop(self) -> None:
        """Continuously schedule and execute engine steps."""
        while not self._stop:
            pending = False
            with self._lock:
                if self._engine.has_pending():
                    pending = True
                    outputs = self._engine.step()
                    for o in outputs:
                        q = self._streams.get(o.request_id)
                        if q is not None:
                            q.put(o)
            if not pending:
                time.sleep(0.001)

    def stop(self) -> None:
        self._stop = True

    # ------------------------------------------------------------------ #
    async def stream(self, prompt: str,
                     sampling_params: Optional[SamplingParams] = None
                     ) -> AsyncIterator:
        """Async generator yielding one ``RequestOutput`` per step."""
        q: "queue.Queue" = queue.Queue()
        with self._lock:
            seq_id = self._engine.add_request(prompt, sampling_params)
            self._streams[seq_id] = q
        try:
            while True:
                o = await asyncio.to_thread(q.get)
                yield o
                if o.finished:
                    break
        finally:
            self._streams.pop(seq_id, None)

    async def complete(self, prompt: str,
                       sampling_params: Optional[SamplingParams] = None):
        """Collect the whole stream and return the final ``RequestOutput``."""
        last = None
        async for o in self.stream(prompt, sampling_params):
            last = o
        return last

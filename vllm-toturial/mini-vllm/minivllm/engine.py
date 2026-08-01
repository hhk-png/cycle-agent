"""The LLMEngine: the heart of the mini-vLLM.

It is the direct analogue of ``vllm/engine/llm_engine.py`` and owns:

* the model and its weights;
* the paged KV cache (``BlockManager``);
* the continuous-batching ``Scheduler``;
* the ``Sampler``;

and exposes ``add_request`` / ``step`` / ``generate`` / ``generate_stream``.
The OpenAI-compatible server and the CLI are thin wrappers around this class.
"""

from __future__ import annotations

import itertools
import time
from typing import Iterator, List, Optional

import numpy as np

from .config import EngineConfig, SamplingParams
from .kv_cache import BlockManager
from .model import TinyGPT
from .scheduler import Scheduler, ScheduledStep
from .sampler import sample_token
from .sequence import FINISHED, RUNNING, Sequence, RequestOutput


class LLMEngine:
    def __init__(self, config: EngineConfig, tokenizer=None, model: Optional[TinyGPT] = None):
        self.config = config
        self.tokenizer = tokenizer
        self.model = model if model is not None else TinyGPT(config.model, seed=config.seed)
        self.block_manager = BlockManager(config.cache, config.model,
                                          enable_prefix_caching=config.enable_prefix_caching)
        self.scheduler = Scheduler(config.scheduler, self.block_manager)
        self.rng = np.random.default_rng(config.seed)
        self._counter = itertools.count()
        self.seqs: dict = {}

    # ------------------------------------------------------------------ #
    # request handling
    # ------------------------------------------------------------------ #
    def add_request(self, prompt: str,
                    sampling_params: Optional[SamplingParams] = None,
                    request_id: Optional[int] = None) -> int:
        prompt_ids = self.tokenizer.encode(prompt)
        if not prompt_ids:
            raise ValueError("prompt encoded to an empty token sequence")
        max_model_len = self.config.max_model_len or self.config.model.block_size
        if len(prompt_ids) > max_model_len - 1:
            prompt_ids = prompt_ids[: max_model_len - 1]  # keep room for 1 output
        seq_id = next(self._counter) if request_id is None else request_id
        seq = Sequence(
            seq_id=seq_id,
            prompt_ids=prompt_ids,
            sampling_params=sampling_params or SamplingParams(),
            arrival_time=time.time(),
            priority=next(self._counter),
        )
        self.seqs[seq_id] = seq
        self.scheduler.add_sequence(seq)
        return seq_id

    def has_pending(self) -> bool:
        return self.scheduler.has_pending()

    def get_num_unfinished(self) -> int:
        return len([s for s in self.seqs.values() if not s.is_finished])

    # ------------------------------------------------------------------ #
    # one engine step
    # ------------------------------------------------------------------ #
    def step(self) -> List[RequestOutput]:
        scheduled = self.scheduler.schedule()
        if not scheduled.prefill_items and not scheduled.decode_items:
            return []
        return self._execute(scheduled)

    def _execute(self, scheduled: ScheduledStep) -> List[RequestOutput]:
        touched: List[Sequence] = []

        # -------- prefill items --------
        for seq, chunk in scheduled.prefill_items:
            if seq.state != RUNNING:
                # the sequence was preempted while this step was being
                # scheduled; it will be re-scheduled from the waiting queue
                continue
            start = seq.cached_len
            ids = seq.all_ids
            tokens = ids[start:start + chunk]
            positions = list(range(start, start + chunk))
            self._run_forward(seq, tokens, positions)
            seq.cached_len += chunk

            if seq.cached_len == seq.num_tokens:
                # prompt (+any re-prefilled outputs) fully cached: emit the
                # first output token and switch to decode.
                seq.phase = "DECODE"
                # The last token of the context is re-stored on the first
                # decode (see scheduler docs); this keeps one uniform path.
                seq.cached_len -= 1
                self._sample_and_apply(seq, seq.cached_len)
            touched.append(seq)

        # -------- decode items --------
        for seq in scheduled.decode_items:
            if seq.state != RUNNING:
                continue
            self.block_manager.ensure_blocks(seq, seq.cached_len + 1)
            self._sample_and_apply(seq, seq.cached_len)
            touched.append(seq)

        outputs = [RequestOutput.from_sequence(s, self.tokenizer) for s in touched]
        return outputs

    def _run_forward(self, seq: Sequence, tokens: List[int], positions: List[int]) -> None:
        x = np.asarray(tokens, dtype=np.int64).reshape(1, -1)
        pos = np.asarray(positions, dtype=np.int64).reshape(1, -1)
        self.model.forward(x, pos, [seq], self.block_manager)

    def _sample_and_apply(self, seq: Sequence, input_position: int) -> None:
        """Run the model on the token at ``input_position`` and sample the next.

        For prefill-completion and decode the input token is always
        ``all_ids[input_position]`` and its KV is stored inside ``forward``.
        """
        token_id = seq.all_ids[input_position]
        x = np.asarray([token_id], dtype=np.int64).reshape(1, 1)
        pos = np.asarray([input_position], dtype=np.int64).reshape(1, 1)
        logits = self.model.forward(x, pos, [seq], self.block_manager)[0, -1, :]
        seq.cached_len += 1

        params = seq.sampling_params
        rng = np.random.default_rng(params.seed) if params.seed is not None else self.rng
        token, logprob = sample_token(logits[None, :], params, rng)
        self._apply_token(seq, int(token[0]), float(logprob[0]))

    def _apply_token(self, seq: Sequence, token_id: int, logprob: float) -> None:
        seq.append_token(token_id)
        seq.cumulative_logprob += logprob

        # stop conditions (order mirrors vLLM's ``_check_stop``)
        max_model_len = self.config.max_model_len or self.config.model.block_size
        if not seq.sampling_params.ignore_eos and token_id == self.tokenizer.eos_token_id:
            seq.stop_reason = "stop"
        elif seq.num_tokens >= max_model_len:
            seq.stop_reason = "length"
        elif len(seq.output_ids) >= seq.sampling_params.max_tokens:
            seq.stop_reason = "length"
        else:
            text = self.tokenizer.decode(seq.output_ids)
            for s in seq.sampling_params.stop or []:
                if s and text.endswith(s):
                    seq.stop_reason = "stop"
                    break

        if seq.stop_reason is not None:
            self._finish(seq)

    def _finish(self, seq: Sequence) -> None:
        # cache the prompt prefix before its blocks are released
        if self.config.enable_prefix_caching and not seq.is_preempted:
            self.block_manager.register_prefix(seq)
        self.block_manager.release(seq)
        seq.state = FINISHED
        if seq in self.scheduler.running:
            self.scheduler.running.remove(seq)

    # ------------------------------------------------------------------ #
    # user-facing generation helpers
    # ------------------------------------------------------------------ #
    def generate(self, prompt: str,
                 sampling_params: Optional[SamplingParams] = None) -> RequestOutput:
        """Run a single request to completion (the analogue of ``LLM.generate``)."""
        seq_id = self.add_request(prompt, sampling_params)
        seq = self.seqs[seq_id]
        while not seq.is_finished:
            self.step()
        return RequestOutput.from_sequence(seq, self.tokenizer)

    def generate_stream(self, prompt: str,
                        sampling_params: Optional[SamplingParams] = None) -> Iterator[RequestOutput]:
        """Stream a single request token-by-token (analogue of ``AsyncLLMEngine``)."""
        seq_id = self.add_request(prompt, sampling_params)
        seq = self.seqs[seq_id]
        while True:
            outputs = self.step()
            for o in outputs:
                if o.request_id == seq_id:
                    yield o
            if seq.is_finished:
                break

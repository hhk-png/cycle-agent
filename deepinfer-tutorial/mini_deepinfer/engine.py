"""The iteration-level engine: schedule, execute, sample, update."""

from __future__ import annotations

import copy
import time
from itertools import count

import numpy as np

from .block_manager import BlockManager
from .cache_engine import PagedKVCache
from .config import EngineConfig
from .errors import EngineClosedError, OutOfBlocksError
from .metrics import EngineMetrics
from .model.tiny_transformer import TinyTransformer
from .sampling import sample_token
from .sampling_params import SamplingParams
from .scheduler import ScheduledSeq, Scheduler, SchedulerOutput, StepResult
from .sequence import RequestOutput, Sequence, SequenceStatus
from .speculative import (
    SpeculativeStats,
    choose_verify_length,
    generate_drafts,
    verify_drafts,
)
from .structured_output import StructuredOutputMatcher, build_matcher
from .tokenizer import Tokenizer


class LLMEngine:
    def __init__(
        self,
        config: EngineConfig | None = None,
        *,
        model: TinyTransformer | None = None,
        tokenizer: Tokenizer | None = None,
        draft_model: TinyTransformer | None = None,
    ) -> None:
        self.config = config or EngineConfig()
        self.tokenizer = tokenizer or Tokenizer()
        self.model = model or TinyTransformer.from_file(self.config.resolve_model_path())
        if self.model.config.vocab_size != self.tokenizer.vocab_size:
            raise ValueError("model and tokenizer vocabularies do not match")
        if self.config.max_model_len > self.model.config.max_seq_len:
            # Silently shrinking the configured limit hides a deployment
            # error; fail at startup and let the operator choose a valid value.
            raise ValueError(
                f"max_model_len {self.config.max_model_len} exceeds model "
                f"max_seq_len {self.model.config.max_seq_len}"
            )
        self.draft_model = draft_model
        if self.config.enable_speculative and self.draft_model is None:
            raise ValueError("speculative decoding requires a draft model")
        self.block_manager = BlockManager(
            self.config.num_blocks,
            self.config.block_size,
            enable_prefix_cache=self.config.enable_prefix_cache,
        )
        self.kv_cache = PagedKVCache(
            self.model.config.n_layers,
            self.block_manager,
            self.model.config.n_kv_heads,
            self.model.config.head_dim,
        )
        self.scheduler = Scheduler(self.config, self.block_manager, self.kv_cache)
        self.metrics = EngineMetrics()
        self.spec_stats = SpeculativeStats()
        self._request_counter = count()
        self._closed = False
        self._pending_outputs: list[RequestOutput] = []
        self._last_preemptions = 0
        # Requests whose terminal output has not been returned yet.  A
        # preempted/aborted sequence never appears in SchedulerOutput, so
        # without this registry its final output would be lost.
        self._unreported: dict[int, Sequence] = {}

    # ------------------------------------------------------------------
    # Request lifecycle
    # ------------------------------------------------------------------
    def add_request(
        self,
        prompt: str | list[int] | None = None,
        sampling_params: SamplingParams | None = None,
        *,
        request_id: str | None = None,
        prompt_token_ids: list[int] | None = None,
    ) -> Sequence:
        if self._closed:
            raise EngineClosedError("engine is closed")
        if prompt is None and prompt_token_ids is None:
            raise ValueError("prompt or prompt_token_ids is required")
        if prompt_token_ids is None:
            if isinstance(prompt, (list, tuple)):
                # The signature promises str | list[int]; encode() would
                # stringify the list and tokenize its repr, which silently
                # produces a much longer prompt.
                prompt_token_ids = [int(token_id) for token_id in prompt]
            else:
                prompt_token_ids = self.tokenizer.encode(str(prompt))
        params = (sampling_params or SamplingParams()).copy()
        if params.eos_token_id is None:
            params.eos_token_id = self.tokenizer.eos_id
        seq_id = next(self._request_counter)
        if request_id is None:
            request_id = str(seq_id)
        seq = Sequence(
            seq_id=seq_id,
            request_id=request_id,
            prompt_token_ids=prompt_token_ids,
            sampling_params=params,
        )
        if params.so_args:
            seq.so_matcher = build_matcher(
                params.so_args,
                eos_token_id=self.tokenizer.eos_id,
                vocab_size=self.tokenizer.vocab_size,
                trigger_token_ids=params.so_trigger_token_ids,
                bound_token_id=params.so_bound_token_id,
            )
        self.metrics.record_request(seq.num_prompt_tokens)
        self.scheduler.add_sequence(seq)
        if seq.is_finished():
            # Rejected at admission (too long, unschedulable): report it once
            # and record it as finished so metrics stay consistent.
            seq.metrics.mark_finished()
            self.metrics.record_finished(seq)
            self._pending_outputs.append(self._make_output(seq, []))
        else:
            self._unreported[seq.seq_id] = seq
        return seq

    def abort_request(self, request_id: str) -> int:
        return self.scheduler.abort(request_id)

    def has_unfinished_requests(self) -> bool:
        return self.scheduler.has_unfinished()

    def get_num_unfinished_requests(self) -> int:
        return self.scheduler.num_unfinished()

    def close(self) -> None:
        self._closed = True

    # ------------------------------------------------------------------
    # Step
    # ------------------------------------------------------------------
    def step(self) -> list[RequestOutput]:
        if self._closed:
            raise EngineClosedError("engine is closed")
        started = time.perf_counter()
        scheduled = self.scheduler.schedule()
        attempts = 0
        while scheduled.empty and self.scheduler.has_unfinished() and attempts < 4:
            # A preemption can change the queue state; retry so the freed
            # blocks are usable in this same engine step.
            scheduled = self.scheduler.schedule()
            attempts += 1
        if scheduled.empty:
            outputs = self._collect_unreported()
            if self._pending_outputs:
                outputs.extend(self._pending_outputs)
                self._pending_outputs = []
            if outputs:
                return outputs
            if self.scheduler.has_unfinished():
                raise OutOfBlocksError(
                    "scheduler could not make progress; increase num_blocks or reduce the batch"
                )
            return []

        results: dict[int, StepResult] = {}
        for item in scheduled.scheduled:
            results[item.seq.seq_id] = self._execute(item)

        self.scheduler.postprocess(scheduled, results)
        self.metrics.record_step(
            prefill_tokens=sum(item.q_len for item in scheduled.scheduled if item.is_prompt),
            decode_tokens=sum(item.q_len for item in scheduled.scheduled if not item.is_prompt),
            elapsed=time.perf_counter() - started,
        )
        if self.scheduler.num_preempted > self._last_preemptions:
            self.metrics.preemptions += self.scheduler.num_preempted - self._last_preemptions
            self._last_preemptions = self.scheduler.num_preempted

        outputs: list[RequestOutput] = []
        for item in scheduled.scheduled:
            result = results[item.seq.seq_id]
            if result.tokens or item.seq.is_finished():
                outputs.append(self._make_output(item.seq, result.tokens))
            if item.seq.is_finished():
                if item.seq.metrics.finish_time is None:
                    # Structured-output failure can finish without appending
                    # a token, so _append_tokens never recorded the finish.
                    item.seq.metrics.mark_finished()
                    self.metrics.record_finished(item.seq)
                self._unreported.pop(item.seq.seq_id, None)
        # Preempted (stop_preempted_request=True), aborted, and otherwise
        # unscheduled sequences finish outside SchedulerOutput.  Emit their
        # terminal output exactly once so callers never hang waiting for it.
        outputs.extend(self._collect_unreported())
        if self._pending_outputs:
            outputs.extend(self._pending_outputs)
            self._pending_outputs = []
        if self.config.log_interval_steps and self.metrics.steps % self.config.log_interval_steps == 0:
            summary = self.metrics.summary()
            print(
                f"[step {self.metrics.steps}] out_tok/s={summary['output_tokens_per_s']:.1f} "
                f"ttft={summary['ttft_mean_ms']:.1f}ms tpot={summary['tpot_mean_ms']:.1f}ms "
                f"running={len(self.scheduler.running)} waiting={len(self.scheduler.waiting)}"
            )
        return outputs

    def _execute(self, item: ScheduledSeq) -> StepResult:
        seq = item.seq
        if item.is_prompt:
            logits, _, _ = self.model.forward_paged(seq, item.start, item.end, self.kv_cache)
            result = StepResult(seq=seq, computed_end=item.end)
            if item.chunk_complete:
                sampled = self._sample(seq, logits[-1])
                if sampled is not None:
                    token, logprob, top = sampled
                    if self._append_tokens(seq, [token], [logprob], [top]):
                        result.tokens = [token]
                        result.logprobs = [logprob]
                        result.top_logprobs = [top]
            return result

        if self.config.enable_speculative and not (
            seq.so_matcher is not None and not seq.sampling_params.greedy
        ):
            # Non-greedy structured sampling needs the grammar mask applied to
            # the draft distribution too; the draft path does not do that yet.
            # Fall back to ordinary decode rather than biasing the accepted
            # distribution.  Greedy + grammar is safe (one-hot).
            return self._execute_speculative(seq)
        logits, _, _ = self.model.forward_paged(
            seq, seq.length - 1, seq.length, self.kv_cache
        )
        sampled = self._sample(seq, logits[0])
        result = StepResult(seq=seq, computed_end=seq.length)
        if sampled is not None:
            token, logprob, top = sampled
            if self._append_tokens(seq, [token], [logprob], [top]):
                result.tokens = [token]
                result.logprobs = [logprob]
                result.top_logprobs = [top]
        return result

    def _execute_speculative(self, seq: Sequence) -> StepResult:
        assert self.draft_model is not None
        old_length = seq.length
        remaining = max(0, self.config.max_model_len - old_length)
        if remaining == 0:
            seq.status = SequenceStatus.FINISHED_LENGTH_CAPPED
            seq.finish_reason = "length"
            return StepResult(seq=seq, computed_end=seq.length)
        proposal = generate_drafts(
            self.draft_model,
            seq,
            min(self.config.speculative_tokens, remaining),
            seq.sampling_params,
        )
        verify_len = choose_verify_length(
            proposal.confidences, threshold=self.config.confidence_threshold
        )
        verify_len = min(verify_len, remaining)
        self.spec_stats.steps += 1
        self.spec_stats.proposed += len(proposal.tokens)
        self.metrics.speculative_steps += 1
        if verify_len <= 0:
            logits, _, _ = self.model.forward_paged(
                seq, seq.length - 1, seq.length, self.kv_cache
            )
            sampled = self._sample(seq, logits[0])
            result = StepResult(seq=seq, computed_end=seq.length)
            if sampled is not None:
                token, logprob, top = sampled
                if self._append_tokens(seq, [token], [logprob], [top]):
                    result.tokens = [token]
                    result.logprobs = [logprob]
                    result.top_logprobs = [top]
            return result

        # Verification needs to advance a grammar state to mask each position,
        # but it must not advance the sequence's real matcher: a stop token can
        # truncate the committed output before every verified position is used.
        # Verify against a copy, then replay the committed tokens into the real
        # matcher in _append_tokens.
        matcher_copy = copy.deepcopy(seq.so_matcher) if seq.so_matcher is not None else None
        try:
            verification = verify_drafts(
                self.model,
                seq,
                proposal,
                verify_len,
                self.kv_cache,
                seq.sampling_params,
                matcher=matcher_copy,
            )
        except ValueError:
            # The grammar could not be satisfied even though the sampler had a
            # legal mask: treat it as a request-level structured-output failure
            # and do not commit any token or KV position.
            seq.status = SequenceStatus.FINISHED_SO_FAILED
            seq.finish_reason = "structured_output"
            return StepResult(seq=seq, computed_end=old_length)
        self.spec_stats.verified += verify_len
        self.spec_stats.accepted += verification.accepted
        self.metrics.speculative_verified += verify_len
        self.metrics.speculative_accepted += verification.accepted
        computed_end = old_length + verification.accepted
        result = StepResult(
            seq=seq,
            computed_end=computed_end,
            tokens=verification.tokens,
            logprobs=verification.logprobs,
            top_logprobs=[[] for _ in verification.tokens],
        )
        appended = self._append_tokens(
            seq,
            verification.tokens,
            verification.logprobs,
            result.top_logprobs,
            advance_matcher=True,
        )
        # A stop token may truncate the committed output before all verified
        # positions are used.  Only the appended prefix belongs in the delta;
        # the verifier's remaining tokens were never committed and must not be
        # streamed to the client or reported as output.
        result.tokens = verification.tokens[:appended]
        result.logprobs = verification.logprobs[:appended]
        result.top_logprobs = result.top_logprobs[:appended]
        # Never claim more KV than the sequence actually holds.
        result.computed_end = min(computed_end, seq.length)
        return result

    def _collect_unreported(self) -> list[RequestOutput]:
        outputs: list[RequestOutput] = []
        for seq_id, seq in list(self._unreported.items()):
            if not seq.is_finished():
                continue
            if seq.metrics.finish_time is None:
                seq.metrics.mark_finished()
                self.metrics.record_finished(seq)
            outputs.append(self._make_output(seq, []))
            self._unreported.pop(seq_id, None)
        return outputs

    # ------------------------------------------------------------------
    # Sampling and bookkeeping
    # ------------------------------------------------------------------
    def _sample(self, seq: Sequence, logits: np.ndarray):
        mask = None
        if seq.so_matcher is not None:
            if seq.so_matcher.aborted:
                seq.status = SequenceStatus.FINISHED_SO_FAILED
                seq.finish_reason = "structured_output"
                return None
            mask = seq.so_matcher.allowed_mask()
        try:
            token, logprob, top = sample_token(
                logits, seq.sampling_params, seq.rng, mask
            )
        except ValueError:
            seq.status = SequenceStatus.FINISHED_SO_FAILED
            seq.finish_reason = "structured_output"
            return None
        if seq.so_matcher is not None:
            if not seq.so_matcher.accept_token(token):
                seq.status = SequenceStatus.FINISHED_SO_FAILED
                seq.finish_reason = "structured_output"
                return None
            if (
                seq.so_matcher.finished
                and token == self.tokenizer.eos_id
                and not seq.sampling_params.ignore_eos
                and seq.num_output_tokens >= seq.sampling_params.min_tokens
            ):
                seq.status = SequenceStatus.FINISHED_STOPPED
                seq.finish_reason = "stop"
        return token, logprob, top

    def _append_tokens(
        self,
        seq: Sequence,
        tokens: list[int],
        logprobs: list[float],
        tops: list[list[tuple[int, float]]],
        *,
        advance_matcher: bool = False,
    ) -> int:
        """Append tokens until a stop condition and return how many were kept.

        The return value matters for speculative decoding: the verifier may
        produce ``k`` tokens, but a stop token can commit only the first few.
        Callers must use the returned count to build ``StepResult.tokens`` and
        the streaming delta; otherwise the client sees tokens that were never
        committed to the sequence.
        """
        appended = 0
        for index, token in enumerate(tokens):
            if seq.is_finished():
                break
            if advance_matcher and seq.so_matcher is not None:
                if not seq.so_matcher.accept_token(token):
                    seq.status = SequenceStatus.FINISHED_SO_FAILED
                    seq.finish_reason = "structured_output"
                    break
            seq.append_token(token, logprobs[index] if index < len(logprobs) else 0.0)
            appended += 1
            seq.top_logprobs = tops[index] if index < len(tops) else []
            if seq.num_output_tokens == 1:
                seq.metrics.mark_first_token()
            text = seq.decoded_output(self.tokenizer)
            if seq.check_stop(self.tokenizer, text):
                break
        if seq.is_finished():
            seq.metrics.mark_finished()
            self.metrics.record_finished(seq)
        return appended

    def _make_output(self, seq: Sequence, delta: list[int]) -> RequestOutput:
        text = seq.decoded_output(self.tokenizer)
        for stop in seq.sampling_params.stop:
            if stop and stop in text:
                text = text.split(stop, 1)[0]
        return RequestOutput(
            request_id=seq.request_id,
            seq_id=seq.seq_id,
            token_ids=list(seq.output_token_ids),
            text=text,
            finished=seq.is_finished(),
            finish_reason=seq.finish_reason,
            prompt_tokens=seq.num_prompt_tokens,
            output_tokens=seq.num_output_tokens,
            metrics=seq.metrics,
            logprobs=list(seq.output_logprobs),
            prefix_cache_hit=seq.prefix_cache_hit,
            extra={
                "delta_token_ids": list(delta),
                "max_tokens": seq.sampling_params.max_tokens,
            },
        )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        return {
            "metrics": self.metrics.summary(),
            "scheduler": self.scheduler.stats(),
            "block_manager": self.block_manager.stats(),
            "kv_cache_bytes": self.kv_cache.memory_bytes(),
            "kv_cache_used_bytes": self.kv_cache.used_bytes(),
            "kv_cache_occupancy": self.kv_cache.occupancy(),
            "speculative": self.spec_stats.as_dict(),
        }

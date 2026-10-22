"""Iteration-level scheduler.

The policy mirrors the observable behaviour of DeepInfer's C++ scheduler:

* prefill and decode are separate step types;
* prefill is chunked and bounded by a token budget;
* decode uses LCFS (longest output first) and preempts the shortest output
  when the paged cache is exhausted;
* a preempted request is either stopped or recomputed (prefix cache may make
  the recomputation cheap);
* NORMAL engines periodically attempt prefill while decode requests run.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .block_manager import BlockManager, compute_block_hash
from .cache_engine import PagedKVCache
from .config import EngineConfig
from .errors import OutOfBlocksError
from .sequence import Sequence, SequenceStatus


@dataclass
class ScheduledSeq:
    seq: Sequence
    start: int
    end: int
    is_prompt: bool
    chunk_complete: bool
    q_len: int


@dataclass
class SchedulerOutput:
    scheduled: list[ScheduledSeq] = field(default_factory=list)
    is_prompt: bool = False
    num_batched_tokens: int = 0

    @property
    def empty(self) -> bool:
        return not self.scheduled


@dataclass
class StepResult:
    seq: Sequence
    tokens: list[int] = field(default_factory=list)
    computed_end: int = 0
    logprobs: list[float] = field(default_factory=list)
    top_logprobs: list[list[tuple[int, float]]] = field(default_factory=list)


class Scheduler:
    def __init__(
        self,
        config: EngineConfig,
        block_manager: BlockManager,
        kv_cache: PagedKVCache,
    ) -> None:
        self.config = config
        self.block_manager = block_manager
        self.kv_cache = kv_cache
        self.waiting: deque[Sequence] = deque()
        self.running: list[Sequence] = []
        self.decode_step = 0
        self.schedule_counter = 0
        self.num_preempted = 0
        self.num_prompt_steps = 0
        self.num_decode_steps = 0
        self._waiting_since: dict[int, int] = {}

    # ------------------------------------------------------------------
    # Admission
    # ------------------------------------------------------------------
    def add_sequence(self, seq: Sequence) -> None:
        seq.extra.setdefault("max_model_len", self.config.max_model_len)
        if seq.length > self.config.max_model_len:
            seq.status = SequenceStatus.FINISHED_IGNORED
            seq.finish_reason = "length"
            return
        if not self.config.enable_chunked_prefill and seq.length > self.config.max_num_batched_tokens:
            seq.status = SequenceStatus.FINISHED_IGNORED
            seq.finish_reason = "length"
            return
        self.prepare_sequence(seq)
        seq.status = SequenceStatus.WAITING
        self.waiting.append(seq)
        self._waiting_since[seq.seq_id] = self.schedule_counter

    def prepare_sequence(self, seq: Sequence) -> None:
        """Match the prefix cache and reset KV bookkeeping."""
        if seq.block_table:
            self.kv_cache.free_sequence(seq)
        # Leave at least one token to compute so the model can produce logits.
        limit = max(0, seq.length - 1)
        hit, blocks, _ = self.block_manager.match_prefix(seq.all_token_ids()[:limit])
        seq.block_table = blocks
        seq.num_computed_tokens = hit
        seq.num_cached_tokens = hit
        seq.prefix_cache_hit = hit
        # Blocks that came from the prefix cache are already registered.
        seq.extra["prefix_registered_blocks"] = len(blocks)

    def register_prefix_blocks(self, seq: Sequence) -> None:
        """Publish newly completed blocks, without rehashing the whole prefix."""
        if not self.config.enable_prefix_cache:
            return
        block_size = self.block_manager.block_size
        start = int(seq.extra.get("prefix_registered_blocks", 0))
        if start < 0 or start > len(seq.block_table):
            start = 0
        # The hash chain needs the previous block's hash.  If that block is
        # not registered (e.g. it was evicted and reused), restart from zero.
        prev = 0
        if start > 0:
            previous = self.block_manager.blocks[seq.block_table[start - 1]]
            if previous.block_hash is None:
                start = 0
            else:
                prev = previous.block_hash
        tokens = seq.all_token_ids()
        limit = seq.num_computed_tokens
        index = start
        while index < len(seq.block_table):
            chunk_start = index * block_size
            chunk_end = chunk_start + block_size
            if chunk_end > limit:
                break
            chunk = tuple(tokens[chunk_start:chunk_end])
            block_hash = compute_block_hash(prev, chunk)
            block_id = seq.block_table[index]
            block = self.block_manager.blocks[block_id]
            if block.block_hash != block_hash:
                self.block_manager.register(block_id, block_hash, chunk)
            prev = block_hash
            index += 1
        seq.extra["prefix_registered_blocks"] = index

    def abort(self, request_id: str) -> int:
        removed = 0
        for seq in list(self.waiting):
            if seq.request_id == request_id:
                seq.status = SequenceStatus.FINISHED_ABORTED
                seq.finish_reason = "aborted"
                self.kv_cache.free_sequence(seq)
                self.waiting.remove(seq)
                removed += 1
        for seq in list(self.running):
            if seq.request_id == request_id:
                seq.status = SequenceStatus.FINISHED_ABORTED
                seq.finish_reason = "aborted"
                self.kv_cache.free_sequence(seq)
                self.running.remove(seq)
                removed += 1
        return removed

    def has_unfinished(self) -> bool:
        return bool(self.waiting or self.running)

    def num_unfinished(self) -> int:
        return len(self.waiting) + len(self.running)

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------
    def _needs_prefill(self) -> bool:
        if not self.waiting:
            return False
        if not self.running:
            return True
        # Continue a chunked prefill immediately.
        head = self.waiting[0]
        if 0 < head.num_computed_tokens < head.length:
            return True
        # ``decode_step`` counts consecutive decode steps since the last
        # prefill.  Using ``>=`` (not ``%``) prevents a burst of prefills:
        # after a prefill it is 0, so the next step does decode work first.
        if self.decode_step >= self.config.prefill_attempt_interval:
            return True
        # Starvation guard: force a prefill attempt for an old request.
        since = self._waiting_since.get(head.seq_id, self.schedule_counter)
        return self.schedule_counter - since >= self.config.prefill_any_attempt_interval

    def schedule(self) -> SchedulerOutput:
        self.schedule_counter += 1
        if self._needs_prefill():
            output = self._schedule_prefill()
            self.decode_step = 0
            self.num_prompt_steps += 1
            if output.empty and self.running:
                # Prefill could not allocate or had no slot; do useful decode
                # work instead of spinning on an empty prompt step.
                output = self._schedule_decode()
                self.num_decode_steps += 1
        else:
            output = self._schedule_decode()
            self.decode_step += 1
            self.num_decode_steps += 1
        return output

    def _schedule_prefill(self) -> SchedulerOutput:
        budget = self.config.max_num_batched_tokens
        scheduled: list[ScheduledSeq] = []
        # In-flight chunked requests count toward the sequence limit.
        available_slots = self.config.max_num_seqs - len(self.running)
        while self.waiting and available_slots > 0 and budget > 0:
            seq = self.waiting[0]
            if seq.is_finished():
                self.waiting.popleft()
                continue
            remaining = seq.length - seq.num_computed_tokens
            if remaining <= 0:
                self.waiting.popleft()
                continue
            chunk = min(remaining, budget)
            if chunk < remaining and not self.config.enable_chunked_prefill:
                break
            target = seq.num_computed_tokens + chunk
            try:
                self.kv_cache.ensure_capacity(seq, target)
            except OutOfBlocksError:
                # A chunked request parked in `waiting` may be holding pages
                # that the head request needs to finish.  Release those pages
                # (the parked request will restart from token 0) and retry.
                if not self._release_waiting_blocks(protected=seq):
                    break
                try:
                    self.kv_cache.ensure_capacity(seq, target)
                except OutOfBlocksError:
                    break
            complete = target == seq.length
            scheduled.append(
                ScheduledSeq(
                    seq=seq,
                    start=seq.num_computed_tokens,
                    end=target,
                    is_prompt=True,
                    chunk_complete=complete,
                    q_len=chunk,
                )
            )
            budget -= chunk
            self.waiting.popleft()
            self._waiting_since.pop(seq.seq_id, None)
            # A completed prompt occupies a running slot next step; a chunked
            # one stays in flight but is not scheduled again in this step.
            if complete:
                available_slots -= 1
        return SchedulerOutput(
            scheduled=scheduled,
            is_prompt=True,
            num_batched_tokens=sum(item.q_len for item in scheduled),
        )

    def _release_waiting_blocks(self, protected: Sequence) -> bool:
        released = False
        for seq in self.waiting:
            if seq is protected or not seq.block_table:
                continue
            self.kv_cache.free_sequence(seq)
            seq.num_computed_tokens = 0
            seq.block_table = []
            seq.status = SequenceStatus.WAITING
            released = True
        return released

    def _preempt(self, seq: Sequence) -> None:
        self.num_preempted += 1
        self.kv_cache.free_sequence(seq)
        seq.num_computed_tokens = 0
        seq.block_table = []
        if self.config.stop_preempted_request:
            seq.status = SequenceStatus.FINISHED_PREEMPTED
            seq.finish_reason = "preempted"
            return
        # Do not re-pin prefix-cache blocks here: the point of preemption is
        # to release memory.  The request will re-match on its next prefill.
        seq.reset_for_recompute()
        self.waiting.appendleft(seq)
        self._waiting_since[seq.seq_id] = self.schedule_counter

    def _schedule_decode(self) -> SchedulerOutput:
        # LCFS: longest output first; preempt the shortest output when memory
        # runs out.  This is the policy in DeepInfer's C++ scheduler.
        queue = sorted(self.running, key=lambda s: s.num_output_tokens)
        scheduled: list[ScheduledSeq] = []
        budget = self.config.max_num_batched_tokens
        q_len = 1 + (self.config.speculative_tokens if self.config.enable_speculative else 0)
        while queue and len(scheduled) < self.config.max_num_seqs:
            seq = queue[-1]
            if seq.length >= self.config.max_model_len:
                seq.status = SequenceStatus.FINISHED_LENGTH_CAPPED
                seq.finish_reason = "length"
                queue.pop()
                continue
            # Never reserve or compute past the model's maximum length: a
            # speculative step that overruns it would index RoPE out of range.
            remaining = self.config.max_model_len - seq.length
            effective_q = min(q_len, max(1, remaining))
            target = seq.length + effective_q - 1
            try:
                self.kv_cache.ensure_capacity(seq, target)
            except OutOfBlocksError:
                if len(queue) > 1:
                    victim = queue.pop(0)
                    self._preempt(victim)
                    continue
                self._preempt(queue.pop())
                break
            if q_len > budget:
                break
            scheduled.append(
                ScheduledSeq(
                    seq=seq,
                    start=seq.length - 1,
                    end=target,
                    is_prompt=False,
                    chunk_complete=True,
                    q_len=q_len,
                )
            )
            budget -= q_len
            queue.pop()
        # Requests that were not scheduled stay in the running set.
        self.running = queue + [item.seq for item in scheduled]
        return SchedulerOutput(
            scheduled=scheduled,
            is_prompt=False,
            num_batched_tokens=sum(item.q_len for item in scheduled),
        )

    # ------------------------------------------------------------------
    # Post-processing
    # ------------------------------------------------------------------
    def postprocess(self, output: SchedulerOutput, results: dict[int, StepResult]) -> None:
        for item in output.scheduled:
            seq = item.seq
            result = results.get(seq.seq_id)
            if result is not None:
                seq.num_computed_tokens = result.computed_end
            if item.is_prompt:
                if item.chunk_complete:
                    if not seq.is_finished():
                        seq.status = SequenceStatus.RUNNING
                        if seq not in self.running:
                            self.running.append(seq)
                    self.register_prefix_blocks(seq)
                else:
                    seq.status = SequenceStatus.WAITING
                    self.waiting.appendleft(seq)
                    self._waiting_since[seq.seq_id] = self.schedule_counter
            else:
                self.register_prefix_blocks(seq)
            if seq.is_finished():
                self.kv_cache.free_sequence(seq)
                if seq in self.running:
                    self.running.remove(seq)
        # Remove any request that finished outside postprocess (abort).
        self.running = [s for s in self.running if not s.is_finished()]

    def stats(self) -> dict[str, int]:
        return {
            "waiting": len(self.waiting),
            "running": len(self.running),
            "preempted": self.num_preempted,
            "prompt_steps": self.num_prompt_steps,
            "decode_steps": self.num_decode_steps,
        }

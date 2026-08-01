"""Continuous-batching scheduler.

This is the mini version of ``vllm/core/scheduler.py``.  It implements:

* **continuous batching**  - a running decode batch is grown with new
  prefills every step, and a finished sequence is immediately replaced;
* **chunked prefill**      - a long prompt can be prefilled over several
  steps, interleaved with decodes, *and* each chunk is limited by the number
  of currently-free KV blocks;
* **recompute preemption** - when the KV cache is full, the lowest-priority
  running sequence is evicted (its KV blocks freed) and re-prefilled later,
  which is vLLM's ``PreemptionMode.RECOMPUTE``.

A single ``schedule()`` call produces one engine step.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import List, Tuple

from .config import SchedulerConfig
from .kv_cache import BlockManager
from .sequence import RUNNING, WAITING, Sequence


@dataclass
class ScheduledStep:
    """The work for one engine step."""

    prefill_items: List[Tuple[Sequence, int]] = field(default_factory=list)
    #: (seq, num_new_tokens) to prefill
    decode_items: List[Sequence] = field(default_factory=list)
    #: sequences that decode one token each
    preempted: List[Sequence] = field(default_factory=list)
    #: sequences evicted this step (recompute preemption)


class Scheduler:
    def __init__(self, config: SchedulerConfig, block_manager: BlockManager):
        self.config = config
        self.block_manager = block_manager
        self.waiting: deque[Sequence] = deque()
        self.running: List[Sequence] = []
        #: seq ids admitted during the *current* schedule() call; they must not
        #: be immediately evicted to make room for a lower-priority request.
        self._newly_admitted = set()

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def add_sequence(self, seq: Sequence) -> None:
        seq.state = WAITING
        self.waiting.append(seq)

    def has_pending(self) -> bool:
        return bool(self.waiting or self.running)

    def schedule(self) -> ScheduledStep:
        """Compute the batch of sequences to run in the next engine step."""
        self.running = [s for s in self.running if not s.is_finished]
        self._newly_admitted = set()
        step = ScheduledStep()
        num_batched_tokens = 0
        max_tokens = self.config.max_num_batched_tokens

        # ---------------- Phase A: decode for fully-prefilled seqs ----------
        # Every running decode consumes exactly one token slot this step and
        # (usually) one extra KV block for the new token.
        for seq in list(self.running):
            if seq.in_prefill:
                continue
            if not self.block_manager.can_allocate(seq, seq.cached_len + 1):
                # KV cache is full: evict the lowest-priority sequence, then
                # retry; if it still fails, leave this sequence for next step.
                self._preempt_one(step)
                if not self.block_manager.can_allocate(seq, seq.cached_len + 1):
                    continue
            self.block_manager.ensure_blocks(seq, seq.cached_len + 1)
            step.decode_items.append(seq)
            num_batched_tokens += 1

        # ---------------- Phase B: prefill -------------------------------
        remaining = max_tokens - num_batched_tokens

        # B1: continue prompts that are mid-prefill
        for seq in list(self.running):
            if not seq.in_prefill:
                continue
            if remaining <= 0:
                break
            need = seq.num_tokens - seq.cached_len
            chunk = self._chunk_for(seq, need, remaining)
            if chunk <= 0:
                self._preempt_one(step)
                continue
            self.block_manager.ensure_blocks(seq, seq.cached_len + chunk)
            step.prefill_items.append((seq, chunk))
            remaining -= chunk
            num_batched_tokens += chunk

        # B2: admit waiting sequences (FCFS)
        while (self.waiting and remaining > 0
               and len(self.running) < self.config.max_num_seqs):
            seq = self.waiting[0]

            # prefix caching: reuse KV blocks for a shared prompt prefix
            if seq.cached_len == 0:
                prefix_len, blocks = self.block_manager.match_prefix(seq.prompt_ids)
                if prefix_len > 0:
                    self.block_manager.attach_shared_blocks(seq, blocks)
                    seq.cached_len = prefix_len

            prompt_remaining = seq.num_tokens - seq.cached_len

            if prompt_remaining == 0:
                # Entire prompt is already cached: jump straight to decode.
                # (Decode convention: cached_len == num_tokens - 1, the last
                # token's KV is re-stored on the first decode step.)
                seq.phase = "DECODE"
                seq.cached_len -= 1
                self.waiting.popleft()
                self._activate(seq)
                if self.block_manager.can_allocate(seq, seq.cached_len + 1):
                    self.block_manager.ensure_blocks(seq, seq.cached_len + 1)
                    step.decode_items.append(seq)
                    num_batched_tokens += 1
                    remaining -= 1
                else:
                    self._preempt_one(step)
                continue

            if not self.config.enable_chunked_prefill:
                # A prompt must fit entirely in one step when chunking is off
                if prompt_remaining > remaining:
                    break

            chunk = self._chunk_for(seq, prompt_remaining, remaining)
            if chunk <= 0:
                if not self._preempt_one(step):
                    break
                continue

            self.waiting.popleft()
            self._activate(seq)
            self.block_manager.ensure_blocks(seq, seq.cached_len + chunk)
            step.prefill_items.append((seq, chunk))
            remaining -= chunk
            num_batched_tokens += chunk

        return step

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _chunk_for(self, seq: Sequence, need: int, remaining: int) -> int:
        """Max prefill chunk limited by the token budget *and* free blocks.

        This is what makes chunked prefill actually work under memory
        pressure: instead of stalling because the whole prompt does not fit,
        the prompt is split into a piece that fits in the blocks that are
        free *right now*.
        """
        block_size = self.block_manager.block_size
        have_blocks = len(self.block_manager.get_block_table(seq))
        max_tokens_by_blocks = (have_blocks + self.block_manager.num_free_blocks) * block_size
        extra_by_blocks = max(0, max_tokens_by_blocks - seq.cached_len)
        return max(0, min(need, remaining, extra_by_blocks))

    def _activate(self, seq: Sequence) -> None:
        self._newly_admitted.add(seq.seq_id)
        seq.state = RUNNING
        self.running.append(seq)

    def _preempt_one(self, step: ScheduledStep | None = None) -> bool:
        """Evict the lowest-priority running sequence (recompute mode).

        Its KV blocks are freed; its generated tokens are kept, and it is put
        back at the front of the waiting queue so it re-prefills promptly.

        Sequences admitted *during this schedule() call* are never evicted,
        otherwise we would admit a request and immediately free it again.
        Any already-scheduled work for the evicted sequence is dropped so the
        engine never executes a stale prefill/decode item.
        """
        if not self.config.enable_preemption:
            return False
        for i in range(len(self.running) - 1, -1, -1):
            seq = self.running[i]
            if seq.seq_id in self._newly_admitted:
                continue
            del self.running[i]
            self.block_manager.release(seq)
            seq.cached_len = 0
            seq.phase = "PREFILL"      # re-prefill prompt + already-generated tokens
            seq.is_preempted = True
            seq.state = WAITING
            self.waiting.appendleft(seq)
            if step is not None:
                step.prefill_items = [(s, c) for s, c in step.prefill_items
                                      if s.seq_id != seq.seq_id]
                step.decode_items = [s for s in step.decode_items
                                     if s.seq_id != seq.seq_id]
            return True
        return False

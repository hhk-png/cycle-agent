"""Runtime data structures: Sequence, CompletionOutput, RequestOutput.

These mirror ``vllm.sequence``.  A real vLLM ``Sequence`` also tracks
logprobs, token logprob dicts, prompt logprobs and (with beam search) a
sequence *group*.  The mini version keeps only what the rest of the engine
needs, but the shape of ``RequestOutput`` matches the OpenAI-compatible
server's expectation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .config import SamplingParams

# Sequence states (subset of vLLM's SequenceStatus)
WAITING = "WAITING"
RUNNING = "RUNNING"
FINISHED = "FINISHED"
PREEMPTED = "PREEMPTED"


@dataclass
class Sequence:
    seq_id: int
    prompt_ids: List[int]
    sampling_params: SamplingParams
    arrival_time: float = 0.0
    output_ids: List[int] = field(default_factory=list)
    cached_len: int = 0          # number of tokens (prompt+outputs) in the KV cache
    phase: str = "PREFILL"       # "PREFILL" | "DECODE"
    state: str = WAITING
    stop_reason: Optional[str] = None
    cumulative_logprob: float = 0.0
    is_preempted: bool = False
    priority: int = 0            # higher = served first (arrival order)

    # ------------------------------------------------------------------ #
    @property
    def prompt_len(self) -> int:
        return len(self.prompt_ids)

    @property
    def all_ids(self) -> List[int]:
        """Every token of the sequence so far (prompt + generated).

        After a recompute-preemption we re-prefill *this* list, so the
        already-generated tokens are not lost.
        """
        return self.prompt_ids + self.output_ids

    @property
    def num_tokens(self) -> int:
        return len(self.prompt_ids) + len(self.output_ids)

    @property
    def is_finished(self) -> bool:
        return self.state == FINISHED

    @property
    def in_prefill(self) -> bool:
        return self.phase == "PREFILL"

    def append_token(self, token_id: int) -> None:
        self.output_ids.append(token_id)

    def __repr__(self) -> str:
        return (f"Sequence(seq_id={self.seq_id}, state={self.state}, "
                f"phase={self.phase}, cached_len={self.cached_len}, "
                f"tokens={self.num_tokens})")


@dataclass
class CompletionOutput:
    """One output sequence of a request (vLLM's ``CompletionOutput``)."""

    index: int
    text: str                       # cumulative decoded text
    token_ids: List[int]            # cumulative generated token ids
    cumulative_logprob: float = 0.0
    finish_reason: Optional[str] = None

    def delta(self, other_text: str) -> str:
        """Newly-generated substring relative to an earlier snapshot."""
        if other_text and self.text.startswith(other_text):
            return self.text[len(other_text):]
        return self.text


@dataclass
class RequestOutput:
    """One request's outputs after an engine step (vLLM's ``RequestOutput``)."""

    request_id: int
    prompt: str
    prompt_token_ids: List[int]
    outputs: List[CompletionOutput]
    finished: bool

    @classmethod
    def from_sequence(cls, seq: Sequence, tokenizer) -> "RequestOutput":
        text = tokenizer.decode(seq.output_ids)
        out = CompletionOutput(
            index=0,
            text=text,
            token_ids=list(seq.output_ids),
            cumulative_logprob=seq.cumulative_logprob,
            finish_reason=seq.stop_reason,
        )
        return cls(
            request_id=seq.seq_id,
            prompt=tokenizer.decode(seq.prompt_ids),
            prompt_token_ids=list(seq.prompt_ids),
            outputs=[out],
            finished=seq.is_finished,
        )

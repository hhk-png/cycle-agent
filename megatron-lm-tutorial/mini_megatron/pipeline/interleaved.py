"""Virtual pipeline (interleaved) layout planning and schedule validation.

A non-interleaved pipeline gives each rank one contiguous block of layers.  A
virtual pipeline (VPP, also called interleaved 1F1B) splits each rank's layers
into ``v`` chunks and interleaves chunk ownership around the ring:

    rank 0: chunks 0, p, 2p, ...
    rank 1: chunks 1, p+1, 2p+1, ...

A microbatch still traverses chunks in global order ``0 .. p*v-1``; the win is
that a rank can work on chunk ``k+p`` while chunk ``k`` is waiting for a
downstream gradient, which shrinks the pipeline bubble from roughly
``(p-1)/(m+p-1)`` to ``(p-1)/(m*v+p-1)``.

This module does **not** reimplement Megatron's production scheduler.  It
provides:

* the chunk->rank/layer mapping used by a VPP layout;
* a dependency validator for a forward/backward trace;
* an interleaved GPipe trace (all forwards, then all backwards) for teaching;
* the analytical bubble ratio used in the chapters.

The production 1F1B interleaved order lives in
``megatron/core/pipeline_parallel/schedules.py`` and additionally handles
warmup/cooldown, activation memory, and communication overlap.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

Event = Tuple[str, int, int]  # ("F"|"B", microbatch, chunk)


@dataclass(frozen=True)
class VirtualPipelineLayout:
    pp_size: int
    chunks_per_rank: int
    num_layers: int

    def __post_init__(self) -> None:
        if self.pp_size <= 0 or self.chunks_per_rank <= 0:
            raise ValueError("pp_size and chunks_per_rank must be positive")
        if self.num_layers % self.num_chunks != 0:
            raise ValueError(
                f"num_layers ({self.num_layers}) must be divisible by "
                f"pp_size*chunks_per_rank ({self.num_chunks})"
            )

    @property
    def num_chunks(self) -> int:
        return self.pp_size * self.chunks_per_rank

    @property
    def layers_per_chunk(self) -> int:
        return self.num_layers // self.num_chunks

    def owner(self, chunk: int) -> int:
        """Rank that owns a global chunk id."""
        self._check_chunk(chunk)
        return chunk % self.pp_size

    def local_chunk(self, chunk: int) -> int:
        """Index of the chunk within its owning rank."""
        self._check_chunk(chunk)
        return chunk // self.pp_size

    def layers_of_chunk(self, chunk: int) -> range:
        self._check_chunk(chunk)
        start = chunk * self.layers_per_chunk
        return range(start, start + self.layers_per_chunk)

    def chunks_of_rank(self, rank: int) -> List[int]:
        if not 0 <= rank < self.pp_size:
            raise ValueError("rank out of range")
        return list(range(rank, self.num_chunks, self.pp_size))

    def _check_chunk(self, chunk: int) -> None:
        if not 0 <= chunk < self.num_chunks:
            raise ValueError(f"chunk {chunk} out of range [0, {self.num_chunks})")


def vpp_bubble_ratio(microbatches: int, pp_size: int, chunks_per_rank: int) -> float:
    """Analytical bubble fraction for interleaved 1F1B (no zero-bubble)."""
    if microbatches <= 0 or pp_size <= 1 or chunks_per_rank <= 0:
        return 0.0
    return (pp_size - 1) / (microbatches * chunks_per_rank + pp_size - 1)


def non_interleaved_bubble_ratio(microbatches: int, pp_size: int) -> float:
    if microbatches <= 0 or pp_size <= 1:
        return 0.0
    return (pp_size - 1) / (microbatches + pp_size - 1)


def gpipe_interleaved_trace(
    layout: VirtualPipelineLayout, microbatches: int
) -> List[Event]:
    """A simple valid VPP trace: all forwards (chunk order), then all backwards.

    This is GPipe-style, not 1F1B: it is easy to validate and makes the chunk
    ordering visible.  It is *not* memory-optimal; production uses 1F1B.
    """
    trace: List[Event] = []
    for mb in range(microbatches):
        for chunk in range(layout.num_chunks):
            trace.append(("F", mb, chunk))
    for mb in reversed(range(microbatches)):
        for chunk in reversed(range(layout.num_chunks)):
            trace.append(("B", mb, chunk))
    return trace


def validate_trace(trace: Sequence[Event], layout: VirtualPipelineLayout) -> None:
    """Raise ``ValueError`` if ``trace`` violates dependencies or rank order."""
    forward_done: Dict[Tuple[int, int], bool] = {}
    backward_done: Dict[Tuple[int, int], bool] = {}
    for step, (op, mb, chunk) in enumerate(trace):
        layout._check_chunk(chunk)
        if op == "F":
            if chunk > 0 and not forward_done.get((mb, chunk - 1), False):
                raise ValueError(
                    f"step {step}: F(mb={mb}, chunk={chunk}) before upstream chunk"
                )
            if (mb, chunk) in forward_done:
                raise ValueError(f"step {step}: duplicate F(mb={mb}, chunk={chunk})")
            forward_done[(mb, chunk)] = True
        elif op == "B":
            if not forward_done.get((mb, chunk), False):
                raise ValueError(
                    f"step {step}: B(mb={mb}, chunk={chunk}) before its forward"
                )
            if chunk + 1 < layout.num_chunks and not backward_done.get(
                (mb, chunk + 1), False
            ):
                raise ValueError(
                    f"step {step}: B(mb={mb}, chunk={chunk}) before downstream gradient"
                )
            if (mb, chunk) in backward_done:
                raise ValueError(f"step {step}: duplicate B(mb={mb}, chunk={chunk})")
            backward_done[(mb, chunk)] = True
        else:
            raise ValueError(f"step {step}: unknown op {op!r}")
        # The next event in a real schedule may run on another rank in
        # parallel; the validator serialises events only to check ordering.


def rank_view(
    trace: Sequence[Event], layout: VirtualPipelineLayout, rank: int
) -> List[Event]:
    return [event for event in trace if layout.owner(event[2]) == rank]


def max_inflight_forwards(trace: Sequence[Event], layout: VirtualPipelineLayout) -> int:
    """Peak number of forwards whose backward has not started (per trace)."""
    started: set[Tuple[int, int]] = set()
    finished: set[Tuple[int, int]] = set()
    peak = 0
    for op, mb, chunk in trace:
        if op == "F":
            started.add((mb, chunk))
        else:
            finished.add((mb, chunk))
        peak = max(peak, len(started - finished))
    return peak


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VPP layout / schedule planner")
    parser.add_argument("--pp", type=int, default=4)
    parser.add_argument("--chunks-per-rank", type=int, default=2)
    parser.add_argument("--layers", type=int, default=16)
    parser.add_argument("--microbatches", type=int, default=8)
    parser.add_argument("--rank", type=int, default=None)
    args = parser.parse_args(argv)

    layout = VirtualPipelineLayout(
        pp_size=args.pp,
        chunks_per_rank=args.chunks_per_rank,
        num_layers=args.layers,
    )
    trace = gpipe_interleaved_trace(layout, args.microbatches)
    validate_trace(trace, layout)
    print(
        f"chunks={layout.num_chunks}, layers/chunk={layout.layers_per_chunk}, "
        f"bubble(1F1B)={vpp_bubble_ratio(args.microbatches, args.pp, args.chunks_per_rank):.3%} "
        f"vs non-interleaved={non_interleaved_bubble_ratio(args.microbatches, args.pp):.3%}"
    )
    for rank in range(args.pp):
        chunks = layout.chunks_of_rank(rank)
        print(f"rank {rank}: chunks={chunks}, layers={[list(layout.layers_of_chunk(c)) for c in chunks]}")
    if args.rank is not None:
        print(f"rank {args.rank} events: {rank_view(trace, layout, args.rank)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

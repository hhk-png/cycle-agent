"""YoCo-style bounded replay for sliding-window attention.

The production engine does not persist the full SWA KV.  When a prefix cache
hit leaves the sliding window incomplete, it replays only the smallest suffix
that can rebuild the window.  This module isolates the decision logic so that
the policy can be tested without a GPU:

* round the block-KV hit down to a block boundary;
* accept the hit only if the SWA state covers the tail window;
* otherwise fall back to a bounded replay of ``window`` tokens;
* for the decoder, replay at most ``keep_tokens`` (window + backtracking).

The numbers are the public DeepInfer defaults (window 128, backtracking 6,
keep 140).  They are parameters, not hard-coded policy, because a different
model may use a different window.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_WINDOW = 128
DEFAULT_BACKTRACKING = 6
DEFAULT_KEEP_TOKENS = DEFAULT_WINDOW + DEFAULT_BACKTRACKING


@dataclass(frozen=True)
class ReplayPlan:
    block_hit_tokens: int
    swa_cached_tokens: int
    effective_hit_tokens: int
    replay_tokens: int
    needs_replay: bool
    reason: str


def _validate(window: int, backtracking: int, keep_tokens: int) -> None:
    if window < 1:
        raise ValueError("window must be >= 1")
    if backtracking < 0:
        raise ValueError("backtracking must be >= 0")
    if keep_tokens < window + backtracking:
        raise ValueError("keep_tokens must cover window + backtracking")


def plan_encoder_hit(
    block_hit_tokens: int,
    swa_cached_tokens: int,
    *,
    window: int = DEFAULT_WINDOW,
    backtracking: int = DEFAULT_BACKTRACKING,
    block_size: int = 128,
) -> ReplayPlan:
    """Resolve a prefix-cache hit against the available SWA state.

    ``block_hit_tokens`` is what the block KV can serve; ``swa_cached_tokens``
    is how many recent tokens the local SWA slot actually covers.  A block hit
    alone is not enough: decode needs a complete window.
    """
    if block_hit_tokens < 0 or swa_cached_tokens < 0:
        raise ValueError("token counts must be non-negative")
    _validate(window, backtracking, window + backtracking)
    if block_size < 1:
        raise ValueError("block_size must be >= 1")

    block_hit = (block_hit_tokens // block_size) * block_size
    if block_hit == 0:
        return ReplayPlan(0, swa_cached_tokens, 0, 0, False, "no block hit")

    if swa_cached_tokens >= block_hit:
        return ReplayPlan(block_hit, swa_cached_tokens, block_hit, 0, False, "swa complete")

    # The SWA state must cover the last ``window`` tokens of the hit.  If it
    # does, the block hit is usable; the window itself is already materialised.
    if swa_cached_tokens >= block_hit - window:
        return ReplayPlan(block_hit, swa_cached_tokens, block_hit, 0, False, "swa covers tail")

    # Otherwise retreat to the last complete window and replay it.  Replaying
    # more than the window is wasted work because SWA cannot see past it.
    replay = min(window, block_hit)
    effective = max(0, block_hit - replay)
    return ReplayPlan(
        block_hit,
        swa_cached_tokens,
        effective,
        replay,
        True,
        "swa missing; replay window",
    )


def plan_decoder_replay(
    sequence_length: int,
    *,
    window: int = DEFAULT_WINDOW,
    backtracking: int = DEFAULT_BACKTRACKING,
    keep_tokens: int = DEFAULT_KEEP_TOKENS,
) -> ReplayPlan:
    """Bound the suffix that must be replayed through the decoder layers.

    The decoder's global KV is projected from the encoder hidden state, so a
    truncation is approximate.  DeepInfer limits the approximation to the
    decode path and keeps the replay length constant (140 by default), which
    is what makes the cost independent of the total context length.
    """
    if sequence_length < 0:
        raise ValueError("sequence_length must be non-negative")
    _validate(window, backtracking, keep_tokens)
    if sequence_length <= keep_tokens:
        return ReplayPlan(
            sequence_length, sequence_length, 0, sequence_length, sequence_length > 0,
            "sequence shorter than keep window",
        )
    effective = sequence_length - keep_tokens
    return ReplayPlan(
        sequence_length,
        sequence_length,
        effective,
        keep_tokens,
        True,
        "bounded decoder replay",
    )


def replay_cost_us(
    replay_tokens: int,
    *,
    layers: int,
    microseconds_per_token_layer: float,
) -> float:
    """A simple linear cost model for the replayed tokens."""
    if replay_tokens < 0 or layers < 1 or microseconds_per_token_layer < 0:
        raise ValueError("invalid replay cost inputs")
    return replay_tokens * layers * microseconds_per_token_layer

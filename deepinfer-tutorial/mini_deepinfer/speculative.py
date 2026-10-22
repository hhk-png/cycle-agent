"""DSpark-style speculative decoding at toy scale.

The real DSpark combines a draft model, a Confidence Head and a hardware cost
model to choose a *variable* verification length.  The toy implementation keeps
the same control flow and uses the draft token's own probability as a
calibrated confidence proxy.  Acceptance itself uses the standard speculative
sampling rule, so greedy outputs are exactly equal to target-only greedy
decoding.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .cache_engine import PagedKVCache
from .model.ops import softmax
from .model.tiny_transformer import TinyTransformer
from .sampling import apply_top_k_top_p, distribution, target_probs
from .sampling_params import SamplingParams
from .sequence import Sequence


@dataclass
class DraftProposal:
    tokens: list[int] = field(default_factory=list)
    probs: list[np.ndarray] = field(default_factory=list)
    confidences: list[float] = field(default_factory=list)


@dataclass
class VerificationResult:
    tokens: list[int] = field(default_factory=list)
    logprobs: list[float] = field(default_factory=list)
    verified: int = 0
    accepted: int = 0
    all_accepted: bool = False


@dataclass
class SpeculativeStats:
    proposed: int = 0
    verified: int = 0
    accepted: int = 0
    steps: int = 0
    target_forwards: int = 0
    draft_forwards: int = 0

    @property
    def acceptance_rate(self) -> float:
        return self.accepted / self.verified if self.verified else 0.0

    @property
    def mean_accepted(self) -> float:
        return self.accepted / self.steps if self.steps else 0.0

    @property
    def mean_verified(self) -> float:
        return self.verified / self.steps if self.steps else 0.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "proposed": self.proposed,
            "verified": self.verified,
            "accepted": self.accepted,
            "steps": self.steps,
            "acceptance_rate": self.acceptance_rate,
            "mean_accepted": self.mean_accepted,
            "mean_verified": self.mean_verified,
        }


def generate_drafts(
    draft_model: TinyTransformer,
    seq: Sequence,
    num_drafts: int,
    params: SamplingParams,
) -> DraftProposal:
    """Autoregressively propose ``num_drafts`` tokens with the draft model."""
    ids = seq.all_token_ids()
    proposal = DraftProposal()
    for _ in range(num_drafts):
        logits, _, _ = draft_model.forward_full(ids)
        row = logits[-1]
        probs = target_probs(row, params)
        if params.greedy:
            token = int(np.argmax(probs))
        else:
            token = int(seq.draft_rng.choice(probs.size, p=probs))
        proposal.tokens.append(token)
        proposal.probs.append(probs)
        # Confidence proxy: the draft's own probability for the proposed
        # token.  A production Confidence Head is a learned projection of the
        # draft hidden state; see the tutorial chapter for the difference.
        proposal.confidences.append(float(min(1.0, max(0.0, probs[token]))))
        ids = ids + [token]
    return proposal


def expected_accepted_length(confidences: list[float]) -> float:
    """Expected number of accepted draft tokens, excluding the bonus token.

    For ``[1, 1, 1]`` this returns 3 (all drafts accepted); the target model
    then advances 4 positions including its own token.  ``roofline``'s
    ``expected_accept_length`` returns that larger number (``1 + ...``).
    """
    total = 0.0
    product = 1.0
    for confidence in confidences:
        product *= confidence
        total += product
    return total


def choose_verify_length(
    confidences: list[float],
    *,
    threshold: float = 0.55,
    target_cost: float = 1.0,
    draft_cost: float = 0.12,
) -> int:
    """Choose a prefix of draft tokens to verify.

    Two signals are combined: a confidence floor (never spend target compute
    on a token that is very unlikely to be accepted) and marginal utility
    (expected accepted tokens per unit of verification cost).
    """
    if not confidences:
        return 0
    best_len = 1
    best_score = -1.0
    prefix: list[float] = []
    for length, confidence in enumerate(confidences, start=1):
        prefix.append(confidence)
        if confidence < threshold:
            break
        expected = expected_accepted_length(prefix)
        score = expected / (target_cost + draft_cost * length)
        if score > best_score:
            best_score = score
            best_len = length
    return best_len


def verify_drafts(
    target_model: TinyTransformer,
    seq: Sequence,
    proposal: DraftProposal,
    verify_len: int,
    kv_cache: PagedKVCache,
    params: SamplingParams,
    *,
    matcher=None,
) -> VerificationResult:
    """Run the target on the last committed token plus draft tokens.

    Returns the accepted tokens and, when every draft is accepted, one bonus
    token sampled from the final target distribution.
    """
    verify_len = min(verify_len, len(proposal.tokens))
    if verify_len <= 0:
        return VerificationResult()
    committed = seq.all_token_ids()
    start = len(committed) - 1
    draft = proposal.tokens[:verify_len]
    input_ids = committed + draft
    end = start + 1 + verify_len
    logits, _, _ = target_model.forward_paged(
        seq, start, end, kv_cache, token_ids=input_ids
    )
    # logits[0] predicts the token after the last committed token, which is
    # exactly draft[0]; logits[i] predicts draft[i+1].
    result = VerificationResult(verified=verify_len)
    accepted = 0
    for index, token in enumerate(draft):
        row = logits[index]
        # Under a grammar, the target distribution is masked by the current
        # parser state.  The draft distribution was produced without the mask,
        # so it must be restricted and renormalised before the acceptance
        # ratio is formed; a draft token outside the grammar is rejected.
        mask = matcher.allowed_mask() if matcher is not None else None
        p_target = target_probs(row, params, mask)
        p_draft = proposal.probs[index]
        if mask is not None:
            allowed = np.asarray(mask, dtype=bool)
            token_allowed = bool(allowed[token])
            q = np.where(allowed, p_draft, 0.0)
            total = float(q.sum())
            q = q / total if total > 0 else p_target.copy()
        else:
            token_allowed = True
            q = p_draft
        if params.greedy:
            accepted_here = token_allowed and int(np.argmax(p_target)) == token
            corrected = int(np.argmax(p_target))
        else:
            ratio = p_target[token] / max(q[token], 1e-12) if token_allowed else 0.0
            accepted_here = seq.rng.random() < min(1.0, float(ratio))
            corrected = -1
            if not accepted_here:
                residual = np.maximum(p_target - q, 0.0)
                if residual.sum() <= 0:
                    residual = p_target
                residual = residual / residual.sum()
                corrected = int(seq.rng.choice(residual.size, p=residual))
        if not accepted_here:
            if matcher is not None and not matcher.accept_token(corrected):
                raise ValueError("grammar rejected the corrected token")
            result.tokens.append(corrected)
            result.logprobs.append(float(np.log(max(p_target[corrected], 1e-300))))
            result.accepted = accepted
            return result
        if matcher is not None and not matcher.accept_token(token):
            raise ValueError("grammar rejected an accepted draft token")
        accepted += 1
        result.tokens.append(token)
        result.logprobs.append(float(np.log(max(p_target[token], 1e-300))))
    result.accepted = accepted
    result.all_accepted = True
    # All drafts accepted: sample the bonus token from the final row.
    final_row = logits[verify_len]
    mask = matcher.allowed_mask() if matcher is not None else None
    probs = distribution(final_row, params, mask)
    if params.greedy:
        bonus = int(np.argmax(probs))
    else:
        bonus = int(seq.rng.choice(probs.size, p=probs))
    if matcher is not None and not matcher.accept_token(bonus):
        raise ValueError("grammar rejected the bonus token")
    result.tokens.append(bonus)
    result.logprobs.append(float(np.log(max(probs[bonus], 1e-300))))
    return result

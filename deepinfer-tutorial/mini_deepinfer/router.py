"""Model routing and cascades for a multi-model inference platform.

A single engine can only answer one question: "given this prompt, what is the
next token?"  A platform has to answer a different one first: "which model
should serve this request?"  Routing is a constrained optimization problem
over quality, context window, latency and cost.  The functions here are small
enough to reason about but keep the semantics a production router needs:
feasibility is explicit, the fallback path is visible, and a cascade's
expected cost is computed rather than guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np


@dataclass(frozen=True)
class ModelSpec:
    """The part of a model card that routing actually needs."""

    name: str
    quality: float
    input_cost_per_mtok: float
    output_cost_per_mtok: float
    base_latency_ms: float
    tpot_ms: float
    max_context: int = 1_000_000

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("model name must not be empty")
        if not 0.0 <= self.quality <= 1.0:
            raise ValueError("quality must be in [0, 1]")
        if self.input_cost_per_mtok < 0 or self.output_cost_per_mtok < 0:
            raise ValueError("token prices must be non-negative")
        if self.base_latency_ms < 0 or self.tpot_ms < 0:
            raise ValueError("latency terms must be non-negative")
        if self.max_context < 1:
            raise ValueError("max_context must be positive")

    def estimate(self, input_tokens: int, output_tokens: int) -> tuple[float, float]:
        """Return ``(cost_usd, latency_ms)`` for one request.

        The latency model is deliberately simple: a fixed overhead plus a
        per-output-token decode term.  It ignores queueing and prefill, which
        is exactly what a router needs for a first-pass comparison.  The real
        decision should still be checked against measured curves.
        """
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token counts must be non-negative")
        cost = (
            input_tokens * self.input_cost_per_mtok
            + output_tokens * self.output_cost_per_mtok
        ) / 1_000_000
        latency = self.base_latency_ms + output_tokens * self.tpot_ms
        return float(cost), float(latency)


@dataclass(frozen=True)
class RouteDecision:
    model: str
    reason: str
    cost_usd: float
    latency_ms: float
    feasible: bool


class ModelRouter:
    """Choose the cheapest model that satisfies quality and latency floors."""

    def __init__(
        self,
        models: Iterable[ModelSpec],
        *,
        quality_floor: float = 0.0,
        latency_budget_ms: float | None = None,
    ) -> None:
        self.models = tuple(models)
        if not self.models:
            raise ValueError("at least one model is required")
        names = [model.name for model in self.models]
        if len(set(names)) != len(names):
            raise ValueError("model names must be unique")
        if not 0.0 <= quality_floor <= 1.0:
            raise ValueError("quality_floor must be in [0, 1]")
        if latency_budget_ms is not None and latency_budget_ms <= 0:
            raise ValueError("latency_budget_ms must be positive or None")
        self.quality_floor = quality_floor
        self.latency_budget_ms = latency_budget_ms

    def route(
        self,
        input_tokens: int,
        output_tokens: int,
        *,
        quality_floor: float | None = None,
        latency_budget_ms: float | None = None,
    ) -> RouteDecision:
        floor = self.quality_floor if quality_floor is None else quality_floor
        budget = self.latency_budget_ms if latency_budget_ms is None else latency_budget_ms

        context_ok = [model for model in self.models if input_tokens + output_tokens <= model.max_context]
        if not context_ok:
            raise ValueError("no model can hold the requested context")

        scored = []
        for model in context_ok:
            cost, latency = model.estimate(input_tokens, output_tokens)
            feasible = model.quality >= floor and (budget is None or latency <= budget)
            scored.append((model, cost, latency, feasible))

        feasible_rows = [row for row in scored if row[3]]
        if feasible_rows:
            # Cheapest first; ties go to higher quality, then lower latency.
            model, cost, latency, _ = min(
                feasible_rows, key=lambda row: (row[1], -row[0].quality, row[2])
            )
            return RouteDecision(
                model=model.name,
                reason="lowest_cost_within_constraints",
                cost_usd=cost,
                latency_ms=latency,
                feasible=True,
            )

        # No model meets every constraint.  Do not silently violate the SLO:
        # pick the strongest capacity-eligible model and say why.
        model, cost, latency, _ = max(
            scored, key=lambda row: (row[0].quality, -row[1], -row[2])
        )
        return RouteDecision(
            model=model.name,
            reason="no_feasible_model_fallback",
            cost_usd=cost,
            latency_ms=latency,
            feasible=False,
        )


@dataclass(frozen=True)
class CascadeEstimate:
    expected_cost_usd: float
    expected_quality: float
    expected_calls: float
    acceptance_probability: float


def cascade_estimate(
    models: Sequence[ModelSpec],
    input_tokens: int,
    output_tokens: int,
    accept_probs: Sequence[float],
) -> CascadeEstimate:
    """Expected cost and quality of a cheap-to-strong cascade.

    ``accept_probs[i]`` is the probability that model ``i``'s answer is good
    enough to stop.  The first model always runs; each later model runs only
    when every earlier model was rejected.  This is the standard cascade
    model, and it makes the trade-off explicit: a cheap first model with high
    acceptance is worth far more than a slightly better one with low
    acceptance.
    """
    if len(models) != len(accept_probs):
        raise ValueError("models and accept_probs must have the same length")
    if not models:
        raise ValueError("at least one model is required")
    for probability in accept_probs:
        if not 0.0 <= probability <= 1.0:
            raise ValueError("acceptance probabilities must be in [0, 1]")

    survival = 1.0
    expected_cost = 0.0
    expected_quality = 0.0
    expected_calls = 0.0
    for model, probability in zip(models, accept_probs):
        cost, _ = model.estimate(input_tokens, output_tokens)
        expected_cost += survival * cost
        expected_quality += survival * probability * model.quality
        expected_calls += survival
        survival *= 1.0 - probability

    # The final model is used even when it rejects, so its quality fills the
    # remaining probability mass.
    expected_quality += survival * models[-1].quality
    return CascadeEstimate(
        expected_cost_usd=float(expected_cost),
        expected_quality=float(expected_quality),
        expected_calls=float(expected_calls),
        acceptance_probability=float(1.0 - survival),
    )


def break_even_acceptance(
    cheap: ModelSpec,
    strong: ModelSpec,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Acceptance rate at which a two-model cascade beats the strong model.

    ``cheap_cost + (1 - a) * strong_cost == strong_cost`` gives
    ``a == cheap_cost / strong_cost``.  Below that acceptance rate, the
    cascade is more expensive than calling the strong model directly; above
    it, the cascade wins on cost (quality still has to be checked separately).
    """
    cheap_cost, _ = cheap.estimate(input_tokens, output_tokens)
    strong_cost, _ = strong.estimate(input_tokens, output_tokens)
    if strong_cost <= 0:
        raise ValueError("strong model cost must be positive")
    return float(np.clip(cheap_cost / strong_cost, 0.0, 1.0))

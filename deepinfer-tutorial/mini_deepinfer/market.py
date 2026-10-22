"""Market sizing and go-to-market models for an inference business.

The economics module answers "what does a token cost"; the finance module
answers "does the investment pay back".  This module answers the question in
between: how large is the addressable market, how fast can it be adopted, and
what does a sales funnel have to convert to reach a revenue plan.

Every function is pure and takes explicit assumptions.  The output is only as
good as those assumptions; the point is to make them arguable, not to produce
a market forecast.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class Segment:
    """One bottom-up market segment.

    ``annual_tokens`` is the yearly token volume per account, in tokens (not
    millions); ``price_per_mtok`` is the realised price per million tokens.
    Keeping the units explicit avoids the classic "millions times millions"
    error in market sizing.
    """

    name: str
    accounts: int
    annual_tokens: float
    price_per_mtok: float

    def annual_revenue(self) -> float:
        if self.accounts < 0 or self.annual_tokens < 0 or self.price_per_mtok < 0:
            raise ValueError("segment inputs must be non-negative")
        return self.accounts * self.annual_tokens / 1_000_000 * self.price_per_mtok


def tam_bottom_up(segments: Iterable[Segment]) -> float:
    """Total addressable market as the sum of segment revenue pools.

    This is a *revenue* TAM, not a token TAM.  If you want a token TAM, drop
    the price term; mixing the two is the most common market-sizing mistake.
    """
    total = 0.0
    for segment in segments:
        total += segment.annual_revenue()
    return total


def serviceable_market(tam: float, serviceable_share: float) -> float:
    """SAM: the part of TAM a product can actually serve.

    The share should reflect product fit, geography, language, compliance and
    channel coverage.  It is not a "we could win this" number; that is SOM.
    """
    if tam < 0:
        raise ValueError("tam must be non-negative")
    if not 0.0 <= serviceable_share <= 1.0:
        raise ValueError("serviceable_share must be in [0, 1]")
    return tam * serviceable_share


def obtainable_market(
    sam: float,
    target_share: float,
    *,
    year: int = 3,
    ramp_years: float = 3.0,
) -> float:
    """SOM at a given year, with a linear ramp to the target share.

    ``target_share`` is the share of SAM the business expects at maturity.
    The ramp is deliberately simple: it forces the plan to state *when* the
    share is reached, instead of assuming day-one penetration.
    """
    if sam < 0:
        raise ValueError("sam must be non-negative")
    if not 0.0 <= target_share <= 1.0:
        raise ValueError("target_share must be in [0, 1]")
    if year < 0 or ramp_years <= 0:
        raise ValueError("year must be non-negative and ramp_years positive")
    progress = min(1.0, year / ramp_years)
    return sam * target_share * progress


def adoption_curve(month: float, *, ceiling: float, rate: float, midpoint: float) -> float:
    """Logistic adoption: slow start, fast middle, saturation.

    ``ceiling`` is the maximum adoptable population, ``rate`` controls the
    steepness, and ``midpoint`` is the month at which half the ceiling is
    reached.  The curve is useful for sanity-checking a ramp, not for
    predicting it.
    """
    if ceiling < 0 or rate <= 0:
        raise ValueError("ceiling must be non-negative and rate positive")
    return ceiling / (1.0 + math.exp(-rate * (month - midpoint)))


def funnel_conversion(visitors: float, rates: Sequence[float]) -> dict[str, float]:
    """Apply a sequence of stage conversion rates to a top-of-funnel count.

    Returns the count and the compound conversion.  Rates must be in [0, 1];
    a rate above 1 is almost always a units mistake (for example, passing a
    percentage such as 20 instead of 0.20).
    """
    if visitors < 0:
        raise ValueError("visitors must be non-negative")
    count = float(visitors)
    for rate in rates:
        if not 0.0 <= rate <= 1.0:
            raise ValueError("conversion rates must be in [0, 1]")
        count *= rate
    compound = count / visitors if visitors else 0.0
    return {"final_count": count, "compound_rate": compound}


def magic_number(new_arr: float, sales_marketing_spend: float) -> float:
    """SaaS magic number: net new ARR divided by sales and marketing spend.

    A value above 1 is usually read as efficient growth; below 0.5 suggests
    the go-to-market engine is not paying for itself.  The ratio is sensitive
    to the measurement window, so state the period with the number.
    """
    if new_arr < 0 or sales_marketing_spend <= 0:
        raise ValueError("new_arr must be non-negative and spend positive")
    return new_arr / sales_marketing_spend


def market_share(revenue: float, market_revenue: float) -> float:
    """Revenue share, with a guard against dividing by zero."""
    if revenue < 0 or market_revenue < 0:
        raise ValueError("revenue values must be non-negative")
    if market_revenue == 0:
        return 0.0
    return revenue / market_revenue


def sales_capacity(
    *,
    reps: int,
    quota_per_rep: float,
    ramp_months: int,
    month: int,
) -> float:
    """Ramped sales capacity at a given month.

    A new rep contributes nothing in month 0 and reaches full quota after
    ``ramp_months``.  Using fully-ramped quota for a hiring plan is one of the
    most common ways to overstate first-year revenue.
    """
    if reps < 0 or quota_per_rep < 0 or ramp_months < 0 or month < 0:
        raise ValueError("capacity inputs must be non-negative")
    if ramp_months == 0:
        return reps * quota_per_rep
    progress = min(1.0, month / ramp_months)
    return reps * quota_per_rep * progress

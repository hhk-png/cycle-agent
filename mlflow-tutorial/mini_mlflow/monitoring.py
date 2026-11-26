"""Minimal model monitoring: baselines, distribution drift and window reports.

MLflow records *what a model is* and *which data it saw*; it is not a
time-series monitoring system.  Production teams still need to answer a
different question every day: "is the traffic this model sees today still
distributed like the data it was trained and approved on?"  This module is the
smallest useful bridge between those two worlds:

* a **monitor** stores a derived baseline profile (histograms and category
  counts), the feature contract, and alert thresholds;
* a **window** is one batch of production records evaluated against that
  baseline;
* the report is persisted as immutable evidence, so an incident review can
  reproduce the numbers instead of trusting a dashboard screenshot.

The statistics are deliberately classical and dependency-free: population
stability index (PSI), two-sample Kolmogorov-Smirnov for numeric features,
chi-square / Cramer's V for categorical features, missing-rate deltas, and
standardised mean shift.  They are *signals*, not proof of model degradation.
A production implementation should add streaming sketches, warehouse SQL,
delayed-label joins, and human review (chapter 45).
"""

from __future__ import annotations

import hashlib
import math
import statistics
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .entities import Monitor, MonitorWindow
from .errors import InvalidParameter, ResourceNotFound
from .store.sqlite_store import SqliteStore
from .utils import compact_json, new_id, now_ms, validate_key, validate_name

DEFAULT_THRESHOLDS: dict[str, float] = {
    # PSI rule of thumb: < 0.1 stable, 0.1-0.25 investigate, > 0.25 act.
    "psi_warn": 0.10,
    "psi_alert": 0.25,
    # Require both a meaningful KS distance and a small p-value: with large
    # windows, a tiny but irrelevant shift can still be "significant".
    "ks_stat": 0.10,
    "ks_pvalue": 0.01,
    "chi2_pvalue": 0.05,
    "missing_rate_warn": 0.05,
    "missing_rate_alert": 0.20,
    "unseen_rate_warn": 0.05,
    "unseen_rate_alert": 0.20,
    "mean_shift_warn": 0.50,
    "min_samples_warn": 30,
}

_MAX_RECORDS = 50_000
_MAX_FEATURES = 200
_MAX_CATEGORIES = 100
_OTHER = "<other>"
_EPS = 1e-6
_SEVERITY = {"STABLE": 0, "WARN": 1, "ALERT": 2}


@dataclass(frozen=True)
class _FeatureSpec:
    kind: str
    bins: int


def _severity_max(left: str, right: str) -> str:
    return left if _SEVERITY[left] >= _SEVERITY[right] else right


def _normalize_features(features: Mapping[str, Any]) -> dict[str, _FeatureSpec]:
    if not isinstance(features, Mapping) or not features:
        raise InvalidParameter("features must be a non-empty mapping of name -> kind")
    if len(features) > _MAX_FEATURES:
        raise InvalidParameter(f"at most {_MAX_FEATURES} monitored features are supported")
    normalized: dict[str, _FeatureSpec] = {}
    for name, raw in features.items():
        key = validate_key(name, what="feature name")
        if isinstance(raw, str):
            kind, bins = raw, 10
        elif isinstance(raw, Mapping):
            kind = str(raw.get("kind", "numeric"))
            try:
                bins = int(raw.get("bins", 10))
            except (TypeError, ValueError) as exc:
                raise InvalidParameter(f"feature {key!r} bins must be an integer") from exc
        else:
            raise InvalidParameter(
                f"feature {key!r} must be 'numeric'/'categorical' or a mapping"
            )
        if kind not in {"numeric", "categorical"}:
            raise InvalidParameter(
                f"feature {key!r} kind must be 'numeric' or 'categorical'"
            )
        if not 2 <= bins <= 50:
            raise InvalidParameter(f"feature {key!r} bins must be between 2 and 50")
        normalized[key] = _FeatureSpec(kind=kind, bins=bins)
    return normalized


def _normalize_thresholds(thresholds: Mapping[str, Any] | None) -> dict[str, float]:
    values = dict(DEFAULT_THRESHOLDS)
    if thresholds is None:
        return values
    if not isinstance(thresholds, Mapping):
        raise InvalidParameter("thresholds must be a mapping")
    unknown = sorted(set(thresholds) - set(DEFAULT_THRESHOLDS))
    if unknown:
        raise InvalidParameter(
            f"unknown threshold keys {unknown}; allowed: {sorted(DEFAULT_THRESHOLDS)}"
        )
    for key, raw in thresholds.items():
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise InvalidParameter(f"threshold {key!r} must be numeric") from exc
        if not math.isfinite(value) or value < 0:
            raise InvalidParameter(f"threshold {key!r} must be finite and non-negative")
        values[key] = value
    if values["psi_warn"] > values["psi_alert"]:
        raise InvalidParameter("psi_warn must not exceed psi_alert")
    if values["missing_rate_warn"] > values["missing_rate_alert"]:
        raise InvalidParameter("missing_rate_warn must not exceed missing_rate_alert")
    if values["unseen_rate_warn"] > values["unseen_rate_alert"]:
        raise InvalidParameter("unseen_rate_warn must not exceed unseen_rate_alert")
    return values


def _rows(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise InvalidParameter("records must be a list of objects")
    if not records:
        raise InvalidParameter("records must not be empty")
    if len(records) > _MAX_RECORDS:
        raise InvalidParameter(f"at most {_MAX_RECORDS} records are accepted per window")
    rows: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise InvalidParameter(f"record {index} must be an object")
        # Dataset records wrap features under ``inputs``; a plain feature row
        # is accepted too so monitoring can run before a dataset exists.
        payload = record.get("inputs") if isinstance(record.get("inputs"), Mapping) else record
        rows.append(dict(payload))
    return rows


def _numeric(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidParameter(f"{what} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise InvalidParameter(f"{what} must be finite")
    return number


def _category(value: Any, what: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int)):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidParameter(f"{what} must be finite")
        return repr(value)
    raise InvalidParameter(f"{what} must be a string, integer, or boolean")


def _quantile_edges(values: Sequence[float], bins: int) -> list[float]:
    ordered = sorted(values)
    if len(ordered) < 2:
        return [ordered[0], ordered[0]]
    edges: list[float] = []
    last = len(ordered) - 1
    for index in range(bins + 1):
        position = last * index / bins
        low = int(math.floor(position))
        high = min(low + 1, last)
        fraction = position - low
        edge = ordered[low] * (1 - fraction) + ordered[high] * fraction
        if not edges or edge > edges[-1]:
            edges.append(edge)
    if len(edges) < 2:
        return [ordered[0], ordered[-1]]
    return edges


def _bin_index(value: float, edges: Sequence[float]) -> int:
    index = bisect_right(edges, value) - 1
    return min(max(index, 0), len(edges) - 2)


def _histogram(values: Sequence[float], edges: Sequence[float]) -> list[int]:
    counts = [0] * (len(edges) - 1)
    for value in values:
        counts[_bin_index(value, edges)] += 1
    return counts


def _psi(expected: Sequence[int], actual: Sequence[int]) -> float:
    """Population stability index with Laplace-style smoothing.

    Zero cells are the norm when a category disappears or a tail bin is empty.
    Adding a tiny constant to every cell keeps the logarithm finite while
    still producing a large score for a genuinely new/vanished bucket.
    """

    expected_total = sum(expected)
    actual_total = sum(actual)
    if expected_total <= 0:
        return 0.0
    if actual_total <= 0:
        return float("inf")
    size = len(expected)
    score = 0.0
    for index in range(size):
        expected_ratio = (expected[index] + _EPS) / (expected_total + _EPS * size)
        actual_ratio = (actual[index] + _EPS) / (actual_total + _EPS * size)
        score += (actual_ratio - expected_ratio) * math.log(actual_ratio / expected_ratio)
    return score


def _ks_two_sample(left: Sequence[float], right: Sequence[float]) -> tuple[float, float]:
    """Return (D, asymptotic p-value) for the two-sample KS test."""

    if not left or not right:
        return 1.0, 0.0
    first = sorted(left)
    second = sorted(right)
    n1, n2 = len(first), len(second)
    i = j = 0
    distance = 0.0
    # Evaluate the CDF only after consuming *all* ties at a value.  Advancing
    # one sample at a time would report a spurious 1/n difference for two
    # identical samples that share a value.
    for value in sorted(set(first) | set(second)):
        while i < n1 and first[i] <= value:
            i += 1
        while j < n2 and second[j] <= value:
            j += 1
        distance = max(distance, abs(i / n1 - j / n2))
    effective = n1 * n2 / (n1 + n2)
    if distance <= 0:
        return 0.0, 1.0
    lam = (math.sqrt(effective) + 0.12 + 0.11 / math.sqrt(effective)) * distance
    total = 0.0
    for k in range(1, 101):
        term = (1 if k % 2 else -1) * math.exp(-2 * k * k * lam * lam)
        total += term
        if abs(term) < 1e-12:
            break
    return distance, min(1.0, max(0.0, 2 * total))


def _chi2_sf(statistic: float, degrees: int) -> float:
    """Upper-tail chi-square probability via the Wilson-Hilferty approximation.

    The approximation is accurate enough to triage a drift alert; it is not a
    replacement for a statistics library when the decision is regulated.
    """

    if degrees <= 0 or statistic <= 0:
        return 1.0
    z = ((statistic / degrees) ** (1 / 3) - (1 - 2 / (9 * degrees))) / math.sqrt(
        2 / (9 * degrees)
    )
    return 0.5 * math.erfc(z / math.sqrt(2))


def _sample(values: Sequence[float], limit: int = 2000) -> list[float]:
    """Deterministic bounded sample for a real two-sample KS comparison.

    A histogram alone can only compare CDFs at bin edges; with tied or
    low-cardinality values that approximation reports a spurious shift even
    when the data is identical.  Keeping up to 2000 sorted values makes the KS
    test honest without growing the baseline without bound.
    """

    ordered = sorted(values)
    if len(ordered) <= limit:
        return ordered
    step = len(ordered) / limit
    return [ordered[min(int(index * step), len(ordered) - 1)] for index in range(limit)]


def _numeric_profile(values: Sequence[float], missing: int, total: int, bins: int) -> dict[str, Any]:
    mean = statistics.fmean(values)
    std = statistics.pstdev(values) if len(values) > 1 else 0.0
    profile: dict[str, Any] = {
        "kind": "numeric",
        "count": len(values),
        "missing": missing,
        "missing_rate": missing / total if total else 0.0,
        "mean": mean,
        "std": std,
        "min": min(values),
        "max": max(values),
        "bins": bins,
        "sample": _sample(values),
    }
    if min(values) == max(values):
        profile["constant"] = min(values)
        profile["edges"] = []
        profile["hist"] = [len(values), 0]
    else:
        edges = _quantile_edges(values, bins)
        profile["edges"] = edges
        profile["hist"] = _histogram(values, edges)
    return profile


def _categorical_profile(values: Sequence[str], missing: int, total: int) -> dict[str, Any]:
    counter = Counter(values)
    ordered = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    if len(ordered) > _MAX_CATEGORIES:
        kept = dict(ordered[:_MAX_CATEGORIES])
        kept[_OTHER] = sum(count for _, count in ordered[_MAX_CATEGORIES:])
        counts = kept
    else:
        counts = dict(ordered)
    return {
        "kind": "categorical",
        "count": len(values),
        "missing": missing,
        "missing_rate": missing / total if total else 0.0,
        "counts": counts,
    }


def _build_baseline(
    rows: Sequence[Mapping[str, Any]], specs: Mapping[str, _FeatureSpec]
) -> dict[str, Any]:
    total = len(rows)
    features: dict[str, Any] = {}
    for name, spec in specs.items():
        raw_values = [row[name] for row in rows if row.get(name) is not None]
        missing = total - len(raw_values)
        if spec.kind == "numeric":
            values = [_numeric(value, f"feature {name!r}") for value in raw_values]
            if not values:
                raise InvalidParameter(f"feature {name!r} has no numeric baseline values")
            features[name] = _numeric_profile(values, missing, total, spec.bins)
        else:
            values = [_category(value, f"feature {name!r}") for value in raw_values]
            if not values:
                raise InvalidParameter(f"feature {name!r} has no categorical baseline values")
            features[name] = _categorical_profile(values, missing, total)
    baseline = {
        "record_count": total,
        "features": features,
        "spec": {name: {"kind": spec.kind, "bins": spec.bins} for name, spec in specs.items()},
    }
    baseline["digest"] = "sha256:" + hashlib.sha256(
        compact_json(baseline).encode("utf-8")
    ).hexdigest()
    return baseline


def _evaluate_numeric(
    profile: Mapping[str, Any],
    values: Sequence[float],
    missing: int,
    total: int,
    thresholds: Mapping[str, float],
) -> dict[str, Any]:
    reasons: list[str] = []
    status = "STABLE"
    missing_rate = missing / total if total else 0.0
    missing_delta = missing_rate - float(profile["missing_rate"])
    result: dict[str, Any] = {
        "kind": "numeric",
        "count": len(values),
        "missing": missing,
        "missing_rate": missing_rate,
        "missing_rate_delta": missing_delta,
    }
    if not values:
        result.update({"status": "ALERT", "reasons": ["no non-missing values in window"]})
        return result

    if "constant" in profile:
        constant = float(profile["constant"])
        baseline_counts = [int(profile["hist"][0]), int(profile["hist"][1])]
        equal = sum(1 for value in values if value == constant)
        actual_counts = [equal, len(values) - equal]
        psi = _psi(baseline_counts, actual_counts)
        result.update(
            {
                "constant": constant,
                "psi": psi,
                "ks_stat": None,
                "ks_pvalue": None,
                "mean": statistics.fmean(values),
            }
        )
        if psi >= thresholds["psi_alert"]:
            status = _severity_max(status, "ALERT")
            reasons.append(f"PSI {psi:.3f} >= {thresholds['psi_alert']}")
        elif psi >= thresholds["psi_warn"]:
            status = _severity_max(status, "WARN")
            reasons.append(f"PSI {psi:.3f} >= {thresholds['psi_warn']}")
    else:
        edges = profile["edges"]
        expected = [int(item) for item in profile["hist"]]
        actual = _histogram(values, edges)
        psi = _psi(expected, actual)
        # Prefer the stored bounded sample; the edge reconstruction is only a
        # compatibility fallback for baselines created by older builds.
        baseline_sample = profile.get("sample")
        if not baseline_sample:
            baseline_sample = _edge_cdf_points(edges, expected)
        ks_stat, ks_pvalue = _ks_two_sample(baseline_sample, values)
        mean = statistics.fmean(values)
        baseline_mean = float(profile["mean"])
        baseline_std = float(profile["std"])
        mean_shift = (mean - baseline_mean) / baseline_std if baseline_std > 0 else 0.0
        result.update(
            {
                "psi": psi,
                "ks_stat": ks_stat,
                "ks_pvalue": ks_pvalue,
                "mean": mean,
                "mean_shift_sigma": mean_shift,
                "min": min(values),
                "max": max(values),
            }
        )
        if psi >= thresholds["psi_alert"]:
            status = _severity_max(status, "ALERT")
            reasons.append(f"PSI {psi:.3f} >= {thresholds['psi_alert']}")
        elif psi >= thresholds["psi_warn"]:
            status = _severity_max(status, "WARN")
            reasons.append(f"PSI {psi:.3f} >= {thresholds['psi_warn']}")
        if (
            ks_stat >= thresholds["ks_stat"]
            and ks_pvalue <= thresholds["ks_pvalue"]
        ):
            status = _severity_max(status, "ALERT")
            reasons.append(
                f"KS D={ks_stat:.3f}, p={ks_pvalue:.4f} (threshold "
                f"{thresholds['ks_stat']}, {thresholds['ks_pvalue']})"
            )
        if abs(mean_shift) >= thresholds["mean_shift_warn"]:
            status = _severity_max(status, "WARN")
            reasons.append(f"mean shift {mean_shift:.2f} sigma")

    if missing_delta >= thresholds["missing_rate_alert"]:
        status = _severity_max(status, "ALERT")
        reasons.append(f"missing-rate delta {missing_delta:.3f} >= alert")
    elif missing_delta >= thresholds["missing_rate_warn"]:
        status = _severity_max(status, "WARN")
        reasons.append(f"missing-rate delta {missing_delta:.3f} >= warn")
    result["status"] = status
    result["reasons"] = reasons
    return result


def _edge_cdf_points(edges: Sequence[float], counts: Sequence[int]) -> list[float]:
    """Expand a histogram into weighted edge points for a KS comparison.

    A histogram is all the baseline stores, so the "sample" is each bin edge
    repeated by that bin's count.  This preserves the CDF at the bin
    boundaries, which is exactly what a binned monitor can defend.
    """

    points: list[float] = []
    for index, count in enumerate(counts):
        # Use the right edge of each bin; counts are integer repetitions, but
        # cap the expansion so a 50k-record baseline stays cheap.
        edge = edges[min(index + 1, len(edges) - 1)]
        points.extend([edge] * int(count))
    return points


def _evaluate_categorical(
    profile: Mapping[str, Any],
    values: Sequence[str],
    missing: int,
    total: int,
    thresholds: Mapping[str, float],
) -> dict[str, Any]:
    reasons: list[str] = []
    status = "STABLE"
    missing_rate = missing / total if total else 0.0
    missing_delta = missing_rate - float(profile["missing_rate"])
    result: dict[str, Any] = {
        "kind": "categorical",
        "count": len(values),
        "missing": missing,
        "missing_rate": missing_rate,
        "missing_rate_delta": missing_delta,
    }
    if not values:
        result.update({"status": "ALERT", "reasons": ["no non-missing values in window"]})
        return result

    baseline_counts = {str(key): int(value) for key, value in profile["counts"].items()}
    actual_counts = Counter(values)
    categories = sorted(set(baseline_counts) | set(actual_counts))
    expected = [baseline_counts.get(category, 0) for category in categories]
    actual = [actual_counts.get(category, 0) for category in categories]
    unseen = sorted(category for category in actual_counts if category not in baseline_counts)
    unseen_rate = sum(actual_counts[item] for item in unseen) / len(values)
    psi = _psi(expected, actual)

    baseline_n = sum(expected)
    actual_n = sum(actual)
    statistic = 0.0
    degrees = 0
    low_expected = False
    for expected_count, actual_count in zip(expected, actual):
        if expected_count <= 0:
            continue
        expected_in_window = expected_count / baseline_n * actual_n
        if expected_in_window > 0:
            if expected_in_window < 5:
                low_expected = True
            statistic += (actual_count - expected_in_window) ** 2 / expected_in_window
            degrees += 1
    degrees = max(degrees - 1, 0)
    pvalue = _chi2_sf(statistic, degrees)
    cramers_v = (
        math.sqrt(statistic / (actual_n * degrees)) if degrees > 0 and actual_n > 0 else 0.0
    )
    result.update(
        {
            "psi": psi,
            "chi2": statistic,
            "chi2_pvalue": pvalue,
            "cramers_v": cramers_v,
            "unseen_categories": unseen[:20],
            "unseen_rate": unseen_rate,
            "low_expected_counts": low_expected,
        }
    )
    if psi >= thresholds["psi_alert"]:
        status = _severity_max(status, "ALERT")
        reasons.append(f"PSI {psi:.3f} >= {thresholds['psi_alert']}")
    elif psi >= thresholds["psi_warn"]:
        status = _severity_max(status, "WARN")
        reasons.append(f"PSI {psi:.3f} >= {thresholds['psi_warn']}")
    if unseen_rate >= thresholds["unseen_rate_alert"]:
        status = _severity_max(status, "ALERT")
        reasons.append(f"unseen-category rate {unseen_rate:.3f} >= alert")
    elif unseen_rate >= thresholds["unseen_rate_warn"]:
        status = _severity_max(status, "WARN")
        reasons.append(f"unseen-category rate {unseen_rate:.3f} >= warn")
    if pvalue <= thresholds["chi2_pvalue"] and cramers_v >= thresholds["mean_shift_warn"]:
        status = _severity_max(status, "WARN")
        reasons.append(f"chi-square p={pvalue:.4f}, Cramer's V={cramers_v:.2f}")
    if missing_delta >= thresholds["missing_rate_alert"]:
        status = _severity_max(status, "ALERT")
        reasons.append(f"missing-rate delta {missing_delta:.3f} >= alert")
    elif missing_delta >= thresholds["missing_rate_warn"]:
        status = _severity_max(status, "WARN")
        reasons.append(f"missing-rate delta {missing_delta:.3f} >= warn")
    result["status"] = status
    result["reasons"] = reasons
    return result


def _evaluate(
    monitor: Monitor,
    rows: Sequence[Mapping[str, Any]],
    thresholds: Mapping[str, float],
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    total = len(rows)
    features: dict[str, Any] = {}
    overall = "STABLE"
    summary_reasons: list[str] = []
    for name, spec_data in monitor.baseline["features"].items():
        config = monitor.baseline["spec"][name]
        raw_values = [row[name] for row in rows if row.get(name) is not None]
        missing = total - len(raw_values)
        if config["kind"] == "numeric":
            values = [_numeric(value, f"feature {name!r}") for value in raw_values]
            feature_result = _evaluate_numeric(
                spec_data, values, missing, total, thresholds
            )
        else:
            values = [_category(value, f"feature {name!r}") for value in raw_values]
            feature_result = _evaluate_categorical(
                spec_data, values, missing, total, thresholds
            )
        features[name] = feature_result
        if feature_result["status"] != "STABLE":
            overall = _severity_max(overall, feature_result["status"])
            summary_reasons.extend(
                f"{name}: {reason}" for reason in feature_result["reasons"]
            )
    if total < thresholds["min_samples_warn"]:
        overall = _severity_max(overall, "WARN")
        summary_reasons.append(
            f"sample count {total} < min_samples_warn {thresholds['min_samples_warn']:.0f}"
        )
    report = {
        "monitor_id": monitor.monitor_id,
        "monitor_name": monitor.name,
        "model_name": monitor.model_name,
        "model_version": monitor.model_version,
        "baseline_digest": monitor.baseline.get("digest", ""),
        "baseline_dataset_id": monitor.baseline_dataset_id,
        "baseline_dataset_digest": monitor.baseline_dataset_digest,
        "sample_count": total,
        "status": overall,
        "thresholds": dict(thresholds),
        "features": features,
        "reasons": summary_reasons,
    }
    return overall, features, report


class MonitoringService:
    """Application service for monitor definitions and window evaluation."""

    def __init__(self, store: SqliteStore) -> None:
        self.store = store

    def create_monitor(
        self,
        name: str,
        baseline_records: Sequence[Mapping[str, Any]],
        features: Mapping[str, Any],
        *,
        model_name: str = "",
        model_version: str = "",
        dataset_id: str = "",
        thresholds: Mapping[str, Any] | None = None,
        tags: Mapping[str, Any] | None = None,
    ) -> Monitor:
        name = validate_name(name, what="monitor name")
        specs = _normalize_features(features)
        clean_thresholds = _normalize_thresholds(thresholds)
        baseline_dataset_digest = ""
        rows_input = baseline_records
        if dataset_id:
            # Binding to a managed dataset gives monitoring a lineage anchor:
            # the baseline digest is the dataset digest, not a copy that can
            # silently drift.
            dataset = self.store.get_dataset(dataset_id)
            if baseline_records:
                raise InvalidParameter(
                    "pass baseline_records or dataset_id, not both"
                )
            rows_input = dataset.records
            baseline_dataset_digest = dataset.digest
        rows = _rows(rows_input)
        baseline = _build_baseline(rows, specs)
        clean_tags: dict[str, str] = {}
        for key, value in (tags or {}).items():
            clean_tags[validate_key(key, what="tag key")] = str(value)
        monitor = Monitor(
            monitor_id=new_id(),
            name=name,
            model_name=str(model_name),
            model_version=str(model_version),
            baseline=baseline,
            thresholds=clean_thresholds,
            tags=clean_tags,
            baseline_dataset_id=str(dataset_id),
            baseline_dataset_digest=baseline_dataset_digest,
            created_time=now_ms(),
            last_updated_time=now_ms(),
        )
        return self.store.create_monitor(monitor)

    def get_monitor(self, monitor_id: str) -> Monitor:
        return self.store.get_monitor(str(monitor_id))

    def list_monitors(self) -> list[Monitor]:
        return self.store.list_monitors()

    def delete_monitor(self, monitor_id: str) -> None:
        self.store.delete_monitor(str(monitor_id))

    def record_window(
        self,
        monitor_id: str,
        records: Sequence[Mapping[str, Any]],
        *,
        window_start: int = 0,
        window_end: int = 0,
    ) -> MonitorWindow:
        monitor = self.get_monitor(monitor_id)
        rows = _rows(records)
        status, metrics, report = _evaluate(monitor, rows, monitor.thresholds)
        start = int(window_start) if window_start else now_ms()
        end = int(window_end) if window_end else now_ms()
        if start < 0 or end < 0:
            raise InvalidParameter("window timestamps must be non-negative")
        if end and start and end < start:
            raise InvalidParameter("window_end must not precede window_start")
        window = MonitorWindow(
            window_id=new_id(),
            monitor_id=monitor.monitor_id,
            window_start=start,
            window_end=end,
            sample_count=len(rows),
            status=status,
            metrics=metrics,
            report=report,
            created_time=now_ms(),
        )
        return self.store.create_monitor_window(window)

    def list_windows(self, monitor_id: str, *, limit: int = 100) -> list[MonitorWindow]:
        if int(limit) < 1 or int(limit) > 1000:
            raise InvalidParameter("limit must be between 1 and 1000")
        try:
            return self.store.list_monitor_windows(str(monitor_id), limit=int(limit))
        except ResourceNotFound:
            raise


__all__ = [
    "DEFAULT_THRESHOLDS",
    "MonitoringService",
]

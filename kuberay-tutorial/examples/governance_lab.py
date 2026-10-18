#!/usr/bin/env python3
"""Data and model governance: point-in-time features, lineage and promotion gates.

Run from the tutorial root:

    python examples/governance_lab.py

KubeRay schedules compute; it does not answer "which data trained this model and
is that data still compatible with serving?".  This lab models the minimum
governance layer a platform team needs:

* dataset versions with schemas and content hashes;
* point-in-time feature joins (no future leakage);
* training/serving schema compatibility (skew detection);
* model promotion gates and an audit trail;
* rollback to the previous production model.

Everything is deterministic and runnable without a feature store or registry.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def hash_rows(rows: list[dict], fields: dict[str, str]) -> str:
    # The schema is part of the identity: the same rows under a different
    # contract are a different dataset version.
    schema_part = ",".join(f"{name}:{kind}" for name, kind in sorted(fields.items()))
    payload = schema_part + "|" + "|".join(
        f"{row['user_id']},{row['event_time']},{row['amount']:.2f}" for row in rows
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


@dataclass
class Schema:
    fields: dict[str, str]

    def compatible_with(self, other: "Schema") -> bool:
        return all(other.fields.get(name) == kind for name, kind in self.fields.items())

    def differences(self, other: "Schema") -> list[str]:
        return [
            f"{name}: expected {kind}, got {other.fields.get(name)}"
            for name, kind in self.fields.items()
            if other.fields.get(name) != kind
        ]


@dataclass
class DatasetVersion:
    name: str
    version: str
    rows: list[dict]
    schema: Schema
    created_at: str = "2026-01-10T00:00:00Z"

    @property
    def content_hash(self) -> str:
        return hash_rows(self.rows, self.schema.fields)


@dataclass
class FeatureView:
    name: str
    dataset: DatasetVersion
    window_days: int = 7
    version: str = "v1"

    def point_in_time(self, user_id: int, as_of: str) -> float:
        """Sum only events in ``(as_of - window, as_of]``."""
        end = parse_time(as_of)
        start = end - timedelta(days=self.window_days)
        total = 0.0
        for row in self.dataset.rows:
            if row["user_id"] != user_id:
                continue
            event_time = parse_time(row["event_time"])
            if start < event_time <= end:
                total += float(row["amount"])
        return round(total, 2)

    def lineage(self) -> dict:
        return {
            "feature_view": f"{self.name}@{self.version}",
            "dataset": f"{self.dataset.name}@{self.dataset.version}",
            "content_hash": self.dataset.content_hash,
            "window_days": self.window_days,
        }


@dataclass
class ModelVersion:
    name: str
    version: str
    auc: float
    dataset: DatasetVersion
    feature_view: FeatureView
    training_schema: Schema
    stage: str = "Staging"
    promoted_from: str | None = None
    notes: str = ""


class Registry:
    def __init__(self) -> None:
        self.models: list[ModelVersion] = []
        self.production: str | None = None
        self.previous_production: str | None = None
        self.audit: list[dict] = []

    def add(self, model: ModelVersion) -> None:
        self.models.append(model)
        self.audit.append({"action": "register", "model": f"{model.name}:{model.version}", "stage": model.stage})

    def get(self, version: str) -> ModelVersion:
        for model in self.models:
            if model.version == version:
                return model
        raise KeyError(version)

    def promotion_gates(self, model: ModelVersion, serving_schema: Schema) -> list[str]:
        failures = []
        if model.auc < 0.80:
            failures.append(f"auc {model.auc:.3f} < 0.80")
        if not model.dataset.content_hash:
            failures.append("dataset content hash is missing")
        if not model.training_schema.compatible_with(serving_schema):
            failures.append("training/serving skew: " + "; ".join(model.training_schema.differences(serving_schema)))
        if model.feature_view.dataset.name != model.dataset.name:
            failures.append("feature view and training dataset disagree")
        return failures

    def promote(self, version: str, serving_schema: Schema) -> tuple[bool, list[str]]:
        model = self.get(version)
        failures = self.promotion_gates(model, serving_schema)
        if failures:
            self.audit.append({"action": "promote-denied", "model": f"{model.name}:{version}", "reasons": failures})
            return False, failures
        if self.production and self.production != version:
            self.previous_production = self.production
            self.get(self.production).stage = "Archived"
        self.production = version
        model.stage = "Production"
        self.audit.append({"action": "promote", "model": f"{model.name}:{version}", "stage": "Production"})
        return True, []

    def rollback(self) -> str | None:
        if not self.previous_production:
            return None
        current = self.production
        if current:
            self.get(current).stage = "Archived"
        self.production = self.previous_production
        self.get(self.production).stage = "Production"
        self.audit.append({
            "action": "rollback",
            "from": f"{current}",
            "to": f"{self.production}",
        })
        self.previous_production = None
        return self.production


def main() -> int:
    schema_v1 = Schema({"user_id": "int", "event_time": "timestamp", "amount": "float"})
    schema_v2 = Schema({"user_id": "int", "event_time": "timestamp", "amount": "float", "currency": "string"})

    rows = [
        {"user_id": 1, "event_time": "2026-01-01T10:00:00Z", "amount": 10.0},
        {"user_id": 1, "event_time": "2026-01-05T10:00:00Z", "amount": 25.0},
        {"user_id": 1, "event_time": "2026-01-09T10:00:00Z", "amount": 7.5},
        {"user_id": 2, "event_time": "2026-01-06T10:00:00Z", "amount": 99.0},
        # Future event: must never appear in a feature computed as of 2026-01-09.
        {"user_id": 1, "event_time": "2026-01-09T12:00:00Z", "amount": 999.0},
    ]
    dataset_v1 = DatasetVersion("user_events", "v1", rows, schema_v1)
    dataset_v2 = DatasetVersion("user_events", "v2", rows, schema_v2)
    view = FeatureView("user_spend_7d", dataset_v1, window_days=7, version="v1")

    as_of = "2026-01-09T10:00:00Z"
    feature = view.point_in_time(1, as_of)
    full_sum = round(sum(row["amount"] for row in rows if row["user_id"] == 1), 2)
    print("== governance lab ==")
    print(f"dataset v1 hash   : {dataset_v1.content_hash} ({len(rows)} rows)")
    print(f"dataset v2 hash   : {dataset_v2.content_hash} (adds currency)")
    print(f"features as of    : {as_of} -> user 1 spend 7d = {feature}")
    print(f"full input sum    : {full_sum} (including the future 999.0 event)")
    print(f"lineage           : {view.lineage()}")

    # Window is (2026-01-02T10:00, 2026-01-09T10:00]: the Jan 1 event is too
    # old and the 999 event is in the future, so only 25 + 7.5 remain.
    assert feature == 32.5, feature
    assert feature < full_sum

    # A model trained on v1, served with the v2 schema: additive change is fine.
    print("\n-- schema compatibility --")
    print(f"v1 -> v2 compatible: {schema_v1.compatible_with(schema_v2)}")
    broken_serving = Schema({"user_id": "int", "event_time": "timestamp", "amount": "string"})
    print(f"v1 -> broken compatible: {schema_v1.compatible_with(broken_serving)}")
    assert schema_v1.compatible_with(schema_v2) is True
    assert schema_v1.compatible_with(broken_serving) is False

    registry = Registry()
    registry.add(ModelVersion("spend-model", "m1", 0.81, dataset_v1, view, schema_v1, notes="baseline"))
    registry.add(ModelVersion("spend-model", "m2", 0.78, dataset_v1, view, schema_v1, notes="underfits"))
    registry.add(ModelVersion("spend-model", "m3", 0.84, dataset_v2, FeatureView("user_spend_7d", dataset_v2), schema_v2, notes="new feature"))
    registry.add(ModelVersion("spend-model", "m4", 0.83, dataset_v2, FeatureView("user_spend_7d", dataset_v2), schema_v2, notes="fixed serving"))

    ok1, why1 = registry.promote("m1", schema_v1)
    ok2, why2 = registry.promote("m2", schema_v1)
    ok3, why3 = registry.promote("m3", broken_serving)
    ok4, why4 = registry.promote("m4", schema_v2)
    rolled = registry.rollback()

    print("\n-- promotion gates --")
    print(f"m1 -> Production : {ok1} {why1}")
    print(f"m2 -> Production : {ok2} {why2}")
    print(f"m3 -> Production : {ok3} {why3}")
    print(f"m4 -> Production : {ok4} {why4}")
    print(f"rollback         : {rolled} (production is now {registry.production})")

    assert ok1 is True and why1 == []
    assert ok2 is False and "auc" in why2[0]
    assert ok3 is False and any("skew" in reason for reason in why3)
    assert ok4 is True and why4 == []
    assert rolled == "m1"
    assert registry.get("m4").stage == "Archived"
    assert registry.get("m1").stage == "Production"
    assert [entry["action"] for entry in registry.audit].count("promote-denied") == 2

    print("\n-- audit trail --")
    for entry in registry.audit:
        print(f"  {entry}")

    print("\nOK: point-in-time features excluded future data, skew blocked a bad model, rollback restored m1.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Typed domain objects and their wire representations.

The internal representation uses dictionaries for parameters/tags because the
access patterns are key/value lookups.  The wire representation mirrors
MLflow's list-of-``{key, value}`` shape so that readers familiar with the real
REST API can map concepts one-to-one.

Every ``from_dict`` is deliberately permissive about list-vs-mapping input:
the server and local client then share one object model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from .errors import InvalidParameter
from .utils import now_ms, pairs_to_list

_PROMPT_VARIABLE_RE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")


def _prompt_variables(template: str) -> list[str]:
    """Return variables in first-appearance order (duplicates removed)."""
    seen: set[str] = set()
    ordered: list[str] = []
    for match in _PROMPT_VARIABLE_RE.finditer(template):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


@dataclass
class PromptVersion:
    """One immutable prompt template revision.

    Templates use ``{{ variable }}`` placeholders.  Rendering is a deliberate
    string substitution, not ``eval``/``str.format``: a value containing
    braces cannot alter the template or reach Python internals.
    """

    name: str
    version: int
    template: str
    commit_message: str = ""
    creation_timestamp: int = field(default_factory=now_ms)
    tags: dict[str, str] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)

    @property
    def variables(self) -> list[str]:
        return _prompt_variables(self.template)

    def format(self, **variables: Any) -> str:
        declared = self.variables
        missing = [name for name in declared if name not in variables]
        if missing:
            raise InvalidParameter(f"missing prompt variables: {missing}")
        extra = sorted(set(variables) - set(declared))
        if extra:
            raise InvalidParameter(f"unexpected prompt variables: {extra}")

        def replace(match: re.Match[str]) -> str:
            value = variables[match.group(1)]
            if value is None:
                raise InvalidParameter(f"prompt variable {match.group(1)!r} is None")
            return str(value)

        return _PROMPT_VARIABLE_RE.sub(replace, self.template)

    def to_single_brace_format(self) -> str:
        """Convert ``{{name}}`` to ``{name}`` for callers using str.format.

        Literal braces outside placeholders are doubled, so a template that
        embeds JSON remains valid under ``str.format``.  Prefer :meth:`format`,
        which avoids format specifiers and attribute traversal entirely.
        """
        result: list[str] = []
        position = 0
        for match in _PROMPT_VARIABLE_RE.finditer(self.template):
            literal = self.template[position : match.start()]
            result.append(literal.replace("{", "{{").replace("}", "}}"))
            result.append("{" + match.group(1) + "}")
            position = match.end()
        tail = self.template[position:]
        result.append(tail.replace("{", "{{").replace("}", "}}"))
        return "".join(result)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": str(self.version),
            "template": self.template,
            "commit_message": self.commit_message,
            "creation_timestamp": self.creation_timestamp,
            "tags": pairs_to_list(self.tags),
            "aliases": list(self.aliases),
            "variables": self.variables,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PromptVersion":
        aliases = data.get("aliases") or []
        if isinstance(aliases, dict):
            aliases = list(aliases)
        return cls(
            name=str(data["name"]),
            version=int(data.get("version", 0)),
            template=str(data.get("template", "")),
            commit_message=str(data.get("commit_message") or ""),
            creation_timestamp=int(data.get("creation_timestamp", 0)),
            tags=_tags_dict(data.get("tags")),
            aliases=[str(item) for item in aliases],
        )


@dataclass
class Assessment:
    """A feedback or expectation attached to a trace.

    ``value`` is intentionally restricted to JSON scalars.  Structured
    judgments belong in ``metadata`` so that search and aggregation stay
    predictable.  ``is_expectation`` separates "what a human wants" from
    "what a judge observed".
    """

    assessment_id: str
    trace_id: str
    name: str
    value: Any
    source_type: str = "HUMAN"
    source_id: str = ""
    rationale: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    is_expectation: bool = False
    valid: bool = True
    create_time: int = field(default_factory=now_ms)
    last_update_time: int = field(default_factory=now_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "assessment_id": self.assessment_id,
            "trace_id": self.trace_id,
            "name": self.name,
            "value": self.value,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "rationale": self.rationale,
            "metadata": dict(self.metadata),
            "is_expectation": self.is_expectation,
            "valid": self.valid,
            "create_time": self.create_time,
            "last_update_time": self.last_update_time,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Assessment":
        source = data.get("source") or {}
        if not isinstance(source, dict):
            source = {}
        return cls(
            assessment_id=str(data.get("assessment_id", "")),
            trace_id=str(data.get("trace_id", "")),
            name=str(data.get("name", "")),
            value=data.get("value"),
            source_type=str(data.get("source_type", source.get("source_type", "HUMAN"))),
            source_id=str(data.get("source_id", source.get("source_id", ""))),
            rationale=str(data.get("rationale") or ""),
            metadata=dict(data.get("metadata") or {}),
            is_expectation=bool(data.get("is_expectation", False)),
            valid=bool(data.get("valid", True)),
            create_time=int(data.get("create_time", 0)),
            last_update_time=int(data.get("last_update_time", 0)),
        )


class RunStatus(str, Enum):
    RUNNING = "RUNNING"
    SCHEDULED = "SCHEDULED"
    FINISHED = "FINISHED"
    FAILED = "FAILED"
    KILLED = "KILLED"


class LifecycleStage(str, Enum):
    ACTIVE = "active"
    DELETED = "deleted"


class ModelVersionStatus(str, Enum):
    PENDING = "PENDING_REGISTRATION"
    READY = "READY"
    FAILED = "FAILED_REGISTRATION"


_TERMINAL_RUN_STATUSES = {
    RunStatus.FINISHED.value,
    RunStatus.FAILED.value,
    RunStatus.KILLED.value,
}


def _tags_dict(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    if isinstance(value, list):
        result: dict[str, str] = {}
        for item in value:
            if isinstance(item, dict) and "key" in item:
                result[str(item["key"])] = str(item.get("value", ""))
        return result
    return {}


@dataclass
class Metric:
    key: str
    value: float
    timestamp: int
    step: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "value": self.value, "timestamp": self.timestamp, "step": self.step}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Metric":
        return cls(
            key=str(data["key"]),
            value=float(data["value"]),
            timestamp=int(data.get("timestamp", 0)),
            step=int(data.get("step", 0)),
        )


@dataclass
class Experiment:
    experiment_id: str
    name: str
    artifact_location: str
    lifecycle_stage: str = LifecycleStage.ACTIVE.value
    creation_time: int = field(default_factory=now_ms)
    last_update_time: int = field(default_factory=now_ms)
    tags: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "name": self.name,
            "artifact_location": self.artifact_location,
            "lifecycle_stage": self.lifecycle_stage,
            "creation_time": self.creation_time,
            "last_update_time": self.last_update_time,
            "tags": pairs_to_list(self.tags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Experiment":
        return cls(
            experiment_id=str(data["experiment_id"]),
            name=str(data["name"]),
            artifact_location=str(data.get("artifact_location", "")),
            lifecycle_stage=str(data.get("lifecycle_stage", "active")),
            creation_time=int(data.get("creation_time", 0)),
            last_update_time=int(data.get("last_update_time", 0)),
            tags=_tags_dict(data.get("tags")),
        )


@dataclass
class RunInfo:
    run_id: str
    experiment_id: str
    run_name: str = ""
    user_id: str = ""
    status: str = RunStatus.RUNNING.value
    start_time: int = field(default_factory=now_ms)
    end_time: int | None = None
    artifact_uri: str = ""
    lifecycle_stage: str = LifecycleStage.ACTIVE.value

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_RUN_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "experiment_id": self.experiment_id,
            "run_name": self.run_name,
            "user_id": self.user_id,
            "status": self.status,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "artifact_uri": self.artifact_uri,
            "lifecycle_stage": self.lifecycle_stage,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunInfo":
        return cls(
            run_id=str(data["run_id"]),
            experiment_id=str(data["experiment_id"]),
            run_name=str(data.get("run_name") or ""),
            user_id=str(data.get("user_id") or ""),
            status=str(data.get("status", "RUNNING")),
            start_time=int(data.get("start_time", 0)),
            end_time=int(data["end_time"]) if data.get("end_time") is not None else None,
            artifact_uri=str(data.get("artifact_uri") or ""),
            lifecycle_stage=str(data.get("lifecycle_stage", "active")),
        )


@dataclass
class RunData:
    metrics: dict[str, float] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metrics": [
                {"key": key, "value": value, "timestamp": 0, "step": 0}
                for key, value in sorted(self.metrics.items())
            ],
            "params": pairs_to_list(self.params),
            "tags": pairs_to_list(self.tags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunData":
        metrics: dict[str, float] = {}
        raw_metrics = data.get("metrics") or []
        if isinstance(raw_metrics, dict):
            metrics = {str(k): float(v) for k, v in raw_metrics.items()}
        else:
            for item in raw_metrics:
                if isinstance(item, dict) and "key" in item:
                    metrics[str(item["key"])] = float(item.get("value", 0.0))
        params = data.get("params") or {}
        tags = data.get("tags") or {}
        if isinstance(params, list):
            params = _tags_dict(params)
        if isinstance(tags, list):
            tags = _tags_dict(tags)
        return cls(metrics=metrics, params={str(k): str(v) for k, v in params.items()}, tags={str(k): str(v) for k, v in tags.items()})


@dataclass
class RunInput:
    dataset_name: str
    dataset_digest: str = ""
    dataset_source: str = ""
    dataset_source_type: str = ""
    context: str = "training"
    tags: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": {
                "name": self.dataset_name,
                "digest": self.dataset_digest,
                "source": self.dataset_source,
                "source_type": self.dataset_source_type,
            },
            "context": self.context,
            "tags": pairs_to_list(self.tags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunInput":
        dataset = data.get("dataset", {})
        return cls(
            dataset_name=str(dataset.get("name", "")),
            dataset_digest=str(dataset.get("digest", "")),
            dataset_source=str(dataset.get("source", "")),
            dataset_source_type=str(dataset.get("source_type", "")),
            context=str(data.get("context", "training")),
            tags=_tags_dict(data.get("tags")),
        )


@dataclass
class Dataset:
    """A versioned evaluation dataset: metadata plus reviewable records.

    A run input (``RunInput``) is a *pointer* recorded on a training run.  A
    dataset is a first-class asset: it has its own identity, content digest,
    schema/profile and an append-only record history.  Records are plain JSON
    objects with three well-known sections::

        {"inputs": {...}, "expectations": {...}, "tags": {...}}

    ``version`` increments when records are merged; ``digest`` covers the
    complete record set, so two datasets with the same records and the same
    order produce the same digest on any machine.
    """

    dataset_id: str
    name: str
    version: int = 1
    digest: str = ""
    source: str = ""
    source_type: str = ""
    schema: dict[str, str] = field(default_factory=dict)
    profile: dict[str, Any] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)
    records: list[dict[str, Any]] = field(default_factory=list)
    record_count: int = 0
    created_time: int = field(default_factory=now_ms)
    last_updated_time: int = field(default_factory=now_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "name": self.name,
            "version": str(self.version),
            "digest": self.digest,
            "source": self.source,
            "source_type": self.source_type,
            "schema": dict(self.schema),
            "profile": dict(self.profile),
            "tags": pairs_to_list(self.tags),
            "records": list(self.records),
            "record_count": self.record_count,
            "created_time": self.created_time,
            "last_updated_time": self.last_updated_time,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Dataset":
        records = data.get("records") or []
        if not isinstance(records, list):
            raise InvalidParameter("dataset records must be a list")
        return cls(
            dataset_id=str(data["dataset_id"]),
            name=str(data.get("name", "")),
            version=int(data.get("version", 1)),
            digest=str(data.get("digest", "")),
            source=str(data.get("source", "")),
            source_type=str(data.get("source_type", "")),
            schema={str(key): str(value) for key, value in (data.get("schema") or {}).items()},
            profile=dict(data.get("profile") or {}),
            tags=_tags_dict(data.get("tags")),
            records=[dict(record) for record in records],
            record_count=int(data.get("record_count", len(records))),
            created_time=int(data.get("created_time", 0)),
            last_updated_time=int(data.get("last_updated_time", 0)),
        )


@dataclass
class Monitor:
    """A model-monitoring definition bound to a baseline distribution.

    MLflow records what a model is and which data it saw; it deliberately does
    not own time-series monitoring or alerting.  A monitor is the bridge: it
    stores a compact baseline profile (histograms and category counts), the
    feature contract, and the thresholds used to classify a later window as
    stable, warning, or alert.  The baseline is derived from data, not typed by
    hand, so it cannot silently disagree with the reference sample.
    """

    monitor_id: str
    name: str
    model_name: str = ""
    model_version: str = ""
    baseline: dict[str, Any] = field(default_factory=dict)
    thresholds: dict[str, float] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)
    baseline_dataset_id: str = ""
    baseline_dataset_digest: str = ""
    created_time: int = field(default_factory=now_ms)
    last_updated_time: int = field(default_factory=now_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "monitor_id": self.monitor_id,
            "name": self.name,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "baseline": dict(self.baseline),
            "thresholds": dict(self.thresholds),
            "tags": pairs_to_list(self.tags),
            "baseline_dataset_id": self.baseline_dataset_id,
            "baseline_dataset_digest": self.baseline_dataset_digest,
            "created_time": self.created_time,
            "last_updated_time": self.last_updated_time,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Monitor":
        thresholds = data.get("thresholds") or {}
        if not isinstance(thresholds, dict):
            raise InvalidParameter("monitor thresholds must be an object")
        return cls(
            monitor_id=str(data["monitor_id"]),
            name=str(data.get("name", "")),
            model_name=str(data.get("model_name", "")),
            model_version=str(data.get("model_version", "")),
            baseline=dict(data.get("baseline") or {}),
            thresholds={str(key): float(value) for key, value in thresholds.items()},
            tags=_tags_dict(data.get("tags")),
            baseline_dataset_id=str(data.get("baseline_dataset_id", "")),
            baseline_dataset_digest=str(data.get("baseline_dataset_digest", "")),
            created_time=int(data.get("created_time", 0)),
            last_updated_time=int(data.get("last_updated_time", 0)),
        )


@dataclass
class MonitorWindow:
    """One evaluated batch for a monitor.

    ``metrics`` contains the per-feature statistics; ``report`` is the compact
    audit object (thresholds, reasons, sample count) that can be logged as an
    artifact or attached to a run.  Storing both is intentional: dashboards
    read the narrow metrics, while incident review needs the full explanation.
    """

    window_id: str
    monitor_id: str
    window_start: int
    window_end: int
    sample_count: int
    status: str = "STABLE"
    metrics: dict[str, Any] = field(default_factory=dict)
    report: dict[str, Any] = field(default_factory=dict)
    created_time: int = field(default_factory=now_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "monitor_id": self.monitor_id,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "sample_count": self.sample_count,
            "status": self.status,
            "metrics": dict(self.metrics),
            "report": dict(self.report),
            "created_time": self.created_time,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MonitorWindow":
        return cls(
            window_id=str(data["window_id"]),
            monitor_id=str(data["monitor_id"]),
            window_start=int(data.get("window_start", 0)),
            window_end=int(data.get("window_end", 0)),
            sample_count=int(data.get("sample_count", 0)),
            status=str(data.get("status", "STABLE")),
            metrics=dict(data.get("metrics") or {}),
            report=dict(data.get("report") or {}),
            created_time=int(data.get("created_time", 0)),
        )


@dataclass
class Run:
    info: RunInfo
    data: RunData = field(default_factory=RunData)
    inputs: list[RunInput] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "info": self.info.to_dict(),
            "data": self.data.to_dict(),
            "inputs": [item.to_dict() for item in self.inputs],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Run":
        return cls(
            info=RunInfo.from_dict(data["info"]),
            data=RunData.from_dict(data.get("data") or {}),
            inputs=[RunInput.from_dict(item) for item in (data.get("inputs") or [])],
        )


class LoggedModelStatus(str, Enum):
    """Wire values used by the MLflow 3 REST API.

    The Python enum in real MLflow is shorter (``READY``, ``PENDING``,
    ``UPLOAD_FAILED``); the protobuf/REST spelling is the one persisted here
    because clients send it in ``PATCH /logged-models/{id}``.
    """

    PENDING = "LOGGED_MODEL_PENDING"
    READY = "LOGGED_MODEL_READY"
    FAILED = "LOGGED_MODEL_UPLOAD_FAILED"


_LOGGED_MODEL_STATUS_ALIASES = {
    "PENDING": LoggedModelStatus.PENDING.value,
    "READY": LoggedModelStatus.READY.value,
    "FAILED": LoggedModelStatus.FAILED.value,
    "UPLOAD_FAILED": LoggedModelStatus.FAILED.value,
    "LOGGED_MODEL_PENDING": LoggedModelStatus.PENDING.value,
    "LOGGED_MODEL_READY": LoggedModelStatus.READY.value,
    "LOGGED_MODEL_UPLOAD_FAILED": LoggedModelStatus.FAILED.value,
}


def normalize_logged_model_status(value: Any) -> str:
    """Accept both the protobuf spelling and the short Python enum name."""
    key = str(getattr(value, "value", value) or "").strip().upper()
    if key not in _LOGGED_MODEL_STATUS_ALIASES:
        raise InvalidParameter(
            "status must be one of LOGGED_MODEL_PENDING, LOGGED_MODEL_READY, "
            "LOGGED_MODEL_UPLOAD_FAILED (short aliases PENDING/READY/FAILED are accepted)"
        )
    return _LOGGED_MODEL_STATUS_ALIASES[key]


@dataclass
class LoggedModel:
    """A model artifact owned by an experiment (MLflow 3 model-centric API).

    ``model_id`` is the identity and the load URI is ``models:/<model_id>``.
    ``source_run_id`` is lineage, not ownership: deleting the run must not
    delete the model, which is why the schema uses ``ON DELETE SET NULL``.
    Params are immutable, tags are mutable, metrics keep the latest value per
    key (the full history lives in the metrics table).
    """

    model_id: str
    experiment_id: str
    name: str
    artifact_location: str
    creation_timestamp: int = field(default_factory=now_ms)
    last_updated_timestamp: int = field(default_factory=now_ms)
    model_type: str = ""
    source_run_id: str | None = None
    status: str = LoggedModelStatus.PENDING.value
    status_message: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)
    metrics: dict[str, Metric] = field(default_factory=dict)

    @property
    def model_uri(self) -> str:
        return f"models:/{self.model_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "info": {
                "model_id": self.model_id,
                "experiment_id": self.experiment_id,
                "name": self.name,
                "model_type": self.model_type,
                "source_run_id": self.source_run_id,
                "status": self.status,
                "status_message": self.status_message,
                "artifact_location": self.artifact_location,
                "creation_timestamp": self.creation_timestamp,
                "last_updated_timestamp": self.last_updated_timestamp,
            },
            "data": {
                "tags": pairs_to_list(self.tags),
                "params": pairs_to_list(self.params),
                "metrics": [
                    self.metrics[key].to_dict() for key in sorted(self.metrics)
                ],
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LoggedModel":
        info = data.get("info") if isinstance(data.get("info"), dict) else data
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        raw_metrics = payload.get("metrics", data.get("metrics")) or []
        metrics: dict[str, Metric] = {}
        if isinstance(raw_metrics, dict):
            for key, value in raw_metrics.items():
                metrics[str(key)] = Metric(str(key), float(value), 0, 0)
        else:
            for item in raw_metrics:
                if isinstance(item, dict) and "key" in item:
                    metric = Metric.from_dict(item)
                    metrics[metric.key] = metric
        return cls(
            model_id=str(info["model_id"]),
            experiment_id=str(info.get("experiment_id", "0")),
            name=str(info.get("name", "")),
            artifact_location=str(info.get("artifact_location", "")),
            creation_timestamp=int(info.get("creation_timestamp", 0)),
            last_updated_timestamp=int(info.get("last_updated_timestamp", 0)),
            model_type=str(info.get("model_type") or ""),
            source_run_id=(
                str(info["source_run_id"]) if info.get("source_run_id") is not None else None
            ),
            status=str(info.get("status", LoggedModelStatus.PENDING.value)),
            status_message=str(info.get("status_message") or ""),
            tags=_tags_dict(payload.get("tags", data.get("tags"))),
            params=_tags_dict(payload.get("params", data.get("params"))),
            metrics=metrics,
        )


@dataclass
class FileInfo:
    path: str
    is_dir: bool = False
    file_size: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "is_dir": self.is_dir, "file_size": self.file_size}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FileInfo":
        return cls(
            path=str(data["path"]),
            is_dir=bool(data.get("is_dir", False)),
            file_size=int(data.get("file_size", 0)),
        )


@dataclass
class RegisteredModel:
    name: str
    creation_timestamp: int = field(default_factory=now_ms)
    last_updated_timestamp: int = field(default_factory=now_ms)
    description: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    aliases: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "creation_timestamp": self.creation_timestamp,
            "last_updated_timestamp": self.last_updated_timestamp,
            "description": self.description,
            "tags": pairs_to_list(self.tags),
            "aliases": [{"alias": k, "version": str(v)} for k, v in sorted(self.aliases.items())],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RegisteredModel":
        aliases: dict[str, int] = {}
        raw_aliases = data.get("aliases") or {}
        if isinstance(raw_aliases, dict):
            aliases = {str(k): int(v) for k, v in raw_aliases.items()}
        else:
            for item in raw_aliases:
                if isinstance(item, dict) and "alias" in item:
                    aliases[str(item["alias"])] = int(item.get("version", 0))
        return cls(
            name=str(data["name"]),
            creation_timestamp=int(data.get("creation_timestamp", 0)),
            last_updated_timestamp=int(data.get("last_updated_timestamp", 0)),
            description=str(data.get("description") or ""),
            tags=_tags_dict(data.get("tags")),
            aliases=aliases,
        )


@dataclass
class ModelVersion:
    name: str
    version: int
    creation_timestamp: int = field(default_factory=now_ms)
    last_updated_timestamp: int = field(default_factory=now_ms)
    description: str = ""
    source: str = ""
    run_id: str = ""
    status: str = ModelVersionStatus.READY.value
    stage: str = "None"
    tags: dict[str, str] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": str(self.version),
            "creation_timestamp": self.creation_timestamp,
            "last_updated_timestamp": self.last_updated_timestamp,
            "description": self.description,
            "source": self.source,
            "run_id": self.run_id,
            "status": self.status,
            "current_stage": self.stage,
            "tags": pairs_to_list(self.tags),
            "aliases": list(self.aliases),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelVersion":
        aliases = data.get("aliases") or []
        if isinstance(aliases, dict):
            aliases = list(aliases)
        return cls(
            name=str(data["name"]),
            version=int(data.get("version", 0)),
            creation_timestamp=int(data.get("creation_timestamp", 0)),
            last_updated_timestamp=int(data.get("last_updated_timestamp", 0)),
            description=str(data.get("description") or ""),
            source=str(data.get("source") or ""),
            run_id=str(data.get("run_id") or ""),
            status=str(data.get("status", "READY")),
            stage=str(data.get("current_stage", data.get("stage", "None")) or "None"),
            tags=_tags_dict(data.get("tags")),
            aliases=[str(item) for item in aliases],
        )


@dataclass
class Webhook:
    """A registry event subscription.

    The secret is stored with the webhook but never returned by ``to_dict``:
    event receivers need it to verify HMAC signatures, while API readers do
    not need the raw value.  A production implementation should encrypt it at
    rest (see chapter 38).
    """

    webhook_id: str
    name: str
    url: str
    events: list[str] = field(default_factory=list)
    secret: str = ""
    description: str = ""
    status: str = "ACTIVE"
    creation_timestamp: int = field(default_factory=now_ms)
    last_updated_timestamp: int = field(default_factory=now_ms)

    def to_dict(self, *, include_secret: bool = False) -> dict[str, Any]:
        payload = {
            "webhook_id": self.webhook_id,
            "name": self.name,
            "url": self.url,
            "events": list(self.events),
            "description": self.description,
            "status": self.status,
            "secret_set": bool(self.secret),
            "creation_timestamp": self.creation_timestamp,
            "last_updated_timestamp": self.last_updated_timestamp,
        }
        if include_secret:
            payload["secret"] = self.secret
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Webhook":
        events = data.get("events") or []
        if isinstance(events, str):
            events = [events]
        return cls(
            webhook_id=str(data.get("webhook_id", "")),
            name=str(data.get("name", "")),
            url=str(data.get("url", "")),
            events=[str(item) for item in events],
            secret=str(data.get("secret") or ""),
            description=str(data.get("description") or ""),
            status=str(data.get("status", "ACTIVE")),
            creation_timestamp=int(data.get("creation_timestamp", 0)),
            last_updated_timestamp=int(data.get("last_updated_timestamp", 0)),
        )


@dataclass
class WebhookDelivery:
    """One outbox row for at-least-once webhook delivery."""

    delivery_id: str
    webhook_id: str
    event: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "PENDING"
    attempts: int = 0
    last_error: str = ""
    response_status: int | None = None
    created_time: int = field(default_factory=now_ms)
    next_attempt_time: int = field(default_factory=now_ms)
    delivered_time: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "webhook_id": self.webhook_id,
            "event": self.event,
            "payload": dict(self.payload),
            "status": self.status,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "response_status": self.response_status,
            "created_time": self.created_time,
            "next_attempt_time": self.next_attempt_time,
            "delivered_time": self.delivered_time,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WebhookDelivery":
        return cls(
            delivery_id=str(data["delivery_id"]),
            webhook_id=str(data["webhook_id"]),
            event=str(data.get("event", "")),
            payload=dict(data.get("payload") or {}),
            status=str(data.get("status", "PENDING")),
            attempts=int(data.get("attempts", 0)),
            last_error=str(data.get("last_error") or ""),
            response_status=(
                int(data["response_status"])
                if data.get("response_status") is not None
                else None
            ),
            created_time=int(data.get("created_time", 0)),
            next_attempt_time=int(data.get("next_attempt_time", 0)),
            delivered_time=(
                int(data["delivered_time"])
                if data.get("delivered_time") is not None
                else None
            ),
        )


@dataclass
class Span:
    span_id: str
    trace_id: str
    name: str
    parent_span_id: str | None = None
    span_type: str = "UNKNOWN"
    start_time_ns: int = 0
    end_time_ns: int | None = None
    status: str = "OK"
    inputs: Any = None
    outputs: Any = None
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def latency_ms(self) -> float | None:
        if self.end_time_ns is None:
            return None
        return (self.end_time_ns - self.start_time_ns) / 1_000_000

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "trace_id": self.trace_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "span_type": self.span_type,
            "start_time_ns": self.start_time_ns,
            "end_time_ns": self.end_time_ns,
            "status": self.status,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "attributes": self.attributes,
            "events": self.events,
            "latency_ms": self.latency_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Span":
        return cls(
            span_id=str(data["span_id"]),
            trace_id=str(data.get("trace_id", "")),
            parent_span_id=str(data["parent_span_id"]) if data.get("parent_span_id") else None,
            name=str(data.get("name", "span")),
            span_type=str(data.get("span_type", "UNKNOWN")),
            start_time_ns=int(data.get("start_time_ns", 0)),
            end_time_ns=int(data["end_time_ns"]) if data.get("end_time_ns") is not None else None,
            status=str(data.get("status", "OK")),
            inputs=data.get("inputs"),
            outputs=data.get("outputs"),
            attributes=dict(data.get("attributes") or {}),
            events=list(data.get("events") or []),
        )


@dataclass
class Trace:
    trace_id: str
    name: str = "trace"
    experiment_id: str = "0"
    timestamp: int = field(default_factory=now_ms)
    status: str = "OK"
    tags: dict[str, str] = field(default_factory=dict)
    request_metadata: dict[str, str] = field(default_factory=dict)
    request: Any = None
    response: Any = None
    spans: list[Span] = field(default_factory=list)
    assessments: list[Assessment] = field(default_factory=list)

    @property
    def latency_ms(self) -> float:
        if not self.spans:
            return 0.0
        starts = [span.start_time_ns for span in self.spans]
        ends = [span.end_time_ns for span in self.spans if span.end_time_ns is not None]
        if not starts or not ends:
            return 0.0
        return (max(ends) - min(starts)) / 1_000_000

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "name": self.name,
            "experiment_id": self.experiment_id,
            "timestamp": self.timestamp,
            "status": self.status,
            "tags": pairs_to_list(self.tags),
            "request_metadata": pairs_to_list(self.request_metadata),
            "request": self.request,
            "response": self.response,
            "latency_ms": self.latency_ms,
            "spans": [span.to_dict() for span in self.spans],
            "assessments": [item.to_dict() for item in self.assessments],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Trace":
        return cls(
            trace_id=str(data["trace_id"]),
            name=str(data.get("name", "trace")),
            experiment_id=str(data.get("experiment_id", "0")),
            timestamp=int(data.get("timestamp", 0)),
            status=str(data.get("status", "OK")),
            tags=_tags_dict(data.get("tags")),
            request_metadata=_tags_dict(data.get("request_metadata")),
            request=data.get("request"),
            response=data.get("response"),
            spans=[Span.from_dict(item) for item in (data.get("spans") or [])],
            assessments=[Assessment.from_dict(item) for item in (data.get("assessments") or [])],
        )


def run_statuses() -> Iterable[str]:
    return tuple(status.value for status in RunStatus)

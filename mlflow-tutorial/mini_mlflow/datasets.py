"""Evaluation dataset registry.

A *run input* records which data a training run pointed at.  A *dataset* is a
different object: a versioned, reviewable collection of evaluation samples
with its own identity, content digest, schema and profile.  The distinction
matters because evaluation data must be curated (redacted, labelled, pinned)
and re-used across model versions.

The implementation is deliberately small but keeps the invariants that make a
dataset trustworthy:

* ``dataset_id`` is the identity; names are searchable labels, not keys;
* each record is an immutable row, stored as ``{inputs, expectations, tags}``;
* merging appends records and increments ``version`` in one transaction;
* ``digest`` is SHA-256 over the canonical JSON of the complete record set, so
  the same records in the same order hash identically across machines;
* schema/profile are derived, not user-supplied, so they cannot silently
  disagree with the records.

Production gaps are analysed in chapter 44: object-store backing, record-level
patching, dataset lineage to runs, PII redaction pipelines, licensing and
retention enforcement.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any, Callable, Mapping, Sequence

from .config import MiniConfig
from .entities import Dataset
from .errors import Conflict, InvalidParameter, ResourceNotFound, SearchSyntaxError
from .search import Field, evaluate_ast, parse_filter
from .store.sqlite_store import SqliteStore
from .utils import (
    coerce_tag_value,
    compact_json,
    new_id,
    now_ms,
    parse_pairs,
    validate_key,
    validate_name,
)

LOGGER = logging.getLogger("mini_mlflow.datasets")

_MAX_RECORDS_PER_MERGE = 10_000
_MAX_TOTAL_RECORDS = 50_000
_MAX_RECORD_BYTES = 1024 * 1024
_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_MAX_SOURCE_BYTES = 4096
_ALLOWED_RECORD_KEYS = {"inputs", "expectations", "tags"}
_ORDER_FIELDS = {"name", "version", "created_time", "last_updated_time", "record_count"}
_ORDER_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_.\-]*)(?:\s+(ASC|DESC))?", re.IGNORECASE
)


def _clean_text(value: Any, what: str, *, max_bytes: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InvalidParameter(f"{what} must be a string")
    if len(value.encode("utf-8")) > max_bytes:
        raise InvalidParameter(f"{what} exceeds {max_bytes} bytes")
    if any(ord(char) < 32 for char in value):
        raise InvalidParameter(f"{what} must not contain control characters")
    return value


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "long"
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _merge_types(types: set[str]) -> str:
    non_null = types - {"null"}
    if not non_null:
        return "null"
    if non_null <= {"long", "double"}:
        return "double" if "double" in non_null or len(non_null) > 1 else "long"
    if len(non_null) == 1:
        return next(iter(non_null))
    return "mixed"


def _schema_and_profile(records: Sequence[dict[str, Any]]) -> tuple[dict[str, str], dict[str, Any]]:
    types: dict[str, set[str]] = {}
    null_counts: dict[str, int] = {}
    expectation_keys: set[str] = set()
    for record in records:
        for key, value in record["inputs"].items():
            types.setdefault(key, set()).add(_json_type(value))
            if value is None:
                null_counts[key] = null_counts.get(key, 0) + 1
        expectation_keys.update(record["expectations"])
    schema = {key: _merge_types(types[key]) for key in sorted(types)}
    profile = {
        "record_count": len(records),
        "input_keys": sorted(types),
        "expectation_keys": sorted(expectation_keys),
        "null_inputs": {key: null_counts[key] for key in sorted(null_counts)},
    }
    return schema, profile


def _digest(records: Sequence[dict[str, Any]]) -> str:
    payload = compact_json(list(records)).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _total_bytes(records: Sequence[dict[str, Any]]) -> int:
    return sum(len(compact_json(record).encode("utf-8")) for record in records)


def _clean_record(record: Any, index: int) -> tuple[dict[str, Any], int]:
    if not isinstance(record, dict):
        raise InvalidParameter(f"record {index} must be an object")
    unknown = sorted(set(record) - _ALLOWED_RECORD_KEYS)
    if unknown:
        raise InvalidParameter(
            f"record {index} has unsupported fields {unknown}; "
            "allowed fields are inputs, expectations, tags"
        )
    inputs = record.get("inputs")
    if not isinstance(inputs, dict) or not inputs:
        raise InvalidParameter(f"record {index} inputs must be a non-empty object")
    expectations = record.get("expectations", {})
    if expectations is None:
        expectations = {}
    if not isinstance(expectations, dict):
        raise InvalidParameter(f"record {index} expectations must be an object")
    tags = record.get("tags", {})
    if tags is None:
        tags = {}
    if not isinstance(tags, dict):
        raise InvalidParameter(f"record {index} tags must be an object")
    for key in inputs:
        if not isinstance(key, str) or not key:
            raise InvalidParameter(f"record {index} input keys must be non-empty strings")
    for key in expectations:
        if not isinstance(key, str) or not key:
            raise InvalidParameter(f"record {index} expectation keys must be non-empty strings")
    clean_tags = {
        validate_key(key, what="record tag key"): coerce_tag_value(value)
        for key, value in tags.items()
    }
    cleaned = {
        "inputs": dict(inputs),
        "expectations": dict(expectations),
        "tags": clean_tags,
    }
    try:
        encoded = json.dumps(
            cleaned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise InvalidParameter(
            f"record {index} must be JSON-serializable without NaN/Infinity "
            "and must not be nested too deeply"
        ) from exc
    size = len(encoded.encode("utf-8"))
    if size > _MAX_RECORD_BYTES:
        raise InvalidParameter(
            f"record {index} is {size} bytes, exceeding {_MAX_RECORD_BYTES}"
        )
    return cleaned, size


def _clean_records(records: Any, *, start: int = 0) -> list[dict[str, Any]]:
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise InvalidParameter("records must be a list of objects")
    if not records:
        raise InvalidParameter("records must not be empty")
    if len(records) > _MAX_RECORDS_PER_MERGE:
        raise InvalidParameter(
            f"a single create/merge supports at most {_MAX_RECORDS_PER_MERGE} records"
        )
    if len(records) + start > _MAX_TOTAL_RECORDS:
        raise InvalidParameter(
            f"a dataset supports at most {_MAX_TOTAL_RECORDS} records"
        )
    cleaned: list[dict[str, Any]] = []
    total_bytes = 0
    for index, record in enumerate(records):
        item, size = _clean_record(record, index)
        total_bytes += size
        cleaned.append(item)
    if total_bytes > _MAX_TOTAL_BYTES:
        raise InvalidParameter(
            f"records are {total_bytes} bytes, exceeding {_MAX_TOTAL_BYTES}"
        )
    return cleaned


class DatasetRegistry:
    """Business rules for evaluation datasets.

    The store owns SQL and row conversion.  This layer owns naming, record
    validation, digest/schema derivation, search semantics and event emission.
    """

    def __init__(
        self,
        store: SqliteStore,
        *,
        config: MiniConfig | None = None,
        emit: Callable[[str, Mapping[str, Any]], Any] | None = None,
    ) -> None:
        self.store = store
        self.config = config or MiniConfig.from_env()
        self._emit_callback = emit

    def _emit(self, event: str, data: Mapping[str, Any]) -> None:
        if self._emit_callback is None:
            return
        try:
            self._emit_callback(event, dict(data))
        except Exception:  # noqa: BLE001 - notification boundary
            LOGGER.exception("failed to enqueue webhook event %s", event)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------
    def create_dataset(
        self,
        name: str,
        records: Sequence[Mapping[str, Any]],
        *,
        source: str = "",
        source_type: str = "",
        tags: Mapping[str, Any] | None = None,
    ) -> Dataset:
        name = validate_name(name, what="dataset name", max_len=255)
        source = _clean_text(source, "dataset source", max_bytes=_MAX_SOURCE_BYTES)
        source_type = _clean_text(source_type, "dataset source_type", max_bytes=128)
        clean_tags = {
            validate_key(key, what="dataset tag key"): coerce_tag_value(value)
            for key, value in parse_pairs(
                tags, what="dataset tags", stringify=False
            ).items()
        }
        cleaned = _clean_records(records)
        schema, profile = _schema_and_profile(cleaned)
        timestamp = now_ms()
        dataset = Dataset(
            dataset_id="d-" + new_id(),
            name=name,
            version=1,
            digest=_digest(cleaned),
            source=source,
            source_type=source_type,
            schema=schema,
            profile=profile,
            tags=clean_tags,
            records=cleaned,
            record_count=len(cleaned),
            created_time=timestamp,
            last_updated_time=timestamp,
        )
        stored = self.store.create_dataset(dataset)
        self._emit(
            "dataset.created",
            {
                "dataset_id": stored.dataset_id,
                "name": stored.name,
                "version": str(stored.version),
                "digest": stored.digest,
                "record_count": stored.record_count,
            },
        )
        return stored

    def merge_records(
        self, dataset_id: str, records: Sequence[Mapping[str, Any]]
    ) -> Dataset:
        dataset_id = self._dataset_id(dataset_id)
        # Merging is read-compute-write.  The store validates that the dataset
        # has not changed since our read; a concurrent merge makes the derived
        # digest/schema stale, so we retry from a fresh snapshot rather than
        # committing metadata that does not describe the rows on disk.
        for attempt in range(8):
            existing = self.store.get_dataset(dataset_id)
            cleaned = _clean_records(records, start=existing.record_count)
            merged = existing.records + cleaned
            if _total_bytes(merged) > _MAX_TOTAL_BYTES:
                raise InvalidParameter(
                    f"dataset would exceed {_MAX_TOTAL_BYTES} bytes"
                )
            schema, profile = _schema_and_profile(merged)
            new_version = existing.version + 1
            new_digest = _digest(merged)
            try:
                stored = self.store.merge_dataset_records(
                    dataset_id,
                    cleaned,
                    digest=new_digest,
                    version=new_version,
                    schema=schema,
                    profile=profile,
                    updated=now_ms(),
                    expected_version=existing.version,
                )
            except Conflict:
                if attempt == 7:
                    raise
                time.sleep(0.005 * (attempt + 1))
                continue
            self._emit(
                "dataset.records.merged",
                {
                    "dataset_id": dataset_id,
                    "name": existing.name,
                    "version": str(new_version),
                    "digest": new_digest,
                    "added_records": len(cleaned),
                    "record_count": len(merged),
                },
            )
            return stored
        raise Conflict(f"dataset {dataset_id!r} could not be merged after retries")

    def delete_dataset(self, dataset_id: str) -> None:
        dataset_id = self._dataset_id(dataset_id)
        dataset = self.store.get_dataset(dataset_id)
        self.store.delete_dataset(dataset_id)
        self._emit(
            "dataset.deleted",
            {"dataset_id": dataset.dataset_id, "name": dataset.name},
        )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def get_dataset(self, dataset_id: str) -> Dataset:
        return self.store.get_dataset(self._dataset_id(dataset_id))

    def search_datasets(
        self,
        filter_string: str | None = None,
        *,
        max_results: int = 100,
        order_by: Sequence[str] | None = None,
    ) -> list[Dataset]:
        if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1:
            raise InvalidParameter("max_results must be a positive integer")
        if max_results > self.config.search_max_results:
            raise InvalidParameter(
                f"max_results cannot exceed {self.config.search_max_results}"
            )
        if order_by is not None and (
            isinstance(order_by, (str, bytes))
            or not isinstance(order_by, Sequence)
            or any(not isinstance(item, str) for item in order_by)
        ):
            raise InvalidParameter("order_by must be a sequence of strings")
        ast = parse_filter(filter_string)
        datasets = self.store.list_datasets()
        if ast is not None:
            filtered: list[Dataset] = []
            for dataset in datasets:
                def resolver(field: Field, item: Dataset = dataset) -> Any:
                    if field.scope == "attributes":
                        key = field.key.lower()
                        mapping = {
                            "dataset_id": item.dataset_id,
                            "name": item.name,
                            "version": item.version,
                            "digest": item.digest,
                            "source": item.source,
                            "source_type": item.source_type,
                            "record_count": item.record_count,
                            "created_time": item.created_time,
                            "last_updated_time": item.last_updated_time,
                        }
                        if key not in mapping:
                            raise SearchSyntaxError(
                                f"unknown dataset attribute {field.key!r}"
                            )
                        return mapping[key]
                    if field.scope == "tags":
                        return item.tags.get(field.key)
                    raise SearchSyntaxError(
                        "dataset filters support attributes.* and tags.* only"
                    )

                if evaluate_ast(ast, resolver):
                    filtered.append(dataset)
            datasets = filtered
        if order_by:
            for raw in reversed(list(order_by)):
                text = str(raw).strip()
                match = _ORDER_RE.fullmatch(text)
                if not match:
                    raise InvalidParameter(f"invalid dataset order_by entry {raw!r}")
                key = match.group(1).removeprefix("attributes.")
                if key not in _ORDER_FIELDS:
                    raise InvalidParameter(f"cannot order datasets by {match.group(1)!r}")
                datasets.sort(
                    key=lambda item, attr=key: getattr(item, attr),
                    reverse=(match.group(2) or "ASC").upper() == "DESC",
                )
        else:
            datasets.sort(key=lambda item: (item.name.lower(), item.name, item.created_time))
        return datasets[:max_results]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _dataset_id(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise InvalidParameter("dataset_id must be a non-empty string")
        return value.strip()

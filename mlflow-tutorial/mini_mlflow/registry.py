"""Model Registry business rules.

The registry is a metadata service, not a file server.  A model version points
at a *source* (typically ``runs:/<run_id>/<artifact_path>``); the artifact
store remains the source of truth for bytes.  That separation is what makes
aliases and rollbacks cheap: promoting a version changes one row, not a copy
of a 10 GB model.

MLflow's stages (Staging/Production/Archived) are kept for compatibility, but
aliases are the better primitive because they are many-to-one, explicit, and
deployment systems can resolve ``models:/name@champion`` without guessing.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from typing import Any, Callable, Mapping, Sequence

from .entities import ModelVersion, RegisteredModel
from .errors import Conflict, InvalidParameter, ResourceNotFound, SearchSyntaxError
from .search import Field, evaluate_ast, parse_filter
from .store.sqlite_store import SqliteStore
from .utils import validate_key, coerce_tag_value, validate_name, is_id

LOGGER = logging.getLogger("mini_mlflow.registry")

_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-/ ]{0,254}$")
_ALIAS_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-.]{0,127}$")
_STAGES = {"none": "None", "staging": "Staging", "production": "Production", "archived": "Archived"}
_SEARCH_MAX = 5000


def validate_model_name(name: str) -> str:
    value = validate_name(name, what="registered model name", max_len=255)
    if not _MODEL_NAME_RE.fullmatch(value) or ".." in value or value.endswith("/"):
        raise InvalidParameter(
            "registered model names must start with a letter or digit and contain only "
            "letters, digits, '_', '-', '.', '/' or spaces; '..' is not allowed"
        )
    return value


def validate_alias(alias: str) -> str:
    if not isinstance(alias, str) or not _ALIAS_RE.fullmatch(alias):
        raise InvalidParameter(
            "aliases must start with a letter or underscore and contain only letters, "
            "digits, '_', '-', '.' (max 128 characters)"
        )
    if alias.isdigit():
        raise InvalidParameter("an alias cannot be a bare number (it would be ambiguous with a version)")
    return alias


def canonical_stage(stage: str | None) -> str:
    key = (stage or "None").strip().lower()
    if key not in _STAGES:
        raise InvalidParameter("stage must be one of None, Staging, Production, Archived")
    return _STAGES[key]


class ModelRegistry:
    def __init__(
        self,
        store: SqliteStore,
        *,
        emit: Callable[[str, Mapping[str, Any]], Any] | None = None,
    ) -> None:
        self.store = store
        self._emit_callback = emit

    def _emit(self, event: str, data: Mapping[str, Any]) -> None:
        """Best-effort event publication.

        A notification failure must not turn a successful registry mutation
        into an error: the mutation is already committed.  Production should
        write the outbox row in the same transaction as the mutation; Mini
        documents this window in chapter 38.
        """
        if self._emit_callback is None:
            return
        try:
            self._emit_callback(event, dict(data))
        except Exception:  # noqa: BLE001 - notification boundary
            LOGGER.exception("failed to enqueue webhook event %s", event)

    # ------------------------------------------------------------------
    # Registered models
    # ------------------------------------------------------------------
    def create_registered_model(
        self,
        name: str,
        *,
        description: str = "",
        tags: dict[str, str] | None = None,
    ) -> RegisteredModel:
        name = validate_model_name(name)
        clean_tags = {validate_key(key, what="tag key"): coerce_tag_value(value) for key, value in (tags or {}).items()}
        model = self.store.create_registered_model(
            name, description=str(description or ""), tags=clean_tags
        )
        self._emit(
            "registered_model.created",
            {
                "name": model.name,
                "description": model.description,
                "tags": dict(model.tags),
            },
        )
        return model

    def get_registered_model(self, name: str) -> RegisteredModel:
        return self.store.get_registered_model(name)

    def rename_registered_model(self, name: str, new_name: str) -> RegisteredModel:
        new_name = validate_model_name(new_name)
        model = self.store.update_registered_model(name, new_name=new_name)
        self._emit(
            "registered_model.renamed",
            {"old_name": name, "new_name": model.name},
        )
        return model

    def update_registered_model(self, name: str, *, description: str | None = None) -> RegisteredModel:
        return self.store.update_registered_model(name, description=description)

    def delete_registered_model(self, name: str, *, force: bool = False) -> None:
        versions = self.store.list_model_versions(name)
        if versions and not force:
            raise InvalidParameter(
                f"registered model {name!r} still has {len(versions)} version(s); "
                "pass force=true after archiving the model explicitly"
            )
        self.store.delete_registered_model(name)
        self._emit("registered_model.deleted", {"name": name, "force": bool(force)})

    def set_registered_model_tag(self, name: str, key: str, value: Any) -> None:
        self.store.set_registered_model_tag(name, validate_key(key, what="tag key"), coerce_tag_value(value))

    def delete_registered_model_tag(self, name: str, key: str) -> None:
        self.store.delete_registered_model_tag(name, key)

    def search_registered_models(
        self,
        filter_string: str | None = None,
        *,
        max_results: int = 1000,
        order_by: Sequence[str] | None = None,
    ) -> list[RegisteredModel]:
        self._validate_max(max_results)
        ast = parse_filter(filter_string)
        models = self.store.list_registered_models()

        if ast is not None:
            filtered: list[RegisteredModel] = []
            for model in models:
                def model_resolver(field: Field, m: RegisteredModel = model) -> Any:
                    if field.scope == "attributes":
                        key = field.key.lower()
                        mapping = {
                            "name": m.name,
                            "creation_timestamp": m.creation_timestamp,
                            "last_updated_timestamp": m.last_updated_timestamp,
                            "description": m.description,
                        }
                        if key not in mapping:
                            raise SearchSyntaxError(f"unknown registered-model attribute {field.key!r}")
                        return mapping[key]
                    if field.scope == "tags":
                        return m.tags.get(field.key)
                    raise SearchSyntaxError("registered-model filters support attributes.* and tags.* only")

                if evaluate_ast(ast, model_resolver):
                    filtered.append(model)
            models = filtered
        return self._order_registered_models(models, order_by)[:max_results]

    def _order_registered_models(
        self, models: list[RegisteredModel], order_by: Sequence[str] | None
    ) -> list[RegisteredModel]:
        if not order_by:
            return sorted(models, key=lambda item: (item.name.lower(), item.name))
        ordered = list(models)
        # Stable multi-key sort: apply from the last key to the first.
        for raw in reversed(list(order_by)):
            field_name, direction = self._split_order(raw)
            key = field_name.removeprefix("attributes.")
            if key not in {"name", "creation_timestamp", "last_updated_timestamp"}:
                raise SearchSyntaxError(f"cannot order registered models by {field_name!r}")
            reverse = direction == "DESC"
            ordered.sort(key=lambda item, key=key: getattr(item, key), reverse=reverse)
        return ordered

    # ------------------------------------------------------------------
    # Model versions
    # ------------------------------------------------------------------
    def create_model_version(
        self,
        name: str,
        *,
        source: str,
        run_id: str = "",
        description: str = "",
        tags: dict[str, str] | None = None,
    ) -> ModelVersion:
        if not name:
            raise InvalidParameter("registered model name is required")
        source = str(source or "").strip()
        if not source:
            raise InvalidParameter("model version source is required")
        if run_id:
            if not isinstance(run_id, str) or not is_id(run_id):
                raise InvalidParameter("run_id must be a 32-character hexadecimal identifier")
            # Validate lineage early; a dangling run_id is almost always a bug.
            self.store.get_run(run_id, include_deleted=False)
        clean_tags = {validate_key(key, what="tag key"): coerce_tag_value(value) for key, value in (tags or {}).items()}
        last_error: Exception | None = None
        for _ in range(5):
            try:
                version = self.store.create_model_version(
                    name,
                    source=source,
                    run_id=run_id,
                    description=str(description or ""),
                    tags=clean_tags,
                )
                self._emit(
                    "model_version.created",
                    {
                        "name": version.name,
                        "version": str(version.version),
                        "source": version.source,
                        "run_id": version.run_id,
                        "description": version.description,
                        "tags": dict(version.tags),
                    },
                )
                return version
            except sqlite3.IntegrityError as exc:
                # Concurrent MAX(version)+1 allocation lost the race. Retry;
                # the primary key (name, version) is the arbiter.
                last_error = exc
                time.sleep(0.01)
        raise Conflict(f"could not allocate a model version for {name!r} after retries") from last_error

    def get_model_version(self, name: str, version: int | str) -> ModelVersion:
        return self.store.get_model_version(name, version)

    def get_latest_versions(self, name: str, *, stages: Sequence[str] | None = None) -> list[ModelVersion]:
        """Latest version per stage (all stages when ``stages`` is omitted)."""
        wanted = {canonical_stage(stage) for stage in stages} if stages is not None else None
        latest: dict[str, ModelVersion] = {}
        for version in self.store.list_model_versions(name):
            if wanted is not None and version.stage not in wanted:
                continue
            current = latest.get(version.stage)
            if current is None or version.version > current.version:
                latest[version.stage] = version
        return sorted(latest.values(), key=lambda item: item.stage)

    def update_model_version(self, name: str, version: int, *, description: str | None = None) -> ModelVersion:
        return self.store.update_model_version(name, version, description=description)

    def set_model_version_tag(self, name: str, version: int, key: str, value: Any) -> None:
        clean_key = validate_key(key, what="tag key")
        clean_value = coerce_tag_value(value)
        self.store.set_model_version_tag(name, version, clean_key, clean_value)
        self._emit(
            "model_version_tag.set",
            {
                "name": name,
                "version": str(version),
                "key": clean_key,
                "value": clean_value,
            },
        )

    def delete_model_version_tag(self, name: str, version: int, key: str) -> None:
        self.store.delete_model_version_tag(name, version, key)
        self._emit(
            "model_version_tag.deleted",
            {"name": name, "version": str(version), "key": key},
        )

    def transition_model_version_stage(self, name: str, version: int, stage: str) -> ModelVersion:
        clean_stage = canonical_stage(stage)
        updated = self.store.update_model_version_stage(name, version, clean_stage)
        self._emit(
            "model_version.transitioned_stage",
            {"name": name, "version": str(version), "stage": clean_stage},
        )
        return updated

    def delete_model_version(self, name: str, version: int) -> None:
        self.store.delete_model_version(name, version)
        self._emit(
            "model_version.deleted",
            {"name": name, "version": str(version)},
        )

    def set_registered_model_alias(self, name: str, alias: str, version: int) -> None:
        clean_alias = validate_alias(alias)
        self.store.set_model_alias(name, clean_alias, int(version))
        self._emit(
            "model_version_alias.created",
            {"name": name, "alias": clean_alias, "version": str(version)},
        )

    def delete_registered_model_alias(self, name: str, alias: str) -> None:
        self.store.delete_model_alias(name, alias)
        self._emit(
            "model_version_alias.deleted",
            {"name": name, "alias": alias},
        )

    def get_model_version_by_alias(self, name: str, alias: str) -> ModelVersion:
        return self.store.get_model_version_by_alias(name, alias)

    def get_latest_version(self, name: str) -> ModelVersion:
        versions = self.store.list_model_versions(name)
        if not versions:
            raise ResourceNotFound(f"registered model {name!r} has no versions")
        return versions[0]

    def search_model_versions(
        self,
        filter_string: str | None = None,
        *,
        name: str | None = None,
        max_results: int = 1000,
        order_by: Sequence[str] | None = None,
    ) -> list[ModelVersion]:
        self._validate_max(max_results)
        versions = self.store.list_model_versions(name) if name else self.store.list_all_model_versions()
        ast = parse_filter(filter_string)
        if ast is not None:
            filtered: list[ModelVersion] = []
            for version in versions:
                def resolver(field: Field, v: ModelVersion = version) -> Any:
                    if field.scope == "attributes":
                        key = field.key.lower()
                        mapping = {
                            "name": v.name,
                            "version": v.version,
                            "run_id": v.run_id,
                            "source": v.source,
                            "status": v.status,
                            "current_stage": v.stage,
                            "stage": v.stage,
                            "description": v.description,
                            "creation_timestamp": v.creation_timestamp,
                            "last_updated_timestamp": v.last_updated_timestamp,
                        }
                        if key not in mapping:
                            raise SearchSyntaxError(f"unknown model-version attribute {field.key!r}")
                        return mapping[key]
                    if field.scope == "tags":
                        return v.tags.get(field.key)
                    raise SearchSyntaxError("model-version filters support attributes.* and tags.* only")

                if evaluate_ast(ast, resolver):
                    filtered.append(version)
            versions = filtered
        return self._order_versions(versions, order_by)[:max_results]

    def _order_versions(
        self, versions: list[ModelVersion], order_by: Sequence[str] | None
    ) -> list[ModelVersion]:
        if not order_by:
            return sorted(versions, key=lambda item: (item.name.lower(), -item.version))
        ordered = list(versions)
        for raw in reversed(list(order_by)):
            field_name, direction = self._split_order(raw)
            key = field_name.removeprefix("attributes.")
            if key not in {
                "name",
                "version",
                "creation_timestamp",
                "last_updated_timestamp",
                "status",
                "current_stage",
            }:
                raise SearchSyntaxError(f"cannot order model versions by {field_name!r}")
            reverse = direction == "DESC"
            target = "stage" if key == "current_stage" else key
            ordered.sort(key=lambda item, target=target: getattr(item, target), reverse=reverse)
        return ordered

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_max(max_results: int) -> None:
        if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= _SEARCH_MAX:
            raise InvalidParameter(f"max_results must be between 1 and {_SEARCH_MAX}")

    @staticmethod
    def _split_order(raw: str) -> tuple[str, str]:
        text = str(raw).strip()
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_.\-]*)(?:\s+(ASC|DESC))?", text, re.IGNORECASE)
        if not match:
            raise SearchSyntaxError(f"invalid order_by entry: {raw!r}")
        return match.group(1), (match.group(2) or "ASC").upper()

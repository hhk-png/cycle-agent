"""Tracking and registry service: the use-case layer.

Ordering of responsibilities:

1. HTTP/client validation gives fast, user-facing errors.
2. This service enforces business invariants (immutable params, run state,
   aliases, ownership of artifact namespaces).
3. ``SqliteStore`` enforces storage invariants (foreign keys, uniqueness,
   atomic batches).

Keeping validation in layers means a programmatic caller cannot bypass a rule
that only the HTTP handler knows about.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .artifacts import ArtifactRepository
from .config import MiniConfig
from .datasets import DatasetRegistry
from .entities import (
    Assessment,
    Experiment,
    LoggedModel,
    LoggedModelStatus,
    Metric,
    ModelVersion,
    RegisteredModel,
    Run,
    RunInput,
    RunStatus,
    Trace,
    normalize_logged_model_status,
)
from .errors import InvalidParameter, ResourceAlreadyExists, ResourceNotFound, SearchSyntaxError
from .models import ModelInfo, save_model
from .monitoring import MonitoringService
from .prompts import PromptRegistry
from .registry import ModelRegistry
from .search import (
    Field,
    compile_experiment_filter,
    compile_experiment_order,
    compile_logged_model_filter,
    compile_logged_model_order,
    compile_run_filter,
    compile_run_order,
    evaluate_ast,
    parse_filter,
)
from .store.sqlite_store import SqliteStore
from .webhooks import WebhookService
from .utils import (
    coerce_metric,
    coerce_param_value,
    coerce_tag_value,
    compact_json,
    decode_page_token,
    encode_page_token,
    file_uri_to_path,
    is_id,
    is_logged_model_id,
    new_id,
    new_logged_model_id,
    now_ms,
    parse_pairs,
    path_to_file_uri,
    redact,
    validate_identifier,
    validate_key,
    validate_metric_step,
    validate_name,
    validate_timestamp,
)

_ALLOWED_END_STATUSES = {RunStatus.FINISHED.value, RunStatus.FAILED.value, RunStatus.KILLED.value}
_MAX_METRIC_HISTORY = 100_000
_TRACE_SCAN_BATCH = 5000


class TrackingService:
    def __init__(
        self,
        config: MiniConfig | None = None,
        *,
        store: SqliteStore | None = None,
        artifacts: ArtifactRepository | None = None,
    ) -> None:
        self.config = config or MiniConfig.from_env()
        self.config.ensure_directories()
        self.artifacts = artifacts or ArtifactRepository(self.config)
        default_location = self.artifacts.experiment_location("0")
        self.store = store or SqliteStore(
            self.config.backend_path,
            default_artifact_location=default_location,
            busy_timeout_ms=self.config.sqlite_busy_timeout_ms,
        )
        self.webhooks = WebhookService(self.store)
        self.registry = ModelRegistry(self.store, emit=self.webhooks.emit)
        self.prompts = PromptRegistry(self.store, emit=self.webhooks.emit)
        self.datasets = DatasetRegistry(
            self.store, config=self.config, emit=self.webhooks.emit
        )
        self.monitoring = MonitoringService(self.store)

    # ------------------------------------------------------------------
    # Experiments
    # ------------------------------------------------------------------
    def create_experiment(
        self,
        name: str,
        *,
        artifact_location: str | None = None,
        tags: Mapping[str, Any] | Sequence[dict[str, Any]] | None = None,
    ) -> Experiment:
        name = validate_name(name, what="experiment name")
        existing = self.store.get_experiment_by_name(name, include_deleted=True)
        if existing is not None:
            hint = " (it is deleted; restore it instead of recreating)" if existing.lifecycle_stage == "deleted" else ""
            raise ResourceAlreadyExists(f"experiment {name!r} already exists{hint}")
        clean_tags = {
            validate_key(key, what="tag key"): coerce_tag_value(value)
            for key, value in parse_pairs(tags, what="experiment tags", stringify=False).items()
        }
        if artifact_location:
            if not isinstance(artifact_location, (str, Path)):
                raise InvalidParameter("artifact_location must be a path string")
            if not str(artifact_location).startswith("file:"):
                artifact_location = path_to_file_uri(artifact_location)
            location = self.artifacts.validate_experiment_location(str(artifact_location))
        else:
            location = self.artifacts.experiment_location("0")
        experiment = self.store.create_experiment(name, location, tags=clean_tags)
        if not artifact_location:
            final_location = self.artifacts.experiment_location(experiment.experiment_id)
            experiment = self.store.update_experiment(
                experiment.experiment_id, artifact_location=final_location
            )
        return experiment

    def get_experiment(self, experiment_id: str) -> Experiment:
        return self.store.get_experiment(self._canonical_experiment_id(experiment_id), include_deleted=True)

    def get_experiment_by_name(self, name: str) -> Experiment | None:
        if not isinstance(name, str):
            raise InvalidParameter("experiment name must be a string")
        return self.store.get_experiment_by_name(name, include_deleted=True)

    @staticmethod
    def _run_id(run_id: Any) -> str:
        if not isinstance(run_id, str) or not is_id(run_id):
            raise InvalidParameter("run_id must be a 32-character hexadecimal identifier")
        return run_id

    @staticmethod
    def _canonical_experiment_id(experiment_id: Any) -> str:
        if isinstance(experiment_id, bool):
            raise InvalidParameter("experiment_id must be an integer")
        try:
            normalized = int(str(experiment_id))
        except (TypeError, ValueError) as exc:
            raise InvalidParameter("experiment_id must be an integer") from exc
        if normalized < 0:
            raise InvalidParameter("experiment_id must be >= 0")
        if normalized > 2**63 - 1:
            raise InvalidParameter("experiment_id is outside the signed 64-bit range")
        return str(normalized)

    def delete_experiment(self, experiment_id: str) -> None:
        canonical = self._canonical_experiment_id(experiment_id)
        if canonical == "0":
            raise InvalidParameter("the Default experiment cannot be deleted")
        self.store.delete_experiment(canonical)

    def restore_experiment(self, experiment_id: str) -> None:
        self.store.restore_experiment(self._canonical_experiment_id(experiment_id))

    def set_experiment_tag(self, experiment_id: str, key: str, value: Any) -> None:
        self.store.set_experiment_tag(
            self._canonical_experiment_id(experiment_id),
            validate_key(key, what="tag key"),
            coerce_tag_value(value),
        )

    def update_experiment(self, experiment_id: str, *, name: str | None = None) -> Experiment:
        if name is not None:
            name = validate_name(name, what="experiment name")
        return self.store.update_experiment(self._canonical_experiment_id(experiment_id), name=name)

    def search_experiments(
        self,
        filter_string: str | None = None,
        *,
        max_results: int = 1000,
        order_by: Sequence[str] | None = None,
        page_token: str | None = None,
        include_deleted: bool = False,
    ) -> tuple[list[Experiment], str | None]:
        max_results = self._validate_max_results(max_results)
        if order_by is not None and (
            isinstance(order_by, (str, bytes))
            or not isinstance(order_by, Sequence)
            or any(not isinstance(item, str) for item in order_by)
        ):
            raise InvalidParameter("order_by must be a sequence of strings")
        if page_token is not None and not isinstance(page_token, str):
            raise InvalidParameter("page_token must be a string")
        where_sql, params = compile_experiment_filter(filter_string, include_deleted=include_deleted)
        order_sql = compile_experiment_order(order_by)
        offset = decode_page_token(page_token)
        rows = self.store.search_experiment_rows(where_sql, params, order_sql, max_results + 1, offset)
        has_more = len(rows) > max_results
        rows = rows[:max_results]
        experiments = [self.store._experiment_from_row(row, self.store._experiment_tags(row["experiment_id"])) for row in rows]  # noqa: SLF001
        return experiments, encode_page_token(offset + max_results) if has_more else None

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------
    def start_run(
        self,
        *,
        experiment_id: str | None = None,
        experiment_name: str | None = None,
        run_name: str | None = None,
        tags: Mapping[str, Any] | Sequence[dict[str, Any]] | None = None,
        run_id: str | None = None,
        user_id: str | None = None,
        start_time: int | None = None,
    ) -> Run:
        if run_id is not None:
            if not is_id(run_id):
                raise InvalidParameter("run_id must be a 32-character hexadecimal identifier")
            run = self.store.get_run(run_id, include_deleted=False)
            if run.info.is_terminal:
                raise InvalidParameter(f"cannot resume run {run_id}: it is already {run.info.status}")
            return run

        if experiment_id is not None and experiment_name is not None:
            raise InvalidParameter("pass experiment_id or experiment_name, not both")
        if experiment_name is not None:
            if not isinstance(experiment_name, str):
                raise InvalidParameter("experiment_name must be a string")
            experiment = self.store.get_experiment_by_name(experiment_name, include_deleted=False)
            if experiment is None:
                raise ResourceNotFound(f"experiment named {experiment_name!r} not found")
            experiment_id = experiment.experiment_id
        experiment_id = self._canonical_experiment_id(experiment_id or "0")
        experiment = self.store.get_experiment(experiment_id, include_deleted=False)
        if experiment.lifecycle_stage != "active":
            raise InvalidParameter("cannot create a run in a deleted experiment")
        if run_id is None:
            from .utils import new_id

            run_id = new_id()
        run_name_text = validate_name(run_name or "", what="run name", max_len=250) if run_name else ""
        if not run_name_text:
            parsed = parse_pairs(tags, what="tags", stringify=False)
            run_name_text = str(parsed.get("mlflow.runName") or "").strip()
        if not run_name_text:
            run_name_text = f"run-{run_id[:8]}"
        run_name_text = validate_name(run_name_text, what="run name", max_len=250)
        clean_tags = {
            validate_key(key, what="tag key"): coerce_tag_value(value)
            for key, value in parse_pairs(tags, what="tags", stringify=False).items()
        }
        user = user_id or os.environ.get("MINI_MLFLOW_USER") or os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"
        clean_tags.setdefault("mlflow.user", user)
        clean_tags.setdefault("mlflow.source.name", sys.argv[0] if sys.argv else "")
        clean_tags["mlflow.runName"] = run_name_text
        artifact_uri = self.artifacts.new_run_artifact_uri(experiment.artifact_location, run_id)
        return self.store.create_run(
            run_id=run_id,
            experiment_id=experiment.experiment_id,
            run_name=run_name_text,
            user_id=user,
            artifact_uri=artifact_uri,
            start_time=validate_timestamp(start_time),
            tags=clean_tags,
        )

    def get_run(self, run_id: str, *, include_deleted: bool = False) -> Run:
        return self.store.get_run(self._run_id(run_id), include_deleted=include_deleted)

    def end_run(self, run_id: str, status: str = "FINISHED") -> Any:
        status = str(status or "").upper()
        if status not in _ALLOWED_END_STATUSES:
            raise InvalidParameter("end status must be FINISHED, FAILED or KILLED")
        return self.store.end_run(self._run_id(run_id), status, now_ms())

    def update_run(self, run_id: str, *, run_name: str | None = None, status: str | None = None) -> Any:
        run_id = self._run_id(run_id)
        if status is not None:
            status = str(status).upper()
            if status not in {item.value for item in RunStatus}:
                raise InvalidParameter(f"invalid run status {status!r}")
        if run_name is not None:
            run_name = validate_name(run_name, what="run name", max_len=250)
        if status in _ALLOWED_END_STATUSES:
            if run_name is not None:
                self.store.update_run(run_id, run_name=run_name)
            return self.store.end_run(run_id, status, now_ms())
        if status is not None:
            run = self.store.get_run(run_id, include_deleted=False)
            if run.info.is_terminal:
                raise InvalidParameter("a terminal run cannot be reopened or rescheduled")
            if status not in {RunStatus.RUNNING.value, RunStatus.SCHEDULED.value}:
                raise InvalidParameter("update_run can only set RUNNING/SCHEDULED or end a run")
        return self.store.update_run(run_id, run_name=run_name, status=status)

    def delete_run(self, run_id: str) -> None:
        self.store.delete_run(self._run_id(run_id))

    def restore_run(self, run_id: str) -> None:
        self.store.restore_run(self._run_id(run_id))

    def _loggable_run(self, run_id: str) -> Run:
        run = self.store.get_run(self._run_id(run_id), include_deleted=False)
        if run.info.is_terminal:
            raise InvalidParameter(
                f"run {run_id} is {run.info.status}; logging requires a running run "
                "(resume is not supported for terminal runs)"
            )
        return run

    def log_param(self, run_id: str, key: str, value: Any) -> None:
        self.log_batch(run_id, params={key: value})

    def log_params(self, run_id: str, params: Mapping[str, Any]) -> None:
        self.log_batch(run_id, params=params)

    def log_metric(
        self,
        run_id: str,
        key: str,
        value: Any,
        *,
        timestamp: int | None = None,
        step: int = 0,
    ) -> None:
        self.log_batch(
            run_id,
            metrics=[{"key": key, "value": value, "timestamp": timestamp, "step": step}],
        )

    def log_metrics(self, run_id: str, metrics: Mapping[str, Any], *, step: int = 0) -> None:
        self.log_batch(
            run_id,
            metrics=[{"key": key, "value": value, "step": step} for key, value in metrics.items()],
        )

    def log_batch(
        self,
        run_id: str,
        *,
        metrics: Sequence[Mapping[str, Any]] | None = None,
        params: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
        tags: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        self._loggable_run(run_id)
        if metrics is not None and (
            isinstance(metrics, (str, bytes)) or not isinstance(metrics, Sequence)
        ):
            raise InvalidParameter("metrics must be a sequence of metric objects")
        raw_metrics = list(metrics or [])
        param_pairs = parse_pairs(params, what="params", stringify=False)
        tag_pairs = parse_pairs(tags, what="tags", stringify=False)
        if not raw_metrics and not param_pairs and not tag_pairs:
            raise InvalidParameter("log_batch requires at least one metric, param, or tag")
        metric_rows: list[tuple[str, float, int, int]] = []
        for item in raw_metrics:
            if not isinstance(item, Mapping) or "key" not in item or "value" not in item:
                raise InvalidParameter("metric entries must contain key and value")
            key = validate_key(str(item["key"]), what="metric key")
            value = coerce_metric(item["value"])
            timestamp = validate_timestamp(item.get("timestamp"))
            step = validate_metric_step(item.get("step", 0))
            metric_rows.append((key, value, timestamp, step))
        clean_params = {
            validate_key(key, what="param key"): coerce_param_value(value)
            for key, value in param_pairs.items()
        }
        clean_tags = {
            validate_key(key, what="tag key"): coerce_tag_value(value)
            for key, value in tag_pairs.items()
        }
        if "mlflow.runName" in clean_tags:
            clean_tags["mlflow.runName"] = validate_name(
                clean_tags["mlflow.runName"], what="run name", max_len=250
            )
        self.store.log_batch(run_id, metrics=metric_rows, params=clean_params, tags=clean_tags)
        if "mlflow.runName" in clean_tags:
            self.store.update_run(run_id, run_name=clean_tags["mlflow.runName"])

    def set_tag(self, run_id: str, key: str, value: Any) -> None:
        self.log_batch(run_id, tags={key: value})

    def delete_tag(self, run_id: str, key: str) -> None:
        self._loggable_run(run_id)
        self.store.delete_run_tag(run_id, validate_key(key, what="tag key"))

    def get_metric_history(self, run_id: str, key: str) -> list[Any]:
        run_id = self._run_id(run_id)
        self.store.get_run(run_id, include_deleted=False)
        return self.store.get_metric_history(run_id, key)

    def log_input(
        self,
        run_id: str,
        *,
        dataset_name: str,
        dataset_digest: str = "",
        dataset_source: str = "",
        dataset_source_type: str = "",
        context: str = "training",
        tags: Mapping[str, Any] | None = None,
    ) -> None:
        self._loggable_run(run_id)
        dataset_name = validate_name(dataset_name, what="dataset name", max_len=250)
        clean_tags = {
            validate_key(key, what="tag key"): coerce_tag_value(value)
            for key, value in (tags or {}).items()
        }
        self.store.log_run_input(
            run_id,
            RunInput(
                dataset_name=dataset_name,
                dataset_digest=str(dataset_digest or ""),
                dataset_source=str(dataset_source or ""),
                dataset_source_type=str(dataset_source_type or ""),
                context=str(context or "training"),
                tags=clean_tags,
            ),
        )

    def search_runs(
        self,
        filter_string: str | None = None,
        *,
        max_results: int = 1000,
        order_by: Sequence[str] | None = None,
        page_token: str | None = None,
        include_deleted: bool = False,
        experiment_ids: Sequence[str | int] | None = None,
    ) -> tuple[list[Run], str | None]:
        max_results = self._validate_max_results(max_results)
        if order_by is not None and (
            isinstance(order_by, (str, bytes))
            or not isinstance(order_by, Sequence)
            or any(not isinstance(item, str) for item in order_by)
        ):
            raise InvalidParameter("order_by must be a sequence of strings")
        if page_token is not None and not isinstance(page_token, str):
            raise InvalidParameter("page_token must be a string")
        where_sql, params = compile_run_filter(filter_string, include_deleted=include_deleted)
        if experiment_ids is not None:
            if isinstance(experiment_ids, (str, bytes)) or not isinstance(experiment_ids, Sequence):
                raise InvalidParameter("experiment_ids must be a sequence")
            if len(experiment_ids) > 500:
                raise InvalidParameter("experiment_ids supports at most 500 values")
            normalized_ids = [self._canonical_experiment_id(value) for value in experiment_ids]
            if not normalized_ids:
                where_sql += " AND 1 = 0"
            else:
                placeholders = ",".join("?" for _ in normalized_ids)
                where_sql += f" AND r.experiment_id IN ({placeholders})"
                params.extend(int(value) for value in normalized_ids)
        order_sql, order_params = compile_run_order(order_by)
        offset = decode_page_token(page_token)
        runs = self.store.search_runs(
            where_sql,
            params,
            order_sql,
            order_params,
            max_results + 1,
            offset,
        )
        has_more = len(runs) > max_results
        return runs[:max_results], encode_page_token(offset + max_results) if has_more else None

    # ------------------------------------------------------------------
    # Artifacts
    # ------------------------------------------------------------------
    def _artifact_run(self, run_id: str) -> Run:
        return self.store.get_run(self._run_id(run_id), include_deleted=False)

    def log_artifact(self, run_id: str, local_path: str | Path, artifact_path: str | None = None) -> str:
        run = self._artifact_run(run_id)
        return self.artifacts.log_artifact(run.info.artifact_uri, local_path, artifact_path)

    def log_artifacts(self, run_id: str, local_dir: str | Path, artifact_path: str | None = None) -> list[str]:
        run = self._artifact_run(run_id)
        return self.artifacts.log_artifacts(run.info.artifact_uri, local_dir, artifact_path)

    def log_text(self, run_id: str, text: str, artifact_file: str) -> str:
        run = self._artifact_run(run_id)
        return self.artifacts.log_text(run.info.artifact_uri, text, artifact_file)

    def log_dict(self, run_id: str, dictionary: Any, artifact_file: str) -> str:
        run = self._artifact_run(run_id)
        return self.artifacts.log_dict(run.info.artifact_uri, dictionary, artifact_file)

    def log_artifact_bytes(
        self,
        run_id: str,
        relative_path: str,
        data: bytes,
        *,
        overwrite: bool = False,
    ) -> str:
        run = self._artifact_run(run_id)
        return self.artifacts.log_bytes(run.info.artifact_uri, relative_path, data, overwrite=overwrite)

    def list_artifacts(self, run_id: str, path: str | None = None) -> list[Any]:
        run = self._artifact_run(run_id)
        return self.artifacts.list_artifacts(run.info.artifact_uri, path)

    def read_artifact(self, run_id: str, path: str) -> bytes:
        run = self._artifact_run(run_id)
        return self.artifacts.read_bytes(run.info.artifact_uri, path)

    def download_artifacts(self, run_id: str, path: str | None, dst_path: str | Path) -> Path:
        run = self._artifact_run(run_id)
        return self.artifacts.download_artifacts(run.info.artifact_uri, path, dst_path)

    # ------------------------------------------------------------------
    # Models + registry convenience
    # ------------------------------------------------------------------
    def log_model(
        self,
        run_id: str,
        model: Any,
        *,
        artifact_path: str = "model",
        registered_model_name: str | None = None,
        signature: Any = None,
        input_example: Any = None,
        metadata: dict[str, Any] | None = None,
        flavor: str = "linear",
        name: str | None = None,
        model_type: str | None = None,
        params: Mapping[str, Any] | None = None,
        tags: Mapping[str, Any] | None = None,
    ) -> ModelInfo:
        run = self._artifact_run(run_id)
        if name is not None:
            return self._log_logged_model(
                run,
                model,
                name=name,
                model_type=model_type or flavor,
                params=params,
                tags=tags,
                signature=signature,
                input_example=input_example,
                metadata=metadata,
                flavor=flavor,
                registered_model_name=registered_model_name,
            )
        with tempfile.TemporaryDirectory(prefix="mini-mlflow-log-model-") as tmp:
            saved = save_model(
                tmp,
                model,
                flavor=flavor,
                signature=signature,
                input_example=input_example,
                metadata=metadata,
                run_id=run_id,
                artifact_path=artifact_path,
                overwrite=True,
            )
            self.log_artifacts(run_id, tmp, artifact_path=artifact_path)
        model_uri = f"runs:/{run_id}/{artifact_path}"
        result = ModelInfo(
            model_uri=model_uri,
            artifact_path=artifact_path,
            run_id=run_id,
            signature=saved.signature,
            flavors=saved.flavors,
            registered_model_name=registered_model_name,
        )
        if registered_model_name:
            try:
                self.registry.get_registered_model(registered_model_name)
            except ResourceNotFound:
                self.registry.create_registered_model(
                    registered_model_name,
                    description=f"Created from run {run_id}",
                )
            version = self.registry.create_model_version(
                registered_model_name,
                source=model_uri,
                run_id=run_id,
                tags={
                    "mlflow.log-model.history": compact_json(
                        [{"run_id": run_id, "artifact_path": artifact_path, "model_uri": model_uri}]
                    )
                },
            )
            result.version = version.version
        return result

    def _log_logged_model(
        self,
        run: Run,
        model: Any,
        *,
        name: str,
        model_type: str,
        params: Mapping[str, Any] | None,
        tags: Mapping[str, Any] | None,
        signature: Any,
        input_example: Any,
        metadata: dict[str, Any] | None,
        flavor: str,
        registered_model_name: str | None,
    ) -> ModelInfo:
        logged = self.create_logged_model(
            name,
            experiment_id=run.info.experiment_id,
            model_type=model_type,
            source_run_id=run.info.run_id,
            tags=tags,
            params=params,
        )
        try:
            with tempfile.TemporaryDirectory(prefix="mini-mlflow-logged-model-") as tmp:
                saved = save_model(
                    tmp,
                    model,
                    flavor=flavor,
                    signature=signature,
                    input_example=input_example,
                    metadata=metadata,
                    run_id=run.info.run_id,
                    artifact_path="",
                    overwrite=True,
                )
                self.artifacts.log_artifacts(logged.artifact_location, tmp)
            logged = self.finalize_logged_model(logged.model_id)
        except BaseException:
            # Best effort: a failed upload should be visible as FAILED rather
            # than leaving a model that looks perpetually pending.
            try:
                self.finalize_logged_model(
                    logged.model_id,
                    LoggedModelStatus.FAILED.value,
                    status_message="artifact upload failed",
                )
            except Exception:  # noqa: BLE001 - preserve the original error
                pass
            raise
        result = ModelInfo(
            model_uri=logged.model_uri,
            artifact_path="",
            run_id=run.info.run_id,
            signature=saved.signature,
            flavors=saved.flavors,
            registered_model_name=registered_model_name,
            model_id=logged.model_id,
            model_type=model_type,
        )
        if registered_model_name:
            try:
                self.registry.get_registered_model(registered_model_name)
            except ResourceNotFound:
                self.registry.create_registered_model(
                    registered_model_name,
                    description=f"Created from logged model {logged.model_id}",
                )
            version = self.registry.create_model_version(
                registered_model_name,
                source=logged.model_uri,
                run_id=run.info.run_id,
                tags={"mlflow.loggedModel.modelId": logged.model_id},
            )
            result.version = version.version
        return result

    def get_model_version_by_alias(self, name: str, alias: str) -> ModelVersion:
        return self.registry.get_model_version_by_alias(name, alias)

    # ------------------------------------------------------------------
    # Logged models (MLflow 3)
    # ------------------------------------------------------------------
    def _logged_model_id(self, model_id: Any) -> str:
        if not isinstance(model_id, str) or not is_logged_model_id(model_id):
            raise InvalidParameter(
                "model_id must be an MLflow logged-model id (m- followed by 32 hex characters)"
            )
        return model_id

    def _default_experiment_id(self) -> str:
        return self._canonical_experiment_id(os.environ.get("MINI_MLFLOW_EXPERIMENT_ID") or "0")

    def create_logged_model(
        self,
        name: str | None = None,
        *,
        experiment_id: str | None = None,
        model_type: str = "",
        source_run_id: str | None = None,
        tags: Mapping[str, Any] | Sequence[dict[str, Any]] | None = None,
        params: Mapping[str, Any] | Sequence[dict[str, Any]] | None = None,
        artifact_location: str | None = None,
        status: str = LoggedModelStatus.PENDING.value,
    ) -> LoggedModel:
        model_id = new_logged_model_id()
        timestamp = now_ms()
        experiment = self.get_experiment(
            experiment_id if experiment_id is not None else self._default_experiment_id()
        )
        if experiment.lifecycle_stage != "active":
            raise InvalidParameter("cannot create a logged model in a deleted experiment")
        clean_name = validate_name(name or "model", what="logged model name", max_len=250)
        if not isinstance(model_type, str):
            raise InvalidParameter("model_type must be a string")
        clean_type = model_type.strip()
        if len(clean_type) > 128 or any(ord(char) < 32 for char in clean_type):
            raise InvalidParameter("model_type must be at most 128 printable characters")
        if source_run_id is not None:
            if not isinstance(source_run_id, str) or not is_id(source_run_id):
                raise InvalidParameter("source_run_id must be a 32-character hexadecimal run id")
            run = self.get_run(source_run_id)
            if run.info.experiment_id != experiment.experiment_id:
                raise InvalidParameter("source_run_id belongs to a different experiment")
        if artifact_location is not None:
            if not isinstance(artifact_location, (str, Path)):
                raise InvalidParameter("artifact_location must be a path string")
            location_uri = str(artifact_location)
            if not location_uri.startswith("file:"):
                location_uri = path_to_file_uri(location_uri)
            location = self.artifacts.validate_experiment_location(location_uri)
            file_uri_to_path(location).mkdir(parents=True, exist_ok=True)
        else:
            base = file_uri_to_path(experiment.artifact_location) / "models" / model_id
            location = self.artifacts.validate_experiment_location(base.resolve().as_uri())
            base.mkdir(parents=True, exist_ok=True)
        clean_tags = {
            validate_key(key, what="tag key"): coerce_tag_value(value)
            for key, value in parse_pairs(tags, what="logged model tags", stringify=False).items()
        }
        clean_params = {
            validate_key(key, what="param key"): coerce_param_value(value)
            for key, value in parse_pairs(params, what="logged model params", stringify=False).items()
        }
        model = LoggedModel(
            model_id=model_id,
            experiment_id=experiment.experiment_id,
            name=clean_name,
            artifact_location=location,
            creation_timestamp=timestamp,
            last_updated_timestamp=timestamp,
            model_type=clean_type,
            source_run_id=source_run_id,
            status=normalize_logged_model_status(status),
            tags=clean_tags,
            params=clean_params,
        )
        return self.store.create_logged_model(model)

    def get_logged_model(self, model_id: str) -> LoggedModel:
        return self.store.get_logged_model(self._logged_model_id(model_id))

    def finalize_logged_model(
        self,
        model_id: str,
        status: str = LoggedModelStatus.READY.value,
        *,
        status_message: str = "",
    ) -> LoggedModel:
        model_id = self._logged_model_id(model_id)
        normalized = normalize_logged_model_status(status)
        if not isinstance(status_message, str):
            raise InvalidParameter("status_message must be a string")
        if len(status_message) > 5000 or any(ord(char) < 32 and char not in "\n\t" for char in status_message):
            raise InvalidParameter("status_message must be at most 5000 printable characters")
        return self.store.finalize_logged_model(
            model_id, normalized, status_message, now_ms()
        )

    def set_logged_model_tags(
        self,
        model_id: str,
        tags: Mapping[str, Any] | Sequence[dict[str, Any]],
    ) -> None:
        model_id = self._logged_model_id(model_id)
        if not tags:
            raise InvalidParameter("at least one tag is required")
        clean = {
            validate_key(key, what="tag key"): coerce_tag_value(value)
            for key, value in parse_pairs(tags, what="logged model tags", stringify=False).items()
        }
        timestamp = now_ms()
        for key, value in clean.items():
            self.store.set_logged_model_tag(model_id, key, value, timestamp)

    def delete_logged_model_tag(self, model_id: str, key: str) -> None:
        self.store.delete_logged_model_tag(
            self._logged_model_id(model_id),
            validate_key(key, what="tag key"),
            now_ms(),
        )

    def log_logged_model_params(
        self,
        model_id: str,
        params: Mapping[str, Any] | Sequence[dict[str, Any]],
    ) -> LoggedModel:
        model_id = self._logged_model_id(model_id)
        if not params:
            raise InvalidParameter("at least one param is required")
        clean = {
            validate_key(key, what="param key"): coerce_param_value(value)
            for key, value in parse_pairs(params, what="logged model params", stringify=False).items()
        }
        return self.store.log_logged_model_params(model_id, clean, now_ms())

    def log_logged_model_metrics(
        self,
        model_id: str,
        metrics: Mapping[str, Any] | Sequence[dict[str, Any]],
        *,
        step: int = 0,
        timestamp: int | None = None,
    ) -> LoggedModel:
        model_id = self._logged_model_id(model_id)
        step = validate_metric_step(step)
        when = validate_timestamp(timestamp)
        entries: list[Metric] = []
        if isinstance(metrics, Mapping):
            for key, value in metrics.items():
                entries.append(
                    Metric(
                        key=validate_key(key, what="metric key"),
                        value=coerce_metric(value),
                        timestamp=when,
                        step=step,
                    )
                )
        elif isinstance(metrics, Sequence) and not isinstance(metrics, (str, bytes)):
            for item in metrics:
                if not isinstance(item, Mapping) or "key" not in item or "value" not in item:
                    raise InvalidParameter("metric entries must contain key and value")
                entries.append(
                    Metric(
                        key=validate_key(str(item["key"]), what="metric key"),
                        value=coerce_metric(item["value"]),
                        timestamp=validate_timestamp(item.get("timestamp", when)),
                        step=validate_metric_step(item.get("step", step)),
                    )
                )
        else:
            raise InvalidParameter("metrics must be a mapping or a sequence of metric entries")
        if not entries:
            raise InvalidParameter("at least one metric is required")
        return self.store.log_logged_model_metrics(model_id, entries, now_ms())

    def search_logged_models(
        self,
        experiment_ids: Sequence[str | int] | None = None,
        *,
        filter_string: str | None = None,
        max_results: int = 100,
        order_by: Sequence[str] | None = None,
        page_token: str | None = None,
    ) -> tuple[list[LoggedModel], str | None]:
        max_results = self._validate_max_results(max_results)
        if order_by is not None and (
            isinstance(order_by, (str, bytes))
            or not isinstance(order_by, Sequence)
            or any(not isinstance(item, str) for item in order_by)
        ):
            raise InvalidParameter("order_by must be a sequence of strings")
        if page_token is not None and not isinstance(page_token, str):
            raise InvalidParameter("page_token must be a string")
        if experiment_ids is None:
            normalized_ids = [self._default_experiment_id()]
        else:
            if isinstance(experiment_ids, (str, bytes)) or not isinstance(experiment_ids, Sequence):
                raise InvalidParameter("experiment_ids must be a sequence")
            if len(experiment_ids) > 10:
                raise InvalidParameter(
                    "search_logged_models supports at most 10 experiments per request"
                )
            normalized_ids = [self._canonical_experiment_id(value) for value in experiment_ids]
        where_sql, params = compile_logged_model_filter(filter_string)
        if not normalized_ids:
            where_sql += " AND 1 = 0"
        else:
            placeholders = ",".join("?" for _ in normalized_ids)
            where_sql += f" AND lm.experiment_id IN ({placeholders})"
            params.extend(int(value) for value in normalized_ids)
        order_sql, order_params = compile_logged_model_order(order_by)
        offset = decode_page_token(page_token)
        models = self.store.search_logged_models(
            where_sql,
            params,
            order_sql,
            order_params,
            max_results + 1,
            offset,
        )
        has_more = len(models) > max_results
        return (
            models[:max_results],
            encode_page_token(offset + max_results) if has_more else None,
        )

    def delete_logged_model(self, model_id: str) -> None:
        """Delete metadata only.

        Artifacts are deliberately left in place: a mistaken delete must be
        recoverable, and production garbage collection needs a retention
        policy that understands shared/referenced blobs.  Chapter 10 discusses
        the same trade-off for run artifacts.
        """
        self.store.delete_logged_model(self._logged_model_id(model_id))

    # ------------------------------------------------------------------
    # Traces
    # ------------------------------------------------------------------
    def log_trace(self, trace: Trace) -> Trace:
        if not is_id(trace.trace_id):
            raise InvalidParameter("trace_id must be a 32-character hexadecimal identifier")
        experiment = self.store.get_experiment(trace.experiment_id, include_deleted=True)
        if experiment.lifecycle_stage != "active":
            raise InvalidParameter("cannot log a trace to a deleted experiment")
        trace.name = validate_name(trace.name, what="trace name", max_len=250)
        trace.tags = {str(key): str(value) for key, value in redact(dict(trace.tags)).items()}
        trace.request_metadata = {
            str(key): str(value) for key, value in redact(dict(trace.request_metadata)).items()
        }
        trace.request = redact(trace.request)
        trace.response = redact(trace.response)
        self._check_trace_payload(trace.name, "trace.name")
        self._check_trace_payload(trace.tags, "trace.tags")
        self._check_trace_payload(trace.request_metadata, "trace.request_metadata")
        self._check_trace_payload(trace.request, "trace.request")
        self._check_trace_payload(trace.response, "trace.response")
        seen: set[str] = set()
        parents: dict[str, str | None] = {}
        for span in trace.spans:
            if not span.span_id or span.span_id in seen:
                raise InvalidParameter(f"duplicate or empty span_id {span.span_id!r}")
            if span.trace_id and span.trace_id != trace.trace_id:
                raise InvalidParameter(
                    f"span {span.span_id!r} belongs to trace {span.trace_id!r}, "
                    f"not {trace.trace_id!r}"
                )
            span.trace_id = trace.trace_id
            span.name = validate_name(span.name, what="span name", max_len=250)
            span.inputs = redact(span.inputs)
            span.outputs = redact(span.outputs)
            span.attributes = dict(redact(dict(span.attributes)))
            span.events = redact(span.events)
            seen.add(span.span_id)
            parents[span.span_id] = span.parent_span_id
            self._check_trace_payload(span.name, f"span {span.span_id}.name")
            self._check_trace_payload(span.inputs, f"span {span.span_id}.inputs")
            self._check_trace_payload(span.outputs, f"span {span.span_id}.outputs")
            self._check_trace_payload(span.attributes, f"span {span.span_id}.attributes")
            self._check_trace_payload(span.events, f"span {span.span_id}.events")
            if span.end_time_ns is not None and span.end_time_ns < span.start_time_ns:
                raise InvalidParameter("span end_time_ns must be >= start_time_ns")
        for span_id, parent_id in parents.items():
            if parent_id is not None and parent_id not in seen:
                raise InvalidParameter(f"span {span_id!r} references unknown parent {parent_id!r}")
        root_spans = [span for span in trace.spans if span.parent_span_id is None]
        trace.status = "ERROR" if any(span.status == "ERROR" for span in trace.spans) else trace.status
        if not trace.spans:
            raise InvalidParameter("a trace must contain at least one span")
        if not root_spans:
            raise InvalidParameter("a trace must have at least one root span")
        if trace.request is None and root_spans[0].inputs is not None:
            trace.request = root_spans[0].inputs
        if trace.response is None:
            if root_spans[0].outputs is not None:
                trace.response = root_spans[0].outputs
            else:
                for span in reversed(trace.spans):
                    if span.outputs is not None:
                        trace.response = span.outputs
                        break
        self.store.log_trace(trace)
        return self.store.get_trace(trace.trace_id)

    def get_trace(self, trace_id: str) -> Trace:
        return self.store.get_trace(trace_id)

    def search_traces(
        self,
        experiment_ids: Sequence[str] | None = None,
        *,
        filter_string: str | None = None,
        max_results: int = 100,
        order_by: Sequence[str] | None = None,
    ) -> list[Trace]:
        if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= 5000:
            raise InvalidParameter("max_results must be between 1 and 5000")
        # Filters are evaluated after loading recent traces. Fetch a bounded
        # candidate set large enough to avoid missing older matches in normal
        # tutorial workloads; production should push filters into the store.
        if experiment_ids is not None:
            if isinstance(experiment_ids, (str, bytes)) or not isinstance(experiment_ids, Sequence):
                raise InvalidParameter("experiment_ids must be a sequence of experiment IDs")
            if len(experiment_ids) > 500:
                raise InvalidParameter("experiment_ids supports at most 500 values")
            experiment_ids = [str(item) for item in experiment_ids]
        ast = parse_filter(filter_string)
        traces: list[Trace] = []
        offset = 0
        batch_size = _TRACE_SCAN_BATCH
        while True:
            batch = self.store.list_traces(
                experiment_ids=experiment_ids, limit=batch_size, offset=offset
            )
            if not batch:
                break
            offset += len(batch)
            for trace in batch:
                if ast is None:
                    traces.append(trace)
                    continue

                def resolver(field: Field, item: Trace = trace) -> Any:
                    if field.scope == "attributes":
                        key = field.key.lower()
                        mapping = {
                            "trace_id": item.trace_id,
                            "name": item.name,
                            "status": item.status,
                            "experiment_id": item.experiment_id,
                            "timestamp": item.timestamp,
                            "latency_ms": item.latency_ms,
                        }
                        if key not in mapping:
                            raise SearchSyntaxError(f"unknown trace attribute {field.key!r}")
                        return mapping[key]
                    if field.scope == "tags":
                        return item.tags.get(field.key)
                    if field.scope == "assessments":
                        matches = [
                            assessment
                            for assessment in item.assessments
                            if assessment.name == field.key and assessment.valid
                        ]
                        if not matches:
                            return None
                        # Multiple sources may judge the same thing; the most
                        # recently updated valid judgment wins for filtering.
                        return max(matches, key=lambda assessment: assessment.last_update_time).value
                    raise SearchSyntaxError(
                        "trace filters support attributes.*, tags.* and assessments.* only"
                    )

                if evaluate_ast(ast, resolver):
                    traces.append(trace)
            if len(batch) < batch_size:
                break
            if not order_by and len(traces) >= max_results:
                break
        if order_by:
            for raw in reversed(list(order_by)):
                text = str(raw).strip()
                match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_.\-]*)(?:\s+(ASC|DESC))?", text, re.IGNORECASE)
                if not match:
                    raise InvalidParameter(f"invalid trace order_by entry {raw!r}")
                name = match.group(1)
                key = name.split(".", 1)[1] if "." in name else name
                mapping = {"timestamp": "timestamp", "name": "name", "status": "status", "latency_ms": "latency_ms"}
                if key not in mapping:
                    raise InvalidParameter(f"cannot order traces by {name!r}")
                traces.sort(
                    key=lambda item, attr=mapping[key]: getattr(item, attr),
                    reverse=(match.group(2) or "ASC").upper() == "DESC",
                )
        return traces[:max_results]

    # ------------------------------------------------------------------
    # Assessments (feedback / expectations)
    # ------------------------------------------------------------------
    def log_feedback(
        self,
        trace_id: str,
        name: str,
        value: Any,
        *,
        source_type: str = "HUMAN",
        source_id: str = "",
        rationale: str = "",
        metadata: Mapping[str, Any] | None = None,
        is_expectation: bool = False,
        valid: bool = True,
    ) -> Assessment:
        """Attach a typed judgment to a trace.

        The identity is (trace, name, source, expectation): logging the same
        judgment twice updates it instead of creating duplicates.  That makes
        feedbackIdempotent enough for retries while preserving create_time.
        """
        if not isinstance(trace_id, str) or not is_id(trace_id):
            raise InvalidParameter("trace_id must be a 32-character hexadecimal identifier")
        self.store.get_trace(trace_id)
        name = validate_key(name, what="assessment name")
        source_type = str(source_type or "HUMAN").upper()
        if source_type not in {"HUMAN", "LLM_JUDGE", "CODE", "UNKNOWN"}:
            raise InvalidParameter("source_type must be HUMAN, LLM_JUDGE, CODE or UNKNOWN")
        if source_id and (not isinstance(source_id, str) or len(source_id) > 250):
            raise InvalidParameter("source_id must be a string of at most 250 characters")
        if len(str(rationale or "")) > 64 * 1024:
            raise InvalidParameter("rationale is too long (max 64 KiB)")
        if not isinstance(metadata, Mapping) and metadata is not None:
            raise InvalidParameter("metadata must be a mapping")
        clean_metadata = dict(metadata or {})
        try:
            encoded_metadata = compact_json(clean_metadata).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise InvalidParameter("metadata must be JSON-serializable") from exc
        if len(encoded_metadata) > self.config.trace_max_payload_bytes:
            raise InvalidParameter("assessment metadata exceeds the configured payload limit")
        if not self._is_scalar(value):
            raise InvalidParameter("assessment value must be a string, number, boolean or null")
        timestamp = now_ms()
        assessment = Assessment(
            assessment_id=new_id(),
            trace_id=trace_id,
            name=name,
            value=value,
            source_type=source_type,
            source_id=str(source_id or ""),
            rationale=str(rationale or ""),
            metadata=clean_metadata,
            is_expectation=bool(is_expectation),
            valid=bool(valid),
            create_time=timestamp,
            last_update_time=timestamp,
        )
        return self.store.upsert_assessment(assessment)

    @staticmethod
    def _is_scalar(value: Any) -> bool:
        if value is None or isinstance(value, (bool, int, str)):
            return True
        return isinstance(value, float) and value == value and value not in (float("inf"), float("-inf"))

    def get_assessment(self, assessment_id: str) -> Assessment:
        return self.store.get_assessment(str(assessment_id))

    def list_assessments(self, trace_id: str) -> list[Assessment]:
        return self.store.list_assessments(str(trace_id))

    def delete_assessment(self, assessment_id: str) -> None:
        self.store.delete_assessment(str(assessment_id))

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------
    def _check_trace_payload(self, value: Any, label: str) -> None:
        if value is None:
            return
        try:
            size = len(compact_json(value).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise InvalidParameter(f"{label} must be JSON-serializable") from exc
        limit = self.config.trace_max_payload_bytes
        if size > limit:
            raise InvalidParameter(
                f"{label} is {size} bytes, exceeding trace_max_payload_bytes={limit}; "
                "truncate/redact before logging"
            )

    def _validate_max_results(self, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise InvalidParameter("max_results must be a positive integer")
        if value > self.config.search_max_results:
            raise InvalidParameter(f"max_results cannot exceed {self.config.search_max_results}")
        return value

    def close(self) -> None:
        self.webhooks.close()
        self.store.close()

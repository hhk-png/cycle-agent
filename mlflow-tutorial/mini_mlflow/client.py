"""Python client with local and remote backends.

The public methods mirror the service layer.  That parity is tested: the same
scenario runs against ``TrackingService`` directly and through HTTP, and both
must return equal domain objects.  Network calls use only ``urllib`` so the
tutorial has no hidden requests/httpx dependency.

Design notes
------------
* Authentication is a single bearer token.  This is enough for a local team
  server behind TLS, not for multi-tenant authorization; see chapter 20.
* Artifact uploads are base64 JSON in this simplified protocol.  A production
  implementation should use streaming multipart or pre-signed object-store
  URLs to avoid a 33% base64 tax and full buffering.
* The client does not automatically retry non-idempotent writes.  Retrying a
  ``log_metric`` can append a duplicate metric; idempotency keys belong at the
  protocol boundary.
"""

from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import quote
import re
from typing import Any, Mapping, Sequence

from .config import MiniConfig
from .entities import (
    Assessment,
    Dataset,
    Experiment,
    FileInfo,
    LoggedModel,
    Metric,
    ModelVersion,
    Monitor,
    MonitorWindow,
    PromptVersion,
    RegisteredModel,
    Run,
    RunData,
    Trace,
    Webhook,
    WebhookDelivery,
)
from .errors import (
    Conflict,
    InvalidParameter,
    MiniMlflowError,
    ModelLoadError,
    PayloadTooLarge,
    PermissionDenied,
    ProjectExecutionError,
    ResourceAlreadyExists,
    ResourceNotFound,
    Unauthorized,
    UnsupportedFlavor,
)
from .models import ModelInfo, load_model as load_model_uri, save_model
from .search import parse_filter
from .tracking import TrackingService
from .utils import compact_json, parse_pairs, safe_join

_ERROR_CLASSES = {
    "INVALID_PARAMETER_VALUE": InvalidParameter,
    "BAD_REQUEST": InvalidParameter,
    "RESOURCE_DOES_NOT_EXIST": ResourceNotFound,
    "RESOURCE_ALREADY_EXISTS": ResourceAlreadyExists,
    "PERMISSION_DENIED": PermissionDenied,
    "UNSUPPORTED_MODEL_FLAVOR": UnsupportedFlavor,
    "MODEL_LOAD_FAILED": ModelLoadError,
    "REQUEST_TOO_LARGE": PayloadTooLarge,
    "RESOURCE_CONFLICT": Conflict,
    "PROJECT_EXECUTION_FAILED": ProjectExecutionError,
}


class RemoteError(MiniMlflowError):
    code = "REMOTE_ERROR"


class MiniClient:
    def __init__(
        self,
        tracking_uri: str | None = None,
        *,
        config: MiniConfig | None = None,
        timeout: float = 30.0,
        auth_token: str | None = None,
    ) -> None:
        if timeout <= 0:
            raise InvalidParameter("timeout must be positive")
        self.timeout = timeout
        if config is not None:
            self.tracking_uri = config.tracking_uri
            self._remote = False
            self._service = TrackingService(config)
            self._config = config
            self._auth_token = None
        else:
            uri = (
                tracking_uri
                or os.environ.get("MINI_MLFLOW_TRACKING_URI")
                or os.environ.get("MLFLOW_TRACKING_URI")
            )
            if uri and uri.startswith(("http://", "https://")):
                self.tracking_uri = uri.rstrip("/")
                self._remote = True
                self._service = None
                self._config = None
                self._auth_token = auth_token or os.environ.get("MINI_MLFLOW_TRACKING_TOKEN") or os.environ.get("MINI_MLFLOW_AUTH_TOKEN")
            else:
                local_config = MiniConfig.from_env(
                    **({"backend_store_uri": uri} if uri else {})
                )
                self.tracking_uri = local_config.tracking_uri
                self._remote = False
                self._config = local_config
                self._service = TrackingService(local_config)
                self._auth_token = None

    @property
    def service(self) -> TrackingService:
        if self._service is None:
            raise RuntimeError("this client is remote; no local service is available")
        return self._service

    @property
    def is_remote(self) -> bool:
        return self._remote

    @property
    def local_config(self) -> MiniConfig | None:
        return self._config

    @property
    def auth_token(self) -> str | None:
        """Token used for remote calls (never serialized by the API)."""
        return self._auth_token

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
        raw: bool = False,
    ) -> Any:
        url = self.tracking_uri + path
        if query:
            clean_query = {key: value for key, value in query.items() if value is not None}
            url += "?" + urllib.parse.urlencode(clean_query, doseq=True)
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
                if raw:
                    return payload
                if not payload:
                    return {}
                content_type = response.headers.get("Content-Type", "")
                if "application/json" not in content_type:
                    raise RemoteError(
                        f"expected JSON from {path}, got {content_type or 'unknown content type'}"
                    )
                return json.loads(payload.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw_payload = exc.read()
            try:
                data = json.loads(raw_payload.decode("utf-8"))
                code = str(data.get("error_code", "REMOTE_ERROR"))
                message = str(data.get("message", raw_payload.decode("utf-8", errors="replace")))
            except Exception:  # noqa: BLE001
                code, message = "REMOTE_ERROR", raw_payload.decode("utf-8", errors="replace")
            error_class = _ERROR_CLASSES.get(code, RemoteError)
            if code == "PERMISSION_DENIED" and exc.code == 401:
                raise Unauthorized(message) from exc
            raise error_class(message) from exc
        except urllib.error.URLError as exc:
            raise RemoteError(f"cannot reach tracking server {self.tracking_uri}: {exc.reason}") from exc

    # ------------------------------------------------------------------
    # Experiments
    # ------------------------------------------------------------------
    def create_experiment(
        self,
        name: str,
        artifact_location: str | None = None,
        tags: Mapping[str, Any] | Sequence[dict[str, Any]] | None = None,
    ) -> Experiment:
        if not self._remote:
            return self.service.create_experiment(name, artifact_location=artifact_location, tags=tags)
        body: dict[str, Any] = {"name": name}
        if artifact_location is not None:
            body["artifact_location"] = artifact_location
        if tags is not None:
            body["tags"] = _pairs_list(tags)
        response = self._request("POST", "/api/2.0/mlflow/experiments/create", body=body)
        return Experiment.from_dict(response["experiment"])

    def get_experiment(self, experiment_id: str) -> Experiment:
        if not self._remote:
            return self.service.get_experiment(experiment_id)
        response = self._request(
            "GET",
            "/api/2.0/mlflow/experiments/get",
            query={"experiment_id": experiment_id},
        )
        return Experiment.from_dict(response["experiment"])

    def get_experiment_by_name(self, name: str) -> Experiment | None:
        if not self._remote:
            return self.service.get_experiment_by_name(name)
        try:
            response = self._request(
                "GET",
                "/api/2.0/mlflow/experiments/get-by-name",
                query={"experiment_name": name},
            )
        except ResourceNotFound:
            return None
        return Experiment.from_dict(response["experiment"])

    def delete_experiment(self, experiment_id: str) -> None:
        if not self._remote:
            return self.service.delete_experiment(experiment_id)
        self._request("POST", "/api/2.0/mlflow/experiments/delete", body={"experiment_id": experiment_id})

    def restore_experiment(self, experiment_id: str) -> None:
        if not self._remote:
            return self.service.restore_experiment(experiment_id)
        self._request("POST", "/api/2.0/mlflow/experiments/restore", body={"experiment_id": experiment_id})

    def set_experiment_tag(self, experiment_id: str, key: str, value: Any) -> None:
        if not self._remote:
            return self.service.set_experiment_tag(experiment_id, key, value)
        self._request(
            "POST",
            "/api/2.0/mlflow/experiments/set-experiment-tag",
            body={"experiment_id": experiment_id, "key": key, "value": value},
        )

    def update_experiment(self, experiment_id: str, *, name: str | None = None) -> Experiment:
        if not self._remote:
            return self.service.update_experiment(experiment_id, name=name)
        body = {"experiment_id": experiment_id}
        if name is not None:
            body["name"] = name
        response = self._request("POST", "/api/2.0/mlflow/experiments/update", body=body)
        return Experiment.from_dict(response["experiment"])

    def search_experiments(
        self,
        filter_string: str | None = None,
        *,
        max_results: int = 1000,
        order_by: Sequence[str] | None = None,
        page_token: str | None = None,
    ) -> tuple[list[Experiment], str | None]:
        if not self._remote:
            return self.service.search_experiments(
                filter_string, max_results=max_results, order_by=order_by, page_token=page_token
            )
        body: dict[str, Any] = {"max_results": max_results}
        if filter_string:
            body["filter"] = filter_string
        if order_by:
            body["order_by"] = list(order_by)
        if page_token:
            body["page_token"] = page_token
        response = self._request("POST", "/api/2.0/mlflow/experiments/search", body=body)
        return (
            [Experiment.from_dict(item) for item in response.get("experiments", [])],
            response.get("next_page_token") or None,
        )

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
    ) -> Run:
        if not self._remote:
            return self.service.start_run(
                experiment_id=experiment_id,
                experiment_name=experiment_name,
                run_name=run_name,
                tags=tags,
                run_id=run_id,
            )
        if run_id:
            existing = self.get_run(run_id)
            if existing.info.is_terminal:
                raise InvalidParameter(
                    f"cannot resume run {run_id}: it is already {existing.info.status}"
                )
            return existing
        body: dict[str, Any] = {}
        if experiment_id:
            body["experiment_id"] = experiment_id
        if experiment_name:
            body["experiment_name"] = experiment_name
        if run_name:
            body["run_name"] = run_name
        if tags:
            body["tags"] = _pairs_list(tags)
        response = self._request("POST", "/api/2.0/mlflow/runs/create", body=body)
        return Run.from_dict(response["run"])

    # Alias for readers used to MlflowClient.create_run
    def create_run(
        self,
        experiment_id: str | None = None,
        *,
        run_name: str | None = None,
        tags: Mapping[str, Any] | Sequence[dict[str, Any]] | None = None,
        run_id: str | None = None,
    ) -> Run:
        return self.start_run(
            experiment_id=experiment_id, run_name=run_name, tags=tags, run_id=run_id
        )

    def get_run(self, run_id: str, *, include_deleted: bool = False) -> Run:
        if not self._remote:
            return self.service.get_run(run_id, include_deleted=include_deleted)
        response = self._request(
            "POST",
            "/api/2.0/mlflow/runs/get",
            body={"run_id": run_id, "include_deleted": include_deleted},
        )
        return Run.from_dict(response["run"])

    def end_run(self, run_id: str, status: str = "FINISHED") -> Any:
        if not self._remote:
            return self.service.end_run(run_id, status)
        response = self._request("POST", "/api/2.0/mlflow/runs/end", body={"run_id": run_id, "status": status})
        from .entities import RunInfo

        return RunInfo.from_dict(response["run_info"])

    def update_run(self, run_id: str, *, run_name: str | None = None, status: str | None = None) -> Any:
        if not self._remote:
            return self.service.update_run(run_id, run_name=run_name, status=status)
        body = {"run_id": run_id}
        if run_name is not None:
            body["run_name"] = run_name
        if status is not None:
            body["status"] = status
        response = self._request("POST", "/api/2.0/mlflow/runs/update", body=body)
        from .entities import RunInfo

        return RunInfo.from_dict(response["run_info"])

    def delete_run(self, run_id: str) -> None:
        if not self._remote:
            return self.service.delete_run(run_id)
        self._request("POST", "/api/2.0/mlflow/runs/delete", body={"run_id": run_id})

    def restore_run(self, run_id: str) -> None:
        if not self._remote:
            return self.service.restore_run(run_id)
        self._request("POST", "/api/2.0/mlflow/runs/restore", body={"run_id": run_id})

    def log_param(self, run_id: str, key: str, value: Any) -> None:
        self.log_batch(run_id, params={key: value})

    def log_params(self, run_id: str, params: Mapping[str, Any]) -> None:
        self.log_batch(run_id, params=params)

    def log_metric(
        self, run_id: str, key: str, value: Any, *, timestamp: int | None = None, step: int = 0
    ) -> None:
        if not self._remote:
            return self.service.log_metric(run_id, key, value, timestamp=timestamp, step=step)
        body = {"run_id": run_id, "key": key, "value": value, "step": step}
        if timestamp is not None:
            body["timestamp"] = timestamp
        self._request("POST", "/api/2.0/mlflow/runs/log-metric", body=body)

    def log_metrics(self, run_id: str, metrics: Mapping[str, Any], *, step: int = 0) -> None:
        self.log_batch(run_id, metrics={key: value for key, value in metrics.items()}, step=step)

    def log_batch(
        self,
        run_id: str,
        *,
        metrics: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
        tags: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
        step: int | None = None,
    ) -> None:
        if not self._remote:
            metric_list = _metric_list(metrics, step)
            return self.service.log_batch(run_id, metrics=metric_list, params=params, tags=tags)
        body: dict[str, Any] = {"run_id": run_id}
        metric_list = _metric_list(metrics, step)
        if metric_list:
            body["metrics"] = metric_list
        if params:
            body["params"] = [{"key": key, "value": value} for key, value in parse_pairs(params, what="params", stringify=False).items()]
        if tags:
            body["tags"] = [{"key": key, "value": value} for key, value in parse_pairs(tags, what="tags", stringify=False).items()]
        self._request("POST", "/api/2.0/mlflow/runs/log-batch", body=body)

    def set_tag(self, run_id: str, key: str, value: Any) -> None:
        if not self._remote:
            return self.service.set_tag(run_id, key, value)
        self._request(
            "POST",
            "/api/2.0/mlflow/runs/set-tag",
            body={"run_id": run_id, "key": key, "value": value},
        )

    def delete_tag(self, run_id: str, key: str) -> None:
        if not self._remote:
            return self.service.delete_tag(run_id, key)
        self._request(
            "POST", "/api/2.0/mlflow/runs/delete-tag", body={"run_id": run_id, "key": key}
        )

    def get_metric_history(self, run_id: str, key: str) -> list[Metric]:
        if not self._remote:
            return self.service.get_metric_history(run_id, key)
        response = self._request(
            "GET",
            "/api/2.0/mlflow/metrics/get-history",
            query={"run_id": run_id, "metric_key": key},
        )
        return [Metric.from_dict(item) for item in response.get("metrics", [])]

    def search_runs(
        self,
        filter_string: str | None = None,
        *,
        max_results: int = 1000,
        order_by: Sequence[str] | None = None,
        page_token: str | None = None,
        experiment_ids: Sequence[str | int] | None = None,
    ) -> tuple[list[Run], str | None]:
        if not self._remote:
            return self.service.search_runs(
                filter_string,
                max_results=max_results,
                order_by=order_by,
                page_token=page_token,
                experiment_ids=experiment_ids,
            )
        body: dict[str, Any] = {"max_results": max_results}
        if filter_string:
            body["filter"] = filter_string
        if experiment_ids is not None:
            body["experiment_ids"] = list(experiment_ids)
        if order_by:
            body["order_by"] = list(order_by)
        if page_token:
            body["page_token"] = page_token
        response = self._request("POST", "/api/2.0/mlflow/runs/search", body=body)
        return (
            [Run.from_dict(item) for item in response.get("runs", [])],
            response.get("next_page_token") or None,
        )

    def log_input(self, run_id: str, **kwargs: Any) -> None:
        if not self._remote:
            return self.service.log_input(run_id, **kwargs)
        body = {"run_id": run_id, **kwargs}
        self._request("POST", "/api/2.0/mlflow/runs/log-input", body=body)

    # ------------------------------------------------------------------
    # Artifacts
    # ------------------------------------------------------------------
    def log_artifact(self, run_id: str, local_path: str | Path, artifact_path: str | None = None) -> str:
        source = Path(local_path)
        if not source.is_file():
            raise ResourceNotFound(f"local artifact not found: {source}")
        if not self._remote:
            return self.service.log_artifact(run_id, source, artifact_path)
        prefix = (artifact_path or "").strip().strip("/")
        relative = f"{prefix}/{source.name}" if prefix else source.name
        return self._upload_file(run_id, relative, source.read_bytes())

    def log_artifacts(self, run_id: str, local_dir: str | Path, artifact_path: str | None = None) -> list[str]:
        source = Path(local_dir)
        if not source.is_dir():
            raise ResourceNotFound(f"local artifact directory not found: {source}")
        if not self._remote:
            return self.service.log_artifacts(run_id, source, artifact_path)
        prefix = (artifact_path or "").strip().strip("/")
        written: list[str] = []
        for path in sorted(source.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(source).as_posix()
            target = f"{prefix}/{relative}" if prefix else relative
            written.append(self._upload_file(run_id, target, path.read_bytes()))
        return written

    def log_text(self, run_id: str, text: str, artifact_file: str) -> str:
        if not self._remote:
            return self.service.log_text(run_id, text, artifact_file)
        return self._upload_bytes(run_id, artifact_file, text.encode("utf-8"))

    def log_dict(self, run_id: str, dictionary: Any, artifact_file: str) -> str:
        return self.log_text(run_id, json.dumps(dictionary, ensure_ascii=False, indent=2, sort_keys=True), artifact_file)

    def _upload_file(self, run_id: str, relative: str, data: bytes) -> str:
        return self._upload_bytes(run_id, relative, data)

    def _upload_bytes(self, run_id: str, relative: str, data: bytes) -> str:
        encoded = base64.b64encode(data).decode("ascii")
        response = self._request(
            "POST",
            "/api/2.0/mlflow/artifacts/log",
            body={"run_id": run_id, "path": relative, "content_b64": encoded},
        )
        return str(response.get("path", relative))

    def list_artifacts(self, run_id: str, path: str | None = None) -> list[FileInfo]:
        if not self._remote:
            return self.service.list_artifacts(run_id, path)
        response = self._request(
            "GET",
            "/api/2.0/mlflow/artifacts/list",
            query={"run_id": run_id, "path": path or ""},
        )
        return [FileInfo.from_dict(item) for item in response.get("files", [])]

    def read_artifact(self, run_id: str, path: str) -> bytes:
        if not self._remote:
            return self.service.read_artifact(run_id, path)
        return self._request(
            "GET",
            "/api/2.0/mlflow/artifacts/get",
            query={"run_id": run_id, "path": path},
            raw=True,
        )

    def download_artifacts(self, run_id: str, path: str | None, dst_path: str | Path) -> Path:
        if not self._remote:
            return self.service.download_artifacts(run_id, path, dst_path)
        destination = Path(dst_path)
        destination.mkdir(parents=True, exist_ok=True)
        relative = (path or "").strip().strip("/")
        listing = self.list_artifacts(run_id, relative or None)
        if not listing:
            raise ResourceNotFound(f"artifact path not found: {path!r}")
        if len(listing) == 1 and listing[0].path == relative and not listing[0].is_dir:
            target = _safe_download_child(destination, PurePosixPath(relative).name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.read_artifact(run_id, relative))
            return target
        for item in listing:
            child = _safe_download_child(destination, _artifact_basename(item.path))
            if item.is_dir:
                self.download_artifacts(run_id, item.path, child)
            else:
                child.parent.mkdir(parents=True, exist_ok=True)
                child.write_bytes(self.read_artifact(run_id, item.path))
        return destination

    # ------------------------------------------------------------------
    # Models
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
        name: str | None = None,
        model_type: str | None = None,
        params: Mapping[str, Any] | None = None,
        tags: Mapping[str, Any] | None = None,
    ) -> ModelInfo:
        if name is not None and self._remote:
            raise InvalidParameter(
                "remote log_model(name=...) requires the logged-model artifact upload "
                "endpoint, which MiniMLflow does not implement; use a local backend or "
                "create the logged model explicitly and log artifacts to its source run"
            )
        if not self._remote:
            return self.service.log_model(
                run_id,
                model,
                artifact_path=artifact_path,
                registered_model_name=registered_model_name,
                signature=signature,
                input_example=input_example,
                metadata=metadata,
                name=name,
                model_type=model_type,
                params=params,
                tags=tags,
            )
        with tempfile.TemporaryDirectory(prefix="mini-mlflow-client-model-") as tmp:
            saved = save_model(
                tmp,
                model,
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
                self.get_registered_model(registered_model_name)
            except ResourceNotFound:
                self.create_registered_model(
                    registered_model_name, description=f"Created from run {run_id}"
                )
            version = self.create_model_version(
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

    def load_model(self, model_uri: str, *, dst_path: str | Path | None = None) -> Any:
        return load_model_uri(model_uri, client=self, dst_path=dst_path)

    # ------------------------------------------------------------------
    # Logged models (MLflow 3)
    # ------------------------------------------------------------------
    def create_logged_model(
        self,
        name: str | None = None,
        *,
        experiment_id: str | None = None,
        model_type: str = "",
        source_run_id: str | None = None,
        tags: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        artifact_location: str | None = None,
    ) -> LoggedModel:
        if not self._remote:
            return self.service.create_logged_model(
                name,
                experiment_id=experiment_id,
                model_type=model_type,
                source_run_id=source_run_id,
                tags=tags,
                params=params,
                artifact_location=artifact_location,
            )
        body: dict[str, Any] = {
            "experiment_id": str(experiment_id if experiment_id is not None else 0),
            "name": name,
            "model_type": model_type,
            "source_run_id": source_run_id,
            "tags": _pairs_list(tags or {}),
            "params": _pairs_list(params or {}),
        }
        if artifact_location is not None:
            body["artifact_location"] = artifact_location
        response = self._request(
            "POST", "/api/2.0/mlflow/logged-models", body=body
        )
        return LoggedModel.from_dict(response["model"])

    def get_logged_model(self, model_id: str) -> LoggedModel:
        if not self._remote:
            return self.service.get_logged_model(model_id)
        response = self._request(
            "GET", f"/api/2.0/mlflow/logged-models/{quote(model_id, safe='')}"
        )
        return LoggedModel.from_dict(response["model"])

    def finalize_logged_model(
        self, model_id: str, status: str = "READY", *, status_message: str = ""
    ) -> LoggedModel:
        if not self._remote:
            return self.service.finalize_logged_model(
                model_id, status, status_message=status_message
            )
        response = self._request(
            "PATCH",
            f"/api/2.0/mlflow/logged-models/{quote(model_id, safe='')}",
            body={"status": status, "status_message": status_message},
        )
        return LoggedModel.from_dict(response["model"])

    def set_logged_model_tags(
        self, model_id: str, tags: Mapping[str, Any]
    ) -> None:
        if not self._remote:
            self.service.set_logged_model_tags(model_id, tags)
            return
        self._request(
            "PATCH",
            f"/api/2.0/mlflow/logged-models/{quote(model_id, safe='')}/tags",
            body={"tags": _pairs_list(tags)},
        )

    def delete_logged_model_tag(self, model_id: str, key: str) -> None:
        if not self._remote:
            self.service.delete_logged_model_tag(model_id, key)
            return
        self._request(
            "DELETE",
            f"/api/2.0/mlflow/logged-models/{quote(model_id, safe='')}"
            f"/tags/{quote(key, safe='')}",
        )

    def log_logged_model_params(
        self, model_id: str, params: Mapping[str, Any]
    ) -> LoggedModel:
        if not self._remote:
            return self.service.log_logged_model_params(model_id, params)
        response = self._request(
            "POST",
            f"/api/2.0/mlflow/logged-models/{quote(model_id, safe='')}/params",
            body={"params": _pairs_list(params)},
        )
        return LoggedModel.from_dict(response["model"])

    def log_logged_model_metrics(
        self,
        model_id: str,
        metrics: Mapping[str, Any] | Sequence[dict[str, Any]],
        *,
        step: int = 0,
        timestamp: int | None = None,
    ) -> LoggedModel:
        if not self._remote:
            return self.service.log_logged_model_metrics(
                model_id, metrics, step=step, timestamp=timestamp
            )
        if isinstance(metrics, Mapping):
            payload = [
                {"key": key, "value": value, "step": step, "timestamp": timestamp}
                for key, value in metrics.items()
            ]
        else:
            payload = list(metrics)
        response = self._request(
            "POST",
            f"/api/2.0/mlflow/logged-models/{quote(model_id, safe='')}/metrics",
            body={"metrics": payload},
        )
        return LoggedModel.from_dict(response["model"])

    def search_logged_models(
        self,
        experiment_ids: Sequence[str | int] | None = None,
        *,
        filter_string: str | None = None,
        max_results: int = 100,
        order_by: Sequence[str] | None = None,
        page_token: str | None = None,
    ) -> tuple[list[LoggedModel], str | None]:
        if not self._remote:
            return self.service.search_logged_models(
                experiment_ids,
                filter_string=filter_string,
                max_results=max_results,
                order_by=order_by,
                page_token=page_token,
            )
        body: dict[str, Any] = {
            "filter": filter_string,
            "max_results": max_results,
            "order_by": list(order_by or []),
            "page_token": page_token,
        }
        if experiment_ids is not None:
            body["experiment_ids"] = [str(item) for item in experiment_ids]
        response = self._request(
            "POST", "/api/2.0/mlflow/logged-models/search", body=body
        )
        return (
            [LoggedModel.from_dict(item) for item in response.get("models", [])],
            response.get("next_page_token"),
        )

    def delete_logged_model(self, model_id: str) -> None:
        if not self._remote:
            self.service.delete_logged_model(model_id)
            return
        self._request(
            "DELETE", f"/api/2.0/mlflow/logged-models/{quote(model_id, safe='')}"
        )

    # ------------------------------------------------------------------
    # Model registry
    # ------------------------------------------------------------------
    def create_registered_model(
        self,
        name: str,
        *,
        description: str = "",
        tags: Mapping[str, Any] | None = None,
    ) -> RegisteredModel:
        if not self._remote:
            return self.service.registry.create_registered_model(name, description=description, tags=dict(tags or {}))
        response = self._request(
            "POST",
            "/api/2.0/mlflow/registered-models/create",
            body={"name": name, "description": description, "tags": _pairs_list(tags or {})},
        )
        return RegisteredModel.from_dict(response["registered_model"])

    def get_registered_model(self, name: str) -> RegisteredModel:
        if not self._remote:
            return self.service.registry.get_registered_model(name)
        response = self._request(
            "GET", "/api/2.0/mlflow/registered-models/get", query={"name": name}
        )
        return RegisteredModel.from_dict(response["registered_model"])

    def rename_registered_model(self, name: str, new_name: str) -> RegisteredModel:
        if not self._remote:
            return self.service.registry.rename_registered_model(name, new_name)
        response = self._request(
            "POST",
            "/api/2.0/mlflow/registered-models/rename",
            body={"name": name, "new_name": new_name},
        )
        return RegisteredModel.from_dict(response["registered_model"])

    def update_registered_model(self, name: str, *, description: str | None = None) -> RegisteredModel:
        if not self._remote:
            return self.service.registry.update_registered_model(name, description=description)
        body = {"name": name}
        if description is not None:
            body["description"] = description
        response = self._request(
            "POST", "/api/2.0/mlflow/registered-models/update", body=body
        )
        return RegisteredModel.from_dict(response["registered_model"])

    def delete_registered_model(self, name: str, *, force: bool = False) -> None:
        if not self._remote:
            return self.service.registry.delete_registered_model(name, force=force)
        self._request(
            "POST",
            "/api/2.0/mlflow/registered-models/delete",
            body={"name": name, "force": force},
        )

    def set_registered_model_tag(self, name: str, key: str, value: Any) -> None:
        if not self._remote:
            return self.service.registry.set_registered_model_tag(name, key, value)
        self._request(
            "POST",
            "/api/2.0/mlflow/registered-models/set-tag",
            body={"name": name, "key": key, "value": value},
        )

    def delete_registered_model_tag(self, name: str, key: str) -> None:
        if not self._remote:
            return self.service.registry.delete_registered_model_tag(name, key)
        self._request(
            "POST",
            "/api/2.0/mlflow/registered-models/delete-tag",
            body={"name": name, "key": key},
        )

    def search_registered_models(
        self,
        filter_string: str | None = None,
        *,
        max_results: int = 1000,
        order_by: Sequence[str] | None = None,
    ) -> list[RegisteredModel]:
        if not self._remote:
            return self.service.registry.search_registered_models(
                filter_string, max_results=max_results, order_by=order_by
            )
        body: dict[str, Any] = {"max_results": max_results}
        if filter_string:
            body["filter"] = filter_string
        if order_by:
            body["order_by"] = list(order_by)
        response = self._request(
            "POST", "/api/2.0/mlflow/registered-models/search", body=body
        )
        return [RegisteredModel.from_dict(item) for item in response.get("registered_models", [])]

    def create_model_version(
        self,
        name: str,
        *,
        source: str,
        run_id: str = "",
        description: str = "",
        tags: Mapping[str, Any] | None = None,
    ) -> ModelVersion:
        if not self._remote:
            return self.service.registry.create_model_version(
                name, source=source, run_id=run_id, description=description, tags=dict(tags or {})
            )
        response = self._request(
            "POST",
            "/api/2.0/mlflow/model-versions/create",
            body={
                "name": name,
                "source": source,
                "run_id": run_id,
                "description": description,
                "tags": _pairs_list(tags or {}),
            },
        )
        return ModelVersion.from_dict(response["model_version"])

    def get_model_version(self, name: str, version: int | str) -> ModelVersion:
        if not self._remote:
            return self.service.registry.get_model_version(name, version)
        response = self._request(
            "GET",
            "/api/2.0/mlflow/model-versions/get",
            query={"name": name, "version": version},
        )
        return ModelVersion.from_dict(response["model_version"])

    def search_model_versions(
        self,
        filter_string: str | None = None,
        *,
        name: str | None = None,
        max_results: int = 1000,
        order_by: Sequence[str] | None = None,
    ) -> list[ModelVersion]:
        if not self._remote:
            return self.service.registry.search_model_versions(
                filter_string, name=name, max_results=max_results, order_by=order_by
            )
        body: dict[str, Any] = {"max_results": max_results}
        if filter_string:
            body["filter"] = filter_string
        if name:
            body["name"] = name
        if order_by:
            body["order_by"] = list(order_by)
        response = self._request(
            "POST", "/api/2.0/mlflow/model-versions/search", body=body
        )
        return [ModelVersion.from_dict(item) for item in response.get("model_versions", [])]

    def transition_model_version_stage(self, name: str, version: int, stage: str) -> ModelVersion:
        if not self._remote:
            return self.service.registry.transition_model_version_stage(name, version, stage)
        response = self._request(
            "POST",
            "/api/2.0/mlflow/model-versions/transition-stage",
            body={"name": name, "version": version, "stage": stage},
        )
        return ModelVersion.from_dict(response["model_version"])

    def set_registered_model_alias(self, name: str, alias: str, version: int) -> None:
        if not self._remote:
            return self.service.registry.set_registered_model_alias(name, alias, version)
        self._request(
            "POST",
            "/api/2.0/mlflow/registered-models/aliases/set",
            body={"name": name, "alias": alias, "version": version},
        )

    def delete_registered_model_alias(self, name: str, alias: str) -> None:
        if not self._remote:
            return self.service.registry.delete_registered_model_alias(name, alias)
        self._request(
            "POST",
            "/api/2.0/mlflow/registered-models/aliases/delete",
            body={"name": name, "alias": alias},
        )

    def get_model_version_by_alias(self, name: str, alias: str) -> ModelVersion:
        if not self._remote:
            return self.service.registry.get_model_version_by_alias(name, alias)
        response = self._request(
            "GET",
            "/api/2.0/mlflow/model-versions/get-by-alias",
            query={"name": name, "alias": alias},
        )
        return ModelVersion.from_dict(response["model_version"])

    def set_model_version_tag(self, name: str, version: int, key: str, value: Any) -> None:
        if not self._remote:
            return self.service.registry.set_model_version_tag(name, version, key, value)
        self._request(
            "POST",
            "/api/2.0/mlflow/model-versions/set-tag",
            body={"name": name, "version": version, "key": key, "value": value},
        )

    def delete_model_version_tag(self, name: str, version: int, key: str) -> None:
        if not self._remote:
            return self.service.registry.delete_model_version_tag(name, version, key)
        self._request(
            "POST",
            "/api/2.0/mlflow/model-versions/delete-tag",
            body={"name": name, "version": version, "key": key},
        )

    def update_model_version(self, name: str, version: int, *, description: str | None = None) -> ModelVersion:
        if not self._remote:
            return self.service.registry.update_model_version(name, version, description=description)
        body = {"name": name, "version": version}
        if description is not None:
            body["description"] = description
        response = self._request(
            "POST", "/api/2.0/mlflow/model-versions/update", body=body
        )
        return ModelVersion.from_dict(response["model_version"])

    def delete_model_version(self, name: str, version: int) -> None:
        if not self._remote:
            return self.service.registry.delete_model_version(name, version)
        self._request(
            "POST",
            "/api/2.0/mlflow/model-versions/delete",
            body={"name": name, "version": version},
        )

    # ------------------------------------------------------------------
    # Traces
    # ------------------------------------------------------------------
    def log_trace(self, trace: Trace) -> Trace:
        if not self._remote:
            return self.service.log_trace(trace)
        response = self._request(
            "POST", "/api/2.0/mlflow/traces/log", body={"trace": trace.to_dict()}
        )
        return Trace.from_dict(response["trace"])

    def get_trace(self, trace_id: str) -> Trace:
        if not self._remote:
            return self.service.get_trace(trace_id)
        response = self._request(
            "GET", "/api/2.0/mlflow/traces/get", query={"trace_id": trace_id}
        )
        return Trace.from_dict(response["trace"])

    def search_traces(
        self,
        experiment_ids: Sequence[str] | None = None,
        *,
        filter_string: str | None = None,
        max_results: int = 100,
        order_by: Sequence[str] | None = None,
    ) -> list[Trace]:
        if not self._remote:
            return self.service.search_traces(
                experiment_ids,
                filter_string=filter_string,
                max_results=max_results,
                order_by=order_by,
            )
        body: dict[str, Any] = {"max_results": max_results}
        if experiment_ids:
            body["experiment_ids"] = list(experiment_ids)
        if filter_string:
            body["filter"] = filter_string
        if order_by:
            body["order_by"] = list(order_by)
        response = self._request("POST", "/api/2.0/mlflow/traces/search", body=body)
        return [Trace.from_dict(item) for item in response.get("traces", [])]

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
        if not self._remote:
            return self.service.log_feedback(
                trace_id,
                name,
                value,
                source_type=source_type,
                source_id=source_id,
                rationale=rationale,
                metadata=metadata,
                is_expectation=is_expectation,
                valid=valid,
            )
        body = {
            "trace_id": trace_id,
            "name": name,
            "value": value,
            "source_type": source_type,
            "source_id": source_id,
            "rationale": rationale,
            "metadata": dict(metadata or {}),
            "is_expectation": is_expectation,
            "valid": valid,
        }
        response = self._request(
            "POST", "/api/2.0/mlflow/traces/assessments/log", body=body
        )
        return Assessment.from_dict(response["assessment"])

    def get_assessment(self, assessment_id: str) -> Assessment:
        if not self._remote:
            return self.service.get_assessment(assessment_id)
        response = self._request(
            "GET",
            "/api/2.0/mlflow/traces/assessments/get",
            query={"assessment_id": assessment_id},
        )
        return Assessment.from_dict(response["assessment"])

    def list_assessments(self, trace_id: str) -> list[Assessment]:
        if not self._remote:
            return self.service.list_assessments(trace_id)
        response = self._request(
            "GET",
            "/api/2.0/mlflow/traces/assessments/list",
            query={"trace_id": trace_id},
        )
        return [Assessment.from_dict(item) for item in response.get("assessments", [])]

    def delete_assessment(self, assessment_id: str) -> None:
        if not self._remote:
            return self.service.delete_assessment(assessment_id)
        self._request(
            "POST",
            "/api/2.0/mlflow/traces/assessments/delete",
            body={"assessment_id": assessment_id},
        )

    # ------------------------------------------------------------------
    # Prompt registry
    # ------------------------------------------------------------------
    def register_prompt(
        self,
        name: str,
        template: str,
        *,
        commit_message: str = "",
        tags: Mapping[str, Any] | None = None,
    ) -> PromptVersion:
        if not self._remote:
            return self.service.prompts.register_prompt(
                name, template, commit_message=commit_message, tags=tags
            )
        response = self._request(
            "POST",
            "/api/2.0/mlflow/prompts/create",
            body={
                "name": name,
                "template": template,
                "commit_message": commit_message,
                "tags": _pairs_list(tags or {}),
            },
        )
        return PromptVersion.from_dict(response["prompt"])

    def load_prompt(
        self, uri: str, *, version: int | str | None = None
    ) -> PromptVersion:
        if not self._remote:
            return self.service.prompts.load(uri, version)
        text = str(uri).strip()
        if text.startswith("prompts:/"):
            text = text[len("prompts:/") :]
        name = text
        alias: str | None = None
        if "@" in text:
            name, alias = text.split("@", 1)
            if version is not None:
                raise InvalidParameter("pass a prompt version or an alias, not both")
        elif "/" in text:
            prefix, _, suffix = text.rpartition("/")
            if prefix and suffix.isdigit():
                name, version = prefix, int(suffix)
            else:
                raise InvalidParameter("prompt URI path must be name/version or name@alias")
        query: dict[str, Any] = {"name": name}
        if alias:
            query["alias"] = alias
        elif version is not None:
            query["version"] = version
        response = self._request(
            "GET", "/api/2.0/mlflow/prompts/get", query=query
        )
        return PromptVersion.from_dict(response["prompt"])

    def list_prompt_versions(self, name: str) -> list[PromptVersion]:
        if not self._remote:
            return self.service.prompts.list_versions(name)
        response = self._request(
            "GET",
            "/api/2.0/mlflow/prompts/versions/list",
            query={"name": name},
        )
        return [PromptVersion.from_dict(item) for item in response.get("prompt_versions", [])]

    def search_prompts(
        self,
        filter_string: str | None = None,
        *,
        max_results: int = 1000,
        order_by: Sequence[str] | None = None,
    ) -> list[PromptVersion]:
        if not self._remote:
            return self.service.prompts.search_prompts(
                filter_string, max_results=max_results, order_by=order_by
            )
        body: dict[str, Any] = {"max_results": max_results}
        if filter_string:
            body["filter"] = filter_string
        if order_by:
            body["order_by"] = list(order_by)
        response = self._request(
            "POST", "/api/2.0/mlflow/prompts/search", body=body
        )
        return [PromptVersion.from_dict(item) for item in response.get("prompts", [])]

    def set_prompt_alias(self, name: str, alias: str, version: int) -> None:
        if not self._remote:
            return self.service.prompts.set_alias(name, alias, version)
        self._request(
            "POST",
            "/api/2.0/mlflow/prompts/aliases/set",
            body={"name": name, "alias": alias, "version": version},
        )

    def delete_prompt_alias(self, name: str, alias: str) -> None:
        if not self._remote:
            return self.service.prompts.delete_alias(name, alias)
        self._request(
            "POST",
            "/api/2.0/mlflow/prompts/aliases/delete",
            body={"name": name, "alias": alias},
        )

    def delete_prompt(self, name: str) -> None:
        if not self._remote:
            return self.service.prompts.delete_prompt(name)
        self._request(
            "POST", "/api/2.0/mlflow/prompts/delete", body={"name": name}
        )

    # ------------------------------------------------------------------
    # Evaluation datasets
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
        if not self._remote:
            return self.service.datasets.create_dataset(
                name,
                records,
                source=source,
                source_type=source_type,
                tags=tags,
            )
        response = self._request(
            "POST",
            "/api/2.0/mlflow/datasets/create",
            body={
                "name": name,
                "records": list(records),
                "source": source,
                "source_type": source_type,
                "tags": _pairs_list(tags or {}),
            },
        )
        return Dataset.from_dict(response["dataset"])

    def get_dataset(self, dataset_id: str) -> Dataset:
        if not self._remote:
            return self.service.datasets.get_dataset(dataset_id)
        response = self._request(
            "GET",
            "/api/2.0/mlflow/datasets/get",
            query={"dataset_id": dataset_id},
        )
        return Dataset.from_dict(response["dataset"])

    def search_datasets(
        self,
        filter_string: str | None = None,
        *,
        max_results: int = 100,
        order_by: Sequence[str] | None = None,
    ) -> list[Dataset]:
        if not self._remote:
            return self.service.datasets.search_datasets(
                filter_string, max_results=max_results, order_by=order_by
            )
        body: dict[str, Any] = {"max_results": max_results}
        if filter_string:
            body["filter"] = filter_string
        if order_by:
            body["order_by"] = list(order_by)
        response = self._request(
            "POST", "/api/2.0/mlflow/datasets/search", body=body
        )
        return [Dataset.from_dict(item) for item in response.get("datasets", [])]

    def merge_dataset_records(
        self, dataset_id: str, records: Sequence[Mapping[str, Any]]
    ) -> Dataset:
        if not self._remote:
            return self.service.datasets.merge_records(dataset_id, records)
        response = self._request(
            "POST",
            "/api/2.0/mlflow/datasets/merge-records",
            body={"dataset_id": dataset_id, "records": list(records)},
        )
        return Dataset.from_dict(response["dataset"])

    def delete_dataset(self, dataset_id: str) -> None:
        if not self._remote:
            return self.service.datasets.delete_dataset(dataset_id)
        self._request(
            "POST",
            "/api/2.0/mlflow/datasets/delete",
            body={"dataset_id": dataset_id},
        )

    # ------------------------------------------------------------------
    # Model monitoring
    # ------------------------------------------------------------------
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
        if not self._remote:
            return self.service.monitoring.create_monitor(
                name,
                baseline_records,
                features,
                model_name=model_name,
                model_version=model_version,
                dataset_id=dataset_id,
                thresholds=thresholds,
                tags=tags,
            )
        body: dict[str, Any] = {
            "name": name,
            "baseline_records": list(baseline_records),
            "features": dict(features),
            "model_name": model_name,
            "model_version": model_version,
            "dataset_id": dataset_id,
            "tags": _pairs_list(tags or {}),
        }
        if thresholds is not None:
            body["thresholds"] = dict(thresholds)
        response = self._request(
            "POST", "/api/2.0/mlflow/monitors/create", body=body
        )
        return Monitor.from_dict(response["monitor"])

    def get_monitor(self, monitor_id: str) -> Monitor:
        if not self._remote:
            return self.service.monitoring.get_monitor(monitor_id)
        response = self._request(
            "GET",
            "/api/2.0/mlflow/monitors/get",
            query={"monitor_id": monitor_id},
        )
        return Monitor.from_dict(response["monitor"])

    def list_monitors(self) -> list[Monitor]:
        if not self._remote:
            return self.service.monitoring.list_monitors()
        response = self._request("GET", "/api/2.0/mlflow/monitors/list")
        return [Monitor.from_dict(item) for item in response.get("monitors", [])]

    def delete_monitor(self, monitor_id: str) -> None:
        if not self._remote:
            return self.service.monitoring.delete_monitor(monitor_id)
        self._request(
            "POST",
            "/api/2.0/mlflow/monitors/delete",
            body={"monitor_id": monitor_id},
        )

    def record_monitor_window(
        self,
        monitor_id: str,
        records: Sequence[Mapping[str, Any]],
        *,
        window_start: int = 0,
        window_end: int = 0,
    ) -> MonitorWindow:
        if not self._remote:
            return self.service.monitoring.record_window(
                monitor_id,
                records,
                window_start=window_start,
                window_end=window_end,
            )
        response = self._request(
            "POST",
            "/api/2.0/mlflow/monitors/windows/create",
            body={
                "monitor_id": monitor_id,
                "records": list(records),
                "window_start": window_start,
                "window_end": window_end,
            },
        )
        return MonitorWindow.from_dict(response["window"])

    def list_monitor_windows(
        self, monitor_id: str, *, limit: int = 100
    ) -> list[MonitorWindow]:
        if not self._remote:
            return self.service.monitoring.list_windows(monitor_id, limit=limit)
        response = self._request(
            "GET",
            "/api/2.0/mlflow/monitors/windows/list",
            query={"monitor_id": monitor_id, "limit": limit},
        )
        return [MonitorWindow.from_dict(item) for item in response.get("windows", [])]

    # ------------------------------------------------------------------
    # Webhooks
    # ------------------------------------------------------------------
    def create_webhook(
        self,
        name: str,
        url: str,
        events: Sequence[str],
        *,
        secret: str = "",
        description: str = "",
    ) -> Webhook:
        if not self._remote:
            return self.service.webhooks.create_webhook(
                name, url, events, secret=secret, description=description
            )
        response = self._request(
            "POST",
            "/api/2.0/mlflow/webhooks/create",
            body={
                "name": name,
                "url": url,
                "events": list(events),
                "secret": secret,
                "description": description,
            },
        )
        return Webhook.from_dict(response["webhook"])

    def get_webhook(self, webhook_id: str) -> Webhook:
        if not self._remote:
            return self.service.webhooks.get_webhook(webhook_id)
        response = self._request(
            "GET",
            "/api/2.0/mlflow/webhooks/get",
            query={"webhook_id": webhook_id},
        )
        return Webhook.from_dict(response["webhook"])

    def list_webhooks(self) -> list[Webhook]:
        if not self._remote:
            return self.service.webhooks.list_webhooks()
        response = self._request("GET", "/api/2.0/mlflow/webhooks/list")
        return [Webhook.from_dict(item) for item in response.get("webhooks", [])]

    def update_webhook(
        self,
        webhook_id: str,
        *,
        name: str | None = None,
        url: str | None = None,
        events: Sequence[str] | None = None,
        secret: str | None = None,
        description: str | None = None,
        status: str | None = None,
    ) -> Webhook:
        if not self._remote:
            return self.service.webhooks.update_webhook(
                webhook_id,
                name=name,
                url=url,
                events=events,
                secret=secret,
                description=description,
                status=status,
            )
        body: dict[str, Any] = {"webhook_id": webhook_id}
        for key, value in {
            "name": name,
            "url": url,
            "events": list(events) if events is not None else None,
            "secret": secret,
            "description": description,
            "status": status,
        }.items():
            if value is not None:
                body[key] = value
        response = self._request(
            "POST", "/api/2.0/mlflow/webhooks/update", body=body
        )
        return Webhook.from_dict(response["webhook"])

    def delete_webhook(self, webhook_id: str) -> None:
        if not self._remote:
            return self.service.webhooks.delete_webhook(webhook_id)
        self._request(
            "POST",
            "/api/2.0/mlflow/webhooks/delete",
            body={"webhook_id": webhook_id},
        )

    def test_webhook(self, webhook_id: str) -> WebhookDelivery:
        if not self._remote:
            return self.service.webhooks.test_webhook(webhook_id)
        response = self._request(
            "POST",
            "/api/2.0/mlflow/webhooks/test",
            body={"webhook_id": webhook_id},
        )
        return WebhookDelivery.from_dict(response["delivery"])

    def list_webhook_deliveries(
        self, *, webhook_id: str | None = None, limit: int = 100
    ) -> list[WebhookDelivery]:
        if not self._remote:
            return self.service.webhooks.list_deliveries(
                webhook_id=webhook_id, limit=limit
            )
        query: dict[str, Any] = {"limit": limit}
        if webhook_id is not None:
            query["webhook_id"] = webhook_id
        response = self._request(
            "GET", "/api/2.0/mlflow/webhooks/deliveries", query=query
        )
        return [
            WebhookDelivery.from_dict(item) for item in response.get("deliveries", [])
        ]

    # ------------------------------------------------------------------
    # Gateway (remote servers may expose one)
    # ------------------------------------------------------------------
    def gateway_routes(self) -> dict[str, Any]:
        if not self._remote:
            raise InvalidParameter("a local client has no gateway; connect to a server")
        return self._request("GET", "/gateway/routes")

    def gateway_stats(self) -> dict[str, Any]:
        if not self._remote:
            raise InvalidParameter("a local client has no gateway; connect to a server")
        return self._request("GET", "/gateway/stats")

    def gateway_invoke(
        self,
        route: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self._remote:
            raise InvalidParameter("a local client has no gateway; connect to a server")
        return self._request(
            "POST",
            "/gateway/invocations",
            body={
                "route": route,
                "messages": list(messages),
                "params": dict(params or {}),
            },
        )

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        if not self._remote:
            return {"status": "ok", "mode": "local", "tracking_uri": self.tracking_uri}
        return self._request("GET", "/health")

    def close(self) -> None:
        if self._service is not None:
            self._service.close()


def _pairs_list(value: Mapping[str, Any] | Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    mapping = parse_pairs(value, what="tags", stringify=False)
    return [{"key": key, "value": value} for key, value in mapping.items()]


def _metric_list(
    metrics: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None,
    step: int | None,
) -> list[dict[str, Any]]:
    if metrics is None:
        return []
    if isinstance(metrics, Mapping):
        result = []
        for key, value in metrics.items():
            item: dict[str, Any] = {"key": key, "value": value}
            if step is not None:
                item["step"] = step
            result.append(item)
        return result
    return [dict(item) for item in metrics]


def _artifact_basename(path: str) -> str:
    text = str(path)
    if "\\" in text or "\x00" in text:
        raise InvalidParameter("server returned an unsafe artifact path")
    pure = PurePosixPath(text)
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        raise InvalidParameter("server returned an unsafe artifact path")
    name = pure.name
    if not name or name in {".", ".."} or ":" in name or re.search(r"[\x00-\x1f\x7f]", name):
        raise InvalidParameter("server returned an unsafe artifact name")
    return name


def _safe_download_child(destination: Path, name: str) -> Path:
    base = destination.resolve()
    target = (base / name).resolve(strict=False)
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise InvalidParameter("artifact download escapes the destination directory") from exc
    return target


# Convenience aliases for readers moving from the real client.
MlflowClient = MiniClient

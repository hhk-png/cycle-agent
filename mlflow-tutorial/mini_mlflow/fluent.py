"""Fluent API with context-local active runs.

The fluent API is a convenience layer over ``MiniClient``.  It is intentionally
not magic: an active run is stored in a ``ContextVar``, so concurrent threads
do not accidentally log to each other's runs, and asyncio tasks inherit the
binding at task creation but can override it independently.

Cross-process behavior is explicit: a child process must resume with
``start_run(run_id=os.environ["MINI_MLFLOW_RUN_ID"])``.  We do not silently
read that variable, because a stray environment variable should not change the
destination of a run.
"""

from __future__ import annotations

import os
from contextvars import ContextVar, Token
from typing import Any, Mapping, Sequence

from .client import MiniClient
from .entities import Assessment, LoggedModel, PromptVersion, Run
from .errors import InvalidParameter, ResourceNotFound
from .models import ModelInfo

_ACTIVE_RUNS: ContextVar[tuple["ActiveRun", ...]] = ContextVar("mini_mlflow_active_runs", default=())
_DEFAULT_CLIENT: MiniClient | None = None
_TRACKING_URI_OVERRIDE: str | None = None


class ActiveRun:
    def __init__(self, client: MiniClient, run: Run) -> None:
        self.client = client
        self.run = run
        self._token: Token[tuple["ActiveRun", ...]] | None = None
        self._ended = False
        self._exit_status: str | None = None

    @property
    def info(self):
        return self.run.info

    def __getattr__(self, name: str) -> Any:
        return getattr(self.run, name)

    def __enter__(self) -> "ActiveRun":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        status = "FAILED" if exc_type is not None else "FINISHED"
        try:
            if not self._ended:
                self.end(status)
        finally:
            previous = _ACTIVE_RUNS.get()
            if previous and previous[-1] is self:
                _ACTIVE_RUNS.set(previous[:-1])
            elif self._token is not None:
                try:
                    _ACTIVE_RUNS.reset(self._token)
                except ValueError:
                    pass
            self._token = None
        return False

    def end(self, status: str | None = None) -> None:
        if self._ended:
            return
        status = (status or self._exit_status or "FINISHED").upper()
        if status not in {"FINISHED", "FAILED", "KILLED"}:
            raise InvalidParameter("status must be FINISHED, FAILED or KILLED")
        self.client.end_run(self.run.info.run_id, status)
        self._ended = True
        self._exit_status = status


def get_default_client() -> MiniClient:
    global _DEFAULT_CLIENT
    if _TRACKING_URI_OVERRIDE:
        if _DEFAULT_CLIENT is None or _DEFAULT_CLIENT.tracking_uri != _normalize_uri(_TRACKING_URI_OVERRIDE):
            if _DEFAULT_CLIENT is not None:
                _DEFAULT_CLIENT.close()
            _DEFAULT_CLIENT = MiniClient(_TRACKING_URI_OVERRIDE)
        return _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = MiniClient()
    return _DEFAULT_CLIENT


def _normalize_uri(uri: str) -> str:
    if uri.startswith(("http://", "https://")):
        return uri.rstrip("/")
    from .config import MiniConfig

    return MiniConfig.from_env(backend_store_uri=uri).tracking_uri


def set_tracking_uri(uri: str) -> None:
    global _DEFAULT_CLIENT, _TRACKING_URI_OVERRIDE
    if not uri:
        raise InvalidParameter("tracking URI must not be empty")
    if _ACTIVE_RUNS.get():
        raise InvalidParameter("cannot change the tracking URI while a run is active")
    if _DEFAULT_CLIENT is not None:
        _DEFAULT_CLIENT.close()
    _DEFAULT_CLIENT = None
    _TRACKING_URI_OVERRIDE = uri
    os.environ["MINI_MLFLOW_TRACKING_URI"] = uri


def get_tracking_uri() -> str:
    return get_default_client().tracking_uri


def get_active_run() -> ActiveRun | None:
    stack = _ACTIVE_RUNS.get()
    return stack[-1] if stack else None


def _require_active() -> ActiveRun:
    active = get_active_run()
    if active is None:
        raise InvalidParameter("no active run; call start_run() first or pass a run_id to the client")
    return active


def start_run(
    run_id: str | None = None,
    experiment_id: str | None = None,
    run_name: str | None = None,
    tags: Mapping[str, Any] | Sequence[dict[str, Any]] | None = None,
    *,
    experiment_name: str | None = None,
    nested: bool = False,
    client: MiniClient | None = None,
) -> ActiveRun:
    active = get_active_run()
    if active is not None and not nested:
        raise InvalidParameter(
            "a run is already active in this context; end it first or pass nested=True"
        )
    client = client or get_default_client()
    effective_tags = dict(tags) if isinstance(tags, Mapping) else {
        str(item["key"]): item.get("value") for item in (tags or []) if isinstance(item, dict) and "key" in item
    }
    if nested and active is not None and "mlflow.parentRunId" not in effective_tags:
        effective_tags["mlflow.parentRunId"] = active.run.info.run_id
    if run_id:
        run = client.start_run(run_id=run_id)
    else:
        if experiment_id is None and experiment_name is None:
            experiment_id = os.environ.get("MINI_MLFLOW_EXPERIMENT_ID")
        run = client.start_run(
            experiment_id=experiment_id,
            experiment_name=experiment_name,
            run_name=run_name,
            tags=effective_tags,
        )
    wrapper = ActiveRun(client, run)
    stack = _ACTIVE_RUNS.get()
    wrapper._token = _ACTIVE_RUNS.set((*stack, wrapper))
    return wrapper


def end_run(status: str = "FINISHED") -> None:
    active = _require_active()
    active.end(status)


def log_param(key: str, value: Any) -> None:
    active = _require_active()
    active.client.log_param(active.run.info.run_id, key, value)


def log_params(params: Mapping[str, Any]) -> None:
    active = _require_active()
    active.client.log_params(active.run.info.run_id, params)


def log_metric(key: str, value: Any, step: int = 0, timestamp: int | None = None) -> None:
    active = _require_active()
    active.client.log_metric(active.run.info.run_id, key, value, step=step, timestamp=timestamp)


def log_metrics(metrics: Mapping[str, Any], step: int = 0) -> None:
    active = _require_active()
    active.client.log_metrics(active.run.info.run_id, metrics, step=step)


def set_tag(key: str, value: Any) -> None:
    active = _require_active()
    active.client.set_tag(active.run.info.run_id, key, value)


def delete_tag(key: str) -> None:
    active = _require_active()
    active.client.delete_tag(active.run.info.run_id, key)


def log_artifact(local_path: str, artifact_path: str | None = None) -> str:
    active = _require_active()
    return active.client.log_artifact(active.run.info.run_id, local_path, artifact_path)


def log_artifacts(local_dir: str, artifact_path: str | None = None) -> list[str]:
    active = _require_active()
    return active.client.log_artifacts(active.run.info.run_id, local_dir, artifact_path)


def log_text(text: str, artifact_file: str) -> str:
    active = _require_active()
    return active.client.log_text(active.run.info.run_id, text, artifact_file)


def log_dict(dictionary: Any, artifact_file: str) -> str:
    active = _require_active()
    return active.client.log_dict(active.run.info.run_id, dictionary, artifact_file)


def log_input(**kwargs: Any) -> None:
    active = _require_active()
    active.client.log_input(active.run.info.run_id, **kwargs)


def log_model(
    model: Any,
    artifact_path: str = "model",
    *,
    registered_model_name: str | None = None,
    signature: Any = None,
    input_example: Any = None,
    metadata: dict[str, Any] | None = None,
    name: str | None = None,
    model_type: str | None = None,
    params: Mapping[str, Any] | None = None,
    tags: Mapping[str, Any] | None = None,
) -> ModelInfo:
    active = _require_active()
    return active.client.log_model(
        active.run.info.run_id,
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


# ----------------------------------------------------------------------
# MLflow 3 logged models
# ----------------------------------------------------------------------
def initialize_logged_model(
    name: str | None = None,
    *,
    source_run_id: str | None = None,
    tags: Mapping[str, Any] | None = None,
    params: Mapping[str, Any] | None = None,
    model_type: str | None = None,
    experiment_id: str | None = None,
) -> LoggedModel:
    client = get_default_client()
    active = get_active_run()
    if active is not None:
        if experiment_id is None:
            experiment_id = active.run.info.experiment_id
        if source_run_id is None:
            source_run_id = active.run.info.run_id
    return client.create_logged_model(
        name,
        experiment_id=experiment_id,
        model_type=model_type or "",
        source_run_id=source_run_id,
        tags=tags,
        params=params,
    )


def get_logged_model(model_id: str) -> LoggedModel:
    return get_default_client().get_logged_model(model_id)


def finalize_logged_model(
    model_id: str,
    status: str = "READY",
    *,
    status_message: str = "",
) -> LoggedModel:
    return get_default_client().finalize_logged_model(
        model_id, status, status_message=status_message
    )


def set_logged_model_tags(model_id: str, tags: Mapping[str, Any]) -> None:
    get_default_client().set_logged_model_tags(model_id, tags)


def delete_logged_model_tag(model_id: str, key: str) -> None:
    get_default_client().delete_logged_model_tag(model_id, key)


def log_logged_model_params(
    model_id: str, params: Mapping[str, Any]
) -> LoggedModel:
    return get_default_client().log_logged_model_params(model_id, params)


def log_logged_model_metrics(
    model_id: str,
    metrics: Mapping[str, Any],
    *,
    step: int = 0,
    timestamp: int | None = None,
) -> LoggedModel:
    return get_default_client().log_logged_model_metrics(
        model_id, metrics, step=step, timestamp=timestamp
    )


def search_logged_models(
    experiment_ids: Sequence[str | int] | None = None,
    *,
    filter_string: str | None = None,
    max_results: int = 100,
    order_by: Sequence[str] | None = None,
    page_token: str | None = None,
) -> list[LoggedModel]:
    models, _ = get_default_client().search_logged_models(
        experiment_ids,
        filter_string=filter_string,
        max_results=max_results,
        order_by=order_by,
        page_token=page_token,
    )
    return models


def delete_logged_model(model_id: str) -> None:
    get_default_client().delete_logged_model(model_id)


def create_experiment(
    name: str,
    artifact_location: str | None = None,
    tags: Mapping[str, Any] | None = None,
):
    return get_default_client().create_experiment(name, artifact_location, tags)


def get_experiment_by_name(name: str):
    return get_default_client().get_experiment_by_name(name)


def set_experiment(experiment_name: str) -> None:
    """Resolve an experiment for the next ``start_run``.

    Real MLflow stores the default experiment per process.  This teaching
    implementation keeps the setting in an environment variable to make
    subprocess behavior explicit and testable.
    """
    experiment = get_default_client().get_experiment_by_name(experiment_name)
    if experiment is None:
        raise ResourceNotFound(f"experiment {experiment_name!r} not found")
    os.environ["MINI_MLFLOW_EXPERIMENT_ID"] = experiment.experiment_id


def get_experiment_id() -> str | None:
    return os.environ.get("MINI_MLFLOW_EXPERIMENT_ID")


# ----------------------------------------------------------------------
# Prompt registry and assessments
# ----------------------------------------------------------------------
def register_prompt(
    name: str,
    template: str,
    *,
    commit_message: str = "",
    tags: Mapping[str, Any] | None = None,
) -> PromptVersion:
    return get_default_client().register_prompt(
        name, template, commit_message=commit_message, tags=tags
    )


def load_prompt(uri: str, *, version: int | str | None = None) -> PromptVersion:
    return get_default_client().load_prompt(uri, version=version)


def set_prompt_alias(name: str, alias: str, version: int) -> None:
    get_default_client().set_prompt_alias(name, alias, version)


def log_feedback(
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
    return get_default_client().log_feedback(
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

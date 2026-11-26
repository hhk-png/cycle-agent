"""A small autologging implementation.

Real MLflow monkey-patches many estimators/frameworks and knows how to map
their parameters and metrics into runs.  MiniMLflow uses an explicit callback
registered by the linear flavor, which demonstrates the same mechanics without
fragile introspection:

* a framework-specific hook observes fit completion;
* it writes params/metrics through the public client;
* it logs the model as a normal artifact.

Autologging must never change training semantics.  Failures while logging are
reported as warnings, not propagated into ``fit``.
"""

from __future__ import annotations

import warnings
from typing import Any

from .errors import MiniMlflowError
from .models.linear import register_fit_callback, unregister_fit_callback

_LOCKED_STATE = {
    "enabled": False,
    "log_models": True,
    "log_input_examples": False,
}


def _callback(model: Any, X: Any, y: Any, training_metrics: dict[str, float]) -> None:
    if not _LOCKED_STATE["enabled"]:
        return
    try:
        from .fluent import get_active_run

        active = get_active_run()
        if active is None:
            return
        client = active.client
        run_id = active.run.info.run_id
        client.log_params(
            run_id,
            {
                "fit_intercept": model.fit_intercept,
                "alpha": model.alpha,
                "n_features": model.n_features_in_,
            },
        )
        client.log_metrics(run_id, training_metrics)
        client.set_tag(run_id, "estimator_name", "mini_mlflow.LinearRegression")
        if _LOCKED_STATE["log_models"]:
            example = X[:10] if isinstance(X, list) else X
            if not _LOCKED_STATE["log_input_examples"]:
                example = None
            client.log_model(
                run_id,
                model,
                artifact_path="model",
                input_example=example,
                metadata={"autolog": True},
            )
    except (MiniMlflowError, ValueError, OSError) as exc:
        warnings.warn(f"MiniMLflow autolog skipped: {exc}", RuntimeWarning, stacklevel=3)


def autolog(
    *,
    log_models: bool = True,
    log_input_examples: bool = False,
    disable: bool = False,
) -> None:
    if disable:
        disable_autolog()
        return
    _LOCKED_STATE["enabled"] = True
    _LOCKED_STATE["log_models"] = bool(log_models)
    _LOCKED_STATE["log_input_examples"] = bool(log_input_examples)
    register_fit_callback(_callback)


def disable_autolog() -> None:
    _LOCKED_STATE["enabled"] = False
    unregister_fit_callback(_callback)


def is_autologging_enabled() -> bool:
    return bool(_LOCKED_STATE["enabled"])

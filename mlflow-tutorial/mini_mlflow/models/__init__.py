"""Model packaging, loading and the ``models:/`` URI resolver."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

from ..errors import InvalidParameter, ModelLoadError
from .data import normalize_rows, normalize_targets
from .linear import LinearRegression, clear_fit_callbacks, register_fit_callback
from .model import ModelInfo, get_model_info, load_model as load_model_from_path, save_model
from .pyfunc import PyFuncModel
from .signature import ColumnSpec, ModelSignature, infer_signature, validate_input

__all__ = [
    "ColumnSpec",
    "LinearRegression",
    "ModelInfo",
    "ModelSignature",
    "PyFuncModel",
    "clear_fit_callbacks",
    "get_model_info",
    "infer_signature",
    "load_model",
    "log_model",
    "normalize_rows",
    "normalize_targets",
    "register_fit_callback",
    "save_model",
    "validate_input",
]

_REGISTRY_STAGES = {"staging", "production", "archived", "none"}
_LOGGED_MODEL_ID = re.compile(r"^m-[0-9a-fA-F]{32}$")


def _load_logged_model(model_id: str, client: Any, dst_path: str | Path | None) -> PyFuncModel:
    if getattr(client, "is_remote", False):
        raise ModelLoadError(
            "remote models:/<model_id> loading needs a logged-model artifact download "
            "endpoint; download the artifact directory first or use a local backend"
        )
    model = client.get_logged_model(model_id)
    if model.status != "LOGGED_MODEL_READY":
        raise ModelLoadError(
            f"logged model {model_id} is {model.status}, not LOGGED_MODEL_READY; "
            "finalize it successfully before loading"
        )
    if not model.artifact_location.startswith("file:"):
        raise ModelLoadError(
            f"logged model {model_id} has a non-local artifact location "
            f"({model.artifact_location}); MiniMLflow only loads file:// artifacts"
        )
    from ..utils import file_uri_to_path

    source = file_uri_to_path(model.artifact_location)
    if not source.is_dir():
        raise ModelLoadError(
            f"logged model {model_id} has no artifact directory at {source}; "
            "it may still be PENDING or the upload may have failed"
        )
    if dst_path is not None:
        destination = Path(dst_path).resolve()
        if destination == source.resolve():
            return load_model_from_path(source)
        destination.mkdir(parents=True, exist_ok=True)
        import shutil

        for item in source.iterdir():
            target = destination / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
        return load_model_from_path(destination)
    return load_model_from_path(source)


def _resolve_registry_uri(model_uri: str, client: Any) -> str:
    if not model_uri.startswith("models:/"):
        return model_uri
    rest = model_uri[len("models:/") :].strip("/")
    if not rest:
        raise InvalidParameter("models:/ URI must include a registered model name")
    if "@" in rest:
        name, alias = rest.rsplit("@", 1)
        if not name or not alias:
            raise InvalidParameter("models:/name@alias requires both name and alias")
        version = client.get_model_version_by_alias(name, alias)
        return version.source
    # Version and stage are the last path component; model names may contain '/'.
    parts = rest.rsplit("/", 1)
    if len(parts) == 2 and (parts[1].isdigit() or parts[1].lower() in _REGISTRY_STAGES or parts[1].lower() == "latest"):
        name, selector = parts
        if selector.isdigit():
            version = client.get_model_version(name, int(selector))
            return version.source
        if selector.lower() == "latest":
            versions = client.search_model_versions(name=name, max_results=1)
            if not versions:
                raise ModelLoadError(f"registered model {name!r} has no versions")
            return versions[0].source
        versions = client.search_model_versions(name=name, max_results=1000)
        wanted = selector.capitalize()
        matching = [item for item in versions if item.stage == wanted]
        if not matching:
            raise ModelLoadError(f"registered model {name!r} has no version in stage {wanted}")
        return max(matching, key=lambda item: item.version).source
    # No selector means latest version.
    versions = client.search_model_versions(name=rest, max_results=1)
    if not versions:
        raise ModelLoadError(f"registered model {rest!r} has no versions")
    return versions[0].source


def load_model(
    model_uri: str | Path,
    *,
    client: Any = None,
    dst_path: str | Path | None = None,
) -> PyFuncModel:
    """Load a local path, ``runs:/`` URI, or ``models:/`` URI.

    Remote artifacts are downloaded into a temporary directory.  The loaded
    object already owns its model state, so the directory can be removed after
    deserialization; this is why the implementation does not return a lazy
    handle to the temp path.
    """
    text = str(model_uri)
    if text.startswith("file://"):
        from ..utils import file_uri_to_path

        return load_model_from_path(file_uri_to_path(text))
    if not text.startswith(("runs:/", "models:/")):
        return load_model_from_path(text)

    if client is None:
        from ..client import MiniClient

        client = MiniClient()

    if text.startswith("models:/"):
        identifier = text[len("models:/") :].strip("/")
        if _LOGGED_MODEL_ID.fullmatch(identifier):
            return _load_logged_model(identifier, client, dst_path)
        text = _resolve_registry_uri(text, client)
        if text.startswith("models:/"):
            identifier = text[len("models:/") :].strip("/")
            if _LOGGED_MODEL_ID.fullmatch(identifier):
                return _load_logged_model(identifier, client, dst_path)
        if not text.startswith("runs:/") and not text.startswith("file://"):
            return load_model_from_path(text)
        if text.startswith("file://"):
            from ..utils import file_uri_to_path

            return load_model_from_path(file_uri_to_path(text))

    # runs:/<run_id>/<artifact path>
    remainder = text[len("runs:/") :]
    if "/" in remainder:
        run_id, artifact_path = remainder.split("/", 1)
    else:
        run_id, artifact_path = remainder, ""
    if not run_id:
        raise InvalidParameter("runs:/ URI must contain a run_id")
    if dst_path is not None:
        downloaded = client.download_artifacts(run_id, artifact_path, dst_path)
        return load_model_from_path(downloaded)
    with tempfile.TemporaryDirectory(prefix="mini-mlflow-model-") as tmp:
        downloaded = client.download_artifacts(run_id, artifact_path, tmp)
        return load_model_from_path(downloaded)


def log_model(
    model: Any,
    artifact_path: str = "model",
    *,
    registered_model_name: str | None = None,
    signature: ModelSignature | dict[str, Any] | None = None,
    input_example: Any = None,
    metadata: dict[str, Any] | None = None,
    run_id: str | None = None,
    client: Any = None,
) -> ModelInfo:
    """Log a model using the active run (or an explicit ``run_id``)."""
    if client is None:
        from ..client import MiniClient

        client = MiniClient()
    if run_id is None:
        from ..fluent import get_active_run

        active = get_active_run()
        if active is None:
            raise InvalidParameter("log_model() requires an active run or an explicit run_id")
        run_id = active.info.run_id
        client = active.client
    return client.log_model(
        run_id,
        model,
        artifact_path=artifact_path,
        registered_model_name=registered_model_name,
        signature=signature,
        input_example=input_example,
        metadata=metadata,
    )

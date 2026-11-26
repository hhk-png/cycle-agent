"""Save/load the MLmodel package format.

A model directory looks like::

    model/
      MLmodel                 # YAML metadata and flavor declarations
      model.json              # linear-flavor parameters (no pickle)
      signature.json          # optional schema (also mirrored in MLmodel)
      input_example.json      # optional example request
      requirements.txt

The important design choice is the *flavor contract*: the generic
``python_function`` flavor says how to load a model, while a named flavor
(``linear``) records the native representation.  Real MLflow supports many
flavors; adding one here means adding a loader and a serializer, not changing
the tracking or registry layers.
"""

from __future__ import annotations

import datetime as _dt
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import yaml

from ..errors import InvalidParameter, ModelLoadError, UnsupportedFlavor
from ..utils import atomic_write_text, pretty_json, safe_join
from ..version import __version__
from .linear import LinearRegression
from .pyfunc import PyFuncModel
from .signature import ModelSignature, infer_signature

_LOADERS = {
    "mini_mlflow.models.linear": ("linear", LinearRegression),
}
_FORMAT_VERSION = "1.0"


@dataclass
class ModelInfo:
    model_uri: str
    artifact_path: str = "model"
    run_id: str = ""
    signature: ModelSignature | None = None
    flavors: tuple[str, ...] = ()
    registered_model_name: str | None = None
    version: int | None = None
    model_id: str | None = None
    model_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_uri": self.model_uri,
            "artifact_path": self.artifact_path,
            "run_id": self.run_id,
            "signature": self.signature.to_dict() if self.signature else None,
            "flavors": list(self.flavors),
            "registered_model_name": self.registered_model_name,
            "version": self.version,
            "model_id": self.model_id,
            "model_type": self.model_type,
        }


def _normalize_signature(signature: ModelSignature | dict[str, Any] | None) -> ModelSignature | None:
    if signature is None:
        return None
    if isinstance(signature, ModelSignature):
        return signature
    if isinstance(signature, dict):
        return ModelSignature.from_dict(signature)
    raise InvalidParameter("signature must be a ModelSignature, a dict, or None")


def save_model(
    path: str | Path,
    model: Any,
    *,
    flavor: str = "linear",
    signature: ModelSignature | dict[str, Any] | None = None,
    input_example: Any = None,
    metadata: dict[str, Any] | None = None,
    run_id: str = "",
    artifact_path: str = "model",
    overwrite: bool = False,
    pip_requirements: Sequence[str] | None = None,
) -> ModelInfo:
    if flavor != "linear":
        raise UnsupportedFlavor(
            f"flavor {flavor!r} is not built in; supported flavors: linear. "
            "The tutorial explains how a production implementation adds sklearn/transformers flavors."
        )
    if not isinstance(model, LinearRegression):
        raise InvalidParameter("linear flavor expects a mini_mlflow.models.linear.LinearRegression")
    if not getattr(model, "feature_names_", None):
        raise InvalidParameter("model must be fitted before save_model()")

    destination = Path(path)
    if destination.exists() and not destination.is_dir():
        raise InvalidParameter(f"model destination must be a directory: {destination}")
    if destination.exists() and any(destination.iterdir()) and not overwrite:
        raise InvalidParameter(f"destination {destination} is not empty; pass overwrite=True explicitly")
    destination.mkdir(parents=True, exist_ok=True)

    normalized_signature = _normalize_signature(signature)
    if normalized_signature is None and input_example is not None:
        normalized_signature = infer_signature(input_example)

    model_record = model.to_dict()
    model_file = destination / "model.json"
    atomic_write_text(model_file, pretty_json(model_record))

    saved_inputs: str | None = None
    if input_example is not None:
        saved_inputs = "input_example.json"
        from ..utils import json_safe

        atomic_write_text(destination / saved_inputs, pretty_json(json_safe(input_example)))

    signature_relative: str | None = None
    if normalized_signature is not None:
        signature_relative = "signature.json"
        atomic_write_text(destination / signature_relative, pretty_json(normalized_signature.to_dict()))

    requirements = list(pip_requirements) if pip_requirements is not None else ["PyYAML>=6.0"]
    atomic_write_text(destination / "requirements.txt", "\n".join(requirements) + "\n")

    created = _dt.datetime.now(tz=_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    mlmodel: dict[str, Any] = {
        "artifact_path": artifact_path,
        "flavors": {
            "python_function": {
                "loader_module": "mini_mlflow.models.linear",
                "data": "model.json",
                "env": "requirements.txt",
                "python_version": sys.version.split()[0],
            },
            "linear": {
                "model_path": "model.json",
                "n_features": model.n_features_in_,
                "coefficients": len(model.coef_),
            },
        },
        "mini_mlflow_version": __version__,
        "model_format_version": _FORMAT_VERSION,
        "model_uuid": _stable_model_uuid(model_record, created),
        "utc_time_created": created,
        "run_id": run_id,
    }
    if normalized_signature is not None:
        mlmodel["signature"] = normalized_signature.to_dict()
    if saved_inputs:
        mlmodel["saved_input_example"] = saved_inputs
    if metadata:
        mlmodel["metadata"] = metadata
    atomic_write_text(destination / "MLmodel", yaml.safe_dump(mlmodel, sort_keys=False, allow_unicode=True))
    return ModelInfo(
        model_uri=destination.resolve().as_uri(),
        artifact_path=artifact_path,
        run_id=run_id,
        signature=normalized_signature,
        flavors=("python_function", "linear"),
    )


def _stable_model_uuid(model_record: dict[str, Any], created: str) -> str:
    import hashlib

    digest = hashlib.sha256(
        (json.dumps(model_record, sort_keys=True) + created).encode("utf-8")
    ).hexdigest()
    return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"


def get_model_info(path: str | Path) -> dict[str, Any]:
    root = Path(path)
    mlmodel_path = root / "MLmodel"
    if not mlmodel_path.is_file():
        raise ModelLoadError(f"MLmodel not found under {root}")
    try:
        raw = yaml.safe_load(mlmodel_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ModelLoadError(f"invalid MLmodel YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ModelLoadError("MLmodel must contain a YAML mapping")
    if "flavors" not in raw or not isinstance(raw["flavors"], dict):
        raise ModelLoadError("MLmodel must declare flavors")
    raw["_path"] = str(root.resolve())
    return raw


def load_model(path: str | Path) -> PyFuncModel:
    root = Path(path)
    info = get_model_info(root)
    pyfunc = info["flavors"].get("python_function")
    if not isinstance(pyfunc, dict):
        raise UnsupportedFlavor("model has no python_function flavor")
    loader_module = str(pyfunc.get("loader_module", ""))
    if loader_module not in _LOADERS:
        raise UnsupportedFlavor(
            f"loader module {loader_module!r} is not available in MiniMLflow. "
            "Only the linear flavor is built in; arbitrary pickle-based loading would be unsafe."
        )
    flavor, model_class = _LOADERS[loader_module]
    data_path = pyfunc.get("data") or info["flavors"].get(flavor, {}).get("model_path")
    if not data_path:
        raise ModelLoadError("MLmodel does not specify a model data file")
    target = safe_join(root, str(data_path))
    if not target.is_file():
        raise ModelLoadError(f"model data file not found: {target}")
    try:
        model_record = json.loads(target.read_text(encoding="utf-8"))
        model = model_class.from_dict(model_record)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ModelLoadError(f"failed to deserialize {flavor} model: {exc}") from exc
    signature = ModelSignature.from_dict(info.get("signature"))
    return PyFuncModel(
        model=model,
        flavor=flavor,
        signature=signature,
        model_uri=str(root.resolve()),
        metadata={
            "run_id": info.get("run_id", ""),
            "utc_time_created": info.get("utc_time_created", ""),
            "mini_mlflow_version": info.get("mini_mlflow_version", ""),
            "model_uuid": info.get("model_uuid", ""),
        },
        flavor_config=info["flavors"].get(flavor, {}),
    )


def copy_model(source: str | Path, destination: str | Path) -> Path:
    src = Path(source)
    if not (src / "MLmodel").is_file():
        raise ModelLoadError(f"{src} is not a model directory")
    dst = Path(destination)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return dst

"""The generic prediction interface exposed by every saved model."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .signature import ModelSignature, validate_input


@dataclass
class PyFuncModel:
    """A loaded model plus its packaging metadata.

    ``predict`` is the stable interface deployment systems use.  Flavor-specific
    objects (here, a linear regressor) are implementation details behind it.
    """

    model: Any
    flavor: str = "linear"
    signature: ModelSignature | None = None
    model_uri: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    flavor_config: dict[str, Any] = field(default_factory=dict)

    def predict(self, data: Any) -> list[Any]:
        validate_input(self.signature, data)
        if not hasattr(self.model, "predict"):
            raise TypeError("loaded flavor does not implement predict")
        predictions = self.model.predict(data)
        if hasattr(predictions, "tolist"):
            predictions = predictions.tolist()
        if isinstance(predictions, list):
            return predictions
        return [predictions]

    @property
    def unwrap_python_model(self) -> Any:
        return self.model

    def __repr__(self) -> str:
        return f"PyFuncModel(flavor={self.flavor!r}, model_uri={self.model_uri!r})"

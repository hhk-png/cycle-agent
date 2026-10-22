"""Model manifests: bind weights, tokenizer, template and engine versions.

A serving incident is often not an engine bug but a version mismatch: new
weights with an old tokenizer, a chat template that emits different control
tokens, a grammar compiled for another vocabulary, or a checkpoint whose
maximum length exceeds the configured KV capacity.  A manifest makes those
assumptions explicit and lets the process fail at startup instead of producing
subtly wrong output under traffic.

The format is deliberately small and JSON-serialisable so that it can be
embedded in a container label, a model repository, or a deployment record.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

MANIFEST_SCHEMA_VERSION = 1
_CHUNK = 1 << 20


def sha256_file(path: str | Path) -> str:
    """Return the SHA-256 of a file without reading it all into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class ModelManifest:
    """Everything that must agree for a checkpoint to be served safely."""

    model_id: str
    model_revision: str
    weight_sha256: str
    tokenizer_sha256: str
    chat_template_sha256: str
    grammar_version: str
    engine_version: str
    dtype: str
    quantization: str | None
    max_model_len: int
    vocab_size: int
    schema_version: int = MANIFEST_SCHEMA_VERSION
    extra: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def from_files(
        cls,
        model_path: str | Path,
        tokenizer_path: str | Path,
        *,
        model_id: str,
        model_revision: str = "local",
        engine_version: str,
        dtype: str = "float32",
        quantization: str | None = None,
        max_model_len: int,
        vocab_size: int,
        chat_template: str = "",
        grammar_version: str = "none",
        extra: dict[str, str] | None = None,
    ) -> "ModelManifest":
        return cls(
            model_id=model_id,
            model_revision=model_revision,
            weight_sha256=sha256_file(model_path),
            tokenizer_sha256=sha256_file(tokenizer_path),
            chat_template_sha256=sha256_text(chat_template),
            grammar_version=grammar_version,
            engine_version=engine_version,
            dtype=dtype,
            quantization=quantization,
            max_model_len=int(max_model_len),
            vocab_size=int(vocab_size),
            extra=tuple(sorted((extra or {}).items())),
        )

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "weight_sha256": self.weight_sha256,
            "tokenizer_sha256": self.tokenizer_sha256,
            "chat_template_sha256": self.chat_template_sha256,
            "grammar_version": self.grammar_version,
            "engine_version": self.engine_version,
            "dtype": self.dtype,
            "quantization": self.quantization,
            "max_model_len": self.max_model_len,
            "vocab_size": self.vocab_size,
            "extra": dict(self.extra),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_dict(cls, payload: dict) -> "ModelManifest":
        if "schema_version" not in payload:
            # Guessing a version turns a forward/backward-incompatible change
            # into silent acceptance.  Require the field and let a migration
            # step add it explicitly.
            raise ConfigError("manifest is missing schema_version; refusing to guess")
        try:
            return cls(
                schema_version=int(payload["schema_version"]),
                model_id=str(payload["model_id"]),
                model_revision=str(payload["model_revision"]),
                weight_sha256=str(payload["weight_sha256"]),
                tokenizer_sha256=str(payload["tokenizer_sha256"]),
                chat_template_sha256=str(payload["chat_template_sha256"]),
                grammar_version=str(payload["grammar_version"]),
                engine_version=str(payload["engine_version"]),
                dtype=str(payload["dtype"]),
                quantization=payload.get("quantization"),
                max_model_len=int(payload["max_model_len"]),
                vocab_size=int(payload["vocab_size"]),
                extra=tuple(sorted((payload.get("extra") or {}).items())),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"invalid model manifest: {exc}") from exc

    @classmethod
    def from_json(cls, text: str) -> "ModelManifest":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"manifest is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ConfigError("manifest must be a JSON object")
        return cls.from_dict(payload)

    @classmethod
    def read(cls, path: str | Path) -> "ModelManifest":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def write(self, path: str | Path) -> None:
        Path(path).write_text(self.to_json() + "\n", encoding="utf-8")

    def fingerprint(self) -> str:
        """Stable identity of the manifest contents, independent of formatting."""
        return hashlib.sha256(_canonical(self.to_dict()).encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def validate_runtime(
        self,
        *,
        engine_version: str,
        vocab_size: int,
        max_model_len: int,
        dtype: str,
        quantization: str | None = None,
    ) -> tuple[str, ...]:
        """Return compatibility problems instead of silently serving a mismatch.

        An empty tuple means the runtime agrees with the manifest.  Major
        engine-version changes are treated as incompatible because scheduler
        and KV layout assumptions may have changed.
        """
        problems: list[str] = []
        if self.schema_version != MANIFEST_SCHEMA_VERSION:
            problems.append(
                f"manifest schema {self.schema_version} != supported {MANIFEST_SCHEMA_VERSION}"
            )
        if self.vocab_size != int(vocab_size):
            problems.append(f"vocab size {vocab_size} != manifest {self.vocab_size}")
        if int(max_model_len) > self.max_model_len:
            problems.append(
                f"runtime max_model_len {max_model_len} exceeds manifest {self.max_model_len}"
            )
        if self.dtype != dtype:
            problems.append(f"dtype {dtype} != manifest {self.dtype}")
        if (self.quantization or "none") != (quantization or "none"):
            problems.append(
                f"quantization {quantization or 'none'} != manifest {self.quantization or 'none'}"
            )
        manifest_major = self.engine_version.split(".", 1)[0]
        runtime_major = str(engine_version).split(".", 1)[0]
        if manifest_major != runtime_major:
            problems.append(
                f"engine major version {runtime_major} != manifest {manifest_major}"
            )
        for name, value in (
            ("weight_sha256", self.weight_sha256),
            ("tokenizer_sha256", self.tokenizer_sha256),
            ("chat_template_sha256", self.chat_template_sha256),
        ):
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value.lower()):
                problems.append(f"{name} is not a SHA-256 hex digest")
        return tuple(problems)

    def assert_compatible(self, **runtime) -> None:
        problems = self.validate_runtime(**runtime)
        if problems:
            raise ConfigError("model manifest is incompatible: " + "; ".join(problems))

    def verify_files(
        self,
        *,
        model_path: str | Path | None = None,
        tokenizer_path: str | Path | None = None,
        chat_template: str | None = None,
    ) -> tuple[str, ...]:
        """Recompute hashes of the files that will actually be served.

        ``validate_runtime`` only checks metadata; without this method a
        swapped checkpoint with the same vocabulary and dtype would pass.
        Paths that are not supplied are skipped, so callers can verify only
        the artifacts they load.
        """
        problems: list[str] = []
        if model_path is not None:
            actual = sha256_file(model_path)
            if actual != self.weight_sha256:
                problems.append(
                    f"weight hash {actual} != manifest {self.weight_sha256}"
                )
        if tokenizer_path is not None and Path(tokenizer_path).exists():
            actual = sha256_file(tokenizer_path)
            if actual != self.tokenizer_sha256:
                problems.append(
                    f"tokenizer hash {actual} != manifest {self.tokenizer_sha256}"
                )
        if chat_template is not None:
            actual = sha256_text(chat_template)
            if actual != self.chat_template_sha256:
                problems.append(
                    f"chat template hash {actual} != manifest {self.chat_template_sha256}"
                )
        return tuple(problems)

    def assert_files(self, **paths) -> None:
        problems = self.verify_files(**paths)
        if problems:
            raise ConfigError("model artifacts do not match the manifest: " + "; ".join(problems))


def load_manifest(path: str | Path) -> ModelManifest:
    return ModelManifest.read(path)

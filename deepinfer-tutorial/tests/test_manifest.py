from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from mini_deepinfer.config import EngineConfig
from mini_deepinfer.errors import ConfigError
from mini_deepinfer.manifest import ModelManifest, sha256_file, sha256_text


ASSETS = Path(__file__).resolve().parents[1] / "mini_deepinfer" / "assets"


@pytest.fixture()
def manifest() -> ModelManifest:
    return ModelManifest.from_files(
        ASSETS / "tiny_lm.npz",
        ASSETS / "vocab.json",
        model_id="mini/tiny-lm",
        model_revision="test",
        engine_version="0.1.0",
        dtype="float32",
        max_model_len=512,
        vocab_size=264,
        chat_template="<|bos|><|user|>{content}<|end|><|assistant|>",
        grammar_version="mini-1",
    )


def test_verify_files_detects_swapped_weights(manifest, tmp_path) -> None:
    copied = tmp_path / "tiny_lm.npz"
    copied.write_bytes((ASSETS / "tiny_lm.npz").read_bytes())
    assert manifest.verify_files(model_path=copied) == ()

    copied.write_bytes(copied.read_bytes() + b"tampered")
    problems = manifest.verify_files(model_path=copied)
    assert len(problems) == 1
    assert "weight hash" in problems[0]
    with pytest.raises(ConfigError):
        manifest.assert_files(model_path=copied)


def test_manifest_without_schema_version_is_rejected(manifest) -> None:
    payload = manifest.to_dict()
    payload.pop("schema_version")
    with pytest.raises(ConfigError):
        ModelManifest.from_dict(payload)


def test_file_and_text_hashes_are_stable() -> None:
    path = ASSETS / "vocab.json"
    assert sha256_file(path) == sha256_file(path)
    assert len(sha256_file(path)) == 64
    assert sha256_text("abc") == sha256_text("abc")
    assert sha256_text("abc") != sha256_text("abd")


def test_manifest_roundtrip_and_fingerprint(manifest: ModelManifest) -> None:
    restored = ModelManifest.from_json(manifest.to_json())
    assert restored == manifest
    assert restored.fingerprint() == manifest.fingerprint()
    # Formatting changes must not change identity.
    pretty = json.dumps(json.loads(manifest.to_json()), indent=4)
    assert ModelManifest.from_json(pretty).fingerprint() == manifest.fingerprint()


def test_manifest_file_roundtrip(tmp_path, manifest: ModelManifest) -> None:
    path = tmp_path / "manifest.json"
    manifest.write(path)
    assert ModelManifest.read(path) == manifest


def test_compatible_runtime_has_no_problems(manifest: ModelManifest) -> None:
    assert (
        manifest.validate_runtime(
            engine_version="0.1.0",
            vocab_size=264,
            max_model_len=512,
            dtype="float32",
            quantization=None,
        )
        == ()
    )


def test_incompatible_runtime_is_reported_not_ignored(manifest: ModelManifest) -> None:
    problems = manifest.validate_runtime(
        engine_version="1.0.0",
        vocab_size=320,
        max_model_len=1024,
        dtype="bfloat16",
        quantization="fp8",
    )
    text = " ".join(problems)
    assert "vocab" in text
    assert "max_model_len" in text
    assert "dtype" in text
    assert "quantization" in text
    assert "major version" in text
    with pytest.raises(ConfigError):
        manifest.assert_compatible(
            engine_version="1.0.0",
            vocab_size=320,
            max_model_len=1024,
            dtype="bfloat16",
        )


def test_invalid_json_and_missing_fields_raise_config_error() -> None:
    with pytest.raises(ConfigError):
        ModelManifest.from_json("{not json")
    with pytest.raises(ConfigError):
        ModelManifest.from_json('{"model_id": "x"}')
    with pytest.raises(ConfigError):
        ModelManifest.from_json("[]")


def test_llm_validates_manifest_at_startup(manifest: ModelManifest) -> None:
    from mini_deepinfer import LLM

    llm = LLM(config=EngineConfig(max_model_len=512), manifest=manifest)
    llm.close()

    incompatible = replace(manifest, max_model_len=128)
    with pytest.raises(ConfigError):
        LLM(config=EngineConfig(max_model_len=256), manifest=incompatible)


def test_tampered_manifest_is_detected(manifest: ModelManifest) -> None:
    payload = json.loads(manifest.to_json())
    payload["weight_sha256"] = "not-a-hash"
    tampered = ModelManifest.from_dict(payload)
    problems = tampered.validate_runtime(
        engine_version="0.1.0",
        vocab_size=264,
        max_model_len=512,
        dtype="float32",
    )
    assert any("weight_sha256" in problem for problem in problems)

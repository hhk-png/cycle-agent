"""Build and validate a model manifest for the shipped checkpoint."""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mini_deepinfer.manifest import ModelManifest  # noqa: E402

ASSETS = ROOT / "mini_deepinfer" / "assets"


def main() -> None:
    manifest = ModelManifest.from_files(
        ASSETS / "tiny_lm.npz",
        ASSETS / "vocab.json",
        model_id="mini/tiny-lm",
        model_revision="local-2026-10",
        engine_version="0.1.0",
        dtype="float32",
        max_model_len=512,
        vocab_size=264,
        chat_template="<|bos|><|user|>{content}<|end|><|assistant|>",
        grammar_version="mini-1",
        extra={"purpose": "tutorial"},
    )
    print("manifest fingerprint:", manifest.fingerprint())
    print("weight sha256       :", manifest.weight_sha256[:16] + "...")
    print("tokenizer sha256    :", manifest.tokenizer_sha256[:16] + "...")
    print("compatible          :", manifest.validate_runtime(
        engine_version="0.1.0",
        vocab_size=264,
        max_model_len=512,
        dtype="float32",
    ) == ())

    tampered_payload = json.loads(manifest.to_json())
    tampered_payload["max_model_len"] = 128
    tampered_payload["vocab_size"] = 300
    tampered = ModelManifest.from_dict(tampered_payload)
    problems = tampered.validate_runtime(
        engine_version="0.1.0",
        vocab_size=264,
        max_model_len=512,
        dtype="float32",
    )
    print("tampered problems   :")
    for problem in problems:
        print("  -", problem)


if __name__ == "__main__":
    main()

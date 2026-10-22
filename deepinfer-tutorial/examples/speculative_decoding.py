"""Target-only decoding vs DSpark-style speculative decoding."""

from __future__ import annotations

import time
from pathlib import Path

from _common import add_project_root

add_project_root()

from mini_deepinfer import EngineConfig, LLM, SamplingParams


ASSETS = Path(__file__).resolve().parents[1] / "mini_deepinfer" / "assets"


def build(speculative: bool) -> LLM:
    return LLM(
        draft_model=ASSETS / "draft_lm.npz" if speculative else None,
        config=EngineConfig(
            num_blocks=128,
            max_num_batched_tokens=128,
            max_num_seqs=4,
            max_model_len=256,
            enable_speculative=speculative,
            speculative_tokens=4,
            confidence_threshold=0.55,
        ),
    )


def main() -> None:
    prompt = "DeepInfer is"
    params = SamplingParams(temperature=0.0, max_tokens=32)

    target = build(False)
    started = time.perf_counter()
    target_text = target.generate(prompt, params)[0]
    target_time = time.perf_counter() - started

    spec = build(True)
    started = time.perf_counter()
    spec_text = spec.generate(prompt, params)[0]
    spec_time = time.perf_counter() - started

    print("target:", target_text)
    print("spec  :", spec_text)
    print("identical:", target_text == spec_text)
    print("target wall time:", f"{target_time * 1000:.2f} ms")
    print("spec wall time  :", f"{spec_time * 1000:.2f} ms")
    print("spec stats:", spec.engine.spec_stats.as_dict())
    print()
    print(
        "On a CPU the draft model is not free, so wall-clock speedup is not "
        "guaranteed.  Acceptance rate and verified-token reduction are the "
        "algorithmic signals; the tutorial derives the GPU speedup model."
    )


if __name__ == "__main__":
    main()

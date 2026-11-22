#!/usr/bin/env python
"""Run the complete verification matrix.

This is intentionally a subprocess driver: each distributed experiment gets a
fresh process tree and a fresh portable communication backend.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
ENV = os.environ.copy()
ENV["PYTHONPATH"] = str(ROOT)
ENV["OMP_NUM_THREADS"] = "1"
ENV["MKL_NUM_THREADS"] = "1"


def run(label: str, argv, timeout: int = 600) -> str:
    started = time.time()
    print(f"\n[{label}] {' '.join(str(x) for x in argv)}", flush=True)
    result = subprocess.run(
        [str(x) for x in argv],
        cwd=ROOT,
        env=ENV,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    elapsed = time.time() - started
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        raise SystemExit(f"FAILED: {label} ({elapsed:.1f}s)")
    tail = "\n".join(result.stdout.strip().splitlines()[-3:])
    print(f"OK ({elapsed:.1f}s) {tail}", flush=True)
    return result.stdout


def main() -> int:
    start = time.time()
    run("compile", [PY, "-m", "compileall", "-q", "mini_megatron", "tests", "scripts"])
    run("unit+equivalence", [PY, "-m", "pytest", "-q"], timeout=900)

    # Single-process: train, checkpoint, resume, generate.
    ckpt = ROOT / ".verify_tmp" / "tiny"
    shutil.rmtree(ROOT / ".verify_tmp", ignore_errors=True)
    run(
        "single-train",
        [PY, "-m", "mini_megatron.cli", "train", "--config", "configs/tiny.json",
         "--steps", "4", "--checkpoint-dir", ckpt],
    )
    run(
        "single-resume",
        [PY, "-m", "mini_megatron.cli", "train", "--config", "configs/tiny.json",
         "--steps", "6", "--checkpoint-dir", ckpt, "--auto-resume"],
    )
    gen = run(
        "generate",
        [PY, "-m", "mini_megatron.cli", "generate", "--config", "configs/tiny.json",
         "--checkpoint", ckpt, "--prompt", "1,2,3,4", "--max-new-tokens", "8",
         "--temperature", "0"],
    )
    assert '"tokens"' in gen

    # Direct VPP equivalence (also covered by pytest, kept as a visible case).
    run(
        "vpp-equivalence",
        [PY, "tests/parallel_vpp.py", "--world-size", "2",
         "--out", ROOT / ".verify_tmp" / "vpp.json"],
        timeout=300,
    )

    # Multi-process matrix.
    cases = [
        (2, "tiny_tp2.json"),
        (2, "tiny_pp2.json"),
        (2, "tiny_pp2_gpipe.json"),
        (2, "tiny_cp2.json"),
        (2, "tiny_dp2.json"),
        (2, "tiny_dp2_flat.json"),
        (2, "tiny_moe_tp2.json"),
        (2, "tiny_ep2.json"),
        (4, "tiny_tp2_pp2.json"),
        (4, "tiny_tp2_dp2.json"),
        (4, "tiny_pp2_dp2.json"),
        (4, "tiny_cp2_pp2.json"),
        (4, "tiny_tp2_cp2.json"),
        (4, "tiny_tp2_ep2.json"),
    ]
    for world, config in cases:
        run(
            f"dist-{world}-{config}",
            [PY, "scripts/run_distributed.py", "--world-size", world,
             "--config", f"configs/{config}", "--steps", "2",
             "--checkpoint-dir", ""],
            timeout=600,
        )

    # Checkpoint round-trip under a replicated-dimensional layout.
    run(
        "dist-checkpoint",
        [PY, "scripts/run_distributed.py", "--world-size", "2",
         "--config", "configs/tiny_pp2.json", "--steps", "2",
         "--checkpoint-dir", ROOT / ".verify_tmp" / "pp2"],
        timeout=600,
    )
    run(
        "dist-resume",
        [PY, "scripts/run_distributed.py", "--world-size", "2",
         "--config", "configs/tiny_pp2.json", "--steps", "4",
         "--checkpoint-dir", ROOT / ".verify_tmp" / "pp2", "--auto-resume"],
        timeout=600,
    )

    # Expert-parallel checkpoint: each rank saves its own expert shard.
    ep_ckpt = ROOT / ".verify_tmp" / "ep2"
    run(
        "dist-ep-checkpoint",
        [PY, "scripts/run_distributed.py", "--world-size", "2",
         "--config", "configs/tiny_ep2.json", "--steps", "2",
         "--checkpoint-dir", ep_ckpt],
        timeout=600,
    )
    run(
        "dist-ep-resume",
        [PY, "scripts/run_distributed.py", "--world-size", "2",
         "--config", "configs/tiny_ep2.json", "--steps", "4",
         "--checkpoint-dir", ep_ckpt, "--auto-resume"],
        timeout=600,
    )

    # Flat distributed optimizer: save and resume its sharded state.
    flat_ckpt = ROOT / ".verify_tmp" / "dp2_flat"
    run(
        "dist-flat-checkpoint",
        [PY, "scripts/run_distributed.py", "--world-size", "2",
         "--config", "configs/tiny_dp2_flat.json", "--steps", "2",
         "--checkpoint-dir", flat_ckpt],
        timeout=600,
    )
    run(
        "dist-flat-resume",
        [PY, "scripts/run_distributed.py", "--world-size", "2",
         "--config", "configs/tiny_dp2_flat.json", "--steps", "4",
         "--checkpoint-dir", flat_ckpt, "--auto-resume"],
        timeout=600,
    )

    elapsed = time.time() - start
    print(
        json.dumps(
            {"status": "ok", "elapsed_sec": round(elapsed, 1), "cases": len(cases) + 11}
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

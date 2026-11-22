"""End-to-end multi-process checks.

These launch child processes because Windows requires the ``spawn`` start
method and because a fresh process is the only honest way to exercise the
portable communication backend.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def run(argv, timeout=180):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        argv,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        pytest.fail(f"command failed: {argv}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
    return result.stdout


@pytest.mark.parametrize(
    "case,loss_tol,grad_tol",
    [
        ("tp", 1e-5, 1e-5),
        ("tp_bias", 1e-5, 1e-5),
        ("tp_learned", 1e-5, 1e-5),
        ("pp", 1e-5, 1e-5),
        ("cp", 2e-5, 1e-5),
    ],
)
def test_parallel_equivalence(tmp_path, case, loss_tol, grad_tol):
    out = tmp_path / f"{case}.json"
    run(
        [
            sys.executable,
            str(ROOT / "tests" / "parallel_equiv.py"),
            "--case",
            case,
            "--world-size",
            "2",
            "--out",
            str(out),
        ]
    )
    result = json.loads(out.read_text())
    assert result["loss_diff"] <= loss_tol, result
    assert result["grad_diff"] <= grad_tol, result
    if "gradnorm_diff" in result:
        assert result["gradnorm_diff"] <= 2e-4, result


def test_flat_distributed_optimizer_equivalence(tmp_path):
    out = tmp_path / "flat_optimizer.json"
    run(
        [
            sys.executable,
            str(ROOT / "tests" / "distributed_optimizer_equiv.py"),
            "--out",
            str(out),
        ]
    )
    result = json.loads(out.read_text())
    assert result["param_max_diff"] <= 1e-6, result
    assert result["loss_diff"] <= 1e-6, result
    assert result["grad_norm_diff"] <= 1e-5, result


def test_expert_parallel_equivalence(tmp_path):
    out = tmp_path / "ep.json"
    run(
        [
            sys.executable,
            str(ROOT / "tests" / "parallel_ep.py"),
            "--world-size",
            "2",
            "--out",
            str(out),
        ]
    )
    result = json.loads(out.read_text())
    assert result["loss_diff"] <= 1e-5, result
    assert result["logits_diff"] <= 1e-5, result
    assert result["aux_diff"] <= 1e-6, result
    assert result["grad_diff"] <= 1e-5, result


@pytest.mark.parametrize("world_size", [1, 2])
def test_interleaved_vpp_equivalence(tmp_path, world_size):
    out = tmp_path / f"vpp{world_size}.json"
    run(
        [
            sys.executable,
            str(ROOT / "tests" / "parallel_vpp.py"),
            "--world-size",
            str(world_size),
            "--out",
            str(out),
        ]
    )
    result = json.loads(out.read_text())
    assert result["loss_diff"] <= 1e-5, result
    assert result["grad_diff"] <= 1e-5, result


@pytest.mark.parametrize(
    "config",
    [
        "tiny_tp2.json",
        "tiny_pp2.json",
        "tiny_pp2_gpipe.json",
        "tiny_cp2.json",
        "tiny_dp2.json",
        "tiny_dp2_flat.json",
        "tiny_ep2.json",
    ],
)
def test_distributed_training_smoke(config):
    output = run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_distributed.py"),
            "--world-size",
            "2",
            "--config",
            str(ROOT / "configs" / config),
            "--steps",
            "2",
            "--checkpoint-dir",
            "",
        ]
    )
    finals = [json.loads(line) for line in output.splitlines() if line.startswith('{"event": "final"')]
    assert finals, output
    assert all(isfinite(finals[-1][k]) for k in ("loss", "grad_norm"))


def _distributed_final(config):
    output = run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_distributed.py"),
            "--world-size",
            "2",
            "--config",
            str(ROOT / "configs" / config),
            "--steps",
            "2",
            "--checkpoint-dir",
            "",
        ]
    )
    finals = [json.loads(line) for line in output.splitlines() if line.startswith('{"event": "final"')]
    assert finals, output
    return finals[-1]


def test_flat_optimizer_tp_clip_uses_global_norm(tmp_path):
    """TP=2/DP=2: the flat clip norm must equal the global norm."""
    out = tmp_path / "flat_tp.json"
    run(
        [
            sys.executable,
            str(ROOT / "tests" / "flat_tp_equiv.py"),
            "--out",
            str(out),
        ],
        timeout=300,
    )
    result = json.loads(out.read_text())
    assert {r["clip"] for r in result["records"]} == {0.0, 0.01}
    assert all(r["param_diff"] < 1e-6 for r in result["records"])


def test_replicated_moe_dp_collective_symmetry():
    """Experts with no gradient on one DP rank must not desynchronise collectives."""
    output = run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_distributed.py"),
            "--world-size",
            "2",
            "--config",
            str(ROOT / "configs" / "tiny_moe_dp2.json"),
            "--steps",
            "2",
            "--checkpoint-dir",
            "",
        ],
        timeout=300,
    )
    finals = [
        json.loads(line)
        for line in output.splitlines()
        if line.startswith('{"event": "final"')
    ]
    assert finals, output
    assert all(isfinite(finals[-1][k]) for k in ("loss", "grad_norm"))


def test_tp_plus_ep_smoke():
    output = run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_distributed.py"),
            "--world-size",
            "4",
            "--config",
            str(ROOT / "configs" / "tiny_tp2_ep2.json"),
            "--steps",
            "2",
            "--checkpoint-dir",
            "",
        ],
        timeout=300,
    )
    finals = [
        json.loads(line)
        for line in output.splitlines()
        if line.startswith('{"event": "final"')
    ]
    assert finals, output
    assert all(isfinite(finals[-1][k]) for k in ("loss", "grad_norm"))


def test_gpipe_and_1f1b_match():
    one = _distributed_final("tiny_pp2.json")
    gpipe = _distributed_final("tiny_pp2_gpipe.json")
    assert abs(one["loss"] - gpipe["loss"]) < 1e-6, (one, gpipe)
    assert abs(one["grad_norm"] - gpipe["grad_norm"]) < 1e-6, (one, gpipe)


def test_zero_matches_adamw(tmp_path):
    out = tmp_path / "zero.json"
    run([sys.executable, str(ROOT / "tests" / "zero_equiv.py"), "--out", str(out)])
    result = json.loads(out.read_text())
    assert result["max_diff"] < 1e-6, result
    assert result["loss_diff"] < 1e-6, result


def isfinite(value):
    return value == value and abs(value) != float("inf")

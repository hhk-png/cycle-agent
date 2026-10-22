from __future__ import annotations

import numpy as np
import pytest

from mini_deepinfer.compiler import (
    Graph,
    Op,
    build_mlp_graph,
    constant_fold,
    dead_code_elimination,
    estimate,
    fuse_rmsnorm_matmul,
    fuse_swiglu,
    optimize,
    plan_memory,
)


def test_graph_runs_and_validates() -> None:
    graph, feed = build_mlp_graph(batch=3, dim=8, hidden=12)
    outputs = graph.run(feed)
    assert outputs["probs"].shape == (3, 8)
    assert outputs["probs_t"].shape == (8, 3)
    assert np.allclose(outputs["probs"].sum(axis=-1), 1.0)

    with pytest.raises(ValueError):
        Graph([Op("add", ("missing", "x"), "y")], inputs=("x",), outputs=("y",))
    with pytest.raises(ValueError):
        graph.run({"x": feed["x"]})


def test_optimize_preserves_numerics_and_reduces_ops() -> None:
    graph, feed = build_mlp_graph(batch=2, dim=8, hidden=16)
    optimized = optimize(graph, feed)
    before = graph.run(feed)
    after = optimized.graph.run(feed)
    for name in before:
        assert np.allclose(before[name], after[name], atol=1e-6)
    assert optimized.after.ops < optimized.before.ops
    assert optimized.op_reduction > 0
    assert optimized.after.launches == optimized.after.ops


def test_constant_fold_and_dead_code() -> None:
    ops = [
        Op("const", (), "a", (("value", np.array([1.0, 2.0])),)),
        Op("const", (), "b", (("value", np.array([3.0, 4.0])),)),
        Op("add", ("a", "b"), "sum"),
        Op("mul", ("sum", "a"), "unused"),
        Op("const", (), "dead", (("value", np.array(0.0)),)),
    ]
    graph = Graph(ops, inputs=(), outputs=("sum",))
    folded = constant_fold(graph)
    assert all(op.name == "const" for op in folded.ops)
    assert np.allclose(folded.run({})["sum"], [4.0, 6.0])

    pruned = dead_code_elimination(folded)
    assert [op.output for op in pruned.ops] == ["sum"]


def test_fusion_requires_single_consumer() -> None:
    # rmsnorm feeds one matmul: fuse.
    ops = [
        Op("rmsnorm", ("x", "w"), "h"),
        Op("matmul", ("h", "m"), "y"),
    ]
    graph = Graph(ops, inputs=("x", "w", "m"), outputs=("y",))
    fused = fuse_rmsnorm_matmul(graph)
    assert [op.name for op in fused.ops] == ["fused_rmsnorm_matmul"]

    # rmsnorm feeds two matmuls: the intermediate is shared, so keep it.
    ops = [
        Op("rmsnorm", ("x", "w"), "h"),
        Op("matmul", ("h", "m1"), "y1"),
        Op("matmul", ("h", "m2"), "y2"),
    ]
    graph = Graph(ops, inputs=("x", "w", "m1", "m2"), outputs=("y1", "y2"))
    unfused = fuse_rmsnorm_matmul(graph)
    assert [op.name for op in unfused.ops] == ["rmsnorm", "matmul", "matmul"]

    # A graph output must remain materialized even if it has one consumer.
    ops = [
        Op("rmsnorm", ("x", "w"), "h"),
        Op("matmul", ("h", "m"), "y"),
    ]
    graph = Graph(ops, inputs=("x", "w", "m"), outputs=("h", "y"))
    guarded = fuse_rmsnorm_matmul(graph)
    assert [op.name for op in guarded.ops] == ["rmsnorm", "matmul"]


def test_swiglu_fusion() -> None:
    ops = [
        Op("silu", ("x",), "act"),
        Op("mul", ("act", "gate"), "y"),
    ]
    graph = Graph(ops, inputs=("x", "gate"), outputs=("y",))
    feed = {
        "x": np.array([0.5, -0.5], dtype=np.float32),
        "gate": np.array([2.0, 3.0], dtype=np.float32),
    }
    fused = fuse_swiglu(graph)
    assert [op.name for op in fused.ops] == ["swiglu"]
    assert np.allclose(graph.run(feed)["y"], fused.run(feed)["y"])


def test_memory_plan_reuses_slots_and_respects_liveness() -> None:
    graph, feed = build_mlp_graph(batch=4, dim=8, hidden=16)
    plan = plan_memory(graph, feed)
    assert plan.peak_bytes <= plan.naive_bytes
    assert plan.saving >= 0.0

    # Reconstruct live intervals and check that overlapping tensors never
    # share a slot.
    definition = {name: 0 for name in graph.inputs}
    for index, op in enumerate(graph.ops):
        definition[op.output] = index + 1
    last = dict(definition)
    for index, op in enumerate(graph.ops):
        for name in op.inputs:
            last[name] = max(last[name], index + 1)
    for name in graph.outputs:
        last[name] = len(graph.ops) + 1
    names = list(plan.slots)
    for i, left in enumerate(names):
        for right in names[i + 1 :]:
            overlap = definition[left] <= last[right] and definition[right] <= last[left]
            if overlap:
                assert plan.slots[left] != plan.slots[right]


def test_estimate_reports_positive_work() -> None:
    graph, feed = build_mlp_graph(batch=2, dim=8, hidden=16)
    stats = estimate(graph, feed)
    assert stats.ops == len(graph.ops)
    assert stats.flops > 0
    assert stats.bytes > 0
    assert stats.naive_bytes >= stats.peak_bytes

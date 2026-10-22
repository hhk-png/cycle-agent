"""Graph-level constant folding, fusion and memory planning."""

from __future__ import annotations

import numpy as np

from _common import add_project_root

add_project_root()

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


def main() -> None:
    graph, feed = build_mlp_graph(batch=4, dim=16, hidden=32)
    before = graph.run(feed)
    print("original ops          :", len(graph.ops))
    for op in graph.ops:
        print("  ", op)

    fused_norm = fuse_rmsnorm_matmul(graph)
    fused = fuse_swiglu(fused_norm)
    print("after fusion          :", len(fused.ops), [op.name for op in fused.ops])

    optimized = optimize(graph, feed)
    after = optimized.graph.run(feed)
    same = all(np.allclose(before[name], after[name], atol=1e-6) for name in before)
    print("numerics preserved    :", same)
    print("ops before / after    :", optimized.before.ops, "/", optimized.after.ops)
    print("op reduction          :", round(optimized.op_reduction, 3))
    print("launches before/after :", optimized.before.launches, "/", optimized.after.launches)
    print("flops                 :", optimized.before.flops)
    print("traffic bytes         :", optimized.before.bytes)

    plan = plan_memory(optimized.graph, feed)
    print("peak buffer bytes     :", plan.peak_bytes)
    print("sum of tensor bytes   :", plan.naive_bytes)
    print("memory saving         :", round(plan.saving, 3))
    print("slot assignment       :", dict(sorted(plan.slots.items())))

    const_graph = Graph(
        [
            Op("const", (), "a", (("value", np.array([1.0, 2.0], dtype=np.float32)),)),
            Op("const", (), "b", (("value", np.array([3.0, 4.0], dtype=np.float32)),)),
            Op("add", ("a", "b"), "sum"),
            Op("mul", ("sum", "a"), "unused"),
        ],
        inputs=(),
        outputs=("sum",),
    )
    folded = dead_code_elimination(constant_fold(const_graph))
    print("constant fold result  :", folded.run({})["sum"].tolist())
    print("ops after folding     :", [op.name for op in folded.ops])
    print("estimate after passes :", estimate(optimized.graph, feed).ops, "ops")


if __name__ == "__main__":
    main()

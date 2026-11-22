"""Tensor parallel, pipeline schedule, MoE routing and Ulysses all-to-all."""

import numpy as np

from _common import header

from mini_verl.parallel import (
    ColumnParallelLinear,
    PipelineSchedule,
    RowParallelLinear,
    moe_route,
    pipeline_forward,
    sequence_parallel_mlp,
    ulysses_merge,
    ulysses_split,
)


def main() -> None:
    header("Four parallelism patterns, each checked against a dense reference")
    rng = np.random.default_rng(0)
    x = rng.normal(size=(4, 8)).astype(np.float32)
    w1 = rng.normal(size=(8, 16)).astype(np.float32)
    b1 = np.zeros(16, dtype=np.float32)
    w2 = rng.normal(size=(16, 8)).astype(np.float32)
    b2 = np.zeros(8, dtype=np.float32)

    col = ColumnParallelLinear(w1, b1, tp_size=4)
    row = RowParallelLinear(w2, b2, tp_size=4)
    dense = np.maximum(x @ w1 + b1, 0) @ w2 + b2
    split = row.forward(np.maximum(col.forward(x), 0))
    print("TP MLP max error:", float(np.abs(split - dense).max()))

    stages = [lambda value: value + 1, lambda value: value * 2]
    micro = [np.asarray([1.0]), np.asarray([2.0]), np.asarray([3.0])]
    print("pipeline outputs:", [float(value[0]) for value in pipeline_forward(stages, micro)])
    print("1F1B bubble:", round(PipelineSchedule(12, 4).bubble_ratio(), 3))

    router = rng.normal(size=(10, 4)).astype(np.float32)
    result = moe_route(router, top_k=2, capacity_factor=1.25)
    print("expert counts:", result["expert_counts"].tolist(), "dropped:", int(result["dropped_tokens"][0]))

    sequence = rng.normal(size=(2, 12, 8)).astype(np.float32)
    chunks = ulysses_split(sequence, 3)
    print("Ulysses split:", [chunk.shape[1] for chunk in chunks], "roundtrip:", np.allclose(ulysses_merge(chunks, 3), sequence))

    sp_out = sequence_parallel_mlp(sequence, w1, b1, w2, b2, 3)
    sp_ref = np.maximum(sequence @ w1 + b1, 0) @ w2 + b2
    print("sequence-parallel MLP max error:", float(np.abs(sp_out - sp_ref).max()))


if __name__ == "__main__":
    main()

"""Memory planning, TP/PP/MoE and KV-cache arithmetic."""

import numpy as np

from _common import header

from mini_verl.engine import estimate_memory
from mini_verl.parallel import (
    ColumnParallelLinear,
    PipelineSchedule,
    RowParallelLinear,
    kv_cache_bytes,
    moe_route,
    ulysses_merge,
    ulysses_split,
)


def main() -> None:
    header("Memory and parallelism: the numbers behind the config choices")
    report = estimate_memory(
        num_parameters=7_000_000_000,
        bytes_per_param=2,
        world_size=8,
        zero_stage=3,
        activation_tokens=4096,
        hidden_size=4096,
        num_layers=32,
        kv_heads=8,
        head_dim=128,
        sequence_length=4096,
        batch_size=1,
    )
    print("7B, 8 GPUs, ZeRO-3:", {key: round(value, 2) for key, value in report.items()})

    rng = np.random.default_rng(0)
    x = rng.normal(size=(2, 8)).astype(np.float32)
    w = rng.normal(size=(8, 12)).astype(np.float32)
    b = rng.normal(size=(12,)).astype(np.float32)
    dense = x @ w + b
    column = ColumnParallelLinear(w, b, tp_size=4)
    print("column-parallel max error:", float(np.abs(column.forward(x) - dense).max()))

    w2 = rng.normal(size=(12, 8)).astype(np.float32)
    b2 = rng.normal(size=(8,)).astype(np.float32)
    hidden = dense
    row = RowParallelLinear(w2, b2, tp_size=4)
    print("row-parallel max error:", float(np.abs(row.forward(hidden) - (hidden @ w2 + b2)).max()))

    tokens = rng.normal(size=(2, 12, 16)).astype(np.float32)
    chunks = ulysses_split(tokens, 4)
    print("Ulysses chunks:", [chunk.shape for chunk in chunks])
    print("Ulysses roundtrip:", np.allclose(ulysses_merge(chunks, 4), tokens))

    schedule = PipelineSchedule(num_microbatches=16, num_stages=4)
    print("pipeline bubble ratio:", round(schedule.bubble_ratio(), 3))

    router = rng.normal(size=(8, 4)).astype(np.float32)
    routing = moe_route(router, top_k=2, capacity_factor=1.0)
    print("MoE expert counts:", routing["expert_counts"].tolist(), "dropped:", int(routing["dropped_tokens"][0]))
    print("KV cache GiB:", round(kv_cache_bytes(8, 4096, 32, 8, 128, 2) / 1024**3, 2))


if __name__ == "__main__":
    main()

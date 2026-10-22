"""Multi-adapter serving, adapter residency and model routing."""

from __future__ import annotations

import numpy as np

from _common import add_project_root

add_project_root()

from mini_deepinfer.lora import AdapterRegistry, apply_batched, make_adapter
from mini_deepinfer.router import ModelRouter, ModelSpec, cascade_estimate


def main() -> None:
    rng = np.random.default_rng(0)
    in_features, out_features = 8, 6
    x = rng.normal(size=(4, in_features)).astype(np.float32)
    base_weight = rng.normal(size=(in_features, out_features)).astype(np.float32)
    base_output = x @ base_weight

    sql = make_adapter("sql", in_features, out_features, rank=2, seed=1)
    legal = make_adapter("legal", in_features, out_features, rank=4, seed=2)

    print("base output row 0     :", np.round(base_output[0], 4).tolist())
    print("sql delta row 0       :", np.round(sql.project("q_proj", x)[0], 4).tolist())
    print("adapter bytes (sql)   :", sql.nbytes)
    print("rank-padded equivalent:", np.allclose(
        sql.project("q_proj", x), sql.rank_padded(4).project("q_proj", x), atol=1e-6
    ))

    ids: list[str | None] = ["sql", "legal", None, "sql"]
    batched = apply_batched(
        "q_proj", x, base_output, ids, {"sql": sql, "legal": legal}
    )
    loop = base_output.copy()
    for row, adapter_id in enumerate(ids):
        if adapter_id is not None:
            adapter = {"sql": sql, "legal": legal}[adapter_id]
            loop[row] = adapter.apply(
                "q_proj", x[row : row + 1], base_output[row : row + 1]
            )[0]
    print("batched == row loop   :", np.allclose(batched, loop, atol=1e-6))
    print("rows without adapter  :", np.allclose(batched[2], base_output[2]))

    registry = AdapterRegistry(max_adapters=2)
    registry.register(sql)
    registry.register(legal)
    registry.get("sql")
    registry.register(make_adapter("chat", in_features, out_features, rank=2, seed=3))
    print("registry stats        :", registry.stats())

    models = [
        ModelSpec("small", 0.70, 0.10, 0.30, 100.0, 2.0, max_context=32_000),
        ModelSpec("medium", 0.85, 0.50, 1.50, 200.0, 3.5, max_context=128_000),
        ModelSpec("large", 0.95, 2.00, 6.00, 400.0, 6.0, max_context=1_000_000),
    ]
    router = ModelRouter(models, quality_floor=0.80)
    for label, tokens_in, tokens_out in [
        ("short chat", 1_000, 500),
        ("long agent", 90_000, 2_000),
    ]:
        decision = router.route(tokens_in, tokens_out)
        print(
            f"{label:12s}: model={decision.model} cost=${decision.cost_usd:.4f} "
            f"latency={decision.latency_ms:.0f}ms feasible={decision.feasible}"
        )

    strict = router.route(1_000, 500, quality_floor=0.95, latency_budget_ms=300)
    print("strict route          :", strict.model, strict.reason, strict.feasible)

    cascade = cascade_estimate(models[:2], 10_000, 1_000, [0.9, 0.9])
    print("cascade expected cost :", round(cascade.expected_cost_usd, 6))
    print("cascade expected qual :", round(cascade.expected_quality, 4))
    print("cascade expected calls:", round(cascade.expected_calls, 4))


if __name__ == "__main__":
    main()

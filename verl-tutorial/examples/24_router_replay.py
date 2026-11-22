"""Router replay: align MoE expert assignments between rollout and training.

The example shows the two failure modes that matter in production:

* replaying recorded routes reproduces them even when the local logits differ;
* a missing hash-router layer shifts every later layer, and the layer-count
  check catches it before a silently wrong update is applied.
"""

import numpy as np

from _common import header

from mini_verl.parallel import (
    moe_route,
    record_moe_routes,
    replay_moe_routes,
    router_replay_report,
)


def _logits(seed: int, tokens: int = 6, experts: int = 4) -> np.ndarray:
    return np.random.default_rng(seed).normal(size=(tokens, experts)).astype(np.float32)


def main() -> None:
    header("Router replay: rollout records, training replays")

    # 1. Rollout backend records one table per MoE layer.
    rollout_logits = [_logits(seed) for seed in range(4)]
    recorded = [
        record_moe_routes(moe_route(logits, top_k=2, capacity_factor=10.0))
        for logits in rollout_logits
    ]
    print("recorded layers:", len(recorded), "shape:", recorded[0].shape)

    # 2. Training logits differ (different precision/kernels); replay restores
    #    the rollout assignment for every layer.
    train_logits = [_logits(100 + seed) for seed in range(4)]
    replayed = replay_moe_routes(train_logits, recorded, top_k=2, capacity_factor=10.0)
    report = router_replay_report(recorded, [r["top_idx"] for r in replayed])
    print("exact replay:", report)

    # 3. A row mask replays only selected tokens and routes the rest natively.
    mixed = moe_route(
        train_logits[0],
        top_k=2,
        capacity_factor=10.0,
        replay_top_idx=recorded[0],
        replay_mask=np.asarray([1, 0, 1, 0, 1, 0], dtype=bool),
    )
    native = moe_route(train_logits[0], top_k=2, capacity_factor=10.0)
    print("row 0 replayed:", np.array_equal(mixed["top_idx"][0], recorded[0][0]))
    print("row 1 native:  ", np.array_equal(mixed["top_idx"][1], native["top_idx"][1]))

    # 4. The hash-router trap: three hash layers + one learned layer recorded,
    #    but training only supplies three layers.  Refuse instead of shifting.
    try:
        replay_moe_routes(train_logits[:3], recorded, top_k=2, capacity_factor=10.0)
    except ValueError as exc:
        print("layer mismatch caught:", str(exc)[:70], "...")

    # 5. Capacity is a separate axis: the same top-k can run a different set of
    #    slots.  Recording `accepted` is what makes that visible.
    tight = moe_route(rollout_logits[0], top_k=2, capacity_factor=0.1)
    recorded_with_accept = record_moe_routes(tight, with_accepted=True)
    loose = moe_route(
        train_logits[0],
        top_k=2,
        capacity_factor=10.0,
        replay_top_idx=recorded_with_accept["top_idx"],
    )
    capacity_report = router_replay_report(
        [recorded_with_accept],
        [{"top_idx": loose["top_idx"], "accepted": loose["accepted"]}],
    )
    print("capacity drop report:", capacity_report)

    # 6. A single corrupted route is localized to its layer and token.
    corrupted = [route.copy() for route in recorded]
    corrupted[2][0, 0] = (corrupted[2][0, 0] + 1) % 4
    print("corruption report:", router_replay_report(recorded, corrupted))


if __name__ == "__main__":
    main()

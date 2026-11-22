"""Router replay: record, replay, row masks and layer-order checks."""

from __future__ import annotations

import numpy as np
import pytest

from mini_verl.parallel import (
    moe_route,
    record_moe_routes,
    replay_moe_routes,
    router_replay_report,
)


def _logits(seed: int, tokens: int = 6, experts: int = 4) -> np.ndarray:
    return np.random.default_rng(seed).normal(size=(tokens, experts)).astype(np.float32)


def test_replay_reproduces_recorded_routes_even_when_logits_change():
    first = moe_route(_logits(0), top_k=2, capacity_factor=10.0)
    recorded = record_moe_routes(first)
    second = moe_route(
        _logits(1),
        top_k=2,
        capacity_factor=10.0,
        replay_top_idx=recorded,
    )
    assert np.array_equal(second["top_idx"], recorded)
    assert bool(second["replayed"][0])
    assert not np.array_equal(second["native_top_idx"], recorded)


def test_replay_mask_mixes_recorded_and_native_rows():
    first = moe_route(_logits(2), top_k=2, capacity_factor=10.0)
    recorded = record_moe_routes(first)
    logits = _logits(3)
    mask = np.asarray([1, 0, 1, 0, 1, 0], dtype=bool)
    mixed = moe_route(
        logits,
        top_k=2,
        capacity_factor=10.0,
        replay_top_idx=recorded,
        replay_mask=mask,
    )
    native = moe_route(logits, top_k=2, capacity_factor=10.0)
    assert np.array_equal(mixed["top_idx"][mask], recorded[mask])
    assert np.array_equal(mixed["top_idx"][~mask], native["top_idx"][~mask])


def test_replay_validates_shapes_and_ids():
    with pytest.raises(ValueError, match="at least 1"):
        moe_route(_logits(4), top_k=0)
    recorded = record_moe_routes(moe_route(_logits(4), top_k=2, capacity_factor=10.0))
    with pytest.raises(ValueError, match="shape"):
        moe_route(_logits(5), top_k=2, capacity_factor=10.0, replay_top_idx=recorded[:, :1])
    with pytest.raises(ValueError, match="out-of-range"):
        moe_route(
            _logits(5),
            top_k=2,
            capacity_factor=10.0,
            replay_top_idx=np.full_like(recorded, 99),
        )
    with pytest.raises(ValueError, match="replay_mask"):
        moe_route(
            _logits(5),
            top_k=2,
            capacity_factor=10.0,
            replay_top_idx=recorded,
            replay_mask=np.ones(3, dtype=bool),
        )
    with pytest.raises(ValueError, match="numeric"):
        moe_route(
            _logits(5),
            top_k=2,
            capacity_factor=10.0,
            replay_top_idx=np.full((6, 2), "x"),
        )
    empty = moe_route(
        np.zeros((0, 4), dtype=np.float32),
        top_k=2,
        capacity_factor=10.0,
        replay_top_idx=np.zeros((0, 2), dtype=np.int64),
    )
    assert empty["top_idx"].shape == (0, 2)


def test_layer_count_mismatch_is_rejected():
    recorded = [record_moe_routes(moe_route(_logits(6), top_k=2, capacity_factor=10.0))]
    with pytest.raises(ValueError, match="layer mismatch"):
        replay_moe_routes([_logits(7)] * 2, recorded)


def test_report_localizes_the_first_bad_layer():
    recorded = [
        record_moe_routes(moe_route(_logits(seed), top_k=2, capacity_factor=10.0))
        for seed in (8, 9, 10)
    ]
    replayed = [route.copy() for route in recorded]
    replayed[1][0, 0] = (replayed[1][0, 0] + 1) % 4
    report = router_replay_report(recorded, replayed)
    assert report["exact_match"] is False
    assert report["mismatched_layers"] == [1]
    assert report["first_mismatch"] == 1
    assert report["mismatched_tokens"] == 1

    clean = router_replay_report(recorded, recorded)
    assert clean["exact_match"] is True
    assert clean["first_mismatch"] is None


def test_route_input_and_expert_output_validation():
    logits = _logits(12)
    with pytest.raises(ValueError, match="2D"):
        moe_route(np.zeros(4, dtype=np.float32))
    with pytest.raises(ValueError, match="2D"):
        moe_route(np.zeros((2, 3, 4), dtype=np.float32))
    with pytest.raises(ValueError, match="integer"):
        moe_route(logits, top_k=2.5)
    with pytest.raises(ValueError, match="real numeric"):
        moe_route(logits.astype(np.complex64))
    with pytest.raises(ValueError, match="finite"):
        moe_route(np.full((3, 4), np.inf, dtype=np.float32))
    with pytest.raises(ValueError, match="replay_mask requires"):
        moe_route(logits, replay_mask=np.ones(6, dtype=bool))
    with pytest.raises(ValueError, match="expert_outputs"):
        moe_route(logits, top_k=2, expert_outputs=np.zeros((4, 6), dtype=np.float32))


def test_dropped_tokens_counts_unique_tokens_not_slots():
    result = moe_route(_logits(13), top_k=2, capacity_factor=0.1)
    assert result["dropped_slots"][0] >= result["dropped_tokens"][0]
    assert result["dropped_tokens"][0] <= 6


def test_report_sees_capacity_drop_even_when_top_k_matches():
    rollout = moe_route(_logits(14), top_k=2, capacity_factor=0.1)
    recorded = record_moe_routes(rollout, with_accepted=True)
    training = moe_route(
        _logits(15),
        top_k=2,
        capacity_factor=10.0,
        replay_top_idx=recorded["top_idx"],
    )
    report = router_replay_report(
        [recorded],
        [{"top_idx": training["top_idx"], "accepted": training["accepted"]}],
    )
    assert report["accepted_compared"] == 1
    assert report["mismatched_tokens"] == 0
    assert report["mismatched_assignments"] > 0
    assert report["exact_match"] is False


def test_report_reports_shape_mismatch_separately():
    report = router_replay_report([np.zeros((3, 2), dtype=np.int64)], [np.zeros((2, 2), dtype=np.int64)])
    assert report["shape_mismatched_layers"] == [0]
    assert report["exact_match"] is False
    assert report["first_mismatch"] == 0


def test_recorded_routes_are_independent_copies():
    result = moe_route(_logits(11), top_k=2, capacity_factor=10.0)
    recorded = record_moe_routes(result)
    result["top_idx"][:] = 0
    assert not np.array_equal(recorded, result["top_idx"])

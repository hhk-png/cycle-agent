from __future__ import annotations

from mini_deepinfer import EngineConfig, LLM
from mini_deepinfer.offline_batch import (
    BatchItem,
    default_cost,
    plan_waves,
    run_offline_batch,
)


def test_plan_waves_respects_limits_and_priority() -> None:
    items = [
        BatchItem("a", "a" * 10, max_tokens=4, priority=0),
        BatchItem("b", "b" * 10, max_tokens=4, priority=5),
        BatchItem("c", "c" * 10, max_tokens=4, priority=1),
    ]
    waves = plan_waves(items, max_batch_size=2, max_batched_tokens=40)
    assert len(waves) == 2
    assert all(len(wave) <= 2 for wave in waves)
    assert all(wave.estimated_tokens <= 40 for wave in waves)
    # The high-priority item is scheduled first.
    assert items[waves[0].indices[0]].request_id == "b"


def test_plan_waves_allows_oversized_item_alone() -> None:
    items = [
        BatchItem("big", "x" * 100, max_tokens=32),
        BatchItem("small", "y" * 4, max_tokens=4),
    ]
    waves = plan_waves(items, max_batch_size=4, max_batched_tokens=64)
    assert any(len(wave) == 1 and items[wave.indices[0]].request_id == "big" for wave in waves)
    assert default_cost(items[0]) > 64


def test_offline_batch_runs_and_reports() -> None:
    llm = LLM(
        config=EngineConfig(
            num_blocks=64,
            block_size=16,
            max_num_seqs=4,
            max_num_batched_tokens=128,
            max_model_len=128,
        )
    )
    items = [
        BatchItem("r0", "DeepInfer is", max_tokens=6, seed=0, deadline_s=1000.0),
        BatchItem("r1", "Once upon a time", max_tokens=6, seed=1, deadline_s=1000.0),
        BatchItem("r2", "The cat sat", max_tokens=6, seed=2, priority=3),
    ]
    report = run_offline_batch(llm, items, max_batch_size=2, max_batched_tokens=96)
    assert len(report.results) == 3
    assert {result.request_id for result in report.results} == {"r0", "r1", "r2"}
    assert report.total_output_tokens > 0
    assert report.elapsed_s > 0
    assert report.output_tokens_per_s > 0
    assert report.deadline_miss_rate == 0.0
    assert report.as_dict()["requests"] == 3


def test_empty_offline_batch() -> None:
    llm = LLM(config=EngineConfig(num_blocks=32, block_size=16, max_model_len=64))
    report = run_offline_batch(llm, [])
    assert report.results == []
    assert report.output_tokens_per_s == 0.0

import pytest

from mini_megatron.pipeline.interleaved import (
    VirtualPipelineLayout,
    gpipe_interleaved_trace,
    max_inflight_forwards,
    non_interleaved_bubble_ratio,
    rank_view,
    validate_trace,
    vpp_bubble_ratio,
)


def test_layout_owner_and_layers():
    layout = VirtualPipelineLayout(pp_size=4, chunks_per_rank=2, num_layers=16)
    assert layout.num_chunks == 8
    assert layout.layers_per_chunk == 2
    assert layout.owner(0) == 0 and layout.owner(5) == 1
    assert layout.local_chunk(5) == 1
    assert list(layout.layers_of_chunk(5)) == [10, 11]
    assert layout.chunks_of_rank(1) == [1, 5]


def test_layout_requires_divisible_layers():
    with pytest.raises(ValueError):
        VirtualPipelineLayout(pp_size=4, chunks_per_rank=2, num_layers=15)


def test_gpipe_trace_is_valid_and_balanced():
    layout = VirtualPipelineLayout(pp_size=2, chunks_per_rank=2, num_layers=8)
    trace = gpipe_interleaved_trace(layout, microbatches=3)
    validate_trace(trace, layout)
    assert len(trace) == 3 * 4 * 2
    per_rank = [len(rank_view(trace, layout, r)) for r in range(2)]
    assert per_rank == [12, 12]
    assert max_inflight_forwards(trace, layout) == 12


def test_dependency_violation_is_detected():
    layout = VirtualPipelineLayout(pp_size=2, chunks_per_rank=1, num_layers=2)
    bad = [("F", 0, 1), ("F", 0, 0)]
    with pytest.raises(ValueError):
        validate_trace(bad, layout)
    bad_backward = [("F", 0, 0), ("F", 0, 1), ("B", 0, 0)]
    with pytest.raises(ValueError):
        validate_trace(bad_backward, layout)


def test_vpp_bubble_is_smaller():
    m, p, v = 16, 8, 2
    assert vpp_bubble_ratio(m, p, v) < non_interleaved_bubble_ratio(m, p)
    assert vpp_bubble_ratio(m, p, 1) == non_interleaved_bubble_ratio(m, p)


def test_tied_embeddings_with_vpp_is_rejected(monkeypatch):
    """The check must raise NotImplementedError, not NameError.

    Regression: ``interleaved_exec`` used a local ``pp_size`` that was never
    defined, so the documented rejection path crashed with NameError before
    the message was printed.
    """
    from mini_megatron.config import ModelConfig, ParallelConfig, TrainConfig
    from mini_megatron.pipeline import interleaved_exec

    monkeypatch.setattr(
        interleaved_exec, "get_pipeline_model_parallel_world_size", lambda: 2
    )
    monkeypatch.setattr(
        interleaved_exec, "get_pipeline_model_parallel_rank", lambda: 0
    )
    monkeypatch.setattr(
        interleaved_exec, "get_pipeline_model_parallel_group", lambda: None
    )
    config = ModelConfig(tie_word_embeddings=True, num_layers=2)
    with pytest.raises(NotImplementedError):
        interleaved_exec.InterleavedGPipeExecutor(
            config, ParallelConfig(), TrainConfig(), chunks_per_rank=1
        )

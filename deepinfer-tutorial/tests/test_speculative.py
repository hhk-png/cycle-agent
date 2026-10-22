from __future__ import annotations

import re

from mini_deepinfer.config import EngineConfig
from mini_deepinfer.llm import LLM
from mini_deepinfer.roofline import expected_accept_length
from mini_deepinfer.sampling_params import SamplingParams
from mini_deepinfer.speculative import (
    DraftProposal,
    choose_verify_length,
    expected_accepted_length,
)


def test_expected_accepted_length() -> None:
    assert expected_accepted_length([1.0, 0.5, 0.5]) == 1.75
    assert expected_accepted_length([]) == 0.0


def test_accept_length_definitions_differ_by_bonus_token() -> None:
    confidences = [0.9, 0.72, 0.5]
    assert expected_accept_length(confidences) == 1.0 + expected_accepted_length(confidences)


def test_choose_verify_length_respects_threshold() -> None:
    assert 1 <= choose_verify_length([0.9, 0.8, 0.2, 0.9], threshold=0.55) <= 2
    assert choose_verify_length([], threshold=0.5) == 0
    assert choose_verify_length([0.1], threshold=0.5) == 1


def test_speculative_acceptance_and_stats(model, draft_model) -> None:
    llm = LLM(
        model=model,
        draft_model=draft_model,
        config=EngineConfig(
            num_blocks=64,
            max_num_batched_tokens=128,
            max_num_seqs=4,
            max_model_len=256,
            enable_speculative=True,
            speculative_tokens=4,
            confidence_threshold=0.0,
        ),
    )
    llm.generate("DeepInfer is", SamplingParams(temperature=0.0, max_tokens=20))
    stats = llm.engine.spec_stats
    assert stats.steps > 0
    assert stats.verified > 0
    assert 0.0 <= stats.acceptance_rate <= 1.0
    assert stats.mean_accepted > 0


def test_speculative_with_structured_output_matches_target(model, draft_model) -> None:
    """A grammar and speculative decoding must compose without corrupting state.

    The verifier masks the target distribution at every position and replays
    only the committed tokens into the real matcher.  With greedy decoding the
    output must therefore equal target-only greedy decoding under the same
    grammar, and must satisfy the grammar.
    """
    params = SamplingParams(
        temperature=0.0,
        max_tokens=8,
        seed=7,
        so_args={"schema_type": "regex", "schema": "[a-z]+"},
    )
    common = dict(
        num_blocks=64,
        max_num_batched_tokens=128,
        max_num_seqs=4,
        max_model_len=128,
    )
    baseline = LLM(
        model=model,
        config=EngineConfig(**common, enable_speculative=False),
    )
    speculative = LLM(
        model=model,
        draft_model=draft_model,
        config=EngineConfig(
            **common,
            enable_speculative=True,
            speculative_tokens=4,
            confidence_threshold=0.0,
        ),
    )
    expected = baseline.generate("abc", [params])[0]
    actual = speculative.generate("abc", [params])[0]
    assert actual == expected
    assert re.fullmatch(r"[a-z]+", actual)
    assert speculative.engine.spec_stats.verified > 0


def test_draft_proposal_shape(draft_model, model) -> None:
    from mini_deepinfer.sampling_params import SamplingParams
    from mini_deepinfer.sequence import Sequence
    from mini_deepinfer.speculative import generate_drafts

    seq = Sequence(0, "r", [65, 66], SamplingParams(temperature=0.0))
    proposal = generate_drafts(draft_model, seq, 3, seq.sampling_params)
    assert len(proposal.tokens) == 3
    assert len(proposal.probs) == 3
    assert all(0.0 <= c <= 1.0 for c in proposal.confidences)
    assert all(p.shape[0] == model.config.vocab_size for p in proposal.probs)

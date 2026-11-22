from __future__ import annotations

import numpy as np
import pytest

from mini_verl.data import CharTokenizer, build_dataset, collate_fn, make_batches, preference_pairs
from mini_verl.protocol import DataProto
from mini_verl.rewards import (
    DAPORewardManager,
    RewardLoopManager,
    RewardManager,
    bandit_reward,
    exact_match_reward,
    extract_boxed,
    format_reward,
    length_penalty,
    load_custom_reward,
    math_reward,
    normalize_number,
)


def test_tokenizer_roundtrip():
    tokenizer = CharTokenizer()
    text = "Tom has 3 apples. Answer: \\boxed{5}"
    ids = tokenizer.encode(text)
    assert tokenizer.decode(ids) == text
    assert tokenizer.decode(tokenizer.encode("hello", add_eos=True)) == "hello"
    assert tokenizer.stoi["<pad>"] == 0


def test_dataset_factories():
    for kind in ["bandit", "copy", "arithmetic", "math"]:
        dataset = build_dataset(kind, 16, seed=0)
        assert len(dataset) == 16
        assert dataset[0].prompt
        assert dataset[0].answer


def test_bandit_uid_unique_and_mapping():
    dataset = build_dataset("bandit", 16, seed=0)
    uids = {sample.uid for sample in dataset}
    assert len(uids) == 16
    for sample in dataset:
        context = sample.prompt[-1]
        assert sample.answer == "xyzw"[ord(context) - ord("A")]


def test_collate_shapes_and_masks():
    tokenizer = CharTokenizer()
    dataset = build_dataset("bandit", 4, seed=0)
    batch = collate_fn(dataset.samples, tokenizer, max_prompt_length=4, max_response_length=1)
    assert len(batch) == 4
    assert batch.batch["input_ids"].shape == (4, 5)
    assert batch.batch["response_mask"].shape == (4, 5)
    assert np.all(batch.batch["response_mask"].sum(axis=-1) == 1)
    assert batch.non_tensor_batch["answers"].tolist() == [s.answer for s in dataset.samples]
    assert batch.batch["prompt_length"].tolist() == [2, 2, 2, 2]


def test_make_batches_drop_last():
    tokenizer = CharTokenizer()
    dataset = build_dataset("bandit", 10, seed=0)
    batches = make_batches(dataset, 4, tokenizer, 4, 1, shuffle=False, drop_last=True)
    assert [len(batch) for batch in batches] == [4, 4]
    batches = make_batches(dataset, 4, tokenizer, 4, 1, shuffle=False, drop_last=False)
    assert [len(batch) for batch in batches] == [4, 4, 2]


def test_rewards():
    assert extract_boxed("so \\boxed{12}") == "12"
    assert extract_boxed("none") is None
    assert normalize_number("1,000") == "1000"
    assert normalize_number("5.0") == "5"
    assert normalize_number("$3.50") == "3.5"
    assert exact_match_reward("x", "x") == 1.0
    assert exact_match_reward("x ", "x") == 1.0
    assert math_reward("\\boxed{7}", "7") == 1.0
    assert math_reward("the answer is 7", "7") == 1.0
    assert math_reward("\\boxed{8}", "7") == 0.0
    assert bandit_reward("xabc", "x") == 1.0
    assert bandit_reward("y", "x") == 0.0
    assert format_reward("\\boxed{1}") > 0.0
    assert length_penalty(20, 10, 0.1) < 0.0


def test_reward_manager_token_placement():
    tokenizer = CharTokenizer()
    dataset = build_dataset("bandit", 2, seed=0)
    data = collate_fn(dataset.samples, tokenizer, 4, 1)
    data.non_tensor_batch["response_text"] = np.asarray(
        [dataset.samples[0].answer, "q"], dtype=object
    )
    manager = RewardManager("bandit")
    out = manager.run(data)
    assert out.batch["rm_scores"].tolist() == [1.0, 0.0]
    assert out.batch["token_level_rewards"][0, 2] == 1.0
    assert out.batch["token_level_rewards"][0].sum() == 1.0


def test_dapo_overlong_penalty():
    tokenizer = CharTokenizer()
    dataset = build_dataset("bandit", 1, seed=0)
    data = collate_fn(dataset.samples, tokenizer, 4, 2)
    data.batch["response_mask"][0, 2:] = 1.0
    data.batch["truncated"] = np.asarray([1], dtype=np.int64)
    data.non_tensor_batch["response_text"] = np.asarray(["x"], dtype=object)
    manager = DAPORewardManager("bandit")
    out = manager.run(data)
    assert out.batch["rm_scores"][0] <= 0.0


def test_natural_eos_at_budget_is_not_truncated():
    tokenizer = CharTokenizer()
    dataset = build_dataset("bandit", 1, seed=0)
    data = collate_fn(dataset.samples, tokenizer, 4, 2)
    data.batch["response_mask"][0, 2:] = 1.0
    data.batch["truncated"] = np.asarray([0], dtype=np.int64)
    data.non_tensor_batch["response_text"] = np.asarray(["x"], dtype=object)
    manager = DAPORewardManager("bandit")
    out = manager.run(data)
    assert out.batch["rm_scores"][0] == 1.0


def test_custom_reward_spec():
    fn = load_custom_reward("mini_verl.rewards:exact_match_reward")
    assert fn(response="a", answer="a") == 1.0
    with pytest.raises(ValueError):
        load_custom_reward("bogus")


def test_reward_loop_manager():
    tokenizer = CharTokenizer()
    dataset = build_dataset("bandit", 4, seed=0)
    data = collate_fn(dataset.samples, tokenizer, 4, 1)
    data.non_tensor_batch["response_text"] = np.asarray([s.answer for s in dataset.samples], dtype=object)
    manager = RewardLoopManager("bandit", max_workers=2)
    out = manager.run(data)
    assert out.batch["rm_scores"].tolist() == [1.0, 1.0, 1.0, 1.0]
    assert manager.failures == 0


def test_preference_pairs():
    tokenizer = CharTokenizer()
    dataset = build_dataset("bandit", 3, seed=0)
    pairs = preference_pairs(dataset, tokenizer, 4, 2)
    assert len(pairs) == 3
    assert pairs[0].batch["chosen_ids"].shape[1] <= 2

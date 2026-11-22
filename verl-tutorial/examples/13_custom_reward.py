"""Load a custom reward function and use it in a RewardManager."""

import numpy as np

from _common import header

from mini_verl.data import CharTokenizer, build_dataset, collate_fn
from mini_verl.rewards import RewardManager, load_custom_reward


def main() -> None:
    header("Custom verifiable reward: module:function, no framework changes")
    fn = load_custom_reward("examples.custom_reward:length_aware_reward")
    print("correct short:", fn(response="x", answer="x"))
    print("correct long:", fn(response="x" + "z" * 20, answer="x"))
    print("wrong:", fn(response="y", answer="x"))

    tokenizer = CharTokenizer()
    dataset = build_dataset("bandit", 4, seed=0)
    data = collate_fn(dataset.samples, tokenizer, 4, 1)
    data.non_tensor_batch["response_text"] = np.asarray(
        [sample.answer if i % 2 == 0 else "q" for i, sample in enumerate(dataset.samples)], dtype=object
    )
    manager = RewardManager("examples.custom_reward:length_aware_reward")
    out = manager.run(data)
    print("scores:", out.batch["rm_scores"].tolist())


if __name__ == "__main__":
    main()

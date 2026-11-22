"""Rollout sleep/wake, prefix cache and weight synchronization."""

import numpy as np

from _common import header

from mini_verl.config import ModelConfig, RolloutConfig
from mini_verl.data import CharTokenizer, build_dataset, collate_fn
from mini_verl.rollout import build_rollout
from mini_verl.sharding import CheckpointEngineManager


def main() -> None:
    header("Rollout lifecycle: generate -> sleep -> sync -> wake")
    tokenizer = CharTokenizer()
    dataset = build_dataset("bandit", 4, seed=0)
    prompts = collate_fn(dataset.samples, tokenizer, 4, 1)
    config = RolloutConfig(prompt_length=4, response_length=1, allowed_chars="xyzw", enable_prefix_caching=True)
    model_config = ModelConfig(arch="linear", hidden_size=8, max_seq_len=8)
    rollout = build_rollout(config, tokenizer, seed=0, model_config=model_config)

    first = rollout.generate_sequences(prompts)
    second = rollout.generate_sequences(prompts)
    print("responses:", first.non_tensor_batch["response_text"].tolist())
    print("prefix hits:", second.meta_info["prefix_hits"], "prefill tokens saved:", second.meta_info["prefill_tokens_saved"])
    print("rollout memory bytes:", rollout.memory_bytes())

    rollout.sleep()
    print("after sleep, model is None:", rollout.model is None, "memory bytes:", rollout.memory_bytes())

    manager = CheckpointEngineManager(backend="naive", replicas=1)
    state = {key: np.zeros_like(value) for key, value in rollout._backup.items()}
    stats = manager.update_weights(state, [rollout])
    rollout.wake_up()
    print("sync bytes:", stats.bytes_sent, "version:", stats.version)
    print("awake after sync:", rollout.model is not None)

    manager.sleep([rollout])
    manager.wake_up([rollout])
    print("CheckpointEngineManager sleep/wake ok:", rollout.model is not None)


if __name__ == "__main__":
    main()

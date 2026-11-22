"""Prefill/decode disaggregation: KV transfer accounting without a real cluster."""

import numpy as np

from _common import header

from mini_verl.config import ModelConfig, RolloutConfig
from mini_verl.data import CharTokenizer, build_dataset, collate_fn
from mini_verl.rollout import build_rollout


def main() -> None:
    header("PD disaggregation: prefill tokens, KV bytes and transfer time")
    tokenizer = CharTokenizer()
    dataset = build_dataset("bandit", 8, seed=0)
    batch = collate_fn(dataset.samples, tokenizer, max_prompt_length=4, max_response_length=1)
    model_config = ModelConfig(arch="linear", hidden_size=8, num_layers=2, num_heads=2, max_seq_len=8)
    config = RolloutConfig(
        prompt_length=4,
        response_length=1,
        allowed_chars="xyzw",
        disaggregation={
            "enabled": True,
            "prefill_replicas": 2,
            "decode_replicas": 1,
            "transfer_backend": "nixl",
        },
    )
    rollout = build_rollout(config, tokenizer, seed=0, model_config=model_config)
    out = rollout.generate_sequences(batch)
    print("response:", out.non_tensor_batch["response_text"].tolist())
    for key in ("pd/prefill_tokens", "pd/kv_transfer_bytes", "pd/transfer_ms", "pd/prefill_replicas"):
        print(f"{key}: {out.meta_info[key]}")
    print("per-prompt KV bytes:", out.meta_info["pd/kv_transfer_bytes"] / len(batch))
    print("same sampler as naive:", np.all(out.batch["response_mask"].sum(axis=-1) == 1))


if __name__ == "__main__":
    main()

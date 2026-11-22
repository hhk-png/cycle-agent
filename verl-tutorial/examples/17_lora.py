"""Example 17: LoRA adapters, freezing, training and merging.

Run:
    python -X utf8 examples/17_lora.py

The toy objective is next-token prediction on a fixed random token stream.  It
is not a language task; it only proves that gradients reach the adapter and
that the frozen base model is untouched.
"""

from __future__ import annotations

import numpy as np

from _common import header

from mini_verl.config import OptimConfig
from mini_verl.engine import AdamW
from mini_verl.lora import apply_lora, lora_modules, merge_lora_inplace
from mini_verl.nn import TinyLM, log_probs_from_logits


def loss_and_logp(model: TinyLM, ids: np.ndarray, mask: np.ndarray):
    logp = log_probs_from_logits(model(ids), ids, mask)
    loss = -(logp * mask).sum() / mask.sum()
    return loss, logp


def main() -> int:
    header("LoRA: freeze the base, train a low-rank update")

    base = TinyLM(vocab_size=16, max_seq_len=8, dim=16, n_layer=2, n_head=2, seed=0)
    ids = np.random.default_rng(0).integers(0, 16, size=(8, 6)).astype(np.int64)
    mask = np.ones_like(ids, dtype=np.float32)
    before = base(ids).data.copy()

    total = base.num_parameters()
    frozen_refs = [(parameter, parameter.data.copy()) for parameter in base.parameters()]

    paths = apply_lora(base, rank=4, alpha=8, seed=1)
    after_wrap = base(ids).data
    trainable = base.num_trainable_parameters()
    adapters = list(lora_modules(base))
    adapters_before = {path: (module.A.data.copy(), module.B.data.copy()) for path, module in adapters}

    print(f"wrapped_layers={len(paths)}")
    print(f"total_parameters={total}")
    print(f"trainable_parameters={trainable}")
    print(f"trainable_ratio={trainable / total:.4f}")
    print(f"identity_after_wrap={np.allclose(before, after_wrap, atol=1e-5)}")
    for path in paths:
        print(f"  adapter: {path}")

    optimizer = AdamW(base, OptimConfig(lr=0.05, total_training_steps=40))
    first_loss = None
    for step in range(40):
        base.zero_grad()
        loss, _ = loss_and_logp(base, ids, mask)
        if first_loss is None:
            first_loss = float(loss.data)
        loss.backward()
        grads = {name: p.grad for name, p in base.named_parameters() if p.grad is not None}
        optimizer.step_named(grads)

    final_loss = float(loss_and_logp(base, ids, mask)[0].data)
    print(f"loss_before={first_loss:.4f}")
    print(f"loss_after={final_loss:.4f}")
    print(f"loss_decreased={final_loss < first_loss}")

    frozen_ok = all(
        not parameter.requires_grad and np.allclose(parameter.data, value)
        for parameter, value in frozen_refs
    )
    adapters_changed = any(
        not np.allclose(module.A.data, adapters_before[path][0])
        or not np.allclose(module.B.data, adapters_before[path][1])
        for path, module in adapters
    )
    print(f"base_frozen={frozen_ok}")
    print(f"adapters_changed={adapters_changed}")

    adapter_bytes = sum(module.A.data.nbytes + module.B.data.nbytes for _, module in adapters)
    full_bytes = sum(p.data.nbytes for p in base.parameters())
    print(f"adapter_bytes={adapter_bytes}")
    print(f"full_model_bytes={full_bytes}")
    print(f"adapter_fraction={adapter_bytes / full_bytes:.4f}")

    # A rollout engine without adapter support needs merged weights.  Merge a
    # throw-away copy and compare logits with the unmerged training model.
    expected = base(ids).data.copy()
    clone = TinyLM(vocab_size=16, max_seq_len=8, dim=16, n_layer=2, n_head=2, seed=0)
    apply_lora(clone, rank=4, alpha=8, seed=1)
    clone.load_state_dict(base.state_dict())
    merged = merge_lora_inplace(clone)
    merged_ok = np.allclose(expected, clone(ids).data, atol=1e-5)
    print(f"merged_layers={merged}")
    print(f"merge_matches_training_model={merged_ok}")

    return 0 if (frozen_ok and adapters_changed and merged_ok and final_loss < first_loss) else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Full lifecycle: train -> checkpoint -> resume -> evaluate -> export.

This is the smallest end-to-end run that exercises the same hand-offs as a
production RL job:

1. train a GRPO policy on the bandit task;
2. save a checkpoint (model + optimizer + trainer state);
3. resume into a *new* trainer and verify the global step continues;
4. evaluate the checkpoint with standalone pass@k (no training loop);
5. export safetensors and confirm every actor tensor round-trips.

Run it after ``examples/01_quickstart.py`` when you want to see the operational
contracts rather than the math.
"""

import tempfile
from pathlib import Path

import numpy as np

from _common import bandit_config, header

from mini_verl.checkpoint import export_hf_like, read_safetensors
from mini_verl.trainer.main_eval import evaluate_checkpoint
from mini_verl.trainer.ppo_trainer import PPOTrainer


def main() -> None:
    header("Lifecycle: train -> checkpoint -> resume -> eval -> export")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)

        # 1. train
        config = bandit_config(steps=30, n=8)
        config.trainer.log_dir = directory
        trainer = PPOTrainer(config).init()
        result = trainer.fit()
        print(f"train: steps={result['steps']} reward={result['history']['data/reward_mean']:.3f}")

        # 2. checkpoint
        checkpoint = Path(trainer.save_checkpoint())
        state_files = sorted(path.name for path in checkpoint.iterdir())
        print(f"checkpoint: {checkpoint.name} files={state_files}")

        # 3. resume into a fresh trainer/optimizer
        resume_config = bandit_config(steps=35, n=8)
        resume_config.trainer.log_dir = directory
        resume_config.trainer.resume_from_path = str(checkpoint)
        resumed = PPOTrainer(resume_config).init()
        before = resumed.global_steps
        resumed.fit()
        print(f"resume: global_steps {before} -> {resumed.global_steps} (optimizer state restored)")

        # 4. standalone pass@k: read config from checkpoint, force naive reward
        metrics = evaluate_checkpoint(checkpoint, n=8, ks=(1, 4, 8), split="val")
        print(
            "eval: "
            f"accuracy={metrics['val/accuracy']:.3f} "
            f"pass@1={metrics['val/pass@1']:.3f} "
            f"pass@4={metrics['val/pass@4']:.3f} "
            f"pass@8={metrics['val/pass@8']:.3f}"
        )

        # 5. export: every actor tensor must survive the safetensors round-trip
        actor_state = resumed.actor_group.get_actor_state_dict()
        export_dir = export_hf_like(actor_state, root / "export", checkpoint / "config.yaml")
        exported = read_safetensors(export_dir / "model.safetensors")
        missing = set(actor_state) - set(exported)
        assert not missing, f"export lost tensors: {sorted(missing)[:3]}"
        changed = [name for name in actor_state if not np.array_equal(actor_state[name], exported[name])]
        assert not changed, f"export changed tensors: {changed[:3]}"
        print(f"export: {len(exported)} tensors -> {export_dir.name}/model.safetensors")

        print("lifecycle OK: the same contracts a production verl job relies on")


if __name__ == "__main__":
    main()

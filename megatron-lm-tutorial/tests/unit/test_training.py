import json
from pathlib import Path

import torch

from mini_megatron.config import ExperimentConfig
from mini_megatron.parallel.state import initialize_model_parallel
from mini_megatron.training import Trainer


def test_two_steps_and_checkpoint(tmp_path):
    initialize_model_parallel(1, 1, 1)
    cfg = ExperimentConfig.from_json("configs/tiny.json")
    cfg.train.max_steps = 3
    cfg.train.save_interval = 2
    cfg.train.checkpoint_dir = str(tmp_path / "ckpt")
    cfg.train.eval_interval = 0
    trainer = Trainer(cfg)
    history = trainer.fit(save_final=False)
    assert len(history) == 3
    assert all(torch.isfinite(torch.tensor(h["loss"])) for h in history)
    latest = tmp_path / "ckpt" / "latest"
    assert latest.exists()

    # ``step_000002`` means two completed updates (steps 0 and 1).  Resuming
    # must run steps 2 and 3, not skip step 2.
    cfg2 = ExperimentConfig.from_json("configs/tiny.json")
    cfg2.train.max_steps = 3
    cfg2.train.save_interval = 2
    cfg2.train.checkpoint_dir = str(tmp_path / "ckpt")
    cfg2.train.eval_interval = 0
    trainer2 = Trainer(cfg2)
    ckpt = tmp_path / "ckpt" / latest.read_text().strip()
    assert trainer2.resume(ckpt) == 2
    resumed = trainer2.fit(resume=ckpt, save_final=False)
    assert [h["step"] for h in resumed] == [2]
    for expected, actual in zip(history[2:], resumed):
        assert abs(expected["loss"] - actual["loss"]) < 1e-6
        assert abs(expected["grad_norm"] - actual["grad_norm"]) < 1e-6


def test_pre_ep_checkpoint_metadata_defaults_to_ep1(tmp_path):
    """Older checkpoints lack expert_model_parallel_size; treat them as EP=1."""
    initialize_model_parallel(1, 1, 1)
    cfg = ExperimentConfig.from_json("configs/tiny.json")
    cfg.train.max_steps = 1
    cfg.train.checkpoint_dir = str(tmp_path / "ckpt")
    trainer = Trainer(cfg)
    trainer.fit()
    step_dir = tmp_path / "ckpt" / (tmp_path / "ckpt" / "latest").read_text().strip()
    metadata_path = step_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.pop("expert_model_parallel_size", None)
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    cfg2 = ExperimentConfig.from_json("configs/tiny.json")
    cfg2.train.max_steps = 1
    cfg2.train.checkpoint_dir = str(tmp_path / "ckpt")
    trainer2 = Trainer(cfg2)
    assert trainer2.resume(step_dir) == 1


def test_optimizer_state_and_lr():
    initialize_model_parallel(1, 1, 1)
    cfg = ExperimentConfig.from_json("configs/tiny.json")
    cfg.train.max_steps = 1
    cfg.train.checkpoint_dir = ""
    trainer = Trainer(cfg)
    record = trainer.train_step(0)
    assert record["lr"] == cfg.train.lr * 0.5  # warmup step 0 of 2
    state = trainer.optimizer.state_dict()
    assert state["state"]

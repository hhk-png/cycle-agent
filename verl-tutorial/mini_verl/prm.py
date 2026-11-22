"""Process reward models (PRMs): step labels, stable BCE, aggregation, serving.

A scalar reward model (``reward_model.py``) answers one question about a whole
response: *is it good?*  A PRM answers the same question at every step:
*is the trajectory still on track after this step?*  That extra signal costs a
new data contract (step boundaries and step labels), a new loss mask, and an
aggregation rule that turns step scores back into a trajectory reward.

Real verl serves reward models through the reward loop; it does not ship a
PRM trainer.  MiniVerl implements the smallest complete PRM pipeline that can
be tested on CPU:

    step-labelled trajectories -> prefix scoring -> stable BCE
        -> step/trajectory metrics -> checkpoint
        -> ProcessRewardManager -> PPO/GAE/GRPO reward

The implementation is deliberately explicit about the two places where PRMs
usually go wrong:

1. **What a step label means.**  ``local`` marks only the corrupted step;
   ``suffix`` marks the corrupted step and everything after it.  The first is
   local correctness, the second is "still on track".  They train different
   models and support different aggregation rules.
2. **How step scores become one reward.**  ``last`` is optimistic, ``min`` is
   conservative, ``mean`` dilutes a single failure, and ``product`` is the
   probability that every step is correct.  The choice is part of the reward
   function, not a reporting detail.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from .autograd import Tensor, no_grad, softplus
from .config import OptimConfig, PPOConfig
from .data import CharTokenizer, RLHFDataset, build_dataset
from .engine import AdamW, clip_gradients
from .protocol import DataProto
from .reward_model import TinyRewardModel

__all__ = [
    "StepSample",
    "StepBatch",
    "build_step_samples",
    "step_prefixes",
    "collate_step_samples",
    "make_step_batches",
    "step_bce_loss",
    "aggregate_step_probs",
    "step_metrics",
    "PRMTrainer",
    "save_prm",
    "load_prm",
    "ProcessRewardManager",
    "AGGREGATIONS",
]

AGGREGATIONS = ("last", "first", "min", "max", "mean", "product")


# ----------------------------------------------------------------------
# data contract
# ----------------------------------------------------------------------


@dataclass
class StepSample:
    """One trajectory split into steps with a binary label per step.

    ``steps`` are response tokens, not prompt tokens.  Prefix scores are built
    by concatenating ``prompt_ids`` with the first ``i + 1`` steps, so the
    model always sees exactly the information available when that step ends.
    """

    prompt_ids: list[int]
    steps: list[list[int]]
    labels: list[int]
    uid: int
    data_source: str

    def __post_init__(self) -> None:
        if len(self.steps) != len(self.labels):
            raise ValueError(
                f"steps and labels have different lengths: {len(self.steps)} vs {len(self.labels)}"
            )
        if not self.steps:
            raise ValueError("a StepSample needs at least one step")
        if any(label not in (0, 1, False, True) for label in self.labels):
            raise ValueError(f"step labels must be binary, got {self.labels}")
        if any(not step for step in self.steps):
            raise ValueError("empty step: every step must contain at least one token")

    @property
    def num_steps(self) -> int:
        return len(self.steps)

    @property
    def response_length(self) -> int:
        return sum(len(step) for step in self.steps)

    @property
    def trajectory_label(self) -> int:
        """A trajectory is positive only when every step is positive."""

        return int(all(self.labels))


@dataclass
class StepBatch:
    """Padded *prefix* sequences consumed by ``TinyRewardModel``.

    Rows are prefixes, not trajectories.  ``group_index[row]`` says which
    trajectory a prefix belongs to; ``step_index[row]`` says which step it
    ends at.  ``labels`` are the supervision for that prefix.
    """

    input_ids: np.ndarray
    attention_mask: np.ndarray
    labels: np.ndarray
    step_index: np.ndarray
    group_index: np.ndarray
    num_groups: int

    def __len__(self) -> int:
        return int(self.input_ids.shape[0])


def _truncate_prompt(ids: list[int], data_source: str, max_prompt_length: int) -> list[int]:
    """Match the dataset contract used by ``data.collate_fn``.

    Math prompts are truncated from the right because the question head carries
    the task; other prompts keep the tail because the immediate context is the
    action cue.  Using a different rule here would make PRM prefixes disagree
    with the prefixes the policy actually saw.
    """

    return ids[:max_prompt_length] if data_source == "math" else ids[-max_prompt_length:]


def _wrong_token(token: int, tokenizer: CharTokenizer, rng: np.random.Generator) -> int:
    """Pick a deterministic, non-special token that differs from ``token``.

    Candidates are restricted to single-character alphanumeric tokens.  This
    matters for more than aesthetics: if training only ever corrupts a step
    with punctuation while rollout is constrained to letters, the PRM never
    sees the wrong tokens the policy can actually produce and the policy can
    reward-hack it.  Keeping the corruption distribution close to the rollout
    distribution is part of the data contract.
    """

    specials = {
        tokenizer.pad_token_id,
        tokenizer.bos_token_id,
        tokenizer.eos_token_id,
        tokenizer.unk_token_id,
    }
    candidates = [
        index
        for index, piece in enumerate(tokenizer.itos)
        if index not in specials and len(piece) == 1 and piece.isalnum() and index != int(token)
    ]
    if candidates:
        return int(rng.choice(candidates))
    for offset in range(1, tokenizer.vocab_size):
        candidate = (int(token) + offset) % tokenizer.vocab_size
        if candidate not in specials and candidate != int(token):
            return candidate
    raise ValueError("tokenizer has no usable non-special token for corruption")


def build_step_samples(
    dataset: RLHFDataset,
    tokenizer: CharTokenizer,
    max_prompt_length: int = 16,
    max_response_length: int = 8,
    step_tokens: int = 1,
    corruptions_per_sample: int = 1,
    label_mode: str = "local",
    seed: int = 0,
) -> list[StepSample]:
    """Turn an RL dataset into step-labelled trajectories.

    The positive trajectory is the gold answer split into chunks of
    ``step_tokens`` tokens.  Each corruption replaces one whole step with
    wrong tokens.  ``label_mode`` controls the labels after the corruption:

    * ``local``: only the corrupted step is negative;
    * ``suffix``: the corrupted step and every later step are negative.

    ``suffix`` is the safer default for a trajectory-level reward because the
    model has to learn that an earlier mistake cannot be undone by a locally
    correct later token.  ``local`` is useful when the reward should identify
    *which* step is wrong rather than score the whole trajectory.
    """

    if step_tokens <= 0:
        raise ValueError("step_tokens must be positive")
    if corruptions_per_sample < 0:
        raise ValueError("corruptions_per_sample must be non-negative")
    if label_mode not in {"local", "suffix"}:
        raise ValueError(f"unknown label_mode {label_mode!r}")

    rng = np.random.default_rng(seed)
    samples: list[StepSample] = []
    for sample in dataset:
        prompt_ids = _truncate_prompt(
            tokenizer.encode(sample.prompt), sample.data_source, max_prompt_length
        )
        # Keep EOS in the supervised response.  The rollout emits EOS as a
        # valid response token, so a PRM trained without it would see an
        # out-of-distribution final step at serving time.
        response = tokenizer.encode(sample.answer, add_eos=True)[:max_response_length]
        if not response:
            continue
        steps = [
            response[start : start + step_tokens]
            for start in range(0, len(response), step_tokens)
        ]
        labels = [1] * len(steps)
        samples.append(
            StepSample(
                prompt_ids=prompt_ids,
                steps=[list(step) for step in steps],
                labels=labels,
                uid=int(sample.uid),
                data_source=sample.data_source,
            )
        )

        # Corrupt content steps before EOS.  Replacing EOS with an ordinary
        # character tests "did the trajectory stop correctly", but it should
        # not dominate the one-corruption curriculum.
        corruptible = [
            index
            for index, step in enumerate(steps)
            if tokenizer.eos_token_id not in step
        ] or list(range(len(steps)))
        for corruption in range(corruptions_per_sample):
            # Cycle the corrupted step across samples so every position is
            # represented even when corruptions_per_sample == 1.
            position_in_order = (int(sample.uid) + corruption) % len(corruptible)
            step_index = corruptible[position_in_order]
            corrupted_steps = [list(step) for step in steps]
            position = int(rng.integers(0, len(corrupted_steps[step_index])))
            original = corrupted_steps[step_index][position]
            corrupted_steps[step_index][position] = _wrong_token(original, tokenizer, rng)
            corrupted_labels = [1] * len(steps)
            if label_mode == "suffix":
                for index in range(step_index, len(corrupted_labels)):
                    corrupted_labels[index] = 0
            else:
                corrupted_labels[step_index] = 0
            samples.append(
                StepSample(
                    prompt_ids=list(prompt_ids),
                    steps=corrupted_steps,
                    labels=corrupted_labels,
                    uid=int(sample.uid),
                    data_source=sample.data_source,
                )
            )
    if not samples:
        raise ValueError("dataset produced no step samples")
    return samples


def step_prefixes(sample: StepSample) -> list[list[int]]:
    """Return the prefix that ends at each step boundary."""

    prefixes: list[list[int]] = []
    prefix = list(sample.prompt_ids)
    for step in sample.steps:
        prefix = prefix + list(step)
        prefixes.append(prefix.copy())
    return prefixes


def collate_step_samples(samples: list[StepSample], pad_token_id: int = 0) -> StepBatch:
    """Flatten trajectories into padded prefix rows plus group metadata."""

    if not samples:
        raise ValueError("cannot collate an empty StepBatch")
    sequences: list[list[int]] = []
    labels: list[int] = []
    step_index: list[int] = []
    group_index: list[int] = []
    for group, sample in enumerate(samples):
        for index, prefix in enumerate(step_prefixes(sample)):
            sequences.append(prefix)
            labels.append(int(sample.labels[index]))
            step_index.append(index)
            group_index.append(group)
    width = max(len(sequence) for sequence in sequences)
    input_ids = np.full((len(sequences), width), pad_token_id, dtype=np.int64)
    attention_mask = np.zeros((len(sequences), width), dtype=np.float32)
    for row, sequence in enumerate(sequences):
        input_ids[row, : len(sequence)] = sequence
        attention_mask[row, : len(sequence)] = 1.0
    return StepBatch(
        input_ids=input_ids,
        attention_mask=attention_mask,
        labels=np.asarray(labels, dtype=np.float32),
        step_index=np.asarray(step_index, dtype=np.int64),
        group_index=np.asarray(group_index, dtype=np.int64),
        num_groups=len(samples),
    )


def make_step_batches(
    samples: list[StepSample],
    batch_size: int,
    pad_token_id: int = 0,
    shuffle: bool = True,
    seed: int = 0,
    drop_last: bool = True,
) -> list[StepBatch]:
    """Batch whole trajectories, never individual prefixes.

    Keeping a trajectory in one batch makes trajectory-level aggregation a
    simple group-by and avoids a partial trajectory being scored with a
    different aggregation context.
    """

    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    order = list(range(len(samples)))
    if shuffle:
        np.random.default_rng(seed).shuffle(order)
    batches: list[StepBatch] = []
    for start in range(0, len(order) - batch_size + 1, batch_size):
        batches.append(
            collate_step_samples(
                [samples[index] for index in order[start : start + batch_size]], pad_token_id
            )
        )
    if not drop_last:
        remainder = order[(len(order) // batch_size) * batch_size :]
        if remainder:
            batches.append(
                collate_step_samples([samples[index] for index in remainder], pad_token_id)
            )
    if not batches:
        raise ValueError(f"{len(samples)} step samples are fewer than one batch of {batch_size}")
    return batches


# ----------------------------------------------------------------------
# loss, aggregation and metrics
# ----------------------------------------------------------------------


def step_bce_loss(
    scores: Tensor, labels: np.ndarray, mask: np.ndarray | None = None
) -> Tensor:
    """Binary cross-entropy on prefix scores, computed stably.

    ``BCEWithLogits(z, y) = softplus(z) - y * z``.  This form never evaluates
    ``log(sigmoid(z))`` or ``log(1 - sigmoid(z))``, so a confident wrong prefix
    (``z = -500``) produces a finite loss and a finite gradient.
    """

    if scores.ndim != 1:
        raise ValueError(f"scores must be 1-D, got shape {scores.shape}")
    labels_array = np.asarray(labels, dtype=np.float32).reshape(-1)
    if labels_array.shape != scores.shape:
        raise ValueError(
            f"labels shape {labels_array.shape} does not match scores {scores.shape}"
        )
    per_example = softplus(scores) - Tensor(labels_array) * scores
    if mask is None:
        return per_example.mean()
    mask_array = np.asarray(mask, dtype=np.float32).reshape(-1)
    if mask_array.shape != scores.shape:
        raise ValueError(f"mask shape {mask_array.shape} does not match scores {scores.shape}")
    per_example = per_example * Tensor(mask_array)
    return per_example.sum() / max(float(mask_array.sum()), 1.0)


def aggregate_step_probs(
    probabilities: Sequence[float] | np.ndarray, method: str = "min"
) -> float:
    """Turn per-step probabilities into one trajectory score.

    ``product`` is computed in log space; a long trajectory would otherwise
    underflow to zero even when every step is confident.
    """

    if method not in AGGREGATIONS:
        raise ValueError(f"unknown aggregation {method!r}; expected one of {AGGREGATIONS}")
    values = np.asarray(probabilities, dtype=np.float64).reshape(-1)
    if values.size == 0:
        return 0.0
    if not np.all(np.isfinite(values)):
        raise ValueError("step probabilities must be finite")
    values = np.clip(values, 0.0, 1.0)
    if method == "last":
        return float(values[-1])
    if method == "first":
        return float(values[0])
    if method == "min":
        return float(values.min())
    if method == "max":
        return float(values.max())
    if method == "mean":
        return float(values.mean())
    logs = np.log(np.clip(values, 1e-12, 1.0))
    return float(np.exp(logs.sum()))


def _sigmoid_numpy(values: np.ndarray) -> np.ndarray:
    z = np.asarray(values, dtype=np.float64)
    output = np.empty_like(z)
    positive = z >= 0
    output[positive] = 1.0 / (1.0 + np.exp(-z[positive]))
    exp_z = np.exp(z[~positive])
    output[~positive] = exp_z / (1.0 + exp_z)
    return output


def step_metrics(
    probabilities: np.ndarray,
    labels: np.ndarray,
    group_index: np.ndarray,
    method: str = "min",
    threshold: float = 0.5,
) -> dict[str, float]:
    """Report both step-level and trajectory-level quality.

    Step accuracy alone can look good while every trajectory contains one bad
    step.  Trajectory accuracy uses the configured aggregation, which is the
    quantity the PPO reward actually consumes.
    """

    probs = np.asarray(probabilities, dtype=np.float64).reshape(-1)
    target = np.asarray(labels, dtype=np.float64).reshape(-1)
    groups = np.asarray(group_index, dtype=np.int64).reshape(-1)
    if not (probs.shape == target.shape == groups.shape):
        raise ValueError("probabilities, labels and group_index must have the same shape")
    if probs.size == 0:
        return {
            "step/accuracy": 0.0,
            "step/brier": 0.0,
            "traj/accuracy": 0.0,
            "traj/mean_score": 0.0,
            "steps": 0.0,
        }
    predictions = (probs >= threshold).astype(np.float64)
    trajectory_scores: list[float] = []
    trajectory_targets: list[float] = []
    for group in np.unique(groups):
        selected = groups == group
        trajectory_scores.append(aggregate_step_probs(probs[selected], method))
        trajectory_targets.append(float(target[selected].min()))
    trajectory_scores_array = np.asarray(trajectory_scores, dtype=np.float64)
    trajectory_targets_array = np.asarray(trajectory_targets, dtype=np.float64)
    return {
        "step/accuracy": float(np.mean(predictions == target)),
        "step/brier": float(np.mean((probs - target) ** 2)),
        "traj/accuracy": float(
            np.mean((trajectory_scores_array >= threshold) == trajectory_targets_array)
        ),
        "traj/mean_score": float(trajectory_scores_array.mean()),
        "steps": float(probs.size),
    }


# ----------------------------------------------------------------------
# trainer
# ----------------------------------------------------------------------


class PRMTrainer:
    """Train a prefix scorer from step-labelled trajectories."""

    def __init__(self, config: PPOConfig):
        self.config = config
        options = dict(config.reward.prm or {})
        self.options = options
        self.tokenizer = CharTokenizer()
        self.arch = str(options.get("arch", "mlp"))
        self.dim = int(options.get("hidden_size", config.actor_rollout_ref.model.hidden_size))
        self.n_layer = int(options.get("num_layers", 1))
        self.n_head = int(options.get("num_heads", 2))
        self.pooling = options.get("pooling")
        self.step_tokens = int(options.get("step_tokens", 1))
        self.label_mode = str(options.get("label_mode", "local"))
        self.corruptions = int(options.get("corruptions_per_sample", 1))
        self.aggregation = str(options.get("aggregation", "min"))
        if self.aggregation not in AGGREGATIONS:
            raise ValueError(f"unknown aggregation {self.aggregation!r}")
        self.batch_size = int(options.get("train_batch_size", config.data.train_batch_size))
        self.total_steps = int(options.get("total_steps", 160))
        self.eval_freq = max(1, int(options.get("eval_freq", 20)))
        self.target_accuracy = float(options.get("target_accuracy", 0.98))
        self.patience = int(options.get("patience", 4))
        self.max_seq_len = config.data.max_prompt_length + config.data.max_response_length

        train_dataset = build_dataset(
            config.data.dataset, config.data.train_size, config.data.seed
        )
        val_dataset = build_dataset(
            config.data.dataset, config.data.val_size, config.data.seed + 1
        )
        self.train_samples = build_step_samples(
            train_dataset,
            self.tokenizer,
            config.data.max_prompt_length,
            config.data.max_response_length,
            step_tokens=self.step_tokens,
            corruptions_per_sample=self.corruptions,
            label_mode=self.label_mode,
            seed=config.data.seed,
        )
        self.val_samples = build_step_samples(
            val_dataset,
            self.tokenizer,
            config.data.max_prompt_length,
            config.data.max_response_length,
            step_tokens=self.step_tokens,
            corruptions_per_sample=self.corruptions,
            label_mode=self.label_mode,
            seed=config.data.seed + 1,
        )
        self.train_batches = make_step_batches(
            self.train_samples,
            self.batch_size,
            self.tokenizer.pad_token_id,
            shuffle=config.data.shuffle,
            seed=config.data.seed,
        )
        self.val_batches = make_step_batches(
            self.val_samples,
            min(self.batch_size, len(self.val_samples)) or 1,
            self.tokenizer.pad_token_id,
            shuffle=False,
            seed=0,
            drop_last=False,
        )

        self.model = TinyRewardModel(
            vocab_size=self.tokenizer.vocab_size,
            max_seq_len=self.max_seq_len,
            arch=self.arch,
            dim=self.dim,
            n_layer=self.n_layer,
            n_head=self.n_head,
            pooling=self.pooling,
            seed=config.trainer.seed,
        )
        optim_config = OptimConfig(
            lr=float(options.get("lr", 0.02)),
            weight_decay=float(options.get("weight_decay", 0.0)),
            clip_grad=float(options.get("clip_grad", 1.0)),
            lr_scheduler_type=str(options.get("lr_scheduler_type", "constant")),
            total_training_steps=self.total_steps,
        )
        self.optimizer = AdamW(self.model, optim_config)
        self.history: dict[str, float] = {}
        self.best_state: dict[str, np.ndarray] | None = None
        self.best_score = -1.0

    # ------------------------------------------------------------------
    def train_step(self, batch: StepBatch) -> dict[str, float]:
        self.model.train()
        self.model.zero_grad()
        scores = self.model(batch.input_ids, batch.attention_mask)
        loss = step_bce_loss(scores, batch.labels)
        loss.backward()
        grads = {
            name: parameter.grad
            for name, parameter in self.model.named_parameters()
            if parameter.grad is not None
        }
        grads, grad_norm = clip_gradients(grads, self.optimizer.config.clip_grad)
        self.optimizer.step_named(grads)
        self.model.zero_grad()
        metrics = step_metrics(
            _sigmoid_numpy(scores.data),
            batch.labels,
            batch.group_index,
            self.aggregation,
        )
        metrics["loss"] = float(loss.item())
        metrics["grad_norm"] = float(grad_norm)
        metrics["lr"] = float(self.optimizer.current_lr())
        return metrics

    def evaluate(self) -> dict[str, float]:
        self.model.eval()
        probabilities: list[np.ndarray] = []
        labels: list[np.ndarray] = []
        groups: list[np.ndarray] = []
        offset = 0
        with no_grad():
            for batch in self.val_batches:
                scores = self.model(batch.input_ids, batch.attention_mask)
                probabilities.append(_sigmoid_numpy(scores.data))
                labels.append(batch.labels)
                groups.append(batch.group_index + offset)
                offset += batch.num_groups
        if not probabilities:
            return step_metrics(np.asarray([]), np.asarray([]), np.asarray([]), self.aggregation)
        return step_metrics(
            np.concatenate(probabilities),
            np.concatenate(labels),
            np.concatenate(groups),
            self.aggregation,
        )

    def fit(self) -> dict[str, Any]:
        eval_metrics = self.evaluate()
        self.history.update({f"val/{key}": value for key, value in eval_metrics.items()})
        stagnant = 0
        step = 0
        for step in range(1, self.total_steps + 1):
            batch = self.train_batches[(step - 1) % len(self.train_batches)]
            train_metrics = self.train_step(batch)
            self.history["train/loss"] = train_metrics["loss"]
            self.history["train/step_accuracy"] = train_metrics["step/accuracy"]
            self.history["train/traj_accuracy"] = train_metrics["traj/accuracy"]
            if step % self.eval_freq == 0 or step == self.total_steps:
                eval_metrics = self.evaluate()
                for key, value in eval_metrics.items():
                    self.history[f"val/{key}"] = value
                print(
                    f"[prm {step:04d}] train loss={train_metrics['loss']:.4f} "
                    f"step_acc={train_metrics['step/accuracy']:.3f} | "
                    f"val step_acc={eval_metrics['step/accuracy']:.3f} "
                    f"traj_acc={eval_metrics['traj/accuracy']:.3f}"
                )
                score = eval_metrics["traj/accuracy"]
                if score > self.best_score:
                    self.best_score = score
                    self.best_state = self.model.state_dict()
                    stagnant = 0
                else:
                    stagnant += 1
                if (
                    eval_metrics["step/accuracy"] >= self.target_accuracy
                    and eval_metrics["traj/accuracy"] >= self.target_accuracy
                ):
                    break
                if self.patience > 0 and stagnant >= self.patience:
                    break
        if self.best_state is not None:
            self.model.load_state_dict(self.best_state)
        final = self.evaluate()
        return {
            "steps": step,
            "train_samples": len(self.train_samples),
            "val_samples": len(self.val_samples),
            "step_accuracy": final["step/accuracy"],
            "trajectory_accuracy": final["traj/accuracy"],
            "brier": final["step/brier"],
            "mean_score": final["traj/mean_score"],
            "best_trajectory_accuracy": self.best_score,
            "history": dict(self.history),
        }

    # ------------------------------------------------------------------
    def save(self, path: str | Path) -> Path:
        directory = Path(path)
        directory.mkdir(parents=True, exist_ok=True)
        np.savez(
            directory / "prm_model.npz",
            **{f"prm.{name}": value for name, value in self.model.state_dict().items()},
        )
        payload = {
            "format": "mini_verl.prm",
            "arch": self.arch,
            "pooling": self.model.pooling,
            "dim": self.dim,
            "n_layer": self.n_layer,
            "n_head": self.n_head,
            "vocab_size": self.tokenizer.vocab_size,
            "pad_token_id": self.tokenizer.pad_token_id,
            "max_seq_len": self.max_seq_len,
            "max_prompt_length": self.config.data.max_prompt_length,
            "max_response_length": self.config.data.max_response_length,
            "step_tokens": self.step_tokens,
            "label_mode": self.label_mode,
            "aggregation": self.aggregation,
            "train_dataset": self.config.data.dataset,
            "trajectory_accuracy": self.best_score,
        }
        (directory / "prm_config.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
        self.tokenizer.save(str(directory / "tokenizer.json"))
        return directory


def save_prm(trainer: PRMTrainer, path: str | Path) -> Path:
    return trainer.save(path)


def load_prm(
    path: str | Path, tokenizer: CharTokenizer | None = None
) -> tuple[TinyRewardModel, dict[str, Any]]:
    directory = Path(path)
    config_path = directory / "prm_config.json"
    if not config_path.exists():
        raise FileNotFoundError(f"{config_path} does not exist")
    metadata = json.loads(config_path.read_text(encoding="utf-8"))
    if metadata.get("format") != "mini_verl.prm":
        raise ValueError(
            f"{config_path} is not a MiniVerl PRM checkpoint "
            f"(format={metadata.get('format')!r})"
        )
    weights_path = directory / "prm_model.npz"
    if not weights_path.exists():
        raise FileNotFoundError(f"{weights_path} does not exist")
    if tokenizer is None and (directory / "tokenizer.json").exists():
        tokenizer = CharTokenizer.load(str(directory / "tokenizer.json"))
    vocab_size = int(metadata["vocab_size"])
    if tokenizer is not None and tokenizer.vocab_size != vocab_size:
        raise ValueError(
            f"tokenizer vocab_size={tokenizer.vocab_size} does not match PRM metadata {vocab_size}"
        )
    model = TinyRewardModel(
        vocab_size=vocab_size,
        max_seq_len=int(metadata["max_seq_len"]),
        arch=str(metadata["arch"]),
        dim=int(metadata["dim"]),
        n_layer=int(metadata["n_layer"]),
        n_head=int(metadata["n_head"]),
        pooling=metadata.get("pooling"),
        seed=0,
    )
    arrays = np.load(weights_path)
    state = {key[len("prm.") :]: arrays[key] for key in arrays.files}
    model.load_state_dict(state)
    model.eval()
    return model, metadata


# ----------------------------------------------------------------------
# serving inside the PPO loop
# ----------------------------------------------------------------------


class ProcessRewardManager:
    """Score every step prefix, aggregate, and scatter the result to PPO.

    The manager keeps the same external contract as ``ModelRewardManager``:
    it writes ``rm_scores`` and ``token_level_rewards``, so DAPO filtering,
    GAE/GRPO, validation and metrics do not need a PRM-specific branch.

    Step boundaries are relative to the response mask (0 = first response
    token).  If ``step_boundaries`` is absent from ``non_tensor_batch``, the
    response is split into fixed chunks of ``step_tokens``; the final chunk
    absorbs the remainder.
    """

    def __init__(
        self,
        model_path: str | Path | None = None,
        tokenizer: CharTokenizer | None = None,
        batch_size: int = 64,
        aggregation: str | None = None,
        step_tokens: int | None = None,
        normalize: str = "none",
        clip: tuple[float, float] | None = None,
        overlong_penalty_factor: float = 0.0,
        outcome_reward_fn: str | None = None,
        outcome_weight: float = 0.0,
        prm_weight: float = 1.0,
        model: TinyRewardModel | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        if model is None:
            if model_path is None:
                raise ValueError("ProcessRewardManager needs model_path or model")
            model, metadata = load_prm(model_path, tokenizer)
        self.model = model
        self.metadata = dict(metadata or {})
        self.model_path = str(model_path) if model_path is not None else "<memory>"
        self.batch_size = max(1, int(batch_size))
        # Checkpoint metadata is the default so a config that omits
        # ``aggregation``/``step_tokens`` cannot silently serve a different
        # segmentation than the model was trained with.  Explicit arguments
        # (config overrides) still win.
        if aggregation is None:
            aggregation = str(self.metadata.get("aggregation", "min"))
        if step_tokens is None:
            step_tokens = self.metadata.get("step_tokens", 1)
        if aggregation not in AGGREGATIONS:
            raise ValueError(f"unknown aggregation {aggregation!r}")
        self.aggregation = aggregation
        step_tokens = int(step_tokens)
        if step_tokens <= 0:
            raise ValueError("step_tokens must be positive")
        self.step_tokens = step_tokens
        if normalize not in {"none", "zscore", "minmax"}:
            raise ValueError(f"unknown reward normalization {normalize!r}")
        self.normalize = normalize
        self.clip = clip
        self.overlong_penalty_factor = float(overlong_penalty_factor)
        self.outcome_reward_fn = outcome_reward_fn
        self.outcome_weight = float(outcome_weight)
        self.prm_weight = float(prm_weight)
        if self.outcome_weight and not self.outcome_reward_fn:
            raise ValueError("outcome_weight > 0 requires outcome_reward_fn")
        if self.outcome_reward_fn and self.outcome_weight <= 0:
            raise ValueError(
                "outcome_reward_fn requires outcome_weight > 0; otherwise the "
                "verifier is silently ignored and the manager is PRM-only"
            )
        if self.outcome_reward_fn not in {None, "exact_match", "bandit", "math"}:
            raise ValueError(
                f"unknown outcome_reward_fn {self.outcome_reward_fn!r}; "
                "expected exact_match, bandit or math"
            )

    def _resolve_outcome_reward(self):
        from .rewards import bandit_reward, exact_match_reward, math_reward

        return {
            "exact_match": exact_match_reward,
            "bandit": bandit_reward,
            "math": math_reward,
        }[str(self.outcome_reward_fn)]

    # ------------------------------------------------------------------
    @classmethod
    def from_model(
        cls, model: TinyRewardModel, metadata: dict[str, Any] | None = None, **kwargs: Any
    ) -> "ProcessRewardManager":
        return cls(model=model, metadata=metadata, **kwargs)

    def _normalize_scores(self, scores: np.ndarray) -> np.ndarray:
        if self.normalize == "zscore":
            std = float(scores.std())
            if std > 1e-8:
                scores = (scores - float(scores.mean())) / std
        elif self.normalize == "minmax" and scores.size:
            low, high = float(scores.min()), float(scores.max())
            if high - low > 1e-8:
                scores = (scores - low) / (high - low)
        if self.clip is not None:
            scores = np.clip(scores, float(self.clip[0]), float(self.clip[1]))
        return scores.astype(np.float32)

    def _score_prefixes(self, prefixes: list[np.ndarray], pad_token_id: int) -> np.ndarray:
        if not prefixes:
            return np.asarray([], dtype=np.float32)
        width = max(len(prefix) for prefix in prefixes)
        if width > self.model.max_seq_len:
            raise ValueError(
                f"prefix length {width} exceeds PRM max_seq_len {self.model.max_seq_len}"
            )
        input_ids = np.full((len(prefixes), width), pad_token_id, dtype=np.int64)
        attention = np.zeros((len(prefixes), width), dtype=np.float32)
        for row, prefix in enumerate(prefixes):
            input_ids[row, : len(prefix)] = prefix
            attention[row, : len(prefix)] = 1.0
        chunks: list[np.ndarray] = []
        with no_grad():
            for start in range(0, len(prefixes), self.batch_size):
                score = self.model(
                    input_ids[start : start + self.batch_size],
                    attention[start : start + self.batch_size],
                ).data
                chunks.append(_sigmoid_numpy(score).astype(np.float32))
        return np.concatenate(chunks) if chunks else np.asarray([], dtype=np.float32)

    def score_steps(
        self,
        input_ids: np.ndarray,
        attention_mask: np.ndarray | None = None,
        response_mask: np.ndarray | None = None,
        step_boundaries: Sequence[Sequence[int]] | None = None,
    ) -> tuple[np.ndarray, list[np.ndarray], list[np.ndarray]]:
        """Score each step prefix.

        Returns ``(trajectory_scores, step_probabilities, boundaries)``.
        ``boundaries[row]`` contains response-relative end indices, inclusive.
        """

        input_ids = np.asarray(input_ids, dtype=np.int64)
        if input_ids.ndim != 2:
            raise ValueError(f"input_ids must be (batch, seq), got {input_ids.shape}")
        if attention_mask is None:
            attention_mask = np.ones(input_ids.shape, dtype=np.float32)
        attention_mask = np.asarray(attention_mask)
        if attention_mask.shape != input_ids.shape:
            raise ValueError(
                f"attention_mask shape {attention_mask.shape} does not match {input_ids.shape}"
            )
        if response_mask is None:
            response_mask = attention_mask
        response_mask = np.asarray(response_mask, dtype=np.float32)
        if response_mask.shape != input_ids.shape:
            raise ValueError(
                f"response_mask shape {response_mask.shape} does not match {input_ids.shape}"
            )
        if step_boundaries is not None and len(step_boundaries) != input_ids.shape[0]:
            raise ValueError(
                f"step_boundaries has {len(step_boundaries)} rows, expected {input_ids.shape[0]}"
            )

        all_prefixes: list[np.ndarray] = []
        row_prefix_counts: list[int] = []
        resolved_boundaries: list[np.ndarray] = []
        for row in range(input_ids.shape[0]):
            valid = np.flatnonzero(response_mask[row] > 0)
            if valid.size == 0:
                if step_boundaries is not None:
                    raise ValueError(
                        f"row {row}: step_boundaries was provided but response_mask "
                        "has no valid response tokens"
                    )
                row_prefix_counts.append(0)
                resolved_boundaries.append(np.asarray([], dtype=np.int64))
                continue
            if step_boundaries is not None:
                ends = [int(value) for value in step_boundaries[row]]
                if not ends:
                    raise ValueError(f"row {row}: step_boundaries is empty")
                if any(end < 0 for end in ends):
                    raise ValueError(f"row {row}: step boundaries must be non-negative")
                if any(left >= right for left, right in zip(ends, ends[1:])):
                    raise ValueError(f"row {row}: step boundaries must be strictly increasing")
                if ends[-1] != len(valid) - 1:
                    raise ValueError(
                        f"row {row}: last boundary {ends[-1]} must equal the last response "
                        f"position {len(valid) - 1}; the final step cannot be dropped"
                    )
            else:
                ends = [
                    min((index + 1) * self.step_tokens, len(valid)) - 1
                    for index in range((len(valid) + self.step_tokens - 1) // self.step_tokens)
                ]
            resolved_boundaries.append(np.asarray(ends, dtype=np.int64))
            for end in ends:
                all_prefixes.append(input_ids[row, : int(valid[end]) + 1].copy())
            row_prefix_counts.append(len(ends))

        pad_token_id = int(self.metadata.get("pad_token_id", 0))
        probabilities = self._score_prefixes(all_prefixes, pad_token_id)
        trajectory_scores = np.zeros(input_ids.shape[0], dtype=np.float32)
        step_probabilities: list[np.ndarray] = []
        cursor = 0
        for row, count in enumerate(row_prefix_counts):
            row_probs = probabilities[cursor : cursor + count]
            cursor += count
            step_probabilities.append(row_probs.astype(np.float32))
            trajectory_scores[row] = aggregate_step_probs(row_probs, self.aggregation)
        return self._normalize_scores(trajectory_scores), step_probabilities, resolved_boundaries

    def run(self, data: DataProto) -> DataProto:
        if "input_ids" not in data.batch:
            raise KeyError("process reward manager needs tensor field 'input_ids'")
        if "response_mask" not in data.batch:
            raise KeyError("process reward manager needs tensor field 'response_mask'")
        response_mask = np.asarray(data.batch["response_mask"], dtype=np.float32)
        attention_mask = data.batch.get("attention_mask")
        boundaries = data.non_tensor_batch.get("step_boundaries")
        scores, step_scores, _ = self.score_steps(
            data.batch["input_ids"], attention_mask, response_mask, boundaries
        )
        raw_scores = scores.copy()
        scores = self.prm_weight * scores
        outcome_scores: np.ndarray | None = None
        if self.outcome_weight:
            if "response_text" not in data.non_tensor_batch:
                raise KeyError(
                    "outcome_reward_fn requires non_tensor_batch['response_text']"
                )
            if "answers" not in data.non_tensor_batch:
                raise KeyError("outcome_reward_fn requires non_tensor_batch['answers']")
            reward_fn = self._resolve_outcome_reward()
            outcome_scores = np.asarray(
                [
                    reward_fn(str(response), str(answer))
                    for response, answer in zip(
                        data.non_tensor_batch["response_text"],
                        data.non_tensor_batch["answers"],
                    )
                ],
                dtype=np.float32,
            )
            if outcome_scores.shape != scores.shape:
                raise ValueError(
                    f"outcome scores shape {outcome_scores.shape} does not match PRM scores {scores.shape}"
                )
            scores = scores + self.outcome_weight * outcome_scores

        response_lengths = response_mask.sum(axis=-1)
        max_length = int(data.meta_info.get("max_response_length", response_mask.shape[-1]))
        truncated = data.batch.get("truncated")
        if self.overlong_penalty_factor and max_length:
            penalty = np.zeros_like(scores)
            for row in range(len(scores)):
                if truncated is not None and bool(truncated[row]):
                    penalty[row] = -self.overlong_penalty_factor * (
                        float(response_lengths[row]) / max_length
                    )
            scores = scores + penalty

        token_rewards = np.zeros_like(response_mask, dtype=np.float32)
        for row in range(len(scores)):
            valid = np.flatnonzero(response_mask[row] > 0)
            if valid.size:
                token_rewards[row, int(valid[-1])] = scores[row]

        data.batch["rm_scores"] = scores.astype(np.float32)
        # ``prm_scores`` is the aggregated trajectory score *before* the
        # verifier term; it is not the per-step probability vector.
        data.batch["prm_scores"] = raw_scores.astype(np.float32)
        data.batch["prm_trajectory_scores"] = raw_scores.astype(np.float32)
        if outcome_scores is not None:
            data.batch["prm_outcome_scores"] = outcome_scores.astype(np.float32)
        data.batch["token_level_rewards"] = token_rewards
        # Force a 1-D object array even when every trajectory has the same
        # number of steps; otherwise ``np.asarray`` silently creates a 2-D
        # object matrix and downstream ``len(row)``/select semantics change.
        ragged = np.empty(len(step_scores), dtype=object)
        ragged[:] = step_scores
        data.non_tensor_batch["prm_step_scores"] = ragged
        data.meta_info["prm/model_path"] = self.model_path
        data.meta_info["prm/aggregation"] = self.aggregation
        data.meta_info["prm/outcome_weight"] = self.outcome_weight
        data.meta_info["prm/prm_weight"] = self.prm_weight
        data.meta_info["prm/score_mean"] = float(np.mean(scores)) if scores.size else 0.0
        data.meta_info["prm/score_std"] = float(np.std(scores)) if scores.size else 0.0
        data.meta_info["prm/mean_steps"] = (
            float(np.mean([len(values) for values in step_scores])) if step_scores else 0.0
        )
        return data

    def without_shaping(self) -> "ProcessRewardManager":
        clone = copy.copy(self)
        clone.overlong_penalty_factor = 0.0
        return clone

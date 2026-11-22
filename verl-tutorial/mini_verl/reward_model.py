"""Scalar reward models: preferences, Bradley-Terry training, calibration, serving.

The real verl does not train reward models; it *serves* them through the reward
loop (Appendix X).  MiniVerl keeps that boundary honest by implementing the
smallest complete RM pipeline that can be verified on CPU:

    preference pairs -> Bradley-Terry loss -> pairwise metrics / ECE
                    -> checkpoint -> ModelRewardManager -> PPO reward

The model is deliberately tiny (linear / MLP / one-layer Transformer).  It is
not a semantic quality judge; it is a *contract and gradient* demonstration.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .autograd import Tensor, log_sigmoid, no_grad
from .config import OptimConfig, PPOConfig
from .data import CharTokenizer, RLHFDataset, build_dataset
from .engine import AdamW, clip_gradients
from .nn import Embedding, LayerNorm, Linear, Module, TinyTransformer
from .protocol import DataProto

__all__ = [
    "PreferencePair",
    "PreferenceBatch",
    "TinyRewardModel",
    "bradley_terry_loss",
    "pairwise_metrics",
    "expected_calibration_error",
    "build_preference_pairs",
    "collate_preference_pairs",
    "make_preference_batches",
    "RMTrainer",
    "save_reward_model",
    "load_reward_model",
    "ModelRewardManager",
]


# ----------------------------------------------------------------------
# data contract
# ----------------------------------------------------------------------


@dataclass
class PreferencePair:
    """One ``(prompt, chosen, rejected)`` triple, already tokenized."""

    prompt_ids: list[int]
    chosen_ids: list[int]
    rejected_ids: list[int]
    uid: int
    data_source: str

    @property
    def chosen_length(self) -> int:
        return len(self.chosen_ids)

    @property
    def rejected_length(self) -> int:
        return len(self.rejected_ids)


@dataclass
class PreferenceBatch:
    """Padded tensors consumed by ``TinyRewardModel``.

    Chosen and rejected are padded independently, which keeps a long rejected
    response from forcing every chosen response to the same width.  Attention
    masks are the only authority on valid positions; padding is never scored.
    """

    chosen_ids: np.ndarray
    chosen_attention: np.ndarray
    rejected_ids: np.ndarray
    rejected_attention: np.ndarray
    chosen_lengths: np.ndarray
    rejected_lengths: np.ndarray
    uid: np.ndarray
    data_source: np.ndarray

    def __len__(self) -> int:
        return int(self.chosen_ids.shape[0])


def _truncate_prompt(ids: list[int], data_source: str, max_prompt_length: int) -> list[int]:
    if data_source == "math":
        return ids[:max_prompt_length]
    return ids[-max_prompt_length:]


def _wrong_response(response: str, data_source: str, rng: np.random.Generator) -> str:
    """Build a deterministic, task-aware negative.

    A random character mutation can accidentally produce the same answer after
    normalization.  For the bandit task we choose a different action; for other
    tasks we mutate the first alphabetic/digit character, which keeps the
    negative close to the chosen response (a useful hard negative).
    """

    if data_source == "bandit":
        candidates = [char for char in "xyzw" if char != response[:1]]
        if candidates:
            return str(rng.choice(candidates)) + response[1:]
    if not response:
        return "0"
    for index, char in enumerate(response):
        if char.isalnum():
            replacement = "0" if char != "0" else "1"
            return response[:index] + replacement + response[index + 1 :]
    return "0" + response[1:]


def build_preference_pairs(
    dataset: RLHFDataset,
    tokenizer: CharTokenizer,
    max_prompt_length: int = 16,
    max_response_length: int = 8,
    seed: int = 0,
) -> list[PreferencePair]:
    """Turn a MiniVerl RL dataset into preference pairs.

    This mirrors the rule-distillation recipe in Appendix X: a verifiable
    answer becomes the chosen response and a controlled perturbation becomes
    the rejected response.  It is intentionally *not* a claim about human
    preference data quality.
    """

    rng = np.random.default_rng(seed)
    pairs: list[PreferencePair] = []
    for sample in dataset:
        prompt_ids = _truncate_prompt(
            tokenizer.encode(sample.prompt), sample.data_source, max_prompt_length
        )
        chosen = tokenizer.encode(sample.answer, add_eos=True)[:max_response_length]
        rejected_text = _wrong_response(sample.answer, sample.data_source, rng)
        rejected = tokenizer.encode(rejected_text, add_eos=True)[:max_response_length]
        if not chosen:
            continue
        if rejected == chosen:
            # Fall back to a one-token mutation so every pair has a direction.
            rejected = list(chosen)
            rejected[0] = (rejected[0] + 1) % tokenizer.vocab_size
        pairs.append(
            PreferencePair(
                prompt_ids=prompt_ids,
                chosen_ids=chosen,
                rejected_ids=rejected,
                uid=int(sample.uid),
                data_source=sample.data_source,
            )
        )
    return pairs


def _pad_sequences(
    sequences: list[list[int]], pad_token_id: int
) -> tuple[np.ndarray, np.ndarray]:
    width = max((len(sequence) for sequence in sequences), default=1)
    ids = np.full((len(sequences), width), pad_token_id, dtype=np.int64)
    mask = np.zeros((len(sequences), width), dtype=np.float32)
    for row, sequence in enumerate(sequences):
        ids[row, : len(sequence)] = sequence
        mask[row, : len(sequence)] = 1.0
    return ids, mask


def collate_preference_pairs(
    pairs: list[PreferencePair], pad_token_id: int = 0
) -> PreferenceBatch:
    if not pairs:
        raise ValueError("cannot collate an empty preference batch")
    chosen_sequences = [pair.prompt_ids + pair.chosen_ids for pair in pairs]
    rejected_sequences = [pair.prompt_ids + pair.rejected_ids for pair in pairs]
    chosen_ids, chosen_attention = _pad_sequences(chosen_sequences, pad_token_id)
    rejected_ids, rejected_attention = _pad_sequences(rejected_sequences, pad_token_id)
    return PreferenceBatch(
        chosen_ids=chosen_ids,
        chosen_attention=chosen_attention,
        rejected_ids=rejected_ids,
        rejected_attention=rejected_attention,
        chosen_lengths=np.asarray([pair.chosen_length for pair in pairs], dtype=np.float32),
        rejected_lengths=np.asarray([pair.rejected_length for pair in pairs], dtype=np.float32),
        uid=np.asarray([pair.uid for pair in pairs], dtype=np.int64),
        data_source=np.asarray([pair.data_source for pair in pairs], dtype=object),
    )


def make_preference_batches(
    pairs: list[PreferencePair],
    batch_size: int,
    pad_token_id: int = 0,
    shuffle: bool = True,
    seed: int = 0,
    drop_last: bool = True,
) -> list[PreferenceBatch]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    order = list(range(len(pairs)))
    if shuffle:
        np.random.default_rng(seed).shuffle(order)
    batches: list[PreferenceBatch] = []
    for start in range(0, len(order) - batch_size + 1, batch_size):
        batches.append(
            collate_preference_pairs(
                [pairs[index] for index in order[start : start + batch_size]], pad_token_id
            )
        )
    if not drop_last:
        remainder = order[(len(order) // batch_size) * batch_size :]
        if remainder:
            batches.append(
                collate_preference_pairs([pairs[index] for index in remainder], pad_token_id)
            )
    if not batches:
        raise ValueError(
            f"{len(pairs)} preference pairs are fewer than one batch of {batch_size}"
        )
    return batches


# ----------------------------------------------------------------------
# model
# ----------------------------------------------------------------------


class TinyRewardModel(Module):
    """A scalar reward model with three interchangeable backbones.

    ``linear`` and ``mlp`` pool token embeddings; ``transformer`` uses the
    causal trunk from :mod:`mini_verl.nn` and pools the last valid token, which
    is the standard production RM shape.  All three expose the same
    ``(input_ids, attention_mask) -> (batch,)`` contract.
    """

    def __init__(
        self,
        vocab_size: int,
        max_seq_len: int,
        arch: str = "mlp",
        dim: int = 32,
        n_layer: int = 1,
        n_head: int = 2,
        pooling: str | None = None,
        seed: int = 0,
    ):
        super().__init__()
        if arch not in {"linear", "mlp", "transformer"}:
            raise ValueError(f"unknown reward-model arch {arch!r}")
        if pooling is None:
            pooling = "last" if arch == "transformer" else "mean"
        if pooling not in {"last", "mean"}:
            raise ValueError(f"unknown pooling {pooling!r}")
        self.arch = arch
        self.pooling = pooling
        self.max_seq_len = max_seq_len
        self.vocab_size = vocab_size
        rng = np.random.default_rng(seed)
        if arch == "transformer":
            self.trunk_model = TinyTransformer(
                vocab_size=vocab_size,
                max_seq_len=max_seq_len,
                dim=dim,
                n_layer=n_layer,
                n_head=n_head,
                seed=seed,
            )
            self.reward_head = Linear(dim, 1, rng=rng)
        else:
            self.tok_emb = Embedding(vocab_size, dim, rng)
            self.ln_f = LayerNorm(dim)
            if arch == "mlp":
                self.fc1 = Linear(dim, max(dim, 2 * dim), rng=rng)
                self.reward_head = Linear(max(dim, 2 * dim), 1, rng=rng)
            else:
                self.reward_head = Linear(dim, 1, rng=rng)

    # ------------------------------------------------------------------
    def _hidden(self, input_ids: np.ndarray) -> Tensor:
        if self.arch == "transformer":
            return self.trunk_model.trunk(input_ids)
        return self.ln_f(self.tok_emb(input_ids))

    def _pool(self, hidden: Tensor, attention_mask: np.ndarray) -> Tensor:
        mask = np.asarray(attention_mask, dtype=np.float32)
        if mask.ndim != 2 or mask.shape != hidden.shape[:2]:
            raise ValueError(
                f"attention_mask shape {mask.shape} does not match hidden {hidden.shape[:2]}"
            )
        if self.pooling == "mean":
            weights = mask / np.maximum(mask.sum(axis=1, keepdims=True), 1.0)
            return (hidden * weights[..., None]).sum(axis=1)
        positions = np.arange(mask.shape[1], dtype=np.int64)[None, :]
        last = np.max(np.where(mask > 0, positions, -1), axis=1)
        one_hot = (positions == last[:, None]).astype(np.float32)
        one_hot[last < 0] = 0.0
        return (hidden * one_hot[..., None]).sum(axis=1)

    def forward(self, input_ids: np.ndarray, attention_mask: np.ndarray | None = None) -> Tensor:
        input_ids = np.asarray(input_ids, dtype=np.int64)
        if input_ids.ndim != 2:
            raise ValueError(f"input_ids must be (batch, seq), got {input_ids.shape}")
        if input_ids.shape[1] > self.max_seq_len:
            raise ValueError(
                f"sequence length {input_ids.shape[1]} exceeds max_seq_len {self.max_seq_len}"
            )
        if attention_mask is None:
            attention_mask = np.ones(input_ids.shape, dtype=np.float32)
        hidden = self._hidden(input_ids)
        pooled = self._pool(hidden, attention_mask)
        if self.arch == "mlp":
            pooled = self.fc1(pooled).gelu()
        return self.reward_head(pooled).reshape(-1)


# ----------------------------------------------------------------------
# losses and metrics
# ----------------------------------------------------------------------


def bradley_terry_loss(
    chosen_scores: Tensor,
    rejected_scores: Tensor,
    margin: float = 0.0,
    label_smoothing: float = 0.0,
) -> Tensor:
    """Negative log-likelihood of pairwise preferences.

    ``margin`` requires the chosen response to win by at least that amount.
    ``label_smoothing`` mixes in the flipped preference, which limits
    over-confidence when annotations are noisy.  Both terms are computed with
    the stable ``log_sigmoid``; using ``log(sigmoid(z))`` underflows for
    strongly violated pairs.
    """

    if chosen_scores.shape != rejected_scores.shape:
        raise ValueError(
            f"score shapes differ: chosen {chosen_scores.shape}, rejected {rejected_scores.shape}"
        )
    if not 0.0 <= float(label_smoothing) < 1.0:
        raise ValueError("label_smoothing must be in [0, 1)")
    z = chosen_scores - rejected_scores - float(margin)
    loss = -log_sigmoid(z).mean()
    if label_smoothing:
        smooth = float(label_smoothing)
        loss = (1.0 - smooth) * loss + smooth * (-log_sigmoid(-z).mean())
    return loss


def _numpy_sigmoid(values: np.ndarray) -> np.ndarray:
    z = np.asarray(values, dtype=np.float64)
    out = np.empty_like(z)
    positive = z >= 0
    out[positive] = 1.0 / (1.0 + np.exp(-z[positive]))
    exp_z = np.exp(z[~positive])
    out[~positive] = exp_z / (1.0 + exp_z)
    return out


def pairwise_metrics(chosen_scores: np.ndarray, rejected_scores: np.ndarray) -> dict[str, float]:
    z = np.asarray(chosen_scores, dtype=np.float64) - np.asarray(
        rejected_scores, dtype=np.float64
    )
    probability = _numpy_sigmoid(z)
    return {
        "accuracy": float(np.mean(z > 0)) if z.size else 0.0,
        "margin": float(np.mean(z)) if z.size else 0.0,
        "prob": float(np.mean(probability)) if z.size else 0.5,
        "loss": float(np.mean(np.logaddexp(0.0, -z))) if z.size else 0.0,
    }


def expected_calibration_error(
    probabilities: np.ndarray, labels: np.ndarray, n_bins: int = 10
) -> float:
    """Expected Calibration Error for binary probabilities.

    ECE is the weighted average gap between predicted confidence and observed
    accuracy over confidence bins.  It answers a different question from
    pairwise accuracy: *can the score be used as a threshold?*
    """

    if n_bins <= 0:
        raise ValueError("n_bins must be positive")
    probabilities = np.clip(np.asarray(probabilities, dtype=np.float64), 0.0, 1.0)
    labels = np.asarray(labels, dtype=np.float64)
    if probabilities.shape != labels.shape:
        raise ValueError("probabilities and labels must have the same shape")
    boundaries = np.linspace(0.0, 1.0, n_bins + 1)
    total = probabilities.size
    if total == 0:
        return 0.0
    ece = 0.0
    for index in range(n_bins):
        lower, upper = boundaries[index], boundaries[index + 1]
        if index == n_bins - 1:
            in_bin = (probabilities >= lower) & (probabilities <= upper)
        else:
            in_bin = (probabilities >= lower) & (probabilities < upper)
        count = int(in_bin.sum())
        if count == 0:
            continue
        confidence = float(probabilities[in_bin].mean())
        accuracy = float(labels[in_bin].mean())
        ece += (count / total) * abs(confidence - accuracy)
    return float(ece)


# ----------------------------------------------------------------------
# trainer
# ----------------------------------------------------------------------


class RMTrainer:
    """Train a scalar RM from preference pairs, with early stopping and export."""

    def __init__(self, config: PPOConfig):
        self.config = config
        options = dict(config.reward.reward_model or {})
        self.options = options
        self.tokenizer = CharTokenizer()
        self.arch = str(options.get("arch", "mlp"))
        self.dim = int(options.get("hidden_size", config.actor_rollout_ref.model.hidden_size))
        self.n_layer = int(options.get("num_layers", 1))
        self.n_head = int(options.get("num_heads", 2))
        self.pooling = options.get("pooling")
        self.margin = float(options.get("margin", 0.0))
        self.label_smoothing = float(options.get("label_smoothing", 0.0))
        self.length_normalize = float(options.get("length_normalize", 0.0))
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
        self.train_pairs = build_preference_pairs(
            train_dataset,
            self.tokenizer,
            config.data.max_prompt_length,
            config.data.max_response_length,
            seed=config.data.seed,
        )
        self.val_pairs = build_preference_pairs(
            val_dataset,
            self.tokenizer,
            config.data.max_prompt_length,
            config.data.max_response_length,
            seed=config.data.seed + 1,
        )
        self.train_batches = make_preference_batches(
            self.train_pairs,
            self.batch_size,
            self.tokenizer.pad_token_id,
            shuffle=config.data.shuffle,
            seed=config.data.seed,
        )
        # Validation keeps every pair; batching with drop_last=False makes the
        # metric independent of the batch size.
        self.val_batches = make_preference_batches(
            self.val_pairs,
            min(self.batch_size, len(self.val_pairs)) or 1,
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
        self.best_accuracy = -1.0

    # ------------------------------------------------------------------
    def _score_batch(self, batch: PreferenceBatch) -> tuple[Tensor, Tensor]:
        chosen = self.model(batch.chosen_ids, batch.chosen_attention)
        rejected = self.model(batch.rejected_ids, batch.rejected_attention)
        if self.length_normalize:
            alpha = self.length_normalize
            chosen = chosen / np.maximum(batch.chosen_lengths, 1.0) ** alpha
            rejected = rejected / np.maximum(batch.rejected_lengths, 1.0) ** alpha
        return chosen, rejected

    def train_step(self, batch: PreferenceBatch) -> dict[str, float]:
        self.model.train()
        self.model.zero_grad()
        chosen, rejected = self._score_batch(batch)
        loss = bradley_terry_loss(
            chosen, rejected, margin=self.margin, label_smoothing=self.label_smoothing
        )
        loss.backward()
        grads = {
            name: parameter.grad
            for name, parameter in self.model.named_parameters()
            if parameter.grad is not None
        }
        grads, grad_norm = clip_gradients(grads, self.optimizer.config.clip_grad)
        self.optimizer.step_named(grads)
        self.model.zero_grad()
        metrics = pairwise_metrics(chosen.data, rejected.data)
        metrics["loss"] = float(loss.item())
        metrics["grad_norm"] = float(grad_norm)
        metrics["lr"] = float(self.optimizer.current_lr())
        return metrics

    def evaluate(self) -> dict[str, float]:
        self.model.eval()
        chosen_all: list[np.ndarray] = []
        rejected_all: list[np.ndarray] = []
        with no_grad():
            for batch in self.val_batches:
                chosen, rejected = self._score_batch(batch)
                chosen_all.append(chosen.data.copy())
                rejected_all.append(rejected.data.copy())
        chosen_scores = np.concatenate(chosen_all) if chosen_all else np.asarray([])
        rejected_scores = np.concatenate(rejected_all) if rejected_all else np.asarray([])
        metrics = pairwise_metrics(chosen_scores, rejected_scores)
        z = chosen_scores - rejected_scores
        probabilities = _numpy_sigmoid(z)
        metrics["ece"] = expected_calibration_error(probabilities, np.ones_like(probabilities))
        metrics["pairs"] = float(len(chosen_scores))
        return metrics

    def fit(self) -> dict[str, Any]:
        eval_metrics = self.evaluate()
        self.history.update({f"val/{key}": value for key, value in eval_metrics.items()})
        stagnant = 0
        step = 0
        for step in range(1, self.total_steps + 1):
            batch = self.train_batches[(step - 1) % len(self.train_batches)]
            train_metrics = self.train_step(batch)
            self.history["train/loss"] = train_metrics["loss"]
            self.history["train/accuracy"] = train_metrics["accuracy"]
            self.history["train/margin"] = train_metrics["margin"]
            if step % self.eval_freq == 0 or step == self.total_steps:
                eval_metrics = self.evaluate()
                for key, value in eval_metrics.items():
                    self.history[f"val/{key}"] = value
                print(
                    f"[rm {step:04d}] train loss={train_metrics['loss']:.4f} "
                    f"acc={train_metrics['accuracy']:.3f} | "
                    f"val acc={eval_metrics['accuracy']:.3f} "
                    f"ece={eval_metrics['ece']:.3f}"
                )
                if eval_metrics["accuracy"] > self.best_accuracy:
                    self.best_accuracy = eval_metrics["accuracy"]
                    self.best_state = self.model.state_dict()
                    stagnant = 0
                else:
                    stagnant += 1
                if eval_metrics["accuracy"] >= self.target_accuracy:
                    break
                if self.patience > 0 and stagnant >= self.patience:
                    break
        if self.best_state is not None:
            self.model.load_state_dict(self.best_state)
        final = self.evaluate()
        result = {
            "steps": step,
            "train_pairs": len(self.train_pairs),
            "val_pairs": len(self.val_pairs),
            "val_accuracy": final["accuracy"],
            "val_ece": final["ece"],
            "val_margin": final["margin"],
            "best_val_accuracy": self.best_accuracy,
            "history": dict(self.history),
        }
        return result

    # ------------------------------------------------------------------
    def save(self, path: str | Path) -> Path:
        directory = Path(path)
        directory.mkdir(parents=True, exist_ok=True)
        np.savez(
            directory / "reward_model.npz",
            **{f"rm.{name}": value for name, value in self.model.state_dict().items()},
        )
        payload = {
            "format": "mini_verl.reward_model",
            "arch": self.arch,
            "pooling": self.model.pooling,
            "dim": self.dim,
            "n_layer": self.n_layer,
            "n_head": self.n_head,
            "vocab_size": self.tokenizer.vocab_size,
            "max_seq_len": self.max_seq_len,
            "max_prompt_length": self.config.data.max_prompt_length,
            "max_response_length": self.config.data.max_response_length,
            "margin": self.margin,
            "label_smoothing": self.label_smoothing,
            "length_normalize": self.length_normalize,
            "train_dataset": self.config.data.dataset,
            "val_accuracy": self.best_accuracy,
        }
        (directory / "rm_config.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
        self.tokenizer.save(str(directory / "tokenizer.json"))
        return directory


def save_reward_model(trainer: RMTrainer, path: str | Path) -> Path:
    return trainer.save(path)


def load_reward_model(
    path: str | Path, tokenizer: CharTokenizer | None = None
) -> tuple[TinyRewardModel, dict[str, Any]]:
    directory = Path(path)
    config_path = directory / "rm_config.json"
    if not config_path.exists():
        raise FileNotFoundError(f"{config_path} does not exist")
    metadata = json.loads(config_path.read_text(encoding="utf-8"))
    weights_path = directory / "reward_model.npz"
    if not weights_path.exists():
        raise FileNotFoundError(f"{weights_path} does not exist")
    if tokenizer is None and (directory / "tokenizer.json").exists():
        tokenizer = CharTokenizer.load(str(directory / "tokenizer.json"))
    vocab_size = int(metadata["vocab_size"])
    if tokenizer is not None and tokenizer.vocab_size != vocab_size:
        raise ValueError(
            f"tokenizer vocab_size={tokenizer.vocab_size} does not match RM metadata {vocab_size}"
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
    state = {key[len("rm.") :]: arrays[key] for key in arrays.files}
    model.load_state_dict(state)
    model.eval()
    return model, metadata


# ----------------------------------------------------------------------
# serving inside the PPO loop
# ----------------------------------------------------------------------


class ModelRewardManager:
    """Score a ``DataProto`` with a trained scalar RM.

    It is the MiniVerl analogue of verl's reward-loop model path: the RM turns
    ``input_ids`` into one scalar per sequence, and the manager scatters that
    scalar to the last valid response token because PPO/GAE consume token-level
    rewards.

    Differences from the real reward loop are explicit: there is no HTTP
    server, no router, no multi-tenant scheduling, and no GPU batching.  The
    request/response and failure semantics are preserved; throughput is not.
    """

    def __init__(
        self,
        model_path: str | Path,
        tokenizer: CharTokenizer | None = None,
        batch_size: int = 64,
        normalize: str = "none",
        clip: tuple[float, float] | None = None,
        overlong_penalty_factor: float = 0.0,
        length_normalize: float | None = None,
    ):
        self.model, self.metadata = load_reward_model(model_path, tokenizer)
        self.model_path = str(model_path)
        self.batch_size = max(1, int(batch_size))
        if normalize not in {"none", "zscore", "minmax"}:
            raise ValueError(f"unknown reward normalization {normalize!r}")
        self.normalize = normalize
        self.clip = clip
        self.overlong_penalty_factor = float(overlong_penalty_factor)
        self.length_normalize = (
            float(self.metadata.get("length_normalize", 0.0))
            if length_normalize is None
            else float(length_normalize)
        )

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

    def score_batch(
        self,
        input_ids: np.ndarray,
        attention_mask: np.ndarray | None = None,
        response_mask: np.ndarray | None = None,
    ) -> np.ndarray:
        input_ids = np.asarray(input_ids, dtype=np.int64)
        if input_ids.ndim != 2:
            raise ValueError(f"input_ids must be (batch, seq), got {input_ids.shape}")
        if attention_mask is None:
            attention_mask = np.ones(input_ids.shape, dtype=np.float32)
        if self.length_normalize:
            if response_mask is not None:
                lengths = np.asarray(response_mask, dtype=np.float32).sum(axis=1)
            else:
                lengths = np.asarray(attention_mask, dtype=np.float32).sum(axis=1)
            lengths = np.maximum(lengths, 1.0)
        else:
            lengths = np.ones(input_ids.shape[0], dtype=np.float32)
        chunks: list[np.ndarray] = []
        with no_grad():
            for start in range(0, input_ids.shape[0], self.batch_size):
                stop = start + self.batch_size
                scores = self.model(input_ids[start:stop], attention_mask[start:stop]).data
                scores = np.asarray(scores, dtype=np.float32)
                if self.length_normalize:
                    scores = scores / (
                        lengths[start:stop] ** float(self.length_normalize)
                    )
                chunks.append(scores)
        merged = np.concatenate(chunks) if chunks else np.asarray([], dtype=np.float32)
        return self._normalize_scores(merged)

    def run(self, data: DataProto) -> DataProto:
        if "input_ids" not in data.batch:
            raise KeyError("model reward manager needs tensor field 'input_ids'")
        response_mask = data.batch.get("response_mask")
        if response_mask is None:
            raise KeyError("model reward manager needs tensor field 'response_mask'")
        attention_mask = data.batch.get("attention_mask")
        scores = self.score_batch(data.batch["input_ids"], attention_mask, response_mask)
        response_lengths = np.asarray(response_mask, dtype=np.float32).sum(axis=-1)
        max_length = int(
            data.meta_info.get("max_response_length", response_mask.shape[-1])
        )
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
        data.batch["token_level_rewards"] = token_rewards
        data.meta_info["rm/model_path"] = self.model_path
        data.meta_info["rm/score_mean"] = float(np.mean(scores)) if scores.size else 0.0
        data.meta_info["rm/score_std"] = float(np.std(scores)) if scores.size else 0.0
        return data

    def without_shaping(self) -> "ModelRewardManager":
        """Same RM, no overlong/format shaping; used by standalone evaluation."""

        clone = copy.copy(self)
        clone.overlong_penalty_factor = 0.0
        return clone

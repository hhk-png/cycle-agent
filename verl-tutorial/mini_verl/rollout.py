"""Rollout engines.

MiniVerl's rollout is a deterministic sampler over the tiny policy.  It keeps
the production-facing concepts that matter for the rest of the tutorial:

* ``n`` samples per prompt and configurable temperature/top-k/top-p;
* a separate inference copy of the weights, with explicit sleep/wake;
* a prefix-cache statistic (prefill tokens saved);
* tensor-parallel weight layout validation;
* an async queue that tags every trajectory with the policy version that
  produced it.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import RolloutConfig
from .data import CharTokenizer
from .nn import TinyLM, build_policy
from .protocol import DataProto

__all__ = [
    "SamplingParams",
    "BaseRollout",
    "NaiveRollout",
    "TPRollout",
    "AsyncRolloutServer",
    "PDDisaggregatedRollout",
    "build_rollout",
]


@dataclass
class SamplingParams:
    temperature: float = 1.0
    top_k: int = -1
    top_p: float = 1.0
    do_sample: bool = True
    max_tokens: int = 16
    ignore_eos: bool = False
    n: int = 1


def _top_k_top_p(logits: np.ndarray, top_k: int, top_p: float) -> np.ndarray:
    logits = logits.astype(np.float64, copy=True)
    if top_k > 0:
        kth = np.sort(logits)[-min(top_k, logits.size)]
        logits[logits < kth] = -np.inf
    if 0.0 < top_p < 1.0:
        order = np.argsort(-logits)
        sorted_logits = logits[order]
        probs = np.exp(sorted_logits - sorted_logits.max())
        probs /= probs.sum()
        cumulative = np.cumsum(probs)
        cutoff = np.searchsorted(cumulative, top_p)
        keep = order[: cutoff + 1]
        masked = np.full_like(logits, -np.inf)
        masked[keep] = logits[keep]
        logits = masked
    return logits


class BaseRollout:
    def generate_sequences(self, prompts: DataProto) -> DataProto:  # pragma: no cover - interface
        raise NotImplementedError

    def update_weights(self, state_dict: dict[str, np.ndarray]) -> dict[str, float]:  # pragma: no cover
        raise NotImplementedError

    def update_weights_partial(self, state_dict: dict[str, np.ndarray]) -> dict[str, float]:  # pragma: no cover
        """Apply only the tensors present in ``state_dict`` (delta sync)."""
        raise NotImplementedError

    def sleep(self) -> None:
        return None

    def wake_up(self) -> None:
        return None


class NaiveRollout(BaseRollout):
    def __init__(
        self,
        model: TinyLM,
        tokenizer: CharTokenizer,
        config: RolloutConfig,
        seed: int = 0,
        model_config=None,
    ):
        self.model = model
        self.tokenizer = tokenizer
        self.config = config
        self.model_config = model_config or config
        self.rng = np.random.default_rng(seed)
        self.awake = True
        self._backup: dict[str, np.ndarray] | None = None
        self.prefix_cache: dict[tuple[int, ...], int] = {}
        self.prefix_hits = 0
        self.prefill_tokens_saved = 0
        self.generated_tokens = 0
        self.version = 0
        self.sync_bytes = 0
        self.sync_time_ms = 0.0

    # ------------------------------------------------------------------
    # generation
    # ------------------------------------------------------------------
    def _sample_one(self, prompt_ids: list[int], params: SamplingParams) -> tuple[list[int], list[float]]:
        if not self.awake or self.model is None:
            raise RuntimeError("rollout engine is asleep; call wake_up() before generating")
        generated: list[int] = []
        log_probs: list[float] = []
        max_tokens = min(params.max_tokens, self.config.response_length)
        for _ in range(max_tokens):
            ids = np.asarray([prompt_ids + generated], dtype=np.int64)
            logits = self.model(ids).data[0, -1, :].astype(np.float64)
            if self.config.allowed_chars:
                allowed = np.full_like(logits, -np.inf)
                for char in self.config.allowed_chars:
                    token_id = self.tokenizer.stoi.get(char)
                    if token_id is not None:
                        allowed[token_id] = 0.0
                allowed[self.tokenizer.eos_token_id] = 0.0
                logits = logits + allowed
            raw_log_probs = logits - np.log(np.exp(logits - logits.max()).sum()) - logits.max()
            if params.do_sample and params.temperature > 0:
                adjusted = logits / params.temperature
                adjusted = _top_k_top_p(adjusted, params.top_k, params.top_p)
                shifted = adjusted - np.max(adjusted)
                probs = np.exp(shifted)
                probs /= probs.sum()
                token = int(self.rng.choice(len(probs), p=probs))
                # ``rollout_log_probs`` must be the log-prob under the
                # behavior distribution that actually drew the token
                # (temperature/top-k/top-p included), not the raw model
                # distribution.  Async IS and bypass mode both rely on it.
                behavior_log_probs = shifted - np.log(np.exp(shifted).sum())
                token_log_prob = float(behavior_log_probs[token])
            else:
                token = int(np.argmax(logits))
                token_log_prob = float(raw_log_probs[token])
            generated.append(token)
            log_probs.append(token_log_prob)
            if token == self.tokenizer.eos_token_id and not params.ignore_eos:
                break
        return generated, log_probs

    def generate_sequences(self, prompts: DataProto, sampling: SamplingParams | None = None) -> DataProto:
        started = time.perf_counter()
        params = sampling or SamplingParams(
            temperature=self.config.temperature,
            top_k=self.config.top_k,
            top_p=self.config.top_p,
            do_sample=self.config.do_sample,
            max_tokens=self.config.response_length,
            ignore_eos=self.config.ignore_eos,
        )
        input_ids = prompts.batch["input_ids"]
        prompt_lengths = prompts.batch["prompt_length"]
        batch_size = len(prompts)
        total_length = input_ids.shape[1]
        out_ids = np.full_like(input_ids, self.tokenizer.pad_token_id, dtype=np.int64)
        attention = np.zeros_like(input_ids, dtype=np.float32)
        response_mask = np.zeros_like(input_ids, dtype=np.float32)
        rollout_log_probs = np.zeros_like(input_ids, dtype=np.float32)
        response_text = np.empty(batch_size, dtype=object)
        response_lengths = np.zeros(batch_size, dtype=np.int64)
        truncated = np.zeros(batch_size, dtype=np.int64)

        for row in range(batch_size):
            prompt_len = int(prompt_lengths[row])
            prompt_ids = [int(token) for token in input_ids[row, :prompt_len]]
            key = tuple(prompt_ids)
            if self.config.enable_prefix_caching:
                if key in self.prefix_cache:
                    self.prefix_hits += 1
                    self.prefill_tokens_saved += prompt_len
                else:
                    self.prefix_cache[key] = prompt_len
            generated, log_probs = self._sample_one(prompt_ids, params)
            sequence = prompt_ids + generated
            length = min(len(sequence), total_length)
            out_ids[row, :length] = sequence[:length]
            attention[row, :length] = 1.0
            response_mask[row, prompt_len:length] = 1.0
            rollout_log_probs[row, prompt_len:length] = log_probs[: max(0, length - prompt_len)]
            response_text[row] = self.tokenizer.decode(generated, skip_special_tokens=True)
            response_lengths[row] = len(generated)
            max_tokens = min(params.max_tokens, self.config.response_length)
            truncated[row] = int(
                max_tokens > 0
                and len(generated) >= max_tokens
                and (not generated or generated[-1] != self.tokenizer.eos_token_id)
            )
            self.generated_tokens += len(generated)

        elapsed = time.perf_counter() - started
        return DataProto(
            batch={
                "input_ids": out_ids,
                "attention_mask": attention,
                "response_mask": response_mask,
                "prompt_length": prompt_lengths.copy(),
                "rollout_log_probs": rollout_log_probs,
                "response_length": response_lengths,
                "truncated": truncated,
            },
            non_tensor_batch=dict(prompts.non_tensor_batch, response_text=response_text),
            meta_info=dict(
                prompts.meta_info,
                rollout_time_s=elapsed,
                rollout_version=self.version,
                prefix_hits=self.prefix_hits,
                prefill_tokens_saved=self.prefill_tokens_saved,
            ),
        )

    # ------------------------------------------------------------------
    # weight lifecycle
    # ------------------------------------------------------------------
    def update_weights(self, state_dict: dict[str, np.ndarray]) -> dict[str, float]:
        started = time.perf_counter()
        if not self.awake or self.model is None:
            self._backup = {key: value.copy() for key, value in state_dict.items()}
        else:
            self.model.load_state_dict(state_dict)
        self.version += 1
        # KV cache is only valid for the weights that produced it.
        self.prefix_cache.clear()
        self.sync_bytes = int(sum(value.nbytes for value in state_dict.values()))
        self.sync_time_ms = (time.perf_counter() - started) * 1000.0
        return {"sync_bytes": float(self.sync_bytes), "sync_time_ms": self.sync_time_ms, "version": float(self.version)}

    def update_weights_partial(self, state_dict: dict[str, np.ndarray]) -> dict[str, float]:
        """Apply a delta without materializing the full parameter set.

        Real delta engines (``delta_checkpoint_engine`` / SGLang delta loader)
        send only changed tensors, or changed elements, and merge them into the
        inference copy in place.  The version still advances once per sync.
        """

        started = time.perf_counter()
        if not self.awake or self.model is None:
            if self._backup is None:
                raise RuntimeError("cannot apply a partial update without a weight snapshot")
            for key, value in state_dict.items():
                self._backup[key] = value.copy()
        else:
            # strict=False is exactly the delta contract: names absent from the
            # payload keep their current value.
            self.model.load_state_dict(state_dict, strict=False)
        self.version += 1
        self.prefix_cache.clear()
        self.sync_bytes = int(sum(value.nbytes for value in state_dict.values()))
        self.sync_time_ms = (time.perf_counter() - started) * 1000.0
        return {"sync_bytes": float(self.sync_bytes), "sync_time_ms": self.sync_time_ms, "version": float(self.version)}

    def sleep(self) -> None:
        if not self.awake:
            return
        self._backup = self.model.state_dict() if self.model is not None else self._backup
        self.model = None
        self.awake = False
        if self.config.free_cache_engine:
            self.prefix_cache.clear()

    def wake_up(self) -> None:
        if self.awake:
            return
        if self._backup is None:
            raise RuntimeError("cannot wake up without a weight snapshot")
        self.model = build_policy(
            self.model_config,
            vocab_size=len(self.tokenizer.itos),
            max_seq_len=max(
                getattr(self.model_config, "max_seq_len", 0),
                self.config.prompt_length + self.config.response_length,
            ),
            seed=0,
        )
        self.model.load_state_dict(self._backup)
        self.awake = True

    def memory_bytes(self) -> int:
        if self.model is None:
            return 0
        return sum(p.size for p in self.model.parameters()) * 4


class TPRollout(NaiveRollout):
    """Inference-side tensor-parallel rollout.

    The tiny model is reconstructed from TP-sharded linear weights at
    ``update_weights`` time.  Generation itself runs on the reconstructed dense
    model, which lets the chapter isolate the weight-layout problem from the
    sampler.
    """

    def __init__(self, *args, tp_size: int = 2, **kwargs):
        super().__init__(*args, **kwargs)
        self.tp_size = tp_size
        self.last_reshard_bytes = 0

    def update_weights(self, state_dict: dict[str, np.ndarray]) -> dict[str, float]:
        # Shard and immediately gather: the byte count is the real 2x cost that
        # a naive reshard pays on every update.
        shards = {}
        merged = {}
        for name, value in state_dict.items():
            if value.ndim == 2 and value.shape[0] % self.tp_size == 0 and name.endswith("weight"):
                per = value.shape[0] // self.tp_size
                local = [value[i * per : (i + 1) * per].copy() for i in range(self.tp_size)]
                shards[name] = local
                merged[name] = np.concatenate(local, axis=0)
            else:
                merged[name] = value.copy()
        self.last_reshard_bytes = int(sum(v.nbytes for v in state_dict.values()) * 2)
        return super().update_weights(merged)


class AsyncRolloutServer:
    """Versioned trajectory queue used by the async chapters.

    The server produces with a frozen snapshot; the trainer can therefore keep
    training on version ``v`` while version ``v+1`` trajectories are produced.
    No threads are needed for correctness: the queue exposes the same
    produce/consume contract as verl's AgentLoop + TransferQueue split.
    """

    def __init__(self, rollout: NaiveRollout):
        self.rollout = rollout
        self.queue: list[DataProto] = []
        self.produced = 0
        self.consumed = 0

    def produce(self, prompts: DataProto, version: int) -> DataProto:
        data = self.rollout.generate_sequences(prompts)
        data.meta_info["rollout_version"] = version
        self.queue.append(data)
        self.produced += len(data)
        return data

    def pop(self) -> DataProto:
        if not self.queue:
            raise IndexError("no produced batches")
        data = self.queue.pop(0)
        self.consumed += len(data)
        return data

    def __len__(self) -> int:
        return len(self.queue)


class PDDisaggregatedRollout(NaiveRollout):
    """Prefill/decode disaggregation with explicit KV-transfer accounting.

    The tiny model still runs in one process.  What is real is the budget:
    prefill tokens, KV bytes crossing the fabric, and the time those bytes take
    at the configured backend bandwidth.  That is the quantity that decides
    whether a P/D split pays off.
    """

    _BANDWIDTH_GBPS = {
        "nixl": 400.0,
        "mooncake": 100.0,
        "naive": 25.0,
    }

    def __init__(
        self,
        *args,
        prefill_replicas: int = 1,
        decode_replicas: int = 1,
        transfer_backend: str = "nixl",
        kv_bytes_per_token: int = 0,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.prefill_replicas = max(1, int(prefill_replicas))
        self.decode_replicas = max(1, int(decode_replicas))
        self.transfer_backend = transfer_backend
        self.kv_bytes_per_token = int(kv_bytes_per_token)
        self.kv_transfer_bytes = 0
        self.prefill_tokens = 0

    def generate_sequences(self, prompts: DataProto, sampling: SamplingParams | None = None) -> DataProto:
        generated = super().generate_sequences(prompts, sampling)
        prompt_tokens = int(np.sum(prompts.batch["prompt_length"]))
        kv_bytes = prompt_tokens * self.kv_bytes_per_token
        bandwidth_gbps = self._BANDWIDTH_GBPS.get(self.transfer_backend, self._BANDWIDTH_GBPS["naive"])
        # Gbps is gigabits/s: convert bytes to bits before dividing.
        transfer_ms = kv_bytes * 8.0 / (bandwidth_gbps * 1e9) * 1000.0
        self.prefill_tokens += prompt_tokens
        self.kv_transfer_bytes += kv_bytes
        generated.meta_info.update(
            {
                "pd/prefill_tokens": float(prompt_tokens),
                "pd/kv_transfer_bytes": float(kv_bytes),
                "pd/transfer_ms": float(transfer_ms),
                "pd/prefill_replicas": float(self.prefill_replicas),
                "pd/decode_replicas": float(self.decode_replicas),
                "pd/transfer_backend": self.transfer_backend,
            }
        )
        return generated


def _pd_kv_bytes_per_token(model_config, bytes_per_element: int = 2) -> int:
    layers = int(getattr(model_config, "num_layers", 2) or 2)
    dim = int(getattr(model_config, "hidden_size", 32) or 32)
    heads = int(getattr(model_config, "num_heads", 2) or 2)
    # MiniVerl has no separate num_kv_heads field; fall back to num_heads.
    kv_heads = int(getattr(model_config, "num_kv_heads", heads) or heads)
    head_dim = max(1, dim // heads)
    # K and V, one element per head dimension.
    return 2 * layers * kv_heads * head_dim * max(1, int(bytes_per_element))


def build_rollout(
    config: RolloutConfig,
    tokenizer: CharTokenizer,
    seed: int = 0,
    model_config=None,
) -> NaiveRollout:
    resolved_model_config = model_config or config
    model = build_policy(
        resolved_model_config,
        vocab_size=len(tokenizer.itos),
        max_seq_len=max(
            getattr(resolved_model_config, "max_seq_len", 0),
            config.prompt_length + config.response_length,
        ),
        seed=seed,
    )
    disaggregation = config.disaggregation or {}
    if disaggregation.get("enabled", False):
        return PDDisaggregatedRollout(
            model,
            tokenizer,
            config,
            seed=seed,
            model_config=model_config,
            prefill_replicas=int(disaggregation.get("prefill_replicas", 1)),
            decode_replicas=int(disaggregation.get("decode_replicas", 1)),
            transfer_backend=str(disaggregation.get("transfer_backend", "nixl")),
            kv_bytes_per_token=int(disaggregation.get("kv_bytes_per_token", 0))
            or _pd_kv_bytes_per_token(
                resolved_model_config,
                int(disaggregation.get("kv_bytes_per_element", 2)),
            ),
        )
    if config.tensor_model_parallel_size > 1:
        return TPRollout(
            model,
            tokenizer,
            config,
            seed=seed,
            tp_size=config.tensor_model_parallel_size,
            model_config=model_config,
        )
    return NaiveRollout(model, tokenizer, config, seed=seed, model_config=model_config)

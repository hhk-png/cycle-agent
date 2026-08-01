"""Configuration objects for mini-vLLM.

These mirror the role of vLLM's config classes
(``ModelConfig`` / ``CacheConfig`` / ``SchedulerConfig`` / ``EngineConfig``)
but are kept deliberately small so the core ideas stay readable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ModelConfig:
    """Hyper-parameters of the tiny GPT-style transformer.

    Matches the fields that real vLLM reads from a HuggingFace
    ``config.json`` (``n_embd``, ``n_layer``, ``n_head``, ``n_positions`` ...).
    """

    vocab_size: int = 128
    n_embd: int = 64          # hidden / embedding size (d_model)
    n_layer: int = 2          # number of transformer blocks
    n_head: int = 4           # number of attention heads
    head_dim: int = 16        # per-head dimension (n_embd // n_head)
    block_size: int = 64      # maximum context length (n_positions)
    dropout: float = 0.0
    tie_word_embeddings: bool = True  # lm_head is the transposed embedding

    @property
    def hidden_dim(self) -> int:
        """Intermediate width of the MLP blocks (4 * d_model in GPT-2)."""
        return 4 * self.n_embd

    def to_json(self) -> dict:
        return dict(
            vocab_size=self.vocab_size,
            n_embd=self.n_embd,
            n_layer=self.n_layer,
            n_head=self.n_head,
            head_dim=self.head_dim,
            block_size=self.block_size,
            dropout=self.dropout,
            tie_word_embeddings=self.tie_word_embeddings,
        )

    @classmethod
    def from_json(cls, data: dict) -> "ModelConfig":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class CacheConfig:
    """How the PagedAttention KV cache is laid out.

    ``block_size`` mirrors vLLM's ``--block-size`` (default 16).
    ``num_gpu_blocks`` is the analogue of the number of blocks that vLLM
    derives from ``gpu_memory_utilization``; we just make it explicit.
    """

    block_size: int = 16
    num_gpu_blocks: int = 64
    num_cpu_blocks: int = 8  # reserved for swap; unused in the mini version

    def to_json(self) -> dict:
        return dict(
            block_size=self.block_size,
            num_gpu_blocks=self.num_gpu_blocks,
            num_cpu_blocks=self.num_cpu_blocks,
        )

    @classmethod
    def from_json(cls, data: dict) -> "CacheConfig":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class SchedulerConfig:
    """Tuning knobs for the continuous-batching scheduler."""

    max_num_seqs: int = 8                 # max concurrent sequences (``--max-num-seqs``)
    max_num_batched_tokens: int = 512     # token budget per engine step (``--max-num-batched-tokens``)
    enable_chunked_prefill: bool = True   # allow a prompt to be split over several steps
    enable_preemption: bool = True        # recompute-based preemption when KV cache is full
    preemption_mode: str = "recompute"    # "recompute" | "swap" (only recompute implemented)

    def to_json(self) -> dict:
        return dict(
            max_num_seqs=self.max_num_seqs,
            max_num_batched_tokens=self.max_num_batched_tokens,
            enable_chunked_prefill=self.enable_chunked_prefill,
            enable_preemption=self.enable_preemption,
            preemption_mode=self.preemption_mode,
        )

    @classmethod
    def from_json(cls, data: dict) -> "SchedulerConfig":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class EngineConfig:
    """Top-level engine configuration (analogous to ``vllm.engine.arg_utils.EngineArgs``)."""

    model: ModelConfig = field(default_factory=ModelConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    seed: int = 0
    dtype: str = "float32"
    quantize: Optional[str] = None        # None | "int8"
    enable_prefix_caching: bool = True
    speculative: bool = False             # bigram-draft speculative decoding (toy)
    max_model_len: int = 0                # 0 -> derived from model.block_size

    def to_json(self) -> dict:
        return dict(
            model=self.model.to_json(),
            cache=self.cache.to_json(),
            scheduler=self.scheduler.to_json(),
            seed=self.seed,
            dtype=self.dtype,
            quantize=self.quantize,
            enable_prefix_caching=self.enable_prefix_caching,
            speculative=self.speculative,
            max_model_len=self.max_model_len,
        )

    @classmethod
    def from_json(cls, data: dict) -> "EngineConfig":
        cfg = cls()
        if "model" in data:
            cfg.model = ModelConfig.from_json(data["model"])
        if "cache" in data:
            cfg.cache = CacheConfig.from_json(data["cache"])
        if "scheduler" in data:
            cfg.scheduler = SchedulerConfig.from_json(data["scheduler"])
        for k in ("seed", "dtype", "quantize", "enable_prefix_caching", "speculative", "max_model_len"):
            if k in data:
                setattr(cfg, k, data[k])
        return cfg


@dataclass
class SamplingParams:
    """Generation parameters.

    Mirrors ``vllm.SamplingParams``: temperature / top-k / top-p / max_tokens /
    stop strings / stop token ids / ignore_eos.
    """

    temperature: float = 1.0
    top_k: int = -1            # -1 means no top-k filtering
    top_p: float = 1.0         # 1.0 means no nucleus filtering
    max_tokens: int = 64
    stop: Optional[List[str]] = None
    stop_token_ids: Optional[List[int]] = None
    ignore_eos: bool = False
    seed: Optional[int] = None  # None -> global rng (non deterministic)
    echo: bool = False          # OpenAI-compatible "echo" flag

    def to_json(self) -> dict:
        return dict(
            temperature=self.temperature,
            top_k=self.top_k,
            top_p=self.top_p,
            max_tokens=self.max_tokens,
            stop=self.stop,
            stop_token_ids=self.stop_token_ids,
            ignore_eos=self.ignore_eos,
            seed=self.seed,
            echo=self.echo,
        )

    @classmethod
    def from_json(cls, data: dict) -> "SamplingParams":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

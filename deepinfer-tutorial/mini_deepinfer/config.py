"""Configuration objects.

The names deliberately follow DeepInfer's ``EngineArgs`` / ``ModelConfig`` /
``CacheConfig`` / ``SchedulerConfig`` split so that readers can map the small
implementation onto the production code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError


@dataclass
class ModelConfig:
    """Architecture of the toy model.

    The defaults are convenient values for a new model.  When a checkpoint is
    loaded, its ``config_json`` is authoritative: the shipped ``tiny_lm.npz``
    uses 2 layers, FFN 128 and window 64, not these defaults.  See chapter 21.
    ``n_encoder_layers`` greater than zero turns on the simplified CED path:
    the first N layers form an encoder and every later layer can read a global
    memory projected from the encoder's final hidden state.
    """

    vocab_size: int = 264
    d_model: int = 64
    n_layers: int = 3
    n_heads: int = 4
    n_kv_heads: int = 2
    head_dim: int = 16
    ffn_dim: int = 192
    max_seq_len: int = 512
    rope_theta: float = 10_000.0
    rms_norm_eps: float = 1e-5
    window_size: int = 32
    swa_layers: tuple[int, ...] = (0,)
    n_encoder_layers: int = 0
    use_moe: bool = False
    n_experts: int = 8
    n_shared_experts: int = 1
    top_k_experts: int = 2
    moe_layers: tuple[int, ...] = ()
    use_engram: bool = False
    engram_layer: int = 1
    engram_buckets: int = 4096
    engram_ngram: int = 2
    tie_embeddings: bool = True

    def __post_init__(self) -> None:
        if self.n_heads < 1 or self.head_dim < 1:
            raise ConfigError("n_heads and head_dim must be positive")
        if self.n_heads * self.head_dim != self.d_model:
            raise ConfigError("n_heads * head_dim must equal d_model")
        if self.n_kv_heads < 1 or self.d_model % self.n_kv_heads != 0:
            raise ConfigError("d_model must be divisible by a positive n_kv_heads")
        if self.n_layers < 1:
            raise ConfigError("n_layers must be positive")
        object.__setattr__(self, "swa_layers", tuple(self.swa_layers))
        for layer in self.swa_layers:
            if not 0 <= layer < self.n_layers:
                raise ConfigError(f"swa layer {layer} out of range")
        if not 0 <= self.n_encoder_layers <= self.n_layers:
            raise ConfigError("n_encoder_layers must be in [0, n_layers]")
        if self.use_moe:
            if self.n_experts < 1 or not 1 <= self.top_k_experts <= self.n_experts:
                raise ConfigError("invalid MoE expert counts")
            if not self.moe_layers:
                # Every other layer becomes an MoE layer, as in many hybrid
                # dense/MoE designs.
                object.__setattr__(self, "moe_layers", tuple(range(1, self.n_layers, 2)))
            object.__setattr__(self, "moe_layers", tuple(self.moe_layers))
            for layer in self.moe_layers:
                if not 0 <= layer < self.n_layers:
                    raise ConfigError(f"moe layer {layer} out of range")

    @property
    def n_decoder_layers(self) -> int:
        return self.n_layers - self.n_encoder_layers

    @property
    def kv_dim(self) -> int:
        return self.n_kv_heads * self.head_dim

    @property
    def ffn_dim_total(self) -> int:
        return self.ffn_dim


@dataclass
class ParallelConfig:
    """Logical parallel layout.

    On a CPU-only teaching box these ranks are emulated inside one process.
    The point is to exercise the same partitioning and communication patterns,
    not to provide real distributed execution.
    """

    data_parallel_size: int = 1
    context_parallel_size: int = 1
    expert_parallel_size: int = 1

    def __post_init__(self) -> None:
        for name in ("data_parallel_size", "context_parallel_size", "expert_parallel_size"):
            if getattr(self, name) < 1:
                raise ConfigError(f"{name} must be >= 1")

    @property
    def world_size(self) -> int:
        return self.data_parallel_size * self.context_parallel_size


@dataclass
class EngineConfig:
    """Runtime knobs of the scheduler and cache."""

    block_size: int = 16
    num_blocks: int = 256
    max_num_seqs: int = 16
    max_num_batched_tokens: int = 512
    max_model_len: int = 512
    gpu_memory_utilization: float = 0.9
    enable_prefix_cache: bool = True
    enable_chunked_prefill: bool = True
    stop_preempted_request: bool = False
    prefill_attempt_interval: int = 4
    prefill_any_attempt_interval: int = 16
    kv_store_path: str | None = None
    kv_store_ttl_s: float = 3600.0
    kv_store_capacity: int = 2048
    enable_kv_store: bool = False
    enable_speculative: bool = False
    speculative_tokens: int = 4
    confidence_threshold: float = 0.55
    enable_async_schedule: bool = False
    batch_invariant: bool = False
    seed: int = 0
    log_interval_steps: int = 0
    model_path: str | None = None
    tokenizer_path: str | None = None
    parallel: ParallelConfig = field(default_factory=ParallelConfig)

    def __post_init__(self) -> None:
        if self.block_size < 1:
            raise ConfigError("block_size must be >= 1")
        if self.num_blocks < 1:
            raise ConfigError("num_blocks must be >= 1")
        if self.max_num_seqs < 1:
            raise ConfigError("max_num_seqs must be >= 1")
        if self.max_num_batched_tokens < 1:
            raise ConfigError("max_num_batched_tokens must be >= 1")
        if not self.enable_chunked_prefill and self.max_num_batched_tokens < self.max_model_len:
            raise ConfigError(
                "max_num_batched_tokens < max_model_len requires chunked prefill"
            )
        if self.block_size > self.max_num_batched_tokens:
            raise ConfigError("block_size must not exceed the token budget")
        if self.speculative_tokens < 0:
            raise ConfigError("speculative_tokens must be >= 0")
        if self.speculative_tokens and self.speculative_tokens + 1 > self.block_size:
            raise ConfigError("speculative window must fit in one KV block")
        if self.max_model_len > self.cache_capacity_tokens:
            raise ConfigError(
                "max_model_len exceeds the KV cache capacity; increase num_blocks "
                "or reduce max_model_len"
            )

    @property
    def cache_capacity_tokens(self) -> int:
        return self.block_size * self.num_blocks

    def resolve_model_path(self) -> Path:
        if self.model_path:
            return Path(self.model_path)
        return Path(__file__).resolve().parent / "assets" / "tiny_lm.npz"

    def resolve_tokenizer_path(self) -> Path:
        if self.tokenizer_path:
            return Path(self.tokenizer_path)
        return Path(__file__).resolve().parent / "assets" / "vocab.json"

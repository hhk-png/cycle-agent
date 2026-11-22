"""Configuration objects for mini-megatron.

The real Megatron-LM exposes hundreds of flags.  This project keeps the same
separation of concerns (model / parallelism / training) but makes every option
explicit and serialisable so that experiments are reproducible.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


@dataclass
class ModelConfig:
    # Vocabulary and sequence
    vocab_size: int = 256
    max_seq_len: int = 128
    # Architecture
    hidden_size: int = 128
    num_layers: int = 4
    num_attention_heads: int = 4
    num_query_groups: int = 4
    ffn_hidden_size: int = 256
    # Regularisation / numerics
    layernorm_epsilon: float = 1e-5
    attention_dropout: float = 0.0
    hidden_dropout: float = 0.0
    init_method_std: float = 0.02
    # Linear layer details
    add_bias_linear: bool = False
    add_qkv_bias: bool = False
    gated_linear_unit: bool = True
    activation: str = "swiglu"  # swiglu | gelu
    # Position encoding
    position_embedding: str = "rope"  # rope | learned
    rotary_base: float = 10000.0
    rotary_interleaved: bool = False
    # Tied embeddings are only possible when embedding and output live on the
    # same pipeline stage, i.e. pp == 1.
    tie_word_embeddings: bool = False
    # Mixture of Experts
    use_moe: bool = False
    num_experts: int = 4
    moe_topk: int = 1
    moe_capacity_factor: float = 1.25
    moe_aux_loss_coeff: float = 0.01
    # Memory optimisations
    activation_checkpointing: bool = False
    # Numerical simulation of Transformer Engine FP8 (the matmul still runs in
    # the compute dtype; values are rounded through float8_e4m3fn).
    fp8_simulation: bool = False

    def validate(self, parallel: "ParallelConfig") -> None:
        _require(self.vocab_size > 0, "vocab_size must be positive")
        _require(self.hidden_size > 0, "hidden_size must be positive")
        _require(self.num_layers > 0, "num_layers must be positive")
        _require(self.num_attention_heads > 0, "num_attention_heads must be positive")
        _require(self.num_query_groups > 0, "num_query_groups must be positive")
        _require(
            self.hidden_size % self.num_attention_heads == 0,
            "hidden_size must be divisible by num_attention_heads",
        )
        _require(
            self.num_attention_heads % self.num_query_groups == 0,
            "num_attention_heads must be divisible by num_query_groups",
        )
        if self.position_embedding == "rope":
            _require(
                (self.hidden_size // self.num_attention_heads) % 2 == 0,
                "head_dim must be even when position_embedding='rope'",
            )
        _require(
            self.num_query_groups % parallel.tensor_model_parallel_size == 0,
            "num_query_groups must be divisible by tensor parallel size "
            "(the mini implementation does not replicate KV heads)",
        )
        _require(
            self.num_attention_heads % parallel.tensor_model_parallel_size == 0,
            "num_attention_heads must be divisible by tensor parallel size",
        )
        _require(
            self.hidden_size % parallel.tensor_model_parallel_size == 0,
            "hidden_size must be divisible by tensor parallel size",
        )
        _require(
            self.vocab_size % parallel.tensor_model_parallel_size == 0,
            "vocab_size must be divisible by tensor parallel size "
            "(pad the tokenizer vocabulary in production)",
        )
        _require(
            self.ffn_hidden_size % parallel.tensor_model_parallel_size == 0,
            "ffn_hidden_size must be divisible by tensor parallel size",
        )
        _require(
            self.max_seq_len % parallel.tensor_model_parallel_size == 0
            if parallel.sequence_parallel
            else True,
            "max_seq_len must be divisible by tensor parallel size when "
            "sequence_parallel is enabled",
        )
        _require(
            self.max_seq_len % parallel.context_parallel_size == 0,
            "max_seq_len must be divisible by context parallel size",
        )
        _require(
            self.num_layers % parallel.pipeline_model_parallel_size == 0,
            "num_layers must be divisible by pipeline parallel size "
            "(the mini implementation uses even stage partitioning)",
        )
        _require(
            not (parallel.sequence_parallel and parallel.context_parallel_size > 1),
            "sequence_parallel and context_parallel cannot be combined in mini-megatron",
        )
        _require(
            not (parallel.sequence_parallel and self.tie_word_embeddings),
            "tie_word_embeddings + sequence_parallel is not supported in the mini build",
        )
        _require(
            self.position_embedding in {"rope", "learned"},
            "position_embedding must be 'rope' or 'learned'",
        )
        _require(
            self.activation in {"swiglu", "gelu"},
            "activation must be 'swiglu' or 'gelu'",
        )
        if self.use_moe:
            _require(self.num_experts > 0, "num_experts must be positive")
            _require(0 < self.moe_topk <= self.num_experts, "moe_topk out of range")
            _require(
                self.num_experts % parallel.expert_model_parallel_size == 0,
                "num_experts must be divisible by expert_model_parallel_size",
            )
        if parallel.expert_model_parallel_size > 1:
            _require(
                self.use_moe,
                "expert_model_parallel_size > 1 requires use_moe",
            )
        if self.tie_word_embeddings:
            _require(
                parallel.pipeline_model_parallel_size == 1,
                "tie_word_embeddings requires pipeline_model_parallel_size == 1",
            )


@dataclass
class ParallelConfig:
    tensor_model_parallel_size: int = 1
    pipeline_model_parallel_size: int = 1
    context_parallel_size: int = 1
    # Expert parallelism is a *re-partitioning* of the DP dimension in the
    # mini build: ep_size must be 1 or equal to the derived DP size.
    expert_model_parallel_size: int = 1
    # Number of data-parallel replicas is derived:
    # world_size / (tp * pp * cp).
    sequence_parallel: bool = False
    distributed_optimizer: bool = False
    # "zero1": per-tensor owner + broadcast (the readable first version).
    # "flat": one flat bucket + reduce-scatter/all-gather (the production shape).
    distributed_optimizer_style: str = "zero1"
    pipeline_schedule: str = "1f1b"  # 1f1b | gpipe

    def validate(self, world_size: int) -> None:
        for name in (
            "tensor_model_parallel_size",
            "pipeline_model_parallel_size",
            "context_parallel_size",
            "expert_model_parallel_size",
        ):
            _require(getattr(self, name) > 0, f"{name} must be positive")
        model_parallel = (
            self.tensor_model_parallel_size
            * self.pipeline_model_parallel_size
            * self.context_parallel_size
        )
        _require(
            world_size % model_parallel == 0,
            f"world_size ({world_size}) must be divisible by "
            f"tp*pp*cp ({model_parallel})",
        )
        dp_size = world_size // model_parallel
        _require(
            self.expert_model_parallel_size in (1, dp_size),
            "mini-megatron shards experts across the whole DP dimension: "
            f"expert_model_parallel_size must be 1 or dp_size ({dp_size})",
        )
        _require(
            self.sequence_parallel
            and self.tensor_model_parallel_size > 1
            or not self.sequence_parallel,
            "sequence_parallel only makes sense with tensor_model_parallel_size > 1",
        )
        _require(
            self.pipeline_schedule in {"1f1b", "gpipe"},
            "pipeline_schedule must be '1f1b' or 'gpipe'",
        )
        _require(
            self.distributed_optimizer_style in {"zero1", "flat"},
            "distributed_optimizer_style must be 'zero1' or 'flat'",
        )

    @property
    def data_parallel_size(self) -> int:
        # Filled by the distributed runtime through `world_size`; this property
        # is only meaningful after validate() has been called by the trainer.
        raise AttributeError("data_parallel_size is computed by the runtime")


@dataclass
class TrainConfig:
    micro_batch_size: int = 2
    global_batch_size: int = 8
    seq_len: int = 32
    max_steps: int = 20
    lr: float = 3e-4
    min_lr: float = 3e-5
    weight_decay: float = 0.1
    adam_beta1: float = 0.9
    adam_beta2: float = 0.95
    adam_eps: float = 1e-8
    grad_clip: float = 1.0
    warmup_steps: int = 2
    lr_decay_style: str = "cosine"  # cosine | linear | constant | inverse_sqrt
    log_interval: int = 1
    eval_interval: int = 0
    eval_iters: int = 2
    save_interval: int = 0
    checkpoint_dir: str = "checkpoints"
    seed: int = 1234
    device: str = "cpu"
    dtype: str = "float32"  # float32 | bfloat16 | float16
    use_autocast: bool = False
    use_grad_scaler: bool = False
    data: str = "synthetic"  # synthetic | bin
    data_path: Optional[str] = None
    synthetic_vocab_size: Optional[int] = None
    log_jsonl: Optional[str] = None

    def validate(self, model: ModelConfig, parallel: ParallelConfig, world_size: int) -> None:
        _require(self.micro_batch_size > 0, "micro_batch_size must be positive")
        _require(self.global_batch_size > 0, "global_batch_size must be positive")
        _require(self.seq_len > 0, "seq_len must be positive")
        _require(self.seq_len <= model.max_seq_len, "seq_len exceeds model.max_seq_len")
        if parallel.context_parallel_size > 1:
            _require(
                self.seq_len % parallel.context_parallel_size == 0,
                "seq_len must be divisible by context_parallel_size",
            )
        if parallel.sequence_parallel:
            _require(
                self.seq_len % parallel.tensor_model_parallel_size == 0,
                "seq_len must be divisible by tensor_model_parallel_size "
                "when sequence_parallel is enabled",
            )
        _require(self.max_steps > 0, "max_steps must be positive")
        dp = world_size // (
            parallel.tensor_model_parallel_size
            * parallel.pipeline_model_parallel_size
            * parallel.context_parallel_size
        )
        _require(
            self.global_batch_size
            % (self.micro_batch_size * dp) == 0,
            "global_batch_size must be divisible by micro_batch_size * data_parallel_size",
        )
        _require(
            self.lr_decay_style in {"cosine", "linear", "constant", "inverse_sqrt"},
            "unknown lr_decay_style",
        )
        _require(self.dtype in {"float32", "bfloat16", "float16"}, "unknown dtype")
        _require(self.data in {"synthetic", "bin"}, "unknown data source")
        _require(
            not (self.use_autocast and parallel.pipeline_model_parallel_size > 1),
            "autocast + pipeline parallel is not supported in the mini build "
            "(activation dtype negotiation between stages is omitted)",
        )

    @property
    def gradient_accumulation_steps(self) -> int:
        raise AttributeError("gradient_accumulation_steps is computed by the trainer")


@dataclass
class ExperimentConfig:
    model: ModelConfig = field(default_factory=ModelConfig)
    parallel: ParallelConfig = field(default_factory=ParallelConfig)
    train: TrainConfig = field(default_factory=TrainConfig)

    def validate(self, world_size: int) -> None:
        self.parallel.validate(world_size)
        self.model.validate(self.parallel)
        self.train.validate(self.model, self.parallel, world_size)
        _require(
            not (
                self.parallel.expert_model_parallel_size > 1
                and self.parallel.distributed_optimizer
            ),
            "expert parallelism and the ZeRO-1 distributed optimizer are "
            "not combined in the mini build (expert state is already sharded)",
        )

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExperimentConfig":
        return cls(
            model=ModelConfig(**data.get("model", {})),
            parallel=ParallelConfig(**data.get("parallel", {})),
            train=TrainConfig(**data.get("train", {})),
        )

    @classmethod
    def from_json(cls, path: str | Path) -> "ExperimentConfig":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    def save_json(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")

"""First-order cost models for training and inference.

The goal is not to replace profiling.  It is to answer the questions that come
*before* a run:

* does the model + optimizer state fit?
* how much activation memory does a micro-batch need?
* how many FLOPs and GPU-hours will a token budget cost?
* which parallel dimension dominates communication?

Every formula is deliberately explicit and documented.  Constants that depend
on the implementation (activation bytes per token/layer, overlap efficiency)
are parameters with conservative defaults, not hidden magic.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional

from .config import ExperimentConfig, ModelConfig, ParallelConfig, TrainConfig


# ---------------------------------------------------------------------------
# Hardware presets.  Values are approximate dense peaks; always confirm against
# the datasheet of the exact SKU (SXM/PCIe/NVL) you are using.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HardwareSpec:
    name: str
    peak_flops: float          # dense FLOP/s for the precision being modelled
    memory_bytes: float
    memory_bandwidth: float    # bytes/s
    intra_node_gpus: int = 8

    @property
    def ridge_point(self) -> float:
        return self.peak_flops / self.memory_bandwidth


H100_SXM = HardwareSpec(
    name="H100-SXM (bf16 dense)",
    peak_flops=989e12,
    memory_bytes=80 * 1024**3,
    memory_bandwidth=3.35e12,
)
A100_SXM = HardwareSpec(
    name="A100-SXM (bf16 dense)",
    peak_flops=312e12,
    memory_bytes=80 * 1024**3,
    memory_bandwidth=2.0e12,
)
B200 = HardwareSpec(
    name="B200 (bf16 dense, approximate)",
    peak_flops=2.2e15,
    memory_bytes=180 * 1024**3,
    memory_bandwidth=8.0e12,
)
CPU_REFERENCE = HardwareSpec(
    name="CPU reference (illustrative)",
    peak_flops=1e12,
    memory_bytes=64 * 1024**3,
    memory_bandwidth=50e9,
)

HARDWARE_PRESETS = {
    "h100": H100_SXM,
    "a100": A100_SXM,
    "b200": B200,
    "cpu": CPU_REFERENCE,
}


# ---------------------------------------------------------------------------
# Parameter accounting
# ---------------------------------------------------------------------------


def dense_layer_parameters(model: ModelConfig) -> int:
    """Parameters in one dense Transformer block (attention + MLP + norms)."""
    h = model.hidden_size
    d = h // model.num_attention_heads
    kv_dim = model.num_query_groups * d
    # q: h x h, k/v: h x kv_dim, out: h x h
    attention = h * h + 2 * h * kv_dim + h * h
    if model.activation == "swiglu":
        mlp = 3 * h * model.ffn_hidden_size
    else:
        mlp = 2 * h * model.ffn_hidden_size
    norms = 4 * h  # two LayerNorm weights + two biases
    bias = 0
    if model.add_bias_linear:
        # SwiGLU has three projections (gate/up/down), GELU has two (fc1/fc2).
        bias += (
            2 * model.ffn_hidden_size + h
            if model.activation == "swiglu"
            else model.ffn_hidden_size + h
        )
    if model.add_qkv_bias:
        bias += h + 2 * kv_dim + h
    return attention + mlp + norms + bias


def _expert_parameter_count(model: ModelConfig) -> int:
    """Weights (+ optional bias) of one expert MLP."""
    h = model.hidden_size
    ffn = model.ffn_hidden_size
    if model.activation == "swiglu":
        count = 3 * h * ffn
    else:
        count = 2 * h * ffn
    if model.add_bias_linear:
        # gate/up/down for SwiGLU, fc1/fc2 for GELU.
        count += 2 * ffn + h if model.activation == "swiglu" else ffn + h
    return count


def moe_layer_parameters(model: ModelConfig) -> int:
    """Parameters in one MoE block (expert-replicated view)."""
    h = model.hidden_size
    d = h // model.num_attention_heads
    kv_dim = model.num_query_groups * d
    attention = h * h + 2 * h * kv_dim + h * h
    if model.add_qkv_bias:
        attention += h + 2 * kv_dim + h
    expert = _expert_parameter_count(model)
    router = h * model.num_experts
    norms = 4 * h
    return attention + model.num_experts * expert + router + norms


def parameter_count(model: ModelConfig) -> Dict[str, int]:
    """Total and active parameter counts (active matters for MoE FLOPs)."""
    layers = (
        moe_layer_parameters(model) if model.use_moe else dense_layer_parameters(model)
    )
    embedding = model.vocab_size * model.hidden_size
    position = (
        model.max_seq_len * model.hidden_size
        if model.position_embedding == "learned"
        else 0
    )
    final_norm = 2 * model.hidden_size
    output = 0 if model.tie_word_embeddings else model.vocab_size * model.hidden_size

    total = embedding + position + layers * model.num_layers + final_norm + output

    if model.use_moe:
        # Every layer has attention + router + E experts; a token activates
        # top-k of them, so the active expert fraction is k/E.
        h = model.hidden_size
        d = h // model.num_attention_heads
        kv_dim = model.num_query_groups * d
        attention = h * h + 2 * h * kv_dim + h * h
        if model.add_qkv_bias:
            attention += h + 2 * kv_dim + h
        expert = _expert_parameter_count(model)
        router = h * model.num_experts
        # Norms are active on every token; only the expert MLPs are sparse.
        active_layer = attention + router + 4 * h + (model.moe_topk / model.num_experts) * (
            model.num_experts * expert
        )
        active = (
            embedding
            + position
            + active_layer * model.num_layers
            + final_norm
            + output
        )
    else:
        active = total
    return {"total": int(total), "active": int(active), "per_layer": int(layers)}


# ---------------------------------------------------------------------------
# Memory accounting
# ---------------------------------------------------------------------------


def optimizer_state_bytes(
    parameters: int,
    *,
    weight_bytes: int = 2,
    gradient_bytes: int = 2,
    master_bytes: int = 4,
    adam_m_bytes: int = 4,
    adam_v_bytes: int = 4,
) -> Dict[str, float]:
    """Bytes for mixed-precision AdamW, before any DP/ZeRO sharding."""
    weights = parameters * weight_bytes
    grads = parameters * gradient_bytes
    master = parameters * master_bytes
    m = parameters * adam_m_bytes
    v = parameters * adam_v_bytes
    return {
        "weights": weights,
        "gradients": grads,
        "master_weights": master,
        "adam_m": m,
        "adam_v": v,
        "total": weights + grads + master + m + v,
    }


def state_memory_per_rank(
    model: ModelConfig,
    parallel: ParallelConfig,
    *,
    zero_shard: int = 1,
    weight_bytes: int = 2,
    gradient_bytes: int = 2,
    bytes_per_state: int = 4,
) -> Dict[str, float]:
    """Per-rank state memory after TP/PP/EP and optional ZeRO-1 sharding.

    Expert parameters are sharded over EP; dense parameters are replicated over
    EP (the mini build uses ``ep = dp``).  ZeRO-1 shards the dense optimizer
    state only; expert state is already owner-local.
    """
    n = parameter_count(model)["total"]
    tp = parallel.tensor_model_parallel_size
    pp = parallel.pipeline_model_parallel_size
    ep = parallel.expert_model_parallel_size
    expert_total = 0
    if model.use_moe and ep > 1:
        expert_total = model.num_layers * model.num_experts * _expert_parameter_count(model)
    dense_total = n - expert_total
    dense_local = dense_total / (tp * pp)
    expert_local = expert_total / (tp * pp * max(1, ep))
    local = dense_local + expert_local
    weights = local * weight_bytes
    grads = local * gradient_bytes
    optimizer = (
        dense_local * 3 * bytes_per_state / max(1, zero_shard)
        + expert_local * 3 * bytes_per_state
    )
    return {
        "parameters": weights,
        "gradients": grads,
        "optimizer": optimizer,
        "total": weights + grads + optimizer,
    }


def activation_memory_bytes(
    model: ModelConfig,
    *,
    batch: int,
    seq_len: int,
    tp: int = 1,
    pp: int = 1,
    cp: int = 1,
    sequence_parallel: bool = False,
    recompute: bool = False,
    bytes_per_element: int = 2,
    bytes_per_token_per_layer: Optional[float] = None,
) -> Dict[str, float]:
    """Rough activation memory.

    The default uses the Megatron-paper style estimate
    ``s*b*h*(34 + 5*a*s/h)`` bytes for fp16, scaled by precision and sharded
    dimensions.  This is a *planning* number: real memory depends on kernels,
    fused ops, temporary buffers and the autograd graph.
    """
    h = model.hidden_size
    a = model.num_attention_heads
    layers_per_stage = model.num_layers / pp
    scale = bytes_per_element / 2.0
    if bytes_per_token_per_layer is None:
        per_token_layer = h * (34.0 + 5.0 * a * seq_len / h) * scale
    else:
        per_token_layer = bytes_per_token_per_layer

    seq_shard = seq_len
    if cp > 1:
        seq_shard /= cp
    if sequence_parallel and tp > 1:
        seq_shard /= tp

    tokens = batch * seq_shard
    total = per_token_layer * tokens * layers_per_stage
    if recompute:
        # Only block inputs (roughly one hidden state per layer) survive.
        total = 0.15 * total + 2.0 * tokens * h * layers_per_stage * scale
    return {
        "activation": total,
        "per_layer": per_token_layer * tokens,
        "layers_per_stage": layers_per_stage,
    }


def attention_score_bytes(
    model: ModelConfig,
    *,
    batch: int,
    seq_len: int,
    tp: int = 1,
    cp: int = 1,
    bytes_per_element: int = 2,
) -> float:
    """Materialised score matrix for one layer (mini's educational attention)."""
    heads_local = model.num_attention_heads / tp
    q_len = seq_len / cp
    kv_len = seq_len
    return batch * heads_local * q_len * kv_len * bytes_per_element


def kv_cache_bytes(
    model: ModelConfig,
    *,
    batch: int,
    seq_len: int,
    tp: int = 1,
    bytes_per_element: int = 2,
) -> float:
    """K and V for every layer and KV head, sharded over TP heads."""
    h = model.hidden_size
    d = h // model.num_attention_heads
    kv_heads_local = model.num_query_groups / tp
    return 2 * model.num_layers * kv_heads_local * d * seq_len * batch * bytes_per_element


# ---------------------------------------------------------------------------
# Compute accounting
# ---------------------------------------------------------------------------


def training_flops(model: ModelConfig, tokens: float, *, seq_len: Optional[int] = None) -> Dict[str, float]:
    """Approximate training FLOPs: 6*N_active*D + attention term."""
    counts = parameter_count(model)
    linear = 6.0 * counts["active"] * tokens
    attention = 0.0
    if seq_len:
        # 4 * L * n_heads * d * s per token == 4 * L * h * s per token.
        attention = 4.0 * model.num_layers * model.hidden_size * seq_len * tokens
    return {"linear": linear, "attention": attention, "total": linear + attention}


def mfu(
    *,
    flops: float,
    seconds: float,
    world_size: int,
    hardware: HardwareSpec,
) -> float:
    if seconds <= 0 or world_size <= 0:
        return 0.0
    return flops / (seconds * world_size * hardware.peak_flops)


def gpu_hours(
    *,
    flops: float,
    hardware: HardwareSpec,
    utilization: float,
) -> float:
    """Total accelerator-hours (summed over every GPU).

    ``GPU-hours`` is a *quantity of work*, not wall-clock time: a 1e20-FLOP job
    at 40% MFU on an H100 is ~70,000 GPU-hours whether it runs on 8 or 8,000
    GPUs.  Dividing by the cluster size gives wall-clock hours, which is what
    :func:`cluster_hours` returns.  Conflating the two is a common 1000x error
    in capacity planning.
    """
    effective = hardware.peak_flops * max(utilization, 1e-9)
    return flops / effective / 3600.0


def cluster_hours(
    *,
    flops: float,
    world_size: int,
    hardware: HardwareSpec,
    utilization: float,
) -> float:
    """Wall-clock hours for a cluster of ``world_size`` GPUs."""
    return gpu_hours(
        flops=flops, hardware=hardware, utilization=utilization
    ) / max(1, world_size)


# ---------------------------------------------------------------------------
# Communication accounting
# ---------------------------------------------------------------------------


def communication_bytes(
    model: ModelConfig,
    train: TrainConfig,
    parallel: ParallelConfig,
    *,
    world_size: int,
    bytes_per_element: int = 2,
) -> Dict[str, float]:
    """Per-optimizer-step communication volume, one replica's perspective."""
    n = parameter_count(model)["total"]
    dp = world_size // (
        parallel.tensor_model_parallel_size
        * parallel.pipeline_model_parallel_size
        * parallel.context_parallel_size
    )
    b = train.micro_batch_size
    s = train.seq_len
    h = model.hidden_size
    tp = parallel.tensor_model_parallel_size
    pp = parallel.pipeline_model_parallel_size
    cp = parallel.context_parallel_size

    # Ring all-reduce per-rank send volume.  EP-sharded experts are not
    # replicated over DP, so the DP gradient reduction covers dense params only.
    ep = parallel.expert_model_parallel_size
    expert_total = 0
    if model.use_moe and ep > 1:
        expert_total = model.num_layers * model.num_experts * _expert_parameter_count(model)
    dp_params = n - expert_total
    dp_grad = 2.0 * dp_params * bytes_per_element * (dp - 1) / max(1, dp)
    # TP collectives: attention + each locally owned expert MLP, forward and
    # backward.  Without sequence parallelism the message carries the full
    # hidden state; with SP each rank only holds s/tp tokens.
    s_tp = s / tp if parallel.sequence_parallel else s
    local_experts = model.num_experts if model.use_moe else 1
    if model.use_moe and ep > 1:
        local_experts = model.num_experts // ep
    tp_comm = 0.0
    if tp > 1:
        tp_comm = (
            model.num_layers
            * 2.0
            * (1 + local_experts)
            * b
            * s_tp
            * h
            * bytes_per_element
        )
    # One activation + one gradient crossing each stage boundary per microbatch.
    # Pipeline activations use the locally sharded sequence layout.
    s_pp = s
    if cp > 1:
        s_pp /= cp
    if parallel.sequence_parallel and tp > 1:
        s_pp /= tp
    microbatches = max(1, train.global_batch_size // max(1, b * dp))
    pp_comm = 2.0 * (pp - 1) * b * s_pp * h * bytes_per_element * microbatches
    # Educational CP all-gathers the full K/V for every layer; each rank sends
    # its local sequence shard.
    cp_comm = (
        4.0 * model.num_layers * b * (s / cp) * h * bytes_per_element
        if cp > 1
        else 0.0
    )
    # EP dispatch+combine: each local token travels to ~k*(ep-1)/ep remote
    # experts; the payload includes the hidden state plus a 4-byte weight.
    ep_comm = 0.0
    if model.use_moe and ep > 1:
        remote_pairs = b * (s / cp) * model.moe_topk * (ep - 1) / ep
        ep_comm = (
            2.0
            * model.num_layers
            * microbatches
            * remote_pairs
            * (h * bytes_per_element + 4)
        )
    return {
        "dp_grad_all_reduce": dp_grad,
        "tp_per_step": tp_comm,
        "pp_per_step": pp_comm,
        "cp_per_step": cp_comm,
        "ep_per_step": ep_comm,
        "total": dp_grad + tp_comm + pp_comm + cp_comm + ep_comm,
    }


# ---------------------------------------------------------------------------
# End-to-end estimate
# ---------------------------------------------------------------------------


def estimate(
    config: ExperimentConfig,
    *,
    world_size: int,
    tokens: Optional[float] = None,
    hardware: HardwareSpec = H100_SXM,
    utilization: float = 0.4,
    zero_shard: int = 1,
) -> Dict[str, object]:
    """Return a JSON-serialisable estimate for a config."""
    model, parallel, train = config.model, config.parallel, config.train
    counts = parameter_count(model)
    state = state_memory_per_rank(
        model, parallel, zero_shard=zero_shard
    )
    activation = activation_memory_bytes(
        model,
        batch=train.micro_batch_size,
        seq_len=train.seq_len,
        tp=parallel.tensor_model_parallel_size,
        pp=parallel.pipeline_model_parallel_size,
        cp=parallel.context_parallel_size,
        sequence_parallel=parallel.sequence_parallel,
        recompute=model.activation_checkpointing,
    )
    tokens = tokens if tokens is not None else 0.0
    flops = training_flops(model, tokens, seq_len=train.seq_len)
    dp = world_size // (
        parallel.tensor_model_parallel_size
        * parallel.pipeline_model_parallel_size
        * parallel.context_parallel_size
    )
    # 1F1B keeps at most `pp-1` forwards not yet backward on a stage (the
    # implemented schedule warms up `pp-rank-1`, then alternates B/F); GPipe
    # keeps all `m`.  The activation number above is per micro-batch, so peak
    # memory must be multiplied by the schedule's in-flight count.
    grad_accum = max(
        1, train.global_batch_size // (train.micro_batch_size * max(dp, 1))
    )
    pp = parallel.pipeline_model_parallel_size
    if pp == 1:
        in_flight = 1
    elif parallel.pipeline_schedule == "gpipe":
        in_flight = grad_accum
    else:
        in_flight = max(1, min(grad_accum, pp - 1))
    activation_peak = activation["activation"] * in_flight
    comm = communication_bytes(
        model, train, parallel, world_size=world_size
    )
    hours = gpu_hours(
        flops=flops["total"],
        hardware=hardware,
        utilization=utilization,
    )
    wall_hours = cluster_hours(
        flops=flops["total"],
        world_size=world_size,
        hardware=hardware,
        utilization=utilization,
    )
    return {
        "model": {
            "parameters_total": counts["total"],
            "parameters_active": counts["active"],
            "parameters_per_layer": counts["per_layer"],
        },
        "parallel": {
            "world_size": world_size,
            "tp": parallel.tensor_model_parallel_size,
            "pp": parallel.pipeline_model_parallel_size,
            "cp": parallel.context_parallel_size,
            "ep": parallel.expert_model_parallel_size,
            "dp": dp,
        },
        "memory": {
            "state_per_rank": state,
            "activation": activation,
            "activation_in_flight_micro_batches": in_flight,
            "activation_peak": activation_peak,
            "attention_scores_one_layer": attention_score_bytes(
                model,
                batch=train.micro_batch_size,
                seq_len=train.seq_len,
                tp=parallel.tensor_model_parallel_size,
                cp=parallel.context_parallel_size,
            ),
            "kv_cache": kv_cache_bytes(
                model,
                batch=train.micro_batch_size,
                seq_len=train.seq_len,
                tp=parallel.tensor_model_parallel_size,
            ),
            "estimated_peak_per_rank": state["total"] + activation_peak,
            "hardware_capacity": hardware.memory_bytes,
        },
        "compute": {
            "tokens": tokens,
            "flops": flops,
            "assumed_mfu": utilization,
            "hardware": hardware.name,
            "gpu_hours": hours,
            "gpu_years": hours / (365 * 24),
            "wall_clock_hours": wall_hours,
        },
        "communication": comm,
        "notes": [
            "State memory excludes temporary buffers and allocator fragmentation.",
            "Activation memory is a planning estimate; profile the real kernel path.",
            "MoE FLOPs use active parameters; memory uses total parameters.",
            "Communication is per optimizer step and excludes overlap.",
        ],
    }


def _fmt_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(value) < 1024 or unit == "PB":
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} PB"


def _fmt_count(value: float) -> str:
    for unit in ("", "K", "M", "B", "T"):
        if abs(value) < 1000 or unit == "T":
            return f"{value:.2f}{unit}"
        value /= 1000
    return f"{value:.2f}T"


def format_estimate(result: Dict[str, object]) -> str:
    """Human-readable rendering used by the CLI (ASCII labels for portability)."""
    lines = []
    model = result["model"]  # type: ignore[index]
    parallel = result["parallel"]  # type: ignore[index]
    memory = result["memory"]  # type: ignore[index]
    compute = result["compute"]  # type: ignore[index]
    comm = result["communication"]  # type: ignore[index]
    lines.append(
        f"parameters: {_fmt_count(model['parameters_total'])} total, "
        f"{_fmt_count(model['parameters_active'])} active"
    )
    lines.append(
        f"parallel: world={parallel['world_size']} TP={parallel['tp']} "
        f"PP={parallel['pp']} CP={parallel['cp']} DP={parallel['dp']}"
    )
    lines.append(f"state / rank: {_fmt_bytes(memory['state_per_rank']['total'])}")
    lines.append(
        f"activation / rank: {_fmt_bytes(memory['activation']['activation'])} "
        f"per micro-batch"
    )
    lines.append(
        f"activation peak / rank: {_fmt_bytes(memory['activation_peak'])} "
        f"({memory['activation_in_flight_micro_batches']} in flight)"
    )
    lines.append(
        f"attention scores (one layer): "
        f"{_fmt_bytes(memory['attention_scores_one_layer'])}"
    )
    lines.append(f"KV cache: {_fmt_bytes(memory['kv_cache'])}")
    lines.append(
        f"estimated peak / rank: {_fmt_bytes(memory['estimated_peak_per_rank'])} "
        f"(device {_fmt_bytes(memory['hardware_capacity'])})"
    )
    if compute["tokens"]:
        lines.append(
            f"train {compute['tokens']:.3g} tokens @ MFU={compute['assumed_mfu']:.0%}: "
            f"{compute['gpu_hours']:.1f} GPU-hours "
            f"({compute['gpu_years']:.2f} GPU-years; "
            f"{compute['wall_clock_hours']:.1f} h wall-clock on {parallel['world_size']} GPUs)"
        )
    lines.append(
        "communication / step: "
        f"DP {_fmt_bytes(comm['dp_grad_all_reduce'])}, "
        f"TP {_fmt_bytes(comm['tp_per_step'])}, "
        f"PP {_fmt_bytes(comm['pp_per_step'])}, "
        f"CP {_fmt_bytes(comm['cp_per_step'])}"
    )
    return "\n".join(lines)

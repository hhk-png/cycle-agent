"""Transformer block and pipeline-stage model.

Only the first pipeline stage owns the embedding, and only the last owns the
final norm and vocabulary projection.  Everything in between is a stack of
identical blocks, which is exactly the abstraction Megatron's
``TransformerLayer`` / ``PipelineStage`` pair provides.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.checkpoint import checkpoint

from ..config import ModelConfig, ParallelConfig
from .attention import RotaryEmbedding, SelfAttention
from .cross_entropy import vocab_parallel_cross_entropy
from .layers import (
    ColumnParallelLinear,
    LearnedPositionEmbedding,
    MLP,
    VocabParallelEmbedding,
)
from .expert_parallel import ExpertParallelMoELayer
from .moe import MoELayer
from .state import (
    all_reduce,
    get_context_parallel_rank,
    get_context_parallel_world_size,
    get_pipeline_model_parallel_rank,
    get_tensor_model_parallel_group,
    get_tensor_model_parallel_rank,
    get_tensor_model_parallel_world_size,
    scatter_to_context_parallel_region,
)


@dataclass
class StageOutput:
    hidden_states: torch.Tensor
    loss: Optional[torch.Tensor] = None
    aux_loss: Optional[torch.Tensor] = None
    logits: Optional[torch.Tensor] = None
    past_key_values: Optional[list] = None


class TransformerBlock(nn.Module):
    def __init__(
        self,
        config: ModelConfig,
        parallel: ParallelConfig,
        *,
        layer_index: int = 0,
    ) -> None:
        super().__init__()
        self.config = config
        self.layer_index = layer_index
        self.sequence_parallel = parallel.sequence_parallel
        self.input_layernorm = nn.LayerNorm(
            config.hidden_size, eps=config.layernorm_epsilon
        )
        self.self_attention = SelfAttention(
            config.hidden_size,
            config.num_attention_heads,
            config.num_query_groups,
            attention_dropout=config.attention_dropout,
            add_qkv_bias=config.add_qkv_bias,
            sequence_parallel=parallel.sequence_parallel,
            init_method_std=config.init_method_std,
            fp8_simulation=config.fp8_simulation,
        )
        self.post_attention_layernorm = nn.LayerNorm(
            config.hidden_size, eps=config.layernorm_epsilon
        )
        for norm in (self.input_layernorm, self.post_attention_layernorm):
            for param in norm.parameters():
                param._is_replicated = True
        if config.use_moe:
            common = dict(
                capacity_factor=config.moe_capacity_factor,
                activation=config.activation,
                add_bias=config.add_bias_linear,
                sequence_parallel=parallel.sequence_parallel,
                init_method_std=config.init_method_std,
                fp8_simulation=config.fp8_simulation,
            )
            if parallel.expert_model_parallel_size > 1:
                self.mlp = ExpertParallelMoELayer(
                    config.hidden_size,
                    config.ffn_hidden_size,
                    config.num_experts,
                    config.moe_topk,
                    layer_index=layer_index,
                    **common,
                )
            else:
                self.mlp = MoELayer(
                    config.hidden_size,
                    config.ffn_hidden_size,
                    config.num_experts,
                    config.moe_topk,
                    **common,
                )
        else:
            self.mlp = MLP(
                config.hidden_size,
                config.ffn_hidden_size,
                activation=config.activation,
                add_bias=config.add_bias_linear,
                sequence_parallel=parallel.sequence_parallel,
                init_method_std=config.init_method_std,
                fp8_simulation=config.fp8_simulation,
            )
        self.hidden_dropout = nn.Dropout(config.hidden_dropout)
        self.activation_checkpointing = config.activation_checkpointing
        if (
            parallel.sequence_parallel
            and get_tensor_model_parallel_world_size() > 1
        ):
            # LayerNorm parameters are replicated; each TP rank sees only its
            # sequence shard, so their gradients must be summed across TP.
            for norm in (self.input_layernorm, self.post_attention_layernorm):
                for param in norm.parameters():
                    param.register_hook(
                        lambda grad: all_reduce(
                            grad, group=get_tensor_model_parallel_group()
                        )
                    )

    def _forward_impl(
        self,
        x: torch.Tensor,
        position_ids: torch.Tensor,
        attention_mask: Optional[torch.Tensor],
        past_key_value: Optional[tuple[torch.Tensor, torch.Tensor]],
        use_cache: bool,
    ) -> Tuple[torch.Tensor, torch.Tensor, Optional[tuple[torch.Tensor, torch.Tensor]]]:
        residual = x
        h = self.input_layernorm(x)
        attn = self.self_attention(
            h,
            position_ids=position_ids,
            attention_mask=attention_mask,
            past_key_value=past_key_value,
            use_cache=use_cache,
        )
        if use_cache:
            attn_out, present = attn
        else:
            attn_out, present = attn, None
        h = residual + self.hidden_dropout(attn_out)

        residual = h
        h = self.post_attention_layernorm(h)
        if getattr(self.mlp, "is_moe", False):
            mlp_out, aux_loss = self.mlp(h)
        else:
            mlp_out, aux_loss = self.mlp(h), x.new_zeros(())
        h = residual + self.hidden_dropout(mlp_out)
        return h, aux_loss, present

    def forward(
        self,
        x: torch.Tensor,
        position_ids: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        past_key_value: Optional[tuple[torch.Tensor, torch.Tensor]] = None,
        use_cache: bool = False,
    ):
        if self.activation_checkpointing and self.training:
            return checkpoint(
                self._forward_impl,
                x,
                position_ids,
                attention_mask,
                past_key_value,
                use_cache,
                use_reentrant=False,
            )
        return self._forward_impl(
            x, position_ids, attention_mask, past_key_value, use_cache
        )


class TransformerStage(nn.Module):
    """One pipeline stage of a decoder-only Transformer.

    For a normal (non-interleaved) pipeline the stage owns a contiguous block
    of layers derived from its pipeline rank.  For virtual pipeline parallelism
    the caller passes an explicit ``layer_indices`` list plus ownership flags,
    so one rank can host several non-contiguous chunks.
    """

    def __init__(
        self,
        config: ModelConfig,
        parallel: ParallelConfig,
        *,
        layer_indices: Optional[list[int]] = None,
        pp_rank: Optional[int] = None,
        pp_size: Optional[int] = None,
        owns_embedding: Optional[bool] = None,
        owns_final: Optional[bool] = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.parallel = parallel
        self.pp_rank = (
            get_pipeline_model_parallel_rank() if pp_rank is None else pp_rank
        )
        self.pp_size = (
            parallel.pipeline_model_parallel_size if pp_size is None else pp_size
        )
        self.tp_rank = get_tensor_model_parallel_rank()
        self.tp_size = get_tensor_model_parallel_world_size()
        self.cp_rank = get_context_parallel_rank()
        self.cp_size = get_context_parallel_world_size()
        self.is_first = (
            self.pp_rank == 0 if owns_embedding is None else bool(owns_embedding)
        )
        self.is_last = (
            self.pp_rank == self.pp_size - 1 if owns_final is None else bool(owns_final)
        )

        layers_per_stage = config.num_layers // self.pp_size
        if layer_indices is None:
            start = self.pp_rank * layers_per_stage
            self.layer_indices = list(range(start, start + layers_per_stage))
        else:
            self.layer_indices = list(layer_indices)
            layers_per_stage = len(self.layer_indices)

        self.embedding: Optional[VocabParallelEmbedding] = None
        self.position_embedding: Optional[LearnedPositionEmbedding] = None
        if self.is_first:
            self.embedding = VocabParallelEmbedding(
                config.vocab_size,
                config.hidden_size,
                init_method_std=config.init_method_std,
                sequence_parallel=parallel.sequence_parallel,
            )
            if config.position_embedding == "learned":
                self.position_embedding = LearnedPositionEmbedding(
                    config.max_seq_len,
                    config.hidden_size,
                    init_method_std=config.init_method_std,
                )
                if (
                    parallel.sequence_parallel
                    and get_tensor_model_parallel_world_size() > 1
                ):
                    # Each TP rank adds only its sequence shard of the position
                    # embedding, so its gradient is a partial sum.  The table is
                    # replicated; all-reduce to get the full gradient.
                    for param in self.position_embedding.parameters():
                        param.register_hook(
                            lambda grad: all_reduce(
                                grad, group=get_tensor_model_parallel_group()
                            )
                        )

        self.rotary_emb: Optional[RotaryEmbedding] = None
        if config.position_embedding == "rope":
            self.rotary_emb = RotaryEmbedding(
                config.hidden_size // config.num_attention_heads,
                config.max_seq_len,
                base=config.rotary_base,
                interleaved=config.rotary_interleaved,
            )

        self.layers = nn.ModuleList(
            [
                TransformerBlock(config, parallel, layer_index=global_layer)
                for global_layer in self.layer_indices
            ]
        )
        for layer in self.layers:
            if self.rotary_emb is not None:
                layer.self_attention.set_rotary_embedding(self.rotary_emb)

        self.final_norm: Optional[nn.LayerNorm] = None
        self.output_layer: Optional[ColumnParallelLinear] = None
        if self.is_last:
            self.final_norm = nn.LayerNorm(
                config.hidden_size, eps=config.layernorm_epsilon
            )
            for param in self.final_norm.parameters():
                param._is_replicated = True
            self.output_layer = ColumnParallelLinear(
                config.hidden_size,
                config.vocab_size,
                bias=False,
                gather_output=False,
                sequence_parallel=parallel.sequence_parallel,
                init_method_std=config.init_method_std,
                fp8_simulation=config.fp8_simulation,
            )
            if config.tie_word_embeddings:
                assert self.embedding is not None
                self.output_layer.weight = self.embedding.weight
            if (
                parallel.sequence_parallel
                and get_tensor_model_parallel_world_size() > 1
            ):
                for param in self.final_norm.parameters():
                    param.register_hook(
                        lambda grad: all_reduce(
                            grad, group=get_tensor_model_parallel_group()
                        )
                    )

    def _position_ids(
        self,
        batch_size: int,
        seq_len: int,
        device: torch.device,
        position_offset: int = 0,
    ) -> torch.Tensor:
        # On non-first stages with SP the local activation has seq/tp tokens,
        # while attention gathers the full sequence before applying RoPE.
        if (
            self.parallel.sequence_parallel
            and self.tp_size > 1
            and not self.is_first
        ):
            seq_len = seq_len * self.tp_size
        start = position_offset
        if self.cp_size > 1:
            start += self.cp_rank * seq_len
        positions = torch.arange(start, start + seq_len, device=device)
        return positions.unsqueeze(0).expand(batch_size, -1)

    def forward(
        self,
        *,
        input_ids: Optional[torch.Tensor] = None,
        hidden_states: Optional[torch.Tensor] = None,
        labels: Optional[torch.Tensor] = None,
        return_logits: bool = False,
        attention_mask: Optional[torch.Tensor] = None,
        past_key_values: Optional[list] = None,
        use_cache: bool = False,
        position_offset: int = 0,
    ) -> StageOutput:
        if use_cache and self.cp_size > 1:
            raise NotImplementedError(
                "KV-cache decoding with context parallel is not supported"
            )
        if self.is_first:
            if input_ids is None:
                raise ValueError("first pipeline stage requires input_ids")
            if self.cp_size > 1:
                input_ids = scatter_to_context_parallel_region(input_ids, dim=1)
                if labels is not None:
                    labels = scatter_to_context_parallel_region(labels, dim=1)
            x = self.embedding(input_ids)
            batch_size, local_seq = input_ids.shape
            positions = self._position_ids(
                batch_size, local_seq, input_ids.device, position_offset
            )
            if self.position_embedding is not None:
                pos = self.position_embedding(positions)
                if self.parallel.sequence_parallel:
                    # VocabParallelEmbedding has already scattered the sequence.
                    pos = torch.chunk(pos, self.tp_size, dim=1)[self.tp_rank]
                x = x + pos
        else:
            if hidden_states is None:
                raise ValueError("non-first pipeline stage requires hidden_states")
            x = hidden_states
            batch_size, local_seq, _ = x.shape
            positions = self._position_ids(
                batch_size, local_seq, x.device, position_offset
            )
            if labels is not None and self.cp_size > 1:
                labels = scatter_to_context_parallel_region(labels, dim=1)

        # With sequence parallelism `x` is sequence-sharded but `positions`
        # spans the full sequence: every attention projection first gathers the
        # sequence, so this is the correct index space.
        aux_loss = x.new_zeros(())
        presents: Optional[list] = [] if use_cache else None
        for index, layer in enumerate(self.layers):
            past = past_key_values[index] if past_key_values is not None else None
            x, layer_aux, present = layer(
                x,
                positions,
                attention_mask,
                past_key_value=past,
                use_cache=use_cache,
            )
            aux_loss = aux_loss + layer_aux
            if presents is not None:
                presents.append(present)

        if self.is_last:
            x = self.final_norm(x)
            logits = None
            loss = None
            if labels is not None or return_logits:
                logits = self.output_layer(x)
            if labels is not None:
                loss = vocab_parallel_cross_entropy(logits, labels)
            return StageOutput(
                hidden_states=x,
                loss=loss,
                aux_loss=aux_loss * self.config.moe_aux_loss_coeff,
                logits=logits,
                past_key_values=presents,
            )
        return StageOutput(
            hidden_states=x,
            loss=None,
            aux_loss=aux_loss * self.config.moe_aux_loss_coeff,
            logits=None,
            past_key_values=presents,
        )

    def num_parameters(self, trainable_only: bool = True) -> int:
        return sum(
            p.numel()
            for p in self.parameters()
            if (p.requires_grad or not trainable_only)
        )


class GPTModel(TransformerStage):
    """Alias kept for readers who expect a ``GPTModel`` entry point.

    With PP > 1 each process constructs only its own stage, so the name still
    describes the local module graph.
    """

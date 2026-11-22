"""An executable interleaved (virtual) pipeline for teaching.

``pipeline/interleaved.py`` only plans and validates a VPP layout.  This module
runs one: every rank hosts ``v`` chunks, a global chunk ``c`` lives on rank
``c % pp``, and a micro-batch traverses chunks ``0 .. pp*v-1`` in order.

The schedule implemented here is **interleaved GPipe**: all forwards in chunk
order, then all backwards in reverse chunk order.  It is not memory-optimal
(production uses interleaved 1F1B), but it exercises the two things students
need to see:

* a rank must communicate with the *next chunk owner*, which wraps around the
  pipeline ring (rank ``pp-1`` sends to rank ``0``);
* activation/gradient buffers are keyed by ``(microbatch, chunk)``, not just
  ``microbatch``.

The production scheduler adds warmup/cooldown, 1F1B interleaving, activation
recomputation and communication overlap.  The dependency order is the same.
"""
from __future__ import annotations

import contextlib
from typing import Dict, List, Optional, Sequence, Tuple

import torch
import torch.nn as nn

from ..config import ModelConfig, ParallelConfig, TrainConfig
from ..parallel import comm
from ..parallel.state import (
    get_context_parallel_world_size,
    get_pipeline_model_parallel_group,
    get_pipeline_model_parallel_rank,
    get_pipeline_model_parallel_world_size,
    get_tensor_model_parallel_world_size,
)
from ..parallel.transformer import StageOutput, TransformerStage
from .interleaved import VirtualPipelineLayout

MicroBatch = Tuple[torch.Tensor, torch.Tensor]

_FORWARD_TAG = 5000
_BACKWARD_TAG = 7000


class InterleavedGPipeExecutor(nn.Module):
    """Run a VPP layout with an interleaved GPipe schedule."""

    def __init__(
        self,
        config: ModelConfig,
        parallel: ParallelConfig,
        train: TrainConfig,
        *,
        chunks_per_rank: int = 2,
    ) -> None:
        super().__init__()
        self.config = config
        self.parallel = parallel
        self.train = train
        self.pp_rank = get_pipeline_model_parallel_rank()
        self.pp_size = get_pipeline_model_parallel_world_size()
        self.pp_group = get_pipeline_model_parallel_group()
        if config.tie_word_embeddings and self.pp_size * chunks_per_rank > 1:
            raise NotImplementedError(
                "tied embeddings across virtual chunks are not supported "
                "(embedding and output live on different chunks)"
            )
        self.layout = VirtualPipelineLayout(
            pp_size=self.pp_size,
            chunks_per_rank=chunks_per_rank,
            num_layers=config.num_layers,
        )
        self.num_microbatches = 0
        self._micro_batches: List[MicroBatch] = []
        self._outs: Dict[Tuple[int, int], StageOutput] = {}
        self._hidden: Dict[Tuple[int, int], torch.Tensor] = {}
        self._grads: Dict[Tuple[int, int], torch.Tensor] = {}
        self._send_works: Dict[Tuple[int, int], object] = {}
        self._losses: Dict[int, torch.Tensor] = {}
        self._aux_total: Optional[torch.Tensor] = None

        self.chunks = nn.ModuleDict()
        for global_chunk in self.layout.chunks_of_rank(self.pp_rank):
            owner = self.layout.owner(global_chunk)
            self.chunks[str(global_chunk)] = TransformerStage(
                config,
                parallel,
                layer_indices=list(self.layout.layers_of_chunk(global_chunk)),
                pp_rank=owner,
                pp_size=self.pp_size,
                owns_embedding=global_chunk == 0,
                owns_final=global_chunk == self.layout.num_chunks - 1,
            )

        local_seq = train.seq_len // get_context_parallel_world_size()
        if parallel.sequence_parallel:
            local_seq //= get_tensor_model_parallel_world_size()
        self.local_seq = local_seq
        self.activation_shape = (
            train.micro_batch_size,
            local_seq,
            config.hidden_size,
        )
        self.dtype = next(self.parameters()).dtype

    # ------------------------------------------------------------------
    # Device / helpers
    # ------------------------------------------------------------------
    def _device(self) -> torch.device:
        if self.train.device:
            return torch.device(self.train.device)
        if torch.cuda.is_available():
            return torch.device("cuda", torch.cuda.current_device())
        return torch.device("cpu")

    def _global_rank(self, pp_rank: int) -> int:
        return comm.get_global_rank(self.pp_group, pp_rank)

    def _chunk(self, chunk: int) -> TransformerStage:
        return self.chunks[str(chunk)]  # type: ignore[return-value]

    @contextlib.contextmanager
    def _autocast(self):
        if not self.train.use_autocast:
            yield
            return
        dtype = torch.bfloat16 if self.train.dtype == "bfloat16" else torch.float16
        device_type = "cuda" if self.train.device.startswith("cuda") else "cpu"
        if device_type == "cpu" and dtype == torch.float16:
            dtype = torch.bfloat16
        with torch.autocast(device_type=device_type, dtype=dtype):
            yield

    def _forward_tag(self, mb: int, chunk: int) -> int:
        return _FORWARD_TAG + mb * self.layout.num_chunks + chunk

    def _backward_tag(self, mb: int, chunk: int) -> int:
        return _BACKWARD_TAG + mb * self.layout.num_chunks + chunk

    def _wait_send(self, mb: int, chunk: int) -> None:
        work = self._send_works.pop((mb, chunk), None)
        if work is not None:
            work.wait()

    # ------------------------------------------------------------------
    # Forward
    # ------------------------------------------------------------------
    def _take_hidden(self, mb: int, chunk: int) -> torch.Tensor:
        if self.layout.owner(chunk) == self.pp_rank:
            return self._hidden.pop((mb, chunk))
        buffer = torch.empty(
            self.activation_shape, dtype=self.dtype, device=self._device()
        )
        comm.recv(
            buffer,
            src=self._global_rank(self.layout.owner(chunk)),
            tag=self._forward_tag(mb, chunk),
            group=self.pp_group,
        )
        return buffer

    def _pass_hidden(self, tensor: torch.Tensor, mb: int, chunk: int) -> None:
        next_chunk = chunk + 1
        if next_chunk >= self.layout.num_chunks:
            return
        next_rank = self.layout.owner(next_chunk)
        if next_rank == self.pp_rank:
            # Mirror the remote path: the downstream chunk receives a fresh
            # leaf.  Storing the upstream graph tensor directly would make the
            # downstream input a non-leaf whose ``.grad`` is None, so the
            # upstream gradient handoff would never fire.
            self._hidden[(mb, chunk)] = tensor.detach()
            return
        work = comm.isend(
            tensor.detach().contiguous(),
            dst=self._global_rank(next_rank),
            tag=self._forward_tag(mb, chunk),
            group=self.pp_group,
        )
        self._send_works[(mb, chunk)] = work

    def _forward_chunk(self, mb: int, chunk: int) -> StageOutput:
        ids, labels = self._micro_batches[mb]
        stage = self._chunk(chunk)
        hidden: Optional[torch.Tensor] = None
        if chunk == 0:
            with self._autocast():
                out = stage(
                    input_ids=ids,
                    labels=labels if stage.is_last else None,
                )
        else:
            hidden = self._take_hidden(mb, chunk - 1)
            hidden.requires_grad_(True)
            with self._autocast():
                out = stage(
                    hidden_states=hidden,
                    labels=labels if stage.is_last else None,
                )
        out.pipeline_input = hidden  # type: ignore[attr-defined]
        self._outs[(mb, chunk)] = out
        self._pass_hidden(out.hidden_states, mb, chunk)
        return out

    # ------------------------------------------------------------------
    # Backward
    # ------------------------------------------------------------------
    def _take_grad(self, mb: int, chunk: int) -> torch.Tensor:
        # ``chunk`` is the upstream chunk whose output gradient we need; the
        # sender is chunk+1, which may be local or remote.  The key/tag is the
        # upstream chunk id (matching ``_pass_grad``).
        sender = chunk + 1
        if self.layout.owner(sender) == self.pp_rank:
            return self._grads.pop((mb, chunk))
        buffer = torch.empty(
            self.activation_shape, dtype=self.dtype, device=self._device()
        )
        comm.recv(
            buffer,
            src=self._global_rank(self.layout.owner(sender)),
            tag=self._backward_tag(mb, chunk),
            group=self.pp_group,
        )
        return buffer

    def _pass_grad(self, tensor: torch.Tensor, mb: int, chunk: int) -> None:
        # ``chunk`` is the upstream chunk that should receive this gradient.
        prev_rank = self.layout.owner(chunk)
        if prev_rank == self.pp_rank:
            self._grads[(mb, chunk)] = tensor
            return
        comm.send(
            tensor.detach().contiguous(),
            dst=self._global_rank(prev_rank),
            tag=self._backward_tag(mb, chunk),
            group=self.pp_group,
        )

    def _backward_chunk(self, mb: int, chunk: int) -> None:
        out = self._outs.pop((mb, chunk))
        stage = self._chunk(chunk)
        self._wait_send(mb, chunk)
        if chunk + 1 < self.layout.num_chunks:
            # The downstream chunk sends the gradient for *this* chunk's
            # output keyed by this chunk id (not chunk+1).
            grad_output: Optional[torch.Tensor] = self._take_grad(mb, chunk)
        else:
            grad_output = None

        scale = 1.0 / max(1, self.num_microbatches)
        tensors: List[torch.Tensor] = []
        grads: List[torch.Tensor] = []
        if stage.is_last:
            if out.loss is None:
                raise RuntimeError("last virtual chunk did not produce a loss")
            tensors.append(out.loss * scale)
            grads.append(torch.ones_like(out.loss))
            self._losses[mb] = out.loss.detach()
        else:
            if grad_output is None:
                raise RuntimeError("missing downstream gradient")
            tensors.append(out.hidden_states)
            grads.append(grad_output)
        if out.aux_loss is not None and out.aux_loss.requires_grad:
            tensors.append(out.aux_loss * scale)
            grads.append(torch.ones_like(out.aux_loss))
        if out.aux_loss is not None:
            det = out.aux_loss.detach()
            self._aux_total = (
                det.clone() if self._aux_total is None else self._aux_total + det
            )

        torch.autograd.backward(tensors, grads, retain_graph=False)

        if chunk > 0:
            input_ref = getattr(out, "pipeline_input", None)
            if input_ref is None:
                raise RuntimeError("missing virtual pipeline input")
            if input_ref.grad is not None:
                self._pass_grad(input_ref.grad, mb, chunk - 1)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def forward_backward(
        self, micro_batches: Sequence[MicroBatch]
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        self._micro_batches = list(micro_batches)
        self.num_microbatches = len(self._micro_batches)
        tag_span = self.num_microbatches * self.layout.num_chunks
        if tag_span >= _BACKWARD_TAG - _FORWARD_TAG:
            raise ValueError("too many (microbatch, chunk) pairs for the tag scheme")
        self._outs = {}
        self._hidden = {}
        self._grads = {}
        self._send_works = {}
        self._losses = {}
        self._aux_total = None
        if self.num_microbatches == 0:
            return torch.zeros(()), torch.zeros(())

        # Interleaved GPipe: every forward, then every backward.
        for mb in range(self.num_microbatches):
            for chunk in range(self.layout.num_chunks):
                if self.layout.owner(chunk) == self.pp_rank:
                    self._forward_chunk(mb, chunk)
        for mb in reversed(range(self.num_microbatches)):
            for chunk in reversed(range(self.layout.num_chunks)):
                if self.layout.owner(chunk) == self.pp_rank:
                    self._backward_chunk(mb, chunk)
        # Any send that has no matching backward (e.g. evaluation) must finish
        # before the buffers go out of scope.
        for key in list(self._send_works):
            self._send_works.pop(key).wait()

        loss = (
            sum(self._losses.values()) / self.num_microbatches
            if self._losses
            else torch.zeros(())
        )
        aux = (
            self._aux_total / self.num_microbatches
            if self._aux_total is not None
            else torch.zeros(())
        )
        if self.pp_size > 1:
            loss = comm.all_reduce(loss, group=self.pp_group)
            aux = comm.all_reduce(aux, group=self.pp_group)
        return loss, aux

    def num_parameters(self, trainable_only: bool = True) -> int:
        return sum(
            parameter.numel()
            for parameter in self.parameters()
            if parameter.requires_grad or not trainable_only
        )

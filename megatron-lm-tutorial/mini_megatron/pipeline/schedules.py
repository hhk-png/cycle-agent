"""Pipeline schedules: GPipe and 1F1B.

One activation tensor travels forward between neighbouring stages and one
gradient tensor travels backward.  Tags include the microbatch id so that the
two directions can never be confused.

GPipe runs every forward before any backward: simple, but its activation
memory grows with the number of microbatches.  1F1B starts the backward of the
oldest microbatch as soon as its gradient is available and then alternates
forward/backward, which is the schedule used by Megatron's default non-
interleaved pipeline engine.
"""
from __future__ import annotations

import contextlib
from typing import Dict, List, Optional, Sequence, Tuple

import torch

from ..config import ParallelConfig, TrainConfig
from ..parallel import comm
from ..parallel.state import (
    get_context_parallel_world_size,
    get_pipeline_model_parallel_group,
    get_pipeline_model_parallel_rank,
    get_pipeline_model_parallel_world_size,
    get_tensor_model_parallel_world_size,
)
from ..parallel.transformer import StageOutput, TransformerStage


MicroBatch = Tuple[torch.Tensor, torch.Tensor]


class PipelineCommunicator:
    def __init__(
        self,
        group,
        rank: int,
        world_size: int,
        activation_shape: Tuple[int, int, int],
        dtype: torch.dtype,
        device: Optional[torch.device] = None,
    ) -> None:
        self.group = group
        self.rank = rank
        self.world_size = world_size
        self.activation_shape = activation_shape
        self.dtype = dtype
        self.device = device
        self._send_works: Dict[Tuple[str, int], object] = {}
        self.global_ranks = [
            comm.get_global_rank(group, i) for i in range(world_size)
        ]

    def _global(self, local_rank: int) -> int:
        return self.global_ranks[local_rank]

    @property
    def has_prev(self) -> bool:
        return self.world_size > 1 and self.rank > 0

    @property
    def has_next(self) -> bool:
        return self.world_size > 1 and self.rank < self.world_size - 1

    def send_forward(self, tensor: torch.Tensor, mb: int):
        if not self.has_next:
            return None
        work = comm.isend(
            tensor.detach().contiguous(),
            dst=self._global(self.rank + 1),
            tag=1000 + mb,
            group=self.group,
        )
        self._send_works[("f", mb)] = work
        return work

    def wait_forward(self, mb: int) -> None:
        work = self._send_works.pop(("f", mb), None)
        if work is not None:
            work.wait()

    def wait_all_forward(self) -> None:
        for key in list(self._send_works):
            self._send_works.pop(key).wait()

    def recv_forward(self, mb: int) -> torch.Tensor:
        tensor = torch.empty(
            self.activation_shape, dtype=self.dtype, device=self._device()
        )
        comm.recv(
            tensor,
            src=self._global(self.rank - 1),
            tag=1000 + mb,
            group=self.group,
        )
        return tensor

    def send_backward(self, tensor: torch.Tensor, mb: int) -> None:
        if not self.has_prev:
            return
        comm.send(
            tensor.detach().contiguous(),
            dst=self._global(self.rank - 1),
            tag=2000 + mb,
            group=self.group,
        )

    def recv_backward(self, mb: int) -> torch.Tensor:
        tensor = torch.empty(
            self.activation_shape, dtype=self.dtype, device=self._device()
        )
        comm.recv(
            tensor,
            src=self._global(self.rank + 1),
            tag=2000 + mb,
            group=self.group,
        )
        return tensor

    def _device(self) -> torch.device:
        if self.device is not None:
            return self.device
        if torch.cuda.is_available():
            return torch.device("cuda", torch.cuda.current_device())
        return torch.device("cpu")


class BaseSchedule:
    def __init__(
        self,
        stage: TransformerStage,
        parallel: ParallelConfig,
        train: TrainConfig,
    ) -> None:
        self.stage = stage
        self.parallel = parallel
        self.train = train
        self.pp_rank = get_pipeline_model_parallel_rank()
        self.pp_size = get_pipeline_model_parallel_world_size()
        self.is_first = self.pp_rank == 0
        self.is_last = self.pp_rank == self.pp_size - 1
        self.micro_batches: List[MicroBatch] = []
        self.num_microbatches = 0
        self.total_loss: Optional[torch.Tensor] = None
        self.total_aux: Optional[torch.Tensor] = None
        self._forwards: Dict[int, StageOutput] = {}
        # Non-last PP stages have no loss/aux tensor, but they still join the
        # reduction.  The placeholder must live on the same device as the
        # model; a CPU zero would make NCCL/Gloo mix CPU and CUDA tensors.
        self._param_device = next(stage.parameters()).device

        local_seq = train.seq_len // get_context_parallel_world_size()
        if parallel.sequence_parallel:
            local_seq //= get_tensor_model_parallel_world_size()
        self.local_seq = local_seq
        dtype = next(stage.parameters()).dtype
        device = torch.device(train.device) if train.device else None
        self.communicator = PipelineCommunicator(
            get_pipeline_model_parallel_group(),
            self.pp_rank,
            self.pp_size,
            (train.micro_batch_size, local_seq, stage.config.hidden_size),
            dtype,
            device,
        )

    def _refresh_shape(self) -> None:
        if not self.micro_batches:
            return
        batch = self.micro_batches[0][0].size(0)
        self.communicator.activation_shape = (
            batch,
            self.local_seq,
            self.stage.config.hidden_size,
        )

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

    def _forward_step(self, mb: int) -> StageOutput:
        ids, labels = self.micro_batches[mb]
        hidden: Optional[torch.Tensor] = None
        if self.is_first:
            with self._autocast():
                out = self.stage(
                    input_ids=ids,
                    labels=labels if self.is_last else None,
                )
        else:
            hidden = self.communicator.recv_forward(mb)
            hidden.requires_grad_(True)
            with self._autocast():
                out = self.stage(
                    hidden_states=hidden,
                    labels=labels if self.is_last else None,
                )
        out.pipeline_input = hidden  # type: ignore[attr-defined]
        if not self.is_last:
            self.communicator.send_forward(out.hidden_states, mb)
        self._forwards[mb] = out
        return out

    def _backward_step(self, mb: int) -> None:
        out = self._forwards[mb]
        if not self.is_last:
            self.communicator.wait_forward(mb)
            grad_output = self.communicator.recv_backward(mb)
        else:
            grad_output = None

        scale = 1.0 / max(1, self.num_microbatches)
        tensors = []
        grads = []
        if self.is_last:
            if out.loss is None:
                raise RuntimeError("last pipeline stage did not produce a loss")
            tensors.append(out.loss * scale)
            grads.append(torch.ones_like(out.loss))
        else:
            assert grad_output is not None
            tensors.append(out.hidden_states)
            grads.append(grad_output)
        if out.aux_loss is not None and out.aux_loss.requires_grad:
            tensors.append(out.aux_loss * scale)
            grads.append(torch.ones_like(out.aux_loss))

        torch.autograd.backward(tensors, grads, retain_graph=False)

        if not self.is_first:
            input_ref = getattr(out, "pipeline_input", None)
            if input_ref is None:
                raise RuntimeError("missing pipeline input reference")
            if input_ref.grad is not None:
                self.communicator.send_backward(input_ref.grad, mb)

        if self.is_last and out.loss is not None:
            det = out.loss.detach()
            self.total_loss = det.clone() if self.total_loss is None else self.total_loss + det
        if out.aux_loss is not None:
            det = out.aux_loss.detach()
            self.total_aux = det.clone() if self.total_aux is None else self.total_aux + det

    def _reset(self, micro_batches: Sequence[MicroBatch]) -> None:
        if len(micro_batches) >= 1000:
            # Forward tags are 1000+mb and backward tags 2000+mb; larger
            # microbatch counts would make the two ranges overlap.
            raise ValueError("microbatch count must be < 1000 for this tag scheme")
        self.micro_batches = list(micro_batches)
        self.num_microbatches = len(self.micro_batches)
        self._forwards = {}
        self.total_loss = None
        self.total_aux = None
        self._refresh_shape()

    def _finish_stats(self) -> Tuple[torch.Tensor, torch.Tensor]:
        loss = (
            self.total_loss
            if self.total_loss is not None
            else torch.zeros((), device=self._param_device)
        )
        aux = (
            self.total_aux
            if self.total_aux is not None
            else torch.zeros((), device=self._param_device)
        )
        loss = loss / max(1, self.num_microbatches)
        aux = aux / max(1, self.num_microbatches)
        if self.pp_size > 1:
            loss = comm.all_reduce(loss, group=get_pipeline_model_parallel_group())
            aux = comm.all_reduce(aux, group=get_pipeline_model_parallel_group())
        return loss, aux

    def forward_only(
        self, micro_batches: Sequence[MicroBatch]
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        self._reset(micro_batches)
        total = torch.zeros((), device=self._param_device)
        aux_total = torch.zeros((), device=self._param_device)
        for mb in range(self.num_microbatches):
            out = self._forward_step(mb)
            if self.is_last and out.loss is not None:
                total = total + out.loss.detach()
            if out.aux_loss is not None:
                aux_total = aux_total + out.aux_loss.detach()
        self.communicator.wait_all_forward()
        if self.pp_size > 1:
            total = comm.all_reduce(total, group=get_pipeline_model_parallel_group())
            aux_total = comm.all_reduce(aux_total, group=get_pipeline_model_parallel_group())
        return total / max(1, self.num_microbatches), aux_total / max(
            1, self.num_microbatches
        )

    def forward_backward(
        self, micro_batches: Sequence[MicroBatch]
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        raise NotImplementedError


class SingleStageSchedule(BaseSchedule):
    def forward_backward(
        self, micro_batches: Sequence[MicroBatch]
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        self._reset(micro_batches)
        for mb in range(self.num_microbatches):
            self._forward_step(mb)
            self._backward_step(mb)
        return self._finish_stats()


class GPipeSchedule(BaseSchedule):
    def forward_backward(
        self, micro_batches: Sequence[MicroBatch]
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        self._reset(micro_batches)
        for mb in range(self.num_microbatches):
            self._forward_step(mb)
        for mb in reversed(range(self.num_microbatches)):
            self._backward_step(mb)
        return self._finish_stats()


class OneForwardOneBackward(BaseSchedule):
    def forward_backward(
        self, micro_batches: Sequence[MicroBatch]
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        self._reset(micro_batches)
        n = self.num_microbatches
        if n == 0:
            return torch.zeros(()), torch.zeros(())

        # A stage must own at least one forward before its first backward.  The
        # first stage warms up the pipeline; the last stage starts with one.
        warmup = min(n, max(1, self.pp_size - self.pp_rank - 1))
        for mb in range(warmup):
            self._forward_step(mb)
        for i in range(n - warmup):
            self._backward_step(i)
            self._forward_step(i + warmup)
        for i in range(n - warmup, n):
            self._backward_step(i)
        return self._finish_stats()


def build_schedule(
    stage: TransformerStage,
    parallel: ParallelConfig,
    train: TrainConfig,
) -> BaseSchedule:
    if parallel.pipeline_model_parallel_size == 1:
        return SingleStageSchedule(stage, parallel, train)
    if parallel.pipeline_schedule == "gpipe":
        return GPipeSchedule(stage, parallel, train)
    return OneForwardOneBackward(stage, parallel, train)

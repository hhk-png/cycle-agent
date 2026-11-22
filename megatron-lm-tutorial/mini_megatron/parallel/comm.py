"""A tiny communication abstraction with two backends.

* ``torch``     uses ``torch.distributed`` (Gloo/NCCL) and is the production-like
                path on Linux.
* ``portable``  uses one ``multiprocessing.Queue`` per rank.  It exists because
                stock Windows PyTorch wheels can fail to create a Gloo TCP
                device (the error mentions ``makeDeviceForHostname``).  The
                portable backend keeps the tutorial's multi-process tests
                runnable on a laptop while preserving the same collective and
                point-to-point semantics.

The abstraction is deliberately small: all-reduce, all-gather, reduce-scatter
(implemented as all-reduce + slice in the portable backend), broadcast,
barrier, send/recv, isend and all-to-all.  That is the subset used by the mini
model, including the expert-parallel MoE path.
"""
from __future__ import annotations

import time
from typing import Any, List, Optional, Sequence, Tuple

import torch

try:  # pragma: no cover - the import itself is trivial
    import torch.distributed as _torch_dist
except Exception:  # pragma: no cover
    _torch_dist = None


SUM = "sum"
MAX = "max"

_BACKEND: Optional[str] = None
_GROUP_COUNTER = 0


class MiniGroup:
    def __init__(self, ranks: Sequence[int], key: Optional[Tuple[int, ...]] = None):
        self.ranks = tuple(ranks)
        self.key = key if key is not None else tuple(ranks)
        self.seq = 0

    def next_seq(self) -> int:
        value = self.seq
        self.seq += 1
        return value


class _PortableBackend:
    def __init__(self, rank: int, world_size: int, inbox, outboxes):
        self.rank = rank
        self.world_size = world_size
        self.inbox = inbox
        self.outboxes = outboxes
        self.buffer: List[Tuple[Any, ...]] = []
        self.world_group = MiniGroup(tuple(range(world_size)), key=("world",))
        self.p2p_seq = {}
        self.timeout = 180.0

    def _send(self, dst: int, message: Tuple[Any, ...]) -> None:
        self.outboxes[dst].put(message)

    def _recv_matching(self, predicate):
        for i, message in enumerate(self.buffer):
            if predicate(message):
                return self.buffer.pop(i)
        started = time.time()
        while True:
            if time.time() - started > self.timeout:
                raise TimeoutError("portable collective timed out")
            message = self.inbox.get(timeout=self.timeout)
            if predicate(message):
                return message
            self.buffer.append(message)

    def _group(self, group: Optional[MiniGroup]) -> MiniGroup:
        return group if group is not None else self.world_group

    def all_reduce(self, tensor: torch.Tensor, op: str = SUM, group=None) -> torch.Tensor:
        g = self._group(group)
        if len(g.ranks) == 1:
            return tensor
        seq = g.next_seq()
        payload = tensor.detach().cpu().clone()
        for peer in g.ranks:
            if peer != self.rank:
                self._send(peer, ("all_reduce", g.key, seq, self.rank, payload))
        result = tensor.detach().cpu().clone()
        for _ in range(len(g.ranks) - 1):
            message = self._recv_matching(
                lambda m: m[0] == "all_reduce" and m[1] == g.key and m[2] == seq
            )
            other = message[4]
            if op == MAX:
                result = torch.maximum(result, other)
            else:
                result = result + other
        return result.to(tensor.device)

    def all_gather(self, tensor_list, tensor: torch.Tensor, group=None) -> None:
        g = self._group(group)
        if len(g.ranks) == 1:
            tensor_list[0].copy_(tensor)
            return
        seq = g.next_seq()
        payload = tensor.detach().cpu().clone()
        for peer in g.ranks:
            if peer != self.rank:
                self._send(peer, ("all_gather", g.key, seq, self.rank, payload))
        received = {self.rank: payload}
        for _ in range(len(g.ranks) - 1):
            message = self._recv_matching(
                lambda m: m[0] == "all_gather" and m[1] == g.key and m[2] == seq
            )
            received[message[3]] = message[4]
        for local_rank, global_rank in enumerate(g.ranks):
            tensor_list[local_rank].copy_(received[global_rank].to(tensor.device))

    def broadcast(self, tensor: torch.Tensor, src: int, group=None) -> None:
        g = self._group(group)
        if len(g.ranks) == 1:
            return
        seq = g.next_seq()
        if self.rank == src:
            payload = tensor.detach().cpu().clone()
            for peer in g.ranks:
                if peer != self.rank:
                    self._send(peer, ("broadcast", g.key, seq, self.rank, payload))
        else:
            message = self._recv_matching(
                lambda m: m[0] == "broadcast" and m[1] == g.key and m[2] == seq
            )
            tensor.copy_(message[4].to(tensor.device))

    def barrier(self, group=None) -> None:
        dummy = torch.zeros(1)
        self.all_reduce(dummy, group=group)

    def send(self, tensor: torch.Tensor, dst: int, tag: int = 0, group=None) -> None:
        key = (dst, tag)
        seq = self.p2p_seq.get(key, 0)
        self.p2p_seq[key] = seq + 1
        self._send(dst, ("p2p", tag, seq, self.rank, tensor.detach().cpu().clone()))

    def recv(self, tensor: torch.Tensor, src: int, tag: int = 0, group=None) -> None:
        key = (src, tag)
        seq = self.p2p_seq.get(key, 0)
        self.p2p_seq[key] = seq + 1
        message = self._recv_matching(
            lambda m: m[0] == "p2p" and m[1] == tag and m[2] == seq and m[3] == src
        )
        tensor.copy_(message[4].to(tensor.device))

    def isend(self, tensor, dst, tag=0, group=None):
        self.send(tensor, dst, tag, group)
        return _DummyWork()

    def all_to_all(self, output_tensors, input_tensors, group=None) -> None:
        """Exchange one tensor with every peer (variable payload per peer).

        The caller preallocates an output tensor per peer with the exact shape
        it expects to receive.  Unlike ``all_gather`` the payloads may differ
        in shape between (source, destination) pairs, which is what expert
        dispatch needs: each rank sends a different number of tokens to each
        expert-owning rank.
        """
        g = self._group(group)
        if len(g.ranks) == 1:
            output_tensors[0].copy_(input_tensors[0])
            return
        if len(input_tensors) != len(g.ranks) or len(output_tensors) != len(g.ranks):
            raise ValueError("all_to_all requires one tensor per group rank")
        seq = g.next_seq()
        for i, peer in enumerate(g.ranks):
            if peer != self.rank:
                self._send(
                    peer,
                    (
                        "all_to_all",
                        g.key,
                        seq,
                        self.rank,
                        input_tensors[i].detach().cpu().clone(),
                    ),
                )
        received = {self.rank: input_tensors[g.ranks.index(self.rank)]}
        for _ in range(len(g.ranks) - 1):
            message = self._recv_matching(
                lambda m: m[0] == "all_to_all" and m[1] == g.key and m[2] == seq
            )
            received[message[3]] = message[4]
        for i, peer in enumerate(g.ranks):
            output_tensors[i].copy_(received[peer].to(output_tensors[i].device))

    def get_global_rank(self, group, rank_in_group: int) -> int:
        if group is None:
            return rank_in_group
        return group.ranks[rank_in_group]


class _DummyWork:
    def wait(self) -> None:
        return None


_BACKEND_OBJ: Optional[_PortableBackend] = None


def initialize_torch(backend: str = "gloo", init_method: str = "env://") -> None:
    global _BACKEND
    if _torch_dist is None:
        raise RuntimeError("torch.distributed is unavailable")
    _torch_dist.init_process_group(backend=backend, init_method=init_method)
    _BACKEND = "torch"


def initialize_portable(rank: int, world_size: int, inbox, outboxes) -> None:
    global _BACKEND, _BACKEND_OBJ
    _BACKEND = "portable"
    _BACKEND_OBJ = _PortableBackend(rank, world_size, inbox, outboxes)


def is_initialized() -> bool:
    if _BACKEND == "torch":
        return _torch_dist is not None and _torch_dist.is_initialized()
    if _BACKEND == "portable":
        return _BACKEND_OBJ is not None
    return False


def is_available() -> bool:
    return True


def get_rank() -> int:
    if _BACKEND == "portable" and _BACKEND_OBJ is not None:
        return _BACKEND_OBJ.rank
    if _BACKEND == "torch" and _torch_dist is not None and _torch_dist.is_initialized():
        return _torch_dist.get_rank()
    return 0


def get_world_size() -> int:
    if _BACKEND == "portable" and _BACKEND_OBJ is not None:
        return _BACKEND_OBJ.world_size
    if _BACKEND == "torch" and _torch_dist is not None and _torch_dist.is_initialized():
        return _torch_dist.get_world_size()
    return 1


def new_group(ranks: Sequence[int], timeout_minutes: int = 30):
    global _GROUP_COUNTER
    ranks = list(ranks)
    if _BACKEND == "portable":
        # Groups with the same rank set (e.g. a CP group and a PP group in a
        # small world) must not share a message key, otherwise a collective
        # from one group can be matched by a sequence number from another.
        # Every rank creates groups in the same order, so the counter is
        # identical across ranks.
        key = ("group", _GROUP_COUNTER, tuple(sorted(ranks)))
        _GROUP_COUNTER += 1
        return MiniGroup(ranks, key=key)
    if _BACKEND == "torch" and _torch_dist is not None:
        from datetime import timedelta

        return _torch_dist.new_group(ranks=ranks, timeout=timedelta(minutes=timeout_minutes))
    return MiniGroup(ranks)


def get_group_size(group) -> int:
    if group is None:
        return get_world_size()
    if isinstance(group, MiniGroup):
        return len(group.ranks)
    return _torch_dist.get_world_size(group=group)


def get_group_rank(group) -> int:
    if group is None:
        # ``None`` means the default/world group.  Its group rank is the global
        # rank, not 0; returning 0 would make every rank pick chunk 0 in
        # ``reduce_scatter_tensor`` and silently duplicate gradients.
        return get_rank()
    if isinstance(group, MiniGroup):
        return group.ranks.index(get_rank())
    return _torch_dist.get_rank(group=group)


def get_global_rank(group, group_rank: int) -> int:
    if group is None:
        return group_rank
    if isinstance(group, MiniGroup):
        return group.ranks[group_rank]
    return _torch_dist.get_global_rank(group, group_rank)


def all_reduce(tensor: torch.Tensor, op: str = SUM, group=None) -> torch.Tensor:
    if _BACKEND == "portable" and _BACKEND_OBJ is not None:
        return _BACKEND_OBJ.all_reduce(tensor, op=op, group=group)
    if _BACKEND == "torch" and _torch_dist is not None and _torch_dist.is_initialized():
        if get_group_size(group) == 1:
            return tensor
        out = tensor.contiguous()
        reduce_op = _torch_dist.ReduceOp.MAX if op == MAX else _torch_dist.ReduceOp.SUM
        _torch_dist.all_reduce(out, op=reduce_op, group=group)
        return out
    return tensor


def all_gather(tensor_list, tensor: torch.Tensor, group=None) -> None:
    if _BACKEND == "portable" and _BACKEND_OBJ is not None:
        _BACKEND_OBJ.all_gather(tensor_list, tensor, group=group)
        return
    if _BACKEND == "torch" and _torch_dist is not None and _torch_dist.is_initialized():
        _torch_dist.all_gather(tensor_list, tensor.contiguous(), group=group)
        return
    if len(tensor_list) != 1:
        raise RuntimeError("all_gather with an uninitialised backend requires one rank")
    tensor_list[0].copy_(tensor)


def all_to_all(output_tensors, input_tensors, group=None) -> None:
    """Send ``input_tensors[i]`` to group rank ``i``; fill outputs per peer."""
    if _BACKEND == "portable" and _BACKEND_OBJ is not None:
        _BACKEND_OBJ.all_to_all(output_tensors, input_tensors, group=group)
        return
    if _BACKEND == "torch" and _torch_dist is not None and _torch_dist.is_initialized():
        _torch_dist.all_to_all(
            output_tensors,
            [tensor.contiguous() for tensor in input_tensors],
            group=group,
        )
        return
    if len(output_tensors) != 1 or len(input_tensors) != 1:
        raise RuntimeError(
            "all_to_all with an uninitialised backend requires one rank"
        )
    output_tensors[0].copy_(input_tensors[0])


def reduce_scatter_tensor(output, tensor: torch.Tensor, op: str = SUM, group=None) -> None:
    if _BACKEND == "portable" and _BACKEND_OBJ is not None:
        reduced = _BACKEND_OBJ.all_reduce(tensor, op=op, group=group)
        world = get_group_size(group)
        rank = get_group_rank(group)
        output.copy_(torch.chunk(reduced, world, dim=-1)[rank])
        return
    if _BACKEND == "torch" and _torch_dist is not None and _torch_dist.is_initialized():
        reduce_op = _torch_dist.ReduceOp.MAX if op == MAX else _torch_dist.ReduceOp.SUM
        _torch_dist.reduce_scatter_tensor(
            output, tensor.contiguous(), op=reduce_op, group=group
        )
        return
    output.copy_(tensor)


def broadcast(tensor: torch.Tensor, src: int = 0, group=None) -> torch.Tensor:
    if _BACKEND == "portable" and _BACKEND_OBJ is not None:
        _BACKEND_OBJ.broadcast(tensor, src, group=group)
        return tensor
    if _BACKEND == "torch" and _torch_dist is not None and _torch_dist.is_initialized():
        _torch_dist.broadcast(tensor, src=src, group=group)
    return tensor


def barrier(group=None) -> None:
    if _BACKEND == "portable" and _BACKEND_OBJ is not None:
        _BACKEND_OBJ.barrier(group=group)
    elif _BACKEND == "torch" and _torch_dist is not None and _torch_dist.is_initialized():
        _torch_dist.barrier(group=group)


def send(tensor: torch.Tensor, dst: int, tag: int = 0, group=None) -> None:
    if _BACKEND == "portable" and _BACKEND_OBJ is not None:
        _BACKEND_OBJ.send(tensor, dst, tag=tag, group=group)
    elif _BACKEND == "torch" and _torch_dist is not None and _torch_dist.is_initialized():
        _torch_dist.send(tensor.contiguous(), dst=dst, tag=tag, group=group)


def recv(tensor: torch.Tensor, src: int, tag: int = 0, group=None) -> None:
    if _BACKEND == "portable" and _BACKEND_OBJ is not None:
        _BACKEND_OBJ.recv(tensor, src, tag=tag, group=group)
    elif _BACKEND == "torch" and _torch_dist is not None and _torch_dist.is_initialized():
        _torch_dist.recv(tensor, src=src, tag=tag, group=group)


def isend(tensor: torch.Tensor, dst: int, tag: int = 0, group=None):
    if _BACKEND == "portable" and _BACKEND_OBJ is not None:
        return _BACKEND_OBJ.isend(tensor, dst, tag=tag, group=group)
    if _BACKEND == "torch" and _torch_dist is not None and _torch_dist.is_initialized():
        return _torch_dist.isend(tensor.contiguous(), dst=dst, tag=tag, group=group)
    return _DummyWork()


def destroy() -> None:
    global _BACKEND, _BACKEND_OBJ, _GROUP_COUNTER
    if _BACKEND == "torch" and _torch_dist is not None and _torch_dist.is_initialized():
        _torch_dist.destroy_process_group()
    _BACKEND = None
    _BACKEND_OBJ = None
    _GROUP_COUNTER = 0

"""Small deterministic datasets and a distributed batch provider.

The production Megatron data pipeline is a separate service: indexed datasets,
blended weights, packing, document masks and a background prefetcher.  The
mini version keeps the same contract: every data-parallel rank receives a
different global-batch shard, while TP/PP/CP replicas of the same DP rank see
identical tokens.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import torch

from ..config import ModelConfig, TrainConfig


class SyntheticTokenDataset:
    """A predictable token stream so a tiny model can actually learn."""

    def __init__(
        self,
        vocab_size: int,
        seq_len: int,
        num_samples: int = 4096,
        seed: int = 0,
    ) -> None:
        self.vocab_size = vocab_size
        self.seq_len = seq_len
        self.num_samples = num_samples
        self.seed = seed

    def __len__(self) -> int:
        return self.num_samples

    def __getitem__(self, index: int) -> torch.Tensor:
        start = (index * 17 + self.seed) % self.vocab_size
        positions = torch.arange(self.seq_len + 1, dtype=torch.long)
        return (positions * 3 + start) % self.vocab_size


class BinTokenDataset:
    """Memory-mapped uint16 token file (the nanoGPT/Megatron packed format)."""

    def __init__(
        self, path: str | Path, seq_len: int, vocab_size: Optional[int] = None
    ) -> None:
        import numpy as np

        self.path = Path(path)
        data = np.memmap(self.path, dtype=np.uint16, mode="r")
        if data.size < seq_len + 1:
            raise ValueError(
                f"{self.path} has {data.size} tokens, need at least {seq_len + 1}"
            )
        self.data = data
        self.seq_len = seq_len
        self.vocab_size = vocab_size
        self.num_samples = max(1, data.size - seq_len)

    def __len__(self) -> int:
        return self.num_samples

    def __getitem__(self, index: int) -> torch.Tensor:
        start = int(index) % self.num_samples
        chunk = self.data[start : start + self.seq_len + 1].astype("int64")
        if self.vocab_size is not None and chunk.size and chunk.max() >= self.vocab_size:
            raise ValueError(
                f"token id {int(chunk.max())} >= vocab_size {self.vocab_size} "
                f"at {self.path}[{start}:{start + self.seq_len + 1}]"
            )
        return torch.from_numpy(chunk)


def create_dataset(
    model: ModelConfig,
    train: TrainConfig,
    *,
    seed: Optional[int] = None,
) -> object:
    seed = train.seed if seed is None else seed
    if train.data == "synthetic":
        vocab = train.synthetic_vocab_size or model.vocab_size
        if vocab != model.vocab_size:
            raise ValueError(
                "synthetic_vocab_size must equal model.vocab_size in this mini build"
            )
        return SyntheticTokenDataset(
            vocab, train.seq_len, num_samples=4096, seed=seed
        )
    if not train.data_path:
        raise ValueError("train.data_path is required for data='bin'")
    return BinTokenDataset(
        train.data_path, train.seq_len, vocab_size=model.vocab_size
    )


class BatchProvider:
    """Shard one deterministic global batch across data-parallel ranks.

    A step samples ``micro_batch_size * grad_accumulation_steps * dp``
    examples once, from a generator seeded by ``(seed, step, sample_offset)``.
    Rank ``r`` takes a contiguous slice and splits it into its micro-batches.
    The union across DP ranks is therefore exactly the batch a single-process
    run would see at the same step, which is what makes "same global batch"
    comparisons meaningful.  Production samplers implement the same contract
    with a sharded index stream rather than re-sampling the global batch on
    every rank, but the observable data order is equivalent.
    """

    def __init__(
        self,
        dataset,
        *,
        micro_batch_size: int,
        grad_accumulation_steps: int,
        data_parallel_rank: int,
        seq_len: int,
        seed: int,
        data_parallel_size: int = 1,
    ) -> None:
        if data_parallel_size < 1:
            raise ValueError("data_parallel_size must be positive")
        if not 0 <= data_parallel_rank < data_parallel_size:
            raise ValueError("data_parallel_rank out of range")
        self.dataset = dataset
        self.micro_batch_size = micro_batch_size
        self.grad_accumulation_steps = grad_accumulation_steps
        self.data_parallel_rank = data_parallel_rank
        self.data_parallel_size = data_parallel_size
        self.seq_len = seq_len
        self.seed = seed
        self._per_rank = micro_batch_size * grad_accumulation_steps

    def _global_indices(self, step: int, sample_offset: int) -> torch.Tensor:
        generator = torch.Generator()
        generator.manual_seed(self.seed + step * 100003 + sample_offset)
        return torch.randint(
            0,
            len(self.dataset),
            (self._per_rank * self.data_parallel_size,),
            generator=generator,
        )

    def get_micro_batches(
        self, step: int, *, sample_offset: int = 0
    ) -> List[Tuple[torch.Tensor, torch.Tensor]]:
        start = self.data_parallel_rank * self._per_rank
        indices = self._global_indices(step, sample_offset)[
            start : start + self._per_rank
        ]
        batches = []
        for offset in range(0, self._per_rank, self.micro_batch_size):
            micro = indices[offset : offset + self.micro_batch_size]
            windows = torch.stack([self.dataset[int(i)] for i in micro])
            x = windows[:, :-1].contiguous()
            y = windows[:, 1:].contiguous()
            batches.append((x, y))
        return batches

    def get_global_batch_tokens(self, step: int) -> torch.Tensor:
        """Return this rank's shard of the global batch.

        The name is historical: the returned tensor is the rank-local slice,
        not the union across DP ranks.  Use it for shape/token accounting.
        """
        batches = self.get_micro_batches(step)
        return torch.cat([x for x, _ in batches], dim=0)

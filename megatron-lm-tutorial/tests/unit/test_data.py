import torch

from mini_megatron.data.dataset import BatchProvider, SyntheticTokenDataset


def test_batch_provider_is_reproducible_and_rank_dependent():
    ds = SyntheticTokenDataset(64, 16, num_samples=128, seed=3)
    a = BatchProvider(ds, micro_batch_size=2, grad_accumulation_steps=2,
                      data_parallel_rank=0, seq_len=16, seed=9)
    b = BatchProvider(ds, micro_batch_size=2, grad_accumulation_steps=2,
                      data_parallel_rank=0, seq_len=16, seed=9)
    c = BatchProvider(ds, micro_batch_size=2, grad_accumulation_steps=1,
                      data_parallel_rank=1, data_parallel_size=2,
                      seq_len=16, seed=9)
    ba = a.get_micro_batches(3)
    bb = b.get_micro_batches(3)
    bc = c.get_micro_batches(3)
    for (x1, y1), (x2, y2) in zip(ba, bb):
        assert torch.equal(x1, x2) and torch.equal(y1, y2)
    assert not torch.equal(ba[0][0], bc[0][0])
    for x, y in ba:
        assert x.shape == (2, 16) and y.shape == (2, 16)
        assert torch.equal(x[:, 1:], y[:, :-1])


def test_data_parallel_ranks_partition_one_global_batch():
    ds = SyntheticTokenDataset(64, 16, num_samples=128, seed=3)
    full = BatchProvider(ds, micro_batch_size=2, grad_accumulation_steps=2,
                         data_parallel_rank=0, seq_len=16, seed=9)
    rank0 = BatchProvider(ds, micro_batch_size=2, grad_accumulation_steps=1,
                          data_parallel_rank=0, data_parallel_size=2,
                          seq_len=16, seed=9)
    rank1 = BatchProvider(ds, micro_batch_size=2, grad_accumulation_steps=1,
                          data_parallel_rank=1, data_parallel_size=2,
                          seq_len=16, seed=9)
    full_x = torch.cat([x for x, _ in full.get_micro_batches(3)], dim=0)
    sharded_x = torch.cat(
        [x for x, _ in rank0.get_micro_batches(3)]
        + [x for x, _ in rank1.get_micro_batches(3)],
        dim=0,
    )
    # The union is the single-process global batch, in slice order.  This is
    # the contract that makes "same global batch" loss comparisons valid.
    assert torch.equal(full_x, sharded_x)
    assert not torch.equal(rank0.get_micro_batches(3)[0][0],
                           rank1.get_micro_batches(3)[0][0])


def test_dataset_targets_follow_inputs():
    ds = SyntheticTokenDataset(32, 8, num_samples=4, seed=1)
    for i in range(4):
        tokens = ds[i]
        assert tokens.shape == (9,)
        assert ((tokens[1:] - tokens[:-1]) % 32 == 3).all()


def test_bin_dataset(tmp_path):
    import numpy as np
    from mini_megatron.data.dataset import BinTokenDataset

    path = tmp_path / "tokens.bin"
    rng = np.random.default_rng(0)
    rng.integers(0, 64, size=512, dtype=np.uint16).tofile(path)
    ds = BinTokenDataset(path, seq_len=16)
    assert len(ds) == 512 - 16
    assert ds[0].shape == (17,)

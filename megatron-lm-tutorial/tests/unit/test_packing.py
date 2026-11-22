import torch

from mini_megatron.data.packing import (
    IGNORE_INDEX,
    PackedSequenceDataset,
    iter_document_masks,
    pack_documents,
)


def test_document_boundaries_reset_positions_and_mask_loss():
    docs = [[1, 2, 3], [4, 5]]
    samples = pack_documents(docs, seq_len=8)
    assert len(samples) == 1
    s = samples[0]
    assert s.input_ids.tolist() == [1, 2, 3, 4, 5, 0, 0, 0]
    assert s.labels.tolist() == [2, 3, IGNORE_INDEX, 5, IGNORE_INDEX, -100, -100, -100]
    assert s.position_ids.tolist() == [0, 1, 2, 0, 1, 0, 0, 0]
    assert s.document_ids.tolist() == [0, 0, 0, 1, 1, -1, -1, -1]
    assert s.loss_mask.tolist() == [1, 1, 0, 1, 0, 0, 0, 0]


def test_windows_are_non_overlapping_and_length_fixed():
    docs = [list(range(10)), list(range(10, 23))]
    samples = pack_documents(docs, seq_len=4)
    assert all(x.input_ids.numel() == 4 for x in samples)
    # A full pack has no padding document id.
    assert samples[0].document_ids.tolist() == [0, 0, 0, 0]
    # Labels never cross a document boundary.
    for sample in samples:
        boundary = sample.document_ids[:-1] != sample.document_ids[1:]
        crossing = boundary & (sample.document_ids[:-1] >= 0)
        for idx in torch.nonzero(crossing).flatten().tolist():
            assert sample.labels[idx].item() == IGNORE_INDEX


def test_dataset_and_block_diagonal_mask():
    ds = PackedSequenceDataset([[1, 2, 3], [4, 5]], seq_len=8)
    assert len(ds) == 1
    item = ds[0]
    mask = next(iter_document_masks(item["document_ids"]))
    assert mask.shape == (8, 8)
    # Token 0 (doc 0) can see 1/2 but not 3 (doc 1).
    assert mask[0, 0] and mask[0, 1]
    assert not mask[0, 3]
    # Padding row keeps its diagonal so softmax is defined.
    assert mask[7, 7]
    assert not mask[7, 0]

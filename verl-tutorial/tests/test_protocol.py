from __future__ import annotations

import numpy as np
import pytest

from mini_verl.protocol import DataProto, pad_dataproto_to_divisor, unpad_dataproto


def make(n=6):
    return DataProto(
        batch={"x": np.arange(n * 2).reshape(n, 2), "y": np.arange(n)},
        non_tensor_batch={"text": np.asarray([f"t{i}" for i in range(n)], dtype=object)},
        meta_info={"tag": "a"},
    )


def test_len_and_validation():
    data = make()
    assert len(data) == 6
    with pytest.raises(ValueError):
        DataProto(batch={"x": np.zeros((2, 3)), "y": np.zeros(3)})


def test_getitem_and_select():
    data = make()
    assert len(data[1]) == 1
    assert data[1].batch["x"][0, 0] == 2
    subset = data.select_idxs([0, 2, 4])
    assert len(subset) == 3
    assert subset.non_tensor_batch["text"].tolist() == ["t0", "t2", "t4"]


def test_chunk_requires_divisible_and_concat_roundtrip():
    data = make()
    with pytest.raises(ValueError):
        data.chunk(4)
    chunks = data.chunk(3)
    assert [len(chunk) for chunk in chunks] == [2, 2, 2]
    restored = DataProto.concat(chunks)
    assert np.array_equal(restored.batch["x"], data.batch["x"])
    assert restored.non_tensor_batch["text"].tolist() == data.non_tensor_batch["text"].tolist()


def test_split():
    data = make(7)
    parts = data.split(3)
    assert [len(part) for part in parts] == [3, 3, 1]


def test_repeat_interleave():
    data = make(3)
    repeated = data.repeat(2, interleave=True)
    assert repeated.non_tensor_batch["text"].tolist() == ["t0", "t0", "t1", "t1", "t2", "t2"]
    tiled = data.repeat(2, interleave=False)
    assert tiled.non_tensor_batch["text"].tolist() == ["t0", "t1", "t2", "t0", "t1", "t2"]


def test_union_and_conflict():
    a = make(3)
    b = DataProto(batch={"z": np.ones(3)})
    merged = a.union(b)
    assert "z" in merged.batch
    with pytest.raises(ValueError):
        a.union(DataProto(batch={"x": np.zeros((3, 2))}))


def test_padding():
    data = make(5)
    padded, pad = pad_dataproto_to_divisor(data, 3)
    assert pad == 1
    assert len(padded) == 6
    restored = unpad_dataproto(padded, pad)
    assert len(restored) == 5


def test_padding_clears_loss_masks():
    data = DataProto(
        batch={
            "x": np.arange(5),
            "response_mask": np.ones((5, 3), dtype=np.float32),
        }
    )
    padded, pad = pad_dataproto_to_divisor(data, 3)
    assert pad == 1
    assert np.all(padded.batch["response_mask"][-1] == 0)
    assert padded.batch["loss_mask"][-1] == 0
    assert np.all(padded.batch["loss_mask"][:-1] == 1)


def test_pop_rename_and_dict_roundtrip():
    data = make(3)
    popped = data.pop("y")
    assert popped.shape == (3,)
    data.rename({"x": "features"})
    assert "features" in data.batch
    restored = DataProto.from_dict(data.to_dict())
    assert np.array_equal(restored.batch["features"], data.batch["features"])


def test_copy_is_independent():
    data = make(3)
    clone = data.copy()
    clone.batch["x"][0, 0] = 99
    assert data.batch["x"][0, 0] != 99

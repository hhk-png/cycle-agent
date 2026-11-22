"""DataProto: chunk, concat, repeat, pad and reorder."""

import numpy as np

from _common import header

from mini_verl.protocol import DataProto


def main() -> None:
    header("DataProto is the contract between controller, workers and rollout")
    data = DataProto(
        batch={"input_ids": np.arange(12).reshape(6, 2), "response_mask": np.ones((6, 2))},
        non_tensor_batch={"uid": np.arange(6), "text": np.asarray([f"s{i}" for i in range(6)], dtype=object)},
        meta_info={"source": "example"},
    )
    print("original:", data)
    chunks = data.chunk(3)
    print("chunk lengths:", [len(chunk) for chunk in chunks])
    print("concat equal:", np.array_equal(DataProto.concat(chunks).batch["input_ids"], data.batch["input_ids"]))
    repeated = data.repeat(2, interleave=True)
    print("interleaved repeat:", repeated.non_tensor_batch["text"].tolist())
    padded, pad = data.pad_to_divisor(4)
    print(f"padded to {len(padded)} (pad={pad}), unpadded={len(padded[: len(padded) - pad])}")
    reordered = data.select_idxs([5, 3, 1, 0, 2, 4])
    print("reordered text:", reordered.non_tensor_batch["text"].tolist())


if __name__ == "__main__":
    main()

"""Packed-sequence dataset with document boundaries and loss masks.

Production Megatron supports packed sequences (and THD layouts) so that short
documents do not waste compute on padding.  Packing is only correct if the
model also knows where documents begin/end:

* attention must not cross document boundaries;
* position ids restart per document (for absolute/learned positions);
* the last token of a document has no in-document next token, so its label is
  ignored;
* padding tokens are ignored in both loss and gradient.

This module builds the *data* half of that contract.  ``mini_megatron``'s
attention currently exposes a causal/padding mask, so the packed dataset is not
wired into the training loop; it is a tested, reusable building block and a
reference for how production packers encode boundaries.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence

import torch

IGNORE_INDEX = -100


@dataclass
class PackedSample:
    input_ids: torch.Tensor
    labels: torch.Tensor
    position_ids: torch.Tensor
    document_ids: torch.Tensor
    loss_mask: torch.Tensor

    def to_dict(self) -> Dict[str, torch.Tensor]:
        return {
            "input_ids": self.input_ids,
            "labels": self.labels,
            "position_ids": self.position_ids,
            "document_ids": self.document_ids,
            "loss_mask": self.loss_mask,
        }


def _flatten_documents(
    documents: Sequence[Sequence[int]],
) -> tuple[List[int], List[int], List[int], List[bool]]:
    tokens: List[int] = []
    doc_ids: List[int] = []
    positions: List[int] = []
    last_in_doc: List[bool] = []
    for doc_id, doc in enumerate(documents):
        if not doc:
            continue
        for pos, token in enumerate(doc):
            tokens.append(int(token))
            doc_ids.append(doc_id)
            positions.append(pos)
            last_in_doc.append(pos == len(doc) - 1)
    return tokens, doc_ids, positions, last_in_doc


def pack_documents(
    documents: Sequence[Sequence[int]],
    *,
    seq_len: int,
    pad_id: int = 0,
    ignore_index: int = IGNORE_INDEX,
) -> List[PackedSample]:
    """Pack documents into non-overlapping fixed-length windows.

    Each sample contains ``seq_len`` inputs and ``seq_len`` labels.  A label is
    set to ``ignore_index`` when the next token belongs to another document or
    is padding.
    """
    if seq_len <= 0:
        raise ValueError("seq_len must be positive")
    tokens, doc_ids, positions, last_in_doc = _flatten_documents(documents)
    if not tokens:
        return []

    samples: List[PackedSample] = []
    # Include a final short window so trailing tokens are not silently dropped.
    for start in range(0, len(tokens), seq_len):
        window_tokens = tokens[start : start + seq_len]
        if not window_tokens:
            break
        # Labels: next token inside the same document, otherwise ignore.
        labels: List[int] = []
        for j in range(len(window_tokens)):
            global_idx = start + j
            if j + 1 < len(window_tokens) and not last_in_doc[global_idx]:
                labels.append(tokens[global_idx + 1])
            elif (
                j + 1 == len(window_tokens)
                and global_idx + 1 < len(tokens)
                and not last_in_doc[global_idx]
            ):
                labels.append(tokens[global_idx + 1])
            else:
                labels.append(ignore_index)
        # Right-pad short final windows.
        pad = seq_len - len(window_tokens)
        input_ids = window_tokens + [pad_id] * pad
        labels = labels + [ignore_index] * pad
        position_ids = positions[start : start + len(window_tokens)] + [0] * pad
        document_ids = doc_ids[start : start + len(window_tokens)] + [-1] * pad
        loss_mask = [0 if label == ignore_index else 1 for label in labels]
        samples.append(
            PackedSample(
                input_ids=torch.tensor(input_ids, dtype=torch.long),
                labels=torch.tensor(labels, dtype=torch.long),
                position_ids=torch.tensor(position_ids, dtype=torch.long),
                document_ids=torch.tensor(document_ids, dtype=torch.long),
                loss_mask=torch.tensor(loss_mask, dtype=torch.long),
            )
        )
    return samples


class PackedSequenceDataset:
    """Map-style dataset over packed documents.

    ``documents`` can be any sequence of token-id sequences.  The class builds
    all packs eagerly; for large corpora, build packs offline and store the
    five arrays in a shard format instead.
    """

    def __init__(
        self,
        documents: Sequence[Sequence[int]],
        *,
        seq_len: int,
        pad_id: int = 0,
        ignore_index: int = IGNORE_INDEX,
    ) -> None:
        self.seq_len = seq_len
        self.pad_id = pad_id
        self.ignore_index = ignore_index
        self.samples = pack_documents(
            documents,
            seq_len=seq_len,
            pad_id=pad_id,
            ignore_index=ignore_index,
        )

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, index: int) -> Dict[str, torch.Tensor]:
        return self.samples[index].to_dict()


def iter_document_masks(
    document_ids: torch.Tensor,
) -> Iterable[torch.Tensor]:
    """Yield a boolean ``[s, s]`` block-diagonal mask for one packed sample.

    ``True`` means the key position may be attended to by the query position.
    The diagonal is always allowed; padding positions (document id ``-1``) are
    only allowed to attend to themselves so softmax never sees an empty row.
    """
    if document_ids.ndim != 1:
        raise ValueError("document_ids must be 1-D")
    s = document_ids.numel()
    same_doc = document_ids[:, None] == document_ids[None, :]
    valid = document_ids >= 0
    mask = same_doc & valid[None, :]
    # Keep one valid key per row to avoid softmax(-inf) for padding rows.
    idx = torch.arange(s)
    mask[idx, idx] = True
    yield mask

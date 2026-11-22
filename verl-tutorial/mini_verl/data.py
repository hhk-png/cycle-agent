"""Tokenizer, synthetic RL datasets and the RLHF collate function."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from .protocol import DataProto

__all__ = [
    "CharTokenizer",
    "RLHFDataset",
    "build_dataset",
    "collate_fn",
    "make_batches",
]

_BASE_CHARS = (
    " 0123456789"
    "+-=:,.?!/\\()[]{}<>"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "'\"#%&*;@_|~"
)


class CharTokenizer:
    """Deterministic character tokenizer with the four verl-style specials."""

    pad_token = "<pad>"
    bos_token = "<bos>"
    eos_token = "<eos>"
    unk_token = "<unk>"

    def __init__(self) -> None:
        chars = sorted(set(_BASE_CHARS))
        self.itos = [self.pad_token, self.bos_token, self.eos_token, self.unk_token] + chars
        self.stoi = {token: index for index, token in enumerate(self.itos)}
        self.pad_token_id = 0
        self.bos_token_id = 1
        self.eos_token_id = 2
        self.unk_token_id = 3

    @property
    def vocab_size(self) -> int:
        return len(self.itos)

    def encode(self, text: str, add_bos: bool = False, add_eos: bool = False) -> list[int]:
        ids = [self.stoi.get(char, self.unk_token_id) for char in text]
        if add_bos:
            ids = [self.bos_token_id] + ids
        if add_eos:
            ids = ids + [self.eos_token_id]
        return ids

    def decode(self, ids: Iterable[int], skip_special_tokens: bool = True) -> str:
        pieces = []
        for index in ids:
            index = int(index)
            if index < 0 or index >= len(self.itos):
                pieces.append(self.unk_token)
                continue
            token = self.itos[index]
            if skip_special_tokens and token in {
                self.pad_token,
                self.bos_token,
                self.eos_token,
                self.unk_token,
            }:
                continue
            pieces.append(token)
        return "".join(pieces)

    def save(self, path: str) -> None:
        import json

        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"itos": self.itos}, handle, ensure_ascii=False)

    @classmethod
    def load(cls, path: str) -> "CharTokenizer":
        import json

        tokenizer = cls()
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        tokenizer.itos = list(payload["itos"])
        tokenizer.stoi = {token: index for index, token in enumerate(tokenizer.itos)}
        return tokenizer


@dataclass
class Sample:
    prompt: str
    answer: str
    data_source: str
    uid: int
    index: int


class RLHFDataset:
    """A deterministic synthetic dataset with verl's field names.

    Real verl reads Parquet files with ``prompt`` / ``reward_model`` /
    ``data_source`` / ``extra_info`` columns.  MiniVerl keeps the same logical
    fields but generates them in code so the tutorial runs offline.
    """

    def __init__(self, samples: list[Sample]):
        self.samples = samples

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, index: int) -> Sample:
        return self.samples[index]

    def __iter__(self):
        return iter(self.samples)

    def subset(self, size: int) -> "RLHFDataset":
        return RLHFDataset(self.samples[:size])


class _BanditDataset(RLHFDataset):
    """Contextual bandit: the only thing that matters is the prompt token."""

    ACTIONS = ["x", "y", "z", "w"]

    def __init__(self, size: int, seed: int = 0):
        rng = random.Random(seed)
        samples = []
        for i in range(size):
            context = i % len(self.ACTIONS)
            samples.append(
                Sample(
                    prompt=f"={chr(ord('A') + context)}",
                    answer=self.ACTIONS[context],
                    data_source="bandit",
                    uid=i,
                    index=i,
                )
            )
        rng.shuffle(samples)
        for position, sample in enumerate(samples):
            sample.index = position
        super().__init__(samples)


class _CopyDataset(RLHFDataset):
    WORDS = ["ax", "by", "cq", "dz", "ev", "fw", "gu", "ht"]

    def __init__(self, size: int, seed: int = 0):
        rng = random.Random(seed)
        samples = []
        for i in range(size):
            word = self.WORDS[i % len(self.WORDS)]
            samples.append(
                Sample(
                    prompt=f"{word}=",
                    answer=word,
                    data_source="copy",
                    uid=i,
                    index=i,
                )
            )
        rng.shuffle(samples)
        for position, sample in enumerate(samples):
            sample.index = position
        super().__init__(samples)


class _ArithmeticDataset(RLHFDataset):
    def __init__(self, size: int, seed: int = 0, max_operand: int = 4):
        rng = random.Random(seed)
        samples = []
        for i in range(size):
            a = rng.randint(0, max_operand)
            b = rng.randint(0, max_operand - a)
            samples.append(
                Sample(
                    prompt=f"{a}+{b}=",
                    answer=str(a + b),
                    data_source="arithmetic",
                    uid=i,
                    index=i,
                )
            )
        super().__init__(samples)


class _GSM8KLiteDataset(RLHFDataset):
    """Tiny templated word problems, used for the verifiable math reward."""

    TEMPLATES = [
        ("Tom has {a} apples and buys {b} more. How many apples does he have?", "a+b"),
        ("A box has {a} pens. {b} pens are removed. How many remain?", "a-b"),
        ("Each bag has {a} marbles. There are {b} bags. How many marbles?", "a*b"),
        ("There are {a} cookies shared by {b} children. How many each?", "a/b"),
    ]

    def __init__(self, size: int, seed: int = 0):
        rng = random.Random(seed)
        samples = []
        for i in range(size):
            template, op = self.TEMPLATES[i % len(self.TEMPLATES)]
            if op == "a/b":
                b = rng.randint(1, 4)
                a = b * rng.randint(1, 4)
            elif op == "a-b":
                a = rng.randint(3, 8)
                b = rng.randint(0, a)
            else:
                a = rng.randint(0, 5)
                b = rng.randint(0, 5)
            prompt = template.format(a=a, b=b) + " Answer: "
            if op == "a+b":
                answer = a + b
            elif op == "a-b":
                answer = a - b
            elif op == "a*b":
                answer = a * b
            else:
                answer = a // b
            samples.append(
                Sample(
                    prompt=prompt,
                    answer=f"\\boxed{{{answer}}}",
                    data_source="math",
                    uid=i,
                    index=i,
                )
            )
        super().__init__(samples)


def build_dataset(kind: str, size: int, seed: int = 0) -> RLHFDataset:
    if kind == "bandit":
        return _BanditDataset(size, seed)
    if kind == "copy":
        return _CopyDataset(size, seed)
    if kind == "arithmetic":
        return _ArithmeticDataset(size, seed)
    if kind in {"math", "gsm8k_lite"}:
        return _GSM8KLiteDataset(size, seed)
    raise ValueError(f"unknown dataset {kind!r}")


def collate_fn(
    samples: list[Sample],
    tokenizer: CharTokenizer,
    max_prompt_length: int = 16,
    max_response_length: int = 8,
) -> DataProto:
    batch_size = len(samples)
    total_length = max_prompt_length + max_response_length
    input_ids = np.full((batch_size, total_length), tokenizer.pad_token_id, dtype=np.int64)
    attention_mask = np.zeros((batch_size, total_length), dtype=np.float32)
    response_mask = np.zeros((batch_size, total_length), dtype=np.float32)
    prompt_lengths = np.zeros(batch_size, dtype=np.int64)
    prompts = np.empty(batch_size, dtype=object)
    answers = np.empty(batch_size, dtype=object)
    data_sources = np.empty(batch_size, dtype=object)
    uids = np.zeros(batch_size, dtype=np.int64)
    indices = np.zeros(batch_size, dtype=np.int64)

    for row, sample in enumerate(samples):
        prompt_ids = tokenizer.encode(sample.prompt)
        if sample.data_source == "math":
            # Math prompts put the operands at the head and the instruction at
            # the tail; left truncation would drop the numbers and make the
            # reward unlearnable.
            prompt_ids = prompt_ids[:max_prompt_length]
        else:
            prompt_ids = prompt_ids[-max_prompt_length:]
        response_ids = tokenizer.encode(sample.answer, add_eos=True)[:max_response_length]
        sequence = prompt_ids + response_ids
        length = len(sequence)
        input_ids[row, :length] = sequence
        attention_mask[row, :length] = 1.0
        response_mask[row, len(prompt_ids) : length] = 1.0
        prompt_lengths[row] = len(prompt_ids)
        prompts[row] = sample.prompt
        answers[row] = sample.answer
        data_sources[row] = sample.data_source
        uids[row] = sample.uid
        indices[row] = sample.index

    return DataProto(
        batch={
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "response_mask": response_mask,
            "prompt_length": prompt_lengths,
        },
        non_tensor_batch={
            "prompts": prompts,
            "answers": answers,
            "data_source": data_sources,
            "uid": uids,
            "index": indices,
        },
        meta_info={"max_prompt_length": max_prompt_length, "max_response_length": max_response_length},
    )


def make_batches(
    dataset: RLHFDataset,
    batch_size: int,
    tokenizer: CharTokenizer,
    max_prompt_length: int,
    max_response_length: int,
    shuffle: bool = True,
    seed: int = 0,
    drop_last: bool = True,
    filter_overlong_prompts: bool = False,
) -> list[DataProto]:
    order = list(range(len(dataset)))
    if filter_overlong_prompts:
        order = [
            index
            for index in order
            if len(tokenizer.encode(dataset[index].prompt)) <= max_prompt_length
        ]
        if len(order) < batch_size:
            raise ValueError(
                f"filter_overlong_prompts left {len(order)} samples, fewer than "
                f"batch_size={batch_size}; raise max_prompt_length or add data"
            )
    if shuffle:
        random.Random(seed).shuffle(order)
    batches = []
    start = 0
    for start in range(0, len(order) - batch_size + 1, batch_size):
        indices = order[start : start + batch_size]
        batches.append(
            collate_fn(
                [dataset[index] for index in indices],
                tokenizer,
                max_prompt_length,
                max_response_length,
            )
        )
    next_index = len(batches) * batch_size
    if not drop_last and next_index < len(order):
        batches.append(
            collate_fn(
                [dataset[index] for index in order[next_index:]],
                tokenizer,
                max_prompt_length,
                max_response_length,
            )
        )
    return batches


def preference_pairs(dataset: RLHFDataset, tokenizer: CharTokenizer, max_prompt_length: int, max_response_length: int) -> list[DataProto]:
    """Build (prompt, chosen, rejected) triples for the DPO chapter.

    The rejected response is a deliberately wrong one-character mutation.  It
    is a teaching fixture, not a preference-data quality claim.
    """
    result: list[DataProto] = []
    for sample in dataset:
        chosen = tokenizer.encode(sample.answer, add_eos=True)[:max_response_length]
        wrong = list(chosen)
        if wrong and wrong[0] != tokenizer.eos_token_id:
            wrong[0] = (wrong[0] + 1) % tokenizer.vocab_size
        rejected = wrong
        prompt_ids = tokenizer.encode(sample.prompt)[-max_prompt_length:]
        result.append(
            DataProto(
                batch={
                    "prompt_ids": np.asarray([prompt_ids], dtype=np.int64),
                    "chosen_ids": np.asarray([chosen], dtype=np.int64),
                    "rejected_ids": np.asarray([rejected], dtype=np.int64),
                    # dpo_loss_fn requires these; without them a DataProto from
                    # preference_pairs raises KeyError at the first batch.
                    "chosen_mask": np.ones((1, len(chosen)), dtype=np.float32),
                    "rejected_mask": np.ones((1, len(rejected)), dtype=np.float32),
                },
                non_tensor_batch={"prompt": np.asarray([sample.prompt], dtype=object)},
                meta_info={},
            )
        )
    return result

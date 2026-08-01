"""A self-contained character-level tokenizer.

Real vLLM uses HuggingFace tokenizers (BPE / SentencePiece / Llama tokenizers)
shipped inside model repositories. For the mini version we use a plain
character tokenizer so the whole tutorial is fully self-contained and does not
depend on the HuggingFace hub.  The interface deliberately mimics the subset
of ``transformers.PreTrainedTokenizer`` that vLLM's tokenizer backend uses:

    encode(text) -> list[int]
    decode(ids)  -> str
    eos_token_id / pad_token_id
"""

from __future__ import annotations

import json
from typing import List, Union

# Characters we allow.  This keeps the vocabulary small enough for a tiny
# numpy model while still being able to render normal English text.
DEFAULT_CHARS = (
    "abcdefghijklmnopqrstuvwxyz "
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    ".,!?;:'\"-()[]{}<>/@#$%^&*+=|\\~`\n\t"
)


class CharTokenizer:
    """Character-level tokenizer with a dedicated EOS token id.

    The EOS id is ``len(chars)`` (one past the last character), which mirrors
    how real tokenizers reserve a special ``<|endoftext|>`` token.
    """

    def __init__(self, chars: str = DEFAULT_CHARS):
        self.chars = "".join(sorted(set(chars)))
        self._stoi = {c: i for i, c in enumerate(self.chars)}
        self._itos = {i: c for i, c in enumerate(self.chars)}
        # reserve the last id for the end-of-sequence marker
        self.eos_token_id = len(self.chars)
        self.pad_token_id = 0

    # ------------------------------------------------------------------ #
    # public API (mimics transformers.PreTrainedTokenizer)
    # ------------------------------------------------------------------ #
    @property
    def vocab_size(self) -> int:
        return len(self.chars) + 1  # chars + EOS

    def encode(self, text: Union[str, List[str]]) -> List[int]:
        if isinstance(text, (list, tuple)):
            return [self.encode(t) for t in text]  # type: ignore[return-value]
        ids = []
        for ch in text:
            if ch not in self._stoi:
                # Unknown characters are dropped (the real vLLM backend maps
                # out-of-vocabulary bytes to a single ``unk`` token).
                continue
            ids.append(self._stoi[ch])
        return ids

    def decode(self, ids: Union[int, List[int], List[List[int]]]) -> str:
        if isinstance(ids, int):
            ids = [ids]
        if ids and isinstance(ids[0], (list, tuple)):
            return [self.decode(i) for i in ids]  # type: ignore[return-value]
        return "".join(self._itos.get(int(i), "") for i in ids)

    # ------------------------------------------------------------------ #
    # serialisation
    # ------------------------------------------------------------------ #
    def to_json(self) -> dict:
        return {"chars": self.chars, "eos_token_id": self.eos_token_id}

    @classmethod
    def from_json(cls, data: dict) -> "CharTokenizer":
        tok = cls(data.get("chars", DEFAULT_CHARS))
        return tok

    def save(self, path) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_json(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path) -> "CharTokenizer":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_json(json.load(f))

    def __repr__(self) -> str:
        return f"CharTokenizer(vocab_size={self.vocab_size})"

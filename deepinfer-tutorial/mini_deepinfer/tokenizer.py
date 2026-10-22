"""A byte-level tokenizer with explicit control tokens.

DeepSeek Recipe makes an important distinction between text that *looks like*
a control token and a control token that the template actually inserted.  A
byte-level vocabulary makes that distinction explicit: user text is always
encoded as bytes, while control tokens are only produced by ``special_id``.
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_SPECIAL_TOKENS: dict[str, int] = {
    "<|pad|>": 256,
    "<|bos|>": 257,
    "<|eos|>": 258,
    "<|user|>": 259,
    "<|assistant|>": 260,
    "<|end|>": 261,
    "<|image|>": 262,
    "<|tool|>": 263,
}

BASE_VOCAB_SIZE = 256
VOCAB_SIZE = BASE_VOCAB_SIZE + len(DEFAULT_SPECIAL_TOKENS)


class Tokenizer:
    """Byte-level encoder/decoder.

    ``encode`` is deliberately conservative: special-token spellings in user
    input are ordinary bytes.  ``encode_special`` is the only way to obtain a
    control id, and it is used by :meth:`apply_chat_template`.
    """

    def __init__(self, special_tokens: dict[str, int] | None = None) -> None:
        self.special_tokens = dict(special_tokens or DEFAULT_SPECIAL_TOKENS)
        self._reverse = {v: k for k, v in self.special_tokens.items()}
        self.pad_id = self.special_tokens.get("<|pad|>")
        self.bos_id = self.special_tokens.get("<|bos|>")
        self.eos_id = self.special_tokens.get("<|eos|>")
        self.unk_id = None
        self.vocab_size = BASE_VOCAB_SIZE + len(self.special_tokens)
        if max(self._reverse) >= self.vocab_size:
            raise ValueError("special token ids must be contiguous after the byte vocabulary")

    @classmethod
    def from_file(cls, path: str | Path) -> Tokenizer:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data.get("special_tokens"))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps({"type": "byte", "special_tokens": self.special_tokens}, indent=2),
            encoding="utf-8",
        )

    def encode(self, text: str, *, add_bos: bool = False) -> list[int]:
        ids = list(text.encode("utf-8"))
        if add_bos and self.bos_id is not None:
            ids.insert(0, self.bos_id)
        return ids

    def encode_special(self, token: str) -> int:
        try:
            return self.special_tokens[token]
        except KeyError as exc:
            raise KeyError(f"unknown special token: {token}") from exc

    def decode(self, ids: list[int], *, skip_special_tokens: bool = True) -> str:
        payload = bytearray()
        for token_id in ids:
            if token_id < BASE_VOCAB_SIZE:
                payload.append(token_id)
            elif not skip_special_tokens:
                payload.extend(self._reverse.get(token_id, "").encode("utf-8"))
        return payload.decode("utf-8", errors="replace")

    def decode_bytes(self, ids: list[int]) -> bytes:
        return bytes(token_id for token_id in ids if 0 <= token_id < BASE_VOCAB_SIZE)

    def is_special(self, token_id: int) -> bool:
        return token_id >= BASE_VOCAB_SIZE

    def apply_chat_template(
        self,
        messages: list[dict[str, str]],
        *,
        add_generation_prompt: bool = True,
    ) -> list[int]:
        """Encode a minimal Chat-Completions-style conversation.

        The template is intentionally simple, but it preserves the property
        that matters for security: role markers come from the template, never
        from message content.
        """
        ids: list[int] = []
        if self.bos_id is not None:
            ids.append(self.bos_id)
        for message in messages:
            role = message.get("role", "user")
            if role not in ("system", "user", "assistant", "tool"):
                raise ValueError(f"unsupported role: {role}")
            ids.append(self.encode_special("<|user|>"))
            if role != "user":
                ids.extend(self.encode(f"[{role}] "))
            ids.extend(self.encode(str(message.get("content", ""))))
            ids.append(self.encode_special("<|end|>"))
        if add_generation_prompt:
            ids.append(self.encode_special("<|assistant|>"))
        return ids

    def __len__(self) -> int:
        return self.vocab_size


class StreamingDetokenizer:
    """Emit text as soon as a complete UTF-8 prefix is available.

    A byte-level tokenizer can split a multi-byte character across tokens (for
    example a Chinese character is normally three tokens).  Buffering the
    incomplete tail prevents replacement characters from leaking into a
    streaming response.
    """

    def __init__(self, tokenizer: Tokenizer) -> None:
        self.tokenizer = tokenizer
        self._buffer = bytearray()

    @staticmethod
    def _complete_prefix(data: bytes) -> int:
        """Length of the longest prefix that is valid UTF-8.

        ``data`` is expected to contain at most one incomplete trailing
        character, which is the case for an incremental byte stream.
        """
        for cut in range(len(data), max(-1, len(data) - 4), -1):
            try:
                data[:cut].decode("utf-8")
                return cut
            except UnicodeDecodeError:
                continue
        # A long invalid run is not a split character.  Consume it with
        # replacement so the stream cannot stall forever.
        return len(data)

    def feed(self, token_id: int) -> str:
        if token_id < BASE_VOCAB_SIZE:
            self._buffer.append(token_id)
        elif self.tokenizer.is_special(token_id):
            return ""
        cut = self._complete_prefix(bytes(self._buffer))
        if cut == 0:
            return ""
        text = bytes(self._buffer[:cut]).decode("utf-8", errors="replace")
        del self._buffer[:cut]
        return text

    def flush(self) -> str:
        text = bytes(self._buffer).decode("utf-8", errors="replace")
        self._buffer.clear()
        return text

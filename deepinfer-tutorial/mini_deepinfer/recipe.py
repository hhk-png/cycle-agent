"""A small protocol adapter inspired by DeepSeek Recipe.

Recipe's production implementation is Rust with Python bindings and supports
Chat Completions, Responses, Messages, tool calls and incremental parsing.
This module keeps the two ideas that matter for the tutorial: normalize
different message shapes into one conversation, and parse a streaming text
stream into reasoning/content events without losing split markers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import MiniDeepInferError
from .tokenizer import Tokenizer

ALLOWED_ROLES = {"system", "user", "assistant", "tool"}


class ProtocolError(MiniDeepInferError, ValueError):
    pass


def _normalize_role(role: str) -> str:
    if role == "developer":
        return "system"
    if role not in ALLOWED_ROLES:
        raise ProtocolError(f"unsupported role: {role}")
    return role


def _content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") in (None, "text", "input_text", "output_text"):
                    parts.append(str(block.get("text", "")))
                elif block.get("type") == "image":
                    parts.append("<|image|>")
                else:
                    parts.append(str(block.get("text", "")))
            else:
                parts.append(str(block))
        return "".join(parts)
    raise ProtocolError(f"unsupported content type: {type(content).__name__}")


def normalize_messages(messages, protocol: str = "openai") -> list[dict[str, str]]:
    """Normalize OpenAI/Anthropic/Responses-style messages.

    The output always uses ``{"role": ..., "content": text}``.  Image blocks
    become the literal ``<|image|>`` marker; the caller is responsible for
    replacing it with real image embeddings.
    """
    if protocol not in ("openai", "anthropic", "responses"):
        raise ProtocolError(f"unsupported protocol: {protocol}")
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    if not isinstance(messages, list):
        raise ProtocolError("messages must be a list or a string")
    normalized = []
    for message in messages:
        if not isinstance(message, dict):
            raise ProtocolError("each message must be an object")
        role = _normalize_role(str(message.get("role", "user")))
        content = message.get("content", "")
        normalized.append({"role": role, "content": _content_to_text(content)})
    return normalized


def encode_conversation(
    tokenizer: Tokenizer,
    messages,
    protocol: str = "openai",
    *,
    add_generation_prompt: bool = True,
) -> list[int]:
    return tokenizer.apply_chat_template(
        normalize_messages(messages, protocol),
        add_generation_prompt=add_generation_prompt,
    )


@dataclass
class Event:
    type: str
    text: str


@dataclass
class StreamingEventParser:
    """Split text into reasoning/content events.

    Markers are ``<reasoning>`` and ``</reasoning>``.  A marker can be split
    across chunks; the parser keeps a small tail buffer until it can decide.
    """

    reasoning_open: str = "<reasoning>"
    reasoning_close: str = "</reasoning>"
    _buffer: str = ""
    _in_reasoning: bool = False
    _events: list[Event] = field(default_factory=list)

    def _emit(self, text: str) -> None:
        if not text:
            return
        event_type = "reasoning" if self._in_reasoning else "content"
        if self._events and self._events[-1].type == event_type:
            self._events[-1].text += text
        else:
            self._events.append(Event(event_type, text))

    def feed(self, text: str) -> list[Event]:
        self._buffer += text
        while self._buffer:
            if not self._in_reasoning:
                index = self._buffer.find(self.reasoning_open)
                if index >= 0:
                    self._emit(self._buffer[:index])
                    self._buffer = self._buffer[index + len(self.reasoning_open) :]
                    self._in_reasoning = True
                    continue
                # Emit everything except a possible partial marker at the tail.
                keep = len(self.reasoning_open) - 1
                if len(self._buffer) > keep:
                    self._emit(self._buffer[:-keep])
                    self._buffer = self._buffer[-keep:]
                break
            index = self._buffer.find(self.reasoning_close)
            if index >= 0:
                self._emit(self._buffer[:index])
                self._buffer = self._buffer[index + len(self.reasoning_close) :]
                self._in_reasoning = False
                continue
            keep = len(self.reasoning_close) - 1
            if len(self._buffer) > keep:
                self._emit(self._buffer[:-keep])
                self._buffer = self._buffer[-keep:]
            break
        events, self._events = self._events, []
        return events

    def flush(self) -> list[Event]:
        self._emit(self._buffer)
        self._buffer = ""
        events, self._events = self._events, []
        return events

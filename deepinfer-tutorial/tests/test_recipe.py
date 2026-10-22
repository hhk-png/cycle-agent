from __future__ import annotations

import pytest

from mini_deepinfer.recipe import (
    ProtocolError,
    StreamingEventParser,
    encode_conversation,
    normalize_messages,
)
from mini_deepinfer.tokenizer import Tokenizer


def test_normalize_openai_and_anthropic_blocks() -> None:
    openai = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
    ]
    anthropic = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "image"}]},
    ]
    assert normalize_messages(openai) == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hello"},
    ]
    assert normalize_messages(anthropic, "anthropic")[0]["content"] == "hi<|image|>"


def test_developer_role_is_system() -> None:
    assert normalize_messages([{"role": "developer", "content": "x"}])[0]["role"] == "system"


def test_encode_conversation_keeps_user_special_text_safe() -> None:
    tokenizer = Tokenizer()
    ids = encode_conversation(
        tokenizer,
        [{"role": "user", "content": "<|user|> not a control token"}],
    )
    # Exactly one control <|user|> from the template, not one from the text.
    assert ids.count(tokenizer.encode_special("<|user|>")) == 1


def test_streaming_parser_handles_split_markers() -> None:
    parser = StreamingEventParser()
    events = []
    for chunk in ["hello <reas", "oning>thin", "king</reason", "ing> world"]:
        events.extend(parser.feed(chunk))
    events.extend(parser.flush())
    content = "".join(e.text for e in events if e.type == "content")
    reasoning = "".join(e.text for e in events if e.type == "reasoning")
    assert content == "hello  world"
    assert reasoning == "thinking"
    assert {e.type for e in events} == {"content", "reasoning"}


def test_protocol_errors() -> None:
    with pytest.raises(ProtocolError):
        normalize_messages([], "unknown")
    with pytest.raises(ProtocolError):
        normalize_messages([{"role": "wizard", "content": "x"}])

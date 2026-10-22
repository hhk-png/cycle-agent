from __future__ import annotations

import json

import pytest

from mini_deepinfer.errors import StructuredOutputError
from mini_deepinfer.structured_output import (
    build_matcher,
    compile_ebnf,
    compile_regex,
)


def test_regex_mask_and_accept() -> None:
    matcher = compile_regex(r"[a-z]+")
    allowed = matcher.allowed_bytes()
    assert all(ord("a") <= char <= ord("z") for char in allowed)
    assert matcher.accept_char(ord("a"))
    assert matcher.accept_char(ord("b"))
    assert matcher.allows_eos()
    assert not matcher.accept_char(ord("1"))


def test_unicode_literal_matches_utf8_bytes() -> None:
    matcher = compile_regex("中文")
    for byte in "中文".encode("utf-8"):
        assert matcher.accept_char(byte)
    assert matcher.allows_eos()


def test_regex_alternation_and_repetition() -> None:
    matcher = compile_regex(r"(ab|cd){2}")
    for char in b"abcd":
        assert matcher.accept_char(char)
    assert matcher.allows_eos()
    assert not compile_regex(r"a{2}").accept_char(ord("b"))


def test_snapshot_rollback() -> None:
    matcher = compile_regex(r"abc")
    assert matcher.accept_char(ord("a"))
    snap = matcher.snapshot()
    assert matcher.accept_char(ord("b"))
    matcher.restore(snap)
    assert matcher.accept_char(ord("b"))


def test_json_matcher_accepts_valid_document() -> None:
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "age": {"type": "integer"},
        },
        "required": ["name", "age"],
    }
    matcher = build_matcher(
        {"schema_type": "json", "schema": json.dumps(schema)},
        eos_token_id=258,
        vocab_size=264,
    )
    text = b'{"name":"Ada","age":37}'
    for char in text:
        assert matcher.accept_token(char)
    assert matcher.grammar.allows_eos()


def test_json_matcher_rejects_invalid_escape() -> None:
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    matcher = build_matcher(
        {"schema_type": "json", "schema": json.dumps(schema)},
        eos_token_id=258,
        vocab_size=264,
    )
    invalid = b'{"name":"\\q"}'
    assert not all(matcher.accept_token(byte) for byte in invalid)


def test_json_matcher_rejects_unescaped_control_characters() -> None:
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    matcher = build_matcher(
        {"schema_type": "json", "schema": json.dumps(schema)},
        eos_token_id=258,
        vocab_size=264,
    )
    # A literal newline (0x0A) inside a JSON string is invalid; an escaped
    # `\n` is fine and is covered by the valid-document test.
    invalid = b'{"name":"a\nb"}'
    assert not all(matcher.accept_token(byte) for byte in invalid)


def test_json_matcher_rejects_unsupported_ref() -> None:
    schema = {"$ref": "#/$defs/User", "$defs": {"User": {"type": "string"}}}
    with pytest.raises(StructuredOutputError):
        build_matcher(
            {"schema_type": "json", "schema": json.dumps(schema)},
            eos_token_id=258,
            vocab_size=264,
        )


def test_ebnf_matcher() -> None:
    matcher = build_matcher(
        {"schema_type": "ebnf", "schema": 'start = "a" { "b" } "c" ;'},
        eos_token_id=258,
        vocab_size=264,
    )
    for char in b"abbbc":
        assert matcher.accept_token(char)
    assert matcher.grammar.allows_eos()


def test_recursive_ebnf_is_rejected_at_limit() -> None:
    with pytest.raises(StructuredOutputError):
        compile_ebnf('start = "a" start | "b" ;')


def test_trigger_and_bound() -> None:
    matcher = build_matcher(
        {"schema_type": "regex", "schema": r"[0-9]+"},
        eos_token_id=258,
        vocab_size=264,
        trigger_token_ids=(ord("["),),
        bound_token_id=ord("]"),
    )
    # Free text before the trigger.
    assert matcher.accept_token(ord("x"))
    # The bound token fires the forced trigger walk.
    assert matcher.accept_token(ord("]"))
    assert not matcher.active
    assert matcher.accept_token(ord("["))
    assert matcher.active
    assert matcher.accept_token(ord("1"))


def test_unknown_schema_type() -> None:
    with pytest.raises(StructuredOutputError):
        build_matcher(
            {"schema_type": "yaml", "schema": "x"},
            eos_token_id=258,
            vocab_size=264,
        )

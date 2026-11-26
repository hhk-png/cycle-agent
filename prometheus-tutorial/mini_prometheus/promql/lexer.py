"""PromQL lexer.

The lexer is context-free except for one convenience: a run of digits followed
by duration units is emitted as a DURATION token.  That is unambiguous because
PromQL never allows a number and an identifier to be juxtaposed without an
operator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from ..model import ParseError
from ..util.timeutil import parse_duration


class TokenKind(str, Enum):
    EOF = "EOF"
    IDENT = "IDENT"
    NUMBER = "NUMBER"
    DURATION = "DURATION"
    STRING = "STRING"
    LPAREN = "("
    RPAREN = ")"
    LBRACE = "{"
    RBRACE = "}"
    LBRACKET = "["
    RBRACKET = "]"
    COMMA = ","
    COLON = ":"
    AT = "@"
    OPERATOR = "OP"


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    text: str
    position: int
    value: object = None

    def __repr__(self) -> str:
        return f"Token({self.kind.value}, {self.text!r})"


_OPERATORS = [
    "=~", "!~", "==", "!=", "<=", ">=",
    "+", "-", "*", "/", "%", "^", "=", "<", ">",
]

_DURATION_RE = re.compile(r"(?:\d+(?:ms|s|m|h|d|w|y))+")
_NUMBER_RE = re.compile(
    r"(?:0[xX][0-9a-fA-F]+)|(?:(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
)
_IDENT_RE = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_ESCAPES = {
    "a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
    "v": "\v", "\\": "\\", '"': '"', "'": "'", "0": "\0",
}


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in " \t\r\n":
            index += 1
            continue
        if char == "#":
            newline = text.find("\n", index)
            index = length if newline < 0 else newline + 1
            continue
        if char == "(":
            tokens.append(Token(TokenKind.LPAREN, char, index)); index += 1; continue
        if char == ")":
            tokens.append(Token(TokenKind.RPAREN, char, index)); index += 1; continue
        if char == "{":
            tokens.append(Token(TokenKind.LBRACE, char, index)); index += 1; continue
        if char == "}":
            tokens.append(Token(TokenKind.RBRACE, char, index)); index += 1; continue
        if char == "[":
            tokens.append(Token(TokenKind.LBRACKET, char, index)); index += 1; continue
        if char == "]":
            tokens.append(Token(TokenKind.RBRACKET, char, index)); index += 1; continue
        if char == ",":
            tokens.append(Token(TokenKind.COMMA, char, index)); index += 1; continue
        if char == ":":
            tokens.append(Token(TokenKind.COLON, char, index)); index += 1; continue
        if char == "@":
            tokens.append(Token(TokenKind.AT, char, index)); index += 1; continue
        if char in ('"', "'", "`"):
            value, index = _read_string(text, index)
            tokens.append(Token(TokenKind.STRING, value, index, value))
            continue
        # Number or duration
        match = _DURATION_RE.match(text, index)
        if match and _is_number_start(char):
            raw = match.group(0)
            # A duration must consume the full run of digits/units; "5m3" is
            # a parse error rather than number 5 followed by metric m3.
            end = match.end()
            if end < length and (text[end].isdigit() or text[end] == "."):
                raise ParseError(f"invalid duration starting at {index}", index)
            tokens.append(
                Token(TokenKind.DURATION, raw, index, parse_duration(raw))
            )
            index = end
            continue
        if _is_number_start(char) or (
            char == "." and index + 1 < length and text[index + 1].isdigit()
        ):
            match = _NUMBER_RE.match(text, index)
            if not match:
                raise ParseError(f"unexpected character {char!r}", index)
            raw = match.group(0)
            try:
                value = float(int(raw, 16)) if raw.lower().startswith("0x") else float(raw)
            except ValueError as exc:
                raise ParseError(f"invalid number {raw!r}", index) from exc
            tokens.append(Token(TokenKind.NUMBER, raw, index, value))
            index = match.end()
            continue
        match = _IDENT_RE.match(text, index)
        if match:
            raw = match.group(0)
            if raw in ("Inf", "NaN"):
                tokens.append(Token(TokenKind.NUMBER, raw, index, float(raw)))
            else:
                tokens.append(Token(TokenKind.IDENT, raw, index))
            index = match.end()
            continue
        for op in _OPERATORS:
            if text.startswith(op, index):
                tokens.append(Token(TokenKind.OPERATOR, op, index))
                index += len(op)
                break
        else:
            raise ParseError(f"unexpected character {char!r}", index)
    tokens.append(Token(TokenKind.EOF, "", length))
    return tokens


def _is_number_start(char: str) -> bool:
    return char.isdigit()


def _read_string(text: str, start: int) -> tuple[str, int]:
    quote = text[start]
    index = start + 1
    out: list[str] = []
    if quote == "`":
        while index < len(text):
            if text[index] == "`":
                return "".join(out), index + 1
            out.append(text[index])
            index += 1
        raise ParseError("unterminated raw string", start)
    while index < len(text):
        char = text[index]
        if char == quote:
            return "".join(out), index + 1
        if char == "\\":
            index += 1
            if index >= len(text):
                raise ParseError("unterminated escape", start)
            esc = text[index]
            if esc in _ESCAPES:
                out.append(_ESCAPES[esc])
                index += 1
            elif esc == "x":
                raw = text[index + 1 : index + 3]
                if len(raw) != 2:
                    raise ParseError("invalid \\x escape", index)
                out.append(chr(int(raw, 16)))
                index += 3
            elif esc == "u":
                raw = text[index + 1 : index + 5]
                out.append(chr(int(raw, 16)))
                index += 5
            elif esc == "U":
                raw = text[index + 1 : index + 9]
                out.append(chr(int(raw, 16)))
                index += 9
            else:
                # Prometheus rejects unknown escapes in quoted strings.
                raise ParseError(f"unknown escape \\{esc}", index)
            continue
        out.append(char)
        index += 1
    raise ParseError("unterminated string", start)


# Backwards-compatible alias used in a few docs/tests.
Lexer = tokenize

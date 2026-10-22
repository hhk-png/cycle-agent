"""Grammar-constrained decoding.

The mini engine compiles a regex, a JSON-schema subset, or a (bounded) EBNF
grammar into an NFA and turns the current state set into a token bitmask.  The
production engine delegates compilation to xgrammar and runs it on a separate
CPU pool; the control flow is the same: compile once, advance per token, mask
illegal tokens, and roll back on rejected speculative tokens.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .errors import StructuredOutputError

ALL_BYTES = tuple(range(256))
DIGITS = tuple(range(ord("0"), ord("9") + 1))
WORD = tuple(
    list(range(ord("a"), ord("z") + 1))
    + list(range(ord("A"), ord("Z") + 1))
    + list(range(ord("0"), ord("9") + 1))
    + [ord("_")]
)
SPACE = (ord(" "), ord("\t"), ord("\n"), ord("\r"), ord("\f"), ord("\v"))


# ----------------------------------------------------------------------
# NFA
# ----------------------------------------------------------------------
class NFA:
    def __init__(self) -> None:
        self.trans: list[dict[int | None, set[int]]] = []
        self.start = 0
        self.accept = 0

    def new_state(self) -> int:
        self.trans.append({})
        return len(self.trans) - 1

    def add(self, src: int, char: int | None, dst: int) -> None:
        self.trans[src].setdefault(char, set()).add(dst)

    def epsilon_closure(self, states: frozenset[int] | set[int]) -> frozenset[int]:
        stack = list(states)
        seen = set(states)
        while stack:
            state = stack.pop()
            for dest in self.trans[state].get(None, ()):
                if dest not in seen:
                    seen.add(dest)
                    stack.append(dest)
        return frozenset(seen)

    def _reverse_reachable(self) -> set[int]:
        reverse: list[set[int]] = [set() for _ in self.trans]
        for src, edges in enumerate(self.trans):
            for dests in edges.values():
                for dst in dests:
                    reverse[dst].add(src)
        stack = [self.accept]
        seen = {self.accept}
        while stack:
            state = stack.pop()
            for prev in reverse[state]:
                if prev not in seen:
                    seen.add(prev)
                    stack.append(prev)
        return seen

    def compile(self) -> "NFAMatcher":
        return NFAMatcher(self)


class NFAMatcher:
    def __init__(self, nfa: NFA) -> None:
        self.nfa = nfa
        self.closure_cache: dict[frozenset[int], frozenset[int]] = {}
        self.useful = nfa._reverse_reachable()
        self.state = nfa.epsilon_closure({nfa.start})
        self.finished = False
        self._mask_cache: dict[frozenset[int], tuple[int, ...]] = {}

    def _closure(self, states: set[int]) -> frozenset[int]:
        key = frozenset(states)
        cached = self.closure_cache.get(key)
        if cached is None:
            cached = self.nfa.epsilon_closure(key)
            self.closure_cache[key] = cached
        return cached

    def _step(self, char: int) -> frozenset[int]:
        dests: set[int] = set()
        for state in self.state:
            dests.update(self.nfa.trans[state].get(char, ()))
        return self._closure(dests) if dests else frozenset()

    def _useful_after(self, states: frozenset[int]) -> bool:
        return any(state in self.useful for state in states)

    def allows_eos(self) -> bool:
        return self.nfa.accept in self.state

    def allowed_bytes(self) -> tuple[int, ...]:
        cached = self._mask_cache.get(self.state)
        if cached is not None:
            return cached
        allowed = []
        for char in ALL_BYTES:
            nxt = self._step(char)
            if nxt and self._useful_after(nxt):
                allowed.append(char)
        result = tuple(allowed)
        self._mask_cache[self.state] = result
        return result

    def allowed_mask(self, vocab_size: int, eos_token_id: int | None) -> Any:
        import numpy as np

        mask = np.zeros(vocab_size, dtype=bool)
        for char in self.allowed_bytes():
            mask[char] = True
        if self.allows_eos() and eos_token_id is not None:
            mask[eos_token_id] = True
        return mask

    def accept_char(self, char: int) -> bool:
        nxt = self._step(char)
        if not nxt or not self._useful_after(nxt):
            return False
        self.state = nxt
        return True

    def accept_token(self, token_id: int, eos_token_id: int | None) -> bool:
        if eos_token_id is not None and token_id == eos_token_id:
            if self.allows_eos():
                self.finished = True
                return True
            return False
        if not 0 <= token_id < 256:
            return False
        return self.accept_char(token_id)

    def snapshot(self) -> tuple[frozenset[int], bool]:
        return self.state, self.finished

    def restore(self, snap: tuple[frozenset[int], bool]) -> None:
        self.state, self.finished = snap


# ----------------------------------------------------------------------
# Regex parser -> NFA
# ----------------------------------------------------------------------
class RegexParser:
    def __init__(self, pattern: str) -> None:
        self.pattern = pattern
        self.pos = 0
        self.nfa = NFA()

    def parse(self) -> NFA:
        pattern = self.pattern
        if pattern.startswith("^"):
            pattern = pattern[1:]
        if pattern.endswith("$") and not pattern.endswith("\\$"):
            pattern = pattern[:-1]
        self.pattern = pattern
        start, end = self._parse_alt()
        self.nfa.start, self.nfa.accept = start, end
        return self.nfa

    def _peek(self) -> str | None:
        return self.pattern[self.pos] if self.pos < len(self.pattern) else None

    def _take(self) -> str:
        char = self.pattern[self.pos]
        self.pos += 1
        return char

    def _parse_alt(self) -> tuple[int, int]:
        start, end = self._parse_concat()
        while self._peek() == "|":
            self._take()
            other_start, other_end = self._parse_concat()
            new_start = self.nfa.new_state()
            new_end = self.nfa.new_state()
            self.nfa.add(new_start, None, start)
            self.nfa.add(new_start, None, other_start)
            self.nfa.add(end, None, new_end)
            self.nfa.add(other_end, None, new_end)
            start, end = new_start, new_end
        return start, end

    def _parse_concat(self) -> tuple[int, int]:
        fragments: list[tuple[int, int]] = []
        while self._peek() is not None and self._peek() not in ")|":
            fragments.append(self._parse_repeat())
        if not fragments:
            state = self.nfa.new_state()
            return state, state
        start, end = fragments[0]
        for next_start, next_end in fragments[1:]:
            self.nfa.add(end, None, next_start)
            end = next_end
        return start, end

    def _parse_repeat(self) -> tuple[int, int]:
        start, end = self._parse_atom()
        char = self._peek()
        if char == "*":
            self._take()
            new_start = self.nfa.new_state()
            new_end = self.nfa.new_state()
            self.nfa.add(new_start, None, start)
            self.nfa.add(new_start, None, new_end)
            self.nfa.add(end, None, start)
            self.nfa.add(end, None, new_end)
            return new_start, new_end
        if char == "+":
            self._take()
            new_start = self.nfa.new_state()
            new_end = self.nfa.new_state()
            self.nfa.add(new_start, None, start)
            self.nfa.add(end, None, start)
            self.nfa.add(end, None, new_end)
            return new_start, new_end
        if char == "?":
            self._take()
            new_start = self.nfa.new_state()
            new_end = self.nfa.new_state()
            self.nfa.add(new_start, None, start)
            self.nfa.add(new_start, None, new_end)
            self.nfa.add(end, None, new_end)
            return new_start, new_end
        if char == "{":
            return self._parse_braces(start, end)
        return start, end

    def _parse_braces(self, start: int, end: int) -> tuple[int, int]:
        close = self.pattern.find("}", self.pos)
        if close < 0:
            raise StructuredOutputError("unterminated repetition")
        body = self.pattern[self.pos + 1 : close]
        self.pos = close + 1
        if "," in body:
            left, right = body.split(",", 1)
            minimum = int(left) if left else 0
            maximum = int(right) if right else minimum + 8
        else:
            minimum = maximum = int(body)
        if minimum > 64 or maximum > 64 or maximum < minimum:
            raise StructuredOutputError("repetition bound is too large or invalid")
        fragments: list[tuple[int, int]] = []
        for _ in range(minimum):
            s, e = self._clone_fragment(start, end)
            fragments.append((s, e))
        for _ in range(maximum - minimum):
            s, e = self._clone_fragment(start, end)
            new_start = self.nfa.new_state()
            new_end = self.nfa.new_state()
            self.nfa.add(new_start, None, s)
            self.nfa.add(new_start, None, new_end)
            self.nfa.add(e, None, new_end)
            fragments.append((new_start, new_end))
        if not fragments:
            state = self.nfa.new_state()
            return state, state
        out_start, out_end = fragments[0]
        for s, e in fragments[1:]:
            self.nfa.add(out_end, None, s)
            out_end = e
        return out_start, out_end

    def _clone_fragment(self, start: int, end: int) -> tuple[int, int]:
        # Re-parse the atom is not possible after the fact; instead copy the
        # subgraph reachable from ``start`` to ``end``.
        mapping: dict[int, int] = {}

        def clone(state: int) -> int:
            if state in mapping:
                return mapping[state]
            new = self.nfa.new_state()
            mapping[state] = new
            return new

        stack = [start]
        seen = {start}
        while stack:
            state = stack.pop()
            clone(state)
            for dests in self.nfa.trans[state].values():
                for dest in dests:
                    if dest not in seen:
                        seen.add(dest)
                        stack.append(dest)
        for state in seen:
            for char, dests in self.nfa.trans[state].items():
                for dest in dests:
                    self.nfa.add(mapping[state], char, mapping[dest])
        return mapping[start], mapping[end]

    def _parse_atom(self) -> tuple[int, int]:
        char = self._peek()
        if char is None:
            raise StructuredOutputError("unexpected end of regex")
        if char == "(":
            self._take()
            if self.pattern.startswith("?:", self.pos):
                self.pos += 2
            start, end = self._parse_alt()
            if self._peek() != ")":
                raise StructuredOutputError("unterminated group")
            self._take()
            return start, end
        if char == "[":
            chars = self._parse_class()
            start = self.nfa.new_state()
            end = self.nfa.new_state()
            for value in chars:
                self.nfa.add(start, value, end)
            return start, end
        if char == ".":
            self._take()
            start = self.nfa.new_state()
            end = self.nfa.new_state()
            for value in ALL_BYTES:
                if value != ord("\n"):
                    self.nfa.add(start, value, end)
            return start, end
        if char == "\\":
            chars = self._parse_escape()
            start = self.nfa.new_state()
            end = self.nfa.new_state()
            for value in chars:
                self._add_literal(start, value, end)
            return start, end
        self._take()
        start = self.nfa.new_state()
        end = self.nfa.new_state()
        self._add_literal(start, ord(char), end)
        return start, end

    def _add_literal(self, start: int, value: int, end: int) -> None:
        """Add a literal codepoint as its UTF-8 byte sequence.

        The matcher works on bytes, so a literal such as ``中`` must become
        three byte transitions rather than one transition with codepoint
        0x4E2D (which no byte mask can ever contain).
        """
        if value < 0:
            raise StructuredOutputError(f"invalid literal codepoint {value}")
        data = chr(value).encode("utf-8")
        current = start
        for byte in data[:-1]:
            nxt = self.nfa.new_state()
            self.nfa.add(current, byte, nxt)
            current = nxt
        self.nfa.add(current, data[-1], end)

    def _parse_escape(self) -> tuple[int, ...]:
        self._take()  # backslash
        char = self._take()
        mapping = {
            "d": DIGITS,
            "w": WORD,
            "s": SPACE,
            "n": (ord("\n"),),
            "t": (ord("\t"),),
            "r": (ord("\r"),),
            "\\": (ord("\\"),),
            ".": (ord("."),),
            "(": (ord("("),),
            ")": (ord(")"),),
            "[": (ord("["),),
            "]": (ord("]"),),
            "{": (ord("{"),),
            "}": (ord("}"),),
            "|": (ord("|"),),
            "*": (ord("*"),),
            "+": (ord("+"),),
            "?": (ord("?"),),
            "^": (ord("^"),),
            "$": (ord("$"),),
            '"': (ord('"'),),
        }
        if char == "x":
            hex_digits = self.pattern[self.pos : self.pos + 2]
            if len(hex_digits) != 2:
                raise StructuredOutputError("bad \\x escape")
            self.pos += 2
            return (int(hex_digits, 16),)
        if char not in mapping:
            return (ord(char),)
        return mapping[char]

    def _parse_class_atom(self) -> int | tuple[int, ...]:
        """Parse one class member: a literal codepoint or an escape set."""
        char = self._peek()
        if char == "\\":
            part = self._parse_escape()
            return part[0] if len(part) == 1 else part
        self._take()
        return ord(char)

    def _parse_class(self) -> tuple[int, ...]:
        self._take()  # [
        negate = False
        if self._peek() == "^":
            self._take()
            negate = True
        values: set[int] = set()
        first = True
        while True:
            char = self._peek()
            if char is None:
                raise StructuredOutputError("unterminated character class")
            if char == "]" and not first:
                self._take()
                break
            first = False
            start = self._parse_class_atom()
            if isinstance(start, tuple):
                # Escapes such as \d expand to a set; they cannot form the
                # endpoint of a range, but they are valid class members.
                values.update(start)
                continue
            if (
                self._peek() == "-"
                and self.pos + 1 < len(self.pattern)
                and self.pattern[self.pos + 1] != "]"
            ):
                self._take()
                end = self._parse_class_atom()
                if isinstance(end, tuple):
                    raise StructuredOutputError("character range endpoint must be a single codepoint")
                if end < start:
                    raise StructuredOutputError("bad character range")
                values.update(range(start, end + 1))
            else:
                values.add(start)
        if negate:
            values = set(ALL_BYTES) - values
        if not values:
            raise StructuredOutputError("empty character class")
        return tuple(sorted(values))


def compile_regex(pattern: str) -> NFAMatcher:
    return RegexParser(pattern).parse().compile()


# ----------------------------------------------------------------------
# JSON schema -> regex (practical subset)
# ----------------------------------------------------------------------
def _json_string() -> str:
    # The upper bound is a teaching-device concession: an unconstrained
    # string length makes the tiny model wander until max_tokens.  A real
    # grammar leaves this open and relies on the model to close the quote.
    # Only the escapes JSON actually permits: \" \\ \/ \b \f \n \r \t and
    # \uXXXX.  A generic \\. would accept "\q", which json.loads rejects.
    # JSON forbids unescaped control characters (U+0000..U+001F) inside a
    # string.  The earlier `[^"\\]` class accepted them, so a matcher could
    # emit a "valid" document that `json.loads` rejects.  Keep the byte class
    # explicit; the matcher works on UTF-8 bytes, not Unicode code points.
    return (
        r'"[^"\\\x00-\x1f]{0,16}'
        r'(?:(?:\\["\\/bfnrt]|\\u[0-9a-fA-F]{4})[^"\\\x00-\x1f]{0,16})*"'
    )


def _json_number() -> str:
    return r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"


def _escape_literal(text: str) -> str:
    return re.escape(text)


_UNSUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "$ref",
        "$defs",
        "definitions",
        "patternProperties",
        "allOf",
        "not",
        "if",
        "then",
        "else",
        "contains",
        "prefixItems",
        "unevaluatedProperties",
        "dependentSchemas",
        "dependentRequired",
    }
)


def _schema_to_regex(schema: dict[str, Any]) -> str:
    # A schema compiler must not silently widen the language.  A `$ref` used to
    # fall through to "any JSON" because it has no `type`; that turns a strict
    # production schema into an unconstrained grammar.  Reject constructs the
    # teaching compiler cannot enforce instead of pretending to support them.
    unsupported = _UNSUPPORTED_SCHEMA_KEYWORDS.intersection(schema)
    if unsupported:
        names = ", ".join(sorted(unsupported))
        raise StructuredOutputError(
            f"unsupported JSON schema keyword(s): {names}; "
            "resolve references or simplify the schema before compiling"
        )
    if "anyOf" in schema:
        return "(?:" + "|".join(_schema_to_regex(item) for item in schema["anyOf"]) + ")"
    if "oneOf" in schema:
        return "(?:" + "|".join(_schema_to_regex(item) for item in schema["oneOf"]) + ")"
    if "const" in schema:
        return _escape_literal(json.dumps(schema["const"], ensure_ascii=False))
    if "enum" in schema:
        return "(?:" + "|".join(_escape_literal(json.dumps(v, ensure_ascii=False)) for v in schema["enum"]) + ")"
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        return "(?:" + "|".join(_schema_to_regex({**schema, "type": t}) for t in schema_type) + ")"
    if schema_type == "object":
        properties: dict[str, Any] = schema.get("properties", {})
        required = set(schema.get("required", properties.keys()))
        if not properties:
            return r"\{[ ]?\}"
        parts = []
        for key, subschema in properties.items():
            if key not in required:
                # Optional fields are accepted only at the end, in schema order.
                continue
            parts.append(
                _escape_literal(json.dumps(key, ensure_ascii=False))
                + r"[ ]?:[ ]?"
                + _schema_to_regex(subschema)
            )
        if not parts:
            return r"\{[ ]?\}"
        body = r"[ ]?,[ ]?".join(parts)
        optional = [k for k in properties if k not in required]
        for key in optional:
            body += (
                r"(?:[ ]?,[ ]?"
                + _escape_literal(json.dumps(key, ensure_ascii=False))
                + r"[ ]?:[ ]?"
                + _schema_to_regex(properties[key])
                + ")?"
            )
        return r"\{" + body + r"[ ]?\}"
    if schema_type == "array":
        item = _schema_to_regex(schema.get("items", {}))
        return r"\[[ ]?(?:" + item + r"(?:[ ]?,[ ]?" + item + r")*)?[ ]?\]"
    if schema_type == "string":
        return _json_string()
    if schema_type == "integer":
        return r"-?(?:0|[1-9][0-9]*)"
    if schema_type == "number":
        return _json_number()
    if schema_type == "boolean":
        return "(?:true|false)"
    if schema_type == "null":
        return "null"
    if schema_type is None:
        return r"(?:\{[ ]?\}|\[[ ]?\]|" + _json_string() + "|" + _json_number() + r"|true|false|null)"
    raise StructuredOutputError(f"unsupported JSON schema type: {schema_type}")


# ----------------------------------------------------------------------
# EBNF -> regex (bounded expansion)
# ----------------------------------------------------------------------
class EBNFParser:
    def __init__(self, text: str) -> None:
        text = re.sub(r"\(\*.*?\*\)", "", text, flags=re.S)
        self.tokens = re.findall(
            r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[A-Za-z_][A-Za-z0-9_]*|[=|;(){}[\]]|\S',
            text,
        )
        self.pos = 0
        self.rules: dict[str, Any] = {}

    def _peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _take(self) -> str:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def parse(self) -> dict[str, Any]:
        while self._peek() is not None:
            name = self._take()
            if self._peek() != "=":
                raise StructuredOutputError(f"expected '=' after {name}")
            self._take()
            expr = self._parse_alt()
            if self._peek() == ";":
                self._take()
            self.rules[name] = expr
        if not self.rules:
            raise StructuredOutputError("empty EBNF grammar")
        return self.rules

    def _parse_alt(self):
        left = self._parse_seq()
        while self._peek() == "|":
            self._take()
            left = ("alt", left, self._parse_seq())
        return left

    def _parse_seq(self):
        items = []
        while self._peek() not in (None, "|", ")", "]", "}", ";"):
            items.append(self._parse_factor())
        if not items:
            return ("empty",)
        result = items[0]
        for item in items[1:]:
            result = ("seq", result, item)
        return result

    def _parse_factor(self):
        token = self._peek()
        if token in ("{", "["):
            self._take()
            inner = self._parse_alt()
            close = "}" if token == "{" else "]"
            if self._peek() != close:
                raise StructuredOutputError("unterminated EBNF group")
            self._take()
            return ("star" if token == "{" else "optional", inner)
        atom = self._parse_atom()
        return atom

    def _parse_atom(self):
        token = self._take()
        if token == "(":
            inner = self._parse_alt()
            if self._peek() != ")":
                raise StructuredOutputError("unterminated EBNF parenthesis")
            self._take()
            return inner
        if token.startswith('"') or token.startswith("'"):
            return ("literal", token[1:-1])
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token):
            if token in ("true", "false", "null"):
                return ("literal", token)
            return ("ref", token)
        return ("literal", token)


def _expand_ebnf(expr, rules: dict[str, Any], depth: int, limit: int = 20) -> str:
    if depth > limit:
        raise StructuredOutputError("recursive EBNF grammar exceeds the expansion limit")
    kind = expr[0]
    if kind == "empty":
        return ""
    if kind == "literal":
        return _escape_literal(expr[1])
    if kind == "ref":
        if expr[1] not in rules:
            raise StructuredOutputError(f"unknown EBNF rule: {expr[1]}")
        return "(?:" + _expand_ebnf(rules[expr[1]], rules, depth + 1, limit) + ")"
    if kind == "seq":
        return _expand_ebnf(expr[1], rules, depth, limit) + _expand_ebnf(expr[2], rules, depth, limit)
    if kind == "alt":
        return "(?:" + _expand_ebnf(expr[1], rules, depth, limit) + "|" + _expand_ebnf(expr[2], rules, depth, limit) + ")"
    if kind == "star":
        return "(?:" + _expand_ebnf(expr[1], rules, depth, limit) + ")*"
    if kind == "optional":
        return "(?:" + _expand_ebnf(expr[1], rules, depth, limit) + ")?"
    raise StructuredOutputError(f"unknown EBNF node: {kind}")


def compile_ebnf(text: str) -> NFAMatcher:
    parser = EBNFParser(text)
    rules = parser.parse()
    start = "start" if "start" in rules else next(iter(rules))
    return compile_regex("(?:" + _expand_ebnf(("ref", start), rules, 0) + ")")


# ----------------------------------------------------------------------
# Public matcher with trigger/bound support
# ----------------------------------------------------------------------
def _kmp_failure(sequence: list[int]) -> list[int]:
    failure = [0] * len(sequence)
    k = 0
    for i in range(1, len(sequence)):
        while k and sequence[i] != sequence[k]:
            k = failure[k - 1]
        if sequence[i] == sequence[k]:
            k += 1
        failure[i] = k
    return failure


@dataclass
class StructuredOutputMatcher:
    grammar: NFAMatcher
    eos_token_id: int | None
    vocab_size: int
    trigger: list[int] = field(default_factory=list)
    bound_token_id: int | None = None
    trigger_progress: int = 0
    tokens_after_bound: int = -1
    finished: bool = False

    def __post_init__(self) -> None:
        self.failure = _kmp_failure(self.trigger) if self.trigger else []
        self.active = not bool(self.trigger)
        self.aborted = False

    # -- trigger -------------------------------------------------------
    def _advance_trigger(self, token: int) -> None:
        if self.bound_token_id is not None and self.tokens_after_bound >= 0:
            if self.trigger_progress < len(self.trigger) and token == self.trigger[self.trigger_progress]:
                self.trigger_progress += 1
                self.tokens_after_bound += 1
                if self.trigger_progress == len(self.trigger):
                    self.active = True
            else:
                self.aborted = True
            return
        progress = self.trigger_progress
        while progress and token != self.trigger[progress]:
            progress = self.failure[progress - 1]
        if token == self.trigger[progress]:
            progress += 1
        self.trigger_progress = progress
        if self.bound_token_id is not None and token == self.bound_token_id:
            self.tokens_after_bound = 0
        if self.trigger_progress == len(self.trigger):
            self.active = True

    # -- mask/accept ---------------------------------------------------
    def allowed_mask(self):
        import numpy as np

        if self.aborted:
            mask = np.zeros(self.vocab_size, dtype=bool)
            return mask
        if not self.active:
            mask = np.ones(self.vocab_size, dtype=bool)
            mask[256:] = False
            if self.eos_token_id is not None:
                mask[self.eos_token_id] = True
            if self.bound_token_id is not None and self.tokens_after_bound >= 0:
                if self.trigger_progress < len(self.trigger):
                    mask[:] = False
                    mask[self.trigger[self.trigger_progress]] = True
            return mask
        return self.grammar.allowed_mask(self.vocab_size, self.eos_token_id)

    def accept_token(self, token_id: int) -> bool:
        if self.aborted:
            return False
        if not self.active:
            if self.eos_token_id is not None and token_id == self.eos_token_id:
                self.finished = True
                return True
            if token_id >= 256:
                self.aborted = True
                return False
            self._advance_trigger(token_id)
            return not self.aborted
        if self.grammar.accept_token(token_id, self.eos_token_id):
            self.finished = self.grammar.finished
            return True
        self.aborted = True
        return False

    def snapshot(self):
        return (
            self.grammar.snapshot(),
            self.active,
            self.trigger_progress,
            self.tokens_after_bound,
            self.finished,
            self.aborted,
        )

    def restore(self, snap) -> None:
        grammar_state, active, progress, after_bound, finished, aborted = snap
        self.grammar.restore(grammar_state)
        self.active = active
        self.trigger_progress = progress
        self.tokens_after_bound = after_bound
        self.finished = finished
        self.aborted = aborted


def build_matcher(
    so_args: dict[str, Any] | None,
    *,
    eos_token_id: int | None,
    vocab_size: int,
    trigger_token_ids: tuple[int, ...] = (),
    bound_token_id: int | None = None,
) -> StructuredOutputMatcher | None:
    if not so_args:
        return None
    schema_type = so_args.get("schema_type")
    schema = so_args.get("schema")
    if schema_type == "regex":
        grammar = compile_regex(str(schema))
    elif schema_type == "json":
        schema_obj = json.loads(schema) if isinstance(schema, str) else schema
        grammar = compile_regex(_schema_to_regex(schema_obj))
    elif schema_type == "ebnf":
        grammar = compile_ebnf(str(schema))
    else:
        raise StructuredOutputError(f"unknown schema_type: {schema_type}")
    return StructuredOutputMatcher(
        grammar=grammar,
        eos_token_id=eos_token_id,
        vocab_size=vocab_size,
        trigger=list(trigger_token_ids),
        bound_token_id=bound_token_id,
    )

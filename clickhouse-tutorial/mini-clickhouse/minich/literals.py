"""字面量解析 —— 把**文本形式**的值解析成 Python 值。

这份代码有两个使用者,而且它们的需求几乎一样:

1. **数据类型**:``Array(UInt8)`` 的 ``deserialize_text("[1,2,3]")``;
2. **SQL 解析器**:``VALUES`` 子句里的 ``[1,2,3]``、``(1,'a')``、``{'k':1}``。

所以把它抽成一个独立模块,而不是塞进任何一边 —— 这也保证了
"``INSERT ... VALUES`` 能写的东西"和"``SELECT ... FORMAT Values``
能吐出来的东西"是**同一套语法**(格式往返测试的基础,见第 17 章)。

支持的语法(全部与真实 ClickHouse 一致):

.. code-block:: text

   123          -1        0x1f        1e-3        1.5
   'abc'        'a\\'b'    'a\\nb'      '\\x41'
   NULL         true       false
   [1, 2, 3]    []         [[1,2],[3]]
   (1, 'a')     ()                       ← 元组
   {'k': 1}     {}                       ← Map
"""

from __future__ import annotations

import re
from typing import Any, List, Optional

from .errors import ErrorCode, raise_error

__all__ = ["parse_literal", "split_top_level", "parse_number", "TokenStream"]


def split_top_level(text: str, sep: str = ",") -> List[str]:
    """按**顶层**分隔符切分,忽略引号与嵌套括号里的分隔符。

    ``split_top_level("Enum8('a,b' = 1, 'c' = 2)")`` 之类的场景都靠它。
    """
    out: List[str] = []
    depth = 0
    quote: Optional[str] = None
    cur: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            cur.append(ch)
            if ch == "\\" and i + 1 < n:
                cur.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
            cur.append(ch)
        elif ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            depth -= 1
            cur.append(ch)
        elif ch == sep and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    tail = "".join(cur).strip()
    if tail or out:
        out.append(tail)
    return out


_NUMBER_RE = re.compile(
    r"^[+-]?(?:0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)$"
)


def parse_number(text: str) -> Any:
    """把一段数字文本解析成 int 或 float。

    规则与真实 ClickHouse 的 ``readIntText``/``readFloatText`` 对齐:
    有 ``.`` 或 ``e`` 就是浮点,``0x`` 前缀是十六进制整数。
    """
    s = text.strip()
    if s.lower().startswith(("0x", "-0x", "+0x")):
        return int(s, 16)
    if "." in s or "e" in s.lower():
        return float(s)
    try:
        return int(s)
    except ValueError:
        raise_error(ErrorCode.CANNOT_PARSE_NUMBER, f"Cannot parse number from {text!r}")


_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "0": "\0", "b": "\b", "f": "\f",
    "a": "\a", "v": "\v", "\\": "\\", "'": "'", '"': '"', "`": "`",
}


class TokenStream:
    """一个极简的字符流 —— 给 :func:`parse_literal` 做递归下降用。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def eof(self) -> bool:
        return self.pos >= len(self.text)

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def next(self) -> str:
        ch = self.peek()
        self.pos += 1
        return ch

    def skip_ws(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def expect(self, ch: str) -> None:
        self.skip_ws()
        if self.peek() != ch:
            raise_error(
                ErrorCode.CANNOT_PARSE_TEXT,
                f"Expected {ch!r} at position {self.pos} in {self.text!r}",
            )
        self.pos += 1

    def read_quoted(self) -> str:
        """读一个引号字符串(**带转义**)—— 返回解码后的内容。"""
        quote = self.next()
        out: List[str] = []
        while True:
            if self.eof():
                raise_error(
                    ErrorCode.CANNOT_PARSE_QUOTED_STRING,
                    f"Unterminated string literal in {self.text!r}",
                )
            ch = self.next()
            if ch == "\\":
                nxt = self.next()
                if nxt in _ESCAPES:
                    out.append(_ESCAPES[nxt])
                elif nxt == "x":
                    hexs = self.text[self.pos : self.pos + 2]
                    if len(hexs) == 2 and all(c in "0123456789abcdefABCDEF" for c in hexs):
                        out.append(chr(int(hexs, 16)))
                        self.pos += 2
                    else:
                        out.append("\\x")
                else:
                    # 未识别的转义:保留反斜杠(与真实 CH 一致)
                    out.append("\\")
                    out.append(nxt)
            elif ch == quote:
                return "".join(out)
            else:
                out.append(ch)

    def read_bare(self, stops: str) -> str:
        """读一段裸 token,遇到 ``stops`` 里的字符或空白就停。"""
        self.skip_ws()
        start = self.pos
        while self.pos < len(self.text):
            ch = self.text[self.pos]
            if ch in stops or ch.isspace():
                break
            self.pos += 1
        return self.text[start : self.pos]


def parse_literal(text: str) -> Any:
    """解析一个**完整的**字面量文本。

    >>> parse_literal("[1, 'a', NULL]")
    [1, 'a', None]
    >>> parse_literal("{'k': 1}")
    {'k': 1}
    >>> parse_literal("'it\\'s'")
    "it's"

    解析失败会**原样返回字符串** —— 因为很多场景(CSV 的一列、
    一个未加引号的标识符)本来就该当成字符串。调用方(带类型的
    ``deserialize_text``)会在拿到字符串后再用**类型**去校验它。
    """
    s = text.strip()
    if s == "":
        return ""
    if s == "NULL" or s == "\\N" or s == "null":
        return None
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    if s[0] in "'\"":
        ts = TokenStream(s)
        try:
            val = ts.read_quoted()
            if not ts.eof():
                # 引号后面还有东西 → 不是纯字面量(比如 'a' = 1 的枚举定义)
                return s
            return val
        except Exception:
            return s
    if s[0] == "[":
        return _parse_list(s, "[", "]")
    if s[0] == "(":
        return tuple(_parse_list(s, "(", ")"))
    if s[0] == "{":
        return _parse_map(s)
    if _NUMBER_RE.match(s):
        return parse_number(s)
    return s


def _parse_list(s: str, open_ch: str = "[", close_ch: str = "]") -> List[Any]:
    """解析 ``[...]``(数组)或 ``(...)``(元组)。

    **两种括号共用一份实现**,因为它们在语法上完全同构 ——
    区别只在解析完之后怎么用(数组 → ``list``,元组 → ``tuple``)。
    这个"同构"也解释了 ClickHouse 里 ``Array`` 与 ``Tuple`` 的
    许多相似之处(它们的内存布局都是"若干子列 + 偏移信息")。
    """
    ts = TokenStream(s)
    ts.expect(open_ch)
    out: List[Any] = []
    ts.skip_ws()
    if ts.peek() == close_ch:
        ts.pos += 1
        return out
    while True:
        out.append(_parse_element(ts, close_ch))
        ts.skip_ws()
        ch = ts.peek()
        if ch == ",":
            ts.pos += 1
            continue
        if ch == close_ch:
            ts.pos += 1
            return out
        raise_error(
            ErrorCode.CANNOT_PARSE_TEXT,
            f"Expected ',' or {close_ch!r} at position {ts.pos} in {s!r}",
        )


def _parse_element(ts: TokenStream, closing: str) -> Any:
    """解析数组/元组里的一个元素(递归)。

    元素可以是嵌套结构、引号串、或裸 token(数字/标识符)。
    裸 token 用"最长裸读"取出来,再交给 :func:`parse_literal` 判定。
    """
    ts.skip_ws()
    ch = ts.peek()
    if ch == "[":
        start = ts.pos
        _skip_balanced(ts, "[", "]")
        return parse_literal(ts.text[start : ts.pos])
    if ch == "(":
        start = ts.pos
        _skip_balanced(ts, "(", ")")
        return parse_literal(ts.text[start : ts.pos])
    if ch == "{":
        start = ts.pos
        _skip_balanced(ts, "{", "}")
        return parse_literal(ts.text[start : ts.pos])
    if ch in "'\"":
        return ts.read_quoted()
    token = ts.read_bare("," + closing)
    return parse_literal(token) if token else None


def _skip_balanced(ts: TokenStream, open_ch: str, close_ch: str) -> None:
    """从当前位置跳过一对配平的括号(含引号内的括号)。"""
    ts.expect(open_ch)
    depth = 1
    while depth > 0:
        if ts.eof():
            raise_error(
                ErrorCode.CANNOT_PARSE_TEXT,
                f"Unbalanced {open_ch}{close_ch} in {ts.text!r}",
            )
        ch = ts.next()
        if ch in "'\"":
            quote = ch
            while not ts.eof():
                c = ts.next()
                if c == "\\":
                    ts.next()
                elif c == quote:
                    break
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1


def _parse_map(s: str) -> dict:
    """解析 ``{'k': v, ...}``。"""
    ts = TokenStream(s)
    ts.expect("{")
    out: dict = {}
    ts.skip_ws()
    if ts.peek() == "}":
        ts.pos += 1
        return out
    while True:
        key = _parse_element(ts, "}")
        ts.skip_ws()
        if ts.peek() != ":":
            raise_error(
                ErrorCode.CANNOT_PARSE_TEXT,
                f"Expected ':' in map literal at position {ts.pos} in {s!r}",
            )
        ts.pos += 1
        value = _parse_element(ts, "}")
        out[key if not isinstance(key, list) else tuple(key)] = value
        ts.skip_ws()
        ch = ts.peek()
        if ch == ",":
            ts.pos += 1
            continue
        if ch == "}":
            ts.pos += 1
            return out
        raise_error(
            ErrorCode.CANNOT_PARSE_TEXT,
            f"Expected ',' or '}}' at position {ts.pos} in {s!r}",
        )

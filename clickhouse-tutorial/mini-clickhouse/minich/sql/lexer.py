"""词法分析 —— SQL 文本 → Token 流。

ClickHouse 的解析器是**手写的递归下降**(``src/Parsers/Parser*.cpp``),
不是 yacc/bison 生成的。这个选择值得说明,因为它直接决定了你能写出什么样的 SQL:

.. code-block:: text

   手写递归下降的好处
     * 错误消息可以**精确到位置**,还能带上"你是不是想写 xxx"的建议;
     * 可以支持"上下文相关的语法"(同一个词在不同位置含义不同);
     * 解析器可以**中途停下来**——`INSERT ... FORMAT CSV` 后面的数据
       交给格式解析器读,而不是当成 SQL 继续解析。这在 LALR 里很难做。

   代价是
     * 语法有歧义时得靠**回溯**(mini-clickhouse 里就是 `_try_parse`)解决,
       代价可能是指数级的 —— ClickHouse 用 `max_parser_depth` 兜底。

.. note::

   **mini-clickhouse 不把关键字做成独立的 token 类型。** 这是有意的:
   ClickHouse 允许大量关键字当标识符用(``SELECT key FROM t``、
   ``SELECT table FROM t`` 都是合法的),把它们严格分类反而会误报错误。
   我们的做法是:标识符 token 带一个 ``upper`` 字段,由解析器**按位置**
   决定"这里这个词是关键字还是名字"。真实 ClickHouse 也用类似的手法
   (``ParserKeyword``),只是它更严格一些。

   一个后果要说明:我们**不**支持 ``SELECT` `\\`a b\\```以外的
   怪异标识符写法(比如 MySQL 的反引号嵌套),因为真实 ClickHouse 也不支持。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterator, List, Optional

from ..errors import ErrorCode, raise_error

__all__ = [
    "Token", "tokenize", "iter_tokens", "TokenKind", "KEYWORDS",
    "RESERVED_FOR_IDENTIFIER",
]


class TokenKind:
    """Token 类型 —— 数量刻意很少(见模块开头的说明)。"""

    IDENT = "ident"
    NUMBER = "number"
    STRING = "string"
    PUNCT = "punct"
    PARAM = "param"
    EOF = "eof"


@dataclass
class Token:
    """一个词法单元。

    ``pos`` 是**起始字符偏移**(从 0 开始),报错时用它来画
    ``Syntax error at position 42`` 这类消息。``line``/``col``
    是从 ``pos`` 反推出来的,给人类看。
    """

    kind: str
    value: Any
    pos: int
    line: int = 1
    col: int = 1
    quoted: bool = False

    @property
    def upper(self) -> str:
        """标识符的大写形式 —— 关键字判定用(``select``/``SELECT`` 都能匹配)。"""
        if isinstance(self.value, str):
            return self.value.upper()
        return ""

    def is_keyword(self, *words: str) -> bool:
        """当前 token 是否是给定的某个关键字(大小写无关)。"""
        if self.kind not in (TokenKind.IDENT, TokenKind.PUNCT):
            return False
        u = self.upper if self.kind == TokenKind.IDENT else str(self.value)
        return u in (w.upper() for w in words)

    def is_punct(self, *chars: str) -> bool:
        return self.kind == TokenKind.PUNCT and self.value in chars

    def __repr__(self) -> str:  # pragma: no cover
        return f"Token({self.kind}, {self.value!r}, pos={self.pos})"


#: ClickHouse 的关键字全集(用于"这个标识符要不要加反引号"的判断,
#: 以及 ``system.keywords`` 表)。这里列的是会**影响解析**的那些。
KEYWORDS = frozenset({
    "SELECT", "FROM", "WHERE", "PREWHERE", "GROUP", "BY", "HAVING", "ORDER",
    "LIMIT", "OFFSET", "WITH", "AS", "DISTINCT", "ALL", "UNION", "JOIN",
    "LEFT", "RIGHT", "INNER", "FULL", "OUTER", "CROSS", "ON", "USING",
    "ARRAY", "FINAL", "SAMPLE", "SETTINGS", "FORMAT", "INTO", "OUTFILE",
    "INSERT", "VALUES", "CREATE", "TABLE", "DATABASE", "IF", "NOT", "EXISTS",
    "DROP", "ALTER", "RENAME", "TRUNCATE", "OPTIMIZE", "SHOW", "DESCRIBE",
    "DESC", "EXPLAIN", "SYSTEM", "SET", "USE", "KILL", "GRANT", "REVOKE",
    "AND", "OR", "IN", "BETWEEN", "IS", "NULL", "LIKE", "ILIKE", "REGEXP",
    "CASE", "WHEN", "THEN", "ELSE", "END", "CAST", "INTERVAL", "ASC", "NULLS",
    "FIRST", "LAST", "TOTALS", "ROLLUP", "CUBE", "GROUPING", "SETS", "WINDOW",
    "OVER", "PARTITION", "ROWS", "RANGE", "UNBOUNDED", "PRECEDING", "FOLLOWING",
    "CURRENT", "ROW", "ENGINE", "PRIMARY", "KEY", "TTL", "CODEC", "INDEX",
    "TYPE", "GRANULARITY", "CONSTRAINT", "CHECK", "PROJECTION", "DEFAULT",
    "MATERIALIZED", "ALIAS", "EPHEMERAL", "COMMENT", "TEMPORARY", "ATTACH",
    "DETACH", "MOVE", "FREEZE", "FETCH", "CLEAR", "MODIFY", "ADD", "COLUMN",
    "SYNC", "POPULATE", "TO", "FUNCTION", "DICTIONARY", "SOURCE", "LAYOUT",
    "LIFETIME", "USER", "ROLE", "QUOTA", "PROFILE", "POLICY", "TRUE", "FALSE",
    "GLOBAL", "ANY", "SEMI", "ANTI", "ASOF", "PASTE", "EXCEPT", "REPLACE",
    "ATOMIC", "ORDINARY", "MEMORY", "TINYLOG", "LOG", "STRIPELOG", "NULL",
    "SETTINGS", "VOLUME", "DISK", "RECOMPRESS", "DELETE", "UPDATE", "WHERE",
})

#: 这些词**无论如何**都不能当标识符(即使不加引号也不行)。
#: 真实 ClickHouse 的这套列表在 ``src/Parsers/Keywords.h`` 里由宏生成。
RESERVED_FOR_IDENTIFIER = frozenset({
    "SELECT", "FROM", "WHERE", "GROUP", "ORDER", "LIMIT", "UNION", "JOIN",
    "INSERT", "VALUES", "CREATE", "DROP", "ALTER",
})

#: 运算符 —— **按长度倒序**匹配,保证 ``<=`` 不会先匹配成 ``<``。
_OPERATORS = [
    "<=>", "->", "::", "||", "<=", ">=", "!=", "<>", "==", "<<", ">>", ":=",
    "+", "-", "*", "/", "%", "=", "<", ">", "(", ")", "[", "]", "{", "}",
    ",", ".", ";", ":", "?", "@", "!", "&", "|", "^", "~",
]

#: 一个合法的"裸标识符"(用于区分查询参数与 Map 字面量)
_IDENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_IDENT_START = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_IDENT_CHARS = _IDENT_START | set("0123456789$")
_DIGITS = set("0123456789")

#: 字符串字面量的转义表 —— 与 :mod:`minich.literals` 共用同一套语义。
_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "0": "\0", "b": "\b", "f": "\f",
    "a": "\a", "v": "\v", "\\": "\\", "'": "'", '"': '"', "`": "`",
}


def iter_tokens(sql: str, pos: int = 0) -> Iterator[Token]:
    """**惰性**版的 :func:`tokenize` —— 要一个 token 才切一个。

    .. warning::

       为什么必须有它:``FORMAT`` **之后**是数据,不是 SQL。而
       :func:`tokenize` 会把整条文本一次切完 —— 于是数据里只要有一个
       词法器不认识的字符(TSV/CSV writer 对任何含 ``\\`` 的值都会写出
       ``\\\\``),就会在**还没走到取 payload 那一步**先报
       ``SYNTAX_ERROR: Unexpected character '\\'``。
       解析器改成"要一个切一个"之后,``FORMAT`` 一到手就停手
       (见 :class:`minich.sql.parser.Parser`)。
    """
    i = pos
    n = len(sql)
    line = sql.count("\n", 0, pos) + 1
    line_start = sql.rfind("\n", 0, pos) + 1

    def _advance_lines(start: int, end: int) -> None:
        """跨过 ``sql[start:end]``,更新行号与"当前行的起点"。

        .. warning::

           起点必须是**绝对偏移**。早期版本写的是
           ``line_start = text.rfind("\\n") + 1`` —— 那是**切片内部**的
           相对位置,一旦这段文本不是从行首开始,后面所有 token 的
           ``col`` 就全偏了。``line`` 仍然是对的,所以这个错误很隐蔽:
           报错信息里的**行号对、列号错**。
        """
        nonlocal line, line_start
        last_nl = -1
        count = 0
        for _idx in range(start, end):
            if sql[_idx] == "\n":
                count += 1
                last_nl = _idx
        if count:
            line += count
            line_start = last_nl + 1

    while i < n:
        ch = sql[i]

        # ── 空白 ────────────────────────────────────────────────────────
        if ch in " \t\r\n":
            j = i
            while j < n and sql[j] in " \t\r\n":
                j += 1
            _advance_lines(i, j)
            i = j
            continue

        # ── 注释 ────────────────────────────────────────────────────────
        if sql.startswith("--", i) or (ch == "#" and not sql.startswith("#!", i)):
            j = sql.find("\n", i)
            if j < 0:
                j = n
            _advance_lines(i, j)
            i = j
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j < 0:
                raise_error(
                    ErrorCode.SYNTAX_ERROR,
                    f"Unterminated block comment starting at position {i}",
                )
            _advance_lines(i, j + 2)
            i = j + 2
            continue

        # ── 查询参数 {name:Type} ────────────────────────────────────────
        if ch == "{":
            close = sql.find("}", i)
            if close > 0:
                body = sql[i + 1 : close]
                if ":" in body:
                    pname, _, ptype = body.partition(":")
                    pname, ptype = pname.strip(), ptype.strip()
                    # ``{name:Type}`` 与 Map 字面量 ``{'a': 1}`` 都以 ``{`` 开头,
                    # 靠"冒号前面那一截是不是**裸标识符**"来区分。
                    # 只查"里面有没有冒号"会把 ``{'a': 1}`` 整块当成查询参数吞掉,
                    # 于是 ``SELECT map('a',1) = {'a': 1}`` 之类的写法
                    # 会报一个和实际原因毫无关系的错。
                    if pname and ptype and _IDENT_NAME_RE.match(pname):
                        yield Token(TokenKind.PARAM, (pname, ptype), i,
                                    line, i - line_start + 1)
                        i = close + 1
                        continue

        # ── 字符串 ──────────────────────────────────────────────────────
        if ch in "'\"":
            quote = ch
            j = i + 1
            out: List[str] = []
            while True:
                if j >= n:
                    raise_error(
                        ErrorCode.CANNOT_PARSE_QUOTED_STRING,
                        f"Unterminated string literal starting at position {i}",
                    )
                c = sql[j]
                if c == "\\":
                    if j + 1 >= n:
                        raise_error(
                            ErrorCode.CANNOT_PARSE_ESCAPE_SEQUENCE,
                            f"Dangling escape at position {j}",
                        )
                    nxt = sql[j + 1]
                    if nxt in _ESCAPES:
                        out.append(_ESCAPES[nxt])
                        j += 2
                    elif nxt == "x":
                        hexs = sql[j + 2 : j + 4]
                        if len(hexs) == 2 and all(c2 in "0123456789abcdefABCDEF" for c2 in hexs):
                            out.append(chr(int(hexs, 16)))
                            j += 4
                        else:
                            out.append("\\x")
                            j += 2
                    else:
                        # 未识别的转义:保留反斜杠(与真实 ClickHouse 一致)
                        out.append("\\")
                        out.append(nxt)
                        j += 2
                    continue
                if c == quote:
                    j += 1
                    break
                # 两个连续的同名引号也是转义(MySQL 风格,ClickHouse 也接受)
                if c == quote:
                    j += 1
                    break
                out.append(c)
                j += 1
            text = "".join(out)
            yield Token(TokenKind.STRING, text, i, line, i - line_start + 1)
            _advance_lines(i, j)
            i = j
            continue

        # ── 反引号 / 双引号标识符 ───────────────────────────────────────
        if ch == "`":
            j = i + 1
            out = []
            while True:
                if j >= n:
                    raise_error(
                        ErrorCode.SYNTAX_ERROR,
                        f"Unterminated quoted identifier starting at position {i}",
                    )
                c = sql[j]
                if c == "\\" and j + 1 < n:
                    out.append(sql[j + 1])
                    j += 2
                    continue
                if c == "`":
                    j += 1
                    break
                out.append(c)
                j += 1
            yield Token(TokenKind.IDENT, "".join(out), i, line, i - line_start + 1, quoted=True)
            i = j
            continue

        # ── 数字 ────────────────────────────────────────────────────────
        # `.5` 这种"前导点小数"只有在**紧左边不是标识符字符或右括号**时才算数字。
        # 否则 `t.1`(元组元素访问)会被误切成一个浮点数 0.1 —— 这正是一个
        # 真实的踩坑点:词法分析器必须靠"前一个字符"来消歧,而不能只看自己。
        # (`t.1` 的前一个字符是 `t`;`SELECT .5` 的前一个字符是空格。)
        #: 前一个字符(输入开头时视为"不可能跟在标识符后面")。
        #:
        #: .. warning::
        #:
        #:    这里**必须**显式判 ``i > 0``。早期版本写成
        #:    ``sql[i-1] if i > 0 else ""`` 然后测 ``"" in _IDENT_CHARS`` ——
        #:    而 Python 里**空字符串是任何字符串的子串**,那个判断恒为真,
        #:    于是输入开头的 ``.5`` 被判成"跟在标识符后面",
        #:    降级成了一个裸的 ``.`` 标点。``SELECT .5`` 直接语法错误。
        _prev_char = sql[i - 1] if i > 0 else None
        _after_ident = _prev_char is not None and (
            _prev_char in _IDENT_CHARS or _prev_char in ")]}"
        )
        if ch in _DIGITS or (
            ch == "."
            and i + 1 < n
            and sql[i + 1] in _DIGITS
            and not _after_ident
        ):
            j = i
            is_float = False
            if sql.startswith(("0x", "0X"), i):
                j = i + 2
                while j < n and sql[j] in "0123456789abcdefABCDEF":
                    j += 1
                if j == i + 2:
                    raise_error(
                        ErrorCode.SYNTAX_ERROR,
                        f"Malformed hexadecimal literal at position {i}",
                    )
                yield Token(TokenKind.NUMBER, int(sql[i:j], 16), i, line, i - line_start + 1)
                i = j
                continue
            while j < n and sql[j] in _DIGITS:
                j += 1
            if j < n and sql[j] == ".":
                # 小心 `t.1`(元组访问)与 `.5`(小数)—— 只有在点后面还是数字时才是小数
                if j + 1 < n and sql[j + 1] in _DIGITS:
                    is_float = True
                    j += 1
                    while j < n and sql[j] in _DIGITS:
                        j += 1
                elif j == i:
                    is_float = True
                    j += 1
                    while j < n and sql[j] in _DIGITS:
                        j += 1
            if j < n and sql[j] in "eE":
                k = j + 1
                if k < n and sql[k] in "+-":
                    k += 1
                if k < n and sql[k] in _DIGITS:
                    is_float = True
                    j = k
                    while j < n and sql[j] in _DIGITS:
                        j += 1
            raw = sql[i:j]
            value: Any = float(raw) if is_float else int(raw)
            yield Token(TokenKind.NUMBER, value, i, line, i - line_start + 1)
            i = j
            continue

        # ── 标识符 / 关键字 ─────────────────────────────────────────────
        if ch in _IDENT_START:
            j = i
            while j < n and sql[j] in _IDENT_CHARS:
                j += 1
            yield Token(TokenKind.IDENT, sql[i:j], i, line, i - line_start + 1)
            i = j
            continue

        # ── 运算符 / 标点 ───────────────────────────────────────────────
        for op in _OPERATORS:
            if sql.startswith(op, i):
                yield Token(TokenKind.PUNCT, op, i, line, i - line_start + 1)
                i += len(op)
                break
        else:
            raise_error(
                ErrorCode.SYNTAX_ERROR,
                f"Unexpected character {ch!r} at position {i} (line {line})",
            )

    yield Token(TokenKind.EOF, None, n, line, n - line_start + 1)


def tokenize(sql: str, pos: int = 0) -> List[Token]:
    """把 SQL 文本切成 Token 列表(末尾一定有一个 ``EOF``)。

    ``pos`` 参数允许"从中间开始切" —— ``INSERT ... FORMAT CSV`` 之后
    的数据部分就是这样交给格式解析器的(第 17 章)。

    "切分点在哪儿"由 :func:`iter_tokens` 决定 —— 这里只是把它跑完。
    解析器不用这个函数,它要的是**按需**切:
    整条文本一次切完会让 ``FORMAT`` 之后的数据也过一次词法分析。
    """
    return list(iter_tokens(sql, pos))

"""文本转义与标识符处理 —— 格式输出、SQL 生成、错误消息共用。

转义看着琐碎,但它决定了**数据能不能无损往返**。
第 17 章有一节专门讲"为什么 CSV 不可能无损"(因为它没有类型信息,
``'1'`` 与 ``1`` 输出成同一个字节串),而 TSV + 转义是 ClickHouse
选择的默认格式 —— 恰恰就因为它是可往返的。

.. code-block:: text

   值               TSV 表示            说明
   a\\tb            a\\\\tb              制表符要转义(它是分隔符)
   a\\nb            a\\\\nb              换行要转义(它是行分隔符)
   a\\\\b           a\\\\\\\\b           反斜杠自己也要转义
   \\x00             \\0                  NUL 用 \\0
   NULL 值          \\N                   这不是转义,是一个**哨兵**
   ""(空串)        (空)                 空串与 NULL 必须能区分开

注意最后一对:空串输出为空、NULL 输出为 ``\\N``。如果转义做错了这两者
就会混在一起 —— 这正是很多"从 CSV 导入数据后发现一堆空串变成 NULL"
事故的根因。
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional

__all__ = [
    "escape_tsv",
    "unescape_tsv",
    "escape_csv_field",
    "parse_csv_line",
    "escape_sql_string",
    "quote_identifier",
    "strip_identifier_quotes",
    "indent",
    "wrap_text",
    "format_readable_size",
]

_TSV_ESCAPES = [
    ("\\", "\\\\"),
    ("\t", "\\t"),
    ("\n", "\\n"),
    ("\r", "\\r"),
    ("\0", "\\0"),
    ("\b", "\\b"),
    ("\f", "\\f"),
    # .. warning::
    #
    #    ``\v``(垂直制表)曾经漏在这一列里 —— 而 :data:`_TSV_UNESCAPES`
    #    早就认得 ``\v``,也就是说这里**本来就不对称**。
    #    后果比"少转义一个字符"严重:``\x0b`` 是 Python ``str.strip()``
    #    眼里的**空白**,于是裸写出去的值会被读取端的
    #    ``parse_rendered_value`` 直接 strip 掉 —— 往返丢字符。
    #    转义成 ``\v`` 之后,它就不再是"两端空白"了(与
    #    ``test_string_with_surrounding_spaces_roundtrip_tsv`` 记下的
    #    那个已 xfail 的缺陷同源,但这一条不需要动 strip)。
    ("\v", "\\v"),
]

_TSV_UNESCAPES = {
    "\\": "\\", "t": "\t", "n": "\n", "r": "\r", "0": "\0",
    "b": "\b", "f": "\f", "a": "\a", "v": "\v", "'": "'", '"': '"',
}


def escape_tsv(text: str) -> str:
    """转义一个 TSV 字段。

    .. note::

       真实 ClickHouse 的 ``TSV`` 默认**只转义** ``\\t \\n \\\\ \\0`` 这几个,
       而 ``\\r`` 在 ``output_format_tsv_write_crlf`` 之外的场景会被
       原样写出。我们统一转义,更安全,且能被自己的解析器无损读回。
    """
    out = text
    for raw, esc in _TSV_ESCAPES:
        out = out.replace(raw, esc)
    return out


def unescape_tsv(text: str) -> str:
    """``escape_tsv`` 的逆操作。"""
    if "\\" not in text:
        return text
    out: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            nxt = text[i + 1]
            if nxt in _TSV_UNESCAPES:
                out.append(_TSV_UNESCAPES[nxt])
                i += 2
                continue
            if nxt == "x" and i + 3 < n:
                try:
                    out.append(chr(int(text[i + 2 : i + 4], 16)))
                    i += 4
                    continue
                except ValueError:
                    pass
        out.append(ch)
        i += 1
    return "".join(out)


def escape_csv_field(text: str, delimiter: str = ",") -> str:
    """CSV 字段转义 —— 只在**需要时**加引号(与 RFC 4180 一致)。

    ``csv`` 模块的 ``QUOTE_MINIMAL`` 就是这套规则,但自己写一遍
    能让"什么时候加引号"这件事显式可见(读者常问:
    "为什么我的 CSV 里有的字段有引号、有的没有")。
    """
    needs_quote = (
        delimiter in text
        or '"' in text
        or "\n" in text
        or "\r" in text
        or text != text.strip()
        or text == ""
    )
    if not needs_quote:
        return text
    return '"' + text.replace('"', '""') + '"'


def parse_csv_line(line: str, delimiter: str = ",") -> List[str]:
    """解析一行 CSV —— 支持引号包裹与 ``""`` 转义。"""
    out: List[str] = []
    cur: List[str] = []
    in_quotes = False
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if in_quotes:
            if ch == '"':
                if i + 1 < n and line[i + 1] == '"':
                    cur.append('"')
                    i += 2
                    continue
                in_quotes = False
                i += 1
                continue
            cur.append(ch)
            i += 1
            continue
        if ch == '"':
            in_quotes = True
            i += 1
            continue
        if ch == delimiter:
            out.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    out.append("".join(cur))
    return out


def escape_sql_string(text: str) -> str:
    """把字符串转义成 SQL 字面量内容(不含外层引号)。"""
    return text.replace("\\", "\\\\").replace("'", "\\'")


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: 需要用反引号包起来的保留字 —— 真实 ClickHouse 的解析器认得更多,
#: 这里只列 mini-clickhouse 会歧义的那些。
RESERVED_WORDS = {
    "select", "from", "where", "group", "order", "by", "having", "limit",
    "join", "on", "using", "as", "with", "union", "all", "distinct", "insert",
    "into", "values", "create", "table", "drop", "alter", "index", "key",
    "primary", "default", "engine", "settings", "format", "left", "right",
    "inner", "full", "cross", "array", "tuple", "map", "date", "system",
}


def quote_identifier(name: str) -> str:
    """给标识符按需加反引号(``SHOW CREATE TABLE`` 用)。"""
    if _IDENT_RE.match(name) and name.lower() not in RESERVED_WORDS:
        return name
    return "`" + name.replace("`", "\\`") + "`"


def strip_identifier_quotes(name: str) -> str:
    """剥掉标识符外层的反引号/双引号。"""
    s = name.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "`\"":
        return s[1:-1]
    return s


def indent(text: str, prefix: str = "  ") -> str:
    """给多行文本统一加缩进(解释计划、错误消息用)。"""
    return "\n".join(prefix + line if line else line for line in text.split("\n"))


def wrap_text(text: str, width: int = 100, initial: str = "", subsequent: str = "") -> str:
    """简单的按宽度折行 —— 给 ``ERROR`` 输出与 ``EXPLAIN`` 用。"""
    words = text.split(" ")
    lines: List[str] = []
    cur = initial
    for w in words:
        candidate = (cur + " " + w) if cur.strip() else (cur + w)
        if len(candidate) > width and cur.strip():
            lines.append(cur)
            cur = subsequent + w
        else:
            cur = candidate
    lines.append(cur)
    return "\n".join(lines)


def format_readable_size(n: int) -> str:
    """字节数 → 人类可读(``1.50 MiB``)。``system.parts`` 的展示用。"""
    if n < 0:
        return "-" + format_readable_size(-n)
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    value = float(n)
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(value)} {units[idx]}"
    return f"{value:.2f} {units[idx]}"


def format_readable_quantity(n: float) -> str:
    """大数字的可读形式(``1.23 million``)—— ``system.query_log`` 用。"""
    for limit, suffix in ((1e12, "trillion"), (1e9, "billion"), (1e6, "million"), (1e3, "thousand")):
        if abs(n) >= limit:
            return f"{n / limit:.2f} {suffix}"
    return str(n)

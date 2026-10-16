"""JSON 格式家族 —— ``JSONEachRow`` / ``JSONCompactEachRow`` / ``JSON`` / …

ClickHouse 的 JSON 格式有**两种排布方式**,理解它们的区别比记住格式名重要:

.. code-block:: text

   按行(EachRow 系列):每一行是一个**独立的 JSON 文档**
     {"id":1,"name":"a"}          ← JSONEachRow
     {"id":2,"name":"b"}          ← 换行分隔,可以边算边发(流式)

   整体(JSON 系列):整个结果集是**一个 JSON 文档**
     { "meta": [...], "data": [[1,"a"],[2,"b"]], "rows": 2, ... }

前者适合日志/管道(一行一条,追加即得),后者适合"给前端一个完整对象"。
两者在 ClickHouse 里的名字只差一个后缀,但**流式能力完全不同** ——
``JSON`` 必须等到最后才知道 ``"rows"`` 是多少,所以它的
``write_suffix`` 才有活干。

.. code-block:: text

   格式名                         每行的形状        顶层是否是单个对象
   JSONEachRow                    {"c":v,…}         否(每行一个对象)
   JSONStringsEachRow             {"c":"v",…}       否(所有值都是字符串)
   JSONCompactEachRow             [v,…]             否(没有列名)
   JSONCompactEachRowWithNames    [v,…]  + 首行是 ["c1","c2"]   否
   JSON                      [[v,…],…] 行数组      **是**
   JSONCompact               [[v,…],…] 行数组      **是**(紧凑:不换行不缩进)

.. warning::

   **64 位整数会被输出成字符串**,这是 ClickHouse 最著名的"惊喜"之一:

   .. code-block:: text

      output_format_json_quote_64bit_integers = 1   ← **默认值**
      {"id":"18446744073709551615"}                 ← 带引号!
      ↑ 因为 JavaScript 的 Number 只有 53 位精度,
        18446744073709551615 到了浏览器里会变成 18446744073709552000

   于是 ``typeof x === "number"`` 会失败 —— 无数人在这里浪费过一个下午。
   mini-clickhouse 把这个设置做成**可切换的**,默认值 1 与真实 CH 一致;
   想拿到裸数字就传 ``settings={"output_format_json_quote_64bit_integers": 0}``。
   读取时两种写法都接受(字符串会被按类型解析回数字)。

.. warning::

   **``nan`` / ``inf`` 是"非法 JSON"**。JSON 标准不允许这两个字面量,但
   真实 ClickHouse 默认就照原样写(``output_format_json_quote_denormals``
   默认 0)::

      {"x":nan}                       ← 真实 ClickHouse 的输出
      json.loads('{"x":nan}')         ← Python 会**报错**(它只认 NaN)

   所以这里的读取器先做一遍"把非标准字面量规范化"的预处理,把
   ``nan``/``inf``/``-inf`` 换成 Python 认的 ``NaN``/``Infinity``/``-Infinity``
   再交给 :func:`minich.util.json_text.from_json_text`。这是**为了往返**,
   不是"我们更喜欢大写"。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterator, List

from ..block import Block, ColumnWithTypeAndName
from ..errors import ErrorCode, raise_error
from ..types import DataType, get_data_type
from ..types.scalars import DataTypeBool
from ..util.json_text import (
    format_float_for_json,  # 故意引用未导出的名字:它就是"CH 风格浮点打印"的唯一实现
    json_escape_string,
)
from .base import (
    IInputFormat,
    IOutputFormat,
    block_from_rows,
    chunk_lines,
    get_setting,
    read_all_text,
    require_schema,
    write_text,
)

__all__ = [
    "json_value_text",
    "JSONEachRowOutputFormat",
    "JSONEachRowInputFormat",
    "JSONStringsEachRowOutputFormat",
    "JSONStringsEachRowInputFormat",
    "JSONCompactEachRowOutputFormat",
    "JSONCompactEachRowInputFormat",
    "JSONCompactEachRowWithNamesOutputFormat",
    "JSONCompactEachRowWithNamesInputFormat",
    "JSONOutputFormat",
    "JSONInputFormat",
    "JSONCompactOutputFormat",
    "JSONCompactInputFormat",
]


# ══════════════════════════════════════════════════════════════════════════
# 值 → JSON 文本
# ══════════════════════════════════════════════════════════════════════════


def json_string(text: str) -> str:
    """字符串 → **带引号的** JSON 字符串。

    .. warning::

       ``minich.util.json_text.json_escape_string`` 返回的是**内容**
       (它把外层引号 ``[1:-1]`` 切掉了),所以这里必须自己补上引号。
       第一版实现漏了这一步,输出变成了 ``{id:1}`` —— 不是合法 JSON,
       一眼就该看出来的错误,却被"读回来时才炸"拖到了后面。
       这正是往返测试的价值:它把"看起来对"和"真的对"分开了。
    """
    return '"' + json_escape_string(text) + '"'


def json_value_text(dtype: DataType, value: Any, settings: Any = None, as_string: bool = False) -> str:
    """把一个值渲染成 **JSON** 片段。

    ``as_string=True`` 是 ``JSONStringsEachRow`` 用的一档:所有标量都变成
    JSON 字符串(``1`` → ``"1"``),但**结构不变**(数组还是数组,
    NULL 还是 ``null``)。这样读取端只要按类型 ``normalize``
    就能把 ``"1"`` 变回 ``1``。

    .. code-block:: text

       dtype             value        as_string=False      as_string=True
       UInt8             1            1                    "1"
       UInt64            2**63-1      "9223372036854775807"(默认设置) 同上
       Float64           nan          nan                  "nan"
       Bool              true         true                 "true"
       String            "a\\nb"       "a\\nb"(JSON 转义)   同左
       Date              19723        "2024-01-01"         "2024-01-01"
       Enum8             'info'       "info"               "info"
       Array(UInt8)      [1,2]        [1,2]                ["1","2"]
       Map(String,UInt8) {'k':1}      {"k":1}              {"k":"1"}
       NULL              None         null                 null
    """
    if value is None:
        return "null"
    if dtype.is_nullable():
        return json_value_text(dtype.nested, value, settings, as_string)  # type: ignore[attr-defined]
    if dtype.is_low_cardinality():
        return json_value_text(dtype.nested, value, settings, as_string)  # type: ignore[attr-defined]
    if dtype.is_array():
        inner = ",".join(
            json_value_text(dtype.element, v, settings, as_string)  # type: ignore[attr-defined]
            for v in value
        )
        return "[" + inner + "]"
    if dtype.is_tuple():
        # Tuple 在 JSON 里**没有名字**,只能退化成数组 —— 具名元组也一样,
        # 这是 JSON 格式的信息损失之一(名字留在了 schema 的 meta 里)。
        inner = ",".join(
            json_value_text(t, v, settings, as_string)
            for t, v in zip(dtype.elements, value)  # type: ignore[attr-defined]
        )
        return "[" + inner + "]"
    if dtype.is_map():
        body = ",".join(
            json_string(str(k))
            + ":"
            + json_value_text(dtype.value, v, settings, as_string)  # type: ignore[attr-defined]
            for k, v in value.items()
        )
        return "{" + body + "}"

    # ── 标量 ────────────────────────────────────────────────────────────
    if isinstance(dtype, DataTypeBool):
        text = "true" if value else "false"
        return json_string(text) if as_string else text
    if dtype.is_float():
        text = format_float_for_json(float(value))
        return json_string(text) if as_string else text
    if dtype.is_decimal():
        text = dtype.serialize_text(value, settings) or "0"
        return json_string(text) if as_string else text
    if dtype.is_integer() and not dtype.is_date() and not dtype.is_date_time() and not dtype.is_enum():
        text = str(int(value))
        if as_string:
            # JSONStringsEachRow:数字也写成字符串
            return json_string(text)
        quote_64bit = int(get_setting(settings, "output_format_json_quote_64bit_integers", 1) or 0)
        if quote_64bit and getattr(dtype, "bits", 0) > 32:
            return json_string(text)
        return text
    # 其余(String / 日期 / 枚举 / UUID / IP / Interval / …)一律是 JSON 字符串
    return json_string(dtype.serialize_text(value, settings) or "")


# ══════════════════════════════════════════════════════════════════════════
# JSON 读取:先把"非标准字面量"拉回标准 JSON
# ══════════════════════════════════════════════════════════════════════════

#: 只在"字符串之外、且两侧都是分隔符"的位置匹配 —— 否则 ``"xnan"`` 这种
#: 字符串内容也会被误改(虽然它在字符串里,但扫描时我们跳过字符串,见下)。
_LOOSE_LITERAL = re.compile(r'(?<![\w"])(-?inf(?:inity)?|nan)(?![\w"])', re.IGNORECASE)

_LOOSE_REPLACEMENTS = {
    "nan": "NaN",
    "inf": "Infinity",
    "infinity": "Infinity",
    "-inf": "-Infinity",
    "-infinity": "-Infinity",
}


def _canonical_literal(token: str) -> str:
    """一个"非法 JSON 字面量" → (Python ``json`` 认得的)标准写法。

    .. warning::

       这里**不能**写成 ``_LOOSE_REPLACEMENTS[token.lower()]``:
       上面的正则能匹配出 ``nan`` / ``inf`` / ``infinity`` / ``-inf`` /
       ``-infinity`` 五种形态,而字典一开始只登记了三种(大小写不敏感的
       三种写法而已)—— ``Infinity`` 与 ``-Infinity`` 直接落空,
       抛的是个**裸 ``KeyError``**(不是 ``MiniChError``)。

       更讽刺的是 ``Infinity`` **正是本仓库自己** :func:`minich.util.json_text.to_json_text`
       对 ``float('inf')`` 的输出 —— 也就是"自己写出去、自己读不回来"。
       所以这里用 ``.get`` + 显式报错:字典漏了哪个键,读者看到的是一句
       带错误码的 ``CANNOT_PARSE_TEXT``,而不是一个不知道怎么冒出来的
       ``KeyError``。
    """
    replacement = _LOOSE_REPLACEMENTS.get(token.lower())
    if replacement is None:  # pragma: no cover - 正则与字典应当同步
        raise_error(
            ErrorCode.CANNOT_PARSE_TEXT,
            f"Cannot parse JSON literal {token!r}",
        )
    return replacement


def normalize_json_literals(text: str) -> str:
    """把 ClickHouse 风格的 ``nan`` / ``inf`` / ``-inf`` 换成 Python 认的写法。

    扫描时**跳过字符串字面量**,所以 ``{"s":"inf"}`` 里的 ``inf`` 不会被改。

    .. note::

       真实的 ClickHouse 解析器本身也接受 ``nan``/``inf``(它自己就这么输出)。
       这里做替换纯粹是因为 Python 的 ``json`` 模块不认小写 —— 换成我们自己
       写一个 JSON 解析器不值得。
    """
    out: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            out.append(ch)
            i += 1
            while i < n:
                c = text[i]
                out.append(c)
                i += 1
                if c == "\\" and i < n:
                    out.append(text[i])
                    i += 1
                elif c == '"':
                    break
            continue
        m = _LOOSE_LITERAL.match(text, i)
        if m:
            out.append(_canonical_literal(m.group(0)))
            i = m.end()
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def json_load(text: str) -> Any:
    """宽容版的 JSON 解析(接受 ``nan``/``inf``,而且**数字不丢精度**)。

    .. warning::

       **为什么不用** :func:`minich.util.json_text.from_json_text`?

       因为它内部的 ``json.loads`` 会把数字解析成 Python ``float`` ——
       而 ``float64`` 只有约 15 位十进制有效数字。``Decimal(18, 4)`` 的
       ``99999999999999.9999``(18 位)一旦被转成 float 就变成 ``1e+14``,
       交给 ``Decimal.normalize`` 会直接报 "does not fit" —— 也就是说
       **读取器会拒绝自己写出来的文件**。

       真实 ClickHouse 是按**目标类型**解析数字的(JSON 里的
       ``99999999999999.9999`` 直接进 ``Decimal``,一分不差),
       所以这里用 ``parse_float=str`` **保留数字的原始文本**,再交给
       ``dtype.normalize`` 去解析 —— 效果与真实 ClickHouse 一致:

       .. code-block:: text

          JSON 里的 0.1        →  normalize("0.1")    → Float64  0.1
          JSON 里的 1.5        →  normalize("1.5")    → Int32    1(截断,见 types)
          JSON 里的 1234.5678  →  normalize("1234.5678") → Decimal(18,4) 精确
          JSON 里的 "123"      →  normalize("123")    → UInt64   123(64 位整数的引号形式)

       ``nan`` / ``inf`` 由 :func:`normalize_json_literals` 先规范化成
       ``NaN`` / ``Infinity``,``json.loads`` 认得它们(输出侧仍然按真实
       ClickHouse 的写法输出小写的 ``nan``/``inf``)。
    """
    prepared = normalize_json_literals(text)
    try:
        return json.loads(prepared, parse_float=str)
    except ValueError as exc:
        # json.JSONDecodeError 是 ValueError 的子类,它的消息里带行列号
        raise_error(ErrorCode.CANNOT_PARSE_TEXT, f"Cannot parse JSON: {exc}")


def json_value_from_json(dtype: DataType, value: Any, settings: Any = None) -> Any:
    """JSON 里的一个值 → 列里的值。

    直接把 JSON 值交给 ``dtype.normalize`` —— 它已经能处理
    ``str``/``int``/``float``/``bool``/``list``/``dict`` 这些形态,
    并且会递归进容器。这里只补一条 ``normalize`` 不该管的规则:
    **非可空列拿到 null 要报错**(而不是静默塞零值)。
    """
    if value is None:
        if dtype.is_nullable():
            return None
        raise_error(
            ErrorCode.CANNOT_INSERT_NULL_IN_ORDINARY_COLUMN,
            f"Cannot insert NULL (JSON null) into a non-Nullable column of type "
            f"{dtype.get_name()}",
        )
    return dtype.normalize(value)


# ══════════════════════════════════════════════════════════════════════════
# EachRow 系列:输出
# ══════════════════════════════════════════════════════════════════════════


class _JSONEachRowOutput(IOutputFormat):
    """``*EachRow`` 输出的公共骨架:一行一个 JSON 文档。"""

    as_string = False
    compact = True       # True: 写成数组 [v,…];False: 写成对象 {"c":v,…}
    write_names = False  # True: 先写一行列名(JSONCompactEachRowWithNames)

    name = "JSONEachRow"

    def write_prefix(self, sample_block: Block, out: Any, settings: Any = None) -> None:
        if self.write_names:
            names = ",".join(json_string(n) for n in sample_block.get_names())
            write_text(out, "[" + names + "]\n")

    def write(self, block: Block, out: Any, settings: Any = None) -> None:
        if block.rows() == 0:
            return
        lines: List[str] = []
        for row in range(block.rows()):
            lines.append(self._render_row(block, row, settings))
        write_text(out, "\n".join(lines) + "\n")

    def _render_row(self, block: Block, row: int, settings: Any) -> str:
        if self.compact:
            body = ",".join(
                json_value_text(c.type, c.column.get(row), settings, self.as_string)
                for c in block.data
            )
            return "[" + body + "]"
        body = ",".join(
            json_string(c.name)
            + ":"
            + json_value_text(c.type, c.column.get(row), settings, self.as_string)
            for c in block.data
        )
        return "{" + body + "}"


class JSONEachRowOutputFormat(_JSONEachRowOutput):
    """``JSONEachRow`` —— 每行一个对象 ``{"id":1,"name":"a"}``。"""

    name = "JSONEachRow"
    compact = False


class JSONStringsEachRowOutputFormat(_JSONEachRowOutput):
    """``JSONStringsEachRow`` —— 每行一个对象,**所有值都是字符串**。

    用途:接收方只用"字符串"一种类型(比如某些日志系统),不必关心
    "这个字段是数字还是字符串"。``null`` 仍然是 JSON 的 ``null``
    (不做成字符串 ``"\\N"``),这样"值是空串"和"值是 NULL"仍然分得开。
    """

    name = "JSONStringsEachRow"
    compact = False
    as_string = True


class JSONCompactEachRowOutputFormat(_JSONEachRowOutput):
    """``JSONCompactEachRow`` —— 每行一个数组 ``[1,"a"]``(省掉列名,更省字节)。"""

    name = "JSONCompactEachRow"


class JSONCompactEachRowWithNamesOutputFormat(_JSONEachRowOutput):
    """``JSONCompactEachRowWithNames`` —— 第一行是列名数组,之后是数据数组。

    .. code-block:: text

       ["id","name"]          ← 表头(write_prefix 写的)
       [1,"a"]
       [2,"b"]
    """

    name = "JSONCompactEachRowWithNames"
    write_names = True


# ══════════════════════════════════════════════════════════════════════════
# EachRow 系列:输入
# ══════════════════════════════════════════════════════════════════════════


class _JSONEachRowInput(IInputFormat):
    """``*EachRow`` 输入:逐行解析(一行一个完整 JSON 文档)。"""

    compact = True
    has_names = False
    name = "JSONEachRow"

    def read(self, source: Any, sample_block: Block, settings: Any = None) -> Iterator[Block]:
        require_schema(sample_block, self.name)
        text = read_all_text(source)
        lines = [ln.strip() for ln in text.split("\n")]
        lines = [ln for ln in lines if ln != ""]

        if self.has_names:
            if not lines:
                raise_error(
                    ErrorCode.UNEXPECTED_END_OF_FILE,
                    f"Format {self.name} expects a header line with column names",
                )
            names = json_load(lines[0])
            lines = lines[1:]
            if not isinstance(names, list) or [str(x) for x in names] != sample_block.get_names():
                raise_error(
                    ErrorCode.TYPE_MISMATCH,
                    f"{self.name} header {names!r} does not match the expected "
                    f"columns {sample_block.get_names()}",
                )

        rows: List[List[Any]] = []
        for line in lines:
            document = json_load(line)
            rows.append(self._row_from_document(document, sample_block, settings, line))

        block_size = int(get_setting(settings, "max_block_size", 65536) or 65536)
        for chunk in chunk_lines(rows, block_size):
            if chunk:
                yield block_from_rows(sample_block, chunk)

    def _row_from_document(
        self,
        document: Any,
        sample_block: Block,
        settings: Any,
        raw: str,
    ) -> List[Any]:
        if self.compact:
            if not isinstance(document, list):
                raise_error(
                    ErrorCode.CANNOT_PARSE_TEXT,
                    f"{self.name} expects a JSON array per row, got {raw!r}",
                )
            if len(document) != sample_block.columns():
                raise_error(
                    ErrorCode.INCORRECT_NUMBER_OF_COLUMNS,
                    f"{self.name} row has {len(document)} value(s) but the block has "
                    f"{sample_block.columns()} column(s): {raw!r}",
                )
            values = document
        else:
            if not isinstance(document, dict):
                raise_error(
                    ErrorCode.CANNOT_PARSE_TEXT,
                    f"{self.name} expects a JSON object per row, got {raw!r}",
                )
            values = []
            for cwtn in sample_block.data:
                if cwtn.name in document:
                    values.append(document[cwtn.name])
                elif cwtn.type.is_nullable():
                    # 缺一个可空列 ⇒ NULL。真实 ClickHouse 的行为也是这样:
                    # 可空列可以缺席,非可空列缺席是错误(除非它有默认值表达式)。
                    values.append(None)
                else:
                    raise_error(
                        ErrorCode.CANNOT_PARSE_TEXT,
                        f"{self.name} row {raw!r} is missing the field "
                        f"'{cwtn.name}' ({cwtn.type.get_name()})",
                    )
        return [
            json_value_from_json(cwtn.type, value, settings)
            for cwtn, value in zip(sample_block.data, values)
        ]


class JSONEachRowInputFormat(_JSONEachRowInput):
    """``JSONEachRow`` 输入。"""

    name = "JSONEachRow"
    compact = False


class JSONStringsEachRowInputFormat(_JSONEachRowInput):
    """``JSONStringsEachRow`` 输入(值都是字符串,按类型 ``normalize`` 回真实类型)。"""

    name = "JSONStringsEachRow"
    compact = False


class JSONCompactEachRowInputFormat(_JSONEachRowInput):
    """``JSONCompactEachRow`` 输入。"""

    name = "JSONCompactEachRow"


class JSONCompactEachRowWithNamesInputFormat(_JSONEachRowInput):
    """``JSONCompactEachRowWithNames`` 输入(第一行是列名数组)。"""

    name = "JSONCompactEachRowWithNames"
    has_names = True


# ══════════════════════════════════════════════════════════════════════════
# JSON / JSONCompact:整个结果集是一个对象
# ══════════════════════════════════════════════════════════════════════════


class _JSONDocumentOutput(IOutputFormat):
    """``JSON`` / ``JSONCompact`` 输出的公共骨架。

    .. code-block:: text

       JSON 的输出分三段(正好对应三个方法):

         write_prefix  ──►  {
                                "meta": [ {"name": "id", "type": "UInt8"}, … ],
                                "data": [
         write(block_i) ─►      [1, "a"],
                                [2, "b"]
         write_suffix  ──►      ],
                                "rows": 2,
                                "statistics": { … }
                            }

    ``"rows"`` 与 ``"statistics"`` 必须等到**最后**才知道,这就是
    ``write_suffix`` 存在的理由 —— 它也解释了为什么 ``JSON`` 格式
    **不能**边算边把最后一行发出去(必须把整个结果集数完)。

    .. note::

       ``"elapsed"`` 在 mini-clickhouse 里恒为 0:我们不在这里计时
       (真实 ClickHouse 会填上真实的耗时,那是个**非确定**的值,
       放进测试里会让输出没法比对)。``"bytes_read"`` 是数据段的字节数。
    """

    compact = False
    name = "JSON"

    def __init__(self) -> None:
        self._wrote_row = False
        self._rows = 0
        self._bytes = 0

    # ── 前缀 ────────────────────────────────────────────────────────────

    def write_prefix(self, sample_block: Block, out: Any, settings: Any = None) -> None:
        self._wrote_row = False
        self._rows = 0
        self._bytes = 0
        meta = ",".join(
            '{"name":"' + json_escape_string(c.name) + '","type":"'
            + json_escape_string(c.type.get_name()) + '"}'
            for c in sample_block.data
        )
        if self.compact:
            write_text(out, '{"meta":[' + meta + '],"data":[')
        else:
            write_text(out, '{\n    "meta": [\n')
            for i, c in enumerate(sample_block.data):
                comma = "," if i + 1 < sample_block.columns() else ""
                write_text(
                    out,
                    '        {"name": "'
                    + json_escape_string(c.name)
                    + '", "type": "'
                    + json_escape_string(c.type.get_name())
                    + '"}'
                    + comma
                    + "\n",
                )
            write_text(out, '    ],\n    "data": [\n')

    # ── 数据 ────────────────────────────────────────────────────────────

    def write(self, block: Block, out: Any, settings: Any = None) -> None:
        for row in range(block.rows()):
            body = ",".join(
                json_value_text(c.type, c.column.get(row), settings)
                for c in block.data
            )
            row_text = "[" + body + "]"
            if self.compact:
                payload = ("," if self._wrote_row else "") + row_text
            else:
                # 逗号写在**上一行的末尾**(即本行的最前面),这样最后一行不会有
                # 尾随逗号 —— JSON 不允许尾随逗号。
                payload = (",\n" if self._wrote_row else "") + "        " + row_text
            self._wrote_row = True
            self._rows += 1
            self._bytes += len(row_text)
            write_text(out, payload)

    # ── 后缀 ────────────────────────────────────────────────────────────

    def write_suffix(self, sample_block: Block, out: Any, settings: Any = None) -> None:
        stats = (
            '"statistics": {"elapsed": 0, "rows_read": '
            + str(self._rows)
            + ", \"bytes_read\": "
            + str(self._bytes)
            + "}"
        )
        if self.compact:
            write_text(out, '],"rows":' + str(self._rows) + "," + stats + "}")
        else:
            # 有数据行时,最后一个数据行末尾没有换行符,补一个
            lead = "\n" if self._wrote_row else ""
            write_text(
                out,
                lead + '    ],\n    "rows": ' + str(self._rows) + ",\n    " + stats + "\n}\n",
            )


class JSONOutputFormat(_JSONDocumentOutput):
    """``JSON`` —— 带缩进、可读性优先的单个对象。"""

    name = "JSON"


class JSONCompactOutputFormat(_JSONDocumentOutput):
    """``JSONCompact`` —— 与 ``JSON`` 内容相同,但不换行、不缩进。

    .. note::

       真实 ClickHouse 里这两个格式的差别**只是排版**(数据形状一样,
       都是"行的数组")。要做成列式(``{"data":{"id":[1,2]}}``)的那个叫
       ``JSONColumns`` —— mini-clickhouse 不实现它(见包文档里的清单)。
    """

    name = "JSONCompact"
    compact = True


class _JSONDocumentInput(IInputFormat):
    """``JSON`` / ``JSONCompact`` 输入:整个文档一次性读进来。"""

    name = "JSON"

    def read(self, source: Any, sample_block: Block, settings: Any = None) -> Iterator[Block]:
        text = read_all_text(source).strip()
        if not text:
            return
        document = json_load(text)
        if not isinstance(document, dict):
            raise_error(
                ErrorCode.CANNOT_PARSE_TEXT,
                f"Format {self.name} expects a single JSON object at the top level",
            )
        schema = self._resolve_schema(document, sample_block)
        data = document.get("data")
        if data is None:
            raise_error(
                ErrorCode.CANNOT_PARSE_TEXT,
                f"Format {self.name} input has no \"data\" array",
            )
        if not isinstance(data, list):
            raise_error(
                ErrorCode.CANNOT_PARSE_TEXT,
                f"Format {self.name} \"data\" must be an array of row arrays",
            )
        rows: List[List[Any]] = []
        for raw_row in data:
            if not isinstance(raw_row, list):
                raise_error(
                    ErrorCode.CANNOT_PARSE_TEXT,
                    f"{self.name} rows must be arrays, got {raw_row!r}",
                )
            if len(raw_row) != schema.columns():
                raise_error(
                    ErrorCode.INCORRECT_NUMBER_OF_COLUMNS,
                    f"{self.name} row has {len(raw_row)} value(s) but the block has "
                    f"{schema.columns()} column(s)",
                )
            rows.append(
                [
                    json_value_from_json(cwtn.type, value, settings)
                    for cwtn, value in zip(schema.data, raw_row)
                ]
            )
        block_size = int(get_setting(settings, "max_block_size", 65536) or 65536)
        for chunk in chunk_lines(rows, block_size):
            if chunk:
                yield block_from_rows(schema, chunk)

    def _resolve_schema(self, document: Dict[str, Any], sample_block: Block) -> Block:
        """决定用哪个 schema:有 sample_block 就用它,否则从 ``meta`` 建。

        ``meta`` 存在时会**校验** —— 类型名对不上直接报错,因为那意味着
        "文件是给另一个表写的",继续读下去只会产生更难懂的错误。
        """
        meta = document.get("meta")
        if sample_block.columns() > 0:
            if isinstance(meta, list):
                names = [str(entry.get("name", "")) for entry in meta if isinstance(entry, dict)]
                types = [str(entry.get("type", "")) for entry in meta if isinstance(entry, dict)]
                expected_names = sample_block.get_names()
                expected_types = [c.type.get_name() for c in sample_block.data]
                if names and names != expected_names:
                    raise_error(
                        ErrorCode.TYPE_MISMATCH,
                        f"{self.name} meta names {names} do not match the expected "
                        f"columns {expected_names}",
                    )
                if types and types != expected_types:
                    raise_error(
                        ErrorCode.TYPE_MISMATCH,
                        f"{self.name} meta types {types} do not match the expected "
                        f"types {expected_types}",
                    )
            return sample_block
        if not isinstance(meta, list) or not meta:
            raise_error(
                ErrorCode.INCORRECT_NUMBER_OF_COLUMNS,
                f"Format {self.name} needs either a non-empty sample block or a "
                f"\"meta\" array with column names and types",
            )
        columns = []
        for entry in meta:
            if not isinstance(entry, dict) or "name" not in entry or "type" not in entry:
                raise_error(
                    ErrorCode.CANNOT_PARSE_TEXT,
                    f"{self.name} meta entries must be {{\"name\": …, \"type\": …}}, "
                    f"got {entry!r}",
                )
            columns.append(
                ColumnWithTypeAndName(str(entry["name"]), get_data_type(str(entry["type"])))
            )
        return Block(columns)


class JSONInputFormat(_JSONDocumentInput):
    """``JSON`` 输入(同样接受紧凑排版的文档 —— 排版不影响解析)。"""

    name = "JSON"


class JSONCompactInputFormat(_JSONDocumentInput):
    """``JSONCompact`` 输入。"""

    name = "JSONCompact"

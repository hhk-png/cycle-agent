"""格式系统的地基 —— 输入/输出接口、注册表,以及所有文本格式共用的值渲染。

``FORMAT`` 是 ClickHouse 最容易被低估的一块。大多数人以为它只是
"打印结果",其实它承担着三件更重要的事:

.. code-block:: text

   ① 数据进出的唯一通道
      INSERT 的数据从格式里来,SELECT 的结果从格式里走。
      所以格式是**用户看见的 ClickHouse**,任何一个字段转义错了
      都会变成"导出的数据再导回来就变了"。

   ② 可往返(round-trip)是硬要求
      同一个 Block 写出去再读回来,必须**逐值相等** ——
      这是第 17 章验证序列化正确性的主要手段,也是本模块的验收标准。
      凡是做不到往返的地方,必须在文档里写清楚**哪一步丢了信息**。

   ③ 流式的单位是 Block,不是"整个结果集"
      write(block) 会被调用很多次(每个 block 一次),所以
      "表头/前缀"和"表尾/后缀"必须单独放在
      write_prefix / write_suffix 里 —— 这解释了为什么
      ClickHouse 的 IOutputFormat 有这三个方法而不是一个 writeResult。

.. code-block:: text

   一次 FORMAT 输出的时间线:

     write_prefix(sample_block)     ← 表头:TSVWithNames 的列名行 /
                                      JSON 的 {"meta":[…],"data":[  / RowBinary 的列名
        write(block0)               ← 数据(会被调用 N 次)
        write(block1)
        ...
     write_suffix(sample_block)     ← 表尾:JSON 的 ],"rows":N} / Native 的结束标记

     sample_block 是"只有列名与类型、零行"的 Block —— 它就是 Block 的 header
     (见 :mod:`minich.block`)。表头信息只能从它拿,所以写表头的方法
     收到的永远是 sample_block 而不是数据块。

**注册表**。真实 ClickHouse 的格式注册表在
``src/Formats/FormatFactory.cpp``,用一张 ``name → creator`` 的表 +
``registerFileSegmentationEngine`` 之类的一堆旁表。mini-clickhouse 只保留
"名字 → 工厂"这一层,并显式记录"这个格式可读 / 可写 / 说明":

.. code-block:: text

   register_format("TSV", TSV, is_input=True, is_output=True, description="…")
                          │
                          └─ 一个**无参工厂**:每次 get_*_format 都新建一个实例。
                             为什么要新建?因为格式对象可能带流式状态
                             (比如 JSON 要记住"是不是写了第一行")。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Sequence, Tuple

from ..block import Block, ColumnWithTypeAndName
from ..errors import ErrorCode, raise_error
from ..literals import parse_literal, split_top_level
from ..types import DataType
from ..types.composite import DataTypeNested
from ..util.strings import escape_tsv

__all__ = [
    "IInputFormat",
    "IOutputFormat",
    "register_format",
    "get_input_format",
    "get_output_format",
    "format_exists",
    "registered_formats",
    "get_setting",
    "NULL_SENTINEL",
    "read_all_text",
    "read_all_bytes",
    "write_text",
    "render_value_text",
    "parse_rendered_value",
    "parse_value_text",
    "render_quoted_value",
    "parse_values_field",
    "block_from_columns",
    "block_from_rows",
    "is_string_like",
    "is_right_aligned",
]


# ══════════════════════════════════════════════════════════════════════════
# 值 ↔ 文本:所有文本格式共用的那一层
# ══════════════════════════════════════════════════════════════════════════

#: NULL 的文本哨兵 —— **两个字符**:反斜杠 + 大写 N。
#:
#: 为什么不用空串表示 NULL? 因为空串是一个合法的 String 值。
#: TSV/CSV 里这一对必须能区分开:
#:
#: ==================  ==============  ==================
#: 值                  字段内容         读回来的结果
#: ==================  ==============  ==================
#: ``NULL``            ``\\N``          ``None``
#: ``""``(空串)        (空字段)         ``""``
#: ``"\\N"``(4 字符)   ``\\\\N``        ``"\\N"``
#: ==================  ==============  ==================
#:
#: 第三行是关键:字面量反斜杠会被转义成两个反斜杠,所以它**永远不会**
#: 和 NULL 哨兵撞车。真实 ClickHouse 用同样的哨兵(``output_format_tsv_…``
#: 一族设置都默认 ``\\N``)。
NULL_SENTINEL = "\\N"

#: ``Values`` / ``SQLInsert`` 里"没有值"的写法(带引号语义的 NULL)。
NULL_LITERAL = "NULL"


def is_string_like(dtype: DataType) -> bool:
    """是否应当按"字符串"来渲染 —— 会穿透 ``LowCardinality``。

    ``LowCardinality(String)`` 的 ``is_string()`` 是 False(它是复合类型),
    但它的文本表示**就是**内层的字符串,而且内层字符串需要转义。
    穿透是为了走对"转义"这条分支。
    """
    d = dtype
    while d.is_low_cardinality():
        d = d.nested  # type: ignore[attr-defined]
    return d.is_string()


def is_right_aligned(dtype: DataType) -> bool:
    """``Pretty*`` / ``Markdown`` 里这一列是否右对齐。

    真实 ClickHouse 按"是不是数字"决定对齐:数值右对齐,其余左对齐。
    ``is_integer()`` 把 ``Date``/``DateTime``/``Enum``/``Bool`` 也算进来了
    —— 它们在 Pretty 里确实也是右对齐的(展示出来就是一串数字/日期)。
    """
    return dtype.is_numeric() or dtype.is_integer() or dtype.is_decimal()


def _escape_quoted(body: str) -> str:
    """把字符串渲染成 ``'…'`` 形式,并转义所有会破坏语法的字符。

    这是 ``Values`` / ``SQLInsert`` / 容器元素使用的那一套转义,它必须与
    :mod:`minich.literals` 的 ``read_quoted`` 严格对称:

    .. code-block:: text

       转义(写)              解码(读,在 read_quoted 里)
       \\\\  →  \\           \\  →  \\
       \\'   →  '            '  →  '
       \\t   →  <TAB>        t  →  <TAB>
       \\n   →  <换行>        n  →  <换行>
       \\r   →  <CR>         r  →  <CR>
       \\0   →  <NUL>        0  →  <NUL>

    .. warning::

       顺序不能反:必须**先**转义反斜杠,否则后面产生的反斜杠会被二次转义。
       ``a\\tb``(反斜杠 + t 两个字面字符)必须写成 ``'a\\\\tb'``,
       读回来才是 ``a\\tb``;如果先转义 ``t`` 就会变成 ``'a\\\\<TAB>'``。
    """
    out = (
        body.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\0", "\\0")
    )
    return "'" + out + "'"


def render_value_text(
    dtype: DataType,
    value: Any,
    settings: Any = None,
    quoted: bool = False,
) -> str:
    """把一个值渲染成**文本格式的一个字段**(未做行内分隔符处理)。

    ``quoted=False``(TSV/CSV/Pretty 的顶层标量):

    .. code-block:: text

       String  "a<TAB>b"   →  a\\tb          (反斜杠转义,不加引号)
       UInt8   42          →  42
       NULL                →  NULL          (调用方通常自己换成 \\N)
       Array   ['a','b']   →  ['a','b']     ← 注意:**容器内的元素加引号**

    ``quoted=True``(``Values`` / ``SQLInsert`` 的顶层标量):
    顶层标量也加引号,于是 ``String`` 变成 ``'a\\tb'``。

    .. note::

       "顶层不加引号、容器内加引号"这条规则不是随便定的,它和真实
       ClickHouse 完全一致:``SELECT 'a b' FORMAT TSV`` 输出 ``a b``,
       而 ``SELECT ['a b'] FORMAT TSV`` 输出 ``['a b']`` —— 因为容器
       里必须先把元素包起来,才能用逗号区分元素边界。

    .. warning::

       ``Map`` 的键**总是**带引号(``{'k':1}``),而且容器是**自己渲染**的,
       没有直接调 ``serialize_text``/``serialize_text_quoted``。原因有两个:

       * ``DataTypeMap.serialize_text`` 给出的是 ``{k:1}``(键不带引号),
         那个形式**无法**被 :mod:`minich.literals` 解析回来;
       * ``minich.literals.parse_literal`` **不认识顶层元组** ——
         ``parse_literal("(1,'a')")`` 会在 ``_parse_list`` 里
         ``expect("[")`` 失败。所以 :func:`parse_rendered_value` 也必须
         自己解析 ``(…)``。

       结论:容器类型的文本编解码由本模块**成对**实现(写在这一层、
       读也在这一层),而不是借用类型自己的 ``serialize_text``。这也正是
       真实 ClickHouse 里 ``Serialization*::serializeTextQuoted`` 承担的角色。
    """
    if value is None:
        return NULL_LITERAL
    if dtype.is_nullable():
        return render_value_text(dtype.nested, value, settings, quoted)  # type: ignore[attr-defined]
    if dtype.is_array():
        inner = ",".join(
            render_value_text(dtype.element, v, settings, True)  # type: ignore[attr-defined]
            for v in value
        )
        return "[" + inner + "]"
    if dtype.is_tuple():
        inner = ",".join(
            render_value_text(t, v, settings, True)
            for t, v in zip(dtype.elements, value)  # type: ignore[attr-defined]
        )
        return "(" + inner + ")"
    if dtype.is_map():
        body = ",".join(
            render_value_text(dtype.key, k, settings, True)  # type: ignore[attr-defined]
            + ":"
            + render_value_text(dtype.value, v, settings, True)  # type: ignore[attr-defined]
            for k, v in value.items()
        )
        return "{" + body + "}"
    if isinstance(dtype, DataTypeNested):
        items = []
        for item in value:
            members = ",".join(
                render_value_text(t, v, settings, True)
                for t, v in zip(dtype.elements, item)
            )
            items.append("(" + members + ")")
        return "[" + ",".join(items) + "]"
    if is_string_like(dtype):
        text = dtype.serialize_text(value, settings)
        return _escape_quoted(text) if quoted else escape_tsv(text)
    if quoted:
        return dtype.serialize_text_quoted(value, settings)
    return escape_tsv(dtype.serialize_text(value, settings) or "")


def _strip_wrapper(text: str, opening: str, closing: str, dtype: DataType) -> str:
    body = text.strip()
    if not (body.startswith(opening) and body.endswith(closing)):
        raise_error(
            ErrorCode.CANNOT_PARSE_TEXT,
            f"Cannot parse {text!r} as {dtype.get_name()}: expected "
            f"{opening}…{closing}",
        )
    return body[1:-1]


def parse_rendered_value(
    dtype: DataType,
    text: str,
    settings: Any = None,
    quoted: bool = False,
) -> Any:
    """:func:`render_value_text` 的**逆操作**。

    ``quoted`` 必须与写出去时用的一致 —— 它决定"标量带不带引号":

    .. code-block:: text

       上下文                        quoted    标量怎么读
       TSV/CSV 的顶层字段             False     deserialize_text(**一次**反转义)
       容器里的元素 / Values 的字段    True     剥掉引号后再反转义

    .. note::

       **裸 token 交给类型自己的 ``deserialize_text``**,而不是
       :func:`parse_literal`。两个原因:

       * ``parse_literal("1234.5678")`` 得到 **float**,而
         ``Decimal(38, 18).deserialize_text`` 走 ``decimal.Decimal``(精确);
       * ``parse_literal`` 会把 ``'abc'`` 这类引号串解成 ``abc`` ——
         但 TSV 顶层的一个字面量 ``'abc'``(带引号的值本身)必须原样保留,
         它没被转义过,不该被当成引号语法。

       这两条决定了"什么时候可以走字面量解析":**只有在我们自己写上
       引号的地方**(容器元素与 Values 字段)才可以。
    """
    s = text.strip()
    if s == NULL_LITERAL:
        return None
    if dtype.is_nullable():
        return parse_rendered_value(dtype.nested, s, settings, quoted)  # type: ignore[attr-defined]
    if dtype.is_array():
        body = _strip_wrapper(s, "[", "]", dtype)
        if body.strip() == "":
            return []
        return [
            parse_rendered_value(dtype.element, item, settings, True)  # type: ignore[attr-defined]
            for item in split_top_level(body, ",")
        ]
    if dtype.is_tuple():
        body = _strip_wrapper(s, "(", ")", dtype)
        items = [] if body.strip() == "" else split_top_level(body, ",")
        elements = dtype.elements  # type: ignore[attr-defined]
        if len(items) != len(elements):
            raise_error(
                ErrorCode.SIZES_OF_COLUMNS_IN_TUPLE_DOESNT_MATCH,
                f"{dtype.get_name()} expects {len(elements)} elements, got "
                f"{len(items)}: {text!r}",
            )
        return tuple(
            parse_rendered_value(t, item, settings, True)
            for t, item in zip(elements, items)
        )
    if dtype.is_map():
        body = _strip_wrapper(s, "{", "}", dtype)
        result: Dict[Any, Any] = {}
        if body.strip() != "":
            for pair in split_top_level(body, ","):
                parts = split_top_level(pair, ":")
                if len(parts) != 2:
                    raise_error(
                        ErrorCode.CANNOT_PARSE_TEXT,
                        f"Cannot parse map entry {pair!r} in {text!r}",
                    )
                result[
                    parse_rendered_value(dtype.key, parts[0], settings, True)  # type: ignore[attr-defined]
                ] = parse_rendered_value(dtype.value, parts[1], settings, True)  # type: ignore[attr-defined]
        return result
    if isinstance(dtype, DataTypeNested):
        body = _strip_wrapper(s, "[", "]", dtype)
        items = [] if body.strip() == "" else split_top_level(body, ",")
        out = []
        for item in items:
            members = split_top_level(_strip_wrapper(item, "(", ")", dtype), ",")
            if len(members) != len(dtype.elements):
                raise_error(
                    ErrorCode.SIZES_OF_COLUMNS_IN_TUPLE_DOESNT_MATCH,
                    f"{dtype.get_name()} expects {len(dtype.elements)} members, "
                    f"got {len(members)}: {item!r}",
                )
            out.append(
                tuple(
                    parse_rendered_value(t, m, settings, True)
                    for t, m in zip(dtype.elements, members)
                )
            )
        return out
    if is_string_like(dtype):
        if quoted and len(s) >= 2 and s.startswith("'") and s.endswith("'"):
            # 我们自己写出去的引号,由 _escape_quoted 转义 → 这里剥掉后解码
            return dtype.deserialize_text(s[1:-1], settings)
        return dtype.deserialize_text(s, settings)
    if quoted and s.startswith("'"):
        # 带引号的标量(日期/枚举/UUID/Dynamic 的引号形式)→ 交给字面量解析
        return dtype.normalize(parse_literal(s))
    return dtype.deserialize_text(s, settings)


def parse_value_text(dtype: DataType, text: str, settings: Any = None) -> Any:
    """把 TSV/CSV 的一个字段读回值(``text`` 已去掉 CSV 的引号层)。

    两条规则:

    1. 字段恰好是 ``\\N``:可空列 → ``None``;非可空列 → 报错
       (真实 ClickHouse 也是如此,它不会把 NULL 静默塞进非可空列);
    2. 其余情况交给 :func:`parse_rendered_value`(``quoted=False``)。

    .. warning::

       可空列要**穿透**到内层再去解析。原因是
       ``DataTypeNullable.deserialize_text`` 把空串也当成 NULL,
       而 TSV 里的空字段含义是"空字符串" —— 如果直接调它,
       ``Nullable(String)`` 里的空串就会被读成 NULL,往返立刻失败。
    """
    if text == NULL_SENTINEL:
        if dtype.is_nullable():
            return None
        raise_error(
            ErrorCode.CANNOT_PARSE_TEXT,
            f"Cannot read the NULL sentinel {NULL_SENTINEL!r} into a non-Nullable "
            f"column of type {dtype.get_name()}",
        )
    return parse_rendered_value(dtype, text, settings, quoted=False)


def render_quoted_value(dtype: DataType, value: Any, settings: Any = None) -> str:
    """``Values`` / ``SQLInsert`` 用的字段渲染(顶层标量也加引号)。"""
    return render_value_text(dtype, value, settings, True)


def parse_values_field(dtype: DataType, text: str, settings: Any = None) -> Any:
    """``Values`` 格式里一个字段的解析(顶层标量带引号)。"""
    return parse_rendered_value(dtype, text, settings, quoted=True)


# ══════════════════════════════════════════════════════════════════════════
# 输入 / 输出接口
# ══════════════════════════════════════════════════════════════════════════


class IInputFormat(ABC):
    """输入格式 —— 把"外部的一坨字节/文本"变成 ``Block`` 流。

    .. code-block:: text

       read(source, sample_block) ──► Block ──► Block ──► …

          source        sample_block
      ┌───────────┐   ┌──────────────────┐
      │ str/bytes │   │ id UInt64 (0 行) │  ← 只有"列名 + 类型"
      │ 或 行迭代 │   │ name String(0 行)│     输入格式的一切解析
      └───────────┘   └──────────────────┘     都以它为准
    """

    #: 格式名 —— 与注册时用的名字一致
    name: str = "Unknown"

    @abstractmethod
    def read(
        self,
        source: Any,
        sample_block: Block,
        settings: Any = None,
    ) -> Iterator[Block]:
        """解析 ``source``,按 ``sample_block`` 的结构产出 Block。

        ``source`` 可以是 ``str`` / ``bytes`` / ``Iterable[str]``
        (文本格式),或者 ``bytes`` / ``bytearray``(二进制格式)。
        返回的每个 Block 的列名与类型都必须与 ``sample_block`` 完全一致。
        """


class IOutputFormat(ABC):
    """输出格式 —— 把 ``Block`` 流写进 ``out``。

    ``out`` 对文本格式是"可写对象"(有 ``.write(str)``),对二进制格式是
    ``bytearray``。为了让测试与调用方少踩坑,:func:`write_text` 同时
    接受这两种,所以**文本格式一律走 write_text,不要直接 out.write**。
    """

    name: str = "Unknown"

    @abstractmethod
    def write(self, block: Block, out: Any, settings: Any = None) -> None:
        """写出一个 Block 的数据(**不含**表头与表尾)。"""

    def write_prefix(self, sample_block: Block, out: Any, settings: Any = None) -> None:
        """写"表头"。被调用一次,之后才是若干次 :meth:`write`。

        ``sample_block`` 是零行的 schema(列名 + 类型都齐全)。
        需要输出列名的格式(``TSVWithNames``、``JSON``、``SQLInsert``)
        都在这里动手。
        """

    def write_suffix(self, sample_block: Block, out: Any, settings: Any = None) -> None:
        """写"表尾"。被调用一次,用来收尾(``JSON`` 的 ``]}``、``Native`` 的结束标记)。"""


# ══════════════════════════════════════════════════════════════════════════
# 注册表
# ══════════════════════════════════════════════════════════════════════════


class _FormatEntry:
    """注册表里的一行。真实 ClickHouse 的对应物是 ``FormatFactory`` 里的 ``FormatCase``。

    一个名字**只有一个** _FormatEntry,它同时装"读"与"写"两个工厂 ——
    因为读和写是同一个格式的两个方向(``TSVInputFormat`` /
    ``TSVOutputFormat``),``system.formats`` 里也只有一行。
    """

    __slots__ = ("name", "input_factory", "output_factory", "description")

    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.input_factory: Any = None
        self.output_factory: Any = None
        self.description = description


#: 名字(小写)→ 注册项。**别名指向同一个 _FormatEntry 对象**,
#: 所以别名与正名行为完全一致,而且 registered_formats() 只列正名。
_REGISTRY: Dict[str, _FormatEntry] = {}


def register_format(
    name: str,
    factory: Any,
    *,
    is_input: bool,
    is_output: bool,
    description: str,
    aliases: Sequence[str] = (),
) -> None:
    """注册一个格式的一个方向(读 / 写)。

    ``factory`` 是一个**无参可调用对象**,返回格式实例 —— 每次
    :func:`get_input_format` / :func:`get_output_format` 都会新造一个。
    这不是浪费:格式对象可能带流式状态(``JSON`` 要数行、``Values`` 要记住
    "上一行写过没"),共享一个实例会让两次输出互相污染。

    同一个名字可以被调用两次 —— 一次注册读、一次注册写,信息会**合并**
    到同一条注册项上::

        register_format("TSV", lambda: TSVOutputFormat(), is_input=False,
                        is_output=True,  description="…")
        register_format("TSV", lambda: TSVInputFormat(),  is_input=True,
                        is_output=False, description="…")

    名字与别名都**大小写不敏感**(统一小写存放),这与真实 ClickHouse
    一致(``FORMAT tsv`` 和 ``FORMAT TSV`` 是同一个东西)。
    """
    key = name.lower()
    entry = _REGISTRY.get(key)
    if entry is None:
        entry = _FormatEntry(name, description)
        _REGISTRY[key] = entry
    if is_input:
        entry.input_factory = factory
    if is_output:
        entry.output_factory = factory
    if description:
        entry.description = description
    for alias in aliases:
        _REGISTRY[alias.lower()] = entry


def _lookup(name: str) -> _FormatEntry:
    entry = _REGISTRY.get(str(name).lower())
    if entry is None:
        raise_error(
            ErrorCode.UNKNOWN_FORMAT,
            f"Unknown format {name}. Available formats: "
            f"{', '.join(n for n, _, _, _ in registered_formats())}",
        )
    return entry


def format_exists(name: str) -> bool:
    """格式名(含别名)是否已注册。"""
    return str(name).lower() in _REGISTRY


def get_input_format(name: str) -> IInputFormat:
    """按名字取一个**输入**格式实例。"""
    entry = _lookup(name)
    if entry.input_factory is None:
        raise_error(
            ErrorCode.UNKNOWN_FORMAT,
            f"Format {entry.name} cannot be used for input (it is output-only)",
        )
    return entry.input_factory()


def get_output_format(name: str) -> IOutputFormat:
    """按名字取一个**输出**格式实例。"""
    entry = _lookup(name)
    if entry.output_factory is None:
        raise_error(
            ErrorCode.UNKNOWN_FORMAT,
            f"Format {entry.name} cannot be used for output (it is input-only)",
        )
    return entry.output_factory()


def registered_formats() -> List[Tuple[str, bool, bool, str]]:
    """``[(名字, 可读, 可写, 说明)]`` —— 数据源是 ``system.formats``。

    只列**正名**,不列别名(与真实 ClickHouse 的 ``system.formats`` 一致:
    别名在 ``aliases`` 列里,不单独成行)。
    """
    seen: Dict[int, Tuple[str, bool, bool, str]] = {}
    for entry in _REGISTRY.values():
        seen[id(entry)] = (
            entry.name,
            entry.input_factory is not None,
            entry.output_factory is not None,
            entry.description,
        )
    return sorted(seen.values(), key=lambda row: row[0])


# ══════════════════════════════════════════════════════════════════════════
# settings 与 IO 小工具
# ══════════════════════════════════════════════════════════════════════════


def get_setting(settings: Any, name: str, default: Any = None) -> Any:
    """从 ``settings`` 里取一个设置项,取不到返回 ``default``。

    ``settings`` 有三种可能的形态,这里全部接受:

    * ``None``               —— 什么都没有,一律用默认值;
    * 一个 Mapping(``dict``)—— 先查原名,再查全小写、全大写;
    * 一个对象               —— 取同名属性。

    大小写三种都试,是因为真实 ClickHouse 的设置名是小写蛇形
    (``output_format_json_quote_64bit_integers``),而 Python 侧
    有人习惯写驼峰 —— 让两边都能过比强迫用户改习惯更省事。
    """
    if settings is None:
        return default
    keys = (name, name.lower(), name.upper())
    getter = getattr(settings, "get", None)
    if callable(getter) and isinstance(settings, Mapping):
        for key in keys:
            value = settings.get(key)
            if value is not None:
                return value
        return default
    for key in keys:
        if hasattr(settings, key):
            value = getattr(settings, key)
            if value is not None:
                return value
    return default


def read_all_text(source: Any) -> str:
    """把各种输入形态统一成一段 ``str``(文本格式的入口)。

    ``str`` 原样返回,``bytes`` 按 ``utf-8 + surrogateescape`` 解码
    (与 ``ColumnString`` 用的是同一套,保证任意字节都能往返),
    可迭代对象按元素拼接,文件对象走 ``read()``。
    """
    if isinstance(source, str):
        return source
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source).decode("utf-8", "surrogateescape")
    if hasattr(source, "read"):
        return read_all_text(source.read())
    if isinstance(source, Iterable):
        parts: List[str] = []
        for chunk in source:
            parts.append(read_all_text(chunk))
        return "".join(parts)
    raise_error(
        ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
        f"Cannot read format input from {type(source).__name__}",
    )
    return ""  # pragma: no cover —— 上面一定抛了


def read_all_bytes(source: Any) -> bytes:
    """把各种输入形态统一成 ``bytes``(二进制格式的入口)。"""
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source)
    if isinstance(source, str):
        return source.encode("utf-8", "surrogateescape")
    if hasattr(source, "read"):
        return read_all_bytes(source.read())
    if isinstance(source, Iterable):
        parts: List[bytes] = []
        for chunk in source:
            parts.append(read_all_bytes(chunk))
        return b"".join(parts)
    raise_error(
        ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
        f"Cannot read binary format input from {type(source).__name__}",
    )
    return ""  # pragma: no cover


def write_text(out: Any, text: str) -> None:
    """往 ``out`` 写一段文本 —— 同时支持文件对象与 ``bytearray``。"""
    if isinstance(out, bytearray):
        out += text.encode("utf-8", "surrogateescape")
        return
    writer = getattr(out, "write", None)
    if writer is None:
        raise_error(
            ErrorCode.CANNOT_WRITE_TO_OSTREAM,
            f"Cannot write text to {type(out).__name__}: it has no write() method",
        )
    writer(text)


# ══════════════════════════════════════════════════════════════════════════
# 构造结果的辅助
# ══════════════════════════════════════════════════════════════════════════


def block_from_columns(sample_block: Block, columns: Sequence[Sequence[Any]]) -> Block:
    """用 ``sample_block`` 的 schema 装一列列值,拼出一个带数据的 Block。

    行数不一致会抛错 —— 这正是 :meth:`Block.check_number_of_rows` 存在的意义。
    """
    if len(columns) != sample_block.columns():
        raise_error(
            ErrorCode.INCORRECT_NUMBER_OF_COLUMNS,
            f"Got {len(columns)} columns of values for a sample block with "
            f"{sample_block.columns()} columns",
        )
    out = Block([], sample_block.info.clone())
    for cwtn, values in zip(sample_block.data, columns):
        column = cwtn.type.create_column()
        for v in values:
            column.append(v)
        out.add_column(ColumnWithTypeAndName(cwtn.name, cwtn.type, column))
    out.check_number_of_rows()
    return out


def block_from_rows(sample_block: Block, rows: Sequence[Sequence[Any]]) -> Block:
    """按**行**拼 Block(输入格式逐行解析时最顺手)。

    实现上先转成列再装,因为列存里"列"才是真正的一等公民
    (这也是为什么真实 ClickHouse 的输入格式内部仍然按列 push)。
    """
    columns: List[List[Any]] = [[] for _ in sample_block.data]
    for row in rows:
        if len(row) != sample_block.columns():
            raise_error(
                ErrorCode.INCORRECT_NUMBER_OF_COLUMNS,
                f"Row has {len(row)} values but the block has "
                f"{sample_block.columns()} columns",
            )
        for i, v in enumerate(row):
            columns[i].append(v)
    return block_from_columns(sample_block, columns)


def require_schema(sample_block: Block, format_name: str) -> None:
    """没有列名/类型的格式必须拿到非空 schema,否则没法解析。

    ``TSV`` 里没有任何类型信息 —— 光看 ``1`` 无法知道它是 ``UInt8`` 还是
    ``String``。所以这类格式的 schema 是**必需的**,缺了就只能报错。
    """
    if sample_block.columns() == 0:
        raise_error(
            ErrorCode.INCORRECT_NUMBER_OF_COLUMNS,
            f"Format {format_name} needs a non-empty sample block (column names and "
            f"types) to know how to parse the input; got a block with 0 columns",
        )


def chunk_lines(lines: Sequence[Any], block_size: int) -> Iterator[Sequence[Any]]:
    """把解析出的行切成一批批 —— 输出格式是**流式**的,输入也该是。"""
    if block_size <= 0:
        block_size = 65536
    for start in range(0, len(lines), block_size):
        yield lines[start : start + block_size]

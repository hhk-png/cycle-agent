"""``minich.formats`` —— 格式(``FORMAT``)子系统。

``SELECT … FORMAT TSV`` 里的那个 ``FORMAT`` 就是这里。整个子系统的中心
只有两个接口和一张注册表:

.. code-block:: text

   ┌──────────────────────── 注册表(minich/formats/base.py)─────────────┐
   │  "TSV"  →  TSVOutputFormat / TSVInputFormat   (可读 ✔ 可写 ✔)      │
   │  "CSV"  →  …                                  (可读 ✔ 可写 ✔)      │
   │  "Pretty" → PrettyOutputFormat                (可读 ✘ 可写 ✔)      │
   │  "Native" → …                                 (可读 ✔ 可写 ✔)      │
   └────────────────────────────────────────────────────────────────────┘
              │                                    ▲
   读: get_input_format(name)            写: get_output_format(name)
              │                                    │
       read(source, sample_block)        write_prefix/write/write_suffix

**本模块实现的格式一览**(``system.formats`` 的数据源,
用 :func:`registered_formats` 取):

===============================  ==========  ==========  ==============================
格式                             可读        可写        一句话
===============================  ==========  ==========  ==============================
``TSV`` / ``TSVWithNames`` /      ✔           ✔           制表符分隔;可往返的默认选择
``TSVWithNamesAndTypes``
``CSV`` / ``CSVWithNames``        ✔           ✔           逗号分隔 + RFC-4180 引号
``Values``                        ✔           ✔           SQL 字面量 ``(1,'a'),(2,'b')``
``JSONEachRow`` /                 ✔           ✔           一行一个 JSON 文档
``JSONStringsEachRow`` /
``JSONCompactEachRow`` /
``JSONCompactEachRowWithNames``
``JSON`` / ``JSONCompact``        ✔           ✔           整个结果集一个对象(meta+data)
``RowBinary`` /                   ✔           ✔           逐值二进制,最省字节
``RowBinaryWithNames`` /
``RowBinaryWithNamesAndTypes``
``Native``                        ✔           ✔           ClickHouse 的队列/part 格式
``Pretty`` / ``PrettyCompact`` /  ✘           ✔           给人看的表格
``PrettyCompactMonoBlock`` /
``PrettySpace``
``Vertical``                      ✘           ✔           一行一块的竖排
``Markdown``                      ✘           ✔           GitHub 风格表格
``SQLInsert``                     ✘           ✔           ``INSERT INTO … VALUES …;``
``Null``                          ✘           ✔           什么都不输出(压测用)
===============================  ==========  ==========  ==============================

**没有实现、也刻意不注册的格式**(不留空壳:注册了却抛
``NOT_IMPLEMENTED`` 比"没这个格式"更糟):

========================  ==============================================
格式                       为什么不做
========================  ==============================================
``JSONColumns``           列式 JSON(``{"data":{"x":[1,2]}}``)。要做就得
                          再实现一套"列 → JSON"的遍历,而它相对 ``JSON``
                          没有新的教学点
``JSONObjectEachRow``     以每行的主键名为顶层 key。与 ``JSONEachRow``
                          只差一层包装,收益太小
``TSKV`` / ``Template`` /  需要一套模板/转义说明语言,属于"可配置格式"
``CustomSeparated`` 一族   那一族,与"实现格式"是两件事
``Parquet`` / ``ORC`` /   列式二进制 + 复杂编码,不适合"纯 Python 教学"
``Avro`` / ``Arrow``
``Protobuf`` / ``MsgPack``
========================  ==============================================

**怎么选**:

.. code-block:: text

   要能原样读回来(往返)        → TSV(默认)、TSVWithNamesAndTypes、Native
   要给别的程序吃                → CSVe(表格)、JSONEachRow(流式 JSON)、JSON(整体)
   看结果                        → PrettyCompact(默认)、Vertical(列多)、Markdown
   灌进 SQL 脚本                 → SQLInsert、Values
   只测性能不看结果              → Null
   要最快、两边都是 ClickHouse   → Native
"""

from __future__ import annotations

from .base import (
    IInputFormat,
    IOutputFormat,
    NULL_SENTINEL,
    block_from_columns,
    block_from_rows,
    format_exists,
    get_input_format,
    get_output_format,
    get_setting,
    parse_value_text,
    register_format,
    registered_formats,
    render_quoted_value,
    render_value_text,
    is_right_aligned,
    is_string_like,
)
from .binary_formats import (
    NativeInputFormat,
    NativeOutputFormat,
    RowBinaryInputFormat,
    RowBinaryOutputFormat,
    RowBinaryWithNamesAndTypesInputFormat,
    RowBinaryWithNamesAndTypesOutputFormat,
    RowBinaryWithNamesInputFormat,
    RowBinaryWithNamesOutputFormat,
)
from .json_formats import (
    JSONCompactEachRowInputFormat,
    JSONCompactEachRowOutputFormat,
    JSONCompactEachRowWithNamesInputFormat,
    JSONCompactEachRowWithNamesOutputFormat,
    JSONCompactInputFormat,
    JSONCompactOutputFormat,
    JSONEachRowInputFormat,
    JSONEachRowOutputFormat,
    JSONInputFormat,
    JSONOutputFormat,
    JSONStringsEachRowInputFormat,
    JSONStringsEachRowOutputFormat,
)
from .pretty_formats import (
    MarkdownOutputFormat,
    NullOutputFormat,
    PrettyCompactMonoBlockOutputFormat,
    PrettyCompactOutputFormat,
    PrettyOutputFormat,
    PrettySpaceOutputFormat,
    SQLInsertOutputFormat,
    VerticalOutputFormat,
)
from .text_formats import (
    CSVInputFormat,
    CSVOutputFormat,
    CSVWithNamesInputFormat,
    CSVWithNamesOutputFormat,
    TSVInputFormat,
    TSVOutputFormat,
    TSVWithNamesAndTypesInputFormat,
    TSVWithNamesAndTypesOutputFormat,
    TSVWithNamesInputFormat,
    TSVWithNamesOutputFormat,
)
from .values_format import ValuesInputFormat, ValuesOutputFormat

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
    "render_value_text",
    "render_quoted_value",
    "parse_value_text",
    "block_from_columns",
    "block_from_rows",
    "is_right_aligned",
    "is_string_like",
]


# ══════════════════════════════════════════════════════════════════════════
# 注册
# ══════════════════════════════════════════════════════════════════════════
#
# 每个格式注册一次,工厂用 lambda —— 每次 get_*_format 都新建实例,
# 因为格式对象可能带流式状态(JSON 的行数、Values 的"是否写过行")。

# ── TSV 家族 ────────────────────────────────────────────────────────────
register_format(
    "TSV",
    lambda: TSVOutputFormat(),
    is_input=False,
    is_output=True,
    description="TabSeparated:制表符分隔,NULL 是 \\N,可无损往返",
    aliases=("TabSeparated",),
)
register_format(
    "TSV",
    lambda: TSVInputFormat(),
    is_input=True,
    is_output=False,
    description="TabSeparated:制表符分隔,NULL 是 \\N,可无损往返",
    aliases=("TabSeparated",),
)
register_format(
    "TSVWithNames",
    lambda: TSVWithNamesOutputFormat(),
    is_input=False,
    is_output=True,
    description="TabSeparated,首行是列名",
    aliases=("TabSeparatedWithNames",),
)
register_format(
    "TSVWithNames",
    lambda: TSVWithNamesInputFormat(),
    is_input=True,
    is_output=False,
    description="TabSeparated,首行是列名",
    aliases=("TabSeparatedWithNames",),
)
register_format(
    "TSVWithNamesAndTypes",
    lambda: TSVWithNamesAndTypesOutputFormat(),
    is_input=False,
    is_output=True,
    description="TabSeparated,首行是列名,第二行是列类型",
    aliases=("TabSeparatedWithNamesAndTypes",),
)
register_format(
    "TSVWithNamesAndTypes",
    lambda: TSVWithNamesAndTypesInputFormat(),
    is_input=True,
    is_output=False,
    description="TabSeparated,首行是列名,第二行是列类型",
    aliases=("TabSeparatedWithNamesAndTypes",),
)

# ── CSV 家族 ────────────────────────────────────────────────────────────
register_format(
    "CSV",
    lambda: CSVOutputFormat(),
    is_input=False,
    is_output=True,
    description="逗号分隔,RFC-4180 引号,NULL 是 \\N",
)
register_format(
    "CSV",
    lambda: CSVInputFormat(),
    is_input=True,
    is_output=False,
    description="逗号分隔,RFC-4180 引号,NULL 是 \\N",
)
register_format(
    "CSVWithNames",
    lambda: CSVWithNamesOutputFormat(),
    is_input=False,
    is_output=True,
    description="逗号分隔,首行是列名",
)
register_format(
    "CSVWithNames",
    lambda: CSVWithNamesInputFormat(),
    is_input=True,
    is_output=False,
    description="逗号分隔,首行是列名",
)

# ── Values ──────────────────────────────────────────────────────────────
register_format(
    "Values",
    lambda: ValuesOutputFormat(),
    is_input=False,
    is_output=True,
    description="SQL 字面量形式:(1,'a'),(2,'b')",
)
register_format(
    "Values",
    lambda: ValuesInputFormat(),
    is_input=True,
    is_output=False,
    description="SQL 字面量形式:(1,'a'),(2,'b')",
)

# ── JSON 家族 ───────────────────────────────────────────────────────────
register_format(
    "JSONEachRow",
    lambda: JSONEachRowOutputFormat(),
    is_input=False,
    is_output=True,
    description="每行一个 JSON 对象 {\"c\":v,…}",
)
register_format(
    "JSONEachRow",
    lambda: JSONEachRowInputFormat(),
    is_input=True,
    is_output=False,
    description="每行一个 JSON 对象 {\"c\":v,…}",
)
register_format(
    "JSONStringsEachRow",
    lambda: JSONStringsEachRowOutputFormat(),
    is_input=False,
    is_output=True,
    description="每行一个 JSON 对象,所有值都是字符串",
)
register_format(
    "JSONStringsEachRow",
    lambda: JSONStringsEachRowInputFormat(),
    is_input=True,
    is_output=False,
    description="每行一个 JSON 对象,所有值都是字符串",
)
register_format(
    "JSONCompactEachRow",
    lambda: JSONCompactEachRowOutputFormat(),
    is_input=False,
    is_output=True,
    description="每行一个 JSON 数组 [v,…]",
)
register_format(
    "JSONCompactEachRow",
    lambda: JSONCompactEachRowInputFormat(),
    is_input=True,
    is_output=False,
    description="每行一个 JSON 数组 [v,…]",
)
register_format(
    "JSONCompactEachRowWithNames",
    lambda: JSONCompactEachRowWithNamesOutputFormat(),
    is_input=False,
    is_output=True,
    description="首行是列名数组,之后每行一个 JSON 数组",
)
register_format(
    "JSONCompactEachRowWithNames",
    lambda: JSONCompactEachRowWithNamesInputFormat(),
    is_input=True,
    is_output=False,
    description="首行是列名数组,之后每行一个 JSON 数组",
)
register_format(
    "JSON",
    lambda: JSONOutputFormat(),
    is_input=False,
    is_output=True,
    description="整个结果集一个对象:{\"meta\":[…],\"data\":[…],\"rows\":N}",
)
register_format(
    "JSON",
    lambda: JSONInputFormat(),
    is_input=True,
    is_output=False,
    description="整个结果集一个对象:{\"meta\":[…],\"data\":[…],\"rows\":N}",
)
register_format(
    "JSONCompact",
    lambda: JSONCompactOutputFormat(),
    is_input=False,
    is_output=True,
    description="与 JSON 内容相同,但不换行不缩进",
)
register_format(
    "JSONCompact",
    lambda: JSONCompactInputFormat(),
    is_input=True,
    is_output=False,
    description="与 JSON 内容相同,但不换行不缩进",
)

# ── 只输出的"人看"格式 ──────────────────────────────────────────────────
register_format(
    "Pretty",
    lambda: PrettyOutputFormat(),
    is_input=False,
    is_output=True,
    description="带框线、行间有分隔线的表格",
)
register_format(
    "PrettyCompact",
    lambda: PrettyCompactOutputFormat(),
    is_input=False,
    is_output=True,
    description="带框线、没有行间分隔线的表格(默认的交互式输出)",
)
register_format(
    "PrettyCompactMonoBlock",
    lambda: PrettyCompactMonoBlockOutputFormat(),
    is_input=False,
    is_output=True,
    description="同 PrettyCompact,但把所有块合成一张表(内存换对齐)",
)
register_format(
    "PrettySpace",
    lambda: PrettySpaceOutputFormat(),
    is_input=False,
    is_output=True,
    description="用空格代替框线的表格",
)
register_format(
    "Vertical",
    lambda: VerticalOutputFormat(),
    is_input=False,
    is_output=True,
    description="一行一块的竖排输出(列多时好用)",
)
register_format(
    "Markdown",
    lambda: MarkdownOutputFormat(),
    is_input=False,
    is_output=True,
    description="GitHub 风格的 Markdown 表格",
)
register_format(
    "Null",
    lambda: NullOutputFormat(),
    is_input=False,
    is_output=True,
    description="什么都不输出(只测查询本身的开销)",
)
register_format(
    "SQLInsert",
    lambda: SQLInsertOutputFormat(),
    is_input=False,
    is_output=True,
    description="INSERT INTO t (…) VALUES (…),(…);",
)

# ── 二进制 ──────────────────────────────────────────────────────────────
register_format(
    "RowBinary",
    lambda: RowBinaryOutputFormat(),
    is_input=False,
    is_output=True,
    description="逐值小端二进制,按行排列,无元数据",
)
register_format(
    "RowBinary",
    lambda: RowBinaryInputFormat(),
    is_input=True,
    is_output=False,
    description="逐值小端二进制,按行排列,无元数据",
)
register_format(
    "RowBinaryWithNames",
    lambda: RowBinaryWithNamesOutputFormat(),
    is_input=False,
    is_output=True,
    description="RowBinary,前面有列名",
)
register_format(
    "RowBinaryWithNames",
    lambda: RowBinaryWithNamesInputFormat(),
    is_input=True,
    is_output=False,
    description="RowBinary,前面有列名",
)
register_format(
    "RowBinaryWithNamesAndTypes",
    lambda: RowBinaryWithNamesAndTypesOutputFormat(),
    is_input=False,
    is_output=True,
    description="RowBinary,前面有列名与类型(自描述)",
)
register_format(
    "RowBinaryWithNamesAndTypes",
    lambda: RowBinaryWithNamesAndTypesInputFormat(),
    is_input=True,
    is_output=False,
    description="RowBinary,前面有列名与类型(自描述)",
)
register_format(
    "Native",
    lambda: NativeOutputFormat(),
    is_input=False,
    is_output=True,
    description="ClickHouse 的队列/part 格式(按列,带表头)",
)
register_format(
    "Native",
    lambda: NativeInputFormat(),
    is_input=True,
    is_output=False,
    description="ClickHouse 的队列/part 格式(按列,带表头)",
)

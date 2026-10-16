"""文本格式家族:**分隔符格式**(``TSV*`` / ``CSV*``)。

这一族是 ClickHouse 里最有教学价值的一族,因为"怎么把一个值写成一行文本、
又怎么原样读回来"这件事,把格式设计的全部取舍都摊开了。

.. code-block:: text

   TSV                                  CSV
   ┌──────────────────────────────┐     ┌───────────────────────────────┐
   │ 1<TAB>hello<TAB>\\N          │     │ 1,hello,\\N                   │
   │ 2<TAB>"a<TAB>b"<TAB>world    │     │ 2,"a\\tb",world               │
   └──────────────────────────────┘     └───────────────────────────────┘
     字段分隔符 <TAB>                      字段分隔符 ,
     值里出现的 <TAB> 必须转义              值里的 , " 换行 用 RFC-4180 引号包起来
     没有"引号"这个概念,一切靠反斜杠       引号之外还有一层反斜杠转义(见下)

**两者共享同一套值转义**。这不是巧合,而是刻意的设计:mini-clickhouse 的
``TSV`` 与 ``CSV`` 只差两件事 —— **字段分隔符**与**要不要加 RFC-4180 引号**;
值本身怎么变成文本,走的是同一个 :func:`minich.formats.base.render_value_text`。

**NULL 与空串为什么必须分开**(TSV 那一节的问题,CSV 更严重):

.. code-block:: text

   值          TSV 字段       CSV 字段         读回来
   NULL        \\N            \\N              None
   ""  (空串)  (空字段)       ""(引号包着空)   ""
   "\\N"(字面)  \\\\N          \\\\N            "\\N"

CSV **单独靠 RFC-4180 是做不到无损的**:RFC-4180 里"空字段"和"值是空串"
是同一个字节串(都是"两个逗号之间什么都没有"),而且 RFC-4180 根本没有
"NULL"这个概念 —— 数据库的 NULL、空串、字符串 ``"NULL"`` 三者会全部塌成
同一个空字段。所以 ClickHouse 引入了 ``\\N`` 这个**哨兵**:

.. code-block:: text

   没有哨兵时(纯 RFC-4180):       有了哨兵之后:
   a,,b        → ?, "", ?           a,\\N,b      → ?, None, ?
   a,"",b      → ?, "", ?           a,"",b       → ?, "", ?
   ↑ 前两行的输出**完全相同**,信息已经丢了

mini-clickhouse 在哨兵之外还保留了与 TSV 相同的反斜杠转义,于是连
"值恰好等于 ``\\N``"这种边角也能往返(它会被写成 ``\\\\N``)。

.. warning::

   这层反斜杠转义是 **mini-clickhouse 相对真实 ClickHouse 的偏离**。
   真实 ClickHouse 的 ``CSV`` **只做 RFC-4180 引号**,值里的反斜杠原样写出。
   后果有两个方向:

   * 别人用真实 CH 导出、值里含反斜杠的 CSV,用我们的读取器读会被当成
     转义序列(``a\\nb`` 会变成换行)—— 这是**我们读别人的文件**时的风险;
   * 我们用 mini-clickhouse 导出的 CSV 里,含制表符/换行/反斜杠的值
     在真实 CH 里读回来是原样字节(不会解转义)—— 这是**别人读我们的文件**时
     的风险。

   要"完全对齐真实 CH 的 CSV",必须关掉这层转义;但那样一来
   tab/换行/反斜杠就再也无法无损往返。教学实现选择"可往返",并把这个
   取舍写在这里,而不是假装两边一样。
"""

from __future__ import annotations

from typing import Any, Iterator, List, Sequence, Tuple

from ..block import Block, ColumnWithTypeAndName
from ..errors import ErrorCode, raise_error
from ..types import get_data_type
from ..util.strings import escape_csv_field, parse_csv_line
from .base import (
    IInputFormat,
    IOutputFormat,
    NULL_SENTINEL,
    block_from_rows,
    chunk_lines,
    get_setting,
    parse_value_text,
    read_all_text,
    render_value_text,
    require_schema,
    write_text,
)

__all__ = [
    "TSVOutputFormat",
    "TSVInputFormat",
    "CSVOutputFormat",
    "CSVInputFormat",
]


# ══════════════════════════════════════════════════════════════════════════
# 输出
# ══════════════════════════════════════════════════════════════════════════


class _SeparatedTextOutput(IOutputFormat):
    """``TSV`` / ``CSV`` 输出的公共骨架。

    一个 Block 写成"每行一条记录",每条记录里各字段用分隔符连接。
    ``write_prefix`` 负责那两三行表头(可选),``write`` 负责数据,
    ``write_suffix`` 什么都不用做 —— 这也是为什么 TSV 天然是
    **流式**的:第一块数据算完就可以发走,不必等整个结果集。
    """

    field_separator: str = "\t"
    csv_quoting: bool = False
    write_names: bool = False
    write_types: bool = False

    # ── 表头 ────────────────────────────────────────────────────────────

    def write_prefix(self, sample_block: Block, out: Any, settings: Any = None) -> None:
        if self.write_names:
            write_text(out, self._header_line(sample_block.get_names(), settings))
        if self.write_types:
            types = [c.type.get_name() for c in sample_block.data]
            write_text(out, self._header_line(types, settings))

    def _header_line(self, fields: Sequence[str], settings: Any) -> str:
        return self._join_fields(list(fields), settings) + "\n"

    # ── 数据 ────────────────────────────────────────────────────────────

    def write(self, block: Block, out: Any, settings: Any = None) -> None:
        pieces: List[str] = []
        for row in range(block.rows()):
            fields: List[str] = []
            for cwtn in block.data:
                value = cwtn.column.get(row)
                if value is None:
                    # NULL 用哨兵写;**不**参与反斜杠转义,否则哨兵自己会被转义
                    field = NULL_SENTINEL
                else:
                    field = render_value_text(cwtn.type, value, settings, quoted=False)
                fields.append(field)
            pieces.append(self._join_fields(fields, settings))
        if pieces:
            write_text(out, "\n".join(pieces) + "\n")

    def _join_fields(self, fields: List[str], settings: Any) -> str:
        if self.csv_quoting:
            fields = [escape_csv_field(f, self.field_separator) for f in fields]
        return self.field_separator.join(fields)


class TSVOutputFormat(_SeparatedTextOutput):
    """``TSV`` —— 制表符分隔,**全部值都用反斜杠转义**(没有引号这一层)。"""

    name = "TSV"


class TSVWithNamesOutputFormat(TSVOutputFormat):
    """``TSVWithNames`` —— 第一行是列名。"""

    name = "TSVWithNames"
    write_names = True


class TSVWithNamesAndTypesOutputFormat(TSVOutputFormat):
    """``TSVWithNamesAndTypes`` —— 第一行列名,第二行列类型。"""

    name = "TSVWithNamesAndTypes"
    write_names = True
    write_types = True


class CSVOutputFormat(_SeparatedTextOutput):
    """``CSV`` —— 逗号分隔 + RFC-4180 引号。"""

    name = "CSV"
    field_separator = ","
    csv_quoting = True


class CSVWithNamesOutputFormat(CSVOutputFormat):
    """``CSVWithNames`` —— 第一行是列名(同样按 CSV 规则加引号)。"""

    name = "CSVWithNames"
    write_names = True


# ══════════════════════════════════════════════════════════════════════════
# 输入
# ══════════════════════════════════════════════════════════════════════════


class _SeparatedTextInput(IInputFormat):
    """``TSV`` / ``CSV`` 输入的公共骨架。

    步骤与输出**严格对称**:

    .. code-block:: text

       文本 ──► 切记录(records) ──► 切字段(fields) ──► 逐字段按类型解析
              ┌──────────────┐
       表头行 │ 名字 / 类型   │  ← 只有 *WithNames* 变体才有;
              └──────────────┘     读到就校验,对不上直接报错
    """

    field_separator: str = "\t"
    csv_quoting: bool = False
    has_names: bool = False
    has_types: bool = False

    # ── 主流程 ──────────────────────────────────────────────────────────

    def read(self, source: Any, sample_block: Block, settings: Any = None) -> Iterator[Block]:
        text = read_all_text(source)
        records = self._split_records(text)

        block, records = self._consume_header(records, sample_block, settings)
        if block.columns() == 0:
            require_schema(sample_block, self.name)
            block = sample_block

        rows = [self._parse_record(record, block, settings) for record in records]
        block_size = int(get_setting(settings, "max_block_size", 65536) or 65536)
        for chunk in chunk_lines(rows, block_size):
            if chunk:
                yield block_from_rows(block, chunk)

    # ── 表头 ────────────────────────────────────────────────────────────

    def _consume_header(
        self,
        records: List[str],
        sample_block: Block,
        settings: Any,
    ) -> Tuple[Block, List[str]]:
        """吃掉表头行并校验。返回 ``(用于解析的 schema, 剩下的数据行)``。

        校验规则比真实 ClickHouse **更严**:真实 CH 默认按名字把输入列
        映射到表列(允许顺序不同、允许缺列),mini-clickhouse 要求
        名字与类型**逐位相同**。理由很直接 —— 教学实现里"悄悄按名字重排"
        会让往返测试失去意义(数据换了个位置也能"通过")。
        """
        if not self.has_names:
            return sample_block, records

        if not records:
            raise_error(
                ErrorCode.UNEXPECTED_END_OF_FILE,
                f"Format {self.name} expects a header line with column names, "
                f"but the input is empty",
            )
        name_fields = self._split_fields(records[0])
        records = records[1:]

        if self.has_types:
            if not records:
                raise_error(
                    ErrorCode.UNEXPECTED_END_OF_FILE,
                    f"Format {self.name} expects a header line with column types, "
                    f"but the input ended after the names",
                )
            type_fields = self._split_fields(records[0])
            records = records[1:]
        else:
            type_fields = None

        if sample_block.columns() == 0:
            # schema 是空的:**用表头建结构**(这正是 *WithNamesAndTypes 的价值)
            if type_fields is None:
                raise_error(
                    ErrorCode.INCORRECT_NUMBER_OF_COLUMNS,
                    f"Format {self.name} has no column types in its header; "
                    f"a non-empty sample block is required",
                )
            if len(name_fields) != len(type_fields):
                raise_error(
                    ErrorCode.INCORRECT_NUMBER_OF_COLUMNS,
                    f"{self.name} header has {len(name_fields)} names but "
                    f"{len(type_fields)} types",
                )
            return self._schema_from_header(name_fields, type_fields), records

        expected_names = sample_block.get_names()
        if name_fields != expected_names:
            raise_error(
                ErrorCode.TYPE_MISMATCH,
                f"{self.name} header names {name_fields} do not match the "
                f"expected columns {expected_names}",
            )
        if type_fields is not None:
            expected_types = [c.type.get_name() for c in sample_block.data]
            if type_fields != expected_types:
                raise_error(
                    ErrorCode.TYPE_MISMATCH,
                    f"{self.name} header types {type_fields} do not match the "
                    f"expected types {expected_types}. If you changed the table, "
                    f"the file was written with a different schema.",
                )
        return sample_block, records

    @staticmethod
    def _schema_from_header(names: Sequence[str], types: Sequence[str]) -> Block:
        columns = []
        for name, type_name in zip(names, types):
            columns.append(ColumnWithTypeAndName(name, get_data_type(type_name)))
        return Block(columns)

    # ── 记录与字段 ──────────────────────────────────────────────────────

    def _split_records(self, text: str) -> List[str]:
        """文本 → 记录列表(丢掉空行)。子类各自实现,因为 CSV 有引号跨行问题。"""
        raise_error(ErrorCode.NOT_IMPLEMENTED, "_split_records must be overridden")

    def _split_fields(self, record: str) -> List[str]:
        raise_error(ErrorCode.NOT_IMPLEMENTED, "_split_fields must be overridden")

    def _parse_record(self, record: str, block: Block, settings: Any) -> List[Any]:
        fields = self._split_fields(record)
        if len(fields) != block.columns():
            raise_error(
                ErrorCode.INCORRECT_NUMBER_OF_COLUMNS,
                f"{self.name} row has {len(fields)} fields but the block has "
                f"{block.columns()} columns: {record!r}",
            )
        return [
            parse_value_text(cwtn.type, field, settings)
            for cwtn, field in zip(block.data, fields)
        ]


class TSVInputFormat(_SeparatedTextInput):
    """``TSV`` 输入。

    .. code-block:: text

       text.split("\\n") ──► 每条记录 .split("\\t") ──► 按类型解析

    ``TSV`` 里**没有**引号这一层,所以字段切分是"按制表符split"这么简单 ——
    ``\\t`` 在值里一定已经被转义成了反斜杠 + t,不可能撞上分隔符。
    ``\\r\\n`` 行尾也支持(会在切记录时去掉尾部 ``\\r``)。
    """

    name = "TSV"

    def _split_records(self, text: str) -> List[str]:
        """按换行切记录 —— 两条规则都不是随手写的。

        .. warning::

           1. **结尾的那一个换行不算一条记录。** 输出侧每写完一行都补一个
              ``\\n``,所以"切出来最后一个空元素"是**行终止符**留下的,
              不是一行数据。(单列 String 的空串行写出来恰好是一个空行 ——
              不把结尾这个丢掉,行数就会多一条,往返立刻失败。)

           2. **中间的空行是记录。** 单列表里"空串"的合法表示就是一行空白,
              把它洗掉等于**丢数据**。以前这里是 ``if raw != "": …``,
              于是 ``['', 'a']`` 写出去是 ``"\\na\\n"``、读回来只剩 ``['a']``
              (CSV 没这个毛病:空串会被 ``""`` 包起来,记录不是空的)。

        .. note::

           取舍:多列表里混进空行会在 :meth:`_parse_record` 处报
           ``INCORRECT_NUMBER_OF_COLUMNS`` —— 一个空行只有 1 个字段,
           却要往 N 列里塞。**这是故意的**:空行到底是"一条空记录"还是
           "排版用的空行",从字节上看不出来,而静默丢数据比报错危险得多。
        """
        if text == "":
            return []
        lines = text.split("\n")
        if lines[-1] == "":
            lines.pop()  # 行终止符,不是记录
        out: List[str] = []
        for raw in lines:
            if raw.endswith("\r"):
                raw = raw[:-1]
            out.append(raw)
        return out

    def _split_fields(self, record: str) -> List[str]:
        return record.split("\t")


class TSVWithNamesInputFormat(TSVInputFormat):
    """``TSVWithNames`` —— 多读一行列名并校验。"""

    name = "TSVWithNames"
    has_names = True


class TSVWithNamesAndTypesInputFormat(TSVInputFormat):
    """``TSVWithNamesAndTypes`` —— 多读两行(列名 + 列类型),类型不符直接报错。

    .. warning::

       类型校验是**按类型名字符串**比对的(``DateTime('UTC')`` 与
       ``DateTime`` 是两个名字,会被判为不同)。这与真实 ClickHouse
       一致 —— 它读 Native/WithTypes 时同样比类型名。
    """

    name = "TSVWithNamesAndTypes"
    has_names = True
    has_types = True


class CSVInputFormat(_SeparatedTextInput):
    """``CSV`` 输入。

    切记录时要**照顾引号**:RFC-4180 允许引号里出现换行,所以不能简单地
    按 ``\\n`` 切。这里的做法是扫描一遍,记住"当前是否在引号里":

    .. code-block:: text

       "a\\nb",c      ← 引号里的换行属于**同一个字段**
        └──┘  └─ 这里的逗号才是分隔符

    (mini-clickhouse 自己写出的 CSV 永远不会出现引号内的裸换行 ——
    换行在值里已经被转义成 ``\\n`` 两个字符了。这层扫描是为了能读
    **别人生成的**标准 CSV。)
    """

    name = "CSV"
    field_separator = ","
    csv_quoting = True

    def _split_records(self, text: str) -> List[str]:
        records: List[str] = []
        current: List[str] = []
        in_quotes = False
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if in_quotes:
                if ch == '"':
                    # "" 是一转义,不算结束
                    if i + 1 < n and text[i + 1] == '"':
                        current.append('""')
                        i += 2
                        continue
                    in_quotes = False
                current.append(ch)
                i += 1
                continue
            if ch == '"':
                in_quotes = True
                current.append(ch)
                i += 1
                continue
            if ch == "\n":
                records.append("".join(current))
                current = []
                i += 1
                continue
            if ch == "\r" and i + 1 < n and text[i + 1] == "\n":
                records.append("".join(current))
                current = []
                i += 2
                continue
            current.append(ch)
            i += 1
        if current:
            records.append("".join(current))
        out = []
        for record in records:
            if record.endswith("\r"):
                record = record[:-1]
            if record != "":
                out.append(record)
        return out

    def _split_fields(self, record: str) -> List[str]:
        return parse_csv_line(record, self.field_separator)


class CSVWithNamesInputFormat(CSVInputFormat):
    """``CSVWithNames`` —— 第一行列名。"""

    name = "CSVWithNames"
    has_names = True

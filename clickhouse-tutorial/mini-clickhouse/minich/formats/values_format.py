"""``Values`` 格式 —— ClickHouse 的"SQL 字面量"表示法。

.. code-block:: text

   (1,'hello',NULL),(2,'a\\tb',[1,2]),…

   每一个元组 = 一行;元组内的字段按**列的顺序**排列。
   它与 ``INSERT INTO t VALUES (…),(…)`` 用的是**同一套语法**
   (都由 :mod:`minich.literals` 兜底),所以:

   .. code-block:: sql

      SELECT * FROM t FORMAT Values    -- 输出
      INSERT INTO t2 FORMAT Values     -- 同样的文本可以喂回去

   这正是"格式可往返"最直观的体现:**导出即导入**。

**和 TSV 的关键区别:这里有引号**。TSV 靠"反斜杠 + 分隔符"这一层转义,
``Values`` 靠的是 **SQL 字面量**:字符串写成 ``'…'``、NULL 写成 ``NULL``、
数组写成 ``[…]``、元组写成 ``(…)``、Map 写成 ``{…}``。

.. code-block:: text

   值               Values 写法         TSV 写法
   NULL            NULL                \\N
   ""              ''                  (空字段)
   "it's"          'it\\'s'             it's
   [1,2]           [1,2]               [1,2]
   {'k':1}         {'k':1}             {'k':1}

.. warning::

   **这层引号是有代价的**:字段里出现引号/反斜杠就要转义,而
   "转义规则" 与 :mod:`minich.literals` 的解码规则必须逐条对称
   (见 :func:`minich.formats.base._escape_quoted`)。转义表里多一条、
   少一条,往返测试都能立刻抓出来 —— 这就是第 17 章"用往返测序列化"
   的具体用法。

.. note::

   ``Values`` 的字段渲染走的是 ``serialize_text_quoted`` 这一族
   (顶层标量也加引号),但 ``Map`` 的**键**在真实 ClickHouse 里是带引号的
   (``{'k':1}``),而 mini-clickhouse 的 ``DataTypeMap.serialize_text``
   给出的是 ``{k:1}``。那个形式无法被解析回来,所以这里对容器做了一层
   显式渲染(见 :func:`minich.formats.base.render_value_text`)。
"""

from __future__ import annotations

from typing import Any, Iterator, List

from ..block import Block
from ..errors import ErrorCode, raise_error
from ..literals import split_top_level
from .base import (
    IInputFormat,
    IOutputFormat,
    block_from_rows,
    chunk_lines,
    get_setting,
    parse_values_field,
    read_all_text,
    render_quoted_value,
    write_text,
)

__all__ = ["ValuesOutputFormat", "ValuesInputFormat"]


class ValuesOutputFormat(IOutputFormat):
    """``Values`` 输出。

    .. code-block:: text

       写出的文本 = 若干个 "(" + 字段 , 字段 , … + ")" 用 "," 连接 + "\\n"

       ┌─ 行 ─┐        ┌─ 行 ─┐
       (1,'a',NULL),(2,'b','c')
       └──┬──┘       └──┬──┘
          └── 行之间用逗号 ──┘   ← 没有尾随逗号

    **流式状态**:行与行之间的那个逗号需要知道"之前写过行没有",所以格式
    实例会记住一个 ``_wrote_row`` 标志。``write_prefix`` 会把它清掉 ——
    也就是说,**要用同一个实例输出第二份结果,必须先调 write_prefix**。
    (真实 ClickHouse 的 ``ValuesRowOutputFormat`` 同样有这类状态,
    只不过它由 pipeline 的生命周期保证一定会被重置。)
    """

    name = "Values"

    def __init__(self) -> None:
        self._wrote_row = False

    def write_prefix(self, sample_block: Block, out: Any, settings: Any = None) -> None:
        self._wrote_row = False

    def write(self, block: Block, out: Any, settings: Any = None) -> None:
        pieces: List[str] = []
        for row in range(block.rows()):
            fields = [
                render_quoted_value(cwtn.type, cwtn.column.get(row), settings)
                for cwtn in block.data
            ]
            pieces.append("(" + ",".join(fields) + ")")
        if not pieces:
            return
        text = ",".join(pieces)
        if self._wrote_row:
            text = "," + text
        self._wrote_row = True
        write_text(out, text)

    def write_suffix(self, sample_block: Block, out: Any, settings: Any = None) -> None:
        # 真实 ClickHouse 的 Values 输出以换行收尾(方便直接 append 到脚本里)。
        if self._wrote_row:
            write_text(out, "\n")


class ValuesInputFormat(IInputFormat):
    """``Values`` 输入。

    解析分三步,每一步都复用 :mod:`minich.literals` 的切分器 ——
    它认识引号与嵌套括号,所以 ``(1,'a,b'),(2,'c')`` 不会在字符串里的
    那个逗号上被切开:

    .. code-block:: text

       "(1,'a,b'),(2,'c')"
            │  split_top_level(text, ",")          ← 顶层逗号 → 三行里的一行
            ▼
       ["(1,'a,b')", "(2,'c')"]
            │  去掉外层圆括号 → 再 split_top_level(",")   ← 字段
            ▼
       ["1", "'a,b'"]
            │  parse_values_field(类型, 字段文本)          ← 按类型解析
            ▼
       [1, "a,b"]

    .. note::

       空输入(``""``)会产出**零个** Block,而不是"一行空数据"。
       "没有行"和"有一行全是默认值"是两件不同的事,不能混。
    """

    name = "Values"

    def read(self, source: Any, sample_block: Block, settings: Any = None) -> Iterator[Block]:
        if sample_block.columns() == 0:
            raise_error(
                ErrorCode.INCORRECT_NUMBER_OF_COLUMNS,
                "Format Values needs a non-empty sample block to know the column types",
            )
        text = read_all_text(source).strip()
        if not text:
            return
        rows: List[List[Any]] = []
        for record in split_top_level(text, ","):
            if record == "":
                continue
            rows.append(self._parse_record(record, sample_block, settings))
        block_size = int(get_setting(settings, "max_block_size", 65536) or 65536)
        for chunk in chunk_lines(rows, block_size):
            if chunk:
                yield block_from_rows(sample_block, chunk)

    @staticmethod
    def _parse_record(record: str, sample_block: Block, settings: Any) -> List[Any]:
        body = record.strip()
        if not (body.startswith("(") and body.endswith(")")):
            raise_error(
                ErrorCode.CANNOT_PARSE_TEXT,
                f"Values row must be a parenthesised tuple, got {record!r}",
            )
        fields = split_top_level(body[1:-1], ",")
        if len(fields) != sample_block.columns():
            raise_error(
                ErrorCode.INCORRECT_NUMBER_OF_COLUMNS,
                f"Values row {record!r} has {len(fields)} field(s) but the block "
                f"has {sample_block.columns()} column(s)",
            )
        return [
            parse_values_field(cwtn.type, field, settings)
            for cwtn, field in zip(sample_block.data, fields)
        ]

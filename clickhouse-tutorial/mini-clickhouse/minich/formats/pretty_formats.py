"""人看的格式:``Pretty*`` / ``Vertical`` / ``Markdown`` / ``Null`` / ``SQLInsert``。

这些格式**只能输出**(没有"读一个 Pretty 表格回来"这种事 —— 一旦画上
框线,列边界就依赖等宽字体,解析它比解析 HTML 还难)。所以它们全部
注册成 ``is_input=False``。

.. code-block:: text

   Pretty / PrettyCompact / PrettySpace 的画法
   (以 SELECT 1 AS id, 'hello' AS name 为例,并假设还有第二行 2/world)

     PrettyCompact                    Pretty                    PrettySpace
     ┌─id─┬─name──┐                  ┌─id─┬─name──┐             id   name
     │  1 │ hello │                  │  1 │ hello │
     │  2 │ world │                  ├────┼───────┤               1   hello
     └────┴───────┘                  │  2 │ world │               2   world
                                     └────┴───────┘
     行之间没有分隔线                 每行之间有 ─┼─           没有框线,用空格

   三个格式共用同一套"列宽"与"对齐"规则,差别只在**画不画分隔线**:

   * 列宽 = max(列名字符数, 第一块里该列所有单元格的字符数);
   * **数值右对齐**(:func:`minich.formats.base.is_right_aligned`),其余左对齐;
   * 单元格左右各留一个空格,所以每列实际占 ``列宽 + 2`` 个字符;
   * 表头就是最上面那条框线(名字嵌在 ``─`` 中间),不额外占一行。

.. warning::

   **列宽是按"字符数"算的,不是"显示宽度"**。中文/emoji 在等宽字体里
   通常占两格,于是含 CJK 的表格在终端里会看起来对不齐。
   真实 ClickHouse 也是这样(它按 UTF-8 码点数补齐),所以这不是偏离 ——
   但知道这一点能省下十分钟的困惑。

   同理,**单元格里的值会被转义**(制表符写成 ``\\t``、换行写成 ``\\n``),
   这样表格不会被一个值撑破。真实 ClickHouse 是**原样**打印的,
   含换行的字符串会直接把表格撕开 —— 这是 mini-clickhouse 有意的一处偏离。

.. note::

   ``Pretty`` 的输出里**没有** ``"2 rows in set. Elapsed: 0.001 sec."``
   这一行。那行是 ``clickhouse-client`` 打印的,不是格式的一部分 ——
   格式层只负责"表格本身"。这一点很容易搞混,因为它总是出现在同一个
   屏幕里。

.. note::

   **行号**:新版 ClickHouse 的 ``Pretty*`` 默认会在最左边多一列行号
   (``output_format_pretty_row_numbers=1``)。mini-clickhouse 不画行号,
   这样输出是确定且干净的(测试里也好比对)。
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence

from ..block import Block
from ..util.strings import quote_identifier
from .base import (
    IOutputFormat,
    get_setting,
    is_right_aligned,
    render_quoted_value,
    render_value_text,
    write_text,
)

__all__ = [
    "PrettyOutputFormat",
    "PrettyCompactOutputFormat",
    "PrettyCompactMonoBlockOutputFormat",
    "PrettySpaceOutputFormat",
    "VerticalOutputFormat",
    "MarkdownOutputFormat",
    "NullOutputFormat",
    "SQLInsertOutputFormat",
]


# ══════════════════════════════════════════════════════════════════════════
# 表格的"形状"计算 —— 三个 Pretty 格式共用
# ══════════════════════════════════════════════════════════════════════════


class _TableShape:
    """一个 Block 的"表格形状":列名、列宽、对齐方式、所有单元格的文本。

    把这四样东西一次算出来,是因为画表格的代码要反复查它们(算边框、
    算分隔线、写单元格)。真实 ClickHouse 的 ``PrettyBlockOutputFormat``
    里对应的是 ``column_names`` / ``column_widths`` / ``alignments`` 三张表。
    """

    __slots__ = ("names", "widths", "right_aligned", "rows")

    def __init__(self, block: Block, settings: Any = None) -> None:
        self.names = block.get_names()
        self.right_aligned = [is_right_aligned(c.type) for c in block.data]
        self.rows: List[List[str]] = []
        for r in range(block.rows()):
            self.rows.append(
                [
                    render_value_text(c.type, c.column.get(r), settings, quoted=False)
                    for c in block.data
                ]
            )
        self.widths: List[int] = []
        for i, name in enumerate(self.names):
            candidates = [len(name)] + [len(row[i]) for row in self.rows]
            self.widths.append(max(candidates))


def _join_segments(left: str, mid: str, right: str, parts: Sequence[str]) -> str:
    return left + mid.join(parts) + right


class _PrettyBase(IOutputFormat):
    """``Pretty`` 家族的公共行为:表头只在第一块画一次,表尾在 suffix 画。

    .. code-block:: text

       第一块:  write(block0)   ← 画 ┌─┬─┐(名字嵌在里面),再写数据行
       后续块:  write(block1)   ← 只追加数据行(必要时先补一条分隔线)
       收尾:    write_suffix()  ← 画 └─┴─┘

    列宽**只由第一块决定**。这不是偷懒:流式输出时后面的块还没算出来,
    而"等到全部算完再画"就不是流式了。真实 ClickHouse 同样如此 ——
    所以一张 Pretty 表里如果后面某块的字符串特别长,它会**溢出**列宽
    (``ljust`` 不会截断,只会让那一格变宽)。
    """

    separators = False

    def __init__(self) -> None:
        self._names: List[str] = []
        self._widths: List[int] = []
        self._right: List[bool] = []
        self._wrote_header = False
        self._wrote_row = False

    # ── 生命周期 ────────────────────────────────────────────────────────

    def write_prefix(self, sample_block: Block, out: Any, settings: Any = None) -> None:
        self._names = []
        self._widths = []
        self._right = []
        self._wrote_header = False
        self._wrote_row = False

    def write(self, block: Block, out: Any, settings: Any = None) -> None:
        if block.columns() == 0:
            return
        shape = _TableShape(block, settings)
        if not self._wrote_header:
            self._names = shape.names
            self._widths = shape.widths
            self._right = shape.right_aligned
            write_text(out, self._header_text())
            self._wrote_header = True
        for row in range(len(shape.rows)):
            if self._wrote_row and self.separators:
                write_text(out, self._separator_text())
            write_text(out, self._row_text(shape, row))
            self._wrote_row = True

    def write_suffix(self, sample_block: Block, out: Any, settings: Any = None) -> None:
        if self._wrote_header:
            write_text(out, self._footer_text())
        self._wrote_header = False
        self._wrote_row = False

    # ── 零件(子类按需覆盖)────────────────────────────────────────────

    def _header_text(self) -> str:
        """顶上那条框线 —— 列名就嵌在里面。"""
        parts = [
            "─" + name + "─" * (width + 1 - len(name))
            for name, width in zip(self._names, self._widths)
        ]
        return _join_segments("┌", "┬", "┐", parts) + "\n"

    def _footer_text(self) -> str:
        return _join_segments("└", "┴", "┘", ["─" * (w + 2) for w in self._widths]) + "\n"

    def _separator_text(self) -> str:
        return _join_segments("├", "┼", "┤", ["─" * (w + 2) for w in self._widths]) + "\n"

    def _row_text(self, shape: _TableShape, row: int) -> str:
        cells = [" " + self._pad(shape.rows[row][c], c) + " " for c in range(len(self._names))]
        return _join_segments("│", "│", "│", cells) + "\n"

    def _pad(self, text: str, col: int) -> str:
        width = self._widths[col]
        return text.rjust(width) if self._right[col] else text.ljust(width)


class PrettyOutputFormat(_PrettyBase):
    """``Pretty`` —— 每行之间有 ``├─┼─┤`` 分隔线的完整表格。"""

    name = "Pretty"
    separators = True


class PrettyCompactOutputFormat(_PrettyBase):
    """``PrettyCompact`` —— **去掉行间分隔线**的紧凑表格。

    .. code-block:: text

       ┌─id─┬─name──┐
       │  1 │ hello │     ← 行与行之间没有 ├─┼─┤
       │  2 │ world │
       └────┴───────┘

    结果集大时它明显更好读(也少画一半的字符)。
    """

    name = "PrettyCompact"


class PrettySpaceOutputFormat(_PrettyBase):
    """``PrettySpace`` —— 用**空格**代替框线。

    适合"要把结果贴进 Markdown/邮件"的场合:没有 ``│`` 这类字符,
    复制出去不会变成乱码。

    .. code-block:: text

       id   name

        1   hello
        2   world
    """

    name = "PrettySpace"

    def _header_text(self) -> str:
        line = "   ".join(
            self._pad(name, i) for i, name in enumerate(self._names)
        )
        # 表头后面空一行 —— 没有框线时,这是"表头"与"数据"唯一的分界。
        return line + "\n\n"

    def _footer_text(self) -> str:
        return ""

    def _row_text(self, shape: _TableShape, row: int) -> str:
        return (
            "   ".join(self._pad(shape.rows[row][c], c) for c in range(len(self._names))) + "\n"
        )


class PrettyCompactMonoBlockOutputFormat(PrettyCompactOutputFormat):
    """``PrettyCompactMonoBlock`` —— 和 ``PrettyCompact`` 一样,但**先全部缓冲**。

    区别只在多块时的表现:

    .. code-block:: text

       PrettyCompact:               PrettyCompactMonoBlock:
       ┌─a─┐    ┌─a─┐               ┌─a─┐
       │ 1 │    │ 3 │               │ 1 │
       └───┘    └───┘               │ 3 │
       (每块一张表)                  └───┘
                                    (所有块合成一张表,列宽按**全部数据**算)

    代价很直接:**内存**。列宽要等所有块到齐才能定,所以它把整张表留在
    内存里 —— 大结果集下这是会 OOM 的。``MonoBlock`` 这个名字里的
    "Mono" 就是指"强制合成一个 Block"。
    """

    name = "PrettyCompactMonoBlock"

    def __init__(self) -> None:
        super().__init__()
        self._blocks: List[Block] = []

    def write(self, block: Block, out: Any, settings: Any = None) -> None:
        if block.columns() == 0:
            return
        self._blocks.append(block)

    def write_suffix(self, sample_block: Block, out: Any, settings: Any = None) -> None:
        if not self._blocks:
            return
        merged = self._blocks[0].clone_empty()
        for block in self._blocks:
            merged.merge(block)
        self._blocks = []
        # 合成一块之后再走 PrettyCompact 的正常路径 —— 列宽自然就正确了
        super().write(merged, out, settings)
        super().write_suffix(sample_block, out, settings)


# ══════════════════════════════════════════════════════════════════════════
# Vertical
# ══════════════════════════════════════════════════════════════════════════


class VerticalOutputFormat(IOutputFormat):
    """``Vertical`` —— **一行一块**的竖排输出。

    .. code-block:: text

       Row 1:
       ──────
       id:   1
       name: hello

       Row 2:
       ──────
       id:   2
       name: world

    什么时候用它:一行里有 30 个列的时候。横排会折行折到看不清,
    竖排则"一个字段一行",而且**不用算列宽** —— 这也是它比 Pretty 简单
    得多的原因(只有字段名的 padding,没有对齐问题)。

    .. note::

       分隔线 ``──────`` 的长度 = ``"Row N:"`` 的长度,所以第 10 行起
       分隔线会跟着变长。真实 ClickHouse 用的是固定长度的 ``──────``;
       这里让它跟着行号走,纯属为了观感统一。

    .. warning::

       竖排输出**没有表头**(不打印列名一行),列名直接作为字段前缀。
       所以它的行数与屏幕高度成正比 —— 100 万行结果是 300 万行输出,
       这是它的使用场景(看几行)决定的,不要拿它导出大数据。
    """

    name = "Vertical"

    def __init__(self) -> None:
        self._row_index = 0

    def write_prefix(self, sample_block: Block, out: Any, settings: Any = None) -> None:
        self._row_index = 0

    def write(self, block: Block, out: Any, settings: Any = None) -> None:
        if block.columns() == 0:
            return
        name_width = max((len(c.name) for c in block.data), default=0)
        pieces: List[str] = []
        for row in range(block.rows()):
            self._row_index += 1
            header = f"Row {self._row_index}:"
            pieces.append(header + "\n" + "─" * len(header) + "\n")
            for c in block.data:
                value = render_value_text(c.type, c.column.get(row), settings, quoted=False)
                pieces.append(c.name.ljust(name_width) + ": " + value + "\n")
            pieces.append("\n")
        write_text(out, "".join(pieces))


# ══════════════════════════════════════════════════════════════════════════
# Markdown
# ══════════════════════════════════════════════════════════════════════════


class MarkdownOutputFormat(IOutputFormat):
    """``Markdown`` —— GitHub 风格表格。

    .. code-block:: text

       | id | name  |
       | -- | ----- |
       |  1 | hello |
       |  2 | world |

    与 ``Pretty`` 共用"数值右对齐"的规则,但有三处不同:

    * 每列宽度至少 **3** —— Markdown 的分隔行要求 ``-`` 至少三个,
      否则很多解析器会把 ``| -- |`` 当成普通文本;
    * 分隔行用 ``-`` 而不是 ``─``(Markdown 不认后者);
    * 没有上下框线。

    .. warning::

       值里含 ``|`` 的单元格**会破坏表格**。Markdown 没有统一的 ``|``
       转义方案(``\\|`` 依赖解析器实现),所以这里原样写出。
       结论:Markdown 可以当**展示**格式,不要当**数据交换**格式 ——
       那个场景请用 TSV 或 JSONEachRow。
    """

    name = "Markdown"

    def __init__(self) -> None:
        self._wrote_header = False

    def write_prefix(self, sample_block: Block, out: Any, settings: Any = None) -> None:
        self._wrote_header = False

    def write(self, block: Block, out: Any, settings: Any = None) -> None:
        if block.columns() == 0:
            return
        shape = _TableShape(block, settings)
        widths = [max(w, 3) for w in shape.widths]

        pieces: List[str] = []
        if not self._wrote_header:
            self._wrote_header = True
            pieces.append(
                "| "
                + " | ".join(shape.names[i].ljust(widths[i]) for i in range(len(shape.names)))
                + " |\n"
            )
            pieces.append(
                "| " + " | ".join("-" * widths[i] for i in range(len(shape.names))) + " |\n"
            )
        for row in range(len(shape.rows)):
            cells = []
            for c in range(len(shape.names)):
                text = shape.rows[row][c]
                cells.append(text.rjust(widths[c]) if shape.right_aligned[c] else text.ljust(widths[c]))
            pieces.append("| " + " | ".join(cells) + " |\n")
        write_text(out, "".join(pieces))


# ══════════════════════════════════════════════════════════════════════════
# Null
# ══════════════════════════════════════════════════════════════════════════


class NullOutputFormat(IOutputFormat):
    """``Null`` —— 把一切丢掉。

    它听起来没用,实际上是**性能测试的必需品**:``SELECT … FORMAT Null``
    能让引擎把整条流水线跑完(读、过滤、聚合都做),却不用把结果变成文本
    或发过网络。于是"查询本身要多久"和"输出要多久"就能分开了 ——
    这也是 ``clickhouse-benchmark`` 默认用的格式。

    .. code-block:: text

       SELECT sum(x) FROM big_table FORMAT Null   -- 只测查询,不测输出

    实现上它什么都不写(连表头都不写),但**仍然会收到每一个 Block** ——
    这正是我们想要的:Block 被产出了,说明数据真的被算出来了。
    """

    name = "Null"

    def write(self, block: Block, out: Any, settings: Any = None) -> None:
        # 故意什么都不做。保留形参是为了接口一致,也提醒读者:
        # "丢弃"是一个**显式**的行为,不是"忘了实现"。
        return


# ══════════════════════════════════════════════════════════════════════════
# SQLInsert
# ══════════════════════════════════════════════════════════════════════════


class SQLInsertOutputFormat(IOutputFormat):
    """``SQLInsert`` —— 输出成一条可以直接执行的 ``INSERT`` 语句。

    .. code-block:: text

       INSERT INTO hits (id, url) VALUES (1,'/a'),(2,'/b');

    它和 ``Values`` 共用同一套字面量渲染(都是 SQL 字面量),区别只在
    **外面裹了一层 ``INSERT INTO … VALUES``**。

    **表名从哪来?** 真实 ClickHouse 从查询的 AST 里取,格式层是拿不到的
    —— mini-clickhouse 里没有"当前查询"这个上下文。所以按这个顺序找:

    1. 实例属性 ``format.table_name``(调用方可以自己设);
    2. ``settings["table"]``(或 ``settings.table``);
    3. 都没有 → 用占位名 ``table``。

    .. code-block:: python

       fmt = get_output_format("SQLInsert")
       fmt.table_name = "hits"                       # 最直接
       fmt.write_prefix(block.clone_empty(), out, settings={"table": "hits"})

    .. warning::

       ``;`` 与换行写在 ``write_suffix`` 里。这意味着**中途抛异常时
       输出不是合法 SQL** —— 这正是"流式输出要自己负责收尾"的代价。
       真实 ClickHouse 也一样(它边写边发,收尾时才发现查询失败)。
    """

    name = "SQLInsert"

    def __init__(self) -> None:
        self.table_name: Optional[str] = None
        self._wrote_row = False

    def write_prefix(self, sample_block: Block, out: Any, settings: Any = None) -> None:
        self._wrote_row = False
        table = self.table_name or get_setting(settings, "table", None) or "table"
        columns = ", ".join(quote_identifier(c.name) for c in sample_block.data)
        write_text(out, f"INSERT INTO {quote_identifier(str(table))} ({columns}) VALUES ")

    def write(self, block: Block, out: Any, settings: Any = None) -> None:
        pieces: List[str] = []
        for row in range(block.rows()):
            fields = [
                render_quoted_value(c.type, c.column.get(row), settings) for c in block.data
            ]
            pieces.append("(" + ",".join(fields) + ")")
        if not pieces:
            return
        text = ",\n".join(pieces)
        if self._wrote_row:
            text = ",\n" + text
        self._wrote_row = True
        write_text(out, text)

    def write_suffix(self, sample_block: Block, out: Any, settings: Any = None) -> None:
        write_text(out, ";\n")

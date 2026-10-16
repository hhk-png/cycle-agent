"""``Block`` / ``Chunk`` —— 在流水线上流动的单位。

这是整个引擎最容易被误解的一对概念,所以先把它们分清楚:

.. code-block:: text

   Block   = 元数据(有哪些列、叫什么、什么类型) + 可选的数据
   Chunk   = 元数据(一个 Block,但列是空的) + 真正的数据(IColumn 列表)

   ┌─────────────────────────────────────────────────────────────┐
   │ Chunk                                                       │
   │  ┌── header: Block ──────────────┐   ┌── columns ────────┐ │
   │  │ id     UInt64   (空列)         │   │ [1,2,3,…,65536]   │ │
   │  │ name   String   (空列)         │   │ ['a','b',…]       │ │
   │  │ ts     DateTime (空列)         │   │ [1700000000,…]    │ │
   │  └───────────────────────────────┘   └───────────────────┘ │
   │  num_rows = 65536                                           │
   └─────────────────────────────────────────────────────────────┘

为什么要这么分? 因为**元数据要被复用,数据要被替换**:

* 一个 100 GB 的表有 100 万个 chunk,但它们的 header **完全相同** ——
  复用同一个 ``Block`` 意味着每一行不必重复存"这一列叫 id、类型是 UInt64";
* 处理器之间传递的是 ``Chunk``,接收方第一件事是拿 header 校验
  "列数、列名、类型对不对" —— 这一步在真实 ClickHouse 里会
  直接抛 ``LOGICAL_ERROR``,因为**类型不匹配的 chunk 流下去
  一定会产生错误结果或者崩溃**。

.. note::

   老版本的 ClickHouse 用的是"带数据的 Block"(没有 Chunk 这个概念),
   2019 年引入 ``Chunk`` 之后,``Block`` 就退化成 header 了。
   你如果读 2020 年之前的博客,会看到大量"Block 就是一批数据"的说法 ——
   那些内容现在已经过时。mini-clickhouse 用的是新模型。

**Block 的另一个身份:结果集**。``SELECT`` 的最终输出、``system.tables``
的每一行、``INSERT`` 的输入,在引擎内部都是一个 Block 序列。
所以格式输出器(``FORMAT Pretty`` / ``JSONEachRow``)的输入是
``Iterable[Block]``,而不是"一个二维数组" —— 这也是为什么
ClickHouse 能做**流式输出**:第一块数据算完就能往客户端发,
不必等整个结果集(第 17 章)。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .columns import IColumn
from .errors import ErrorCode, MiniChError, raise_error
from .types.base import DataType

__all__ = ["ColumnWithTypeAndName", "BlockInfo", "Block", "Chunk"]


class ColumnWithTypeAndName:
    """一列 = **名字 + 类型 + 数据**。

    真实 ClickHouse 里这个结构体叫 ``ColumnWithTypeAndName``,
    有一个重要的细节:它的 ``type`` 是**权威**,``column`` 只是
    "当前的物化方式"。所以同一个 CWTN 在不同执行阶段可以是

    * ``type=UInt64, column=ColumnVector`` —— 普通读;
    * ``type=UInt64, column=ColumnConst`` —— 常量折叠后;
    * ``type=UInt64, column=ColumnSparse`` —— 稀疏编码;

    而对外一律声明成 ``UInt64``。**判断列"是什么"永远看 type,
    判断列"怎么存"才看 column。**
    """

    __slots__ = ("name", "type", "column")

    def __init__(self, name: str, type: DataType, column: Optional[IColumn] = None) -> None:
        self.name = name
        self.type = type
        self.column = column if column is not None else type.create_column()

    def clone_empty(self) -> "ColumnWithTypeAndName":
        return ColumnWithTypeAndName(self.name, self.type, self.column.clone_empty())

    def is_empty(self) -> bool:
        """是否是"只有元数据"的空壳(没有数据行)。"""
        return self.column.size() == 0

    def __repr__(self) -> str:  # pragma: no cover
        return f"{self.name} {self.type} (rows={self.column.size()})"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, ColumnWithTypeAndName)
            and self.name == other.name
            and self.type.equals(other.type)
        )

    def __hash__(self) -> int:
        return hash((self.name, self.type.get_name()))


class BlockInfo:
    """Block 上的"附加信息"。

    真实 ClickHouse 的 ``BlockInfo`` 有字段:

    * ``is_overflows`` / ``bucket_num`` —— 用于 ``GROUP BY ... WITH TOTALS``
      与小数据量下"桶"式聚合(见第 12 章的两级聚合);
    * 在 ``Distributed`` 查询里,这两个字段会被**序列化进网络包**,
      远程节点据此把部分聚合结果发到正确的归并节点。
    """

    __slots__ = ("is_overflows", "bucket_num")

    def __init__(self, is_overflows: bool = False, bucket_num: int = -1) -> None:
        self.is_overflows = is_overflows
        self.bucket_num = bucket_num

    def clone(self) -> "BlockInfo":
        return BlockInfo(self.is_overflows, self.bucket_num)

    def __repr__(self) -> str:  # pragma: no cover
        return f"BlockInfo(overflows={self.is_overflows}, bucket={self.bucket_num})"


class Block:
    """一批**同构**的列 —— 既是"数据块",也是"header"。

    同一个类承担两个角色是刻意的:header 就是"把数据清空的 Block",
    所以 ``block.clone_empty()`` 得到的既是"这一批列的类型",也是
    "下一批数据该长什么样"。

    行数约定:块内所有列的行数**必须相同**,否则整个引擎的语义就崩了。
    ``rows()`` 返回第一列的行数;``check_number_of_rows()`` 会校验全部列。
    真实 ClickHouse 在 ``Chunk`` 构造时做这个校验(mini-clickhouse 同理)。
    """

    __slots__ = ("info", "data")

    def __init__(
        self,
        columns: Optional[Sequence[ColumnWithTypeAndName]] = None,
        info: Optional[BlockInfo] = None,
    ) -> None:
        self.info = info if info is not None else BlockInfo()
        self.data: List[ColumnWithTypeAndName] = list(columns) if columns else []

    # ── 基本属性 ────────────────────────────────────────────────────────

    def rows(self) -> int:
        """行数 —— 没有列时返回 0。"""
        return self.data[0].column.size() if self.data else 0

    def columns(self) -> int:
        return len(self.data)

    def is_empty(self) -> bool:
        return not self.data

    def check_number_of_rows(self) -> None:
        """校验所有列行数一致 —— **引擎里每一步之后都应当能通过。**"""
        if not self.data:
            return
        n = self.data[0].column.size()
        for c in self.data[1:]:
            if c.column.size() != n:
                raise_error(
                    ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH,
                    f"Block is not consistent: column '{self.data[0].name}' has {n} rows "
                    f"but column '{c.name}' has {c.column.size()}",
                )

    # ── 按位置 / 名字取列 ───────────────────────────────────────────────

    def get_by_position(self, i: int) -> ColumnWithTypeAndName:
        if i < 0 or i >= len(self.data):
            raise_error(
                ErrorCode.NOT_FOUND_COLUMN_IN_BLOCK,
                f"Position {i} is out of bound in block with {len(self.data)} columns",
            )
        return self.data[i]

    def get_by_name(self, name: str) -> ColumnWithTypeAndName:
        c = self.try_get_by_name(name)
        if c is None:
            raise_error(
                ErrorCode.NOT_FOUND_COLUMN_IN_BLOCK,
                f"There is no column with name '{name}' in block. "
                f"Available: {', '.join(c.name for c in self.data) or '(none)'}",
            )
        return c

    def try_get_by_name(self, name: str) -> Optional[ColumnWithTypeAndName]:
        for c in self.data:
            if c.name == name:
                return c
        return None

    def get_position_by_name(self, name: str) -> int:
        for i, c in enumerate(self.data):
            if c.name == name:
                return i
        return -1

    def has(self, name: str) -> bool:
        return self.get_position_by_name(name) >= 0

    def __contains__(self, name: str) -> bool:
        return self.has(name)

    # ── 增删 ────────────────────────────────────────────────────────────

    def add_column(self, cwtn: ColumnWithTypeAndName) -> None:
        """追加一列 —— 新列的行数必须与已有列一致。"""
        if self.data and cwtn.column.size() != self.rows():
            raise_error(
                ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH,
                f"Cannot add column '{cwtn.name}' with {cwtn.column.size()} rows "
                f"to a block with {self.rows()} rows",
            )
        self.data.append(cwtn)

    def insert_column(self, index: int, cwtn: ColumnWithTypeAndName) -> None:
        self.data.insert(index, cwtn)

    def erase(self, name: str) -> None:
        pos = self.get_position_by_name(name)
        if pos >= 0:
            del self.data[pos]

    def resize(self, n: int) -> None:
        """把每一列都调整到 ``n`` 行(少了补默认值)。"""
        for c in self.data:
            c.column.resize(n)

    def clear(self) -> None:
        for c in self.data:
            c.column = c.column.clone_empty()

    # ── 复制 ────────────────────────────────────────────────────────────

    def clone_empty(self) -> "Block":
        """同结构、零行的新 Block(= header)。"""
        return Block(
            [c.clone_empty() for c in self.data],
            self.info.clone(),
        )

    def clone_empty_without_columns(self) -> "Block":
        """连列都不要,只要 ``BlockInfo`` —— 序列化前的 header 用。"""
        return Block([], self.info.clone())

    def clone_without_columns(self) -> "Block":
        return self.clone_empty()

    def clone_with_columns(self) -> "Block":
        """**深拷贝数据和结构**(O(数据量))。慎用:引擎里的默认约定是共享。"""
        out = self.clone_empty()
        for src, dst in zip(self.data, out.data):
            dst.column = src.column.clone()
        return out

    def clone_without_column(self, name: str) -> "Block":
        return Block(
            [c.clone_empty() for c in self.data if c.name != name],
            self.info.clone(),
        )

    def clone_with_column(self, cwtn: ColumnWithTypeAndName) -> "Block":
        out = self.clone_empty()
        for c in out.data:
            if c.name == cwtn.name:
                c.type = cwtn.type
        out.add_column(cwtn.clone_empty())
        return out

    # ── 变换 ────────────────────────────────────────────────────────────

    def filter(self, mask: Sequence[bool]) -> "Block":
        """就地过滤所有列。``mask`` 长度必须等于 ``rows()``。"""
        if len(mask) != self.rows():
            raise_error(
                ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH,
                f"filter mask size {len(mask)} != block rows {self.rows()}",
            )
        for c in self.data:
            c.column.filter(mask)
        return self

    def permute(self, indexes: Sequence[int]) -> "Block":
        """**就地**重排所有行。

        .. warning::

           **这是就地操作,而且它是个陷阱。** 如果要在循环里对**同一个**
           Block 反复取不同子集(例如"按分区键把一块数据切成多个 part"),
           第一次 permute 会把 Block 本身缩小,后面几次的索引就全部越界了
           —— 报出来的是 ``IndexError: index 10351 is out of bounds for
           axis 0 with size 10343``,完全看不出根因在哪。

           要在循环里按索引取子集,请用 :meth:`take`(返回**新** Block)。
           (这个坑在实现 MergeTree 的按分区切分时踩到过。)
        """
        for c in self.data:
            c.column = c.column.permute(indexes)
        return self

    def take(self, indexes: Sequence[int]) -> "Block":
        """按索引取一个**新** Block —— 不改自己,可以反复调用。

        与 :meth:`IColumn.get_many` 的关系:``take`` 就是"对每一列调
        ``get_many``",所以语义与列的取子集完全一致。
        """
        out = Block(info=self.info.clone())
        for c in self.data:
            out.add_column(ColumnWithTypeAndName(
                c.name, c.type, c.column.get_many(indexes)
            ))
        return out

    def slice(self, offset: int, length: int) -> "Block":
        """取一个**新** Block(不共享列数据)。

        .. note::

           真实 ClickHouse 的 ``Block::slice`` 同样是拷贝语义,
           但在 pipeline 里更常用的是 ``Chunk`` 级的
           ``Chunk::cloneEmpty`` + 列切片(列本身是不可变共享的)。
           我们这里直接复制,简单且不会出现"两个处理器改同一列"的 bug。
        """
        if offset < 0 or length < 0 or offset + length > self.rows():
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"slice({offset}, {length}) on a block of {self.rows()} rows",
            )
        out = self.clone_empty()
        for src, dst in zip(self.data, out.data):
            dst.column.insert_range_from(src.column, offset, length)
        return out

    def merge(self, other: "Block") -> None:
        """把 ``other`` 的行**追加**到自己末尾(列必须同名同类型)。

        这是 ``MergingSortedTransform`` 的工作方式之外最常用的一种合并:
        把同一个表的一百个 part 的 Block 拼成一个大 Block。
        """
        if self.is_empty():
            for c in other.data:
                self.add_column(ColumnWithTypeAndName(c.name, c.type, c.column.clone()))
            return
        if self.columns() != other.columns():
            raise_error(
                ErrorCode.INCOMPATIBLE_COLUMNS,
                f"Cannot merge blocks with different column counts: "
                f"{self.columns()} vs {other.columns()}",
            )
        for dst, src in zip(self.data, other.data):
            if dst.name != src.name or not dst.type.equals(src.type):
                raise_error(
                    ErrorCode.INCOMPATIBLE_COLUMNS,
                    f"Cannot merge columns '{dst.name} {dst.type}' and "
                    f"'{src.name} {src.type}'",
                )
            dst.column.insert_range_from(src.column, 0, src.column.size())

    # ── 元数据 ──────────────────────────────────────────────────────────

    def get_names_and_types(self) -> List[Tuple[str, DataType]]:
        return [(c.name, c.type) for c in self.data]

    def get_names(self) -> List[str]:
        return [c.name for c in self.data]

    def dump_structure(self) -> str:  # pragma: no cover
        lines = [f"Block: {self.rows()} rows, {self.columns()} columns"]
        for c in self.data:
            lines.append(f"  {c.name} {c.type}  →  {c.column.dump_structure(2)}")
        return "\n".join(lines)

    def __len__(self) -> int:
        return self.rows()

    def __repr__(self) -> str:  # pragma: no cover
        names = ", ".join(f"{c.name}:{c.type}" for c in self.data[:6])
        more = ", …" if self.columns() > 6 else ""
        return f"Block({self.rows()} rows, {{{names}{more}}})"


class Chunk:
    """**真正流动在流水线上的东西** —— header + 一批列。

    三个不变量(构造时校验,违反就抛 ``LOGICAL_ERROR`` 家族的错误):

    1. ``len(columns) == header.columns()``;
    2. 每列的行数都等于 ``num_rows``;
    3. ``header`` 里的类型与列的**实际类型**一致。

    .. warning::

       第 3 条在 mini-clickhouse 里只校验"行数"与"列数",不校验
       "列的 Python 类型是否和声明一致" —— 因为 ``ColumnVector``
       本身没有携带"我是 UInt8 还是 UInt16"的信息(那在 ``DataType`` 里)。
       真实 ClickHouse 也不做这个校验(靠类型系统在编译期保证),
       但**它会在序列化时暴露**:把 UInt16 列当成 UInt8 写出去,
       文件就坏了。第 17 章会讲怎么用 format 的往返测试来防这一类错。
    """

    __slots__ = ("header", "columns", "num_rows")

    def __init__(
        self,
        header: Block,
        columns: Optional[List[IColumn]] = None,
        num_rows: Optional[int] = None,
    ) -> None:
        self.header = header
        self.columns: List[IColumn] = columns if columns is not None else []
        if self.columns:
            actual = self.columns[0].size()
            self.num_rows = actual if num_rows is None else num_rows
            if self.num_rows != actual:
                raise_error(
                    ErrorCode.LOGICAL_ERROR,
                    f"Chunk num_rows={self.num_rows} does not match the first column "
                    f"size {actual}",
                )
            for i, c in enumerate(self.columns):
                if c.size() != self.num_rows:
                    raise_error(
                        ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH,
                        f"Chunk column {i} has {c.size()} rows, expected {self.num_rows}",
                    )
        else:
            self.num_rows = 0 if num_rows is None else num_rows
        # 列数与表头必须一致 —— **但有一个例外**:``clone_empty()`` 产出的
        # "空壳 chunk"(有表头、零列)是合法的。真实 ClickHouse 的
        # ``Chunk::cloneEmpty()`` 正是这么定义的:它是"下一个 chunk 该长什么样"
        # 的模板,用来在流水线里传递结构信息,里面没有数据。
        if self.columns and self.header.columns() and len(self.columns) != self.header.columns():
            raise_error(
                ErrorCode.LOGICAL_ERROR,
                f"Chunk has {len(self.columns)} columns but the header declares "
                f"{self.header.columns()}",
            )

    # ── 访问 ────────────────────────────────────────────────────────────

    def get_header(self) -> Block:
        return self.header

    def get_columns(self) -> List[IColumn]:
        return self.columns

    def get_column_by_position(self, i: int) -> IColumn:
        return self.columns[i]

    def get_column_by_name(self, name: str) -> IColumn:
        pos = self.header.get_position_by_name(name)
        if pos < 0:
            raise_error(
                ErrorCode.NOT_FOUND_COLUMN_IN_BLOCK,
                f"There is no column '{name}' in chunk",
            )
        return self.columns[pos]

    def get_columns_with_type(self) -> List[ColumnWithTypeAndName]:
        """把 chunk 还原成一个"带数据的 Block"(格式输出、测试断言常用)。"""
        return [
            ColumnWithTypeAndName(c.name, c.type, self.columns[i])
            for i, c in enumerate(self.header.data)
        ]

    def get_block_with_data(self) -> Block:
        return Block(self.get_columns_with_type(), self.header.info.clone())

    def add_column(self, name: str, col: IColumn) -> None:
        self.columns.append(col)
        self.header.add_column(ColumnWithTypeAndName(name, _unknown_type(), col.clone_empty()))
        if col.size() != self.num_rows:
            raise_error(
                ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH,
                f"Added column '{name}' has {col.size()} rows, expected {self.num_rows}",
            )

    # ── 复制与变换 ──────────────────────────────────────────────────────

    def clone_empty(self) -> "Chunk":
        return Chunk(self.header, [], 0)

    def clone(self) -> "Chunk":
        return Chunk(self.header, [c.clone() for c in self.columns], self.num_rows)

    def filter(self, mask: Sequence[bool]) -> "Chunk":
        if len(mask) != self.num_rows:
            raise_error(
                ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH,
                f"filter mask size {len(mask)} != chunk rows {self.num_rows}",
            )
        cols = []
        for c in self.columns:
            c.filter(mask)
            cols.append(c)
        return Chunk(self.header, cols, sum(1 for m in mask if m))

    def permute(self, indexes: Sequence[int]) -> "Chunk":
        return Chunk(
            self.header,
            [c.permute(indexes) for c in self.columns],
            len(indexes),
        )

    def slice(self, offset: int, length: int) -> "Chunk":
        cols = []
        for c in self.columns:
            out = c.clone_empty()
            out.insert_range_from(c, offset, length)
            cols.append(out)
        return Chunk(self.header, cols, length)

    def __len__(self) -> int:
        return self.num_rows

    def __repr__(self) -> str:  # pragma: no cover
        return f"Chunk({self.num_rows} rows, {len(self.columns)} columns)"


def _unknown_type() -> DataType:
    """给 ``Chunk.add_column`` 用的占位类型(真实 CH 里没有这个口子)。"""
    from .types import get_data_type

    return get_data_type("String")


def chunk_from_block(block: Block) -> Chunk:
    """把一个"带数据的 Block"转成 Chunk(header 就是它的空壳)。"""
    return Chunk(block.clone_empty(), [c.column for c in block.data], block.rows())


def block_from_chunks(chunks: Iterable[Chunk]) -> Block:
    """把一批 chunk 拼成一个 Block(测试与格式输出常用)。"""
    out: Optional[Block] = None
    for ch in chunks:
        b = ch.get_block_with_data()
        if out is None:
            out = b
        else:
            out.merge(b)
    if out is None:
        return Block()
    return out

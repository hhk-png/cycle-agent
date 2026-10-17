"""RecordBatch —— "一批行"的列式表示。

LanceDB 内部所有的数据流动都是 ``RecordBatch``：

* 用户 `add()` 的一批数据先变成一个 batch；
* 读一个数据文件先读出一个 batch（可能再按 row range 切成多个）；
* 过滤器作用在 batch 上产生 ``mask``；
* ANN 索引返回行号，`take` 出结果 batch。

之所以不是逐行处理：列存下"同一列的所有值"在内存里是连续的，
过滤器可以整列向量化求值（numpy 直接算），投影可以完全不碰其它列。
本文件还顺带实现了 **批量大小 / 行组** 的概念 ——
Lance 写文件时会按 ``max_rows_per_file`` 把数据切成多个片段（fragment），
每个片段内部再按 ``max_rows_per_group`` 切成行组，这两级切分决定了
"扫描时能跳过多少数据"。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np

from .arrays import (
    Array,
    FixedSizeListArray,
    StructArray,
    array_from_pylist,
    concat_arrays,
    empty_array,
    indices_to_mask,
    mask_to_indices,
)
from .errors import SchemaError, TypeMismatchError
from .schema import Schema
from .types import (
    BOOL,
    DataType,
    INT64,
    List as ListType,
    Struct as StructType,
    UINT64,
    infer_schema,
)

__all__ = [
    "RecordBatch",
    "DEFAULT_MAX_ROWS_PER_FILE",
    "DEFAULT_MAX_ROWS_PER_GROUP",
    "DEFAULT_MAX_BYTES_PER_FILE",
    "DEFAULT_WRITE_BATCH_SIZE",
]

#: 一个数据文件（fragment）最多放多少行。
#: 真实 Lance 的默认值是 ``1024 * 1024``（约 100 万行）。
DEFAULT_MAX_ROWS_PER_FILE = 1024 * 1024
#: 一个行组（row group / page 组）最多多少行。
#: 真实 Lance 用 ``max_rows_per_group``，默认也是 100 万，多数场景会调小。
DEFAULT_MAX_ROWS_PER_GROUP = 1024 * 1024
#: 单个文件的大小上限（字节），真实 Lance 默认 90 GB，实际会同时受行数约束。
DEFAULT_MAX_BYTES_PER_FILE = 90 * 1024**3
#: 写入时攒多大一批再落盘。
DEFAULT_WRITE_BATCH_SIZE = 1024


class RecordBatch:
    """不可变的一批列式数据。"""

    __slots__ = ("schema", "_columns")

    def __init__(self, schema: Schema, columns: Mapping[str, Array]) -> None:
        self.schema = schema
        missing = [f.name for f in schema.fields if f.name not in columns]
        if missing:
            raise SchemaError("RecordBatch 缺少列", missing=missing)
        n = len(next(iter(columns.values()))) if columns else 0
        for name, arr in columns.items():
            if len(arr) != n:
                raise SchemaError(
                    "列长度不一致", column=name, length=len(arr), expected=n
                )
        self._columns: Dict[str, Array] = dict(columns)

    # -- 构造 -------------------------------------------------------------
    @staticmethod
    def from_pylist(
        rows: Sequence[Mapping[str, Any]],
        schema: Optional[Schema] = None,
        *,
        strict: bool = True,
    ) -> "RecordBatch":
        """从 ``[{"a": 1, "vector": [...]}]`` 构造。"""

        rows = list(rows)
        if schema is None:
            schema = infer_schema(rows) if rows else Schema([])
        cols: Dict[str, Array] = {}
        for f in schema.fields:
            values = []
            for i, r in enumerate(rows):
                if f.name in r:
                    values.append(r[f.name])
                elif strict:
                    raise SchemaError(
                        f"第 {i} 行缺少列 {f.name!r}",
                        column=f.name,
                        row=i,
                    )
                else:
                    values.append(None)
            cols[f.name] = array_from_pylist(f.type, values)
        return RecordBatch(schema, cols)

    @staticmethod
    def from_pydict(
        data: Mapping[str, Sequence[Any]], schema: Optional[Schema] = None
    ) -> "RecordBatch":
        """从 ``{"a": [1, 2], "b": ["x", "y"]}`` 构造。"""

        if schema is None:
            keys = list(data.keys())
            lengths = {len(v) for v in data.values()}
            if len(lengths) > 1:
                raise SchemaError("各列长度不一致", lengths={k: len(v) for k, v in data.items()})
            n = lengths.pop() if lengths else 0
            rows = [{k: data[k][i] for k in keys} for i in range(n)]
            schema = infer_schema(rows) if rows else Schema([])
        cols = {
            f.name: array_from_pylist(f.type, list(data.get(f.name, [])))
            for f in schema.fields
        }
        return RecordBatch(schema, cols)

    @staticmethod
    def from_arrow(arrow_table: Any, *, schema: Optional[Schema] = None) -> "RecordBatch":
        """从 ``pyarrow.Table`` 构造（走 ``.to_pylist()``，慢但正确）。"""

        sch = schema or Schema.from_arrow(arrow_table.schema)
        if arrow_table.num_rows == 0:
            return RecordBatch(sch, {f.name: empty_array(f.type) for f in sch.fields})
        return RecordBatch.from_pylist(arrow_table.to_pylist(), sch)

    @staticmethod
    def empty(schema: Schema) -> "RecordBatch":
        return RecordBatch(schema, {f.name: empty_array(f.type) for f in schema.fields})

    # -- 元信息 -----------------------------------------------------------
    def __len__(self) -> int:
        return self.num_rows

    @property
    def num_rows(self) -> int:
        if not self._columns:
            return 0
        return len(next(iter(self._columns.values())))

    @property
    def num_columns(self) -> int:
        return len(self._columns)

    @property
    def column_names(self) -> List[str]:
        return list(self._columns)

    @property
    def nbytes(self) -> int:
        return sum(a.nbytes for a in self._columns.values())

    def __repr__(self) -> str:
        cols = ", ".join(f"{n}: {a.type.name}" for n, a in self._columns.items())
        return f"RecordBatch(rows={self.num_rows}, columns=[{cols}])"

    # -- 访问 -------------------------------------------------------------
    def column(self, name: str) -> Array:
        try:
            return self._columns[name]
        except KeyError:
            raise SchemaError(
                f"批次里没有列 {name!r}", available=list(self._columns)
            ) from None

    def __getitem__(self, name: str) -> Array:
        return self.column(name)

    def __contains__(self, name: object) -> bool:
        return name in self._columns

    def to_pydict(self) -> Dict[str, List[Any]]:
        return {name: arr.to_pylist() for name, arr in self._columns.items()}

    def column_pylist(self, name: str) -> List[Any]:
        return self.column(name).to_pylist()

    def to_pylist(self) -> List[Dict[str, Any]]:
        """行式展开 —— 只应该在"交给用户看"的边界上调用。"""

        cols = self.to_pydict()
        names = list(cols)
        return [{n: cols[n][i] for n in names} for i in range(self.num_rows)]

    def row(self, i: int) -> Dict[str, Any]:
        return {name: arr.value(i) for name, arr in self._columns.items()}

    def to_arrow(self) -> Any:
        import pyarrow as pa

        arrays = []
        for f in self.schema.fields:
            arrays.append(_array_to_arrow(self._columns[f.name]))
        return pa.Table.from_arrays(arrays, schema=self.schema.to_arrow())

    def to_pandas(self) -> Any:
        return self.to_arrow().to_pandas()

    def to_numpy(self, column: str) -> np.ndarray:
        arr = self.column(column)
        if hasattr(arr, "to_numpy"):
            return arr.to_numpy()  # type: ignore[attr-defined]
        if isinstance(arr, FixedSizeListArray):
            return arr.to_matrix()
        raise TypeMismatchError(f"列 {column!r} 不能转成 numpy")

    # -- 变换 -------------------------------------------------------------
    def select(self, names: Sequence[str]) -> "RecordBatch":
        for n in names:
            if n not in self._columns:
                raise SchemaError(
                    f"select 引用了不存在的列 {n!r}", available=list(self._columns)
                )
        return RecordBatch(
            self.schema.select(names), {n: self._columns[n] for n in names}
        )

    def drop(self, names: Sequence[str]) -> "RecordBatch":
        keep = [n for n in self._columns if n not in set(names)]
        return RecordBatch(
            Schema([self.schema[n] for n in keep], dict(self.schema.metadata)),
            {n: self._columns[n] for n in keep},
        )

    def with_column(self, name: str, array: Array) -> "RecordBatch":
        cols = dict(self._columns)
        if name in cols:
            schema = self.schema.set_column(name, self.schema[name].with_type(array.type))
        else:
            schema = self.schema.append_column(_field_like(name, array.type))
        cols[name] = array
        return RecordBatch(schema, cols)

    def rename_column(self, old: str, new: str) -> "RecordBatch":
        cols = {(new if k == old else k): v for k, v in self._columns.items()}
        return RecordBatch(self.schema.rename_column(old, new), cols)

    def take(self, indices: Sequence[int]) -> "RecordBatch":
        idx = np.asarray(indices, dtype=np.int64)
        if idx.size and (idx.max() >= self.num_rows or idx.min() < -self.num_rows):
            raise SchemaError(
                "take 的行号越界", max_index=int(idx.max()), rows=self.num_rows
            )
        return RecordBatch(self.schema, {n: a.take(idx) for n, a in self._columns.items()})

    def filter(self, mask: Sequence[bool] | np.ndarray) -> "RecordBatch":
        m = np.asarray(mask, dtype=bool)
        if m.ndim != 1 or len(m) != self.num_rows:
            raise SchemaError("过滤掩码长度错误", mask_len=len(m), rows=self.num_rows)
        return RecordBatch(self.schema, {n: a.filter(m) for n, a in self._columns.items()})

    def slice(self, offset: int, length: Optional[int] = None) -> "RecordBatch":
        return RecordBatch(
            self.schema,
            {n: a.slice(offset, length) for n, a in self._columns.items()},
        )

    def head(self, n: int = 5) -> "RecordBatch":
        return self.slice(0, min(n, self.num_rows))

    def limit(self, n: int) -> "RecordBatch":
        return self.slice(0, min(n, self.num_rows))

    def add_rowid(self, start: int, *, name: str = "_rowid") -> "RecordBatch":
        """附加一列单调递增的行号，用来模拟 Lance 的 ``_rowid``。"""

        from .arrays import PrimitiveArray

        ids = np.arange(start, start + self.num_rows, dtype=np.uint64)
        arr = PrimitiveArray(UINT64, ids, None)
        cols = dict(self._columns)
        cols[name] = arr
        if name in self.schema:
            schema = self.schema
        else:
            schema = self.schema.append_column(_field_like(name, UINT64))
        return RecordBatch(schema, cols)

    def with_nulls_for(self, column: str, nulls: np.ndarray) -> "RecordBatch":
        """把已知的删除行标成 null（软删除的可视化形式）。"""

        arr = self._columns[column]
        new = _set_nulls(arr, nulls)
        cols = dict(self._columns)
        cols[column] = new
        return RecordBatch(self.schema, cols)

    # -- 拼接 -------------------------------------------------------------
    @staticmethod
    def concat(batches: Sequence["RecordBatch"]) -> "RecordBatch":
        batches = [b for b in batches if b is not None]
        non_empty = [b for b in batches if b.num_rows > 0]
        if not batches:
            raise SchemaError("concat 需要至少一个批次")
        schema = batches[0].schema
        for b in batches[1:]:
            if b.schema != schema:
                raise SchemaError("concat 的批次 schema 不一致", left=str(schema), right=str(b.schema))
        if not non_empty:
            return RecordBatch.empty(schema)
        cols = {
            f.name: concat_arrays([b.column(f.name) for b in non_empty], f.type)
            for f in schema.fields
        }
        return RecordBatch(schema, cols)

    # -- 切分 -------------------------------------------------------------
    def split_by_rows(self, max_rows: int) -> List["RecordBatch"]:
        """按行数切成多个批次（写文件时的 ``max_rows_per_file``）。"""

        if max_rows <= 0:
            raise SchemaError("max_rows 必须为正", max_rows=max_rows)
        out: List[RecordBatch] = []
        for start in range(0, max(1, self.num_rows), max_rows):
            piece = self.slice(start, min(max_rows, self.num_rows - start))
            if piece.num_rows:
                out.append(piece)
        return out or [self.slice(0, 0)]

    def split_by_size(self, max_bytes: int) -> List["RecordBatch"]:
        """按估算字节数切分（写文件时的 ``max_bytes_per_file``）。

        这里用"均摊行宽 × 行数"做估算。真实 Lance 是边写边累计实际字节数，
        更精确；估算版的好处是不会为了决策而多遍历一次数据。
        """

        if self.num_rows == 0:
            return [self.slice(0, 0)]
        per_row = max(1, self.nbytes // self.num_rows)
        rows_per = max(1, max_bytes // per_row)
        return self.split_by_rows(rows_per)

    def row_group_ranges(self, max_rows_per_group: int) -> List[Tuple[int, int]]:
        """行组边界 ``[(start, stop), ...]`` —— 扫描器用它决定"要不要读这一组"。"""

        if max_rows_per_group <= 0:
            return [(0, self.num_rows)]
        return [
            (s, min(s + max_rows_per_group, self.num_rows))
            for s in range(0, self.num_rows, max_rows_per_group)
        ]

    # -- 校验 -------------------------------------------------------------
    def validate(self) -> None:
        for f in self.schema.fields:
            arr = self.column(f.name)
            if arr.type != f.type:
                raise TypeMismatchError(
                    f"列 {f.name} 的数组类型 {arr.type.name} 与 schema 的 {f.type.name} 不符"
                )
            arr.validate()
            if not f.nullable and arr.nulls is not None:
                bad = np.flatnonzero(arr.nulls)
                raise SchemaError(
                    f"非空列 {f.name} 出现了 {len(bad)} 个 null",
                    column=f.name,
                    first_bad_row=int(bad[0]),
                )


def _field_like(name: str, type: DataType) -> Any:
    from .schema import Field

    return Field(name, type, nullable=True)


def _set_nulls(arr: Array, nulls: np.ndarray) -> Array:
    """给已有数组叠加一层新的 null 掩码。"""

    extra = np.asarray(nulls, dtype=bool)
    base = arr.null_mask()
    merged = base | extra
    if isinstance(arr, FixedSizeListArray):
        return FixedSizeListArray(arr.type, arr.values, merged)
    try:
        return type(arr)(arr.type, arr.data, merged)  # type: ignore[call-arg]
    except TypeError:
        # 变长/嵌套类型：退化成"重建成普通列"
        vals = arr.to_pylist()
        for i in np.flatnonzero(extra):
            vals[int(i)] = None
        return array_from_pylist(arr.type, vals)


def _array_to_arrow(arr: Array) -> Any:
    import pyarrow as pa

    from .arrays import (
        BinaryArray,
        DictionaryArray,
        FixedSizeListArray as FSL,
        ListArray,
        MapArray,
        NullArray,
        PrimitiveArray,
        StringArray,
        StructArray,
    )

    if isinstance(arr, PrimitiveArray):
        return pa.array(arr.to_pylist(), type=arr.type._to_arrow(pa))
    if isinstance(arr, NullArray):
        return pa.nulls(len(arr))
    if isinstance(arr, (StringArray, BinaryArray)):
        return pa.array(arr.to_pylist(), type=arr.type._to_arrow(pa))
    if isinstance(arr, FSL):
        return pa.array(arr.to_pylist(), type=arr.type._to_arrow(pa))
    if isinstance(arr, ListArray):
        return pa.array(arr.to_pylist(), type=arr.type._to_arrow(pa))
    if isinstance(arr, StructArray):
        return pa.array(arr.to_pylist(), type=arr.type._to_arrow(pa))
    if isinstance(arr, MapArray):
        return pa.array(arr.to_pylist(), type=arr.type._to_arrow(pa))
    if isinstance(arr, DictionaryArray):
        return pa.array(arr.to_pylist(), type=arr.type._to_arrow(pa))
    raise TypeMismatchError(f"无法转成 Arrow: {type(arr).__name__}")


class BatchReader:
    """把 ``RecordBatch`` 列表包成迭代器，并支持"按行数流式切分"。

    查询引擎的输入是"一批一批的"，不是一个大结果集 ——
    这样才能做流式聚合和背压。
    """

    __slots__ = ("_batches", "_pos", "_current")

    def __init__(self, batches: Sequence[RecordBatch]) -> None:
        self._batches = list(batches)
        self._pos = 0
        self._current: Optional[RecordBatch] = None

    def __iter__(self) -> Iterator[RecordBatch]:
        return iter(self._batches)

    def read_next(self) -> Optional[RecordBatch]:
        if self._pos >= len(self._batches):
            return None
        b = self._batches[self._pos]
        self._pos += 1
        return b

    def read_all(self) -> RecordBatch:
        return RecordBatch.concat(self._batches) if self._batches else RecordBatch.empty(Schema([]))

    @property
    def num_batches(self) -> int:
        return len(self._batches)

    @property
    def total_rows(self) -> int:
        return sum(b.num_rows for b in self._batches)

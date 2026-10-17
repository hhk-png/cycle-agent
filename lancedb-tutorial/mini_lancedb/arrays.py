"""列式内存表示：Arrow 风格的一维数组。

LanceDB 的查询引擎在内存里搬运的是 Arrow 列，不是 Python 对象。
本模块用纯 Python + numpy 复刻 Arrow 的几种数组布局：

* :class:`PrimitiveArray` —— 定长标量，"值缓冲区 + 有效性位图"两件套；
* :class:`StringArray` / :class:`BinaryArray` —— "offsets + 值缓冲区"；
* :class:`FixedSizeListArray` —— **向量列的物理形态**，子列长度 = 行数 × 维度；
* :class:`ListArray` / :class:`StructArray` / :class:`MapArray`；
* :class:`DictionaryArray` —— 字典编码，值和下标分离。

所有数组都支持三个核心操作，它们刚好对应 LanceDB 数据流的三个动作：

* ``take(indices)`` —— 按行号取子集。**ANN 检索的结果就是一堆行号**，
  整个"召回 → 回表"的过程就是 ``take``；
* ``filter(mask)`` —— 布尔掩码筛行，谓词下推的最终形态；
* ``slice(offset, length)`` —— 零拷贝切片（numpy 侧确实是 view），
  对应 Lance 文件里的"只读第 7~15 页"。

有效性位图用 ``np.bool_`` 数组表示，``True`` 表示"这一行是 null"。
没有 null 时 ``nulls is None`` —— 这是 Arrow 里很常见的空间优化，
也让"绝大多数列都没有 null"这个事实在代码里显式化。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np

from .errors import CorruptedFileError, SchemaError, TypeMismatchError
from .schema import Field
from .types import (
    BINARY,
    BOOL,
    DataType,
    FixedSizeList,
    List as ListType,
    Map as MapType,
    Dictionary as DictionaryType,
    Null as NullType,
    Struct as StructType,
    UTF8,
    is_vector_type,
)

__all__ = [
    "Array",
    "PrimitiveArray",
    "StringArray",
    "BinaryArray",
    "FixedSizeListArray",
    "ListArray",
    "StructArray",
    "MapArray",
    "DictionaryArray",
    "NullArray",
    "array_from_pylist",
    "concat_arrays",
    "empty_array",
    "vector_matrix",
    "mask_to_indices",
    "indices_to_mask",
    "union_mask",
]

NullBitmap = Optional[np.ndarray]


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _as_null_bitmap(nulls: Any, n: int) -> NullBitmap:
    """把各种"谁为空"的输入规整成 ``np.bool_`` 数组或 ``None``。

    空数组（``n == 0``）一律归一成 ``None``，避免出现"长度 0 的零向量位图"
    和 ``None`` 两种表示同一件事的情况。
    """

    if n == 0:
        return None
    if nulls is None:
        return None
    arr = np.asarray(nulls, dtype=bool)
    if arr.ndim != 1 or arr.shape[0] != n:
        raise SchemaError("有效性位图长度与列长不符", nulls_len=arr.shape[0], rows=n)
    if not arr.any():
        return None
    return arr


def mask_to_indices(mask: np.ndarray) -> np.ndarray:
    """布尔掩码 → 行号数组。``np.flatnonzero`` 的一层语义包装。"""

    return np.flatnonzero(np.asarray(mask, dtype=bool))


def indices_to_mask(indices: Sequence[int], n: int) -> np.ndarray:
    """行号数组 → 布尔掩码。用于把索引返回的行号变成过滤条件。"""

    mask = np.zeros(n, dtype=bool)
    if len(indices):
        idx = np.asarray(indices, dtype=np.int64)
        idx = idx[(idx >= 0) & (idx < n)]
        mask[idx] = True
    return mask


def union_mask(bitmaps: Iterable[Optional[np.ndarray]], n: int) -> NullBitmap:
    """把若干有效性位图求并（多列拼出来的行只要有一列为空就算空）。"""

    out = np.zeros(n, dtype=bool)
    any_ = False
    for b in bitmaps:
        if b is None:
            continue
        out |= b
        any_ = True
    return out if any_ else None


# ---------------------------------------------------------------------------
# 基类
# ---------------------------------------------------------------------------


class Array:
    """所有一维列式数组的基类。"""

    __slots__ = ("type", "nulls")

    #: 逻辑类型
    type: DataType

    def __init__(self, type: DataType, nulls: NullBitmap = None) -> None:
        self.type = type
        self.nulls = nulls

    # -- 基本属性 ---------------------------------------------------------
    def __len__(self) -> int:
        raise NotImplementedError

    def __repr__(self) -> str:
        head = self.to_pylist()[:4]
        tail = "" if len(self) <= 4 else ", ..."
        return f"{type(self).__name__}({self.type.name}, {head}{tail}, n={len(self)})"

    @property
    def num_nulls(self) -> int:
        return 0 if self.nulls is None else int(self.nulls.sum())

    @property
    def num_rows(self) -> int:
        return len(self)

    @property
    def nbytes(self) -> int:
        """近似内存占用，用来演示"列存为什么省内存"。"""

        return 0 if self.nulls is None else self.nulls.nbytes

    def is_null(self, i: int) -> bool:
        return self.nulls is not None and bool(self.nulls[i])

    def null_mask(self) -> np.ndarray:
        n = len(self)
        return np.zeros(n, dtype=bool) if self.nulls is None else self.nulls

    # -- 抽象接口 ---------------------------------------------------------
    def value(self, i: int) -> Any:
        raise NotImplementedError

    def to_pylist(self, *, null_as_none: bool = True) -> List[Any]:
        raise NotImplementedError

    def take(self, indices: np.ndarray) -> "Array":
        raise NotImplementedError

    def filter(self, mask: np.ndarray) -> "Array":
        return self.take(mask_to_indices(mask))

    def slice(self, offset: int, length: Optional[int] = None) -> "Array":
        raise NotImplementedError

    def _child_arrays(self) -> Dict[str, "Array"]:
        return {}

    # -- 派生 -------------------------------------------------------------
    def concat(self, others: Sequence["Array"]) -> "Array":
        return concat_arrays([self, *others], self.type)  # type: ignore[arg-type]

    def cast(self, target: DataType) -> "Array":
        """类型转换。只支持"安全"的转换：数值之间、定长/变长列表之间。"""

        if target == self.type:
            return self
        from .types import FixedSizeList as FSL, List as LT, promote_types

        if isinstance(self, PrimitiveArray) and target.numpy_dtype_or_none is not None:
            p = promote_types(self.type, target)
            if p is None:
                raise TypeMismatchError(
                    f"不能把 {self.type.name} 转成 {target.name}"
                )
            data = self.data.astype(target.numpy_dtype, copy=False)
            return PrimitiveArray(target, data, self.nulls)
        if isinstance(self, (StringArray, BinaryArray)) and isinstance(
            target, (type(UTF8), type(BINARY))
        ):
            cls = StringArray if isinstance(target, type(UTF8)) else BinaryArray
            return cls(target, self.offsets.copy(), self.buffer, self.nulls)
        if isinstance(self, FixedSizeListArray) and isinstance(target, FSL):
            # **改列表长度不是类型转换，是数据变换。**
            # 早期版本这里直接换了子类型、保留了子列长度，于是
            # 500 行 × 8 维被当成 1000 行 × 4 维——行数静默翻倍，
            # 而且要到很久以后才因为别的原因炸掉。
            # 正确做法是明确拒绝：想让向量维度变，必须显式重建数据。
            if target.list_size != self.type.list_size:
                raise TypeMismatchError(
                    f"不能把 {self.type.name} 直接转成 {target.name}："
                    f"列表长度从 {self.type.list_size} 变成 {target.list_size} "
                    f"是数据变换而不是类型转换",
                    hint="先取出数据、按新维度重建列，再写回",
                )
            child = self.values.cast(target.child)
            return FixedSizeListArray(target, child, self.nulls)
        if isinstance(self, ListArray) and isinstance(target, LT):
            child = self.values.cast(target.child)
            return ListArray(target, self.offsets.copy(), child, self.nulls)
        if isinstance(self, PrimitiveArray) and isinstance(target, (StringArray, BinaryArray)):
            cls = StringArray if isinstance(target, StringArray) else BinaryArray
            return cls.from_pylist(target, [str(v) for v in self.to_pylist()])
        raise TypeMismatchError(f"暂不支持 {self.type.name} → {target.name} 的转换")

    def validate(self) -> None:
        """自检：长度、offsets 单调性、子列长度。写盘前和测试里都调用。"""

        n = len(self)
        if self.nulls is not None and (self.nulls.ndim != 1 or len(self.nulls) != n):
            raise CorruptedFileError("有效性位图长度错误", expected=n)
        if isinstance(self, NullArray) and self.type != NullType():
            raise CorruptedFileError("NullArray 的类型必须是 null")


# ---------------------------------------------------------------------------
# 定长标量
# ---------------------------------------------------------------------------


class PrimitiveArray(Array):
    """定长标量数组：bool / 各种 int / 各种 float / 时间戳（底层是整数）。"""

    __slots__ = ("data",)

    def __init__(self, type: DataType, data: np.ndarray, nulls: NullBitmap = None) -> None:
        if not isinstance(data, np.ndarray):
            data = np.asarray(data)
        expected = type.numpy_dtype_or_none
        if expected is not None and data.dtype != expected:
            try:
                data = data.astype(expected)
            except (TypeError, ValueError) as exc:
                raise TypeMismatchError(
                    f"无法把 {data.dtype} 数组转成 {type.name}", error=str(exc)
                ) from None
        super().__init__(type, _as_null_bitmap(nulls, len(data)))
        self.data = data

    def __len__(self) -> int:
        return len(self.data)

    @property
    def nbytes(self) -> int:
        return self.data.nbytes + super().nbytes

    def value(self, i: int) -> Any:
        if self.is_null(i):
            return None
        v = self.data[i]
        return v.item() if isinstance(v, np.generic) else v

    def to_pylist(self, *, null_as_none: bool = True) -> List[Any]:
        out = self.data.tolist()
        if self.nulls is not None:
            for i in np.flatnonzero(self.nulls):
                out[int(i)] = None
        return out

    def to_numpy(self) -> np.ndarray:
        return self.data

    def take(self, indices: np.ndarray) -> "PrimitiveArray":
        idx = np.asarray(indices, dtype=np.int64)
        return PrimitiveArray(
            self.type,
            self.data.take(idx),
            None if self.nulls is None else self.nulls.take(idx),
        )

    def slice(self, offset: int, length: Optional[int] = None) -> "PrimitiveArray":
        stop = len(self) if length is None else offset + length
        return PrimitiveArray(
            self.type,
            self.data[offset:stop],
            None if self.nulls is None else self.nulls[offset:stop],
        )

    @classmethod
    def from_pylist(cls, type: DataType, values: Sequence[Any]) -> "PrimitiveArray":
        n = len(values)
        nulls = np.zeros(n, dtype=bool)
        dtype = type.numpy_dtype
        buf = np.zeros(n, dtype=dtype)
        for i, v in enumerate(values):
            if v is None:
                nulls[i] = True
                continue
            if isinstance(v, float) and np.isnan(v) and dtype.kind != "f":
                nulls[i] = True
                continue
            try:
                if isinstance(type, NullType):
                    nulls[i] = True
                else:
                    buf[i] = v
            except (TypeError, ValueError) as exc:
                raise TypeMismatchError(
                    f"第 {i} 行的值 {v!r} 无法放进 {type.name}: {exc}"
                ) from None
        return cls(type, buf, nulls)

    def with_nulls(self, nulls: NullBitmap) -> "PrimitiveArray":
        return PrimitiveArray(self.type, self.data, nulls)


# ---------------------------------------------------------------------------
# 字符串 / 二进制
# ---------------------------------------------------------------------------


class _BinaryBackedArray(Array):
    """offsets + 一段连续字节缓冲区 —— Arrow 的变长类型布局。"""

    __slots__ = ("offsets", "buffer", "_cache")

    #: 每行是不是"变长字节"
    _decode_error = "strict"

    def __init__(
        self,
        type: DataType,
        offsets: np.ndarray,
        buffer: bytes,
        nulls: NullBitmap = None,
    ) -> None:
        n = len(offsets) - 1
        super().__init__(type, _as_null_bitmap(nulls, n))
        self.offsets = np.asarray(offsets, dtype=np.int64)
        self.buffer = buffer if isinstance(buffer, bytes) else bytes(buffer)
        self._cache: Optional[List[Any]] = None
        self.validate()

    def validate(self) -> None:
        super().validate()
        n = len(self)
        if len(self.offsets) != n + 1:
            raise CorruptedFileError(
                "offsets 长度必须是行数 + 1", offsets=len(self.offsets), rows=n
            )
        if n and self.offsets[-1] != len(self.buffer):
            raise CorruptedFileError(
                "最后一个 offset 必须等于缓冲区长度",
                last_offset=int(self.offsets[-1]),
                buffer_len=len(self.buffer),
            )
        if n and np.any(np.diff(self.offsets) < 0):
            raise CorruptedFileError("offsets 不是单调不减的")

    def __len__(self) -> int:
        return len(self.offsets) - 1

    @property
    def nbytes(self) -> int:
        return self.offsets.nbytes + len(self.buffer) + super().nbytes

    def raw(self, i: int) -> Optional[bytes]:
        if self.is_null(i):
            return None
        return self.buffer[int(self.offsets[i]) : int(self.offsets[i + 1])]

    def value(self, i: int) -> Any:
        raw = self.raw(i)
        return None if raw is None else self._decode(raw)

    def _decode(self, raw: bytes) -> Any:
        raise NotImplementedError

    def to_pylist(self, *, null_as_none: bool = True) -> List[Any]:
        if self._cache is None:
            self._cache = [self.value(i) for i in range(len(self))]
        return list(self._cache)

    def take(self, indices: np.ndarray) -> "_BinaryBackedArray":
        idx = np.asarray(indices, dtype=np.int64)
        return _rebuild_binary_like(self, idx)

    def slice(self, offset: int, length: Optional[int] = None) -> "_BinaryBackedArray":
        stop = len(self) if length is None else offset + length
        lo = int(self.offsets[offset])
        hi = int(self.offsets[stop])
        new_offsets = self.offsets[offset : stop + 1] - lo
        return type(self)(
            self.type,
            new_offsets,
            self.buffer[lo:hi],
            None if self.nulls is None else self.nulls[offset:stop],
        )

    @classmethod
    def from_pylist(cls, type: DataType, values: Sequence[Any]) -> "_BinaryBackedArray":
        n = len(values)
        nulls = np.zeros(n, dtype=bool)
        emitter = cls._raw_of
        chunks: List[bytes] = []
        offsets = np.zeros(n + 1, dtype=np.int64)
        total = 0
        for i, v in enumerate(values):
            if v is None:
                nulls[i] = True
            else:
                b = emitter(v)
                chunks.append(b)
                total += len(b)
            offsets[i + 1] = total
        return cls(type, offsets, b"".join(chunks), nulls)

    @staticmethod
    def _raw_of(v: Any) -> bytes:
        raise NotImplementedError


class StringArray(_BinaryBackedArray):
    """UTF-8 字符串列。

    物理上是 ``offsets`` + 一段 UTF-8 字节。这样做的直接后果是：
    字符串列非常适合走字典编码和 FSST 压缩，而这两者正是 Lance
    对付"高重复度文本列"的两板斧。
    """

    __slots__ = ()

    def _decode(self, raw: bytes) -> str:
        return raw.decode("utf-8", errors="strict")

    @staticmethod
    def _raw_of(v: Any) -> bytes:
        if isinstance(v, str):
            return v.encode("utf-8")
        if isinstance(v, (bytes, bytearray, memoryview)):
            return bytes(v)
        return str(v).encode("utf-8")

    @classmethod
    def from_strings(cls, values: Sequence[Optional[str]], type: DataType = UTF8) -> "StringArray":
        return cls.from_pylist(type, values)


class BinaryArray(_BinaryBackedArray):
    __slots__ = ()

    def _decode(self, raw: bytes) -> bytes:
        return raw

    @staticmethod
    def _raw_of(v: Any) -> bytes:
        if isinstance(v, str):
            return v.encode("utf-8")
        if isinstance(v, (bytes, bytearray, memoryview)):
            return bytes(v)
        raise TypeMismatchError(f"{v!r} 不是二进制数据")


def _rebuild_binary_like(src: _BinaryBackedArray, idx: np.ndarray) -> _BinaryBackedArray:
    """按行号重排变长列：重算 offsets 并拼接缓冲区。

    这是 ANN 召回后"回表"最贵的一步 —— 因为它要拷贝字节。
    Lance 的做法是尽量只在最后一步做，且只取需要的列。
    """

    if len(idx) == 0:
        return type(src)(src.type, np.zeros(1, dtype=np.int64), b"", None)
    offsets = src.offsets
    lengths = offsets[idx + 1] - offsets[idx]
    new_offsets = np.zeros(len(idx) + 1, dtype=np.int64)
    np.cumsum(lengths, out=new_offsets[1:])
    parts: List[bytes] = []
    for i in idx:
        parts.append(src.buffer[int(offsets[i]) : int(offsets[i + 1])])
    nulls = None if src.nulls is None else src.nulls.take(idx)
    return type(src)(src.type, new_offsets, b"".join(parts), nulls)


# ---------------------------------------------------------------------------
# 向量列：FixedSizeList
# ---------------------------------------------------------------------------


class FixedSizeListArray(Array):
    """定长列表数组 —— **向量列就是它**。

    子列 :attr:`values` 的长度恒等于 ``行数 × list_size``，
    所以第 ``i`` 行的向量是 ``values[i*D : (i+1)*D]``，一个 view 就够了。
    """

    __slots__ = ("values",)

    def __init__(self, type: FixedSizeList, values: Array, nulls: NullBitmap = None) -> None:
        if not isinstance(type, FixedSizeList):
            raise SchemaError("FixedSizeListArray 的类型必须是 fixed_size_list")
        n = len(values) // type.list_size if type.list_size else 0
        if n * type.list_size != len(values):
            raise CorruptedFileError(
                "子列长度不是 list_size 的整数倍",
                list_size=type.list_size,
                child_len=len(values),
            )
        super().__init__(type, _as_null_bitmap(nulls, n))
        self.values = values

    @property
    def list_size(self) -> int:
        return self.type.list_size

    def __len__(self) -> int:
        return len(self.values) // self.list_size

    @property
    def nbytes(self) -> int:
        return self.values.nbytes + super().nbytes

    def value(self, i: int) -> Any:
        if self.is_null(i):
            return None
        d = self.list_size
        return self.values.to_pylist()[i * d : (i + 1) * d]

    def to_pylist(self, *, null_as_none: bool = True) -> List[Any]:
        d = self.list_size
        flat = self.values.to_pylist()
        out: List[Any] = [flat[i * d : (i + 1) * d] for i in range(len(self))]
        if self.nulls is not None:
            for i in np.flatnonzero(self.nulls):
                out[int(i)] = None
        return out

    def to_matrix(self) -> np.ndarray:
        """把整列摊成一个 ``(n, D)`` 的矩阵。

        所有向量距离计算的前提。真实 LanceDB 在 Rust 侧做的也是这一层
        ``reshape``，只不过它是零拷贝借用 Arrow 的 buffer。
        """

        if not isinstance(self.values, PrimitiveArray):
            raise TypeMismatchError("只有定长标量子列能摊成矩阵")
        return self.values.data.reshape(len(self), self.list_size)

    def take(self, indices: np.ndarray) -> "FixedSizeListArray":
        idx = np.asarray(indices, dtype=np.int64)
        d = self.list_size
        child_idx = (idx[:, None] * d + np.arange(d)[None, :]).reshape(-1)
        return FixedSizeListArray(
            self.type,
            self.values.take(child_idx),
            None if self.nulls is None else self.nulls.take(idx),
        )

    def slice(self, offset: int, length: Optional[int] = None) -> "FixedSizeListArray":
        stop = len(self) if length is None else offset + length
        d = self.list_size
        return FixedSizeListArray(
            self.type,
            self.values.slice(offset * d, (stop - offset) * d),
            None if self.nulls is None else self.nulls[offset:stop],
        )

    @classmethod
    def from_pylist(
        cls, type: FixedSizeList, values: Sequence[Any]
    ) -> "FixedSizeListArray":
        d = type.list_size
        flat: List[Any] = []
        nulls = np.zeros(len(values), dtype=bool)
        for i, v in enumerate(values):
            if v is None:
                nulls[i] = True
                flat.extend([0] * d)
                continue
            seq = list(v)
            if hasattr(seq, "tolist"):
                seq = seq.tolist()
            if len(seq) != d:
                raise TypeMismatchError(
                    f"第 {i} 行的向量维度是 {len(seq)}，schema 声明的是 {d}",
                    column=None,
                )
            flat.extend(seq)
        # 子类型不一定是定长标量 —— 多向量列的类型是
        # ``fixed_size_list<fixed_size_list<float16>[128]>[N]``，
        # 子列本身又是一个定长列表。所以这里必须走通用的构造器，
        # 不能假定子列是 PrimitiveArray。
        child = array_from_pylist(type.child, flat)
        return cls(type, child, nulls)

    @classmethod
    def from_matrix(cls, type: FixedSizeList, matrix: np.ndarray) -> "FixedSizeListArray":
        m = np.ascontiguousarray(matrix, dtype=type.child.numpy_dtype)
        if m.ndim != 2 or m.shape[1] != type.list_size:
            raise TypeMismatchError(
                f"矩阵形状 {m.shape} 与 {type.name} 不匹配"
            )
        return cls(type, PrimitiveArray(type.child, m.reshape(-1)), None)


# ---------------------------------------------------------------------------
# 变长列表
# ---------------------------------------------------------------------------


class ListArray(Array):
    __slots__ = ("offsets", "values")

    def __init__(
        self, type: ListType, offsets: np.ndarray, values: Array, nulls: NullBitmap = None
    ) -> None:
        n = len(offsets) - 1
        super().__init__(type, _as_null_bitmap(nulls, n))
        self.offsets = np.asarray(offsets, dtype=np.int64)
        self.values = values
        if len(self.offsets) != n + 1 or (n and self.offsets[-1] != len(values)):
            raise CorruptedFileError(
                "ListArray 的 offsets 与子列长度不匹配",
                rows=n,
                child_len=len(values),
            )

    def __len__(self) -> int:
        return len(self.offsets) - 1

    @property
    def nbytes(self) -> int:
        return self.offsets.nbytes + self.values.nbytes + super().nbytes

    def raw_slice(self, i: int) -> Optional[Array]:
        if self.is_null(i):
            return None
        return self.values.slice(int(self.offsets[i]), int(self.offsets[i + 1] - self.offsets[i]))

    def value(self, i: int) -> Any:
        sub = self.raw_slice(i)
        return None if sub is None else sub.to_pylist()

    def to_pylist(self, *, null_as_none: bool = True) -> List[Any]:
        flat = self.values.to_pylist()
        return [
            None
            if self.is_null(i)
            else flat[int(self.offsets[i]) : int(self.offsets[i + 1])]
            for i in range(len(self))
        ]

    def take(self, indices: np.ndarray) -> "ListArray":
        idx = np.asarray(indices, dtype=np.int64)
        if len(idx) == 0:
            return ListArray(self.type, np.zeros(1, dtype=np.int64), empty_array(self.values.type), None)
        counts = self.offsets[idx + 1] - self.offsets[idx]
        new_offsets = np.zeros(len(idx) + 1, dtype=np.int64)
        np.cumsum(counts, out=new_offsets[1:])
        child_idx = np.concatenate(
            [np.arange(self.offsets[i], self.offsets[i + 1]) for i in idx]
        )
        return ListArray(
            self.type,
            new_offsets,
            self.values.take(child_idx),
            None if self.nulls is None else self.nulls.take(idx),
        )

    def slice(self, offset: int, length: Optional[int] = None) -> "ListArray":
        stop = len(self) if length is None else offset + length
        lo, hi = int(self.offsets[offset]), int(self.offsets[stop])
        return ListArray(
            self.type,
            self.offsets[offset : stop + 1] - lo,
            self.values.slice(lo, hi - lo),
            None if self.nulls is None else self.nulls[offset:stop],
        )

    @classmethod
    def from_pylist(cls, type: ListType, values: Sequence[Any]) -> "ListArray":
        offsets = [0]
        flat: List[Any] = []
        nulls = np.zeros(len(values), dtype=bool)
        for i, v in enumerate(values):
            if v is None:
                nulls[i] = True
            else:
                flat.extend(list(v))
            offsets.append(len(flat))
        return cls(
            type,
            np.asarray(offsets, dtype=np.int64),
            array_from_pylist(type.child, flat),
            nulls,
        )


# ---------------------------------------------------------------------------
# 结构体
# ---------------------------------------------------------------------------


class StructArray(Array):
    __slots__ = ("fields",)

    def __init__(
        self, type: StructType, fields: Mapping[str, Array], nulls: NullBitmap = None
    ) -> None:
        names = {f.name for f in type.fields}
        missing = names - set(fields)
        if missing:
            raise SchemaError("StructArray 缺少子列", missing=sorted(missing))
        n = len(next(iter(fields.values()))) if fields else 0
        for name, arr in fields.items():
            if len(arr) != n:
                raise CorruptedFileError(
                    "StructArray 子列长度不一致", field=name, length=len(arr), rows=n
                )
        super().__init__(type, _as_null_bitmap(nulls, n))
        self.fields: Dict[str, Array] = dict(fields)

    def __len__(self) -> int:
        return len(next(iter(self.fields.values()))) if self.fields else 0

    @property
    def nbytes(self) -> int:
        return sum(a.nbytes for a in self.fields.values()) + super().nbytes

    def field(self, name: str) -> Array:
        if name not in self.fields:
            raise SchemaError(f"StructArray 里没有子列 {name!r}", available=list(self.fields))
        return self.fields[name]

    def value(self, i: int) -> Any:
        if self.is_null(i):
            return None
        return {name: arr.value(i) for name, arr in self.fields.items()}

    def to_pylist(self, *, null_as_none: bool = True) -> List[Any]:
        cols = {name: arr.to_pylist() for name, arr in self.fields.items()}
        out = [{name: cols[name][i] for name in cols} for i in range(len(self))]
        if self.nulls is not None:
            for i in np.flatnonzero(self.nulls):
                out[int(i)] = None
        return out

    def take(self, indices: np.ndarray) -> "StructArray":
        idx = np.asarray(indices, dtype=np.int64)
        return StructArray(
            self.type,
            {name: arr.take(idx) for name, arr in self.fields.items()},
            None if self.nulls is None else self.nulls.take(idx),
        )

    def slice(self, offset: int, length: Optional[int] = None) -> "StructArray":
        stop = len(self) if length is None else offset + length
        return StructArray(
            self.type,
            {name: arr.slice(offset, stop - offset) for name, arr in self.fields.items()},
            None if self.nulls is None else self.nulls[offset:stop],
        )

    @classmethod
    def from_pylist(cls, type: StructType, values: Sequence[Any]) -> "StructArray":
        rows = list(values)
        cols: Dict[str, Array] = {}
        for f in type.fields:
            col = [None if r is None else r.get(f.name) for r in rows]
            cols[f.name] = array_from_pylist(f.type, col)
        nulls = np.asarray([r is None for r in rows], dtype=bool)
        return cls(type, cols, nulls)


class MapArray(Array):
    """``map<key, value>``：物理上就是 ``List<Struct<key, value>>``。"""

    __slots__ = ("offsets", "keys", "items")

    def __init__(
        self,
        type: MapType,
        offsets: np.ndarray,
        keys: Array,
        items: Array,
        nulls: NullBitmap = None,
    ) -> None:
        n = len(offsets) - 1
        super().__init__(type, _as_null_bitmap(nulls, n))
        self.offsets = np.asarray(offsets, dtype=np.int64)
        self.keys = keys
        self.items = items

    def __len__(self) -> int:
        return len(self.offsets) - 1

    @property
    def nbytes(self) -> int:
        return self.offsets.nbytes + self.keys.nbytes + self.items.nbytes + super().nbytes

    def value(self, i: int) -> Any:
        if self.is_null(i):
            return None
        lo, hi = int(self.offsets[i]), int(self.offsets[i + 1])
        ks = self.keys.to_pylist()[lo:hi]
        vs = self.items.to_pylist()[lo:hi]
        return dict(zip(ks, vs))

    def to_pylist(self, *, null_as_none: bool = True) -> List[Any]:
        return [self.value(i) for i in range(len(self))]

    def take(self, indices: np.ndarray) -> "MapArray":
        idx = np.asarray(indices, dtype=np.int64)
        counts = self.offsets[idx + 1] - self.offsets[idx]
        new_offsets = np.zeros(len(idx) + 1, dtype=np.int64)
        np.cumsum(counts, out=new_offsets[1:])
        child_idx = np.concatenate(
            [np.arange(self.offsets[i], self.offsets[i + 1]) for i in idx]
        ) if len(idx) else np.zeros(0, dtype=np.int64)
        return MapArray(
            self.type,
            new_offsets,
            self.keys.take(child_idx),
            self.items.take(child_idx),
            None if self.nulls is None else self.nulls.take(idx),
        )

    def slice(self, offset: int, length: Optional[int] = None) -> "MapArray":
        stop = len(self) if length is None else offset + length
        lo, hi = int(self.offsets[offset]), int(self.offsets[stop])
        return MapArray(
            self.type,
            self.offsets[offset : stop + 1] - lo,
            self.keys.slice(lo, hi - lo),
            self.items.slice(lo, hi - lo),
            None if self.nulls is None else self.nulls[offset:stop],
        )

    @classmethod
    def from_pylist(cls, type: MapType, values: Sequence[Any]) -> "MapArray":
        offsets = [0]
        ks: List[Any] = []
        vs: List[Any] = []
        nulls = np.zeros(len(values), dtype=bool)
        for i, v in enumerate(values):
            if v is None:
                nulls[i] = True
            else:
                for k, val in dict(v).items():
                    ks.append(k)
                    vs.append(val)
            offsets.append(len(ks))
        return cls(
            type,
            np.asarray(offsets, dtype=np.int64),
            array_from_pylist(type.key_type, ks),
            array_from_pylist(type.item_type, vs),
            nulls,
        )


# ---------------------------------------------------------------------------
# 字典 / null
# ---------------------------------------------------------------------------


class DictionaryArray(Array):
    """字典编码列：``indices`` 指向 :attr:`dictionary`。

    Lance 在写文件时对低基数列自动做这件事（见 ``encoding/dictionary.py``）；
    这里的类是它在内存里的对应物。
    """

    __slots__ = ("indices", "dictionary")

    def __init__(
        self,
        type: DictionaryType,
        indices: np.ndarray,
        dictionary: Array,
        nulls: NullBitmap = None,
    ) -> None:
        super().__init__(type, _as_null_bitmap(nulls, len(indices)))
        self.indices = np.asarray(indices, dtype=np.int64)
        self.dictionary = dictionary

    def __len__(self) -> int:
        return len(self.indices)

    @property
    def nbytes(self) -> int:
        return self.indices.nbytes + self.dictionary.nbytes + super().nbytes

    @property
    def compression_ratio(self) -> float:
        """相对于"直接存值"的压缩比，调试时挺有用。"""

        raw = self.dictionary.nbytes
        if raw == 0:
            return 1.0
        return raw / max(1, self.nbytes)

    def value(self, i: int) -> Any:
        if self.is_null(i):
            return None
        return self.dictionary.value(int(self.indices[i]))

    def to_pylist(self, *, null_as_none: bool = True) -> List[Any]:
        return [self.value(i) for i in range(len(self))]

    def take(self, indices: np.ndarray) -> "DictionaryArray":
        idx = np.asarray(indices, dtype=np.int64)
        return DictionaryArray(
            self.type,
            self.indices.take(idx),
            self.dictionary,
            None if self.nulls is None else self.nulls.take(idx),
        )

    def slice(self, offset: int, length: Optional[int] = None) -> "DictionaryArray":
        stop = len(self) if length is None else offset + length
        return DictionaryArray(
            self.type,
            self.indices[offset:stop],
            self.dictionary,
            None if self.nulls is None else self.nulls[offset:stop],
        )

    def decode(self) -> Array:
        """展开成普通列。"""

        return self.dictionary.take(self.indices)

    @classmethod
    def encode(cls, array: Array, index_type: DataType) -> "DictionaryArray":
        """对一个已有列做字典编码（保持首次出现顺序）。"""

        values = array.to_pylist()
        uniq: List[Any] = []
        pos: Dict[Any, int] = {}
        indices = np.zeros(len(values), dtype=np.int64)
        for i, v in enumerate(values):
            if v is None:
                indices[i] = 0
                continue
            key = v if not isinstance(v, list) else tuple(v)
            if key not in pos:
                pos[key] = len(uniq)
                uniq.append(v)
            indices[i] = pos[key]
        dict_values = uniq if uniq else [None] * 0
        dictionary = array_from_pylist(array.type, dict_values) if dict_values else empty_array(array.type)
        return cls(DictionaryType(index_type, array.type), indices, dictionary, array.nulls)

    @classmethod
    def from_pylist(cls, type: DictionaryType, values: Sequence[Any]) -> "DictionaryArray":
        plain = array_from_pylist(type.value_type, values)
        return cls.encode(plain, type.index_type)


class NullArray(Array):
    __slots__ = ("_n",)

    def __init__(self, n: int) -> None:
        super().__init__(NullType(), np.ones(n, dtype=bool))
        self._n = n

    def __len__(self) -> int:
        return self._n

    @property
    def nbytes(self) -> int:
        return 0

    def value(self, i: int) -> Any:
        return None

    def to_pylist(self, *, null_as_none: bool = True) -> List[Any]:
        return [None] * self._n

    def take(self, indices: np.ndarray) -> "NullArray":
        return NullArray(len(indices))

    def slice(self, offset: int, length: Optional[int] = None) -> "NullArray":
        stop = self._n if length is None else offset + length
        return NullArray(max(0, stop - offset))


# ---------------------------------------------------------------------------
# 工厂 / 拼接
# ---------------------------------------------------------------------------


def array_from_pylist(type: DataType, values: Sequence[Any]) -> Array:
    """按声明类型构造数组 —— 写入路径的第一站。

    这里是类型检查最严的地方：维度不对、类型不对都会在**写入前**报错，
    而不是等到落盘后读出来才发现。这正是 ``create_table`` 时
    ``on_bad_vectors`` 参数要处理的场景。
    """

    if isinstance(type, NullType):
        n = len(values)
        if any(v is not None for v in values):
            raise TypeMismatchError("null 类型的列里出现了非 null 值")
        return NullArray(n)
    if isinstance(type, FixedSizeList):
        return FixedSizeListArray.from_pylist(type, values)
    if isinstance(type, ListType):
        return ListArray.from_pylist(type, values)
    if isinstance(type, StructType):
        return StructArray.from_pylist(type, values)
    if isinstance(type, MapType):
        return MapArray.from_pylist(type, values)
    if isinstance(type, DictionaryType):
        return DictionaryArray.from_pylist(type, values)
    if type.name in ("utf8", "large_utf8"):
        return StringArray.from_pylist(type, values)
    if type.name in ("binary", "large_binary"):
        return BinaryArray.from_pylist(type, values)
    if type.numpy_dtype_or_none is not None:
        return PrimitiveArray.from_pylist(type, values)
    # 兜底：时间/十进制等统一按底层整数走
    try:
        return PrimitiveArray.from_pylist(type, values)
    except TypeMismatchError:
        raise TypeMismatchError(f"无法为类型 {type.name} 构造数组") from None


def empty_array(type: DataType) -> Array:
    """长度为 0 的同类型空数组 —— 处理"过滤后啥也不剩"的场景。"""

    if isinstance(type, FixedSizeList):
        return FixedSizeListArray(type, empty_array(type.child), None)
    if isinstance(type, ListType):
        return ListArray(type, np.zeros(1, dtype=np.int64), empty_array(type.child), None)
    if isinstance(type, StructType):
        return StructArray(type, {f.name: empty_array(f.type) for f in type.fields}, None)
    if isinstance(type, NullType):
        return NullArray(0)
    return array_from_pylist(type, [])


def _is_offset_based(arr: Array) -> bool:
    return isinstance(
        arr, (StringArray, BinaryArray, ListArray, MapArray)
    )


def concat_arrays(arrays: Sequence[Array], dtype: Optional[DataType] = None) -> Array:
    """把多个同类型数组合并成一个（``append`` 和片段合并都会用到）。

    参数名刻意不叫 ``type``：那样会把内置的 ``type()`` 遮住，
    是个很容易踩的坑。
    """

    arrays = [a for a in arrays if a is not None]
    if not arrays:
        if dtype is None:
            raise SchemaError("concat_arrays 需要至少一个数组或一个类型")
        return empty_array(dtype)
    if len(arrays) == 1:
        return arrays[0]
    t = dtype or arrays[0].type
    first = arrays[0]
    nulls = [a.nulls for a in arrays]
    any_null = any(n is not None for n in nulls)
    total = sum(len(a) for a in arrays)

    if isinstance(first, PrimitiveArray):
        return PrimitiveArray(
            t,
            np.concatenate([a.data for a in arrays]),  # type: ignore[attr-defined]
            np.concatenate([a.null_mask() for a in arrays]) if any_null else None,
        )
    if isinstance(first, FixedSizeListArray):
        return FixedSizeListArray(
            t,  # type: ignore[arg-type]
            concat_arrays([a.values for a in arrays]),  # type: ignore[attr-defined]
            np.concatenate([a.null_mask() for a in arrays]) if any_null else None,
        )
    if isinstance(first, _BinaryBackedArray):
        # 这里刻意走"解码再编码"的朴素路径：真实 Lance 会直接拼 buffer，
        # 但那要求两个数组共享同一个 buffer 家族（slice 出来的才成立）。
        vals: List[Any] = []
        for a in arrays:
            vals.extend(a.to_pylist())
        return first.__class__.from_pylist(t, vals)
    if isinstance(first, ListArray):
        offsets = [0]
        child_parts: List[Array] = []
        cur = 0
        for a in arrays:
            piece = a.slice(0, len(a))
            child_parts.append(piece.values)
            cur += len(piece.values)
            offsets.extend(list(np.asarray(piece.offsets[1:]) + offsets[-1]))
        return ListArray(
            t,  # type: ignore[arg-type]
            np.asarray(offsets, dtype=np.int64),
            concat_arrays(child_parts),
            np.concatenate([a.null_mask() for a in arrays]) if any_null else None,
        )
    if isinstance(first, StructArray):
        cols = {
            name: concat_arrays([a.field(name) for a in arrays])  # type: ignore[attr-defined]
            for name in first.fields
        }
        return StructArray(
            t,  # type: ignore[arg-type]
            cols,
            np.concatenate([a.null_mask() for a in arrays]) if any_null else None,
        )
    if isinstance(first, DictionaryArray):
        vals = []
        for a in arrays:
            vals.extend(a.to_pylist())
        return DictionaryArray.from_pylist(t, vals)  # type: ignore[arg-type]
    if isinstance(first, NullArray):
        return NullArray(total)
    raise TypeMismatchError(f"不支持拼接 {type(first).__name__}")


def vector_matrix(array: Array) -> np.ndarray:
    """把向量列转成 ``(n, D)`` 矩阵；不是向量列就报错。

    这是所有向量索引和暴力检索的唯一入口 —— 保证"距离计算只在矩阵上做"。
    """

    if isinstance(array, FixedSizeListArray):
        return array.to_matrix()
    if isinstance(array, ListArray) and array.type.child.is_floating:
        rows = array.to_pylist()
        dims = {len(r) for r in rows if r is not None}
        if len(dims) > 1:
            raise TypeMismatchError("变长向量列里出现了不一致的维度", dims=sorted(dims))
        d = dims.pop() if dims else 0
        out = np.zeros((len(rows), d), dtype=array.type.child.numpy_dtype)
        for i, r in enumerate(rows):
            if r is not None:
                out[i] = r
        return out
    raise TypeMismatchError(f"{array.type.name} 不是向量列")

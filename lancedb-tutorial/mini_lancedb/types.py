"""mini-LanceDB 的类型系统。

Arrow 的类型系统是整个 Lance/LanceDB 数据模型的基石：Lance 文件的列元数据、
manifest 里的 schema、索引对列的假设，全都建立在一套"逻辑类型 + 物理布局"
的映射上。本模块用一个精简但结构完整的方式复刻它：

* 每一种类型都知道自己的 **numpy 物理表示**（``numpy_dtype``）；
* 每一种类型都能和 Arrow 双向转换（装了 pyarrow 才有用，见 ``to_arrow``）；
* 每一种类型都能序列化成 JSON（写进 manifest 用）。

向量列的建模方式是理解 LanceDB 的关键：它并没有引入新的 "vector" 类型，
而是把 ``FixedSizeList<Float32>[D]`` 这一种普通的嵌套类型当作向量列。
好处是向量天然可以参与投影、切片、join；代价是维度 D 写死在类型里，
所以改维度必须走 schema 演进（``alter_columns``）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

import numpy as np

from .errors import SchemaError, TypeMismatchError

__all__ = [
    "DataType",
    "Null",
    "Bool",
    "Int8",
    "Int16",
    "Int32",
    "Int64",
    "UInt8",
    "UInt16",
    "UInt32",
    "UInt64",
    "Float16",
    "Float32",
    "Float64",
    "Utf8",
    "LargeUtf8",
    "Binary",
    "LargeBinary",
    "Decimal128",
    "Date32",
    "Date64",
    "Time64",
    "Timestamp",
    "Duration",
    "List",
    "LargeList",
    "FixedSizeList",
    "Struct",
    "Map",
    "Dictionary",
    "STRUCT",
    "NULL",
    "BOOL",
    "INT8",
    "INT16",
    "INT32",
    "INT64",
    "UINT8",
    "UINT16",
    "UINT32",
    "UINT64",
    "FLOAT16",
    "FLOAT32",
    "FLOAT64",
    "UTF8",
    "BINARY",
    "DATE32",
    "DATE64",
    "is_vector_type",
    "vector_dim",
    "promote_types",
    "infer_type",
    "infer_schema",
    "parse_type",
    "narrowest_int_type",
    "describe",
    "union_of_types",
]


# ---------------------------------------------------------------------------
# 基类
# ---------------------------------------------------------------------------


class DataType:
    """所有类型的抽象基类。

    子类必须实现 :meth:`_key`（用于相等/哈希）、:meth:`to_json`，
    以及（如果它是定长标量）:attr:`numpy_dtype`。
    """

    __slots__ = ()

    #: 人类可读的名字，例如 ``"int32"`` / ``"fixed_size_list<float32>[128]"``
    name: str = "unknown"

    # -- 序列化 ----------------------------------------------------------
    def to_json(self) -> Dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def from_json(obj: Any) -> "DataType":
        return _type_from_json(obj)

    # -- 语义 ------------------------------------------------------------
    @property
    def is_nested(self) -> bool:
        """是否包含子列（List / Struct / Map / Dictionary 都是）。"""
        return False

    @property
    def is_primitive(self) -> bool:
        return not self.is_nested

    @property
    def numpy_dtype(self) -> np.dtype:
        raise TypeMismatchError(f"{self.name} 没有直接的 numpy 物理类型")

    @property
    def numpy_dtype_or_none(self) -> Optional[np.dtype]:
        try:
            return self.numpy_dtype
        except TypeMismatchError:
            return None

    @property
    def is_floating(self) -> bool:
        return False

    @property
    def is_integer(self) -> bool:
        return False

    @property
    def is_temporal(self) -> bool:
        return False

    @property
    def supports_nan(self) -> bool:
        return self.is_floating

    # -- Arrow 互操作 ----------------------------------------------------
    def to_arrow(self) -> Any:
        """返回对应的 ``pyarrow.DataType``；未安装 pyarrow 时抛 ImportError。"""
        import pyarrow as pa  # noqa: F401  (延迟导入)

        return self._to_arrow(pa)

    def _to_arrow(self, pa: Any) -> Any:
        raise NotImplementedError(self.name)

    @staticmethod
    def from_arrow(arrow_type: Any) -> "DataType":
        return _type_from_arrow(arrow_type)

    # -- 相等性 ----------------------------------------------------------
    def _key(self) -> tuple:
        return (type(self).__name__,)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, DataType) and self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __repr__(self) -> str:
        return self.name

    # -- 校验 ------------------------------------------------------------
    def validate_scalar(self, value: Any) -> None:
        """检查一个 Python 标量是否能无损塞进这个类型（不做转换）。"""

        if value is None:
            return
        if not self._scalar_ok(value):
            raise TypeMismatchError(
                f"值 {value!r} ({type(value).__name__}) 不能放进 {self.name}"
            )

    def _scalar_ok(self, value: Any) -> bool:
        return True

    def coerce_scalar(self, value: Any) -> Any:
        """尽力把一个 Python 值转换成该类型的原生表示（字符串 → 日期等）。"""

        return value


# ---------------------------------------------------------------------------
# 定长标量类型
# ---------------------------------------------------------------------------


class _FixedWidthType(DataType):
    """int/float/bool 这类在内存里定长的标量类型。"""

    __slots__ = ("_numpy_dtype", "name")

    def __init__(self, name: str, numpy_dtype: str) -> None:
        self.name = name
        self._numpy_dtype = np.dtype(numpy_dtype)

    @property
    def numpy_dtype(self) -> np.dtype:
        return self._numpy_dtype

    @property
    def bits(self) -> int:
        return self._numpy_dtype.itemsize * 8

    def _key(self) -> tuple:
        return (type(self).__name__, self.name)

    def to_json(self) -> Dict[str, Any]:
        return {"name": self.name}

    def _to_arrow(self, pa: Any) -> Any:
        return _ARROW_SCALARS[self.name](pa)

    def _scalar_ok(self, value: Any) -> bool:
        return isinstance(value, (int, float, np.number)) or (
            self.name == "bool" and isinstance(value, (bool, np.bool_))
        )

    def coerce_scalar(self, value: Any) -> Any:
        if value is None:
            return None
        return self._numpy_dtype.type(value)

    def __repr__(self) -> str:
        return self.name


class Null(DataType):
    """``null`` 类型 —— 只用来描述"这一列全是 null"。"""

    name = "null"
    __slots__ = ()

    def _key(self) -> tuple:
        return ("Null",)

    def to_json(self) -> Dict[str, Any]:
        return {"name": "null"}

    def _to_arrow(self, pa: Any) -> Any:
        return pa.null()

    @property
    def numpy_dtype(self) -> np.dtype:
        return np.dtype(object)

    def _scalar_ok(self, value: Any) -> bool:
        return value is None


class Bool(_FixedWidthType):
    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("bool", "bool")

    @property
    def is_integer(self) -> bool:
        # Arrow 里 bool 不算整数，但表达式求值时我们允许它参与逻辑运算
        return False


class Int8(_FixedWidthType):
    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("int8", "int8")

    @property
    def is_integer(self) -> bool:
        return True


class Int16(_FixedWidthType):
    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("int16", "int16")

    @property
    def is_integer(self) -> bool:
        return True


class Int32(_FixedWidthType):
    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("int32", "int32")

    @property
    def is_integer(self) -> bool:
        return True


class Int64(_FixedWidthType):
    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("int64", "int64")

    @property
    def is_integer(self) -> bool:
        return True


class UInt8(_FixedWidthType):
    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("uint8", "uint8")

    @property
    def is_integer(self) -> bool:
        return True


class UInt16(_FixedWidthType):
    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("uint16", "uint16")

    @property
    def is_integer(self) -> bool:
        return True


class UInt32(_FixedWidthType):
    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("uint32", "uint32")

    @property
    def is_integer(self) -> bool:
        return True


class UInt64(_FixedWidthType):
    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("uint64", "uint64")

    @property
    def is_integer(self) -> bool:
        return True


class Float16(_FixedWidthType):
    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("float16", "float16")

    @property
    def is_floating(self) -> bool:
        return True


class Float32(_FixedWidthType):
    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("float32", "float32")

    @property
    def is_floating(self) -> bool:
        return True


class Float64(_FixedWidthType):
    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("float64", "float64")

    @property
    def is_floating(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# 变长字节 / 字符串
# ---------------------------------------------------------------------------


class _VarWidthType(DataType):
    __slots__ = ("name", "offset_bits")

    def __init__(self, name: str, offset_bits: int = 32) -> None:
        self.name = name
        self.offset_bits = offset_bits

    def _key(self) -> tuple:
        return (type(self).__name__, self.name)

    def to_json(self) -> Dict[str, Any]:
        return {"name": self.name}

    @property
    def numpy_dtype(self) -> np.dtype:
        # 物理上不是定长，但 Python 侧统一用 object 数组承载
        return np.dtype(object)

    def _scalar_ok(self, value: Any) -> bool:
        return isinstance(value, (str, bytes, bytearray))


class Utf8(_VarWidthType):
    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("utf8", 32)

    def _to_arrow(self, pa: Any) -> Any:
        return pa.string()

    def _scalar_ok(self, value: Any) -> bool:
        return isinstance(value, str)

    def coerce_scalar(self, value: Any) -> Any:
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, (bytes, bytearray)):
            return bytes(value).decode("utf-8")
        raise TypeMismatchError(f"无法把 {value!r} 转成 utf8")


class LargeUtf8(_VarWidthType):
    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("large_utf8", 64)

    def _to_arrow(self, pa: Any) -> Any:
        return pa.large_string()

    def _scalar_ok(self, value: Any) -> bool:
        return isinstance(value, str)

    def coerce_scalar(self, value: Any) -> Any:
        return None if value is None else str(value)


class Binary(_VarWidthType):
    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("binary", 32)

    def _to_arrow(self, pa: Any) -> Any:
        return pa.binary()

    def _scalar_ok(self, value: Any) -> bool:
        return isinstance(value, (bytes, bytearray, memoryview))

    def coerce_scalar(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            return value.encode("utf-8")
        return bytes(value)


class LargeBinary(_VarWidthType):
    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("large_binary", 64)

    def _to_arrow(self, pa: Any) -> Any:
        return pa.large_binary()

    def _scalar_ok(self, value: Any) -> bool:
        return isinstance(value, (bytes, bytearray, memoryview))

    def coerce_scalar(self, value: Any) -> Any:
        return Binary.coerce_scalar(self, value)


# ---------------------------------------------------------------------------
# 十进制 / 时间
# ---------------------------------------------------------------------------


class Decimal128(DataType):
    __slots__ = ("precision", "scale", "name")

    def __init__(self, precision: int = 38, scale: int = 10) -> None:
        if not (1 <= precision <= 38):
            raise SchemaError("Decimal128 精度必须在 1..38", precision=precision)
        if not (-precision < scale <= precision):
            raise SchemaError("Decimal128 scale 越界", scale=scale, precision=precision)
        self.precision = precision
        self.scale = scale
        self.name = f"decimal128({precision}, {scale})"

    def _key(self) -> tuple:
        return ("Decimal128", self.precision, self.scale)

    def to_json(self) -> Dict[str, Any]:
        return {"name": "decimal128", "precision": self.precision, "scale": self.scale}

    def _to_arrow(self, pa: Any) -> Any:
        return pa.decimal128(self.precision, self.scale)

    @property
    def numpy_dtype(self) -> np.dtype:
        # 教学实现里用 int128 承载，实际 Lance 是按 16 字节定长存的
        return np.dtype("int64")

    def _scalar_ok(self, value: Any) -> bool:
        return isinstance(value, (int, float, str)) or hasattr(value, "as_tuple")


class Date32(DataType):
    """自 1970-01-01 起的天数（int32）。"""

    name = "date32"
    __slots__ = ()

    def _key(self) -> tuple:
        return ("Date32",)

    def to_json(self) -> Dict[str, Any]:
        return {"name": "date32"}

    def _to_arrow(self, pa: Any) -> Any:
        return pa.date32()

    @property
    def numpy_dtype(self) -> np.dtype:
        return np.dtype("int32")

    @property
    def is_temporal(self) -> bool:
        return True

    def _scalar_ok(self, value: Any) -> bool:
        import datetime as _dt

        return isinstance(value, (int, np.integer, _dt.date))

    def coerce_scalar(self, value: Any) -> Any:
        import datetime as _dt

        if value is None:
            return None
        if isinstance(value, _dt.date) and not isinstance(value, _dt.datetime):
            return (value - _dt.date(1970, 1, 1)).days
        if isinstance(value, _dt.datetime):
            return (value.date() - _dt.date(1970, 1, 1)).days
        return int(value)


class Date64(DataType):
    """自 1970-01-01 起的毫秒数（int64）。"""

    name = "date64"
    __slots__ = ()

    def _key(self) -> tuple:
        return ("Date64",)

    def to_json(self) -> Dict[str, Any]:
        return {"name": "date64"}

    def _to_arrow(self, pa: Any) -> Any:
        return pa.date64()

    @property
    def numpy_dtype(self) -> np.dtype:
        return np.dtype("int64")

    @property
    def is_temporal(self) -> bool:
        return True

    def _scalar_ok(self, value: Any) -> bool:
        return isinstance(value, (int, np.integer)) or hasattr(value, "year")


class Time64(DataType):
    __slots__ = ("unit", "name")

    def __init__(self, unit: str = "us") -> None:
        if unit not in ("us", "ns"):
            raise SchemaError("Time64 只支持 us / ns", unit=unit)
        self.unit = unit
        self.name = f"time64[{unit}]"

    def _key(self) -> tuple:
        return ("Time64", self.unit)

    def to_json(self) -> Dict[str, Any]:
        return {"name": "time64", "unit": self.unit}

    def _to_arrow(self, pa: Any) -> Any:
        return pa.time64(self.unit)

    @property
    def numpy_dtype(self) -> np.dtype:
        return np.dtype("int64")

    def _scalar_ok(self, value: Any) -> bool:
        return isinstance(value, (int, np.integer)) or hasattr(value, "hour")


class Timestamp(DataType):
    """时间戳。

    ``unit`` 取 ``s|ms|us|ns``，``tz`` 为时区字符串或 ``None``。
    Lance 把时间戳存成自 epoch 起的整数计数（有 tz 时是 UTC 计数），
    这一点和 Arrow 完全一致，也是它能在 manifest 里稳定表达时间的原因。
    """

    __slots__ = ("unit", "tz", "name")

    _UNIT_MULTIPLIER = {"s": 1, "ms": 1_000, "us": 1_000_000, "ns": 1_000_000_000}

    def __init__(self, unit: str = "us", tz: Optional[str] = None) -> None:
        if unit not in self._UNIT_MULTIPLIER:
            raise SchemaError("Timestamp unit 必须是 s/ms/us/ns", unit=unit)
        self.unit = unit
        self.tz = tz
        self.name = f"timestamp[{unit}{', tz=' + tz if tz else ''}]"

    def _key(self) -> tuple:
        return ("Timestamp", self.unit, self.tz)

    def to_json(self) -> Dict[str, Any]:
        return {"name": "timestamp", "unit": self.unit, "tz": self.tz}

    def _to_arrow(self, pa: Any) -> Any:
        return pa.timestamp(self.unit, tz=self.tz)

    @property
    def numpy_dtype(self) -> np.dtype:
        return np.dtype("int64")

    @property
    def is_temporal(self) -> bool:
        return True

    def _scalar_ok(self, value: Any) -> bool:
        import datetime as _dt

        return isinstance(value, (int, np.integer, _dt.datetime, _dt.date))

    def coerce_scalar(self, value: Any) -> Any:
        import datetime as _dt

        if value is None:
            return None
        if isinstance(value, _dt.datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=_dt.timezone.utc)
            epoch = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
            delta = value - epoch
            micros = (
                delta.days * 86_400_000_000
                + delta.seconds * 1_000_000
                + delta.microseconds
            )
            mult = self._UNIT_MULTIPLIER[self.unit]
            return micros * mult // 1_000_000
        if isinstance(value, _dt.date):
            return self.coerce_scalar(_dt.datetime(value.year, value.month, value.day))
        return int(value)


class Duration(DataType):
    __slots__ = ("unit", "name")

    def __init__(self, unit: str = "us") -> None:
        if unit not in ("s", "ms", "us", "ns"):
            raise SchemaError("Duration unit 必须是 s/ms/us/ns", unit=unit)
        self.unit = unit
        self.name = f"duration[{unit}]"

    def _key(self) -> tuple:
        return ("Duration", self.unit)

    def to_json(self) -> Dict[str, Any]:
        return {"name": "duration", "unit": self.unit}

    def _to_arrow(self, pa: Any) -> Any:
        return pa.duration(self.unit)

    @property
    def numpy_dtype(self) -> np.dtype:
        return np.dtype("int64")

    def _scalar_ok(self, value: Any) -> bool:
        return isinstance(value, (int, np.integer))


# ---------------------------------------------------------------------------
# 嵌套类型
# ---------------------------------------------------------------------------


class List(DataType):
    """变长列表：``List<child>``。

    物理布局 = 一个 int64/int32 的 offsets 数组 + 一个扁平的子列。
    这是 Arrow 的 "list" 布局，也是 Lance 里嵌套列的基础。
    """

    __slots__ = ("child", "offset_bits", "name")

    def __init__(self, child: DataType, offset_bits: int = 32) -> None:
        self.child = child
        self.offset_bits = offset_bits
        self.name = f"list<{child.name}>"

    @property
    def is_nested(self) -> bool:
        return True

    def _key(self) -> tuple:
        return ("List", self.child, self.offset_bits)

    def to_json(self) -> Dict[str, Any]:
        return {
            "name": "list",
            "child": self.child.to_json(),
            "offset_bits": self.offset_bits,
        }

    def _to_arrow(self, pa: Any) -> Any:
        if self.offset_bits == 64:
            return pa.large_list(self.child._to_arrow(pa))
        return pa.list_(self.child._to_arrow(pa))

    @property
    def numpy_dtype(self) -> np.dtype:
        return np.dtype(object)


class LargeList(List):
    __slots__ = ()

    def __init__(self, child: DataType) -> None:
        super().__init__(child, offset_bits=64)
        # 父类会把 name 设成 ``list<...>``，这里必须改回来 ——
        # 否则 ``parse_type("large_list<x>")`` 的往返会把类型名改掉，
        # 而类型名会进 manifest，于是"读出来再写回去"就不再是同一个类型。
        self.name = f"large_list<{child.name}>"

    def _key(self) -> tuple:
        return ("LargeList", self.child)

    def to_json(self) -> Dict[str, Any]:
        return {"name": "large_list", "child": self.child.to_json()}


class FixedSizeList(DataType):
    """定长列表：``FixedSizeList<child, N>``。

    **向量列就是这个类型**。LanceDB 里一个 ``vector`` 列的类型写作
    ``fixed_size_list<float32>[768]``。定长的好处是：

    * 行 i 的子元素固定落在 ``[i*N, (i+1)*N)``，可以零拷贝切片；
    * 一页里第 j 个子列（dimension）是连续段，所以按下标取某一维是 O(1)；
    * 向量化距离计算可以直接 ``reshape(n, D)`` 后走 BLAS。
    """

    __slots__ = ("child", "list_size", "name")

    def __init__(self, child: DataType, list_size: int) -> None:
        if list_size <= 0:
            raise SchemaError("FixedSizeList 的 list_size 必须为正", list_size=list_size)
        self.child = child
        self.list_size = list_size
        self.name = f"fixed_size_list<{child.name}>[{list_size}]"

    @property
    def is_nested(self) -> bool:
        return True

    def _key(self) -> tuple:
        return ("FixedSizeList", self.child, self.list_size)

    def to_json(self) -> Dict[str, Any]:
        return {
            "name": "fixed_size_list",
            "child": self.child.to_json(),
            "list_size": self.list_size,
        }

    def _to_arrow(self, pa: Any) -> Any:
        child = self.child._to_arrow(pa)
        try:
            return pa.fixed_size_list(child, self.list_size)
        except AttributeError:  # pragma: no cover - 老版本 pyarrow
            return pa.list_(child, self.list_size)

    @property
    def numpy_dtype(self) -> np.dtype:
        # 向量列的"物理 dtype"就是子类型的 dtype，形状信息另算
        return self.child.numpy_dtype_or_none or np.dtype(object)

    @property
    def vector_dtype(self) -> Any:
        """``(np.dtype, dim)`` —— 直接拿来做 ``reshape``。"""

        return (self.child.numpy_dtype, self.list_size)


class Struct(DataType):
    """结构体：子列按名字寻址。"""

    __slots__ = ("fields", "name")

    def __init__(self, fields: Sequence[Any]) -> None:
        from .schema import Field

        self.fields = tuple(
            f if isinstance(f, Field) else Field(*f) if isinstance(f, tuple) else f
            for f in fields
        )
        names = [f.name for f in self.fields]
        if len(set(names)) != len(names):
            raise SchemaError("Struct 里出现重复子字段", names=names)
        inner = ", ".join(f"{f.name}: {f.type.name}" for f in self.fields)
        self.name = f"struct<{inner}>"

    @property
    def is_nested(self) -> bool:
        return True

    def _key(self) -> tuple:
        return ("Struct", tuple((f.name, f.type, f.nullable) for f in self.fields))

    def to_json(self) -> Dict[str, Any]:
        return {
            "name": "struct",
            "fields": [f.to_json() for f in self.fields],
        }

    def _to_arrow(self, pa: Any) -> Any:
        return pa.struct([f._to_arrow_field(pa) for f in self.fields])

    def field(self, name: str) -> Any:
        for f in self.fields:
            if f.name == name:
                return f
        raise SchemaError(f"Struct 里没有字段 {name!r}", available=[f.name for f in self.fields])

    @property
    def numpy_dtype(self) -> np.dtype:
        return np.dtype(object)


class Map(DataType):
    """键值映射，物理上等价于 ``List<Struct<key, value>>``。"""

    __slots__ = ("key_type", "item_type", "keys_sorted", "name")

    def __init__(
        self, key_type: DataType, item_type: DataType, keys_sorted: bool = False
    ) -> None:
        self.key_type = key_type
        self.item_type = item_type
        self.keys_sorted = keys_sorted
        self.name = f"map<{key_type.name}, {item_type.name}>"

    @property
    def is_nested(self) -> bool:
        return True

    def _key(self) -> tuple:
        return ("Map", self.key_type, self.item_type, self.keys_sorted)

    def to_json(self) -> Dict[str, Any]:
        return {
            "name": "map",
            "key_type": self.key_type.to_json(),
            "item_type": self.item_type.to_json(),
            "keys_sorted": self.keys_sorted,
        }

    def _to_arrow(self, pa: Any) -> Any:
        return pa.map_(
            self.key_type._to_arrow(pa),
            self.item_type._to_arrow(pa),
            keys_sorted=self.keys_sorted,
        )

    @property
    def numpy_dtype(self) -> np.dtype:
        return np.dtype(object)


class Dictionary(DataType):
    """字典编码列：值只存一份，行里存下标。

    在 Lance 里这不是一种"用户声明"的类型，而是**物理编码**的一种选择
    （低基数列自动走 dictionary encoding）。这里把它建模成逻辑类型，
    是为了让示例能直白地演示"字典页 + 索引页"的两段式结构。
    """

    __slots__ = ("index_type", "value_type", "ordered", "name")

    def __init__(
        self, index_type: DataType, value_type: DataType, ordered: bool = False
    ) -> None:
        self.index_type = index_type
        self.value_type = value_type
        self.ordered = ordered
        self.name = f"dictionary<{index_type.name}, {value_type.name}>"

    @property
    def is_nested(self) -> bool:
        return True

    def _key(self) -> tuple:
        return ("Dictionary", self.index_type, self.value_type, self.ordered)

    def to_json(self) -> Dict[str, Any]:
        return {
            "name": "dictionary",
            "index_type": self.index_type.to_json(),
            "value_type": self.value_type.to_json(),
            "ordered": self.ordered,
        }

    def _to_arrow(self, pa: Any) -> Any:
        return pa.dictionary(
            self.index_type._to_arrow(pa),
            self.value_type._to_arrow(pa),
            ordered=self.ordered,
        )

    @property
    def numpy_dtype(self) -> np.dtype:
        return self.index_type.numpy_dtype


# ---------------------------------------------------------------------------
# 单例工厂（保持 `Int32()` 每次返回同一个对象，让 `is` 比较也能用）
# ---------------------------------------------------------------------------

_INT8 = Int8()
_INT16 = Int16()
_INT32 = Int32()
_INT64 = Int64()
_UINT8 = UInt8()
_UINT16 = UInt16()
_UINT32 = UInt32()
_UINT64 = UInt64()
_FLOAT16 = Float16()
_FLOAT32 = Float32()
_FLOAT64 = Float64()
_BOOL = Bool()
_NULL = Null()
_UTF8 = Utf8()
_BINARY = Binary()
_DATE32 = Date32()
_DATE64 = Date64()

#: 大写的便捷常量，写法贴近 Arrow：``INT32`` 而不是 ``Int32()``
STRUCT = Struct
NULL = _NULL
BOOL = _BOOL
INT8 = _INT8
INT16 = _INT16
INT32 = _INT32
INT64 = _INT64
UINT8 = _UINT8
UINT16 = _UINT16
UINT32 = _UINT32
UINT64 = _UINT64
FLOAT16 = _FLOAT16
FLOAT32 = _FLOAT32
FLOAT64 = _FLOAT64
UTF8 = _UTF8
BINARY = _BINARY
DATE32 = _DATE32
DATE64 = _DATE64

#: 主键自增 id 用的类型 —— 真实 Lance 用 ``uint64`` 作为稳定的行 id
ROW_ID_TYPE = UINT64


_ARROW_SCALARS = {
    "bool": lambda pa: pa.bool_(),
    "int8": lambda pa: pa.int8(),
    "int16": lambda pa: pa.int16(),
    "int32": lambda pa: pa.int32(),
    "int64": lambda pa: pa.int64(),
    "uint8": lambda pa: pa.uint8(),
    "uint16": lambda pa: pa.uint16(),
    "uint32": lambda pa: pa.uint32(),
    "uint64": lambda pa: pa.uint64(),
    "float16": lambda pa: pa.float16(),
    "float32": lambda pa: pa.float32(),
    "float64": lambda pa: pa.float64(),
}


# ---------------------------------------------------------------------------
# JSON 反序列化
# ---------------------------------------------------------------------------


def _type_from_json(obj: Any) -> DataType:
    if isinstance(obj, DataType):
        return obj
    if isinstance(obj, str):
        return parse_type(obj)
    if not isinstance(obj, Mapping):
        raise SchemaError("无法解析类型描述", obj=obj)
    name = obj.get("name")
    if name in _ARROW_SCALARS:
        return _FIXED_BY_NAME[name]
    if name == "null":
        return NULL
    if name == "utf8":
        return _UTF8
    if name == "large_utf8":
        return LargeUtf8()
    if name == "binary":
        return _BINARY
    if name == "large_binary":
        return LargeBinary()
    if name == "decimal128":
        return Decimal128(int(obj.get("precision", 38)), int(obj.get("scale", 10)))
    if name == "date32":
        return _DATE32
    if name == "date64":
        return _DATE64
    if name == "time64":
        return Time64(obj.get("unit", "us"))
    if name == "timestamp":
        return Timestamp(obj.get("unit", "us"), obj.get("tz"))
    if name == "duration":
        return Duration(obj.get("unit", "us"))
    if name == "list":
        return List(_type_from_json(obj["child"]), int(obj.get("offset_bits", 32)))
    if name == "large_list":
        return LargeList(_type_from_json(obj["child"]))
    if name == "fixed_size_list":
        return FixedSizeList(_type_from_json(obj["child"]), int(obj["list_size"]))
    if name == "struct":
        from .schema import Field

        return Struct([Field.from_json(f) for f in obj["fields"]])
    if name == "map":
        return Map(
            _type_from_json(obj["key_type"]),
            _type_from_json(obj["item_type"]),
            bool(obj.get("keys_sorted", False)),
        )
    if name == "dictionary":
        return Dictionary(
            _type_from_json(obj["index_type"]),
            _type_from_json(obj["value_type"]),
            bool(obj.get("ordered", False)),
        )
    raise SchemaError(f"未知类型名 {name!r}", payload=obj)


_FIXED_BY_NAME: Dict[str, DataType] = {
    "bool": _BOOL,
    "int8": _INT8,
    "int16": _INT16,
    "int32": _INT32,
    "int64": _INT64,
    "uint8": _UINT8,
    "uint16": _UINT16,
    "uint32": _UINT32,
    "uint64": _UINT64,
    "float16": _FLOAT16,
    "float32": _FLOAT32,
    "float64": _FLOAT64,
}


# ---------------------------------------------------------------------------
# Arrow 反序列化
# ---------------------------------------------------------------------------


def _type_from_arrow(t: Any) -> DataType:
    import pyarrow as pa

    if pa.types.is_null(t):
        return NULL
    if pa.types.is_boolean(t):
        return _BOOL
    for name, factory in (
        ("int8", _INT8),
        ("int16", _INT16),
        ("int32", _INT32),
        ("int64", _INT64),
        ("uint8", _UINT8),
        ("uint16", _UINT16),
        ("uint32", _UINT32),
        ("uint64", _UINT64),
        ("float16", _FLOAT16),
        ("float32", _FLOAT32),
        ("float64", _FLOAT64),
    ):
        if getattr(pa.types, f"is_{name}")(t):
            return factory
    if pa.types.is_string(t):
        return _UTF8
    if pa.types.is_large_string(t):
        return LargeUtf8()
    if pa.types.is_binary(t) or pa.types.is_fixed_size_binary(t):
        return _BINARY
    if pa.types.is_large_binary(t):
        return LargeBinary()
    if pa.types.is_decimal(t):
        return Decimal128(t.precision, t.scale)
    if pa.types.is_date32(t):
        return _DATE32
    if pa.types.is_date64(t):
        return _DATE64
    if pa.types.is_time64(t):
        return Time64("ns" if t.unit == "ns" else "us")
    if pa.types.is_timestamp(t):
        return Timestamp(t.unit, t.tz)
    if pa.types.is_duration(t):
        return Duration(t.unit)
    if pa.types.is_dictionary(t):
        return Dictionary(
            _type_from_arrow(t.index_type),
            _type_from_arrow(t.value_type),
            bool(t.ordered),
        )
    if pa.types.is_map(t):
        return Map(
            _type_from_arrow(t.key_type),
            _type_from_arrow(t.item_type),
            bool(getattr(t, "keys_sorted", False)),
        )
    if pa.types.is_fixed_size_list(t):
        return FixedSizeList(_type_from_arrow(t.value_type), t.list_size)
    if pa.types.is_large_list(t):
        return LargeList(_type_from_arrow(t.value_type))
    if pa.types.is_list(t):
        return List(_type_from_arrow(t.value_type))
    if pa.types.is_struct(t):
        from .schema import Field

        return Struct(
            [
                Field.from_arrow(t.field(i))
                for i in range(t.num_fields)
            ]
        )
    raise SchemaError(f"不支持从 Arrow 类型转换: {t}")


# ---------------------------------------------------------------------------
# 字符串解析（`"int32"` / `"fixed_size_list<float32>[768]"`）
# ---------------------------------------------------------------------------


def _split_top_level(text: str, sep: str = ",") -> List[str]:
    """按分隔符切开，但忽略 ``<>`` 和 ``[]`` 里面的一层。

    ``"x: int64, y: struct<a: utf8, b: int32>"`` 必须切成两项，
    而不是四项 —— 朴素 ``str.split(",")`` 会把嵌套类型的内部逗号也切开。
    """

    out: List[str] = []
    depth = 0
    cur: List[str] = []
    for ch in text:
        if ch in "<[":
            depth += 1
        elif ch in ">]":
            depth -= 1
        if ch == sep and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur).strip())
    return [p for p in out if p]


def parse_type(text: str) -> DataType:
    """把类型字符串解析成 :class:`DataType`。

    支持的写法::

        int32 / float32 / utf8 / binary / bool / date32
        list<int64>
        large_list<utf8>
        fixed_size_list<float32>[128]
        struct<x: int64, y: utf8>
        struct<x: int64, y: struct<a: utf8, b: int32>>   （嵌套也行）
        map<utf8, int64>
        timestamp[us] / timestamp[us, tz=UTC]
        decimal128(38, 10)
    """

    s = text.strip()
    lower = s.lower()
    if lower in _FIXED_BY_NAME:
        return _FIXED_BY_NAME[lower]
    if lower in ("null", "utf8", "string", "large_utf8", "binary", "large_binary"):
        if lower == "string":
            return _UTF8
        return {
            "null": NULL,
            "utf8": _UTF8,
            "large_utf8": LargeUtf8(),
            "binary": _BINARY,
            "large_binary": LargeBinary(),
        }[lower]
    if lower.startswith("fixed_size_list<"):
        # 必须用"跳过嵌套尖括号"的扫描，不能用 partition(">")：
        # 对 ``fixed_size_list<fixed_size_list<float16>[128]>[N]``
        # （多向量列就是这种类型）第一个 ">" 属于内层，partition 会切错。
        inner = _strip_outer(s, "fixed_size_list<")
        rest = s[len("fixed_size_list<") + len(inner) + 1 :].strip()
        if not rest.startswith("["):
            raise SchemaError(f"fixed_size_list 缺少 [N]: {text!r}")
        size = int(rest[1:].rstrip("]").strip())
        return FixedSizeList(parse_type(inner), size)
    if lower.startswith("list<"):
        return List(parse_type(_strip_outer(s, "list<")))
    if lower.startswith("large_list<"):
        return LargeList(parse_type(_strip_outer(s, "large_list<")))
    if lower.startswith("struct<"):
        from .schema import Field

        inner = _strip_outer(s, "struct<")
        fields = []
        for part in _split_top_level(inner):
            if ":" not in part:
                raise SchemaError(f"struct 字段缺少类型: {part!r}", expression=text)
            fname, _, ftype = part.partition(":")
            fields.append(Field(fname.strip().strip('"'), parse_type(ftype.strip())))
        return Struct(fields)
    if lower.startswith("map<"):
        inner = _strip_outer(s, "map<")
        parts = _split_top_level(inner)
        if len(parts) != 2:
            raise SchemaError(f"map 需要恰好两个类型参数: {text!r}")
        return Map(parse_type(parts[0]), parse_type(parts[1]))
    if lower.startswith("timestamp"):
        # 括号里是 ``unit`` 或 ``unit, tz=XXX``。之前用 partition(",") 再找 "]"
        # 的写法在只有 unit 时会抛 ValueError（找不到 "]"），
        # 这类"只在某一种写法下才炸"的解析 bug 很典型。
        body = s[s.index("[") + 1 : s.rindex("]")] if "[" in s else "us"
        unit, _, tz_part = body.partition(",")
        unit = unit.strip() or "us"
        tz = tz_part.split("=", 1)[1].strip() if "=" in tz_part else None
        return Timestamp(unit, tz or None)
    if lower.startswith("decimal128"):
        if "(" in s:
            body = s[s.index("(") + 1 : s.rindex(")")]
            parts = [p.strip() for p in body.split(",")]
            return Decimal128(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
        return Decimal128()
    if lower.startswith("dictionary<"):
        inner = _strip_outer(s, "dictionary<")
        parts = _split_top_level(inner)
        if len(parts) != 2:
            raise SchemaError(f"dictionary 需要恰好两个类型参数: {text!r}")
        return Dictionary(parse_type(parts[0]), parse_type(parts[1]))
    raise SchemaError(f"无法解析类型字符串 {text!r}")


def _strip_outer(s: str, prefix: str) -> str:
    """去掉 ``prefix<...>`` 的最外层尖括号。"""

    inner = s[len(prefix) :]
    depth = 0
    for i, ch in enumerate(inner):
        if ch == "<":
            depth += 1
        elif ch == ">":
            if depth == 0:
                return inner[:i]
            depth -= 1
    return inner


# ---------------------------------------------------------------------------
# 类型推断与提升
# ---------------------------------------------------------------------------


def infer_type(values: Sequence[Any]) -> DataType:
    """从一列 Python 值推断出一个"足够宽"的类型。

    规则（和 Arrow 的推断基本一致，但更保守）：

    * 全 None → ``null``
    * bool 不与数字混用 —— 混了就当 ``utf8`` 之外的错误处理
    * 整数看取值范围选最小能装下的（int8/16/32/64，负数/非负分别走 int/uint）
    * 有 float 就上 float64（除非全都能精确表示成 float32 —— 我们不去猜，直接 float64）
    * list/tuple 且长度全部相同 → ``fixed_size_list``（这正是向量自动识别的入口！）
    * list/tuple 长度不一 → ``list<...>``
    * dict → ``struct``
    """

    seen: List[Any] = []
    for v in values:
        if v is None:
            continue
        seen.append(v)
    if not seen:
        return NULL

    first = seen[0]
    if isinstance(first, bool) or isinstance(first, np.bool_):
        return _BOOL
    if isinstance(first, (int, np.integer)):
        return _infer_int_type(seen)
    if isinstance(first, (float, np.floating)):
        return _FLOAT64
    if isinstance(first, str):
        return _UTF8
    if isinstance(first, (bytes, bytearray, memoryview)):
        return _BINARY
    if isinstance(first, (list, tuple, np.ndarray)):
        return _infer_seq_type(seen)
    if isinstance(first, Mapping):
        return _infer_struct_type(seen)
    import datetime as _dt

    if isinstance(first, _dt.datetime):
        return Timestamp("us", "UTC")
    if isinstance(first, _dt.date):
        return _DATE32
    # 兜底：转成字符串，和 Arrow 对未知对象的处理一致
    return _UTF8


def _infer_int_type(values: Sequence[Any]) -> DataType:
    """Python int → ``int64``。

    刻意和 Arrow 对齐：pyarrow 从 Python int 推断出的永远是 ``int64``，
    不会去"猜最小能装下的类型"。原因很实际 —— 表是长期存在的，
    按当前数据选最小类型意味着**下一批数据就可能装不下**，
    于是要重写整列。稳定的类型比省几字节重要。
    """

    return _INT64


def narrowest_int_type(values: Sequence[Any]) -> DataType:
    """反过来：挑能装下这批值的最小整数类型。

    写文件时不用它（那会让类型随数据漂移），但**编码器**会用它来决定
    "位宽" —— Lance 的 ``bitpack`` / ``FOR`` 编码就是这么选 bit width 的。
    """

    seen = [int(v) for v in values if v is not None]
    if not seen:
        return _INT64
    lo, hi = min(seen), max(seen)
    if lo >= 0:
        for t in (_UINT8, _UINT16, _UINT32, _UINT64):
            if hi <= int(np.iinfo(t.numpy_dtype).max):
                return t
        return _UINT64
    for t in (_INT8, _INT16, _INT32, _INT64):
        info = np.iinfo(t.numpy_dtype)
        if lo >= int(info.min) and hi <= int(info.max):
            return t
    return _INT64


def _infer_seq_type(values: Sequence[Any]) -> DataType:
    lengths = {len(v) for v in values}
    flat: List[Any] = []
    for v in values:
        flat.extend(list(v))
    child = infer_type(flat)
    if len(lengths) == 1:
        n = lengths.pop()
        if n == 0:
            return List(child)
        return FixedSizeList(child, n)
    return List(child)


def _infer_struct_type(values: Sequence[Mapping]) -> DataType:
    from .schema import Field

    keys: List[str] = []
    for v in values:
        for k in v:
            if k not in keys:
                keys.append(k)
    fields = []
    for k in keys:
        col = [v.get(k) for v in values]
        fields.append(Field(k, infer_type(col), nullable=any(c is None for c in col)))
    return Struct(fields)


def infer_schema(rows: Sequence[Mapping[str, Any]], *, sample_size: int = 1000) -> Any:
    """从 ``[{"a": 1, "b": [0.1, 0.2]}, ...]`` 推断出完整 Schema。"""

    from .schema import Field, Schema

    if not rows:
        raise SchemaError("无法从空数据推断 Schema")
    sample = rows[:sample_size]
    keys: List[str] = []
    for r in sample:
        for k in r:
            if k not in keys:
                keys.append(k)
    fields = []
    for k in keys:
        col = [r.get(k) for r in sample]
        t = infer_type(col)
        fields.append(Field(k, t, nullable=any(c is None for c in col)))
    return Schema(fields)


def promote_types(a: DataType, b: DataType) -> Optional[DataType]:
    """求两个类型的公共超类型；不可兼容时返回 ``None``。

    用在 schema 演进（append 的数据列类型和表 schema 不一致）和
    表达式类型推导里。矩阵很小，直接查表：

    * 相同 → 本身
    * 整数 × 整数 → 更宽的那个（有符号优先）
    * 整数 × 浮点 → 浮点
    * 任意 × null → 另一个
    * 嵌套 → 只要能递归提升到同一个类型就行
    """

    if a == b:
        return a
    if isinstance(a, Null):
        return b
    if isinstance(b, Null):
        return a
    if a.is_floating and b.is_floating:
        return a if a.bits >= b.bits else b  # type: ignore[attr-defined]
    if a.is_floating and b.is_integer:
        return a
    if b.is_floating and a.is_integer:
        return b
    if a.is_integer and b.is_integer:
        a_signed = a.numpy_dtype.kind == "i"
        b_signed = b.numpy_dtype.kind == "i"
        if a_signed == b_signed:
            return a if a.bits >= b.bits else b  # type: ignore[attr-defined]
        # 有符号 + 无符号：一律升到有符号里更宽的
        signed = a if a_signed else b
        unsigned = b if a_signed else a
        if signed.bits > unsigned.bits:  # type: ignore[attr-defined]
            return signed
        for cand in (_INT16, _INT32, _INT64):
            if cand.bits > unsigned.bits:  # type: ignore[attr-defined]
                return cand
        return _INT64
    if isinstance(a, (Utf8, LargeUtf8)) or isinstance(b, (Utf8, LargeUtf8)):
        # 字符串和别的类型混 —— 不做隐式提升，交给上层报
        return None
    if isinstance(a, FixedSizeList) and isinstance(b, FixedSizeList):
        if a.list_size != b.list_size:
            return None
        child = promote_types(a.child, b.child)
        return None if child is None else FixedSizeList(child, a.list_size)
    if isinstance(a, List) and isinstance(b, List):
        child = promote_types(a.child, b.child)
        return None if child is None else List(child)
    if isinstance(a, Struct) and isinstance(b, Struct):
        return None
    return None


# ---------------------------------------------------------------------------
# 向量列识别
# ---------------------------------------------------------------------------


def is_vector_type(t: DataType) -> bool:
    """向量列的判定：``FixedSizeList<浮点>`` 或 ``List<浮点>``。

    真实 LanceDB 还允许 ``Float16``/``Float64``，以及固定长度的
    ``Binary`` 当作二进制向量用。
    """

    if isinstance(t, FixedSizeList):
        return t.child.is_floating
    if isinstance(t, List):
        return t.child.is_floating
    return False


def vector_dim(t: DataType) -> Optional[int]:
    """向量维度；非定长向量返回 ``None``。"""

    if isinstance(t, FixedSizeList) and t.child.is_floating:
        return t.list_size
    return None


def describe(t: DataType) -> str:
    """带一点解释的类型描述，教程里用来打印 schema 概览。"""

    bits = [t.name]
    if is_vector_type(t):
        dim = vector_dim(t)
        if dim is not None:
            bits.append(f"→ 向量列，维度 {dim}")
        else:
            bits.append("→ 变长向量列")
    if isinstance(t, (Utf8, LargeUtf8)):
        bits.append("→ 可建 FTS / BITMAP / NGRAM 索引")
    if t.is_integer or isinstance(t, Bool):
        bits.append("→ 可建 BTREE / BITMAP 索引")
    return " ".join(bits)


def union_of_types(types: Sequence[DataType]) -> DataType:
    """把一批类型归并成一个（schema 推断里用于合并多个 batch）。"""

    if not types:
        return NULL
    acc = types[0]
    for t in types[1:]:
        merged = promote_types(acc, t)
        if merged is None:
            raise TypeMismatchError(
                "无法归并类型", left=acc.name, right=t.name
            )
        acc = merged
    return acc

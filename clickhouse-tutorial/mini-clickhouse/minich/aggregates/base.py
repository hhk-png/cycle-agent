"""聚合函数系统的底座 —— 接口、注册表、状态字节、组合器解析。

聚合是列存数据库**最核心**的一件事,因为列存的价值一大半发生在聚合上:
按列压缩的数据读进来之后,``sum``/``count``/``uniq`` 可以直接在压缩域上
扫一遍,不需要先把行拼回来。理解聚合的**接口形状**,比记住 40 个函数名
重要得多:

.. code-block:: text

    一次聚合 = 状态机 + 三条边界

    ┌────────────── 状态(state)──────────────┐
    │  一个"中间结果对象":                     │
    │    sum   → 一个数                        │
    │    avg   → (和, 计数)                    │
    │    uniq  → 一个 HyperLogLog 草图         │
    │    groupArray → 一个数组                 │
    └─────────────────────────────────────────┘
          ▲                ▲              │
    create_state      add(行)        insert_result_into
      (空状态)      (一行接一行地喂)     (吐到结果列)

    ┌────────── 三条边界(为什么是这个接口)──────────┐
    │ ① add(state, columns, row)                    │
    │    入口是**列 + 行号**,不是"一行值" ——        │
    │    这样函数可以在内部分批/向量化处理(第 15 章) │
    │ ② merge(dst, src)                             │
    │    把两个**部分状态**并起来 ——                 │
    │    这是"分布式聚合"与"AggregatingMergeTree"的  │
    │    全部秘密:每个分片各算一半,归并时 merge     │
    │ ③ serialize_state / deserialize_state         │
    │    状态要能落盘、能跨网络、能存进物化视图 ——    │
    │    所以状态必须是**可序列化且可合并**的         │
    └──────────────────────────────────────────────┘

**为什么 add 收的是"列 + 行号"而不是"一行标量?"**

因为列存里最贵的操作是"把列拆成一个个 Python 对象"。如果接口是
``add(一行值)``,那么每个函数都必须自己从列里取值,循环体里充满了
``col.get(i)`` —— 而 ``col.get(i)`` 正是把 numpy 标量装箱成 Python 对象的
那一步(慢)。接口用 ``(columns, row)`` 只是把"取第几行"这件事**推迟**到
函数内部,聪明的函数(比如 ``count``)可以整列一次算完,一次装箱都不做。

.. note::

   真实 ClickHouse 的 ``IAggregateFunction`` 接口与这里**同名同形**:
   ``create()`` / ``add(...)`` / ``merge(...)`` / ``insertResultInto(...)`` /
   ``serialize(...)`` / ``deserialize(...)``。mini-clickhouse 把
   ``add`` 的 C++ 版那一堆重载(单值 / 批量 / Arena 分配器)简化成一个
   ``add(state, columns, row)``,语义等价。

**组合器(combinator)是这里最值得读的一段。** 真实 ClickHouse 有十几个
聚合组合器(``-If`` / ``-Array`` / ``-Merge`` / ``-State`` / ``-Distinct`` /
``-OrNull`` / ``-OrDefault`` / ``-Resample`` / ``-ForEach`` …),它们不是
新函数,而是**把一个已有的聚合**套上一层壳:

.. code-block:: text

    sumIf(x, c)      =   -If    → 包住 sum: 条件为假就跳过
    sumArray(a)      =   -Array → 包住 sum: 对每个数组的每个元素求和
    sumMerge(s)      =   -Merge → 包住 sum: 参数是"部分状态",合并即可
    sumState(x)      =   -State → 包住 sum: 返回的是状态(给 AggregatingMergeTree 存)
    uniqDistinct(x)  =   -Distinct → 先去重再喂给 uniq

组合器可以**叠加**:``sumIfMerge`` = ``sum`` + ``-If`` + ``-Merge``。
于是"注册 40 个函数"实际得到的是"40 × 组合器"的几百个可用名字 ——
这正是 ClickHouse ``system.functions`` 里聚合函数看起来那么多的原因。

.. warning::

   **名字冲突规则(必须记住)**:如果某个名字**本身就是注册过的函数**
   (例如 ``sumMap``、``groupUniqArray``、``countDistinct``),它**优先**
   命中注册表,不会被当成"``sum`` + 组合器"。只有当整名查不到时,
   才会尝试剥掉组合器后缀。这条规则保证
   ``get_aggregate_function("sumMap")`` 拿到的是那个真正的 ``sumMap``,
   而不是"``sum`` 套 ``-Map``"(后者根本不存在)。
"""

from __future__ import annotations

import copy
import re
import struct
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..errors import ErrorCode, MiniChError, raise_error
from ..literals import parse_literal, split_top_level
from ..serialization import VarUIntReader, write_var_uint
from ..types import DataType, get_data_type
from ..types.composite import (
    DataTypeAggregateFunction,
    DataTypeArray,
    DataTypeNullable,
    DataTypeTuple,
)

__all__ = [
    "IAggregateFunction",
    "ByteWriter",
    "ByteReader",
    "ValueColumn",
    "encode_key_value",
    "decode_key_value",
    "key_bytes",
    "register_aggregate",
    "register_combinator",
    "split_combinators",
    "combinator_suffixes",
    "get_aggregate_function",
    "aggregate_function_exists",
    "registered_aggregate_functions",
]

#: 64 位掩码 —— HLL 与位运算聚合都会用到
_MASK64 = (1 << 64) - 1

#: 一个"看起来像整数"的文本 —— 从字符串还原数字时判断 int 还是 float
_INT_TEXT_RE = re.compile(r"^-?\d+$")


# ══════════════════════════════════════════════════════════════════════════
# 状态的字节读写 —— 序列化的最小工具
# ══════════════════════════════════════════════════════════════════════════


class ByteWriter:
    """往一个 bytearray 里写各种标量 —— 聚合状态序列化的公共出口。

    .. note::

       真实 ClickHouse 的聚合状态是**直接往一块内存上按结构体读写**的
       (``AggregateFunctionSumData`` 就是 ``memcpy`` 一个 ``T``),
       既没有"长度前缀"也没有"自描述"。mini-clickhouse 为了能让状态
       在 ``serialize_state`` / ``deserialize_state`` 之间**精确往返**,
       选用了"自描述的小端编码" —— 代价是比真实实现多几个字节,
       换来的是"任何状态都能被独立解析回来"。
    """

    __slots__ = ("buf",)

    def __init__(self) -> None:
        self.buf = bytearray()

    def uvarint(self, value: int) -> "ByteWriter":
        write_var_uint(self.buf, int(value))
        return self

    def raw(self, data: bytes) -> "ByteWriter":
        self.buf += bytes(data)
        return self

    def u64(self, value: int) -> "ByteWriter":
        self.buf += struct.pack("<Q", int(value) & _MASK64)
        return self

    def i64(self, value: int) -> "ByteWriter":
        self.buf += struct.pack("<q", int(value))
        return self

    def f64(self, value: float) -> "ByteWriter":
        self.buf += struct.pack("<d", float(value))
        return self

    def string(self, text: str) -> "ByteWriter":
        """写一段文本:``[varint 字节长度][UTF-8 字节]``。"""
        data = str(text).encode("utf-8", "surrogateescape")
        self.uvarint(len(data))
        self.buf += data
        return self

    def number(self, value: Any) -> "ByteWriter":
        """写一个**精确往返**的数字(整数走 str,浮点走 repr)。

        为什么不用定长 ``<q``/``<d``? 因为 ``sum`` 可能累加成超过 64 位的
        Python 大整数,而 ``<q`` 会截断;用十进制文本表示则永远无损,
        且确定性(同一个值永远得到同一串字节)。
        """
        self.string(_number_to_text(value))
        return self

    def bytes(self) -> bytes:
        return bytes(self.buf)


class ByteReader:
    """从字节里读回 :class:`ByteWriter` 写下的东西(带游标)。"""

    __slots__ = ("r",)

    def __init__(self, data: bytes) -> None:
        self.r = VarUIntReader(data)

    def uvarint(self) -> int:
        return self.r.read_var_uint()

    def raw(self, n: int) -> bytes:
        return self.r.read(n)

    def u64(self) -> int:
        return struct.unpack("<Q", self.r.read(8))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.r.read(8))[0]

    def f64(self) -> float:
        return struct.unpack("<d", self.r.read(8))[0]

    def string(self) -> str:
        n = self.r.read_var_uint()
        return self.r.read(n).decode("utf-8", "surrogateescape")

    def number(self) -> Any:
        return _text_to_number(self.string())

    def eof(self) -> bool:
        return self.r.eof()

    def remaining(self) -> int:
        return self.r.remaining()


def _number_to_text(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return repr(float(value))
    return str(value)


def _text_to_number(text: str) -> Any:
    if _INT_TEXT_RE.match(text):
        return int(text)
    return float(text)


# ══════════════════════════════════════════════════════════════════════════
# 用 Python 值喂一行 —— 组合器内部用
# ══════════════════════════════════════════════════════════════════════════


class ValueColumn:
    """一个"假列" —— 把一串 Python 值伪装成 ``IColumn``。

    ``-Array`` / ``-ForEach`` 这些组合器要把**数组里的元素**喂给内层聚合,
    但内层聚合的 ``add`` 收的是"列 + 行号"。与其为每个元素造一个真正的
    ``ColumnVector``(那要装箱到 numpy),不如造一个只实现了
    ``size()`` / ``get(i)`` 的极薄对象 —— 这正是真实 ClickHouse 里
    ``-Array`` 组合器用 ``ColumnArray::getData()`` 那一层的等价物。
    """

    __slots__ = ("values",)

    def __init__(self, values: Sequence[Any]) -> None:
        self.values = values

    def size(self) -> int:
        return len(self.values)

    def get(self, i: int) -> Any:
        return self.values[i]


# ══════════════════════════════════════════════════════════════════════════
# 键的规范化编码 —— uniq / groupUniqArray / Distinct 用
# ══════════════════════════════════════════════════════════════════════════


def encode_key_value(out: ByteWriter, value: Any) -> None:
    """把一个值编码成**确定性**的字节(同一值永远同一串字节)。

    带一个"类型标签字节",这样 ``1``(整数)和 ``'1'``(字符串)不会撞车。
    键的编码要求**确定性**(用于去重)而不是**唯一性**(那需要密码学哈希),
    所以这个简单方案已经够用;真正的哈希在 ``key_bytes`` 之后由
    SipHash 完成。
    """
    if value is None:
        out.raw(b"\x00")
    elif isinstance(value, bool):
        out.raw(b"\x01\x01" if value else b"\x01\x00")
    elif isinstance(value, int):
        out.raw(b"\x02")
        out.string(str(value))
    elif isinstance(value, float):
        out.raw(b"\x03")
        out.f64(value)
    elif isinstance(value, (bytes, bytearray)):
        out.raw(b"\x05")
        data = bytes(value)
        out.uvarint(len(data))
        out.raw(data)
    elif isinstance(value, str):
        out.raw(b"\x04")
        out.string(value)
    elif isinstance(value, (list, tuple)):
        out.raw(b"\x06")
        out.uvarint(len(value))
        for v in value:
            encode_key_value(out, v)
    elif isinstance(value, dict):
        out.raw(b"\x07")
        items = sorted(value.items(), key=lambda kv: repr(kv[0]))
        out.uvarint(len(items))
        for k, v in items:
            encode_key_value(out, k)
            encode_key_value(out, v)
    else:  # pragma: no cover - Date/Decimal 等落到这里
        out.raw(b"\x08")
        out.string(repr(value))


def key_bytes(values: Sequence[Any]) -> bytes:
    """把一行"若干参数值"编码成一段字节(用于去重/哈希)。"""
    out = ByteWriter()
    if len(values) == 1:
        encode_key_value(out, values[0])
    else:
        out.uvarint(len(values))
        for v in values:
            encode_key_value(out, v)
    return out.bytes()


def decode_key_value(r: "ByteReader") -> Any:
    """:func:`encode_key_value` 的逆 —— 从读取器里解出一个值。

    这才是"值 → 字节 → 值"真正**精确往返**的保证:几处需要把任意值
    (min 的当前最小值、groupArray 的元素)存进状态字节的函数都靠它。
    """
    tag = r.raw(1)
    if tag == b"\x00":
        return None
    if tag == b"\x01":
        return bool(r.raw(1)[0])
    if tag == b"\x02":
        return int(r.string())
    if tag == b"\x03":
        return r.f64()
    if tag == b"\x04":
        return r.string()
    if tag == b"\x05":
        return r.raw(r.uvarint())
    if tag == b"\x06":
        n = r.uvarint()
        return tuple(decode_key_value(r) for _ in range(n))
    if tag == b"\x07":
        n = r.uvarint()
        return {decode_key_value(r): decode_key_value(r) for _ in range(n)}
    if tag == b"\x08":
        return r.string()
    raise_error(
        ErrorCode.CANNOT_PARSE_TEXT,
        f"Unknown key tag {tag!r} while decoding aggregate state",
    )


# ══════════════════════════════════════════════════════════════════════════
# 聚合函数接口
# ══════════════════════════════════════════════════════════════════════════


class IAggregateFunction:
    """所有聚合函数的基类 —— 与真实 ClickHouse 的 ``IAggregateFunction`` 同名。

    子类至少要实现四个方法::

        get_return_type()          # 结果类型(只看 arg_types,不看数据)
        create_state()             # 一个空状态
        add(state, columns, row)   # 把第 row 行喂进状态
        insert_result_into(state, column)   # 把最终结果 append 进列

    要成为"可合并、可落盘"的聚合(``AggregatingMergeTree`` 需要),
    还要实现 ``merge`` / ``serialize_state`` / ``deserialize_state`` ——
    本模块提供的所有函数都实现了它们,而且保证**往返精确**。
    """

    #: 函数名(展示用)。子类通常由工厂设成注册时的名字。
    name: str = "unnamed"

    #: 是否禁用对参数列的解引用(``count()`` 这类零参数函数用)。
    _requires_columns: bool = True

    def __init__(
        self,
        name: str,
        arg_types: Sequence[DataType],
        params: Sequence[Any] = (),
    ) -> None:
        self.name = name
        #: 参数列的类型 —— 决定结果类型,也在 ``-State`` 里写进 ``AggregateFunction(...)``
        self.arg_types: List[DataType] = list(arg_types)
        #: 函数参数(``quantile(0.9)`` 里的 ``0.9``、``topK(5)`` 里的 ``5``)
        self.params: List[Any] = list(params)
        self._return_type: Optional[DataType] = None

    # ── 类型 ────────────────────────────────────────────────────────────

    def get_return_type(self) -> DataType:
        """结果类型 —— **只依赖 arg_types / params,不看数据**。

        真实 ClickHouse 里这条是硬约束:准备阶段调用一次,
        运行阶段不再变。违反会导致"结果列的类型与实际值不符"。
        """
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"Aggregate function {self.name} does not implement get_return_type()",
        )

    # ── 状态 ────────────────────────────────────────────────────────────

    def create_state(self) -> Any:
        """新建一个**空**状态(一个分组一份)。"""
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"Aggregate function {self.name} does not implement create_state()",
        )

    def clone_state(self, state: Any) -> Any:
        """深拷贝一个状态。

        默认用 ``deepcopy`` —— 对所有"纯 Python 数据结构"的状态都成立。
        真实 ClickHouse 用"把状态字节从 Arena 拷到目标"的方式,
        语义等价(都得到一份独立状态)。
        """
        return copy.deepcopy(state)

    # ── 三条边界 ────────────────────────────────────────────────────────

    def add(self, state: Any, columns: Sequence[Any], row: int) -> None:
        """把第 ``row`` 行喂进状态 —— 注意入口是**列 + 行号**。"""
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"Aggregate function {self.name} does not implement add()",
        )

    def merge(self, dst_state: Any, src_state: Any) -> None:
        """把 ``src_state`` 合并进 ``dst_state``(就地改 ``dst_state``)。

        这是分布式聚合与 ``AggregatingMergeTree`` 的基石:
        "每个分片各算一半,归并节点 merge 起来"。
        """
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"Aggregate function {self.name} does not implement merge() "
            f"(it cannot be used with -Merge)",
        )

    def insert_result_into(self, state: Any, column: Any) -> None:
        """把最终结果 **append** 进结果列(一个分组一个值)。"""
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"Aggregate function {self.name} does not implement insert_result_into()",
        )

    # ── 序列化 ──────────────────────────────────────────────────────────

    def serialize_state(self, state: Any) -> bytes:
        """把状态序列化成字节(**必须确定性**,同一状态永远同一串字节)。"""
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"Aggregate function {self.name} does not implement serialize_state()",
        )

    def deserialize_state(self, data: bytes) -> Any:
        """从字节还原状态 —— 必须与 ``serialize_state`` 精确互逆。"""
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"Aggregate function {self.name} does not implement deserialize_state()",
        )

    # ── 可选钩子 ────────────────────────────────────────────────────────

    def is_state_combinator(self) -> bool:
        """这个函数是否是 ``-State`` / ``-Merge`` 这类"状态组合器"。"""
        return False

    def requires_arg_columns(self) -> bool:
        """``add`` 是否需要真的参数列(``count`` 返回 ``False``)。"""
        return self._requires_columns

    # ── 内部辅助 ────────────────────────────────────────────────────────

    def _default_result(self) -> Any:
        """空分组时结果列的默认值(``-OrDefault`` 与"空集"用)。"""
        return self.get_return_type().default_value()

    def __repr__(self) -> str:  # pragma: no cover
        args = ", ".join(t.get_name() for t in self.arg_types)
        return f"<{type(self).__name__} {self.name}({args})>"


# ══════════════════════════════════════════════════════════════════════════
# 注册表
# ══════════════════════════════════════════════════════════════════════════

#: 名字(小写) → 工厂。工厂签名 ``(name, params, arg_types) -> IAggregateFunction``
_FACTORY: Dict[str, Callable[[str, Sequence[Any], Sequence[DataType]], IAggregateFunction]] = {}

#: 别名(小写) → 规范名(小写)
_ALIASES: Dict[str, str] = {}

#: 名字(小写) → 一句话说明(``system.functions`` 用)
_DESCRIPTIONS: Dict[str, str] = {}

#: 组合器后缀注册表 —— ``combinators`` 模块启动时填进来。
#: 顺序**重要**:先注册的先尝试剥离。
_COMBINATORS: List[Tuple[str, Callable[..., IAggregateFunction]]] = []


def register_aggregate(
    name: str,
    cls: Optional[type] = None,
    *,
    aliases: Sequence[str] = (),
    description: str = "",
    factory: Optional[Callable[[str, Sequence[Any], Sequence[DataType]], IAggregateFunction]] = None,
) -> Any:
    """注册一个聚合函数。三种用法::

        @register_aggregate("sum", description="求和")
        class AggregateFunctionSum(IAggregateFunction): ...

        register_aggregate("sum", AggregateFunctionSum)

        register_aggregate("median", factory=lambda n, p, a: _Quantile(n, p, a))

    ``factory`` 收到 ``(canonical_name, params, arg_types)``;``cls`` 形式
    则认为类构造器签名是 ``(name, arg_types, params)``。
    """

    def _do(cls_: Optional[type]) -> None:
        key = name.lower()
        if factory is not None:
            _FACTORY[key] = factory
        else:
            if cls_ is None:
                raise_error(
                    ErrorCode.LOGICAL_ERROR,
                    f"Aggregate function {name} registered without an implementation",
                )

            def _default_factory(
                n: str,
                p: Sequence[Any],
                a: Sequence[DataType],
                _cls: type = cls_,
            ) -> IAggregateFunction:
                return _cls(n, a, p)  # type: ignore[call-arg]

            _FACTORY[key] = _default_factory
        if description:
            _DESCRIPTIONS[key] = description
        for alias in aliases:
            akey = alias.lower()
            _ALIASES[akey] = key
            _FACTORY.setdefault(akey, _FACTORY[key])
            if description:
                _DESCRIPTIONS.setdefault(akey, description)

    if factory is not None:
        # 工厂形态:立即注册(不当作装饰器)
        _do(None)
        return None
    if cls is not None:
        # 直接传类:立即注册
        _do(cls)
        return cls
    # 无 cls 无 factory:返回装饰器,交给 @register_aggregate(...) 使用
    return _do


def register_combinator(
    suffix: str,
    handler: Callable[..., IAggregateFunction],
) -> None:
    """注册一个组合器后缀(``sumIf`` 里的 ``If``)。

    ``handler`` 的签名是 ``(inner_name, full_name, params, arg_types, resolve)``:
    它负责**改写参数类型**、递归调用 ``resolve`` 拿到内层函数,
    再套上自己的壳。详见 :mod:`minich.aggregates.combinators`。
    """
    _COMBINATORS.append((suffix, handler))


# ══════════════════════════════════════════════════════════════════════════
# 组合器后缀的**拆分**(供表达式分析器使用)
# ══════════════════════════════════════════════════════════════════════════


def _peelable(name: str) -> bool:
    """这个名字"剥掉若干组合器后缀之后能落到一个真实函数"吗?

    这个判定是拆分算法的基础。没有它,``groupArray`` 会被误拆成
    ``group`` + ``Array`` —— 因为 ``Array`` 恰好是一个组合器后缀,
    而 ``groupArray`` 本身就是一个完整的函数名。
    """
    if _FACTORY.get(name.lower()) is not None:
        return True
    for suffix, _handler in _COMBINATORS:
        if len(name) > len(suffix) and name.lower().endswith(suffix.lower()):
            if _peelable(name[: len(name) - len(suffix)]):
                return True
    return False


def split_combinators(name: str) -> Tuple[str, Optional[str], bool, bool]:
    """把一个聚合函数名拆成 ``(基础名, 组合器链, 是否 -State, 是否 -Merge)``。

    .. code-block:: python

       >>> split_combinators("sum")
       ('sum', None, False, False)
       >>> split_combinators("sumIf")
       ('sum', 'If', False, False)
       >>> split_combinators("sumIfMerge")
       ('sum', 'IfMerge', False, True)
       >>> split_combinators("quantilesTiming")
       ('quantiles', 'Timing'... )  # 见下方说明
       >>> split_combinators("groupArray")   # 不会被误拆
       ('groupArray', None, False, False)

    为什么需要它? 因为**分析期要区分"聚合函数的参数"和"组合器追加的参数"**:
    ``sumIf(x, cond)`` 里 ``cond`` 不是 ``sum`` 的参数,而是 ``-If`` 追加的;
    而在聚合后求值时,``-State`` / ``-Merge`` 决定了"结果列是状态还是最终值"。

    真实 ClickHouse 的做法是在**名字解析期**就把组合器剥出来、构造出
    一个包装过的 ``AggregateFunction``(见 ``AggregateFunctionCombinator``),
    并把"用了哪些组合器"记在聚合函数对象自己身上。
    我们这里额外提供一个纯文本的拆分函数,是为了让**分析器**能在
    还没有类型信息的时候就看清楚名字的结构。
    """
    base = name
    parts: List[str] = []
    while True:
        if _FACTORY.get(base.lower()) is not None:
            break
        matched = False
        for suffix, _handler in _COMBINATORS:
            if len(base) > len(suffix) and base.lower().endswith(suffix.lower()):
                candidate = base[: len(base) - len(suffix)]
                if _peelable(candidate):
                    base = candidate
                    parts.insert(0, suffix)
                    matched = True
                    break
        if not matched:
            break
    combinator = "".join(parts) or None
    lowered = [p.lower() for p in parts]
    return base, combinator, "state" in lowered, "merge" in lowered


def combinator_suffixes() -> List[str]:
    """已注册的组合器后缀(供 ``system.functions`` 与文档引用)。"""
    return [s for s, _ in _COMBINATORS]


def registered_aggregate_functions() -> List[Tuple[str, str]]:
    """返回 ``[(名字, 说明), ...]`` —— ``system.functions`` 里聚合函数的数据源。

    只列出**注册表里真正有实现**的名字(含别名);组合器产生的成千上万个
    变体不在这里,因为它们是运行时按后缀拼出来的。
    """
    out: List[Tuple[str, str]] = []
    for key in sorted(_FACTORY):
        out.append((key, _DESCRIPTIONS.get(key, "")))
    return out


def _lookup_direct(
    name: str, params: Sequence[Any], arg_types: Sequence[DataType]
) -> Optional[IAggregateFunction]:
    factory = _FACTORY.get(name.lower())
    if factory is None:
        return None
    return factory(name, list(params), list(arg_types))


def _split_name_params(text: str) -> Tuple[str, List[Any]]:
    """把 ``"quantiles(0.5, 0.9)"`` 拆成 ``("quantiles", [0.5, 0.9])``。

    允许多层括号与引号里的逗号(借 ``split_top_level``)。
    """
    s = text.strip()
    if "(" not in s:
        return s, []
    i = s.index("(")
    func = s[:i].strip()
    body = s[i + 1 :]
    if not body.endswith(")"):
        raise_error(
            ErrorCode.SYNTAX_ERROR,
            f"Malformed aggregate function name: {text!r}",
        )
    inner = body[:-1]
    params = [parse_literal(p) for p in split_top_level(inner) if p != ""]
    return func, params


def _resolve(
    name: str,
    params: List[Any],
    arg_types: List[DataType],
) -> IAggregateFunction:
    """把名字解析成实例:**先查注册表,再剥组合器后缀**。"""
    direct = _lookup_direct(name, params, arg_types)
    if direct is not None:
        return direct
    lowered = name.lower()
    for suffix, handler in _COMBINATORS:
        if len(name) > len(suffix) and lowered.endswith(suffix.lower()):
            inner_name = name[: len(name) - len(suffix)]
            try:
                return handler(inner_name, name, params, arg_types, _resolve)
            except MiniChError as exc:
                if exc.code == ErrorCode.UNKNOWN_AGGREGATE_FUNCTION:
                    # 这个后缀剥掉之后内层也不认识 → 换下一个后缀再试
                    continue
                raise
    raise_error(
        ErrorCode.UNKNOWN_AGGREGATE_FUNCTION,
        f"Unknown aggregate function {name}",
    )


def get_aggregate_function(
    name: str,
    params: Optional[Sequence[Any]] = None,
    arg_types: Optional[Sequence[DataType]] = None,
) -> IAggregateFunction:
    """按名字取一个聚合函数实例 —— 组合器也在这里解析。

    支持三种调用形态::

        get_aggregate_function("sum", [], [UInt8])          # 普通函数
        get_aggregate_function("quantiles(0.5, 0.9)", [], [UInt8])   # 名字里带参数
        get_aggregate_function("sumIf", [], [UInt8, UInt8])  # 组合器
        get_aggregate_function("sumMerge", [], [AggregateFunction(sum, UInt8)])
        get_aggregate_function("quantilesTiming")            # 名字即可,参数类型后补

    .. note::

       ``arg_types`` 允许为空或"个数不完全对" —— 教学实现不像真实
       ClickHouse 那样在准备阶段严格校验参数个数(那是分析器的职责,
       见 :mod:`minich.execution` 的聚合处理器)。这里对**明显**的
       类型错误(比如把非数字喂给 ``sum``)仍然会抛错。
    """
    arg_list: List[DataType] = list(arg_types) if arg_types else []
    base_name, parsed_params = _split_name_params(name)
    use_params: List[Any] = parsed_params if parsed_params else list(params or [])
    return _resolve(base_name, use_params, arg_list)


def _exists(name: str) -> bool:
    key = name.lower()
    if key in _FACTORY:
        return True
    for suffix, _ in _COMBINATORS:
        if len(name) > len(suffix) and key.endswith(suffix.lower()):
            if _exists(name[: len(name) - len(suffix)]):
                return True
    return False


def aggregate_function_exists(name: str) -> bool:
    """这个名字是否是可解析的聚合函数(含组合器变体)。"""
    base_name, _ = _split_name_params(name)
    return _exists(base_name)


# ══════════════════════════════════════════════════════════════════════════
# 公共小工具(各函数模块复用)
# ══════════════════════════════════════════════════════════════════════════


def is_condition_type(t: DataType) -> bool:
    """``-If`` 的"条件列"是否是合法类型 —— 真实 CH 要求 ``UInt8``。

    mini-clickhouse 额外接受 ``Bool``(它底层就是 UInt8),方便
    ``WHERE`` 里推出来的布尔表达式直接接上。
    """
    if t.is_nullable():
        return is_condition_type(t.nested)  # type: ignore[attr-defined]
    return t.get_name() in ("UInt8", "Bool")


def truthy(value: Any) -> bool:
    """把条件值按 ClickHouse 的布尔语义求值(``0``/``NULL`` 为假)。"""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, (str, bytes)):
        return len(value) > 0
    return bool(value)


def numeric_arg(arg_types: Sequence[DataType], name: str) -> DataType:
    """取出唯一一个数值参数类型,不是数值就报错。"""
    if not arg_types:
        raise_error(
            ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
            f"Aggregate function {name} requires at least one argument",
        )
    t = arg_types[0]
    if not (t.is_numeric() or t.is_decimal()):
        raise_error(
            ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
            f"Aggregate function {name} requires a numeric argument, got {t.get_name()}",
        )
    return t


def make_array_type(element: DataType) -> DataType:
    return DataTypeArray(element)


def make_tuple_type(elements: Sequence[DataType]) -> DataType:
    return DataTypeTuple(list(elements))


def make_nullable(t: DataType) -> DataType:
    if t.is_nullable():
        return t
    if not t.nullable_allowed:
        return t
    return DataTypeNullable(t)


def wrap_int_to_type(value: int, t: DataType) -> int:
    """把整数按目标数值类型**回绕**(C++ 溢出语义,与真实 CH 一致)。"""
    if hasattr(t, "wrap"):
        return t.wrap(value)  # type: ignore[attr-defined]
    return value

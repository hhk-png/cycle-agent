"""算术函数 —— ``plus`` / ``multiply`` / ``intDiv`` / ``bitAnd`` 这一大家子。

算术函数看起来最"没技术含量",其实**压力全在类型系统上**:
同一个 ``plus`` 要同时接受 ``UInt8``、``Decimal(38, 10)``、``Date``、``Float32``,
而且结果类型必须和真实存储一致 —— 一旦推错,下游的序列化、压缩、比较会全部错位。
所以这个模块的代码量有九成是在做**类型推导**,一成才是算数。

真实 ClickHouse 把推导规则放在 ``src/Core/NumberTraits.h``,核心是三个 trait:

.. code-block:: text

   加减乘(ResultOfAdditionMultiplication)   减(ResultOfSubtraction)    除(ResultOfDivision)
   ─────────────────────────────────────   ──────────────────────    ────────────────────
   先看浮点:有 Float 就浮点                 和左边一样,但**强制有符号** 位宽取较宽者,**不进档**
   再看整数:位宽取较宽者,再**进一档**                                  UInt8 / UInt8 = UInt8
     UInt8  + UInt8  = UInt16                                            Int64 / UInt64 = Int64
     UInt8  + UInt16 = UInt32
     UInt16 + UInt16 = UInt32
     UInt64 + UInt64 = UInt64   ← 到 64 位**封顶**(所以会回绕)
   有符号/无符号混用:结果一律有符号
     Int8   + UInt8  = Int16
     Int64  + UInt64 = Int64    ← 真实 CH 这么干(可能溢出)

mini-clickhouse 只在**一处**故意偏离真实 ClickHouse,而且偏得更安全:

.. code-block:: text

   有符号与无符号混用时的位宽:
     真实 CH                       mini-clickhouse
     Int64 + UInt64 → Int64        Int64 + UInt64 → Int128   ← 见下面 warning

"进一档"这条规则值得单独记住,因为它是 ClickHouse 最容易被问到的类型题之一::

    SELECT toTypeName(0), toTypeName(0 + 0), toTypeName(0 + 0 + 0);
    ┌─toTypeName(0)─┬─toTypeName(plus(0, 0))─┬─toTypeName(plus(plus(0, 0), 0))─┐
    │ UInt8         │ UInt16                 │ UInt32                          │
    └───────────────┴────────────────────────┴─────────────────────────────────┘

为什么要有这条规则? 因为 ``UInt8 + UInt8`` 的数学结果可能到 510,塞不进 UInt8。
"进位宽"把绝大多数溢出挡在了类型推导阶段 —— 但**只挡一档**,
``UInt64 + UInt64`` 依然会回绕(见 :mod:`minich.columns.base` 里对 PaddedPODArray 的讨论)。

.. warning::

   **与真实 ClickHouse 的差异**(全部写在每个函数的 docstring 里,这里汇总):

   1. 有符号/无符号混用:真实 CH 取较大的位宽(``Int64 + UInt64 = Int64``,
      会溢出);mini-clickhouse 借 ``get_wider_numeric_type`` 把位宽**翻倍**
      (``Int64 + UInt64 = Int128``)。更安全,但和真实 CH 不一致。
   2. ``divide`` 在 mini-clickhouse 里对整数对**一定**返回浮点
      (``divide(1, 2) = 0.5``),这与真实 CH 一致;而 ``intDiv(1, 2) = 0``。
      两者的分工见 :class:`FunctionDivide` 的 docstring。
   3. ``Decimal`` 的加/减:结果精度用 ``precision_a + precision_b``(上限 76),
      真实 CH 只加 1 位。乘法按"scale 相加"推(与真实 CH 一致)。
   4. 日期/时间的 ``+`` / ``-`` 只实现了**整数**与**固定秒数**的 Interval
      (Second/Minute/Hour/Day/Week);``INTERVAL 1 MONTH`` 这类日历运算
      会抛 ``NOT_IMPLEMENTED_IN_MINICH``,请用 :mod:`minich.functions.datetime_funcs`
      里的 ``addMonths`` 一族。
   5. 位运算不检查两侧位宽是否相同(真实 CH 对 ``bitAnd`` 之类的重载会要求
      相同位宽),窄的一侧按结果类型扩宽后再算。

这个模块还**顺带承担了整个 functions 包的底层设施**(``storage_layout`` /
``np_dtype_of`` / ``wrap_value`` / ``common_supertype``)—— 因为它们全都是
"类型与列之间怎么互转"的问题,放在算术模块里最自然,别的模块 import 过去用即可。
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, localcontext
from decimal import Decimal as _PyDecimal
from typing import Any, Dict, Optional, Sequence, Tuple

import numpy as np

from ..block import ColumnWithTypeAndName
from ..columns import ColumnVector, IColumn
from ..errors import ErrorCode, raise_error
from ..types import DataType, get_data_type, get_wider_numeric_type
from ..types.composite import DataTypeDecimal
from ..types.scalars import DataTypeNumber
from .base import FunctionBase, execute_with_null_propagation, register_function

__all__ = [
    # 公共底座(别的函数模块会 import 这些)
    "storage_layout",
    "np_dtype_of",
    "unwrap_nullable",
    "wrap_value",
    "coerce_value",
    "full_column",
    "numpy_arg",
    "is_integer_like",
    "is_date_time_like",
    "common_supertype",
    "arithmetic_result_type",
    "compute_arithmetic_scalar",
    "check_arity",
    "ILLEGAL_DIVISION",
    # 函数实现
    "FunctionPlus",
    "FunctionMinus",
    "FunctionMultiply",
    "FunctionDivide",
    "FunctionIntDiv",
    "FunctionModulo",
    "FunctionNegate",
    "FunctionAbs",
    "FunctionPositive",
    "FunctionGCD",
    "FunctionLCM",
    "FunctionMax2",
    "FunctionMin2",
    "FunctionBitAnd",
    "FunctionBitOr",
    "FunctionBitXor",
    "FunctionBitNot",
    "FunctionBitShiftLeft",
    "FunctionBitShiftRight",
    "FunctionBitRotateLeft",
    "FunctionBitRotateRight",
    "FunctionBitTest",
    "FunctionBitTestAll",
    "FunctionBitTestAny",
    "FunctionBitCount",
    "FunctionBitHammingDistance",
]

#: 真实 ClickHouse 的 ``ILLEGAL_DIVISION``(``src/Common/ErrorCodes.cpp`` = 153)。
#: mini-clickhouse 的错误码表(``errors.py``)只保留了教学与测试真正用到的那几十个,
#: 里面**没有** 153 —— 所以这里直接用数字,并保留真实编号,方便读者对照。
ILLEGAL_DIVISION = 153

#: 真实 ClickHouse 的 ``ILLEGAL_TYPE_OF_ARGUMENT`` = 24,与 ``ErrorCode`` 里的同名常量一致
_ILLEGAL_TYPE = ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT


# ══════════════════════════════════════════════════════════════════════════
# ⓪ 注册入口 —— 一层薄薄的装饰器
# ══════════════════════════════════════════════════════════════════════════


def _register(
    name: str,
    cls: Optional[type] = None,
    *,
    aliases: Sequence[str] = (),
    description: str = "",
) -> Any:
    """本模块统一的注册装饰器(等价于 ``base.register_function``)。

    .. note::

       ``base.register_function`` 作为**装饰器**用时返回的是 ``None``:
       真正干活的是它的内层函数 ``_do``,而 ``_do`` 没有返回值。
       于是::

           @register_function("plus")
           class FunctionPlus(FunctionBase): ...
           # FunctionPlus 现在是 None —— 注册成功了,但类名丢了!

       注册本身不受影响(工厂闭包里已经拿着类对象),但模块级的类名会变成
       ``None``,``__all__``、子类化、以及"用类本身当基类"的写法全都会踩空。
       这里薄薄包一层,让它像正常的装饰器一样**把类原样返回**。
    """
    if cls is not None:
        register_function(name, cls, aliases=aliases, description=description)
        return cls

    def _decorator(klass: type) -> type:
        register_function(name, klass, aliases=aliases, description=description)
        return klass

    return _decorator


# ══════════════════════════════════════════════════════════════════════════
# ① 底层设施:类型 ↔ 存储 的互相转换
# ══════════════════════════════════════════════════════════════════════════


def check_arity(
    name: str,
    args: Sequence[Any],
    n_min: int,
    n_max: Optional[int] = None,
) -> None:
    """校验实参个数,错误消息照抄真实 CH 的措辞。

    ``.. note::`` 真实 ClickHouse 在**分析阶段**就拦住参数个数不对的调用,
    而且消息里会带上"期望几个",这对写脚本的人非常友好 —— 照抄它。
    """
    n = len(args)
    if n < n_min or (n_max is not None and n > n_max):
        if n_max is None:
            expected = f"at least {n_min}"
        elif n_min == n_max:
            expected = str(n_min)
        else:
            expected = f"{n_min} to {n_max}"
        raise_error(
            ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
            f"Number of arguments for function {name} doesn't match: "
            f"passed {n}, expected {expected}",
        )


#: 少数几个"名字 → (位宽, 是否有符号, 是否浮点)"的映射。
#: ``DataTypeNumber`` 自己带这三个属性,但 Date/DateTime/Enum 这些
#: **语域类型**(domain type)没有 —— 它们的存储布局写在 ``DataType`` 之外,
#: 只能在这里补一张表。真实 ClickHouse 用 C++ 模板 ``NativeType`` 表达同一件事。
_DOMAIN_LAYOUT: Dict[str, Tuple[int, bool, bool]] = {
    "Date": (16, False, False),
    "Date32": (32, True, False),
    "Bool": (8, False, False),
}


def decimal_int_bits(precision: int) -> int:
    """``Decimal(P, S)`` 的底层整数位宽(与 ``DataTypeDecimal._int_bits`` 同规则)。"""
    if precision <= 9:
        return 32
    if precision <= 18:
        return 64
    return 128 if precision <= 38 else 256


def storage_layout(type_: DataType) -> Optional[Tuple[int, bool, bool]]:
    """返回 ``(位宽, 是否有符号, 是否浮点)``;不是"数值型存储"则返回 ``None``。

    这一步很关键:``Date`` 的 ``is_numeric()`` 是 **False**(它是一个语域类型),
    但它的列里存的就是 ``UInt16`` 天数 —— 要算 ``date + 1`` 就必须知道这件事。
    真实 ClickHouse 用 ``WhichDataType`` + ``NativeType<T>`` 拿到同样的信息。
    """
    if isinstance(type_, DataTypeNumber):
        return (type_.bits, type_.signed, type_.float)
    if type_.is_decimal():
        return (decimal_int_bits(type_.precision), True, False)
    name = type_.get_name()
    layout = _DOMAIN_LAYOUT.get(name)
    if layout is not None:
        return layout
    if name == "DateTime" or name.startswith("DateTime('"):
        return (32, False, False)
    if name.startswith("DateTime64("):
        return (64, True, False)
    if name.startswith("Enum8("):
        return (8, True, False)
    if name.startswith("Enum16("):
        return (16, True, False)
    if name.startswith("Interval"):
        return (64, True, False)
    return None


def np_dtype_of(type_: DataType) -> Optional[np.dtype]:
    """把类型映射到 NumPy 的存储 dtype;NumPy 装不下的(128/256 位)返回 ``None``。

    返回 ``None`` 的含义是"这条路必须逐行用 Python 大整数算" ——
    对应真实 ClickHouse 里 ``__int128`` / ``wide::integer<256>`` 的特化路径。
    """
    layout = storage_layout(type_)
    if layout is None:
        return None
    bits, signed, is_float = layout
    if bits > 64:
        return None
    if is_float:
        return np.dtype(np.float32 if bits == 32 else np.float64)
    return np.dtype(("i" if signed else "u") + str(bits // 8))


def unwrap_nullable(type_: DataType) -> DataType:
    """``Nullable(T)`` → ``T``,其余类型原样返回。

    为什么所有函数都从这一步开始? 因为 ``get_return_type`` 的契约是
    "只推**非空侧**的类型",NULL 的包装由 ``execute_with_null_propagation``
    统一负责(见 :mod:`minich.functions.base`)。把它写在最前面,
    后面每一行代码就都不用再想 NULL 了。
    """
    return type_.nested if type_.is_nullable() else type_


def wrap_value(type_: DataType, value: Any) -> Any:
    """把一个 Python 数值压进该类型的**存储表示** —— 整数**回绕**,不报错。

    .. code-block:: python

       >>> wrap_value(u8, 250 + 10)
       4
       >>> wrap_value(i8, 127 + 1)
       -128

    这就是 C++ 的整数溢出语义(定义良好,且真实 ClickHouse 依赖它)。
    NumPy 2.x 在 ``arr[i] = 300`` 这种**标量赋值**上会直接抛 ``OverflowError``,
    所以凡是逐行构造定长列的路径,都必须先过这一道。
    """
    if value is None:
        return None
    layout = storage_layout(type_)
    if layout is None:
        return value
    bits, signed, is_float = layout
    if is_float:
        v = float(value)
        # Float32 也要"压"一下,否则 Python 的双精度会悄悄溜进 Float32 列
        return float(np.float32(v)) if bits == 32 else v
    if isinstance(value, float):
        # 浮点 → 整数:向零截断(与 ``DataTypeNumber.normalize`` 一致)
        if math.isnan(value) or math.isinf(value):
            return 0
        value = int(value)
    v = int(value) & ((1 << bits) - 1)
    if signed and v >= (1 << (bits - 1)):
        v -= 1 << bits
    return v


def coerce_value(type_: DataType, value: Any) -> Any:
    """把值转成该类型可接受的形式;``Nullable(T)`` 会下钻到 ``T``。"""
    if value is None:
        return None
    if type_.is_nullable():
        return coerce_value(type_.nested, value)
    return wrap_value(type_, value)


def full_column(arg: ColumnWithTypeAndName) -> IColumn:
    """取实参的**完整列**(常量列展开成等长列)。

    常量列必须展开,否则 ``for i in range(rows): col.get(i)`` 会一直拿到同一个值,
    而结果行数就错了 —— 这是 :mod:`minich.functions.base` 里点名过的坑。
    """
    col = arg.column
    return col.convert_to_full_column() if col.is_const() else col


def numpy_arg(arg: ColumnWithTypeAndName) -> np.ndarray:
    """取实参的 ``np.ndarray`` 视图(常量列先展开)。

    非 :class:`~minich.columns.ColumnVector` 的列(比如 ``ColumnString``)
    会退回"先转 Python 列表再建数组" —— 这条路能跑,但会丢掉向量化的意义,
    所以调用方应当先确认类型是可以向量化的。
    """
    col = full_column(arg)
    if isinstance(col, ColumnVector):
        return col.numpy_view()
    return np.asarray(col.to_list())


def is_integer_like(type_: DataType) -> bool:
    """是否是"整数存储"(含 Date/DateTime/Enum/Bool/Interval)。"""
    layout = storage_layout(type_)
    return layout is not None and not layout[2]


def is_date_time_like(type_: DataType) -> bool:
    """是否是真正的日期/时间类型(Date/Date32/DateTime/DateTime64)。"""
    return type_.is_date() or type_.is_date_time()


def is_interval_type(type_: DataType) -> bool:
    return type_.get_name().startswith("Interval")


# ══════════════════════════════════════════════════════════════════════════
# ② 类型推导:算术的结果类型
# ══════════════════════════════════════════════════════════════════════════

#: "进一档"表 —— ``nextSize`` in ``NumberTraits.h``。
#:
#: 注意 **64 位封顶**:真实 CH 没有在表达式里自动升到 128 位,
#: ``UInt64 + UInt64`` 的结果就是 ``UInt64``(会回绕)。
#: 128/256 位只在**显式类型**(``toUInt128``、Decimal128)里出现,
#: 所以这里只对已经处于 128 位的输入继续进档。
_NEXT_WIDTH = {8: 16, 16: 32, 32: 64, 64: 64, 128: 256, 256: 256}


def _int_type(bits: int, signed: bool) -> DataType:
    return get_data_type(f"{'Int' if signed else 'UInt'}{bits}")


def _date_like_operand(op: str, ta: DataType, tb: DataType) -> Optional[DataType]:
    """``plus``/``minus`` 里"日期类操作数**吃掉**整数"的规则。

    真实 ClickHouse 有一条很好用的例外:算术运算遇到日期/时间类型时,
    **日期那一侧的类型就是结果类型**(而不是把两边提升成一个更大的整数)::

        SELECT toTypeName(now() + 1);          -- DateTime
        SELECT toTypeName(toDate('2024-01-01') + 1);  -- Date

    它成立的前提是"另一侧是整数或 Interval"—— 两个日期相加在真实 CH 里是
    非法的(语义上说不通),我们也不做。

    Enum 与 Interval 也参与这条规则,但优先级低于真正的日期/时间类型:
    ``Date + IntervalDay`` 取 ``Date``(Interval 被"乘上它的单位"后加上去)。
    """
    if op not in ("add", "sub"):
        return None
    for w, o in ((ta, tb), (tb, ta)):
        if is_date_time_like(w) and (is_integer_like(o) or is_interval_type(o)):
            return w
    for w, o in ((ta, tb), (tb, ta)):
        if (w.is_enum() or is_interval_type(w)) and is_integer_like(o) and not is_date_time_like(o):
            return w
    return None


def arithmetic_result_type(op: str, ta: DataType, tb: DataType) -> DataType:
    """**算术结果类型的唯一入口** —— ``op`` 取 ``add/sub/mul/div/intDiv/mod``。

    参数应当已经过 :func:`unwrap_nullable`(NULL 的包装不在这里管)。
    """
    # ① 日期/时间/Enum/Interval 与整数:日期那一侧说了算
    date_like = _date_like_operand(op, ta, tb)
    if date_like is not None:
        return date_like

    # ② Decimal 参与
    if ta.is_decimal() or tb.is_decimal():
        return _decimal_result_type(op, ta, tb)

    # ③ 除法:整数对也返回浮点(divide 与 intDiv 的分工)
    if op == "div":
        if ta.is_float() or tb.is_float():
            return ta if ta.is_float() and tb.is_float() and ta.get_name() == tb.get_name() \
                else get_data_type("Float64")
        return get_data_type("Float64")

    # ④ intDiv / modulo:纯整数运算,位宽取较宽者但**不进档**
    if op in ("intDiv", "mod"):
        for t in (ta, tb):
            if not is_integer_like(t):
                raise_error(
                    _ILLEGAL_TYPE,
                    f"Illegal types of arguments for "
                    f"{'intDiv' if op == 'intDiv' else 'modulo'}: "
                    f"{ta.get_name()}, {tb.get_name()}",
                )
        return _wider_integer(ta, tb)

    # ⑤ add / sub / mul
    if not ta.is_numeric() or not tb.is_numeric():
        raise_error(
            _ILLEGAL_TYPE,
            f"Illegal types of arguments for arithmetic function: "
            f"{ta.get_name()}, {tb.get_name()}",
        )
    if ta.is_float() or tb.is_float():
        return get_wider_numeric_type(ta, tb)
    return _integer_arithmetic_type(op, ta, tb)


def _integer_arithmetic_type(op: str, ta: DataType, tb: DataType) -> DataType:
    """两个整数之间的 ``+`` / ``-`` / ``*`` 类型(真实的 NumberTraits 规则)。"""
    # 有符号与无符号混用:真实 CH 取较宽的位宽;mini 把位宽翻倍(deviation ①)
    if ta.signed != tb.signed:
        return get_wider_numeric_type(ta, tb)
    widest = ta if ta.bits >= tb.bits else tb
    bits = widest.bits
    # minus 的结果**永远有符号**(ResultOfSubtraction):
    # UInt8 - UInt8 = Int16,否则 0 - 1 就没法表示了
    signed = widest.signed or op == "sub"
    return _int_type(_NEXT_WIDTH[bits], signed)


def _wider_integer(ta: DataType, tb: DataType) -> DataType:
    """整数取宽(intDiv / modulo 用)—— 同号时进位宽取大,异号时补一位符号。"""
    la = storage_layout(ta)
    lb = storage_layout(tb)
    assert la is not None and lb is not None
    bits = max(la[0], lb[0])
    if la[1] == lb[1]:
        return _int_type(bits, la[1])
    return _int_type(min(bits * 2, 256), True)


#: ``op`` → 真实 ClickHouse 里对应的算术函数名 —— 错误消息要带上它。
_DECIMAL_OP_FUNC_NAME = {
    "add": "plus",
    "sub": "minus",
    "mul": "multiply",
    "div": "divide",
    "intDiv": "intDiv",
    "mod": "modulo",
}


def _decimal_result_type(op: str, ta: DataType, tb: DataType) -> DataType:
    """Decimal 参与时的结果类型。

    ===========  ===========================================  ==========================
    运算          规则(真实 CH)                                 mini-clickhouse
    ===========  ===========================================  ==========================
    ``+``/``-``  scale 取大,precision 加 1                     precision 取**两者之和**
    ``*``        scale 相加,precision 相加                     **与真实 CH 一致**
    ``/``        scale 取**被除数**的,precision 不变            **与真实 CH 一致**
    ``intDiv``   真实 CH 支持(返回整数)                        **不支持**,抛 ILLEGAL_TYPE
    ===========  ===========================================  ==========================

    加法那个偏离是刻意的:mini-clickhouse 借用 ``types/__init__.py`` 里
    ``get_wider_numeric_type`` 的既有规则(``precision_a + precision_b``),
    宁可精度浪费一点,也不要出现"算着算着精度不够"的静默错误。

    ``Decimal`` 与**浮点**混算在真实 ClickHouse 里是**非法**的(两者没有
    公共类型):``toDecimal32(1.23, 2) + 0.5`` 会抛 ``ILLEGAL_TYPE_OF_ARGUMENT``
    ("Illegal types Decimal(9, 2) and Float64 of arguments of function plus")。
    这里照做 —— 早先把浮点当整数隐式提升,``int()`` 一截断 ``0.5`` 就变成了 0
    (除法更会直接撞上 "Division by zero")。
    """
    if (ta.is_decimal() and tb.is_float()) or (tb.is_decimal() and ta.is_float()):
        raise_error(
            _ILLEGAL_TYPE,
            f"Illegal types {ta.get_name()} and {tb.get_name()} of arguments "
            f"of function {_DECIMAL_OP_FUNC_NAME.get(op, op)}",
        )
    if op in ("intDiv", "mod"):
        raise_error(
            _ILLEGAL_TYPE,
            f"Illegal types of arguments for function "
            f"{'intDiv' if op == 'intDiv' else 'modulo'}: "
            f"{ta.get_name()}, {tb.get_name()} "
            f"(mini-clickhouse 不支持 Decimal 上的整除/取模)",
        )
    if op == "div":
        # 结果的 scale = **被除数**的 scale:1.00 / 3.00 = 0.33(不是 0.3333)
        if ta.is_decimal():
            return DataTypeDecimal(ta.precision, ta.scale)
        return DataTypeDecimal(tb.precision, tb.scale)
    if op == "mul":
        if ta.is_decimal() and tb.is_decimal():
            precision = min(ta.precision + tb.precision, 76)
            scale = min(ta.scale + tb.scale, precision)
            return DataTypeDecimal(precision, scale)
        # Decimal × 整数:整数被隐式提升成 Decimal,结果保持原类型
        return ta if ta.is_decimal() else tb
    return get_wider_numeric_type(ta, tb)


def common_supertype(ta: DataType, tb: DataType) -> DataType:
    """两个类型的**公共类型** —— 分支/合并类函数(``if``/``greatest``/``coalesce``)用。

    规则(与真实 CH 的 ``getLeastSupertype`` 大方向一致):

    * 两边相同 → 原样;
    * 两边都是数值 → :func:`~minich.types.get_wider_numeric_type`;
    * 两边都是字符串(``String``/``FixedString``/``Enum`` 的文本面)→ ``String``;
    * 一边是 ``Nothing``(即 "NULL 字面量")→ 另一边(调用方负责包 ``Nullable``);
    * 其余组合 → 抛 ``ILLEGAL_TYPE_OF_ARGUMENT``,真实 CH 的对应错误是
      ``NO_COMMON_TYPE``(386),同样不在 mini 的错误码表里。
    """
    if ta.equals(tb):
        return ta
    if ta.is_nothing():
        return tb
    if tb.is_nothing():
        return ta
    if ta.is_numeric() and tb.is_numeric():
        return get_wider_numeric_type(ta, tb)
    if ta.is_string() and tb.is_string():
        return get_data_type("String")
    if ta.is_string() and tb.is_enum():
        return ta
    if tb.is_string() and ta.is_enum():
        return tb
    raise_error(
        _ILLEGAL_TYPE,
        f"There is no common type for {ta.get_name()} and {tb.get_name()}",
    )


# ══════════════════════════════════════════════════════════════════════════
# ③ 逐行计算:一套标量语义,向量化与回退路径共用
# ══════════════════════════════════════════════════════════════════════════


def _trunc_div(a: int, b: int) -> int:
    """整数除法 —— **向零截断**(C++ 的 ``/``),不是 Python 的地板除。"""
    q = abs(a) // abs(b)
    return -q if (a < 0) != (b < 0) else q


def _trunc_mod(a: int, b: int) -> int:
    """整数取余 —— **向零截断**(C++ 的 ``%``):``-7 % 3 = -1``(Python 是 ``2``)。"""
    return a - b * _trunc_div(a, b)


def _decimal_divide(a: int, b: int, exponent: int) -> int:
    """Decimal 除法:``a``/``b`` 是未缩放整数,``exponent`` 见调用处。

    用 ``decimal`` 模块做一次高精度计算再四舍五入 —— 因为除法**一定会**产生
    无限小数(``1/3``),必须显式规定精度与舍入方向。这里用 ``ROUND_HALF_UP``,
    与 :meth:`minich.types.composite.DataTypeDecimal.normalize` 保持一致。
    """
    if b == 0:
        raise_error(ILLEGAL_DIVISION, "Division by zero")
    with localcontext() as ctx:
        ctx.prec = 80
        value = (_PyDecimal(a) / _PyDecimal(b)) * (_PyDecimal(10) ** exponent)
        return int(value.to_integral_value(rounding=ROUND_HALF_UP))


def _compute_decimal(
    op: str,
    a: Any,
    b: Any,
    ta: DataType,
    tb: DataType,
) -> Any:
    """Decimal 运算 —— **入参与结果都是未缩放整数**。

    列里存的是整数,小数点位置由 ``DataType`` 记着(见
    :class:`minich.columns.base.ColumnBigInt` 的 docstring)。
    所以所有 Decimal 运算都是"先把两边对齐到同一个 scale,再做整数运算":

    .. code-block:: text

       Decimal(9,2) 1.23 + Decimal(9,3) 4.567
         a = 123, b = 4567
         S = max(2, 3) = 3
         → 123 * 10^(3-2) + 4567 = 5797  →  5.797
    """
    sa = ta.scale if ta.is_decimal() else 0
    sb = tb.scale if tb.is_decimal() else 0
    if op == "add":
        s = max(sa, sb)
        return int(a) * 10 ** (s - sa) + int(b) * 10 ** (s - sb)
    if op == "sub":
        s = max(sa, sb)
        return int(a) * 10 ** (s - sa) - int(b) * 10 ** (s - sb)
    if op == "mul":
        return int(a) * int(b)
    if op == "div":
        # 结果的 scale = 被除数的 scale(mini 与真实 CH 一致)
        s = sa if ta.is_decimal() else sb
        return _decimal_divide(int(a), int(b), sb + s - sa)
    raise_error(_ILLEGAL_TYPE, f"Unsupported decimal operation {op}")


def _compute_int(
    op: str,
    a: Any,
    b: Any,
    ta: DataType,
    tb: DataType,
    zero_safe: bool,
) -> Any:
    if op == "add":
        return a + b
    if op == "sub":
        return a - b
    if op == "mul":
        return a * b
    if op == "div":
        # 真实 CH:整数相除也返回浮点,0 作除数得到 inf/nan(不报错)
        if b == 0:
            if a == 0:
                return float("nan")
            return math.copysign(float("inf"), a)
        return a / b
    if op in ("intDiv", "mod"):
        if b == 0:
            if zero_safe:
                return 0
            raise_error(
                ILLEGAL_DIVISION,
                f"Division by zero in function {op}",
            )
        return _trunc_div(a, b) if op == "intDiv" else _trunc_mod(a, b)
    raise_error(ErrorCode.LOGICAL_ERROR, f"Unknown arithmetic op {op}")


def interval_multiplier(date_type: DataType, interval_type: DataType) -> int:
    """``date + INTERVAL n UNIT`` 里那个 n 要**乘多少**才能加到日期存储上。

    三种存储粒度决定了三个换算系数:

    .. code-block:: text

       类型           列里存的是         INTERVAL 1 DAY 的系数
       ────────────  ────────────────   ────────────────────
       Date/Date32   **天数**(UInt16)   1
       DateTime      **秒**(UInt32)     86400
       DateTime64(p) **刻度**(Int64)    86400 * 10^p
       Interval      Int64 计数          1(Interval + Interval 就是纯整数加)
    """
    if is_interval_type(date_type):
        return 1
    unit = _interval_seconds(interval_type)
    if date_type.is_date():
        if unit % 86400 != 0:
            raise_error(
                ErrorCode.NOT_IMPLEMENTED_IN_MINICH,
                f"mini-clickhouse cannot add {interval_type.get_name()} to "
                f"{date_type.get_name()}: the unit is finer than a day",
            )
        return unit // 86400
    name = date_type.get_name()
    if name.startswith("DateTime64("):
        return unit * (10 ** date_type.precision)  # type: ignore[attr-defined]
    return unit


def _interval_seconds(interval_type: DataType) -> int:
    """Interval 类型的一个单位等于多少秒(日历单位抛 NOT_IMPLEMENTED)。"""
    from ..types.scalars import INTERVAL_KINDS

    kind = getattr(interval_type, "kind", None)
    if kind is None:
        raise_error(_ILLEGAL_TYPE, f"{interval_type.get_name()} is not an interval")
    seconds = INTERVAL_KINDS[kind][1]
    if seconds is None:
        raise_error(
            ErrorCode.NOT_IMPLEMENTED_IN_MINICH,
            f"Calendar interval {interval_type.get_name()} requires calendar "
            f"arithmetic, which mini-clickhouse does not implement; "
            f"use addMonths/addYears instead",
        )
    if seconds < 1:
        raise_error(
            ErrorCode.NOT_IMPLEMENTED_IN_MINICH,
            f"Sub-second interval {interval_type.get_name()} is not supported "
            f"in mini-clickhouse in this position",
        )
    return int(seconds)


def compute_arithmetic_scalar(
    op: str,
    a: Any,
    b: Any,
    ta: DataType,
    tb: DataType,
    zero_safe: bool = False,
) -> Any:
    """一次标量算术 —— 向量化路径与逐行回退路径**共用**的语义。

    把语义收敛到一个函数里,是为了让"向量化"和"逐行"两条路**不可能算出不同的结果**。
    真实 ClickHouse 也面对同样的问题(``executeImpl`` 的模板特化 vs
    ``executeImplDryRun``),它的做法是把语义写在 ``OperationTraits`` 里,
    两条路都调用它。
    """
    date_like = _date_like_operand(op, ta, tb)
    if date_like is not None:
        date_is_left = ta.equals(date_like)
        other = tb if date_is_left else ta
        if op == "sub" and not date_is_left:
            raise_error(
                _ILLEGAL_TYPE,
                f"Cannot subtract {date_like.get_name()} from {other.get_name()}",
            )
        multiplier = interval_multiplier(date_like, other) if is_interval_type(other) else 1
        x = a if date_is_left else b
        y = b if date_is_left else a
        return x + y * multiplier if op == "add" else x - y * multiplier
    if ta.is_decimal() or tb.is_decimal():
        return _compute_decimal(op, a, b, ta, tb)
    return _compute_int(op, a, b, ta, tb, zero_safe)


# ══════════════════════════════════════════════════════════════════════════
# ④ 二元算术函数
# ══════════════════════════════════════════════════════════════════════════


class _BinaryArithmetic(FunctionBase):
    """二元算术的**公共骨架**:类型推导 + 向量化 + 逐行回退。

    ``execute`` 会依次尝试三条路,顺序不能变:

    .. code-block:: text

       ① 有 Nullable 实参?   → 交给 execute_with_null_propagation(基类的统一机制)
       ② 能向量化?          → NumPy 整列一次算完(算完**回绕**到声明类型)
       ③ 否则               → 逐行用 Python 大整数算(128/256 位、Decimal 走这条)

    第 ③ 条不是"兜底凑合":``Int128`` / ``Decimal128`` 在 NumPy 里根本没有对应
    dtype,真实 ClickHouse 也是给它们单独实例化一套模板代码。两条路都调用
    :func:`compute_arithmetic_scalar`,所以结果一定一致。
    """

    #: ``add`` / ``sub`` / ``mul`` / ``div`` / ``intDiv`` / ``mod``
    op: str = "add"

    #: 除数为 0 时返回 0 而不是抛错(``intDivOrZero`` / ``moduloOrZero``)
    zero_safe: bool = False

    #: ``get_return_type`` 顺手缓存下实参类型 —— ``execute_row`` 需要它,
    #: 而真实 CH 的 ``executeRow`` 同样拿得到 ``Block`` 的元信息。
    _arg_types: Optional[Tuple[DataType, DataType]] = None

    # ── 类型 ────────────────────────────────────────────────────────────

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 2, 2)
        ta, tb = unwrap_nullable(args[0]), unwrap_nullable(args[1])
        self._arg_types = (ta, tb)
        return arithmetic_result_type(self.op, ta, tb)

    # ── 执行 ────────────────────────────────────────────────────────────

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 2, 2)
        if any(a.type.is_nullable() for a in args):
            col, _ = execute_with_null_propagation(self, args, rows, context)
            return col
        ta, tb = args[0].type, args[1].type
        ret = self.get_return_type([ta, tb])
        if (ta.is_decimal() or tb.is_decimal()) or not self._can_vectorize(ta, tb, ret):
            return self._execute_rowwise(args, rows, ret, ta, tb)
        return self._execute_vectorized(args, rows, ret, ta, tb)

    def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
        """单行实现 —— 语义与向量化路径完全一致(都走 :func:`compute_arithmetic_scalar`)。"""
        if self._arg_types is None:
            raise_error(
                ErrorCode.LOGICAL_ERROR,
                f"{self.name}: get_return_type() must be called before execute_row()",
            )
        ta, tb = self._arg_types
        ret = arithmetic_result_type(self.op, ta, tb)
        return wrap_value(
            ret,
            compute_arithmetic_scalar(self.op, values[0], values[1], ta, tb, self.zero_safe),
        )

    def _execute_rowwise(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        ret: DataType,
        ta: DataType,
        tb: DataType,
    ) -> IColumn:
        a = full_column(args[0])
        b = full_column(args[1])
        out = ret.create_column()
        for i in range(rows):
            raw = compute_arithmetic_scalar(self.op, a.get(i), b.get(i), ta, tb, self.zero_safe)
            out.append(wrap_value(ret, raw))
        return out

    def _can_vectorize(self, ta: DataType, tb: DataType, ret: DataType) -> bool:
        """能不能交给 NumPy?

        四个否决条件,每一个都对应一类**静默算错**的风险:

        * 结果或实参是 128/256 位 —— NumPy 没有这种 dtype;
        * 浮点与"有可能超过 2^53"的整数混合 —— NumPy 会把两边都转成 float64,
          大整数会丢精度(``9007199254740993 + 0.0`` 就不再是它自己);
        * 两个 64 位且符号不同 —— NumPy 同样只能提升成 float64;
        * **日期/Interval 参与** —— 这一条最隐蔽:``DateTime + IntervalDay``
          不是"加 1",而是"加 1 天(= 86400 秒)",NumPy 不知道这件事,
          照直加就会得到一个**静默错一天**的结果。所以这类运算一律走逐行,
          让 :func:`compute_arithmetic_scalar` 去乘那个换算系数。
        """
        if np_dtype_of(ret) is None:
            return False
        if _date_like_operand(self.op, ta, tb) is not None:
            return False
        da, db = np_dtype_of(ta), np_dtype_of(tb)
        if da is None or db is None:
            return False
        if (da.kind == "f") != (db.kind == "f"):
            narrower_int = db if da.kind == "f" else da
            if narrower_int.itemsize > 4:
                return False
        if da.kind != db.kind and da.itemsize == 8 and db.itemsize == 8:
            return False
        return True

    @staticmethod
    def _compute_dtype(da: np.dtype, db: np.dtype, ret_dtype: np.dtype) -> np.dtype:
        """向量化时的**中间计算 dtype** —— 这一段写错就会静默算错值。

        规则:在"能装下**两个操作数**,也能装下**结果**"的类型里算,
        最后再 ``astype`` 到结果类型(= C++ 的末尾回绕)。三个例子::

           UInt8  +  UInt8  → UInt16   必须在 uint16 里算,否则 250+10 在 uint8 里回绕成 4
           UInt8  -  UInt8  → Int16    必须有符号,否则 0-1 在 uint8 里变成 255
           Int64  +  Int64  → Int64    结果类型就是 64 位,那就让它按 C++ 回绕
           Int32  +  Int32  → Int64    (mini 的"进一档")在 int64 里算,结果精确

        无符号 → 有符号(且不更窄)的转换是**无损**的,所以"结果的符号不同但更宽"
        也算安全;反过来(有符号 → 无符号)**不行** —— 那会丢掉符号。
        """
        if ret_dtype.kind == "f":
            return ret_dtype
        if da.kind != db.kind:
            # 有符号/无符号混用:8 字节的已被 _can_vectorize 排除,这里升到 int64 是安全的
            return np.dtype(np.int64)
        if ret_dtype.itemsize >= da.itemsize and (
            ret_dtype.kind == da.kind or (da.kind == "u" and ret_dtype.kind == "i")
        ):
            return ret_dtype
        return da if da.itemsize >= db.itemsize else db

    def _execute_vectorized(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        ret: DataType,
        ta: DataType,
        tb: DataType,
    ) -> IColumn:
        ret_dtype = np_dtype_of(ret)
        assert ret_dtype is not None
        dtype = self._compute_dtype(np_dtype_of(ta), np_dtype_of(tb), ret_dtype)  # type: ignore[arg-type]
        a = numpy_arg(args[0]).astype(dtype, copy=False)
        b = numpy_arg(args[1]).astype(dtype, copy=False)
        res = self._vector_op(a, b, ret_dtype)
        return ColumnVector(ret_dtype, np.asarray(res, dtype=ret_dtype))

    def _vector_op(self, a: np.ndarray, b: np.ndarray, ret_dtype: np.dtype) -> np.ndarray:
        if self.op == "add":
            return a + b
        if self.op == "sub":
            return a - b
        if self.op == "mul":
            return a * b
        if self.op == "div":
            ftype = np.float32 if ret_dtype.itemsize == 4 else np.float64
            with np.errstate(divide="ignore", invalid="ignore"):
                return a.astype(ftype) / b.astype(ftype)
        # intDiv / modulo:先处理除零,再按**向零截断**修正 NumPy 的地板语义
        zero = b == 0
        if bool(zero.any()) and not self.zero_safe:
            first = int(np.argmax(zero))
            raise_error(
                ILLEGAL_DIVISION,
                f"Division by zero in function {self.name} at row {first}",
            )
        safe = np.where(zero, 1, b)
        if self.op == "intDiv":
            q = a // safe
            rem = a - q * safe
            adjust = (rem != 0) & ((a < 0) != (safe < 0))
            res = np.where(adjust, q + 1, q)
        else:
            r = a % safe
            adjust = (r != 0) & ((a < 0) != (safe < 0))
            res = np.where(adjust, r - safe, r)
        if bool(zero.any()):
            res = np.where(zero, 0, res)
        return res


@_register("plus", aliases=["add"], description="加法(结果类型规则见 NumberTraits)")
class FunctionPlus(_BinaryArithmetic):
    """``plus(a, b)`` —— 加法。

    .. code-block:: sql

       SELECT plus(1, 2), 1 + 2, plus(toUInt8(250), toUInt8(10));
       -- 3   3   260

    第三列是重点:``toUInt8(250) + toUInt8(10)`` **不会**回绕成 4,
    因为 ``UInt8 + UInt8`` 的结果类型是 ``UInt16``,"进一档"把 260 装下了。
    真正会回绕的是 64 位封顶之后的情形::

       plus(toUInt64(18446744073709551615), toUInt64(1))  = 0   ← 回绕成 0
       plus(toInt64(-9223372036854775808), toInt64(-1))   = 9223372036854775807

    结果类型规则(完整表见模块 docstring):

    .. code-block:: text

       UInt8  + UInt8   = UInt16        Int8  + UInt8  = Int16
       UInt8  + UInt16  = UInt32        Int64 + UInt64 = Int128   ← mini 的偏离
       Float32 + UInt64 = Float64       Date  + 1      = Date
    """

    op = "add"


@_register("minus", aliases=["subtract"], description="减法(结果永远有符号)")
class FunctionMinus(_BinaryArithmetic):
    """``minus(a, b)`` —— 减法。**结果类型永远有符号**,这是刻意的。

    .. code-block:: text

       toTypeName(0)       = UInt8
       toTypeName(0 - 0)   = Int16      ← 不是 UInt16!
       toTypeName(0 - 0 - 0)= Int32

    为什么? 因为 ``UInt8 - UInt8`` 的结果可以是负数,如果沿用"同号"规则就会
    得到一个无符号的 Int16(即 UInt16),``0 - 1`` 会变成 65535 ——
    真实 ClickHouse 用 ``ResultOfSubtraction`` 这条 trait 强制加符号位。

    日期减整数(``date - 7``)与 :class:`FunctionPlus` 的规则对称;
    ``7 - date`` 是非法的(会抛错),因为语义上讲不通。
    """

    op = "sub"


@_register("multiply", description="乘法(位宽进一档以避免平凡溢出)")
class FunctionMultiply(_BinaryArithmetic):
    """``multiply(a, b)`` —— 乘法。

    .. code-block:: text

       UInt8  * UInt8  = UInt16    ← 8 位乘 8 位最多 16 位,这是"进一档"的经典动机
       UInt16 * UInt16 = UInt32
       UInt64 * UInt64 = UInt64    ← 64 位封顶,这一档之外仍然会回绕
       Decimal(9,2) * Decimal(9,2) = Decimal(18,4)   ← scale **相加**

    最后一条是最容易踩的:``1.23 * 4.56 = 5.6088`` 需要 4 位小数,
    所以 Decimal 乘法会**放大 scale**,再乘一次就又是 4 位。
    """

    op = "mul"


@_register("divide", description="除法(整数相除也返回浮点)")
class FunctionDivide(_BinaryArithmetic):
    """``divide(a, b)`` —— 除法。**整数相除返回浮点**,这是本模块最常被问的一点。

    .. code-block:: sql

       SELECT divide(1, 2), intDiv(1, 2), divide(1, 0);
       -- 0.5   0   inf

    * ``divide`` 是"数学除法":``divide(1, 2) = 0.5``,结果类型 ``Float64``;
    * ``intDiv`` 是"整除":``intDiv(1, 2) = 0``,结果类型仍是整数;
    * 除数为 0 **不报错**:``Float64`` 下得到 ``inf`` / ``nan``,这是 IEEE 语义,
      真实 ClickHouse 也是这样(想报错请用 ``intDiv``)。
    * ``Decimal`` 参与时结果仍是 ``Decimal``,而且 **scale 取被除数的**:
      ``divide(toDecimal64(1, 2), toDecimal64(3, 2))`` = ``0.33``(不是 0.3333)。

    .. warning:: mini-clickhouse 的 ``Float32 / Float32`` 也会先转成 ``Float32``
       结果,与真实 CH 一致;但中间不做 ``Float32`` 精度的逐行截断,
       所以极端情况下最后一位可能有差异。
    """

    op = "div"


@_register("intDiv", description="整除(向零截断,除零报错)")
class FunctionIntDiv(_BinaryArithmetic):
    """``intDiv(a, b)`` —— 整除,**向零截断**(不是向下取整)。

    .. code-block:: text

       intDiv(7, 2)    = 3        intDiv(-7, 2)  = -3
       floor(-7 / 2) 会得到 -4 —— 这两者不是一回事,是 C++ 与 Python 的经典差异
       intDiv(1, 0)    → Code: 153. DB::Exception: Division by zero (ILLEGAL_DIVISION)

    结果类型:位宽取两侧较宽者但**不进档**(``Int64 / UInt64 = Int128``,
    真实 CH 是 ``Int64``;见 :func:`_wider_integer`)。
    """

    op = "intDiv"


@_register("modulo", aliases=["mod"], description="取模(向零取余,除零报错)")
class FunctionModulo(_BinaryArithmetic):
    """``modulo(a, b)`` —— 取余,**余数符号跟着被除数**(C++ 的 ``%``)。

    .. code-block:: text

       modulo(7, 3)  =  1        modulo(-7, 3) = -1
       Python 的 -7 % 3 会得到 2  —— 这不是 ClickHouse 的语义
       modulo(-7, -3) = -1       modulo(7, -3) =  1

    想要"余数永远非负"要用 ``positiveModulo``(mini-clickhouse 未实现,
    可自行写成 ``modulo(modulo(a, b) + b, b)``)。
    """

    op = "mod"


@_register("intDivOrZero", description="整除,除数为 0 时返回 0")
class FunctionIntDivOrZero(FunctionIntDiv):
    """``intDivOrZero(a, b)`` —— ``intDiv`` 的"除零给 0"版本。

    .. code-block:: sql

       SELECT intDiv(1, 0);        -- Code: 153. ... Division by zero (ILLEGAL_DIVISION)
       SELECT intDivOrZero(1, 0);  -- 0

    为什么要有这个变体? 因为**除零在真实数据里太常见了** —— 分母往往来自
    另一列的聚合,某个分组恰好算出了 0。``OrZero`` 让你"先把结果算出来,
    再回头看有多少个 0",而不是让整条查询挂掉。ClickHouse 里
    ``OrZero``/``OrNull`` 家族如此庞大,原因就在这里。
    """

    zero_safe = True


@_register("moduloOrZero", description="取模,除数为 0 时返回 0")
class FunctionModuloOrZero(FunctionModulo):
    """``moduloOrZero(a, b)`` —— ``modulo`` 的"除零给 0"版本。

    语义与 :class:`FunctionIntDivOrZero` 完全相同,只是作用的运算不同。
    """

    zero_safe = True


# ══════════════════════════════════════════════════════════════════════════
# ⑤ 一元算术:negate / abs / positive
# ══════════════════════════════════════════════════════════════════════════


class _UnaryArithmetic(FunctionBase):
    """一元算术的公共骨架(``negate`` / ``abs`` / ``positive``)。

    和二元那套一样的三条路:NULL 传播 → 向量化 → 逐行回退。
    """

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 1, 1)
        if args[0].type.is_nullable():
            col, _ = execute_with_null_propagation(self, args, rows, context)
            return col
        ta = args[0].type
        ret = self.get_return_type([ta])
        if ta.is_decimal() or np_dtype_of(ta) is None or np_dtype_of(ret) is None:
            col = full_column(args[0])
            out = ret.create_column()
            for i in range(rows):
                out.append(wrap_value(ret, self.compute_scalar(col.get(i), ta)))
            return out
        ret_dtype = np_dtype_of(ret)
        assert ret_dtype is not None
        # 注意:实参数组**按原 dtype** 交给 vector_op —— 取负/取绝对值都需要
        # 先看到"原来的符号",再由 np.asarray 统一**回绕**到结果类型。
        return ColumnVector(
            ret_dtype, np.asarray(self.vector_op(numpy_arg(args[0])), dtype=ret_dtype)
        )

    def vector_op(self, arr: np.ndarray) -> np.ndarray:
        raise_error(ErrorCode.NOT_IMPLEMENTED, f"{self.name}: vector_op is not implemented")

    def compute_scalar(self, value: Any, ta: DataType) -> Any:
        raise_error(ErrorCode.NOT_IMPLEMENTED, f"{self.name}: compute_scalar is not implemented")


@_register("negate", description="取负(结果类型的位宽进一档且有符号)")
class FunctionNegate(_UnaryArithmetic):
    """``negate(x)`` —— 取负;也可写成 ``-x``。

    .. code-block:: text

       negate(UInt8)  = Int16     ← 无符号取负必须加符号位,否则 -1 变成 255
       negate(Int8)   = Int16     ← 再进一档,-(-128) 才表示得出来
       negate(Int64)  = Int64
       negate(UInt64) = Int64     ← 64 位封顶,负的 UInt64 会回绕
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 1)
        ta = unwrap_nullable(args[0])
        if ta.is_decimal():
            return ta
        if ta.is_float():
            return ta
        if not is_integer_like(ta):
            raise_error(
                _ILLEGAL_TYPE,
                f"Illegal type of argument for function {self.name}: {ta.get_name()}",
            )
        bits, _, _ = storage_layout(ta)
        return _int_type(_NEXT_WIDTH[bits], True)

    def vector_op(self, arr: np.ndarray) -> np.ndarray:
        # 整数要在**有符号的宽类型**里取负,否则 -UInt8(1) 会在 uint8 里
        # 变成 255(那是回绕,不是负一)
        if arr.dtype.kind in "iu":
            with np.errstate(over="ignore"):
                return -arr.astype(np.int64)
        return -arr

    def compute_scalar(self, value: Any, ta: DataType) -> Any:
        return -value


@_register("abs", description="绝对值(有符号整数变成同宽的无符号)")
class FunctionAbs(_UnaryArithmetic):
    """``abs(x)`` —— 绝对值。

    .. code-block:: text

       abs(Int8 -1)  = 1   类型是 **UInt8**(有符号 → 同宽无符号)
       abs(UInt8 3)  = 3   类型不变
       abs(Float64)  = Float64,abs(Decimal(9,2)) = Decimal(9,2)

    为什么有符号整数要变成**无符号**? 因为 ``abs(Int8(-128))`` = 128 装不进 Int8。
    代价是 ``abs(Int8(-128))`` 会**回绕**成 -128(真实 CH 同样如此,
    这是补码世界里的不可救药之处)。

    别名:``ABS`` 大写也能解析到同一个函数(函数名大小写不敏感)。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 1)
        ta = unwrap_nullable(args[0])
        if ta.is_decimal() or ta.is_float():
            return ta
        if not is_integer_like(ta):
            raise_error(
                _ILLEGAL_TYPE,
                f"Illegal type of argument for function {self.name}: {ta.get_name()}",
            )
        bits, signed, _ = storage_layout(ta)
        return _int_type(bits, False) if signed else ta

    def vector_op(self, arr: np.ndarray) -> np.ndarray:
        # 在 int64 里取绝对值:abs(Int8(-128)) = 128 装不回 Int8,
        # 但结果类型是 UInt8,所以"先算宽、再回绕"正好给出正确答案
        if arr.dtype.kind == "u":
            return arr
        return np.abs(arr.astype(np.int64))

    def compute_scalar(self, value: Any, ta: DataType) -> Any:
        return -value if value < 0 else value


@_register("positive", description="返回参数本身(结果永远无符号)")
class FunctionPositive(_UnaryArithmetic):
    """``positive(x)`` —— 返回 ``x`` 本身,但结果类型**永远无符号**。

    .. code-block:: text

       positive(Int8 5)   = 5   类型 UInt8
       positive(Int8 -1)  = 255 类型 UInt8   ← 值被"重新解释"了!

    它存在的意义是"我不做运算,但我要求结果是可当无符号用的":
    典型用法是配合 ``bitAnd`` / ``bitOr`` 做位掩码,或配合
    ``arrayJoin`` 把索引规范化。真实 ClickHouse 里有这个函数
    (docs: "positive(x) — Returns x, the result is always unsigned")。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 1)
        ta = unwrap_nullable(args[0])
        if ta.is_float() or ta.is_decimal():
            return ta
        if not is_integer_like(ta):
            raise_error(
                _ILLEGAL_TYPE,
                f"Illegal type of argument for function {self.name}: {ta.get_name()}",
            )
        bits, signed, _ = storage_layout(ta)
        return _int_type(bits, False) if signed else ta

    def vector_op(self, arr: np.ndarray) -> np.ndarray:
        return arr

    def compute_scalar(self, value: Any, ta: DataType) -> Any:
        return value


# ══════════════════════════════════════════════════════════════════════════
# ⑥ gcd / lcm / max2 / min2
# ══════════════════════════════════════════════════════════════════════════


class _IntegerPairFunction(FunctionBase):
    """整数二元函数(``gcd`` / ``lcm``)的公共骨架 —— 逐行实现,向量化收益很低。"""

    name = "gcd"

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 2, 2)
        ta, tb = unwrap_nullable(args[0]), unwrap_nullable(args[1])
        for t in (ta, tb):
            if not is_integer_like(t):
                raise_error(
                    _ILLEGAL_TYPE,
                    f"Illegal types of arguments for function {self.name}: "
                    f"{ta.get_name()}, {tb.get_name()}",
                )
        self._arg_types = (ta, tb)
        # 与 plus 用同一条"进一档"规则:lcm 的中间结果可能超出两个操作数
        return _integer_arithmetic_type("add", ta, tb)

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 2, 2)
        if any(a.type.is_nullable() for a in args):
            col, _ = execute_with_null_propagation(self, args, rows, context)
            return col
        ret = self.get_return_type([args[0].type, args[1].type])
        a = full_column(args[0])
        b = full_column(args[1])
        out = ret.create_column()
        for i in range(rows):
            out.append(wrap_value(ret, self.compute(a.get(i), b.get(i))))
        return out

    def compute(self, a: Any, b: Any) -> Any:
        raise_error(ErrorCode.NOT_IMPLEMENTED, f"{self.name}: compute is not implemented")


@_register("gcd", description="最大公约数")
class FunctionGCD(_IntegerPairFunction):
    """``gcd(a, b)`` —— 最大公约数(greatest common divisor)。

    .. code-block:: sql

       SELECT gcd(12, 18), gcd(0, 5), gcd(-12, 18);
       -- 6   5   6      ← 符号不影响结果(公约数按绝对值算)

    结果类型与 ``plus`` 一致(进一档):``gcd(UInt8, UInt8) = UInt16``。
    真实 CH 用的是同一族 ``NumberTraits`` 规则。
    """

    name = "gcd"

    def compute(self, a: Any, b: Any) -> Any:
        return math.gcd(int(a), int(b))


@_register("lcm", description="最小公倍数")
class FunctionLCM(_IntegerPairFunction):
    """``lcm(a, b)`` —— 最小公倍数(least common multiple)。

    .. code-block:: sql

       SELECT lcm(4, 6), lcm(0, 5);
       -- 12   0        ← 任何数与 0 的最小公倍数是 0

    .. warning:: **lcm 比 gcd 容易溢出**。``lcm(UInt8, UInt8)`` 的最大值是
       ``253 * 254 = 64262``,刚好装进 UInt16(进一档的意义就在这);
       但 ``lcm(Int64, Int64)`` 在 64 位封顶下会**回绕**,真实 CH 也一样。
    """

    name = "lcm"

    def compute(self, a: Any, b: Any) -> Any:
        x, y = abs(int(a)), abs(int(b))
        if x == 0 or y == 0:
            return 0
        return x // math.gcd(x, y) * y


class _MinMax2(FunctionBase):
    """``max2`` / ``min2`` —— 两个值的"较大/较小者",**NULL 被忽略**。

    .. code-block:: text

       max2(1, 2)          = 2
       max2(NULL, 2)       = 2       ← NULL 不参与比较,而不是"结果是 NULL"
       max2(NULL, NULL)    = NULL
       max2(1, 'a')        → ILLEGAL_TYPE_OF_ARGUMENT(没有公共类型)

    这正是 ``and``/``or`` 之外,另一处**必须自己看 NULL** 的地方:
    如果沿用默认的 NULL 传播,``max2(NULL, 2)`` 会变成 NULL —— 那就错了。
    所以这一类函数的 ``propagate_nulls = False``。
    """

    #: ``max`` 或 ``min``
    mode: str = "max"

    propagate_nulls = False

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 2, 2)
        ta, tb = unwrap_nullable(args[0]), unwrap_nullable(args[1])
        base = common_supertype(ta, tb)
        if args[0].is_nullable() or args[1].is_nullable():
            # 任一侧可空 → 结果可空,因为"两边都是 NULL"时结果只能是 NULL
            from ..types.composite import DataTypeNullable

            if base.nullable_allowed:
                return DataTypeNullable(base)
        return base

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 2, 2)
        ret = self.get_return_type([a.type for a in args])
        a = full_column(args[0])
        b = full_column(args[1])
        out = ret.create_column()
        for i in range(rows):
            va, vb = a.get(i), b.get(i)
            if va is None and vb is None:
                out.append(None)
                continue
            if va is None:
                out.append(coerce_value(ret, vb))
                continue
            if vb is None:
                out.append(coerce_value(ret, va))
                continue
            pick = va if self._prefer(va, vb) else vb
            out.append(coerce_value(ret, pick))
        return out

    def _prefer(self, a: Any, b: Any) -> bool:
        return a >= b if self.mode == "max" else a <= b


@_register("max2", description="两个值中较大者(NULL 被忽略)")
class FunctionMax2(_MinMax2):
    """``max2(a, b)`` —— 两个值里的较大者。

    .. warning:: 真实 ClickHouse 里的公开名字是 ``greatest(a, b, ...)``
       (变参,定义在 :mod:`minich.functions.comparison`),``max2`` 是它的
       二元内部名。mini-clickhouse 把两个名字都注册成可用的函数,
       ``max2`` 只接受两个参数。
    """

    mode = "max"


@_register("min2", description="两个值中较小者(NULL 被忽略)")
class FunctionMin2(_MinMax2):
    """``min2(a, b)`` —— 两个值里的较小者。见 :class:`FunctionMax2` 的 warning。"""

    mode = "min"


# ══════════════════════════════════════════════════════════════════════════
# ⑦ 位运算
# ══════════════════════════════════════════════════════════════════════════


class _BitwiseBinary(FunctionBase):
    """``bitAnd`` / ``bitOr`` / ``bitXor`` —— 按位与或异或,整列向量化。

    位运算**不改数值语义,只改位模式**,所以"结果类型"就是两侧里较宽的那个
    (窄的一侧被扩宽,位模式按补码规则"符号扩展"):

    .. code-block:: text

       bitAnd(UInt8 0xF0, UInt16 0x0F0F) = UInt16 0x00F0
       bitAnd(Int8 -1, UInt16 1)         = UInt16 1
                                          ↑ Int8 的 -1 补码是 0xFF…,扩宽后全 1

    .. warning:: 真实 ClickHouse 对 ``bitAnd`` 之类的重载要求两侧**位宽相同**
       (``bitAnd(UInt8, UInt16)`` 会抛 "Arguments of bitAnd must have equal bit sizes")。
       mini-clickhouse 放宽了这条限制(窄侧按结果类型扩宽),
       因为 Python 里显式转类型很啰嗦,而在教学里"掩码"是最常见的用法。
    """

    #: ``and`` / ``or`` / ``xor``
    bit_op: str = "and"

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 2, 2)
        ta, tb = unwrap_nullable(args[0]), unwrap_nullable(args[1])
        for t in (ta, tb):
            if not is_integer_like(t):
                raise_error(
                    _ILLEGAL_TYPE,
                    f"Illegal types of arguments for function {self.name}: "
                    f"{ta.get_name()}, {tb.get_name()}",
                )
        return _wider_integer(ta, tb)

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 2, 2)
        if any(a.type.is_nullable() for a in args):
            col, _ = execute_with_null_propagation(self, args, rows, context)
            return col
        ta, tb = args[0].type, args[1].type
        ret = self.get_return_type([ta, tb])
        ret_dtype = np_dtype_of(ret)
        da, dbb = np_dtype_of(ta), np_dtype_of(tb)
        if ret_dtype is None or da is None or dbb is None or (
            da.kind != dbb.kind and da.itemsize == 8 and dbb.itemsize == 8
        ):
            # 128/256 位,或 64 位以上的符号混用 → 逐行(用 Python 的任意精度整数)
            a = full_column(args[0])
            b = full_column(args[1])
            out = ret.create_column()
            for i in range(rows):
                out.append(wrap_value(ret, self.compute_scalar(a.get(i), b.get(i))))
            return out
        # 先把两侧都扩到**结果类型**,再做位运算 —— 位模式与 C++ 一致
        a = numpy_arg(args[0]).astype(ret_dtype, copy=False)
        b = numpy_arg(args[1]).astype(ret_dtype, copy=False)
        op = {"and": np.bitwise_and, "or": np.bitwise_or, "xor": np.bitwise_xor}[self.bit_op]
        return ColumnVector(ret_dtype, op(a, b))

    def compute_scalar(self, a: Any, b: Any) -> Any:
        return {
            "and": a & b,
            "or": a | b,
            "xor": a ^ b,
        }[self.bit_op]


@_register("bitAnd", description="按位与")
class FunctionBitAnd(_BitwiseBinary):
    """``bitAnd(a, b)`` —— 按位与。最常见的用途是**取网段**与**掩码判断**::

       SELECT bitAnd(toUInt8(0b1100), toUInt8(0b1010));  -- 8 (0b1000)
    """

    bit_op = "and"


@_register("bitOr", description="按位或")
class FunctionBitOr(_BitwiseBinary):
    """``bitOr(a, b)`` —— 按位或。常见用途是把若干标志位**打包**成一个整数。"""

    bit_op = "or"


@_register("bitXor", description="按位异或")
class FunctionBitXor(_BitwiseBinary):
    """``bitXor(a, b)`` —— 按位异或。配合 ``bitCount`` 可以做汉明距离(见下)。"""

    bit_op = "xor"


class _BitUnary(FunctionBase):
    """一元位运算的骨架(``bitNot`` / ``bitCount``)。"""

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 1, 1)
        if args[0].type.is_nullable():
            col, _ = execute_with_null_propagation(self, args, rows, context)
            return col
        ta = args[0].type
        ret = self.get_return_type([ta])
        col = full_column(args[0])
        out = ret.create_column()
        for i in range(rows):
            out.append(wrap_value(ret, self.compute_scalar(col.get(i), ta)))
        return out

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 1)
        ta = unwrap_nullable(args[0])
        if not is_integer_like(ta):
            raise_error(
                _ILLEGAL_TYPE,
                f"Illegal type of argument for function {self.name}: {ta.get_name()}",
            )
        return ta

    def compute_scalar(self, value: Any, ta: DataType) -> Any:
        raise_error(ErrorCode.NOT_IMPLEMENTED, f"{self.name}: compute_scalar is not implemented")


@_register("bitNot", description="按位取反(同类型内回绕)")
class FunctionBitNot(_BitUnary):
    """``bitNot(x)`` —— 按位取反,**结果类型与参数相同**。

    .. code-block:: text

       bitNot(toUInt8(0))   = 255        ~0 在 8 位里就是 255
       bitNot(toInt8(0))    = -1         同样的位模式,有符号解释
       bitNot(toUInt16(0))  = 65535      **不是** 255 —— 位宽由类型决定

    最后一行是重点:取反是"在这个类型的位宽内取反",不是"取反成 255"。
    """

    def compute_scalar(self, value: Any, ta: DataType) -> Any:
        bits, _, _ = storage_layout(ta)
        return ~int(value) & ((1 << bits) - 1)


@_register("bitCount", description="二进制中 1 的个数(popcount)")
class FunctionBitCount(_BitUnary):
    """``bitCount(x)`` —— 二进制表示里 1 的个数(popcount),返回 ``UInt8``。

    .. code-block:: sql

       SELECT bitCount(toUInt8(0b10110110));  -- 5

    .. note:: 这就是 Census 变换的核心指令(``POPCNT``),
       真实 ClickHouse 用它算汉明距离、基数估计与稀疏位图。
       Python 里对应 ``int.bit_count()``(3.10+)。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 1)
        ta = unwrap_nullable(args[0])
        if not is_integer_like(ta):
            raise_error(
                _ILLEGAL_TYPE,
                f"Illegal type of argument for function {self.name}: {ta.get_name()}",
            )
        return get_data_type("UInt8")

    def compute_scalar(self, value: Any, ta: DataType) -> Any:
        return bin(int(value)).count("1")


class _BitShiftBinary(FunctionBase):
    """``bitShiftLeft`` / ``bitShiftRight`` / ``bitRotateLeft`` / ``bitRotateRight`` 的骨架。

    四个函数共享两个语义细节,而这两个细节正是**不能交给 NumPy** 的原因:

    1. 移位数可以是负数,表示**反方向移位**(真实 CH 的行为);
    2. 移位量 ≥ 位宽时结果为 0(左移/逻辑右移)或按位宽取模(循环移位)——
       x86 的移位指令会把移位数**取模 64/32**,直接照搬到 NumPy 上会得到一个
       悄无声息的错误答案(``1 << 8`` 在 UInt8 上可能得到 1 而不是 0)。
    """

    #: ``shl`` / ``shr`` / ``rotl`` / ``rotr``
    shift_op: str = "shl"

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 2, 2)
        ta, tb = unwrap_nullable(args[0]), unwrap_nullable(args[1])
        for t in (ta, tb):
            if not is_integer_like(t):
                raise_error(
                    _ILLEGAL_TYPE,
                    f"Illegal types of arguments for function {self.name}: "
                    f"{ta.get_name()}, {tb.get_name()}",
                )
        # 移位数不影响结果类型 —— 结果类型永远是**左边那个**的类型
        return ta

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 2, 2)
        if any(a.type.is_nullable() for a in args):
            col, _ = execute_with_null_propagation(self, args, rows, context)
            return col
        ret = self.get_return_type([args[0].type, args[1].type])
        a = full_column(args[0])
        b = full_column(args[1])
        out = ret.create_column()
        for i in range(rows):
            out.append(wrap_value(ret, self.compute_scalar(a.get(i), b.get(i), ret)))
        return out

    def compute_scalar(self, value: Any, shift: Any, ret: DataType) -> Any:
        bits, signed, _ = storage_layout(ret)
        mask = (1 << bits) - 1
        x = int(value) & mask
        n = int(shift)
        op = self.shift_op
        if op in ("shl", "shr") and n < 0:
            # 负的移位数 = 反方向移位(真实 CH 的实现就是这个语义)
            n = -n
            op = "shr" if op == "shl" else "shl"
        if op == "shl":
            return 0 if n >= bits else x << n
        if op == "shr":
            return 0 if n >= bits else x >> n
        n %= bits  # 循环移位:移位数按位宽取模
        if n == 0:
            return x
        if op == "rotl":
            return ((x << n) & mask) | (x >> (bits - n))
        return (x >> n) | ((x << (bits - n)) & mask)


@_register("bitShiftLeft", description="按位左移(移出的位被丢弃)")
class FunctionBitShiftLeft(_BitShiftBinary):
    """``bitShiftLeft(x, N)`` —— 左移 N 位,移出去的位丢掉,进来的补 0。

    .. code-block:: text

       bitShiftLeft(toUInt8(1), 7)  = 128
       bitShiftLeft(toUInt8(1), 8)  = 0     ← 整个值被移出去了
       bitShiftLeft(toUInt8(0b1000), -3) = 1  ← 负 N 表示右移

    结果类型永远是**第一个参数**的类型(第二个参数只是"移多少位")。
    """

    shift_op = "shl"


@_register("bitShiftRight", description="按位右移")
class FunctionBitShiftRight(_BitShiftBinary):
    """``bitShiftRight(x, N)`` —— 右移 N 位。

    真实 CH 对**有符号**数做的是**逻辑右移**(高位补 0,不是算术右移):
    ``bitShiftRight(toInt8(-1), 1)`` 得到 127,而不是 -1。
    这一点与大多数语言里的 ``>>`` 不同,值得单独记住。

    结果类型永远是第一个参数的类型。
    """

    shift_op = "shr"


@_register("bitRotateLeft", description="按位循环左移")
class FunctionBitRotateLeft(_BitShiftBinary):
    """``bitRotateLeft(x, N)`` —— 循环左移:移出去的位从右边补回来。

    .. code-block:: text

       bitRotateLeft(toUInt8(0b10000001), 1) = 0b00000011 = 3

    循环移位在哈希与伪随机数生成里非常常见(它保证"一位不少"),
    在加密实现里也是固定套路。移位数按位宽取模(``N % 8`` 对 UInt8)。
    """

    shift_op = "rotl"


@_register("bitRotateRight", description="按位循环右移")
class FunctionBitRotateRight(_BitShiftBinary):
    """``bitRotateRight(x, N)`` —— 循环右移,见 :class:`FunctionBitRotateLeft`。"""

    shift_op = "rotr"


class _BitTestBase(FunctionBase):
    """``bitTest`` / ``bitTestAll`` / ``bitTestAny`` 的公共骨架。

    三个函数的返回值都是 ``UInt8``(0/1),而不是整数的位模式 ——
    它们回答的是"这一位是不是 1"这个**布尔问题**,所以结果类型是 UInt8。
    """

    #: ``test`` / ``all`` / ``any``
    test_mode: str = "test"

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 2 if self.test_mode == "test" else 3)
        ta = unwrap_nullable(args[0])
        if not is_integer_like(ta):
            raise_error(
                _ILLEGAL_TYPE,
                f"Illegal type of argument for function {self.name}: {ta.get_name()}",
            )
        for t in args[1:]:
            if not is_integer_like(unwrap_nullable(t)):
                raise_error(
                    _ILLEGAL_TYPE,
                    f"Illegal types of arguments for function {self.name}: "
                    f"index arguments must be integers",
                )
        return get_data_type("UInt8")

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        if self.test_mode == "test":
            check_arity(self.name, args, 2, 2)
        else:
            check_arity(self.name, args, 3, None)
        if any(a.type.is_nullable() for a in args):
            col, _ = execute_with_null_propagation(self, args, rows, context)
            return col
        self.get_return_type([a.type for a in args])
        cols = [full_column(a) for a in args]
        out = ColumnVector(np.uint8)
        for i in range(rows):
            out.append(self.compute_row([c.get(i) for c in cols]))
        return out

    def compute_row(self, values: Sequence[Any]) -> int:
        x = int(values[0])
        bits = [int(v) for v in values[1:]]
        tested = [(x >> i) & 1 for i in bits]
        if self.test_mode == "test":
            return tested[0]
        if self.test_mode == "all":
            return 1 if all(tested) else 0
        return 1 if any(tested) else 0


@_register("bitTest", description="测试某一位是否为 1")
class FunctionBitTest(_BitTestBase):
    """``bitTest(x, i)`` —— 第 ``i`` 位(从 0 开始)是 1 就返回 1。

    .. code-block:: sql

       SELECT bitTest(toUInt8(0b1010), 1), bitTest(toUInt8(0b1010), 0);
       -- 1   0

    位下标超出位宽时返回 0(不报错)。结果类型 ``UInt8``。
    """

    test_mode = "test"


@_register("bitTestAll", description="测试若干位是否全为 1")
class FunctionBitTestAll(_BitTestBase):
    """``bitTestAll(x, i1, i2, ...)`` —— 指定的位**全都是** 1 时返回 1。

    典型用途:一次判断多个标志位是否都置位::

       SELECT bitTestAll(flags, 0, 3, 5) FROM t;   -- 相当于 (flags & 0b101001) = 0b101001
    """

    test_mode = "all"


@_register("bitTestAny", description="测试若干位是否有一位为 1")
class FunctionBitTestAny(_BitTestBase):
    """``bitTestAny(x, i1, i2, ...)`` —— 指定的位**至少有一个**是 1 时返回 1。"""

    test_mode = "any"


@_register("bitHammingDistance", description="两个值的汉明距离(不同位的个数)")
class FunctionBitHammingDistance(FunctionBase):
    """``bitHammingDistance(a, b)`` —— 两个整数的**汉明距离**:不同的二进制位个数。

    实现是一行:``bitCount(a XOR b)``。真实 ClickHouse 里它有一个更有意思的
    用法 —— **支持字符串**,把两个字符串按字节对齐后求不同的位,
    于是"近似去重"可以直接用它 + 阈值:

    .. code-block:: sql

       SELECT countIf(bitHammingDistance(s1, s2) < 3) FROM near_duplicates;

    .. note:: mini-clickhouse 只对**整数**实现(字符串版本需要先把
       ``String`` 的字节流取出来按较短的截断,与真实实现的边界处理还有差异,
       不实现比实现错好)。结果类型 ``UInt8``:汉明距离最多等于位宽,
       64 位的结果也装得进 UInt8。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 2, 2)
        ta, tb = unwrap_nullable(args[0]), unwrap_nullable(args[1])
        for t in (ta, tb):
            if not is_integer_like(t):
                raise_error(
                    _ILLEGAL_TYPE,
                    f"Illegal types of arguments for function {self.name}: "
                    f"{ta.get_name()}, {tb.get_name()} (mini-clickhouse 只支持整数)",
                )
        la = storage_layout(ta)
        lb = storage_layout(tb)
        if la[0] != lb[0]:  # type: ignore[index]
            raise_error(
                _ILLEGAL_TYPE,
                f"Arguments of function {self.name} must have equal bit sizes: "
                f"{ta.get_name()}, {tb.get_name()}",
            )
        return get_data_type("UInt8")

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 2, 2)
        if any(a.type.is_nullable() for a in args):
            col, _ = execute_with_null_propagation(self, args, rows, context)
            return col
        self.get_return_type([a.type for a in args])
        a = full_column(args[0])
        b = full_column(args[1])
        out = ColumnVector(np.uint8)
        for i in range(rows):
            out.append(bin(int(a.get(i)) ^ int(b.get(i))).count("1"))
        return out

"""数学函数 —— ``exp`` / ``sqrt`` / ``pow`` / ``sin`` / ``erf`` / ``factorial`` …

数学函数是"向量化收益最大"的一类函数:它们都是逐元素的纯函数,
没有分支、没有状态,一整列进来、一整列出去,NumPy 可以直接在 C/SIMD 层跑完。
所以这个模块里几乎所有函数都只有三行代码:

.. code-block:: python

   def get_return_type(self, args):        # ① 推类型:整数 → Float64,Float32 → Float32
       return float_result_type(args[0])
   def execute(self, args, rows, context):  # ② 整列交给 NumPy
       return _apply(self.np_func, args, rows, self.get_return_type([args[0].type]))

真实 ClickHouse 的 ``FunctionsMath.h`` 用的是同一套结构(宏展开出
``FunctionExp``/``FunctionLog`` 一族),区别只在它按 C++ 模板类型特化,
而不是在运行时查 ``np.exp``。

**结果类型规则**(与真实 CH 一致):

.. code-block:: text

   参数类型               结果类型        例
   ─────────────────────  ──────────────  ────────────────────────
   Float32                Float32         exp(toFloat32(1)) → Float32
   Float64                Float64         exp(1.0)         → Float64
   整数(UInt8…Int256)     Float64         exp(1)           → Float64
   Decimal                Float64         exp(toDecimal64(1,2)) → Float64

为什么整数参数要变成 ``Float64`` 而不是"保持整数"? 因为 ``sqrt(2)`` 根本
不是整数 —— 结果类型必须在**编译期**就定下来,那就只能取一个装得下的浮点。
(想要整数结果的平方根要用 ``intSqrt`` / ``sqrt`` 的整数重载,
它们返回 UInt 并**向下取整**。)

.. warning:: **这里有一个必须写出来的坑**:不能把实参原样丢给 NumPy。
   NumPy 的运算结果类型是跟着**数组 dtype** 走的,而 ``UInt8`` 列里的
   ``1000`` 早就回绕成了 232::

       np.log10(np.array([1000], dtype=np.uint8))       # → 2.365,不是 3.0
       np.power(np.uint8(2), np.uint8(10))              # → 0,不是 1024

   所以 :func:`_apply` 会先把参数统一转成浮点(顺带把 Decimal 按 scale 还原),
   再调用 ufunc —— 这正是真实 ClickHouse 在 ``FunctionMath`` 的
   ``executeImpl`` 里显式做的 ``convertToFloat``。这就是本模块
   **不用** ``base.numpy_execute`` 而自己包一层的原因。

.. warning:: mini-clickhouse 与真实 ClickHouse 的差异:

   1. ``lgamma`` / ``tgamma`` 没有 NumPy 实现,是**逐行**用 ``math`` 模块算的
      (慢,但语义精确);``erf``/``erfc`` 走 NumPy,是向量化的。
   2. ``log`` 只接受一个参数(自然对数);真实 CH 也没有 ``log(base, x)`` 这种
      重载,但有一整套 ``log2``/``log10``/``ln`` 的别名,这里都注册了。
   3. ``intExp2`` / ``intExp10`` 在指数超出 64 位时返回 **0**(真实 CH 是
      C++ 的未定义行为,一般表现为回绕或 0)。
   4. ``factorial`` 在 n ≥ 21 时**回绕**(UInt64 溢出),真实 CH 同样如此 ——
      ``factorial(21)`` 不是 51090942171709440000,而是一个回绕后的小数字。
"""

from __future__ import annotations

import math
from typing import Any, Callable, Sequence

import numpy as np

from ..block import ColumnWithTypeAndName
from ..columns import ColumnVector, IColumn
from ..errors import ErrorCode, raise_error
from ..types import DataType, get_data_type
from ..types.scalars import DataTypeNumber
from .arithmetic import _register  # noqa: F401  —— 本模块统一的注册装饰器
from .arithmetic import (
    check_arity,
    full_column,
    np_dtype_of,
    unwrap_nullable,
    wrap_value,
)
from .base import FunctionBase, execute_with_null_propagation, numpy_execute

__all__ = [
    "FunctionExp",
    "FunctionLog",
    "FunctionLog2",
    "FunctionLog10",
    "FunctionSqrt",
    "FunctionCbrt",
    "FunctionPow",
    "FunctionSign",
    "FunctionSigmoid",
    "FunctionFactorial",
    "FunctionErf",
    "FunctionErfc",
    "FunctionIntExp2",
    "FunctionIntExp10",
    "FunctionE",
    "FunctionPi",
]


def float_result_type(type_: DataType) -> DataType:
    """"数学函数的结果类型":Float32 保持不变,其余一律 Float64。

    这条规则在真实 CH 里叫 ``NumberTraits::ResultOfFloatingPoint`` 家族,
    它保证 ``toFloat32(x) * 2`` 不会**莫名其妙**变成 Float64 ——
    小数点后的位数由输入的精度决定,不该由函数名字决定。
    """
    if isinstance(type_, DataTypeNumber) and type_.float and type_.bits == 32:
        return type_
    return get_data_type("Float64")


def numeric_array(arg: ColumnWithTypeAndName, rows: int) -> np.ndarray:
    """把一整列取成数值 ndarray,而且**按 scale 还原 Decimal**。

    .. warning:: 这里有一个必须小心的地方:**Decimal 列里存的是未缩放整数**。
    ``toDecimal64(1.5, 1)`` 在列里是 ``15``。如果直接把它丢给 ``np.exp``,
    算出来的是 ``e^15`` 而不是 ``e^1.5`` —— 差着好几个数量级。
    真实 ClickHouse 会先把 Decimal 隐式转换成 Float64(按 scale 除),
    这个函数做的就是同一件事。
    """
    ta = arg.type
    col = full_column(arg)
    if ta.is_decimal():
        scale = float(10 ** ta.scale)
        return np.fromiter(
            (float(v) / scale for v in col.to_list()), dtype=np.float64, count=rows
        )
    if isinstance(col, ColumnVector):
        return col.numpy_view()
    return np.asarray(col.to_list())


def _apply(
    func: Callable[..., Any],
    args: Sequence[ColumnWithTypeAndName],
    rows: int,
    ret: DataType,
) -> IColumn:
    """把一元/二元数学函数作用到整列上 —— **本模块真正在用的向量化入口**。

    .. note:: 为什么不用现成的 ``numpy_execute``? 因为它会把实参**按原
       dtype** 交给 ``op``,而 NumPy 的运算类型是跟着数组走的::

           np.log10(np.array([1000], dtype=np.uint8))   # 1000 在 uint8 里已经是 232
           # → 2.365  ← 不是 3.0!
           np.power(np.array([2], dtype=np.uint8), np.array([10], dtype=np.uint8))
           # → 0      ← 2^10 在 uint8 里回绕了

       数学函数**必须先把参数转成浮点**:它要的是"1000 这个数",
       而不是"这一列里那一串 8 位存储"。真实 ClickHouse 也是先
       ``convertToFloat`` 再算的。所以这里统一做两件事:

       ① 按结果类型(Float32/Float64)把实参数组转成浮点;
       ② 顺带把 Decimal 的未缩放整数按 scale 还原(:func:`numeric_array`)。
    """
    dtype = np_dtype_of(ret) or np.dtype(np.float64)
    if dtype.kind != "f":
        raise_error(
            ErrorCode.LOGICAL_ERROR,
            "math function must not return a non-float type",
        )
    try:
        arrays = [np.asarray(numeric_array(a, rows), dtype=dtype) for a in args]
    except ValueError as exc:
        raise_error(
            ErrorCode.CANNOT_PARSE_NUMBER,
            f"Cannot convert an argument of a math function to float: {exc}",
        )
    with np.errstate(all="ignore"):
        res = func(*arrays)
    return ColumnVector(dtype, np.asarray(res, dtype=dtype))


class _UnaryMath(FunctionBase):
    """一元数学函数的公共骨架:整列交给 NumPy。

    子类只需要给出 ``np_func``(一个 ufunc)与 :attr:`name`。
    """

    #: NumPy 里的实现 —— 一元 ufunc
    np_func: Callable[..., Any] = staticmethod(np.exp)  # type: ignore[assignment]

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 1)
        return float_result_type(unwrap_nullable(args[0]))

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
        ret = self.get_return_type([args[0].type])
        return _apply(self.np_func, args, rows, ret)


def _make_unary_math(
    name: str,
    np_func: Callable[..., Any],
    description: str,
    doc: str,
) -> type:
    """批量生成一元数学函数类(``sin``/``cos``/… 一共十几个,手写太长)。

    真实 ClickHouse 用宏做同一件事(``M(Exp) M(Log) M(Sqrt)``),
    我们用一个循环 —— 语义一样,而且生成的类照样有独立的 docstring。
    """
    klass = type(
        "Function" + name[0].upper() + name[1:],
        (_UnaryMath,),
        {"np_func": staticmethod(np_func), "__doc__": doc},
    )
    _register(name, klass, description=description)
    globals()[klass.__name__] = klass
    __all__.append(klass.__name__)
    return klass


def _nullary_constant(name: str, value: float, description: str, doc: str) -> type:
    """生成"没有参数、只返回一个常量"的函数(``e`` / ``pi``)。"""

    def get_return_type(self: FunctionBase, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 0, 0)
        return get_data_type("Float64")

    def execute(
        self: FunctionBase,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 0, 0)
        col = ColumnVector(np.float64)
        col.append_many([value] * rows)
        return col

    klass = type(
        "Function" + name[0].upper() + name[1:],
        (FunctionBase,),
        {
            "get_return_type": get_return_type,
            "execute": execute,
            "__doc__": doc,
        },
    )
    _register(name, klass, description=description)
    globals()[klass.__name__] = klass
    __all__.append(klass.__name__)
    return klass


# ══════════════════════════════════════════════════════════════════════════
# 指数与对数
# ══════════════════════════════════════════════════════════════════════════


@_register("exp", description="自然指数 e^x")
class FunctionExp(_UnaryMath):
    """``exp(x)`` —— 自然指数 ``e^x``。

    .. code-block:: sql

       SELECT exp(0), exp(1), round(exp(1), 6);
       -- 1   2.718281828459045   2.718282

    .. note:: 浮点的"最短往返表示"在这里体现得很直接:``exp(1)`` 打印出
       ``2.718281828459045`` 而不是一个三位小数 —— 因为 ClickHouse 的输出
       必须能**精确解析回同一个 double**(见 ``DataTypeNumber.serialize_text``)。
       人看着累,但 ``FORMAT TSV`` 的往返无损靠的就是这条。
    """

    np_func = staticmethod(np.exp)


@_register("exp2", description="2 的 x 次方")
class FunctionExp2(_UnaryMath):
    """``exp2(x)`` —— ``2^x``。二进制世界里比 ``e^x`` 更有用(位运算的连续版本)。"""

    np_func = staticmethod(np.exp2)


@_register("exp10", description="10 的 x 次方")
class FunctionExp10(_UnaryMath):
    """``exp10(x)`` —— ``10^x``。

    NumPy 没有 ``exp10``(只有 ``exp2``),所以这里写成 ``10 ** x``。
    用 ``np.power(10.0, x)`` 而不是 ``10 ** x`` 是为了避免整数参数走整型幂
    (那会得到一个被回绕的整数,而结果类型是 Float64)。
    """

    np_func = staticmethod(lambda x: np.power(10.0, x))


@_register("log", aliases=["ln"], description="自然对数")
class FunctionLog(_UnaryMath):
    """``log(x)`` / ``ln(x)`` —— 自然对数(以 ``e`` 为底)。

    .. code-block:: sql

       SELECT log(1), ln(exp(2)), log(0), log(-1);
       -- 0   2   -inf   nan

    ``log(0)`` 是 ``-inf``、``log(-1)`` 是 ``nan`` —— **不报错**。
    这是 IEEE 语义,也是真实 CH 的行为(数学生成式里的 ``log`` 经常吃进 0)。
    想报错就自己加 ``WHERE x > 0``。
    """

    np_func = staticmethod(np.log)


@_register("log2", description="以 2 为底的对数")
class FunctionLog2(_UnaryMath):
    """``log2(x)`` —— 以 2 为底的对数。

    .. code-block:: sql

       SELECT log2(1024), log2(1), log2(0);
       -- 10   0   -inf

    在存储打交道的场景里它比自然对数常用得多(块大小、位数、编码长度)。
    """

    np_func = staticmethod(np.log2)


@_register("log10", description="以 10 为底的对数")
class FunctionLog10(_UnaryMath):
    """``log10(x)`` —— 以 10 为底的对数,常常用来数"一个数有几位"。

    .. code-block:: sql

       SELECT log10(1000), ceil(log10(999)) ;   -- 3  (999 是三位数)
    """

    np_func = staticmethod(np.log10)


# ══════════════════════════════════════════════════════════════════════════
# 根号与幂
# ══════════════════════════════════════════════════════════════════════════


@_register("sqrt", description="平方根")
class FunctionSqrt(_UnaryMath):
    """``sqrt(x)`` —— 平方根。负数返回 ``nan``(不报错)。

    .. code-block:: sql

       SELECT sqrt(4), sqrt(2), sqrt(-1);
       -- 2   1.4142135623730951   nan

    .. warning:: 想要**整数**的平方根请用 ``intSqrt``(整数入、整数出,
       向下取整),mini-clickhouse 未实现它 —— 用 ``floor(sqrt(x))`` 近似,
       但要注意 float64 只能精确表示 2^53 以内的整数,再大就会算错。
    """

    np_func = staticmethod(np.sqrt)


@_register("cbrt", description="立方根")
class FunctionCbrt(_UnaryMath):
    """``cbrt(x)`` —— 立方根。**负数有实根**,所以 ``cbrt(-8) = -2``。

    .. code-block:: sql

       SELECT cbrt(8), cbrt(-8);
       -- 2   -2

    这是它和 ``pow(x, 1/3)`` 的区别:后者对负数会得到 ``nan``。
    """

    np_func = staticmethod(np.cbrt)


@_register("pow", aliases=["power"], description="幂运算 x 的 y 次方")
class FunctionPow(FunctionBase):
    """``pow(x, y)`` / ``power(x, y)`` —— ``x`` 的 ``y`` 次方。

    .. code-block:: sql

       SELECT pow(2, 10), power(2, 0.5), pow(-1, 0.5);
       -- 1024   1.4142135623730951   nan

    结果类型永远是**浮点**(``Float32`` 只在两侧都是 ``Float32`` 时保留):
    ``pow(2, -1) = 0.5`` 说明幂运算天生就会产生小数,
    所以它不能像 ``plus`` 那样"整数进、整数出"。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 2, 2)
        ta, tb = unwrap_nullable(args[0]), unwrap_nullable(args[1])
        if ta.is_float() and tb.is_float() and ta.get_name() == tb.get_name():
            return ta
        return get_data_type("Float64")

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
        ret = self.get_return_type([a.type for a in args])
        return _apply(np.power, args, rows, ret)


@_register("hypot", description="直角三角形的斜边长度")
class FunctionHypot(FunctionBase):
    """``hypot(a, b)`` —— ``sqrt(a*a + b*b)``,但**中间不会溢出**。

    .. code-block:: sql

       SELECT hypot(3, 4), hypot(1e200, 1e200);
       -- 5   1.4142135623730951e+200

    第二列说明它为什么不能直接写成 ``sqrt(a*a + b*b)``:
    ``1e200 * 1e200`` 早就超出 double 的范围变成 ``inf`` 了。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 2, 2)
        ta, tb = unwrap_nullable(args[0]), unwrap_nullable(args[1])
        if ta.is_float() and tb.is_float() and ta.get_name() == tb.get_name():
            return ta
        return get_data_type("Float64")

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
        ret = self.get_return_type([a.type for a in args])
        return _apply(np.hypot, args, rows, ret)


# ══════════════════════════════════════════════════════════════════════════
# 三角函数
# ══════════════════════════════════════════════════════════════════════════

_make_unary_math(
    "sin",
    np.sin,
    "正弦(x 单位为弧度)",
    """``sin(x)`` —— 正弦,``x`` 以**弧度**为单位。

    .. code-block:: sql

       SELECT sin(0), sin(pi() / 2), sin(pi());
       -- 0   1   1.2246467991473532e-16     ← 不是 0!

    最后一列是浮点的老问题:``pi()`` 本身就不精确,``sin(pi)`` 得
    一个 1e-16 量级的残渣。要判等请用 ``abs(sin(pi())) < 1e-15``。
    """,
)
_make_unary_math(
    "cos",
    np.cos,
    "余弦(x 单位为弧度)",
    """``cos(x)`` —— 余弦。``cos(0) = 1``,``cos(pi()) = -1``(同样带浮点残差)。""",
)
_make_unary_math(
    "tan",
    np.tan,
    "正切(x 单位为弧度)",
    """``tan(x)`` —— 正切。``x`` 接近 ``pi/2`` 时会得到非常大的值(而不是报错)。

    .. code-block:: sql

       SELECT tan(0), tan(pi() / 2);
       -- 0   1.633123935319537e+16
    """,
)
_make_unary_math(
    "asin",
    np.arcsin,
    "反正弦,返回弧度",
    """``asin(x)`` —— 反正弦,返回**弧度**。``|x| > 1`` 时返回 ``nan``。""",
)
_make_unary_math(
    "acos",
    np.arccos,
    "反余弦,返回弧度",
    """``acos(x)`` —— 反余弦,返回弧度。``|x| > 1`` 时返回 ``nan``。""",
)
_make_unary_math(
    "atan",
    np.arctan,
    "反正切,返回弧度",
    """``atan(x)`` —— 反正切,返回弧度(值域固定为 ``(-pi/2, pi/2)``)。""",
)
_make_unary_math(
    "sinh",
    np.sinh,
    "双曲正弦",
    """``sinh(x)`` —— 双曲正弦 ``(e^x - e^-x) / 2``。""",
)
_make_unary_math(
    "cosh",
    np.cosh,
    "双曲余弦",
    """``cosh(x)`` —— 双曲余弦 ``(e^x + e^-x) / 2``。""",
)
_make_unary_math(
    "tanh",
    np.tanh,
    "双曲正切",
    """``tanh(x)`` —— 双曲正切,值域 ``(-1, 1)``。

    深度学习里最常见的激活函数之一;它比 ``sigmoid`` 更"陡",
    而且**以 0 为中心**。
    """,
)
_make_unary_math(
    "asinh",
    np.arcsinh,
    "反双曲正弦",
    """``asinh(x)`` —— 反双曲正弦(定义域是整个实数轴,不会返回 nan)。""",
)
_make_unary_math(
    "acosh",
    np.arccosh,
    "反双曲余弦",
    """``acosh(x)`` —— 反双曲余弦,``x < 1`` 时返回 ``nan``。""",
)
_make_unary_math(
    "atanh",
    np.arctanh,
    "反双曲正切",
    """``atanh(x)`` —— 反双曲正切,``|x| >= 1`` 时返回 ``nan`` 或 ``inf``。""",
)


class _Atan2(FunctionBase):
    """``atan2(y, x)`` —— 两参数反正切,可以分辨"象限"。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 2, 2)
        ta, tb = unwrap_nullable(args[0]), unwrap_nullable(args[1])
        if ta.is_float() and tb.is_float() and ta.get_name() == tb.get_name():
            return ta
        return get_data_type("Float64")

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
        ret = self.get_return_type([a.type for a in args])
        return _apply(np.arctan2, args, rows, ret)


_register("atan2", description="两参数反正切(能分辨象限)")(_Atan2)
FunctionAtan2 = _Atan2
_Atan2.__doc__ = """``atan2(y, x)`` —— 两参数反正切,返回弧度。

.. code-block:: sql

   SELECT atan2(1, 1), atan(1), atan2(-1, -1);
   -- 0.7853981633974483   0.7853981633974483   -2.356194490192345

它与 ``atan(y / x)`` 的区别在第三列:``atan(-1 / -1) = atan(1) = pi/4``,
而 ``atan2(-1, -1)`` 知道两个都是负数(第三象限),所以得到 ``-3pi/4``。
**丢掉象限信息**是坐标变换里最常见的 bug 之一。
"""
__all__.append("FunctionAtan2")


# ══════════════════════════════════════════════════════════════════════════
# 误差函数 / 伽马函数
class _RowwiseMath(FunctionBase):
    """``lgamma`` / ``tgamma`` / ``erf`` / ``erfc`` 的公共骨架 —— 用 ``math`` 逐行算。

    .. note:: NumPy 里**没有** ``lgamma``/``tgamma``/``erf``/``erfc``:
       这一族在 SciPy(``scipy.special``)里。为了不引入 SciPy 这个重依赖,
       它们走逐行回退 —— 也正是 :mod:`minich.functions.base` 的 docstring
       里说的"能跑但慢"的那条路。真实 ClickHouse 自己实现了这些函数
       (``src/Common/...`` 里的 ``lgamma`` 一族 + Boost 备选)。
    """

    #: ``math`` 模块里的实现
    math_func: Callable[[float], float] = staticmethod(math.lgamma)  # type: ignore[assignment]

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 1)
        return float_result_type(unwrap_nullable(args[0]))

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
        values = numeric_array(args[0], rows)
        out = ColumnVector(np.float64)
        for v in values:
            out.append(self.compute(float(v)))
        return out

    def compute(self, x: float) -> float:
        try:
            return self.math_func(x)
        except ValueError:
            # 极点在非正整数上:gamma 发散,lgamma 也发散
            return float("inf") if x <= 0 else float("nan")
        except OverflowError:
            return float("inf")


# ══════════════════════════════════════════════════════════════════════════

@_register("erf", description="误差函数 erf(x)")
class FunctionErf(_RowwiseMath):
    """``erf(x)`` —— 误差函数,值域 ``(-1, 1)``。

    .. code-block:: sql

       SELECT erf(0), erf(1), erf(-1);
       -- 0   0.8427007929497149   -0.8427007929497149

    正态分布的累积分布函数可以直接用它表示::

       CDF(x) = 0.5 * (1 + erf((x - mu) / (sigma * sqrt(2))))

    它的导数是 ``2/sqrt(pi) * e^(-x^2)`` —— 也就是正态分布的核。
    """

    math_func = staticmethod(math.erf)


@_register("erfc", description="互补误差函数 1 - erf(x)")
class FunctionErfc(_RowwiseMath):
    """``erfc(x) = 1 - erf(x)`` —— 互补误差函数。

    .. code-block:: sql

       SELECT erfc(0), erfc(5), 1 - erf(5);
       -- 1   1.5374597944280351e-12   1.5374597944280351e-12   (x 小时两者一样)

    为什么不直接写 ``1 - erf(x)``? 因为 ``x`` 大时 ``erf(x)`` 已经贴着 1,
    相减会把有效数字全丢掉(灾难性抵消)。``x = 10`` 时这个差别就很明显了。
    """

    math_func = staticmethod(math.erfc)


@_register("lgamma", description="伽马函数的对数 ln|Γ(x)|")
class FunctionLgamma(_RowwiseMath):
    """``lgamma(x)`` —— ``ln|Γ(x)|``(伽马函数的对数)。

    为什么要给"对数"单独开一个函数? 因为 ``Γ(x)`` 增长得比浮点快得多:
    ``Γ(171)`` 已经超过 double 的上限变成 ``inf``,而 ``lgamma(171)``
    只是 706 而已。**对数域里做计算**是数值统计的常规操作
    (最大似然、Beta 分布、斯特林公式)。

    .. code-block:: sql

       SELECT lgamma(1), lgamma(5), lgamma(171), lgamma(0);
       -- 0   3.1780538303479458   706.5730622457875   inf

    最后一个是极点(``Γ`` 在 0 处发散)。
    """

    math_func = staticmethod(math.lgamma)


@_register("tgamma", description="伽马函数 Γ(x)")
class FunctionTgamma(_RowwiseMath):
    """``tgamma(x)`` —— 伽马函数本身,``Γ(n) = (n-1)!``。

    .. code-block:: sql

       SELECT tgamma(1), tgamma(5), factorial(4);
       -- 1   24   24

    名字里的 ``t`` 是 "true gamma" —— 历史上有过一个命名混乱
    (老代码里的 ``gamma`` 指的是 ``lgamma``)。mini-clickhouse 只注册
    ``tgamma``,不注册那个有歧义的 ``gamma``。
    """

    math_func = staticmethod(math.gamma)


# ══════════════════════════════════════════════════════════════════════════
# 符号 / 阶跃 / 角度
# ══════════════════════════════════════════════════════════════════════════


@_register("sign", description="符号函数(-1/0/1,类型与参数相同)")
class FunctionSign(FunctionBase):
    """``sign(x)`` —— 取符号:负数 ``-1``、零 ``0``、正数 ``1``。

    .. code-block:: text

       sign(-3.5) = -1        sign(0) = 0        sign(7) = 1
       sign(toUInt8(3)) = 1   ← **结果类型与参数相同**,所以无符号参数只会得到 0/1
       toTypeName(sign(toInt8(-3))) = Int8

    "结果类型跟着参数走"看起来有点怪,但这正是 C++ 里
    ``x < 0 ? -1 : (x > 0 ? 1 : 0)`` 这个表达式天然的类型:
    三个分支的值都在参数类型的范围内(无符号的 -1 除外 —— 它是 0,
    因为 ``toUInt8(-1)`` 会回绕,所以这个分支永远选不到)。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 1)
        ta = unwrap_nullable(args[0])
        if ta.is_numeric():
            return ta
        return get_data_type("Float64")

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
        ret = self.get_return_type([args[0].type])
        if args[0].type.is_decimal():
            # C++ 里的 sign 是 x < 0 ? -1 : (x > 0 ? 1 : 0),三个分支的常量
            # 会被转成 Decimal —— 于是"1"在列里是 10^scale,而不是 1
            scale = 10 ** args[0].type.scale
            col = full_column(args[0])
            out = ret.create_column()
            for i in range(rows):
                v = col.get(i)
                out.append(0 if v == 0 else (scale if v > 0 else -scale))
            return out
        return numpy_execute(args, rows, np.sign, ret)


@_register("sigmoid", description="S 型函数 1/(1+e^-x)")
class FunctionSigmoid(_UnaryMath):
    """``sigmoid(x)`` —— ``1 / (1 + e^(-x))``,值域 ``(0, 1)``。

    .. code-block:: sql

       SELECT sigmoid(0), sigmoid(10), sigmoid(-10);
       -- 0.5   0.9999546000702375   4.5397868702434395e-05

    它是**逻辑回归**的输出层,也是"把任意实数压成概率"的最短写法。
    注意 ``sigmoid`` 不饱和到 0/1(那会毁掉梯度),而 ``erf`` 才是
    经典的"软阶跃"。
    """

    np_func = staticmethod(lambda x: 1.0 / (1.0 + np.exp(-x)))


@_register("degrees", description="弧度转角度")
class FunctionDegrees(_UnaryMath):
    """``degrees(x)`` —— 弧度 → 角度(乘 ``180/pi``)。

    .. code-block:: sql

       SELECT degrees(pi()), degrees(pi() / 2);
       -- 180   90
    """

    np_func = staticmethod(np.degrees)


@_register("radians", description="角度转弧度")
class FunctionRadians(_UnaryMath):
    """``radians(x)`` —— 角度 → 弧度(乘 ``pi/180``)。

    .. code-block:: sql

       SELECT radians(180), sin(radians(90));
       -- 3.141592653589793   1
    """

    np_func = staticmethod(np.radians)


# ══════════════════════════════════════════════════════════════════════════
# 整数幂与阶乘
# ══════════════════════════════════════════════════════════════════════════


class _IntegerMath(FunctionBase):
    """``factorial`` / ``intExp2`` / ``intExp10`` 的公共骨架:整数入、UInt64 出。"""

    return_type_cannot_be_nullable = True

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 1)
        return get_data_type("UInt64")

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
        ret = self.get_return_type([args[0].type])
        col = full_column(args[0])
        out = ColumnVector(np.uint64)
        for i in range(rows):
            out.append(wrap_value(ret, self.compute(int(col.get(i)))))
        return out

    def compute(self, x: int) -> int:
        raise_error(ErrorCode.NOT_IMPLEMENTED, f"{self.name}: compute is not implemented")


@_register("factorial", description="阶乘 n!(UInt64,会回绕)")
class FunctionFactorial(_IntegerMath):
    """``factorial(n)`` —— 阶乘 ``n!``,结果类型 ``UInt64``。

    .. code-block:: sql

       SELECT factorial(0), factorial(1), factorial(5), factorial(20);
       -- 1   1   120   2432902008176640000

    .. warning:: **21 就开始回绕**:``20!`` 是 UInt64 装得下的最大阶乘,
       ``21! = 51090942171709440000`` 超出 64 位,结果被回绕成一个"看起来
       毫无规律"的数字。真实 CH 同样如此(它不做大数运算)。
       负数返回 0(真实 CH 的 ``factorial(-1)`` 也是 0)。
    """

    def compute(self, x: int) -> int:
        if x < 0:
            return 0
        return math.factorial(x)


@_register("intExp2", description="2 的 x 次方(UInt64)")
class FunctionIntExp2(_IntegerMath):
    """``intExp2(x)`` —— ``2^x`` 的**整数**版本,返回 ``UInt64``。

    .. code-block:: sql

       SELECT intExp2(0), intExp2(10), intExp2(64);
       -- 1   1024   0        ← 64 位以上溢出了,mini 返回 0(真实 CH 是 UB)

    它比 ``exp2`` 快(整数移位 vs 浮点幂),在算桶编号
    (``intExp2(intDiv(log2(n), 2))``)时很常见。
    """

    def compute(self, x: int) -> int:
        if x < 0 or x > 63:
            return 0
        return 1 << x


@_register("intExp10", description="10 的 x 次方(UInt64)")
class FunctionIntExp10(_IntegerMath):
    """``intExp10(x)`` —— ``10^x`` 的整数版本,返回 ``UInt64``。

    .. code-block:: sql

       SELECT intExp10(0), intExp10(3), intExp10(20);
       -- 1   1000   0        ← 10^20 超出 UInt64,mini 返回 0
    """

    def compute(self, x: int) -> int:
        if x < 0 or x > 19:
            return 0
        return 10 ** x


# ══════════════════════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════════════════════

_nullary_constant(
    "e",
    math.e,
    "自然常数 e",
    """``e()`` —— 自然常数 ``e = 2.718281828459045``。

    .. code-block:: sql

       SELECT e(), exp(1), e() = exp(1);
       -- 2.718281828459045   2.718281828459045   1

    两个值的**二进制表示完全相同** —— 它们都是 IEEE 754 里最接近 ``e`` 的那个
    double。所以 ``e() = exp(1)`` 是 1(而不是"浮点不准所以不等")。
    """,
)
_nullary_constant(
    "pi",
    math.pi,
    "圆周率 pi",
    """``pi()`` —— 圆周率 ``3.141592653589793``。

    .. code-block:: sql

       SELECT pi(), sin(pi() / 2), cos(pi());
       -- 3.141592653589793   1   -1

    注意 ``sin(pi())`` 并不等于 0(而是 1.22e-16):``pi()`` 本身就已经
    被截断过,这是"浮点无精确三角函数"的直接体现。
    """,
)

"""取整函数 —— ``floor`` / ``ceil`` / ``round`` / ``trunc`` / ``roundAge`` …

取整看起来是最"没有歧义"的一类函数,其实有三个地方必须先想清楚:

.. code-block:: text

   ① 往哪边取?
        floor(x)   向下(向 -inf)     floor(-1.5) = -2
        ceil(x)    向上(向 +inf)      ceil(-1.5)  = -1
        trunc(x)   向零               trunc(-1.5) = -1
        round(x)   四舍五入(半远离零) round(-1.5) = -2
        roundBankers(x) 四舍五入(半取偶) roundBankers(0.5) = 0, roundBankers(1.5) = 2
      "trunc" 和 "floor" 在正数上一样、在负数上不一样,这是最常见的笔误来源。

   ② 对 Decimal 怎么办?
        Decimal(P, S) 是**定点数**,取整后**类型不变**(scale 不变),只是低位清零:
        floor(toDecimal64(1.99, 2)) = 1.00,类型仍是 Decimal(18, 2)。
      它不是变成 Int —— 因为"钱"列不该因为取了个整就换类型。

   ③ 第二个参数 N 可以是负数
        floor(1234, -2) = 1200      round(1250, -2) = 1300
      N 表示"保留几位小数",负数就是"小数点左边第几位清零" ——
      做按千分位归档的报表时非常有用。

.. warning:: mini-clickhouse 与真实 ClickHouse 的差异:

   1. ``roundDuration`` 的分档表按官方文档实现(1/10/30/60/120/180/240/300/
      600/1200/1800/3600/7200/18000/36000),但**返回类型取 UInt64** ——
      官方文档没有写明它的返回类型,而这些值显然装不进 UInt8。
   2. ``roundDown`` 在真实 CH 里是**变参**:``roundDown(x, 0, 18, 25, 35, 45, 55)``
      表示"取不超过 x 的最大档位"。mini-clickhouse 实现同样的语义,
      另外允许只有 1 个参数(此时退化成"向下取整到整数")。
   3. 真实 CH 的 ``floor``/``round`` 对第二个参数**要求是常量**,
      mini-clickhouse 只读第 0 行的值(常量列是常态,表达式求值器给的
      也基本是常量列)。
"""

from __future__ import annotations

import math
from typing import Any, List, Sequence

import numpy as np

from ..block import ColumnWithTypeAndName
from ..columns import ColumnVector, IColumn
from ..errors import ErrorCode, raise_error
from ..types import DataType, get_data_type
from ..types.scalars import DataTypeNumber
from .arithmetic import _register
from .arithmetic import (
    check_arity,
    coerce_value,
    common_supertype,
    full_column,
    is_integer_like,
    np_dtype_of,
    unwrap_nullable,
)
from .base import FunctionBase, execute_with_null_propagation

__all__ = [
    "rounding_return_type",
    "FunctionFloor",
    "FunctionCeil",
    "FunctionRound",
    "FunctionRoundBankers",
    "FunctionTrunc",
    "FunctionRoundToExp2",
    "FunctionRoundAge",
    "FunctionRoundDuration",
    "FunctionRoundDown",
]

#: 取整的几种方向 —— 名字与真实 ClickHouse 的实现结构一一对应
_FLOOR = "floor"
_CEIL = "ceil"
_TRUNC = "trunc"
_ROUND = "round"
_BANKERS = "bankers"


def rounding_return_type(type_: DataType) -> DataType:
    """取整类函数的结果类型:

    ===================  ==================  ============================
    参数类型              结果类型             说明
    ===================  ==================  ============================
    ``Float32``          ``Float32``         精度不因取整而提高
    ``Float64``          ``Float64``
    整数/日期/枚举        不变                取整对整数是恒等(除非 N < 0)
    ``Decimal(P, S)``    ``Decimal(P, S)``   **类型不变**,低位清零
    ``String`` 等        ``Float64``         先按浮点解析
    ===================  ==================  ============================
    """
    if isinstance(type_, DataTypeNumber):
        return type_
    if type_.is_decimal() or is_integer_like(type_):
        return type_
    return get_data_type("Float64")


# ══════════════════════════════════════════════════════════════════════════
# ① 逐值实现:float / int / decimal 三条
# ══════════════════════════════════════════════════════════════════════════


def _apply_int_mode(mode: str, v: int, m: int) -> int:
    """把整数 ``v`` 按"模 ``m``"取整 —— 整数运算,绝不引入浮点误差。

    这是 Decimal/整数取整的公共内核:``m = 10^k``,结果就是"把最低 k 位清零"。
    """
    if m == 1:
        return v
    if mode == _FLOOR:
        return (v // m) * m
    if mode == _CEIL:
        return -((-v) // m) * m
    if mode == _TRUNC:
        return (abs(v) // m) * m * (1 if v >= 0 else -1)
    quotient, remainder = divmod(abs(v), m)
    if mode == _ROUND:
        # 半远离零:0.5 一定"向上"(在绝对值意义上),与 C++ 的 std::round 一致
        if 2 * remainder >= m:
            quotient += 1
    elif mode == _BANKERS:
        # 半取偶:正好在中间时向偶数靠 —— 反复取整不会累积偏差
        if 2 * remainder > m or (2 * remainder == m and quotient % 2 == 1):
            quotient += 1
    else:  # pragma: no cover
        raise_error(ErrorCode.LOGICAL_ERROR, f"Unknown rounding mode {mode}")
    return quotient * m * (1 if v >= 0 else -1)


def _apply_float_mode(mode: str, y: float) -> float:
    if mode == _FLOOR:
        return math.floor(y)
    if mode == _CEIL:
        return math.ceil(y)
    if mode == _TRUNC:
        return math.trunc(y)
    if mode == _ROUND:
        # C++ 的 std::round:半远离零。Python 的 round() 是"半取偶",不能直接用
        return math.floor(y + 0.5) if y >= 0 else math.ceil(y - 0.5)
    if mode == _BANKERS:
        return float(round(y))  # Python 内置 round 就是 half-to-even
    raise_error(ErrorCode.LOGICAL_ERROR, f"Unknown rounding mode {mode}")  # pragma: no cover


def _quantize_scalar(mode: str, value: Any, type_: DataType, n: int) -> Any:
    """对一个值按"保留 ``n`` 位小数"取整;``n < 0`` 表示小数点左边第 |n| 位。"""
    if type_.is_decimal():
        # 列里是未缩放整数:先把"要清零的位数"换算到未缩放表示上
        digit = type_.scale - n
        if digit <= 0:
            return value
        return _apply_int_mode(mode, int(value), 10 ** digit)
    if is_integer_like(type_):
        if n >= 0:
            return value  # 整数取整是恒等
        return _apply_int_mode(mode, int(value), 10 ** (-n))
    # 浮点(以及 String 之类的"先转浮点再算")
    x = float(value)
    if not math.isfinite(x):
        return x
    scale = 10.0 ** n
    if scale == 0.0 or math.isinf(scale):
        return x
    return _apply_float_mode(mode, x * scale) / scale


def _vectorized_float(
    mode: str,
    arr: np.ndarray,
    n: int,
    dtype: np.dtype,
) -> np.ndarray:
    """浮点列的整体取整 —— NumPy 一遍过。

    ``np.round`` 用的正是 IEEE 的"半取偶",所以 ``roundBankers`` 可以直接用它;
    而 ``round``(半远离零)要自己拼(``np.where`` 两个分支)。
    """
    scale = 10.0 ** n
    y = arr.astype(np.float64, copy=False) * scale
    with np.errstate(all="ignore"):
        if mode == _FLOOR:
            r = np.floor(y)
        elif mode == _CEIL:
            r = np.ceil(y)
        elif mode == _TRUNC:
            r = np.trunc(y)
        elif mode == _ROUND:
            r = np.where(y >= 0, np.floor(y + 0.5), np.ceil(y - 0.5))
        else:
            r = np.round(y)
        r = r / scale
    return np.asarray(r, dtype=dtype)


# ══════════════════════════════════════════════════════════════════════════
# ② 函数骨架
# ══════════════════════════════════════════════════════════════════════════


class _RoundingFunction(FunctionBase):
    """``floor`` / ``ceil`` / ``round`` / ``trunc`` 的公共骨架。

    三条路,和算术模块一个套路:

    .. code-block:: text

       ① 有 NULL → execute_with_null_propagation
       ② 浮点列(N 是常量) → NumPy 一遍过
       ③ 其它(Decimal / 整数 / 负数 N / 非数值) → 逐行
    """

    #: ``floor`` / ``ceil`` / ``round`` / ``bankers`` / ``trunc``
    mode: str = "floor"

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 2)
        return rounding_return_type(unwrap_nullable(args[0]))

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 1, 2)
        if any(a.type.is_nullable() for a in args):
            col, _ = execute_with_null_propagation(self, args, rows, context)
            return col
        ta = args[0].type
        ret = self.get_return_type([a.type for a in args])
        n = _const_decimal_places(args[1]) if len(args) > 1 else 0
        dtype = np_dtype_of(ret)
        if isinstance(ta, DataTypeNumber) and ta.float and dtype is not None:
            arr = full_column(args[0])
            return ColumnVector(
                dtype, _vectorized_float(self.mode, arr.numpy_view(), n, dtype)
            )
        col = full_column(args[0])
        out = ret.create_column()
        for i in range(rows):
            raw = _quantize_scalar(self.mode, col.get(i), ta, n)
            out.append(coerce_value(ret, raw))
        return out


def _const_decimal_places(arg: ColumnWithTypeAndName) -> int:
    """读第二个参数 ``N``(真实 CH 要求它是**常量**)。

    为什么必须是常量? 两个理由:

    1. 走向量化还是逐行,取决于"取整粒度"是否固定 —— 非常量就退化成逐行;
    2. 更重要的:``floor(x, N)`` 的结果**类型**与 N 无关,但 N 一旦逐行变化,
       调用者就没法预判精度,读出来的数据也会随行而异。

    所以这里**校验**它确实是个常量:一旦发现同一列里有不同的值,
    就抛 ``ILLEGAL_TYPE_OF_ARGUMENT``(真实 CH 的报错是
    "Second argument for function floor must be constant")。
    """
    col = full_column(arg)
    if col.size() == 0:
        return 0
    first = int(col.get(0))
    for i in range(1, col.size()):
        if int(col.get(i)) != first:
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                "Second argument of a rounding function must be constant",
            )
    return first


@_register("floor", description="向下取整(向 -inf)")
class FunctionFloor(_RoundingFunction):
    """``floor(x[, N])`` —— 向下取整(朝 ``-inf`` 方向)。

    .. code-block:: sql

       SELECT floor(1.9), floor(-1.1), floor(1.234, 2), floor(1234, -2);
       -- 1   -2   1.23   1200

    第三列说明 ``N`` 是"保留几位小数";第四列说明 ``N`` 可以是负数
    (把小数点左边第 2 位清零)。

    ``floor(Decimal(P, S))`` **保持类型不变**:``floor(toDecimal64(1.99, 2))``
    得到 ``1.00`` 而不是 ``1`` —— 定点数的 scale 是列的属性,
    不该因为一次取整而改变(否则下游的写入就会类型不匹配)。
    """

    mode = _FLOOR


@_register("ceil", aliases=["ceiling"], description="向上取整(向 +inf)")
class FunctionCeil(_RoundingFunction):
    """``ceil(x[, N])`` / ``ceiling(x[, N])`` —— 向上取整(朝 ``+inf``)。

    .. code-block:: sql

       SELECT ceil(1.1), ceil(-1.9), ceil(1.234, 2);
       -- 2   -1   1.24

    注意 ``ceil(-1.9) = -1``:是"往大的方向走",不是"去掉小数部分"。
    想去掉小数部分要用 ``trunc``。这个区别在算"翻页数"时很致命 ——
    ``ceil(-1.9)`` 有人会以为该得到 -2。
    """

    mode = _CEIL


@_register("round", description="四舍五入(半远离零)")
class FunctionRound(_RoundingFunction):
    """``round(x[, N])`` —— 四舍五入,一半时**远离零**。

    .. code-block:: sql

       SELECT round(1.5), round(2.5), round(-1.5), round(1.2345, 2);
       -- 2   3   -2   1.23

    第二条 ``round(2.5) = 3`` 是它与 ``roundBankers`` 的分水岭:
    银行家舍入会给出 2(因为 2 是偶数)。金融场景用哪个取决于监管口径,
    但**不能想当然**——这是审计里真正会被追责的细节。

    .. warning:: ``round`` 自己定义域里的"半"是 **十进制** 意义上的半。
        ``round(2.675, 2)`` 得到 ``2.67`` 而不是 ``2.68``,因为 ``2.675``
       在二进制里其实是 ``2.67499999999999982…`` —— 它**本来就小于** 2.675。
       ClickHouse 也一样(它用的是同一套 IEEE 语义)。
       要"十进制精确"就用 ``Decimal`` 列。
    """

    mode = _ROUND


@_register("roundBankers", description="四舍五入(半取偶,银行家舍入)")
class FunctionRoundBankers(_RoundingFunction):
    """``roundBankers(x[, N])`` —— 银行家舍入:正好在中间时向**偶数**靠。

    .. code-block:: sql

       SELECT roundBankers(0.5), roundBankers(1.5), roundBankers(2.5), round(0.5), round(2.5);
       -- 0   2   2   (round: 1)   (round: 3)

    为什么金融界喜欢它? 因为"半远离零"在大量数据上会**系统性地把结果推大**:
    每一笔都多 0.5,一万笔就是 5000 的偏差。而"半取偶"在两个方向上
    概率相等,长期不漂移 —— 它也叫 "unbiased rounding"。

    NumPy 的 ``np.round`` 就是半取偶,所以这条路是**纯向量化**的。
    """

    mode = _BANKERS


@_register("trunc", aliases=["truncate"], description="向零取整(截断)")
class FunctionTrunc(_RoundingFunction):
    """``trunc(x[, N])`` / ``truncate`` —— 向零截断(去掉小数部分)。

    .. code-block:: sql

       SELECT trunc(1.9), trunc(-1.9), floor(-1.9), ceil(-1.9);
       -- 1   -1   -2   -1

    第二、三列就是 ``trunc`` 与 ``floor`` 的区别:负数上它们不一样。
    C++/Python 的 ``int(x)`` 强转也是向零截断 —— 三者是同一个语义,
    所以从别处搬代码过来时最容易错的就是这里。
    """

    mode = _TRUNC


# ══════════════════════════════════════════════════════════════════════════
# ③ roundToExp2 —— 按 2 的幂取整
# ══════════════════════════════════════════════════════════════════════════


@_register("roundToExp2", description="向下取整到不大于它的 2 的幂")
class FunctionRoundToExp2(FunctionBase):
    """``roundToExp2(x)`` —— 把 ``x`` 向下取整到"不大于它的最大的 2 的幂"。

    .. code-block:: sql

       SELECT roundToExp2(3), roundToExp2(15), roundToExp2(16), roundToExp2(0.9), roundToExp2(-5);
       -- 2   8   16   0   0

    第 4、5 列是它和普通取整最不一样的地方:小于 1(含负数)一律得到 **0**
    (官方文档:"If the number is less than 1, it returns 0")。

    .. warning:: 真实 ClickHouse 里可能还有 ``floorToExp2`` / ``ceilToExp2``
       这两个名字,但 mini-clickhouse **没有注册**它们 —— 一是无法确认它们
       在当前版本里是否存在,二是"取 2 的幂"这个概念更容易被误解成
       ``floor`` 的别名(那会得到一个完全不同、而且**不会报错**的结果)。
       宁可少一个名字,不要给出一个语义可疑的名字。
       确实需要"不小于 x 的 2 的幂"时,写成 ``2 ^ ceil(log2(x))`` 更明确。

    为什么需要这种函数? 因为哈希表、分块、位图这些结构的大小**必须是 2 的幂**
    (这样 "index = hash & (size - 1)" 才能代替取模运算),
    而用户给的大小往往是 1000、100 万 —— ``roundToExp2`` 正好把它们
    压到合法的容量上。``arrayResize`` / ``GROUP BY`` 的分桶都用这条规则。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 1)
        ta = unwrap_nullable(args[0])
        if ta.is_float() or is_integer_like(ta):
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
        values = _scaled_values(args[0], rows)
        out = ret.create_column()
        for x in values:
            if x < 1:
                out.append(coerce_value(ret, 0))
                continue
            out.append(coerce_value(ret, 2.0 ** math.floor(math.log2(x))))
        return out


def _scaled_values(arg: ColumnWithTypeAndName, rows: int) -> List[float]:
    """把一整列取成浮点值列表(Decimal 按 scale 还原)。"""
    ta = arg.type
    col = full_column(arg)
    if ta.is_decimal():
        scale = float(10 ** ta.scale)
        return [float(v) / scale for v in col.to_list()]
    return [float(v) for v in col.to_list()]


# ══════════════════════════════════════════════════════════════════════════
# ④ roundAge / roundDuration / roundDown —— Yandex.Metrica 的产物
# ══════════════════════════════════════════════════════════════════════════

#: ``roundAge`` 的分档(官方文档修订后的口径)。
#:
#: 它来自 Yandex.Metrica 的"访客年龄"报表:报表上只需要几个粗档位,
#: 而原始数据里的年龄是连续的。所以这些函数的价值不是"数学上正确",
#: 而是**整个公司对同一份数据算出同一个数字** —— 口径由函数固化下来。
#:
#: 结构是 ``(上界, 结果)`` 的阶梯,读起来就是那张对齐的文档表:
#: ``[0, 17]`` 之外的分界点(18/25/35/45/55)恰好是**一个档位的起点**。
_AGE_BUCKETS: List = [
    (1, 0),
    (18, 17),
    (25, 18),
    (35, 25),
    (45, 35),
    (55, 45),
    (math.inf, 55),
]

#: ``roundDuration`` 的分档(秒)—— 访问时长报表用的档位。
_DURATION_BUCKETS = [1, 10, 30, 60, 120, 180, 240, 300, 600, 1200, 1800,
                     3600, 7200, 18000, 36000]


@_register("roundAge", description="把年龄压到报表用的固定档位")
class FunctionRoundAge(FunctionBase):
    """``roundAge(x)`` —— 把年龄向下压到固定的档位,返回 ``UInt8``。

    .. code-block:: text

       输入范围        结果
       ──────────────  ──────
       x < 1           0
       1  ≤ x ≤ 17     17
       18 ≤ x ≤ 24     18
       25 ≤ x ≤ 34     25
       35 ≤ x ≤ 44     35
       45 ≤ x ≤ 54     45
       x ≥ 55          55

    .. code-block:: sql

       SELECT roundAge(15), roundAge(20), roundAge(29), roundAge(38), roundAge(1000);
       -- 17   18   25   35   55

    这一个函数就解释了"为什么数据库里要有业务函数":档位是**公司口径**,
    如果让每个分析师自己写 CASE WHEN,半年后报表就对不上了。
    把它写成函数,口径就只有一处、可被版本管理。

    .. warning:: 真实 ClickHouse 的历史文档曾是"低于 18 返回 0,然后取
       18/25/35/45/55 里最接近的档";官方后来出过一个文档修订,
       把口径改成上面这张表(多了 1–17 → 17 这一档)。mini-clickhouse
       按**修订后**的口径实现 —— 换句话说不保证和所有老版本 ClickHouse 一致。
    """

    return_type_cannot_be_nullable = True

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 1)
        return get_data_type("UInt8")

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
        values = _scaled_values(args[0], rows)
        out = ColumnVector(np.uint8)
        for x in values:
            out.append(_round_age(x))
        return out


def _round_age(x: float) -> int:
    """按 :data:`_AGE_BUCKETS` 的阶梯取值(第一个上界大于 x 的档位)。"""
    for upper, result in _AGE_BUCKETS:
        if x < upper:
            return result
    return 55  # pragma: no cover —— 最后一条的 upper 是 inf


@_register("roundDuration", description="把时长压到报表用的固定档位(秒)")
class FunctionRoundDuration(FunctionBase):
    """``roundDuration(x)`` —— 把时长(秒)向下压到固定的档位。

    .. code-block:: text

       档位集合:{1, 10, 30, 60, 120, 180, 240, 300, 600, 1200, 1800,
                 3600, 7200, 18000, 36000}

    取"不超过 ``x`` 的最大档位"(小于 1 则返回 0)::

       roundDuration(0.5)   = 0
       roundDuration(15)    = 10
       roundDuration(1000)  = 600
       roundDuration(10000) = 7200
       roundDuration(999999)= 36000

    与 :class:`FunctionRoundAge` 一样,它存在的意义是**统一口径**:
    "这次访问算 10 秒档还是 30 秒档"这种问题不该由每个查询自己回答。

    .. warning:: 返回类型取 ``UInt64``(档位上界是 36000,装不进 UInt8);
       官方文档没有写明这个函数的返回类型,所以这里是一个**有意的推断**,
       不要拿它当权威。
    """

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
        values = _scaled_values(args[0], rows)
        out = ColumnVector(np.uint64)
        for x in values:
            best = 0
            for bucket in _DURATION_BUCKETS:
                if bucket <= x:
                    best = bucket
                else:
                    break
            out.append(best)
        return out


@_register("roundDown", description="向下取整到给定的档位集合里最大的那个")
class FunctionRoundDown(FunctionBase):
    """``roundDown(x, border... )`` —— 取不超过 ``x`` 的**最大档位**。

    .. code-block:: sql

       SELECT roundDown(20, 0, 17, 18, 25, 35, 45, 55);
       -- 18

    它和 :class:`FunctionRoundAge` 的关系正是"通用实现的实例化":
    ``roundAge(20) = 18`` 与上面的结果完全一样 ——
    区别是 ``roundDown`` 把档位表**暴露给调用者**(可以换成自己的报表口径),
    而 ``roundAge`` 把口径**固化**在函数里(所有人算出来都一样)。

    这种"通用函数 + 固化口径的专用函数"的搭配在 ClickHouse 里到处都是
    (``quantile`` vs ``median``、``uniq`` vs ``uniqExact``),
    它们的取舍永远是同一个问题:**灵活 vs 一致**。

    .. warning:: 真实 CH 的 ``roundDown`` 是变参函数,档位都要写成常量。
       mini-clickhouse 允许档位来自列(逐行取值),但只对第 0 行的
       静态类型做结果类型推导 —— 档位来自非常量列是**非标准用法**。
       另外:只给一个参数时,退化成"向下取整到整数"。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, None)
        result = unwrap_nullable(args[0])
        for t in args[1:]:
            result = common_supertype(result, unwrap_nullable(t))
        return result

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 1, None)
        if any(a.type.is_nullable() for a in args):
            col, _ = execute_with_null_propagation(self, args, rows, context)
            return col
        ret = self.get_return_type([a.type for a in args])
        first = _scaled_values(args[0], rows)
        border_cols = [_scaled_values(a, rows) for a in args[1:]]
        out = ret.create_column()
        for i in range(rows):
            x = first[i]
            if not border_cols:
                out.append(coerce_value(ret, math.floor(x)))
                continue
            borders = [col[i] for col in border_cols]
            candidates = [b for b in borders if b <= x]
            out.append(coerce_value(ret, max(candidates) if candidates else min(borders)))
        return out

"""类型转换函数 —— ``toUInt8`` / ``toString`` / ``CAST`` / ``reinterpretAsString``。

转换函数里藏着一个必须先分清的对立,它决定了整个模块的结构:

.. code-block:: text

   转换(cast)                         重解释(reinterpret)
   ─────────────────────────────────  ────────────────────────────────────────
   改变**值**,保留"数的含义"            保留**字节**,改变"怎么解读"
   toUInt8(300)      = 44              reinterpretAsUInt8(toInt32(1)) = 1
   toInt8(-1)        = -1              reinterpretAsUInt8(toInt32(-1)) = 255
   toDate('2024-01-01') = 19723        reinterpretAsDate(toUInt32(19723)) = 19723 天
   走 toUInt8 的语义:回绕 / 舍入        走 memcpy 的语义:按位拷贝
   失败会报错(或 -OrZero/-OrNull)      永远不会失败(只要字节够)

"300 变成 44" 不是 bug,是 C++ 的整数回绕 —— :meth:`minich.types.scalars.DataTypeNumber.normalize`
里那句 ``self.wrap(value)`` 就是它的实现。**转换 = 用目标类型的 normalize**,
于是"插入数据"和"toXxx 转换"用的是同一套语义,不会出现"插进去是这个值、
转出来是另一个值"的割裂。

.. code-block:: text

   toXxx 的三个变体(这是 Ch 里最一致的一组命名约定):

     toUInt8(x)          失败 → **抛异常**        "我不接受坏数据"
     toUInt8OrZero(x)    失败 → 该类型的零值 0    "坏数据当 0 用"
     toUInt8OrNull(x)    失败 → NULL              "坏数据标成缺失"
                        ↑ 类型变成 Nullable(UInt8)

.. warning:: **mini-clickhouse 的一个框架级限制**(这是本模块最需要说清楚的事)。

   有几个函数的**结果类型取决于一个"值"**,而不是取决于实参的类型:

   ==========================  ==============================================
   函数                        类型依赖的值
   ==========================  ==============================================
   ``CAST(x, 'UInt8')``        第二个实参字符串里的类型名
   ``accurateCast`` 同上        同上
   ``toDecimal64(x, 2)``       第二个实参给的 scale
   ``toFixedString(s, 16)``    第二个实参给的 N
   ``defaultValueOfTypeName``  实参里的类型名
   ==========================  ==============================================

   而 :meth:`minich.functions.base.FunctionBase.get_return_type` 收到的只有
   **类型列表**:``get_return_type(args: Sequence[DataType])`` —— 它看不到任何值。
   真实 ClickHouse 的签名是
   ``getReturnTypeImpl(const ColumnsWithTypeAndName & arguments)``,
   它**拿得到列**,所以没有这个问题。mini-clickhouse 为了少一层抽象
   (见 base.py 的"重载解析被简化掉"那条 note)付出了这个代价。

   于是本模块的处理策略是**统一的三条**(每条都写在对应函数的 docstring 里):

   .. code-block:: text

      ① 类型名/参数作为**函数参数**传入时(即 get_function(name, params=[...]),
         由解析器把常量提成参数)→ 语义**完全正确**,这是推荐用法;
      ② 有"安全默认值"可用(声明类型与实际产生的类型仍然一致)
         → 用默认值并明确标注偏离(toFixedString 之类);
      ③ 没有安全默认值(猜错会静默算出错的值)→ 抛 NOT_IMPLEMENTED_IN_MINICH,
         消息里直接给出正确写法。

   ``CAST`` / ``accurateCast`` / ``defaultValueOfTypeName`` / ``toDecimal32…``
   属于第 ③ 类(它们的"猜"会让**值**出错,不能忍);
   ``toFixedString`` / ``toDateTime64OrNull`` 属于第 ② 类。
   这是"绝不给错结果"这条总原则的直接推论 —— 宁可报错,不要静默算错。
"""

from __future__ import annotations

import math
import struct
from typing import Any, Dict, Optional, Sequence, Tuple

import numpy as np

from ..block import ColumnWithTypeAndName
from ..columns import (
    ColumnConst,
    ColumnFixedString,
    ColumnNullable,
    ColumnString,
    ColumnVector,
    IColumn,
)
from ..errors import ErrorCode, MiniChError, raise_error
from ..types import DataType, get_data_type
from ..types.composite import (
    DataTypeDecimal,
    DataTypeLowCardinality,
    DataTypeNullable,
)
from ..types.scalars import DataTypeString, INTERVAL_KINDS
from .arithmetic import _register
from .arithmetic import (
    check_arity,
    full_column,
    np_dtype_of,
    storage_layout,
    unwrap_nullable,
    wrap_value,
)
from .base import FunctionBase, execute_with_null_propagation

__all__ = [
    "FunctionToString",
    "FunctionToStringCutToZero",
    "FunctionToFixedString",
    "FunctionToJSONString",
    "FunctionToLowCardinality",
    "FunctionToTypeName",
    "FunctionToColumnTypeName",
    "FunctionIsConstant",
    "FunctionMaterialize",
    "FunctionIdentity",
    "FunctionBlockSize",
    "FunctionDefaultValueOfTypeName",
    "FunctionCast",
    "FunctionAccurateCast",
]

_ILLEGAL_TYPE = ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT

#: ``-OrNull`` 家族的结果类型是 ``Nullable(目标类型)``,于是**不能**再走默认的
#: NULL 传播(那会得到 ``Nullable(Nullable(T))`` —— 类型系统里非法)。
#: 这条错误消息在 :class:`_CastBase` 里被用来提醒读者。
_PARAMS_HINT = (
    "mini-clickhouse 的 FunctionBase.get_return_type 只能看到实参的**类型**、"
    "看不到实参的**值**,而这个函数的结果类型取决于一个常量值。"
    "请把它作为**函数参数**传入,例如 get_function({name!r}, params={params!r});"
    "真实 ClickHouse 的 getReturnTypeImpl 收到的是列(能读到值),所以没有这个限制。"
)


# ══════════════════════════════════════════════════════════════════════════
# ① toXxx 族:数值 / Bool / 日期时间 / UUID / Interval
# ══════════════════════════════════════════════════════════════════════════


class _CastBase(FunctionBase):
    """``toXxx`` 家族的公共骨架 —— "用目标类型的 normalize 转换每一个值"。

    三条路:

    .. code-block:: text

       ① 数值 → 数值(且目标是整数/浮点):整列 astype —— 与 C++ 的回绕语义一致
       ② 其余(字符串解析、日期、UUID、Decimal…):逐行调用 target.normalize()
       ③ 失败时的行为由 on_error 决定:raise / zero / null

    为什么"数值 → 数值"能走 astype? 因为 ``np.ndarray.astype`` 的越界转换就是
    C 的截断/回绕(它不做检查,也不饱和)—— 与 ``DataTypeNumber.wrap`` 完全一致。
    这条路是**唯一**能让 ``toUInt64(x)`` 在一亿行上跑得动的原因。
    """

    #: 目标类型的名字(``"UInt8"``、``"Date"``、``"UUID"`` …)
    target_name: str = "UInt8"

    #: 转换失败时怎么办:``throw`` / ``zero`` / ``null``
    on_error: str = "throw"

    #: ``-OrNull`` 家族自己处理 NULL(结果类型本来就带 Nullable)
    propagate_nulls = True

    def get_target(self) -> DataType:
        return get_data_type(self.target_name)

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 1)
        target = self.get_target()
        if self.on_error == "null":
            if target.nullable_allowed:
                return DataTypeNullable(target)
            return target
        return target

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 1, 1)
        if self.on_error != "null" and args[0].type.is_nullable():
            # 默认的 NULL 传播:任一实参为 NULL → 结果 NULL(-OrZero 家族也一样)
            col, _ = execute_with_null_propagation(self, args, rows, context)
            return col
        return self._convert_column(args[0], rows)

    def _convert_column(self, arg: ColumnWithTypeAndName, rows: int) -> IColumn:
        target = self.get_target()
        ret = self.get_return_type([arg.type])
        src = unwrap_nullable(arg.type)
        col = full_column(arg)
        # 快路径:数值 → 数值。整列一次 astype,越界按 C 的语义截断/回绕
        if (
            self.on_error == "throw"
            and not _is_bool_type(target)
            and not target.is_string()
            and not src.is_string()
            and not src.is_decimal()
            and not target.is_decimal()
            and np_dtype_of(src) is not None
            and np_dtype_of(target) is not None
            and isinstance(col, ColumnVector)
        ):
            dtype = np_dtype_of(target)
            return ColumnVector(dtype, col.numpy_view().astype(dtype))
        out = ret.create_column()
        for i in range(rows):
            out.append(self.convert_one(col.get(i), target, ret))
        return out

    def convert_one(self, value: Any, target: DataType, ret: DataType) -> Any:
        if value is None:
            return None
        try:
            return target.normalize(value)
        except MiniChError as exc:
            return self.failure_value(value, target, exc)
        except (ValueError, TypeError, OverflowError, ArithmeticError, AttributeError) as exc:
            # AttributeError 也在名单里,原因很实在:基础层
            # ``DataTypeNumber.deserialize_text`` 在解析失败时会去取
            # ``ErrorCode.CANNOT_PARSE_FLOAT`` —— 那个常量在我们的错误码表里
            # **并不存在**,于是抛的是 AttributeError 而不是 MiniChError。
            # 这里把它兜住,换成一条正经的 CANNOT_CONVERT_TYPE
            # (这个"错误码表里缺名字"的问题已在报告里单独指出)。
            return self.failure_value(value, target, exc)

    def failure_value(self, value: Any, target: DataType, error: Any = None) -> Any:
        """转换失败时的落点 —— ``-OrZero`` 给零值,``-OrNull`` 给 NULL,否则抛错。

        .. note:: 两个细节:

        1. ``-OrZero`` 的"零值"必须是**存储表示**而不是显示形式:
           ``Date`` 的 ``default_value()`` 是字符串 ``'1970-01-01'``,
           而列里存的是天数 ``0``。所以这里再走一次 ``normalize`` ——
           让类型自己把"零值"翻译成内部表示(``DataType.default_value``
           与 ``normalize`` 的这一对,正是"零值"在引擎里的两处出现)。
        2. "抛错"这条路会**原样重抛**底层抛出的错误(例如 CANNOT_PARSE_NUMBER),
           不把它换成笼统的 CANNOT_CONVERT_TYPE —— 错误码是接口的一部分,
           能给出精确的码就不要给模糊的码(见 :mod:`minich.errors` 的模块 docstring)。
        """
        if self.on_error == "zero":
            return target.normalize(target.default_value())
        if self.on_error == "null":
            return None
        if isinstance(error, MiniChError):
            raise error
        raise_error(
            ErrorCode.CANNOT_CONVERT_TYPE,
            f"Cannot convert {value!r} to {target.get_name()} in function {self.name}",
        )


def _is_bool_type(type_: DataType) -> bool:
    """``Bool`` 在 mini-clickhouse 里是一个**独立类型**(族名 ``Bool``),
    而它的底层列是 ``UInt8`` —— 所以判断"是不是 Bool"只能按名字来。"""
    return type_.get_name() == "Bool"


def _register_cast(
    name: str,
    target_name: str,
    on_error: str,
    description: str,
    doc: str,
) -> type:
    """注册一个 ``toXxx`` / ``toXxxOrZero`` / ``toXxxOrNull``。

    三种变体只差两个类属性,所以用一个表驱动生成 —— 真实 ClickHouse 也是
    用宏批量生成的(``M(ToUInt8, UInt8)`` 之类),这里用 ``type()`` 做同一件事。
    """
    klass = type(
        "Function" + name[0].upper() + name[1:],
        (_CastBase,),
        {
            "target_name": target_name,
            "on_error": on_error,
            "propagate_nulls": on_error != "null",
            "__doc__": doc,
        },
    )
    _register(name, klass, description=description)
    globals()[klass.__name__] = klass
    __all__.append(klass.__name__)
    return klass


#: 走 toXxx 家族的标量类型。**不包括** Date/DateTime/UUID/IP —— 它们的转换
#: 需要额外信息(时区、解析规则),单独写在下面的小类里。
_NUMERIC_CAST_TARGETS = [
    "UInt8", "UInt16", "UInt32", "UInt64", "UInt128", "UInt256",
    "Int8", "Int16", "Int32", "Int64", "Int128", "Int256",
    "Float32", "Float64",
]

for _target in _NUMERIC_CAST_TARGETS:
    _register_cast(
        f"to{_target}",
        _target,
        "throw",
        f"转换成 {_target}(越界回绕,不能解析则报错)",
        f"""``to{_target}(x)`` —— 转换成 ``{_target}``。

数值转换遵循 C++ 的语义:**整数越界回绕、浮点转整数向零截断**。

.. code-block:: sql

   SELECT to{_target}(1), to{_target}('2.9'), to{_target}(250);
   -- 1   2   250(越界时回绕,不报错)

不能解析的字符串(``'abc'``)会抛 ``CANNOT_PARSE_NUMBER``;
想要"坏数据给 0"用 ``to{_target}OrZero``,想要 NULL 用 ``to{_target}OrNull``。

.. note:: 与真实 ClickHouse 的一个小差异:真实 CH 把 **Float → Int** 定义为
   "四舍五入"(``toInt8(1.5) = 2``),mini-clickhouse 沿用
   :class:`minich.types.scalars.DataTypeNumber` 的既定语义 —— **向零截断**
   (``toInt8(1.5) = 1``)。插入数据与 ``toXxx`` 走的是同一个函数,
   所以两边**自洽**,但与真实 CH 不完全一致。
""",
    )
    _register_cast(
        f"to{_target}OrZero",
        _target,
        "zero",
        f"转换成 {_target},失败给 0",
        f"""``to{_target}OrZero(x)`` —— 转换成 ``{_target}``,解析失败时返回 **0**。

.. code-block:: sql

   SELECT to{_target}OrZero('abc'), to{_target}OrZero('3');
   -- 0   3

"坏数据当 0"是最省事的清洗策略,但也是**最容易掩盖问题**的:
一列 100 万行里混进 3 行脏数据,报表上只会少一点、不会报错。
生产上更好的做法是先 ``countIf(x = 0 AND s != '0')`` 量一下脏数据的规模。

.. warning:: NULL 输入仍然按 **NULL 传播**(结果是 ``Nullable({_target})`` 的 NULL),
不会被当成"失败 → 0"。
""",
    )
    _register_cast(
        f"to{_target}OrNull",
        _target,
        "null",
        f"转换成 {_target},失败给 NULL",
        f"""``to{_target}OrNull(x)`` —— 转换成 ``{_target}``,解析失败时返回 **NULL**。

.. code-block:: sql

   SELECT to{_target}OrNull('abc'), to{_target}OrNull('3'), toTypeName(to{_target}OrNull(1));
   -- NULL   3   Nullable({_target})

它是"清洗"与"不丢信息"之间最好的折中:坏数据变成 NULL,
既不会污染求和(``sum`` 跳过 NULL),又能用 ``countIf(isNull(...))`` 精确统计。
代价是列类型变成 ``Nullable`` —— 而 ``Nullable`` 会关掉一批优化(见
:class:`minich.types.composite.DataTypeNullable` 的 warning)。
""",
    )


_register_cast(
    "toDate32",
    "Date32",
    "throw",
    "转换成 Date32(1900–2299)",
    """``toDate32(x)`` —— 转换成 ``Date32`` —— 范围 **1900-01-01 ~ 2299-12-31**。

.. code-block:: sql

   SELECT toDate32('1900-01-01'), toDate32(1);
   -- 1900-01-01   1970-01-02

``Date`` 是 ``UInt16`` 天数(最多到 2149 年),而 ``Date32`` 是 ``Int32`` 天数 ——
所以"Date 装不下的历史日期"只能用它。为一份 1990 年之前的数据建表时,
把列类型写成 ``Date`` 就是一个会**静默回绕**的陷阱(真实 CH 会按
``date_time_overflow_behavior`` 饱和,mini-clickhouse 与 ``DataTypeDate`` 一致地回绕)。
""",
)
_register_cast(
    "toDateOrNull",
    "Date",
    "null",
    "转换成 Date,失败给 NULL",
    """``toDateOrNull(x)`` —— 转换成 ``Date``,解析失败返回 **NULL**,类型 ``Nullable(Date)``。

清洗"日期列里混了空串/垃圾"的老数据时最实用的函数。
""",
)
_register_cast(
    "toDateOrZero",
    "Date",
    "zero",
    "转换成 Date,失败给零值 1970-01-01",
    """``toDateOrZero(x)`` —— 转换成 ``Date``,解析失败返回**零值** ``1970-01-01``。

.. warning:: 零值 ``1970-01-01`` 在报表里**看起来像真数据** ——
   这是它比 ``toDateOrNull`` 危险的地方。要用它,请先确认 1970-01-01
   在你的业务里不可能出现。
""",
)
_register_cast(
    "toDate32OrNull",
    "Date32",
    "null",
    "转换成 Date32,失败给 NULL",
    """``toDate32OrNull(x)`` —— 转换成 ``Date32``,解析失败返回 ``NULL``。""",
)
_register_cast(
    "toDate32OrZero",
    "Date32",
    "zero",
    "转换成 Date32,失败给零值 1900-01-01",
    """``toDate32OrZero(x)`` —— 转换成 ``Date32``,解析失败返回零值 ``1900-01-01``。""",
)
_register_cast(
    "toDateTimeOrNull",
    "DateTime",
    "null",
    "转换成 DateTime,失败给 NULL",
    """``toDateTimeOrNull(x[, tz])`` —— 转换成 ``DateTime``,失败返回 ``NULL``。

mini-clickhouse 只接受一个参数(时区写在**列类型**上,见
:class:`minich.functions.datetime.FunctionToDateTime` 的说明)。
""",
)
_register_cast(
    "toDateTimeOrZero",
    "DateTime",
    "zero",
    "转换成 DateTime,失败给零值 1970-01-01 00:00:00",
    """``toDateTimeOrZero(x)`` —— 转换成 ``DateTime``,解析失败返回零值 ``1970-01-01 00:00:00``。""",
)
_register_cast(
    "toBool",
    "Bool",
    "throw",
    "转换成 Bool(非零即真)",
    """``toBool(x)`` —— 转换成 ``Bool``(**非零即真**)。

.. code-block:: sql

   SELECT toBool(0), toBool(2), toBool(-1), toBool('true'), toBool('abc');
   -- false   true   true   true   (抛 CANNOT_PARSE_TEXT)

注意 ``toBool(2)`` 是 ``true`` 而不是 2:``Bool`` 的底层是 ``UInt8``,
但它的**值域只有 0 和 1** —— 这正是"Bool 是独立类型"的意义所在。
字符串接受 ``'true'/'false'/'1'/'0'/'t'/'f'`` 这些写法(与真实 CH 一致)。
""",
)
_register_cast(
    "toUUID",
    "UUID",
    "throw",
    "转换成 UUID",
    """``toUUID(x)`` —— 转换成 ``UUID``(接受 ``'550e8400-…'`` 这样的文本或 128 位整数)。

.. code-block:: sql

   SELECT toUUID('550e8400-e29b-41d4-a716-446655440000');
   -- 550e8400-e29b-41d4-a716-446655440000

``UUID`` 在列里存的是**无符号 128 位整数**,文本只是它的显示形式 ——
所以比较、排序、``ORDER BY`` 都是数值比较(比字符串快得多)。
格式不合法的字符串会抛 ``CANNOT_PARSE_UUID``。
""",
)
_register_cast(
    "toUUIDOrNull",
    "UUID",
    "null",
    "转换成 UUID,失败给 NULL",
    """``toUUIDOrNull(x)`` —— 转换成 ``UUID``,失败返回 ``NULL``(类型 ``Nullable(UUID)``)。""",
)

# ── Interval:11 个 toIntervalXxx ──────────────────────────────────────────

for _kind, (_suffix, _seconds) in INTERVAL_KINDS.items():
    _register_cast(
        f"toInterval{_suffix}",
        f"Interval{_suffix}",
        "throw",
        f"转换成 Interval{_suffix}(用于日期加减)",
        f"""``toInterval{_suffix}(n)`` —— 把整数 ``n`` 变成一个 ``Interval{_suffix}`` 值。

.. code-block:: sql

   SELECT now() + toInterval{_suffix}(1);

它等价于字面量 ``INTERVAL 1 {_kind}``,但**可以来自表达式**
(``toIntervalDay(days)``),而 ``INTERVAL n DAY`` 里的 n 必须是常量 ——
这是"动态间隔"唯一的标准写法。

.. note:: mini-clickhouse 里 ``Date``/``DateTime`` 与 Interval 的加减只支持
   **固定秒数**的那些单位(Second/Minute/Hour/Day/Week);
   Month/Quarter/Year 需要日历运算,会抛 ``NOT_IMPLEMENTED_IN_MINICH``
   (请用 ``addMonths`` 一族),见
   :func:`minich.functions.arithmetic.interval_multiplier`。
""",
    )


# ══════════════════════════════════════════════════════════════════════════
# ② Decimal 族 —— 需要 scale,属于"策略 ③"
# ══════════════════════════════════════════════════════════════════════════


class _DecimalCast(FunctionBase):
    """``toDecimal32/64/128/256(x[, S])`` 与 ``toDecimal(x[, P, S])``。

    .. warning:: **必须把 scale 作为函数参数传入**(策略 ③):

       .. code-block:: python

          # 正确:S 作为"函数参数"(解析器把常量提成参数)
          get_function("toDecimal64", params=[2])      # → Decimal(18, 2)

          # 不支持:把 S 写在括号里当实参
          #   toDecimal64(x, 2)   ← mini 会抛 NOT_IMPLEMENTED_IN_MINICH

       理由不是懒,而是**不允许猜**:``get_return_type`` 看不到实参的值,
       如果随手默认成 ``Decimal(18, 2)``,那么用户写 ``toDecimal64(x, 6)``
       就会得到"类型是 scale=2、值却按 scale=6 解释"的结果 —— 一个**静默的
       数值错误**(差 10^4 倍)。宁可报错。

    Decimal 的 scale 就是"小数点后几位",它**属于类型而不属于值**
    (见 :class:`minich.types.composite.DataTypeDecimal`):
    ``Decimal(18, 2)`` 的列里存的是整数,小数点位置记在类型上。
    所以"toDecimal 系列"的本质是"给一个数**指定**小数点位置"。
    """

    #: ``Decimal32`` = 精度 9,``Decimal64`` = 18,``Decimal128`` = 38,``Decimal256`` = 76
    bits: Optional[int] = None

    propagate_nulls = True

    def _resolve(self) -> Tuple[int, int]:
        params = list(getattr(self, "params", []) or [])
        if self.bits is not None:
            if len(params) != 1:
                raise_error(
                    ErrorCode.NOT_IMPLEMENTED_IN_MINICH,
                    _PARAMS_HINT.format(name=self.name, params=[2]),
                )
            precision = {32: 9, 64: 18, 128: 38, 256: 76}[self.bits]
            return precision, int(params[0])
        if len(params) != 2:
            raise_error(
                ErrorCode.NOT_IMPLEMENTED_IN_MINICH,
                _PARAMS_HINT.format(name=self.name, params=[18, 2]),
            )
        return int(params[0]), int(params[1])

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 1)
        precision, scale = self._resolve()
        return DataTypeDecimal(precision, scale)

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
        out = ret.create_column()
        for i in range(rows):
            # DataTypeDecimal.normalize 返回的是**未缩放整数**(列里就存这个)
            out.append(wrap_value(ret, ret.normalize(col.get(i))))
        return out


for _bits in (32, 64, 128, 256):
    _klass = type(
        f"FunctionToDecimal{_bits}",
        (_DecimalCast,),
        {
            "bits": _bits,
            "__doc__": f"""``toDecimal{_bits}(x)`` —— 把 ``x`` 转成 ``Decimal{_bits}(S)``。

精度固定为 ``{{32: 9, 64: 18, 128: 38, 256: 76}}[{{_bits}}] = {({32: 9, 64: 18, 128: 38, 256: 76})[_bits]}``,
scale ``S`` 由**函数参数**给出(见 :class:`_DecimalCast` 的 warning)::

   get_function("toDecimal{_bits}", params=[2])   # → Decimal({({32: 9, 64: 18, 128: 38, 256: 76})[_bits]}, 2)

比它更精确的表达是 ``accurateCast(x, 'Decimal(P, S)')`` ——
后者会**检查范围**,装不下就报错,而 ``toDecimal`` 系列沿用"回绕"语义。
""",
        },
    )
    _register(f"toDecimal{_bits}", _klass, description=f"转换成 Decimal{_bits}(S)", )
    globals()[_klass.__name__] = _klass
    __all__.append(_klass.__name__)


class FunctionToDecimal(_DecimalCast):
    """``toDecimal(x)`` —— 任意精度的 Decimal,``P`` 与 ``S`` 都来自函数参数。

    .. code-block:: python

       get_function("toDecimal", params=[18, 4])   # → Decimal(18, 4)

    它比 ``toDecimal64`` 灵活(精度自己定),代价是必须自己保证
    ``P`` 至少装得下整数部分 + 小数部分。
    """


_register("toDecimal", FunctionToDecimal, description="转换成 Decimal(P, S)(P/S 来自参数)")


# ══════════════════════════════════════════════════════════════════════════
# ③ 字符串族:toString / toStringCutToZero / toFixedString / toJSONString
# ══════════════════════════════════════════════════════════════════════════


@_register("toString", description="把任意值转成它的文本表示")
class FunctionToString(FunctionBase):
    """``toString(x)`` —— 把任意类型的值转成它的**文本表示**(类型 ``String``)。

    .. code-block:: sql

       SELECT toString(1), toString(1.5), toString(toDate('2024-01-01')), toString([1,2,3]);
       -- 1   1.5   2024-01-01   [1,2,3]

    "文本表示"不是随便定的 —— 它必须**往返无损**:
    ``toDate(toString(d)) = d``、``toUInt64(toString(x)) = x``。
    mini-clickhouse 直接调用 :meth:`minich.types.base.DataType.serialize_text`,
    也就是 ``FORMAT TSV`` 用的同一个函数 —— 于是"打印出来"和"转成字符串"
    天然是同一件事(真实 ClickHouse 也是这么分层的:``ISerialization::serializeText``)。

    .. warning:: ``toString`` 是**显式**的。ClickHouse 不会把数字隐式转成字符串
       (``concat('a', 1)`` 合法是因为 ``concat`` 自己接受任意类型,
       而 ``'a' = 1`` 会报 "Illegal types of arguments")。这条严格性挡住了
       一大批"迁移过来就悄悄变了语义"的查询。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 1)
        return get_data_type("String")

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
        col = full_column(args[0])
        out = ColumnString()
        for i in range(rows):
            out.append(ta.serialize_text(col.get(i)))
        return out


@_register("toStringCutToZero", description="转字符串并在第一个 0 字节处截断")
class FunctionToStringCutToZero(FunctionBase):
    """``toStringCutToZero(s)`` —— 转成 ``String``,并在**第一个零字节处截断**。

    .. code-block:: sql

       SELECT toStringCutToZero('ab\\0cd'), toStringCutToZero('ab');
       -- ab   ab

    为什么需要它? 因为在 **C 风格**的数据源里,字符串是"以 NUL 结尾"的:
    一个 ``FixedString(16)`` 里存了 ``'ab'``,后面 14 个字节都是 0 ——
    ``toString`` 会原样保留(因为 Ch 的 String 是二进制安全的),
    而 ``toStringCutToZero`` 会帮你按 C 的语义切掉。

    真实 CH 里它最常出现在"解析二进制协议/读老文件"的场景;
    日常查询里我们更希望你用 ``FixedString`` 的**读取语义**
    (``ColumnFixedString.get`` 本来就会剥掉尾部零字节,见
    :mod:`minich.columns.string`)。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 1)
        return get_data_type("String")

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
        col = full_column(args[0])
        out = ColumnString()
        for i in range(rows):
            text = ta.serialize_text(col.get(i))
            cut = text.find("\0")
            out.append(text if cut < 0 else text[:cut])
        return out


class FunctionToFixedString(FunctionBase):
    """``toFixedString(s, N)`` —— 转成定长 ``FixedString(N)``,不足补 0 字节。

    .. code-block:: sql

       SELECT toFixedString('ab', 4), length(toFixedString('ab', 4));
       -- ab  4        ← 补了两个 0 字节,所以长度是 4

    定长字符串的价值在**存储与比较**:没有 offsets 数组、比较是一次
    ``memcmp``、可以直接当哈希键用(见 :mod:`minich.columns.string` 里的
    ``ColumnFixedString``)。代价是**超长会报错**(``SIZE_OF_FIXED_STRING_DOESNT_MATCH``),
    而不是静默截断 —— 真实 CH 也一样。

    .. warning:: **mini-clickhouse 的降级**(策略 ②):``N`` 如果是**实参**
       (``toFixedString(s, 16)``),``get_return_type`` 看不到它的值,
       于是这里降级成"返回 ``String`` 且**不做补齐**"。

       * 声明类型与实际产生的类型**一致**(都是 String)—— 不会给出错类型的列;
       * 值与"剥掉尾部 0 字节的 FixedString"完全相同 —— 所以**打印结果一样**;
       * 差别在于类型:``length()`` 得到的是真实字符数而不是 N。

       想要精确的 ``FixedString(N)``,把 N 作为函数参数传入:
       ``get_function("toFixedString", params=[16])``。
    """

    def _n(self) -> Optional[int]:
        params = list(getattr(self, "params", []) or [])
        if len(params) == 1:
            return int(params[0])
        return None

    def _check(self, args: Sequence[Any]) -> None:
        """两种调用形式的参数个数校验。

        * 参数形式 ``get_function("toFixedString", params=[N])(s)`` → **1 个实参**;
        * 实参形式 ``toFixedString(s, N)`` → **2 个实参**(降级,见 docstring)。
        """
        check_arity(self.name, args, 1, 2)
        if self._n() is None and len(args) < 2:
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                f"{self.name} needs the length either as a function parameter "
                f"(get_function('{self.name}', params=[16])) or as a second argument",
            )

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        self._check(args)
        n = self._n()
        if n is None:
            # 降级:类型退化成 String(见 docstring 的 warning)
            return get_data_type("String")
        return get_data_type(f"FixedString({n})")

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        self._check(args)
        if any(a.type.is_nullable() for a in args):
            col, _ = execute_with_null_propagation(self, args, rows, context)
            return col
        ret = self.get_return_type([a.type for a in args])
        col = full_column(args[0])
        out = ret.create_column()
        for i in range(rows):
            # ColumnFixedString.append 负责补 0 字节 / 校验超长;
            # 降级到 String 时就是原样写入(见 docstring 里的说明)
            out.append(DataTypeString().serialize_text(col.get(i)))
        return out


_register("toFixedString", FunctionToFixedString, description="转换成 FixedString(N)")


@_register("toJSONString", description="把值序列化成 JSON 文本")
class FunctionToJSONString(FunctionBase):
    """``toJSONString(x)`` —— 把值序列化成 **JSON 文本**(类型 ``String``)。

    .. code-block:: sql

       SELECT toJSONString(1), toJSONString('a'), toJSONString([1,2]);
       -- 1   "a"   [1,2]

    注意字符串会**带引号**(它是 JSON 字符串),数字不带 ——
    这是它与 ``toString`` 的关键区别,也是"构造一个 JSON 字段"的正确起点::

       SELECT concat('{"v":', toJSONString(value), '}') FROM t;

    mini-clickhouse 用的是 :mod:`minich.util.json_text`(与 ``FORMAT JSONEachRow``
    同一个编码器),所以 ``nan``/``inf`` 也会像真实 CH 一样照原样输出。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 1)
        return get_data_type("String")

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
        from ..util.json_text import to_json_text

        ta = args[0].type
        col = full_column(args[0])
        out = ColumnString()
        for i in range(rows):
            value = col.get(i)
            if ta.is_numeric() or ta.is_enum():
                # 数值按 JSON 数字输出(1 → 1),不套引号
                out.append(to_json_text(_plain(value, ta)))
            elif ta.is_array() or ta.is_tuple() or ta.is_map() or ta.is_json():
                out.append(to_json_text(value))
            else:
                # 字符串/日期/UUID/IP/Decimal:先转成它自己的文本,再当 JSON 字符串
                out.append(to_json_text(ta.serialize_text(value)))
        return out


def _plain(value: Any, ta: DataType) -> Any:
    """把内部存储值还原成"人看到的值"(Decimal 按 scale 还原成 float)。"""
    if ta.is_decimal():
        return float(ta.to_decimal(value))
    return value


# ══════════════════════════════════════════════════════════════════════════
# ④ LowCardinality / toTypeName / 列名 / 常量性 / materialize
# ══════════════════════════════════════════════════════════════════════════


@_register("toLowCardinality", description="把参数包成 LowCardinality(字典编码)")
class FunctionToLowCardinality(FunctionBase):
    """``toLowCardinality(x)`` —— 把类型包成 ``LowCardinality(T)``(字典编码)。

    .. code-block:: sql

       SELECT toTypeName(toLowCardinality('a'));
       -- LowCardinality(String)

    什么时候值得用? **基数低 + 重复度高 + 频繁做等值比较/GROUP BY**
    (``method``、``country``、``status`` 这类列)。字典编码之后:

    * 比较的是**整数索引**而不是字符串(见 ``ColumnLowCardinality.hash_value``);
    * 哈希表的大小受**字典大小**限制,而不是行数。

    什么时候**不该**用? 高基数列(``user_id``)—— 字典本身会和原列一样大,
    还要多维护一份索引,反而更慢。

    .. warning:: ``LowCardinality(Nullable(T))`` 在 mini-clickhouse 里是**非法**类型
       (真实 CH 支持但内部表示不同),所以对可空参数调用本函数会抛
       ``ILLEGAL_TYPE_OF_ARGUMENT`` —— 该行为与 :class:`minich.types.composite.DataTypeLowCardinality`
       的约束一致。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 1)
        inner = unwrap_nullable(args[0])
        if inner.is_low_cardinality():
            return inner
        return DataTypeLowCardinality(inner)

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
        out = ret.create_column()
        for i in range(rows):
            out.append(col.get(i))
        return out


@_register("toTypeName", description="返回参数的静态类型名(String)")
class FunctionToTypeName(FunctionBase):
    """``toTypeName(x)`` —— 返回参数的**静态类型名**,类型 ``String``。

    .. code-block:: sql

       SELECT toTypeName(1), toTypeName(1 + 1), toTypeName(NULL), toTypeName([1, NULL]);
       -- UInt8   UInt16   Nullable(Nothing)   Array(Nullable(UInt8))

    它是"理解 ClickHouse 类型推导"的最短路径:**结果只取决于类型、与数据无关**,
    所以它能在不读一行数据的情况下告诉你这一列是什么类型。

    .. warning:: 两个坑,都写在实现里:

       1. **必须自己处理 NULL**:``toTypeName(NULL)`` 要返回 ``'Nullable(Nothing)'``,
          如果走默认的 NULL 传播,结果会变成 NULL —— 那这个函数就失去意义了。
          所以 ``propagate_nulls = False``,而且 ``Nullable`` 包装由基类
          (``execute_with_null_propagation``)负责,``get_return_type``
          只用 ``unwrap_nullable`` 后的类型 —— 两边配合才不会推出
          ``Nullable(Nullable(...))`` 这种非法类型。
       2. **它不能被"常量折叠掉"**:结果是一个常量(所有行一样),
          但**不能**当成"可以省略"—— 打印/导出时每一行都要有值。
          所以这里老老实实生成 ``rows`` 个值,而不是返回一个空列。
    """

    #: 需要看到 Nullable 参数本身,所以自己处理 NULL
    propagate_nulls = False
    return_type_cannot_be_nullable = True

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 1)
        return get_data_type("String")

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 1, 1)
        name = args[0].type.get_name()
        out = ColumnString()
        for _ in range(rows):
            out.append(name)
        return out


@_register("toColumnTypeName", description="返回参数在内存里的物化列类型名(String)")
class FunctionToColumnTypeName(FunctionBase):
    """``toColumnTypeName(x)`` —— 返回这一列**在内存里怎么存**(物化列的类型名)。

    .. code-block:: sql

       SELECT toColumnTypeName(1), toColumnTypeName('a'), toColumnTypeName(NULL);
       -- ColumnVector<UInt8>   ColumnString   ColumnNullable(ColumnVector<Nothing>)

    它与 :class:`FunctionToTypeName` 的区别,正好是
    ``DataType``(是什么类型)与 ``IColumn``(怎么存)的区别 ——
    这是整个引擎最重要的一对概念(见 :mod:`minich.columns.base` 的模块 docstring):

    .. code-block:: text

       toTypeName(1)          → UInt8                 "它是 UInt8"
       toColumnTypeName(1)    → ColumnVector<UInt8>   "它现在存在一个定长数组里"

    真实 ClickHouse 里这个函数是排查性能问题时的第一把武器:
    ``ColumnConst``(常量列)、``ColumnSparse``(稀疏列)、
    ``ColumnLowCardinality`` 的差别,在 ``toTypeName`` 里是看不出来的。

    .. warning:: 名称是**近似**的:真实 CH 的类名是 C++ 模板名
       (``ColumnVector<UInt8>``、``ColumnDecimal<Decimal64>``),
       mini-clickhouse 用 Python 的列类名映射过去,个别类型对不上,已尽量贴近。
    """

    propagate_nulls = False
    return_type_cannot_be_nullable = True

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 1)
        return get_data_type("String")

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 1, 1)
        name = _column_type_name(args[0].column, args[0].type)
        out = ColumnString()
        for _ in range(rows):
            out.append(name)
        return out


def _column_type_name(col: IColumn, type_: DataType) -> str:
    """把"物化列"翻译成真实 ClickHouse 的列类型名(近似)。"""
    if isinstance(col, ColumnConst):
        return f"ColumnConst({_column_type_name(col.data, unwrap_nullable(type_))})"
    if isinstance(col, ColumnNullable):
        return f"ColumnNullable({_column_type_name(col.nested, type_.nested)})"
    if isinstance(col, ColumnString):
        return "ColumnString"
    if isinstance(col, ColumnFixedString):
        return "ColumnFixedString"
    if isinstance(col, ColumnVector):
        return f"ColumnVector<{unwrap_nullable(type_).get_name()}>"
    from ..columns import (
        ColumnArray,
        ColumnBigInt,
        ColumnLowCardinality,
        ColumnMap,
        ColumnNothing,
        ColumnSparse,
        ColumnTuple,
        ColumnUUID,
    )

    if isinstance(col, ColumnBigInt):
        return f"ColumnVector<{unwrap_nullable(type_).get_name()}>"
    if isinstance(col, ColumnUUID):
        return "ColumnVector<UUID>"
    if isinstance(col, ColumnArray):
        return f"ColumnArray({_column_type_name(col.nested, type_.element)})"  # type: ignore[attr-defined]
    if isinstance(col, ColumnTuple):
        inner = ", ".join(
            _column_type_name(c, t) for c, t in zip(col.columns, type_.elements)  # type: ignore[attr-defined]
        )
        return f"ColumnTuple({inner})"
    if isinstance(col, ColumnMap):
        return "ColumnMap"
    if isinstance(col, ColumnLowCardinality):
        return f"ColumnLowCardinality({_column_type_name(col.dictionary, type_.nested)})"  # type: ignore[attr-defined]
    if isinstance(col, ColumnSparse):
        return "ColumnSparse"
    if isinstance(col, ColumnNothing):
        return "ColumnNothing"
    return type(col).__name__


@_register("isConstant", description="参数是不是常量列(UInt8)")
class FunctionIsConstant(FunctionBase):
    """``isConstant(x)`` —— 参数是不是**常量列**,返回 ``UInt8``。

    .. code-block:: sql

       SELECT isConstant(1), isConstant(number) FROM numbers(1);
       -- 1   0

    "常量列"(``ColumnConst``)是 ClickHouse 的第一层优化:
    它只存**一个值 + 行数**,而不是每行一份(见 ``ColumnConst`` 的 docstring)。
    在**主键/分区键裁剪**和**索引跳过**里,判断"这个表达式是不是常量"
    决定了能不能把整棵子树在查询开始前算完。

    .. note:: 它看的是"这一列**现在**怎么存",而不是"这个表达式的语义是不是常量":

       * 常量折叠**之后**才有 ``ColumnConst``(``1 + 1`` 折叠成常量 2);
       * ``any(x)`` 这种聚合出来的列是普通列,即使"所有值都一样"也不是常量列。

    所以 ``isConstant`` 的结果与**执行阶段**有关 —— 拿它做业务判断要小心。
    """

    propagate_nulls = False
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
        flag = 1 if args[0].column.is_const() else 0
        out = ColumnVector(np.uint8)
        out.append_many([flag] * rows)
        return out


@_register("materialize", description="把常量列展开成完整列")
class FunctionMaterialize(FunctionBase):
    """``materialize(x)`` —— 把**常量列展开**成真正的完整列(类型不变)。

    .. code-block:: sql

       SELECT materialize(1) FROM numbers(3), toColumnTypeName(materialize(1));
       -- 1 1 1   ColumnVector<UInt8>     ← 而不是 ColumnConst

    为什么需要它? 因为"常量列"虽然省内存,却会**关掉**某些优化:
    一个常量列参与 ``arrayJoin``、``ORDER BY`` 这些"就地改写列"的操作时,
    必须先展开(``convert_to_full_column``),否则行数会算错 ——
    这是真实 ClickHouse 里被修过不止一次的 bug 类型(见 ``ColumnConst`` 的 warning)。

    ``materialize`` 把这件事**显式化**:我就是要每行一份。

    .. warning:: 它是**性能相关的函数**,不是语义函数:``materialize(x)``
       与 ``x`` 在结果上完全一样(值一样、类型一样),只是内存布局不同。
       在 mini-clickhouse 里我们把列**复制**一份返回 —— 意味着调用者拿到的
       列与输入的列不再是同一个对象,后续处理器不会互相踩内存。
       真实 CH 是共享的(single-writer 约定),这里选择了更安全的做法。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 1)
        return args[0]

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 1, 1)
        col = args[0].column
        if col.is_const():
            return col.convert_to_full_column()
        return col.clone()


@_register("identity", description="原样返回参数(常用于阻止优化)")
class FunctionIdentity(FunctionBase):
    """``identity(x)`` —— 原样返回 ``x``(值与类型都不变)。

    .. code-block:: sql

       SELECT identity(1), toTypeName(identity(toUInt16(1)));
       -- 1   UInt16

    它有两个实用场景,都不是"算数":

    1. **阻止优化**。``identity(materialize(x))`` 之类组合可以挡住常量折叠
       (折叠会改变内存布局,有时你恰恰**不想**让它折叠);
    2. **调试表达式树**。想知道"表达式在某一层求值出来是什么类型、
       是不是常量"时,把它包一层再看 ``toTypeName``/``toColumnTypeName``。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 1)
        return args[0]

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 1, 1)
        return args[0].column


@_register("blockSize", description="当前块的期望行数(max_block_size)")
class FunctionBlockSize(FunctionBase):
    """``blockSize()`` —— 当前块的**期望行数**(即 ``max_block_size`` 设置)。

    .. code-block:: sql

       SELECT blockSize() FROM numbers(3);
       -- 65409   ← 本块的"标准大小",不是你拿到的行数

    这两者经常被搞混:

    .. code-block:: text

       blockSize()        = 这一块"最大能装多少行"(配置项 max_block_size)
       count() / length()  = 这一块"实际有几行"

    最后一个块通常**不满** —— 所以 ``blockSize()`` 报 65409、
    实际只有 3 行的情形很常见。它的真正用途是在**自定义聚合**
    (``AggregatingMergeTree`` 的状态大小、``uniq`` 的桶数)里
    按块规模预估内存。

    真实 ClickHouse 的默认 ``max_block_size`` 是 **65409** 行
    (不是一个整齐的 2 的幂 —— 这个数字是为了留出对齐与 padding 的余量);
    教程里常写的 65536 是"块的量级",两者别混。
    """

    return_type_cannot_be_nullable = True

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 0, 0)
        return get_data_type("UInt64")

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 0, 0)
        size = getattr(context, "block_size", None) or 65409
        out = ColumnVector(np.uint64)
        out.append_many([int(size)] * rows)
        return out


class FunctionDefaultValueOfTypeName(FunctionBase):
    """``defaultValueOfTypeName('T')`` —— 返回某个类型的**默认值**(零值)。

    .. code-block:: sql

       SELECT defaultValueOfTypeName('UInt8'), defaultValueOfTypeName('String');

    "默认值"就是每个类型的零值:数值 ``0``、字符串 ``''``、``Array`` 是 ``[]``、
    ``Nullable(T)`` 是 ``NULL``(见 :meth:`minich.types.base.DataType.default_value`)。
    它是 ``ALTER TABLE ADD COLUMN`` 补老数据、以及"缺列补全"时用的同一个值。

    .. warning:: **必须把类型名作为函数参数传入**(策略 ③):

       .. code-block:: python

          get_function("defaultValueOfTypeName", params=["UInt8"])

       因为"这个函数返回什么类型"取决于**那个字符串的值**,
       而 ``get_return_type`` 只能看到类型 —— 猜不出来就只能报错。
       (真实 ClickHouse 的解析器把类型名直接放进 AST 的参数里,
       所以那边看起来像普通实参,其实不是。)
    """

    return_type_cannot_be_nullable = True

    def _type_name(self) -> str:
        params = list(getattr(self, "params", []) or [])
        if len(params) != 1:
            raise_error(
                ErrorCode.NOT_IMPLEMENTED_IN_MINICH,
                _PARAMS_HINT.format(name=self.name, params=["UInt8"]),
            )
        return str(params[0]).strip().strip("'\"`")

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return get_data_type(self._type_name())

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        ret = self.get_return_type([])
        out = ret.create_column()
        for _ in range(rows):
            out.append(ret.default_value())
        return out


_register(
    "defaultValueOfTypeName",
    FunctionDefaultValueOfTypeName,
    description="返回某个类型的默认值(类型名作为函数参数传入)",
)


# ══════════════════════════════════════════════════════════════════════════
# ⑤ reinterpretAs* 族 —— 不动字节,只换解读方式
# ══════════════════════════════════════════════════════════════════════════

#: 目标名 → (目标类型名, 期望字节数)。``None`` 表示"字节数由源决定"(String)。
_REINTERPRET_TARGETS: Dict[str, Tuple[str, Optional[int]]] = {
    "UInt8": ("UInt8", 1),
    "UInt16": ("UInt16", 2),
    "UInt32": ("UInt32", 4),
    "UInt64": ("UInt64", 8),
    "Int8": ("Int8", 1),
    "Int16": ("Int16", 2),
    "Int32": ("Int32", 4),
    "Int64": ("Int64", 8),
    "Float32": ("Float32", 4),
    "Float64": ("Float64", 8),
    "String": ("String", None),
    "Date": ("Date", 2),
    "Date32": ("Date32", 4),
    "DateTime": ("DateTime", 4),
    "UUID": ("UUID", 16),
}


def _source_bytes(value: Any, type_: DataType) -> bytes:
    """取一个值在内存里的**小端字节**(与 x86 / 真实 ClickHouse 一致)。

    ``String`` 的字节就是它的 UTF-8(surrogateescape);数值是补码的小端表示。
    """
    if type_.is_string():
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value)
        return str(value).encode("utf-8", "surrogateescape")
    if type_.is_uuid():
        return int(value).to_bytes(16, "little", signed=False)
    layout = storage_layout(type_)
    if layout is None:
        raise_error(
            _ILLEGAL_TYPE,
            f"reinterpretAs cannot work on {type_.get_name()}",
        )
    bits, signed, is_float = layout
    if is_float:
        return struct.pack("<f" if bits == 32 else "<d", float(value))
    width = bits // 8
    return int(value).to_bytes(width, "little", signed=signed)


def _read_bytes(raw: bytes, type_: DataType) -> Any:
    """把一段小端字节按目标类型解读;字节不够就补 0,多了就**截断**。"""
    if type_.is_string():
        return raw.decode("utf-8", "surrogateescape")
    if type_.is_uuid():
        # UUID 是 128 位无符号整数,列里存的就是这个整数
        padded = raw[:16] + b"\0" * max(0, 16 - len(raw))
        return int.from_bytes(padded, "little", signed=False)
    layout = storage_layout(type_)
    if layout is None:
        raise_error(_ILLEGAL_TYPE, f"reinterpretAs cannot produce {type_.get_name()}")
    bits, signed, is_float = layout
    width = bits // 8
    padded = raw[:width] + b"\0" * max(0, width - len(raw))
    if is_float:
        return struct.unpack("<f" if bits == 32 else "<d", padded)[0]
    return int.from_bytes(padded, "little", signed=signed)


class _ReinterpretAs(FunctionBase):
    """``reinterpretAsXxx(x)`` —— **按位重新解读**:不转换值,只换"怎么读这些字节"。

    .. code-block:: sql

       SELECT reinterpretAsUInt8(toInt32(-1)),   -- 255   取最低 1 字节
              reinterpretAsUInt32(toInt32(-1)),  -- 4294967295  整个 32 位当无符号看
              reinterpretAsFloat32(toUInt32(1065353216));  -- 1.0  ← 1.0f 的位模式

    三条必须记住的规则:

    1. **小端**。``reinterpretAsString(toUInt32(1))`` 得到 ``'\\x01\\x00\\x00\\x00'``
       (x86 与真实 ClickHouse 都是小端);
    2. 目标比源**窄** → 取**低位**字节(丢掉高位);
       目标比源**宽** → 高位**补 0**(这是 mini 的明确选择:真实 CH 在
       C++ 层面越界读是 UB,我们给出确定的行为);
    3. 它**永远不会失败**(只要字节够)—— 这正是它和 ``toXxx`` 的本质区别。

    .. warning:: 真实 ClickHouse 的 ``reinterpretAs`` 对"目标比源宽"没有定义
       (UB),mini-clickhouse 规定"补 0"。另外**未实现**的目标类型有:
       ``reinterpretAsFixedString``。字符串**源**是支持的
       (``reinterpretAsUInt8('a')`` 得到 97 —— 取第一个字节)。
    """

    #: ``UInt8`` / ``Int32`` / ``Float64`` / ``String`` / ``Date`` / ``DateTime`` / ``UUID`` …
    target_name: str = "UInt8"

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 1)
        return get_data_type(self.target_name)

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
        src = args[0].type
        col = full_column(args[0])
        out = ret.create_column()
        for i in range(rows):
            out.append(_read_bytes(_source_bytes(col.get(i), src), ret))
        return out


for _suffix, (_tname, _width) in _REINTERPRET_TARGETS.items():
    _reinterpret_doc = f"""``reinterpretAs{_suffix}(x)`` —— 把 ``x`` 的**字节**按 ``{_tname}`` 重新解读。

.. code-block:: sql

   SELECT reinterpretAs{_suffix}(toUInt32(0)), reinterpretAs{_suffix}(toInt32(-1));

* 小端字节序;
* 目标是 ``{_tname}``({_width if _width else "长度由源决定"} 字节);
* 源更窄时高位补 0,更宽时只取低位字节;
* **不做任何数值转换**,所以永远不报错。

同类函数见 :class:`_ReinterpretAs` 的 docstring(小端 / 截断 / 补零三条规则)。
"""
    _klass = type(
        f"FunctionReinterpretAs{_suffix}",
        (_ReinterpretAs,),
        {"target_name": _tname, "__doc__": _reinterpret_doc},
    )
    _register(
        f"reinterpretAs{_suffix}",
        _klass,
        description=f"按位重新解读成 {_tname}",
    )
    globals()[_klass.__name__] = _klass
    __all__.append(_klass.__name__)


# ══════════════════════════════════════════════════════════════════════════
# ⑥ CAST / accurateCast —— 类型名来自"参数"(策略 ③)
# ══════════════════════════════════════════════════════════════════════════


class FunctionCast(FunctionBase):
    """``CAST(x, 'T')`` / ``_CAST`` —— 显式类型转换。

    .. code-block:: sql

       SELECT CAST(1, 'String'), CAST('123', 'UInt8'), CAST('2024-01-01', 'Date');

    ``CAST`` 是 SQL 标准里的写法,等价于直接调用 ``toT(x)``:
    真实 ClickHouse 的解析器会把 ``CAST(x AS T)``、``x::T`` 都改写成
    ``_CAST(x, 'T')`` 这一个内部函数。

    它和 ``toXxx`` 的区别只有一个,但很重要:

    .. code-block:: text

       CAST(x, 'UInt8')   ← 类型是**运行时**决定的(来自那个字符串)
       toUInt8(x)         ← 类型是**编译期**就定死的

    所以 ``CAST`` 更灵活(可以拼出类型名、可以在泛型代码里用),
    代价是多一层解析 —— 真实 ClickHouse 会在第一次调用后把解析结果缓存起来。

    .. warning:: **mini-clickhouse 的限制**(策略 ③):类型名必须作为
       **函数参数**传入:

       .. code-block:: python

          get_function("CAST", params=["UInt8"])     # 等价于 CAST(x, 'UInt8')

       原因是 ``FunctionBase.get_return_type`` 只能看到实参的**类型**,
       而"目标类型"是实参的**值** —— 拿不到值就只能报错,
       绝不会猜一个类型出来(猜错会让整列的**值**变成另一个东西)。
       真实 ClickHouse 的 ``getReturnTypeImpl`` 收的是列(能读到值),没有这个限制。

    转换语义与 ``toXxx`` 完全相同(整数回绕、浮点向零截断、
    ``Decimal`` 越界报错),因为两者最终都调用目标类型的 ``normalize``。
    """

    def _target(self) -> DataType:
        params = list(getattr(self, "params", []) or [])
        if len(params) != 1:
            raise_error(
                ErrorCode.NOT_IMPLEMENTED_IN_MINICH,
                _PARAMS_HINT.format(name=self.name, params=["UInt8"]),
            )
        text = str(params[0]).strip().strip("'\"`")
        return get_data_type(text)

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 2)
        return self._target()

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 1, 2)
        if args[0].type.is_nullable():
            col, _ = execute_with_null_propagation(self, args, rows, context)
            return col
        ret = self._target()
        col = full_column(args[0])
        out = ret.create_column()
        for i in range(rows):
            out.append(wrap_value(ret, ret.normalize(col.get(i))))
        return out


_register("CAST", FunctionCast, aliases=["_CAST"], description="显式类型转换(类型名作为参数)")


class FunctionAccurateCast(FunctionBase):
    """``accurateCast(x, 'T')`` —— **带范围检查**的类型转换。

    .. code-block:: text

       toUInt8(300)                 = 44        ← 回绕
       CAST(300, 'UInt8')           = 44        ← 一样,CAST 也回绕
       accurateCast(300, 'UInt8')   → 报错!     ← 装不下就拒绝

    这一条差异是整个类型系统里最容易吃亏的地方:静默回绕会**静默改数据**,
    而 ``accurateCast`` 把"这个值装不进去"变成一个**显式错误**。
    数据导入校验、跨表搬运(源列比目标列宽)时应该用它。

    ``accurateCastOrNull`` 是它的 ``-OrNull`` 版本:装不下返回 ``NULL``
    (类型变成 ``Nullable(T)``),适合"先标出来、事后再处理"的清洗流程。

    检查规则(与真实 CH 一致):

    * 整数目标:值必须落在 ``[min, max]`` 闭区间内(用**未回绕**的原值判断);
    * 浮点目标:整数转浮点要检查是否超过浮点能精确表示的范围(简化:只查溢出);
    * 字符串目标:任何值都能转(不会失败);
    * ``Decimal`` 目标:检查精度是否装得下(由 ``DataTypeDecimal.normalize`` 负责)。

    .. warning:: 与 ``CAST`` 一样,类型名必须作为**函数参数**传入(策略 ③)。
    """

    #: ``True`` = 失败返回 NULL(accurateCastOrNull),``False`` = 抛异常
    or_null = False

    propagate_nulls = True

    def _target(self) -> DataType:
        params = list(getattr(self, "params", []) or [])
        if len(params) != 1:
            raise_error(
                ErrorCode.NOT_IMPLEMENTED_IN_MINICH,
                _PARAMS_HINT.format(name=self.name, params=["UInt8"]),
            )
        text = str(params[0]).strip().strip("'\"`")
        return get_data_type(text)

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 2)
        target = self._target()
        if self.or_null and target.nullable_allowed:
            return DataTypeNullable(target)
        return target

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 1, 2)
        if args[0].type.is_nullable() and not self.or_null:
            col, _ = execute_with_null_propagation(self, args, rows, context)
            return col
        target = self._target()
        ret = self.get_return_type([args[0].type])
        src = unwrap_nullable(args[0].type)
        col = full_column(args[0])
        out = ret.create_column()
        for i in range(rows):
            value = col.get(i)
            if value is None:
                out.append(None if self.or_null else None)
                continue
            out.append(self._cast_one(value, src, target))
        return out

    def _cast_one(self, value: Any, src: DataType, target: DataType) -> Any:
        """先算"未回绕的真值",再检查它装不装得进目标类型。"""
        if target.is_numeric() and target.is_integer():
            if isinstance(value, str):
                try:
                    probe = float(value.strip()) if "." in value or "e" in value.lower() \
                        else int(value.strip(), 0)
                except ValueError:
                    return self._fail(value, target)
            elif isinstance(value, (int, float)):
                probe = value
            else:
                return self._fail(value, target)
            if isinstance(probe, float) and (math.isnan(probe) or math.isinf(probe)):
                return self._fail(value, target)
            if probe < target.min_value() or probe > target.max_value():
                return self._fail(value, target)
            return target.normalize(probe)
        try:
            return target.normalize(value)
        except MiniChError:
            return self._fail(value, target)

    def _fail(self, value: Any, target: DataType) -> Any:
        if self.or_null:
            return None
        raise_error(
            ErrorCode.ARGUMENT_OUT_OF_BOUND,
            f"Cannot convert {value!r} to {target.get_name()} in function {self.name}: "
            f"the value is out of range; use CAST (or the corresponding toXxx function) "
            f"if you want the wrapping behaviour of a plain cast",
        )


_register("accurateCast", FunctionAccurateCast, description="带范围检查的类型转换")


class FunctionAccurateCastOrNull(FunctionAccurateCast):
    """``accurateCastOrNull(x, 'T')`` —— ``accurateCast`` 的"装不下给 NULL"版本。

    .. code-block:: sql

       SELECT accurateCastOrNull(300, 'UInt8'), accurateCastOrNull(200, 'UInt8');
       -- NULL   200

    结果类型是 ``Nullable(T)``。它比 ``accurateCast`` 更适合**批量清洗**:

    .. code-block:: sql

       -- 先看看有多少行装不下,再决定是改列类型还是改数据
       SELECT countIf(isNull(accurateCastOrNull(raw, 'UInt8'))) FROM t;
    """

    or_null = True
    propagate_nulls = False


_register(
    "accurateCastOrNull",
    FunctionAccurateCastOrNull,
    description="带范围检查的类型转换(装不下给 NULL)",
)

"""杂项函数 —— ``now`` / ``bar`` / ``neighbor`` / ``runningDifference`` / ``version`` …

这个模块里的函数**没有共同点**,而这正是它的价值:它们恰好都展示了
"一个函数不只是数学公式"的那一面。

.. code-block:: text

   依赖查询上下文(不能用参数算出来)
     now() / today() / yesterday()     时间**每次调用**都可能不同
     currentDatabase() / currentUser() / currentQueryID()
     blockSize()                       当前块的期望行数(在 type_funcs 里)

   依赖**块内的位置**(不是"逐行的值")
     rowNumberInBlock()                这一行在本块里排第几
     neighbor(x, n)                    取"隔壁那一行"的同一个列
     runningDifference(x)              与上一行的差

   有副作用(不能常量折叠)
     sleep(seconds) / sleepEachRow(...) 真的会等
     throwIf(cond)                      真的会抛

   只是"画出来给人看"
     bar(x, min, max, width)            终端里的进度条

**为什么"依赖上下文"的函数都要走 ``context``?**
因为 ClickHouse 是一条查询跑在一个**上下文**里(数据库、用户、query_id、
当前时间、块大小)。这些值不是数据、也不是常量,而是"这次执行的现场"。
真实 ClickHouse 的 ``IExecutionContext`` 就是这个东西(它一路传给
``IFunction::executeImpl``、``ISource``、``IStorage``)。mini-clickhouse
用一条约定代替它:``execute(args, rows, context)`` 的 ``context``
可以带 ``now()`` / ``database`` / ``user`` / ``query_id`` / ``block_size``
这几个属性 —— 全都用 ``getattr`` 读,读不到就用**合理的默认值**,
于是"没有上下文也能跑"(测试里直接调函数时不必造一个假 Context)。

.. warning:: **不能常量折叠**的函数都显式声明了 ``deterministic = False``::

      now()      → 同一行查询里两次 now() 应该给出同一个时刻?
                   真实 ClickHouse 是**每次调用都取一次**,所以长查询里
                   前后两次 now() 可能相差几秒。把 now() 折叠成常量
                   反而会改变行为(见 base.py 的 is_deterministic 说明)。
      sleep()    → 折叠掉就等于不睡了,副作用消失。
      rand()     → 折叠掉就变成"所有行同一个随机数"(它在别的模块)。

.. warning:: 与真实 ClickHouse 的差异(逐条列在函数 docstring 里):

   1. ``rowNumberInAllBlocks`` 在 mini-clickhouse 里**与** ``rowNumberInBlock``
      相同(没有跨块的状态)。真实 CH 里它靠函数对象自己带的原子计数器递增。
   2. ``bar`` 的样式按题目要求做成"**填满 width 个字符**":不足的部分用
      ``░`` 补齐。真实 ClickHouse 的 ``UnicodeBar::render`` 只输出被填充的
      那一段(用 ``█`` 与 1/8 精度的局部方块 ``▏▎▍▌▋▊▉``),**不补空格**,
      所以 ``bar(5, 0, 10, 10)`` 在真实 CH 里是 5 个字符而不是 10 个。
      两种都"对",差别只在"画给人看时要不要对齐"。
   3. ``version()`` / ``hostName()`` / ``currentQueryID()`` 返回的是
      **本实现**的值,不冒充真实服务器的值。
"""

from __future__ import annotations

import datetime as _dt
import math
import socket
import time
from typing import Any, Sequence

import numpy as np

from ..block import ColumnWithTypeAndName
from ..columns import ColumnArray, ColumnString, ColumnVector, IColumn
from ..errors import ErrorCode, raise_error
from ..types import DataType, get_data_type
from ..types.composite import DataTypeArray
from .arithmetic import _register
from .arithmetic import (
    check_arity,
    coerce_value,
    common_supertype,
    full_column,
    unwrap_nullable,
    wrap_value,
)
from .base import FunctionBase, execute_with_null_propagation

__all__ = [
    "FunctionIsFinite",
    "FunctionIsInfinite",
    "FunctionIsNaN",
    "FunctionIgnore",
    "FunctionSleep",
    "FunctionSleepEachRow",
    "FunctionThrowIf",
    "FunctionBar",
    "FunctionUptime",
    "FunctionVersion",
    "FunctionHostName",
    "FunctionNow",
    "FunctionToday",
    "FunctionYesterday",
    "FunctionCurrentDatabase",
    "FunctionCurrentUser",
    "FunctionCurrentQueryID",
    "FunctionDisplayName",
    "FunctionNeighbor",
    "FunctionRunningDifference",
    "FunctionRowNumberInBlock",
    "FunctionReplicate",
]

#: 模块被导入的时刻 —— ``uptime()`` 的起点(真实 CH 里是服务端启动时刻)
_SERVER_START = time.time()

#: 真实 ClickHouse 的错误码 ``FUNCTION_THROW_IF_VALUE_IS_NON_ZERO`` = 395。
#: mini-clickhouse 的错误码表里没有它,所以直接用数字并标注来源。
_THROW_IF_NON_ZERO = 395

#: ``bar`` 用到的两种字符(Unicode 码点一并标注,方便你 `ord()` 校验)
_BAR_FILL = "█"      # █ FULL BLOCK
_BAR_EMPTY = "░"     # ░ LIGHT SHADE
#: 1/8 精度的局部方块 —— 真实 ClickHouse 的 UnicodeBar 用的就是这一组
_BAR_PARTIALS = "▏▎▍▌▋▊▉"  # ▏▎▍▌▋▊▉


# ══════════════════════════════════════════════════════════════════════════
# ① 浮点状态判定:isFinite / isInfinite / isNaN
# ══════════════════════════════════════════════════════════════════════════


class _FloatPredicate(FunctionBase):
    """``isFinite`` / ``isInfinite`` / ``isNaN`` 的公共骨架。

    三个函数都返回 ``UInt8``,而且都满足两条容易忽略的规则:

    1. **NULL 得到 0**(不是 NULL)。它们 ``propagate_nulls = False``,
       因为"这一列里有没有 NaN"本身就是个问题,NULL 不是 NaN,
       所以答案是确定的"没有";
    2. **整数永远满足 isFinite**(整数没有 inf/nan 的概念),
       而字符串会先试着解析成浮点。
    """

    propagate_nulls = False
    return_type_cannot_be_nullable = True

    #: ``finite`` / ``infinite`` / ``nan``
    mode: str = "finite"

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
        ta = args[0].type
        col = full_column(args[0])
        out = ColumnVector(np.uint8)
        for i in range(rows):
            value = col.get(i)
            out.append(self.judge(value, unwrap_nullable(ta)))
        return out

    def judge(self, value: Any, type_: DataType) -> int:
        if value is None:
            return 0  # NULL 不是 NaN、不是 inf、也不是"有限"
        if type_.is_string():
            try:
                value = float(value)
            except ValueError:
                return 0
        if not isinstance(value, float):
            if self.mode == "finite":
                return 1  # 整数一定是有限的
            return 0
        if self.mode == "nan":
            return 1 if math.isnan(value) else 0
        if self.mode == "infinite":
            return 1 if math.isinf(value) else 0
        return 1 if math.isfinite(value) else 0


@_register("isFinite", description="是否是有限浮点数(不是 inf/nan)")
class FunctionIsFinite(_FloatPredicate):
    """``isFinite(x)`` —— 是有限值(既不是 ``inf`` 也不是 ``nan``)返回 1。

    .. code-block:: sql

       SELECT isFinite(1), isFinite(1 / 0), isFinite(0 / 0), isFinite(1.5);
       -- 1   0   0   1

    它是**清洗浮点数据的第一道闸门**:``1 / 0`` 在 ClickHouse 里不报错而是
    得到 ``inf``(见 :class:`minich.functions.arithmetic.FunctionDivide`),
    于是求和、平均都会被污染。标准做法::

       -- 先把非法值挡掉,再算
       SELECT avgIf(x, isFinite(x)) FROM t;
       -- 或者先把它们标出来看看规模
       SELECT countIf(NOT isFinite(x)) FROM t;
    """

    mode = "finite"


@_register("isInfinite", description="是否是无穷(inf/-inf)")
class FunctionIsInfinite(_FloatPredicate):
    """``isInfinite(x)`` —— 是 ``inf`` 或 ``-inf`` 返回 1。

    .. code-block:: sql

       SELECT isInfinite(1 / 0), isInfinite(-1 / 0), isInfinite(nan);
       -- 1   1   0     ← nan 不是无穷,它连"大小"都没有
    """

    mode = "infinite"


@_register("isNaN", description="是否是 NaN(不是数字)")
class FunctionIsNaN(_FloatPredicate):
    """``isNaN(x)`` —— 是 ``nan`` 返回 1。

    .. code-block:: sql

       SELECT isNaN(0 / 0), isNaN(nan), isNaN(log(-1));
       -- 1   1   1

    ``nan``(Not a Number)来自:``0/0``、``inf - inf``、``sqrt(-1)``、``log(-1)``、
    ``asin(2)`` 等等。它的诡异之处是 ``nan != nan``(**不等于它自己**)——
    所以判 NaN 只能靠 ``isNaN``,不能靠比较(见
    :class:`minich.functions.comparison.FunctionEquals` 的说明)。
    """

    mode = "nan"


# ══════════════════════════════════════════════════════════════════════════
# ② 有副作用的函数:ignore / sleep / sleepEachRow / throwIf
# ══════════════════════════════════════════════════════════════════════════


@_register("ignore", description="吞掉参数并返回 0(UInt8)")
class FunctionIgnore(FunctionBase):
    """``ignore(x, ...)`` —— 把参数的**求值**做完,然后丢掉,返回 ``UInt8`` 0。

    .. code-block:: sql

       SELECT ignore(throwIf(0), 1 + 1);
       -- 0

    它存在的意义是"我要这个表达式**被求值**,但不需要它的结果":

    * 测试/压测时想让某个表达式真的跑起来(而不是被优化掉);
    * 把带副作用的表达式(``sleep``)塞进 ``WHERE`` 之类必须返回布尔的上下文。

    真实 ClickHouse 的 ``ignore`` 是变参、且返回值固定为 0 ——
    因为它的契约就是"有输入、没输出"。
    """

    return_type_cannot_be_nullable = True

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 0, None)
        return get_data_type("UInt8")

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 0, None)
        out = ColumnVector(np.uint8)
        out.append_many([0] * rows)
        return out


class _Sleep(FunctionBase):
    """``sleep(seconds)`` / ``sleepEachRow(seconds)`` 的公共骨架。

    .. warning:: 它们是**非确定性**函数(``deterministic = False``):
       常量折叠会把 ``sleep(1)`` 算成一个常量,副作用就消失了 ——
       而副作用正是它存在的**唯一**理由。
    """

    deterministic = False
    return_type_cannot_be_nullable = True

    #: ``True`` = 每一行都睡一次(``sleepEachRow``)
    each_row = False

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
        col = full_column(args[0])
        if self.each_row:
            for i in range(rows):
                _sleep_seconds(col.get(i))
        else:
            # 一个参数、睡一次 —— 只读第 0 行(真实 CH 要求它是常量)
            _sleep_seconds(col.get(0))
        out = ColumnVector(np.uint8)
        out.append_many([0] * rows)
        return out


def _sleep_seconds(value: Any) -> None:
    seconds = float(value)
    if seconds > 0:
        time.sleep(min(seconds, 60.0))  # 上限 60 秒,防止教学环境里误sleep 一小时


@_register("sleep", description="等待若干秒(有副作用,不可折叠)")
class FunctionSleep(_Sleep):
    """``sleep(seconds)`` —— 阻塞 ``seconds`` 秒,返回 ``UInt8`` 0。

    .. code-block:: sql

       SELECT sleep(0.1), count() FROM numbers(3);

    真实用途只有两类:压测时**人为制造慢查询**(验证超时/告警/队列),
    以及在分布式测试里制造时序差。生产查询里出现 ``sleep`` 一定是 bug。

    .. warning:: 参数必须是常量(整列只看第 0 行),而且 mini-clickhouse
       给它加了 **60 秒上限** —— 教学环境里 ``sleep(3600)`` 会把课堂拖死。
    """

    each_row = False


@_register("sleepEachRow", description="每一行等待一次(有副作用,不可折叠)")
class FunctionSleepEachRow(_Sleep):
    """``sleepEachRow(seconds)`` —— **每一行**都睡 ``seconds`` 秒。

    .. code-block:: sql

       SELECT sleepEachRow(number) FROM numbers(3);   -- 睡 0+1+2 = 3 秒

    它和 ``sleep`` 的差别不只是"睡几次",而是**睡多久取决于数据** ——
    于是它可以用来模拟"处理时间随数据量变化"的服务。
    代价是:一张 1000 万行的表上 ``sleepEachRow(0.001)`` 要跑 3 小时。

    .. warning:: 同样有 60 秒/行的上限,且是**非确定性**函数。
    """

    each_row = True


@_register("throwIf", description="参数非 0 时抛异常")
class FunctionThrowIf(FunctionBase):
    """``throwIf(cond)`` —— ``cond`` 非 0 就**抛异常**,否则返回 ``UInt8`` 0。

    .. code-block:: sql

       SELECT throwIf(number > 2) FROM numbers(3);
       -- Code: 395. DB::Exception: Value passed to 'throwIf' function is non-zero.
       -- (FUNCTION_THROW_IF_VALUE_IS_NON_ZERO)

    它是 SQL 里的 ``assert``:数据校验、ETL 的前置断言、测试用例里
    "这一步**必须**不成立"的写法都靠它。结合 ``if`` 的惰性求值还能做到
    "只在某个条件下才断言"::

       SELECT throwIf(isNull(x) AND expected_not_null), ... FROM t;
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
        col = full_column(args[0])
        for i in range(rows):
            if col.get(i):
                raise_error(
                    _THROW_IF_NON_ZERO,
                    f"Value passed to 'throwIf' function is non-zero at row {i}",
                )
        out = ColumnVector(np.uint8)
        out.append_many([0] * rows)
        return out


# ══════════════════════════════════════════════════════════════════════════
# ③ bar —— 终端里的进度条
# ══════════════════════════════════════════════════════════════════════════


@_register("bar", description="画一条与 (x - min)/(max - min) 成比例的进度条")
class FunctionBar(FunctionBase):
    """``bar(x, min, max[, width])`` —— 画一条宽度是 ``width`` 个字符的进度条。

    .. code-block:: sql

       SELECT bar(5, 0, 10, 10), bar(0, 0, 10, 10), bar(10, 0, 10, 10);
       -- █████░░░░░   ░░░░░░░░░░   ██████████

    实现上就是一次比例换算 + 两种字符:

    .. code-block:: text

       ratio  = (x - min) / (max - min)     然后夹到 [0, 1]
       填充数 = ratio * width
         ├─ 整数部分 → '█'(U+2588 FULL BLOCK)
         └─ 余下的 1/8 → '▏▎▍▌▋▊▉'(1/8 精度的局部方块,真实 CH 也用这组)
       其余位置    → '░'(U+2591 LIGHT SHADE)
       最后**一定**是 width 个字符 —— 于是多行输出天然对齐

    ``.. warning::`` 这里与真实 ClickHouse 有一个**明确的偏离**:
    真实 CH 的 ``UnicodeBar::render`` **只输出被填充的那一段**,
    不补任何字符(所以 ``bar(5, 0, 10, 10)`` 在真实 CH 里是 5 个字符,
    对齐靠输出格式器去做)。mini-clickhouse 按本题要求补满 ``width`` 个字符,
    好处是**在终端里直接看就对齐**。两种做法都能用,但要清楚差别 ——
    如果你把这里的输出喂给别的程序解析,请以"长度为 width"为准。

    负区间(``min > max``)也能用:比例公式对调之后方向自然反过来
    (真实 CH 对负区间有专门处理,细节略有差异)。
    """

    return_type_cannot_be_nullable = True

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 3, 4)
        return get_data_type("String")

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 3, 4)
        if any(a.type.is_nullable() for a in args):
            col, _ = execute_with_null_propagation(self, args, rows, context)
            return col
        x_col = full_column(args[0])
        lo_col = full_column(args[1])
        hi_col = full_column(args[2])
        width = _const_int(args[3], default=80) if len(args) > 3 else 80
        out = ColumnString()
        for i in range(rows):
            out.append(
                render_bar(float(x_col.get(i)), float(lo_col.get(i)), float(hi_col.get(i)), width)
            )
        return out


def _const_int(arg: ColumnWithTypeAndName, default: int) -> int:
    """读一个"必须是常量"的整数参数(与 ``rounding`` 里的同类校验一致)。"""
    col = full_column(arg)
    if col.size() == 0:
        return default
    first = int(col.get(0))
    for i in range(1, col.size()):
        if int(col.get(i)) != first:
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                "The last argument of function bar must be constant",
            )
    return first


def render_bar(x: float, lo: float, hi: float, width: int) -> str:
    """把 ``x`` 在 ``[lo, hi]`` 里的位置画成 ``width`` 个字符。

    单独抽成函数,是因为它是**纯计算**,可以被测试与复用 —— 真实 ClickHouse
    也把它放在 ``Common/UnicodeBar.cpp`` 里,而不是塞在函数实现内部。
    """
    if width <= 0:
        return ""
    span = hi - lo
    if span == 0:
        ratio = 1.0 if x >= hi else 0.0
    else:
        ratio = (x - lo) / span
    ratio = max(0.0, min(1.0, ratio))
    filled = ratio * width
    whole = int(filled)
    remainder = filled - whole
    bar = _BAR_FILL * whole
    if whole < width:
        eighths = int(remainder * 8)
        if eighths > 0:
            bar += _BAR_PARTIALS[eighths - 1]
    return bar + _BAR_EMPTY * max(0, width - len(bar))


# ══════════════════════════════════════════════════════════════════════════
# ④ 服务端信息:uptime / version / hostName
# ══════════════════════════════════════════════════════════════════════════


class _ConstantStringFunction(FunctionBase):
    """"每次调用都返回同一个字符串"的函数骨架(``version`` / ``hostName``)。

    它们与 ``now()`` 的区别是:值虽然是**运行期**才知道的,但**一次运行内不变**,
    所以可以当常量用(可以参与常量折叠) —— 真实 ClickHouse 里
    ``hostName()`` 就是这样被折叠的。
    """

    return_type_cannot_be_nullable = True

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 0, 0)
        return get_data_type("String")

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 0, 0)
        value = self.value(context)
        out = ColumnString()
        out.append_many([value] * rows)
        return out

    def value(self, context: Any = None) -> str:
        raise_error(ErrorCode.NOT_IMPLEMENTED, f"{self.name}: value is not implemented")


@_register("uptime", description="服务已运行秒数")
class FunctionUptime(FunctionBase):
    """``uptime()`` —— 服务端已运行的秒数(``UInt32``)。

    .. code-block:: sql

       SELECT uptime(), uptime() / 3600 AS hours;

    在 mini-clickhouse 里"服务端"没有进程的概念,所以起点取的是
    **本模块被导入的时刻**(即 ``import minich.functions`` 的时间)。
    真实 ClickHouse 返回的是 ``Server``/``Keeper`` 启动到现在的秒数。

    .. warning:: 它是**非确定性**的(``deterministic = False``):两次调用之间
       时间在流逝,所以不能被折叠成常量 —— 这与 ``now()`` 是同一个道理。
    """

    deterministic = False
    return_type_cannot_be_nullable = True

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 0, 0)
        return get_data_type("UInt32")

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 0, 0)
        seconds = int(time.time() - _SERVER_START) & 0xFFFFFFFF
        out = ColumnVector(np.uint32)
        out.append_many([seconds] * rows)
        return out


@_register("version", description="服务端版本号")
class FunctionVersion(_ConstantStringFunction):
    """``version()`` —— 服务端版本号(``String``),形如 ``25.3.1.1``。

    mini-clickhouse 返回的是 ``minich.TARGET_CLICKHOUSE_VERSION`` 拼出来的
    版本串 —— 也就是"我们对标的是哪个版本",而不是某个真实构建号。
    真实 ClickHouse 返回 ``VERSION_STRING``(编译期常量)。

    .. warning:: 别拿它做兼容性判断。它想表达的是"**教程里的行为描述
       以这个版本为准**"(见 ``minich/__init__.py`` 里的
       ``TARGET_CLICKHOUSE_VERSION``),而不是"这台服务器装了什么"。
    """

    def value(self, context: Any = None) -> str:
        from .. import TARGET_CLICKHOUSE_VERSION

        return f"{TARGET_CLICKHOUSE_VERSION}.1.1"


@_register("hostName", description="服务端主机名")
class FunctionHostName(_ConstantStringFunction):
    """``hostName()`` —— 服务端主机名(``String``)。

    在分布式表/副本的排查里它极其常用:找出"这些数据到底是**哪台机器**写的"::

       SELECT hostName(), count() FROM distributed_table GROUP BY 1;

    取不到主机名时(某些容器环境)退化成 ``'localhost'``,而不是抛错 ——
    因为它只是**辅助信息**,不该让查询挂掉。
    """

    def value(self, context: Any = None) -> str:
        name = getattr(context, "host_name", None)
        if name:
            return str(name)
        try:
            return socket.gethostname() or "localhost"
        except OSError:
            return "localhost"


# ══════════════════════════════════════════════════════════════════════════
# ⑤ 查询上下文:now / today / yesterday / currentDatabase / currentUser / currentQueryID
# ══════════════════════════════════════════════════════════════════════════


def _context_now(context: Any) -> _dt.datetime:
    """取"本次查询的当前时刻" —— 上下文给了就用它,否则用系统时间。

    ``context.now`` 既可能是**方法**(真实 CH 的 ``IExecutionContext`` 里
    时间是一个被缓存的 ``DateTime``,取一次就一直用),也可能直接是
    一个 ``datetime``。两种都接受,读不到就用 ``datetime.now()``。
    """
    raw = getattr(context, "now", None)
    if raw is not None:
        value = raw() if callable(raw) else raw
        if isinstance(value, _dt.datetime):
            return value
    return _dt.datetime.now()


def _context_str(context: Any, attr: str, default: str) -> str:
    value = getattr(context, attr, None)
    if value is None:
        return default
    return str(value)


@_register("now", description="当前时间(DateTime,非确定性)")
class FunctionNow(FunctionBase):
    """``now()`` —— 当前时间,类型 ``DateTime``。

    .. code-block:: sql

       SELECT now(), toTypeName(now()), now() - toDateTime('2024-01-01 00:00:00');

    两个容易忽略的点:

    1. **它是非确定性函数**(``deterministic = False``)—— 绝对不能被常量折叠。
       如果折叠了,一条跑 10 分钟的查询里前后两个 ``now()`` 会变成同一个时刻,
       看起来"更一致",实际上与真实行为不符(真实 CH 每次调用都取一次,
       所以同一查询里两次 ``now()`` 可能相差几秒);
    2. **它是 UTC 秒**:``DateTime`` 列里存的是 epoch 秒,时区是**列类型的属性**
       (见 :class:`minich.types.scalars.DataTypeDateTime`),所以
       ``toDateTime(now(), 'Asia/Shanghai')`` 这种"转换时区"其实是换类型,
       不是换算数值。

    .. warning:: mini-clickhouse 的 ``now()`` 不接受时区参数
       (真实 CH 支持 ``now('Asia/Shanghai')``)。原因是"结果类型取决于参数值"
       这件框架限制(见 :mod:`minich.functions.type_funcs` 的模块 docstring)——
       时区是值,而 ``get_return_type`` 看不到值。
    """

    deterministic = False
    return_type_cannot_be_nullable = True

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 0, 0)
        return get_data_type("DateTime")

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 0, 0)
        seconds = int(_context_now(context).replace(tzinfo=None).timestamp())
        out = ColumnVector(np.uint32)
        out.append_many([seconds & 0xFFFFFFFF] * rows)
        return out


class _TodayBase(FunctionBase):
    """``today()`` / ``yesterday()`` 的公共骨架 —— 都返回 ``Date``(天数)。"""

    deterministic = False
    return_type_cannot_be_nullable = True

    #: 相对今天的天数偏移:``today`` 是 0,``yesterday`` 是 -1
    day_offset: int = 0

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 0, 0)
        return get_data_type("Date")

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 0, 0)
        base = _context_now(context).date() + _dt.timedelta(days=self.day_offset)
        days = (base - _dt.date(1970, 1, 1)).days
        out = ColumnVector(np.uint16)
        out.append_many([days & 0xFFFF] * rows)
        return out


@_register("today", description="今天的日期(Date)")
class FunctionToday(_TodayBase):
    """``today()`` —— 今天的日期,类型 ``Date``(内部是"自 1970-01-01 起的天数")。

    .. code-block:: sql

       SELECT today(), toTypeName(today()), today() - toDate('2024-01-01');

    与 ``now()`` 的关系是"取日期部分",但注意:**它按服务器时区算**,
    而不是 UTC —— 跨时区部署时"今天"是什么时候开始,取决于服务器配置。
    这是日报类查询里最常见的差一天问题。
    """

    day_offset = 0


@_register("yesterday", description="昨天的日期(Date)")
class FunctionYesterday(_TodayBase):
    """``yesterday()`` —— 昨天的日期,类型 ``Date``。

    .. code-block:: sql

       SELECT yesterday(), today() - yesterday();
       -- 2024-05-31   1

    它是"日期分区表"里最常用的过滤条件之一:``WHERE day = yesterday()``。
    """

    day_offset = -1


class _ContextString(FunctionBase):
    """``currentDatabase`` / ``currentUser`` / ``currentQueryID`` 的公共骨架。"""

    return_type_cannot_be_nullable = True

    #: 上下文里的属性名
    context_attr: str = "database"

    #: 取不到时的默认值
    default: str = "default"

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 0, 0)
        return get_data_type("String")

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 0, 0)
        value = _context_str(context, self.context_attr, self.default)
        out = ColumnString()
        out.append_many([value] * rows)
        return out


@_register("currentDatabase", description="当前数据库名(String)")
class FunctionCurrentDatabase(_ContextString):
    """``currentDatabase()`` —— 当前数据库名(``String``)。

    完整表名由"数据库 + 表"两段组成,而 ``currentDatabase()`` 是**后半段
    省略时的前半段**:``SELECT * FROM hits`` 里的 ``hits`` 其实是
    ``<currentDatabase()>.hits``。所以它经常出现在
    "拼出完整表名"的元数据查询里。
    """

    context_attr = "database"
    default = "default"


@_register("currentUser", description="当前用户名(String)")
class FunctionCurrentUser(_ContextString):
    """``currentUser()`` —— 当前用户名(``String``)。

    它是**权限判断**的入口(``system.grants`` 里的每一行都挂着用户),
    也是审计日志里的必填项。mini-clickhouse 取不到上下文时返回 ``'default'`` ——
    与真实 ClickHouse 的默认用户同名。
    """

    context_attr = "user"
    default = "default"


@_register("currentQueryID", description="当前 query_id(String)")
class FunctionCurrentQueryID(_ContextString):
    """``currentQueryID()`` —— 当前查询的 ``query_id``(``String``)。

    真实 ClickHouse 给每条查询一个 UUID 形式的 id,它出现在:

    * ``system.query_log`` 的每一行里(从而能和客户端日志对上);
    * ``system.processes`` 的 ``query_id`` 列(从而能 ``KILL QUERY WHERE``);
    * 客户端收到的每个 Block 的附加信息里(从而能做端到端追踪)。

    所以它是"把一次查询的日志串起来"的钥匙。取不到上下文时返回空串。
    """

    context_attr = "query_id"
    default = ""


@_register("displayName", description="当前用户的显示名(String)")
class FunctionDisplayName(_ContextString):
    """``displayName([x])`` —— 当前用户的**显示名**(``String``)。

    .. warning:: 真实 ClickHouse 里 ``displayName`` 的语义与
       ``IUser`` 的显示名有关、并不算一个"标准 SQL 函数",不同版本的表现
       也不一致。mini-clickhouse 把它实现成"返回当前会话的显示名,
       取不到就退化成用户名",并且**允许一个可选参数**(这样
       ``displayName()`` 与 ``displayName(x)`` 都能解析)——
       属于"有意的宽松",列出以示区别。
    """

    context_attr = "user"
    default = "default"

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 0, 1)
        return get_data_type("String")

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 0, 1)
        value = _context_str(context, "display_name", "") or _context_str(
            context, self.context_attr, self.default
        )
        out = ColumnString()
        out.append_many([value] * rows)
        return out


# ══════════════════════════════════════════════════════════════════════════
# ⑥ 块内位置:nighbor / runningDifference / rowNumberInBlock
# ══════════════════════════════════════════════════════════════════════════


@_register("neighbor", description="取同一列里相邻行的值")
class FunctionNeighbor(FunctionBase):
    """``neighbor(x[, offset[, default]])`` —— 取"隔壁那一行"的同一个值。

    .. code-block:: sql

       SELECT number, neighbor(number) AS next, neighbor(number, -1) AS prev
       FROM numbers(4);
       -- 0  1  NULL?      ← 越界的那一端返回**默认值**(0)
       -- 1  2  0
       -- 2  3  1
       -- 3  0  2

    ``offset`` 默认是 **1**(下一行),负数就是上一行。越界时返回
    ``default``(给了第三个参数就用它,否则用该类型的**零值**)——
    **不报错**,因为它天然用于"算与相邻行的差",边界上总得有东西。

    真实 CH 里它有几个硬性要求(这里也照做):``offset`` 必须是常量,
    而且它**只在当前块内有效** —— 跨块时"上一行"其实在**另一个块**里,
    所以 ``neighbor`` 的结果在块的边界上是不完整的。这条限制是
    ``LIMIT BY`` / ``runningDifference`` 这类函数的共同底色。

    .. warning:: 参数个数:mini-clickhouse 支持 1~3 个参数;
       真实 CH 还允许"按分区取邻居"的重载(需要窗口函数框架),
       这里没有实现。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 3)
        result = args[0]
        if len(args) == 3:
            result = common_supertype(unwrap_nullable(args[0]), unwrap_nullable(args[2]))
        return result

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 1, 3)
        if any(a.type.is_nullable() for a in args):
            col, _ = execute_with_null_propagation(self, args, rows, context)
            return col
        ret = self.get_return_type([a.type for a in args])
        col = full_column(args[0])
        offset = _const_int(args[1], default=1) if len(args) > 1 else 1
        has_default = len(args) > 2
        default_col = full_column(args[2]) if has_default else None
        out = ret.create_column()
        for i in range(rows):
            j = i + offset
            if 0 <= j < rows:
                out.append(coerce_value(ret, col.get(j)))
            elif has_default:
                out.append(coerce_value(ret, default_col.get(i)))  # type: ignore[union-attr]
            else:
                out.append(coerce_value(ret, ret.default_value()))
        return out


class _RunningDifferenceBase(FunctionBase):
    """``runningDifference`` / ``runningDifferenceStartingWithFirstValue`` 的公共骨架。

    两者都是"与上一行的差",区别只在**第一行**:

    .. code-block:: text

       x        =  10  12  15  11
       runningDifference(x)                      第一行 = 0  →  0  2  3  -4
       runningDifferenceStartingWithFirstValue   第一行 = 10 → 10  2  3  -4

    为什么第一行要特殊处理? 因为"没有上一行"这个状态必须有个表示 ——
    用 0(表示"差是 0")还是用首值(表示"从它自己开始累加")取决于用途:
    画累计曲线用后者,算增量用前者。
    """

    #: ``True`` = 第一行取自己的值(``...StartingWithFirstValue``)
    start_with_first: bool = False

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
        if args[0].type.is_nullable():
            col, _ = execute_with_null_propagation(self, args, rows, context)
            return col
        ret = self.get_return_type([args[0].type])
        col = full_column(args[0])
        out = ret.create_column()
        prev: Any = None
        for i in range(rows):
            value = col.get(i)
            if prev is None:
                out.append(
                    coerce_value(ret, value) if self.start_with_first
                    else coerce_value(ret, 0)
                )
            else:
                out.append(wrap_value(ret, value - prev))
            prev = value
        return out


@_register("runningDifference", description="与上一行的差(第一行给 0)")
class FunctionRunningDifference(_RunningDifferenceBase):
    """``runningDifference(x)`` —— ``x[i] - x[i-1]``,第一行是 **0**。

    .. code-block:: sql

       SELECT number, runningDifference(number) FROM numbers(4);
       -- 0 0 / 1 1 / 2 1 / 3 1

    ...它其实是"差分的差分的和"那一族的基础(``runningAccumulate`` /
    ``runningConcurrency`` 都建立在"逐行的窗口"这个抽象上)。

    .. warning:: 结果类型**与参数相同**,于是无符号列上会出现意料之外的回绕:
       ``runningDifference`` 一个 ``UInt8`` 列,``5 → 3`` 得到 ``254``
       而不是 ``-2``。真实 ClickHouse 也是这样(它不替你做类型提升)。
       要负数就用 ``toInt32(x)`` 先转一下。
    """

    start_with_first = False


@_register("runningDifferenceStartingWithFirstValue", description="与上一行的差(第一行给首值)")
class FunctionRunningDifferenceStartingWithFirstValue(_RunningDifferenceBase):
    """``runningDifferenceStartingWithFirstValue(x)`` —— 同上,但**第一行取自己的值**。

    这个变体的名字长到离谱,原因是它在真实 ClickHouse 里是**改行为**的
    兼容性开关:老版本 ``runningDifference`` 的第一行就是首值,后来改成 0,
    于是新的语义被开成了一个新名字。这种"改名不改语义"的做法在数据库里很常见
    (修 bug 时最省事、也最不伤用户)。
    """

    start_with_first = True


@_register("rowNumberInBlock", description="当前行在块内的序号(从 0 开始)")
class FunctionRowNumberInBlock(FunctionBase):
    """``rowNumberInBlock()`` —— 这一行在**当前块**里的序号(从 0 开始,``UInt64``)。

    .. code-block:: sql

       SELECT rowNumberInBlock() FROM numbers(3);
       -- 0 1 2

    ...看起来和 ``number`` 一样,其实完全不同 —— ``number`` 是**表级别的行号**
    (由存储层提供),而 ``rowNumberInBlock`` 是**块内**的偏移:

    .. code-block:: text

       一个 100 万行的表,按 65409 行切块:
         number           0 … 65408 | 65409 … 131817 | …
         rowNumberInBlock 0 … 65408 | 0     … 65408  | …   ← 每块都从 0 开始

    它在真实实现里有硬用途:某些向量化代码需要"本块内的位置"来做
    ``arrayJoin`` 的反向定位与稀疏编码(见 ``ColumnSparse`` 的 offsets)。
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
        return ColumnVector(np.uint64, list(range(rows)))


@_register("rowNumberInAllBlocks", description="当前行在所有块连起来之后的序号")
class FunctionRowNumberInAllBlocks(FunctionRowNumberInBlock):
    """``rowNumberInAllBlocks()`` —— 跨块累加的行号。

    .. warning:: **mini-clickhouse 与真实 ClickHouse 的差异**:真实 CH 的
       ``rowNumberInAllBlocks`` 在函数对象里带一个**递增计数器**,
       于是第二个块从 65409 继续数;而 mini-clickhouse 的函数实例是
       "每次查询新建、每次调用无状态",拿不到"这是第几个块",
       所以这里**与** ``rowNumberInBlock()`` 完全一样(每块都从 0 开始)。

       要跨块累加,请在查询层用 ``sum(count()) OVER ()`` 之类的写法自己算 ——
       这也是真实 CH 官方更推荐的做法(它把状态从函数里挪到了窗口框架)。
    """


# ══════════════════════════════════════════════════════════════════════════
# ⑦ replicate —— 把标量"摊平"成与数组等长的数组
# ══════════════════════════════════════════════════════════════════════════


@_register("replicate", description="把第一个参数重复成与第二个数组等长的数组")
class FunctionReplicate(FunctionBase):
    """``replicate(x, arr)`` —— 造一个长度与 ``arr`` 相同、**每个元素都是 x** 的数组。

    .. code-block:: sql

       SELECT replicate(1, ['a','b','c']), replicate('z', [1,2]);
       -- [1,1,1]   ['z','z']

    它是 ``arrayMap`` 的"反向工具":``arrayMap(i -> f(x), arr)`` 只关心
    "循环几次"而完全不用 ``arr`` 的值时,写成 ``replicate`` 更直白
    (也省掉了一层 lambda 的实参开销)。

    真实 CH 的签名是 ``replicate(x, arr)``:``x`` 是**标量**,``arr`` 决定长度。
    结果类型是 ``Array(第一个参数的类型)``。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 2, 2)
        element = args[0]
        if not element.is_nullable() and element.is_nothing():
            element = get_data_type("Nullable(Nothing)")
        return DataTypeArray(element)

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
        scalar_col = full_column(args[0])
        arr_col = full_column(args[1])
        nested = ret.element.create_column()  # type: ignore[attr-defined]
        out = ColumnArray(nested)
        for i in range(rows):
            value = scalar_col.get(i)
            length = len(arr_col.get(i) or [])
            out.append([value] * length)
        return out

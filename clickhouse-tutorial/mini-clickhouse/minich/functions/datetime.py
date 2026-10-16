"""日期时间函数 —— **时区是"列的属性",不是会话的属性**。

这是 ClickHouse 与 MySQL 最根本的一个分歧点,值得先把图画清楚:

.. code-block:: text

   MySQL / PostgreSQL(SET timezone)        ClickHouse
   ┌──────────────────────────────┐        ┌──────────────────────────────┐
   │ 会话时区 = 'Asia/Shanghai'    │        │ ts  DateTime('Asia/Shanghai')│
   │ 所有 timestamp 列共享它       │        │ ts2 DateTime('UTC')          │
   │ 换个会话, 同一条记录显示不同  │        │ 同一张表里两列两个时区        │
   └──────────────────────────────┘        └──────────────────────────────┘

所以在这份实现里,一个"日期时间函数"要同时处理三样东西:

1. **存储值** —— ``DateTime`` 是 UInt32(UTC 秒),``DateTime64`` 是 Int64 的
   **刻度**(秒 × 10^precision),``Date`` 是 UInt16(自 1970-01-01 的天数);
2. **列类型** —— 它带着时区(``DataTypeDateTime.timezone``)与精度
   (``DataTypeDateTime64.precision``);
3. **展示** —— 本地时间 = UTC 秒 + 时区偏移。

于是"取年份"这件事在内部其实是三步:读刻度 → 加偏移得到**本地时间** →
取 ``.year``。漏掉第二步,``toYear`` 在跨年时刻上就会差一天 ——
第 03 章有一整节讲这个。

.. warning::

   **有两个限制是 mini-clickhouse 的类型系统带来的,不是语义选择:**

   1. ``get_return_type`` 只能看到**实参类型**,看不到**实参的值**。
      所以 ``toDateTime64(x, 6)`` / ``parseDateTime64BestEffort(s, 6)``
      这类"精度写在参数里"的函数,返回类型统一取约定默认的
      ``DateTime64(3)``;``toDateTime(x, tz)`` / ``parseDateTime(x, f, tz)``
      返回的是**不带时区的** ``DateTime``(tz 参数只影响**解析**输入时的
      解释方式)。真实 ClickHouse 的重载解析器能读到常量实参,所以没有
      这个限制。
   2. ``toTimeZone`` 同理:值不变,但我们无法把新时区写进返回类型。

   第 15 章在"与真实 ClickHouse 的差异"表里把这两条单独列了出来。
"""

from __future__ import annotations

import calendar
import datetime as _dt
import re
from typing import Any, List, Optional, Sequence, Tuple

from ..errors import ErrorCode, raise_error
from ..types import get_data_type
from ..types.base import DataType
from ..types.scalars import (
    datetime_to_epoch,
    format_datetime,
    parse_timezone_offset,
)
from .base import FunctionBase, register_function

__all__ = [
    "add_units",
    "to_epoch_seconds",
    "local_datetime",
    "format_datetime_pattern",
]

_EPOCH = _dt.datetime(1970, 1, 1)
_EPOCH_DATE = _dt.date(1970, 1, 1)

_UINT8 = get_data_type("UInt8")
_UINT16 = get_data_type("UInt16")
_UINT32 = get_data_type("UInt32")
_UINT64 = get_data_type("UInt64")
_INT32 = get_data_type("Int32")
_INT64 = get_data_type("Int64")
_STRING = get_data_type("String")
_DATE = get_data_type("Date")
_DATETIME = get_data_type("DateTime")

_ABBR_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_FULL_WEEKDAYS = [
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
]
_ABBR_MONTHS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]
_FULL_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


# ══════════════════════════════════════════════════════════════════════════
# 类型与值之间的"翻译层"
# ══════════════════════════════════════════════════════════════════════════


def _offset_of(t: DataType) -> int:
    """列类型上携带的时区偏移(**秒**)—— 没有时区就是 0。"""
    fn = getattr(t, "tz_offset_seconds", None)
    return fn() if callable(fn) else 0


def _scale_of(t: DataType) -> int:
    """``DateTime64`` 的刻度(``10 ** precision``);其它类型是 1。"""
    fn = getattr(t, "scale", None)
    return fn() if callable(fn) else 1


def _has_scale(t: DataType) -> bool:
    return callable(getattr(t, "scale", None))


def _tz_name_of(t: DataType) -> Optional[str]:
    return getattr(t, "timezone", None)


def to_epoch_seconds(value: Any, t: DataType) -> int:
    """把列里的整数值还原成 **UTC 秒**。

    三种存储布局要分开处理(这是最容易写错的一处,第 03 章专门强调过):

    .. code-block:: text

       Date        存的是**天数**       → × 86400
       DateTime    存的是 UTC 秒        → 原样
       DateTime64  存的是"秒 × 10^p"    → 整除 10^p(丢掉小数)

    ``Date`` 那一行是最阴险的:如果忘了乘 86400,``toYear('2024-03-05')``
    会得到 1970 年 —— 而且**不报错**,只是安静地给出一个荒唐的结果。
    """
    if t.is_date():
        return int(value) * 86400
    scale = _scale_of(t)
    v = int(value)
    return v if scale == 1 else v // scale


def local_datetime(value: Any, t: DataType) -> _dt.datetime:
    """列值 → **本地时间**的 ``datetime``(已经加过时区偏移)。

    这是所有"取字段"函数的入口。它就是 ``EXTRACT`` 系列能正确跨年的原因。
    """
    if t.is_date():
        try:
            d = _EPOCH_DATE + _dt.timedelta(days=int(value))
        except (OverflowError, ValueError):
            d = _dt.date(2149, 6, 6)
        return _dt.datetime(d.year, d.month, d.day)
    secs = to_epoch_seconds(value, t) + _offset_of(t)
    try:
        return _EPOCH + _dt.timedelta(seconds=secs)
    except (OverflowError, ValueError):
        return _dt.datetime(2106, 2, 7, 6, 28, 15)


def encode_local(target: DataType, dt: _dt.datetime) -> Any:
    """本地时间 → 目标类型的**存储值**。

    直接复用 ``DataType.normalize`` —— 因为"把日期转成天数""把时间转成
    UTC 秒"这两件事本来就该由类型自己负责,函数层再写一遍只会重复实现 bug。
    """
    if target.is_date():
        return target.normalize(dt.date())
    return target.normalize(dt)


# ══════════════════════════════════════════════════════════════════════════
# 逐行执行底座 —— 因为要"按类型决定列宽/时区",不能只看值
# ══════════════════════════════════════════════════════════════════════════


class _TypedRowWise(FunctionBase):
    """一个"能看见实参**类型**"的逐行执行底座。

    基类的 ``execute`` 只把**值**交给 ``execute_row``,而日期函数需要
    "这一列是哪个时区、哪个精度" —— 所以这里重写 ``execute``,
    把 ``(值, 类型)`` 一起传下去。真实 ClickHouse 里这些信息在
    ``IFunction::executeImpl`` 的参数里是随手可得的。
    """

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"Function {self.name} does not implement compute()",
        )

    def execute(self, args: Sequence[Any], rows: int, context: Any = None) -> Any:
        cols = [
            a.column.convert_to_full_column() if a.column.is_const() else a.column
            for a in args
        ]
        types = [a.type for a in args]
        out = self.get_return_type(types).create_column()
        for i in range(rows):
            out.append(self.compute([c.get(i) for c in cols], types))
        return out


# ══════════════════════════════════════════════════════════════════════════
# 日历辅助
# ══════════════════════════════════════════════════════════════════════════


def _days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _add_months(dt: _dt.datetime, n: int) -> _dt.datetime:
    """加减月份,**日号超界时钳到月末**(1月31日 + 1个月 = 2月28/29日)。

    这条与真实 ClickHouse 的 ``addMonths`` 一致,也是它和
    "把月份当 30 天"的实现最容易分道扬镳的地方。
    """
    total = dt.year * 12 + (dt.month - 1) + n
    year, month0 = divmod(total, 12)
    month = month0 + 1
    day = min(dt.day, _days_in_month(year, month))
    return dt.replace(year=year, month=month, day=day)


_UNIT_SECONDS = {
    "SECOND": 1, "MINUTE": 60, "HOUR": 3600, "DAY": 86400, "WEEK": 604800,
}


def add_units(dt: _dt.datetime, n: int, unit: str) -> _dt.datetime:
    """按 ``unit`` 加 ``n`` 个单位(负数就是减)。"""
    unit = unit.upper()
    if unit == "YEAR":
        return _add_months(dt, n * 12)
    if unit == "QUARTER":
        return _add_months(dt, n * 3)
    if unit == "MONTH":
        return _add_months(dt, n)
    if unit == "WEEK":
        return dt + _dt.timedelta(weeks=n)
    if unit == "DAY":
        return dt + _dt.timedelta(days=n)
    if unit == "HOUR":
        return dt + _dt.timedelta(hours=n)
    if unit == "MINUTE":
        return dt + _dt.timedelta(minutes=n)
    if unit == "SECOND":
        return dt + _dt.timedelta(seconds=n)
    if unit == "MILLISECOND":
        return dt + _dt.timedelta(milliseconds=n)
    if unit == "MICROSECOND":
        return dt + _dt.timedelta(microseconds=n)
    if unit == "NANOSECOND":
        return dt + _dt.timedelta(microseconds=n / 1000.0)
    raise_error(ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT, f"Unknown interval unit {unit!r}")


def _interval_kind(values: Sequence[Any], types: Sequence[DataType], i: int) -> str:
    """取出"单位"参数。

    单位可能以两种形态出现,必须都认:

    * 一个字符串 / 枚举值:``dateDiff('day', a, b)``;
    * 一个 ``IntervalDay`` 类型的值:``dateDiff(INTERVAL 1 DAY, a, b)``
      (这种写法下真正的单位藏在**类型名**里,值只是那个 1)。
    """
    name = types[i].get_name()
    if name.startswith("Interval"):
        return name[len("Interval"):].upper()
    return str(values[i]).upper()


def _week_number(d: _dt.date, mode: int) -> int:
    """MySQL ``WEEK()`` 的等价实现 —— ``toWeek`` / ``toYearWeek`` / ``%U`` / ``%W`` 共用。

    模式表(与 MySQL 完全一致,ClickHouse 直接照搬):

    =====  ================  =======  ================================
    mode   一周第一天         范围     第 1 周的定义
    =====  ================  =======  ================================
    0      Sunday            0-53     含本年第一个周日的那一周
    1      Monday            0-53     含本年第一个周一的那一周
    2      Sunday            1-53     同上
    3      Monday            1-53     同上
    4      Sunday            0-53     含本年 ≥4 天的那一周(ISO 式)
    5      Monday            0-53     同上
    6      Sunday            1-53     同上
    7      Monday            1-53     同上(= ISO 周)
    =====  ================  =======  ================================

    范围是 ``1-53`` 的模式下,"第 1 周之前的日子"要归入**上一年的最后一周**
    —— 这就是那句递归存在的原因。
    """
    monday_first = mode in (1, 3, 5, 7)
    target_wd = 0 if monday_first else 6  # Python 里 Monday=0 … Sunday=6
    jan1 = _dt.date(d.year, 1, 1)
    if mode <= 3:
        delta = (target_wd - jan1.weekday()) % 7
        week1_start = jan1 + _dt.timedelta(days=delta)
    else:
        week_of_jan1 = jan1 - _dt.timedelta(days=(jan1.weekday() - target_wd) % 7)
        days_in_year = 7 - (jan1 - week_of_jan1).days
        week1_start = week_of_jan1 if days_in_year >= 4 else week_of_jan1 + _dt.timedelta(days=7)
    if d < week1_start:
        if mode in (0, 1, 4, 5):
            return 0
        return _week_number(_dt.date(d.year - 1, 12, 31), mode)
    return (d - week1_start).days // 7 + 1


def _truncate(dt: _dt.datetime, unit: str) -> _dt.datetime:
    """把本地时间截断到某个单位(向左取整)。"""
    unit = unit.upper()
    if unit == "YEAR":
        return dt.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    if unit == "QUARTER":
        month = ((dt.month - 1) // 3) * 3 + 1
        return dt.replace(month=month, day=1, hour=0, minute=0, second=0, microsecond=0)
    if unit == "MONTH":
        return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if unit == "WEEK":
        return _start_of_week(dt, mode=0)
    if unit == "DAY":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == "HOUR":
        return dt.replace(minute=0, second=0, microsecond=0)
    if unit == "MINUTE":
        return dt.replace(second=0, microsecond=0)
    if unit == "SECOND":
        return dt.replace(microsecond=0)
    if unit in ("MILLISECOND", "MICROSECOND", "NANOSECOND"):
        return dt
    raise_error(ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT, f"Unknown unit {unit!r} for truncation")


def _start_of_week(dt: _dt.datetime, mode: int = 0) -> _dt.datetime:
    """本周的第一天。``mode`` 偶数 → 周日,奇数 → 周一(与 MySQL 一致)。"""
    target_wd = 6 if mode % 2 == 0 else 0
    delta = (dt.weekday() - target_wd) % 7
    return (dt - _dt.timedelta(days=delta)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _date_diff(start: _dt.datetime, end: _dt.datetime, unit: str) -> int:
    """``dateDiff`` —— **按"跨了几个单位边界"数**,而不是"差了几个整单位"。

    所以 ``dateDiff('day', '2021-01-01 23:59', '2021-01-02 00:01') = 1``
    (跨过了一天),尽管两者只差两分钟。这正是真实 ClickHouse 的定义:
    两边先截断到单位,再相减。
    """
    unit = unit.upper()
    if unit in _UNIT_SECONDS:
        span = _UNIT_SECONDS[unit]
        if unit == "WEEK":
            a = _start_of_week(start, 0)
            b = _start_of_week(end, 0)
        else:
            a = _truncate(start, unit)
            b = _truncate(end, unit)
        return int((b - a).total_seconds() // span)
    if unit == "MONTH":
        return (end.year - start.year) * 12 + (end.month - start.month)
    if unit == "QUARTER":
        return _date_diff(start, end, "MONTH") // 3
    if unit == "YEAR":
        return end.year - start.year
    raise_error(ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT, f"Unknown unit {unit!r} for dateDiff")


def _age(start: _dt.datetime, end: _dt.datetime, unit: str) -> int:
    """``age`` —— **完整的**单位数(与 ``dateDiff`` 的"边界数"相对)。

    ``age('month', '2020-08-15', '2021-03-01') = 6`` —— 因为 2 月 15 日到
    3 月 1 日不足一个月,那一个月不算。这就是 ``age`` 与 ``dateDiff`` 的
    全部区别,也是它最容易被误用成"两个日期差几个月"的地方。
    """
    unit = unit.upper()
    if unit in _UNIT_SECONDS:
        return int((end - start).total_seconds() // _UNIT_SECONDS[unit])
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if (end.day, end.hour, end.minute, end.second) < (
        start.day, start.hour, start.minute, start.second
    ):
        months -= 1
    if unit == "MONTH":
        return months
    if unit == "QUARTER":
        return months // 3
    if unit == "YEAR":
        return months // 12
    raise_error(ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT, f"Unknown unit {unit!r} for age")


# ══════════════════════════════════════════════════════════════════════════
# 字段提取
# ══════════════════════════════════════════════════════════════════════════


def _register_field(name: str, fn: Any, ret: DataType, description: str,
                    aliases: Sequence[str] = ()) -> type:
    class _F(_TypedRowWise):
        def get_return_type(self, args: Sequence[DataType]) -> DataType:
            return ret

        def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
            return fn(local_datetime(values[0], types[0]))

    _F.__name__ = "Function" + name[:1].upper() + name[1:]
    register_function(name, _F, aliases=list(aliases), description=description)
    return _F


_register_field("toYear", lambda d: d.year, _UINT16,
                "取年份(按列时区换算成本地时间后)", aliases=["YEAR", "toYYYY"])
_register_field("toQuarter", lambda d: (d.month - 1) // 3 + 1, _UINT8,
                "取季度 1-4", aliases=["QUARTER"])
_register_field("toMonth", lambda d: d.month, _UINT8,
                "取月份 1-12", aliases=["MONTH"])
_register_field("toDayOfMonth", lambda d: d.day, _UINT8,
                "取一个月中的第几天 1-31", aliases=["DAYOFMONTH", "DAY"])
_register_field("toDayOfWeek", lambda d: d.isoweekday(), _UINT8,
                "取星期几(**周一=1** … 周日=7)", aliases=["DAYOFWEEK"])
_register_field("toDayOfYear", lambda d: d.timetuple().tm_yday, _UINT16,
                "取一年中的第几天 1-366", aliases=["DAYOFYEAR"])
_register_field("toHour", lambda d: d.hour, _UINT8, "取小时 0-23", aliases=["HOUR"])
_register_field("toMinute", lambda d: d.minute, _UINT8, "取分钟 0-59", aliases=["MINUTE"])
_register_field("toSecond", lambda d: d.second, _UINT8, "取秒 0-59", aliases=["SECOND"])
_register_field("toISOYear", lambda d: d.isocalendar()[0], _UINT16,
                "取 ISO 8601 周所在的年份")
_register_field("toISOWeek", lambda d: d.isocalendar()[1], _UINT8,
                "取 ISO 8601 周号 1-53")


_register_field("toYYYYMM", lambda d: d.year * 100 + d.month, _UINT32,
                "取 YYYYMM 形式的整数")
_register_field("toYYYYMMDD", lambda d: d.year * 10000 + d.month * 100 + d.day,
                _UINT32, "取 YYYYMMDD 形式的整数")
_register_field(
    "toYYYYMMDDhhmmss",
    lambda d: (d.year * 10000000000 + d.month * 100000000 + d.day * 1000000
               + d.hour * 10000 + d.minute * 100 + d.second),
    _UINT64, "取 YYYYMMDDhhmmss 形式的整数",
)


# ══════════════════════════════════════════════════════════════════════════
# toWeek / toYearWeek
# ══════════════════════════════════════════════════════════════════════════


class FunctionToWeek(_TypedRowWise):
    """``toWeek(dt[, mode])`` —— 一年中的第几周(MySQL 模式语义)。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT8

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        d = local_datetime(values[0], types[0]).date()
        mode = int(values[1]) if len(values) >= 2 else 0
        return _week_number(d, mode)


class FunctionToYearWeek(_TypedRowWise):
    """``toYearWeek(dt[, mode])`` —— ``年 × 100 + 周号``。

    .. warning::

       跨年周的处理用了启发式(见代码注释),在 1 月的极少数日期上可能
       与真实 ClickHouse 相差 1。``toISOWeek`` / ``toISOYear`` 走的是
       Python 的标准 ISO 实现,是完全正确的。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _UINT32

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        d = local_datetime(values[0], types[0]).date()
        mode = int(values[1]) if len(values) >= 2 else 0
        week = _week_number(d, mode)
        year = d.year
        # 范围 1-53 的模式下,1 月初的高周号实际上属于**上一年**
        if mode in (2, 3, 6, 7) and d.month == 1 and week >= 52:
            year -= 1
        return year * 100 + week


register_function("toWeek", FunctionToWeek,
                  aliases=["WEEK"],
                  description="一年中的第几周(0-53,mode 决定首日与第 1 周)")
register_function("toYearWeek", FunctionToYearWeek,
                  description="年×100+周号")


# ══════════════════════════════════════════════════════════════════════════
# toStartOf* 系列
# ══════════════════════════════════════════════════════════════════════════


def _start_result_type(unit: str, src: DataType) -> DataType:
    """``toStartOfXxx`` 的返回类型。

    规则(与真实 ClickHouse 一致):

    * 年/季/月/周 → ``Date``(``DateTime64`` 也一样);
    * **小时及以下(含 ``DAY``)→ 保持输入的"时刻类型"**
      (``DateTime`` / ``DateTime64``)。``toStartOfDay`` 返回的是
      ``DateTime`` 而**不是** ``Date`` —— 它保留"当天 00:00:00"的
      时刻渲染(``2020-01-02 00:00:00``),不是 ``2020-01-02``;
    * 输入本来就是 ``Date`` → 还是 ``Date``(``Date32`` 则保持 ``Date32``)。

    (早先把 ``DAY`` 和年/季/月/周归到一类,于是 ``toStartOfDay`` 返回
    ``Date``,既与真实 CH 不符,也丢掉了时刻部分。)
    """
    if src.is_date():
        return get_data_type("Date32") if src.get_name() == "Date32" else _DATE
    if unit in ("YEAR", "QUARTER", "MONTH", "WEEK"):
        return _DATE
    if _has_scale(src):
        return src
    return _DATETIME


def _register_start_of(name: str, unit: str, description: str,
                       aliases: Sequence[str] = ()) -> type:
    class _F(_TypedRowWise):
        def get_return_type(self, args: Sequence[DataType]) -> DataType:
            return _start_result_type(unit, args[0])

        def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
            local = local_datetime(values[0], types[0])
            return encode_local(self.get_return_type(types), _truncate(local, unit))

    _F.__name__ = "Function" + name[:1].upper() + name[1:]
    register_function(name, _F, aliases=list(aliases), description=description)
    return _F


_register_start_of("toStartOfYear", "YEAR", "截断到年初")
_register_start_of("toStartOfQuarter", "QUARTER", "截断到季度初")
_register_start_of("toStartOfMonth", "MONTH", "截断到月初")
_register_start_of("toStartOfDay", "DAY", "截断到当天 00:00:00")
_register_start_of("toStartOfHour", "HOUR", "截断到整点")
_register_start_of("toStartOfMinute", "MINUTE", "截断到整分")
_register_start_of("toStartOfSecond", "SECOND", "截断到整秒(去掉毫秒)")
_register_start_of("toStartOfISOYear", "YEAR", "截断到 ISO 年的第一周")


class FunctionToStartOfWeek(_TypedRowWise):
    """``toStartOfWeek(dt[, mode])`` —— 本周的第一天。

    ``mode`` 默认 **0 = 周日**;奇数 → 周一(与 MySQL 的模式表一致)。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _DATE

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        local = local_datetime(values[0], types[0])
        mode = int(values[1]) if len(values) >= 2 else 0
        return encode_local(_DATE, _start_of_week(local, mode))


class FunctionToMonday(_TypedRowWise):
    """``toMonday(dt)`` —— 本周周一(等价于 ``toStartOfWeek(dt, 1)``)。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _DATE

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        local = local_datetime(values[0], types[0])
        return encode_local(_DATE, _start_of_week(local, 1))


def _register_start_of_n(name: str, minutes: int, description: str) -> type:
    class _F(_TypedRowWise):
        def get_return_type(self, args: Sequence[DataType]) -> DataType:
            return _start_result_type("MINUTE", args[0])

        def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
            local = local_datetime(values[0], types[0])
            span = minutes * 60
            secs = int((local - _EPOCH).total_seconds())
            aligned = (secs // span) * span
            return encode_local(self.get_return_type(types), _EPOCH + _dt.timedelta(seconds=aligned))

    _F.__name__ = "Function" + name[:1].upper() + name[1:]
    register_function(name, _F, description=description)
    return _F


_register_start_of_n("toStartOfFiveMinute", 5, "截断到 5 分钟边界")
_register_start_of_n("toStartOfTenMinutes", 10, "截断到 10 分钟边界")
_register_start_of_n("toStartOfFifteenMinutes", 15, "截断到 15 分钟边界")


class FunctionToStartOfInterval(_TypedRowWise):
    """``toStartOfInterval(dt, count, unit)`` —— 按"每 count 个 unit"取整。

    .. warning::

       真实 ClickHouse 的第二个参数是一个 ``INTERVAL`` 字面量
       (``INTERVAL 3 HOUR``),mini-clickhouse 收的是
       ``(count, unit)`` 两个普通参数。``WEEK`` 的桶边界我们还简化成了
       "按周起点对齐",没实现"以某个 origin 为基准"的选择。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _start_result_type("MINUTE", args[0])

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        local = local_datetime(values[0], types[0])
        count = int(values[1]) if len(values) >= 2 else 1
        unit = _interval_kind(values, types, 2) if len(values) >= 3 else "DAY"
        target = self.get_return_type(types)
        if count <= 0:
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"toStartOfInterval: count must be positive, got {count}",
            )
        if unit in _UNIT_SECONDS:
            if unit == "WEEK":
                base = _start_of_week(local, 0)
                weeks = int((base - _start_of_week(_EPOCH, 0)).days // 7)
                aligned = weeks - (weeks % count)
                return encode_local(target, base - _dt.timedelta(weeks=weeks - aligned))
            span = _UNIT_SECONDS[unit] * count
            secs = int((local - _EPOCH).total_seconds())
            return encode_local(target, _EPOCH + _dt.timedelta(seconds=(secs // span) * span))
        if unit in ("MONTH", "QUARTER", "YEAR"):
            months = count * {"MONTH": 1, "QUARTER": 3, "YEAR": 12}[unit]
            total = local.year * 12 + (local.month - 1)
            aligned = (total // months) * months
            year, month0 = divmod(aligned, 12)
            return encode_local(target, _dt.datetime(year, month0 + 1, 1))
        raise_error(ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT, f"toStartOfInterval: unknown unit {unit!r}")


register_function("toStartOfWeek", FunctionToStartOfWeek,
                  description="本周的第一天(mode 默认 0 = 周日)")
register_function("toMonday", FunctionToMonday,
                  description="本周周一对应的日期")
register_function("toStartOfInterval", FunctionToStartOfInterval,
                  description="按 count×unit 取整到区间起点")


class FunctionToLastDayOfMonth(_TypedRowWise):
    """``toLastDayOfMonth(dt)`` —— 当月最后一天,返回 ``Date``。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _DATE

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        local = local_datetime(values[0], types[0])
        last = _days_in_month(local.year, local.month)
        return encode_local(_DATE, local.replace(day=last))


register_function("toLastDayOfMonth", FunctionToLastDayOfMonth,
                  description="当月最后一天")


class FunctionToTime(_TypedRowWise):
    """``toTime(dt)`` —— 去掉时间部分,返回 ``Date``。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _DATE

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        return encode_local(_DATE, local_datetime(values[0], types[0]))


register_function("toTime", FunctionToTime,
                  description="把日期时间截成日期(去掉时分秒)")


class FunctionTimeSlot(_TypedRowWise):
    """``timeSlot(dt)`` —— 向下取整到 **30 分钟**的边界(返回 ``DateTime``)。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _DATETIME

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        local = local_datetime(values[0], types[0])
        secs = int((local - _EPOCH).total_seconds())
        aligned = (secs // 1800) * 1800
        return encode_local(_DATETIME, _EPOCH + _dt.timedelta(seconds=aligned))


register_function("timeSlot", FunctionTimeSlot,
                  description="向下取整到 30 分钟边界")


# ══════════════════════════════════════════════════════════════════════════
# 时间戳与类型转换
# ══════════════════════════════════════════════════════════════════════════


class FunctionToUnixTimestamp(_TypedRowWise):
    """``toUnixTimestamp(dt[, tz])`` —— UTC 秒。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        if _has_scale(args[0]):
            return _INT64
        return _UINT32

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        return to_epoch_seconds(values[0], types[0])


register_function("toUnixTimestamp", FunctionToUnixTimestamp,
                  description="转换成 UTC 时间戳(秒)")


def _to_epoch(value: Any, src: DataType, offset: int) -> int:
    """把各种输入统一解释成 **UTC 秒**。"""
    if src.is_date_time():
        return to_epoch_seconds(value, src)
    if src.is_date():
        return int(value) * 86400 - offset
    if src.is_string():
        return datetime_to_epoch(str(value), offset)
    # 数值:直接就是 epoch 秒
    return int(value)


def _to_days(value: Any, src: DataType, offset: int) -> int:
    if src.is_date():
        return int(value)
    secs = _to_epoch(value, src, offset)
    days = (secs + offset) // 86400
    return days


class FunctionToDate(_TypedRowWise):
    """``toDate(x[, tz])`` —— 转成 ``Date``。

    ``x`` 可以是 ``DateTime`` / ``DateTime64`` / ``Date`` / 字符串 / 数字。
    字符串按 ``YYYY-MM-DD``(以及 ``YYYY-MM-DD hh:mm:ss``)解析。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        if args[0].is_date():
            return args[0]
        return _DATE

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        offset = parse_timezone_offset(str(values[1])) if len(values) >= 2 else _offset_of(types[0])
        return _DATE.normalize(_dt.date(1970, 1, 1) + _dt.timedelta(days=_to_days(values[0], types[0], offset)))


class FunctionToDateTime(_TypedRowWise):
    """``toDateTime(x[, tz])`` —— 转成 ``DateTime``。

    .. warning::

       返回类型是**不带时区**的 ``DateTime`` —— 因为``tz`` 是**实参的值**,
       而 ``get_return_type`` 只能看到类型。``tz`` 仍然会参与**解释**输入
       (字符串按该时区的墙上时间解析),所以``toDateTime('2024-01-01 08:00:00',
       'Asia/Shanghai')`` 得到的 UTC 秒是正确的,只是列类型上没记下时区。
       参见模块开头的说明。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        if args[0].is_date_time():
            return args[0]
        return _DATETIME

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        offset = parse_timezone_offset(str(values[1])) if len(values) >= 2 else _offset_of(types[0])
        return _to_epoch(values[0], types[0], offset)


class FunctionToDateTime64(_TypedRowWise):
    """``toDateTime64(x[, precision[, tz]])`` —— 转成 ``DateTime64``。

    .. warning::

       ``precision`` 是**实参的值**,所以返回类型固定取约定默认的
       ``DateTime64(3)``。真实 ClickHouse 会因为常量实参推导出
       ``DateTime64(6)`` 之类,mini-clickhouse 不行。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        if _has_scale(args[0]):
            return args[0]
        return get_data_type("DateTime64(3)")

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        target = self.get_return_type(types)
        precision = target.precision  # type: ignore[attr-defined]
        offset = parse_timezone_offset(str(values[2])) if len(values) >= 3 else _offset_of(types[0])
        secs = _to_epoch(values[0], types[0], offset)
        return secs * (10 ** precision)


register_function("toDate", FunctionToDate,
                  aliases=["DATE"],
                  description="转成 Date(可带时区参数解释输入)")
register_function("toDateTime", FunctionToDateTime,
                  aliases=["DATETIME"],
                  description="转成 DateTime")
register_function("toDateTime64", FunctionToDateTime64,
                  description="转成 DateTime64(精度固定为 3)")


# ══════════════════════════════════════════════════════════════════════════
# formatDateTime —— 自己实现格式化,不依赖平台 strftime
# ══════════════════════════════════════════════════════════════════════════


def format_datetime_pattern(dt: _dt.datetime, pattern: str, epoch_seconds: int) -> str:
    """把本地时间按 ClickHouse 的格式串渲染成文本。

    **为什么不用 ``strftime``?** 因为 Windows 的 C 运行时不认 ``%u`` / ``%V``
    / ``%G`` 这些 ISO 指定符(它们不是 C99 标准),而 ClickHouse 的格式串
    里恰恰有它们。自己实现一遍还有第二个好处:**每一种指定符的含义都能
    直接读出来**,不用去查平台文档。

    支持的全部指定符见 :func:`_format_specifier`。
    """
    out: List[str] = []
    iso = dt.isocalendar()  # (ISO 年, ISO 周, ISO 星期 1-7)
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch != "%":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            out.append("%")
            break
        out.append(_format_specifier(dt, pattern[i + 1], iso, epoch_seconds))
        i += 2
    return "".join(out)


def _format_specifier(dt: _dt.datetime, spec: str, iso: Tuple[int, int, int],
                      epoch_seconds: int) -> str:
    """一个 ``%X`` 的展开。

    =====  ==================================================
    %Y %y  年(4 位 / 2 位)
    %C     世纪(年 / 100,2 位)
    %m %d  月 / 日(2 位);%e 日(空格补齐)
    %D     %m/%d/%y;%F = %Y-%m-%d
    %j     一年中的第几天(3 位)
    %H %I  24 小时 / 12 小时制;%l 12 小时制(空格补齐)
    %M %i  分钟(2 位)—— %i 是为了兼容 MySQL 的解析格式
    %S     秒(2 位)
    %f     微秒(6 位)
    %s     Unix 时间戳(秒)
    %p     AM / PM
    %G %g  ISO 年(4 位 / 2 位);%V ISO 周号
    %u %w  ISO 星期(周一=1)/ 星期(周日=0)
    %a %A  英文星期缩写 / 全称
    %b %h %B  英文月份缩写(%b/%h)/ 全称(%B)
    %c     ctime 风格
    %U %W  周号(周日为首日 / 周一为首日)
    %Q %q  季度 1-4
    %o     ISO 周号(与 %V 同,mini-clickhouse 的解释)
    %n %%  换行 / 百分号
    =====  ==================================================
    """
    if spec == "Y":
        return f"{dt.year:04d}"
    if spec == "y":
        return f"{dt.year % 100:02d}"
    if spec == "C":
        return f"{dt.year // 100:02d}"
    if spec == "m":
        return f"{dt.month:02d}"
    if spec == "d":
        return f"{dt.day:02d}"
    if spec == "e":
        return f"{dt.day:2d}"
    if spec == "j":
        return f"{dt.timetuple().tm_yday:03d}"
    if spec == "D":
        return f"{dt.month:02d}/{dt.day:02d}/{dt.year % 100:02d}"
    if spec == "F":
        return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
    if spec == "H":
        return f"{dt.hour:02d}"
    if spec == "I":
        return f"{(dt.hour % 12) or 12:02d}"
    if spec == "l":
        return f"{(dt.hour % 12) or 12:2d}"
    if spec in ("M", "i"):
        return f"{dt.minute:02d}"
    if spec == "S":
        return f"{dt.second:02d}"
    if spec == "f":
        return f"{dt.microsecond:06d}"
    if spec == "s":
        return str(epoch_seconds)
    if spec == "p":
        return "AM" if dt.hour < 12 else "PM"
    if spec == "G":
        return f"{iso[0]:04d}"
    if spec == "g":
        return f"{iso[0] % 100:02d}"
    if spec == "V":
        return f"{iso[1]:02d}"
    if spec == "o":
        return f"{iso[1]:02d}"
    if spec == "u":
        return str(iso[2])
    if spec == "w":
        return str(iso[2] % 7)
    if spec == "a":
        return _ABBR_WEEKDAYS[iso[2] - 1]
    if spec == "A":
        return _FULL_WEEKDAYS[iso[2] - 1]
    if spec in ("b", "h"):
        return _ABBR_MONTHS[dt.month - 1]
    if spec == "B":
        return _FULL_MONTHS[dt.month - 1]
    if spec == "c":
        return (
            f"{_ABBR_WEEKDAYS[iso[2] - 1]} {_ABBR_MONTHS[dt.month - 1]} {dt.day:2d} "
            f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d} {dt.year:04d}"
        )
    if spec == "Q" or spec == "q":
        return str((dt.month - 1) // 3 + 1)
    if spec == "U":
        return f"{_week_number(dt.date(), 0):02d}"
    if spec == "W":
        return f"{_week_number(dt.date(), 1):02d}"
    if spec == "R":
        return f"{dt.hour:02d}:{dt.minute:02d}"
    if spec == "T":
        return f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
    if spec == "n":
        return "\n"
    if spec == "%":
        return "%"
    return "%" + spec


class FunctionFormatDateTime(_TypedRowWise):
    """``formatDateTime(dt, format[, tz])`` —— 按格式串渲染日期时间。

    常用例:``formatDateTime(ts, '%Y-%m-%d %H:%i:%S')``。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        if len(values) >= 3:
            offset = parse_timezone_offset(str(values[2]))
        else:
            offset = _offset_of(types[0])
        secs = to_epoch_seconds(values[0], types[0])
        try:
            local = _EPOCH + _dt.timedelta(seconds=secs + offset)
        except (OverflowError, ValueError):
            local = _dt.datetime(2106, 2, 7, 6, 28, 15)
        # %f 需要真实的小数部分:DateTime64 的刻度里就带着它
        scale = _scale_of(types[0])
        if scale > 1:
            frac = int(values[0]) % scale
            micros = int(frac * 1_000_000 / scale)
            local = local.replace(microsecond=micros)
        return format_datetime_pattern(local, str(values[1]), secs)


register_function("formatDateTime", FunctionFormatDateTime,
                  aliases=["DATE_FORMAT"],
                  description="按格式串渲染日期时间(如 %Y-%m-%d %H:%i:%S)")


# ══════════════════════════════════════════════════════════════════════════
# 解析
# ══════════════════════════════════════════════════════════════════════════

#: ``parseDateTime`` 的格式串里各指定符 → "这个组怎么解析"
_PARSE_SPECS = {
    "Y": ("year", r"\d{4}"), "y": ("year2", r"\d{2}"), "C": (None, r"\d{2}"),
    "m": ("month", r"\d{1,2}"), "d": ("day", r"\d{1,2}"), "e": ("day", r"\d{1,2}"),
    "H": ("hour", r"\d{1,2}"), "I": ("hour12", r"\d{1,2}"), "k": ("hour", r"\d{1,2}"),
    "M": ("minute", r"\d{1,2}"), "i": ("minute", r"\d{1,2}"),
    "S": ("second", r"\d{1,2}"), "s": ("second", r"\d{1,2}"),
    "f": ("micro", r"\d{1,6}"), "j": ("yday", r"\d{1,3}"),
    "p": ("ampm", r"AM|PM|am|pm"), "b": ("monthname", r"[A-Za-z]{3}"),
    "B": ("monthname_full", r"[A-Za-z]+"), "a": ("weekday", r"[A-Za-z]+"),
    "A": ("weekday", r"[A-Za-z]+"),
}


def _build_parse_regex(fmt: str) -> "Tuple[Any, List[Tuple[str, str]]]":
    parts: List[str] = []
    fields: List[Tuple[str, str]] = []
    i = 0
    n = len(fmt)
    while i < n:
        ch = fmt[i]
        if ch == "%" and i + 1 < n:
            spec = fmt[i + 1]
            i += 2
            if spec == "%":
                parts.append("%")
                continue
            if spec in _PARSE_SPECS:
                name, pat = _PARSE_SPECS[spec]
                if name is None:
                    parts.append("(?:" + pat + ")")
                else:
                    gname = f"g{len(fields)}"
                    parts.append(f"(?P<{gname}>{pat})")
                    fields.append((gname, name))
                continue
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"parseDateTime: unsupported format specifier %{spec}",
            )
        parts.append(re.escape(ch))
        i += 1
    return re.compile("".join(parts)), fields


def _parse_with_format(text: str, fmt: str, offset: int) -> int:
    rx, fields = _build_parse_regex(fmt)
    m = rx.search(text)
    if not m:
        raise_error(
            ErrorCode.CANNOT_PARSE_DATETIME,
            f"parseDateTime: {text!r} does not match the format {fmt!r}",
        )
    got: dict = {}
    for gname, kind in fields:
        got.setdefault(kind, m.group(gname))
    year = int(got.get("year", 1970))
    if "year2" in got and "year" not in got:
        y2 = int(got["year2"])
        year = 2000 + y2 if y2 < 70 else 1900 + y2
    month = int(got.get("month", 1))
    if "monthname" in got or "monthname_full" in got:
        token = (got.get("monthname") or got.get("monthname_full")).lower()[:3]
        for idx, name in enumerate(_ABBR_MONTHS, start=1):
            if name.lower() == token:
                month = idx
                break
    day = int(got.get("day", 1))
    hour = int(got.get("hour", 0))
    if "hour12" in got:
        hour = int(got["hour12"]) % 12
        if str(got.get("ampm", "")).upper() == "PM":
            hour += 12
    minute = int(got.get("minute", 0))
    second = int(got.get("second", 0))
    micro = 0
    if "micro" in got:
        frac = got["micro"]
        # 小数部分按"位数"补齐到 6 位:'.5' -> 500000,'.123456' -> 123456
        micro = int((frac + "000000")[:6])
    if "yday" in got and "month" not in got and "day" not in got:
        base = _dt.date(year, 1, 1) + _dt.timedelta(days=int(got["yday"]) - 1)
        month, day = base.month, base.day
    dt = _dt.datetime(year, month, day, hour, minute, second, micro)
    return int((dt - _EPOCH).total_seconds()) - offset


class FunctionParseDateTime(_TypedRowWise):
    """``parseDateTime(str, format[, tz])`` —— 按格式串解析。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _DATETIME

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        offset = parse_timezone_offset(str(values[2])) if len(values) >= 3 else 0
        return _parse_with_format(str(values[0]), str(values[1]), offset)


_BEST_EFFORT_FORMATS = [
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y",
    "%Y%m%d%H%M%S",
    "%Y%m%d",
]


def _parse_best_effort(text: str) -> Optional[_dt.datetime]:
    """尽最大努力解析 —— 返回 ``None`` 表示失败(由调用方决定抛错还是给 NULL)。"""
    s = text.strip()
    if not s:
        return None
    if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
        try:
            return _EPOCH + _dt.timedelta(seconds=int(s))
        except (OverflowError, ValueError):
            return None
    for fmt in _BEST_EFFORT_FORMATS:
        try:
            return _dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


class FunctionParseDateTimeBestEffort(_TypedRowWise):
    """``parseDateTimeBestEffort(str[, tz])`` —— 猜格式解析,失败**抛错**。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _DATETIME

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        offset = parse_timezone_offset(str(values[1])) if len(values) >= 2 else 0
        dt = _parse_best_effort(str(values[0]))
        if dt is None:
            raise_error(
                ErrorCode.CANNOT_PARSE_DATETIME,
                f"parseDateTimeBestEffort: cannot parse {values[0]!r}",
            )
        return int((dt - _EPOCH).total_seconds()) - offset


class FunctionParseDateTimeBestEffortOrNull(_TypedRowWise):
    """``parseDateTimeBestEffortOrNull(str[, tz])`` —— 失败返回 **NULL**。

    它的返回类型是 ``Nullable(DateTime)``;解析失败时往结果列里塞 ``None``。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return get_data_type("Nullable(DateTime)")

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        offset = parse_timezone_offset(str(values[1])) if len(values) >= 2 else 0
        dt = _parse_best_effort(str(values[0]))
        if dt is None:
            return None
        return int((dt - _EPOCH).total_seconds()) - offset


class FunctionParseDateTimeBestEffortOrZero(FunctionParseDateTimeBestEffortOrNull):
    """``parseDateTimeBestEffortOrZero`` —— 失败返回**零值**(1970-01-01)。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _DATETIME

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        dt = _parse_best_effort(str(values[0]))
        if dt is None:
            return 0
        offset = parse_timezone_offset(str(values[1])) if len(values) >= 2 else 0
        return int((dt - _EPOCH).total_seconds()) - offset


class FunctionParseDateTime64BestEffort(_TypedRowWise):
    """``parseDateTime64BestEffort(str[, precision[, tz]])`` —— 返回 ``DateTime64``。

    .. warning::

       同 ``toDateTime64``:``precision`` 是实参的值,所以返回类型固定
       ``DateTime64(3)``。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return get_data_type("DateTime64(3)")

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        dt = _parse_best_effort(str(values[0]))
        if dt is None:
            raise_error(
                ErrorCode.CANNOT_PARSE_DATETIME,
                f"parseDateTime64BestEffort: cannot parse {values[0]!r}",
            )
        offset = parse_timezone_offset(str(values[2])) if len(values) >= 3 else 0
        secs = int((dt - _EPOCH).total_seconds()) - offset
        return secs * 1000 + dt.microsecond // 1000


register_function("parseDateTime", FunctionParseDateTime,
                  aliases=["STR_TO_DATE"],
                  description="按格式串解析出 DateTime")
register_function("parseDateTimeBestEffort", FunctionParseDateTimeBestEffort,
                  description="尽力猜测格式解析(失败抛错)")
register_function("parseDateTimeBestEffortOrNull", FunctionParseDateTimeBestEffortOrNull,
                  description="尽力解析,失败返回 NULL")
register_function("parseDateTimeBestEffortOrZero", FunctionParseDateTimeBestEffortOrZero,
                  description="尽力解析,失败返回零值")
register_function("parseDateTime64BestEffort", FunctionParseDateTime64BestEffort,
                  description="尽力解析出 DateTime64(精度固定 3)")


# ══════════════════════════════════════════════════════════════════════════
# 加减与截断
# ══════════════════════════════════════════════════════════════════════════


class FunctionDateAdd(_TypedRowWise):
    """``dateAdd(unit, n, dt)`` / ``dateSub(unit, n, dt)`` —— 日历感知的加减。"""

    sign = 1

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return args[2]

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        unit = _interval_kind(values, types, 0)
        n = int(values[1]) * self.sign
        local = local_datetime(values[2], types[2])
        return encode_local(self.get_return_type(types), add_units(local, n, unit))


class FunctionDateSub(FunctionDateAdd):
    sign = -1


register_function("dateAdd", FunctionDateAdd,
                  description="按单位给日期加 n(dateAdd(unit, n, dt))")
register_function("dateSub", FunctionDateSub,
                  description="按单位给日期减 n(dateSub(unit, n, dt))")


class FunctionDateDiff(_TypedRowWise):
    """``dateDiff(unit, start, end)`` —— **跨过了几个单位边界**。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _INT64

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        unit = _interval_kind(values, types, 0)
        start = local_datetime(values[1], types[1])
        end = local_datetime(values[2], types[2])
        return _date_diff(start, end, unit)


class FunctionAge(_TypedRowWise):
    """``age(unit, start, end)`` —— **完整的**单位个数。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _INT64

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        unit = _interval_kind(values, types, 0)
        start = local_datetime(values[1], types[1])
        end = local_datetime(values[2], types[2])
        return _age(start, end, unit)


register_function("dateDiff", FunctionDateDiff,
                  aliases=["date_diff", "timestampDiff"],
                  description="两个日期之间跨过了几个单位边界")
register_function("age", FunctionAge,
                  description="两个日期之间相差几个**完整**单位")


class FunctionDateTrunc(_TypedRowWise):
    """``dateTrunc(unit, dt[, tz])`` —— 截断到单位(别名 ``date_trunc``)。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _DATETIME

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        unit = _interval_kind(values, types, 0)
        local = local_datetime(values[1], types[1])
        return encode_local(_DATETIME, _truncate(local, unit))


register_function("dateTrunc", FunctionDateTrunc, aliases=["date_trunc"],
                  description="把日期时间截断到某个单位")


def _register_add_sub(name: str, unit: str, sign: int, description: str) -> type:
    class _F(_TypedRowWise):
        def get_return_type(self, args: Sequence[DataType]) -> DataType:
            return args[0]

        def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
            n = int(values[1]) * sign
            local = local_datetime(values[0], types[0])
            return encode_local(self.get_return_type(types), add_units(local, n, unit))

    _F.__name__ = "Function" + name[:1].upper() + name[1:]
    register_function(name, _F, description=description)
    return _F


for _unit in (
    "Years", "Quarters", "Months", "Weeks", "Days", "Hours",
    "Minutes", "Seconds", "Milliseconds", "Microseconds", "Nanoseconds",
):
    _singular = _unit[:-1].upper()
    _register_add_sub(f"add{_unit}", _singular, 1, f"给日期加上若干个 {_unit}")
    _register_add_sub(f"subtract{_unit}", _singular, -1, f"给日期减去若干个 {_unit}")
del _unit, _singular


# ══════════════════════════════════════════════════════════════════════════
# 时区
# ══════════════════════════════════════════════════════════════════════════


class FunctionTimezone(_TypedRowWise):
    """``timezone()`` —— 服务端时区。mini-clickhouse 固定返回 ``'UTC'``。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        return "UTC"


class FunctionTimezoneOf(_TypedRowWise):
    """``timezoneOf(dt)`` —— **列类型上**的时区名,没有就是 ``'UTC'``。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _STRING

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        return _tz_name_of(types[0]) or "UTC"


class FunctionTimezoneOffset(_TypedRowWise):
    """``timezoneOffset(dt)`` —— 列时区相对 UTC 的偏移(**秒**)。"""

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _INT32

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        return _offset_of(types[0])


class FunctionToTimeZone(_TypedRowWise):
    """``toTimeZone(dt, tz)`` —— 换时区。

    .. warning::

       真实 ClickHouse 返回 ``DateTime(tz)`` —— **存储值不变,只是类型上的
       时区变了**。mini-clickhouse 的返回类型拿不到那个 tz 字符串,所以
       返回的是不带时区的 ``DateTime``。值本身是对的,只是"展示时按哪个
       时区"这件事丢了。参见模块开头的说明。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _DATETIME

    def compute(self, values: Sequence[Any], types: Sequence[DataType]) -> Any:
        parse_timezone_offset(str(values[1]))  # 校验时区名合法
        return to_epoch_seconds(values[0], types[0])


register_function("timezone", FunctionTimezone,
                  description="服务端时区(固定 UTC)")
register_function("timezoneOf", FunctionTimezoneOf,
                  description="这一列的类型上带的时区")
register_function("timezoneOffset", FunctionTimezoneOffset,
                  description="这一列的时区相对 UTC 的偏移秒数")
register_function("toTimeZone", FunctionToTimeZone,
                  description="把值换到另一个时区(存储值不变)")


# ══════════════════════════════════════════════════════════════════════════
# 未实现(注册但明确报错,而不是静默缺失)
# ══════════════════════════════════════════════════════════════════════════


class FunctionToRelativeNum(_TypedRowWise):
    """``toRelativeXxxNum`` 这一族 —— **未实现**。

    它们在真实 ClickHouse 里的语义是"单位内部编号"(``月 = 年×12+月`` 之类),
    用途很窄(主要是老的分区表达式),所以 mini-clickhouse 不做。
    注册出来是为了让 ``system.functions`` 里能看到它们存在、以及为什么缺席。
    """

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        return _INT64

    def execute(self, args: Sequence[Any], rows: int, context: Any = None) -> Any:
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"{self.name} is not implemented in mini-clickhouse: it is only used "
            f"by legacy partition expressions",
        )


for _rel in ("Year", "Quarter", "Month", "Week", "Day", "Hour", "Minute", "Second"):
    register_function(
        f"toRelative{_rel}Num", FunctionToRelativeNum,
        description=f"相对编号(未实现)",
    )
del _rel


# ══════════════════════════════════════════════════════════════════════════
# 固定步长的"整 N 分钟"截断
# ══════════════════════════════════════════════════════════════════════════
#
# ClickHouse 里有三个固定步长的截断函数,名字是**复数**:
#
#     toStartOfFiveMinutes / toStartOfTenMinutes / toStartOfFifteenMinutes
#
# 它们等价于 ``toStartOfInterval(ts, INTERVAL 5 MINUTE)`` 等,但走的是
# 一条更短的代码路径(不需要构造 Interval 参数)。
#
# .. warning::
#
#    **单复数很容易写错**:``toStartOfFiveMinute``(单数)在真实
#    ClickHouse 里**不存在** —— 官方在一次更名里把单数改成了复数。
#    这里以复数为规范名,同时把单数注册成**别名**,这样两种写法都能跑,
#    读者拿旧文档里的写法来试也不会撞错。真实 ClickHouse 不接受单数,
#    这一点在正文里说明。
#
# 截断规则与 ClickHouse 一致:**按"从纪元起"的整除**,不是"按当天 0 点起"。
# 对一个 ``DateTime`` 来说,``toStartOfFifteenMinutes('2024-01-01 00:07:00')``
# 得到 ``00:00:00``、``00:22:00`` 得到 ``00:15:00`` —— 因为
# ``00:07`` 落在"纪元以来的第 n 个 15 分钟"里。这一点在跨时区时尤其要小心:
# 不是所有时区都相对 UTC 整小时偏移(如 +05:45 的尼泊尔),
# 那种情况下"整 15 分钟"的边界会落在非整点位置。


def _register_start_of_minutes(name: str, minutes: int, singular: str) -> None:
    step = minutes * 60

    class _F(FunctionBase):
        deterministic = True

        def get_return_type(self, args: Sequence[DataType]) -> DataType:
            # 输入是 DateTime(或 DateTime64)时结果仍是 DateTime —— 与真实
            # ClickHouse 一致。Date 进来也能用(它在内部就是天数,截断到
            # 整 N 分钟对"天"这个粒度没有意义,但不会崩)。
            if args and args[0].get_name().startswith("DateTime"):
                return _DATETIME
            return _DATETIME

        def execute_row(self, values: Sequence[Any], context: Any = None) -> Any:
            """把秒数按"从纪元起"的整除截断到最近的 N 分钟边界。

            注意是**从纪元起**整除,不是"从当天 0 点起"。对 UTC 整点偏移的
            时区两者等价;对尼泊尔(+05:45)这类非整小时偏移的时区,
            边界会落在非整点位置上 —— 这与真实 ClickHouse 的行为一致,
            因为它在内部就是对这个整数做的整除。
            """
            epoch = int(values[0]) if values[0] is not None else 0
            return (epoch // step) * step

    _F.__name__ = "Function" + name[:1].upper() + name[1:]
    register_function(
        name, _F, aliases=[singular],
        description=f"截断到最近的整 {minutes} 分钟",
    )


_register_start_of_minutes("toStartOfFiveMinutes", 5, "toStartOfFiveMinute")
_register_start_of_minutes("toStartOfTenMinutes", 10, "toStartOfTenMinute")
_register_start_of_minutes("toStartOfFifteenMinutes", 15, "toStartOfFifteenMinute")

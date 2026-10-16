"""键的构造 —— ``GROUP BY`` / ``DISTINCT`` / ``JOIN`` / 主键共用的那一套。

真实 ClickHouse 的做法是**把键列按行序列化成一段连续字节**
(``serializeKeys``,见 ``src/Interpreters/ExpressionActions.cpp``),
然后用这段字节做哈希与相等比较。好处:

* 一次 ``memcmp`` 比完所有键列(而不是逐列比);
* 键字节连续存放,哈希表探测时缓存命中率高;
* 比较语义唯一 —— ``1`` 和 ``1.0`` 的字节不同,所以不会被误判为相等。

mini-clickhouse 用 **Python 元组**做键:语义完全等价(相等的元组 ⟺
相等的序列化字节),但在 CPython 里元组哈希是 C 层实现的,比手工
拼字节串快得多。**唯一的坑是浮点的 ``NaN``** ——
``float('nan') != float('nan')``,而两个 ``NaN`` 在 ClickHouse 里
必须落到同一个分组。所以 :func:`normalize_key_value` 必须先把
NaN 换成一个可比较的哨兵。

第 12 章会对比这两种键的做法,并给出一个"如果键里有 NULL 会怎样"的例子。
"""

from __future__ import annotations

import math
from typing import Any, Iterable, List, Sequence, Tuple

from ..columns import IColumn
from ..errors import ErrorCode, raise_error

__all__ = [
    "normalize_key_value",
    "key_from_columns",
    "KeyDescription",
    "build_key",
]


class _NaNKey:
    """``NaN`` 的键表示 —— 所有 NaN 都相等,且与任何数不相等。"""

    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _NaNKey)

    def __hash__(self) -> int:
        return 0x7FF8000000000000

    def __repr__(self) -> str:  # pragma: no cover
        return "nan"

    def __lt__(self, other: Any) -> bool:
        # 真实 ClickHouse 的排序里 NaN 是**最大值**(比 +inf 还大),
        # 所以它排在最后(ASC)或最前(DESC),与 NULL 的行为一致。
        return False

    def __gt__(self, other: Any) -> bool:
        return not isinstance(other, _NaNKey)


#: NaN 的全局单例 —— 保证所有 NaN 的键都是**同一个对象**,
#: 这样元组比较时能走 Python 的身份短路。
NAN_KEY = _NaNKey()


def normalize_key_value(value: Any) -> Any:
    """把一个值转成"可哈希、可比较"的键元素。

    处理三件事:

    1. ``float`` 的 ``NaN`` → :data:`NAN_KEY`(让所有 NaN 相等);
    2. ``list``(Array 值)→ ``tuple``(列表不可哈希);
    3. ``dict``(Map 值)→ 排序后的元组对(字典不可哈希,且顺序无关)。
    """
    if isinstance(value, float):
        return NAN_KEY if math.isnan(value) else value
    if isinstance(value, list):
        return tuple(normalize_key_value(v) for v in value)
    if isinstance(value, tuple):
        return tuple(normalize_key_value(v) for v in value)
    if isinstance(value, dict):
        # Map 的键值对顺序不影响相等性 —— 真实 CH 的 Map 哈希也是顺序无关的
        return tuple(
            sorted(
                ((normalize_key_value(k), normalize_key_value(v)) for k, v in value.items()),
                key=repr,
            )
        )
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    return value


def build_key(columns: Sequence[IColumn], row: int) -> tuple:
    """从若干列取出第 ``row`` 行的值,拼成一个元组键。

    单列时**直接返回裸值而不是单元组** —— 这是一个有意的优化:
    ``GROUP BY user_id`` 是最常见的形态,裸整数的哈希比单元组快一截,
    而真实 ClickHouse 在只有一个键列时也不做序列化,直接用值的字节。
    """
    if not columns:
        raise_error(ErrorCode.LOGICAL_ERROR, "build_key requires at least one column")
    if len(columns) == 1:
        return normalize_key_value(columns[0].get(row))
    return tuple(normalize_key_value(c.get(row)) for c in columns)


def key_from_columns(columns: Sequence[IColumn], row: int) -> Any:
    """同 :func:`build_key`,但保留"单列 → 裸值"的形态。"""
    return build_key(columns, row)


class KeyDescription:
    """一组键列的**描述**(列索引 + 方向 + NULL 位置)。

    ``ORDER BY a ASC, b DESC NULLS FIRST`` 会编译成
    ``KeyDescription(columns=[0,1], reverse=[False,True], nulls_first=[True,False])``。
    排序、``LIMIT BY``、``DISTINCT`` 与 MergeTree 的主键都复用这个结构 ——
    它和真实 ClickHouse 的 ``SortDescription`` 一一对应。
    """

    __slots__ = ("columns", "reverse", "nulls_first")

    def __init__(
        self,
        columns: Sequence[int],
        reverse: Sequence[bool] | None = None,
        nulls_first: Sequence[bool] | None = None,
    ) -> None:
        self.columns = list(columns)
        n = len(self.columns)
        self.reverse = list(reverse) if reverse else [False] * n
        #: ``None`` 表示"用默认"—— 而默认取决于方向:
        #: ASC 时 NULL 在最后,DESC 时 NULL 在最前(因为 NULL 是最大值)。
        self.nulls_first = list(nulls_first) if nulls_first else [False] * n
        if len(self.reverse) != n or len(self.nulls_first) != n:
            raise_error(
                ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH,
                "KeyDescription directions must match the number of key columns",
            )

    def __len__(self) -> int:
        return len(self.columns)

    def __repr__(self) -> str:  # pragma: no cover
        parts = []
        for i, c in enumerate(self.columns):
            d = "DESC" if self.reverse[i] else "ASC"
            nf = " NULLS FIRST" if self.nulls_first[i] else ""
            parts.append(f"#{c} {d}{nf}")
        return "KeyDescription(" + ", ".join(parts) + ")"

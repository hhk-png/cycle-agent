"""逻辑函数 —— ``and`` / ``or`` / ``not`` / ``xor``,以及**三值逻辑**。

这个小模块是整本书里最值得"逐格背下来"的一个,因为它解释了一个经典面试题:

.. code-block:: sql

   SELECT NULL AND 0, NULL AND 1, NULL OR 1, NULL OR 0, NOT NULL;
   -- 0    NULL    1    NULL    NULL

为什么 ``NULL AND 0`` 是 **0** 而不是 NULL? 因为逻辑学里 NULL 代表
"**未知**"(unknown),而:

* 「未知 且 假」—— 无论未知的那半是什么,结论一定是**假**;
* 「未知 且 真」—— 结论取决于未知的那半,所以还是**未知**;
* 「未知 或 真」—— 一定有真,结论必然是**真**;
* 「未知 或 假」—— 还是未知。

这不是 ClickHouse 的发明,而是 SQL 标准里的 **三值逻辑**(three-valued logic,
3VL)。完整真值表(``T``/``F``/``NULL`` 三行三列):

.. code-block:: text

     AND  │  F    NULL   T            OR   │  F    NULL   T          NOT
     ─────┼────────────────────        ─────┼────────────────────      ────────
     F    │  F     F     F             F    │  F    NULL   T          T → F
     NULL │  F    NULL  NULL           NULL │ NULL  NULL   T          F → T
     T    │  F    NULL   T             T    │  T     T     T          NULL → NULL

两个"反常识"的格子就是左上角的 ``NULL AND F = F`` 与右上角的 ``NULL OR T = T``:
**一个确定的值可以"吃掉"另一边的不确定性**。这条规则是短路求值的理论基础
(编译器可以说"&& 的左边是 false 时右边不用算"),也是优化器能放心
把 ``WHERE a = 1 AND b = 2`` 拆成两个过滤条件的前提。

实现这个语义有个前提:**函数必须能看见 NULL**。默认的 NULL 传播会在
``and`` 看到任何一个 NULL 的瞬间把结果变成 NULL —— 那就会算出
``NULL AND 0 = NULL``,错了。所以四个函数一律 ``propagate_nulls = False``,
自己拿着 ``None`` 逐行判断:

.. code-block:: python

   if a is None:                      # 未知
       if b == 0: return 0            # 「未知 且 假」= 假
       return None                    # 「未知 且 真/未知」= 未知

.. note:: 真实 ClickHouse 还额外做了一件事:``and``/``or`` 支持
   **短路求值**(``short_circuit_function_evaluation`` 设置,默认开),
   于是 ``WHERE x != 0 AND 1 / x > 3`` 不会因为 ``x = 0`` 而算出 ``inf``。
   mini-clickhouse 不做这一层(它需要修改表达式的求值顺序,
   而不是函数的实现)—— 照旧,不实现比假装实现好。
"""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np

from ..block import ColumnWithTypeAndName
from ..columns import ColumnVector, IColumn
from ..errors import ErrorCode, raise_error
from ..types import DataType, get_data_type
from ..types.composite import DataTypeNullable
from .arithmetic import _register  # noqa: F401  —— 本模块统一的注册装饰器
from .arithmetic import check_arity, full_column, unwrap_nullable
from .base import FunctionBase

__all__ = [
    "truthy",
    "FunctionAnd",
    "FunctionOr",
    "FunctionNot",
    "FunctionXor",
]


def truthy(value: Any) -> bool:
    """把任意值解释成"真/假" —— **非零即真**。

    真实 ClickHouse 的 ``and``/``or`` 会把实参转成 ``UInt8`` 再按位判断,
    所以 ``and(2, 3) = 1``(而不是 2 或者 3)。字符串参数在真实 CH 里会被
    试着转成数字;mini-clickhouse 这里退回 Python 的真值规则
    (``''`` 为假、非空串为真),这一点写在模块的差异清单里。
    """
    if value is None:
        return False
    if isinstance(value, str):
        return len(value) > 0
    if isinstance(value, (bytes, bytearray)):
        return len(value) > 0
    return value != 0


def _three_valued_and(a: Any, b: Any) -> int:
    """三值逻辑的 AND:任一侧为"假"就是假,否则有 NULL 就是 NULL。"""
    if a is None:
        return 0 if (b is not None and not truthy(b)) else None  # type: ignore[return-value]
    if b is None:
        return 0 if not truthy(a) else None  # type: ignore[return-value]
    return 1 if (truthy(a) and truthy(b)) else 0


def _three_valued_or(a: Any, b: Any) -> int:
    """三值逻辑的 OR:任一侧为"真"就是真,否则有 NULL 就是 NULL。"""
    if a is None:
        return 1 if (b is not None and truthy(b)) else None  # type: ignore[return-value]
    if b is None:
        return 1 if truthy(a) else None  # type: ignore[return-value]
    return 1 if (truthy(a) or truthy(b)) else 0


class _LogicalBinary(FunctionBase):
    """``and`` / ``or`` 的公共骨架 —— 它们都是"变参 + 三值逻辑"。

    ``and(a, b, c)`` 在真实 ClickHouse 里是合法的(变参),
    折叠顺序从左到右;``NULL`` 的"传染"也遵循结合律,所以顺序不影响结果。
    """

    #: ``and`` 或 ``or``
    logic: str = "and"

    propagate_nulls = False

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 2, None)
        for t in args:
            inner = unwrap_nullable(t)
            if inner.is_numeric() or inner.is_integer() or inner.is_string() or inner.is_nothing():
                continue
            raise_error(
                ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT,
                f"Illegal type of argument for function {self.name}: {inner.get_name()}",
            )
        if any(t.is_nullable() for t in args):
            return DataTypeNullable(get_data_type("UInt8"))
        return get_data_type("UInt8")

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 2, None)
        ret = self.get_return_type([a.type for a in args])
        fold = _three_valued_and if self.logic == "and" else _three_valued_or
        if any(a.type.is_nullable() for a in args):
            cols = [full_column(a) for a in args]
            out = ret.create_column()
            for i in range(rows):
                acc = cols[0].get(i)
                for c in cols[1:]:
                    acc = fold(acc, c.get(i))
                out.append(None if acc is None else int(acc))
            return out
        # 没有 NULL 时三条路等价,直接向量化(整列一次算完)
        acc_arr = None
        for a in args:
            arr = np.asarray(full_column(a).to_list(), dtype=object)
            flags = np.fromiter((1 if truthy(v) else 0 for v in arr), dtype=np.uint8, count=rows)
            acc_arr = flags if acc_arr is None else (
                (acc_arr & flags) if self.logic == "and" else (acc_arr | flags)
            )
        assert acc_arr is not None
        return ColumnVector(np.uint8, acc_arr)


@_register("and", description="逻辑与(三值逻辑:NULL AND 0 = 0)")
class FunctionAnd(_LogicalBinary):
    """``and(a, b, ...)`` —— 逻辑与。SQL 里的 ``a AND b`` 就是它。

    .. code-block:: sql

       SELECT 1 AND 1, 1 AND 0, NULL AND 0, NULL AND 1, NULL AND NULL;
       -- 1   0   0   NULL   NULL

    第三、四列是本函数的全部价值:``NULL AND 0`` 是 **0**(未知也不会改变"假"),
    ``NULL AND 1`` 是 **NULL**(未知乘以真还是未知)。

    返回值是 ``UInt8``(0/1);任一侧可空时结果类型是 ``Nullable(UInt8)``。
    """

    logic = "and"


@_register("or", description="逻辑或(三值逻辑:NULL OR 1 = 1)")
class FunctionOr(_LogicalBinary):
    """``or(a, b, ...)`` —— 逻辑或。SQL 里的 ``a OR b`` 就是它。

    .. code-block:: sql

       SELECT 1 OR 0, 0 OR 0, NULL OR 1, NULL OR 0, NULL OR NULL;
       -- 1   0   1   NULL   NULL

    ``NULL OR 1 = 1`` 是最常被考的那一格:"未知"不妨碍我们已经知道有个"真"。
    这条规则让 ``WHERE cond1 OR cond2`` 里的 NULL 不会凭空"吞掉"整行。
    """

    logic = "or"


@_register("not", description="逻辑非(三值逻辑:NOT NULL = NULL)")
class FunctionNot(FunctionBase):
    """``not(x)`` —— 逻辑非,``NOT NULL`` 仍然是 ``NULL``。

    .. code-block:: sql

       SELECT NOT 1, NOT 0, NOT NULL;
       -- 0   1   NULL

    为什么要 ``propagate_nulls = False``? 因为 ``NOT NULL = NULL`` 虽然
    和默认的传播结果一样,但**理由不同**:默认传播是"有 NULL 就全 NULL",
    而这里是三值逻辑的一条真值表规则。写成显式的一行,读者才能看出
    "这两个 NULL 是一回事"。
    """

    propagate_nulls = False

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 1, 1)
        if args[0].is_nullable():
            return DataTypeNullable(get_data_type("UInt8"))
        return get_data_type("UInt8")

    def execute(
        self,
        args: Sequence[ColumnWithTypeAndName],
        rows: int,
        context: Any = None,
    ) -> IColumn:
        check_arity(self.name, args, 1, 1)
        ret = self.get_return_type([args[0].type])
        col = full_column(args[0])
        out = ret.create_column()
        for i in range(rows):
            v = col.get(i)
            out.append(None if v is None else (0 if truthy(v) else 1))
        return out


@_register("xor", description="逻辑异或(有 NULL 就是 NULL)")
class FunctionXor(FunctionBase):
    """``xor(a, b)`` —— 逻辑异或(不同为真)。

    .. code-block:: text

       xor(1, 0) = 1     xor(1, 1) = 0     xor(0, 0) = 0
       xor(NULL, 0) = NULL    xor(NULL, 1) = NULL

    与 ``and``/``or`` 不同,``xor`` **没有**"吃掉 NULL"的格子:
    异或的结果取决于两侧的**具体取值**(一个像 1 一个像 0),所以只要有一侧未知,
    结果就一定是未知。这也是这三兄弟里唯一一个"NULL 完全传播"的。

    只接受两个参数(真实 CH 里也是二元;要判"奇数个真"请用
    ``sum(x) % 2 = 1``)。
    """

    propagate_nulls = False

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        check_arity(self.name, args, 2, 2)
        if any(a.is_nullable() for a in args):
            return DataTypeNullable(get_data_type("UInt8"))
        return get_data_type("UInt8")

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
            if va is None or vb is None:
                out.append(None)
                continue
            out.append(1 if truthy(va) != truthy(vb) else 0)
        return out

"""**辅助 / 常量**类系统表 —— ``one`` 与 ``numbers``。

这一组只有三张表,但它们在整个 SQL 世界里出现频率极高,而且各自都有
一个"为什么它必须存在"的理由:

.. code-block:: text

   system.one       恰好一行 —— 让 "SELECT 常量" 有地方站
   system.numbers   0,1,2,3,… 无限流 —— 生成序列 / 造测试数据
   system.numbers_mt  上面的多线程版(本实现里行为相同,见下)

它们的共同点是:**它们是"表",但不对应任何存储** —— 没有文件、没有
目录、没有 part。是"计算出来的表"。真实 ClickHouse 把它们实现成真的
``IStorage``(``StorageSystemOne`` / ``StorageSystemNumbers``),同样
是为了"不给解释器开特例"。
"""

from __future__ import annotations

from typing import Any, Iterable, Tuple

from .base import (
    NumbersSystemTable,
    SimpleSystemTable,
    register_system_table,
)

__all__ = ["SystemOne", "register"]


class SystemOne(SimpleSystemTable):
    """``system.one`` —— 一张**恰好一行**的表,唯一的列是 ``dummy UInt8 = 0``。

    它回答的问题很朴素:"我手上有个不依赖任何表的表达式,该从哪儿 SELECT 它?"

    .. code-block:: sql

       SELECT 1 + 1;                 -- 没有 FROM,引擎内部等价于 system.one
       SELECT * FROM system.one;     -- 一行:dummy = 0
       SELECT (SELECT count() FROM t) AS n;   -- 标量子查询也从这里"起跳"

    真实 ClickHouse 里 ``SELECT 1`` 会被改写成 ``SELECT 1 FROM system.one``,
    这样"无 FROM 的查询"和"有 FROM 的查询"就走进**同一条执行流水线**了
    —— 少掉一整类特判。mini-clickhouse 的解释器对"无 FROM"也造了一个
    "恰好一行"的源,思路完全一样,只是没有真的去查这张表。
    """

    name = "one"
    columns = [("dummy", "UInt8")]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        yield (0,)


class SystemNumbers(NumbersSystemTable):
    """``system.numbers`` —— **无限**的 ``UInt64`` 序列,唯一列叫 ``number``。

    .. code-block:: sql

       SELECT * FROM system.numbers LIMIT 5;              -- 0,1,2,3,4
       SELECT count() FROM system.numbers LIMIT 1000000;  -- 一千万元素,不物化

    它是生成序列的**标准工具**:补日期空洞、造压测数据都靠它。
    唯一的列名是 ``number``(类型 ``UInt64``),与真实 ClickHouse 一字不差。

    .. warning::

       **真实 ClickHouse 的 ``system.numbers`` 是无界的** ——
       ``SELECT * FROM system.numbers``(不写 LIMIT)会一直流,
       永远不返回。mini-clickhouse 无法从接口拿到查询的 ``LIMIT``,
       所以给了一个**默认上界**(默认 10000 行,见
       :func:`minich.system.base.set_numbers_default_limit`)。
       这是本实现相对真实 ClickHouse 的**唯一一处保底**,
       教程里凡用此表都应显式写 ``LIMIT``。
    """

    def __init__(self) -> None:
        super().__init__("numbers", mt=False)


class SystemNumbersMT(NumbersSystemTable):
    """``system.numbers_mt`` —— ``system.numbers`` 的"多线程版"。

    真实 ClickHouse 里,``numbers_mt`` 会**多线程并行**生成并读取,
    所以 ``SELECT count() FROM system.numbers_mt`` 在大数量级下更快。
    表名里的 ``_mt`` 就是 "multi-threaded"。

    .. note::

       **mini-clickhouse 里它与 ``system.numbers`` 行为完全相同** ——
       我们是纯 Python,受 GIL 所限,多线程不会带来真正的并行
       (CPU 密集的生成器更是如此)。保留这张表只为一个理由:
       教程里引用 ``numbers_mt`` 的 SQL 不会报 "table does not exist"。
       这个"名字在、能力不在"的差异,与 ``system.numbers`` 的无界性一样,
       属于**必须写进报告**的不忠实之处。
    """

    def __init__(self) -> None:
        super().__init__("numbers_mt", mt=True)


def register() -> None:
    """把本模块的表装进全局注册表。"""
    for table in (SystemOne(), SystemNumbers(), SystemNumbersMT()):
        register_system_table(table)

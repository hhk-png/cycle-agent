"""**复制**类系统表 —— ``system.replicas``。

复制表(``ReplicatedMergeTree``)最反直觉的一点是:**它的存储格式和普通
MergeTree 完全一样** —— 同样的 part、同样的索引、同样的磁盘布局。区别
全在**协调**:谁去写 Keeper 日志、谁去拉 part、谁来做合并。所以复制出
问题时,数据本身看不出来,必须看**协调状态** —— 而 ``system.replicas``
就是那份状态的看板。

.. code-block:: text

   ┌─ system.replicas 的三个"一眼判死活"的列 ───────────────────────────┐
   │                                                                     │
   │  absolute_delay   队列里最老的、还没执行的 entry 等了多久(秒)     │
   │                   ★ 不为 0 ⇒ 这个副本落后了,读它会读到旧数据      │
   │                                                                     │
   │  queue_size       还没执行的 entry 条数(压力有多大)              │
   │                                                                     │
   │  is_readonly      只读 —— 会话断了 / 队列积压 / part 丢了          │
   │                   ★ 复制表拒绝写入时,答案基本都在这里             │
   └─────────────────────────────────────────────────────────────────────┘

另外一对列 ``log_pointer`` / ``log_max_index`` 是**进度条**:
前者是"我处理到哪了",后者是"Keeper 里最新到哪了",两者相等才是
完全跟上。``active_replicas`` / ``total_replicas`` 则告诉你"集群还剩下
几个活的副本"——真实 ClickHouse 里两者都由 Keeper 的临时节点数决定,
所以副本挂掉时**两个数会一起掉**,这不是 bug。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .base import SimpleSystemTable, register_system_table

__all__ = ["SystemReplicas"]


class SystemReplicas(SimpleSystemTable):
    """``system.replicas`` —— 回答"这个副本落后了吗、还在写吗、集群有几个人"。

    每条 ``SELECT`` 之前先看它一眼,是复制表运维的习惯动作:

    .. code-block:: sql

       -- ① 有没有副本掉队了(absolute_delay 是关键)
       SELECT database, table, replica_name, absolute_delay, queue_size,
              log_pointer, log_max_index, is_readonly
       FROM system.replicas ORDER BY absolute_delay DESC;

       -- ② 谁在当合并 leader(同一时刻每个分片只有一个)
       SELECT replica_name FROM system.replicas WHERE is_leader;

       -- ③ 是不是有副本掉线了(active 少于 total)
       SELECT database, table, active_replicas, total_replicas
       FROM system.replicas WHERE active_replicas < total_replicas;

    ``absolute_delay`` 与 ``queue_size`` 都是**真实读数**(由
    :meth:`minich.replication.Replica.get_status` 从 Keeper 的状态现算),
    不是常量 —— 这正是这张看板的价值所在。

    .. note::

       **mini-clickhouse 的复制是"可选的":只有挂上了集群仿真台,
       这里才会有行。**

       数据来自 ``ctx.replicas`` / ``ctx.replication``(等价地指向一个
       :class:`minich.replication.LocalCluster`,或是一个"副本名 → 状态"
       的容器)。**没挂的实例上这张表为空** —— 这是正确答案:
       一台没配复制的服务器**没有**副本可报告,而不是"复制坏了"。

       ``last_queue_update`` 在引擎侧存的是**毫秒**时间戳,而真实
       ClickHouse 的 ``DateTime`` 列是**秒** —— 转换在这里做(见
       :func:`_epoch_to_text`),这样两边的表读出来是同一个时间语义。
    """

    name = "replicas"
    columns = [
        ("database", "String"),
        ("table", "String"),
        ("engine", "String"),
        ("is_leader", "UInt8"),
        ("is_readonly", "UInt8"),
        ("absolute_delay", "UInt32"),
        ("queue_size", "UInt32"),
        ("inserts_in_queue", "UInt32"),
        ("merges_in_queue", "UInt32"),
        ("log_max_index", "UInt64"),
        ("log_pointer", "UInt64"),
        ("total_replicas", "UInt8"),
        ("active_replicas", "UInt8"),
        ("last_queue_update", "DateTime"),
        ("zookeeper_path", "String"),
        ("replica_name", "String"),
        ("replica_path", "String"),
        ("columns_version", "Int32"),
        ("lost_part_count", "UInt64"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        for status in _replica_statuses(ctx):
            yield _replica_row(status)


# ══════════════════════════════════════════════════════════════════════════
# 数据源适配
# ══════════════════════════════════════════════════════════════════════════


def _replica_statuses(ctx: Any) -> List[Any]:
    """从上下文里把"每个副本的状态"掏出来 —— 掏不到返回空列表。

    接受几种形状(按优先级):

    1. ``ctx.replicas`` / ``ctx.replication`` 是一个
       :class:`minich.replication.LocalCluster` —— 它有 ``status_rows()``;
    2. 是一个 ``{副本名: Replica}`` 的 dict(或 list);
    3. 里面的元素本身是 :class:`ReplicatedTableStatus`(或有 ``as_row()``)。

    任何一种形状读到的东西都是**引擎算出来的真实状态**,
    这里只做"取出来、摆平"。
    """
    source = getattr(ctx, "replicas", None)
    if source is None:
        source = getattr(ctx, "replication", None)
    if source is None:
        return []

    # ① LocalCluster.status_rows() → {名字: dict}
    fn = getattr(source, "status_rows", None)
    if callable(fn):
        try:
            rows = fn()
        except Exception:  # pragma: no cover
            rows = None
        if rows is not None:
            return _coerce_status_list(rows)

    # ② 容器:优先取 `.replicas`,否则源本身若可迭代就用它
    container: Any = getattr(source, "replicas", None)
    if container is None and isinstance(source, (dict, list, tuple)):
        container = source
    if container is None:
        return []
    return _coerce_status_list(container)


def _coerce_status_list(container: Any) -> List[Any]:
    """把一个容器(dict / list / 单元素)摊平成"状态 dict"的列表。"""
    if container is None:
        return []
    if isinstance(container, dict):
        items: Iterable[Any] = container.values()
    elif isinstance(container, (list, tuple)):
        items = container
    else:
        items = [container]
    out: List[Any] = []
    for item in items:
        row = _status_as_dict(item)
        if row is not None:
            out.append(row)
    return out


def _status_as_dict(item: Any) -> Optional[Dict[str, Any]]:
    """把"一个副本状态"变成 dict;认不出来返回 ``None``。

    认得三种:已经是 dict;有 ``as_row()``(:class:`ReplicatedTableStatus`);
    有 ``get_status()``(:class:`Replica` —— 先取状态再 ``as_row()``)。
    """
    if item is None:
        return None
    if isinstance(item, dict):
        return item
    as_row = getattr(item, "as_row", None)
    if callable(as_row):
        try:
            row = as_row()
        except Exception:  # pragma: no cover
            return None
        return row if isinstance(row, dict) else None
    get_status = getattr(item, "get_status", None)
    if callable(get_status):
        try:
            status = get_status()
        except Exception:  # pragma: no cover
            return None
        as_row = getattr(status, "as_row", None)
        if callable(as_row):
            row = as_row()
            return row if isinstance(row, dict) else None
    return None


def _replica_row(status: Any) -> Tuple[Any, ...]:
    """把一份副本状态映射成 ``system.replicas`` 的一行。"""
    return (
        str(_get(status, "database", "")),
        str(_get(status, "table", "")),
        str(_get(status, "engine", "ReplicatedMergeTree") or "ReplicatedMergeTree"),
        int(bool(_get(status, "is_leader", False))),
        int(bool(_get(status, "is_readonly", False))),
        int(_get(status, "absolute_delay", 0) or 0),
        int(_get(status, "queue_size", 0) or 0),
        int(_get(status, "inserts_in_queue", 0) or 0),
        int(_get(status, "merges_in_queue", 0) or 0),
        int(_get(status, "log_max_index", 0) or 0),
        int(_get(status, "log_pointer", 0) or 0),
        int(_get(status, "total_replicas", 0) or 0),
        int(_get(status, "active_replicas", 0) or 0),
        _epoch_to_text(_get(status, "last_queue_update", 0)),
        str(_get(status, "zookeeper_path", "") or ""),
        str(_get(status, "replica_name", "") or ""),
        str(_get(status, "replica_path", "") or ""),
        int(_get(status, "columns_version", 0) or 0),
        int(_get(status, "lost_part_count", 0) or 0),
    )


def _get(obj: Any, key: str, default: Any) -> Any:
    """从 dict 或对象里取一个字段(读不到返回 ``default``)。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _epoch_to_text(value: Any) -> str:
    """把"时间"渲染成 ``DateTime`` 能收的文本。

    引擎侧的 ``last_queue_update`` 用的是**毫秒**时间戳,而这里要填的是
    ``DateTime``(秒)。所以见招拆招:

    .. code-block:: text

       None / "" / 0     → "1970-01-01 00:00:00"(= "从未更新")
       datetime 对象     → 直接格式化
       数值 > 1e12       → 毫秒,除以 1000 再换算
       其它数值          → 当成秒

    这条 >1e12 的判断不是魔法:以**毫秒**表示的当前时间必然是 13 位数,
    而秒只有 10 位 —— 两者相差三个数量级,不会误判。
    """
    if value is None or value == "" or value == 0:
        return "1970-01-01 00:00:00"
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    try:
        secs = float(value)
    except (TypeError, ValueError):  # pragma: no cover
        return str(value)
    if secs > 1e12:            # 毫秒时间戳
        secs /= 1000.0
    try:
        return _dt.datetime.fromtimestamp(secs).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):  # pragma: no cover
        return "1970-01-01 00:00:00"


# ══════════════════════════════════════════════════════════════════════════
# 注册
# ══════════════════════════════════════════════════════════════════════════


def register() -> None:
    register_system_table(SystemReplicas())

"""TTL —— "数据自己会过期"。

.. code-block:: text

   ╔══════════════════════════════════════════════════════════════════════╗
   ║  TTL 是 ClickHouse 里最"省运维"的一个特性:它的定位是"少查就删"      ║
   ╚══════════════════════════════════════════════════════════════════════╝

   .. code-block:: sql

      -- ① 行级 TTL:30 天前的明细自动删除(最常见的监控/日志场景)
      CREATE TABLE logs (d Date, msg String)
      ENGINE = MergeTree ORDER BY d
      TTL d + INTERVAL 30 DAY DELETE;

      -- ② 冷热分层:7 天前的数据挪到慢盘
      TTL d + INTERVAL 7 DAY TO DISK 'slow_disk';

      -- ③ 重新压缩:3 天前的数据换更省空间的编码
      TTL d + INTERVAL 3 DAY RECOMPRESS CODEC(ZSTD(9));

      -- ④ 列级 TTL:某个列的过期时间与整行不同
      CREATE TABLE t (
          id   UInt64,
          raw  String TTL d + INTERVAL 1 DAY DELETE     -- 只有 raw 列被清空
      ) ENGINE = MergeTree ORDER BY id TTL d + INTERVAL 90 DAY;

   这些都是"**声明式**"的:你告诉 ClickHouse 规则,它自己找时间执行 ——
   没有 cron、没有外部脚本、没有运维半夜起来删数据的日子。

.. code-block:: text

   ┌──────────────────────────────────────────────────────────────────────┐
   │  ★ 最大的性能杠杆:``ttl_only_drop_parts``                          │
   └──────────────────────────────────────────────────────────────────────┘

   TTL 到期时,ClickHouse 有两条路可走:

   .. code-block:: text

     ┌── 路线 A:逐行删(ttl_only_drop_parts = 0,默认)────────────────┐
     │                                                                │
     │   part 里有 1000 万行,其中 3 行过期了                          │
     │        │                                                        │
     │        └── 读出所有 1000 万行 → 丢掉 3 行 → **写回一整份 part** │
     │            ↑ 代价 = 重写整个 part(可能几百 MB 的 IO)          │
     └────────────────────────────────────────────────────────────────┘

     ┌── 路线 B:整 part 丢(ttl_only_drop_parts = 1)────────────────┐
     │                                                                │
     │   如果 part 里**所有**行都过期了                               │
     │        │                                                        │
     │        └── 直接把 part 目录删掉,或者标记 obsolete             │
     │            ↑ 代价 = 元数据操作,O(1)                            │
     └────────────────────────────────────────────────────────────────┘

   路线 B 快上百倍 —— **但前提是"整个 part 都过期了"**。
   这就是为什么 TTL 表一定要**和分区配合使用**:

   .. code-block:: sql

      PARTITION BY toYYYYMM(d)          -- 按月的 part
      TTL d + INTERVAL 6 MONTH DELETE   -- 半年前的整个月一起过期

   于是每个月的 part 在"月末+6个月"之后会**整体**过期,走路线 B,
   一次元数据操作就回收几十 GB。而如果只有行级 TTL、没有分区,
   就永远是路线 A —— 反复重写。

   .. warning::

      mini-clickhouse 的 ``ttl_only_drop_parts`` 默认与真实 ClickHouse
      一致(``0``)。但我们的实现**额外**做了一件事:当发现
      "这个 part 的所有行都过期"时,无论开关如何都直接丢弃它
      (而不是走路线 A 写一个空 part)。真实 ClickHouse 在
      ``ttl_only_drop_parts=0`` 下会产出一个空 part 再删掉它,
      结果是"部分重写" —— 这一点在 ``merge_tree.py`` 的
      ``_apply_ttl`` 注释里标出来了。
"""

from __future__ import annotations

import datetime as _dt
import time as _time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ...block import Block, ColumnWithTypeAndName
from ...errors import ErrorCode, raise_error
from ...types import DataType, get_data_type
from .expression import evaluate_expression

__all__ = [
    "TTLDescription",
    "TTLInfo",
    "TTLRowsRemover",
    "ttl_value_in_seconds",
    "evaluate_ttl_bounds",
    "DEFAULT_TTL_ONLY_DROP_PARTS",
]


#: 真实 ClickHouse 的默认值 —— ``0`` 表示"逐行删、顺便重写 part"。
DEFAULT_TTL_ONLY_DROP_PARTS = 0

SECONDS_PER_DAY = 86400

#: "永不过期" —— 用来表示**值为 NULL 的 TTL 表达式**。
#:
#: .. code-block:: text

#:    行级:NULL 的行**永不过期**(真实 ClickHouse 的语义)
#:    part 级:只要 part 里**有一行**是 NULL,这个 part 就**不能整块丢弃**
#:
#: 为什么要一个具体的时间戳而不是 ``None``?
#: 因为 part 级判断只做一次 ``now > max(ttl)`` 的比较。如果 NULL 被
#: **跳过**(而不是当成"极大"),一个"非 NULL 行全部过期 + 有 NULL 行"的
#: part 看起来就是"整块过期",于是被整块删掉 —— **NULL 行被误删**。
#: 取 9999-12-31T23:59:59 而不是浮点 inf,是为了让 ``ttl.txt`` 里
#: 那个时间戳还能被 ``strftime`` 格式化。
TTL_NEVER_EXPIRES = 253402300799


@dataclass
class TTLDescription:
    """一条 TTL 规则 —— ``TTL <expr> <action> [WHERE ...]``。

    ``action`` 的四种取值正好对应四种资源维度:

    ==================  =====================================================
    action              回收的是什么
    ==================  =====================================================
    ``DELETE``          **行数**(最常用)
    ``TO DISK``         **盘位**:热数据挪到慢盘 / 冷盘
    ``TO VOLUME``       **卷位**:一组盘的抽象(见 ``storage_policy``)
    ``RECOMPRESS``      **磁盘字节**:换个更激进的编码
    ==================  =====================================================
    """

    action: str = "DELETE"
    expr: Any = None
    #: ``TO DISK`` / ``TO VOLUME`` 的目标名
    target: Optional[str] = None
    #: ``RECOMPRESS CODEC(...)`` 的编解码器链
    codec: Optional[str] = None
    where: Any = None
    group_by: List[Any] = field(default_factory=list)
    text: str = ""

    def kind(self) -> str:
        return self.action.upper()

    def describe(self) -> str:  # pragma: no cover
        if self.text:
            return self.text
        return f"TTL {self.expr} {self.action}"


@dataclass
class TTLInfo:
    """一个 part 的 TTL 元数据 —— ``ttl.txt`` 的内容。

    真实 ClickHouse 只记**每个动作在 part 内部的极值时间**
    (而不是"每一行的过期时间"),因为做决策只需要极值:

    .. code-block:: text

       delete_at = max(ttl_expr)  ← 这个 part 里**最晚**过期的那个时间点
                                    "现在 > delete_at" ⟹ **整个 part 都过期了**
       recompress_at = min(ttl_expr) ← "现在 > recompress_at" ⟹ 有行需要重压缩了
       move_at       = min(ttl_expr)

    这就是"整 part 丢弃"能被判定的原因:一个比较就够了。
    """

    delete_at: Optional[int] = None
    delete_min: Optional[int] = None
    recompress_at: Optional[int] = None
    recompress_min: Optional[int] = None
    to_disk_at: Optional[int] = None
    to_disk_min: Optional[int] = None
    to_volume_at: Optional[int] = None
    to_volume_min: Optional[int] = None

    def any_expired(self, now: int) -> bool:
        for v in (self.delete_min, self.recompress_min, self.to_disk_min, self.to_volume_min):
            if v is not None and now > v:
                return True
        return False

    def whole_part_expired_for_delete(self, now: int) -> bool:
        return self.delete_at is not None and now > self.delete_at

    def to_ttl_txt(self) -> Dict[str, Any]:
        """``ttl.txt`` 的内容。

        .. warning::

           **格式与真实 ClickHouse 不同。** 真实 CH 的 ``ttl.txt`` 是
           ``{"delete": "2024-...", "recompress": "..."}`` —— 每个动作只记
           **一个**时间(它内部另有一套 min/max 推导)。我们记
           ``{"delete": {"min": ..., "max": ...}}`` 的**嵌套**形式,因为
           我们的判定需要两个极值:

           .. code-block:: text

              max(delete) = "最晚过期的那一行" → now > max ⟹ **整 part 过期**
              min(delete) = "最早过期的那一行" → now > min ⟹ **有行要删**

           少一个极值,要么丢掉"整 part 丢弃"这条快路径,
           要么每次都要重扫数据 —— 那 ttl.txt 就白写了。
        """

        def fmt(v: Optional[int]) -> Optional[str]:
            if v is None:
                return None
            return _dt.datetime.utcfromtimestamp(v).strftime("%Y-%m-%dT%H:%M:%S")

        return {
            "delete": {"min": fmt(self.delete_min), "max": fmt(self.delete_at)},
            "recompress": {"min": fmt(self.recompress_min), "max": fmt(self.recompress_at)},
            "to_disk": {"min": fmt(self.to_disk_min), "max": fmt(self.to_disk_at)},
            "to_volume": {"min": fmt(self.to_volume_min), "max": fmt(self.to_volume_at)},
        }


# ══════════════════════════════════════════════════════════════════════════
# 求值
# ══════════════════════════════════════════════════════════════════════════


def ttl_value_in_seconds(value: Any, dtype: DataType) -> Optional[int]:
    """把 TTL 表达式的值统一到"epoch 秒"。

    .. code-block:: text

       Date      内部存的是**天数**   → × 86400
       Date32    同上
       DateTime  内部就是**秒**       → 原样
       DateTime64 内部是"按精度缩放过的秒" → 按精度还原
       **NULL**  → :data:`TTL_NEVER_EXPIRES`(永不过期)

    **不统一就会出错** —— 这是 TTL 最隐蔽的一个坑:
    ``Date`` 列算出来的 19766(天数)与 ``now()`` 的 17 亿(秒)比大小,
    永远是"没过期",TTL **静默失效**。

    NULL 的处理同样要害:真实 ClickHouse 的语义是"TTL 表达式为 NULL
    的行**永不过期**"。把它当成"永不过期"(而不是"跳过")还有一个
    连带作用 —— **part 级的 max 会变成极大值**,于是"整块丢弃"这条
    快路径自动失效,不会把带 NULL 的 part 整块删掉。
    """
    if value is None:
        return TTL_NEVER_EXPIRES
    # ``Nullable(T)`` 的物理表示就是 ``T``(+ 空位图),所以按**内层**类型
    # 决定换算规则。不剥掉这层包装,``getattr(dtype, "scale")`` 会取不到
    # ``DateTime64`` 的精度,于是"毫秒值当成秒"→ TTL 立刻认为已过期。
    # (``is_date_time`` / ``is_date`` 现在也会透传,但 ``scale`` 不会 ——
    # 它不是 DataType 的公共接口,只能在这里显式剥包装。)
    if dtype.is_nullable():
        dtype = dtype.nested  # type: ignore[attr-defined]
    if dtype.is_date_time():
        # ``DateTime`` 内部就是秒;``DateTime64(p)`` 内部是"按 p 位精度缩放过的
        # 整数"(毫秒/微秒…),必须按精度还原成秒 —— 不还原就会拿 1e12 级的
        # 毫秒值去和 ``now()`` 的 1e9 级秒值比,TTL **静默失效**。
        scale = getattr(dtype, "scale", None)
        if scale is None:
            return int(value)
        try:
            return int(value) // int(scale())
        except (TypeError, ValueError):  # pragma: no cover - 防御性兜底
            return int(value)
    if dtype.is_date():
        return int(value) * SECONDS_PER_DAY
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        from ...types.scalars import date_to_days

        return date_to_days(value) * SECONDS_PER_DAY
    return TTL_NEVER_EXPIRES


def evaluate_ttl_bounds(
    ttl: TTLDescription,
    block: Block,
    context: Any = None,
) -> Tuple[Optional[int], Optional[int]]:
    """算出这条 TTL 在这个 block 上的 ``(min, max)`` 过期时间(epoch 秒)。

    NULL 值按 :data:`TTL_NEVER_EXPIRES` 参与 min/max —— 见
    :func:`ttl_value_in_seconds` 的说明("不能跳过 NULL")。
    """
    if ttl.expr is None:
        return None, None
    col = evaluate_expression(ttl.expr, block, context)
    dtype = col.type
    c = col.column.convert_to_full_column() if col.column.is_const() else col.column
    if c.size() == 0:
        return None, None
    lo: Optional[int] = None
    hi: Optional[int] = None
    for i in range(c.size()):
        secs = ttl_value_in_seconds(c.get(i), dtype)
        if secs is None:
            secs = TTL_NEVER_EXPIRES
        lo = secs if lo is None else min(lo, secs)
        hi = secs if hi is None else max(hi, secs)
    return lo, hi


def compute_ttl_info(
    ttls: Sequence[TTLDescription],
    block: Block,
    context: Any = None,
) -> TTLInfo:
    """对一批 TTL 规则算出 :class:`TTLInfo`(写 part 时调用)。"""
    info = TTLInfo()
    for ttl in ttls:
        lo, hi = evaluate_ttl_bounds(ttl, block, context)
        kind = ttl.kind()
        if kind == "DELETE":
            info.delete_at = _max_of(info.delete_at, hi) if hi is not None else info.delete_at
            info.delete_min = _min_of(info.delete_min, lo) if lo is not None else info.delete_min
        elif kind == "RECOMPRESS":
            info.recompress_at = _max_of(info.recompress_at, hi) if hi is not None else info.recompress_at
            info.recompress_min = _min_of(info.recompress_min, lo) if lo is not None else info.recompress_min
        elif kind == "TO DISK":
            info.to_disk_at = _max_of(info.to_disk_at, hi) if hi is not None else info.to_disk_at
            info.to_disk_min = _min_of(info.to_disk_min, lo) if lo is not None else info.to_disk_min
        elif kind == "TO VOLUME":
            info.to_volume_at = _max_of(info.to_volume_at, hi) if hi is not None else info.to_volume_at
            info.to_volume_min = _min_of(info.to_volume_min, lo) if lo is not None else info.to_volume_min
    return info


def _max_of(a: Optional[int], b: int) -> int:
    return b if a is None else max(a, b)


def _min_of(a: Optional[int], b: int) -> int:
    return b if a is None else min(a, b)


def merge_ttl_infos(a: Optional[TTLInfo], b: Optional[TTLInfo]) -> Optional[TTLInfo]:
    """两个 part 合并时,TTL 信息也要合并(取并集)。"""
    if a is None:
        return b
    if b is None:
        return a
    out = TTLInfo()
    out.delete_at = _combine_max(a.delete_at, b.delete_at)
    out.delete_min = _combine_min(a.delete_min, b.delete_min)
    out.recompress_at = _combine_max(a.recompress_at, b.recompress_at)
    out.recompress_min = _combine_min(a.recompress_min, b.recompress_min)
    out.to_disk_at = _combine_max(a.to_disk_at, b.to_disk_at)
    out.to_disk_min = _combine_min(a.to_disk_min, b.to_disk_min)
    out.to_volume_at = _combine_max(a.to_volume_at, b.to_volume_at)
    out.to_volume_min = _combine_min(a.to_volume_min, b.to_volume_min)
    return out


def _combine_max(a: Optional[int], b: Optional[int]) -> Optional[int]:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _combine_min(a: Optional[int], b: Optional[int]) -> Optional[int]:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


# ══════════════════════════════════════════════════════════════════════════
# 行级删除器
# ══════════════════════════════════════════════════════════════════════════


class TTLRowsRemover:
    """行级 TTL 的执行器 —— 真实 ClickHouse 里这个类就叫 ``TTLRowsRemover``。

    它只做一件事:**给定一个 block,把过期的行删掉**。
    "决定删不删"是 TTL 策略层的事(``TTLInfo`` 的极值比较),
    这里只负责"删"这个动作 —— 职责清晰带来一个好处:
    它既能在**合并**时被调用(最常见),也能在 ``OPTIMIZE`` 时被调用。

    .. code-block:: text

       输入 block(1000 万行)
          │
          ├─ 对每条 TTL DELETE 规则求 ttl_expr → 每行一个"过期时间"
          │
          ├─ 掩码 = 所有规则里"现在还没过期"的行(OR 起来)
          │
          └─ block.filter(掩码)
             ↑
        注意语义:多条 TTL DELETE 规则是 **OR** 关系
        ("任意一条说过期就删"),而不是 AND。
    """

    def __init__(self, ttls: Sequence[TTLDescription], now: Optional[int] = None) -> None:
        self.ttls = [t for t in ttls if t.kind() == "DELETE" and t.expr is not None]
        if self.ttls and now is None:
            raise_error(
                ErrorCode.LOGICAL_ERROR,
                "TTLRowsRemover needs a 'now' timestamp; passing None would make "
                "the result depend on wall-clock time inside the filter loop",
            )
        self.now = now if now is not None else int(_time.time())

    # ── 条件 ────────────────────────────────────────────────────────────

    def has_row_filter(self) -> bool:
        """是否有任何一条 DELETE 规则带 ``WHERE``。

        .. warning::

           带 ``WHERE`` 时**必须关掉"整块丢弃"那条快路径** ——
           因为 part 级的极值只反映"过期时间",完全不知道
           "哪些行满足 WHERE"。一个 part 可能整体过期,但只有一部分行
           匹配 WHERE,整块丢掉就会**删掉不该删的行**。
        """
        return any(t.where is not None for t in self.ttls)

    def should_remove_anything(self, info: Optional[TTLInfo]) -> bool:
        """先用 part 级极值判断"有没有可能删到东西" —— 一次比较 vs 全表扫描。"""
        if info is None or not self.ttls:
            return False
        if info.delete_min is None:
            return False
        return self.now > info.delete_min

    def process(self, block: Block, context: Any = None) -> Block:
        """就地删掉过期行,返回同一个 block。

        .. code-block:: text

           一行被删除 ⟺ 「它的 TTL 表达式 <= now」**且**「它满足 TTL 的 WHERE」

           TTL d + INTERVAL 30 DAY DELETE WHERE id = 1
               ↑ 只对 id = 1 的行生效;其它行**永远不动**(哪怕 d 很老)

        ``WHERE`` 不是"优化",而是**语义的一部分**:它限定了这条 TTL
        "管哪些行"。忽略它 = 删掉用户明确不想删的数据。

        多条 TTL DELETE 规则之间是 **OR**(任意一条判它过期就删),
        所以这里从"全部保留"出发,逐条规则把命中的行划掉。
        """
        if not self.ttls or block.rows() == 0:
            return block
        rows = block.rows()
        keep = [True] * rows
        for ttl in self.ttls:
            col = evaluate_expression(ttl.expr, block, context)
            dtype = col.type
            c = col.column.convert_to_full_column() if col.column.is_const() else col.column
            where_ok = None
            if ttl.where is not None:
                from .expression import evaluate_predicate

                where_ok = evaluate_predicate(ttl.where, block, context)
            for i in range(rows):
                secs = ttl_value_in_seconds(c.get(i), dtype)
                if secs is None:
                    secs = TTL_NEVER_EXPIRES
                if secs > self.now:
                    continue          # 还没过期
                if where_ok is not None and not where_ok[i]:
                    continue          # 过期了,但这条 TTL 不管这一行
                keep[i] = False       # 过期且受这条规则管辖 → 删
        block.filter(keep)
        return block

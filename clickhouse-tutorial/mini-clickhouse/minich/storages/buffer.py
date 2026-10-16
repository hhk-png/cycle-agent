"""``Buffer`` 引擎 —— 在内存里攒批,达到阈值再**成批**刷入目标表。

.. code-block:: text

   CREATE TABLE buf ENGINE = Buffer(db, target, 1,
                                    10, 100,        -- min_time, max_time(秒)
                                    10000, 1000000, -- min_rows, max_rows
                                    10000000, 100000000)   -- min_bytes, max_bytes

   INSERT INTO buf VALUES (...)      -- 只进内存
   ... 攒够 min_rows 且过了 min_time ...
   → 一次性写入 db.target            -- 目标表看到的是"大批量写入"

**它存什么?** 存"还没刷出去的写入"。数据先落进第 0 层内存缓冲,
攒到阈值后**晋升**到第 1 层,最后一层再写进真正的目标表。

**它不能做什么?**
* 不能保证不丢数据 —— 见下面的 warning;
* 不能保证读到的顺序/时机;``SELECT`` 会把"目标表已有的数据 + 尚未刷出的缓冲"
  拼在一起(粗粒度地近似真实行为);
* 不能有索引、分区、主键。

**什么时候该选它(而不是直接写 MergeTree)?**
当"写入的**频率**远高于目标表能舒服承受的频率"时。典型场景:

* 目标表是 ``MergeTree``,而你是**每几毫秒写一行**的上报接口 ——
  直接写会产生海量小 part,后台 merge 跟不上;
  ``Buffer`` 把它们攒成每 10 万行一个批次再写;
* 目标是**分布式表**(``Distributed``)—— ``Buffer`` 让每次网络往返带更多数据。

.. code-block:: text

   多层缓冲的晋升机制(num_layers = 3):

     INSERT ─▶ [层0] ──满/超时──▶ [层1] ──满/超时──▶ [层2] ──写穿──▶ 目标表
               ▲                                                      │
               └────────────── FLUSH TABLE / 进程正常退出 ─────────────┘

   每一层用**同一组阈值**(min_*/max_*)。层数越多,"越晚越粗":
   层 0 快速把小批攒起来、层 2 只在大批时才惊动目标表。

.. warning::

   **``Buffer`` 会在非正常退出时丢数据。** 数据在"刷出去"之前只存在于内存,
   所以进程被 ``kill -9``、OOM、断电时,还在缓冲里的行**永远消失**,
   目标表不会知道有过这些数据。这是 Buffer 引擎最基本的取舍 ——
   它用"可能丢一小段"换"目标表写入更平稳"。要既不丢又平稳,
   得让上游能重放(至少一次语义),或者干脆不用 Buffer。

   mini-clickhouse 里 ``shutdown()`` 会尽力 ``flush()`` 一次(进程正常关闭),
   但**不**为崩溃场景兜底 —— 这与真实 ClickHouse 一致。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Sequence

from ..block import Block, Chunk, ColumnWithTypeAndName
from ..errors import ErrorCode, raise_error
from ._common import (
    _align_block,
    _check_chunk,
    _make_chunk,
    _max_block_size,
    _read_limit,
    _requested_columns,
    _settings_of,
    _stored_columns,
)
from .base import IStorage, StorageFeatures, register_storage_engine

__all__ = ["StorageBuffer"]

#: 真实 ClickHouse 的默认阈值(``Buffer`` 的 9 个参数的默认值)
_DEFAULTS = {
    "num_layers": 1,
    "min_time": 10,
    "max_time": 100,
    "min_rows": 10000,
    "max_rows": 1000000,
    "min_bytes": 10000000,
    "max_bytes": 100000000,
}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class _BufferLayer:
    """一层内存缓冲 —— 就是"一堆还没晋升的 Block"。"""

    __slots__ = ("blocks", "first_time", "rows", "bytes")

    def __init__(self) -> None:
        self.blocks: List[Block] = []
        self.first_time: Optional[float] = None
        self.rows = 0
        self.bytes = 0

    def append(self, block: Block) -> None:
        if block.rows() == 0:
            return
        if self.first_time is None:
            self.first_time = time.monotonic()
        self.blocks.append(block)
        self.rows += block.rows()
        self.bytes += sum(c.column.byte_size() for c in block.data)

    def drain(self) -> List[Block]:
        blocks = self.blocks
        self.blocks = []
        self.first_time = None
        self.rows = 0
        self.bytes = 0
        return blocks


class StorageBuffer(IStorage):
    """缓冲引擎 —— 见模块文档。"""

    engine_name = "Buffer"

    features = StorageFeatures(parallel_read=False)

    def __init__(self, table, storage_id, context=None, engine_args=None, params=None) -> None:
        super().__init__(table, storage_id, context, engine_args)
        self.params: Dict[str, Any] = dict(params or {})
        args = list(engine_args or [])
        # 前两个参数是目标表名(database, table);其余是阈值。
        self._target_database: Optional[str] = None
        self._target_table_name: Optional[str] = None
        numeric: List[Any] = []
        for arg in args:
            s = arg if isinstance(arg, str) else getattr(arg, "name", None) or getattr(
                arg, "value", arg
            )
            if isinstance(s, str) and not s.replace(".", "", 1).isdigit():
                if self._target_database is None:
                    self._target_database = s
                elif self._target_table_name is None:
                    self._target_table_name = s
            else:
                numeric.append(s)
        thresholds = self._parse_thresholds(numeric)
        self.num_layers = max(1, thresholds["num_layers"])
        self.min_time = max(0.0, float(thresholds["min_time"]))
        self.max_time = max(0.0, float(thresholds["max_time"]))
        self.min_rows = thresholds["min_rows"]
        self.max_rows = thresholds["max_rows"]
        self.min_bytes = thresholds["min_bytes"]
        self.max_bytes = thresholds["max_bytes"]

        self._layers: List[_BufferLayer] = [_BufferLayer() for _ in range(self.num_layers)]
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def _parse_thresholds(self, numeric: Sequence[Any]) -> Dict[str, Any]:
        order = ["num_layers", "min_time", "max_time", "min_rows",
                 "max_rows", "min_bytes", "max_bytes"]
        out = dict(_DEFAULTS)
        for name, value in zip(order, numeric):
            out[name] = _as_int(value, out[name]) if name != "min_time" and name != "max_time" \
                else _as_float(value, out[name])
        # params 里的显式设置优先级最高(便于测试直接控制刷新时机)
        for name in order:
            if name in self.params:
                out[name] = self.params[name]
        return out

    # ── 元信息 ──────────────────────────────────────────────────────────

    def get_columns(self) -> List[ColumnWithTypeAndName]:
        return _stored_columns(self.table)

    def get_name(self) -> str:
        target = f"{self._target_database or '?'}.{self._target_table_name or '?'}"
        return f"Buffer({target}, {self.num_layers})"

    def buffered_rows(self) -> int:
        return sum(layer.rows for layer in self._layers)

    def size_in_bytes(self) -> int:
        return sum(layer.bytes for layer in self._layers)

    def row_count(self) -> int:
        target = self._target_table()
        base = target.row_count() if target is not None else 0
        return base + self.buffered_rows()

    def describe_stats(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "layers": self.num_layers,
            "min_time": self.min_time,
            "max_time": self.max_time,
            "min_rows": self.min_rows,
            "max_rows": self.max_rows,
            "min_bytes": self.min_bytes,
            "max_bytes": self.max_bytes,
        }
        for i, layer in enumerate(self._layers):
            out[f"layer_{i}_rows"] = layer.rows
        return out

    # ── 目标表 ──────────────────────────────────────────────────────────

    def _target_table(self) -> Any:
        catalog = getattr(self.context, "catalog", None)
        if catalog is None or self._target_database is None or self._target_table_name is None:
            return None
        return catalog.try_get_table(self._target_database, self._target_table_name)

    # ── 写 ──────────────────────────────────────────────────────────────

    def write(self, ctx: Any, block: Block) -> None:
        stored = [c for c in self.table.columns if c.is_stored()]
        aligned = _align_block(block, stored, strict=True, table_name=self.full_table_name())
        with self._lock:
            self._layers[0].append(aligned)
            self._maybe_flush(force_full=False)
        self._ensure_thread()

    # ── 读 ──────────────────────────────────────────────────────────────

    def read(self, ctx: Any = None, **kwargs: Any) -> Iterator[Chunk]:
        expected = _requested_columns(ctx, self.get_columns())
        if not expected:
            return
        settings = _settings_of(ctx)
        block_size = _max_block_size(settings)
        limit = _read_limit(ctx)
        produced = 0

        target = self._target_table()
        if target is not None and target.engine is not None:
            # 目标表里已经刷出去的数据(目标表的 chunk 已经是"同构"的)
            for chunk in target.engine.read(ctx):
                if limit is not None and produced >= limit:
                    return
                produced += chunk.num_rows
                yield chunk
                if limit is not None and produced >= limit:
                    return

        # 还没刷出去的缓冲(近似:目标表在前、缓冲在后)
        with self._lock:
            pending = [b for layer in self._layers for b in layer.blocks]
        for block in pending:
            for start in range(0, block.rows(), block_size):
                count = min(block_size, block.rows() - start)
                if limit is not None:
                    count = min(count, limit - produced)
                    if count <= 0:
                        return
                columns = []
                for cwt in expected:
                    src = block.try_get_by_name(cwt.name)
                    column = cwt.type.create_column()
                    if src is not None:
                        column.insert_range_from(src.column, start, count)
                    else:
                        column.append_many_default(count)
                    columns.append(column)
                chunk = _make_chunk(expected, columns)
                _check_chunk(chunk, expected)
                yield chunk
                produced += count
                if limit is not None and produced >= limit:
                    return

    # ── 刷新 ────────────────────────────────────────────────────────────

    def flush(self) -> None:
        """``FLUSH TABLE`` —— 把所有层的缓冲一路晋升并写进目标表。"""
        with self._lock:
            self._flush_all_locked()

    def _maybe_flush(self, force_full: bool) -> None:
        """按阈值决定各层是否晋升(**必须在持锁时调用**)。"""
        now = time.monotonic()
        for i in range(self.num_layers):
            layer = self._layers[i]
            if layer.rows == 0:
                continue
            age = now - (layer.first_time or now)
            forced = (
                (self.max_rows and layer.rows >= self.max_rows)
                or (self.max_bytes and layer.bytes >= self.max_bytes)
                or (self.max_time and age >= self.max_time)
            )
            soft = (
                not force_full
                and (self.min_rows and layer.rows >= self.min_rows)
                and (self.min_time <= 0 or age >= self.min_time)
            )
            if forced or soft:
                self._promote_locked(i)

    def _promote_locked(self, index: int) -> None:
        """把第 ``index`` 层的缓冲推到下一层;最后一层写穿到目标表。"""
        blocks = self._layers[index].drain()
        if not blocks:
            return
        if index + 1 < self.num_layers:
            for block in blocks:
                self._layers[index + 1].append(block)
        else:
            self._write_through(blocks)

    def _flush_all_locked(self) -> None:
        for i in range(self.num_layers):
            self._promote_locked(i)

    def _write_through(self, blocks: List[Block]) -> None:
        """最后一层:把数据真正写进目标表。"""
        target = self._target_table()
        if target is None or target.engine is None:
            raise_error(
                ErrorCode.UNKNOWN_TABLE,
                f"Buffer target {self._target_database}.{self._target_table_name} "
                f"does not exist; cannot flush",
            )
        for block in blocks:
            target.engine.write(self.context, block)

    # ── 后台线程 ────────────────────────────────────────────────────────

    def _ensure_thread(self) -> None:
        """按需启动后台刷新线程(**幂等**:只启一个)。"""
        if self._shutdown or self._thread is not None:
            return
        thread = threading.Thread(
            target=self._background_loop,
            name=f"BufferFlush[{self.full_table_name()}]",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def _background_loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(0.2)
            try:
                with self._lock:
                    self._maybe_flush(force_full=False)
            except Exception:  # pragma: no cover - 后台线程不应当把异常抛出去
                pass

    # ── 生命周期 ────────────────────────────────────────────────────────

    def shutdown(self) -> None:
        """幂等关闭 —— 尽力 flush 一次,然后停掉后台线程。

        这里**不保证**像正常关闭那样把数据写出去(如果目标表已不可用,
        ``_write_through`` 会抛),但至少会尝试 —— 与真实 ClickHouse
        "shutdown 时 flush" 的行为一致。
        """
        if self._shutdown:
            return
        self._shutdown = True
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
            self._thread = None
        try:
            with self._lock:
                self._flush_all_locked()
        except Exception:  # pragma: no cover - 目标表可能已经没了
            pass

    def truncate(self) -> None:
        with self._lock:
            for layer in self._layers:
                layer.drain()


def _factory(table, storage_id, context, engine_args, params) -> StorageBuffer:
    return StorageBuffer(table, storage_id, context, engine_args, params)


register_storage_engine(
    "Buffer",
    _factory,
    description="在内存里攒批,达到 min/max 阈值后成批刷入目标表(崩溃会丢数据)。",
    aliases=("BUFFER",),
)

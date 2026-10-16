"""复制 —— ``ReplicatedMergeTree`` 的协调引擎。

这个模块是 :mod:`minich.keeper` 之上的**状态机**:Keeper 提供"一棵到处
一致的树",这里提供"副本们如何用这棵树把自己变得和数据源一样"。

先把整条链路讲清楚,后面每个类都在回答其中的一小块:

.. code-block:: text

   ① 客户端往 replica_a 插一批数据
         │
         ▼
   ② replica_a: 把数据写成本地 part,然后往 Keeper 的 /log 追加一条
      LogEntry(type=GET_PART, new_part_name=all_1_1_0, source_replica=a)
      —— 这一步只是"写日志",数据还只在 a 的磁盘上
         │
         ▼
   ③ replica_b / replica_c: 各自从 Keeper 读 /log,看到这条 entry
         │
         ▼
   ④ 它们调用 **表引擎提供的回调**去 b/c 那里"执行"这条 entry:
      GET_PART  → 去 source_replica(a) 那里把 part 拉过来(on_fetch_part)
      MERGE_PARTS → 本地把那几个 part 合起来(on_merge_parts)
         │
         ▼
   ⑤ 执行成功后推进自己的队列指针 /replicas/<r> = "我处理到第 N 条了"

**关键**:② 和 ④ 之间可能隔着很长时间、很多次重试、甚至副本重启。
所以 ④ 必须**幂等** —— 同一条 entry 执行两遍,结果必须和执行一遍一样。
这是整个复制引擎最重要的不变量,本章有一半的注释在反复解释它。

.. code-block:: text

   副本内部状态机(每个 part 都会经历其中若干态):

        ┌─────────┐  从日志里看到要拉这个 part
        │ QUEUED  │──────────────────────────────┐
        └─────────┘                              ▼
                                           ┌───────────┐
                                           │ FETCHING  │  正在从别的副本下载
                                           └───────────┘
                                                 │ 下载完
                                                 ▼
                                           ┌───────────┐
                                           │ ATTACHING │  挂到本地表上
                                           └───────────┘
                                                 │ 挂好
                                                 ▼
                                           ┌───────────┐
                                           │ COMMITTED │  可被 SELECT 看到
                                           └───────────┘
                                                 │ 被合并 / 被删
                                                 ▼
                                           ┌───────────┐
                                           │ DELETING  │
                                           └───────────┘

        OUTDATED: 这个 part 已经不需要了(被合并进更大的 part),
                  但日志里可能还有引用它的旧 entry —— 用来"认识但不使用"

.. warning::

   **mini-clickhouse 的复制是"单进程里的多个对象"**,不是真的多个
   ClickHouse 进程。差异清单:

   * 真正的 part 数据传输走副本之间的 **HTTP**(``interserver_http_port``,
     即 "fetch")。这里 ``on_fetch_part`` 直接从一个内存对象拷贝到另一个;
   * 真正的副本是**并发**的:插入、拉取、合并、心跳各自一个线程池。
     这里全靠调用方显式地"推进"(:meth:`Replica.process_log_entries`),
     quorum 等待时用一个 ``_pump`` 钩子来假装别的副本在跑;
   * 真正的进程重启会重建所有内存状态。这里 :meth:`LocalCluster.revive_replica`
     用"重新建会话 + 从日志头重放"来模拟。

   这些差异**不影响协议层**(日志、去重、选主、quorum 的语义都一
   一对应),影响的是"能不能演示真实的时序竞争"。凡是时序相关的地方,
   下文都标了 ``.. warning::``。

.. note::

   本模块只依赖 :mod:`minich.keeper`、:mod:`minich.serialization`
   与 :mod:`minich.util`,**不 import** ``settings`` / ``storages`` /
   ``execution`` —— 所以它可以脱离数据库引擎单独跑、单独测。
"""

from __future__ import annotations

import enum
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import ErrorCode, MiniChError, raise_error
from .keeper import (
    Create,
    KeeperError,
    KeeperServer,
    NodeExistsError,
    ReplicatedTablePaths,
    DEFAULT_SESSION_TIMEOUT_MS,
)
from .serialization import VarUIntReader, write_var_uint
from .util.hashing import siphash64
from .util.logger import Logger, get_logger
from .util.stopwatch import Stopwatch

__all__ = [
    # 日志条目
    "EntryType",
    "LogEntry",
    "ENTRY_TYPE_NAMES",
    "LOG_ENTRY_FORMAT_VERSION",
    # part 与状态
    "PartState",
    "PartInfo",
    "PartStore",
    "MergeCandidate",
    "default_merge_selector",
    # 结果与状态
    "InsertResult",
    "ReplicatedTableStatus",
    # 异常
    "DuplicateInsertError",
    "PartNotFoundError",
    "QuorumTimeoutError",
    "ReplicaReadOnlyError",
    "NotLeaderError",
    # 主体
    "Replica",
    "LocalCluster",
]


# ══════════════════════════════════════════════════════════════════════════
# 异常
# ══════════════════════════════════════════════════════════════════════════

#: 真实 ClickHouse 的 ``ErrorCodes::DUPLICATE_DATA``。
#: :mod:`minich.errors` 的表里没有它(它只列了教学真正会抛的那些),
#: 所以这里本地定义 —— 编号照抄源码,方便对上官方文档。
_DUPLICATE_DATA = 117
#: ``ErrorCodes::NO_REPLICA_HAS_PART`` —— 没有任何副本有你要的 part。
_NO_REPLICA_HAS_PART = 248


class DuplicateInsertError(MiniChError):
    """插入的 block 已经存在(命中去重)。**这是"成功"的一种形式**,不是故障。

    ``INSERT`` 的重试(客户端超时重发、上游 at-least-once 的管道路由)
    靠它变得幂等。真实 ClickHouse 在新版本里**不再抛这个异常**,而是
    静默跳过并写 ``system.query_log`` 的 ``written_rows=0``;更老的版本
    (以及 ``insert_deduplicate=0`` 之外的一些边角)会抛
    ``DB::Exception: Duplicate data``。mini-clickhouse 两种都支持:
    默认返回"重复"结果,``raise_on_duplicate=True`` 时抛这个。
    """

    default_code = _DUPLICATE_DATA

    def __init__(self, message: str, block_id: Optional[str] = None, code: Optional[int] = None) -> None:
        super().__init__(message, code=code)
        self.name = "DUPLICATE_DATA"
        self.block_id = block_id


class PartNotFoundError(MiniChError):
    """没有任何副本有需要的 part —— ``NO_REPLICA_HAS_PART`` (248)。

    出现在 ``GET_PART`` 找不到 source、且其它副本也没有这个 part 时。
    真实 CH 接下来会把该 part 标成"丢失"并(可能)让副本进入只读,
    因为一个 part 同时在所有副本上消失通常意味着有人手动删了文件。
    """

    default_code = _NO_REPLICA_HAS_PART

    def __init__(self, message: str, part_name: Optional[str] = None, code: Optional[int] = None) -> None:
        super().__init__(message, code=code)
        self.name = "NO_REPLICA_HAS_PART"
        self.part_name = part_name


class QuorumTimeoutError(MiniChError):
    """等 quorum 超时 —— ``TIMEOUT_EXCEEDED`` (159)。"""

    default_code = ErrorCode.TIMEOUT_EXCEEDED

    def __init__(self, message: str, code: Optional[int] = None) -> None:
        super().__init__(message, code=code)


class ReplicaReadOnlyError(MiniChError):
    """副本处于只读 —— ``TABLE_IS_READ_ONLY`` (120)。

    触发条件(真实 CH 里有一长串):Keeper 会话断了、队列积压太久、
    有 part 丢失、``max_replicated_logs_to_keep`` 超限……
    mini-clickhouse 只实现最核心的一条:**Keeper 会话死了 → 只读**。
    """

    default_code = ErrorCode.TABLE_IS_READ_ONLY

    def __init__(self, message: str, code: Optional[int] = None) -> None:
        super().__init__(message, code=code)


class NotLeaderError(MiniChError):
    """非 leader 副本试图做只有 leader 能做的事(比如挑选合并)。"""

    default_code = ErrorCode.LOGICAL_ERROR

    def __init__(self, message: str, code: Optional[int] = None) -> None:
        super().__init__(message, code=code)
        self.name = "NOT_LEADER"


# ══════════════════════════════════════════════════════════════════════════
# 日志条目
# ══════════════════════════════════════════════════════════════════════════


class EntryType(enum.IntEnum):
    """``ReplicatedMergeTreeLogEntry::Type`` —— **日志条目的类型**。

    .. code-block:: text

       EMPTY=0             占位,什么都不做
       GET_PART=1          "从 source_replica 拉一个 part 过来"
                           ← 插入、以及"老副本补数据"都走它(最常见)
       ATTACH_PART=2       "这个 part 就在本地,直接挂上"(比 GET_PART 快的捷径)
       MERGE_PARTS=3       "把这几个 part 合成一个新的"(leader 下发)
       DROP_RANGE=4        "把某个分区里某个 block 号区间的 part 全删了"
       REPLACE_RANGE=5     "把一批分区整个换成另一张表的 part"
                           ← REPLACE PARTITION 的实现方式
       MUTATE_PART=6       "对这个 part 跑一次 ALTER ... UPDATE/DELETE"
       ALTER_METADATA=7    "列结构变了,更新你的 metadata"
       DROP_PART=8         "删掉这个 part"(不删分区,只删 part)
       CLEAR_COLUMN=9      "把这一列从所有 part 里清掉"
       DROP_PARTITION=10   "删掉整个分区"
       FETCH_PART=11       "我(主动)要去拉这个 part 了"
                           ← GET_PART 的"手";它先入队,防止别的副本抢着拉
       REMOVE_PART=12      "把这个 part 从本地删掉"(被淘汰的冗余 part)
       DETACH_PART=13      "把这个 part 挪到 detached/ 目录"(不动数据,只下架)
       DROP_REPLICA=14     "删掉这个副本的全部数据"
       UPDATE_QUORUM=15    "更新某个 part 的 quorum 名单"
       KILL=16             "丢弃这条 entry 的处理(用于把坏副本踢出队列)"

    .. warning::

       这些编号**跟随 ClickHouse 25.3 的 ``enum Type`` 顺序**,只是
       mini-clickhouse 内部的约定 —— 真实 CH 的类型在 znode 里是**按
       名字**存的(见 :meth:`LogEntry.to_text`),不是按数字存的。
       所以拿这里的数字去和别的版本对比没有意义;要对齐的是**名字**。
    """

    EMPTY = 0
    GET_PART = 1
    ATTACH_PART = 2
    MERGE_PARTS = 3
    DROP_RANGE = 4
    REPLACE_RANGE = 5
    MUTATE_PART = 6
    ALTER_METADATA = 7
    DROP_PART = 8
    CLEAR_COLUMN = 9
    DROP_PARTITION = 10
    FETCH_PART = 11
    REMOVE_PART = 12
    DETACH_PART = 13
    DROP_REPLICA = 14
    UPDATE_QUORUM = 15
    KILL = 16

    @classmethod
    def from_name(cls, name: str) -> "EntryType":
        return _ENTRY_TYPE_BY_NAME.get(name.strip().upper(), cls.EMPTY)

    @property
    def label(self) -> str:
        return ENTRY_TYPE_NAMES.get(int(self), f"TYPE_{int(self)}")


#: ``int`` → 名字。用于日志、``to_text`` 与调试输出。
ENTRY_TYPE_NAMES: Dict[int, str] = {int(t): t.name for t in EntryType}
_ENTRY_TYPE_BY_NAME: Dict[str, EntryType] = {t.name: t for t in EntryType}

#: 这些类型的 entry 算"插入"(``system.replicas.inserts_in_queue``)。
INSERT_ENTRY_TYPES: frozenset = frozenset(
    (EntryType.GET_PART, EntryType.ATTACH_PART, EntryType.FETCH_PART, EntryType.REPLACE_RANGE)
)
#: 这些类型的 entry 算"合并"(``system.replicas.merges_in_queue``)。
MERGE_ENTRY_TYPES: frozenset = frozenset((EntryType.MERGE_PARTS, EntryType.MUTATE_PART))

#: mini-clickhouse 的二进制格式版本。将来加字段时靠它做兼容。
LOG_ENTRY_FORMAT_VERSION = 1


def _write_str(out: bytearray, value: str) -> None:
    """写一个字符串 —— ``[varint 长度][UTF-8 字节]``。"""
    raw = value.encode("utf-8")
    write_var_uint(out, len(raw))
    out += raw


def _read_str(reader: VarUIntReader) -> str:
    length = reader.read_var_uint()
    return reader.read(length).decode("utf-8")


def _write_str_list(out: bytearray, values: Sequence[str]) -> None:
    write_var_uint(out, len(values))
    for v in values:
        _write_str(out, v)


def _read_str_list(reader: VarUIntReader) -> List[str]:
    count = reader.read_var_uint()
    return [_read_str(reader) for _ in range(count)]


class LogEntry:
    """一条复制日志 —— ``ReplicatedMergeTreeLogEntry``。

    这是复制的**唯一通信单位**:副本之间不直接说话,只通过"读同一条
    entry、各自执行"来达成一致。所以 entry 里必须装下"执行它需要的
    全部信息" —— 尤其是 ``source_replica``(去哪拉)、``new_part_name``
    (目标 part 叫什么)、``parts``(合并/删除的源 part 列表)。

    .. code-block:: text

       真实 ClickHouse 在 Keeper 的 znode 里存的是**文本**(每行一个字段):

           format version: 3
           type: GET_PART
           source replica: replica_a
           block id: all_1_1_0
           get part: all_1_1_0
           quorum: replica_a
           ...

       由 ReplicatedMergeTreeLogEntry::writeText / readText 读写。
       mini-clickhouse 用**确定性二进制**(下面这套 varint 布局)替代:

           [varint 格式版本][varint type]
           [String source_replica][String new_part_name]
           [varint n][String × n  parts]
           [String block_id]
           [varint n][String × n  quorum]
           [String deduplication_block_id][varint create_time]
           [String znode_name][varint alter_version]
           [String column_name][String partition_id]
           [String from_part][String to_part]
           [varint is_detach][String mutation_id]
           [varint mutations_version][varint num_tries]

    .. warning::

       **二进制格式是 mini-clickhouse 的简化**,不是真实 wire format。
       好处是紧凑、逐字节可断言(测试里直接比对 ``to_bytes()``);
       代价是不能和真实 Keeper 的 znode 内容互读。字段的**语义**
       与真实一一对应,要对接真实 Keeper,把 :meth:`to_text` 的输出
       (它模仿真实文本格式)当作参考即可。

    .. note::

       ``znode_name``(``log-0000000001``)与 ``log_index``(``1``)是
       **运行时字段**,不参与序列化 —— 真实 CH 里它们由日志 znode 的
       位置决定,而不是 entry 内容的一部分。:meth:`from_bytes` 之后
       调用方需要自己填(见 :meth:`Replica.process_log_entries`)。
    """

    __slots__ = (
        "type", "source_replica", "new_part_name", "parts", "block_id", "quorum",
        "deduplication_block_id", "create_time", "znode_name", "alter_version",
        "column_name", "partition_id", "from_part", "to_part", "is_detach",
        "mutation_id", "mutations_version", "num_tries",
        # 运行时字段(不序列化)
        "log_index", "exception",
    )

    def __init__(
        self,
        type: EntryType = EntryType.EMPTY,
        source_replica: str = "",
        new_part_name: str = "",
        parts: Optional[Sequence[str]] = None,
        block_id: str = "",
        quorum: Optional[Sequence[str]] = None,
        deduplication_block_id: str = "",
        create_time: Optional[int] = None,
        znode_name: str = "",
        alter_version: int = 0,
        column_name: str = "",
        partition_id: str = "",
        from_part: str = "",
        to_part: str = "",
        is_detach: bool = False,
        mutation_id: str = "",
        mutations_version: int = 0,
        num_tries: int = 0,
    ) -> None:
        self.type = EntryType(type)
        self.source_replica = source_replica
        self.new_part_name = new_part_name
        self.parts: List[str] = list(parts) if parts else []
        self.block_id = block_id
        self.quorum: List[str] = list(quorum) if quorum else []
        self.deduplication_block_id = deduplication_block_id
        self.create_time = int(time.time() * 1000) if create_time is None else int(create_time)
        self.znode_name = znode_name
        self.alter_version = alter_version
        self.column_name = column_name
        self.partition_id = partition_id
        self.from_part = from_part
        self.to_part = to_part
        self.is_detach = is_detach
        self.mutation_id = mutation_id
        self.mutations_version = mutations_version
        self.num_tries = num_tries
        self.log_index = 0
        self.exception = ""

    # ── 构造糖(比一堆关键字参数好读)───────────────────────────────────

    @classmethod
    def get_part(cls, new_part_name: str, source_replica: str, *, block_id: str = "",
                 partition_id: str = "", quorum: Optional[Sequence[str]] = None) -> "LogEntry":
        """插入/补数据用 —— "从 ``source_replica`` 拉 ``new_part_name``"。"""
        return cls(
            EntryType.GET_PART, source_replica=source_replica, new_part_name=new_part_name,
            parts=[new_part_name], block_id=block_id or new_part_name,
            partition_id=partition_id, quorum=quorum or [],
        )

    @classmethod
    def attach_part(cls, part_name: str, source_replica: str = "local") -> "LogEntry":
        return cls(EntryType.ATTACH_PART, source_replica=source_replica,
                   new_part_name=part_name, parts=[part_name])

    @classmethod
    def fetch_part(cls, part_name: str, source_replica: str = "") -> "LogEntry":
        return cls(EntryType.FETCH_PART, source_replica=source_replica, new_part_name=part_name)

    @classmethod
    def merge_parts(cls, source_parts: Sequence[str], new_part_name: str, source_replica: str,
                    *, block_id: str = "", partition_id: str = "") -> "LogEntry":
        """合并 —— ``source_parts`` 合成 ``new_part_name``。

        ``block_id`` 必须填:合并出来的 part 也是一个"新 block",
        同样要参与去重(否则副本重启后重放这条 entry 会合出一个
        重复的 part)。
        """
        return cls(
            EntryType.MERGE_PARTS, source_replica=source_replica,
            parts=list(source_parts), new_part_name=new_part_name,
            block_id=block_id or f"merge-{new_part_name}",
            deduplication_block_id=block_id or f"merge-{new_part_name}",
            partition_id=partition_id,
        )

    @classmethod
    def mutate_part(cls, part_name: str, mutation_id: str, mutations_version: int,
                    source_replica: str = "", *, new_part_name: str = "") -> "LogEntry":
        return cls(EntryType.MUTATE_PART, source_replica=source_replica, parts=[part_name],
                   new_part_name=new_part_name or part_name, mutation_id=mutation_id,
                   mutations_version=mutations_version)

    @classmethod
    def alter_metadata(cls, alter_version: int, source_replica: str = "") -> "LogEntry":
        return cls(EntryType.ALTER_METADATA, source_replica=source_replica, alter_version=alter_version)

    @classmethod
    def drop_part(cls, part_name: str, source_replica: str = "", *, is_detach: bool = False) -> "LogEntry":
        return cls(EntryType.DETACH_PART if is_detach else EntryType.DROP_PART,
                   source_replica=source_replica, parts=[part_name], new_part_name=part_name)

    @classmethod
    def drop_range(cls, partition_id: str, from_part: str, to_part: str,
                   source_replica: str = "") -> "LogEntry":
        return cls(EntryType.DROP_RANGE, source_replica=source_replica, partition_id=partition_id,
                   from_part=from_part, to_part=to_part)

    @classmethod
    def drop_partition(cls, partition_id: str, source_replica: str = "") -> "LogEntry":
        return cls(EntryType.DROP_PARTITION, source_replica=source_replica, partition_id=partition_id)

    @classmethod
    def clear_column(cls, column_name: str, partition_id: str = "", source_replica: str = "") -> "LogEntry":
        return cls(EntryType.CLEAR_COLUMN, source_replica=source_replica, column_name=column_name,
                   partition_id=partition_id)

    @classmethod
    def replace_range(cls, partition_id: str, source_replica: str = "", *,
                      from_part: str = "", to_part: str = "", parts: Optional[Sequence[str]] = None) -> "LogEntry":
        return cls(EntryType.REPLACE_RANGE, source_replica=source_replica, partition_id=partition_id,
                   from_part=from_part, to_part=to_part, parts=list(parts) if parts else [])

    @classmethod
    def remove_part(cls, part_name: str, source_replica: str = "") -> "LogEntry":
        return cls(EntryType.REMOVE_PART, source_replica=source_replica, parts=[part_name],
                   new_part_name=part_name)

    @classmethod
    def update_quorum(cls, new_part_name: str, quorum: Sequence[str], source_replica: str = "") -> "LogEntry":
        return cls(EntryType.UPDATE_QUORUM, source_replica=source_replica,
                   new_part_name=new_part_name, quorum=list(quorum))

    # ── 序列化 ──────────────────────────────────────────────────────────

    def to_bytes(self) -> bytes:
        """确定性序列化 —— 固定字段顺序,不用字典、不用 Python 对象身份。

        确定性是**硬要求**:两个副本要能对同一条 entry 的内容达成一致,
        测试也要能直接 ``assert a.to_bytes() == b.to_bytes()``。
        """
        out = bytearray()
        write_var_uint(out, LOG_ENTRY_FORMAT_VERSION)
        write_var_uint(out, int(self.type))
        _write_str(out, self.source_replica)
        _write_str(out, self.new_part_name)
        _write_str_list(out, self.parts)
        _write_str(out, self.block_id)
        _write_str_list(out, self.quorum)
        _write_str(out, self.deduplication_block_id)
        write_var_uint(out, max(0, self.create_time))
        _write_str(out, self.znode_name)
        write_var_uint(out, max(0, self.alter_version))
        _write_str(out, self.column_name)
        _write_str(out, self.partition_id)
        _write_str(out, self.from_part)
        _write_str(out, self.to_part)
        write_var_uint(out, 1 if self.is_detach else 0)
        _write_str(out, self.mutation_id)
        write_var_uint(out, max(0, self.mutations_version))
        write_var_uint(out, max(0, self.num_tries))
        return bytes(out)

    @classmethod
    def from_bytes(cls, data: bytes) -> "LogEntry":
        """反序列化。字段顺序必须与 :meth:`to_bytes` **严格一致**。

        末尾多出字节直接报错 —— 因为"解出来还剩一点"通常意味着
        版本错配(比如把 v2 的数据当 v1 解),静默忽略会让 bug
        藏得很深。这与 :func:`minich.serialization.deserialize_binary`
        对"必须刚好用完"的要求是同一个原则。
        """
        reader = VarUIntReader(bytes(data))
        version = reader.read_var_uint()
        if version != LOG_ENTRY_FORMAT_VERSION:
            raise_error(
                ErrorCode.CANNOT_PARSE_TEXT,
                f"Unsupported LogEntry format version {version} "
                f"(expected {LOG_ENTRY_FORMAT_VERSION})",
            )
        entry = cls()
        entry.type = EntryType(reader.read_var_uint())
        entry.source_replica = _read_str(reader)
        entry.new_part_name = _read_str(reader)
        entry.parts = _read_str_list(reader)
        entry.block_id = _read_str(reader)
        entry.quorum = _read_str_list(reader)
        entry.deduplication_block_id = _read_str(reader)
        entry.create_time = reader.read_var_uint()
        entry.znode_name = _read_str(reader)
        entry.alter_version = reader.read_var_uint()
        entry.column_name = _read_str(reader)
        entry.partition_id = _read_str(reader)
        entry.from_part = _read_str(reader)
        entry.to_part = _read_str(reader)
        entry.is_detach = bool(reader.read_var_uint())
        entry.mutation_id = _read_str(reader)
        entry.mutations_version = reader.read_var_uint()
        entry.num_tries = reader.read_var_uint()
        if not reader.eof():
            raise_error(
                ErrorCode.CANNOT_READ_ALL_DATA,
                f"LogEntry has {reader.remaining()} trailing bytes after decoding",
            )
        return entry

    def to_text(self) -> str:
        """模仿**真实** ClickHouse 的文本格式(便于对照真实 znode 内容)。

        注意:这个方法的输出**不被** :meth:`from_bytes` 读取 —— 它纯粹
        是给人看的/给教程引用的,用来揭示"真实 Keeper 里长什么样"。
        """
        lines = [
            "format version: 3",
            f"type: {self.type.label}",
            f"source replica: {self.source_replica}",
            f"block id: {self.block_id}",
        ]
        if self.new_part_name:
            lines.append(f"get part: {self.new_part_name}")
        if self.parts:
            lines.append("parts: " + ", ".join(self.parts))
        if self.quorum:
            lines.append("quorum: " + ", ".join(self.quorum))
        if self.partition_id:
            lines.append(f"partition_id: {self.partition_id}")
        if self.column_name:
            lines.append(f"column: {self.column_name}")
        if self.mutation_id:
            lines.append(f"mutation: {self.mutation_id} ({self.mutations_version})")
        return "\n".join(lines)

    # ── 杂项 ────────────────────────────────────────────────────────────

    def is_insert(self) -> bool:
        return self.type in INSERT_ENTRY_TYPES

    def is_merge(self) -> bool:
        return self.type in MERGE_ENTRY_TYPES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type.label,
            "source_replica": self.source_replica,
            "new_part_name": self.new_part_name,
            "parts": list(self.parts),
            "block_id": self.block_id,
            "quorum": list(self.quorum),
            "create_time": self.create_time,
            "znode_name": self.znode_name,
            "partition_id": self.partition_id,
            "num_tries": self.num_tries,
        }

    def clone(self) -> "LogEntry":
        e = LogEntry.from_bytes(self.to_bytes())
        e.znode_name = self.znode_name
        e.log_index = self.log_index
        return e

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, LogEntry):
            return NotImplemented
        return self.to_bytes() == other.to_bytes()

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<LogEntry {self.type.label} new={self.new_part_name!r} "
            f"src={self.source_replica!r} parts={self.parts} idx={self.log_index}>"
        )


# ══════════════════════════════════════════════════════════════════════════
# part 与本地状态
# ══════════════════════════════════════════════════════════════════════════


class PartState(enum.IntEnum):
    """一个 part 在副本本地的状态 —— ``ReplicatedMergeTreeQueue`` 的那套。

    .. code-block:: text

       QUEUED     已知道这个 part 存在,但还没开始处理
       FETCHING   正在从别的副本下载
       ATTACHING  数据已到,正在挂到表上(建索引、写元数据)
       COMMITTED  可用 —— SELECT 能看到它
       DELETING   正在删除
       OUTDATED   已被更大的 part 取代;日志里可能还有引用它的旧 entry,
                  所以"认识它,但不用它"

    ``OUTDATED`` 是这套状态里最容易被忽略、却最要命的一个:因为日志是
    **追加**的,旧的 ``MERGE_PARTS`` entry 里可能还写着"把 all_1_1_0 合进去",
    而那个 part 早就被更早的合并吃掉了。处理这条旧 entry 时,副本必须
    认出"源 part 已 OUTDATED",直接跳过,而不是报错。
    """

    QUEUED = 0
    FETCHING = 1
    ATTACHING = 2
    COMMITTED = 3
    DELETING = 4
    OUTDATED = 5


class PartInfo:
    """一个 part 的**描述** —— 名字、行数、字节数、所属分区、层级。

    真实 ClickHouse 的 part 名字是
    ``<partition_id>_<min_block>_<max_block>_<level>``,例如:

    .. code-block:: text

       all_1_1_0     ← 第 1 个 block 单独成 part(level 0,插入产生)
       all_2_2_0     ← 第 2 个 block
       all_1_2_1     ← 上面两个合并(level 1)

    **为什么名字要带 min/max block 和 level,而不是随便一个自增号?**
    因为副本们各自独立地合并,必须能在**不看数据**的前提下判断
    "我们指的是同一个 part"。名字里带 block 号区间,就让
    "all_1_2_1" 在 A、B、C 三个副本上必然指同一批数据 —— 这是
    无中心协调能做合并的前提。
    """

    __slots__ = ("name", "partition_id", "min_block", "max_block", "level",
                 "rows", "size_bytes", "block_id", "state", "mutation")

    def __init__(
        self,
        name: str,
        partition_id: str = "all",
        min_block: int = 0,
        max_block: int = 0,
        level: int = 0,
        rows: int = 0,
        size_bytes: int = 0,
        block_id: str = "",
        state: PartState = PartState.COMMITTED,
        mutation: str = "",
    ) -> None:
        self.name = name
        self.partition_id = partition_id
        self.min_block = min_block
        self.max_block = max_block
        self.level = level
        self.rows = rows
        self.size_bytes = size_bytes
        self.block_id = block_id
        self.state = state
        self.mutation = mutation

    @staticmethod
    def make_name(partition_id: str, min_block: int, max_block: int, level: int) -> str:
        """按真实 ClickHouse 的规则拼 part 名。"""
        return f"{partition_id}_{min_block}_{max_block}_{level}"

    @staticmethod
    def parse_name(name: str) -> Tuple[str, int, int, int]:
        """解析 part 名 —— 解析失败抛 ``BAD_DATA_PART_NAME``。

        真实 CH 对 part 名的合法性检查很严(它在磁盘上到处都是,
        解析错一个字段就会读错数据),所以解析失败是**硬错误**而非警告。
        """
        parts = name.rsplit("_", 3)
        if len(parts) != 4:
            raise_error(ErrorCode.BAD_DATA_PART_NAME, f"Malformed part name: {name!r}")
        partition_id, a, b, c = parts
        try:
            return partition_id, int(a), int(b), int(c)
        except ValueError:
            raise_error(ErrorCode.BAD_DATA_PART_NAME, f"Malformed part name: {name!r}")

    def copy(self) -> "PartInfo":
        return PartInfo(
            self.name, self.partition_id, self.min_block, self.max_block, self.level,
            self.rows, self.size_bytes, self.block_id, self.state, self.mutation,
        )

    def signature(self) -> Tuple[Any, ...]:
        """用于"副本之间是否收敛"的比较签名(不含状态)。"""
        return (self.name, self.min_block, self.max_block, self.level, self.rows, self.size_bytes)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Part {self.name} rows={self.rows} size={self.size_bytes} "
            f"state={self.state.name}>"
        )


class PartStore:
    """一个副本的**本地 part 集合** —— 模拟磁盘上的那张表。

    在真实 ClickHouse 里,这个角色由 ``MergeTreeData`` 的文件系统目录 +
    ``system.parts`` 扮演。mini-clickhouse 用一个字典代替,这样
    "副本 A 和副本 B 的最终状态是否一致"就变成了一次字典比较。
    """

    def __init__(self, replica_name: str = "") -> None:
        self.replica_name = replica_name
        self.parts: Dict[str, PartInfo] = {}

    def add(self, part: PartInfo) -> None:
        self.parts[part.name] = part

    def remove(self, name: str) -> bool:
        return self.parts.pop(name, None) is not None

    def get(self, name: str) -> Optional[PartInfo]:
        return self.parts.get(name)

    def has(self, name: str) -> bool:
        return name in self.parts

    def names(self) -> List[str]:
        return sorted(self.parts)

    def committed(self) -> List[PartInfo]:
        return [p for p in self.parts.values() if p.state == PartState.COMMITTED]

    def by_partition(self, partition_id: str) -> List[PartInfo]:
        return [p for p in self.parts.values()
                if p.partition_id == partition_id and p.state == PartState.COMMITTED]

    def total_rows(self) -> int:
        return sum(p.rows for p in self.committed())

    def parts_of(self, names: Iterable[str]) -> List[PartInfo]:
        return [self.parts[n] for n in names if n in self.parts]

    def signature(self) -> List[Tuple[Any, ...]]:
        """可比较的"表状态"签名 —— 排序后逐项比较即可判断副本是否一致。"""
        return sorted(p.signature() for p in self.parts.values())

    def __len__(self) -> int:
        return len(self.parts)

    def __contains__(self, name: object) -> bool:
        return name in self.parts

    def __iter__(self):
        return iter(sorted(self.parts.values(), key=lambda p: p.name))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PartStore {self.replica_name!r} parts={len(self.parts)} rows={self.total_rows()}>"


class MergeCandidate:
    """一次"可以合并这些 part"的提议 —— leader 选出来、然后写进日志。"""

    __slots__ = ("parts",)

    def __init__(self, parts: Sequence[PartInfo]) -> None:
        self.parts: List[PartInfo] = list(parts)

    @property
    def partition_id(self) -> str:
        return self.parts[0].partition_id

    @property
    def min_block(self) -> int:
        return min(p.min_block for p in self.parts)

    @property
    def max_block(self) -> int:
        return max(p.max_block for p in self.parts)

    @property
    def level(self) -> int:
        return max(p.level for p in self.parts) + 1

    @property
    def rows(self) -> int:
        return sum(p.rows for p in self.parts)

    @property
    def size_bytes(self) -> int:
        return sum(p.size_bytes for p in self.parts)

    @property
    def new_part_name(self) -> str:
        return PartInfo.make_name(self.partition_id, self.min_block, self.max_block, self.level)

    @property
    def source_names(self) -> List[str]:
        return [p.name for p in self.parts]

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MergeCandidate {self.source_names} -> {self.new_part_name}>"


def default_merge_selector(replica: "Replica", max_parts_in_merge: int = 10,
                           min_parts_in_merge: int = 2) -> Optional[MergeCandidate]:
    """极简的合并选择器 —— 真实 CH 的 ``SimpleMergeSelector`` 的玩具版。

    真实的选择器要考虑:part 大小、写入速率、分区大小、``max_bytes_to_merge_at_max_space_in_pool``、
    合并层级的"阶梯"策略(防止写放大)、以及一堆 ``merge_tree`` 设置。
    mini-clickhouse 只做一件事:**按 (分区, level) 分组,挑出成员最多的
    那一组,组内按大小取最小的至多 ``max_parts_in_merge`` 个**,够
    ``min_parts_in_merge`` 个就提议合并。

    ``level`` 必须相等这条限制不是可选的 —— 它是真实 CH 里防止
    "反复重写巨型 part 拖垮写入"的核心:只让**同层级**的 part 互相
    合并,part 大小就会按几何级数增长,总写放大是 O(n log n) 而不是 O(n²)。

    .. warning::

       真实 CH 里这个决策由 **leader 副本**做,而且它会先"预留"
       这些 part(在内存队列里标记),避免两个合并互相吃掉对方的源。
       mini-clickhouse 用 :attr:`Replica.reserved_parts` 做同样的预留,
       但没有真实 CH 那套"部分 part 不可用时的回退策略"。
    """
    by_partition: Dict[str, List[PartInfo]] = {}
    for part in replica.part_store.committed():
        if part.name in replica.reserved_parts:
            continue
        by_partition.setdefault(part.partition_id, []).append(part)

    best: Optional[List[PartInfo]] = None
    for partition_id, parts in by_partition.items():
        by_level: Dict[int, List[PartInfo]] = {}
        for p in parts:
            by_level.setdefault(p.level, []).append(p)
        for level, group in by_level.items():
            if len(group) < min_parts_in_merge:
                continue
            group = sorted(group, key=lambda p: (p.size_bytes, p.name))[:max_parts_in_merge]
            if len(group) < min_parts_in_merge:
                continue
            if best is None or len(group) > len(best):
                best = group
            elif len(group) == len(best) and sum(p.size_bytes for p in group) < sum(p.size_bytes for p in best):
                best = group

    if best is None:
        return None
    return MergeCandidate(best)


# ══════════════════════════════════════════════════════════════════════════
# 插入结果与 system.replicas 状态
# ══════════════════════════════════════════════════════════════════════════


class InsertResult:
    """一次插入的结果 —— 包含了去重与 quorum 这两件事的全部信息。"""

    __slots__ = ("part_name", "block_id", "block_number", "log_index", "entry_name",
                 "duplicate", "quorum_replicas", "quorum_size")

    def __init__(
        self,
        part_name: str = "",
        block_id: str = "",
        block_number: int = 0,
        log_index: int = 0,
        entry_name: str = "",
        duplicate: bool = False,
        quorum_replicas: Optional[Sequence[str]] = None,
        quorum_size: int = 0,
    ) -> None:
        self.part_name = part_name
        self.block_id = block_id
        self.block_number = block_number
        self.log_index = log_index
        self.entry_name = entry_name
        #: True = 这次插入命中去重,数据**没有**被重复写入
        self.duplicate = duplicate
        self.quorum_replicas: List[str] = list(quorum_replicas) if quorum_replicas else []
        #: 客户端要求的 quorum 大小(0 = 没要求)
        self.quorum_size = quorum_size

    @property
    def quorum_satisfied(self) -> bool:
        return self.quorum_size == 0 or len(self.quorum_replicas) >= self.quorum_size

    def __repr__(self) -> str:  # pragma: no cover
        if self.duplicate:
            return f"<InsertResult DUPLICATE block_id={self.block_id!r}>"
        return (
            f"<InsertResult part={self.part_name!r} log#{self.log_index} "
            f"quorum={len(self.quorum_replicas)}/{self.quorum_size}>"
        )


class ReplicatedTableStatus:
    """``system.replicas`` 的一行 —— 复制表的健康度看板。

    .. list-table::
       :widths: 22 78

       * - ``absolute_delay``
         - **队列里最老的、还没执行的 entry 已经等了多久(秒)**。
           这是复制最重要的一个指标:它不为 0 就说明这个副本落下了。
       * - ``queue_size``
         - 还没执行的 entry 条数。
       * - ``inserts_in_queue`` / ``merges_in_queue``
         - 队列里插入/合并各有多少(分开看才能区分"写入压力"和"合并压力")。
       * - ``log_max_index`` / ``log_pointer``
         - Keeper 里最新的日志编号 / 这个副本处理到的编号。
       * - ``active_replicas`` / ``total_replicas``
         - 当前活跃副本数 / 已知副本数(真实 CH 里两者都由 ``/replicas``
           的临时子节点数决定,所以副本挂掉时**两个数都会掉**)。
       * - ``is_leader``
         - 这个副本是不是合并 leader。
       * - ``is_readonly``
         - 只读(会话断了、队列积压、有 part 丢失……)。
       * - ``lost_part_count``
         - 丢失的 part 数(所有副本都没有的那个)。
       * - ``columns_version``
         - 列结构版本(``/columns`` 节点的 version)——
           副本落后时会显示成负数,提示"我的列结构旧了"。

    ``absolute_delay`` 与 ``queue_size`` 都是**真的从 Keeper 状态算出来的**
    (见 :meth:`Replica.get_status`),不是写死的数字 —— 这正是这个类
    存在的意义:看板必须能反映真实积压。
    """

    __slots__ = (
        "database", "table", "engine", "is_leader", "is_readonly", "absolute_delay",
        "queue_size", "inserts_in_queue", "merges_in_queue", "log_max_index",
        "log_pointer", "total_replicas", "active_replicas", "last_queue_update",
        "zookeeper_path", "replica_name", "replica_path", "columns_version", "lost_part_count",
    )

    def __init__(self, **kwargs: Any) -> None:
        self.database = kwargs.get("database", "")
        self.table = kwargs.get("table", "")
        self.engine = kwargs.get("engine", "ReplicatedMergeTree")
        self.is_leader = kwargs.get("is_leader", False)
        self.is_readonly = kwargs.get("is_readonly", False)
        self.absolute_delay = kwargs.get("absolute_delay", 0.0)
        self.queue_size = kwargs.get("queue_size", 0)
        self.inserts_in_queue = kwargs.get("inserts_in_queue", 0)
        self.merges_in_queue = kwargs.get("merges_in_queue", 0)
        self.log_max_index = kwargs.get("log_max_index", 0)
        self.log_pointer = kwargs.get("log_pointer", 0)
        self.total_replicas = kwargs.get("total_replicas", 0)
        self.active_replicas = kwargs.get("active_replicas", 0)
        self.last_queue_update = kwargs.get("last_queue_update", 0)
        self.zookeeper_path = kwargs.get("zookeeper_path", "")
        self.replica_name = kwargs.get("replica_name", "")
        self.replica_path = kwargs.get("replica_path", "")
        self.columns_version = kwargs.get("columns_version", 0)
        self.lost_part_count = kwargs.get("lost_part_count", 0)

    def as_row(self) -> Dict[str, Any]:
        """按 ``system.replicas`` 的列顺序输出(方便直接打印/比对)。"""
        return {f: getattr(self, f) for f in self.__slots__}

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ReplicatedTableStatus {self.replica_name} leader={self.is_leader} "
            f"queue={self.queue_size} delay={self.absolute_delay:.3f}s "
            f"pointer={self.log_pointer}/{self.log_max_index} readonly={self.is_readonly}>"
        )


# ══════════════════════════════════════════════════════════════════════════
# Replica —— 一个副本的协调状态机
# ══════════════════════════════════════════════════════════════════════════

#: 回调名 → 说明(供文档与错误信息使用)。
CALLBACK_NAMES = (
    "on_fetch_part", "on_attach_part", "on_merge_parts", "on_mutate",
    "on_alter_metadata", "on_drop_part", "on_drop_range", "on_drop_partition",
    "on_remove_part", "on_clear_column", "on_replace_range", "on_update_quorum",
)


class Replica:
    """一个副本的**协调状态机** —— 它不存数据,只决定"下一步该做什么"。

    数据在 :class:`PartStore`(模拟磁盘),协调在 Keeper(真的一致服务),
    这个类夹在中间:阅读 Keeper 的日志 → 调用表引擎回调去动数据 →
    推进指针。

    .. code-block:: text

       process_log_entries() 的主循环:

         log_pointer = 我已经执行到的编号
         while True:
             next = log_pointer + 1
             if next > Keeper 里最大的编号:  break        # 追平了
             entry = 读 /log/log-{next}
             ok = 执行 entry(调回调)                       # ← 必须幂等!
             if 执行失败:
                 条目留在队列里(不推指针),下次再试        # ← 真实 CH 的行为
                 break
             log_pointer = next
             写回 /replicas/<我> = next                    # ← 提交点

    提交点是"写回指针"这一步。如果副本在第 4 行和第 9 行之间崩溃,
    重启后它会**重放**这条 entry —— 所以执行必须是幂等的。

    .. warning::

       **mini-clickhouse 严格按编号顺序执行**,遇到执行不了的 entry 就
       停住。真实 CH 的队列允许**乱序执行**:它从中挑"当前可执行"的
       entry(依赖的 part 都到齐了),所以一个卡住的插入不会阻塞后面
       的合并。这是本模块最重要的一处简化 —— 影响的是吞吐与卡顿
       行为,不影响最终一致性。
    """

    def __init__(
        self,
        paths: ReplicatedTablePaths,
        replica_name: str,
        session_id: Optional[int] = None,
        *,
        part_store: Optional[PartStore] = None,
        callbacks: Optional[Dict[str, Callable[["Replica", LogEntry], bool]]] = None,
        dedup_window: int = 100,
        log_pointer: Optional[int] = None,
        logger: Optional[Logger] = None,
    ) -> None:
        self.paths = paths
        self.replica_name = replica_name
        self.session_id = session_id
        self.part_store = part_store if part_store is not None else PartStore(replica_name)
        self.part_store.replica_name = replica_name
        self.log = logger if logger is not None else get_logger("ReplicatedMergeTree")

        #: 表引擎给的回调。缺哪个就用"什么都不做、直接成功"的默认实现。
        self.callbacks: Dict[str, Callable[["Replica", LogEntry], bool]] = dict(callbacks or {})

        #: ``replicated_deduplication_window``(默认 100)—— 记住最近多少个 block。
        self.dedup_window = dedup_window

        #: 本地已知的每个 part 的状态。**这是副本的"账本"**,真相在 Keeper 的日志里。
        self.part_states: Dict[str, PartState] = {}

        #: 已经被某条待执行 entry 预留的 part(part 名 → 日志编号)。
        self.reserved_parts: Dict[str, int] = {}

        #: 执行失败的条目(编号 → entry)。真实 CH 会把它们留在队列里重试。
        self.failed_entries: Dict[int, LogEntry] = {}

        #: 丢失的 part 数(所有副本都没有)。
        self.lost_part_count = 0

        #: 只读标记。会话断掉 / 队列积压时置位。
        self.is_readonly = False

        #: 最近一次队列更新的时间(毫秒)。
        self.last_queue_update = int(time.time() * 1000)

        #: ``wait_for_quorum`` 期间用来"推进别的副本"的钩子 —— 单进程里
        #: 代替真实的多线程并发。:class:`LocalCluster` 会把它接上。
        self._pump: Optional[Callable[[], Any]] = None

        #: quorum 轮询间隔(毫秒)。
        self.quorum_poll_ms = 5

        #: ``insert_quorum_parallel=False`` 时用来串行化 quorum 插入。
        self._quorum_lock = threading.Lock()

        # 队列指针的本地缓存;真相在 /replicas/<我> 的 data 里。
        if log_pointer is None:
            log_pointer = paths.get_replica_log_pointer(replica_name, default=0)
        self.log_pointer = int(log_pointer)

        self._handlers: Dict[EntryType, Callable[[LogEntry], bool]] = {
            EntryType.EMPTY: self._apply_empty,
            EntryType.GET_PART: self._apply_get_part,
            EntryType.ATTACH_PART: self._apply_attach_part,
            EntryType.FETCH_PART: self._apply_fetch_part,
            EntryType.MERGE_PARTS: self._apply_merge_parts,
            EntryType.MUTATE_PART: self._apply_mutate_part,
            EntryType.ALTER_METADATA: self._apply_alter_metadata,
            EntryType.DROP_PART: self._apply_drop_part,
            EntryType.DETACH_PART: self._apply_drop_part,
            EntryType.REMOVE_PART: self._apply_remove_part,
            EntryType.DROP_RANGE: self._apply_drop_range,
            EntryType.DROP_PARTITION: self._apply_drop_partition,
            EntryType.CLEAR_COLUMN: self._apply_clear_column,
            EntryType.REPLACE_RANGE: self._apply_replace_range,
            EntryType.UPDATE_QUORUM: self._apply_update_quorum,
            EntryType.DROP_REPLICA: self._apply_drop_replica,
            EntryType.KILL: self._apply_empty,
        }

    # ── 生命期 ──────────────────────────────────────────────────────────

    def join(self) -> None:
        """加入复制:建 ``/replicas/<我>`` **临时节点** + 加入 leader 选举。

        临时节点是关键 —— 副本进程一死,这两个节点立刻消失,别的副本
        马上知道"它不在了",leader 也立刻重选。这就是整套机制里
        "故障检测"的全部实现(没有心跳超时轮询)。
        """
        self.paths.create_replica_node(self.replica_name, self.session_id, self.log_pointer)
        self.paths.join_election(self.replica_name, self.session_id)

    def on_session_lost(self) -> None:
        """Keeper 会话断了 —— 副本进入只读,直到重连并重建临时节点。

        真实 CH 在这个状态下:``is_readonly = 1``,拒绝 ``INSERT``,
        但**仍然服务 ``SELECT``**(读本地的 part 不需要 Keeper)。
        这也是为什么"Keeper 挂了"通常表现为"写入报错、查询还能跑"。
        """
        self.is_readonly = True
        self.session_id = None
        self.log.warning(f"Replica {self.replica_name}: Keeper session lost, switching to readonly")

    def check_writable(self) -> None:
        if self.is_readonly:
            raise ReplicaReadOnlyError(
                f"Replica {self.replica_name} is readonly (Keeper session lost or part lost)"
            )

    # ── leader ──────────────────────────────────────────────────────────

    def is_leader(self) -> bool:
        """当前 leader 是不是我 —— 直接问 Keeper,不看本地缓存。"""
        return self.paths.is_leader(self.replica_name)

    # ── 队列视图 ────────────────────────────────────────────────────────

    def pending_indices(self) -> List[int]:
        """还没执行的日志编号(升序)。"""
        max_index = self.paths.get_max_log_index()
        return list(range(self.log_pointer + 1, max_index + 1))

    def read_entry(self, index: int) -> Optional[LogEntry]:
        raw = self.paths.read_log_entry(index)
        if raw is None:
            return None
        entry = LogEntry.from_bytes(raw)
        entry.znode_name = self.paths.log_entry_name(index)
        entry.log_index = index
        return entry

    def pending_entries(self) -> List[Tuple[int, LogEntry]]:
        out: List[Tuple[int, LogEntry]] = []
        for index in self.pending_indices():
            entry = self.read_entry(index)
            if entry is not None:
                out.append((index, entry))
        return out

    def queue_size(self) -> int:
        max_index = self.paths.get_max_log_index()
        if max_index <= self.log_pointer:
            return 0
        return max_index - self.log_pointer

    # ── 日志应用(核心)───────────────────────────────────────────────

    def process_log_entries(
        self,
        max_entries: Optional[int] = None,
        raise_on_error: bool = False,
        force_advance: bool = False,
    ) -> int:
        """把日志从 ``log_pointer + 1`` 一路执行到最新。返回执行条数。

        这是 :class:`Replica` 的主循环,细节见类文档里的流程图。

        参数
        ----
        max_entries
            最多执行多少条(``None`` = 一直追平)。
        raise_on_error
            回调抛异常时是否直接往外抛(默认 False:记进
            :attr:`failed_entries`、不推进指针、停下)。
        force_advance
            **仅供测试/演示**:执行失败也强制推进指针。真实 CH **没有**
            这个开关 —— 它会一直重试,直到 part 找回来或人被叫醒。
        """
        if self.is_readonly and not force_advance:
            return 0
        applied = 0
        while max_entries is None or applied < max_entries:
            next_index = self.log_pointer + 1
            max_index = self.paths.get_max_log_index()
            if next_index > max_index:
                break

            entry = self.read_entry(next_index)
            if entry is None:
                # 日志被 cleanup 线程删掉了 —— 追不上了,只能等别人补
                self.log.warning(
                    f"{self.replica_name}: log entry {next_index} is missing "
                    f"(cleaned up?); pointer stuck"
                )
                break

            try:
                ok = self.apply_entry(entry)
            except Exception as exc:
                entry.exception = str(exc)
                self.failed_entries[next_index] = entry
                self.log.error(
                    f"{self.replica_name}: failed to execute {entry.znode_name} "
                    f"({entry.type.label}): {exc}"
                )
                if raise_on_error:
                    raise
                break

            if not ok and not force_advance:
                # 数据层失败(比如源 part 在所有副本上都没了)。
                # 真实 CH:条目**留在队列里**重试,副本可能转只读。
                # 指针不推进 —— 下次 process_log_entries 还会再试这条。
                self.failed_entries[next_index] = entry
                self.log.warning(
                    f"{self.replica_name}: entry {entry.znode_name} could not be "
                    f"applied (missing data); leaving it queued"
                )
                break

            if ok:
                self.failed_entries.pop(next_index, None)
            self.log_pointer = next_index
            self.last_queue_update = int(time.time() * 1000)
            self.paths.set_replica_log_pointer(self.replica_name, next_index)
            applied += 1
        return applied

    def apply_entry(self, entry: LogEntry) -> bool:
        """执行**单条** entry —— 幂等。

        幂等性不是"顺手实现的性质",而是本方法的契约:

        * ``GET_PART``:part 已在本地 → 直接成功(不重复下载);
        * ``MERGE_PARTS``:目标 part 已存在 → 只把残留的源 part 清掉,
          不重新合并;
        * ``ALTER_METADATA``:版本没变 → 什么都不做;
        * 删除类:目标不存在 → 视为成功(删除是幂等的)。

        测试会**故意对同一条 entry 调两次**来证明这一点。
        """
        handler = self._handlers.get(entry.type)
        if handler is None:
            self.log.warning(f"{self.replica_name}: unknown entry type {entry.type}")
            return True
        return bool(handler(entry))

    # ── 各类型 handler ──────────────────────────────────────────────────
    #
    # 职责划分(**这条规则贯穿全部 handler**):
    #
    #   * **回调(表引擎)** 负责动 `part_store` —— 把数据拉进来、合起来、
    #     删掉。真实 CH 里这些动作意味着读盘/写盘/HTTP。
    #   * **handler(这里)** 只负责**簿记**:决定这条 entry 涉及哪些 part,
    #     然后更新 `part_states` / `reserved_parts`。
    #
    # 对于"entry 里直接写明了 part 列表"的类型(GET_PART / MERGE_PARTS /
    # DROP_PART),回调自己就能从 entry 里读出要动哪些 part。对于**范围类**
    # (DROP_RANGE / DROP_PARTITION / REPLACE_RANGE),范围要**算**出来才知道
    # 涉及哪些 part,所以 handler 先算好、再通过一份"填好 parts 的 entry 副本"
    # 交给回调 —— 这样回调的签名保持统一,也不用重复实现范围判定。

    def _with_resolved_parts(self, entry: LogEntry, names: Sequence[str]) -> LogEntry:
        """复制一份 entry,把 ``parts`` 换成 handler 算好的 part 名单。"""
        sub = entry.clone()
        sub.parts = list(names)
        return sub

    def _parts_in_partition(self, partition_id: str) -> List[str]:
        return [p.name for p in self.part_store.parts.values()
                if p.partition_id == partition_id]

    def _apply_empty(self, entry: LogEntry) -> bool:
        return True

    def _apply_get_part(self, entry: LogEntry) -> bool:
        """从 ``source_replica`` 拉 ``new_part_name``。

        幂等点:**本地已有这个 part 就直接返回**。副本重启后重放日志时,
        绝大多数 GET_PART 都会走到这条捷径 —— 这也是"重放"在真实
        系统里代价不高的原因。
        """
        part_name = entry.new_part_name
        if self.part_store.has(part_name):
            self.part_states[part_name] = PartState.COMMITTED
            self._record_quorum_if_needed(entry)
            return True

        self.part_states[part_name] = PartState.FETCHING
        callback = self.callbacks.get("on_fetch_part")
        ok = bool(callback(self, entry)) if callback else False
        if not ok:
            self.part_states[part_name] = PartState.QUEUED
            self.lost_part_count += 1
            self.log.error(
                f"{self.replica_name}: part {part_name} not found on any replica "
                f"(source={entry.source_replica})"
            )
            return False

        self.part_states[part_name] = PartState.ATTACHING
        self.part_states[part_name] = PartState.COMMITTED
        self._record_quorum_if_needed(entry)
        return True

    def _apply_attach_part(self, entry: LogEntry) -> bool:
        """本地已有数据,直接"挂上" —— 不下载。幂等同上。"""
        part_name = entry.new_part_name
        if self.part_store.has(part_name):
            self.part_states[part_name] = PartState.COMMITTED
            self._record_quorum_if_needed(entry)
            return True
        callback = self.callbacks.get("on_attach_part")
        ok = bool(callback(self, entry)) if callback else False
        if ok:
            self.part_states[part_name] = PartState.COMMITTED
            self._record_quorum_if_needed(entry)
        return ok

    def _apply_fetch_part(self, entry: LogEntry) -> bool:
        """``FETCH_PART`` 只是"我要开始拉了"的标记 —— 真正干活的是 GET_PART。"""
        self.part_states.setdefault(entry.new_part_name, PartState.QUEUED)
        return True

    def _apply_merge_parts(self, entry: LogEntry) -> bool:
        """合并 ``entry.parts`` → ``entry.new_part_name``。

        **幂等的关键**在这段"目标已存在"的分支:重放日志时,合并结果
        通常已经在本地,这时不能直接返回成功就走 —— 否则**源 part 会
        永远残留**(它们是上一条 GET_PART 重放时又加回来的),
        副本之间就再也不收敛了。正确做法是**清掉源 part**。
        """
        new_name = entry.new_part_name

        if self.part_store.has(new_name):
            for src in entry.parts:
                if src != new_name:
                    self.part_store.remove(src)
                    self.part_states.pop(src, None)
                    self.reserved_parts.pop(src, None)
            self.part_states[new_name] = PartState.COMMITTED
            return True

        missing = [s for s in entry.parts if not self.part_store.has(s)]
        if missing:
            # 源 part 还没到 —— 真实 CH 会把这条 entry 留在队列里等,
            # 而不是失败。mini-clickhouse 顺序执行,所以直接停在这里。
            self._stage_reservation(entry)
            return False

        callback = self.callbacks.get("on_merge_parts")
        ok = bool(callback(self, entry)) if callback else False
        if not ok:
            return False
        for src in entry.parts:
            self.part_states.pop(src, None)
            self.reserved_parts.pop(src, None)
        self.part_states[new_name] = PartState.COMMITTED
        return True

    def _apply_mutate_part(self, entry: LogEntry) -> bool:
        callback = self.callbacks.get("on_mutate")
        ok = bool(callback(self, entry)) if callback else True
        if ok and entry.new_part_name:
            self.part_states[entry.new_part_name] = PartState.COMMITTED
        return ok

    def _apply_alter_metadata(self, entry: LogEntry) -> bool:
        """更新列结构。幂等:版本没变就什么都不做。

        真实 CH 的 ``ALTER_METADATA`` 会把 entry 里的 ``alter_version``
        和本地 ``/columns`` 的版本对比,落后才去拉完整定义。
        """
        if entry.alter_version and entry.alter_version <= self.paths.get_columns_version():
            return True
        callback = self.callbacks.get("on_alter_metadata")
        ok = bool(callback(self, entry)) if callback else True
        if ok and entry.alter_version:
            self.paths.set_columns(f"version={entry.alter_version}")
        return ok

    def _apply_drop_part(self, entry: LogEntry) -> bool:
        targets = [entry.new_part_name] + [p for p in entry.parts if p]
        targets = [t for t in targets if t]
        if not any(self.part_store.has(t) for t in targets):
            return True  # 已经没了 → 幂等成功
        callback = self.callbacks.get("on_drop_part")
        ok = bool(callback(self, entry)) if callback else True
        if ok:
            for name in targets:
                self.part_states.pop(name, None)
                self.reserved_parts.pop(name, None)
        return ok

    def _apply_remove_part(self, entry: LogEntry) -> bool:
        callback = self.callbacks.get("on_remove_part")
        ok = bool(callback(self, entry)) if callback else True
        if ok:
            for name in entry.parts:
                self.part_states.pop(name, None)
        return ok

    def _apply_drop_range(self, entry: LogEntry) -> bool:
        """删掉某分区里 **block 号落在 [from, to] 区间** 的 part。

        .. warning::

           **必须按 block 号比较,不能按 part 名字符串比较!**
           ``"all_1_1_0" > "all_10_10_0"`` 在字典序下成立(前两个字符
           ``"1_"`` 相同,接着 ``"1"`` > ``"0"``),字符串比较会把范围判反
           —— 这是真实 ClickHouse 里被踩过的经典坑,所以它把
           ``(min_block, max_block)`` 存成整数单独比较。

           区间判定是**相交**:只要 part 的 ``[min_block, max_block]``
           与 ``[from_block, to_block]`` 有交集就删。
        """
        from_block = self._part_block(entry.from_part, 1)
        to_block = self._part_block(entry.to_part, 2)

        resolved: List[str] = []
        for part in self.part_store.parts.values():
            if part.partition_id != entry.partition_id:
                continue
            if from_block is not None and part.max_block < from_block:
                continue
            if to_block is not None and part.min_block > to_block:
                continue
            resolved.append(part.name)

        callback = self.callbacks.get("on_drop_range")
        ok = bool(callback(self, self._with_resolved_parts(entry, resolved))) if callback else True
        if not ok:
            return False
        for name in resolved:
            self.part_states.pop(name, None)
        return True

    @staticmethod
    def _part_block(part_name: str, index: int) -> Optional[int]:
        """从 part 名里取 block 号 —— ``index`` 1=min_block,2=max_block。

        解析失败返回 ``None``,调用方据此把该边界当成"不限制"。
        part 名是磁盘上的东西,可能来自更老/更新的版本,所以这里
        **宁可放宽也不报错**。
        """
        if not part_name:
            return None
        try:
            return PartInfo.parse_name(part_name)[index]
        except Exception:
            return None

    def _apply_drop_partition(self, entry: LogEntry) -> bool:
        resolved = self._parts_in_partition(entry.partition_id)
        callback = self.callbacks.get("on_drop_partition")
        ok = bool(callback(self, self._with_resolved_parts(entry, resolved))) if callback else True
        if not ok:
            return False
        for name in resolved:
            self.part_states.pop(name, None)
        return True

    def _apply_clear_column(self, entry: LogEntry) -> bool:
        callback = self.callbacks.get("on_clear_column")
        return bool(callback(self, entry)) if callback else True

    def _apply_replace_range(self, entry: LogEntry) -> bool:
        resolved = self._parts_in_partition(entry.partition_id)
        callback = self.callbacks.get("on_replace_range")
        return bool(callback(self, self._with_resolved_parts(entry, resolved))) if callback else True

    def _apply_update_quorum(self, entry: LogEntry) -> bool:
        callback = self.callbacks.get("on_update_quorum")
        return bool(callback(self, entry)) if callback else True

    def _apply_drop_replica(self, entry: LogEntry) -> bool:
        self.on_session_lost()
        return True

    # ── quorum ──────────────────────────────────────────────────────────

    def _record_quorum_if_needed(self, entry: LogEntry) -> None:
        """如果这条 entry 是个 quorum 插入,把自己记进 ``/quorum/<entry>``。

        判据就是 ``entry.quorum`` 非空 —— 发起副本会把自己写进去当
        初始成员,于是"这条 entry 需要 quorum"这个信息**搭在日志里**
        传给了所有副本,不需要额外的协调。
        """
        if entry.quorum and entry.znode_name:
            self.paths.record_quorum(entry.znode_name, self.replica_name)

    def wait_for_quorum(self, entry_name: str, quorum: int, timeout_ms: int) -> List[str]:
        """等到至少 ``quorum`` 个副本确认拥有这条 entry 的数据。

        .. code-block:: text

           发起副本:  /quorum/log-0000000005 = "replica_a"
           副本 b 执行完 GET_PART → 追加 "replica_b" → "replica_a\nreplica_b"
           发起副本轮询到 len() >= insert_quorum → INSERT 返回成功

        .. warning::

           **单进程里没有真正的并发**,所以这个循环每次轮询会调用
           ``self._pump()``(:class:`LocalCluster` 接的就是"让所有副本
           处理一遍日志")来假装别的副本在跑。真实 CH 里,别的副本是
           真的在另一个进程里并发执行的,发起副本只是单纯地等。
        """
        stopwatch = Stopwatch()
        while True:
            replicas = self.paths.get_quorum_replicas(entry_name)
            if len(replicas) >= quorum:
                return replicas
            if self._pump is not None:
                self._pump()
            if stopwatch.elapsed_ms() >= timeout_ms:
                raise QuorumTimeoutError(
                    f"Insert quorum timeout: only {len(replicas)}/{quorum} replicas "
                    f"confirmed for {entry_name} within {timeout_ms} ms"
                )
            time.sleep(self.quorum_poll_ms / 1000.0)

    # ── 插入(客户端路径)─────────────────────────────────────────────

    def insert_block(
        self,
        partition_id: str,
        block_id: str,
        *,
        rows: int = 1,
        size_bytes: int = 0,
        level: int = 0,
        insert_deduplicate: bool = True,
        raise_on_duplicate: bool = False,
        insert_quorum: int = 0,
        insert_quorum_timeout_ms: int = 600_000,
        insert_quorum_parallel: bool = True,
    ) -> InsertResult:
        """插入一个 block —— **刻意叫 ``block`` 而不是 ``rows``**。

        原因:去重的粒度是"块"而不是"行"。真实 CH 在 ``INSERT`` 时把
        数据切成若干块(默认 ``max_insert_block_size`` = 1048576 行),
        **每一块单独去重**。所以"同一条 INSERT 重发两次"会被去重
        拦住,但"把一个 block 拆成两块重发"拦不住。

        一次插入的完整步骤(每一步都对应真实 CH 里的一个动作):

        .. code-block:: text

           1. 去重检查   /blocks/<hash(block_id)> 已存在? → 直接返回 duplicate
           2. 取号       在 /block_numbers/<partition>/ 下取一个 block number
           3. part 名字  <partition>_<n>_<n>_<level>  ← 名字由号决定
           4. 写本地 part(在 mini-clickhouse 里就是往 PartStore 里放)
           5. 登记去重 + 追加日志  ← **这两步必须原子**(见下)
           6. quorum    如果 insert_quorum > 0,等足够多副本确认

        第 5 步用 Keeper 的 ``multi``/事务把它做成原子的:要么"去重标记
        和日志条目都有",要么都没有。真实 CH 把 ``/blocks`` 与
        ``/log`` 的写入放进**同一个 Keeper 事务**,原因就是怕出现
        "有日志、没标记"这种状态 —— 那会让去重彻底失效。

        关于两个关键设置:

        * ``insert_deduplicate``(默认 1):关掉它就是第 1 步不做检查。
          注意去重窗口由 ``replicated_deduplication_window``(默认 100)
          决定 —— **只认最近 100 个 block**;
        * ``insert_quorum``:见 :meth:`wait_for_quorum`。它真正的价值
          不是"写多份",而是让 **``INSERT`` 即使在客户端重试时也幂等**
          (quorum 未达成的插入会失败,重试时被去重拦住)。
        """
        self.check_writable()

        # ── 1. 去重检查(快路径)───────────────────────────────────────
        if insert_deduplicate and self.paths.has_block(block_id):
            self.log.notice(
                f"{self.replica_name}: block {block_id!r} already exists, "
                f"skipping (deduplicated)"
            )
            if raise_on_duplicate:
                raise DuplicateInsertError(
                    f"Block with id {block_id!r} already exists on replica set",
                    block_id=block_id,
                )
            return InsertResult(block_id=block_id, duplicate=True)

        # ── 2. 取 block number ────────────────────────────────────────
        try:
            block_number = self.paths.create_block_number(partition_id, self.session_id)
        except KeeperError as exc:
            self.on_session_lost()
            raise ReplicaReadOnlyError(
                f"Replica {self.replica_name} cannot allocate a block number: {exc}"
            )

        part_name = PartInfo.make_name(partition_id, block_number, block_number, level)
        part = PartInfo(
            name=part_name, partition_id=partition_id, min_block=block_number,
            max_block=block_number, level=level, rows=rows, size_bytes=size_bytes,
            block_id=block_id, state=PartState.COMMITTED,
        )

        # ── 3. 写本地 part(先落盘,再发日志)─────────────────────────
        # 顺序不能反:日志条目写的是"来 replica_x 拉这个 part",所以
        # **必须**先让 part 在本副本上真实存在,别人才可能拉到它。
        # 真实 CH 也是先写 part 到磁盘、再往 /log 追加条目。
        self.part_store.add(part)
        self.part_states[part_name] = PartState.COMMITTED

        # ── 4. quorum 名单 ────────────────────────────────────────────
        quorum_list = [self.replica_name] if insert_quorum > 0 else []
        entry = LogEntry.get_part(
            new_part_name=part_name, source_replica=self.replica_name,
            block_id=block_id, partition_id=partition_id, quorum=quorum_list,
        )

        # ── 5. 原子地"登记去重 + 追加日志" ────────────────────────────
        # 如果这一步发现 block 标记已经被别人抢先写下了(并发重复插入),
        # 我们就必须**把第 3 步留下的 part 撤掉**。否则本副本会永远留着
        # 一个"日志里根本没有"的孤儿 part:别的副本永远不会来拉它、
        # 也不会有人删它,副本之间就再也不收敛了。
        # 真实 CH 不会有这个问题 —— 它的 sink 在写 part 之前就已经
        # 通过 Keeper 事务占好了 block 标记(见 _commit_insert_atomically)。
        try:
            entry_name, log_index = self._commit_insert_atomically(
                entry, block_id, block_number, deduplicate=insert_deduplicate
            )
        except BaseException as exc:
            # 第 3 步已经把 part 落到本地了,但"标记 + 日志"没有提交成功。
            # **任何**失败都必须把 part 撤掉 —— 不管失败原因是并发重复插入,
            # 还是 Keeper 报了别的错。留着它的后果是:本副本从此多出一个
            # 日志里根本不存在的 part,既不会被别人拉走、也不会被任何人删掉,
            # 副本之间**永久**不收敛。
            self.part_store.remove(part_name)
            self.part_states.pop(part_name, None)
            self.reserved_parts.pop(part_name, None)
            if isinstance(exc, DuplicateInsertError):
                self.log.notice(
                    f"{self.replica_name}: concurrent duplicate insert of {block_id!r}; "
                    f"rolled back the staged part {part_name}"
                )
                if raise_on_duplicate:
                    raise
                return InsertResult(block_id=block_id, duplicate=True,
                                    block_number=block_number)
            raise
        entry.znode_name = entry_name
        entry.log_index = log_index

        # 发起副本**自己**也算一个 quorum 成员 —— 它的 part 已经落盘了,
        # 不需要等自己"执行"自己写的那条日志。真实 CH 的 sink 也是这么做的。
        if insert_quorum > 0:
            self.paths.record_quorum(entry_name, self.replica_name)

        # ── 6. 修剪去重窗口 ──────────────────────────────────────────
        if self.dedup_window > 0:
            self.paths.prune_blocks(self.dedup_window)

        # ── 7. quorum:等别人确认 ─────────────────────────────────────
        quorum_replicas: List[str] = []
        if insert_quorum > 0:
            if insert_quorum_parallel:
                quorum_replicas = self.wait_for_quorum(
                    entry_name, insert_quorum, insert_quorum_timeout_ms
                )
            else:
                # 非并行:同一时刻只允许一个 quorum 插入在等 —— 真实 CH
                # 用这个设置来保证 quorum 的"顺序性"(相邻两次插入的
                # quorum 名单不会交叉)。
                with self._quorum_lock:
                    quorum_replicas = self.wait_for_quorum(
                        entry_name, insert_quorum, insert_quorum_timeout_ms
                    )

        self.log.information(
            f"{self.replica_name} inserted {part_name} (block {block_id!r}) "
            f"as log entry {entry_name}"
        )
        return InsertResult(
            part_name=part_name, block_id=block_id, block_number=block_number,
            log_index=log_index, entry_name=entry_name, duplicate=False,
            quorum_replicas=quorum_replicas, quorum_size=insert_quorum,
        )

    def _commit_insert_atomically(self, entry: LogEntry, block_id: str,
                                  block_number: int, deduplicate: bool = True) -> Tuple[str, int]:
        """把"写 ``/blocks`` 标记"和"追加 ``/log`` 条目"放进一个事务。

        如果这两步之间崩了,去重就会失效(有日志、没标记 → 重发时
        找不到标记 → 重复数据)。真实 CH 用 Keeper 的 ``multi`` 解决,
        这里用同一套原语(见 :meth:`KeeperServer.multi`)。

        ``deduplicate=False``(即 ``insert_deduplicate=0``)时,标记节点
        用 ``ignore_existing`` 写 —— 这样同一个 block id 再插一次也**不会**
        被当成重复。真实 CH 在关掉去重时也是这样"照写不误、不做判定"。
        """
        storage = self.paths.keeper
        ops = [
            Create(self.paths.block_path(block_id), str(block_number).encode("ascii"),
                   ignore_existing=not deduplicate),
            Create(self.paths.log_path() + "/log-", entry.to_bytes(),
                   sequential=True, session_id=self.session_id),
        ]
        try:
            with storage.transaction():
                results = [op.apply(storage) for op in ops]
        except NodeExistsError as exc:
            # 并发(或去重窗口之外的)重复插入 —— 当成"命中去重"
            raise DuplicateInsertError(
                f"Block with id {block_id!r} was inserted concurrently", block_id=block_id
            ) from exc
        real_log_path = results[1]
        name = real_log_path.rsplit("/", 1)[1]
        return name, int(name[4:])

    # ── 合并(leader 路径)────────────────────────────────────────────

    def _stage_reservation(self, entry: LogEntry) -> None:
        """把这条 entry 的源 part 记成"已预留",避免被别的合并挑走。"""
        for src in entry.parts:
            self.reserved_parts[src] = entry.log_index

    def run_merge_iteration(
        self,
        selector: Optional[Callable[["Replica"], Optional[MergeCandidate]]] = None,
    ) -> Optional[LogEntry]:
        """**只有 leader** 会走到这一步:挑一组 part、写一条 MERGE_PARTS 日志。

        非 leader 副本什么都不做 —— 它们只是**执行** leader 写下的日志。
        这就是"为什么合并不会打架":决策权在一个副本手里,其余副本
        只是观众。

        挑中之后:
        1. 给这次合并取一个 block number(用于去重 —— 合出来的 part
           也是一个新 block,重启重放时不能再合一次);
        2. 预留源 part(写进 :attr:`reserved_parts`),避免下一次
           挑选又把它们选进去;
        3. 写 MERGE_PARTS entry。

        .. warning::

           真实 CH 里 leader 是**周期性**做这件事的(``BackgroundSchedulePool``),
           而且它在写日志前还要处理一堆情况:队列里有待执行的同名
           合并要跳过、part 正在被 fetch 要跳过、超过
           ``number_of_free_entries_in_pool_to_lower_max_size_of_merge``
           要缩小合并规模……mini-clickhouse 只做"挑 + 预留 + 写"。
        """
        if not self.is_leader():
            return None
        if self.is_readonly:
            return None

        choose = selector or default_merge_selector
        candidate = choose(self)
        if candidate is None:
            return None

        block_number = self.paths.create_block_number(candidate.partition_id, self.session_id)
        merged_block_id = f"merge-{candidate.new_part_name}-{block_number}"
        entry = LogEntry.merge_parts(
            source_parts=candidate.source_names,
            new_part_name=candidate.new_part_name,
            source_replica=self.replica_name,
            block_id=merged_block_id,
            partition_id=candidate.partition_id,
        )
        entry.block_id = merged_block_id

        # 原子地:登记合并结果的去重标记 + 追加日志条目
        storage = self.paths.keeper
        with storage.transaction():
            storage.create(self.paths.block_path(merged_block_id),
                           str(block_number).encode("ascii"), ignore_existing=True)
            name, index = self.paths.append_log_entry(entry.to_bytes(), session_id=self.session_id)

        entry.znode_name = name
        entry.log_index = index
        self._stage_reservation(entry)

        self.log.information(
            f"Leader {self.replica_name} scheduled merge {candidate.source_names} "
            f"-> {candidate.new_part_name} as {name}"
        )
        return entry

    # ── 状态 ────────────────────────────────────────────────────────────

    def get_status(self) -> ReplicatedTableStatus:
        """算出 ``system.replicas`` 的那一行 —— ``absolute_delay`` 与
        ``queue_size`` 都是**真实读数**,不是常量。"""
        pending = self.pending_entries()
        now = int(time.time() * 1000)

        # absolute_delay:队列里最老的 entry 等了多久(秒)
        if pending:
            oldest = min(e.create_time for _, e in pending)
            absolute_delay = max(0.0, (now - oldest) / 1000.0)
        else:
            absolute_delay = 0.0

        inserts_in_queue = sum(1 for _, e in pending if e.is_insert())
        merges_in_queue = sum(1 for _, e in pending if e.is_merge())

        active = self.paths.get_active_replicas()
        total_replicas = len(active)
        # 真实 CH:节点存在但 data 为空 = "还没完全启动" → 不算 active
        active_replicas = 0
        for name in active:
            if self.paths.get_replica_pointer_or_none(name) is not None:
                active_replicas += 1

        return ReplicatedTableStatus(
            database=self.paths.database,
            table=self.paths.table,
            engine="ReplicatedMergeTree",
            is_leader=self.is_leader(),
            is_readonly=self.is_readonly,
            absolute_delay=absolute_delay,
            queue_size=len(pending),
            inserts_in_queue=inserts_in_queue,
            merges_in_queue=merges_in_queue,
            log_max_index=self.paths.get_max_log_index(),
            log_pointer=self.log_pointer,
            total_replicas=total_replicas,
            active_replicas=active_replicas,
            last_queue_update=self.last_queue_update,
            zookeeper_path=self.paths.root,
            replica_name=self.replica_name,
            replica_path=self.paths.replica_path(self.replica_name),
            columns_version=self.paths.get_columns_version(),
            lost_part_count=self.lost_part_count,
        )

    def status_dict(self) -> Dict[str, Any]:
        return self.get_status().as_row()

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Replica {self.replica_name!r} pointer={self.log_pointer} "
            f"parts={len(self.part_store)} leader={self.paths.is_leader(self.replica_name)} "
            f"readonly={self.is_readonly}>"
        )


# ══════════════════════════════════════════════════════════════════════════
# LocalCluster —— 进程内跑 N 个副本的测试台
# ══════════════════════════════════════════════════════════════════════════


class LocalCluster:
    """在**一个进程里**跑 N 个 :class:`Replica`,共享一个 :class:`KeeperServer`。

    这是教程与测试用的仿真台,它能把复制的关键剧情演一遍:

    .. code-block:: text

       cluster = LocalCluster(["replica_a", "replica_b", "replica_c"])

       cluster.insert("replica_a", partition_id="all", block_id="b1")
       cluster.process_all()                       # b、c 跟上 → 三副本一致

       cluster.kill_replica("replica_a")           # 会话断 → 临时节点消失
       print(cluster.leader())                     # → replica_b(自动重选)
       cluster.process_all()
       cluster.merge_round()                       # 新 leader 做合并
       cluster.process_all()                       # 所有人应用这条合并

       cluster.assert_converged()                  # 断言最终一致

    .. warning::

       **单进程 ⇒ 没有真并发,也没有真网络。** 后果:

       * "副本死了"是 ``close_session`` 模拟的,不是进程退出;
       * part 传输是内存拷贝,没有断点续传、没有校验和;
       * 时序竞争(两个副本同时抢 same block number、同时挑同一个合并)
         在这里**不会自发发生** —— 想演示得手工构造。

       它忠实模拟的是**协议语义**(谁写日志、谁读日志、写什么、
       去重规则、选主规则),不是物理过程。
    """

    def __init__(
        self,
        replica_names: Sequence[str] = ("replica_a", "replica_b", "replica_c"),
        shard: str = "01",
        table: str = "t",
        database: str = "default",
        *,
        dedup_window: int = 100,
        session_timeout_ms: int = DEFAULT_SESSION_TIMEOUT_MS,
        merge_selector: Optional[Callable[["Replica"], Optional[MergeCandidate]]] = None,
        auto_start: bool = True,
        heartbeat: bool = True,
        heartbeat_interval_ms: Optional[int] = None,
    ) -> None:
        self.keeper = KeeperServer(session_timeout_ms=session_timeout_ms)
        self.session_timeout_ms = session_timeout_ms
        #: 心跳间隔 —— 默认取 ``timeout/3``,这是 ZooKeeper 客户端的通行做法
        #: (任何一次心跳丢了都还有两次重试机会,不会误判超时)。
        self.heartbeat_interval_ms = (
            max(1, session_timeout_ms // 3) if heartbeat_interval_ms is None
            else max(1, heartbeat_interval_ms)
        )
        self._members_lock = threading.Lock()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_stop = threading.Event()
        if auto_start:
            self.keeper.start()
        self.paths = ReplicatedTablePaths(
            self.keeper.storage, shard=shard, table=table, database=database
        )
        self.paths.ensure_created()
        self.paths.set_metadata(
            f"CREATE TABLE {database}.{table} (...) ENGINE = ReplicatedMergeTree "
            f"('/clickhouse/tables/{shard}/{table}', '{{replica}}')"
        )
        self.dedup_window = dedup_window
        self.merge_selector = merge_selector or default_merge_selector
        self.part_stores: Dict[str, PartStore] = {}
        self.replicas: Dict[str, Replica] = {}
        self.log: Logger = get_logger("ReplicatedMergeTree")
        for name in replica_names:
            self.add_replica(name)
        # 心跳只在 keeper 也跑起来时才需要(没跑就没有 reaper,也就没人回收会话)
        if auto_start and heartbeat:
            self.start_heartbeat()

    # ── 心跳 ────────────────────────────────────────────────────────────

    def start_heartbeat(self) -> bool:
        """启动后台心跳线程。**已经在跑就返回 False(幂等)。**

        .. warning::

           **没有心跳,集群会被自己的 reaper 杀死。** ``/replicas/<r>`` 与
           ``leader_election`` 下的节点都是**临时**节点,而
           :class:`~minich.keeper.KeeperServer` 的后台 reaper 会把
           "超过 ``session_timeout_ms`` 没心跳"的会话连同它的临时节点
           一起删掉。真实 ClickHouse 客户端库在后台自动发 ``PING``,
           所以用户感知不到这件事 —— 但仿真台必须显式做,否则
           集群只要活得比 ``session_timeout_ms`` 长(默认 30 秒),
           就会**悄悄地**全部掉线、leader 消失。

           心跳是**幂等且无副作用**的:它只刷新 ``last_touched``,
           不改变任何树状态。
        """
        if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
            return False
        self._heartbeat_stop.clear()
        thread = threading.Thread(
            target=self._heartbeat_loop, name="minich-cluster-heartbeat", daemon=True
        )
        self._heartbeat_thread = thread
        thread.start()
        return True

    def _heartbeat_loop(self) -> None:
        interval = max(self.heartbeat_interval_ms, 1) / 1000.0
        while not self._heartbeat_stop.wait(interval):
            try:
                self.heartbeat_once()
            except Exception as exc:  # pragma: no cover - 心跳线程不能因异常死掉
                self.log.error(f"LocalCluster heartbeat error: {exc}")

    def heartbeat_once(self) -> int:
        """给所有**活着**的副本会话发一次心跳,返回成功数。

        已经死掉的副本(``session_id is None``,被 :meth:`kill_replica`
        置空)会被跳过 —— 心跳**不能**把死副本救活。
        """
        with self._members_lock:
            replicas = list(self.replicas.values())
        touched = 0
        for replica in replicas:
            sid = replica.session_id
            if sid is None:
                continue
            try:
                self.keeper.touch(sid)
                touched += 1
            except Exception:
                # 会话已被回收(比如测试里手动 expire 过)—— 忽略
                pass
        return touched

    def stop_heartbeat(self, join_timeout: float = 2.0) -> bool:
        """停掉心跳线程并 join。**没在跑就返回 False(幂等),不泄漏线程。**"""
        if self._heartbeat_thread is None:
            return False
        self._heartbeat_stop.set()
        thread, self._heartbeat_thread = self._heartbeat_thread, None
        thread.join(join_timeout)
        return True

    # ── 成员管理 ────────────────────────────────────────────────────────

    def add_replica(self, name: str, log_pointer: int = 0) -> Replica:
        """加一个副本:开会话 → 建临时节点 → 加入选主 → 挂上回调。"""
        session = self.keeper.new_session()
        store = PartStore(name)
        self.part_stores[name] = store
        replica = Replica(
            self.paths, name, session.session_id,
            part_store=store,
            callbacks=self._make_callbacks(name),
            dedup_window=self.dedup_window,
            log_pointer=log_pointer,
        )
        # quorum 等待时"推进其它副本" —— 单进程里代替真实并发
        replica._pump = self.process_all
        replica.join()
        with self._members_lock:
            self.replicas[name] = replica
        return replica

    def _make_callbacks(self, name: str) -> Dict[str, Callable[[Replica, LogEntry], bool]]:
        """构造这个副本的"表引擎回调"。

        真实 ClickHouse 里这些动作由 ``StorageReplicatedMergeTree`` 的
        一大串方法实现(拉数据走 HTTP、合并走 MergeTree 的合并器)。
        这里全部用内存 PartStore 的对拷代替。
        """

        def on_fetch_part(replica: Replica, entry: LogEntry) -> bool:
            # 优先从 entry 指定的 source 拉;它可能已经合并掉了这个 part,
            # 所以再兜底扫一遍所有副本(真实 CH 也会换一个副本重试)。
            candidates = [entry.source_replica] + [n for n in self.part_stores if n != entry.source_replica]
            for source in candidates:
                if source == replica.replica_name:
                    continue
                src_store = self.part_stores.get(source)
                if src_store is None:
                    continue
                info = src_store.get(entry.new_part_name)
                if info is not None:
                    replica.part_store.add(info.copy())
                    return True
            return False

        def on_attach_part(replica: Replica, entry: LogEntry) -> bool:
            return replica.part_store.has(entry.new_part_name)

        def on_merge_parts(replica: Replica, entry: LogEntry) -> bool:
            store = replica.part_store
            if store.has(entry.new_part_name):
                # 幂等:合出来的 part 已在 → 清掉残留源 part
                for src in entry.parts:
                    store.remove(src)
                return True
            sources = [store.get(s) for s in entry.parts]
            if any(s is None for s in sources):
                return False
            merged = PartInfo(
                name=entry.new_part_name,
                partition_id=sources[0].partition_id,
                min_block=min(s.min_block for s in sources),
                max_block=max(s.max_block for s in sources),
                level=max(s.level for s in sources) + 1,
                rows=sum(s.rows for s in sources),
                size_bytes=sum(s.size_bytes for s in sources),
                block_id=entry.block_id,
                state=PartState.COMMITTED,
            )
            for src in entry.parts:
                store.remove(src)
            store.add(merged)
            return True

        def on_mutate(replica: Replica, entry: LogEntry) -> bool:
            for src in entry.parts:
                info = replica.part_store.get(src)
                if info is not None:
                    info.mutation = entry.mutation_id
            return True

        def on_alter_metadata(replica: Replica, entry: LogEntry) -> bool:
            self.log.information(
                f"{name}: applying ALTER_METADATA version {entry.alter_version}"
            )
            return True

        # 删除类回调**只动 entry.parts 里点名的 part** —— 范围类 entry
        # 的 parts 已由 Replica 的 handler 算好填进来了(见 Replica 的
        # "职责划分" 注释),回调不许自己再按 partition 猜。
        def on_drop_part(replica: Replica, entry: LogEntry) -> bool:
            for src in list(entry.parts) + [entry.new_part_name]:
                if src:
                    replica.part_store.remove(src)
            return True

        def on_drop_range(replica: Replica, entry: LogEntry) -> bool:
            for src in entry.parts:
                replica.part_store.remove(src)
            return True

        def on_drop_partition(replica: Replica, entry: LogEntry) -> bool:
            for src in entry.parts:
                replica.part_store.remove(src)
            return True

        def on_remove_part(replica: Replica, entry: LogEntry) -> bool:
            for src in entry.parts:
                replica.part_store.remove(src)
            return True

        def on_replace_range(replica: Replica, entry: LogEntry) -> bool:
            for src in entry.parts:
                replica.part_store.remove(src)
            return True

        def on_clear_column(replica: Replica, entry: LogEntry) -> bool:
            return True

        return {
            "on_fetch_part": on_fetch_part,
            "on_attach_part": on_attach_part,
            "on_merge_parts": on_merge_parts,
            "on_mutate": on_mutate,
            "on_alter_metadata": on_alter_metadata,
            "on_drop_part": on_drop_part,
            "on_drop_range": on_drop_range,
            "on_drop_partition": on_drop_partition,
            "on_remove_part": on_remove_part,
            "on_replace_range": on_replace_range,
            "on_clear_column": on_clear_column,
        }

    def replica(self, name: str) -> Replica:
        return self.replicas[name]

    def active_replica_names(self) -> List[str]:
        return self.paths.get_active_replicas()

    def leader(self) -> Optional[str]:
        """当前合并 leader 的副本名(问 Keeper,不是本地缓存)。"""
        return self.paths.get_leader()

    def leaders(self) -> List[str]:
        """所有"认为自己是 leader"的副本 —— 正常情况下最多 1 个。"""
        return [name for name, r in self.replicas.items() if r.is_leader()]

    # ── 插入 ────────────────────────────────────────────────────────────

    def insert(self, replica_name: str, partition_id: str, block_id: str, **kwargs: Any) -> InsertResult:
        """在指定副本上插入 —— 等价于"客户端连到了这个副本"。"""
        return self.replicas[replica_name].insert_block(partition_id, block_id, **kwargs)

    # ── 推进 ────────────────────────────────────────────────────────────

    def process_replica(self, name: str, max_entries: Optional[int] = None) -> int:
        return self.replicas[name].process_log_entries(max_entries=max_entries)

    def process_all(self, max_entries: Optional[int] = None, max_rounds: int = 32) -> int:
        """让所有副本各处理一遍日志,直到**没有副本再有进展**。

        为什么要循环?因为"应用一条 entry"可能产生新的 entry
        (比如一个副本 fetch 完发现还需要另一个 part)。真实 CH 里
        这是后台线程持续在做的事,这里用有界轮次模拟。

        注意 ``is_readonly`` 的副本会被跳过(它已经不能碰 Keeper 了)。
        """
        total = 0
        for _ in range(max_rounds):
            progressed = 0
            for name, replica in self.replicas.items():
                if replica.is_readonly:
                    continue
                progressed += replica.process_log_entries(max_entries=max_entries)
            total += progressed
            if progressed == 0:
                break
        return total

    # ── 故障注入 ────────────────────────────────────────────────────────

    def kill_replica(self, name: str) -> Optional[str]:
        """让副本"死掉" —— 关掉它的 Keeper 会话。

        瞬间发生的事(全部由"临时节点随会话消失"这一条性质驱动):

        .. code-block:: text

           会话关闭
             ├── /replicas/<name>       消失  → 活跃副本数 -1
             ├── /leader_election/n-xxx 消失  → 若它是 leader,别人自动上位
             └── /block_numbers/...     消失  → 它占的号还回去

        返回新的 leader(方便写断言)。
        """
        replica = self.replicas[name]
        if replica.session_id is not None:
            self.keeper.close_session(replica.session_id, expired=True)
        replica.on_session_lost()
        new_leader = self.leader()
        self.log.information(f"Replica {name} killed; leader is now {new_leader!r}")
        return new_leader

    def expire_replica(self, name: str) -> Optional[str]:
        """:meth:`kill_replica` 的别名(强调"会话过期"这条路径)。"""
        return self.kill_replica(name)

    def revive_replica(self, name: str, *, replay_from: int = 0) -> Replica:
        """让"死掉"的副本重启:重开会话、重建临时节点、**从日志重放**。

        ``replay_from`` 默认 0 —— 也就是**从头重放整条日志**。
        这不是为了折磨人,而是真实情况下副本重启后**别无选择**:
        它的队列指针存在临时节点里,会话一断那个指针就没了。所以
        副本必须靠"重放日志 + 幂等的执行"把自己带回正确状态。

        这正是本章反复强调幂等的原因 —— 也是这个方法存在的意义:
        重放之后,副本的表状态必须和其他副本**完全一致**。
        """
        old = self.replicas[name]
        session = self.keeper.new_session()
        # 保留磁盘上的 part(replica 重启用的是同一块盘),但指针归零
        old.session_id = session.session_id
        old.is_readonly = False
        old.failed_entries.clear()
        old.log_pointer = replay_from
        old.paths.create_replica_node(name, session.session_id, replay_from)
        old.paths.join_election(name, session.session_id)
        self.log.information(f"Replica {name} revived; replaying log from {replay_from}")
        return old

    # ── 合并 ────────────────────────────────────────────────────────────

    def merge_round(self) -> Optional[LogEntry]:
        """让当前 leader 做一轮合并决策,并把产生的 entry 应用到所有副本。

        返回 leader 写下的 entry;没有可合并的东西时返回 ``None``。
        """
        leader_name = self.leader()
        if leader_name is None or leader_name not in self.replicas:
            return None
        entry = self.replicas[leader_name].run_merge_iteration(self.merge_selector)
        if entry is None:
            return None
        self.process_all()
        return entry

    # ── 观测 ────────────────────────────────────────────────────────────

    def state(self) -> Dict[str, Dict[str, Any]]:
        """每个副本的可比较状态快照 —— 测试里直接 ``==`` 用。"""
        out: Dict[str, Dict[str, Any]] = {}
        for name, replica in self.replicas.items():
            out[name] = {
                "parts": [p for p in replica.part_store.signature()],
                "rows": replica.part_store.total_rows(),
                "log_pointer": replica.log_pointer,
                "is_leader": replica.is_leader(),
                "is_readonly": replica.is_readonly,
            }
        return out

    def assert_converged(self, only_active: bool = True) -> None:
        """断言所有(活跃)副本的**表状态**完全一致 —— 复制的终极目标。

        ``only_active=False`` 会把"死掉的"副本也算进来 —— 那通常会失败,
        因为死掉的副本停在了它死的那一刻。
        """
        signatures: Dict[str, Any] = {}
        for name, replica in self.replicas.items():
            if only_active and replica.is_readonly:
                continue
            signatures[name] = replica.part_store.signature()
        if len(set(repr(s) for s in signatures.values())) > 1:
            detail = "\n".join(f"  {n}: {s}" for n, s in signatures.items())
            raise AssertionError(f"Replicas did not converge:\n{detail}")

    def status_rows(self) -> Dict[str, Dict[str, Any]]:
        """``system.replicas`` 的全部行(每个副本一行)。"""
        return {name: r.status_dict() for name, r in self.replicas.items()}

    def dump(self) -> str:
        lines = ["=== LocalCluster ===", self.keeper.dump(), "=== replicas ==="]
        for name, replica in self.replicas.items():
            lines.append(
                f"  {name}: parts={replica.part_store.names()} rows={replica.part_store.total_rows()} "
                f"pointer={replica.log_pointer} leader={replica.is_leader()} "
                f"readonly={replica.is_readonly}"
            )
        return "\n".join(lines)

    def stop(self) -> None:
        """停掉心跳线程与 keeper 的 reaper 线程(**两个都要停**)。

        幂等;不调用会留下两个 daemon 线程。顺序上先停心跳再停
        keeper —— 反过来的话,心跳可能在 reaper 已经停掉之后还去
        ``touch`` 一个正在被回收的会话(虽然也只会被忽略,但没必要)。
        """
        self.stop_heartbeat()
        self.keeper.stop()

    def __enter__(self) -> "LocalCluster":
        self.keeper.start()
        self.start_heartbeat()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<LocalCluster replicas={list(self.replicas)}>"

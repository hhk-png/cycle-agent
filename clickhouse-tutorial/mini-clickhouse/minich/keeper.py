"""Keeper —— mini-clickhouse 的迷你 ClickHouse Keeper(也就是 ZooKeeper)。

**这个模块是复制(Replication)的地基。** 先理解一件事,它解释了
``ReplicatedMergeTree`` 为什么长成那个样子:

.. code-block:: text

   ❌ 常见误解:副本之间互相"打招呼"(gossip)来同步
   ✅ 真实情况:副本之间**从不直接说话** —— 它们都连到一个
              **独立的一致性服务**上,靠它达成一致

   +-----------+          +-----------------------+
   | replica A |<-------->|                       |
   +-----------+          |   ClickHouse Keeper   |   <-- 单独一个进程/集群
   +-----------+          |   (ZooKeeper 协议)     |       必须自己先有一致性
   | replica B |<-------->|                       |
   +-----------+          |   /clickhouse/tables/ |      副本 A 写一条日志
   +-----------+          |      01/t/log         |      副本 B/C 读这条日志
   | replica C |<-------->|                       |      谁都不直接连谁
   +-----------+          +-----------------------+

   副本 A 插入数据 → 往 Keeper 的 ``/log`` 下追加一条"我有新 part"的记录;
   副本 B、C 各自从 Keeper 读到这条记录 → 去 A 那里把 part 拉过来。
   **Keeper 只负责"达成一致",不负责传数据**(数据走副本之间的 HTTP)。

Keeper 是 ClickHouse 自己用 C++ 重写的 ZooKeeper(协议兼容),
代码在 ``src/Coordination/`` 下。它的数据模型是一棵 **znode 树**
(ZooKeeper Node),每个节点:

* 有一个**路径**(``/clickhouse/tables/01/t/log/log-0000000001``);
* 有一小段**数据**(通常在几 KB 以内 —— Keeper 不是数据库,别塞大 blob);
* 有一份 **stat**(版本号、创建/修改时间、zxid……);
* 可以有一堆**子节点**。

.. code-block:: text

   /                                         <- 根
   └── clickhouse
       └── tables
           └── 01                            <- shard
               └── t                         <- 表名
                   ├── log                   <- 复制日志(顺序节点序列!)
                   │   ├── log-0000000001     data = 一条 LogEntry
                   │   ├── log-0000000002
                   │   └── log-0000000003
                   ├── replicas              <- 每个副本一个**临时节点**
                   │   ├── replica_a         data = "3"(队列指针)
                   │   └── replica_b         data = "1"
                   ├── leader_election       <- 选主用的临时顺序节点
                   │   └── n-0000000000      data = "replica_a"
                   ├── blocks                <- 插入去重标记
                   │   └── 9f3c...          data = "7"(block number)
                   ├── block_numbers         <- 每个分区一个"取号机"
                   │   └── all
                   │       └── block-0000000007
                   └── metadata / columns / quorum / ...

为什么是这棵树、而不是"一张表存所有状态"?因为:

1. **临时节点(ephemeral)** 让"进程死了"这件事**自动、即时**地可见 ——
   会话一断,它建的临时节点就消失。`/replicas/replica_a` 不见了
   就等于"replica_a 掉线了",不需要心跳超时轮询、也不需要谁去删。
   这一条性质是 Keeper 被所有分布式系统选中的**根本原因**;
2. **顺序节点(sequential)** 让"追加并全局排序"变成一个原子操作 ——
   `/log/log-` 前缀 + 自增编号,天然就是一条**全局有序的复制日志**;
3. **watch** 让副本不用轮询 —— 日志一追加就通知所有订阅者。

.. warning::

   **mini-clickhouse 的 Keeper 是单节点的,没有共识。**
   真实 Keeper 内部跑 Raft(早期 ZooKeeper 是 Zab),由多数派
   (quorum)保证"写进去就是真的、且所有节点看到同一条日志"。
   mini-clickhouse 只有一个内存实例、一把 ``RLock``,所以:

   * 它可以**建模**复制(日志怎么追加、指针怎么推进、谁当 leader);
   * 它**不能**演示两个真实故障场景:**脑裂(split-brain)** 与
     **丢失多数派(quorum loss)** —— 这两个场景需要"网络能分区、
     节点会各自选举"的前提,单节点没有这个前提。
   教程里讲到这两点时只能说清机制,不能真的复现;

.. warning::

   真实 Keeper 的 watch 是**一次性**的(one-shot):触发一次就自动注销,
   想要持续监听必须重新注册 —— 这是 ZooKeeper 最著名的"坑"
   (:meth:`KeeperStorage.add_watch` 的文档里有详解)。ClickHouse 的 Keeper
   在 Zodiac 协议上新加了**持久 watch / watch 列表**
   (``AddWatchMode::PERSISTENT``),mini-clickhouse 用 ``persistent=True``
   参数模拟它。

.. note::

   本模块**刻意不依赖** :mod:`minich.settings`、:mod:`minich.storages`、
   :mod:`minich.execution` —— 它只用到 ``errors`` / ``util`` 两个基础层,
   所以可以脱离"数据库"单独跑、单独测。这正是 Keeper 在架构上的位置:
   **它在数据库之外**。
"""

from __future__ import annotations

import contextlib
import copy
import random
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

from .errors import MiniChError
from .util.hashing import siphash64
from .util.logger import Logger, get_logger

__all__ = [
    # 错误码与异常
    "KeeperErrorCode",
    "KeeperError",
    "NoNodeError",
    "NodeExistsError",
    "NotEmptyError",
    "BadVersionError",
    "NoChildrenForEphemeralsError",
    "SessionExpiredError",
    "UnknownSessionError",
    "NothingError",
    "ConnectionLossError",
    "BadArgumentsError",
    "raise_keeper_error",
    # 数据模型
    "ZNodeStat",
    "ZNode",
    "Session",
    "CreateMode",
    "PERSISTENT",
    "EPHEMERAL",
    "PERSISTENT_SEQUENTIAL",
    "EPHEMERAL_SEQUENTIAL",
    # watch
    "WatchEvent",
    "ZKNodeCreated",
    "ZKNodeDeleted",
    "ZKNodeDataChanged",
    "ZKNodeChildrenChanged",
    "ZK_ALL_EVENTS",
    # 存储 / 服务
    "KeeperStorage",
    "KeeperServer",
    "ReplicatedTablePaths",
    "MultiResult",
    "Create",
    "Set",
    "Delete",
    "Check",
    "Get",
    "GetChildren",
]


# ══════════════════════════════════════════════════════════════════════════
# ZooKeeper 错误码 —— 与 Apache ZooKeeper 的 ``KeeperException.Code`` 一致
# ══════════════════════════════════════════════════════════════════════════


class KeeperErrorCode:
    """ZooKeeper 错误码 —— **负数**,与 ClickHouse 的正数错误码是两套体系。

    .. code-block:: text

       ZooKeeper / Keeper 的错误长这样(负号,来自 KeeperException.Code):
           Code: -101. KeeperError: node does not exist (/a/b). (NONODE)

       ClickHouse 自己的错误长这样(正号,来自 ErrorCodes):
           Code: 60. DB::Exception: Table default.hits does not exist. (UNKNOWN_TABLE)

    为什么必须把两套编号分开?因为它们是**两个独立系统的接口**,两者的
    编号空间在历史上各自演化、互不相关。ClickHouse 把 Keeper 的错误
    包进自己的 ``ZooKeeperException``(继承 ``DB::Exception``),但**保留
    了原始的 ZK 错误码** —— 因为客户端判断"该不该重试"要靠它
    (``CONNECTIONLOSS`` 可重试,``NONODE`` 不可重试)。

    这里的编号是**照抄** ZooKeeper 官方 ``KeeperException.Code`` 的,
    所以拿 mini-clickhouse 的日志去搜 ZooKeeper 文档能直接对上。
    """

    OK = 0
    SYSTEMERROR = -1
    RUNTIMEINCONSISTENCY = -2
    DATAINCONSISTENCY = -3
    CONNECTIONLOSS = -4
    MARSHALLINGERROR = -5
    UNIMPLEMENTED = -6
    OPERATIONTIMEOUT = -7
    BADARGUMENTS = -8
    UNKNOWNSESSION = -12
    NONODE = -101
    NOAUTH = -102
    BADVERSION = -103
    NOCHILDRENFOREPHEMERALS = -108
    NODEEXISTS = -110
    NOTEMPTY = -111
    SESSIONEXPIRED = -112
    INVALIDCALLBACK = -113
    INVALIDACL = -114
    AUTHFAILED = -115
    CLOSING = -116
    NOTHING = -117
    SESSIONMOVED = -118
    NOTREADONLY = -119
    EPHEMERALONLOCALSESSION = -120
    NOWATCHER = -121
    REQUESTTIMEOUT = -122
    RECONFIGDISABLED = -123
    SESSIONCLOSEDREQUIRESASLAUTH = -124
    THROTTLEDOP = -127


#: 错误码 → 官方名字。异常的 ``.name`` 字段用这个。
ZK_CODE_TO_NAME: Dict[int, str] = {
    KeeperErrorCode.OK: "OK",
    KeeperErrorCode.SYSTEMERROR: "SYSTEMERROR",
    KeeperErrorCode.RUNTIMEINCONSISTENCY: "RUNTIMEINCONSISTENCY",
    KeeperErrorCode.DATAINCONSISTENCY: "DATAINCONSISTENCY",
    KeeperErrorCode.CONNECTIONLOSS: "CONNECTIONLOSS",
    KeeperErrorCode.MARSHALLINGERROR: "MARSHALLINGERROR",
    KeeperErrorCode.UNIMPLEMENTED: "UNIMPLEMENTED",
    KeeperErrorCode.OPERATIONTIMEOUT: "OPERATIONTIMEOUT",
    KeeperErrorCode.BADARGUMENTS: "BADARGUMENTS",
    KeeperErrorCode.UNKNOWNSESSION: "UNKNOWNSESSION",
    KeeperErrorCode.NONODE: "NONODE",
    KeeperErrorCode.NOAUTH: "NOAUTH",
    KeeperErrorCode.BADVERSION: "BADVERSION",
    KeeperErrorCode.NOCHILDRENFOREPHEMERALS: "NOCHILDRENFOREPHEMERALS",
    KeeperErrorCode.NODEEXISTS: "NODEEXISTS",
    KeeperErrorCode.NOTEMPTY: "NOTEMPTY",
    KeeperErrorCode.SESSIONEXPIRED: "SESSIONEXPIRED",
    KeeperErrorCode.INVALIDCALLBACK: "INVALIDCALLBACK",
    KeeperErrorCode.INVALIDACL: "INVALIDACL",
    KeeperErrorCode.AUTHFAILED: "AUTHFAILED",
    KeeperErrorCode.CLOSING: "CLOSING",
    KeeperErrorCode.NOTHING: "NOTHING",
    KeeperErrorCode.SESSIONMOVED: "SESSIONMOVED",
    KeeperErrorCode.NOTREADONLY: "NOTREADONLY",
    KeeperErrorCode.EPHEMERALONLOCALSESSION: "EPHEMERALONLOCALSESSION",
    KeeperErrorCode.NOWATCHER: "NOWATCHER",
    KeeperErrorCode.REQUESTTIMEOUT: "REQUESTTIMEOUT",
    KeeperErrorCode.RECONFIGDISABLED: "RECONFIGDISABLED",
    KeeperErrorCode.SESSIONCLOSEDREQUIRESASLAUTH: "SESSIONCLOSEDREQUIRESASLAUTH",
    KeeperErrorCode.THROTTLEDOP: "THROTTLEDOP",
}


class KeeperError(MiniChError):
    """所有 Keeper 异常的基类 —— 携带一个 **ZooKeeper 错误码**。

    为什么让它继承 :class:`~minich.errors.MiniChError`?因为真实 ClickHouse
    里 Keeper 的错误最终会被包成 ``DB::Exception`` 抛给客户端(具体是
    ``ZooKeeperException : DB::Exception``)。所以继承关系是**忠实**的,
    只是错误码换成了 ZK 那一套负数。

    .. note::

       真实 ClickHouse 的 ``Coordination::Exception`` 其实继承自
       ``Poco::Exception``,``ZooKeeperException`` 才是 ``DB::Exception`` 的子类。
       mini-clickhouse 把它压成一层,是为了让"Keeper 错误也是数据库错误"
       这个事实在类型系统里一眼可见。
    """

    #: 该类对应的 ZK 错误码(子类覆盖)
    zk_code: int = KeeperErrorCode.SYSTEMERROR
    #: 该类对应的 ZK 错误名(子类覆盖)
    zk_name: str = "SYSTEMERROR"

    def __init__(self, message: str, path: Optional[str] = None, code: Optional[int] = None) -> None:
        c = self.zk_code if code is None else code
        super().__init__(message, code=c)
        self.zk_code = c
        self.name = ZK_CODE_TO_NAME.get(c, self.zk_name)
        self.zk_name = self.name
        self.path = path

    def __str__(self) -> str:  # pragma: no cover - 纯格式化
        where = f" (path: {self.path})" if self.path else ""
        return f"Code: {self.zk_code} ({self.zk_name}). KeeperError: {self.message}{where}"

    def __repr__(self) -> str:  # pragma: no cover
        return f"{type(self).__name__}({self.zk_code}, {self.zk_name!r}, {self.message!r})"


class NoNodeError(KeeperError):
    """节点不存在 —— ``NONODE`` (-101)。

    触发:``get`` / ``set`` / ``delete`` 一个不存在的路径,或 ``create`` 时
    父路径不存在。这是**最常见的 Keeper 错误**,运维看到它第一反应
    应该是"表/复制路径没建好"或"这个节点的会话过期被删了"。
    """

    zk_code = KeeperErrorCode.NONODE
    zk_name = "NONODE"


class NodeExistsError(KeeperError):
    """节点已存在 —— ``NODEEXISTS`` (-110)。顺序节点永不触发(名字必不同)。"""

    zk_code = KeeperErrorCode.NODEEXISTS
    zk_name = "NODEEXISTS"


class NotEmptyError(KeeperError):
    """节点还有子节点,不能删 —— ``NOTEMPTY`` (-111)。

    ZooKeeper **不做级联删除**。这一点是设计选择而非疏漏:级联删除在
    一棵可能很大的树上会是长事务,而 ZooKeeper 要保证每次操作都是
    常数时间的共识提交。想删一整棵子树,得**从叶子往上**逐个删
    (``ReplicatedMergeTree`` 删表时就是这么做的)。
    """

    zk_code = KeeperErrorCode.NOTEMPTY
    zk_name = "NOTEMPTY"


class BadVersionError(KeeperError):
    """版本号对不上 —— ``BADVERSION`` (-103)。乐观并发控制(CAS)的失败信号。

    用法:``set(path, data, version=v)`` 只有当节点当前 ``version == v``
    时才生效,否则抛这个。``version = -1`` 表示"不检查"。
    """

    zk_code = KeeperErrorCode.BADVERSION
    zk_name = "BADVERSION"


class NoAuthError(KeeperError):
    """没有权限 —— ``NOAUTH`` (-102)。

    ZooKeeper 的 ACL 是**每个 znode 一份**的,权限不足就抛这个。
    mini-clickhouse 不实现 ACL(所以永远不会真的抛),保留这个类是为了
    让"错误码表"完整 —— 读者对照官方文档时不会扑空。
    """

    zk_code = KeeperErrorCode.NOAUTH
    zk_name = "NOAUTH"


class AuthFailedError(KeeperError):
    """认证失败 —— ``AUTHFAILED`` (-115)。同 :class:`NoAuthError`,不实现。"""

    zk_code = KeeperErrorCode.AUTHFAILED
    zk_name = "AUTHFAILED"


class NoChildrenForEphemeralsError(KeeperError):
    """临时节点不能有子节点 —— ``NOCHILDRENFOREPHEMERALS`` (-108)。

    这条约束是有原因的:临时节点随会话消失,如果它还能带子节点,
    就会产生"挂在已消失节点下面的孤儿",树会出现无法访问的悬空分支。
    """

    zk_code = KeeperErrorCode.NOCHILDRENFOREPHEMERALS
    zk_name = "NOCHILDRENFOREPHEMERALS"


class SessionExpiredError(KeeperError):
    """会话已过期 —— ``SESSIONEXPIRED`` (-112)。客户端必须重连、重建临时节点。"""

    zk_code = KeeperErrorCode.SESSIONEXPIRED
    zk_name = "SESSIONEXPIRED"


class UnknownSessionError(KeeperError):
    """会话不存在 —— ``UNKNOWNSESSION`` (-12)。"""

    zk_code = KeeperErrorCode.UNKNOWNSESSION
    zk_name = "UNKNOWNSESSION"


class NothingError(KeeperError):
    """``NOTHING`` (-117)—— 一个特殊的"没有错误,但也没有结果"信号。

    Keeper 的 ``multi`` 事务里,如果某个 op 是"可选的"、失败也不算错,
    就用这个码表示"这条 op 什么也没做"。
    """

    zk_code = KeeperErrorCode.NOTHING
    zk_name = "NOTHING"


class ConnectionLossError(KeeperError):
    """连接丢失 —— ``CONNECTIONLOSS`` (-4)。**可重试**,与 ``NONODE`` 相对。"""

    zk_code = KeeperErrorCode.CONNECTIONLOSS
    zk_name = "CONNECTIONLOSS"


class BadArgumentsError(KeeperError):
    """参数非法 —— ``BADARGUMENTS`` (-8),例如路径不以 ``/`` 开头。"""

    zk_code = KeeperErrorCode.BADARGUMENTS
    zk_name = "BADARGUMENTS"


#: 错误码 → 异常类。这样"按码构造异常"就能得到正确的子类。
_ZK_ERROR_CLASSES: Dict[int, type] = {
    KeeperErrorCode.CONNECTIONLOSS: ConnectionLossError,
    KeeperErrorCode.BADARGUMENTS: BadArgumentsError,
    KeeperErrorCode.UNKNOWNSESSION: UnknownSessionError,
    KeeperErrorCode.NONODE: NoNodeError,
    KeeperErrorCode.NOAUTH: NoAuthError,
    KeeperErrorCode.BADVERSION: BadVersionError,
    KeeperErrorCode.NOCHILDRENFOREPHEMERALS: NoChildrenForEphemeralsError,
    KeeperErrorCode.NODEEXISTS: NodeExistsError,
    KeeperErrorCode.NOTEMPTY: NotEmptyError,
    KeeperErrorCode.SESSIONEXPIRED: SessionExpiredError,
    KeeperErrorCode.AUTHFAILED: AuthFailedError,
    KeeperErrorCode.NOTHING: NothingError,
}


def raise_keeper_error(code: int, message: str, path: Optional[str] = None) -> None:
    """按 ZooKeeper 错误码抛出对应的 :class:`KeeperError` 子类。

    与 :func:`minich.errors.raise_error` 同一个套路(函数而不是 ``raise``
    语句,方便塞进表达式),只是这里会自动挑子类。
    """
    cls = _ZK_ERROR_CLASSES.get(code, KeeperError)
    err = cls(message, path=path, code=code)
    raise err


# ══════════════════════════════════════════════════════════════════════════
# 数据模型:ZNodeStat / ZNode / Session
# ══════════════════════════════════════════════════════════════════════════


def _now_ms() -> int:
    """墙上时钟毫秒。ZooKeeper 的 ``ctime``/``mtime`` 就是它(不是单调时钟)。

    注意:这里**故意**用墙上时钟,因为 stat 的时间要能和人看的日志对上。
    但会话超时的判定用的是同一套毫秒值 —— 真实 ZooKeeper 也用墙上时钟
   (这也是它偶尔会因时钟回拨而误判会话的原因)。
    """
    return int(time.time() * 1000)


class ZNodeStat:
    """znode 的元数据 —— 字段名**逐字**照抄 ZooKeeper 的 ``Stat``。

    .. code-block:: text

       czxid           创建这个节点的事务 id(zxid)
       mzxid           最后一次**修改数据**的事务 id
       pzxid           最后一次**修改子节点列表**的事务 id
       ctime           创建时间(毫秒)
       mtime           最后修改数据的时间(毫秒)
       version         数据版本号 —— set 一次 +1(用做 CAS)
       cversion        子节点版本号 —— 子节点增删一次 +1
       aversion        ACL 版本号(mini-clickhouse 不实现 ACL,恒为 0)
       ephemeralOwner  临时节点的属主会话 id;普通节点为 0
       dataLength      数据长度
       numChildren     子节点个数

    为什么要有 ``version`` 和 ``cversion`` **两个**版本号?因为它们变化
    的时机不同:改数据只动 version,增删子节点只动 cversion。分开之后,
    "我只想防止别人同时改数据、不在乎别人加了子节点"这种需求
    (ReplicatedMergeTree 里到处都是)才能表达。
    """

    __slots__ = (
        "czxid", "mzxid", "pzxid", "ctime", "mtime",
        "version", "cversion", "aversion",
        "ephemeralOwner", "dataLength", "numChildren",
    )

    def __init__(
        self,
        czxid: int = 0,
        mzxid: int = 0,
        pzxid: int = 0,
        ctime: int = 0,
        mtime: int = 0,
        version: int = 0,
        cversion: int = 0,
        aversion: int = 0,
        ephemeralOwner: int = 0,
        dataLength: int = 0,
        numChildren: int = 0,
    ) -> None:
        self.czxid = czxid
        self.mzxid = mzxid
        self.pzxid = pzxid
        self.ctime = ctime
        self.mtime = mtime
        self.version = version
        self.cversion = cversion
        self.aversion = aversion
        self.ephemeralOwner = ephemeralOwner
        self.dataLength = dataLength
        self.numChildren = numChildren

    def copy(self) -> "ZNodeStat":
        s = ZNodeStat()
        for f in self.__slots__:
            setattr(s, f, getattr(self, f))
        return s

    def to_dict(self) -> Dict[str, int]:
        return {f: getattr(self, f) for f in self.__slots__}

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, ZNodeStat):
            return NotImplemented
        return all(getattr(self, f) == getattr(other, f) for f in self.__slots__)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"ZNodeStat(czxid={self.czxid}, mzxid={self.mzxid}, pzxid={self.pzxid}, "
            f"version={self.version}, cversion={self.cversion}, "
            f"ephemeralOwner={self.ephemeralOwner}, numChildren={self.numChildren})"
        )


class ZNode:
    """一个 znode —— 路径 + 数据 + stat + 子节点。

    ``children`` 是 ``{名字: ZNode}``(不含路径前缀),所以从任意节点
    一步就能走到子节点。**注意路径一律用规范化后的绝对路径**
    (见 :func:`_normalize_path`):不能有 ``//``、不能有 ``.``/``..``、
    除根以外不能有尾斜杠。ZooKeeper 对路径的规矩很严,我们照抄。
    """

    __slots__ = ("path", "data", "stat", "children")

    def __init__(
        self,
        path: str,
        data: bytes = b"",
        stat: Optional[ZNodeStat] = None,
        children: Optional[Dict[str, "ZNode"]] = None,
    ) -> None:
        self.path = path
        self.data = data
        self.stat = stat if stat is not None else ZNodeStat()
        self.children: Dict[str, ZNode] = children if children is not None else {}

    @property
    def name(self) -> str:
        """自身名字(路径最后一段);根节点为 ``""``。"""
        return "" if self.path == "/" else self.path.rsplit("/", 1)[1]

    def is_ephemeral(self) -> bool:
        return self.stat.ephemeralOwner != 0

    def copy(self) -> "ZNode":
        """返回一份**快照**(stat 与 data 都是拷贝,children 是浅引用)。

        所有读操作返回的都是快照 —— 因为在真实 ZooKeeper 里,客户端
        **本来**就只能拿到序列化过去的副本,拿不到服务端的活对象。
        mini-clickhouse 这样实现既更接近真实,也顺手防住了"测试里
        拿到活节点、结果断言时数据已经被改掉"的坑。
        """
        return ZNode(self.path, bytes(self.data), self.stat.copy(), dict(self.children))

    def __repr__(self) -> str:  # pragma: no cover
        eph = f" ephemeral={self.stat.ephemeralOwner}" if self.is_ephemeral() else ""
        return f"<ZNode {self.path!r} len={len(self.data)} version={self.stat.version}{eph}>"


class Session:
    """一个 Keeper 会话 —— **临时节点的生命周期就是会话的生命周期**。

    .. code-block:: text

       new_session()              建立会话(session_id, timeout_ms, last_touched)
            │
            ├── touch()           心跳:刷新 last_touched
            │
            ├── 客户端持续 touch   → 永不超时
            │
            └── 停止 touch
                 │  (now - last_touched > timeout_ms)
                 ▼
               expire_sessions()  回收:把会话建的**所有临时节点删掉**
                 │
                 ▼
               close_session(expired=True)

    这就是"副本进程挂掉 → 它的 ``/replicas/<r>`` 和 ``leader_election``
    节点消失 → 别的副本立刻看得到"的全部机制。**没有轮询、没有租约
    协商、没有谁去主动清理** —— 全靠"会话断 = 临时节点消失"这一条。

    .. note::

       真实 ZooKeeper 的 ``session_id`` 长得很有讲究(高 8 位是超时相关的
       时间戳、后面是机器 id 和自增计数器),这样 id 本身就能用于
       会话迁移。mini-clickhouse 直接随机一个 63 位数。
    """

    __slots__ = ("session_id", "timeout_ms", "created_at", "last_touched", "expired", "closed", "ephemerals")

    def __init__(self, session_id: int, timeout_ms: int, created_at: Optional[int] = None) -> None:
        self.session_id = session_id
        self.timeout_ms = int(timeout_ms)
        now = _now_ms() if created_at is None else created_at
        self.created_at = now
        self.last_touched = now
        self.expired = False
        self.closed = False
        #: 这个会话建的所有临时节点路径 —— 会话一断就照着这张表删。
        self.ephemerals: Set[str] = set()

    @property
    def timeout(self) -> int:
        """``timeout_ms`` 的别名(真实 Keeper 的字段就叫 ``timeout``)。"""
        return self.timeout_ms

    def is_alive(self, now_ms: Optional[int] = None) -> bool:
        """是否还活着 —— 未关闭、未过期、且距离上次心跳没超过 ``timeout_ms``。"""
        if self.closed or self.expired:
            return False
        now = _now_ms() if now_ms is None else now_ms
        return (now - self.last_touched) <= self.timeout_ms

    def touch(self, now_ms: Optional[int] = None) -> None:
        self.last_touched = _now_ms() if now_ms is None else now_ms

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Session 0x{self.session_id:016x} timeout={self.timeout_ms}ms eph={len(self.ephemerals)}>"


# ══════════════════════════════════════════════════════════════════════════
# 创建模式与 watch 事件
# ══════════════════════════════════════════════════════════════════════════


class CreateMode:
    """znode 的创建模式 —— 编号与 ClickHouse 的 ``Coordination::CreateMode`` 一致。

    .. code-block:: text

       PERSISTENT              = 0   普通持久节点
       EPHEMERAL               = 1   临时节点(随会话消失,不能有子节点)
       PERSISTENT_SEQUENTIAL   = 2   持久 + 名字末尾追加 10 位自增编号
       EPHEMERAL_SEQUENTIAL    = 3   临时 + 顺序

    .. warning::

       **这里的编号顺序和 Java 版 ZooKeeper 不一样!** Java 的
       ``CreateMode`` 枚举顺序是 PERSISTENT / PERSISTENT_SEQUENTIAL /
       EPHEMERAL / EPHEMERAL_SEQUENTIAL(0/1/2/3),ClickHouse 的 C++
       版本按"是否临时、是否顺序"重新排成 0/1/2/3。两者在协议上
       都会用"是否 ephemeral、是否 sequential"两个布尔传输,所以
       不影响互通 —— 但如果有人拿 Java 的 ordinal 来对本模块的编号,
       就会错。mini-clickhouse 跟 ClickHouse。
    """

    PERSISTENT = 0
    EPHEMERAL = 1
    PERSISTENT_SEQUENTIAL = 2
    EPHEMERAL_SEQUENTIAL = 3


PERSISTENT = CreateMode.PERSISTENT
EPHEMERAL = CreateMode.EPHEMERAL
PERSISTENT_SEQUENTIAL = CreateMode.PERSISTENT_SEQUENTIAL
EPHEMERAL_SEQUENTIAL = CreateMode.EPHEMERAL_SEQUENTIAL


class WatchEvent:
    """watch 事件的类型 —— 四个是**互斥的枚举值,不是位掩码**。

    .. code-block:: text

       ZKNodeCreated          = 1   节点被创建
       ZKNodeDeleted          = 2   节点被删除
       ZKNodeDataChanged      = 3   节点数据变了
       ZKNodeChildrenChanged  = 4   节点的**子节点列表**变了

    .. warning::

       注意 1、3 不是位掩码关系吗?**不是!** ``1 | 2 == 3`` 在数值上
       成立纯属巧合,而 ``3`` 恰好也是 ``ZKNodeDataChanged``。所以
       **绝不能**把它们按位或起来当掩码用 —— 这是真实 ZooKeeper API
       的核心事件类型,历史上是 Java enum 的 ordinal,不是为了
       按位运算设计的。mini-clickhouse 因此把 ``event_mask`` 参数
       定义成"一个事件类型"或"一组事件类型(集合)",而不是位掩码。

    还有一个关键点:**一次性地**。watch 触发一次就自动注销,想继续
    监听就得再注册一次。这是 ZooKeeper 最出名的坑,细节见
    :meth:`KeeperStorage.add_watch`。
    """

    ZKNodeCreated = 1
    ZKNodeDeleted = 2
    ZKNodeDataChanged = 3
    ZKNodeChildrenChanged = 4

    NAMES: Dict[int, str] = {
        1: "NodeCreated",
        2: "NodeDeleted",
        3: "NodeDataChanged",
        4: "NodeChildrenChanged",
    }

    @classmethod
    def name_of(cls, event_type: int) -> str:
        return cls.NAMES.get(event_type, f"Event{event_type}")


#: 模块级快捷常量(真实代码里就是这么用的)。
ZKNodeCreated = WatchEvent.ZKNodeCreated
ZKNodeDeleted = WatchEvent.ZKNodeDeleted
ZKNodeDataChanged = WatchEvent.ZKNodeDataChanged
ZKNodeChildrenChanged = WatchEvent.ZKNodeChildrenChanged

#: "所有事件" —— 传给 ``add_watch`` 的 ``event_mask``。
ZK_ALL_EVENTS = frozenset(
    (ZKNodeCreated, ZKNodeDeleted, ZKNodeDataChanged, ZKNodeChildrenChanged)
)


# ══════════════════════════════════════════════════════════════════════════
# 路径工具
# ══════════════════════════════════════════════════════════════════════════


def _normalize_path(path: str) -> str:
    """把路径规范化成 ZooKeeper 认可的绝对路径。

    ``"/a//b/"`` → ``"/a/b"``;``"/"`` → ``"/"``。
    不以 ``/`` 开头、或含 ``.``/``..`` 的路径一律 ``BADARGUMENTS``。
    """
    if not isinstance(path, str) or not path.startswith("/"):
        raise_keeper_error(
            KeeperErrorCode.BADARGUMENTS,
            f"Path must start with '/': {path!r}",
            path=path if isinstance(path, str) else None,
        )
    if path == "/":
        return "/"
    parts = [p for p in path.split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise_keeper_error(
            KeeperErrorCode.BADARGUMENTS,
            f"Path may not contain '.' or '..': {path!r}",
            path=path,
        )
    return "/" + "/".join(parts)


def _parent_path(path: str) -> Optional[str]:
    """父路径;根节点没有父,返回 ``None``。"""
    if path == "/":
        return None
    if "/" not in path[1:]:
        return "/"
    return path.rsplit("/", 1)[0]


def _node_name(path: str) -> str:
    """路径最后一段。"""
    if path == "/":
        return ""
    return path.rsplit("/", 1)[1]


# ══════════════════════════════════════════════════════════════════════════
# KeeperStorage —— 单节点的 znode 树 + 会话 + watch
# ══════════════════════════════════════════════════════════════════════════

#: 默认为会话超时 —— 与 ZooKeeper / ClickHouse 社区常用默认值一致(30 秒)。
DEFAULT_SESSION_TIMEOUT_MS = 30_000


class _Watch:
    """一条注册在某个路径上的 watch。"""

    __slots__ = ("types", "callback", "persistent", "path")

    def __init__(
        self,
        path: str,
        types: Optional[frozenset],
        callback: Optional[Callable[[int, str], None]],
        persistent: bool = False,
    ) -> None:
        self.path = path
        #: ``None`` 表示"所有事件类型"
        self.types = types
        self.callback = callback
        #: True = 持久 watch(触发后不注销),对应 Keeper 的 AddWatchMode::PERSISTENT
        self.persistent = persistent

    def matches(self, event_type: int) -> bool:
        return self.types is None or event_type in self.types


class KeeperStorage:
    """Keeper 的**数据面**:一棵 znode 树 + 一组会话 + 一堆 watch。

    这个类不关心网络、不关心线程池、不关心共识(那都是
    :class:`KeeperServer` 或真实 Keeper 里 Raft 的事),它只保证
    "在拿到锁的前提下,树与会话的语义是对的"。

    **线程安全**:所有公开方法都持有一把 :class:`threading.RLock`。
    用 RLock 而非 Lock 的原因很实在 —— ``create`` 会调 ``_fire_watches``,
    而 watch 回调里很可能又调一次 Keeper 读接口;如果是普通 Lock,
    同一个线程就会自己把自己锁死。这在真实 Keeper 里对应
    "事件是在**提交之后**异步派发的"这一设计。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._nodes: Dict[str, ZNode] = {}
        self._zxid = 0
        self._sessions: Dict[int, Session] = {}
        #: session_id → 它建的临时节点路径集合(与 Session.ephemerals 同步)
        self._ephemerals: Dict[int, Set[str]] = {}
        #: 路径 → 该路径上的 watch 列表
        self._watches: Dict[str, List[_Watch]] = {}
        #: 每个父节点下、顺序节点用的只增不减计数器
        self._sequence_counters: Dict[str, int] = {}
        #: 事务(``multi``)嵌套深度与"待派发事件"
        #: 事务嵌套深度 —— 0 表示不在事务里(事件立即派发)。
        self._multi_depth = 0
        #: **每一层事务一个事件队列**(栈)。
        #:
        #: 为什么必须是"栈"而不是一个扁平的列表?因为事务可以嵌套。
        #: 内层事务回滚时,**只应该丢掉内层自己排队的那些事件**;
        #: 外层在此之前已经排好的事件必须原样留着。用一个扁平列表时,
        #: 内层回滚没法只丢自己那一段,结果是"内层被回滚掉的改动,
        #: 外层提交时照样把 watch 派发了出去" —— 客户端会收到一个
        #: 指向**从未存在过的节点**的事件。
        self._deferred_stack: List[List[Tuple[str, int]]] = []
        self.log: Logger = get_logger("Keeper")

        now = _now_ms()
        self._nodes["/"] = ZNode("/", b"", ZNodeStat(czxid=0, mzxid=0, pzxid=0, ctime=now, mtime=now))

    # ── 基础设施 ────────────────────────────────────────────────────────

    @property
    def lock(self) -> threading.RLock:
        """暴露底层锁,供 :meth:`KeeperServer.multi` 做"跨多步的原子性"。"""
        return self._lock

    def _bump_zxid(self) -> int:
        """推进全局事务 id。每一次**写**操作都消耗一个 zxid,单调递增。"""
        self._zxid += 1
        return self._zxid

    def _next_sequence(self, parent: str) -> int:
        """取父节点下顺序节点的下一个编号(从 1 开始)。

        .. warning::

           真实 ZooKeeper 用的是**父节点的 ``cversion``** 当编号,因此
           **删除**旧子节点也会让后面的编号出现空洞。mini-clickhouse
           改用只增不减的专属计数器,好处是 ``/log`` 的编号**严格连续**,
           于是 ``log_pointer`` 可以直接当"处理到第几条"的下标用 ——
           这正是教程要演示的东西。这是**简化**,代价是我们无法演示
           "删除日志头部导致编号出现空洞"这个真实现象。
        """
        nxt = self._sequence_counters.get(parent, 0) + 1
        self._sequence_counters[parent] = nxt
        return nxt

    @staticmethod
    def _as_bytes(data: Union[bytes, bytearray, str, None]) -> bytes:
        if data is None:
            return b""
        if isinstance(data, str):
            return data.encode("utf-8")
        return bytes(data)

    # ── watch 派发 ─────────────────────────────────────────────────────

    def _fire_watches(self, path: str, event_type: int) -> None:
        """在 ``path`` 上派发一个事件。事务未提交时先入队,提交后再派发。"""
        if self._multi_depth > 0:
            self._deferred_stack[-1].append((path, event_type))
            return
        self._dispatch_events([(path, event_type)])

    def _dispatch_events(self, events: Sequence[Tuple[str, int]]) -> None:
        """真正调用回调。**一次性 watch 在这里被消费掉。**"""
        for path, event_type in events:
            watches = self._watches.get(path)
            if not watches:
                continue
            remaining: List[_Watch] = []
            fired: List[_Watch] = []
            for w in watches:
                if w.matches(event_type):
                    fired.append(w)
                    if w.persistent:
                        remaining.append(w)
                else:
                    remaining.append(w)
            if remaining:
                self._watches[path] = remaining
            else:
                self._watches.pop(path, None)
            for w in fired:
                if w.callback is None:
                    continue
                try:
                    w.callback(event_type, path)
                except Exception as exc:  # pragma: no cover - 回调里的异常不该影响 Keeper
                    self.log.warning(
                        f"Keeper watch callback failed on {path} "
                        f"({WatchEvent.name_of(event_type)}): {exc}"
                    )

    def add_watch(
        self,
        path: str,
        event_mask: Union[int, Iterable[int], None] = None,
        callback: Optional[Callable[[int, str], None]] = None,
        persistent: bool = False,
    ) -> _Watch:
        """在 ``path`` 上注册一个 watch,返回内部 watch 对象。

        **为什么 watch 是一次性的(one-shot)?**

        .. code-block:: text

           客户端: getdata("/log", watch=True)     <-- 注册
           服务端: ... /log 变了 → 发事件、然后**注销**这个 watch ...
           客户端: 收到事件 → 必须**重新** getdata(..., watch=True)
                   如果没有重新注册 → 之后的变化**再也收不到**

        设计动机(Joel Spolsky 时代留下的 ZooKeeper 原话):一次性 watch
        让服务端**不需要为每个客户端维护长期状态** —— 事件发出即遗忘,
        内存开销与"当前挂着的 watch 数"成正比,而不是与"客户端兴趣"
        成正比。这在"一个 Keeper 集群扛几千个客户端"的规模下很关键。

        代价就是那个著名的坑:**忘记重新注册 = 静默漏事件**。
        ClickHouse 的 Keeper 因此在 Zodiac 协议里加了持久 watch
        (``AddWatchMode::PERSISTENT`` / ``PERSISTENT_RECURSIVE``),
        我们用一个 ``persistent=True`` 参数来模拟。

        参数
        ----
        event_mask
            * ``None`` 或 :data:`ZK_ALL_EVENTS` —— 监听所有事件类型;
            * 一个 ``int`` —— 只监听这一种(``ZKNodeCreated`` 等);
            * 一个可迭代对象 —— 监听这几种的**集合**。
        callback
            ``callback(event_type, path)``。在**持有存储锁**的情况下被调用
            (RLock 可重入,回调里再读 Keeper 不会死锁),所以**不要在回调里
            干重活或长时间阻塞** —— 真实 Keeper 是异步派发事件的。
        persistent
            ``True`` 时触发后不注销(模拟 Keeper 的持久 watch)。
        """
        p = _normalize_path(path)
        if event_mask is None:
            types: Optional[frozenset] = None
        elif isinstance(event_mask, int):
            types = frozenset((event_mask,))
        else:
            types = frozenset(int(t) for t in event_mask)
        watch = _Watch(p, types, callback, persistent=persistent)
        with self._lock:
            self._watches.setdefault(p, []).append(watch)
        return watch

    def remove_watch(self, watch: _Watch) -> bool:
        """手动注销一个 watch(真实客户端在断开时也会这么做)。"""
        with self._lock:
            lst = self._watches.get(watch.path)
            if not lst or watch not in lst:
                return False
            lst.remove(watch)
            if not lst:
                self._watches.pop(watch.path, None)
            return True

    def watch_count(self, path: Optional[str] = None) -> int:
        """挂着的 watch 数(测试用)。"""
        with self._lock:
            if path is None:
                return sum(len(v) for v in self._watches.values())
            return len(self._watches.get(_normalize_path(path), []))

    # ── 事务(multi 的底座)─────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        """整棵树的深拷贝 —— 供 ``multi`` 回滚。

        .. warning::

           真实 Keeper 的事务是**在 Raft 日志里回放**的:一次 ``multi``
           要么整体提交、要么整体不提交,不需要"快照回滚"。
           mini-clickhouse 用快照回滚来模拟同样的语义 —— 结果一样,
           机制完全不同,而且这里**只有单个实例**,不存在"回滚要同步
           到别的节点"的问题。
        """
        with self._lock:
            return {
                "nodes": copy.deepcopy(self._nodes),
                "zxid": self._zxid,
                "ephemerals": {k: set(v) for k, v in self._ephemerals.items()},
                "seq": dict(self._sequence_counters),
            }

    def restore(self, snap: Dict[str, Any]) -> None:
        with self._lock:
            self._nodes = copy.deepcopy(snap["nodes"])
            self._zxid = snap["zxid"]
            self._ephemerals = {k: set(v) for k, v in snap["ephemerals"].items()}
            self._sequence_counters = dict(snap["seq"])

    def begin_transaction(self) -> Dict[str, Any]:
        with self._lock:
            self._multi_depth += 1
            self._deferred_stack.append([])
            return self.snapshot()

    def commit_transaction(self) -> None:
        """结束一层事务并**提交**它:它的事件要么向上合并,要么被派发。"""
        events: List[Tuple[str, int]] = []
        with self._lock:
            if self._deferred_stack:
                events = self._deferred_stack.pop()
            self._multi_depth = max(0, self._multi_depth - 1)
            if self._multi_depth > 0 and self._deferred_stack:
                # 还有外层事务:这些事件**确实发生了**,交给外层一起派发
                # (派发时机仍然是"最外层提交之后")。
                self._deferred_stack[-1].extend(events)
                events = []
        # 事件在**提交之后**才派发 —— 与真实 ZooKeeper/Keeper 一致:
        # 客户端看到的是"事务要么整件事发生了"。
        if events:
            self._dispatch_events(events)

    def abort_transaction(self, snap: Dict[str, Any]) -> None:
        """结束一层事务并**回滚**它:**只丢弃本层自己排队的事件**。"""
        with self._lock:
            if self._deferred_stack:
                self._deferred_stack.pop()  # 本层的事件整批丢掉
            self.restore(snap)
            self._multi_depth = max(0, self._multi_depth - 1)
            if self._multi_depth == 0:
                # 防御性:正常路径下栈此时已经空了
                self._deferred_stack = []

    @contextlib.contextmanager
    def transaction(self):
        """``with storage.transaction():`` —— **持有锁**跑完一整组操作。

        这是 :meth:`KeeperServer.multi` 与副本插入路径真正用的东西。
        单独调 ``begin_transaction`` / ``commit_transaction`` 的话,
        两步之间锁是放开的 —— 在并发环境里就不原子了(另一个线程
        可能插进来看见"写了一半"的树)。所以正确的用法必须是
        "整个事务期间不放锁",这里用 ``with self._lock`` 保证它。

        RLock 可重入,所以事务里的每个 ``create``/``set`` 再各自加锁
        也没问题 —— 它们会重入同一把锁,不会自锁。
        """
        with self._lock:
            snapshot = self.begin_transaction()
            try:
                yield snapshot
            except BaseException:
                self.abort_transaction(snapshot)
                raise
            else:
                self.commit_transaction()

    # ── 会话 ────────────────────────────────────────────────────────────

    def new_session(self, timeout_ms: int = DEFAULT_SESSION_TIMEOUT_MS, session_id: Optional[int] = None) -> Session:
        """建立一个会话。``timeout_ms`` 是**客户端提议**的超时;真实 Keeper
        会把它夹在 ``min_session_timeout_ms`` 与 ``max_session_timeout_ms``
        之间(默认 10s / 100s),mini-clickhouse 不夹,方便测试用极小值。"""
        with self._lock:
            if session_id is None:
                session_id = random.getrandbits(63) or 1
            session = Session(session_id, timeout_ms)
            self._sessions[session_id] = session
            self._ephemerals.setdefault(session_id, set())
            return session

    def get_session(self, session_id: int) -> Optional[Session]:
        with self._lock:
            return self._sessions.get(session_id)

    def sessions(self) -> List[Session]:
        with self._lock:
            return list(self._sessions.values())

    def touch(self, session_id: int, now_ms: Optional[int] = None) -> None:
        """心跳。**这是阻止会话过期的唯一手段** —— 客户端每隔
        ``timeout/3`` 左右发一次。真实 Keeper 客户端库自动做这件事。

        会话不存在(已过期/已关闭)时抛 :class:`SessionExpiredError`,
        客户端据此重连并**重建所有临时节点与 watch**。
        """
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None or s.closed or s.expired:
                raise_keeper_error(
                    KeeperErrorCode.SESSIONEXPIRED,
                    f"Session 0x{session_id:016x} does not exist or has expired",
                )
            s.touch(now_ms)

    def close_session(self, session_id: int, expired: bool = False, now_ms: Optional[int] = None) -> bool:
        """关闭会话,并**删掉它建的所有临时节点**。

        ``expired=True`` 表示是"超时被动回收",``False`` 表示"客户端主动关闭"。
        两者对临时节点的效果相同(都删),区别只在语义/日志。

        删除时**从深到浅排序**(``reverse=True``):因为 ZooKeeper 不允许
        删有子节点的节点,而临时节点又不能有子节点 —— 这里其实不存在
        嵌套,但真实场景里会话可能建了多层节点,排序是防御性的。
        """
        with self._lock:
            s = self._sessions.pop(session_id, None)
            if s is None:
                return False
            s.closed = True
            s.expired = expired
            for path in sorted(self._ephemerals.pop(session_id, set()), reverse=True):
                node = self._nodes.get(path)
                if node is not None and node.is_ephemeral() and node.stat.ephemeralOwner == session_id:
                    self._delete_node(path, node, watch_events=True)
            s.ephemerals.clear()
        self.log.information(
            f"Keeper session 0x{session_id:016x} "
            f"{'expired' if expired else 'closed'}"
        )
        return True

    def expire_sessions(self, now_ms: Optional[int] = None) -> List[int]:
        """回收所有已超时的会话,返回被回收的 session_id 列表。

        后台 reaper(:class:`KeeperServer` 的线程)周期性调它;
        测试也可以**手动**调它来实现确定性的"让会话立刻过期"。
        """
        now = _now_ms() if now_ms is None else now_ms
        with self._lock:
            dead = [
                sid for sid, s in self._sessions.items()
                if not s.closed and not s.expired and (now - s.last_touched) > s.timeout_ms
            ]
        for sid in dead:
            self.close_session(sid, expired=True, now_ms=now)
        return dead

    def force_expire_session(self, session_id: int) -> bool:
        """不管超时没超时,立刻让一个会话过期 —— 测试"副本猝死"用。"""
        return self.close_session(session_id, expired=True)

    # ── 读操作 ──────────────────────────────────────────────────────────

    def exists(self, path: str, watch: bool = False,
               callback: Optional[Callable[[int, str], None]] = None) -> Optional[ZNodeStat]:
        """节点存在则返回 **stat 快照**,否则返回 ``None``。

        这是唯一一个"找不到不报错、返回 None"的读接口 —— 因为
        ``exists`` 的语义本来就是"存在吗",而不是"读它"。真实
        ZooKeeper 的 ``exists`` 就是这个行为。
        """
        p = _normalize_path(path)
        with self._lock:
            if watch:
                self.add_watch(p, None, callback)
            node = self._nodes.get(p)
            return None if node is None else node.stat.copy()

    def get(self, path: str, watch: bool = False,
            callback: Optional[Callable[[int, str], None]] = None) -> ZNode:
        """取节点快照。**节点不存在时抛 :class:`NoNodeError`** —— 而不是
        返回 ``None``。

        为什么 ``get`` 和 ``exists`` 对"不存在"的处理相反?因为它们的
        用途不同:``get`` 的调用方**预期**节点存在,返回 None 会把
        "不存在"这个错误静默地带到很远的地方(然后在某处变成
        ``AttributeError``);``exists`` 的调用方本来就在问"在不在"。
        这条"读不存在的数据要报错、不要返回 None"的原则,在
        :mod:`minich.storages` 里也同样适用。
        """
        p = _normalize_path(path)
        with self._lock:
            if watch:
                self.add_watch(p, None, callback)
            node = self._nodes.get(p)
            if node is None:
                raise_keeper_error(KeeperErrorCode.NONODE, "node does not exist", path=p)
            return node.copy()

    def get_data(self, path: str, watch: bool = False,
                 callback: Optional[Callable[[int, str], None]] = None) -> bytes:
        """只要数据,不要 stat(便利方法)。"""
        return self.get(path, watch=watch, callback=callback).data

    def stat(self, path: str) -> ZNodeStat:
        """只要 stat。节点不存在时抛 :class:`NoNodeError`。"""
        return self.get(path).stat

    def get_children(self, path: str, watch: bool = False,
                     callback: Optional[Callable[[int, str], None]] = None) -> List[str]:
        """返回子节点**名字**列表(已排序,保证确定性)。

        .. note::

           真实 ZooKeeper 不保证子节点顺序。mini-clickhouse **排序**,
           因为教程与测试都依赖"同一状态给出同一输出"的确定性。
           依赖顺序的调用方(比如"取编号最小的 leader")本就应该自己
           排序,真实 CH 也是这么写的。
        """
        p = _normalize_path(path)
        with self._lock:
            node = self._nodes.get(p)
            if node is None:
                raise_keeper_error(KeeperErrorCode.NONODE, "node does not exist", path=p)
            if watch:
                self.add_watch(p, None, callback)
            return sorted(node.children.keys())

    def count_children(self, path: str) -> int:
        p = _normalize_path(path)
        with self._lock:
            node = self._nodes.get(p)
            if node is None:
                raise_keeper_error(KeeperErrorCode.NONODE, "node does not exist", path=p)
            return len(node.children)

    def list_tree(self, root: str = "/") -> Dict[str, Any]:
        """把子树导出成嵌套 dict —— 调试用(``dump()`` 的原料)。

        .. code-block:: text

           {"path": "/", "data": b"", "stat": {...},
            "children": {"clickhouse": {...}, ...}}
        """
        p = _normalize_path(root)
        with self._lock:
            node = self._nodes.get(p)
            if node is None:
                raise_keeper_error(KeeperErrorCode.NONODE, "node does not exist", path=p)
            return self._tree_dict(node)

    def _tree_dict(self, node: ZNode) -> Dict[str, Any]:
        return {
            "path": node.path,
            "data": bytes(node.data),
            "stat": node.stat.to_dict(),
            "children": {name: self._tree_dict(child) for name, child in sorted(node.children.items())},
        }

    def node_count(self) -> int:
        with self._lock:
            return len(self._nodes)

    # ── 写操作 ──────────────────────────────────────────────────────────

    def create(
        self,
        path: str,
        data: Union[bytes, bytearray, str] = b"",
        ephemeral: bool = False,
        sequential: bool = False,
        mode: Optional[int] = None,
        session_id: Optional[int] = None,
        ignore_existing: bool = False,
    ) -> str:
        """创建一个 znode,返回**真实路径**(顺序节点会带自增后缀)。

        .. code-block:: text

           create("/log/log-", b"...", sequential=True)
                │
                ├── 规范化父路径 /log(必须已存在,否则 NONODE)
                ├── 取下一个编号 1
                ├── 真实路径 = "/log/log-0000000001"
                ├── 已存在? → NODEEXISTS(unless ignore_existing)
                └── 建好 + 触发 NodeCreated(自己)/ ChildrenChanged(父)

        参数里 ``ephemeral`` 与 ``sequential`` 会与 ``mode`` 合并
        (``mode`` 是更"原味"的写法;两个布尔是给调用方省事的糖)。

        ``ignore_existing=True`` 对应 Zodiac 的
        ``create(..., ignore_if_exists=True)`` —— ClickHouse 建表的
        一长串"确保目录存在"操作全靠它,否则并发建表必然 NODEEXISTS。
        注意顺序节点**永远不会** NODEEXISTS(名字必然唯一)。
        """
        p = _normalize_path(path)
        if mode is not None:
            ephemeral = ephemeral or mode in (CreateMode.EPHEMERAL, CreateMode.EPHEMERAL_SEQUENTIAL)
            sequential = sequential or mode in (
                CreateMode.PERSISTENT_SEQUENTIAL, CreateMode.EPHEMERAL_SEQUENTIAL
            )

        with self._lock:
            parent = _parent_path(p)
            parent_node = self._nodes.get(parent) if parent is not None else None
            if parent is None or parent_node is None:
                raise_keeper_error(
                    KeeperErrorCode.NONODE,
                    "parent node does not exist",
                    path=parent,
                )
            if parent_node.is_ephemeral():
                # 临时节点下不能建子节点
                raise_keeper_error(
                    KeeperErrorCode.NOCHILDRENFOREPHEMERALS,
                    "cannot create a child of an ephemeral node",
                    path=parent,
                )

            if sequential:
                seq = self._next_sequence(parent)
                if parent == "/":
                    p = f"/{_node_name(p)}{seq:010d}"
                else:
                    p = f"{parent}/{_node_name(p)}{seq:010d}"

            if p in self._nodes:
                if ignore_existing:
                    return p
                raise_keeper_error(KeeperErrorCode.NODEEXISTS, "node already exists", path=p)

            if ephemeral:
                if session_id is None:
                    raise_keeper_error(
                        KeeperErrorCode.BADARGUMENTS,
                        "ephemeral nodes require a session_id",
                        path=p,
                    )
                if session_id not in self._sessions:
                    raise_keeper_error(
                        KeeperErrorCode.UNKNOWNSESSION,
                        f"session 0x{session_id:016x} does not exist",
                        path=p,
                    )

            payload = self._as_bytes(data)
            zxid = self._bump_zxid()
            now = _now_ms()
            stat = ZNodeStat(
                czxid=zxid,
                mzxid=zxid,
                pzxid=zxid,
                ctime=now,
                mtime=now,
                ephemeralOwner=(session_id if ephemeral and session_id is not None else 0),
                dataLength=len(payload),
                numChildren=0,
            )
            node = ZNode(p, payload, stat)
            self._nodes[p] = node
            parent_node.children[_node_name(p)] = node
            parent_node.stat.cversion += 1
            parent_node.stat.pzxid = zxid
            parent_node.stat.numChildren = len(parent_node.children)

            if ephemeral and session_id is not None:
                self._ephemerals.setdefault(session_id, set()).add(p)
                sess = self._sessions.get(session_id)
                if sess is not None:
                    sess.ephemerals.add(p)

            self._fire_watches(p, ZKNodeCreated)
            self._fire_watches(parent, ZKNodeChildrenChanged)
            return p

    def set(
        self,
        path: str,
        data: Union[bytes, bytearray, str],
        version: int = -1,
    ) -> ZNodeStat:
        """改数据。``version=-1`` 表示"不检查版本";否则做 CAS:版本不符抛
        :class:`BadVersionError`。**成功则 ``version`` 加 1。**

        .. code-block:: text

           set("/a", b"x", version=3)   →  节点 version 必须 == 3
                                            成功:version 变成 4
                                            失败:BadVersionError
        """
        p = _normalize_path(path)
        payload = self._as_bytes(data)
        with self._lock:
            node = self._nodes.get(p)
            if node is None:
                raise_keeper_error(KeeperErrorCode.NONODE, "node does not exist", path=p)
            if version != -1 and node.stat.version != version:
                raise_keeper_error(
                    KeeperErrorCode.BADVERSION,
                    f"version mismatch: expected {version}, actual {node.stat.version}",
                    path=p,
                )
            zxid = self._bump_zxid()
            node.data = payload
            node.stat.version += 1
            node.stat.mzxid = zxid
            node.stat.mtime = _now_ms()
            node.stat.dataLength = len(payload)
            self._fire_watches(p, ZKNodeDataChanged)
            return node.stat.copy()

    def delete(self, path: str, version: int = -1) -> bool:
        """删节点。有子节点抛 :class:`NotEmptyError`;**根节点不可删**。"""
        p = _normalize_path(path)
        if p == "/":
            raise_keeper_error(KeeperErrorCode.BADARGUMENTS, "cannot delete the root node", path=p)
        with self._lock:
            node = self._nodes.get(p)
            if node is None:
                raise_keeper_error(KeeperErrorCode.NONODE, "node does not exist", path=p)
            if node.children:
                raise_keeper_error(
                    KeeperErrorCode.NOTEMPTY,
                    f"node has {len(node.children)} children",
                    path=p,
                )
            if version != -1 and node.stat.version != version:
                raise_keeper_error(
                    KeeperErrorCode.BADVERSION,
                    f"version mismatch: expected {version}, actual {node.stat.version}",
                    path=p,
                )
            self._delete_node(p, node, watch_events=True)
            return True

    def _delete_node(self, path: str, node: ZNode, watch_events: bool) -> None:
        """内部删除 —— **不检查**子节点/版本(调用方保证)。持锁调用。"""
        zxid = self._bump_zxid()
        self._nodes.pop(path, None)
        parent = _parent_path(path)
        if parent is not None:
            parent_node = self._nodes.get(parent)
            if parent_node is not None:
                parent_node.children.pop(_node_name(path), None)
                parent_node.stat.cversion += 1
                parent_node.stat.pzxid = zxid
                parent_node.stat.numChildren = len(parent_node.children)
        if node.is_ephemeral():
            owner = node.stat.ephemeralOwner
            eph = self._ephemerals.get(owner)
            if eph is not None:
                eph.discard(path)
            sess = self._sessions.get(owner)
            if sess is not None:
                sess.ephemerals.discard(path)
        if watch_events:
            self._fire_watches(path, ZKNodeDeleted)
            if parent is not None:
                self._fire_watches(parent, ZKNodeChildrenChanged)

    def delete_recursive(self, path: str) -> int:
        """从叶子往上删一整棵子树,返回删除的节点数。

        ZooKeeper 没有级联删除,所以这个"自己实现"的东西反而更接近
        运维真实的做法。``ReplicatedMergeTree`` 删表时要清掉
        ``/clickhouse/tables/...`` 整棵树,就是这么一层层删的。
        """
        p = _normalize_path(path)
        with self._lock:
            node = self._nodes.get(p)
            if node is None:
                raise_keeper_error(KeeperErrorCode.NONODE, "node does not exist", path=p)
            paths = self._collect_subtree_paths(node)
        count = 0
        # 深到浅:子节点一定比父节点长
        for sub in sorted(paths, key=len, reverse=True):
            if sub == "/":
                continue
            with self._lock:
                n = self._nodes.get(sub)
                if n is not None:
                    self._delete_node(sub, n, watch_events=True)
                    count += 1
        return count

    def _collect_subtree_paths(self, node: ZNode) -> List[str]:
        out = [node.path]
        for child in node.children.values():
            out.extend(self._collect_subtree_paths(child))
        return out

    # ── 调试 ────────────────────────────────────────────────────────────

    def dump_tree(self) -> str:
        """把整棵树画成 ASCII —— 比 ``list_tree()`` 的 dict 好读得多。"""
        with self._lock:
            root = self._nodes.get("/")
            lines: List[str] = []
            if root is not None:
                self._dump_node(root, "", True, lines)
        return "\n".join(lines)

    def _dump_node(self, node: ZNode, prefix: str, is_last: bool, lines: List[str]) -> None:
        if node.path == "/":
            lines.append("/")
            child_prefix = ""
        else:
            connector = "└── " if is_last else "├── "
            label = f"{node.name}"
            meta = f"  (v={node.stat.version}"
            if node.data:
                preview = node.data[:40].decode("utf-8", "replace").replace("\n", "\\n")
                if len(node.data) > 40:
                    preview += "…"
                meta += f", data={preview!r}"
            if node.is_ephemeral():
                meta += f", eph=0x{node.stat.ephemeralOwner:016x}"
            meta += ")"
            lines.append(prefix + connector + label + meta)
            child_prefix = prefix + ("    " if is_last else "│   ")
        children = sorted(node.children.values(), key=lambda n: n.name)
        for i, child in enumerate(children):
            self._dump_node(child, child_prefix, i == len(children) - 1, lines)


# ══════════════════════════════════════════════════════════════════════════
# KeeperServer —— 存储 + 会话 + 后台 reaper 线程
# ══════════════════════════════════════════════════════════════════════════


class KeeperServer:
    """一个"能跑起来"的 Keeper 节点。

    ==================  ====================================================
    真实 Keeper          这里
    ==================  ====================================================
    Raft 共识             **没有**:单实例、一把锁
    网络协议(TCP/HTTPS)   **没有**:直接方法调用
    会话超时回收线程        **有**:``_reaper_loop``
    watch 异步派发         **有**(同步回调,但语义一致)
    持久化(snapshot/log)  **没有**:纯内存
    ==================  ====================================================

    **幂等启停**:测试会创建、销毁成百上千个 KeeperServer,所以
    ``start()``/``stop()`` 重复调用必须安全(第二次 start 返回 False、
    stop 在没跑的时候返回 False),而且不能泄漏线程(``stop`` 会 join)。

    .. warning::

       **单节点 = 无法演示脑裂与多数派丢失。**
       真实 Keeper 的价值恰恰在于"三个节点挂掉一个,服务还在、数据还对"
       (多数派 2/3 仍在)。mini-clickhouse 只有一个内存实例,所以:

       * "Keeper 挂掉"在这里等于"整个进程重启、树全丢" —— 而不是
         "少数派挂掉、多数派继续服务";
       * **脑裂**:不可能发生,因为没有第二个节点可以"分出去";
       * **quorum loss**:不可能发生,因为没有投票。

       教程提到这两个概念时,必须明确说"这里只解释机制,无法复现现象"。
    """

    def __init__(
        self,
        storage: Optional[KeeperStorage] = None,
        session_timeout_ms: int = DEFAULT_SESSION_TIMEOUT_MS,
        tick_ms: int = 50,
    ) -> None:
        self.storage = storage if storage is not None else KeeperStorage()
        self.default_session_timeout_ms = session_timeout_ms
        #: reaper 线程的轮询间隔。真实 Keeper 是"到期时间堆 + 定时唤醒";
        #: 这里是简单轮询 —— 换来实现简单,语义一致。
        self.tick_ms = tick_ms
        self.log: Logger = get_logger("Keeper")
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._running = False
        self._lock = threading.Lock()

    # ── 生命周期 ────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def start(self) -> bool:
        """启动 reaper 线程。**已经跑着就返回 False(不重复启动)。**"""
        with self._lock:
            if self._running:
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._reaper_loop, name="minich-keeper-reaper", daemon=True
            )
            self._running = True
            self._thread.start()
        self.log.debug("Keeper server started")
        return True

    def stop(self, join_timeout: float = 2.0) -> bool:
        """停止 reaper 线程并 join。**没跑就返回 False。**"""
        with self._lock:
            if not self._running:
                return False
            self._stop.set()
            thread = self._thread
            self._running = False
            self._thread = None
        if thread is not None:
            thread.join(join_timeout)
        self.log.debug("Keeper server stopped")
        return True

    def __enter__(self) -> "KeeperServer":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    def _reaper_loop(self) -> None:
        interval = max(self.tick_ms, 1) / 1000.0
        while not self._stop.wait(interval):
            try:
                expired = self.storage.expire_sessions()
                for sid in expired:
                    self.log.notice(f"Keeper session 0x{sid:016x} expired (no heartbeat)")
            except Exception as exc:  # pragma: no cover - reaper 不能因异常死掉
                self.log.error(f"Keeper reaper error: {exc}")

    # ── 会话(委托给 storage)───────────────────────────────────────────

    def new_session(self, timeout_ms: Optional[int] = None) -> Session:
        return self.storage.new_session(
            self.default_session_timeout_ms if timeout_ms is None else timeout_ms
        )

    def close_session(self, session_id: int, expired: bool = False) -> bool:
        return self.storage.close_session(session_id, expired=expired)

    def touch(self, session_id: int) -> None:
        self.storage.touch(session_id)

    def expire_sessions(self, now_ms: Optional[int] = None) -> List[int]:
        return self.storage.expire_sessions(now_ms)

    def force_expire_session(self, session_id: int) -> bool:
        """测试"副本猝死"用 —— 不等超时,立刻回收。"""
        return self.storage.force_expire_session(session_id)

    # ── 事务:multi ──────────────────────────────────────────────────────

    def multi(self, ops: Sequence["_MultiOp"], raise_on_error: bool = True) -> "MultiResult":
        """把一组 op **原子地**应用到树上 —— Zodiac 的 ``multi``。

        .. code-block:: text

           事务开始(快照)
             ├─ op1  成功 → 事件入队(不立即派发)
             ├─ op2  成功 → 事件入队
             └─ op3  ★抛错★
                  └── 回滚快照,丢弃所有入队事件  →  树回到事务前
           事务成功
             └── 统一派发所有 watch 事件(客户端此时才"看见"变化)

        为什么必须原子?以"插入去重"为例:一次插入要同时 (a) 占一个
        block number、(b) 写 ``/blocks/<hash>``、(c) 追加 ``/log`` 条目。
        如果这三步只做了一半,别的副本就会看到"有日志说该拉这个 part、
        但没有去重标记"的坏状态。

        .. warning::

           **真实 ZooKeeper 的 ``multi`` 不是原子的!** 它只是把若干 op
           打包发送、按序执行,某条失败**不回滚**前面的 —— 这是
           ZooKeeper 与 ClickHouse Keeper 的一个关键差异。
           ClickHouse 的 Keeper(Zodiac 协议)才真正实现了带回滚的
           事务。mini-clickhouse 实现的是 **Keeper 语义**(带回滚),
           因为 ``ReplicatedMergeTree`` 依赖它。教程会专门讲这个差异。

        ``raise_on_error=False`` 时不抛异常,而是返回带 ``error`` 的
        :class:`MultiResult`(对应真实 Keeper 返回"每个 op 一个错误码"
        的 ``MultiResponse``)。
        """
        results: List[Any] = []
        try:
            # storage.transaction() 在整个事务期间**不放锁** —— 这才是真原子
            with self.storage.transaction():
                for op in ops:
                    results.append(op.apply(self.storage))
        except KeeperError as exc:
            if raise_on_error:
                raise
            return MultiResult(zxid=self.storage._zxid, results=results, error=exc)
        return MultiResult(zxid=self.storage._zxid, results=results)

    # ── 调试 ────────────────────────────────────────────────────────────

    def dump(self) -> str:
        """整棵树的 ASCII 图 + 当前会话列表 —— 排障用。"""
        lines = ["=== Keeper znode tree ===", self.storage.dump_tree()]
        lines.append("=== sessions ===")
        sessions = self.storage.sessions()
        if not sessions:
            lines.append("  (no active sessions)")
        for s in sessions:
            state = "expired" if s.expired else ("closed" if s.closed else "alive")
            lines.append(
                f"  0x{s.session_id:016x}  timeout={s.timeout_ms}ms  {state}  "
                f"ephemerals={len(s.ephemerals)}"
            )
        return "\n".join(lines)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<KeeperServer running={self.running} nodes={self.storage.node_count()}>"


# ══════════════════════════════════════════════════════════════════════════
# multi 的操作与结果
# ══════════════════════════════════════════════════════════════════════════


class _MultiOp:
    """事务里的一个操作。``apply`` 直接在存储上调对应方法。"""

    __slots__ = ()

    def apply(self, storage: KeeperStorage) -> Any:  # pragma: no cover - 抽象
        raise NotImplementedError


class Create(_MultiOp):
    """``create`` 的 op 形态(名字与 Zodiac 的 ``Create`` 一致)。"""

    __slots__ = ("path", "data", "ephemeral", "sequential", "session_id", "ignore_existing")

    def __init__(self, path: str, data: Union[bytes, str] = b"", ephemeral: bool = False,
                 sequential: bool = False, session_id: Optional[int] = None,
                 ignore_existing: bool = False) -> None:
        self.path = path
        self.data = data
        self.ephemeral = ephemeral
        self.sequential = sequential
        self.session_id = session_id
        self.ignore_existing = ignore_existing

    def apply(self, storage: KeeperStorage) -> Any:
        real = storage.create(
            self.path, self.data, ephemeral=self.ephemeral, sequential=self.sequential,
            session_id=self.session_id, ignore_existing=self.ignore_existing,
        )
        return real

    def __repr__(self) -> str:  # pragma: no cover
        return f"Create({self.path!r})"


class Set(_MultiOp):
    """``set`` 的 op 形态。"""

    __slots__ = ("path", "data", "version")

    def __init__(self, path: str, data: Union[bytes, str], version: int = -1) -> None:
        self.path = path
        self.data = data
        self.version = version

    def apply(self, storage: KeeperStorage) -> Any:
        return storage.set(self.path, self.data, version=self.version)

    def __repr__(self) -> str:  # pragma: no cover
        return f"Set({self.path!r})"


class Delete(_MultiOp):
    """``delete`` 的 op 形态。"""

    __slots__ = ("path", "version")

    def __init__(self, path: str, version: int = -1) -> None:
        self.path = path
        self.version = version

    def apply(self, storage: KeeperStorage) -> Any:
        return storage.delete(self.path, version=self.version)

    def __repr__(self) -> str:  # pragma: no cover
        return f"Delete({self.path!r})"


class Check(_MultiOp):
    """``check`` —— **只校验、不修改**。对应真实 ZooKeeper 的 ``check`` op。

    用途:在事务里断言"这个节点的版本必须还是 5",用来实现
    "读-改-写"的原子版本控制。若节点不存在或版本不符,整个事务回滚。
    """

    __slots__ = ("path", "version")

    def __init__(self, path: str, version: int = -1) -> None:
        self.path = path
        self.version = version

    def apply(self, storage: KeeperStorage) -> Any:
        node = storage.get(self.path)
        if self.version != -1 and node.stat.version != self.version:
            raise_keeper_error(
                KeeperErrorCode.BADVERSION,
                f"check failed: expected version {self.version}, actual {node.stat.version}",
                path=self.path,
            )
        return node.stat

    def __repr__(self) -> str:  # pragma: no cover
        return f"Check({self.path!r}, version={self.version})"


class Get(_MultiOp):
    """``get`` —— 事务里的读(真实 Keeper 的 multi 只允许 get/getChildren 这类只读 op)。"""

    __slots__ = ("path",)

    def __init__(self, path: str) -> None:
        self.path = path

    def apply(self, storage: KeeperStorage) -> Any:
        return storage.get_data(self.path)

    def __repr__(self) -> str:  # pragma: no cover
        return f"Get({self.path!r})"


class GetChildren(_MultiOp):
    """``getChildren`` 的 op 形态。"""

    __slots__ = ("path",)

    def __init__(self, path: str) -> None:
        self.path = path

    def apply(self, storage: KeeperStorage) -> Any:
        return storage.get_children(self.path)

    def __repr__(self) -> str:  # pragma: no cover
        return f"GetChildren({self.path!r})"


class MultiResult:
    """``multi`` 的结果 —— 每个 op 一项,外加事务的 zxid。

    ``ok`` 为 False 时 ``error`` 是导致回滚的那个异常(仅当
    ``raise_on_error=False`` 才会拿到这种结果)。
    """

    __slots__ = ("zxid", "results", "error")

    def __init__(self, zxid: int, results: List[Any], error: Optional[KeeperError] = None) -> None:
        self.zxid = zxid
        self.results = results
        self.error = error

    @property
    def ok(self) -> bool:
        return self.error is None

    def __repr__(self) -> str:  # pragma: no cover
        return f"MultiResult(zxid={self.zxid}, n={len(self.results)}, ok={self.ok})"


# ══════════════════════════════════════════════════════════════════════════
# ReplicatedTablePaths —— 复制表的 znode 布局 + 协调原语
# ══════════════════════════════════════════════════════════════════════════

#: ``/replicas/<r>`` 里存队列指针时的十进制宽度(和 log 编号一致)。
_INDEX_WIDTH = 10


class ReplicatedTablePaths:
    """``ReplicatedMergeTree`` 在 Keeper 里的**目录布局 + 协调原语**。

    这个类是"Keeper 的通用树"和"复制的具体协议"之间的桥:
    它知道每个路径该长什么样,也知道"读队列指针""抢 leader""取 block
    number"这些动作对应的 znode 操作是什么。

    .. code-block:: text

       /clickhouse/tables/<shard>/<table>/
         ├── metadata                表的定义(CREATE TABLE 的文本)
         ├── columns                 列版本快照(ALTER 用)
         ├── log/                    复制日志:顺序节点,一条 = 一个 LogEntry
         │     └── log-0000000001
         ├── replicas/<replica>      每个副本的临时节点,data = "队列指针"
         ├── leader_election/        选主:临时顺序节点,最小的那个是 leader
         │     └── n-0000000000      data = 副本名
         ├── blocks/<hash>           插入去重:这个 block 已被插入过,data = block number
         ├── block_numbers/<part>/   每个分区的取号机(临时顺序节点)
         │     └── block-0000000007
         └── quorum/<log 名>           quorum 插入:哪些副本已经有这个 part

    .. warning::

       真实 ClickHouse 的默认路径由服务端参数 ``default_replica_path``
       决定,新版本默认是 ``/clickhouse/tables/{uuid}/{shard}``(用表的
       UUID 而不是数据库/表名,这样 rename/移动表不会丢复制状态)。
       mini-clickhouse 用**老式的 ``/clickhouse/tables/<shard>/<table>``**
       —— 它更好读、也是网上文档里最常见的写法。两者语义相同,
       只是路径来源不同。
    """

    def __init__(
        self,
        keeper: KeeperStorage,
        shard: str = "01",
        table: str = "t",
        database: str = "default",
        root: Optional[str] = None,
        replica_name: Optional[str] = None,
    ) -> None:
        self.keeper = keeper
        self.shard = shard
        self.table = table
        self.database = database
        self.replica_name = replica_name
        self.root = root if root is not None else f"/clickhouse/tables/{shard}/{table}"

    # ── 纯路径构造 ─────────────────────────────────────────────────────

    def log_path(self) -> str:
        return f"{self.root}/log"

    def log_entry_path(self, index: int) -> str:
        return f"{self.log_path()}/log-{index:0{_INDEX_WIDTH}d}"

    def log_entry_name(self, index: int) -> str:
        return f"log-{index:0{_INDEX_WIDTH}d}"

    def replicas_path(self) -> str:
        return f"{self.root}/replicas"

    def replica_path(self, replica: str) -> str:
        return f"{self.replicas_path()}/{replica}"

    def leader_election_path(self) -> str:
        return f"{self.root}/leader_election"

    def blocks_path(self) -> str:
        return f"{self.root}/blocks"

    def block_numbers_path(self) -> str:
        return f"{self.root}/block_numbers"

    def block_numbers_path_for(self, partition_id: str) -> str:
        return f"{self.block_numbers_path()}/{partition_id}"

    def quorum_path(self) -> str:
        return f"{self.root}/quorum"

    def quorum_path_for(self, entry_name: str) -> str:
        return f"{self.quorum_path()}/{entry_name}"

    def metadata_path(self) -> str:
        return f"{self.root}/metadata"

    def columns_path(self) -> str:
        return f"{self.root}/columns"

    def block_name(self, block_id: str) -> str:
        """``block_id`` → ``/blocks`` 下的 znode 名字。

        真实 ClickHouse 的 ``block_id`` **本身就是一个哈希字符串**
        (``partition_id`` + 块内容的 siphash),所以直接当 znode 名用。
        mini-clickhouse 再哈希一次(``siphash64`` 取十六进制),好处是
        不管调用方塞什么样(哪怕带 ``/``)的 ``block_id`` 进来都安全。
        """
        return f"{siphash64(block_id.encode('utf-8')):016x}"

    def block_path(self, block_id: str) -> str:
        return f"{self.blocks_path()}/{self.block_name(block_id)}"

    # ── 建表骨架 ───────────────────────────────────────────────────────

    def ensure_created(self) -> None:
        """建立整棵目录骨架 —— 对应 ``ReplicatedMergeTree`` 建表时的一长串
        "确保路径存在"操作(真实实现用 ``createIfNotExists`` /
        ``ignore_if_exists=True``,所以并发建表不会 NODEEXISTS)。
        """
        k = self.keeper
        for path in (
            "/clickhouse",
            "/clickhouse/tables",
            f"/clickhouse/tables/{self.shard}",
            self.root,
            self.log_path(),
            self.replicas_path(),
            self.leader_election_path(),
            self.blocks_path(),
            self.block_numbers_path(),
            self.quorum_path(),
            self.metadata_path(),
            self.columns_path(),
        ):
            k.create(path, b"", ignore_existing=True)

    # ── 复制日志 ───────────────────────────────────────────────────────

    def get_max_log_index(self) -> int:
        """``/log`` 下最大的编号;**空日志返回 0**(entry 从 1 开始编号,
        所以 0 天然是"还没有任何 entry"的哨兵值)。"""
        try:
            children = self.keeper.get_children(self.log_path())
        except NoNodeError:
            return 0
        mx = 0
        for name in children:
            if name.startswith("log-"):
                try:
                    mx = max(mx, int(name[4:]))
                except ValueError:
                    continue
        return mx

    def append_log_entry(self, data: bytes, session_id: Optional[int] = None) -> Tuple[str, int]:
        """把一条(已序列化的)日志条目追加到 ``/log``。

        返回 ``(znode 名, 编号)``,例如 ``("log-0000000001", 1)``。
        **用顺序节点** —— 所以"谁先追加谁编号小"是 Keeper 保证的原子事实,
        这就是复制日志的全局顺序来源。
        """
        path = self.keeper.create(
            self.log_path() + "/log-", data, sequential=True, session_id=session_id
        )
        name = _node_name(path)
        index = int(name[4:])
        return name, index

    def read_log_entry(self, index: int) -> Optional[bytes]:
        """读第 ``index`` 条日志的原始字节;不存在返回 ``None``。"""
        path = self.log_entry_path(index)
        try:
            return self.keeper.get_data(path)
        except NoNodeError:
            return None

    def list_log_indices(self) -> List[int]:
        """当前 ``/log`` 下所有编号(升序)。"""
        out = []
        try:
            children = self.keeper.get_children(self.log_path())
        except NoNodeError:
            return out
        for name in children:
            if name.startswith("log-"):
                try:
                    out.append(int(name[4:]))
                except ValueError:
                    continue
        return sorted(out)

    def cleanup_log(self, keep_from: int) -> int:
        """删掉编号小于 ``keep_from`` 的日志条目(真实 CH 的 ``cleanup`` 线程)。

        返回删除条数。真实 CH 只删"所有副本都已处理过"的条目,否则
        落后的副本就再也追不上了 —— mini-clickhouse 把"是否安全"的判断
        交给调用方,只做删除。
        """
        removed = 0
        for index in self.list_log_indices():
            if index >= keep_from:
                continue
            try:
                self.keeper.delete(self.log_entry_path(index))
                removed += 1
            except KeeperError:
                pass
        return removed

    # ── 副本节点与队列指针 ─────────────────────────────────────────────

    def create_replica_node(self, replica: str, session_id: int, pointer: int = 0) -> str:
        """建 ``/replicas/<replica>`` **临时节点**,data 初始为队列指针。

        临时是关键:副本进程一死(会话过期),这个节点自动消失,
        ``system.replicas`` 立刻少一个副本,leader 也立刻重新选。
        """
        return self.keeper.create(
            self.replica_path(replica),
            str(pointer).encode("ascii"),
            ephemeral=True,
            ignore_existing=True,
            session_id=session_id,
        )

    def get_replica_log_pointer(self, replica: str, default: int = 0) -> int:
        """读副本的队列指针(``log_pointer`` = **已经处理到的日志编号**)。"""
        try:
            raw = self.keeper.get_data(self.replica_path(replica))
        except NoNodeError:
            return default
        raw = raw.strip()
        if not raw:
            return default
        try:
            return int(raw.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            return default

    def set_replica_log_pointer(self, replica: str, pointer: int) -> None:
        """写副本的队列指针。

        .. warning::

           **这里没有 CAS。** 真实 CH 用版本号做乐观并发,防止两个线程
           同时推同一个副本的指针。mini-clickhouse 每个副本只有一个
           处理循环,所以不加版本检查 —— 但读者要记住真实实现是有的。
        """
        try:
            self.keeper.set(self.replica_path(replica), str(pointer).encode("ascii"))
        except NoNodeError:
            # 会话已死/节点被删:静默忽略,副本会把自己标记为只读
            pass

    def get_replica_pointer_or_none(self, replica: str) -> Optional[int]:
        try:
            self.keeper.get(self.replica_path(replica))
        except NoNodeError:
            return None
        return self.get_replica_log_pointer(replica)

    def get_active_replicas(self) -> List[str]:
        """当前活着的副本名(``/replicas`` 的子节点,已排序)。

        .. note::

           因为 ``/replicas/<r>`` 是**临时**节点,这里的"子节点"== "活跃
           副本"。真实 ``system.replicas`` 的 ``total_replicas`` 也是数
           这个,所以副本一挂,``total_replicas`` 就会掉 —— 很多人
           第一次看到会以为"表配置丢了",其实只是那个副本的会话断了。
        """
        try:
            return self.keeper.get_children(self.replicas_path())
        except NoNodeError:
            return []

    def count_active_replicas(self) -> int:
        return len(self.get_active_replicas())

    # ── leader 选举 ────────────────────────────────────────────────────

    def join_election(self, replica: str, session_id: int) -> str:
        """加入选主:在 ``leader_election`` 下建一个**临时顺序节点**。

        .. code-block:: text

           leader_election/
             ├── n-0000000000   data="replica_a"   ← 最小 → 它是 leader
             ├── n-0000000001   data="replica_b"
             └── n-0000000002   data="replica_c"

           replica_a 挂掉 → 它的临时节点消失 →
              n-0000000001 变成最小 → replica_b **自动**成为新 leader

        整个过程**没有任何"选举消息"** —— 选主就是"看谁的第一个到达"。
        这就是 ZooKeeper 式选主的精髓。

        .. warning::

           真实 CH 把**副本名**当作顺序节点的名字前缀(所以节点名形如
           ``replica_a0000000003``),再按完整名字排序;mini-clickhouse
           用固定前缀 ``n-`` + 把副本名放进 **data**,好处是排序结果
           严格等于创建顺序(否则 ``replica_a...``/``replica_b...`` 的
           字典序会把不同长度的副本名排错)。
        """
        return self.keeper.create(
            self.leader_election_path() + "/n-",
            replica.encode("utf-8"),
            ephemeral=True,
            sequential=True,
            session_id=session_id,
        )

    def leader_election_children(self) -> List[str]:
        try:
            return sorted(self.keeper.get_children(self.leader_election_path()))
        except NoNodeError:
            return []

    def get_leader(self) -> Optional[str]:
        """当前 leader 的**副本名**;没人选就返回 ``None``。"""
        children = self.leader_election_children()
        if not children:
            return None
        try:
            raw = self.keeper.get_data(f"{self.leader_election_path()}/{children[0]}")
        except NoNodeError:
            return None
        return raw.decode("utf-8") or None

    def is_leader(self, replica: str) -> bool:
        return self.get_leader() == replica

    # ── block number(分区取号机)──────────────────────────────────────

    def create_block_number(self, partition_id: str, session_id: int) -> int:
        """为 ``partition_id`` 取一个 block number,返回整数编号。

        这是插入路径里**最先**做的一件事,而且它必须是临时的、顺序的:

        * **顺序** —— 保证同一个分区里的 part 有全序名字,副本们按
          这个顺序合并,结果才收敛(否则 A/B 各自合并出不同的 part,
          part 名字冲突、去重失效);
        * **临时** —— 持有这个号的是副本的会话。副本死掉时它的号会
          被回收(节点消失),因为号本来就只在"活着的时候"有意义。

        .. warning::

           **号机是只增不减的,不会"归零"。** 编号来自
           :attr:`KeeperStorage._sequence_counters` 里那个每个父路径
           一个、只加不减的计数器 —— 就算 ``/block_numbers/<partition>``
           下的临时节点**全部消失**(所有副本都死光),下一个副本取到的
           号也是从上一个号继续往上,而不是回到 1。

           这不是偷懒,而是**和真实行为一致**:真实 ZooKeeper 的
           顺序编号取自父节点的 ``cversion``,而 ``cversion`` 同样是
           单调递增的 —— 删掉子节点也只会让它继续涨,绝不会回退。
           (mini-clickhouse 的差别只在"用专属计数器而不是 cversion",
           见 :meth:`KeeperStorage._next_sequence`;单调性是一样的。)

           单调性正是 part 名不会撞车的前提:如果号会归零,重启后的
           副本就可能把**不同的数据**取到和以前一样的名字
           (``all_1_1_0``),副本之间会立刻互相污染。
        """
        path = self.block_numbers_path_for(partition_id)
        if self.keeper.exists(path) is None:
            self.keeper.create(path, b"", ignore_existing=True)
        created = self.keeper.create(
            path + "/block-", b"", ephemeral=True, sequential=True, session_id=session_id
        )
        return int(_node_name(created)[6:])

    def get_max_block_number(self, partition_id: str) -> int:
        path = self.block_numbers_path_for(partition_id)
        try:
            children = self.keeper.get_children(path)
        except NoNodeError:
            return 0
        mx = 0
        for name in children:
            if name.startswith("block-"):
                try:
                    mx = max(mx, int(name[6:]))
                except ValueError:
                    continue
        return mx

    # ── 插入去重(blocks)─────────────────────────────────────────────

    def has_block(self, block_id: str) -> bool:
        """这个 block 是否已经被插入过(去重判定)。"""
        return self.keeper.exists(self.block_path(block_id)) is not None

    def add_block(self, block_id: str, block_number: int, replica: str = "") -> str:
        """登记"这个 block 已插入",data 存它拿到的 block number。

        .. warning::

           真实 CH 的 ``/blocks/<hash>`` 节点 data 里还记着**哪些副本
           已经有这个块**,用于"另一个副本遇到同一个 block_id 时,
           要不要把自己也算上"的判断。mini-clickhouse 只存 block number
           —— 去重的判定(存在与否)不受影响,但无法复现"部分副本
           去重、部分不去重"的边界情况。
        """
        return self.keeper.create(
            self.block_path(block_id), str(block_number).encode("ascii"), ignore_existing=True
        )

    def get_block_number(self, block_id: str) -> Optional[int]:
        try:
            raw = self.keeper.get_data(self.block_path(block_id)).strip()
        except NoNodeError:
            return None
        try:
            return int(raw.decode("ascii")) if raw else None
        except (ValueError, UnicodeDecodeError):
            return None

    def remove_block(self, block_id: str) -> bool:
        try:
            return self.keeper.delete(self.block_path(block_id))
        except KeeperError:
            return False

    def list_blocks(self) -> List[str]:
        """``/blocks`` 下的 znode 名字(哈希),按创建顺序(czxid)排。"""
        try:
            children = self.keeper.get_children(self.blocks_path())
        except NoNodeError:
            return []
        pairs = []
        for name in children:
            try:
                stat = self.keeper.stat(f"{self.blocks_path()}/{name}")
            except NoNodeError:
                continue
            pairs.append((stat.czxid, name))
        return [name for _, name in sorted(pairs)]

    def block_count(self) -> int:
        try:
            return self.keeper.count_children(self.blocks_path())
        except NoNodeError:
            return 0

    def prune_blocks(self, window: int) -> int:
        """把 ``/blocks`` 修剪到最多 ``window`` 个 —— 对应设置
        ``replicated_deduplication_window``(默认 100)。

        **去重窗口就是靠这个实现的**:只记住最近 N 个 block,更老的
        被删掉,于是"很久以前的重复插入"不再被识别为重复。
        ``window <= 0`` 表示不限制(真实 CH 里 ``window=0`` 是**关闭**
        去重,mini-clickhouse 用 ``<=0`` 表示"不修剪"以避免歧义,
        由调用方决定是否调它)。
        """
        if window <= 0:
            return 0
        names = self.list_blocks()
        if len(names) <= window:
            return 0
        removed = 0
        for name in names[: len(names) - window]:
            try:
                self.keeper.delete(f"{self.blocks_path()}/{name}")
                removed += 1
            except KeeperError:
                pass
        return removed

    # ── quorum 插入 ────────────────────────────────────────────────────

    def record_quorum(self, entry_name: str, replica: str) -> List[str]:
        """记下"``replica`` 已经有这条日志对应的数据了",返回当前全部名单。

        ``quorum/<log 条目名>`` 的 data 是**换行分隔的副本名**(有序)。
        发起副本轮询它,直到名单长度够 ``insert_quorum`` 为止。

        .. warning::

           **整段读-改-写必须持锁完成。** 这个操作是
           ``读名单 → 追加自己 → 写回`` —— 如果每个子步骤各自加一次锁,
           三个副本同时来确认时就会互相覆盖:大家读到的是同一份旧名单,
           各自追加自己再写回,最后节点上只剩下**最后一个**写的人。
           后果非常隐蔽 —— ``wait_for_quorum`` 会一直等不到足够多的
           确认,于是**明明所有副本都有数据,INSERT 却报 quorum 超时**。

           真实 Keeper 里这个 RMW 是在服务端**单条请求内**完成的
           (Zodiac 的 ``multi`` 事务里带 ``check`` 的 ``set``),所以
           天然原子。这里用 ``with self.keeper.lock`` 达到同样效果 ——
           ``RLock`` 可重入,里面再调 ``get_data``/``set`` 不会自锁。
        """
        path = self.quorum_path_for(entry_name)
        with self.keeper.lock:
            existing = self.get_quorum_replicas(entry_name)
            if replica not in existing:
                existing.append(replica)
            existing = sorted(set(existing))
            payload = ("\n".join(existing)).encode("utf-8")
            if self.keeper.exists(path) is None:
                self.keeper.create(path, payload, ignore_existing=True)
            else:
                self.keeper.set(path, payload)
            return existing

    def get_quorum_replicas(self, entry_name: str) -> List[str]:
        try:
            raw = self.keeper.get_data(self.quorum_path_for(entry_name))
        except NoNodeError:
            return []
        text = raw.decode("utf-8").strip()
        if not text:
            return []
        return sorted(set(text.split("\n")))

    def remove_quorum(self, entry_name: str) -> bool:
        try:
            return self.keeper.delete(self.quorum_path_for(entry_name))
        except KeeperError:
            return False

    # ── metadata / columns ─────────────────────────────────────────────

    def set_metadata(self, create_table_query: str) -> None:
        """存表的定义文本(真实 CH 里 ``ALTER`` 也会改它并 bump 版本)。"""
        payload = create_table_query.encode("utf-8")
        with self.keeper.lock:  # 读-改-写,持锁做完整段(理由见 record_quorum)
            if self.keeper.exists(self.metadata_path()) is None:
                self.keeper.create(self.metadata_path(), payload, ignore_existing=True)
            else:
                self.keeper.set(self.metadata_path(), payload)

    def get_metadata(self) -> bytes:
        return self.keeper.get_data(self.metadata_path())

    # ── columns(列结构版本)─────────────────────────────────────────────

    def set_columns(self, columns_description: Union[bytes, str]) -> int:
        """更新 ``/columns`` 节点,返回**新的版本号**。

        版本号就是 znode 的 ``version``(每次 ``set`` 加一)。它对应
        ``system.replicas.columns_version``:副本们靠比对它来判断
        "我的列结构落后了,需要按 ``ALTER_METADATA`` 追上来"。
        """
        payload = columns_description.encode("utf-8") if isinstance(columns_description, str) else bytes(columns_description)
        # 同样是读-改-写(exists → create/set),持锁做完整段(理由见 record_quorum)
        with self.keeper.lock:
            if self.keeper.exists(self.columns_path()) is None:
                self.keeper.create(self.columns_path(), payload, ignore_existing=True)
            else:
                self.keeper.set(self.columns_path(), payload)
            return self.get_columns_version()

    def get_columns_version(self) -> int:
        try:
            return self.keeper.stat(self.columns_path()).version
        except NoNodeError:
            return 0

    def __repr__(self) -> str:  # pragma: no cover
        return f"ReplicatedTablePaths({self.root!r})"

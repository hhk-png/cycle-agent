"""Keeper 与复制引擎的测试 —— ``minich.keeper`` + ``minich.replication``。

这两个模块是 ``ReplicatedMergeTree`` 的地基,它们**不依赖**数据库的
``settings`` / ``storages`` / ``execution``(见两个模块的文档字符串),
所以可以脱离"表引擎"单独测 —— 这正是本文件的做法:只用
:class:`~minich.keeper.KeeperStorage` / :class:`~minich.keeper.KeeperServer` /
:class:`~minich.replication.LocalCluster` 三个对象就能把整条复制链路演一遍。

测试分五块,顺序与学习顺序一致:

1. **Keeper 读写**:create / get / set / delete 的语义 —— 尤其是
   "读不存在的数据要报错,而不是返回 None"这一条(``get`` vs ``exists``);
2. **Keeper 错误码**:断言的是 ZooKeeper 那一套**负数**错误码
   (``NONODE = -101``、``NODEEXISTS = -110`` …),不是 ClickHouse 的正数;
3. **顺序 / 临时节点 / watch / multi**:复制的四块积木;
4. **LogEntry 序列化**:每个 ``EntryType`` 都要能 ``to_bytes`` → ``from_bytes``
   原样回来(两个副本要对同一条日志达成一致,靠的就是这个确定性);
5. **LocalCluster**:3 副本收敛、leader 重选、插入去重。

.. note::

   本文件里有若干 ``@pytest.mark.xfail`` 测试 —— 它们**故意**断言"正确的
   行为",而 mini-clickhouse 目前做不到。``strict`` 是关闭的(默认),所以
   如果哪天库被修好了,这些测试会由 ``xfailed`` 变成 ``xpassed``,套件仍然
   通过。每个 xfail 的 ``reason`` 里都写清了是什么 bug。

.. note::

   时间相关的测试一律**不睡真实时间**:需要"会话过期"时,要么用
   :meth:`~minich.keeper.KeeperServer.force_expire_session`(立刻过期),
   要么给 :meth:`~minich.keeper.KeeperStorage.expire_sessions` 传一个
   显式的 ``now_ms``。唯一真的等了一下的地方是 quorum 超时测试,它把
   超时设成 50ms(API 允许的最小可用值),并只等这一次。
"""

from __future__ import annotations

import pytest

from minich.errors import ErrorCode, MiniChError
from minich.keeper import (
    # 异常与错误码
    BadArgumentsError,
    BadVersionError,
    Check,
    Create,
    CreateMode,
    Delete,
    Get,
    GetChildren,
    KeeperError,
    KeeperErrorCode,
    KeeperServer,
    KeeperStorage,
    MultiResult,
    NoChildrenForEphemeralsError,
    NodeExistsError,
    NoNodeError,
    NotEmptyError,
    ReplicatedTablePaths,
    SessionExpiredError,
    Set,
    UnknownSessionError,
    # watch
    ZK_ALL_EVENTS,
    ZKNodeChildrenChanged,
    ZKNodeCreated,
    ZKNodeDataChanged,
    ZKNodeDeleted,
)
from minich.replication import (
    DuplicateInsertError,
    ENTRY_TYPE_NAMES,
    EntryType,
    InsertResult,
    LocalCluster,
    LOG_ENTRY_FORMAT_VERSION,
    LogEntry,
    MergeCandidate,
    PartInfo,
    PartState,
    PartStore,
    QuorumTimeoutError,
    Replica,
    ReplicaReadOnlyError,
    default_merge_selector,
)

# ══════════════════════════════════════════════════════════════════════════
# 夹具 —— 全部在本文件内定义,不动 tests/conftest.py
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def keeper() -> KeeperStorage:
    """一个干净的、单实例的内存 Keeper(只有一棵根节点的树)。"""
    return KeeperStorage()


@pytest.fixture
def server(keeper: KeeperStorage) -> KeeperServer:
    """包着 ``keeper`` 的 KeeperServer —— 但不 start()，不启后台 reaper 线程。

    大部分测试不需要 reaper(会话回收用手动调用即可),不启线程就不会有
    线程泄漏,测试也更确定。
    """
    return KeeperServer(keeper)


@pytest.fixture
def make_cluster():
    """造 :class:`LocalCluster` 的工厂,并在测试结束时统一 ``stop()``。

    ``stop()`` 会 join 掉 keeper 的后台 reaper 线程 —— 每个 cluster 一个
    daemon 线程,不 join 的话几百个测试下来会攒一堆。
    """
    created: list[LocalCluster] = []

    def _make(**kwargs) -> LocalCluster:
        cluster = LocalCluster(**kwargs)
        created.append(cluster)
        return cluster

    yield _make
    for cluster in created:
        cluster.stop()


@pytest.fixture
def cluster(make_cluster) -> LocalCluster:
    """默认的 3 副本集群(``replica_a`` / ``replica_b`` / ``replica_c``)。"""
    return make_cluster()


# ══════════════════════════════════════════════════════════════════════════
# 1. Keeper 基础读写
# ══════════════════════════════════════════════════════════════════════════


def test_create_get_set_delete_roundtrip(keeper: KeeperStorage) -> None:
    """create → get → set → delete 这条最常用的链路。

    ``create`` **返回真实路径**(顺序节点会带自增后缀,普通节点就是你给的
    路径);``get`` 返回的是 :class:`~minich.keeper.ZNode` 快照。
    """
    keeper.create("/a")                    # ZooKeeper 不会自动建中间目录
    path = keeper.create("/a/b", b"hello")
    assert path == "/a/b"

    node = keeper.get("/a/b")
    assert node.data == b"hello"          # get 拿到的是数据本身
    assert node.stat.version == 0          # 刚建好,版本 0
    assert node.stat.numChildren == 0

    keeper.set("/a/b", b"world")
    assert keeper.get_data("/a/b") == b"world"
    assert keeper.stat("/a/b").version == 1  # set 一次,版本 +1

    assert keeper.delete("/a/b") is True
    assert keeper.exists("/a/b") is None
    # 删掉之后 get 必须抛 NONODE —— 而不是返回 None
    with pytest.raises(NoNodeError):
        keeper.get("/a/b")


def test_create_accepts_str_and_bytes(keeper: KeeperStorage) -> None:
    """``create``/``set`` 的 data 收 ``str``(按 UTF-8 编码)和 ``bytes``。"""
    keeper.create("/s", "中文")
    assert keeper.get_data("/s") == "中文".encode("utf-8")
    keeper.set("/s", bytearray(b"xy"))
    assert keeper.get_data("/s") == b"xy"


def test_get_returns_snapshot_not_live_node(keeper: KeeperStorage) -> None:
    """读操作返回的是**快照**,改它不会影响 Keeper 里的节点。

    真实 ZooKeeper 里客户端本来就只能拿到序列化过去的副本;mini-clickhouse
    用 ``ZNode.copy()`` 复刻这一点,顺手也堵住了"测试拿到活对象、断言时
    数据已被改掉"的坑。
    """
    keeper.create("/a", b"orig")
    snapshot = keeper.get("/a")
    snapshot.data = b"tampered"
    snapshot.stat.version = 99
    assert keeper.get_data("/a") == b"orig"
    assert keeper.stat("/a").version == 0


def test_exists_and_get_children(keeper: KeeperStorage) -> None:
    """``exists`` 找不返回 ``None``(它就是问"在不在");``get_children``
    返回**已排序**的子节点名字。"""
    keeper.create("/p")
    assert keeper.exists("/p") is not None
    assert keeper.exists("/nope") is None

    for name in ("c", "a", "b"):
        keeper.create(f"/p/{name}")
    assert keeper.get_children("/p") == ["a", "b", "c"]  # 确定性排序
    assert keeper.count_children("/p") == 3
    with pytest.raises(NoNodeError):
        keeper.get_children("/nope")  # 注意:get_children 不存在要报错


def test_create_ignore_existing(keeper: KeeperStorage) -> None:
    """``ignore_existing=True`` 对应 Zodiac 的 ``ignore_if_exists`` ——
    ClickHouse 建表时那一长串"确保目录存在"全靠它,否则并发建表必然
    NODEEXISTS。"""
    keeper.create("/once", b"first", ignore_existing=True)
    assert keeper.create("/once", b"second", ignore_existing=True) == "/once"
    assert keeper.get_data("/once") == b"first"  # 已存在时**不覆盖**数据

    keeper.create("/once2")
    keeper.create("/once2/child")
    assert keeper.count_children("/once2") == 1


def test_node_path_normalization(keeper: KeeperStorage) -> None:
    """路径会被规范化:``//`` 折叠、去掉尾斜杠。"""
    keeper.create("/a")
    assert keeper.create("/a//b/") == "/a/b"
    assert keeper.exists("/a/b") is not None


# ══════════════════════════════════════════════════════════════════════════
# 2. Keeper 错误码 —— ZooKeeper 的负数错误码
# ══════════════════════════════════════════════════════════════════════════


def test_zk_error_code_constants() -> None:
    """错误码**照抄** ZooKeeper ``KeeperException.Code`` —— 全是负数。

    为什么强调"负数"?因为 ClickHouse 自己的错误码是正数(``Code: 60``),
    两者是**两套独立编号空间**。客户端判断"该不该重试"用的就是 ZK 这套
    (``CONNECTIONLOSS`` 可重试、``NONODE`` 不可重试)。
    """
    assert KeeperErrorCode.NONODE == -101
    assert KeeperErrorCode.NODEEXISTS == -110
    assert KeeperErrorCode.NOTEMPTY == -111
    assert KeeperErrorCode.BADVERSION == -103
    assert KeeperErrorCode.SESSIONEXPIRED == -112
    assert KeeperErrorCode.NOCHILDRENFOREPHEMERALS == -108
    assert KeeperErrorCode.BADARGUMENTS == -8
    assert KeeperErrorCode.UNKNOWNSESSION == -12


def test_get_missing_raises_nonode_with_metadata(keeper: KeeperStorage) -> None:
    """读一个不存在的节点 → ``NoNodeError``,``exc.code == NONODE``。

    顺便验证异常携带的元数据:``code``(ZK 错误码)、``name``(官方错误名)、
    ``path``(出错的路径)、以及它继承自 :class:`~minich.errors.MiniChError`。
    """
    with pytest.raises(NoNodeError) as excinfo:
        keeper.get("/does/not/exist")
    exc = excinfo.value
    assert exc.code == KeeperErrorCode.NONODE
    assert exc.zk_code == KeeperErrorCode.NONODE
    assert exc.name == "NONODE"
    assert exc.path == "/does/not/exist"
    assert isinstance(exc, KeeperError)
    assert isinstance(exc, MiniChError)
    assert "NONODE" in str(exc)


def test_create_existing_raises_nodeexists(keeper: KeeperStorage) -> None:
    """建一个已存在的节点 → ``NodeExistsError`` (NODEEXISTS)。"""
    keeper.create("/dup")
    with pytest.raises(NodeExistsError) as excinfo:
        keeper.create("/dup")
    assert excinfo.value.code == KeeperErrorCode.NODEEXISTS


def test_delete_non_empty_raises_notempty(keeper: KeeperStorage) -> None:
    """ZooKeeper **不做级联删除** —— 删有子节点的节点 → ``NotEmptyError``。

    这不是疏漏而是设计:级联删除会变成一个长事务,而 ZooKeeper 要求每次
    操作都是常数时间的共识提交。想删一整棵子树得从叶子往上逐个删。
    """
    keeper.create("/p")
    keeper.create("/p/child")
    with pytest.raises(NotEmptyError) as excinfo:
        keeper.delete("/p")
    assert excinfo.value.code == KeeperErrorCode.NOTEMPTY
    # 删掉子节点之后就能删父节点了
    keeper.delete("/p/child")
    assert keeper.delete("/p") is True


def test_set_version_is_compare_and_swap(keeper: KeeperStorage) -> None:
    """``set(version=v)`` 是 CAS:版本不符 → ``BadVersionError``。

    ``version=-1`` 表示"不检查版本"。这套乐观并发控制是
    ``ReplicatedMergeTree`` 里防止两个线程同时推同一个指针的基础。
    """
    keeper.create("/n", b"a")
    with pytest.raises(BadVersionError) as excinfo:
        keeper.set("/n", b"b", version=5)
    assert excinfo.value.code == KeeperErrorCode.BADVERSION
    assert keeper.get_data("/n") == b"a"       # 失败了,数据没动

    keeper.set("/n", b"b", version=0)          # 版本正好是 0 → 成功
    assert keeper.get_data("/n") == b"b"
    assert keeper.stat("/n").version == 1

    keeper.set("/n", b"c")                     # 默认 version=-1 → 不检查
    assert keeper.stat("/n").version == 2


@pytest.mark.parametrize("bad_path", ["nope", "/a/../b", "/a/./b"])
def test_bad_path_raises_badarguments(keeper: KeeperStorage, bad_path: str) -> None:
    """路径不以 ``/`` 开头、或含 ``.``/``..`` → ``BadArgumentsError``。

    ZooKeeper 对路径的规矩很严(不能有 ``//``、不能有 ``.``/``..``、
    除根以外不能有尾斜杠),我们照抄 —— 否则不同客户端拼出来的路径
    会指向不同的节点。
    """
    with pytest.raises(BadArgumentsError) as excinfo:
        keeper.create(bad_path)
    assert excinfo.value.code == KeeperErrorCode.BADARGUMENTS


def test_delete_root_raises_badarguments(keeper: KeeperStorage) -> None:
    """根节点不可删 —— 也是 ``BADARGUMENTS``。"""
    with pytest.raises(BadArgumentsError) as excinfo:
        keeper.delete("/")
    assert excinfo.value.code == KeeperErrorCode.BADARGUMENTS


def test_structural_errors(keeper: KeeperStorage) -> None:
    """一组"树结构不允许"的错误,每个都对应一条真实的 Keeper 约束。"""
    # 父路径不存在 → NONODE(create **不会**自动建中间目录)
    with pytest.raises(NoNodeError) as e1:
        keeper.create("/no/such/parent")
    assert e1.value.code == KeeperErrorCode.NONODE

    # 临时节点不能有子节点 —— 否则会话一断就会留下悬空分支
    session = keeper.new_session()
    keeper.create("/eph", b"", ephemeral=True, session_id=session.session_id)
    with pytest.raises(NoChildrenForEphemeralsError) as e2:
        keeper.create("/eph/child")
    assert e2.value.code == KeeperErrorCode.NOCHILDRENFOREPHEMERALS

    # 建临时节点却没给 session_id → BADARGUMENTS
    with pytest.raises(BadArgumentsError) as e3:
        keeper.create("/eph2", b"", ephemeral=True)
    assert e3.value.code == KeeperErrorCode.BADARGUMENTS

    # 给的 session_id 根本不存在 → UNKNOWNSESSION
    with pytest.raises(UnknownSessionError) as e4:
        keeper.create("/eph3", b"", ephemeral=True, session_id=0xDEADBEEF)
    assert e4.value.code == KeeperErrorCode.UNKNOWNSESSION


# ══════════════════════════════════════════════════════════════════════════
# 3. 顺序节点 / 临时节点 / 会话
# ══════════════════════════════════════════════════════════════════════════


def test_sequential_nodes_are_monotonic(keeper: KeeperStorage) -> None:
    """顺序节点 = "追加并全局排序"的原子操作 —— 复制日志的全局顺序来源。

    ``/log/log-`` 前缀 + 自增编号,天然就是一条全局有序的日志。
    """
    keeper.create("/log")
    created = [keeper.create("/log/log-", b"x", sequential=True) for _ in range(3)]
    assert created == [
        "/log/log-0000000001",
        "/log/log-0000000002",
        "/log/log-0000000003",
    ]
    # 编号是 10 位零填充(和 ReplicatedTablePaths.log_entry_name 一致)
    assert keeper.get_children("/log") == [
        "log-0000000001", "log-0000000002", "log-0000000003",
    ]
    # 顺序节点名必然唯一 → 永远不会 NODEEXISTS
    assert keeper.create("/log/log-", b"x", sequential=True) == "/log/log-0000000004"


def test_sequential_via_create_mode(keeper: KeeperStorage) -> None:
    """``mode=`` 是"原味"写法,与两个布尔参数等价。"""
    keeper.create("/d")
    assert keeper.create("/d/n-", b"", mode=CreateMode.PERSISTENT_SEQUENTIAL) == "/d/n-0000000001"
    session = keeper.new_session()
    name = keeper.create(
        "/d/e-", b"", mode=CreateMode.EPHEMERAL_SEQUENTIAL, session_id=session.session_id
    )
    assert name == "/d/e-0000000002"   # 与上面的持久顺序节点共用一个自增计数器
    assert keeper.get(name).is_ephemeral()


def test_ephemeral_removed_when_session_expires(keeper: KeeperStorage) -> None:
    """**Keeper 被所有分布式系统选中的根本原因**:会话一断,临时节点就没。

    这里是整条复制链路里"故障检测"的全部实现 —— 没有心跳超时轮询,
    没有谁去主动清理,``/replicas/<r>`` 不见了就等于那个副本掉线了。
    """
    session = keeper.new_session(timeout_ms=30_000)
    keeper.create("/replicas")
    keeper.create("/replicas/r1", b"0", ephemeral=True, session_id=session.session_id)
    assert keeper.exists("/replicas/r1") is not None
    assert keeper.get("/replicas/r1").stat.ephemeralOwner == session.session_id

    # 不等 30 秒 —— 直接"立刻过期",确定性地触发回收路径
    assert keeper.force_expire_session(session.session_id) is True
    assert keeper.exists("/replicas/r1") is None
    assert keeper.get_children("/replicas") == []


def test_ephemeral_removed_on_close_session(keeper: KeeperStorage) -> None:
    """客户端**主动**关闭会话(``expired=False``)同样删掉临时节点。"""
    session = keeper.new_session()
    keeper.create("/e", b"", ephemeral=True, session_id=session.session_id)
    assert keeper.close_session(session.session_id) is True
    assert keeper.exists("/e") is None


def test_expire_sessions_with_explicit_now_ms(keeper: KeeperStorage) -> None:
    """``expire_sessions(now_ms=...)`` 可以确定性地判定"谁超时了"。

    真实 Keeper 由后台 reaper 周期性调它;测试里传一个显式的 ``now_ms``
    就不用真的等 —— 这是"测试不许睡真实时间"的落地方式。
    """
    session = keeper.new_session(timeout_ms=1_000)
    keeper.create("/x", b"", ephemeral=True, session_id=session.session_id)

    # 还没超时
    assert keeper.expire_sessions(now_ms=session.last_touched + 500) == []
    # 超过 timeout_ms 了 → 被回收,返回值里带它的 session_id
    expired = keeper.expire_sessions(now_ms=session.last_touched + 2_000)
    assert expired == [session.session_id]
    assert keeper.exists("/x") is None
    assert keeper.get_session(session.session_id) is None


def test_touch_expired_session_raises_sessionexpired(keeper: KeeperStorage) -> None:
    """会话没了之后``touch``(心跳)→ ``SESSIONEXPIRED``;客户端据此重连并
    **重建所有临时节点与 watch**。"""
    session = keeper.new_session()
    keeper.force_expire_session(session.session_id)
    with pytest.raises(SessionExpiredError) as excinfo:
        keeper.touch(session.session_id)
    assert excinfo.value.code == KeeperErrorCode.SESSIONEXPIRED


def test_session_lifecycle() -> None:
    """:class:`~minich.keeper.Session` 的字段与存活判定。"""
    keeper = KeeperStorage()
    session = keeper.new_session(timeout_ms=1_000)
    assert session.session_id != 0
    assert session.timeout_ms == 1_000
    assert session.timeout == 1_000          # 别名(真实 Keeper 的字段就叫 timeout)
    assert session.is_alive() is True
    assert session.is_alive(session.last_touched + 2_000) is False

    session.touch(session.last_touched + 500)
    assert session.is_alive(session.last_touched) is True


def test_keeper_server_start_stop_is_idempotent(keeper: KeeperStorage) -> None:
    """``start`` / ``stop`` 重复调用必须安全,且不泄漏线程。

    测试会造出成百上千个 KeeperServer,所以幂等启停是硬要求:第二次
    ``start`` 返回 False、没跑的时候 ``stop`` 返回 False。
    """
    server = KeeperServer(keeper)
    assert server.running is False
    assert server.start() is True
    assert server.running is True
    assert server.start() is False            # 已经在跑,不重复启动
    assert server.stop() is True
    assert server.running is False
    assert server.stop() is False             # 已经停了,不重复 join


def test_keeper_server_session_delegation(keeper: KeeperStorage) -> None:
    """``KeeperServer`` 的会话接口委托给 ``storage``。"""
    server = KeeperServer(keeper, session_timeout_ms=1_000)
    session = server.new_session()
    assert session.timeout_ms == 1_000         # 用 server 的默认超时
    assert server.force_expire_session(session.session_id) is True
    assert server.close_session(session.session_id) is False  # 已经没了


# ══════════════════════════════════════════════════════════════════════════
# 4. watch
# ══════════════════════════════════════════════════════════════════════════


def test_watch_fires_on_create(keeper: KeeperStorage) -> None:
    """``exists(缺失路径, watch=True)``:节点一被建出来就收到 NodeCreated。"""
    hits: list[tuple[int, str]] = []
    keeper.exists("/watched", watch=True, callback=lambda t, p: hits.append((t, p)))
    keeper.create("/watched")
    assert hits == [(ZKNodeCreated, "/watched")]


def test_watch_fires_on_data_change_and_is_one_shot(keeper: KeeperStorage) -> None:
    """watch 是**一次性**的:触发一次就自动注销,想继续监听必须重新注册。

    这是 ZooKeeper 最著名的"坑":忘记重新注册 = 静默漏事件。
    """
    keeper.create("/w")
    hits: list[int] = []
    keeper.add_watch("/w", None, lambda t, p: hits.append(t))

    keeper.set("/w", b"1")     # 触发一次 → watch 被消费
    assert hits == [ZKNodeDataChanged]
    assert keeper.watch_count("/w") == 0

    keeper.set("/w", b"2")     # 没重新注册 → 收不到
    assert hits == [ZKNodeDataChanged]


def test_watch_fires_on_delete(keeper: KeeperStorage) -> None:
    """监听一个已存在的节点,把它删掉 → NodeDeleted。"""
    keeper.create("/d")
    hits: list[int] = []
    keeper.exists("/d", watch=True, callback=lambda t, p: hits.append(t))
    keeper.delete("/d")
    assert hits == [ZKNodeDeleted]


def test_persistent_watch_fires_repeatedly(keeper: KeeperStorage) -> None:
    """``persistent=True`` 模拟 ClickHouse Keeper 的持久 watch(Zodiac 的
    ``AddWatchMode::PERSISTENT``)—— 触发后**不**注销。"""
    keeper.create("/p")
    hits: list[int] = []
    keeper.add_watch("/p", ZKNodeDataChanged, lambda t, p: hits.append(t), persistent=True)
    keeper.set("/p", b"1")
    keeper.set("/p", b"2")
    keeper.set("/p", b"3")
    assert hits == [ZKNodeDataChanged, ZKNodeDataChanged, ZKNodeDataChanged]
    assert keeper.watch_count("/p") == 1


def test_watch_event_mask_filters(keeper: KeeperStorage) -> None:
    """``event_mask`` 是**一组事件类型的集合**,不是位掩码。

    注意 ``ZKNodeCreated=1``、``ZKNodeDeleted=2``、``ZKNodeDataChanged=3``:
    ``1 | 2 == 3`` 数值上成立纯属巧合 —— 千万不能按位或起来当掩码用。
    """
    keeper.create("/m")
    hits: list[int] = []
    keeper.add_watch("/m", {ZKNodeCreated, ZKNodeDeleted}, lambda t, p: hits.append(t))
    keeper.set("/m", b"x")        # DataChanged 不在掩码里 → 不触发,watch 也不被消费
    assert hits == []
    keeper.delete("/m")           # NodeDeleted 在掩码里 → 触发
    assert hits == [ZKNodeDeleted]


def test_get_children_watch_fires_on_child_added(keeper: KeeperStorage) -> None:
    """``get_children(path, watch=True)``:子节点列表变了就通知。"""
    keeper.create("/c")
    hits: list[int] = []
    keeper.get_children("/c", watch=True, callback=lambda t, p: hits.append(t))
    keeper.create("/c/new-child")
    assert hits == [ZKNodeChildrenChanged]
    assert keeper.watch_count("/c") == 0   # 一次性,已被消费


def test_remove_watch(keeper: KeeperStorage) -> None:
    """手动注销 watch(真实客户端断开时也会这么做)。"""
    keeper.create("/r")
    hits: list[int] = []
    watch = keeper.add_watch("/r", None, lambda t, p: hits.append(t))
    assert keeper.watch_count("/r") == 1
    assert keeper.remove_watch(watch) is True
    assert keeper.watch_count("/r") == 0
    keeper.set("/r", b"1")
    assert hits == []
    assert keeper.remove_watch(watch) is False   # 重复注销返回 False


def test_zk_all_events_constant_is_a_frozenset() -> None:
    """``ZK_ALL_EVENTS`` 是"所有事件类型"的集合,可以直接当 ``event_mask``。"""
    assert ZK_ALL_EVENTS == frozenset(
        (ZKNodeCreated, ZKNodeDeleted, ZKNodeDataChanged, ZKNodeChildrenChanged)
    )
    keeper = KeeperStorage()
    keeper.create("/all")
    hits: list[int] = []
    keeper.add_watch("/all", ZK_ALL_EVENTS, lambda t, p: hits.append(t))
    keeper.set("/all", b"y")
    assert hits == [ZKNodeDataChanged]


# ── watch 的 xfail:以下三条断言的是**真实 ZooKeeper 的语义** ─────────────


@pytest.mark.xfail(
    raises=AssertionError,
    reason=(
        "BUG(minich.keeper):get(path, watch=True) 用 event_mask=None(=所有事件)注册 watch,"
        "于是子节点变化(ZKNodeChildrenChanged)也会把它消费掉。真实 ZooKeeper 的 getData watch "
        "只在 DataChanged/Deleted 时触发 —— 一次性 watch 被错误的事件消费,后续真正的数据变化会被漏掉。"
    ),
)
def test_get_watch_should_not_fire_on_children_changed(keeper: KeeperStorage) -> None:
    keeper.create("/a")
    hits: list[int] = []
    keeper.get("/a", watch=True, callback=lambda t, p: hits.append(t))
    keeper.create("/a/child")           # 子节点变化:getData watch 不该被触发
    assert hits == []


@pytest.mark.xfail(
    raises=AssertionError,
    reason=(
        "BUG(minich.keeper):get_children(path, watch=True) 同样用 None(=所有事件)注册,"
        "节点自身的数据变化(ZKNodeDataChanged)会触发它。真实 ZooKeeper 的 getChildren watch "
        "只在子节点列表变化或节点被删时触发。"
    ),
)
def test_get_children_watch_should_not_fire_on_data_changed(keeper: KeeperStorage) -> None:
    keeper.create("/b")
    keeper.create("/b/c")
    hits: list[int] = []
    keeper.get_children("/b", watch=True, callback=lambda t, p: hits.append(t))
    keeper.set("/b", b"x")             # 数据变化:getChildren watch 不该被触发
    assert hits == []


@pytest.mark.xfail(
    raises=AssertionError,
    reason=(
        "BUG(minich.keeper):get() 在读节点**之前**就注册了 watch。于是 "
        "get(missing, watch=True) 先挂上一个 watch、再抛 NONODE,留下一个悬空 watch。"
        "真实 ZooKeeper 里失败的 getData 不注册 watch。"
    ),
)
def test_get_missing_with_watch_should_not_leave_watch(keeper: KeeperStorage) -> None:
    with pytest.raises(NoNodeError):
        keeper.get("/missing", watch=True, callback=lambda t, p: None)
    assert keeper.watch_count("/missing") == 0


# ══════════════════════════════════════════════════════════════════════════
# 5. multi —— 带回滚的事务
# ══════════════════════════════════════════════════════════════════════════


def test_multi_applies_all_ops(keeper: KeeperStorage, server: KeeperServer) -> None:
    """``multi`` 把一组 op 原子地应用;成功时每个 op 的结果按序返回。"""
    keeper.create("/x")
    result = server.multi([Create("/z", b"1"), Set("/x", b"2")])
    assert isinstance(result, MultiResult)
    assert result.ok is True
    assert result.error is None
    assert result.results[0] == "/z"
    assert result.zxid > 0
    assert keeper.get_data("/z") == b"1"
    assert keeper.get_data("/x") == b"2"


def test_multi_rolls_back_on_failure(keeper: KeeperStorage, server: KeeperServer) -> None:
    """**任一个 op 失败 → 整棵树回到事务前。**

    以"插入去重"为例:一次插入要同时 (a) 占一个 block number、(b) 写
    ``/blocks/<hash>``、(c) 追加 ``/log``。如果只做了一半,别的副本就会
    看到"有日志说该拉这个 part、但没有去重标记"的坏状态。
    """
    keeper.create("/y")
    keeper.create("/y/child")          # /y 非空 → 删它必然失败
    before = keeper.node_count()

    with pytest.raises(NotEmptyError) as excinfo:
        server.multi([Create("/z", b"1"), Delete("/y")])
    assert excinfo.value.code == KeeperErrorCode.NOTEMPTY
    assert keeper.node_count() == before
    assert keeper.exists("/z") is None  # 前一个 op 也回滚了


def test_multi_raise_on_error_false(keeper: KeeperStorage, server: KeeperServer) -> None:
    """``raise_on_error=False``:不抛异常,返回带 ``error`` 的结果
    (对应真实 Keeper 返回"每个 op 一个错误码"的 ``MultiResponse``)。"""
    keeper.create("/nonempty")
    keeper.create("/nonempty/child")
    result = server.multi(
        [Create("/z", b"1"), Delete("/nonempty")], raise_on_error=False
    )
    assert result.ok is False
    assert isinstance(result.error, NotEmptyError)
    assert result.error.code == KeeperErrorCode.NOTEMPTY
    assert keeper.exists("/z") is None


def test_multi_check_and_read_ops(keeper: KeeperStorage, server: KeeperServer) -> None:
    """``Check``(只校验不修改)失败 → 整个事务回滚;``Get`` / ``GetChildren``
    是事务里的只读 op。"""
    keeper.create("/c")
    keeper.set("/c", b"v")             # version 现在是 1
    read = server.multi([Get("/c"), GetChildren("/c")])
    assert read.ok is True
    assert read.results == [b"v", []]

    with pytest.raises(BadVersionError) as excinfo:
        server.multi([Set("/c", b"z"), Check("/c", version=99)])
    assert excinfo.value.code == KeeperErrorCode.BADVERSION
    assert keeper.get_data("/c") == b"v"   # Set 也被回滚了


def test_multi_dispatches_events_only_after_commit(
    keeper: KeeperStorage, server: KeeperServer
) -> None:
    """watch 事件在**提交之后**才派发 —— 客户端看到的是"整件事发生了"。

    回滚的事务则把所有入队事件丢掉:客户端**不该**看到"写了一半"的中间态。
    """
    keeper.create("/w")
    hits: list[int] = []
    keeper.add_watch("/w", ZKNodeDataChanged, lambda t, p: hits.append(t))

    server.multi([Set("/w", b"1"), Set("/w", b"2")])
    # 一次性 watch:即便事务里改了两次,提交后也只派发一次
    assert hits == [ZKNodeDataChanged]
    assert keeper.get_data("/w") == b"2"

    # 回滚的事务:create 了 /r 但整个事务失败 → 既没建 /r,也没有任何事件
    rolled_back: list[int] = []
    keeper.add_watch("/r", None, lambda t, p: rolled_back.append(t))
    with pytest.raises(NoNodeError):
        server.multi([Create("/r", b"1"), Delete("/does/not/exist")])
    assert rolled_back == []
    assert keeper.exists("/r") is None


@pytest.mark.xfail(
    raises=AssertionError,
    reason=(
        "BUG(minich.keeper):一次**失败**的顺序节点 create 会把自增计数器推进一格,"
        "于是 /log 风格的名字出现空洞。而 _next_sequence 的文档明确承诺 mini-clickhouse 的"
        "编号'严格连续'(好处是 log_pointer 可以直接当'处理到第几条'的下标)。"
    ),
)
def test_failed_sequential_create_does_not_consume_number(keeper: KeeperStorage) -> None:
    keeper.create("/seq")
    with pytest.raises(UnknownSessionError):
        # 临时 + 顺序 + 不存在的 session:这个 create 会失败,但编号已被取走
        keeper.create("/seq/n-", b"", sequential=True, ephemeral=True, session_id=0xBAD)
    assert keeper.create("/seq/n-", b"", sequential=True) == "/seq/n-0000000001"


# ══════════════════════════════════════════════════════════════════════════
# 6. LogEntry 序列化 —— 复制的唯一通信单位
# ══════════════════════════════════════════════════════════════════════════


def test_entry_type_table_matches_labels() -> None:
    """``EntryType`` 是 ClickHouse ``ReplicatedMergeTreeLogEntry::Type`` 的
    忠实复刻,一共 17 种;``label`` / ``from_name`` 都要能对上。"""
    assert len(list(EntryType)) == 17
    assert EntryType.GET_PART.label == "GET_PART"
    assert ENTRY_TYPE_NAMES[int(EntryType.MERGE_PARTS)] == "MERGE_PARTS"
    assert EntryType.from_name("merge_parts") is EntryType.MERGE_PARTS
    assert EntryType.from_name("bogus") is EntryType.EMPTY   # 认不出 → EMPTY


@pytest.mark.parametrize("entry_type", list(EntryType), ids=lambda t: t.name)
def test_log_entry_round_trip_every_type(entry_type: EntryType) -> None:
    """**每一种** ``EntryType`` 都必须能 ``to_bytes`` → ``from_bytes`` 原样回来。

    确定性序列化是硬要求:两个副本要能对同一条 entry 的内容达成一致,
    靠的就是"同一组字段 → 同一串字节"。
    """
    entry = LogEntry(
        entry_type,
        source_replica="replica_a",
        new_part_name="all_1_1_0",
        parts=["all_1_1_0", "all_2_2_0"],
        block_id="block-1",
        quorum=["replica_a", "replica_b"],
        deduplication_block_id="block-1",
        znode_name="log-0000000007",
        alter_version=4,
        column_name="c1",
        partition_id="all",
        from_part="all_1_1_0",
        to_part="all_2_2_0",
        is_detach=True,
        mutation_id="mutation-9",
        mutations_version=5,
        num_tries=3,
    )
    back = LogEntry.from_bytes(entry.to_bytes())
    assert back.type is entry_type
    assert back.to_bytes() == entry.to_bytes()   # 逐字节一致
    assert back == entry                          # __eq__ 也是按字节比
    # 逐个字段确认没有"写进去但读不回来"的字段
    assert back.source_replica == "replica_a"
    assert back.new_part_name == "all_1_1_0"
    assert back.parts == ["all_1_1_0", "all_2_2_0"]
    assert back.quorum == ["replica_a", "replica_b"]
    assert back.is_detach is True
    assert back.mutation_id == "mutation-9"
    assert back.num_tries == 3


def test_log_entry_helper_constructors() -> None:
    """各"构造糖"类方法产出的字段必须对得上 —— 它们是复制路径里真正用到的
    写法(插入 = ``get_part``、合并 = ``merge_parts`` …)。"""
    insert = LogEntry.get_part("all_1_1_0", "replica_a", block_id="b1", partition_id="all")
    assert insert.type is EntryType.GET_PART
    assert insert.parts == ["all_1_1_0"]
    assert insert.is_insert() and not insert.is_merge()

    merge = LogEntry.merge_parts(["a", "b"], "c", "replica_a", block_id="mb")
    assert merge.type is EntryType.MERGE_PARTS
    assert merge.parts == ["a", "b"]
    assert merge.new_part_name == "c"
    assert merge.deduplication_block_id == "mb"   # 合并结果也要参与去重
    assert merge.is_merge()

    drop = LogEntry.drop_part("all_1_1_0", is_detach=True)
    assert drop.type is EntryType.DETACH_PART   # is_detach 会换成 DETACH_PART

    mutate = LogEntry.mutate_part("all_1_1_0", "mutation-7", 5)
    assert mutate.type is EntryType.MUTATE_PART
    assert mutate.mutation_id == "mutation-7"
    assert mutate.mutations_version == 5

    alter = LogEntry.alter_metadata(3, "replica_a")
    assert alter.type is EntryType.ALTER_METADATA
    assert alter.alter_version == 3

    rng = LogEntry.drop_range("all", "all_1_1_0", "all_5_5_0")
    assert rng.type is EntryType.DROP_RANGE
    assert (rng.partition_id, rng.from_part, rng.to_part) == ("all", "all_1_1_0", "all_5_5_0")

    assert LogEntry.attach_part("p").type is EntryType.ATTACH_PART
    assert LogEntry.fetch_part("p", "r").type is EntryType.FETCH_PART
    assert LogEntry.remove_part("p").type is EntryType.REMOVE_PART
    assert LogEntry.update_quorum("p", ["r1", "r2"]).type is EntryType.UPDATE_QUORUM
    assert LogEntry.replace_range("all", "r").type is EntryType.REPLACE_RANGE
    assert LogEntry.clear_column("c", "all").type is EntryType.CLEAR_COLUMN
    assert LogEntry.drop_partition("all").type is EntryType.DROP_PARTITION

    # 构造糖产出的 entry 一样要能被序列化回来
    for entry in (insert, merge, drop, mutate, alter, rng):
        assert LogEntry.from_bytes(entry.to_bytes()) == entry


@pytest.mark.parametrize(
    "payload",
    [
        b"\x00",                     # 多一个尾字节
        b"\x00\x00\x00",             # 多三个
    ],
    ids=["one-trailing", "three-trailing"],
)
def test_log_entry_rejects_trailing_bytes(payload: bytes) -> None:
    """末尾多出字节必须报错 —— "解出来还剩一点"通常意味着版本错配,
    静默忽略会让 bug 藏得很深。"""
    raw = LogEntry.get_part("all_1_1_0", "replica_a").to_bytes() + payload
    with pytest.raises(MiniChError) as excinfo:
        LogEntry.from_bytes(raw)
    assert excinfo.value.code == ErrorCode.CANNOT_READ_ALL_DATA


def test_log_entry_rejects_unknown_format_version() -> None:
    """格式版本对不上 → ``CANNOT_PARSE_TEXT``(将来加字段时靠它做兼容)。"""
    raw = LogEntry.get_part("all_1_1_0", "replica_a").to_bytes()
    bad = bytes([LOG_ENTRY_FORMAT_VERSION + 1]) + raw[1:]
    with pytest.raises(MiniChError) as excinfo:
        LogEntry.from_bytes(bad)
    assert excinfo.value.code == ErrorCode.CANNOT_PARSE_TEXT


def test_log_entry_runtime_fields_are_not_serialized() -> None:
    """``log_index`` / ``exception`` 是**运行时**字段,不进字节流 ——
    它们的值由日志 znode 的位置决定,而不是 entry 内容的一部分。"""
    entry = LogEntry.get_part("all_1_1_0", "replica_a")
    entry.log_index = 42
    entry.exception = "boom"
    back = LogEntry.from_bytes(entry.to_bytes())
    assert back.log_index == 0
    assert back.exception == ""
    assert back == entry            # __eq__ 只看字节 → 运行时字段不影响相等


@pytest.mark.xfail(
    raises=AssertionError,
    reason=(
        "BUG(minich.replication):类文档的 .. note:: 声明 znode_name 是'运行时字段,不参与序列化',"
        "但 to_bytes 实际上把它写进了字节流(且 from_bytes 又读了回来)。文档自相矛盾:"
        "上面的二进制布局图里确实列了 [String znode_name]。"
    ),
)
def test_log_entry_znode_name_should_not_be_serialized() -> None:
    entry = LogEntry.get_part("all_1_1_0", "replica_a")
    entry.znode_name = "log-0000000007"
    back = LogEntry.from_bytes(entry.to_bytes())
    assert back.znode_name == ""    # 按文档:运行时字段,不参与序列化


# ══════════════════════════════════════════════════════════════════════════
# 7. part 与合并选择器
# ══════════════════════════════════════════════════════════════════════════


def test_part_name_make_and_parse() -> None:
    """part 名 ``<partition>_<min>_<max>_<level>`` 是"无中心协调也能合并"的前提:
    不看数据就能判断两个副本指的是不是同一个 part。"""
    assert PartInfo.make_name("all", 1, 2, 1) == "all_1_2_1"
    assert PartInfo.parse_name("all_1_2_1") == ("all", 1, 2, 1)
    with pytest.raises(MiniChError) as excinfo:
        PartInfo.parse_name("not-a-part-name")
    assert excinfo.value.code == ErrorCode.BAD_DATA_PART_NAME


def _replica_with_parts(*parts: PartInfo) -> Replica:
    """造一个只装了给定 part 的 Replica(不启 server,自带一个会话)。"""
    server = KeeperServer()
    paths = ReplicatedTablePaths(server.storage)
    paths.ensure_created()
    store = PartStore("r")
    for part in parts:
        store.add(part)
    return Replica(paths, "r", server.new_session().session_id, part_store=store)


def test_merge_selector_requires_same_level() -> None:
    """合并选择器只挑**同层级**的 part —— 这条限制保证 part 大小按几何级数
    增长,总写放大是 O(n log n) 而不是 O(n²)。"""
    lvl0_a = PartInfo("all_1_1_0", "all", 1, 1, 0, rows=1, size_bytes=100)
    lvl0_b = PartInfo("all_2_2_0", "all", 2, 2, 0, rows=1, size_bytes=100)
    lvl1 = PartInfo("all_3_3_1", "all", 3, 3, 1, rows=1, size_bytes=100)

    # 一个 level-0 + 一个 level-1:两个"组"各只有一个成员 → 没有可合并的
    assert default_merge_selector(_replica_with_parts(lvl0_a, lvl1)) is None

    candidate = default_merge_selector(_replica_with_parts(lvl0_a, lvl0_b, lvl1))
    assert candidate is not None
    assert set(candidate.source_names) == {"all_1_1_0", "all_2_2_0"}


def test_merge_candidate_aggregates() -> None:
    """``MergeCandidate`` 把若干 part 聚合成一个新 part 的名字与统计量。"""
    candidate = MergeCandidate([
        PartInfo("all_1_1_0", "all", 1, 1, 0, rows=1, size_bytes=100),
        PartInfo("all_2_2_0", "all", 2, 2, 0, rows=2, size_bytes=200),
    ])
    assert candidate.new_part_name == "all_1_2_1"   # level = max(0,0)+1
    assert candidate.level == 1
    assert candidate.rows == 3
    assert candidate.size_bytes == 300
    assert (candidate.min_block, candidate.max_block) == (1, 2)


def test_replica_apply_entry_is_idempotent() -> None:
    """同一条 entry 执行两遍 = 执行一遍 —— 副本重启后重放日志的正确性
    全靠这条不变量。"""
    server = KeeperServer()
    paths = ReplicatedTablePaths(server.storage)
    paths.ensure_created()
    store = PartStore("r")
    store.add(PartInfo("all_1_1_0", "all", 1, 1, 0, rows=5))

    def on_fetch_part(replica: Replica, entry: LogEntry) -> bool:
        replica.part_store.add(PartInfo(entry.new_part_name, "all", 1, 1, 0, rows=5))
        return True

    replica = Replica(
        paths, "r", server.new_session().session_id,
        part_store=store, callbacks={"on_fetch_part": on_fetch_part},
    )
    entry = LogEntry.get_part("all_1_1_0", "replica_a")
    assert replica.apply_entry(entry) is True
    assert replica.apply_entry(entry) is True      # 幂等
    assert replica.part_store.names() == ["all_1_1_0"]
    assert replica.part_store.total_rows() == 5    # 没有重复计入
    assert replica.part_states["all_1_1_0"] == PartState.COMMITTED


def test_drop_range_compares_numeric_blocks() -> None:
    """``DROP_RANGE`` 必须按 **block 号** 比较,不能按 part 名字符串比较。

    ``"all_10_10_0" > "all_1_1_0"`` 在字典序下成立,字符串比较会把范围判反
    —— 这是真实 ClickHouse 里被踩过的经典坑。区间判定是**相交**:
    part 的 ``[min,max]`` 与 ``[from,to]`` 有交集就删。
    """
    server = KeeperServer()
    paths = ReplicatedTablePaths(server.storage)
    paths.ensure_created()
    store = PartStore("r")
    for name in ("all_1_1_0", "all_2_2_0", "all_10_10_0", "all_1_5_1"):
        pid, lo, hi, lvl = PartInfo.parse_name(name)
        store.add(PartInfo(name, pid, lo, hi, lvl))

    dropped: list[str] = []

    def on_drop_range(replica: Replica, entry: LogEntry) -> bool:
        dropped.extend(entry.parts)
        for src in entry.parts:
            replica.part_store.remove(src)
        return True

    replica = Replica(
        paths, "r", server.new_session().session_id,
        part_store=store, callbacks={"on_drop_range": on_drop_range},
    )
    assert replica.apply_entry(LogEntry.drop_range("all", "all_1_1_0", "all_2_2_0")) is True
    # 数值区间 [1,2] 不含 all_10_10_0 —— 字符串比较会把它误删
    assert "all_10_10_0" not in dropped
    assert store.has("all_10_10_0")
    # all_1_5_1 的 [1,5] 与 [1,2] 相交 → 删;另外两个正好落在区间里
    assert set(dropped) == {"all_1_1_0", "all_2_2_0", "all_1_5_1"}
    assert store.names() == ["all_10_10_0"]


# ══════════════════════════════════════════════════════════════════════════
# 8. LocalCluster —— 在一个进程里跑 3 个副本
# ══════════════════════════════════════════════════════════════════════════


def test_cluster_has_three_replicas_and_initial_leader(cluster: LocalCluster) -> None:
    """初始状态:3 个副本都活着,选主顺序最小的 ``replica_a`` 是 leader。"""
    assert list(cluster.replicas) == ["replica_a", "replica_b", "replica_c"]
    assert cluster.active_replica_names() == ["replica_a", "replica_b", "replica_c"]
    assert cluster.leader() == "replica_a"
    assert cluster.leaders() == ["replica_a"]   # 最多只有一个人认为自己是 leader


def test_insert_converges_all_replicas(cluster: LocalCluster) -> None:
    """核心场景:副本 A 插入 → B、C 从日志读到这条记录 → 各自把 part 拉过来
    → 三副本状态完全一致。**副本之间从不直接说话。**"""
    result = cluster.insert("replica_a", "all", "block-1", rows=5)
    assert result.duplicate is False
    applied = cluster.process_all()
    assert applied == 3                     # 三个副本各应用了一条日志

    cluster.assert_converged()
    for name in ("replica_a", "replica_b", "replica_c"):
        replica = cluster.replicas[name]
        assert replica.part_store.total_rows() == 5
        assert replica.part_store.names() == ["all_1_1_0"]
        assert replica.log_pointer == 1


def test_insert_result_fields(cluster: LocalCluster) -> None:
    """一次插入的结果里带着去重与 quorum 的**全部**信息。"""
    result = cluster.insert("replica_a", "all", "block-1", rows=3)
    assert isinstance(result, InsertResult)
    assert result.block_id == "block-1"
    assert result.block_number == 1
    assert result.part_name == "all_1_1_0"        # <partition>_<n>_<n>_<level>
    assert result.log_index == 1
    assert result.entry_name == "log-0000000001"
    assert result.quorum_size == 0
    assert result.quorum_satisfied is True        # 没要求 quorum 就算满足


def test_leader_reelection_after_kill(cluster: LocalCluster) -> None:
    """leader 挂掉 → 它的临时顺序节点消失 → 下一个最小的自动上位。

    整个过程**没有任何"选举消息"** —— 选主就是"看谁的第一个到达"。
    """
    cluster.insert("replica_a", "all", "b1")
    cluster.process_all()
    assert cluster.leader() == "replica_a"

    new_leader = cluster.kill_replica("replica_a")
    assert new_leader == "replica_b"
    assert cluster.leader() == "replica_b"
    assert cluster.leaders() == ["replica_b"]
    assert cluster.active_replica_names() == ["replica_b", "replica_c"]


def test_killed_replica_is_readonly(cluster: LocalCluster) -> None:
    """Keeper 会话断了 → 副本转只读,拒绝 INSERT(但 SELECT 还能跑)。"""
    cluster.kill_replica("replica_c")
    replica = cluster.replicas["replica_c"]
    assert replica.is_readonly is True
    assert replica.session_id is None
    with pytest.raises(ReplicaReadOnlyError) as excinfo:
        cluster.insert("replica_c", "all", "x1")
    assert excinfo.value.code == ErrorCode.TABLE_IS_READ_ONLY


def test_dedup_same_block_id(cluster: LocalCluster) -> None:
    """同一个 ``block_id`` 插两次:第二次命中去重,数据**没有**被重复写入。

    去重的粒度是"块"不是"行",所以"同一条 INSERT 重发两次"会被拦住 ——
    这正是重试(客户端超时重发、上游 at-least-once)能变幂等的原因。
    """
    first = cluster.insert("replica_a", "all", "dup-block", rows=4)
    assert first.duplicate is False

    second = cluster.insert("replica_a", "all", "dup-block", rows=4)
    assert second.duplicate is True
    assert second.part_name == ""           # 没建新 part
    assert cluster.paths.block_count() == 1  # 去重标记只有一个

    cluster.process_all()
    cluster.assert_converged()
    assert cluster.replicas["replica_b"].part_store.total_rows() == 4


def test_dedup_raise_on_duplicate(cluster: LocalCluster) -> None:
    """``raise_on_duplicate=True`` 时去重会抛 :class:`DuplicateInsertError`
    (对应老版本 ClickHouse 的 ``DB::Exception: Duplicate data``)。"""
    cluster.insert("replica_a", "all", "dup-block")
    with pytest.raises(DuplicateInsertError) as excinfo:
        cluster.insert("replica_a", "all", "dup-block", raise_on_duplicate=True)
    assert excinfo.value.code == 117          # ErrorCodes::DUPLICATE_DATA
    assert excinfo.value.name == "DUPLICATE_DATA"
    assert excinfo.value.block_id == "dup-block"


def test_dedup_disabled_inserts_twice(cluster: LocalCluster) -> None:
    """``insert_deduplicate=False`` 关掉判定 —— 同一个 block_id 再插一次
    **不算重复**,会真的多出一个 part。"""
    first = cluster.insert("replica_a", "all", "same-id", insert_deduplicate=False)
    second = cluster.insert("replica_a", "all", "same-id", insert_deduplicate=False)
    assert first.duplicate is False
    assert second.duplicate is False
    assert first.part_name != second.part_name
    # 去重标记用 ignore_existing 写,所以同一个 block_id 仍然只有一个标记
    assert cluster.paths.block_count() == 1


def test_dedup_window_prunes_old_blocks(make_cluster) -> None:
    """去重窗口(``replicated_deduplication_window``,默认 100)只记住最近
    N 个 block;更老的被删掉 → "很久以前的重复插入"不再被识别为重复。"""
    cluster = make_cluster(dedup_window=2)
    cluster.insert("replica_a", "all", "b1")
    cluster.insert("replica_a", "all", "b2")
    cluster.insert("replica_a", "all", "b3")
    assert cluster.paths.block_count() == 2      # 只留最近 2 个

    # b1 的标记已被修剪掉 → 再插一次不会被去重
    again = cluster.insert("replica_a", "all", "b1")
    assert again.duplicate is False


def test_revive_replica_replays_and_converges(cluster: LocalCluster) -> None:
    """副本重启后**别无选择**:它的队列指针在临时节点里,会话一断就没了,
    所以只能从日志头重放 —— 靠的正是"执行必须幂等"这条不变量。"""
    cluster.kill_replica("replica_c")            # c 先掉线,错过插入
    cluster.insert("replica_a", "all", "b1", rows=7)
    cluster.process_all()
    assert cluster.replicas["replica_c"].log_pointer == 0
    assert cluster.replicas["replica_c"].part_store.total_rows() == 0

    cluster.revive_replica("replica_c")          # 重开会话 + 从日志重放
    assert cluster.replicas["replica_c"].is_readonly is False
    cluster.process_all()
    cluster.assert_converged()
    assert cluster.replicas["replica_c"].part_store.total_rows() == 7
    assert cluster.replicas["replica_c"].log_pointer == 1
    assert cluster.active_replica_names() == ["replica_a", "replica_b", "replica_c"]


def test_merge_round_reduces_parts_and_converges(cluster: LocalCluster) -> None:
    """leader 挑一组 part → 写一条 MERGE_PARTS 日志 → 所有副本各自执行。
    非 leader 只是**观众**,"为什么合并不会打架"就是因为决策权在一个人手里。"""
    for block in ("b1", "b2", "b3"):
        cluster.insert("replica_a", "all", block, rows=1)
    cluster.process_all()
    assert cluster.replicas["replica_a"].part_store.names() == [
        "all_1_1_0", "all_2_2_0", "all_3_3_0",
    ]

    entry = cluster.merge_round()
    assert entry is not None
    assert entry.type is EntryType.MERGE_PARTS
    assert entry.new_part_name == "all_1_3_1"
    cluster.assert_converged()
    for name in cluster.replicas:
        assert cluster.replicas[name].part_store.names() == ["all_1_3_1"]
        assert cluster.replicas[name].part_store.total_rows() == 3


def test_merge_round_returns_none_when_nothing_to_merge(cluster: LocalCluster) -> None:
    """只有一个 part → 选择器挑不出"同层级的一组" → leader 什么都不写。"""
    cluster.insert("replica_a", "all", "b1")
    cluster.process_all()
    assert cluster.leader() == "replica_a"
    assert cluster.merge_round() is None


def test_non_leader_merge_returns_none(cluster: LocalCluster) -> None:
    """非 leader 发起合并 → ``None``(只有 leader 有决策权)。"""
    cluster.insert("replica_a", "all", "b1")
    cluster.insert("replica_a", "all", "b2")
    cluster.process_all()
    assert cluster.leader() == "replica_a"
    assert cluster.replicas["replica_b"].run_merge_iteration() is None
    assert cluster.replicas["replica_a"].run_merge_iteration() is not None


def test_quorum_insert(cluster: LocalCluster) -> None:
    """``insert_quorum=2``:等至少 2 个副本确认拥有这条 entry 的数据才返回。

    它真正的价值不是"写多份",而是让 INSERT 在客户端重试时也幂等。
    """
    result = cluster.insert("replica_a", "all", "q1", insert_quorum=2)
    assert result.quorum_size == 2
    assert result.quorum_satisfied is True
    assert len(result.quorum_replicas) >= 2
    assert "replica_a" in result.quorum_replicas   # 发起副本自己也算一个成员
    cluster.process_all()
    cluster.assert_converged()


def test_quorum_timeout_raises(cluster: LocalCluster) -> None:
    """凑不够 quorum → ``QuorumTimeoutError``(TIMEOUT_EXCEEDED)。

    这里只等 50ms —— 也就是 quorum 轮询间隔(5ms)的十来倍,足够确定性地
    触发超时,又不会拖慢套件。
    """
    cluster.kill_replica("replica_b")
    cluster.kill_replica("replica_c")
    with pytest.raises(QuorumTimeoutError) as excinfo:
        cluster.insert(
            "replica_a", "all", "q1",
            insert_quorum=3, insert_quorum_timeout_ms=50,
        )
    assert excinfo.value.code == ErrorCode.TIMEOUT_EXCEEDED


def test_status_reflects_pending_queue(cluster: LocalCluster) -> None:
    """``system.replicas`` 的读数(**真的从 Keeper 状态算出来**,不是常量):
    还没处理日志的副本 ``queue_size`` / ``inserts_in_queue`` 不为零。"""
    cluster.insert("replica_a", "all", "b1")
    status = cluster.status_rows()["replica_b"]
    assert status["queue_size"] == 1
    assert status["inserts_in_queue"] == 1
    assert status["merges_in_queue"] == 0
    assert status["log_pointer"] == 0
    assert status["log_max_index"] == 1
    assert status["total_replicas"] == 3
    assert status["active_replicas"] == 3
    assert status["is_readonly"] is False
    assert status["is_leader"] is False       # leader 是 replica_a

    cluster.process_all()
    settled = cluster.status_rows()["replica_b"]
    assert settled["queue_size"] == 0
    assert settled["log_pointer"] == 1


# ── 复制的 xfail ──────────────────────────────────────────────────────────


@pytest.mark.xfail(
    raises=AssertionError,
    reason=(
        "BUG(minich.replication):PartState.OUTDATED 定义了却**从未被使用**。副本从日志头重放时,"
        "会卡在一条 GET_PART 上 —— 那个 part 早已被更大的 part 合并掉了,任何副本都没有它,"
        "于是 on_fetch_part 失败、指针永远停在 0(queue_size 不收敛)。"
        "模块文档专门解释了 OUTDATED 就是用来'认出源 part 已被取代、直接跳过'的。"
    ),
)
def test_replay_superseded_get_part_advances_pointer(cluster: LocalCluster) -> None:
    cluster.insert("replica_a", "all", "b1", rows=1)
    cluster.insert("replica_a", "all", "b2", rows=2)
    cluster.process_all()
    cluster.merge_round()               # all_1_1_0 + all_2_2_0 → all_1_2_1
    cluster.kill_replica("replica_c")

    cluster.revive_replica("replica_c")  # 从日志头(0)重放
    cluster.process_all()
    replica_c = cluster.replicas["replica_c"]
    # 重放完,指针应该追上日志末尾(数据层确实收敛了,但队列没有)
    assert replica_c.log_pointer == cluster.paths.get_max_log_index()


@pytest.mark.xfail(
    raises=AssertionError,
    reason=(
        "BUG(minich.replication):每次重试同一条'找不到 part'的 entry 都会把 lost_part_count +1,"
        "于是一个丢失的 part 会让这个指标一路涨到 2、3、4…。而它的语义(以及 system.replicas 里"
        "的 lost_part_count 列)是'丢失的 part 数',应该只数**不同的** part。"
    ),
)
def test_lost_part_count_counts_distinct_parts(cluster: LocalCluster) -> None:
    # 造一条谁都拿不到的 GET_PART —— "所有副本都没有这个 part"
    ghost = LogEntry.get_part("all_9_9_0", "replica_a", block_id="ghost")
    cluster.paths.append_log_entry(ghost.to_bytes())
    replica_c = cluster.replicas["replica_c"]

    assert replica_c.process_log_entries() == 0
    assert replica_c.process_log_entries() == 0     # 重试一次
    assert replica_c.lost_part_count == 1           # 只丢了一个 part

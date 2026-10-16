"""**注册表 / 配置 / 权限**类系统表。

这一组表回答的是"这个实例**能做什么**":支持哪些类型与函数、有哪些存储
引擎、当前会话的设置是什么值、有哪些集群、以及谁能干什么。

.. code-block:: text

   system.data_type_families ── 类型名(Array/Map/Decimal/…)
   system.table_engines      ── 引擎能力矩阵(有没有主键/TTL/复制)
   system.formats            ── 输入/输出格式(CSV/JSONEachRow/Pretty)
   system.functions          ── 函数清单(标量 + 聚合)
   system.settings           ── **当前会话**每条设置的值与来源
   system.clusters           ── 集群拓扑(分片/副本)
        │
        └─► 权限:settings_profiles / users / roles / quotas /
                   row_policies / grants / current_roles

它们和"结构类"表(:mod:`minich.system.tables`)的区别是:**这里的数据
来自引擎自己的注册表**,不是来自目录。所以它们回答的是"软件的能力边界",
而不是"我存了什么数据"。

.. note::

   mini-clickhouse 的所有注册表都是**进程级单例**(导入即注册),
   所以这里的几张表(``data_type_families`` / ``table_engines`` /
   ``formats`` / ``functions``)在任何上下文里都返回同样的内容。
   这也意味着它们的行是整个进程的"能力清单",与会话无关。
"""

from __future__ import annotations

import datetime as _dt
import os
import platform
import shutil
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .base import SimpleSystemTable, register_system_table, to_text

__all__ = [
    "SystemDataTypeFamilies",
    "SystemTableEngines",
    "SystemFormats",
    "SystemFunctions",
    "SystemSettings",
    "SystemClusters",
    "SystemSettingsProfiles",
    "SystemUsers",
    "SystemRoles",
    "SystemQuotas",
    "SystemRowPolicies",
    "SystemGrants",
    "SystemCurrentRoles",
    "SystemContributors",
    "SystemBuildOptions",
    "SystemTimeZones",
    "SystemTimezones",
    "SystemDiskSpace",
    "SystemDisks",
    "SystemWarnings",
]


# ══════════════════════════════════════════════════════════════════════════
# 类型 / 引擎 / 格式 / 函数 注册表
# ══════════════════════════════════════════════════════════════════════════


class SystemDataTypeFamilies(SimpleSystemTable):
    """``system.data_type_families`` —— 回答"这台服务器认识哪些类型名"。

    类型在 ClickHouse 里是**可扩展的**:每个 ``DataTypeFactory`` 注册一个
    "族名"(``Array``、``Map``、``AggregateFunction``…)。这张表就是那份
    注册表,写给两类人看:

    * **迁移的人** —— "``Object('json')`` 还在不在?"(它已经变成 ``JSON`` 族);
    * **写工具的人** —— "我要生成 DDL,得先知道哪些类型名能解析"。

    ``case_insensitive`` 说明这个族名大小写是否敏感 —— ClickHouse 的
    类型解析是大小写不敏感的(``UINT8`` == ``UInt8``),所以大多数行是 1。

    .. note::

       **``alias_to`` 在 mini-clickhouse 里恒为空**:我们的
       :func:`minich.types.register_data_type` 只记录"族名 → 工厂",
       不记录别名关系(真实 ClickHouse 会把 ``INT`` → ``Int32`` 这类
       别名单独列成一行)。这里如实留空,而不是编一张别名表。
    """

    name = "data_type_families"
    columns = [
        ("name", "String"),
        ("case_insensitive", "UInt8"),
        ("alias_to", "String"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        try:
            from ..types import registered_type_names
        except Exception:  # pragma: no cover
            return
        for name in registered_type_names():
            yield (name, 1, "")


def _storage_features() -> Dict[str, Any]:
    """``引擎名(小写) → StorageFeatures``。

    做法是**扫** :mod:`minich.storages` 包及其子模块的命名空间,找出所有
    ``IStorage`` 子类,读它们的 ``engine_name`` 与 ``features``。
    这样"引擎能力"永远与代码同步 —— 新加一个引擎,这张表自动多一行。

    .. note::

       必须**递归扫子模块**,而不能只看 ``minich.storages`` 的顶层命名空间
       —— 核心引擎 ``MergeTree`` 定义在 ``minich.storages.merge_tree.merge_tree``
       里,顶层是看不到它的。这个细节很典型:凡是"扫包找实现"的代码,
       都必须想清楚"实现在哪一层"。

    真实 ClickHouse 里这些能力是一堆虚函数
    (``supportsTTL`` / ``supportsReplication`` …),我们用一个
    :class:`minich.storages.base.StorageFeatures` 数据类表达。
    """
    out: Dict[str, Any] = {}
    try:
        from ..storages import base as _sb
    except Exception:  # pragma: no cover
        return out
    try:
        import importlib
        import types

        pkg = importlib.import_module("..storages", __package__)
    except Exception:  # pragma: no cover
        return out

    def _scan(namespace: Dict[str, Any]) -> None:
        for obj in namespace.values():
            if not isinstance(obj, type):
                continue
            try:
                if not issubclass(obj, _sb.IStorage):
                    continue
            except TypeError:  # pragma: no cover
                continue
            engine_name = getattr(obj, "engine_name", None)
            if not engine_name or engine_name == "Unknown":
                continue
            # 每个引擎名只记第一次出现的那个类(同名子类以先到者为准)
            out.setdefault(engine_name.lower(), getattr(obj, "features", None))

    def _is_submodule(mod: Any, root: str) -> bool:
        return isinstance(mod, types.ModuleType) and getattr(mod, "__name__", "").startswith(root)

    root = getattr(pkg, "__name__", "minich.storages")
    _scan(vars(pkg))
    # 两层子模块就够覆盖 minich.storages.<pkg>.<module>(如 merge_tree.merge_tree)
    for sub in vars(pkg).values():
        if not _is_submodule(sub, root):
            continue
        _scan(vars(sub))
        for sub2 in vars(sub).values():
            if _is_submodule(sub2, root):
                _scan(vars(sub2))
    # ── 兜底:别名引擎(如 ReplicatedReplacingMergeTree)没有自己的类,
    #    它注册时复用了另一个类的工厂。从工厂闭包里把那个类捞回来,
    #    这样别名也能报出**真实**的能力,而不是一片 0。──
    _scan_factory_registry(out, _sb)
    return out


def _scan_factory_registry(out: Dict[str, Any], storage_base: Any) -> None:
    """对"还没找到类"的引擎名,试着从注册的工厂闭包里恢复它的类。

    :func:`minich.storages.base.register_storage_engine` 的注册表里存的是
    工厂函数;有的工厂是 :func:`functools.partial`,更多是一个**闭包**,
    把类捕获在 ``__closure__`` 里(如 ``minich.storages.merge_tree.engines._make_factory``)。
    从这里取回类,是为了让"别名引擎"也能报出真实能力 —— 这是**取回**
    已有事实,不是猜测。
    """
    registry = getattr(storage_base, "_REGISTRY", None)
    if not isinstance(registry, dict):
        return
    istorage = getattr(storage_base, "IStorage", None)
    for name, factory in registry.items():
        if name in out:
            continue
        cls = _class_from_factory(factory, istorage)
        if cls is not None:
            out[name] = getattr(cls, "features", None)


def _class_from_factory(factory: Any, istorage: Any) -> Any:
    """从一个引擎工厂里把背后的 ``IStorage`` 子类捞出来(捞不到返回 ``None``)。"""
    def _ok(obj: Any) -> bool:
        if not isinstance(obj, type) or istorage is None:
            return False
        try:
            return issubclass(obj, istorage)
        except TypeError:  # pragma: no cover
            return False

    for cell in getattr(factory, "__closure__", None) or ():
        try:
            value = cell.cell_contents
        except ValueError:  # pragma: no cover
            continue
        if _ok(value):
            return value
    wrapped = getattr(factory, "__wrapped__", None)
    if _ok(wrapped):
        return wrapped
    return None


class SystemTableEngines(SimpleSystemTable):
    """``system.table_engines`` —— 回答"哪些引擎能排序、能 TTL、能复制"。

    .. code-block:: sql

       -- 找出所有支持 TTL 的引擎
       SELECT name FROM system.table_engines WHERE supports_ttl;

    它的价值在于**用数据代替记忆**:分布式系统的运维手册里经常写
    "用 MergeTree 才能 TTL",而这台服务器上一查就知道
    ``Log``/``TinyLog`` 确实不支持 —— 不用去翻文档。

    .. note::

       ``supports_deduplication`` 与 ``supports_parallel_insert`` 在
       mini-clickhouse 里**恒为 0**:我们的 ``StorageFeatures`` 只建模了
       主键/分区/排序键/复制/合并能力,没有建模"去重"与"并行插入"。
       与其按引擎名猜(比如"名字里有 Replacing 就当它支持去重"),
       不如如实报 0 —— 猜出来的能力矩阵比没有能力矩阵更危险。

       ``supports_replication`` 除了看 ``StorageFeatures.replication``,
       还会认"引擎名以 ``Replicated`` 开头"这一条 —— 那是**定义性**的事
        (名字里带 Replicated 的引擎就是复制引擎),不是猜测。
    """

    name = "table_engines"
    columns = [
        ("name", "String"),
        ("supports_sort_order", "UInt8"),
        ("supports_ttl", "UInt8"),
        ("supports_replication", "UInt8"),
        ("supports_deduplication", "UInt8"),
        ("supports_parallel_insert", "UInt8"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        try:
            from ..storages.base import registered_storage_engines
        except Exception:  # pragma: no cover
            return
        features = _storage_features()
        seen = set()
        for name, _deprecated, _desc in registered_storage_engines():
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            feat = features.get(key)
            replicated = bool(getattr(feat, "replication", False)) or \
                name.startswith("Replicated")
            yield (
                name,
                int(bool(getattr(feat, "sorting_key", False))),
                int(bool(getattr(feat, "ttl", False))),
                int(replicated),
                0,   # supports_deduplication —— 未建模,如实报 0
                0,   # supports_parallel_insert —— 未建模,如实报 0
            )


class SystemFormats(SimpleSystemTable):
    """``system.formats`` —— 回答"这个格式能读还是能写"。

    ``clickhouse-client`` 的 ``FORMAT`` 参数支持几十种格式,但**不是每种
    都能双向使用**:``Values`` 只能读、``Pretty`` 只能写。这张表说清了
    每个格式的方向 —— 所以 "``INSERT ... FORMAT Pretty``" 会失败这件事
    是可以提前查到的。

    .. note::

       只列**正名**,不列别名 —— 与真实 ClickHouse 的 ``system.formats``
       一致(别名不单独成行)。
    """

    name = "formats"
    columns = [
        ("name", "String"),
        ("is_input", "UInt8"),
        ("is_output", "UInt8"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        try:
            from ..formats import registered_formats
        except Exception:  # pragma: no cover - 格式子系统缺失时降级
            return
        for name, can_read, can_write, _desc in registered_formats():
            yield (name, int(bool(can_read)), int(bool(can_write)))


class SystemFunctions(SimpleSystemTable):
    """``system.functions`` —— 回答"这个函数是标量还是聚合、有没有别名"。

    这张表把**两个原本分离的注册表**合到一起看,这很重要:

    .. code-block:: text

       minich.functions.base.registered_functions()      ← 标量:plus / length / toString
       minich.aggregates.base.registered_aggregate_functions() ← 聚合:sum / uniq / quantile

    它们在源码里是两套东西(连目录都不同),但对用户来说是"函数"这一个
    概念。``is_aggregate`` 就是那面镜子 —— 它告诉你"这个名字能不能放进
    ``GROUP BY`` 的 SELECT 里"。

    ``alias_to`` 让"``SUM`` 到底调的是谁"变得可查:我们的函数名统一
    小写存放,查询时也小写化,所以别名关系是**注册表里真实存在的**,
    不是猜的。

    .. note::

       * ``case_insensitive`` 恒为 1 —— mini-clickhouse 的函数名一律
         大小写不敏感(见 :func:`minich.functions.base.register_function`);
       * ``categories`` 是 ``Array(String)``。标量函数目前只有一个笼统的
         分类(``Regular``);聚合函数的注册表**不带分类信息**,所以那里
         是空数组 —— 我们没有编一套分类出来。
    """

    name = "functions"
    columns = [
        ("name", "String"),
        ("is_aggregate", "UInt8"),
        ("case_insensitive", "UInt8"),
        ("alias_to", "String"),
        ("categories", "Array(String)"),
        ("description", "String"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        seen: Dict[str, Tuple[Any, ...]] = {}
        # ── 标量 ──
        try:
            from ..functions.base import function_aliases, registered_functions

            aliases = function_aliases()
            for name, category, description in registered_functions():
                cats = [category] if category else []
                seen[name] = (
                    name, 0, 1, aliases.get(name, ""), cats, description or "",
                )
        except Exception:  # pragma: no cover - 函数子系统未加载时降级
            pass
        # ── 聚合(同名的以聚合为准 —— 一个名字不能既是标量又是聚合)──
        try:
            from ..aggregates import registered_aggregate_functions

            for name, description in registered_aggregate_functions():
                seen[name] = (
                    name, 1, 1, "", [], description or "",
                )
        except Exception:  # pragma: no cover
            pass
        for name in sorted(seen):
            yield seen[name]


# ══════════════════════════════════════════════════════════════════════════
# system.settings
# ══════════════════════════════════════════════════════════════════════════


class SystemSettings(SimpleSystemTable):
    """``system.settings`` —— 回答"**这个会话**的每条设置现在是多少"。

    这是"为什么我的查询行为和文档不一样"的**第一现场**:

    .. code-block:: sql

       SELECT name, value, changed FROM system.settings WHERE changed = 1;

    ``changed = 1`` 的行就是"相对内置默认被改过的"。它之所以这么有用,
    是因为 ClickHouse 的设置是**分层覆盖**的(查询 > 会话 > profile >
    server > 默认,见 :mod:`minich.settings`),光看配置文件**永远**不知道
    某条查询最终用的是哪个值 —— 必须问当前的上下文。

    .. note::

       数据来自 ``ctx.settings.as_rows()``,所以它反映的是**当前会话的
       有效值**(不是默认值)。这一条是刻意的:系统表存在的意义就是
       "让我看到实际在生效的东西"。

       列顺序与真实 ClickHouse 一致(``type`` 夹在 ``readonly`` 与
       ``default`` 之间)。``min`` / ``max`` 用 ``String`` + 空串表示
       "无约束"(真实 CH 是 ``Nullable(String)``);``value`` / ``default``
       一律渲染成文本(布尔 → ``0``/``1``)。
    """

    name = "settings"
    columns = [
        ("name", "String"),
        ("value", "String"),
        ("changed", "UInt8"),
        ("description", "String"),
        ("min", "String"),
        ("max", "String"),
        ("readonly", "UInt8"),
        ("type", "String"),
        ("default", "String"),
        ("is_obsolete", "UInt8"),
        ("tier", "String"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        settings = getattr(ctx, "settings", None)
        if settings is None:
            return
        for row in settings.as_rows():
            # as_rows 的列顺序:
            #   name, value, changed, description, min, max, type,
            #   default, readonly, is_obsolete, tier
            yield (
                row[0],
                to_text(row[1]),
                int(row[2]),
                row[3],
                to_text(row[4]),
                to_text(row[5]),
                int(row[8]),        # readonly
                row[6],             # type
                to_text(row[7]),    # default
                int(row[9]),        # is_obsolete
                row[10],            # tier
            )


# ══════════════════════════════════════════════════════════════════════════
# system.clusters
# ══════════════════════════════════════════════════════════════════════════


class SystemClusters(SimpleSystemTable):
    """``system.clusters`` —— 回答"集群里有哪些分片、每个分片有哪些副本"。

    分布式查询的所有行为都从这张表出发:

    .. code-block:: sql

       SELECT cluster, shard_num, replica_num, host_name, port, is_local
       FROM system.clusters ORDER BY cluster, shard_num, replica_num;

    最常用的一个用途是**验证拓扑**:``is_local = 1`` 的那一行就是本机,
    如果它不在你期望的分片里,说明 ``remote_servers`` 配错了。

    .. note::

       数据来自 ``ctx.clusters``。mini-clickhouse 的 ``Context`` 默认
       ``clusters`` 为空字典(集群模块是可选的、由外部注入),所以
       在没配集群的实例上这张表**为空** —— 这是正确答案,不是 bug。
       我们只做"尽力映射":集群对象既可能是 dict、也可能是带
       ``shards`` 属性的对象,两种都能读出来。
    """

    name = "clusters"
    columns = [
        ("cluster", "String"),
        ("shard_num", "UInt32"),
        ("replica_num", "UInt32"),
        ("host_name", "String"),
        ("port", "UInt32"),
        ("is_local", "UInt8"),
        ("user", "String"),
        ("errors_count", "UInt32"),
        ("estimated_recovery_time", "UInt32"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        clusters = getattr(ctx, "clusters", None) or {}
        if not isinstance(clusters, dict):
            return
        for cluster_name in sorted(clusters):
            for row in _cluster_rows(cluster_name, clusters[cluster_name]):
                yield row


def _cluster_rows(cluster_name: str, cluster: Any) -> List[Tuple[Any, ...]]:
    """把一个集群定义摊平成"每副本一行"。

    支持两种形状(与常见的配置对象约定一致):

    .. code-block:: text

       {"shards": [ {"replicas": [ {"host": "a", "port": 9000, "user": "default"} ]} ]}
       Cluster(shards=[ Shard(replicas=[ Replica(host="a", ...) ]) ])

    读不出来的部分填默认值,而不是报错 —— 系统表**永远**不该把
    一条"看拓扑"的查询变成异常。
    """
    shards = _get(cluster, "shards", None)
    if shards is None:
        return []
    out: List[Tuple[Any, ...]] = []
    for si, shard in enumerate(shards, start=1):
        shard_num = int(_get(shard, "shard_num", si) or si)
        replicas = _get(shard, "replicas", None)
        if replicas is None:
            replicas = [shard]
        for ri, replica in enumerate(replicas, start=1):
            replica_num = int(_get(replica, "replica_num", ri) or ri)
            out.append(
                (
                    cluster_name,
                    shard_num,
                    replica_num,
                    str(_get(replica, "host", _get(replica, "host_name", "")) or ""),
                    int(_get(replica, "port", 0) or 0),
                    int(bool(_get(replica, "is_local", False))),
                    str(_get(replica, "user", "default") or "default"),
                    int(_get(replica, "errors_count", 0) or 0),
                    int(_get(replica, "estimated_recovery_time", 0) or 0),
                )
            )
    return out


def _get(obj: Any, key: str, default: Any) -> Any:
    """从 dict 或对象里取一个字段(读不到返回 ``default``)。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# ══════════════════════════════════════════════════════════════════════════
# 权限 —— 数据来自 ctx.access(可选子系统)
# ══════════════════════════════════════════════════════════════════════════


class _AccessTable(SimpleSystemTable):
    """权限类系统表的共同底座 —— **只是把"缺子系统 ⇒ 空表"这件事讲清楚**。

    ``ctx.access`` 是访问控制子系统的入口(对应真实 ClickHouse 的
    ``AccessControl``)。mini-clickhouse 里它是可选的:没有它,所有
    权限相关的系统表就**空着** —— 因为"没有配置任何用户/角色"本身
    就是一个合法的、可解释的状态(单用户教学实例)。

    子类只要声明:

    * ``access_attr`` —— 从 ``ctx.access`` 上读哪个集合(如 ``"users"``);
    * ``columns`` —— 表结构;
    * ``map_item(item)`` —— 把集合里的一个元素映射成一行。
    """

    #: ``ctx.access`` 上集合的属性名。
    access_attr: str = ""

    def _items(self, ctx: Any) -> List[Any]:
        access = getattr(ctx, "access", None)
        if access is None:
            return []
        collection = getattr(access, self.access_attr, None)
        if collection is None:
            return []
        if isinstance(collection, dict):
            return list(collection.values())
        try:
            return list(collection)
        except TypeError:  # pragma: no cover
            return []

    def map_item(self, item: Any) -> Tuple[Any, ...]:  # pragma: no cover - 抽象
        raise NotImplementedError

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        for item in self._items(ctx):
            yield self.map_item(item)


class SystemSettingsProfiles(_AccessTable):
    """``system.settings_profiles`` —— 回答"有哪些设置档(profile)、各自几条设置"。

    设置档是"给一类用户预设一批设置"的机制(比如 ``readonly`` 档给分析
    同学只读权限)。生产里排查"为什么这个用户的查询超时时间不一样",
    答案通常在这里。

    .. note::

       从 ``ctx.access.settings_profiles`` 读;访问控制子系统缺席时为空。
    """

    name = "settings_profiles"
    access_attr = "settings_profiles"
    columns = [
        ("name", "String"),
        ("id", "UUID"),
        ("storage", "String"),
        ("num_elements", "UInt64"),
        ("apply_to_all", "UInt8"),
        ("apply_to_list", "Array(String)"),
        ("apply_to_except", "Array(String)"),
    ]

    def map_item(self, item: Any) -> Tuple[Any, ...]:
        name = str(_get(item, "name", "") or "")
        elements = _get(item, "elements", _get(item, "settings", {})) or {}
        try:
            num = len(elements)
        except TypeError:  # pragma: no cover
            num = 0
        return (
            name,
            str(_get(item, "id", "") or ""),
            str(_get(item, "storage", "memory") or "memory"),
            int(_get(item, "num_elements", num) or 0),
            int(bool(_get(item, "apply_to_all", True))),
            list(_get(item, "apply_to_list", []) or []),
            list(_get(item, "apply_to_except", []) or []),
        )


class SystemUsers(_AccessTable):
    """``system.users`` —— 回答"有哪些用户、各自的认证与默认角色"。

    .. code-block:: sql

       SELECT name, auth_type, default_roles_all, default_roles_list
       FROM system.users;

    ``default_roles_all = 1`` 是很常见的"图省事"配置(所有角色都给),
    但它也是权限过大最常见的来源 —— 值得定期查一遍。

    .. note::

       从 ``ctx.access.users`` 读;子系统缺席时为空。真实 ClickHouse 的
       ``system.users`` 有几十列(还内嵌了一份 grant 信息),这里只保留
       认证与角色相关的核心列。
    """

    name = "users"
    access_attr = "users"
    columns = [
        ("name", "String"),
        ("id", "UUID"),
        ("storage", "String"),
        ("auth_type", "String"),
        ("host_ip", "String"),
        ("host_names", "Array(String)"),
        ("default_roles_all", "UInt8"),
        ("default_roles_list", "Array(String)"),
        ("default_roles_except", "Array(String)"),
        ("grantees", "Array(String)"),
    ]

    def map_item(self, item: Any) -> Tuple[Any, ...]:
        return (
            str(_get(item, "name", "") or ""),
            str(_get(item, "id", "") or ""),
            str(_get(item, "storage", "memory") or "memory"),
            str(_get(item, "auth_type", "") or ""),
            str(_get(item, "host_ip", "") or ""),
            list(_get(item, "host_names", []) or []),
            int(bool(_get(item, "default_roles_all", False))),
            list(_get(item, "default_roles_list", []) or []),
            list(_get(item, "default_roles_except", []) or []),
            list(_get(item, "grantees", []) or []),
        )


class SystemRoles(_AccessTable):
    """``system.roles`` —— 回答"定义了哪些角色"。

    角色是权限的"打包单位"。回答"权限是从哪来的"时,第一站就是看
    这个用户身上挂了哪些角色 —— 而角色清单在这里。

    .. note::

       从 ``ctx.access.roles`` 读;子系统缺席时为空。
    """

    name = "roles"
    access_attr = "roles"
    columns = [
        ("name", "String"),
        ("id", "UUID"),
        ("storage", "String"),
    ]

    def map_item(self, item: Any) -> Tuple[Any, ...]:
        return (
            str(_get(item, "name", "") or ""),
            str(_get(item, "id", "") or ""),
            str(_get(item, "storage", "memory") or "memory"),
        )


class SystemQuotas(_AccessTable):
    """``system.quotas`` —— 回答"有哪些配额、限的是什么"。

    配额是"每个用户/角色每小时能跑多少查询、读多少行"的硬闸门。
    "我的查询被拒了但报错很奇怪"经常是配额打满了 —— 先看这张表。

    .. note::

       从 ``ctx.access.quotas`` 读;子系统缺席时为空。
    """

    name = "quotas"
    access_attr = "quotas"
    columns = [
        ("name", "String"),
        ("id", "UUID"),
        ("storage", "String"),
        ("keys", "Array(String)"),
        ("values", "Array(String)"),
        ("is_keyed", "UInt8"),
    ]

    def map_item(self, item: Any) -> Tuple[Any, ...]:
        intervals = _get(item, "intervals", _get(item, "limits", [])) or []
        keys: List[str] = []
        values: List[str] = []
        for interval in intervals:
            keys.append(str(_get(interval, "duration", "") or ""))
            values.append(str(_get(interval, "queries", "") or ""))
        return (
            str(_get(item, "name", "") or ""),
            str(_get(item, "id", "") or ""),
            str(_get(item, "storage", "memory") or "memory"),
            keys,
            values,
            int(bool(_get(item, "is_keyed", False))),
        )


class SystemRowPolicies(_AccessTable):
    """``system.row_policies`` —— 回答"哪些表被行级策略过滤了"。

    行策略(``CREATE ROW POLICY``)是"同一个表,不同用户看到不同行"的机制。
    它最容易踩的坑是**无意中把数据藏了** —— 所以排查"数据对不上"
    时一定要查这张表。

    .. note::

       从 ``ctx.access.row_policies`` 读;子系统缺席时为空。
    """

    name = "row_policies"
    access_attr = "row_policies"
    columns = [
        ("name", "String"),
        ("short_name", "String"),
        ("database", "String"),
        ("table", "String"),
        ("id", "UUID"),
        ("storage", "String"),
        ("filter", "String"),
        ("is_restrictive", "UInt8"),
    ]

    def map_item(self, item: Any) -> Tuple[Any, ...]:
        name = str(_get(item, "name", "") or "")
        return (
            name,
            name.split("_ON_")[0] if name else "",
            str(_get(item, "database", "") or ""),
            str(_get(item, "table", "") or ""),
            str(_get(item, "id", "") or ""),
            str(_get(item, "storage", "memory") or "memory"),
            str(_get(item, "filter", "") or ""),
            int(bool(_get(item, "is_restrictive", False))),
        )


class SystemGrants(_AccessTable):
    """``system.grants`` —— 回答"谁被授予了什么权限"。

    这是审计视角的核心表:

    .. code-block:: sql

       SELECT user_name, access_type, database, table
       FROM system.grants WHERE database = 'finance';

    一句话就能回答"谁能看 ``finance`` 库"。

    .. note::

       从 ``ctx.access.grants`` 读;子系统缺席时为空。
    """

    name = "grants"
    access_attr = "grants"
    columns = [
        ("user_name", "String"),
        ("role_name", "String"),
        ("access_type", "String"),
        ("database", "String"),
        ("table", "String"),
        ("column", "String"),
        ("is_partial_revoke", "UInt8"),
        ("grant_option", "UInt8"),
    ]

    def map_item(self, item: Any) -> Tuple[Any, ...]:
        return (
            str(_get(item, "user_name", "") or ""),
            str(_get(item, "role_name", "") or ""),
            str(_get(item, "access_type", "") or ""),
            str(_get(item, "database", "") or ""),
            str(_get(item, "table", "") or ""),
            str(_get(item, "column", "") or ""),
            int(bool(_get(item, "is_partial_revoke", False))),
            int(bool(_get(item, "grant_option", False))),
        )


class SystemCurrentRoles(_AccessTable):
    """``system.current_roles`` —— 回答"**当前会话**激活了哪些角色"。

    用户可能被授予了 10 个角色,但当前只 ``SET ROLE`` 激活了 2 个。
    排查"我明明有这个权限,为什么还是 Access denied"时,
    答案十有八九是**角色没激活** —— 而这张表就是那个答案。

    .. note::

       从 ``ctx.access.current_roles`` 读;子系统缺席时为空。
    """

    name = "current_roles"
    access_attr = "current_roles"
    columns = [("role_name", "String")]

    def map_item(self, item: Any) -> Tuple[Any, ...]:
        if isinstance(item, str):
            return (item,)
        return (str(_get(item, "name", _get(item, "role_name", "")) or ""),)


# ══════════════════════════════════════════════════════════════════════════
# 构建信息 / 时区 / 磁盘 / 警告
# ══════════════════════════════════════════════════════════════════════════


class SystemContributors(SimpleSystemTable):
    """``system.contributors`` —— 回答"这份构建是谁贡献的"。

    真实 ClickHouse 在**构建时**从 git 历史生成这张表,里面有**几百行**
    贡献者名字(``system.contributors`` 是一个"彩蛋表",很多人第一次
    ``SELECT *`` 出来会愣一下)。

    .. note::

       **mini-clickhouse 不伪造这份名单,所以这张表为空。** 我们没法
       "构建时从 git 历史生成"(那是上游发布流程做的事),而在源码里
       硬编码一串人名既不可维护、也不诚实。schema 保留,是为了让
       ``SELECT *`` 不报错、并把这个差异讲清楚。
    """

    name = "contributors"
    columns = [("name", "String")]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        return iter(())


class SystemBuildOptions(SimpleSystemTable):
    """``system.build_options`` —— 回答"这台服务器是怎么编译出来的"。

    它存在的意义是**把"构建差异"变成可查询的数据**:同一个版本
    在不同发行版/不同编译选项下行为可能不同(比如是否链接了某个
    压缩库),而运维需要知道手上这个二进制的确切身份。

    .. note::

       **真实 ClickHouse 会输出约 100 行构建标志**(``CMAKE_BUILD_TYPE``、
       ``USE_SSL``、各种 ``ENABLE_*``…)。mini-clickhouse 是纯 Python,
       没有编译期标志可言,所以只如实报告:版本、对标的 ClickHouse 版本、
       Python 版本、解释器类型、平台。
    """

    name = "build_options"
    columns = [
        ("name", "String"),
        ("value", "String"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        try:
            from .. import TARGET_CLICKHOUSE_VERSION, __version__
        except Exception:  # pragma: no cover
            __version__, TARGET_CLICKHOUSE_VERSION = "0.0.0", "unknown"
        yield ("VERSION", str(__version__))
        yield ("TARGET_CLICKHOUSE_VERSION", str(TARGET_CLICKHOUSE_VERSION))
        yield ("PYTHON_VERSION", platform.python_version())
        yield ("PYTHON_IMPLEMENTATION", platform.python_implementation())
        yield ("PLATFORM", platform.platform())
        yield ("ENGINE", "pure-python")


class SystemTimeZones(SimpleSystemTable):
    """``system.time_zones`` —— 回答"这台服务器认识哪些时区名"。

    ``DateTime('Asia/Shanghai')`` 里的那个 ``'Asia/Shanghai'`` 必须是
    一个认识的时区名。这张表就是那份名单 —— 写 DDL 前先查一下,
    免得拿到 ``Unknown timezone``。

    .. note::

       数据来自 Python 标准库的 ``zoneinfo.available_timezones()`` ——
       是**真实的 IANA 时区表**,不是编的。但它在一些平台上需要
       ``tzdata`` 包(Windows 尤其如此):**拿不到时区数据时这张表为空**,
       而不是报错。注意 :class:`minich.types.scalars.DataTypeDateTime`
       只支持 UTC 与固定偏移,不查 IANA 库 —— 也就是说这里列出的名字
       在 mini-clickhouse 里**未必**真的能用。这个"表比实现更大"的
       差异是刻意保留的,教程会拿它讲"不要把系统表当成实现能力的承诺"。
    """

    name = "time_zones"
    columns = [("name", "String")]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        for name in _available_timezones():
            yield (name,)


class SystemTimezones(SimpleSystemTable):
    """``system.timezones`` —— 回答"每个时区**此刻**相对 UTC 差多少秒"。

    与 :class:`SystemTimeZones` 的区别:那张表只有名字,这张表带 ``offset``。

    .. note::

       ``offset`` 是**查询这一刻**的偏移 —— 对有夏令时的时区,它在
       一年里会变两次。真实 ClickHouse 的这张表也是这个语义。时区数据
       不可用(见 :class:`SystemTimeZones`)时本表为空。
    """

    name = "timezones"
    columns = [
        ("name", "String"),
        ("offset", "Int64"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        try:
            from zoneinfo import ZoneInfo
        except Exception:  # pragma: no cover
            return
        now = _dt.datetime.now()
        for name in _available_timezones():
            try:
                delta = now.astimezone(ZoneInfo(name)).utcoffset()
            except Exception:  # pragma: no cover - 个别名字可能加载失败
                continue
            yield (name, int(delta.total_seconds()) if delta is not None else 0)


def _available_timezones() -> List[str]:
    """可用的 IANA 时区名(排序);拿不到就返回空列表。"""
    try:
        from zoneinfo import available_timezones
    except Exception:  # pragma: no cover
        return []
    try:
        return sorted(available_timezones())
    except Exception:  # pragma: no cover - 缺 tzdata 时可能抛
        return []


class SystemDiskSpace(SimpleSystemTable):
    """``system.disk_space`` —— 回答"每个磁盘还剩多少空间"。

    "``Cannot reserve 8.00 MiB, not enough space``" 是 ClickHouse 最疼的
    报错之一(它意味着后台合并无法进行,插入会开始失败)。这张表让你
    **提前**看到磁盘在逼近上限:

    .. code-block:: sql

       SELECT name, path, free_space, total_space
       FROM system.disk_space;

    .. note::

       mini-clickhouse 只声明**一个**磁盘(名为 ``default``),路径取自
       ``ctx.data_path``(为 ``None`` 时退化成当前工作目录)。数值来自
       ``shutil.disk_usage`` —— 是**真实的**文件系统空闲/总量。

       想看"这台服务器挂了哪几块盘、各自是什么类型",用
       :class:`SystemDisks`(``system.disks``);这张表只报空间读数。
    """

    name = "disk_space"
    columns = [
        ("name", "String"),
        ("path", "String"),
        ("free_space", "UInt64"),
        ("total_space", "UInt64"),
        ("unreserved_space", "UInt64"),
        ("keep_free_space", "UInt64"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        path = getattr(ctx, "data_path", None) or os.getcwd()
        try:
            usage = shutil.disk_usage(path)
        except OSError:  # pragma: no cover - 路径不存在时降级
            return
        yield (
            "default",
            str(path),
            int(usage.free),
            int(usage.total),
            int(usage.free),
            0,
        )


class SystemDisks(SimpleSystemTable):
    """``system.disks`` —— 回答"这台服务器挂了哪几块盘、各自还剩多少"。

    和 :class:`SystemDiskSpace` 的区别值得说清楚:

    .. code-block:: text

       system.disks        服务器**配置了几块盘**(静态拓扑:名字/路径/类型)
       system.disk_space   每块盘**此刻**的剩余空间(动态读数)

    真实 ClickHouse 里这张表是**存储策略**(storage policy)的入口:
    "冷数据下沉到 S3、热数据留在本地 SSD"这套玩法,就是在这里定义
    ``default`` / ``cold`` / ``hot`` 几块盘,再用
    ``CREATE TABLE ... SETTINGS storage_policy = 'hot_cold'`` 引用的。

    .. code-block:: sql

       SELECT name, path, type, free_space, total_space FROM system.disks;

    .. note::

       数据来自 ``ctx.disk``(一个 :class:`minich.storages.merge_tree.disk.DiskLocal`):
       名字取 ``disk.name``,路径取 ``disk.get_root_path()``,空间数字用
       ``shutil.disk_usage(root)`` 实测 —— 都是**真实读数**。

       **``ctx.data_path`` 为 ``None`` 时**(纯内存实例)仍然会有**一行**:
       它描述的是"那个隐式/进程内的盘",路径退化成当前工作目录。
       如实说明:mini-clickhouse **没有存储策略层** ——
       只有一块本地盘,``type`` 恒为 ``local``,``is_encrypted`` /
       ``is_read_only`` 恒为 0(我们的 ``DiskLocal`` 既不加密,也可写)。
       这些列保留是为了与真实 ClickHouse 的 schema 对齐。
    """

    name = "disks"
    columns = [
        ("name", "String"),
        ("path", "String"),
        ("free_space", "UInt64"),
        ("total_space", "UInt64"),
        ("unreserved_space", "UInt64"),
        ("keep_free_space", "UInt64"),
        ("type", "String"),
        ("is_encrypted", "UInt8"),
        ("is_read_only", "UInt8"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        disk = getattr(ctx, "disk", None)
        data_path = getattr(ctx, "data_path", None)
        if disk is not None:
            name = str(getattr(disk, "name", "") or "default")
            path = _disk_root(disk) or data_path or os.getcwd()
        else:
            # 没有显式盘 —— 仍然是"一行",描述那个隐式/进程内的盘。
            name = "default"
            path = data_path or os.getcwd()
        free, total = _disk_usage(path)
        yield (
            name,
            str(path),
            free,
            total,
            free,      # unreserved_space = free - keep_free_space(keep 恒为 0)
            0,         # keep_free_space
            "local",   # type —— mini-clickhouse 只有本地盘
            0,         # is_encrypted
            0,         # is_read_only
        )


def _disk_root(disk: Any) -> Optional[str]:
    """取磁盘的根路径;盘对象不支持 ``get_root_path()`` 时返回 ``None``。"""
    fn = getattr(disk, "get_root_path", None)
    if not callable(fn):
        return None
    try:
        return fn()
    except Exception:  # pragma: no cover
        return None


def _disk_usage(path: Any) -> Tuple[int, int]:
    """``(free, total)`` 字节数;路径不可用时返回 ``(0, 0)``。"""
    try:
        usage = shutil.disk_usage(str(path))
    except OSError:  # pragma: no cover - 路径不存在时降级
        return 0, 0
    return int(usage.free), int(usage.total)


class SystemWarnings(SimpleSystemTable):
    """``system.warnings`` —— 回答"服务器现在有没有什么不对劲"。

    真实 ClickHouse 会把"值得你立刻知道"的事集中到这一张表里:
    某个表 part 数逼近上限、某个后台池被挤占、副本落后…… 很多运维
    把它接进告警:

    .. code-block:: sql

       SELECT * FROM system.warnings;

    .. note::

       我们只计算**能诚实算出来的**那一条:某张表的活跃 part 数超过
       ``parts_to_throw_insert``(默认 3000,可用 ``SET`` 改)—— 那正是
       真实 ClickHouse 触发 "Too many parts" 的阈值,所以这是一条
       **真警告**,不是凑数。算不出来的其它警告类型一律不报;
       引擎不暴露 ``part_count()`` 时这张表为空。
    """

    name = "warnings"
    columns = [
        ("message", "String"),
        ("message_format_string", "String"),
        ("value", "String"),
        ("last_warning_time", "DateTime"),
        ("last_warning_message", "String"),
    ]

    def rows(self, ctx: Any) -> Iterable[Tuple[Any, ...]]:
        catalog = getattr(ctx, "catalog", None)
        if catalog is None:
            return
        settings = getattr(ctx, "settings", None)
        threshold = 3000
        if settings is not None:
            try:
                threshold = int(settings.try_get("parts_to_throw_insert", 3000))
            except Exception:  # pragma: no cover
                threshold = 3000
        now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for db_name in catalog.database_names():
            for table in sorted(catalog.tables_of(db_name), key=lambda t: t.name):
                engine = getattr(table, "engine", None)
                if engine is None:
                    continue
                try:
                    parts = int(engine.part_count())
                except Exception:  # pragma: no cover
                    continue
                if parts > threshold:
                    fmt = "Too many parts in table {}: {} parts (threshold {})"
                    yield (
                        fmt.format(table.full_name(), parts, threshold),
                        fmt,
                        str(parts),
                        now,
                        f"Too many parts ({parts}) in table {table.full_name()}",
                    )


# ══════════════════════════════════════════════════════════════════════════
# 注册
# ══════════════════════════════════════════════════════════════════════════


def register() -> None:
    """把本模块的表装进全局注册表。"""
    for cls in (
        SystemDataTypeFamilies,
        SystemTableEngines,
        SystemFormats,
        SystemFunctions,
        SystemSettings,
        SystemClusters,
        SystemSettingsProfiles,
        SystemUsers,
        SystemRoles,
        SystemQuotas,
        SystemRowPolicies,
        SystemGrants,
        SystemCurrentRoles,
        SystemContributors,
        SystemBuildOptions,
        SystemTimeZones,
        SystemTimezones,
        SystemDiskSpace,
        SystemDisks,
        SystemWarnings,
    ):
        register_system_table(cls())

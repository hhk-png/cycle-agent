"""命名空间 —— 在"目录即表"之上再叠一层**组织结构**。

**先说清楚为什么会有这一层。**

LanceDB 的表模型极其扁平：``<root>/<name>.lance`` 就是一个表，
``table_names()`` 就是列一遍目录。这个模型的好处是**零元数据服务** ——
没有目录数据库、没有单点、没有"目录挂了全库打不开"。
OS 或对象存储本身就是目录。

代价在**多租户**下暴露：

* 一个租户的表名会撞上另一个租户的（都想叫 ``documents``）；
* 没法一次列出"某个项目下的所有表"；
* 授权只能落在"全局表名"这个粒度上，而租户 A 无权知道自己和
  租户 B 的表重名。

真实 LanceDB 为此提供了 **Namespace API**（企业版 / 云端）：
``create_namespace`` / ``list_namespaces`` / ``table_exists``，
命名空间可以嵌套（``["tenant_a", "project_x"]``），
并且远端有真正的元数据服务来保证隔离。

**本实现的定位**：一个**本地模型**。它把命名空间树持久化成根目录下的
一个小 JSON manifest（``_namespaces.json``），并明确承认两个不足：

1. **物理表是扁平的** —— 命名空间里的表仍然指向根目录下的
   ``<name>.lance``，所以**一张表名不能同时属于两个命名空间**
   （它们会是同一张物理表）。真实实现是在存储层就做了物理隔离；
2. **没有并发控制** —— manifest 的读改写不是原子的（真实后端
   由元数据服务保证）。单机、单写者的教学场景够用，多写者会丢更新。

**为什么要把它做出来而不是只写一段文档？** 因为"命名空间和表目录是
两层，而且可以不一致"这件事，只有亲手把表建在命名空间外面、
再 ``list_namespaces()`` 看它不在那里，才真的记得住。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import (
    MiniLanceError,
    QueryError,
    TableAlreadyExistsError,
    TableNotFoundError,
)

__all__ = [
    "Namespace",
    "NamespaceClient",
    "NamespaceNotFoundError",
    "NAMESPACE_MANIFEST",
]

#: 命名空间 manifest 的文件名。放在根目录而不是某个表里 ——
#: 它是**连接级**的元数据，和任何一张表都无关。
NAMESPACE_MANIFEST = "_namespaces.json"

#: 根命名空间的路径。
ROOT: Tuple[str, ...] = ()


class NamespaceNotFoundError(MiniLanceError):
    """请求的命名空间不存在。"""


def _as_path(path: Any) -> Tuple[str, ...]:
    """把 ``"a.b"`` / ``["a", "b"]`` / ``None`` 统一成 ``("a", "b")``。"""

    if path is None:
        return ROOT
    if isinstance(path, str):
        if not path.strip():
            return ROOT
        # 两种分隔符都认：点号是 REST API 风格，斜杠是路径风格
        parts = path.replace("/", ".").split(".")
        return tuple(p for p in (x.strip() for x in parts) if p)
    return tuple(str(p) for p in path)


def _key(path: Sequence[str]) -> str:
    return ".".join(path)


def _validate_name(name: str, *, what: str = "名称") -> str:
    name = str(name).strip()
    if not name:
        raise QueryError(f"{what}不能为空")
    if "." in name or "/" in name:
        raise QueryError(
            f"{what}里不能含 '.' 或 '/'（它们是路径分隔符）",
            name=name,
        )
    return name


@dataclass
class Namespace:
    """一个命名空间：若干子命名空间 + 若干表。"""

    path: Tuple[str, ...] = ROOT
    properties: Dict[str, str] = dc_field(default_factory=dict)
    #: 直接子命名空间的**名字**（不是完整路径）
    children: List[str] = dc_field(default_factory=list)
    #: 直接属于本命名空间的表名（不是完整路径）
    tables: List[str] = dc_field(default_factory=list)
    created_at: float = dc_field(default_factory=time.time)

    @property
    def name(self) -> str:
        return self.path[-1] if self.path else ""

    @property
    def full_path(self) -> str:
        return _key(self.path)

    def child_paths(self) -> List[str]:
        return [self.full_path + ("." if self.full_path else "") + c for c in self.children]

    def table_paths(self) -> List[str]:
        return [self.full_path + ("." if self.full_path else "") + t for t in self.tables]

    def to_json(self) -> Dict[str, Any]:
        return {
            "path": list(self.path),
            "properties": dict(self.properties),
            "children": sorted(self.children),
            "tables": sorted(self.tables),
            "created_at": self.created_at,
        }

    @staticmethod
    def from_json(obj: Mapping[str, Any]) -> "Namespace":
        return Namespace(
            path=tuple(obj.get("path") or ()),
            properties={str(k): str(v) for k, v in (obj.get("properties") or {}).items()},
            children=list(obj.get("children") or []),
            tables=list(obj.get("tables") or []),
            created_at=float(obj.get("created_at", time.time())),
        )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"Namespace({self.full_path or '<root>'!r}, "
            f"{len(self.children)} 个子空间, {len(self.tables)} 张表)"
        )


class NamespaceClient:
    """命名空间的读改写。

    **每一次写都要读一遍 manifest 再整体写回**（"读改写"）。
    这在单机上是毫秒级的操作，在对象存储上是"一次 GET + 一次 PUT" ——
    但它**不是原子的**：两个并发写者会丢一个更新。

    真实系统在这里必须用"条件写 + 版本号"（S3 的 ``If-None-Match``）
    或者干脆把元数据放到一个真正的事务型目录里。本实现把这一点
    写在文档里、而不是假装它不存在 —— 因为"元数据服务为什么必须存在"
    正是这一章要讲的东西。
    """

    def __init__(
        self,
        store: Any,
        *,
        self_uri: str = "",
        connection: Any = None,
        manifest_path: str = NAMESPACE_MANIFEST,
    ) -> None:
        self._store = store
        self._uri = self_uri
        self._connection = connection
        self._path = manifest_path

    # -- manifest 读改写 --------------------------------------------------
    def _load(self) -> Dict[str, Namespace]:
        if not self._store.exists(self._path):
            return {_key(ROOT): Namespace(ROOT)}
        try:
            raw = json.loads(self._store.get(self._path).decode("utf-8"))
        except Exception:  # pragma: no cover - manifest 坏了不该让连接打不开
            return {_key(ROOT): Namespace(ROOT)}
        out: Dict[str, Namespace] = {}
        for key, obj in (raw.get("namespaces") or {}).items():
            ns = Namespace.from_json(obj)
            out[key] = ns
        out.setdefault(_key(ROOT), Namespace(ROOT))
        return out

    def _save(self, namespaces: Mapping[str, Namespace]) -> None:
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "namespaces": {k: ns.to_json() for k, ns in sorted(namespaces.items())},
        }
        self._store.put(
            self._path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        )

    # -- 查询 -------------------------------------------------------------
    def exists(self, namespace_path: Any = None) -> bool:
        return _key(_as_path(namespace_path)) in self._load()

    def get(self, namespace_path: Any = None) -> Namespace:
        """取一个命名空间；不存在抛 :class:`NamespaceNotFoundError`。"""

        namespaces = self._load()
        key = _key(_as_path(namespace_path))
        if key not in namespaces:
            raise NamespaceNotFoundError(
                f"命名空间不存在: {_as_path(namespace_path) or '<root>'}",
                available=sorted(namespaces),
            )
        return namespaces[key]

    def list_namespaces(self, namespace_path: Any = None) -> List[str]:
        """列出直接子命名空间的**完整路径**。

        返回路径而不是名字，是为了让"嵌套命名空间"这件事在调用处就看得见：
        ``["tenant_a.project_x", "tenant_a.project_y"]`` 比
        ``["project_x", "project_y"]`` 信息量大得多。
        """

        return self.get(namespace_path).child_paths()

    def list_tables(self, namespace_path: Any = None) -> List[str]:
        """列出直接属于这个命名空间的表。"""

        return self.get(namespace_path).table_paths()

    def describe(self, namespace_path: Any = None) -> Dict[str, Any]:
        """一个命名空间的完整描述。"""

        ns = self.get(namespace_path)
        info = ns.to_json()
        info["full_path"] = ns.full_path
        info["parent"] = _key(ns.path[:-1]) if ns.path else None
        info["physical_tables"] = {
            t: self._physical_name(t) for t in ns.tables
        }
        return info

    # -- 写 ---------------------------------------------------------------
    def create_namespace(
        self,
        namespace_path: Any,
        mode: Optional[str] = None,
        properties: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        """建命名空间（父路径不存在会自动补出来）。

        ``mode``：

        * ``None`` / ``"create"`` —— 已存在就报错；
        * ``"exist_ok"`` / ``"overwrite"`` —— 已存在时不报错（``overwrite``
          还会覆盖 properties）。

        **自动补父路径**是有意的：``create_namespace(["a","b","c"])``
        而 ``a`` 不存在时，报错只会逼用户写三次调用，
        而"自动建出 a 和 b"没有任何歧义 —— 它们是通往 c 的必经之路。
        """

        parts = _as_path(namespace_path)
        if not parts:
            raise QueryError("不能创建根命名空间（它一直存在）")
        for part in parts:
            _validate_name(part, what="命名空间名")

        namespaces = self._load()
        existing = _key(parts) in namespaces
        if existing and mode in (None, "create"):
            raise QueryError(
                f"命名空间已存在: {_key(parts)}",
                hint="传 mode='exist_ok' 忽略，或 mode='overwrite' 覆盖属性",
            )

        # 逐级补齐
        for i in range(1, len(parts) + 1):
            key = _key(parts[:i])
            ns = namespaces.get(key)
            if ns is None:
                ns = Namespace(path=parts[:i], properties={})
                namespaces[key] = ns
            parent = namespaces[_key(parts[: i - 1])]
            if parts[i - 1] not in parent.children:
                parent.children.append(parts[i - 1])
        target = namespaces[_key(parts)]
        if properties:
            target.properties.update({str(k): str(v) for k, v in properties.items()})
        self._save(namespaces)
        return self.describe(parts)

    def drop_namespace(self, namespace_path: Any, mode: Optional[str] = None) -> None:
        """删命名空间。

        **默认不允许删非空的**（里面还有子空间或表）——
        这是从文件系统学来的：``rmdir`` 只删空目录，``rm -rf`` 得显式写。
        ``mode="recursive"`` 才级联删掉子空间。

        ⚠️ **删命名空间不会删任何一张物理表。** 表还在根目录下、
        还能被 ``table_names()`` 看到。这是刻意的：命名空间是**目录**，
        不是**所有权**。把这两件事混在一起，是"删了个命名空间结果
        数据全没了"这类事故的来源。
        """

        parts = _as_path(namespace_path)
        if not parts:
            raise QueryError("不能删根命名空间")
        namespaces = self._load()
        key = _key(parts)
        if key not in namespaces:
            raise NamespaceNotFoundError(f"命名空间不存在: {key}")
        ns = namespaces[key]
        if (ns.children or ns.tables) and mode != "recursive":
            raise QueryError(
                f"命名空间非空，拒绝删除: {key}",
                children=ns.children,
                tables=ns.tables,
                hint="传 mode='recursive' 级联删除（**仍然不会删任何物理表**）",
            )
        if mode == "recursive":
            for other in list(namespaces):
                if other == key or other.startswith(key + "."):
                    namespaces.pop(other, None)
        else:
            namespaces.pop(key, None)
        parent = namespaces.get(_key(parts[:-1]))
        if parent is not None and parts[-1] in parent.children:
            parent.children.remove(parts[-1])
        self._save(namespaces)

    # -- 表 ---------------------------------------------------------------
    def create_table(
        self,
        name: str,
        namespace_path: Any = None,
        data: Any = None,
        schema: Any = None,
        **kwargs: Any,
    ) -> Any:
        """在某个命名空间里建表。

        表名在命名空间内唯一；**物理表名是全局唯一的**，
        所以同一张物理表不能落在两个命名空间里（见模块文档）。
        """

        parts = _as_path(namespace_path)
        name = _validate_name(name, what="表名")
        namespaces = self._load()
        key = _key(parts)
        if key not in namespaces:
            raise NamespaceNotFoundError(
                f"命名空间不存在: {key or '<root>'}",
                hint="先 create_namespace(...)",
            )
        ns = namespaces[key]
        if name in ns.tables:
            raise TableAlreadyExistsError(f"命名空间 {key or '<root>'} 里已有表 {name!r}")
        if self._connection is None:
            raise MiniLanceError("NamespaceClient 没有绑定连接，无法建表")
        table = self._connection.create_table(name, data=data, schema=schema, **kwargs)
        ns.tables.append(name)
        self._save(namespaces)
        return table

    def open_table(self, name: str, namespace_path: Any = None, **kwargs: Any) -> Any:
        """打开命名空间里的表。"""

        parts = _as_path(namespace_path)
        namespaces = self._load()
        ns = namespaces.get(_key(parts))
        if ns is None:
            raise NamespaceNotFoundError(f"命名空间不存在: {_key(parts) or '<root>'}")
        if name not in ns.tables:
            raise TableNotFoundError(
                f"表不在这个命名空间里: {name!r} @ {_key(parts) or '<root>'}",
                tables=sorted(ns.tables),
                hint="表可能在别的命名空间、或者根本没被登记进任何命名空间",
            )
        if self._connection is None:
            raise MiniLanceError("NamespaceClient 没有绑定连接，无法打开表")
        return self._connection.open_table(name, **kwargs)

    def drop_table(self, name: str, namespace_path: Any = None, **kwargs: Any) -> None:
        """从命名空间里摘掉一张表。

        ``physical=True`` 时连物理表一起删。**默认只摘登记** ——
        和 ``drop_namespace`` 一样，把"目录"和"数据"分开。
        """

        physical = bool(kwargs.pop("physical", False))
        parts = _as_path(namespace_path)
        namespaces = self._load()
        ns = namespaces.get(_key(parts))
        if ns is None:
            raise NamespaceNotFoundError(f"命名空间不存在: {_key(parts) or '<root>'}")
        if name not in ns.tables:
            raise TableNotFoundError(f"表不在这个命名空间里: {name!r}")
        ns.tables.remove(name)
        self._save(namespaces)
        if physical and self._connection is not None:
            self._connection.drop_table(name)

    # -- 辅助 -------------------------------------------------------------
    def _physical_name(self, table_path: str) -> str:
        """命名空间里的表路径 → 物理表名。

        本实现的映射是**恒等的**（扁平物理表），这个函数存在的意义是
        把这个假设**集中在一个地方** —— 将来真要做物理隔离（比如
        ``<root>/<ns>/<name>.lance``），只要改这一个函数。
        """

        return table_path.split(".")[-1]

    def to_json(self) -> Dict[str, Any]:
        """整个命名空间树（给 ``describe`` / 教程用）。"""

        return {k: ns.to_json() for k, ns in sorted(self._load().items())}

    def __repr__(self) -> str:  # pragma: no cover
        namespaces = self._load()
        return f"NamespaceClient(uri={self._uri!r}, {len(namespaces)} 个命名空间)"

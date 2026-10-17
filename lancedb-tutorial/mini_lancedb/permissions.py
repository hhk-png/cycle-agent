"""权限 —— 一个**本地模型**，不是安全边界。

**先说清楚这一层不是什么。** 下面对每个权限的检查都发生在
**Python 进程内部**：任何人只要能 ``import mini_lancedb``，
就能绕过 :class:`AccessController` 直接对 store 调 ``get``/``put``。
所以它**不构成任何安全保证**，只表达"哪些操作被允许"这个**意图**，
让教程能演示 RBAC 的形状。

真实系统的权限检查必须落在**数据所在的边界上**：对象存储的
IAM policy、服务端的鉴权中间件、或者云端的 Namespace API。
把检查写在客户端里，等于把门锁装在门外面 —— 锁再结实，
拆掉它也不需要钥匙。这一点是本节最重要的结论，注释里反复强调它，
是因为"客户端权限检查"是本领域最常见的一种错觉。

**为什么还要做？** 因为它让三件事变得可以讨论、可以复现：

1. **权限的粒度**。是"整张表"还是"某些行"？只做表级的话，
   同一张表里的多租户数据就无法隔离 —— 而那是更常见的需求。
   本实现只到表级，并明确写出这个限制。
2. **授予的可组合性**。``ALTER`` 隐含 ``SELECT`` 吗？``DROP`` 隐含
   ``CREATE`` 吗？spreadsheet 式的"权限格子"看起来简单，
   组合起来就有一堆"隐含关系"要定义；一条定错就是提权漏洞。
3. **拒绝的可见性**。用户查不到数据，是"没数据"还是"没权限"？
   一律返回空会让排查变成猜谜；一律报错会泄露表的存在。
   本实现选择**报错**（:class:`PermissionDenied`），
   因为教程里"看得见拒绝"比"看不见"有用，而在生产里这个选择
   必须结合威胁模型重新决定。
"""

from __future__ import annotations

import enum
import json
import time
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .errors import MiniLanceError, QueryError

__all__ = [
    "Permission",
    "PermissionDenied",
    "TablePermissions",
    "PermissionStore",
    "AccessController",
    "with_tables_filtered",
    "PERMISSION_MANIFEST",
]

#: 授权表落盘的文件名。
PERMISSION_MANIFEST = "_permissions.json"


class Permission(enum.Enum):
    """七种表级权限。

    取值是字符串（而不是自增整数），因为授权表要落盘成人能读的 JSON；
    整数权限位在文件里是 ``7`` 这种没法复核的东西，而 ``"select"``
    一眼能看懂、也能被 diff。

    分成七种而不是简单的读写两种，是因为"读"和"写"各自内部的
    风险差异很大：``SELECT`` 只泄露数据，``DROP`` 直接毁掉数据，
    而 ``ALTER`` 会**静默改变已有数据的解释方式**（改列类型），
    它比 ``DROP`` 更阴险 —— 丢数据看得见，改错类型看不见。
    """

    SELECT = "select"
    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"
    CREATE = "create"
    ALTER = "alter"
    DROP = "drop"

    def __str__(self) -> str:  # pragma: no cover
        return self.value


class PermissionDenied(MiniLanceError):
    """权限不足。

    **定义在这里而不是 ``errors.py``**：``errors.py`` 是"库的结构性错误"
    的清单，而权限是**这一层**的概念。把它塞进全局错误体系里，
    会让"哪些错误是存储层的、哪些是策略层的"这条界线变模糊。
    """

    def __init__(
        self,
        message: str,
        *,
        principal: Optional[str] = None,
        table: Optional[str] = None,
        permission: Optional[Any] = None,
        hint: Optional[str] = None,
        **context: Any,
    ) -> None:
        if principal is not None:
            context["principal"] = principal
        if table is not None:
            context["table"] = table
        if permission is not None:
            context["permission"] = str(permission)
        if hint:
            context["hint"] = hint
        super().__init__(message, **context)
        self.principal = principal
        self.table = table
        self.permission = permission


#: 权限的隐含关系：授予左边的，自动获得右边的。
#:
#: 这张表**必须显式写出来**，不能靠"角色名字"约定。
#: 每条都值得单独想一遍：
#:
#: * ``SELECT`` 什么都不隐含 —— 只读就是只读；
#: * 三种写权限都隐含 ``SELECT`` —— 不能读却要改，只有
#:   "按 id 更新"这类盲写才成立，而盲写没法知道改对了没有；
#: * ``ALTER`` 隐含 ``INSERT`` —— alter 的常见实现是"重写整列"，
#:   本质是一次批量写；
#: * ``DROP`` 隐含 ``CREATE`` —— 能删表的人当然能重建一张，
#:   不给的话只是让"重建"变成"删完再找管理员"；
#: * ``CREATE`` **不**隐含 ``DROP`` —— 这是最容易被漏掉的一条。
#:   如果能建表就等于能删表，那么"给分析师一个可以随便试的沙箱"
#:   就变成了"分析师可以删掉生产表"。
_IMPLIED: Dict[Permission, Set[Permission]] = {
    Permission.SELECT: set(),
    Permission.INSERT: {Permission.SELECT},
    Permission.UPDATE: {Permission.SELECT},
    Permission.DELETE: {Permission.SELECT},
    Permission.CREATE: set(),
    Permission.ALTER: {Permission.SELECT, Permission.INSERT},
    Permission.DROP: {Permission.SELECT, Permission.CREATE},
}


def expand(permission: Permission) -> Set[Permission]:
    """把一个权限展开成"它实际意味着的全部权限"。"""

    out = {permission}
    out |= _IMPLIED.get(permission, set())
    return out


@dataclass
class TablePermissions:
    """一个主体（principal）在一张表上的权限集合。"""

    #: 主体标识：用户名 / 服务账号 / 角色名
    principal: str
    #: ``"*"`` 表示任意表
    table: str = "*"
    permissions: Set[Permission] = dc_field(default_factory=set)
    granted_at: float = dc_field(default_factory=time.time)
    granted_by: Optional[str] = None

    def allows(self, permission: Permission) -> bool:
        """是否允许某个权限 —— **按隐含关系展开后判断**。

        注意是 ``issued ⊆ held`` 而不是 "held 里有 issued"：
        授予 ``ALTER`` 的人要通过 ``INSERT`` 检查，靠的就是展开。
        """

        return expand(permission).issubset(self.permissions)

    def add(self, *permissions: Any) -> "TablePermissions":
        for p in permissions:
            self.permissions.add(_coerce(p))
        return self

    def remove(self, *permissions: Any) -> "TablePermissions":
        for p in permissions:
            self.permissions.discard(_coerce(p))
        return self

    def to_json(self) -> Dict[str, Any]:
        return {
            "principal": self.principal,
            "table": self.table,
            "permissions": sorted(p.value for p in self.permissions),
            "granted_at": self.granted_at,
            "granted_by": self.granted_by,
        }

    @staticmethod
    def from_json(obj: Mapping[str, Any]) -> "TablePermissions":
        return TablePermissions(
            principal=str(obj["principal"]),
            table=str(obj.get("table") or "*"),
            permissions={_coerce(p) for p in (obj.get("permissions") or [])},
            granted_at=float(obj.get("granted_at", time.time())),
            granted_by=obj.get("granted_by"),
        )

    def __repr__(self) -> str:  # pragma: no cover
        perms = ",".join(sorted(p.value for p in self.permissions)) or "-"
        return f"TablePermissions({self.principal!r} @ {self.table!r}: {perms})"


def _coerce(permission: Any) -> Permission:
    if isinstance(permission, Permission):
        return permission
    if isinstance(permission, str):
        key = permission.strip().lower()
        for p in Permission:
            if p.value == key:
                return p
        raise QueryError(
            f"不认识的权限: {permission!r}",
            supported=[p.value for p in Permission],
        )
    raise QueryError(
        "权限必须是 Permission 或字符串",
        got=type(permission).__name__,
    )


# ---------------------------------------------------------------------------
# 授权表
# ---------------------------------------------------------------------------


class PermissionStore:
    """授权表的持久化。

    结构极简：``{principal: {table: [perm, ...]}}``。
    ``*`` 作为表名表示"任意表"（通配）。

    换成一个真正的 policy 引擎（OPA / Cedar）时，这一层就是被替换掉的
    那一层 —— 接口只需要 ``get`` / ``grant`` / ``revoke`` / ``all_grants``。
    """

    def __init__(self, store: Any = None, *, path: str = PERMISSION_MANIFEST) -> None:
        self._store = store
        self._path = path
        #: ``[(principal, TablePermissions), ...]``
        self._grants: List[TablePermissions] = []
        if store is not None:
            self._load()

    # -- 持久化 -----------------------------------------------------------
    def _load(self) -> None:
        if not self._store.exists(self._path):
            return
        try:
            raw = json.loads(self._store.get(self._path).decode("utf-8"))
        except Exception:  # pragma: no cover
            return
        self._grants = [
            TablePermissions.from_json(obj) for obj in (raw.get("grants") or [])
        ]

    def save(self) -> None:
        if self._store is None:
            return
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "grants": [g.to_json() for g in self._grants],
        }
        self._store.put(
            self._path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        )

    # -- 读 ---------------------------------------------------------------
    def get(self, principal: str, table: str) -> Optional[TablePermissions]:
        """取"某个主体在某张表上"的授权（精确匹配，不通配）。"""

        for g in self._grants:
            if g.principal == principal and g.table == table:
                return g
        return None

    def all_grants(self) -> List[TablePermissions]:
        return list(self._grants)

    def effective(self, principal: str, table: str) -> Set[Permission]:
        """**有效权限集**：精确授权 ∪ 通配授权，并且**展开隐含关系**。

        这里把 ``expand`` 的语义落到实际集合上：``ALTER`` 授权在
        ``effective`` 里会同时出现 ``INSERT`` 和 ``SELECT``，
        这样"用户实际能干什么"和"我们给他记了什么"是两个可分开看的东西 ——
        排查提权问题时，这个区分非常有用。
        """

        out: Set[Permission] = set()
        for g in self._grants:
            if g.principal != principal:
                continue
            if g.table not in (table, "*"):
                continue
            for p in g.permissions:
                out |= expand(p)
        return out

    # -- 写 ---------------------------------------------------------------
    def grant(
        self,
        principal: str,
        table: str,
        permissions: Iterable[Any],
        *,
        granted_by: Optional[str] = None,
        persist: bool = True,
    ) -> TablePermissions:
        """授予权限（幂等：重复授予不会重复记）。"""

        perms = {_coerce(p) for p in permissions}
        if not perms:
            raise QueryError("至少要授予一个权限")
        existing = self.get(principal, table)
        if existing is None:
            existing = TablePermissions(
                principal=principal, table=table, granted_by=granted_by
            )
            self._grants.append(existing)
        existing.permissions |= perms
        existing.granted_at = time.time()
        if granted_by is not None:
            existing.granted_by = granted_by
        if persist:
            self.save()
        return existing

    def revoke(
        self,
        principal: str,
        table: str = "*",
        permissions: Optional[Iterable[Any]] = None,
        *,
        persist: bool = True,
    ) -> TablePermissions:
        """收回权限。

        ``permissions=None`` 表示"收回这张表上的全部权限"（整条授权删掉）。
        收回 ``SELECT`` 时**不会**连带收回 ``ALTER`` —— 但 ``ALTER``
        的有效集里仍然含 ``SELECT``（因为隐含关系存在），
        所以用户**依然读得到**。这是隐含关系的一个反直觉后果，
        真实 RBAC 系统里同样存在，值得单独想清楚。
        """

        existing = self.get(principal, table)
        if existing is None:
            raise QueryError(
                f"没有这条授权: {principal!r} @ {table!r}",
                hint="先 grant() 再 revoke()；通配授权要用 table='*' 收回",
            )
        if permissions is None:
            self._grants = [g for g in self._grants if g is not existing]
        else:
            for p in permissions:
                existing.permissions.discard(_coerce(p))
            if not existing.permissions:
                self._grants = [g for g in self._grants if g is not existing]
        if persist:
            self.save()
        return existing

    def clear(self, *, persist: bool = True) -> None:
        self._grants = []
        if persist:
            self.save()

    def __repr__(self) -> str:  # pragma: no cover
        return f"PermissionStore({len(self._grants)} 条授权)"


# ---------------------------------------------------------------------------
# 访问控制
# ---------------------------------------------------------------------------


class AccessController:
    """权限检查的入口。

    ⚠️ **再次强调：这是进程内的检查，不是安全边界。**
    任何能import 本库的代码都可以直接调用
    ``PermissionStore.grant(...)`` 给自己发权限。
    真实的边界在对象存储的 IAM / 服务端中间件。
    """

    #: 默认的"超级主体"。``None`` 表示不设超级主体（更安全的默认值）。
    ROOT_PRINCIPAL = "root"

    def __init__(
        self,
        store: Optional[PermissionStore] = None,
        *,
        enabled: bool = True,
        superusers: Optional[Iterable[str]] = None,
    ) -> None:
        self.store = store if store is not None else PermissionStore()
        #: ``enabled=False`` 时 :meth:`check` 直接放行。
        #: 存在的意义是让"库里加了一层权限"不至于把所有老代码打断 ——
        #: 默认开还是关是个策略问题，本实现默认**开**（更接近真实行为），
        #: 但提供一条无痛的关闭路径。
        self.enabled = bool(enabled)
        self.superusers: Set[str] = set(superusers or [self.ROOT_PRINCIPAL])

    # -- 检查 -------------------------------------------------------------
    def check(self, principal: str, table: str, permission: Any) -> None:
        """检查权限；不足则抛 :class:`PermissionDenied`。"""

        if not self.enabled:
            return
        if principal in self.superusers:
            return
        perm = _coerce(permission)
        held = self.store.effective(principal, table)
        if not expand(perm).issubset(held):
            raise PermissionDenied(
                f"主体 {principal!r} 无权对表 {table!r} 执行 {perm.value}",
                principal=principal,
                table=table,
                permission=perm,
                hint=(
                    f"当前有效权限: {','.join(sorted(p.value for p in held)) or '(无)'}；"
                    "用 access.grant(...) 授权（注意这是进程内检查，不是安全边界）"
                ),
            )

    def allows(self, principal: str, table: str, permission: Any) -> bool:
        """:meth:`check` 的布尔版本 —— **不抛异常**。

        两者的关系是"可能/必须"：``allows`` 用来**过滤**（列出你能看的表），
        ``check`` 用来**守卫**（你正在做的事必须被允许）。
        用 ``allows`` 做守卫是可读性陷阱：它默默返回 False 的路径
        很容易被写成 ``if not allows(...): pass``，于是拒绝变成了静默跳过。
        """

        try:
            self.check(principal, table, permission)
            return True
        except PermissionDenied:
            return False

    # -- 授权 -------------------------------------------------------------
    def grant(
        self,
        principal: str,
        table: str,
        permissions: Iterable[Any],
        *,
        granted_by: Optional[str] = None,
    ) -> TablePermissions:
        return self.store.grant(
            principal, table, permissions, granted_by=granted_by
        )

    def revoke(
        self,
        principal: str,
        table: str = "*",
        permissions: Optional[Iterable[Any]] = None,
    ) -> TablePermissions:
        return self.store.revoke(principal, table, permissions)

    def effective(self, principal: str, table: str) -> List[Permission]:
        return sorted(self.store.effective(principal, table), key=lambda p: p.value)

    # -- 过滤 -------------------------------------------------------------
    def filter_tables(
        self, principal: str, tables: Iterable[str], permission: Any = Permission.SELECT
    ) -> List[str]:
        perm = _coerce(permission)
        return [t for t in tables if self.allows(principal, t, perm)]

    def with_tables_filtered(
        self, principal: str, tables: Iterable[str], permission: Any = Permission.SELECT
    ) -> "FilteredTables":
        """返回一个**只包含"你能看的表"**的表集合。

        它是个独立的返回类型（而不是直接返回 ``list``），因为
        "被过滤掉的表给你一个明确的提示"和"直接消失"是两种体验：
        前者能回答"我明明建过这张表，怎么没了"，后者只会让人怀疑是不是自己记错了。
        本实现**明确列出被过滤掉的**（只在 ``denied`` 属性里，
        不放进 ``tables``）—— 这是"有用"和"不泄露存在性"之间的一个取舍，
        在真实系统里通常要选后者。
        """

        allowed: List[str] = []
        denied: List[str] = []
        perm = _coerce(permission)
        for t in tables:
            (allowed if self.allows(principal, t, perm) else denied).append(t)
        return FilteredTables(principal=principal, permission=perm, tables=allowed, denied=denied)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"AccessController(enabled={self.enabled}, "
            f"{len(self.store.all_grants())} 条授权)"
        )


class FilteredTables:
    """:meth:`AccessController.with_tables_filtered` 的返回值。

    刻意**不做成 list 的子类**：``list`` 接口太宽，一不小心就会
    把它当成"全部的表"传到写路径上去。这里只暴露必要的东西，
    尤其 ``denied`` 是**只读**的 —— 不能 append 进去"假装有权"。
    """

    def __init__(
        self,
        *,
        principal: str,
        permission: Permission,
        tables: Sequence[str],
        denied: Sequence[str],
    ) -> None:
        self.principal = principal
        self.permission = permission
        self.tables: List[str] = list(tables)
        self.denied: Tuple[str, ...] = tuple(denied)

    def __iter__(self) -> Any:
        return iter(self.tables)

    def __len__(self) -> int:
        return len(self.tables)

    def __contains__(self, item: object) -> bool:
        return item in self.tables

    def summary(self) -> str:
        return (
            f"{self.principal} 可 {self.permission.value} {len(self.tables)} 张表"
            + (f"；被拒绝 {len(self.denied)} 张（{'、'.join(self.denied)}）" if self.denied else "")
        )

    def __repr__(self) -> str:  # pragma: no cover
        return f"FilteredTables({self.summary()})"


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------


def with_tables_filtered(
    access: AccessController,
    principal: str,
    tables: Iterable[str],
    permission: Any = Permission.SELECT,
) -> List[str]:
    """模块级便捷函数：直接返回"可见的表名列表"。

    它和 :meth:`AccessController.with_tables_filtered` 的差别是返回值：
    这里给的是**裸 list**，适合 ``for name in with_tables_filtered(...)``
    这种"只想要名字"的场合；需要知道"哪些被拒了"时用方法版。
    """

    return access.filter_tables(principal, tables, permission)

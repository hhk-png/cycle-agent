"""访问控制 —— 用户、角色、权限、行策略、配额。

.. warning::

   **先读这段,再读代码。** mini-clickhouse 是一个**教学实现,没有网络认证**。

   * 这里**没有**客户端握手、没有密码校验、没有会话令牌。``Context.user``
     是一个**被完全信任**的字符串 —— 你设成谁,系统里你就是谁。
   * 真实 ClickHouse 的认证发生在连接建立时(明文/``sha256_password``/
     TLS 客户端证书/外部认证器),权限校验发生在**每一条查询的分析期**,
     而且**默认是强制的**(``CREATE USER`` 之后不发 ``GRANT`` 就什么都做不了)。
   * mini-clickhouse 的默认是**不强制**:解释器只有在真的去调用
     :func:`check_access` 时才会拦你。这一条是**有意为之的差异**,
     它让我们能把"权限模型"当作一个可观察、可测试的数据结构来讲,
     而不必先把整套认证/会话机制搭出来。

   教程里讲安全的那一章会解释 ClickHouse 的真实威胁模型(网络、认证、
   权限继承、行策略、配额、以及"为什么默认账号一定要改")。

模型一览(与真实 ClickHouse 的 ``src/Access/`` 一一对应):

.. code-block:: text

   AccessControl(单例,挂在 ctx.access 上)
     ├── users           用户 —— 认证方式、默认角色、默认库
     ├── roles           角色 —— 权限的"打包单位"
     ├── settings_profiles  设置档 —— 给一类用户预设一批 settings
     ├── quotas          配额 —— 每用户/角色每时间窗的硬闸门
     ├── row_policies    行策略 —— "同一个表,不同用户看到不同行"
     └── grants          授权 —— (权限, 库, 表, 列, grant option, 被授者)

一条授权的形状(和真实 ClickHouse 的 ``AccessRightsElement`` 对应):

.. code-block:: text

   (access_type, database_pattern, table_pattern, grant_option, grantee)
        │             │                │              │           │
        │             │                │              │           └ 用户或角色
        │             │                │              └ 能不能再授给别人(WITH GRANT OPTION)
        │             │                └ 表名(可含 * 通配)
        │             └ 库名(可含 * 通配)
        └ SELECT / INSERT / ALTER / CREATE / DROP / SHOW / SYSTEM /
          TRUNCATE / OPTIMIZE / dictGet / ALL,以及列级 SELECT(a, b)

**授权解析顺序(这是理解"为什么我明明授了权还是被拒"的钥匙):**

.. code-block:: text

   请求到达:  check_access(SELECT, db, t, user=u)
        │
        ▼
   把 u 的"有效权限集合"并起来:
       ① u 自己直接持有的 grant
       ② u 的每一个**默认角色**持有的 grant
       ③ 那些角色**自己的角色**(roles-of-roles)持有的 grant …… 递归
        │
        ▼
   任意一条能覆盖 (SELECT, db, t, 请求的列)  →  放行
   一条都没有                                 →  ACCESS_DENIED

   为什么**回收**必须看整条链? 因为一条权限可能从多个来源同时拥有:
   你把 "SELECT ON t" 从角色 r 上 REVOKE 了,但 u 自己**也**直接有
   "SELECT ON t" —— 这时 u 仍然能读 t。只看"r 的权限没了"是不够的;
   要判断"u 的有效集合里还有没有第二条覆盖它的授权"。
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import uuid as _uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .errors import ErrorCode, MiniChError, error_code, raise_error

__all__ = [
    "AccessControl",
    "User",
    "Role",
    "Quota",
    "QuotaInterval",
    "RowPolicy",
    "SettingsProfile",
    "Grant",
    "get_access_control",
    "check_access",
    "has_access",
    "apply_row_policies",
    "apply_column_policies",
    "make_sha256_password",
    "verify_sha256_password",
    "KNOWN_ACCESS_TYPES",
]


#: mini-clickhouse 认识的权限类型。真实 ClickHouse 有几百个(每一个功能点
#: 都是一个 access type);这里保留教学/测试真正用到的那批。
KNOWN_ACCESS_TYPES = (
    "SELECT", "INSERT", "ALTER", "CREATE", "DROP", "SHOW", "SYSTEM",
    "TRUNCATE", "OPTIMIZE", "dictGet", "ALL",
)


def _new_id() -> str:
    return str(_uuid.uuid4())


def _match(pattern: str, value: str) -> bool:
    """库/表名匹配 —— 支持 ``*`` 通配(真实 CH 用 regex,这里用 fnmatch)。

    ``*`` 与空串都表示"任意"。这是**有意简化**:真实 ClickHouse 的
    数据库/表模式是**正则**,mini-clickhouse 用 shell 风格通配。
    """
    if pattern in ("", "*"):
        return True
    return fnmatch.fnmatchcase(value, pattern) or pattern == value


# ══════════════════════════════════════════════════════════════════════════
# 认证 —— sha256_password(真实 ClickHouse 的现代默认之一)
# ══════════════════════════════════════════════════════════════════════════


def make_sha256_password(password: str, salt: Optional[str] = None) -> Dict[str, str]:
    """构造一个 ``sha256_password`` 认证记录。

    真实 ClickHouse 的 ``sha256_password`` 存的是 ``SHA256(salt + password)``,
    ``salt`` 是**每个用户独立随机**的一串字节。为什么带 salt?
    因为不带 salt 的话,"两个用户密码哈希相同 ⇒ 密码相同"会泄漏信息,
    而且彩虹表可以直接查。带 salt 后,同一个密码在不同用户下哈希不同。

    我们照做:返回 ``{"method", "salt", "hash"}``。
    """
    salt = salt if salt is not None else os.urandom(16).hex()
    digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return {"method": "sha256_password", "salt": salt, "hash": digest}


def verify_sha256_password(stored: Dict[str, str], password: str) -> bool:
    """校验密码 —— 用**存储的 salt** 重算一次,再做常数时间比较。"""
    import hmac

    digest = hashlib.sha256((stored.get("salt", "") + password).encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, stored.get("hash", ""))


# ══════════════════════════════════════════════════════════════════════════
# 实体:用户 / 角色 / 配额 / 行策略 / 设置档 / 授权
# ══════════════════════════════════════════════════════════════════════════


class Role:
    """角色 —— 权限的打包单位。

    角色可以**继承角色**(``GRANT role_a TO role_b``),这就形成了角色链。
    解析权限时要沿链递归(见模块开头的图)。

    ``system.roles`` 读 ``name / id / storage`` 三列。
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.id = _new_id()
        self.storage = "memory"
        #: 这个角色拥有的子角色(``GRANT r_child TO r_parent``)。
        self.roles: List[str] = []
        #: 这个角色被授给的用户/角色(反向索引,便于回收)。
        self.grantees: List[str] = []

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Role {self.name}>"


class User:
    """用户 —— 认证方式 + 默认角色 + 默认库。

    ``system.users`` 读 ``name / id / storage / auth_type / host_ip /
    host_names / default_roles_all / default_roles_list /
    default_roles_except / grantees``。所以这些字段都得是**真属性**。
    """

    def __init__(
        self,
        name: str,
        auth_method: str = "no_password",
        host_pattern: str = "ANY",
        default_database: str = "default",
    ) -> None:
        self.name = name
        self.id = _new_id()
        self.storage = "memory"
        #: 认证方式名(``no_password`` / ``sha256_password`` / ``plaintext_password``)
        self.auth_method = auth_method
        #: 认证记录(salt/hash);``no_password`` 时为 None。
        self.auth: Optional[Dict[str, str]] = None
        #: 明文密码(仅 ``plaintext_password`` 时保留 —— 真实 CH 也不推荐)。
        self.password_plaintext: Optional[str] = None
        #: host 限制(``ANY`` / ``LOCAL`` / 具体 IP 或主机名模式)。
        self.host_pattern = host_pattern or "ANY"
        #: 默认库(``DEFAULT DATABASE db``)。
        self.default_database = default_database
        #: 默认角色。
        self.default_roles_all = False
        self.default_roles_list: List[str] = []
        self.default_roles_except: List[str] = []
        #: 该用户/角色被授给了谁(``TO`` 列表)。
        self.grantees: List[str] = []
        #: 该用户默认挂在哪些设置档上。
        self.settings_profile: Optional[str] = None
        #: 该用户挂的配额(``QUOTA q`` / ``SETTINGS ...``)。
        self.quota: Optional[str] = None
        self.settings: Dict[str, Any] = {}

    # ── 给 system.users 用的别名 ───────────────────────────────────────
    @property
    def auth_type(self) -> str:
        return self.auth_method

    @property
    def host_ip(self) -> str:
        """``host_ip`` 列 —— 只有一个具体主机时给出,否则空串。"""
        pat = self.host_pattern
        if pat and pat.upper() not in ("ANY", "LOCAL", "NAME"):
            return pat
        return ""

    @property
    def host_names(self) -> List[str]:
        pat = self.host_pattern
        if pat and pat.upper() == "NAME":
            return []
        if pat and pat.upper() not in ("ANY", "LOCAL", "NAME"):
            return [pat]
        return []

    def set_password(self, method: str, password: Optional[str]) -> None:
        self.auth_method = method
        if method == "no_password":
            self.auth = None
            self.password_plaintext = None
        elif method == "sha256_password":
            self.auth = make_sha256_password(password or "")
            self.password_plaintext = None
        elif method == "plaintext_password":
            self.password_plaintext = password or ""
            self.auth = None
        else:
            raise_error(
                ErrorCode.NOT_IMPLEMENTED,
                f"Authentication method '{method}' is not implemented in mini-clickhouse "
                f"(supported: no_password, sha256_password, plaintext_password)",
            )

    def check_password(self, password: str) -> bool:
        if self.auth_method == "no_password":
            return True
        if self.auth_method == "sha256_password" and self.auth is not None:
            return verify_sha256_password(self.auth, password)
        if self.auth_method == "plaintext_password":
            import hmac

            return hmac.compare_digest(self.password_plaintext or "", password)
        return False

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.name} auth={self.auth_method}>"


class QuotaInterval:
    """配额的一个时间窗 —— ``FOR INTERVAL n second MAX queries = N, errors = N, …``。

    真实 ClickHouse 的 ``Quota::Interval`` 里有一张 ``限项 → 上限`` 的表,
    限项包括 ``queries`` / ``errors`` / ``result_rows`` / ``read_rows`` /
    ``execution_time``。``duration`` 是窗口长度(秒)。
    """

    __slots__ = ("duration", "limits", "_used")

    def __init__(self, duration: int, limits: Optional[Dict[str, int]] = None) -> None:
        self.duration = int(duration)
        self.limits: Dict[str, int] = dict(limits or {})
        self._used: Dict[str, int] = {}

    # 单列取值(system.quotas 的键值对形式会用)
    @property
    def queries(self) -> Any:
        return self.limits.get("queries", "")

    @property
    def errors(self) -> Any:
        return self.limits.get("errors", "")

    @property
    def result_rows(self) -> Any:
        return self.limits.get("result_rows", "")

    @property
    def read_rows(self) -> Any:
        return self.limits.get("read_rows", "")

    @property
    def execution_time(self) -> Any:
        return self.limits.get("execution_time", "")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<QuotaInterval {self.duration}s {self.limits}>"


class Quota:
    """配额 —— "每个用户/角色每个时间窗最多能跑多少查询、读多少行"。

    真实 ClickHouse 的配额**按用户/角色 + 时间窗**计数,超过就抛
    ``TOO_MANY_SIMULTANEOUS_QUERIES``。这里是单进程教学实现:计数在内存里,
    每次 ``consume`` 加一。
    """

    def __init__(self, name: str, intervals: Optional[List[QuotaInterval]] = None) -> None:
        self.name = name
        self.id = _new_id()
        self.storage = "memory"
        self.intervals: List[QuotaInterval] = intervals or []
        self.is_keyed = False
        self.keys: List[str] = []
        self.grantees: List[str] = []

    def consume(self, query_info: Any = None) -> None:
        """给一次查询"记账";超限就抛。

        .. note::

           **错误码的选择**:真实 ClickHouse 在配额打满时抛的是
           ``TOO_MANY_SIMULTANEOUS_QUERIES``;mini-clickhouse 没有专门的
           "配额超限"编号,所以**沿用** ``TOO_MANY_SIMULTANEOUS_QUERIES``
           (编号 67)—— 并在错误消息里说清楚是配额在拦你。这是一个
           **有意的近似**,不值得为它新造一个编号。

           (``minich.errors.ErrorCode`` 这个类里**没有**同名的常量属性,
           所以我们用 :func:`minich.errors.error_code` 按名字查编号。)
        """
        read_rows = int(getattr(query_info, "read_rows", 0) or 0)
        result_rows = int(getattr(query_info, "result_rows", 0) or 0)
        elapsed = float(getattr(query_info, "elapsed", 0.0) or 0.0)
        for interval in self.intervals:
            usage = {
                "queries": 1,
                "result_rows": result_rows,
                "read_rows": read_rows,
                "execution_time": int(elapsed),
            }
            for metric, inc in usage.items():
                cap = interval.limits.get(metric)
                if cap is None:
                    continue
                interval._used[metric] = interval._used.get(metric, 0) + inc
                if interval._used[metric] > cap:
                    raise_error(
                        error_code("TOO_MANY_SIMULTANEOUS_QUERIES"),
                        f"Quota '{self.name}' exceeded: {metric} = "
                        f"{interval._used[metric]} > {cap} per {interval.duration}s interval",
                    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Quota {self.name} {[i.duration for i in self.intervals]}>"


class RowPolicy:
    """行策略 —— ``CREATE ROW POLICY ... USING expr``。

    ``USING`` 后面的表达式会被**逐行求值**,为假的行被过滤掉。这是
    "同一个表,不同用户看到不同行"的实现方式。

    ``system.row_policies`` 读 ``name / short_name / database / table / id /
    storage / filter / is_restrictive``。
    """

    def __init__(
        self,
        name: str,
        database: str,
        table: str,
        filter_sql: str = "",
        *,
        restrictive: bool = False,
        for_select: bool = True,
        grantees: Optional[List[str]] = None,
    ) -> None:
        self.name = name
        self.database = database
        self.table = table
        self.filter = filter_sql
        self.is_restrictive = restrictive
        self.for_select = for_select
        self.grantees: List[str] = grantees or []
        self.id = _new_id()
        self.storage = "memory"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<RowPolicy {self.name} ON {self.database}.{self.table}>"


class SettingsProfile:
    """设置档 —— ``CREATE SETTINGS PROFILE p SETTINGS k = v``。

    ``system.settings_profiles`` 读 ``name / id / storage / num_elements /
    apply_to_all / apply_to_list / apply_to_except``,并用
    ``elements``(或 ``settings``)作为设置的映射。
    """

    def __init__(self, name: str, settings: Optional[Dict[str, Any]] = None) -> None:
        self.name = name
        self.id = _new_id()
        self.storage = "memory"
        self.elements: Dict[str, Any] = dict(settings or {})
        self.apply_to_all = True
        self.apply_to_list: List[str] = []
        self.apply_to_except: List[str] = []

    @property
    def settings(self) -> Dict[str, Any]:
        return self.elements

    @property
    def num_elements(self) -> int:
        return len(self.elements)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SettingsProfile {self.name} {self.elements}>"


class Grant:
    """一条授权。

    ``system.grants`` 需要 ``user_name / role_name / access_type / database /
    table / column / is_partial_revoke / grant_option`` 这几列 ——
    所以被授者是用户时 ``user_name`` 有值、``role_name`` 为空,反之亦然。
    """

    def __init__(
        self,
        access_type: str,
        database: str = "*",
        table: str = "*",
        column: Optional[str] = None,
        grant_option: bool = False,
        grantee: str = "",
        grantee_is_role: bool = False,
    ) -> None:
        self.access_type = access_type.upper()
        self.database = database or "*"
        self.table = table or "*"
        self.column = column
        self.grant_option = grant_option
        self.grantee = grantee
        self.grantee_is_role = grantee_is_role
        self.is_partial_revoke = False

    # ── system.grants 的分列 ───────────────────────────────────────────
    @property
    def user_name(self) -> str:
        return "" if self.grantee_is_role else self.grantee

    @property
    def role_name(self) -> str:
        return self.grantee if self.grantee_is_role else ""

    def covers(
        self,
        access_type: str,
        database: str,
        table: str,
        columns: Optional[Sequence[str]] = None,
    ) -> bool:
        """这条授权能否覆盖一次 ``(类型, 库, 表, 列)`` 请求。"""
        if self.access_type != "ALL" and self.access_type != access_type.upper():
            return False
        if not _match(self.database, database):
            return False
        if not _match(self.table, table):
            return False
        if columns:
            if self.column is None:
                return True  # 整表授权覆盖任意列
            return all(self.column == c for c in columns) if len(columns) == 1 else False
        return True

    def key(self) -> Tuple[Any, ...]:
        return (self.access_type, self.database, self.table, self.column,
                self.grantee, self.grantee_is_role)

    def to_sql(self) -> str:
        priv = self.access_type if self.column is None else f"{self.access_type}({self.column})"
        grantee = self.grantee
        if self.access_type in ("", "ROLE"):
            # 角色授予(没有 ON 子句)
            return f"GRANT {grantee} TO {grantee}"
        stmt = f"GRANT {priv} ON {self.database}.{self.table} TO {grantee}"
        if self.grant_option:
            stmt += " WITH GRANT OPTION"
        return stmt

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Grant {self.access_type} ON {self.database}.{self.table} TO {self.grantee}>"


class RoleGrant:
    """``GRANT role TO user`` —— 角色授予(没有 ON 子句)。

    单独一个类是为了和"权限授予"区分开:权限授予挂在 :class:`Grant` 上,
    角色授予挂在"谁拥有哪些角色"上(:attr:`AccessControl.entity_roles`)。
    """

    __slots__ = ("role", "grantee", "grantee_is_role", "grant_option")

    def __init__(self, role: str, grantee: str,
                 grantee_is_role: bool = False, grant_option: bool = False) -> None:
        self.role = role
        self.grantee = grantee
        self.grantee_is_role = grantee_is_role
        self.grant_option = grant_option

    def __repr__(self) -> str:  # pragma: no cover
        return f"<RoleGrant {self.role} -> {self.grantee}>"


# ══════════════════════════════════════════════════════════════════════════
# AccessControl —— 单例,挂在 ctx.access 上
# ══════════════════════════════════════════════════════════════════════════


class AccessControl:
    """访问控制子系统。

    解释器对这些东西的调用点只有五处(见 ``minich/execution/interpreter.py``):

    .. code-block:: text

       CREATE USER/ROLE/QUOTA/ROW POLICY/…  →  create(node)
       DROP USER/ROLE                       →  drop(kind, name, if_exists)
       GRANT / REVOKE                       →  apply_grant(node)
       SHOW GRANTS                          →  show_grants(user)

    而 :func:`check_access` / :func:`apply_row_policies` 是**给更完整的
    解释器**准备的强制入口 —— 当前的解释器**不主动**调用它们,所以
    mini-clickhouse 默认**不拦查询**。
    """

    def __init__(self) -> None:
        self.users: Dict[str, User] = {}
        self.roles: Dict[str, Role] = {}
        self.quotas: Dict[str, Quota] = {}
        self.row_policies: Dict[str, RowPolicy] = {}
        self.settings_profiles: Dict[str, SettingsProfile] = {}
        #: 所有权限授予(扁平表)。``system.grants`` 直接读它。
        self.grants: List[Grant] = []
        #: 角色授予:grantee → [role, …]。
        self.entity_roles: Dict[str, List[str]] = {}
        #: 当前会话激活动作的角色(``SET ROLE`` 会改它)。
        self.current_roles: List[str] = []
        #: 是否强制权限检查。默认 True —— 但**没人调用 check_access 时**
        #: 它也就无所谓;解释器默认不调用,所以实际是不强制。
        self.enabled = True
        #: 是否已经有"真实用户"被创建过(用于区分"空配置"与"有配置")。
        self._default_user_created = False

    # ── 查询接口 ───────────────────────────────────────────────────────

    def try_get_user(self, name: str) -> Optional[User]:
        return self.users.get(name)

    def is_role(self, name: str) -> bool:
        return name in self.roles

    def canon(self, name: str) -> str:
        """把名字规范化成**已存在的实体名**(大小写不敏感)。

        为什么需要这一步? 因为 ``minich/sql/parser.py`` 在解析 ``GRANT`` 时
        会把权限/角色名 **大写化**(``GRANT reader TO alice`` 里的
        ``reader`` 变成 ``READER``),而 ``CREATE ROLE reader`` 保留原样。
        真实 ClickHouse 的标识符是大小写敏感的,但为了在这个解析器下
        "授的角色就是建的角色",我们做一次大小写不敏感的归一 —— 这是一条
        **已知的、有意的**适配。找不到时原样返回。
        """
        if name in self.users or name in self.roles:
            return name
        low = name.lower()
        for u in self.users:
            if u.lower() == low:
                return u
        for r in self.roles:
            if r.lower() == low:
                return r
        return name

    def effective_roles(self, name: str) -> List[str]:
        """算出一个用户的**有效角色集合**(含角色链的传递闭包)。

        顺序:先自己直接持有的角色,再沿着角色链往下(roles-of-roles)。
        用 BFS + visited 去重,避免角色环导致无限递归(真实 CH 也会
        检测角色授予环并报错)。
        """
        out: List[str] = []
        seen: Set[str] = set()
        queue = list(self.entity_roles.get(name, []))
        while queue:
            role = queue.pop(0)
            if role in seen:
                continue
            seen.add(role)
            out.append(role)
            queue.extend(self.entity_roles.get(role, []))
        return out

    def grants_for(self, name: str) -> List[Grant]:
        """一个用户/角色的**有效授权集合** —— 自己 + 有效角色,并递归展开。"""
        owners = {name} | set(self.effective_roles(name))
        return [g for g in self.grants if g.grantee in owners]

    def check(
        self,
        user_name: str,
        access_type: str,
        database: str = "",
        table: str = "",
        columns: Optional[Sequence[str]] = None,
    ) -> bool:
        """纯查询:用户是否有某项权限(不抛异常)。"""
        if not self.enabled:
            return True
        if not self.users and not self.roles:
            # 还没配置任何用户/角色 → 教学实例的"敞开"状态。
            return True
        for grant in self.grants_for(user_name):
            if grant.covers(access_type, database, table, columns):
                return True
        return False

    # ── CREATE ─────────────────────────────────────────────────────────

    def create(self, node: Any) -> Any:
        """``CREATE USER / ROLE / QUOTA / ROW POLICY / SETTINGS PROFILE``。"""
        kind = str(getattr(node, "kind", "USER") or "USER").upper()
        name = getattr(node, "name", "") or ""
        clauses = list(getattr(node, "clauses", []) or [])
        if_not_exists = bool(getattr(node, "if_not_exists", False))

        if kind == "USER":
            return self._create_user(name, clauses, if_not_exists)
        if kind == "ROLE":
            return self._create_role(name, if_not_exists)
        if kind == "QUOTA":
            return self._create_quota(name, clauses, if_not_exists)
        if kind == "ROW POLICY":
            return self._create_row_policy(name, clauses, if_not_exists)
        if kind == "SETTINGS PROFILE":
            return self._create_settings_profile(name, clauses, if_not_exists)
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"CREATE {kind} is not implemented in mini-clickhouse "
            f"(supported: USER, ROLE, QUOTA, ROW POLICY, SETTINGS PROFILE)",
        )

    def _create_user(self, name: str, clauses: List[str], if_not_exists: bool) -> User:
        if name in self.users:
            if if_not_exists:
                return self.users[name]
            raise_error(ErrorCode.ACCESS_DENIED, f"User {name} already exists")
        user = User(name)
        self._parse_user_clauses(user, clauses)
        self.users[name] = user
        return user

    def _create_role(self, name: str, if_not_exists: bool) -> Role:
        if name in self.roles:
            if if_not_exists:
                return self.roles[name]
            raise_error(ErrorCode.ACCESS_DENIED, f"Role {name} already exists")
        role = Role(name)
        self.roles[name] = role
        return role

    def _create_quota(self, name: str, clauses: List[str], if_not_exists: bool) -> Quota:
        if name in self.quotas:
            if if_not_exists:
                return self.quotas[name]
            raise_error(ErrorCode.ACCESS_DENIED, f"Quota {name} already exists")
        quota = Quota(name)
        quota.intervals = _parse_quota_intervals(clauses)
        for grantee in _grantees_after(clauses, "TO"):
            quota.grantees.append(grantee)
        self.quotas[name] = quota
        return quota

    def _create_row_policy(self, name: str, clauses: List[str], if_not_exists: bool) -> RowPolicy:
        if name in self.row_policies:
            if if_not_exists:
                return self.row_policies[name]
            raise_error(ErrorCode.ACCESS_DENIED, f"Row policy {name} already exists")
        database, table, filter_sql, restrictive, for_select = _parse_row_policy(name, clauses)
        policy = RowPolicy(name, database, table, filter_sql,
                           restrictive=restrictive, for_select=for_select)
        self.row_policies[name] = policy
        return policy

    def _create_settings_profile(self, name: str, clauses: List[str],
                                 if_not_exists: bool) -> SettingsProfile:
        if name in self.settings_profiles:
            if if_not_exists:
                return self.settings_profiles[name]
            raise_error(ErrorCode.ACCESS_DENIED, f"Settings profile {name} already exists")
        profile = SettingsProfile(name, _parse_settings_clauses(clauses))
        self.settings_profiles[name] = profile
        return profile

    def _parse_user_clauses(self, user: User, clauses: List[str]) -> None:
        i = 0
        n = len(clauses)
        while i < n:
            tok = clauses[i].upper()
            if tok == "IDENTIFIED":
                # IDENTIFIED [WITH method [BY 'password']] | IDENTIFIED BY 'password'
                i += 1
                method = "sha256_password"
                password = None
                if i < n and clauses[i].upper() == "WITH":
                    i += 1
                    if i < n:
                        method = clauses[i].lower()
                        i += 1
                if i < n and clauses[i].upper() == "BY":
                    i += 1
                    if i < n:
                        password = clauses[i]
                        i += 1
                if method in ("no_password",):
                    user.set_password("no_password", None)
                elif method in ("sha256_password", "sha256"):
                    user.set_password("sha256_password", password or "")
                elif method in ("plaintext_password", "plaintext"):
                    user.set_password("plaintext_password", password or "")
                else:
                    raise_error(
                        ErrorCode.NOT_IMPLEMENTED,
                        f"Authentication method '{method}' is not implemented in "
                        f"mini-clickhouse (supported: no_password, sha256_password, "
                        f"plaintext_password)",
                    )
            elif tok == "HOST":
                i += 1
                parts = []
                while i < n and clauses[i].upper() not in _USER_CLAUSE_KEYWORDS:
                    parts.append(clauses[i])
                    i += 1
                user.host_pattern = " ".join(parts) if parts else "ANY"
            elif tok == "DEFAULT" and i + 1 < n and clauses[i + 1].upper() == "ROLE":
                i += 2
                roles = []
                all_roles = False
                all_except = False
                while i < n and clauses[i].upper() not in _USER_CLAUSE_KEYWORDS:
                    word = clauses[i]
                    if word == ",":
                        i += 1
                        continue
                    if word.upper() == "ALL":
                        all_roles = True
                    elif word.upper() == "EXCEPT":
                        all_except = True
                    elif word.upper() == "NONE":
                        pass
                    else:
                        roles.append(word)
                    i += 1
                if all_roles:
                    user.default_roles_all = True
                    if all_except:
                        user.default_roles_except = roles
                    else:
                        user.default_roles_list = []
                else:
                    user.default_roles_list = roles
            elif tok == "DEFAULT" and i + 1 < n and clauses[i + 1].upper() == "DATABASE":
                i += 2
                if i < n:
                    user.default_database = clauses[i]
                    i += 1
            elif tok == "SETTINGS":
                settings = _parse_settings_clauses(clauses[i + 1:])
                user.settings.update(settings)
                i = n
            else:
                i += 1

    # ── DROP ───────────────────────────────────────────────────────────

    def drop(self, kind: str, name: str, if_exists: bool = False) -> None:
        kind = str(kind or "").upper()
        if kind == "USER":
            if name not in self.users:
                if not if_exists:
                    raise_error(ErrorCode.UNKNOWN_IDENTIFIER, f"User {name} does not exist")
                return
            del self.users[name]
            self._purge_grants_for(name)
        elif kind == "ROLE":
            if name not in self.roles:
                if not if_exists:
                    raise_error(ErrorCode.UNKNOWN_IDENTIFIER, f"Role {name} does not exist")
                return
            del self.roles[name]
            self._purge_grants_for(name)
        else:
            raise_error(ErrorCode.NOT_IMPLEMENTED, f"DROP {kind} is not implemented in mini-clickhouse")

    def _purge_grants_for(self, name: str) -> None:
        self.grants = [g for g in self.grants if g.grantee != name]
        self.entity_roles.pop(name, None)

    # ── GRANT / REVOKE ─────────────────────────────────────────────────

    def apply_grant(self, node: Any) -> None:
        """``GRANT`` / ``REVOKE`` 的落地。

        ``node`` 是 :class:`minich.sql.ast_.GrantQuery`:

        * ``privileges`` —— 权限名列表;列级权限形如 ``"SELECT(a, b)"``;
          没有 ``ON`` 时它们是**角色名**;
        * ``on`` —— ``"db.*"`` / ``"db.t"`` / ``""``(角色授予);
        * ``to`` —— 被授者(用户或角色名);
        * ``with_grant_option`` / ``revoke``。
        """
        revoke = bool(getattr(node, "revoke", False))
        on = str(getattr(node, "on", "") or "").strip()
        grantees = list(getattr(node, "to", []) or [])
        with_grant_option = bool(getattr(node, "with_grant_option", False))

        if not on:
            # 角色授予:GRANT role TO user  /  REVOKE role FROM user
            for role in node.privileges:
                role_name = str(role)
                for grantee in grantees:
                    if revoke:
                        self._revoke_role(role_name, grantee, if_exists=True)
                    else:
                        self._grant_role(role_name, grantee, grant_option=with_grant_option)
            return

        database, table = _split_on(on)
        elements = _expand_privileges(node.privileges)
        for grantee in grantees:
            grantee = self.canon(grantee)
            is_role = self.is_role(grantee)
            if not is_role and grantee not in self.users:
                # 真实 CH 会报"用户不存在";我们**宽松**一点:隐式建立用户,
                # 否则"给还没建的用户授权"在教学里很别扭。见报告里的差异清单。
                self.users[grantee] = User(grantee)
            for access_type, column in elements:
                if revoke:
                    self._revoke_privilege(grantee, is_role, access_type, database,
                                          table, column)
                else:
                    self._add_privilege(grantee, is_role, access_type, database,
                                        table, column, with_grant_option)

    def _grant_role(self, role: str, grantee: str, grant_option: bool = False) -> None:
        role = self.canon(role)
        grantee = self.canon(grantee)
        if role not in self.roles:
            self.roles[role] = Role(role)
        if grantee not in self.users and grantee not in self.roles:
            self.users[grantee] = User(grantee)
        roles = self.entity_roles.setdefault(grantee, [])
        if role not in roles:
            roles.append(role)
        grantee_obj = self.users.get(grantee) or self.roles.get(grantee)
        if grantee_obj is not None and role not in grantee_obj.grantees:
            grantee_obj.grantees.append(role)

    def _revoke_role(self, role: str, grantee: str, if_exists: bool = False) -> None:
        role = self.canon(role)
        grantee = self.canon(grantee)
        roles = self.entity_roles.get(grantee, [])
        if role in roles:
            roles.remove(role)
        else:
            if not if_exists:
                raise_error(
                    ErrorCode.ACCESS_DENIED,
                    f"Role {role} is not granted to {grantee}",
                )

    def _add_privilege(self, grantee: str, is_role: bool, access_type: str,
                       database: str, table: str, column: Optional[str],
                       grant_option: bool) -> None:
        if access_type not in KNOWN_ACCESS_TYPES:
            raise_error(
                ErrorCode.NOT_IMPLEMENTED,
                f"Unknown privilege '{access_type}'. Known: {', '.join(KNOWN_ACCESS_TYPES)}",
            )
        for g in self.grants:
            if (g.grantee == grantee and g.access_type == access_type
                    and g.database == database and g.table == table
                    and g.column == column):
                g.grant_option = g.grant_option or grant_option
                return
        self.grants.append(Grant(access_type, database, table, column,
                                 grant_option, grantee, is_role))

    def _revoke_privilege(self, grantee: str, is_role: bool, access_type: str,
                          database: str, table: str, column: Optional[str]) -> None:
        """回收一条权限 —— **必须看整条链**(见模块开头的说明)。

        这里只从**被授者自己**的授权里删 —— 因为角色链上的授权属于角色,
        不该因为"回收了某个用户"而消失。但有一个坑:``REVOKE ... FROM u``
        之后,如果 ``u`` 还挂着某个**恰好也持有同一条权限**的角色,那 ``u``
        实际上**仍然有**这条权限。这正是"回收要看整条链"的由来 ——
        我们用一条 docstring 把它讲清楚,并在 ``check_access`` 里如实体现
        (它算的是**并集**,不会因为某一条被删就以为权限没了)。
        """
        before = len(self.grants)
        self.grants = [
            g for g in self.grants
            if not (g.grantee == grantee and g.access_type == access_type
                    and g.database == database and g.table == table
                    and g.column == column)
        ]
        if len(self.grants) == before:
            raise_error(
                ErrorCode.ACCESS_DENIED,
                f"No privilege '{access_type}' ON {database}.{table} to revoke from {grantee}",
            )

    # ── SHOW GRANTS ────────────────────────────────────────────────────

    def show_grants(self, user: str) -> List[str]:
        """``SHOW GRANTS [FOR user]`` —— 一行一条授权语句。

        解释器会把返回的列表 ``"".join(...)`` 成**一个单元格**(见
        ``InterpreterShowQuery`` 里 ``SHOW GRANTS`` 的分支)。所以每条
        以分隔符结尾,拼起来才是一条条可读的语句。
        """
        lines: List[str] = []
        user_obj = self.users.get(user)
        # 该用户自己的角色
        for role in self.entity_roles.get(user, []):
            lines.append(f"GRANT {role} TO {user};\n")
        # 递归角色链上的角色授予
        for role in self.effective_roles(user):
            for sub in self.entity_roles.get(role, []):
                lines.append(f"GRANT {sub} TO {role};\n")
            if user_obj is not None and role in user_obj.default_roles_list:
                pass
        for grant in self.grants_for(user):
            priv = grant.access_type if grant.column is None else f"{grant.access_type}({grant.column})"
            stmt = f"GRANT {priv} ON {grant.database}.{grant.table} TO {grant.grantee}"
            if grant.grant_option:
                stmt += " WITH GRANT OPTION"
            lines.append(stmt + ";\n")
        lines.append("SHOW GRANTS FOR CURRENT_USER();\n")
        return lines

    def show_create_user(self, user: str) -> str:
        """``SHOW CREATE USER`` 的语句文本。"""
        u = self.users.get(user)
        if u is None:
            raise_error(ErrorCode.UNKNOWN_IDENTIFIER, f"User {user} does not exist")
        parts = [f"CREATE USER {u.name}"]
        if u.auth_method == "no_password":
            parts.append("IDENTIFIED WITH no_password")
        elif u.auth_method == "sha256_password":
            parts.append("IDENTIFIED WITH sha256_password BY '<hidden>'")
        elif u.auth_method == "plaintext_password":
            parts.append(f"IDENTIFIED WITH plaintext_password BY '{u.password_plaintext}'")
        if u.default_roles_all:
            parts.append("DEFAULT ROLE ALL")
        elif u.default_roles_list:
            parts.append("DEFAULT ROLE " + ", ".join(u.default_roles_list))
        if u.host_pattern and u.host_pattern.upper() != "ANY":
            parts.append(f"HOST {u.host_pattern}")
        parts.append(f"DEFAULT DATABASE {u.default_database}")
        return " ".join(parts)

    # ── 行策略 / 列策略 ────────────────────────────────────────────────

    def row_policies_for(self, database: str, table: str, user: str) -> List[RowPolicy]:
        """找出对 ``(db, t, user)`` 生效的行策略。"""
        out: List[RowPolicy] = []
        for policy in self.row_policies.values():
            if policy.database != database or policy.table != table:
                continue
            if policy.grantees and user not in policy.grantees \
                    and not any(r in policy.grantees for r in self.effective_roles(user)):
                continue
            out.append(policy)
        return out

    def column_policy(self, database: str, table: str, user: str) -> Optional[SettingsProfile]:
        """取该用户在该表上生效的设置档(用于列掩码)。"""
        u = self.users.get(user)
        if u is not None and u.settings_profile:
            return self.settings_profiles.get(u.settings_profile)
        return None


# ══════════════════════════════════════════════════════════════════════════
# 子句解析 —— 解析器把访问控制语句的尾巴收成一串"原始 token"
# ══════════════════════════════════════════════════════════════════════════
#
# .. note::
#
#    ``minich/sql/parser.py`` 的 ``_parse_create_access`` **有意**不做完整的
#    访问控制语法:它把 ``CREATE USER ...`` 里名字之后的所有 token 原样
#    收进 ``clauses``(一个字符串列表)。所以我们在这里做一层**健壮但保守**
#    的解析 —— 认得的子句结构化,认不得的就跳过。真实 ClickHouse 有
#    一整套专门的 Parser,这里是为了"机制讲清楚"而做的取舍。

_USER_CLAUSE_KEYWORDS = {
    "IDENTIFIED", "HOST", "DEFAULT", "SETTINGS", "GRANTEES", "VALID",
}


def _split_on(on: str) -> Tuple[str, str]:
    """把 ``GRANT ... ON <on>`` 里的 ``on`` 拆成 ``(database, table)``。"""
    text = on.strip()
    if not text:
        return "*", "*"
    if "(" in text:
        text = text.split("(", 1)[0]
    if "." in text:
        db, _, tbl = text.partition(".")
        return db or "*", tbl or "*"
    return "*", text


def _expand_privileges(privileges: Sequence[str]) -> List[Tuple[str, Optional[str]]]:
    """把 ``["SELECT(a, b)", "INSERT", "ALL"]`` 展平成 ``[(类型, 列), …]``。

    列级权限 ``SELECT(a, b)`` 会被拆成**两条** ``("SELECT", "a")`` /
    ``("SELECT", "b")`` —— 和真实 ClickHouse 一条列授一行一致。
    """
    out: List[Tuple[str, Optional[str]]] = []
    for raw in privileges:
        text = str(raw).strip()
        if "(" in text and text.endswith(")"):
            base, _, cols = text.partition("(")
            cols = cols[:-1]
            for col in [c.strip() for c in cols.split(",") if c.strip()]:
                out.append((base.upper(), col))
        else:
            out.append((text.upper(), None))
    return out


def _parse_settings_clauses(clauses: Sequence[str]) -> Dict[str, Any]:
    """解析 ``SETTINGS k = v, k2 = v2``。"""
    out: Dict[str, Any] = {}
    i = 0
    n = len(clauses)
    if n and str(clauses[0]).upper() == "SETTINGS":
        i = 1
    while i < n:
        name = str(clauses[i])
        if name.upper() in ("INHERIT", "READONLY", "CONST", "PROFILE", "MIN", "MAX"):
            # 设置档修饰符 —— 教学实现里不改变行为,跳过修饰词
            i += 1
            continue
        if i + 1 < n and str(clauses[i + 1]) == "=":
            out[name] = clauses[i + 2] if i + 2 < n else None
            i += 3
        else:
            i += 1
    return out


def _grantees_after(clauses: Sequence[str], keyword: str) -> List[str]:
    """取 ``... TO a, b`` / ``... FROM a, b`` 后面的被授者列表。"""
    out: List[str] = []
    i = 0
    n = len(clauses)
    while i < n:
        if str(clauses[i]).upper() == keyword.upper():
            j = i + 1
            while j < n and str(clauses[j]).upper() not in ("TO", "FROM"):
                word = str(clauses[j])
                if word != ",":
                    out.append(word)
                j += 1
            break
        i += 1
    return out


def _parse_quota_intervals(clauses: Sequence[str]) -> List[QuotaInterval]:
    """解析 ``FOR INTERVAL n unit MAX queries = N, errors = N, …``。

    可能有多段 ``FOR INTERVAL``(每个时间窗一段)。我们逐段扫。
    """
    intervals: List[QuotaInterval] = []
    tokens = [str(c) for c in clauses]
    i = 0
    n = len(tokens)
    current: Optional[QuotaInterval] = None
    while i < n:
        up = tokens[i].upper()
        if up == "FOR":
            i += 1
            if i < n and tokens[i].upper() == "INTERVAL":
                i += 1
                amount = tokens[i] if i < n else "1"
                i += 1
                unit = tokens[i] if i < n else "second"
                i += 1
                current = QuotaInterval(_unit_to_seconds(amount, unit))
                intervals.append(current)
            continue
        if up == "MAX":
            i += 1
            if current is None:
                current = QuotaInterval(3600)
                intervals.append(current)
            if i < n:
                metric = tokens[i].lower()
                i += 1
                if i < n and tokens[i] == "=":
                    i += 1
                value = tokens[i] if i < n else "0"
                i += 1
                try:
                    current.limits[metric] = int(float(value))
                except (TypeError, ValueError):
                    pass
            continue
        i += 1
    return intervals


def _unit_to_seconds(amount: str, unit: str) -> int:
    try:
        n = int(float(amount))
    except (TypeError, ValueError):
        n = 1
    unit = unit.lower()
    mult = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}.get(unit)
    if mult is None:
        raise_error(
            ErrorCode.UNSUPPORTED_PARAMETER,
            f"Unsupported quota interval unit '{unit}' "
            f"(expected second/minute/hour/day)",
        )
    return n * mult


def _parse_row_policy(name: str, clauses: Sequence[str]) -> Tuple[str, str, str, bool, bool]:
    """解析行策略的尾巴,返回 ``(database, table, filter_sql, restrictive, for_select)``。

    ``clauses`` 形如 ``['ON','default','.','t','USING','a','>','0']``。注意
    **字符串字面量的引号在解析器那一层已经丢了**(它只留 token 的 value),
    所以带字符串常量的过滤表达式会失真 —— 这是已知限制,写在报告里。
    """
    tokens = [str(c) for c in clauses]
    database, table = "default", ""
    restrictive = False
    for_select = True
    filter_sql = ""
    i = 0
    n = len(tokens)
    while i < n:
        up = tokens[i].upper()
        if up == "ON":
            i += 1
            name_parts: List[str] = []
            while i < n and tokens[i].upper() not in ("USING", "FOR", "TO", "AS", "WHERE"):
                name_parts.append(tokens[i])
                i += 1
            joined = "".join(name_parts)
            if "." in joined:
                database, _, table = joined.partition(".")
            else:
                table = joined
        elif up == "USING":
            i += 1
            expr_parts: List[str] = []
            while i < n and tokens[i].upper() not in ("FOR", "TO", "AS"):
                expr_parts.append(tokens[i])
                i += 1
            filter_sql = " ".join(expr_parts)
        elif up == "AS":
            i += 1
            if i < n:
                restrictive = tokens[i].upper() == "RESTRICTIVE"
                i += 1
        elif up == "FOR":
            i += 1
            if i < n:
                for_select = tokens[i].upper() in ("SELECT", "ALL")
                i += 1
        else:
            i += 1
    return database, table, filter_sql, restrictive, for_select


# ══════════════════════════════════════════════════════════════════════════
# 模块级入口 —— 解释器/调用方用这些
# ══════════════════════════════════════════════════════════════════════════


def get_access_control(ctx: Any) -> AccessControl:
    """取(必要时创建)``ctx`` 上的访问控制单例。

    真实 ClickHouse 里 ``AccessControl`` 是**进程单例**(整个 server 一个);
    mini-clickhouse 挂在 ``Context`` 上,这样"一个 Context = 一个 server 实例"
    的模型保持一致,测试之间也不会互相污染。
    """
    access = getattr(ctx, "access", None)
    if access is None:
        access = AccessControl()
        try:
            ctx.access = access
        except Exception:  # pragma: no cover - 极少数不可写 ctx
            pass
    return access


def _resolve_access(ctx: Any) -> Optional[AccessControl]:
    return getattr(ctx, "access", None)


def _user_of(ctx: Any, user: Optional[str]) -> str:
    return user if user is not None else (getattr(ctx, "user", None) or "default")


def check_access(
    ctx: Any,
    access_type: str,
    database: Optional[str] = None,
    table: Optional[str] = None,
    columns: Optional[Sequence[str]] = None,
    user: Optional[str] = None,
) -> None:
    """强制的权限检查入口 —— 无权限时抛 ``ACCESS_DENIED``。

    .. note::

       **只有装了 AccessControl 的 Context 才会被检查。** 没装(``ctx.access``
       为 ``None``)时这是**空操作** —— mini-clickhouse 默认不强制(见模块
       开头的 warning)。当前解释器不主动调用它;这个入口是留给"想要强制"
       的调用方(以及测试)的。
    """
    access = _resolve_access(ctx)
    if access is None:
        return
    name = _user_of(ctx, user)
    db = database if database is not None else getattr(ctx, "database", "")
    tbl = table or ""
    if not access.check(name, access_type, db, tbl, columns):
        where = f"database {db}" if not tbl else f"database {db}, table {tbl}"
        if columns:
            where += f", columns ({', '.join(columns)})"
        raise_error(
            ErrorCode.ACCESS_DENIED,
            f"{name}: Access denied for user {name} to {where}. "
            f"Required privilege: {access_type}",
        )


def has_access(
    ctx: Any,
    access_type: str,
    database: Optional[str] = None,
    table: Optional[str] = None,
    columns: Optional[Sequence[str]] = None,
    user: Optional[str] = None,
) -> bool:
    """``check_access`` 的"不抛异常"版本。"""
    access = _resolve_access(ctx)
    if access is None:
        return True
    name = _user_of(ctx, user)
    db = database if database is not None else getattr(ctx, "database", "")
    return access.check(name, access_type, db, table or "", columns)


def apply_row_policies(ctx: Any, table: Any, block: Any, user: Optional[str] = None) -> Any:
    """按行策略**过滤**一个 Block —— 原地过滤,返回同一个 Block。

    流程:

    .. code-block:: text

       for policy in 生效的行策略(db, t, user):
           filter_sql  →  AST(parse_expression_text)
                       →  Scope(把 block 的列绑成变量)
                       →  Evaluator 求值出一个布尔列
           block.filter(mask)     ← 就地删掉"策略判为假"的行
    """
    access = _resolve_access(ctx)
    if access is None or block is None:
        return block
    database = getattr(table, "database", "") or ""
    name = _user_of(ctx, user)
    policies = access.row_policies_for(database, getattr(table, "name", ""), name)
    if not policies:
        return block

    from .execution.expression import Evaluator, Scope

    for policy in policies:
        if not policy.filter:
            continue
        mask = _evaluate_filter(ctx, block, policy.filter)
        if mask is not None:
            block.filter(mask)
    return block


def _evaluate_filter(ctx: Any, block: Any, filter_sql: str) -> Optional[List[bool]]:
    """把一段过滤 SQL 在 block 上求值成布尔掩码;失败返回 ``None``(不拦)。"""
    from .sql import parse_expression_text
    from .execution.expression import Evaluator, Scope

    try:
        node = parse_expression_text(filter_sql)
    except MiniChError:
        return None
    scope = Scope()
    for c in block.data:
        scope.add_column(c.name, c)
    try:
        result = Evaluator(ctx, scope, block.rows()).evaluate(node)
    except MiniChError:
        return None
    col = result.column
    return [bool(col.get(i)) for i in range(block.rows())]


def apply_column_policies(ctx: Any, table: Any, block: Any,
                          user: Optional[str] = None) -> Any:
    """按列策略**掩码/删除**列。

    真实 ClickHouse 用 ``CREATE SETTINGS PROFILE ... READONLY`` 或列级
    ``GRANT`` 控制"能不能看某列"。这里实现一个**保守版本**:对每个
    设置了 ``mask`` 的列(设置档里形如 ``mask_column = 'xxx'`` 或
    ``hide_column = 1``),把整列替换成该类型的默认值 / 直接删列。
    """
    access = _resolve_access(ctx)
    if access is None or block is None:
        return block
    database = getattr(table, "database", "") or ""
    name = _user_of(ctx, user)
    profile = access.column_policy(database, getattr(table, "name", ""), name)
    if profile is None:
        return block
    for col_name, value in list(profile.elements.items()):
        key = str(col_name)
        if not (key.startswith("mask_") or key.startswith("hide_")):
            continue
        target = key.split("_", 1)[1]
        idx = block.get_position_by_name(target)
        if idx < 0:
            continue
        if key.startswith("hide_"):
            block.erase(target)
        else:
            cwtn = block.data[idx]
            dtype = cwtn.type
            replacement = dtype.create_column()
            replacement.append_many([dtype.default_value()] * block.rows())
            cwtn.column = replacement
    return block

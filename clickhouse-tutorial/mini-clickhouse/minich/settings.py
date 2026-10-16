"""设置(Settings)系统 —— 一张**有类型、有约束、分层覆盖**的表,而不是字符串字典。

很多第一次用 ClickHouse 的人会以为“设置”就是一个 ``dict[str, str]``。
其实不是。每一条设置都带有:

* **类型** —— ``UInt64`` / ``Bool`` / ``Seconds`` / ``Enum8(...)`` …,
  ``SET`` 时要按类型校验,``SET max_threads = 'abc'`` 会直接报错;
* **默认值**与**约束** —— ``min`` / ``max``,超出范围报
  ``SETTING_CONSTRAINT_VIOLATION``;
* **级别/重要性** —— 普通(``Regular``)、``IMPORTANT``、``OBSOLETE``;
* **只读**、**别名**等属性;
* 以及它能在**哪一层**被设置。

把设置做成“有类型的表”而不是字典,换来的是**能在写错的那一刻就报错**:
``SET max_threads = 'abc'`` 不该等到半夜跑到一半才炸。


四层作用域与解析顺序
---------------------

同一条查询里,一个设置的“最终值”是下面五层**从下往上覆盖**得到的
(越靠上优先级越高):

.. code-block:: text

    优先级 高
      ┌────────────────────────────────────────────────┐
      │ 1. query    SELECT ... SETTINGS max_threads = 4 │  只影响这一条查询
      ├────────────────────────────────────────────────┤
      │ 2. session  SET max_threads = 4                 │  影响当前连接(会话)
      ├────────────────────────────────────────────────┤
      │ 3. profile  SETTINGS PROFILE / users.xml 的档   │  用户所属的设置档
      ├────────────────────────────────────────────────┤
      │ 4. server   config.xml <profiles> 里的默认值    │  服务端配置的默认
      ├────────────────────────────────────────────────┤
      │ 5. default  代码里的内置默认 SettingDef.default │  最后的兜底
      └────────────────────────────────────────────────┘
    优先级 低

本模块用 ``Settings(base=...)`` 表达这种“层叠”:``session`` 层的对象把
``server`` 层的对象当成 ``base``;``get`` 时先看本层有没有显式设过,
没有就问 ``base``,``base`` 也没有才用内置默认值。``clone()`` 复制出一层
新的覆盖。这样“会话改了什么”“相对默认改了什么”都能算出来
(见 :meth:`Settings.changed`)。


为什么 ``max_block_size`` 是 65409?
------------------------------------

``65409 = 65536 - 127``。(``max_insert_block_size`` 同理:
``1048449 = 1048576 - 127``。)为什么不用整整齐齐的 65536?

* 一个 **2 的幂**的行数,会让每一列的“跨行步长”也接近 2 的幂,
  访问同一列相邻行时地址低位大量重复,容易映射到同一组 cache set,
  造成 **cache 冲突缺失(cache associativity thrashing)**;
* 65536 又是常见 SIMD 宽度(16 / 32 字节)的整数倍,导致尾部循环
  没有剩余元素、向量化路径的边界处理与手工展开都不理想。

减去 ``127 = 2**7 - 1`` 之后,块大小变成奇数、不再是任何 2 的幂的倍数,
从而同时避开上面两点;而少 127 行(约 0.2%)对吞吐几乎没有影响。

.. note::

   “必须不是 SIMD 宽度的整数倍,同时留出一点 padding 余量”是这件事
   **流传最广**的解释。mini-clickhouse 的工作区里没有上游 ``Settings.cpp``
   可供核对,所以这里按“广为人知的解释”呈现,而不是断言它就是源码注释。


``system.settings`` 的形状
--------------------------

:meth:`Settings.as_rows` 的每一行对应 ``system.settings`` 的一列:

.. code-block:: text

   name | value | changed | description | min | max | type | default | readonly | is_obsolete | tier

其中 ``type`` 对枚举会渲染成 ``Enum8('throw', 'break')`` 这种形式
(取值集合来自 :attr:`SettingDef.param`)。别名(``allow_experimental_*``
指向 ``enable_*``)会**单独占一行**,``tier`` 标成 ``Obsolete``,
方便读者在表里直接看到“这个名字已被取代”。


mini-clickhouse 有意简化的地方
------------------------------

* ``Bool`` 用 ``0`` / ``1`` 表示(和 ``system.settings`` 一致),
  而不是 Python 的 ``True`` / ``False``;
* 真实 CH 的 ``max_threads`` 类型是 ``MaxThreads``(能写 ``'auto'`` /
  ``'max'``),本模块按接口约定用 ``UInt64``;
* 错误编号沿用 :mod:`minich.errors` 的表,其中有几处与上游并不相同。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .errors import ErrorCode, error_code, raise_error

__all__ = [
    "SettingType",
    "SettingDef",
    "Settings",
    "DEFAULT",
    "register_setting",
    "register_alias",
    "get_setting_def",
    "registered_settings",
    "canonical_name",
]

#: 只读相关的错误码(164,``READONLY``)。:mod:`minich.errors` 的错误码表里有
#: 这一项,但 ``ErrorCode`` 类没有把它暴露成属性,所以这里按**名字**取,
#: 而不是硬编码 164。
_READONLY_CODE = error_code("READONLY")


# ─────────────────────────────────────────────────────────────────────────────
# 类型
# ─────────────────────────────────────────────────────────────────────────────


class SettingType(str, Enum):
    """一条设置声明的类型。

    继承 ``str`` 是为了让 ``SettingType.UInt64 == "UInt64"`` 成立,
    方便直接拼进 SQL / 系统表输出。

    真实 ClickHouse 的类型更多(``MaxThreads``、``LoadBalancing``、
    ``TotalsMode``、``Seconds`` 带单位解析等),这里按教学接口约定收敛成
    下面 11 种。其中 ``Seconds`` / ``Milliseconds`` 在 mini-clickhouse 里
    就是“单位固定的整数”,与 ``UInt64`` 的校验方式一致;``Enum8`` 用
    :attr:`SettingDef.param` 保存取值集合。
    """

    UInt64 = "UInt64"
    Int64 = "Int64"
    Float = "Float"
    Bool = "Bool"
    String = "String"
    BoolAuto = "BoolAuto"
    Seconds = "Seconds"
    Milliseconds = "Milliseconds"
    Timezone = "Timezone"
    Char = "Char"
    Enum8 = "Enum8"


@dataclass
class SettingDef:
    """一条设置的**定义**(与它的**当前取值**是两回事,取值存在 :class:`Settings` 里)。"""

    name: str
    type: SettingType
    default: Any
    description: str
    min: Any = None
    max: Any = None
    aliases: Tuple[str, ...] = ()
    is_obsolete: bool = False
    is_readonly: bool = False
    tier: str = "Regular"
    #: ``Enum8`` 风格的取值集合,逗号分隔,如 ``"throw,break,any"``。
    param: str = ""

    @property
    def type_name(self) -> str:
        """渲染给 ``system.settings.type`` 用的类型名。

        枚举会带上取值集合:``Enum8(throw,break)``。
        """
        if self.type is SettingType.Enum8:
            return f"Enum8({self.param})" if self.param else "Enum8"
        return self.type.value

    @property
    def enum_values(self) -> List[str]:
        """``Enum8`` 的取值列表;非枚举返回空列表。"""
        if self.type is not SettingType.Enum8 or not self.param:
            return []
        return [p.strip() for p in self.param.split(",") if p.strip()]


# ─────────────────────────────────────────────────────────────────────────────
# 注册表
# ─────────────────────────────────────────────────────────────────────────────

#: 规范名 -> 定义。
_SETTINGS: Dict[str, SettingDef] = {}

#: 别名 -> (规范名, 备注)。真实 CH 里 ``allow_experimental_*`` 这类旧名
#: 会解析到新的规范名(如 ``enable_analyzer``)。
_ALIASES: Dict[str, Tuple[str, str]] = {}

#: 合法的设置作用域。
_SCOPES: Tuple[str, ...] = ("query", "session", "profile", "server")

#: ``SET name = DEFAULT`` 里的 ``DEFAULT`` 哨兵 —— 表示“恢复默认值”。
#: 因为 ``set`` 收到的是从 SQL 解析来的**原始值**,一个裸的 ``"DEFAULT"``
#: 字符串(未加引号)也当作哨兵处理;加了引号的 ``'default'`` 则是一个
#: 普通字符串值(``join_algorithm`` 的旧值恰好就叫 ``default``)。
DEFAULT: Any = object()


def register_setting(
    name: str,
    type: SettingType,
    default: Any,
    description: str,
    **kw: Any,
) -> None:
    """注册一条设置。

    ``kw`` 可传 :class:`SettingDef` 的其余字段(``min`` / ``max`` /
    ``aliases`` / ``is_obsolete`` / ``is_readonly`` / ``tier`` / ``param``)。

    .. note::

       **默认值不经过约束校验**。这不是偷懒:真实 ClickHouse 的
       ``max_threads`` 默认就是 ``0``(表示“自动”),而它作为用户可写的值
       的下限却是 ``1`` —— 默认值走的是另一条不经 ``checkConstraint`` 的路径。
       所以这里也允许 ``default < min``。
    """
    if name in _SETTINGS:
        raise_error(ErrorCode.LOGICAL_ERROR_IN_MINICH, f"Setting '{name}' is already registered")
    aliases = tuple(kw.pop("aliases", ()))
    defn = SettingDef(name=name, type=type, default=default, description=description, **kw)
    _SETTINGS[name] = defn
    for alias in aliases:
        register_alias(alias, name)


def register_alias(alias: str, target: str, note: str = "") -> None:
    """把一个别名登记到规范名 ``target`` 上。

    ``get_setting_def(alias)`` 会解析到 ``target`` 的定义,
    ``Settings.set(alias, v)`` 也会写到 ``target`` 上。
    """
    if target not in _SETTINGS:
        raise_error(
            ErrorCode.LOGICAL_ERROR_IN_MINICH,
            f"Alias '{alias}' points to unregistered setting '{target}'",
        )
    _ALIASES[alias] = (target, note)


def canonical_name(name: str) -> Optional[str]:
    """把名字解析成**规范名**;没注册过返回 ``None``。"""
    if name in _SETTINGS:
        return name
    hit = _ALIASES.get(name)
    return hit[0] if hit else None


def get_setting_def(name: str) -> Optional[SettingDef]:
    """取一条设置的定义;别名会解析到目标设置的**规范定义**。"""
    canonical = canonical_name(name)
    return _SETTINGS[canonical] if canonical else None


def registered_settings() -> Dict[str, SettingDef]:
    """当前注册表的**快照**(只含规范名,不含别名)。"""
    return dict(_SETTINGS)


# ─────────────────────────────────────────────────────────────────────────────
# 取值转换 —— 严格到能抓住真错误,又接受 ClickHouse 接受的写法
# ─────────────────────────────────────────────────────────────────────────────

#: 时区名:``UTC``、``Asia/Shanghai`` 这类 IANA 名字(允许 ``+`` / ``-``)。
_TZ_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*$")

_TRUE_WORDS = frozenset({"true", "1", "on", "yes"})
_FALSE_WORDS = frozenset({"false", "0", "off", "no"})


def _parse_fail(name: str, value: Any, typename: str) -> None:
    raise_error(
        ErrorCode.CANNOT_PARSE_TEXT,
        f"Cannot parse '{value}' as {typename} for setting '{name}'",
    )


def _to_int(value: Any, name: str, typename: str) -> int:
    if isinstance(value, bool):  # 注意:bool 是 int 的子类,必须先判
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        _parse_fail(name, value, typename)
    if isinstance(value, str):
        s = value.strip()
        try:
            if s.lower().lstrip("+-").startswith("0x"):
                return int(s, 16)
            return int(s, 10)
        except ValueError:
            _parse_fail(name, value, typename)
    _parse_fail(name, value, typename)
    raise AssertionError("unreachable")  # pragma: no cover


def _to_float(value: Any, name: str) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            _parse_fail(name, value, "Float")
    _parse_fail(name, value, "Float")
    raise AssertionError("unreachable")  # pragma: no cover


def _to_bool(value: Any, name: str) -> int:
    """布尔值统一用 ``0`` / ``1`` 表示(与 ``system.settings`` 一致)。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        if value in (0, 1):
            return value
        _parse_fail(name, value, "Bool")
    if isinstance(value, str):
        s = value.strip().lower()
        if s in _TRUE_WORDS:
            return 1
        if s in _FALSE_WORDS:
            return 0
    _parse_fail(name, value, "Bool")
    raise AssertionError("unreachable")  # pragma: no cover


def _to_bool_auto(value: Any, name: str) -> Any:
    """``BoolAuto``:``true`` / ``false`` / ``auto`` 三态。"""
    if isinstance(value, str) and value.strip().lower() == "auto":
        return "auto"
    return _to_bool(value, name)


def _to_enum(defn: SettingDef, value: Any) -> str:
    text = value if isinstance(value, str) else str(value)
    low = text.strip().lower()
    for candidate in defn.enum_values:
        if candidate.lower() == low:
            return candidate  # 存规范写法,大小写不敏感地匹配
    raise_error(
        ErrorCode.CANNOT_PARSE_TEXT,
        f"Unknown value '{value}' for setting '{defn.name}'. "
        f"Allowed values: {', '.join(defn.enum_values)}",
    )
    raise AssertionError("unreachable")  # pragma: no cover


def _coerce(defn: SettingDef, value: Any) -> Any:
    """把从 SQL 拿到的**原始值**转成该设置声明的类型。

    非法输入会抛 :class:`minich.errors.MiniChError`,消息里一定带设置名 ——
    不然用户面对一屏设置根本不知道改错了哪条。
    """
    t = defn.type
    name = defn.name

    if t is SettingType.String:
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float)):
            return str(value)
        if value is None:
            return ""
        _parse_fail(name, value, "String")

    if t is SettingType.Timezone:
        s = value if isinstance(value, str) else ("" if value is None else str(value))
        s = s.strip()
        if s and not _TZ_RE.match(s):
            _parse_fail(name, value, "Timezone")
        return s

    if t is SettingType.Char:
        s = value if isinstance(value, str) else ("" if value is None else str(value))
        if len(s) != 1:
            raise_error(
                ErrorCode.CANNOT_PARSE_TEXT,
                f"Value for setting '{name}' must be a single character",
            )
        return s

    if t in (SettingType.UInt64, SettingType.Seconds, SettingType.Milliseconds):
        iv = _to_int(value, name, t.value)
        if iv < 0:
            _parse_fail(name, value, t.value)
        return iv

    if t is SettingType.Int64:
        return _to_int(value, name, "Int64")

    if t is SettingType.Float:
        return _to_float(value, name)

    if t is SettingType.Bool:
        return _to_bool(value, name)

    if t is SettingType.BoolAuto:
        return _to_bool_auto(value, name)

    if t is SettingType.Enum8:
        return _to_enum(defn, value)

    raise_error(  # pragma: no cover - 防御性
        ErrorCode.LOGICAL_ERROR_IN_MINICH,
        f"No coercion implemented for setting '{name}' of type {t}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Settings —— 一层(可能带 base)的可变设置集合
# ─────────────────────────────────────────────────────────────────────────────


class Settings:
    """一“层”设置。每一层只保存**被显式设过**的值,其余交给 ``base`` 或默认值。

    .. code-block:: python

       server = Settings()                 # 服务端默认(空的一层,只读默认值)
       session = Settings(base=server)     # 会话层
       session.set("max_threads", "8")     # SET max_threads = '8'
       session.max_threads                 # 8(属性式访问)
       query = session.clone()
       query.apply_dict({"max_threads": 16})   # SELECT ... SETTINGS max_threads = 16
    """

    def __init__(self, base: Optional["Settings"] = None) -> None:
        #: 被继承的下一层(session 继承 server 默认)。``None`` 表示终点。
        self._base = base
        #: 规范名 -> 本层显式设过的值。
        self._values: Dict[str, Any] = {}
        #: 规范名 -> 最近一次设置的作用域(便于排错 / 打印)。
        self._scopes: Dict[str, str] = {}

    # ── 读 ────────────────────────────────────────────────────────────────

    def _lookup(self, canonical: str) -> Any:
        """按“本层 -> base -> 内置默认”的顺序取值。``canonical`` 必须是规范名。"""
        if canonical in self._values:
            return self._values[canonical]
        if self._base is not None:
            return self._base.get(canonical)
        return _SETTINGS[canonical].default

    def get(self, name: str) -> Any:
        """取设置的**有效值**;名字没注册过则抛 ``UNKNOWN_SETTING``。"""
        canonical = canonical_name(name)
        if canonical is None:
            raise_error(ErrorCode.UNKNOWN_SETTING, f"Unknown setting '{name}'")
        return self._lookup(canonical)

    def try_get(self, name: str, default: Any = None) -> Any:
        """和 :meth:`get` 一样,但未注册的名字返回 ``default`` 而不报错。"""
        canonical = canonical_name(name)
        if canonical is None:
            return default
        return self._lookup(canonical)

    def get_def(self, name: str) -> Optional[SettingDef]:
        """取设置的定义(等价于模块级 :func:`get_setting_def`)。"""
        return get_setting_def(name)

    # ── 写 ────────────────────────────────────────────────────────────────

    def set(self, name: str, value: Any, *, scope: str = "session") -> None:
        """设置一个值,顺带做类型 / 范围 / 只读校验。

        * ``value`` 可以是字符串(``SET max_threads = '8'`` 合法),会被转成
          声明的类型;
        * ``value`` 是 :data:`DEFAULT` 或裸字符串 ``"DEFAULT"`` 时,
          **恢复默认**(从本层删掉,重新落到 base / 内置默认);
        * 别名(如 ``allow_experimental_analyzer``)会写到底层规范名
          (``enable_analyzer``)上。
        """
        if scope not in _SCOPES:
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"Unknown settings scope '{scope}'. Expected one of: {', '.join(_SCOPES)}",
            )
        canonical = canonical_name(name)
        if canonical is None:
            raise_error(ErrorCode.UNKNOWN_SETTING, f"Unknown setting '{name}'")
        defn = _SETTINGS[canonical]

        # 只读:1) 该设置本身被标成 read-only;2) 会话处在 readonly=1。
        if defn.is_readonly:
            raise_error(
                _READONLY_CODE,
                f"Cannot modify '{name}' setting in readonly mode",
            )
        if scope in ("session", "query") and int(self._lookup("readonly") or 0) == 1:
            raise_error(
                _READONLY_CODE,
                f"Cannot modify '{name}' setting in readonly mode",
            )

        # SET name = DEFAULT —— 恢复默认。
        if value is DEFAULT or (isinstance(value, str) and value == "DEFAULT"):
            self._values.pop(canonical, None)
            self._scopes.pop(canonical, None)
            return

        coerced = _coerce(defn, value)

        if defn.min is not None and coerced < defn.min:
            raise_error(
                ErrorCode.SETTING_CONSTRAINT_VIOLATION,
                f"Value for setting '{name}' is too low. Minimum value: {defn.min}",
            )
        if defn.max is not None and coerced > defn.max:
            raise_error(
                ErrorCode.SETTING_CONSTRAINT_VIOLATION,
                f"Value for setting '{name}' is too high. Maximum value: {defn.max}",
            )

        self._values[canonical] = coerced
        self._scopes[canonical] = scope

    def apply_dict(self, d: Dict[str, Any], scope: str = "query") -> None:
        """批量设置 —— 对应 ``SELECT ... SETTINGS a = 1, b = 'x'``。"""
        for key, val in d.items():
            self.set(key, val, scope=scope)

    def reset(self, name: str) -> None:
        """显式恢复某条设置为默认(等价于 ``SET name = DEFAULT``)。"""
        canonical = canonical_name(name)
        if canonical is None:
            raise_error(ErrorCode.UNKNOWN_SETTING, f"Unknown setting '{name}'")
        self._values.pop(canonical, None)
        self._scopes.pop(canonical, None)

    # ── 观察 ──────────────────────────────────────────────────────────────

    def changed(self) -> Dict[str, Any]:
        """相对**内置默认值**改过的项 —— ``{规范名: 当前有效值}``。

        注意它反映的是**整条解析链**的结果:即使改动发生在 base 层,
        也会出现在这里(因为“相对默认”确实变了)。
        """
        out: Dict[str, Any] = {}
        for name, defn in sorted(_SETTINGS.items()):
            value = self._lookup(name)
            if value != defn.default:
                out[name] = value
        return out

    def as_rows(self) -> List[Tuple[Any, ...]]:
        """``system.settings`` 形状的行,按名字排序。

        列顺序:``name, value, changed, description, min, max, type,
        default, readonly, is_obsolete, tier``。别名会额外占一行。
        """
        rows: List[Tuple[Any, ...]] = [self._row(name, defn) for name, defn in _SETTINGS.items()]
        for alias, (target, _note) in _ALIASES.items():
            rows.append(self._row(alias, _SETTINGS[target], alias_of=target))
        rows.sort(key=lambda r: r[0])
        return rows

    def _row(self, name: str, defn: SettingDef, alias_of: Optional[str] = None) -> Tuple[Any, ...]:
        value = self._lookup(defn.name)
        changed = 1 if value != defn.default else 0
        if alias_of is None:
            description = defn.description
            tier = defn.tier
            is_obsolete = 1 if defn.is_obsolete else 0
        else:
            description = f"【别名】指向 {alias_of}"
            tier = "Obsolete"
            is_obsolete = 1
        return (
            name,                       # name
            value,                      # value
            changed,                    # changed
            description,                # description
            defn.min,                   # min
            defn.max,                   # max
            defn.type_name,             # type
            defn.default,               # default
            1 if defn.is_readonly else 0,  # readonly
            is_obsolete,                # is_obsolete
            tier,                       # tier
        )

    # ── 其它 ──────────────────────────────────────────────────────────────

    def clone(self) -> "Settings":
        """复制出**同样 base** 的一层新对象 —— 常用于“查询级覆盖不改会话”。"""
        dup = Settings(base=self._base)
        dup._values = dict(self._values)
        dup._scopes = dict(self._scopes)
        return dup

    def __getattr__(self, name: str) -> Any:
        """让 ``settings.max_threads`` 这种访问成立。

        未注册的名字抛 ``AttributeError``(而不是 ``UNKNOWN_SETTING``),
        这样 ``hasattr`` / ``getattr(..., default)`` 的行为才正常。
        """
        if name.startswith("_"):
            raise AttributeError(name)
        canonical = canonical_name(name)
        if canonical is None:
            raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")
        return self._lookup(canonical)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试
        base = "None" if self._base is None else "Settings(...)"
        return f"Settings(base={base}, overrides={len(self._values)})"


# ─────────────────────────────────────────────────────────────────────────────
# 默认注册表 —— 真实 ClickHouse 的名字、默认值与范围(目标版本 25.3)
# ─────────────────────────────────────────────────────────────────────────────


def _register_defaults() -> None:
    R = register_setting

    # ── 查询执行 / 性能 ───────────────────────────────────────────────────
    R("max_threads", SettingType.UInt64, 0,
      "执行一条查询最多用多少个线程。0 = 由服务端自动决定(注意:能写的下限是 1)。",
      min=1)
    R("max_block_size", SettingType.UInt64, 65409,
      "读取时单个块的最大行数(65536 - 127,原因见模块文档)。", min=1)
    R("min_block_size", SettingType.UInt64, 1024,
      "读取时单个块的最小行数。(默认值未确认)", min=1)
    R("max_insert_block_size", SettingType.UInt64, 1048449,
      "INSERT 时单个块的最大行数(1048576 - 127,和 max_block_size 同一套路)。", min=1)
    R("max_download_threads", SettingType.UInt64, 4,
      "下载数据(URL 引擎 / remote 表函数等)时使用的最大线程数。")
    R("max_parser_depth", SettingType.UInt64, 1000,
      "SQL 解析器允许的最大递归深度,防止恶意查询把解析栈打爆。")
    R("max_execution_time", SettingType.Seconds, 0,
      "单条查询的最长执行时间(秒)。0 = 不限制。")
    R("timeout_overflow_mode", SettingType.Enum8, "throw",
      "超过 max_execution_time 后:throw 抛错,break 返回已算出的部分结果。",
      param="throw,break")
    R("max_execution_speed", SettingType.UInt64, 0,
      "每秒最多处理多少行(降级 / 限速用)。0 = 不限速。")
    R("max_memory_usage", SettingType.UInt64, 0,
      "单条查询可用的最大内存(字节)。0 = 由服务端配置决定。")
    R("memory_overcommit_ratio_denominator", SettingType.UInt64, 1073741824,
      "计算“全局内存软上限”时用到的分母(字节)。(默认值未确认)")
    R("max_rows_to_read", SettingType.UInt64, 0, "一条查询最多能从表中读取多少行。0 = 不限。")
    R("read_overflow_mode", SettingType.Enum8, "throw",
      "读行数超限时:throw 抛错,break 返回部分结果。", param="throw,break")
    R("max_bytes_to_read", SettingType.UInt64, 0, "一条查询最多能读取多少字节。0 = 不限。")
    R("max_result_rows", SettingType.UInt64, 0, "结果集最多多少行。0 = 不限。")
    R("result_overflow_mode", SettingType.Enum8, "throw",
      "结果超限时:throw 抛错,break 截断。", param="throw,break")
    R("max_rows_to_group_by", SettingType.UInt64, 0,
      "GROUP BY 期间最多维护多少个聚合键。0 = 不限。")
    R("group_by_overflow_mode", SettingType.Enum8, "throw",
      "聚合键超限时:throw 抛错 / break 截断 / any 只保留前 N 个键。",
      param="throw,break,any")
    R("max_bytes_before_external_group_by", SettingType.UInt64, 0,
      "聚合状态超过该字节数就落盘(外部聚合)。0 = 不落盘。")
    R("max_bytes_before_external_sort", SettingType.UInt64, 0,
      "排序数据超过该字节数就落盘(外部排序)。0 = 不落盘。")
    R("max_bytes_in_join", SettingType.UInt64, 0,
      "JOIN 构建的哈希表最多占多少字节。0 = 不限。")
    R("join_overflow_mode", SettingType.Enum8, "throw",
      "JOIN 超限时:throw / break / any。", param="throw,break,any")
    R("max_rows_in_set", SettingType.UInt64, 0,
      "IN / 部分 JOIN 生成的 set 最多多少行。0 = 不限。")
    R("max_bytes_in_set", SettingType.UInt64, 0,
      "IN / 部分 JOIN 生成的 set 最多多少字节。0 = 不限。")

    # ── 聚合 ──────────────────────────────────────────────────────────────
    R("group_by_two_level_threshold", SettingType.UInt64, 100000,
      "聚合键数量超过该值时改用两级(可落盘)聚合。")
    R("group_by_two_level_threshold_bytes", SettingType.UInt64, 50000000,
      "聚合状态字节数超过该值时改用两级聚合。")
    R("optimize_aggregation_in_order", SettingType.Bool, 0,
      "数据已按 GROUP BY 键有序时,用流式(不建哈希表)聚合。")
    R("aggregation_in_order_max_block_bytes", SettingType.UInt64, 50000000,
      "有序聚合在流水线里最多累积多少字节的块。")
    R("distributed_aggregation_memory_efficient", SettingType.Bool, 1,
      "分布式聚合时使用更省内存的两阶段合并。")
    R("aggregate_functions_null_for_empty", SettingType.Bool, 0,
      "把空结果集上的聚合结果重写成 NULL(而不是默认值)。")
    R("optimize_group_by_function_keys", SettingType.Bool, 1,
      "优化 GROUP BY 中函数形式的键(如 f(x))。")

    # ── 排序 / LIMIT ──────────────────────────────────────────────────────
    R("optimize_read_in_order", SettingType.Bool, 1,
      "查询能利用主键顺序时按顺序读,省掉一次显式排序。")
    R("optimize_sorting_by_input_stream_properties", SettingType.Bool, 1,
      "利用输入流已有的有序性省掉排序。")
    R("read_in_order_two_level_merge_threshold", SettingType.UInt64, 100,
      "有序读取时,数据块数超过该值才切到两阶段合并流水线。")

    # ── JOIN ──────────────────────────────────────────────────────────────
    R("join_algorithm", SettingType.String, "direct,parallel_hash,hash",
      "JOIN 算法列表,按顺序取第一个适用的。旧值 'default' 已废弃且等价于 "
      "'direct,hash';24.12 起默认改为 direct,parallel_hash,hash。")
    R("join_use_nulls", SettingType.Bool, 0,
      "JOIN 未匹配到的列填 NULL(1)还是填该类型的默认值(0)。")
    R("max_rows_in_join", SettingType.UInt64, 0, "JOIN 哈希表最多多少行。0 = 不限。")
    R("join_default_strictness", SettingType.Enum8, "ALL",
      "JOIN 没写 ALL / ANY 时的默认严格度。", param="ALL,ANY,ASOF")
    R("any_join_distinct_right_table_keys", SettingType.Bool, 0,
      "ANY JOIN 是否对右表键去重(兼容旧行为)。")
    R("prefer_global_in_and_join", SettingType.Bool, 0,
      "是否把普通的 IN / JOIN 默认当成 GLOBAL IN / GLOBAL JOIN。")

    # ── MergeTree / 存储 ──────────────────────────────────────────────────
    R("index_granularity", SettingType.UInt64, 8192,
      "每个索引粒度包含多少行。建表时确定,之后不可改(它决定了磁盘布局)。")
    R("index_granularity_bytes", SettingType.UInt64, 10485760,
      "自适应粒度的字节上限(10 MiB)。")
    R("enable_mixed_granularity_parts", SettingType.Bool, 1,
      "是否启用自适应粒度(按字节而不是只按行)。")
    R("min_bytes_for_wide_part", SettingType.UInt64, 10485760,
      "part 超过该字节数才用 wide 布局(每列一个文件)。")
    R("min_rows_for_wide_part", SettingType.UInt64, 0,
      "part 超过该行数才用 wide 布局。0 = 只看字节数。")
    R("max_parts_in_total", SettingType.UInt64, 100000,
      "一张表所有分区加起来最多允许多少个活跃 part。")
    R("parts_to_delay_insert", SettingType.UInt64, 1000,
      "单分区活跃 part 超过该值时,插入开始被指数退避式限速。"
      "(23.6 之前默认 150。)")
    R("parts_to_throw_insert", SettingType.UInt64, 3000,
      "单分区活跃 part 超过该值时,插入直接报 Too many parts。"
      "(23.6 之前默认 300。)")
    R("max_part_loading_threads", SettingType.UInt64, 0,
      "加载 part 的线程数上限。**已废弃**:新版由服务端线程池设置"
      "(如 max_active_parts_loading_thread_pool_size)控制,此设置不再生效。",
      is_obsolete=True, tier="Obsolete")
    R("max_suspicious_broken_parts", SettingType.UInt64, 10,
      "允许的损坏 part 数量上限,超过则拒绝加载(防误删)。")
    R("merge_tree_min_rows_for_concurrent_read", SettingType.UInt64, 163840,
      "part 行数超过该值才允许被多线程并发读。")
    R("merge_tree_max_rows_to_use_cache", SettingType.UInt64, 1048576,
      "part 行数不超过该值时才使用未压缩数据缓存。")
    R("max_bytes_to_merge_at_max_space_in_pool", SettingType.UInt64, 161061273600,
      "后台合并能产生的最大 part 大小(150 GiB),是最关键的合并调参。")
    R("old_parts_lifetime", SettingType.Seconds, 480,
      "被替换掉的旧 part 在删除前保留多少秒,避免删掉正在被读的文件。")
    R("max_number_of_merges_with_ttl_in_pool", SettingType.UInt64, 2,
      "后台池中最多同时跑多少个带 TTL 的合并,给普通合并留线程。")
    R("ttl_only_drop_parts", SettingType.Bool, 0,
      "TTL 到期时整块丢弃 part(而不是逐行删)。")

    # ── 插入 / 格式 ───────────────────────────────────────────────────────
    R("input_format_skip_unknown_fields", SettingType.Bool, 0,
      "导入时遇到目标表没有的列是跳过(1)还是报错(0)。")
    R("input_format_allow_errors_num", SettingType.UInt64, 0,
      "导入时最多允许多少行解析失败。")
    R("input_format_allow_errors_ratio", SettingType.Float, 0.0,
      "导入时允许解析失败的比例上限(0..1)。", min=0.0, max=1.0)
    R("input_format_defaults_for_omitted_fields", SettingType.Bool, 1,
      "输入中缺省的列是否用列的默认值填充。")
    R("input_format_null_as_default", SettingType.Bool, 1,
      "把输入里的 NULL 当成该列的默认值。")
    R("input_format_csv_empty_as_default", SettingType.Bool, 1,
      "CSV 里的空字段当成列的默认值。")
    R("output_format_json_quote_64bit_integers", SettingType.Bool, 1,
      "JSON 输出时给 64 位整数加引号,避免被 JS 的 Number 丢精度。")
    R("output_format_json_quote_denormals", SettingType.Bool, 0,
      "JSON 输出时给 NaN / Infinity 加引号。")
    R("output_format_json_escape_forward_slashes", SettingType.Bool, 0,
      "JSON 输出时是否把正斜杠转义成 \\/。")
    R("date_time_input_format", SettingType.Enum8, "basic",
      "解析输入里日期时间字符串的宽松程度。", param="basic,best_effort,best_effort_us")
    R("date_time_output_format", SettingType.Enum8, "simple",
      "日期时间的输出格式。", param="simple,iso,unix_timestamp")
    R("format_csv_delimiter", SettingType.Char, ",",
      "CSV 的字段分隔符(必须是单个字符)。")
    R("output_format_pretty_row_numbers", SettingType.Bool, 0,
      "Pretty 格式是否在每行前面加行号。")
    R("output_format_pretty_max_column_pad_width", SettingType.UInt64, 250,
      "Pretty 格式里单个显示列的最大宽度。")
    R("output_format_pretty_max_rows", SettingType.UInt64, 10000,
      "Pretty 格式最多输出多少行。")

    # ── 优化器 ────────────────────────────────────────────────────────────
    R("optimize_move_to_prewhere", SettingType.Bool, 1,
      "把 WHERE 里能下推的过滤条件下推到 PREWHERE。")
    R("optimize_move_to_prewhere_if_final", SettingType.Bool, 0,
      "带 FINAL 的查询是否也做上面的下推。")
    R("optimize_throw_if_noop", SettingType.Bool, 0,
      "优化没有生效时是否直接报错(排查优化器用的开关)。")
    R("optimize_use_projections", SettingType.Bool, 1,
      "是否使用 projection 加速查询。")
    R("optimize_use_implicit_projections", SettingType.Bool, 1,
      "是否自动选用隐式 projection。")
    R("force_index_by_date", SettingType.Bool, 0,
      "没有主键条件时是否强制使用日期索引。")
    R("force_primary_key", SettingType.Bool, 0,
      "是否要求查询必须能用上主键,否则报错。")
    R("use_skip_indexes", SettingType.Bool, 1,
      "是否使用跳数索引(skip index)。")
    R("use_skip_indexes_if_final", SettingType.Bool, 0,
      "带 FINAL 时是否也使用跳数索引。25.6 起默认变为 1(版本相关)。")
    R("enable_optimize_predicate_expression", SettingType.Bool, 1,
      "是否把谓词下推到子查询内部。")
    R("optimize_functions_to_subcolumns", SettingType.Bool, 1,
      "是否把 mapKeys / arrayElement 这类函数改写成读子列。")
    R("optimize_trivial_count_query", SettingType.Bool, 1,
      "把不带条件的 count() 直接用主键元信息回答,不扫数据。")
    R("enable_early_constant_folding", SettingType.Bool, 1,
      "是否在分析早期做常量折叠。")

    # ── 函数 ──────────────────────────────────────────────────────────────
    R("function_json_value_return_type_allow_complex", SettingType.Bool, 0,
      "JSON_VALUE 这类函数是否允许返回复杂类型(对象 / 数组)。")

    # ── 复制 / 协调 ───────────────────────────────────────────────────────
    R("insert_quorum", SettingType.UInt64, 0,
      "INSERT 返回成功前要确认写入的副本数。0 = 不等待。")
    R("insert_quorum_timeout", SettingType.Milliseconds, 600000,
      "等待 insert_quorum 的超时(毫秒)。")
    R("select_sequential_consistency", SettingType.Bool, 0,
      "是否只从包含最新数据的副本读,保证顺序一致性。")
    R("insert_quorum_parallel", SettingType.Bool, 1,
      "是否允许并行的 insert quorum(更快,但一致性语义稍弱)。")
    R("max_replica_delay_for_distributed_queries", SettingType.Seconds, 300,
      "分布式查询里,副本延迟超过该秒数就不再使用它。")
    R("fallback_to_stale_replicas_for_distributed_queries", SettingType.Bool, 1,
      "找不到新鲜副本时,是否退回到陈旧的副本继续。")

    # ── 分布式 ────────────────────────────────────────────────────────────
    R("distributed_group_by_no_merge", SettingType.UInt64, 0,
      "分布式聚合是否跳过最终的合并(已知单分片时用)。")
    R("distributed_push_down_limit", SettingType.Bool, 1,
      "是否把 LIMIT 下推到各分片。")
    R("optimize_distributed_group_by_sharding_key", SettingType.Bool, 1,
      "GROUP BY 分片键时是否只做本地聚合,省掉网络搬运。")
    R("optimize_skip_unused_shards", SettingType.Bool, 0,
      "是否跳过用不到的分片(只对带分片键过滤的查询有效)。"
      "24.x 之后默认改为 1(版本相关)。")

    # ── 实验性 / 警告级 ───────────────────────────────────────────────────
    R("allow_experimental_database_replicated", SettingType.Bool, 0,
      "是否允许创建 Replicated 数据库引擎。")
    R("allow_experimental_object_type", SettingType.Bool, 0,
      "是否允许旧的 Object('json') 类型。(默认值未确认)")
    R("allow_experimental_full_text_index", SettingType.Bool, 0,
      "是否允许 full-text(text)索引。")
    R("allow_experimental_inverted_index", SettingType.Bool, 0,
      "是否允许旧的 inverted 索引(现已改名 text)。(默认值未确认)")
    R("allow_experimental_variant_type", SettingType.Bool, 1,
      "是否允许 Variant 类型。已生产可用,25.3 起默认开启(更早版本默认 0,版本相关)。")
    R("allow_experimental_dynamic_type", SettingType.Bool, 1,
      "是否允许 Dynamic 类型。已生产可用,25.3 起默认开启(更早版本默认 0,版本相关)。")

    # ── 别名:真实 CH 以 enable_* 为规范名,allow_experimental_* 是旧名 ──
    R("enable_analyzer", SettingType.Bool, 1,
      "是否启用新的查询分析器(analyzer)。",
      aliases=("allow_experimental_analyzer",))
    R("enable_json_type", SettingType.Bool, 1,
      "是否启用新版 JSON 类型。",
      aliases=("allow_experimental_json_type",))
    R("enable_lightweight_delete", SettingType.Bool, 1,
      "是否启用轻量级 DELETE(用 _row_exists 掩码,不重写 part)。",
      aliases=("allow_experimental_lightweight_delete",))

    # ── 日志 / 可观测性 ───────────────────────────────────────────────────
    R("log_queries", SettingType.Bool, 1, "是否把查询写进 query_log 系统表。")
    R("log_query_threads", SettingType.Bool, 0,
      "是否把每条查询的线程信息写进 query_thread_log。")
    R("log_profile_events", SettingType.Bool, 1,
      "query_log 里是否记录本次查询的 ProfileEvents 增量。")
    R("log_query_settings", SettingType.Bool, 1,
      "query_log 里是否记录本次查询用到的设置。")
    R("log_comment", SettingType.String, "",
      "给查询打的自定义备注,会出现在 query_log 里(便于定位)。")
    R("send_logs_level", SettingType.Enum8, "fatal",
      "服务端把不低于该级别的日志推给客户端。",
      param="none,fatal,error,warning,information,debug,trace")
    R("query_profiler_real_time_period_ns", SettingType.Int64, 1000000000,
      "实时(墙钟)查询剖析器的采样周期(纳秒)。0 = 关闭。")
    R("query_profiler_cpu_time_period_ns", SettingType.Int64, 1000000000,
      "CPU 时间查询剖析器的采样周期(纳秒)。0 = 关闭。")

    # ── 其它行为 ──────────────────────────────────────────────────────────
    R("max_parallel_replicas", SettingType.UInt64, 1,
      "一条查询最多并行使用多少个副本(需配合分布式表 / 分片)。")
    R("readonly", SettingType.UInt64, 0,
      "0 = 不限;1 = 只读且禁止改设置;2 = 只读但允许改设置。", max=2)
    R("use_uncompressed_cache", SettingType.Bool, 0,
      "是否使用未压缩数据块缓存(适合反复读小范围数据)。")
    R("load_balancing", SettingType.Enum8, "random",
      "选择副本时的负载均衡策略。",
      param="random,nearest_hostname,in_order,first_or_random,round_robin")
    R("prefer_localhost_replica", SettingType.Bool, 1,
      "有本地副本时是否优先读本地。")
    R("extremes", SettingType.Bool, 0,
      "是否额外计算结果集的最小 / 最大值(极值行)。")
    R("totals_mode", SettingType.Enum8, "after_having_exclusive",
      "WITH TOTALS 时小计行的计算方式。",
      param="after_having_exclusive,before_having,after_having_inclusive,after_having_auto,throw")
    R("totals_auto_threshold", SettingType.Float, 0.5,
      "totals_mode = after_having_auto 时,自动切换的阈值。", min=0.0, max=1.0)
    R("max_query_size", SettingType.UInt64, 262144,
      "一条查询文本的最大字节数(超出会报 Query is too large)。")
    R("interactive_delay", SettingType.UInt64, 100000,
      "报告查询进度之间至少间隔多少微秒。")
    R("session_timezone", SettingType.Timezone, "",
      "当前会话使用的时区。空字符串 = 用服务端时区。")
    R("use_client_time_zone", SettingType.Bool, 0,
      "是否把客户端的时区当成会话时区。")


_register_defaults()

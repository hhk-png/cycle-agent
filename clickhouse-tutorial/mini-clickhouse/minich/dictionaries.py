"""外部字典 —— ``CREATE DICTIONARY`` / ``dictGet`` 背后的那套"维表查找"机制。

字典不是表,也不是视图 —— 它是**住在查询引擎旁边的一张查找表**。
你给它一个**源**(数据从哪来)、一个**布局**(内存里怎么存)、一个
**生命周期**(多久刷新一次),然后就能在 SQL 里这么用:

.. code-block:: sql

   SELECT dictGet('geo', 'country', user_region) FROM hits;

.. warning::

   为什么要有字典? 因为"给事件流补维度"这件事在生产里每秒要做**几百万次**。
   用普通表做这件事意味着 ``JOIN``:构建哈希表、探测、处理 NULL、
   把两张表拉齐 —— 而字典把它压缩成**一次 O(1) 的内存查找,没有 JOIN**。
   这就是 ClickHouse 里"能用字典就别 JOIN"的由来。

   代价是:字典的数据是**一次性加载、按生命周期刷新**的,不参与事务,
   也不保证和源表一致(刷新之间源表改了,字典里还是旧值)。这一点必须记住。

字典的四个概念(真实 ClickHouse 的 ``src/Dictionaries/`` 就是按这四层组织的):

.. code-block:: text

   ┌── SOURCE(源) ────────────────────────────────────────────────────┐
   │  数据从哪来。file / executable / http / clickhouse / mysql / …    │
   │  → 一次全量读取,产出一批"键列 + 属性列"的行                        │
   ├── LAYOUT(布局) ───────────────────────────────────────────────────┤
   │  源读出来的行,在内存里**以什么数据结构**存,才能 O(1) 查到          │
   │  flat / hashed / sparse_hashed / range_hashed / complex_key_* / … │
   ├── LIFETIME(生命周期) ─────────────────────────────────────────────┤
   │  多久之后数据算"过期"。注意:**过期是"下次访问时惰性重载"** ——       │
   │  不是定时器。没有访问就不会刷新。                                  │
   └── ATTRIBUTES(属性) ────────────────────────────────────────────────┘
      声明"这个字典有哪些列、它们是什么类型、缺省值是什么"。

布局对比表(教学重点 —— 为什么同一个字典能有好几种存法):

=========================  ==================  =========  ==========================
布局                        键的要求             查找       内存 / 说明
=========================  ==================  =========  ==========================
``flat``                   稠密 ``UInt64``      O(1)       一个**数组**,下标即键。
                                               **无哈希** 最快,但键必须"从 0 开始
                                                          且不太稀疏" —— 稀疏/负键
                                                          会浪费巨量内存,所以被拒。
``hashed``                 任意                 O(1)       Python ``dict``。通用,
                                                          每格多一点哈希开销。
``hashed_array``           任意                 O(1)       键 → 行号 的 dict + 属性
                                                          数组。属性重复多时更省。
``sparse_hashed``          任意                 O(1)*     **一整条小块的链**(真实实现
                                                          在 arena 里分配)。故意做得
                                                          不如 hashed 省内存,但**永不
                                                          rehash** —— 因为重哈希的
                                                          停顿在高频查询里不可接受。
``range_hashed``           键 + (min, max)      O(log n)  键 + 区间;按区间下界排序后
                                                          二分。``dictGet(d,v,key,ts)``
                                                          找包含 ts 的区间。
``complex_key_hashed``     复合(元组/字符串)    O(1)      和 hashed 一样,只是键是
                                                          元组。
``complex_key_direct``     复合                 O(源)     每次查找都问源,不缓存。
``direct``                 任意                 O(源)     每次查找都问源(不建索引)。
``cache``                  任意                 O(1)+LRU  在另一种布局外面套一层 LRU。
=========================  ==================  =========  ==========================

.. note::

   **关于"返回类型在分析期决定"这条重要差异。** 真实 ClickHouse 在
   *查询分析期*就按字面量(字典名、属性名)解析出字典并**内嵌一个指针**,
   所以 ``dictGet`` 的返回类型能被精确推导出来(这也是"字典不能像表那样
   跨服务器移植"的原因 —— 指针不跨机)。mini-clickhouse 的求值器只把
   **实参类型**(不是值)交给 ``get_return_type``,因此普通 ``dictGet``
   无法在分析期知道属性类型。我们采取的措施见 :func:`_DictGetReturnType`
   的说明:带类型后缀的 ``dictGetString``/``dictGetUInt64`` 等能给出精确
   类型,普通 ``dictGet`` 退化成在**执行期**拿到真实值,类型声明是占位。
   需要参与运算时请用带类型后缀的版本(与线上"显式写类型"的经验一致)。
"""

from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import io
import os
import random
import shlex
import subprocess
import time
import urllib.request
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .block import Block
from .catalog import ColumnDefinition
from .errors import ErrorCode, MiniChError, raise_error
from .types import DataType, get_data_type
from .types.base import DataTypePtr

__all__ = [
    "ISource",
    "ILayout",
    "Dictionary",
    "DictionaryAttribute",
    "Lifetime",
    "parse_lifetime",
    "create_dictionary",
    "drop_dictionary",
    "reload_dictionaries",
    "get_dictionary",
    "resolve_dictionary",
    "register_source",
    "register_layout",
    "dictionary_sources",
    "dictionary_layouts",
]


# ══════════════════════════════════════════════════════════════════════════
# 字典注册表 —— dictGet 需要能"按名字"找到字典
# ══════════════════════════════════════════════════════════════════════════
#
# 真实 ClickHouse 在分析期把字典名解析成 ``const IDictionary*`` 并随查询
# 计划带到执行期。mini-clickhouse 的标量函数拿不到那个指针,只能在执行期
# 按名字查。我们维护**两份**:
#
#   * ``ctx.dictionaries`` —— 每个 Context 自己的集合(首选);
#   * ``_REGISTRY``        —— 模块级兜底(没有 context 时用)。
#
# 后者是一种**简化**:它意味着"同名字典在一个进程里是全局的"。真实的
# 多租户隔离靠的是"字典属于某个 server / 某个数据库",而不是进程全局。
_REGISTRY: Dict[str, "Dictionary"] = {}

#: 最近一次被设置的上下文 —— 兜底的兜底。
_CURRENT_CONTEXT: Any = None


def _key_for(database: str, name: str) -> str:
    return f"{database}.{name}".lower()


def register_dictionary(ctx: Any, dictionary: "Dictionary") -> None:
    """把字典登记进 ``ctx.dictionaries``(以及模块级兜底表)。"""
    key = _key_for(dictionary.database, dictionary.name)
    if ctx is not None and hasattr(ctx, "dictionaries"):
        ctx.dictionaries[key] = dictionary
    _REGISTRY[key] = dictionary


def resolve_dictionary(name: str, context: Any = None) -> Optional["Dictionary"]:
    """按名字找字典。

    查找顺序(和真实 ClickHouse 的"先看当前库再看全名"一致):

    1. ``name`` 原样(大小写不敏感);
    2. 当前库限定 ``<ctx.database>.<name>``;
    3. ``name`` 里若带 ``.`` 则当全名;
    4. 全局唯一匹配(只在恰好一个候选时成立,避免歧义)。

    找不到返回 ``None``。
    """
    if not name:
        return None
    raw = str(name)
    lowered = raw.lower()

    registries: List[Dict[str, Any]] = []
    if context is not None and hasattr(context, "dictionaries"):
        registries.append(context.dictionaries)
    if _CURRENT_CONTEXT is not None and _CURRENT_CONTEXT is not context \
            and hasattr(_CURRENT_CONTEXT, "dictionaries"):
        registries.append(_CURRENT_CONTEXT.dictionaries)
    registries.append(_REGISTRY)

    # 1) 原样(允许大小写不敏感)
    for reg in registries:
        hit = reg.get(raw) or reg.get(lowered)
        if hit is not None:
            return hit
    # 2) 当前库限定
    if context is not None and getattr(context, "database", None):
        qualified = _key_for(context.database, raw)
        for reg in registries:
            hit = reg.get(qualified)
            if hit is not None:
                return hit
    # 4) 唯一后缀匹配
    candidates = {}
    for reg in registries:
        for key, dic in reg.items():
            if key.endswith("." + lowered):
                candidates[key] = dic
    if len(candidates) == 1:
        return next(iter(candidates.values()))
    return None


def get_dictionary(name: str, context: Any = None) -> "Dictionary":
    """和 :func:`resolve_dictionary` 一样,但找不到就抛 ``UNKNOWN_TABLE``。

    真实 ClickHouse 在这方面报的正是字典不存在,错误消息形如
    ``Dictionary `db.dict` not found``。我们沿用 ``UNKNOWN_TABLE`` 编号
    (真实 CH 用的是专门的 ``UNKNOWN_ELEMENT_IN_CONFIG`` 家族,编号不固定;
    这里取"找不到实体"这个最贴近的稳定编号)。
    """
    dic = resolve_dictionary(name, context)
    if dic is None:
        raise_error(ErrorCode.UNKNOWN_TABLE, f"Dictionary `{name}` not found")
    return dic


# ══════════════════════════════════════════════════════════════════════════
# 生命周期 —— LIFETIME(300) / LIFETIME(MIN 300 MAX 360)
# ══════════════════════════════════════════════════════════════════════════


def parse_lifetime(text: Optional[str]) -> Tuple[int, int]:
    """把 DDL 里的 ``LIFETIME(...)`` 拆成 ``(min, max)`` 秒。

    真实 ClickHouse 接受三种写法,它们**语义不同**:

    .. code-block:: text

       LIFETIME(300)                  → min = max = 300  固定 300 秒
       LIFETIME(MIN 300 MAX 360)      → 每次在 [300, 360] 里随机取值
       LIFETIME(300, 360)             → 同上(逗号写法)

    为什么要"随机区间"? 因为如果有一万个字典都写 ``LIFETIME(300)``,
    它们会在**同一秒**集体过期、集体去源库拉数据 —— 把源库打垮。
    真实 CH 的默认写法就是 ``MIN ... MAX ...``(抖动,防止惊群)。
    ``LIFETIME(0)`` 表示**永不自动刷新**(只能 ``SYSTEM RELOAD``)。
    """
    if not text:
        return 0, 0
    raw = str(text).strip()
    if not raw:
        return 0, 0
    upper = raw.upper()
    if "MIN" in upper and "MAX" in upper:
        nums = _ints_in(raw)
        if len(nums) >= 2:
            return nums[0], nums[1]
        if len(nums) == 1:
            return nums[0], nums[0]
        return 0, 0
    nums = _ints_in(raw)
    if not nums:
        return 0, 0
    if len(nums) == 1:
        return nums[0], nums[0]
    return nums[0], nums[1]


def _ints_in(text: str) -> List[int]:
    out: List[int] = []
    cur = ""
    for ch in text:
        if ch.isdigit():
            cur += ch
        else:
            if cur:
                out.append(int(cur))
                cur = ""
    if cur:
        out.append(int(cur))
    return out


class Lifetime:
    """字典的刷新周期 —— 记录 ``min/max`` 与"下次该刷新的时刻"。

    关键语义(**真实 ClickHouse 就是这样**):到期**不是**靠定时器触发的,
    而是**下一次访问时惰性判断**。所以

    * 一个没人查的字典,即使早过期也不会去刷新源;
    * ``SYSTEM RELOAD DICTIONARY`` 是唯一的"立即刷新"手段。

    这样做的好处是"刷新成本只在真有流量时付出",代价是"第一个踩到过期
    的查询要等一次加载"(真实 CH 有 ``background_reload`` 之类的优化,
    但惰性语义是基线)。
    """

    __slots__ = ("min_seconds", "max_seconds", "_next_reload", "_last_load")

    def __init__(self, min_seconds: int = 0, max_seconds: int = 0) -> None:
        self.min_seconds = max(0, int(min_seconds))
        self.max_seconds = max(0, int(max_seconds))
        self._last_load = time.time()
        self._next_reload = self._compute_next()

    def _compute_next(self) -> float:
        if self.max_seconds <= 0:
            # LIFETIME(0) —— 永不自动刷新
            return float("inf")
        if self.max_seconds == self.min_seconds:
            span = self.min_seconds
        else:
            span = random.randint(self.min_seconds, self.max_seconds)
        return time.time() + span

    def mark_loaded(self) -> None:
        self._last_load = time.time()
        self._next_reload = self._compute_next()

    def is_expired(self) -> bool:
        return time.time() >= self._next_reload

    def seconds_until_reload(self) -> Optional[float]:
        if self._next_reload == float("inf"):
            return None
        return max(0.0, self._next_reload - time.time())

    @property
    def seconds(self) -> int:
        """对外报的"生命周期秒数" —— ``system.dictionaries.lifetime`` 用最小值。"""
        return self.min_seconds

    def __repr__(self) -> str:  # pragma: no cover
        return f"Lifetime(min={self.min_seconds}, max={self.max_seconds})"


# ══════════════════════════════════════════════════════════════════════════
# 属性 —— 字典的列
# ══════════════════════════════════════════════════════════════════════════

#: 属性标志(真实 ClickHouse 的 ``DictionaryAttribute`` 里的几个 bool)。
#: 只有 ``HIERARCHICAL`` 会**改变行为**(``dictGetHierarchy`` 依赖它),
#: 其余两个纯粹是"给优化器的提示 / 元数据",我们原样存下来并在
#: ``system.dictionaries`` 可见。
_ATTRIBUTE_FLAGS = ("HIERARCHICAL", "INJECTIVE", "IS_OBJECT_ID")


class DictionaryAttribute:
    """字典里的一列。

    * ``name`` / ``type`` —— 列名与类型;
    * ``default_value`` —— **缺失键时返回的值**(真实字典的关键语义:
      找不到键**不报错**,而是返回该属性的 ``DEFAULT``;没写 ``DEFAULT``
      就是类型的零值);
    * ``hierarchical`` —— 是否是"父节点"列(层级字典);
    * ``injective`` / ``is_object_id`` —— 只作为元数据保存;
    * ``is_key`` —— 是否是主键的一部分。
    """

    __slots__ = (
        "name", "type", "default_value", "hierarchical", "injective",
        "is_object_id", "is_key",
    )

    def __init__(
        self,
        name: str,
        dtype: DataType,
        default_value: Any = None,
        hierarchical: bool = False,
        injective: bool = False,
        is_object_id: bool = False,
        is_key: bool = False,
    ) -> None:
        self.name = name
        self.type = dtype
        self.default_value = default_value
        self.hierarchical = hierarchical
        self.injective = injective
        self.is_object_id = is_object_id
        self.is_key = is_key

    def to_column_definition(self) -> ColumnDefinition:
        return ColumnDefinition(name=self.name, type=self.type)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Attribute {self.name} {self.type.get_name()}>"


def _split_attribute_type(type_name: str) -> Tuple[str, Dict[str, bool]]:
    """从"类型名 + 尾部标志"里拆出类型与标志。

    解析器目前**不**单独解析属性标志(它会尝试把标志当成下一个属性名),
    所以这一层更多是"如果将来上层把标志拼进类型名,也能正确识别"的
    防御式实现。标志词的匹配大小写不敏感。
    """
    flags = {f: False for f in _ATTRIBUTE_FLAGS}
    text = str(type_name).strip()
    tokens = text.split()
    kept: List[str] = []
    depth = 0
    for tok in tokens:
        if depth == 0 and tok.upper() in _ATTRIBUTE_FLAGS:
            flags[tok.upper()] = True
            continue
        depth += tok.count("(") - tok.count(")")
        kept.append(tok)
    return " ".join(kept), flags


# ══════════════════════════════════════════════════════════════════════════
# 源(ISource)
# ══════════════════════════════════════════════════════════════════════════


class ISource:
    """字典数据源的接口。

    一个源要做的事只有一件:**产出一批"列名 → 值的行"**。真正的字典
    构建过程是

    .. code-block:: text

       source.read(ctx)  ──► (列名列表, 行列表)
              │
              ▼
       按 DDL 的 attributes 从列里挑出"键列 + 属性列"
              │
              ▼
       layout.load(键, 值)  ──► 内存里的查找结构

    源是**一次性全量**读取的:字典没有"增量更新",每次刷新都是重读全部。
    这也是为什么字典适合"维表"(几千~几百万行)而不适合"事实表"。
    """

    #: 源名(与 ``SOURCE(...)`` 里的名字一致,小写)
    name = "source"

    def __init__(self, params: Any = None, dictionary: Optional["Dictionary"] = None) -> None:
        self.params: Dict[str, Any] = _params_to_dict(params)
        self.dictionary = dictionary

    def get_columns(self, ctx: Any) -> List[str]:
        """源能提供哪些列名(用于把 DDL 的属性映射到源的列)。"""
        return []

    def read(self, ctx: Any) -> Tuple[List[str], List[Tuple[Any, ...]]]:
        """读取全部数据,返回 ``(列名, 行)``。子类必须实现。"""
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"Dictionary source '{self.name}' does not implement read()",
        )

    def describe(self) -> str:
        """一句话描述这个源(``system.dictionaries.source`` 用)。"""
        if not self.params:
            return self.name.upper()
        inner = ", ".join(f"{k} {v!r}" for k, v in self.params.items())
        return f"{self.name.upper()}({inner})"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Source {self.name} {self.params}>"


def _params_to_dict(raw: Any) -> Dict[str, Any]:
    """把 DDL 里收集到的参数规整成 dict。

    解析器把 ``SOURCE(FILE(path '/x' format 'CSV'))`` 收集成一个**扁平
    列表** ``['path', '/x', 'format', 'CSV']``;真实 ClickHouse 也是
    "一串 ``键 值`` 对"。这里按"交替的键/值"重组,同时对
    "已经是 dict / (k, v) 列表"的形态保持兼容 —— 因为将来解析器可能
    改成结构化表示。
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return {str(k).upper(): v for k, v in raw.items()}
    if isinstance(raw, str):
        return {"_": raw}
    items = list(raw)
    if not items:
        return {}
    if all(isinstance(x, (tuple, list)) and len(x) == 2 for x in items):
        return {str(k).upper(): v for k, v in items}
    out: Dict[str, Any] = {}
    i = 0
    while i < len(items):
        key = str(items[i]).upper()
        if i + 1 < len(items):
            out[key] = items[i + 1]
            i += 2
        else:
            out[key] = ""
            i += 1
    return out


def _param(params: Dict[str, Any], *names: str, default: Any = None) -> Any:
    for n in names:
        if n.upper() in params:
            return params[n.upper()]
    return default


class FileSource(ISource):
    """``SOURCE(FILE(...))`` —— 从磁盘上的文件读。

    支持的参数(真实 CH 的常用子集):

    .. code-block:: text

       path        文件路径(也接受 'file' 别名)
       format      CSV / TSV / CSVWithNames / TSVWithNames / Values
       structure   内联的列结构,如 'id UInt64, name String'
       delimiter   分隔符(默认按 format 取 , 或 \\t)

    .. note::

       ``Values`` 格式用 :mod:`minich.literals` 里的字面量解析器;
       CSV/TSV 用标准库 ``csv``。**不实现引号/编码的边角情况**,
       教学用足够。
    """

    name = "file"

    def _path(self) -> str:
        path = _param(self.params, "path", "file", "filename")
        if not path:
            raise_error(ErrorCode.UNSUPPORTED_PARAMETER, "FILE source requires a 'path' parameter")
        return str(path)

    def _format(self) -> str:
        return str(_param(self.params, "format", default="CSV")).upper()

    def get_columns(self, ctx: Any) -> List[str]:
        struct = _param(self.params, "structure")
        if struct:
            return [c.strip().split()[0] for c in str(struct).split(",") if c.strip()]
        fmt = self._format()
        path = self._path()
        if fmt in ("CSVWITHNAMES", "TSVWITHNAMES", "CSVWITHHEADER"):
            try:
                with open(path, "r", encoding="utf-8", newline="") as fh:
                    reader = csv.reader(fh, delimiter="\t" if fmt.startswith("TSV") else ",")
                    return next(reader, [])
            except OSError:
                return []
        if self.dictionary is not None:
            return [a.name for a in self.dictionary.attributes]
        return []

    def read(self, ctx: Any) -> Tuple[List[str], List[Tuple[Any, ...]]]:
        path = self._path()
        fmt = self._format()
        if not os.path.exists(path):
            raise_error(ErrorCode.CANNOT_READ_FROM_ISTREAM if hasattr(ErrorCode, "CANNOT_READ_FROM_ISTREAM") else ErrorCode.NOT_IMPLEMENTED,
                        f"FILE source: no such file: {path}")
        with open(path, "r", encoding="utf-8", newline="") as fh:
            text = fh.read()
        return _parse_text_payload(text, fmt, self.params, self.dictionary)


class ExecutableSource(ISource):
    """``SOURCE(EXECUTABLE(...))`` —— 跑一个命令,把 stdout 当成数据。

    参数:

    .. code-block:: text

       command     要执行的命令(会被 shlex 切分)
       format      同 FILE(默认 TSV —— 因为脚本最容易吐 TSV)

    .. warning::

       这是**唯一会执行任意代码**的源。真实 ClickHouse 有
       ``<dictionaries_config>`` 的白名单与 ``command_allowlist`` 限制;
       mini-clickhouse 不做沙箱 —— 你在自己的进程里跑自己的命令。
    """

    name = "executable"

    def get_columns(self, ctx: Any) -> List[str]:
        struct = _param(self.params, "structure")
        if struct:
            return [c.strip().split()[0] for c in str(struct).split(",") if c.strip()]
        if self.dictionary is not None:
            return [a.name for a in self.dictionary.attributes]
        return []

    def read(self, ctx: Any) -> Tuple[List[str], List[Tuple[Any, ...]]]:
        command = _param(self.params, "command", "execute")
        if not command:
            raise_error(ErrorCode.UNSUPPORTED_PARAMETER, "EXECUTABLE source requires a 'command' parameter")
        fmt = str(_param(self.params, "format", default="TSV")).upper()
        try:
            proc = subprocess.run(
                shlex.split(str(command)), capture_output=True, text=True, check=False,
            )
        except OSError as exc:  # pragma: no cover - 环境相关
            raise_error(ErrorCode.NOT_IMPLEMENTED, f"EXECUTABLE source failed to run: {exc}")
        if proc.returncode != 0:
            raise_error(
                ErrorCode.NOT_IMPLEMENTED,
                f"EXECUTABLE source command exited with {proc.returncode}: "
                f"{proc.stderr.strip()[:200]}",
            )
        return _parse_text_payload(proc.stdout, fmt, self.params, self.dictionary)


class HTTPSource(ISource):
    """``SOURCE(HTTP(...))`` —— GET 一个 URL,把响应体当成数据。

    参数:``url``、``format``(默认 CSV)。用标准库 ``urllib`` —— 不引入
    第三方 HTTP 客户端,因为教学里这个源多半指向本地的小文件服务。
    """

    name = "http"

    def get_columns(self, ctx: Any) -> List[str]:
        struct = _param(self.params, "structure")
        if struct:
            return [c.strip().split()[0] for c in str(struct).split(",") if c.strip()]
        if self.dictionary is not None:
            return [a.name for a in self.dictionary.attributes]
        return []

    def read(self, ctx: Any) -> Tuple[List[str], List[Tuple[Any, ...]]]:
        url = _param(self.params, "url")
        if not url:
            raise_error(ErrorCode.UNSUPPORTED_PARAMETER, "HTTP source requires a 'url' parameter")
        fmt = str(_param(self.params, "format", default="CSV")).upper()
        try:
            with urllib.request.urlopen(str(url), timeout=10) as resp:
                body = resp.read().decode("utf-8", "replace")
        except Exception as exc:  # pragma: no cover - 依赖网络
            raise_error(
                ErrorCode.ALL_CONNECTION_TRIES_FAILED if hasattr(ErrorCode, "ALL_CONNECTION_TRIES_FAILED") else ErrorCode.NOT_IMPLEMENTED,
                f"HTTP source could not fetch {url}: {exc}",
            )
        return _parse_text_payload(body, format_override=fmt, params=self.params,
                                   dictionary=self.dictionary)


class TableSource(ISource):
    """``SOURCE(TABLE(...))`` / ``SOURCE(IN_PROCESS(...))`` —— 读本地目录里的表。

    这是**教学与测试里唯一真正好用的源**:不碰网络、不碰磁盘,直接读
    ``ctx.catalog`` 里的一张表。参数:``name``(表名)/``db``(库名)。
    """

    name = "table"

    def get_columns(self, ctx: Any) -> List[str]:
        table = self._table(ctx)
        return table.get_column_names()

    def _table(self, ctx: Any):
        name = _param(self.params, "name", "table")
        if not name:
            raise_error(ErrorCode.UNSUPPORTED_PARAMETER, "TABLE source requires a 'name' parameter")
        db = _param(self.params, "db", "database")
        db = str(db) if db else getattr(ctx, "database", "default")
        table = ctx.catalog.try_get_table(db, str(name))
        if table is None:
            raise_error(
                ErrorCode.UNKNOWN_TABLE,
                f"Dictionary TABLE source: table {db}.{name} does not exist",
            )
        return table

    def read(self, ctx: Any) -> Tuple[List[str], List[Tuple[Any, ...]]]:
        table = self._table(ctx)
        return _read_local_table(ctx, table)


class InProcessSource(TableSource):
    """``SOURCE(IN_PROCESS(...))`` —— 语义上就是"读本进程里的表"。"""

    name = "in-process"


class ClickHouseSource(ISource):
    """``SOURCE(CLICKHOUSE(...))`` —— 从 ClickHouse 读。

    .. note::

       **同机优化是真实行为,不是偷懒。** 真实 ClickHouse 在
       "SOURCE(CLICKHOUSE(...)) 指向本机"时会**绕开网络**,直接读本地
       表(内部把它优化成一次本地查询)。所以当我们没有拿到 ``host``/
       ``port``(或拿到的是本机)时,我们就读 ``ctx.catalog`` 里的表 ——
       这既符合真实语义,也让教学环境开箱即用。

       一旦参数里出现了**远端** ``host``/``port``,我们就如实报
       ``NOT_IMPLEMENTED`` —— mini-clickhouse 没有跨进程协议客户端。
    """

    name = "clickhouse"

    def _is_remote(self) -> bool:
        host = _param(self.params, "host")
        return bool(host) and str(host) not in ("localhost", "127.0.0.1", "::1")

    def get_columns(self, ctx: Any) -> List[str]:
        if self._is_remote():
            _raise_remote_not_supported("CLICKHOUSE")
        table = self._local_table(ctx)
        return table.get_column_names()

    def _local_table(self, ctx: Any):
        name = _param(self.params, "table")
        if not name:
            raise_error(
                ErrorCode.UNSUPPORTED_PARAMETER,
                "CLICKHOUSE source (local) requires a 'table' parameter",
            )
        db = _param(self.params, "db", "database")
        db = str(db) if db else getattr(ctx, "database", "default")
        table = ctx.catalog.try_get_table(db, str(name))
        if table is None:
            raise_error(
                ErrorCode.UNKNOWN_TABLE,
                f"Dictionary CLICKHOUSE source: table {db}.{name} does not exist",
            )
        return table

    def read(self, ctx: Any) -> Tuple[List[str], List[Tuple[Any, ...]]]:
        if self._is_remote():
            _raise_remote_not_supported("CLICKHOUSE")
        table = self._local_table(ctx)
        return _read_local_table(ctx, table)


class NotSupportedSource(ISource):
    """所有"注册了但没实现"的源的共同实现。

    真实 ClickHouse 支持 ``mysql`` / ``postgresql`` / ``odbc`` / ``mongodb``
    / ``redis`` / ``s3`` / ``hdfs`` 等源。mini-clickhouse **故意不实现**
    它们 —— 那需要引入各自的驱动,和"用纯标准库把机制讲清楚"的目标冲突。
    但我们**保留名字**:这样 ``CREATE DICTIONARY ... SOURCE(MYSQL(...))``
    报的是一个**精确的** ``NOT_IMPLEMENTED``(并说明原因),而不是含糊的
    "Unknown source"。
    """

    def __init__(self, params: Any = None, dictionary: Optional["Dictionary"] = None,
                 *, reason: str = "") -> None:
        super().__init__(params, dictionary)
        self.reason = reason

    def read(self, ctx: Any) -> Tuple[List[str], List[Tuple[Any, ...]]]:
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            f"Dictionary source '{self.name.upper()}' is not implemented in mini-clickhouse: "
            f"{self.reason}",
        )


def _raise_remote_not_supported(kind: str) -> None:
    raise_error(
        ErrorCode.NOT_IMPLEMENTED,
        f"{kind} dictionary source over the network is not implemented in "
        f"mini-clickhouse (no client protocol); only the same-server optimization "
        f"(reading a local table) is supported",
    )


def _read_local_table(ctx: Any, table: Any) -> Tuple[List[str], List[Tuple[Any, ...]]]:
    """读一张本地表的全部行 —— 供 TABLE / CLICKHOUSE(本机)源共用。"""
    names = table.get_column_names()
    rows: List[Tuple[Any, ...]] = []
    chunks = table.read(ctx)
    for chunk in chunks:
        n = chunk.num_rows
        cols = chunk.get_columns()
        for i in range(n):
            rows.append(tuple(cols[j].get(i) for j in range(len(cols))))
    return names, rows


# ══════════════════════════════════════════════════════════════════════════
# 文本载荷解析(CSV/TSV/Values)—— FILE/EXECUTABLE/HTTP 共用
# ══════════════════════════════════════════════════════════════════════════


def _parse_text_payload(
    text: str,
    format_override: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    dictionary: Optional["Dictionary"] = None,
) -> Tuple[List[str], List[Tuple[Any, ...]]]:
    """把一段文本按指定格式解析成 ``(列名, 行)``。"""
    params = params or {}
    fmt = (format_override or params.get("FORMAT") or "CSV").upper()
    structure = params.get("STRUCTURE")
    header_names: Optional[List[str]] = None
    if structure:
        header_names = [c.strip().split()[0] for c in str(structure).split(",") if c.strip()]
    elif dictionary is not None:
        header_names = [a.name for a in dictionary.attributes]

    if fmt.startswith("TSV"):
        delim = "\t"
    else:
        delim = str(params.get("DELIMITER") or ",")

    if fmt == "VALUES":
        return _parse_values_payload(text, header_names, dictionary)

    with_names = "WITHNAMES" in fmt or "WITHHEADER" in fmt or "WITH_NAMES" in fmt
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    raw_rows = [r for r in reader if r]
    names = header_names or []
    if with_names and raw_rows:
        names = raw_rows[0]
        raw_rows = raw_rows[1:]
    if not names:
        names = [f"c{i}" for i in range(len(raw_rows[0]))] if raw_rows else []
    return names, [tuple(r) for r in raw_rows]


def _parse_values_payload(
    text: str,
    names: Optional[List[str]],
    dictionary: Optional["Dictionary"],
) -> Tuple[List[str], List[Tuple[Any, ...]]]:
    """解析 ``VALUES`` 风格 ``(1, 'a'), (2, 'b')``。"""
    from .literals import split_top_level, parse_literal  # 延迟导入,避免环

    try:
        chunks = [c for c in split_top_level(text, ",") if c.strip()]
        values = [parse_literal(c.strip()) for c in chunks]
    except Exception as exc:
        raise_error(ErrorCode.CANNOT_PARSE_TEXT, f"Values payload could not be parsed: {exc}")
    names = names or []
    rows = [tuple(v) if isinstance(v, (tuple, list)) else (v,) for v in values]
    if not names and rows:
        names = [f"c{i}" for i in range(len(rows[0]))]
    return names, rows


# ══════════════════════════════════════════════════════════════════════════
# 布局(ILayout)
# ══════════════════════════════════════════════════════════════════════════


class ILayout:
    """字典在内存里的存法。

    接口只有三件事:

    * :meth:`load` —— 把 ``(键, 值)`` 批灌进来,建索引;
    * :meth:`get`  —— 按键取值(``range_hashed`` 还要一个时间点);
    * :meth:`has`  —— 键是否存在(``dictHas`` 用)。

    所有布局的 ``键`` 都统一成 **tuple**:单键是长度 1 的 tuple,复合键
    是长度 n 的 tuple。这样 ``dictGet`` 那一层不需要关心布局。
    """

    name = "layout"

    def __init__(self, dictionary: Optional["Dictionary"] = None) -> None:
        self.dictionary = dictionary

    def load(self, keys: List[Tuple[Any, ...]], values: List[Tuple[Any, ...]]) -> None:
        raise_error(ErrorCode.NOT_IMPLEMENTED, f"Layout {self.name} does not implement load()")

    def get(self, key: Tuple[Any, ...], ts: Any = None) -> Optional[Tuple[Any, ...]]:
        raise_error(ErrorCode.NOT_IMPLEMENTED, f"Layout {self.name} does not implement get()")

    def has(self, key: Tuple[Any, ...]) -> bool:
        return self.get(key) is not None

    # ── 统计 ────────────────────────────────────────────────────────────
    def element_count(self) -> int:
        return 0

    def bytes_allocated(self) -> int:
        return 0

    def load_factor(self) -> float:
        return 0.0

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Layout {self.name} elements={self.element_count()}>"


class FlatLayout(ILayout):
    """``flat`` —— 一个**数组**,下标就是键。**最快,但键必须稠密**。

    .. code-block:: text

       flat 的内存形态(键 = 下标):

         keys:  0    1    2    3    4    5
              ┌────┬────┬────┬────┬────┬────┐
       slot  │ v0 │ v1 │ v2 │ v3 │ v4 │ v5 │   lookup(key) = slot[key]
              └────┴────┴────┴────┴────┴────┘
              第 key 个位置就是答案 —— 一次内存寻址,**没有任何哈希**。

    为什么它最快? 因为"取下标"是 O(1) 且常数极小:没有哈希计算、
    没有冲突处理、没有指针追逐(CPU 预取器喜欢顺序内存)。
    这也是为什么它的**约束**必须被尊重:

    * 键必须是**无符号整数**(``UInt*``);
    * 键必须**落在 [0, max_key] 的稠密区间**里 —— 因为数组要开
      ``max_key + 1`` 个格子。

    真实的 ``flat`` 字典有一条硬规则:**如果键稀疏,内存会被浪费到
    无法接受**(比如只有一个键 ``10^9``,却要开 10 亿个格子)。
    所以真实 ClickHouse 在这时会**拒绝** ``flat``,让你改用 ``hashed``。
    mini-clickhouse 同样拒绝(抛 ``NOT_IMPLEMENTED`` 家族之外的
    ``ARGUMENT_OUT_OF_BOUND``),并给出"换 hashed"的建议。
    """

    name = "flat"

    #: 数组最多开这么多格 —— 防止一个坏键把内存吃光。
    MAX_SIZE = 1 << 24  # 1600 万格

    def __init__(self, dictionary: Optional["Dictionary"] = None) -> None:
        super().__init__(dictionary)
        self._slots: List[Optional[Tuple[Any, ...]]] = []
        self._max_key = -1

    def load(self, keys: List[Tuple[Any, ...]], values: List[Tuple[Any, ...]]) -> None:
        if not keys:
            self._slots = []
            self._max_key = -1
            return
        for k in keys:
            if len(k) != 1:
                raise_error(
                    ErrorCode.NOT_IMPLEMENTED,
                    "flat layout supports a single integer key only; "
                    "use complex_key_hashed for tuple keys",
                )
            kv = k[0]
            if not isinstance(kv, int) or isinstance(kv, bool) or kv < 0:
                raise_error(
                    ErrorCode.ARGUMENT_OUT_OF_BOUND,
                    f"flat layout requires non-negative integer keys, got {kv!r}; "
                    f"use LAYOUT(HASHED()) for sparse or negative keys",
                )
        max_key = max(k[0] for k in keys)
        if max_key >= self.MAX_SIZE:
            raise_error(
                ErrorCode.ARGUMENT_OUT_OF_BOUND,
                f"flat layout would need {max_key + 1} slots (max key {max_key}); "
                f"keys are too sparse — use LAYOUT(HASHED())",
            )
        self._max_key = max_key
        self._slots = [None] * (max_key + 1)
        for k, v in zip(keys, values):
            self._slots[k[0]] = v

    def get(self, key: Tuple[Any, ...], ts: Any = None) -> Optional[Tuple[Any, ...]]:
        if len(key) != 1:
            return None
        kv = key[0]
        if not isinstance(kv, int) or isinstance(kv, bool) or kv < 0 or kv > self._max_key:
            return None
        return self._slots[kv]

    def element_count(self) -> int:
        return sum(1 for s in self._slots if s is not None)

    def bytes_allocated(self) -> int:
        # 每个格子一个指针 + 值的近似大小
        val = self.dictionary.value_size() if self.dictionary else 16
        return len(self._slots) * (8 + val)

    def load_factor(self) -> float:
        if not self._slots:
            return 0.0
        return self.element_count() / float(len(self._slots))


class HashedLayout(ILayout):
    """``hashed`` —— Python ``dict``,键 → 值。通用、可预测。"""

    name = "hashed"

    def __init__(self, dictionary: Optional["Dictionary"] = None) -> None:
        super().__init__(dictionary)
        self._table: Dict[Tuple[Any, ...], Tuple[Any, ...]] = {}

    def load(self, keys: List[Tuple[Any, ...]], values: List[Tuple[Any, ...]]) -> None:
        self._table = {}
        for k, v in zip(keys, values):
            self._table[k] = v

    def get(self, key: Tuple[Any, ...], ts: Any = None) -> Optional[Tuple[Any, ...]]:
        return self._table.get(key)

    def element_count(self) -> int:
        return len(self._table)

    def bytes_allocated(self) -> int:
        val = self.dictionary.value_size() if self.dictionary else 16
        # dict 的每项:键对象 + 值对象 + 哈希槽(约 3 个指针)
        return len(self._table) * (24 + val)

    def load_factor(self) -> float:
        return 1.0 if self._table else 0.0


class HashedArrayLayout(ILayout):
    """``hashed_array`` —— 键 → **行号**,属性存在平行数组里。

    .. code-block:: text

       hashed_array:
         index:  { key0: 0, key1: 1, ... }   ← 只有键 → 行号,很小
         values: [ (v0…), (v1…), (v2…), … ]  ← 属性按行连续存放

    当**多个键共享同一批属性**、或者属性行本身很大时,这比 "dict of value
    tuple" 省一点(索引里只放整数)。真实 CH 用它做"键特别多、值特别宽"
    的字典。
    """

    name = "hashed_array"

    def __init__(self, dictionary: Optional["Dictionary"] = None) -> None:
        super().__init__(dictionary)
        self._index: Dict[Tuple[Any, ...], int] = {}
        self._values: List[Tuple[Any, ...]] = []

    def load(self, keys: List[Tuple[Any, ...]], values: List[Tuple[Any, ...]]) -> None:
        self._index = {}
        self._values = list(values)
        for i, k in enumerate(keys):
            self._index[k] = i

    def get(self, key: Tuple[Any, ...], ts: Any = None) -> Optional[Tuple[Any, ...]]:
        idx = self._index.get(key)
        return self._values[idx] if idx is not None else None

    def element_count(self) -> int:
        return len(self._index)

    def bytes_allocated(self) -> int:
        val = self.dictionary.value_size() if self.dictionary else 16
        return len(self._index) * 24 + len(self._values) * val

    def load_factor(self) -> float:
        return 1.0 if self._index else 0.0


class SparseHashedLayout(ILayout):
    """``sparse_hashed`` —— **一整条小块的链**,永不 rehash。

    真实 ClickHouse 的 ``SparseHashedDictionary`` 把数据分配在
    **arena** 里,键的存放分成若干"块"。它的设计目标不是省内存 ——
    恰恰相反,它**故意比 hashed 多用内存**,换来两个性质:

    .. code-block:: text

       hashed:        一个巨大的连续哈希表
                      ┌──────────────────────────────────────┐
                      │ … 增长到需要扩容时:**整体 rehash**    │
                      └──────────────────────────────────────┘
                                 ↑ 一次停顿,可能在查询路径上

       sparse_hashed: 一串小块,新键进"最后一块"
                      ┌────┐┌────┐┌────┐┌────┐
                      │ k… ││ k… ││ k… ││ k… │  ← 满了就开新块,
                      └────┘└────┘└────┘└────┘     从不重排老块

    为什么值得? 因为字典可能被**高频查询同时命中**,而"一次 rehash
    停顿"在高并发下会放大成尾延迟。稀疏哈希用"多占点内存"把它换掉了。
    """

    name = "sparse_hashed"

    #: 每块最多多少项(真实实现由 arena 的粒度决定,这里取一个教学值)
    BLOCK_SIZE = 256

    def __init__(self, dictionary: Optional["Dictionary"] = None) -> None:
        super().__init__(dictionary)
        self._blocks: List[Dict[Tuple[Any, ...], Tuple[Any, ...]]] = []
        self._count = 0

    def load(self, keys: List[Tuple[Any, ...]], values: List[Tuple[Any, ...]]) -> None:
        self._blocks = []
        self._count = 0
        current: Dict[Tuple[Any, ...], Tuple[Any, ...]] = {}
        for k, v in zip(keys, values):
            current[k] = v
            self._count += 1
            if len(current) >= self.BLOCK_SIZE:
                self._blocks.append(current)
                current = {}
        if current:
            self._blocks.append(current)

    def get(self, key: Tuple[Any, ...], ts: Any = None) -> Optional[Tuple[Any, ...]]:
        # 小块时从头扫;**实现上这仍是 O(块数),但每块是纯 dict 查找**。
        for block in self._blocks:
            hit = block.get(key)
            if hit is not None:
                return hit
        return None

    def element_count(self) -> int:
        return self._count

    def bytes_allocated(self) -> int:
        val = self.dictionary.value_size() if self.dictionary else 16
        return len(self._blocks) * 64 + self._count * (32 + val)

    def load_factor(self) -> float:
        return 0.5 if self._count else 0.0


class RangeHashedLayout(ILayout):
    """``range_hashed`` —— 键 + ``(range_min, range_max)`` 区间。

    这是"按时间点查有效值"的布局:同一张维表里,一个键在**不同时间段**
    有不同的值(比如"某用户在某段时间属于某套餐")。查询写成:

    .. code-block:: sql

       dictGet('tariffs', 'price', user_id, event_time)

    .. code-block:: text

       键 K 的区间(按 range_min 排序后二分):

         ts:   100        200        300        400
               │          │          │          │
         K:  [── range0 ──)[── range1 ──)[── range2 ──]
               price=1       price=2       price=3

       lookup(K, 250) → 二分找到 range_min<=250 的最后一个区间 → range1

    真实 ClickHouse 要求区间列就叫 ``range_min``/``range_max``;
    我们照做(在 DDL 的属性里按名字识别)。
    """

    name = "range_hashed"

    def __init__(self, dictionary: Optional["Dictionary"] = None) -> None:
        super().__init__(dictionary)
        #: 键 → [(min, max, value), …],按 min 有序
        self._ranges: Dict[Tuple[Any, ...], List[Tuple[Any, Any, Tuple[Any, ...]]]] = {}

    @staticmethod
    def _to_number(value: Any) -> Any:
        if isinstance(value, _dt.datetime):
            return value.timestamp()
        if isinstance(value, _dt.date):
            return _dt.datetime(value.year, value.month, value.day).timestamp()
        return value

    def load(self, keys: List[Tuple[Any, ...]], values: List[Tuple[Any, ...]]) -> None:
        rmin_idx, rmax_idx = self._range_indexes()
        self._ranges = {}
        for k, v in zip(keys, values):
            lo = self._to_number(v[rmin_idx])
            hi = self._to_number(v[rmax_idx])
            self._ranges.setdefault(k, []).append((lo, hi, v))
        for lst in self._ranges.values():
            lst.sort(key=lambda item: item[0])

    def _range_indexes(self) -> Tuple[int, int]:
        if self.dictionary is None:
            return 0, 1
        return self.dictionary.range_attribute_indexes()

    def get(self, key: Tuple[Any, ...], ts: Any = None) -> Optional[Tuple[Any, ...]]:
        ranges = self._ranges.get(key)
        if not ranges:
            return None
        if ts is None:
            # 没给时间点 → 用"最晚的一个区间"(真实 CH 会报错要求给时间;
            # 这里给出一个确定性的便利行为,并在此说明)。
            return ranges[-1][2]
        point = self._to_number(ts)
        # 二分:找 range_min <= point 的最后一个区间
        lo, hi = 0, len(ranges) - 1
        best = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if ranges[mid][0] <= point:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        if best is None:
            return None
        rmin, rmax, value = ranges[best]
        if rmax is not None and point > rmax:
            return None
        return value

    def element_count(self) -> int:
        return sum(len(v) for v in self._ranges.values())

    def bytes_allocated(self) -> int:
        val = self.dictionary.value_size() if self.dictionary else 16
        return self.element_count() * (val + 32)

    def load_factor(self) -> float:
        return 0.5 if self._ranges else 0.0


class ComplexKeyHashedLayout(HashedLayout):
    """``complex_key_hashed`` —— 键是**元组**(复合键)。"""

    name = "complex_key_hashed"


class DirectLayout(ILayout):
    """``direct`` —— **每次查找都去问源**,不建任何索引。

    真实 ClickHouse 里 ``direct`` 用于"源本身很快(比如一个内存表、
    一个本地文件),而且字典大到不值得全部驻留内存"的场景。

    .. code-block:: text

       hashed:  load 时读全部 → 内存里查       (查询快,加载/内存贵)
       direct:  不预先 load   → 每次查时问源    (加载免费,查询贵)

    我们把"问源"实现成:每次 ``get`` 时**重新读一遍源**(并且带上键过滤
    的意图)。教学实现里这确实很慢;真实实现在源侧做下推(把键塞进
    ``WHERE``)。这里的重点是让你看清**这条时间/空间的权衡**。
    """

    name = "direct"

    def __init__(self, dictionary: Optional["Dictionary"] = None) -> None:
        super().__init__(dictionary)
        self._loaded_rows: Optional[List[Tuple[Any, ...]]] = None
        self._names: List[str] = []
        self._key_cols: List[int] = []
        self._value_cols: List[int] = []

    def load(self, keys: List[Tuple[Any, ...]], values: List[Tuple[Any, ...]]) -> None:
        # direct 不缓存;但为了"同一进程里可重放",我们记住源的名字映射。
        self._loaded_rows = None

    def _rows(self, ctx: Any) -> Tuple[List[str], List[Tuple[Any, ...]]]:
        if self.dictionary is None or self.dictionary._source_obj is None:
            return [], []
        return self.dictionary._source_obj.read(ctx)

    def get(self, key: Tuple[Any, ...], ts: Any = None) -> Optional[Tuple[Any, ...]]:
        # direct 每次"问源" —— 我们没有一个随 get 传入的 ctx,所以退化成
        # "用最近一次 load 记住的源行做一次线性扫描"(真实实现在源侧把键
        # 下推成 WHERE,并且是逐次真的去问源)。重点是**没有索引**这件事。
        if self._loaded_rows is None or self.dictionary is None:
            return None
        key_cols = self._key_cols
        value_cols = self._value_cols
        for row in self._loaded_rows:
            if tuple(row[i] for i in key_cols) == key:
                return tuple(row[i] for i in value_cols)
        return None

    def load_from_source(self, ctx: Any) -> None:
        names, rows = self._rows(ctx)
        self._names = names
        self._loaded_rows = rows
        # 预先算好"键列 / 值列"在源行里的下标 —— 这样 get() 返回的
        # 就**和 hashed 一样是"非键属性的值元组"**,dictGet 那一层不必区分布局。
        lower = {str(n).lower(): i for i, n in enumerate(names)}
        dic = self.dictionary
        if dic is not None:
            self._key_cols = [lower[n.lower()] for n in dic.key_names if n.lower() in lower]
            self._value_cols = [
                lower[n.lower()] for n in dic._value_attribute_names if n.lower() in lower
            ]

    def element_count(self) -> int:
        return 0

    def bytes_allocated(self) -> int:
        return 0

    def load_factor(self) -> float:
        return 0.0


class ComplexKeyDirectLayout(DirectLayout):
    """``complex_key_direct`` —— ``direct`` 的复合键版本。"""

    name = "complex_key_direct"


class CacheLayout(ILayout):
    """``cache`` —— 在另一种布局(或 ``direct``)外面套一层 **LRU**。

    参数(真实 CH 的参数名):``size_in_cells``(缓存多少格,默认 1000)、
    ``lifetime``。命中就返回,未命中就问内层(通常是源),并把结果放进
    LRU。淘汰用"最近最少使用"。

    .. code-block:: text

       查找 key:
          ┌── 在 LRU 里? ──是──► 命中,更新 LRU 顺序,返回
          │
          └──否──► 问内层布局/源 ──► 放进 LRU(满了就淘汰最久未用的)

    LRU 用 ``collections.OrderedDict`` 实现 —— ``move_to_end`` + ``popitem``
    就是"最近使用"与"淘汰最旧"。
    """

    name = "cache"

    def __init__(self, dictionary: Optional["Dictionary"] = None,
                 size_in_cells: int = 1000) -> None:
        super().__init__(dictionary)
        from collections import OrderedDict

        self.size_in_cells = int(size_in_cells)
        self._lru: "OrderedDict[Tuple[Any, ...], Optional[Tuple[Any, ...]]]" = OrderedDict()
        self._inner: Optional[ILayout] = None
        self._hits = 0
        self._misses = 0

    def set_inner(self, layout: ILayout) -> None:
        self._inner = layout

    def load(self, keys: List[Tuple[Any, ...]], values: List[Tuple[Any, ...]]) -> None:
        if self._inner is not None:
            self._inner.load(keys, values)

    def get(self, key: Tuple[Any, ...], ts: Any = None) -> Optional[Tuple[Any, ...]]:
        if key in self._lru:
            self._hits += 1
            self._lru.move_to_end(key)
            return self._lru[key]
        self._misses += 1
        value = self._inner.get(key, ts) if self._inner is not None else None
        self._lru[key] = value
        if len(self._lru) > self.size_in_cells:
            self._lru.popitem(last=False)
        return value

    def element_count(self) -> int:
        return len(self._lru)

    def bytes_allocated(self) -> int:
        val = self.dictionary.value_size() if self.dictionary else 16
        return len(self._lru) * (32 + val)

    def load_factor(self) -> float:
        return (len(self._lru) / float(self.size_in_cells)) if self.size_in_cells else 0.0

    def stats(self) -> Dict[str, Any]:
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": (self._hits / total) if total else 0.0,
        }


# ══════════════════════════════════════════════════════════════════════════
# 源 / 布局注册表
# ══════════════════════════════════════════════════════════════════════════

_SOURCES: Dict[str, Callable[..., ISource]] = {}
_LAYOUTS: Dict[str, Callable[..., ILayout]] = {}


def register_source(name: str, factory: Callable[..., ISource]) -> None:
    _SOURCES[name.lower()] = factory


def register_layout(name: str, factory: Callable[..., ILayout]) -> None:
    _LAYOUTS[name.lower()] = factory


def dictionary_sources() -> List[str]:
    return sorted(_SOURCES)


def dictionary_layouts() -> List[str]:
    return sorted(_LAYOUTS)


def _register_builtin_sources() -> None:
    register_source("file", FileSource)
    register_source("executable", ExecutableSource)
    register_source("http", HTTPSource)
    register_source("table", TableSource)
    register_source("in-process", InProcessSource)
    register_source("in_process", InProcessSource)
    register_source("clickhouse", ClickHouseSource)
    # 未实现但注册了名字的源(报精确的 NOT_IMPLEMENTED)
    for name, reason in (
        ("mysql", "no MySQL client driver is bundled (would need a third-party package)"),
        ("postgresql", "no PostgreSQL client driver is bundled"),
        ("odbc", "no ODBC driver manager is bundled"),
        ("mongodb", "no MongoDB client is bundled"),
        ("redis", "no Redis client is bundled"),
        ("s3", "no S3/HTTP object-store client is bundled"),
        ("hdfs", "no HDFS client is bundled"),
    ):
        register_source(name, _make_not_supported_source(name, reason))


def _make_not_supported_source(name: str, reason: str) -> Callable[..., ISource]:
    def _factory(params: Any = None, dictionary: Optional["Dictionary"] = None) -> ISource:
        return NotSupportedSource(params, dictionary, reason=reason)
    _factory.__name__ = f"{name.title()}Source"
    return _factory


def _simple_layout_factory(cls: type) -> Callable[..., ILayout]:
    """把"只收 dictionary"的布局类适配成工厂签名 ``(params, dictionary)``。"""
    def _factory(params: Any = None, dictionary: Optional["Dictionary"] = None) -> ILayout:
        return cls(dictionary)
    _factory.__name__ = cls.__name__
    return _factory


def _register_builtin_layouts() -> None:
    register_layout("flat", _simple_layout_factory(FlatLayout))
    register_layout("hashed", _simple_layout_factory(HashedLayout))
    register_layout("hashed_array", _simple_layout_factory(HashedArrayLayout))
    register_layout("sparse_hashed", _simple_layout_factory(SparseHashedLayout))
    register_layout("range_hashed", _simple_layout_factory(RangeHashedLayout))
    register_layout("complex_key_hashed", _simple_layout_factory(ComplexKeyHashedLayout))
    register_layout("complex_key_direct", _simple_layout_factory(ComplexKeyDirectLayout))
    register_layout("direct", _simple_layout_factory(DirectLayout))
    # cache / ssd_cache 需要一个"内层布局 + LRU",用工厂处理参数
    register_layout("cache", _make_cache_layout)

    def _ssd_cache(params: Any = None, dictionary: Optional["Dictionary"] = None) -> ILayout:
        raise_error(
            ErrorCode.NOT_IMPLEMENTED,
            "ssd_cache layout is not implemented in mini-clickhouse: it persists the "
            "dictionary to an on-disk cache file, and we deliberately avoid an on-disk "
            "format here; use LAYOUT(CACHE(SIZE_IN_CELLS n)) for an in-memory LRU",
        )
        raise AssertionError  # pragma: no cover

    register_layout("ssd_cache", _ssd_cache)

    def _unsupported(layout_name: str, why: str) -> Callable[..., ILayout]:
        def _factory(params: Any = None, dictionary: Optional["Dictionary"] = None) -> ILayout:
            raise_error(
                ErrorCode.NOT_IMPLEMENTED,
                f"Layout {layout_name} is not implemented in mini-clickhouse: {why}",
            )
            raise AssertionError  # pragma: no cover
        return _factory

    register_layout("polygon", _unsupported(
        "polygon", "point-in-polygon lookup needs a geometric library and a spatial index"))
    register_layout("ip_trie", _unsupported(
        "ip_trie", "longest-prefix IP lookup needs a trie over address ranges"))
    register_layout("regexp_tree", _unsupported(
        "regexp_tree", "regex path lookup needs a regex automaton (RE2-style)"))


def _make_cache_layout(params: Any = None, dictionary: Optional["Dictionary"] = None) -> ILayout:
    p = _params_to_dict(params)
    size = int(_param(p, "size_in_cells", "size", default=1000) or 1000)
    return CacheLayout(dictionary, size_in_cells=size)


_register_builtin_sources()
_register_builtin_layouts()


# ══════════════════════════════════════════════════════════════════════════
# 字典
# ══════════════════════════════════════════════════════════════════════════

#: 生命周期状态 —— 与真实 ClickHouse 的 ``DictionaryStatus`` 一致。
#: 注意 ``FAILED`` 的字典**仍然留在注册表里**:这样 ``system.dictionaries``
#: 能告诉你它为什么失败(``last_exception``),而不是"查无此典"。
STATUS_LOADING = "LOADING"
STATUS_LOADED = "LOADED"
STATUS_FAILED = "FAILED"
STATUS_NOT_LOADED = "NOT_LOADED"


class Dictionary:
    """一个已注册的外部字典。

    对外要满足两类消费者:

    1. ``dictGet`` 家族 —— 通过 :meth:`get_attribute` / :meth:`has_key`;
    2. ``system.dictionaries`` —— 通过一批**属性**(``status``、``element_count``
       …)。这张系统表用 ``getattr`` 式映射读字段,所以我们把这些字段
       做成**真属性/属性函数**,并额外提供 :meth:`describe_row`。
    """

    def __init__(
        self,
        database: str,
        name: str,
        attributes: List[DictionaryAttribute],
        key_names: List[str],
        source: Optional[ISource],
        layout: ILayout,
        lifetime: Lifetime,
        *,
        layout_name: str = "",
        source_sql: str = "",
        create_sql: str = "",
    ) -> None:
        self.database = database
        self.name = name
        self.attributes = attributes
        self.key_names = key_names
        self._source_obj = source
        self.layout = layout
        #: 真正持有刷新周期的是这个对象;对外暴露的 ``lifetime`` 属性是
        #: **整数秒**(``system.dictionaries.lifetime`` 需要的形态)。
        self._lifetime = lifetime
        self.layout_name = layout_name or getattr(layout, "name", "")
        self.source_sql = source_sql
        self.create_sql = create_sql

        # ── 状态与统计(system.dictionaries 的列)──────────────────────
        self.status = STATUS_NOT_LOADED
        self.last_exception = ""
        self.loading_start_time = "1970-01-01 00:00:00"
        self.loading_duration = 0.0
        self._last_successful_update = 0.0
        self.query_count = 0
        self.hit_count = 0
        #: ``origin`` —— 真实 CH 里 'Load'(CREATE DICTIONARY 建的)/
        #: 'Config'(配置档里的)。我们都是前者。
        self.origin = "Load"
        self.storage = "memory"

        # 属性名 → 位置(非键属性在值元组里的下标)
        self._attr_index: Dict[str, int] = {}
        self._key_index: Dict[str, int] = {}
        self._value_attribute_names: List[str] = []
        self._recompute_indexes()

    # ── 索引 ────────────────────────────────────────────────────────────

    def _recompute_indexes(self) -> None:
        key_set = set(self.key_names)
        self._value_attribute_names = [
            a.name for a in self.attributes if a.name not in key_set
        ]
        self._attr_index = {name: i for i, name in enumerate(self._value_attribute_names)}
        self._key_index = {}
        for i, a in enumerate(self.attributes):
            if a.name in key_set:
                self._key_index[a.name] = i

    def attribute_by_name(self, attr: str) -> Optional[DictionaryAttribute]:
        for a in self.attributes:
            if a.name.lower() == str(attr).lower():
                return a
        return None

    def get_attribute(self, attr: str) -> Optional[DictionaryAttribute]:
        return self.attribute_by_name(attr)

    def key_columns(self) -> List[int]:
        """键列在"源行"里的下标(按 DDL 里 key_names 的顺序)。"""
        return [self._key_index[n] for n in self.key_names if n in self._key_index]

    def range_attribute_indexes(self) -> Tuple[int, int]:
        """``range_hashed`` 用:找 range_min / range_max 在值元组里的下标。"""
        names = [n.lower() for n in self._value_attribute_names]
        rmin = names.index("range_min") if "range_min" in names else 0
        rmax = names.index("range_max") if "range_max" in names else min(rmin + 1, len(names) - 1)
        return rmin, rmax

    def hierarchical_attribute(self) -> Optional[DictionaryAttribute]:
        for a in self.attributes:
            if a.hierarchical:
                return a
        return None

    # ── 载入 ────────────────────────────────────────────────────────────

    def load(self, ctx: Any) -> None:
        """**真正去读源、建布局** —— 由 ``create_dictionary`` 与重载触发。

        真实 ClickHouse 的字典是**后台异步**加载的(所以刚建完
        ``status`` 可能是 ``LOADING``)。mini-clickhouse 为了确定性
        **同步**加载 —— 一句 ``CREATE DICTIONARY`` 返回时,字典一定已经
        是 ``LOADED`` 或 ``FAILED``。这是一个有意的、便于测试的偏离。
        """
        self.status = STATUS_LOADING
        self.loading_start_time = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        start = time.perf_counter()
        try:
            rows_data = self._load_rows(ctx)
            keys, values = rows_data
            # direct / cache 布局的"内层"要先灌数据
            if isinstance(self.layout, CacheLayout) and self.layout._inner is not None:
                self.layout._inner.load(keys, values)
            if isinstance(self.layout, DirectLayout):
                self.layout.load_from_source(ctx)  # type: ignore[attr-defined]
            else:
                self.layout.load(keys, values)
            self.status = STATUS_LOADED
            self.last_exception = ""
            self._last_successful_update = time.time()
        except MiniChError as exc:
            self.status = STATUS_FAILED
            self.last_exception = str(exc)
            raise
        except Exception as exc:  # pragma: no cover - 防御
            self.status = STATUS_FAILED
            self.last_exception = str(exc)
            raise_error(ErrorCode.NOT_IMPLEMENTED, f"Dictionary {self.name} failed to load: {exc}")
        finally:
            self.loading_duration = time.perf_counter() - start
            self._lifetime.mark_loaded()

    def _load_rows(self, ctx: Any) -> Tuple[List[Tuple[Any, ...]], List[Tuple[Any, ...]]]:
        if self._source_obj is None:
            raise_error(ErrorCode.LOGICAL_ERROR, f"Dictionary {self.name} has no source")
        names, rows = self._source_obj.read(ctx)
        name_to_col = {}
        for i, n in enumerate(names):
            name_to_col.setdefault(str(n).lower(), i)

        def col_index(col_name: str, *, required: bool = True) -> Optional[int]:
            idx = name_to_col.get(col_name.lower())
            if idx is None and required:
                raise_error(
                    ErrorCode.NO_SUCH_COLUMN_IN_TABLE,
                    f"Dictionary {self.name}: source has no column '{col_name}'",
                )
            return idx

        key_cols = [col_index(n) for n in self.key_names]
        value_cols = [col_index(n) for n in self._value_attribute_names]
        keys: List[Tuple[Any, ...]] = []
        values: List[Tuple[Any, ...]] = []
        for row in rows:
            keys.append(tuple(self._coerce(row[i], self._attr_type(self.key_names[j]))
                              for j, i in enumerate(key_cols)))
            values.append(tuple(
                self._coerce(row[i], self._value_attribute_types()[j])
                for j, i in enumerate(value_cols)
            ))
        return keys, values

    def _attr_type(self, name: str) -> Optional[DataType]:
        a = self.attribute_by_name(name)
        return a.type if a else None

    def _value_attribute_types(self) -> List[DataType]:
        return [self.attribute_by_name(n).type for n in self._value_attribute_names]

    @staticmethod
    def _coerce(value: Any, dtype: Optional[DataType]) -> Any:
        if dtype is None:
            return value
        try:
            return dtype.normalize(value)
        except Exception:
            return value

    # ── 查找 ────────────────────────────────────────────────────────────

    def maybe_reload(self, ctx: Any) -> None:
        """**惰性刷新** —— 到期且被访问时才重读源。"""
        if self._lifetime.is_expired():
            try:
                self.load(ctx)
            except MiniChError:
                # 刷新失败不该让查询崩:保留旧数据,把异常记在 last_exception。
                self.status = STATUS_FAILED

    def normalize_key(self, values: Sequence[Any]) -> Tuple[Any, ...]:
        """把 ``dictGet`` 收到的键参数统一成 tuple。"""
        vals = list(values)
        if len(vals) == 1 and isinstance(vals[0], (tuple, list)):
            return tuple(vals[0])
        return tuple(vals)

    def has_key(self, key_values: Sequence[Any]) -> bool:
        key = self.normalize_key(key_values)
        return self.layout.get(key) is not None

    def lookup(self, key_values: Sequence[Any], ts: Any = None) -> Optional[Tuple[Any, ...]]:
        """返回**整行值元组**(非键属性的值);缺失返回 ``None``。"""
        self.query_count += 1
        key = self.normalize_key(key_values)
        row = self.layout.get(key, ts)
        if row is not None:
            self.hit_count += 1
        return row

    def get_value(
        self, attr: str, key_values: Sequence[Any], ts: Any = None
    ) -> Tuple[bool, Any]:
        """返回 ``(found, value)``。

        找不到键时 **value 是该属性的 DEFAULT**(而不是 None)—— 这是
        外部字典最重要的语义之一:维表里没有这个键,字典回你一个缺省值,
        查询不会因此报错或产出 NULL。
        """
        attribute = self.attribute_by_name(attr)
        if attribute is None:
            raise_error(
                ErrorCode.THERE_IS_NO_COLUMN,
                f"Dictionary {self.database}.{self.name} has no attribute '{attr}'",
            )
        row = self.lookup(key_values, ts)
        if row is None:
            return False, attribute.default_value
        idx = self._attr_index.get(attribute.name)
        if idx is None:
            # 取的是键列自己
            key = self.normalize_key(key_values)
            kidx = self.key_names.index(attribute.name) if attribute.name in self.key_names else 0
            return True, key[kidx] if kidx < len(key) else attribute.default_value
        return True, row[idx]

    # ── 层级 ────────────────────────────────────────────────────────────

    def hierarchy(self, key_values: Sequence[Any]) -> List[Any]:
        """``dictGetHierarchy`` —— 从键一路沿 ``hierarchical`` 属性爬到顶。"""
        attr = self.hierarchical_attribute()
        if attr is None:
            raise_error(
                ErrorCode.NOT_IMPLEMENTED,
                f"Dictionary {self.name} has no HIERARCHICAL attribute; "
                f"dictGetHierarchy requires one",
            )
        out: List[Any] = []
        seen = set()
        cur = self.normalize_key(key_values)
        while cur is not None:
            if cur in seen:
                break
            seen.add(cur)
            out.append(cur[0] if len(cur) == 1 else cur)
            found, parent = self.get_value(attr.name, cur)
            if not found:
                break
            if parent is None or parent == 0:
                break
            cur = (parent,) if len(cur) == 1 else tuple([parent])
        return out

    def children(self, key_values: Sequence[Any]) -> List[Any]:
        """``dictGetChildren`` —— 直接子节点(一层)。"""
        attr = self.hierarchical_attribute()
        if attr is None:
            raise_error(ErrorCode.NOT_IMPLEMENTED,
                        f"Dictionary {self.name} has no HIERARCHICAL attribute")
        target = self.normalize_key(key_values)[0]
        out = []
        for key in self._all_keys():
            found, parent = self.get_value(attr.name, key)
            if found and parent == target:
                out.append(key[0] if len(key) == 1 else key)
        return out

    def descendants(self, key_values: Sequence[Any], level: int = -1) -> List[Any]:
        """``dictGetDescendants`` —— 所有后代,``level`` 限定深度(-1 = 无限)。"""
        out: List[Any] = []
        frontier = [self.normalize_key(key_values)]
        depth = 0
        while frontier and (level < 0 or depth < level):
            nxt = []
            for key in frontier:
                for child in self.children(key):
                    child_key = (child,) if not isinstance(child, (tuple, list)) else tuple(child)
                    out.append(child)
                    nxt.append(child_key)
            frontier = nxt
            depth += 1
        return out

    def is_in(self, child: Sequence[Any], ancestor: Sequence[Any]) -> bool:
        """``dictIsIn`` —— ``child`` 是否在 ``ancestor`` 的层级链上。"""
        target = self.normalize_key(ancestor)
        chain = self.hierarchy(child)
        for item in chain:
            item_key = (item,) if not isinstance(item, (tuple, list)) else tuple(item)
            if item_key == target:
                return True
        return False

    def _all_keys(self) -> List[Tuple[Any, ...]]:
        layout = self.layout
        if isinstance(layout, HashedLayout):
            return list(layout._table.keys())
        if isinstance(layout, HashedArrayLayout):
            return list(layout._index.keys())
        if isinstance(layout, SparseHashedLayout):
            out: List[Tuple[Any, ...]] = []
            for block in layout._blocks:
                out.extend(block.keys())
            return out
        if isinstance(layout, FlatLayout):
            return [(i,) for i, s in enumerate(layout._slots) if s is not None]
        if isinstance(layout, RangeHashedLayout):
            return list(layout._ranges.keys())
        return []

    # ── 元数据 / 统计(system.dictionaries 读这些)────────────────────

    @property
    def element_count(self) -> int:
        return self.layout.element_count()

    @property
    def bytes_allocated(self) -> int:
        return self.layout.bytes_allocated()

    @property
    def load_factor(self) -> float:
        return self.layout.load_factor()

    @property
    def last_successful_update_time(self) -> str:
        if not self._last_successful_update:
            return "1970-01-01 00:00:00"
        return _dt.datetime.fromtimestamp(self._last_successful_update).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    @property
    def lifetime_seconds(self) -> int:
        return self._lifetime.seconds

    @property
    def lifetime(self) -> int:
        """生命周期(**秒**)—— ``system.dictionaries.lifetime`` 用整数。

        真正的 :class:`Lifetime` 对象通过 :attr:`lifetime_config` 拿。
        """
        return self._lifetime.seconds

    @property
    def lifetime_config(self) -> Lifetime:
        return self._lifetime

    @property
    def type(self) -> str:
        return self.layout_name

    @property
    def dictionary_type(self) -> str:
        return self.layout_name

    @property
    def source_name(self) -> str:
        return self._source_obj.name if self._source_obj is not None else ""

    @property
    def source(self) -> str:
        """``system.dictionaries.source`` 列 —— **是字符串,不是对象**。

        真正的源对象在 :attr:`source_obj`。系统表用 ``getattr`` 式映射读
        ``source`` 并直接塞进 String 列,所以这里必须给字符串。这也是
        一个真实的设计约束:系统表的"列"和内存里的"对象"不是一回事。
        """
        return self.source_display

    @property
    def source_obj(self) -> Optional[ISource]:
        return self._source_obj

    @property
    def source_display(self) -> str:
        return self._source_obj.describe() if self._source_obj is not None else ""

    @property
    def key(self) -> str:
        if len(self.key_names) == 1:
            return self.key_names[0]
        return "tuple(" + ", ".join(self.key_names) + ")"

    @property
    def hit_rate(self) -> float:
        if not self.query_count:
            return 0.0
        return self.hit_count / float(self.query_count)

    @property
    def lifetime_value(self) -> int:
        return self._lifetime.seconds

    def value_size(self) -> int:
        """一行的近似字节数(供布局估内存)。"""
        total = 0
        for a in self.attributes:
            if not a.is_key:
                total += a.type.get_size_of_value_in_memory()
        return max(8, total)

    def describe_row(self) -> Dict[str, Any]:
        """``system.dictionaries`` 的一行(字典形态)。

        .. note::

           真实的 ``system.dictionaries`` 由系统表模块用 ``getattr`` 式
           映射读取我们的属性;``describe_row`` 是同一份数据的**显式**版本,
           便于单测与文档。
        """
        return {
            "database": self.database,
            "name": self.name,
            "status": self.status,
            "origin": self.origin,
            "type": self.layout_name,
            "key": self.key,
            "bytes_allocated": self.bytes_allocated,
            "element_count": self.element_count,
            "load_factor": self.load_factor,
            "source": self.source_display,
            "lifetime": self._lifetime.seconds,
            "loading_start_time": self.loading_start_time,
            "loading_duration": self.loading_duration,
            "last_exception": self.last_exception,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<Dictionary {self.database}.{self.name} layout={self.layout_name} "
                f"status={self.status} elements={self.element_count}>")


# ══════════════════════════════════════════════════════════════════════════
# CREATE / DROP / RELOAD
# ══════════════════════════════════════════════════════════════════════════


def create_dictionary(ctx: Any, node: Any) -> "Dictionary":
    """``CREATE DICTIONARY`` 的落地实现 —— 由解释器调用。

    流程:

    .. code-block:: text

       node(database, name, attributes, primary_key, source, layout, lifetime)
            │
            ├─ 解析属性 → DictionaryAttribute(类型 + DEFAULT + 标志)
            ├─ 建源   (SOURCE 注册表)
            ├─ 建布局 (LAYOUT 注册表,可能要先建内层布局)
            ├─ 同步 load()(读源 → 按主键切键/值 → 灌布局)
            └─ 登记进 ctx.dictionaries[<db>.<name>](小写)

    .. note::

       **同名处理**:mini-clickhouse 里重复 ``CREATE DICTIONARY`` 同名会
       直接覆盖(真实 CH 会报 ``DICTIONARY_ALREADY_EXISTS``;我们的
       ``IF NOT EXISTS`` 只是简单跳过)。这个差异写在报告里。
    """
    global _CURRENT_CONTEXT
    _CURRENT_CONTEXT = ctx

    database = getattr(node, "database", None) or getattr(ctx, "database", "default")
    name = node.name

    existing = resolve_dictionary(_key_for(database, name), ctx)
    if existing is not None and getattr(node, "if_not_exists", False):
        return existing

    attributes = _build_attributes(ctx, node)
    key_names = [str(k) for k in (node.primary_key or [])]
    if not key_names:
        # 没写 PRIMARY KEY → 用第一个属性当键(与 ClickHouse 的约定一致)
        if attributes:
            key_names = [attributes[0].name]
    known = {a.name for a in attributes}
    for k in key_names:
        if k not in known:
            raise_error(
                ErrorCode.THERE_IS_NO_COLUMN,
                f"PRIMARY KEY column '{k}' is not among the dictionary attributes",
            )
    for a in attributes:
        a.is_key = a.name in set(key_names)

    source = _build_source(ctx, node, None)
    layout = _build_layout(node)

    lifetime = Lifetime(*parse_lifetime(getattr(node, "lifetime", None)))
    dic = Dictionary(
        database, name, attributes, key_names, source, layout, lifetime,
        layout_name=getattr(node, "layout", "") or layout.name,
        source_sql=_source_sql(node),
        create_sql=_safe_to_sql(node),
    )
    dic._source_obj.dictionary = dic
    layout.dictionary = dic

    # cache 布局先把内层建好再灌数据
    if isinstance(layout, CacheLayout):
        layout.set_inner(HashedLayout(dic))

    register_dictionary(ctx, dic)
    dic.load(ctx)
    return dic


def _build_attributes(ctx: Any, node: Any) -> List[DictionaryAttribute]:
    attrs: List[DictionaryAttribute] = []
    for entry in node.attributes:
        attr_name, type_name, default_node = entry
        clean_type, flags = _split_attribute_type(type_name)
        dtype = get_data_type(clean_type)
        default_value = _eval_attribute_default(ctx, default_node, dtype)
        attrs.append(DictionaryAttribute(
            name=attr_name,
            dtype=dtype,
            default_value=default_value,
            hierarchical=flags.get("HIERARCHICAL", False),
            injective=flags.get("INJECTIVE", False),
            is_object_id=flags.get("IS_OBJECT_ID", False),
        ))
    return attrs


def _eval_attribute_default(ctx: Any, default_node: Any, dtype: DataType) -> Any:
    """把属性的 ``DEFAULT`` 求值成一个**具体的默认值**。

    这是字典最重要的语义之一:**缺失键返回该属性的 DEFAULT**(而不是报错)。
    字面量(``DEFAULT ''`` / ``DEFAULT 0``)直接取;更复杂的表达式
    (``DEFAULT now()``)退化成在表达式求值器里算一次常量。
    """
    if default_node is None:
        return dtype.default_value()
    if type(default_node).__name__ == "Literal":
        val = getattr(default_node, "value", None)
        try:
            return dtype.normalize(val)
        except Exception:
            return val
    try:
        from .execution.expression import Evaluator, Scope

        col = Evaluator(ctx, Scope(), 1).evaluate(default_node)
        return dtype.normalize(col.column.get(0))
    except Exception:
        return dtype.default_value()


def _build_source(ctx: Any, node: Any, dic: Optional[Dictionary]) -> ISource:
    source_name = (getattr(node, "source", None) or "").lower()
    if not source_name:
        raise_error(ErrorCode.UNKNOWN_STORAGE, "CREATE DICTIONARY requires a SOURCE(...)")
    factory = _SOURCES.get(source_name)
    if factory is None:
        raise_error(
            ErrorCode.UNKNOWN_STORAGE,
            f"Unknown dictionary source '{node.source}'. Known: "
            f"{', '.join(dictionary_sources())}",
        )
    return factory(getattr(node, "source_params", None), dic)


def _build_layout(node: Any) -> ILayout:
    layout_name = (getattr(node, "layout", None) or "flat").lower()
    factory = _LAYOUTS.get(layout_name)
    if factory is None:
        raise_error(
            ErrorCode.UNKNOWN_STORAGE,
            f"Unknown dictionary layout '{node.layout}'. Known: "
            f"{', '.join(dictionary_layouts())}",
        )
    return factory(getattr(node, "layout_params", None), None)


def _source_sql(node: Any) -> str:
    src = getattr(node, "source", "") or ""
    params = getattr(node, "source_params", []) or []
    return f"{src}({', '.join(str(p) for p in params)})"


def _safe_to_sql(node: Any) -> str:
    try:
        return node.to_sql()
    except Exception:  # pragma: no cover - 防御
        return ""


def drop_dictionary(ctx: Any, database: str, name: str, if_exists: bool = False) -> None:
    """``DROP DICTIONARY``。

    ``IF EXISTS`` 时字典不存在**不报错**;否则抛 ``UNKNOWN_TABLE``
    (真实 ClickHouse 报的是 ``UNKNOWN_DICTIONARY``,我们取最贴近的稳定编号)。
    """
    key = _key_for(database, name)
    existed = False
    if ctx is not None and hasattr(ctx, "dictionaries") and key in ctx.dictionaries:
        ctx.dictionaries.pop(key, None)
        existed = True
    if _REGISTRY.pop(key, None) is not None:
        existed = True
    if not existed and not if_exists:
        raise_error(ErrorCode.UNKNOWN_TABLE, f"Dictionary `{database}.{name}` not found")


def reload_dictionaries(ctx: Any, table: Any = None) -> None:
    """``SYSTEM RELOAD DICTIONARIES``(全部)/ ``SYSTEM RELOAD DICTIONARY db.name``(单个)。

    解释器把 ``node.table`` 传进来:``None`` 表示"全部"。
    """
    if table:
        # table 可能是 "db.name" 或 "name"
        if isinstance(table, str) and "." in table:
            db, _, nm = table.partition(".")
            key = _key_for(db, nm)
        else:
            db = getattr(ctx, "database", "default")
            key = _key_for(db, str(table))
        dic = None
        if ctx is not None and hasattr(ctx, "dictionaries"):
            dic = ctx.dictionaries.get(key)
        if dic is None:
            dic = _REGISTRY.get(key)
        if dic is None:
            raise_error(ErrorCode.UNKNOWN_TABLE, f"Dictionary `{table}` not found")
        dic.load(ctx)
        return
    seen = set()
    for dic in list(_REGISTRY.values()):
        if id(dic) in seen:
            continue
        seen.add(id(dic))
        dic.load(ctx)


# ══════════════════════════════════════════════════════════════════════════
# dictGet 家族 —— 把字典接进 SQL 的那一组标量函数
# ══════════════════════════════════════════════════════════════════════════
#
# .. warning::
#
#    **这几个函数"注册在哪"是一个真实的工程约束。** 真正的 ClickHouse 在
#    **查询分析期**就把字典名(字面量)解析成一个 ``const IDictionary*``,
#    随执行计划一路带到执行期,所以 ``dictGet`` 的返回类型与实现都能在
#    分析期定死。mini-clickhouse 的求值器只把**实参类型**交给
#    ``get_return_type``(拿不到实参的值),因此普通 ``dictGet`` **无法**
#    在分析期推出属性类型。我们的取舍是:
#
#    * 带类型后缀的 ``dictGetString`` / ``dictGetUInt64`` / … 精确声明类型;
#    * 普通 ``dictGet`` / ``dictGetOrDefault`` / ``dictGetOrNull`` 声明成
#      ``String``(占位),但**执行期返回的列里装的是真实值**(列的真实类型
#      来自属性声明)。需要参与算术/比较时,请用带类型后缀的版本 ——
#      这与线上"字典字段用哪种类型要写清楚"的经验一致。

from .functions.base import FunctionBase, register_function  # noqa: E402


def _const_value(arg: Any, row: int = 0) -> Any:
    """取一个"应当是常量"的实参的值(字典名、属性名都是字面量)。"""
    return arg.column.get(row)


def _row_value(arg: Any, row: int) -> Any:
    """取第 ``row`` 行的值 —— 常量列每行返回同一个值。"""
    return arg.column.get(row)


def _build_column(dtype: Optional[DataType], values: List[Any]) -> Any:
    """按给定类型造一列(类型拿不到时按值猜)。"""
    if dtype is None:
        from .columns import column_from_values

        return column_from_values(values)
    col = dtype.create_column()
    for v in values:
        try:
            col.append(dtype.normalize(v) if v is not None else None)
        except Exception:
            col.append(v)
    return col


#: 带类型后缀的 dictGet —— 名字 → 返回类型名。
_INT_SUFFIXES = (8, 16, 32, 64, 128, 256)
_TYPE_SUFFIX_MAP: Dict[str, str] = {
    "dictgetstring": "String",
    "dictgetfloat32": "Float32",
    "dictgetfloat64": "Float64",
    "dictgetdate": "Date",
    "dictgetdatetime": "DateTime",
    "dictgetuuid": "UUID",
    "dictgetipv4": "IPv4",
    "dictgetipv6": "IPv6",
}
for _bits in _INT_SUFFIXES:
    _TYPE_SUFFIX_MAP[f"dictgetint{_bits}"] = f"Int{_bits}"
    _TYPE_SUFFIX_MAP[f"dictgetuint{_bits}"] = f"UInt{_bits}"
del _bits

#: 返回"数组"的几个函数
_ARRAY_FUNCS = ("dictgethierarchy", "dictgetchildren", "dictgetdescendants", "dictgetall")


class FunctionDictGet(FunctionBase):
    """``dictGet`` 家族的统一实现 —— 用 ``self.name`` 区分行为。

    为什么写成一个类而不是二十个类? 因为它们的**骨架完全一样**
    ("取字典 → 取键 → 查布局 → 缺失时按规则回默认值"),只有
    "缺省值从哪来"和"返回什么类型"两点不同。真实 ClickHouse 用的是
    一个基类 ``FunctionDictionaryBase`` + 一堆薄子类,思路相同。

    .. note::

       **常量折叠被刻意关掉**(``deterministic = False``)。因为字典会随
       生命周期刷新,若把 ``dictGet(...)`` 在分析期折叠成常量,那么
       "刷新后的新值"在同一条查询里就永远看不到了。真实 ClickHouse 也
       把字典读取视为**非确定性**(字典内容会变)。
    """

    deterministic = False
    #: 键为 NULL 时不走"整列 NULL 传播",而是交给字典按"未命中"处理。
    propagate_nulls = False
    name = "dictGet"

    # ── 类型 ────────────────────────────────────────────────────────────

    def get_return_type(self, args: Sequence[DataType]) -> DataType:
        key = self.name.lower()
        if key in ("dicthas", "dictisin"):
            return get_data_type("UInt8")
        if key in _ARRAY_FUNCS:
            return get_data_type("Array(String)")
        if key == "dictgetornull":
            return get_data_type("Nullable(String)")
        if key in _TYPE_SUFFIX_MAP:
            return get_data_type(_TYPE_SUFFIX_MAP[key])
        return get_data_type("String")

    # ── 执行 ────────────────────────────────────────────────────────────

    def execute(self, args: Sequence[Any], rows: int, context: Any = None) -> Any:
        key = self.name.lower()
        if not args:
            raise_error(
                ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                f"{self.name} requires at least a dictionary name argument",
            )
        dict_name = _const_value(args[0])
        dic = get_dictionary(str(dict_name), context)
        if context is not None:
            dic.maybe_reload(context)

        if key == "dicthas":
            return self._do_has(dic, args[1:], rows)
        if key == "dictisin":
            return self._do_is_in(dic, args[1:], rows)
        if key in ("dictgethierarchy", "dictgetchildren", "dictgetdescendants"):
            return self._do_hierarchy(key, dic, args[1:], rows)
        if key == "dictgetall":
            return self._do_all(dic, args[1:], rows)
        return self._do_get(key, dic, args[1:], rows)

    # ── 具体实现 ────────────────────────────────────────────────────────

    def _key_args_and_ts(self, dic: "Dictionary", rest: Sequence[Any]) -> Tuple[Sequence[Any], Any]:
        """把"属性之后的实参"拆成 (键实参, 时间点实参)。"""
        if isinstance(dic.layout, RangeHashedLayout):
            return rest[:1], (rest[1] if len(rest) > 1 else None)
        return rest, None

    def _do_get(self, key: str, dic: "Dictionary", rest: Sequence[Any], rows: int) -> Any:
        if not rest:
            raise_error(ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                        f"{self.name} requires an attribute argument")
        attr = str(_const_value(rest[0]))
        rest = list(rest[1:])
        default_arg = None
        if key == "dictgetordefault":
            if len(rest) < 2:
                raise_error(ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                            "dictGetOrDefault requires a default value argument")
            default_arg = rest[-1]
            rest = rest[:-1]
        key_args, ts_arg = self._key_args_and_ts(dic, rest)

        attribute = dic.attribute_by_name(attr)
        if attribute is None:
            raise_error(
                ErrorCode.THERE_IS_NO_COLUMN,
                f"Dictionary {dic.database}.{dic.name} has no attribute '{attr}'",
            )
        # 目标类型:带类型后缀的用后缀;dictGetOrNull 包成 Nullable;
        # 其余用属性声明的类型。
        if key == "dictgetornull":
            out_dtype: Any = f"Nullable({attribute.type.get_name()})"
        else:
            out_dtype = _TYPE_SUFFIX_MAP.get(key) or attribute.type

        values: List[Any] = []
        for i in range(rows):
            key_vals = [_row_value(a, i) for a in key_args]
            ts = _row_value(ts_arg, i) if ts_arg is not None else None
            found, value = dic.get_value(attr, key_vals, ts=ts)
            if not found:
                if key == "dictgetordefault":
                    value = _row_value(default_arg, i)
                elif key == "dictgetornull":
                    value = None
                else:
                    value = attribute.default_value
            values.append(value)

        dtype = get_data_type(out_dtype) if isinstance(out_dtype, str) else out_dtype
        return _build_column(dtype, values)

    def _do_has(self, dic: "Dictionary", rest: Sequence[Any], rows: int) -> Any:
        key_args, ts_arg = self._key_args_and_ts(dic, rest)
        values = []
        for i in range(rows):
            key_vals = [_row_value(a, i) for a in key_args]
            if ts_arg is not None:
                ts = _row_value(ts_arg, i)
                found = dic.get_value(dic.attributes[0].name, key_vals, ts=ts)[0]
            else:
                found = dic.has_key(key_vals)
            values.append(1 if found else 0)
        return _build_column(get_data_type("UInt8"), values)

    def _do_is_in(self, dic: "Dictionary", rest: Sequence[Any], rows: int) -> Any:
        if len(rest) < 2:
            raise_error(ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                        "dictIsIn requires (child_key, ancestor_key)")
        child_arg, anc_arg = rest[0], rest[1]
        values = []
        for i in range(rows):
            child = _row_value(child_arg, i)
            anc = _row_value(anc_arg, i)
            ok = dic.is_in((child,), (anc,))
            values.append(1 if ok else 0)
        return _build_column(get_data_type("UInt8"), values)

    def _do_hierarchy(self, key: str, dic: "Dictionary", rest: Sequence[Any], rows: int) -> Any:
        if not rest:
            raise_error(ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                        f"{self.name} requires a key argument")
        key_args, _ts = self._key_args_and_ts(dic, rest)
        level_arg = rest[-1] if key == "dictgetdescendants" and len(rest) > 1 else None
        values: List[Any] = []
        for i in range(rows):
            key_vals = [_row_value(a, i) for a in key_args]
            if key == "dictgethierarchy":
                values.append(list(dic.hierarchy(key_vals)))
            elif key == "dictgetchildren":
                values.append(list(dic.children(key_vals)))
            else:
                level = _row_value(level_arg, i) if level_arg is not None else -1
                values.append(list(dic.descendants(key_vals, level=level)))
        return _build_column(get_data_type("Array(String)"), values)

    def _do_all(self, dic: "Dictionary", rest: Sequence[Any], rows: int) -> Any:
        if not rest:
            raise_error(ErrorCode.NUMBER_OF_ARGUMENTS_DOESNT_MATCH,
                        "dictGetAll requires an attribute argument")
        attr = str(_const_value(rest[0]))
        key_args, _ts = self._key_args_and_ts(dic, rest[1:])
        attribute = dic.attribute_by_name(attr)
        if attribute is None:
            raise_error(ErrorCode.THERE_IS_NO_COLUMN,
                        f"Dictionary {dic.database}.{dic.name} has no attribute '{attr}'")
        values = []
        for i in range(rows):
            key_vals = [_row_value(a, i) for a in key_args]
            found, value = dic.get_value(attr, key_vals)
            values.append([value] if found else [])
        array_type = get_data_type(f"Array({attribute.type.get_name()})")
        return _build_column(array_type, values)


def _register_dict_functions() -> None:
    """把整个家族注册进标量函数表(导入本模块即注册)。

    所有名字共用 :class:`FunctionDictGet` 这一个类 —— 行为靠 ``self.name``
    区分。``register_function(name, cls)`` 会用无参构造造实例并把
    ``name`` 写进去,所以一个类能安全地占住二十多个名字。
    """
    names = [
        "dictGet", "dictGetOrDefault", "dictGetOrNull", "dictHas",
        "dictGetHierarchy", "dictGetChildren", "dictGetDescendants",
        "dictGetAll", "dictIsIn",
    ]
    names += [f"dictGetInt{b}" for b in _INT_SUFFIXES]
    names += [f"dictGetUInt{b}" for b in _INT_SUFFIXES]
    names += ["dictGetString", "dictGetFloat32", "dictGetFloat64", "dictGetDate",
              "dictGetDateTime", "dictGetUUID", "dictGetIPv4", "dictGetIPv6"]
    for name in names:
        register_function(name, FunctionDictGet,
                          description="外部字典查找(见 minich/dictionaries.py)")


_register_dict_functions()

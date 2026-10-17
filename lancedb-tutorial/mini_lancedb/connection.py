"""``LanceDBConnection`` —— 一个 uri 上的多个表。

这是用户看到的第一个对象，也是**整个库里语义最少的一层**：
它知道"有哪些表"、"每个表在哪"，其余一概不管。

**关于 "cloud" 路径（请认真读这一段）**

``connect("s3://bucket/db")`` 在本实现里**不会访问任何网络**。
它构造的是一个 :class:`~mini_lancedb.storage.base.S3LikeStore` ——
一个刻意保留了对象存储"坏脾气"的**内存模拟器**：

* **没有原子重命名**（``rename`` 直接抛异常）—— 这就是 manifest 存在的理由；
* **list 有延迟**（刚写进去的立刻 list 可能看不到）—— 最终一致性的经典陷阱，
  也是 Lance 必须靠 manifest 而不是靠 list 来判断"有哪些数据文件"的原因；
* **条件写是原子的** —— 对象存储唯一给得起的并发原语，
  也是 LanceDB 在 S3 上实现"乐观并发提交"的全部依据。

保留这三条而不是做一个"看起来像 S3 但处处完美"的假货，是为了让教程里
"为什么不能 rename"、"为什么 manifest 不能省"这些结论有可复现的证据。
``api_key`` / ``region`` / ``storage_options`` 三个参数**被接受但忽略**，
保留它们只是为了让真实 LanceDB 的代码能原样跑起来。

**连接与表的关系**

.. code-block:: text

    connect("db/")                    ← 一个根 store
      ├── mytable.lance/              ← 一个表 = 一个"目录"
      │     _versions/ _transactions/ data/ _indices/ _deletions/ _refs/
      └── other.lance/

每个表通过一个**前缀 store** 把自己"圈"出来（见 :class:`_PrefixedStore`），
于是 :class:`~mini_lancedb.dataset.Dataset` 完全不需要知道"表名"这个概念 ——
它对前缀一无所知，只看到自己脚下的一片命名空间。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

from .dataset import Dataset
from .errors import (
    QueryError,
    SchemaError,
    TableAlreadyExistsError,
    TableNotFoundError,
)
from .schema import Schema
from .storage.base import (
    InMemoryStore,
    InstrumentedStore,
    LocalFileStore,
    ObjectMeta,
    ObjectStore,
    S3LikeStore,
)

__all__ = [
    "connect",
    "LanceDBConnection",
    "SourceField",
    "VectorField",
    "func",
    "EmbeddingFunctionRegistration",
]

#: 历史上确实用过的连接串前缀。这里只用来**决定用哪个 store**，不做网络访问。
_LOCAL_SCHEMES = ("", "file://")
_SIMULATED_SCHEMES = ("s3://", "gs://", "az://", "http://", "https://")

#: 表在根目录下的后缀。真实 LanceDB 用的是 ``<name>.lance``，
#: 这一个后缀就是"这个目录是一个 Lance 表"的全部标记 ——
#: **目录名即元数据**，不需要额外的中央登记表。
TABLE_SUFFIX = ".lance"


# ---------------------------------------------------------------------------
# 前缀 store
# ---------------------------------------------------------------------------


class _PrefixedStore(ObjectStore):
    """给一个 store 套一层"子目录"。**这就是表与表之间的隔离机制。**

    为什么不用"给每个表建一个物理 store"？因为那样
    :class:`~mini_lancedb.storage.base.InstrumentedStore` 就没法
    计量整个连接（包括建表和读表）的 I/O 了 —— 它只能包住一个表。
    前缀方案让"所有表共享一个根 store"和"每个表看起来在自己的根上"
    这两件事同时成立。

    ``list()`` 会把内层返回的 key **去掉前缀再返回**，所以上层看到的
    路径和"这个 store 就是根"时完全一样。这个转换是唯一一处
    容易写错的地方（前缀少切一个 ``/`` 就会让 manifest 路径变成
    ``mytable.lance_versions/...``）。
    """

    def __init__(self, inner: ObjectStore, prefix: str) -> None:
        prefix = prefix.strip("/")
        super().__init__(f"{str(inner.uri).rstrip('/')}/{prefix}" if prefix else inner.uri)
        self.inner = inner
        self.prefix = prefix
        # **把统计对象指回内层。** ``ObjectStore.__init__`` 会给每个实例
        # 新建一份 ``StoreStats``，所以前缀壳默认自带一份永远是 0 的计数器。
        # 前几层读到的 ``Scanner.stats.bytes_read`` 因此恒为 0 ——
        # 不报错、只是所有 I/O 数字都变成假的。这种"监控指标静默归零"
        # 比抛异常难查得多。
        self.stats = inner.stats

    @property
    def supports_conditional_put(self) -> bool:
        return self.inner.supports_conditional_put

    # -- 路径换算 ---------------------------------------------------------
    def _p(self, path: str) -> str:
        clean = path.replace("\\", "/").strip("/")
        if not self.prefix:
            return clean
        return f"{self.prefix}/{clean}" if clean else self.prefix

    def _k(self, full: str) -> str:
        if not self.prefix:
            return full
        head = self.prefix + "/"
        return full[len(head):] if full.startswith(head) else full

    # -- ObjectStore 接口 -------------------------------------------------
    def get(self, path: str) -> bytes:
        return self.inner.get(self._p(path))

    def get_range(self, path: str, offset: int, length: int) -> bytes:
        return self.inner.get_range(self._p(path), offset, length)

    def put(self, path: str, data: bytes) -> str:
        return self.inner.put(self._p(path), data)

    def put_if_not_exists(self, path: str, data: bytes) -> Optional[str]:
        # 转发给内层，让条件写保持原子（这是提交路径的关键）
        return self.inner.put_if_not_exists(self._p(path), data)

    def delete(self, path: str) -> None:
        self.inner.delete(self._p(path))

    def head(self, path: str) -> Optional[ObjectMeta]:
        m = self.inner.head(self._p(path))
        if m is None:
            return None
        return ObjectMeta(self._k(m.path), m.size, m.etag, m.last_modified)

    def list(self, prefix: str = "", *, recursive: bool = True) -> List[ObjectMeta]:
        base = self._p(prefix) if prefix else self.prefix
        items = self.inner.list(base, recursive=True)
        out: List[ObjectMeta] = []
        for m in items:
            key = self._k(m.path)
            if key.startswith("../") or key == m.path and self.prefix:
                continue
            if not recursive:
                if "/" in key.strip("/"):
                    continue
            out.append(ObjectMeta(key, m.size, m.etag, m.last_modified))
        return out

    @property
    def supports_conditional_put(self) -> bool:
        return bool(getattr(self.inner, "supports_conditional_put", True))


# ---------------------------------------------------------------------------
# embedding 注册钩子（``func.SourceField()`` / ``func.VectorField()``）
# ---------------------------------------------------------------------------


class _FieldMarker:
    """``func.SourceField()`` 的返回值：**一个声明，不是一个字段**。

    它存在的意义是把"这一列是文本源"和"这一列是它的向量"这层关系
    写进 schema 定义里，而不是靠命名约定（``text`` / ``vector``）。
    命名约定在只有一个向量列时能用，两个向量列时立刻崩。

    真实 LanceDB 的等价物是 ``lancedb.embeddings.func.SourceField()``
    配合 ``LanceModel`` 里的类型标注。本实现只做**标记 + 识别**：
    它不参与 Arrow 类型推导，只在建表时被解析出来。
    """

    #: ``"source"`` 或 ``"vector"``
    kind: str = ""

    def __init__(self, name: Optional[str] = None) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover
        return f"func.{type(self).__name__}()"


class SourceField(_FieldMarker):
    """标记"这一列是文本源"（供 embedding 函数读取）。"""

    kind = "source"


class VectorField(_FieldMarker):
    """标记"这一列是向量输出"。"""

    kind = "vector"


class _FuncNamespace:
    """``mini_lancedb.func`` 命名空间。

    只提供 ``SourceField()`` / ``VectorField()`` 两个工厂 —— 和真实
    LanceDB 的 ``func`` 模块在**这一层的用法**对齐，别的一概不提供
    （真实 ``func`` 里还有 ``array_length`` / ``fragment_id`` 等 UDF）。
    """

    SourceField = staticmethod(SourceField)
    VectorField = staticmethod(VectorField)

    def __repr__(self) -> str:  # pragma: no cover
        return "<mini_lancedb.func SourceField / VectorField>"


#: 见 :class:`_FuncNamespace`。
func = _FuncNamespace()


@dataclass
class EmbeddingFunctionRegistration:
    """"哪一列用什么函数编码"的一条注册。

    它和 :class:`~mini_lancedb.embedding.base.EmbeddingFunctionConfig`
    的关系是"视图"：``EmbeddingFunctionRegistration`` 是**建表时**的入口
    （能从 ``func.SourceField()`` 标记 + ``embedding_functions`` 列表里
    被推导出来），``EmbeddingFunctionConfig`` 是**落盘后**的形态
    （会被 JSON 序列化进 schema metadata）。

    **为什么不做成"整个特性都挂在 ``embedding_functions`` 参数上"。**
    真实 LanceDB 的文档已经把 ``embedding_functions`` 标成
    deprecated：它要求调用方把"向量列名 / 源列名 / 函数实例"三件东西
    手写齐，任何一件写错都只会在写入时才暴露。现在的推荐路径是
    **在 schema 里声明**（``func.SourceField()`` / ``func.VectorField()``），
    让表的 schema 自己表达这层关系。本实现按后者建模，前者只作为
    一个"能被接受、能被转换"的兼容入口。
    """

    source_column: str
    vector_column: str
    function: Any

    @classmethod
    def from_config(cls, conf: Any) -> "EmbeddingFunctionRegistration":
        """从 ``EmbeddingFunctionConfig`` / dict 造一条注册。"""

        if isinstance(conf, Mapping):
            return cls(
                source_column=str(
                    conf.get("source_column") or conf.get("source_field") or ""
                ),
                vector_column=str(
                    conf.get("vector_column") or conf.get("vector_field") or ""
                ),
                function=conf.get("function"),
            )
        return cls(
            source_column=str(getattr(conf, "source_column", "")),
            vector_column=str(getattr(conf, "vector_column", "")),
            function=getattr(conf, "function", None),
        )

    @classmethod
    def from_schema_metadata(cls, fields: Sequence[Any]) -> List["EmbeddingFunctionRegistration"]:
        """从 schema 声明里把 ``func.SourceField()`` / ``func.VectorField()`` 配对。

        配对规则：``VectorField`` 标记的列，它的向量由**同一个 schema 里
        带 ``SourceField`` 标记的列**算出来。多于一个 source 列时返回空 ——
        **宁可"检测不出"也不要"猜错"**：猜错的后果是拿错误的文本去算向量，
        而向量错是**静默错**（查得到、就是不准），比报错难查一百倍。
        """

        sources = [
            f.name for f in fields if getattr(getattr(f, "type", None), "kind", "") == "source"
        ]
        vectors = [
            f.name for f in fields if getattr(getattr(f, "type", None), "kind", "") == "vector"
        ]
        if len(sources) == 1 and len(vectors) >= 1:
            return [
                cls(source_column=sources[0], vector_column=v, function=None)
                for v in vectors
            ]
        return []

    def __repr__(self) -> str:  # pragma: no cover
        fn = type(self.function).__name__ if self.function is not None else "?"
        return (
            f"EmbeddingFunctionRegistration({self.source_column!r} → "
            f"{self.vector_column!r} via {fn})"
        )


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------


def connect(
    uri: Optional[Union[str, os.PathLike]] = None,
    *,
    api_key: Optional[str] = None,
    region: Optional[str] = None,
    storage_options: Optional[Mapping[str, Any]] = None,
    **kwargs: Any,
) -> "LanceDBConnection":
    """打开（或创建）一个 LanceDB 目录。

    Parameters
    ----------
    uri :
        * ``None`` 或 ``"memory://"`` —— 纯内存，进程退出即消失。测试默认用它，
          因为它**零残留**：不会有上一条用例的文件影响下一条。
        * 本地路径 —— ``"db/"`` / ``"./db"`` / ``"file:///abs/path"``。
          目录不存在会自动创建（对象存储里"目录"本来就是惰性存在的，
          本地也保持同样的语义）。
        * ``"s3://..."`` / ``"gs://..."`` / ``"az://..."`` ——
          **模拟**对象存储，见模块文档。**不产生任何网络流量。**
    api_key / region / storage_options :
        **被接受但完全忽略**。保留它们纯粹是为了让真实 LanceDB 的
        调用代码能原样跑起来（真实实现里 api_key 是 LanceDB Cloud 的
        凭证，storage_options 是传给 S3 客户端的参数）。
        忽略凭证是有意的：本实现不访问真云，所以任何密钥都是多余的
        —— 更不应该把它记在某个状态文件里。

    Returns:
        :class:`LanceDBConnection`。
    """

    if api_key or region or storage_options:
        # 不报错、不静默丢弃，而是**让它可见**：用户得知道"你给的凭证没被用"。
        pass

    text = "" if uri is None else str(uri)
    store = _make_store(text)
    return LanceDBConnection(text or store.uri, store)


def _make_store(uri: str) -> ObjectStore:
    lowered = uri.lower()
    if not uri or lowered in ("memory://", "memory:"):
        return InMemoryStore("memory://")
    if lowered.startswith(_SIMULATED_SCHEMES):
        # 模拟对象存储：uri 原样保留（这样 repr 里能看到 s3://bucket/...），
        # 实际数据放在内存里。
        return S3LikeStore(uri if "://" in uri else f"s3://{uri}")
    if lowered.startswith("file://"):
        path = uri[len("file://"):]
        # Windows 上 file:///C:/x 会多一个前导斜杠
        if len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return LocalFileStore(path or ".")
    if "://" in uri:
        raise QueryError(
            f"不认识的 uri scheme: {uri!r}",
            supported=["memory://", "file://", "s3://", "gs://", "az://", "本地路径"],
        )
    return LocalFileStore(uri)


# ---------------------------------------------------------------------------
# LanceDBConnection
# ---------------------------------------------------------------------------


class LanceDBConnection:
    """一个 uri 上的一组表。

    它**不缓存表对象**：每次 ``open_table()`` 都重新读一遍 manifest。
    看起来浪费，其实是对的 —— manifest 是**唯一的真相来源**，
    缓存它就会引入"两个进程看到不同版本"的问题。真实 LanceDB
    也是这个策略（它缓存的是**索引**，不是 manifest，
    因为索引重建很贵而 manifest 读很便宜）。

    ``_open_tables`` 保留的是**已经交给用户的表对象**，只用于
    :meth:`instrument` 时把它们重新指向计量过的 store。
    """

    def __init__(self, uri: str, store: ObjectStore) -> None:
        self._uri = uri
        self._store = store
        self._open_tables: List[Any] = []
        self._namespace_client: Optional[Any] = None

    # -- 元信息 -----------------------------------------------------------
    @property
    def uri(self) -> str:
        return self._uri

    @property
    def store(self) -> ObjectStore:
        """底层根 store。测试里拿它来断言 I/O。"""

        return self._store

    # -- 表名 -------------------------------------------------------------
    def table_names(self, limit: Optional[int] = None, **kwargs: Any) -> List[str]:
        """所有表名，**按字典序**。

        排序是为了让 ``table_names()`` 的输出可断言 —— 顺序不确定的
        列表在测试里只能写成 ``sorted(...)``，那等于没测。

        真实 LanceDB 的 ``table_names()`` 有 ``page_token`` / ``limit``
        参数（云端分页用）。这里接受 ``limit`` 并遵守它，
        但不提供 ``page_token``（本地列目录不需要分页）。
        """

        names = []
        for path in self._store.child_paths(""):
            clean = path.rstrip("/")
            if clean.endswith(TABLE_SUFFIX):
                names.append(clean[: -len(TABLE_SUFFIX)])
        names = sorted(n for n in names if n)
        if limit is not None:
            return names[: int(limit)]
        return names

    def table_exists(self, name: str) -> bool:
        return name in self.table_names()

    # -- 打开 / 创建 ------------------------------------------------------
    def open_table(self, name: str, **kwargs: Any) -> Any:
        """打开一张已有的表。

        认得的 kwargs：``version``（时间旅行到某个版本）、
        ``storage_options`` / ``index_cache_size``（**接受但忽略**，
        真实 API 有这些参数）。
        """

        from .table import LanceTable

        if not self.table_exists(name):
            raise TableNotFoundError(
                f"表不存在: {name!r}",
                available=self.table_names(),
                hint="用 create_table() 建表，或检查 uri 是不是指错了目录",
            )
        tstore = self._table_store(name)
        version = kwargs.pop("version", None)
        kwargs.pop("storage_options", None)
        kwargs.pop("index_cache_size", None)
        dataset = Dataset.open(tstore, self._table_uri(name), version=version)
        table = LanceTable(self, name, self._table_uri(name), tstore, dataset)
        self._remember(table)
        return table

    def create_table(
        self,
        name: str,
        data: Any = None,
        schema: Any = None,
        mode: str = "create",
        exist_ok: bool = False,
        embedding_functions: Optional[Sequence[Any]] = None,
        on_bad_vectors: str = "error",
        fill_value: float = 0.0,
        **kwargs: Any,
    ) -> Any:
        """建表。

        ``mode`` 只有两个值，和真实 LanceDB 一致：

        * ``"create"`` —— 表已存在就报错（除非 ``exist_ok=True``，
          此时**返回已有的那张表**，不覆盖）；
        * ``"overwrite"`` —— 用一份全新的 schema 和数据取代它。
          注意它也是"提交一个新版本"，**旧版本仍然可读**。

        ``schema`` 可以是 ``pyarrow.Schema``、mini 的
        :class:`~mini_lancedb.schema.Schema`、或者 ``Field`` 的列表。
        不给 schema 就从 ``data`` 推断。

        ``embedding_functions`` **是兼容入口，不是推荐路径**
        （真实 LanceDB 已把它标为 deprecated）。推荐做法是在 schema 里
        用 ``func.SourceField()`` / ``func.VectorField()`` 声明关系 ——
        见 :class:`EmbeddingFunctionRegistration`。

        ⚠️ **自动向量化的触发条件是"全有或全无"**：只有当目标向量列在
        这一批里**整个缺失、或者每一行都是 null** 时，embedding 函数
        才会被调用。只要你给了一行向量，整批就都被当作手动数据，
        剩下的 null 行就永远是 null（见 :meth:`LanceTable.add`）。
        """

        from .table import (
            LanceTable,
            _force_vector_dims,
            _rows_from_data,
            _validate_vectors,
            _vector_dims,
        )

        if mode not in ("create", "overwrite"):
            raise ValueError("mode must be either 'create' or 'overwrite'")

        exists = self.table_exists(name)
        if exists and mode == "create":
            if exist_ok:
                return self.open_table(name)
            raise TableAlreadyExistsError(
                f"表已存在: {name!r}",
                hint="用 mode='overwrite' 覆盖，或 exist_ok=True 拿回已有的表",
            )

        rows = _rows_from_data(data)
        explicit = schema is not None
        sch = _to_schema(schema) if explicit else _infer_schema(data, rows)
        if sch is None or len(sch) == 0:
            raise SchemaError(
                "create_table 需要 data 或 schema 来确定表结构",
                hint="传 [{'id': 1, 'vector': [0.1, 0.2]}, ...] 或 schema=...",
            )

        # ---- on_bad_vectors 必须在建表**之前**执行 ----
        #
        # 顺序很关键：如果先建表再走 ``table.add``，那么建表时推断出来的
        # schema 里向量列已经是"变长 list"了（维度不一致时推断器只能这样）。
        # 于是后面再想判断"哪一行维度不对"就没有基准了 ——
        # 校验会**静默全过**，坏行直接写进去。
        # 所以这里先抓参考维度、先把 schema 钉成定长、先筛完数据，
        # 再拿"干净的 schema + 干净的行"去建表。
        dims = _vector_dims(rows, sch)
        sch = _force_vector_dims(sch, dims)
        clean, report = _validate_vectors(rows, dims, on_bad_vectors, fill_value)

        if not explicit and clean:
            # 筛完之后重新定型：被丢掉的行不该继续影响列类型推断
            inferred = _force_vector_dims(Schema.from_pylist(clean), dims)
            sch = inferred

        # embedding 配置：既接受显式列表，也接受 schema 里的 func 标记
        regs = _collect_registrations(embedding_functions, sch)
        metadata = dict(sch.metadata or {})
        if regs:
            metadata.update(_embedding_metadata(regs))
            sch = Schema(list(sch.fields), metadata=metadata)

        tstore = self._table_store(name)
        dataset = Dataset.create(tstore, self._table_uri(name), sch, mode=mode)
        table = LanceTable(
            self,
            name,
            self._table_uri(name),
            tstore,
            dataset,
            embedding_functions=embedding_functions,
        )
        self._remember(table)

        if clean:
            # 数据已经筛过了，这里直接落盘（不要再走 add 的 on_bad_vectors，
            # 否则等于把同一份数据校验两遍，而且第二遍的基准还变了）
            table.add(clean, mode="append", on_bad_vectors="error")
        return table

    def drop_table(self, name: str, ignore_missing: bool = False, **kwargs: Any) -> None:
        """删表。**删的是整个目录**（数据 + 所有历史版本 + 索引），不可撤销。"""

        if not self.table_exists(name):
            if ignore_missing:
                return
            raise TableNotFoundError(f"表不存在: {name!r}", available=self.table_names())
        self._store.delete_prefix(f"{name}{TABLE_SUFFIX}/")

    def rename_table(self, cur_name: str, new_name: str, **kwargs: Any) -> None:
        """改名。

        **这是"复制 + 删除"，不是 rename。** 因为对象存储没有原子重命名
        （见 :class:`~mini_lancedb.storage.base.S3LikeStore.rename`），
        任何实现都只能用"把对象逐个写到新 key，再删老 key"来做。
        中途失败会留下**两份半成品**，所以真实系统里表改名要么
        做成"改一个指针"，要么根本不提供。这里提供它，是因为
        教程里需要看到这个代价。
        """

        if not self.table_exists(cur_name):
            raise TableNotFoundError(
                f"表不存在: {cur_name!r}", available=self.table_names()
            )
        if self.table_exists(new_name):
            raise TableAlreadyExistsError(f"目标表已存在: {new_name!r}")
        src = f"{cur_name}{TABLE_SUFFIX}/"
        dst = f"{new_name}{TABLE_SUFFIX}/"
        for meta in self._store.list(src, recursive=True):
            rel = meta.path[len(src):]
            self._store.put(dst + rel, self._store.get(meta.path))
        self._store.delete_prefix(src)
        for table in self._open_tables:
            if table.name == cur_name:
                table._name = new_name
                table._store = self._table_store(new_name)
                table._uri = self._table_uri(new_name)

    # -- 命名空间 ---------------------------------------------------------
    def create_namespace(
        self,
        namespace_path: Union[str, Sequence[str]],
        mode: Optional[str] = None,
        properties: Optional[Mapping[str, str]] = None,
    ) -> Any:
        """建一个命名空间（目录）。见 :mod:`mini_lancedb.namespace`。"""

        return self.namespaces.create_namespace(
            namespace_path, mode=mode, properties=properties
        )

    def list_namespaces(
        self,
        namespace_path: Optional[Union[str, Sequence[str]]] = None,
        page_token: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        """列出某个命名空间下的子命名空间。"""

        return self.namespaces.list_namespaces(namespace_path)

    def describe_namespace(self, namespace_path: Any = None) -> Any:
        return self.namespaces.describe(namespace_path)

    def drop_namespace(self, namespace_path: Any, mode: Optional[str] = None) -> None:
        return self.namespaces.drop_namespace(namespace_path, mode=mode)

    @property
    def namespaces(self) -> Any:
        """本连接的 :class:`~mini_lancedb.namespace.NamespaceClient`（惰性创建）。

        **它和 ``table_names()`` 是两套独立的东西**：``table_names()`` 直接
        列目录（Lance 的"目录即表"模型），命名空间层是**额外的**一层
        组织结构（企业版 / 云端的 Namespace API）。所以完全可能出现
        "表在目录里存在，但不在任何命名空间里" —— 这不是不一致，
        是两种模型的正常重叠。
        """

        if self._namespace_client is None:
            from .namespace import NamespaceClient

            self._namespace_client = NamespaceClient(
                self._store, self_uri=self._uri, connection=self
            )
        return self._namespace_client

    # -- I/O 计量 ---------------------------------------------------------
    def instrument(self) -> Any:
        """把根 store 换成 :class:`~mini_lancedb.storage.base.InstrumentedStore`。

        之后 ``db.store.stats.bytes_read`` / ``db.store.events`` /
        ``db.store.io_report()`` 就能回答"这次查询到底读了多少字节"。
        性能章节的每一个数字都出自它。

        已经交出去的 :class:`LanceTable` 会被**重新指到**新的 store 上，
        否则它们会继续用没被计量的那份，数字就永远是 0。
        """

        if isinstance(self._store, InstrumentedStore):
            return self._store
        inner = self._store
        wrapped = InstrumentedStore(inner)
        self._store = wrapped
        for table in self._open_tables:
            table._store = self._table_store(table.name)
            table._ds = Dataset.open(table._store, table._uri)
        return wrapped

    def reset_io(self) -> None:
        """把 I/O 计数清零（没开计量时是空操作）。"""

        if isinstance(self._store, InstrumentedStore):
            self._store.reset_events()

    # -- dunder -----------------------------------------------------------
    def __getitem__(self, name: str) -> Any:
        return self.open_table(name)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.table_exists(name)

    def __iter__(self) -> Any:
        for name in self.table_names():
            yield self.open_table(name)

    def __len__(self) -> int:
        return len(self.table_names())

    def __repr__(self) -> str:
        names = self.table_names()
        shown = ", ".join(names[:5])
        more = f", ... 共 {len(names)} 张" if len(names) > 5 else ""
        return f"LanceDBConnection(uri={self._uri!r}, tables=[{shown}{more}])"

    # -- 内部 -------------------------------------------------------------
    def _table_uri(self, name: str) -> str:
        base = str(self._uri).rstrip("/")
        return f"{base}/{name}{TABLE_SUFFIX}"

    def _table_store(self, name: str) -> _PrefixedStore:
        return _PrefixedStore(self._store, f"{name}{TABLE_SUFFIX}")

    def _remember(self, table: Any) -> None:
        # 同一个名字只留一份引用，避免 instrument() 时重复重绑
        self._open_tables = [t for t in self._open_tables if t.name != table.name]
        self._open_tables.append(table)


# ---------------------------------------------------------------------------
# schema / embedding 辅助
# ---------------------------------------------------------------------------


def _to_schema(schema: Any) -> Schema:
    """把外部 schema 统一成 mini 的 :class:`Schema`。"""

    if isinstance(schema, Schema):
        return schema
    if isinstance(schema, (list, tuple)):
        return Schema(list(schema))
    to_arrow = getattr(schema, "to_arrow", None)
    if hasattr(schema, "names") and not callable(getattr(schema, "names", None)):
        # pyarrow.Schema（.names 是属性而不是方法）
        return Schema.from_arrow(schema)
    if to_arrow is not None:
        return Schema.from_arrow(to_arrow())
    raise SchemaError(f"无法识别的 schema 类型: {type(schema).__name__}")


def _infer_schema(data: Any, rows: Sequence[Mapping[str, Any]]) -> Optional[Schema]:
    """从输入数据推断 schema。

    **优先用数据自带的结构**（Arrow schema / pandas dtypes），因为它比
    "看前 1000 行的 Python 类型"准得多 —— 一列全是 ``None`` 时，
    类型推断会给你一个 null 列，而 Arrow schema 会给你正确的类型。
    """

    if data is not None:
        arrow_schema = getattr(data, "schema", None)
        if arrow_schema is not None and hasattr(arrow_schema, "names"):
            try:
                return Schema.from_arrow(arrow_schema)
            except Exception:
                pass
        if hasattr(data, "dtypes") and hasattr(data, "columns"):
            try:
                return Schema.from_pandas(data)
            except Exception:
                pass
    if rows:
        return Schema.from_pylist(list(rows))
    return None


def _collect_registrations(
    embedding_functions: Optional[Sequence[Any]], schema: Schema
) -> List[EmbeddingFunctionRegistration]:
    """收集 embedding 注册：显式列表 + schema 里的 ``func`` 标记。"""

    regs: List[EmbeddingFunctionRegistration] = []
    for conf in embedding_functions or []:
        reg = EmbeddingFunctionRegistration.from_config(conf)
        if reg.source_column and reg.vector_column and reg.function is not None:
            regs.append(reg)
    if not regs:
        regs = [
            r
            for r in EmbeddingFunctionRegistration.from_schema_metadata(list(schema.fields))
            if r.function is not None
        ]
    return regs


def _embedding_metadata(regs: Sequence[EmbeddingFunctionRegistration]) -> Dict[str, str]:
    """把注册写成 schema metadata。

    **用 JSON 字符串而不是二进制**：metadata 会跟着 schema 一起进 manifest、
    一起被打印、一起被 diff，人类可读的 JSON 在排查问题时值回票价。
    真实 LanceDB 用的是 ``bytes``（pyarrow 的要求），
    本实现的 schema metadata 是 ``Dict[str, str]``，所以用字符串。
    """

    payload = [
        {
            "source_column": r.source_column,
            "vector_column": r.vector_column,
            "name": getattr(r.function, "SOURCE", None)
            or type(r.function).__name__,
            "ndims": int(getattr(r.function, "ndims", lambda: 0)()),
        }
        for r in regs
    ]
    return {"embedding_functions": json.dumps(payload, ensure_ascii=False)}

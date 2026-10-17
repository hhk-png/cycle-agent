"""``LanceTable`` —— 用户天天打交道的那张表。

这一层**不做任何存储决策**，它只做三件事：

1. **把用户的输入规范化**（dict 列表 / Arrow 表 / DataFrame → 内部记录批次，
   顺便执行 ``on_bad_vectors`` 的策略）；
2. **把调用转给** :class:`~mini_lancedb.dataset.Dataset` **或**
   :mod:`mini_lancedb.query`；
3. **把结果包装成用户期望的形状**（Arrow 表 / DataFrame / list[dict]）。

这层之所以值钱，全在"规范化"上。比如 ``on_bad_vectors`` 这一个参数，
四种取值背后是**四种不同的失败哲学**：

.. list-table::
   :header-rows: 1

   * - 取值
     - 遇到坏向量（维度不对 / 非数值 / NaN）
     - 什么时候选它
   * - ``"error"``（默认）
     - 抛 :class:`~mini_lancedb.errors.DataValidationError`，整批不写
     - 生产默认。坏数据静默丢比报错危险得多
   * - ``"drop"``
     - 丢掉这些行，其余照写
     - 爬虫 / 日志这类"丢几行无所谓"的场景
   * - ``"fill"``
     - 用 ``fill_value`` 填满整个向量
     - 明确知道"缺失 = 零向量"且下游能接受
   * - ``"null"``
     - 存成 null
     - 想留着行、但**绝不让它被检索到**（null 向量在距离计算里是 NaN，
       会被 ``top_k`` 排到最后）

还有一个**极容易踩的坑**写在 :meth:`add` 的文档里：embedding 函数
**只在向量列整个缺失或整批全为 null 时**才会被触发。只要你给了一行
向量，整批就都被当成"手动提供的向量"，剩下那些 null 行就永远是 null。

**关于 ``_rowid`` 的一个诚实说明**：本实现里它是**逻辑行号**
（0..count_rows-1），而真实 LanceDB 用的是稳定的 64 位行地址
``(fragment_id << 32) | offset``。后者在压实 / 删除之后依然指向同一行，
前者会变。教程里用逻辑行号是因为它肉眼可读，但**不要把它当稳定主键用**。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np

from .dataset import (
    DeleteResult,
    Dataset,
    MergeInsertBuilder,
    UpdateResult,
    VersionInfo,
    WriteParams,
)
from .errors import (
    DataValidationError,
    NotSupportedError,
    PlanError,
    QueryError,
    SchemaError,
)
from .query import Query, create_query
from .record import RecordBatch
from .schema import Schema, merge_schemas

__all__ = ["LanceTable", "Table", "AddResult", "DropColumnsResult", "AlterColumnsResult",
           "AddColumnsResult", "TableTags"]

#: ``add()`` / ``create_table(mode=...)`` 认识的模式。
_ADD_MODES = ("append", "overwrite")
_CREATE_MODES = ("create", "overwrite")
_BAD_VECTOR_MODES = ("error", "drop", "fill", "null")


# ---------------------------------------------------------------------------
# 小结果对象
# ---------------------------------------------------------------------------


class _SimpleResult:
    """一个带 ``__repr__`` 的"结果回执"。

    真实 LanceDB 的 ``add()`` / ``add_columns()`` 返回的是一堆这种小对象
    （``AddResult`` / ``AddColumnsResult`` / ...），它们唯一的用处是
    能被打印出来、能点出字段。用 ``dict`` 也行，但 ``dict`` 打印出来
    太难看，而且点错字段不报错。
    """

    def __init__(self, **fields: Any) -> None:
        # 不用 __slots__：这些回执的字段名是调用方定的（``columns`` /
        # ``version`` / ``num_rows`` ...），用 slots 就得为每个子类
        # 写一遍字段表，收益为零。
        self._fields = fields
        for k, v in fields.items():
            setattr(self, k, v)

    def __repr__(self) -> str:  # pragma: no cover - 只为调试
        inner = ", ".join(f"{k}={v!r}" for k, v in self._fields.items())
        return f"{type(self).__name__}({inner})"


class AddResult(_SimpleResult):
    """``add()`` 的回执。"""


class AddColumnsResult(_SimpleResult):
    """``add_columns()`` 的回执。"""

    @property
    def _results(self) -> List[str]:
        return list(self.columns)


class AlterColumnsResult(_SimpleResult):
    """``alter_columns()`` 的回执。"""


class DropColumnsResult(_SimpleResult):
    """``drop_columns()`` 的回执。"""


# ---------------------------------------------------------------------------
# 标签
# ---------------------------------------------------------------------------


class TableTags:
    """``table.tags`` —— 标签（tag）的增删查改。

    标签在 Lance 格式里**只是一个指向版本号的小文件**
    （``_refs/tags/<name>.json``）。这个极简设计带来一个很有用的性质：
    **打标签不产生新版本、不复制任何数据**，所以"给每次发版打一个 tag"
    的成本接近零。

    真实 LanceDB 还有 ``branches``（可写的 ref），本实现只做 tags。
    """

    def __init__(self, table: "LanceTable") -> None:
        self._table = table

    @property
    def _ds(self) -> Dataset:
        return self._table._dataset

    def create(self, name: str, version: Optional[int] = None) -> Dict[str, Any]:
        """在 ``version``（默认当前版本）上打一个标签。"""

        if not name:
            raise QueryError("标签名不能为空")
        info = self._ds.create_tag(name, version=version)
        return info.to_json()

    def list(self) -> Dict[str, Dict[str, Any]]:
        """所有标签 → ``{name: {"version": int, ...}}``。"""

        return self._ds.list_tags()

    def delete(self, name: str) -> bool:
        return self._ds.delete_tag(name)

    def get_version(self, name: str) -> int:
        return self._ds.tag_version(name)

    def update(self, name: str, version: Optional[int] = None) -> Dict[str, Any]:
        """把标签挪到另一个版本。

        实现上是"删掉再建"。**这里有一个真实存在的竞态**：两步之间
        如果有读者正在按这个标签解析版本，它会看到"标签不存在"。
        真实 LanceDB 在对象存储上也做不到原子的 ref 更新（对象存储
        没有重命名），所以它同样有这个窗口 —— 只是窗口更小。
        """

        v = self._ds.version if version is None else int(version)
        self._ds.delete_tag(name)
        info = self._ds.create_tag(name, version=v)
        return info.to_json()

    def get(self, name: str) -> Dict[str, Any]:
        refs = self._ds.list_tags()
        if name not in refs:
            raise QueryError(f"标签不存在: {name!r}", available=sorted(refs))
        return refs[name]

    def __repr__(self) -> str:  # pragma: no cover
        tags = self.list()
        return f"<TableTags {len(tags)} 个: {', '.join(sorted(tags)) or '(空)'}>"


# ---------------------------------------------------------------------------
# 输入规范化
# ---------------------------------------------------------------------------

#: 真实 LanceDB 对"一次只给一个 dict"的报错原文。这里逐字复刻，
#: 因为教程里会拿它当作"输入形状不对"的范例。
_SINGLE_DICT_ERROR = (
    "Cannot create or add rows from a single dictionary. "
    "Use a list of dictionaries instead."
)


def _rows_from_data(data: Any) -> List[Dict[str, Any]]:
    """把各种输入统一成 ``list[dict]``。

    **刻意不接受以下几种输入**，而且报错信息和真实 LanceDB 一致：

    * 单个 ``dict`` —— 用户十有八九是忘了套一层 ``[...]``；
    * **生成器 / 任意迭代器** —— 这一条容易被忽视：LanceDB 要求
      ``list``/``tuple``/Arrow 表/DataFrame，因为**写入需要两遍数据**
      （一遍推断 schema，一遍落盘），而生成器只能消费一次。
      传生成器不会"碰巧能跑"，会直接报上面那句错。

    Args:
        data: 要写的数据。

    Returns:
        行字典的列表。
    """

    if data is None:
        return []
    if isinstance(data, Mapping):
        raise ValueError(_SINGLE_DICT_ERROR)
    if isinstance(data, (list, tuple)):
        rows = list(data)
        if rows and not isinstance(rows[0], Mapping):
            raise SchemaError(
                "行必须是 dict（或 dict 的映射）",
                got=type(rows[0]).__name__,
            )
        return [dict(r) for r in rows]
    # pyarrow.Table / pa.RecordBatch / 任何有 to_pylist 的
    if hasattr(data, "to_pylist") and hasattr(data, "schema"):
        out = data.to_pylist()
        return [dict(r) for r in out]
    # pandas.DataFrame
    if hasattr(data, "to_dict") and hasattr(data, "columns"):
        return [dict(r) for r in data.to_dict("records")]
    # 生成器、set、dict_keys…… 全部落到这里
    raise ValueError(_SINGLE_DICT_ERROR)


def _vector_dims(
    rows: Sequence[Mapping[str, Any]], schema: Optional[Schema]
) -> Dict[str, Optional[int]]:
    """确定每个向量列的**参考维度**。

    为什么需要这一步？因为"维度对不对"这个判断需要一个基准，而这个基准
    在两种情况下来源不同：

    * **表已存在** —— 基准是 schema 里那个 ``fixed_size_list<float32>[d]``，
      它是权威，d 以外的任何行都是坏数据；
    * **表还不存在**（第一次 ``create_table``）—— 基准只能从数据里来，
      取**第一个非 null 向量**的维度。

    第二种情况有一个必须点明的后果：如果先把 schema 从数据推断出来，
    而数据里混着 2 维和 3 维的向量，推断器只会得出一个"变长 list"，
    **维度信息就丢了**，之后再也无法判断哪一行是坏的。
    所以这里刻意在推断之前先抓一个参考维度，再让
    :func:`_force_vector_dims` 把 schema 钉回定长。
    """

    dims: Dict[str, Optional[int]] = {}
    if schema is not None:
        vec_cols = list(schema.vector_columns)
    else:
        # 还没有 schema：把"看起来像向量"的列（非空数值序列）挑出来
        vec_cols = []
        for row in rows:
            for name, value in row.items():
                if name in vec_cols or value is None or isinstance(value, (str, bytes)):
                    continue
                try:
                    arr = np.asarray(value, dtype=np.float64)
                except (TypeError, ValueError):
                    continue
                if arr.ndim == 1 and arr.size:
                    vec_cols.append(name)
    for col in vec_cols:
        dim = schema[col].vector_dim if (schema is not None and col in schema) else None
        if dim is None:
            for row in rows:
                value = row.get(col)
                if value is None:
                    continue
                try:
                    arr = np.asarray(value, dtype=np.float64)
                except (TypeError, ValueError):
                    continue
                if arr.ndim == 1 and arr.size:
                    dim = int(arr.size)
                    break
        dims[col] = dim
    return dims


def _force_vector_dims(schema: Schema, dims: Mapping[str, Optional[int]]) -> Schema:
    """把"变长 list"的向量列钉回定长。

    推断器在维度不一致时会退化成 ``list<float64>`` —— 那是一次
    **语义损失**：变长向量列在距离计算里毫无意义（两行之间算不了距离），
    而且从此再也判断不出"这一行维度对不对"。所以拿到参考维度后
    立刻把它钉死成 ``fixed_size_list``。
    """

    from .types import FLOAT32

    out = schema
    for col, dim in dims.items():
        if not dim or col not in out:
            continue
        field = out[col]
        if field.is_vector and field.vector_dim == int(dim):
            continue
        # 保留原来的浮点子类型（float64 别被静默降成 float32）
        child = getattr(field.type, "child", None)
        dtype = child if (child is not None and getattr(child, "is_floating", False)) else FLOAT32
        out = out.with_vector(col, int(dim), dtype=dtype)
    return out


def _validate_vectors(
    rows: Sequence[Mapping[str, Any]],
    vec_dims: Mapping[str, Optional[int]],
    on_bad_vectors: str,
    fill_value: float,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """执行 ``on_bad_vectors`` 的策略，返回 ``(干净的行, 报告)``。

    "坏向量"的三种定义，缺一不可：

    1. **维度不对** —— 最常见。模型换了但没重建表时就会出现，
       而且如果不管，写入会静默产生一个"有的行 768 维、有的 1536 维"
       的列，读的时候才炸；
    2. **不是数值** —— 比如把字符串塞进了向量列；
    3. **NaN / inf** —— 最阴险的一种。NaN 在距离比较里永远为
       ``False``，不显式处理的话它会**随机地**出现在结果里或消失。
       :func:`mini_lancedb.index.metric.top_k` 把它当 ``+inf`` 排到最后，
       但"写进去"本身就是错的。

    ``None`` **不算坏**：它是"这一行没有向量"，是合法状态
    （embedding 函数没跑、或者这一行本来就没内容）。
    """

    if on_bad_vectors not in _BAD_VECTOR_MODES:
        raise ValueError(
            f"on_bad_vectors 只能是 {list(_BAD_VECTOR_MODES)} 之一，"
            f"收到 {on_bad_vectors!r}"
        )
    vec_cols = list(vec_dims)
    if not vec_cols:
        return [dict(r) for r in rows], {"checked": 0, "bad": 0, "mode": on_bad_vectors}

    clean: List[Dict[str, Any]] = []
    bad_count = 0
    first_problem: Optional[str] = None
    for index, row in enumerate(rows):
        out = dict(row)
        row_bad = False
        for col in vec_cols:
            dim = vec_dims.get(col)
            value = out.get(col)
            if value is None:
                continue
            problem = _vector_problem(value, dim)
            if problem is None:
                continue
            row_bad = True
            if first_problem is None:
                first_problem = f"第 {index} 行的 {col!r}: {problem}"
            if on_bad_vectors == "error":
                raise DataValidationError(
                    "向量不合法，写入中止",
                    row=index,
                    column=col,
                    reason=problem,
                    hint="把 on_bad_vectors 改成 'drop' / 'fill' / 'null' 可以不放行整批",
                )
            if on_bad_vectors == "fill":
                out[col] = [float(fill_value)] * int(dim or 0)
            elif on_bad_vectors == "null":
                out[col] = None
            # "drop" 什么都不做，下面整行丢掉
        if row_bad and on_bad_vectors == "drop":
            bad_count += 1
            continue
        if row_bad:
            bad_count += 1
        clean.append(out)
    return clean, {
        "checked": len(rows),
        "bad": bad_count,
        "mode": on_bad_vectors,
        "first_problem": first_problem,
    }


def _infer_expression_type(probe: Any, sql: str) -> Any:
    """给 ``add_columns(computed=...)`` 的表达式推断输出类型。

    做法是"拿一行先算一遍"。这不是偷懒 —— 表达式求值器本身就知道结果
    是什么类型，硬编一张"哪种表达式→哪种类型"的映射表反而会在
    表达式求值器演进之后过期。
    """

    from .expr import evaluate, parse
    from .types import UTF8, infer_type

    if probe is None or probe.num_rows == 0:
        return "utf8"  # 空表：没有可推断的样本，update 也不会写任何东西
    try:
        expr = parse(str(sql), probe.schema)
        values = list(evaluate(expr, probe).to_pylist())
        return infer_type(values)
    except Exception:
        # 推不出来就退回 UTF8：它会**写成功**，但至少打印时一眼能看出不对
        return UTF8


def _vector_problem(value: Any, dim: Optional[int]) -> Optional[str]:
    """检查一个向量值，返回问题描述或 ``None``。"""

    if isinstance(value, (str, bytes)):
        return "向量不能是字符串/字节串"
    try:
        arr = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError):
        return f"含非数值元素: {type(value).__name__}"
    if arr.ndim != 1:
        return f"应该是 1 维，实际 {arr.ndim} 维"
    if dim is not None and arr.size != int(dim):
        return f"维度不符：期望 {int(dim)}，实际 {arr.size}"
    if arr.size == 0:
        return "向量为空"
    if not np.all(np.isfinite(arr)):
        bad = int((~np.isfinite(arr)).sum())
        return f"含 {bad} 个 NaN/inf（NaN 会让距离比较永远为 False）"
    return None


# ---------------------------------------------------------------------------
# LanceTable
# ---------------------------------------------------------------------------


class LanceTable:
    """一张 LanceDB 表。

    它**不是**数据本身：数据在不可变的 ``.lance`` 文件里，表对象只是
    "当前应该看哪个版本的 manifest" 这个指针 + 一堆便捷方法。
    所以它很轻，构造一个表对象几乎不产生 I/O。
    """

    def __init__(
        self,
        db: Any,
        name: str,
        uri: str,
        store: Any,
        dataset: Dataset,
        *,
        embedding_functions: Optional[Sequence[Any]] = None,
    ) -> None:
        self._db = db
        self._name = name
        self._uri = uri
        self._store = store
        self._ds = dataset
        self._checked_out: Optional[int] = None
        #: ``{vector_column: EmbeddingFunctionConfig}``
        self._embedding_regs: Dict[str, Any] = {}
        for conf in embedding_functions or []:
            self._register_embedding(conf)
        # 从 schema metadata 里恢复（进程重启后仍然认得）
        self._load_embedding_metadata()

    # -- 元信息 -----------------------------------------------------------
    @property
    def name(self) -> str:
        return self._name

    @property
    def uri(self) -> str:
        return self._uri

    @property
    def schema(self) -> Schema:
        return self._ds.schema

    @property
    def version(self) -> int:
        """当前看到的版本号（``checkout`` 之后就是被检出的那个）。"""

        return self._ds.version

    @property
    def _dataset(self) -> Dataset:
        """查询构建器通过这个属性拿数据。

        故意做成属性而不是字段：``checkout()`` / ``restore()`` 会换掉
        底层 Dataset，属性让"换掉"对上层透明。
        """

        return self._ds

    def to_lance(self, **kwargs: Any) -> Dataset:
        """返回底层的 :class:`~mini_lancedb.dataset.Dataset`。

        **与真实 LanceDB 的差异**：真实的 ``to_lance()`` 返回一个
        ``lance.LanceDataset``（能直接喂给 ``lance`` / ``pyarrow.dataset``
        的生态）。这里返回的是 mini 实现自己的 ``Dataset``，
        因为依赖真实的 ``lance`` 就不算"从零实现"了。
        """

        return self._ds

    def count_rows(self, filter: Optional[str] = None) -> int:
        """行数。带 ``filter`` 时只数命中的行。

        ``filter=None`` 时是**零 I/O** 的：行数在 manifest 里，
        打开表的时候已经读进来了。
        """

        if not filter or filter.strip() in ("", "true", "TRUE"):
            return self._ds.count_rows()
        from .scanner import Scanner

        return Scanner(self._ds, columns=[], filter=filter).count_rows()

    def head(self, n: int = 5) -> Any:
        """前 n 行，返回 ``pyarrow.Table``（和真实 LanceDB 一致）。"""

        return self._ds.head(n).to_arrow()

    def stats(self) -> Any:
        """表的统计信息，返回 :class:`~mini_lancedb.stats.TableStatistics`。"""

        from .stats import collect_table_statistics

        return collect_table_statistics(self)

    def list_versions(self) -> List[VersionInfo]:
        """所有版本，**按版本号升序**。

        顺序是升序而不是降序，因为教程里最常见的用法是
        "从 v1 看到 vN，看数据是怎么长出来的"。
        真实 LanceDB 返回的是 ``list[dict]``；这里返回
        :class:`~mini_lancedb.dataset.VersionInfo`（字段名一致，多了 ``__str__``）。
        """

        return self._ds.list_versions()

    def list_indices(self) -> List[Any]:
        """表上所有索引的元数据（``list[IndexMetadata]``）。"""

        return self._ds.list_indices()

    def index_stats(self, name: str) -> Optional[Dict[str, Any]]:
        """某个索引的统计信息；**索引不存在时返回 ``None``，不抛异常**。

        这个"返回 None 而不是报错"的行为是从真实 LanceDB 抄来的，
        而且必须抄 —— 如果这里抛异常，调用方就得先 ``list_indices()``
        再判断，多一次往返；返回 ``None`` 让"有没有索引"变成一个
        可以直接 ``if`` 的表达式。

        代价是**它和"索引存在但没建好"无法区分**（后者也返回 None）。
        需要区分时请自己查 ``list_indices()``。
        """

        meta = None
        for m in self._ds.indices:
            if m.name == name:
                meta = m
                break
        if meta is None:
            return None
        total = self.count_rows()
        return {
            "index_type": meta.index_type,
            "distance_type": meta.distance_type,
            "num_indexed_rows": int(meta.num_indexed_rows),
            "num_unindexed_rows": max(0, int(total) - int(meta.num_indexed_rows)),
            "num_indices": 1,
            "size_bytes": int(meta.size_bytes),
            "details": dict(meta.details),
        }

    @property
    def tags(self) -> TableTags:
        return TableTags(self)

    # -- 版本 -------------------------------------------------------------
    def checkout(self, version: Union[int, str]) -> None:
        """把这张表切到某个历史版本（时间旅行）。

        **零成本**：只读一个老的 manifest，数据文件一个都不用碰 ——
        这正是"数据文件不可变"这个设计的直接回报。
        和 git 一样，检出历史版本之后**写入仍然是在最新版本上做的**
        （见 :meth:`restore`）；本实现里检出之后表对象是只读视图。
        """

        self._ds = Dataset.open(self._store, self._uri, version=version)
        self._checked_out = self._ds.version

    def restore(self, version: Optional[Union[int, str]] = None) -> None:
        """把某个历史版本的内容**作为一个新版本**提交回来。

        注意这不是"把指针拨回去"：v7 的数据还在、v8 新增的数据也还在，
        只是 v9 的内容等于 v5。好处是**回滚本身也可回滚** ——
        回滚错了再 restore 回 v8 就行。

        不传 ``version`` 时用 :meth:`checkout` 检出的那个版本
        （这就是真实 LanceDB 的用法：``tbl.checkout(5)`` 然后 ``tbl.restore()``）。
        """

        if version is None:
            version = self._checked_out
        if version is None:
            raise QueryError("restore() 需要版本号，或者先 checkout(version)")
        latest = Dataset.open(self._store, self._uri)
        latest.restore(version)
        self._ds = latest
        self._checked_out = None

    # -- 维护 -------------------------------------------------------------
    def optimize(
        self,
        *,
        cleanup_older_than: Any = None,
        delete_unverified: bool = False,
        retrain: bool = False,
        **kw: Any,
    ) -> Any:
        """一次做完该做的维护，返回 :class:`~mini_lancedb.optimize.OptimizeStats`。"""

        from .optimize import optimize as _optimize

        return _optimize(
            self,
            cleanup_older_than=cleanup_older_than,
            delete_unverified=delete_unverified,
            retrain=retrain,
            **kw,
        )

    def cleanup_old_versions(
        self, older_than: Any = None, *, delete_unverified: bool = False, dry_run: bool = False
    ) -> Any:
        """回收旧版本占用的空间，返回 ``CleanupStats``。

        **危险的不是删除，是还在跑的读者。** 一个长事务可能正在读 v3，
        这时把 v3 的文件删掉，它就会读到半个表。所以默认策略是
        "``older_than`` 之内的一律不删"，用时间换安全。
        """

        from .optimize import cleanup_old_versions as _cleanup

        return _cleanup(
            self,
            older_than=older_than,
            delete_unverified=delete_unverified,
            dry_run=dry_run,
        )

    def compact_files(self, *args: Any, **kwargs: Any) -> Any:
        """压实小文件 / 落地删除，返回 ``CompactionStats``。"""

        from .optimize import compact_files as _compact

        return _compact(self, *args, **kwargs)

    # -- 索引 -------------------------------------------------------------
    def create_index(
        self,
        column: str,
        config: Any = None,
        name: Optional[str] = None,
        replace: bool = True,
        wait_timeout: Any = None,
        **kwargs: Any,
    ) -> Any:
        """建索引。

        ``config`` 接受三种形状（这是本实现为了可用性做的放宽，
        真实 LanceDB 只接受它自己的 config 对象）：

        * ``None`` —— 自动选：向量列 → ``IVF_PQ``，其它列 → ``BTREE``；
        * 字符串 —— 索引类型名，如 ``"FTS"`` / ``"IVF_PQ"`` / ``"HNSW"``；
        * dict 或对象 —— 取 ``index_type``，其余键当参数透传
          （``{"index_type": "IVF_PQ", "num_partitions": 4}``）。

        ``wait_timeout`` 被**接受但忽略**：真实 LanceDB 用它限制
        索引构建的超时（超时后索引在后台继续建），本实现是同步构建的，
        所以没有它的余地。保留参数是为了让真实 API 的代码能直接跑。
        """

        itype, params, metric = _resolve_index_config(column, config, kwargs, self.schema)
        return self._ds.create_index(
            column,
            index_type=itype,
            name=name,
            metric=metric,
            replace=replace,
            **params,
        )

    def drop_index(self, name: str) -> None:
        """删索引。注意它**只删索引**，数据一行不动。"""

        self._ds.drop_index(name)

    # -- 写入 -------------------------------------------------------------
    def add(
        self,
        data: Any,
        mode: str = "append",
        on_bad_vectors: str = "error",
        fill_value: float = 0.0,
        **kwargs: Any,
    ) -> AddResult:
        """追加 / 覆盖写入。

        ``mode`` 只有 ``"append"`` 和 ``"overwrite"``。**没有任何
        "upsert" 模式** —— 想 upsert 请用 :meth:`merge_insert`。
        这两个模式是**故意**不收窄的：一个"看起来像 upsert 其实是 append"
        的模式，会在生产里安静地把表写重。

        ⚠️ **embedding 函数的触发条件（真实 LanceDB 就有这个坑）**

        注册了 embedding 函数之后，它**只在目标向量列"整个缺失、
        或者整批全是 null"时才会被调用**。反之，只要你给了一行向量，
        整批就都被当作"手动提供的向量"，剩下那些 null 行**就永远是
        null，永远查不到**，而且不会有任何警告。

        判断"整批是否全空"用的是 ``all(row.get(col) is None)``：
        一行都没给向量 → 全部自动算；给了一半 → 一行都不算。
        这个"全有或全无"的语义容易让人以为"给一半也行"，
        所以测试里专门有一个用例把它钉死。

        Returns:
            :class:`AddResult`，带 ``num_rows`` / ``version`` / ``embedding``。
        """

        if mode not in _ADD_MODES:
            raise ValueError(f"Invalid mode: {mode}")

        rows = _rows_from_data(data)
        schema = self._ds.schema
        schema, embedding_status = self._maybe_embed(rows, schema)
        dims = _vector_dims(rows, schema)
        clean, report = _validate_vectors(rows, dims, on_bad_vectors, fill_value)
        if not clean:
            return AddResult(
                num_rows=0, version=self._ds.version, embedding=embedding_status,
                bad_vectors=report.get("bad", 0),
            )

        # 目标 schema = 表现有的 schema（权威）∪ 新数据推断出来的（支持加列）。
        # 两边都把向量列先钉成定长，否则 merge_schemas 会因为
        # "变长 list vs 定长 list" 而拒绝归并 —— 那个报错很有误导性。
        base = _force_vector_dims(schema, dims)
        inferred = _force_vector_dims(Schema.from_pylist(clean), dims)
        target = merge_schemas(base, inferred)
        # 但 **表已有的向量列必须以表为准**：新数据里万一维度不同，
        # 上面那一句会把列"改造"成新维度，等于静默改表结构。
        target = _force_vector_dims(target, {c: schema[c].vector_dim for c in schema.vector_columns})
        batch = RecordBatch.from_pylist(clean, target, strict=False)
        params = WriteParams(**kwargs) if kwargs else None
        version = self._ds.write([batch], mode=mode, params=params)
        return AddResult(
            num_rows=len(clean),
            version=version,
            embedding=embedding_status,
            bad_vectors=report.get("bad", 0),
        )

    def _register_embedding(self, conf: Any) -> None:
        """记住"哪一列用什么函数编码"。"""

        from .embedding.base import EmbeddingFunctionConfig

        if isinstance(conf, EmbeddingFunctionConfig):
            self._embedding_regs[conf.vector_column] = conf
            return
        if isinstance(conf, Mapping):
            self._embedding_regs[str(conf["vector_column"])] = conf
            return
        raise SchemaError(
            "embedding_functions 的元素必须是 EmbeddingFunctionConfig 或 dict",
            got=type(conf).__name__,
        )

    def _load_embedding_metadata(self) -> None:
        """从 schema metadata 里恢复 embedding 配置。

        真实 LanceDB 就是把这份配置**写在 schema 的 metadata 里**的
        （key ``embedding_functions``，值是一段 JSON）。放在 metadata
        而不是一个旁挂文件里的理由：**表的自描述性**。拿到 table 目录
        的任何工具（哪怕不是 LanceDB）都能知道"这一列的向量是哪来的"。
        """

        try:
            from .embedding.base import get_registry
        except Exception:  # pragma: no cover
            return
        meta = getattr(self._ds.schema, "metadata", None)
        if not meta:
            return
        try:
            parsed = get_registry().parse_functions(meta)
        except Exception:  # pragma: no cover - metadata 脏数据不该让开表失败
            return
        for key, conf in (parsed or {}).items():
            self._embedding_regs.setdefault(key, conf)

    def _embedding_function_for(self, column: str) -> Any:
        conf = self._embedding_regs.get(column)
        if conf is None:
            return None
        if isinstance(conf, Mapping):
            return None
        return conf.function

    def _maybe_embed(
        self, rows: List[Dict[str, Any]], schema: Schema
    ) -> Tuple[Schema, str]:
        """按"全有或全无"规则决定要不要跑 embedding 函数。

        Returns:
            ``(schema, 状态字符串)``。状态字符串会出现在 ``AddResult`` 里，
            这样"这次到底算了没有"是**可断言**的，而不是靠猜。
        """

        if not self._embedding_regs or not rows:
            return schema, "not_configured"
        status = "manual"
        for vector_column, conf in self._embedding_regs.items():
            source_column = getattr(conf, "source_column", None) or (
                conf.get("source_column") if isinstance(conf, Mapping) else None
            )
            function = getattr(conf, "function", None)
            if function is None or source_column is None:
                continue
            if not all(row.get(vector_column) is None for row in rows):
                # ⚠️ 只要有一行给了向量，整批都不自动算 —— 见 add() 的说明
                status = "skipped_partial" if any(
                    row.get(vector_column) is not None for row in rows
                ) else status
                continue
            vectors = function.compute_source_vectors(
                [row.get(source_column) for row in rows]
            )
            for row, vec in zip(rows, vectors):
                row[vector_column] = None if vec is None else list(map(float, vec))
            if vector_column not in schema:
                schema = schema.with_vector(vector_column, int(function.ndims()))
            status = "computed"
        return schema, status

    # -- 改数据 -----------------------------------------------------------
    def delete(self, where: str) -> DeleteResult:
        """按谓词删行。

        **删除是纯元数据操作**：不写任何数据，只在删除文件里记下"第几行没了"。
        代价是索引里会留下"幽灵行"、读放大随删除比例上升，
        所以删完（尤其删得多时）应该跑一次 :meth:`compact_files`。
        """

        return self._ds.delete(where)

    def update(
        self,
        where: Optional[str] = None,
        values: Optional[Dict[str, Any]] = None,
        *,
        values_sql: Optional[Dict[str, str]] = None,
    ) -> UpdateResult:
        """更新匹配的行。

        **语义是"删掉 + 追加"，不是就地改**。于是有一个必须知道的副作用：

        ⚠️ **被更新的行会跑到表末尾。**

        如果你用 ``limit`` + ``offset`` 做分页，翻页时就会漏行
        （第一页走了，第二页的 offset 已经错位）。真实 LanceDB 的文档
        专门警告过这一点。分页请用"按稳定键排序 + 游标"，不要用 offset。
        """

        return self._ds.update(values, where=where, values_sql=values_sql)

    def merge_insert(self, on: Union[str, Sequence[str]]) -> MergeInsertBuilder:
        """upsert 构建器。

        整个 merge 在**一次提交**里完成（删除文件 + 新片段一起点亮）。
        这一点是硬要求：分成"先删再写"两次提交，中途失败就会留下
        "旧行删了、新行没写"的空洞 —— 在 upsert 场景下等价于丢数据。
        """

        return self._ds.merge_insert(on)

    # -- schema 演进 ------------------------------------------------------
    def add_columns(
        self,
        new_columns: Optional[Mapping[str, Any]] = None,
        *,
        computed: Optional[Mapping[str, str]] = None,
        **kwargs: Any,
    ) -> AddColumnsResult:
        """加列。

        * ``new_columns={"tag": "utf8"}`` —— **只改 manifest，不碰任何数据**
          文件。老片段里没有这一列，读的时候补 null。所以哪怕表里有
          十亿行，这个操作也是毫秒级的。
        * ``computed={"full": "first || ' ' || last"}`` —— 需要**每一行
          都有值**，所以必须重写：本实现的做法是"先加空列，再
          ``update(where='true')``"。这个代价是 O(行数)，而且会打乱行序。
          真实 LanceDB 走的是 Lance 的 ``add_columns`` 物理算子，
          只重写一次、不产生删除文件。
        """

        new_columns = dict(new_columns or {})
        for name, spec in dict(kwargs).items():
            new_columns[name] = spec
        if computed:
            # 计算列的**类型不能猜**。先拿一行试算一遍，用真实的求值结果
            # 去推断 dtype —— 否则 `id * 2` 会被当字符串写进去（值是
            # ``"0"`` 而不是 ``0``），而且两种写法在打印时都"看起来对"。
            probe = self._ds.scanner(limit=1).to_batch()
            for name, sql in computed.items():
                new_columns.setdefault(name, _infer_expression_type(probe, sql))
        if not new_columns:
            raise QueryError("add_columns() 至少要给一列")

        version = self._ds.add_columns(new_columns)
        done: List[str] = list(new_columns)
        if computed:
            # 加完空列之后，用 values_sql 把值填进去
            self._ds.update(None, where="true", values_sql=dict(computed))
            version = self._ds.version
            done.extend(computed)
        return AddColumnsResult(columns=sorted(set(done)), version=version)

    def drop_columns(self, columns: Iterable[str]) -> DropColumnsResult:
        """删列。

        **数据文件原样保留。** 这就是"字段必须有稳定 id"的直接回报：
        如果按位置编号，删掉中间一列之后，后面所有列的数据都会读错位。
        """

        names = list(columns)
        version = self._ds.drop_columns(names)
        return DropColumnsResult(columns=names, version=version)

    def alter_columns(self, *alterations: Any, **kwargs: Any) -> AlterColumnsResult:
        """改列（类型 / 名字 / nullable）。

        接受两种写法：

        * 真实 Lance 风格：``alter_columns({"path": "id", "rename": "pk"})``、
          ``alter_columns({"path": "id", "data_type": "int64"})``；
        * 简写：``alter_columns({"id": "int64"})``、``alter_columns(id="int64")``。

        **改类型是这里唯一昂贵的演进**：它必须重写该列的数据。
        真实 Lance 会先判断新旧类型是否"二进制兼容"
        （int32 → int64 可以零拷贝重新解释，int32 → string 不行），
        兼容就只改元数据。本实现一律重写，代价更高但结果一样。
        """

        spec: Dict[str, Any] = {}
        for item in alterations:
            if isinstance(item, Mapping):
                if "path" in item:
                    name = str(item["path"])
                    entry: Dict[str, Any] = {}
                    if "rename" in item:
                        entry["name"] = str(item["rename"])
                    if "data_type" in item:
                        entry["type"] = item["data_type"]
                    if "nullable" in item:
                        entry["nullable"] = bool(item["nullable"])
                    spec[name] = entry or item
                else:
                    for k, v in item.items():
                        spec[str(k)] = v
            else:
                raise PlanError(
                    "alter_columns 的参数必须是 dict",
                    got=type(item).__name__,
                )
        for k, v in kwargs.items():
            spec[k] = v
        if not spec:
            raise QueryError("alter_columns() 至少要给一列")
        version = self._ds.alter_columns(spec)
        return AlterColumnsResult(columns=sorted(spec), version=version)

    # -- 表级操作 ---------------------------------------------------------
    def rename_table(self, new_name: str) -> None:
        """改名。**这是目录级的重命名**，不是改一个元数据字段。"""

        if self._db is None:
            raise NotSupportedError("独立打开的 LanceTable 不支持 rename_table")
        self._db.rename_table(self._name, new_name)
        self._name = new_name
        self._uri = f"{self._db.uri.rstrip('/')}/{new_name}.lance"

    def drop(self) -> None:
        """删表（删整个目录）。**不可撤销** —— 回收空间请用 ``cleanup_old_versions``。"""

        if self._db is None:
            raise NotSupportedError("独立打开的 LanceTable 不支持 drop")
        self._db.drop_table(self._name)

    def rollback_to(self, version: Union[int, str]) -> None:
        """便捷方法：``restore(version)`` 的别名，语义更直白。"""

        self.restore(version)

    # -- 查询 -------------------------------------------------------------
    def search(
        self,
        query: Any = None,
        vector_column_name: Optional[str] = None,
        query_type: str = "vector",
        fts_columns: Optional[Union[str, Sequence[str]]] = None,
        **kwargs: Any,
    ) -> Query:
        """造一个查询。

        ``query_type`` 与真实 LanceDB 的差异：**真实 LanceDB 的默认是
        ``"auto"``**（按 ``query`` 的类型猜：字符串走 FTS、数组走向量），
        本实现按题目约定默认 ``"vector"``。``"auto"`` 也支持，直接传即可。

        ``query=None`` 时得到的是一个**普通扫描查询**（对应真实 LanceDB
        的 ``LanceEmptyQueryBuilder``）：``tbl.search().where("id > 3").to_list()``。

        ``**kwargs`` 里认得 ``limit`` / ``offset`` / ``where`` / ``select`` /
        ``metric`` / ``nprobes`` / ``prefilter``，直接作用在返回的构建器上。
        """

        q = create_query(
            self,
            query,
            query_type,
            vector_column_name=vector_column_name,
            fts_columns=fts_columns,
        )
        if "limit" in kwargs:
            q.limit(kwargs.pop("limit"))
        if "offset" in kwargs:
            q.offset(kwargs.pop("offset"))
        if "select" in kwargs:
            q.select(kwargs.pop("select"))
        if "where" in kwargs:
            q.where(kwargs.pop("where"), prefilter=kwargs.pop("prefilter", True))
        if "prefilter" in kwargs:
            q._prefilter = bool(kwargs.pop("prefilter"))
        if "metric" in kwargs:
            q.distance_type(kwargs.pop("metric"))
        if "nprobes" in kwargs:
            q.nprobes(kwargs.pop("nprobes"))
        if "refine_factor" in kwargs:
            q.refine_factor(kwargs.pop("refine_factor"))
        if kwargs:
            raise QueryError(
                "search() 收到不认识的参数",
                unexpected=sorted(kwargs),
            )
        return q

    # -- 导出 -------------------------------------------------------------
    def to_arrow(self) -> Any:
        """整表 → ``pyarrow.Table``（**不带 limit**，全都要）。"""

        return self._ds.to_arrow()

    def to_pandas(self, **kwargs: Any) -> Any:
        return self._ds.to_arrow().to_pandas()

    def to_polars(self, batch_size: Any = None) -> Any:
        """整表 → ``polars.DataFrame``。

        **polars 没装就报 ImportError 并给出安装命令** —— 不静默退回
        pandas。理由：调用方写了 ``to_polars()`` 就是明确要 polars 的性能
        特性（惰性、零拷贝），给它一个 pandas 对象是最坏的结果 ——
        代码能跑，但在数据量大时慢十倍，而且没人知道为什么。
        """

        try:
            import polars as pl  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "to_polars() 需要 polars；安装：pip install polars"
            ) from exc
        return pl.from_arrow(self._ds.to_arrow())

    def to_list(self) -> List[Dict[str, Any]]:
        return self._ds.to_pylist()

    # -- 构造 -------------------------------------------------------------
    @classmethod
    def create(
        cls,
        db: Any,
        name: str,
        data: Any = None,
        schema: Any = None,
        mode: str = "create",
        exist_ok: bool = False,
        on_bad_vectors: str = "error",
        fill_value: float = 0.0,
        embedding_functions: Optional[Sequence[Any]] = None,
        **kwargs: Any,
    ) -> "LanceTable":
        """建表（见 :meth:`LanceDBConnection.create_table`）。"""

        if db is None:
            raise NotSupportedError("Table.create() 需要一个连接对象")
        return db.create_table(
            name,
            data=data,
            schema=schema,
            mode=mode,
            exist_ok=exist_ok,
            on_bad_vectors=on_bad_vectors,
            fill_value=fill_value,
            embedding_functions=embedding_functions,
            **kwargs,
        )

    @classmethod
    def open(cls, db: Any, name: str, **kwargs: Any) -> "LanceTable":
        """打开已有的表（见 :meth:`LanceDBConnection.open_table`）。"""

        if db is None:
            raise NotSupportedError("Table.open() 需要一个连接对象")
        return db.open_table(name, **kwargs)

    def __repr__(self) -> str:
        cols = ", ".join(self._ds.schema.names)
        return (
            f"LanceTable(name={self._name!r}, version={self.version}, "
            f"rows={self._ds.num_rows}, columns=[{cols}])"
        )

    def __len__(self) -> int:  # pragma: no cover - 便利
        return self._ds.num_rows


#: 真实 LanceDB 里这张表的名字就叫 ``Table``。
Table = LanceTable


# ---------------------------------------------------------------------------
# 索引配置解析
# ---------------------------------------------------------------------------


def _resolve_index_config(
    column: str, config: Any, kwargs: Dict[str, Any], schema: Schema
) -> Tuple[str, Dict[str, Any], str]:
    """把 ``config`` 归一成 ``(index_type, params, metric)``。

    这里比真实 LanceDB 宽松得多（它只认自己的 config 对象）。
    宽松是刻意的：教程里要能一眼看出"建 FTS 就是给个 'FTS'"，
    而不是先 import 五个类。
    """

    from .index.base import normalize_index_type

    kwargs = dict(kwargs)
    params: Dict[str, Any] = {}
    metric = "l2"
    itype: Optional[str] = None

    if config is None:
        itype = kwargs.pop("index_type", None)
        metric = str(kwargs.pop("metric", kwargs.pop("distance_type", "l2")))
        params.update(kwargs)
    elif isinstance(config, str):
        itype = config
        metric = str(kwargs.pop("metric", kwargs.pop("distance_type", "l2")))
        params.update(kwargs)
    elif isinstance(config, Mapping):
        d = dict(config)
        itype = d.pop("index_type", d.pop("type", None))
        metric = str(d.pop("metric", d.pop("distance_type", "l2")))
        params.update(d)
        params.update(kwargs)
    else:
        # 对象：真实 LanceDB 的 IvfPq(...) / FTS(...) / BTree(...)
        itype = getattr(config, "index_type", None)
        if itype is None:
            itype = type(config).__name__
        dumped: Dict[str, Any] = {}
        for attr in ("model_dump", "to_dict", "dict"):
            fn = getattr(config, attr, None)
            if callable(fn):
                try:
                    dumped = dict(fn())
                    break
                except Exception:  # pragma: no cover
                    continue
        if not dumped:
            dumped = {
                k: v for k, v in vars(config).items() if not k.startswith("_")
            }
        metric = str(dumped.pop("distance_type", dumped.pop("metric", "l2")))
        params.update({k: v for k, v in dumped.items() if v is not None})
        params.update(kwargs)

    if itype is None:
        # 自动选：向量列默认 IVF_PQ，其它列默认 BTREE
        is_vec = column in schema and schema[column].is_vector
        itype = "IVF_PQ" if is_vec else "BTREE"

    itype = normalize_index_type(itype)
    params = {k: v for k, v in params.items() if v is not None}
    return itype, params, metric

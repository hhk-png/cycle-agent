"""Dataset —— 数据集层，LanceDB 的"表"在存储侧的真身。

一个 dataset 目录的完整形态::

    mytable.lance/
      _versions/
        18446744073709551615.manifest      ← 版本 0 不存在；v1 从 ...614 开始
        18446744073709551614.manifest
        latest_version_hint.json           ← {"version": 2}
      _transactions/
        1-8f3c....txn                      ← "我基于 v1 做了 append"
      _refs/tags/v1.0.json                 ← 标签只是一个指向版本号的小文件
      data/
        3f9a....lance                      ← 不可变的数据文件
      _deletions/
        7-2-9c1e....bin                    ← 删除文件
      _indices/
        vector_idx/index.idx               ← 向量索引
        vector_idx/auxiliary.idx
        fts_idx/meta.lance、invert.lance、... ← 倒排索引
        btree_idx/page_data.lance、page_lookup.lance

本模块实现 dataset 的全部写路径与版本管理，读路径主要交给
:mod:`mini_lancedb.scanner`（它负责把"用户查询"变成"读哪些片段的哪些列"）。

**三个贯穿全篇的设计原则**

1. **数据文件永不修改。** 删除走删除文件，更新走"删+追加"，
   压实走"写新片段 + 在 manifest 里换掉旧的"。于是任何历史版本
   都能完整重放，任何并发读者都不会看到写了一半的状态。
2. **一切变更都是"复制 manifest 改一处"。** 没有增量文件、
   没有就地修改；manifest 本身就是一份完整的快照。
3. **行号是 (片段 id, 片段内偏移)。** 不依赖任何全局可变状态，
   所以压实之后"老行号 → 新行号"的映射是一次纯函数计算。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field as dc_field
from typing import (
    Any,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

import numpy as np

from .arrays import Array, PrimitiveArray, array_from_pylist, concat_arrays, empty_array
from .encoding.compress import COMPRESSION_NONE
from .errors import (
    CommitConflict,
    IndexNotFoundError,
    MiniLanceError,
    NotSupportedError,
    PlanError,
    SchemaError,
    TableAlreadyExistsError,
    TableNotFoundError,
    TypeMismatchError,
    VersionNotFoundError,
)
from .format.deletion import (
    DeletionFile,
    deletion_mask,
    merge_deletions,
    read_deletion_offsets,
    write_deletion_file,
)
from .format.lance_file import LanceFileReader, LanceFileWriter, write_lance_file
from .format.manifest import (
    DATA_DIR,
    INDEX_DIR,
    Manifest,
    manifest_path,
    read_manifest,
    try_read_latest,
    write_latest_hint,
    write_manifest_if_absent,
    list_manifest_versions,
    tag_path,
    list_refs,
    resolve_ref,
)
from .format.transaction import Operation, Transaction, write_transaction
from .fragment import DataFile, Fragment, new_fragment_id
from .index.base import IndexMetadata, ObjectStoreIndexStore
from .record import (
    DEFAULT_MAX_ROWS_PER_FILE,
    DEFAULT_WRITE_BATCH_SIZE,
    RecordBatch,
)
from .schema import Field, Schema, merge_schemas
from .storage.base import LocalFileStore, ObjectStore
from .types import ROW_ID_TYPE, UINT64, DataType

__all__ = [
    "Dataset",
    "WriteParams",
    "DeleteResult",
    "UpdateResult",
    "MergeResult",
    "VersionInfo",
    "TagInfo",
    "open_dataset",
    "create_dataset",
    "clear_index_cache",
]

#: 一个片段建议至少多少行 —— 低于它就值得压实。
MIN_ROWS_PER_FRAGMENT = 1024
#: 压实的目标片段大小。
DEFAULT_TARGET_ROWS_PER_FRAGMENT = 1024 * 1024


#: 进程内的索引缓存。key 里带版本号和文件清单，所以版本一变自动失效。
#: 真实 Lance 的等价物叫 index cache，它也带来过一个真实的资源泄漏 bug
#: （optimize() 每次调用泄漏一个文件描述符），所以缓存不是免费的午餐。
_INDEX_CACHE: Dict[Any, Any] = {}


def clear_index_cache() -> int:
    """清空索引缓存，返回清掉的条目数。测试和"我想量冷启动成本"时用得上。"""

    n = len(_INDEX_CACHE)
    _INDEX_CACHE.clear()
    return n


def _drop_index_cache(uri: str, name: str) -> None:
    """踢掉某个索引的所有缓存条目。"""

    for key in [k for k in _INDEX_CACHE if k[0] == uri and k[1] == name]:
        _INDEX_CACHE.pop(key, None)


@dataclass
class WriteParams:
    """写入参数。名字尽量和真实 LanceDB 的 ``WriteParams`` 对齐。"""

    max_rows_per_file: int = DEFAULT_MAX_ROWS_PER_FILE
    max_rows_per_group: int = 1024 * 1024
    max_bytes_per_file: int = 90 * 1024**3
    target_values_per_page: int = 8192
    compression: str = COMPRESSION_NONE
    #: 每次 ``add`` 攒够多少行写一个片段
    batch_size: int = DEFAULT_WRITE_BATCH_SIZE

    def describe(self) -> str:
        return (
            f"max_rows_per_file={self.max_rows_per_file}, "
            f"max_bytes_per_file={self.max_bytes_per_file}, "
            f"values_per_page={self.target_values_per_page}, "
            f"compression={self.compression}"
        )


@dataclass
class VersionInfo:
    version: int
    timestamp: float
    operation: str = ""
    num_rows: int = 0
    num_fragments: int = 0
    num_deleted_rows: int = 0
    transaction_id: str = ""

    def __str__(self) -> str:  # pragma: no cover
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.timestamp))
        return (
            f"v{self.version:<4} {ts}  {self.operation or '-':<14} "
            f"rows={self.num_rows:<8} fragments={self.num_fragments:<4} "
            f"deleted={self.num_deleted_rows}"
        )


@dataclass
class DeleteResult:
    num_deleted_rows: int
    version: int

    def __repr__(self) -> str:  # pragma: no cover
        return f"DeleteResult(num_deleted_rows={self.num_deleted_rows}, version={self.version})"


@dataclass
class UpdateResult:
    rows_updated: int
    version: int

    def __repr__(self) -> str:  # pragma: no cover
        return f"UpdateResult(rows_updated={self.rows_updated}, version={self.version})"


@dataclass
class MergeResult:
    version: int
    num_updated_rows: int = 0
    num_inserted_rows: int = 0
    num_deleted_rows: int = 0
    num_attempts: int = 1
    num_rows: int = 0

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"MergeResult(version={self.version}, updated={self.num_updated_rows}, "
            f"inserted={self.num_inserted_rows}, deleted={self.num_deleted_rows})"
        )


@dataclass
class TagInfo:
    version: int
    manifest_size: int = 0
    created_at: float = dc_field(default_factory=time.time)

    def to_json(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "manifest_size": self.manifest_size,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------


class Dataset:
    """一个 Lance 数据集（= LanceDB 的表）。"""

    def __init__(self, store: ObjectStore, uri: str, manifest: Manifest) -> None:
        self.store = store
        self.uri = uri
        self.manifest = manifest
        #: 行映射缓存（逻辑行 → 片段/物理偏移），首次需要时构建
        self._row_map_cache: Optional[Tuple[np.ndarray, np.ndarray]] = None

    # -- 打开 / 创建 ------------------------------------------------------
    @staticmethod
    def open(
        store: ObjectStore, uri: str, *, version: Optional[Union[int, str]] = None
    ) -> "Dataset":
        if version is None:
            manifest = try_read_latest(store)
            if manifest is None:
                raise TableNotFoundError("数据集不存在", uri=uri)
        else:
            v = version
            if isinstance(v, str) and not v.isdigit():
                resolved = resolve_ref(store, v)
                if resolved is None:
                    raise VersionNotFoundError("找不到该标签或分支", ref=v, uri=uri)
                v = resolved
            manifest = read_manifest(store, int(v), dataset_uri=uri)
        return Dataset(store, uri, manifest)

    @staticmethod
    def create(
        store: ObjectStore,
        uri: str,
        schema: Schema,
        *,
        mode: str = "create",
        writer: str = "mini-lancedb",
    ) -> "Dataset":
        existing = try_read_latest(store)
        if existing is not None and mode == "create":
            raise TableAlreadyExistsError(
                "数据集已存在", uri=uri, hint="用 mode='overwrite' 覆盖，或先 drop_table"
            )
        if mode == "create" and existing is None:
            manifest = Manifest(version=1, schema=schema, fragments=[], parent_version=None)
        else:
            # overwrite 也走"提交一个新版本"，而不是删掉重来 ——
            # 这样旧版本仍然可读，回滚永远可行。
            base = existing if existing is not None else Manifest(version=0, schema=schema)
            manifest = base.clone(
                version=(existing.version + 1) if existing else 1, schema=schema, fragments=[]
            )
        manifest.writer = writer
        if not write_manifest_if_absent(store, manifest):
            raise CommitConflict("创建数据集时检测到并发写", uri=uri, version=manifest.version)
        write_latest_hint(store, manifest.version)
        return Dataset(store, uri, manifest)

    # -- 元信息 -----------------------------------------------------------
    @property
    def version(self) -> int:
        return self.manifest.version

    @property
    def schema(self) -> Schema:
        return self.manifest.schema

    @property
    def fragments(self) -> List[Fragment]:
        return self.manifest.fragments

    @property
    def indices(self) -> List[IndexMetadata]:
        return self.manifest.indices

    @property
    def num_rows(self) -> int:
        return self.manifest.num_rows

    @property
    def physical_rows(self) -> int:
        return self.manifest.physical_rows

    @property
    def num_deleted_rows(self) -> int:
        return self.manifest.num_deleted_rows

    def count_rows(self) -> int:
        return self.num_rows

    def vector_columns(self) -> List[str]:
        return self.schema.vector_columns

    def __repr__(self) -> str:  # pragma: no cover
        return f"Dataset({self.uri!r}, v{self.version}, {self.num_rows} 行)"

    # -- 行映射 -----------------------------------------------------------
    def _row_map(self) -> Tuple[np.ndarray, np.ndarray]:
        """逻辑行号 → ``(片段下标, 物理偏移)`` 两个数组。

        **逻辑行号**是用户看到的序号（0..count_rows-1），**物理偏移**
        是行在数据文件里的位置（删除不会改变它）。

        真实 Lance 不建这张表：它直接把行号编码成
        ``(fragment_id << 32) | offset`` 的 64 位地址，任何"第 n 行"
        都是纯算术。这里为了可读性建了显式映射，代价是 O(行数) 内存
        —— 在教程里这是可接受的，在生产里不可接受，注释里点明。
        """

        if self._row_map_cache is not None:
            return self._row_map_cache
        frag_idx: List[np.ndarray] = []
        offsets: List[np.ndarray] = []
        for i, frag in enumerate(self.fragments):
            deleted = read_deletion_offsets(self.store, frag.deletion_file, frag.physical_rows)
            alive = deletion_mask(deleted, frag.physical_rows)
            offs = np.flatnonzero(alive)
            frag_idx.append(np.full(len(offs), i, dtype=np.int64))
            offsets.append(offs)
        if frag_idx:
            self._row_map_cache = (
                np.concatenate(frag_idx) if len(frag_idx) > 1 else frag_idx[0],
                np.concatenate(offsets) if len(offsets) > 1 else offsets[0],
            )
        else:
            self._row_map_cache = (
                np.zeros(0, dtype=np.int64),
                np.zeros(0, dtype=np.int64),
            )
        return self._row_map_cache

    def row_address(self, logical_row: int) -> int:
        """逻辑行号 → Lance 风格的 64 位行地址 ``(fragment_id << 32) | offset``。"""

        fi, off = self._row_map()
        if logical_row < 0 or logical_row >= len(fi):
            raise IndexError(f"行号 {logical_row} 越界（共 {len(fi)} 行）")
        frag = self.fragments[int(fi[logical_row])]
        return (frag.id << 32) | int(off[logical_row])

    # -- 读片段 -----------------------------------------------------------
    def read_fragment(
        self,
        fragment: Fragment,
        columns: Optional[Sequence[str]] = None,
        row_range: Optional[Tuple[int, int]] = None,
        *,
        include_deleted: bool = False,
        with_address: bool = False,
    ) -> RecordBatch:
        """读一个片段。

        这里体现了"按列组拆文件"的价值：请求 3 列时，只有装这 3 列的
        文件会被打开；如果它们分散在两个文件里（因为曾经加过列），
        就分别读、再按行拼起来，缺失的列补 null **而不需要重写老文件**。
        """

        cols = list(columns) if columns else self.schema.names
        for c in cols:
            if c not in self.schema:
                raise PlanError(f"列不存在: {c!r}", available=self.schema.names)

        field_ids = {f.name: (f.field_id or 0) for f in self.schema.fields}
        groups: Dict[str, List[str]] = {}
        for name in cols:
            df = fragment.file_for_field(field_ids[name])
            if df is None:
                continue  # 这个片段里没有这一列 → 后面补 null
            groups.setdefault(df.path, []).append(name)

        physical_rows = (
            fragment.physical_rows if row_range is None else max(0, row_range[1] - row_range[0])
        )
        out: Dict[str, Array] = {}
        for path, names in groups.items():
            reader = LanceFileReader(self.store, path)
            batch = reader.read(columns=names, row_range=row_range)
            for n in names:
                if n in batch:
                    out[n] = batch.column(n)

        missing = [c for c in cols if c not in out]
        if missing:
            # 老片段里没有的新列 → 全部是 null（这就是"免重写加列"）
            for name in missing:
                out[name] = empty_array(self.schema[name].type)
                out[name] = array_from_pylist(self.schema[name].type, [None] * physical_rows)

        # 有效性位图在文件里只覆盖"有数据的行"，补出来的 null 列长度必须对齐
        for name in cols:
            if len(out[name]) != physical_rows:
                out[name] = _pad_to_length(out[name], physical_rows)

        if not include_deleted and fragment.deletion_file is not None:
            deleted = read_deletion_offsets(
                self.store, fragment.deletion_file, fragment.physical_rows
            )
            base = 0 if row_range is None else row_range[0]
            local_deleted = deleted - base
            local_deleted = local_deleted[
                (local_deleted >= 0) & (local_deleted < physical_rows)
            ]
            if len(local_deleted):
                keep = np.ones(physical_rows, dtype=bool)
                keep[local_deleted.astype(np.int64)] = False
                out = {k: v.filter(keep) for k, v in out.items()}

        schema = Schema([self.schema[c] for c in cols])
        batch = RecordBatch(schema, out)
        if with_address:
            batch = self._attach_address(batch, fragment, row_range, include_deleted)
        return batch

    def _attach_address(
        self,
        batch: RecordBatch,
        fragment: Fragment,
        row_range: Optional[Tuple[int, int]],
        include_deleted: bool,
    ) -> RecordBatch:
        base = 0 if row_range is None else row_range[0]
        offs = np.arange(base, base + (fragment.physical_rows if row_range is None else row_range[1] - row_range[0]))
        if not include_deleted and fragment.deletion_file is not None:
            deleted = read_deletion_offsets(
                self.store, fragment.deletion_file, fragment.physical_rows
            )
            offs = np.setdiff1d(offs, deleted, assume_unique=False)
        addrs = np.array([(fragment.id << 32) | int(o) for o in offs], dtype=np.uint64)
        return batch.with_column("_rowaddr", PrimitiveArray(UINT64, addrs))

    def take_rows(
        self,
        logical_rows: Sequence[int],
        columns: Optional[Sequence[str]] = None,
        *,
        with_address: bool = False,
    ) -> RecordBatch:
        """按**逻辑行号**取一批行 —— ANN 检索"回表"走的就是这条路。

        实现要点：按片段分组，每组建一个"片段内物理偏移"数组，
        然后对该片段的文件做一次 ``take``。**绝不能逐行读**，
        那会把一次查询变成几千次 I/O。
        """

        cols = list(columns) if columns else self.schema.names
        fi, offs = self._row_map()
        rows = np.asarray(logical_rows, dtype=np.int64)
        if len(rows) == 0:
            return RecordBatch.empty(Schema([self.schema[c] for c in cols]))
        bad = rows[(rows < 0) | (rows >= len(fi))]
        if len(bad):
            raise IndexError(f"行号越界: {int(bad[0])}（共 {len(fi)} 行）")

        frag_of = fi[rows]
        phys = offs[rows]
        pieces: Dict[int, List[int]] = {}
        order: List[Tuple[int, int]] = []  # (片段下标, 该片段内第几个)
        for k, (f, p) in enumerate(zip(frag_of, phys)):
            pieces.setdefault(int(f), []).append(int(p))
            order.append((int(f), len(pieces[int(f)]) - 1))

        collected: Dict[int, RecordBatch] = {}
        for f_idx, phys_list in pieces.items():
            frag = self.fragments[f_idx]
            batch = self.read_fragment(
                frag,
                columns=cols,
                include_deleted=True,
                with_address=with_address,
            )
            collected[f_idx] = batch.take(np.asarray(phys_list, dtype=np.int64))

        # 按用户给的行序重排
        prop: Dict[int, int] = {}
        out_batches = []
        for f_idx, k in order:
            b = collected[f_idx].slice(k, 1)
            out_batches.append(b)
        return RecordBatch.concat(out_batches) if out_batches else RecordBatch.empty(
            Schema([self.schema[c] for c in cols])
        )

    # -- 扫描 -------------------------------------------------------------
    def scanner(self, **kwargs: Any) -> Any:
        from .scanner import Scanner

        return Scanner(self, **kwargs)

    def to_batches(
        self,
        columns: Optional[Sequence[str]] = None,
        *,
        filter: Optional[str] = None,
        limit: Optional[int] = None,
        batch_size: Optional[int] = None,
    ) -> Iterator[RecordBatch]:
        return self.scanner(
            columns=columns, filter=filter, limit=limit, batch_size=batch_size
        ).to_batches()

    def to_arrow(
        self,
        columns: Optional[Sequence[str]] = None,
        *,
        filter: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        batches = list(self.to_batches(columns, filter=filter, limit=limit))
        table = RecordBatch.concat(batches) if batches else RecordBatch.empty(self.schema)
        return table.to_arrow()

    def to_pylist(
        self,
        columns: Optional[Sequence[str]] = None,
        *,
        filter: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        batches = list(self.to_batches(columns, filter=filter, limit=limit))
        if not batches:
            return []
        combined = RecordBatch.concat(batches) if len(batches) > 1 else batches[0]
        return combined.to_pylist()

    def head(self, n: int = 5, columns: Optional[Sequence[str]] = None) -> RecordBatch:
        batches = list(self.to_batches(columns, limit=n))
        if not batches:
            return RecordBatch.empty(self.schema.select(list(columns)) if columns else self.schema)
        b = RecordBatch.concat(batches) if len(batches) > 1 else batches[0]
        return b.limit(n)

    # -- 写 ---------------------------------------------------------------
    def write(
        self,
        batches: Iterable[RecordBatch],
        *,
        mode: str = "append",
        params: Optional[WriteParams] = None,
        writer: str = "mini-lancedb",
        max_retries: int = 3,
    ) -> int:
        """写入并提交一个新版本，返回新版本号。

        ``mode`` 只有两个值（和真实 LanceDB 一致）：

        * ``"append"`` —— 追加片段；
        * ``"overwrite"`` —— **新片段取代旧片段列表**，但旧片段文件
          一个都不删（旧版本仍可读，空间由 ``cleanup_old_versions`` 回收）。
        """

        if mode not in ("append", "overwrite"):
            raise ValueError(f"mode 必须是 'append' 或 'overwrite'，收到 {mode!r}")
        params = params or WriteParams()
        batches = [b for b in batches if b.num_rows > 0]
        if not batches:
            return self.version

        # schema 演进：新数据的列可能比表多/少
        new_schema = self.schema
        for b in batches:
            new_schema = merge_schemas(new_schema, b.schema)

        attempt = 0
        while True:
            attempt += 1
            base = self if attempt == 1 else Dataset.open(self.store, self.uri)
            new_fragments: List[Fragment] = []
            for b in batches:
                aligned = _align_batch_to_schema(b, new_schema)
                for piece in _split_batch(aligned, params):
                    new_fragments.append(self._write_fragment(piece, params))

            fragments = (
                base.fragments + new_fragments if mode == "append" else list(new_fragments)
            )
            manifest = base.manifest.clone(
                version=base.version + 1,
                schema=new_schema,
                fragments=fragments,
                # 索引在 schema 变化时可能失效，这里保守地保留但打上标记
                indices=_revalidate_indices(base.indices, new_schema),
                writer=writer,
            )
            txn = Transaction(
                read_version=base.version,
                operation=Operation.APPEND if mode == "append" else Operation.OVERWRITE,
                payload={
                    "new_fragments": [f.id for f in new_fragments],
                    "num_rows": sum(f.physical_rows for f in new_fragments),
                    "mode": mode,
                },
                writer=writer,
            )
            if self._commit(base, manifest, txn):
                self.manifest = manifest
                self._row_map_cache = None
                return manifest.version
            if attempt >= max_retries:
                raise CommitConflict(
                    "写入时反复发生提交冲突",
                    uri=self.uri,
                    attempts=attempt,
                    hint="请降低并发写入者数量，或增大重试次数",
                )

    def _write_fragment(self, batch: RecordBatch, params: WriteParams) -> Fragment:
        """把一个 batch 落成一个片段（一个 ``.lance`` 文件）。

        真实 Lance 会按 ``max_rows_per_file`` 把大 batch 切成多个文件，
        也让一次 fragment 写入可以只包含"新增的列"。本实现保持
        "一个片段一个文件"，但把 ``fields`` 列表保留下来 —— 结构上和
        真实格式一致，所以"按列组分文件"的能力随时可以接上。
        """

        path = f"{DATA_DIR}/{uuid.uuid4().hex}.lance"
        stats = write_lance_file(
            self.store,
            path,
            [batch],
            target_values_per_page=params.target_values_per_page,
            compression=params.compression,
        )
        field_ids = [f.field_id or 0 for f in batch.schema.fields]
        data_file = DataFile(
            path=path,
            fields=field_ids,
            num_rows=batch.num_rows,
            column_names=list(batch.schema.names),
            size_bytes=stats.total_bytes,
            # 列统计抄进 manifest：查询剪枝时零 I/O
            column_stats=dict(stats.zone_maps),
        )
        return Fragment(
            id=new_fragment_id(),
            files=[data_file],
            physical_rows=batch.num_rows,
        )

    def _commit(self, base: "Dataset", manifest: Manifest, txn: Transaction) -> bool:
        """写入事务文件 + 条件写 manifest。返回是否成功。"""

        # 事务文件先写：它不权威，但能让"提交成功但 manifest 丢失"可重放
        txn.payload.setdefault("target_version", manifest.version)
        write_transaction(self.store, txn)
        manifest.transaction_id = txn.id
        if not write_manifest_if_absent(self.store, manifest):
            return False
        write_latest_hint(self.store, manifest.version)
        return True

    # -- 删除 -------------------------------------------------------------
    def _delete_append(
        self,
        by_fragment: Mapping[int, Sequence[int]],
        append_batch: Optional[RecordBatch],
        *,
        operation: str,
        payload: Dict[str, Any],
        writer: str = "mini-lancedb",
        params: Optional[WriteParams] = None,
    ) -> int:
        """**一次事务里**完成"删一批行 + 追加一批行"，返回新版本号。

        LanceDB 的 ``delete`` / ``update`` / ``merge_insert`` 全都走这条路。
        关键在于它只提交一次：如果分成"先删再写"两次提交，中途失败就会
        留下"行已经删了、新行还没写"的空洞 —— 这在 upsert 场景下
        等价于**丢数据**。

        代价是这里同时要产出两类东西：若干新的删除文件，和若干新片段。
        两者都是不可变的、先写不提交，最后靠一次条件写把它们一起"点亮"。
        """

        params = params or WriteParams()
        fragments = list(self.fragments)
        num_deleted = 0
        new_deletion_files: List[Dict[str, Any]] = []
        for f_idx, rows in by_fragment.items():
            if not rows:
                continue
            frag = fragments[f_idx]
            old = (
                read_deletion_offsets(self.store, frag.deletion_file, frag.physical_rows)
                if frag.deletion_file
                else np.zeros(0, dtype=np.int64)
            )
            merged = merge_deletions(old, rows)
            gained = len(merged) - len(old)
            if gained <= 0:
                continue
            df = write_deletion_file(
                self.store, frag.id, frag.physical_rows, merged, self.version
            )
            fragments[f_idx] = Fragment(
                id=frag.id,
                files=frag.files,
                physical_rows=frag.physical_rows,
                deletion_file=df,
                metadata=dict(frag.metadata),
            )
            num_deleted += gained
            new_deletion_files.append({"fragment": frag.id, "rows": int(len(merged))})

        new_fragments: List[Fragment] = []
        if append_batch is not None and append_batch.num_rows:
            aligned = _align_batch_to_schema(append_batch, self.schema)
            for piece in _split_batch(aligned, params):
                new_fragments.append(self._write_fragment(piece, params))

        if num_deleted == 0 and not new_fragments:
            return self.version

        # 删行会让**逻辑行号整体前移**，而索引里存的正是旧的行号。
        # 不处理的话，下一次向量检索会拿一个指向已删行的行号去回表，
        # 轻则报"行号越界"，重则返回错的行。
        indices = self._remap_indices_after_delete(by_fragment)

        manifest = self.manifest.clone(
            version=self.version + 1,
            fragments=fragments + new_fragments,
            indices=indices,
            writer=writer,
        )
        txn = Transaction(
            read_version=self.version,
            operation=operation,
            payload={
                **payload,
                "num_deleted": num_deleted,
                "new_fragments": [f.id for f in new_fragments],
                "deletion_files": new_deletion_files,
            },
            writer=writer,
        )
        if not self._commit(self, manifest, txn):
            raise CommitConflict("提交时发生并发写", uri=self.uri, operation=operation)
        self.manifest = manifest
        self._row_map_cache = None
        return manifest.version

    def _logical_rows_to_fragment_offsets(
        self, logical_rows: Sequence[int]
    ) -> Dict[int, List[int]]:
        """逻辑行号 → ``{片段下标: [片段内物理偏移]}``。

        这一步是"删除/更新"的核心：用户给的是逻辑行号
        （删掉的行不占号），而删除文件记的是**物理偏移**
        （删掉的行依然占号）。两者的映射由行映射表给出。
        """

        fi, offs = self._row_map()
        rows = np.asarray(logical_rows, dtype=np.int64)
        if len(rows) == 0:
            return {}
        bad = rows[(rows < 0) | (rows >= len(fi))]
        if len(bad):
            raise IndexError(f"行号越界: {int(bad[0])}（共 {len(fi)} 行）")
        out: Dict[int, List[int]] = {}
        for f, p in zip(fi[rows], offs[rows]):
            out.setdefault(int(f), []).append(int(p))
        return out

    def delete(self, predicate: str, *, writer: str = "mini-lancedb") -> DeleteResult:
        """按谓词删除行。

        **删除是纯元数据操作**：不为被删的行写任何数据，只写删除文件。
        代价是索引里会留下"幽灵行"，读的时候要过滤掉；
        而且删除比例高了以后必须压实，否则读放大越来越严重。
        """

        mask = self._predicate_mask(predicate)
        targets = np.flatnonzero(mask)
        if len(targets) == 0:
            return DeleteResult(0, self.version)
        grouped = self._logical_rows_to_fragment_offsets(targets)
        version = self._delete_append(
            grouped,
            None,
            operation=Operation.DELETE,
            payload={"predicate": predicate},
            writer=writer,
        )
        return DeleteResult(len(targets), version)

    # -- 更新 -------------------------------------------------------------
    def update(
        self,
        updates: Optional[Mapping[str, Any]] = None,
        *,
        where: Optional[str] = None,
        values_sql: Optional[Mapping[str, str]] = None,
        writer: str = "mini-lancedb",
    ) -> UpdateResult:
        """更新匹配的行。

        LanceDB 的 ``update`` 语义上是 **"删掉再按新值追加"**，
        而不是就地改写：

        * 就地改写要重写整个片段文件（文件不可变）——
          为了改一行而重写一整个片段；
        * 删 + 追加只写"被改的那些行"，代价与改动量成正比，
          而且天然保留历史版本。

        副作用是**行序会变**（被更新的行跑到末尾）。用
        ``limit`` + ``offset`` 做分页时会因此"翻页漏行"，
        真实 LanceDB 的文档也专门警告过这一点。
        """

        if not updates and not values_sql:
            raise PlanError("update 必须给出 updates 或 values_sql")
        where = where or "true"
        mask = self._predicate_mask(where)
        rows = np.flatnonzero(mask)
        if len(rows) == 0:
            return UpdateResult(0, self.version)

        current = self.take_rows(rows, columns=list(self.schema.names))
        new_values: Dict[str, List[Any]] = {
            name: list(current.column(name).to_pylist()) for name in self.schema.names
        }

        if updates:
            for k, v in updates.items():
                if k not in self.schema:
                    raise PlanError(f"要更新的列不存在: {k!r}", available=self.schema.names)
                if _is_expression_value(v):
                    new_values[k] = self._eval_update_expression(str(v), current)
                else:
                    new_values[k] = [v] * len(rows)
        if values_sql:
            for k, expr in values_sql.items():
                if k not in self.schema:
                    raise PlanError(f"要更新的列不存在: {k!r}", available=self.schema.names)
                new_values[k] = self._eval_update_expression(expr, current)

        new_batch = RecordBatch.from_pylist(
            [{k: new_values[k][i] for k in self.schema.names} for i in range(len(rows))],
            self.schema,
            strict=False,
        )
        grouped = self._logical_rows_to_fragment_offsets(rows)
        version = self._delete_append(
            grouped,
            new_batch,
            operation=Operation.UPDATE,
            payload={
                "where": where,
                "updates": _json_safe(dict(updates or {})),
                "values_sql": dict(values_sql or {}),
            },
            writer=writer,
        )
        return UpdateResult(len(rows), version)

    def _eval_update_expression(self, expr: str, batch: RecordBatch) -> List[Any]:
        """求值 ``values_sql`` 里的表达式（可以引用现有列，如 ``id + 100``）。"""

        from .expr import evaluate, parse

        parsed = parse(str(expr), batch.schema)
        return list(evaluate(parsed, batch).to_pylist())

    # -- merge_insert -----------------------------------------------------
    def merge_insert(self, on: Union[str, Sequence[str]]) -> "MergeInsertBuilder":
        """创建 upsert 构建器。用法::

            (tbl.merge_insert("id")
                .when_matched_update_all()
                .when_not_matched_insert_all()
                .execute(new_rows))

        整个 merge 在**一次提交**里完成（删除文件 + 新片段一起点亮），
        这一点很重要：否则 upsert 中途失败会丢数据。
        """

        return MergeInsertBuilder(self, on)

    def _execute_merge(
        self, builder: "MergeInsertBuilder", source: RecordBatch
    ) -> MergeResult:
        keys = builder.key_columns
        for k in keys:
            if k not in source.schema:
                raise PlanError(f"源数据缺少 join 键 {k!r}", available=source.schema.names)

        # 1) 把目标表的 join 键读出来（只读键列 —— 这是本实现里最贵的一次读）
        target_keys = self._read_key_columns(keys)
        src_keys = _key_tuples(source, keys)
        tgt_keys = _key_tuples(target_keys, keys)

        # 2) 建立 key → 逻辑行号 的映射。重复键取**最后一行**，
        #    和真实 LanceDB 的 merge_insert 一致（文档里也有说明）。
        tgt_index: Dict[tuple, int] = {}
        for i, k in enumerate(tgt_keys):
            tgt_index[k] = i

        # 源数据里同一个 key 出现多次时**只保留最后一条** ——
        # 和真实 LanceDB 一致。如果不做这一步，两条同 key 的源行会把
        # 目标表里那一行更新两次，结果就是目标表凭空多出一行。
        src_index: Dict[tuple, int] = {}
        for i, k in enumerate(src_keys):
            src_index[k] = i
        deduped_src = sorted(src_index.values())

        matched_src: List[int] = []
        matched_tgt: List[int] = []
        inserted_src: List[int] = []
        for i in deduped_src:
            k = src_keys[i]
            j = tgt_index.get(k)
            if j is None:
                inserted_src.append(i)
            else:
                matched_tgt.append(j)
                matched_src.append(i)

        # 3) 处理匹配上的行
        to_delete: List[int] = []
        updated_pairs: List[Tuple[int, int]] = []  # (target_row, source_row)
        num_deleted = 0
        if matched_tgt and builder.has_matched_action():
            current = self.take_rows(matched_tgt, columns=list(self.schema.names))
            if builder.action_on_match == "delete":
                if builder.matched_delete_predicate is not None:
                    from .expr import evaluate_predicate, parse

                    pred = parse(builder.matched_delete_predicate, current.schema)
                    del_mask = evaluate_predicate(pred, current)
                else:
                    # ``when_matched_delete()`` **不给 where** 的语义是
                    # "匹配上的全删"。早期版本在这种情况下什么都不做、
                    # 还报 num_deleted_rows=0 —— 一个静默的空操作，
                    # 比报错危险得多。
                    del_mask = np.ones(len(matched_tgt), dtype=bool)
                to_delete = [matched_tgt[i] for i in np.flatnonzero(del_mask)]
                num_deleted = len(to_delete)
                keep = ~del_mask
            else:
                keep = np.ones(len(matched_tgt), dtype=bool)

            if builder.update_all:
                idx = np.flatnonzero(keep)
                to_delete.extend(matched_tgt[i] for i in idx)
                updated_pairs = [(matched_tgt[i], matched_src[i]) for i in idx]
            elif builder.action_on_match == "delete":
                pass

        # 4) 组装"要追加的行" = 更新后的行 + 新插入的行
        appended: List[Dict[str, Any]] = []
        current_all = (
            self.take_rows(
                [t for t, _ in updated_pairs], columns=list(self.schema.names)
            )
            if updated_pairs
            else None
        )
        for r, (t, s) in enumerate(updated_pairs):
            row: Dict[str, Any] = {}
            for name in self.schema.names:
                if name in source.schema:
                    row[name] = source.column(name).value(s)
                else:
                    row[name] = current_all.column(name).value(r) if current_all else None
            appended.append(row)
        for s in inserted_src:
            if not builder.insert_all:
                continue
            src_row = source.row(s)
            appended.append({k: src_row.get(k) for k in self.schema.names})

        # 5) 一次提交完成删除 + 追加
        grouped = self._logical_rows_to_fragment_offsets(to_delete) if to_delete else {}
        append_batch = (
            RecordBatch.from_pylist(appended, self.schema, strict=False) if appended else None
        )
        version = self._delete_append(
            grouped,
            append_batch,
            operation=Operation.MERGE_INSERT,
            payload={"on": list(keys), "num_matched": len(matched_tgt)},
        )
        return MergeResult(
            version=version,
            num_updated_rows=len(updated_pairs),
            num_inserted_rows=len(appended) - len(updated_pairs),
            num_deleted_rows=num_deleted,
            num_attempts=1,
            num_rows=len(appended),
        )

    def _remap_indices_after_delete(
        self, by_fragment: Mapping[int, Sequence[int]]
    ) -> List[IndexMetadata]:
        """把索引里的行号从"删除前的逻辑行号"搬到"删除后的逻辑行号"。

        映射本身是一个纯算术：旧行 i 的新位置 = ``i - (i 之前删掉的行数)``，
        被删掉的行映射到 ``-1``。因为删除不会重排片段，
        逻辑行号的顺序就是"片段顺序 + 片段内物理偏移顺序"。

        搬不动的索引（比如实现里没做 remap 的类型）会被标记为失效，
        查询路径会跳过失效索引并退化成暴力检索 —— **降级比崩溃好**。
        """

        if not self.indices or self.num_rows == 0:
            return _revalidate_indices(self.indices, self.schema)

        fi, offs = self._row_map()
        deleted_indices: List[int] = []
        for frag_idx, rows in by_fragment.items():
            if not rows:
                continue
            positions = np.flatnonzero(fi == frag_idx)
            # 片段内物理偏移 → 全局逻辑行号，才能和索引里的行号对齐
            local = {int(p): int(g) for g, p in zip(positions, offs[positions])}
            deleted_indices.extend(local[int(r)] for r in rows if int(r) in local)

        out: List[IndexMetadata] = []
        if not deleted_indices:
            return _revalidate_indices(self.indices, self.schema)

        deleted = np.unique(np.asarray(sorted(deleted_indices), dtype=np.int64))
        old_n = self.num_rows
        old_to_new = np.arange(old_n, dtype=np.int64) - np.searchsorted(
            deleted, np.arange(old_n), side="left"
        )
        old_to_new[deleted[deleted < old_n]] = -1

        for meta in self.indices:
            if not meta.valid:
                out.append(meta)
                continue
            try:
                index = self.load_index(meta.name)
                index.remap(old_to_new)
                istore = ObjectStoreIndexStore(self.store, f"{INDEX_DIR}/{meta.name}")
                files = index.save(istore)
                # **重映射会就地修改缓存里的那个对象。** 文件名没变，
                # 所以缓存 key 也没变 —— 如果不显式踢掉，任何早先拿到
                # 这个索引引用的代码都会看到一个"悄悄变了"的对象。
                # 就地改共享状态是最难查的那类 bug，这里直接清掉重来。
                _drop_index_cache(self.uri, meta.name)
                stats = index.stats()
                clone = IndexMetadata.from_json(meta.to_json())
                clone.num_indexed_rows = int(stats.get("num_indexed_rows", 0))
                clone.size_bytes = int(stats.get("size_bytes", 0))
                clone.files = files
                out.append(clone)
            except Exception:
                clone = IndexMetadata.from_json(meta.to_json())
                clone.valid = False
                clone.details = dict(clone.details)
                clone.details["invalidated_by_delete"] = True
                out.append(clone)
        return out

    def _read_key_columns(self, keys: Sequence[str]) -> RecordBatch:
        """只把 join 键列读出来。"""

        schema = Schema([self.schema[k] for k in keys])
        batches = list(self.scanner(columns=list(keys)).to_batches())
        if not batches:
            return RecordBatch.empty(schema)
        combined = RecordBatch.concat(batches) if len(batches) > 1 else batches[0]
        return combined.select(list(keys))

    # -- schema 演进 ------------------------------------------------------
    def add_columns(self, new_columns: Mapping[str, Any]) -> int:
        """加列：**只改 manifest，不碰任何数据文件**。"""

        schema = self.schema
        for name, spec in new_columns.items():
            if name in schema:
                raise SchemaError(f"列 {name!r} 已存在", column=name)
            t = spec
            if isinstance(spec, str):
                from .types import parse_type

                t = parse_type(spec)
            elif callable(spec) and not isinstance(spec, DataType):
                t = spec()  # 允许传"表达式生成的常量列"
            schema = schema.append_column(Field(name, t, nullable=True))
        manifest = self.manifest.clone(
            version=self.version + 1, schema=schema, writer="mini-lancedb"
        )
        txn = Transaction(
            read_version=self.version,
            operation=Operation.ALTER_SCHEMA,
            payload={"added": list(new_columns.keys())},
        )
        if not self._commit(self, manifest, txn):
            raise CommitConflict("加列时发生提交冲突", uri=self.uri)
        self.manifest = manifest
        self._row_map_cache = None
        return manifest.version

    def drop_columns(self, columns: Sequence[str]) -> int:
        """删列：从 schema 里摘掉 field_id，数据文件原样保留。

        这就是"字段要有稳定 id"的直接回报 —— 如果按位置编号，
        删掉中间一列之后所有后续列的数据都会读错位。
        """

        schema = self.schema
        for c in columns:
            if c not in schema:
                raise SchemaError(f"列 {c!r} 不存在", available=schema.names)
            schema = schema.drop_column(c)
        manifest = self.manifest.clone(
            version=self.version + 1, schema=schema, writer="mini-lancedb"
        )
        txn = Transaction(
            read_version=self.version,
            operation=Operation.ALTER_SCHEMA,
            payload={"dropped": list(columns)},
        )
        if not self._commit(self, manifest, txn):
            raise CommitConflict("删列时发生提交冲突", uri=self.uri)
        self.manifest = manifest
        return manifest.version

    def alter_columns(self, columns: Mapping[str, Any]) -> int:
        """改列类型 / 改名 / 改可空性。**改类型要重写数据**，是唯一昂贵的演进。

        真实 Lance 的 ``alter_columns`` 会先判断两个类型是不是
        "compatible"（比如 int32 → int64 可以按位解释并加宽，不用重写；
        int32 → string 必须重写），据此决定是"只改元数据"还是"重写数据"。

        **本实现里一个真实的坑值得单独讲。** 早期版本无论改什么类型
        都只更新 manifest，不碰数据。结果是：schema 说这一列是
        ``fixed_size_list<float32>[8]``，而读出来的数组还是
        ``fixed_size_list<float64>[4]``——**schema 和数据的类型对不上**，
        而且正常读路径不会报错（只有显式的 ``RecordBatch.validate()``
        才抓得到）。这类"元数据撒谎"的 bug 比崩溃危险得多，
        因为它会一路传播到用户的业务代码里。

        现在的做法是：**只要类型变了，就整表重写一遍**（读出来、按新类型
        转、重新写片段、在同一个事务里换掉片段列表）。
        保守但正确。只改名或只改可空性时仍然只动元数据。
        """

        from .types import parse_type

        original = self.schema
        # 分两步走：**先改名、再改类型**。
        # 两步混在一起写会让"用旧名字找列、用新名字查类型"这种错误
        # 悄悄出现，而且只在同时改名和改类型时才触发。
        rename_pairs: List[Tuple[str, str]] = []
        for name, spec in columns.items():
            if name not in original:
                raise SchemaError(f"列 {name!r} 不存在", available=original.names)
            if isinstance(spec, Mapping) and "name" in spec:
                new_name = str(spec["name"])
                if new_name != name:
                    rename_pairs.append((name, new_name))

        schema = self.schema
        for old, new in rename_pairs:
            schema = schema.rename_column(old, new)

        needs_rewrite = False
        for name, spec in columns.items():
            target = dict(rename_pairs).get(name, name)
            old_field = original[name]
            new_type = old_field.type
            new_nullable = old_field.nullable
            if isinstance(spec, str):
                new_type = parse_type(spec)
            elif isinstance(spec, Mapping):
                if "type" in spec:
                    t = spec["type"]
                    new_type = parse_type(t) if isinstance(t, str) else t
                if "nullable" in spec:
                    new_nullable = bool(spec["nullable"])
            else:
                new_type = spec
            if new_type != old_field.type:
                needs_rewrite = True
            schema = schema.set_column(
                target,
                Field(target, new_type, new_nullable, dict(old_field.metadata), old_field.field_id),
            )

        if not needs_rewrite:
            # 纯元数据变更：只改名 / 只改可空性，不碰任何数据文件
            manifest = self.manifest.clone(
                version=self.version + 1, schema=schema, writer="mini-lancedb"
            )
            txn = Transaction(
                read_version=self.version,
                operation=Operation.ALTER_SCHEMA,
                payload={
                    "altered": list(columns.keys()),
                    "requires_rewrite": False,
                    "renames": rename_pairs,
                },
            )
            if not self._commit(self, manifest, txn):
                raise CommitConflict("改列时发生提交冲突", uri=self.uri)
            self.manifest = manifest
            return manifest.version

        # 类型变了 —— 必须重写，而且要在**一次提交**里完成，
        # 否则中途失败会留下"schema 已经改了、数据还是旧的"的中间态。
        old_schema = self.schema
        batches = []
        for frag in self.fragments:
            b = self.read_fragment(frag, columns=list(old_schema.names))
            if b.num_rows:
                batches.append(b)
        combined = RecordBatch.concat(batches) if batches else RecordBatch.empty(old_schema)

        # 按新 schema 重建：逐列 cast，列名/顺序按新 schema 来，
        # 重新命名产生的映射在这一步生效。
        reverse_rename = {new: old for old, new in rename_pairs}
        cols: Dict[str, Array] = {}
        for f in schema.fields:
            src_name = reverse_rename.get(f.name, f.name)
            if src_name in combined:
                arr = combined.column(src_name)
                cols[f.name] = arr.cast(f.type) if arr.type != f.type else arr
            else:
                cols[f.name] = array_from_pylist(f.type, [None] * combined.num_rows)
        rewritten = RecordBatch(schema, cols)
        rewritten.validate()

        params = WriteParams(target_values_per_page=8192)
        new_fragments: List[Fragment] = []
        for piece in _split_batch(rewritten, params):
            new_fragments.append(self._write_fragment(piece, params))

        manifest = self.manifest.clone(
            version=self.version + 1,
            schema=schema,
            fragments=new_fragments,
            # 重写会改行号，索引里的行号已经指错了
            indices=_revalidate_indices(self.indices, schema),
            writer="mini-lancedb",
        )
        txn = Transaction(
            read_version=self.version,
            operation=Operation.ALTER_SCHEMA,
            payload={
                "altered": list(columns.keys()),
                "requires_rewrite": True,
                "rows_rewritten": rewritten.num_rows,
                "fragments_created": len(new_fragments),
            },
        )
        if not self._commit(self, manifest, txn):
            raise CommitConflict("改列时发生提交冲突", uri=self.uri)
        self.manifest = manifest
        self._row_map_cache = None
        return manifest.version

    # -- 版本 -------------------------------------------------------------
    def checkout(self, version: Union[int, str]) -> "Dataset":
        """时间旅行：返回一个指向历史版本的 Dataset。

        **零成本**：只读一个老的 manifest。数据文件都还在，
        所以老版本和新版本读起来的代码路径完全一样。
        """

        return Dataset.open(self.store, self.uri, version=version)

    def restore(self, version: Union[int, str]) -> int:
        """把某个历史版本的内容**作为一个新版本**提交回来。

        注意不是"回退指针"：v7 的数据仍在、v8 新增的数据也仍在，
        只是 v9 的内容等于 v5。这样"回滚"本身也是可回滚的。
        """

        old = Dataset.open(self.store, self.uri, version=version)
        manifest = old.manifest.clone(
            version=self.version + 1,
            fragments=[Fragment.from_json(f.to_json()) for f in old.fragments],
            schema=old.schema,
        )
        manifest.metadata = dict(manifest.metadata)
        manifest.metadata["restored_from"] = str(version)
        txn = Transaction(
            read_version=self.version,
            operation=Operation.RESTORE,
            payload={"restored_from": version},
        )
        if not self._commit(self, manifest, txn):
            raise CommitConflict("恢复时发生提交冲突", uri=self.uri)
        self.manifest = manifest
        self._row_map_cache = None
        return manifest.version

    def list_versions(self) -> List[VersionInfo]:
        out: List[VersionInfo] = []
        for v in sorted(list_manifest_versions(self.store)):
            try:
                m = read_manifest(self.store, v)
            except MiniLanceError:
                continue
            out.append(
                VersionInfo(
                    version=m.version,
                    timestamp=m.timestamp,
                    operation=dict(m.metadata).get("restored_from", "") and "restore" or "",
                    num_rows=m.num_rows,
                    num_fragments=m.num_fragments,
                    num_deleted_rows=m.num_deleted_rows,
                    transaction_id=m.transaction_id,
                )
            )
        return out

    # -- 标签 -------------------------------------------------------------
    def create_tag(self, name: str, *, version: Optional[int] = None) -> TagInfo:
        v = self.version if version is None else int(version)
        m = read_manifest(self.store, v)
        info = TagInfo(version=v, manifest_size=len(m.to_bytes()))
        self.store.put(tag_path(name), json.dumps(info.to_json(), sort_keys=True).encode())
        return info

    def list_tags(self) -> Dict[str, Dict[str, Any]]:
        return list_refs(self.store, "tag")

    def delete_tag(self, name: str) -> bool:
        path = tag_path(name)
        if not self.store.exists(path):
            return False
        self.store.delete(path)
        return True

    def tag_version(self, name: str) -> int:
        refs = self.list_tags()
        if name not in refs:
            raise VersionNotFoundError("标签不存在", tag=name, available=sorted(refs))
        return int(refs[name]["version"])

    # -- 索引 -------------------------------------------------------------
    def create_index(
        self,
        column: str,
        *,
        index_type: str = "IVF_PQ",
        name: Optional[str] = None,
        metric: str = "l2",
        replace: bool = True,
        **params: Any,
    ) -> IndexMetadata:
        """建索引并把元数据写进 manifest。

        索引构建是**在 dataset 版本之上**做的：索引记录自己覆盖哪一行、
        对应哪个版本。之后再 append 的数据不在索引里，需要
        "delta 索引"或重建来覆盖（见 ``optimize``）。
        """

        from .index import create_vector_index, create_scalar_index
        from .index.base import normalize_index_type

        itype = normalize_index_type(index_type)
        if column not in self.schema:
            raise PlanError(f"列不存在: {column!r}", available=self.schema.names)
        name = name or f"{column}_idx"
        if not replace:
            existing = self.manifest.index_by_name(name)
            if existing is not None:
                raise IndexNotFoundError(f"索引 {name!r} 已存在", name=name)
        is_vector = bool(self.schema[column].is_vector)

        # 行号用**逻辑行号**：简单、直观。真实 Lance 用的是稳定的
        # 64 位行地址 ``(fragment_id << 32) | offset``，好处是压实之后
        # 还能把老行号映射到新位置；代价是每次回表都要多做一次换算。
        # 本实现选简单的那条路，并在 compaction 里把索引标记失效。
        rows = np.arange(self.num_rows, dtype=np.uint64)
        batch = self.take_rows(rows, columns=[column])
        row_ids = rows.copy()
        data: Optional[np.ndarray] = None
        if is_vector:
            data = np.asarray(batch.to_numpy(column), dtype=np.float32)
            metadata = IndexMetadata(
                name=name,
                index_type=itype,
                columns=[column],
                version=self.version,
                params={"distance_type": metric, **params},
                num_indexed_rows=len(rows),
            )
            index = create_vector_index(itype, data, row_ids, metadata)
        else:
            values = batch.column(column)
            metadata = IndexMetadata(
                name=name,
                index_type=itype,
                columns=[column],
                version=self.version,
                params=dict(params),
                num_indexed_rows=len(rows),
            )
            index = create_scalar_index(itype, values, row_ids, metadata)

        store_prefix = f"{INDEX_DIR}/{name}"
        istore = ObjectStoreIndexStore(self.store, store_prefix)
        files = index.save(istore)
        stats = index.stats()
        metadata.files = files
        metadata.size_bytes = int(stats.get("size_bytes", 0))
        metadata.details = {k: v for k, v in stats.items() if k not in ("size_bytes",)}

        indices = [i for i in self.manifest.indices if i.name != name] + [metadata]
        manifest = self.manifest.clone(
            version=self.version + 1, indices=indices, writer="mini-lancedb"
        )
        txn = Transaction(
            read_version=self.version,
            operation=Operation.CREATE_INDEX,
            payload={"name": name, "type": itype, "column": column, "files": files},
        )
        if not self._commit(self, manifest, txn):
            raise CommitConflict("建索引时发生提交冲突", uri=self.uri)
        self.manifest = manifest
        return metadata

    def drop_index(self, name: str) -> int:
        if self.manifest.index_by_name(name) is None:
            raise IndexNotFoundError("索引不存在", name=name)
        indices = [i for i in self.manifest.indices if i.name != name]
        manifest = self.manifest.clone(
            version=self.version + 1, indices=indices, writer="mini-lancedb"
        )
        txn = Transaction(
            read_version=self.version, operation=Operation.DROP_INDEX, payload={"name": name}
        )
        if not self._commit(self, manifest, txn):
            raise CommitConflict("删索引时发生提交冲突", uri=self.uri)
        self.manifest = manifest
        self.store.delete_prefix(f"{INDEX_DIR}/{name}")
        return manifest.version

    def list_indices(self) -> List[IndexMetadata]:
        return list(self.manifest.indices)

    def load_index(self, name: str, *, use_cache: bool = True) -> Any:
        """加载一个索引。

        **默认带进程内缓存。** 这不是可有可无的优化：一个 5 万行表的
        B 树索引文件就有 1.5 MB，一个 IVF_PQ 索引可能是几百 MB。
        如果每次查询都重新读一遍，"建了索引反而更慢"就成了真的 ——
        我们在自己的例子里量到过：一次 `WHERE n BETWEEN` 查询里，
        1.5 MB 的读取量几乎全是索引文件的加载，真正的数据只占几十 KB。

        真实 Lance 有一个专门的 **index cache**（并且它同时是已知的
        资源泄漏点：有一个仍未修复的 issue 描述
        "index cache leaks one file descriptor per optimize() per table"）。
        缓存换性能这件事本身没有争议，争议只在于**什么时候失效**。

        我们的失效规则很简单：**key 里带版本号和文件清单**。
        索引文件是不可变的，版本一变、文件一变，key 就变了，
        老条目自然被淘汰（不会被读到）。
        """

        meta = self.manifest.index_by_name(name)
        if meta is None:
            raise IndexNotFoundError("索引不存在", name=name, available=[i.name for i in self.indices])
        key = (self.uri, name, meta.version, tuple(meta.files))
        if use_cache and key in _INDEX_CACHE:
            return _INDEX_CACHE[key]
        store = ObjectStoreIndexStore(self.store, f"{INDEX_DIR}/{name}")
        from .index import load_index as _load

        index = _load(meta, store)
        if use_cache:
            _INDEX_CACHE[key] = index
        return index

    # -- 谓词 -------------------------------------------------------------
    def _predicate_mask(self, predicate: Optional[str]) -> np.ndarray:
        """把 SQL 谓词求值成整表的逻辑行布尔掩码。"""

        if predicate is None or str(predicate).strip() in ("", "true", "TRUE"):
            return np.ones(self.num_rows, dtype=bool)
        from .expr import evaluate_predicate, parse

        masks: List[np.ndarray] = []
        for batch in self.to_batches():
            expr = parse(predicate, batch.schema)
            masks.append(evaluate_predicate(expr, batch))
        if not masks:
            return np.zeros(0, dtype=bool)
        return np.concatenate(masks) if len(masks) > 1 else masks[0]

    # -- 维护 -------------------------------------------------------------
    def compact(
        self,
        *,
        target_rows_per_fragment: int = DEFAULT_TARGET_ROWS_PER_FRAGMENT,
        materialize_deletions: bool = True,
        min_rows_per_fragment: int = MIN_ROWS_PER_FRAGMENT,
        max_fragments: Optional[int] = None,
    ) -> Dict[str, Any]:
        """压实：合并小片段、把删除真正落地。

        为什么必须压实？三个原因，一个比一个现实：

        1. **读放大**：查一次要打开 N 个文件；N 大了就是 N 次提交 I/O；
        2. **元数据膨胀**：manifest 要记住每个片段，几十万个小片段
           会让 manifest 大到"打开表都慢"；
        3. **索引失效**：压实改了行号，索引必须重建或重映射。

        代价是**一次完整重写**，所以它是个后台任务，不是实时操作。
        """

        fragments = list(self.fragments)
        before = {
            "num_fragments": len(fragments),
            "num_deleted_rows": self.num_deleted_rows,
            "num_rows": self.num_rows,
        }
        if not fragments:
            return {"compacted": False, **before}

        mergeable = [
            f
            for f in fragments
            if f.num_rows < target_rows_per_fragment
            or (materialize_deletions and f.num_deleted_rows > 0)
        ]
        mergeable_ids = {f.id for f in mergeable}
        if max_fragments is not None:
            mergeable = mergeable[:max_fragments]
            mergeable_ids = {f.id for f in mergeable}
        if not mergeable:
            return {"compacted": False, **before}

        # 把要压实的片段整个读出来（含已删除的行会被丢掉）
        batches = []
        total_rows = 0
        for f in mergeable:
            b = self.read_fragment(f, columns=list(self.schema.names))
            if b.num_rows:
                batches.append(b)
                total_rows += b.num_rows
        if not batches:
            # 全被删光了 —— 直接把这些片段从 manifest 里去掉
            kept = [f for f in fragments if f.id not in mergeable_ids]
            manifest = self.manifest.clone(
                version=self.version + 1, fragments=kept, writer="mini-lancedb"
            )
            self._commit(
                self,
                manifest,
                Transaction(self.version, Operation.COMPACT, {"dropped_all_deleted": True}),
            )
            self.manifest = manifest
            self._row_map_cache = None
            return {"compacted": True, "before": before, "after": {"num_fragments": len(kept)}}

        combined = RecordBatch.concat(batches) if len(batches) > 1 else batches[0]
        params = WriteParams(target_values_per_page=8192)
        new_fragments: List[Fragment] = []
        for piece in combined.split_by_rows(target_rows_per_fragment):
            new_fragments.append(self._write_fragment(piece, params))

        old_ids = {f.id for f in mergeable}
        kept = [f for f in fragments if f.id not in old_ids]
        # 行号重映射表：旧片段 id → 新片段列表里的位置由 fragment_to_new 决定
        frag_map = _build_fragment_map(mergeable, new_fragments)
        new_indices = _remap_indices(self, self.indices, frag_map)
        manifest = self.manifest.clone(
            version=self.version + 1,
            fragments=kept + new_fragments,
            indices=new_indices,
            writer="mini-lancedb",
        )
        txn = Transaction(
            read_version=self.version,
            operation=Operation.COMPACT,
            payload={
                "merged_fragments": sorted(old_ids),
                "num_new_fragments": len(new_fragments),
                "rows": total_rows,
            },
        )
        if not self._commit(self, manifest, txn):
            raise CommitConflict("压实时发生提交冲突", uri=self.uri)
        self.manifest = manifest
        self._row_map_cache = None
        after = {
            "num_fragments": len(manifest.fragments),
            "num_deleted_rows": manifest.num_deleted_rows,
            "num_rows": manifest.num_rows,
        }
        return {
            "compacted": True,
            "before": before,
            "after": after,
            "rows_rewritten": total_rows,
            "fragments_merged": len(old_ids),
            "fragments_created": len(new_fragments),
        }

    def cleanup_old_versions(
        self,
        *,
        older_than: Optional[float] = None,
        keep_versions: int = 1,
        delete_unverified: bool = False,
    ) -> Dict[str, Any]:
        """回收旧版本占用的空间。

        **危险的敌人是"还在跑的读者"。** 一个长事务可能正在读 v3，
        这时把 v3 的文件删掉，它就会读到半个表。所以典型策略是
        "保留最近 N 个版本 + 只删早于 X 分钟前的版本"，
        用时间换安全 —— ``older_than`` 就是那个 X（秒）。

        注意真实 Lance 的 ``cleanup_old_versions`` **没有 dry_run 参数**；
        本实现提供 ``dry_run`` 只是为了教学方便（先看会删多少）。
        """

        versions = sorted(list_manifest_versions(self.store), reverse=True)
        if len(versions) <= keep_versions:
            return {"cleaned": False, "reason": "版本数不足", "kept": versions}

        now = time.time()
        cutoff = None if older_than is None else now - float(older_than)
        removable: List[int] = []
        for v in versions[keep_versions:]:
            m = read_manifest(self.store, v)
            if cutoff is not None and m.timestamp >= cutoff:
                continue
            if not delete_unverified and v == self.version:
                continue
            removable.append(v)

        # 标签指向的版本必须**先**并进 keep_set，再算 live_files。
        # 这里曾经有过一个数据丢失的 bug：referenced_tags 在 live_files
        # 算完之后才算，于是被标签保护的版本虽然没被删 manifest，
        # 它的数据文件和删除文件却已经被当成垃圾回收掉了 ——
        # checkout 那个标签会读到一张残缺的表（StoreError: 对象不存在）。
        # 顺序不能调换：keep_set → live_files → 回收。
        referenced_tags = {int(t["version"]) for t in self.list_tags().values()}
        protected = referenced_tags & set(versions)

        keep_set = (
            set(versions[:keep_versions])
            | {v for v in versions if v not in removable}
            | protected
        )
        live_files = set()
        for v in sorted(keep_set):
            m = read_manifest(self.store, v)
            for f in m.fragments:
                for df in f.files:
                    live_files.add(df.path)
                if f.deletion_file:
                    live_files.add(f.deletion_file.path)

        for v in referenced_tags:
            if v in removable:
                removable.remove(v)

        # 收集候选删除对象
        victims: List[str] = []
        for m in self.store.list(DATA_DIR + "/"):
            if m.path not in live_files:
                victims.append(m.path)
        for m in self.store.list("_deletions/"):
            if m.path not in live_files:
                victims.append(m.path)
        for v in removable:
            victims.append(manifest_path(v))

        freed = sum(self.store.size(p) for p in victims if self.store.exists(p))
        if not delete_unverified:
            # 不真正删除，只报告（教学默认）
            return {
                "cleaned": False,
                "dry_run": True,
                "removable_versions": removable,
                "removable_files": len(victims),
                "bytes_reclaimable": freed,
                "kept_versions": sorted(keep_set, reverse=True),
            }

        for p in victims:
            self.store.delete(p)
        return {
            "cleaned": True,
            "removed_versions": removable,
            "removed_files": len(victims),
            "bytes_freed": freed,
            "kept_versions": sorted(keep_set, reverse=True),
        }

    def optimize(
        self,
        *,
        target_rows_per_fragment: int = DEFAULT_TARGET_ROWS_PER_FRAGMENT,
        cleanup: bool = False,
        older_than: Optional[float] = 24 * 3600,
    ) -> Dict[str, Any]:
        """``optimize()`` —— 把"该做的维护"一次做完。

        真实 LanceDB 的 ``optimize()`` 做三件事：压实小文件、
        增量索引合并（把新数据纳入索引）、清理旧版本。它**返回 ``None``**
        （尽管类型标注说返回 ``OptimizeStats``，实测是 ``None``）——
        想拿统计信息得自己调 ``compact_files`` / ``cleanup_old_versions``。
        本实现返回一个字典，便于教程里观察效果。
        """

        result: Dict[str, Any] = {}
        result["compact"] = self.compact(target_rows_per_fragment=target_rows_per_fragment)
        if cleanup:
            result["cleanup"] = self.cleanup_old_versions(older_than=older_than)
        result["stats"] = self.stats()
        return result

    # -- 统计 -------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        frag_sizes = [f.num_rows for f in self.fragments]
        total_bytes = sum(f.size_bytes for f in self.fragments)
        return {
            "uri": self.uri,
            "version": self.version,
            "num_rows": self.num_rows,
            "physical_rows": self.physical_rows,
            "num_deleted_rows": self.num_deleted_rows,
            "num_columns": len(self.schema),
            "num_fragments": len(self.fragments),
            "num_indices": len(self.indices),
            "data_bytes": total_bytes,
            "bytes_per_row": round(total_bytes / self.num_rows, 2) if self.num_rows else 0.0,
            "avg_fragment_rows": round(sum(frag_sizes) / len(frag_sizes), 1) if frag_sizes else 0,
            "min_fragment_rows": min(frag_sizes) if frag_sizes else 0,
            "max_fragment_rows": max(frag_sizes) if frag_sizes else 0,
            "num_small_fragments": sum(1 for n in frag_sizes if n < MIN_ROWS_PER_FRAGMENT),
            "deletion_ratio": round(self.num_deleted_rows / self.physical_rows, 4)
            if self.physical_rows
            else 0.0,
            "indices": [
                {
                    "name": i.name,
                    "type": i.index_type,
                    "columns": i.columns,
                    "rows": i.num_indexed_rows,
                    "size_bytes": i.size_bytes,
                }
                for i in self.indices
            ],
        }


# ---------------------------------------------------------------------------
# merge_insert 的构建器
# ---------------------------------------------------------------------------


class MergeInsertBuilder:
    """对应真实 LanceDB 的 ``LanceMergeInsertBuilder``。"""

    def __init__(self, dataset: Dataset, on: Union[str, Sequence[str]]) -> None:
        self.dataset = dataset
        self.key_columns: List[str] = [on] if isinstance(on, str) else list(on)
        self.update_all = False
        self.insert_all = False
        self.action_on_match = "nothing"  # nothing | update_all | delete
        self.matched_delete_predicate: Optional[str] = None
        self.delete_not_matched_by_source = False
        self.not_matched_by_source_predicate: Optional[str] = None

    def when_matched_update_all(self, *, where: Optional[str] = None) -> "MergeInsertBuilder":
        self.update_all = True
        self.action_on_match = "update_all"
        return self

    def when_matched_delete(self, *, where: Optional[str] = None) -> "MergeInsertBuilder":
        self.action_on_match = "delete"
        self.matched_delete_predicate = where
        return self

    def when_not_matched_insert_all(self) -> "MergeInsertBuilder":
        self.insert_all = True
        return self

    def when_not_matched_by_source_delete(
        self, *, where: Optional[str] = None
    ) -> "MergeInsertBuilder":
        self.delete_not_matched_by_source = True
        self.not_matched_by_source_predicate = where
        return self

    def has_matched_action(self) -> bool:
        return self.update_all or self.action_on_match == "delete"

    def execute(self, data: Any) -> MergeResult:
        if not self.update_all and not self.insert_all and self.action_on_match == "nothing":
            raise PlanError("merge_insert 至少需要指定一个动作")
        batch = _coerce_to_batch(data, self.dataset.schema)
        return self.dataset._execute_merge(self, batch)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _align_batch_to_schema(batch: RecordBatch, schema: Schema) -> RecordBatch:
    """把 batch 对齐到目标 schema：缺列补 null，多列丢弃（多列已在 merge_schemas 里加了）。"""

    cols: Dict[str, Array] = {}
    for f in schema.fields:
        if f.name in batch:
            arr = batch.column(f.name)
            if arr.type != f.type:
                arr = arr.cast(f.type)
            cols[f.name] = arr
        else:
            cols[f.name] = array_from_pylist(f.type, [None] * batch.num_rows)
    return RecordBatch(schema, cols)


def _split_batch(batch: RecordBatch, params: WriteParams) -> List[RecordBatch]:
    """按 ``max_rows_per_file`` / ``max_bytes_per_file`` 切分。"""

    if batch.num_rows == 0:
        return []
    by_rows = batch.split_by_rows(params.max_rows_per_file)
    out: List[RecordBatch] = []
    for b in by_rows:
        if b.nbytes > params.max_bytes_per_file:
            out.extend(b.split_by_size(params.max_bytes_per_file))
        else:
            out.append(b)
    return [b for b in out if b.num_rows]


def _pad_to_length(arr: Array, n: int) -> Array:
    if len(arr) == n:
        return arr
    if len(arr) > n:
        return arr.slice(0, n)
    pad = array_from_pylist(arr.type, [None] * (n - len(arr)))
    return concat_arrays([arr, pad], arr.type)


def _revalidate_indices(
    indices: Sequence[IndexMetadata], schema: Schema
) -> List[IndexMetadata]:
    """schema 变化后重新评估索引是否仍有效。"""

    out = []
    for i in indices:
        still = all(c in schema for c in i.columns)
        clone = IndexMetadata.from_json(i.to_json())
        clone.valid = still and i.valid
        out.append(clone)
    return out


def _build_fragment_map(
    old: Sequence[Fragment], new: Sequence[Fragment]
) -> Dict[int, int]:
    """压实时的行号重映射：旧片段 id → 新片段 id。

    本实现按"旧片段顺序 → 新片段顺序"贪心对齐（压实是把连续的旧片段
    合并成连续的新片段）。真实 Lance 走的是
    ``DeletionFile`` + ``RowIdLineage`` 那套更完整的行血脉追踪，
    能跨多次压实把老行号一路映射到新行号。
    """

    mapping: Dict[int, int] = {}
    n_old = len(old)
    n_new = len(new)
    if n_old == 0 or n_new == 0:
        return mapping
    for i, f in enumerate(old):
        j = min(int(i * n_new / n_old), n_new - 1)
        mapping[f.id] = new[j].id
    return mapping


def _remap_indices(
    dataset: Dataset, indices: Sequence[IndexMetadata], frag_map: Mapping[int, int]
) -> List[IndexMetadata]:
    """压实之后索引怎么办？三种选择，这里选最保守的一种。"""

    out = []
    for i in indices:
        clone = IndexMetadata.from_json(i.to_json())
        # 行号变了，索引里的 row_id 已经指向旧地址 → 标记失效。
        # 真实 Lance 会尝试"重映射"（把 row_id 换成新片段里的位置），
        # 重映射不了才重建；本实现一律标记失效并在 optimize 里提示重建。
        clone.valid = False
        clone.details = dict(clone.details)
        clone.details["invalidated_by_compaction"] = True
        out.append(clone)
    return out


def _key_tuples(batch: RecordBatch, keys: Sequence[str]) -> List[tuple]:
    cols = [batch.column(k).to_pylist() for k in keys]
    return [tuple(c[i] for c in cols) for i in range(batch.num_rows)]


def _json_safe(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.ndarray,)):
        return obj.tolist()
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def _is_expression_value(v: Any) -> bool:
    """判断 update 的值是"字面量"还是"SQL 表达式"。"""

    return isinstance(v, str) and any(op in v for op in "+-*/") and not v.startswith("'")


def _coerce_to_batch(data: Any, schema: Schema) -> RecordBatch:
    """把各种输入统一成 ``RecordBatch``。"""

    if isinstance(data, RecordBatch):
        return data
    if isinstance(data, dict):
        raise SchemaError(
            "不能从单个字典建表/追加行",
            hint="请传 [{'a': 1}, ...] 这样的列表",
        )
    if hasattr(data, "to_pylist") and hasattr(data, "schema"):
        try:
            return RecordBatch.from_arrow(data, schema=None)
        except Exception:
            pass
    if hasattr(data, "to_dict") and hasattr(data, "columns"):
        return RecordBatch.from_pydict({c: list(data[c]) for c in data.columns})
    if isinstance(data, (list, tuple)):
        rows = list(data)
        if rows and not isinstance(rows[0], Mapping):
            raise SchemaError("行必须是 dict", got=type(rows[0]).__name__)
        return RecordBatch.from_pylist(rows, schema if len(schema) else None, strict=False)
    if hasattr(data, "__iter__"):
        rows = list(data)
        if rows and isinstance(rows[0], Mapping):
            return RecordBatch.from_pylist(rows, schema if len(schema) else None, strict=False)
    raise SchemaError(f"无法把 {type(data).__name__} 转成记录批次")


# ---------------------------------------------------------------------------
# 模块级入口
# ---------------------------------------------------------------------------


def open_dataset(store: ObjectStore, uri: str, **kw: Any) -> Dataset:
    return Dataset.open(store, uri, **kw)


def create_dataset(
    store: ObjectStore, uri: str, data: Any, *, mode: str = "create", schema: Optional[Schema] = None
) -> Dataset:
    batch = _coerce_to_batch(data, schema or Schema([]))
    sch = schema or batch.schema
    ds = Dataset.create(store, uri, sch, mode=mode)
    if batch.num_rows:
        ds.write([batch], mode="append")
    return ds

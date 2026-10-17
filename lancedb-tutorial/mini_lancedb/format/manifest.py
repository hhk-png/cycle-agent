"""版本 manifest —— Lance 数据集的"当前状态"。

一个数据集目录里只有一样东西是"活的"：**版本 manifest**。
它记录了"这个版本由哪些片段组成、schema 是什么、有哪些索引"。
所有别的东西（数据文件、删除文件、索引文件）都是**不可变**的，
一旦写出就再也不改。

这个设计带来三个直接好处：

1. **时间旅行是免费的。** 老 manifest 还在，就还能读老版本 ——
   读 v3 和读 v7 的代码路径完全一样，只是加载的 manifest 不同；
2. **快照隔离是天然的。** 一个读者拿到 manifest 之后，无论写者
   怎么折腾，它看到的世界都不会变；
3. **提交是原子的。** 只要"写出新 manifest"这一步是原子的，
   整个版本切换就是原子的。对象存储上没有事务，但有
   *条件写*（``If-None-Match``）—— 那就是 Lance 的全部并发控制。

**文件名为什么是 "u64::MAX 减版本号"？**

真实 Lance v2 把 manifest 存成 ``_versions/{u64::MAX - version:020}.manifest``。
于是按 key 做**字典序**列举时，**最新的版本排在最前面**（因为
u64::MAX - 大版本 = 小数字 = 小字符串）。这样"列出最近的 N 个版本"
只需要列举前 N 个 key，不用把整个目录拉回来排序 ——
对象存储的 list 是按 key 有序的，这个技巧能把一次 list 的
返回量从 O(所有版本) 降到 O(N)。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..errors import CommitConflict, CorruptedFileError, VersionNotFoundError
from ..fragment import Fragment
from ..index.base import IndexMetadata
from ..schema import Schema
from ..storage.base import ObjectStore

__all__ = [
    "Manifest",
    "MANIFEST_DIR",
    "TRANSACTION_DIR",
    "TAG_DIR",
    "manifest_filename",
    "manifest_path",
    "latest_hint_path",
    "write_manifest_if_absent",
    "read_manifest",
    "read_manifest_at",
    "try_read_latest",
    "list_manifest_versions",
    "write_latest_hint",
    "read_latest_hint",
    "U64_MAX",
]

U64_MAX = 2**64 - 1

MANIFEST_DIR = "_versions"
TRANSACTION_DIR = "_transactions"
TAG_DIR = "_refs/tags"
BRANCH_DIR = "_refs/branches"
INDEX_DIR = "_indices"
DATA_DIR = "data"
DELETION_DIR = "_deletions"

#: 版本号 0 保留给"空数据集"（还没有任何提交）
FIRST_VERSION = 1


def manifest_filename(version: int) -> str:
    """``u64::MAX - version`` 补零到 20 位 —— 让字典序恰好等于时间倒序。"""

    return f"{U64_MAX - int(version):020}.manifest"


def manifest_path(version: int) -> str:
    return f"{MANIFEST_DIR}/{manifest_filename(version)}"


def latest_hint_path() -> str:
    return f"{MANIFEST_DIR}/latest_version_hint.json"


def transaction_path(read_version: int, txn_id: str) -> str:
    return f"{TRANSACTION_DIR}/{read_version}-{txn_id}.txn"


def index_path(name: str) -> str:
    return f"{INDEX_DIR}/{name}"


def tag_path(tag: str) -> str:
    return f"{TAG_DIR}/{tag}.json"


def branch_path(branch: str) -> str:
    return f"{BRANCH_DIR}/{branch}.json"


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


@dataclass
class Manifest:
    """一个版本的完整快照。"""

    version: int
    schema: Schema
    fragments: List[Fragment] = dc_field(default_factory=list)
    indices: List[IndexMetadata] = dc_field(default_factory=list)
    #: 上一版号（用于时间旅行时校验链路完整）
    parent_version: Optional[int] = None
    timestamp: float = dc_field(default_factory=time.time)
    metadata: Dict[str, str] = dc_field(default_factory=dict)
    #: 产生这个版本的写者标识，便于排查"是谁在写"
    writer: str = "mini-lancedb"
    #: 这个版本由哪个事务产生（指向 ``_transactions/`` 里的文件）
    transaction_id: str = ""

    # -- 统计 -------------------------------------------------------------
    @property
    def num_fragments(self) -> int:
        return len(self.fragments)

    @property
    def num_rows(self) -> int:
        return sum(f.num_rows for f in self.fragments)

    @property
    def physical_rows(self) -> int:
        return sum(f.physical_rows for f in self.fragments)

    @property
    def num_deleted_rows(self) -> int:
        return sum(f.num_deleted_rows for f in self.fragments)

    @property
    def size_bytes(self) -> int:
        return sum(f.size_bytes for f in self.fragments)

    def index_by_name(self, name: str) -> Optional[IndexMetadata]:
        for idx in self.indices:
            if idx.name == name:
                return idx
        return None

    def index_for_column(self, column: str) -> List[IndexMetadata]:
        return [i for i in self.indices if column in i.columns]

    # -- 序列化 -----------------------------------------------------------
    def to_json(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "parent_version": self.parent_version,
            "schema": self.schema.to_json(),
            "fragments": [f.to_json() for f in self.fragments],
            "indices": [i.to_json() for i in self.indices],
            "timestamp": self.timestamp,
            "metadata": dict(self.metadata),
            "writer": self.writer,
            "transaction_id": self.transaction_id,
            # 便于人肉核对的自检统计
            "stats": {
                "num_rows": self.num_rows,
                "physical_rows": self.physical_rows,
                "num_deleted_rows": self.num_deleted_rows,
                "num_fragments": self.num_fragments,
                "size_bytes": self.size_bytes,
            },
        }

    def to_bytes(self) -> bytes:
        return json.dumps(self.to_json(), ensure_ascii=False, sort_keys=True).encode("utf-8")

    @staticmethod
    def from_json(obj: Mapping[str, Any]) -> "Manifest":
        return Manifest(
            version=int(obj["version"]),
            schema=Schema.from_json(obj["schema"]),
            fragments=[Fragment.from_json(f) for f in (obj.get("fragments") or [])],
            indices=[IndexMetadata.from_json(i) for i in (obj.get("indices") or [])],
            parent_version=obj.get("parent_version"),
            timestamp=float(obj.get("timestamp", 0.0)),
            metadata={str(k): str(v) for k, v in (obj.get("metadata") or {}).items()},
            writer=str(obj.get("writer", "")),
            transaction_id=str(obj.get("transaction_id", "")),
        )

    @staticmethod
    def from_bytes(data: bytes) -> "Manifest":
        return Manifest.from_json(json.loads(data.decode("utf-8")))

    # -- 便利 -------------------------------------------------------------
    def clone(self, *, version: Optional[int] = None, **changes: Any) -> "Manifest":
        """派生一个新版本 —— 所有写操作都是"复制 manifest + 改一处"。"""

        payload = self.to_json()
        payload.update(
            {
                "version": version if version is not None else self.version + 1,
                "parent_version": self.version,
                "timestamp": time.time(),
            }
        )
        for k, v in changes.items():
            if k == "fragments":
                payload["fragments"] = [f.to_json() for f in v]
            elif k == "indices":
                payload["indices"] = [i.to_json() for i in v]
            elif k == "schema":
                payload["schema"] = v.to_json()
            else:
                payload[k] = v
        return Manifest.from_json(payload)

    def validate(self) -> None:
        ids = [f.id for f in self.fragments]
        if len(set(ids)) != len(ids):
            raise CorruptedFileError("manifest 里出现重复的片段 id", ids=ids)
        for f in self.fragments:
            f.validate()
        names = [i.name for i in self.indices]
        if len(set(names)) != len(names):
            raise CorruptedFileError("manifest 里出现重复的索引名", names=names)

    def describe(self) -> str:
        return (
            f"v{self.version} | {self.num_rows} 行（物理 {self.physical_rows}，"
            f"删 {self.num_deleted_rows}）| {self.num_fragments} 个片段 | "
            f"{len(self.schema)} 列 | {len(self.indices)} 个索引 | "
            f"{self.size_bytes} B"
        )


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------


def write_manifest_if_absent(store: ObjectStore, manifest: Manifest) -> bool:
    """**原子提交**：版本文件不存在才写。返回 ``False`` 表示有人抢先了。

    整个 Lance 的并发控制就压在这一个调用上。S3 上是
    ``PutObject`` + ``If-None-Match: *``；本地文件系统上是 ``O_EXCL``。
    """

    path = manifest_path(manifest.version)
    return store.put_if_not_exists(path, manifest.to_bytes()) is not None


def read_manifest(store: ObjectStore, version: int, *, dataset_uri: str = "") -> Manifest:
    path = manifest_path(version)
    if not store.exists(path):
        raise VersionNotFoundError(
            "版本不存在", version=version, uri=dataset_uri or store.uri
        )
    m = Manifest.from_bytes(store.get(path))
    if m.version != version:
        raise CorruptedFileError(
            "manifest 文件里的版本号与文件名不符",
            filename_version=version,
            manifest_version=m.version,
        )
    return m


def read_manifest_at(store: ObjectStore, ref: Any) -> Manifest:
    """按"版本号 / 标签 / 分支名"读取 manifest。"""

    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        return read_manifest(store, int(ref))
    if isinstance(ref, str):
        tag = resolve_ref(store, ref)
        if tag is not None:
            return read_manifest(store, tag)
    raise VersionNotFoundError("无法解析的版本引用", ref=ref)


def resolve_ref(store: ObjectStore, name: str) -> Optional[int]:
    """把标签/分支名解析成版本号。"""

    for path in (tag_path(name), branch_path(name)):
        if store.exists(path):
            obj = json.loads(store.get(path).decode("utf-8"))
            return int(obj["version"])
    return None


def list_refs(store: ObjectStore, kind: str = "tag") -> Dict[str, Dict[str, Any]]:
    base = TAG_DIR if kind == "tag" else BRANCH_DIR
    out: Dict[str, Dict[str, Any]] = {}
    for m in store.list(base + "/"):
        name = m.path.rsplit("/", 1)[-1]
        if not name.endswith(".json"):
            continue
        obj = json.loads(store.get(m.path).decode("utf-8"))
        out[name[: -len(".json")]] = obj
    return out


def list_manifest_versions(store: ObjectStore) -> List[int]:
    """列出所有版本号，**从新到旧**。

    因为我们用 ``u64::MAX - version`` 命名，字典序列举天然就是
    时间倒序，不需要读任何文件内容。
    """

    out: List[int] = []
    for m in store.list(MANIFEST_DIR + "/"):
        name = m.path.rsplit("/", 1)[-1]
        if not name.endswith(".manifest"):
            continue
        try:
            inv = int(name[: -len(".manifest")])
        except ValueError:
            continue
        out.append(U64_MAX - inv)
    out.sort(reverse=True)
    return out


def read_latest_hint(store: ObjectStore) -> Optional[int]:
    path = latest_hint_path()
    if not store.exists(path):
        return None
    try:
        return int(json.loads(store.get(path).decode("utf-8"))["version"])
    except (KeyError, ValueError, json.JSONDecodeError):
        return None


def write_latest_hint(store: ObjectStore, version: int) -> None:
    """写"最新版本提示"。

    它**不是权威来源**，只是省一次 list 的缓存 —— 对象存储的 list
    既慢又可能最终一致，有了这个提示，打开表的常见路径就是
    一次 GET（40 字节），而不是一次 LIST。

    提示可能过期（比如别人写了新版本还没来得及更新提示），
    所以读取方必须**以 manifest 是否存在为准**做兜底。
    """

    store.put(
        latest_hint_path(),
        json.dumps({"version": version}, sort_keys=True).encode("utf-8"),
    )


def try_read_latest(store: ObjectStore) -> Optional[Manifest]:
    """尽最大努力读出最新 manifest；表不存在返回 ``None``。"""

    hint = read_latest_hint(store)
    if hint is not None and store.exists(manifest_path(hint)):
        return read_manifest(store, hint)
    versions = list_manifest_versions(store)
    if not versions:
        return None
    manifest = read_manifest(store, versions[0])
    # 顺手修正过期提示
    if hint != manifest.version:
        write_latest_hint(store, manifest.version)
    return manifest


def next_version(store: ObjectStore) -> int:
    m = try_read_latest(store)
    return FIRST_VERSION if m is None else m.version + 1


def find_version_by_timestamp(store: ObjectStore, before: float) -> Optional[int]:
    """时间旅行到"某个时刻之前的最新版本"。

    真实 LanceDB 的 ``restore`` 只接受版本号或标签；按时间旅行是
    上层（比如带审计需求的场景）自己维护的映射。
    这里实现它，是因为"按时间点恢复"在实践中非常常用。
    """

    best: Optional[int] = None
    for v in list_manifest_versions(store):
        m = read_manifest(store, v)
        if m.timestamp <= before:
            if best is None or m.timestamp > read_manifest(store, best).timestamp:
                best = v
    return best

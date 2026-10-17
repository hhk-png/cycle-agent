"""事务文件与提交协议。

LanceDB 没有分布式事务，也没有锁服务。它有的只是对象存储的
**条件写**（conditional put）："这个 key 不存在才写成功"。
整个并发控制在三个文件上完成::

    _transactions/{read_version}-{uuid}.txn   ← 意图（可审计、可重放）
    _versions/{u64::MAX-version:020}.manifest ← 状态（唯一权威）
    _versions/latest_version_hint.json        ← 缓存（非权威）

提交一个新版本的完整流程：

1. 记下当前版本 ``v``（**read_version**）；
2. 干正事：写数据文件 / 索引文件（它们都是新的、不可变的，随便写）；
3. 写事务文件 ``_transactions/{v}-{uuid}.txn``，把"我基于 v 做了什么"写清楚；
4. 用条件写尝试创建 ``_versions/{v+1}.manifest``；
5. **成功** → 提交完成，更新 hint；
6. **失败**（说明别人已经提交了 v+1）→ 冲突。此时重新读取最新 manifest，
   判断"我这次操作和别人的操作能不能兼容"，能兼容就基于新版本重做一遍
   （**rebase**），不能就报错让调用方重试。

第 6 步是这个协议里唯一需要动脑的地方。真实 Lance 维护了一张
**冲突矩阵**：两个 append 可以合并；append 与 overwrite 冲突；
两个 delete 可以合并；create_index 与 append 可以共存……
本模块把这张矩阵实现了出来（``can_rebase``）。

**为什么要有事务文件？**
manifest 只保存"结果"，不保存"过程"。事务文件保存了操作语义
（append 了哪些片段、delete 了哪些行、建了什么索引），于是：
多写入者冲突时可以据此判定能否 rebase；
出问题时可以重放事务来重建 manifest；
审计时能回答"这个版本是谁、在什么时候、改了什么"。
"""

from __future__ import annotations

import base64
import json
import time
import uuid as uuid_mod
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..errors import CommitConflict, CorruptedFileError
from ..storage.base import ObjectStore
from .manifest import TRANSACTION_DIR, transaction_path

__all__ = [
    "Transaction",
    "Operation",
    "write_transaction",
    "read_transaction",
    "commit_manifest",
    "can_rebase",
    "CONFLICT_MATRIX",
    "rebase_plan",
]

#: 事务类型。名字和真实 Lance 的 ``Transaction`` protobuf 里的 oneof 字段对应。
class Operation:
    CREATE = "create"
    APPEND = "append"
    OVERWRITE = "overwrite"
    DELETE = "delete"
    UPDATE = "update"
    MERGE_INSERT = "merge_insert"
    CREATE_INDEX = "create_index"
    DROP_INDEX = "drop_index"
    ALTER_SCHEMA = "alter_schema"
    RESTORE = "restore"
    COMPACT = "compact"
    REWRITE = "rewrite"
    TAG = "tag"


#: 冲突矩阵：``(我, 别人) -> 能否 rebase``。
#:
#: 判定原则只有一条：**我的操作是否依赖于我对"别人改了哪些片段"的完整了解？**
#:
#: * 追加数据（append）只新增片段，不碰别人写的片段 → 永远可合并；
#: * 删除/更新/合并插入要改写"某个片段"的删除文件 —— 如果别人
#:   同时也动了同一个片段，两边算出来的删除文件会互相覆盖 → 保守地判冲突；
#:   真实 Lance 的解法是在 rebase 时重新对最新片段列表求一次交集，
#:   本实现保留这个保守版本，并在文档里点明代价；
#: * 建索引不修改任何片段，只往 manifest 的索引列表里加一条 → 可合并；
#: * 覆盖写（overwrite）会重写整个片段列表 → 与任何其它写操作都冲突。
CONFLICT_MATRIX: Dict[Tuple[str, str], bool] = {}


def _fill_matrix() -> None:
    additive = {Operation.APPEND, Operation.CREATE_INDEX, Operation.DROP_INDEX, Operation.TAG}
    mutating = {Operation.DELETE, Operation.UPDATE, Operation.MERGE_INSERT, Operation.COMPACT}
    destructive = {Operation.OVERWRITE, Operation.RESTORE, Operation.REWRITE, Operation.ALTER_SCHEMA}

    for a in (additive | mutating | destructive):
        for b in (additive | mutating | destructive):
            if a in destructive or b in destructive:
                # 破坏性操作会重写片段列表，任何人基于旧列表做的决定都失效
                ok = False
            elif a in additive and b in additive:
                ok = True
            elif a in additive and b in mutating:
                ok = True  # 我追加、别人删 —— 各改各的片段
            elif a in mutating and b in additive:
                ok = True
            else:
                # 两个都改行：同一个片段上可能互相覆盖，保守判冲突
                ok = False
            CONFLICT_MATRIX[(a, b)] = ok


_fill_matrix()


@dataclass
class Transaction:
    """一次提交的完整描述。"""

    #: 我做这次操作时看到的版本号
    read_version: int
    operation: str
    payload: Dict[str, Any] = dc_field(default_factory=dict)
    id: str = ""
    timestamp: float = dc_field(default_factory=time.time)
    writer: str = "mini-lancedb"
    #: 附属二进制（真实 Lance 用它塞新 schema 等）；这里用 base64 存 JSON 里
    blobs: Dict[str, str] = dc_field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid_mod.uuid4().hex[:16]

    @property
    def path(self) -> str:
        return transaction_path(self.read_version, self.id)

    def to_json(self) -> Dict[str, Any]:
        return {
            "read_version": self.read_version,
            "operation": self.operation,
            "payload": self.payload,
            "id": self.id,
            "timestamp": self.timestamp,
            "writer": self.writer,
            "blobs": self.blobs,
        }

    def to_bytes(self) -> bytes:
        return json.dumps(self.to_json(), ensure_ascii=False, sort_keys=True).encode("utf-8")

    @staticmethod
    def from_json(obj: Mapping[str, Any]) -> "Transaction":
        return Transaction(
            read_version=int(obj["read_version"]),
            operation=str(obj["operation"]),
            payload=dict(obj.get("payload") or {}),
            id=str(obj.get("id", "")),
            timestamp=float(obj.get("timestamp", 0.0)),
            writer=str(obj.get("writer", "")),
            blobs={str(k): str(v) for k, v in (obj.get("blobs") or {}).items()},
        )

    @staticmethod
    def from_bytes(data: bytes) -> "Transaction":
        return Transaction.from_json(json.loads(data.decode("utf-8")))

    def describe(self) -> str:
        return f"[{self.read_version}→] {self.operation} by {self.writer} ({self.id})"


def write_transaction(store: ObjectStore, txn: Transaction) -> str:
    """写事务文件。

    用普通 ``put`` 而不是条件写：文件名里带 uuid，天然不会撞；
    而且事务文件**不是权威状态**，就算被覆盖也不影响正确性 ——
    权威永远是 manifest。这种"日志可以丢、状态不能丢"的取舍，
    让它可以在低持久性存储上以更低的成本写入。
    """

    store.put(txn.path, txn.to_bytes())
    return txn.path


def read_transaction(store: ObjectStore, path: str) -> Transaction:
    return Transaction.from_bytes(store.get(path))


def list_transactions(store: ObjectStore) -> List[Transaction]:
    out = []
    for m in store.list(TRANSACTION_DIR + "/"):
        if m.path.endswith(".txn"):
            out.append(read_transaction(store, m.path))
    out.sort(key=lambda t: (t.read_version, t.timestamp))
    return out


# ---------------------------------------------------------------------------
# 提交
# ---------------------------------------------------------------------------


def commit_manifest(store: ObjectStore, manifest: Any) -> bool:
    """尝试把 manifest 提交为新的权威版本。原子性全在这里。"""

    from .manifest import write_manifest_if_absent

    return write_manifest_if_absent(store, manifest)


def can_rebase(mine: str, theirs: str) -> bool:
    """两个并发操作能不能合并。"""

    return CONFLICT_MATRIX.get((mine, theirs), False)


def rebase_plan(mine: str, conflicts: Sequence[str]) -> Dict[str, Any]:
    """给出冲突时的处置建议 —— 报错信息里带上它，用户就知道该怎么办。"""

    blocked = [c for c in conflicts if not can_rebase(mine, c)]
    return {
        "my_operation": mine,
        "conflicting_operations": list(conflicts),
        "can_rebase": not blocked,
        "blocked_by": blocked,
        "advice": (
            "基于最新版本重做本次操作（会自动重试）"
            if not blocked
            else "本次操作与并发写入冲突，请重新读取表状态后重试"
        ),
    }


class CommitError(CommitConflict):
    """带 rebase 建议的提交冲突。"""

    def __init__(self, plan: Mapping[str, Any], attempt: int = 1) -> None:
        super().__init__(
            "提交版本时检测到并发写",
            attempt=attempt,
            **{k: v for k, v in plan.items() if k != "advice"},
        )
        self.plan = dict(plan)


def describe_protocol() -> str:
    """给教程用：把提交协议打印成可读文本。"""

    return (
        "提交协议（乐观并发）：\n"
        "  1. 读 latest manifest，记下 read_version = v\n"
        "  2. 写数据文件 / 索引文件（不可变，随便写）\n"
        "  3. 写 _transactions/{v}-{uuid}.txn 记录意图\n"
        "  4. put_if_not_exists(_versions/{v+1}.manifest)  ← 唯一的原子点\n"
        "  5. 成功则更新 latest_version_hint.json；失败则按冲突矩阵决定 rebase 或报错"
    )

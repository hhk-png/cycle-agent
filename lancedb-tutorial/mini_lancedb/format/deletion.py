"""删除文件（deletion file）。

LanceDB 的删除**不改写数据**。删掉 1000 行里的 3 行，磁盘上做的是：

1. 生成一个删除文件，记录"片段 7 里的第 12、88、901 行被删了"；
2. 在 manifest 里把片段 7 的 ``deletion_file`` 指向这个文件。

于是删除是 **O(删除行数) 的元数据操作**，不是 O(表大小) 的数据操作。
代价是读取时多一层过滤，而且随着删除累积，向量索引里会留下"幽灵行"
—— 所以必须定期 compaction（见 ``mini_lancedb/optimize.py``）。

真实 Lance 的删除文件有两种编码（Arrow IPC 或 bitmap），用扩展名区分，
文件名形如 ``{fragment_id}-{read_version}-{id}.{arrow|bin}``。
其中 ``.arrow`` 那一档按 proto（``fragment_metadata.proto``）的定义
装的是一个 **int32 数组**的行号——注意不是 64 位：
行号是**片段内**的偏移，不是全局行地址，int32 装得下。
（不同来源对符号说法不一，有写 ``Int32Array`` 的、也有写
``UInt32Array`` 的；本教程没有拿真实落盘文件逐字节核对过这一点，
只按 proto 的字段定义记。）

本实现用一种自描述的紧凑二进制：**排序后的行号做差分 + varint**。

为什么是差分 + varint 而不是 bitmap？

* 删除通常是**零星**的（用户删掉某几条），差分后每行号只需 1~2 字节；
* bitmap 是固定 ``num_rows/8`` 字节，删 3 行和删一半一样大；
* 真实 Lance 也正是这么权衡的 —— 稀疏删除走 Arrow 数组那一档，
  效果等价。
"""

from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from ..errors import CorruptedFileError
from ..storage.base import ObjectStore
from .codec_util import varint_decode_list, varint_encode_list

__all__ = [
    "MAGIC",
    "DeletionFile",
    "write_deletion_file",
    "read_deletion_file",
    "deletion_path",
    "merge_deletions",
]

#: 删除文件魔数
MAGIC = b"LDEL"
#: 头部：magic(4) + version(u32) + fragment_id(u64) + num_rows(u64) + num_deleted(u64)
_HEADER = struct.Struct("<4sIQQQ")
HEADER_SIZE = _HEADER.size


@dataclass
class DeletionFile:
    """指向一个删除文件的引用，会被写进 manifest。"""

    #: 片段 id
    fragment_id: int
    #: 引用它的读版本号（用于文件名与 GC 判定）
    read_version: int
    #: 对象存储上的相对路径
    path: str
    #: 删掉的行数（缓存在 manifest 里，省一次读）
    num_deleted_rows: int
    #: 这个片段被删除前的物理行数
    num_rows: int
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid.uuid4().hex[:16]

    @property
    def deleted_fraction(self) -> float:
        return self.num_deleted_rows / self.num_rows if self.num_rows else 0.0

    def to_json(self) -> Dict[str, Any]:
        return {
            "fragment_id": self.fragment_id,
            "read_version": self.read_version,
            "path": self.path,
            "num_deleted_rows": self.num_deleted_rows,
            "num_rows": self.num_rows,
            "id": self.id,
        }

    @staticmethod
    def from_json(obj: Dict[str, Any]) -> "DeletionFile":
        return DeletionFile(
            fragment_id=int(obj["fragment_id"]),
            read_version=int(obj.get("read_version", 0)),
            path=str(obj["path"]),
            num_deleted_rows=int(obj.get("num_deleted_rows", 0)),
            num_rows=int(obj.get("num_rows", 0)),
            id=str(obj.get("id", "")),
        )


def deletion_path(fragment_id: int, read_version: int, file_id: str) -> str:
    """真实 Lance 的命名规则：``_deletions/{frag}-{read_version}-{uuid}.bin``。

    把这三个信息放进文件名不是审美问题：GC 的时候要能**只靠文件名**
    判断哪些删除文件还被引用着，否则就得把全部 manifest 读一遍。
    """

    return f"_deletions/{fragment_id}-{read_version}-{file_id}.bin"


def serialize_deletions(
    fragment_id: int, num_rows: int, deleted_offsets: Sequence[int]
) -> bytes:
    """把行号集合编码成删除文件的字节。"""

    rows = sorted({int(r) for r in deleted_offsets})
    for r in rows:
        if r < 0 or r >= num_rows:
            raise CorruptedFileError(
                "删除的行号越界", row=r, num_rows=num_rows, fragment=fragment_id
            )
    head = _HEADER.pack(MAGIC, 1, fragment_id, num_rows, len(rows))
    if not rows:
        return head
    # 差分：第一个存原值，后面存与前一个的差
    deltas = [rows[0]] + [rows[i] - rows[i - 1] for i in range(1, len(rows))]
    return head + varint_encode_list(deltas)


def deserialize_deletions(data: bytes) -> Tuple[int, int, np.ndarray]:
    """解出 ``(fragment_id, num_rows, 被删的行号数组)``。"""

    if len(data) < HEADER_SIZE:
        raise CorruptedFileError("删除文件太短", size=len(data))
    magic, version, fragment_id, num_rows, num_deleted = _HEADER.unpack_from(data, 0)
    if magic != MAGIC:
        raise CorruptedFileError("删除文件魔数不匹配", found=magic.hex())
    if version != 1:
        raise CorruptedFileError("不支持的删除文件版本", version=version)
    if num_deleted == 0:
        return fragment_id, num_rows, np.zeros(0, dtype=np.int64)
    deltas = varint_decode_list(data[HEADER_SIZE:], num_deleted)
    return fragment_id, num_rows, np.cumsum(np.asarray(deltas, dtype=np.int64))


def write_deletion_file(
    store: ObjectStore,
    fragment_id: int,
    num_rows: int,
    deleted_offsets: Sequence[int],
    read_version: int,
) -> DeletionFile:
    rows = sorted({int(r) for r in deleted_offsets})
    df = DeletionFile(
        fragment_id=fragment_id,
        read_version=read_version,
        path="",
        num_deleted_rows=len(rows),
        num_rows=num_rows,
    )
    df.path = deletion_path(fragment_id, read_version, df.id)
    store.put(df.path, serialize_deletions(fragment_id, num_rows, rows))
    return df


def read_deletion_file(store: ObjectStore, df: DeletionFile) -> np.ndarray:
    """读删除文件，返回片段内的物理行号数组。

    **注意这是"物理行号"而不是"逻辑行号"**：物理行号 = 在数据文件里的
    第几行，即使前面有别的行被删了也不变。逻辑行号（用户视角的第几行）
    需要把删除位图求补之后重新编号 —— 压缩包（compaction）会做这件事。
    """

    data = store.get(df.path)
    fragment_id, num_rows, rows = deserialize_deletions(data)
    if fragment_id != df.fragment_id:
        raise CorruptedFileError(
            "删除文件里的片段 id 与 manifest 不符",
            in_file=fragment_id,
            in_manifest=df.fragment_id,
        )
    return rows


def read_deletion_offsets(store: ObjectStore, df: Optional[DeletionFile], num_rows: int) -> np.ndarray:
    if df is None or df.num_deleted_rows == 0:
        return np.zeros(0, dtype=np.int64)
    return read_deletion_file(store, df)


def deletion_mask(df_rows: np.ndarray, num_rows: int) -> np.ndarray:
    """行号数组 → "哪些行还活着"的布尔掩码（True = 活着）。"""

    alive = np.ones(num_rows, dtype=bool)
    if len(df_rows):
        ok = df_rows[(df_rows >= 0) & (df_rows < num_rows)]
        alive[ok.astype(np.int64)] = False
    return alive


def merge_deletions(a: Sequence[int], b: Sequence[int]) -> np.ndarray:
    """两次删除的并集（同一片段被删两次时用）。"""

    if not len(a):
        return np.asarray(sorted(set(int(x) for x in b)), dtype=np.int64)
    if not len(b):
        return np.asarray(sorted(set(int(x) for x in a)), dtype=np.int64)
    return np.union1d(np.asarray(a, dtype=np.int64), np.asarray(b, dtype=np.int64))


def estimate_deletion_cost(df: Optional[DeletionFile]) -> Dict[str, Any]:
    """评估一个片段的删除比例 —— compaction 要不要合并它就看这个。"""

    if df is None:
        return {"deleted": 0, "rows": 0, "fraction": 0.0, "needs_compaction": False}
    frac = df.deleted_fraction
    return {
        "deleted": df.num_deleted_rows,
        "rows": df.num_rows,
        "fraction": frac,
        # 经验阈值：删掉超过 10% 就值得重写一遍
        "needs_compaction": frac > 0.10,
    }

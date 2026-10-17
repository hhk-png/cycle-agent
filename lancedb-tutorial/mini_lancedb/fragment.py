"""片段（Fragment）与数据文件（DataFile）。

Lance 的数据组织是三层：

* **表** —— 一串片段；
* **片段** —— 一次写入产生的一批行，**不可变**，是版本管理和行号的单位；
* **数据文件** —— 片段内部按"列组"拆出的 ``.lance`` 文件。

"按列组拆文件"这件事值得单独强调，它是 Lance 实现 **免重写的 schema
演进**的关键：

* 老片段里只有一个文件，装着列 ``[a, b, c]``（用 field_id 标记）；
* 现在执行 ``add_columns`` 加了一列 ``d`` —— 不重写老数据，
  而是**新写一个只含 ``d`` 的文件**追加到这个片段上；
* 读的时候按 field_id 对齐：老文件的列 + 新文件的列拼起来，
  老行在新列上就是 null。

于是"加一列"的代价是 O(新列的数据量)，而不是 O(表大小)。
删列同理：把 field_id 从 schema 里摘掉就行，数据文件原样不动。
真正贵的是**改类型**（需要重写那一列）和**删除大量行**（需要 compaction）。

片段还是**行号的来源**：Lance 的"稳定行 id"由 ``(片段 id, 片段内偏移)``
组成，所以片段一旦建成就绝不能被原地修改 —— 这也是"删除用删除文件、
而不改数据文件"的根本原因。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from .errors import CorruptedFileError, SchemaError
from .format.deletion import DeletionFile

__all__ = ["DataFile", "Fragment", "new_fragment_id"]


def new_fragment_id() -> int:
    """片段 id：真实 Lance 用随机 u64（这样多个写入者不会撞 id）。

    用自增计数器在多写入者场景下必然冲突 —— 两个进程各自"下一个是 7"，
    提交时就会互相覆盖。随机 u64 的碰撞概率足够低，配合
    "提交冲突则重试"就够了。
    """

    return int(np.random.default_rng().integers(1, 2**63 - 1, dtype=np.int64))


@dataclass
class DataFile:
    """片段里的一个 ``.lance`` 文件，带它负责哪些列。"""

    #: 对象存储上的相对路径，形如 ``data/{uuid}.lance``
    path: str
    #: 这个文件里装着的顶层字段的 ``field_id`` 列表
    fields: List[int]
    #: 文件里的行数（同一片段的所有文件行数必须一致）
    num_rows: int
    #: 便于诊断：这个文件里实际有哪些列名
    column_names: List[str] = dc_field(default_factory=list)
    #: 文件字节数（从 manifest 直接可读，省一次 head）
    size_bytes: int = 0
    #: 列级统计：``{列名: {min, max, null_count}}``。
    #: **把统计抄进 manifest** 是刻意的：查询规划阶段做剪枝时，
    #: 不打开任何数据文件就能判断"这个片段不可能命中"。
    #: 真实 Lance 也是这么做的（fragment metadata 里带 column stats）。
    column_stats: Dict[str, Dict[str, Any]] = dc_field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "fields": list(self.fields),
            "num_rows": self.num_rows,
            "column_names": list(self.column_names),
            "size_bytes": self.size_bytes,
            "column_stats": {k: dict(v) for k, v in self.column_stats.items()},
        }

    @staticmethod
    def from_json(obj: Mapping[str, Any]) -> "DataFile":
        return DataFile(
            path=str(obj["path"]),
            fields=[int(f) for f in (obj.get("fields") or [])],
            num_rows=int(obj.get("num_rows", 0)),
            column_names=list(obj.get("column_names") or []),
            size_bytes=int(obj.get("size_bytes", 0)),
            column_stats={
                str(k): dict(v) for k, v in (obj.get("column_stats") or {}).items()
            },
        )


@dataclass
class Fragment:
    """一个不可变的数据批次。"""

    id: int
    files: List[DataFile] = dc_field(default_factory=list)
    #: 物理行数 = 写入时的行数（删除不会改变它）
    physical_rows: int = 0
    deletion_file: Optional[DeletionFile] = None
    #: 覆盖这个片段的增量索引版本（预留）
    metadata: Dict[str, Any] = dc_field(default_factory=dict)

    # -- 元信息 -----------------------------------------------------------
    @property
    def num_rows(self) -> int:
        """**逻辑**行数 = 物理行数 - 已删除行数。用户看到的是这个。"""

        return self.physical_rows - self.num_deleted_rows

    @property
    def num_deleted_rows(self) -> int:
        return self.deletion_file.num_deleted_rows if self.deletion_file else 0

    @property
    def size_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)

    @property
    def field_ids(self) -> List[int]:
        out: List[int] = []
        for f in self.files:
            for fid in f.fields:
                if fid not in out:
                    out.append(fid)
        return out

    def file_for_field(self, field_id: int) -> Optional[DataFile]:
        for f in self.files:
            if field_id in f.fields:
                return f
        return None

    def has_field(self, field_id: int) -> bool:
        return any(field_id in f.fields for f in self.files)

    def covers_fields(self, field_ids: Sequence[int]) -> List[int]:
        """哪些字段在这个片段里**没有**数据（需要按 null 补）。"""

        return [fid for fid in field_ids if not self.has_field(fid)]

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"Fragment(id={self.id}, rows={self.num_rows}/{self.physical_rows}, "
            f"files={len(self.files)}, deleted={self.num_deleted_rows})"
        )

    # -- 序列化 -----------------------------------------------------------
    def to_json(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "files": [f.to_json() for f in self.files],
            "physical_rows": self.physical_rows,
            "deletion_file": self.deletion_file.to_json() if self.deletion_file else None,
            "metadata": dict(self.metadata),
        }

    @staticmethod
    def from_json(obj: Mapping[str, Any]) -> "Fragment":
        df = obj.get("deletion_file")
        return Fragment(
            id=int(obj["id"]),
            files=[DataFile.from_json(f) for f in (obj.get("files") or [])],
            physical_rows=int(obj.get("physical_rows", 0)),
            deletion_file=DeletionFile.from_json(df) if df else None,
            metadata=dict(obj.get("metadata") or {}),
        )

    # -- 行号 -------------------------------------------------------------
    def row_id_range(self) -> Tuple[int, int]:
        """这个片段占用的**行地址**区间（不含删除信息）。"""

        return (self.id << 32, (self.id << 32) + self.physical_rows)

    def address_to_row(self, address: int) -> Tuple[int, int]:
        """行地址 → ``(片段 id, 片段内偏移)``。

        真实 Lance 的行地址就是 ``(fragment_id << 32) | offset`` 这样一个
        u64；好处是"行地址 → 位置"是纯算术，不需要查表。
        """

        return (address >> 32, address & 0xFFFFFFFF)

    def row_to_address(self, offset: int) -> int:
        return (self.id << 32) | offset

    def validate(self) -> None:
        if self.physical_rows < 0:
            raise CorruptedFileError("片段行数为负", fragment=self.id)
        counts = {f.num_rows for f in self.files}
        if len(counts) > 1:
            raise CorruptedFileError(
                "同一片段内各文件的行数必须一致",
                fragment=self.id,
                counts=sorted(counts),
            )
        if counts and self.physical_rows and counts != {self.physical_rows}:
            raise CorruptedFileError(
                "片段行数与文件行数不一致",
                fragment=self.id,
                fragment_rows=self.physical_rows,
                file_rows=sorted(counts),
            )
        if self.deletion_file and self.deletion_file.fragment_id != self.id:
            raise CorruptedFileError(
                "删除文件的片段 id 不匹配", fragment=self.id, deletion=self.deletion_file.fragment_id
            )

    @property
    def is_empty(self) -> bool:
        """整个片段被删光了（compaction 应该直接把它扔掉）。"""

        return self.num_rows == 0


def fragment_from_batch_files(
    files: Sequence[DataFile], *, fragment_id: Optional[int] = None
) -> Fragment:
    rows = files[0].num_rows if files else 0
    return Fragment(
        id=fragment_id if fragment_id is not None else new_fragment_id(),
        files=list(files),
        physical_rows=rows,
    )

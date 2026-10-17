"""``mini_lancedb.format`` —— 磁盘格式层。

这一层只关心"字节长什么样"，不关心用户 API：

* :mod:`~mini_lancedb.format.lance_file` —— ``.lance`` 文件容器（读写、页表、footer）
* :mod:`~mini_lancedb.format.manifest` —— 版本 manifest（数据集的"当前状态"）
* :mod:`~mini_lancedb.format.transaction` —— 事务文件与乐观并发提交协议
* :mod:`~mini_lancedb.format.deletion` —— 删除文件（行号差分 + varint）
* :mod:`~mini_lancedb.format.codec_util` —— 元数据层用的小 varint 工具
"""

from .deletion import (
    DeletionFile,
    deletion_mask,
    deletion_path,
    estimate_deletion_cost,
    merge_deletions,
    read_deletion_file,
    write_deletion_file,
)
from .lance_file import (
    FOOTER_SIZE,
    MAGIC,
    FileStatistics,
    LanceFileReader,
    LanceFileWriter,
    ReadStats,
    inspect_lance_file,
    read_lance_file,
    write_lance_file,
)
from .manifest import (
    DELETION_DIR,
    DATA_DIR,
    INDEX_DIR,
    MANIFEST_DIR,
    TAG_DIR,
    TRANSACTION_DIR,
    Manifest,
    list_manifest_versions,
    manifest_filename,
    manifest_path,
    read_latest_hint,
    read_manifest,
    try_read_latest,
    write_latest_hint,
    write_manifest_if_absent,
)
from .transaction import (
    CONFLICT_MATRIX,
    Operation,
    Transaction,
    can_rebase,
    commit_manifest,
    describe_protocol,
    read_transaction,
    write_transaction,
)

__all__ = [
    "MAGIC",
    "FOOTER_SIZE",
    "LanceFileWriter",
    "LanceFileReader",
    "FileStatistics",
    "ReadStats",
    "write_lance_file",
    "read_lance_file",
    "inspect_lance_file",
    "Manifest",
    "manifest_path",
    "manifest_filename",
    "write_manifest_if_absent",
    "read_manifest",
    "try_read_latest",
    "list_manifest_versions",
    "write_latest_hint",
    "read_latest_hint",
    "MANIFEST_DIR",
    "TRANSACTION_DIR",
    "TAG_DIR",
    "INDEX_DIR",
    "DATA_DIR",
    "DELETION_DIR",
    "Transaction",
    "Operation",
    "write_transaction",
    "read_transaction",
    "commit_manifest",
    "can_rebase",
    "CONFLICT_MATRIX",
    "describe_protocol",
    "DeletionFile",
    "write_deletion_file",
    "read_deletion_file",
    "deletion_path",
    "deletion_mask",
    "merge_deletions",
    "estimate_deletion_cost",
]

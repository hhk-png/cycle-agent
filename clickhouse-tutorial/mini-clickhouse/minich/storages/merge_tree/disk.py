"""磁盘抽象 —— ``IDisk`` 与本地文件系统实现。

.. code-block:: text

   ┌──────────────────────────────────────────────────────────────┐
   │                     为什么要有 IDisk 这一层?                  │
   └──────────────────────────────────────────────────────────────┘

   真实 ClickHouse 的表可以建在本地盘、S3、HDFS、Azure Blob 上:

   .. code-block:: sql

      CREATE TABLE t (...) ENGINE = MergeTree ORDER BY id
      SETTINGS disk = disk(type = s3, endpoint = 'https://...');

   而 ``MergeTreeData`` 的代码里**一个字都没改** —— 因为它只跟
   ``IDisk`` 打交道。这条抽象的边界画得非常准:

   .. code-block:: text

     MergeTreeData          IStorage / part 管理 / 后台 merge
         │  write_file(path, data) / read_file(path) / list_dir(path) ...
         ▼
     IDisk                  "一个扁平的、分层的字节存储"
         ├── DiskLocal      local 盘(os.open / os.replace)
         ├── DiskS3         每个文件一次 HTTP PUT
         └── DiskHDFS       HDFS 客户端

   于是"元数据的原子性"这件事被压缩成了**一个原语**:

   .. code-block:: text

     ╔══════════════════════════════════════════════════════════════╗
     ║  atomic_write(path, data)                                    ║
     ║    ① 写 tmp_<随机名>          ← 此时 path 还不存在,读者看不到 ║
     ║    ② os.replace(tmp, path)    ← 同文件系统内**原子**改名     ║
     ╚══════════════════════════════════════════════════════════════╝

   **"读方永远看不到写了一半的 part"这条保证,就是建立在这一条之上。**
   它不是靠"加锁"实现的(锁保护不了崩溃),而是靠"改名是原子的":

   * 一个 part 是由 **几十个文件** 组成的目录,但整个 part 的"可见性"
     只取决于目录名这一个原子操作;
   * 写入方先把 ``tmp_insert_xxx/`` 建好、写满、校验完
     (见 :meth:`minich.storages.merge_tree.part.write_part`),
     最后 ``move(tmp, final_name)`` —— 这一刻之前,任何查询都看不到它;
   * 如果进程在中间崩溃,磁盘上只会留下一个 ``tmp_*`` 目录,启动时
     扫到就删掉(真实 ClickHouse 的 ``detachUnexpectedParts`` 做这件事)。

   .. warning::

      **``os.replace`` 的原子性只保证"同一个文件系统内"**。跨设备
      (比如 tmp 在 ``/tmp``、part 在另一个挂载点)执行 rename 会退化成
      "拷贝+删除",原子性就没了 —— 这也是真实 ClickHouse 要求
      ``tmp_policy``/``storage_policy`` 的磁盘必须在同一卷上的原因。
      本实现把临时文件写在**目标目录的同级**(``<parent>/tmp_*``),
      从而始终落在同一个文件系统里。

   .. note::

      mini-clickhouse 只实现 ``DiskLocal``。``DiskS3``/``DiskHDFS`` 的
      接口一样,但需要网络依赖,不适合教学实现。``IDisk`` 的接口
      故意做成**同步阻塞**的 —— 真实 ClickHouse 用 ``AsyncReadBuffer``
      把 IO 放进线程池,那是为了打满带宽,与我们讲的存储语义无关。
"""

from __future__ import annotations

import os
import shutil
import threading
import uuid as _uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple

from ...errors import ErrorCode, raise_error

__all__ = ["IDisk", "DiskLocal", "DiskSpace", "get_available_space"]


# ══════════════════════════════════════════════════════════════════════════
# 磁盘空间
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class DiskSpace:
    """磁盘空间信息 —— ``system.disks`` 的每一行。

    真实 ClickHouse 用 ``keep_free_space_bytes`` 给每块盘留一条"安全水位":
    磁盘使用量超过 ``(capacity - keep_free_space_bytes)`` 时,后台 merge
    会**主动停下**(``failpoint``/``reserve`` 机制),避免把系统盘写满
    导致整个进程崩掉。这是生产环境里最容易被忽略、后果最严重的一个设置。
    """

    name: str
    path: str
    #: 总容量(字节)
    capacity: int
    #: 已用(字节)
    used: int
    #: 给这块盘留的"保命空间"(字节)—— 真实 CH 默认 ``0`` 但强烈建议设置
    keep_free_space_bytes: int = 0

    @property
    def available(self) -> int:
        """可用空间 —— **已经扣掉了保命水位**。"""
        return max(0, self.capacity - self.used - self.keep_free_space_bytes)

    def can_allocate(self, bytes_: int) -> bool:
        return bytes_ <= self.available

    def describe(self) -> str:
        from ...util.strings import format_readable_size

        return (
            f"{self.name} ({self.path}): "
            f"{format_readable_size(self.capacity)} total, "
            f"{format_readable_size(self.used)} used, "
            f"{format_readable_size(self.available)} available"
        )


def get_available_space(path: str) -> Tuple[int, int, int]:
    """返回 ``(total, used, free)`` 字节数。

    ``shutil.disk_usage`` 在 Linux 上是 ``statvfs``、Windows 上是
    ``GetDiskFreeSpaceEx`` —— 两者都返回"整个文件系统的用量",
    而不是"某个目录的用量"(目录用量要靠自己累加文件大小)。
    真实 ClickHouse 的 ``DiskLocal::getDiskSpace`` 同理。
    """
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        # 路径还不存在时返回全零 —— 让调用方知道"拿不到",而不是崩
        return 0, 0, 0
    return usage.total, usage.used, usage.free


# ══════════════════════════════════════════════════════════════════════════
# 接口
# ══════════════════════════════════════════════════════════════════════════


class IDisk(ABC):
    """一个**扁平的**分层字节存储 —— 与真实 ClickHouse 的 ``IDisk`` 同名。

    与 ``IStorage`` 的区别值得强调:

    * ``IStorage`` 懂"表、part、列"这些概念;
    * ``IDisk`` 只懂"路径、文件、目录"——它**不知道 part 是什么**。

    这个"无知"是刻意的:正因为 ``IDisk`` 不懂 part,换一块盘
    (本地 → S3)才不需要改存储引擎的代码。

    ``path`` 一律是**相对路径**(相对于盘的根)。这样同一份逻辑可以
    跑在 ``/var/lib/clickhouse/data/`` 下,也可以跑在 ``s3://bucket/data/`` 下。
    """

    #: 盘的名字 —— ``system.disks.name``
    name: str = "default"

    # ── 读 ──────────────────────────────────────────────────────────────

    @abstractmethod
    def read_file(self, path: str) -> bytes:
        """把整个文件读进内存。

        .. note::

           真实 ClickHouse 读 part 文件用的是 ``ReadBuffer`` + 随机 seek
           (它只需要读一个 granule 的字节,不读整个文件)。mini-clickhouse
           的 part 文件都不大,读整个文件更简单,而且**语义等价**
           —— 差的是 IO 量,不是正确性。第 05 章会量化这个差别。
        """

    def read_file_into(self, path: str, out: bytearray) -> None:
        """把文件内容**追加**到 ``out`` —— 避免一次临时 bytes 分配。"""
        out += self.read_file(path)

    # ── 写 ──────────────────────────────────────────────────────────────

    @abstractmethod
    def write_file(self, path: str, data: bytes) -> None:
        """整文件写入(覆盖已有内容)。"""

    @abstractmethod
    def append_file(self, path: str, data: bytes) -> None:
        """追加写入(文件不存在就创建)。

        .. warning::

           **追加不是原子的。** 真实 ClickHouse 只在两种场合用追加:
           ``.log`` 系列表引擎、以及 ``ReplicatedMergeTree`` 的
           ``log`` 里追加操作。part 的写入**永远**走
           :meth:`atomic_write`,不走追加 —— 因为一个 part 必须是
           "要么完整可见、要么不存在"的。
        """

    @abstractmethod
    def atomic_write(self, path: str, data: bytes) -> None:
        """**原子**写文件 —— 本模块最重要的一个方法。

        实现等价于::

            tmp = <parent>/tmp_<random>
            write(tmp, data)
            os.replace(tmp, path)      # 原子

        保证:任何时刻从 ``path`` 读,拿到的要么是"旧的完整内容",
        要么是"新的完整内容",**绝不会有中间态**。
        """

    # ── 元信息 ──────────────────────────────────────────────────────────

    @abstractmethod
    def exists(self, path: str) -> bool:
        ...

    @abstractmethod
    def exists_directory(self, path: str) -> bool:
        ...

    @abstractmethod
    def list_dir(self, path: str) -> List[str]:
        """列出目录下的名字(不递归,顺序**已排序**以保证确定性)。"""

    @abstractmethod
    def file_size(self, path: str) -> int:
        ...

    @abstractmethod
    def create_dir(self, path: str) -> None:
        ...

    @abstractmethod
    def remove_file(self, path: str) -> None:
        ...

    @abstractmethod
    def remove_dir(self, path: str) -> None:
        """递归删目录(part 是一个目录)。"""

    @abstractmethod
    def move(self, from_path: str, to_path: str) -> None:
        """改名/移动 —— 对 part 来说就是"提交"。"""

    @abstractmethod
    def is_directory_empty(self, path: str) -> bool:
        ...

    # ── 空间 ────────────────────────────────────────────────────────────

    def get_disk_space(self) -> DiskSpace:
        total, used, _free = get_available_space(self.get_root_path())
        return DiskSpace(self.name, self.get_root_path(), total, used)

    def get_root_path(self) -> str:
        return ""

    def get_name(self) -> str:
        return self.name

    # ── 临时文件路径 ────────────────────────────────────────────────────

    def temp_path_in(self, directory: str, prefix: str = "tmp") -> str:
        """在 ``directory`` **同级**生成一个临时路径。

        必须与目标在同一个文件系统内,``os.replace`` 才原子。
        用随机后缀避免并发写冲突(真实 CH 用 ``ThreadLocal`` 计数器 +
        ``tmp_`` 前缀,见 ``IDisk::getTemporaryPath``)。
        """
        parent = _dirname(directory)
        return _join(parent, f"{prefix}_{_uuid.uuid4().hex[:16]}")


# ══════════════════════════════════════════════════════════════════════════
# 本地文件系统实现
# ══════════════════════════════════════════════════════════════════════════


class DiskLocal(IDisk):
    """本地文件系统盘 —— 生产里 99% 的 ClickHouse 用的就是它。

    ``root_path`` 是这块盘的根:所有 ``path`` 参数都相对于它。真实
    ClickHouse 建盘时会把 ``root_path`` 规范化(去掉末尾斜杠、
    检查是否存在、必要时创建),并对 **符号链接逃逸** 做检查 ——
    ``DiskLocal::isPathInsideRoot`` 会拒绝 ``../../etc/passwd``。
    这个检查不是洁癖:如果 part 名字能被用户影响,不检查就会变成
    路径穿越漏洞。
    """

    def __init__(self, root_path: str, name: str = "default") -> None:
        self.root_path = os.path.abspath(root_path)
        self.name = name
        self._lock = threading.RLock()
        self._tmp_counter = 0
        os.makedirs(self.root_path, exist_ok=True)

    # ── 路径 ────────────────────────────────────────────────────────────

    def get_root_path(self) -> str:
        return self.root_path

    def full_path(self, path: str) -> str:
        """把相对路径拼成绝对路径,**并检查没有逃出根目录**。"""
        if not path:
            return self.root_path
        p = path.replace("\\", "/")
        joined = os.path.abspath(os.path.join(self.root_path, p.lstrip("/")))
        root = self.root_path
        if joined != root and not joined.startswith(root + os.sep):
            raise_error(
                ErrorCode.ACCESS_DENIED,
                f"Path {path!r} escapes the disk root {root!r}",
            )
        return joined

    # ── 读 ──────────────────────────────────────────────────────────────

    def read_file(self, path: str) -> bytes:
        with open(self.full_path(path), "rb") as f:
            return f.read()

    # ── 写 ──────────────────────────────────────────────────────────────

    def write_file(self, path: str, data: bytes) -> None:
        full = self.full_path(path)
        _ensure_parent(full)
        with open(full, "wb") as f:
            f.write(data)

    def append_file(self, path: str, data: bytes) -> None:
        full = self.full_path(path)
        _ensure_parent(full)
        with open(full, "ab") as f:
            f.write(data)

    def atomic_write(self, path: str, data: bytes) -> None:
        full = self.full_path(path)
        parent = os.path.dirname(full)
        os.makedirs(parent, exist_ok=True)
        with self._lock:
            self._tmp_counter += 1
            tmp = os.path.join(parent, f"tmp_{_uuid.uuid4().hex[:12]}")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, full)  # ← 原子:在此之前 full 是旧内容或不存在

    # ── 元信息 ──────────────────────────────────────────────────────────

    def exists(self, path: str) -> bool:
        return os.path.isfile(self.full_path(path))

    def exists_directory(self, path: str) -> bool:
        return os.path.isdir(self.full_path(path))

    def list_dir(self, path: str) -> List[str]:
        full = self.full_path(path)
        try:
            names = os.listdir(full)
        except FileNotFoundError:
            return []
        return sorted(names)

    def list_dir_full(self, path: str) -> List[str]:
        """列出目录下的**完整相对路径**(临时路径的父目录也在这里)。"""
        return [_join(path, n) for n in self.list_dir(path)]

    def file_size(self, path: str) -> int:
        try:
            return os.path.getsize(self.full_path(path))
        except OSError:
            return 0

    def create_dir(self, path: str) -> None:
        os.makedirs(self.full_path(path), exist_ok=True)

    def remove_file(self, path: str) -> None:
        try:
            os.remove(self.full_path(path))
        except FileNotFoundError:
            pass

    def remove_dir(self, path: str) -> None:
        full = self.full_path(path)
        if os.path.isdir(full):
            shutil.rmtree(full, ignore_errors=True)

    def move(self, from_path: str, to_path: str) -> None:
        src, dst = self.full_path(from_path), self.full_path(to_path)
        _ensure_parent(dst)
        if os.path.exists(dst):
            # 真实 CH 在 RENAME 到已存在目标时会报错;我们保守地先删目标,
            # 这样"重写同一个 part"是可重入的(测试里会反复跑到)。
            if os.path.isdir(dst):
                shutil.rmtree(dst, ignore_errors=True)
            else:
                os.remove(dst)
        os.replace(src, dst)

    def is_directory_empty(self, path: str) -> bool:
        try:
            return not os.listdir(self.full_path(path))
        except FileNotFoundError:
            return True

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DiskLocal {self.name} root={self.root_path}>"


# ── 路径小工具(全部用 ``/``,Windows 上 os 会自动转换)──────────────────


def _join(a: str, b: str) -> str:
    if not a:
        return b
    if a.endswith("/"):
        return a + b
    return a + "/" + b


def _dirname(path: str) -> str:
    path = path.replace("\\", "/")
    idx = path.rfind("/")
    return path[:idx] if idx > 0 else ""


def _ensure_parent(full_path: str) -> None:
    parent = os.path.dirname(full_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

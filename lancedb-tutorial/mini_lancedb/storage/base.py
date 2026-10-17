"""对象存储抽象层。

LanceDB 最反直觉的一个设计决定是：**它不假设自己有一个本地磁盘**。
整个数据集就是一堆按 key 寻址的不可变对象，读写全部经过一个"对象存储"
接口。这个抽象带来三个直接后果：

1. 本地目录、S3、GCS、Azure、HTTP 只读镜像，走的是同一条代码路径；
2. 写操作必须假设"只有原子性、没有事务" —— 提交只能靠
   *条件写*（``put_if_not_exists``）实现"抢到就是我的"；
3. 读放大是真金白银：一次查询要发几次 GET、拿多少字节，
   是可以被数出来、也必须要被优化的。

本模块定义这个接口，并提供三种实现：

* :class:`InMemoryStore` —— 测试用，最快；
* :class:`LocalFileStore` —— 落到本地目录，行为和 S3 一致（key 里的 ``/``
  会变成目录分隔符，但**不保证目录语义**）；
* :class:`S3LikeStore` —— 在 :class:`InMemoryStore` 之上模拟对象存储的
  那些"坑"：list 的最终一致、条件写的原子性、没有真正的重命名。
"""

from __future__ import annotations

import abc
import os
import posixpath
import threading
import time
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from ..errors import CommitConflict, StoreError

__all__ = [
    "ObjectMeta",
    "ObjectStore",
    "StoreStats",
    "InMemoryStore",
    "LocalFileStore",
    "S3LikeStore",
    "InstrumentedStore",
]


@dataclass(frozen=True)
class ObjectMeta:
    """一个对象的元信息。对象存储的 ``list`` 通常只返回这个，不返回内容。"""

    path: str
    size: int
    #: 对象存储上常见的 ETag / 版本号。Lance 用它做条件写的判断。
    etag: str = ""
    last_modified: float = 0.0

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.path} ({self.size} B)"


@dataclass
class StoreStats:
    """I/O 计数器。

    几乎每一章性能分析都要用它 —— "这个查询到底发了几次请求、
    读了多少字节"是判断有没有走索引、有没有下推的最硬证据。
    """

    gets: int = 0
    puts: int = 0
    deletes: int = 0
    lists: int = 0
    heads: int = 0
    bytes_read: int = 0
    bytes_written: int = 0
    #: 按路径前缀累计的读取次数，用来定位"谁被反复读"
    get_by_prefix: Dict[str, int] = dc_field(default_factory=dict)

    def snapshot(self) -> Dict[str, int]:
        return {
            "gets": self.gets,
            "puts": self.puts,
            "deletes": self.deletes,
            "lists": self.lists,
            "heads": self.heads,
            "bytes_read": self.bytes_read,
            "bytes_written": self.bytes_written,
        }

    def reset(self) -> None:
        self.gets = self.puts = self.deletes = self.lists = self.heads = 0
        self.bytes_read = self.bytes_written = 0
        self.get_by_prefix.clear()

    def __str__(self) -> str:  # pragma: no cover
        s = self.snapshot()
        return (
            "I/O: {gets} GET / {puts} PUT / {lists} LIST / {deletes} DELETE; "
            "读 {bytes_read} B 写 {bytes_written} B".format(**s)
        )


class ObjectStore(abc.ABC):
    """LanceDB 依赖的最小对象存储接口。

    只要求 7 个方法。凡是能提供这 7 个方法的存储，理论上都能跑 LanceDB
    —— 这也是"LanceDB 无服务器"这句话的全部技术依据。
    """

    #: 实现是否支持"不存在才写"（条件写）。没有它就只能靠外部锁。
    supports_conditional_put: bool = True

    def __init__(self, uri: str = "") -> None:
        self.uri = uri
        self.stats = StoreStats()

    # -- 读 ---------------------------------------------------------------
    @abc.abstractmethod
    def get(self, path: str) -> bytes:
        """读整个对象。小文件（manifest、索引元数据）用得最多。"""

    @abc.abstractmethod
    def get_range(self, path: str, offset: int, length: int) -> bytes:
        """范围读。**列式格式的性能命脉**：只读需要的页，不读整个文件。"""

    # -- 写 ---------------------------------------------------------------
    @abc.abstractmethod
    def put(self, path: str, data: bytes) -> str:
        """整对象写，返回 etag。覆盖写是允许的（所以不能拿它做并发控制）。"""

    def put_if_not_exists(self, path: str, data: bytes) -> Optional[str]:
        """不存在才写，返回 etag；已存在则返回 ``None``。

        这是整个 LanceDB **唯一的原子提交原语**。
        S3 上是 ``PutObject`` 带 ``If-None-Match: *``；
        GCS 上是 ``ifGenerationMatch=0``；本地文件系统上是 ``O_EXCL``。
        """

        if self.head(path) is not None:
            return None
        return self.put(path, data)

    @abc.abstractmethod
    def delete(self, path: str) -> None:
        """删除对象。对象存储上"删除目录"要靠 list + 逐个删。"""

    # -- 元信息 -----------------------------------------------------------
    @abc.abstractmethod
    def head(self, path: str) -> Optional[ObjectMeta]:
        """取元信息；不存在返回 ``None``。"""

    @abc.abstractmethod
    def list(self, prefix: str = "", *, recursive: bool = True) -> List[ObjectMeta]:
        """按前缀列举。

        对象存储的 list 是它有别于文件系统的地方：没有"目录"，
        只有按 key 排序的扁平命名空间，``a/b`` 只是一个恰好含 ``/`` 的 key。
        """

    # -- 便利方法 ---------------------------------------------------------
    def exists(self, path: str) -> bool:
        return self.head(path) is not None

    def size(self, path: str) -> int:
        m = self.head(path)
        if m is None:
            raise StoreError("对象不存在", path=path, store=self.uri)
        return m.size

    def get_text(self, path: str, encoding: str = "utf-8") -> str:
        return self.get(path).decode(encoding)

    def put_text(self, path: str, text: str, encoding: str = "utf-8") -> str:
        return self.put(path, text.encode(encoding))

    def delete_prefix(self, prefix: str) -> int:
        """删除一个"目录"。返回删掉的对象数。"""

        victims = [m.path for m in self.list(prefix, recursive=True)]
        for p in victims:
            self.delete(p)
        return len(victims)

    def child_paths(self, prefix: str) -> List[str]:
        """列举某个前缀下的"直接子项"（去重后的下一级路径段）。"""

        prefix = prefix.rstrip("/") + "/" if prefix else ""
        out = set()
        for m in self.list(prefix, recursive=True):
            rest = m.path[len(prefix) :]
            if not rest:
                continue
            head = rest.split("/", 1)[0]
            out.add(prefix + head + ("/" if "/" in rest else ""))
        return sorted(out)

    def join(self, *parts: str) -> str:
        """对象 key 一律用 ``/`` 拼接 —— 哪怕跑在 Windows 上。

        这是有意的：如果本地实现用 ``os.sep``，同一份数据在 Windows 上
        和 Linux 上的 key 就不一样，manifest 里记录的路径会失效。
        """

        return "/".join(p.strip("/") for p in parts if p not in ("", None))

    def __repr__(self) -> str:  # pragma: no cover
        return f"{type(self).__name__}({self.uri!r})"


# ---------------------------------------------------------------------------
# 内存实现
# ---------------------------------------------------------------------------


class InMemoryStore(ObjectStore):
    """字典实现。测试的默认后端。"""

    def __init__(self, uri: str = "memory://") -> None:
        super().__init__(uri)
        self._objects: Dict[str, bytes] = {}
        self._lock = threading.RLock()

    def get(self, path: str) -> bytes:
        with self._lock:
            try:
                data = self._objects[path]
            except KeyError:
                raise StoreError("对象不存在", path=path, store=self.uri) from None
        self.stats.gets += 1
        self.stats.bytes_read += len(data)
        self._count_prefix(path)
        return data

    def get_range(self, path: str, offset: int, length: int) -> bytes:
        return self.get(path)[offset : offset + length]

    def put(self, path: str, data: bytes) -> str:
        if not isinstance(data, (bytes, bytearray)):
            raise StoreError("put 只接受 bytes", type=type(data).__name__)
        with self._lock:
            self._objects[path] = bytes(data)
        self.stats.puts += 1
        self.stats.bytes_written += len(data)
        return _etag(data)

    def put_if_not_exists(self, path: str, data: bytes) -> Optional[str]:
        with self._lock:
            if path in self._objects:
                return None
            self._objects[path] = bytes(data)
        self.stats.puts += 1
        self.stats.bytes_written += len(data)
        return _etag(data)

    def delete(self, path: str) -> None:
        with self._lock:
            self._objects.pop(path, None)
        self.stats.deletes += 1

    def head(self, path: str) -> Optional[ObjectMeta]:
        self.stats.heads += 1
        with self._lock:
            data = self._objects.get(path)
        if data is None:
            return None
        return ObjectMeta(path, len(data), _etag(data), time.time())

    def list(self, prefix: str = "", *, recursive: bool = True) -> List[ObjectMeta]:
        self.stats.lists += 1
        with self._lock:
            items = list(self._objects.items())
        out = []
        for k, v in sorted(items):
            if not k.startswith(prefix):
                continue
            if not recursive:
                rest = k[len(prefix) :].lstrip("/")
                if "/" in rest:
                    continue
            out.append(ObjectMeta(k, len(v), _etag(v), time.time()))
        return out

    def snapshot_bytes(self) -> Dict[str, bytes]:
        """给测试用的内容快照。"""

        with self._lock:
            return dict(self._objects)

    def _count_prefix(self, path: str) -> None:
        key = path.split("/", 1)[0] if "/" in path else path
        self.stats.get_by_prefix[key] = self.stats.get_by_prefix.get(key, 0) + 1


def _etag(data: bytes) -> str:
    """便宜的伪 ETag：长度 + 简单哈希。对象存储用的是 MD5，我们只需要唯一性。"""

    h = 0
    for b in data[:512]:
        h = (h * 31 + b) & 0xFFFFFFFF
    return f"{len(data):x}-{h:08x}"


# ---------------------------------------------------------------------------
# 本地文件系统
# ---------------------------------------------------------------------------


class LocalFileStore(ObjectStore):
    """把本地目录当成"对象存储"用。

    刻意**不做**任何目录语义的假设：key 里的 ``/`` 直接映射成路径分隔符，
    但 list 是通过遍历实现的；也没有"空目录"这个概念。
    这样写出来的代码在本地和 S3 上行为才一致 —— 这正是 Lance 团队强调的
    "local-first, cloud-ready"。
    """

    def __init__(self, uri: str, *, create: bool = True) -> None:
        super().__init__(uri)
        self.root = os.path.abspath(uri)
        if create:
            os.makedirs(self.root, exist_ok=True)

    # -- 路径 -------------------------------------------------------------
    def _full(self, path: str) -> str:
        clean = path.replace("\\", "/").strip("/")
        if clean.startswith("../") or "/../" in clean:
            raise StoreError("key 里不允许出现 ..", path=path)
        return os.path.join(self.root, *clean.split("/")) if clean else self.root

    def _key(self, full: str) -> str:
        return os.path.relpath(full, self.root).replace("\\", "/")

    # -- 读 ---------------------------------------------------------------
    def get(self, path: str) -> bytes:
        full = self._full(path)
        try:
            with open(full, "rb") as fh:
                data = fh.read()
        except FileNotFoundError:
            raise StoreError("对象不存在", path=path, store=self.uri) from None
        self.stats.gets += 1
        self.stats.bytes_read += len(data)
        return data

    def get_range(self, path: str, offset: int, length: int) -> bytes:
        full = self._full(path)
        try:
            with open(full, "rb") as fh:
                fh.seek(offset)
                data = fh.read(length)
        except FileNotFoundError:
            raise StoreError("对象不存在", path=path, store=self.uri) from None
        self.stats.gets += 1
        self.stats.bytes_read += len(data)
        return data

    # -- 写 ---------------------------------------------------------------
    def put(self, path: str, data: bytes) -> str:
        full = self._full(path)
        os.makedirs(os.path.dirname(full) or self.root, exist_ok=True)
        # 先写临时文件再 rename：避免读者看到写了一半的文件
        tmp = full + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, full)
        self.stats.puts += 1
        self.stats.bytes_written += len(data)
        return _etag(data)

    def put_if_not_exists(self, path: str, data: bytes) -> Optional[str]:
        full = self._full(path)
        os.makedirs(os.path.dirname(full) or self.root, exist_ok=True)
        # O_EXCL 是本地文件系统上的"条件写"，等价于 S3 的 If-None-Match: *
        try:
            fd = os.open(full, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return None
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        self.stats.puts += 1
        self.stats.bytes_written += len(data)
        return _etag(data)

    def delete(self, path: str) -> None:
        full = self._full(path)
        try:
            os.remove(full)
        except FileNotFoundError:
            pass
        self.stats.deletes += 1

    # -- 元信息 -----------------------------------------------------------
    def head(self, path: str) -> Optional[ObjectMeta]:
        self.stats.heads += 1
        full = self._full(path)
        try:
            st = os.stat(full)
        except FileNotFoundError:
            return None
        return ObjectMeta(self._key(full), st.st_size, f"mtime-{int(st.st_mtime)}", st.st_mtime)

    def list(self, prefix: str = "", *, recursive: bool = True) -> List[ObjectMeta]:
        self.stats.lists += 1
        out: List[ObjectMeta] = []
        base = self._full(prefix)
        walk_root = base if os.path.isdir(base) else os.path.dirname(base)
        if not os.path.isdir(walk_root):
            return out
        prefix_key = prefix.strip("/")
        for dirpath, dirnames, filenames in os.walk(walk_root):
            dirnames[:] = [d for d in dirnames if not d.endswith(".tmp")]
            for fn in filenames:
                if fn.endswith(".tmp"):
                    continue
                full = os.path.join(dirpath, fn)
                key = self._key(full)
                if prefix_key and not key.startswith(prefix_key):
                    continue
                if not recursive:
                    rest = key[len(prefix_key) :].lstrip("/")
                    if "/" in rest:
                        continue
                try:
                    st = os.stat(full)
                except FileNotFoundError:
                    continue
                out.append(ObjectMeta(key, st.st_size, f"mtime-{int(st.st_mtime)}", st.st_mtime))
        out.sort(key=lambda m: m.path)
        return out


# ---------------------------------------------------------------------------
# 模拟对象存储
# ---------------------------------------------------------------------------


class S3LikeStore(InMemoryStore):
    """刻意保留对象存储"坏脾气"的模拟器。

    三处不一样，都是有教学目的的：

    1. **没有原子重命名**：``rename`` 直接抛异常，逼着上层用"写新对象 +
       更新指针"的方式提交 —— 这就是 manifest 存在的原因；
    2. **list 有延迟**：写入后立刻 list 可能看不到（最终一致），
       Lance 因此必须靠 manifest 而不是靠 list 来判断"有哪些数据文件"；
    3. **条件写是原子的**：这是对象存储唯一给得起的并发原语。
    """

    def __init__(self, uri: str = "s3://demo", *, list_delay_writes: int = 0) -> None:
        super().__init__(uri)
        self._pending_list: List[str] = []
        self.list_delay_writes = list_delay_writes
        #: 记录一次"提交冲突"供测试断言
        self.conflicts = 0

    @property
    def supports_conditional_put(self) -> bool:
        return True

    def put(self, path: str, data: bytes) -> str:
        etag = super().put(path, data)
        self._pending_list.append(path)
        return etag

    def list(self, prefix: str = "", *, recursive: bool = True) -> List[ObjectMeta]:
        # 模拟"刚写进去的还没出现在 list 里"
        hidden = set(self._pending_list[: self.list_delay_writes])
        self._pending_list = self._pending_list[self.list_delay_writes :]
        return [m for m in super().list(prefix, recursive=recursive) if m.path not in hidden]

    def rename(self, src: str, dst: str) -> None:
        raise StoreError(
            "对象存储没有原子重命名",
            src=src,
            dst=dst,
            hint="用 put + 更新 manifest 的方式提交，不要 rename",
        )

    def conditional_commit(self, path: str, data: bytes, *, expected_etag: Optional[str]) -> str:
        """演示"乐观并发"：只有当目标还是我看到的那个版本时，才允许覆盖。"""

        cur = self.head(path)
        cur_etag = cur.etag if cur else None
        if cur_etag != expected_etag:
            self.conflicts += 1
            raise CommitConflict(
                "版本已被其他写入者推进",
                path=path,
                expected=expected_etag,
                actual=cur_etag,
            )
        return self.put(path, data)


class InstrumentedStore(ObjectStore):
    """给任意 store 套一层 I/O 计量。

    性能章节的所有数字都出自这里。用法::

        store = InstrumentedStore(LocalFileStore("./db"))
        ...
        print(store.stats)
        with store.measure() as m:
            table.search([...]).limit(10).to_list()
        print(m.summary())
    """

    def __init__(self, inner: ObjectStore) -> None:
        super().__init__(inner.uri)
        self.inner = inner
        self._events: List[Tuple[str, str, int]] = []

    # 计量同时打到内层和自身，方便"只看这一段"地重置
    def get(self, path: str) -> bytes:
        data = self.inner.get(path)
        self._record("GET", path, len(data))
        return data

    def get_range(self, path: str, offset: int, length: int) -> bytes:
        data = self.inner.get_range(path, offset, length)
        self._record("GET_RANGE", path, len(data))
        return data

    def put(self, path: str, data: bytes) -> str:
        etag = self.inner.put(path, data)
        self._record("PUT", path, len(data))
        return etag

    def put_if_not_exists(self, path: str, data: bytes) -> Optional[str]:
        etag = self.inner.put_if_not_exists(path, data)
        self._record("PUT_IF_NOT_EXISTS", path, len(data))
        return etag

    def delete(self, path: str) -> None:
        self.inner.delete(path)
        self._record("DELETE", path, 0)

    def head(self, path: str) -> Optional[ObjectMeta]:
        m = self.inner.head(path)
        self._record("HEAD", path, 0)
        return m

    def list(self, prefix: str = "", *, recursive: bool = True) -> List[ObjectMeta]:
        items = self.inner.list(prefix, recursive=recursive)
        self._record("LIST", prefix, sum(i.size for i in items))
        return items

    @property
    def supports_conditional_put(self) -> bool:
        return self.inner.supports_conditional_put

    def _record(self, op: str, path: str, nbytes: int) -> None:
        self._events.append((op, path, nbytes))
        self.stats.lists += op == "LIST"
        self.stats.puts += op in ("PUT", "PUT_IF_NOT_EXISTS")
        self.stats.deletes += op == "DELETE"
        self.stats.heads += op == "HEAD"
        if op in ("GET", "GET_RANGE"):
            self.stats.gets += 1
            self.stats.bytes_read += nbytes
            key = path.split("/")[0]
            self.stats.get_by_prefix[key] = self.stats.get_by_prefix.get(key, 0) + 1
        if op in ("PUT", "PUT_IF_NOT_EXISTS"):
            self.stats.bytes_written += nbytes

    # -- 分析 -------------------------------------------------------------
    @property
    def events(self) -> List[Tuple[str, str, int]]:
        return list(self._events)

    def reset_events(self) -> None:
        self._events.clear()
        self.stats.reset()

    def io_report(self, top: int = 12) -> str:
        """按对象聚合的 I/O 报告 —— 定位"谁被读爆了"最快的手段。"""

        agg: Dict[str, List[int]] = {}
        ops: Dict[str, str] = {}
        for op, path, n in self._events:
            key = _generalize_path(path)
            rec = agg.setdefault(key, [0, 0])
            rec[0] += 1
            rec[1] += n
            ops.setdefault(key, op)
        rows = sorted(agg.items(), key=lambda kv: (-kv[1][1], kv[0]))[:top]
        lines = [f"{'对象':<52}{'次数':>6}{'字节':>12}"]
        for key, (cnt, nb) in rows:
            lines.append(f"{key:<52}{cnt:>6}{nb:>12}")
        lines.append(
            f"合计 {self.stats.gets} 次读 / {self.stats.bytes_read} 字节，"
            f"{self.stats.puts} 次写 / {self.stats.bytes_written} 字节"
        )
        return "\n".join(lines)


def _generalize_path(path: str) -> str:
    """把带长哈希/版本号的文件名收敛成模式，便于聚合统计。"""

    parts = path.split("/")
    out = []
    for p in parts:
        if len(p) > 40 and all(c in "0123456789abcdef-" for c in p.lower()):
            out.append("<hash>")
        elif p.isdigit():
            out.append("<n>")
        else:
            out.append(p)
    return "/".join(out)

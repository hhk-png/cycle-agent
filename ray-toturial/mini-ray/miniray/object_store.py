"""对象存储 —— mini-ray 的「Plasma」。

Ray 里,`ray.put()` 的返回值、每个 task 的返回值,都存在**对象存储**里,
而不是在进程间直接传。这个设计是整个 Ray 性能模型的基础:

* **不可变** —— 对象一旦写入就不能改,所以可以安全地被任意多个进程共享;
* **零拷贝** —— 同一节点上的多个 worker 拿到的是**同一块共享内存的视图**,
  ``ray.get`` 一个 1GB 的 numpy 数组不会产生 1GB 的拷贝;
* **引用计数 + 溢出** —— 内存不够时,先把对象 spill 到磁盘,实在不行才驱逐。

mini-ray 实现了前两条 + 第三条的简化版:

.. code-block:: text

   共享内存段(每节点若干个,按 2 的幂分桶)
   ┌──────────────────────────────────────────────────────────┐
   │ bucket=4KB 段: [obj1][obj2][free][obj3]      ▲ bump 分配 │
   │ bucket=1MB 段: [       obj4          ][free]            │
   │ bucket=64MB 段:[              obj5                    ] │
   └──────────────────────────────────────────────────────────┘
        ▲                                    ▲
        │ driver 进程创建                     │ worker 进程按名字 attach
        │ (raylet 持有)                       │ np.ndarray(buffer=shm.buf, offset=…)
        └────────────────────────────────────┘
                    同一份物理内存,零拷贝

关键的安全机制(和真实 Ray 一致,少了它就会 segfault):

1. **只读视图**。``ray.get`` 拿到的 numpy 数组是**只读**的(``writeable=False``)。
   Ray 的行为就是这样 —— 对象存储里的对象是不可变的,想改请先 ``.copy()``。
2. **pin / unpin**。只要还有进程持有指向某块共享内存的 numpy 视图,这块内存
   **既不能被驱逐也不能被 spill**(否则视图就悬空了)。mini-ray 用
   ``weakref.finalize`` 挂在数组上,数组被 GC 时自动 unpin —— 所以 pin 的
   生命周期跟着**数组**,而不是跟着 ObjectRef(这点和 Ray 的 ``PlasmaBuffer`` 一致)。
3. **写满时的顺序**:先回收 refcount=0 的对象 → 再 spill 无 pin 的对象 →
   都做不到就抛 :class:`ObjectStoreFullError`(绝不静默丢数据)。
"""

from __future__ import annotations

import atexit
import os
import pickle
import shutil
import struct
import tempfile
import threading
import time
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field
from multiprocessing import shared_memory
from typing import Any, Callable, Dict, List, Optional, Tuple

from .errors import ObjectStoreFullError, RaySystemError

try:  # numpy 是可选的:没有它就没有零拷贝快路径,但功能仍然完整
    import numpy as _np
except ImportError:  # pragma: no cover
    _np = None  # type: ignore[assignment]

__all__ = [
    "ObjectStore",
    "SharedMemoryAllocator",
    "BlockHandle",
    "create_ndarray_view",
    "object_store_stats",
    "DEFAULT_OBJECT_STORE_BYTES",
]

#: 默认对象存储容量。Ray 默认取「机器内存的 30%」上限约 2GB 左右,这里给一个
#: 保守值:既能装下教程里的数据,又能让「溢出/驱逐」在测试里被触发。
DEFAULT_OBJECT_STORE_BYTES = 512 * 1024 * 1024

#: 小于这个大小的对象不做共享内存零拷贝(不值得,元数据比数据还大)
_SHM_THRESHOLD = 4096

#: 分配对齐(64 字节,numpy 与 SIMD 都舒服)
_ALIGN = 64

_MIN_BUCKET_SHIFT = 12  # 4KB
_MAX_BUCKET_SHIFT = 28  # 256MB
_SLOTS_PER_SEGMENT = 8

#: 是否允许零拷贝视图可写(默认关闭 —— 打开就等着数据被意外改坏)
_ALLOW_ZERO_COPY_WRITES = os.environ.get("MINIRAY_ALLOW_ZERO_COPY_WRITES") == "1"


# ---------------------------------------------------------------------------
# 共享内存段的进程内缓存
# ---------------------------------------------------------------------------
#
# numpy 数组一旦建在 shm.buf 上,就必须保证 SharedMemory 对象活着:
# SharedMemory.close() 会 release 掉 memoryview,而有导出 buffer 时 release
# 会抛 BufferError。所以这里把「本进程打开过的所有段」缓存起来,活到进程退出。

_SHM_CACHE: Dict[str, shared_memory.SharedMemory] = {}
_SHM_LOCK = threading.Lock()


def _open_shm(name: str) -> shared_memory.SharedMemory:
    with _SHM_LOCK:
        shm = _SHM_CACHE.get(name)
        if shm is None:
            shm = shared_memory.SharedMemory(name=name)
            _SHM_CACHE[name] = shm
        return shm


def _safe_close_shm(shm: shared_memory.SharedMemory) -> None:
    """关闭并(尽力)删除一个共享内存段。

    有 numpy 视图活着时 ``release()`` 会抛 ``BufferError`` —— 这正是我们要的
    保护(它保证视图不会悬空)。这里吞掉它并继续尝试 unlink。
    """
    try:
        shm.close()
    except BufferError:
        pass
    except Exception:  # pragma: no cover
        return
    try:
        shm.unlink()
    except (FileNotFoundError, OSError):
        pass


@atexit.register
def _close_all_shm() -> None:  # pragma: no cover - 进程退出路径
    with _SHM_LOCK:
        for shm in list(_SHM_CACHE.values()):
            _safe_close_shm(shm)
        _SHM_CACHE.clear()


# ---------------------------------------------------------------------------
# 分配器
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlockHandle:
    """一块共享内存的「句柄」—— 可以安全地跨进程传递(它只是名字 + 偏移)。

    ``size`` 是逻辑大小(调用方要的字节数);实际占用是 ``bucket`` 对应的
    2 的幂(分桶分配,和 Plasma 一样不追求零内部碎片)。
    """

    shm_name: str
    offset: int
    size: int
    bucket: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "shm": self.shm_name,
            "offset": self.offset,
            "nbytes": self.size,
            "bucket": self.bucket,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "BlockHandle":
        return BlockHandle(d["shm"], int(d["offset"]), int(d["nbytes"]), int(d["bucket"]))


class SharedMemoryAllocator:
    """按 2 的幂分桶的共享内存分配器(Plasma 简化版)。

    每个 bucket 有一个「当前段」,用 bump 指针线性分配;释放的槽进 free list
    复用。段装不下就再开一段。这套策略的好处是实现极小、分配是 O(1),
    代价是有内部碎片 —— Plasma 也有,而且实测够用。
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled and _np is not None
        self._buckets: Dict[int, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._segments: List[shared_memory.SharedMemory] = []
        self.bytes_allocated = 0
        self.num_segments = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @staticmethod
    def _bucket_for(size: int) -> int:
        shift = max(_MIN_BUCKET_SHIFT, (size - 1).bit_length())
        return min(shift, _MAX_BUCKET_SHIFT)

    def _new_segment(self, bucket: int) -> Dict[str, Any]:
        seg_bytes = max(1 << 20, (1 << bucket) * _SLOTS_PER_SEGMENT)
        shm = shared_memory.SharedMemory(create=True, size=seg_bytes)
        self._segments.append(shm)
        self.num_segments += 1
        return {"shm": shm, "offset": 0, "free": [], "size": seg_bytes}

    def allocate(self, size: int) -> Optional[BlockHandle]:
        if not self._enabled:
            return None
        size = max(1, int(size))
        bucket = self._bucket_for(size)
        slot = 1 << bucket
        with self._lock:
            state = self._buckets.get(bucket)
            if state is None:
                state = self._new_segment(bucket)
                self._buckets[bucket] = state
            if state["free"]:
                offset = state["free"].pop()
            else:
                offset = state["offset"]
                if offset + slot > state["size"]:
                    state = self._new_segment(bucket)
                    self._buckets[bucket] = state
                    offset = 0
                state["offset"] = offset + slot
            self.bytes_allocated += slot
            return BlockHandle(state["shm"].name, offset, size, bucket)

    def free(self, handle: BlockHandle) -> None:
        if not self._enabled:
            return
        with self._lock:
            state = self._buckets.get(handle.bucket)
            if state is None or state["shm"].name != handle.shm_name:
                # 属于旧段(段被换掉了):整段会在 close 时一起释放
                self.bytes_allocated -= 1 << handle.bucket
                return
            state["free"].append(handle.offset)
            self.bytes_allocated -= 1 << handle.bucket

    def close(self) -> None:
        with self._lock:
            for shm in self._segments:
                _safe_close_shm(shm)
            self._segments.clear()
            self._buckets.clear()
            self.bytes_allocated = 0


# ---------------------------------------------------------------------------
# numpy 视图
# ---------------------------------------------------------------------------


def _can_use_shm(value: Any) -> bool:
    return (
        _np is not None
        and isinstance(value, _np.ndarray)
        and value.dtype.hasobject is False
        and value.flags.c_contiguous
        and value.nbytes >= _SHM_THRESHOLD
    )


def ndarray_descriptor(array: Any, block: BlockHandle) -> Dict[str, Any]:
    """numpy 数组的「数据描述符」—— 足以在另一个进程里重建视图。"""
    return {
        "kind": "ndarray",
        "shm": block.shm_name,
        "offset": block.offset,
        "nbytes": array.nbytes,
        "bucket": block.bucket,
        "dtype": array.dtype.str,  # 用 str(dtype) 保留字节序信息
        "shape": tuple(int(x) for x in array.shape),
        "readonly": not _ALLOW_ZERO_COPY_WRITES,
    }


def create_ndarray_view(descriptor: Dict[str, Any], *, writeable: Optional[bool] = None):
    """在**当前进程**里,从描述符重建一个指向共享内存的 numpy 视图(零拷贝)。

    这个函数可以在任意进程调用 —— 这正是零拷贝的落地点:
    数据一直躺在共享内存里,大家只是各自建了个「窗口」。
    """
    if _np is None:  # pragma: no cover - 无 numpy 时不会走到这里
        raise RaySystemError("当前环境没有安装 numpy,无法创建零拷贝视图")
    shm = _open_shm(descriptor["shm"])
    arr = _np.ndarray(
        shape=tuple(descriptor["shape"]),
        dtype=_np.dtype(descriptor["dtype"]),
        buffer=shm.buf,
        offset=int(descriptor["offset"]),
    )
    ro = descriptor.get("readonly", True)
    if writeable is not None:
        ro = not writeable
    if ro:
        arr.flags.writeable = False
    return arr


# ---------------------------------------------------------------------------
# 对象条目
# ---------------------------------------------------------------------------


@dataclass
class ObjectEntry:
    """对象存储里的一条记录。"""

    object_id: bytes
    size: int
    created_at: float
    last_access: float
    #: 存储形态:``plain``(小对象,直接放内存)、``block``(共享内存)之一
    plain: Optional[bytes] = None
    block: Optional[BlockHandle] = None
    descriptor: Optional[Dict[str, Any]] = None
    spill_path: Optional[str] = None
    #: 引用计数(集群范围内还有多少 ObjectRef 指向它)
    ref_count: int = 0
    #: 「谁 pin 了它」—— owner(worker id)→ 次数。零拷贝视图活着时必须 pin。
    pins: Dict[str, int] = field(default_factory=dict)
    #: 统计用
    num_spills: int = 0

    @property
    def in_memory(self) -> bool:
        return self.plain is not None or self.block is not None

    @property
    def pin_count(self) -> int:
        return sum(self.pins.values())

    def kind(self) -> str:
        return "ndarray" if self.descriptor is not None else "bytes"


# ---------------------------------------------------------------------------
# 对象存储(单节点)
# ---------------------------------------------------------------------------

_SPILL_HEADER = struct.Struct("!I")


class ObjectStore:
    """一个节点上的对象存储。

    线程安全(raylet 里的多个 handler 线程会并发访问)。
    """

    def __init__(
        self,
        node_id: str,
        *,
        capacity_bytes: int = DEFAULT_OBJECT_STORE_BYTES,
        spill_dir: Optional[str] = None,
        enable_spilling: bool = True,
        allocator: Optional[SharedMemoryAllocator] = None,
    ) -> None:
        self.node_id = node_id
        self.capacity_bytes = int(capacity_bytes)
        self.enable_spilling = bool(enable_spilling)
        self._spill_dir = spill_dir
        self._owns_spill_dir = spill_dir is None
        self._allocator = allocator or SharedMemoryAllocator()
        self._entries: "OrderedDict[bytes, ObjectEntry]" = OrderedDict()
        self._lock = threading.RLock()
        self._used = 0
        self._closed = False
        # 统计
        self.num_objects_added = 0
        self.num_objects_deleted = 0
        self.num_objects_spilled = 0
        self.num_objects_restored = 0
        self.num_objects_evicted = 0
        self.num_objects_reclaimed = 0
        self.num_full_errors = 0

    # ------------------------------------------------------------- 基本属性
    @property
    def used_bytes(self) -> int:
        return self._used

    def _touch(self, entry: ObjectEntry) -> None:
        entry.last_access = time.time()
        # LRU 顺序:访问过的挪到队尾
        self._entries.move_to_end(entry.object_id)

    def contains(self, object_id: bytes) -> bool:
        with self._lock:
            return object_id in self._entries

    def num_objects(self) -> int:
        with self._lock:
            return len(self._entries)

    def object_ids(self) -> List[bytes]:
        with self._lock:
            return list(self._entries.keys())

    # ---------------------------------------------------------------- 写入
    def put_bytes(self, object_id: bytes, data: bytes) -> None:
        """写入一个「已序列化」的对象。

        大对象会走共享内存(避免后续跨进程读时的拷贝),小对象直接放内存。
        """
        payload = bytes(data)
        block = None
        descriptor = None
        with self._lock:
            self._require_space(len(payload), object_id)
            if self._allocator.enabled and len(payload) >= _SHM_THRESHOLD:
                block = self._allocator.allocate(len(payload))
        if block is not None:
            shm = _open_shm(block.shm_name)
            shm.buf[block.offset : block.offset + len(payload)] = payload
            descriptor = {
                "kind": "bytes",
                "shm": block.shm_name,
                "offset": block.offset,
                "nbytes": len(payload),
                "bucket": block.bucket,
            }
            payload = b""  # 数据已经在共享内存里了,内存里不再留一份
        with self._lock:
            self._insert(object_id, size=len(data), plain=payload if block is None else None,
                         block=block, descriptor=descriptor)

    def put_ndarray(self, object_id: bytes, array: Any) -> None:
        """零拷贝写入一个 numpy 数组:数据直接 memcpy 进共享内存。"""
        if _np is None or not isinstance(array, _np.ndarray):  # pragma: no cover
            raise RaySystemError("put_ndarray 需要 numpy 数组")
        nbytes = int(array.nbytes)
        if not _can_use_shm(array):
            # 太小/非连续/含对象:退化成普通序列化路径
            self.put_bytes(object_id, pickle.dumps(array, protocol=5))
            return
        with self._lock:
            self._require_space(nbytes, object_id)
            block = self._allocator.allocate(nbytes)
        if block is None:  # pragma: no cover - 分配器被禁用
            self.put_bytes(object_id, pickle.dumps(array, protocol=5))
            return
        shm = _open_shm(block.shm_name)
        dest = _np.ndarray(array.shape, array.dtype, buffer=shm.buf, offset=block.offset)
        dest[:] = array  # 唯一的一次 memcpy;之后所有读者都是零拷贝
        descriptor = ndarray_descriptor(array, block)
        with self._lock:
            self._insert(object_id, size=nbytes, plain=None, block=block, descriptor=descriptor)

    def allocate_block(self, size: int) -> Optional[BlockHandle]:
        """给 worker 预留一块共享内存(worker 直接把结果写进去,省一次拷贝)。"""
        with self._lock:
            return self._allocator.allocate(size)

    def commit_block(self, object_id: bytes, block: BlockHandle, descriptor: Dict[str, Any]) -> None:
        """worker 已经把数据写进 block,这里只做登记。"""
        with self._lock:
            self._require_space(int(block.size), object_id)
            self._insert(object_id, size=int(block.size), plain=None, block=block, descriptor=descriptor)

    def release_block(self, block: BlockHandle) -> None:
        """worker 分配了 block 但没用上(执行失败等),归还给分配器。"""
        self._allocator.free(block)

    def _insert(
        self,
        object_id: bytes,
        *,
        size: int,
        plain: Optional[bytes],
        block: Optional[BlockHandle],
        descriptor: Optional[Dict[str, Any]] = None,
    ) -> None:
        old = self._entries.pop(object_id, None)
        if old is not None:
            self._used -= old.size
            self._free_storage(old)
        entry = ObjectEntry(
            object_id=object_id,
            size=size,
            created_at=time.time(),
            last_access=time.time(),
            plain=plain,
            block=block,
            descriptor=descriptor,
            ref_count=1,  # 创建者天然持有一个引用(引用计数从 1 开始)
        )
        self._entries[object_id] = entry
        self._used += size
        self.num_objects_added += 1

    # ---------------------------------------------------------------- 读取
    def get_bytes(self, object_id: bytes) -> bytes:
        """取出对象的原始字节(必要的话先恢复/重算可用性检查)。"""
        with self._lock:
            entry = self._lookup(object_id)
            self._touch(entry)
            if entry.spill_path is not None:
                self._restore_locked(entry)
            if entry.plain is not None:
                return entry.plain
            if entry.descriptor is not None and entry.descriptor.get("kind") == "bytes":
                shm = _open_shm(entry.descriptor["shm"])
                off, n = entry.descriptor["offset"], entry.descriptor["nbytes"]
                return bytes(shm.buf[off : off + n])
            # ndarray 被当成字节读:调用方要的是 pickle 之后的字节
            raise RaySystemError(f"对象 {object_id.hex()[:8]} 是 ndarray,请用 get_descriptor")

    def get_descriptor(self, object_id: bytes) -> Optional[Dict[str, Any]]:
        """返回 ndarray 的数据描述符(可跨进程重建视图);非 ndarray 返回 None。"""
        with self._lock:
            entry = self._lookup(object_id)
            self._touch(entry)
            if entry.spill_path is not None:
                self._restore_locked(entry)
            if entry.descriptor is None or entry.descriptor.get("kind") != "ndarray":
                return None
            return dict(entry.descriptor)

    def is_ndarray(self, object_id: bytes) -> bool:
        with self._lock:
            entry = self._entries.get(object_id)
            return entry is not None and entry.descriptor is not None and entry.descriptor.get("kind") == "ndarray"

    def get_serialized(self, object_id: bytes) -> bytes:
        """ndarray 也能以字节形式取出(pickle 一遍,用于需要传输的场景)。"""
        with self._lock:
            entry = self._entries.get(object_id)
        if entry is not None and entry.descriptor is not None and entry.descriptor.get("kind") == "ndarray":
            view = self.get_ndarray(object_id)
            return pickle.dumps(view, protocol=5)
        return self.get_bytes(object_id)

    def get_ndarray(self, object_id: bytes):
        """取出 numpy 视图(零拷贝;只读)。"""
        with self._lock:
            entry = self._lookup(object_id)
            self._touch(entry)
            if entry.spill_path is not None:
                self._restore_locked(entry)
            if entry.descriptor is None or entry.descriptor.get("kind") != "ndarray":
                raise RaySystemError(f"对象 {object_id.hex()[:8]} 不是 ndarray")
            return create_ndarray_view(entry.descriptor)

    def _lookup(self, object_id: bytes) -> ObjectEntry:
        entry = self._entries.get(object_id)
        if entry is None:
            raise KeyError(object_id)
        return entry

    # ------------------------------------------------------- 引用计数 / pin
    def add_ref(self, object_id: bytes, count: int = 1) -> None:
        with self._lock:
            entry = self._entries.get(object_id)
            if entry is not None:
                entry.ref_count += count

    def remove_ref(self, object_id: bytes, count: int = 1) -> None:
        with self._lock:
            entry = self._entries.get(object_id)
            if entry is not None:
                entry.ref_count = max(0, entry.ref_count - count)

    def set_ref_count(self, object_id: bytes, count: int) -> None:
        """把引用计数**设为**绝对值(而不是增减)。

        raylet 侧的账本是「每个 worker 各持几份引用」,它算好总数之后用这个方法
        压给对象存储 —— 存储据此决定对象能不能被驱逐(``ref_count == 0`` 才回收)。
        这里用**绝对值**而不是增量,是因为两边可能漏掉若干次增量(worker 猝死),
        绝对值会自我纠正,增量不会。
        """
        with self._lock:
            entry = self._entries.get(object_id)
            if entry is not None:
                entry.ref_count = max(0, int(count))

    def pin(self, object_id: bytes, owner: str) -> None:
        """登记一个 pin。只要还有 pin,这个对象就不能被驱逐/溢出。"""
        with self._lock:
            entry = self._entries.get(object_id)
            if entry is None:
                raise KeyError(object_id)
            entry.pins[owner] = entry.pins.get(owner, 0) + 1

    def unpin(self, object_id: bytes, owner: str) -> None:
        with self._lock:
            entry = self._entries.get(object_id)
            if entry is None:
                return
            remaining = entry.pins.get(owner, 0) - 1
            if remaining > 0:
                entry.pins[owner] = remaining
            else:
                entry.pins.pop(owner, None)

    def release_owner(self, owner: str) -> int:
        """某个 worker 死了:释放它持有的全部 pin。返回释放的对象数。"""
        released = 0
        with self._lock:
            for entry in self._entries.values():
                if owner in entry.pins:
                    entry.pins.pop(owner, None)
                    released += 1
        return released

    def ref_counts(self) -> Dict[bytes, int]:
        with self._lock:
            return {oid: e.ref_count for oid, e in self._entries.items()}

    # ---------------------------------------------------------------- 容量
    def _require_space(self, extra: int, object_id: bytes) -> None:
        """确保还能放下 ``extra`` 字节,否则先腾地方。"""
        if extra > self.capacity_bytes:
            raise ObjectStoreFullError(
                f"对象 {object_id.hex()[:8]} 大小 {extra} 超过整个对象存储容量 "
                f"{self.capacity_bytes};请调大 object_store_memory 或把数据切小"
            )
        self._enforce_capacity(extra)

    def _enforce_capacity(self, extra: int = 0) -> None:
        """按「回收 → 溢出 → 报错」的顺序腾空间。

        顺序很重要:

        1. 先回收 **refcount=0**(没人再引用)且没被 pin 的对象 —— 这些对象
           用户已经拿不到了,丢掉完全安全;
        2. 再 spill 无 pin 的对象 —— 数据搬到磁盘,逻辑上还在,下次访问恢复;
        3. 还不行就抛 :class:`ObjectStoreFullError`。
           绝不驱逐有 pin 的对象:有进程正拿着指向它的 numpy 视图。
        """
        attempts = 0
        while self._used + extra > self.capacity_bytes:
            attempts += 1
            if attempts > 100000:  # pragma: no cover - 防御死循环
                break
            if not self._evict_one(reclaim_only=True):
                if not self.enable_spilling or not self._evict_one(reclaim_only=False):
                    self.num_full_errors += 1
                    raise ObjectStoreFullError(
                        f"节点 {self.node_id} 对象存储已满:已用 {self._used} / 容量 "
                        f"{self.capacity_bytes},还需要 {extra} 字节。"
                        "可能原因:有对象被零拷贝视图 pin 住(把用完的 numpy 数组 del 掉),"
                        "或者 object_store_memory 配小了;也可以开启 spill(object_spilling 默认开)。"
                    )

    def _evict_one(self, *, reclaim_only: bool) -> bool:
        """按 LRU 挑一个牺牲者。``reclaim_only=True`` 时只回收无引用的对象。"""
        for object_id, entry in list(self._entries.items()):
            if entry.pin_count > 0:
                continue  # 有零拷贝视图活着,不能动
            if reclaim_only:
                if entry.ref_count > 0:
                    continue
                self._delete_locked(entry)
                self.num_objects_reclaimed += 1
                return True
            if entry.spill_path is None:
                self._spill_locked(entry)
                return True
        return False

    # ---------------------------------------------------------------- 溢出
    def _ensure_spill_dir(self) -> str:
        if self._spill_dir is None:
            self._spill_dir = tempfile.mkdtemp(prefix="miniray-spill-")
        os.makedirs(self._spill_dir, exist_ok=True)
        return self._spill_dir

    def _spill_locked(self, entry: ObjectEntry) -> None:
        """把对象写到磁盘,并释放它占的内存。

        格式:``[4 字节 header 长度][pickle header][payload]``。
        header 里存的是「怎么把这堆字节还原成一个对象」。
        """
        if not self.enable_spilling:
            raise ObjectStoreFullError("对象溢出(spilling)已被禁用")
        spill_dir = self._ensure_spill_dir()
        path = os.path.join(spill_dir, f"{entry.object_id.hex()}.obj")

        if entry.descriptor is not None and entry.descriptor["kind"] == "ndarray":
            view = self.get_ndarray(entry.object_id)
            header = {"kind": "ndarray", "dtype": view.dtype.str, "shape": tuple(view.shape)}
            payload = view.tobytes()
        elif entry.plain is not None:
            header = {"kind": "bytes"}
            payload = entry.plain
        else:
            shm = _open_shm(entry.descriptor["shm"])
            off, n = entry.descriptor["offset"], entry.descriptor["nbytes"]
            header = {"kind": "bytes"}
            payload = bytes(shm.buf[off : off + n])

        header_bytes = pickle.dumps(header, protocol=5)
        with open(path, "wb") as fh:
            fh.write(_SPILL_HEADER.pack(len(header_bytes)))
            fh.write(header_bytes)
            fh.write(payload)

        self._free_storage(entry)
        self._used -= entry.size
        entry.spill_path = path
        entry.num_spills += 1
        self.num_objects_spilled += 1

    def _restore_locked(self, entry: ObjectEntry) -> None:
        """把溢出到磁盘的对象读回内存(必要时先腾空间)。"""
        path = entry.spill_path
        assert path is not None
        with open(path, "rb") as fh:
            (header_len,) = _SPILL_HEADER.unpack(fh.read(_SPILL_HEADER.size))
            header = pickle.loads(fh.read(header_len))
            payload = fh.read()
        self._require_space(len(payload), entry.object_id)

        if header["kind"] == "ndarray" and _np is not None:
            array = _np.frombuffer(payload, dtype=_np.dtype(header["dtype"])).reshape(header["shape"])
            block = self._allocator.allocate(array.nbytes)
            if block is not None:
                shm = _open_shm(block.shm_name)
                dest = _np.ndarray(array.shape, array.dtype, buffer=shm.buf, offset=block.offset)
                dest[:] = array
                entry.block = block
                entry.plain = None
                entry.descriptor = ndarray_descriptor(array, block)
            else:  # pragma: no cover - 分配器禁用
                entry.plain = pickle.dumps(array, protocol=5)
                entry.descriptor = None
        else:
            block = self._allocator.allocate(len(payload))
            if block is not None:
                shm = _open_shm(block.shm_name)
                shm.buf[block.offset : block.offset + len(payload)] = payload
                entry.block = block
                entry.plain = None
                entry.descriptor = {
                    "kind": "bytes",
                    "shm": block.shm_name,
                    "offset": block.offset,
                    "nbytes": len(payload),
                    "bucket": block.bucket,
                }
            else:  # pragma: no cover
                entry.plain = payload
                entry.descriptor = None

        self._used += entry.size
        entry.spill_path = None
        try:
            os.remove(path)
        except OSError:  # pragma: no cover
            pass
        self.num_objects_restored += 1

    # ---------------------------------------------------------------- 删除
    def delete(self, object_id: bytes) -> bool:
        with self._lock:
            entry = self._entries.pop(object_id, None)
            if entry is None:
                return False
            self._used -= entry.size
            self._free_storage(entry)
            self.num_objects_deleted += 1
            return True

    def _delete_locked(self, entry: ObjectEntry) -> None:
        self._entries.pop(entry.object_id, None)
        self._used -= entry.size
        self._free_storage(entry)
        self.num_objects_deleted += 1
        self.num_objects_evicted += 1

    def _free_storage(self, entry: ObjectEntry) -> None:
        if entry.block is not None:
            self._allocator.free(entry.block)
            entry.block = None
        entry.plain = None
        entry.descriptor = None
        if entry.spill_path is not None:
            try:
                os.remove(entry.spill_path)
            except OSError:  # pragma: no cover
                pass
            entry.spill_path = None

    # ---------------------------------------------------------------- 观测
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            spilled = sum(1 for e in self._entries.values() if e.spill_path is not None)
            pinned = sum(1 for e in self._entries.values() if e.pin_count > 0)
            return {
                "node_id": self.node_id,
                "num_objects": len(self._entries),
                "used_bytes": self._used,
                "capacity_bytes": self.capacity_bytes,
                "utilization": round(self._used / self.capacity_bytes, 4) if self.capacity_bytes else 0.0,
                "num_spilled": spilled,
                "num_pinned": pinned,
                "shm_bytes_allocated": self._allocator.bytes_allocated,
                "shm_segments": self._allocator.num_segments,
                "counters": {
                    "added": self.num_objects_added,
                    "deleted": self.num_objects_deleted,
                    "spilled": self.num_objects_spilled,
                    "restored": self.num_objects_restored,
                    "evicted": self.num_objects_evicted,
                    "reclaimed": self.num_objects_reclaimed,
                    "full_errors": self.num_full_errors,
                },
            }

    def close(self) -> None:
        """关闭存储:释放所有对象、共享内存与临时目录。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for entry in list(self._entries.values()):
                self._free_storage(entry)
            self._entries.clear()
            self._used = 0
            self._allocator.close()
            if self._owns_spill_dir and self._spill_dir and os.path.isdir(self._spill_dir):
                shutil.rmtree(self._spill_dir, ignore_errors=True)


def object_store_stats(stores: Dict[str, ObjectStore]) -> Dict[str, Any]:
    """聚合多个节点存储的统计(供 state API / 测试使用)。"""
    per_node = {node: store.stats() for node, store in stores.items()}
    return {
        "nodes": per_node,
        "total_used_bytes": sum(s["used_bytes"] for s in per_node.values()),
        "total_capacity_bytes": sum(s["capacity_bytes"] for s in per_node.values()),
        "total_objects": sum(s["num_objects"] for s in per_node.values()),
    }


# ---------------------------------------------------------------------------
# 与真实 Ray 的差异
#   * Plasma 用 mmap 匿名文件 + 自己的分配器;mini-ray 用
#     ``multiprocessing.shared_memory``(本质也是 mmap,但由 Python 管理),
#     好处是零依赖、跨平台(Windows 上 Ray 的 Plasma 支持受限)。
#   * Ray 的溢出文件默认用 mmap 读回(不再拷贝);mini-ray 老老实实读回内存,
#     少一次「文件被映射期间不能被删」的平台坑。
#   * Ray 的引用计数由对象「owner」通过 GCS 汇总,语义完整到能跨节点回收;
#     mini-ray 的引用计数由 raylet 汇总,**只在内存压力下才回收 refcount=0 的对象**
#     (真实 Ray 是立即删除)。这是有意的取舍:更安全,但对象在存储里多待一会儿。
#   * Ray 支持对象跨节点传输 + 分布式引用计数 + 对象广播;mini-ray 的跨节点
#     传输退化成一次拷贝(见 raylet.py 的 pull 逻辑)。
# ---------------------------------------------------------------------------

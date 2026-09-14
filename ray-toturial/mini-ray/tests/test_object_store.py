"""对象存储的单元测试。

覆盖:分桶分配器、零拷贝(含跨进程写穿)、只读语义、引用计数、溢出、驱逐、
pin 保护、容量报错。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import numpy as np
import pytest

from miniray.errors import ObjectStoreFullError
from miniray.object_store import (
    BlockHandle,
    ObjectStore,
    SharedMemoryAllocator,
    create_ndarray_view,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_MB = 1024 * 1024


@pytest.fixture()
def store():
    st = ObjectStore("node-0", capacity_bytes=8 * _MB)
    yield st
    st.close()


# ---------------------------------------------------------------- 分配器


def test_allocator_buckets_and_reuse():
    alloc = SharedMemoryAllocator()
    try:
        a = alloc.allocate(100)
        b = alloc.allocate(200)
        assert a.bucket == 12 and b.bucket == 12, "都该落在 4KB 桶里"
        assert alloc.allocate(5000).bucket == 13, "5000 字节应落在 8KB 桶"
        assert a.offset != b.offset

        alloc.free(a)
        reused = alloc.allocate(100)
        assert reused.offset == a.offset, "释放的槽应该被复用"
        assert reused.shm_name == a.shm_name
    finally:
        alloc.close()


def test_allocator_new_segment_when_full():
    alloc = SharedMemoryAllocator()
    try:
        handles = [alloc.allocate(1 * _MB) for _ in range(10)]
        names = {h.shm_name for h in handles}
        assert len(names) > 1, "1MB 桶一个段装不下 10 个,应该开了新段"
        assert alloc.num_segments >= 2
    finally:
        alloc.close()


# ---------------------------------------------------------------- 基本读写


def test_put_get_small_bytes(store):
    oid = b"\x01" * 16
    store.put_bytes(oid, b"hello world")
    assert store.contains(oid)
    assert store.get_bytes(oid) == b"hello world"
    assert store.stats()["num_objects"] == 1


def test_large_bytes_go_to_shared_memory(store):
    oid = b"\x02" * 16
    payload = b"x" * (256 * 1024)
    store.put_bytes(oid, payload)
    assert store.get_bytes(oid) == payload
    stats = store.stats()
    assert stats["shm_bytes_allocated"] > 0, "大对象应该占用共享内存"


def test_ndarray_zero_copy_in_process(store):
    oid = b"\x03" * 16
    arr = np.arange(4096, dtype="float32")
    store.put_ndarray(oid, arr)

    view = store.get_ndarray(oid)
    assert np.array_equal(view, arr)
    assert view.flags.writeable is False, "从对象存储取出的数组必须是只读的(Ray 的语义)"

    # 真的共享同一块内存:改 store 里的原始字节,view 会跟着变
    descriptor = store.get_descriptor(oid)
    raw = create_ndarray_view(dict(descriptor, readonly=False))
    raw[0] = 12345.0
    assert view[0] == 12345.0, "视图没有共享内存(发生拷贝了)"


def test_ndarray_cross_process_zero_copy(store):
    """核心验证:另一个进程 attach 同一块内存,写入后父进程可见。"""
    oid = b"\x04" * 16
    arr = np.arange(1024, dtype="float64").reshape(32, 32)
    store.put_ndarray(oid, arr)
    desc = store.get_descriptor(oid)

    proc = subprocess.run(
        [
            sys.executable,
            "-u",
            os.path.join(_HERE, "_shm_child.py"),
            desc["shm"],
            str(desc["offset"]),
            json.dumps(list(desc["shape"])),
            desc["dtype"],
            "--write",
            "999",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=os.path.dirname(_HERE),
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:")][0]
    result = json.loads(line[len("RESULT:") :])
    assert result["before"] == float(arr.sum())
    assert result["readonly_blocked"] is True, "子进程的默认视图应该是只读的"

    # 子进程写了 999,父进程必须看到 —— 这才叫零拷贝
    after = store.get_ndarray(oid)
    assert after.flat[0] == 999.0


def test_small_ndarray_falls_back_to_pickle(store):
    oid = b"\x05" * 16
    small = np.array([1, 2, 3])
    store.put_ndarray(oid, small)  # 小于阈值 → 走 pickle 路径
    assert store.get_descriptor(oid) is None
    restored = __import__("pickle").loads(store.get_bytes(oid))
    assert np.array_equal(restored, small)


# ---------------------------------------------------------------- 引用与 pin


def test_ref_count_and_reclaim_under_pressure():
    # 容量只够放两个 1MB 对象(装不下第三个)
    store = ObjectStore("n", capacity_bytes=int(2.5 * _MB), enable_spilling=False)
    try:
        a, b = b"\x0a" * 16, b"\x0b" * 16
        store.put_bytes(a, b"a" * _MB)
        store.put_bytes(b, b"b" * _MB)
        assert store.stats()["used_bytes"] == 2 * _MB

        store.remove_ref(a)  # a 不再被引用
        c = b"\x0c" * 16
        store.put_bytes(c, b"c" * _MB)  # 需要腾出 1MB
        assert not store.contains(a), "refcount=0 的对象应该优先被回收"
        assert store.contains(b), "还有引用的对象不该被回收"
        assert store.stats()["counters"]["reclaimed"] == 1
    finally:
        store.close()


def test_spill_and_restore():
    store = ObjectStore("n", capacity_bytes=3 * _MB)  # 允许溢出
    try:
        big = b"\x0d" * 16
        payload = b"z" * (2 * _MB)
        store.put_bytes(big, payload)
        other = b"\x0e" * 16
        store.put_bytes(other, b"y" * (2 * _MB))  # 触发溢出

        stats = store.stats()
        assert stats["counters"]["spilled"] == 1
        assert stats["num_spilled"] == 1
        assert store.get_bytes(big) == payload, "被溢出的对象必须能读回来"
        assert store.stats()["counters"]["restored"] == 1
    finally:
        store.close()


def test_spilled_ndarray_roundtrip():
    store = ObjectStore("n", capacity_bytes=2 * _MB)
    try:
        oid = b"\x0f" * 16
        arr = np.arange(128 * 1024, dtype="float64")  # 1MB
        store.put_ndarray(oid, arr)
        store.put_bytes(b"\x10" * 16, b"q" * (1536 * 1024))  # 挤掉它

        assert store.stats()["num_spilled"] == 1
        restored = store.get_ndarray(oid)
        assert np.array_equal(restored, arr), "溢出后读回的数组必须与原来一致"
    finally:
        store.close()


def test_pinned_object_is_never_evicted():
    store = ObjectStore("n", capacity_bytes=2 * _MB, enable_spilling=False)
    try:
        oid = b"\x11" * 16
        store.put_bytes(oid, b"p" * (1536 * 1024))
        store.pin(oid, owner="worker-1")
        with pytest.raises(ObjectStoreFullError, match="已满"):
            store.put_bytes(b"\x12" * 16, b"n" * (1024 * 1024))
        assert store.contains(oid), "被 pin 的对象绝不能丢"
        store.unpin(oid, owner="worker-1")
    finally:
        store.close()


def test_release_owner_releases_pins(store):
    oid = b"\x13" * 16
    store.put_bytes(oid, b"data" * 100)
    store.pin(oid, owner="w1")
    store.pin(oid, owner="w1")
    store.pin(oid, owner="w2")
    assert store.stats()["num_pinned"] == 1
    assert store.release_owner("w1") == 1
    assert store.stats()["num_pinned"] == 1, "w2 的 pin 不该被 w1 的释放影响"
    assert store.release_owner("w2") == 1
    assert store.stats()["num_pinned"] == 0
    assert store.release_owner("不存在的 owner") == 0


def test_oversized_object_raises_clear_error(store):
    with pytest.raises(ObjectStoreFullError, match="超过整个对象存储容量"):
        store.put_bytes(b"\x14" * 16, b"x" * (16 * _MB))


def test_delete_and_close(store):
    oid = b"\x15" * 16
    store.put_bytes(oid, b"gone")
    assert store.delete(oid) is True
    assert store.contains(oid) is False
    assert store.delete(oid) is False
    store.close()
    assert store.stats()["used_bytes"] == 0


def test_stats_shape(store):
    stats = store.stats()
    for key in ("node_id", "num_objects", "used_bytes", "capacity_bytes", "utilization", "counters"):
        assert key in stats

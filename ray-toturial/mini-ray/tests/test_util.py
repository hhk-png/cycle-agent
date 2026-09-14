"""工具层测试:ActorPool、GPU/自定义资源、对象溢出、生命周期、命名空间。"""

from __future__ import annotations

import os
import time

import numpy as np
import pytest

import miniray as ray
from miniray import state


@ray.remote
class Adder:
    def __init__(self):
        self.sum = 0

    def add(self, value: int) -> int:
        self.sum += value
        return self.sum

    def slow_add(self, value: int, seconds: float = 0.05) -> int:
        time.sleep(seconds)
        self.sum += value
        return self.sum


@ray.remote
def read_env(name: str, default=None):
    return os.environ.get(name, default)


@ray.remote
def gpu_report():
    import os

    return {
        "cuda_visible": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "gpu_ids": ray.get_gpu_ids(),
    }


@ray.remote
def echo(value):
    return value


@ray.remote
class Doubler:
    """无状态 actor:让 ActorPool 的测试结果可预测。"""

    def process(self, value: int) -> int:
        return value * 2


@ray.remote
def big_payload(size_mb: float = 2.0) -> int:
    return int(size_mb * 1024 * 1024)


# ---------------------------------------------------------------------------
# ActorPool
# ---------------------------------------------------------------------------


def test_actor_pool_map(cluster):
    from miniray.util import ActorPool

    pool = ActorPool([Doubler.remote() for _ in range(3)])
    assert pool.num_actors() == 3
    results = list(pool.map(lambda actor, value: actor.process.remote(value), [1, 2, 3, 4]))
    assert results == [2, 4, 6, 8], "map 应该按提交顺序返回"
    assert pool.num_idle() == 3 and pool.num_busy() == 0


def test_actor_pool_map_unordered(cluster):
    from miniray.util import ActorPool

    pool = ActorPool([Doubler.remote() for _ in range(3)])
    results = list(
        pool.map_unordered(lambda actor, value: actor.process.remote(value), [1, 2, 3, 4, 5, 6])
    )
    assert sorted(results) == [2, 4, 6, 8, 10, 12]


def test_actor_pool_get_next(cluster):
    from miniray.util import ActorPool

    pool = ActorPool([Doubler.remote()])
    ref = pool.submit(lambda actor, value: actor.process.remote(value), 7)
    assert pool.num_busy() == 1
    assert pool.get_next() == 14
    assert pool.num_idle() == 1
    assert ref is not None


def test_actor_pool_submit_requires_object_ref(cluster):
    from miniray.util import ActorPool
    from miniray.errors import MiniRayError

    pool = ActorPool([Adder.remote()])
    with pytest.raises(MiniRayError):
        pool.submit(lambda actor, value: value, 1)


# ---------------------------------------------------------------------------
# 资源:GPU 与自定义资源
# ---------------------------------------------------------------------------


def test_gpu_allocation(make_cluster):
    """声明 ``num_gpus=1`` 的任务会拿到 CUDA_VISIBLE_DEVICES,并且是独占的。"""
    ray = make_cluster(num_cpus=4, num_gpus=2)
    assert ray.cluster_resources()["GPU"] == pytest.approx(2.0)

    # 并发提交:两个各要 1 GPU 的任务必须拿到**不同**的卡
    first, second = ray.get(
        [gpu_report.options(num_gpus=1).remote() for _ in range(2)]
    )
    assert first["cuda_visible"] in ("0", "1")
    assert first["gpu_ids"] == [int(first["cuda_visible"])]
    assert second["cuda_visible"] != first["cuda_visible"], "两个 GPU 任务不该抢同一块卡"

    third = ray.get(gpu_report.options(num_gpus=2).remote())
    assert sorted(third["gpu_ids"]) == [0, 1]


def test_custom_resources(make_cluster):
    """自定义资源:``resources={"TPU": 1}``。"""
    ray = make_cluster(num_cpus=2, resources={"TPU": 2})

    @ray.remote(resources={"TPU": 1})
    def use_tpu():
        return "tpu"

    assert ray.get(use_tpu.remote()) == "tpu"
    assert ray.cluster_resources()["TPU"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# 对象存储:溢出与容量
# ---------------------------------------------------------------------------


def test_object_spilling_under_pressure(make_cluster):
    """对象存储写满时先溢出到磁盘,数据不丢(Ray 的默认行为)。"""
    ray = make_cluster(num_cpus=2, object_store_memory=3 * 1024 * 1024)

    refs = [echo.remote(b"x" * (1024 * 1024)) for _ in range(3)]
    values = ray.get(refs)
    assert all(len(value) == 1024 * 1024 for value in values)

    stats = state.summarize_objects()
    assert stats["used_bytes"] <= stats["capacity_bytes"]
    assert stats["counters"]["spilled"] >= 1, f"应该发生了溢出: {stats['counters']}"


def test_oversized_put_raises_object_store_full(make_cluster):
    """对象比整个存储还大:``ray.put`` 直接报 ObjectStoreFullError。"""
    ray = make_cluster(num_cpus=2, object_store_memory=1024 * 1024)
    with pytest.raises(ray.ObjectStoreFullError):
        ray.put(b"y" * (4 * 1024 * 1024))


def test_oversized_task_result_fails_task(make_cluster):
    """任务结果放不下 → 任务失败,错误里带着 ObjectStoreFullError。"""
    ray = make_cluster(num_cpus=2, object_store_memory=1024 * 1024)
    with pytest.raises(ray.RayTaskError) as excinfo:
        ray.get(echo.remote(b"y" * (4 * 1024 * 1024)))
    assert "ObjectStoreFull" in str(excinfo.value) or "容量" in str(excinfo.value)


def test_zero_copy_between_workers(make_cluster):
    """同一个节点上的两个 worker 读到的是**同一块共享内存**。"""
    ray = make_cluster(num_cpus=2)

    @ray.remote
    def make_array(n: int):
        return np.arange(n, dtype="float64")

    @ray.remote
    def inspect(array):
        return {
            "shape": array.shape,
            "readonly": not array.flags.writeable,
            "sum": float(array.sum()),
        }

    array_ref = make_array.remote(50_000)
    info = ray.get(inspect.remote(array_ref))
    assert info["shape"] == (50_000,)
    assert info["readonly"] is True
    assert info["sum"] == pytest.approx(sum(range(50_000)))


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


def test_is_initialized_and_reinit(cluster):
    assert ray.is_initialized()
    with pytest.raises(ray.MiniRayError):
        ray.init(num_cpus=2)
    ray.init(num_cpus=2, ignore_reinit_error=True)  # 显式要求重来一次
    assert ray.is_initialized()


def test_shutdown_then_reinit():
    ray.init(num_cpus=2, logging_level="warning")
    assert ray.is_initialized()
    ray.shutdown()
    assert not ray.is_initialized()

    ray.init(num_cpus=2, logging_level="warning")
    try:
        assert ray.get(echo.remote(1)) == 1
    finally:
        ray.shutdown()


def test_namespace_is_reported(cluster):
    context = ray.get_runtime_context()
    assert context.get_namespace() == "default"


def test_util_namespaces_importable():
    """从 Ray 迁移过来的 import 路径应该都能用。

    ⚠️ **第八轮补强**：原来只测了 `from miniray.util.state import ...` 这一种写法 ——
    它能过，**是因为它绕开了 `miniray/util/__init__.py`**。
    而读者从真实 Ray 迁过来最自然的写法是**属性访问** `ray.util.state.list_actors()`，
    那条路以前直接 `AttributeError: module 'miniray.util' has no attribute 'state'`
    （`util/__init__.py` 从没 import 过 `state`）——
    **测试全绿，文档却写着这条路可用。** 所以两种写法都要断言。
    """
    from miniray.util import ActorPool  # noqa: F401
    from miniray.util.placement_group import placement_group  # noqa: F401
    from miniray.util.scheduling_strategies import (  # noqa: F401
        NodeAffinitySchedulingStrategy,
        PlacementGroupSchedulingStrategy,
    )
    from miniray.util.state import list_actors  # noqa: F401

    # 属性访问路径（Ray 风格：`ray.util.state.list_actors()`）
    import miniray as ray

    assert hasattr(ray.util, "state"), "miniray.util 必须导入 state 子模块"
    assert callable(ray.util.state.list_actors)
    assert callable(ray.util.state.list_objects)
    assert "state" in ray.util.__all__


def test_object_ref_passed_as_argument(cluster):
    """ObjectRef 本身也能当参数传(接收方拿到的是引用,而不是值)。"""
    ref = ray.put({"a": 1})
    assert ray.get(echo.remote(ref)) == {"a": 1}

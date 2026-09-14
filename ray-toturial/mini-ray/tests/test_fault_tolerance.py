"""容错测试 —— 分布式系统里最值得写测试的部分。

三个机制,分别对应三种故障:

============================  ==========================================
故障                           机制
============================  ==========================================
任务抛异常 / worker 崩溃        重试(``max_retries``,换 worker 重跑)
对象丢失(节点故障)            lineage 重建(重新执行产出它任务)
对象丢失且没有血缘(ray.put)     明确报 :class:`ObjectLostError`
============================  ==========================================

Ray 文档里 "Fault Tolerance" 一节讲的就是前两条;第三条是诚实面对
「不是所有东西都能重建」。
"""

from __future__ import annotations

import os
import time

import pytest

import miniray as ray
from miniray import state
from miniray._private import fault_injection


@ray.remote(max_retries=0)
def crash_immediately():
    os._exit(17)  # 直接干掉 worker 进程(模拟段错误 / OOM)


@ray.remote(max_retries=2)
def crash_once(marker_path: str):
    """第一次执行时崩溃,之后正常返回 —— 验证「换一个 worker 重跑」。"""
    if not os.path.exists(marker_path):
        with open(marker_path, "w") as handle:
            handle.write("crashed")
        os._exit(3)
    return "recovered"


@ray.remote
def expensive_sum(n: int) -> int:
    return sum(i * i for i in range(n))


@ray.remote
def double(x: int) -> int:
    return x * 2


# ---------------------------------------------------------------------------


def test_worker_crash_raises_worker_crashed_error(cluster):
    with pytest.raises(ray.RayTaskError) as excinfo:
        ray.get(crash_immediately.remote(), timeout=30)
    assert isinstance(excinfo.value, ray.WorkerCrashedError), (
        "worker 崩溃应该是 WorkerCrashedError(与「函数抛异常」区分开)"
    )


def test_task_recovers_after_worker_crash(cluster, tmp_path):
    """worker 崩溃 → 重试到另一个 worker → 成功。

    这是 Ray 容错的核心承诺:**任务会重跑,用户代码不用管**。
    """
    marker = str(tmp_path / "crashed.marker")
    assert ray.get(crash_once.remote(marker), timeout=30) == "recovered"

    tasks = [t for t in state.list_tasks() if t["name"].endswith("crash_once")]
    assert tasks and max(t["num_attempts"] for t in tasks) >= 2, "应该重试过"


def test_lineage_reconstruction(cluster):
    """对象丢失后靠 lineage 重建:对象 ID 不变,值重新算出来。

    ``lose_objects()`` 模拟节点故障(该节点的对象全没了)。因为
    「哪个任务产出了它」被记着,系统会重跑那个任务 —— 调用方完全无感。
    """
    ref = expensive_sum.remote(1000)
    expected = sum(i * i for i in range(1000))
    assert ray.get(ref) == expected

    result = fault_injection.lose_objects()
    assert result["lost"], "应该有对象被丢掉"
    assert result["reconstructed"] >= 1

    # 对象被重建,ray.get 依然返回正确结果
    assert ray.get(ref, timeout=30) == expected
    assert state.summarize_objects()["num_reconstructions"] >= 1


def test_reconstruction_cascades_through_dependencies(cluster):
    """重建要沿依赖链递归:重建 C 需要先重建它的输入 B,B 又需要 A。"""
    a = expensive_sum.remote(100)
    b = double.remote(a)
    c = double.remote(b)
    expected = sum(i * i for i in range(100)) * 4
    assert ray.get(c) == expected

    fault_injection.lose_objects()
    assert ray.get(c, timeout=60) == expected


def test_put_object_lost_is_reported(cluster):
    """``ray.put`` 的对象没有 lineage(不是任务算出来的),丢了只能报错。

    这是有意的:Ray 不保证对象存储是持久的,想要持久化请自己写检查点。
    """
    ref = ray.put({"big": "payload"})
    assert ray.get(ref) == {"big": "payload"}

    fault_injection.lose_objects()
    with pytest.raises(ray.ObjectLostError):
        ray.get(ref, timeout=30)


def test_failed_task_marks_all_return_refs(cluster):
    """多返回值任务失败时,每一个结果对象都带上同一个异常(与 Ray 一致)。"""

    @ray.remote(num_returns=3, max_retries=0)
    def fail_multi():
        raise ValueError("multi failure")

    refs = fail_multi.remote()
    assert isinstance(refs, list) and len(refs) == 3
    for ref in refs:
        with pytest.raises(ray.RayTaskError):
            ray.get(ref)

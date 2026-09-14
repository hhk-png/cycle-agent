"""``local_mode=True`` 的测试。

local_mode 是 Ray 的**调试模式**:所有任务/actor 都在 driver 进程里执行。
它的价值不是性能,而是**可调试性**:断点能停、print 不会被缓冲、
异常堆栈是连续的、不用在多个进程之间追日志。

代价也说清楚:没有并行度、全局状态会互相污染(任务改的全局变量会影响 driver)。
"""

from __future__ import annotations

import os

import pytest

import miniray as ray


@ray.remote
def identify():
    return {"pid": os.getpid(), "driver": os.getpid()}


@ray.remote
def add(a, b):
    return a + b


@ray.remote
def boom():
    raise ValueError("local mode error")


@ray.remote
class Counter:
    def __init__(self, start=0):
        self.value = start

    def inc(self):
        self.value += 1
        return self.value


def test_tasks_run_in_driver_process(make_cluster):
    ray = make_cluster(local_mode=True)
    result = ray.get(identify.remote())
    assert result["pid"] == os.getpid(), "local_mode 下任务应该跑在 driver 进程里"


def test_dependencies_and_values(make_cluster):
    ray = make_cluster(local_mode=True)
    a = add.remote(1, 2)
    b = add.remote(a, 10)
    assert ray.get(b) == 13
    assert ray.get(ray.put({"x": 1})) == {"x": 1}


def test_errors_are_still_captured(make_cluster):
    """local_mode 下异常依然通过 ray.get 抛出(语义与分布式一致)。"""
    ray = make_cluster(local_mode=True)
    with pytest.raises(ray.RayTaskError):
        ray.get(boom.remote())


def test_actor_in_local_mode(make_cluster):
    ray = make_cluster(local_mode=True)
    counter = Counter.remote(5)
    assert ray.get([counter.inc.remote() for _ in range(3)]) == [6, 7, 8]


def test_wait_works_in_local_mode(make_cluster):
    ray = make_cluster(local_mode=True)
    refs = [add.remote(i, i) for i in range(4)]
    ready, remaining = ray.wait(refs, num_returns=4, timeout=5)
    assert len(ready) == 4 and not remaining


def test_state_api_in_local_mode(make_cluster):
    from miniray import state

    ray = make_cluster(local_mode=True)
    ray.get(add.remote(1, 1))
    assert state.summarize_tasks()["total"] >= 1

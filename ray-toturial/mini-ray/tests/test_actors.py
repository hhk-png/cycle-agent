"""Actor 语义测试:状态、顺序、并发、组、async、重启、命名、资源。"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

import miniray as ray
from miniray import state


@ray.remote
class Counter:
    def __init__(self, start: int = 0):
        self.value = start

    def inc(self, k: int = 1) -> int:
        self.value += k
        return self.value

    def get(self) -> int:
        return self.value

    def pid(self) -> int:
        return os.getpid()


@ray.remote
class SlowActor:
    def __init__(self):
        self.calls = 0

    def slow(self, seconds: float) -> float:
        time.sleep(seconds)
        self.calls += 1
        return seconds


@ray.remote
class BoomActor:
    def __init__(self):
        pass

    def boom(self) -> None:
        raise ValueError("actor method failed")


@ray.remote
class InitFails:
    def __init__(self):
        raise RuntimeError("actor init failed")

    def ping(self) -> str:
        return "pong"


@ray.remote(max_restarts=2)
class Restartable:
    def __init__(self):
        self.value = 0

    def inc(self) -> int:
        self.value += 1
        return self.value

    def crash(self) -> None:
        os._exit(1)


@ray.remote(concurrency_groups={"io": 4, "compute": 1})
class GroupedActor:
    @ray.method(concurrency_group="io")
    def io_work(self, seconds: float) -> str:
        time.sleep(seconds)
        return "io"

    @ray.method(concurrency_group="compute")
    def compute_work(self, seconds: float) -> str:
        time.sleep(seconds)
        return "compute"


@ray.remote
class AsyncActor:
    """方法里有 ``async def`` → mini-ray 会把它当成 asyncio actor(默认并发 1000)。"""

    def __init__(self):
        self.count = 0

    async def work(self, seconds: float) -> str:
        await asyncio.sleep(seconds)
        self.count += 1
        return "done"

    async def fetch_and_combine(self):
        """在 async actor 里 await 一个远程对象。"""
        inner = add_one.remote(41)
        return await inner


@ray.remote
def add_one(x: int) -> int:
    return x + 1


@ray.remote
def call_actor(handle, k: int) -> int:
    """把 actor handle 当参数传给普通任务 —— 参数服务器模式的基础。"""
    return ray.get(handle.inc.remote(k))


@ray.remote(num_returns=2)
class SplittingActor:
    def split(self, text: str):
        return text, len(text)


# ---------------------------------------------------------------------------


def test_actor_state_persists(cluster):
    counter = Counter.remote(10)
    assert ray.get(counter.inc.remote()) == 11
    assert ray.get(counter.inc.remote(5)) == 16
    assert ray.get(counter.get.remote()) == 16


def test_actor_runs_in_own_process(cluster):
    counter = Counter.remote()
    assert ray.get(counter.pid.remote()) != os.getpid()


def test_actor_methods_execute_in_order(cluster):
    """同一个 actor 的方法按提交顺序执行(即使都不等待)。"""
    counter = Counter.remote()
    refs = [counter.inc.remote() for _ in range(20)]
    assert ray.get(refs) == list(range(1, 21))


def test_actor_exception(cluster):
    actor = BoomActor.remote()
    with pytest.raises(ray.RayActorError):
        ray.get(actor.boom.remote())


def test_actor_creation_error_surfaces_on_first_call(cluster):
    """``__init__`` 失败时,错误在第一次方法调用时抛出来(与 Ray 一致)。"""
    actor = InitFails.remote()
    with pytest.raises(ray.RayActorError):
        ray.get(actor.ping.remote(), timeout=20)


def test_actor_handle_passed_to_task(cluster):
    counter = Counter.remote(100)
    assert ray.get(call_actor.remote(counter, 1)) == 101
    assert ray.get(counter.inc.remote()) == 102


def test_actor_max_concurrency(make_cluster):
    """``max_concurrency=4`` 让 4 个方法真正并行(默认是 1,会串行)。"""
    ray = make_cluster(num_cpus=4)
    actor = SlowActor.options(max_concurrency=4).remote()
    start = time.time()
    ray.get([actor.slow.remote(0.5) for _ in range(4)])
    elapsed = time.time() - start
    assert elapsed < 1.4, f"没有并发起来,耗时 {elapsed:.2f}s"


def test_actor_default_concurrency_is_serial(make_cluster):
    """默认并发度是 1:4 个 0.4 秒的调用要花 1.6 秒以上。"""
    ray = make_cluster(num_cpus=4)
    actor = SlowActor.remote()
    start = time.time()
    ray.get([actor.slow.remote(0.4) for _ in range(4)])
    elapsed = time.time() - start
    assert elapsed >= 1.5, f"默认应该是串行的,实际耗时 {elapsed:.2f}s"


def test_concurrency_groups(make_cluster):
    """并发组:io 组 4 并发、compute 组 1 并发,互不干扰。"""
    ray = make_cluster(num_cpus=4)
    actor = GroupedActor.remote()

    start = time.time()
    ray.get([actor.compute_work.remote(0.3) for _ in range(3)])
    compute_elapsed = time.time() - start
    assert compute_elapsed >= 0.9, "compute 组应该串行"

    start = time.time()
    ray.get([actor.io_work.remote(0.3) for _ in range(4)])
    io_elapsed = time.time() - start
    assert io_elapsed < 0.9, f"io 组应该 4 路并发,实际 {io_elapsed:.2f}s"


def test_async_actor_concurrency(make_cluster):
    """async actor 天然并发:8 个 0.4 秒的协程应该几乎同时结束。

    这个测试的阈值一开始是 1.5s(宽松),因为当时实现里有一个真实的 bug:
    **轮询连接与上报结果共用同一个 RPC 连接**,而长轮询会在服务端挂起最多 1 秒;
    结果上报又是在事件循环线程里同步调用的 —— 于是事件循环被阻塞,8 个协程里
    有 7 个要等 1 秒才醒(看起来就像「async 没生效」)。
    给轮询单独一条连接之后,耗时降到 0.4s 出头,所以这里收紧到 1.0s 守住它。
    """
    ray = make_cluster(num_cpus=4)
    actor = AsyncActor.remote()
    ray.get(actor.work.remote(0.001))          # 预热:等 actor 进程真正起来
    start = time.time()
    ray.get([actor.work.remote(0.4) for _ in range(8)])
    elapsed = time.time() - start
    assert elapsed < 1.0, f"async actor 没有并发,耗时 {elapsed:.2f}s"


def test_async_actor_await_object_ref(make_cluster):
    ray = make_cluster(num_cpus=4)
    actor = AsyncActor.remote()
    assert ray.get(actor.fetch_and_combine.remote()) == 42


def test_actor_restart_after_crash(make_cluster):
    """``max_restarts=2``:进程崩了会自动重启(但状态会丢)。"""
    ray = make_cluster(num_cpus=4)
    actor = Restartable.remote()
    assert ray.get(actor.inc.remote()) == 1

    with pytest.raises(ray.RayActorError):
        ray.get(actor.crash.remote(), timeout=20)

    # 等重启完成
    deadline = time.time() + 20
    recovered = None
    while time.time() < deadline:
        try:
            recovered = ray.get(actor.inc.remote(), timeout=5)
            break
        except ray.RayActorError:
            time.sleep(0.2)
    assert recovered == 1, "重启后应该是全新状态(值从 0 开始)"
    actors = [a for a in state.list_actors() if a["actor_id"] == actor.actor_id.hex()]
    assert actors and actors[0]["num_restarts"] >= 1


def test_actor_without_restart_dies(cluster):
    actor = Counter.remote()
    ray.kill(actor)
    with pytest.raises(ray.RayActorError):
        ray.get(actor.inc.remote(), timeout=20)


def test_named_actor(cluster):
    Counter.options(name="my-counter").remote(5)
    handle = ray.get_actor("my-counter")
    assert ray.get(handle.inc.remote()) == 6

    with pytest.raises(ValueError):
        ray.get_actor("不存在的名字")


def test_list_actors(cluster):
    Counter.remote()
    Counter.remote()
    actors = ray.list_actors()
    assert len(actors) == 2
    assert all("actor_id" in actor and "state" in actor for actor in actors)


def test_actor_holds_resources(cluster):
    """actor 的资源是**终身持有**的:创建后 available 就少了一块。"""
    before = ray.available_resources().get("CPU", 0)
    Counter.options(num_cpus=2).remote()
    time.sleep(0.3)
    after = ray.available_resources().get("CPU", 0)
    assert before - after == pytest.approx(2.0), f"{before} -> {after}"


def test_actor_method_num_returns(cluster):
    actor = SplittingActor.remote()
    text, length = ray.get(actor.split.remote("hello"))
    assert (text, length) == ("hello", 5)


def test_actor_options_are_copied(cluster):
    """``.options()`` 返回副本,不影响原类。"""
    optioned = Counter.options(max_concurrency=2)
    a = optioned.remote()
    b = Counter.remote()
    assert ray.get(a.inc.remote()) == 1
    assert ray.get(b.inc.remote()) == 1


def test_actor_method_bind(cluster):
    """``handle.method.bind()`` 绑定部分参数,原方法不受影响。

    与 ``functools.partial`` 同语义:绑定的参数**前置**到本次调用的参数之前;
    ``bind`` 与 ``options`` 都返回副本,可链式组合。
    """
    actor = Counter.remote()
    assert ray.get(actor.inc.remote(1)) == 1

    bound = actor.inc.bind(2)          # 预置 k=2
    assert ray.get(bound.remote()) == 3
    assert ray.get(bound.remote()) == 5      # 每次调用都生效,可复用

    # 原 ActorMethod 不受 bind 影响
    assert ray.get(actor.inc.remote(1)) == 6

    # bind 与 options 链式组合,且都不改原对象
    chained = actor.inc.bind(10).options(num_returns=1)
    assert ray.get(chained.remote()) == 16

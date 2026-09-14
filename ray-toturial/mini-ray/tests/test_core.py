"""核心语义测试:任务、依赖、取值、异常、取消、背压。

这些是**用户每天都用的东西**,语义必须和 Ray 一致 —— 所以每个测试都对应
一条 Ray 的文档承诺。
"""

from __future__ import annotations

import os
import time

import numpy as np
import pytest

import miniray as ray
from miniray import state

# ---------------------------------------------------------------------------
# 被测试的远程函数(定义在模块顶层:worker 里按引用 import 就能拿到)
# ---------------------------------------------------------------------------


@ray.remote
def square(x: int) -> int:
    return x * x


@ray.remote
def add(a: int, b: int) -> int:
    return a + b


@ray.remote
def sleep_for(seconds: float, value=None):
    time.sleep(seconds)
    return value


@ray.remote
def raise_error(kind: str = "value"):
    if kind == "value":
        raise ValueError("bad value")
    raise RuntimeError("other error")


@ray.remote
def worker_pid() -> int:
    return os.getpid()


@ray.remote(num_returns=2)
def split_text(text: str):
    return text, len(text)


@ray.remote
def default_kwargs(a: int, b: int = 10, c: int = 100) -> int:
    return a + b + c


@ray.remote
def make_array(n: int):
    return np.arange(n, dtype="float64")


# ---------------------------------------------------------------------------
# 基本任务
# ---------------------------------------------------------------------------


def test_task_returns_value(cluster):
    assert ray.get(square.remote(7)) == 49


def test_tasks_run_in_worker_process(cluster):
    """任务必须跑在**另一个进程**里 —— 这是 Ray 与「本地多线程」的根本区别。"""
    assert ray.get(worker_pid.remote()) != os.getpid()


def test_many_tasks_run_in_parallel(cluster):
    """20 个 sleep(0.2) 的任务,总耗时应该远小于串行的 4 秒。"""
    start = time.time()
    refs = [sleep_for.remote(0.2, i) for i in range(20)]
    values = ray.get(refs)
    elapsed = time.time() - start
    assert values == list(range(20))
    assert elapsed < 2.5, f"没有并行起来,耗时 {elapsed:.2f}s"


def test_task_dependencies(cluster):
    """把一个任务的返回值直接当参数传给另一个任务(Ray 最常用的写法)。"""
    a = square.remote(3)
    b = square.remote(4)
    assert ray.get(add.remote(a, b)) == 25


def test_deep_dependency_chain(cluster):
    """链式依赖:100 层累加。"""
    ref = square.remote(1)
    for _ in range(50):
        ref = add.remote(ref, 1)
    assert ray.get(ref) == 51


def test_kwargs_and_defaults(cluster):
    assert ray.get(default_kwargs.remote(1)) == 111
    assert ray.get(default_kwargs.remote(1, b=2)) == 103
    assert ray.get(default_kwargs.remote(1, c=3, b=2)) == 6


def test_num_returns_multiple(cluster):
    text, length = ray.get(split_text.remote("hello"))
    assert text == "hello" and length == 5

    refs = split_text.remote("hello")
    assert isinstance(refs, list) and len(refs) == 2
    assert ray.get(refs) == ["hello", 5]


def test_num_returns_mismatch_is_reported(cluster):
    @ray.remote(num_returns=3)
    def wrong():
        return 1, 2

    with pytest.raises(ray.RayTaskError):
        ray.get(wrong.remote())


# ---------------------------------------------------------------------------
# 取值语义
# ---------------------------------------------------------------------------


def test_get_nested_structure(cluster):
    """ray.get 支持任意嵌套结构,非 ref 的值原样返回。"""
    a = square.remote(2)
    b = square.remote(3)
    result = ray.get({"x": [a, 5], "y": (b, "lit"), "z": {"deep": a}})
    assert result == {"x": [4, 5], "y": (9, "lit"), "z": {"deep": 4}}


def test_get_plain_value_is_noop(cluster):
    assert ray.get(42) == 42
    assert ray.get([1, 2, 3]) == [1, 2, 3]


def test_put_get_roundtrip(cluster):
    for value in [1, "text", [1, 2, {"k": None}], {"nested": (1, 2)}]:
        assert ray.get(ray.put(value)) == value


def test_put_large_array_is_zero_copy(cluster):
    """``ray.put`` 的大数组应该是共享内存零拷贝,而且**只读**(与 Ray 一致)。"""
    array = np.arange(200_000, dtype="float64")
    ref = ray.put(array)
    got = ray.get(ref)
    assert np.array_equal(got, array)
    assert got.flags.writeable is False, "对象存储里的数组必须只读"

    # 真的共享同一块内存:再取一次,还是同一块(通过引用计数与 pin 保证不被回收)
    again = ray.get(ref)
    assert again.ctypes.data == got.ctypes.data

    # 想改就 copy
    editable = got.copy()
    editable[0] = 1.0
    assert editable[0] == 1.0 and got[0] == 0.0


def test_large_return_value_from_task(cluster):
    array = ray.get(make_array.remote(100_000))
    assert array.shape == (100_000,)
    assert float(array.sum()) == pytest.approx(sum(range(100_000)))


# ---------------------------------------------------------------------------
# 异常语义
# ---------------------------------------------------------------------------


def test_remote_exception_type_and_traceback(cluster):
    with pytest.raises(ray.RayTaskError) as excinfo:
        ray.get(raise_error.remote("value"))
    error = excinfo.value
    assert "ValueError" in str(error)
    assert "raise_error" in error.traceback_str or "ValueError" in error.traceback_str

    # 还原成原始异常类型(这样 except ValueError 也能用)
    original = error.as_instanceof_cause()
    assert isinstance(original, ValueError)
    assert "bad value" in str(original)


def test_remote_exception_new_type(cluster):
    with pytest.raises(ray.RayTaskError) as excinfo:
        ray.get(raise_error.remote("runtime"))
    assert isinstance(excinfo.value.as_instanceof_cause(), RuntimeError)


def test_dependency_failure_propagates(cluster):
    """上游失败 → 下游任务也失败(错误沿依赖链传播,与 Ray 一致)。"""
    bad = raise_error.remote("value")
    downstream = add.remote(bad, 1)
    with pytest.raises(ray.RayTaskError):
        ray.get(downstream)


def test_task_retries_are_counted(cluster):
    """Ray 默认 ``max_retries=3``:抛异常的任务会被重试 4 次(1 次 + 3 次重试)。"""
    with pytest.raises(ray.RayTaskError):
        ray.get(raise_error.remote("value"))
    tasks = [t for t in state.list_tasks() if t["name"].endswith("raise_error")]
    assert tasks, "state API 里应该有这个任务的记录"
    assert max(t["num_attempts"] for t in tasks) == 4


def test_no_retry_when_max_retries_zero(cluster):
    @ray.remote(max_retries=0)
    def always_fails():
        raise ValueError("nope")

    with pytest.raises(ray.RayTaskError):
        ray.get(always_fails.remote())
    tasks = [t for t in state.list_tasks() if t["name"].endswith("always_fails")]
    assert max(t["num_attempts"] for t in tasks) == 1


# ---------------------------------------------------------------------------
# ray.wait(背压的基础)
# ---------------------------------------------------------------------------


def test_wait_returns_ready_and_remaining(cluster):
    refs = [sleep_for.remote(0.05 * (i + 1), i) for i in range(4)]
    ready, remaining = ray.wait(refs, num_returns=2, timeout=10)
    assert len(ready) == 2
    assert len(remaining) == 2
    assert set(ready) | set(remaining) == set(refs)


def test_wait_timeout_returns_partial(cluster):
    fast = sleep_for.remote(0.05, "fast")
    slow = sleep_for.remote(30, "slow")
    ready, remaining = ray.wait([fast, slow], num_returns=2, timeout=1.0)
    assert ready == [fast]
    assert remaining == [slow]


def test_get_timeout(cluster):
    ref = sleep_for.remote(30, "slow")
    with pytest.raises(ray.GetTimeoutError):
        ray.get(ref, timeout=0.5)


def test_backpressure_pattern(cluster):
    """Ray 官方推荐的背压写法:提交到上限就用 ray.wait 收一部分回来。"""
    max_pending = 6
    pending = []
    results = []
    for i in range(20):
        if len(pending) >= max_pending:
            ready, pending = ray.wait(pending, num_returns=1)
            results.extend(ray.get(ready))
        pending.append(square.remote(i))
    results.extend(ray.get(pending))
    assert sorted(results) == [i * i for i in range(20)]


# ---------------------------------------------------------------------------
# 其它
# ---------------------------------------------------------------------------


def test_cancel_pending_task(make_cluster):
    """资源被占满时,排队的任务可以被取消。"""
    ray = make_cluster(num_cpus=1)
    blocker = sleep_for.remote(2.0, "blocker")
    queued = square.remote(5)

    # 让它有机会进入排队状态
    time.sleep(0.5)
    ray.cancel(queued)
    with pytest.raises(ray.TaskCancelledError):
        ray.get(queued)
    assert ray.get(blocker) == "blocker"  # 正在跑的任务不受影响


def test_cancel_running_task_force(make_cluster):
    """force=True 会掐掉正在执行它的 worker(并抛出 TaskCancelledError)。"""
    ray = make_cluster(num_cpus=1)
    ref = sleep_for.remote(30, "never")
    time.sleep(0.5)
    ray.cancel(ref, force=True)
    with pytest.raises(ray.TaskCancelledError):
        ray.get(ref, timeout=15)


def test_bind_partial_application(cluster):
    """``.bind()`` 绑定一部分参数,得到一个「模板任务」。"""
    adder = add.bind(10)
    assert ray.get(adder.remote(5)) == 15
    assert ray.get(adder.remote(1)) == 11


def test_options_override(cluster):
    """``.options()`` 返回副本,不改原函数。"""
    with_gpu = square.options(num_cpus=0.5)
    assert ray.get(with_gpu.remote(4)) == 16
    assert ray.get(square.remote(3)) == 9


def test_runtime_env_env_vars(cluster):
    """runtime_env 可以给任务注入环境变量(执行完还原)。"""

    @ray.remote(runtime_env={"env_vars": {"MINIRAY_TEST_VAR": "hello"}})
    def read_env():
        return os.environ.get("MINIRAY_TEST_VAR")

    assert ray.get(read_env.remote()) == "hello"
    assert os.environ.get("MINIRAY_TEST_VAR") is None, "driver 的环境不该被污染"


def test_worker_reuse(cluster):
    """worker 会被复用:一个任务跑完,worker 回到池子里等下一个任务。

    注意要**串行**提交才能观察到复用:一次性提交 5 个任务时,raylet 会让
    5 个任务同时就绪,于是按需拉起多个 worker(真实 Ray 也一样)。
    """
    pids = [ray.get(worker_pid.remote()) for _ in range(5)]
    assert len(set(pids)) <= 2, f"5 个串行的小任务不该拉起 5 个进程: {pids}"


def test_closed_function_over_closure(cluster):
    """闭包函数也能送过去执行(Ray 用 cloudpickle,mini-ray 用自带的按值序列化)。"""
    factor = 7

    @ray.remote
    def multiply(x):
        return x * factor

    assert ray.get(multiply.remote(6)) == 42


def test_lambda_remote(cluster):
    fn = ray.remote(lambda x, y: x + y)
    assert ray.get(fn.remote(2, 3)) == 5

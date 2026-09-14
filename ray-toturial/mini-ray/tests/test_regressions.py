"""回归测试:第六轮代码审计抓到的 10 个缺陷。

这些 bug 有一个共同点 —— **148 个测试全绿的时候它们依然存在**。
原因不是测试写错了,而是**这些路径从来没有被任何测试走到过**:

* 流式生成器:``grep -rn "generator" tests/`` 在修复前是 **0 命中**
  (整个特性、整个 ``examples/07`` 都没有测试);
* raylet → ObjectStore 的引用计数链路:``store.set_ref_count`` 从未被调用过
  —— 因为它**根本不存在**,而异常被 ``except: pass`` 吞掉了;
* GPU actor:修复前**没有任何测试**创建过 ``num_gpus > 0`` 的 actor;
* async actor + ``concurrency_groups``:只测过同步 actor 的并发组。

所以这个文件的价值不只是"防回归",更是**把那几条没人走过的路走一遍**。
每一条都对应第 37 章结语里的那句话:
**测试全绿不等于没有 bug,只等于"没有测试覆盖到那个 bug"。**
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

import miniray as ray


# ---------------------------------------------------------------------------
# 夹具里要用的 actor / task 必须定义在**模块顶层** ——
# mini-ray 靠 pickle 引用按名字传递它们,函数内定义的类 pick 不起来。
# ---------------------------------------------------------------------------


@ray.remote(num_gpus=1)
class GpuActor:
    def report(self):
        return ray.get_gpu_ids(), os.environ.get("CUDA_VISIBLE_DEVICES")


@ray.remote(memory=1024**3)
class MemActor:
    def ping(self):
        return "ok"


@ray.remote(runtime_env={"env_vars": {"MINIRAY_R6_PROBE": "yes"}})
class EnvActor:
    def probe(self):
        return os.environ.get("MINIRAY_R6_PROBE")


@ray.remote(concurrency_groups={"io": 2, "compute": 2})
class AsyncGrouped:
    def __init__(self):
        self.n = 0

    @ray.method(concurrency_group="io")
    async def io_call(self, k):
        await asyncio.sleep(0.01)
        return ("io", k)

    @ray.method(concurrency_group="compute")
    async def compute_call(self, k):
        return ("compute", k)


@ray.remote(concurrency_groups={"io": 2})
class BadGroupActor:
    @ray.method(concurrency_group="typo_group")
    def run(self):
        return 1


@ray.remote(num_returns="dynamic", max_retries=0)
def squares(n):
    for i in range(n):
        yield i * i


@ray.remote(num_returns="dynamic", max_retries=0)
def failing_generator(fail_after):
    """产出 ``fail_after`` 个 chunk 之后抛异常。"""
    for i in range(fail_after):
        yield i
    raise ValueError("生成器中途失败")


@ray.remote
def make_list(n):
    return list(range(n))


@ray.remote
def consume(d):
    # 如果 d 是个异常对象,这里会抛出莫名其妙的 TypeError,
    # 而不是把真正的「对象丢了」报出来
    return len(d)


@ray.remote
def slow_task():
    time.sleep(0.4)
    return 1


# ---------------------------------------------------------------------------
# ① GPU actor:修复前**完全创建不出来**(Resources.from_request 位置参数错位)
# ---------------------------------------------------------------------------


def test_gpu_actor_can_be_created(make_cluster):
    """``@ray.remote(num_gpus=1)`` 的 actor 必须能创建。

    修复前这里必然抛 ``ScheduleError``:actor 的 ``num_gpus`` 被当成 CPU 用了,
    而第三个位置参数(``memory``)被填了 ``1.0``;节点总内存是 0.0,
    于是「没有节点能放下这个 actor」—— 报错信息还把矛头指向内存,极具误导性。
    """
    ray = make_cluster(num_cpus=4, num_gpus=2)
    handle = GpuActor.remote()          # 修复前:这里就抛 ScheduleError
    ids, _env = ray.get(handle.report.remote())
    assert ids != []


def test_gpu_actors_get_distinct_devices(make_cluster):
    """两个 GPU actor 应拿到**不同**的卡号,而且进程里能看见。

    这条覆盖的是第二个缺陷:actor 的资源在 raylet 侧被正确扣掉了,
    但 ``gpu_ids`` 从来没传给 actor 进程 —— 于是 ``ray.get_gpu_ids()``
    返回 ``[]``、``CUDA_VISIBLE_DEVICES`` 根本没设,
    actor 里的 ``torch.cuda`` 会看到机器上**全部**的卡。
    """
    ray = make_cluster(num_cpus=8, num_gpus=4)
    a, b = GpuActor.remote(), GpuActor.remote()
    ids_a, env_a = ray.get(a.report.remote())
    ids_b, env_b = ray.get(b.report.remote())

    assert len(ids_a) == 1 and len(ids_b) == 1
    assert ids_a != ids_b, f"两个 GPU actor 拿到了同一张卡: {ids_a} / {ids_b}"
    assert env_a == str(ids_a[0]), "CUDA_VISIBLE_DEVICES 没有跟着 actor 的分配走"


def test_actor_memory_option_is_not_silently_dropped(make_cluster):
    """actor 的 ``memory=`` 必须真的生效。

    修复前 ``memory`` 被丢掉了 —— 第三个位置参数正是 memory,
    但实参传的是 ``1.0 if num_gpus else None``,于是它要么被忽略、
    要么变成一个凭空多要的 1.0(而节点内存是 0.0)。
    """
    ray = make_cluster(num_cpus=4, memory=8 * 1024**3)
    assert ray.get(MemActor.remote().ping.remote()) == "ok"


def test_actor_runtime_env_is_applied(make_cluster):
    """actor 级 ``runtime_env`` 必须注入到 actor 进程里。"""
    ray = make_cluster(num_cpus=4)
    assert ray.get(EnvActor.remote().probe.remote()) == "yes"


def test_unknown_concurrency_group_fails_loudly(make_cluster):
    """声明了不存在的并发组 → **报错**,而不是永久挂起。

    修复前:方法声明的组不存在时,任务被投进一个没有执行槽位的邮箱,
    谁也取不到,``ray.get`` 永久挂起 —— 而且不报任何错,
    因为「没人来取」和「还在排队」在调用方看来一模一样。
    """
    ray = make_cluster(num_cpus=4)
    with pytest.raises(Exception):
        handle = BadGroupActor.remote()
        ray.get(handle.run.remote(), timeout=10)


# ---------------------------------------------------------------------------
# ② async actor + concurrency_groups:修复前**永久挂起**
# ---------------------------------------------------------------------------


def test_async_actor_with_concurrency_groups_runs(make_cluster):
    """async actor 声明了 ``concurrency_groups`` 时,组里的方法必须被执行。

    修复前 async 执行上下文只轮询 ``""`` 组,而 raylet 按方法声明的组投递 ——
    于是 ``"io"`` 邮箱里的任务**永远没人取**,``ray.get`` 永久挂起。
    """
    ray = make_cluster(num_cpus=4)
    handle = AsyncGrouped.remote()
    assert ray.get(handle.io_call.remote(1), timeout=30) == ("io", 1)
    assert ray.get(handle.compute_call.remote(2), timeout=30) == ("compute", 2)
    # 同一个组连续调用也要通(确认轮询线程没有被自己饿死)
    assert ray.get(handle.io_call.remote(3), timeout=30) == ("io", 3)


# ---------------------------------------------------------------------------
# ③ 流式生成器:失败时消费端修复前**永久死循环**
# ---------------------------------------------------------------------------


def test_generator_success_path(cluster):
    """生成器的基本语义(修复前这条路径**一个测试都没有**)。"""
    ray = cluster
    got = [ray.get(ref) for ref in squares.remote(4)]
    assert got == [0, 1, 4, 9]


def test_generator_is_lazy(cluster):
    """生成器是**边产边消费**的:第一个 chunk 应该很快拿到。"""
    ray = cluster
    gen = squares.remote(3)
    first = next(iter(gen))
    assert ray.get(first) == 0


def _consume_generator(gen, timeout=20.0):
    """把生成器**抽干**,返回 ``(拿到的 ref, 捕获的异常)``。

    必须放在线程里跑并带超时:生成器内部的 ``wait_for_generator`` 超时是
    60 秒,而它在没等到东西时**只是返回、不抛错** —— 所以在 bug 存在的
    情况下,直接 ``for ref in gen`` 会**永远**转下去,把整个测试挂死。
    用守护线程 + join(timeout) 把"挂起"暴露成一条明确的失败信息。
    """
    import threading

    refs: list = []
    error: list = []

    def run():
        try:
            for ref in gen:
                refs.append(ref)
        except BaseException as exc:  # noqa: BLE001 - 原样带出来
            error.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        pytest.fail(
            f"遍历失败的生成器时挂起了 {timeout} 秒 —— 说明生成器没有被标记为结束"
            "(修复前 task_failed 不会调用 _finish_generator,消费者会一直等下一个 chunk)"
        )
    return refs, (error[0] if error else None)


def test_failing_generator_raises_instead_of_hanging(cluster):
    """生成器失败 → 消费者必须**收到异常**,而不是永远等下一个 chunk。

    修复前 ``task_failed`` 不调用 ``_finish_generator``,
    于是生成器的 ``done`` 永远是 False;而 ``wait_for_generator`` 到点
    只是**返回**、不抛错 —— ``for ref in gen`` 每 60 秒空转一次,永不结束。
    """
    ray = cluster
    refs, error = _consume_generator(failing_generator.remote(2))

    assert error is not None, "生成器失败了,消费者却收到了干净的正常结束"
    # 原异常被包在 RayTaskError 里(和普通任务的失败语义一致),
    # 但原因必须可辨认 —— 不能变成一个无关的 TypeError/AttributeError
    assert "ValueError" in repr(error), f"抛出的异常里看不到原始原因: {error!r}"
    assert "生成器中途失败" in repr(error), f"丢掉了原始错误信息: {error!r}"
    # 已经产出的 chunk 要**先交付**,再报错 —— 顺序不能反
    assert [ray.get(r) for r in refs] == [0, 1]


def test_failing_generator_does_not_overwrite_delivered_chunks(cluster):
    """失败不能把**已经交付**的 chunk 换成异常对象。

    ``record.result_ids`` 对生成器是边产边追加的,所以「把异常写进每一个
    结果对象」会把消费者已经取走的数据覆盖掉 —— 这是一个更隐蔽的错误:
    第一次 ``ray.get`` 拿到正确的值,第二次同一个 ref 拿到异常。
    """
    ray = cluster
    refs, error = _consume_generator(failing_generator.remote(2))
    assert error is not None
    # 已经交付的 ref 必须**稳定地**返回原值(重复 ray.get 也一样)
    assert [ray.get(r) for r in refs] == [0, 1]
    assert [ray.get(r) for r in refs] == [0, 1]


# ---------------------------------------------------------------------------
# ④ 依赖失败传播:异常对象不能被当成**普通参数**传进下游
# ---------------------------------------------------------------------------


@ray.remote
def sleep_long(seconds, tag):
    time.sleep(seconds)
    return tag


def test_dependency_error_is_not_passed_as_a_value(cluster):
    """上游任务的**异常对象**不能被当成普通参数传进下游。

    判据是「值本身是个异常对象」,而不是「它是不是 ``RayTaskError``」。

    这里用 ``TaskCancelledError`` 做探针 —— 它继承的是 ``MiniRayError``
    而不是 ``RayTaskError``,所以修复前它会**穿过**那道检查、
    被当成普通参数喂进用户函数:用户看到一个莫名其妙的 ``TypeError``,
    而真正的原因(上游被取消)被彻底掩盖。
    ``ActorDiedError`` 恰好继承 ``RayTaskError``,所以以前没暴露。

    (顺带说明为什么不用"丢对象"来测:mini-ray 实现了 **lineage 重建** ——
    对象丢了会**重跑生产它的任务**把它恢复出来,这是第 10 章的正面特性,
    不是错误路径。要测异常传播就得用"不会自愈"的失败。)
    """
    ray = cluster
    ref = sleep_long.remote(30, "never")
    time.sleep(0.3)
    ray.cancel(ref, force=True)
    time.sleep(0.3)

    with pytest.raises(Exception) as excinfo:
        ray.get(consume.remote(ref), timeout=30)
    text = repr(excinfo.value)
    assert "cancel" in text.lower(), (
        f"下游任务看到的不是「上游被取消」这个原因,而是: {text}"
    )


# ---------------------------------------------------------------------------
# ⑤ 引用计数:修复前整条链路**从未生效**,且错误被静默吞掉
# ---------------------------------------------------------------------------


def test_object_store_set_ref_count_exists():
    """raylet 调的 ``store.set_ref_count`` 必须真的存在。

    修复前这个方法**不存在**,而调用点的异常被 ``except Exception: pass``
    吞掉了 —— 于是每一次引用计数上报都在抛 ``AttributeError``、
    每一次都被丢弃,对象存储的 ``ref_count`` 永远停在插入时的值,
    ``_evict_one`` 的「refcount == 0 才回收」成了死代码。
    """
    from miniray.object_store import ObjectStore

    assert hasattr(ObjectStore, "set_ref_count"), (
        "ObjectStore.set_ref_count 不存在 —— raylet 的 _push_ref_counts 会抛 "
        "AttributeError,而调用链上的 except 会把它吞掉"
    )


def test_object_store_ref_count_roundtrip(tmp_path):
    """``set_ref_count`` 要按**绝对值**设置,并且能降到 0。"""
    from miniray.object_store import ObjectStore

    store = ObjectStore("node1", capacity_bytes=4 * 1024 * 1024)
    oid = b"\x01" * 24
    store.put_bytes(oid, b"payload")

    store.set_ref_count(oid, 5)
    assert store.ref_counts().get(oid) == 5

    # 绝对值语义:设 2 之后是 2,不是 5+2
    store.set_ref_count(oid, 2)
    assert store.ref_counts().get(oid) == 2

    store.set_ref_count(oid, 0)
    assert store.ref_counts().get(oid) == 0

    # 负数夹到 0
    store.set_ref_count(oid, -3)
    assert store.ref_counts().get(oid) == 0
    store.close()


def test_ref_count_flush_survives_a_normal_workload(cluster):
    """跑一段正常负载,引用计数上报**不能**抛异常。

    修复前这条路径每 50 ms 抛一次 AttributeError(被静默吞掉)。
    现在 ``flush`` 只在收尾阶段安静,运行期的错误会打 warning;
    这里断言的是"能正常跑完且没有把集群搞坏"。
    """
    ray = cluster
    for _ in range(3):
        refs = [make_list.remote(100) for _ in range(4)]
        ray.get(refs)
        del refs
        time.sleep(0.15)          # 至少跨过一个上报周期
    assert ray.get(make_list.remote(1)) == [0]


# ---------------------------------------------------------------------------
# ⑥ 资源归还必须幂等:重复归还会让 available > total
# ---------------------------------------------------------------------------


def test_available_never_exceeds_total(cluster):
    """``available_resources()`` 不能超过 ``cluster_resources()``。

    修复前 ``_release`` 不幂等:取消路径先归还一次,
    临死的 worker 报的 ``task_done`` 可能在另一个线程里再归还一次 ——
    可用资源就能超过总量,之后每一次调度决策都建立在错误的账本上。
    """
    ray = cluster
    total = ray.cluster_resources()
    refs = [slow_task.remote() for _ in range(4)]
    ray.wait(refs, timeout=0.05)
    time.sleep(0.2)
    avail = ray.available_resources()
    for key in ("CPU", "GPU"):
        if key in total:
            assert avail.get(key, 0) <= total[key] + 1e-6, (
                f"{key} 可用量 {avail.get(key)} 超过了总量 {total[key]} —— "
                "说明资源被重复归还了"
            )
    ray.get(refs, timeout=30)


def test_release_is_idempotent():
    """``_release`` 直接调用两次,资源只应该还一次。"""
    from miniray.ids import TaskID
    from miniray.scheduler import NodeResources, Resources, TaskRecord, TaskScheduler

    resources = Resources(cpu=4.0, gpu=0.0, memory=0.0)
    node = NodeResources(node_id="node1", total=resources, available=resources)
    sched = TaskScheduler(nodes={"node1": node})
    record = TaskRecord(
        task_id=TaskID.from_random(),
        name="t",
        result_ids=[],
        deps=set(),
        resources=Resources(cpu=1.0, gpu=0.0, memory=0.0),
        owner="driver",
    )
    sched.add_task(record)
    sched.assign(record, "node1")
    before = node.available.cpu

    sched._release(record)
    after_first = node.available.cpu
    sched._release(record)
    after_second = node.available.cpu

    assert after_first == pytest.approx(before + 1.0)
    assert after_second == pytest.approx(after_first), "重复 _release 又还了一次资源"


def test_release_resets_on_reassign():
    """**重试**要能重新持有资源 —— 幂等标记必须在重新分配时清掉。

    否则一个失败后重试的任务第二次跑完时,归还会被当成重复调用跳过,资源泄漏。
    """
    from miniray.ids import TaskID
    from miniray.scheduler import NodeResources, Resources, TaskRecord, TaskScheduler

    resources = Resources(cpu=4.0, gpu=0.0, memory=0.0)
    node = NodeResources(node_id="node1", total=resources, available=resources)
    sched = TaskScheduler(nodes={"node1": node})
    record = TaskRecord(
        task_id=TaskID.from_random(),
        name="retry-me",
        result_ids=[],
        deps=set(),
        resources=Resources(cpu=2.0, gpu=0.0, memory=0.0),
        owner="driver",
        max_retries=3,
    )
    sched.add_task(record)

    # 第一轮:分配 → 失败归还
    sched.assign(record, "node1")
    sched.task_failed(record.task_id.hex(), "boom")
    after_fail = node.available.cpu

    # 第二轮:重新分配 → 再归还
    sched.assign(record, "node1")
    sched.task_finished(record.task_id.hex())
    after_retry = node.available.cpu

    assert after_fail == pytest.approx(4.0)
    assert after_retry == pytest.approx(4.0), (
        f"重试后资源没有还回来(available={after_retry}),说明幂等标记没被清掉"
    )


# ===========================================================================
# 第七轮:165 个测试全绿时依然存在的缺陷
#
# 这一轮抓到的 6 条有一个共同的形状:**同一个特性有两条实现路径,只测了其中
# 一条**。local_mode 与分布式、生成器与普通任务、actor 创建与 actor 方法调用、
# 「创建时就有资源」与「等资源空出来」—— 每条的第二条路都没有测试走过。
# ===========================================================================


@ray.remote
def _marker(path, tag):
    """把一个标记追加到文件里 —— 数得清「这个函数体到底跑了几次」。"""
    import os as _os

    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{tag}\n")
    return _os.getpid()


@ray.remote
def _boom_marker(path):
    """先记一笔再抛异常 —— 「跑了几次」和「失败得快不快」一起看。"""
    import os as _os

    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{_os.getpid()}\n")
    raise ValueError("planned failure")


@ray.remote(num_returns="dynamic", max_retries=0)
def _slow_generator(n):
    """每 0.3 秒产一个 —— 给「取消」留出可以介入的窗口。"""
    for i in range(n):
        time.sleep(0.3)
        yield i


@ray.remote(num_returns="dynamic", max_retries=0)
def _marker_generator(path, n):
    import os as _os

    with open(path, "a", encoding="utf-8") as handle:
        handle.write("generator\n")
    for i in range(n):
        yield (_os.getpid(), i)


# ---------------------------------------------------------------------------
# ⑦ local_mode:任务被**执行了两次**(driver 里一次 + 子进程里一次)
# ---------------------------------------------------------------------------


def test_local_mode_task_runs_exactly_once(make_cluster, tmp_path):
    """``local_mode=True`` 下,任务函数体只能跑**一次**。

    修复前 ``_execute_inline`` 跑完之后没有在调度器里收尾:任务仍然停在 READY
    状态、留在 READY 队列里,下一个 tick 就被 ``_dispatch`` 捞起来派给一个
    **新拉起的子进程**又跑了一遍。表现是:

    * 副作用(写文件、发请求、改数据库)翻倍;
    * worker 池里凭空多出一个进程 —— 而 local_mode 的意义就是「不起进程」;
    * 用户完全看不出来:driver 里那次的结果先写进对象存储,``ray.get`` 拿到
      的是正确值,状态 API 里 ``num_attempts`` 也还是 1。

    这正是「165 个测试全绿」的原因:``test_tasks_run_in_driver_process`` 只断言
    **driver 里那次**的 pid,多出来的那次它看不见。
    """
    ray = make_cluster(local_mode=True)
    log = tmp_path / "runs.log"

    ray.get(_marker.remote(str(log), "plain"), timeout=30)
    time.sleep(1.0)  # 给「多出来的那次」足够的时间暴露

    lines = log.read_text(encoding="utf-8").split()
    assert lines == ["plain"], (
        f"local_mode 下任务执行了 {len(lines)} 次(应该是 1 次): {lines} —— "
        "说明 _execute_inline 跑完后没有在调度器里收尾,任务又被派给子进程跑了一遍"
    )
    from miniray import runtime

    spawned = runtime.get_raylet()._pool.stats()["spawned"]
    assert spawned == 0, f"local_mode 不该拉起 worker 进程,实际拉起了 {spawned} 个"


def test_local_mode_failing_task_is_not_rerun_in_a_subprocess(make_cluster, tmp_path):
    """local_mode 下**失败**的任务同样只能跑一次。

    这条比「成功路径跑两次」更隐蔽:local_mode 的任务从来没走过 ``assign``,
    所以 ``num_attempts`` 恒为 0,按 ``num_attempts <= max_retries`` 判断会得到
    「可以重试」—— 于是失败的任务又被派给新拉起的子进程,重试满 ``max_retries``
    次。实测一个函数体被调用 5 次(1 次在 driver、4 次在子进程里)。

    而且重试毫无意义:inline 那次已经把异常写进结果对象了,调用方看到的
    就是最终结果;副作用(写文件/发请求)却实实在在地重复了 4 次。
    """
    ray = make_cluster(local_mode=True)
    log = tmp_path / "fails.log"

    with pytest.raises(ray.RayTaskError):
        ray.get(_boom_marker.remote(str(log)), timeout=30)
    time.sleep(1.5)

    lines = log.read_text(encoding="utf-8").split()
    assert lines == [str(os.getpid())], (
        f"失败的任务被调用了 {len(lines)} 次(应该是 1 次,且都在 driver 进程里): {lines}"
    )


def test_local_mode_generator_runs_exactly_once(make_cluster, tmp_path):
    """生成器任务在 local_mode 下同样只能跑一次。

    修复前生成器走的是 ``submit_task`` 里更早返回的那条支路,那里
    ``_schedule_needed.set()`` 是**无条件**调的 —— 于是除了 inline 那次,调度
    循环还会把它派给子进程再跑一遍:函数体执行两次、chunk 交错来自两个生产者,
    消费端拿到的 ref 数量翻倍。
    """
    ray = make_cluster(local_mode=True)
    log = tmp_path / "gen.log"

    gen = _marker_generator.remote(str(log), 2)
    values = [ray.get(ref, timeout=30) for ref in gen]
    time.sleep(1.0)

    lines = log.read_text(encoding="utf-8").split()
    assert lines == ["generator"], (
        f"local_mode 下生成器执行了 {len(lines)} 次(应该是 1 次): {lines}"
    )
    assert len(values) == 2, f"chunk 数量应该是 2,实际 {len(values)}(两个生产者各产了一份)"


# ---------------------------------------------------------------------------
# ⑧ local_mode + 生成器失败:消费端**永久挂起**,已交付的 chunk 还被换成异常
# ---------------------------------------------------------------------------


def test_local_mode_failing_generator_terminates_and_keeps_chunks(make_cluster):
    """local_mode 下生成器中途失败 —— 和分布式路径**同样的**两条要求:

    1. 消费端要收到异常,不能永远等下一个 chunk;
    2. 已经交付的 chunk 不能被异常对象覆盖。

    修复前 ``_execute_inline`` 的 except 分支完全没区分生成器:
    它把异常写进 ``record.result_ids`` 的**每一个** —— 而生成器的 result_ids
    是边产边追加的,里面包含消费者已经取走的 chunk;同时它不调用
    ``_finish_generator``,于是 ``done`` 永远是 False,而
    ``wait_for_generator`` 到点只是返回、不抛错 —— ``for ref in gen`` 每 60 秒
    空转一次,永不结束。
    """
    ray = make_cluster(local_mode=True)
    refs, error = _consume_generator(failing_generator.remote(2), timeout=20.0)

    assert error is not None, "local_mode 下生成器失败了,消费者却收到了干净的正常结束"
    assert "生成器中途失败" in repr(error), f"丢掉了原始错误信息: {error!r}"
    assert [ray.get(r, timeout=10) for r in refs] == [0, 1], (
        "已经交付的 chunk 被异常对象覆盖了(分布式路径有回归测试,local_mode 这条路漏了)"
    )


# ---------------------------------------------------------------------------
# ⑨ actor 的 ``__init__`` 收到 ObjectRef 时,actor **完全创建不出来**
# ---------------------------------------------------------------------------


@ray.remote
def _make_value(n):
    return n


@ray.remote
class RefConsumer:
    def __init__(self, base):
        self.base = base

    def get(self):
        return self.base


def test_actor_init_can_take_an_object_ref(cluster):
    """``A.remote(some_ref)`` —— actor 的构造参数里内联一个 ObjectRef。

    这是 Ray 里很常见的写法(把数据集/配置的句柄交给 actor),但 raylet 从来
    没把 ``__init__`` 参数的依赖告诉 worker:``_spawn_actor_worker`` 构造的
    ``actor_spec`` 里**没有** ``creation_deps`` 这个字段,而 worker 侧读的正是它
    (``spec.get("creation_deps") or []``,永远是空) —— 依赖没被取回,参数里的
    ref 在反序列化时找不到值,创建**必然失败**。

    之所以一直没被发现:``local_mode`` 走的是另一条路(``_create_local_actor``
    自己算了 deps),而 actor 相关测试全都没有往 ``__init__`` 里传过 ref。
    """
    ray = cluster
    ref = _make_value.remote(7)
    handle = RefConsumer.remote(ref)
    assert ray.get(handle.get.remote(), timeout=30) == 7


# ---------------------------------------------------------------------------
# ⑩ 「等资源」的 actor 拿不到自己的 GPU(创建时资源不够 → PENDING 这条支路)
# ---------------------------------------------------------------------------


@ray.remote
def _hold_gpu(seconds):
    time.sleep(seconds)
    return ray.get_gpu_ids()


def test_gpu_actor_that_waited_for_resources_still_gets_its_devices(make_cluster):
    """资源不够、排了一会儿队才起来的 GPU actor,``gpu_ids`` 不能丢。

    ``runtime.gpu_ids`` 是**创建那一刻**从 ``record.gpu_ids`` 拷的快照;资源不够
    时这条快照是空的,而 GPU 是后来 ``_process_pending_actors`` 里的 ``assign``
    才填进 record 的 —— 那条支路忘了把它同步回 ``runtime.gpu_ids``,于是
    actor 进程收到的永远是 ``[]``:``ray.get_gpu_ids()`` 返回空、
    ``CUDA_VISIBLE_DEVICES`` 不设,actor 里的 torch 看得见机器上**全部**的卡。

    第六轮的 GPU 回归测试只覆盖了「创建时就有资源」那条路。
    """
    ray = make_cluster(num_cpus=8, num_gpus=2)
    # 先用两个任务把两张卡**占满**(它们睡 3 秒),这样后面的 actor 只能排队
    hogs = [_hold_gpu.options(num_gpus=1).remote(3.0) for _ in range(2)]
    time.sleep(1.0)

    handle = GpuActor.remote()  # 此刻创建 → 必然走 PENDING → _process_pending_actors
    ids, env = ray.get(handle.report.remote(), timeout=90)

    assert ids != [], "排队等资源的 GPU actor 拿到的是空 gpu_ids"
    assert env == str(ids[0]), f"CUDA_VISIBLE_DEVICES 没跟着分配走: {env!r}"
    ray.get(hogs, timeout=60)


# ---------------------------------------------------------------------------
# ⑪ 调度阶段的失败被当成「可重试」→ READY 队列每 0.2 秒翻一倍
# ---------------------------------------------------------------------------


def test_unmatchable_node_affinity_fails_instead_of_spinning(make_cluster):
    """节点亲和指向一个不存在的节点 → 立刻失败,而不是无限重试。

    修复前这里会失控:``pick_node`` 抛 ``ScheduleError`` → ``_fail_task_permanently``
    → ``task_failed`` 按 ``num_attempts <= max_retries`` 判定为「可重试」——
    可是调度失败**根本没走到 assign**,``num_attempts`` 永远是 0,于是判定恒为真;
    而队列里那条旧记录并没有被消费掉,重试路径又 append 一次 —— 同一个 task
    在 READY 队列里出现两次、下一轮两条都失败、都再 append……

    实测:0.5 秒后队列里 29874 条,2 秒后 6 万条,``ray.get`` 永远等不到结果。
    这是「任务永远挂着 + CPU 空转 + 内存涨」三合一,而且完全不报错。
    """
    from miniray import runtime
    from miniray.util.scheduling_strategies import NodeAffinitySchedulingStrategy

    ray = make_cluster(num_cpus=4)
    strategy = NodeAffinitySchedulingStrategy("deadbeef" * 5)  # 不存在的节点
    ref = _make_value.options(scheduling_strategy=strategy).remote(1)

    with pytest.raises(Exception) as excinfo:
        ray.get(ref, timeout=30)
    assert "节点亲和" in repr(excinfo.value), (
        f"调用方看到的不是「节点亲和不可满足」这个原因: {excinfo.value!r}"
    )

    time.sleep(0.6)
    queued = len(runtime.get_raylet()._scheduler._ready)  # noqa: SLF001
    assert queued < 50, (
        f"READY 队列里积了 {queued} 条(修复前每 0.2 秒翻一倍)—— "
        "调度阶段的失败被当成可重试,重试路径又重复入队"
    )


# ---------------------------------------------------------------------------
# ⑫ ``ray.get_actor(name)`` 把方法元数据里的 num_returns 写死成 1
# ---------------------------------------------------------------------------


@ray.remote
class SplittingActor:
    @ray.method(num_returns=2)
    def split(self, n):
        return (n, n * 2)


def test_get_actor_preserves_num_returns(make_cluster):
    """按名字取回来的句柄,必须保留 ``@ray.method(num_returns=2)``。

    修复前 ``miniray.get_actor`` 用 ``{method: {"num_returns": 1}}`` 造元数据
    (把所有方法一律当成只返回一个值),而 GCS 里其实存着 worker 注册上来的
    完整元数据。后果:同一个方法,

    * 直接用创建时拿到的 handle 调用 → 返回 ``[5, 10]``;
    * 用 ``ray.get_actor(name)`` 拿到的 handle 调用 → 调用方只要 1 个结果,
      **第二个返回值被静默丢掉**,返回类型也从 list 变成了单个 ObjectRef。

    没有任何报错,``ray.get`` 拿到的是一个看起来完全正常的值。
    """
    ray = make_cluster(num_cpus=2)
    handle = SplittingActor.options(name="r7-splitting-actor").remote()

    direct = handle.split.remote(5)
    assert ray.get(direct, timeout=30) == [5, 10]

    time.sleep(0.5)
    fetched = ray.get_actor("r7-splitting-actor")
    result = fetched.split.remote(7)
    assert isinstance(result, list), (
        "ray.get_actor() 拿到的句柄把 num_returns 丢了 —— 只返回了一个 ObjectRef,"
        "第二个返回值被静默丢弃"
    )
    assert ray.get(result, timeout=30) == [7, 14]


# ---------------------------------------------------------------------------
# ⑬ ``ray.cancel(生成器 chunk)`` 静默什么都不做
# ---------------------------------------------------------------------------


def test_cancel_reaches_a_streaming_generator(cluster):
    """``ray.cancel(chunk_ref, force=True)`` 必须真的取消生成器任务。

    修复前这里是个**静默空操作**:``cancel_task`` 靠 ``_object_tasks`` 把「对象 ID」
    映射回「产出它的 task」,而生成器的 chunk **从来没有**做过这条登记 ——
    于是查不到记录、直接 return。既不报错也不生效:``force=True`` 之后生成器
    照样把 30 个 chunk 全部产完。

    顺带一提,同一个字段缺失还让 state API 里的 ``produced_by`` 对生成器永远是
    ``None``(可以拿 ``list_objects()`` 复查)。
    """
    ray = cluster
    gen = _slow_generator.remote(30)
    first = next(iter(gen))
    assert ray.get(first, timeout=20) == 0

    ray.cancel(first, force=True)

    refs, error = _consume_generator(gen, timeout=25)
    assert error is not None, (
        "生成器跑完了整整 30 个 chunk 都没有报「被取消」—— ray.cancel 是个空操作"
    )
    assert "cancel" in repr(error).lower() or "取消" in repr(error), (
        f"消费者看到的不是「被取消」: {error!r}"
    )
    assert len(refs) < 30, f"取消之后生成器还在继续产出({len(refs)} 个 chunk)"

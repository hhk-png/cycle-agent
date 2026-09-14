"""回归测试:第八轮代码审计抓到的 10 个缺陷(F1–F10)。

为什么**单开一个文件**、而不是续写 ``tests/test_regressions.py``?

* ``test_regressions.py`` 已经是第六、七两轮的回归集(800+ 行、26 个用例),
  再塞十轮进去,"哪条用例对应哪一轮的哪一条"就看不出来了 —— 而这份文件的
  价值恰恰在于**可追溯**:每条用例的名字里都带着缺陷编号(``test_f3_…``),
  能和审计报告逐条对上。
* 第八轮的 10 条有一个共同的形状,值得单独成篇:**同一个能力有多条实现路径,
  只测了其中一条**。所以文件里刻意把两条路径都钉住:
  local_mode / 分布式、task / actor、同步 actor / 异步 actor、
  「创建时就有资源」/「等资源空出来」、单节点 / 多节点。
  ``tests/test_regressions.py`` 里那句总结在这里同样成立 ——
  **测试全绿不等于没有 bug,只等于「没有测试覆盖到那个 bug」。**

每一条用例都验证过「把修复回退掉 → 用例必须失败」,否则它就不是回归测试,
只是一个恰好通过的断言。
"""

from __future__ import annotations

import logging
import os
import time

import pytest

import miniray as ray
from miniray import runtime
from miniray.scheduling_strategies import (
    NodeAffinitySchedulingStrategy,
    PlacementGroupSchedulingStrategy,
)
from miniray.util.placement_group import placement_group, remove_placement_group


# ---------------------------------------------------------------------------
# 夹具里要用的 task / actor 必须定义在**模块顶层** ——
# mini-ray 靠 pickle 引用按名字传递它们,函数内定义的类 pick 不起来。
# ---------------------------------------------------------------------------


@ray.remote
def _slow_marker(path, tag, delay=0.6):
    """先记一笔再慢慢跑 —— 「跑了几次」数得清,而且跑得比调度 tick(0.2s)慢。"""
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{tag}:{os.getpid()}\n")
    time.sleep(delay)
    return tag


@ray.remote(num_returns="dynamic", max_retries=0)
def _slow_marker_generator(path, delay=0.6):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"gen:{os.getpid()}\n")
    time.sleep(delay)
    yield 1


@ray.remote(num_cpus=0, num_gpus=1)
def gpu_probe_task():
    return ray.get_gpu_ids()


@ray.remote(num_gpus=1, runtime_env={"env_vars": {"MINIRAY_R8_PROBE": "hello"}})
def env_gpu_probe_task():
    return os.environ.get("MINIRAY_R8_PROBE"), ray.get_gpu_ids()


@ray.remote(num_gpus=1, runtime_env={"env_vars": {"MINIRAY_R8_PROBE": "hello"}})
class EnvGpuProbeActor:
    def probe(self):
        return os.environ.get("MINIRAY_R8_PROBE"), ray.get_gpu_ids()


@ray.remote(max_task_retries=2)
class RetryActor:
    """``max_task_retries>0`` 的 actor —— F3 里它被杀掉后调用必须**报错**而不是挂起。"""

    def slow(self, path):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("started\n")
        time.sleep(30)
        return "done"


@ray.remote
class SplittingActor:
    def split(self):
        return (1, 2)

    def single(self):
        return 5


@ray.remote
class WhereActor:
    def where(self):
        return ray.get_runtime_context().get_node_id()

    def ping(self):
        return "pong"


@ray.remote(concurrency_groups={"io": 2})
class GroupedAsyncActor:
    async def plain(self):
        import asyncio

        await asyncio.sleep(0.3)
        return "plain"


@ray.remote(max_concurrency=1)
class SerialActor:
    def work(self, tag):
        time.sleep(1.0)
        return tag


@ray.remote
class QueueActor:
    """F3 的第二个入口:邮箱里**排队中**的调用。"""

    def blocking(self):
        time.sleep(30)
        return "blocking"

    def queued(self):
        return "queued"


@ray.remote(num_cpus=1)
def boom_with_the_real_cause():
    raise ValueError("THE REAL CAUSE")


@ray.remote(num_cpus=1)
def echo(value):
    return value


# ---------------------------------------------------------------------------
# F1. local_mode 下**慢**任务被就地执行之后又被派给子进程跑了一遍
# ---------------------------------------------------------------------------


def test_f1_scheduler_refuses_to_schedule_an_inline_running_task():
    """「不可调度」这件事必须有**两道**闸门:``ready_tasks`` 与 ``assign``。

    纯调度器单测(不起进程、不看时间),所以它不会 flaky ——
    而这条路径上的时序竞争恰恰是最难用集成测试钉死的。
    """
    from miniray.ids import TaskID
    from miniray.scheduler import (
        NodeResources,
        Resources,
        ScheduleError,
        TaskRecord,
        TaskScheduler,
    )

    total = Resources(cpu=4.0)
    node = NodeResources(node_id="node1", total=total, available=total)
    sched = TaskScheduler(nodes={"node1": node})
    record = TaskRecord(
        task_id=TaskID.from_random(),
        name="inline-me",
        result_ids=[],
        deps=set(),
        resources=Resources(cpu=1.0),
        owner="driver",
    )
    sched.add_task(record)
    assert [t.task_id for t in sched.ready_tasks()] == [record.task_id]

    sched.begin_inline(record)
    assert record.inline_running is True
    assert sched.ready_tasks() == [], (
        "正在被 raylet 就地执行的 task 绝不能出现在就绪队列里 —— "
        "否则调度线程会把它派给一个新拉起的子进程再跑一遍"
    )
    with pytest.raises(ScheduleError):
        sched.assign(record, "node1")

    # 白盒:把 id **人为地**塞回就绪队列,模拟「摘除那一步漏了」的实现。
    # 这时唯一还能挡住它的就是 ready_tasks() 里的 inline_running 过滤 ——
    # 两个机制必须**各自**成立,不能靠对方兜底。
    sched._enqueue_ready(record.task_id.hex())
    assert sched.ready_tasks() == [], (
        "id 还在就绪队列里时,inline_running 过滤也必须挡住它"
    )

    # 跑完之后(``task_finished``)才回到可调度/终态
    sched.task_finished(record.task_id.hex())
    assert record.inline_running is False


def test_f1_local_mode_slow_task_runs_exactly_once(make_cluster, tmp_path):
    """local_mode 下**慢于一个调度 tick** 的任务也只能执行一次。

    ``test_regressions.py`` 里那条 ``..._runs_exactly_once`` 用的是**快**任务:
    inline 执行在一个 tick(0.2 秒)之内就结束了,状态已经是 FINISHED,
    所以它挡不住这个缺陷。慢任务(0.6 秒)整个执行窗口里 record 都还停在
    READY —— 调度线程在这个窗口里能把它捞走、派给一个新拉起的 worker 子进程。
    表现是:副作用翻倍、``num_attempts`` 看着正常、资源账本是平的。
    """
    ray = make_cluster(local_mode=True, num_cpus=4)
    log = tmp_path / "slow.log"

    assert ray.get(_slow_marker.remote(str(log), "slow"), timeout=30) == "slow"
    time.sleep(1.2)  # 给「多出来的那次」足够的时间暴露

    lines = log.read_text(encoding="utf-8").split()
    assert len(lines) == 1, (
        f"local_mode 下慢任务执行了 {len(lines)} 次(应该是 1 次): {lines} —— "
        "说明执行期间 record 还停在 READY,被调度线程派给了子进程"
    )
    raylet = runtime.get_raylet()
    assert raylet._pool.stats()["spawned"] == 0, "local_mode 不该拉起 worker 进程"
    assert os.getpid() == int(lines[0].split(":")[1]), "那一次应该在 driver 进程里跑"


def test_f1_local_mode_slow_generator_runs_exactly_once(make_cluster, tmp_path):
    """生成器走的是 ``submit_task`` 里更早返回的那条支路,同样要挡住。"""
    ray = make_cluster(local_mode=True, num_cpus=4)
    log = tmp_path / "slow_gen.log"

    generator = _slow_marker_generator.remote(str(log))
    values = [ray.get(ref, timeout=30) for ref in generator]
    time.sleep(1.2)

    lines = log.read_text(encoding="utf-8").split()
    assert len(lines) == 1, f"local_mode 下慢生成器执行了 {len(lines)} 次: {lines}"
    assert values == [1], f"chunk 数量应该是 1,实际 {values}"


# ---------------------------------------------------------------------------
# F2. GPU 放置组里的任务**永远跑不起来**(永久挂起 + bundle 被扣坏)
# ---------------------------------------------------------------------------


def test_f2_gpu_task_inside_placement_group_runs_and_repeats(make_cluster):
    """放置组里声明了 GPU 的任务必须能跑,而且能**反复**跑。

    修复前 reserve 把 GPU 从公共池挪进了 ``pg_gpus``,assign 却去公共池
    ``take_gpus``(此时已经空了)→ 抛 ``ScheduleError``;而失败发生在
    ``pg_free`` 已经被减掉**之后**,于是 bundle 的 GPU 被永久扣掉
    (``pg_free`` 里的 bundle 变成 ``{}``),之后连节点都选不出来。
    表现:``ray.get`` 永久挂起,READY、error=None、没有任何诊断信息。
    """
    from miniray.scheduler import Resources

    ray = make_cluster(num_cpus=4, num_gpus=1)
    raylet = runtime.get_raylet()
    node_id = raylet.node_ids[0]

    pg = placement_group([{"GPU": 1}], strategy="STRICT_PACK")
    ray.get(pg.ready())

    for round_index in range(2):
        ref = gpu_probe_task.options(
            scheduling_strategy=PlacementGroupSchedulingStrategy(pg)
        ).remote()
        assert ray.get(ref, timeout=30) == [0], f"第 {round_index} 轮没拿到 GPU"

    # 每轮跑完后,bundle 的账要回来(修复前第一轮就把它扣成 {} 了)
    bundle = raylet._scheduler.nodes[node_id].pg_free[pg.id.hex()][0]
    assert bundle.gpu == pytest.approx(1.0), (
        f"bundle 的 GPU 被永久扣掉了: {bundle.to_dict()}"
    )

    # 放置组删掉之后,卡要回到节点公共池(不然每建一次放置组就少一张卡)
    remove_placement_group(pg)
    time.sleep(0.3)
    assert raylet._scheduler.nodes[node_id].gpus_free == [0], (
        "放置组释放后 GPU 没有回到公共池: "
        f"{raylet._scheduler.nodes[node_id].gpus_free}"
    )
    assert ray.get(gpu_probe_task.remote(), timeout=30) == [0]


# ---------------------------------------------------------------------------
# F3. ``max_task_retries>0`` + actor 死亡 → 在飞调用被塞回**死 actor** 的邮箱
# ---------------------------------------------------------------------------


def test_f3_actor_death_with_task_retries_raises_instead_of_hanging(make_cluster, tmp_path):
    """``max_task_retries>0`` 的 actor 被杀掉 → 在飞调用必须**报错**。

    修复前它被重新投回邮箱,而这个 actor 已经彻底 DEAD、不会再有进程来取 ——
    ``actor_poll`` 对 DEAD actor 直接返回 shutdown,于是 ``ray.get`` 既不超时
    也不报错,**永久挂起**。荒谬的是:``max_task_retries=0`` 时立刻抛
    ``ActorDiedError``,``=2`` 反而挂起 —— 两边的表现正好相反。

    用「方法真的开始跑了」作为前置条件(写文件),而不是 ``sleep(1.0)``
    撞运气:方法还没开始跑就被杀是**另一个**问题(排队中的任务),不该混进来。
    """
    ray = make_cluster(num_cpus=2)
    started = tmp_path / "started.flag"

    handle = RetryActor.remote()
    ref = handle.slow.remote(str(started))

    deadline = time.time() + 30
    while time.time() < deadline and not started.exists():
        time.sleep(0.05)
    assert started.exists(), "方法没能开始执行,测试前提不成立"

    ray.kill(handle)
    start = time.time()
    with pytest.raises(ray.ActorDiedError):
        ray.get(ref, timeout=30)
    assert time.time() - start < 25, "报错得太慢了,像是先挂起再被 timeout 兜住"


def test_f3_queued_actor_calls_die_with_their_actor(cluster):
    """**邮箱里排队中**的调用也要跟着 actor 一起死。

    这是同一个洞的第二个入口:「在飞」只覆盖了已经被执行槽位取走的那一半,
    排在邮箱里等槽位的调用同样永远不会有人来取 —— actor 一死,
    ``actor_poll`` 就返回 shutdown 了。只堵在飞那半的话,用户换个
    ``max_concurrency``、或者多提交一个调用,就又会撞上永久挂起。
    """
    ray = cluster
    handle = QueueActor.remote()

    blocking = handle.blocking.remote()   # 占住唯一的执行槽位
    queued = handle.queued.remote()       # 排在邮箱里,不会被执行
    time.sleep(0.5)
    ray.kill(handle)

    with pytest.raises(ray.ActorDiedError):
        ray.get(blocking, timeout=30)
    with pytest.raises(ray.ActorDiedError):
        ray.get(queued, timeout=30)


# ---------------------------------------------------------------------------
# F4. actor 的 ``options(num_returns=N)`` 与 ``@ray.method(num_returns=…)`` 不一致
# ---------------------------------------------------------------------------


def test_f4_actor_method_call_options_num_returns_is_honoured(cluster):
    """``handle.method.options(num_returns=2)`` 必须真的产出 2 个结果对象。

    修复前 actor 侧按**方法元数据**(默认 1)产出,driver 侧 raylet 按调用时的
    options 分配了 2 个 —— ``task_done`` 里的 ``zip`` 把多余的**静默截断**,
    第二个 ref 永远停在 PENDING,``ray.get(refs)`` 永久挂起。
    对照:task 路径遇到数量不符会显式抛错(``execution.normalize_results``)。
    """
    ray = cluster
    handle = SplittingActor.remote()
    refs = handle.split.options(num_returns=2).remote()
    assert len(refs) == 2
    assert ray.get(refs, timeout=30) == [1, 2]


def test_f4_actor_method_num_returns_mismatch_fails_loudly(cluster):
    """数量**真的**对不上时,要和 task 路径一样**显式报错**,不能静默。

    ``single()`` 返回一个 int,但调用方要 2 个结果对象 —— 两边不可能对上。
    修复前:``zip`` 静默丢掉多余的那个,调用方永久挂起。
    现在:异常写进**每一个**结果对象,``ray.get`` 立刻报出来。
    """
    ray = cluster
    handle = SplittingActor.remote()
    refs = handle.single.options(num_returns=2).remote()
    with pytest.raises(Exception) as excinfo:
        ray.get(refs, timeout=30)
    assert "num_returns" in repr(excinfo.value), (
        f"报错信息里应该点明 num_returns 不一致,实际: {excinfo.value!r}"
    )
    # 两个结果对象都要写进异常(不能只写一个,另一个永远 PENDING)
    for ref in refs:
        with pytest.raises(Exception):
            ray.get(ref, timeout=30)


# ---------------------------------------------------------------------------
# F5. 失败任务的错误对象**存在 driver 节点上**,却对外宣称在生产它的节点上
# ---------------------------------------------------------------------------


def test_f5_task_error_is_stored_on_the_node_that_produced_it(make_cluster):
    """错误对象的**内容**与**对象目录**必须指向同一个节点。

    修复前 ``_store_error`` 恒写 driver 节点,而 ``_mark_object_ready`` 用的是
    ``record.node_id``(任务实际跑的节点)—— 跨节点消费者去那个节点拉取,
    发现根本没有,于是回落成 ``ObjectLostError``:
    「对象已丢失」这句**指向了与事实完全相反的方向**。同节点消费者碰巧能拿到,
    所以单节点下完全测不出来(三个节点:driver / 生产者 / 消费者各不相同)。
    """
    ray = make_cluster(num_cpus=6, num_nodes=3)
    raylet = runtime.get_raylet()
    driver_node = raylet._driver_node
    producer_node = [n for n in raylet.node_ids if n != driver_node][0]
    consumer_node = [n for n in raylet.node_ids if n not in (driver_node, producer_node)][0]

    bad = boom_with_the_real_cause.options(
        scheduling_strategy=NodeAffinitySchedulingStrategy(producer_node)
    ).remote()
    with pytest.raises(ray.RayTaskError):
        ray.get(bad, timeout=30)

    object_id = bad.hex()
    assert raylet._object_states[object_id]["node_id"] == producer_node
    assert raylet._stores[producer_node].contains(bytes.fromhex(object_id)), (
        "错误对象的内容不在生产它的节点上"
    )
    assert not raylet._stores[driver_node].contains(bytes.fromhex(object_id)), (
        "错误对象的内容跑到了 driver 节点上,而对象目录说它在生产节点 —— "
        "跨节点消费者会在生产节点上找不到它,报出与事实相反的「对象已丢失」"
    )

    # 第三个节点上的消费者拿到的必须是**真实原因**,而不是「对象已丢失」
    down = echo.options(
        scheduling_strategy=NodeAffinitySchedulingStrategy(consumer_node)
    ).remote(bad)
    with pytest.raises(Exception) as excinfo:
        ray.get(down, timeout=30)
    text = repr(excinfo.value) + repr(getattr(excinfo.value, "cause", None))
    assert "ObjectLostError" not in text, f"报成了「对象已丢失」: {text[:200]}"
    assert "THE REAL CAUSE" in text, f"真实原因丢了: {text[:200]}"


# ---------------------------------------------------------------------------
# F6. actor 的 ``scheduling_strategy`` / ``placement_group`` 被静默丢弃
# ---------------------------------------------------------------------------


def test_f6_actor_node_affinity_is_honoured(make_cluster):
    """actor 的节点亲和必须生效。

    默认调度在资源相同的节点之间按 ``min(node_id)`` 取 —— 所以把 actor 钉到
    ``max(node_id)`` 上:策略被忽略时它必然落在另一个节点。
    """
    ray = make_cluster(num_cpus=8, num_nodes=2)
    raylet = runtime.get_raylet()
    n0, n1 = raylet.node_ids
    target = max(n0, n1)

    handle = WhereActor.options(
        scheduling_strategy=NodeAffinitySchedulingStrategy(target)
    ).remote()
    assert ray.get(handle.where.remote(), timeout=30) == target, (
        "actor 没有落在 NodeAffinitySchedulingStrategy 指定的节点上"
    )
    ray.kill(handle)


def test_f6_actor_lands_on_its_placement_group_bundle(make_cluster):
    """「先占资源、再往 bundle 里放 actor」必须能跑通(而不是死锁)。

    bundle 要 **2 个 CPU**(正好一个节点的全部),于是:
    * 预留之后,bundle 所在节点**没有任何**公共 CPU 可用了;
    * 默认调度只能把 actor 放到**另一个**节点上 —— 但那正是缺陷的表现
      (actor 永远 PENDING,预留的资源摆着用不上,经典的「占住再填」死锁)。

    这个构造让「策略被忽略」和「策略生效」的结果**必然不同**,不靠概率。
    """
    ray = make_cluster(num_cpus=4, num_nodes=2)
    raylet = runtime.get_raylet()

    pg = placement_group([{"CPU": 2}], strategy="STRICT_PACK")
    ray.get(pg.ready())
    bundle_node = [
        node_id
        for node_id, node in raylet._scheduler.nodes.items()
        if node.pg_free.get(pg.id.hex())
    ][0]
    other_node = [n for n in raylet.node_ids if n != bundle_node][0]
    assert raylet._scheduler.nodes[other_node].available.cpu == pytest.approx(2.0)

    handle = WhereActor.options(
        scheduling_strategy=PlacementGroupSchedulingStrategy(
            pg, placement_group_bundle_index=0
        )
    ).remote()
    assert ray.get(handle.where.remote(), timeout=30) == bundle_node, (
        f"actor 没落在放置组的 bundle 所在节点({bundle_node[:8]})上"
    )
    assert ray.get(handle.ping.remote(), timeout=30) == "pong"
    ray.kill(handle)


# ---------------------------------------------------------------------------
# F7. local_mode 下 ``runtime_env`` 与 GPU 可见性被静默丢弃
# ---------------------------------------------------------------------------


def test_f7_local_mode_applies_runtime_env_and_gpu_ids(make_cluster):
    """local_mode 下 task 与 actor **两条**路径都要看到 ``env_vars`` 与 GPU。

    分布式路径是 ``worker.py`` 里用 ``runtime_env_context`` 包住函数体;
    local_mode 是另一条实现路径 —— 修复前那条路径完全没碰 runtime_env,
    于是 ``env=None``、``gpus=[]``(分布式下是 ``'hello'`` / ``[0]``),
    而 ``ray.get_gpu_ids()`` 读的就是 ``CUDA_VISIBLE_DEVICES``:
    任务/actor 会看到机器上**全部**的卡(第 30 章那个场景)。
    """
    ray = make_cluster(num_cpus=4, num_gpus=2, local_mode=True)

    env, gpus = ray.get(env_gpu_probe_task.remote(), timeout=30)
    assert env == "hello", "local_mode 下 task 的 runtime_env 被丢掉了"
    assert gpus == [0], f"local_mode 下 task 看不到自己分到的 GPU: {gpus}"

    actor_env, actor_gpus = ray.get(
        EnvGpuProbeActor.remote().probe.remote(), timeout=30
    )
    assert actor_env == "hello", "local_mode 下 actor 的 runtime_env 被丢掉了"
    assert actor_gpus == [0], f"local_mode 下 actor 看不到自己分到的 GPU: {actor_gpus}"


def test_f7_local_mode_restores_the_environment_afterwards(make_cluster):
    """任务跑完要把 ``os.environ`` 还原 —— 不能污染 driver 自己。"""
    ray = make_cluster(num_cpus=4, num_gpus=1, local_mode=True)
    os.environ.pop("MINIRAY_R8_PROBE", None)
    assert ray.get(env_gpu_probe_task.remote(), timeout=30)[0] == "hello"
    assert os.environ.get("MINIRAY_R8_PROBE") is None
    assert os.environ.get("CUDA_VISIBLE_DEVICES") is None


# ---------------------------------------------------------------------------
# F8. async actor 声明了 ``concurrency_groups`` 后 ``max_concurrency`` 塌成 1
# ---------------------------------------------------------------------------


def test_f8_async_actor_with_concurrency_groups_is_not_serialised(make_cluster):
    """有并发组的 async actor,默认组必须**并发**执行(上限 1000,不是 1)。

    修复前 ``if is_async and max_concurrency <= 1 and not groups`` 里的
    ``and not groups`` 让「声明了并发组的 async actor」默认并发度塌成 1,
    整个 actor 退化成串行:8 次 0.3 秒的调用要 2.4 秒。而显式写
    ``max_concurrency=8`` 就正常 —— 说明组容量机制本身是好的,坏的只是默认值。
    """
    ray = make_cluster(num_cpus=4)
    handle = GroupedAsyncActor.remote()

    n = 8
    start = time.time()
    ray.get([handle.plain.remote() for _ in range(n)], timeout=30)
    elapsed = time.time() - start

    assert elapsed < 1.2, (
        f"{n} 次 0.3 秒的调用花了 {elapsed:.2f} 秒(并发应该 ~0.3 秒,"
        "串行是 2.4 秒)—— 有并发组的 async actor 默认并发度塌成了 1"
    )
    info = runtime.get_core_worker()._gcs.get_actor(handle.actor_id.hex())
    assert info["max_concurrency"] == 1000, (
        f"有并发组的 async actor 默认并发度应该是 1000,实际 {info['max_concurrency']}"
    )


# ---------------------------------------------------------------------------
# F9. ``ray.cancel()`` 作用在 actor 方法 ref 上是**静默空操作**
# ---------------------------------------------------------------------------


def test_f9_cancel_actor_method_ref_is_not_a_noop(cluster):
    """``ray.cancel`` 作用在 actor 方法 ref 上必须真的拦住这个调用。

    修复前 ``cancel_task`` 只查 ``self._scheduler.tasks``,而 actor 方法调用
    **从不进调度器**(由 actor 自己的邮箱排队)—— 查不到就 ``return``:
    不报错、不告警,方法照跑不误,``ray.get`` 拿到的是「本来不该跑」的结果。
    这类「静默什么都不做」是最坏的一种 API 行为。
    """
    ray = cluster
    handle = SerialActor.remote()

    assert ray.get(handle.work.remote("warmup"), timeout=30) == "warmup"
    blocker = handle.work.remote("blocker")          # 占住唯一的执行槽位
    victim = handle.work.remote("should-not-run")    # 排在邮箱里
    ray.cancel(victim)

    assert ray.get(blocker, timeout=30) == "blocker"
    with pytest.raises(ray.TaskCancelledError):
        ray.get(victim, timeout=30)


def test_f9_cancel_actor_method_force_stops_a_running_call(cluster):
    """``force=True`` 作用在**正在跑**的 actor 方法上也要有确定的结果。

    actor 方法没有真的中断手段(和 task 路径一样),但结果必须是确定的:
    执行槽位放开、结果判成取消 —— 而不是「什么都不做」。
    """
    ray = cluster
    handle = SerialActor.remote()
    assert ray.get(handle.work.remote("warmup"), timeout=30) == "warmup"

    running = handle.work.remote("running")
    time.sleep(0.4)                    # 让它真的被取走、开始跑
    ray.cancel(running, force=True)

    with pytest.raises(ray.TaskCancelledError):
        ray.get(running, timeout=30)
    # 执行槽位要放开,后面的调用还能正常跑
    assert ray.get(handle.work.remote("after"), timeout=30) == "after"


# ---------------------------------------------------------------------------
# F10. 引用计数上报的**诊断路径本身是坏的**
# ---------------------------------------------------------------------------


class _Collect(logging.Handler):
    def __init__(self, sink):
        super().__init__()
        self.sink = sink

    def emit(self, record):
        self.sink.append(record.getMessage())


def test_f10_refcount_failure_reports_the_real_error(make_cluster, monkeypatch):
    """上报失败时,报出来的必须是**真实的异常**。

    修复前 ``_report_ref_counts`` 里写的是 ``self._stop.is_set()`` ——
    而 ``_stop`` 只存在于 ``ReferenceCounter`` 上,``CoreWorker`` 没有这个属性。
    于是每一次失败都被换成一个 ``AttributeError("'CoreWorker' object has no
    attribute '_stop'")``:**真实的异常被完全顶掉**,而且第二条起降级成 debug。
    诊断路径出错比不诊断更糟 —— 它把用户引到一个根本不存在的方向上。
    """
    ray = make_cluster(num_cpus=2)
    core = runtime.get_core_worker()

    def boom(worker_id, deltas):
        raise RuntimeError("simulated raylet failure")

    monkeypatch.setattr(core._raylet, "update_ref_counts", boom)

    messages = []
    logger = logging.getLogger("miniray.core_worker")
    handler = _Collect(messages)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        core.references.add("ab" * 16, 1)
        core.references.flush()
    finally:
        logger.removeHandler(handler)

    joined = "\n".join(messages)
    assert "simulated raylet failure" in joined, f"真实异常没被报出来: {joined!r}"
    assert "_stop" not in joined, (
        f"诊断路径自己抛了 AttributeError,把真实异常顶掉了: {joined!r}"
    )


def test_f10_refcount_reaches_the_raylet(make_cluster):
    """上报链路要**真的**把计数送到 raylet(不是「看起来在报」)。

    ``test_regressions.py`` 里那条只断言了 ``set_ref_count`` 这个方法存在 ——
    那还不足以说明上报链路是通的。这里从 ``ray.put`` 开始,一路查到 raylet 侧的
    引用账本和对象存储的 ``ref_count``。
    """
    ray = make_cluster(num_cpus=2)
    core = runtime.get_core_worker()
    raylet = runtime.get_raylet()

    ref = ray.put({"payload": 1})
    object_id = ref.hex()

    # 上报是**批量**的(后台线程每 0.05 秒一次),这里主动 flush 一下,
    # 免得断言跑在上报节拍前面 —— 那样测的是时序,不是链路。
    deadline = time.time() + 10
    owners: dict = {}
    while time.time() < deadline:
        core.references.flush()
        owners = raylet._ref_counts.get(object_id, {})
        if owners.get(core.worker_id, 0) >= 1:
            break
        time.sleep(0.05)
    assert owners.get(core.worker_id, 0) >= 1, (
        f"引用计数没有送到 raylet 的账本里: {owners}"
    )
    store = raylet._stores[core.node_id]
    assert store.ref_counts().get(bytes.fromhex(object_id), 0) >= 1, (
        "raylet 没有把「集群范围还有多少引用」压给对象存储 —— "
        "存储据此决定对象能不能回收,不压的话对象会被提前回收"
    )

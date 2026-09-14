"""调度测试:资源视图、节点亲和、放置组、资源预留。

mini-ray 用 ``num_nodes`` 模拟多节点集群 —— 单机上也能观察调度策略的行为。
"""

from __future__ import annotations

import time

import pytest

import miniray as ray
from miniray import state
from miniray.util.placement_group import placement_group, remove_placement_group
from miniray.util.scheduling_strategies import (
    NodeAffinitySchedulingStrategy,
    PlacementGroupSchedulingStrategy,
)


@ray.remote(num_cpus=2)
def two_cpu_task():
    time.sleep(0.05)
    return "done"


@ray.remote
def small_task():
    return "small"


@ray.remote
class OneCpuActor:
    def ping(self):
        return "pong"


# ---------------------------------------------------------------------------
# 资源视图
# ---------------------------------------------------------------------------


def test_multi_node_cluster_resources(make_cluster):
    ray = make_cluster(num_cpus=4, num_nodes=2)
    resources = ray.cluster_resources()
    assert resources["CPU"] == pytest.approx(4.0)
    node_keys = [key for key in resources if key.startswith("node:")]
    assert len(node_keys) == 2, f"应该有 2 个节点: {resources}"
    assert ray.available_resources()["CPU"] == pytest.approx(4.0)

    nodes = ray.nodes()
    assert len(nodes) == 2
    assert all(node["Alive"] for node in nodes)
    assert nodes[0]["Resources"]["CPU"] == pytest.approx(2.0)


def test_resources_are_released_after_task(make_cluster):
    ray = make_cluster(num_cpus=2)
    ref = two_cpu_task.remote()
    deadline = time.time() + 5
    # 任务跑起来之后 CPU 应该被占住
    while time.time() < deadline:
        if ray.available_resources()["CPU"] < 2.0:
            break
        time.sleep(0.02)
    ray.get(ref)
    assert ray.available_resources()["CPU"] == pytest.approx(2.0)


def test_tasks_spread_across_nodes(make_cluster):
    """两个各要 2 CPU 的任务,在 2×2 CPU 的集群上必然分散到两个节点。"""
    ray = make_cluster(num_cpus=4, num_nodes=2)
    ray.get([two_cpu_task.remote() for _ in range(2)])
    tasks = [t for t in state.list_tasks() if t["name"].endswith("two_cpu_task")]
    assert len({t["node_id"] for t in tasks}) == 2, f"应该分散到两个节点: {tasks}"


def test_node_affinity_strategy(make_cluster):
    """硬节点亲和:任务必须落在指定节点上。"""
    ray = make_cluster(num_cpus=2, num_nodes=2)
    target = ray.nodes()[1]["NodeID"]
    strategy = NodeAffinitySchedulingStrategy(target, soft=False)
    ray.get(small_task.options(scheduling_strategy=strategy).remote())
    task = [t for t in state.list_tasks() if t["name"].endswith("small_task")][0]
    assert task["node_id"] == target


# ---------------------------------------------------------------------------
# 放置组
# ---------------------------------------------------------------------------


def test_placement_group_strict_pack(make_cluster):
    ray = make_cluster(num_cpus=4, num_nodes=2)
    pg = placement_group([{"CPU": 1}, {"CPU": 1}], strategy="STRICT_PACK")
    bundles = ray.get(pg.ready())
    assert len(bundles) == 2
    assert len(set(bundles.values())) == 1, f"STRICT_PACK 应该在同一节点: {bundles}"


def test_placement_group_strict_spread(make_cluster):
    ray = make_cluster(num_cpus=4, num_nodes=2)
    pg = placement_group([{"CPU": 1}, {"CPU": 1}], strategy="STRICT_SPREAD")
    bundles = ray.get(pg.ready())
    assert len(set(bundles.values())) == 2, f"STRICT_SPREAD 应该在不同节点: {bundles}"


def test_placement_group_reserves_resources(make_cluster):
    ray = make_cluster(num_cpus=4, num_nodes=2)
    assert ray.available_resources()["CPU"] == pytest.approx(4.0)

    pg = placement_group([{"CPU": 1}, {"CPU": 1}], strategy="STRICT_PACK")
    ray.get(pg.ready())
    assert ray.available_resources()["CPU"] == pytest.approx(2.0), "预留的资源不能再给别人"

    remove_placement_group(pg)
    deadline = time.time() + 5
    while time.time() < deadline:
        if ray.available_resources()["CPU"] == pytest.approx(4.0):
            break
        time.sleep(0.05)
    assert ray.available_resources()["CPU"] == pytest.approx(4.0), "删除放置组要还资源"


def test_task_runs_inside_placement_group_bundle(make_cluster):
    """用 SchedulingStrategy 把任务放进 PG 的某个 bundle 里。"""
    ray = make_cluster(num_cpus=4, num_nodes=2)
    pg = placement_group([{"CPU": 1}, {"CPU": 1}], strategy="STRICT_PACK")
    ray.get(pg.ready())

    refs = [
        small_task.options(
            scheduling_strategy=PlacementGroupSchedulingStrategy(
                pg, placement_group_bundle_index=index
            )
        ).remote()
        for index in range(2)
    ]
    assert ray.get(refs) == ["small", "small"]

    tasks = [t for t in state.list_tasks() if t["name"].endswith("small_task")]
    assert len({t["node_id"] for t in tasks}) == 1, "两个 bundle 同节点 → 两个任务也同节点"


def test_placement_group_waits_until_resources_free(make_cluster):
    """资源不够时放置组保持 PENDING,等资源释放后自动变 CREATED。

    这正是 Ray 放置组的关键语义:**先原子性占住资源,再往里放东西**,
    所以「占不到」时应该等,而不是部分成功。
    """
    ray = make_cluster(num_cpus=2, num_nodes=1)
    pg = placement_group([{"CPU": 2}], strategy="STRICT_PACK")
    assert ray.get(pg.ready(), timeout=20) == {0: ray.nodes()[0]["NodeID"]}

    # 剩下的资源装不下第二个放置组
    second = placement_group([{"CPU": 2}], strategy="STRICT_PACK")
    time.sleep(0.5)
    assert second.state == "PENDING", "资源不足时应该停在 PENDING"

    remove_placement_group(pg)
    deadline = time.time() + 10
    while time.time() < deadline and second.state != "CREATED":
        time.sleep(0.1)
    assert second.state == "CREATED", "资源释放后应该自动分配成功"


def test_actor_inside_placement_group(make_cluster):
    """actor 必须落在它**自己的 bundle** 所在的节点上。

    ⚠️ 这条断言以前是 ``len(actor_nodes) == 1``(两个 actor 在同一节点上)——
    它**测不出**「actor 的 scheduling_strategy 被静默丢弃」这个缺陷:
    ``STRICT_PACK`` 把两个 bundle 放在同一节点上,而默认调度恰好也会把这两个
    actor 放在同一节点上,于是断言照样通过。现在改成和 bundle 的落点比对 ——
    这才是「放置组生效」的真正含义(bundle 落在哪,actor 就该在哪)。
    """
    ray = make_cluster(num_cpus=4, num_nodes=2)
    pg = placement_group([{"CPU": 1}, {"CPU": 1}], strategy="STRICT_PACK")
    ray.get(pg.ready())
    from miniray import runtime

    raylet = runtime.get_raylet()
    bundle_nodes = {
        node_id
        for node_id, node in raylet._scheduler.nodes.items()
        if node.pg_free.get(pg.id.hex())
    }
    assert len(bundle_nodes) == 1, f"STRICT_PACK 的 bundle 应该在同一节点: {bundle_nodes}"
    actors = [
        OneCpuActor.options(
            scheduling_strategy=PlacementGroupSchedulingStrategy(
                pg, placement_group_bundle_index=index
            )
        ).remote()
        for index in range(2)
    ]
    assert ray.get([actor.ping.remote() for actor in actors]) == ["pong", "pong"]
    actor_nodes = {a["node_id"] for a in state.list_actors()}
    assert actor_nodes == bundle_nodes, (
        f"actor 没有落在 bundle 所在的节点上: actor={actor_nodes} bundle={bundle_nodes}"
    )


def test_placement_group_tasks_can_run_in_multiple_rounds(make_cluster):
    """放置组里的任务要能**反复**跑,而不是只跑一轮。

    这是一个真实的 bug:任务释放资源时还给了公共池而不是 bundle 的预留下,
    于是第二轮任务在 bundle 里永远排不上 —— 表现是「放置组里的任务只成功一轮,
    之后静默卡住」。这类 bug 在只跑一次任务的测试里完全看不出来。
    """
    ray = make_cluster(num_cpus=4, num_nodes=1)
    pg = placement_group([{"CPU": 1}, {"CPU": 1}], strategy="PACK")
    ray.get(pg.ready())

    for round_index in range(4):
        refs = [
            small_task.options(
                scheduling_strategy=PlacementGroupSchedulingStrategy(
                    pg, placement_group_bundle_index=index
                )
            ).remote()
            for index in range(2)
        ]
        assert ray.get(refs, timeout=30) == ["small", "small"], f"第 {round_index} 轮失败"

    # 每轮跑完后,bundle 里的资源应该都回来了
    info = ray.util.placement_group_table()[pg.id.hex()]
    assert info["state"] == "CREATED"

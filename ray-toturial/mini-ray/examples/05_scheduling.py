"""05 · 调度:资源、节点亲和、放置组。

运行::

    python examples/05_scheduling.py

Ray 的调度可以一句话概括:**你声明资源,系统决定放哪儿**。

mini-ray 用 ``num_nodes`` 在一台机器上模拟多节点集群,这样你能直接观察到:

* 资源不足时任务**排队**,而不是失败;
* 放置组把资源**原子性预留**下来(这是分布式训练不卡死的关键);
* 节点亲和/放置组能把任务**钉**在指定位置。

.. note::
   真实 Ray 的节点来自 ``ray start --num-cpus=...`` 或 KubeRay 拉起的 Pod;
   mini-ray 的 ``num_nodes`` 是**模拟参数**,但调度语义是一致的。
"""

from __future__ import annotations

import time

import os
import sys

# 让示例在**不安装**包的情况下也能直接运行:把项目根目录加进 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import miniray as ray  # noqa: E402  (必须在 sys.path 调整之后导入)
from miniray import state
from miniray.util.placement_group import placement_group, remove_placement_group
from miniray.util.scheduling_strategies import (
    NodeAffinitySchedulingStrategy,
    PlacementGroupSchedulingStrategy,
)


@ray.remote(num_cpus=1)
def identify(label: str) -> str:
    time.sleep(0.05)
    return label


@ray.remote(num_cpus=2)
def heavy(label: str) -> str:
    return label


def main() -> None:
    # 2 个节点,每节点 2 CPU(总共 4 CPU)
    ray.init(num_cpus=4, num_nodes=2)
    try:
        print("== 集群视图 ==")
        print(f"  cluster_resources: {ray.cluster_resources()}")
        print(f"  available        : {ray.available_resources()}")
        for node in ray.nodes():
            print(f"  节点 {node['NodeID'][:8]}: {node['Resources']}")

        # ---- 1) 资源约束 ----
        print("\n== 1) 资源约束 ==")
        ray.get([heavy.remote(f"heavy-{i}") for i in range(2)])
        tasks = [t for t in state.list_tasks() if t["name"].endswith("heavy")]
        nodes = [t["node_id"][:8] for t in tasks]
        print(f"  两个各要 2 CPU 的任务落在节点:{nodes}(每节点只有 2 CPU,所以必然分开)")

        # ---- 2) 排队:资源不够时任务会等 ----
        print("\n== 2) 资源不足时排队 ==")
        refs = [heavy.remote(f"queued-{i}") for i in range(4)]  # 4 个任务抢 4 CPU
        start = time.time()
        ray.get(refs)
        print(f"  4 个 2-CPU 任务在 4-CPU 集群上跑完,耗时 {time.time() - start:.2f}s")
        print("  它们分两轮跑完,不是失败 —— 这是 Ray 与「线程池满了就报错」的区别")

        # ---- 3) 节点亲和 ----
        print("\n== 3) 节点亲和(把任务钉在某节点) ==")
        target = ray.nodes()[1]["NodeID"]
        strategy = NodeAffinitySchedulingStrategy(target, soft=False)
        ray.get(identify.options(scheduling_strategy=strategy).remote("pinned"))
        task = [t for t in state.list_tasks() if t["name"].endswith("identify")][-1]
        print(f"  指定节点 {target[:8]},实际跑在 {task['node_id'][:8]}")

        # ---- 4) 放置组:先占资源,再放东西 ----
        print("\n== 4) 放置组(Placement Group) ==")
        before = ray.available_resources()["CPU"]
        pg = placement_group([{"CPU": 1}, {"CPU": 1}], strategy="STRICT_PACK")
        bundles = ray.get(pg.ready())
        after = ray.available_resources()["CPU"]
        print(f"  放置组就绪:{ {k: v[:8] for k, v in bundles.items()} }")
        print(f"  预留前可用 CPU {before} → 预留后 {after}(资源被扣住了)")

        actors_or_tasks = [
            identify.options(
                scheduling_strategy=PlacementGroupSchedulingStrategy(
                    pg, placement_group_bundle_index=index
                )
            ).remote(f"in-bundle-{index}")
            for index in range(2)
        ]
        print(f"  放进 bundle 的任务:{ray.get(actors_or_tasks)}")
        placed = [t for t in state.list_tasks() if t["name"].endswith("identify")][-2:]
        print(f"  它们都在同一个节点上(STRICT_PACK):{[t['node_id'][:8] for t in placed]}")

        remove_placement_group(pg)
        time.sleep(0.3)
        print(f"  删除放置组后可用 CPU 回到 {ray.available_resources()['CPU']}")

        print(
            "\n放置组为什么重要?\n"
            "  假设你要起 4 个各需 1 GPU 的 actor。如果逐个创建,可能出现\n"
            "  「前 3 个成功、第 4 个永远等不到 GPU」—— 训练卡死在启动阶段。\n"
            "  放置组先原子性地把 4 张卡占下来,占不到就整体等待,不会半途卡住。"
        )
    finally:
        ray.shutdown()


if __name__ == "__main__":
    main()

"""09 · 可观测性:State API 与 timeline。

运行::

    python examples/09_observability.py

排查分布式系统的三个问题(按重要性排序):

1. **现在集群在干什么?** → :mod:`miniray.state`(对齐 ``ray.util.state``)
2. **这些时间都花在哪了?** → :func:`miniray.timeline`(Chrome Trace / HTML 甘特图)
3. **这个对象为什么还在内存里?** → ``state.list_objects()`` 看引用计数

真实 Ray 还有 Dashboard(web UI)与 ``ray memory`` / ``ray status`` CLI;
mini-ray 把「背后的表」直接暴露成 Python API,再附一张自包含的 HTML 甘特图。
"""

from __future__ import annotations

import os
import tempfile
import time

import sys

# 让示例在**不安装**包的情况下也能直接运行:把项目根目录加进 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import miniray as ray  # noqa: E402  (必须在 sys.path 调整之后导入)
from miniray import state


@ray.remote
def stage(name: str, seconds: float) -> str:
    time.sleep(seconds)
    return name


@ray.remote
class Database:
    def __init__(self):
        self.rows = []

    def insert(self, row: str) -> int:
        time.sleep(0.02)
        self.rows.append(row)
        return len(self.rows)


def main() -> None:
    ray.init(num_cpus=4, num_nodes=2)
    outdir = tempfile.mkdtemp(prefix="miniray-observability-")
    try:
        # ---- 造一点负载,好观察 ----
        refs = [stage.remote(f"stage-{i}", 0.05 + i * 0.02) for i in range(6)]
        db = Database.remote()
        inserts = [db.insert.remote(f"row-{i}") for i in range(5)]
        ray.get(refs + inserts)

        # ---- 1) 聚合视图(先看这个) ----
        print("== 1) 聚合视图 ==")
        print(f"  tasks  : {state.summarize_tasks()}")
        print(f"  objects: {state.summarize_objects()}")
        print(f"  actors : {state.summarize_actors()}")

        # ---- 2) 明细:每个任务跑在哪、多久 ----
        print("\n== 2) 任务明细 ==")
        print(f"  {'name':<14}{'state':<10}{'node':<10}{'attempts':<9}{'duration':<10}")
        for task in state.list_tasks()[:6]:
            node = (task["node_id"] or "-")[:8]
            duration = f"{task['duration']:.3f}s" if task["duration"] else "-"
            print(
                f"  {task['name'][:13]:<14}{task['state']:<10}{node:<10}"
                f"{task['num_attempts']:<9}{duration:<10}"
            )
        print(f"  (共 {len(state.list_tasks())} 个任务)")

        # ---- 3) 资源与 worker ----
        print("\n== 3) 资源与 worker 池 ==")
        print(f"  cluster  : {ray.cluster_resources()}")
        print(f"  available: {ray.available_resources()}")
        for worker in state.list_workers():
            print(
                f"  worker pid={worker['pid']:<7} {worker['state']:<9}"
                f"已执行 {worker['num_tasks_executed']} 个任务"
            )

        # ---- 4) actor 视角 ----
        print("\n== 4) actor 状态 ==")
        for actor in state.list_actors():
            print(f"  actor {actor['actor_id'][:8]} state={actor['state']} "
                  f"mailbox={actor['mailbox']} inflight={actor['inflight']}")

        # ---- 5) timeline ----
        print("\n== 5) timeline ==")
        json_path = os.path.join(outdir, "timeline.json")
        html_path = os.path.join(outdir, "timeline.html")
        events = ray.timeline(json_path, html=html_path)
        print(f"  事件数:{len(events)}")
        print(f"  Chrome Trace:{json_path}")
        print(f"  HTML 甘特图 :{html_path}")
        print("  打开方式:浏览器打开 HTML,或者把 JSON 拖进 chrome://tracing / Perfetto")
        print(
            "\n  甘特图能直接回答的问题:\n"
            "    * 6 个 stage 任务是并行还是串行?\n"
            "    * 有没有 worker 空闲等待(说明并行度不够)?\n"
            "    * 任务之间有没有「空隙」(调度延迟)?"
        )
    finally:
        ray.shutdown()


if __name__ == "__main__":
    main()

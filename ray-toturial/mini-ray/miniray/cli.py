"""``python -m miniray`` 命令行 —— 对齐 ``ray`` CLI 的一小部分。

.. code-block:: bash

    python -m miniray version                # 版本
    python -m miniray status --num-cpus 8    # 起一个本地集群并打印资源视图(类似 ray status)
    python -m miniray demo                   # 内置演示:任务/依赖/actor/对象/timeline
    python -m miniray demo --local-mode      # 同样的演示,但跑在 local_mode 下

真实 Ray 的 CLI 有 ``ray start`` / ``ray status`` / ``ray job submit`` /
``ray state`` 等等;mini-ray 只有「本地起集群 + 看状态」这一条路径,
因为它的集群生命周期跟着 driver 进程走。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from typing import Any, Dict, List

__all__ = ["main"]


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="miniray", description="mini-ray:一个可运行的 Ray 简化实现"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("version", help="打印版本")

    status = sub.add_parser("status", help="起一个本地集群并打印资源/worker 视图")
    status.add_argument("--num-cpus", type=float, default=4.0)
    status.add_argument("--num-gpus", type=float, default=0.0)
    status.add_argument("--num-nodes", type=int, default=1)
    status.add_argument("--json", action="store_true", help="输出原始 JSON")

    demo = sub.add_parser("demo", help="运行内置演示")
    demo.add_argument("--local-mode", action="store_true")
    demo.add_argument("--num-cpus", type=float, default=4.0)
    demo.add_argument("--timeline", default="", help="把 timeline 写到这里(.html 或 .json)")

    args = parser.parse_args(argv)
    if args.command == "version":
        from . import __version__

        print(f"mini-ray {__version__}")
        return 0
    if args.command == "status":
        return _status(args)
    if args.command == "demo":
        return _demo(args)
    parser.print_help()
    return 1


def _status(args: argparse.Namespace) -> int:
    import miniray as ray
    from miniray import state

    ray.init(
        num_cpus=args.num_cpus,
        num_gpus=args.num_gpus,
        num_nodes=args.num_nodes,
        logging_level="warning",
    )
    try:
        view = {
            "cluster_resources": ray.cluster_resources(),
            "available_resources": ray.available_resources(),
            "nodes": state.list_nodes(),
            "workers": state.list_workers(),
            "objects": state.summarize_objects(),
        }
        if args.json:
            print(json.dumps(view, indent=2, ensure_ascii=False, default=str))
        else:
            print("=== mini-ray status ===")
            print("cluster resources :", view["cluster_resources"])
            print("available         :", view["available_resources"])
            for node in view["nodes"]:
                print(
                    f"  node {node['node_id'][:8]}  total={node['resources']}  "
                    f"available={node['available']}  util={node['utilization']}"
                )
            print("workers           :", view["workers"] or "（还没有 worker 被拉起）")
            print("objects           :", view["objects"])
    finally:
        ray.shutdown()
    return 0


def _demo(args: argparse.Namespace) -> int:
    import miniray as ray
    from miniray import state
    from miniray.util import ActorPool

    ray.init(
        num_cpus=args.num_cpus,
        local_mode=args.local_mode,
        logging_level="warning",
    )

    @ray.remote
    def square(x: int) -> int:
        return x * x

    @ray.remote
    def add(a: int, b: int) -> int:
        return a + b

    @ray.remote
    class Accumulator:
        def __init__(self) -> None:
            self.total = 0

        def add(self, value: int) -> int:
            self.total += value
            return self.total

    try:
        print(f"== mini-ray demo(local_mode={args.local_mode})==\n")
        print("1) 并行任务")
        refs = [square.remote(i) for i in range(6)]
        print("   square 结果:", ray.get(refs))

        print("\n2) 任务依赖(把上一个任务的结果直接当参数传)")
        total = add.remote(refs[0], refs[1])
        print("   square(0)+square(1) =", ray.get(total))

        print("\n3) actor(有状态,方法按顺序执行)")
        counter = Accumulator.remote()
        print("   连续调用 add:", ray.get([counter.add.remote(1) for _ in range(3)]))

        print("\n4) ActorPool(一组 actor 并行处理一批输入)")
        pool = ActorPool([Accumulator.remote() for _ in range(2)])
        results = list(pool.map_unordered(lambda actor, value: actor.add.remote(value), range(6)))
        print("   map_unordered 结果:", results)

        print("\n5) 对象存储(大数组零拷贝)")
        import numpy as np

        array = np.arange(200_000, dtype="float64")
        ref = ray.put(array)
        got = ray.get(ref)
        print(f"   put/get {array.nbytes} 字节,结果一致: {bool((got == array).all())}")

        print("\n6) 可观测性")
        print("   tasks:", state.summarize_tasks())
        print("   objects:", state.summarize_objects())

        if args.timeline:
            if args.timeline.endswith(".json"):
                ray.timeline(args.timeline)
            else:
                ray.timeline(html=args.timeline)
            print(f"   timeline 已写入 {args.timeline}")

        tmp_json = os.path.join(tempfile.gettempdir(), "miniray_timeline.json")
        tmp_html = os.path.join(tempfile.gettempdir(), "miniray_timeline.html")
        ray.timeline(tmp_json, html=tmp_html)
        print(f"\ntimeline: {tmp_json}\n         {tmp_html}(可直接用浏览器打开)")
    finally:
        ray.shutdown()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

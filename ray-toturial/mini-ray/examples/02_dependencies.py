"""02 · 依赖图与流水线:把任务组织成 DAG。

运行::

    python examples/02_dependencies.py

Ray 最实用的能力之一:**任务之间的依赖由数据流隐式表达**。
你不需要写 DAG、不需要显式声明边 —— 把上一个任务的 ObjectRef 当参数传进去,
调度器就会自动保证顺序,并且**尽可能并行**。

这个示例模拟一个典型的「数据处理流水线」:

.. code-block:: text

    shard0 ─┐
    shard1 ─┼─▶ parse ─┐
    shard2 ─┘          ├─▶ aggregate ─▶ 结果
    shard3 ────▶ parse ─┘
"""

from __future__ import annotations

import random
import time

import os
import sys

# 让示例在**不安装**包的情况下也能直接运行:把项目根目录加进 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import miniray as ray  # noqa: E402  (必须在 sys.path 调整之后导入)


@ray.remote
def read_shard(shard_id: int) -> dict:
    """模拟「从存储读一批原始数据」。"""
    time.sleep(0.1)
    return {
        "shard": shard_id,
        "rows": [{"id": shard_id * 100 + i, "value": i * shard_id} for i in range(5)],
    }


@ray.remote
def parse(record: dict) -> dict:
    """模拟「解析/清洗」。"""
    rows = record["rows"]
    return {
        "shard": record["shard"],
        "count": len(rows),
        "total": sum(row["value"] for row in rows),
    }


@ray.remote
def aggregate(partials: list) -> dict:
    """汇总:注意它的参数是一个 **list of dict** —— 依赖一次带齐。"""
    return {
        "num_shards": len(partials),
        "total_rows": sum(part["count"] for part in partials),
        "total_value": sum(part["total"] for part in partials),
    }


def main() -> None:
    ray.init(num_cpus=4)
    try:
        shards = 4
        start = time.time()

        # 第一层:并发读
        raw_refs = [read_shard.remote(shard) for shard in range(shards)]
        # 第二层:每个分片各自解析(仍然并发)
        parsed_refs = [parse.remote(raw) for raw in raw_refs]
        # 第三层:聚合成一个结果
        summary = ray.get(aggregate.remote(parsed_refs))

        elapsed = time.time() - start
        print("流水线结果:", summary)
        print(f"总耗时 {elapsed:.2f}s(数据流自动并行,不需要手写线程池)")

        # ---- 展示「依赖是隐式的」:同一个函数,不同的组合方式 ----
        print("\n== 依赖决定执行顺序 ==")
        a = read_shard.remote(0)
        b = parse.remote(a)  # b 必须等 a
        c = parse.remote(a)  # c 也等 a,但 b 和 c 可以并行
        start = time.time()
        ray.get([b, c])
        print(f"  b、c 都依赖 a,但彼此并行:耗时 {time.time() - start:.2f}s")

        # ---- 依赖失败会沿着链传播 ----
        print("\n== 依赖失败会传播 ==")

        @ray.remote(max_retries=0)
        def broken():
            raise ValueError("上游炸了")

        @ray.remote
        def downstream(value):
            return value

        try:
            ray.get(downstream.remote(broken.remote()))
        except ray.RayTaskError as error:
            print(f"  下游任务也失败了:{type(error.cause).__name__}: {error.cause}")

        # ---- 小练习:改一改 ----
        print(
            "\n试试:把 shards 改成 8、num_cpus 改成 2,观察耗时变化"
            "(提示:4 核时 8 个分片要两轮,2 核时要四轮)"
        )
        _ = random  # 保持 import 有意义(示例里可以自己加随机数据)
    finally:
        ray.shutdown()


if __name__ == "__main__":
    main()

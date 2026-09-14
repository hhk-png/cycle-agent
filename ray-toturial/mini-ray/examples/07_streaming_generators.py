"""07 · 流式生成器:``num_returns="dynamic"`` —— 一个任务产出多个对象。

运行::

    python examples/07_streaming_generators.py

普通任务「跑完才返回」;生成器任务可以**边算边产**,下游**边产边消费**。
这是 Ray 做大结果分块、流式推理、训练数据流的基础设施。

.. code-block:: text

   普通任务:  [========== 10s ==========] → 结果(10s 后才能用)
   生成器:    [=2s=]→chunk0  [=2s=]→chunk1  [=2s=]→chunk2 …
                        ▲ 下游可以在第一个 chunk 出来时就开始处理

注意 Ray 的约束:生成器任务 **不能重试**(``max_retries=0``),因为流可能已经
被消费了一部分,重放会产生重复数据。
"""

from __future__ import annotations

import time

import os
import sys

# 让示例在**不安装**包的情况下也能直接运行:把项目根目录加进 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import miniray as ray  # noqa: E402  (必须在 sys.path 调整之后导入)
from miniray import state


@ray.remote(num_returns="dynamic")
def stream_chunks(total: int, chunk_seconds: float = 0.2):
    """每 0.2 秒产出一个 chunk。"""
    for index in range(total):
        time.sleep(chunk_seconds)
        yield {"chunk": index, "data": list(range(index * 10, index * 10 + 10))}


@ray.remote
def consume(chunk: dict) -> int:
    """下游消费者:每个 chunk 一到就能开始处理。"""
    payload = chunk.get("data") or chunk.get("samples") or []
    return sum(payload)


@ray.remote(num_returns="dynamic")
def generate_batches(num_batches: int):
    """更贴近真实的场景:分 batch 产数据,下游边收边训练。"""
    for batch in range(num_batches):
        time.sleep(0.05)
        yield {"batch": batch, "samples": [batch * 4 + i for i in range(4)]}


def main() -> None:
    ray.init(num_cpus=4)
    try:
        # ---- 1) 遍历生成器:拿到一个 chunk 就处理一个 ----
        print("== 1) 边产边消费 ==")
        generator = stream_chunks.remote(5, 0.2)
        start = time.time()
        for index, ref in enumerate(generator):
            value = ray.get(ref)
            elapsed = time.time() - start
            print(f"  第 {index} 个 chunk(提交后 {elapsed:.2f}s 可用):{value['chunk']}")
        print(f"  全部取完耗时 {time.time() - start:.2f}s(数据产完就能用,不是等全部算完)")

        # ---- 2) 一次取完(语法糖:ray.get(生成器) 会物化成列表) ----
        print("\n== 2) ray.get(生成器) 直接物化 ==")
        values = ray.get(stream_chunks.remote(3, 0.05))
        print(f"  拿到 {len(values)} 个 chunk:{[v['chunk'] for v in values]}")

        # ---- 3) 流式 + 下游任务:注意下游任务是**边收边提交**的 ----
        print("\n== 3) 流式管道 ==")
        start = time.time()
        totals = []
        for ref in generate_batches.remote(4):
            totals.append(consume.remote(ray.get(ref)))  # 每批一到就提交下游任务
        print(f"  每个 batch 的和:{ray.get(totals)}")
        print(f"  总耗时 {time.time() - start:.2f}s")

        # ---- 4) 长度与状态 ----
        print("\n== 4) 生成器状态 ==")
        generator = stream_chunks.remote(3, 0.05)
        first = next(iter(generator))
        print(f"  取了一个之后:已知 chunk 数 = {len(generator)}")
        ray.get(first)
        print(f"  第一个 chunk 的内容已取出")

        # ---- 5) 与普通任务对比 ----
        print("\n== 5) 普通任务 vs 生成器 ==")

        @ray.remote
        def all_at_once(total: int, chunk_seconds: float = 0.2):
            chunks = []
            for index in range(total):
                time.sleep(chunk_seconds)
                chunks.append(index)
            return chunks

        start = time.time()
        ray.get(all_at_once.remote(5, 0.2))
        print(f"  普通任务:第一个结果在 {time.time() - start:.2f}s 后才有")

        generator = stream_chunks.remote(5, 0.2)
        start = time.time()
        for ref in generator:
            ray.get(ref)
            print(f"  生成器  :第一个结果在 {time.time() - start:.2f}s 后就有了")
            break

        tasks = [t for t in state.list_tasks() if "stream_chunks" in t["name"]]
        print(f"\n  生成器任务的状态:{tasks[-1]['state'] if tasks else 'N/A'}")
    finally:
        ray.shutdown()


if __name__ == "__main__":
    main()

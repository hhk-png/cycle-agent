"""10 · 综合实战:用 actor + 任务写一个「数据并行训练」骨架。

运行::

    python examples/10_parameter_server.py

这是 Ray 最经典的使用形态(也是 Ray 诞生时的目标场景):

.. code-block:: text

                      ┌──────────────────────┐
                      │  ParameterServer     │  权重在对象存储 / actor 内存里
                      │  weights = [...]     │
                      └───────┬──────────────┘
                    pull ▲    │    ▲ push(梯度)
             ┌───────────┘    │    └───────────┐
             │                │                │
        ┌────┴────┐      ┌────┴────┐      ┌────┴────┐
        │ Worker0 │      │ Worker1 │      │ Worker2 │   每个 worker:
        │ 数据分片 │      │ 数据分片 │      │ 数据分片 │   算梯度 → 推回去
        └─────────┘      └─────────┘      └─────────┘

真实训练会用 NCCL/all-reduce 取代「推给中心 actor」,但**并行骨架是一样的**:
数据分片 → 并行计算 → 同步梯度。理解了它,再去看 Ray Train / vLLM 的
分布式实现,结构一眼就认出来了。

(为了让示例不依赖 torch,这里的「模型」是一个纯 Python 的线性回归,
用梯度下降手写。)
"""

from __future__ import annotations

import random
import time
from typing import List, Tuple

import os
import sys

# 让示例在**不安装**包的情况下也能直接运行:把项目根目录加进 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import miniray as ray  # noqa: E402  (必须在 sys.path 调整之后导入)
from miniray import state


@ray.remote
class ParameterServer:
    """中心参数服务器:保存权重,提供 pull / push 两个方法。

    为什么要用 actor?因为**权重是有状态的**,而且必须被所有 worker 串行访问
    (否则并发写会把梯度算坏)。
    """

    def __init__(self, dim: int):
        self.weights = [0.0] * dim
        self.version = 0
        self.updates = 0

    def pull(self) -> Tuple[List[float], int]:
        return list(self.weights), self.version

    def push(self, gradients: List[float], lr: float) -> int:
        assert len(gradients) == len(self.weights), "梯度维度不匹配"
        for index, grad in enumerate(gradients):
            self.weights[index] -= lr * grad
        self.version += 1
        self.updates += 1
        return self.version

    def stats(self) -> dict:
        return {"version": self.version, "updates": self.updates}


@ray.remote
def compute_gradient(server, shard: List[Tuple[List[float], float]], lr: float) -> List[float]:
    """一个 worker:拉权重 → 在自己的数据分片上算梯度 → 推回去。"""
    weights, _version = ray.get(server.pull.remote())

    dim = len(weights)
    gradients = [0.0] * dim
    for features, label in shard:
        prediction = sum(w * x for w, x in zip(weights, features))
        error = prediction - label
        for index in range(dim):
            gradients[index] += 2 * error * features[index] / len(shard)

    # 把梯度推回中心(返回的是新的版本号 —— 顺带演示 actor 返回值)
    version = ray.get(server.push.remote(gradients, lr))
    return shard and [version] + [round(g, 4) for g in gradients[:2]]


def make_data(samples: int, dim: int, seed: int) -> List[Tuple[List[float], float]]:
    """造一批线性可分的数据:y = 3*x0 + (-2)*x1 + 1 + 噪声。"""
    rng = random.Random(seed)
    truth = [3.0, -2.0] + [0.5] * (dim - 2)
    data = []
    for _ in range(samples):
        features = [rng.uniform(-1, 1) for _ in range(dim)]
        label = sum(w * x for w, x in zip(truth, features)) + 1.0 + rng.gauss(0, 0.01)
        data.append((features, label))
    return data


def main() -> None:
    ray.init(num_cpus=4)
    try:
        dim = 4
        shards = 4
        epochs = 12
        lr = 0.15

        server = ParameterServer.remote(dim)
        data = make_data(400, dim, seed=0)
        chunk = len(data) // shards
        shards_data = [data[i * chunk : (i + 1) * chunk] for i in range(shards)]

        print(f"== 数据并行训练骨架:dim={dim}, {shards} 个分片, {epochs} 轮 ==")
        start = time.time()
        for epoch in range(epochs):
            # 每一轮:并发启动所有 worker,每个 worker 自己 pull/push
            results = ray.get(
                [compute_gradient.remote(server, shard, lr) for shard in shards_data]
            )
            if epoch % 4 == 0:
                weights, version = ray.get(server.pull.remote())
                print(
                    f"  epoch {epoch:>2} 版本={version} "
                    f"权重前两位={[round(w, 3) for w in weights[:2]]} "
                    f"(真实值 [3.0, -2.0])"
                )

        weights, version = ray.get(server.pull.remote())
        elapsed = time.time() - start
        print(f"\n训练完成:耗时 {elapsed:.2f}s,版本 {version}")
        print(f"  学到权重:{[round(w, 3) for w in weights]}")

        # ---- 观察一下这个过程在系统里留下了什么 ----
        print("\n== 系统视角 ==")
        print(f"  actor 状态:{state.list_actors()[0]['state']},"
              f"处理了 {ray.get(server.stats.remote())['updates']} 次梯度更新")
        summary = state.summarize_tasks()
        print(f"  任务总数:{summary['total']}(每轮 {shards} 个 worker 任务)")
        print(f"  worker 池:{len(state.list_workers())} 个进程被复用了 {summary['total']} 次")

        print(
            "\n接下来可以自己改:\n"
            "  * 把 shards 改成 8、num_cpus 改成 2 —— 观察每轮耗时(会变成两轮串行)\n"
            "  * 用 ActorPool 重写 worker 部分(减少每轮拉起任务的固定开销)\n"
            "  * 把 push 改成异步(不 ray.get),看看会发生什么 —— 想想为什么会有问题"
        )
    finally:
        ray.shutdown()


if __name__ == "__main__":
    main()

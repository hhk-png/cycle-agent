"""03 · Actor:有状态的服务、参数服务器、ActorPool。

运行::

    python examples/03_actors.py

任务(task)是**无状态**的一次性计算;actor 是**有状态**的常驻服务。
Ray 里两者的写法几乎一样,区别只在于 ``@ray.remote`` 装饰的是函数还是类。

三个必须记住的 actor 语义(示例里都有验证):

1. 方法调用**异步**,actor 内部**按提交顺序**执行;
2. actor 的资源**终身持有** —— 所以这个示例需要 8 个 CPU:
   它一共要同时活着 7 个 actor(2 个 Counter + 1 个 ParameterServer + 4 个 Incrementer),
   每个占 1 个 CPU。**资源不够时 actor 不会报错,而是静静地排队**(与 Ray 一致),
   10 秒后 raylet 会打一条告警把原因说清楚;
3. actor 挂了默认**不重启**,状态就没了(要重启得配 ``max_restarts``)。
"""

from __future__ import annotations

import time

import os
import sys

# 让示例在**不安装**包的情况下也能直接运行:把项目根目录加进 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import miniray as ray  # noqa: E402  (必须在 sys.path 调整之后导入)


@ray.remote
class Counter:
    def __init__(self, start: int = 0):
        self.value = start

    def inc(self, k: int = 1) -> int:
        self.value += k
        return self.value

    def get(self) -> int:
        return self.value


@ray.remote
class ParameterServer:
    """一个极简的参数服务器:保存一组权重,支持 push/pull。"""

    def __init__(self, num_params: int = 4):
        self.weights = [0.0] * num_params

    def push(self, gradients: list) -> list:
        for index, grad in enumerate(gradients):
            self.weights[index] += grad
        return list(self.weights)

    def pull(self) -> list:
        return list(self.weights)


@ray.remote
def worker_step(server, worker_id: int, lr: float = 0.1) -> float:
    """一个「训练 worker」:拉权重 → 假装算梯度 → 推回去。

    注意这里把 **actor handle 当参数传**给了普通任务 —— 这是 Ray 的
    参数服务器、多 actor 协作的基础写法。
    """
    weights = ray.get(server.pull.remote())
    gradients = [lr * (worker_id + 1) * (index + 1) for index in range(len(weights))]
    updated = ray.get(server.push.remote(gradients))
    return sum(updated)


def main() -> None:
    # 8 个 CPU:见文件开头的说明(每个 actor 终身占 1 个 CPU)
    ray.init(num_cpus=8)
    try:
        # ---- 1) 状态是持久的 ----
        print("== 1) actor 有状态 ==")
        counter = Counter.remote(10)
        print(f"  连续 inc:{ray.get([counter.inc.remote() for _ in range(3)])}")
        print(f"  当前值:{ray.get(counter.get.remote())}")

        # ---- 2) 顺序保证:方法按提交顺序执行 ----
        print("\n== 2) 方法按提交顺序执行 ==")
        async_counter = Counter.remote()
        refs = [async_counter.inc.remote() for _ in range(10)]  # 全都不等待
        print(f"  10 个并发提交的结果:{ray.get(refs)}")

        # ---- 3) 参数服务器(actor handle 当参数传) ----
        print("\n== 3) 参数服务器模式 ==")
        server = ParameterServer.remote(4)
        values = ray.get([worker_step.remote(server, worker) for worker in range(4)])
        print(f"  4 个 worker 各推一次梯度后,权重和:{values}")
        print(f"  最终权重:{ray.get(server.pull.remote())}")

        # ---- 4) ActorPool:一组 actor 并行处理一批输入 ----
        print("\n== 4) ActorPool ==")
        from miniray.util import ActorPool

        @ray.remote
        class Incrementer:
            def __init__(self):
                self.n = 0

            def process(self, value: int, seconds: float = 0.05) -> int:
                time.sleep(seconds)
                self.n += 1
                return value * 10 + self.n

        # 先把不再需要的 actor 杀掉,把资源还给集群 —— 这是 actor 使用的基本纪律。
        # 不杀的话下面的 4 个 Incrementer 会一直等资源(在 Ray 里也一样)。
        for handle in (counter, async_counter, server):
            ray.kill(handle)
        print(f"  杀掉不再需要的 actor 后,可用资源: {ray.available_resources()}")

        pool = ActorPool([Incrementer.remote() for _ in range(4)])
        start = time.time()
        results = list(pool.map_unordered(lambda actor, v: actor.process.remote(v), range(8)))
        print(f"  8 个任务 / 4 个 actor 耗时 {time.time() - start:.2f}s")
        print(f"  结果(乱序):{sorted(results)}")

        # ---- 5) actor 的并发:默认串行,可配 max_concurrency ----
        print("\n== 5) 并发度 ==")

        @ray.remote
        class Sleeper:
            def sleep(self, seconds: float) -> float:
                time.sleep(seconds)
                return seconds

        serial = Sleeper.remote()  # 默认并发度 1
        start = time.time()
        ray.get([serial.sleep.remote(0.3) for _ in range(4)])
        serial_elapsed = time.time() - start

        parallel = Sleeper.options(max_concurrency=4).remote()
        start = time.time()
        ray.get([parallel.sleep.remote(0.3) for _ in range(4)])
        parallel_elapsed = time.time() - start

        print(f"  并发度 1 : {serial_elapsed:.2f}s")
        print(f"  并发度 4 : {parallel_elapsed:.2f}s")
        print("  注意:并发度 > 1 时方法可能**乱序**执行,有状态 actor 要自己想清楚")
    finally:
        ray.shutdown()


if __name__ == "__main__":
    main()

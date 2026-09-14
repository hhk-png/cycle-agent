"""示例 12：用 ``ray.util.queue.Queue`` 搭一条生产者/消费者流水线。

对应教程：**第 06 章**（原语的实现）、**第 29 章 §29.7**（数据怎么在任务间流动）
以及附录 A 的 ``ray.util.queue`` 一节。

为什么需要队列？因为 **ObjectRef 是"一次性交接"**：
``ray.get(ref)`` 拿走值之后,ref 的引用计数随时可能归零、对象被回收。
要让数据在**多个消费者之间流转**、还要**限制在飞数量**,你需要一个
有状态的中间站 —— 那就是 ``Queue``。

这个示例演示四件事：

1. **基本收发**：跨任务传队列（队列本身是可以序列化的 actor handle）；
2. **背压**：``maxsize`` 满了生产者会**阻塞**，而不是把内存吃光；
3. **多消费者**：每个元素**恰好**被取走一次（不重不漏）；
4. **优雅结束**：用哨兵值告诉消费者"没有更多了" ——
   注意**不能用 ``qsize() == 0`` 判断结束**，那是竞态的。

还有一个**很容易踩的死锁**在这个示例里被刻意暴露出来：
队列本身也是一个 actor，**默认占 1 个 CPU**。如果你的集群 CPU 数是
「消费者个数 + 生产者个数」，生产者就会**永远排不上队**（它要等消费者让出 CPU，
而消费者在等生产者喂数据）。所以这里给队列传
``actor_options={"num_cpus": 0}`` —— 它几乎不耗算力，不该占一个核。

运行::

    python examples/12_queue_pipeline.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import miniray as ray  # noqa: E402
from miniray.util.queue import Queue  # noqa: E402

#: 消费者看到这个值就收工
SENTINEL = None


@ray.remote
def produce(q, n, delay=0.0):
    """生产者：往队列里塞 n 个元素，塞完放 n 个哨兵。"""
    for i in range(n):
        q.put({"seq": i, "payload": f"item-{i}"})
        if delay:
            time.sleep(delay)
    return n


@ray.remote
def consume(q, name):
    """消费者：一直取到哨兵为止，返回自己处理过的序号。"""
    handled = []
    while True:
        item = q.get()
        # 哨兵必须**再放回去** —— 否则只有一个消费者会收到"结束"信号，
        # 其余的会永远等下去。这是队列编程里最经典的一个坑。
        if item is SENTINEL:
            q.put(SENTINEL)
            return {"consumer": name, "handled": handled}
        handled.append(item["seq"])
        time.sleep(0.005)          # 模拟真实处理耗时
    # unreachable


def main() -> int:
    # 8 个核：4 个消费者 + 1 个生产者 + 3 个 ActorPool 的 actor，留余量。
    # 队列自己不占 CPU（见 actor_options），否则这里 4 个核就够呛。
    ray.init(num_cpus=8, logging_level="warning")
    q_opts = {"num_cpus": 0}          # 队列几乎不耗算力,别占一个核

    try:
        # ---------------------------------------------------------------- ① 基本收发
        print("=" * 68)
        print("① 基本收发：队列可以当参数传给任务")
        print("=" * 68)

        q = Queue(actor_options=q_opts)
        q.put("hello")
        q.put("world")
        print(f"  塞了 2 个，qsize={q.qsize()}")
        print(f"  取出来：{q.get()!r}, {q.get()!r}")
        print(f"  取空后 empty={q.empty()}")

        # 非阻塞与超时语义（与标准库 queue 一致）
        from queue import Empty, Full

        try:
            q.get(block=False)
        except Empty:
            print("  空队列 get(block=False) -> Empty  [OK]")

        q2 = Queue(maxsize=2, actor_options=q_opts)
        q2.put(1)
        q2.put(2)
        try:
            q2.put(3, block=False)
        except Full:
            print("  满队列(maxsize=2) put(block=False) -> Full  [OK]")
        q2.shutdown()
        q.shutdown()

        # ---------------------------------------------------------------- ② 背压
        print()
        print("=" * 68)
        print("② 背压：maxsize 让生产者慢下来，而不是把内存吃光")
        print("=" * 68)

        bounded = Queue(maxsize=8, actor_options=q_opts)
        started = time.time()
        # 生产者想塞 40 个,但它会在第 9 个处挂住 ——
        # 因为没人消费,队列满了。这就是背压。
        producer = produce.remote(bounded, 40)
        time.sleep(0.5)                       # 给它足够时间塞满

        stuck_at = bounded.qsize()
        print(f"  生产者被卡住时,队列里是 {stuck_at} 个（maxsize=8）")
        assert stuck_at == 8, stuck_at

        # 开始消费,生产者立刻解冻
        drained = [bounded.get() for _ in range(40)]
        assert ray.get(producer) == 40
        print(f"  消费者取走后,生产者继续跑完并返回 40（耗时 {time.time() - started:.2f}s）")
        print(f"  取到的序号连续且无重复：{sorted(x['seq'] for x in drained) == list(range(40))}")
        bounded.shutdown()

        # ---------------------------------------------------------------- ③ 多消费者
        print()
        print("=" * 68)
        print("③ 多消费者：每个元素恰好被取走一次（不重不漏）")
        print("=" * 68)

        work_q = Queue(maxsize=16, actor_options=q_opts)
        n_items = 60
        n_consumers = 4

        consumers = [consume.remote(work_q, f"consumer-{i}") for i in range(n_consumers)]
        ray.get(produce.remote(work_q, n_items))
        # 哨兵要放 n_consumers 个 —— 每个消费者各拿走一个
        for _ in range(n_consumers):
            work_q.put(SENTINEL)

        results = ray.get(consumers)
        per_consumer = {r["consumer"]: len(r["handled"]) for r in results}
        all_seqs = sorted(s for r in results for s in r["handled"])

        for name, count in sorted(per_consumer.items()):
            print(f"  {name}: 处理了 {count} 个")
        print(f"  合计 {len(all_seqs)} 个，期望 {n_items} 个")
        print(f"  不重不漏：{all_seqs == list(range(n_items))}")
        assert all_seqs == list(range(n_items))

        work_q.shutdown()

        # ---------------------------------------------------------------- ④ 与 ActorPool 对比
        print()
        print("=" * 68)
        print("④ 什么时候用 Queue，什么时候用 ActorPool")
        print("=" * 68)
        from miniray.util import ActorPool

        @ray.remote
        class Doubler:
            def step(self, x):
                return x * 2

        pool = ActorPool([Doubler.remote() for _ in range(3)])
        doubled = list(pool.map_unordered(lambda a, x: a.step.remote(x), range(6)))
        print(f"  ActorPool（一问一答、结果直接返回）：{sorted(doubled)}")
        print("  区别：ActorPool 适合「请求→响应」；Queue 适合「无固定配对、")
        print("        有缓冲、多消费者抢」的场景（日志、任务分发、流水线）。")
        for actor in pool._all:                                   # noqa: SLF001
            ray.kill(actor)

    finally:
        ray.shutdown()

    print()
    print("全部通过 [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

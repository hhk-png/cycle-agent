"""06 · 容错:重试、worker 崩溃、lineage 重建。

运行::

    python examples/06_fault_tolerance.py

分布式系统里「出错」是常态而不是异常。Ray 的三层容错:

============================  ================================================
故障                           机制
============================  ================================================
函数抛异常                     重试(``max_retries``,默认 3 次)
worker 进程崩溃                换一个 worker 重跑(节点上的其它 worker 不受影响)
对象丢失(节点故障)             **lineage 重建**:重新执行产出它的任务
============================  ================================================

最后一条是 Ray 最有意思的设计:**对象存储不是持久的,但「怎么算出来」是可重放的**。
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
from miniray._private import fault_injection


@ray.remote(max_retries=2)
def flaky(fail_times: int, marker: str) -> str:
    """前 ``fail_times`` 次调用抛异常,之后成功 —— 观察重试。"""
    attempts = 0
    if os.path.exists(marker):
        with open(marker, encoding="utf-8") as handle:
            attempts = int(handle.read())
    attempts += 1
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write(str(attempts))
    if attempts <= fail_times:
        raise RuntimeError(f"第 {attempts} 次尝试失败了")
    return f"第 {attempts} 次尝试成功"


@ray.remote(max_retries=3)
def crash_once(marker: str) -> str:
    """第一次直接干掉 worker 进程(模拟段错误 / 被 OOM killer 杀掉)。"""
    if not os.path.exists(marker):
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("crashed")
        os._exit(9)
    return "在另一个 worker 上重跑成功"


@ray.remote
def expensive(n: int) -> int:
    time.sleep(0.05)
    return sum(i * i for i in range(n))


@ray.remote
def double(x: int) -> int:
    return x * 2


def main() -> None:
    ray.init(num_cpus=4)
    workdir = tempfile.mkdtemp(prefix="miniray-fault-demo-")
    try:
        # ---- 1) 异常重试 ----
        print("== 1) 函数抛异常 → 自动重试 ==")
        marker = os.path.join(workdir, "flaky.marker")
        result = ray.get(flaky.remote(2, marker), timeout=30)
        print(f"  结果:{result}")
        tasks = [t for t in state.list_tasks() if t["name"].endswith("flaky")]
        print(f"  执行次数:{max(t['num_attempts'] for t in tasks)}(1 次原始 + 2 次重试)")

        print("\n== 2) worker 崩溃 → 换一个 worker 重跑 ==")
        marker = os.path.join(workdir, "crash.marker")
        result = ray.get(crash_once.remote(marker), timeout=30)
        print(f"  结果:{result}")
        crashed_workers = [e for e in state.list_workers()]
        print(f"  当前 worker 池:{[(w['pid'], w['state']) for w in crashed_workers]}")

        # ---- 3) lineage 重建 ----
        print("\n== 3) 对象丢失 → lineage 重建 ==")
        ref = expensive.remote(2000)
        expected = sum(i * i for i in range(2000))
        print(f"  第一次取值:{ray.get(ref) == expected}")
        print(f"  对象存储在:{[o['node_id'][:8] for o in state.list_objects() if o['refs'] > 0][:1]}")

        result = fault_injection.lose_objects()  # 模拟节点故障:对象全丢
        print(f"  模拟节点故障:丢了 {len(result['lost'])} 个对象")

        recovered = ray.get(ref, timeout=30)
        print(f"  再次取值:{recovered == expected}(值重新算了一遍,调用方无感)")
        print(f"  重建次数:{state.summarize_objects()['num_reconstructions']}")

        # ---- 4) 依赖链会一起重建 ----
        print("\n== 4) 依赖链的递归重建 ==")
        a = expensive.remote(500)
        b = double.remote(a)
        c = double.remote(b)
        print(f"  链条 c←b←a 的结果:{ray.get(c)}")
        fault_injection.lose_objects()
        print(f"  丢光所有对象后再取 c:{ray.get(c, timeout=60)}(a、b 都重算了)")

        # ---- 5) 没有 lineage 的对象只能报错 ----
        print("\n== 5) 不是任务产出的对象无法重建 ==")
        put_ref = ray.put({"note": "这个对象没有血缘"})
        fault_injection.lose_objects()
        try:
            ray.get(put_ref, timeout=10)
        except ray.ObjectLostError as error:
            print(f"  如实报错:{error}")
            print("  这就是为什么「重要数据」必须自己落盘 —— 对象存储不是数据库")

        print(
            "\n工程建议:\n"
            "  * 任务要**幂等/确定性**,否则重建出来的结果和原来不一样;\n"
            "  * 长任务自己写检查点(checkpoint),不要指望 lineage;\n"
            "  * actor 的状态默认**不恢复**,重启后要自己从检查点加载。"
        )
    finally:
        ray.shutdown()


if __name__ == "__main__":
    main()

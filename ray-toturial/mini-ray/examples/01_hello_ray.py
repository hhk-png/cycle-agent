"""01 · 最小可用示例:任务、ObjectRef、ray.get / ray.put / ray.wait。

运行::

    python examples/01_hello_ray.py

这个文件把「Ray 的四件套」演示一遍。理解这四件事,就理解了 Ray 的 80%:

1. ``@ray.remote`` 把函数变成**远程函数**;
2. ``f.remote(x)`` 是**异步提交**,立刻返回 ObjectRef(未来值);
3. ``ray.get(ref)`` 等结果;
4. ``ray.put(value)`` 把值放进对象存储(大对象零拷贝共享)。
"""

from __future__ import annotations

import time

import os
import sys

# 让示例在**不安装**包的情况下也能直接运行:把项目根目录加进 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import miniray as ray  # noqa: E402  (必须在 sys.path 调整之后导入)


@ray.remote
def square(x: int) -> int:
    return x * x


@ray.remote
def slow_double(x: int, seconds: float = 0.3) -> int:
    time.sleep(seconds)
    return x * 2


def main() -> None:
    ray.init(num_cpus=4)  # 单机 4 核;真实 Ray 里这里是 `ray.init()`
    try:
        # ---- 1) 提交任务:注意 .remote() 立刻返回,不阻塞 ----
        print("== 1) 异步提交 ==")
        start = time.time()
        refs = [square.remote(i) for i in range(8)]
        print(f"  提交 8 个任务耗时 {time.time() - start:.4f}s(没有等待结果)")
        print(f"  类型是 {type(refs[0]).__name__}:{refs[0]}")
        print(f"  取值:{ray.get(refs)}")

        # ---- 2) 并行:4 个 0.3 秒的任务 ≈ 0.3 秒,而不是 1.2 秒 ----
        print("\n== 2) 并行执行 ==")
        start = time.time()
        values = ray.get([slow_double.remote(i) for i in range(4)])
        elapsed = time.time() - start
        print(f"  4 个 0.3s 的任务总耗时 {elapsed:.2f}s(串行需要 1.2s)")
        print(f"  结果:{values}")

        # ---- 3) 依赖:把 ref 直接当参数传 ----
        print("\n== 3) 任务依赖 ==")
        first = square.remote(3)
        second = square.remote(4)
        total = slow_double.remote(first, 0.0)  # 依赖 first
        print(f"  square(3) + square(4) 的中间结果都放在对象存储里")
        print(f"  依赖链结果:{ray.get([total, second])}")

        # ---- 4) ray.put:手动放对象 ----
        print("\n== 4) 对象存储 ==")
        big = list(range(100_000))
        ref = ray.put(big)
        print(f"  put 一个 10 万元素的列表 -> {ref}")
        print(f"  get 回来长度:{len(ray.get(ref))}")

        # ---- 5) ray.wait:等「任意 N 个」完成 ----
        print("\n== 5) ray.wait(做背压的基础) ==")
        pending = [slow_double.remote(i, 0.2) for i in range(4)]
        ready, remaining = ray.wait(pending, num_returns=2, timeout=5)
        print(f"  已完成 {len(ready)} 个,还剩 {len(remaining)} 个")
        print(f"  已完成的取值:{ray.get(ready)}")
    finally:
        ray.shutdown()


if __name__ == "__main__":
    main()

"""04 · 对象存储:numpy 零拷贝、只读语义、溢出与内存管理。

运行::

    python examples/04_object_store.py

这是理解 Ray **性能模型**的关键一章。核心事实:

* 对象存储是**节点级共享内存**;``ray.get`` 一个 numpy 数组**不做拷贝**;
* 代价是对象**不可变** —— 所以拿到的是只读数组(想改先 ``.copy()``);
* 内存不够时先**溢出到磁盘**(spill),而不是丢数据;
* 引用计数归零的对象才会被回收,而**零拷贝视图会把对象钉住**(pin)。
"""

from __future__ import annotations

import os
import time

import numpy as np

import sys

# 让示例在**不安装**包的情况下也能直接运行:把项目根目录加进 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import miniray as ray  # noqa: E402  (必须在 sys.path 调整之后导入)
from miniray import state


@ray.remote
def make_matrix(rows: int, cols: int):
    return np.arange(rows * cols, dtype="float64").reshape(rows, cols)


@ray.remote
def sum_matrix(matrix):
    return float(matrix.sum())


@ray.remote
def mutate_attempt(matrix):
    """试图改一个从对象存储拿到的数组 —— 会被只读标志挡住。"""
    try:
        matrix[0, 0] = 1.0
        return "居然改成功了(不应该发生)"
    except ValueError as error:
        return f"被挡住了:{error}"


def main() -> None:
    ray.init(num_cpus=4, object_store_memory=64 * 1024 * 1024)
    try:
        # ---- 1) 零拷贝 ----
        print("== 1) 零拷贝 ==")
        ref = make_matrix.remote(1000, 1000)  # 8MB
        matrix = ray.get(ref)
        print(f"  任务返回的矩阵:{matrix.shape} {matrix.dtype} {matrix.nbytes / 1e6:.1f}MB")
        print(f"  只读? {not matrix.flags.writeable}(对象存储里的对象不可变)")
        print(f"  在 worker 里试图写入:{ray.get(mutate_attempt.remote(ref))}")

        # ---- 2) 同一份数据被多个任务共享 ----
        print("\n== 2) 多个消费者共享同一份内存 ==")
        start = time.time()
        totals = ray.get([sum_matrix.remote(ref) for _ in range(4)])
        print(f"  4 个任务各求和一次,耗时 {time.time() - start:.3f}s")
        print(f"  结果一致:{totals}(数据只存了一份,读的时候没有拷贝)")

        # ---- 3) put 的对象也被钉住 ----
        print("\n== 3) ray.put 与 pin ==")
        local = np.ones((500, 500), dtype="float64")
        put_ref = ray.put(local)
        view = ray.get(put_ref)
        print(f"  put 后拿到视图:{view.shape},只读={not view.flags.writeable}")
        before = state.summarize_objects()
        del view  # 视图没了 → pin 释放
        time.sleep(0.2)
        after = state.summarize_objects()
        print(f"  对象数 {before['num_objects']} → {after['num_objects']}(引用释放后仍留在存储里,")
        print("   因为 mini-ray 只在内存压力下回收 —— 真实 Ray 会更激进地删)")

        # ---- 4) 溢出:写满时先落盘,而不是丢数据 ----
        print("\n== 4) 对象溢出(spill) ==")

        @ray.remote
        def big_blob(size_mb: float):
            return b"z" * int(size_mb * 1024 * 1024)

        refs = [big_blob.remote(20) for _ in range(4)]  # 80MB > 64MB 的存储
        values = ray.get(refs)
        stats = state.summarize_objects()
        print(f"  4×20MB 的数据在 64MB 存储里全部取回:{all(len(v) == 20 * 1024 * 1024 for v in values)}")
        print(f"  已用 {stats['used_bytes'] / 1e6:.1f}MB / 容量 {stats['capacity_bytes'] / 1e6:.0f}MB")
        print(f"  溢出次数:{stats['counters']['spilled']},恢复次数:{stats['counters']['restored']}")
        print("  提示:溢出目录在 session 目录下,进程退出后自动清理")

        # ---- 5) 内存管理建议 ----
        print("\n== 5) 什么时候会把内存吃爆 ==")
        leaked = [ray.put(np.zeros(1_000_000)) for _ in range(8)]  # 故意持有引用
        print(f"  持有 {len(leaked)} 个 8MB 对象的引用 → 它们不会被回收")
        print(f"  查看:{state.summarize_objects()['used_bytes'] / 1e6:.1f}MB")
        del leaked
        print("  把引用丢掉之后再检查(引用计数归零):")
        time.sleep(0.3)
        print(f"  {state.summarize_objects()['used_bytes'] / 1e6:.1f}MB")
        print(
            "\n  排查内存问题的套路:\n"
            "    from miniray import state; state.list_objects()\n"
            "  找 refs 一直 > 0 的对象 —— 那就是被某处漏掉的引用钉住的。"
        )
        _ = os
    finally:
        ray.shutdown()


if __name__ == "__main__":
    main()

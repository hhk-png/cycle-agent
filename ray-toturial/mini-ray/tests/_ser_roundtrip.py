"""按值序列化的端到端验证脚本(由 test_serialization.py 调用)。

这个脚本刻意**在 __main__ 里定义**函数和类 —— 这正是真实用户脚本的样子
(``python train.py``),也是按引用序列化一定会失败的场景。然后:

1. 本进程(python _ser_roundtrip.py)把函数/类序列化成 bytes,写进临时文件;
2. spawn 一个子进程,让它在**全新的解释器**里反序列化并真正调用它们;
3. 打印 ``RESULT: <json>`` 给父进程断言。
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import tempfile

# 确保子进程能 import miniray(子进程继承 sys.path,但显式加上更稳)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from miniray import serialization  # noqa: E402

# ---------------------------------------------------------------------------
# 以下内容刻意定义在 __main__ 里
# ---------------------------------------------------------------------------

MODULE_LEVEL_FACTOR = 3  # 全局变量:必须跟着函数一起"按值"传过去


def plain_function(x):
    """最普通的模块级函数(但它在 __main__ 里)。"""
    return x * 2


def uses_global(x):
    """引用全局变量的函数 —— 按值序列化必须把 MODULE_LEVEL_FACTOR 带上。"""
    return x * MODULE_LEVEL_FACTOR


def recursive_fib(n):
    """递归函数:重建后 globals 里必须能找到它自己。"""
    if n < 2:
        return n
    return recursive_fib(n - 1) + recursive_fib(n - 2)


def make_adder(n):
    """闭包:返回的函数捕获了 n。"""
    return lambda x: x + n


class Counter:
    """__main__ 里定义的类 —— actor 的典型形态。"""

    def __init__(self, start=0):
        self.value = start

    def inc(self, by=1):
        self.value += by
        return self.value

    def __repr__(self):
        return f"Counter({self.value})"


class Derived(Counter):
    """继承 + 方法覆盖,验证基类/子类一起按值传。"""

    def inc(self, by=2):
        return super().inc(by) * 10


def build_payload():
    import numpy as np

    return {
        "fn": plain_function,
        "global_fn": uses_global,
        "fib": recursive_fib,
        "adder": make_adder(100),
        "cls": Counter,
        "derived": Derived,
        "data": np.arange(6, dtype="float64").reshape(2, 3),
        "nested": {"a": [make_adder(1), lambda s: s.upper()]},
    }


# ---------------------------------------------------------------------------
# 子进程:反序列化并调用
# ---------------------------------------------------------------------------


def child(path: str) -> None:
    import numpy as np

    with open(path, "rb") as fh:
        payload = serialization.loads(fh.read())

    out = {}
    out["plain"] = payload["fn"](21)
    out["global"] = payload["global_fn"](5)  # 3 * 5
    out["fib"] = payload["fib"](10)
    out["adder"] = payload["adder"](1)  # 100 + 1
    out["nested0"] = payload["nested"]["a"][0](2)
    out["nested1"] = payload["nested"]["a"][1]("abc")

    counter = payload["cls"](10)
    out["cls"] = [counter.inc(), counter.inc(5)]

    derived = payload["derived"](1)
    out["derived"] = derived.inc()  # (1+2)*10

    arr = payload["data"]
    out["ndarray"] = [arr.shape, arr.dtype.str, float(arr.sum())]

    # 重建出来的类必须还能再 pickle(用于 actor handle 传递等场景)
    out["repicklable"] = serialization.loads(serialization.dumps(payload["cls"]))(7).inc()

    print("RESULT:" + json.dumps(out), flush=True)


if __name__ == "__main__":
    payload = build_payload()
    blob = serialization.dumps(payload)
    with tempfile.NamedTemporaryFile("wb", suffix=".pkl", delete=False) as fh:
        fh.write(blob)
        path = fh.name

    ctx = mp.get_context("spawn")
    proc = ctx.Process(target=child, args=(path,))
    proc.start()
    proc.join(60)
    if proc.exitcode != 0:
        print(f"CHILD FAILED exitcode={proc.exitcode}", flush=True)
        sys.exit(1)
    os.unlink(path)

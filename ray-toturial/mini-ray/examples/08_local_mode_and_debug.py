"""08 · local_mode 与调试:让分布式代码能被断点调试。

运行::

    python examples/08_local_mode_and_debug.py

**这是 Ray 最被低估的功能**。分布式代码之所以难调,是因为:

* 断点只能停在 driver 里;
* ``print`` 在 worker 进程里,和 driver 的输出交错;
* 异常堆栈跨进程截断,看不到真正出错的那几行。

``ray.init(local_mode=True)`` 把任务/actor 都放进 driver 进程执行,
于是上面三个问题一次性消失。**代价是没有并行、全局状态会互相污染**,
所以它只适合调试,不要用于跑真实负载。

这个示例演示:

1. local_mode 下任务跑在同一个进程(用 pid 验证);
2. 异常堆栈是**连续**的,能直接看到用户代码那一行;
3. 同一个脚本,去掉 local_mode 就是分布式执行(代码不用改)。
"""

from __future__ import annotations

import os
import traceback

import sys

# 让示例在**不安装**包的情况下也能直接运行:把项目根目录加进 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import miniray as ray  # noqa: E402  (必须在 sys.path 调整之后导入)


@ray.remote
def where_am_i() -> dict:
    return {"pid": os.getpid(), "cwd": os.getcwd()}


@ray.remote
def buggy_division(a: int, b: int) -> float:
    # 这里故意写一个会被 0 除的表达式:在 local_mode 下你能直接看到这一行
    return a / b


@ray.remote
class Session:
    """在 local_mode 下,actor 就是 driver 进程里的一个普通对象。"""

    def __init__(self):
        self.events = []

    def record(self, event: str) -> int:
        self.events.append(event)
        return len(self.events)


def run(local_mode: bool) -> None:
    label = "local_mode=True" if local_mode else "分布式(默认)"
    print(f"\n===== {label} =====")
    ray.init(num_cpus=2, local_mode=local_mode, logging_level="warning")
    try:
        info = ray.get(where_am_i.remote())
        same = info["pid"] == os.getpid()
        print(f"  任务进程 pid={info['pid']},driver pid={os.getpid()} "
              f"→ {'同一个进程' if same else '另一个进程'}")

        session = Session.remote()
        print(f"  actor 记录 3 个事件:{ray.get([session.record.remote(f'e{i}') for i in range(3)])}")

        # 异常:观察堆栈的完整程度
        try:
            ray.get(buggy_division.remote(1, 0))
        except ray.RayTaskError as error:
            lines = [line for line in error.traceback_str.splitlines() if "buggy_division" in line]
            print(f"  异常类型:{type(error.cause).__name__}: {error.cause}")
            print(f"  堆栈里能定位到用户代码的那一行:{lines[-1].strip() if lines else '(没找到)'}")
    finally:
        ray.shutdown()


def main() -> None:
    print("调试建议:")
    print("  1. 逻辑复杂、要打断点时 → local_mode=True")
    print("  2. 要观察真实并行/性能时 → 关掉它(默认)")
    print("  3. 分布式模式下查问题 → print 会出现在同一个终端里(worker 直接继承)")

    run(local_mode=True)
    run(local_mode=False)

    print(
        "\n真实 Ray 里的对应能力:\n"
        "  * ray.init(local_mode=True) —— 完全一样\n"
        "  * RAY_DEBUG=1 + ray debug —— 进入失败任务的 pdb\n"
        "  * ray timeline / ray state / dashboard —— 看「发生了什么」\n"
        "  * 注意:RLlib 在新 API stack 里已经移除了 local_mode 支持,\n"
        "    但 ray core 的 local_mode 仍然可用(截至 Ray 2.58)"
    )
    _ = traceback


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
r"""03 —— 后台合并 + OPTIMIZE ... FINAL:part 数量是怎么塌下来的。

示例 02 说"一次 INSERT 一个 part"。那写 20 次就有 20 个 part,查询越来越慢吗?
不会 —— **后台合并线程**会把同分区的小 part 合并成大 part。这个示例让你亲眼看到
这个塌缩过程。

为了让"合并前"和"合并后"两次观察**可复现**,我们做了两件事:

1. ``SYSTEM STOP MERGES`` 先关掉后台合并 —— 否则它会在你数之前就把 part 吃掉;
2. 写完 20 个小 part 后,用 ``OPTIMIZE TABLE ... FINAL`` **手动**触发一次
   "把整个分区合成一个 part"的合并。

输出的三个数字就是全部重点:

.. code-block:: text

   插入后(后台暂停)    20 个 part,每个 1 行
   OPTIMIZE ... FINAL   同步合并,level 从 0 直接跳到 4
   合并后               1 个 part,20 行

为什么 level 跳到 4 而不是 1? 因为合并是**成对**进行的:20 → 10 → 5 → … 每合一次
level 加一(合并后的 level = 参与合并的 part 里最大的 level 再 +1)。20 个 1 行的
part 只需要 4 层就能汇成一个,这正是"合并树"这个名字的来历。

最后 ``wait_for_merges()`` 那一段演示的是**不靠 OPTIMIZE**的自动合并:
先 ``SYSTEM START MERGES`` 把后台打开,它在 1 秒的轮询间隔里自己就把 part 收了。
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from minich.execution.context import Context  # noqa: E402

BATCHES = 20


def part_count(ctx: Context) -> int:
    return ctx.execute("SELECT count() FROM system.parts WHERE active").scalar()


def main() -> None:
    print("=" * 72)
    print("03 · 后台合并与 OPTIMIZE FINAL —— 让 20 个 part 塌成 1 个")
    print("=" * 72)

    data_dir = tempfile.mkdtemp(prefix="minich03-")
    ctx = Context(data_path=data_dir)
    try:
        # merge_selecting_sleep_ms 默认 1000ms;这里先靠 STOP MERGES 冻结后台。
        ctx.execute("CREATE TABLE roll (id UInt64, v UInt64) "
                    "ENGINE = MergeTree ORDER BY id")
        ctx.execute("SYSTEM STOP MERGES roll")

        # 20 次小批量 INSERT —— 每次一个新 part。
        for i in range(BATCHES):
            ctx.execute(f"INSERT INTO roll VALUES ({i}, {i * 10})")

        before = part_count(ctx)
        print(f"\n[BEFORE] 插入 {BATCHES} 批(后台合并已暂停)")
        print(f"         活跃 part 数 = {before}")
        print(ctx.execute("SELECT name, rows, level FROM system.parts "
                          "WHERE active ORDER BY name LIMIT 6").text())
        print(f"         …… 共 {before} 个,每个 1 行、level 都是 0")

        # OPTIMIZE ... FINAL = "把这棵合并树压平成一个 part"(同步执行)。
        ctx.execute("OPTIMIZE TABLE roll FINAL")
        after = part_count(ctx)
        rows = ctx.execute("SELECT sum(rows) FROM system.parts WHERE active").scalar()
        merged_name, merged_level = ctx.execute(
            "SELECT name, level FROM system.parts WHERE active").rows()[0]

        print(f"\n[AFTER]  OPTIMIZE TABLE roll FINAL")
        print(f"         活跃 part 数 = {after}({before} → {after})")
        print(f"         总行数       = {rows}(一行都不能少)")
        print(ctx.execute("SELECT name, rows, level FROM system.parts "
                          "WHERE active ORDER BY name").text())
        print(f"""
读法:name = {merged_name!r},里面的 level 字段从 0 变成了 {merged_level} ——
      说明这份数据是被 {merged_level} 层合并一层层汇总上来的,而不是"一次性重写"。
      合并是**不可变 part 的替换**:新 part 落盘、旧 part 变 obsolete,
      任何时刻都不会原地修改(这正是 ClickHouse 敢在写入时并发查询的底气)。

现在把后台合并打开,看它自己(按 merge_selecting_sleep_ms 轮询)收 part:
""")

        # 再来一轮,这次让后台线程自己合 —— 用 wait_for_merges() 等它完成。
        ctx.execute("CREATE TABLE roll2 (id UInt64) ENGINE = MergeTree ORDER BY id "
                    "SETTINGS merge_selecting_sleep_ms = 100")
        for i in range(12):
            ctx.execute(f"INSERT INTO roll2 VALUES ({i})")
        n_before = part_count(ctx) - after
        engine = ctx.catalog.get_table("default", "roll2").engine
        ok = engine.wait_for_merges(timeout=15)
        n_after = part_count(ctx) - after
        print(f"\n[BACKGROUND] roll2:插入后 {n_before} 个 part,"
              f"wait_for_merges() -> {ok},合并后 {n_after} 个 part")
        print("说明:后台合并是**异步**的,所以示例里要 wait 它;")
        print("      写生产代码时不能像这样'数 part 等一会',要靠 system.parts 观察。")
    finally:
        ctx.shutdown()


if __name__ == "__main__":
    main()

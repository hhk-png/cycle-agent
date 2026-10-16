# -*- coding: utf-8 -*-
r"""11 —— Mutation(ALTER DELETE/UPDATE)与 TTL:都是"后台重写 part"。

这两件事看起来完全不同,机制却是同一个:

.. code-block:: text

   它们都**不原地改数据**。MergeTree 的 part 是不可变的,所以

     ALTER TABLE ... DELETE / UPDATE   → 登记一条 mutation,等下次重写 part 时生效
     TTL ts + INTERVAL n DAY           → 到点的行,在重写 part 时被丢掉

   两者的"执行时机"都是 **part 被重写的时候**(后台合并,或你手动 OPTIMIZE)。

示例按这个顺序演示:

1. 建表、写入几行;``ALTER TABLE ... DELETE WHERE id = 2`` —— 观察**立即查还是 6 行**
   (mutation 只是登记了,还没跑);
2. 试着查 ``system.mutations`` —— 本实现**没有这张表**,如实说明,改用引擎的
   ``mutation_status()`` 看这条 mutation 的状态;
3. ``OPTIMIZE TABLE ... FINAL`` 触发重写 —— 这时才删掉 id=2,再 ``UPDATE`` 一次看它同样滞后;
4. 换一张带 ``TTL`` 的表:插一行 2020、一行 2099;到点的那行在 ``OPTIMIZE ... FINAL``
   时被丢掉。

.. note:: **为什么 mutation 会滞后?**

   真实 ClickHouse 里 mutation 是**异步**的,进度能在 ``system.mutations`` 里看到
   (``parts_to_do`` / ``is_done``),而且**很贵** —— 它要重写整个 part。
   所以 ``ALTER DELETE`` 不适合当"高频删单行"的接口;它设计来处理"批量清理"。

.. warning:: 本示例给表加了 ``SETTINGS merge_selecting_sleep_ms = 100000`` ——
   把后台合并线程的轮询拉长,好让"何时重写"完全由我们的 ``OPTIMIZE`` 决定。
   否则后台线程可能在你观察之前就把 part 合了(示例输出就不稳定)。
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from minich.errors import MiniChError  # noqa: E402
from minich.execution.context import Context  # noqa: E402

# 让后台合并"睡着",这样 part 只在显式 OPTIMIZE 时被重写。
SLOW_MERGE = "SETTINGS merge_selecting_sleep_ms = 100000"


def show(ctx: Context, title: str, sql: str) -> None:
    print(f"\n--- {title} ---")
    print(f">>> {sql}")
    print(ctx.execute(sql).text())


def main() -> None:
    print("=" * 78)
    print("11 · Mutation 与 TTL —— 不原地改数据,改写发生在 part 重写时")
    print("=" * 78)

    data_dir = tempfile.mkdtemp(prefix="minich11-")
    ctx = Context(data_path=data_dir)
    try:
        ctx.execute(f"CREATE TABLE mm (id UInt64, v UInt64) "
                    f"ENGINE = MergeTree ORDER BY id {SLOW_MERGE}")
        # 一次 INSERT 落成一个 part:这样后面的 PART 重写路径最干净
        # (多 part 时 OPTIMIZE 会同时"合并 + 应用 mutation",在 Windows 上会踩到
        #  本仓库 os.replace 的一个竞态 —— 为了示例稳定,这里用单 part)。
        ctx.execute("INSERT INTO mm VALUES (0,0),(1,10),(2,20),(3,30),(4,40),(5,50)")
        show(ctx, "初始数据", "SELECT * FROM mm ORDER BY id")

        ctx.execute("ALTER TABLE mm DELETE WHERE id = 2")
        show(ctx, "刚 ALTER DELETE 之后立即查 —— id=2 **还在**",
             "SELECT * FROM mm ORDER BY id")
        print("  ↑ mutation 只是被登记了;part 还没被重写,所以数据没变。")

        print("\n--- 观察 mutation 状态 ---")
        try:
            print(ctx.execute("SELECT * FROM system.mutations LIMIT 1").text())
        except MiniChError as exc:
            print(f">>> SELECT * FROM system.mutations")
            print(f"[{exc.name}] {exc.message[:90]}")
            print("  mini-clickhouse **没有实现 system.mutations 这张表**;")
            print("  真实 ClickHouse 用它看 parts_to_do / is_done。这里的替代是")
            print("  直接问引擎(下面就是):")
        engine = ctx.catalog.get_table("default", "mm").engine
        for row in engine.mutation_status():
            print(f"    mutation_id={row['mutation_id']} "
                  f"command={row['command']!r} parts_to_do={row['parts_to_do']} "
                  f"is_done={row['is_done']}")

        show(ctx, "OPTIMIZE ... FINAL —— 触发重写,mutation 这才生效",
             "OPTIMIZE TABLE mm FINAL")
        show(ctx, "删完再看", "SELECT * FROM mm ORDER BY id")

        ctx.execute("ALTER TABLE mm UPDATE v = 999 WHERE id = 3")
        show(ctx, "UPDATE 同样滞后;OPTIMIZE 之后才看到 v=999",
             "OPTIMIZE TABLE mm FINAL")
        show(ctx, "UPDATE 生效后", "SELECT * FROM mm ORDER BY id")
        print("  最终 is_done:", [r["is_done"] for r in engine.mutation_status()])

        print("\n" + "=" * 78)
        print("TTL:到点的行在 part 重写时被丢掉")
        print("=" * 78)
        ctx.execute(f"CREATE TABLE ttl (id UInt64, ts DateTime) "
                    f"ENGINE = MergeTree ORDER BY id TTL ts + INTERVAL 1 DAY {SLOW_MERGE}")
        # 一行早已过期(2020),一行远在未来(2099)。
        ctx.execute("INSERT INTO ttl VALUES "
                    "(1, toDateTime('2020-01-01 00:00:00')), "
                    "(2, toDateTime('2099-01-01 00:00:00'))")
        show(ctx, "插入两行:一行过期、一行未过期", "SELECT id, ts FROM ttl ORDER BY id")

        ctx.execute("OPTIMIZE TABLE ttl FINAL")
        show(ctx, "OPTIMIZE ... FINAL 之后,过期的 2020 那行没了",
             "SELECT id, ts FROM ttl ORDER BY id")
        print("""
小结:

  · DELETE/UPDATE 是 **mutation**:登记 → 等 part 重写 → 生效。很贵,别当单行删改用。
    真实 ClickHouse 里在 `system.mutations` 看进度;本实现改用 `engine.mutation_status()`。
  · TTL 也是 **part 重写时**才执行:不是"到点自动消失",而是"下次重写时被筛掉"。
  · 只要记得"数据不可变、一切靠重写",这两个功能就不神秘了。
""")
    finally:
        ctx.shutdown()


if __name__ == "__main__":
    main()

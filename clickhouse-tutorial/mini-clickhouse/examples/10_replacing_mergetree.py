# -*- coding: utf-8 -*-
r"""10 —— ReplacingMergeTree:"最终一致的去重",不是唯一约束。

很多人第一次用 ReplacingMergeTree 会以为"插了重复主键就会被去重"。**不会。**
它的真正语义是:

.. code-block:: text

   合并发生之前,重复行**存在**;合并之后,按排序键分组,每组只保留
   "版本列最大"的那一行。所以去重是**最终(eventual)**的,而且是**碰运气**的
   ——取决于后台合并在什么时候、把哪些 part 合到一起。

本示例按这个顺序把语义钉死:

1. 三次 INSERT,给同一个键 ``k=1`` 写入两个版本 ``ver=1('old')`` / ``ver=2('new')``;
2. ``SELECT`` 直接读 —— 你会看到**两行都在**(去重还没发生);
3. ``SELECT ... FINAL`` —— 语义上应该触发"读时去重",本示例如实打印它现在的结果
   (当前 mini-clickhouse 版本里它**没有**折叠,这点会明确标注);
4. ``OPTIMIZE TABLE ... FINAL`` —— 强制合并,这时才真的去重;
5. 再去查 ``system.parts``,看 part 数量和行的对应关系。

记住一句话:**ReplacingMergeTree 保证的是"合并后不重复",不是"插入时不重复"。**
要强唯一性得在应用层做,或者用 ``SELECT ... FINAL`` / 聚合兜底。
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from minich.execution.context import Context  # noqa: E402


def show(ctx: Context, title: str, sql: str) -> None:
    print(f"\n--- {title} ---")
    print(f">>> {sql}")
    print(ctx.execute(sql).text())


def main() -> None:
    print("=" * 78)
    print("10 · ReplacingMergeTree —— 去重是'合并时'发生的,不是'插入时'")
    print("=" * 78)

    data_dir = tempfile.mkdtemp(prefix="minich10-")
    ctx = Context(data_path=data_dir)
    try:
        # ReplacingMergeTree(ver):合并时按 ORDER BY 分组,保留 ver 最大的行。
        ctx.execute("CREATE TABLE latest (k UInt64, ver UInt64, v String) "
                    "ENGINE = ReplacingMergeTree(ver) ORDER BY k")

        # 三次 INSERT = 三个 part。k=1 被写了两次(两个版本)。
        ctx.execute("INSERT INTO latest VALUES (1, 1, 'old')")
        ctx.execute("INSERT INTO latest VALUES (1, 2, 'new'), (2, 1, 'only')")
        ctx.execute("INSERT INTO latest VALUES (3, 1, 'third')")

        show(ctx, "插完之后:part 数量",
             "SELECT count() AS n_parts, sum(rows) AS n_rows FROM system.parts WHERE active")

        show(ctx, "直接 SELECT —— 同一把键 K=1 的两行**都还在**",
             "SELECT * FROM latest ORDER BY k, ver")
        print("  ↑ 没有去重。这就是'最终一致':合并之前,重复就是重复。")

        show(ctx, "SELECT ... FINAL(本实现里的实情)",
             "SELECT * FROM latest FINAL ORDER BY k, ver")
        print("  ⚠ 预期上 FINAL 应当在读时按版本折叠成 k=1 的 'new' 一行;")
        print("    但当前 mini-clickhouse 的 SQL 读取路径**没有把这个 FINAL 接到")
        print("    引擎的折叠逻辑上**,所以这里仍然看到两行。")
        print("    (直接调 engine.read(final=True) 是能折叠的 —— 差异出在解释器这一层。)")
        print("    不假装它生效:真去重要靠下面的 OPTIMIZE ... FINAL。")

        show(ctx, "OPTIMIZE TABLE latest FINAL —— 强制合并,这时才去重",
             "OPTIMIZE TABLE latest FINAL")
        show(ctx, "合并后再 SELECT",
             "SELECT * FROM latest ORDER BY k")
        print("  ↑ k=1 只剩 ver=2 的 'new' —— 版本大的赢了。")

        show(ctx, "去重后的 part 情况",
             "SELECT name, rows, level FROM system.parts WHERE active ORDER BY name")
        print("  ↑ 4 行输入 → 3 行存活(2 个旧的 k=1 合成 1 个)。")

        print("""
小结:

  · ReplacingMergeTree = MergeTree + "合并时按排序键保留最大版本"。
    它用 ORDER BY 当分组键,用参数列当版本;没有版本列时默认保留最后写入的。
  · 去重是**最终**的:查询可能短暂看到旧版本 —— 这点和"数据库唯一约束"是两回事。
  · 需要"读的时候就是去重后的状态",要么 `OPTIMIZE ... FINAL`,要么查询里兜底:
    比如 `SELECT k, argMax(v, ver) FROM latest GROUP BY k`。
""")
    finally:
        ctx.shutdown()


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
r"""09 —— 窗口函数:三个排名函数在"有并列"时的区别,以及帧(frame)是什么。

窗口函数和 GROUP BY 最大的不同是:**它不折叠行**。每一行都还在,只是旁边多贴了
一列"相对于一个窗口算出来的值"。这个"窗口"就是帧(frame),它决定了"算当前这行时
能看见哪些行"。

数据里故意放了**并列值**(两行 v=10)和**空档**(20 之后跳到 30),因为排名函数
的差别全在这些地方露出来:

.. code-block:: text

   行   v     row_number  rank  dense_rank
   b   10       1         1      1        ← 并列的第一行
   a   10       2         1      1        ← 并列:rank 停住,dense_rank 也停住
   c   20       3         3      2        ← rank 跳过 2,dense_rank 只 +1
   d   30       4         4      3

   row_number  永远连续递增(1,2,3,4)—— 并列也分先后
   rank        并列同名,之后**跳号**(1,1,3,4)
   dense_rank  并列同名,之后**不跳号**(1,1,2,3)

然后演示两类"带帧"的窗口:

* ``lagInFrame(v)`` —— 取帧内**上一行**的 v(第一行没有上一行,给默认值 0);
* ``sum(v) OVER (ORDER BY v ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)``
  —— **累计求和(running total)**:每行算的是"从窗口开头到当前行"的和。

.. note::

   **当前行之后的行看不见** —— 这叫"帧"。

   ⚠ mini-clickhouse 里 ``RANGE`` 帧被当成 ``ROWS`` 处理(不按值分组),
   所以在并列行上累计值会和真实 ClickHouse 略有差别。示例里用 ROWS,
   并如实说明这个差异。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from minich.execution.context import Context  # noqa: E402


def main() -> None:
    print("=" * 82)
    print("09 · 窗口函数 —— 有并列时 row_number / rank / dense_rank 分道扬镳")
    print("=" * 82)

    ctx = Context()
    try:
        ctx.execute("CREATE TABLE w (k String, v Int32) ENGINE = Memory")
        ctx.execute("INSERT INTO w VALUES "
                    "('a', 10), ('b', 10), ('c', 20), ('d', 30), ('e', 30), ('f', 40)")

        print("\n数据(注意 v=10 和 v=30 各有一对并列):")
        print(ctx.execute("SELECT k, v FROM w ORDER BY v, k").text())

        sql = ("SELECT k, v, "
               "row_number() OVER (ORDER BY v) AS rn, "
               "rank()       OVER (ORDER BY v) AS rk, "
               "dense_rank() OVER (ORDER BY v) AS dr "
               "FROM w ORDER BY v, k")
        print("--- 三个排名函数,同一个窗口 ORDER BY v ---")
        print(f">>> {sql}")
        print(ctx.execute(sql).text())
        print("""
看并列点(v=10 和 v=30):

  · row_number 无视并列,一路 1,2,3,4,5,6 —— 它是"行号",不是"名次";
  · rank       并列的两行同为 1,然后**跳过**被占掉的名次(…→ 3);
  · dense_rank 并列的两行同为 1,下一名只 +1(→ 2,不跳号)。

选哪个取决于问题:要"每个人排第几且不能并列"用 row_number;
要"成绩排名,并列算同名次"用 rank 或 dense_rank。
""")

        sql = ("SELECT k, v, "
               "lagInFrame(v) OVER (ORDER BY v) AS prev_v, "
               "sum(v) OVER (ORDER BY v ROWS BETWEEN UNBOUNDED PRECEDING "
               "AND CURRENT ROW) AS running_sum "
               "FROM w ORDER BY v, k")
        print("--- lagInFrame + 累计求和(ROWS 帧)---")
        print(f">>> {sql}")
        print(ctx.execute(sql).text())
        print("""
读法:

  · prev_v  = "帧内上一行的 v"。第一行没有上一行,填的是**类型默认值 0**。
    这就是 lagInFrame 和普通 lag 在 ClickHouse 里的共同点(本实现两者等价)。
  · running_sum = 从窗口开头累计到当前行的和:10,20,40,70,100,140。
    这里的帧是 `ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`,
    也就是"从头到当前行"。

帧(frame)就是"当前行能看见的范围"。最常见的几种:

    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW   → 累计(从头到我这行)
    ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING           → 前后各一行的滑动窗
    ROWS BETWEEN 2 PRECEDING AND CURRENT ROW           → 近 3 行移动平均

⚠ 本实现把 `RANGE` 当 `ROWS` 处理(不按值分组),所以在并列行上算累计会与
  真实 ClickHouse 不同。要精确就显式写 ROWS,别依赖 RANGE 的默认语义。
""")
    finally:
        ctx.shutdown()


if __name__ == "__main__":
    main()

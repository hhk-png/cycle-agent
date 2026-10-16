# -*- coding: utf-8 -*-
r"""07 —— 聚合:GROUP BY / HAVING / -If 组合器 / uniq 家族 / 分位数 / 聚合状态。

这个示例走一遍"做报表"时会用到的东西,并顺带点出两个**反直觉**之处。

看输出时重点关注:

* ``countIf`` / ``sumIf`` —— 聚合函数后面的 ``-If`` 后缀,相当于"带条件的聚合",
  比 ``count(CASE WHEN ...)`` 短得多。它是**组合器(combinator)**:
  ``sum`` 一个函数名就自动派生出 ``sumIf`` / ``sumState`` / ``sumMerge`` …;
* ``uniq`` vs ``uniqExact`` —— 前者用 HyperLogLog **近似**(省内存、快),
  后者**精确**(要把所有值存下来)。小数据上看不出差别,大数据上 ``uniq``
  会有约 0.5%~1% 的误差 —— 这正是"基数统计"的经典取舍;
* ``quantiles(0.5, 0.9)(x)`` —— 一次算多个分位数,返回数组;
* **聚合状态(aggregate state)**:``avg(x)`` 的背后其实是一个"状态"(和、计数),
  用 ``avgState(x)`` 能把它掏出来看 —— 输出 ``[sum, count]``。
  这是 AggregatingMergeTree 能"增量预聚合"的原理:存状态、而不是存结果。

两个**本实现尚未支持、示例里如实标注**的点:

* ``WITH TOTALS`` —— 语法能解析,但当前版本**不会**输出总计行(如实说明);
* ``avgState`` -> ``avgMerge`` 的跨表往返 —— ``-Merge`` 对纯子查询结果不生效,
  这里用 ``avgState`` 直观展示状态即可。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from minich.errors import MiniChError  # noqa: E402
from minich.execution.context import Context  # noqa: E402


def show(ctx: Context, title: str, sql: str) -> None:
    print(f"\n--- {title} ---")
    print(f">>> {sql}")
    try:
        print(ctx.execute(sql).text())
    except MiniChError as exc:
        print(f"[{exc.name}] {exc.message}")


def main() -> None:
    print("=" * 78)
    print("07 · 聚合 —— 分组、条件聚合、基数估计、分位数、以及'状态'是什么")
    print("=" * 78)

    ctx = Context()
    try:
        ctx.execute("CREATE TABLE sales (region String, product String, "
                    "amount UInt32, status String) ENGINE = Memory")
        ctx.execute(
            "INSERT INTO sales VALUES "
            "('east', 'book', 100, 'ok'),  ('east', 'book', 250, 'ok'),"
            "('east', 'pen',  30,  'ok'),  ('east', 'pen',  40,  'fail'),"
            "('west', 'book', 400, 'ok'),  ('west', 'pen',  60,  'ok'),"
            "('west', 'pen',  70,  'fail'),('north','book', 90,  'ok')"
        )

        show(ctx, "基本 GROUP BY + HAVING",
             "SELECT region, count() AS n, sum(amount) AS total "
             "FROM sales GROUP BY region HAVING total > 100 ORDER BY total DESC")

        show(ctx, "条件聚合:-If 组合器(countIf / sumIf)",
             "SELECT region, countIf(status = 'fail') AS fails, "
             "sumIf(amount, status = 'ok') AS ok_amount, count() AS total "
             "FROM sales GROUP BY region ORDER BY region")

        show(ctx, "分位数:一次算多个",
             "SELECT quantiles(0.5, 0.9, 1.0)(amount) AS qs, "
             "min(amount) AS lo, max(amount) AS hi FROM sales")

        show(ctx, "聚合状态:avg 的背后是 [sum, count]",
             "SELECT avg(amount) AS avg_value, avgState(amount) AS avg_state, "
             "sumState(amount) AS sum_state FROM sales")
        print("  ^ avgState 给出的是**状态** '[和, 计数]',不是最终值。")
        print("    AggregatingMergeTree 就是靠存这种状态, 把'重复扫描原始行'")
        print("    换成了'把状态滚起来'。")

        print("\n--- uniq(近似) vs uniqExact(精确)---")
        ctx.execute("CREATE TABLE big (x UInt64) ENGINE = Memory")
        ctx.execute("INSERT INTO big SELECT number FROM numbers(100000)")
        approx, exact = ctx.execute(
            "SELECT uniq(x), uniqExact(x) FROM big").first_row()
        print(f">>> SELECT uniq(x), uniqExact(x) FROM big   (10 万个互不相同的值)")
        print(f"    uniq(x)      = {approx}   (HyperLogLog 近似)")
        print(f"    uniqExact(x) = {exact}   (精确)")
        err = abs(approx - exact) / exact * 100 if exact else 0.0
        print(f"    偏差 = {err:.2f}% —— uniq 用固定大小的草图换来了内存与速度,")
        print("    代价就是这点误差。要'一个不差'就用 uniqExact(它会随基数线性涨内存)。")

        print("\n--- WITH TOTALS(本实现:能解析,但不产出总计行)---")
        try:
            text = ctx.execute("SELECT region, sum(amount) FROM sales "
                               "GROUP BY region WITH TOTALS").text()
            print(f">>> SELECT region, sum(amount) FROM sales GROUP BY region WITH TOTALS")
            print(text)
            print("  注:上面只有分区行,**没有**最后那一行'总计'。")
            print("  mini-clickhouse 的 TotalsTransform 目前是个空壳 —— 不假装它生效。")
            print("  想要总计就单独再跑一次不带 GROUP BY 的聚合。")
        except MiniChError as exc:
            print(f"  [{exc.name}] {exc.message}")

        print("\n小结:聚合函数不是'一堆内置函数',而是'少量基函数 + 组合器'。")
        print("      记住 -If / -State / -Merge 三条后缀,就能拼出很多写法。")
    finally:
        ctx.shutdown()


if __name__ == "__main__":
    main()

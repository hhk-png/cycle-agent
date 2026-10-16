# -*- coding: utf-8 -*-
r"""08 —— JOIN 全解:同样的两张表,七种连接方式,看差异在哪里。

JOIN 的难点不在语法,而在"**没匹配上的行怎么办**"和"**匹配上多行怎么办**"。
本示例拿同一对表,把这两条轴上的典型取值排开跑一遍:

.. code-block:: text

   INNER   两侧都有才出         LEFT   左边全留,右边缺的补默认值
   ANY     每个左行只取右边 1 行  SEMI   左边只要"右边存在"就出一行(不扩列)
   ANTI    左边"右边不存在"才出   ASOF   按不等号找'最近的一条'(时序对齐神器)

左表 ``left_t`` 的键是 a/b/c,右表 ``right_t`` 是 a/a/b/d —— 故意留了
"左边有右边没有(c)"和"右边有左边没有(d)",好把外连接的行为露出来。

**重点看两件事:**

1. ``ANY`` / ``SEMI`` / ``ANTI`` 和 ``INNER`` 的行数差别 ——
   它们回答的是不同的问题,不是"性能优化选项";
2. ``LEFT JOIN`` 里 c 那一行的右列是 ``0`` / ``''`` 而**不是 NULL**。
   这是 mini-clickhouse 的一个**有意简化**(``join_use_nulls`` 默认 0):
   真实 ClickHouse 默认也**不是** SQL 标准那样填 NULL,而是填**类型默认值**。
   想要 NULL 语义要显式 ``SET join_use_nulls = 1``。

最后一个是 ASOF 的提示:mini-clickhouse 的 ASOF 目前**简化**成"取等值桶里最后
一行",不等号条件被忽略 —— 所以它只有在右表已排好序时才等价于真实语义。示例里
如实标注。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from minich.execution.context import Context  # noqa: E402

JOIN_KINDS = ["INNER", "LEFT", "ANY", "SEMI", "ANTI"]


def main() -> None:
    print("=" * 78)
    print("08 · JOIN 全解 —— 同一对表,七种连接,行数和补值方式各不相同")
    print("=" * 78)

    ctx = Context()
    try:
        ctx.execute("CREATE TABLE left_t (k String, v Int32) ENGINE = Memory")
        ctx.execute("INSERT INTO left_t VALUES ('a', 1), ('b', 2), ('c', 3)")
        ctx.execute("CREATE TABLE right_t (k String, w Int32) ENGINE = Memory")
        ctx.execute("INSERT INTO right_t VALUES ('a', 10), ('a', 11), ('b', 20), ('d', 40)")

        print("\n左表 left_t : ('a',1) ('b',2) ('c',3)")
        print("右表 right_t: ('a',10) ('a',11) ('b',20) ('d',40)\n")

        for kind in JOIN_KINDS:
            if kind in ("SEMI", "ANTI"):
                # SEMI/ANTI 是"存在性过滤":右表的列本来就取不到,这里只选左表列。
                cols = "left_t.k, v"
            else:
                cols = "left_t.k, v, w"
            sql = (f"SELECT {cols} FROM left_t {kind} JOIN right_t "
                   f"ON left_t.k = right_t.k ORDER BY left_t.k")
            print(f"--- {kind} JOIN ---")
            print(f">>> {sql}")
            print(ctx.execute(sql).text())

        print("--- CROSS JOIN(笛卡尔积:3 × 4 = 12 行,不写 ON)---")
        print(ctx.execute("SELECT count() FROM left_t CROSS JOIN right_t").text())

        print("--- FULL JOIN(两边都全留)---")
        print(ctx.execute(
            "SELECT left_t.k, v, w FROM left_t FULL JOIN right_t "
            "ON left_t.k = right_t.k ORDER BY w, left_t.k").text())

        print("""
读法对照:

  · INNER:只有 a、b 有匹配;c 没有对家,直接消失。
  · LEFT :c 保住了,但右列 w 填的是 **0**(Int32 的默认值),不是 NULL。
  · ANY  :a 在右边有两个对家,ANY 只取一个 —— 左表 3 行就出 3 行,不放大。
  · SEMI :只判断"右表里有没有",不把右表的列带出来 —— 所以没有 w 列。
  · ANTI :反过来,只有"右表里没有"的 c 出来。SEMI/ANTI 是"存在性过滤"。
  · CROSS:3 × 4 = 12 行 —— 这是所有 JOIN 里唯一会**放大行数且无 ON** 的形式。
  · FULL :a、b 有对家,c(只有左)和 d(只有右)也都在,缺的一侧填默认值。

⚠ 注意 LEFT/FULL 里 c、d 那一行的缺失侧是 **0 / ''**,不是 NULL ——
  mini-clickhouse 与真实 ClickHouse 默认都是这个行为(join_use_nulls=0)。
  想要 NULL 得显式打开。
""")

        print("--- ASOF JOIN(时序对齐:按不等号取'最近的一条')---")
        ctx.execute("CREATE TABLE trades (sym String, ts UInt32, px Float64) "
                    "ENGINE = Memory")
        ctx.execute("INSERT INTO trades VALUES "
                    "('AAPL', 10, 100.0), ('AAPL', 20, 101.0), ('AAPL', 30, 102.0)")
        ctx.execute("CREATE TABLE quotes (sym String, ts UInt32, bid Float64) "
                    "ENGINE = Memory")
        ctx.execute("INSERT INTO quotes VALUES "
                    "('AAPL', 5, 99.0), ('AAPL', 15, 100.5), ('AAPL', 25, 101.5)")
        print(ctx.execute(
            "SELECT t.ts AS trade_ts, t.px, q.ts AS quote_ts, q.bid "
            "FROM trades t ASOF JOIN quotes q ON t.sym = q.sym AND t.ts >= q.ts "
            "ORDER BY t.ts").text())
        print("""
语义:每笔成交去配"时间不晚于它、且最近的"那条报价。真 ClickHouse 上应是:
      trade_ts=10 -> quote_ts=5,trade_ts=20 -> quote_ts=15,trade_ts=30 -> 25。

⚠ mini-clickhouse 的 ASOF 目前**简化**为"取等值键桶里的最后一行",
  上面的不等号条件(>=)被忽略 —— 所以输出只有在右表恰好按 ts 排好序时才对。
  这里如实演示,别把它当完整实现。
""")
    finally:
        ctx.shutdown()


if __name__ == "__main__":
    main()

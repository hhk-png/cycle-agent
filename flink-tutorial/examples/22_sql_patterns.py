"""MiniFlink 22：Flink SQL 生产模式的有界表子集。

生产 Flink 的 SQL 模式（去重、窗口聚合、TopN、维表 join）是流式的，
状态和 changelog 由运行时维护。MiniFlink 的 Table 层用 SQLite 对一张
有界表执行关系代数，因此这里演示的是**同一条 SQL 的形状与结果**，不是
流式语义本身。

对应附录 O（Flink SQL 生产模式库）。边界说明见附录 C。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miniflink import TableEnvironment

# 原始订单：o1 重复一次，随后有一条金额更正；event_time 单位毫秒。
ORDERS = [
    {"order_id": "o1", "user_id": "u1", "amount": 100, "event_time": 1000},
    {"order_id": "o1", "user_id": "u1", "amount": 100, "event_time": 1000},
    {"order_id": "o1", "user_id": "u1", "amount": 120, "event_time": 1500},
    {"order_id": "o2", "user_id": "u1", "amount": 50, "event_time": 2000},
    {"order_id": "o3", "user_id": "u2", "amount": 200, "event_time": 3000},
    {"order_id": "o4", "user_id": "u2", "amount": 80, "event_time": 4000},
    {"order_id": "o5", "user_id": "u3", "amount": 150, "event_time": 4500},
]

DIM_USER = [
    {"user_id": "u1", "region": "north"},
    {"user_id": "u2", "region": "south"},
    {"user_id": "u3", "region": "east"},
]


def main() -> None:
    te = TableEnvironment()
    te.from_elements(ORDERS, name="orders_raw")
    te.from_elements(DIM_USER, name="dim_user")

    # 模式一：按 order_id 去重，保留 event_time 最新的一条。
    # 生产上输出是 changelog（-U/+U），下游必须支持 upsert。
    dedup = te.sql(
        """
        SELECT order_id, user_id, amount, event_time
        FROM (
          SELECT *,
                 ROW_NUMBER() OVER (
                   PARTITION BY order_id
                   ORDER BY event_time DESC
                 ) AS rn
          FROM orders_raw
        )
        WHERE rn = 1
        """
    )
    te.register_table("order_dedup", dedup)
    dedup_rows = sorted(dedup.execute(), key=lambda row: row["order_id"])
    assert [row["order_id"] for row in dedup_rows] == ["o1", "o2", "o3", "o4", "o5"]
    assert dedup_rows[0]["amount"] == 120, "o1 应保留金额更正后的 120"
    print("dedup:", dedup_rows)

    # 模式三：滚动窗口聚合。TUMBLE_START 会被 MiniFlink 翻译成 SQLite 表达式；
    # 生产 Flink 用 TABLE(TUMBLE(...)) 的窗口 TVF。
    windowed = te.sql(
        """
        SELECT user_id,
               TUMBLE_START(event_time, 2000) AS window_start,
               SUM(amount) AS total
        FROM order_dedup
        GROUP BY user_id, TUMBLE_START(event_time, 2000)
        """
    )
    window_rows = sorted(
        windowed.execute(), key=lambda row: (row["window_start"], row["user_id"])
    )
    assert window_rows == [
        {"user_id": "u1", "window_start": 0, "total": 120},
        {"user_id": "u1", "window_start": 2000, "total": 50},
        {"user_id": "u2", "window_start": 2000, "total": 200},
        {"user_id": "u2", "window_start": 4000, "total": 80},
        {"user_id": "u3", "window_start": 4000, "total": 150},
    ], window_rows
    print("windowed:", window_rows)

    # 模式二：全局 TopN。生产上没有时间边界时状态会随 key 基数增长，
    # 需要窗口或 table.exec.state.ttl。
    topn = te.sql(
        """
        SELECT order_id, user_id, amount
        FROM (
          SELECT *,
                 ROW_NUMBER() OVER (ORDER BY amount DESC, order_id ASC) AS rn
          FROM order_dedup
        )
        WHERE rn <= 3
        ORDER BY amount DESC, order_id ASC
        """
    )
    top_rows = topn.execute()
    assert [row["order_id"] for row in top_rows] == ["o3", "o5", "o1"], top_rows
    print("topn:", top_rows)

    # 模式四/维表 join：这里是小维表 join；生产上的 lookup join 是
    # FOR SYSTEM_TIME AS OF proc_time 的外部点查。
    enriched = te.sql(
        """
        SELECT o.order_id, o.user_id, o.amount, d.region
        FROM order_dedup AS o
        JOIN dim_user AS d ON o.user_id = d.user_id
        """
    )
    enriched_rows = sorted(enriched.execute(), key=lambda row: row["order_id"])
    assert [row["region"] for row in enriched_rows] == [
        "north", "north", "south", "south", "east"
    ], enriched_rows
    print("enriched:", enriched_rows)

    # 模式九：CTAS/VIEW AS SELECT 的物化子集。MiniFlink 在 CREATE 时一次性
    # 求值并注册；生产 Flink 的 CTAS 是持续写入。
    te.execute_sql(
        """
        CREATE VIEW region_gmv AS
        SELECT region, SUM(amount) AS gmv
        FROM (
          SELECT o.order_id, o.amount, d.region
          FROM order_dedup AS o
          JOIN dim_user AS d ON o.user_id = d.user_id
        )
        GROUP BY region
        """
    )
    gmv = te.sql("SELECT region, gmv FROM region_gmv ORDER BY region")
    gmv_rows = gmv.execute()
    assert gmv_rows == [
        {"region": "east", "gmv": 150.0},
        {"region": "north", "gmv": 170.0},
        {"region": "south", "gmv": 280.0},
    ], gmv_rows
    print("region_gmv:", gmv_rows)

    print("sql patterns: OK (dedup / window / topn / join / ctas-shape)")


if __name__ == "__main__":
    main()

"""附录 O 的可执行子集：去重、窗口聚合、TopN、维表 join 与 CTAS 形状。

这些测试验证的是 MiniFlink 的 SQLite 有界表语义，不是 Flink 的流式
changelog/状态语义。逐字节对拍与生产边界见附录 C/O。
"""

from __future__ import annotations

from miniflink import TableEnvironment

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


def _environment() -> TableEnvironment:
    te = TableEnvironment()
    te.from_elements(ORDERS, name="orders_raw")
    te.from_elements(DIM_USER, name="dim_user")
    te.execute_sql(
        """
        CREATE VIEW order_dedup AS
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
    return te


def test_dedup_keeps_latest_by_event_time() -> None:
    rows = _environment().sql("SELECT * FROM order_dedup").execute()
    assert len(rows) == 5
    latest = {row["order_id"]: row["amount"] for row in rows}
    assert latest["o1"] == 120
    assert latest["o2"] == 50


def test_window_aggregation_uses_tumble_start() -> None:
    rows = _environment().sql(
        """
        SELECT user_id,
               TUMBLE_START(event_time, 2000) AS window_start,
               SUM(amount) AS total
        FROM order_dedup
        GROUP BY user_id, TUMBLE_START(event_time, 2000)
        """
    ).execute()
    observed = {
        (row["user_id"], row["window_start"]): row["total"] for row in rows
    }
    assert observed[("u1", 0)] == 120
    assert observed[("u1", 2000)] == 50
    assert observed[("u2", 2000)] == 200


def test_topn_orders_by_amount() -> None:
    rows = _environment().sql(
        """
        SELECT order_id, amount
        FROM (
          SELECT *,
                 ROW_NUMBER() OVER (ORDER BY amount DESC, order_id ASC) AS rn
          FROM order_dedup
        )
        WHERE rn <= 3
        ORDER BY amount DESC, order_id ASC
        """
    ).execute()
    assert [row["order_id"] for row in rows] == ["o3", "o5", "o1"]


def test_dimension_join_and_materialized_view_shape() -> None:
    te = _environment()
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
    rows = te.sql("SELECT region, gmv FROM region_gmv ORDER BY region").execute()
    assert rows == [
        {"region": "east", "gmv": 150},
        {"region": "north", "gmv": 170},
        {"region": "south", "gmv": 280},
    ]
    assert "region_gmv" in te.list_tables()

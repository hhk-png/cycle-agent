"""MiniFlink 12：SQL 查询、join 与窗口聚合。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miniflink import TableEnvironment

env = TableEnvironment()
env.from_elements(
    [
        {"order_id": 1, "user": "alice", "sku": "p1", "amount": 30, "ts": 1_000},
        {"order_id": 2, "user": "bob", "sku": "p2", "amount": 10, "ts": 2_000},
        {"order_id": 3, "user": "alice", "sku": "p1", "amount": 20, "ts": 6_000},
    ],
    name="orders",
)
env.from_elements(
    [{"sku": "p1", "name": "book"}, {"sku": "p2", "name": "pen"}],
    name="products",
)

query = """
SELECT o.user, p.name, SUM(o.amount) AS revenue
FROM orders AS o
JOIN products AS p ON o.sku = p.sku
GROUP BY o.user, p.name
ORDER BY revenue DESC
"""
print(env.sql(query).execute())

window_query = """
SELECT TUMBLE_START(ts, 5000) AS window_start, SUM(amount) AS revenue
FROM orders
GROUP BY window_start
ORDER BY window_start
"""
print(env.sql(window_query).execute())

# CTAS 形状：MiniFlink 会立即物化查询结果并注册为新表。
# 生产 Flink 的 CTAS 在流模式下提交的是持续写入作业，语义见第 14 章。
env.execute_sql(
    """
    CREATE TABLE user_totals AS
    SELECT user, SUM(amount) AS total, COUNT(*) AS order_count
    FROM orders
    GROUP BY user
    """
)
print(env.sql("SELECT * FROM user_totals ORDER BY total DESC").execute())

import pytest

from miniflink import Duration, StreamExecutionEnvironment, TableEnvironment
from miniflink.table import Schema


def _env():
    env = TableEnvironment()
    env.from_elements(
        [
            {"user": "alice", "sku": "p1", "amount": 20, "ts": 1000},
            {"user": "bob", "sku": "p2", "amount": 15, "ts": 1500},
            {"user": "alice", "sku": "p1", "amount": 30, "ts": 6000},
        ],
        name="orders",
    )
    env.from_elements(
        [{"sku": "p1", "name": "book"}, {"sku": "p2", "name": "pen"}],
        name="products",
    )
    return env


def test_table_select_where():
    table = _env().sql("SELECT user, amount FROM orders WHERE amount >= 20")
    rows = table.execute()
    assert len(rows) == 2
    assert set(rows[0]) == {"user", "amount"}


def test_group_by_aggregate():
    rows = (
        _env()
        .sql("SELECT user, SUM(amount) AS total FROM orders GROUP BY user ORDER BY user")
        .execute()
    )
    assert rows == [{"user": "alice", "total": 50}, {"user": "bob", "total": 15}]


def test_table_api_group_by():
    env = _env()
    table = env._tables["orders"]
    rows = table.group_by("user").aggregate(total="SUM(amount)", cnt="COUNT(*)").order_by("user").execute()
    assert rows[0] == {"user": "alice", "total": 50, "cnt": 2}


def test_join_and_order_by():
    rows = (
        _env()
        .sql(
            "SELECT o.user, p.name, SUM(o.amount) AS revenue "
            "FROM orders o JOIN products p ON o.sku = p.sku "
            "GROUP BY o.user, p.name ORDER BY revenue DESC"
        )
        .execute()
    )
    assert rows[0] == {"user": "alice", "name": "book", "revenue": 50}

    # Table API join 使用注册表名，且同名列不能互相覆盖。
    join_env = TableEnvironment()
    left = join_env.from_elements(
        [{"id": 1, "left_value": "a"}], name="left_table"
    )
    right = join_env.from_elements(
        [{"id": 1, "right_value": "b"}], name="right_table"
    )
    joined = left.join(right, "left_table.id = right_table.id").execute()
    assert joined == [
        {"id": 1, "left_value": "a", "id_1": 1, "right_value": "b"}
    ]


def test_limit():
    rows = _env().sql("SELECT user FROM orders ORDER BY amount DESC LIMIT 2").execute()
    assert len(rows) == 2


def test_window_tumble_table_api():
    env = _env()
    table = env._tables["orders"].window_tumble("ts", Duration.of_seconds(5))
    rows = (
        table.group_by("window_start", "window_end")
        .aggregate(revenue="SUM(amount)")
        .order_by("window_start")
        .execute()
    )
    assert rows == [
        {"window_start": 0, "window_end": 5000, "revenue": 35},
        {"window_start": 5000, "window_end": 10000, "revenue": 30},
    ]


def test_sql_tumble_start():
    rows = (
        _env()
        .sql(
            "SELECT TUMBLE_START(ts, 5000) AS w, SUM(amount) AS total "
            "FROM orders GROUP BY w ORDER BY w"
        )
        .execute()
    )
    assert rows == [{"w": 0, "total": 35}, {"w": 5000, "total": 30}]


def test_register_and_list_tables():
    env = _env()
    assert env.list_tables() == ["orders", "products"]
    with pytest.raises(ValueError):
        env.register_table("bad name", env._tables["orders"])


def test_explain_sql_returns_plan():
    plan = _env().explain_sql("SELECT * FROM orders")
    assert "SCAN" in plan.upper() or "orders" in plan.lower()


def test_invalid_sql_raises_value_error():
    with pytest.raises(ValueError):
        _env().sql("SELECT missing FROM orders")


def test_table_to_data_stream():
    env = _env()
    stream = env._tables["orders"].to_data_stream(StreamExecutionEnvironment())
    rows = stream.execute_and_collect()
    assert len(rows) == 3


def test_empty_table_schema_from_explicit_schema():
    env = TableEnvironment()
    schema = Schema.from_names(["name"])
    table = env.from_elements([], schema=schema, name="empty")
    assert env.sql("SELECT * FROM empty").execute() == []


def test_join_duplicate_columns_are_not_dropped():
    env = TableEnvironment()
    env.from_elements([{"id": 1}], name="left_t")
    env.from_elements([{"id": 1, "id_1": 99}], name="right_t")
    rows = env.sql(
        "SELECT * FROM left_t JOIN right_t ON left_t.id = right_t.id"
    ).execute()
    assert len(rows) == 1
    assert len(rows[0]) == 3
    assert 99 in rows[0].values()


def test_tumble_start_floors_negative_timestamps():
    env = TableEnvironment()
    env.from_elements([{"ts": -5}], name="events")
    rows = env.sql(
        "SELECT TUMBLE_START(ts, 10) AS w, TUMBLE_END(ts, 10) AS e FROM events"
    ).execute()
    assert rows == [{"w": -10, "e": 0}]


def test_schema_inference_uses_whole_column():
    mixed = Schema.from_rows([{"code": 8}, {"code": "007"}])
    assert mixed.fields[0].type_name == "STRING"

    numeric = Schema.from_rows([{"amount": 9}, {"amount": 10.5}])
    assert numeric.fields[0].type_name == "DOUBLE"


def test_create_table_as_select_registers_bounded_result():
    env = _env()
    created = env.execute_sql(
        "CREATE TABLE user_totals AS "
        "SELECT user, SUM(amount) AS total FROM orders GROUP BY user"
    )
    assert created.execute() == [
        {"user": "alice", "total": 50},
        {"user": "bob", "total": 15},
    ]
    assert "user_totals" in env.list_tables()
    assert env.sql(
        "SELECT total FROM user_totals WHERE user = 'alice'"
    ).execute() == [{"total": 50}]

    # IF NOT EXISTS 保留已有表；没有修饰符时重复建表应失败而不是静默覆盖。
    env.execute_sql(
        "CREATE TABLE IF NOT EXISTS user_totals AS SELECT user FROM orders"
    )
    assert env.sql("SELECT total FROM user_totals WHERE user = 'alice'").execute() == [
        {"total": 50}
    ]
    with pytest.raises(ValueError, match="表已存在"):
        env.execute_sql("CREATE TABLE user_totals AS SELECT user FROM orders")


def test_create_view_as_select_and_replace():
    env = _env()
    env.execute_sql(
        "CREATE VIEW big_orders AS "
        "SELECT user, amount FROM orders WHERE amount >= 20"
    )
    assert env.sql("SELECT COUNT(*) AS cnt FROM big_orders").execute() == [
        {"cnt": 2}
    ]

    env.execute_sql(
        "CREATE OR REPLACE VIEW big_orders AS "
        "SELECT user, amount FROM orders WHERE amount >= 25"
    )
    assert env.sql("SELECT COUNT(*) AS cnt FROM big_orders").execute() == [
        {"cnt": 1}
    ]

    # 无 AS 的 DDL 会给出明确错误，而不是把 SQLite 的语法错误直接抛给用户。
    with pytest.raises(ValueError, match="仅支持"):
        env.execute_sql("CREATE TABLE raw_orders (id INT) WITH ('connector'='kafka')")

# 第32章 MiniFlink：Table/SQL、CEP 与可观测性

## 32.1 Table 层为什么用 SQLite

生产 Flink SQL 的链路是：

```text
SQL → Calcite 解析 → 逻辑计划 → 优化器 → 物理算子 → 运行时
```

MiniFlink 不实现 Calcite，而是：

```text
SQL → 少量函数翻译 → SQLite 执行 → 行集合
```

这是一个**有界表**模型：

- 所有数据先物化为 `list[dict]`；
- 查询一次性执行；
- 结果是一次性返回的 `Table`；
- 没有 changelog、watermark、增量更新。

它的价值是：

- 验证关系代数语义；
- 演示 Table API 的链式操作；
- 给窗口列/聚合一个可运行的对照；
- 不引入第三方依赖。

它不是 Flink SQL 兼容层，不能用来验证流式 SQL 的语义。

## 32.2 Schema 与 Row

```python
@dataclass(frozen=True)
class Field:
    name: str
    type_name: str
    nullable: bool = True

class Schema:
    def __init__(self, fields): ...
    @classmethod
    def from_rows(cls, rows): ...   # 从 dict 推断类型
    def to_sqlite_ddl(self, table_name): ...

@dataclass
class Row:
    values: tuple
    def as_dict(self, schema): ...
```

`Schema.from_rows` 按**整列**而不是第一个值推断 SQLite 类型：

```python
def _infer_column_type(values):
    if not values: return "STRING"
    if all(isinstance(v, bool) for v in values): return "BOOLEAN"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return "BIGINT"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
        return "DOUBLE"
    if all(isinstance(v, bytes) for v in values): return "BYTES"
    return "STRING"   # 混合列退化为 TEXT，避免数值/文本比较反转
```

映射到 SQLite 时，`BIGINT` 用 `INTEGER`、`DOUBLE` 用 `REAL`、`BYTES` 用 `BLOB`、其余用 `TEXT`。这套类型系统很粗：

- 没有 Decimal/时间类型精度；
- 没有嵌套结构；
- 没有 null 语义的严格区分；
- 没有 schema 演进。

## 32.3 Table API

```python
env = TableEnvironment()
orders = env.from_elements([
    {"user": "alice", "amount": 20},
    {"user": "bob", "amount": 15},
], name="orders")

result = (
    orders
    .where("amount > 0")
    .group_by("user")
    .aggregate(total="SUM(amount)", cnt="COUNT(*)")
    .order_by("total DESC")
)
```

`Table` 的方法：

- `select(*expressions)`；
- `where(condition)` / `filter`；
- `group_by(*columns)` → `GroupedTable.aggregate(**aggs)`；
- `order_by(*expressions)`；
- `limit(n)`；
- `join(other, condition, how=...)`；
- `window_tumble(time_column, size)`；
- `to_data_stream(env)`；
- `execute()` / `collect()`。

每次操作都会：

1. 新建内存 SQLite 连接；
2. 把当前表加载为临时表 `__table`；
3. 执行 SQL；
4. 把结果读成新的 `Table`。

```python
def _query(self, sql):
    connection = self._connection("__table")
    try:
        cursor = connection.execute(sql)
        rows, schema = _fetch_rows(cursor)
        return Table(rows, schema)
    finally:
        connection.close()
```

这是“物化执行”，不是流式计划。

## 32.4 SQL 子集

`TableEnvironment.sql()` 会：

1. 把已注册的表加载进内存 SQLite；
2. 调用 `_translate_sql` 做少量函数映射；
3. 执行并返回 `Table`。

支持的 SQL：

- `SELECT` / `WHERE` / `GROUP BY` / `HAVING`；
- `ORDER BY` / `LIMIT`；
- `JOIN`（INNER/LEFT/RIGHT/FULL）；
- 常用聚合（SUM/COUNT/AVG/MIN/MAX）；
- `TUMBLE_START(ts, size)` 的简化翻译；
- `CREATE [OR REPLACE] TABLE/VIEW [IF NOT EXISTS] name AS SELECT ...` 的 CTAS/VIEW 教学子集。

```sql
SELECT TUMBLE_START(ts, 5000) AS window_start, SUM(amount) AS revenue
FROM orders
GROUP BY window_start
ORDER BY window_start;
```

`execute_sql()` 会识别 `CREATE ... AS SELECT`：先执行 SELECT，把结果 `Table` 注册到 `_tables`，再返回它。由于 MiniFlink 没有 Catalog 和外部存储，这是一次性物化，不是 Flink 的持续写入；`VIEW` 也会立即物化，而不是惰性视图。重复建表在缺少 `IF NOT EXISTS` / `OR REPLACE` 时报错，这一点与 Flink 的 DDL 语义保持一致。生产 CTAS 的 Catalog、原子性和失败语义见[第 14 章 14.9 节](14-table-sql.md#149-ctasrtas-与数据管道)。

`TUMBLE_START` 被翻译成：

```sql
(CAST(ts / 5000 AS INTEGER) * 5000)
```

这不是完整的 TVF 语义：

- 没有 watermark；
- 没有迟到更新；
- 没有 changelog；
- 没有状态；
- 没有优化器。

## 32.5 窗口列

`Table.window_tumble` 给每行补 `window_start/window_end`：

```python
def window_tumble(self, time_column, size, offset=0):
    size_ms = size.millis if isinstance(size, Duration) else int(size)
    for row in self.rows:
        ts = row[time_column]
        start = ts - ((ts - offset) % size_ms)
        enriched = dict(row)
        enriched["window_start"] = start
        enriched["window_end"] = start + size_ms
```

之后可以：

```python
(
    orders
    .window_tumble("ts", Duration.of_seconds(5))
    .group_by("window_start")
    .aggregate(revenue="SUM(amount)")
)
```

这与 DataStream 窗口的语义差别：

- 没有 keyBy（除非 group_by 包含 key）；
- 没有 watermark；
- 没有迟到；
- 窗口是物化的。

它适合教学演示“窗口列 + 分组聚合”的形状。

## 32.6 CEP

MiniFlink 的 Pattern API：

```python
from miniflink import Pattern, pattern

login_pattern = (
    Pattern.begin("first").where(lambda e: e["type"] == "fail")
    .next("second").where(lambda e: e["type"] == "fail")
    .within(Duration.of_seconds(5))
)

pattern(stream.key_by(lambda e: e["user"]), login_pattern) \
    .select(lambda match: (match.key, match["first"], match["second"]))
```

`Pattern` 保存步骤：

```python
@dataclass
class _Step:
    name: str
    predicate: Callable[[Any], bool]
    kind: str          # "next" / "followed_by"
    quantifier: str    # "one" / "one_or_more" / "optional"
```

`CepOperator` 的状态：

```python
self.partials: dict[Any, list[_PartialMatch]] = {}
self.positions: dict[Any, int] = {}
```

每条记录：

1. 按 key 增加 position；
2. 清理不满足 `next`/`within` 的部分匹配；
3. 尝试推进每个部分匹配；
4. 从当前记录开启新匹配；
5. 按 watermark 清理超时部分匹配。

```python
for partial in current:
    step = self.pattern.steps[partial.step_index]
    if step.kind == "next" and partial.last_position < position - 1:
        continue
    if within and ts - partial.start_timestamp > within_ms:
        continue
    if step.predicate(record.value):
        ...
```

### 严格 vs 非严格

- `next`：同一个 key 的下一条记录必须匹配，中间不能有别的记录；
- `followed_by`：允许中间出现其他记录；
- `one_or_more`：重复匹配；
- `within`：整个模式必须在时间窗口内完成。

MiniFlink 的实现是“逐条扫描部分匹配”，没有 NFA 的状态压缩/共享。模式复杂、部分匹配多时会很慢。

## 32.7 广播状态

```python
rules = env.from_collection([...]).broadcast()
events.key_by(lambda e: e["user"]) \
      .connect(rules) \
      .process(MatchRules())
```

`BroadcastCoProcessOperator`：

- `input_index == 0`：主数据，按 key 处理；
- `input_index == 1`：广播数据，更新 `broadcast_state`；
- `snapshot()` 保存 `broadcast_state`；
- `restore()` 还原。

```python
from miniflink import BroadcastProcessFunction, MapStateDescriptor

RULES = MapStateDescriptor("rules", dict)

class MatchRules(BroadcastProcessFunction):
    def process_element(self, event, ctx, out):
        # BroadcastMapState 没有 values()；遍历用 items()
        for _, rule in ctx.get_broadcast_state(RULES).items():
            if rule.matches(event):
                out.collect(...)

    def process_broadcast_element(self, rule, ctx, out):
        ctx.get_broadcast_state(RULES).put(rule.id, rule)
```

MiniFlink 的广播状态是普通 dict，没有 key/namespace，也没有 per-subtask 的只读视图。它足以演示“规则热更新”的形状。

## 32.8 指标与 CLI

`metrics.py`：

- `Counter`：只增；
- `Gauge`：即时值；
- `Meter`：速率；
- `MetricGroup`：层级命名；
- `LoggingReporter`：把指标快照追加到内存 `history`（不打印，也尚未接入运行时）。

```python
group = MetricGroup("job")
group.counter("recordsProcessed").inc()
group.gauge("queueSize", lambda: len(queue))
```

`JobExecution.metrics()` 汇总：

```python
{
  "job": {...},
  "checkpoints": {...},
  "tasks": {...},
  "side_outputs": {...},
}
```

CLI：

```bash
python -m miniflink.cli run examples/01_word_count.py
python -m miniflink.cli sql --table orders=orders.csv \
  --query "SELECT user, SUM(amount) FROM orders GROUP BY user"
```

CLI 的 `run` 会加载 Python 文件并执行 `main()`；`sql` 会把 CSV 读成表，执行查询并打印。

## 32.9 测试覆盖

`tests/` 覆盖：

- `test_table.py`：Schema、Table API、SQL、窗口列；
- `test_sql_patterns.py`：附录 O 子集（去重、窗口聚合、TopN、维表 join、物化形状）；
- `test_cep.py`：序列、严格/非严格、within；
- `test_join_broadcast.py`：窗口/区间 join、广播；
- `test_metrics.py`：Counter/Gauge/Meter；
- `test_cli.py`：CLI parser 与 `sql` 命令（`run` 只做到解析/入口，没有端到端用例）。

测试思路：

```python
def test_tumble_sum():
    env = TableEnvironment()
    env.from_elements([...], name="orders")
    rows = env.sql("""
        SELECT TUMBLE_START(ts, 5000) AS w, SUM(amount) AS total
        FROM orders GROUP BY w ORDER BY w
    """).execute()
    assert rows[0]["total"] == 30
```

## 32.10 限制

- 没有 Calcite/优化器；
- 没有动态表/changelog；
- 没有 watermark/窗口 TVF；
- 没有 Catalog；
- 没有 UDF 注册；
- CEP 没有否定/optional/模式组；
- 广播状态没有 key/namespace；
- 有 reporter 抽象与内存 `history`，但未接入运行时，也没有外部导出。

这些限制让 MiniFlink 只能做“语义形状”的演示，不能替代生产 Flink SQL/CEP。

## 本章小结

MiniFlink 的 Table 层是“SQLite 上的关系代数”，CEP 是“逐条扫描部分匹配”，指标是“进程内计数”。它们共同展示了 Flink 高层 API 的底层形状：关系、状态机、度量。

下一章把 MiniFlink 与生产 Flink 对齐：哪些语义已经一致，哪些差距无法回避，以及如何从教学实现走向生产系统。

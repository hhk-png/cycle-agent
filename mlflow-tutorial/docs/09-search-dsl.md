# 第 9 章 搜索 DSL：解析、校验与安全 SQL

## 学习目标

- 读懂搜索表达式的词法、语法与 AST。
- 理解为什么不能把用户输入拼进 SQL，也不能直接用 `eval`。
- 掌握字段作用域、最新指标语义和动态 ORDER BY 的参数化。
- 能够扩展一种新操作符，并评估对索引和分页的影响。

## 9.1 支持的语法

```text
expr        := or_expr
or_expr     := and_expr (OR and_expr)*
and_expr    := unary (AND unary)*
unary       := NOT unary | '(' expr ')' | predicate
predicate   := field (comparison literal
                      | [NOT] IN '(' literal (',' literal)* ')'
                      | [NOT] LIKE string
                      | [NOT] ILIKE string
                      | IS [NOT] NULL)
field       := IDENT | '`' IDENT '`'
comparison  := '=' | '!=' | '<>' | '>' | '>=' | '<' | '<='
```

示例：

```text
metrics.val_auc >= 0.85
params.model = "linear" AND tags.team = "nlp"
attributes.status IN ("FINISHED", "FAILED")
tags.stage NOT IN ("dev", "test")
attributes.run_name LIKE "fraud-%"
tags.`model name` = "x y"
metrics.val_auc IS NOT NULL
```

关键字大小写不敏感；字符串用单/双引号；支持 `''`/`""` 转义和反斜杠转义。

### 字段作用域

| 作用域 | 可用字段 | 示例 |
|---|---|---|
| `attributes` | run_id、experiment_id、run_name、user_id、status、start_time、end_time、artifact_uri、lifecycle_stage | `attributes.status = "FINISHED"` |
| `params` | 任意参数 key | `params.lr > 0.001` |
| `tags` | 任意标签 key | `tags.owner = "alice"` |
| `metrics` | 任意指标 key，使用最新值 | `metrics.auc >= 0.9` |
| `datasets` | name、digest、context（字符串） | `datasets.name = "transactions" AND datasets.context = "training"` |

`name` 这种裸字段会被当作 `attributes.name`；在 run 搜索中 `name` 不是合法属性（应写 `attributes.run_name`），在注册模型搜索中 `name` 合法。

`datasets.*` 只接受字符串比较（`=`、`!=`、`IN`、`LIKE`、`ILIKE`），每个谓词编译为一个独立的 `EXISTS` 子查询。这意味着 `datasets.name = "a" AND datasets.context = "evaluation"` 可能由同一个 run 的两条不同 dataset input 分别满足；AND 表达的是“两个条件都为真”，不是“同一份数据同时满足两个条件”。需要后者时，应在应用层先确定 digest，再用单个条件过滤。

## 9.2 为什么不用 `eval` 或字符串拼接

假设实现成：

```python
sql = f"SELECT * FROM runs WHERE {filter_string}"
```

攻击者可以输入：

```text
1=1; DROP TABLE runs; --
```

即使没有分号，也可以构造 `OR 1=1` 绕过过滤，或通过子查询读取其他表。`eval` 更糟，直接执行任意 Python。

正确做法：

1. 只接受白名单语法；
2. 字段名映射到固定列表达式；
3. 所有值作为绑定参数；
4. 限制最大表达式长度/深度；
5. 拒绝未知字段、未知操作符。

Mini 的 `parse_filter` 只返回 AST，不执行；`compile_run_filter` 只拼接**代码中定义的** SQL 片段，用户值全部走 `?`。

## 9.3 词法与语法

`Token` 类型包括：`IDENT`、`LITERAL`、`NUMBER`、`OP`、`LPAREN`、`RPAREN`、`COMMA`、关键字。

几个实现细节：

- 数字支持负数、小数、科学计数法；布尔 `true/false` 转 Python bool。
- 字符串支持 SQL 风格双引号转义和反斜杠转义。
- 反引号用于包含空格/特殊字符的 key。
- `NOT` 只能出现在 `NOT IN`/`NOT LIKE` 或一元 `NOT` 位置；解析器会显式报错，而不是猜。
- 语法错误包含位置，便于 UI/CLI 提示。

递归下降：

```python
parse_or -> parse_and -> parse_unary -> parse_predicate
```

`AND` 优先级高于 `OR`，与 SQL 一致。测试覆盖了 `a OR b AND c` 的树形结构。

## 9.4 AST → SQL：run 搜索

以：

```text
metrics.val_auc > 0.8 AND tags.team = "nlp"
```

编译为：

```sql
r.lifecycle_stage = 'active'
AND (
  EXISTS (
    SELECT 1 FROM metrics m
    WHERE m.run_id = r.run_id AND m.key = ?
      AND m.value > ?
      AND m.id = (
        SELECT m2.id FROM metrics m2
        WHERE m2.run_id = m.run_id AND m2.key = m.key
        ORDER BY m2.timestamp DESC, m2.id DESC LIMIT 1
      )
  )
  AND EXISTS (
    SELECT 1 FROM run_tags t
    WHERE t.run_id = r.run_id AND t.key = ? AND t.value = ?
  )
)
```

参数顺序：`["val_auc", 0.8, "team", "nlp"]`。

为什么用相关子查询而不是 JOIN？

- 每个条件独立，语义清晰；
- 不会因为多个指标条件产生笛卡尔积；
- 对每个 run 只取最新一行。

代价是条件多时子查询多。生产可以改为物化最新指标表、倒排索引或专用查询引擎。

### 类型处理

- `attributes.experiment_id` 是整数列，值可以是数字或数字字符串；SQLite 会做亲和转换。
- `params.*` 默认字符串；`>`/`<` 且值为数字时，Mini 对列做 `CAST(... AS REAL)`。这会让参数索引失效，但语义更符合用户直觉。
- `metrics.*` 的值必须是数值；字符串会报 `SearchSyntaxError`。
- `LIKE`/`ILIKE` 只允许字符串；`ILIKE` 用 `LOWER(...) LIKE LOWER(?)`，对非 ASCII 大小写不保证。

### `IS NULL`

对 `params`/`tags`/`metrics`，表示“不存在该 key”，实现为 `NOT EXISTS (...)`。对 `attributes`，直接对列做 `IS NULL`。这避免了“空字符串”和“缺失”混淆。

## 9.5 动态 ORDER BY

用户输入：

```text
["metrics.val_auc DESC", "attributes.start_time ASC"]
```

不能把字段名直接拼进 SQL。Mini 的做法：

```sql
ORDER BY
  (SELECT m.value FROM metrics m
   WHERE m.run_id = r.run_id AND m.key = ?
   ORDER BY m.timestamp DESC, m.id DESC LIMIT 1) DESC,
  r.start_time ASC,
  r.run_id ASC
```

`m.key` 的占位符按出现顺序收集，`compile_run_order` 返回 `(sql, order_params)`；服务层把 `order_params` 放在 WHERE 参数之后、LIMIT 之前。

方向只允许 `ASC`/`DESC`，来自正则解析后的白名单（不是原样拼接）。字段只允许 `attributes/params/tags/metrics`，并在编译时校验。

### 稳定性

始终追加 `r.run_id ASC` 作为 tie-breaker，避免相同时间/指标值的 run 在分页时跳动。实验搜索追加 `e.experiment_id ASC`。

## 9.6 分页

服务层取 `max_results + 1` 条：

```python
rows = store.search_runs(..., max_results + 1, offset)
has_more = len(rows) > max_results
```

token 是：

```json
{"offset": 40}
```

的优点是无状态；缺点是深分页和并发写下的漂移。第 7 章给出 keyset 方案。

## 9.7 实验与注册表的搜索

### 实验

支持：

```text
attributes.name、experiment_id、lifecycle_stage、creation_time、last_update_time
tags.*
```

编译为 SQL，走 `experiments` 表 + `experiment_tags` 的 EXISTS。默认过滤 `lifecycle_stage='active'`；`include_deleted=True` 用于审计。

### 注册表

注册表数据量通常远小于 run，Mini 用 AST + Python `evaluate_ast`：

```python
resolver = lambda field: model.tags.get(field.key) if field.scope == "tags" else ...
evaluate_ast(ast, resolver)
```

好处是不用为每种实体写一套 SQL；坏处是大规模下全表加载。真实系统应为注册表建索引/查询服务。

注册模型支持：`name/creation_timestamp/last_updated_timestamp/description` + tags。
模型版本支持：`name/version/run_id/source/status/current_stage/tags/...`。
`LIKE` 用 SQL 风格 `%`/`_` 转正则，大小写按 `ILIKE` 处理。

## 9.8 错误与限制

会报错的情况：

- 未知作用域（`foo.bar`）；
- 未知属性（`attributes.password`）；
- 非法字面量（`metrics.auc = "high"`）；
- 未闭合字符串/括号；
- 空 IN 列表；
- 尾随 token；
- `max_results` 越界；
- 非法 order_by。

Mini 不支持的：

- 函数（`LOWER()`、`ABS()`）；
- 算术（`metrics.auc * 2`）；
- 子查询；
- JOIN；
- `BETWEEN`；
- 日期函数；
- 正则。

这些不是“实现不完”，而是安全边界。扩展时应先扩展 AST 与白名单，而不是放开字符串拼接。

## 9.9 性能与安全清单

- 表达式长度限制（建议 4–8 KB）；
- AST 深度限制（防递归炸弹）；
- `max_results` 上限；
- 只允许白名单字段/操作符；
- 所有值绑定参数；
- 动态 ORDER BY 的 key 也必须参数化；
- 对 `LIKE` 输入不做 SQL 转义时要明确语义（用户可使用 `%`）；
- 日志记录慢查询与拒绝原因，但不要记录敏感值。

## 9.10 与真实 MLflow 的差距

- 真实 MLflow 的过滤语法支持更丰富的字段和部分函数，但同样不是 SQL。
- 真实后端可能把过滤下推到 SQL/搜索服务；Mini 用相关子查询。
- 真实分页 token 是不透明字符串，可能带排序键；Mini 是 offset。
- 真实 MLflow 对 `params` 的类型处理依后端；Mini 明确数值比较时 CAST。

## 小结

- 搜索 DSL 的安全性来自“白名单 + AST + 参数绑定”，不是输入转义。
- 最新指标语义要用相关子查询明确表达。
- 动态 ORDER BY 同样需要参数化。
- 分页与排序稳定性要在设计时考虑。

## 练习

1. 为 DSL 增加 `BETWEEN`，写出 AST、SQL 编译和测试用例。
2. 为什么 `params.lr > 0.001` 对参数列 CAST 可能导致索引失效？如何优化？
3. 设计一个 keyset 分页 token，说明并发插入时的行为。

---

下一章：[第 10 章 Artifact Store](10-artifacts.md)

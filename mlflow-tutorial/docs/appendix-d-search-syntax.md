# 附录 D 搜索语法参考

## D.1 文法

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
literal     := NUMBER | STRING | TRUE | FALSE | IDENT
```

## D.2 字段

### Run

```text
attributes.run_id
attributes.experiment_id
attributes.run_name
attributes.user_id
attributes.status
attributes.start_time
attributes.end_time
attributes.artifact_uri
attributes.lifecycle_stage
params.<key>
tags.<key>
metrics.<key>
datasets.name
datasets.digest
datasets.context
```

裸字段（如 `status`）等价于 `attributes.status`。

`datasets.*` 的可用操作符是 `=`、`!=`、`IN`、`NOT IN`、`LIKE`、`ILIKE`；不支持 `>`/`<`（字段是字符串）和 `IS NULL`（列非空）。每个谓词编译为独立 `EXISTS`，因此 AND 两侧可能命中同一个 run 的不同 dataset input。

### 实验

```text
attributes.name
attributes.experiment_id
attributes.creation_time
attributes.last_update_time
attributes.lifecycle_stage
tags.<key>
```

### 注册模型

```text
name / attributes.name
attributes.creation_timestamp
attributes.last_updated_timestamp
attributes.description
tags.<key>
```

### 模型版本

```text
attributes.name
attributes.version
attributes.run_id
attributes.source
attributes.status
attributes.current_stage
attributes.description
attributes.creation_timestamp
attributes.last_updated_timestamp
tags.<key>
```

### Trace

```text
attributes.trace_id
attributes.name
attributes.status
attributes.experiment_id
attributes.timestamp
attributes.latency_ms
tags.<key>
assessments.<name>
```

`assessments.<name>` 取该 trace 上同名、有效（`valid=true`）的最近更新 assessment 的值；缺失视为不匹配。它是 Trace 搜索专属，不能用于 Run 搜索。

### 评测数据集

```text
attributes.dataset_id
attributes.name
attributes.version
attributes.digest
attributes.source
attributes.source_type
attributes.record_count
attributes.created_time
attributes.last_updated_time
tags.<key>
```

数据集搜索只过滤数据集元数据，不搜索 `dataset_records` 内部的 inputs/expectations；需要按记录过滤时，应增加记录级 API，而不是把记录字段硬塞进元数据。

## D.3 示例

```text
attributes.status = "FINISHED"
metrics.val_auc >= 0.85 AND tags.team = "nlp"
params.model IN ("linear", "tree")
tags.stage NOT IN ("dev", "test")
attributes.run_name LIKE "fraud-%"
tags.`model name` = "x y"
metrics.loss IS NOT NULL
NOT (tags.env = "prod" OR tags.env = "staging")
assessments.quality >= 0.8 AND tags.prompt = "support-reply"
attributes.name LIKE "support-%" AND tags.stage = "golden"
```

## D.4 语义细节

- `AND` 优先于 `OR`；用括号显式表达。
- 字符串单/双引号均可；支持 `''`/`""` 和反斜杠转义。
- 数字支持负数/小数/科学计数法。
- `metrics.*` 比较必须是数值。
- 最新指标按 `timestamp DESC, id DESC`。
- `IS NULL` 对 params/tags/metrics 表示“不存在该 key”。
- `LIKE` 使用 SQL 通配符 `%`/`_`；`ILIKE` 不区分大小写（非 ASCII 不保证）。
- 未加引号的右侧标识符按字符串处理（兼容常见写法）。

## D.5 ORDER BY

```json
["metrics.val_auc DESC", "attributes.start_time ASC"]
```

允许：

- `attributes.*`（run 的字段白名单）
- `metrics.*`、`params.*`、`tags.*`（相关子查询）
- 实验：仅 `attributes.*`
- 注册表：名称/时间等白名单
- 数据集：`attributes.name/version/created_time/last_updated_time/record_count` 排序白名单

方向只允许 `ASC`/`DESC`。

## D.6 分页

```json
{"filter":"...", "max_results":20, "page_token":"eyJvZmZzZXQiOjIwfQ"}
```

Token 是 base64 编码的 `{"offset": n}`。深分页请改用时间/ID 范围。

## D.7 限制

- 不支持函数、算术、子查询、JOIN、BETWEEN、正则。
- 不支持跨实验的标签 join。
- 结果上限由 `search_max_results` 控制。
- 复杂过滤可能慢；先缩小时间/实验范围。

## D.8 错误示例

| 输入 | 错误 |
|---|---|
| `attributes.password = "x"` | 未知属性 |
| `metrics.acc = "high"` | 指标需要数值 |
| `tags.x IN ()` | 空 IN |
| `a = 1 OR` | 语法不完整 |
| `metrics.acc > 0.5; DROP` | 非法 token |

---

返回：[教程首页](../README.md) · [附录 E](appendix-e-cli.md)

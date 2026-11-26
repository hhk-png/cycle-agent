# 第 43 章 数据平台与分析 API

## 43.1 数据平台 API 的特点

数据平台 API 服务的是：

- 内部数据团队；
- BI 工具；
- 机器学习管道；
- 外部客户的数据导出；
- 合规审计；
- 实时应用。

特点：

- 数据量大；
- 查询耗时长；
- schema 复杂；
- 结果可能巨大；
- 成本高；
- 权限细；
- 需要血缘与治理；
- 查询可能失败/超时；
- 结果可能过期。

### 与传统 CRUD API 的差异

| 维度 | CRUD API | 数据平台 API |
|---|---|---|
| 结果大小 | 小 | 大 |
| 延迟 | 毫秒 | 秒~小时 |
| 查询 | 固定 | 任意 |
| 成本 | 低 | 高 |
| 权限 | 对象 | 库/表/列/行 |
| 演进 | schema | schema + 数据 |

## 43.2 数据接入

### 批量导入

```text
POST /imports
```

```json
{
  "source": { "type": "s3", "uri": "s3://bucket/data/" },
  "format": "parquet",
  "target": { "database": "analytics", "table": "events" },
  "mode": "append",
  "schema": { "columns": [] }
}
```

返回异步任务。

### 流式接入

```text
POST /streams
```

```json
{
  "source": { "type": "kafka", "topic": "events" },
  "format": "json",
  "target": "events_raw",
  "schemaRegistry": "..."
}
```

### 幂等与 exactly-once

- 批次 ID；
- 去重键；
- 事务写入；
- checkpoint；
- 重放；
- 幂等写入。

### 导入模式

- append；
- overwrite；
- upsert；
- merge；
- partition replace。

## 43.3 查询 API

### 同步查询

适合小结果、低延迟：

```http
POST /query
```

```json
{
  "sql": "SELECT status, count(*) FROM orders GROUP BY status",
  "timeoutSeconds": 30,
  "maxRows": 1000
}
```

响应：

```json
{
  "columns": [
    { "name": "status", "type": "string" },
    { "name": "count", "type": "int64" }
  ],
  "rows": [["PAID", 120], ["SHIPPED", 80]],
  "rowCount": 2,
  "truncated": false
}
```

### 异步查询

大数据量应异步：

```text
POST /queries
GET  /queries/{id}
GET  /queries/{id}/results
POST /queries/{id}/cancel
```

状态：

```text
QUEUED → RUNNING → SUCCEEDED
                 ├→ FAILED
                 └→ CANCELLED
```

### 分页

- 结果游标；
- 最大行数；
- 导出；
- 流式结果；
- 结果 TTL。

### 查询结果的结构

- 列元数据；
- 行数据；
- 类型；
- 空值；
- 截断标志；
- 统计信息；
- 查询 ID。

### 查询生命周期

```text
提交 → 解析/校验 → 权限检查 → 成本估算 → 排队
→ 执行 → 取结果 → 缓存/存储 → 过期清理
```

每个阶段都应可观测：

- 解析失败；
- 权限拒绝；
- 成本超限；
- 排队时间；
- 执行时间；
- 结果大小；
- 取消；
- 失败原因。

### 查询的幂等

- 查询本身通常只读；
- 重复提交可能浪费资源；
- 可用 `requestId` 去重；
- 异步查询可返回已有查询；
- 写查询需要幂等键。

## 43.4 SQL over HTTP 的注意事项

- 只读 vs 读写；
- 多语句；
- 参数化；
- 注入；
- 资源限制；
- 超时；
- 结果大小；
- 计费；
- 审计；
- 租户隔离。

不要直接把用户 SQL 拼接到数据库；使用参数化、权限校验、查询白名单或受限方言。

### SQL 注入

```python
# 错误：表名与值都拼接
query = f"SELECT * FROM {table} WHERE id = '{user_id}'"

# 正确：标识符白名单 + 值参数化
if table not in ALLOWED_TABLES:
    raise InvalidTable(table)

query = f"SELECT * FROM {table} WHERE id = :id"
cursor.execute(query, {"id": user_id})
```

关键点：

- **值**用参数绑定，不要拼字符串；
- **表名/列名**不能被参数绑定，必须用白名单映射（`"orders" -> "orders_v2"`），不能直接接受用户输入；
- 排序字段、聚合函数、时间粒度同理，都要白名单；
- 即使用 ORM，也要检查 `order_by`/`select` 是否接受原始字符串。

### 资源限制

- 最大扫描字节；
- 最大运行时间；
- 最大结果行数；
- 最大并发；
- 最大 JOIN 数；
- 禁止某些操作；
- 队列。

### 多租户

- 每个租户独立 schema/数据库；
- 行级过滤；
- 查询重写；
- 资源隔离；
- 计费隔离。

## 43.5 Schema 与目录

```text
GET /catalogs
GET /catalogs/{catalog}/databases
GET /catalogs/{catalog}/databases/{db}/tables
GET /catalogs/{catalog}/databases/{db}/tables/{table}
```

表信息：

- schema；
- 分区；
- 统计；
- 所有者；
- 标签；
- 血缘；
- 保留期；
- 敏感级别。

### 目录的价值

- 发现数据；
- 理解 schema；
- 权限申请；
- 血缘；
- 质量；
- 成本。

## 43.6 数据血缘

```text
raw.events → cleaned.events → analytics.orders → dashboard.sales
```

API：

```text
GET /lineage/{entity}
```

用途：

- 影响分析；
- 合规；
- 故障排查；
- 数据质量；
- 变更影响；
- 成本归因。

### 血缘的粒度

- 表级；
- 列级；
- 作业级；
- 运行时；
- 静态分析。

## 43.7 数据质量

- 空值率；
- 唯一性；
- 范围；
- 新鲜度；
- 行数异常；
- schema 漂移；
- 分布变化。

API：

```text
GET /tables/{table}/quality
GET /tables/{table}/freshness
```

### 质量规则

```json
{
  "table": "orders",
  "rules": [
    { "type": "not_null", "column": "id" },
    { "type": "unique", "column": "id" },
    { "type": "range", "column": "amount", "min": 0 },
    { "type": "freshness", "column": "created_at", "maxAge": "1h" }
  ]
}
```

## 43.8 权限与治理

- 库/表/列/行级权限；
- 标签；
- 脱敏；
- 审计；
- 数据分类；
- 保留；
- 删除；
- 跨境。

列级权限：

```json
{
  "table": "customers",
  "columns": {
    "email": ["role:analyst"],
    "ssn": ["role:compliance"]
  }
}
```

### SQL 重写与列级授权

用户提交的是任意 SQL，列级权限不能只靠“文档说明”。常见实现：

- 解析 SQL，提取表/列；
- 与权限矩阵比对；
- 重写查询，屏蔽无权列；
- 或改用只暴露安全列的视图；
- 对 `SELECT *` 特别处理；
- 对函数/子查询/CTE 做递归分析；
- 拒绝无法静态分析的查询。

```sql
-- 用户查询
SELECT * FROM customers;

-- 重写后
SELECT id, name, email FROM customers_secure_view;
```

列级授权是数据平台最容易被绕过的部分，必须用解析器而不是字符串替换。

### 行级权限

```sql
CREATE POLICY tenant_isolation ON orders
USING (tenant_id = current_setting('app.tenant_id')::text);
```

### 脱敏

- 静态脱敏；
- 动态脱敏；
- 哈希；
- 截断；
- 令牌化。

## 43.9 导出

```text
POST /exports
```

```json
{
  "queryId": "q_123",
  "format": "parquet",
  "destination": { "type": "s3", "uri": "s3://..." }
}
```

- 异步；
- 预签名下载；
- 分片；
- 压缩；
- 加密；
- 过期；
- 审计；
- 权限；
- 计费。

## 43.10 计费

数据平台成本：

- 扫描字节；
- 计算时长；
- 存储；
- 出口；
- 查询次数；
- 并发。

API 设计应支持：

- 用量计量；
- 配额；
- 预算；
- 成本预估；
- 查询取消；
- 成本归因。

### 成本预估

```http
POST /queries/estimate
```

```json
{
  "estimatedBytesScanned": 10737418240,
  "estimatedCost": 0.05,
  "currency": "USD"
}
```

在查询前告诉用户成本，可以避免账单惊讶。

### 成本计算示例

```text
扫描 10 GB，单价 $5/TB
计算 30 秒，单价 $0.10/小时
结果存储 1 GB，单价 $0.02/GB/月
```

```text
扫描：10 / 1024 × 5 ≈ $0.049
计算：30 / 3600 × 0.10 ≈ $0.0008
存储：1 × 0.02 ≈ $0.02
合计：约 $0.07
```

成本模型要能按：

- 租户；
- 查询；
- 表；
- 用户；
- 项目；

归因，否则无法优化。

## 43.11 开放表格式、查询协议与数据共享

前面讨论的是“怎么查询”。这一节讨论“查询的对象是什么”，以及数据如何以契约形式在组织之间流动。现代数据平台的 API 表面，往往不是一张数据库表，而是**表格式 + 目录 + 查询协议 + 共享协议**的组合。

### 43.11.1 表格式：从“文件集合”到“有事务的表”

数据湖里的 Parquet 文件本身没有事务、没有 schema 演进、没有快照。开放表格式在文件之上增加一层元数据：

| 格式 | 元数据 | 关键能力 | 典型取舍 |
|---|---|---|---|
| Apache Iceberg | metadata + manifest list + manifest + snapshot | 快照隔离、隐藏分区、分区演进、时间旅行 | 元数据层较厚，commit 依赖 catalog |
| Delta Lake | `_delta_log` 事务日志 + checkpoint | ACID、时间旅行、schema 演进、乐观并发 | 日志重放与 checkpoint 管理 |
| Apache Hudi | timeline + 文件组 |  upsert、增量拉取、Copy-on-Write/Merge-on-Read | 运维与 compaction 复杂 |

对 API 设计而言，表格式提供了几个可写进契约的语义：

- **快照**：一次查询绑定一个 snapshot ID，结果可复现；
- **时间旅行**：按时间戳/版本读取历史；
- **schema 演进**：加列、改类型、重命名有明确规则；
- **分区演进**：分区策略变化不要求重写全部历史；
- **增量读取**：只取两个快照之间的变更。

这些能力会改变 API 的形状。例如：

```http
POST /queries
{
  "table": "analytics.orders",
  "snapshot": "8291736450192837465",
  "sql": "SELECT status, count(*) FROM t GROUP BY status"
}
```

如果响应只返回结果，不返回 snapshot/commit ID，调用方就无法复现或对账。**可复现性是数据 API 的一等契约**。

### 43.11.2 Catalog 与提交协议

表格式需要一个 catalog 来回答：

- 表在哪里？
- 当前快照是哪个？
- 谁可以提交新快照？
- 并发写入如何冲突解决？

Iceberg REST Catalog 把这些问题标准化为 HTTP API：

```text
GET  /v1/config
GET  /v1/namespaces
GET  /v1/namespaces/{ns}/tables
POST /v1/namespaces/{ns}/tables
POST /v1/namespaces/{ns}/tables/{table}/metrics
POST /v1/namespaces/{ns}/tables/{table}/transactions/commit
```

关键设计点：

- **原子提交**：catalog 用 compare-and-swap 把表指针从旧快照换到新快照；
- **乐观并发**：两个 writer 同时提交时，只有一个成功，另一个收到冲突；
- **幂等**：重试 commit 必须安全，通常用 request ID / commit UUID 去重；
- **权限**：catalog 是元数据控制面，必须独立授权，不能只靠存储桶权限；
- **审计**：谁在什么时候提交了哪个快照、改了哪些文件。

一个常见的错误是只给存储桶读写权限，却让客户端直接改 metadata。这绕过了 catalog 的并发控制，可能产生“丢失更新”或损坏表。正确做法是：**写路径只经过 catalog/表服务，存储凭证由服务短期签发**。

### 43.11.3 查询协议：REST、JDBC 与 Arrow Flight SQL

数据平台的查询接口大致有三类：

| 协议 | 形态 | 适合 | 注意 |
|---|---|---|---|
| REST/JSON | 提交 SQL，轮询/流式取结果 | 通用、易集成、跨语言 | 大结果序列化开销、分页 |
| JDBC/ODBC | 数据库标准协议 | BI 工具、既有生态 | 连接管理、驱动兼容、超时 |
| Arrow Flight SQL | 基于 gRPC 的列式传输 | 高吞吐分析、跨语言 | 生态较新、网络与认证要求 |

Arrow Flight SQL 的价值在于：

- 列式传输，避免 JSON 行列转换；
- 基于 gRPC 流式返回，适合大结果；
- 支持 prepared statement、metadata 发现；
- 可以与 Arrow 生态（Pandas、Polars、DuckDB）直接衔接。

但它不是“更快的 REST”：

- 需要 gRPC 基础设施、HTTP/2、连接池；
- 浏览器支持有限，通常要经过网关或 BFF；
- 认证/授权要与 Flight 的 token 机制对齐；
- 结果集仍要限流、超时、取消；
- 不适合小请求、低频调用。

查询网关（如 Trino Gateway、Superset 前置层）通常负责：

- 路由到集群；
- 认证与 impersonation；
- 资源组与队列；
- 查询标签与成本归因；
- 结果缓存；
- 熔断与降级。

### 43.11.4 数据共享：让数据离开，但不失去控制

数据共享协议把“给对方一份 Parquet”变成“给对方一个受控的读取契约”。常见形态：

- **Delta Sharing**：开放的 REST 协议，接收方按 share/schema/table 读取，提供方控制授权与撤销；
- **云厂商共享**：如 Snowflake Secure Data Sharing、BigQuery Analytics Hub、AWS Clean Rooms；
- **基于对象存储的共享**：预签名 URL + 短期凭证 + 清单文件；
- **视图/行过滤/列掩码**：共享的是“经过策略的视图”，不是原始表。

一个概念性的共享 API：

```text
GET  /shares/{share}/schemas
GET  /shares/{share}/schemas/{schema}/tables
GET  /shares/{share}/schemas/{schema}/tables/{table}/metadata
POST /shares/{share}/schemas/{schema}/tables/{table}/query
```

共享契约必须写清：

- **数据范围**：表、列、行过滤、时间范围；
- **新鲜度**：多久同步一次，最大延迟；
- **一致性**：快照隔离还是最终一致；
- **凭证**：短期、最小权限、可撤销；
- **审计**：谁读了什么，能否导出；
- **二次分发**：合同是否允许，是否有水印；
- **退役**：共享撤销后，对方已下载的数据怎么办。

共享不是“导出”的同义词。导出通常产生一份独立副本；共享应尽量保持**读取时授权**，并让提供方可以撤销。若必须落地副本，则要叠加合同、加密、水印与保留期。

### 43.11.5 联邦查询与语义一致性

联邦查询把多个数据源（数据湖、仓库、数据库、SaaS API）放在一个 SQL 接口下：

```sql
SELECT o.id, c.segment, p.risk_score
FROM lake.orders o
JOIN warehouse.customers c ON c.id = o.customer_id
JOIN postgres.payments p ON p.order_id = o.id;
```

设计时要回答：

- 哪些过滤/聚合能下推到源端？不能下推时会不会拉全量？
- 各源的一致性模型不同，JOIN 的结果以哪个快照为准？
- 跨源 JOIN 的延迟与成本如何预估？
- 源端权限如何映射？会不会因为联邦层的高权限账号绕过？
- 结果缓存的新鲜度与失效策略是什么？
- 某个源不可用时，返回部分结果还是失败？

**语义一致性比语法统一更难**。同一个 `order.status` 在湖里是字符串、在仓库里是枚举、在支付系统里是整数；同一个 `revenue` 可能含税/不含税、含退款/不含退款。联邦查询必须在目录层暴露这些语义，而不是让每个分析师自己猜。

### 43.11.6 对 API 契约的影响

| 能力 | 契约中要写什么 |
|---|---|
| 快照读取 | snapshot ID / as-of 时间；结果可复现 |
| 提交 | commit ID、冲突错误、幂等键、重试语义 |
| schema 演进 | 兼容规则、未知列处理、类型变更 |
| 数据共享 | share/table 标识、凭证有效期、撤销语义 |
| 联邦查询 | 源列表、下推能力、一致性、部分失败 |
| 成本 | 扫描字节、计算时长、预估与上限 |
| 权限 | 表/列/行/视图、impersonation、审计 |
| 新鲜度 | data-through、最大延迟、告警 |

一个成熟的响应应该同时包含：

```json
{
  "query_id": "q_123",
  "snapshot": "8291736450192837465",
  "data_through": "2026-11-26T09:55:00Z",
  "schema_version": 7,
  "rows": [],
  "truncated": false,
  "cost": { "bytes_scanned": 10737418240, "estimated_usd": 0.05 }
}
```

没有 snapshot、data-through 与 schema version 的结果，在事故复盘与对账时几乎无法使用。

### 43.11.7 检查清单

- [ ] 查询结果绑定 snapshot/commit，可复现。
- [ ] 提交路径经过 catalog，使用乐观并发与幂等键。
- [ ] 存储凭证短期、最小权限、按表/前缀签发。
- [ ] schema 演进有兼容规则与 CI 检查。
- [ ] 共享协议支持撤销、审计、行/列策略。
- [ ] 联邦查询声明下推能力、一致性与部分失败行为。
- [ ] 结果包含 data-through、schema version 与成本。
- [ ] 大结果走 Arrow Flight/对象存储，不塞进 JSON。
- [ ] 查询网关负责认证、队列、限流与成本归因。

## 43.12 本章小结

- 数据平台 API 要处理大数据量、长查询、复杂 schema 和治理。
- 接入要支持批量/流式、幂等、schema。
- 查询分同步/异步，结果分页/导出。
- SQL over HTTP 要严格限制权限和资源。
- 目录、血缘、质量、权限是平台能力。
- 导出与计费是产品化关键。
- 成本预估能减少账单惊讶。
- 开放表格式把“文件”变成有快照、事务与 schema 演进的表；catalog 是提交与并发的控制面。
- 查询协议（REST、JDBC、Arrow Flight SQL）服务不同消费者，不能互相替代。
- 数据共享应尽量保持读取时授权与可撤销性，而不是复制一份失控的副本。
- 结果要携带 snapshot、data-through 与 schema version，否则无法复现与对账。
- 数据契约、指标口径版本、语义层与特征 point-in-time 正确性，见[附录 AB](../appendices/ab-data-contracts-metrics.md)；隐私聚合与差分隐私见[附录 AG](../appendices/ag-privacy-enhancing-tech.md)。
- 数据中的时间、金额、单位与标识语义（时区、账期、精度、汇率、币种、UCUM）见[附录 AH](../appendices/ah-primitive-semantics.md)。

## 设计练习

为“企业数据仓库 API”设计：

- 数据接入；
- SQL 查询；
- 异步任务；
- 结果导出；
- 表目录；
- 血缘；
- 列级权限；
- 用量计费。

写出查询状态机与权限模型。然后回答：

1. 如何防止一个查询拖垮整个仓库？
2. 列级权限如何与 SQL 解析结合？
3. 导出大结果集如何限流？
4. 如何向客户解释查询成本？

---

上一章：[IoT 与设备 API](42-iot.md)  
下一章：[内部平台与平台工程 API](44-internal-platform.md)

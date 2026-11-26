# 附录 AB：数据契约、指标语义与数据产品 API

> 业务 API 的契约通常围绕“一次请求改变或读取一个对象”；数据 API 的契约围绕“一批数据在什么时间、以什么口径、由谁负责、质量如何”。这两类契约的失败方式完全不同：业务 API 错一个字段可能让一次下单失败；数据 API 错一个口径可能让全公司的季度报表、奖金和董事会决策一起错。本附录补齐第 43 章未展开的数据契约、指标语义层与数据产品化。

## AB.1 数据 API 与业务 API 的差异

| 维度 | 业务 API | 数据 API |
|---|---|---|
| 调用形态 | 单对象、低延迟、请求-响应 | 批量、扫描、异步、流式 |
| 数据量 | 字节到 KB | MB 到 TB |
| 一致性 | 强一致或会话保证 | 快照、水位线、分区一致性 |
| 时间语义 | 事件发生时间 | 事件时间、处理时间、数据截止时间 |
| 错误形态 | 单请求失败 | 部分分区失败、迟到数据、口径错误 |
| 消费者 | 应用、Agent | BI、ML、报表、下游数据产品 |
| 演进 | 字段兼容 | Schema、分区、口径、回填 |
| 责任 | 服务 owner | 数据 owner + 指标 owner |

数据 API 的核心问题不是“怎么把表暴露出去”，而是：

1. 这批数据代表什么业务事实？
2. 它什么时候完整？水位线在哪里？
3. 口径由谁定义、版本是什么？
4. 质量如何度量？坏了谁负责？
5. 消费者如何知道数据变了？

## AB.2 数据契约的六个要素

一个可执行的数据契约至少包含六部分。

### AB.2.1 Schema

- 字段名、类型、可空性；
- 主键/唯一键；
- 分区键与排序；
- 枚举与取值范围；
- 嵌套结构；
- 字符集与排序规则；
- 单位与精度（金额、时长、百分比）。

### AB.2.2 语义

Schema 只说明“长什么样”，语义说明“是什么意思”：

```yaml
field: gross_revenue
type: decimal(18,2)
unit: CNY
definition: |
  商品成交金额，含税、未扣除折扣与退款；
  以订单支付成功时间为归属时间；
  不包含运费与赠品。
owner: finance-metrics@example.com
```

没有语义的字段，消费者只能猜。`amount` 是含税还是不含税？`revenue` 是 GMV 还是净收入？`active_user` 是登录还是下单？

### AB.2.3 所有权

- 数据 owner（业务负责人）；
- 技术 owner（管道/表）；
- 指标 owner（口径）；
- 质量 owner；
- 访问审批人；
- 事故联系人。

所有权不是“谁建的表”，而是“谁对数据的正确性和可用性负责”。

### AB.2.4 新鲜度

```yaml
freshness:
  expected: 15m
  warning: 30m
  critical: 2h
  timezone: UTC
  dataThroughField: event_time
```

“每天更新”不是新鲜度承诺。要写清：

- 更新频率；
- 数据截止时间（watermark）；
- 迟到数据的处理窗口；
- 失败时的最大延迟；
- 监控与告警。

### AB.2.5 质量

```yaml
quality:
  - rule: order_id is not null and unique
    severity: critical
  - rule: amount >= 0
    severity: critical
  - rule: status in ('PAID','REFUNDED','CANCELLED')
    severity: warning
  - rule: daily_row_count within 3 sigma of 7-day average
    severity: warning
```

质量规则要可执行、可告警、有 owner、有处理流程。只写“保证数据质量”没有意义。

### AB.2.6 生命周期

- Schema 版本与兼容策略；
- 废弃字段与迁移窗口；
- 数据保留与删除；
- 回填与重算；
- 分区保留；
- 访问权限变更；
- 合同终止后的数据处置。

## AB.3 机器可读的契约

### AB.3.1 Schema 描述

| 格式 | 适合 | 注意 |
|---|---|---|
| JSON Schema | JSON API、事件、配置 | 表达力强，但类型系统与 SQL 不完全对应 |
| Avro | Kafka、批处理 | Schema Registry 生态成熟，适合演进 |
| Protobuf | gRPC、跨语言 | 字段编号不可复用，适合高性能 |
| Parquet/ORC schema | 数据湖 | 列式存储，需与逻辑模型映射 |
| dbt contracts | 数仓模型 | 与 SQL 模型同仓库，适合分析工程 |

### AB.3.2 数据契约标准

Bitol 的 Data Contract Spec、Open Data Contract Standard（ODCS）等尝试用 YAML 描述：

```yaml
apiVersion: v3.0.0
kind: DataContract
id: orders.v1
name: Orders
version: 1.0.0
status: active
domain: commerce
tenant: example
owner: orders-team@example.com

schema:
  - name: orders
    logicalType: object
    properties:
      - name: order_id
        logicalType: string
        required: true
        unique: true
      - name: customer_id
        logicalType: string
        required: true
      - name: total_amount
        logicalType: number
        required: true
      - name: currency
        logicalType: string
        required: true
      - name: status
        logicalType: string
        required: true
      - name: paid_at
        logicalType: timestamp
        required: false

quality:
  - rule: order_id is unique
    severity: critical
  - rule: total_amount >= 0
    severity: critical

serviceLevel:
  freshness: 15m
  availability: 99.9%
```

不同标准的字段名不同，但核心一致：**schema + 语义 + 质量 + SLA + 所有权 + 生命周期**。选一个团队能维护的，不要为了“标准化”引入三套并行描述。

### AB.3.3 契约的存放位置

- 与生产者代码同仓库（推荐）；
- 发布到 schema registry / data catalog；
- 消费者通过引用而不是复制；
- 变更走 PR 与兼容性检查；
- 契约版本与数据版本可对应；
- 文档从契约生成，避免双写。

## AB.4 Schema 演进与兼容性

### AB.4.1 兼容性类型

| 类型 | 含义 | 场景 |
|---|---|---|
| Backward | 新代码能读旧数据 | 消费者先升级 |
| Forward | 旧代码能读新数据 | 生产者先升级 |
| Full | 双向兼容 | 长期共存、事件流 |

数据 API 通常要求 **full 兼容**，因为消费者可能滞后数月，历史数据也可能被重放。

### AB.4.2 安全变更

- 新增可选字段；
- 新增枚举值（消费者必须容忍未知值）；
- 放宽约束（如 nullable）；
- 增加文档与语义说明；
- 不删除、不重命名、不改类型、不改单位；
- 不改变字段含义（最隐蔽的破坏）。

### AB.4.3 破坏性变更的处理

```text
v1 继续服务
→ 发布 v2（新表/新视图/新主题）
→ 双写/回填
→ 消费者迁移
→ 对账
→ 下线 v1
```

不要在原表上“就地升级”。数据消费者不像 API 客户端那样能快速回滚；历史数据一旦按新口径重算，旧报表就无法复现。

### AB.4.4 回填

回填是数据 API 特有的发布动作：

- 回填范围（分区/时间窗口）；
- 回填期间的读一致性；
- 幂等与去重；
- 回填完成标记；
- 新旧数据对账；
- 回填失败的回滚；
- 消费者通知。

回填不是“跑个 SQL”。它会改变历史，必须像 API 破坏性变更一样管理。

## AB.5 指标 API 的语义

### AB.5.1 指标定义的要素

```yaml
metric: net_revenue
display_name: 净收入
unit: CNY
aggregation: sum
numerator: gross_revenue - discounts - refunds
dimensions:
  - date
  - region
  - product_category
  - channel
time_grain: day
timezone: Asia/Shanghai
data_through: event_time
freshness: 2h
owner: finance-metrics@example.com
version: 3
```

任何一项缺失都会导致“同名不同义”：

- `revenue` 是含税还是不含税？
- `active_user` 是 DAU、WAU 还是月活？
- `churn` 是按用户数还是按收入？
- `refund_rate` 的分母是订单数、金额还是支付笔数？
- 日期归属用事件时间、支付时间还是结算时间？
- 时区是 UTC 还是业务本地时区？

### AB.5.2 指标 API 的响应

```json
{
  "metric": "net_revenue",
  "definitionVersion": 3,
  "unit": "CNY",
  "timeGrain": "day",
  "timezone": "Asia/Shanghai",
  "dataThrough": "2026-11-25T23:59:59+08:00",
  "computedAt": "2026-11-26T02:10:00+08:00",
  "filters": {
    "region": "apac",
    "channel": "web"
  },
  "series": [
    { "date": "2026-11-24", "value": 1289000 },
    { "date": "2026-11-25", "value": 1356000 }
  ],
  "quality": {
    "status": "OK",
    "completeness": 0.998,
    "lateEvents": 12
  }
}
```

要点：

- 返回 `definitionVersion`，让消费者知道口径；
- 返回 `dataThrough`，让消费者知道数据截止；
- 返回 `computedAt`，便于排查缓存；
- 返回 `quality`，让消费者决定是否使用；
- 返回 `filters`，保证结果可复现；
- 金额带币种，不带浮点；
- 时间带时区；
- 分页/截断要明确。

### AB.5.3 口径版本

口径变更必须版本化：

```text
v2：退款按支付时间归属
v3：退款按退款时间归属，并扣除优惠券分摊
```

消费者可以：

- 固定 `definitionVersion=2` 复现旧报表；
- 显式升级到 v3；
- 对比两版差异；
- 在合同/审计中引用具体版本。

没有版本的口径变更，会让“为什么这个月和上个月对不上”变成永久争议。

## AB.6 语义层与指标平台

### AB.6.1 语义层解决什么

语义层把“表 + SQL”抽象成“实体 + 维度 + 指标 + 关系”：

```text
Order ──< OrderItem >── Product
  │
  └── Customer ──< Region
```

消费者写：

```text
sum(net_revenue) by region, month where channel = 'web'
```

语义层负责：

- 生成正确 SQL（join 路径、去重、时间粒度）；
- 统一口径；
- 行级/列级权限；
- 查询成本限制；
- 缓存与预热；
- 口径版本与血缘。

### AB.6.2 语义层 API 的设计

```http
POST /metrics/query
Content-Type: application/json

{
  "metrics": ["net_revenue", "order_count"],
  "dimensions": ["region", "month"],
  "filters": [
    { "field": "channel", "op": "eq", "value": "web" }
  ],
  "timeRange": {
    "start": "2026-01-01",
    "end": "2026-11-01",
    "timezone": "Asia/Shanghai"
  },
  "definitionVersion": 3,
  "maxRows": 10000
}
```

响应包含数据、口径、质量、查询 ID：

```json
{
  "queryId": "q_01J...",
  "definitionVersion": 3,
  "columns": [
    { "name": "region", "type": "string" },
    { "name": "month", "type": "date" },
    { "name": "net_revenue", "type": "decimal", "unit": "CNY" },
    { "name": "order_count", "type": "integer" }
  ],
  "rows": [
    ["apac", "2026-01", 1289000, 4210],
    ["emea", "2026-01", 987000, 3120]
  ],
  "rowCount": 2,
  "truncated": false,
  "dataThrough": "2026-11-25T23:59:59+08:00",
  "quality": { "status": "OK" }
}
```

设计要点：

- 指标名稳定，维度名稳定；
- 不暴露底层表名与 join 逻辑；
- 查询有成本上限（扫描量、超时、行数）；
- 大结果走异步导出；
- 权限在语义层执行，不只靠 BI 前端；
- 返回 `queryId` 便于审计与复现；
- 相同查询可缓存，但缓存键包含口径版本与权限范围。

### AB.6.3 BI 与 API 的口径一致性

最常见的组织问题：BI 团队用一套 SQL，产品 API 用另一套 SQL，两个数字对不上。

解决：

- 指标定义只有一个 owner；
- BI 与 API 都从语义层取数；
- 用同一份测试数据对拍；
- 发布前做“数字对拍”；
- 监控同名指标的差异；
- 口径变更同步通知所有消费者。

## AB.7 特征与 ML 数据 API

### AB.7.1 训练/服务 skew

ML 模型在离线训练时用一批特征，在线推理时用另一批特征。如果两者口径不同，模型效果会下降：

```text
离线：user_7d_purchase_count 按自然日、含退款
在线：user_7d_purchase_count 按滚动 168 小时、不含退款
```

### AB.7.2 Point-in-time 正确性

训练样本必须只使用“当时可见”的特征，避免标签泄漏：

```sql
SELECT
  e.user_id,
  e.label,
  f.purchase_count_7d
FROM events e
LEFT JOIN LATERAL (
  SELECT purchase_count_7d
  FROM feature_snapshots f
  WHERE f.user_id = e.user_id
    AND f.snapshot_time <= e.event_time
  ORDER BY f.snapshot_time DESC
  LIMIT 1
) f ON true
```

关键是取“事件发生时刻最近的一次快照”，而不是把 7 天内所有快照都 join 进来。后者会产生重复行，还可能把事件之后写入的快照混进训练样本。部分引擎提供 `ASOF JOIN`，语义更直接。

要求：

- 特征快照带 `valid_from` / `valid_to`；
- 事件带准确的 `event_time`；
- 迟到事件不能改写历史快照；
- 训练与服务使用同一套特征定义；
- 特征版本与模型版本绑定；
- 可回放某个历史时点的特征。

### AB.7.3 特征 API

```http
POST /features/online
Content-Type: application/json

{
  "entityType": "user",
  "entityIds": ["u_123", "u_456"],
  "features": ["purchase_count_7d", "avg_order_value_30d"],
  "featureVersion": "2026-11-01"
}
```

响应：

```json
{
  "featureVersion": "2026-11-01",
  "entities": [
    {
      "entityId": "u_123",
      "values": {
        "purchase_count_7d": 4,
        "avg_order_value_30d": 128.5
      },
      "freshness": "2026-11-26T09:50:00Z"
    }
  ],
  "missing": []
}
```

要点：

- 批量查询有上限；
- 缺失值语义明确（`null` vs 默认值 vs 缺失列表）；
- 特征新鲜度可查；
- 特征版本可固定；
- 在线/离线一致性可验证；
- 敏感特征有访问控制与审计。

## AB.8 数据产品与 Data Mesh

### AB.8.1 数据产品的四个属性

| 属性 | 含义 | API 体现 |
|---|---|---|
| 可发现 | 消费者能找到 | 目录、搜索、文档、示例 |
| 可寻址 | 有稳定标识 | 数据集 ID、版本、URL |
| 可信 | 有质量与 SLA | 新鲜度、质量报告、owner |
| 可互操作 | 有标准 schema/协议 | OpenAPI/AsyncAPI/Avro、标准字段 |

### AB.8.2 领域所有权

Data Mesh 的核心不是“去中心化”，而是“领域团队对数据产品负责”：

- 领域团队拥有生产管道与数据质量；
- 平台团队提供存储、计算、目录、权限、可观测性；
- 消费者通过契约使用，不直接读别人的表；
- 跨域指标由指标 owner 统一。

失败模式：

- 每个团队建自己的湖，形成数据沼泽；
- 目录存在但没人维护；
- 质量规则只在生产团队内部看；
- 消费者绕过契约直接读表；
- 平台团队被迫承担所有数据质量。

### AB.8.3 数据产品的定价

内部数据产品通常不直接收费，但需要：

- 成本归因（存储、扫描、计算）；
- 配额与预算；
- 高价值数据集优先保障；
- 避免“全公司扫同一张大表”。

对外数据产品（数据 API、洞察 API）可以按：

- 订阅；
- 查询量；
- 数据量；
- 席位；
- 报告/洞察；

定价要覆盖边际成本（扫描、出口流量、合规），否则用量增长会变成亏损。

## AB.9 共享、导出与可携带性

### AB.9.1 批量导出

- 一致性快照（同一时间点）；
- 稳定排序；
- 分片与校验和；
- 预签名 URL 与 TTL；
- 脱敏与权限过滤；
- 导出审计；
- 大导出异步任务；
- 失败可重试、可断点续传。

### AB.9.2 CDC

变更数据捕获把数据库变更转成事件流：

```text
orders 表 → CDC → Kafka → 下游数仓/搜索/缓存
```

契约要点：

- 变更事件包含 `before`/`after` 或至少主键与版本；
- 删除用墓碑；
- 顺序按主键分区；
- schema 演进兼容；
- 重复消费幂等；
- 初始快照与增量切换一致；
- 不泄露敏感列。

### AB.9.3 数据可携带性

GDPR 等法规要求数据主体能导出自己的数据。API 设计要支持：

- 按主体 ID 导出；
- 机器可读格式（JSON/CSV）；
- 包含元数据与解释；
- 异步任务与通知；
- 身份验证与授权；
- 速率限制；
- 删除请求的传播（数据库、搜索、缓存、备份、分析管道）。

## AB.10 数据质量与可观测性

### AB.10.1 五个维度

| 维度 | 问题 | 指标 |
|---|---|---|
| 新鲜度 | 数据是否按时到 | `data_age`, `watermark_lag` |
| 完整性 | 是否缺分区/缺行 | `row_count`, `null_rate` |
| 准确性 | 值是否正确 | 对账差异、规则失败率 |
| 一致性 | 跨表/跨系统是否一致 | 主键覆盖、金额总和 |
| 唯一性 | 是否重复 | 重复率、主键冲突 |

### AB.10.2 数据事故

数据事故的响应与 API 事故不同：

- 影响可能是“报表错了三天”而不是“服务挂了”；
- 需要追溯口径、回填、通知消费者；
- 需要判断是否影响对外披露/合规；
- 需要冻结下游使用；
- 需要发布更正数据与说明。

事故记录要包含：

- 发现时间与方式；
- 影响的数据集/指标/时间范围；
- 根因（管道、schema、口径、迟到数据）；
- 修复与回填；
- 消费者通知；
- 防复发措施。

## AB.11 案例：从指标定义到 API

需求：向合作伙伴提供“月度净收入”指标。

### 第一步：定义

```yaml
metric: net_revenue
definition: gross_revenue - discounts - refunds
grain: month
timezone: Asia/Shanghai
attribution: payment_time
filters: [region, channel]
freshness: 24h
owner: finance-metrics
version: 1
```

### 第二步：契约

- 响应字段：`month`, `region`, `channel`, `value`, `currency`, `definitionVersion`, `dataThrough`, `quality`；
- 金额用最小单位整数；
- 月份用 `YYYY-MM`，另带时区说明；
- 缺失月份返回 `null` 还是省略？明确；
- 退款跨月如何处理？明确；
- 汇率如何折算？明确；
- 权限：合作伙伴只能看自己的 region；
- 速率限制与缓存。

### 第三步：验证

- 与财务总账对账；
- 与 BI 同名指标对拍；
- 边界测试：月末、闰年、时区、退款跨月、折扣分摊；
- 迟数据测试；
- 权限测试：跨 region 不可见；
- 回填测试：历史月份重算后版本变化。

### 第四步：运营

- 数据截止时间每日监控；
- 质量规则告警；
- 口径变更通知；
- 版本升级窗口；
- 消费者支持。

## AB.12 检查清单

- [ ] 每个数据集有 schema、语义、owner、新鲜度、质量规则、生命周期。
- [ ] 契约机器可读，存放在生产者仓库并发布到目录。
- [ ] 字段有单位、精度、时区、枚举与空值语义。
- [ ] Schema 演进遵循 full 兼容，破坏性变更走 v2 + 回填 + 对账。
- [ ] 指标有唯一定义、版本、口径 owner 与 data-through。
- [ ] BI 与 API 从同一语义层取数，并有对拍。
- [ ] 特征有 point-in-time 正确性、在线/离线一致性与版本。
- [ ] 数据产品可发现、可寻址、可信、可互操作。
- [ ] 导出有一致性快照、权限过滤、校验和与审计。
- [ ] CDC 有墓碑、顺序、幂等与 schema 兼容。
- [ ] 数据质量覆盖新鲜度、完整性、准确性、一致性、唯一性。
- [ ] 数据事故有影响评估、回填、通知与更正流程。
- [ ] 数据驻留、删除、DSAR 能传播到所有副本与分析管道。

## AB.13 小结

- 数据 API 的契约核心是“语义 + 时间 + 质量 + 所有权”，不只是 schema。
- 数据契约应机器可读、与生产者代码同仓库、有版本与生命周期。
- 指标 API 必须返回口径版本、数据截止时间、质量与过滤条件，保证可复现。
- 语义层是 BI 与产品 API 口径一致的工程手段，不是单纯的 BI 工具。
- ML 特征 API 要保证 point-in-time 正确性与在线/离线一致。
- 数据产品化需要可发现、可寻址、可信、可互操作，以及清晰的领域所有权。
- 数据事故的影响往往比服务事故更隐蔽，必须有回填、对账与通知机制。

---

相关章节：[第 30 章 契约优先、代码生成与 Schema 治理](../part-3-protocols-architecture/30-contract-first.md)、[第 43 章 数据平台与分析 API](../part-5-domain-playbooks/43-data-platform.md)、[第 25 章 事件驱动](../part-3-protocols-architecture/25-event-driven.md)、[附录 S 批量操作、导入导出与数据可携带性](s-bulk-import-export.md)、[附录 V 计量、计费与收入工程](v-metering-billing.md)、[附录 AG 隐私增强技术与 API](ag-privacy-enhancing-tech.md)

返回：[目录](../SUMMARY.md)

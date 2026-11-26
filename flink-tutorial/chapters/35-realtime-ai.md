# 第35章 实时 Data + AI：特征、推理、向量检索与 Agent

前置知识：第 4、12、17 章（事件时间、维表/Async I/O、UDF/模型函数）、第 14 章（Table/SQL）。不要求先读第 34 章，但 2.x 的 PTF 与 1.20+ 的物化表的版本边界以第 34 章为准。

流处理和 AI 的结合，最容易陷入两种极端：

- 把 Flink 当成“模型服务框架”，在算子线程里同步调用 LLM，结果延迟、成本和稳定性一起失控；
- 把 AI 当成“离线训练完再导出”的一次性工作，实时链路只负责搬运数据，错过特征新鲜度和事件驱动决策的价值。

这一章给出一个更工程化的分层：**Flink 负责事件、时间、状态和一致性；模型服务负责推理；向量库/湖仓负责检索和记忆；Agent 负责编排**。四者通过明确的契约连接，而不是把所有东西塞进一个 `map()`。

前置阅读：第 12 章（Async I/O）、第 17 章（UDF/PTF）、第 34 章（2.x 的 PTF/物化表）。本章的示例代码是教学形状，生产参数要以所用 Flink 版本、模型供应商和连接器文档为准。

## 35.1 三种结合层次

| 层次 | 问题 | Flink 的角色 | 典型延迟 | 失败影响 |
|---|---|---|---|---|
| 实时特征 | “模型/规则需要最新特征” | 窗口聚合、状态、特征回填 | 毫秒～秒 | 特征过期/泄漏 |
| 实时推理 | “每条事件都要打分” | 异步调用、批处理、限流、降级 | 十毫秒～秒 | 延迟/成本/超时 |
| 实时 Agent | “事件触发多步决策/工具调用” | 事件驱动编排、状态、审计 | 秒～分钟 | 外部副作用/不可回滚 |

三者不是互斥的。一个风控链路可能同时需要：

1. 用窗口算“最近 1 分钟交易次数”；
2. 用 `ML_PREDICT` 调风险模型；
3. 用 Agent 做人工复核建议；
4. 把特征、模型版本、Agent 决策写入湖仓审计。

关键设计原则：**推理是外部依赖，不是纯函数**。它会有超时、限流、价格、版本和不确定性；必须像对待数据库/HTTP 服务一样对待它。

## 35.2 实时特征：新鲜度、时点正确性与回填

### 特征不是“越大越好”

实时特征的价值来自“在决策时刻可用”。一个特征如果晚到 5 秒，可能比没有更糟：模型用它做了错误决策，事后还难以解释。

设计特征时先回答：

- 这个特征在决策时刻是否已经产生？
- 它的时间语义是事件时间还是处理时间？
- 允许迟到多久？超过后是丢弃、用旧值还是回填？
- 训练时用的特征口径和线上是否一致？

### 窗口特征

```sql
CREATE VIEW user_features_1m AS
SELECT
  user_id,
  window_start,
  window_end,
  COUNT(*) AS tx_count_1m,
  SUM(amount) AS tx_amount_1m,
  COUNT(DISTINCT merchant_id) AS merchant_cnt_1m
FROM TABLE(
  TUMBLE(TABLE payments, DESCRIPTOR(event_time), INTERVAL '1' MINUTE))
GROUP BY user_id, window_start, window_end;
```

这类特征的运行时状态是“key + window + 聚合值”。状态大小 ≈ 活跃 key 数 × 窗口数 × 每个窗口的状态。高基数用户 + 多窗口 + 长 allowedLateness 会让状态迅速膨胀。

优化顺序：

1. 缩短窗口/允许迟到；
2. 用增量聚合而不是保留明细；
3. 对高基数 key 做预聚合/盐化；
4. 只保留模型真正使用的特征；
5. 给 SQL 状态设 TTL，并监控实际大小。

### 时点正确性（point-in-time correctness）

训练样本必须使用“当时能看到”的特征，而不是“事后修正过”的特征。否则会出现**特征泄漏**：

```text
训练样本（离线）：
  用 T+1 的订单总额作为 T 时刻的特征  ← 泄漏了未来信息

线上推理：
  T 时刻只能看到 T 之前的订单          ← 分布不一致
```

工程手段：

- 特征表带 `event_time` 和 `ingestion_time`，查询时按 `AS OF` 语义取当时版本；
- 离线回填和线上计算共用同一份 SQL/算子逻辑；
- 记录每次决策使用的特征快照（feature snapshot id）；
- 对训练/线上特征做分布对比（PSI/KL/分位数）。

### 训练-服务偏差（training-serving skew）

常见来源：

- 离线用批 SQL，线上用流 SQL，函数/时区/空值处理不同；
- 离线用全量历史，线上只用最近窗口；
- 离线特征回填时用了修正后的维表；
- 线上缓存了旧值，离线没有缓存。

最有效的办法不是“写文档”，而是**同一份特征定义，双跑对拍**：

```text
历史事件 → 离线批算 → 特征 A
同一批事件 → 流式回放 → 特征 B
比较 A/B 的差异，按 key/时间窗口定位
```

第 36 章会把对拍/对账展开成一套正确性工程。

### 特征存储与在线查询

特征结果通常写到：

- Redis/OLAP：低延迟点查；
- Paimon/Iceberg：离线训练、审计、回填；
- Kafka：下游流式消费。

Flink 只负责“算”，不负责“存”和“查”。特征存储要解决：

- 主键与版本；
- TTL 与冷热分层；
- 点查延迟；
- 批量导出；
- 权限与血缘。

## 35.3 `ML_PREDICT`：把模型调用变成 SQL 算子

Flink 2.1 引入 Model DDL 与 `ML_PREDICT`，2.2 扩展到 Table API。它让 SQL 可以直接调用模型，而不必写 Java UDF。

### Model DDL

```sql
CREATE MODEL risk_scorer
INPUT (amount DOUBLE, tx_count_1m BIGINT, merchant_cnt_1m BIGINT)
OUTPUT (risk_score DOUBLE, risk_label STRING)
WITH (
  'provider' = 'openai-compatible',
  'endpoint' = 'https://model.internal/v1/chat/completions',
  'api-key' = '${secret:model_api_key}',
  'model' = 'risk-v3',
  'system-prompt' = 'You are a risk scorer. Return JSON with risk_score and risk_label.'
);
```

不同 provider 的参数不同；上面是示意。生产上要注意：

- **API key 不要写死在 SQL**：用密钥管理/环境变量/连接器凭证；
- **模型版本要固定**：不要把 `latest` 写进生产；
- **输入/输出 schema 要显式**：否则模型返回格式变化会直接打穿作业；
- **超时/重试/限流**要配置，不能依赖默认值。

### `ML_PREDICT` 查询

```sql
SELECT
  p.event_id,
  p.user_id,
  r.risk_score,
  r.risk_label
FROM payments p,
LATERAL TABLE(ML_PREDICT(
  TABLE p,
  MODEL risk_scorer,
  DESCRIPTOR(amount, tx_count_1m, merchant_cnt_1m),
  MAP['async', 'true', 'timeout', '2s']
)) AS r;
```

或者使用命名参数形式（以所用版本文档为准）：

```sql
SELECT *
FROM ML_PREDICT(
  INPUT => TABLE payments,
  MODEL => MODEL risk_scorer,
  ARGS => DESCRIPTOR(amount, tx_count_1m, merchant_cnt_1m),
  CONFIG => MAP['async', 'true', 'timeout', '2s']
);
```

关键语义：

- `ML_PREDICT` 是**表值函数**，具体输出行数由函数/模型定义决定；常见模型推理是一行输入对应一行输出；
- 它通常是**非确定性**的：同样的输入，模型版本/温度/服务状态不同，输出可能不同；
- 它主要面向 **append-only** 输入；不要把带 retract/upsert 的 changelog 直接喂进去；
- 输出列名冲突时，Flink 会自动加后缀；
- 异步模式下，算子会并发调用模型，但受 capacity/timeout/背压限制。

### 延迟与成本模型

同步调用会把算子线程卡住。假设：

```text
事件速率 = 10,000 events/s
调用比例 = 1%
模型 P99 延迟 = 800 ms
每次调用 tokens = 600
```

则：

```text
调用 QPS = 100
并发需求 ≈ QPS × 延迟 = 100 × 0.8 = 80
token 速率 = 100 × 600 = 60,000 tokens/s
每小时 tokens = 216,000,000
```

如果按每百万 token 0.5 美元（仅示意），仅模型调用就约 **108 美元/小时**。这还没算重试、失败重放、向量检索和人工复核。

所以实时 AI 的第一道防线不是“模型多强”，而是：

- 只对必要事件调用模型；
- 先规则/小模型过滤，再大模型复核；
- 缓存相同/相似请求；
- 设置超时、熔断、降级；
- 把调用成本作为一等指标。

### 失败语义

模型调用不是事务性的。Flink 的 checkpoint 能恢复算子状态，但**不能回滚已经发生的模型调用**。因此：

- 如果调用有副作用（扣费、发消息、写外部库），必须幂等或走事务 sink；
- 如果只是打分，重复调用通常可接受，但要在结果里记录 `request_id`/模型版本；
- 超时要走死信/侧输出，不要静默丢弃；
- 下游要能处理“同一事件多次打分”的情况。

## 35.4 `VECTOR_SEARCH`：实时 RAG 的检索层

`VECTOR_SEARCH` 是 2.2 引入的表值函数，用于在流式 SQL 中做向量相似度检索。它的典型场景是实时 RAG、推荐、去重、语义告警。

### 基本语法

位置参数顺序容易记错。Flink 2.2 官方文档的顺序是 `(SEARCH_TABLE, COLUMN_TO_QUERY, COLUMN_TO_SEARCH, TOP_K[, CONFIG])`：第 2 个是“查询向量”，第 3 个才是“被检索的列”。为避免版本差异，建议直接用命名参数（命名参数的顺序不重要）：

```sql
SELECT
  e.event_id,
  e.user_id,
  v.doc_id,
  v.score
FROM events e,
LATERAL TABLE(VECTOR_SEARCH(
  SEARCH_TABLE => TABLE knowledge_base,
  COLUMN_TO_QUERY => e.query_vector,
  COLUMN_TO_SEARCH => DESCRIPTOR(embedding),
  TOP_K => 10,
  CONFIG => MAP['async', 'true', 'timeout', '1s']
)) AS v;
```

如果所用版本文档给的是位置参数形式，按 `TABLE → 查询向量 → DESCRIPTOR(检索列) → TOP_K` 的顺序写；不要把 `CONFIG` 塞到 `TOP_K` 的位置。部分厂商方言（如阿里云 VVR）把 `COLUMN_TO_SEARCH` 放在 `COLUMN_TO_QUERY` 前面，并可能带 `OUTPUT_COLUMNS` 等额外参数，落地时以所用发行版为准。

### 语义与限制

- 检索表必须实现 `VectorSearchTableSource` 能力；不是所有 connector 都支持；
- 默认按**最新快照**做处理时间关联，重放时结果不一定可复现；
- `LATERAL` 在需要与输入行关联时通常必需；
- `TOP_K`、距离函数、索引类型由底层存储决定；
- 异步检索同样受 capacity/timeout/背压限制；
- 向量维度、索引刷新延迟、召回率都会影响结果。

### 实时 RAG 的链路

```text
文档/知识库
  → 清洗/脱敏/切分
  → embedding 模型
  → 向量表（Paimon/Fluss/专用向量库）
       ↑
事件流 → 查询向量 → VECTOR_SEARCH → LLM/Agent → 结果
```

Flink 可以做：

- 文档变更捕获（CDC）；
- 切分/去重/embedding；
- 向量表增量写入；
- 查询侧的事件预处理和检索；
- 结果审计。

Flink 不适合：

- 大规模向量索引的长期存储与低延迟点查（交给专用向量库/湖仓）；
- 高频全量重建索引；
- 需要强一致、可复现的检索快照（当前语义仍是处理时间关联）。

### 成本与一致性

- 每次检索的延迟 = 网络 + 索引查询 + 结果传输；
- `TOP_K` 越大，下游 LLM 的上下文越长，token 成本越高；
- 向量表更新后，检索结果可能立刻变化，导致同一事件重放结果不同；
- 需要记录 `index_version`/`embedding_model_version`，否则无法解释决策。

## 35.5 Flink Agents：事件驱动的 Agent 运行时

Flink Agents 是 Apache Flink 的子项目（FLIP-531），由 Flink PMC 管理，不是独立的 ASF 孵化器项目；0.4.0 于 2026-10-30 发布，是 1.0 前的最后一个预览版。

### 它是什么

它把 Agent 作为 Flink 作业里的算子运行：

```text
事件流 → Agent Operator → 输出流
              │
              ├── 短期/长期记忆
              ├── 模型（OpenAI、DashScope、Jev 等；OpenAI 兼容端点通常可接入）
              ├── 工具（MCP/本地函数/HTTP）
              └── 子 Agent
```

核心抽象是 **Events、Actions、Resources**：

- Event：触发 Agent 的输入；
- Action：Agent 的处理步骤（调用模型、调用工具、写记忆）；
- Resource：模型、工具、提示词、MCP server、技能等，按名称引用。

它支持 Java、Python、YAML 三种 API；底层仍然用 Flink 的 checkpoint、状态和并行度。

### 可靠性模型

Agent 的外部调用（模型、工具、数据库）不是 Flink 状态的一部分。Flink Agents 用两层机制处理：

1. **Flink checkpoint**：保存 Agent 的内部状态、事件位置；
2. **外部 Action State Store**：记录已完成的动作和 durable-call 结果；恢复时复用，避免重复执行有副作用的动作；
3. **Reconciler**：处理“调用已发出但结果未知”的在途请求。

这比“在 ProcessFunction 里直接调用 LLM”可靠得多，但仍然不是银弹：

- 外部系统必须支持幂等键/查询结果；
- 长时间运行的工具调用会拉长 checkpoint/恢复时间；
- 人工审批（human-in-the-loop）需要额外的状态机和超时；
- 模型输出不确定，审计要记录输入、模型版本、提示词版本和原始输出。

### 0.4.0 的关键能力

| 能力 | 作用 | 工程注意 |
|---|---|---|
| 子 Agent | 任务分解/委派 | 调用链/超时/成本 |
| Decision Model API | 统一 Jev/OpenAI/阿里云等模型 | 版本/路由/降级 |
| 模型路由 | 按请求选模型 | 成本/延迟/质量权衡 |
| 多模态消息 | 文本/图像等 | 存储/带宽/隐私 |
| `ctx.chat` | 一次动作内完成模型调用与响应处理 | 超时/重试 |
| Trace Log | 步骤/模型/工具/子 Agent 调用追踪 | 日志量/敏感信息 |
| MCP | 工具/提示词接入 | 权限/审计/网络 |
| Mem0 长期记忆 | 跨会话记忆 | 隐私/删除/一致性 |
| Agent Skills | 指令/参考资料 | 版本管理 |

### 适合与不适合

适合：

- 实时客服/工单分派；
- 风控复核（规则 + LLM + 人工）；
- 设备运维助手；
- 实时内容审核；
- 事件驱动的多步工具编排。

不适合：

- 微秒/毫秒级硬实时；
- 高频、低价值、可规则化的事件；
- 需要强一致、可回滚外部副作用的场景（除非外部系统配合）；
- 没有成本/延迟预算的“全量 LLM”方案。

### 一个参考拓扑

```text
Kafka(events)
  → 规则/小模型过滤
  → Agent Operator（只在需要时调用）
      ├── 检索（VECTOR_SEARCH/工具）
      ├── LLM 推理
      ├── 工具调用（MCP）
      └── 记忆写入
  → 决策流（Kafka）
  → 审计湖仓（Paimon/Iceberg）
  → 人工复核队列
```

关键设计：**先过滤，再调用**。让 99% 的事件走规则/小模型，1% 走大模型/Agent，成本才能可控。

## 35.6 LLM 数据管道：从原始事件到可用上下文

实时 RAG/Agent 的上游往往是一条数据管道：

```text
CDC/日志/文档
  → 解析（JSON/PDF/HTML）
  → 清洗（去噪、去重、脱敏）
  → 切分（chunk）
  → embedding
  → 向量表/索引
  → 检索/Agent
```

Flink 适合做其中的流式部分：

- 文档变更捕获；
- 增量切分/embedding；
- 去重（按 doc_id/chunk_hash）；
- 元数据 enrichment；
- 写入向量表/湖仓；
- 失败重试/死信。

不适合：

- 大文件解析的 CPU 密集任务（放独立服务）；
- 全量重建索引的批任务（放批引擎/向量库工具）；
- 长期存储原始文档（放对象存储/湖仓）。

### 切分与去重

- chunk 大小影响召回率和 token 成本；
- 重叠窗口提高召回，但增加存储和 embedding 成本；
- 按 `doc_id + chunk_index + content_hash` 去重，避免重复 embedding；
- 文档更新时，要能删除旧 chunk（changelog/主键 upsert）；
- PII 要在 embedding 前脱敏，否则会进入向量和日志。

### 限流与背压

embedding 服务和 LLM 都有 QPS/token 限额。流作业要做：

- 令牌桶/并发限制；
- 指数退避 + 抖动；
- 死信队列；
- 降级（跳过/缓存/小模型）；
- 监控 429/5xx/超时。

不要用无界队列硬扛。Flink 的反压会传导到 source，最终影响整条链路；如果 source 不能停，必须有丢弃/降级策略。

## 35.7 工程化：模型、提示词、评测与治理

### 版本管理

要记录：

- 模型名称/版本/供应商；
- 提示词模板版本；
- 工具/MCP server 版本；
- 向量模型/索引版本；
- 特征定义版本；
- Flink 作业版本/uid。

没有这些，事故复盘只能靠猜。

### 评测与灰度

- 离线评测：历史回放 + 标注集；
- 影子流量：新模型只打分不生效；
- 灰度：按用户/流量比例切换；
- A/B：业务指标 + 成本 + 延迟；
- 回滚：模型/提示词/索引都要能回退。

### 护栏（guardrails）

- 输入长度/格式限制；
- 输出 schema 校验；
- 敏感词/合规过滤；
- 工具调用白名单；
- 金额/权限等高风险动作需要人工确认；
- 超时/重试/熔断。

### 可观测性

| 指标 | 用途 |
|---|---|
| 调用 QPS / 并发 | 容量 |
| P50/P95/P99 延迟 | SLO |
| 错误率（超时/429/5xx） | 稳定性 |
| token 数/成本 | 预算 |
| 缓存命中率 | 成本/延迟 |
| 检索召回/相关性 | 质量 |
| 模型/提示词版本分布 | 灰度/回滚 |
| 人工复核率/通过率 | 业务效果 |

### 安全与隐私

- API key 放密钥管理，不进代码/SQL/日志；
- 传输 TLS，存储加密；
- PII 脱敏/最小化；
- 模型供应商的数据保留/训练政策；
- 跨境数据流合规；
- 审计日志保留期限。

## 35.8 参考架构与容量估算

### 实时风控 + RAG + Agent

```text
支付/行为事件
  → 标准化/去重
  → 窗口特征（1m/1h/24h）
  → 规则过滤（90%+）
  → ML_PREDICT（风险分）
  → VECTOR_SEARCH（相似案例/名单）
  → Agent（仅高风险，工具调用/复核建议）
  → 决策 Kafka
  → 审计湖仓
```

分层目标：

- 规则层：< 10 ms，覆盖大多数；
- 模型层：< 100 ms，异步/批处理；
- 检索层：< 50 ms，缓存热点；
- Agent 层：秒级，异步/人工；
- 审计层：分钟级，不阻塞决策。

### 容量估算模板

```text
事件速率 E（events/s）
调用比例 r
模型延迟 L（s）
并发 ≈ E × r × L
token 速率 ≈ E × r × tokens_per_call
每小时成本 ≈ token 速率 × 3600 / 1e6 × 单价
向量检索 QPS ≈ E × r_search
索引大小 ≈ chunk 数 × 维度 × 4 bytes × 放大系数
```

示例（仅示意）：

```text
E = 50,000 events/s
r = 1%
L = 0.8 s
并发 ≈ 400
tokens/call = 600
token 速率 = 300,000/s ≈ 1.08B/h
```

这已经是一条重成本链路。必须用规则/缓存/小模型把 `r` 降下来，或者把 Agent 放到异步复核。

## 35.9 MiniFlink 的映射与示例

MiniFlink 不实现 `ML_PREDICT`/`VECTOR_SEARCH`/Flink Agents，但它可以演示“流式特征 + 模型打分 + 检索”的形状：

- `examples/20_realtime_ai_pipeline.py` 用窗口聚合算特征，用一个本地“模型”函数打分，再用一个内存向量表做相似度检索；
- 它不调用真实模型，也不保证 exactly-once 的外部副作用；
- 它展示的是**算子拓扑、状态、时间语义和结果审计**，不是 AI 框架。

对照表：

| 生产概念 | MiniFlink 近似 | 差距 |
|---|---|---|
| 窗口特征 | `window().aggregate()` | 无 SQL 特征存储 |
| `ML_PREDICT` | `map()` 调本地函数 | 无模型 DDL/异步/限流 |
| `VECTOR_SEARCH` | `async_map()` 查内存表 | 无索引/召回语义 |
| Agent | `ProcessFunction` 状态机 | 无工具/记忆/durable call |
| 审计 | sink 收集 | 无湖仓/血缘 |

## 本章小结

实时 AI 的难点不在“能不能调用模型”，而在**把不确定性关进工程边界**：

- 特征要新鲜、时点正确、可回填；
- 推理要异步、限流、超时、降级、可审计；
- 检索要明确索引版本和一致性语义；
- Agent 要有状态、记忆、工具权限和外部动作的幂等；
- 成本要按 token/QPS/并发算，而不是只看集群账单。

Flink 的位置是“事件与状态的编排层”，不是模型服务或向量数据库。把边界划清楚，实时 AI 才能既快又可控。

SQL 语法/函数速查见附录 G；源码阅读路线见附录 H。

下一章换一个同样容易被忽视、但决定系统能否上生产的话题：正确性工程——对账、回放、数据质量与事故复盘。

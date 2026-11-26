# 第38章 综合实战：实时风控平台从 0 到 1

## 38.1 业务需求与 SLO

假设我们要为一家支付公司搭建实时风控平台。业务目标：

- 每笔支付在 200 ms 内完成规则判断；
- 识别“短时间多次失败登录后大额支付”“异地登录后立即转账”等序列；
- 风险名单/规则可热更新，不重启作业；
- 误拦截率低于 0.5%，漏拦截率低于 0.1%；
- 7×24 运行，故障 10 分钟内恢复；
- 所有决策可追溯、可审计。

先把目标翻译成工程 SLO：

```text
端到端 P99 延迟 < 200 ms（规则判断）
数据新鲜度 < 5 s（特征/名单）
可用性 >= 99.95%
RPO <= 1 min，RTO <= 10 min
规则热更新生效 <= 30 s
决策日志保留 >= 180 天
```

注意“200 ms”是**规则判断**的延迟，不是整个链路的对账延迟。不同指标要分开定义，否则无法验收。

## 38.2 架构总览

```text
业务系统 ──支付事件──> Kafka(payments)
登录/设备 ──行为事件──> Kafka(behaviors)
名单/规则 ──配置流────> Kafka(rules)
                              │
                              ▼
                    Flink 风控作业
        ┌─────────────┼─────────────┐
        │             │             │
   事件标准化     序列/CEP      维表/特征
        │             │             │
        └─────────────┼─────────────┘
                      ▼
              决策结果 Kafka(decisions)
                 │              │
                 ▼              ▼
            风控引擎/拦截    湖仓(审计/回放)
```

组件职责：

- **Kafka**：事件总线，保留 7 天，支持回放；
- **Flink**：标准化、序列匹配、特征聚合、规则判断；
- **Redis**：在线特征/名单点查（可选，低延迟）；
- **Paimon/Iceberg**：决策日志、特征快照、审计；
- **规则服务**：规则 CRUD、校验、灰度，写 Kafka；
- **监控**：Prometheus/Grafana/告警。

### 为什么拆成多个作业

不要把所有逻辑塞进一个作业：

- 事件标准化：无状态/低状态，独立扩缩容；
- 序列检测：有状态、CEP，状态较大；
- 特征聚合：窗口/状态，独立调优；
- 决策输出：sink 密集，独立限流。

```text
标准化作业 → 序列作业 → 决策作业
     │            │
     └─ 特征作业 ─┘
```

拆分边界按“状态/扩缩容/故障域”划，而不是按团队。

## 38.3 数据模型与事件契约

支付事件：

```json
{
  "event_id": "pay-123",
  "event_time": "2026-11-26T10:00:00.123Z",
  "user_id": "u-42",
  "account_id": "a-7",
  "amount": 12800.00,
  "currency": "CNY",
  "channel": "card",
  "device_id": "d-9",
  "ip": "203.0.113.5",
  "merchant_id": "m-1",
  "status": "created"
}
```

行为事件：

```json
{
  "event_id": "beh-456",
  "event_time": "2026-11-26T09:58:00.000Z",
  "user_id": "u-42",
  "type": "login_failed",
  "ip": "203.0.113.5",
  "device_id": "d-9"
}
```

契约要求：

- 必有 `event_id`（幂等/去重）；
- 必有 `event_time`（事件时间）；
- `user_id` 非空（key）；
- 金额用 Decimal，不用 float；
- schema 版本字段，支持演进；
- 时间统一 UTC。

```sql
CREATE TABLE payments (
  event_id STRING,
  event_time TIMESTAMP(3),
  user_id STRING,
  account_id STRING,
  amount DECIMAL(18, 2),
  currency STRING,
  channel STRING,
  device_id STRING,
  ip STRING,
  merchant_id STRING,
  proc_time AS PROCTIME(),
  WATERMARK FOR event_time AS event_time - INTERVAL '3' SECOND
) WITH (
  'connector' = 'kafka',
  'topic' = 'payments',
  'properties.bootstrap.servers' = 'kafka:9092',
  'properties.group.id' = 'risk-payments',
  'scan.startup.mode' = 'group-offsets',
  'format' = 'json'
);
```

## 38.4 作业一：标准化与去重

目标：

- 解析/校验/脱敏；
- 按 `event_id` 去重（防上游重复）；
- 统一时间/金额/币种；
- 输出标准化事件。

```sql
-- payments_clean 的 DDL 里需要有 proc_time AS PROCTIME()
-- （或者决策作业在查询里重新生成处理时间列）
INSERT INTO payments_clean
SELECT
  event_id, event_time, user_id, account_id,
  CAST(amount AS DECIMAL(18, 2)) AS amount,
  UPPER(currency) AS currency,
  channel, device_id, ip, merchant_id,
  PROCTIME() AS proc_time
FROM (
  SELECT *,
    ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY event_time) AS rn
  FROM payments
)
WHERE rn = 1;
```

去重的状态：

- 每个 `event_id` 保留一条；
- 设置 `table.exec.state.ttl`（例如 24 小时）；
- 高基数时状态大，需监控。

```yaml
table.exec.state.ttl: 24h
```

更严格的去重可以用 Redis/外部存储，但会增加延迟。

## 38.5 作业二：序列检测（CEP）

规则示例：

1. 5 分钟内 3 次登录失败；
2. 登录失败后 10 分钟内大额支付（> 1 万元）；
3. 异地登录后 30 分钟内转账。

用 CEP 表达：

```java
Pattern<Event, ?> loginFailures = Pattern.<Event>begin("f1")
    .where(e -> e.type().equals("login_failed"))
    .next("f2").where(e -> e.type().equals("login_failed"))
    .next("f3").where(e -> e.type().equals("login_failed"))
    .within(Duration.ofMinutes(5));

Pattern<Event, ?> riskyPayment = Pattern.<Event>begin("fail")
    .where(e -> e.type().equals("login_failed"))
    .followedBy("pay")
    .where(e -> e.type().equals("payment") && e.amount() > 10_000)
    .within(Duration.ofMinutes(10));
```

用 SQL 也可以表达部分逻辑，但序列匹配更适合 CEP/PTF：

```sql
-- 5 分钟内 3 次失败：窗口计数
SELECT user_id, COUNT(*) AS failures
FROM TABLE(TUMBLE(TABLE behaviors, DESCRIPTOR(event_time), INTERVAL '5' MINUTES))
WHERE type = 'login_failed'
GROUP BY user_id, window_start, window_end
HAVING COUNT(*) >= 3;
```

选择：

- 固定窗口计数 → SQL；
- 有序序列/时间间隔 → CEP；
- 复杂状态/外部调用 → ProcessFunction/PTF。

## 38.6 作业三：特征聚合

特征示例：

- 最近 1 分钟支付笔数/金额；
- 最近 1 小时失败登录数；
- 最近 24 小时交易对手数；
- 设备/IP 关联用户数。

```sql
SELECT
  user_id,
  window_start,
  window_end,
  COUNT(*) AS tx_count_1m,
  SUM(amount) AS tx_amount_1m
FROM TABLE(TUMBLE(TABLE payments_clean, DESCRIPTOR(event_time), INTERVAL '1' MINUTE))
GROUP BY user_id, window_start, window_end;
```

状态设计：

- 窗口状态按 key + window；
- TTL 覆盖最大窗口 + 允许迟到；
- 高基数 key 要防倾斜；
- 特征结果写 Redis/Paimon，供决策作业查询。

```yaml
table.exec.state.ttl: 25h
```

## 38.7 作业四：决策与输出

决策作业：

1. 消费支付事件；
2. lookup join 名单/特征（Async I/O）；
3. 应用规则（广播状态）；
4. 输出决策（approve/review/reject）；
5. 写 Kafka + 湖仓。

```sql
SELECT
  p.event_id,
  p.user_id,
  p.amount,
  r.risk_level,
  f.tx_count_1m,
  CASE
    WHEN r.risk_level = 'high' THEN 'reject'
    WHEN f.tx_count_1m > 10 THEN 'review'
    ELSE 'approve'
  END AS decision
FROM payments_clean p
LEFT JOIN risk_list FOR SYSTEM_TIME AS OF p.proc_time AS r
  ON p.user_id = r.user_id
LEFT JOIN features FOR SYSTEM_TIME AS OF p.proc_time AS f
  ON p.user_id = f.user_id;
```

注意：这里用 `p.proc_time` 做的是**处理时间 lookup join**，不是版本化 temporal join。`risk_list`/`features` 必须有主键，且 connector 支持 lookup（JDBC/HBase/Redis/Paimon 等），普通 append-only 表不行；`payments_clean` 也必须带 `proc_time`。如果要按事件发生时刻取维表的历史版本，应改成 `FOR SYSTEM_TIME AS OF p.event_time`，并要求维表是版本表（主键 + 事件时间 + changelog）。两者在计划期和运行期的要求不同，不能混用。

延迟预算：

```text
Kafka 消费 5 ms
反序列化/标准化 5 ms
lookup join（缓存命中）1 ms / 未命中 20 ms
规则匹配 1 ms
Kafka 写出 5 ms
--------------------------------
P99 约 50~100 ms（缓存命中）
```

如果 lookup 未命中率高，P99 会被外部查询拖到几百毫秒。必须做缓存/异步/降级。

## 38.8 状态设计

| 状态 | 大小驱动 | 保留 | 优化 |
|---|---|---|---|
| 去重 event_id | 事件量 × TTL | 24h | TTL、布隆过滤器 |
| CEP 部分匹配 | 用户数 × 模式数 | within | 缩短 within、next 优先 |
| 窗口特征 | key 数 × 窗口数 | 25h | 滚动窗口、预聚合 |
| 广播规则 | 规则数 | 常驻 | 控制规则大小 |
| lookup 缓存 | 热点 key | 分钟级 | LRU/TTL |

估算（本章算例与第 26、40 章的规模/单价相互独立，不能直接相加）：

```text
1000 万用户 × 每用户 2 KB × 1.5 放大 ≈ 30 GB
加上 CEP/窗口/去重，可能 100~300 GB
```

状态是容量规划的核心。先算状态，再算 CPU。

## 38.9 Exactly-once 链路

```text
KafkaSource(offset in checkpoint)
  → Flink 状态（去重/CEP/窗口）
  → KafkaSink(2PC, read_committed)
  → 湖仓 sink(2PC)
```

要求：

- checkpoint 间隔 30～60 s；
- Kafka 事务超时 > checkpoint 间隔 + 超时；
- 下游 `isolation.level=read_committed`；
- 湖仓 sink 支持事务提交；
- 决策日志用 `event_id` 幂等。

```yaml
execution.checkpointing.interval: 30s
execution.checkpointing.timeout: 10min
execution.checkpointing.min-pause: 10s
execution.checkpointing.mode: EXACTLY_ONCE
```

RPO 30～60 s，RTO 取决于状态大小和恢复方式。大状态用 RocksDB + 增量 checkpoint + 本地恢复。

## 38.10 容量与成本

假设：

- 峰值 5 万支付/秒，行为事件 20 万/秒；
- 状态 300 GB；
- 并行度 32；
- 每 TM 16 vCPU/64 GB，4 slot；
- 需要 8 台 TM + 2 台 JM（HA）。

```text
计算：10 台 × 16 vCPU × 730h × $0.10/vCPU·h ≈ $11,680/月（示意单价）
存储：300 GB 状态 + checkpoint 增量 × 保留 7 天
网络：Kafka 跨 AZ + checkpoint 上传
人力：2 个平台/作业工程师
```

这里的 `$0.10/vCPU·h` 只是算例基线，与第 26、40 章的单价和规模不同，不能跨章相加；做真实预算时要统一地区、实例类型、存储请求和人力成本。

成本优化顺序：

1. 状态 TTL/窗口设计（最大收益）；
2. 增量 checkpoint；
3. 预聚合/广播减少 shuffle；
4. 并行度/机型；
5. 冷数据归档。

## 38.11 部署与发布

- Kubernetes + Flink Operator；
- application 模式，每作业独立；
- checkpoint/savepoint 放对象存储；
- GitOps 管理 CRD/镜像/配置；
- 升级用 savepoint；
- 灰度：先 1% 流量，再 10%，再全量；
- 回滚：旧镜像 + 旧 savepoint。

```yaml
job:
  upgradeMode: savepoint
  state: running
```

发布检查：

- [ ] 作业图 diff（uid/并行度/状态）；
- [ ] savepoint 恢复演练；
- [ ] 规则/名单版本；
- [ ] 下游兼容；
- [ ] 回滚脚本。

## 38.12 测试与演练

上线前：

1. 回放 7 天历史数据，对比离线结果；
2. 注入重复/迟到/乱序/脏数据；
3. kill TaskManager，验证恢复；
4. Kafka 限流/不可用，验证降级；
5. 规则热更新，验证 30 s 内生效；
6. 压测 2 倍峰值；
7. 对账：决策数/金额/拦截率。

```text
对账指标：
- 支付事件数 vs 决策数
- 拦截金额 vs 人工复核
- 重复 event_id 比例
- P99 延迟/规则命中率
```

## 38.13 三个典型事故案例

### 案例一：窗口不触发

现象：5 分钟特征一直不更新。

根因：Kafka 一个分区长时间无数据，watermark 取最小值被卡住。

修复：开启 idleness，或调整分区/水位策略。

### 案例二：恢复后重复扣款

现象：故障恢复后部分支付被重复拦截/记账。

根因：sink 是 at-least-once，且下游没有幂等键。

修复：Kafka 2PC + 下游 `event_id` 去重；或改幂等 upsert。

### 案例三：状态爆炸

现象：checkpoint 从 2 分钟涨到 20 分钟，最后超时。

根因：CEP 的 `followedBy` + 长 `within` 产生大量部分匹配。

修复：改 `next`、缩短 `within`、限制每 key 并发匹配、加 TTL。

## 38.14 演进路线

```text
阶段 1：单作业、规则硬编码、at-least-once
阶段 2：拆作业、广播规则、Kafka EOS
阶段 3：CEP/特征、湖仓审计、SLO
阶段 4：多租户平台、自动扩缩容、AI 辅助规则
```

每个阶段都要解决“状态、恢复、成本、可观测性”，而不是只加功能。

## 本章小结

实时风控平台是全书技术的综合演练：事件契约、时间/watermark、CEP、状态、维表、EOS、容量、部署、演练。真正的难点不是某一个 API，而是**在延迟、正确性、成本和可运维性之间做出一致的选择**。

下一章从技术回到商业：流处理的市场、价值链和商业模式。

# 第36章 正确性工程：对账、回放、数据质量与事故复盘

前置知识：第 4、8、9、11 章（watermark、checkpoint、恢复、Kafka EOS）。本章的方法独立于具体引擎，但对账/SLO 的例子会引用这些章节。

“测试全绿”“checkpoint 成功”“Kafka 开了 exactly-once”，都不等于业务结果正确。

流系统的正确性至少有三层：

1. **引擎正确性**：算子语义、watermark、状态、checkpoint 是否符合预期；
2. **数据正确性**：输入是否完整、唯一、及时、合法；
3. **业务正确性**：算出来的指标/决策是否真的对应业务事实。

前两层可以靠测试、对账和回放验证；第三层必须和业务方一起定义“什么算对”。这一章给出一套可落地的方法：**先定义不变量，再设计对账，最后用回放/演练证明**。

前置阅读：第 4 章（时间/watermark）、第 8 章（checkpoint）、第 9 章（恢复）、第 11 章（Kafka EOS）、第 35 章（实时 AI 的审计）。

## 36.1 先定义“正确”的不变量

没有不变量的“正确性”无法验收。流作业常见的不变量：

| 不变量 | 含义 | 违反后果 |
|---|---|---|
| 不丢 | 每条源事件至少产生一次结果 | 少算/漏拦截 |
| 不重 | 同一业务事件只产生一次有效结果 | 多算/重复扣款 |
| 一致 | 同一事件在 source/sink 的标识一致 | 对账失败 |
| 有序 | 同一 key 内按事件时间/版本处理 | 状态错乱 |
| 及时 | 结果在 SLO 内可见 | 业务错过窗口 |
| 可解释 | 每个结果能追溯到输入/版本 | 无法审计/复盘 |

把这六条写成**可执行的断言**，而不是口号：

```text
count(sink_unique_event_id) == count(source_unique_event_id)
sum(sink_amount) == sum(source_amount) - sum(rejected_amount)
max(sink_event_time) >= max(source_event_time) - allowed_lateness
for every decision: decision_id exists in audit table
```

### 不变量要分层

- **事件级**：每个 `event_id` 是否有对应输出；
- **窗口级**：每个时间窗口的计数/金额是否一致；
- **作业级**：全天/全月累计是否一致；
- **业务级**：拦截率、通过率、坏账率是否在合理区间。

只做作业级对账，可能掩盖局部错误；只做事件级对账，成本又太高。通常是“事件级抽样 + 窗口级全量 + 作业级趋势”。

## 36.2 数据质量：在计算之前先保证输入可用

### 六个维度

| 维度 | 问题 | 流式检查 |
|---|---|---|
| 完整性 | 字段/事件是否缺失 | 非空、必填、source lag |
| 唯一性 | 是否重复 | `event_id` 去重、幂等键 |
| 及时性 | 是否迟到 | watermark lag、event_time 与 ingestion_time 差 |
| 有效性 | 格式/范围是否合法 | 正则、枚举、金额 > 0 |
| 一致性 | 跨表/跨流是否一致 | 维表 join、主外键、币种/单位 |
| 准确性 | 是否对应真实世界 | 抽样人工核对、离线对拍 |

### 流式质量检查的三种输出

```text
输入流
  ├── 合格记录 → 主流程
  ├── 可修复记录 → 修复流（补默认值/标准化）
  └── 不可修复记录 → 死信队列 + 告警
```

不要在主流程里 `try/catch` 后静默丢弃。丢弃必须有计数、有原因、有样例、有告警。

### SQL 示例：基础校验

```sql
CREATE VIEW checked AS
SELECT
  *,
  CASE
    WHEN event_id IS NULL THEN 'missing_event_id'
    WHEN user_id IS NULL THEN 'missing_user_id'
    WHEN amount IS NULL OR amount < 0 THEN 'invalid_amount'
    WHEN event_time IS NULL THEN 'missing_event_time'
    ELSE 'ok'
  END AS quality
FROM raw_events;

INSERT INTO good_events
SELECT * FROM checked WHERE quality = 'ok';

INSERT INTO dead_letter
SELECT * FROM checked WHERE quality <> 'ok';
```

### 重复与唯一性

上游重试、Kafka 至少一次、CDC 回放都会产生重复。去重策略：

1. **业务主键**：`event_id`/`order_id`，最可靠；
2. **状态去重**：按 key 保留最近 N 条/时间窗口；
3. **幂等 sink**：主键 upsert，重复写入结果一致；
4. **布隆过滤器**：节省状态，但有假阳性，只适合“允许极少量误判”的场景；
5. **外部去重**：Redis/数据库唯一键，增加延迟但跨作业有效。

去重不是“加一个 `DISTINCT`”那么简单。要明确：

- 去重键是什么？
- 保留多久？
- 迟到重复是否也算重复？
- 去重状态多大？
- 故障恢复后状态是否还在？

## 36.3 对账：用数据证明“没丢没重”

对账不是财务部门的专属。任何流作业都可以做三层对账。

### 第一层：计数对账

按时间窗口比较 source 和 sink 的记录数：

```sql
WITH src AS (
  SELECT
    window_start,
    COUNT(*) AS cnt,
    SUM(amount) AS amount
  FROM TABLE(TUMBLE(TABLE source_events, DESCRIPTOR(event_time), INTERVAL '1' MINUTE))
  GROUP BY window_start, window_end
),
snk AS (
  SELECT
    window_start,
    COUNT(*) AS cnt,
    SUM(amount) AS amount
  FROM TABLE(TUMBLE(TABLE sink_events, DESCRIPTOR(event_time), INTERVAL '1' MINUTE))
  GROUP BY window_start, window_end
)
SELECT
  COALESCE(src.window_start, snk.window_start) AS window_start,
  src.cnt AS src_cnt,
  snk.cnt AS snk_cnt,
  src.amount AS src_amount,
  snk.amount AS snk_amount
FROM src FULL OUTER JOIN snk
  ON src.window_start = snk.window_start
WHERE src.cnt IS DISTINCT FROM snk.cnt
   OR src.amount IS DISTINCT FROM snk.amount;
```

注意：

- 两边必须用同一时间语义和同一窗口；
- sink 可能延迟可见，对账要等 watermark/新鲜度满足后再跑；
- 过滤/聚合会改变计数，不能直接比“源条数 = 结果条数”；
- 对账本身要幂等，重复跑不能产生副作用。

### 第二层：事件级对账

按业务主键比较“源有、sink 无”“sink 有、源无”“重复”：

```sql
WITH ids AS (
  SELECT event_id, 'src' AS side FROM source_events
  UNION ALL
  SELECT event_id, 'snk' AS side FROM sink_events
)
SELECT event_id,
       SUM(CASE WHEN side = 'src' THEN 1 ELSE 0 END) AS src_cnt,
       SUM(CASE WHEN side = 'snk' THEN 1 ELSE 0 END) AS snk_cnt
FROM ids
GROUP BY event_id
HAVING src_cnt <> 1 OR snk_cnt <> 1;
```

结果分类：

| src_cnt | snk_cnt | 含义 | 处理 |
|---|---|---|---|
| 1 | 1 | 正常 | 通过 |
| 1 | 0 | 丢失 | 补算/重放 |
| 1 | >1 | 重复 | 幂等/去重 |
| 0 | 1 | 无源输出 | 脏数据/回填 |
| >1 | * | 源重复 | 上游去重 |

事件级对账的成本高，通常按天/按小时抽样，或只对高风险事件全量跑。

### 第三层：业务对账

- 金额：`sum(decision_amount)` 与账务系统/人工复核一致；
- 拦截率：与历史基线/AB 实验一致；
- 特征：离线特征与线上特征分布一致；
- 模型：离线评测与线上效果一致。

业务对账需要业务方定义口径。技术团队不能自己发明“正确”。

### 对账的工程化

```text
T+0：流式对账（分钟级窗口）→ 实时告警
T+1：批式对账（全量/分区）→ 日报/工单
T+7：抽样人工核对 → 质量评估
```

对账结果要落表，带：

- 对账批次/时间范围；
- 源/目标表版本；
- 差异条数/金额；
- 样例 event_id；
- 处理状态/负责人。

## 36.4 回放、影子链路与差异分析

### 确定性回放

回放是验证“同一输入是否产生同一输出”的最直接方法。前提：

- 输入可重放（Kafka 保留、湖仓快照、对象存储）；
- 作业逻辑确定（不依赖随机数/当前时间/外部可变状态）；
- 状态从同一起点开始（空状态或指定 savepoint）；
- 输出可比较（事件级/聚合级）。

```text
历史事件 → 回放作业（固定并行度/固定版本）
         → 输出 A
原作业输出 → 输出 B
比较 A/B → 差异 → 归因
```

常见差异来源：

- 处理时间/当前时间；
- 非确定性 UDF（随机、UUID、模型）；
- 外部维表变化；
- 并行度/分区变化导致顺序变化；
- 迟到数据/watermark 不同；
- 状态 TTL/清理时机不同。

### 影子链路

生产环境同时跑新旧两套逻辑：

```text
             ┌── 旧作业 → 旧输出（生效）
源流 ──复制──┤
             └── 新作业 → 新输出（影子，不生效）
```

比较：

- 事件级：同一 `event_id` 的决策是否一致；
- 聚合级：窗口计数/金额差异；
- 分布级：拦截率/延迟/模型分数分布；
- 成本级：资源/token/存储。

影子链路的关键是**不污染生产副作用**：新作业的 sink 要写影子表/影子 topic，不能直接扣款/发消息。

### 差异分析框架

| 差异类型 | 可能原因 | 排查 |
|---|---|---|
| 少量随机差异 | 非确定性/时间 | 固定时间/随机种子 |
| 系统性偏移 | 逻辑/口径变化 | 对比 SQL/算子 |
| 边界差异 | 窗口/watermark/late | 检查时间语义 |
| 状态差异 | TTL/恢复/扩缩容 | 对比状态大小/key |
| 外部差异 | 维表/模型/缓存 | 固定版本/快照 |

不要用“差异率很低”掩盖系统性问题。先分类，再定量。

## 36.5 幂等：端到端正确性的最后一道门

Flink 的 checkpoint 保证作业内部状态的一致切面；端到端正确性还取决于 sink。

### 三种 sink 语义

| 语义 | 机制 | 适用 |
|---|---|---|
| at-most-once | 不重试/可能丢 | 可丢失的日志 |
| at-least-once | 重试/可能重 | 可幂等下游 |
| exactly-once | 2PC/幂等 + 状态 | 金融/账务 |

### 幂等键设计

- 用业务主键（`event_id`/`order_id`），不要用自增 id；
- 幂等键要进入 sink 的唯一约束/主键；
- 同一业务事件的重试必须产生相同键；
- 键的生成要确定，不能包含随机数/时间戳；
- 跨系统要统一键（source/sink/审计）。

### 事务 sink 的边界

Kafka 2PC：

- source offset 进 checkpoint；
- sink 事务在 checkpoint 完成时提交；
- 下游 `read_committed`；
- 事务超时 > checkpoint 间隔 + 超时。

湖仓 sink：

- 支持事务提交/主键 upsert；
- 小文件/compaction 不影响可见性；
- 提交延迟与 checkpoint 对齐。

数据库 sink：

- 用 upsert/唯一键；
- 避免“先查后写”的竞态；
- 批量提交要有幂等重试。

### 不要在外设副作用里“绕过” checkpoint

```java
// 反例：map 里直接扣款/发消息
stream.map(order -> {
    paymentService.charge(order);   // 不可回滚
    return order;
});
```

故障恢复后这段代码会重放，导致重复扣款。正确做法：

- 把副作用放到支持事务/幂等的 sink；
- 或者写“决策事件”，由下游服务做幂等执行；
- 或者用 outbox/事务表。

## 36.6 故障与脏数据注入

测试正常路径只能证明“顺利时正确”。生产事故来自异常路径。建议的注入矩阵：

| 注入 | 观察 | 期望 |
|---|---|---|
| 重复事件 | 输出去重/幂等 | 不重复 |
| 迟到事件 | watermark/late side output | 按策略处理 |
| 乱序事件 | 窗口/join | 结果正确 |
| 空字段/非法格式 | 死信/告警 | 不污染主流程 |
| source 暂停 | watermark/延迟 | 可恢复 |
| sink 超时 | 重试/降级 | 不丢不重 |
| TM kill | checkpoint 恢复 | 状态一致 |
| checkpoint 失败 | 告警/重试 | 不无限重启 |
| 状态膨胀 | 容量/恢复 | 可清理/扩缩容 |
| 模型 429/超时 | 降级/死信 | 不阻塞主流程 |

### 属性测试与随机化

对窗口/join/CEP 这类边界多的算子，写属性测试：

- 随机生成事件时间/乱序/重复；
- 与一个简单的参考实现对比；
- 检查不变量（不丢、不重、窗口和）；
- 固定随机种子，保证可复现。

MiniFlink 的测试就是一个可读的参考实现；但它本身也可能有 bug（见附录 C 的“测试没骗你”）。

### 故障注入

- kill TaskManager/JobManager；
- 断网/限流/对象存储 5xx；
- Kafka broker 重启/分区 leader 切换；
- 磁盘满/句柄耗尽；
- 模型服务 429/500/超时；
- 规则/维表更新失败。

故障注入要在预发/影子环境做，并记录恢复时间、数据差异、告警是否触发。

## 36.7 状态正确性：checkpoint 之外的检查

checkpoint 成功不代表状态正确。要检查：

- 状态大小是否与输入/基数匹配；
- 是否有 key 泄漏（TTL 没生效/清理逻辑缺失）；
- 定时器是否残留；
- 恢复后 watermark 是否跳变；
- 扩缩容后 key 是否重新分配；
- savepoint 能否恢复；
- 状态 schema 是否兼容。

### 状态审计

```text
状态大小 = 活跃 key 数 × 每 key 状态 × 放大系数
```

如果实际大小远超估算，检查：

- key 基数是否爆炸（用户 id/设备 id/订单 id）；
- 是否有无界窗口/无 TTL 的聚合；
- 是否有 CEP 部分匹配堆积；
- 是否有 join 缓存未清理；
- 是否有广播状态过大。

### State Processor API

用于：

- 离线读取 savepoint，统计/修复状态；
- 迁移状态到新作业；
- 删除脏 key；
- 验证 schema。

它适合“停机/双跑”的运维场景，不适合在线高频操作。

### 扩缩容后的验证

1. 用 savepoint 恢复；
2. 检查 key group 分配；
3. 对比关键 key 的状态；
4. 观察输出是否重复/丢失；
5. 监控状态大小和 checkpoint 时长。

## 36.8 SLO、错误预算与告警

正确性也要有 SLO。推荐四个指标：

| 指标 | 定义 | 示例目标 |
|---|---|---|
| 完整性 | 1 - 缺失事件/总事件 | ≥ 99.99% |
| 唯一性 | 1 - 重复输出/总输出 | ≥ 99.999% |
| 新鲜度 | P99 结果可见延迟 | ≤ 5 s |
| 一致性 | 1 - 对账差异/对账总量 | ≥ 99.99% |

错误预算：

```text
月度错误预算 = (1 - SLO) × 总事件数
```

例如 99.99% 完整性，月 10 亿事件，预算 10 万条。预算燃烧过快时：

- 降级非核心功能；
- 暂停发布；
- 加对账频率；
- 回滚。

告警设计：

- 对账差异 > 阈值；
- 死信率突增；
- watermark lag 超 SLO；
- checkpoint 连续失败；
- 状态大小异常增长；
- 模型/检索错误率/成本异常。

告警要带**可执行信息**：哪个作业、哪个窗口、差异样例、最近变更、runbook 链接。

## 36.9 事故复盘模板

```text
1. 摘要：发生了什么，影响多少业务/用户/金额
2. 时间线：发现、响应、恢复、收尾（UTC/本地时区）
3. 影响：数据/决策/SLO/合规
4. 检测：谁/什么告警发现，是否及时
5. 根因：技术根因 + 促成因素
6. 恢复：临时措施、回滚、补数
7. 证据：指标、日志、对账、变更记录
8. 行动项：负责人/截止时间/验收标准
9. 复盘：哪些做得好，哪些要改
```

区分：

- **触发因素**：直接导致故障的事件（如某次发布）；
- **根因**：让触发因素造成严重后果的系统性问题（如缺少对账/回滚）；
- **促成因素**：监控缺失、容量不足、文档过期。

好的复盘不是“某人操作失误”，而是“为什么这个操作没有防护”。

## 36.10 MiniFlink 的映射与示例

MiniFlink 的 `examples/19_reconciliation_pipeline.py` 演示：

- 输入含重复 `event_id`；
- 按 `event_id` 键控去重；
- 在微批边界做 checkpoint；
- 恢复后继续处理；
- 统计 source/unique/output 计数，验证不丢不重。

它不能证明生产正确性，但能把“对账/恢复/去重”变成可运行的对象。

| 生产概念 | MiniFlink 近似 | 差距 |
|---|---|---|
| 事件级对账 | 计数/集合比较 | 无外部账务 |
| checkpoint 恢复 | 微批快照 | 无 barrier/HA |
| 幂等 sink | 去重后输出 | 无事务 sink |
| 死信 | 侧输出 | 无质量规则引擎 |
| 影子链路 | 双 execution | 无流量复制 |

练习：

1. 给示例加一个“迟到事件”侧输出，统计迟到率；
2. 加一个“金额为负”的死信规则；
3. 故意在 checkpoint 后注入重复，观察去重状态是否生效；
4. 把去重状态改成 TTL，观察状态大小与正确性的权衡。

## 本章小结

正确性不是某个开关，而是一组**可验证的不变量 + 对账 + 回放 + 幂等 + 复盘**：

- 先定义“什么算对”，再设计指标和告警；
- 数据质量要在主流程之前分流，不要静默丢弃；
- 对账分事件级/窗口级/业务级，成本与覆盖要平衡；
- 回放和影子链路是验证逻辑变更的最可靠手段；
- 端到端正确性最终落在 sink 的幂等/事务上；
- 事故复盘要找到系统性根因，而不是停在“操作失误”。

对账/窗口 SQL 的语法速查见附录 G；MiniFlink 的边界见附录 C。

下一章把视角拉回生态：Flink 在流处理版图中的位置、如何选型，以及未来几年的技术判断。

# 第22章 监控指标与可观测性

## 22.1 三类信号

- **Metrics**：数值指标，适合告警和趋势；
- **Logs**：事件详情，适合排查；
- **Traces**：请求链路，适合定位跨系统延迟。

Flink 自带 Metrics、Web UI、REST API，也可以接 Prometheus/Grafana、OpenTelemetry、ELK 等。

```text
业务 SLO
   ├─ Metrics：延迟、吞吐、滞后、错误率
   ├─ Logs：异常、脏数据、状态变化
   └─ Traces：跨 Kafka/DB/服务的调用链
```

## 22.2 核心指标

作业级：

- `numRecordsIn/Out`：吞吐；
- `numRecordsInPerSecond/OutPerSecond`；
- `numBytesIn/Out`；
- `busyTimeMsPerSecond`：算子忙碌程度；
- `backPressuredTimeMsPerSecond`：反压；
- `idleTimeMsPerSecond`：空闲；
- `lastCheckpointDuration`；
- `lastCheckpointSize`；
- `lastCheckpointAlignmentTime`；
- `numberOfFailedCheckpoints`；
- `numRestarts`；
- `uptime`。

Source：

- Kafka lag（各分区）；
- 消费速率；
- offset 提交失败；
- watermark；
- 分区发现延迟。

Sink：

- 写入延迟/失败；
- 事务提交失败；
- 批量大小；
- 外部系统限流。

系统：

- JVM heap、GC、metaspace；
- CPU、内存、磁盘、网络；
- RocksDB block cache、compaction；
- 容器 OOMKill。

### 指标的分层

```text
集群层：TM 数、slot 使用率、JM 存活
作业层：吞吐、延迟、checkpoint、重启
算子层：busy、反压、状态大小、records
subtask 层：倾斜、异常、GC
外部层：Kafka lag、DB 延迟、S3 错误率
```

排障时从作业层往下钻，不要一上来就看 JVM 指标。

### 延迟指标：内置追踪能说明什么

Flink 内置的延迟追踪基于 **latency marker**：source 按固定间隔注入一个带时间戳的标记，算子收到标记时记录“从 source 发出到现在”的时间差，聚合成直方图指标 `latency`。

```yaml
metrics.latency.interval: 60s            # Duration；默认 0，表示关闭
metrics.latency.granularity: operator    # single / operator / subtask
metrics.latency.history-size: 128        # 每个算子保留的采样数
```

三种粒度：

- `single`：不区分 source 和 subtask，只保留一个全局直方图；
- `operator`（默认）：按 source 区分，但不区分 subtask；
- `subtask`：按 source + subtask 区分，直方图数量随并行度平方增长，开销最大。

必须知道的四个边界：

1. **marker 不能超过普通记录**。如果记录在算子前排队，排队时间会算进延迟；因此反压时这个指标反而更接近真实感受；
2. **marker 绕过用户函数**。算子内部处理时间、窗口缓冲、状态访问、异步等待都不计入，除非它们导致输入队列积压；
3. **它测的是“source → 当前算子”**，不是“业务事件产生 → 下游可见”的端到端延迟；
4. **它假设各节点时钟同步**。NTP 漂移会直接变成延迟误差；`subtask` 粒度在大并行度下开销明显，通常只在排障时短时开启。

所以：

- 想知道“哪个算子/通道在排队”，用内置 `latency` + `backPressuredTimeMsPerSecond`；
- 想知道“用户从下单到看到结果”的端到端延迟，必须在 source 记录事件时间、在 sink 记录写出时间，或用 trace/业务埋点，不能只看 Flink 的 `latency`；
- 端到端延迟的预算拆分方法见[第 26 章 26.12 节](26-capacity-cost.md#2612-端到端-p99-延迟预算)。

## 22.3 Prometheus 集成

```yaml
metrics.reporter.prom.factory.class: org.apache.flink.metrics.prometheus.PrometheusReporterFactory
metrics.reporter.prom.port: 9249
```

或在 Kubernetes Operator 中暴露：

```yaml
spec:
  flinkConfiguration:
    metrics.reporter.prom.factory.class: org.apache.flink.metrics.prometheus.PrometheusReporterFactory
    metrics.reporter.prom.port: "9249"
```

Prometheus 抓取后，Grafana 看板通常包含：

- 吞吐/延迟；
- checkpoint 成功率和时长；
- 反压热力图；
- 各 subtask 的倾斜；
- Kafka lag；
- JVM/GC。

### 标签与基数

Flink 的指标标签包含 `job_name`、`task_name`、`subtask_index` 等。高基数会拖垮 Prometheus：

- 不要给每个记录打标签；
- 控制 `task_name` 的数量；
- 避免把用户 ID 放进指标标签；
- 用直方图/摘要而不是逐值。

## 22.4 日志

关键日志：

- JobManager：作业提交、调度、checkpoint 协调、failover；
- TaskManager：task 启动/失败、OOM、磁盘、网络；
- 用户代码：异常、脏数据、外部调用失败。

建议：

- 结构化 JSON；
- 带 jobId/taskId/subtask；
- 限制日志量，避免磁盘打满；
- 敏感信息脱敏；
- 集中收集（Loki/ELK）；
- 错误日志加告警。

```json
{
  "ts": "2026-11-26T10:00:00Z",
  "level": "ERROR",
  "job": "risk-job",
  "task": "window-sum[3]",
  "msg": "checkpoint failed",
  "checkpoint": 42,
  "reason": "timeout"
}
```

### 日志级别与采样

- 生产默认 INFO；
- 排查时临时开 DEBUG，但要限时；
- 高频日志（每条记录）用采样/计数器；
- 异常日志要包含上下文（key、offset、traceId），但不要打印敏感数据。

## 22.5 链路追踪

Flink 作业内部可以通过指标和日志定位，但跨 Kafka/数据库/下游服务的延迟需要 trace。常见做法：

- 在消息头传递 traceId；
- UDF/Sink 埋点；
- 接 OpenTelemetry；
- 与上游/下游系统共享 trace。

```text
Producer → Kafka(headers: traceId) → Flink source → 算子 → sink → 下游
                                                    │
                                                    └─ span: process/query/write
```

注意：trace 会增加开销，采样率要合理。2.3 的 OpenTelemetry exporter 支持压缩/批量，适合高吞吐。

## 22.6 SLO 与告警

不要只告警“作业挂了”。更有用的 SLO：

- 端到端 P99 延迟；
- 数据新鲜度（watermark 与当前时间差）；
- Kafka lag；
- checkpoint 成功率；
- 脏数据比例；
- 结果表更新延迟。

```text
SLO 示例：
- 99% 的订单在 5 秒内进入结果表
- checkpoint 成功率 >= 99%
- Kafka lag < 100 万且不持续增长
- 脏数据比例 < 0.1%
```

告警分级：

- P0：作业失败、数据断流、checkpoint 连续失败；
- P1：延迟超阈值、lag 持续增长、反压严重；
- P2：状态增长异常、GC 频繁、磁盘使用率高。

每个告警都要有 runbook，否则只是噪音。

### 告警设计原则

- 告警要可行动：收到后知道做什么；
- 避免“抖动告警”：用持续时间/多窗口；
- 分级：P0 打电话，P2 工单；
- 抑制：上游故障时不要重复告警；
- 复盘：每次告警后更新 runbook。

## 22.7 Web UI 与 REST

Web UI 适合：

- 看 DAG、反压、checkpoint、异常；
- 手工触发 savepoint；
- 查看 subtask 指标。

REST API 适合自动化：

- 提交/取消作业；
- 查询状态；
- 触发 savepoint；
- 获取 metrics。

```bash
# 查询作业
curl http://jm:8081/jobs/overview

# 触发 savepoint
curl -X POST http://jm:8081/jobs/<job-id>/savepoints \
  -d '{"target-directory":"s3://bucket/savepoints","cancel-job":false}'
```

生产平台通常封装 REST，做权限、审计和配额。

作业结束后，JM 的内存和 Web UI 都会消失，但排障往往发生在事后。**HistoryServer** 读取归档的作业信息（`jobmanager.archive.fs.dir`）和已完成作业的日志/指标，提供只读的 Web UI 与 REST：

```yaml
jobmanager.archive.fs.dir: s3://bucket/flink/archive
historyserver.archive.fs.dir: s3://bucket/flink/archive
historyserver.web.port: 8082
```

它适合复盘“昨天 02:00 那个作业为什么重启”，但不能替代实时监控——归档有延迟，且依赖作业在结束前成功写入归档。

## 22.8 2.x 的可观测性增强

- **OpenTelemetry**：更完整的 metrics/traces 导出；2.3 的 FLIP-553 给 gRPC exporter 增加了压缩、批量和属性值截断，解决大作业指标 payload 超限被拒的问题：

```yaml
metrics.reporter.otel.exporter.compression: gzip
metrics.reporter.otel.batch.size: 1000
# 属性值截断的键名/结构随 FLIP-553 的实现变化，按所用版本的
# OTel reporter 插件文档配置，不要照抄二手文章的键名
```

- **应用级视图**（2.3）：集群 → 应用 → 作业；
- **自适应调度器指标**：并行度调整、资源等待；
- **Watermark alignment 指标**：drift、暂停时间；
- **物化表指标**：刷新状态、新鲜度。

升级后要更新看板和告警阈值，不要假设旧指标仍然存在。

## 22.9 MiniFlink 的指标

MiniFlink 提供 `MetricGroup`、Counter、Gauge、Meter，以及 `JobExecution.metrics()`：

```python
job = env.execute()
print(job.metrics()["job"])
print(job.metrics()["checkpoints"])
print(job.metrics()["tasks"])
```

它统计处理记录数、任务输入输出、watermark、checkpoint 次数和侧输出数量。没有 JMX/Prometheus reporter、历史存储和告警。指标实现见[第 32 章](32-miniflink-table-cep.md)。

## 22.10 PromQL 与看板

几个常用的查询（指标名随版本/ reporter 略有差异）：

```promql
# 吞吐（按算子）：numRecordsInPerSecond 已经是 Meter 导出的 gauge，
# 不要再套 rate()；也可以对累计量 numRecordsIn 做 rate()
sum(flink_taskmanager_job_task_numRecordsInPerSecond) by (task_name)

# 反压比例
sum(flink_taskmanager_job_task_backPressuredTimeMsPerSecond) by (task_name)
  / sum(flink_taskmanager_job_task_busyTimeMsPerSecond) by (task_name)

# checkpoint 最近一次时长（gauge，不是 histogram）
max by (job_name) (flink_jobmanager_job_lastCheckpointDuration)

# checkpoint 大小
max by (job_name) (flink_jobmanager_job_lastCheckpointSize)

# 重启次数：numRestarts 是 Gauge，不是 Counter，不能用 increase()
max by (job_name) (flink_jobmanager_job_numRestarts)

# Kafka lag：Flink 内置 reporter 通常没有这个指标；
# 用 Kafka exporter / JMX 的 kafka_consumergroup_lag，或 kafka-consumer-groups 命令
max by (consumergroup, topic) (kafka_consumergroup_lag)
```

注意：`lastCheckpointDuration` 是 gauge，不存在 `_bucket` 系列；要算 P99 必须自己从 checkpoint 历史/日志聚合，或者用支持 histogram 的自定义 reporter。`flink_kafka_consumer_lag` 也不是 Flink 的内置指标。

看板建议分四层：

```text
第一屏：SLO（延迟、lag、可用性、checkpoint 成功率）
第二屏：作业（吞吐、反压、状态、重启）
第三屏：subtask（倾斜、GC、磁盘）
第四屏：外部（Kafka/DB/S3 错误率、延迟）
```

告警规则示例：

```yaml
- alert: FlinkCheckpointFailing
  expr: flink_jobmanager_job_numberOfFailedCheckpoints > 3
  for: 10m
  labels: {severity: P1}
  annotations:
    summary: "Flink checkpoint 连续失败"
    runbook: "https://wiki/runbooks/flink-checkpoint"

- alert: FlinkKafkaLagGrowing
  # 来自 Kafka exporter/JMX，不是 Flink 内置指标
  expr: deriv(kafka_consumergroup_lag[10m]) > 0
  for: 15m
  labels: {severity: P1}
```

## 22.11 日志与 trace 的关联

在日志里带上：

- `job_id`、`task_name`、`subtask`；
- `checkpoint_id`；
- `trace_id`（从消息头透传）；
- `event_id`（业务幂等）；
- 错误分类（可重试/不可重试）。

```java
MDC.put("job_id", jobId);
MDC.put("subtask", String.valueOf(getRuntimeContext().getIndexOfThisSubtask()));
MDC.put("trace_id", record.getHeader("trace_id"));
```

这样从一条告警能跳到日志，再跳到 trace/业务事件，而不是在多个系统间猜。

## 22.12 OpenTelemetry trace 落地：传播、采样与关联

22.5 讲了“为什么需要 trace”，这一节讲“怎么让 trace 在 Flink 里真正连起来”。难点不在接入一个 exporter，而在**上下文传播**：一条记录从 Kafka 进来，经过多个算子、异步调用、sink，trace 不能断。

### 22.12.1 metrics 与 traces 的分工

| 信号 | 回答的问题 | 典型工具 |
|---|---|---|
| metrics | 整体是否健康、趋势如何 | Prometheus/Grafana |
| logs | 某个时刻发生了什么 | Loki/ELK |
| traces | 一条请求/记录慢在哪一段 | Tempo/Jaeger/OTel |

Flink 2.x 的 OpenTelemetry reporter 可以导出 metrics；trace 的覆盖取决于连接器和用户代码埋点。不要把“接了 OTel”当成“自动有全链路 trace”。

### 22.12.2 配置形状

```yaml
metrics.reporters: otel
metrics.reporter.otel.factory.class: org.apache.flink.metrics.otel.OpenTelemetryMetricReporterFactory
metrics.reporter.otel.exporter.endpoint: http://otel-collector:4317
metrics.reporter.otel.exporter.protocol: grpc
metrics.reporter.otel.exporter.compression: gzip
metrics.reporter.otel.batch.size: 1000
metrics.reporter.otel.service.name: flink-orders
```

2.3 的 FLIP-553 增强了 gRPC exporter 的压缩、批量和属性值截断，适合大作业。trace 使用独立的 `traces.reporters` 列表，工厂类名随版本变化（常见为 `org.apache.flink.traces.otel.OpenTelemetryTraceReporterFactory`，以发行版插件 jar 为准）：

```yaml
traces.reporters: otel
traces.reporter.otel.factory.class: org.apache.flink.traces.otel.OpenTelemetryTraceReporterFactory
traces.reporter.otel.exporter.endpoint: http://otel-collector:4317
traces.reporter.otel.service.name: flink-orders
```

采样通常放在 OTel Collector（尾部采样）或 reporter 的 filter 配置里，而不是假设有一个固定的 `sampler.ratio` 键；具体参数以所用版本的 Trace Reporters 文档为准。

无论键名如何，原则一致：

- **异步导出**：不要阻塞 task 线程；
- **批量 + 压缩**：降低网络和 collector 压力；
- **失败不影响作业**：exporter 挂了不能拖垮数据处理；
- **资源限制**：队列有界，避免 OOM。

### 22.12.3 上下文传播

一条记录的生命周期：

```text
Producer ── traceparent ──► Kafka ──► Source ──► Operator ──► Sink ──► 下游
             (headers)                 │           │          │
                                       └─ span ────┴─ span ───┴─ span
```

关键点：

1. **消息头透传**：上游把 W3C `traceparent`/`tracestate` 放进 Kafka headers；source 读取并恢复 context；
2. **算子内创建子 span**：在 `ProcessFunction`/`RichMapFunction`/UDF 里用当前 context 创建子 span，而不是新建一个孤立 trace；
3. **sink 注入**：写 Kafka/HTTP/DB 时把 context 注入下游消息头/请求头；
4. **异步调用**：Async I/O 的线程池会丢 ThreadLocal，必须显式传递 context；
5. **checkpoint/恢复**：trace 不需要进 checkpoint；恢复后是新 trace，不要试图续接旧 trace。

一个最小埋点：

```java
Span span = tracer.spanBuilder("parse-order")
    .setParent(Context.current().with(parentSpan))
    .setAttribute("job_id", jobId)
    .setAttribute("task_name", getRuntimeContext().getTaskName())
    .startSpan();
try (Scope ignored = span.makeCurrent()) {
    return parse(record);
} catch (Exception e) {
    span.recordException(e);
    throw e;
} finally {
    span.end();
}
```

`span.end()` 必须放在 `finally`，否则异常路径会泄漏 span。

### 22.12.4 采样

全量 trace 在高吞吐下成本很高。常见策略：

| 策略 | 做法 | 适用 |
|---|---|---|
| 头部采样 | 按比例决定是否采样 | 通用、成本可控 |
| 尾部采样 | collector 按错误/延迟决定保留 | 想保留异常链路 |
| 错误优先 | 出错/超时链路全采 | 排障 |
| 动态采样 | 按流量/租户调整 | 多租户平台 |

采样率要与 SLO 匹配：P99 排障需要足够样本，但不需要每条都采。头部采样一旦决定不采，后续错误也看不到；如果业务更关心异常，优先尾部采样或在 collector 侧做策略。

### 22.12.5 span 命名与属性

建议：

- span 名用稳定的操作名（`kafka.consume`、`parse-order`、`http.lookup`），不要把参数拼进去；
- 属性用低基数：`job_id`、`task_name`、`subtask`、`topic`、`partition`；
- 高基数字段（`order_id`、`user_id`）放日志/事件，不要做 span 属性；
- 记录错误类型、重试次数、超时；
- 不要记录敏感数据（手机号、身份证、token）。

### 22.12.6 常见坑

| 现象 | 原因 | 处理 |
|---|---|---|
| trace 在算子间断了 | 没有传播 context | source/sink 显式注入/提取 |
| 一条记录一个 trace，量爆炸 | 粒度太细 | 按批次/请求采样，降低 span 数 |
| 异步调用没有子 span | ThreadLocal 丢失 | 显式传 context |
| exporter 拖慢作业 | 同步导出/无界队列 | 异步、批量、有界 |
| span 不结束 | 异常路径未 `end()` | `try/finally` |
| 日志里没有 trace_id | MDC 未设置 | 在 source/算子入口写入 |
| 采样后看不到错误 | 头部采样丢弃 | 尾部采样/错误优先 |

### 22.12.7 检查清单

- [ ] metrics、logs、traces 使用同一套 `service.name`/`job_id`；
- [ ] 上游→Kafka→Flink→下游的 trace context 能透传；
- [ ] 异步 I/O/线程池显式传递 context；
- [ ] exporter 异步、批量、有界，失败不影响作业；
- [ ] 采样率与成本/排障需求匹配；
- [ ] span 属性低基数，不含敏感数据；
- [ ] 日志带 `trace_id`/`span_id`，能从告警跳转；
- [ ] trace 存储有保留期和成本预算。

指标、日志和 trace 回答“发生了什么”；如果要把 savepoint、停止、升级和回滚串成可重试的自动化流程，见[附录 M](../appendix/M-rest-operations.md)。

## 本章小结

可观测性不是“接一个 Prometheus”，而是从业务 SLO 反推指标、日志和 trace。Flink 的指标很多，先抓住吞吐、延迟、反压、checkpoint、lag 五组，再逐步细化。

下一章讨论安全与多租户：当流平台从“一个作业”变成“多人共享的基础设施”时，认证、授权、隔离和审计如何设计。

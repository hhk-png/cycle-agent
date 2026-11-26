# 附录 U Prometheus 自监控与元监控

监控系统自己也会坏：抓取循环卡住、WAL 写满、规则求值超时、Alertmanager 不可达、remote write 队列积压。最危险的情况是"Prometheus 坏了，但没人知道"，因为它本身就是告警链路的一部分。

本附录给出 Prometheus 自监控的指标分类、告警规则、外部元监控架构和排障顺序。第 13、22、23 章分散讲过这些点；这里把它们收成一套可落地的清单。

## U.1 为什么需要"元监控"

三个失败模式：

1. **自监控盲区**：Prometheus 挂了，它自己的规则无法求值，告警发不出去；
2. **静默降级**：抓取还在跑，但样本被丢弃/规则失败/通知队列满，面板看起来正常；
3. **告警链路故障**：Prometheus 正常，Alertmanager 不可达，告警停在队列里。

元监控的目标不是"再装一个 Prometheus"，而是：

- 用**独立**的监控/告警路径观察生产 Prometheus；
- 覆盖抓取、存储、查询、规则、通知、remote write 六个面；
- 对"没有数据"和"数据错误"都告警；
- 定期演练告警链路（dead man's switch）。

## U.2 自监控指标分类

### 进程/运行时

```promql
up{job="prometheus"}
prometheus_build_info
process_cpu_seconds_total
process_resident_memory_bytes
process_open_fds
process_max_fds
go_goroutines
go_memstats_alloc_bytes
go_gc_duration_seconds
```

用途：OOM、FD 泄漏、GC 压力、版本/构建信息。

### 抓取

```promql
prometheus_target_scrapes_total
prometheus_target_scrape_pool_targets
prometheus_target_scrape_pool_sync_total
prometheus_target_scrapes_exceeded_sample_limit_total
prometheus_target_scrapes_sample_out_of_order_total
prometheus_target_scrapes_sample_duplicate_timestamp_total
scrape_duration_seconds
scrape_samples_scraped
scrape_series_added
```

用途：目标数、抓取失败、样本限制、乱序/重复、抓取耗时、基数增长。

注意：`up` 是每个目标一条；`prometheus_target_scrapes_total` 是计数器，适合算速率。

### TSDB/存储

```promql
prometheus_tsdb_head_series
prometheus_tsdb_head_samples_appended_total
prometheus_tsdb_head_chunks
prometheus_tsdb_storage_blocks_bytes
prometheus_tsdb_blocks_loaded
prometheus_tsdb_compactions_total
prometheus_tsdb_compactions_failed_total
prometheus_tsdb_compaction_duration_seconds
prometheus_tsdb_wal_segment_current
prometheus_tsdb_wal_truncations_total
prometheus_tsdb_wal_corruptions_total
prometheus_tsdb_out_of_order_samples_total
```

用途：head 基数、摄入速率、磁盘、compaction 健康、WAL 滚动/损坏、乱序。

### 查询

```promql
prometheus_engine_queries
prometheus_engine_queries_concurrent_max
prometheus_engine_query_duration_seconds
prometheus_http_requests_total
prometheus_http_request_duration_seconds_bucket
prometheus_http_response_size_bytes_bucket
```

用途：并发查询、慢查询、API 错误率、响应体大小。

### 规则

```promql
prometheus_rule_evaluations_total
prometheus_rule_evaluation_failures_total
prometheus_rule_evaluation_duration_seconds
prometheus_rule_group_last_duration_seconds
prometheus_rule_group_last_evaluation_timestamp_seconds
prometheus_rule_group_rules
```

用途：规则失败、求值延迟、规则组是否卡住、规则数量。

### 通知

```promql
prometheus_notifications_errors_total
prometheus_notifications_dropped_total
prometheus_notifications_queue_length
prometheus_notifications_queue_capacity
prometheus_notifications_alertmanagers_discovered
```

用途：Alertmanager 不可达、队列满、告警丢弃。

### Remote write

```promql
prometheus_remote_storage_samples_pending
prometheus_remote_storage_samples_failed_total
prometheus_remote_storage_samples_dropped_total
prometheus_remote_storage_samples_retried_total
prometheus_remote_storage_queue_highest_sent_timestamp_seconds
prometheus_remote_storage_shards
prometheus_remote_storage_shards_desired
prometheus_remote_storage_shards_max
```

用途：积压、失败、丢弃、发送延迟、分片是否顶满。

### 服务发现

```promql
prometheus_sd_refresh_failures_total
prometheus_sd_discovered_targets
prometheus_sd_kubernetes_events_total
```

用途：SD 刷新失败、目标数异常、K8s 事件。

## U.3 自监控告警规则

以下规则需要按环境调整阈值；重点是"每个规则都能指向一个可行动的原因"。

```yaml
groups:
  - name: prometheus-self
    interval: 30s
    rules:
      - alert: PrometheusDown
        expr: up{job="prometheus"} == 0
        for: 5m
        labels:
          severity: page
        annotations:
          summary: "Prometheus {{ $labels.instance }} is down"
          runbook_url: "https://runbooks.example/prometheus-down"

      - alert: PrometheusConfigReloadFailed
        expr: prometheus_config_last_reload_successful == 0
        for: 10m
        labels:
          severity: page
        annotations:
          summary: "Prometheus config reload failed"
          runbook_url: "https://runbooks.example/prometheus-reload"

      - alert: PrometheusTSDBCompactionFailed
        expr: increase(prometheus_tsdb_compactions_failed_total[1h]) > 0
        for: 10m
        labels:
          severity: page
        annotations:
          summary: "TSDB compaction is failing"

      - alert: PrometheusWALCorruption
        expr: increase(prometheus_tsdb_wal_corruptions_total[1h]) > 0
        for: 5m
        labels:
          severity: page
        annotations:
          summary: "WAL corruption detected"

      - alert: PrometheusHeadSeriesHigh
        expr: prometheus_tsdb_head_series > 2e6
        for: 30m
        labels:
          severity: ticket
        annotations:
          summary: "Head series is high ({{ $value }})"

      - alert: PrometheusRuleEvaluationFailures
        expr: rate(prometheus_rule_evaluation_failures_total[5m]) > 0
        for: 10m
        labels:
          severity: page
        annotations:
          summary: "Rule evaluation is failing"

      - alert: PrometheusNotificationFailures
        expr: rate(prometheus_notifications_errors_total[5m]) > 0
        for: 10m
        labels:
          severity: page
        annotations:
          summary: "Prometheus cannot deliver alerts to Alertmanager"

      - alert: PrometheusNotificationsDropped
        expr: rate(prometheus_notifications_dropped_total[5m]) > 0
        for: 5m
        labels:
          severity: page
        annotations:
          summary: "Prometheus is dropping notifications"

      - alert: PrometheusRemoteWriteLag
        expr: time() - prometheus_remote_storage_queue_highest_sent_timestamp_seconds > 300
        for: 10m
        labels:
          severity: page
        annotations:
          summary: "Remote write is more than 5m behind"

      - alert: PrometheusRemoteWriteDropped
        expr: rate(prometheus_remote_storage_samples_dropped_total[5m]) > 0
        for: 5m
        labels:
          severity: page
        annotations:
          summary: "Remote write is dropping samples"

      - alert: PrometheusScrapeSampleLimit
        expr: rate(prometheus_target_scrapes_exceeded_sample_limit_total[5m]) > 0
        for: 10m
        labels:
          severity: ticket
        annotations:
          summary: "A target is exceeding sample_limit"

      - alert: PrometheusOutOfOrderSamples
        expr: rate(prometheus_target_scrapes_sample_out_of_order_total[5m]) > 0
        for: 10m
        labels:
          severity: ticket
        annotations:
          summary: "Out-of-order samples are being dropped"

      - alert: PrometheusQueryLatencyHigh
        expr: |
          histogram_quantile(
            0.99,
            sum by (le) (rate(prometheus_http_request_duration_seconds_bucket{handler="/api/v1/query"}[5m]))
          ) > 2
        for: 15m
        labels:
          severity: ticket
        annotations:
          summary: "Prometheus query P99 > 2s"

      - alert: PrometheusDiskWillFill
        expr: predict_linear(node_filesystem_avail_bytes{mountpoint="/prometheus"}[6h], 4*24*3600) < 0
        for: 30m
        labels:
          severity: ticket
        annotations:
          summary: "Prometheus disk will fill within 4 days"
```

注意：

- `PrometheusDown` 必须由**外部** Prometheus/监控系统求值，否则 Prometheus 挂了没人告警；
- `prometheus_tsdb_wal_corruptions_total` 在部分版本中不存在，使用前先确认指标；
- `prometheus_http_request_duration_seconds_bucket` 的 `handler` 标签值随版本变化，先在 `/metrics` 里确认；
- 阈值（2e6 series、300s lag、2s P99）只是示例，按规模调整。

## U.4 外部元监控架构

最可靠的做法是"监控系统不要自己监控自己"：

```text
生产 Prometheus ──remote write──► 中心/长期存储 ──► 查询/告警
        │
        └──被外部 Prometheus 抓取 /metrics（只抓自监控指标）
```

三种模式：

1. **独立 Prometheus（推荐）**：一个小的、独立的 Prometheus 只抓生产 Prometheus 的 `/metrics`，规则/Alertmanager 也独立；成本低，盲区小。
2. **中心长期存储**：生产 Prometheus remote write 到 Mimir/Thanos，中心查询层对"最后样本时间/序列数"做告警；但 remote write 故障时中心也看不到。
3. **外部 SaaS/云监控**：用云厂商的 uptime/黑盒探测抓 `/-/healthy`、`/-/ready`，作为最后一道防线。

至少要有：

- 一个不依赖生产 Prometheus 的 `up`/健康检查；
- 一个 dead man's switch（永远 firing 的测试告警），验证 Prometheus → Alertmanager → 通知渠道全链路；
- 定期演练：停掉生产 Prometheus/Alertmanager，确认告警能到达。

## U.5 健康端点与探针

```text
/-/healthy   进程存活（不检查 TSDB/抓取）
/-/ready     TSDB 完成 WAL replay，可服务查询
/metrics     自身指标
/-/reload    重载（需 --web.enable-lifecycle）
```

Kubernetes 探针建议：

```yaml
livenessProbe:
  httpGet:
    path: /-/healthy
    port: 9090
  initialDelaySeconds: 30
  periodSeconds: 10
readinessProbe:
  httpGet:
    path: /-/ready
    port: 9090
  initialDelaySeconds: 10
  periodSeconds: 5
```

要点：

- `/-/healthy` 只表示进程活着，不代表抓取/规则正常；
- `/-/ready` 在 WAL replay 期间返回非 200，避免流量过早进入；
- 不要把 `/-/reload` 暴露给 liveness/readiness；
- 大规模 WAL replay 可能几分钟，`initialDelaySeconds` 要留足。

## U.6 自监控面板

一个最小可用的 Prometheus 自监控面板应包含：

1. **可用性**：`up{job="prometheus"}`、`/-/ready` 探测；
2. **抓取**：目标数、失败率、`scrape_duration_seconds` P99、`scrape_series_added`；
3. **存储**：head series、摄入速率、blocks bytes、compaction 失败/耗时、WAL 段；
4. **查询**：QPS、P99、错误率、并发、max-samples 触顶；
5. **规则**：求值失败、求值耗时、规则组最后求值时间；
6. **通知**：队列长度/容量、失败/丢弃；
7. **remote write**：pending/dropped/retried、highest sent lag、shards；
8. **资源**：CPU、RSS、FD、GC、磁盘。

面板要区分"Prometheus 自身"和"被监控目标"；不要把业务指标和自监控指标混在一个图里。

## U.7 常见自监控故障

| 症状 | 可能原因 | 排查 |
|---|---|---|
| 所有告警突然消失 | Prometheus 挂了/规则失败/Alertmanager 不可达 | 外部 `up`、`/-/ready`、notifier 指标 |
| 告警延迟 | 规则求值慢/队列积压 | `rule_evaluation_duration_seconds`、queue |
| 面板数据缺口 | 抓取失败/append 错误/remote write 积压 | `up`、out-of-order、pending |
| 内存持续增长 | 高基数/慢查询/规则中间结果 | head series、查询日志、GC |
| 磁盘增长快 | retention/compaction/高基数 | blocks bytes、compaction、retention |
| reload 失败 | 配置错误/权限/secret | `config_last_reload_successful`、日志 |
| 规则不触发 | 表达式/数据/for | `/rules` health、last evaluation |
| remote write 落后 | 网络/远端限流/队列满 | highest sent、pending、shards |

## U.8 容量与自监控

自监控本身也有成本：

- 抓取自身 `/metrics` 会增加少量序列；
- 规则求值/告警会产生额外查询；
- 长期保留自监控指标会占存储；
- 外部元监控 Prometheus 也需要容量规划。

建议：

- 自监控指标 retention 可以短于业务指标（如 15～30 天）；
- 只保留关键自监控指标，不要把所有 `go_*` 都长期保留；
- 自监控告警规则要少而精，避免"监控系统的告警风暴"；
- 元监控 Prometheus 的资源独立于生产 Prometheus，避免一起 OOM。

## U.9 Mini-Prometheus 的自监控

Mini 的 `/metrics` 暴露：

```text
prometheus_build_info
prometheus_tsdb_head_series
prometheus_tsdb_head_samples_appended_total
prometheus_tsdb_blocks_loaded
prometheus_tsdb_storage_blocks_bytes
prometheus_config_last_reload_successful
prometheus_rule_evaluations_total
prometheus_rule_evaluation_failures_total
prometheus_target_scrapes_total
up
```

它足以演示"Prometheus 监控自己"，但不是完整自监控指标集：

- 没有 remote write 队列指标；
- 没有完整的 query/HTTP 指标；
- 没有 WAL/compaction 细分指标；
- 没有 SD 指标。

用 Mini 练习时，可以先抓自己的 `/metrics`，再写一条 `up{job="mini"}` 和 `prometheus_tsdb_head_series` 的告警；生产请以真实 Prometheus 的指标名为准。

## 本章小结

- 元监控的核心是"独立路径 + 六个面"：抓取、TSDB、查询、规则、通知、remote write。
- `up` 只说明抓取成功；规则/通知/remote write 各有独立失败指标。
- 自监控告警必须由外部系统求值，否则 Prometheus 挂了没人知道。
- 健康端点、dead man's switch、告警链路演练是最后一道防线。
- 自监控也要控制基数/retention，避免监控系统自己成为成本/故障源。

## 延伸阅读

- Prometheus 官方 "Operational aspects" 与自监控指标文档。
- 本教程第 13 章（架构/失败域）、第 22 章（容量）、第 23 章（运维）、附录 E（故障排查）。
- 附录 S（生产事故案例集）中的 Prometheus 自身故障案例。

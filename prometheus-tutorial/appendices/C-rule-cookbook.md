# 附录 C 规则库：可直接改造的 recording/alerting 示例

## C.1 使用说明

- 所有规则都需要按你的指标名/标签调整；
- `for`/阈值应基于历史数据校准；
- 每个 page 告警都应配 `runbook_url`；
- 规则命名遵循 `level:metric:operations`；
- 用 `promtool test rules` 或 pytest 验证。

## C.2 Recording rules

```yaml
groups:
  - name: aggregation
    interval: 30s
    rules:
      - record: job:http_requests:rate5m
        expr: sum by (job) (rate(http_requests_total[5m]))

      - record: job:http_errors:ratio5m
        expr: |
          sum by (job) (rate(http_requests_total{code=~"5.."}[5m]))
          /
          sum by (job) (rate(http_requests_total[5m]))

      - record: job:http_request_duration_seconds:p90_5m
        expr: |
          histogram_quantile(
            0.9,
            sum by (job, le) (rate(http_request_duration_seconds_bucket[5m]))
          )

      - record: instance:node_cpu:utilization5m
        expr: |
          1 - avg by (instance) (
            rate(node_cpu_seconds_total{mode="idle"}[5m])
          )
```

## C.3 可用性/错误率

```yaml
groups:
  - name: api-slo
    interval: 30s
    rules:
      - alert: APITargetDown
        expr: up{job="api"} == 0
        for: 2m
        labels:
          severity: page
        annotations:
          summary: "{{ $labels.instance }} is down"
          runbook_url: "https://runbooks.example/target-down"

      - alert: APIHighErrorRatio
        expr: |
          sum by (job) (rate(http_requests_total{code=~"5.."}[5m]))
          /
          clamp_min(sum by (job) (rate(http_requests_total[5m])), 0.001)
          > 0.05
        for: 10m
        labels:
          severity: page
        annotations:
          summary: "{{ $labels.job }} 5xx ratio > 5%"
          runbook_url: "https://runbooks.example/api-errors"

      - alert: APIHighLatencyP90
        expr: |
          histogram_quantile(
            0.9,
            sum by (job, le) (rate(http_request_duration_seconds_bucket[5m]))
          ) > 0.3
        for: 10m
        labels:
          severity: page
        annotations:
          summary: "{{ $labels.job }} P90 latency > 300ms"
```

## C.4 饱和度

```yaml
groups:
  - name: saturation
    rules:
      - alert: NodeCPUHigh
        expr: instance:node_cpu:utilization5m > 0.9
        for: 15m
        labels:
          severity: ticket
        annotations:
          summary: "{{ $labels.instance }} CPU > 90%"

      - alert: NodeMemoryHigh
        expr: |
          (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) > 0.9
        for: 15m
        labels:
          severity: ticket

      - alert: DiskWillFillIn4Days
        expr: |
          predict_linear(node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"}[6h], 4*24*3600) < 0
        for: 30m
        labels:
          severity: ticket
        annotations:
          summary: "{{ $labels.instance }} {{ $labels.mountpoint }} will fill in 4 days"

      - alert: QueueBacklog
        expr: sum by (queue) (queue_depth) > 1000
        for: 10m
        labels:
          severity: page
```

## C.5 Kubernetes

```yaml
groups:
  - name: kubernetes
    rules:
      - alert: PodCrashLooping
        expr: |
          increase(kube_pod_container_status_restarts_total[15m]) > 3
        for: 5m
        labels:
          severity: ticket
        annotations:
          summary: "{{ $labels.namespace }}/{{ $labels.pod }} is restarting"

      - alert: DeploymentReplicasMismatch
        expr: |
          kube_deployment_status_replicas_available
          < kube_deployment_spec_replicas
        for: 15m
        labels:
          severity: ticket

      - alert: PVCAlmostFull
        expr: |
          kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes > 0.85
        for: 15m
        labels:
          severity: ticket

      - alert: NodeNotReady
        expr: kube_node_status_condition{condition="Ready",status="true"} == 0
        for: 5m
        labels:
          severity: page
```

## C.6 SLO burn rate

```yaml
groups:
  - name: slo-burn
    interval: 30s
    rules:
      - record: job:slo_availability:ratio5m
        expr: |
          sum by (job) (rate(http_requests_total{code!~"5.."}[5m]))
          /
          sum by (job) (rate(http_requests_total[5m]))

      - record: job:slo_availability:ratio1h
        expr: |
          sum by (job) (rate(http_requests_total{code!~"5.."}[1h]))
          /
          sum by (job) (rate(http_requests_total[1h]))

      - alert: SLOBurnRateHigh
        expr: |
          (1 - job:slo_availability:ratio1h) > 14.4 * 0.001
          and
          (1 - job:slo_availability:ratio5m) > 14.4 * 0.001
        for: 2m
        labels:
          severity: page
        annotations:
          summary: "{{ $labels.job }} is burning error budget >14.4x"
          runbook_url: "https://runbooks.example/slo-burn"
```

这里假设 SLO 99.9%（预算 0.001），14.4 是 1h 窗口的 page 阈值；按你的 SLO 调整。

## C.7 证书/时间/进程

```yaml
groups:
  - name: platform
    rules:
      - alert: TLSCertExpiringSoon
        expr: |
          probe_ssl_earliest_cert_expiry - time() < 7 * 24 * 3600
        for: 1h
        labels:
          severity: ticket

      - alert: PrometheusConfigReloadFailed
        expr: prometheus_config_last_reload_successful == 0
        for: 5m
        labels:
          severity: page

      - alert: PrometheusNotificationsFailing
        expr: rate(prometheus_notifications_errors_total[5m]) > 0
        for: 10m
        labels:
          severity: page
```

## C.8 规则审查清单

- 表达式是否先过滤再聚合？
- 是否有分母为 0 的保护？
- `for` 是否合理？
- 标签是否低基数？
- 是否有 runbook？
- 是否覆盖空数据/缺失？
- 是否测试 pending/firing/resolve？
- 是否与现有告警重复？

## 延伸阅读

- 本教程第 9、25、26 章。
- Prometheus 官方 alerting rules 示例。

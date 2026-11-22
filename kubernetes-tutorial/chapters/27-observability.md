# 第 27 章 可观测性：指标、日志、追踪与事件

## 27.1 三个支柱 + 一个

- **Metrics**：聚合数值，适合趋势、告警、容量规划。
- **Logs**：离散事件，适合细节、审计、排障。
- **Traces**：请求链路，适合定位跨服务延迟。
- **Events**：Kubernetes 特有，记录调度、拉镜像、探针失败等。

可观测性不是“装 Prometheus + ELK”，而是：

1. 能回答未知问题。
2. 数据有上下文（服务、版本、节点、租户）。
3. 成本可控。
4. 告警可行动。

## 27.2 黄金信号与 USE/RED

服务级：

- **Latency**：延迟（区分成功/失败）。
- **Traffic**：QPS。
- **Errors**：错误率。
- **Saturation**：饱和度（CPU/内存/队列）。

资源级（USE）：

- Utilization：使用率。
- Saturation：排队/饱和度。
- Errors：错误。

微服务（RED）：

- Rate：请求速率。
- Errors：错误数。
- Duration：延迟分布。

Kubernetes 控制面：

- API Server 请求延迟/错误/排队。
- etcd 延迟/磁盘 fsync/leader 变化。
- 调度器调度延迟/Pending Pod。
- 控制器 reconcile 错误/队列深度。
- kubelet PLEG 延迟/运行时操作延迟。

## 27.3 指标

### metrics-server

提供：

- Node CPU/内存使用。
- Pod CPU/内存使用。
- 供 HPA `Resource` 指标和 `kubectl top`。

不提供长期存储、历史查询、业务指标。

### Prometheus

拉取模型：

```yaml
scrape_configs:
  - job_name: kubernetes-pods
    kubernetes_sd_configs:
      - role: pod
    relabel_configs:
      - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_scrape]
        action: keep
        regex: "true"
```

部署方式：

- Prometheus Operator + ServiceMonitor/PodMonitor。
- kube-prometheus-stack（Prometheus + Alertmanager + Grafana + exporters）。
- 托管服务（AMP、GCP Managed Prometheus、Azure Monitor）。

关键指标：

- `kube_pod_status_phase`
- `kube_deployment_status_replicas_available`
- `container_cpu_usage_seconds_total`
- `container_memory_working_set_bytes`
- `kubelet_volume_stats_used_bytes`
- `apiserver_request_duration_seconds`
- `etcd_server_leader_changes_seen_total`
- `scheduler_scheduling_attempt_duration_seconds`

基数问题：

- 标签组合爆炸（Pod 名、URL、用户 ID）。
- 每个 Pod 的指标在扩缩容时产生新时间序列。
- 高基数导致 Prometheus 内存/存储爆炸。

治理：

- 限制标签。
- 用 recording rules 聚合。
- 丢弃不需要的指标。
- 控制 scrape 频率。
- 使用 remote write 分层存储。

## 27.4 日志

### 容器日志

kubelet 把容器 stdout/stderr 写到节点文件：

```text
/var/log/pods/<namespace>_<pod>_<uid>/<container>/0.log
```

`kubectl logs` 读取这些文件（通过 API Server）。

日志轮转：

```yaml
# kubelet 配置
containerLogMaxSize: 50Mi
containerLogMaxFiles: 5
```

### 聚合

常见架构：

```text
容器 stdout → 节点 agent（Fluent Bit/Filebeat/Vector）
           → 日志后端（Loki/Elasticsearch/ClickHouse/S3）
           → 查询/告警（Grafana/Kibana）
```

选择：

- Loki：低成本、标签索引、适合 Kubernetes。
- Elasticsearch/OpenSearch：全文搜索强，成本高。
- ClickHouse：高吞吐分析，适合自建。
- 云日志服务：托管、按量计费。

### 结构化日志

推荐 JSON：

```json
{"level":"error","ts":"2026-01-01T00:00:00Z","service":"api","trace_id":"...","msg":"db timeout","duration_ms":1200}
```

好处：

- 可查询字段。
- 与 trace_id 关联。
- 减少正则解析。

### 日志成本

日志通常比指标贵：

- 量大。
- 保留久。
- 全文索引贵。

策略：

- 分级保留（热/温/冷）。
- 采样/丢弃低价值日志。
- 只索引关键字段。
- 生产 debug 日志默认关闭。

## 27.5 追踪

OpenTelemetry：

- SDK 在应用中埋点。
- Collector 接收/处理/导出。
- 后端：Jaeger、Tempo、Zipkin、云厂商。

Kubernetes 相关：

- 服务网格自动生成 span。
- Ingress/Gateway 注入 trace context。
- `trace_id` 写入日志，关联三者。

采样：

- 头部采样：入口决定。
- 尾部采样：收集完整 trace 后决定（成本低，延迟高）。
- 对错误/慢请求全采样。

## 27.6 事件

```bash
kubectl get events -A --sort-by=.lastTimestamp
kubectl describe pod <pod>
```

事件类型：

- Normal：Scheduled、Pulled、Created、Started。
- Warning：FailedScheduling、FailedMount、Unhealthy、BackOff、Evicted。

事件保留时间有限（默认 1 小时），不适合长期审计。需要长期保留时，用事件导出器写入日志/分析系统。

## 27.7 SLO 与告警

SLO（Service Level Objective）：

```text
99.9% 的请求在 300ms 内成功，按 30 天窗口
```

错误预算：

```text
30 天 * 0.1% = 43.2 分钟不可用
```

告警策略：

- 症状告警（错误率、延迟）优先于原因告警（CPU 高）。
- 多窗口/多 burn rate。
- 告警要有 runbook。
- 避免“每个指标都告警”。

Kubernetes 常见告警：

- Pod CrashLoopBackOff。
- Deployment 可用副本不足。
- Node NotReady。
- PVC 使用率 > 85%。
- 证书即将过期。
- API Server 5xx/延迟。
- etcd fsync 延迟。
- 调度 Pending Pod 超过阈值。
- HPA 达到 maxReplicas。
- Job 失败。

## 27.8 可观测性数据关联

排障时需要把：

- 指标（哪个服务/时间点异常）。
- 日志（具体错误）。
- Trace（哪个调用慢）。
- Event（Pod 调度/重启）。
- 变更（发布/配置/扩缩容）。

关联键：

- `service.name`、`service.version`。
- `k8s.namespace.name`、`k8s.pod.name`、`k8s.node.name`。
- `trace_id`、`span_id`。
- `cluster`、`region`、`tenant`。

没有统一标签，数据就是孤岛。

## 27.9 OpenTelemetry 语义约定

使用标准属性：

```text
service.name
service.version
deployment.environment.name
k8s.cluster.name
k8s.namespace.name
k8s.pod.name
k8s.container.name
http.request.method
http.response.status_code
```

旧的 `deployment.environment` 已被 `deployment.environment.name` 取代；混用会让仪表盘按环境拆分失败。升级 SDK/Collector 时先统一语义约定版本。

好处：

- 后端/工具兼容。
- Dashboard 可复用。
- 跨团队查询一致。

## 27.10 成本控制

可观测性成本可能超过计算成本：

- 指标基数。
- 日志量。
- Trace 采样。
- 保留周期。
- 跨区传输。

策略：

- 分层：热/温/冷。
- 采样。
- 聚合。
- 只保留关键标签。
- 定期清理无用 dashboard/alert。
- 用用量配额约束团队。

## 27.11 MiniK8s 的可观测性

- `MetricsController` 生成 PodMetrics/NodeMetrics。
- API Server `/metrics` 暴露简单 Prometheus 文本。
- Event 对象记录调度、启动、探针失败。
- CLI `top pods/nodes` 读取 metrics API。
- 日志通过 kubelet 内存缓冲和 `/log` 子资源。

示例：

```bash
python -m mini_k8s.cli top pods
python -m mini_k8s.cli get events
curl -s http://127.0.0.1:8080/metrics
```

MiniK8s 没有 Prometheus/Grafana/OTel 集成，但 API 形状与真实集群相似。GPU 指标（利用率、显存、Xid、ECC、NVLink）见第 22 章。

## 27.12 OpenTelemetry Collector 的部署与采样

应用的埋点只是开始，数据要经过采集、处理、导出。OpenTelemetry Collector 是这一层的事实标准。

### 三种部署形态

```text
1. Agent（DaemonSet）：每节点一个，接收本节点 Pod 的 OTLP/日志
2. Gateway（Deployment）：集中接收/处理/导出，做采样/脱敏/路由
3. Sidecar：按 Pod 注入，隔离性好但资源开销大
```

常见组合：DaemonSet agent 收集 + Gateway 聚合。大规模下不要让每个 Pod 直连后端，否则连接数/认证/限流都难管理。

### 最小配置

```yaml
receivers:
  otlp:
    protocols:
      grpc: {endpoint: 0.0.0.0:4317}
      http: {endpoint: 0.0.0.0:4318}
processors:
  batch: {}
  memory_limiter:
    check_interval: 1s
    limit_mib: 512
  k8sattributes:
    extract:
      metadata: [k8s.namespace.name, k8s.pod.name, k8s.node.name]
exporters:
  otlphttp:
    endpoint: https://otel.example.com
service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, k8sattributes, batch]
      exporters: [otlphttp]
```

要点：

- `memory_limiter` 必须放在 batch 前，防止 OOM。
- `k8sattributes` 补全 Pod/namespace/node，才能和指标/日志关联。
- `batch` 降低导出压力，但会增加延迟；对实时告警要权衡。
- 用 `filter`/`transform` 删除高基数/敏感字段，而不是全部送到后端。

### 采样策略

- **头部采样**：入口决定，简单但可能漏掉错误/慢请求。
- **尾部采样**：Collector 收集完整 trace 后按延迟/错误决定，成本低、效果好，但需要缓冲和更多内存。
- **按比例 + 关键路径全采样**：核心交易全采，健康检查/静态资源低采样。
- **错误全采样**：`status.code=ERROR` 或延迟超过阈值时保留。

### 与 Prometheus 的关系

- OTel 指标可以导出到 Prometheus（remote write/ exporter），但语义不完全一致（delta vs cumulative、exemplars）。
- 不要同时维护两套埋点；选择一个 SDK/语义约定，后端按需转换。
- exemplars 可以把指标和 trace 关联起来，但存储/查询支持因后端而异。

### 成本与可靠性

- Collector 自身要高可用/多副本，否则观测数据丢失。
- 导出失败要有重试/队列/磁盘缓冲，避免后端抖动导致应用阻塞。
- 控制标签基数：`http.target` 含用户 ID、Pod 名作标签，都会爆炸。
- 日志/追踪/指标分别设保留与采样策略，不要一刀切。

MiniK8s 只暴露简单 `/metrics` 和 Event，不集成 OTel；这一节是真实平台的可观测性数据面。

## 27.13 Prometheus 生产栈：从采集到告警

“装一个 Prometheus”与“有一套可用的可观测性”之间，差的是采集覆盖面、标签治理、告警路由和高可用。

### 27.13.1 四类采集源

| 来源 | 内容 | 典型组件 |
|---|---|---|
| Kubernetes 对象状态 | Deployment 副本、Pod phase、PVC、Node condition | kube-state-metrics |
| 节点/内核 | CPU、内存、磁盘、网络、文件系统 | node-exporter |
| 容器/cgroup | 容器 CPU/内存/网络、OOM | cAdvisor（kubelet 内） |
| 应用/中间件 | 请求量、延迟、队列、JVM/Go runtime | 应用埋点、exporter |

它们的分工：

- **kube-state-metrics** 读 API Server，生成“对象状态”指标；它不采集资源使用量。
- **node-exporter** 在节点上采集主机指标；容器指标由 kubelet/cAdvisor 暴露。
- 应用指标应由应用自己暴露 `/metrics`，或通过 sidecar/exporter 转换。

常见误区：用 kube-state-metrics 算 CPU 使用率，或用 cAdvisor 算 Deployment 副本数。两者语义不同。

**1.37 的指标移除**：kubelet 移除了部分 cAdvisor 标志与旧指标（如 `container_cpu_load_average_10s`、`container_tasks_state`、`userDefinedMetrics` 等，具体以 release notes 为准）。升级后仪表盘/告警可能“静默变空”，而不是报错。升级前用 `kubectl get --raw /metrics` 对比指标名，检查 Prometheus rule 与 Grafana 查询。

### 27.13.2 ServiceMonitor 与采集配置

Prometheus Operator 用 CRD 管理采集：

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: orders
  namespace: shop
spec:
  selector:
    matchLabels: {app: orders}
  endpoints:
    - port: metrics
      interval: 30s
      path: /metrics
      relabelings:
        - sourceLabels: [__meta_kubernetes_pod_name]
          targetLabel: pod
```

要点：

- `selector` 匹配 Service 的 label，不是 Pod；确保 Service 暴露了 metrics 端口。
- `interval` 与 retention 一起决定存储量。
- 用 `metricRelabelings` 丢弃高基数/敏感指标。
- 多租户集群中，ServiceMonitor 的 namespace selector 要限制，避免租户抓取别人的指标。

### 27.13.3 告警规则：围绕症状，而不是原因

好的告警描述“用户受影响”，而不是“某个内部指标高了”。

```yaml
groups:
  - name: slo
    rules:
      - alert: APIHighErrorRate
        expr: |
          sum(rate(http_requests_total{job="orders",code=~"5.."}[5m]))
            /
          sum(rate(http_requests_total{job="orders"}[5m]))
            > 0.05
        for: 10m
        labels:
          severity: page
        annotations:
          summary: "orders 5xx 错误率超过 5%"
          runbook_url: "https://runbooks.example.com/orders-5xx"

      - alert: PodCrashLooping
        expr: |
          increase(kube_pod_container_status_restarts_total[15m]) > 3
        for: 5m
        labels:
          severity: ticket
```

原则：

- **for** 避免瞬时抖动；但关键故障不能等太久。
- **severity** 分级：page（立即处理）、ticket（工作时间）、info。
- 每条告警有 runbook、owner、影响面。
- 避免“每个 Pod 都告警”；用聚合/服务级 SLO。
- 告警风暴时先抑制根因告警，再处理衍生告警。

### 27.13.4 Alertmanager 路由与抑制

```yaml
route:
  receiver: default
  group_by: [alertname, cluster, service]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - matchers: [severity="page"]
      receiver: pager
      continue: true
    - matchers: [severity="ticket"]
      receiver: ticket
receivers:
  - name: default
  - name: pager
  - name: ticket
inhibit_rules:
  - source_matchers: [alertname="NodeDown"]
    target_matchers: [severity=~"page|ticket"]
    equal: [cluster, node]
```

要点：

- `group_by` 决定告警合并；按 cluster/service 分组通常合理。
- `repeat_interval` 太长会漏提醒，太短会疲劳。
- 抑制规则避免“节点挂了，所有 Pod 都告警”。
- 静默（silence）要有到期时间与责任人，不能永久静音。

### 27.13.5 高可用与长期存储

Prometheus 单机不是高可用：

- 两个副本各自采集，查询时去重/聚合；或使用 Thanos/Mimir/VictoriaMetrics。
- 本地存储有 retention 上限；长期数据需要远程写。
- 远程写失败要有队列/磁盘缓冲，避免丢数据或阻塞采集。
- 查询层要限制大范围查询，避免打爆存储。

容量估算：

```text
每日样本数 ≈ 时间序列数 × 86400 / scrape_interval
存储 ≈ 每日样本数 × 每样本字节数 × retention 天数 × 副本数
```

时间序列数由“指标名 × 标签组合”决定；Pod 名、URL、用户 ID 做标签会迅速爆炸。

### 27.13.6 OTel Operator 与采集侧治理

OpenTelemetry Operator 可以注入/管理 Collector：

```yaml
apiVersion: opentelemetry.io/v1beta1
kind: OpenTelemetryCollector
metadata:
  name: otel
spec:
  mode: daemonset
  config:
    receivers:
      otlp:
        protocols: {grpc: {}, http: {}}
    processors:
      memory_limiter:
        check_interval: 1s
        limit_percentage: 80
      k8sattributes: {}
      batch: {}
    exporters:
      otlp:
        endpoint: otel-gateway.observability:4317
    service:
      pipelines:
        traces:
          receivers: [otlp]
          processors: [memory_limiter, k8sattributes, batch]
          exporters: [otlp]
```

治理点：

- DaemonSet 采集节点日志/指标，Deployment 做网关聚合。
- `memory_limiter` 防止 Collector OOM。
- `k8sattributes` 补全 namespace/pod/node，便于关联。
- 在采集侧丢弃/脱敏，而不是全部送到后端。
- Collector 自身要监控：队列长度、丢弃数、导出失败。

### 27.13.7 审计日志与事件

- Kubernetes Event 默认只保留短时间（etcd TTL），需要事件导出器（如 `kubernetes-event-exporter`）落到日志/存储。
- 审计日志是安全调查的关键，不应只存在控制面本地文件（见 23.10）。
- 事件/审计/指标/日志的时间戳要统一（NTP），否则关联会错位。

## 27.14 持续 profiling 与 eBPF 自动埋点

指标告诉你“慢了”，日志告诉你“哪次请求”，追踪告诉你“哪个调用”，但**为什么慢**往往要看到函数级 CPU/内存/锁竞争。持续 profiling 把采样式剖析变成常驻能力：

- **Pyroscope/Grafana Alloy**：按服务/标签聚合 CPU、内存、goroutine、锁 profile，支持差异对比（部署前后）。
- **Parca/Polar Signals**：eBPF 或语言级采样，适合多语言集群。
- **Java/JVM**：async-profiler/JFR；Go：pprof；Python：py-spy。
- **eBPF 自动埋点**：Pixie/OTel eBPF、Cilium Tetragon 可以在不改代码的情况下拿到 HTTP/gRPC 延迟、连接、系统调用；适合遗留服务和故障取证。

落地原则：

1. **采样而非全量**：持续 profiling 默认低采样率，故障时再提高；避免 CPU/存储开销。
2. **标签治理**：service/namespace/pod/version 必须一致，否则无法和指标/追踪关联。
3. **隐私与安全**：profile 可能包含函数名、参数、路径；限制访问与保留期。
4. **与发布关联**：把 profile 的版本标签与 Git SHA 对齐，才能做“这次发布变慢了”的对比。
5. **eBPF 的内核/权限前提**：内核版本、BTF、特权/能力；云托管环境可能限制。

## 本章小结

- 可观测性 = 指标 + 日志 + 追踪 + 事件 + 变更上下文。
- 指标适合告警，日志适合细节，追踪适合跨服务，事件适合 Kubernetes 状态。
- 基数和日志量是主要成本来源。
- SLO/错误预算让告警围绕用户体验，而不是堆指标。
- 统一语义标签是数据关联的前提。
- OTel Collector 负责采集/处理/导出；memory_limiter、k8sattributes、尾部采样和数据量治理是生产关键。
- 生产栈要分清 kube-state-metrics/node-exporter/cAdvisor 的职责，告警围绕症状与 SLO，Alertmanager 做分组/抑制，长期存储用远程写。

## 延伸阅读

- [第 30 章 性能与规模化](30-performance-scale.md)
- [第 31 章 故障排查方法论](31-troubleshooting.md)
- [第 39 章 测试策略](39-testing.md)
- [第 49 章 应用工程](49-application-engineering.md)：应用侧 RED/USE、日志/trace 契约与 SLO。
- [第 50 章 事件驱动、工作流与数据管道](50-event-driven-workflows.md)：lag、DLQ、异步 trace 与事件平台 SLO。

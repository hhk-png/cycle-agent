# 第 24 章 Kubernetes 监控栈

## 24.1 组件全景

```text
kube-apiserver ─┐
etcd ───────────┤
kubelet/cAdvisor├─► Prometheus ─► Alertmanager
node-exporter ──┤        │
kube-state-metrics ─┘    └─► Grafana / 远端存储
应用 /metrics ──┘
```

- **kube-state-metrics (KSM)**：Kubernetes 对象状态（Deployment/ Pod/Job/PVC 等）。
- **node-exporter**：节点 OS 指标（CPU/内存/磁盘/网络）。
- **cAdvisor**（kubelet 内置）：容器资源使用。
- **kubelet**：节点/容器运行时指标。
- **apiserver/etcd/scheduler/controller-manager**：控制面指标。
- **应用**：业务指标 + RED/USE。

## 24.2 Prometheus Operator

Operator 把 Prometheus 配置抽象成 CRD：

- `Prometheus`：Prometheus 实例；
- `ServiceMonitor`：按 Service 选择抓取目标；
- `PodMonitor`：按 Pod 选择；
- `Probe`：黑盒探测；
- `PrometheusRule`：规则；
- `Alertmanager`：Alertmanager 实例；
- `ThanosRuler`：长期规则求值。

优点：

- 声明式管理；
- 自动生成 scrape 配置；
- 规则/告警与 GitOps 集成；
- 多实例/分片支持。

Operator 的协调循环、分片/副本区别、Admission Webhook、升级顺序和排障路径见附录 P。

## 24.3 ServiceMonitor 示例

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: api
  namespace: monitoring
  labels:
    release: kube-prometheus-stack
spec:
  namespaceSelector:
    matchNames: ["app"]
  selector:
    matchLabels:
      app: api
  endpoints:
    - port: metrics
      path: /metrics
      interval: 30s
      scrapeTimeout: 10s
      relabelings:
        - sourceLabels: [__meta_kubernetes_pod_name]
          targetLabel: pod
      metricRelabelings:
        - sourceLabels: [__name__]
          regex: "go_.*"
          action: drop
```

要点：

- `selector` 选 Service，不是 Pod；
- `endpoints[].port` 是 Service 端口名；
- `relabelings` 作用于目标，`metricRelabelings` 作用于样本；
- namespaceSelector 决定发现范围。

## 24.4 PodMonitor 示例

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata:
  name: batch
  namespace: monitoring
spec:
  selector:
    matchLabels:
      app: batch
  podMetricsEndpoints:
    - port: metrics
      interval: 60s
```

适合没有 Service 的 Pod（Job、DaemonSet、临时任务）。

## 24.5 PrometheusRule 示例

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: api-rules
  namespace: monitoring
  labels:
    release: kube-prometheus-stack
spec:
  groups:
    - name: api
      interval: 30s
      rules:
        - record: job:http_requests:rate5m
          expr: sum by (job) (rate(http_requests_total[5m]))
        - alert: APIHighErrorRatio
          expr: |
            sum by (job) (rate(http_requests_total{code=~"5.."}[5m]))
            /
            sum by (job) (rate(http_requests_total[5m])) > 0.05
          for: 10m
          labels:
            severity: page
          annotations:
            summary: "{{ $labels.job }} 5xx > 5%"
            runbook_url: "https://runbooks.example/api"
```

Operator 会把 `spec.groups` 渲染成规则文件并 reload Prometheus。

## 24.6 关键指标

节点：

```promql
node_cpu_seconds_total
node_memory_MemAvailable_bytes
node_filesystem_avail_bytes
node_network_receive_bytes_total
node_load1
```

容器：

```promql
container_cpu_usage_seconds_total
container_memory_working_set_bytes
container_network_receive_bytes_total
container_fs_usage_bytes
```

KSM：

```promql
kube_pod_status_phase
kube_deployment_status_replicas_available
kube_job_status_failed
kube_persistentvolumeclaim_status_phase
kube_node_status_condition
```

控制面：

```promql
apiserver_request_duration_seconds_bucket
apiserver_request_total
etcd_server_leader_changes_seen_total
etcd_disk_wal_fsync_duration_seconds_bucket
scheduler_scheduling_attempt_duration_seconds_bucket
```

## 24.7 常用告警

```promql
# 节点不可用
up{job="node-exporter"} == 0

# Pod CrashLoopBackOff
kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"} > 0

# Deployment 副本不足
kube_deployment_status_replicas_available
  < kube_deployment_spec_replicas

# PVC 使用率高
kubelet_volume_stats_used_bytes
  / kubelet_volume_stats_capacity_bytes > 0.85

# API Server 错误率高
sum(rate(apiserver_request_total{code=~"5.."}[5m]))
/
sum(rate(apiserver_request_total[5m])) > 0.05
```

## 24.8 资源规划

Prometheus 自身：

- 2～4 CPU、8～16 GB 内存起步（取决于序列数）；
- SSD；
- memory limit 留 20～30% 余量；
- 反亲和/多副本。

KSM：

- 大集群（10k+ Pod）需要更多内存/分片；
- 用 `--resources` 限制指标；
- 监控 `kube_state_metrics_list_total` 延迟。

node-exporter：

- DaemonSet，每节点一个；
- hostNetwork/hostPID；
- 只读挂载 `/proc`、`/sys`。

## 24.9 多集群

模式：

1. **每集群 Prometheus + Thanos 全局查询**：最常见；
2. **中心 Prometheus 联邦**：只抓聚合指标；
3. **Mimir 多租户**：每集群/团队一个租户；
4. **Agent + 中心存储**：边缘轻量。

external labels：

```yaml
global:
  external_labels:
    cluster: prod-cn
    region: cn-north
```

用于区分集群，也用于远端去重。

## 24.10 常见问题

- **ServiceMonitor 不生效**：selector/labels 不匹配 Operator 的 `serviceMonitorSelector`。
- **指标重复**：ServiceMonitor 和 PodMonitor 同时选中同一 Pod。
- **KSM 指标缺失**：RBAC 权限不足或 resource 未启用。
- **cAdvisor 指标名变化**：Kubernetes 版本/容器运行时差异。
- **etcd 监控缺失**：需要单独配置证书/端点。
- **高基数**：Pod 名/容器名/镜像 tag 进入标签。
- **Prometheus OOM**：KSM + 应用指标 + 高基数标签叠加。

## 24.11 Mini 的定位

Mini 不实现 Kubernetes SD/Operator，但：

- relabel 语义与 Operator 生成的一致；
- 可以用 file_sd 模拟 K8s 目标；
- 第 11 章给出 K8s SD 配置；
- 第 39 章用 file_sd 演示 relabel。

学习时可以用 Mini 跑通 PromQL/告警，再在真实集群用 Operator 部署。

## 24.12 关键指标分层

按"集群 → 节点 → 工作负载 → 容器 → 应用"分层：

```text
集群：节点数/容量/调度失败/Pending Pod
节点：CPU/内存/磁盘/网络/文件系统
工作负载：副本数/可用副本/滚动更新/Job 成功
容器：CPU throttling/OOM/重启/资源使用
应用：请求率/错误率/延迟/饱和度
```

排查顺序：

- 应用 SLO 异常 → 先看应用 RED；
- 再看容器资源/重启；
- 再看节点压力；
- 再看调度/控制面；
- 最后看集群容量。

不要一上来就看节点 CPU；大多数应用故障不是节点 CPU 导致的。

## 24.13 KSM 指标与陷阱

常用：

```promql
# 副本不足
kube_deployment_status_replicas_available
  < kube_deployment_spec_replicas

# 容器重启
increase(kube_pod_container_status_restarts_total[1h]) > 3

# Pod 未就绪
kube_pod_status_ready{condition="true"} == 0

# Job 失败
kube_job_status_failed > 0

# PVC 使用率（需要 kubelet_volume_stats）
kubelet_volume_stats_used_bytes
  / kubelet_volume_stats_capacity_bytes > 0.85
```

陷阱：

- KSM 指标是"对象状态"，不是"使用量"；资源使用看 cAdvisor/node-exporter；
- `kube_pod_*` 系列按 Pod 名产生高基数，Pod 频繁重建会推高序列数；
- 不同 K8s 版本指标名/标签可能变化；
- KSM 需要 RBAC 读所有对象，权限要最小化；
- KSM 自身可能成为瓶颈（大集群）；
- `kube_pod_container_status_restarts_total` 是 counter，重启后可能重置（取决于 KSM 版本/状态）。

## 24.14 node-exporter 与 cAdvisor

node-exporter：

```promql
# CPU 使用率（按 mode）
1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m]))

# 内存使用率
1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes

# 文件系统
1 - node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"}
  / node_filesystem_size_bytes

# 网络
rate(node_network_receive_bytes_total{device!="lo"}[5m])
```

cAdvisor（kubelet）：

```promql
# 容器 CPU throttling
rate(container_cpu_cfs_throttled_seconds_total[5m]) > 0

# 容器内存工作集
container_memory_working_set_bytes

# OOM
increase(container_oom_events_total[1h]) > 0

# 容器重启（kubelet 指标）
increase(kubelet_container_log_filesystem_used_bytes[1h])
```

注意：

- cAdvisor 指标名/标签随 K8s/CRI 变化；
- `container_*` 包含 pause 容器/系统容器，查询要过滤；
- CPU throttling 比 CPU 使用率更能反映"被限流"；
- 内存要看 working set，不是 RSS；
- 节点文件系统要排除 tmpfs/overlay。

## 24.15 控制面监控

apiserver：

```promql
# 请求延迟
histogram_quantile(0.99,
  sum by (verb, le) (rate(apiserver_request_duration_seconds_bucket[5m]))
)

# 错误率
sum(rate(apiserver_request_total{code=~"5.."}[5m]))
/
sum(rate(apiserver_request_total[5m]))

# 429 限流
sum(rate(apiserver_request_total{code="429"}[5m])) > 0
```

etcd：

```promql
etcd_server_leader_changes_seen_total
etcd_disk_wal_fsync_duration_seconds
etcd_disk_backend_commit_duration_seconds
etcd_server_proposals_failed_total
```

scheduler/controller-manager：

```promql
scheduler_pending_pods
scheduler_schedule_attempts_total{result="error"}
controller_runtime_reconcile_errors_total
```

注意：

- 控制面指标通常需要 TLS 客户端证书/授权；
- 不同发行版（EKS/GKE/AKS/自建）暴露方式不同；
- 托管控制面可能不暴露 etcd/scheduler；
- 控制面指标基数低，但要防止 label 爆炸。

## 24.16 多集群与租户

方案：

```text
每集群一个 Prometheus + external_labels.cluster
  → Thanos/Mimir 全局查询
或
每集群 Agent → 中心 remote write
或
中心联邦（只聚合）
```

要点：

- `cluster`/`region`/`tenant` 作为 external label，不要在每个指标上重复；
- 租户隔离用独立实例/命名空间/Mimir 租户；
- 查询层按 cluster/tenant 过滤；
- 告警路由按 cluster/team；
- 多集群的时钟/NTP 要同步；
- 跨集群查询注意延迟/部分响应。

## 24.17 容量与成本

K8s 监控的基数来源：

```text
Pod 名/容器名/镜像 tag/UID
KSM 的对象状态
cAdvisor 的容器/网络/文件系统
控制面/CRD
```

治理：

- 用 `pod` 前缀/正则规范化；
- 删除不需要的 KSM resource；
- 限制 cAdvisor 指标；
- 用 recording rules 聚合；
- 按 namespace/team 分片；
- 监控 `prometheus_tsdb_head_series` 与 KSM 自身指标。

成本：

- KSM 指标多、更新频繁；
- cAdvisor 每容器多序列；
- 控制面指标少但重要；
- 多集群 × 多副本放大。

## 24.18 排障清单

```text
[ ] ServiceMonitor/PodMonitor selector 是否匹配
[ ] Operator 的 serviceMonitorSelector/namespaceSelector
[ ] RBAC 是否允许 KSM/Prometheus 读对象
[ ] 目标端口名/路径/scheme 是否正确
[ ] 是否重复抓取（Service + Pod）
[ ] KSM/cAdvisor 指标名是否随版本变化
[ ] 高基数标签是否来自 Pod/镜像/UID
[ ] 控制面证书/端点是否可达
[ ] 多集群 external labels 是否一致
[ ] 资源/容量是否足够
```

## 本章小结

- K8s 监控栈 = Prometheus + KSM + node-exporter + cAdvisor + 控制面指标 + 应用指标。
- Operator 用 ServiceMonitor/PodMonitor/PrometheusRule 声明式管理。
- 关键指标按节点/容器/对象/控制面分层。
- 多集群用 external labels + Thanos/Mimir/agent。
- 高基数和 RBAC 是最常见的问题源。

## 延伸阅读

- prometheus-operator 文档与 kube-prometheus-stack。
- Kubernetes 监控最佳实践（SIG Instrumentation）。
- 本教程第 11 章（K8s SD）、第 27 章（基数）、附录 M（Grafana 可视化）、附录 P（Operator 深入）与附录 AA（node_exporter/blackbox 的指标与告警）。

# 附录 P Prometheus Operator 与 Kubernetes 生产实践

第 24 章给出了 Kubernetes 监控栈的全景；本附录把 Prometheus Operator 单独拆开，讲清它到底生成了什么、为什么 ServiceMonitor 不生效、分片和副本有什么区别、升级时哪些东西会坏。目标不是背 CRD 字段，而是能独立评审一套 `kube-prometheus-stack` 的生产配置。

## P.1 Operator 解决的不是"安装"

手工部署 Prometheus 需要维护：

- `prometheus.yml` 的抓取配置；
- 规则文件与 reload；
- RBAC、ServiceAccount、Service；
- 多实例/分片/副本；
- 配置变更的滚动与回滚；
- 多团队自助接入。

Operator 把这些变成 Kubernetes 自定义资源（CRD）和协调循环（reconcile）：

```text
ServiceMonitor/PodMonitor/Probe/ScrapeConfig
        + PrometheusRule + AlertmanagerConfig
                        ↓
              Operator reconcile
                        ↓
        生成 prometheus.yml / rules / alertmanager.yml
                        ↓
       挂载到 Pod + config-reloader 触发 /-/reload
```

它不存储指标，也不替代 Prometheus；它只是"把配置变成声明式资源"。指标仍然由 Prometheus 自己抓取、存储、查询。

## P.2 CRD 全景

| CRD | 作用 | 典型使用方 |
|---|---|---|
| `Prometheus` | 一个 Prometheus 实例/集群 | 平台团队 |
| `PrometheusAgent` | agent 模式实例（只抓取 + remote write） | 边缘/大规模 |
| `Alertmanager` | Alertmanager 实例/集群 | 平台团队 |
| `ThanosRuler` | 长期规则求值 | 多集群/长期存储 |
| `ServiceMonitor` | 按 Service 选择抓取目标 | 应用团队 |
| `PodMonitor` | 按 Pod 选择抓取目标 | 应用/批处理 |
| `Probe` | 黑盒探测（blackbox exporter） | SRE |
| `ScrapeConfig` | 原生 scrape_config 的 CRD 封装（Beta，`v1beta1`） | 平台/特殊场景 |
| `PrometheusRule` | recording/alerting 规则 | 应用/SRE |
| `AlertmanagerConfig` | 命名空间级路由/接收器/抑制 | 应用团队 |

选择原则：

- 有 Service 的长期服务用 `ServiceMonitor`；
- 没有 Service 的 Pod（Job、DaemonSet、临时任务）用 `PodMonitor`；
- 需要从外部探测 URL/端口用 `Probe`；
- 需要 `kubernetes_sd_configs` 之外的原生配置（如 EC2、Consul、特殊 relabel）用 `ScrapeConfig` 或 `additionalScrapeConfigs`；
- 团队自助告警路由用 `AlertmanagerConfig`，平台保留全局兜底路由。

## P.3 协调循环与配置生成

一次配置变更的路径：

1. 用户 `kubectl apply` 一个 `ServiceMonitor`；
2. Operator 的 informer 收到事件，把相关 `Prometheus` 加入队列；
3. reconcile 读取 `Prometheus.spec.serviceMonitorSelector`/`serviceMonitorNamespaceSelector`，筛选匹配的 `ServiceMonitor`；
4. 把每个 endpoint 转换成 `scrape_configs` 条目，注入 `job_name`、`kubernetes_sd_configs`、relabel；
5. 生成 `prometheus.yml`，写入 Secret/ConfigMap（具体资源类型随版本变化）；
6. Pod 挂载配置，`config-reloader` sidecar 检测到文件变化，调用 Prometheus 的 `/-/reload`；
7. Prometheus 校验配置；失败则保留旧配置，`prometheus_config_last_reload_successful=0`。

关键点：

- Operator 只负责"生成配置"，真正的 reload 由 sidecar 或 Prometheus 自己完成；
- reload 失败不会自动回滚 CR，只会让 Prometheus 保持旧配置；要看 Operator 日志和 `status.conditions`；
- 规则文件通常单独生成（`PrometheusRule` → 规则 ConfigMap/Secret），与主配置分开挂载；
- 大规模环境下，ConfigMap/Secret 大小、reload 频率、生成耗时都会成为瓶颈。

## P.4 Prometheus CR 的关键字段

一个最小但可用的 `Prometheus`：

```yaml
apiVersion: monitoring.coreos.com/v1
kind: Prometheus
metadata:
  name: main
  namespace: monitoring
spec:
  replicas: 2
  retention: 15d
  retentionSize: 400GB
  scrapeInterval: 30s
  evaluationInterval: 30s
  serviceAccountName: prometheus
  serviceMonitorSelector:
    matchLabels:
      release: kube-prometheus-stack
  serviceMonitorNamespaceSelector: {}
  podMonitorSelector:
    matchLabels:
      release: kube-prometheus-stack
  ruleSelector:
    matchLabels:
      release: kube-prometheus-stack
  resources:
    requests:
      cpu: 2
      memory: 8Gi
    limits:
      memory: 16Gi
  storage:
    volumeClaimTemplate:
      spec:
        storageClassName: fast-ssd
        resources:
          requests:
            storage: 500Gi
  securityContext:
    runAsNonRoot: true
    fsGroup: 2000
  externalLabels:
    cluster: prod-cn
    region: cn-north
```

字段分组：

| 分组 | 字段 | 说明 |
|---|---|---|
| 规模 | `replicas`、`shards` | 副本数 × 分片数 = Pod 总数 |
| 存储 | `retention`、`retentionSize`、`storage`、`walCompression` | 本地 TSDB |
| 抓取 | `scrapeInterval`、`scrapeTimeout`、`scrapeProtocols` | 全局默认 |
| 规则 | `evaluationInterval`、`ruleSelector`、`ruleNamespaceSelector` | 规则发现 |
| 选择器 | `serviceMonitorSelector`、`podMonitorSelector`、`probeSelector`、`scrapeConfigSelector` | 决定"哪些 CR 被这个 Prometheus 采纳" |
| 远端 | `remoteWrite`、`remoteRead` | 长期存储 |
| 告警 | `alerting.alertmanagers` | Alertmanager 端点 |
| Thanos | `thanos` | sidecar/对象存储 |
| 限制 | `enforcedSampleLimit`、`enforcedTargetLimit`、`enforcedLabelLimit` | 平台级兜底 |
| 安全 | `securityContext`、`podMetadata`、`priorityClassName`、`affinity` | 生产调度 |

`serviceMonitorSelector` 为空对象 `{}` 表示"选择所有"，不是"不选择"；这是最常见的误配之一。命名空间选择器同理：`{}` 表示所有命名空间。

## P.5 ServiceMonitor、PodMonitor、Probe、ScrapeConfig

### ServiceMonitor

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
      honorLabels: false
      relabelings:
        - sourceLabels: [__meta_kubernetes_pod_name]
          targetLabel: pod
      metricRelabelings:
        - sourceLabels: [__name__]
          regex: "go_.*"
          action: drop
```

工作方式：

- `selector` 选 **Service**，不是 Pod；
- `namespaceSelector` 决定在哪些命名空间找 Service；
- `endpoints[].port` 是 Service 的**端口名**，不是端口号；
- Operator 最终通过 `kubernetes_sd_configs: role: endpoints` 发现 Endpoint，再 relabel 成目标；
- `relabelings` 作用于目标标签，`metricRelabelings` 作用于样本标签。

不生效的常见原因：

1. `ServiceMonitor` 的 labels 不匹配 `Prometheus.spec.serviceMonitorSelector`；
2. `namespaceSelector` 没包含 Service 所在命名空间；
3. Service 没有同名端口，或端口没有 `name`；
4. Service 的 selector 选不到 Pod；
5. Pod 没有 ready（Endpoint 为空）；
6. 另一个 `ServiceMonitor`/`PodMonitor` 已经抓了同一目标，导致重复或冲突；
7. Operator 没有 RBAC 读取该命名空间的 CR。

### PodMonitor

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
      filterRunning: true
```

适合没有 Service 的 Pod。`filterRunning: true` 会跳过非 Running 的 Pod，避免抓到终止中的容器。

### Probe

```yaml
apiVersion: monitoring.coreos.com/v1
kind: Probe
metadata:
  name: api-blackbox
  namespace: monitoring
spec:
  jobName: blackbox-api
  prober:
    url: blackbox-exporter.monitoring.svc:9115
  module: http_2xx
  targets:
    staticConfig:
      static:
        - https://api.example.com/health
      labels:
        env: prod
  interval: 30s
  scrapeTimeout: 10s
```

Operator 把 `targets` 转成 `blackbox_exporter` 的 `target` 参数；Probe 的分片哈希默认使用 `__param_target`，而不是 `__address__`。

### ScrapeConfig

`ScrapeConfig` 是较新的 Beta CRD（`v1beta1`），用来表达 `kubernetes_sd_configs` 之外的抓取配置（EC2、Consul、file_sd、特殊 relabel）。它的字段接近原生 `scrape_config`，但受 Operator 的选择器和分片注入影响。生产使用前要确认：

- CRD 版本与 Operator 版本匹配；
- 是否被 `spec.scrapeConfigSelector` 选中；
- 分片注入是否与已有 `hashmod` 冲突（ServiceMonitor/PodMonitor 总是注入，ScrapeConfig/additionalScrapeConfigs 在已有 hashmod 时会跳过）。

## P.6 副本与分片：两个完全不同的概念

```text
spec.replicas: 2   # 每个 shard 内 2 个副本，抓同样的目标（HA）
spec.shards: 4     # 4 个 shard，每个抓 1/4 的目标（水平扩展）
总 Pod 数 = replicas × shards = 8
```

- **副本**：解决高可用。两个副本抓同一批目标，写入相同远端；查询层按 `replica` 标签去重。
- **分片**：解决抓取容量。每个 shard 只抓一部分目标；查询需要全局层（Thanos/Mimir）合并。

Operator 的分片实现：

```yaml
# 生成的配置大致等价于
- source_labels: [__address__]
  modulus: 4
  target_label: __tmp_hash
  action: hashmod
- source_labels: [__tmp_hash]
  regex: "0"          # 每个 shard 不同
  action: keep
```

要点：

- `ServiceMonitor`/`PodMonitor` 总是注入 `hashmod → __tmp_hash`，再按 shard 保留；
- `Probe` 默认哈希 `__param_target`；
- 可以在 `relabelings` 里自己设置 `__tmp_hash`，覆盖默认哈希键；
- `__tmp_disable_sharding` 设为非空可让所有 shard 都抓该目标（例如 kube-state-metrics 这类单例）；
- 分片变化会导致目标迁移，出现短暂重复/缺口；要配合远端去重和查询层；
- `replicas` 变化不会改变目标分配，只改变副本数。

常见错误：

- 把 `shards` 当 HA 用：分片不是副本，单 shard 故障会丢一部分目标；
- 把 `replicas` 当扩展用：副本抓同样的目标，不会提升抓取容量；
- 分片后没有全局查询：Grafana 只能看到部分数据；
- external labels 在副本间不一致：远端无法去重。

## P.7 PrometheusRule 与 AlertmanagerConfig

### PrometheusRule

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

- `spec.groups` 与原生规则文件一致；
- Operator 把匹配 `ruleSelector` 的规则合并成规则文件；
- 规则表达式错误会被 admission webhook 或 Prometheus reload 拒绝；
- 规则变更会触发 reload，但不会重置整个 Prometheus；告警状态可能因规则重建而重置。

### AlertmanagerConfig

```yaml
apiVersion: monitoring.coreos.com/v1
kind: AlertmanagerConfig
metadata:
  name: team-api
  namespace: app
spec:
  route:
    receiver: team-api-slack
    groupBy: [alertname, cluster, service]
    routes:
      - matchers:
          - name: severity
            value: critical
        receiver: team-api-pager
        continue: false
  receivers:
    - name: team-api-slack
      slackConfigs:
        - apiURL:
            name: slack-webhook
            key: url
          channel: "#api-alerts"
    - name: team-api-pager
      pagerdutyConfigs:
        - routingKey:
            name: pagerduty
            key: routingKey
  inhibitRules:
    - sourceMatchers:
        - name: alertname
          value: ServiceDown
      targetMatchers:
        - name: severity
          value: warning
      equal: [service]
```

要点：

- `AlertmanagerConfig` 是命名空间级资源，需要 `Alertmanager.spec.alertmanagerConfigSelector` 和 `alertmanagerConfigNamespaceSelector` 选中；
- 路由会与全局 `route` 合并，注意 `continue` 和匹配顺序；
- secret 引用用 `name`/`key`，不要把 webhook URL 明文写进 CR；
- 团队自助路由要有平台兜底，避免告警无人接收；
- 变更前用 `amtool check-config` 或 Operator 的校验。

## P.8 Admission Webhook 与校验

Operator 通常部署 admission webhook，用于：

- 校验 `PrometheusRule` 的 PromQL 语法与规则结构；
- 校验 `ServiceMonitor`/`PodMonitor` 的端口、relabel、TLS 等字段；
- 拒绝非法的 `AlertmanagerConfig` 路由/接收器；
- 在启用实验函数时，校验规则是否使用了未开启的函数。

它不能替代：

- `promtool check config` 对完整配置的校验；
- `promtool test rules` 对规则行为的测试；
- 真实数据上的查询/告警演练。

如果 webhook 不可用，创建/更新 CR 可能失败；升级 Operator 时要先确认 webhook 证书和 Service 正常。

## P.9 kube-prometheus-stack 的关键 values

Helm chart `kube-prometheus-stack` 把 Operator、Prometheus、Alertmanager、Grafana、node-exporter、kube-state-metrics 和默认规则打包在一起。生产评审重点：

```yaml
prometheus:
  prometheusSpec:
    replicas: 2
    retention: 15d
    retentionSize: 400GB
    resources:
      requests: {cpu: 2, memory: 8Gi}
      limits: {memory: 16Gi}
    storageSpec:
      volumeClaimTemplate:
        spec:
          storageClassName: fast-ssd
          resources:
            requests:
              storage: 500Gi
    serviceMonitorSelectorNilUsesHelmValues: false
    podMonitorSelectorNilUsesHelmValues: false
    ruleSelectorNilUsesHelmValues: false
    externalLabels:
      cluster: prod-cn
    remoteWrite:
      - url: https://mimir.example/api/v1/push
        headers:
          X-Scope-OrgID: platform

alertmanager:
  enabled: true
  alertmanagerSpec:
    replicas: 3
    storage:
      volumeClaimTemplate:
        spec:
          storageClassName: fast-ssd
          resources:
            requests:
              storage: 10Gi

grafana:
  enabled: true
  persistence:
    enabled: true
    size: 10Gi

nodeExporter:
  enabled: true

kubeStateMetrics:
  enabled: true

defaultRules:
  create: true
```

最容易踩的 values：

- `serviceMonitorSelectorNilUsesHelmValues`：默认 `true` 时，Prometheus 只选择带 chart release 标签的 ServiceMonitor；团队自建的 ServiceMonitor 不加标签就不生效。设为 `false` 会选所有，需配合命名空间隔离。
- `prometheus.prometheusSpec.replicas` 与 `shards` 混淆：副本不是分片。
- `storageSpec` 不配：使用 `emptyDir`，Pod 重启丢历史。
- `resources.limits` 只设 CPU 不设内存：OOM 风险；只设内存不设 CPU：节流。
- `externalLabels` 每个集群必须唯一且稳定；变更会影响 remote write 去重和告警身份。
- `defaultRules.create`：默认规则可能产生大量告警，生产要按需裁剪。

## P.10 升级与兼容

升级顺序建议：

1. 备份 Prometheus/Alertmanager 的 CR、PVC、Grafana dashboard；
2. 先升级 CRD（`kubectl apply --server-side`），再升级 Operator；
3. 再升级 Prometheus/Alertmanager 镜像；
4. 观察 `status.conditions`、reload 成功率、抓取/查询/告警；
5. 最后清理旧 CRD 版本/废弃字段。

高风险点：

- CRD 版本 skew：Operator 与 CRD 不匹配会导致 reconcile 失败；
- `Prometheus` 镜像大版本升级：PromQL 行为、flag、TSDB 格式可能变化；
- `config-reloader` 镜像与 Operator 版本不一致：reload 失败；
- webhook 证书过期/Service 不通：CR 更新失败；
- `ScrapeConfig` Beta（`v1beta1`）：字段仍可能变化；
- Helm chart 大版本升级：values 结构、默认规则、Grafana 版本都会变。

回滚：

- 保留旧 Operator/CRD/镜像版本；
- 回滚 CR 前确认新版本写入的 TSDB 是否能被旧版本读取；
- 规则/告警回滚要同时回滚 `PrometheusRule` 和 `AlertmanagerConfig`；
- 升级窗口内避免同时改抓取配置和存储配置。

## P.11 故障排查路径

按"CR → Operator → 生成配置 → Prometheus"四层排查：

```bash
# 1. CR 是否存在/被选中
kubectl get servicemonitor -n app api -o yaml
kubectl get prometheus -n monitoring main -o yaml | yq '.spec.serviceMonitorSelector'

# 2. Operator 是否报错
kubectl -n monitoring logs deploy/prometheus-operator --tail=200
kubectl -n monitoring get prometheus main -o yaml | yq '.status'

# 3. 生成配置是否包含目标
kubectl -n monitoring get secret prometheus-main -o jsonpath='{.data.prometheus\.yaml\.gz}' \
  | base64 -d | gunzip | grep -A20 'job_name: app/api'

# 4. Prometheus 是否 reload 成功
kubectl -n monitoring logs prometheus-main-0 -c config-reloader --tail=100

# 5. 目标是否 up（port-forward 到本地再查）
kubectl -n monitoring port-forward svc/prometheus-main 9090:9090
curl -s localhost:9090/api/v1/targets | jq '.data.activeTargets[] | select(.health!="up")'
curl -s localhost:9090/api/v1/status/config | head
```

常见症状与原因：

| 症状 | 优先检查 |
|---|---|
| ServiceMonitor 创建了但无目标 | selector/labels/namespaceSelector/port name |
| 目标 up=0 | Service selector、Pod ready、network policy、路径/TLS |
| 配置里有目标但 Prometheus 没加载 | reload 失败、`prometheus_config_last_reload_successful` |
| 规则不生效 | `ruleSelector`、规则文件挂载、表达式错误 |
| 告警不发送 | `alerting.alertmanagers`、AlertmanagerConfig 选择器、路由 |
| 分片后数据缺失 | shard 数、全局查询层、external labels |
| 升级后 CR 更新失败 | CRD/Operator/webhook 版本 skew |

## P.12 生产检查清单

- [ ] `replicas` 与 `shards` 的语义已区分，容量/HA 目标明确；
- [ ] `serviceMonitorSelector`/`podMonitorSelector`/`ruleSelector` 不是"意外全选"；
- [ ] 每个 ServiceMonitor 的 `port` 与 Service 端口名一致；
- [ ] namespaceSelector 覆盖目标命名空间，但不越权；
- [ ] 存储用 PVC，不是 emptyDir；retention/retentionSize 已设置；
- [ ] resources requests/limits 合理，内存留 20%～30% 余量；
- [ ] `externalLabels` 每集群唯一且稳定；
- [ ] remote write 有队列/重试/去重配置；
- [ ] 规则有 `promtool test rules` 或 CI 校验；
- [ ] AlertmanagerConfig 有团队路由和平台兜底；
- [ ] admission webhook 正常，升级前验证证书；
- [ ] 备份/恢复演练过（CR、PVC、快照）；
- [ ] 监控 Operator 自身：reconcile 错误、reload 失败、CRD 版本；
- [ ] 升级顺序和回滚路径已文档化。

## 本章小结

- Operator 把 Prometheus 配置变成 CRD + 协调循环；它生成配置并触发 reload，不存储指标。
- ServiceMonitor/PodMonitor/Probe/ScrapeConfig 覆盖不同发现场景；selector、namespace、port name 是最常见故障点。
- `replicas` 是 HA，`shards` 是水平扩展；总 Pod 数 = 两者乘积，分片后必须有全局查询层。
- PrometheusRule 与 AlertmanagerConfig 让规则/路由自助化，但需要平台兜底和校验。
- 升级要按 CRD → Operator → 工作负载的顺序，webhook/CRD skew 是高风险点。
- 排查按"CR → Operator → 生成配置 → Prometheus"四层走，不要一上来就改 PromQL。

## 延伸阅读

- prometheus-operator 官方文档：Design、API Reference、Sharding、Troubleshooting。
- kube-prometheus-stack Helm chart values 与默认规则。
- 本教程第 11 章（服务发现/relabel）、第 24 章（Kubernetes 监控栈）、附录 B（配置参考）。

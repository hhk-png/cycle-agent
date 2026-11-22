# 第 20 章 自动伸缩：HPA、VPA、Cluster Autoscaler 与 KEDA

## 20.1 三个维度

自动伸缩可以发生在三层：

1. **Pod 数量（水平）**：HPA、KEDA 修改工作负载的 `replicas`。
2. **Pod 资源（垂直）**：VPA，调整 requests/limits。
3. **节点数量（集群）**：Cluster Autoscaler、Karpenter、云厂商节点池。

Karpenter **不修改 Pod 副本数**：它观察 Pending Pod 的调度需求，创建/整合节点。把它归到“Pod 水平扩缩”是常见混淆。

它们解决不同问题，不能互相替代：

- HPA 扩容后没有节点资源 → Pod Pending，需要节点层扩容。
- 节点层扩容但没有 Pending Pod → 浪费。
- VPA 调整 requests 可能触发 Pod 重建。
- KEDA 基于事件源扩缩，适合队列/消息。

## 20.2 HPA 基础

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: web
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: web
  minReplicas: 2
  maxReplicas: 20
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 60
```

HPA 控制器每 15 秒（默认）同步一次：

1. 读取目标对象的 scale 子资源。
2. 获取 Pod 指标（metrics-server/自定义指标）。
3. 计算期望副本数。
4. 通过 scale 子资源修改 replicas。

## 20.3 HPA 算法

以 CPU utilization 为例：

```text
currentUtilization = 所有 Pod 的实际 CPU 使用总和 / 所有 Pod 的 requests 总和 * 100
desiredReplicas = ceil(currentReplicas * currentUtilization / targetUtilization)
```

示例：

- 当前 2 个 Pod，requests 各 100m，实际各 100m → utilization 100%。
- target 50%。
- desired = ceil(2 * 100 / 50) = 4。

约束：

- 不小于 minReplicas，不大于 maxReplicas。
- 如果 metrics 缺失，保持当前副本数（或按 behavior 处理）。
- 有容差（默认 10%）：避免在目标附近频繁抖动。

真实 HPA 还处理：

- 多指标：取每个指标计算的最大 desired。
- 多个容器：requests 求和，usage 求和。
- 初始化/未 Ready Pod：可能被忽略或单独处理。
- stabilizationWindowSeconds：缩容冷却。
- scaleUp/scaleDown policies：限制每次变化速率。

## 20.4 扩容与缩容策略

```yaml
spec:
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 0
      policies:
        - type: Percent
          value: 100
          periodSeconds: 15
        - type: Pods
          value: 4
          periodSeconds: 15
      selectPolicy: Max
    scaleDown:
      stabilizationWindowSeconds: 300
      policies:
        - type: Percent
          value: 10
          periodSeconds: 60
```

含义：

- 扩容可以快（0 秒稳定窗口，最多翻倍/4 Pod）。
- 缩容要慢（5 分钟窗口，每次最多 10%）。
- `selectPolicy: Max` 选择允许变化最大的策略；`Min` 最保守。

原则：

- 扩容快，缩容慢。
- 避免抖动：目标利用率不要贴着实际负载。
- 对突发流量，预留 minReplicas 或使用 KEDA 预测。

### 20.4.1 容差、scale-to-zero 与指标回退

- **可配置容差（1.37 GA）**：HPA 默认只在指标偏离目标超过 10% 时调整；新版本允许按 HPA 设置容差，减少副本抖动。容差太小会频繁扩缩，太大则响应迟钝。
- **scale-to-zero（1.37 beta）**：对象/外部指标可以驱动副本到 0；CPU/内存指标通常不适合直接归零。到 0 后必须有人/事件把它拉起来，冷启动和请求缓冲要设计。
- **外部指标回退（1.36 beta）**：指标源故障时使用配置的回退值，避免 HPA 冻结在旧副本数。回退值是“保守值”，不是真实容量。
- **与原地扩缩容的交互**：HPA 改副本数，VPA 改规格；两者同时工作时用不同的指标维度（如 HPA 看 QPS，VPA 看 CPU/内存），并设置防抖窗口。

## 20.5 metrics-server 与自定义指标

HPA 需要指标来源：

- **Resource metrics**：CPU/内存，来自 metrics-server。
- **Custom metrics**：应用指标，通过 Prometheus Adapter、KEDA、Datadog 等暴露。
- **External metrics**：云队列、数据库连接数等。

metrics-server 只提供资源指标，不适合业务指标（QPS、队列长度）。

自定义指标需要实现 `custom.metrics.k8s.io` API，通常由 Prometheus Adapter 提供；外部指标走 `external.metrics.k8s.io`。

`autoscaling/v2` 支持五类指标：

```yaml
metrics:
  # 1. 资源利用率（最常见）
  - type: Resource
    resource:
      name: memory
      target: {type: Utilization, averageUtilization: 70}
  # 2. 单容器资源（多容器 Pod 中只针对某个容器）
  - type: ContainerResource
    containerResource:
      name: cpu
      container: app
      target: {type: Utilization, averageUtilization: 60}
  # 3. 每个 Pod 的平均自定义指标
  - type: Pods
    pods:
      metric: {name: requests_per_second}
      target: {type: AverageValue, averageValue: "100"}
  # 4. 某个对象的指标（如 Ingress 的 QPS）
  - type: Object
    object:
      describedObject: {apiVersion: networking.k8s.io/v1, kind: Ingress, name: web}
      metric: {name: requests_per_second}
      target: {type: Value, value: "1000"}
  # 5. 外部指标（云队列、数据库连接数）
  - type: External
    external:
      metric: {name: queue_depth, selector: {matchLabels: {queue: jobs}}}
      target: {type: AverageValue, averageValue: "30"}
```

`Utilization` 的分母是 requests；没有 requests 的 Pod 无法用利用率目标。多指标时 HPA 取**所需副本数的最大值**；某个指标不可用时的行为由 `behavior`/条件决定，不会静默忽略。

### 20.5.1 Prometheus Adapter：把业务指标接进 HPA

Prometheus Adapter 把 PromQL 查询映射为 custom/external metrics API。核心是规则：

```yaml
rules:
  - seriesQuery: 'http_requests_total{namespace!="",pod!=""}'
    resources:
      overrides:
        namespace: {resource: namespace}
        pod: {resource: pod}
    name:
      matches: "^(.*)_total$"
      as: "${1}_per_second"
    metricsQuery: 'sum(rate(<<.Series>>{<<.LabelMatchers>>}[2m])) by (<<.GroupBy>>)'
```

配置要点：

- `seriesQuery` 只选需要的序列，避免把高基数指标全部暴露。
- `metricsQuery` 聚合到 Pod/Service 维度；HPA 需要“每 Pod 平均值”时用 `Pods` 指标。
- 规则错误会导致 HPA 报 `FailedGetPodsMetric`，但 Pod 仍正常运行。
- 指标延迟/采样窗口要小于扩缩容决策周期，否则会滞后。
- 对队列长度这类外部指标，用 `External` 类型 + `external.metrics.k8s.io`。

## 20.6 VPA

Vertical Pod Autoscaler 调整 Pod 的 requests/limits：

```yaml
apiVersion: autoscaling.k8s.io/v1
kind: VerticalPodAutoscaler
metadata:
  name: web
spec:
  targetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: web
  updatePolicy:
    updateMode: Auto
```

VPA 由三个组件组成：

- **Recommender**：基于历史用量计算推荐值（lowerBound/target/upperBound）。
- **Updater**：根据 updatePolicy 决定何时驱逐/更新 Pod。
- **Admission Controller**：在 Pod 创建时写入推荐 requests（Initial 模式）。

组件结构：

```text
metrics → Recommender → VPA status.recommendation
                              │
                    Updater ──┴──> evict/update Pods
                              │
                 Admission ───┴──> set requests at create
```

模式：

| 模式 | 行为 | 适用 |
|---|---|---|
| `Off` | 只计算推荐，不修改 | 先观察，人工应用 |
| `Initial` | 只在 Pod 创建时设置推荐值 | 启动成本低、可重建 |
| `Recreate` | 自动更新，通过驱逐重建 Pod | 可接受重启 |
| `Auto` | 当前等价于 `Recreate`（未来可能变化） | 同 Recreate |
| `InPlaceOrRecreate` | 优先原地调整，失败再重建（beta，依赖原地扩缩容） | 延迟敏感、运行 1.35+ 且支持原地扩缩容的集群 |

更细的策略：

```yaml
spec:
  updatePolicy:
    updateMode: Auto
  resourcePolicy:
    containerPolicies:
      - containerName: "*"
        controlledValues: RequestsAndLimits   # 或 RequestsOnly
        minAllowed: {cpu: 100m, memory: 128Mi}
        maxAllowed: {cpu: "4", memory: 8Gi}
        mode: Auto
```

VPA 的挑战：

- 更新 requests 通常需要重建 Pod（1.35+ 可走原地扩缩容，但仍受运行时与资源类型限制）；重启会打断长连接/缓存。
- 与 HPA 同时使用可能冲突：HPA 基于 utilization，VPA 改 requests 会改变分母。推荐 HPA 用自定义指标（QPS/队列），VPA 管 CPU/内存。
- 推荐值需要历史数据，冷启动不准；突发流量会把推荐值推高。
- 不适合快速变化或强实时负载。
- 多容器 Pod 要按容器配置策略，不要让一个 sidecar 的历史用量污染主容器。

生产常用：

- 先 `Off` 观察 1–2 周，再人工应用或切 `Initial`。
- HPA 管副本，VPA 管 requests，但避免同一指标。
- 对延迟敏感服务，VPA 可能造成重启抖动；优先 `InPlaceOrRecreate`（1.35+）或人工发布。

## 20.7 Cluster Autoscaler

CA 监控 Pending Pod：

1. 发现因资源不足无法调度的 Pod。
2. 模拟在节点组扩容后是否能调度。
3. 调用云 API 增加节点。
4. 节点加入后调度器绑定 Pod。

缩容：

- 找到利用率低的节点。
- 检查 Pod 是否能迁移到其他节点。
- 驱逐 Pod（遵守 PDB）。
- 删除节点。

挑战：

- 扩容延迟（云厂商 1–5 分钟）。
- 节点组实例类型/可用区限制。
- 缩容被 PDB/本地存储/DaemonSet 阻止。
- 成本与调度策略冲突（bin-packing vs spread）。

## 20.8 Karpenter

Karpenter 直接创建节点，不依赖预定义节点组：

- 根据 Pending Pod 的资源/亲和性选择实例类型。
- 直接调用云 API。
- 支持 consolidation（合并低利用率节点）。
- 更灵活，但云厂商支持有限。

与 CA 对比：

- CA 基于节点组，配置简单但灵活性差。
- Karpenter 更动态，但需要云厂商集成和更高权限。

## 20.9 KEDA

KEDA 基于事件源扩缩：

- 队列长度（Kafka、RabbitMQ、SQS）。
- 数据库查询。
- Prometheus 指标。
- Cron 触发器。

KEDA 可以：

- 缩到 0（scale-to-zero）。
- 为 Deployment/StatefulSet 创建 HPA。
- 提供 ScaledObject/ScaledJob。

适合：

- 事件驱动、突发、空闲时无流量。
- 批处理/队列消费者。

不适合：

- 有状态、不能缩到 0 的服务。
- 事件源指标不可靠的场景。

### 20.9.1 一个 ScaledObject

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: orders-consumer
  namespace: shop
spec:
  scaleTargetRef:
    name: orders-consumer
  minReplicaCount: 0
  maxReplicaCount: 30
  cooldownPeriod: 300
  triggers:
    - type: kafka
      metadata:
        bootstrapServers: kafka:9092
        consumerGroup: orders
        topic: orders
        lagThreshold: "100"
    - type: prometheus
      metadata:
        serverAddress: http://prometheus:9090
        metricName: orders_queue_depth
        query: sum(orders_queue_depth{namespace="shop"})
        threshold: "50"
```

要点：

- 多触发器时，KEDA 按各自计算副本并取最大（与 HPA 类似）。
- `minReplicaCount: 0` 需要能处理冷启动；首次请求/消息会等待。
- `cooldownPeriod` 防止消息短暂清空就缩到 0。
- 消费者要幂等/可重试；缩容可能中断正在处理的消息。
- KEDA 生成 HPA，最终仍由 HPA 调整副本；排查时同时看 ScaledObject 与 HPA 状态。

## 20.10 自动伸缩的交互

```text
HPA 扩容 Pod
  → 节点资源不足，Pod Pending
  → Cluster Autoscaler 扩容节点
  → 调度器绑定
  → Pod 启动
```

问题：

- HPA 扩容太快，CA 跟不上，Pod 长时间 Pending。
- CA 缩容时，HPA 又扩容，震荡。
- VPA 调整 requests，HPA utilization 突变。
- KEDA 缩到 0，冷启动慢。

设计：

- 预留缓冲容量（buffer）。
- 设置 HPA 扩容策略与 CA 扩容阈值协调。
- 监控 Pending Pod 时长。
- 用 PDB 保护缩容。
- 对冷启动敏感的服务保留 minReplicas。

## 20.11 成本与性能

自动伸缩不是免费的：

- 频繁扩缩容导致镜像拉取、冷启动、连接重建。
- 节点扩容有最小计费单位（按秒/分钟）。
- 过度预留浪费；过度紧缩导致 SLO 违约。
- HPA 基于 CPU 可能滞后于真实瓶颈（I/O、连接池、下游限流）。

建议：

- 用业务指标（QPS、队列长度、延迟）而不是只用 CPU。
- 压测确定单 Pod 容量。
- 设置合理的 requests（HPA 的分母）。
- 监控扩容延迟和 Pending 时间。
- 用预算/配额限制最大成本。

GPU 推理/训练的扩缩容指标与 CPU 服务不同（队列长度、TTFT/ITL、显存），见第 22 章。

## 20.12 MiniK8s 的实现

`mini_k8s/controllers/autoscaling.py`：

- `MetricsController` 生成 PodMetrics/NodeMetrics（基于注解或 requests 的模拟值）。
- `HPAController` 计算每个指标的 desired，取最大值，写入目标 replicas。
- 支持 min/max、Resource/Pods 指标、10% 容差（避免目标附近抖动）、状态条件。

示例：

```bash
python -m mini_k8s.cli apply -f examples/12-hpa.yaml
python -m mini_k8s.cli get hpa
python -m mini_k8s.cli top pods
```

测试：

- `test_hpa_scales_deployment`
- `test_hpa_respects_max_replicas`

未实现：

- behavior 策略（扩容/缩容速率、selectPolicy）。
- 缩容稳定窗口（stabilizationWindowSeconds）。
- 自定义/外部指标适配器。
- 完整的 `ScalingLimited` 原因与事件。

## 20.13 容量模型与扩缩容演练

自动伸缩的配置不难，难的是“扩得出来、缩得安全”。建议用一个简单的容量模型把各层参数连起来。

### 从 SLO 反推容量

假设：

```text
峰值 QPS = 2000
单 Pod 安全容量 = 200 QPS（压测得出，留 30% 余量）
目标利用率 = 60%
所需 Pod = 2000 / (200 * 0.6) ≈ 17
```

再算节点：

```text
每 Pod requests = 500m CPU / 1Gi
17 Pod 需要 8.5 核 / 17Gi
每节点 allocatable = 8 核 / 30Gi（已扣除系统预留）
节点数 = ceil(max(8.5/8, 17/30)) = 2
```

如果 HPA `maxReplicas` 只有 10，peak 时必然过载；如果 CA 节点池上限只有 1，HPA 扩出的 Pod 会 Pending。**HPA、节点池、配额三者的上限必须一起算。**

### 扩容链路的时间预算

```text
指标采集（15s） + HPA 决策（15s） + 调度（<1s）
+ 节点扩容（60–300s） + 镜像拉取（10–120s） + 应用启动（10–60s）
```

端到端可能 2–8 分钟。对秒级突发，需要：

- 保留 `minReplicas` 缓冲。
- 预拉镜像/使用 P2P 分发。
- 使用 KEDA 的事件驱动/预测扩容。
- 入口限流/排队，避免扩容期间雪崩。

### 缩容安全

- 缩容前确认 Pod 不再接收流量（readiness/endpoint 摘除）。
- 设置 `terminationGracePeriodSeconds` 与 preStop，排空长连接。
- 用 PDB 防止维护与缩容叠加导致可用副本不足。
- 对批处理/有状态任务，缩容可能中断作业；用 Kueue/优先级保护。

### 演练清单

1. 压测单 Pod 容量，得到真实 QPS/资源曲线。
2. 逐步加压，观察 HPA 副本、Pending Pod、节点数、延迟。
3. 记录从加压到稳定的时间，和从减压到缩容的时间。
4. 模拟节点池达到上限，确认告警与降级策略。
5. 模拟 metrics 缺失/延迟，确认 HPA 不会疯狂抖动。
6. 演练节点 drain + HPA 同时发生。

MiniK8s 的 HPA 只验证公式与 min/max，不模拟节点扩容/镜像拉取；容量模型要在真实集群压测。

## 本章小结

- HPA 管副本，VPA 管资源，CA/Karpenter 管节点，KEDA 管事件驱动。
- HPA 的核心是“实际使用/请求”的比值，requests 设置直接影响扩缩结果。
- 扩容快、缩容慢，避免抖动。
- 自动伸缩需要容量缓冲、指标质量和成本治理。
- 没有一种伸缩器能解决所有问题。

## 延伸阅读

- [第 7 章 容器运行时、资源与 QoS](07-runtime-resources-qos.md)
- [第 21 章 批处理与队列](21-batch-and-queues.md)
- [第 30 章 性能与规模化](30-performance-scale.md)
- [第 44 章 商业分析 II：托管 Kubernetes 的经济学](44-business-economics.md)：伸缩策略如何影响单位成本。
- [附录 N 成本工程](../appendices/N-cost-engineering.md)：利用率、闲置与分摊。

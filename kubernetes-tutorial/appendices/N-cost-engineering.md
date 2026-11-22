# 附录 N Kubernetes 成本工程：度量、分摊与优化

> 第 44 章讨论托管 Kubernetes 的单位经济学，第 46 章给出企业 TCO 的计算方法。本附录把视角收回到集群内部：**钱到底花在哪个 namespace、哪个工作负载、哪一类浪费上，怎样用可复现的指标而不是感觉来做决定。** 文中的价格、折扣和利用率均为示例假设，用于演示方法，不代表任何厂商报价。

## N.1 成本工程回答三个问题

1. **花了多少**：账单口径，按云账号/资源类型汇总。
2. **花在哪里**：分配口径，把共享的节点、控制面、网络、存储成本摊到团队/应用。
3. **花得值不值**：单位经济学，用每请求、每租户、每 GPU 小时、每训练任务的成本对照业务价值。

这三个问题的数据来源、精度和更新频率都不同。把账单直接按 Pod 数量平均，是最常见也最误导的做法。

## N.2 先统一口径

### 账单成本与分配成本

- **账单成本（billed cost）**：云厂商实际收费，含承诺折扣、Spot、闲置节点、未挂载磁盘。
- **分配成本（allocated cost）**：把账单按规则摊到工作负载，必须满足“分摊之和 ≈ 账单”（允许未分配项显式列出）。
- **边际成本（marginal cost）**：多跑一个 Pod/一个副本真正增加的支出。在已购节点上通常是 0，直到触发扩容。
- **平均成本（average cost）**：总成本 / 总用量，适合预算，不适合决策“要不要再加一个副本”。

优化时最容易被平均成本误导：节点没满时，删除一个空闲副本并不省钱；但把它纳入长期容量规划，会减少下一次采购。

### 共享成本怎么处理

| 成本项 | 常见分摊方式 | 注意 |
|---|---|---|
| 工作节点 | 按 requests、按实际用量、或混合 | 按 requests 稳定但会奖励“超配”；按用量公平但波动大 |
| 控制面 | 按集群均摊到 namespace，或计入平台费 | 托管控制面通常按集群计费 |
| 网络出口/NAT | 按 namespace 流量或按应用标签 | 需要流量指标，不能只看 Pod |
| 存储 | 按 PVC 容量/IOPS 或按实际写入 | 未挂载的 PV 也要计入 |
| 可观测/安全 SaaS | 按节点、按日志量、按事件量 | 常被遗漏，增长快 |
| 平台人力 | 按团队数/工单量/服务数 | 商业 TCO 必算，集群账单里看不到 |

未分配成本（idle、系统组件、已购未用）必须单列。把它藏进分母会让所有团队的成本看起来都“差不多”，失去优化信号。

## N.3 从节点账单到 Pod 成本

### 请求制分配

```text
工作负载月成本 ≈ Σ(所在节点的小时成本) × (Pod requests / 节点 allocatable) × 730
```

优点：与调度器账本一致，可提前预算；缺点：requests 超配时会把成本记到“占位”而不是“实际使用”。

### 用量制分配

```text
工作负载月成本 ≈ Σ(节点小时成本) × (Pod 实际用量 / 节点 allocatable) × 730
```

优点：反映真实消耗；缺点：短时尖峰、内存缓存、GPU 空闲都会扭曲结果，且需要可靠的采样。

### 混合制（推荐）

- 基础容量按 requests 分摊，体现“预留”。
- 超出 requests 的部分按实际用量分摊，体现“额外消耗”。
- 空闲容量单列为 platform overhead。

### 一个必须记住的换算

```text
每 vCPU 小时成本 = 节点月成本 / (allocatable vCPU × 730)
每 GiB 小时成本 = 节点月成本 / (allocatable GiB × 730)
```

示例：节点 $292/月，16 vCPU、64 GiB：

```text
CPU: 292 / (16 × 730) ≈ $0.025 / vCPU·小时
内存: 292 / (64 × 730) ≈ $0.00625 / GiB·小时
```

若一个服务 requests 2 vCPU/4 GiB 且长期满载，其节点成本约 `2×0.025×730 + 4×0.00625×730 ≈ $54.75/月`；但若节点本来空闲，增加它的**边际**成本接近 0。两个数字都对，回答的是不同问题。

## N.4 关键指标

| 指标 | 定义 | 用途 | 常见误读 |
|---|---|---|---|
| Requests 覆盖率 | Σrequests / Σallocatable | 还有多少调度空间 | 高覆盖率不等于高成本，可能是超配 |
| CPU 利用率 | 实际用量 / requests | 判断是否超配 | 低于 30% 不一定要缩容，可能保峰值 |
| CPU 占容量比 | 实际用量 / allocatable | 判断集群是否浪费 | 最接近“付了钱没用” |
| 内存工作集 | working_set / requests | 判断 OOM 风险与浪费 | 缓存可回收，不能简单当泄漏 |
| 空闲成本 | 未分配容量 × 单价 | 采购与整合依据 | 应单列，不摊给应用 |
| 每请求成本 | 总成本 / 请求数 | 业务单位经济学 | 需排除健康检查、重试等噪声 |
| 每千 token 成本 | GPU/推理成本 / token 数 | AI 服务定价 | 要区分输入/输出、批处理与缓存 |
| 资源浪费率 | (requests − P95 用量) / requests | rightsizing 优先级 | 用平均值会低估峰值需求 |

## N.5 工具链与查询

### 数据来源

- **kube-state-metrics**：对象期望状态，如 requests、limits、quota、PV 容量。
- **cAdvisor / kubelet**：容器实际用量，`container_*` 指标。
- **node-exporter**：节点级 CPU、内存、磁盘、网络。
- **OpenCost / Kubecost**：把云价格、节点、Pod、PVC、网络组合成分配成本。
- **云账单导出**：Billing/CUR/成本明细，校准分配模型。
- **应用指标**：请求数、队列长度、token 数，用于单位经济学。

### 常用 PromQL

```promql
# 各 namespace 的 CPU requests（核）
sum by (namespace) (
  kube_pod_container_resource_requests{resource="cpu"}
)

# 各 namespace 的 CPU 实际用量（核，5 分钟速率）
sum by (namespace) (
  rate(container_cpu_usage_seconds_total{container!="", image!=""}[5m])
)

# requests 利用率（按 namespace）
sum by (namespace) (rate(container_cpu_usage_seconds_total{container!="", image!=""}[5m]))
/
sum by (namespace) (kube_pod_container_resource_requests{resource="cpu"})

# 各 namespace 的内存 requests 与工作集
sum by (namespace) (kube_pod_container_resource_requests{resource="memory"})
sum by (namespace) (container_memory_working_set_bytes{container!="", image!=""})

# 节点可分配资源
sum by (node) (kube_node_status_allocatable{resource="cpu"})
sum by (node) (kube_node_status_allocatable{resource="memory"})

# 集群小时成本（OpenCost 指标）
sum(node_total_hourly_cost)

# 按 namespace 的 CPU 分配成本（示例：分配核数 × 每核小时成本）
sum by (namespace) (container_cpu_allocation * on(node) group_left node_cpu_hourly_cost)
```

注意：

- `container_cpu_usage_seconds_total` 含 pause 容器，需 `container!=""` 等过滤。
- 内存优先用 `working_set_bytes`，不要用 `rss` 判断可回收量。
- `kube_pod_container_resource_requests` 对已终止 Pod 可能仍有时间序列，查询时限定 `kube_pod_status_phase{phase=~"Running|Pending"}` 或使用 OpenCost 的分配口径。
- 所有比率查询都要处理分母为 0 的情况。

### OpenCost / Kubecost API

```bash
# OpenCost：按 namespace 聚合最近 7 天
curl -sG http://opencost:9003/allocation/compute \
  --data-urlencode 'window=7d' \
  --data-urlencode 'aggregate=namespace' \
  --data-urlencode 'includeIdle=true' | jq

# Kubecost：按 controller 聚合
curl -sG http://kubecost-cost-analyzer:9090/model/allocation \
  --data-urlencode 'window=7d' \
  --data-urlencode 'aggregate=controller' | jq '.data[0]'
```

两者都需要正确的云价格表；自建集群要手动配置节点单价，否则分配结果只是“相对比例”，不是金额。

### 快速盘点命令

```bash
# 谁申请了最多 CPU/内存
kubectl get pods -A -o json | jq -r '
  .items[] | [.metadata.namespace, .metadata.name,
  ([.spec.containers[].resources.requests.cpu // "0"] | join("+")),
  ([.spec.containers[].resources.requests.memory // "0"] | join("+"))] | @tsv' |
  sort -k3 -hr | head -20

# 没有设置 requests 的 Pod
kubectl get pods -A -o json | jq -r '
  .items[] | select(any(.spec.containers[]; .resources.requests == null)) |
  "\(.metadata.namespace)/\(.metadata.name)"'

# 未挂载的 PV
kubectl get pv -o json | jq -r '
  .items[] | select(.status.phase != "Bound") |
  "\(.metadata.name) \(.status.phase) \(.spec.capacity.storage)"'
```

## N.6 分摊：showback 与 chargeback

- **Showback**：给团队看成本，不实际扣款。阻力小，适合起步。
- **Chargeback**：把成本记入团队预算。激励强，但需要稳定的分配规则和争议处理机制。

落地顺序：

1. 统一标签：`team`、`app`、`env`、`cost-center`，并通过准入策略强制。
2. 先做 namespace 级 showback，和账单对账（误差 < 5%）。
3. 再下钻到 controller/服务；不要一开始就按 Pod 分摊。
4. 定义共享成本与空闲成本的公开规则。
5. 建立月度评审：异常、趋势、优化项，而不是只看排名。
6. 需要 chargeback 时，先跑 2–3 个月“影子账单”，校准规则。

标签缺失时，用 ownerReferences 向上归因到 Deployment/StatefulSet，再回退到 namespace。不要把“未打标签”全部归到 platform，否则团队没有动力补标签。

## N.7 优化杠杆（按收益/风险排序）

| 顺序 | 杠杆 | 典型收益 | 风险/前提 | 验证方式 |
|---|---|---|---|---|
| 1 | 删除僵尸资源 | 高 | 低；需确认无流量、无数据 | 无请求、无连接、无 PVC 写入 |
| 2 | 回收未挂载 PV/旧快照/旧镜像 | 中高 | 低；保留策略 | 存储账单、快照清单 |
| 3 | Rightsizing requests/limits | 中 | 中；峰值与 OOM | P95/P99 用量、压测 |
| 4 | 提高装箱率/节点整合 | 高 | 中；碎片与故障域 | 节点利用率、调度成功率 |
| 5 | HPA/KEDA/Cluster Autoscaler | 中高 | 中；冷启动、指标延迟 | 扩容时延、SLO |
| 6 | Spot/抢占式实例 | 高 | 高；中断 | 中断率、PDB、混部比例 |
| 7 | 承诺折扣/预留 | 高 | 低；锁定 | 基线负载、现金流 |
| 8 | 架构优化（缓存、批处理、异步） | 很高 | 高；研发投入 | 每请求成本 |
| 9 | GPU 共享/MIG/批处理 | 高 | 高；隔离与性能 | 利用率、延迟、错误率 |

重要：**Rightsizing requests 本身通常不直接降低账单**。它改善调度空间，只有配合节点整合、自动伸缩或释放预留容量，才转化为真金白银。把“requests 降了 40%”当成“省了 40%”是成本汇报里最常见的错误。

### 节点整合的安全边界

- 保留故障域冗余：至少 N+1，关键服务跨 AZ。
- 检查 PDB 与驱逐预算，避免整合引发可用性事故。
- 大页、GPU、本地盘、特定架构节点往往无法简单装箱。
- 用 `descheduler` 或 Karpenter consolidation 时，先观察驱逐频率与 P99 延迟。

## N.8 容量规划：从 SLO 到节点数

一个可复现的容量模型：

```text
峰值 QPS
  × 单请求 CPU 时间（秒）
  = 所需 CPU 核数（Little's Law 的近似）
  × 安全系数（1.3–2.0）
  ÷ 单 Pod 有效容量
  = 副本数
  × Pod requests
  ÷ 节点可分配资源
  = 节点数
  + 故障域冗余
  + 系统组件预留
```

示例（假设）：

- 峰值 2,000 QPS，单请求 5 ms CPU → 10 核。
- 安全系数 1.5 → 15 核。
- 单 Pod requests 1 核、压测可稳定承载 150 QPS → 需要 14 个副本（取整并满足 PDB）。
- 每节点可分配 14 核（16 核减去系统预留）→ 1 个节点即可，但生产至少 2 个故障域 + N+1 → 3 个节点。

容量规划必须用压测校准“单 Pod 有效容量”，不能只用 CPU 利用率。队列、连接池、GC、下游限流都可能先成为瓶颈。

### 扩容链路的时间预算

```text
指标采集延迟 + HPA 决策周期 + 调度 + 镜像拉取 + 应用启动 + 就绪探针
```

这段时间内的流量必须由现有副本和队列吸收。若冷启动 90 秒，而流量 30 秒内翻倍，HPA 再准也来不及；需要预扩容、缓冲队列或更快的运行时。

## N.9 GPU 成本

GPU 成本的三个乘数：

1. **利用率**：`DCGM_FI_DEV_GPU_UTIL`、显存占用、SM 活跃度。GPU 利用率 30% 不等于成本浪费 70%，还要看显存和吞吐。
2. **共享方式**：时间片、MPS、MIG、vGPU。共享提高利用率，但隔离性、显存带宽和故障域不同。
3. **任务形态**：在线推理看 P99 延迟与每 token 成本；训练看有效算力利用率（MFU）与排队时间。

常用指标：

```promql
# GPU 平均利用率（按节点/GPU）
avg by (node, gpu) (DCGM_FI_DEV_GPU_UTIL)

# 显存使用
avg by (node, gpu) (DCGM_FI_DEV_FB_USED / (DCGM_FI_DEV_FB_USED + DCGM_FI_DEV_FB_FREE))

# 每美元产出多少 token（inference_tokens_total 是 counter，
# gpu_hourly_cost 是 gauge，不能对 gauge 用 rate）
sum(increase(inference_tokens_total[1h])) / sum(gpu_hourly_cost)

# 业务口径：每百万 token 成本（美元）
sum(gpu_hourly_cost) * 1e6 / sum(increase(inference_tokens_total[1h]))
```

要点：

- counter 用 `rate`/`increase`；gauge（成本、利用率、显存）用 `avg`/`sum`/`max`。对 gauge 用 `rate` 得到的是“变化率”，通常为 0 或噪声。
- 分子分母的时间窗口必须一致；GPU 成本要按实际计费单位（卡时/小时）聚合，再按 cluster/namespace/model 过滤。
- 单位成本要和业务指标一起看：每百万 token 成本、每请求成本、每租户成本。只看 GPU 利用率会把“排队但没算”的浪费藏起来。

成本优化不是“把 GPU 塞满”：批处理能提高吞吐，但会增加尾延迟；MIG 能隔离，但可能降低大模型可用显存；Spot GPU 便宜，但训练必须能容错检查点。

## N.10 预算、告警与异常

- **配额不是预算**：ResourceQuota 限制 requests/对象数，不直接对应金额；预算要按价格模型换算。
- **单位成本护栏**：每请求成本、每 token 成本、每租户成本，比总额更能发现回归。
- **异常检测**：按 namespace/controller 的日环比、周同比；新出现的无 requests 大 Pod、未挂载 PV、NAT 出口突增。
- **预算告警分层**：50% 预警、80% 升级、100% 阻断非关键变更；不要只在月底看账单。
- **成本回归测试**：在 CI 中对关键服务做基准压测，记录每千请求的 CPU/内存，防止“功能上线，成本翻倍”。

## N.11 一个完整核算示例（假设）

集群：30 个等效工作节点，16 vCPU/64 GiB，$292/节点/月；总 CPU allocatable 480 核。

| 项目 | 数值 | 说明 |
|---|---|---|
| 节点成本 | $8,760/月 | 30 × 292 |
| CPU requests | 288 核（60%） | 调度账本 |
| CPU P95 用量 | 115 核（24%） | 实际消耗 |
| 每 vCPU 小时成本 | $0.025 | 8760 / (480 × 730) |
| Requests 对应的“预留成本” | $5,256/月 | 288 × 0.025 × 730 |
| 实际用量成本 | $2,099/月 | 115 × 0.025 × 730 |
| 空闲/超配差额 | $3,157/月 | 需结合峰值与冗余判断 |

优化推演：

1. 把 20 个服务的 requests 从“峰值 × 2”调到“P95 × 1.3”，requests 降到 210 核。
2. 配合节点整合，节点从 30 台降到 24 台，直接节省 `6 × 292 = $1,752/月`。
3. 对无状态批处理使用 40% Spot，假设折扣 65%，节省 `0.4 × 8760 × 0.65 ≈ $2,278/月`。
4. 对稳定基线购买 1 年承诺，假设节省 30%，作用在剩余按需部分。

注意：第 1 步单独不省钱；第 2 步才把调度空间变成账单下降。汇报时要把“容量效率提升”和“实际节省”分开。

## N.12 常见误区

- **按 Pod 数量平均分摊**：大 Pod 和小 Pod 成本相同，完全失真。
- **把 requests 当实际用量**：超配团队看起来“很贵”，实际可能很闲；反过来也会掩盖浪费。
- **只看 CPU 不看内存/存储/网络**：内存和 NAT 出口常常是隐形大头。
- **忽略空闲与未分配**：账单里最容易被浪费的就是“买了没用”。
- **用平均利用率做缩容决策**：峰值、突发、批处理都会被平均值掩盖。
- **把 Spot 节省当成净收益**：中断、重试、SLA 违约和工程成本要计入。
- **没有对账**：分配成本与账单长期偏差 > 10%，说明模型已经不可信。

## 延伸阅读

- [第 20 章 自动伸缩](../chapters/20-autoscaling.md)：HPA/VPA/Cluster Autoscaler/Karpenter 的机制与交互。
- [第 30 章 性能与规模化](../chapters/30-performance-scale.md)：容量、压测与拐点判断。
- [第 44 章 托管 Kubernetes 的经济学](../chapters/44-business-economics.md)：成本结构与单位经济模型。
- [第 46 章 企业 TCO、选型与落地路线图](../chapters/46-business-tco-selection.md)：从技术成本到商业案例。
- [附录 L 云原生生态地图](L-ecosystem-map.md)：OpenCost、Kubecost 等成本工具的定位。

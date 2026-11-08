# 第 10 章 · 运维与可观测性

## 10.1 运维 Kueue 的四个问题

日常运维可以归结为四个问题：

1. **队列是否健康？** ClusterQueue/LocalQueue 是否 Active，配额是否合理。
2. **作业为什么没开始？** Pending、QuotaReserved、AdmissionCheck，还是物理资源不够。
3. **资源是否被公平使用？** 谁在借用，谁在饥饿，是否需要调整权重/配额。
4. **系统是否稳定？** 控制器是否重启、调谐是否变慢、apiserver 是否被大量 watch 压垮。

Kueue 提供了指标、条件、事件和 Visibility API 来回答这些问题。

## 10.2 关键指标

以下是常见指标（名称以目标版本为准）：

### 队列与配额

| 指标 | 含义 |
|---|---|
| `kueue_cluster_queue_status` | CQ 状态 |
| `kueue_cluster_queue_nominal_quota` | CQ nominal 配额 |
| `kueue_cluster_queue_resource_usage` | CQ 资源使用 |
| `kueue_cluster_queue_resource_reservation` | CQ 资源预留 |
| `kueue_cluster_queue_resource_pending` | CQ pending 资源 |
| `kueue_cluster_queue_borrowing_limit` | 借用上限 |
| `kueue_cluster_queue_lending_limit` | 出借上限 |
| `kueue_cluster_queue_weighted_share` | 加权公平份额 |
| `kueue_pending_workloads` | pending Workload 数；带 `status="active"/"inadmissible"` 标签，告警通常只看 active |
| `kueue_admitted_active_workloads` | 当前已准入 |
| `kueue_reserving_active_workloads` | 已预留未准入 |
| `kueue_local_queue_pending_workloads` | LocalQueue 维度的 pending 数 |
| `kueue_local_queue_admission_fair_sharing_usage` | AFS 历史用量（仅开启 AFS 时有意义） |
| `kueue_local_queue_resource_usage` / `_reservation` | LocalQueue 维度使用/预留 |

### 准入与驱逐

| 指标 | 含义 |
|---|---|
| `kueue_admitted_workloads_total` | 累计准入 |
| `kueue_quota_reserved_workloads_total` | 累计配额预留 |
| `kueue_evicted_workloads_total` | 累计驱逐 |
| `kueue_evicted_workloads_once_total` | 按原因去重的驱逐 |
| `kueue_preempted_workloads_total` | 累计抢占 |
| `kueue_admission_wait_time_seconds` | 排队等待时间 |
| `kueue_admission_checks_wait_time_seconds` | AdmissionCheck 等待时间 |
| `kueue_admission_attempt_duration_seconds` | 准入尝试耗时 |

### Cohort

| 指标 | 含义 |
|---|---|
| `kueue_cohort_subtree_quota` | 子树配额 |
| `kueue_cohort_subtree_resource_reservations` | 子树预留 |
| `kueue_cohort_subtree_admitted_active_workloads` | 子树活跃作业 |
| `kueue_cohort_weighted_share` | Cohort 加权份额 |

### 控制器健康

| 指标 | 含义 |
|---|---|
| `kueue_build_info` | 版本/构建信息 |
| `kueue_admission_cycle_preemption_skips` | 抢占跳过 |
| `kueue_pending_scheduling_hashes` | 待调度哈希（内部；仅 `SchedulingEquivalenceHashing` 开启时出现） |
| `kueue_admission_attempts_total` | 准入尝试次数 |
| `kueue_preemption_target_recomputations_total` | 抢占目标重算次数 |
| `kueue_replaced_workload_slices_total` | TAS 热替换/切片替换次数（仅 Elastic Workload Slices 相关 gate 开启时出现） |
| controller-runtime 的 workqueue/reconcile 指标 | 控制器性能 |

### 更完整的指标清单

Kueue 的指标按对象分族，排障时不要只盯一个：

```text
# 准入/等待
kueue_admission_wait_time_seconds
kueue_quota_reserved_wait_time_seconds
kueue_ready_wait_time_seconds
kueue_admitted_until_ready_wait_time_seconds
kueue_admission_checks_wait_time_seconds
kueue_workload_creation_latency_seconds
kueue_execution_time_seconds

# Workload 计数
kueue_pending_workloads
kueue_unadmitted_workloads
kueue_reserving_active_workloads
kueue_admitted_active_workloads
kueue_finished_workloads
kueue_admitted_workloads_total
kueue_quota_reserved_workloads_total
kueue_evicted_workloads_total
kueue_evicted_workloads_once_total
kueue_preempted_workloads_total

# 队列/公平
kueue_cluster_queue_weighted_share
kueue_cluster_queue_resource_usage
kueue_cluster_queue_resource_reservation
kueue_cluster_queue_resource_pending
kueue_cluster_queue_nominal_quota
kueue_cluster_queue_borrowing_limit
kueue_cluster_queue_lending_limit
kueue_cohort_weighted_share
kueue_cohort_subtree_quota
kueue_cohort_subtree_resource_reservations
kueue_local_queue_admission_fair_sharing_usage
```

指标名以你的版本 `/metrics` 输出为准；不同版本会增删。

### 指标语义陷阱

1. **`kueue_pending_workloads` 可能包含 inadmissible**：看 active vs inadmissible 的标签/指标，不要把所有 pending 都当成"在排队"。
2. **`evicted_workloads_total` vs `evicted_workloads_once_total`**：前者每次驱逐都加，后者按 `(reason, underlyingCause)` 去重，用于区分"同一作业反复被驱"。
3. **`weighted_share` 为 0 不代表公平**：可能只是没有借用；要看 usage/nominal/borrow。
4. **`resource_pending` 是 pending Workload 的请求总量**，不是物理空闲量。
5. **直方图指标要同时看 `_bucket`/`_count`/`_sum`**，只看 `histogram_quantile` 会掩盖样本量不足。

## 10.3 PromQL 示例

```promql
# 每个 CQ 的 pending 作业数（只看 active；inadmissible 是结构性不可准入）
sum by (cluster_queue) (kueue_pending_workloads{status="active"})

# 每个 CQ 的 CPU 使用率（usage/nominal 都带 flavor 标签；先按 CQ 聚合再相除）
sum by (cluster_queue) (kueue_cluster_queue_resource_usage{resource="cpu"})
/
sum by (cluster_queue) (kueue_cluster_queue_nominal_quota{resource="cpu"})

# 当前借用最多的 CQ
sum by (cluster_queue) (
  kueue_cluster_queue_resource_usage{resource="nvidia.com/gpu"}
  - kueue_cluster_queue_nominal_quota{resource="nvidia.com/gpu"}
)

# AdmissionCheck 等待时间 P99
histogram_quantile(0.99,
  sum by (le) (rate(kueue_admission_checks_wait_time_seconds_bucket[5m]))
)

# 驱逐速率
sum by (reason) (rate(kueue_evicted_workloads_total[5m]))
```

标签名和指标集合会随版本/配置变化；先用 `/metrics` 确认。

## 10.4 条件与事件

### 查看 Workload

```bash
kubectl get workloads -A
kubectl get workload <name> -n <ns> -o wide
kubectl describe workload <name> -n <ns>
kubectl get workload <name> -n <ns> -o yaml
```

重点：

```yaml
status:
  conditions:
  - type: QuotaReserved
  - type: Admitted
  - type: Evicted
  - type: Preempted
  - type: Finished
  # 真实 Kueue：Inadmissible 是 QuotaReserved=False 的 Reason，不是条件类型
  # MiniKueue：额外写 Inadmissible=True 合成条件，便于 Visibility 查询
  status.admission: ...
  status.admissionChecks: ...
  status.requeueState: ...
```

### 查看队列

```bash
kubectl get clusterqueues -o wide
kubectl describe clusterqueue <name>
kubectl get localqueues -A -o wide
kubectl describe localqueue <name> -n <ns>
```

`ACTIVE=False` 时，`REASON` 列和 Conditions 会告诉你原因：

- ResourceFlavor 不存在；
- AdmissionCheck 不存在/未 Active；
- Cohort 引用错误；
- 配额 schema 非法。

### 事件

```bash
kubectl get events -A --sort-by=.lastTimestamp | tail -n 50
kubectl describe workload <name> -n <ns>
```

与准入相关的事件类型：

| 事件 | 含义 |
|---|---|
| `QuotaReserved` | 配额预留成功 |
| `Admitted` / `Evicted` | 准入/驱逐 |
| `Preempted` | 被更高优先级/公平策略抢占 |
| `AdmissionGated` / `AdmissionGateCleared` | 外部 gate 阻塞/清除 |
| `ProvisioningRetry` / `ProvisioningReady` | **MiniKueue-lite 事件**：ProvisioningRequest-lite 的置备重试/完成 |
| `AdmissionCheckRejected` | 检查永久失败，Workload 被停用 |
| `Migrated` | **MiniKueue-lite 事件**：Concurrent Admission 的 Flavor 迁移 |

事件不是状态机本身，排障时仍以 `status.conditions` 为准；事件用于回答"什么时候发生了什么"。真实 Kueue 的事件名/Reason 以 release 的源码和 `kubectl get events` 为准；带 "MiniKueue-lite" 的行不要拿去和真实集群对拍。

### 10.4.1 控制器/Webhook 故障时的行为

Kueue 挂了不等于集群挂了，但不同对象的行为不一样。先把"谁在继续、谁被卡住"分清：

| 场景 | 已有 Admitted 作业 | 新提交的作业 | 删除/更新 Kueue 对象 |
|---|---|---|---|
| controller-manager 全部不可用 | Pod 继续运行，Job 继续跑 | 集成控制器可能仍创建 Workload，但没人准入；Job 保持 suspended | finalizer 不被处理，删除可能卡住；先恢复控制器 |
| leader 切换中 | 短暂无人调度，已运行作业不受影响 | 等待新 leader | 短暂延迟 |
| mutating/validating webhook 不可用（`failurePolicy: Fail`） | 不受影响 | 创建/更新 Job、CQ、LQ 可能被 API server 拒绝 | 同样被拒绝 |
| webhook 不可用（`failurePolicy: Ignore`） | 不受影响 | 可能绕过默认值/校验，产生不一致对象 | 高风险，不建议在生产用 Ignore |
| etcd/API server 故障 | Pod 继续跑 | 整个控制面不可用 | 不可用 |

排障顺序：

```bash
# 1. Deployment/Lease
kubectl -n kueue-system get deploy,pods
kubectl -n kueue-system get lease

# 2. webhook 配置与证书
kubectl get validatingwebhookconfiguration,mutatingwebhookconfiguration | grep kueue
kubectl -n kueue-system get secret | grep -i cert

# 3. 控制器日志里的启动/证书/leader 错误
kubectl -n kueue-system logs deploy/kueue-controller-manager --tail=200

# 4. 端到端：创建一个最小 CQ/LQ/Job，看卡在哪一层
```

升级顺序建议：**先镜像/CRD，再控制器，再 webhook 证书**；不要把 CRD 删除再重建。升级前先确认没有大量 Pending/正在抢占的 Workload，升级后观察 workqueue、准入延迟和 webhook 拒绝率。

## 10.5 排障流程

### 场景一：Job 一直 suspended

```text
1. Job 有 queue-name 标签吗？
   kubectl get job <name> -n <ns> -o jsonpath='{.metadata.labels}'
2. Workload 存在吗？
   kubectl get workloads -n <ns>
3. Workload 的 QuotaReserved 条件是什么？
   kubectl get workload <name> -n <ns> -o yaml
4. LocalQueue 指向的 CQ 存在且 Active 吗？
   kubectl get lq -n <ns> -o wide
   kubectl get cq -o wide
5. namespaceSelector 匹配吗？
6. 配额够吗？看 resource_pending / pending_workloads
7. 有 AdmissionCheck 吗？状态是什么？
8. 有 admission gate 吗？conditions 里是否有 AdmissionGated=True？
9. DRA 设备是否已映射？deviceClassMappings 是否覆盖请求？
```

### 场景二：QuotaReserved=True 但 Admitted=False

```bash
kubectl get workload <name> -n <ns> -o jsonpath='{.status.admissionChecks}'
```

- `Pending`：检查 AdmissionCheck 控制器是否运行、日志、外部依赖；
- `Retry`：临时失败，等退避；
- `Rejected`：永久失败，Workload 会被停用，需要修复后重新激活。

ProvisioningRequest 还要检查：

```bash
kubectl get provisioningrequests -A
kubectl describe provisioningrequest <name>
```

### 场景三：CQ 使用超过 nominal，但新作业进不来

检查：

- 是否有其他 CQ 借用？
- 是否有 `lendingLimit` 限制？
- 是否需要 `reclaimWithinCohort`？
- 是否被 `Hold`？
- 是否 Cohort 总量已满？

### 场景四：作业被反复抢占

检查：

- 优先级是否设置过低？
- `withinClusterQueue` 是否是 `LowerPriority`？
- Fair Sharing 是否在起作用？
- 是否有 `borrowWithinCohort` 导致借用时抢占？
- 是否需要给关键队列更高的权重/优先级/保护？

### 场景五：节点上有空闲 GPU，但作业 Pending

可能原因：

- Kueue 配额已满（逻辑上）；
- Flavor 不匹配（nodeSelector/toleration 不匹配）；
- TAS 拓扑放不下；
- DRA 设备分配失败；
- Pod 亲和性/污点；
- `WaitForPodsReady` 超时。

先分清"Kueue 层"还是"kube-scheduler 层"：

```bash
kubectl describe workload <name> -n <ns>
kubectl describe pod <pod> -n <ns>
kubectl get events -n <ns> --sort-by=.lastTimestamp
```

## 10.6 日志与控制器

```bash
kubectl logs -n kueue-system deploy/kueue-controller-manager -f
kubectl logs -n kueue-system deploy/kueue-controller-manager --tail=500
```

看什么：

- 调度循环错误；
- webhook 拒绝原因；
- AdmissionCheck 控制器错误；
- 队列 inactive 原因；
- 大量重复 reconcile（可能是抖动）。

日志级别可以通过配置调整；生产环境不要长期开 debug。

### 10.6.1 pprof

如果怀疑控制器 CPU/内存/锁竞争，可以临时开启 pprof：

```yaml
controllerManager:
  pprofBindAddress: "127.0.0.1:6060"
```

```bash
kubectl -n kueue-system port-forward deploy/kueue-controller-manager 6060:6060
go tool pprof http://127.0.0.1:6060/debug/pprof/profile?seconds=30
go tool pprof http://127.0.0.1:6060/debug/pprof/heap
```

注意：pprof 端点只绑定 localhost，通过 port-forward 访问；不要暴露到公网。

### 10.6.2 KueueViz

KueueViz 适合快速看"队列里有什么、谁在等、谁在用"。它不能替代 Prometheus 告警和日志，但能显著降低和租户沟通的成本。

### 10.6.3 证据链

排障时按"对象 → 条件 → 事件 → 指标 → 日志"的顺序收集证据：

```bash
kubectl get workload <name> -n <ns> -o yaml          # status.conditions / admissionChecks / requeueState
kubectl describe workload <name> -n <ns>             # Events
kubectl get clusterqueue <cq> -o yaml                # status.conditions / flavorsReservation
kubectl get localqueue <lq> -n <ns> -o yaml          # status
kubectl get events -n <ns> --sort-by=.lastTimestamp  # 近期事件
```

不要从日志开始猜；先让对象自己说话。

## 10.7 仪表盘与告警

建议的告警：

1. **Pending 作业持续增长**：
   ```promql
   sum(kueue_pending_workloads{status="active"}) > threshold
   for: 30m
   ```
2. **CQ 长期满载/借用异常**：
   ```promql
   kueue_cluster_queue_resource_usage / kueue_cluster_queue_nominal_quota > 0.95
   for: 1h
   ```
3. **驱逐率异常**：
   ```promql
   rate(kueue_evicted_workloads_total[10m]) > threshold
   ```
4. **AdmissionCheck 等待过长**：
   ```promql
   histogram_quantile(0.99, rate(kueue_admission_checks_wait_time_seconds_bucket[10m])) > 600
   ```
5. **控制器重启/leader 切换**：
   ```promql
   increase(kube_pod_container_status_restarts_total{namespace="kueue-system"}[1h]) > 0
   ```
6. **apiserver 压力**：watch 数、request latency、429。

仪表盘建议按：

- 队列视角：pending、admitted、usage/nominal、share；
- Cohort 视角：子树配额、借用、公平；
- 作业视角：等待时间、执行时间、驱逐原因；
- 控制器视角：reconcile 延迟、队列深度、错误率。

## 10.8 容量规划

### 输入

- 每个 CQ 的 nominal；
- Cohort 的共享额度；
- 典型作业的资源画像（CPU/内存/GPU/Pod 数）；
- 到达率、运行时长、优先级分布；
- 物理集群的真实容量。

### 经验

- nominal 总和不要长期显著超过物理容量，否则大量作业会"逻辑上可准入但物理上 pending"。
- Cohort 共享额度可以提高利用率，但要留出回收空间。
- GPU 资源要同时看卡数和节点数/拓扑；只看卡数会低估碎片。
- 用 P50/P95/P99 等待时间而不是平均值评估队列健康。
- 大作业和小作业混合时，考虑 StrictFIFO 的阻塞风险，或用预留/公平保护大作业。

### 模拟

在改配额前，用 MiniKueue 或真实集群的 shadow workload 做模拟：

- 调整 nominal/borrowing/lending；
- 观察 pending、借用量、抢占次数；
- 再上线。

## 10.9 升级与版本兼容

- 先读目标版本的 release notes 和 upgrade policy。
- 注意 CRD schema、默认值、feature gate 变化。
- 备份关键对象（CQ、Cohort、Flavor、AdmissionCheck）。
- 在 staging 集群验证 webhook/CRD 升级。
- 不要跨太多 minor 版本直接升级。
- 多副本控制器升级时注意 leader election 和 webhook 证书。

## 10.10 卸载与 finalizer

Kueue 给 Workload、ClusterQueue、ResourceFlavor、AdmissionCheck、Topology 和被管理的 Pod 加 finalizer。卸载顺序：

1. 删除 Job/JobSet 等；
2. 删除 Workload；
3. 删除 LocalQueue/ClusterQueue；
4. 删除 Cohort/ResourceFlavor/AdmissionCheck；
5. 删除 Topology；
6. 卸载 Kueue。

如果卡住：

```bash
kubectl get workloads -A
kubectl get clusterqueues
kubectl describe clusterqueue <name>
```

不要直接 force delete CRD；先恢复控制器清理 finalizer。

## 10.11 MiniKueue 的可观测性

MiniKueue 暴露一个简化的 Prometheus 文本：

```text
minikueue_admission_cycles_total
minikueue_admitted_workloads_total
minikueue_finished_workloads_total
minikueue_evicted_workloads_total
minikueue_preempted_workloads_total
minikueue_quota_reserved_workloads_total
minikueue_admission_check_waits_total
minikueue_pods_ready_timeouts_total
minikueue_reclaimed_pods_total
minikueue_pending_workloads
minikueue_reserving_active_workloads
minikueue_admitted_active_workloads
minikueue_finished_workloads
```

运行：

```bash
go run ./cmd/minikueue -scenario basic -ticks 4
```

每个 tick 打印：

- 事件；
- Workload 状态表；
- CQ 使用量；
- 最终指标。

此外，CLI 支持一个简化的 Visibility 查询：

```bash
go run ./cmd/minikueue -scenario basic -ticks 2 -query basic
```

输出 pending/reserving/admitted/finished 分类、pending 原因和 usage/nominal。这是 `Engine.QueueStatus` 的 CLI 暴露，对应真实 Kueue 的 Visibility API 思路。

它不能替代真实 Kueue 的指标，但可以用来理解"准入/驱逐/借用"如何改变计数器。

## 10.12 本章小结

- 运维 Kueue 的核心是队列健康、作业等待原因、公平性和控制器稳定性。
- 指标覆盖 CQ、Cohort、Workload、准入/驱逐和控制器本身。
- 排障要先分清"Kueue 配额层"和"kube-scheduler 物理层"。
- 告警应关注 pending 增长、长期过载、异常驱逐和 AdmissionCheck 等待。
- 升级/卸载要尊重 CRD 和 finalizer 的生命周期。
- MiniKueue 提供了一套简化的指标和事件，便于教学和实验。

---

[← 上一章：高级能力](09-advanced-features.md) | [返回目录](../README.md) | [下一章：安全与多租户 →](11-security-multitenancy.md)

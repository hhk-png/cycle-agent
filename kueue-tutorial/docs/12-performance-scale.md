# 第 12 章 · 性能与规模

## 12.1 Kueue 的性能瓶颈在哪里

Kueue 不是数据面。它不转发流量、不跑 Pod。它的性能问题主要来自控制面：

1. **API watch 量**：Job、Workload、Queue、Pod 等对象的 informer 事件。
2. **调度循环**：每次有 Workload/配额变化时，重新计算队列和准入。
3. **Flavor/抢占模拟**：多资源组、多 Flavor、多候选 victim 的组合。
4. **缓存快照**：为一致性复制/汇总 usage。
5. **指标基数**：CQ/LQ/Workload 标签组合。
6. **Webhook**：创建/更新时的校验和默认值。

规模上来后，最常见的症状不是"CPU 跑满"，而是：

- 准入延迟上升；
- pending 队列长时间不收敛；
- apiserver watch/request 压力；
- 控制器 reconcile 队列积压；
- 指标 cardinality 爆炸。

## 12.2 控制器架构

Kueue 基于 controller-runtime：

```text
API Server
   │ watch/list
   ▼
Informer Cache
   │ events
   ▼
Workqueue
   │ key
   ▼
Reconciler
   ├── QueueManager
   ├── Scheduler Cache
   ├── Scheduler
   └── Admission/Preemption
   │
   ▼
API Server（更新 status/conditions）
```

关键设计：

- **Informer 缓存**：避免每次 reconcile 都直接 list apiserver。
- **Workqueue**：按 key 去重、限速、重试。
- **Cache**：维护 CQ/Cohort/Workload 的实时 usage，避免每次从头扫描。
- **Snapshot**：调度时用一致视图，防止边算边变。
- **Leader election**：多副本时只有一个 leader 执行写操作。

## 12.3 调度循环的复杂度

### 12.3.1 队列排序

每个 CQ 的 pending Workload 需要按优先级/创建时间排序：

```text
O(n log n)
```

n 是该 CQ 的 pending 数。通常不是瓶颈，除非单个 CQ 堆积了数万作业。

### 12.3.2 Flavor 分配

对每个资源组，按 Flavor 顺序尝试。最坏情况是多个资源组的组合：

```text
O(F1 × F2 × ... × Fk)
```

Kueue 通过以下限制控制复杂度：

- 资源组最多 16 个；
- 所有 Flavor 总数最多 256 个；
- `flavorFungibility` 控制是否尝试下一个；
- 缓存可用配额，避免每次全量扫描。

即便如此，**大量 Flavor + 大量 CQ + 高频提交**仍会显著增加准入延迟。API reference 明确提示：只配置必要的 Flavor，并在峰值负载下做性能测试。

### 12.3.3 抢占

抢占需要：

1. 枚举已准入 Workload（候选人）；
2. 排序；
3. 逐个模拟移除并重新检查配额；
4. 最小化 victim 集合。

复杂度大致是：

```text
O(admitted × candidateAssignmentCost)
```

最坏情况下可能很贵。优化点：

- 只考虑同 CQ/Cohort 的候选人；
- 先用便宜的过滤（优先级、是否借用）缩小集合；
- 缓存每个 victim 的资源贡献；
- 限制重试/抢占频率；
- 避免对称的可配置抢占规则。

### 12.3.4 Fair Sharing

每个 CQ 需要计算 DRS/加权 share。成本与：

- CQ 数；
- Flavor 数；
- 资源数；
- Cohort 树深度；

相关。Cohort 树越深、CQ 越多，计算越重。

### 12.3.5 并发准入

Concurrent Admission 会为每个 Flavor 创建一个 Variant Workload。假设：

- 32 个 Flavor；
- 1000 个 CQ；
- 每个 Workload 32 个 Variant；

控制器的工作量会成倍增加。Kueue 文档明确提示：在数千 CQ 的规模下，要评估并发准入的性能。注意它**为每个 Flavor 都创建 Variant，即使明显放不下**，所以成本接近"Workload 数 × Flavor 数"。

### 12.3.6 TAS 与 DRA

TAS 的成本不在"选 domain"本身，而在：

- 每个调度周期重建/克隆拓扑缓存；
- 对每个候选 domain 做容量检查；
- 热替换时模拟 Pod 移动；
- DRA 可行性检查对每个候选节点尝试设备分配。

DRA 的成本尤其容易被低估：`KueueDRADeviceFeasibility` 会在每个候选节点上跑一次 allocation，CEL selector 复杂时会显著拖慢调度。节点数 × Pod 数 × 调度频率是实际成本。

### 12.3.7 快照与锁

Kueue 的调度读 `Snapshot`，写回到 API server，再由 informer 更新 Cache。这个设计避免了"边算边变"，但快照本身要复制/聚合大量状态。CQ/Cohort 越多、层级越深、TAS domain 越细，快照成本越高。

MiniKueue 用 `Cache.Admitted()` 克隆所有已准入 Workload 来模拟快照，复杂度是 O(admitted)；这也是它的 `TryAdmit` 基准随已准入数线性增长的原因之一。

### 12.3.8 资源变换、Configurable/Fair Sharing 抢占

- **资源变换**：每个 PodSet 在计算 per-pod usage 时遍历规则，成本 O(规则数 × 资源数)。规则通常很少（<10），不是瓶颈；但若规则里有大量 outputs，`TotalUsage` 的 map 操作会增加。
- **Configurable Preemption**：直接准入失败后才执行。候选收集是 O(规则数 × selector 数 × 已准入 Workload 数)；最坏情况接近 O(admitted²)，因为每个候选都要重新跑 `findAssignment`。生产配置要控制 selector 数量和 scope，避免 `AnyClusterQueue` + 多个宽规则。
- **Fair Sharing 抢占**：候选过滤 O(admitted)，每个候选重新计算 share O(Flavor × Resource)，再做最小化。它的成本高于 Classic，但只在"配额不足且 Classic 失败"时触发。
- **ProvisioningRequest**：每个 tick 对每个未 Ready 的检查做常数工作；真正的成本在外部控制器/云扩容，不在 Kueue 内部。
- **Backoff**：O(1)。

MiniKueue 的 `BenchmarkTryAdmit` 不触发抢占，所以新增路径不会出现在基准里；要测它们，需要单独构造"直接准入失败 + 候选集"的基准。

## 12.4 规模限制与经验值

以下不是硬性上限，而是工程经验：

| 维度 | 建议 |
|---|---|
| ClusterQueue 数 | 数百到数千取决于控制器资源；上千时要做基准测试 |
| 每 CQ Flavor 数 | 只保留必要 Flavor；避免几十个同质 Flavor |
| 资源组数 | 通常 1～3 个；不要为了"分类"拆太细 |
| Cohort 深度 | 2～4 层；过深增加公平计算和排障难度 |
| Pending 作业数 | 关注单 CQ 的排队长度；过大时考虑分片/多队列 |
| 指标标签 | 避免把高基数 Workload 名放进 metrics label |
| 事件 | 避免每个 tick 都写 Event 造成 apiserver 压力 |

API reference 的硬限制（资源组 16、Flavor 256）是"不要超过"，不是"推荐值"。实际推荐值通常远低于上限。

## 12.5 性能测试方法

### 12.5.1 宏观指标

- **准入延迟**：Workload 创建到 Admitted 的时间。
- **排队时间**：Pending 到 QuotaReserved。
- **调度吞吐**：每秒准入/驱逐的 Workload 数。
- **控制器 CPU/内存**：尤其是 informer cache 和指标。
- **apiserver QPS/latency**：watch、list、update。
- **workqueue 深度/延迟**：是否积压。

### 12.5.2 负载模型

要覆盖：

- 稳定提交；
- 突发提交；
- 大作业/小作业混合；
- 多优先级；
- 抢占触发；
- 多 Cohort 借用；
- AdmissionCheck 慢路径；
- 节点故障/驱逐。

只测"空集群提交 1000 个小作业"没有意义。

### 12.5.3 环境

- 控制器资源限制要与生产一致；
- apiserver 要能承受 watch 负载；
- 指标抓取要开启/关闭分别测；
- 网络延迟要接近生产。

### 12.5.4 结果解读

- P99 比平均值重要；
- 区分"Kueue 慢"和"物理资源不足"；
- 区分"准入慢"和"Pod 调度慢"；
- 观察长时间运行后的内存增长（informer cache、指标、Workload 保留）。

### 12.5.5 一个可复现的压测方案

1. **固定工作负载画像**：例如 70% 小作业（1-2 CPU）、20% 中作业（8 CPU）、10% 大作业（32 GPU）。
2. **固定到达过程**：泊松/固定速率/突发三组，分别测。
3. **固定 CQ/Flavor 配置**：从 10 CQ/1 Flavor 逐步扩到 1000 CQ/32 Flavor。
4. **记录基线**：空集群、无指标抓取时的准入延迟。
5. **逐项打开**：TAS → 公平分享 → 抢占 → 指标抓取 → Concurrent Admission，观察每一步的增量。
6. **回放真实 trace**：用生产 Workload 的创建时间/资源分布，而不是随机数。
7. **记录 P50/P95/P99 和 workqueue 深度**，不要只记平均值。

如果压测结果和"感觉慢"不一致，先确认是不是 apiserver/watch 或物理调度慢，而不是 Kueue 决策慢。

## 12.6 调优清单

### 队列与算法

- 默认用 `BestEffortFIFO`；只在需要严格顺序时用 `StrictFIFO`。
- 限制 Flavor 数量；合并同质 Flavor。
- 避免不必要的资源组拆分。
- 对抢占设置合理阈值，避免频繁抢占。
- 用 `reclaimWithinCohort` 而不是 `Any`，除非确实需要。
- Fair Sharing 权重不要极端化（例如 0 或 1000）。

### 控制器

- 给 controller-manager 合理的 CPU/内存 requests/limits。
- 只启用实际使用的 integration。
- 控制 metrics 抓取频率和 cardinality。
- 调整 workqueue 限速/重试参数（如果版本暴露）。
- 大规模下评估 `HighMaxParallelismWithinReconcile`（v0.20 beta，提高单次 reconcile 的并发更新上限；具体数值按 release notes 核对）；它能降低大批量状态更新的延迟，但会放大 API server 压力。
- 用 `clientConnection.qps`/`burst` 控制控制器到 API server 的速率，结合 `apiserver_request_total` 和 workqueue depth 调。
- 避免长期 debug 日志。
- 监控 informer cache 大小和 watch 数。
- 用 `objectRetentionPolicies` 控制 Workload 数量，避免 etcd 膨胀；但删除策略要评估 owner 级联。

### API Server

- 使用 APF 保护 Visibility API 和高频查询。
- 避免频繁 list 全量 Workload；用 label/field selector。
- 控制 Event 产生速率。
- 大规模集群考虑拆分 apiserver/etcd 负载。

### 多集群

- manager 的 quota 要接近 worker 总容量，避免过度提名。
- Incremental dispatcher 的 batch size/间隔要调优。
- 控制 worker 数量和跨集群 watch 负载。

## 12.7 MiniKueue 的性能

MiniKueue 是教学实现，不是性能标杆。它的复杂度：

- `TryAdmit`：遍历 Flavor、资源组、资源；Cohort 汇总会扫描成员；
- 抢占：扫描所有已准入 Workload；
- 每次 Tick 重建 Scheduler View 和队列。

在本机（Intel Core Ultra 9 285H，Go 1.27）上的一个简单基准：

```text
BenchmarkTryAdmit-16    93055    13085 ns/op    880 B/op    8 allocs/op
BenchmarkTryAdmit-16    80638    12808 ns/op    880 B/op    8 allocs/op
```

条件：

- 1 个 CQ；
- 500 个已准入 Workload；
- 每次尝试准入 1 个 1 CPU 的 Workload；
- 不触发抢占。

这个数字只说明"在 500 个已准入对象下，单次决策是微秒级"，不能外推到生产。真实 Kueue 还要处理 API watch、序列化、etcd、网络和并发。多次运行的波动通常来自机器负载：空闲时约 13–16 µs/op，高负载时会出现 29–33 µs/op 的离群值；分配次数稳定在 880 B/op、8 allocs/op。

复现：

```bash
cd mini-kueue
go test ./internal/scheduler -bench BenchmarkTryAdmit -benchmem -run '^$'
```

## 12.8 什么时候该怀疑性能问题

- Pending 队列持续增长，但集群有闲置资源；
- Workload 创建后很久才 QuotaReserved；
- 控制器 CPU 长期 100%，workqueue 积压；
- apiserver 出现大量 429/超时；
- 指标抓取导致控制器内存上涨；
- 抢占次数异常，作业反复驱逐；
- 增加 Flavor/CQ 后准入延迟明显上升。

排查顺序：

1. 看 Kueue 指标和日志；
2. 看 apiserver/watch/QPS；
3. 看 controller 资源限制；
4. 看队列/Flavor 配置；
5. 用最小复现做基准；
6. 再考虑拆分/限流/优化。

## 12.9 本章小结

- Kueue 的性能瓶颈在控制面：watch、调度循环、Flavor 组合、抢占、指标基数。
- 资源组/Flavor 数量是准入延迟的关键因素；不要配置不必要的组合。
- 抢占和并发准入会放大工作量；需要基准测试和限制。
- 调优要同时看 Kueue、apiserver、controller 资源和负载模型。
- MiniKueue 的基准只用于理解算法成本，不能代表生产。

---

[← 上一章：安全与多租户](11-security-multitenancy.md) | [返回目录](../README.md) | [下一章：MiniKueue 设计 →](13-mini-kueue-design.md)

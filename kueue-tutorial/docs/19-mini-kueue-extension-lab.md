# 第 19 章 · MiniKueue 扩展实验室

前面的章节把 MiniKueue 当作"读懂 Kueue 的教具"。本章把它当作"可以动手改的代码库"：从加一个字段开始，逐步实现 TAS 的切片、DRA-lite、MultiKueue-lite，最后讨论如何迁移到真实 controller-runtime。

> 实验约定：所有改动都在 `mini-kueue/` 内；每完成一个实验，至少补一个测试，并跑 `test -z "$(gofmt -l .)" && go vet ./... && go test ./... -count=1`（`gofmt -l` 本身只打印、不失败，必须显式判断输出为空）。不要只改场景不改测试——场景输出会"看起来对"，但不变量测试才能抓住账本错误。

## 19.1 实验环境

```bash
cd mini-kueue
go test ./... -count=1
go run ./cmd/minikueue -list
go run ./cmd/minikueue -scenario tas -ticks 2
```

当前代码的关键路径：

```text
Submit → Store
Tick
  ├── decayAFS                   # Admission Fair Sharing 半衰期
  ├── recomputeEffectiveQuotas   # CapacityProvider → effective nominal
  ├── rebuildScheduler           # Store → Scheduler.View
  ├── ensureCache
  ├── applyStopPolicies
  ├── reapDeactivated
  ├── expireExecution
  ├── checkPodsReady             # WaitForPodsReady
  ├── migrateConcurrentAdmissions
  ├── admitReadyReservations
  ├── schedule                   # TryAdmit → reserve/evict；AFS 排序
  ├── admitReadyReservations
  └── updateGauges
```

理解这条路径，比记住每个函数名更重要。

## 19.2 实验一：加一个多 PodSet 作业

### 目标

创建一个 `driver + worker` 的 Workload，验证资源汇总和整体准入。

### 步骤

1. 在 `internal/scenario/scenarios.go` 加一个构造函数：

```go
func MultiPodSet() *controller.Engine {
    e := New()
    MustCQ(e, SimpleCQ("multi", "default", api.RL("cpu", 8000, "memory", 8192)))
    MustLQ(e, "default", "q", "multi")
    MustNamespace(e, "default", nil)
    w := &api.Workload{
        Namespace: "default",
        Name:      "train",
        Spec: api.WorkloadSpec{
            Active:    true,
            QueueName: "q",
            PodSets: []api.PodSet{
                {Name: "driver", Count: 1, Requests: api.RL("cpu", 2000, "memory", 2048)},
                {Name: "worker", Count: 3, Requests: api.RL("cpu", 1000, "memory", 1024)},
            },
        },
    }
    MustSubmit(e, w)
    return e
}
```

2. 注册到 `Names()` / `Run()`。
3. 加测试：tick 后应 Admitted；`TotalUsage` 应为 CPU 5000m、内存 5Gi、pods 4。
4. 把 CQ 配额改成 4000m，验证整体 Pending（不会只准入 driver）。

### 你会学到

- `TotalUsage` 是对所有 PodSet 求和，并自动加 `pods`；
- Kueue 的 all-or-nothing 是"逻辑上一起放得下"，不是"先跑一部分"；
- 多 PodSet 不需要改调度器，只需要改资源汇总。

### 19.2.1 实验：给 selector 加一个 Operator

当前 `LabelSelectorRequirement` 支持 `In`/`NotIn`/`Exists`/`DoesNotExist`。试着加 `Gt`/`Lt`（数值比较）：

1. 在 `Labels` 里把值解析成整数；
2. 解析失败时按字符串比较或直接不匹配；
3. 补表驱动测试，覆盖负数、前导零、非数字；
4. 更新 `ValidateLabelSelector` 的合法 operator 列表。

这个练习会让你理解 Kubernetes LabelSelector 的边界：它本身不支持数值比较，很多平台用 annotation + 准入策略实现。

## 19.3 实验二：扩展 TAS

MiniKueue 已经实现了 TAS 的四种放置模式，以及两个扩展能力：

| 模式 | 行为 |
|---|---|
| `Required` | 所有 Pod 必须在同一 domain |
| `Preferred` | 优先同 domain，放不下则退化为无约束 |
| `Unconstrained` | 不要求同 domain，按容量铺开 |
| `Group` | 多个 PodSet 共享同一个 domain |
| `SliceSize` | 把一个 Required PodSet 切成若干片，每片一个 domain |
| `Spreading` | 优先选择当前用量最少的 domain |

代码入口：

- `api.PodSetTopology` / `api.Topology` / `api.TopologyDomain`；
- `scheduler.planTopology`；
- `Scheduler.TopologyUsage`（跨层物理账本）；
- `scenario.TopologyAware` / `scenario.TASSlice`。

### 19.3.1 观察现有行为

```bash
go run ./cmd/minikueue -scenario tas -ticks 2
```

你会看到：

```text
job-a  tas[g0/tas-flavor,tas:rack/rack-a]
job-b  tas[g0/tas-flavor,tas:rack/rack-b]
job-c  Pending
```

每个 rack 有两个 host，每个 host 2 张卡；4 卡作业必须占满一个 rack 的两个 host。这正是"domain 容量是层级聚合"的体现。

### 19.3.2 实验：验证 partial slice，扩展多层约束

`SliceSize`、`Spreading` 和 partial slice 已经在 MiniKueue 中实现，可以先观察：

```bash
go run ./cmd/minikueue -scenario tas-slice -ticks 1
```

你会看到 `slice-four` 产生两条 `TopologyAssignment`（h3/h4），`spread-two` 选择 h2 而不是 h1。slice 内部固定按"最空 domain 优先"选域，`Spreading` 只控制整个 PodSet 的选域。

下一步扩展：

1. **partial slice 回归测试**：当 `count` 不是 `SliceSize` 的整数倍时（例如 10 和 4），最后一个 partial slice 只按实际 Pod 数计费；补一个测试断言 `TopologyAssignment.PodCount` 之和等于 `EffectiveCount`，且不会虚占容量。
2. **多层 slice 约束**：给 `PodSetTopology` 增加 `constraints []SliceConstraint`，先按 block 切片，再在片内按 rack 切片；内层 size 必须整除外层。
3. **slice 与 group 的边界**：当前校验拒绝 `SliceSize + Group`；如果产品上确有需求，先定义"group 内每个 PodSet 独立切片"还是"整个 group 切片"，不要直接放开校验。

### 19.3.3 实验：把打散扩展为跨 Workload 公平

当前 `Spreading` 按 domain 的当前资源用量选择最空域。真实 Kueue 的 `TASTopologySpreading` 还考虑跨 Workload 的分布。你可以：

- 在 `Cache` 增加 `TopologyWorkloadCount(topology, level)`；
- `orderedDomains` 先按 Workload 数、再按资源用量排序；
- 加一个"两个同尺寸作业应落在不同 rack"的测试；
- 注意确定性：所有排序都要有稳定的名字 tie-breaker。

### 19.3.4 实验：节点故障热替换

真实 Kueue 的 `TASFailedNodeReplacement` 会尝试只替换故障节点。MiniKueue 没有节点健康模型。你可以：

1. 增加 `TopologyDomain.Unavailable bool`；
2. Tick 时把不可用 domain 上的 TAS assignment 标记为失效；
3. 尝试在原 Workload 内重新放置，而不是整体驱逐；
4. 失败时再整体驱逐。

这是一个很好的练习：它会逼你思考"部分重调度"和"配额账本"如何保持一致。

## 19.4 实验三：扩展 DRA-lite

真实 DRA 用 `ResourceClaim`/`DeviceClass` 描述设备。MiniKueue 已经实现了"设备类 → 逻辑资源"的计费映射，可以用 `-scenario dra` 观察。下面先回顾已实现的类型，再给出扩展方向。

### 19.4.1 设计

新增：

```go
type DeviceClass struct {
    Name                 string
    ExtendedResourceName api.ResourceName
}

type DeviceRequest struct {
    DeviceClass string
    Count       int32
}
```

在 `PodSet` 增加：

```go
Devices []DeviceRequest
```

在 `ClusterQueueSpec` 增加：

```go
DeviceClassMappings map[string]api.ResourceName
```

规则：

- 如果 PodSet 通过 `ResourceClaimTemplate` 请求设备，按 `DeviceClass` 的 `count` 计费；
- 如果 Pod 同时写了 `nvidia.com/gpu: 1` 和 DRA 请求，要避免重复计费；
- 计费结果仍然落到 `ResourceList`，复用现有 `canFitFlavor`。

### 19.4.2 已实现的代码位置

1. 类型在 `api/types.go`：`DeviceRequest`、`PodSet.Devices`、`ClusterQueueSpec.DeviceClassMappings`；
2. `validate.go` 校验 `DeviceClass` 非空、`Count` 非负，并在 `ValidateWorkloadRefs` 检查映射存在；
3. `Workload.PodSetPerPodUsage` 先把**每 Pod** 的设备请求转换成逻辑资源，并与每 Pod 的 `requests` 取 `max`；`TotalUsage` 最后才乘有效 Pod 数：

```go
devices := api.ResourceList{}
for _, d := range ps.Devices {
    if name, ok := w.Spec.DeviceClassMappings[d.DeviceClass]; ok {
        devices[name] += int64(d.Count) // 每 Pod 的设备数
    }
}
perPod := ps.Requests.Clone()
for name, value := range devices {
    if value > perPod.Get(name) {
        perPod[name] = value // max(requests, devices)，不是相加
    }
}
// 回到 TotalUsage：total[name] += perPod[name] * EffectiveCount(ps)
```

注意不要把 `count` 乘进 `PodSetPerPodUsage`，否则 `TotalUsage` 会再乘一次，导致重复计费。

4. `scheduler.findAssignment` 不需要改：设备已经变成普通资源；
5. 场景 `dra` 演示两个 4 卡作业在 6 卡配额下的准入/Pending；
6. 测试覆盖重复计费保护、未知 DeviceClass、负数。

扩展方向：

- 增加 Counter/Capacity 计费模式（MIG/共享显存）；
- 从 `ResourceClaimTemplate` 解析 DeviceClass，而不是自定义结构；
- 给 `TopologyDomain` 增加设备容量，在 TAS 放置时检查设备可行性；
- 设备健康/污点与抢占释放。

### 19.4.3 与真实 DRA 的差距

- 真实 DRA 有设备分配、绑定、健康状态、设备污点；
- Kueue 需要读取 `ResourceClaimTemplate`，而不是自定义结构；
- 设备容量可能来自 `DeviceClass`/`ResourceSlice`，不是 CQ nominal；
- TAS 需要检查"节点是否能分配该设备"。

MiniKueue 的 DRA-lite 只演示"配额计费"，不演示"设备分配"。这正是第 15 章差距矩阵里的一项。

## 19.5 实验四：扩展 MultiKueue-lite

真实 MultiKueue 是 manager/worker 架构。MiniKueue 的 `internal/multikueue` 已经实现了单进程模拟，可以先用 `-scenario multikueue` 和 `-scenario multikueue-failover` 观察：

```text
Manager Engine
  ├── W1 Engine（独立 Store/Cache/Scheduler）
  ├── W2 Engine
  └── Dispatcher
        ├── AllAtOnce：复制到所有 worker
        └── Incremental：分批提名
```

### 19.5.1 已实现的最小设计

1. `internal/multikueue` 包已存在；
2. 核心类型：

```go
type Worker struct {
    Name     string
    Engine   *controller.Engine
    Workload *api.Workload
    Down     bool
}

type Config struct {
    Dispatcher        Dispatcher
    StepSize          int
    WorkerLostTimeout int
}

type Manager struct {
    Workload *api.Workload
    Workers  []*Worker
    Selected string
}
```

3. Dispatcher 每个 tick：
   - 检查已选中的 worker 是否失联；超过 `WorkerLostTimeout` 就重新分发；
   - 跳过 `Down` 的 worker，把 manager 的 Workload 复制到健康候选；
   - 在健康 worker 上调用 `Submit` + `Tick`；
   - 第一个 `Admitted` 的 worker 成为 `Selected`；
   - 清理其他健康 worker 上的副本；失联 worker 的副本等恢复后再清理。
4. 状态：
   - `Nominated []string`（对应 `status.nominatedClusterNames`）；
   - `Selected string`（对应 `status.clusterName`）；
   - `Worker.Down` 与 `downTicks`（失联计时）。
5. 加场景：两个 worker，配额不同，观察谁先准入、另一个如何清理；`multikueue-failover` 演示选中 worker 失联后的超时切换。

### 19.5.2 扩展难点

- **状态同步**：当前只在选中时读取 worker 状态，没有持续回写 manager；
- **失败清理**：已实现 `Down` + `WorkerLostTimeout`，但没有真实网络/连接退避；
- **重复执行**：`Down` 只是"manager 看不见"，不是"worker 停止运行"；真实环境要检测双跑；
- **配额一致性**：manager 的配额应该约等于 worker 总量，当前由场景手工配置；
- **幂等**：已实现重复 tick 不重复创建副本；
- **公平**：多个 manager 竞争同一 worker 时如何排序。

### 19.5.3 验收标准

- 两个 worker 都收到副本；
- 只有一个最终 Admitted；
- 另一个副本被删除/停用；
- manager 的 `Selected` 稳定；
- 重复 tick 不产生重复对象；
- 选中 worker 失联后，超时前保持选中，超时后切换；
- 失联 worker 恢复后，陈旧副本被清理。

### 19.5.4 继续扩展

1. **连接退避**：给 `Down` worker 加指数退避，模拟真实 MultiKueue 的 1m→10m；
2. **抢占 gate**：让每个副本默认带一个关闭的 gate，manager 一次只打开一个；
3. **重复执行检测**：当旧 worker 恢复且仍持有 Admitted 副本时告警；
4. **多 Workload**：把 Manager 从单 Workload 扩展成队列，验证并发提名。

## 19.6 实验五：从整数资源迁移到 Kubernetes Quantity

MiniKueue 用 `int64`（millicore/MiB）换取确定性和简单性。真实 Kueue 用 `resource.Quantity`。迁移步骤：

1. 定义 `ResourceList map[ResourceName]resource.Quantity`；
2. 用 `Quantity.MilliValue()` / `Value()` 做整数化比较，或直接用 `Cmp`；
3. 处理 `1000m`、`1Gi`、`1.5` 等格式；
4. 保留 `FormatQuantity` 用于 CLI；
5. 更新所有测试的期望值。

这一步会暴露很多边界：四舍五入、单位换算、负值、零值、超大数。建议先写一组表驱动测试，再改类型。

## 19.7 实验六：迁移到 controller-runtime

把 MiniKueue 的 `scheduler`/`cache` 移植到真实控制器，需要：

| MiniKueue | controller-runtime |
|---|---|
| `api` 结构体 | CRD 类型 + kubebuilder 标记 |
| `store` | informer cache + client |
| `Engine.Tick` | reconcile + workqueue |
| `Cache` | 进程内调度缓存 |
| `Submit` | Job 集成创建 Workload |
| `ApproveCheck` | AdmissionCheck 控制器 |
| `Metrics` | Prometheus registry |
| `Validate*` | webhook |
| `scenario` | envtest / 集成测试 |

建议顺序：

1. 用 kubebuilder 建项目；
2. 定义 CRD，生成 deepcopy/CRD/client；
3. 把 `scheduler`/`cache` 包原样搬过去，替换类型；
4. 写 Workload reconciler，先只做 status 同步；
5. 写 Job 集成，暂停/放行；
6. 加 webhook；
7. 用 envtest 做端到端测试；
8. 再加 TAS/DRA/MultiKueue。

不要一开始就追求生产级并发；先把"创建 Workload → 准入 → 放行 Job"跑通。

## 19.8 实验七：加一个可观测性查询

真实 Kueue 有 Visibility API。MiniKueue 可以加一个简单的查询接口：

```go
type QueueStatus struct {
    ClusterQueue string
    Pending      []string
    Reserved     []string
    Admitted     []string
    Usage        api.ResourceList
}

func (e *Engine) QueueStatus(cq string) QueueStatus
```

然后：

- 在 CLI 加 `-query <cq>`；
- 在 metrics 里加 pending 原因分布；
- 加测试：pending/admitted 分类正确。

这能帮助你理解"为什么这个作业没开始"需要哪些数据。

## 19.9 实验八：扩展 Visibility

当前 `QueueStatus` 只返回状态分类和 pending 原因。试着扩展：

1. 增加 `Borrowed` 字段：对每个 flavor/resource 计算 `max(0, usage - nominal)`；
2. 增加 `TopPending`：按优先级/创建时间取前 N 个 pending Workload；
3. 增加 `AdmissionCheckWaits`：统计每个 check 的 pending 数量；
4. 在 CLI 的 `-query` 输出中展示；
5. 加测试：借用、pending 排序、check 统计。

再进一步：把它做成一个只读 HTTP handler（`/queuez?cq=...`），加上超时和分页。真实 Kueue 的 Visibility API 还要考虑 APF、鉴权和结果大小。

## 19.10 实验九：给指数退避加最大重试次数

MiniKueue 已实现 `BackoffConfig` 指数退避（`BaseTicks`/`MaxTicks`/`Multiplier`，见 `internal/controller/engine.go` 的 `requeueDelay`），AdmissionCheck Retry 和抢占重排队都会使用它。当前缺的是**最大重试次数**：退避会无限继续。

真实实现的核心公式是：

```go
// attempt 从 0 开始（第一次重排队时 w.Status.Requeues == 0）
delay := float64(base) * math.Pow(multiplier, float64(attempt))
if maxTicks > 0 && delay > float64(maxTicks) {
    delay = float64(maxTicks)
}
```

`multiplier <= 0` 时按 2 处理，`base <= 0` 时按 1 处理；这些兜底也在 `requeueDelay` 里。试着扩展：

```go
type BackoffConfig struct {
    BaseTicks  int64
    MaxTicks   int64
    Multiplier float64 // 新增字段已存在；默认 2
    MaxRetries int32   // 新增；0 表示不限制
}
```

1. 在 `Engine.Backoff` 上增加 `MaxRetries`；
2. Retry 时检查 `RetryCount`，超过后转 `Rejected` 或停用 Workload；
3. 加测试：`base=1,multiplier=2` 的 1/2/4/8 序列、`MaxTicks` 上限、`MaxRetries=0`（不限制）、刚好等于上限、超过上限；
4. 对照真实 Kueue 的 `ProvisioningRequestConfig.retryStrategy`（`backoffBaseSeconds`/`backoffMaxSeconds`/`backoffLimitCount`）。

## 19.11 实验十：ProvisioningRequest-lite（已实现，继续扩展）

真实 ProvisioningRequest 是一个 AdmissionCheck 控制器，向 Cluster Autoscaler 请求扩容。MiniKueue 已用 `controller.ProvisioningConfig` 实现了观察到的核心行为：

- `CheckName` 指定驱动的 AdmissionCheck；
- `DelayTicks` 表示置备耗时，也是失败重试的退避；
- `Failures` 表示前 N 次尝试以 Retry 结束；
- Retry 会释放配额并重排队，重试计数跨预留保留；
- 成功路径在重新预留后等待 `DelayTicks` 再置 Ready。

场景与测试：`go run ./cmd/minikueue -scenario provisioning-request -ticks 5`，`TestProvisioningRequestLiteRetriesThenAdmits`。

继续扩展的方向：

1. 新增 `api.ProvisioningRequest` 对象与 `Engine.ProvisioningRequests`，让每次尝试可观测；
2. 让 `CapacityProvider` 模拟扩容：失败若干 tick 后增加容量；
3. 用 `backoffLimitCount` 替代布尔式 `Failures`，耗尽后 Rejected 并停用；
4. 支持 `ProvisioningRequestConfig.retryStrategy` 的 base/max/limit 三元组；
5. 支持 `podSetUpdates`，把 provisioning class 注入 Pod 模板。

## 19.12 实验十一：资源变换（已实现，继续扩展）

真实 Kueue 的 Configuration 支持 `resources.transformations`，把 MIG 等输入资源折算成配额资源。MiniKueue 已实现：

- `api.ResourceTransformation{Input, Strategy, MultiplyBy, Outputs}`；
- `Replace`/`Retain`；
- `multiplyBy` 可以是资源名（取 Pod 请求量）或固定整数；
- 所有规则基于原始 Requests，避免链式污染；
- `Workload.PodSetPerPodUsage` 在 DRA 映射之后应用变换；
- 校验拒绝 `pods`、空资源名、重复 input、负 output。

场景与测试：`go run ./cmd/minikueue -scenario resource-transform -ticks 2`，HAMi 风格单测见第 5.10 节。

继续扩展的方向：

1. 用 `Quantity` 支持小数/单位后缀，而不是整数；
2. 支持 `Retain` + `multiplyBy` 的官方边界（保留原始 input，只放大 outputs）；
3. 把变换从 Engine 字段提升为 Configuration 对象并支持热更新；
4. 对变换后的资源做 CQ 覆盖校验，提前拒绝无法计费的配置；
5. 增加"输入资源同时出现在 outputs"时的防环/防双计测试。

## 19.13 实验十二：Configurable Preemption-lite（已实现，继续扩展）

真实 `PreemptionConfig` 用声明式规则定义触发条件和候选选择器。MiniKueue 已实现：

- `api.PreemptionConfig`、`PreemptionRule`、`PreemptionCandidateSelector`；
- trigger：`Always`/`InsufficientQuota`/`QuotaFeasibleAndInsufficientTopology`；
- scope：`WithinLocalQueue`/`WithinClusterQueue`/`WithinParentCohort`/`WithinCohortTree`/`AnyClusterQueue`；
- priority（Base/Boosted + 比较符）、`numericLabels`、`labelSelector`、`clusterQueueSelector`；
- 规则候选与 Classic 候选合并、去重、统一最小化；
- CQ 通过 `spec.preemptionConfigName` 引用。

场景与测试：`go run ./cmd/minikueue -scenario configurable-preemption -ticks 2`，`TestConfigurablePreemptionAuthorizesEviction` 与 `TestValidatePreemptionConfig`。

继续扩展的方向：

1. 增加 `preemptorSelectors`，限制哪些 incoming Workload 能触发规则；
2. 实现规则优先级与互斥，避免对称规则造成 A↔B 循环；
3. 用真实 numeric label 计算 Boosted priority，而不是固定 +1000；
4. 实现逐层 `WithinParentCohort`，而不是用根 Cohort 近似；
5. 把配置从 Engine 内存对象迁移为 CRD + webhook 校验。

## 19.14 实验十三：Fair Sharing 抢占

MiniKueue 已实现 `LessThanOrEqualToFinalShare` 与 `LessThanInitialShare` 的 lite 版本：

1. 候选来自同一 Cohort 树、优先级不高于 preemptor、`FairShare > 0` 的 Workload；
2. 每次加入 victim 后重新计算 `preemptorNewShare`、`targetOldShare`、`targetNewShare`；
3. 按 strategy 判断是否允许；
4. 反向最小化 victim 集合。

场景：`go run ./cmd/minikueue -scenario fair-sharing-preemption -ticks 2`。

继续扩展：AlmostLCA、按目标 CQ 的 share 排序、预抢占、采样窗口与精确锚点。

## 19.15 实验十四：AlmostLCA

真实 Kueue 在层级 Cohort 中用 AlmostLCA 做公平比较。MiniKueue 当前把整棵树聚合成一个 lendable 分母。实现 AlmostLCA：

1. 为每个 CQ 记录从根到叶的路径；
2. 对两个 CQ，找到 LCA，再取 LCA 下各自的分支节点；
3. 计算分支节点的加权 DRS，而不是叶子 CQ 的 DRS；
4. 在 `FairShareOrder` 中按分支节点分组排序；
5. 测试：`org-a` 下 10 个小 CQ 不应因为数量多而压过 `org-b` 的 1 个大 CQ。

## 19.16 实验十五：子树隔离的共享池

MiniKueue 当前的共享池按根树扁平聚合，兄弟子树的共享池对目标 CQ 可见。真实 Kueue 的 Cohort 共享池只应在其子树内可见。实现思路：

1. 给 `cohortNominal` / `cohortSharedFree` 增加目标 CQ 参数；
2. 只汇总目标 CQ 到根路径上的祖先 Cohort 的共享池，而不是整棵根树；
3. 共享池用量需要按子树归属记账，否则兄弟子树使用自己的池也会被算进目标的可借量；
4. 加一个测试：`root -> (org-a, org-b)`，org-a 有共享池、org-b 没有；org-b 的 CQ 不应能借到 org-a 的共享池；
5. 注意这会影响 `FairShare` 的 lendable 分母和 Cohort 级 limit，改完要重跑全部不变量。

这是一个很好的"从扁平近似到逐层账本"的练习，也是第 15 章列出的主要差距之一。

这些实验都不需要改 Kubernetes 依赖，适合作为"从 lite 到更真实"的进阶练习。

## 19.17 测试方法

每加一个功能，至少覆盖四类测试：

1. **单元测试**：纯函数/算法（scheduler、cache、api）；
2. **控制器集成测试**：Tick 后的状态、Cache、条件；
3. **场景测试**：所有场景可运行且不变量成立；
4. **随机不变量测试**：随机提交/完成/抢占下账本不泄漏。

推荐的不变量：

```text
1. usage >= 0
2. 无 Cohort 的 CQ：usage <= effective nominal
3. 显式 borrowingLimit：usage <= nominal + limit
4. Cohort 树：总 usage <= 总 nominal（含共享池）
5. TAS：每个 domain/level 的 usage <= capacity
6. Cache 中的 Workload 状态必须是 QuotaReserved/Admitted
7. Store 中 QuotaReserved/Admitted 的 Workload 必须在 Cache 中
8. 被驱逐的 Workload 不得继续占用 Cache
```

## 19.18 验收命令

```bash
cd mini-kueue
gofmt -l .                                  # 应无输出
go vet ./...                                # 应无告警
go test ./... -count=1                      # 全部通过
go test ./... -cover -count=1               # 看覆盖率
go test ./internal/scheduler -bench . -benchmem -run '^$'
cd ..
python scripts/verify_docs.py               # 文档/YAML 校验
```

如果改了场景，再跑：

```bash
for s in $(go run ./cmd/minikueue -list | awk '{print $1}'); do
  go run ./cmd/minikueue -scenario "$s" -ticks 5 >/dev/null
  echo "ok $s"
done
```

## 19.19 本章小结

- 加多 PodSet 作业只需要改资源汇总，不需要改调度器。
- selector 扩展要先想清楚 Kubernetes LabelSelector 的能力边界。
- TAS 的核心是"domain 容量 + 层级聚合 + 放置模式"；slice、partial slice 和打散已实现，下一步是多层约束、热替换和节点/非 TAS Pod 容量推导。
- DRA-lite 的关键是"设备 → 逻辑资源"的计费映射，不涉及真实设备分配；下一步是 Counter/Capacity 和设备可行性。
- MultiKueue-lite 的关键是提名、单一选中、副本清理和状态同步；worker 失联与恢复清理已实现，下一步是 External dispatcher、编排式抢占和跨集群网络/凭据。
- Visibility 是"只读投影"，不要让它参与决策；退避要防忙循环和无限重试。
- 从整数资源迁移到 Quantity、从模拟器迁移到 controller-runtime，是两条主要的工程化路径。
- 进阶实验：ProvisioningRequest-lite、资源变换、Configurable Preemption、AlmostLCA；它们对应真实 Kueue 仍未被 MiniKueue 覆盖的部分。
- 每加一个功能，都要补单元、集成、场景和随机不变量测试。

---

[← 上一章：生产落地手册](18-production-adoption.md) | [返回目录](../README.md) | [下一章：调度语义边界 →](20-scheduling-edge-cases.md)

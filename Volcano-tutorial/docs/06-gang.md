# 第 6 章 Gang 调度

## 6.1 什么是 gang

Gang scheduling 要求一组任务**要么一起开始，要么都不开始**。它是分布式训练、MPI、HPC 的基础语义。

没有 gang 时：

```text
8 卡节点，作业需要 8 个 worker（每个 1 卡）
已有 4 卡被占用，剩 4 卡
默认调度器：启动 4 个 worker，另外 4 个 Pending
结果：先启动的 4 个 worker 在 all-reduce 上永远等待 → 死锁
```

有 gang 时：

```text
资源不足 → 一个都不启动 → 作业保持 Pending（PodGroup 可能处于 Inqueue）
资源够了 → 8 个一起绑定 → 一起 Running
```

Volcano 用 `minAvailable` 表达 gang 门槛：

- `minAvailable = 总副本数`：严格 all-or-nothing。
- `0 < minAvailable < 总副本数`：至少启动这么多，剩余副本后续扩容。
- `minAvailable = 0`：创建时会被 mutating webhook 补成 Σ task 的有效 minAvailable（上游 CRD 无法区分“省略”和“0”）；它**不是**关闭 gang 的开关。

LLM 预训练的 `minAvailable` 还要满足并行策略的整除约束（例如 `TP×PP` 的倍数），具体算法与常见世界大小见第 34.3 节。

## 6.2 gang 插件

gang 是调度器插件，通常在 tier 1：

```yaml
tiers:
- name: tier-1
  plugins:
  - name: priority
  - name: gang
    enablePreemptable: true
    enableJobStarving: true
```

注意：`enablePreemptable`/`enableJobStarving` 是 **plugin option**（与 `name` 同级），不是 `arguments` 里的键。写进 `arguments` 不会报错，但会被忽略——这是很常见的“配置了却没生效”。

它注册的扩展点：

| 扩展点 | 行为 |
|---|---|
| `JobValidFn` | `CheckTaskValid`/`CheckSubJobValid` 且有效 Pod 数 ≥ `minAvailable`（`minAvailable` 超过总副本由 webhook 拒绝） |
| `JobOrderFn` | 未 Ready 的作业优先，便于先凑齐 gang |
| `JobReadyFn` | `CheckTaskReady` + `CheckSubJobReady` + `IsReady` |
| `JobPipelinedFn` | `CheckTaskPipelined` + `CheckSubJobPipelined` + `IsPipelined` |
| `JobStarvingFn` | `enableJobStarving=true` 时注册；判定 `Waiting + Ready < MinAvailable` |
| `PreemptableFn`/`ReclaimableFn` | 只允许驱逐“超过 `minAvailable`”的副本，保护 gang 底线 |
| `UnifiedEvictableFn` | v1.15 gang-aware eviction 的统一入口 |

“Ready”在 gang 语境下包括 `Allocated/Bound/Binding/Running/Pipelined/Succeeded` 等状态，不只是 Running。

### 6.2.1 enableJobStarving

`enableJobStarving` **不是**“按 minAvailable 还是按总副本判断饥饿”的开关，而是**是否注册 gang 的 `JobStarvingFn`**：

- `true`（plugin option 默认值）：注册 hook，判定 `Waiting + Ready < MinAvailable`；
- `false`：不注册 hook。`Session.JobStarving()` 找不到可用的 starving 函数时返回 `false`，于是 `preempt`/`reclaim`/`gangpreempt` 会认为该作业“不饥饿”并跳过它（除非 tier 里还有别的插件注册了 JobStarving）。

注意：plugin option 的 nil 会在 `ApplyPluginConfDefaults` 里被补成 `true`，所以默认是启用；只有显式写 `enableJobStarving: false` 才会关闭。它影响的是抢占/回收/分配阶段的 starving 判断，不是 allocate 的 gang 就绪判断（后者由 `JobReadyFn` 负责）。

一个 `minAvailable=4, replicas=8` 的作业，Running=4 时：

- `enableJobStarving: true`：`Ready=4`，不小于 4，不饥饿；
- `false`：hook 不存在，`Session.JobStarving` 返回 false，`preempt`/`reclaim` 不会把它当作抢占者。

### 6.2.2 minTaskMember：按角色设门槛

作业级 `minAvailable` 有一个盲区：它不区分角色。一个 `ps×1 + worker×8` 的训练作业，`minAvailable=5` 可能被满足为“5 个 worker 启动、ps 没启动”——数量够了，角色不对，训练仍然起不来。

Volcano 的解法是 **task 级 `minAvailable` → PodGroup `minTaskMember`**：

```yaml
spec:
  minAvailable: 5
  tasks:
    - name: ps
      replicas: 1
      minAvailable: 1
    - name: worker
      replicas: 8
      minAvailable: 4
```

控制器生成的 PodGroup：

```yaml
spec:
  minMember: 5
  minTaskMember:
    ps: 1
    worker: 4
```

gang 插件的检查从“一个总数”变成“一个总数 + 每个角色”：

```text
job 级：Ready >= MinMember
角色级：Ready(ps) >= 1 且 Ready(worker) >= 4
```

#### 生效条件：作业门槛必须覆盖角色门槛之和

这是最容易踩的坑。上游的实现是：

```text
if job.MinAvailable < Σ task.MinAvailable:
    # 跳过所有角色检查，只按总数判断
    return true for task checks
else:
    # 逐角色检查 CheckTaskValid / CheckTaskReady / CheckTaskPipelined
```

也就是说，把上面的 `minAvailable` 从 5 改成 4，`ps:1 / worker:4` 的角色门槛会**静默失效**；调度器只要求任意 4 个 Pod 就绪。配置规则：

```text
job.minAvailable >= ps.minAvailable + worker.minAvailable + ...
```

如果希望“1 个 ps + 4 个 worker 就算最小可用”，就把作业门槛设成 5，而不是 4。

#### 和 SubGroupPolicy 的关系

`minTaskMember` 是“每个角色各自一个最低数”。v1.14+ 的 `subGroupPolicy` 更进一步，支持“子组”级别的 gang 与拓扑。注意入口不同：**Volcano Job 写 `tasks[].partitionPolicy`，控制器生成 PodGroup `subGroupPolicy`；只有手动创建 PodGroup 才直接写 `subGroupPolicy`**。

- 一个 PodGroup 可以按标签或 `matchLabelKeys` 分组；
- 每个子组有 `subGroupSize`；
- 只有 `minSubGroups > 0` 时，至少这么多个子组能满足拓扑约束才调度；默认 0 表示不做子组 gang 门控；
- 子组内还可以再套 `networkTopology`。

PodGroup API 的注释说 **SubGroupPolicy 覆盖 minTaskMember 的能力，是长期演进方向；不建议两者同时使用**。但这句话成立的前提是 `minSubGroups > 0`；否则它只是拓扑分组，不承担角色/子组 gang。新设计优先用 SubGroupPolicy，存量作业继续用 `minTaskMember`。字段细节见 11.6。

#### 与 `enableJobStarving` 的交互

`enableJobStarving` 决定是否注册 gang 的 `JobStarvingFn`（默认 true；false 时 preempt/reclaim 不把作业当作饥饿）；`minTaskMember` 决定“就绪”的角色下限。两者叠加时可能出现：

```text
作业 Running（总数已满足 minAvailable）
但某个角色一直没达标 → 角色级检查失败 → 新一轮 allocate 可能无法为该作业提交新的绑定
```

排障时不要只看 `kubectl get vcjob` 的 Running，必须看 PodGroup：

```bash
kubectl -n <ns> get podgroup | grep <job>        # 名字是 <job>-<uid>
kubectl get podgroup <podgroup-name> -o yaml | yq '.spec.minTaskMember, .status'
kubectl get pods -l volcano.sh/job-name=<job> -o wide
```

#### 手动创建 PodGroup 时

非 Volcano 工作负载（裸 Deployment/StatefulSet）也可以直接创建 PodGroup 并写 `minTaskMember`，此时要保证：

1. Pod 的 `schedulerName` 是 `volcano`；
2. Pod 带 `scheduling.k8s.io/group-name: <podgroup>` 注解；
3. Pod 带 `volcano.sh/task-spec: <角色名>` 标签（或同名注解），调度器才能把 Ready 数归到正确的角色；
4. `spec.minMember >= Σ spec.minTaskMember`，否则上游会走“作业门槛低于角色门槛之和”的跳过分支，角色检查不生效。通常把 `minMember` 设成角色门槛之和。

缺任何一个，角色计数都可能是 0，表现为“资源够但 gang 永远不 Ready”。

#### MiniVolcano 如何落地

Mini 把这条链压缩成四个位置：

```text
api.TaskSpec.MinAvailable
  → Job.MinTaskMember() / Job.SumTaskMinAvailable()
  → cache.SubmitJob 写入 PodGroup.MinTaskMember
  → gang.JobReady + actions.allocateJob + controller.taskGatesReady
```

判定函数是 `Job.RoleGatesActive()`：只有 `minAvailable > 0` 且 `minAvailable >= Σ task.minAvailable` 时角色门槛才生效；低于之和时与上游一样跳过，不会把作业锁死。`MinResources` 也先按角色门槛预留，再按声明顺序补足，避免准入预算被某个角色独占。`Statement` 提供 `ReadyCountByTask`，所以同一个 `allocate` 事务里能看到前一个 Pod 的试绑定；8 个 worker 全部就绪、但 master 没就绪时，`allocate` 会丢弃整个 Statement。

可以跑：

```bash
cd mini-volcano
go run ./cmd/minivolcano -scenario task-gang -explain default/train
```

输出会显示 `READY/MIN 5/5`，而节点上只有 5 个 Pod：1 个 master + 4 个 worker，剩余 4 个 worker 仍是 Pending。场景刻意把 worker 声明在 master 前面，用来验证 allocate 的两轮分配与 `gang` 的 `TaskOrderFn`：第一轮只满足每个 active 角色的门槛，第二轮才按“有门槛 > 无门槛、门槛小 > 门槛大、声明顺序、副本序号”填额外副本。否则容量恰好等于 `minAvailable` 时，调度器可能把 5 个槽全给 worker，master 永远排不上，每轮都回滚。把 `job.minAvailable` 改成 4，角色门槛就失效——这正是上游的边界，也是 6.2.2 反复强调“作业门槛必须覆盖角色门槛之和”的原因。

## 6.3 事务：gang 是怎么实现的

Volcano 的 allocate action 不会“边分配边绑定”。它为作业创建一个 **Statement**：

```text
for task in job.tasks:
    node = selectNode(task)
    if node == nil:
        statement.Rollback()
        return false
    statement.Allocate(task, node)

if job.ReadyCount() >= minAvailable and taskGatesSatisfied(job):
    statement.Commit()
else:
    statement.Rollback()
```

这解释了三个现象：

1. **为什么 Pending 的 Pod 可能已经被“试分配”过**：Statement 是内存操作，失败会回滚。
2. **为什么 gang 作业不会出现“部分绑定”**：提交前所有任务必须在同一个事务里成功。
3. **为什么调度器需要快照**：事务基于 Session 的 cache 副本，不污染全局缓存。

### 6.3.1 Pipelined

Volcano 有一个内部状态 `Pipelined`：任务已经被预分配，但还在等待 gang 凑齐。gang 插件把 Pipelined 计入 Ready，因此：

- 先分配的任务进入 Pipelined；
- 当 Pipelined + Running + Allocated ≥ minAvailable 时，整个作业可以提交；
- 如果最终失败，Pipelined 任务回滚。

这是 Volcano 处理“大 gang 分批试分配”的机制。

## 6.4 一个完整的 gang 轨迹

集群：1 节点，4 张 GPU。

提交：

```yaml
# filler: 2 个 Pod，每个 2 GPU
minAvailable: 2
replicas: 2
# train: 4 个 Pod，每个 1 GPU
minAvailable: 4
replicas: 4
```

Session 1：

```text
filler: 试分配 2 个 Pod → 占用 4 GPU → Ready=2 → Commit
train:  试分配 0 个 Pod（无空闲）→ Ready=0 < 4 → Rollback
```

结果：filler Running，train 的 Job 保持 Pending、PodGroup 处于 Inqueue。

filler 完成后：

```text
空闲 4 GPU
train: 试分配 4 个 Pod → Ready=4 → Commit
```

结果：train Running。

如果 filler 和 train 同优先级，train 不能抢占 filler；如果 train 优先级更高，preempt action 可以驱逐 filler 的 Pod 来凑齐 train 的 gang。第 9 章会展开。

## 6.5 gang 与队列的交互

gang 不是孤立的。一个作业能否凑齐，取决于：

- 队列 capability 是否还有空间；
- 队列 deserved 是否被其他队列借用；
- 节点是否有足够的碎片化资源；
- 拓扑约束是否允许所有任务落在同一域；
- 优先级是否允许抢占。

因此“gang 作业 Pending”可能不是 gang 本身的问题，而是队列/拓扑/优先级的问题。

## 6.6 gang 粒度抢占（v1.15+）

v1.15 引入 **Gang-Aware Preemption & Reclamation（Alpha）**：

- 新增 `gangPreempt`、`gangReclaim` action；
- 抢占以作业为粒度评估，而不是逐 Pod；
- 受害者排序优先“多余副本”，保护关键副本；
- 在真正驱逐前做放置模拟，确认抢占者 gang 能启动；
- 与 HyperNode 拓扑集成。

这解决了经典问题：逐 Pod 抢占可能驱逐了一堆受害者，结果抢占者仍然凑不齐 gang，白抢。

启用方式（示意）：

```yaml
actions: "enqueue, allocate, gangpreempt, gangreclaim, backfill"
```

具体 action 名称与参数以目标版本为准；v1.16 的 roadmap 仍在讨论是否合并回传统 preempt/reclaim。

## 6.7 碎片化：gang 的隐形杀手

假设 4 个节点，每个 2 GPU，共 8 GPU：

```text
node-a: 1 free
node-b: 1 free
node-c: 1 free
node-d: 1 free
```

一个需要 4 个 1-GPU Pod 的 gang 能跑吗？能，因为每个节点有 1 张空闲卡，4 个 Pod 可以分散。但一个需要 2 个 2-GPU Pod 的 gang 不能跑，因为没有任何节点有连续 2 张空闲卡。

这就是**资源碎片**：总空闲量够，但形状不匹配。缓解手段：

- binpack 插件把 Pod 聚拢，减少碎片；
- 预留/guarantee 给大 gang 留出连续资源；
- 拓扑感知把 gang 限制在同一个域，避免跨域碎片；
- 监控“最大可调度 gang 大小”，而不只是总空闲 GPU。

## 6.8 诊断 gang Pending

典型报错：

```text
0/0 tasks in gang unschedulable: pod group is not ready, 4 minAvailable
Reason: NotEnoughResources
```

排查顺序：

1. **PodGroup phase**：是 Pending 还是 Inqueue？Pending 说明 enqueue 没过；Inqueue 说明在等 allocate。
2. **minResources vs 队列可用**：`kubectl describe podgroup` 看 `minResources`，对比 Queue 的 capability/allocated。
3. **节点空闲形状**：`kubectl describe node` 看每张卡是否被占，是否有污点。
4. **优先级/抢占**：是否有更低优先级的作业占着资源，但 preempt action 没启用？
5. **拓扑约束**：hard 模式下是否没有单个域能容纳整个 gang？
6. **调度器日志**：`kubectl logs deploy/volcano-scheduler -n volcano-system | grep <job>`。

一个常见误判：看到 `minAvailable=4`、集群有 4 张空闲卡，就认为应该能跑。实际上可能：

- 4 张卡在 4 个节点，而作业要求每 Pod 2 卡；
- 队列 capability 只剩 2 卡；
- 4 张卡中有 2 张被 reservation 锁定；
- 节点有污点，Pod 没有 toleration。

## 6.9 MiniVolcano 的实现

MiniVolcano 的 gang 逻辑在 `internal/actions/allocate.go` 与 `internal/plugins/gang.go`：

```go
st := ssn.NewStatement()
for _, pod := range pending {
    node := bestNode(st, pod)
    if node == "" { continue }
    st.Bind(pod, node)
}
if st.ReadyCount(job) >= minAvailable {
    st.Commit()
} else {
    st.Discard()   // 回滚所有 tentative bindings
}
```

`Statement` 持有 cache 的深拷贝；`Commit` 把 session 的 cache 指向 trial，`Discard` 丢弃。测试覆盖：

- `TestAllocateGangCommit`：4 卡 4 Pod 全部提交；
- `TestAllocateGangRollback`：6 Pod 只有 4 卡，全部回滚；
- `TestAllocateCompletesPartialGang`：已有 2 个 Running，再分配 2 个后满足 minAvailable。

MiniVolcano 没有实现 Pipelined 的跨 Session 保留，也没有 gangPreempt；它用“整个作业一次事务”达到同样的 all-or-nothing 效果。

## 6.10 gang 的活性：为什么“资源够”却永远等

gang 保证的是安全性（不会部分启动），不保证活性（最终一定能启动）。一个 gang 作业可能因为下面任何一条永远 Pending，而集群看起来“还有空闲”。

### 6.10.1 六类停滞

| 类型 | 现象 | 根因 | 验证 |
|---|---|---|---|
| 容量型 | 队列 allocated < capability，但作业不分配 | `minAvailable` 的资源需求超过队列剩余 capability | `kubectl get queue -o yaml`，比较 `capability - allocated` 与 `minResources` |
| 形状型 | 总空闲 GPU 够，gang 仍 Pending | 空闲资源碎片化，没有节点能放下单个 Pod 或多个 Pod | 每节点最大连续空闲；`kubectl describe node` |
| 拓扑型 | 单节点够，hard 拓扑作业仍 Pending | 没有单个 HyperNode 域能容纳整个 gang | `kubectl get hypernode`，按域求空闲和 |
| 优先级型 | 资源被低优先级作业占着，gang 不抢 | `preempt` 未启用、优先级相同、`preemptionPolicy=Never` | scheduler 日志 `grep -i preempt` |
| 保护型 | 有可抢占对象，但驱逐被拒 | PDB `disruptionsAllowed=0`、gang 插件保护、`reclaimable=false` | `kubectl describe pdb`、插件配置 |
| 配置型 | 作业从未进入 allocate | `minAvailable` 大于总副本、queue 不存在/Closed、`schedulerName` 不是 volcano | `kubectl describe vcjob` 的 Events |

### 6.10.2 一个“资源够但形状不对”的例子

```text
4 个节点，每个节点 2 GPU，共 8 GPU 空闲
作业需要 4 个 Pod，每个 2 GPU，minAvailable=4
```

总空闲 8 ≥ 需求 8，但每个节点只能放一个 Pod，4 个 Pod 需要 4 个节点，恰好能放下。把作业改成 2 个 Pod、每个 4 GPU，就一个也放不下——总空闲仍是 8。再改成 8 个 Pod、每个 1 GPU，又能放下。**gang 的可行性取决于“资源形状 × Pod 形状 × 节点形状”三者匹配**，不是简单的总量比较。

### 6.10.3 诊断顺序

```text
1. 作业是否合法：minAvailable ≤ 总副本？queue 存在且 Open？schedulerName=volcano？
2. 队列是否还有空间：capability - allocated ≥ minResources？
3. 节点形状是否匹配：每个 task 的 requests 能否在某个节点上满足？
4. 拓扑域是否足够：hard 模式下有没有域能装下整个 gang？
5. 是否有可抢占对象：同队列、低优先级、未被 PDB/gang 保护？
6. 抢占后是否真能启动：启用 gangPreempt 的版本会做放置模拟；没有模拟的实现可能白抢。
```

把这条顺序写成脚本或 runbook，比每次凭经验猜快得多。

### 6.10.4 minAvailable、maxRetry 与 policies 的交互

三个参数经常被一起配置，但它们的语义层次不同：

| 参数 | 作用层 | 典型值 | 配错的后果 |
|---|---|---|---|
| `minAvailable` | 调度层 | 总副本（严格）或较小值（弹性） | 大于总副本被拒；小于总副本会“部分 Running” |
| `maxRetry` | 作业生命周期 | 3–10 | 太小：一次驱逐就 Failed；太大：坏作业反复重启 |
| `policies` | 事件 → 动作 | `PodEvicted -> RestartJob` | 不配：被驱逐后部分 worker 空转 |

一个常见的死循环：

```text
低优先级 gang 被高优先级作业抢占
  → PodEvicted -> RestartJob
  → 作业重新入队
  → 又凑不齐，或又被抢占
  → retryCount 增加，最终 Failed
```

如果业务希望“被抢占后等待而不是失败”，应该：

- 不要在 `PodEvicted` 上配 `RestartJob`，让 Pod 保持 Pending 等待重新调度；
- 或者显式设置较大的 `maxRetry`，并接受重启成本；
- 对训练作业配合 checkpoint，避免每次重启从头开始。

### 6.10.5 什么时候该放弃严格 gang

严格 all-or-nothing 的代价是“资源碎片时永远等”。以下场景可以考虑 `minAvailable < replicas`：

- 推理/服务型作业：少几个副本仍可服务；
- 弹性训练：框架支持动态 worker（PyTorch Elastic、Ray）；
- 批处理：部分完成也有价值。

但要注意：`minAvailable < replicas` 之后，作业会“部分 Running”，排障时不要把它误判为 gang 失效。第 4.4 节已经给出这个现象的解释。

## 6.11 本章小结

- gang 是“全有或全无”，`minAvailable` 是门槛；task 级门槛写入 `minTaskMember`，但只在作业门槛覆盖角色门槛之和时生效。
- Volcano 用 Statement 事务 + Ready 计数实现 gang，失败整体回滚。
- `enableJobStarving` 影响饥饿判定，进而影响抢占。
- gang 作业 Pending 可能是队列、碎片、拓扑或优先级问题。
- v1.15 的 gangPreempt 把抢占也提升到作业粒度。

下一章拆开调度器框架：Session、actions、plugins、tiers 如何协作。

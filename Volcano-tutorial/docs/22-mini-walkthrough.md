# 第 22 章 MiniVolcano 实现走读

本章按“数据 → 快照 → 会话 → 插件 → 动作 → 控制器 → 指标 → CLI”的顺序走读代码。所有路径相对 `mini-volcano/`。

## 22.1 Resource：多维资源的算术

`internal/api/resource.go`：

```go
type Resource map[string]int64

func (r Resource) LessEqual(o Resource) bool {
    for k, v := range r {
        if v > o[k] { return false }
    }
    for k, v := range o {
        if r[k] > v { return false }
    }
    return true
}
```

`LessEqual` 用并集比较，缺省维度为 0。节点容量检查用它是对的（节点的 allocatable 包含所有维度）。

但队列 capability 是**部分约束**：只写 GPU 时，不应该把 CPU 约束为 0。因此另有：

```go
func (r Resource) WithinLimits(limit Resource) bool {
    for k, max := range limit {
        if r[k] > max { return false }
    }
    return true
}
```

这个区别在开发中导致过一个真实 bug：hierarchy 场景的 org capability 只写了 GPU，用 `LessEqual` 把 CPU 当 0，导致所有作业无法入队。第 25 章会复盘。

## 22.2 Cache：快照与资源账本

### 22.2.1 对象表

```go
type Cache struct {
    Now          int64
    Nodes        map[string]*NodeState
    Pods         map[string]*api.Pod
    Jobs         map[string]*api.Job
    PodGroups    map[string]*api.PodGroup
    Queues       map[string]*api.Queue
    Reservations map[string]*api.Reservation
    HyperNodes   map[string]*api.HyperNode
    PriorityClasses map[string]int32
}
```

`Clone()` 深拷贝所有对象。所有 map 遍历在输出/排序时都经过排序，避免随机性。

### 22.2.2 SubmitJob：admission + 物化

```go
func (c *Cache) SubmitJob(j *api.Job) error {
    // 1. 解析 PriorityClass
    // 2. MutateJob：默认 schedulerName/queue/minAvailable
    // 3. ValidateJob
    // 4. 创建 Job + PodGroup
    // 5. MaterializeJob：为每个 task replica 创建 Pod
}
```

PodGroup 还会继承 `job.Spec.Labels`，这些 labels 是 `group-topology-affinity` 选择对端的依据。

`MinAvailableSet` 解决了一个建模问题：

- 未设置 → 默认等于总副本（严格 gang）；
- 显式 0 → 关闭 gang（Mini 行为；上游 webhook 会把 0 补成 Σ task 有效 minima，不能用 0 关闭）。

二者在 YAML 里都可能是 `0`，必须有额外位区分。

### 22.2.3 Bind / Evict / Finish

```go
func (c *Cache) Bind(podUID, nodeName string, tick int64) error {
    // 检查 Pending、节点存在、可调度、资源足够
    // 如果有 NUMANodes，选一个能容纳的 NUMA 节点
    // 更新 pod.NodeName/Phase=Allocated
    // node.Allocated += requests
    // node.Pods[podUID] = pod
}
```

```go
func (c *Cache) Evict(podUID string, tick int64) error {
    // 释放节点资源
    // pod.Phase=Pending, NodeName="", Evicted=true
}
```

```go
func (c *Cache) Finish(podUID string, success bool, tick int64) error {
    // 释放节点资源
    // pod.Phase=Succeeded/Failed
    // 保留 NodeName 作为放置历史
}
```

保留 `NodeName` 是一个细节：Kubernetes 的 Pod 完成后仍保留 `spec.nodeName`。早期实现清空了它，导致拓扑/异构场景无法验证“Pod 曾经落在哪个节点”。这个 bug 在第 25 章记录。

### 22.2.4 IdleFor：预留的扣减

```go
func (c *Cache) IdleFor(nodeName, queue, jobUID string, tick int64) api.Resource {
    idle := node.Allocatable - node.Allocated
    for _, r := range c.Reservations {
        if !r.ActiveAt(tick) { continue }
        if !contains(r.NodeNames, nodeName) { continue }
        if r.Queue == queue && (r.Job == "" || r.Job == jobUID) { continue }
        remaining = r.Resources - ownerUsedOnNode   // 只扣尚未使用的预留
        idle = idle - remaining
    }
    return idle
}
```

它把“预留”从节点容量中扣除，但不改变 `node.Allocated`。这样：

- 预留不显示为“已分配”；
- 其他队列看不到这部分资源；
- 预留队列自己不受影响。

## 22.3 Framework：Session、Hook、Statement

### 22.3.1 NewSession

```go
func NewSession(c *cache.Cache, now int64, cfg Config, plugins []PluginInstance) *Session {
    s := &Session{Cache: c, Now: now, ...}
    for _, p := range plugins {
        s.tier, s.plugin = p.Tier, p.Plugin.Name()
        p.Plugin.OnSessionOpen(s)
    }
    s.sortHooks()
    return s
}
```

- 每个插件打开时注册 hook；
- `sortHooks` 按 tier + 注册顺序排序；
- 插件实例跨 Session 复用（fairshare 依赖）。

### 22.3.2 Hook

15 种 hook：

```go
AddJobOrderFn / AddQueueOrderFn / AddTaskOrderFn
AddPredicateFn / AddNodeOrderFn
AddPreemptableFn / AddReclaimableFn
AddJobReadyFn / AddJobValidFn / AddJobEnqueueableFn
AddOverusedFn / AddAllocatableFn / AddJobStarvingFn
AddVictimTasksFns / AddDisruptableFn
```

组合规则：

- Order：第一个非 0 胜出；
- Predicate：全部通过；
- NodeOrder：分数相加；
- 布尔：AND；
- Overused：OR。

### 22.3.3 Predicate 与 NodeScore

```go
func (s *Session) Predicate(pod *api.Pod, node *cache.NodeState) error {
    // 1. 节点可调度
    // 2. 内置 fit：requests <= IdleFor(node, queue, job)
    // 3. 插件 predicates
}
```

```go
func (s *Session) NodeScore(pod *api.Pod, node *cache.NodeState) (float64, error) {
    // 所有 NodeOrderFn 求和
}
```

内置 fit 保证任何插件组合都不会把 Pod 放到容量不足的节点。

### 22.3.4 Statement

```go
func (s *Session) NewStatement() *Statement {
    return &Statement{ssn: s, trial: s.Cache.Clone()}
}

func (st *Statement) Bind(podUID, nodeName string) error {
    // trial.Bind + 记录事件
}

func (st *Statement) Commit() {
    st.ssn.Cache = st.trial
    // 事件写入 session
}

func (st *Statement) Discard() {
    // 丢弃 trial
}
```

`Predicate`/`NodeScore` 在 trial 上执行时，会临时把 `ssn.Cache` 指向 trial：

```go
func (st *Statement) withTrial(fn func() error) error {
    old := st.ssn.Cache
    st.ssn.Cache = st.trial
    defer func() { st.ssn.Cache = old }()
    return fn()
}
```

这让 task-topology 等插件在同一个 Statement 内看到之前绑定的 Pod。

## 22.4 Plugins

### 22.4.1 gang

```go
ssn.AddJobReadyFn("gang", func(job *api.Job) bool {
    if ssn.ReadyCount(job.UID) < int(job.Spec.MinAvailable) {
        return false
    }
    if !job.RoleGatesActive() {
        return true
    }
    byTask := ssn.Cache.ReadyCountByTask(job.UID)
    for _, t := range job.Spec.Tasks {
        if want := t.EffectiveMinAvailable(); want > 0 && byTask[t.Name] < int(want) {
            return false
        }
    }
    return true
})
```

`ReadyCount` 统计 `Allocated/Pipelined/Bound/Binding/Running/Succeeded`；`ReadyCountByTask` 是同一口径按 `Pod.Task` 分组。`JobStarving` 用 `RunningCountByTask`，避免把“已分配未运行”的 Pod 当成已在跑。

`allocate` 不能直接调用 `ssn.JobReady`，因为后者读的是 session cache，看不到同一个 Statement 里的试绑定。它用的是 `st.ReadyCount()` 加 `taskGatesSatisfied(st, job)`，在 trial cache 上做同样判定；提交失败时 `gangReason` 会指出具体是哪个角色缺几个。

场景 `task-gang` 验证了这一点：节点只有 5 个 CPU 槽，作业有 1 master + 8 worker，`minAvailable=5`，而且刻意把 worker 声明在 master 前面。allocate 分两轮：第一轮只满足每个 active 角色的门槛，第二轮才按 `TaskOrderFn`（有门槛 > 无门槛、门槛小 > 门槛大、声明顺序）填额外副本；否则 5 个槽会被 worker 占满，master 永远排不上，每轮都回滚。最终 1 master + 4 worker 运行，4 个 worker Pending；如果没有角色门槛，5 个 worker 也会被误判为 gang 满足。

### 22.4.2 proportion

```go
deserved = total * weight / sumWeight
if capability != nil && deserved > capability { deserved = capability }
if guarantee > deserved { deserved = guarantee }
```

队列顺序：

```go
share = max(allocated[k] / deserved[k])
```

`share` 小的队列先分。`Overused` = allocated > deserved。`Reclaimable` = 队列 reclaimable && overused。

### 22.4.3 capacity

递归计算父子 deserved：

```go
if parent == nil { parentDeserved = total }
else { parentDeserved = compute(parent) }
deserved = parentDeserved * weight / siblingWeight
```

`allocatable` 同时检查：

- 本队列的 effective capability；
- 祖先的 capability；
- 祖先的状态。

`allocatedTree` 汇总子树用量。

### 22.4.4 drf

```go
demand = JobAllocated + Σ pending pod requests
share = max(demand[k] / total[k])
```

按 share 升序。它没有使用历史，历史由 fairshare 负责。

### 22.4.5 fairshare

```go
if dt > 0 {
    decay = 0.5^(dt / halfLife)
    usage[q] *= decay
    usage[q] += share(q) * dt
}
```

按 usage 升序影响队列/作业顺序。

### 22.4.6 network-topology-aware

Session 打开时选域：

```go
for each job with hard topology:
    tier = highestTierAllowed
    domains = HyperNodesAtTier(tier)
    for d in domains:
        cap = Σ IdleFor(node in d)
        if cap >= job.MinResources: candidate
    choose smallest candidate
```

Predicate 限制节点必须在域内。Soft 模式用 NodeOrder 加分。

### 22.4.7 reservation / tdm / sla

- reservation：活动期内只扣减预留中尚未被 owner 使用的部分，预留作业排序优先；
- tdm：preemptable 作业只能进 revocable 节点且必须在窗口内；
- sla：等待超预算的作业排序优先。

### 22.4.8 rescheduling

`rescheduling` 不注册 Predicate/NodeOrder，只注册 `VictimTasksFn`：

```go
ssn.AddVictimTasksFns("rescheduling", func() []*api.Pod {
    if interval > 0 && now-lastTick < interval { return nil }
    switch strategy {
    case "fragmentation":
        // 低利用率非空节点 → 作为源节点，交给 binpack 重打包
    case "lowNodeUtilization":
        // 高利用率节点 → 低利用率节点，负载均衡（上游方向）
    }
    // 过滤掉驱逐后会跌破 minAvailable 的 Pod
    return victims
})
```

四个细节：

1. **两种策略方向相反**：`lowNodeUtilization` 从热节点驱逐到冷节点（配合 nodeorder/spread）；`fragmentation` 从半满节点驱逐到更忙节点（配合 binpack）。场景 `shuffle` 用的是后者。
2. **只选一个源节点**：一次驱逐整个集群会让 shuffle 不可解释，也无法做渐进式整理；如果源节点的 Pod 全被 gang 保护，会尝试下一个候选，而不是永久 no-op。
3. **gang 保护按“仍占节点的 Pod”计数**：不能用 `ReadyCount`，因为它把 Succeeded 也算进去；否则可能驱逐掉最后两个 Running 副本，留下一个“Running 但零运行”的作业。
4. **确定性**：先按节点名排序，再按 `Pod.Name + UID` 排序；只比 Name 会在跨 namespace 同名作业时退化。不能依赖 map 遍历。

实现见 `internal/plugins/rescheduling.go`。

### 22.4.9 治理插件：pdb / resourcequota / nodegroup

三个插件分别回答“能不能中断”“能不能入队”“能不能放这个节点”：

```text
pdb            PreemptableFn / ReclaimableFn / DisruptableFn
resourcequota  JobEnqueueableFn
nodegroup      PredicateFn
```

**pdb** 读取简化 PDB 的 selector，统计匹配 Pod 的 `available` 与 `desired`：

```go
if budget.HasMin && available-1 < minAvailable { return false }
if budget.HasMax && available-1 < desired-maxUnavailable { return false }
```

它同时注册三个 hook，是因为三种主动中断路径的上下文不同：preempt/reclaim 有“抢占者”，shuffle 没有。`DisruptableFn` 只回答“这个受害者本身能不能动”。

**resourcequota** 在 enqueue 时用 `NamespaceAdmittedUsage` 计算已准入作业的请求总和，再加上当前作业的 `MinResources`，与每个 quota 的 `Hard` 做 `WithinLimits`。它只统计已准入作业，否则所有 Pending 作业都会互相抬高用量，准入结果依赖提交顺序。

**nodegroup** 在 Predicate 中检查：

```go
group := node.Node.Labels[api.NodeGroupLabel]
if len(queue.Spec.NodeGroup.Names) > 0 && !contains(names, group) { reject }
if cap, ok := queue.Spec.NodeGroup.Capability[group]; ok {
    if used + pod.Requests > cap { reject }
}
```

因为 Predicate 在 Statement 的 trial cache 上运行，一次 allocate 绑定多个 Pod 时，后一个 Pod 能看到前一个的占用，不会突破组容量。

三者与 shuffle 的交汇点在 PDB。`shuffle` 先做 gang 安全检查，再做累计 PDB 检查：

```go
victims = gangSafeVictims(ssn, ssn.VictimTasks())
victims = disruptableVictims(ssn, victims)  // Statement 内逐个 Evict + 检查
if !canRepack(ssn, victims) { return nil }
```

`disruptableVictims` 用 trial cache 重放驱逐，是为了让第 N 次 PDB 检查看到前 N-1 次驱逐的结果。没有这一步，4 副本、minAvailable=3 的 PDB 会放行 3 个“各自看起来安全”的受害者，最终只剩 1 个可用副本。对应测试是 `TestDisruptableVictimsAppliesPDBCumulatively`。

### 22.4.10 group-topology-affinity

`group-topology-affinity` 对应 v1.16 的 PodGroup 级反亲和。它不改变单个作业的域选择，而是约束**不同 PodGroup 之间**能否落在同一个 HyperNode 域：

```go
// 成对评估：既看自己声明的规则，也看别人声明、选中自己的规则。
for _, declarer := range sortedPodGroups(ssn) {
    for _, term := range declarer.TopologyAffinity.PodGroupAntiAffinity {
        domain := ssn.Cache.DomainOfNodeAtTier(node.Node.Name, term.TopologyTier)
        if declarer.UID == pg.UID {
            for _, peer := range sortedPodGroups(ssn) {
                if peer.UID != pg.UID &&
                   labelsMatch(peer.Labels, term.MatchLabels) &&
                   hasPlacedPods(ssn, peer) &&
                   podsInDomain(ssn, peer, domain, term.TopologyTier) {
                    // required: Predicate 失败；preferred: penalty += term.Weight
                }
            }
        } else if labelsMatch(pg.Labels, term.MatchLabels) {
            if podsInDomain(ssn, declarer, domain, term.TopologyTier) {
                // 对端声明方已经占了这个域
            }
        }
    }
}
```

四个实现细节：

1. **成对评估**：规则既从声明方检查匹配的对端，也从被选中方检查声明方。只在一边写规则时，另一边也不能绕过。上游按对象逐一评估，Mini 更严格。
2. **trial cache 可见性**：Predicate 在 Statement 的 trial cache 上运行，同一轮 allocate 中先提交的 PodGroup 对后一个可见；否则两个组会同时“看到空域”而落到一起。
3. **数字 tier 与 namespace**：Mini 的 HyperNode 没有 `tierName`，所以用 `TopologyTier`；对端默认限定在同 namespace，和上游 `podGroupSelector` 的默认作用域一致，但不支持 namespace selector。省略 tier 时按 tier 1 处理。
4. **只在一轮 allocate 内生效**：Predicate/NodeOrder 检查 `ssn.Action == "allocate"`；backfill、preempt、reclaim、shuffle 的放置探测不会应用反亲和。preferred 惩罚按“对端”去重，双方都声明时只计一次。

场景 `group-topology`：rack-1 有两个节点、rack-2 有一个节点；只有 serve-a 声明规则，serve-b 只带匹配标签。serve-a 落在 rack-1 后，serve-b 仍被拒绝进入 rack-1 的空节点，最终落到 rack-2。测试 `TestGroupTopologyRequiredAntiAffinity` 验证单边规则的对称约束、无匹配对端时的 no-op、backfill 绕过；`TestGroupTopologyPreferredPenalty` 验证双方声明只计一次、跨 namespace 不计分、插件 `weight` 参数。

## 22.5 Actions

### 22.5.1 enqueue

```go
if queue.State != Open: record; continue
if err := ssn.JobValid(job); err != nil: record; continue
if !ssn.JobEnqueueable(job): record; continue
pg.Phase = Inqueue
job.Status.Phase = Inqueue
```

Mini 默认不做 idle 检查（`EnqueueIdleCheck=false`），以便演示“先入队、再抢占”。真实 Volcano 的 enqueue 有 idle/overcommit 逻辑，Mini 用 `overcommit` 插件模拟。

### 22.5.2 allocate

```go
st := ssn.NewStatement()
// 第一轮：每个 active 角色先拿到自己的门槛
for task in job.tasks where task.minAvailable > 0:
    while ready(task) < task.minAvailable:
        bind next pending pod of task
// 第二轮：按 TaskOrder 填额外副本
for pod in pending:
    bind pod if a node is feasible
if gangSatisfied(st, job):
    st.Commit()
else:
    st.Discard()
```

`bestNode`：

```go
for node in sorted nodes:
    if st.Predicate(pod, node) != nil: continue
    score = st.NodeScore(pod, node)
    pick max, tie by name
```

### 22.5.3 preempt

```go
victims = same queue, lower priority, different job
for pod in pending:
    while no node and victims remain:
        st.Evict(victim)
    st.Bind(pod, node)
if ready >= minAvailable && evicted > 0: Commit
else: Discard
```

只处理同队列，符合 Volcano 的 preempt 语义。

### 22.5.4 reclaim

```go
for target queue with allocated < deserved:
    victims = other queues, overused, reclaimable
    evict until victim queue <= deserved
    bind target job
    if success: Commit
```

### 22.5.5 backfill

```go
for pending pod with zero requests:
    bind best node
    commit per pod
```

### 22.5.6 shuffle

```go
victims := gangSafeVictims(ssn, ssn.VictimTasks())
if len(victims) == 0: return
if !canRepack(ssn, victims): return   // 干跑：每个 Pod 都有严格更优的目标
st := ssn.NewStatement()
for v in victims: st.Evict(v.UID, "shuffle")
st.Commit()
```

它和 preempt 的区别：

- 不比较优先级，不要求抢占者存在；
- 不绑定任何 Pod，只驱逐；
- 受害者来自插件，而不是 action 内部遍历；
- 下一轮 `allocate` 才决定 Pod 去哪。

两个安全增强：

1. **合并后的 gang 校验**：`VictimTasks` 会把同一 tier 的多个供应商合并，单个供应商的“再驱逐一个还安全”不代表合起来安全；action 层再按作业扣减一次。
2. **目标可行性干跑**：在一个临时 Statement 里先驱逐、再尝试绑定到分数严格更高的非源节点；任何一步失败就放弃本轮。没有这一步，binpack 会把 Pod 放回原节点，形成每 tick 一次的活锁。

测试覆盖：

- `TestShuffleEvictsThenAllocateRepacks`：驱逐数量、事件、重打包后账本守恒；
- `TestShuffleSkipsWhenNoBetterTarget`：没有更优目标时拒绝驱逐；
- 场景 `shuffle`：端到端的“碎片 → 疏散 → 单节点装满”。

## 22.6 Controller

### 22.6.1 Sync 顺序

```go
func (m *Manager) Sync(now int64) {
    m.syncCron(now)
    m.syncFlows(now)
    for job := range jobs { MaterializeJob(job) }
    for pod := range sorted(pods) { advancePod(pod) }
    for pod := range sorted(pods) { handlePodEvents(pod) }
    recomputeStatuses()
}
```

顺序很重要：

- 先创建 Cron/Flow 的新 Job；
- 再推进 Pod（Allocated→Running→Succeeded/Failed）；
- 再处理策略；
- 最后回写状态。

### 22.6.2 策略

```go
policy = task policy || job policy || default
switch policy.Action {
case RestartTask: ResetForRestart(pod); retries++
case RestartJob: reset all pods; retries++
case AbortJob/TerminateJob: stopJob
case CompleteJob: completeJob
}
```

默认：

- `PodFailed` → `RestartTask`；
- `PodEvicted` → 不重启（Pod 保持 Pending，等待重新调度）；
- `TaskCompleted` → 无动作。

这与真实 Volcano 的默认策略可能不同，属于 Mini 的简化；生产语义由用户显式配置。

### 22.6.3 Job 状态

```go
if all pods succeeded: Completed
if minSuccess set && succeeded >= minSuccess: Completed + terminate the rest
if all terminal with failures and no running: Failed
if ready >= minAvailable && roleGatesReady && running > 0: Running
if pg Inqueue: Inqueue
else: Pending
```

`minSuccess` 的终止逻辑在 `completeJob` 里：占用节点的 Pod 走 `Finish(true)`，还没启动的 `Pending` Pod 直接标成 `Succeeded` 并写 `FinishTick`。这样作业完成后不会留下 4 个永远 Pending 的副本。场景 `min-success` 断言了 4 个 Pod 全部 Succeeded、节点空闲回到 2 CPU。

`roleGatesReady` 与 allocate 的 `taskGatesSatisfied` 共用 `Job.RoleGatesActive()`；控制器不能只看 `ReadyCount`，否则 5 个 worker 就会把 Job 误标成 Running。

### 22.6.4 PodGroup/Queue

- PodGroup 的 running/succeeded/failed 来自 Pod 计数；
- Queue 的 allocated 来自 cache；
- Queue 的 pending/inqueue/running 作业计数来自 Job 状态。

## 22.7 Metrics

`internal/metrics` 是一个小型 Prometheus 文本 registry：

- counter：`Add/Inc`；
- gauge：`Set`；
- `Reset`：清除某个 family，避免 gauge 的旧标签残留；
- `Render`：按名字/标签排序输出。

调度器每次 Run 后：

- 累计 sessions/events；
- `SetMetricsGauges` 先 Reset 再写入当前队列/节点/作业状态。

早期实现没有 Reset，导致“Pending”作业的 gauge 在作业 Running 后仍残留。第 25 章记录。

## 22.8 CLI

```bash
go run ./cmd/minivolcano -list
go run ./cmd/minivolcano -scenario basic
go run ./cmd/minivolcano -scenario priority -explain default/train-high
go run ./cmd/minivolcano -scenario proportion -what-if team-a/nvidia.com/gpu=2
go run ./cmd/minivolcano -scenario basic -metrics -events 20
go run ./cmd/minivolcano -all
```

`-all` 运行所有场景并检查断言，退出码非 0 表示失败。

## 22.9 复杂度与取舍

| 操作 | 复杂度 | 说明 |
|---|---|---|
| Snapshot | O(nodes+pods+jobs) | 每个 Session |
| Statement | O(cluster) | 每个作业尝试 |
| allocate | O(jobs×tasks×nodes×hooks) | 主要成本 |
| preempt | O(jobs×victims×nodes) | 受害者遍历 |
| reclaim | O(queues²×pods) | 简化 |
| rescheduling | O(nodes+pods) | 每个 Session 计算一次利用率 |
| shuffle | O(victims) + 一次 Statement 深拷贝 | 驱逐后由下一轮 allocate 重打包 |

教学实现的取舍：

- 深拷贝换正确性；
- 单线程换确定性；
- 全量遍历换可读性；
- 没有增量缓存/分片。

## 22.10 本章小结

- Resource 的 `WithinLimits` 与 `LessEqual` 是两种不同语义。
- Cache 负责账本与快照；Statement 负责事务。
- 插件通过 15 种 hook 组合；tier 决定顺序。
- Actions 是流水线；控制器是状态机。
- 治理插件的检查点不同：pdb 在中断前，resourcequota 在入队前，nodegroup 在放置前。
- shuffle 把“提名受害者”和“执行驱逐”分开，rescheduling 只负责前者，pdb 负责按累计状态过滤。
- 指标要先 Reset 再 Set，避免旧标签残留。
- 代码结构支持扩展，但性能不是目标。

下一章用 28 个场景验证这些语义。

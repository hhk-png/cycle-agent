# 第 7 章 调度器框架

## 7.1 一次 Session 的完整生命周期

Volcano 调度器不是“来一个 Pod 调一个”，而是周期性地打开 Session：

```text
1. Snapshot
   cache → 深拷贝/快照
2. OpenSession
   for plugin in tiers:
       plugin.OnSessionOpen(session)
   插件向 session 注册 JobOrderFn / PredicateFn / ...
3. Actions（按配置顺序）
   enqueue → allocate → backfill → preempt → reclaim → ...
4. CloseSession
   plugin.OnSessionClose(session)
   绑定结果写回 API Server / cache
```

为什么要快照？

- **一致性**：一次调度看到的是同一时刻的集群视图，不会因为并发事件看到“半个绑定”。
- **可回滚**：Statement 基于快照，失败时丢弃即可。
- **可测试**：给定快照和配置，调度结果是确定的。

## 7.2 Actions

Action 是调度流水线的一个阶段。每个 action 读取 session，调用插件，产生绑定/驱逐。

| Action | 作用 | 是否必需 |
|---|---|---|
| `enqueue` | 把 Pending 作业放入 Inqueue | 通常必需 |
| `allocate` | 为 Inqueue 作业分配资源 | 通常必需 |
| `backfill` | 调度 BestEffort/无请求 Pod | 可选 |
| `preempt` | 队列内优先级抢占 | 可选 |
| `reclaim` | 跨队列回收 | 可选 |
| `shuffle` | 按插件提名的受害者驱逐，下一轮重打包 | 可选 |
| `reserve` | 资源预留（旧） | 已废弃，被 SLA/预留插件取代 |
| `gangpreempt` | 作业粒度抢占（v1.15 Alpha） | 可选 |
| `gangreclaim` | 作业粒度回收（v1.15 Alpha） | 可选 |

Action 名在配置里是**全小写**的（如 `gangpreempt`，不是 `gangPreempt`）；写错会让 scheduler 启动时找不到 action。默认配置通常只有：

```yaml
actions: "enqueue, allocate, backfill"
```

生产环境如果要多租户公平/抢占，会加上 `preempt` 和 `reclaim`：

```yaml
actions: "enqueue, allocate, backfill, preempt, reclaim"
```

顺序很重要：

- `enqueue` 必须在 `allocate` 前，否则作业没有 Inqueue 状态；
- `preempt` 通常在 `allocate` 后，先尝试正常分配，失败再抢；
- `reclaim` 在 `preempt` 后，先处理队列内，再处理跨队列；
- `shuffle` 可以放在 `allocate` 后（本轮只驱逐，下一轮重打包），也可以放在 `allocate` 前（同一轮先腾挪再分配，但要求受害者不会立刻被原样放回）；
- `backfill` 的常见位置是 `allocate` 之后、`preempt/reclaim` 之前（默认配置就是如此），也可以放到最后兜底；它只处理无请求/BestEffort Pod，位置取决于你希望它先填空闲还是最后收尾。

### 7.2.1 shuffle 的特殊性

`shuffle` 是唯一一个**不自己选节点、不自己选作业**的 action。上游实现直接逐条驱逐：

```text
victims = ssn.VictimTasks()      # 由 plugin 的 VictimTasksFn 提名
for v in victims: ssn.Evict(v)   # 失败记日志，没有整体回滚
```

MiniVolcano 用 `Statement` 包了一层，并增加“目标节点严格更优”的干跑检查；这是教学实现的安全增强，不是上游行为。

真正的“放哪”留给下一轮 `allocate`。这带来两个工程后果：

1. **shuffle 必须与重调度策略配套**。如果 `allocate` 的打分方向与驱逐方向相反，Pod 会被放回原处，形成抖动。
2. **shuffle 不等于 preempt**。它不比较优先级，也不保证被驱逐的 Pod 能重新运行；生产上要配合 PDB、gang、checkpoint 和 `PodEvicted -> RestartJob` 策略使用。

v1.15 的 `gangPreempt`/`gangReclaim` 则是另一种思路：抢占前先做放置模拟，确认抢占者 gang 能启动才驱逐。它们与传统 `preempt`/`reclaim` 的取舍见第 9 章。

## 7.3 插件

插件通过 `OnSessionOpen` 注册扩展函数。一个插件可以只注册自己关心的函数。

常见调度器插件：

| 插件 | 主要扩展点 | 作用 |
|---|---|---|
| `gang` | JobOrder/JobReady/JobPipelined/JobValid/JobStarving | all-or-nothing |
| `priority` | JobOrder/TaskOrder/Preemptable | 按优先级排序与抢占 |
| `drf` | JobOrder/TaskOrder | 主导资源公平 |
| `proportion` | QueueOrder/Allocatable/Overused/Reclaimable | 加权队列份额 |
| `capacity` | QueueOrder/Allocatable/Overused/Reclaimable | 层级队列份额 |
| `fairshare` | JobOrder/JobEnqueueable | namespace 历史用量公平（v1.16） |
| `sla` | JobOrder/JobEnqueueable | 等待时间 SLA |
| `predicates` | Predicate | 污点、选择器、节点条件 |
| `nodeorder` | NodeOrder | 节点打分（空闲、均衡） |
| `lifecycle` | NodeOrder | 按 Pod 生命周期相似度聚拢，辅助缩容（发行版扩展，非 upstream v1.16） |
| `binpack` | NodeOrder | 装箱/打散 |
| `resource-strategy-fit` | NodeOrder | 按资源维度组合策略（v1.13+） |
| `usage` | Predicate/NodeOrder | 基于真实利用率（metrics-server/Prometheus） |
| `numa-aware` | Predicate/NodeOrder | NUMA 约束 |
| `task-topology` | Predicate/NodeOrder | 任务角色亲和/反亲和 |
| `network-topology-aware` | Predicate/NodeOrder/JobOrder | HyperNode 拓扑 |
| `group-topology-affinity` | Predicate/NodeOrder | PodGroup 级拓扑反亲和（v1.16） |
| `deviceshare` | Predicate/NodeOrder | GPU/vGPU 共享账本 |
| `nodegroup` | Predicate/NodeOrder | 节点组亲和与分组资源上限（v1.16） |
| `tdm` | Predicate/NodeOrder/VictimTasks | 时分复用 |
| `rescheduling` | VictimTasks | 重调度：LNU 负载均衡 / 碎片整理（shuffle 的受害者来源） |
| `cdp` | Preemptable/Reclaimable（受害者过滤） | 冷却保护：刚启动的 Pod 在 `volcano.sh/cooldown-time` 内不被选为受害者 |
| `pdb` | Preemptable/Reclaimable/Disruptable（上游还参与 VictimTasks） | PodDisruptionBudget 保护 |
| `reservation` | JobOrder/Predicate | 预留资源（历史插件） |
| `overcommit` | JobEnqueueable | 超卖因子 |
| `resourcequota` | JobEnqueueable | 检查 namespace ResourceQuota |
| `conformance` | JobValid | 只管理 Volcano 作业 |
| `extender` | 多个扩展点 | 把决策委托给外部调度器 |

这张表覆盖了 v1.16 的主要内置插件，但不是全部；`pkg/scheduler/plugins` 下还有面向特定硬件/场景的插件。读配置时以二进制实际注册的 factory 为准：插件名写错会让 scheduler 启动失败，而不是静默忽略。

### 7.3.1 插件扩展点的“组合语义”

同一个扩展点被多个插件注册时，组合规则不同，这是最容易配错的地方：

| 扩展点 | 组合方式 | 典型陷阱 |
|---|---|---|
| JobOrder/QueueOrder/TaskOrder | 按 tier 顺序，第一个非 0 胜出 | priority 放前面会压住 DRF |
| Predicate | 全部通过（AND） | 任一插件过严，整个节点不可用 |
| NodeOrder | 分数相加 | nodeorder 与 binpack 方向相反会抵消 |
| JobReady/JobEnqueueable/Allocatable | AND | 一个插件说不行就不行 |
| Preemptable/Reclaimable | AND | PDB/gang 任一保护即不可驱逐 |
| Overused | OR | 任一插件判定超额即超额 |
| VictimTasks | 按 tier 短路，取第一个非空 tier | shuffle 只消费一个 tier 的提名 |

理解这张表，比记住“某个插件有什么参数”更重要。生产配置里 80% 的“插件没生效”都是组合语义问题。

### 7.3.2 插件接口

```go
type Plugin interface {
    Name() string
    OnSessionOpen(ssn *Session)
    OnSessionClose(ssn *Session)
}
```

一个最小插件：

```go
type MyPlugin struct{}

func (p *MyPlugin) Name() string { return "my-plugin" }

func (p *MyPlugin) OnSessionOpen(ssn *framework.Session) {
    ssn.AddJobOrderFn("my-plugin", func(l, r *api.Job) int {
        // 返回 -1 表示 l 优先，1 表示 r 优先，0 表示交给下一层
        return 0
    })
}

func (p *MyPlugin) OnSessionClose(ssn *framework.Session) {}
```

在 `plugins/factory.go` 注册后，即可在 config 中按名字启用。

### 7.3.3 tier 的意义

当多个插件都注册 JobOrderFn 时，tier 1 的插件有最终决定权，tier 2 只在平手时生效。这就是为什么“priority 与 drf 同层”会让 DRF 失效：priority 对任意不同优先级的作业都返回非 0，DRF 根本轮不到。

一个实用的分层原则：

- **tier 1：硬语义**。priority、gang、conformance、PDB 这类“不能妥协”的规则。
- **tier 2：公平与打分**。drf、fairshare、proportion、predicates、nodeorder、binpack。
- **tier 3：实验性策略**。灰度插件、租户定制规则；出问题时可整层移除。

tier 不是“越靠前越重要”的装饰，而是**短路顺序**。把公平插件放在硬语义前面，等于让公平覆盖优先级。

## 7.4 Tier 配置

```yaml
actions: "enqueue, allocate, backfill, preempt, reclaim"
tiers:
- name: tier-1
  plugins:
  - name: priority
  - name: gang
    enablePreemptable: true
  - name: conformance
- name: tier-2
  plugins:
  - name: drf
  - name: predicates
  - name: proportion
    enablePreemptable: true
  - name: nodeorder
  - name: binpack
    arguments:
      binpack.weight: "1"
```

这里有一个容易致命的区别：

- **plugin option**：与 `name` 同级，如 `enablePreemptable`、`enableHierarchy`、`enableJobStarving`；
- **arguments**：`arguments:` 下的键，通常是插件自己的参数，常带前缀，如 `binpack.weight`、`fairshare.halfLifeMinutes`、`capacity.DynamicResourceAllocationEnable`。

把 plugin option 写进 `arguments` 不会报错，但会被忽略；把 `arguments` 键写到 plugin 级同样不生效。本节的配置与附录 L 的配方都按这个规则书写。

解读：

- tier 1 放“硬语义”：优先级、gang、conformance。
- tier 2 放“公平与打分”：DRF、predicates、proportion、nodeorder、binpack。
- 同一 tier 内的插件按注册顺序参与比较；不同 tier 按 tier 顺序。

一个常见错误：把 `priority` 和 `drf` 放在同一 tier，然后困惑为什么 DRF 没有生效。priority 总是返回非 0（除非同优先级），DRF 根本轮不到。正确做法是 priority 在 tier 1，DRF 在 tier 2。

## 7.5 enqueue 的细节

enqueue 决定“哪些 Pending 作业可以进入 Inqueue”。它检查：

1. **JobValid**：作业是否合法（gang 的 minAvailable、conformance 的 schedulerName）。
2. **JobEnqueueable**：
   - 队列是否 Open；
   - 队列 capability 是否还装得下 `minResources`；
   - overcommit 因子是否允许；
   - 预留/层级队列是否允许。
3. 可选的 idle 检查：集群空闲资源是否足够。

一个容易误解的点：**Inqueue 不等于能运行**。Inqueue 只表示“允许参与本轮 allocate”。如果资源不够，作业会在 allocate 阶段失败并保持 Inqueue，等待下一轮。

## 7.6 allocate 的细节

allocate 的伪代码：

```text
for queue in queues ordered by QueueOrder:
    if queue not Open: continue
    for job in jobs ordered by JobOrder:
        if job not Inqueue: continue
        statement = new Statement
        for task in tasks ordered by TaskOrder:
            node = argmax(NodeScore) among nodes passing Predicate
            if node == nil: record reason; continue
            statement.Allocate(task, node)
        if job.Ready >= minAvailable:
            statement.Commit()
        else:
            statement.Rollback()
```

几个细节：

- **队列顺序**先于作业顺序；公平性首先体现在队列之间。
- **节点打分**是所有 NodeOrderFn 的和；平手时按节点名/UID 稳定排序。
- **Predicate 失败**会记录原因，供 `vcctl`/日志/诊断使用。
- **Statement** 只在 gang 满足时提交。

## 7.7 调度器配置的加载

配置在 `volcano-system/volcano-scheduler-configmap`：

```bash
kubectl -n volcano-system get configmap volcano-scheduler-configmap -o yaml
```

修改后需要重启 scheduler：

```bash
kubectl -n volcano-system rollout restart deploy/volcano-scheduler
```

配置解析失败会导致 scheduler CrashLoop。排障：

```bash
kubectl -n volcano-system logs deploy/volcano-scheduler --previous
```

## 7.8 扩展一个插件：从想法到上线

这一节用 MiniVolcano 里真实存在的 `group-topology-affinity`（第 22.4.10 节）走一遍完整流程。它比“写个 Hello World”更有代表性：插件需要读 PodGroup、HyperNode 和 Statement 的 trial cache，还要同时注册硬约束和软打分。

### 7.8.1 第一步：把需求翻译成扩展点

需求是“两组推理副本不能落在同一个超节点/机架”。先拆成可执行的判断：

| 问题 | 扩展点 | 失败时的表现 |
|---|---|---|
| 这个节点能不能放？ | `PredicateFn` | 作业 Pending，日志有原因 |
| 两个节点都不完美时选哪个？ | `NodeOrderFn` | 仍可调度，但布局次优 |
| 这个作业该不该入队？ | `JobEnqueueableFn` | Pending 在 Inqueue 之前 |
| 这个受害者能不能驱逐？ | `PreemptableFn` / `DisruptableFn` | 该抢不抢 / 该整理不整理 |
| 谁先调度？ | `JobOrderFn` | 公平/优先级问题 |

Mini 的 `group-topology-affinity` 同时注册 Predicate 和 NodeOrder：required 是硬约束，preferred 是软偏好。它在 Mini 里不注册 JobOrder，因为反亲和不应该改变作业之间的优先级；上游版本可能还注册排序/评分类 hook，配置时以源码为准。

一个常见错误是“所有逻辑都塞进 NodeOrder”。NodeOrder 只能改变偏好，不能阻止违规放置；required 语义必须用 Predicate。

### 7.8.2 第二步：定义数据模型

真实 Volcano 把规则放在 `PodGroup.spec.topologyAffinity.podGroupAntiAffinity`，并分成 `required` 与 `preferred` 两个列表。MiniVolcano 保留同样的概念，但把两者合并成一个带 `Weight` 的列表，并把 `topologyTierName` 简化为数字 `topologyTier`：

```go
type PodGroupAntiAffinityTerm struct {
    MatchLabels  map[string]string `json:"matchLabels"`
    TopologyTier int               `json:"topologyTier"`
    Weight       int32             `json:"weight,omitempty"` // 0=required, >0=preferred
}

type TopologyAffinitySpec struct {
    PodGroupAntiAffinity []PodGroupAntiAffinityTerm `json:"podGroupAntiAffinity,omitempty"`
}
```

数据模型要回答三个问题：

1. **规则挂在谁身上？** PodGroup，而不是 Pod。反亲和是组与组之间的约束。
2. **怎么选中对端？** 用 PodGroup 的 labels，不是 Pod labels。这也意味着 Job 的 labels 必须传播到 PodGroup。
3. **required 和 preferred 怎么区分？** Mini 用 `Weight == 0` 表示 required，`Weight > 0` 表示 preferred；上游用两个独立列表。迁移时要做一次结构转换，不能直接复制 YAML。

### 7.8.3 第三步：实现插件

插件骨架只有三个方法：

```go
type GroupTopologyAffinity struct{}

func (g *GroupTopologyAffinity) Name() string { return "group-topology-affinity" }

func (g *GroupTopologyAffinity) OnSessionOpen(ssn *framework.Session) {
    ssn.AddPredicateFn("group-topology-affinity", func(pod *api.Pod, node *cache.NodeState) error {
        pg := ssn.Cache.PodGroups[pod.Job]
        if pg == nil { return nil }
        conflicts := g.conflicts(ssn, pg, node.Node.Name, false)
        if len(conflicts) > 0 {
            c := conflicts[0]
            return fmt.Errorf("node %s in domain %s conflicts with %s", node.Node.Name, c.domain, c.peer)
        }
        return nil
    })

    ssn.AddNodeOrderFn("group-topology-affinity", func(pod *api.Pod, node *cache.NodeState) (float64, error) {
        pg := ssn.Cache.PodGroups[pod.Job]
        if pg == nil { return 0, nil }
        penalty := 0.0
        for _, c := range g.conflicts(ssn, pg, node.Node.Name, true) {
            penalty += float64(c.weight)
        }
        return -penalty, nil
    })
}

func (g *GroupTopologyAffinity) OnSessionClose(ssn *framework.Session) {}
```

最容易写错的是“对端在哪里”。完整实现遍历所有 PodGroup：

```go
for _, declarer := range sortedPodGroups(ssn) {
    for _, term := range declarer.TopologyAffinity.PodGroupAntiAffinity {
        domain := ssn.Cache.DomainOfNodeAtTier(nodeName, term.TopologyTier)
        if declarer.UID == pg.UID {
            // 自己声明的规则：匹配的对端不能在同一域
            for _, peer := range groups {
                if labelsMatch(peer.Labels, term.MatchLabels) &&
                   podsInDomain(ssn, peer, domain, term.TopologyTier) {
                    conflict(peer)
                }
            }
        } else if labelsMatch(pg.Labels, term.MatchLabels) {
            // 对端声明的规则选中了我：对端不能在同一域
            if podsInDomain(ssn, declarer, domain, term.TopologyTier) {
                conflict(declarer)
            }
        }
    }
}
```

三个细节决定正确性：

- **对称评估**：只在 A 上写规则，B 后调度时也要被挡住。否则“先到先占”会绕过反亲和。
- **trial cache**：Predicate 在 Statement 的 trial cache 上执行，所以同一轮 allocate 里后一个 PodGroup 能看到前一个已经绑定的 Pod。
- **确定性**：PodGroup 按 namespace/name/UID 排序，错误信息稳定，测试可断言。

### 7.8.4 第四步：注册与配置

在 `internal/plugins/registry.go` 增加一行：

```go
var registry = map[string]Builder{
    // ...
    "group-topology-affinity": newGroupTopologyAffinity,
}
```

在场景或 scheduler JSON 中启用：

```json
{
  "actions": ["enqueue", "allocate"],
  "tiers": [
    {"name": "tier-1", "plugins": [{"name": "priority"}, {"name": "gang"}]},
    {"name": "tier-2", "plugins": [
      {"name": "predicates"},
      {"name": "group-topology-affinity"},
      {"name": "nodeorder"}
    ]}
  ]
}
```

放哪个 tier 也有讲究：

- 和 `predicates` 同层：硬约束与硬约束一起短路，顺序影响错误信息，不影响可行性。
- 放在 `nodeorder` 前面：NodeOrder 是求和，顺序不影响结果。
- 不要放在 `priority` 前面：Mini 版本不注册 JobOrder，放哪儿都不会改变排序，但会误导读者。

真实 Volcano 还要在 `plugins/factory.go` 注册构造函数，并在 scheduler ConfigMap 的 tiers 中启用；插件名写错会让 scheduler 启动失败。

### 7.8.5 第五步：测试三层

一个插件至少要有三层验证：

```go
// 1) 纯单元：构造 cache、绑定一个对端，断言 Predicate 拒绝/放行
func TestGroupTopologyRequiredAntiAffinity(t *testing.T) {
    c := topologyCache(t)
    a := antiAffinityJob(t, c, "a", 0)
    b := antiAffinityJob(t, c, "b", 0)
    c.Bind(a.UID+"-w-0", "n1", 0)
    ssn := session(t, c, 0, framework.PluginConfig{Name: "group-topology-affinity"})
    if err := ssn.Predicate(c.Pods[b.UID+"-w-0"], c.Nodes["n2"]); err == nil {
        t.Fatal("same rack must be rejected")
    }
}
```

```bash
# 2) 场景：端到端跑通调度，断言最终布局
go run ./cmd/minivolcano -scenario group-topology -events 12

# 3) 全量回归：确认没有破坏其他场景
go run ./cmd/minivolcano -all
go test ./... -count=1
```

required 测试故意只让 `a` 声明规则、`b` 只带匹配标签，用来验证对称评估；如果只测“双方都声明”，很容易漏掉单边规则被绕过的 bug。

场景断言要检查“结果”，而不只是“事件”：

```go
if domains["serve-a"] == domains["serve-b"] {
    return fmt.Errorf("required anti-affinity violated: both in %s", domains["serve-a"])
}
```

事件只能证明“某个 Pod 被调度”，不能证明“两个组在不同域”。很多插件 bug 就是事件看起来正常、最终布局错了。

### 7.8.6 第六步：灰度与上线

1. **只对测试 namespace/队列启用**：用单独的 scheduler ConfigMap 或副本。
2. **先 required 还是 preferred？** 先上 preferred，观察打分是否符合预期；required 容易把作业卡死。
3. **看三个指标**：Pending 作业数、Predicate 拒绝原因、跨域比例。
4. **准备回滚**：从 tiers 移除插件即可；PodGroup 上的规则可以保留，不会被旧调度器读取。
5. **写 runbook**：required 规则导致全组 Pending 时，先看 HyperNode 是否覆盖足够多的域，再看 selector 是否匹配到了自己。

### 7.8.7 五个常见错误

1. **把 required 写成 preferred**：作业能跑，但布局不符合故障域要求。检查 `weight` 是否误填。
2. **selector 匹配 Pod labels 而不是 PodGroup labels**：规则静默失效。先 `kubectl get podgroup --show-labels`。
3. **只在一侧声明规则**：如果实现不做对称评估，后调度的组会绕过。
4. **在 NodeOrder 里做硬约束**：分数再低也只是一个候选，不能阻止违规。
5. **忽略 trial cache**：同一轮里两个组都看到“空域”，一起落到同一个域。

## 7.9 调度器指标

Volcano 暴露 Prometheus 指标，v1.16 常见的：

- `volcano_schedule_attempts_total{result}`：调度尝试；
- `volcano_e2e_scheduling_latency_milliseconds`：Session 端到端耗时；
- `volcano_eviction_transactions_total{action}`：已提交的驱逐；
- `volcano_queue_allocated_*` / `volcano_queue_deserved_*` / `volcano_queue_inqueue_*`：队列账本；
- `volcano_job_retry_counts{job_id}`：作业重试；
- `volcano_plugin_scheduling_latency_milliseconds`：插件耗时。

没有 `volcano_scheduler_session_total` 这类名字；具体名称随版本变化，以 `/metrics` 为准。第 16 章会给出采集与告警建议。

## 7.10 源码地图：从 Session 到 Statement

前面的内容讲“语义”，这一节讲“代码落在哪”。源码路径以 v1.16 的主干为准，patch 版本可能有文件名调整，但结构稳定。

### 7.10.1 目录与关键类型

```text
pkg/scheduler/
├── scheduler.go              # Scheduler.Run：快照 → 打开 Session → 执行 actions
├── cache/
│   ├── cache.go              # SchedulerCache：Nodes/Jobs/Pods/Queues/PDB...
│   ├── event_handlers.go     # informer 事件 → cache 更新
│   └── interface.go          # Cache 接口
├── framework/
│   ├── session.go            # Session、扩展点注册、OpenSession/CloseSession
│   ├── statement.go          # Statement：操作日志、Commit/Rollback
│   └── interface.go          # Plugin/扩展函数类型
├── actions/
│   ├── enqueue/              # 入队
│   ├── allocate/             # 分配（核心）
│   ├── backfill/             # 低优先级填充
│   ├── preempt/              # 队列内抢占
│   ├── reclaim/              # 跨队列回收
│   └── shuffle/              # 受害者驱逐
├── plugins/
│   ├── factory.go            # RegisterPluginBuilder
│   ├── gang/ priority/ drf/ proportion/ capacity/ ...
│   ├── rescheduling/         # VictimTasksFn（lowNodeUtilization）
│   ├── pdb/                  # PDB 保护
│   └── ...
├── api/                      # JobInfo/QueueInfo/NodeInfo/PodInfo
└── conf/                     # 配置解析
```

| 概念 | 类型 | 关键字段/方法 |
|---|---|---|
| 集群缓存 | `SchedulerCache` | `Nodes`、`Jobs`、`Pods`、`Queues`、`Snapshot()`、`Bind()`、`Evict()` |
| 作业视图 | `JobInfo` | `PodGroup`、`Pods`、`MinAvailable`、`Allocated`、`Pipelined`、`TaskStatusIndex` |
| 队列视图 | `QueueInfo` | `Weight`、`Capability`、`Deserved`、`Guarantee`、`Allocated`、`Reclaimable` |
| 节点视图 | `NodeInfo` | `Allocatable`、`Allocated`、`Idle`、`Tasks`、`Node` |
| 调度会话 | `Session` | `Cache`、`Jobs`、`Queues`、`Nodes`、扩展点 map、`Statement` |
| 事务 | `Statement` | `operations`、`Allocate()`、`Evict()`、`Pipeline()`、`Commit()`、`Rollback()` |
| 插件 | `Plugin` | `OnSessionOpen()`、`OnSessionClose()` |

### 7.10.2 一次 allocate 的调用链

```text
Scheduler.Run()
  └─ cache.Snapshot()                    # 一致性视图
  └─ framework.OpenSession(...)          # 每个 plugin.OnSessionOpen 注册 hook
  └─ allocate.Execute(ssn)
       └─ for queue in ssn.Queues:
            sort by ssn.QueueOrderFn
            for job in queue.Jobs:
              sort by ssn.JobOrderFn
              allocateJob(ssn, job)
                ├─ for task in job.Tasks:
                │    sort by ssn.TaskOrderFn
                │    for node in nodes:
                │      ssn.PredicateFn(task, node)
                │      ssn.NodeOrderFn(task, node)
                │    statement.Allocate(task, node)
                └─ if job.Ready >= minAvailable:
                       statement.Commit()
                   else:
                       statement.Rollback()
  └─ ssn.Close()                          # plugin.OnSessionClose
```

几个容易读错的地方：

- `allocate` 不是“给一个 Pod 找节点”，而是“给一个队列/作业找一组节点”；外层循环是 queue → job → task → node。
- `Statement` 在 allocate 里是**每个作业一个**，不是每个 Pod 一个；gang 的回滚粒度是作业。
- `PredicateFn` 返回 error，`NodeOrderFn` 返回分数；两者的短路规则不同。
- `Commit()` 才真正修改 cache；之前所有 `Allocate()` 都只写操作日志。

### 7.10.3 Statement 的两种实现

真实 Volcano 的 Statement 是**操作日志**：

```go
type Statement struct {
    ssn        *Session
    operations []operation
}

type operation interface {
    Execute() error
    Discard()
}
```

- `Allocate(task, node)` 追加一个 allocate operation；
- `Evict(task)` 追加一个 evict operation；
- `Pipeline(task)` 追加一个 pipeline operation；
- `Commit()` 依次 `Execute()`；
- `Rollback()` 逆序 `Discard()`。

MiniVolcano 用 cache 深拷贝实现同样语义（见第 22 章）。两者的区别不是“对不对”，而是**成本模型**：

| | 操作日志（真实 Volcano） | 深拷贝（MiniVolcano） |
|---|---|---|
| 回滚 | 逆序撤销 | 丢弃副本 |
| 内存 | O(操作数) | O(集群大小) |
| 正确性 | 需要每个 operation 都实现 Discard | 天然正确 |
| 大集群 | 可扩展 | 不适合 |

读源码时先看 `Statement` 的 operation 类型，再看每个 action 怎么用它；这是理解“为什么 gang 不会部分绑定”的最短路径。

### 7.10.4 读源码的五个入口

不建议从 `main.go` 开始读。推荐顺序：

1. `pkg/scheduler/api/job_info.go`：先搞清调度器眼里的作业长什么样。
2. `pkg/scheduler/cache/cache.go`：看 Snapshot/Bind/Evict 如何维护账本。
3. `pkg/scheduler/framework/session.go` + `statement.go`：看扩展点与事务。
4. `pkg/scheduler/actions/allocate/allocate.go`：看主循环。
5. `pkg/scheduler/plugins/gang/gang.go` 与 `proportion/proportion.go`：看两个最重要的插件如何注册 hook。

每读一个 action，问三个问题：

- 它从 `Session` 读哪些对象？
- 它调用哪些扩展点？
- 它什么时候 `Commit`，什么时候 `Rollback`？

能回答这三个问题，基本就能独立写插件了。

### 7.10.5 一次 allocate 的逐帧追踪

把“三层缓存”记住，读源码会快很多：

```text
informer cache        API Server 的最终事实（Pod/Node/CRD）
   │ Snapshot / Clone
   ▼
session cache         本轮调度的只读起点
   │ NewStatement
   ▼
trial cache           某个作业的 tentative 绑定，Commit 前对外不可见
```

下面是一次 2 Pod gang 在 1 个 4 卡节点上的逐帧状态。节点已用 2 卡，作业每 Pod 1 卡：

| 帧 | 代码位置 | 动作 | session cache 空闲 | trial cache 空闲 | 可见绑定 |
|---:|---|---|---:|---:|---|
| 0 | `cache.Snapshot()` | 复制集群视图 | 2 GPU | — | 0 |
| 1 | `actions/enqueue` | 作业 Pending → Inqueue | 2 GPU | — | 0 |
| 2 | `actions/allocate` | 选队列、选作业 | 2 GPU | — | 0 |
| 3 | `ssn.NewStatement()` | 克隆 session cache | 2 GPU | 2 GPU | 0 |
| 4 | `st.Predicate(task0, node)` | 切到 trial，过滤+打分 | 2 GPU | 2 GPU | 0 |
| 5 | `st.Allocate(task0, node)` | 写 trial 账本 | 2 GPU | 1 GPU | 1 |
| 6 | `st.Predicate(task1, node)` | 在 trial 上看到 task0 | 2 GPU | 1 GPU | 1 |
| 7 | `st.Allocate(task1, node)` | 写 trial 账本 | 2 GPU | 0 GPU | 2 |
| 8 | `job.Ready >= minAvailable` | 满足 gang | 2 GPU | 0 GPU | 2 |
| 9 | `st.Commit()` | session cache ← trial | 0 GPU | 0 GPU | 2 |
| 10 | `ssn.Close()` | 插件收尾、写回 | 0 GPU | — | 2 |

如果第 6 帧 Predicate 失败（例如第二张卡被别的队列占着）：

```text
task1 找不到节点 → 不 Allocate
ready=1 < minAvailable=2 → st.Rollback()
```

回滚后 trial 被丢弃，session cache 仍是“空闲 2 GPU、可见绑定 0”。这就是 gang“要么一起开始，要么都不开始”的实现方式。

读代码时用这张表对照三个位置：

- `actions/allocate/allocate.go`：帧 2–8 的外层循环；
- `framework/statement.go`：帧 3、5、7、9 的账本操作；
- `framework/session.go`：帧 4、6 的 Predicate/NodeOrder 组合。

一个容易忽略的细节：第 9 帧之前，API Server 看不到任何绑定；Commit 只更新调度器 cache，真正的 binding 由 cache 的 Binder 异步写回 API Server。因此“调度器已经做了决策但 Pod 还没 Bound”是正常状态，不是故障。

## 7.11 MiniVolcano 的框架

MiniVolcano 用更小的代码复刻了同样的结构：

| 真实 Volcano | MiniVolcano |
|---|---|
| Session | `framework.Session` |
| Statement（操作日志） | `framework.Statement`（cache 深拷贝 + commit/discard） |
| Plugin 接口 | 同名接口 |
| 扩展点 | 15 种 hook（含 VictimTasks） |
| Tier | `Config.Tiers`，注册时记录 tier 序号 |
| Actions | enqueue/allocate/preempt/reclaim/backfill/shuffle |
| 配置 | JSON（真实 Volcano 是 YAML） |

Mini 的实现取舍：

- **用深拷贝代替操作日志**：回滚更简单，但大集群开销更高；教学场景规模小，可接受。
- **没有 informer/API Server**：世界是离散事件模拟，控制器直接改 cache。
- **没有 Pipelined 跨 Session**：gang 在一个事务里完成。
- **插件状态持久化**：fairshare 的实例跨 Session 复用，模拟真实插件的长生命周期。

第 22 章会逐行走读。

## 7.12 在真实 Volcano 里写一个插件

第 7.8 节用 MiniVolcano 走了一遍插件设计。这一节换成真实 Volcano 的代码结构，给出一个可以照着改的最小插件。**函数签名以目标版本的 `pkg/scheduler/framework` 为准**，但结构是稳定的。

### 7.12.1 最小插件骨架

```go
package myplugin

import (
    "fmt"

    "volcano.sh/volcano/pkg/scheduler/api"
    "volcano.sh/volcano/pkg/scheduler/framework"
)

const PluginName = "my-plugin"

type myPlugin struct {
    // 跨 Session 的状态放这里；插件实例是长生命周期的。
    weight float64
}

func New(arguments map[string]string) framework.Plugin {
    p := &myPlugin{weight: 1}
    if v, ok := arguments["my-plugin.weight"]; ok {
        fmt.Sscanf(v, "%f", &p.weight)
    }
    return p
}

func (p *myPlugin) Name() string { return PluginName }

func (p *myPlugin) OnSessionOpen(ssn *framework.Session) {
    ssn.AddJobOrderFn(PluginName, func(l, r *api.JobInfo) int {
        // 返回 -1 表示 l 优先；1 表示 r 优先；0 交给下一层。
        return 0
    })
}

func (p *myPlugin) OnSessionClose(ssn *framework.Session) {}
```

注册通常放在插件的 `init()` 或 `pkg/scheduler/plugins/factory.go`：

```go
func init() {
    framework.RegisterPluginBuilder(PluginName, New)
}
```

不同版本的 builder 参数可能包含 `*framework.Session`、`*api.JobInfo` 或 `[]string`；不要照抄签名，先看同目录下 `gang`、`priority` 的写法。

### 7.12.2 选择扩展点

| 你想改变 | 注册 | 返回语义 | 注意事项 |
|---|---|---|---|
| 作业排序 | `AddJobOrderFn` | -1/0/1 | tier 短路；priority 在前会压住后续 |
| 队列排序 | `AddQueueOrderFn` | -1/0/1 | 影响“先服务哪个队列” |
| 任务顺序 | `AddTaskOrderFn` | -1/0/1 | gang 常用“未 Ready 优先” |
| 节点硬过滤 | `AddPredicateFn` | error | 任何 error 都让节点不可用 |
| 节点打分 | `AddNodeOrderFn` | float64, error | 分数相加；方向冲突会抵消 |
| 作业可入队 | `AddJobEnqueueableFn` | bool | 配额/门控在这里 |
| 作业合法性 | `AddJobValidFn` | error | 配置错误尽早失败 |
| gang 就绪 | `AddJobReadyFn` | bool | 决定 Statement 能否 commit |
| 是否可抢占 | `AddPreemptableFn` | bool | 与 PDB/gang 是 AND |
| 是否可回收 | `AddReclaimableFn` | bool | 跨队列回收 |
| 是否超额 | `AddOverusedFn` | bool | OR 组合 |
| 是否可分配 | `AddAllocatableFn` | bool | AND 组合 |
| 是否饥饿 | `AddJobStarvingFn` | bool | 影响抢占触发 |
| 提名受害者 | `AddVictimTasksFns` | []*api.TaskInfo | shuffle 消费，tier 短路 |

一个实用原则：**硬约束放 Predicate，偏好放 NodeOrder，顺序放 Order，门控放 Enqueueable/Allocatable**。把所有逻辑塞进 NodeOrder 是插件失效的最常见原因——分数再低也阻止不了违规放置。

### 7.12.3 一个可用的例子：按注解限制队列

需求：只有带 `example.com/allow-burst: "true"` 的作业，才能使用 `burst` 队列。

```go
func (p *myPlugin) OnSessionOpen(ssn *framework.Session) {
    ssn.AddJobValidFn(PluginName, func(job *api.JobInfo) error {
        if job.PodGroup.Spec.Queue == "burst" {
            if job.PodGroup.Annotations["example.com/allow-burst"] != "true" {
                return fmt.Errorf("queue burst requires annotation example.com/allow-burst=true")
            }
        }
        return nil
    })
}
```

配置：

```yaml
tiers:
- name: tier-1
  plugins:
  - name: priority
  - name: gang
  - name: my-plugin
  - name: conformance
```

验证：

1. `go build ./cmd/scheduler` 或构建镜像；
2. 更新 ConfigMap；
3. `kubectl -n volcano-system rollout restart deploy/volcano-scheduler`；
4. 提交一个不带注解的 burst 作业，确认被 JobValid 拒绝；
5. 带上注解再提交，确认能入队。

### 7.12.4 测试与上线

| 层级 | 做什么 | 工具 |
|---|---|---|
| 单元 | 构造 `JobInfo`/`TaskInfo`，断言 hook 返回值 | `go test` |
| 框架 | 构造 Session，验证 tier 顺序与组合语义 | 复用上游 `framework` 测试工具 |
| 调度 | 在 KWOK/kind 上跑一组作业，检查最终布局 | Volcano Benchmark、kwok |
| 回归 | 对比启用/禁用插件的分配序列 | 固定 seed、固定作业集 |
| 灰度 | 只对测试队列启用 | 独立 ConfigMap 或 scheduler 副本 |

上线检查：

- 插件名与 ConfigMap 完全一致（拼错会让 scheduler 启动失败）；
- 参数带命名空间前缀（如 `my-plugin.weight`），避免与其他插件冲突；
- 不在 `OnSessionOpen` 里做 O(nodes × pods) 的重计算；
- 有状态插件要考虑分片/多副本下的状态一致性；
- 准备好“从 tiers 移除插件”的回滚路径。

### 7.12.5 与 MiniVolcano 的对照

| 真实 Volcano | MiniVolcano |
|---|---|
| `*api.JobInfo` / `*api.TaskInfo` | `*api.Job` / `*api.Pod` |
| `framework.Session` | `framework.Session` |
| `RegisterPluginBuilder` + factory | `plugins/registry.go` 的 map |
| YAML 配置 | JSON 配置 |
| `OnSessionOpen` 注册 hook | 同名方法 |
| 操作日志 Statement | 深拷贝 Statement |

Mini 的价值是让你在没有集群时把 hook 组合跑通；真实插件还要处理 informer、并发、版本兼容和构建发布。两者不要混用。

## 7.13 本章小结

- Session = 快照 + 插件注册 + actions + 提交/回滚。
- Action 是流水线阶段，顺序影响语义。
- 插件按扩展点注册，tier 决定同类型函数的优先级。
- enqueue 决定“能否排队”，allocate 决定“能否放置”。
- MiniVolcano 用更小的代码复刻了同一套框架。

下一章讲公平：proportion、DRF、fairshare、SLA 如何共同决定“谁先跑”。

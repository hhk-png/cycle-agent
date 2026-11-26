# 第 21 章 MiniVolcano 设计

## 21.1 为什么要自己写一个

读源码和写实现是两种理解。自己实现一遍，你会被迫回答：

- 快照和事务到底怎么隔离？
- gang 失败后如何回滚？
- 队列 deserved 在什么时候计算？
- 抢占的受害者怎么选？
- 控制器和调度器的边界在哪？
- 哪些语义是“看起来对但实际错”的？

MiniVolcano 的目标不是复刻 Volcano 的每一行代码，而是用**可运行、可测试、可解释**的方式复刻它的核心语义。

## 21.2 目标与非目标

### 目标

1. 用 Go 实现一个不依赖 Kubernetes 的确定性调度器。
2. 覆盖 Volcano 的核心对象：Job、PodGroup、Queue、HyperNode、CronJob、JobFlow。
3. 覆盖 6 个核心 action：enqueue、allocate、preempt、reclaim、backfill、shuffle。
4. 覆盖 23 个调度器插件（含公平/配额/重调度/治理/分组拓扑插件）。
5. 有控制器状态机、指标、explain、what-if。
6. 有 28 个自检查场景、141 个测试通过、随机不变量验证。
7. 代码可读，能作为二次开发的起点。

### 非目标

- API 兼容真实 Volcano CRD。
- 真实容器/Pod 生命周期。
- 多集群、DRA、MIG/vGPU。
- 安全/RBAC/webhook。
- 生产级性能。
- 完整的 Volcano 插件/action 集合：仍缺 `usage`、`deviceshare`、`extender`、`cdp`、`gangPreempt`/`gangReclaim`；`group-topology-affinity` 只实现了简化版。
- 真实的 PDB 控制器语义（`status.disruptionsAllowed`、Eviction API）与 API Server 级 ResourceQuota 准入；Mini 只实现调度器侧的简化模型。

这些差距在第 24 章逐条列出。

## 21.3 架构

```text
cmd/minivolcano
   │
   ├── internal/scenario   28 个场景 + 断言 + 随机不变量
   ├── internal/sim        World：cache + manager + scheduler + tick
   ├── internal/controller Manager：Pod 生命周期、Job/PG/Queue、Cron、JobFlow
   ├── internal/scheduler  Session 编排、action 执行、指标
   ├── internal/actions    enqueue/allocate/preempt/reclaim/backfill/shuffle
   ├── internal/plugins    23 个插件（含公平/配额/重调度/治理/分组拓扑）
   ├── internal/framework  Session、hook、tier、Statement
   ├── internal/cache      集群快照、绑定/驱逐/释放、HyperNode
   ├── internal/api        Resource、Job、Pod、PodGroup、Queue…
   └── internal/metrics    Prometheus 文本
```

数据流：

```text
World.Tick()
  ├── Manager.Sync(now)          # 推进 Pod 生命周期、策略、Cron/Flow
  ├── Scheduler.Run(now)
  │     ├── Cache.Clone()        # 快照
  │     ├── NewSession()         # 插件 OnSessionOpen
  │     ├── actions...           # Statement 提交/回滚
  │     ├── CloseSession()
  │     └── publish(cache)       # 写回共享 cache
  ├── Manager.Sync(now)          # 根据新绑定更新状态
  └── Metrics gauges
```

## 21.4 对象模型

### 21.4.1 Resource

```go
type Resource map[string]int64
```

单位约定：

- `cpu`：milli-core（1000 = 1 核）；
- `memory`：MiB；
- `nvidia.com/gpu`：整数；
- 其他扩展资源：整数。

提供 `Add/Sub/Clone/LessEqual/WithinLimits/Dominant/MulScalar`。

`WithinLimits` 是一个关键细节：capability 只约束显式出现的维度。例如：

```yaml
capability:
  nvidia.com/gpu: "8"
```

不应该把 CPU 约束为 0。这个语义在开发中被一个场景暴露出来（见第 25 章）。

### 21.4.2 Job / PodGroup / Pod

- `Job.Spec.MinAvailable` + `MinAvailableSet`：区分“未设置（默认 Σ task 有效 minima，没有角色门槛时回退总副本）”与“显式 0（关闭 gang）”。显式 0 是 Mini 的教学扩展；上游 CRD 是 int32，webhook 会把 0 补成 Σ task 有效 minima，无法用 0 关闭 gang。
- `TaskSpec.MinAvailable` + `EffectiveMinAvailable`：task 级角色门槛；`Job.RoleGatesActive()` 复刻“job 门槛低于角色门槛之和时跳过角色检查”的上游行为。
- `Job.Spec.MinSuccess`：达到成功副本数即提前 Completed；未启动副本在 Mini 中被标记为终止。
- `Pod` 保留 `Task/TaskIndex/Requests/NodeSelector/Tolerations/Phase/NUMANode`。
- `PodGroup` 由 `SubmitJob` 自动创建，`MinResources` 先按各 task 的有效门槛预留、再按声明顺序补足 minAvailable；`MinTaskMember` 来自各 task 的有效门槛。

### 21.4.3 Queue

保留 weight/capability/deserved/guarantee/reclaimable/parent/priority/state。

### 21.4.4 扩展对象

- `HyperNode`：tier/parent/children/members。
- `CronJob`：ticks 周期、并发策略、历史限制。
- `JobFlow`：内联 JobSpec 的 DAG。
- `Reservation`：教学抽象（真实 Volcano 无此 CRD）。

## 21.5 设计决策

### 21.5.1 用深拷贝代替操作日志

真实 Volcano 的 Statement 是操作日志，最终 apply 到 cache。MiniVolcano 的 Statement 是一个 cache 深拷贝：

```go
type Statement struct {
    ssn   *Session
    trial *cache.Cache
}
```

优点：

- 回滚 = 丢弃；
- 不会出现“部分操作已应用”的 bug；
- 代码容易理解。

缺点：

- 每次尝试 O(cluster) 拷贝；
- 不适合大集群。

这是教学实现最合理的取舍。

### 21.5.2 单线程、确定性

- 所有 map 遍历都排序；
- 节点打分平手按名字；
- 作业/任务排序有稳定 fallback；
- tick 驱动，不依赖真实时间；
- 随机场景用固定 seed。

这保证同一场景重复运行结果一致，测试可以断言精确状态。

### 21.5.3 控制器与调度器分离

- Scheduler 只做放置；
- Manager 只做生命周期/状态；
- World 负责 tick 顺序。

边界与真实 Volcano 一致，便于讲清“谁负责什么”。

### 21.5.4 插件长生命周期

插件实例在 Scheduler 创建时构建，跨 Session 复用。fairshare 依赖这一点保存历史用量。

### 21.5.5 配置用 JSON

真实 Volcano 用 YAML。MiniVolcano 用 JSON 以避免外部 YAML 依赖：

```json
{
  "actions": ["enqueue", "allocate", "preempt", "reclaim", "backfill", "shuffle"],
  "tiers": [
    {"name": "tier-1", "plugins": [{"name": "priority"}, {"name": "gang"}]}
  ]
}
```

`examples/config/minivolcano-scheduler.json` 是完整例子。

### 21.5.6 受害者提名与执行分离

`shuffle` 的实现刻意把“谁被驱逐”和“怎么驱逐”分开：

- `rescheduling` 插件只注册 `VictimTasksFn`，返回候选 Pod；
- `shuffle` action 只负责用 `Statement.Evict` 执行，不判断节点、优先级或队列；
- `VictimTasksFn` 按 tier 短路，和真实 Volcano 的组合规则一致。

这样做的好处：

- 新增一种重调度策略（如 `usage`、`group-topology-affinity`）只需要注册新的 `VictimTasksFn`，不用改 action；
- action 的事务边界清晰：一次 shuffle 的驱逐要么全提交，要么全回滚；
- 测试可以分别验证“提名是否正确”和“驱逐是否安全”。

代价：

- PDB 是简化模型：有 selector、minAvailable/maxUnavailable 和累计保护，但没有控制器维护的 `disruptionsAllowed`、Eviction API 与 Pod 健康探测；
- `rescheduling` 实现 `lowNodeUtilization` 与教学用 `fragmentation` 两个策略，但没有真实 Volcano 的 CPU/内存/Pod 数分维百分比阈值与 queueSelector/labelSelector；
- 没有真实的五分钟冷却，`interval` 只是 tick 计数。

## 21.6 与真实 Volcano 的映射

| 概念 | 真实 Volcano | MiniVolcano | 差异 |
|---|---|---|---|
| 调度单位 | PodGroup | PodGroup | 同 |
| Job phase | Pending/Running/...（无 Inqueue） | 含 `JobInqueue` | Mini 把 PodGroup 的入队状态映射到 Job，便于场景输出 |
| task 级 gang | `minTaskMember` + `dependsOn` | `MinTaskMember` + 角色就绪判定 | 有角色门槛与生效条件；无 `dependsOn` |
| 提前完成 | `minSuccess` + 全部终止后的失败判定 | `MinSuccess` | 只有提前完成，无第二阶段失败判定 |
| 会话 | Session + 快照 | Session + cache clone | 同 |
| 事务 | Statement 操作日志 | Statement 深拷贝 | 实现不同 |
| 插件 | Go 插件 + factory | 同名接口 + registry | 简化 |
| PDB | policy/v1 + pdb 插件 | `api.PodDisruptionBudget` + `pdb` 插件 | 无控制器状态/Eviction API |
| ResourceQuota | API Server 准入 + resourcequota 插件 | `api.ResourceQuota` + `resourcequota` 插件 | 仅 enqueue 时检查已准入作业 |
| NodeGroup | `spec.affinity` + 注解 + 节点标签 | Queue 类型字段 + 节点标签 | 无 required/preferred/strict |
| tier | 配置 | 配置 | 同 |
| actions | 8 | 6 | 实现了 shuffle；缺 gangPreempt/gangReclaim（reserve 已移除） |
| 作业插件 | svc/ssh/... | 无 | 控制面注入不模拟 |
| cdp | 冷却保护插件 | 无 | 缺 |
| 控制器 | controller-runtime | 手写 Manager | 无 informer |
| 队列层级 | capacity | capacity | 简化 |
| PodGroup 反亲和 | `spec.topologyAffinity`（required/preferred） | `api.PodGroup.TopologyAffinity`（Weight=0 为 required） | 合并两列表、数字 tier、成对评估、默认同 namespace、仅 allocate |
| 预留 | 无 CRD，插件/动作 | Reservation 教学对象 | **不兼容** |
| Cron | cron 表达式 | ticks | 简化 |
| JobFlow | 引用 JobTemplate | 内联 JobSpec | 简化 |
| DRA | v1.15 配额 | 无 | 缺 |
| 多集群 | Volcano Global | 无 | 缺 |

## 21.7 代码规模

| 目录 | 主要内容 |
|---|---|
| `internal/api` | 类型、资源、校验 |
| `internal/cache` | 快照、绑定、保留、拓扑 |
| `internal/framework` | Session/hook/Statement |
| `internal/plugins` | 23 个插件 |
| `internal/actions` | 6 个 action |
| `internal/controller` | 状态机、Cron、Flow |
| `internal/scheduler` | 编排、指标 |
| `internal/sim` | World |
| `internal/scenario` | 28 个场景 + 测试 |
| `cmd/minivolcano` | CLI |

## 21.8 如何扩展

### 加一个插件

1. 在 `internal/plugins/` 新建文件；
2. 实现 `framework.Plugin`；
3. 在 `registry.go` 注册；
4. 写测试；
5. 在场景配置中启用。

### 加一个 action

1. 在 `internal/actions/` 新建文件；
2. 实现 `Action`；
3. 在 `registry.go` 注册；
4. 在配置的 `actions` 中加入；
5. 写场景验证。

### 加一个场景

1. 在 `internal/scenario/` 新建函数；
2. 在 `All()` 中注册；
3. 用 `Build/Steps/Check` 表达；
4. 跑 `go run ./cmd/minivolcano -all`。

## 21.9 补齐路线图：四个高价值缺口

MiniVolcano 当前实现了 23 个插件、6 个 action。下面四个能力是“真实 Volcano 有、Mini 没有”的高价值缺口。它们没有实现，但设计草图足以作为二次开发的起点；补任何一项都要同步场景、测试与文档计数。

### 21.9.1 usage 插件：从 requests 到真实利用率

**缺口**：Mini 的所有账本基于 requests，无法表达“request 很高但实际很闲”。

**数据模型**：

```go
type NodeState struct {
    // ...
    Usage api.Resource // 最近一次采集的实际用量
}
```

**插件**：

```go
func (u *usagePlugin) OnSessionOpen(ssn *framework.Session) {
    ssn.AddPredicateFn("usage", func(pod *api.Pod, node *cache.NodeState) error {
        if ratio(node.Usage, node.Allocatable, "cpu") > u.cpuThreshold { return errOverCPU }
        if ratio(node.Usage, node.Allocatable, "memory") > u.memThreshold { return errOverMem }
        return nil
    })
    ssn.AddNodeOrderFn("usage", func(pod *api.Pod, node *cache.NodeState) (float64, error) {
        return -u.weight * maxRatio(node.Usage, node.Allocatable), nil
    })
}
```

**场景**：两个节点 requests 相同、usage 不同；带 usage 插件时 Pod 落到更闲的节点，不带时按 nodeorder 落到原节点。**测试**：阈值拒绝、权重影响排序、无 usage 数据时的降级。

### 21.9.2 deviceshare：共享 GPU 的账本

**缺口**：Mini 只把 GPU 当整数，无法表达一张卡被多个 Pod 共享。

**数据模型**：

```go
type Device struct {
    ID       string
    Type     string            // gpu / mig / vgpu
    Capacity api.Resource      // 显存、算力
    Allocated api.Resource
    Pods     map[string]bool
}
type NodeState struct {
    // ...
    Devices map[string]*Device
}
```

**插件**：

- Predicate：找到能容纳请求的 device（显存/算力/型号匹配）；
- NodeOrder：按 `SchedulePolicy` 选择聚拢或打散；
- Bind：在 trial cache 中把 Pod 计入 device 账本；
- Evict/Finish：从 device 账本扣除。

**场景**：一张 24Gi 卡被两个 8Gi Pod 共享；第三个 12Gi Pod 被拒；驱逐一个后又能放下。**测试**：账本守恒、设备型号匹配、驱逐释放。

### 21.9.3 gangPreempt：作业粒度抢占

**缺口**：Mini 的 preempt 逐 Pod 驱逐，可能为了一个 Pod 打散另一个 gang 的关键成员。

**算法草图**：

```text
for preemptor in starving jobs:
    pending = preemptor.pending
    for victimJob in same-queue lower-priority jobs:
        surplus = victimJob.running - victimJob.minAvailable   // 只动多余副本
        if surplus <= 0: continue
        st = new Statement
        evict up to surplus pods from victimJob
        bind pending pods on freed nodes
        if preemptor ready >= minAvailable and bound > 0:
            commit; break
        else:
            discard
```

**关键**：先算“受害者能拿出多少富余”，再决定是否值得抢；不要逐 Pod 试错。**场景**：两个 gang 竞争，gangPreempt 只驱逐富余副本，受害者仍保持 minAvailable。**测试**：不击穿受害者 gang、抢占者未就绪时回滚、同队列限制。

### 21.9.4 NamespaceQueue：namespace 内的队列树

**缺口**：Mini 的 Queue 是全局名字，无法表达“同名队列在不同 namespace 是不同对象”。

**数据模型**：

```go
type QueueKey struct {
    Namespace string // "" 表示集群级
    Name      string
}
type NamespaceQueue struct {
    Key       QueueKey
    Parent    QueueKey
    Spec      QueueSpec
    Ready     bool
    Authorized bool
}
```

**要点**：

1. 所有 queue 引用从 `string` 改为 `QueueKey`；
2. 作业的 `queue: namespace/<name>` 解析为 `QueueKey{Namespace: pod.Namespace, Name: name}`；
3. 父链校验：`cluster/<name>` / 同 namespace / 默认 `cluster/default`；
4. 授权：集群 Queue 的 `allowedNamespaces` 决定子树能否挂载；
5. 配额：子树汇总到集群父队列，父 capability 仍是硬上限。

**影响面**：这是四个缺口里改动最大的，会触及 cache、session、所有插件的 queue 参数、场景与测试；建议单独分支，不要和调度算法改动混在一起。

### 21.9.5 补齐顺序建议

```text
1. usage         （数据模型小、场景直观、不影响其他插件）
2. deviceshare   （需要设备账本，但可独立于队列）
3. gangPreempt   （算法独立，复用现有 Statement/Preemptable）
4. NamespaceQueue（全局键改造，最后做）
```

每补一项，至少增加：一个场景、一个正向测试、一个负向测试、一次 `-all` 回归；然后同步 README/附录 P/第 25 章的计数。

## 21.10 本章小结

- MiniVolcano 的目标是语义可解释，不是 API 兼容。
- 用深拷贝事务、确定性排序、插件长生命周期复刻核心行为。
- 对象/动作/插件与真实 Volcano 有清晰映射，也有明确差距；作业级 gang、task 级 `minTaskMember` 与 `minSuccess` 已落地，`dependsOn`、`subGroupPolicy`、usage/deviceshare 等仍是路线图。
- 代码结构支持加插件、加 action、加场景。

下一章逐行走读实现。

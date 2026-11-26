# 第 25 章 验证工程

## 25.1 验证主张

本教程声称 MiniVolcano“可运行、可验证”。具体含义：

- 代码可构建、可 vet、可格式化；
- 9 个包的单测全部通过；
- 28 个场景全部通过自检查；
- 12 个随机 seed 的不变量检查通过；
- 同一场景重复运行结果一致；
- 关键语义有对应的负向测试（回滚、拒绝、超限）。

它**不**声称：

- 与真实 Volcano API 兼容；
- 生产级性能/并发安全；
- 覆盖 DRA/多集群/安全；
- 真实内核/网络/GPU 行为。

## 25.2 验证环境

| 项 | 值 |
|---|---|
| OS | Windows 11 Home China 10.0.26200 |
| Go | go1.27.0 windows/amd64 |
| 模块 | `example.com/minivolcano` |
| 外部依赖 | 无（仅标准库） |
| 数据库/集群 | 不需要 |

## 25.3 命令与结果

```bash
cd mini-volcano

gofmt -l .
# 无输出

go vet ./...
# 通过

go build ./...
# 通过

go test ./... -count=1
# api, cache, framework, plugins, actions, controller, metrics, scheduler, scenario 全部 ok

go run ./cmd/minivolcano -all
# 28 scenarios, 28 passed, 0 failed
```

测试计数：

```bash
go test ./... -count=1 -v | grep -c '^--- PASS'
# 129

go test ./... -count=1 -v | grep -c '^    --- PASS'
# 12（随机不变量子测试）
```

合计 **141 个 PASS 行**（129 顶层 + 12 子测试）。

`-race` 未运行：当前 Windows 环境 `CGO_ENABLED=0`，`go test -race` 需要 cgo。这是一个验证缺口，不是“通过”。

## 25.4 测试分布

| 包 | 顶层测试 | 覆盖内容 |
|---|---:|---|
| `api` | 21 | 资源算术、字符串、解析、校验、默认值、minResources、克隆隔离 |
| `cache` | 20 | 物化、绑定/驱逐/完成、预留、NUMA、HyperNode、优先级类 |
| `framework` | 10 | tier 顺序、fallback、predicate、打分、Statement 提交/回滚/驱逐 |
| `plugins` | 29 | gang、priority、DRF、proportion、capacity、predicates、nodeorder、binpack、task-topology、network-topology、group-topology-affinity、TDM、reservation、SLA、fairshare、NUMA、overcommit、conformance、resource-strategy-fit、rescheduling、pdb、resourcequota、nodegroup |
| `actions` | 20 | enqueue、allocate 提交/回滚/容量、preempt 同队列、reclaim、backfill、shuffle 驱逐/重打包、registry |
| `controller` | 12 | 完成、重启、maxRetry、驱逐、PG/Queue 状态、TaskCompleted、Cron、JobFlow |
| `metrics` | 5 | 计数器/仪表、标签、转义、排序、Reset |
| `scheduler` | 6 | 配置校验、冲突、Run 发布、指标、加载 |
| `scenario` | 6 + 12 | 28 场景、确定性、what-if、explain、随机不变量 |

## 25.5 负向测试

“能跑通”不够，还要证明“该失败时失败”：

- `TestAllocateGangRollback`：6 Pod / 4 卡，全部 Pending，队列分配 0；
- `TestPreemptOnlySameQueue`：跨队列不抢占，受害者队列资源不变；
- `TestAllocateRespectsQueueCapability`：cap 2，3 卡 gang 回滚；
- `TestConfigValidate`：重复 action/plugin 被拒绝；
- `TestMaxRetryFailsJob`：maxRetry=0，失败后 Job Failed；
- `TestValidateQueue`：guarantee > capability 被拒绝；
- `TestNUMAAllocation`：6 CPU 无法放入 4 CPU NUMA；
- `TestConformanceRejectsForeignScheduler`：`default-scheduler` 被拒绝；
- `TestReschedulingVictimsRespectUtilizationAndGang`：低利用率节点提名，且 `minAvailable=1` 的单副本作业不被驱逐；
- `TestShuffleEvictsThenAllocateRepacks`：驱逐数量正确、事件存在、重打包后账本守恒；
- `TestReschedulingLowNodeUtilizationDirection`：LNU 只从高利用率节点驱逐，并按 targetThreshold 控制数量；
- `TestShuffleSkipsWhenNoBetterTarget`：没有更优目标时拒绝驱逐，防止原地折返活锁；
- `TestPDBAllowsOneDisruptionThenBlocks` / `TestPDBMaxUnavailable`：PDB 允许一次中断、阻断下一次；
- `TestResourceQuotaGatesEnqueue`：namespace 配额拒绝并记录原因；
- `TestNodeGroupRestrictsAndCaps`：组外节点与组容量上限都被拒绝；
- `TestDisruptableVictimsAppliesPDBCumulatively`：多个受害者合并后仍不跌破 PDB。
- `TestGroupTopologyRequiredAntiAffinity`：只在一侧声明规则仍被硬拒、无匹配对端为 no-op、backfill 绕过。
- `TestGroupTopologyPreferredPenalty`：preferred 只扣分、双方声明只计一次、插件 weight 生效、跨 namespace 不计分。
- `TestGangTaskMinMemberReadiness`：5 个 worker 但没有 master 时不 Ready；补上 master 后才 Ready。
- `TestAllocateSatisfiesAllRoleGatesBeforeExtraReplicas`：a1+b2 的可行解不会被“先填满 a”活锁。
- `TestAllocateRollsBackWhenRoleUnavailable`：总数能凑够、但某个角色不可调度时，整个 Statement 回滚。
- `TestBackfillDoesNotPartiallyCommitGang`：一个 BestEffort 角色无法满足时，backfill 不提交另一个角色。
- `TestPreemptRequiresRoleGates`：角色门槛未满足时，preempt 不驱逐受害者。
- `TestAllocateRollsBackWhenRoleUnavailable`：总数够但角色不可调度时，整个 Statement 回滚。
- `TestRoleGatesBlockRunning`：总数够但 master 缺失时，控制器不把 Job 标成 Running。
- `TestRoleGatesInactiveWhenJobMinBelowTaskSum`：job 门槛低于角色门槛之和时，角色检查静默失效且作业仍合法。
- `TestValidateMinSuccess`：minSuccess 超过总副本被拒绝。
- 场景 `task-gang` / `min-success`：角色门槛与提前完成的端到端断言。

## 25.6 随机不变量

`TestRandomizedInvariants` 用 12 个 seed，每个：

- 4 个节点（2–5 GPU，8 CPU，32Gi）；
- 2 个队列（权重 1/2）；
- 18 个随机作业（1–5 副本，minAvailable 随机，CPU/GPU 混合，优先级 0/10，时长 1–4）；
- 运行 25 tick；
- 每个 tick 检查资源守恒、gang 就绪、放置合法性。

随机作业没有 task 级门槛，因此 I13（角色门槛）不在随机不变量里；它由 `task-gang` 场景和 `TestGangTaskMinMemberReadiness` 定向覆盖。把 I13 随机化需要同时随机 task 拆分、每个 task 的 replicas 与 minAvailable，否则大量样本会因 `RoleGatesActive()==false` 退化成空检查——这本身也是一个值得记住的测试设计陷阱。

它捕获的是“组合路径”的 bug，例如：

- 驱逐后节点账本未释放；
- 完成 Pod 仍占用节点；
- Running 作业的 ready 数低于 minAvailable；
- 队列 allocated 与 Pod 求和不一致。

## 25.7 开发期实际修复的缺陷

下面这些不是假设，而是本轮实现中真实出现、被场景/测试/人工复核暴露并修复的问题。它们说明“测试全绿”不等于“语义正确”。

### 25.7.1 nil capability 被克隆成空 map

- **现象**：所有未设置 capability 的队列，enqueue 都返回 false，basic 场景完全不调度。
- **原因**：`Resource.Clone()` 对 nil 返回空 map；Queue.Clone 把“未设置”变成了“空能力”，`LessEqual` 把缺失维度当 0。
- **修复**：Queue.Clone 对 capability/deserved/guarantee 保留 nil。
- **测试**：`TestCloneIsolation` 断言 nil 保持 nil。

### 25.7.2 部分 capability 被当成“其他维度为 0”

- **现象**：hierarchy 场景中 org 只写了 GPU capability，子队列的 CPU 请求全部被拒。
- **原因**：用 `LessEqual` 比较 capability，缺失的 CPU 维度被当作 0。
- **修复**：新增 `WithinLimits`，只约束 capability 显式出现的维度。
- **测试**：`TestWithinLimits` + hierarchy 场景。

### 25.7.3 Prometheus gauge 残留旧标签

- **现象**：priority 场景最终有 2 个作业，指标却显示 `phase="Pending" 1`。
- **原因**：gauge 只 Set 当前出现的标签，不清理已消失的标签。
- **修复**：`Registry.Reset(name)`，每次采集前清空 gauge family。
- **测试**：`TestResetRemovesStaleGauge`。

### 25.7.4 完成 Pod 清空 NodeName

- **现象**：topology/heterogeneous 场景完成后无法验证 Pod 落在哪个节点。
- **原因**：`Finish` 把 `NodeName` 清空；Kubernetes 实际保留 `spec.nodeName`。
- **修复**：完成时保留 NodeName/NUMANode，仅释放资源。
- **测试**：`TestBindEvictFinishAccounting`。

### 25.7.5 SLA 全局默认值污染所有作业

- **现象**：SLA 场景中 J2 先于 J3 调度，因为所有作业都被标成 overdue，排序退化为 FIFO。
- **原因**：配置里全局 `sla-waiting-time=1`，没有注释的作业也超时。
- **修复**：场景只给 J3 注解；插件跳过 terminal 作业。
- **测试**：`TestSLAOverdueFirst` + sla 场景。

### 25.7.6 驱逐消耗 maxRetry，导致被抢占作业直接 Failed

- **现象**：reclaim 场景中 A 被驱逐后整个作业失败，而不是降级运行。
- **原因**：默认 `PodEvicted -> RestartTask`，每次驱逐增加 retries，maxRetry 很快耗尽。
- **修复**：默认驱逐不重启（Pod 保持 Pending 等待重调度）；用户要更强语义时显式配置。
- **测试**：`TestEvictionDoesNotRestartByDefault`。

### 25.7.7 capacity 的 root 自递归

- **现象**：如果显式定义名为 root 的队列，`allocatedTree("root")` 会把自己当子队列，无限递归。
- **修复**：遍历时跳过自身与 root。
- **测试**：hierarchy 场景 + 随机不变量。

### 25.7.8 what-if 解析把资源名里的 `/` 当分隔符

- **现象**：`team-a/nvidia.com/gpu=2` 被拆成 3 段，报错。
- **修复**：`SplitN(key, "/", 2)`。
- **测试**：`TestWhatIf`。

### 25.7.9 队列清空后指标序列消失

- **现象**：basic 场景完成后，`minivolcano_queue_allocated` 没有 `queue=default` 的样本，`TestEventLogAndMetrics` 失败。
- **原因**：gauge 只为当前非零资源发样本；队列清空后整个序列消失，监控会误判为“没有采集到”。
- **修复**：对队列 deserved 中出现的维度显式发 0 值样本。
- **测试**：`TestEventLogAndMetrics`（在最终 tick 检查 gauge 存在）。

### 25.7.10 rescheduling 的受害者选择依赖 map 顺序

- **现象（实现期发现）**：同一节点上有多个 Pod 时，先遍历 `node.Pods`（map）再过滤，导致“先看哪个 Pod”随机，可能选中不同的副本。
- **原因**：gang 保护需要按顺序扣减 ready 计数；map 遍历顺序会让结果不稳定。
- **修复**：先把节点上的 Pod 按名字排序，再初始化 ready/minAvailable，再过滤。
- **测试**：`TestReschedulingVictimsRespectUtilizationAndGang` 断言唯一受害者；场景 `shuffle` 再用 `-all` 复跑验证确定性。

### 25.7.11 PDB 累计保护被逐个检查绕过

- **现象（治理插件实现期发现）**：4 个副本、PDB `minAvailable=3`，shuffle 提名 3 个受害者。每个受害者单独检查时都看到“当前可用 4，驱逐后 3，仍满足”，于是 3 个全部被放行，最终可用副本降到 1。
- **原因**：`DisruptableFn` 只读已提交的 cache，没有把同一批驱逐的“前一次结果”计入下一次检查。
- **修复**：在 shuffle action 中用 Statement 重放驱逐，`Statement.Disruptable` 临时切到 trial cache，使第 N 次检查看到前 N-1 次驱逐；`canRepack` 的干跑保持独立。
- **测试**：`TestDisruptableVictimsAppliesPDBCumulatively`（3 个候选只放行 1 个）。
- **教训**：所有“批量决策 + 单次检查”的组合都可能遇到这个问题；preempt/reclaim 因为本来就在 Statement 内逐次检查，反而没有中招。

### 25.7.12 reclaim 用大 Pod 击穿 guarantee

- **现象（治理轮复核发现）**：队列 b 有 8 卡、deserved/guarantee=4，唯一受害者是一个 8 卡 Pod。reclaim 看到“8 > 4，超额”就驱逐，b 直接掉到 0，低于 guarantee 4。
- **原因**：只检查了“驱逐前是否 Overused”，没有检查“驱逐后是否仍 ≥ guarantee”；pods 是整块资源，最后一步会越过 deserved。
- **修复**：每次驱逐前计算 `allocated - victim.Requests`，若不满足 `guarantee ≤ post` 就跳过该受害者；没有更小受害者时本次回收失败。
- **测试**：`TestReclaimNeverBreaksGuarantee`。
- **教训**：公平算法里的“线”是连续量，资源分配是离散量；任何“停在某条线”的承诺都要检查离散跳变的落点。

### 25.7.13 第三轮 group-topology-affinity 的独立复核

新增插件后做了一轮独立的只读复核（在复制的代码树上构造反例，不改仓库），发现并修复了下面几类问题：

| # | 发现 | 严重度 | 修复 |
|---:|---|---|---|
| 1 | required 在“匹配不到任何对端”或“节点不在 HyperNode 树”时仍硬拒，导致本应 no-op 的规则把作业卡死 | 高 | 先检查是否有已落地的匹配对端，再判断域；无对端时 no-op |
| 2 | preferred 在双方都声明时重复计分，`50 + 50 = 100` 而文档只说“扣 50” | 中 | 按对端去重，同一对只取一次权重 |
| 3 | 插件在 backfill/preempt/reclaim/shuffle 的放置探测中也生效，违反“allocate-only”的上游语义 | 中 | `Session.Action` + 插件门控；非 allocate 直接跳过 |
| 4 | 测试与场景声称“只在一侧声明”，实际双方都声明，属于假绿 | 中 | required 测试与 `group-topology` 场景改为只由一侧声明；场景断言精确到 rack-1/rack-2 与 rack-1 仍有空节点 |
| 5 | `arguments.weight` 被文档化但实现忽略 | 低 | 解析并校验 `weight`，NodeOrder 结果乘以该系数；测试用 weight=2 验证 |
| 6 | 省略 tier 被静默当成 tier 1 | 低 | 保留容错，但在第 11/22/35 章明确写出迁移时必须显式写 tier |
| 7 | 第 11 章小结仍写“未实现 PodGroup 反亲和” | 低 | 改为“实现了简化模型” |

复核同时确认了以下路径没有发现问题：单边声明的对称性（两种调度顺序）、默认 namespace 作用域、trial cache 的可见性、preferred 不硬拒、PodGroup/term 的深拷贝、空 selector 的 match-all 语义。仍未覆盖：多调度器/分片下的协调、真实 API Server/CRD 校验、`-race`。

### 25.7.14 第九轮 task 级 gang 的八个边界与一个测试盲区

第九轮实现角色门槛、`minSuccess` 与饥饿判定时，代码评审与独立对抗性复核先后暴露了八个语义问题，均在合并前修复并补测试：

1. **把“job 门槛低于角色门槛之和”当成非法配置**：最初在 `ValidateJob` 里直接拒绝，但这与上游“跳过角色检查、只按总数判断”的行为相反，也会让存量作业在 API 层失败。修复为 `Job.RoleGatesActive()`，作业合法、角色门槛不生效；测试 `TestRoleGatesInactiveWhenJobMinBelowTaskSum`。
2. **`minSuccess` 完成后留下 Pending 副本**：只调用 `Finish` 会拒绝非占用节点的 Pod，导致作业 Completed 后仍有 Pod 卡在 Pending。修复为在 `completeJob` 中把未启动的 `Pending` 标记为 `Succeeded` 并写 `FinishTick`；场景 `min-success` 断言 4 个 Pod 全部成功、节点空闲回到 2 CPU。
3. **`allocate` 用 session cache 判断角色门槛**：`ssn.JobReady` 看不到同一个 Statement 的试绑定，会误判回滚。修复为在 trial cache 上提供 `Statement.ReadyCountByTask`，并让 `allocateJob` 用 `taskGatesSatisfied(st, job)`；错误信息 `gangReason` 指出具体角色缺几个。
4. **`enableJobStarving=false` 仍注册 hook**：文档说 false 时 `JobStarvingFn` 不注册、`Session.JobStarving` 返回 false，但代码仍注册并按总副本判断，`Session` 的无 hook fallback 也返回 true。修复为按 option 决定是否注册，并让无 hook 的 fallback 返回 false；测试 `TestGangDisableJobStarvingDoesNotRegister`。
5. **preempt/reclaim 只检查总数**：两者提交前只看 `ReadyCount >= minAvailable`，没有调用角色门槛，可能驱逐受害者后提交一个角色残缺的 gang；控制器随后拒绝标记 Running，但已提交的 Pod 仍会 Running。修复为统一的 `gangSatisfied(st, job)`，三个 action 共用；测试 `TestPreemptRequiresRoleGates`。
6. **backfill 逐 Pod 提交**：每个 BestEffort Pod 一个 Statement，直接 Commit，绕过 gang 与角色门槛。修复为每个作业一个 Statement，绑定完再统一判定；测试 `TestBackfillDoesNotPartiallyCommitGang`。
7. **TaskOrder 单轮贪心导致活锁**：只按“门槛小优先”排序，会把门槛小的角色所有副本排前面，容量恰好时另一角色永远拿不到门槛；即使存在 `a1+b2` 的可行解也会每轮回滚。修复为 allocate 两轮：第一轮只满足每个角色门槛，第二轮再按 TaskOrder 填额外副本；测试 `TestAllocateSatisfiesAllRoleGatesBeforeExtraReplicas`。
8. **shuffle/rescheduling 受害者只保护总数**：`gangSafeVictims`/`safeVictims` 只比较 `minAvailable`，可能驱逐某个角色的最后一个占用 Pod，使 Running 作业的角色门槛失衡。修复为在受害者选择时同时检查 `OccupyingCountByTask` 与角色门槛；没有专门的合并受害者测试，相关路径由 preempt/allocate 测试间接覆盖，已记入附录 F。

这八条都不是“编译错误”，而是**语义错误**；它们说明角色门槛不是“把 minAvailable 拆成 map”这么简单，必须同时处理生效条件、事务可见性、终态清理、受害者保护、backfill 特例与分配轮次。

**测试盲区**：独立复核还发现，最初 `task-gang` 场景即使把 `taskGatesSatisfied` 或 `taskGatesReady` 改成恒真也能通过——因为 master 名称排序靠前，场景从未观察到“总数够但角色不够”的提交。因此补了五个测试：`TestAllocateSatisfiesAllRoleGatesBeforeExtraReplicas`（两轮分配）、`TestAllocateRollsBackWhenRoleUnavailable`（角色不可调度时回滚）、`TestBackfillDoesNotPartiallyCommitGang`（backfill 事务）、`TestPreemptRequiresRoleGates`（抢占提交）、`TestRoleGatesBlockRunning`（控制器 Running 门槛）。另补 `TestMinResourcesReservesRoleGatesFirst` 与 `TestGangDisableJobStarvingDoesNotRegister`。

## 25.8 独立审计

2026-11-26 对 `mini-volcano/` 做了一轮只读对抗性审计（不复用开发者的测试结论，审计者自行构造反例）。审计时 22 个场景通过（当时版本），单测尚有一个已知的 metrics 失败（见 25.7.9）；除此之外仍发现 10 项单测未覆盖的问题。下表汇总 11 项，均已修复并补回归测试。

| # | 发现 | 严重度 | 修复 |
|---:|---|---|---|
| 1 | preempt/reclaim 在抢占者没有真正绑定 Pending Pod 时也提交驱逐（ReadyCount 被已有 Allocated Pod 满足） | 高 | 增加 `bound > 0` 条件；`TestPreemptDoesNotEvictWhenPreemptorCannotBind` |
| 2 | `CompleteJob` 只改 Job 状态，不释放仍在运行的副本；Cron GC 删除 Pod 也不释放节点 | 高 | `completeJob`/`deleteJob` 释放占用；`TestCompleteJobReleasesOtherReplicas` |
| 3 | 节点有 GPU 但 NUMA 只声明 CPU/内存时，GPU Pod 被误拒 | 中 | NUMA 适配只约束 NUMA 声明的维度；`TestNUMAIgnoresNodeLevelDeviceDimensions` |
| 4 | HyperNode 层级 fixture 反转；没有 tier 单调性/环校验 | 中 | 叶子 tier1、机架 tier2、根 tier3；`ValidateTopology` + 两个回归测试 |
| 5 | nodeorder 用 map 遍历累加浮点，分数不稳定，绕过节点名平手 | 中 | 按排序后的维度累加；`TestNodeOrderDeterministic` |
| 6 | TaskCompleted 策略每次 reconcile 重复触发 | 低 | `PolicyHandled` 提前返回；`TestTaskCompletedFiresOnce` |
| 7 | 队列清空后 gauge 序列消失 | 中 | 显式 0 值样本；`TestEventLogAndMetrics` |
| 8 | 预留把“已使用部分”重复扣除，guest 可用资源被低估 | 中 | 只扣预留的未使用部分；`TestReservationOnlyHoldsUnusedCapacity` |
| 9 | 队列/HyperNode 环或缺失父节点可导致递归崩溃 | 中 | `ValidateTopology` + 递归 visited 集合；`TestValidateQueueParentCycle` 等 |
| 10 | `minAvailable=0` 的作业仍按全部副本做入队检查，无法部分准入 | 中 | `MinResources` 取单个副本；`TestGangDisabledJobPartiallyAdmits` |
| 11 | `Clone` 让 `node.Pods` 与 `Pods` 指向不同对象，阶段更新可能不一致 | 潜在 | Clone 复用同一 Pod 指针；`TestCloneKeepsSinglePodIdentity` |

这轮审计的价值不在于“又修了几个 bug”，而在于证明了第 25.6 节的判断：**测试绿只能证明已覆盖的路径；语义正确性需要独立构造反例**。

第三轮新增的 `group-topology-affinity` 做了一轮独立的只读复核，发现的问题与修复见第 25.7.13 节；仍**没有**覆盖多调度器/分片、真实 API Server/CRD 与 `-race`。

## 25.9 测试不能证明什么

- **API 兼容**：Mini 的结构不是 CRD。
- **并发安全**：没有并发访问；真实调度器有 informer/多 worker。
- **性能**：深拷贝/全量遍历不是生产实现。
- **设备**：没有 DRA/MIG/vGPU。
- **多集群**：没有成员集群。
- **安全**：没有 RBAC/webhook/网络。
- **真实故障**：没有节点 NotReady、网络分区、证书过期。
- **Kubernetes 语义**：没有 API Server 的乐观并发/默认值/准入链。
- **PDB**：Mini 有简化 PDB（selector/minAvailable/maxUnavailable），但没有 `status.disruptionsAllowed`、Eviction API 与控制器状态。
- **真实利用率**：没有 metrics-server/usage 插件，所有判断基于 requests。
- **重调度成本**：没有镜像/模型加载/checkpoint 时间，无法验证生产抖动。

## 25.10 对抗性复核方法

如果你要继续审计 MiniVolcano，建议：

1. **读语义而不是读测试**：先写下“应该怎样”，再看代码。
2. **构造反例**：每个 action 问“如果资源刚好差 1 会怎样”。
3. **检查账本**：绑定/驱逐/完成/重启后，节点和队列是否守恒。
4. **检查事务**：失败路径是否真的回滚，事件是否泄漏。
5. **检查确定性**：跑两次，diff 输出。
6. **检查边界**：0 副本、minAvailable=0、capability 只写一个维度、root 队列、空队列。
7. **检查控制器**：策略、重试、终态、Cron/Flow 清理。

一个有效的技巧是“把断言反过来”：如果测试断言“2 个 Pod 被分配”，试着构造“只有 1 个能分配”的场景，看是否真的回滚。

## 25.11 本章小结

- 验证包括构建、vet、单测、场景、随机不变量、确定性。
- 141 个 PASS 行覆盖 9 个包；28 个场景覆盖端到端语义。
- 开发期修复 11 类；后续独立审计复核 11 项（含 1 项已知 metrics 问题），其中新发现 10 项“测试全绿但语义错”的问题；第二轮治理插件又修复了 PDB 累计保护和 reclaim 击穿 guarantee 两个问题；第三轮新增 group-topology-affinity 与对应场景/测试，并做独立只读复核（见 25.7.13）；第九轮新增 task 级 gang、minSuccess、角色优先 minResources 与 enableJobStarving 缺省行为的 11 个测试和 2 个场景。当前插件 23、场景 28、PASS 141。
- 验证不能证明 API 兼容、并发、性能、设备、安全、多集群。
- 对抗性复核要读语义、构造反例、检查账本与事务。

下一章看未来：Volcano v1.17+ 与 AI 调度的发展方向。

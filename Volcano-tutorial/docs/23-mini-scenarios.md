# 第 23 章 MiniVolcano 场景与实验

## 23.1 场景是测试，也是文档

MiniVolcano 的 28 个场景不是“demo 脚本”，而是**自检查的实验**：

- 每个场景构建一个确定性的集群；
- 用 tick 推进；
- 断言最终状态；
- `-all` 运行全部并返回退出码。

```bash
cd mini-volcano
go run ./cmd/minivolcano -list
go run ./cmd/minivolcano -all
```

预期输出：

```text
PASS basic
PASS gang
PASS task-gang
PASS min-success
...
PASS throughput

28 scenarios, 28 passed, 0 failed
```

## 23.2 场景总表

| # | 场景 | 验证点 | 关键断言 | 配置 |
|---|---|---|---|---|
| 1 | `basic` | enqueue→allocate→run→complete 主链路 | Job Completed，资源释放 | 默认 |
| 2 | `gang` | 4 卡 gang 等待 filler 释放 | 无部分启动，最终完成 | 默认 |
| 3 | `priority` | 高优先级后到，抢占同队列低优先级 | high 4 Running，low 4 Running | 默认 |
| 4 | `reclaim` | A 超额，B 回收 | A 回到 deserved 4，B 4 | 默认 |
| 5 | `proportion` | 3:1 权重分 8 卡 | A 6，B 2 | 默认 |
| 6 | `drf` | 剩余 1 CPU 给主导份额更小的作业 | B Running，A2 Pending | 默认 |
| 7 | `binpack` | 装箱到一个节点 | node-a 8 CPU，node-b 0 | 自定义（去 nodeorder） |
| 8 | `topology` | hard HyperNode 选域 | 所有 Pod 在 leaf-a* | +network-topology-aware |
| 9 | `task-topology` | ps/worker 亲和 | 所有 Pod 同节点 | +task-topology |
| 10 | `reservation` | 4 卡预留阻塞 8 卡作业 | X 在 tick 6 前不 Running | +reservation |
| 11 | `sla` | 超预算作业优先 | J3 先于 J2 | +sla |
| 12 | `fairshare` | 历史用量影响下一轮 | B 先于 A2 | +fairshare |
| 13 | `hierarchy` | 父队列 cap 6，两子各 3 | org-a/org-b 各 3 | capacity hierarchy |
| 14 | `numa` | 12 CPU 无法放入单 NUMA | fit Running，big Pending | +numa-aware |
| 15 | `tdm` | 窗口内 preemptable 用 revocable 节点 | preemptable 在 rz 节点 | +tdm |
| 16 | `retry` | PodFailed→RestartJob | retries≥1，最终 Completed | 默认 + 故障注入 |
| 17 | `cron` | 周期创建 + 历史限制 | ≥3 次创建，保留≤2 | 默认 |
| 18 | `jobflow` | prep→train→eval DAG | 3 步全部 Completed | 默认 |
| 19 | `backfill` | 禁用 allocate，BestEffort 由 backfill 放置 | 2 Pod Running | 自定义 actions |
| 20 | `heterogeneous` | nodeSelector 选 A100 | Pod 只在 a100-* | 默认 |
| 21 | `throughput` | 10 个 2 卡作业分批完成 | 全部 Completed | 默认 |
| 22 | `shuffle` | 碎片节点疏散 + binpack 重打包（fragmentation 策略） | 全部 Pod 在一个节点，另一节点 0 | +rescheduling，actions 含 shuffle |
| 23 | `pdb` | PDB 允许一次中断、阻断第二次 | crit-a 3 Running，crit-b 4，high-2 Pending | +pdb |
| 24 | `resourcequota` | namespace 配额准入 | a 2、c 1 Running，b Pending | +resourcequota |
| 25 | `nodegroup` | 队列限定节点组 | a-job 只在组 a，b-job 只在组 b | +nodegroup |
| 26 | `group-topology` | PodGroup 级拓扑反亲和 | 两个 serving 组落在不同 rack | +group-topology-affinity |
| 27 | `task-gang` | task 级 `minTaskMember` | 1 master + 4/8 worker；8 worker 不等于满足 | 默认 |
| 28 | `min-success` | `minSuccess` 提前完成 | 2/4 成功即 Completed；未启动副本终止 | 默认 |

## 23.3 典型场景

### 23.3.1 gang：等待而不是部分启动

```bash
go run ./cmd/minivolcano -scenario gang -events 12
```

关键事件：

```text
t=3 Completed queue=default job=filler all tasks succeeded
t=3 Scheduled ... job=train pod=train-worker-0 ... node=gpu-node
t=3 Scheduled ... job=train pod=train-worker-1 ... node=gpu-node
t=3 Scheduled ... job=train pod=train-worker-2 ... node=gpu-node
t=3 Scheduled ... job=train pod=train-worker-3 ... node=gpu-node
t=3 Allocated queue=default job=train pods=4 ready=4/4
```

在 tick 3 之前，train 没有任何 Pod 被绑定。filler 一完成，4 个 Pod 在同一个 Session 里一起提交。这就是 gang。

### 23.3.2 priority：同队列抢占

```bash
go run ./cmd/minivolcano -scenario priority -events 10
```

关键事件：

```text
t=2 Evicted ... job=train-low pod=train-low-worker-1 preempted by higher-priority job train-high
t=2 Scheduled ... job=train-high pod=train-high-worker-0 ...
...
t=2 Preempted queue=default job=train-high victims=4 ready=4/4
```

- 低优先级作业先跑满 8 卡；
- 高优先级作业在 tick 2 提交；
- preempt 驱逐 4 个低优先级 Pod，绑定 4 个高优先级 Pod；
- 低优先级作业仍有 4 个 Running，处于 Inqueue（starving）。

### 23.3.3 topology：先选域，再选节点

```bash
go run ./cmd/minivolcano -scenario topology -events 10
```

```text
t=0 Scheduled ... pod=train-worker-0 node=leaf-a1
t=0 Scheduled ... pod=train-worker-1 node=leaf-a1
t=0 Scheduled ... pod=train-worker-2 node=leaf-a2
t=0 Scheduled ... pod=train-worker-3 node=leaf-a2
```

4 个 Pod 全部落在 rack-a（leaf-a1/leaf-a2），没有任何一个去 rack-b。hard 模式不是“给同域加分”，而是“只允许域内节点”。

### 23.3.4 reservation：时间窗的力量

```bash
go run ./cmd/minivolcano -scenario reservation -events 10
```

- 节点 8 卡；
- 为 reserved 队列预留 4 卡，窗口 [0,6)；
- r-job（4 卡）在 tick 0 运行，tick 3 完成；
- x-job（8 卡）在 tick 0–5 一直无法凑齐（可用只有 4 卡）；
- tick 6 预留失效，x-job 一次拿到 8 卡，tick 9 完成。

```text
t=6 Allocated queue=other job=x-job pods=8 ready=8/8
t=9 Completed queue=other job=x-job all tasks succeeded
```

注意：这是 MiniVolcano 的教学抽象。真实 Volcano 没有 Reservation CRD，等价语义要用 Queue guarantee + reservation 插件/SLA 表达。

### 23.3.5 shuffle：从碎片到装满

```bash
go run ./cmd/minivolcano -scenario shuffle -events 12
```

场景从一个人为碎片状态开始：两个 8 CPU 节点，两个作业各 2 副本，每节点各放一个副本（各 50%）。调度器配置启用了 `rescheduling` 的 `fragmentation` 策略（阈值 0.6）和 `shuffle`，并用 `binpack` 负责重打包。

关键事件：

```text
t=0 Evicted ... pod=left-worker-0 node=node-a shuffle: low-utilization or over-utilized node
t=0 Evicted ... pod=right-worker-0 node=node-a shuffle: low-utilization or over-utilized node
t=0 Shuffled evicted 2 pod(s) for repacking
t=1 Scheduled ... pod=left-worker-0 node=node-b
t=1 Scheduled ... pod=right-worker-0 node=node-b
```

最终 node-b 装 8 CPU、node-a 为空，两个作业都保持 Running（minAvailable=1，驱逐一个副本不会打破 gang）。

这个场景验证了三件事：

1. `rescheduling` 只提名一个源节点，不会一次打散整个集群；
2. `shuffle` 只驱逐、不绑定，重打包由下一轮 `allocate` 完成；
3. gang 保护生效：如果驱逐会让某作业跌破 minAvailable，该 Pod 不会被提名。

与真实 Volcano 的差异：这个场景没有启用 PDB、没有五分钟冷却、没有多策略；阈值是单维度的“最大资源占比”。中断保护由场景 `pdb` 单独验证。

### 23.3.6 pdb：允许一次、阻断第二次

```bash
go run ./cmd/minivolcano -scenario pdb -events 20
```

场景里有三个作业：

- `crit-a`：4 副本，minAvailable=3，PDB `minAvailable=3`，最多损失 1 个；
- `crit-b`：4 副本，minAvailable=4，PDB `minAvailable=4`，一个都不能少；
- `high-1`/`high-2`：后到的高优先级作业，分别需要 1/2 个 Pod。

tick 2 时 high-1 抢占 crit-a 的一个副本（4→3，PDB 仍满足）；tick 3 时 high-2 需要 2 个受害者，但 crit-a 再少一个就跌破 3，crit-b 完全不可动，于是 preempt 找不到可驱逐对象，整个 Statement 回滚：

```text
t=2 Preempted ... job=high-1 victims=1 ready=1/1
t=3 ... job=high-2 ... no preemptable victims (priority, gang or PDB protection)
```

最终 crit-a=3、crit-b=4、high-1=1、high-2=0，GPU 账本仍是 8。

### 23.3.7 resourcequota：按 namespace 准入

```bash
go run ./cmd/minivolcano -scenario resourcequota -events 20
```

namespace `team-q` 的配额是 4 CPU：

- a（2 CPU）先入队，已用 2；
- b（3 CPU）需要 2+3=5 > 4，被拒绝，Pod 保持 Pending；
- c（1 CPU）需要 2+1=3 ≤ 4，仍然可以入队。

这说明配额检查是**逐作业**的，不是“队列一旦拒绝就全拒”。同时，已准入但尚未运行的副本也计入用量，避免“先入队、后超卖”。

### 23.3.8 group-topology：反亲和是成对的

```bash
go run ./cmd/minivolcano -scenario group-topology -events 12
```

两个 serving 作业都带 `serving-group=online` 标签；**只有 serve-a** 在 PodGroup 上声明 required 反亲和（tier 2 的 rack 域），serve-b 不声明。rack-1 有两个节点，rack-2 只有一个：

```text
t=0 Scheduled ... job=serve-a pod=serve-a-worker-0 node=node-a1
t=0 Scheduled ... job=serve-b pod=serve-b-worker-0 node=node-b1
```

serve-b 没有去 rack-1 的空节点 node-a2，因为那会与 serve-a 同域。这个场景验证三件事：

1. required term 是硬约束，不是加分；
2. 同一轮 allocate 中，后一个 PodGroup 能看到前一个的绑定；
3. 规则按对评估——只在 serve-a 上写规则，serve-b 仍然会被挡住。

它对应真实 Volcano 的 `group-topology-affinity` 插件，但 Mini 用数字 tier、没有 namespace selector。

### 23.3.9 nodegroup：队列被限制在节点组

```bash
go run ./cmd/minivolcano -scenario nodegroup -events 20
```

节点 `a1/a2` 带 `volcano.sh/nodegroup-name=a`，`b1` 带 `b`。team-a 队列只允许组 a，即使 b1 一直空闲，a-job 的 6 个 Pod 也只落在 a1/a2；team-b 的 4 个 Pod 只落在 b1。组容量上限还可以进一步限制“一个队列在一个组里最多用多少”，由 Predicate 在 Statement 的 trial cache 上逐 Pod 检查。

### 23.3.10 task-gang：数量够不等于角色齐

```bash
go run ./cmd/minivolcano -scenario task-gang -events 8
go run ./cmd/minivolcano -scenario task-gang -explain default/train
```

场景只有 5 个 CPU 槽，作业是 1 个 master + 8 个 worker：

```yaml
minAvailable: 5
tasks:
  - name: master
    replicas: 1
    minAvailable: 1
  - name: worker
    replicas: 8
    minAvailable: 4
```

tick 0 的 allocate 先进入第一轮：按角色门槛绑定 master 1 个、worker 4 个；第二轮才尝试填额外副本（节点已满）。`Statement` 的 trial cache 上 `ReadyCount=5`、`ReadyCountByTask={master:1, worker:4}`，两个条件都满足才 Commit。结果：

```text
READY/MIN 5/5
RUNNING   5
PENDING   4   # 剩下的 worker，节点没有槽
```

如果只看总量，5 个 worker 就绪会被误判为“gang 满足”；角色门槛把它挡在 Commit 之前。把 `minAvailable` 改成 4，`RoleGatesActive()` 返回 false，角色检查被跳过——这正是 6.2.2 的上游边界。

### 23.3.11 min-success：提前完成与资源回收

```bash
go run ./cmd/minivolcano -scenario min-success -events 8
```

4 个分片、每片 1 CPU、节点 2 CPU、`minAvailable=2`、`minSuccess=2`：

1. tick 0 只绑定 2 个分片（gang 门槛 2）；
2. 两个分片运行 2 tick 后成功；
3. 控制器看到 `succeeded >= minSuccess`，立即 `Completed`；
4. 另外 2 个尚未启动的分片被标记为 `Succeeded` 并释放配额；节点 idle 回到 2 CPU。

这个场景验证两件事：`minSuccess` 不是 `minAvailable` 的替代（gang 仍按 2 提交），以及提前完成不会泄漏 Pending 副本。真实 Volcano 会删除/终止这些副本，Mini 用状态标记模拟同一账本结果。

## 23.4 explain：把 Pending 变成证据链

```bash
go run ./cmd/minivolcano -scenario drf -explain default/a2
```

输出（节选）：

```text
JOB default/a2
  phase=Inqueue minAvailable=2 replicas=2 maxRetry=3 priority=0
  ready=0 running=0 ... pending=2
  queue=default state=Open ... allocated=cpu=1
  reasons:
    - node cpu-node has insufficient idle resources (need cpu=1, idle cpu=0)
    - gang not ready after allocation: ready=0 minAvailable=2; rolled back 2 tentative bindings
```

它回答三个问题：

1. 作业在哪个阶段（Inqueue）；
2. 为什么没分配（没有空闲 CPU）；
3. gang 是否回滚（rolled back 2）。

真实 Volcano 没有完全相同的 `-explain`，但 PodGroup conditions、scheduler 日志、vcctl 提供类似信息。

## 23.5 what-if：反事实调度

```bash
go run ./cmd/minivolcano -scenario proportion \
  -what-if team-a/nvidia.com/gpu=2 -no-check -events 0
```

结果：

- team-a 的 capability 被改成 2；
- a-job 需要 6 卡 gang，永远无法满足，保持 Pending；
- team-b 正常拿到 2 卡。

这个实验说明：

- capability 是硬上限；
- gang 作业在 cap 小于 minAvailable 时不是“部分运行”，而是完全不运行；
- what-if 会从零重放整个场景，而不是修改当前状态。

## 23.6 指标

```bash
go run ./cmd/minivolcano -scenario basic -metrics
```

会输出：

```text
minivolcano_scheduler_sessions_total
minivolcano_scheduler_events_total{type="Scheduled"}
minivolcano_queue_allocated{queue="default",resource="cpu"}
minivolcano_jobs{phase="Completed"}
minivolcano_node_idle{node="node-a",resource="cpu"}
```

这些指标与真实 Volcano 的指标不是同名，但设计思路一致：

- counter 累计调度/事件；
- gauge 反映当前队列/节点/作业状态；
- 每次采集先 Reset gauge，避免旧标签残留。

## 23.7 随机不变量

`internal/scenario/scenario_test.go` 里有一个 `TestRandomizedInvariants`：

- 12 个 seed；
- 每个 seed 随机生成 18 个作业、4 个节点、2 个队列；
- 运行 25 tick；
- 每个 tick 检查：
  - 节点 allocated = 该节点上占用 Pod 的 requests 之和；
  - 队列 allocated = 队列内占用 Pod 的 requests 之和；
  - 节点不超卖、不为负；
  - 占用 Pod 必须有 NodeName 且节点存在；
  - Running 的 gang 作业必须满足 minAvailable。

这是“测试全绿但语义错”的防线：单元测试覆盖已知路径，随机不变量覆盖组合。

随机不变量目前不生成 PodGroup 反亲和规则，也不生成 task 级门槛；这两部分语义分别由 `group-topology` 与 `task-gang` 场景、以及对应的插件测试覆盖，属于“定向验证”而不是“随机验证”。I13（角色门槛）的随机化需要同时随机 task 拆分与 `minAvailable`，否则大多数字例会因 `RoleGatesActive()==false` 而退化成空检查。

## 23.8 如何加一个场景

```go
func myScenario() *Scenario {
    return &Scenario{
        Name: "my-scenario",
        Description: "...",
        Ticks: 5,
        Build: func(w *sim.World) error {
            // 建节点/队列/作业
            return nil
        },
        Check: func(w *sim.World) error {
            // 断言
            return nil
        },
    }
}
```

然后在 `All()` 中追加。运行：

```bash
go run ./cmd/minivolcano -scenario my-scenario
go run ./cmd/minivolcano -all
go test ./internal/scenario -run TestAllScenarios -count=1
```

## 23.9 本章小结

- 28 个场景覆盖 gang、角色门槛、提前完成、公平、抢占、回收、拓扑、预留、SLA、TDM、NUMA、异构、Cron、JobFlow、吞吐、重调度、PDB、namespace 配额、节点组与 PodGroup 级拓扑反亲和。
- 每个场景都有断言，`-all` 是集成测试。
- explain 把 Pending 变成证据链；what-if 做反事实分析。
- 随机不变量覆盖组合爆炸，弥补单元测试的盲区。
- 场景是扩展 MiniVolcano 的最佳入口。

下一章讨论从 MiniVolcano 到生产 Volcano 的差距与迁移路径。

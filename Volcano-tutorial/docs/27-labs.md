# 第 27 章 端到端实验手册

## 27.1 怎么用这一章

这一章把前面 26 章的知识变成 14 个可以动手的实验。每个实验都遵循同一个结构：

1. **目标**：你要验证的语义是什么；
2. **命令**：可以直接复制运行；
3. **预期**：你应该看到什么；
4. **为什么**：结果背后的调度逻辑；
5. **故障注入/练习**：把参数改坏，观察系统如何失败。

没有 Kubernetes 集群也能完成 13 个实验：MiniVolcano 是一个确定性模拟器。第 14 个实验需要真实集群，命令给出但不强求。如果你想把多个机制放进同一条时间线，看附录 T 的 14 tick 综合轨迹；如果你要演练生产故障（证书、DRA、leader 抖动），看附录 V 的剧本库。

约定：

```bash
cd Volcano-tutorial/mini-volcano
# 所有 MiniVolcano 命令都在这个目录执行
export MV="go run ./cmd/minivolcano"
```

每次实验前建议先跑一遍基线：

```bash
$MV -all
# 28 scenarios, 28 passed, 0 failed
```

## 27.2 实验一：第一条证据链

**目标**：理解 enqueue → allocate → run → complete，以及 `-explain` 的证据结构。

```bash
$MV -scenario basic -events 20
$MV -scenario basic -explain default/web
```

预期：

```text
t=0 Scheduled ... job=web pod=web-worker-0 node=node-a
...
t=0 Allocated queue=default job=web pods=4 ready=4/4
t=2 Completed queue=default job=web all tasks succeeded
```

`-explain` 会输出 phase、ready、queue、message 和失败原因（如果 Pending）。

**为什么**：Job 控制器先物化 4 个 Pod，调度器在同一个 Session 里为它们找节点；4/4 就绪才提交。完成时释放节点，但保留 `NodeName` 作为放置历史。

**练习**：

```bash
$MV -scenario basic -ticks 1 -no-check
```

只跑 1 个 tick，观察作业还没完成时的中间状态。再把 `-ticks` 改成 2，比较事件数量。

## 27.3 实验二：gang 是事务，不是“一起创建”

**目标**：验证 gang 不会部分启动，以及回滚会撤销 tentative binding。

```bash
$MV -scenario gang -events 20
$MV -scenario gang -explain default/train
```

预期：filler 占用 2 卡时，train 的 4 个 Pod 一个都不绑定；filler 完成后，4 个 Pod 在同一个 tick 一起出现。

```text
t=3 Completed ... job=filler
t=3 Scheduled ... job=train pod=train-worker-0 ... node=gpu-node
...
t=3 Allocated queue=default job=train pods=4 ready=4/4
```

**为什么**：allocate 为 train 创建 Statement，试绑 4 个 Pod；在 filler 完成前只有 2 卡空闲，ready=2 < minAvailable=4，Statement 被丢弃。下一轮重新尝试。

**练习**：

```bash
$MV -scenario gang -events 20 -ticks 2 -no-check
```

在 filler 完成前停下，确认 train 的 4 个 Pod 都是 Pending，且节点上没有它们的绑定记录。

## 27.4 实验三：队列配额、借用与回收

**目标**：理解 weight、capability、deserved、reclaimable 四个字段的联合效果。

```bash
$MV -scenario proportion -events 12
$MV -scenario reclaim -events 12
```

预期：

- proportion：8 卡集群，team-a:team-b 权重 3:1，最终 6:2；
- reclaim：team-a 先占满 8 卡，team-b 到达后回收 4 卡，team-a 保留 deserved 4 卡。

**为什么**：proportion 计算 deserved 并按 share 排序；reclaim 只驱逐“超额且可回收”的队列，且每次驱逐后重新检查 Overused。

**练习（反事实）**：

```bash
$MV -scenario proportion -what-if team-a/nvidia.com/gpu=2 -no-check -events 0
```

把 team-a 的 capability 改成 2。它的 6 卡 gang 永远无法满足，因此完全不运行；team-b 仍拿到 2 卡。这验证了“capability 是硬上限”和“gang 在 cap < minAvailable 时整体回滚”。

再试：

```bash
$MV -scenario proportion -what-if team-a/nvidia.com/gpu=8,team-b/nvidia.com/gpu=1 -no-check
```

观察 team-b 的 2 卡作业在 cap=1 下如何表现。

## 27.5 实验四：优先级与同队列抢占

**目标**：区分 preempt（同队列、按优先级）与 reclaim（跨队列、按份额）。

```bash
$MV -scenario priority -events 20
```

预期：

```text
t=2 Evicted ... job=train-low pod=train-low-worker-1 preempted by higher-priority job train-high
t=2 Scheduled ... job=train-high pod=train-high-worker-0 ...
t=2 Preempted queue=default job=train-high victims=4 ready=4/4
```

train-low 仍有 4 个 Running，处于 Inqueue（starving），但不会反向抢占高优先级。

**为什么**：preempt 只在同队列内找优先级更低的受害者；跨队列的抢占由 reclaim 负责。两者分开是为了避免“高优先级队列永远压死低优先级队列”。

**练习**：

```bash
$MV -scenario priority -explain default/train-low
```

观察 `preemption found no preemptable victims` 或 `gang not ready` 之类的证据，理解“为什么低优先级不反抢”。

## 27.6 实验五：DRF、fairshare 与层级队列

**目标**：比较三种公平机制。

```bash
$MV -scenario drf -events 12
$MV -scenario fairshare -events 12
$MV -scenario hierarchy -events 12
```

预期：

- DRF：剩余 1 CPU 给主导份额更小的作业，a2 的 2 卡 gang 继续 Pending；
- fairshare：上一轮用量高的队列排后面，B 先于 A2；
- hierarchy：父队列 cap=6，两个子队列各 3 卡，不能通过子队列绕过父上限。

**为什么**：DRF 比较作业的 `max(allocated/total)`；fairshare 用半衰期衰减历史用量；capacity 递归计算父子 deserved，并同时检查祖先 capability。

**练习**：

```bash
$MV -scenario hierarchy -explain org-a/a-job
```

如果想改父队列 cap，需要复制场景代码或改配置；CLI 的 `-what-if` 只能改队列 capability，不能改父队列结构。先用 `-events 0` 看 summary 里的实际队列/作业名，再决定读哪个对象。

## 27.7 实验六：节点打分与装箱

**目标**：理解 nodeorder 与 binpack 的方向冲突。

```bash
$MV -scenario binpack -events 12
```

预期：4 个 2 CPU 作业全部落在 node-a，node-b 空闲。

**为什么**：场景配置去掉了 nodeorder，只保留 binpack，并把权重调到 10。binpack 偏好“已用资源多”的节点，因此 Pod 会继续堆在同一台机器上。

**练习**：用自定义配置切换策略。

```bash
$MV -scenario basic -config ../examples/config/minivolcano-scheduler.json -events 0
```

打开 `examples/config/minivolcano-scheduler.json`，删除 `binpack`（或把 `weight` 改成 `0`），再跑一次，观察 `nodeorder` 如何把 Pod 分散到两台节点。

## 27.8 实验七：拓扑——先选域，再选节点

**目标**：验证 hard 拓扑是“限域”而不是“加分”。

```bash
$MV -scenario topology -events 12
$MV -scenario group-topology -events 12
$MV -scenario task-topology -events 12
```

预期：

- topology：4 个 Pod 全部落在 rack-a（leaf-a1/leaf-a2），没有一个去 rack-b；
- group-topology：serve-a 在 rack-1、serve-b 在 rack-2，即使 rack-1 还有空节点；
- task-topology：ps 与 worker 被放到同一节点（或按配置的亲和策略）。

**为什么**：network-topology-aware 在 Session 打开时先选域，再在 Predicate 中限制节点；task-topology 用任务角色图做硬过滤/软偏好。

**练习**：

```bash
$MV -scenario topology -metrics -events 0
```

观察 `minivolcano_node_idle`，确认 rack-b 的节点完全空闲——这不是“没调度”，而是被域约束排除了。

## 27.9 实验八：预留、SLA 与 TDM

**目标**：理解三类时间维度策略。

```bash
$MV -scenario reservation -events 12
$MV -scenario sla -events 12
$MV -scenario tdm -events 12
```

预期：

- reservation：4 卡预留窗口 [0,6)，8 卡作业在 tick 6 前无法凑齐，窗口结束后一次拿到；
- sla：等待超预算的作业排到前面；
- tdm：preemptable 作业只能在窗口内使用 revocable 节点。

**为什么**：reservation 从 `IdleFor` 中扣除“预留中尚未使用的部分”，不改变 `allocated`；SLA 改 JobOrder；TDM 同时改 Predicate 和 VictimTasks。

**注意**：MiniVolcano 的 `Reservation` 是教学抽象。真实 Volcano 没有 Reservation CRD，等价语义要用 Queue guarantee + reservation 插件 + SLA 表达（第 12 章）。

## 27.10 实验九：异构设备与 NUMA

**目标**：验证资源名、nodeSelector 与 NUMA 约束。

```bash
$MV -scenario heterogeneous -events 12
$MV -scenario numa -events 12
```

预期：

- heterogeneous：作业只落在 `a100-*` 节点；
- numa：12 CPU 的 Pod 无法放入单个 8 CPU 的 NUMA 节点，保持 Pending；4 CPU 的 Pod 可以运行。

**为什么**：异构是标签/资源名匹配；NUMA 是“单个 NUMA 节点内满足全部请求”的额外约束。GPU 这类节点级设备不应被 NUMA 的 CPU/内存维度误伤（第 25 章的真实缺陷）。

**练习**：

```bash
$MV -scenario numa -explain default/big
```

检查失败原因是否明确指出“无法放入任何 NUMA 节点”。

## 27.11 实验十：重调度与中断保护

**目标**：验证 shuffle 的“提名—干跑—驱逐—重打包”链路，以及 PDB 的保护。

```bash
$MV -scenario shuffle -events 20
$MV -scenario pdb -events 20
```

预期：

- shuffle：两个半满节点被整理成一个满节点 + 一个空节点；
- pdb：crit-a 的 PDB 允许 1 次驱逐，crit-b 的 PDB 不允许；high-2 需要 2 个受害者，最终整体回滚。

**为什么**：rescheduling 只提名受害者，shuffle 只执行驱逐，下一轮 allocate 用 binpack 重打包。PDB 通过 Preemptable/Reclaimable/Disruptable 三个 hook 同时保护 preempt、reclaim、shuffle。

**练习**：

```bash
$MV -scenario pdb -explain default/high-2
```

确认证据链里有 `no preemptable victims`，并且 crit-b 的 4 个副本一个都没少。

## 27.12 实验十一：控制器、Cron 与 JobFlow

**目标**：区分调度器与控制器。

```bash
$MV -scenario retry -events 20
$MV -scenario cron -events 20
$MV -scenario jobflow -events 20
```

预期：

- retry：PodFailed 触发 RestartJob，重试次数增加，最终 Completed；
- cron：按 tick 周期创建作业，历史保留不超过限制；
- jobflow：prep → train → eval 三步按依赖完成。

**为什么**：调度器只负责放置；重启、重试、周期、DAG 都由 Manager 的状态机推进。把生命周期塞进调度器，会导致“调度器一重启，作业状态就乱”。

**练习**：

```bash
$MV -scenario retry -events 30
```

数一数 `Failed`、`Restarting`、`Scheduled` 事件的顺序，确认没有“失败后直接 Completed”的跳变。

## 27.13 实验十二：可观测性与故障演练

**目标**：把指标、事件、explain 串成排障流程。

```bash
$MV -scenario basic -metrics -events 20
$MV -scenario drf -explain default/a2
$MV -scenario resourcequota -events 20
$MV -scenario nodegroup -events 20
```

预期指标：

```text
minivolcano_scheduler_sessions_total
minivolcano_scheduler_events_total{type="Scheduled"}
minivolcano_queue_allocated{queue="default",resource="cpu"}
minivolcano_jobs{phase="Pending"}
minivolcano_node_idle{node="node-a",resource="cpu"}
```

故障演练（每次改一个变量，并用 `-config` 指定改过的 JSON）：

```bash
# 1) 从 actions 中删掉 preempt，跑 priority：
#    train-high 因资源不足保持 Pending，不再抢占。
$MV -scenario priority -config ../examples/config/minivolcano-scheduler.json \
    -no-check -events 20

# 2) 从 tier-1 删掉 pdb，跑 pdb：
#    没有 PDB 保护，high-2 可以驱逐 2 个副本并运行。
$MV -scenario pdb -config ../examples/config/minivolcano-scheduler.json \
    -no-check -events 20

# 3) 从 tier-2 删掉 resourcequota，跑 resourcequota：
#    b 的 3 CPU 作业不再被配额拒绝，最终和 a/c 一起运行（总 6 CPU ≤ 8）。
$MV -scenario resourcequota -config ../examples/config/minivolcano-scheduler.json \
    -no-check -events 20

# 4) 不改配置，只做反事实：team-a 的 capability 改成 2。
$MV -scenario proportion -what-if team-a/nvidia.com/gpu=2 -no-check -events 0
```

预期差异：

| 改动 | 观察点 |
|---|---|
| 去掉 `preempt` | priority 场景 high 不再 Running |
| 去掉 `pdb` | pdb 场景 high-2 变成 Running，crit 副本减少 |
| 去掉 `resourcequota` | resourcequota 场景 b 变成 Running |
| `what-if cap=2` | proportion 场景 a-job 保持 Pending |

注意：`gang` 插件删掉后 allocate 仍有 fallback 的 `JobReady` 检查，所以“删 gang 导致部分启动”**不是**一个有效的故障注入；这是一个容易误判的实现细节。

## 27.14 实验十三：角色门槛与提前完成

**目标**：验证 `minTaskMember` 不是“把总数拆开”，以及 `minSuccess` 完成后不泄漏副本。

```bash
$MV -scenario task-gang -events 8
$MV -scenario task-gang -explain default/train
$MV -scenario min-success -events 8
```

预期（task-gang）：

```text
JOB default/train
  phase=Running minAvailable=5 replicas=9
  ready=5/5 running=5 pending=4
PODS
  train-master-0   Running
  train-worker-0..3 Running
  train-worker-4..7 Pending
```

预期（min-success）：

```text
JOB default/train
  phase=Completed
  succeeded=4 failed=0
NODE cpu-node  allocated=cpu=0  allocatable=cpu=2
```

**为什么**：`task-gang` 的节点只有 5 个槽，allocate 绑定了 1 master + 4 worker；`Statement` 在 trial cache 上同时检查 `ReadyCount >= 5` 与 `ReadyCountByTask(master)>=1, worker>=4`，满足才 Commit。`min-success` 的 `minSuccess=2` 在 2 个分片成功时触发 `completeJob`，把未启动的 2 个副本标成 `Succeeded` 并释放配额。

**故障注入**：

```bash
# 1) 把 task-gang 的 job minAvailable 改成 4：角色门槛失效。
#    预期可能出现 5 个 worker 运行、master 仍在 Pending（取决于 TaskOrder），
#    这正是 6.2.2 的“静默失效”边界。
# 2) 把 min-success 的 minSuccess 改成 1：第一个分片成功就完成，
#    观察剩余副本是否被终止、节点是否释放。
# 3) 把 min-success 的 minAvailable 改成 4：gang 要求 4 个副本，
#    但节点只有 2 个槽，作业永远 Pending，minSuccess 不会触发。
```

**边界**：Mini 的 `minSuccess` 没有模拟“全部终止后仍不足 minAvailable 则 Failed”的第二阶段；真实 Volcano 的判定见 4.2.4。

## 27.15 实验十四：真实集群（可选）

**目标**：在 Kubernetes 上复现 gang、队列、抢占。

前置：一个 3 节点以上的集群，节点有 `nvidia.com/gpu` 或至少可调度 CPU。

```bash
helm repo add volcano-sh https://volcano-sh.github.io/helm-charts
helm repo update
helm install volcano volcano-sh/volcano -n volcano-system --create-namespace --version 1.16.0

kubectl get pods -n volcano-system
kubectl get crd | grep volcano
kubectl get queue

kubectl apply -f examples/queues/priorityclasses.yaml
kubectl apply -f examples/queues/queues.yaml
kubectl apply -f examples/jobs/vcjob-pytorch.yaml

kubectl get vcjob,podgroup -w
kubectl describe vcjob pytorch-job
kubectl logs -n volcano-system deploy/volcano-scheduler --tail=200
```

观察点：

1. Pod 已创建但 `nodeName` 为空 —— 调度器还没绑定；
2. PodGroup 的 phase 从 Pending → Inqueue → Running；
3. `kubectl get events` 里是否有 `Evicted`、`Preempted`；
4. 修改 scheduler configmap 后是否 rollout restart。

**注意**：示例镜像、节点名、GPU 标签需要按集群替换；不要在共享集群上直接跑抢占/回收实验。

## 27.16 一张实验地图

| 实验 | 场景 | 主要语义 | 对应章节 |
|---:|---|---|---|
| 1 | basic | 主链路、explain | 3、4 |
| 2 | gang | 事务、回滚 | 6 |
| 3 | proportion/reclaim | 配额、借用、回收 | 5、8 |
| 4 | priority | 同队列抢占 | 9 |
| 5 | drf/fairshare/hierarchy | 三种公平 | 8 |
| 6 | binpack | 打分方向 | 10 |
| 7 | topology/group-topology/task-topology | 域、分组反亲和、角色 | 11 |
| 8 | reservation/sla/tdm | 时间维度 | 12 |
| 9 | heterogeneous/numa | 设备与 NUMA | 10、13 |
| 10 | shuffle/pdb | 重调度与保护 | 9、11 |
| 11 | retry/cron/jobflow | 生命周期 | 14、15 |
| 12 | metrics/resourcequota/nodegroup | 可观测性与治理 | 16、5 |
| 13 | task-gang/min-success | 角色门槛、提前完成 | 4、6 |
| 14 | 真实集群 | 端到端 | 3、19 |

## 27.17 实验记录模板

做实验时建议记录下面几项，方便复盘和写事故报告：

```text
实验：
输入（scenario/config/what-if/ticks）：
观察到的相位：
关键事件（带 tick）：
账本是否守恒：
与预期的差异：
归因（enqueue / allocate / preempt / reclaim / controller / kubelet）：
下一步：
```

最后一项“归因”最重要。调度问题的时间主要浪费在跨层猜测；把问题钉在某一层，证据链才有意义。

## 27.18 本章小结

- 13 个离线实验覆盖了 Volcano 的主要语义，全部可重复。
- 每个实验都先看预期，再做故障注入，最后写归因。
- `-explain` 把 Pending 变成证据链；`-metrics` 把状态变成时间序列。
- 真实集群实验只做最小闭环，不在共享环境跑破坏性操作。

下一章讨论真实世界的迁移、选型与容量规划。

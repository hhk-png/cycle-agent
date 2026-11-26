# 第 2 章 架构与核心对象

## 2.1 四个核心组件，一条主链路

Volcano 安装后在 `volcano-system` 命名空间里运行三个 Deployment 和一个命令行工具；v1.16 还提供可选的独立 HyperNode controller：

| 组件 | 容器/二进制 | 职责 |
|---|---|---|
| `volcano-scheduler` | `vc-scheduler` | 打开 Session，执行 actions，调用 plugins，决定 Pod 绑定 |
| `volcano-controllers` | `vc-controller-manager` | Job、PodGroup、Queue、CronJob、JobFlow、HyperNode 控制器 |
| `volcano-admission` | `vc-webhook-manager` | Mutating/Validating Webhook：默认值、合法性校验 |
| `vcctl` | 本地 CLI | 查看/操作 Job、Queue、PodGroup |
| `vc-hypernode-controller-manager`（可选） | 独立二进制 | v1.16 起可独立部署的 HyperNode controller，供非 Volcano 调度器复用拓扑 API |

`volcano-admission-init` Job 负责生成 webhook 证书。很多安装问题最后都落在证书或 webhook 配置上。

一条完整的提交链路：

```text
kubectl apply -f vcjob.yaml
        │
        ▼
API Server ──► MutatingWebhook（补 schedulerName、默认 queue、校验 minAvailable）
        │
        ▼
Job Controller ──► 创建 PodGroup + 每个 task 的 Pod
        │
        ▼
Scheduler Informer ──► Cache ──► Snapshot ──► Session
                                               │
                          ┌────────────────────┼────────────────────┐
                          ▼                    ▼                    ▼
                       enqueue              allocate             preempt/reclaim
                          │                    │                    │
                          └────────── plugins / tiers ─────────────┘
                                               │
                                               ▼
                                       Bind Pod to Node
                                               │
                                               ▼
                                            kubelet
```

关键点：

- **Job controller 先创建 Pod，调度器再决定 Pod 去哪。** 所以你会看到 Pending 的 Pod 已经存在，但 `nodeName` 为空。
- **PodGroup 是调度器看到的“作业”——不是 Job。** Job 只是面向用户的高层对象；调度器通过 PodGroup 的 `minMember`、`minResources`、`queue`、`priority` 做决策。
- **Admission 会改写对象。** 例如给 Job 补 `schedulerName: volcano`、默认队列；给 PodGroup 补最小资源。关掉 webhook 会导致大量“作业不调度”的假故障。

## 2.2 对象模型

### 2.2.1 Volcano Job

```yaml
apiVersion: batch.volcano.sh/v1alpha1
kind: Job
metadata:
  name: train
spec:
  schedulerName: volcano
  queue: team-a
  priorityClassName: high-priority
  minAvailable: 4
  maxRetry: 3
  tasks:
    - name: worker
      replicas: 4
      template: { ... PodTemplateSpec ... }
  policies:
    - event: PodEvicted
      action: RestartJob
```

Job 的关键字段：

| 字段 | 含义 | 常见误区 |
|---|---|---|
| `minAvailable` | gang 的最小可运行副本数，默认等于所有 task 副本之和 | 以为它只是“期望副本数”；实际上它决定是否整体启动 |
| `maxRetry` | 作业级重试次数 | 与 Pod 的 `restartPolicy` 不是一回事 |
| `tasks[].replicas` | 该角色的副本数 | 所有 task 副本之和才是总 Pod 数 |
| `policies` | 事件 → 动作的生命周期策略 | 不配策略时，失败/驱逐后的行为可能与预期不同 |
| `plugins` | **作业插件**（svc/ssh/pytorch/…），不是调度器插件 | 与 scheduler 的 gang/drf 插件完全两套体系 |
| `queue` | 所属队列 | 队列必须存在且为 Open |
| `schedulerName` | 通常为 `volcano` | 省略时 admission 会补默认调度器；写成 `default-scheduler` 时 Volcano 不管 |

### 2.2.2 PodGroup

```yaml
apiVersion: scheduling.volcano.sh/v1beta1
kind: PodGroup
metadata:
  name: train
  namespace: default
spec:
  minMember: 4
  minResources:
    cpu: "8"
    memory: 16Gi
    nvidia.com/gpu: "4"
  queue: team-a
  priorityClassName: high-priority
```

PodGroup 是调度器的输入契约：

- `minMember`：至少多少个 Pod 同时可调度，才允许整个组启动。
- `minResources`：这 `minMember` 个 Pod 的最小资源总和。enqueue 阶段用它判断队列/集群是否有希望容纳。
- `queue`、`priorityClassName`：从 Job 继承，也可以由用户直接创建 PodGroup 时指定。
- `labels`：PodGroup 级选择器的匹配对象（v1.16 的拓扑反亲和按它选中对端）。
- `topologyAffinity`（v1.16）：PodGroup 级拓扑反亲和，详见第 11.4 节。

非 Volcano Job 的工作负载（Deployment、StatefulSet、裸 Pod）要获得 gang 调度，需要：

1. 手动创建 PodGroup；
2. Pod 上注解 `scheduling.k8s.io/group-name: <podgroup-name>`；
3. Pod 的 `schedulerName` 设为 `volcano`。

`examples/jobs/podgroup-raw.yaml` 给出了完整例子。

### 2.2.3 Queue

```yaml
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata:
  name: team-a
spec:
  weight: 3
  reclaimable: true
  capability:
    cpu: "64"
    nvidia.com/gpu: "8"
  guarantee:
    cpu: "8"
    nvidia.com/gpu: "2"
```

Queue 是集群级对象，五个核心字段：

- `weight`：加权公平的权重，proportion 插件用它计算 deserved。
- `capability`：硬上限，任何时刻该队列（含子队列）不能超过。
- `deserved`：capacity 插件下显式指定的应得份额；proportion 下由 weight 推导。
- `guarantee`：保底，其他队列不能占用；回收时不能跌破。
- `reclaimable`：超出 deserved 的部分是否可被其他队列回收，默认 `true`。

此外还有 `parent`（层级队列）、`priority`、`state`（Open/Closed）、`dequeueStrategy` 等字段。

### 2.2.4 其他 CRD

| CRD | API Group | 作用 |
|---|---|---|
| `CronJob` | `batch.volcano.sh/v1alpha1` | 定时 Volcano Job（v1.13+） |
| `JobFlow` | `flow.volcano.sh/v1alpha1` | Job 级 DAG 编排 |
| `JobTemplate` | `flow.volcano.sh/v1alpha1` | JobFlow 引用的可复用 JobSpec |
| `HyperNode` | `topology.volcano.sh/v1alpha1` | 网络拓扑树（v1.11+，v1.13 增强） |
| `NamespaceQueue` | `scheduling.volcano.sh/v1beta1` | namespace 级队列（v1.16 Alpha，默认关闭） |
| `NodeShard` | `shard.volcano.sh/v1alpha1` | 动态节点分片（v1.14 Alpha，配合 Sharding Controller） |
| `Numatopology`（shortName `numatopo`） | `nodeinfo.volcano.sh/v1alpha1` | NUMA/CPU 拓扑信息，供 NUMA-aware 插件使用 |
| `Command` | `bus.volcano.sh/v1alpha1` | 操作 Job/Queue 的命令对象；`vcctl queue operate`/作业操作会创建它 |
| `Reservation` | 无 | **Volcano 核心没有独立 Reservation CRD**；预留由 Queue guarantee + reservation 插件/elect/reserve 动作表达 |

最后一行很重要：很多资料把“资源预留”讲成一个 Reservation 对象，但 Volcano 的实现并不是这样。第 12 章会详细拆解。

## 2.3 状态机

### 2.3.1 Job 状态

```text
Pending ──PodGroup Inqueue / Running Pod 数达到 minAvailable──► Running
   ▲                                           │
   │                                           ├── 满足完成条件 ──► Completing ──► Completed
   │                                           ├── Pod 失败/驱逐 ──► Restarting ──► Pending
   │                                           └── 策略终止 ──► Terminating ──► Terminated
   │                                                        └── Aborting ──► Aborted
   └──────────────────── 失败且不可重试 ──► Failed
```

注意：`Inqueue` 是 **PodGroup** 的 phase，不是 Job phase。enqueue action 把 PodGroup 从 Pending 推到 Inqueue；Job 仍保持 Pending，直到 Running Pod 数达到 `minAvailable`。`Running/Restarting/Completing/Terminating/Aborting/Failed` 由 Job controller 根据 Pod 状态与 `policies` 推进。

### 2.3.2 PodGroup 状态

`Pending → Inqueue → Running → Completed`，异常时为 `Unknown`。PodGroup 是调度器的视角；它不一定与 Job 状态完全同步，排障时要同时看两者。

### 2.3.3 Queue 状态

- **Open**：接受新 PodGroup，正常调度。
- **Closed**：不接受新作业，已有作业仍可运行/完成；只有 Closed 的队列可以删除。
- **Closing**：用户发出 close 命令后，若仍有 PodGroup，控制器先进入 Closing，排空后变 Closed。
- **Unknown**：异常/网络抖动导致状态不可判定。

用户通过 `vcctl queue operate --action open|close` 发出请求（底层是创建一个指向 Queue 的 `Command` 对象）；控制器维护 `status.state`。Queue 没有 `spec.state` 字段，也不要手改 status。

## 2.4 调度器内部结构

Volcano 调度器的包结构大致如下（v1.15/v1.16）：

```text
pkg/scheduler/
├── cache/           # 集群缓存、快照、队列/作业/节点信息
├── framework/       # Session、Statement、插件接口、扩展点
├── actions/         # enqueue, allocate, preempt, reclaim, backfill, shuffle, ...
├── plugins/         # gang, priority, drf, proportion, capacity, ...
├── api/             # 调度器内部 API 类型
├── conf/            # 配置解析
└── metrics/         # Prometheus 指标
```

一次 Session 的生命周期：

1. **Snapshot**：复制 cache，保证本次调度看到一致视图。
2. **OpenSession**：每个插件 `OnSessionOpen`，向 Session 注册扩展函数。
3. **Actions**：按配置顺序执行。每个 action 从 Session 取队列/作业/节点，调用扩展函数。默认配置通常只启用 enqueue → allocate → backfill；常见生产配置再加 preempt → reclaim，启用重调度时再加 shuffle，位置可在 allocate 前或后。
4. **Statement**：分配/驱逐先记录为操作；gang 成功才提交，否则回滚。
5. **CloseSession**：插件收尾，统计、事件、绑定写回。

Volcano 的插件扩展点（部分）：

| 扩展点 | 作用 |
|---|---|
| `JobOrderFn` | 同一队列内作业排序 |
| `QueueOrderFn` | 队列顺序（公平性） |
| `TaskOrderFn` | 作业内任务顺序 |
| `PredicateFn` | 节点硬过滤 |
| `NodeOrderFn` | 节点打分 |
| `PreemptableFn` | 某个受害者是否可被抢占 |
| `ReclaimableFn` | 某个队列的 Pod 是否可被回收 |
| `JobReadyFn` | gang 是否满足 |
| `JobValidFn` | 作业是否合法 |
| `JobEnqueueableFn` | 是否允许入队 |
| `OverusedFn` | 队列是否超额 |
| `AllocatableFn` | 队列是否还能分配 |
| `JobStarvingFn` | 作业是否处于饥饿 |
| `VictimTasksFn` | shuffle 的受害者提名（rescheduling/tdm；pdb 在上游也参与） |
| `DisruptableFn` | 受害者是否允许被主动中断（Mini 的 pdb 用它保护 shuffle） |

这张表是第 7 章和 MiniVolcano 的骨架。

## 2.5 组件之间的边界

### 2.5.1 调度器不创建 Pod

调度器只修改 `pod.spec.nodeName`（或绑定缓存），不负责创建 Pod。Pod 的创建、删除、重启由 Job controller 完成。这个边界让 Volcano 可以调度任何带 PodGroup 的工作负载。

### 2.5.2 控制器不做放置决策

Job controller 不知道节点忙不忙。它只根据 Pod 状态和策略推进 Job 状态。把放置决策塞进控制器，是很多自研调度器最后失控的原因。

但 Job controller 会调用 **作业插件**（`spec.plugins`）的 `OnPodCreate`，在创建 Pod 前注入 `VC_TASK_INDEX`、`MASTER_ADDR` 等环境变量，或创建 Service/ConfigMap。作业插件只改对象，不参与节点选择；它的接口与开发方式见 4.3 和 33.6。

### 2.5.3 Webhook 不做调度

Admission 只做默认值和合法性校验。它可能影响“作业能不能被创建”，但不影响“作业能不能被调度”。

### 2.5.4 vcctl 不是控制器

`vcctl` 只是 API 客户端。它展示的状态来自 CRD status，不是实时调度器内存。

## 2.6 MiniVolcano 的映射

为了让后面的实现章节有参照，先给出对象映射：

| 真实 Volcano | MiniVolcano | 说明 |
|---|---|---|
| `batch.volcano.sh/v1alpha1 Job` | `api.Job` | 保留 tasks/minAvailable/policies/queue/priority |
| `scheduling.volcano.sh/v1beta1 PodGroup` | `api.PodGroup` | 由 SubmitJob 自动创建 |
| `Queue` | `api.Queue` | 保留 weight/capability/guarantee/reclaimable/parent |
| `HyperNode` | `api.HyperNode` | 保留 tier/parent/children/members |
| `JobFlow/JobTemplate` | `api.JobFlow` | 简化：步骤内联 JobSpec，而不是引用模板 |
| `CronJob` | `api.CronJob` | 简化：以 tick 为周期，而不是 cron 表达式 |
| 无 Reservation CRD | `api.Reservation` | **教学抽象**，模拟队列/作业级预留，不是真实 API |
| informer/cache | `internal/cache` | 深拷贝快照 |
| Session/Statement | `internal/framework` | 插件 hook + 事务 |
| actions/plugins | `internal/actions`、`internal/plugins` | 6 个 action（含 shuffle）、23 个插件（含 rescheduling 与 group-topology-affinity） |

MiniVolcano 不追求 API 兼容，而追求**语义可解释**：每个真实概念都能在代码里找到对应实现，且能通过场景验证。

## 2.7 一次绑定的完整时间线

前面给的是组件图，这一节把时间轴展开。理解每一步“谁写了什么、谁还看不到”，排障时就不会把正常延迟当成故障。

假设用户执行 `kubectl apply -f vcjob.yaml`，集群里有一个名为 `node-a` 的节点：

| # | 发生的事 | 执行方 | 此时谁能看到 | 常见误判 |
|---:|---|---|---|---|
| 1 | API Server 认证/鉴权、schema 校验 | kube-apiserver | 请求链内部 | 以为提交即通过 |
| 2 | MutatingWebhook 补 `schedulerName`、默认 queue、`minAvailable` | vc-webhook-manager | 后续 admission 链 | 关掉 webhook 后作业“不调度” |
| 3 | ValidatingWebhook 校验 `minAvailable`、task 名、queue 合法性 | vc-webhook-manager | 同上 | 把校验失败当成调度失败 |
| 4 | 对象写入 etcd，watch 事件开始传播 | kube-apiserver | 所有 watch 者（异步） | 以为写入即调度 |
| 5 | Job controller 创建 PodGroup 与 Pod | vc-controller-manager | API Server 的 watch 流 | 以为调度器创建 Pod |
| 6 | Pod 进入 `Pending`，`spec.nodeName` 为空 | kube-apiserver | 调度器 informer | 看到 Pending 就断定资源不足 |
| 7 | 调度器 informer 收到事件，更新本地 cache | vc-scheduler | 调度器进程内 | 以为 cache 与 API Server 强一致 |
| 8 | 下一轮 Session 克隆快照，执行 enqueue/allocate | vc-scheduler | 调度器进程内 | 以为每次提交立刻触发一轮 |
| 9 | Statement commit，Binder 异步写 Binding | vc-scheduler | API Server | 以为 commit 等于 Pod 已绑定 |
| 10 | kube-apiserver 把 `nodeName` 写入 Pod | kube-apiserver | kubelet/watch 者 | 把“已绑定未 Running”当成调度失败 |
| 11 | kubelet 拉镜像、挂卷、启动容器 | kubelet | Pod status | 把镜像拉取失败归因于调度器 |
| 12 | Pod `Running`，PodGroup 状态随后更新 | kubelet + PodGroup controller | 最终一致 | 状态短暂不一致是正常现象 |

四个工程结论：

1. **调度是异步的**。提交成功只代表对象被接受，不代表本轮 Session 会立刻处理它。调度器可能正在处理上一轮，或处于 leader 选举。
2. **Binder 是异步的**。Statement commit 后，绑定请求进入队列；如果 API Server 限流或网络抖动，会出现“调度器已决策、Pod 仍未 Bound”的窗口。
3. **cache 是最终一致的**。informer 有延迟，调度器可能基于稍旧的视图做决策；这也是为什么需要快照和乐观并发处理。
4. **状态传播有先后**。Pod 先 Running，PodGroup/Job 的 status 由控制器随后更新；排障时以 Pod 的 `nodeName` 和事件为准，不要只盯 CRD 的 phase。

一个具体的排障例子：

```text
Pod 有 nodeName=node-a，但 phase=Pending
  → 不是调度问题；看 kubelet 事件（镜像、卷、污点、资源）

Pod phase=Running，但 PodGroup phase=Inqueue
  → 不是调度问题；看 PodGroup controller 是否滞后或 leader 是否切换

Pod 没有 nodeName，且 scheduler 日志没有该 Job
  → 先看 schedulerName 是否 volcano、informer 是否连上 API Server
```

## 2.8 CRD 与 feature gate 版本矩阵

Volcano 的功能分散在 CRD、scheduler 插件、feature gate 三层。升级时最危险的不是镜像 tag，而是“CRD 已升级、控制器没升”或“插件已启用、gate 没开”。

| 能力 | API/对象 | 引入版本 | 默认状态 | 依赖 |
|---|---|---|---|---|
| Volcano Job / PodGroup / Queue | `batch.volcano.sh`、`scheduling.volcano.sh` | 早期 | GA | 无 |
| HyperNode | `topology.volcano.sh/v1alpha1` | v1.11 | 可用 | 手动或自动发现 |
| Cron VolcanoJob | `batch.volcano.sh/v1alpha1 CronJob` | v1.13 | GA | 无 |
| JobFlow / JobTemplate | `flow.volcano.sh/v1alpha1` | v1.10 起演进 | 可用 | 无 |
| NodeShard / 动态分片 | `shard.volcano.sh/v1alpha1 NodeShard` | v1.14 | Alpha | Sharding Controller |
| Agent Scheduler | 独立组件 | v1.14 | Alpha | 分片协调 |
| DRA 队列配额 | capacity 插件参数 | v1.15 | 关闭 | K8s DRA、feature gate |
| gangpreempt / gangreclaim | action 名称（配置里全小写） | v1.15 | Alpha | gang 插件、拓扑 |
| NamespaceQueue | `scheduling.volcano.sh/v1beta1 NamespaceQueue` | v1.16 | Alpha，默认关闭 | `NamespaceQueue=true` + 层级 capacity |
| fairshare | scheduler 插件 | v1.16 | 需显式启用 | 无 |
| group-topology-affinity | scheduler 插件 + PodGroup `topologyAffinity` | v1.16 | 需显式启用 | HyperNode |
| 独立 HyperNode controller | `vc-hypernode-controller-manager` | v1.16 | 可选 | 无 |
| Unschedulable Job Cache | scheduler 参数 | v1.16 | 默认关闭 | 失效机制 |

使用这张表的三个原则：

1. **Alpha 不等于可用**。Alpha 功能的 CRD schema、参数名、默认值都可能在 minor 之间变化；生产启用前要在同版本测试集群验证“创建、调度、升级、回滚”四条路径。
2. **gate 要三处一致**。NamespaceQueue 这类功能需要 scheduler、controllers、admission 同时打开 gate；只开一处会出现“对象能创建但没人处理”的半启用状态。
3. **CRD 先于控制器**。升级顺序通常是 CRD → controller → scheduler → admission；反过来会让新控制器读到旧 schema。

## 2.9 本章小结

- Volcano 由 scheduler、controller manager、admission、vcctl 四部分组成。
- 调度器看 PodGroup，不看 Job；Job controller 创建 Pod，调度器只绑定。
- Queue 是资源治理的核心，Job/PodGroup 是调度单位。
- Session + Statement + tier/plugin 是调度器可扩展性的来源。
- Volcano 没有独立的 Reservation CRD，预留是队列 guarantee 加插件/动作的组合。
- 调度链路是异步、最终一致的：提交、入队、决策、绑定、启动分属不同层，排障要按层取证。
- CRD、插件、feature gate 是三条独立的版本线，升级时要同时对齐。

下一章从零安装一个 Volcano，并跑通第一个作业。

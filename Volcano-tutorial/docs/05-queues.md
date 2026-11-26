# 第 5 章 队列、配额与层级

## 5.1 为什么 Queue 是 Volcano 的核心

没有队列，Volcano 只是一个 gang 调度器；有了队列，它才成为多租户平台。Queue 负责回答四个问题：

1. **谁应该先用？** `weight` 与 deserved。
2. **谁最多能用多少？** `capability`。
3. **谁至少能拿到多少？** `guarantee`。
4. **空闲时能不能借？借了能不能收回？** `reclaimable` + reclaim。

## 5.2 Queue 字段

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
    memory: 256Gi
    nvidia.com/gpu: "8"
  guarantee:
    cpu: "8"
    memory: 32Gi
    nvidia.com/gpu: "2"
  # deserved: ...      # capacity 插件下显式指定
  # parent: org        # 层级队列
  # priority: 10
  # dequeueStrategy: traverse
  # affinity:          # nodegroup 插件的节点组亲和/反亲和
  #   nodeGroupAffinity:
  #     requiredDuringSchedulingIgnoredDuringExecution: ["a100"]
  # 注意：Queue 没有 spec.state；开/关用 vcctl queue operate，
  # 控制器把结果写到 status.state。
```

| 字段 | 类型 | 默认 | 语义 |
|---|---|---|---|
| `weight` | int32 | 1 | 加权公平的权重；proportion 用它算 deserved |
| `capability` | ResourceList | 由 realCapability 推导 | 硬上限 |
| `deserved` | ResourceList | 由插件计算 | capacity 插件下的显式应得份额 |
| `guarantee` | ResourceList | 空 | 保底，其他队列不可占用 |
| `reclaimable` | *bool | true | 超出 deserved 的部分是否可被回收 |
| `parent` | string | root | 层级队列的父队列 |
| `priority` | int32 | 0 | 队列优先级（capacity 插件排序用） |
| `dequeueStrategy` | fifo/traverse | traverse | 队列内作业出队策略 |
| `affinity` | object | 空 | nodegroup 插件的节点组亲和/反亲和（见 5.7.3） |
| `type` | string | 空 | 队列类型，供上层平台/策略区分 |
| `extendClusters` | []Cluster | 空 | Volcano Global 的扩展集群列表（见第 20 章） |
| `status.state` | Open/Closed/Closing/Unknown | 控制器维护 | 是否接受新作业；用户通过 `vcctl queue operate` 请求切换 |

`capability` 的默认值不是“无限”。在 proportion 插件下，它通常取 **realCapability = 集群总资源 - 其他队列 guarantee 之和**。这意味着给一个队列设置过大的 guarantee，会压缩其他队列的默认上限。

`dequeueStrategy` 容易被忽略：

- `traverse`（默认）：队头作业卡住时，调度器继续尝试后面的作业，避免队头阻塞；
- `fifo`：严格按 FIFO，队头不能出队时后面的作业都等着，适合有严格顺序要求的流水线。

它不是 `JobOrderFn` 的替代品：插件决定“谁先比较”，`dequeueStrategy` 决定“队头卡住时要不要跳过”。

## 5.3 proportion：按权重分蛋糕

proportion 插件把集群资源按 weight 分给队列：

```text
deserved_i = total * weight_i / Σ weight
```

然后：

- 如果 `deserved_i > capability_i`，截断到 capability；
- 如果 `deserved_i < guarantee_i`，抬到 guarantee。

举例：集群 64 张 GPU，三个队列：

| 队列 | weight | guarantee | capability |
|---|---|---|---|
| team-a | 3 | 8 | 48 |
| team-b | 1 | 4 | 24 |
| team-c | 0 | 0 | 8 |

总权重 4：

- team-a 的初始 deserved = 48，未超 cap，得 48；
- team-b 的初始 deserved = 16，未超 cap，得 16；
- team-c 权重 0，初始 0；如果它有 guarantee，则抬到 guarantee。

注意 `capability` 之和 48+24+8=80 > 64，这没问题：capability 是“最多能用”，不是“预留”。

### 5.3.1 借用（borrowing）

仍用上面的例子：team-b 只用了 4 张卡，集群还有 12 张空闲。team-a 能否超过自己的 deserved 48？

- 如果 team-a 的 `capability=48`，不能：capability 是硬上限，即使集群空闲也不能超。
- 如果 team-a 的 `capability=64`，可以：48 是 deserved，额外 16 张是借来的；team-b 需要时，reclaim 可以把这 16 张收回去。

“超过 deserved 使用空闲资源”就是借用。借用的前提是：

- 资源确实空闲；
- 借入队列的 capability 允许；
- 借出队列的 reclaimable 为 true（回收时）。

### 5.3.2 回收（reclaim）

当 team-b 需要资源、而 team-a 超过了 deserved，reclaim action 会从 team-a 驱逐 Pod，直到：

- team-a 降回 deserved，或
- team-b 的需求被满足，或
- 到达 team-a 的 guarantee 底线（不会击穿保底）。

这就是 `guarantee` 的意义：它保证即使被回收，队列也不会低于这条线。

## 5.4 capacity：层级队列

proportion 只处理扁平队列。需要“部门 → 团队 → 项目”的树时，用 capacity 插件：

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: volcano-scheduler-configmap
  namespace: volcano-system
data:
  volcano-scheduler.conf: |
    actions: "enqueue, allocate, backfill, preempt, reclaim"
    tiers:
    - name: tier-1
      plugins:
      - name: priority
      - name: gang
      - name: conformance
    - name: tier-2
      plugins:
      - name: drf
      - name: predicates
      - name: capacity
        enableHierarchy: true
      - name: nodeorder
```

队列树：

```yaml
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata:
  name: org
spec:
  weight: 1
  capability:
    nvidia.com/gpu: "48"
---
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata:
  name: org-a
spec:
  parent: org
  weight: 1
  guarantee:
    nvidia.com/gpu: "8"
---
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata:
  name: org-b
spec:
  parent: org
  weight: 1
  guarantee:
    nvidia.com/gpu: "8"
```

语义：

- `org` 的上限 48 张卡；
- `org-a`、`org-b` 按 weight 各分 24；
- 子队列的 guarantee 是 8，父队列的分配不能击穿它；
- 父队列本身不直接承载作业（也可以承载，但不推荐）。

Volcano 会自动维护一个 `root` 队列作为树的根。定义 `parent: root` 或省略 parent 都表示顶层。

### 5.4.1 capacity 与 proportion 互斥

两者都试图定义“应得份额”，同时启用会让 `QueueOrder`/`Allocatable`/`Overused`/`Reclaimable` 钩子叠加，语义冲突。upstream v1.16 **没有硬性校验拒绝**这个组合，所以配错不会报错，只会得到难以解释的调度结果。生产上应二选一：

- 扁平租户、按权重公平：proportion。
- 多级组织、显式 capability/deserved/guarantee：capacity。

## 5.5 Queue 状态管理

```bash
# 关闭队列：不再接受新作业，已有作业继续跑
vcctl queue operate --name team-b --action close

# 重新打开
vcctl queue operate --name team-b --action open

# 查看（status.state 才是实际状态）
kubectl get queue team-b -o custom-columns='NAME:.metadata.name,STATUS:.status.state'
```

`vcctl` 实际是创建一个指向 Queue 的 `bus.volcano.sh/v1alpha1 Command` 对象，Queue controller 消费命令后更新 `status.state`。也可以用 `kubectl` 手动创建 Command，但不要直接改 status。

状态机：

```text
Open ──close 命令──► Closing（仍有 PodGroup）──► Closed（排空）
  ▲                                                │
  └──────────────── open 命令 ◄────────────────────┘
```

规则：

- 用户通过 `close`/`open` 命令请求切换；Closing 由控制器管理。
- 只有 `status.state: Closed` 的队列可以删除。
- `default` 和 `root` 不能删除。
- 向非 Open 队列提交作业会失败或挂起。

常见的“队列关不掉”：队列处于 Closing 且仍有 PodGroup 未完成。需要等作业结束，或先终止作业。

## 5.6 NamespaceQueue（v1.16，Alpha）

### 5.6.1 它解决什么问题

集群级 Queue 有一个组织上的矛盾：**平台团队希望控制总配额，业务团队希望在自己的 namespace 里继续细分队列**。如果让业务团队直接创建集群级 Queue，他们就能看到/修改别人的队列；如果不让，所有队列调整都要走平台工单。

NamespaceQueue 把队列树“下沉”一层：

- 管理员用集群 Queue 的 `spec.allowedNamespaces` 授权某个 namespace；
- 租户在该 namespace 内创建 `NamespaceQueue`，自己维护子树；
- 同名 NamespaceQueue 可以出现在不同 namespace，**身份互不共享**；
- 整棵树最终挂到管理员指定的集群 Queue 下，总配额仍由平台控制。

它不是“每个 namespace 一个 Queue”的语法糖，而是**命名空间内的层级队列 API**。默认关闭，属于 Alpha。

### 5.6.2 授权与对象

管理员先创建（或复用）一个集群 Queue，并列出允许接入的 namespace：

```yaml
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata:
  name: research
spec:
  weight: 1
  capability:
    cpu: "256"
    nvidia.com/gpu: "64"
  allowedNamespaces:
    - team-a
    - team-b
```

租户在 `team-a` 内创建自己的队列树：

```yaml
apiVersion: scheduling.volcano.sh/v1beta1
kind: NamespaceQueue
metadata:
  name: training
  namespace: team-a
spec:
  parent: cluster/research       # 挂到集群 Queue
  capability:
    cpu: "100"
    nvidia.com/gpu: "32"
  deserved:
    cpu: "80"
    nvidia.com/gpu: "24"
  guarantee:
    cpu: "40"
    nvidia.com/gpu: "8"
  reclaimable: true
  priority: 10
---
apiVersion: scheduling.volcano.sh/v1beta1
kind: NamespaceQueue
metadata:
  name: inference
  namespace: team-a
spec:
  parent: training               # 同 namespace 的 NamespaceQueue
  capability:
    cpu: "40"
    nvidia.com/gpu: "8"
```

每个维度必须满足 `guarantee <= deserved <= capability`。`allowedNamespaces` 为空表示拒绝所有；`["*"]` 表示允许全部 namespace。开启 feature gate 后新建的 `default` 集群 Queue 默认是 `["*"]`，已存在的 Queue 不会被自动改写——升级后要手动检查。

完整示例见 `examples/queues/namespace-queue.yaml`（一个集群 Queue + 两个 NamespaceQueue）。

### 5.6.3 parent 语法与作业引用

`NamespaceQueue.spec.parent` 有三种写法：

| 写法 | 含义 |
|---|---|
| `cluster/<name>` | 引用集群级 Queue |
| `<name>`（无前缀） | 引用**同 namespace** 的另一个 NamespaceQueue |
| 省略 | 默认挂到 `cluster/default` |
| `cluster/root` | 非法；调度器的 root 不能作为租户父队列 |

作业/PodGroup 的 `queue` 字段用 `namespace/<name>` 引用 NamespaceQueue：

```yaml
spec:
  queue: namespace/training
```

不带 `namespace/` 前缀时仍然按集群 Queue 解析。跨 namespace 引用不支持——这是有意的隔离边界。

### 5.6.4 启用、限制与坑

```bash
helm upgrade --install volcano volcano-sh/volcano \
  -n volcano-system \
  --set custom.namespace_queue_enable=true
```

清单安装则给 scheduler、controllers、admission 都加：

```text
--feature-gates=NamespaceQueue=true
```

限制与注意事项：

1. **必须使用层级 capacity**（插件级 `enableHierarchy: true`，不是 `arguments` 里的键），不要与 proportion 同时启用。
2. NamespaceQueue 是 Alpha，CRD schema 与字段可能在 patch/minor 间变化。
3. v1.16 的 NamespaceQueue CRD 没有 `helm.sh/resource-policy: keep` 注解；`helm uninstall` 可能连带删除 CRD 与所有 NamespaceQueue，生产升级/卸载前先备份。
4. 作业只有在 NamespaceQueue `Ready=True` 且 `Authorized=True` 时才会被接收；排障要先看这两个 condition。
5. 租户能创建队列，不等于能突破平台给的 `capability`；但租户可以把 `guarantee` 设得接近 `capability`，相当于在子树内“锁死”资源。平台要在准入侧限制这个比例。

### 5.6.5 什么时候值得用

适合：

- 一个大部门/子公司有多个项目，平台不想替他们维护每一个子队列；
- 需要“平台管总账、租户管细账”的自治模型；
- 多 namespace 之间必须复用同名队列（如每个业务都有 `prod`/`dev`）。

不适合：

- 队列树稳定、变更很少——直接集群 Queue 更简单；
- 仍在使用 proportion；
- 团队没有能力理解父子配额与 guarantee 的约束。

## 5.7 ResourceQuota 与 NodeGroup：另外两本账

Queue 不是唯一的资源账本。生产集群通常同时存在四种约束：

| 账本 | 作用域 | 拒绝点 | 典型问题 |
|---|---|---|---|
| Queue / NamespaceQueue | 调度器 | enqueue/allocate | 配额够但节点不够 |
| Kubernetes ResourceQuota | namespace | API Server 准入 | Pod 创建失败，调度器看不到 |
| NodeGroup limits | 队列 × 节点组 | 调度器 | 队列总量没超，但某个节点组超了 |
| 节点 allocatable | 节点 | 绑定 | 碎片导致“总量够、形状不够” |

### 5.7.1 ResourceQuota 插件

Volcano 的 `resourcequota` 插件在 **enqueue** 阶段检查 namespace 的原生 `ResourceQuota`：

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: team-a-quota
  namespace: team-a
spec:
  hard:
    requests.cpu: "100"
    requests.memory: 200Gi
    requests.nvidia.com/gpu: "16"
```

插件的判断近似为：

```text
Used + Pending + 本作业请求 <= Hard
```

它注册 `JobEnqueueableFn`，不参与节点打分。注意：

- 这是 **namespace 级**硬边界，和 Queue 的租户份额是两套账；
- ResourceQuota 由 API Server 在创建 Pod 时执行，Volcano 插件只是在入队时提前判断；
- 两者配置不一致时，常见结果是“Job 入队了，但 Pod 创建被拒”或“Pod 创建了，但永远 Pending”；
- 开启插件前确认所有相关 namespace 都有配额对象，否则行为取决于插件对“无配额”的处理。

推荐做法：ResourceQuota 设为 namespace 的**物理上限**（略高于 Queue capability），Queue 负责**调度公平**。不要用 ResourceQuota 做租户公平，也不要用 Queue 做 API 层硬拒绝。

### 5.7.2 NodeGroup 资源上限（v1.16）

NodeGroup 解决“同一个队列在不同节点池上的上限不同”。例如训练队列总共可以用 64 卡，但在 A100 池最多 32 卡、H100 池最多 16 卡。

节点用标签归属：

```yaml
metadata:
  labels:
    volcano.sh/nodegroup-name: a100
```

队列用注解声明每个节点组的资源上限：

```yaml
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata:
  name: training
  annotations:
    volcano.sh/nodegroup-resource-limits: |
      {"a100": {"cpu": "128", "nvidia.com/gpu": "32"},
       "h100": {"nvidia.com/gpu": "16"}}
spec:
  capability:
    nvidia.com/gpu: "64"
```

语义：

- 只限制注解里**显式出现**的维度；没写的资源不受该节点组限制；
- 没列出的节点组不限制；
- 子队列**不继承**父队列的注解，需要各自声明；
- 超限时 predicate 返回 `nodegroup <name> resource limit exceeded`。

它和 Queue capability 是“总量 vs 分组”的关系：capability 管队列总盘子，NodeGroup limits 管盘子在每个池子里的上限。两者都满足才可调度。

### 5.7.3 队列 × 节点组：亲和、反亲和与 strict

上一节的注解回答“每个节点组最多用多少”，`Queue.spec.affinity` 回答“能落到哪些节点组”。两者由同一个 `nodegroup` 插件读取：

```yaml
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata:
  name: training
spec:
  weight: 3
  capability:
    nvidia.com/gpu: "64"
  affinity:
    nodeGroupAffinity:
      requiredDuringSchedulingIgnoredDuringExecution: ["a100"]
      preferredDuringSchedulingIgnoredDuringExecution: ["h100"]
    nodeGroupAntiAffinity:
      requiredDuringSchedulingIgnoredDuringExecution: ["arm"]
      preferredDuringSchedulingIgnoredDuringExecution: ["spot"]
```

节点侧仍用标签归属：

```bash
kubectl label node gpu-a100-01 volcano.sh/nodegroup-name=a100
kubectl label node gpu-h100-01 volcano.sh/nodegroup-name=h100
```

语义：

| 规则 | 类型 | 行为 |
|---|---|---|
| `nodeGroupAffinity.required` | 硬（允许集） | 与 preferred 一起构成允许集；required 组打满分；不在任何列表中的组被 Predicate 拒绝 |
| `nodeGroupAffinity.preferred` | 软 | 与 required 一起构成允许集；默认 preferred 组加半分（`enablePreferredOrder` 可拉开顺序） |
| `nodeGroupAntiAffinity.required` | 硬 | 从允许集中移除；但若同组也在 antiPreferred，会被后者重新放行 |
| `nodeGroupAntiAffinity.preferred` | 软 | 允许但把分数置为 -1；真正禁止要用 required |

三个容易误解的点：

1. **required 不是排他列表，而是“高分允许集”**。只写 `required: [a100]`、又写 `preferred: [h100]` 时，h100 仍然可被调度，只是分数低于 a100——因为实现把两者都放进了允许集。想“只允许 a100”，就不要把 h100 写进 preferred；想“优先 a100、溢出到 h100”，写 `required: [a100]` + `preferred: [h100]` 即可。
2. **反亲和 required 优先于亲和，但会被 antiPreferred 反转**。v1.16 的判定顺序是：affinity 先把组放进允许集，antiRequired 再移除，最后 antiPreferred 又把它放回。因此一个组同时出现在 `antiAffinity.required` 和 `antiAffinity.preferred` 时，结果是“允许但 -1 分”，而不是禁止。上游测试就覆盖了这个组合。要真正禁止，别把同一个组写进 antiPreferred。
3. **反亲和 preferred 不是“软禁止”**。源码里它会把该组放进允许集并给 -1 分；如果该组原本不在任何 affinity 列表中，它反而会变成“可调度但最不优先”。

#### strict：没有 affinity 的队列怎么办

`nodegroup` 插件有一个 `strict` 参数，默认 `true`：

```yaml
- name: nodegroup
  enableHierarchy: true      # plugin option：让无 affinity 的子队列继承祖先
  arguments:
    strict: "false"          # 默认 true
    enablePreferredOrder: "true"
```

| 配置 | 队列没有 affinity 时 |
|---|---|
| `strict: true`（默认） | **没有任何节点可落**：带标签的节点返回 `node group affinity not specified`，不带标签的返回 `node group label not found` |
| `strict: false` | 只有**不带** nodegroup 标签的节点可落；带标签的节点仍因 `affinity not specified` 被拒 |

这是升级/启用插件时最容易被忽略的“全集群 Pending”：默认 `strict: true` 下，队列没写 affinity 就一个节点也调度不了，哪怕节点都打了标签。启用前先盘点节点标签，并给每个队列补 affinity，或显式设 `strict: "false"` 只放行无标签节点。

#### enablePreferredOrder：让 preferred 有顺序

默认所有 preferred 组同分。设置 `enablePreferredOrder: "true"` 后，列表顺序决定打分：越靠前分越高，适合“先填满固定池，再溢出到弹性池”：

```yaml
affinity:
  nodeGroupAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - gpu-fixed
      - gpu-serverless
```

#### 与注解配额、层级队列的交互

- **affinity vs limits**：affinity 决定“能不能去”，`volcano.sh/nodegroup-resource-limits` 决定“去了最多用多少”。一个任务可能通过了 affinity，却因为组配额已满被拒。
- **层级队列**：`nodegroup` 插件自身也有一个 **plugin option** `enableHierarchy: true`（与 capacity 的同名选项不是一回事）；开启后，没有 affinity 的子队列会继承最近祖先的 affinity。但 `nodegroup-resource-limits` 注解**不会**自动继承，每个需要分组上限的队列要自己写。
- **NamespaceQueue**：`NamespaceQueueSpec` 没有 `affinity` 字段；需要节点组隔离时，把它配在祖先集群 Queue 上（开启层级继承时子队列才会继承），并在目标版本上验证 nodegroup 插件是否覆盖 NamespaceQueue。

排障：

```bash
kubectl get queue training -o yaml | yq '.spec.affinity'
kubectl get nodes -L volcano.sh/nodegroup-name
kubectl -n volcano-system logs deploy/volcano-scheduler | grep -i nodegroup
```

如果日志出现 `node group affinity not specified` 或 `not satisfy`，先确认节点标签、队列 affinity 与 `strict` 三者是否一致。

### 5.7.4 四本账如何对齐

一个可操作的顺序：

1. **节点池**：按型号/租户/拓扑划分 NodeGroup，打标签。
2. **ResourceQuota**：按 namespace 设物理上限，防止 API 层无限创建。
3. **Queue capability**：按租户设调度上限，通常 ≤ ResourceQuota。
4. **NodeGroup limits**：按“队列 × 池”设分组上限。
5. **guarantee/weight**：最后再谈公平与保底。

对齐检查可以写成脚本：对每个 namespace，比较 `ResourceQuota.hard`、所属 Queue 的 `capability`、以及 NodeGroup limits 之和。任何一层明显小于另一层，都会制造“看起来有配额但跑不起来”的假故障。

## 5.8 容量规划方法

### 5.8.1 先算总账

```text
总 GPU = Σ 节点 allocatable
可用 GPU = 总 GPU - 系统预留 - 故障域冗余
```

### 5.8.2 再定 guarantee

guarantee 是“最坏情况下也要拿到”的资源。它应该由业务 SLA 决定，而不是由“现在用了多少”决定。

```text
Σ guarantee_i ≤ 可用资源 * 安全系数（如 0.8）
```

如果 guarantee 之和超过集群容量，队列会互相阻塞；Volcano 不会替你变出 GPU。

### 5.8.3 再定 capability

capability 是“最多能用”，可以大于 deserved。它防止一个队列在空闲时吃光整个集群，导致其他队列一来就长时间等待。

### 5.8.4 最后定 weight

weight 只影响 deserved 的相对比例。它不改变 guarantee，也不突破 capability。

一个 96 卡集群的例子：

| 队列 | weight | guarantee | capability | deserved（proportion） |
|---|---|---|---|---|
| prod | 2 | 16 | 48 | 32 |
| train | 3 | 8 | 64 | 48 |
| dev | 1 | 0 | 16 | 16 |

总权重 6，`96 × 2/6 = 32`、`96 × 3/6 = 48`、`96 × 1/6 = 16`，三者之和正好 96。这个配置允许 train 在 prod 空闲时用到 64，但 prod 需要时能回收到 48；dev 最多 16，避免实验把集群打满。

## 5.9 常见误区

1. **把 capability 当 guarantee**：capability 是上限，不保证拿得到。
2. **guarantee 之和超过容量**：队列之间会互相卡死。
3. **weight 设成 0**：该队列 deserved 为 0，只能借空闲资源，容易被回收。
4. **关闭 reclaimable 但不给 guarantee**：队列可能长期占用资源，其他队列无法收回。
5. **在 proportion 下设置 deserved**：proportion 不读这个字段，deserved 由 weight 计算。
6. **层级队列没开 capacity**：parent 字段不会生效。
7. **同时启用 proportion 和 capacity**：配置非法。
8. **忘记 default 队列**：Job 不写 queue 时会落到 default，default 不存在则提交失败。

## 5.10 MiniVolcano 的实现

MiniVolcano 的 `Queue` 保留：

- `Weight`、`Capability`、`Deserved`、`Guarantee`、`Reclaimable`、`Parent`、`Priority`、`State`；
- `proportion` 插件：按权重计算 deserved，capability 截断，guarantee 抬底；
- `capacity` 插件：递归计算父子 deserved，祖先 capability 检查；
- `reclaim` action：从超额队列回收到 deserved 线；
- 队列状态 Open/Closed。

简化点：

- proportion 的 deserved 不做“剩余资源再分配”，只做一次加权截断；
- capacity 的层级是单继承树，不支持跨树借用；
- NamespaceQueue 未模拟；
- `resourcequota` 插件只统计已准入作业，不覆盖 API Server 级准入；
- `nodegroup` 用 `Queue.spec.nodeGroup` 类型字段，而不是上游的 `spec.affinity` + `nodegroup-resource-limits` 注解；
- 队列状态用 `spec.state` 简化（上游没有该字段，开/关走 Command + `status.state`）；
- `dequeueStrategy` 等字段未实现。

这些差异在第 24 章会逐条列出。

## 5.11 一个借用与回收的完整时间线

前面的公式容易让人以为“deserved 是多少就能拿多少”。实际运行时，借用、回收、capability、guarantee 是四个不同时间尺度上的约束。下面用一个小集群把时间轴走一遍。

集群：8 张 GPU，两个队列：

| 队列 | weight | guarantee | capability | deserved（proportion） |
|---|---:|---:|---:|---:|
| A | 3 | 2 | 8 | 6 |
| B | 1 | 2 | 8 | 2 |

### 5.11.1 时间线

```text
t0  A 提交 6 卡作业，B 空闲
    A.allocated=6, B.allocated=0
    A 正好等于 deserved；B 的 2 卡份额暂时空着

t1  A 再提交 2 卡作业
    空闲 2 卡；A.capability=8 允许
    A.allocated=8（其中 2 卡是向 B 借的）
    B.allocated=0

t2  B 提交 4 卡作业
    enqueue：B.allocated=0 < deserved=2，允许 Inqueue
    allocate：无空闲，失败
    reclaim：B 欠服务，A 超额（8 > 6）
      驱逐 A 的 2 卡 → A.allocated=6
      B 获得 2 卡 → B.allocated=2
    B 仍有 2 卡 Pending

t3  A 的 6 卡作业完成，释放 6 卡
    B 的剩余 2 卡获得资源 → B.allocated=4
    但 B 的 deserved 仍是 2；多出的 2 卡是 B 向 A 借的
    A 新作业到达时，可以回收这 2 卡
```

关键结论：

1. **借用的上限是 capability，不是 deserved**。A 能用 8 卡，是因为它的 capability 允许，而且当时确实空闲。
2. **回收的目标是 deserved，不是“把借入方赶尽”**。B 要 4 卡，但只从 A 收回 2 卡，因为 A 的 deserved 是 6；B 多出的需求只能等空闲或借。
3. **guarantee 是回收的底线**。如果 A 的 guarantee 是 8，reclaim 一张也拿不走；如果 B 的 guarantee 是 4，B 已拿到的份额不会被继续回收，但 guarantee 不是“凭空变出空闲资源”的承诺——资源不够时 B 仍然要等。
4. **borrow 不是承诺**。A 在 t1 拿到的 2 卡随时可能被收回；对延迟敏感的作业不应该依赖借用容量。

### 5.11.2 如果配置不同

| 变更 | t2 的结果 | 原因 |
|---|---|---|
| A 的 `capability=6` | A 在 t1 就无法到 8，B 直接拿到 2 卡 | capability 是硬上限 |
| A 的 `reclaimable=false` | B 一张也拿不到，只能等 A 完成 | 回收被队列开关阻止 |
| A 的 `guarantee=8` | B 无法回收，A 保持 8 | guarantee 不可击穿 |
| B 的 `guarantee=4` | 回收仍只到 A 的 deserved，B 拿 2；但 B 的保底会阻止别人再回收它 | guarantee 保护的是“已拿到的最低量” |
| B 的 `weight=3` | deserved 变为 A=4、B=4，t2 可回收 4 卡 | 权重改变稳态份额 |

### 5.11.3 回收为什么不能“精确停在 deserved”

资源是整 Pod 分配的。A 的 8 卡可能来自一个 8 卡 Pod，也可能是四个 2 卡 Pod。如果只有一个 8 卡 Pod，回收它会让 A 从 8 直接掉到 0，击穿 guarantee=2。因此实现必须：

```text
每次驱逐前检查：
    A.allocated - victim.requests >= A.guarantee
不满足就换更小的受害者；没有合适的就放弃本次回收
```

这解释了为什么“B 欠 2 卡”有时一张也回收不出来：不是调度器不想，而是没有“拆得动”的受害者。第 9.3.2 节和第 26.5.2 节会从实现和形式化两个角度继续展开。

### 5.11.4 观察借用与回收

```bash
# 当前分配与应得
kubectl get queue -o custom-columns='NAME:.metadata.name,ALLOC:.status.allocated,DESERVED:.status.deserved,CAP:.spec.capability'
# 回收事件
kubectl get events --field-selector reason=Reclaimed
# 调度器日志
kubectl -n volcano-system logs deploy/volcano-scheduler | grep -i -E 'reclaim|overused'
```

如果 B 长期低于 deserved 且没有 Reclaimed 事件，按顺序检查：B 是否有 Pending 作业、A 是否真的超额、A 是否 `reclaimable`、受害者是否被 PDB/gang 保护、受害者是否会把 A 打到 guarantee 以下。

## 5.12 本章小结

- Queue 的五个核心字段构成“保底 + 上限 + 权重 + 可回收”的治理模型。
- proportion 用于扁平加权公平，capacity 用于层级配额；两者语义冲突，不要同时启用。
- NamespaceQueue 把队列树下沉到 namespace，但总配额仍由集群 Queue 授权；默认关闭且是 Alpha。
- ResourceQuota、Queue、NodeGroup limits、节点容量是四本不同的账，必须显式对齐。
- `spec.affinity` 决定“能落在哪些节点组”，`nodegroup-resource-limits` 决定“每组最多用多少”；`strict` 默认 true，且队列没有 affinity 时一个节点也落不了。
- guarantee 是回收的底线，capability 是分配的上限。
- Queue 状态影响准入，不影响已运行作业。
- 容量规划先算 guarantee，再算 capability，最后算 weight。

下一章进入 Volcano 最核心的调度语义：gang。

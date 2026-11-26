# 第 11 章 拓扑感知

## 11.1 两种“拓扑”

Volcano 的拓扑能力分两层，容易混淆：

| 能力 | 对象 | 解决的问题 | 插件 |
|---|---|---|---|
| 任务角色拓扑 | 同一作业内的 task | ps 与 worker 是否同节点/不同节点 | `task-topology` |
| 网络拓扑感知 | 节点所在的 HyperNode 树 | 整个 gang 是否落在同一交换机/机架/超节点 | `network-topology-aware` |
| PodGroup 拓扑反亲和 | 不同 PodGroup | 多组推理副本是否分散到不同超节点 | `group-topology-affinity`（v1.16） |

前两者是“任务之间/节点之间的距离”，第三者把约束提升到“作业之间”。三者可以叠加。

## 11.2 task-topology：角色亲和与反亲和

场景：

- 参数服务器（ps）和 worker 需要同节点，减少 RPC 延迟；
- 推理副本需要打散到不同节点，避免单节点故障；
- 数据预处理和训练需要分开，避免争抢本地盘。

配置（Job 级）：

```yaml
spec:
  taskTopology:
    - taskNames: ["ps", "worker"]
      policy: affinity
```

或反亲和：

```yaml
spec:
  taskTopology:
    - taskNames: ["worker"]
      policy: anti-affinity
```

插件在 Predicate 阶段检查：

- **affinity**：如果同组任务已有 Pod 绑定在别处，当前任务必须去同一个节点/域；
- **anti-affinity**：当前节点上不能有同组任务。

它还会给同组节点加 NodeOrder 分，让亲和更容易被选中。

## 11.3 HyperNode：网络拓扑树

### 11.3.1 模型

HyperNode 是一棵树：

```text
root (tier 3, cluster)
├── rack-a (tier 2)
│   ├── leaf-a1 (tier 1, ToR) ── node-a1
│   └── leaf-a2 (tier 1, ToR) ── node-a2
└── rack-b (tier 2)
    ├── leaf-b1 (tier 1) ── node-b1
    └── leaf-b2 (tier 1) ── node-b2
```

- **tier 越小，域越近**（延迟越低、带宽越大）。
- 叶子节点用 `type: Node` 精确匹配节点名。
- 父节点用 `type: HyperNode` 引用子树。
- 也可以用 `regexMatch`、`labelMatch` 批量匹配。

```yaml
apiVersion: topology.volcano.sh/v1alpha1
kind: HyperNode
metadata:
  name: leaf-a1
spec:
  tier: 1
  tierName: tor
  members:
    - type: Node
      selector:
        exactMatch:
          name: node-a1
---
apiVersion: topology.volcano.sh/v1alpha1
kind: HyperNode
metadata:
  name: rack-a
spec:
  tier: 2
  tierName: rack
  members:
    - type: HyperNode
      selector:
        exactMatch:
          name: leaf-a1
    - type: HyperNode
      selector:
        exactMatch:
          name: leaf-a2
```

完整示例见 `examples/queues/hypernode.yaml`。

### 11.3.2 作业约束

```yaml
spec:
  networkTopology:
    mode: hard
    highestTierAllowed: 2
```

语义：

- **hard**：所有任务必须落在**同一个** tier ≤ `highestTierAllowed` 的 HyperNode 内；否则作业保持 Pending。
- **soft**：尽量落在同一域，允许跨域；通过 NodeOrder 加分引导。
- `highestTierAllowed=2` 表示允许到第 2 层域（例如机架），`1` 表示更严格的 ToR/叶子域。

注意：`highestTierAllowed` 只对 hard 模式有意义。部分版本/CRD 也支持 `highestTierName`（按 HyperNode 的 `tierName` 指定，而不是数字），两者不要同时写；示例见 `examples/jobs/podgroup-anti-affinity.yaml`。

### 11.3.3 自动发现

Volcano 支持多种发现方式：

- **节点标签**（v1.13+）：用标签自动构建 HyperNode，不依赖 IB 硬件；
- **UFM/RoCE**：从网络控制器读取拓扑；
- 手动 CR。

标签式发现降低了门槛：没有 InfiniBand 的集群也能做“机架/交换机”级别的拓扑。

## 11.4 PodGroup 拓扑反亲和（v1.16）

### 11.4.1 与 task-topology 的区别

- `task-topology` 管**同一个作业内部**的角色（ps 和 worker 是否同节点）；
- `group-topology-affinity` 管**不同 PodGroup 之间**的放置（两组推理副本是否必须在不同超节点）；
- `network-topology-aware` 管**单个作业**能否装进一个域。

三者解决不同粒度的问题，可以叠加。

### 11.4.2 规则模型

`group-topology-affinity` 不是默认启用的插件，需要显式加入 tiers。规则写在 PodGroup 上：

```yaml
apiVersion: scheduling.volcano.sh/v1beta1
kind: PodGroup
metadata:
  name: inference-a
  labels:
    app: inference
    serving-group: online
spec:
  minMember: 2
  queue: default
  topologyAffinity:
    podGroupAntiAffinity:
      # required：省略 weight，所有匹配组必须落在不同 domain
      required:
        - podGroupSelector:
            labelSelector:
              matchLabels:
                serving-group: online
          topologyTierName: supernode
      # preferred：设置 weight（1–100），尽量不同 domain
      preferred:
        - podGroupSelector:
            labelSelector:
              matchLabels:
                app: inference
          topologyTierName: rack
          weight: 50
```

语义：

- **required term**：所有匹配的 PodGroup **必须**落在不同域；任一 required 不满足，作业不调度。required 必须省略 `weight`。
- **preferred term**：尽量不同域，域不够时可以共享；`weight`（1–100）影响打分，越大越强，但不保证。
- 每个 term 必须且只能指定 `topologyTierName` 或 `topologyTier` 之一。
- `podGroupSelector` 匹配的是 **PodGroup 的 labels**，不是 Pod labels；默认作用域是当前 namespace，可用 namespace selector 扩大。Mini 实现了“同 namespace”默认值，没有实现 namespace selector。
- 只在 `allocate` 阶段生效；backfill 的 BestEffort Pod 不受约束，抢占/回收也不会重新计算反亲和。Mini 用 `Session.Action` 实现这一点：非 `allocate` 的 action 直接跳过插件。
- 插件参数 `arguments.weight`（默认 1）控制 preferred 打分在总分中的权重；required 是硬约束，不受该参数影响。
- 规则匹配的是 PodGroup labels；成员的 Pod 仍需要 `schedulerName: volcano` 与 `scheduling.k8s.io/group-name` 注解（如果不由 Volcano Job 自动创建）。

### 11.4.3 为什么推理场景需要它

大模型推理通常部署多组副本：

- 每组是一个 PodGroup（多机多卡 serving）；
- 组内要 gang 启动；
- 组间要跨超节点，避免单机架故障同时打掉所有副本；
- 但超节点数量有限，不能无限强制。

`required` 用于“必须跨故障域”的副本，`preferred` 用于“尽量跨机架”的优化。它把“故障域打散”从 Deployment 的 Pod 反亲和提升到了**作业/PodGroup 粒度**，避免一个 8 卡 serving 组被拆到不同超节点导致集合通信跨域。

### 11.4.4 失败模式

- required 规则太多，集群没有足够的域，所有匹配 PodGroup 一起 Pending；
- selector 匹配到了自己或不该匹配的组，导致自相矛盾；
- PodGroup 没有打标签，selector 匹配为空，规则静默失效；没有匹配对端时，required 也是 no-op；
- 节点不在 HyperNode 树里，且存在已落地的匹配对端：required 会以“无法判定域”拒绝；先补全拓扑再调度；
- 插件未加入 tiers，但 YAML 已经写了规则——不会报错，只是不生效；
- 与 `network-topology-aware` 的 hard 约束叠加，可行域交集为空。

排障时先看 PodGroup labels 与 selector，再看 HyperNode 是否覆盖了足够多的域。完整示例见 `examples/jobs/podgroup-anti-affinity.yaml`。

## 11.5 调度算法

hard 模式的核心是“选域 + 限域”：

```text
1. 对作业的每个候选域 D（tier <= highestTierAllowed）：
       capacity(D) = Σ free(node) for node in D
2. 选一个能容纳 minResources 的最小域 D*
3. 所有任务只能在 D* 的节点上调度
4. 如果找不到 D*：作业 Pending，记录原因
```

实际的 Volcano 实现更复杂：

- 维护 `AllocatedHyperNode`，记录作业已经占用的域；
- 用 LCA（最近公共祖先）计算当前作业的域；
- 支持子组（SubGroup）和 partition（v1.14+）；
- 支持 HyperNode 级 binpack；
- v1.15 的 gangPreempt 与拓扑集成；
- v1.16 的 `group-topology-affinity` 支持 PodGroup 级反亲和。

### 11.5.1 为什么不能只做“节点打分”

如果只给同域节点加分，一个 64 卡作业可能：

- 前 32 卡在 rack-a；
- 后 32 卡因为 rack-a 满了，落到 rack-b；
- 结果跨了两个机架，网络成为瓶颈。

hard 模式必须在**选节点之前**先选域，否则无法保证整个 gang 在一个域内。这是它与普通 node affinity 的本质区别。

## 11.6 子组与 partition（v1.14+）

大模型训练经常有“多个子组”：

- 每个 pipeline stage 是一个子组；
- 子组内部要紧凑，子组之间可以分开；
- 每个子组有独立的拓扑约束。

v1.14 引入两类互补的建模方式：Task 上的 `partitionPolicy`（用户入口）与 PodGroup 上的 `subGroupPolicy`（调度器入口）。**Volcano Job 本身没有 `subGroupPolicy` 字段**；控制器把每个带 `partitionPolicy` 的 task 转成一条 PodGroup `subGroupPolicy`。手动创建 PodGroup 时，才直接写 `subGroupPolicy`。

### 11.6.1 subGroupPolicy：PodGroup 内的二级分组

`PodGroup.spec.subGroupPolicy[]` 的字段：

| 字段 | 作用 |
|---|---|
| `name` | 子组策略名；Job 路径下等于 task 名 |
| `subGroupSize` | 每个子组包含多少 Pod；默认 1 |
| `minSubGroups` | 至少有多少个子组满足条件才触发调度；默认 0（0 = 不做子组 gang 门控） |
| `labelSelector` | 选择参与该策略的 Pod |
| `matchLabelKeys` | 在 selector 基础上，再按这些标签的值分组；Job 路径固定为 `volcano.sh/partition-id` |
| `networkTopology` | 子组内部的拓扑约束（mode + highestTierAllowed/Name） |

它同时表达两件事：

1. **子组级 gang**：只有 `minSubGroups > 0` 时，`minSubGroups × subGroupSize` 个 Pod 能凑齐才调度；
2. **子组级拓扑**：每个子组内部要落在同一个 HyperNode 域，子组之间可以跨域。

手动创建 PodGroup 时，一个 1024 卡作业可以写成：

```yaml
apiVersion: scheduling.volcano.sh/v1beta1
kind: PodGroup
metadata:
  name: train-pg
spec:
  minMember: 1024
  subGroupPolicy:
    - name: pipeline-stage
      subGroupSize: 128
      minSubGroups: 8
      matchLabelKeys: ["training.example.com/stage"]
      networkTopology:
        mode: hard
        highestTierAllowed: 2
```

语义是“8 个 stage 子组、每组 128 卡、组内落在 tier 2 以内的同一域”。注意 `minSubGroups` 是**调度门槛**，不是副本数；实际 Pod 数仍由 Job 的 `replicas`/`minMember` 决定。`minSubGroups: 0` 时子组 gang 门控被跳过，只剩拓扑分组——不要用它来“覆盖 minTaskMember”。

### 11.6.2 partitionPolicy：Task 内的等分片

`tasks[].partitionPolicy` 把一个 task 的副本等分成若干片：

| 字段 | 约束 |
|---|---|
| `totalPartitions` | 分片数，> 0；`totalPartitions × partitionSize` 必须等于 `replicas` |
| `partitionSize` | 每片 Pod 数，> 0 |
| `minPartitions` | 至少多少片满足条件才调度；默认 0 |
| `networkTopology` | 分片内部的拓扑约束 |

它与 `minAvailable` 有联动：

- 若 `minPartitions > 0` 且 task 的 `minAvailable` 已设置，webhook 要求 `minAvailable == minPartitions × partitionSize`；
- 若 task 没设 `minAvailable` 且 `minPartitions > 0`，mutating webhook 会用 `minPartitions × partitionSize` 推导，而不是用 `replicas`；`minPartitions = 0` 时仍回退到 replicas，避免静默把 gang 关掉。

例子：32 个 worker、每 8 个一片、至少 2 片：

```yaml
- name: worker
  replicas: 32
  partitionPolicy:
    totalPartitions: 4
    partitionSize: 8
    minPartitions: 2
    networkTopology:
      mode: soft
      highestTierAllowed: 3
```

它允许“先起 2 片（16 个 Pod）”，而不是等 4 片全齐；每片内部仍受拓扑约束。

Job 路径下，控制器会生成对应的 PodGroup 条目：

```text
SubGroupPolicy.Name         = task.Name
SubGroupPolicy.SubGroupSize = PartitionSize
SubGroupPolicy.MinSubGroups = MinPartitions
SubGroupPolicy.LabelSelector = {volcano.sh/task-spec: task.Name}
SubGroupPolicy.MatchLabelKeys = [volcano.sh/partition-id]
```

所以 `partitionPolicy` 与 `subGroupPolicy` **不是二选一**：前者是用户写的 Job 字段，后者是控制器写出的 PodGroup 字段。只有手动创建 PodGroup 时，才需要直接写 `subGroupPolicy`。

### 11.6.3 三者怎么选

| 需求 | 用法 |
|---|---|
| 每个角色一个最低数（ps≥1、worker≥4） | task `minAvailable` → `minTaskMember`（见 6.2.2） |
| 组内紧凑、组间可分开、组级 gang | Job 写 `partitionPolicy`；手动 PodGroup 写 `subGroupPolicy` |
| 把一个 task 等分成多片、按片数设门槛 | `partitionPolicy`（`minPartitions > 0`） |
| 整个作业在一个域 | 作业级 `networkTopology`（见 11.3） |

PodGroup API 的注释建议：`SubGroupPolicy` 覆盖 `minTaskMember` 的能力，是长期演进方向，不建议两者同时使用。但只有 `minSubGroups > 0` 时它才承担 gang 门控；`minSubGroups: 0` 只是拓扑分组。Job 路径只写 `partitionPolicy`，`subGroupPolicy` 由控制器生成；只有手动创建的 PodGroup 才直接写 `subGroupPolicy`。

一个常见错误：把 `subGroupSize` 当成 `replicas`，结果 `minSubGroups × subGroupSize` 与 Job 的总副本数对不上，调度器一直等一个不存在的子组。提交前用 `kubectl get podgroup -o yaml` 核对 `minMember`、`minTaskMember` 与 `subGroupPolicy` 三者是否自洽。

## 11.7 拓扑与抢占

hard 拓扑作业被驱逐后，重新调度必须重新找域。可能出现：

- 原来的域已经被别人占了；
- 新域容量不够；
- 反复抢占但始终凑不齐。

v1.15 的 gangPreempt 在驱逐前做放置模拟，能减少这种“白抢”。即便如此，拓扑作业的抢占仍然比普通作业更脆弱，生产上要慎用高优先级 + hard 拓扑 + 频繁抢占的组合。

## 11.8 诊断拓扑 Pending

```bash
kubectl describe vcjob <name>
kubectl get podgroup <name> -o yaml
kubectl get hypernode
kubectl get nodes --show-labels | grep -i topology
kubectl -n volcano-system logs deploy/volcano-scheduler | grep -i topology
```

常见原因：

- HyperNode 没有覆盖目标节点；
- `highestTierAllowed` 设得太小，没有域能容纳 gang；
- 域内空闲总量够，但形状不匹配（碎片）；
- hard 模式与 soft 模式配置反了；
- 拓扑插件未启用，或 tier 顺序导致 predicate 没生效。

## 11.9 MiniVolcano 的实现

MiniVolcano 提供两个插件：

- `task-topology`：作业内角色亲和/反亲和；
- `network-topology-aware`：HyperNode 树 + hard/soft。

hard 模式的实现：

1. Session 打开时，为每个作业选域：
   - 找 tier ≤ highestTierAllowed 的最宽域；
   - 计算每个域的空闲容量；
   - 选能容纳 `minResources` 的最小域；
   - 找不到则记录原因，作业不可调度。
2. Predicate 只允许域内节点；
3. 分配仍在 Statement 中进行，gang 失败会回滚。

soft 模式：

- 不限制节点；
- NodeOrder 给“同域已有同作业 Pod”的节点加分。

场景：

- `topology`：4 个节点、2 个机架，4 卡 gang 被限制在 rack-a；
- `group-topology`：serve-a 声明 required 反亲和，serve-b 只带匹配标签；两者仍落在不同 rack（验证单边规则的对称约束）；
- `task-topology`：ps + worker 亲和，全部落在同一节点。

简化：

- 没有 SubGroup/partition；
- 没有自动发现；
- 没有 HyperNode binpack；
- 没有 LCA 的复杂逻辑，只按 tier 选域；
- 没有拓扑抢占模拟；
- `group-topology-affinity` 只实现 PodGroup 级反亲和：用数字 tier、按“声明方 + 被选中方”成对评估、默认同 namespace；没有 `topologyTierName`、namespace selector 与完整的 CRD 校验。
- 省略或写 0 的 tier 在 Mini 中按 tier 1 处理；从 `topologyTierName` 迁移时必须显式写数字 tier。

## 11.10 HyperNode 自动发现：标签规范与运维流程

手动维护 HyperNode CR 在几十个节点时可行，上百个节点就会漂移。v1.13 起 Volcano 支持基于节点标签的自动发现，让拓扑树跟随节点标签生成。

### 11.10.1 三种发现方式

| 方式 | 数据来源 | 适用 | 代价 |
|---|---|---|---|
| 手动 CR | 人工编写 HyperNode | 小集群、拓扑稳定 | 易漂移，节点变更要同步 |
| 标签自动发现 | 节点 labels | 云上/无 IB 集群、机架感知 | 依赖标签规范与控制器 |
| UFM/RoCE 发现 | 网络控制器/交换机 | InfiniBand/RoCE 高性能集群 | 需要网络侧集成 |

标签方式降低了门槛：没有 InfiniBand 也能表达“机架/交换机/可用区”。具体标签键随版本变化，使用前先用 `kubectl get hypernode -o yaml` 观察控制器生成的树，再对照官方文档确认键名。

### 11.10.2 一个可操作的标签规范

无论具体键名如何，建议平台内部固定三层语义：

```text
tier 1：叶子/ToR/单机柜内交换机
tier 2：机架/机柜组
tier 3：spine/集群/可用区
```

节点标签示例（键名以你的 Volcano 版本为准）：

```yaml
metadata:
  labels:
    topology.example.com/tier1: leaf-a1
    topology.example.com/tier2: rack-a
    topology.example.com/tier3: spine-1
```

规范一旦确定，就纳入 GitOps/节点池模板，不要让人手工打标签。新节点加入时由节点池自动带上标签，避免“新机器不在拓扑树里”。

### 11.10.3 运维流程

```text
1. 定义 tier 语义与命名规范
2. 在节点池模板/机器配置里写入标签
3. 部署或启用 HyperNode controller（v1.16 可独立部署）
4. kubectl get hypernode 验证树形结构与成员
5. 用 soft 模式灰度，观察 NodeOrder 是否把作业聚拢
6. 确认域容量足够后，再对特定队列启用 hard 模式
```

验证命令：

```bash
kubectl get hypernode
kubectl get hypernode <name> -o yaml
kubectl get nodes --show-labels | grep topology
kubectl -n volcano-system logs deploy/volcano-controllers | grep -i hypernode
```

### 11.10.4 五个常见故障

1. **新节点没有标签**：不在任何 HyperNode 里；hard 作业不会使用它，表现为“节点空闲但作业 Pending”。
2. **标签层级冲突**：同一节点同时属于两个 tier 1 叶子；控制器可能拒绝或产生不确定的树，先在准入侧校验。
3. **tier 数字反了**：把机架标成 tier 1、叶子标成 tier 2；`highestTierAllowed` 会选到错误的域。
4. **节点替换后标签丢失**：自动扩缩容的节点模板没带标签；每次扩容都要验证树。
5. **HyperNode controller 未运行**：CR 存在但 status/成员不更新；hard 调度看到的是旧树。

### 11.10.5 设计建议

- **不要过度分层**。三层通常够用；层数越多，域容量越小，hard 作业越难凑齐。
- **给每个 tier 留冗余**。如果 tier 1 只能放 8 卡，就不要把 16 卡 gang 的 `highestTierAllowed` 设为 1。
- **soft 先行**。先用 soft 观察布局，再切 hard；hard 一旦配置错误会让整批作业 Pending。
- **把拓扑纳入容量规划**。总空闲 GPU 不等于某个域的空闲 GPU；按域分别统计。
- **与 NodeGroup 区分**。NodeGroup 是资源分组/上限，HyperNode 是网络距离；两者可以叠加，但不要互相替代。

## 11.11 本章小结

- task-topology 管作业内角色，network-topology-aware 管单作业的域，group-topology-affinity 管作业之间的反亲和。
- HyperNode 用 tier 表达网络距离，hard 模式先选域再选节点。
- 子组/partition 让超大作业可以分层约束：`subGroupPolicy` 管组级 gang 与拓扑，`partitionPolicy` 管 task 内等分片。
- v1.16 的 PodGroup 反亲和把“故障域打散”提升到作业粒度，适合多组推理副本。
- 拓扑作业对碎片和抢占更敏感。
- MiniVolcano 用“选域 + 限域 + soft 加分”复刻了核心语义，并用简化模型实现了 PodGroup 反亲和（数字 tier、默认同 namespace、按对评估）。

下一章讲预留、SLA、TDM 与混部——这些机制决定了“资源在时间维度上怎么分配”。

# 第 38 章 · 原生 WAS 与 gang scheduling：Kueue 的双轨期

> 版本提示：本章讨论 Kubernetes 1.36/1.37 引入的原生 Workload-Aware Scheduling（WAS）与 Kueue 的衔接。截至 2026 年 11 月，Kubernetes 1.37 已把 Workload/PodGroup 推进到 Beta（`scheduling.k8s.io/v1beta1`，`GenericWorkload` gate 默认关闭），Kueue 稳定线是 v0.20.x。Kueue 侧的 WAS 集成仍处于 RFC/分阶段落地，**不要把设计提案当成已发布 API**；具体字段、gate 名称和默认值以你部署的 Kubernetes/Kueue release notes 为准。

## 38.1 为什么会有"两个 gang"

Kueue 一直强调自己不做节点放置：它只决定"这个 Workload 能不能占用逻辑配额"，然后放行 Pod，由 `kube-scheduler` 决定 Pod 落到哪。这个分工在批处理作业上工作得很好，但在 gang scheduling 上有一个结构性缺口：

- Kueue 的 Workload 是**作业级**的：一个 Workload 可以包含多个 PodSet，Kueue 一次性为整个 Workload 预留配额；
- `kube-scheduler` 是**Pod 级**的：它默认逐个 Pod 调度，不保证"32 个 Pod 要么全起、要么全等"；
- 于是"作业级准入"和"Pod 级放置"之间需要一层 gang 协调。Kueue 过去用 `WaitForPodsReady`、Job 控制器的 suspend、以及和 Volcano 等 gang 调度器的组合来补，但这些都不是 Kubernetes 原生语义。

原生 WAS 要解决的正是这一层：让 API server 和 kube-scheduler 认识"一组 Pod 属于同一个 Workload/PodGroup"，并在调度器内部实现 gang（Permit 阶段等待）和作业级抢占。它不取代 Kueue 的配额准入，而是把"放置阶段的 gang"变成一等公民。

于是 2026 年的平台会出现两条并行的轨道：

```text
轨道 A（Kueue 传统）：
  Job -> Kueue Workload -> 配额准入 -> 创建 Pod -> kube-scheduler 逐 Pod 放置
                                      └─ Kueue WaitForPodsReady 兜底

轨道 B（原生 WAS）：
  Job -> Kueue Workload -> 配额准入 -> 创建 Pod + PodGroup
                                      └─ kube-scheduler 以 PodGroup 为单位 gang 放置
```

两条轨道可以组合，也可以互相冲突。本章讲清楚它们各自负责什么、双轨期怎么配置、怎么迁移、怎么排障。

## 38.2 原生 WAS 的时间线

| 版本 | 能力 | 状态 |
|---|---|---|
| Kubernetes 1.36 | 树内引入 Workload/PodGroup API | Alpha |
| Kubernetes 1.37（2026-08） | Workload/PodGroup 进入 Beta，API 为 `scheduling.k8s.io/v1beta1`；`GangScheduling` 与 `WorkloadAwarePreemption` 合并为 `GenericWorkload` gate | Beta，默认关闭 |
| Kubernetes 1.37 | `CompositePodGroup`（gang of gangs） | Alpha，`scheduling.k8s.io/v1alpha3` |
| Kubernetes 1.37 | `DRAWorkloadResourceClaims`：PodGroup 共享 ResourceClaim 生命周期/预留 | Beta |
| Kueue v0.20.x | 既有标签式 pod group；WAS 集成 RFC（#12385）讨论三阶段迁移 | 设计/分阶段 |

需要记住三件事：

1. **Beta 不等于默认开启**。`GenericWorkload` 是 Beta 但默认关闭；不开 gate，API 可能存在但调度器不会按 PodGroup 做 gang。
2. **API 版本会变**。1.36 到 1.37 之间 `scheduling.k8s.io` 的版本演进不是无损的；升级前要读 conversion/release notes。
3. **Kueue 的 WAS 集成不是"打开一个开关"**。它涉及"谁创建 PodGroup、PodGroup 如何驱动 PodSets、旧标签式 pod group 如何退场"三个问题，RFC 给的是分阶段方案。

## 38.3 Kueue 现在的 pod group：标签式

在原生 WAS 之前，Kueue 的 Pod 集成用标签把多个 Pod 归成一个 Workload。典型形态是给同一组 Pod 打上：

```yaml
metadata:
  labels:
    kueue.x-k8s.io/pod-group-name: "distributed-training"
    kueue.x-k8s.io/pod-group-total-count: "32"
```

Kueue 的 Pod 集成控制器看到这些标签后，会：

1. 把同一 `pod-group-name` 的 Pod 合并成一个 Workload；
2. 按角色/模板拆成多个 PodSet（如果 Pod 模板不同）；
3. 对整个 Workload 做配额准入；
4. 准入后放行所有 Pod。

这条路径的优点是：

- 不需要 Kubernetes 侧的新 API；
- 对裸 Pod、自定义控制器都可用；
- 与 Kueue 的 Workload/PodSet 模型直接对应。

缺点是：

- **标签是弱契约**：用户可以手写错、可以只给部分 Pod 打标签、可以在运行中改标签；
- **没有 API 级校验**：total-count 和实际 Pod 数不一致时，Kueue 只能尽量收敛；
- **没有调度器级 gang**：Pod 被放行后，kube-scheduler 仍然逐个调度；Kueue 只能靠 `WaitForPodsReady` 在超时后驱逐，属于"事后补救"；
- **与 Job 控制器的 suspend 语义重叠**：谁来保证"全部 Pod 一起创建"取决于集成层。

原生 WAS 的 PodGroup 把这些弱契约变成 API 对象：有 schema、有 status、有调度器参与。

## 38.4 双轨期的对象关系

双轨期最容易被忽略的是**名字冲突**：Kueue 有一个 `kueue.x-k8s.io/v1beta2` 的 `Workload`，原生 WAS 有一个 `scheduling.k8s.io/v1beta1` 的 `Workload`/`PodGroup`。它们不是同一个对象，也不是一对一：

```text
Kueue Workload（配额单位）
  ├── PodSet: launcher   (count=1)
  ├── PodSet: worker     (count=31)
  └── status.admission   (配额见证)

scheduling PodGroup（放置单位）
  ├── pod group name = 训练作业名
  ├── minCount = 32
  └── status.conditions  (调度器 gang 状态)
```

一个 Kueue Workload 可能对应一个 PodGroup；一个 PodGroup 也可能只覆盖某个 PodSet。关键是：**配额账本的单位**和**gang 放置的单位**不必相同，但必须显式对齐，否则会出现：

- Kueue 按 32 个 Pod 计费，PodGroup 只认 16 个 → 配额多算/少算；
- PodGroup 等 32 个 Pod，Kueue 只放行 16 个 → gang 永远不满足；
- 两个 PodGroup 共享一个 Kueue Workload → 一个 gang 失败会拖垮另一个。

### 38.4.1 原生 Workload 与 PodGroup 不是一回事

双轨期还有一个同名陷阱：`scheduling.k8s.io` 侧的 **Workload** 和 **PodGroup** 也是两个不同对象。粗略分工是：

| 对象 | 作用 | 谁创建 |
|---|---|---|
| Kueue `Workload`（`kueue.x-k8s.io/v1beta2`） | 配额/准入单位，包含 PodSet 与 admission 状态 | Kueue 的 Job/Pod 集成 |
| 原生 `Workload`（`scheduling.k8s.io/v1beta1`） | gang 的**策略模板**：`podGroupTemplates`（通常最多 8 个）、控制器归属等 | 作业控制器（当前 in-tree 主要是 Job 控制器） |
| 原生 `PodGroup`（`scheduling.k8s.io/v1beta1`） | gang 的**运行时实例**：`minCount`、成员/调度状态 | Workload 控制器或作业控制器 |

所以不要写出"Kueue Workload 就是 PodGroup"或"一个 Workload 必然一个 PodGroup"这种结论。三者的计数单位（Kueue 的 PodSet count、原生 Workload 的模板、PodGroup 的 `minCount`）必须显式对齐；`minMember` 是 Volcano/YuniKorn 的叫法，不是原生 WAS 字段。

### 38.4.2 三种对齐方式

| 方式 | 谁创建 PodGroup | 适用 | 风险 |
|---|---|---|---|
| Kueue 创建 | Kueue 的 Job/Pod 集成在准入后创建 PodGroup | 平台统一管理 | 需要 Kueue 支持；创建失败要回滚配额 |
| 作业框架创建 | JobSet/LWS/Ray 等框架创建 PodGroup | 框架已理解 gang | 框架与 Kueue 的标签/所有权要对齐 |
| 用户创建 | 用户手写 PodGroup | 迁移/实验 | 最容易出现 total-count 不一致和孤儿对象 |

RFC #12385 讨论的方向偏向第一种：Kueue 作为配额和作业语义的拥有者，负责把 PodGroup 创建出来，再用 PodGroup 驱动 PodSets；长期逐步弃用标签式 pod group。具体落地阶段和 gate 名称要以 Kueue release notes 为准。

## 38.5 一次端到端准入（双轨期）

假设一个 32 Pod 的训练作业，Kueue 负责配额，原生 WAS 负责 gang 放置：

```text
t0  Job 控制器创建 suspended Job（batch Job 用 suspend=true）
t1  Kueue Job 集成创建 Workload（launcher 1 + worker 31）
t2  Kueue 调度：QuotaReserved=True
t3  Kueue 创建 PodGroup（如果采用 Kueue 创建模式）
t4  Kueue 放行 Job；Job 控制器创建 32 个 Pod，带 PodGroup 引用
t5  kube-scheduler 逐个调度 Pod；每个 Pod 在 Permit 阶段等待
t6  32 个 Pod 都到达 Permit 且资源可行 -> 一起放行 -> Running
t7  运行时：Kueue 观察 PodsReady；原生 WAS 观察 PodGroup 状态
```

关键时间点：

- **t3/t4 的顺序**：PodGroup 必须先于 Pod 存在，否则 Pod 的 group 引用无效；
- **t5 的 Permit 超时**：gang 调度器通常有等待上限；超时后是回滚已调度 Pod，还是让它们继续等待，取决于实现和配置；
- **t6 的原子性**：原生 gang 只保证"调度决策一起成功"，不保证"容器一起启动"；镜像拉取、GPU 初始化仍可能让部分 Pod 晚起；
- **t7 的双重观测**：Kueue 的 `PodsReady` 和 PodGroup 的 condition 可能不一致，排障时要先确定哪一层在报错。

### 38.5.1 与 Kueue `WaitForPodsReady` 的关系

`WaitForPodsReady` 是 Kueue 的"事后兜底"：准入后若 Pod 在超时内没有全部就绪，驱逐 Workload、释放配额。原生 WAS 是"事前协调"：在调度阶段就不让部分 Pod 先跑。

两者同时开启时要注意：

- 原生 gang 已经保证"调度层面一起过"，但容器启动仍可能超时；`WaitForPodsReady` 仍然有价值；
- 如果原生 gang 的 Permit 等待时间接近 `WaitForPodsReady` 的超时，可能出现"调度器还在等，Kueue 已经把 Workload 驱逐"的竞态；
- 建议 `WaitForPodsReady.timeout` 明显大于 gang Permit 超时 + 镜像拉取预算，并给两类事件建立关联 ID（Workload UID / PodGroup UID）。

## 38.6 配置与版本矩阵

双轨期最重要的不是"开哪个 gate"，而是**四层版本要对齐**：

| 层 | 需要确认 | 检查方式 |
|---|---|---|
| Kubernetes API | 是否有 `scheduling.k8s.io/v1beta1`；`GenericWorkload` 是否开启 | `kubectl api-resources \| grep podgroups`；看 kube-apiserver/kube-scheduler 的 `--feature-gates` |
| kube-scheduler | 是否启用 gang/Permit 逻辑 | 调度器配置与日志；`GenericWorkload` gate |
| Kueue | 是否支持 WAS 集成；用哪个 gate/版本 | Kueue release notes、controller `--feature-gates` |
| 作业框架 | 是否创建/识别 PodGroup | JobSet/LWS/Ray 版本与配置 |

一个实用的检查脚本骨架：

```bash
# 1. API 是否存在
kubectl api-resources | grep -E 'podgroups|workloads.scheduling'

# 2. API 版本
kubectl get --raw /apis/scheduling.k8s.io | jq -r '.versions[].version'

# 3. Kueue 控制器 gate
kubectl -n kueue-system get deploy kueue-controller-manager \
  -o jsonpath='{.spec.template.spec.containers[0].args}' | tr ' ' '\n' | grep feature-gates

# 4. 现有 PodGroup
kubectl get podgroups -A
```

### 38.6.1 版本 skew 的四种组合

| K8s | Kueue | 结果 | 建议 |
|---|---|---|---|
| 无 WAS API | 任意 | 只能走标签式 pod group | 维持现状 |
| 有 API，gate 关 | 支持 WAS | PodGroup 可创建，但调度器不 gang | 不要在生产开 Kueue WAS 创建 |
| 有 API，gate 开 | 不支持 WAS | Kueue 仍走标签；PodGroup 由框架/用户创建 | 明确所有权，避免双写 |
| 有 API，gate 开 | 支持 WAS | 可以双轨/切换 | 按 38.7 迁移 |

## 38.7 迁移手册

迁移的目标不是"删掉标签"，而是"让配额单位和放置单位对齐，并且可回滚"。

### 阶段 0：盘点

- 哪些作业用了 `kueue.x-k8s.io/pod-group-*` 标签？
- 哪些框架已经创建 PodGroup？
- 集群里有没有同名/同 UID 的 Kueue Workload 与 scheduling PodGroup？
- 当前 `WaitForPodsReady`、gang 调度器、Job 控制器的超时分别是多少？

### 阶段 1：staging 双写

在一个非生产队列上：

1. 开启 Kubernetes 侧 `GenericWorkload`；
2. 开启 Kueue 侧 WAS 集成（如果目标版本提供）；
3. 让 Kueue 创建 PodGroup，同时保留标签（双写）；
4. 对比两套状态：Kueue Workload 的 PodSets 与 PodGroup 的成员是否一致。

### 阶段 2：只读对比

对同一批作业，记录：

- 准入时间（Kueue QuotaReserved）；
- 首个 Pod 调度时间；
- 全部 Pod Permit 时间；
- 全部 Pod Ready 时间；
- 驱逐/超时次数。

如果原生 WAS 的"全部 Permit"显著晚于 Kueue 准入，说明 gang 等待成本高；如果 `PodsReady` 超时率上升，说明两层超时不匹配。

### 阶段 3：切换

按队列/标签灰度：

```yaml
# 示意：只让某个 LocalQueue 走 WAS 路径
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  name: was-enabled
  namespace: team-a
spec:
  clusterQueue: team-a
  # 具体开关字段以目标 Kueue 版本为准；没有字段时用作业标签/配置分流
```

### 阶段 4：退场

- 停止写旧标签，但保留读取兼容；
- 确认没有作业依赖标签式分组；
- 再删除兼容代码/配置。

### 回滚

回滚的第一原则：**不要让 Pod 依赖一个会被删除的 API 对象**。

- 回滚前先关闭 Kueue 的 WAS 创建路径，让新作业回到标签式；
- 已创建的 PodGroup 不要直接删，等对应 Workload 完成/驱逐；
- `scheduling.k8s.io` 的 Workload/PodGroup 是 **Kubernetes in-tree API，不是 CRD**，没有 CRD 可删；真正的风险是：关闭 `GenericWorkload` 时仍有 Pod 引用 `spec.schedulingGroup`，以及升级到 1.37 前残留的 `v1alpha2` 对象；
- 升级/回滚顺序：先排空引用 PodGroup 的 Pod → 关闭 gate → 确认没有 Pod 引用 `spec.schedulingGroup` → 清理旧的 v1alpha2 对象（如果跨版本升级）。

## 38.8 故障模式与诊断

| 症状 | 可能原因 | 检查 |
|---|---|---|
| Workload Admitted，但 Pod 全 Pending | PodGroup 未创建/引用错误；gang Permit 等待 | `kubectl get podgroup <name> -o yaml`；Pod 的 group 引用 |
| 部分 Pod Running，部分 Pending | 标签式与 PodGroup 双轨冲突；total-count 不一致 | 对比 Workload PodSets 与 PodGroup 成员 |
| PodGroup Pending 很久 | gang 成员永远凑不齐；资源碎片 | PodGroup conditions；逐 Pod 调度事件 |
| Kueue 驱逐了正在等 Permit 的 Workload | `WaitForPodsReady` 小于 gang 等待预算 | 两层超时配置 |
| 升级后 Pod 创建失败 | `scheduling.k8s.io` 版本不兼容/gate 关闭 | API 版本、conversion、controller 日志 |
| MultiKueue worker 上 gang 不生效 | PodGroup 只在 manager 创建，worker 没有 | worker 集群的 PodGroup 与 gate |
| DRA 设备重复分配/泄漏 | PodGroup 共享 claim 与 Kueue 计费不一致 | `DRAWorkloadResourceClaims` gate；ResourceClaim 状态 |

### 38.8.1 一个分层诊断顺序

```text
1. Kueue Workload: QuotaReserved / Admitted 条件
2. PodGroup: 是否存在、成员数、conditions
3. Pod: 是否创建、是否有 group 引用、调度事件
4. kube-scheduler: Permit 等待、gang 结果、抢占
5. 运行时: 镜像、GPU 初始化、DRA claim
```

不要从 Pod 事件直接推断 Kueue 配额问题；也不要从 Kueue 的 `Admitted=True` 推断 Pod 一定能起来。第 28 章的分层决策树仍然适用，只是中间多了一层 PodGroup。

## 38.9 与 MiniKueue 的映射

MiniKueue 没有 PodGroup 对象，但它的模型已经包含了双轨期的两个核心概念：

| 原生 WAS / Kueue | MiniKueue |
|---|---|
| Kueue Workload（配额单位） | `api.Workload` |
| PodSet（同构副本组） | `api.PodSet` |
| PodGroup（gang 放置单位） | 没有独立对象；一个 Workload 的多个 PodSet 整体准入 |
| gang Permit | 没有调度器；由 `Scheduler.TryAdmit` 一次性分配 |
| `WaitForPodsReady` | `Engine.WaitForPodsReady` + `MarkPodsReady` |
| PodGroup condition | Workload condition + 事件 |

差距：

- MiniKueue 的"gang"是**配额层**的：它保证一个 Workload 的所有 PodSet 一起预留，但不模拟 kube-scheduler 的 Permit；
- 它没有"部分 Pod 已调度、部分等待"的状态；
- 它没有 PodGroup 的成员校验和 total-count 语义；
- 它没有 DRA claim 共享。

### 38.9.1 实验：给 MiniKueue 加一个 PodGroup-lite

目标：在不引入真实调度器的前提下，模拟"配额已准入，但 gang 未满足时 Pod 不能运行"。

建议设计：

1. 新增 `PodGroup` 结构：`Name`、`MinCount`、`Members []string`、`State`；
2. Workload 准入后不立即 `Admitted`，而是进入 `GangPending`；
3. 每个成员 Pod 通过 `MarkPodScheduled` 报到；当 `len(scheduled) >= MinCount` 时一起转为 `Admitted`；
4. 增加超时：超过 `GangTimeout` 后驱逐 Workload，释放配额；
5. 写三个测试：正常 gang、成员不足超时、成员数超过 MinCount。

这个实验能让你亲手实现第 38.5 节的 t5/t6，并观察"配额准入"和"放置 gang"两层状态如何组合。

## 38.10 设计建议

### 38.10.1 什么时候需要原生 WAS

- 作业有严格的 all-or-nothing 放置要求；
- 已经出现"部分 Pod 先跑、占着 GPU 等其余 Pod"的浪费；
- 使用 LWS/JobSet/Ray 等能表达 gang 的框架；
- 集群版本和 Kueue 版本都支持，且团队能承担双轨期的复杂度。

### 38.10.2 什么时候继续用 Kueue 现有机制

- 作业以单 Pod 或松耦合 Pod 为主；
- 已有 Volcano/YuniKorn 等成熟 gang 调度器，且不想再引入一层；
- Kubernetes 版本低，无法启用 `GenericWorkload`；
- 团队没有精力处理 PodGroup 的所有权和版本 skew。

### 38.10.3 平台契约

无论选哪条路，平台都应明确：

- **谁创建 PodGroup**（Kueue/框架/用户）；
- **谁删除 PodGroup**（finalizer/GC）；
- **配额单位与 gang 单位如何对齐**；
- **两层超时如何设置**；
- **哪一层是 SLO 的观测点**（Kueue Admitted / PodGroup Scheduled / Pod Ready）；
- **回滚时 PodGroup 的生命周期**。

把这些写进平台契约，比在故障时临时判断"到底是 Kueue 还是调度器"更重要。

## 38.11 未来

短期内可以预期的方向：

- Kueue 侧 WAS 集成分阶段落地，逐步从"标签式 pod group"过渡到"PodGroup 驱动 PodSets"；
- `CompositePodGroup` 支持 "gang of gangs"，对应多角色训练/推理链；
- `DRAWorkloadResourceClaims` 让一个 PodGroup 共享设备预留，减少 DRA 与 gang 的重复协调；
- Workload-aware preemption 把抢占从 Pod 级提升到作业级，减少"抢了一半"的抖动；
- topology-aware WAS 与 Kueue TAS 的边界进一步明确：谁负责 domain 约束，谁负责 gang 原子性。

这些能力成熟前，最稳妥的策略是：**Kueue 继续做配额与作业语义的唯一事实来源，原生 WAS 只做放置层的 gang；两者之间用显式的 PodGroup 所有权和超时契约连接。** 不要把其中一层的实现细节写死成平台接口。

## 38.12 本章小结

- 原生 WAS 把 gang 从"Kueue 事后兜底"推进到"kube-scheduler 事前协调"，但不取代 Kueue 的配额准入。
- 双轨期的核心风险是对象/单位不对齐：Kueue Workload、scheduling PodGroup、PodSet、Pod 成员数必须能互相解释。
- 1.37 的 `GenericWorkload` 是 Beta 但默认关闭；Kueue 侧集成仍是 RFC/分阶段，不能按"已发布"设计。
- 迁移要分盘点、双写、对比、灰度、退场五步，并保留不依赖 PodGroup 的回滚路径。
- 排障要按 Kueue → PodGroup → Pod → kube-scheduler → 运行时分层，不要跨层猜原因。
- MiniKueue 用"整 Workload 准入 + WaitForPodsReady"覆盖了概念，但没有真实 Permit；38.9 的实验可以补上这一层直觉。

---

[← 上一章：调度决策的可解释性与 What-if](37-decision-explainability.md) | [返回目录](../README.md) | [返回验证记录](../VERIFICATION.md)

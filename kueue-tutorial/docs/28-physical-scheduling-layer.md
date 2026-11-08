# 第 28 章 · 物理落地：Kueue 与 kube-scheduler 的协作边界

前 27 章主要回答"Kueue 如何决定一个作业能不能开始"。本章回答另一半问题：**作业被准入之后，Pod 到底如何落到节点上；当它落不下去时，问题出在哪一层。**

很多生产事故的共同点是：Kueue 显示 `Admitted=True`，用户就认为"调度成功了"；实际上 Pod 可能还卡在 scheduling gate、nodeSelector、拓扑约束、DRA 设备分配或 Cluster Autoscaler 里。把这两层分开看，排障才有清晰的边界。

> 版本提示：本章描述的是 Kueue v0.20.x 稳定线与 Kubernetes 1.34～1.37 的协作模型。Pod 门控名称、TAS 注入方式、DRA API 版本会随版本变化，具体以目标版本的文档和 CRD schema 为准。

## 28.1 两层调度不是重复劳动

Kueue 和 `kube-scheduler` 经常被误认为"两个调度器在抢同一件事"。实际上它们回答的是不同问题：

| 问题 | 负责者 | 决策对象 | 失败表现 |
|---|---|---|---|
| 这个作业现在有没有资格开始？ | Kueue | Workload | `Pending`、`Inadmissible`、`QuotaReserved` 但未 `Admitted` |
| 这个 Pod 应该放在哪个节点？ | kube-scheduler | Pod | `Pending`、`FailedScheduling`、`Unschedulable` |
| 节点上真的有设备/镜像/卷吗？ | kubelet + 插件 | Container | `ContainerCreating`、`ImagePullBackOff`、`FailedMount` |
| 集群不够时要不要扩容？ | Cluster Autoscaler + ProvisioningRequest | Node | ProvisioningRequest `Pending`/`Failed` |

把它们压成一层会失去两个关键能力：

1. **作业级原子性**：Kueue 在 Pod 创建前就锁定全部配额，避免 32 卡作业只启动 20 个 Pod。
2. **节点级最优放置**：kube-scheduler 了解节点亲和性、污点、卷、设备、碎片和打分，Kueue 不应该重复实现这些。

反过来，只靠 kube-scheduler 也做不到作业级配额、Cohort 借用、公平分享和跨集群提名。两层各司其职，通过"暂停/放行 + Flavor 注入 + 状态回写"协作。

## 28.2 从 Job 到 Pod 的七个时间点

以 batch/Job 为例，把一次提交拆成七个可观察的时间点：

```text
t0  用户创建 Job（带 queue-name 标签）
      │
t1  Job 集成创建 Workload，并把 Job 的 spec.suspend 设为 true
      │
t2  Kueue 预留配额：QuotaReserved=True，status.admission 写入
      │
t3  AdmissionCheck 全部 Ready：Admitted=True
      │
t4  Job 集成取消 suspend；Job 控制器创建 Pod
      │
t5  Pod 通过 scheduling gate / nodeSelector / 亲和性进入 kube-scheduler
      │
t6  kube-scheduler 绑定 Node；kubelet 拉镜像、挂卷、启动容器
      │
t7  Pod Ready；WaitForPodsReady 观察全部 Pod 后放行后续作业
```

排障时先确定"停在哪一个 t"：

```bash
# t1：Workload 存在吗？
kubectl get workload -n <ns> -o wide

# t2/t3：条件和 admission
kubectl get workload <name> -n <ns> -o yaml

# t4：Job 是否还被 suspend？
kubectl get job <name> -n <ns> -o jsonpath='{.spec.suspend}'
kubectl get pods -n <ns> -l job-name=<name>

# t5/t6：Pod 是否被调度？
kubectl describe pod <pod> -n <ns>
kubectl get events -n <ns> --sort-by=.lastTimestamp

# t7：容器是否真的起来？
kubectl get pod <pod> -n <ns> -o wide
kubectl logs <pod> -n <ns>
```

这七个时间点对应不同的 owner。把"Kueue 没准入"和"kube-scheduler 没调度"混在一起，会让排障在错误的层反复打转。

## 28.3 暂停与门控：Pod 不能提前创建

Kueue 的核心不变量是：

```text
Workload 未 Admitted 之前，真实 Pod 不应该被创建并开始消耗资源。
```

不同作业对象用不同机制实现这条不变量。

### 28.3.1 batch/Job：`spec.suspend`

Job 原生支持 `spec.suspend`。集成控制器在创建 Workload 后把 Job 挂起，准入后再取消挂起：

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: train
  labels:
    kueue.x-k8s.io/queue-name: team-a-queue
spec:
  suspend: true          # 由集成管理，用户不要手改
  parallelism: 8
  template:
    spec:
      containers:
      - name: worker
        image: example/trainer:latest
```

常见错误：

- 用户手工把 `suspend` 改成 `false`，Pod 在配额预留前就被创建；
- 集成与用户同时写 `suspend`，last-write-wins 导致状态抖动；
- 只暂停了部分副本（JobSet 的某些 replicatedJob），造成半启动。

### 28.3.2 裸 Pod：scheduling gate

Pod 没有 `suspend` 字段，Kueue 的 Pod 集成通常使用 **scheduling gate**：Pod 可以被创建，但在 gate 被移除前不会被调度。

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: single
  labels:
    kueue.x-k8s.io/queue-name: team-a-queue
spec:
  schedulingGates:
  - name: kueue.x-k8s.io/admission   # 名称以目标版本为准
  containers:
  - name: main
    image: busybox
    command: ["sleep", "3600"]
```

语义：

- Pod 已创建，但 `kube-scheduler` 会跳过带 gate 的 Pod；
- Kueue 准入后移除 gate，Pod 才进入调度；
- gate 移除必须是幂等的，重复 reconcile 不能重复添加/删除；
- 不要把 gate 当成配额：gate 只阻止调度，不阻止 Pod 占用 API server/etcd 对象。

### 28.3.3 Deployment/StatefulSet：没有天然暂停

Deployment/StatefulSet 没有"作业"语义，也没有统一的 suspend 字段。两者的集成方式不同：

- **Deployment**：Kueue 通过 webhook 把队列标签注入 Pod 模板，再由 Pod 集成**逐 Pod** 管理；每个 Pod 是独立 Workload，扩容 Pod 在准入前被 scheduling gate 挡住。
- **StatefulSet**：Kueue v0.20 的官方集成**不支持 StatefulSet 的扩缩操作**，而是通过 Plain Pod Group/集成 gate 管理成员；不要手动把 `replicas` 改成 0 再恢复，这会绕过集成的成员记账。具体能力以目标版本文档为准。

上线前必须定义：

- 什么算"完成"？
- 缩容/滚动更新时，配额账本如何变化？
- 旧 Pod 和新 Pod 是否同时计入？
- 副本数变化如何通过 `reclaimablePods` 报告？
- Deployment 的 `maxSurge` 是否为双倍配额留了空间？

如果回答不了，长期服务不适合直接放进批处理队列。第 25 章给出了各框架的暂停机制对照，第 36 章给出推理/弹性负载的完整边界。

## 28.4 ResourceFlavor 注入：逻辑配额如何变成节点约束

Kueue 的 `ResourceFlavor` 不只是配额维度，它还负责把"逻辑资源池"映射到"物理节点池"。准入后，集成层会把 Flavor 的约束注入 Pod：

| ResourceFlavor 字段 | 注入/作用 | 说明 |
|---|---|---|
| `spec.nodeLabels` | 注入 Pod 的 `nodeSelector` | 让 Pod 落到对应节点池 |
| `spec.tolerations` | 注入 Pod 的 `tolerations` | 让 Pod 能容忍节点污点 |
| `spec.nodeTaints` | 准入门槛 | 用户必须在 Pod 模板里显式容忍，Kueue 不代劳 |
| `spec.topologyName` | 启用 TAS | 影响拓扑放置，不一定直接注入 selector |

示例：

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: a100-spot
spec:
  nodeLabels:
    node.kubernetes.io/instance-type: a100-spot
  tolerations:
  - key: nvidia.com/gpu
    operator: Exists
    effect: NoSchedule
```

准入后，Pod 上应该能看到：

```bash
kubectl get pod <pod> -n <ns> -o jsonpath='{.spec.nodeSelector}'
kubectl get pod <pod> -n <ns> -o jsonpath='{.spec.tolerations}'
```

### 28.4.1 Flavor 注入不做什么

Kueue **不会**替你：

- 改 `resources.requests`；
- 加节点亲和性/反亲和性；
- 加 `topologySpreadConstraints`；
- 安装 GPU device plugin；
- 创建 PV/PVC 或解决卷拓扑；
- 保证节点上真的有 GPU。

这些仍由你的 Pod 模板、调度器、设备插件和存储系统负责。Flavor 只保证"这个作业被计到哪个池子，以及 Pod 应该带什么节点约束"。

### 28.4.2 注入失败的典型症状

| 症状 | 可能原因 |
|---|---|
| Workload Admitted，Job 也 unsuspend，但 Pod 不出现 | 集成没有正确更新 Pod 模板/取消 suspend |
| Pod 出现但 `nodeSelector` 为空 | 集成没有注入 Flavor 约束 |
| Pod `FailedScheduling: node(s) didn't match Pod's node affinity/selector` | 节点标签与 Flavor 不一致 |
| Pod `FailedScheduling: untolerated taint` | 用户没容忍 `nodeTaints`，或 Flavor `tolerations` 没注入 |
| Pod 落到错误节点池 | 多个 Flavor 的 nodeLabels 重叠，或 selector 过于宽松 |

排障顺序：先看 Workload 的 `status.admission.podSetAssignments[*].flavors`，再看 Pod 模板，再看节点标签/污点。

## 28.5 All-or-nothing 的三个层次

"gang scheduling"常被当成一个词，但它至少有三个层次：

| 层次 | 保证什么 | 由谁实现 | 失败后果 |
|---|---|---|---|
| 配额 gang | 全部 Pod 的配额一起预留 | Kueue | 不会只占一半配额 |
| 放置 gang | 全部 Pod 能一起放到节点上 | kube-scheduler gang 插件 / Volcano / YuniKorn | 部分 Pod 已绑定，部分永远 Pending |
| 运行 gang | 全部容器一起启动/就绪 | 应用/框架/Job 控制器 | 训练进程互相等待，超时退出 |

Kueue 的准入只保证第一层。它把"逻辑上放得下"提前到 Pod 创建之前，但**不保证 kube-scheduler 一定能原子地放下所有 Pod**。这就是为什么还需要：

- TAS：在准入时检查拓扑容量；
- ProvisioningRequest：在云上等物理容量；
- WaitForPodsReady：最后的超时安全网；
- 调度器 gang 插件：真正保证放置原子性。

### 28.5.1 Kueue + Volcano 的组合

一个常见组合是：

```text
Kueue        → 作业级配额、借用、公平、抢占
Volcano      → Pod 级 gang 放置、DRF、队列
kube-scheduler → 不用（由 Volcano 替代）或只处理非 gang 负载
```

组合时要明确边界：

- 不要让 Kueue 和 Volcano 同时做"队列顺序"，否则两套语义会打架；
- Kueue 的 `Admitted` 只表示配额到手，Volcano 的 `PodGroup` 还要满足 gang 条件；
- 抢占可能发生两次：Kueue 驱逐 Workload，Volcano 驱逐 Pod。需要统一的优先级/受害者模型；
- 监控要同时看 Kueue 的 `Evicted` 和 Volcano 的 `Unschedulable`。

如果集群主要跑 AI 训练，且必须严格 gang 放置，可以在 Kueue 之外保留一个 gang 调度器；如果只是希望"不要半启动"，Kueue + TAS/ProvisioningRequest/WaitForPodsReady 通常已经够用。

### 28.5.2 Kubernetes 原生 gang scheduling

社区一直在推进 PodGroup/Gang Scheduling 的原生 API。无论最终形态如何，Kueue 的 Workload 仍然有独立价值：它是**跨框架的作业抽象**和**配额账本**。即使调度器原生支持 gang，Kueue 仍负责"哪个作业先获得配额"。

## 28.6 TAS 如何影响真实放置

第 9 章讲了 TAS 的对象和注解。本节从"物理落地"角度补充：TAS 的 assignment 如何变成节点约束，以及它和 kube-scheduler 其他能力的关系。

### 28.6.1 从节点到 domain

TAS 的 `Topology` 定义层级：

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: Topology
metadata:
  name: gpu-topology
spec:
  levels:
  - nodeLabel: cloud.provider.com/topology-block
  - nodeLabel: cloud.provider.com/topology-rack
  - nodeLabel: kubernetes.io/hostname
```

Kueue 从节点标签推导每个 domain 的容量，并扣除：

- 已准入 TAS Workload 的预留；
- 非 TAS Pod（DaemonSet、Deployment、系统组件）的占用；
- 已绑定但未被 TAS reservation 覆盖的 Pod；
- 不健康/不可调度节点的容量（受 `TASResidualPodAccounting` 等 gate 影响）。

这与 MiniKueue 的"显式叶子域容量"不同：真实 TAS 的容量是**动态推导**的，节点增减、Pod 起停都会改变 domain 可用量。

### 28.6.2 assignment 如何落地

Kueue 在 `status.admission.podSetAssignments[*].topologyAssignment` 中记录每个 PodSet（或 slice）落在哪个 domain、多少个 Pod。集成层负责把 assignment 转成 Pod 的节点约束。

需要确认的两件事：

1. **约束形式**：不同版本可能用 nodeSelector、nodeAffinity 或两者组合。以目标版本的集成代码为准。
2. **约束粒度**：Required 应该把 Pod 限制在同一个 domain；Preferred 允许降级；Unconstrained 只按容量铺开。

排障时不要只看 Pod 的 `nodeName`，还要看：

```bash
kubectl get workload <name> -n <ns> \
  -o jsonpath='{.status.admission.podSetAssignments}' | jq .
kubectl get pod <pod> -n <ns> -o yaml | grep -A20 affinity
```

### 28.6.3 TAS 与 PodTopologySpread 的区别

| 能力 | TAS | PodTopologySpread |
|---|---|---|
| 决策时机 | 准入时（Kueue） | 调度时（kube-scheduler） |
| 作用对象 | Workload/PodSet | Pod |
| 是否感知配额 | 是 | 否 |
| 能否跨 Workload 打散 | 通过 TAS spreading gate | 原生支持 |
| 失败语义 | Required 找不到 domain 就不准入 | 尽量满足，失败可能 Pending |

它们不是替代关系：TAS 保证"配额和拓扑一起可行"，PodTopologySpread 保证"运行时的分布"。可以同时使用，但要让两者的约束一致，避免 TAS 选了一个 domain，PodTopologySpread 又把它打散到别的 domain。

### 28.6.4 一个 TAS 放置推演

```text
4 台 host，每台 2 GPU，分属两个 rack：
  rack-a: h1(2), h2(2)
  rack-b: h3(2), h4(2)

作业：8 个 Pod，每个 1 GPU，required-topology=rack
```

结果：

- rack-a 聚合 4 GPU，放不下 8；
- rack-b 聚合 4 GPU，也放不下；
- Required 模式不会降级，Workload Pending。

把注解改成 `preferred-topology=rack`：

- 先尝试同 rack，失败后逐级放宽；
- 可能跨 rack 放置，但物理上仍需要 8 个 GPU；
- 如果节点总数够，Workload Admitted，Pod 可能分散在两个 rack。

这就是"配额够 ≠ 拓扑放得下"的物理版本。

## 28.7 DRA：配额计数与设备分配是两件事

DRA（Dynamic Resource Allocation）把 GPU/FPGA/网卡等设备从扩展资源升级为一等对象：

```text
DeviceClass       设备类别（例如 gpu-a100）
ResourceSlice     节点上报的设备清单
ResourceClaim     一次设备分配请求
ResourceClaimTemplate  Pod 可引用的 claim 模板
```

Kueue 在准入阶段做的是**配额计数**：把 DeviceClass 映射成逻辑资源（如 `nvidia.com/gpu`），按设备数/Counter/Capacity 计费。真正的设备分配仍由 kube-scheduler 的 DRA 插件完成。

### 28.7.1 两条计费路径

| 路径 | Pod 写法 | Kueue 如何计数 | 注意 |
|---|---|---|---|
| ResourceClaimTemplate | `resourceClaims[].resourceClaimTemplateName` | 读 DeviceClass，按 `deviceClassMappings` 映射 | 需要配置映射 |
| 扩展资源 | `requests["nvidia.com/gpu"]` | 读 DeviceClass 的 `extendedResourceName` | 通常不需要映射 |

两条路径不要对同一个 DeviceClass 同时配置，否则容易出现"Pod 只请求一次、账本扣两次"。

### 28.7.2 失败分层

| 现象 | 层 | 排查 |
|---|---|---|
| Workload Pending，`InsufficientQuota` | Kueue | 配额/映射/DeviceClass 是否正确 |
| Workload Admitted，Pod Pending，`FailedScheduling` | kube-scheduler | 节点是否有 ResourceSlice、设备是否被占用、claim 是否可分配 |
| Pod Pending，`ResourceClaim` 未分配 | DRA driver | driver 是否运行、设备健康、CEL selector |
| Pod Running 但设备不可用 | kubelet/driver | 设备插件、CDI、运行时 |

`KueueDRADeviceFeasibility` 可以在 TAS 放置时做一次设备可行性检查，但它不是万能的：它不覆盖多 Pod 竞争、抢占释放、所有 DRA 特性和分配耗时。

### 28.7.3 DRA 排障顺序

```text
1. Workload 是否计费？        status.admission.usage 是否包含逻辑资源
2. DeviceClass 是否映射？     deviceClassMappings / extendedResourceName
3. Pod 是否引用 claim？       resourceClaims / resources.requests
4. ResourceClaim 是否分配？   kubectl get resourceclaims -A
5. 节点是否有设备？           kubectl get resourceslices
6. DRA driver 是否正常？      kubectl get pods -n <driver-ns>
```

## 28.8 ProvisioningRequest 与 Cluster Autoscaler

ProvisioningRequest 把"逻辑配额"和"云上物理容量"连接起来。它解决的是：**Kueue 说配额够，但集群里没有节点能放 Pod。**

### 28.8.1 完整流程

```text
Workload QuotaReserved
      │
      ▼
ProvisioningRequest 控制器创建 ProvisioningRequest
      │
      ▼
Cluster Autoscaler / 云容量控制器扩容
      │
      ▼
新节点 Ready
      │
      ▼
AdmissionCheck Ready
      │
      ▼
Workload Admitted → Job unsuspend → Pod 创建 → kube-scheduler 绑定
```

### 28.8.2 provisioning class

| Class | 行为 | 适用 |
|---|---|---|
| `check-capacity.autoscaling.x-k8s.io` | 只检查现有容量，不扩容 | 已有节点池，验证能否放下 |
| `best-effort-atomic-scale-up.autoscaling.x-k8s.io` | 请求原子扩容，尽力而为 | 云上按需扩容 |
| 厂商自定义 class | 由驱动实现 | 特定云/裸金属 |

"best effort" 意味着不保证严格 gang：云厂商可能只扩出部分节点。真正的 all-or-nothing 仍要靠配额 + TAS + WaitForPodsReady。

### 28.8.3 Retry 与 Rejected

- 临时失败（容量不足、配额申请中）：`Retry`，释放配额，退避重试；
- 永久失败（配置错误、超出预算）：`Rejected`，停用 Workload；
- 每次重试创建新的 ProvisioningRequest，名字通常是 `<workload>-<check>-<attempt>`；
- 退避由 `ProvisioningRequestConfig.spec.retryStrategy` 控制。

### 28.8.4 为什么"Admitted 了但没节点"

ProvisioningRequest 的 Ready 表示"容量控制器认为可以扩出来"，不一定表示节点已经 Ready。常见时间差：

- 云上扩容需要几分钟；
- 节点加入集群后还要安装 device plugin、拉镜像；
- 节点池的 max size 已满，扩容被拒绝；
- 可用区容量不足，扩容失败但检查还没超时。

这就是为什么生产上要同时监控：

```text
kueue_admission_checks_wait_time_seconds
kueue_evicted_workloads_total{reason="PodsReadyTimeout"}
provisioningrequest 的 status/conditions
节点池的 desired/current size
```

### 28.8.5 Scheduling gate 与 Cluster Autoscaler/Karpenter

ProvisioningRequest 不是"直接扩容"，它需要 Cluster Autoscaler（CA）或 Karpenter 配合。双轨期最常见的误解是："Pod 已经被 Kueue 放行，CA 看到 Pending 就会扩容。"

实际上：

- 带 `schedulingGates` 的 Pod 通常不会被 CA/Karpenter 当作普通 Pending Pod 来触发扩容；
- CA 需要一个显式的 ProvisioningRequest 关联，通常通过 Pod 模板上的注解（例如 `autoscaling.x-k8s.io/consume-provisioning-request`，具体键名以 CA/Kueue 版本为准）告诉它"这个 Pod 属于哪个 PR、用哪个 provisioning class"；
- Kueue 通过 AdmissionCheck 的 `podSetUpdates` 把这些注解注入 Pod 模板；
- 如果注解缺失、PR 名字不匹配、provisioning class 不存在，CA 会认为没有可扩容的请求，Pod 永远 Pending。

排查清单：

```text
1. Pod 模板是否有 PR 关联注解？
2. 注解里的 PR 名字是否等于实际对象名？
3. ProvisioningRequest 的 provisioningClassName 是否存在？
4. PR condition 是 Accepted/Provisioned/Failed 中的哪一个？
5. 节点池 max size / 可用区配额是否已满？
6. CA/Karpenter 日志里是否出现 "no pod to scale up" / "provisioning request not found"？
```

一个实用原则：**不要同时让 Kueue 的 `WaitForPodsReady`、CA 的扩容超时、PR 的 provisioning timeout 三者互相等待**。给它们设置明确的先后预算，并把 PR 名字、Workload UID、Pod 名字放进同一份排障记录。

## 28.9 分层诊断决策树

面对"作业没跑起来"，先分层，再深挖：

```text
1. Workload 存在吗？
   ├─ 否 → Job 集成 / queue-name 标签 / 控制器是否运行
   └─ 是 ↓
2. QuotaReserved=False 且 Reason=Inadmissible？
   ├─ 是 → namespaceSelector / LimitRange / Flavor / 资源名
   └─ 否 ↓
3. AdmissionGated=True？
   ├─ 是 → 外部 gate 未清除
   └─ 否 ↓
4. QuotaReserved=False？
   ├─ 是 → 配额/Cohort/退避/StopPolicy
   └─ 否 ↓
5. Admitted=False？
   ├─ 是 → AdmissionCheck / ProvisioningRequest / MultiKueue
   └─ 否 ↓
6. Job 还 suspend 吗？
   ├─ 是 → 集成没有放行 / 条件未同步
   └─ 否 ↓
7. Pod 存在吗？
   ├─ 否 → Job 控制器 / scheduling gate / 副本数
   └─ 是 ↓
8. Pod 被调度了吗？
   ├─ 否 → nodeSelector / toleration / 亲和性 / TAS / DRA / 碎片
   └─ 是 ↓
9. 容器 Ready 吗？
   ├─ 否 → 镜像 / 卷 / 设备 / Probe / 启动脚本
   └─ 是 → 运行中，看业务日志
```

每一步只回答一个问题。不要从控制器日志开始猜；先让对象状态和事件说话。

### 28.9.1 各层的证据来源

| 层 | 主要证据 | 命令 |
|---|---|---|
| Kueue 配额 | Workload conditions/admission | `kubectl describe workload` |
| Job 集成 | Job `spec.suspend`、Pod 存在性 | `kubectl get job -o yaml` |
| kube-scheduler | Pod events、`nodeName` | `kubectl describe pod` |
| kubelet | Container status、events | `kubectl describe pod`、`kubectl logs` |
| DRA | ResourceClaim/Slice | `kubectl get resourceclaims,resourceslices -A` |
| Autoscaler | ProvisioningRequest、节点池 | `kubectl get provisioningrequests -A` |
| 控制器 | 日志、指标、leader | `kubectl logs`、`/metrics` |

### 28.9.2 案例：Admitted 但 Pod 一直 Pending

```text
Workload: Admitted=True
Job: suspend=false
Pod: Pending
Event: 0/12 nodes are available: 12 node(s) didn't match Pod's node affinity/selector
```

根因通常不是 Kueue，而是：

- Flavor 的 `nodeLabels` 与节点标签不一致；
- 集成没有注入 nodeSelector，Pod 用了默认调度；
- 节点标签被改动，Flavor 没有同步；
- TAS assignment 指向了一个没有容量的 domain。

处理：对比 `status.admission.podSetAssignments[*].flavors`、Pod 的 nodeSelector/affinity、节点标签。

### 28.9.3 案例：Pod 调度了但容器起不来

```text
Pod: Running/Pending, nodeName 已设置
Event: FailedMount / ImagePullBackOff / CrashLoopBackOff
```

这时 Kueue 的职责已经结束。不要继续调 Kueue 配额；转向卷、镜像、设备插件、启动命令和资源 limits。

### 28.9.4 案例：所有 Pod 都 Running，但 Workload 不 Finished

可能原因：

- Job 集成没有把完成状态同步回 Workload；
- Job 的 `completions`/`successPolicy` 与 Kueue 的 Finished 判定不一致；
- Pod 完成后 Job 还在重试/清理；
- `reclaimablePods` 没有正确报告，账本仍按旧请求计算。

检查 Job 状态、Pod 完成数、Workload 的 `Finished` 条件。

## 28.10 一个端到端演练

目标：一个 8 卡作业，使用 TAS（同 rack）、DRA 设备、云上扩容。把它在每一层的期望状态写出来：

```text
1. 用户提交 JobSet（8 个 worker，每个 1 GPU，required-topology=rack）
2. Kueue 创建 Workload，JobSet suspend
3. Kueue 检查配额：CQ 有 16 GPU nominal，足够
4. TAS 检查 rack 容量：rack-a 有 8 GPU，选中 rack-a
5. QuotaReserved=True，status.admission 记录 flavor/rack
6. AdmissionCheck=ProvisioningRequest：创建 PR，等待云扩容
7. PR Ready → Admitted=True
8. JobSet unsuspend → 8 个 Pod 创建
9. Pod 带 nodeSelector/affinity，kube-scheduler 绑定到 rack-a 的节点
10. DRA 插件为每个 Pod 分配 GPU
11. kubelet 启动容器，Pod Ready
12. WaitForPodsReady 观察 8/8 Ready，Workload 进入稳定运行
```

如果第 4 步失败：Workload Pending，原因是拓扑容量不足，不是配额不足。
如果第 6 步失败：Workload QuotaReserved 但未 Admitted，配额已被锁住。
如果第 9 步失败：Workload Admitted，Pod Pending，问题在 kube-scheduler。
如果第 10 步失败：Pod Pending，ResourceClaim 未分配，问题在 DRA driver。
如果第 11 步失败：Pod 已绑定，问题在 kubelet/镜像/卷/设备。

把这条链路画出来，比背 API 字段更能定位问题。

## 28.11 物理层检查清单

- [ ] 每个 Flavor 的 `nodeLabels` 与真实节点标签一致；
- [ ] `nodeTaints` 与用户 toleration 匹配，`tolerations` 已注入；
- [ ] Job/Pod 集成在 Admitted 前不会创建可调度 Pod；
- [ ] 多 PodSet 作业整体暂停/放行；
- [ ] TAS 的 Topology levels 与节点标签一致；
- [ ] TAS 容量扣除了非 TAS Pod 和残留 Pod；
- [ ] DRA 的 DeviceClass 映射只保留一条计费路径；
- [ ] ProvisioningRequest 的 provisioning class 与云控制器匹配；
- [ ] WaitForPodsReady 的 timeout 覆盖镜像拉取/设备初始化时间；
- [ ] 监控能区分"Kueue 没准入"和"调度器没放下"；
- [ ] gang 需求明确：配额 gang、放置 gang、运行 gang 分别由谁保证；
- [ ] 节点池/autoscaler 的上限与 Kueue 的 nominal 匹配。

## 28.12 本章小结

- Kueue 负责"作业何时开始、配额给谁"，kube-scheduler 负责"Pod 放哪"，两层不能互相替代。
- 从 Job 到 Pod 有七个时间点；排障先确定停在哪一层。
- 暂停/门控保证 Admitted 前不创建可调度 Pod；不同框架机制不同。
- ResourceFlavor 的 nodeLabels/tolerations 会注入 Pod；nodeTaints 是用户必须跨过的门槛。
- All-or-nothing 分三层：配额 gang（Kueue）、放置 gang（调度器）、运行 gang（应用）。
- TAS 的 domain 容量是动态推导的；assignment 最终要变成 Pod 的节点约束。
- DRA 的配额计数与设备分配是两件事；排障要分层看 Kueue、scheduler、driver、kubelet。
- ProvisioningRequest 把逻辑配额接到云容量，但"Admitted"不等于"节点已 Ready"。
- 用 28.9 的分层决策树和 28.11 的检查清单，把"作业没跑起来"拆成可验证的问题。

---

[← 上一章：验证工程](27-verification-engineering.md) | [返回目录](../README.md) | [下一章：排队论与容量工程 →](29-queueing-capacity-engineering.md)

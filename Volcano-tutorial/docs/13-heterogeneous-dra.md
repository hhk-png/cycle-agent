# 第 13 章 异构设备与 DRA

## 13.1 GPU 不是一种资源

至少有以下差异：

- **厂商**：NVIDIA、AMD、昇腾、寒武纪、海光……
- **型号**：A100、H100、H800、MI300、910B……
- **显存**：40G/80G/141G……
- **互联**：PCIe、NVLink、NVSwitch、RDMA、HCCS……
- **切分**：整卡、MIG、vGPU、vNPU、时间片共享……
- **拓扑**：NUMA 亲和、GPU 直连、网卡亲和……

调度器如果只看到 `nvidia.com/gpu: 1`，就无法区分 A100 和 H100，也无法处理 MIG/共享。Volcano 的异构能力分几层。

## 13.2 资源名与节点标签

Kubernetes 用扩展资源表达设备：

```yaml
resources:
  requests:
    nvidia.com/gpu: "1"
  limits:
    nvidia.com/gpu: "1"
```

节点上由 device plugin 上报 `allocatable`。型号选择通常用节点标签：

```yaml
nodeSelector:
  nvidia.com/gpu.product: NVIDIA-A100-SXM4-80GB
```

GPU Operator 会打一系列标签：

- `nvidia.com/gpu.product`
- `nvidia.com/gpu.memory`
- `nvidia.com/gpu.count`
- `nvidia.com/mig.config`

先用 `kubectl get node --show-labels` 确认实际键值，不要照抄网上示例。

## 13.3 MIG 与 vGPU

### 13.3.1 MIG

MIG（Multi-Instance GPU）把一张 A100/H100 切成多个隔离实例，每个实例有独立显存和计算单元。Kubernetes 中通常表现为额外资源：

```text
nvidia.com/mig-1g.5gb
nvidia.com/mig-2g.10gb
...
```

调度器把它当普通扩展资源。注意：

- MIG 配置由节点侧管理，Pod 不能动态切分；
- 不同 MIG profile 的资源名不同，队列配额要分别统计；
- MIG 实例的拓扑/亲和可能影响性能。

### 13.3.2 vGPU / 时间片

vGPU（如 HAMi、Volcano 的 vGPU 支持）允许一个物理 GPU 被多个 Pod 共享，通常通过：

- 显存/算力比例；
- 设备内存隔离；
- 调度器侧的账本。

Volcano 在 v1.14/v1.15/v1.16 持续增强 vGPU/vNPU 支持，包括：

- Ascend vNPU 的 MindCluster/HAMi 两种模式；
- vGPU UUID allow/deny 注解；
- 设备级账本与队列配额。

共享 GPU 的坑：

- 两个 Pod 共享一张卡，显存争抢可能导致 OOM；
- 队列配额按“卡”还是“显存/算力”统计，必须统一；
- 驱逐一个共享 Pod 可能影响同卡的其他 Pod。

## 13.4 deviceshare：GPU 共享的账本

### 13.4.1 共享的四种粒度

Volcano 生态里的 GPU 共享至少有四种：

| 粒度 | 资源/机制 | 隔离性 | 典型场景 |
|---|---|---|---|
| 整卡 | `nvidia.com/gpu` | 强 | 训练 |
| 显存共享 | `volcano.sh/gpu-memory` | 中（依赖设备插件） | 推理、小模型 |
| vGPU（HAMi） | `volcano.sh/vgpu-number/memory/cores` | 中 | 多租户推理 |
| MIG | `nvidia.com/mig-*` | 强（硬件隔离） | 稳定推理 |

`deviceshare` 插件负责“共享 GPU 的调度账本”：它知道一张卡被哪些 Pod 共享、还剩多少显存/算力，并在 Predicate/NodeOrder 中做匹配。

> 版本提示：`volcano.sh/gpu-memory` 这类 GPU Sharing 模式在较新版本中已不是主推路径，vGPU（HAMi）与 MIG 更常见。新部署优先评估 vGPU/MIG；老集群升级前确认 device plugin 与 scheduler 的资源名是否仍匹配。

### 13.4.2 配置

```yaml
- name: deviceshare
  arguments:
    deviceshare.GPUSharingEnable: "true"
    deviceshare.VGPUEnable: "false"
    deviceshare.SchedulePolicy: "binpack"   # binpack | spread
```

注意：

- **GPU Sharing 与 vGPU 互斥**；同时开启会导致 scheduler 启动失败（fatal）。
- `SchedulePolicy` 决定共享 Pod 是聚拢还是打散；与节点打分插件叠加时要注意方向。
- 共享模式需要配套的 device plugin（Volcano devices 或 HAMi）；只有 scheduler 插件没有设备侧支持，Pod 仍然拿不到设备。

### 13.4.3 作业写法

显存共享：

```yaml
resources:
  requests:
    volcano.sh/gpu-memory: "8192"     # MiB
  limits:
    volcano.sh/gpu-memory: "8192"
```

vGPU：

```yaml
resources:
  requests:
    volcano.sh/vgpu-number: "1"
    volcano.sh/vgpu-memory: "8192"
    volcano.sh/vgpu-cores: "30"       # 百分比
```

具体资源名取决于 device plugin 版本；先用 `kubectl describe node` 确认 allocatable。

### 13.4.4 账本陷阱

1. **队列配额写错资源名**：Queue capability 写 `nvidia.com/gpu`，作业却请求 `volcano.sh/vgpu-number`，配额永远为 0 或被绕过。
2. **共享 Pod 驱逐影响同卡邻居**：一张卡被 4 个 Pod 共享，驱逐一个可能让其余 Pod 的显存/算力配额失真。
3. **设备插件与调度器版本不匹配**：资源名、注解、分配协议都可能变化。
4. **MIG 与共享混用**：MIG 实例是独立资源，和 vGPU 账本不是一套。
5. **vGPU cores 超卖**：cores 是百分比，节点总 cores 与物理算力不是线性关系；配额要留安全余量。

### 13.4.5 排障

```bash
kubectl get node <node> -o json | jq '.status.allocatable | with_entries(select(.key|test("gpu|vgpu")))'
kubectl get pods -n kube-system | grep -E 'volcano-device|hami'
kubectl -n volcano-system logs deploy/volcano-scheduler | grep -i deviceshare
```

如果 Pod Pending 且日志显示设备不足，先确认是“节点没有该资源名”还是“共享账本已满”。

## 13.5 NUMA 与设备亲和

一个 GPU 训练 Pod 可能同时需要：

- 8 CPU；
- 64Gi 内存；
- 1 张 GPU；
- 1 张 RDMA 网卡。

如果 CPU/内存在 socket 0，GPU 在 socket 1，跨 socket 访问会增加延迟。NUMA-aware 插件与设备插件配合，尽量把资源放在同一个 NUMA 节点。

Volcano 的 `numa-aware` 插件读取 `Numatopology`（numatopo）拓扑，要求 Pod 的资源能放进单个 NUMA 节点。它不负责网卡/GPU 的 PCIe 拓扑，那通常由设备插件或拓扑管理器（Topology Manager）处理。

## 13.6 DRA：Kubernetes 的新设备模型

DRA（Dynamic Resource Allocation）用 `ResourceClaim`/`DeviceClass` 表达设备，而不是简单的整数扩展资源。

### 13.6.1 为什么需要 DRA

- GPU 有型号、显存、MIG profile、互联等属性；
- 一个 Pod 可能需要“两张同型号、同 NUMA、带 NVLink 的 GPU”；
- 设备可能由多个容器共享；
- 设备分配需要结构化参数，而不是一个整数。

DRA 用：

- `DeviceClass`：设备类别；
- `ResourceClaim`：一次具体的设备请求；
- `ResourceClaimTemplate`：为每个 Pod 生成 claim；
- `ResourceSlice`：节点上报设备。

### 13.6.2 Volcano 的 DRA 支持

- v1.14 及更早：可以调度使用 DRA 的 Pod，但队列配额不覆盖 claim；
- **v1.15**：capacity 插件支持 DRA 队列配额；
- v1.16：DRA 打分/容量修复，Kubernetes 1.36 支持。

启用（scheduler config）：

```yaml
- name: capacity
  enableHierarchy: true          # plugin option，不是 argument
  arguments:
    capacity.DynamicResourceAllocationEnable: "true"
    capacity.DRAConsumableCapacityEnable: "true"
```

队列配额键：

```yaml
capability:
  cpu: "64"
  memory: 256Gi
  "deviceclass/gpu.nvidia.com": "8"
  "cores.deviceclass/hami-core-gpu.project-hami.io": "800"
  "memory.deviceclass/hami-core-gpu.project-hami.io": "320Gi"
```

- `deviceclass/<class>`：整设备数量；
- `<dim>.deviceclass/<class>`：可消费容量（如 vGPU 的 cores/memory）。

重要语义：

- 共享的 `ResourceClaim` 只计一次，不会因为多个 Pod 引用而重复计费；
- 直接 claim、模板生成的 claim、共享 claim 都要计入；
- 部分分配模式可能还不在配额范围内，升级前确认。

完整示例见 `examples/queues/dra-queue.yaml` 与 `examples/config/scheduler-config-dra.yaml`。

### 13.6.3 DRA claim 的生命周期

DRA 不是“把整数换成对象”这么简单，它多了一条异步的资源准备链路：

```text
Pod 创建
  → 调度器为 Pod 选择节点，并分配/预留 ResourceClaim
  → DRA driver 在节点上 Prepare（可能挂载设备、配置 MIG）
  → kubelet 启动容器
  → Pod 删除
  → driver Unprepare，claim 释放（共享 claim 可能被其他 Pod 继续引用）
```

对调度器的影响：

1. **配额统计要覆盖 claim**：v1.15+ 的 capacity 插件把 `deviceclass/<class>` 与可消费容量计入队列；共享 claim 只计一次。
2. **Prepare 失败会回退**：调度器已经选了节点，但 driver 准备失败时 Pod 会卡在 ContainerCreating；Volcano 的队列账本要能感知并释放。
3. **共享 claim 的引用计数**：多个 Pod 引用同一 claim 时，删一个 Pod 不能释放设备；配额也不能重复计算。
4. **驱逐的延迟**：被驱逐的 Pod 要等 claim 释放后，设备才能给下一个作业；高频抢占会放大这个延迟。

排障命令：

```bash
kubectl get resourceclaim -A
kubectl describe resourceclaim <name>
kubectl get resourceslice
kubectl -n <driver-ns> logs <dra-driver-pod>
kubectl -n volcano-system logs deploy/volcano-scheduler | grep -i -E 'dra|claim'
```

常见现象：

- Pod Pending 且 scheduler 日志显示 claim 分配失败 → 检查 DeviceClass/ResourceSlice 是否覆盖该节点；
- Pod 卡在 ContainerCreating → driver Prepare 失败，不是调度器问题；
- 队列配额未超但 claim 分配失败 → 共享 claim/部分分配模式未计入，或 driver 容量不足；
- 删除 Pod 后设备仍被占用 → claim 未释放或仍被其他 Pod 引用。

## 13.7 异构调度策略

### 13.7.1 按型号分队列

```text
a100-queue: capability nvidia.com/gpu=32
h100-queue: capability nvidia.com/gpu=16
```

作业用 nodeSelector 选型号。队列配额按型号分别统计，避免“A100 作业占了 H100 的配额”。

### 13.7.2 按优先级分队列

- 高优先级训练用整卡；
- 低优先级实验用 MIG/vGPU；
- 推理用共享 GPU，设置显存上限。

### 13.7.3 拓扑与互联

- NVLink 域内优先；
- 跨机 RDMA 用 HyperNode；
- 网卡与 GPU 的 NUMA 亲和。

这些约束不是单个插件能解决的，需要 device plugin、topology manager、Volcano 的 network-topology-aware/NUMA 共同配合。

## 13.8 账本与配额陷阱

1. **整卡 vs 切片**：队列 capability 写 `nvidia.com/gpu: 8`，但节点用 MIG 上报 `nvidia.com/mig-1g.5gb: 56`，两者对不上。
2. **共享 claim 重复计费**：没有去重时，多个 Pod 引用同一 claim 会多算。
3. **requests != limits**：扩展资源必须相等，否则 Pod 被拒绝。
4. **节点标签漂移**：GPU Operator 升级后标签键变化，nodeSelector 失效。
5. **DRA feature gate**：Kubernetes 版本/feature gate 不匹配，claim 创建失败。
6. **vGPU 超卖**：显存配额与队列配额是两套账，可能一个允许、一个拒绝。

## 13.9 诊断

```bash
# 节点资源
kubectl describe node <node> | grep -A20 Allocatable

# GPU Operator
kubectl get pods -n gpu-operator

# DRA
kubectl get deviceclass
kubectl get resourceclaim -A
kubectl get resourceslice

# Volcano
kubectl get queue <q> -o yaml
kubectl describe podgroup <pg>
kubectl -n volcano-system logs deploy/volcano-scheduler | grep -i -E 'dra|device'
```

一个常见误判：节点 `allocatable` 有 GPU，但 Pod 仍然 Pending。原因可能是：

- 队列 capability 不够；
- 节点有污点；
- GPU 被 MIG 切分，资源名不匹配；
- DRA claim 未绑定；
- NUMA 约束无法满足。

## 13.10 MiniVolcano 的实现

MiniVolcano 只实现“标量资源 + 节点选择器 + NUMA”：

- 资源用 `map[string]int64`，支持任意扩展资源名；
- `predicates` 支持 nodeSelector/taints；
- `numa-aware` 要求单 NUMA 适配；
- 没有 MIG/vGPU/DRA/设备拓扑；
- 没有 `deviceshare` 的共享账本（显存/算力），只有整卡式的标量计数；
- 队列 capability 可以写任意资源名（包括 `nvidia.com/gpu`），但不会解析 `deviceclass/...` 或 `volcano.sh/vgpu-*`。

场景：

- `heterogeneous`：A100 节点选择；
- `numa`：单 NUMA 约束。

这些差异在第 24 章会列出。Mini 的价值是展示“资源向量 + 过滤 + 打分”的骨架，而不是设备管理。

## 13.11 GPU 互联与选型矩阵

调度器看到的“GPU 数量”只是设备的一半信息。对训练作业，GPU 之间的互联方式往往比单卡型号更决定性能。

### 13.11.1 互联层次

| 层次 | 技术 | 影响 | 调度器能做什么 |
|---|---|---|---|
| 卡内 | HBM、L2 | 单卡带宽/容量 | 只能通过型号/显存标签区分 |
| 节点内 | PCIe、NVLink、NVSwitch | 卡间 all-reduce、P2P | 通过设备插件/DRA 属性或节点标签表达 |
| 节点间 | InfiniBand、RoCE、以太网 | 多机通信、参数同步 | HyperNode/拓扑感知 |
| 网卡- GPU | PCIe 亲和、GPUDirect | 跨节点吞吐 | 需要 device plugin/DRA 报告亲和 |
| 厂商互联 | NVLink-C2C、HCCS | CPU-GPU/NPU 一致性 | 通常由厂商插件处理 |

一个常见误区：节点有 8 张 H100，就认为“8 卡作业一定快”。如果这 8 张卡分属两个 NVLink 域，跨域通信会降速；调度器需要知道“卡在哪个域”，而不只是“节点有几张卡”。

### 13.11.2 选型矩阵

| 场景 | 首选 | 次选 | 不建议 |
|---|---|---|---|
| 大模型预训练 | 整卡 + NVLink/NVSwitch + IB | 整卡 + RoCE | MIG/时间片共享 |
| 微调/中等训练 | 整卡或 MIG（按显存） | vGPU（隔离足够时） | 过度超卖 |
| 在线推理（稳定） | MIG（硬件隔离） | 整卡 + 多副本 | 时间片共享（尾延迟抖动） |
| 在线推理（弹性） | vGPU/共享 + 显存上限 | MIG | 无上限共享 |
| HPC/MPI | 整卡 + IB + NUMA 亲和 | RoCE | 跨域拼接 |
| 开发/实验 | vGPU/共享 | MIG 小切片 | 占用整卡长期空闲 |

### 13.11.3 调度器需要哪些设备属性

如果只上报 `nvidia.com/gpu: 8`，调度器无法区分：

- 哪两张卡在同一个 NVLink 域；
- 哪张卡和哪张网卡在同一 NUMA/PCIe 交换机下；
- 哪张卡支持 MIG、当前切了什么 profile；
- 哪张卡已被其他 Pod 共享、还剩多少显存。

这些信息要么通过节点标签（粗粒度），要么通过 device plugin 的分配协议，要么通过 DRA 的 `ResourceSlice` 属性（细粒度）暴露。**调度器只能基于被暴露的信息做决策**；没有上报的拓扑，它无法“猜”。

### 13.11.4 从 device plugin 迁移到 DRA

DRA 不是“把整数换成对象”这么简单，它改变了设备请求、分配和生命周期。迁移路线：

```text
阶段 0：盘点
  - 现有资源名（nvidia.com/gpu、volcano.sh/vgpu-*、mig-*）
  - 设备插件版本、驱动版本、Kubernetes 版本
  - 队列 capability 里用到的资源键

阶段 1：并行验证
  - 在测试节点启用 DRA driver 与 feature gate
  - 创建 DeviceClass/ResourceClaim，跑通一个 Pod
  - 确认调度器能识别 claim，队列配额能计入

阶段 2：双栈
  - 新作业用 DRA，旧作业保留 device plugin
  - 两类资源分别记账，避免同一张卡被两套账本重复分配
  - 监控 claim 的 Prepare/Unprepare 与泄漏

阶段 3：切换
  - 队列 capability 改用 deviceclass/<class> 键
  - 更新作业模板与平台 API
  - 保留回滚到 device plugin 的节点池

阶段 4：清理
  - 下线旧 device plugin（确认没有作业引用）
  - 删除旧资源名的配额与告警
```

迁移中最容易出问题的三件事：

1. **双账本**：同一张卡既被 device plugin 上报为 `nvidia.com/gpu`，又被 DRA 上报为 DeviceClass；两个调度路径可能同时分配它。
2. **配额键不一致**：Queue capability 写 `nvidia.com/gpu`，作业请求 `deviceclass/gpu.nvidia.com`，配额看起来永远是 0。
3. **claim 生命周期**：Pod 删除后 claim 未释放，设备被占住；高频抢占会放大这个问题。

### 13.11.5 一个设备选型的检查清单

- [ ] 作业是整卡还是切片？切片需要 MIG 还是 vGPU？
- [ ] 需要跨机互联吗？需要 IB 还是 RoCE？
- [ ] 需要 NUMA/PCIe 亲和吗？设备插件是否上报？
- [ ] 队列配额按哪种资源名统计？和作业请求一致吗？
- [ ] 共享 GPU 的显存/算力上限如何隔离？
- [ ] 驱逐/重启时设备如何释放？claim 会不会泄漏？
- [ ] 升级驱动/插件时，资源名和标签会不会变？

## 13.12 非 NVIDIA 加速器：Ascend、AMD 与国产 NPU

前面的讨论默认是 NVIDIA GPU。真实的 AI 平台往往同时有昇腾、AMD、寒武纪、海光、天数等加速器。Volcano 的调度模型是“资源向量 + 标签 + 插件”，本身不绑定厂商；但**资源名、集合通信库、拓扑表达和设备切分方式**会变化，配置写错时表现通常是“作业 Pending 或性能腰斩”，而不是报错。

### 13.12.1 一张对照表

| 厂商/生态 | 常见资源名（以 device plugin 上报为准） | 集合通信 | 互联/拓扑 | 切分 |
|---|---|---|---|---|
| NVIDIA | `nvidia.com/gpu`、`nvidia.com/mig-*` | NCCL | NVLink/NVSwitch、IB/RoCE | MIG、vGPU、时间片 |
| 昇腾 Ascend | `huawei.com/Ascend910`、`huawei.com/Ascend310P` 等 | HCCL | HCCS、RoCE/IB | vNPU（MindCluster/HAMi） |
| AMD | `amd.com/gpu` | RCCL | Infinity Fabric、PCIe | MIG（部分型号）/共享 |
| 寒武纪 | `cambricon.com/mlu` | CNCL/厂商库 | MLU-Link/PCIe | 厂商方案 |
| 海光 DCU | 以 `hygon.com/*` 上报为准 | 厂商 RCCL 兼容层 | 厂商互联 | 厂商方案 |
| 天数智芯 | 以 `iluvatar.com/*` 上报为准 | 厂商库 | 厂商互联 | 厂商方案 |

三条使用原则：

1. **不要照抄资源名**。不同 device plugin 版本可能把整卡、切分卡、网络设备拆成不同键；先 `kubectl describe node` 看 `allocatable`。
2. **不要只改镜像**。昇腾需要 CANN 驱动/固件、HCCL 环境、`hcclrank` 作业插件；AMD 需要 ROCm 内核模块和 RCCL。镜像对了、宿主驱动不对，Pod 仍然起不来。
3. **不要把不同厂商混进一个队列的同一个 capability 键**。`nvidia.com/gpu: 8` 不会约束昇腾；要按资源名分别写，或用 NodeGroup/节点池隔离。

### 13.12.2 昇腾：CANN、HCCL 与 vNPU

昇腾的软件栈大致是：

```text
CANN（驱动/算子/运行时）
  └── HCCL（集合通信）
        └── MindSpore/PyTorch（torch_npu）/PaddlePaddle
              └── Volcano（PodGroup/Queue/拓扑）
```

对调度器有影响的点：

- **资源名**：整卡通常是 `huawei.com/Ascend910`；具体型号/切分卡以 device plugin 或 MindCluster 上报为准。队列 `capability` 必须用同一个键。
- **rank 注入**：多机训练需要 HCCL rank/ID。Volcano 提供 `hcclrank` 作业插件（v1.13+），用法与 `pytorch` 插件类似；如果不用插件，就要自己从 Pod 序号/Service DNS 生成。
- **拓扑**：HCCS 是节点内互联，跨节点通常走 RoCE/IB；HyperNode 的 tier 语义仍然适用，但节点标签/自动发现要按昇腾环境的实际数据源配置。
- **vNPU**：MindCluster 或 HAMi 可以把一张 NPU 切成多个 vNPU。调度器看到的是切分后的资源名，队列配额要按切分后的键统计；不要用整卡数去估算 vNPU 作业的容量。
- **NUMA**：昇腾服务器通常多 socket，NPU 与 CPU/网卡的亲和会影响吞吐；`numa-aware` 只覆盖 CPU/内存维度，设备亲和仍要依赖 device plugin/DRA 暴露的属性。

一个昇腾作业的骨架（字段以你的插件版本为准）：

```yaml
spec:
  schedulerName: volcano
  queue: ascend-train
  minAvailable: 8
  plugins:
    svc: []
    hcclrank: []
  tasks:
    - name: worker
      replicas: 8
      template:
        spec:
          nodeSelector:
            accelerator: ascend-910b
          containers:
            - name: trainer
              image: ascendhub.huawei.com/ascend-pytorch:latest
              resources:
                requests:
                  huawei.com/Ascend910: "8"
                limits:
                  huawei.com/Ascend910: "8"
```

### 13.12.3 AMD 与 ROCm

AMD Instinct（MI300X 等）在 Kubernetes 上通常上报 `amd.com/gpu`，集合通信走 RCCL。调度侧与 NVIDIA 的差异主要在：

- 镜像基于 ROCm，不是 CUDA；
- `ROCR_VISIBLE_DEVICES`、`HIP_VISIBLE_DEVICES` 等环境变量由 device plugin 注入；
- RCCL 的网络配置与 NCCL 类似，但变量名/后端支持有差异；
- MIG 类切分在 AMD 产品线上不完全等价，别假设有和 NVIDIA 一样的 profile。

队列/拓扑的写法不变：按 `amd.com/gpu` 配 capability，用节点标签选型号，用 HyperNode 表达机架/交换机。

### 13.12.4 混合加速器集群的四个坑

1. **资源名与队列配额不一致**：队列写 `nvidia.com/gpu`，昇腾作业请求 `huawei.com/Ascend910`，配额永远为 0 或被绕过。
2. **镜像/驱动错配**：CUDA 镜像跑在昇腾节点、ROCm 镜像跑在 NVIDIA 节点，Pod 会 CrashLoop；用 nodeSelector/taint 强制分池。
3. **集合通信库错配**：NCCL 作业跑在无 IB 的昇腾节点、HCCL 作业跑在 NVIDIA 节点，性能或启动失败。
4. **拓扑标签缺失**：混合集群的 HyperNode 只覆盖 NVIDIA 节点，昇腾 hard 拓扑作业永远 Pending；自动发现要覆盖所有厂商节点。

### 13.12.5 选型矩阵

| 场景 | 首选 | 次选 | 不建议 |
|---|---|---|---|
| 大模型预训练（生态优先） | NVIDIA H100/H200 + IB | AMD MI300X + RoCE | 多厂商混训同一作业 |
| 国产化/信创 | 昇腾 910B/910C + HCCL | 寒武纪/海光 | 直接用 CUDA 镜像 |
| 推理（成本优先） | 昇腾/AMD/推理卡 + vNPU/MIG | 共享 GPU | 无隔离的时间片 |
| 开发/实验 | 单一厂商小池 | 共享切分卡 | 跨厂商混部 |
| 多厂商平台 | 按厂商分节点池 + 分队列 | 统一抽象层 | 一个队列混所有资源名 |

**多厂商不是“一个队列装下所有卡”**。更现实的做法是：每个厂商一个节点池、一个队列前缀、一套镜像/驱动基线；平台层用统一 API 隐藏差异，调度层保持资源名和拓扑的显式区分。

## 13.13 本章小结

- GPU 是异构资源，型号、切分、互联、NUMA 都影响调度。
- MIG/vGPU 改变资源名与账本，队列配额要跟着变。
- `deviceshare` 负责共享 GPU 的账本，但需要配套 device plugin；共享与 vGPU 模式互斥。
- DRA 用 DeviceClass/ResourceClaim 表达设备，v1.15 起纳入队列配额；claim 有 Prepare/Unprepare 生命周期，共享 claim 要去重。
- 共享 claim 要去重，部分分配模式可能未覆盖。
- 非 NVIDIA 加速器（昇腾/AMD/国产 NPU）改变资源名、集合通信库和切分方式；多厂商平台应按节点池/队列隔离，不要用一个 capability 键覆盖所有卡。
- MiniVolcano 只模拟标量资源与 NUMA，不覆盖设备管理或共享账本。

下一章讲作业生态：JobFlow、CronJob、JobTemplate、LWS、Ray、Kubeflow 等如何接入 Volcano。

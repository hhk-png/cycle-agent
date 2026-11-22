# 第 22 章 GPU、加速器与 AI 推理平台

> 本章讨论 Kubernetes 如何把 GPU、NPU、FPGA、高速网卡等“非 CPU 资源”纳入调度与生命周期管理。它是第 17 章调度、第 20 章自动伸缩和第 21 章批处理的延伸，也是理解 AI 基础设施成本与可靠性的基础。

## 22.1 为什么 GPU 不是“更大的 CPU”

CPU 是通用、可压缩、可分时复用的资源：500m 与 500m 可以共享一个核，超卖通常只带来延迟上升。GPU 不是这样：

- **它是外设**，通过 PCIe/CXL 挂在总线上，有独立的显存、驱动和固件。
- **它按整卡或固定切片分配**，传统上不能被调度器“切一半”。
- **它高度依赖拓扑**：同一块 GPU 上的 NVLink、同一 NUMA 节点、同一 PCIe switch、同一机架，性能可能差数倍。
- **它的故障模式不同**：ECC 错误、Xid、掉卡、驱动崩溃、NVLink 降级，都会让整卡不可用。
- **它很贵**：空闲 GPU 是 AI 平台最大的浪费之一。

因此 Kubernetes 对 GPU 的处理不是“给节点加一个数字”，而是要把**设备发现、分配、隔离、拓扑、健康、计费**串起来。

```text
应用/Pod
  │ 声明 nvidia.com/gpu: 1 或 ResourceClaim
  ▼
调度器 ── 过滤/打分：整卡、拓扑、NUMA、队列配额
  ▼
kubelet ── Device Plugin / DRA：Allocate、Prepare
  ▼
容器运行时 ── 注入设备节点、驱动库、环境变量
  ▼
GPU 驱动/固件 ── 真正执行 CUDA/NCCL
```

MiniK8s 在第 16 个示例中实现了这条链路的“调度账本”部分：节点声明 `nvidia.com/gpu`，调度器按整数扣减，超额 Pod 保持 Pending；设备发现与真实分配不在模拟范围内。

## 22.2 Device Plugin：事实标准

在 DRA 成熟之前，几乎所有 GPU 厂商都通过 **Device Plugin** 接入 Kubernetes。它是一个运行在节点上的 gRPC 服务，由 kubelet 管理。

### 注册与发现

```text
/var/lib/kubelet/device-plugins/
├── kubelet.sock              # kubelet 暴露的注册接口
├── nvidia-gpu.sock           # 插件自己的 Unix socket
└── ...
```

启动流程：

1. 插件启动 gRPC server，监听自己的 socket。
2. 插件调用 kubelet 的 `Register`，声明资源名（如 `nvidia.com/gpu`）和 API 版本。
3. kubelet 调用插件的 `ListAndWatch`，插件持续返回设备列表与健康状态。
4. kubelet 把设备数量写入 Node 的 `status.capacity/allocatable`。
5. Pod 调度到节点后，kubelet 调用插件的 `Allocate`，拿到环境变量、挂载、设备节点。
6. 容器运行时把这些内容注入容器。

一个简化后的插件接口：

```go
service DevicePlugin {
  rpc GetDevicePluginOptions(Empty) returns (DevicePluginOptions);
  rpc ListAndWatch(Empty) returns (stream ListAndWatchResponse);
  rpc Allocate(AllocateRequest) returns (AllocateResponse);
  rpc GetPreferredAllocation(...) returns (...);
  rpc PreStartContainer(...) returns (...);
}
```

`ListAndWatch` 返回的设备包含 ID 和健康状态。设备不健康时，kubelet 会把它从 allocatable 中扣除；这正是 GPU Xid/ECC 故障后“节点还在，但 GPU 不可调度”的机制来源。

### 扩展资源的语义

Device Plugin 暴露的资源是 **extended resource**：

- 资源名必须是 `vendor-domain/resource` 形式，如 `nvidia.com/gpu`、`amd.com/gpu`。
- 只能是**整数**，不能是 `500m`。
- **不可超卖**：Pod 的 requests 必须等于 limits；调度器按整数扣减。
- 只有 requests 没有 limits 时，Kubernetes 会自动把 limit 设为 request。
- 扩展资源同样遵守 init/主容器的 `max(主容器之和, init 最大值)` 语义，但只接受整数，且不可超卖。
- 扩展资源按 Pod 的有效请求计入调度和配额，不按容器分别“借还”。
- ResourceQuota、LimitRange 可以管理扩展资源，但语义与 CPU/内存不同。

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: trainer
spec:
  containers:
    - name: trainer
      image: example/trainer:v1
      resources:
        limits:
          nvidia.com/gpu: "1"   # 扩展资源只接受整数
```

如果节点有 8 张卡，已经调度了 7 张，新的 1 卡 Pod 仍然可以调度；2 卡 Pod 只能 Pending。调度器不需要理解 CUDA，只需要理解“整数账本”。

### 设备插件的工程细节

- **节点标签**：GPU Feature Discovery（GFD）把型号、显存、MIG 能力、驱动版本写成标签，供 nodeSelector/affinity 使用。
- **驱动生命周期**：GPU Operator 用 DaemonSet 管理驱动、container toolkit、device plugin、DCGM exporter、MIG manager。驱动升级通常需要 drain 节点。
- **特权与安全**：插件通常需要访问 `/dev`、host 路径和 kubelet socket，权限较高，必须作为受信系统组件运行。
- **健康检查**：插件应定期探测设备；不要把不健康设备继续上报。
- **多厂商共存**：不同 vendor 的资源名不同，可以同时存在；调度器把它们当作独立资源。

## 22.3 GPU 拓扑：调度器看不见的性能

即使两张卡都在同一节点，性能也可能完全不同：

```text
节点
├── NUMA 0
│   ├── CPU 0-31
│   ├── PCIe switch A
│   │   ├── GPU 0 ── NVLink ── GPU 1
│   │   └── GPU 2 ── NVLink ── GPU 3
│   └── NIC 0 (RDMA)
└── NUMA 1
    ├── CPU 32-63
    └── ...
```

关键拓扑维度：

- **NVLink/NVSwitch**：GPU 间高带宽互联；跨 NVLink 域通信会降级到 PCIe。
- **PCIe 根复合体**：同一 switch 下的卡通信更便宜。
- **NUMA**：GPU 与 CPU 内存的亲和性影响数据拷贝。
- **RDMA 网卡亲和**：多机训练时，GPU 与 IB/RoCE 网卡是否在同一 NUMA/PCIe 域，决定 NCCL 带宽。
- **MIG 切片**：一张 A100/H100 可以切成多个 GPU instance，切片之间有独立显存和计算单元。

原生调度器只看到 `nvidia.com/gpu: N`，不知道这些卡是否在同一 NVLink 域。生产方案通常组合：

1. **GFD 标签 + nodeAffinity**：把 Pod 限制到特定机型/拓扑。
2. **Topology-aware Device Plugin**：插件在 `Allocate` 时选择同一 NVLink 域的卡。
3. **拓扑感知调度器**：Volcano、Kueue、NVIDIA 的拓扑插件在调度阶段就考虑 gang 与拓扑。
4. **DRA**：把设备属性、拓扑、选择器写进 API，让调度器原生理解。

## 22.4 DRA：把设备变成一等 API 对象

Dynamic Resource Allocation（DRA）试图解决 Device Plugin 的三个结构性问题：

- 资源声明只有数量，没有“我要什么型号/属性/拓扑”。
- 分配发生在 kubelet，调度器无法参与复杂决策。
- 多设备组合、共享、动态配置表达困难。

DRA 的核心对象：

| 对象 | 作用 |
|---|---|
| `DeviceClass` | 设备类别与驱动，如 `gpu.nvidia.com` |
| `ResourceSlice` | 节点/驱动发布的设备清单与属性 |
| `ResourceClaim` | 一个工作负载对设备的具体请求 |
| `ResourceClaimTemplate` | 为每个 Pod 生成 Claim 的模板 |
| `ResourceClaim` 的 `status.allocation` | 调度器写入的分配结果 |

一个概念性示例（`resource.k8s.io/v1`，1.34 GA）：

```yaml
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: gpu-claim
spec:
  spec:
    devices:
      requests:
        - name: gpu
          exactly:
            deviceClassName: gpu.nvidia.com
            allocationMode: ExactCount
            selectors:
              - cel:
                  expression: device.attributes["gpu.nvidia.com"].model == "H100"
      config:
        - requests: ["gpu"]
          opaque:
            driver: gpu.nvidia.com
            parameters:
              apiVersion: gpu.nvidia.com/v1alpha1
              kind: GpuConfig
              sharingStrategy: TimeSlicing
```

Pod 引用 Claim：

```yaml
spec:
  resourceClaims:
    - name: gpu
      resourceClaimTemplateName: gpu-claim
  containers:
    - name: trainer
      resources:
        claims:
          - name: gpu
```

DRA 的价值：

- **属性选择**：按型号、显存、MIG 能力、拓扑选择设备。
- **调度器参与**：kube-scheduler 在绑定前完成分配，失败可重试。
- **共享与配置**：同一设备可以被多个 Claim 以受控方式共享。
- **可扩展**：厂商通过驱动和 API 表达设备语义，而不是把逻辑塞进 kubelet。

代价：

- 版本与驱动支持不齐，集群升级要确认 DRA feature gate 与 API 版本。
- 调试链路更长：Claim、Slice、Class、驱动、kubelet 都可能出错。
- 并非所有厂商/云托管服务都支持同样的 DRA 能力。

## 22.5 GPU 共享：利用率与隔离的权衡

一张 GPU 只跑一个 Pod，利用率经常低于 20%。共享方式主要有：

### Time-slicing

NVIDIA device plugin 可以把一张卡虚拟成多个副本，多个容器分时使用：

```yaml
# 概念性配置：通过 device plugin 的 sharing 配置暴露 4 个副本
sharing:
  timeSlicing:
    resources:
      - name: nvidia.com/gpu
        replicas: 4
```

优点：简单、无需 MIG、适合开发和低负载推理。
缺点：**没有显存隔离**，一个进程 OOM 可能影响同卡其他容器；延迟抖动；不适合多租户生产。

### MPS（Multi-Process Service）

多个 CUDA 进程共享同一 GPU 上下文，减少上下文切换开销。需要配置 MPS control daemon，隔离仍然弱。

### MIG（Multi-Instance GPU）

把 A100/H100 等卡切成固定切片，每个切片有独立显存、计算单元和带宽：

```text
A100 80GB
├── 1g.10gb × 7
├── 2g.20gb × 3
├── 3g.40gb × 2
└── 7g.80gb × 1
```

优点：硬件级隔离，适合多租户推理。
缺点：切片规格固定、需要特定型号、切分后不能跨切片使用、运维复杂。

### vGPU / SR-IOV

通过虚拟化层把 GPU 分配给 VM 或容器，隔离更强，但依赖厂商授权与硬件能力。

选择建议：

- 开发/实验：time-slicing。
- 多租户推理：MIG 或独立 GPU。
- 训练：整卡，避免共享带来的 NCCL/带宽干扰。
- 强隔离/合规：VM（KubeVirt）或机密计算。

## 22.6 训练基础设施：不只是“跑一个 PyTorch Job”

分布式训练对平台提出额外要求：

- **gang scheduling**：所有 worker 同时启动，避免占着 GPU 等最后一张。
- **拓扑感知**：同 NVLink 域、同机架、同 IB 交换机。
- **RDMA/InfiniBand**：多机 AllReduce 的带宽决定训练效率；需要 SR-IOV、GPUDirect、NCCL 配置。
- **高性能存储**：并行文件系统（Lustre、GPFS、JuiceFS）、对象存储、本地 NVMe 缓存。
- **故障恢复**：checkpoint 到持久存储，失败后从最近 checkpoint 恢复；否则长任务永远跑不完。
- **队列与配额**：Kueue/Volcano/YuniKorn 管理团队配额、借用、抢占。
- **可观测**：GPU 利用率、显存、温度、ECC、Xid、NVLink 带宽、NCCL 超时。

常见工作负载 API：

| API | 适用 |
|---|---|
| `Job` | 单机/简单脚本 |
| `JobSet` | 一组相关 Job，索引与失败策略 |
| Kubeflow `PyTorchJob` | PyTorch DDP/FSDP |
| `RayJob` / `RayCluster` | Ray 训练与服务 |
| `MPIJob` | HPC/MPI |
| Kueue `Workload` | 队列准入与配额 |

一个训练平台的最小闭环：

```text
提交作业 → 队列准入 → gang 调度 → 拉镜像/数据 → 初始化 NCCL
  → 训练循环 → 定期 checkpoint → 成功/失败 → 释放 GPU
```

任何一环缺失，都会表现为“GPU 被占着但训练不前进”。

## 22.7 推理服务：延迟、吞吐与成本的三角

推理与训练的优化目标不同：

- 训练追求吞吐和稳定性，可以排队。
- 推理追求 **TTFT**（首 token 延迟）、**ITL**（token 间延迟）、吞吐和成本。

主流栈：

- **KServe**：`InferenceService` 抽象 predictor/transformer/explainer，支持 scale-to-zero、canary、模型仓库。
- **vLLM**：PagedAttention、连续批处理、前缀缓存，适合 LLM 高吞吐服务。
- **Triton Inference Server**：多框架、多模型、动态批处理。
- **Ray Serve**：Python 原生组合式服务，适合复合推理链路。
- **NVIDIA NIM/Triton**：厂商优化过的推理容器。

关键工程问题：

1. **模型加载**：大模型从对象存储加载可能几分钟；需要模型缓存、预热、镜像分层。
2. **冷启动**：scale-to-zero 节省成本，但首请求延迟高；需要权衡最小副本。
3. **批处理**：连续批处理提高吞吐，但会增加单请求延迟。
4. **显存**：KV cache 占用随并发增长；OOM 会直接杀进程。
5. **多模型共享**：一张卡放多个小模型，或按 LoRA 适配器共享基座。
6. **灰度**：模型版本、提示模板、tokenizer 都要版本化；用 canary 比较质量与延迟。

## 22.8 推理扩缩容：指标比 CPU 更重要

CPU HPA 对推理服务几乎无效：GPU 利用率高不代表该扩容（可能只是 batch 大），利用率低也不代表不需要副本（可能卡在排队）。

更有效的指标：

- 队列长度/等待请求数。
- TTFT、ITL、P99 延迟。
- 每副本并发/吞吐。
- GPU 显存利用率与 KV cache 占用。
- 自定义业务指标（每秒 token、请求积压）。

工具组合：

- **HPA + Prometheus Adapter**：按自定义指标扩缩容。
- **KEDA**：按队列/事件源扩缩容，支持 scale-to-zero。
- **Knative Serving**：请求驱动、scale-to-zero、流量路由。
- **Kueue**：批式推理/离线推理的队列与配额。
- **Cluster Autoscaler/Karpenter**：节点层扩容；GPU 节点启动慢，需预留缓冲。

一个常见的分层策略：

```text
在线低延迟服务：最小副本 + HPA（延迟/并发）
离线批推理：Kueue 队列 + 作业级并发
突发流量：KEDA + 节点自动伸缩 + 预热池
```

## 22.9 可观测性与成本

GPU 可观测的最小指标集：

- `DCGM_FI_DEV_GPU_UTIL`：GPU 利用率。
- `DCGM_FI_DEV_MEM_COPY_UTIL`：显存带宽利用率。
- `DCGM_FI_DEV_FB_USED/FB_FREE`：显存使用。
- `DCGM_FI_DEV_TEMPERATURE`、`DCGM_FI_DEV_POWER_USAGE`。
- ECC 错误、Xid、NVLink 错误、降频原因。
- 训练侧：step time、吞吐、loss、NCCL 超时。
- 推理侧：TTFT、ITL、QPS、队列长度、错误率。

成本优化顺序：

1. **提高利用率**：共享、批处理、混部、合适的副本数。
2. **减少空闲**：队列、抢占、scale-to-zero、节点池伸缩。
3. **选对卡型**：小模型不必用 H100；推理可用 L4/A10。
4. **降低迁移/存储成本**：模型缓存、数据本地性。
5. **按团队计费**：命名空间/队列/标签分摊，避免“公地悲剧”。

不要只看“GPU 利用率”。一个利用率 90% 但大部分时间在等数据的训练任务，成本仍然浪费在存储和网络上。

## 22.10 安全与多租户

GPU 平台的攻击面：

- 驱动/固件漏洞可能突破容器隔离。
- device plugin 权限高，被攻破影响节点。
- time-slicing 下同卡容器可见彼此的侧信道风险。
- 模型权重、训练数据、token 是核心资产。
- 多租户共享节点时，GPU 显存/PCIe 可能成为侧信道。

加固方向：

- 训练/推理隔离到专用节点池，用 taint/toleration 与 affinity。
- 多租户优先 MIG 或独立卡；强隔离用 VM/Kata/机密容器。
- device plugin 以最小权限运行，限制 hostPath 与 socket。
- 镜像签名、SBOM、准入校验；模型文件也要签名/校验。
- 审计 GPU 分配与镜像拉取，监控异常进程与网络。
- 对模型服务做输入/输出审计与速率限制。

## 22.11 MiniK8s 的扩展资源模型

MiniK8s 不实现 Device Plugin 或 DRA，但实现了调度器对扩展资源的账本语义：

- `Kubelet.capacity` 可以包含任意 `vendor/domain` 资源。
- Node 的 `status.allocatable` 会带上这些资源。
- 调度器 `pod_requests` 累加所有容器 requests，按整数比较。
- 超额时 Pod 保持 Pending，并产生 `FailedScheduling` 事件。

启动一个带 GPU 的开发集群：

```bash
python -m mini_k8s --port 8080 --nodes 2 --node-gpus 1
```

应用示例：

```bash
python -m mini_k8s.cli apply -f examples/16-gpu-extended-resource.yaml
python -m mini_k8s.cli get pods -o wide
```

两个 GPU Pod 各占一张卡，第三个保持 Pending。这对应真实集群中“设备插件上报 1 张卡、调度器按整数分配”的行为，但**不包含**驱动、设备节点注入、MIG、DRA 和真实 CUDA 执行。

## 22.12 生产清单

- [ ] GPU 节点池用 taint 隔离，只有 GPU 工作负载能调度。
- [ ] GFD/节点标签完整：型号、显存、MIG、驱动、CUDA、拓扑。
- [ ] device plugin/DRA 驱动版本与 Kubernetes 版本兼容矩阵已确认。
- [ ] 训练作业有 gang scheduling、checkpoint、失败重试与超时。
- [ ] 推理服务有 TTFT/ITL/队列长度指标与基于指标的扩缩容。
- [ ] GPU 健康监控覆盖 Xid、ECC、温度、降频、NVLink。
- [ ] 多租户策略明确：整卡、MIG、time-slicing 或 VM。
- [ ] GPU 配额按团队/队列设置，并有成本分摊。
- [ ] 模型与镜像供应链有签名/扫描/准入。
- [ ] 节点驱动升级有 drain、回滚与维护窗口。

## 22.13 LLM 推理平台：KV cache、PD 分离与容量模型

大语言模型推理与“一张卡跑一个模型”的传统推理不同：请求是自回归生成的，显存被 KV cache 占用，延迟由首 token 和逐 token 两段组成，吞吐随批大小变化。Kubernetes 的调度单位仍然是 Pod，但平台必须理解这些内部指标，否则扩容只会增加成本而不改善体验。

### 22.13.1 两个阶段：Prefill 与 Decode

一次生成请求分两段：

1. **Prefill**：把整个 prompt 送入模型，计算 KV cache。计算密集，决定 **TTFT**（Time To First Token）。
2. **Decode**：逐 token 生成，每步只处理一个新 token，但要把历史 KV cache 读一遍。显存带宽密集，决定 **ITL**（Inter-Token Latency）和吞吐。

工程含义：

- 长 prompt 会拉高 TTFT，并占用大量 KV cache。
- 长输出会拉长 Decode，降低并发容量。
- Prefill 与 Decode 对 GPU 资源的偏好不同：前者吃算力，后者吃显存带宽。
- 把两者放在同一个 Pod 里会互相干扰；分离部署（PD disaggregation）成为大规模平台的选项。

### 22.13.2 连续批处理与 PagedAttention

传统静态批处理要等一批请求凑齐，短请求被长请求拖住。连续批处理（continuous batching）在每个 decode step 动态加入/移除请求，显著提高吞吐。

PagedAttention（vLLM 等）把 KV cache 分成固定大小的 block，像虚拟内存一样按需分配，减少碎片和预留浪费。它带来几个平台可见的效应：

- 显存利用率提高，同样一张卡能服务更多并发。
- KV cache 成为可观测资源：block 使用率、eviction、preemption。
- 请求可能因显存不足被抢占/重排队，表现为延迟抖动。

平台指标不应只看 GPU utilization，还要看：

- 运行中请求数、排队请求数。
- KV cache 使用率与 block 碎片。
- Prefill/Decode 队列长度。
- TTFT、ITL、端到端延迟的 p50/p95/p99。
- 每副本 token/s、每 token 成本。

### 22.13.3 PD 分离

PD 分离把 Prefill 和 Decode 放到不同进程/节点池：

```text
请求 → 路由/调度 → Prefill worker（算 KV）
                         │ 传输 KV cache
                         ▼
                    Decode worker（逐 token 生成）
```

优点：

- 两类负载独立扩缩容，避免长 prompt 阻塞 decode。
- Prefill 节点可以用高算力卡，Decode 节点用大显存/高带宽卡。
- 故障隔离更好。

代价：

- KV cache 传输需要高带宽（NVLink、RDMA、高速网络）。
- 调度器要感知两段资源与拓扑。
- 失败恢复更复杂：Prefill 完成但 Decode 失败要重算或缓存复用。

常见实现思路：vLLM 的 disaggregated prefill、NVIDIA Dynamo、Mooncake 等。采用前确认框架版本、网络和运维复杂度；对中小规模，单池连续批处理通常更简单。

### 22.13.4 用 DRA 表达模型与设备需求

Device Plugin 只能表达“需要 1 张 GPU”。LLM 平台常需要：

- 特定型号/显存（A100 80GB、H100、L40S）。
- 特定拓扑（同 NVLink 域、同机多卡）。
- 共享/分区（MIG、time-slicing）。
- 模型缓存/本地 SSD 的附着。

DRA 用 `ResourceClaim`/`DeviceClass` 表达这些属性，调度器按 claim 分配，而不是只扣减整数。示意：

```yaml
apiVersion: resource.k8s.io/v1
kind: ResourceClaim
metadata:
  name: llm-gpu
spec:
  devices:
    requests:
      - name: gpu
        exactly:
          deviceClassName: nvidia-h100
          count: 1
```

平台侧要把“模型需要什么设备”变成 claim 模板，而不是在 Pod 里硬编码 `nvidia.com/gpu: 1`。

### 22.13.5 部署拓扑

常见三种形态：

| 形态 | 适用 | 特点 |
|---|---|---|
| 单 Pod 单模型 | 中小规模、模型隔离 | 简单，扩缩容粒度高，GPU 利用率可能低 |
| 单 Pod 多副本/多模型 | 小模型、LoRA 适配器 | 提高共享，隔离与路由复杂 |
| PD 分离 + 路由层 | 大规模、长 prompt/高并发 | 吞吐高，运维复杂 |

路由层（Gateway/推理网关）要支持：

- 按模型/版本/租户路由。
- 感知副本队列长度，而不是简单轮询。
- 超时、重试、熔断、限流。
- 灰度与 A/B（模型版本、提示模板、tokenizer 必须一起版本化）。
- 请求/响应审计与脱敏。

### 22.13.6 扩缩容：指标与冷启动

GPU 推理的扩缩容不能用 CPU HPA：

- GPU 利用率高可能是 batch 大，不一定要扩容。
- 利用率低可能是卡在排队或 KV cache，扩容也未必有用。
- 模型加载可能几分钟，扩容跟不上突发流量。

推荐指标：

- 排队请求数/队列等待时间。
- TTFT/ITL 的 p95/p99。
- 每副本并发/KV cache 使用率。
- 每秒 token 数/请求数。

策略：

- 保留最小副本 + 预热池，避免冷启动。
- 用 KEDA/HPA + Prometheus Adapter 按队列/延迟扩缩。
- 节点层用 Karpenter/Cluster Autoscaler，GPU 节点启动慢，预留缓冲。
- 离线批推理走 Kueue 队列，不与在线服务争抢。
- 缩容要等请求排空，设置 `terminationGracePeriodSeconds` 与 preStop。

### 22.13.7 容量模型：从 token 到卡数

一个简化的容量估算：

```text
每卡可服务并发 ≈ 该卡可用于 KV cache 的显存 / 每请求 KV cache
每请求 KV cache ≈ 2 × 层数 × n_kv_heads × head_dim × 序列长度 × 精度字节 ÷ TP
吞吐(tokens/s) ≈ 并发 × 每步 token 数 / 每步耗时
所需副本 ≈ 峰值 token 速率 / 每副本可持续 token 速率
```

要点：

- `n_kv_heads × head_dim` 才是每个 token 每层的 KV 宽度。MHA 时它等于 `hidden_size`；GQA/MQA 时小得多。用 `hidden_size` 代入会**高估** GQA 模型的 KV cache（Llama 3 8B 约 4 倍，70B 级可能 8 倍），进而高估卡数。
- tensor parallel 会把每层的 KV 头切到多张卡上，所以按 TP 除；pipeline parallel 不切 KV 头，不能按 PP 除。
- 可用 KV 显存 = 卡显存 − 模型权重 − 激活/临时缓冲 − PagedAttention block 碎片/预留。不要拿整卡显存做分母。

一个 worked example（Llama 3 8B，FP16，8K context，单卡 80GB）：

```text
每请求 KV = 2 × 32 层 × 8 KV heads × 128 head_dim × 8192 × 2 B
          = 1 GiB
模型权重 ≈ 16 GB；激活/框架预留 ≈ 8–12 GB
可用 KV ≈ 80 − 16 − 10 ≈ 54 GB
理论上限 ≈ 54 个并发请求（还要留 block slack，实际按 45–50 估）
```

这只是“显存能装下”的上限，不等于“延迟达标”的并发。TTFT/ITL 的拐点要用压测找；长 prompt 还会让 Prefill 抢占 Decode 的算力。实际值要用压测得到。压测至少覆盖：

- 短 prompt/短输出、长 prompt/短输出、长输出。
- 并发从低到高，记录 TTFT/ITL 拐点。
- KV cache 打满时的排队/抢占行为。
- 模型加载/冷启动时间。
- 故障注入：杀副本、GPU Xid、网络抖动。

成本指标：每百万 token 成本、每请求成本、GPU 小时利用率。只看 GPU 利用率会误导：高利用率可能只是长请求排队。

### 22.13.8 常见故障

1. **OOM**：KV cache 超过显存；降低最大并发/序列长度，或增加副本。
2. **TTFT 飙升**：长 prompt 或 Prefill 排队；考虑 PD 分离/前缀缓存。
3. **ITL 抖动**：Decode 批次过大或显存带宽争用。
4. **扩容无效**：瓶颈在路由/队列/模型加载，不在副本数。
5. **模型版本不一致**：滚动更新时新旧副本 tokenizer/模板不同，输出不可比。
6. **GPU 掉卡**：Xid/ECC 错误；节点隔离与自动替换。
7. **请求堆积**：限流/超时/熔断缺失，故障扩散。

### 22.13.9 MiniK8s 的边界

MiniK8s 只模拟扩展资源的整数账本，不理解 KV cache、Prefill/Decode、DRA 或模型加载。它适合验证“GPU Pod 如何被调度、配额如何扣减”，不适合验证推理性能。真实平台应在 GPU 集群上做小规模压测，再用容量模型外推。

## 本章小结

- GPU 是外设而非可压缩 CPU，Kubernetes 通过扩展资源、Device Plugin、DRA 和拓扑感知来管理它。
- Device Plugin 仍是事实标准；DRA 把设备选择、共享和调度推进到 API 层。
- 拓扑、NUMA、NVLink、RDMA 决定训练性能，原生调度器需要标签、插件或 DRA 辅助。
- 推理平台的核心是延迟/吞吐/成本三角，扩缩容应基于队列和延迟而非 CPU。
- LLM 推理要理解 Prefill/Decode、KV cache、连续批处理与 PD 分离；容量按 token 速率和显存估算，而不是只看 GPU 利用率。
- MiniK8s 只模拟扩展资源账本；真实 GPU 执行、驱动、MIG 与 DRA 需在真实集群验证。

## 延伸阅读

- [第 17 章 调度器](17-scheduler.md)
- [第 20 章 自动伸缩](20-autoscaling.md)
- [第 21 章 批处理与队列](21-batch-and-queues.md)
- [第 30 章 性能与规模化](30-performance-scale.md)
- [第 44 章 商业分析 II：托管 Kubernetes 的经济学](44-business-economics.md)

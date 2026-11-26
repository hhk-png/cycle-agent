# 第 34 章 大模型工作负载：训练、推理与 Agent 的调度工程

前 33 章讲的是“Volcano 怎么工作、怎么扩展、怎么运营”。这一章换一个视角：**把 Volcano 放到真实的大模型工作负载下面，看它到底在承托什么。**

同样是一个 PodGroup，预训练、微调、在线推理、Agent 沙箱对调度器的要求并不相同：

- 预训练要的是“64 个 Pod 一起开始、尽量落在同一个 spine、被打断能从 checkpoint 恢复”；
- 微调要的是“8 张卡、几小时内完成、可以被抢占但别丢进度”；
- 在线推理要的是“副本别一起挂、PDB 别被击穿、扩容别和 gang 打架”；
- Agent 要的是“几秒内起沙箱、海量短任务、彼此隔离”。

本章不重复 gang、拓扑、PDB 的机制，而是回答四个工程问题：

1. 一个 LLM 作业的“形状”如何映射成 `replicas`、`minAvailable`、`tasks` 和拓扑约束？
2. 训练、推理、Agent 各自应该用哪套队列、优先级、中断策略？
3. 容量和性能怎么估算，才不会被“卡数”这个单一数字骗到？
4. 哪些语义可以用 MiniVolcano 验证，哪些必须上真实集群？

> 本章的性能公式和容量数字是**工程估算模型**，不是 benchmark 承诺。GPU 型号、框架版本、模型结构、网络和存储都会改变结果；上线前用自己的负载压测。

## 34.1 三类负载，三种调度问题

| 维度 | 预训练 | 微调 / 后训练 | 在线推理 | Agent |
|---|---|---|---|---|
| 典型时长 | 天–周 | 小时–天 | 7×24 持续 | 秒–分钟 |
| 调度单位 | 大 gang（64–1024+ 卡） | 中 gang（8–64 卡）或单机 | 副本组 / 单 Pod | 单个沙箱 Pod |
| gang 强度 | 极强，缺一卡就停 | 强，但可 checkpoint | 弱，副本可独立 | 无 |
| 拓扑敏感度 | 极高（NVLink/IB/spine） | 中（节点内 NVLink 为主） | 低–中（KV cache 有亲和） | 低 |
| 中断代价 | 极高（回滚到 checkpoint） | 中（可恢复） | 高（丢请求/破 SLO） | 低–中（可重试） |
| 核心指标 | MFU、goodput、完成时间 | 完成时间、卡时成本 | TTFT/TPOT、tokens/s、P99 | 冷启动、成功率、隔离 |
| 典型队列 | `llm-train` | `llm-finetune` | `llm-infer` | `llm-agent` |
| 推荐中断策略 | 严格 gang + RestartJob + checkpoint | 弹性/可抢占 + checkpoint | PDB + 禁止随意驱逐 | 可重试、低优先级或独立池 |

三类负载混在一个队列里，通常会出现两种结果：训练把推理的延迟打爆，或者推理的 PDB 让训练永远凑不齐 gang。**先分池、再分队列、最后才谈权重**，是 LLM 平台比普通批处理平台更需要强调的顺序。

## 34.2 并行策略如何变成 Pod

### 34.2.1 五种并行

大模型训练不是“一个模型一个进程”，而是把模型和计算切到多个 rank 上：

| 并行 | 切什么 | 通信 | 通常放哪 |
|---|---|---|---|
| 数据并行（DP） | 切 batch，每个 rank 一份模型副本 | 梯度 all-reduce | 跨节点、跨 rail |
| 张量并行（TP） | 切单层矩阵 | 每层 all-reduce，延迟敏感 | 节点内 NVLink/NVSwitch |
| 流水并行（PP） | 切层，形成 stage | stage 间 p2p | 跨节点，尽量同 spine |
| 专家并行（EP） | 切 MoE 专家 | all-to-all | 跨节点，高带宽 |
| 上下文并行（CP/SP） | 切序列/激活 | attention 通信 | 跨节点，常与 TP 组合 |

一个常用的 rank 布局是“TP 最内、PP 次之、DP 最外”：

```text
world_size = DP × PP × TP × CP
rank = ((dp * PP + pp) * TP + tp) * CP + cp
```

框架的具体 rank 映射可能不同，但**通信越密集的维度应该放得越近**这条原则不变：

- TP 的 all-reduce 每层都发生，必须放在同一节点的 NVLink 域；
- PP 的 p2p 只在 stage 边界发生，可以跨节点；
- DP 的梯度 all-reduce 频率低于 TP，但数据量大，应尽量落在同一 leaf/spine，避免跨 spine；
- EP 的 all-to-all 对网络最敏感，通常要求 rail-optimized 网络和同 spine 放置。

### 34.2.2 网络层次与 HyperNode 的对应

| 层次 | 典型技术 | 带宽量级 | HyperNode tier 建议 |
|---|---|---|---|
| 卡内 | HBM/L2 | TB/s 级 | 不建模 |
| 节点内 | NVLink/NVSwitch、PCIe | 数百 GB/s（NVLink） | tier 1（叶子/节点） |
| 机架内 | leaf 交换机、NDR IB | 数百 Gb/s 每口 | tier 2（rack/leaf） |
| 集群 | spine、核心交换机 | 取决于收敛比 | tier 3（spine/cluster） |

HyperNode 的 tier 越小表示域越近。一个 64 卡作业如果 `highestTierAllowed=2`，就必须整体落在一个 rack 内；如果 rack 只有 4 台 8 卡节点，就装不下，作业会一直 Pending。这时要么放宽到 tier 3，要么把作业拆小，要么扩容 rack。

### 34.2.3 Pod 粒度：每节点一个还是每卡一个

同一个 64 卡作业，有两种常见建模：

| 方式 | 写法 | 优点 | 缺点 |
|---|---|---|---|
| 每节点一个 Pod | `replicas=8`，每个 Pod 请求 8 GPU | Pod 少、DNS 稳定、NCCL 简单、拓扑约束少 | 调度粒度粗；一个 Pod 占满节点 |
| 每卡一个 Pod | `replicas=64`，每个 Pod 请求 1 GPU | 调度粒度细、易与队列/拓扑插件交互 | Pod 多、服务发现复杂、启动开销大 |

大模型预训练通常选**每节点一个 Pod**：8 卡节点上一个 Pod 跑 8 个 rank，`torchrun --nproc_per_node=8` 拉起进程；Volcano 只需要保证 8 个 Pod 一起启动。微调/推理更常见“每卡一个 Pod”或“每副本一个 Pod”，因为需要更细的共享和扩缩。

选型时问三个问题：

1. 框架是否依赖稳定的 Pod DNS/主机名？（MPI、NCCL bootstrap 通常需要）
2. 是否需要把不同角色的 Pod 放到不同节点？（ps/worker、prefill/decode）
3. 是否要按单卡粒度做队列配额和共享？（MIG/vGPU 通常需要）

### 34.2.4 一个 64 卡的例子

假设 8 台节点，每台 8 张 H100，NVLink 域是节点内，rack 有 4 台，spine 下有 2 个 rack：

```text
TP=8：每个 TP 组正好落在一台节点内
PP=2：两个 stage，跨节点
DP=4：四份流水线副本
world_size = 8 × 2 × 4 = 64
```

映射到 Volcano：

```yaml
minAvailable: 8          # 8 个 Pod（每 Pod 8 卡）一起启动
tasks:
  - name: worker
    replicas: 8
networkTopology:
  mode: hard
  highestTierAllowed: 3  # 允许跨 rack，但仍在同一 spine
```

如果 `highestTierAllowed=2`，这个作业就只能落在单个 rack 的 4 台节点上，最多 32 卡，永远凑不齐。**拓扑约束不是越紧越好，它必须和作业的世界大小匹配。**

## 34.3 gang 尺寸：minAvailable 怎么定

### 34.3.1 严格 gang：世界大小就是门槛

对不容忍部分启动的预训练：

```text
minAvailable = replicas = 节点数（每节点一个 Pod）
```

任何一卡不可用，整个作业不启动。这是最安全、也最容易“永远等”的配置。它适合：

- 没有 checkpoint 或 checkpoint 很贵；
- 框架不支持动态成员；
- 作业有明确完成时限，宁可等也不要跑一半。

### 34.3.2 弹性 gang：最小可用世界大小

如果框架支持弹性（PyTorch Elastic、TorchFT、Ray Train），可以设：

```text
minAvailable < replicas
```

但 `minAvailable` 不是随便填的，它必须满足并行约束。注意单位：`minAvailable` 是 **Pod 数**，世界大小是：

```text
world_size = minAvailable × 每个 Pod 的 GPU 数
world_size 必须能被 TP × PP × CP 整除
```

以每节点一个 Pod、每 Pod 8 卡、`TP=8, PP=2` 为例，世界大小必须是 16 的倍数，因此 `minAvailable` 必须是偶数：6 个 Pod（48 卡）可以，7 个 Pod（56 卡）不行。填错会导致框架启动后 rank 分配失败，而不是调度器报错。

弹性 gang 的代价：

- 作业会“部分 Running”，排障时不要误判为 gang 失效；
- 框架必须处理成员变化、重新分片、checkpoint 兼容；
- 队列账本会在扩缩时波动，监控要能区分“扩缩”与“抖动”。

多角色作业（RLHF 的 actor/reference/reward、推理的 prefill/decode）还要加 **task 级 `minAvailable`**：作业级 `minAvailable` 只保证总数，不保证每个角色都启动。例如 `actor×10 + reward×2`，作业 `minAvailable=10` 可能满足为 10 个 actor、0 个 reward；把每个 task 的 `minAvailable` 写成 `actor:8 / reward:2`，并保持作业门槛 ≥ 10（此例正好 10），才会按角色检查。规则与陷阱见 4.2.4 与 6.2.2。

### 34.3.3 拓扑域容量

一个 gang 能否放下，取决于三个容量同时满足：

```text
集群空闲 ≥ world_size
目标域空闲 ≥ world_size
每个节点能放下该 Pod 的形状（例如 8 GPU）
```

第三个条件最容易被忽略。一个 rack 有 8 台节点、每台只剩 4 张空闲卡，总空闲 32 卡，但一个需要 8 卡 Pod 的作业一张也放不下。**容量规划要按“最大连续空闲形状”统计，而不是只加总空闲卡数。**

### 34.3.4 常见世界大小与放置

| 世界大小 | 典型并行 | 节点数（8 卡/节点） | 拓扑建议 |
|---:|---|---:|---|
| 8 | TP=8 | 1 | 单节点，无跨机 |
| 16 | TP=8, PP=2 | 2 | 同 leaf/同 rack |
| 32 | TP=8, PP=2, DP=2 | 4 | 同 rack |
| 64 | TP=8, PP=2, DP=4 | 8 | 同 spine，允许跨 rack |
| 128 | TP=8, PP=4, DP=4 | 16 | 同 spine，rail 对齐 |
| 512 | TP=8, PP=8, DP=8 | 64 | 多 spine，需网络规划 |
| 1024+ | 多级并行/分区 | 128+ | 多集群或专用 fabric |

这张表是“常见起点”，不是硬规则。真正的约束来自模型结构、显存、batch size 和网络收敛比。

## 34.4 预训练 Job：从 YAML 到启动

`examples/jobs/vcjob-llm-pretrain.yaml` 给出一个 64 卡、8 Pod 的预训练作业。关键字段逐条解释：

```yaml
spec:
  schedulerName: volcano
  queue: llm-train
  priorityClassName: high-priority
  minAvailable: 8
  maxRetry: 3
  plugins:
    ssh: []
    svc: []
    pytorch: ["--master=master", "--worker=worker"]
  policies:
    - event: PodEvicted
      action: RestartJob
    - event: PodFailed
      action: RestartJob
  networkTopology:
    mode: hard
    highestTierAllowed: 3
```

- `minAvailable: 8`：8 个 Pod 必须一起启动；这是严格 gang。
- `policies`：被抢占或失败后整体重启，避免部分 worker 继续跑出不一致的梯度。
- `networkTopology`：整个 gang 落在同一个 tier≤3 的域；如果集群没有 HyperNode 数据，hard 模式会让作业永远 Pending。
- `plugins`：`svc` 提供 headless Service/DNS，`pytorch` 注入 `MASTER_ADDR`/`RANK`/`WORLD_SIZE`，`ssh` 便于 MPI/自定义 launcher。

容器侧还有四个容易漏掉的点：

1. **`/dev/shm`**：PyTorch DataLoader、NCCL 共享内存需要足够的 shared memory；默认 64MiB 经常不够，挂一个 `emptyDir: {medium: Memory, sizeLimit: 64Gi}`。
2. **`requests == limits`**：扩展资源（GPU）必须相等，否则 Pod 被 API Server 拒绝。
3. **NCCL 环境变量**：`NCCL_IB_HCA`、`NCCL_SOCKET_IFNAME`、`NCCL_DEBUG` 要按集群的网卡名设置；写错会静默退化到 TCP。
4. **RDMA 设备**：多机训练需要把 IB 设备暴露给容器（device plugin、SR-IOV，或在受控环境下 hostNetwork + 设备挂载），否则 NCCL 走 TCP，性能可能差一个数量级。

一个最小的启动命令：

```bash
torchrun \
  --nnodes=8 \
  --nproc_per_node=8 \
  --node_rank=${RANK} \
  --master_addr=${MASTER_ADDR} \
  --master_port=29500 \
  train_pretrain.py \
  --parallel tp=8 pp=2 dp=4
```

`RANK`/`MASTER_ADDR` 由作业插件注入；如果你不用 `pytorch` 插件，就要自己通过 Service DNS 或 ConfigMap 生成。**不要假设环境变量一定存在**，启动脚本先打印并校验它们。

## 34.5 微调、LoRA 与 RLHF

### 34.5.1 SFT / LoRA

微调的形态比预训练多：

| 方式 | 显存 | 典型卡数 | gang | 抢占 |
|---|---|---:|---|---|
| 全参 SFT | 高（优化器状态 + 激活） | 8–64 | 强 | 可，需 checkpoint |
| LoRA/QLoRA | 低 | 1–8 | 弱/无 | 可，重启便宜 |
| 长上下文微调 | 很高（激活） | 8–32 | 强 | 谨慎 |

`examples/jobs/vcjob-llm-finetune.yaml` 用 4 个 Pod × 2 卡、`minAvailable: 4`，适合 8 卡全参微调。LoRA 可以退化成单 Pod、`minAvailable: 1`，甚至不用 Volcano Job，用普通 Kubernetes Job + PodGroup 即可。

微调作业的调度建议：

- 队列 `llm-finetune` 的 `guarantee` 不必很大，但 `capability` 要能吸收突发；
- 允许被 `llm-train` 回收，但 checkpoint 间隔要短（例如 15–30 分钟）；
- `maxRetry` 不要太小，否则一次抢占就 Failed；也不要太大，坏作业会反复重启；
- 对时限敏感的微调可以用 SLA 注解，但不要把它当硬保证。

### 34.5.2 RLHF / GRPO 的多角色

后训练常常同时有 actor、reference、reward、critic 等角色，资源形状不同：

```text
actor      8 卡（训练）
reference  8 卡（只推理，可量化/共享）
reward     1–2 卡（小模型）
critic     8 卡（可选）
```

三种建模方式：

1. **一个 Volcano Job 多个 task**：`actor`/`reference`/`reward` 各一个 task，`minAvailable` 等于总副本。gang 保证所有角色一起启动，但资源形状差异大时容易碎片化。
2. **多个 Job + JobFlow**：按依赖编排，角色之间通过对象存储/共享 PVC 交换；gang 粒度更细，但跨 Job 没有原子性。
3. **共置（colocation）**：把小角色和 actor 放同一节点，用 MIG/vGPU 或分时复用；节省卡，但隔离和显存争抢要自己处理。

如果角色之间必须同时在线（例如 on-policy RL），优先方式 1；如果角色可以串行或异步，方式 2 更灵活。

## 34.6 框架与 operator：谁创建 Pod

| 框架/operator | 生命周期归谁 | gang 怎么表达 | 与 Volcano 的集成 | 适用 |
|---|---|---|---|---|
| Volcano Job | Volcano controller | `minAvailable` + tasks | 原生 | 自研训练脚本、MPI、Megatron/DeepSpeed launcher |
| Kubeflow Trainer | Trainer operator | PyTorchJob/TFJob + PodGroup | `schedulerName: volcano` + PodGroup | 标准 PyTorch/TF 训练 |
| TorchFT / 弹性训练 | 框架/自研 controller | 成员变更 + checkpoint | 通常用 PodGroup + 自定义 controller | 容错/弹性预训练 |
| Megatron-LM / DeepSpeed | 启动脚本 | 外部 launcher + torchrun | Volcano Job 包一层 | 大模型预训练 |
| Ray Train / KubeRay | KubeRay operator | RayCluster + PodGroup | `schedulerName: volcano` | Ray 生态 |
| LWS | LWS controller | LeaderWorkerSet + PodGroup | 原生集成（版本相关） | 多机推理 |

一条硬规则：**一个作业只能有一个“Pod 所有者”。** 如果 Kubeflow Trainer 已经创建 Pod，就不要再让 Volcano Job 为同一个作业创建 Pod；否则会出现重复 Pod、重复 PodGroup、状态互相覆盖。正确做法是让 operator 创建 Pod，Volcano 只负责调度（`schedulerName` + PodGroup 注解）。

另一条：**不要让框架和 Volcano 同时做 gang。** 如果 operator 已经保证“所有 worker 一起创建”，Volcano 再把 `minAvailable` 设成总副本，通常没问题；但如果 operator 是“按需创建/动态扩缩”，严格 gang 会让它卡死。

## 34.7 checkpoint、重启与弹性

### 34.7.1 checkpoint 的三个层次

| 存储 | 速度 |  durability | 适用 |
|---|---|---|---|
| 节点本地 NVMe | 最快 | 节点故障即丢 | 高频 checkpoint + 异步上传 |
| 共享 PVC（RWX） | 中 | 取决于后端 | 中小规模、同区域 |
| 对象存储（S3/OSS） | 慢 | 高 | 跨集群/长期保存 |

训练作业至少要有两层：本地高频、远端低频。只写本地，节点一挂就全丢；只写远端，checkpoint 时间会拖垮 MFU。

### 34.7.2 重启策略

严格训练：

```yaml
policies:
  - event: PodEvicted
    action: RestartJob
  - event: PodFailed
    action: RestartJob
maxRetry: 3
```

弹性训练：

```yaml
minAvailable: 6       # 8 个副本里至少 6 个在跑
# 不配置 PodEvicted -> RestartJob；被驱逐的 Pod 保持 Pending，
# 框架在成员变化后继续训练
```

两种策略的区别不是“哪个更先进”，而是**你是否接受世界大小变化**。如果不接受，严格 gang + RestartJob；如果接受，弹性 gang + 框架层容错。

### 34.7.3 避免重启风暴

一个常见的死循环：

```text
低优先级预训练被高优先级作业抢占
  → PodEvicted -> RestartJob
  → 重新入队，又凑不齐
  → 再被抢占
  → maxRetry 耗尽，Failed
```

缓解：

- 给预训练队列足够的 `guarantee`，不要让它长期处于“可被随意回收”的状态；
- 对可抢占的训练使用弹性 gang + checkpoint，而不是严格 gang + 无限重试；
- 设置合理的 `maxRetry` 和重启退避（控制器/框架层）；
- 监控 `Evicted`/`RestartJob` 的速率，超过阈值就告警。

`examples/jobs/vcjob-torchft-elastic.yaml` 给出一个 8 副本、`minAvailable: 6` 的弹性作业骨架；真正的成员变更由 TorchFT/PyTorch Elastic 处理，Volcano 只负责“至少 6 个一起跑”。

## 34.8 推理：副本、路由与扩缩

### 34.8.1 三层调度

```text
第一层：副本放置（Volcano）
  一个 serving 组需要几张卡、是否同超节点、是否跨故障域
第二层：请求路由（网关/KServe/Kthena）
  请求去哪个副本、KV cache 是否命中、是否需要排队
第三层：扩缩容（HPA/KEDA/自定义）
  副本数、模型实例数、GPU 共享比例
```

Volcano 的强项在第一层。把三层混在一个控制器里，会出现“调度器不理解 token，网关不理解节点”的割裂。

### 34.8.2 单机推理与多机推理

| 形态 | 建模 | gang | 典型组件 |
|---|---|---|---|
| 单机多卡 | 一个 Pod 请求 N 张 GPU | 可选（单 Pod 天然原子） | vLLM/SGLang + TP |
| 多机多卡 | leader + worker 多个 Pod | 需要 | LWS、Ray、vLLM 多节点 |
| 多副本 | N 个独立 Pod/Deployment | 通常不要严格 gang | HPA/KEDA |

单机推理不需要 gang：一个 Pod 要么能放下，要么放不下。多机推理需要 gang，否则 leader 起来了、worker 没起来，服务不可用。

### 34.8.3 为什么推理通常不该用严格 gang

Deployment 的滚动更新会“先起新 Pod、再停旧 Pod”。如果 PodGroup 的 `minMember` 等于副本数，滚动更新期间新旧 Pod 同时存在，调度器会认为组超员或资源不够，更新可能卡住。

三种处理：

1. 不给独立副本用 gang，只用 PodGroup 做队列/优先级归属（`minMember: 1`）；
2. 用 LWS 的 leader/worker 语义，让控制器处理滚动；
3. 用 `maxSurge: 0` + `maxUnavailable: 1`，接受更新期间少一个副本，并让 PDB 保护最小可用数。

`examples/jobs/vcjob-vllm-serving.yaml` 用一个 2 Pod 的 serving 组演示 gang + PDB；生产上如果是独立副本的 Deployment，应把 `minMember` 降到 1，或改用 LWS。

### 34.8.4 PDB、反亲和与 KV cache

- **PDB**：给在线副本设 `minAvailable`，防止抢占/回收/shuffle 一次打掉太多；但 `minAvailable` 不要等于副本数，否则任何自愿中断都被禁止，节点无法 drain。
- **组间反亲和**：多组 serving 副本用 `group-topology-affinity` 分散到不同超节点/机架，避免单机架故障同时打掉所有副本。
- **KV cache 亲和**：请求路由层希望相同会话落到同一副本；这是网关/推理层的职责，Volcano 只能通过节点标签和副本放置提供粗粒度支持。
- **P/D 分离**：prefill 和 decode 的资源形状不同（prefill 计算密集、decode 显存带宽密集），通常分成两个 Deployment/队列，通过高速网络传 KV cache；调度上要保证 P 和 D 的网络距离。

## 34.9 Agent：短任务、沙箱与低延迟

Agent 工作负载与训练几乎相反：

- 任务短（秒级），提交频繁；
- 需要沙箱/隔离（gVisor/Kata/独立节点池）；
- 对冷启动敏感（镜像、依赖、模型加载）；
- 可能访问外部工具/网络，安全边界更重要；
- 单个任务失败可重试，不需要 gang。

调度建议：

1. **独立队列/节点池**：不要让 Agent 沙箱和预训练共享同一个队列；Agent 的突发会打乱训练的 gang 排队。
2. **不要用严格 gang**：每个沙箱是独立任务，`minAvailable` 大于 1 只会让它们互相等待。
3. **优先级与配额**：Agent 可以高优先级（延迟敏感），但 `capability` 要限制，避免吃光训练保底。
4. **冷启动优化**：镜像预热、节点本地缓存、快照恢复；调度器只能决定“去哪”，不能决定“多快启动”。
5. **安全隔离**：NetworkPolicy 默认拒绝、只允许必要的工具出口；沙箱用 RuntimeClass；不要把 Agent 放进特权命名空间。

Volcano 的 Agent Scheduler（v1.14 Alpha）面向这类低延迟、海量短任务，与批调度器通过分片协调节点。生产采用前要确认版本成熟度和隔离方案。

## 34.10 容量与性能模型

### 34.10.1 训练显存

一个粗略的分解（以混合精度 + Adam 为例）：

```text
权重（bf16）        2 bytes/param
梯度（bf16）        2 bytes/param
fp32 master 权重    4 bytes/param
Adam m/v            8 bytes/param
总计                约 16 bytes/param（不含激活）
```

70B 模型仅模型状态就约 `70e9 × 16 ≈ 1.12 TB`；ZeRO-3/FSDP 把它切到所有 rank 上，64 卡时每卡约 17.5GB，剩下的显存留给激活、KV cache 和通信缓冲。激活显存与 batch、序列长度、层数、是否重计算强相关，必须用框架的 memory estimator 或小规模实测。

LoRA 只训练少量参数，优化器状态大幅减少，但基础权重和激活仍在；不要用“LoRA 很省”来低估长上下文微调的显存。

### 34.10.2 训练算力与时间

```text
训练 FLOPs ≈ 6 × 参数量 × token 数
训练时间 ≈ FLOPs / (GPU 数 × 单卡峰值 × MFU)
```

例：70B 模型、1T token、512 张 H100（bf16 峰值约 989 TFLOPS）、MFU 40%：

```text
FLOPs = 6 × 70e9 × 1e12 = 4.2e23
有效算力 = 512 × 989e12 × 0.4 ≈ 2.02e17 FLOP/s
时间 ≈ 4.2e23 / 2.02e17 ≈ 2.08e6 s ≈ 24 天
```

MFU 是端到端有效算力占比，受并行策略、通信、气泡、checkpoint、数据加载影响。**卡数翻倍不等于时间减半**：通信、气泡和故障率都会吃掉收益。

### 34.10.3 推理显存与吞吐

KV cache 的粗略公式：

```text
KV bytes = 2 × 层数 × KV 头数 × head_dim × 序列长度 × 并发序列数 × 每元素字节数
```

以 Llama-3 70B（80 层、8 个 KV 头、head_dim 128、bf16）为例，单条 4096 token 的 KV cache：

```text
2 × 80 × 8 × 128 × 4096 × 2 ≈ 1.34 GB
```

32 条并发就是约 43GB，还没算权重和激活。这解释了为什么长上下文推理需要多卡、GQA/MQA、KV cache 量化或 P/D 分离。

推理的核心指标：

- **TTFT**（time to first token）：prefill 延迟，受算力和输入长度影响；
- **TPOT**（time per output token）：decode 延迟，通常受显存带宽限制；
- **tokens/s**：吞吐；
- **goodput**：满足 SLO 的有效吞吐，比峰值 tokens/s 更有意义。

decode 阶段是 memory-bound：每生成一个 token 要把权重和 KV cache 读一遍。提高 batch 能摊薄权重读取，但会增加 KV cache 和延迟；这就是 continuous batching 和 PagedAttention 要解决的问题。

### 34.10.4 排队与容量

设到达率 λ（作业/秒）、服务率 μ（作业/秒）、利用率 ρ = λ/μ。M/M/1 的排队等待：

```text
Wq = ρ / (1 - ρ) × E[S]
```

| 利用率 ρ | 等待/服务时间倍数 |
|---:|---:|
| 0.50 | 1.0 |
| 0.70 | 2.3 |
| 0.80 | 4.0 |
| 0.90 | 9.0 |
| 0.95 | 19.0 |

这就是“利用率 90% 看起来很好，但 P99 等待已经爆炸”的原因。训练平台不要把可持续利用率规划到 90% 以上；留 10%–20% 的 headroom 吸收突发、碎片和故障。

更一般的 G/G/1 近似（Kingman）：

```text
Wq ≈ (ρ / (1 - ρ)) × ((Ca² + Cs²) / 2) × E[S]
```

`Ca`、`Cs` 是到达和服务时间的变异系数。gang 作业的服务时间分布通常不是指数分布，碎片会让方差更大；这个公式只用来判断量级和趋势，不用来做精确承诺。

### 34.10.5 一个容量估算表

| 负载 | 估算对象 | 公式/方法 | 注意 |
|---|---|---|---|
| 预训练 | 卡数 | `world_size` 由模型/并行策略决定 | 按最大 gang 留连续资源 |
| 预训练 | 时间 | `6PD / (N × peak × MFU)` | MFU 用实测，不要用峰值 |
| 微调 | 卡数 | 模型状态 + 激活 + batch | LoRA 也要算基础权重 |
| 推理 | 显存 | 权重 + KV cache + 激活 | 长上下文先算 KV cache |
| 推理 | 副本数 | `峰值 QPS × 平均 token 数 / 单副本 tokens/s` | 再乘冗余系数 |
| Agent | 并发 | `到达率 × 平均时长`（Little 定律） | 冷启动和沙箱开销另算 |
| 所有 | headroom | 10%–20% | 碎片/故障/突发 |

### 34.10.6 一个端到端推理容量例子

公式只有变成数字才有用。下面用一个可复算的例子，把“QPS → 副本 → GPU → 队列 → 成本”串起来。**所有输入都是示例，必须用你自己的压测替换。**

假设：

```text
模型：70B，bf16
上下文：4096 token
每请求输出：500 token
峰值负载：20 QPS
单副本：8 × H100，实测 2000 output tokens/s
目标利用率：70%（留 30% 给突发和长尾）
冗余：N+1
```

第一步，算基础副本数：

```text
base = QPS × 输出 token / (单副本 tokens/s × 目标利用率)
     = 20 × 500 / (2000 × 0.7)
     = 7.14
     → 向上取整 8 个副本
```

第二步，加冗余：

```text
总副本 = 8 + 1 = 9
GPU 数 = 9 × 8 = 72
年成本 = 72 × $2/GPU-hour × 8760 ≈ $1.261M
```

第三步，检查显存是否装得下。70B bf16 权重约 140GB，8 卡每卡 17.5GB；单序列 4096 token 的 KV cache 约 1.34GB（34.10.3 节）。如果每个副本同时服务 150 条序列：

```text
KV cache / 副本 = 150 × 1.34GB ≈ 201GB
KV cache / GPU（按 TP=8 切分）≈ 25GB
权重 / GPU ≈ 17.5GB
合计 ≈ 42.5GB / 80GB
剩余 ≈ 37.5GB 给激活、通信缓冲和碎片
```

这说明 9 个副本在显存上是可行的，但余量不算奢侈。如果把并发提到 200，KV 变成约 268GB/副本、33.5GB/GPU，加上权重约 51GB/GPU；仍然能跑，但激活和碎片的空间变小，P99 延迟会更敏感。**推理容量不是“权重装得下”就结束，KV cache 和并发才是长上下文的真正预算。**

第四步，落到 Volcano：

```yaml
# 省略 Pod 模板细节
spec:
  schedulerName: volcano
  queue: llm-infer
  minAvailable: 8          # 9 个里至少 8 个可用；允许一次滚动/中断
  tasks:
    - name: serving
      replicas: 9
      template:
        spec:
          containers:
            - name: vllm
              resources:
                requests: {nvidia.com/gpu: "8"}
                limits:   {nvidia.com/gpu: "8"}
```

配套的队列和 PDB：

```yaml
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata:
  name: llm-infer
spec:
  weight: 2
  guarantee: 48            # 至少 6 个副本的保底
  capability: 72           # 最多 9 个副本
  reclaimable: false
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: llm-infer-pdb
spec:
  minAvailable: 8          # 9 个里最多中断 1 个
  selector:
    matchLabels: {app: llm-infer}
```

> 这里的 `llm-infer` 是**单个服务算例**；34.11 的四队列是平台级参考架构，两者的 capability/guarantee 不要混用。真实平台通常先按服务算副本，再把多个服务的总和落到队列配额。

三个检查：

1. **队列 cap 与副本数一致**：`capability=72` 正好等于 9×8；如果稍后把副本加到 10，队列会先拒绝，而不是让第 10 个副本永远 Pending。
2. **PDB 留 1 个冗余**：`minAvailable=8` 允许节点 drain 和一次自愿中断；设成 9 会让节点永远无法维护。
3. **gang 与滚动更新一致**：这里 9 个是独立副本，所以 `minAvailable=8` 与 PDB 的 8 一致，允许一次滚动/中断；如果 9 个是同一个模型的 shard、必须全部在线，才用 `minAvailable=9`，那时 PDB 也应设为 9（或不用 PDB），并改用 LWS 处理有序启动（34.8.3 节）。**不要一边写 `minAvailable=9`、一边写 PDB=8**：一次自愿中断就会同时违反两者。

第五步，做敏感性分析：

| 变化 | 基础副本 | 总副本 | GPU | 年成本（$2/卡时） |
|---|---:|---:|---:|---:|
| 20 QPS（本例） | 8 | 9 | 72 | $1.261M |
| 30 QPS | 11 | 12 | 96 | $1.682M |
| 40 QPS | 15 | 16 | 128 | $2.243M |
| 20 QPS，单副本 3000 tokens/s | 5 | 6 | 48 | $0.841M |
| 20 QPS，目标利用率 50% | 10 | 11 | 88 | $1.542M |

这张表说明两件事：

- **单副本吞吐比卡数更敏感**。同样的 20 QPS，单副本从 2000 提到 3000 tokens/s，GPU 从 72 降到 48；优化推理引擎/量化/批处理的收益，可能比“加卡”更大。
- **目标利用率不是越高越好**。70% 降到 50%，副本从 9 涨到 11。利用率 90% 时排队延迟会非线性上升（34.10.4 节），省下的卡可能抵不过 SLO 违约。

最后，把 SLO 写进验收：

```text
TTFT P99 < 2s（prefill）
TPOT P99 < 50ms（decode）
goodput ≥ 目标 tokens/s（满足 SLO 的有效吞吐）
PDB 违约 = 0
```

如果 TTFT 超标、TPOT 正常，优先加 prefill 容量或做 P/D 分离；如果 TPOT 超标，先看 KV cache/显存带宽和 batch，而不是盲目加副本。**TTFT 和 TPOT 是两种不同的瓶颈，扩容策略也不同。**

这个例子的计算可以用附录 U 的 `scripts/capacity_model.py` 复算；把 `--qps 20 --out-tokens 500 --tokens-per-sec-replica 2000 --target-util 0.7 --redundancy 1 --gpus-per-replica 8` 传进去，会得到同样的 8/9/72。

## 34.11 参考架构：三池、四队列、两层拓扑

### 34.11.1 节点池

```text
train-pool    8 卡/节点，NVLink/NVSwitch + IB，H100/A100/昇腾
infer-pool    4–8 卡/节点，L40S/H100/MIG，高出口带宽
agent-pool    CPU/混合，沙箱运行时，独立网络策略
```

每个池用 taint/toleration + nodeSelector 隔离，避免默认调度器或其他队列把非 GPU Pod 放进训练池。

### 34.11.2 队列

`examples/queues/llm-queues.yaml` 给出一个扁平四队列的起点：

| 队列 | weight | guarantee | capability | reclaimable | 用途 |
|---|---:|---:|---:|---|---|
| `llm-train` | 4 | 256 | 512 | true | 预训练/大 gang |
| `llm-finetune` | 2 | 64 | 256 | true | 微调/后训练 |
| `llm-infer` | 2 | 64 | 128 | false | 在线推理 |
| `llm-agent` | 1 | 0 | 64 | true | Agent 沙箱 |

检查：

```text
Σ guarantee = 256 + 64 + 64 + 0 = 384 ≤ 512 × 0.9 ≈ 460   ✓
Σ capability = 512 + 256 + 128 + 64 = 960 > 512             ✓（允许超卖）
```

如果使用层级 capacity，把四个队列挂到一个 `llm` 父队列下，父 `capability` 设为集群可用容量；不要同时启用 proportion。

### 34.11.3 调度器配置

```yaml
actions: "enqueue, allocate, backfill, preempt, reclaim"
tiers:
- name: tier-1
  plugins:
  - name: priority
  - name: gang
    enablePreemptable: true
  - name: pdb
  - name: conformance
- name: tier-2
  plugins:
  - name: drf
  - name: predicates
  - name: proportion
  - name: network-topology-aware
  - name: group-topology-affinity
  - name: nodeorder
  - name: binpack
    arguments: {binpack.weight: "5"}
```

三个注意点：

1. **训练队列不要开 shuffle/rescheduling**。驱逐一个 rank 可能触发整个作业重启，代价远高于“整理碎片”的收益。
2. **推理队列的 PDB 必须在 tier 1**，否则抢占/回收可能绕过保护。
3. **拓扑插件只对有 `networkTopology` 的作业生效**；没有 HyperNode 数据时不要开 hard 模式。

### 34.11.4 可观测性

至少采集四组指标：

```text
训练：MFU、goodput、checkpoint 时长、重启次数、gang 等待时间
推理：TTFT/TPOT P99、tokens/s、KV cache 命中率、队列等待
队列：allocated/deserved/capability、Pending 作业数、借用/回收次数
节点：DCGM GPU 利用率、显存、温度、NVLink/IB 错误计数
```

只看 GPU 利用率会把“占着卡等数据”误判为高效。训练看 MFU/goodput，推理看满足 SLO 的 tokens/s。

### 34.11.5 上线顺序

```text
第 1 步：训练池 + 单机 8 卡作业，验证 gang、镜像、NCCL
第 2 步：多机 64 卡作业，验证拓扑、IB、checkpoint
第 3 步：推理池 + 单副本，验证 PDB、Service、HPA
第 4 步：推理多副本 + 组间反亲和，验证故障域
第 5 步：Agent 池 + 沙箱隔离，验证网络策略与冷启动
第 6 步：队列公平/回收，先在微调/Agent 队列灰度
```

每一步都要有回滚条件：gang 失败率、P99 等待、抢占次数、PDB 违约、GPU 利用率异常。

## 34.12 用 MiniVolcano 验证哪些语义

MiniVolcano 不模拟 NCCL、checkpoint、KV cache 或推理框架，但它能验证**放置与中断语义**：

| LLM 语义 | Mini 场景 | 能证明 | 不能证明 |
|---|---|---|---|
| 世界大小 = gang 门槛 | `gang` | 少一卡就不启动 | 真实 NCCL 初始化 |
| 拓扑域选择 | `topology` | hard 模式把所有 Pod 限在同一域 | 真实 IB 带宽/收敛比 |
| 组间故障域打散 | `group-topology` | 两个 serving 组不共享 rack | 真实故障切换 |
| 推理中断保护 | `pdb` | 一次中断允许、第二次阻断 | Eviction API/健康探测 |
| 等待预算 | `sla` | 超预算作业优先 | 真实 SLA 合同 |
| 失败重启 | `retry` | RestartJob/maxRetry | 容器/模型加载时间 |
| 异构选型 | `heterogeneous` | 只落 A100 节点 | 真实 GPU 型号/驱动 |
| NUMA | `numa` | 单 NUMA 适配 | PCIe/NVLink 拓扑 |

推荐命令：

```bash
cd mini-volcano
go run ./cmd/minivolcano -scenario gang -events 12
go run ./cmd/minivolcano -scenario topology -events 12
go run ./cmd/minivolcano -scenario group-topology -events 12
go run ./cmd/minivolcano -scenario pdb -explain default/high-2
go run ./cmd/minivolcano -scenario retry -events 20
```

这些场景证明的是“调度器的决策逻辑”，不是“训练能跑多快”。把 Mini 当算法试验田，把真实集群当性能与故障的试验田。

## 34.13 反模式清单

| # | 反模式 | 现象 | 根因 | 修复 |
|---:|---|---|---|---|
| 1 | 多机训练用 Deployment，不设 gang | 部分 worker 启动，all-reduce 卡死 | Deployment 不保证一起启动 | 用 Volcano Job/LWS + PodGroup |
| 2 | 64 卡作业 `minAvailable: 1` | 只启动 1 个 rank，其余 Pending | 把 gang 门槛当“最少副本” | `minAvailable = world_size` |
| 3 | 严格 gang + 弹性框架 | 扩缩时永远凑不齐 | 框架按需增减成员 | 降低 `minAvailable`，用弹性 gang |
| 4 | 推理 Deployment 用 `minMember=replicas` | 滚动更新卡住 | 新旧 Pod 同时存在 | 用 LWS，或 `minMember=1` |
| 5 | 训练队列开 shuffle | 作业反复重启，进度倒退 | 驱逐 rank 触发 RestartJob | 训练队列关闭重调度 |
| 6 | 没有 checkpoint 就允许抢占 | 一次驱逐丢几天进度 | 中断代价未建模 | checkpoint + 优先级/保底 |
| 7 | `maxRetry` 过大 | 坏作业反复重启，占住队列 | 缺少失败分类 | 区分可重试/不可重试错误 |
| 8 | `maxRetry` 过小 | 一次抢占就 Failed | 把驱逐当失败 | 驱逐不消耗重试或调大 |
| 9 | 每卡一个 Pod + TP=8 | 调度碎片、DNS 复杂 | Pod 粒度过细 | 大训练用每节点一个 Pod |
| 10 | 混用 A100/H100 不选型 | 作业落到错误型号 | 只有 `nvidia.com/gpu` | nodeSelector/节点池 |
| 11 | 队列 capability 写 `nvidia.com/gpu`，作业用 DRA | 配额永远为 0 或被绕过 | 两套资源键 | 统一 `deviceclass/<class>` |
| 12 | PDB `minAvailable = replicas` | 节点无法 drain，抢占被全阻 | 把 PDB 当“绝不中断” | 留 1 个冗余 |
| 13 | PDB selector 写错 | 看起来有保护，实际没有 | 标签不匹配 | `kubectl describe pdb` 验证 |
| 14 | 推理和训练同队列 | 训练把推理延迟打爆 | 没有分池/分队列 | 独立队列 + 优先级 |
| 15 | Agent 无出口策略 | 数据外泄/横向移动 | 默认网络放通 | 默认拒绝 + 白名单 |
| 16 | vGPU 超卖无上限 | 显存 OOM/邻居干扰 | 只看卡数不看显存 | 显存/算力配额 |
| 17 | 镜像用 `latest` | 重跑结果不可复现 | 没有 digest | 固定 digest + 签名 |
| 18 | GPU 池没有 taint | 非 GPU Pod 占住节点 | 默认调度器/其他队列进入 | taint/toleration + nodeSelector |
| 19 | 忘记 `/dev/shm` | DataLoader/NCCL 报错 | 默认 64MiB | 挂 Memory emptyDir |
| 20 | 用短作业压测 | 利用率/等待时间失真 | 负载形状不真实 | 按生产分布回放 |

## 34.14 本章小结

- LLM 工作负载不是一个“大 gang”问题，而是训练、微调、推理、Agent 四类不同形状的问题。
- 并行策略决定世界大小和通信层次：TP 在节点内，PP/DP/EP 跨节点；gang 尺寸必须与 `TP × PP × CP` 的整除约束一致。
- 预训练用严格 gang + RestartJob + checkpoint；微调可以弹性/可抢占；推理用 PDB + 反亲和，不要用严格 gang 卡住滚动更新；Agent 要独立池、沙箱和出口策略。
- 容量估算要同时看显存、算力、KV cache、网络和排队；利用率超过 90% 时等待时间会非线性上升。
- MiniVolcano 能验证 gang、拓扑、PDB、SLA、重试等放置语义，但不能验证 NCCL、checkpoint、KV cache 和真实性能。
- 第 34 章给出的 YAML 是起点，不是可直接投产的模板；镜像、节点标签、拓扑、存储和网络都要按集群替换。

下一章是附录：API、配置、命令、术语、排障索引、配置配方、LLM 速查与自测题。

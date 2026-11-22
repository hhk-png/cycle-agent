# 第 32 章 集群、故障恢复与运维

## 32.1 拓扑决定并行

一个典型 GPU 集群：

```text
节点内：8×GPU + NVLink/NVSwitch
节点间：InfiniBand/RoCE（leaf/spine）
存储：本地 NVMe + 并行文件系统/对象存储
调度：Slurm / Kubernetes / 自研
```

并行映射原则：

- TP/EP 的高频通信留在 NVLink 域；
- PP/D 的 p2p/梯度可以跨节点；
- DP 分层 all-reduce；
- 存储/数据加载尽量本地/预取；
- 故障域要小于 checkpoint 间隔。

具体拓扑（H100/H200/B200/GB300 NVL72）差异很大；GB300 NVL72 把 72 个 GPU
放在一个 NVLink 域，改变了 TP/EP 的可行规模，但也带来供电/散热/调度挑战。

## 32.2 调度与放置

Slurm：

- `--nodes/--gres=gpu:8`；
- 拓扑感知（`--ntasks-per-node`、`--cpu-bind`）；
- 用 `srun` 启动 torchrun；
- 作业抢占/排队。

Kubernetes：

- GPU operator/device plugin；
- Pod 拓扑/NUMA；
- Kueue/Volcano 等批调度；
- NCCL 拓扑感知需要额外配置。

### 32.2.1 一个 Slurm 启动模板

```bash
#!/bin/bash
#SBATCH --nodes=8
#SBATCH --ntasks-per-node=8
#SBATCH --gpus-per-node=8
#SBATCH --cpus-per-task=16
#SBATCH --exclusive

export MASTER_ADDR=$(scontrol show hostnames "$SLURM_JOB_NODELIST" | head -n 1)
export MASTER_PORT=29500
export NCCL_DEBUG=INFO

# 每个节点启动一个 torchrun，由它在节点内起 8 个 worker
srun --mpi=pmix --ntasks=8 --ntasks-per-node=1 \
  torchrun --nnodes=8 --nproc_per_node=8 \
    --rdzv_backend=c10d --rdzv_endpoint="$MASTER_ADDR:$MASTER_PORT" \
    -m mini_megatron.cli train --config configs/tiny_tp2_pp2.json
```

要点：

- `MASTER_ADDR/PORT` 必须所有节点一致；
- `--ntasks-per-node` 与 `--nproc_per_node` 对齐；
- 拓扑感知放置（`--cpu-bind`、GPU affinity）影响数据加载；
- 先小规模验证启动脚本，再放大。

Kubernetes 下通常由 Training Operator/Kueue/Volcano 注入
`MASTER_ADDR/PORT/RANK/WORLD_SIZE`，并由 device plugin 分配 GPU；NCCL 拓扑
感知需要节点标签/亲和性配置。

放置不当会导致：

- TP group 跨 leaf switch，all-reduce 变慢；
- 同一 NVLink 域的 GPU 被分到不同作业；
- CPU/内存不足，数据加载成为瓶颈；
- 存储带宽争用。

### 32.2.2 Kubernetes：一个最小 PyTorchJob 模板

Kubernetes 上不要手写“每个 Pod 自己算 rank”。让 operator/调度器注入
`MASTER_ADDR/PORT/RANK/WORLD_SIZE`，容器内只负责启动 `torchrun`。下面是一个
Kubeflow PyTorchJob 的骨架（镜像、GPU 型号、共享存储按你的集群替换）：

```yaml
apiVersion: kubeflow.org/v1
kind: PyTorchJob
metadata:
  name: mini-tp2
spec:
  pytorchReplicaSpecs:
    Master:
      replicas: 1
      restartPolicy: OnFailure
      template:
        spec:
          hostNetwork: true          # 让 NCCL 看到真实网卡；按集群策略
          containers:
            - name: trainer
              image: registry.example.com/mini-megatron:0.4.4
              command: ["torchrun"]
              args:
                - "--nnodes=2"
                - "--nproc_per_node=1"
                - "--rdzv_backend=c10d"
                - "--rdzv_endpoint=$(MASTER_ADDR):$(MASTER_PORT)"
                - "-m"
                - "mini_megatron.cli"
                - "train"
                - "--config"
                - "configs/tiny_tp2.json"
              env:
                - name: NCCL_DEBUG
                  value: "INFO"
                - name: NCCL_SOCKET_IFNAME
                  value: "eth0"
              resources:
                limits:
                  nvidia.com/gpu: 1
              volumeMounts:
                - {name: data, mountPath: /data}
                - {name: dshm, mountPath: /dev/shm}
          volumes:
            - name: data
              persistentVolumeClaim: {claimName: training-data}
            - name: dshm
              emptyDir: {medium: Memory, sizeLimit: 64Gi}
    Worker:
      replicas: 1
      restartPolicy: OnFailure
      template:
        spec:
          # 与 Master 相同的容器/资源；operator 注入 rank
          hostNetwork: true
          containers:
            - name: trainer
              image: registry.example.com/mini-megatron:0.4.4
              command: ["torchrun"]
              args:
                - "--nnodes=2"
                - "--nproc_per_node=1"
                - "--rdzv_backend=c10d"
                - "--rdzv_endpoint=$(MASTER_ADDR):$(MASTER_PORT)"
                - "-m"
                - "mini_megatron.cli"
                - "train"
                - "--config"
                - "configs/tiny_tp2.json"
              resources:
                limits:
                  nvidia.com/gpu: 1
              volumeMounts:
                - {name: data, mountPath: /data}
                - {name: dshm, mountPath: /dev/shm}
          volumes:
            - name: data
              persistentVolumeClaim: {claimName: training-data}
            - name: dshm
              emptyDir: {medium: Memory, sizeLimit: 64Gi}
```

工程上要额外检查：

| 项 | 为什么 | 常见配置 |
|---|---|---|
| gang scheduling | 所有 worker 必须同时起来，否则 NCCL 等不到对端 | Kueue/Volcano/operator 的 minAvailable |
| `/dev/shm` | 数据加载/NCCL 共享内存不足会莫名挂起 | `emptyDir: medium=Memory`，≥ 节点内存的一部分 |
| 网卡选择 | 容器里可能有多张网卡，NCCL 选错会走管理网 | `NCCL_SOCKET_IFNAME`、`NCCL_IB_HCA` |
| GPU 拓扑 | 同一 Pod 内多卡要落在同一 NVLink 域 | node affinity + device plugin 拓扑感知 |
| 抢占/重启 | 训练需要从检查点恢复，而不是从 step 0 | `restartPolicy: OnFailure` + 自动 resume |
| 存储 | 多节点共享检查点目录 | RWX PVC/对象存储；避免 NFS 小文件风暴 |

注意 `torchrun --nnodes=2 --nproc_per_node=1` 只是让“每个 Pod 一个 rank”；
如果你的容器请求 8 张 GPU，应该用 `--nproc_per_node=8`，并让配置的 world
size 与之匹配（第 03 章的整除关系同样适用）。

## 32.3 故障模型

大规模训练的故障源：

- GPU 掉卡/ECC/Xid；
- NVLink 降速/错误；
- IB 链路抖动/交换机故障；
- 节点宕机/被抢占；
- 存储限流/坏文件；
- NCCL 超时/挂起；
- 数据/代码 bug 导致 NaN。

假设 1000 卡、MTBF 数万小时，训练数周几乎必然遇到故障。系统必须：

- 快速检测（heartbeat/health check）；
- 隔离坏节点；
- 从最近 checkpoint 恢复；
- 支持弹性/重调度；
- 记录故障根因。

### 32.3.1 故障容错栈：从“能恢复”到“少中断”

生产上通常分四层，不要只做其中一层：

| 层 | 机制 | 作用 | 代价/边界 |
|---|---|---|---|
| 1. 检测 | heartbeat、Xid/ECC、NCCL async error、health check | 快速发现坏节点/挂起 | 误报/漏报；检测时间直接变浪费 |
| 2. 隔离 | 节点 drain/taint、GPU reset、链路禁用 | 防止坏节点反复被调度 | 需要调度器/运维接口 |
| 3. 恢复 | checkpoint + 重启、in-job restart | 从最近状态继续 | 丢进度；重启要重建 group/通信 |
| 4. 弹性 | TorchElastic/Slurm 重新组队、重分片 | 换卡数继续 | 数据/RNG/优化器语义复杂 |

“checkpointless”/in-memory recovery 是第 3 层的激进版本：把状态复制到
其他节点的 CPU/GPU 内存，故障后从副本恢复，减少写盘。它省的是**写检查点
的时间和存储带宽**，代价是占用内存/网络，并且只能容忍“副本还在”的故障。
不要把“checkpointless”理解成“不需要检查点”——它仍需要周期性持久化来应对
整机/机架级故障。

另外两类常被忽略：

- **SDC（silent data corruption）**：GPU 计算错误但不报错，表现为 loss 缓慢
  漂移或 NaN。需要 burn-in/自检（如 GPU 诊断、已知答案的 matmul 校验）、
  多副本比对、异常 step 的 checkpoint 回溯。
- **慢故障**：NVLink 降速、IB 重传、电源降频。它们不触发“失败”，但会让
  整个 job 的 step time 被最慢 rank 拖住。需要 per-rank/per-stage 的
  straggler 检测，而不是只看平均吞吐。

## 32.4 检查点策略

- 间隔：按故障率/恢复成本平衡；太频繁会拖慢训练，太稀疏会丢进度；
- 异步：copy + 后台写，避免阻塞；
- 分层：本地 NVMe 快照 + 对象存储长期；
- 校验：定期读回验证；
- 保留：最近 N 个 + 里程碑；
- 重分片：换并行度/卡数时使用。

一个 70B 模型 checkpoint 可能 1 TB+；千卡同时写会打爆存储。需要：

- 分 rank/分 shard 写；
- 限流；
- 去重/压缩；
- 只保存必要状态。

## 32.5 弹性训练

弹性（elastic）要解决：

- 节点增减后重新划分 rank/group；
- 参数/优化器状态重分片；
- 数据 sampler 重新分片；
- 通信重建；
- 保证训练语义（global batch）不漂移。

Megatron 本身提供并行/检查点能力，弹性调度通常由上层平台（Slurm/K8s/
TorchElastic）实现。弹性不是“免费”：重分片/重启/数据重排都有成本。

### 32.5.1 弹性重启的语义陷阱

一个“能重新拉起”的 job 不等于“训练语义正确”。检查清单：

1. **global batch 是否漂移**：world size 变了，DP 变了，`global_batch =
   micro_batch × grad_accum × DP` 必须重新配平；否则等效 batch/LR 变了。
2. **数据是否重复/跳过**：sampler 的 epoch/offset 要持久化；按 rank 重新
   分片后，原来的“第 r 个 shard”不再对应同一批样本。
3. **优化器状态是否可重分片**：ZeRO/FSDP 的本地索引随 DP 变化；只加载
   权重会“能跑但不对”。
4. **RNG 是否连续**：TP/DP/CP 的 RNG 流语义不同，换并行度后不能假设
   “同一个 seed”就复现。
5. **学习率/scheduler 的 step**：重启后从 `step+1` 继续，不要重置 warmup。
6. **通信 group 重建**：旧 group 的 rank 映射失效，必须重新
   `initialize_model_parallel`；残留的 NCCL 连接要清理。

最小验收：在**同一 checkpoint** 上用两种卡数各跑 20–50 步，比较 loss 曲线
的斜率与 grad norm 量级；如果显著分叉，先查数据/优化器/并行语义，而不是
归咎于“弹性不稳定”。

## 32.6 Straggler 与负载不均

- 某些 GPU 降频/发热/ECC；
- 某些节点网络拥塞；
- MoE 专家负载不均；
- 数据长度不均导致 tail latency；
- PP 最慢 stage 决定整体。

监控：

- 每卡/每 stage 的 step time；
- NCCL 通信时间；
- GPU 利用率/功耗/温度；
- 专家 token 分布；
- 数据加载时间。

处理：

- 隔离慢节点；
- 负载均衡（MoE aux/capacity）；
- packing/变长 batch；
- 调整并行映射。

## 32.7 安全与合规

- 训练数据可能含敏感信息；
- checkpoint 可能含数据记忆；
- 多租户集群隔离；
- 供应链（PyTorch/TE/NCCL 版本）；
- 访问控制/审计；
- 模型/数据许可证。

这些在企业落地时往往比“多快”更重要。

### 32.7.1 非 NVIDIA 硬件与区域生态

Megatron Core 的主线优化围绕 NVIDIA GPU + NCCL + Transformer Engine，但
生产选型不能假设“只有一条硬件路”：

| 生态 | 代表 | 与 Megatron 的关系 | 主要摩擦 |
|---|---|---|---|
| AMD ROCm | MI300X/MI325X | 有 ROCm 版 PyTorch/通信库；部分 kernel/并行路径可移植，需逐项验证 | TE/NCCL 替换为 hipBLASLT/RCCL；融合 kernel 覆盖不同 |
| Google TPU | TPU v5e/v5p/Trillium | Megatron 不是主栈；JAX/MaxText/PaxML 更自然 | 编程模型/集合通信/XLA 差异 |
| AWS Trainium | Trainium2 | 主要走 Neuron SDK/NeMo 适配 | 编译器/算子覆盖、迁移成本 |
| 华为昇腾 | Ascend 910B/910C | **MindSpeed** 是面向昇腾的 Megatron 风格并行/加速套件；公开报道显示在国内有较多采用（无统一份额数据） | CANN/HCCL、算子/精度、生态工具链差异 |
| 其他国产 GPU | 寒武纪/海光/壁仞等 | 多数提供 PyTorch 适配或自研并行库 | 成熟度/算子覆盖/社区 |

关键判断：

- **“Megatron 代码能跑”不等于“性能可比”**。TP/PP/EP 的通信原语、融合
  kernel、FP8 支持、集合通信拓扑都依赖硬件/软件栈；
- 昇腾生态的 MindSpeed 不是“Megatron 的简单 fork”，而是把并行策略映射到
  CANN/HCCL 并补算子；读它的代码时要注意哪些是 Megatron 语义、哪些是
  硬件适配；
- 跨硬件迁移的第一验收不是 loss，而是**逐层数值对齐**（固定权重/输入，
  比较 hidden/logits）和**通信正确性**（group/顺序/精度）；
- 出口管制/供应链会让“同一模型多硬件备份”成为真实需求，架构上要保留
  可替换的 kernel/通信/checkpoint 层。

## 32.8 mini 的运维视角

mini 没有集群调度/弹性，但保留了：

- 检查点/恢复；
- 多进程启动；
- 日志/错误处理；
- 配置校验；
- portable/torch 后端切换。

它可以帮助理解“故障恢复的最小闭环”，但不能替代生产平台。

## 32.9 本章小结

- 拓扑决定并行映射；TP/EP 留 NVLink，PP/DP 跨节点。
- 大规模训练必须假设故障会发生；检查点/弹性/隔离是基础。
- 检查点要异步、分层、校验、可重分片。
- Straggler/MoE 负载/数据长度都会造成尾延迟。
- 安全/合规是企业落地的硬约束。

下一章讨论调试、数值稳定性与可观测性。

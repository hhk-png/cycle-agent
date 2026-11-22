# 附录 W 资源分离、检查点引擎与弹性部署

同步 PPO 的经典形态是 colocate：actor、rollout、ref、critic 共享同一组 GPU，训练时释放 KV cache，生成时暂停训练。它简单、省卡，但有两个天花板：

- rollout 与训练不能同时跑，GPU 在两种负载之间切换时空转；
- 一个慢请求会阻塞整个 batch，长尾任务利用率低。

资源分离（separation）把训练与推理放到不同资源池，用异步或流水线掩盖切换。代价是权重同步跨进程/跨节点，必须有一套可靠的权重传输层。本附录讲 v0.9.1 的 separation、checkpoint engine 与弹性部署。

## W.1 三种资源拓扑

| 拓扑 | 训练与推理 | 权重同步 | 适用 |
|---|---|---|---|
| colocate | 同一组 GPU，分时复用 | 内存拷贝或 NCCL | 中小规模、同步 PPO |
| separation | 不同资源池，可同时运行 | checkpoint engine / RDMA | 异步 RL、长尾生成、独立扩缩 |
| fully async | 训练与 rollout 完全解耦，流式 | TransferQueue + checkpoint engine | 大规模、高吞吐、可容忍 staleness |

separation 不是"把进程拆开"这么简单。它要求：

1. 训练侧能把模型状态 detach 到 CPU 并释放 GPU；
2. 推理侧能在不重启的情况下接收新权重；
3. 两侧对"当前是第几个版本"有共识；
4. 故障时能重建通信组或重新调度。

## W.2 SeparateRayPPOTrainer 与 DetachActorWorker

`verl/experimental/separation/ray_trainer.py` 的 `SeparateRayPPOTrainer` 继承 `RayPPOTrainer`，复用了 PPO 的训练逻辑，只改资源池与 worker 映射。它支持两类场景：

- fully async policy；
- one-step off-policy。

`verl/experimental/separation/utils.py` 里有两个关键函数：

```python
def create_resource_pool_manager(config, roles):
    ...
def create_role_worker_mapping(config):
    ...
```

这两个 helper 只负责**训练侧**：`create_resource_pool_manager` 只创建 `trainer_pool`，并把 Actor/ActorRollout/Critic/RefPolicy 映射进去；`create_role_worker_mapping` 只决定每个角色用哪个 worker 类。rollout 资源池不是这两个函数创建的，而是由 fully-async 的 rollouter / 独立配置分配；`rollout.n_gpus_per_node` / `rollout.nnodes` 是给那一侧用的。因此"训练池与推理池分开"是整体部署的事实，不是 `create_resource_pool_manager` 一个函数完成的。

`verl/experimental/separation/engine_workers.py` 的 `DetachActorWorker` 继承 `ActorRolloutRefWorker`，增加两个能力：

- **detach**：把模型状态保存到 CPU，释放 GPU；
- **restore**：把状态搬回 GPU，继续训练。

它支持 FSDP、FSDP2、VeOmni、Megatron 四种策略。实现上有一个容易踩的差异：

- FSDP1 把参数分片成 **flat tensor**；
- FSDP2 把参数分片成 **DTensor**；
- VeOmni 内部用 FSDP2 做数据并行，因此参数也是 DTensor；
- Megatron 的分片布局由 TP/PP/EP 决定。

所以 detach/restore 不能共用一套 `state_dict` 读写工具；必须按策略选择对应的 save/restore handler。`DetachActorWorker._get_strategy_handlers()` 就是做这件事。

## W.3 权重同步的抽象：CheckpointEngine

`verl/checkpoint_engine/base.py` 定义了统一的权重传输抽象：

```python
class CheckpointEngine(ABC):
    async def send_weights(self, weights, global_steps=None): ...
    async def receive_weights(self, global_steps=None) -> Generator: ...

class CheckpointEngineWithCache(CheckpointEngine):
    async def get_weights(self) -> Generator: ...
```

两个基础 API 的语义：

- `send_weights`：接收一个 `(name, tensor)` 生成器，流式发送；
- `receive_weights`：返回一个 `(name, tensor)` 生成器，逐张量接收。

`get_weights` 不在 `CheckpointEngine` 上，而在 `CheckpointEngineWithCache` 上（用于部分 rollout 场景的本地共享内存/磁盘缓存）；v0.9.1 里这个子类没有具体实现，worker 路径实际用的是 `receive_weights`。

训练侧的 `get_per_tensor_param()` 返回 `(generator, metadata)` 二元组，不是裸生成器：

```python
weights, metadata = actor.get_per_tensor_param()
await engine.send_weights(weights, global_steps=step)
```

`CheckpointEngineRegistry` 负责按 backend 名注册与实例化。它还会记录各 backend 模块的 import error，这样"nixl 没装"会以"backend 未注册 + 具体 ImportError"的形式报出来，而不是一个模糊的 key error。

`wire_format` 有两种：

- `named_tensors`：`(name, tensor)` 对，按 bucket 聚合成整张量加载；
- `delta_flush`：`(named_tensors, is_last)`，首个 seed 是全量，后续是稀疏补丁。

## W.4 六种后端与选择

`verl/checkpoint_engine/README.md` 给出了一张很实用的表：

| 后端 | 通信库 | 拓扑 | 硬件 | 性能 | 弹性 | 典型场景 |
|---|---|---|---|---|---|---|
| naive | torch.distributed | all_gather | NVIDIA/AMD/Ascend | 很高 | 不适用 | colocate 同步训练 |
| nccl | NCCL | all_gather + broadcast | NVIDIA | 很高 | 低（重建组） | 分离但固定集群 |
| hccl* | HCCL | all_gather + broadcast | Ascend | 高 | 低（重建组） | Ascend 分离部署 |
| nixl | NIXL | all_gather + ring p2p | 多种传输 | 中/高 | 高（动态环） | 弹性 rollout、异构硬件 |
| kimi_ckpt_engine | Mooncake + NCCL/HCCL | p2p + broadcast | NVIDIA/Ascend | 高 | 低（重建组） | 每次保存 checkpoint 的分离训练 |
| mooncake | Mooncake Transfer Engine | all_gather + ring p2p | NVIDIA/Ascend | 高 | 高（动态环） | 固定集群的分离部署 |

\* `hccl` 是通信库，不是独立的注册键。`verl/checkpoint_engine/hccl_checkpoint_engine.py` 把类注册在 `"nccl"` 下；Ascend 上配置 `backend=nccl` 会自动走 HCCL。文档表格按通信库列它，是为了说明硬件路径，不是让你写 `backend=hccl`。

选择逻辑：

- **colocate**：naive 足够，权重就在同一进程/同一 GPU 上；
- **固定集群的分离部署**：nccl 最直接，带宽高；Ascend 也配 `nccl`（实际走 HCCL）；
- **需要弹性扩缩或故障恢复**：nixl/mooncake 的动态环更合适；
- **Ascend**：`backend=nccl` + HCCL 库；kimi 引擎需要 Mooncake 的 P2P 能力与额外环境变量；
- **每次更新都落 checkpoint**：kimi 引擎的 offload + P2P 路径针对这个场景优化。

上游 README 给出的量级（Qwen3-30B-A3B，H100 400 Gbps IB）大致是：NCCL/NIXL 约 7 秒、Mooncake 约 5.9 秒、Ascend 910C 上 HCCL 约 11 秒。这些数字依赖拓扑、模型与 bucket 大小，只能作为量级参考，不是选型承诺。

## W.5 delta 同步：只传变化的部分

全量同步的字节数是模型大小乘以更新次数。对一个 30B 模型，每步传 60 GB 是不现实的。delta 同步的思路是：训练侧维护一份 pinned-CPU 快照，逐字节比较，只发送变化的 `(position, value)` 对。

`verl/checkpoint_engine/delta_checkpoint_engine.py` 的关键设计：

- 首次（seed）同步发送 **完整 HF 导出**，保证 resume 可用；
- seed 之后，训练侧 prime 分片快照，后续只发稀疏 delta；
- 接收侧不需要在 rollout 侧镜像整个模型；峰值内存是"一个 bucket + 一个 decode chunk"；
- SGLang 通过 `--custom-weight-loader` 的 `delta_loader` 原地 masked-apply，不需要 fork；
- vLLM 通过同卡 IPC 把 delta 解码成 checkpoint patch，复用原生 `load_weights` 映射。

delta 的收益取决于**变化比例**。如果每步大部分参数都变（例如全参数 RL 的大学习率），delta 可能不如全量；如果只更新 LoRA adapter 或小部分专家，delta 收益显著。上线前要测"每步变化字节 / 全量字节"。

## W.6 弹性与故障恢复

分离部署把故障面扩大了：训练侧、推理侧、传输层、路由层都可能出问题。

| 故障 | 影响 | 处理 |
|---|---|---|
| rollout worker 挂 | 在途请求丢失 | 请求重试 + 幂等 id；nixl 动态环移除节点 |
| 训练 worker 挂 | 当前 step 失败 | 从最近 checkpoint 恢复；重建通信组 |
| 传输超时 | step 卡住 | 超时 + 降级到全量/重建组 |
| 版本不一致 | 训练用新权重、推理用旧权重 | 版本号随权重一起传；更新后确认 |
| 路由把请求发给已下线副本 | 请求失败 | 健康检查 + 路由摘除 |

弹性不是"自动重启"就够。关键是**版本共识**：每个 rollout replica 必须知道自己持有的是第几版权重；训练侧必须知道哪些 replica 已经确认更新。否则异步 RL 的 staleness 计算会错，重要性采样会用到错误的 old policy。

## W.7 部署编排：Slurm、Kubernetes 与 SkyPilot

三种常见形态：

- **Slurm**：`srun` 拉起 Ray head/worker，适合已有 HPC 集群；要点是等所有节点加入后再提交训练，且 `scontrol` 输出的主机名要按数组处理。
- **Kubernetes**：RayCluster/RayJob，适合多租户与弹性；要点是 GPU 资源声明、共享存储、NCCL 网络策略。
- **SkyPilot**：跨云/本地集群的统一编排，v0.9.1 在 `examples/tutorial/skypilot/` 提供 PPO/GRPO 配置；适合"先跑起来再优化"的团队。

无论哪种，部署检查项一致：

- [ ] 所有节点能看到同一份数据与 checkpoint 存储；
- [ ] NCCL/HCCL 的网卡、IB/RoCE、MTU 一致；
- [ ] 每个节点的 Python/驱动/容器镜像一致；
- [ ] Ray 的 object store 与 spill 目录有足够空间；
- [ ] 失败重启不会覆盖未完成的 checkpoint；
- [ ] 有集中日志与指标（见第 31 章与附录 J）。

## W.8 成本模型

分离部署的成本要同时算三笔账：

$$
\text{总成本} = \underbrace{C_{\text{train}}}_{\text{训练卡时}} + \underbrace{C_{\text{rollout}}}_{\text{推理卡时}} + \underbrace{C_{\text{sync}}}_{\text{权重传输与空闲}}.
$$

colocate 的 $C_{\text{sync}}$ 很低，但 $C_{\text{rollout}}$ 与 $C_{\text{train}}$ 不能独立优化；separation 的 $C_{\text{sync}}$ 高，但两组资源可以各自跑到更高利用率。

一个粗略判断：设分离后训练与推理的利用率分别从 $u$ 提升到 $u'$，同步开销占比 $s$。只有当

$$
\frac{1}{u'} + s < \frac{1}{u}
$$

时，分离才在成本上划算。举例：colocate 利用率 0.5，分离后各 0.8，同步开销 10%，则右边 2.0，左边 1.25 + 0.1 = 1.35，明显划算；如果同步开销 80%，左边 2.05，就不划算。

这笔账要在目标集群上实测，而不是用别家的数字外推。

## W.9 上线检查清单

- [ ] 训练/推理资源池独立配置，角色映射正确。
- [ ] DetachActorWorker 的 save/restore handler 与后端策略匹配（FSDP1/FSDP2/VeOmni/Megatron）。
- [ ] checkpoint engine 后端与集群拓扑匹配；依赖已安装。
- [ ] 首次同步是全量 seed，之后才是 delta；resume 走 seed 路径。
- [ ] 权重版本随同步传播；每个 replica 确认后再进入下一步。
- [ ] 有传输超时、重试、降级与重建组策略。
- [ ] 监控同步耗时、字节数、失败率与 staleness。
- [ ] 成本模型包含同步开销与利用率，而不是只算卡数。
- [ ] 故障演练覆盖 rollout 挂、训练挂、传输超时、版本不一致。

## 延伸阅读

- `verl/experimental/separation/ray_trainer.py`、`verl/experimental/separation/engine_workers.py`、`verl/experimental/separation/utils.py`
- `verl/checkpoint_engine/base.py`、`verl/checkpoint_engine/README.md`
- `verl/checkpoint_engine/delta_checkpoint_engine.py`、`verl/checkpoint_engine/delta_sync/`
- `verl/checkpoint_engine/nixl_checkpoint_engine.py`、`verl/checkpoint_engine/mooncake_checkpoint_engine.py`、`verl/checkpoint_engine/kimi_checkpoint_engine.py`
- 第 14 章：权重同步与 3D-HybridEngine
- 第 27 章：异步 RL
- 附录 J：部署与集群运维
- 附录 M：引擎后端与扩展

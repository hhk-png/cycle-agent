# 第 4 章 HybridFlow：单控制器 + 多进程 worker 的编程模型

## 4.1 两种极端都不够好

RL 训练框架通常落在两个极端之间。

**极端 A：单进程 SPMD。** 所有 GPU 跑同一份 Python 代码，用 `torchrun` 启动，训练循环写在每个 rank 里。优点是简单、通信高效；缺点是 RL 的控制流（生成、奖励、过滤、异步）要在每个 rank 上重复执行，容易出现“只有 rank 0 知道下一步做什么”的分裂。

**极端 B：纯多控制器。** 每个 worker 自己决定何时生成、何时训练，通过消息队列协作。优点是异步、解耦；缺点是算法逻辑被拆散，难以复现，调试困难。

HybridFlow 选择中间路线：

> **单控制器写数据流，多进程 worker 执行计算。**

控制器是一个普通 Python 进程，里面写着：

```python
batch = rollout.generate(prompts)
batch = reward_manager(batch)
batch = actor.compute_log_prob(batch)
batch = ref.compute_ref_log_prob(batch)
batch = critic.compute_values(batch)
batch = compute_advantage(batch)
critic.update(batch)
actor.update(batch)
```

每个 `rollout`、`actor`、`critic` 背后是一个 worker group。控制器调用它们时，worker group 负责把数据切分、发给各 rank、收集结果。控制器看到的是“一个方法调用”，worker 看到的是“自己那一份数据”。

## 4.2 三个核心抽象

### 4.2.1 Worker

`verl/single_controller/base/worker.py` 中的 `Worker` 是一个逻辑进程。它知道自己属于哪个 group、rank 是多少、world size 是多少，并提供 collective 操作的封装。真实 verl 中每个 Worker 是一个 Ray actor；MiniVerl 中是同进程对象，但保留了 rank/world size/group 语义。

### 4.2.2 WorkerGroup

`verl/single_controller/base/worker_group.py` 中的 `WorkerGroup` 负责：

- 管理一组 worker；
- 把方法调用按 dispatch 模式分发；
- 收集结果并按 collect 模式合并；
- 处理 padding/unpadding；
- 监控 worker 存活。

MiniVerl 的 `LocalWorkerGroup` 实现了同样的 dispatch/collect 逻辑：

```python
class DemoWorker(Worker):
    @register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO)
    def compute(self, data):
        return data

group = LocalWorkerGroup(ResourcePool.single_node(2), DemoWorker)
out = group.compute(batch)   # batch 被切成 2 份，结果 concat
```

### 4.2.3 ResourcePool

`ResourcePool` 描述“每个节点起几个进程”。例如：

```python
ResourcePool(process_on_nodes=[8, 8])   # 2 台机器，每台 8 个 worker
```

真实 verl 会把它映射成 Ray placement group：每个 bundle 请求 1 个 GPU + 若干 CPU，`STRICT_PACK` 保证同一组的 worker 尽量落在同一节点。MiniVerl 只记录进程数与 local rank，用于模拟 rank 语义。

## 4.3 Dispatch 模式

verl 的 `verl/single_controller/base/decorator.py` 定义了这些模式：

| 模式 | 行为 | 典型用途 |
|---|---|---|
| `ONE_TO_ALL` | 每个 worker 收到同样的参数 | 初始化、广播配置 |
| `RANK_ZERO` | 只在 rank 0 执行 | 保存 checkpoint、取 state dict |
| `ALL_TO_ALL` | 参数本来就是每 rank 一份 | 自定义分发 |
| `DP_COMPUTE` | 参数是长度为 world size 的列表 | 手动切分 |
| `DP_COMPUTE_PROTO` | 把 `DataProto` 按 world size 切分，结果 concat | actor/critic/ref 的批计算 |
| `DP_COMPUTE_PROTO_WITH_FUNC` | 第一个参数是函数，其余是 DataProto | 把 loss 函数发给 worker |
| `DP_COMPUTE_METRIC` | DP 计算但只收集指标 | 日志 |
| `make_nd_compute_dataproto_dispatch_fn(mesh_name=...)` | 按实际并行拓扑切分 | Megatron PP/CP 等多维 mesh |

`DP_COMPUTE_PROTO` 是最常用的。它做三件事：

1. 如果 batch 长度不能被 world size 整除，先 padding；
2. 每个 rank 拿一个 chunk；
3. 收集后 concat，再去掉 padding。

伪代码：

```python
def dispatch_dp_compute_data_proto(group, data):
    data, pad = pad_to_divisor(data, group.world_size)
    chunks = data.chunk(group.world_size)
    outputs = [worker.compute(chunks[i]) for i, worker in enumerate(group.workers)]
    result = DataProto.concat(outputs)
    return unpad(result, pad)
```

MiniVerl 的 `single_controller/worker_group.py` 就是这个逻辑，并额外支持 `sync_gradients=True`：每个 worker 只做前向/反向，group 在所有 worker 返回后对梯度做 all-reduce，再让每个 worker 执行 optimizer step。这是把 NCCL all-reduce 从 worker 内部搬到控制器侧的教学模拟。

## 4.4 数据流：V1 trainer 的一步

v0.9.1 的 V1 trainer（`verl/trainer/ppo/v1/trainer_base.py`）把一步拆成：

```text
replay_buffer.sample
  -> [optional] colocated reward model
  -> balance batch
  -> compute old_log_prob
  -> [optional] compute ref_log_prob
  -> [optional] compute values
  -> compute advantage
  -> [optional] update critic
  -> update actor
  -> on_step_end: update weights
```

注意 `replay_buffer.sample` 不是简单的 dataloader：V1 用 TransferQueue 管理 prompt group 与生成结果，支持：

- prompt 级别的流式生产/消费；
- DAPO 过滤后的重新采样；
- 异步模式下的 staleness 控制；
- checkpoint 时保存/恢复队列状态。

TransferQueue 的 `put/get/snapshot` 契约与背压策略见 27.12。这让“训练循环”与“数据生产”解耦：控制器仍然按顺序写逻辑，但样本可以来自不同版本的策略。

## 4.5 3D-HybridEngine

训练和推理的模型布局不同：

- 训练：FSDP 分片参数/梯度/优化器，Megatron 可能还有 TP/PP/CP/EP；
- 推理：vLLM/SGLang 需要按 TP 切分的权重、KV cache、continuous batching。

如果每轮都把完整模型从训练布局 gather 成 HF 格式，再让推理引擎加载，会有两个问题：

1. **显存峰值**：gather 出完整权重需要额外一份内存；
2. **通信量大**：每轮同步全部参数，即使只更新了一小部分。

3D-HybridEngine 的思路是：

- 在训练侧保留 3D 并行布局；
- 在推理侧只保留推理需要的 TP 布局；
- 通过 sharding manager 做**局部的 reshape/reshard**，避免完整 gather；
- 训练时把推理引擎的显存释放（sleep），生成时再唤醒（wake）。

v0.9.1 进一步把权重同步抽象成 `checkpoint_engine`：

| backend | 语义 |
|---|---|
| `naive` | 逐 tensor 拷贝，最通用 |
| `nccl` | 一次 NCCL broadcast，适合全量同步（NPU 上由 HCCL 实现类注册为同名 `nccl`） |
| `delta_sharded` | 逐 shard 做 diff，不 materialize 完整 tensor |
| `nixl` | NIXL p2p 传输，支持 UCX/UCCL/Mooncake 等底层 |
| `mooncake` | Mooncake KV/RDMA 传输栈 |
| `kimi_ckpt_engine` | Kimi 的 checkpoint 传输实现 |

注意 v0.9.1 **没有**名为 `delta` 的注册后端（`delta` 是 MiniVerl 的教学实现），也没有 `hccl`/`kimi` 这两个配置字符串。MiniVerl 的 `CheckpointEngineManager` 模拟了 `naive/nccl/delta/delta_sharded` 四种 backend，并记录 `bytes_sent`、`changed_ratio`、`sync_time_ms`。这些数字是教学用的，不是生产 benchmark，但足以说明“delta 同步为什么能省带宽”。

## 4.6 资源放置：colocate 还是分离

verl 支持几种资源模式：

### 同步 colocate

actor、critic、ref、rollout 共享同一组 GPU。生成时 rollout 占显存，训练时 rollout sleep。优点是资源利用率高；缺点是无法真正重叠生成与训练。

### colocate async

训练与生成共享 GPU，但通过 replay buffer 让训练消费上一轮样本，生成在训练的同时进行。需要处理：

- 权重版本；
- staleness；
- 显存竞争；
- partial rollout。

### separate async

rollout 使用独立资源池，训练与生成完全解耦。优点是长尾不阻塞训练；缺点是总 GPU 数增加，权重同步走网络。

选择哪种不是“越新越好”：

- 集群小、模型大：colocate sync 最稳；
- 长尾严重、追求吞吐：colocate/separate async；
- 多租户平台：separate，便于隔离与弹性。

## 4.7 控制器不是“单点瓶颈”吗

单控制器只做调度和数据搬运，不做模型计算。它的开销主要是：

- 序列化/反序列化 `DataProto`；
- 调度 RPC；
- 收集指标；
- 保存 checkpoint。

在千卡规模下，这些开销相对于 GPU 计算可以忽略。真正的瓶颈通常是：

- 权重同步的带宽；
- rollout 的长尾；
- 数据加载与 reward 计算；
- 跨节点 NCCL 拓扑。

verl 通过 Ray 的 zero-copy 序列化、TransferQueue、异步 RPC、bucket 化权重传输等手段降低控制器开销。

## 4.8 MiniVerl 的映射

| HybridFlow 概念 | MiniVerl |
|---|---|
| 单控制器 | `PPOTrainer` |
| Worker | `single_controller/worker.py::Worker` |
| WorkerGroup | `single_controller/worker_group.py::LocalWorkerGroup` |
| Dispatch | `single_controller/decorator.py::Dispatch` |
| DataProto | `protocol.py::DataProto` |
| ResourcePool | `worker_group.py::ResourcePool` |
| 3D-HybridEngine | `sharding.py::CheckpointEngineManager` + `rollout.py` |
| V1 数据流 | `trainer/ppo_trainer.py::PPOTrainer.train_step` |
| TransferQueue | `async_policy.py::ReplayBuffer`（简化） |

MiniVerl 没有 Ray、没有 NCCL、没有真正的多进程；它把“分布式语义”压缩成同进程的 rank/collective 模拟。这样做的代价是性能不真实，好处是每一步都可以单步调试。

## 本章小结

- HybridFlow 的核心是“控制流集中、计算流分布”。
- WorkerGroup 用 dispatch/collect 把数据切分与合并封装成 RPC。
- 3D-HybridEngine 解决训练/推理布局转换，checkpoint engine 解决权重传输。
- V1 trainer 用 TransferQueue 把数据生产与训练解耦，支持同步、colocate async、separate async。
- 资源模式的选择是吞吐、稳定性、成本三者的折中。

## 延伸阅读

- HybridFlow 论文：<https://arxiv.org/abs/2409.19256>
- verl 编程指南：<https://verl.readthedocs.io/en/latest/hybrid_flow.html>
- 下一章：v0.9.1 仓库地图与版本演进。

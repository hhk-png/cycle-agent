# 第 14 章 权重同步与 3D-HybridEngine

## 14.1 为什么权重同步是 RL 的核心成本

每轮 PPO 更新后，rollout 引擎必须用新权重生成下一批数据。同步的频率通常是“每个 global step 一次”，在异步模式下可能每几个本地更新一次。

一次同步要搬多少字节？

$$
\text{bytes} = \text{num\_params}\times\text{bytes\_per\_param}\times\text{replicas}.
$$

7B 模型、bf16、1 个 rollout 副本：14 GB/次。若 1000 步，总传输 14 TB。即使 NVLink/NVSwitch 带宽很高，这也是显著开销；跨节点走 IB 更贵。

所以 verl 提供了多种后端：

- `naive`：逐 tensor 拷贝；
- `nccl`：一次 broadcast（NPU 上由 HCCL 实现类注册为同名 `nccl`）；
- `delta_sharded`：逐 shard diff，不 materialize 完整 tensor；
- `nixl`：NIXL p2p 传输（UCX/UCCL/Mooncake 等底层）；
- `mooncake`：Mooncake KV/RDMA 传输栈；
- `kimi_ckpt_engine`：Kimi 的 checkpoint 传输实现。

v0.9.1 没有名为 `delta` 的注册后端；`delta` 只存在于 MiniVerl 的教学实现。`hccl`/`kimi` 也不是配置字符串。

## 14.2 训练布局 vs 推理布局

训练侧（FSDP）：

```text
parameter shard 0..N-1
```

推理侧（vLLM/SGLang TP）：

```text
每个 rank 持有完整的某些层 + 该层的一个 TP 切片
```

两者不是简单的“复制”关系：

- FSDP 按 flat buffer 分片，可能把一个矩阵切成不连续的块；
- Megatron 按 TP/PP/EP 分片，一个参数可能被切成多维；
- 推理引擎可能需要 fused QKV、不同的 bias 布局、不同的 dtype。

因此需要 **reshard**：把训练布局转成推理布局。

## 14.3 v0.9.1 的权重导出 API

`verl/workers/engine/base.py::BaseEngine` 定义了：

```python
def get_per_tensor_param(self) -> tuple[Generator[tuple[str, torch.Tensor], None, None], dict | None]:
    ...

def get_per_tensor_param_shard(self, **kwargs) -> tuple[Generator, dict | None]:
    ...

def get_per_tensor_param_delta_shard(self, **kwargs) -> tuple[Generator, dict | None]:
    ...
```

三个方法都返回 `(生成器, 可选 dict)`；第二个元素是 PEFT/LoRA 配置，没有 adapter 时为 `None`。只解包生成器会丢掉 adapter 信息，导致 rollout 端加载权重时缺少 LoRA 元数据。

### `get_per_tensor_param`

逐 tensor 产出**完整的 HF 坐标参数**。适合 naive/nccl 全量同步。实现可能要 all-gather 分片，因此有显存峰值。

### `get_per_tensor_param_shard`

逐 rank 产出本地 shard + `ShardSpec`。`ShardSpec` 描述：

- `full_shape`；
- `mesh` 与 `placements`（DTensor 的 DeviceMesh + Shard/Replicate）；
- `place`/`gather_group`/`contributes`；
- `to_hf_chunk`/`hf_slots`（fused 参数的转换）。

这让 checkpoint engine 不需要知道后端，只要按 spec gather。

### `get_per_tensor_param_delta_shard`

只产出**自上次同步以来变化的元素**，并转换成 HF 坐标。它需要：

- 上次的 snapshot；
- 变化元素的索引与值；
- 每个 rank 的贡献顺序一致（lockstep）。

`delta_sharded` 后端就是靠这个 API 做“逐 shard diff、只传变化”。

## 14.4 CheckpointEngineManager

`verl/checkpoint_engine/base.py` 的 `CheckpointEngineManager` 负责：

1. 选择 backend；
2. 调用 engine 导出参数；
3. 通过 NCCL/共享内存/对象存储传输；
4. 让 rollout server 加载；
5. 记录同步指标。

典型调用：

```python
checkpoint_manager.update_weights(global_steps)
checkpoint_manager.sleep_replicas()
checkpoint_manager.wake_up_replicas()
```

v0.9.1 的 `ActorRolloutRefWorker.update_weights` 是异步 RPC（`blocking=False`），控制器可以不等同步完成就继续调度。

## 14.5 3D-HybridEngine

3D-HybridEngine 的目标是**避免完整 gather**：

- 训练侧保留 3D 并行布局；
- 推理侧只保留 TP 布局；
- 通过局部的 reshape/reshard 把参数从训练布局映射到推理布局；
- 训练时释放推理显存，生成时再唤醒。

它解决三个问题：

1. 显存峰值：不需要同时持有完整训练权重和完整推理权重；
2. 通信量：只搬必要的数据；
3. 切换开销：sleep/wake 与权重更新可以重叠。

在 v0.9.1 中，这套能力被拆成 `checkpoint_engine` + `BaseRollout.update_weights` + `sleep/wake`，3D-HybridEngine 是设计理念，不是单个类名。

## 14.6 sleep/wake 的语义

`rollout.sleep()` 通常做：

- 释放推理权重；
- 释放 KV cache；
- 释放 CUDA graph；
- 保留必要状态（如 tokenizer、配置）。

`rollout.wake_up()` 重新分配并加载权重。注意：

- sleep 后不能生成；
- 权重更新可以在 sleep 期间发生（存到 CPU/共享内存），wake 时加载；
- 异步模式下不能随意 sleep，因为生成仍在进行；
- partial rollout 需要保存进行中的请求状态。

MiniVerl 的 `NaiveRollout.sleep/wake_up` 演示了这套生命周期：sleep 时 `model=None`、清空 prefix cache；wake 时用备份状态重建模型。

## 14.7 同步后端的选择

| backend | 适用 | 优点 | 缺点 |
|---|---|---|---|
| `naive` | 调试、单机 | 简单 | 慢、逐 tensor |
| `nccl` | 多卡全量 | 高带宽 | 需要 NCCL 拓扑；NPU 上走 HCCL 实现 |
| `delta_sharded` | 微调/LoRA/大模型分片 | 省带宽/显存 | 需要 snapshot，实现复杂 |
| `kimi_ckpt_engine` | Kimi 传输栈 | 特定优化 | 依赖外部系统 |
| `mooncake`/`nixl` | KV/RDMA/p2p | 高性能 | 依赖外部系统 |

MiniVerl 额外提供 `delta`（整 tensor diff）用于教学对照；真实配置里请写 `delta_sharded`。

经验：

- 全参训练、每步都变：`nccl`；
- LoRA/稀疏更新：`delta_sharded`（MiniVerl 教学版叫 `delta`）；
- 大规模 MoE：`delta_sharded` 或专用后端；
- 单机调试：`naive`。

## 14.8 同步正确性的检查

权重同步错了，训练不会报错，只会“rollout 输出和 actor 不一致”。检查方法：

1. 固定一个 batch，比较 actor 与 rollout 的 logits；
2. 计算 `rollout_log_probs` 与 `old_log_probs` 的差；
3. 检查所有参数名是否都被同步；
4. 检查 dtype（bf16/fp16/fp32）是否一致；
5. 检查 TP/PP 布局转换是否可逆；
6. 检查 sleep/wake 后是否加载了最新版本。

MiniVerl 的 `test_rollout_checkpoint.py` 与 `test_engine_parallel.py` 覆盖了布局 roundtrip、delta 字节数与 sleep/wake。

## 14.9 一次同步的字节账

以 7B、bf16、8 个 rollout 副本为例：

```text
全量: 7e9 * 2 = 14 GB
8 副本: 112 GB/次
1000 步: 112 TB
```

如果每步只有 1% 参数变化：

```text
delta: 140 MB/副本
8 副本: 1.12 GB/次
1000 步: 1.12 TB
```

这就是为什么 delta 同步在微调/LoRA 场景下价值巨大。MiniVerl 的 `CheckpointEngineManager` 会打印 `bytes_sent` 与 `changed_ratio`，可以直接观察这个比例。

## 14.10 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| 同步后输出不变 | 只同步了部分参数 | 检查参数名与 `get_per_tensor_param` |
| 同步后输出乱码 | 布局转换错 | 对比 HF logits、用 model merger 校验 |
| 同步很慢 | 全量 + 多副本 | 换 nccl/delta_sharded、减少副本 |
| sleep 后 wake 失败 | 备份状态不完整 | 保存完整 state dict 与配置 |
| 异步下 staleness 过大 | 同步频率太低 | 调 `parameter_sync_step` |
| NCCL hang | 进程组/拓扑问题 | 检查 `nccl_timeout`、IB、rank |

## 14.11 MiniVerl 对照

- `mini_verl/sharding.py::CheckpointEngineManager`：naive/nccl/delta/delta_sharded；
- `mini_verl/sharding.py::tp_shard_state_dict/tp_gather_state_dict`：TP 布局；
- `mini_verl/sharding.py::fsdp_shard/fsdp_gather`：flat shard；
- `mini_verl/rollout.py::NaiveRollout.update_weights/sleep/wake_up`：生命周期；
- `mini_verl/trainer/ppo_trainer.py::_sync_weights`：训练循环里的同步点。

MiniVerl 的同步是 Python 拷贝，不经过 NCCL；它记录字节数与版本号，用于教学和测试。

## 14.12 delta 同步的工程实现：稀疏 gather、桶传输与正确性

delta 同步不是“比较两个 state_dict 然后发一个 diff”这么简单。一个可用的实现要回答四个问题：**比什么、按什么粒度发、怎么应用到推理副本、如何证明没有漏参**。

### 四个步骤

1. **snapshot**：在上一次同步后保存一份参数快照。快照可以是完整 tensor，也可以是分片后的 flat buffer；关键是它必须与当前训练布局一致。
2. **diff**：逐 tensor 比较。全参训练几乎每个 tensor 都变，delta 没有收益；LoRA/稀疏更新只有 adapter 或少数专家变，收益才明显。
3. **sparse gather**：把变化元素按推理侧需要的 HF 坐标重排。训练侧的 TP/PP/EP 分片与推理侧的 TP 分片不是同一套坐标，所以“变化的 shard”不能直接拷过去，必须先 gather/reshape。
4. **apply + version**：推理副本按 tensor 或元素合并，版本号加一。版本号必须与训练侧 snapshot 对应，否则下一次 diff 会基于错误的基线。

### 三种粒度

| 粒度 | 发送内容 | 带宽 | 实现复杂度 |
|---|---|---|---|
| tensor | 变化的完整 tensor | 中 | 低 |
| element | 变化元素的索引 + 值 | 最低 | 高 |
| shard | 变化的 shard（不 materialize 完整 tensor） | 低 | 最高 |

tensor 粒度的代价可以用一个简单公式估算：

$$
\text{wire bytes}=\sum_{i\in\text{changed}}\text{numel}_i\times\text{bytes\_per\_elem}
+\text{metadata}.
$$

element 粒度则是：

$$
\text{wire bytes}=\text{changed\_elements}\times(\text{bytes\_per\_elem}+\text{index\_bytes}).
$$

LoRA 场景下，adapter 只占全参数的百分之几，element 粒度可以把每步同步从 GB 级降到 MB 级；但索引与稀疏 scatter 的 CPU 开销可能反而成为瓶颈，所以生产实现通常在“tensor 粒度 + 只同步 adapter”和“element 粒度”之间做取舍。

### 正确性不变量

delta 同步最容易出的不是性能问题，而是静默错误：

- **基线一致**：训练侧 snapshot 与推理侧当前权重必须来自同一版本；中途 wake/sleep 或重启会破坏这个前提。
- **参数名完整**：diff 的 key 集合要覆盖推理侧所有参数；漏掉一个 norm 或 bias，输出只会“稍微不对”。
- **dtype 一致**：bf16 参数与 fp32 副本比较时，`!=` 会把舍入误差当成变化，导致每步全量发送。
- **布局可逆**：sparse gather 后的 HF 坐标必须能无歧义地 scatter 回推理布局；fused QKV、gate/up 融合是常见坑。
- **顺序一致**：多 rank 的 delta 必须按相同顺序应用，否则同一 tensor 的多个分片会互相覆盖。

### 桶传输与重叠

全量同步可以把参数切成固定大小的 bucket，边导边发，减少显存峰值；delta 同步也可以按 tensor 分桶，但桶内元素可能稀疏，压缩率取决于实现。工程上常见的组合是：

- 训练侧用 `get_per_tensor_param_delta_shard` 产出稀疏 delta；
- 传输层用 NCCL/共享内存/对象存储，按 bucket 流水线；
- 推理侧用后台线程 apply，尽量与下一次生成重叠；
- 控制器只等待“版本可见”信号，不等待每个 tensor 的 ACK。

### MiniVerl 的对应实现

`mini_verl/sharding.py::CheckpointEngineManager` 现在按后端区分：

- `delta`：只把变化的 tensor 传给 `update_weights_partial`，rollout 用 `load_state_dict(strict=False)` 原地合并；
- `delta_sharded`：按变化元素数估算线缆字节（含索引开销），模拟 element 粒度；
- `naive`/`nccl`：仍走全量 `update_weights`。

`tests/test_rollout_checkpoint.py::test_delta_checkpoint_engine_applies_partial_update` 验证了“第二次同步只传变化 tensor，且推理副本确实更新”；`test_engine_parallel.py::test_checkpoint_engine_delta` 验证字节数下降。真实 verl 的 delta 引擎还要处理分片、dtype 与多副本，但“snapshot → diff → sparse gather → apply → version”这条链是一样的。

## 本章小结

- 权重同步是 RL 的主要系统成本之一，字节数 = 参数量 × dtype × 副本数。
- 训练布局与推理布局不同，需要 reshard；3D-HybridEngine 的目标是避免完整 gather。
- v0.9.1 用 `get_per_tensor_param*` + checkpoint engine 抽象同步。
- 真实后端是 naive/nccl/delta_sharded/nixl/mooncake/kimi_ckpt_engine；MiniVerl 另有一个教学用的 `delta`。
- sleep/wake 与异步模式需要仔细协调，否则会生成旧权重或崩溃。

## 延伸阅读

- `verl/checkpoint_engine/base.py`
- `verl/workers/engine/base.py`
- `verl/workers/engine/spec.py`
- 附录 V：量化权重（FP8/FP4/MXFP4）的同步生命周期
- 附录 W：checkpoint engine 后端、delta 同步与分离部署
- 下一章：Rollout 引擎：vLLM、SGLang、HF 与 server 模式。

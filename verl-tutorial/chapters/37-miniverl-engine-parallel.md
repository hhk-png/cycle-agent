# 第 37 章 MiniVerl 的引擎、分片与并行模拟

## 37.1 BaseEngine

`mini_verl/engine.py::BaseEngine` 定义：

- `initialize()`：创建优化器；
- `train_mode()` / `eval_mode()`；
- `forward_backward_batch(data, loss_fn, forward_only)`；
- `train_batch()`；
- `infer_batch()`；
- `get_gradients()` / `set_gradients()` / `finalize_update()`；
- `get_per_tensor_param()`；
- `save_checkpoint()` / `load_checkpoint()`；
- `to(device)`；
- `memory_bytes()`。

这与真实 verl 的 `BaseEngine` 接口对应，只是张量是 NumPy。

## 37.2 优化器

`AdamW`：

```python
m = beta1*m + (1-beta1)*g
v = beta2*v + (1-beta2)*g*g
m_hat = m / (1-beta1^t)
v_hat = v / (1-beta2^t)
p -= lr * (m_hat/(sqrt(v_hat)+eps) + wd*p)
```

`clip_gradients` 在 step 前裁剪全局范数。测试 `test_adamw_reduces_quadratic` 验证优化器能收敛到最优点。

## 37.3 梯度同步

`LocalWorkerGroup._sync_gradients_and_step`：

1. 收集每个 worker 的 `get_gradients()`；
2. 按参数名 stack 并平均；
3. `set_gradients` 回每个 worker；
4. `finalize_update()`（裁剪 + step + scheduler）；
5. `post_update_metrics()` 合并 grad_norm/lr。

这是 MiniVerl 版的 all-reduce。真实 verl 在 FSDP 内部做 reduce-scatter，在 Megatron 内部做 TP/PP 通信；语义都是“全局梯度一致”。

## 37.4 FSDP/TP shard

`sharding.py`：

- `fsdp_shard/fsdp_gather`：flat tensor 分片/合并；
- `tp_shard_state_dict/tp_gather_state_dict`：布局无关的往返（按 2D 轴切/合）；
- `CheckpointEngineManager`：naive/nccl/delta/delta_sharded。

**边界**：`tp_shard_state_dict` 不是 Megatron 的真实 TP 布局。Megatron 对 embedding、`lm_head`、bias 有列/行/词表并行的专门规则，而这里只做“切得开、合得回”的往返验证，用来测试 checkpoint engine 的契约。要模拟真实布局，需要 per-module 的 `ShardSpec`（见附录 M）。

测试：

- `test_fsdp_shard_gather`
- `test_tp_shard_gather`
- `test_checkpoint_engine_delta`

它们验证了布局转换的可逆性与 delta 字节数。

## 37.5 TP/PP/MoE/Ulysses

`parallel.py`：

### ColumnParallelLinear

```python
weight_shards = split(weight, tp_size, axis=1)
local = x @ weight_shards[rank] + bias_shards[rank]
out = concat(local_outputs, axis=-1)
```

### RowParallelLinear

```python
input_shards = split(x, tp_size, axis=-1)
weight_shards = split(weight, tp_size, axis=0)
partial = input_shards[rank] @ weight_shards[rank]
out = sum(partials) + bias
```

### PipelineSchedule

气泡比例：

$$
\text{bubble}=\frac{p-1}{m+p-1}.
$$

`timeline()` 返回每个 micro-batch/stage/phase 的时间槽，用于展示“哪些槽存在”；它**不是**完整的 1F1B 交错调度，makespan 不能直接拿去和 bubble 公式对拍。`bubble_ratio()` 是独立的理论估算。真实 1F1B/zero-bubble 的重叠语义见第 12.4 节。

### MoE

`moe_route(router_logits, top_k, capacity_factor, expert_outputs)`：

- softmax router；
- top-k；
- capacity 限制；
- 丢弃统计；
- 加权合并。

### Ulysses

`ulysses_split/merge` 按序列切分/还原；`sequence_parallel_mlp` 验证按序列切分后与 dense 等价。

## 37.6 显存估算

`estimate_memory(num_parameters, bytes_per_param, world_size, zero_stage, ...)`：

- 参数；
- 梯度；
- AdamW 状态；
- 激活；
- KV cache。

它用简化系数，目的是展示量级与 ZeRO 分片的效果。真实显存要用 profiler。

## 37.7 为什么用 dense replica 模拟 FSDP

真实 FSDP 分片参数，MiniVerl 每个 worker 持有完整模型。这样：

- 代码简单；
- 训练数学与 FSDP 一致（梯度平均）；
- 显存不真实，但可以通过 `estimate_memory`/`fsdp_shard` 单独演示。

这是教学取舍：先保证“更新语义”正确，再讲“显存布局”。

## 37.8 一个并行例子

```python
x = np.random.randn(4, 8)
w1 = np.random.randn(8, 16)
b1 = np.zeros(16)
w2 = np.random.randn(16, 8)
b2 = np.zeros(8)

col = ColumnParallelLinear(w1, b1, tp_size=4)
row = RowParallelLinear(w2, b2, tp_size=4)
dense = np.maximum(x @ w1 + b1, 0) @ w2 + b2
tp = row.forward(np.maximum(col.forward(x), 0))
assert np.allclose(tp, dense, atol=1e-5)
```

`examples/16_parallel_patterns.py` 运行四个模式的完整输出。

## 37.9 AdamW 的偏差修正、权重衰减与更新顺序

`mini_verl/engine.py::AdamW.step_named` 的更新可以拆成四步：

$$
\begin{aligned}
m_t &= \beta_1 m_{t-1} + (1-\beta_1)g_t,\\
v_t &= \beta_2 v_{t-1} + (1-\beta_2)g_t^2,\\
\hat m_t &= \frac{m_t}{1-\beta_1^t},\qquad \hat v_t=\frac{v_t}{1-\beta_2^t},\\
\theta_t &= \theta_{t-1} - \eta\left(\frac{\hat m_t}{\sqrt{\hat v_t}+\epsilon}+w\,\theta_{t-1}\right).
\end{aligned}
$$

四个实现细节：

1. **`t` 从 1 开始。** 代码用 `self.step_count + 1` 做偏差修正；如果先 `step_count += 1` 再算，第一次更新会用 $t=2$，动量被高估。
2. **`m/v` 按参数名保存。** `state` 是 `dict[name] -> {"m","v"}`，与 `named_parameters()` 的顺序解耦；checkpoint 里也按名字存，恢复时不会错位。
3. **冻结参数直接跳过。** LoRA 的基座 `requires_grad=False`；如果只把梯度置零而不跳过，weight decay 仍会移动基座。`test_lora` 与优化器测试都覆盖了这一点。
4. **更新顺序是 clip → step → zero_grad。** `finalize_update` 先对全局梯度范数裁剪，再调用 `step_named`，最后清梯度。顺序反过来会让裁剪作用于旧梯度，或者让下一次 forward 累加到未清的梯度上。

`weight_decay` 放在自适应项之后，是 decoupled AdamW 的形式；如果写成 `grad += wd*param` 再进 Adam，权重衰减会被 $1/\sqrt{\hat v}$ 缩放，学习率与衰减的耦合方式就变了。

`current_lr()` 依赖 `optim.total_training_steps`；MiniVerl 在 `PPOTrainer.init()` 里把它设为 trainer 的总步数（MiniVerl 的 `parameter_sync_step` 是同步频率，不是 V1 的子步拆分，所以不乘 S）。如果不接这一行，`cosine`/warmup 会静默退化成常数学习率——这是第二轮审计发现的一个假绿。

## 37.10 显存公式：从参数量到激活与 KV cache

`estimate_memory` 的输出是五个分量：

```text
params      = N * bytes_per_param
grads       = N * bytes_per_param
optimizer   = N * 8        # AdamW 的 m/v 各 4 字节（fp32）
activations = B * T * H * L * bytes * (1 if checkpointing else 8)
kv_cache    = B * S * L * 2 * H_kv * D * bytes
```

再按 ZeRO stage 分片：

- stage 1：只分 optimizer state；
- stage 2：再分 gradients；
- stage 3：再分 parameters。

以 7B、bf16、单卡为例：

```text
params  = 7e9 * 2 = 14 GB
grads   = 14 GB
optim   = 7e9 * 8 = 56 GB
合计    = 84 GB（还没算激活）
```

8 卡 ZeRO-3 把三项都除以 8，得到约 10.5 GB/卡；这也是“7B 全参训练至少 8 卡”的经验来源。激活项与 `B*T*H*L` 成正比，长序列下会迅速超过参数项；gradient checkpointing 把系数从 8 降到 1，代价是重算。

KV cache 的公式里有两个容易漏的因子：

- **2**：K 和 V 各一份；
- **`H_kv` 而不是 `H`**：GQA/MQA 下 KV 头数小于 Q 头数，这是长上下文推理省显存的关键。

数值例子：7B、32 层、8 个 KV 头、head_dim=128、bf16、batch=1、序列 32K：

```text
KV = 1 * 32768 * 32 * 2 * 8 * 128 * 2 ≈ 4.3 GB
```

这只是一条序列；rollout 的并发 batch 会把它乘上去。MiniVerl 的 `estimate_memory` 是规划工具，不含碎片、临时 buffer、通信 buffer 与 CUDA context，真实占用通常更高。

## 37.11 TP/PP 的通信量与气泡

### 列并行与行并行

`ColumnParallelLinear` 把权重按输出维切分：

```python
weight_shards = split(weight, tp_size, axis=1)   # (in, out/tp)
local = x @ weight_shards[rank] + bias_shards[rank]
out = concat(local_outputs, axis=-1)             # all-gather
```

`RowParallelLinear` 把权重按输入维切分，每个 rank 算一个部分和，再一次 all-reduce：

```python
input_shards = split(x, tp_size, axis=-1)
weight_shards = split(weight, tp_size, axis=0)
partial = input_shards[rank] @ weight_shards[rank]
out = sum(partials) + bias                        # all-reduce
```

一个 MLP 的通信量（每层、每 token）大致是：

- 列并行 forward：all-gather 输出，$2\cdot BSH$ 字节（ring 算法近似）；
- 行并行 forward：all-reduce 部分和，$2\cdot BSH$ 字节；
- backward 各一次，量级相同。

所以 TP 的通信量与 `B*S*H` 成正比，与 TP 度数关系不大（ring 实现下）；TP 越大，单卡计算越少，通信占比越高。这就是“TP 通常不超过单机 8 卡”的原因。

### 流水线气泡

`PipelineSchedule.bubble_ratio()` 给出：

$$
\text{bubble}=\frac{p-1}{m+p-1},
$$

其中 $p$ 是 stage 数，$m$ 是 micro-batch 数。$m\gg p$ 时气泡趋近 0；但 $m$ 受限于全局 batch 与显存，不能无限增大。`timeline()` 只是把 forward/backward 的时间槽画出来，**不是**完整的 1F1B 交错调度；章节 12.4 解释了真实 1F1B/zero-bubble 的重叠语义。

一个数值例子：$p=8$、$m=32$，气泡 $=7/39\approx18\%$；把 $m$ 提到 128，气泡降到 $7/135\approx5\%$。代价是每个 micro-batch 更小、GPU 利用率与归一化统计会变化。

### MoE 的 capacity 与丢弃

`moe_route` 的容量：

```python
capacity = ceil(tokens * top_k / experts * capacity_factor)
```

`capacity_factor=1.25` 表示每个专家最多收 1.25 倍平均负载。超过容量的 token 被丢弃（`gates=0`），这会改变前向结果，也会让不同 rank 的 token 数不一致。工程上要监控：

- 每个专家的 `expert_counts` 与容量；
- `dropped_tokens / (tokens*top_k)` 的丢弃率；
- 负载均衡 loss 与 router 熵。

`router replay` 则是在训练步之间固定路由，避免同一 token 在不同 micro-batch 被分到不同专家；它解决的是可复现性与梯度一致性，不是负载均衡。

## 37.12 FSDP 分片、TP 布局与 delta 同步的字节账

### FSDP 的 flat shard

`flatten_state_dict` 把所有参数按名字排序后拼成一个 flat 向量，并记录 `(name, shape, start, end)`：

```python
flat = concatenate([value.reshape(-1) for name, value in sorted(state.items())])
layout = [(name, shape, offset, offset + size), ...]
```

`fsdp_shard` 把 flat 向量按 `world_size` 等分；不能整除时在末尾补零，`fsdp_gather` 拼接后按 layout 切片，自动忽略 padding。这与 FSDP2 的 `fully_shard` 在语义上一致：参数、梯度、优化器状态都可以按 flat buffer 分片，通信是 all-gather/reduce-scatter。

**边界**：MiniVerl 的 `fsdp_shard` 只做“切得开、合得回”的布局往返，不模拟 FSDP 的 prefetch、reshard-after-forward、混合精度 master weights。显存收益要用 37.10 的公式单独估算。

### TP 布局的边界

`tp_shard_state_dict` 用参数名猜切分轴：

- `c_attn`/`fc_in`/`qkv`/`gate` → 输出维（axis 1）；
- `c_proj`/`fc_out`/`lm_head`/`value_head` → 输入维（axis 0）；
- 其它 2D → 默认 axis 1；非 2D 或不能整除 → 复制。

这不是 Megatron 的真实布局：embedding 的词表并行、fused QKV 的重排、bias 的复制/切分都有专门规则。它只保证“按这个规则切完能合回”，用于测试 checkpoint engine 的契约；真实布局要用 `ShardSpec` 与 mbridge/DTensor（附录 M）。

### delta 同步的三种口径

| 后端 | 传输内容 | 字节数 | 适用 |
|---|---|---|---|
| `naive` | 每个 tensor 全量拷贝 | `sum(nbytes)` | 调试、正确性基线 |
| `nccl` | 一次 broadcast 全量 | `sum(nbytes)`（时间模型更优） | 同机/同集群 |
| `delta` | 只传变化的 tensor（整 tensor） | `sum(nbytes of changed tensors)` | 稀疏更新、LoRA |
| `delta_sharded` | 只传变化的元素 + 索引开销 | `changed_elements * 4 * 1.25`，上限为全量 | 大规模稀疏更新 |

`delta_sharded` 的 `1.25` 是索引/元数据的经验系数；实现里还用 `min(bytes_sent, total_bytes)` 保证“元素级传输不会比全量更贵”。`changed_ratio = bytes_sent / total_bytes` 是监控同步是否真的稀疏的关键指标；如果它长期接近 1，delta 没有收益，应该回到 `nccl`。

一个容易忽略的正确性问题：delta 同步要求**上一次的 snapshot 与本次的 state 使用同一套参数名与形状**。如果中途换了模型结构、LoRA rank 或 dtype，`_previous` 里的旧 tensor 会对不上；MiniVerl 用 `np.array_equal` 比较，形状不同会直接进入 `payload`（视为全量），不会静默错位。生产实现要显式校验版本与 shape。

## 本章小结

- MiniVerl 的 Engine 接口与 verl 对应，优化器/梯度同步/checkpoint 都可运行。
- FSDP/TP/PP/MoE/Ulysses 用 NumPy 数值模拟，测试与 dense 对照。
- `sync_gradients` 是显式的 all-reduce 模拟。
- AdamW 的偏差修正、冻结参数跳过、clip→step→zero_grad 顺序决定更新是否等价。
- 显存估算把参数/梯度/优化器/激活/KV 分开算；7B 全参训练的 84 GB 是“至少 8 卡”的来源。
- TP 的通信量与 `B*S*H` 成正比；PP 气泡是 `(p-1)/(m+p-1)`；MoE capacity 会丢弃 token。
- FSDP flat shard 与 TP 名字猜轴只保证布局往返，不是真实 Megatron 布局；delta 的 `changed_ratio` 是判断收益的关键指标。
- dense replica 是教学取舍，换来简单与可调试。

## 延伸阅读

- `mini_verl/engine.py`
- `mini_verl/parallel.py`
- `mini_verl/sharding.py`
- 下一章：Rollout、权重同步与 PPO/GRPO 主循环。

# 第 11 章 FSDP/FSDP2 引擎：参数、梯度与优化器分片

## 11.1 为什么 RL 训练首选 FSDP

FSDP（Fully Sharded Data Parallel）把参数、梯度、优化器状态沿数据并行维切分。相比 DDP 的“每卡一份完整副本”，FSDP 的显存随 world size 下降：

| 状态 | 每参数字节（bf16 + AdamW） | DDP 每卡 | FSDP 每卡（全分片） |
|---|---|---|---|
| 参数（bf16） | 2 | 2 | 2/N |
| 梯度（bf16） | 2 | 2 | 2/N |
| 一阶矩（fp32） | 4 | 4 | 4/N |
| 二阶矩（fp32） | 4 | 4 | 4/N |
| fp32 master 参数（可选） | 4 | 4 | 4/N |
| 合计（含 master） | 16 | 16 | 16/N |
| 合计（不含 master） | 12 | 12 | 12/N |

（N 为 DP 规模；DDP 每卡一份完整副本，FSDP 把每项状态切成 N 份。全组总计：DDP 为 16N，FSDP 为 16。实际还有通信 buffer 与临时 all-gather。混合精度 AdamW 通常保留一份 fp32 master 参数用于更新，因此 16 B/参数是更保守的估算；如果框架直接用 bf16 参数做更新，则是 12 B/参数。）

对 7B 模型：

- 全量 bf16 参数约 14 GB；
- 梯度约 14 GB；
- AdamW m/v 约 56 GB（7B × 8 B）；
- 若保留 fp32 master 参数，再加约 28 GB；
- 合计约 112 GB（含 master）或 84 GB（不含），单卡 80 GB 都放不下；
- 8 卡 FSDP 后每卡约 14 GB（含 master）或 10.5 GB（不含），加上激活与通信 buffer 可行。

很多文档把“m/v + master”笼统写成“优化器状态”，读数字时先确认它到底包含哪几项，否则 56 GB 与 84 GB 会混用。

RL 还要同时放 rollout 引擎（vLLM/SGLang）和参考模型，所以 FSDP 的显存优势非常关键。

## 11.2 FSDP1 与 FSDP2

PyTorch 有两代 FSDP：

- **FSDP1**：基于 `FlatParameter`，把一个 module 的参数展平成一个 flat tensor，再分片；
- **FSDP2**：基于 `DTensor` 与 `fully_shard`，逐参数分片，支持更灵活的组合与更少的重分片开销。

verl 同时支持 `fsdp` 与 `fsdp2`，通过 `actor.strategy` / `engine.strategy` 选择。v0.9.1 的 `FSDPEngineWithLMHead`/`FSDPEngineWithValueHead` 封装了：

- 模型构建（HF + monkey patch）；
- 优化器与 LR scheduler；
- `fully_shard` / `FSDP` 包装；
- 前向/反向；
- checkpoint 保存/加载；
- 权重导出。

`verl/workers/engine/fsdp/transformer_impl.py` 是主要实现；`fsdp_turbo_impl.py` 是面向更高吞吐的实验实现。

## 11.3 关键配置

```yaml
actor_rollout_ref:
  actor:
    strategy: fsdp2
    fsdp_config:
      param_offload: false
      optimizer_offload: false
      offload_policy: false
      reshard_after_forward: true
      fsdp_size: -1
      forward_prefetch: false
      model_dtype: fp32
      use_orig_params: false
      seed: 42
      full_determinism: false
      ulysses_sequence_parallel_size: 1
      use_torch_compile: true
```

### `reshard_after_forward`

- `true`：前向后立即释放 all-gather 的参数分片，显存低、通信多；
- `false`：保留完整参数到反向，显存高、通信少。

RL 训练通常 `true`，因为要同时容纳 rollout/ref。

### `param_offload` / `optimizer_offload`

把参数或优化器状态放到 CPU，需要时再搬回 GPU。它能把 80 GB 卡跑 32B 模型，但会显著降低吞吐。典型用法：

- 单卡调试大模型；
- colocate 时给 rollout 让显存；
- 长序列训练。

### `fsdp_size`

- `-1`：使用整个 world size；
- 正整数：组成 FSDP group 的大小，其余维可用于 TP/PP 或 HSDP。

### `use_orig_params`

FSDP1 的参数展平后，优化器看到的是 flat parameter。`use_orig_params=true` 让优化器看到原始参数，便于与 LoRA、参数分组、部分冻结配合，但有额外开销。

### `ulysses_sequence_parallel_size`

Ulysses 序列并行：把序列维切到多个 rank，每个 rank 处理一段 token，attention 处做 all-to-all。它降低长序列的激活显存，但增加通信。

## 11.4 前向/反向到底发生了什么

以一个 Transformer block 为例，FSDP 的前向：

1. 对当前 block 做 all-gather，得到完整参数；
2. 计算 block；
3. 如果 `reshard_after_forward=true`，释放完整参数，只保留分片；
4. 继续下一个 block；
5. 反向时重新 all-gather，计算梯度，reduce-scatter 到分片；
6. 优化器只更新本地分片。

这带来“通信与计算重叠”的优化空间：

- `forward_prefetch=true`：提前 all-gather 下一个 block；
- `use_torch_compile=true`：编译计算图，减少 launch 开销；
- bucket 化 reduce-scatter：把小张量合并，提升带宽利用率。

## 11.5 混合精度与 dtype

verl 的 FSDP 配置里有 `model_dtype`、`dtype`、`use_orig_params` 等字段。典型组合：

- 参数/梯度 bf16；
- 优化器状态 fp32；
- LayerNorm/softmax 用 fp32；
- rollout 用 bf16 或 fp16。

bf16 比 fp16 动态范围大，RL 训练更稳；fp16 在某些推理引擎上更快。混用时要注意：

- loss scaling；
- KL 计算的数值稳定性；
- log-prob 的精度会放大比率误差；
- 不同后端的 dtype 不一致会导致权重同步后 logits 偏移。

## 11.6 激活显存与重计算

FSDP 只分片参数/梯度/优化器，**激活**仍然是每卡的大头。对长序列：

$$
\text{activations}\approx B\cdot T\cdot H\cdot L\cdot \text{bytes}\cdot k
$$

其中 $k$ 取决于是否保存 attention 中间量、是否用 flash attention、是否重计算。

`enable_gradient_checkpointing=true` 用时间换显存：前向不保存中间激活，反向重新计算。它通常能把激活降到 1/3～1/5，但训练慢 20%～40%。

## 11.7 FSDP 与 rollout 的显存竞争

colocate 模式下，训练和 rollout 共享 GPU：

- 训练需要参数分片、梯度、优化器、激活；
- rollout 需要推理权重、KV cache、CUDA graph。

常见策略：

1. 训练时 `sleep` rollout，释放 KV cache 与权重；
2. 同步权重时只保留推理需要的 TP 布局；
3. `gpu_memory_utilization` 给 rollout 设上限；
4. `param_offload`/`optimizer_offload` 给训练让显存；
5. 用 FSDP2 `reshard_after_forward` 降低峰值。

如果仍然 OOM，优先降 `ppo_micro_batch_size_per_gpu`，再降 `max_response_length`，最后才考虑 offload。

## 11.8 一个显存估算例子

7B 模型，bf16，AdamW，8 卡 FSDP2：

```text
参数（bf16）:        7e9 * 2 / 8  = 1.75 GB/卡
梯度（bf16）:        7e9 * 2 / 8  = 1.75 GB/卡
优化器 m/v（fp32）:  7e9 * 8 / 8  = 7.0 GB/卡
fp32 master（如启用）: 7e9 * 4 / 8 = 3.5 GB/卡
小计（含 master）:                    14.0 GB/卡
小计（不含 master）:                  10.5 GB/卡
激活（B=4, T=4096, H=4096, L=32）: 数 GB～十几 GB
rollout KV cache: 取决于并发与长度
```

这只是量级。真实峰值还受 all-gather buffer、CUDA context、碎片影响。MiniVerl 的 `estimate_memory` 用同样的思路给出粗略数字。

## 11.9 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| 前向 OOM，反向不 OOM | `reshard_after_forward` 与激活 | 开重计算，降 micro batch |
| 反向 OOM | 保存了过多激活 | 开 gradient checkpointing |
| 训练很慢但 GPU 利用率低 | all-gather 通信瓶颈 | 调 bucket、prefetch、TP/FSDP 组合 |
| loss 出现 NaN | fp16/bf16 溢出、KL 太大 | 用 bf16、降 lr/KL、检查 logits |
| checkpoint 加载后效果变差 | 分片布局/优化器状态不匹配 | 用同一 world size 或 reshard |
| 多机 rank 错位 | placement group/IP 变化 | 固定节点亲和、共享存储 |

## 11.10 MiniVerl 的 FSDP 模拟

MiniVerl 的 `FSDPEngine` 不做真正的参数分片：每个 worker 持有完整模型，worker group 对梯度做平均。这是“语义上的 FSDP”：

- 用户看到的更新结果与数据并行一致；
- 显存不是分片后的；
- 通信是 Python 层的数组平均，不是 NCCL。

`mini_verl/sharding.py::fsdp_shard/fsdp_gather` 单独演示了 flat shard 与 gather；`tests/test_engine_parallel.py` 验证了 roundtrip。`estimate_memory` 演示了 ZeRO-1/2/3 的显存公式。

## 本章小结

- FSDP 把参数、梯度、优化器状态沿 DP 维切分，是 RL 训练的首选。
- FSDP2 基于 DTensor，逐参数分片，逐渐替代 FSDP1。
- `reshard_after_forward`、offload、gradient checkpointing 是三个主要显存杠杆。
- 激活显存与 rollout KV cache 是 colocate 的主要竞争点。
- MiniVerl 用 dense replica + gradient all-reduce 模拟 FSDP 语义，并单独演示 flat shard。

## 延伸阅读

- PyTorch FSDP2 文档：<https://pytorch.org/docs/stable/distributed.fsdp.fully_shard.html>
- `verl/workers/engine/fsdp/transformer_impl.py`
- `verl/workers/config/engine.py`
- 下一章：Megatron 引擎与 TP/PP/CP/EP。

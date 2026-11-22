# 第 13 章 序列并行、remove padding 与变长批

## 13.1 padding 浪费了多少算力

一个 batch 里响应长度差异很大：

```text
sample 0: 32 tokens
sample 1: 512 tokens
sample 2: 64 tokens
sample 3: 4096 tokens
```

如果按最长样本 4096 padding，前三个样本分别浪费 99.2%、87.5%、98.4% 的 token 计算。对推理模型，长尾更严重：大多数样本几百 token，少数几万 token。

padding 浪费不仅是算力，还会改变 batch 的有效大小：

- 有效 token 数远小于 `B × T`；
- 梯度被 padding 稀释；
- 显存被 padding 的激活占满。

## 13.2 三种解决思路

### 思路一：按长度分桶

把长度接近的样本放在同一个 batch：

```python
sorted_indices = argsort(lengths)
for bucket in chunks(sorted_indices, batch_size):
    ...
```

优点：实现简单；缺点：长度分布会随时间变化，bucket 边界难调。

### 思路二：动态 batch（按 token 数）

不固定 batch size，而是固定 token 预算：

```text
max_tokens_per_gpu = 16384
每次取样本直到累计 token 数接近预算
```

verl 的 `use_dynamic_bsz=true` 与 `ppo_max_token_len_per_gpu` 就是这种。它比固定 batch 更稳，尤其在长尾明显时。

### 思路三：remove padding（varlen）

把 batch 内所有有效 token 拼成一维，用 cu_seqlens 记录每条序列的边界：

```text
tokens: [a b c d | e f | g h i j k]
cu_seqlens: [0, 4, 6, 11]
```

Flash Attention 的 varlen 接口直接接受这种布局，不做 padding。verl 的 `use_remove_padding=true` 走这条路；`verl/trainer/ppo/padding_utils.py` 与 `verl/utils/seqlen_balancing.py` 提供辅助。

优点：零浪费；缺点：

- 需要模型/attention 支持 varlen；
- position_ids 要按序列重置；
- loss 聚合要按有效 token 数；
- 与 TP/PP/CP 组合时更复杂。

## 13.3 左 padding 还是右 padding

因果 LM 的 padding 位置会影响 attention：

- 右 padding：pad token 在序列末尾，前面的 token 看不到它们；但如果 attention mask 没处理好，pad 位置仍会产生 logits。
- 左 padding：pad token 在开头，所有真实 token 都能看到它们；必须用 attention_mask 屏蔽。

推理引擎通常用左 padding（便于 batch 生成）；训练通常用右 padding（便于计算 next-token）。混用时必须保证 position_ids 与 attention_mask 一致。

verl 的 `padding_utils.py` 与 `maybe_fix_3d_position_ids` 处理这些边界。

## 13.4 position_ids 的坑

对每条序列，position_ids 应该从 0 开始，而不是沿用 padding 后的全局位置。否则模型会把“第 100 个 token”误认为位置 100，即使它实际是序列的第 3 个 token。

在 varlen 布局中，position_ids 也要按 `cu_seqlens` 重置。MiniVerl 的 `TinyTransformer.trunk` 对每个 batch 单独生成 0..T-1，避开了这个问题；真实 verl 需要显式处理。

## 13.5 序列并行的通信

Ulysses 序列并行：

1. 输入按序列切成 `sp_size` 段；
2. attention 前做 all-to-all，把“序列分段”变成“head 分段”；
3. 每个 rank 计算自己那部分 head 的 attention；
4. attention 后再 all-to-all 回来；
5. LayerNorm/MLP 仍按序列段计算。

它降低单卡激活，但增加两次 all-to-all。适合长序列、TP 已经用满的场景。

MiniVerl 的 `ulysses_split/merge` 只演示切分/还原；`sequence_parallel_mlp` 演示按序列切分后 dense 等价。

## 13.6 长度均衡

即使做了动态 batch，DP rank 之间的 token 数仍可能不均：

- rank 0 拿到 3 条长序列；
- rank 1 拿到 8 条短序列；
- rank 0 成为 straggler，其他 rank 等它。

verl 的 `trainer.balance_batch=true` 走 `verl/utils/seqlen_balancing.py`，调用 `karmarkar_karp(...)`：这是 **Karmarkar–Karp differencing / Largest Differencing Method（LDM）**，一种近似最优的多路数划分启发式，不是简单的 LPT 贪心。简单的 `position % world_size` 交错并不能最小化最大 rank 和，在长尾分布上仍会留下 straggler。MiniVerl 的 `_balance_batch` 用堆实现 LPT 作为教学替代，二者分区结果不保证一致：

```python
order = argsort(-lengths)
heap = [(0.0, rank) for rank in range(world_size)]
heapify(heap)
for index in order:
    load, rank = heappop(heap)
    groups[rank].append(index)
    heappush(heap, (load + lengths[index], rank))
```

实现时还要给每个 rank 设固定容量（前 W−1 个 `ceil(N/W)`，最后一个拿余数），这样重排后的分组边界才与 dispatch 的等分 chunk 对齐；否则指标会“看起来均衡”，实际 rank 负载并不对应。MiniVerl 的 `_balance_batch` 用容量约束的 LPT，并记录 `data/seqlen_imbalance`。

## 13.7 loss 聚合与变长批

动态 batch 后，每个 micro-batch 的 token 数不同。`token-mean` 必须用全局 token 数归一，而不是每个 micro-batch 各自平均。verl 的 `agg_loss` 通过 `batch_num_tokens` 传入全局值；MiniVerl 在 worker 里用 `data.meta_info["global_num_tokens"]`。

如果实现错了，长序列多的 batch 会有更大权重，训练目标悄悄偏向长回答。

## 13.8 配置建议

```yaml
actor_rollout_ref:
  actor:
    use_dynamic_bsz: true
    ppo_max_token_len_per_gpu: 16384
    ppo_micro_batch_size_per_gpu: null
  ref:
    log_prob_use_dynamic_bsz: true
    log_prob_max_token_len_per_gpu: 16384
  model:
    use_remove_padding: true
```

经验：

- 7B、序列 < 4K：先开 dynamic bsz，remove padding 可选；
- 32B、序列 8K～32K：remove padding + Ulysses；
- 长尾极明显：dynamic bsz + 长度均衡 + 过滤超长样本；
- 调试阶段：先关 remove padding，确认 mask 正确，再开优化。

## 13.9 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| 开了 remove padding 后 loss 异常 | position_ids 未重置 | 检查 `cu_seqlens` 与 position_ids |
| 训练比不开还慢 | varlen 内核未生效 | 确认 attention 实现与 dtype |
| DP rank 等待严重 | 长度不均衡 | 开 `balance_batch` |
| 长序列 OOM | 激活未分片 | Ulysses/CP/重计算 |
| 验证结果与训练不一致 | 左/右 padding 混用 | 统一 padding 侧与 mask |

## 13.10 MiniVerl 对照

- `mini_verl/trainer/ppo_trainer.py::_balance_batch`：长度均衡；
- `mini_verl/parallel.py::ulysses_split/merge`：序列切分；
- `mini_verl/parallel.py::sequence_parallel_mlp`：序列并行 MLP；
- `mini_verl/engine.py::estimate_memory`：激活显存估算；
- `mini_verl/data.py::collate_fn`：固定长度 padding（教学简化）。

MiniVerl 默认不用 varlen，因为 TinyTransformer 的 attention 是手写的；它用固定 `max_prompt_length + max_response_length`，把重点放在 mask 与 dispatch 上。要扩展 varlen，需要 `cu_seqlens` 感知的 attention kernel 与 position_ids 重置；第 32 章讨论真实 verl 的 remove padding 与 attention backend，MiniVerl 有意不实现这一层。

## 本章小结

- padding 会浪费大量算力，长尾任务尤其严重。
- 动态 batch、长度均衡、remove padding 是三种互补手段。
- position_ids、attention_mask、loss 聚合是变长批的三个高频错误点。
- Ulysses 用 all-to-all 换激活显存，适合长序列。
- MiniVerl 用固定 padding + 长度均衡演示核心语义。

## 延伸阅读

- Flash Attention varlen：<https://github.com/Dao-AILab/flash-attention>
- `verl/trainer/ppo/padding_utils.py`
- `verl/utils/seqlen_balancing.py`
- 下一章：权重同步与 3D-HybridEngine。

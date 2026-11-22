# 第 06 章 张量并行：把一层矩阵切开

## 6.1 核心问题

一个线性层 `Y = XW + b`，`X` 的形状是 `[b·s, h]`，`W` 是 `[h, 4h]`。单卡放不下
或算不快时，怎样切 `W`，才能让每个 rank 只算一部分，同时前向/反向仍然正确？

Megatron 的答案不是“随便切”，而是两种互补的切法：

- **Column Parallel**：切输出维度，每个 rank 算一部分列；
- **Row Parallel**：切输入维度，每个 rank 算一部分行，最后求和。

这两个算子可以组合出 MLP 和 attention，使一次前向只引入少量集合通信。

## 6.2 Column Parallel Linear

设 TP size 为 `t`，rank 为 `r`。把 `W` 按输出维切成 `t` 份：

```text
W = [W_0, W_1, ..., W_{t-1}]      # 每份 [h, out/t]，按输出维(dim=1)拼接
Y_r = X W_r + b_r                  # 每份 [b·s, out/t]
Y = [Y_0, Y_1, ..., Y_{t-1}]      # 按最后一维拼接
```

前向：

- 输入 `X` 是完整的（或经过序列并行 all-gather）；
- 每个 rank 只算自己的 `out/t`；
- 如果下游需要完整输出，就 all-gather；Megatron 通常让下游 Row Parallel
  直接消费分片，避免显式拼接。

前向/反向的矩阵推导（含梯度 all-reduce 为什么必要）见 [附录 A](A-math.md) 的 A.1。

反向：

```text
dW_r = dY_r^T X
dX_r = dY_r W_r^T
dX = Σ_r dX_r        # 必须 all-reduce！
```

**这是最容易漏的一步。** 前向每个 rank 的输出是独立的列，loss 看起来完全正确；
但 `X` 的梯度是各 rank 贡献之和。如果只在 Row Parallel 做前向 all-reduce，
而 Column Parallel 的反向不做 `dX` all-reduce，梯度会少掉其他 rank 的贡献，
训练仍然能跑，却是错的。

mini 的 `_ColumnParallelLinearFn` 明确做了这件事：

```python
if ctx.needs_input_grad[0]:
    grad_input = grad_output.matmul(weight)
    if ctx.world > 1:
        grad_input = all_reduce(grad_input, group=ctx.group)
```

并且 `tests/parallel_equiv.py` 把 TP=1 与 TP=2 的 `q_proj`/`out_proj` 梯度逐元素
对比，确保不是“loss 对、梯度错”。

## 6.3 Row Parallel Linear

把 `W` 按输入维切成 `t` 份：

```text
W = [W_0; W_1; ...; W_{t-1}]      # 每份 [h/t, out]，按输入维(dim=0)拼接
Y_r = X_r W_r                      # X_r 是 [b·s, h/t]
Y = Σ_r Y_r + b                    # all-reduce
```

前向需要一次 all-reduce（或 sequence parallel 的 reduce-scatter）。反向：

```text
dX_r = dY W_r^T
dW_r = dY^T X_r
```

`dX_r` 只属于本地输入分片，不需要再 all-reduce；`dW_r` 也只对应本地列。偏置 `b`
是复制的，普通 TP 下每个 rank 加一次即可；序列并行下偏置只在本地序列块上相加，
梯度需要跨 TP 求和（mini 用 hook 处理，见第 07 章）。

## 6.4 MLP 的标准切法

以 SwiGLU 为例：

```text
gate = SiLU(X W_gate)
up   = X W_up
h    = gate * up
Y    = h W_down
```

把 `W_gate`、`W_up` 做 Column Parallel（输出维度切分），把 `W_down` 做 Row
Parallel（输入维度切分）：

```text
每个 rank:
  gate_r = SiLU(X W_gate_r)     # [b·s, ffn/t]
  up_r   = X W_up_r             # [b·s, ffn/t]
  h_r    = gate_r * up_r
  y_r    = h_r W_down_r          # [b·s, h]
  y      = all_reduce(y_r)       # 一次 all-reduce
```

关键点：`gate` 和 `up` 的 **同一个位置** 必须在同一个 rank 上，否则逐元素乘
`gate * up` 会配错。实现上有两种方式：

1. 分开两个 Column Parallel 层（mini 采用），各自切分输出，位置自然对齐；
2. 融合 gate/up 权重，并按交错布局切分，减少 kernel 数量（生产常用）。

如果简单地把 `2·ffn` 输出按连续块切给两个 rank，rank 0 拿到全部 gate、rank 1
拿到全部 up，`chunk(2)` 就会错位。这是实现 GLU 时非常常见的坑。

## 6.5 Attention 的切法

Multi-head attention 天然适合 TP：按 **head** 切分。

- Q 投影：`[h, n_h·d]` 按 head 切，每个 rank 得到 `n_h/t` 个 head；
- K/V 投影：按 KV head 切，GQA 时要求 `n_kv` 能被 `t` 整除（或专门复制 KV head）；
- 每个 rank 在自己的 head 上做 attention；
- 输出投影 `[h, h]` 是 Row Parallel，按输入维切；
- 最后 all-reduce 得到完整 hidden。

约束：

```text
n_h % t == 0
n_kv % t == 0     # mini 的要求；生产支持 KV head 复制等更复杂策略
hidden % t == 0
```

如果 `n_kv < t`，不能简单整除。生产 Megatron 会把 KV head 复制到多个 TP rank，
或在更细的粒度上切 `head_dim`；mini 直接在配置校验中拒绝，避免悄悄算错。

## 6.6 词表并行与交叉熵

词表通常很大（32K–256K），输出层 `[h, V]` 是显存和计算的大头。做法：

- `VocabParallelEmbedding`：按词表行切分，每个 rank 只保存 `V/t` 行；
- 输出层 `ColumnParallelLinear`：按词表行切分，每个 rank 输出 `V/t` 个 logits；
- 交叉熵：每个 rank 计算本地 vocab 的 `max`/`sum_exp`，all-reduce 得到全局
  log-sum-exp；目标 token 只在一个 rank 的 vocab 范围内，其他 rank 贡献 0；
- 反向：本地 softmax 减去 one-hot，除以全局有效 token 数。

mini 的 `vocab_parallel_cross_entropy` 还处理了 `ignore_index` 和 CP 的全局
归一化。注意它返回的是 **全局平均 loss**，而不是“每个 rank 的本地平均再平均”；
后者在 CP/变长序列下会算错。

## 6.7 通信量与放置

TP 的通信主要发生在：

- Row Parallel 前向 all-reduce；
- Column Parallel 反向 grad_input all-reduce；
- 序列并行的 all-gather/reduce-scatter；
- 词表 CE 的 log-sum-exp all-reduce。

通信量与 `b·s·h` 成正比，且每个 Transformer 层都要发生。TP 因此对带宽/延迟
非常敏感，通常限制在 **单个 NVLink 域**（如 8 卡 H100）。跨节点 TP 会把
all-reduce 推到 IB，往往得不偿失。

生产 Megatron 会用：

- 融合的 GEMM + all-reduce（如 Apex/TE）；
- 异步 all-reduce、通信与反向重叠；
- 按拓扑分组，避免跨 leaf switch；
- 对 LayerNorm/dropout 使用序列并行降低激活。

## 6.8 1D、2D、2.5D TP

经典 Megatron 是 1D TP：一个矩阵只在一个维度切。对超大 hidden/FFN，1D TP 的
通信量仍然很高。研究与工业界有 2D/2.5D TP：

- 2D TP：把 `t` 个 rank 排成 `t1 × t2` 网格，矩阵按两个维度切，通信量下降；
- 2.5D：结合 batch/序列维度，进一步减少 all-reduce；
- Colossal-AI、Tofu 等系统探索过这些方案。

### 6.8.1 2D TP 的通信账

设 `t = t1 × t2`，把权重看成 `[h, h]` 并切成 `t1 × t2` 的块。一次前向可以
组织成两次“按行/按列”的集合通信（SUMMA 风格）：

```text
每个 rank 每层通信量 ≈ 2 · b · s · h · bpe · (1/t1 + 1/t2)
```

作为对比，1D TP 的 Row Parallel 每层 all-reduce 每卡约
`2·b·s·h·bpe·(t-1)/t ≈ 2bsh·bpe`。取 `t1 = t2 = sqrt(t)`：

```text
2D:   4bsh·bpe / sqrt(t)
1D:   ≈ 2bsh·bpe
```

2D 把每卡通信从 `O(1)` 降到 `O(1/√t)`，代价是：

- 每层有两次同步（行 all-gather + 列 reduce-scatter），延迟更高；
- 需要 2D 的 rank 映射与更复杂的 buffer 布局；
- 小 batch/短序列时，通信启动开销可能吃掉收益。

### 6.8.2 2.5D 与 batch 维

2.5D 在 2D 网格上再复制 `c` 份权重，把 batch 也切开：

```text
rank 数 = t1 × t2 × c
每卡权重 = 1/(t1·t2)        # 参数仍按 2D 切
每卡激活 = 1/(t1·t2·c)      # batch 再切 c 份
```

它的收益主要来自**激活显存**和 all-reduce 的消息大小，而不是参数显存。
`c > 1` 时同一份权重被多个 rank 复制，适合“激活是瓶颈、参数不是”的场景。

### 6.8.3 为什么 Megatron 主线仍是 1D TP

1D TP 的通信模式简单（一次 all-reduce），能与 NVLink/NVSwitch 的
all-reduce kernel、TE 融合 GEMM、CUDA Graph 深度配合；2D/2.5D 的收益在
`t > 8`（跨 NVLink 域）时才明显，而 Megatron 的常规建议是把 TP 限制在
节点内，用 PP/DP/EP 扩展。因此：

- **节点内**：1D TP（≤8）+ SP，是性价比最高的默认；
- **必须跨节点 TP**（超大单层、模型并行域 >8）：才评估 2D/2.5D；
- **研究/特殊硬件**：2D/2.5D、MoE-TP 混合、以及按拓扑定制的 all-reduce。

理解 1D 的通信/梯度语义仍是读懂其他方案的基础；mini 只实现 1D TP，并在
`tests/parallel_equiv.py --case tp` 中验证它。

## 6.9 mini 中的实现与测试

核心文件：

- `mini_megatron/parallel/layers.py`
  - `ColumnParallelLinear`：自定义 autograd，forward 本地 GEMM，backward
    all-reduce `grad_input`；
  - `RowParallelLinear`：forward all-reduce 或 sequence-parallel reduce-scatter；
  - `VocabParallelEmbedding`：mask 本地词表、all-reduce。
- `mini_megatron/parallel/cross_entropy.py`：全局 log-sum-exp。
- `tests/parallel_equiv.py`：TP=2 与 TP=1 的 loss/梯度对比。
- `tests/unit/test_layers.py`：world=1 的基础形状/数值。

一个真实调试记录：最初 `ColumnParallelLinear` 只用 `F.linear`，没有自定义
backward。TP=2 的 loss 与 TP=1 **完全一致**，但 `out_proj` 梯度范数只有参考的
约 0.68 倍。补上 `grad_input` all-reduce 后，梯度误差降到 1e-8。完整病历与
回归断言见 27.5 的 bug 1。这个例子说明：**前向等价不能证明并行实现正确，
必须对比梯度。**

## 6.10 本章小结

- Column 切输出、Row 切输入；两者组合出 MLP/attention。
- Column 的反向必须 all-reduce `grad_input`，最容易漏。
- GLU 的 gate/up 必须在同一 rank 上按位置配对。
- GQA 的 KV head 切分有整除约束；不满足时生产实现会用复制策略。
- TP 通信频繁，应限制在 NVLink 域内。

下一章讲序列并行：它不改变 TP 的数学，但改变激活的布局，是长序列训练的
关键优化。

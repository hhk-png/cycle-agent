# 第 09 章 上下文并行与长序列

## 9.1 当序列成为瓶颈

把模型宽度切给 TP、深度切给 PP 之后，长序列仍然会爆显存：

- 激活随 `s` 线性增长；
- attention score 随 `s²` 增长；
- KV cache 随 `s` 线性增长。

上下文并行（Context Parallel, CP）把**序列维**切到多个 GPU 上。每个 CP rank
只持有 `s/cp` 个 token 的隐藏状态，但 attention 需要看到完整的 K/V。于是问题
变成：怎样在切分序列的同时，正确计算因果注意力和梯度？

## 9.2 三种主流实现

### 9.2.1 All-gather KV（教学版）

每个 rank 持有本地 Q，前向时把所有 rank 的 K/V all-gather 成完整序列，然后
计算本地 Q 对全局 K/V 的 attention：

统一记号：`b`=batch，`n_h`=query head 数，`d_h`=head_dim，
`h = n_h·d_h`=hidden；不要用 `h` 同时表示 head 数和 hidden。

```text
Q_r: [b, n_h, s/cp, d_h]
K,V: [b, n_h, s, d_h]        # all-gather；GQA 时 KV head 更少
O_r = softmax(Q_r K^T / √d_h + mask) V
```

优点：实现简单，容易验证正确性。  
缺点：每个 rank 都复制完整 K/V，K/V 显存**没有**降到 `1/cp`；score 矩阵是
`[b, n_h, s/cp, s]`，也只在一维上降了 `cp`；通信量随 `s` 增长。更准确地说，
CP 让 Q、MLP 激活、LayerNorm 等按序列切到 `1/cp`，但 all-gather-KV 版本把
K/V 的省显存收益吃掉了。mini 采用这种实现，并在章末说明它和生产 ring
attention 的差距。

### 9.2.2 Ring attention

把 K/V 分成 `cp` 个块，让 Q 块在 ring 上流动，每个 rank 依次处理一个 K/V 块，
用 online softmax 累积结果。这样每个 rank 只需要本地 K/V 块和一份流动的 Q
（或反之），KV 显存降到 `1/cp`。注意：ring 的**总通信字节**并不比 all-gather
少（bf16 下每层 K+V 约 `4·b·s·h` 字节，`h=n_h·d_h`，忽略 `(cp-1)/cp`），
优势在单条消息更小、显存更省、可以通信/计算重叠。
生产 Megatron 的 CP 还包含：

- load-balanced causal mask；
- CP indexer（用于稀疏/混合注意力）；
- 与 TP/PP 的二维组合；
- 通信与 attention 计算重叠。

公开 roadmap 中有 64K→1M 上下文的训练课程、CP indexer、降低 indexer/混合
注意力显存等条目。按第 36.1 节的发布分级，这些属于 **L3（路线图）**而非发布承诺；引用时
要带季度标签并注明“未逐条核对 release tag”。

### 9.2.3 一个可运行的 ring attention 参考实现

`mini_megatron/parallel/ring_attention.py` 在单进程里模拟 `cp` 个 rank，
把 ring 的算术、因果 mask 和 online softmax 显式写出来。核心循环是：

```python
m_i = -inf; l_i = 0; acc = 0
for step in range(cp_size):
    j = (r - step) % cp_size          # KV 块在环上倒着传
    if causal and j > r: continue     # 整块未来，跳过
    s = q_r @ k_j.T / sqrt(d) + mask
    m_new = max(m_i, s.max(-1))
    alpha = exp(m_i - m_new)
    p = exp(s - m_new)
    l_i = l_i * alpha + p.sum(-1)
    acc = acc * alpha + p @ v_j
    m_i = m_new
out_r = acc / l_i
```

三个容易写错的点：

1. **`alpha` 必须乘在旧的 `acc` 和 `l` 上**，否则新块会把旧块的贡献覆盖；
2. **`m_i` 初始为 `-inf`** 时，第一块的 `exp(-inf - m_new)=0`，不能写成
   `exp(m_i - m_new)` 后再 `nan_to_num` 掩盖；
3. **因果 mask 用全局位置**：rank `r` 的 query 位置是
   `[r·s/cp, (r+1)·s/cp)`，不是本地 `[0, s/cp)`。

用 `scripts/ring_attention_demo.py` 可以直接对照 dense 参考：

```text
cp=1/2/4/8 的 max_abs_error 约 3.6e-7 ~ 4.8e-7
blocks_per_rank = [1,2,3,4,5,6,7,8]（cp=8）
```

最后一行暴露了 ring 的**负载不均**：因果 attention 下，rank 7 要处理 8 个
KV 块，rank 0 只处理 1 个，尾延迟由最慢的 rank 决定。生产 CP 用
load-balanced causal mask（把 query 区间对半切，前后半段配对）把每个 rank
的块数拉平；这属于调度层优化，不改变上面的数学。测试
`tests/unit/test_ring_attention.py` 还验证了反向梯度与 dense 实现一致
（`atol=2e-5`），因为 ring 的 online softmax 必须对 `m/l/acc` 都可微。

### 9.2.4 Ulysses：用 all-to-all 换布局

Ring 保持“每个 rank 拥有全部 head、只拥有序列块”的布局，让 K/V 块在环上流动。
**Ulysses**（DeepSpeed 提出）反过来：用两次 all-to-all 把布局换成“每个 rank
拥有全部序列、只拥有 head 切片”，然后做一次普通的全序列 attention。

设 `P=cp_size`、序列长 `S`、head 数 `H`、head_dim `d`、batch `B`：

```text
变换前（每 rank）：[B, H, S/P, d]     # 一个序列块，全部 head
第一次 all-to-all：按 head 重排
变换后（每 rank）：[B, H/P, S, d]     # 全部 token，一个 head 切片
attention：对 H/P 个 head 做普通的全局因果 attention
第二次 all-to-all：按序列重排，恢复 [B, H, S/P, d]
```

两次变换互为逆置换。第一次 all-to-all 中，每个 rank 持有全部 H 个 head 的
一个序列块；它把第 r 个 head 切片发给序列块 r 的持有者（每个目的 rank 收到
`H/P` 个切片）。第二次 all-to-all 反过来：每个序列块收集所有 head 切片。
实现上可以把
“重排 + attention + 重排”写成一个纯函数，mini 的
`parallel/ulysses.py` 就是这样做的：

```python
# q/k/v: [B, H, S, D] 全局布局（单进程模拟所有 rank）
# rank r 先拿 head 切片 r 的全序列视图
q_r = cat([q[:, h0:h1, s0:s0+block, :] for s0 in range(0, S, block)], dim=2)
# 普通全序列 attention（因果 mask 用全局位置）
out_r = softmax(q_r @ k_r.transpose(-2, -1) / sqrt(D) + mask) @ v_r
# 第二次 all-to-all：序列块 r_seq 收集所有 head 切片
chunk = cat([out_r[:, :, s0:s0+block, :] for r in range(P)], dim=1)
```

`tests/unit/test_ulysses.py` 验证了 cp=1/2/4 与 dense 参考的 logits 一致、
反向梯度一致、两次布局变换是精确的位级往返，以及不可整除的
`S % P`/`H % P` 会显式报错。`scripts/ulysses_demo.py` 直接打印每个 cp 的
最大误差、往返是否精确以及 per-rank/全组通信元素数，便于和 ring 的账本
对照。

**通信账。** 每个 rank 在第一次 all-to-all 发送 Q/K/V 的
`(P-1)/P`（本地元素），第二次发送输出的 `(P-1)/P`。设每 rank 初始张量为
`B·H·(S/P)·d` 个元素，则每 rank 发送

```text
4 · B·H·(S/P)·d · (P-1)/P  =  4BSHd · (P-1)/P²   个元素
```

全组聚合为 `4BSHd·(P-1)/P`。对照 ring：每个 rank 每层发送 K/V 共
`2·(P-1)·B·H·(S/P)·d`，全组聚合为 `2BSHd·(P-1)`。所以 **Ulysses 的总通信量
约为 ring 的 `2/P`**；P 越大，节省越明显。代价是：

- 每层有 **两次 all-to-all 的固定延迟**，不像 ring 可以把 p2p 与 attention
  计算重叠；
- 每个 rank 持有完整 `S` 的 Q/K/V 视图，attention 若物化 score 矩阵需要
  `O(S²·H/P)` 显存（用 FlashAttention 可避免物化，但仍是全序列计算）；
- 要求 `S % P == 0` 且 `H % P == 0`；GQA 下 KV head 往往只有 8 个，`P>8`
  时 KV 必须复制或另做切分，这是 Ulysses 扩到 64/128 卡时最现实的限制；
- all-to-all 对拓扑和 straggler 敏感：跨节点 all-to-all 的尾延迟会直接暴露
  在关键路径上，通常要求 NVLink 域内或良好的 rail 对齐。

**什么时候选哪一个。**

| 场景 | 更合适 | 原因 |
|---|---|---|
| 中等序列（4K–32K）、head 多、NVLink 域大 | Ulysses | 总通信量低、无因果负载不均 |
| 超长序列（128K–1M）、显存紧 | ring | 不物化全序列、KV 显存降到 `1/P` |
| 跨节点、带宽不均 | ring / 分层 | 环状 p2p 更平滑，易重叠 |
| GQA 且 KV head 少 | ring 或混合 | Ulysses 要求 head 可整除 |
| 追求极限吞吐 | 混合（Ulysses × ring） | head 维和序列维同时切，分摊两种代价 |

需要强调：**Megatron Core 的 CP 实现以 all-gather KV / ring attention 为主**
（见 9.2.1–9.2.3 与 29.2 的代码地图）；Ulysses 主要出现在 DeepSpeed 以及一些
“Ulysses + ring”混合实现中。看到某框架宣称“支持 CP”时，要问清它用的是哪种
布局、KV 是否真的降到 `1/P`、以及 GQA 下 KV head 怎么处理，而不是只看
“CP size”这个数字。

这也解释了一个常见误判：CP 的收益不只是“序列切成 P 份”。all-gather-KV 版本
省了 Q/MLP 激活，却没省 KV；ring 省了 KV，但通信总量不低；Ulysses 省通信，
但要求全序列 attention 和 head 整除。**三种实现省的是不同的东西**，选型必须
对着显存账本和拓扑账本，而不是对着并行度数字。

## 9.3 因果 mask

CP 下每个 rank 的 Q 位置是全局的：

```text
q_start = cp_rank * (s / cp)
q_positions = [q_start, ..., q_start + s/cp - 1]
k_positions = [0, ..., s - 1]
mask[i, j] = k_positions[j] <= q_positions[i]
```

如果错误地使用“本地 Q 对本地 K”的三角 mask，rank 1 会看不到 rank 0 的 K，
attention 完全错。mini 的 `CoreAttention` 显式构造全局位置：

```python
q_positions = torch.arange(q_start, q_start + sq)
kv_positions = torch.arange(kv_len)
causal = kv_positions.unsqueeze(0) <= q_positions.unsqueeze(1)
```

RoPE 也必须使用**全局位置**：rank 1 的第一个 token 位置是 `s/cp`，不是 0。

## 9.4 损失归一化

每个 CP rank 只算本地 token 的 loss。如果直接对本地 loss 取平均，再对 CP 求平均，
得到的是“各 rank 平均值的平均”，在序列长度不等或 ignore_index 不同时会错。

正确做法是：

```text
global_loss = (Σ_r local_sum_r) / (Σ_r local_count_r)
```

即先 all-reduce 分子和分母，再相除。反向时，每个本地 token 的梯度要除以
**全局**有效 token 数，而不是本地 token 数。mini 的
`vocab_parallel_cross_entropy` 把 `global_count` 存进 autograd context，确保
backward 使用正确缩放。

## 9.5 梯度：最容易被忽略的 all-reduce

CP 的梯度路径有两个层次：

### 9.5.1 K/V all-gather 的反向

前向把各 rank 的 K/V 拼成完整序列。每个 rank 用同一份完整 K/V 计算**不同 Q
块**的 attention，而全局 loss 是各 Q 块 loss 的和。因此对本地 K/V 的梯度是
**所有 rank 的 grad_output 之和**：

```python
grad_output = all_reduce(grad_output, group=cp_group)
local_grad = chunk(grad_output, cp)[cp_rank]
```

如果只 `chunk` 不 all-reduce，每个 rank 只拿到自己的 Q 块对本地 K/V 的贡献，
丢掉了其他 rank 的贡献。mini 的 `_GatherContextParallel.backward` 就踩过这个
坑：修复前 K/V 梯度范数只有参考的约 0.85 倍，norm 梯度只有 0.74 倍；修复后
误差降到 1e-8。完整病例如 27.5 的 bug 4。

### 9.5.2 复制参数的梯度

CP rank 持有相同的 TP 分片参数（LayerNorm、bias 等），但处理不同 token。所有
本地参数的梯度都需要在 CP 组内求和：

```python
all_reduce_gradients(model, group=cp_group, average=False)
```

这一步在 `Trainer.train_step` 中完成。如果不做，loss 前向正确，但参数更新只
看到本地序列块。

## 9.6 CP 与 TP/PP/DP 的组合

rank 布局：

```text
rank = ((pp_idx * dp_size + dp_idx) * cp_size + cp_idx) * tp_size + tp_idx
```

其中 `pp_idx/dp_idx/cp_idx/tp_idx` 是各维度的局部编号，`*_size` 是该维度的
并行度；这正是 `mini_megatron/parallel/state.py` 使用的布局。

- CP group：固定 tp/dp/pp，变化 cp；
- TP group：固定 pp/dp/cp，变化 tp；
- DP group：固定 pp/tp/cp，变化 dp；
- PP group：固定 tp/cp/dp，变化 pp。

mini 支持 `CP+TP`、`CP+PP`、`CP+DP`，但**不支持 SP+CP**。生产 Megatron 支持
更复杂的组合，代价是需要二维序列网格、两层 all-gather/reduce-scatter，以及
更复杂的 mask/归一化。

## 9.7 长上下文的其他手段

CP 不是唯一手段，通常组合使用：

| 手段 | 作用 | 代价 |
|---|---|---|
| FlashAttention | 不物化 `s²` 矩阵 | kernel 限制、实现复杂 |
| CP | 切序列激活/KV | 通信、mask 复杂 |
| 激活重算 | 降激活 | 额外前向 |
| 稀疏/混合注意力 | 降 `s²` 计算 | 质量/实现复杂 |
| KV 量化/分页 | 降推理 KV | 精度/管理 |
| 序列 packing | 提高短样本利用率 | document mask |
| 分块 prefill | 降推理峰值 | 调度复杂 |

1M 上下文不是“把 CP 开到 128”就结束：需要数据课程、位置编码外推、注意力
稀疏化、通信拓扑和收敛验证共同配合。Megatron 2026 roadmap 的 1M 目标也是
“训练课程 + 稀疏注意力 + CP 调度”一起推进的。

## 9.8 mini 代码路径与验证

- `parallel/attention.py`
  - `_GatherContextParallel`：all-gather K/V，backward all-reduce + chunk；
  - `CoreAttention`：全局 q/k 位置、causal mask、fp32 softmax。
- `parallel/transformer.py`
  - 第一个 stage 按 CP 切 `input_ids/labels`；
  - `_position_ids` 加 `cp_rank * local_seq` 偏移。
- `parallel/cross_entropy.py`
  - CP 组内 all-reduce loss 的分子/分母。
- `optim/adamw.py`
  - CP 组梯度求和。
- `tests/parallel_equiv.py --case cp`
  - CP=2 与 CP=1 的 loss、logits、q/k/v/norm 梯度对比。
- `parallel/ulysses.py`
  - `ulysses_attention`：单进程模拟两次 all-to-all + 全序列 attention；
  - `ulysses_layout_roundtrip`：验证两次布局变换是精确逆置换；
  - `tests/unit/test_ulysses.py`：cp=1/2/4 与 dense 的前向/反向一致，以及
    通信量公式。

验证时建议分三步：

1. 先只比较 logits（前向）；
2. 再比较 q_proj 梯度（本地 token）；
3. 最后比较 k/v/norm 梯度（跨 rank 聚合）。

如果 1 对、2 对、3 错，几乎可以确定是 all-gather 反向或复制参数梯度的问题。

## 9.9 本章小结

- CP 切序列，核心是全局 mask、RoPE 位置和 loss 归一化。
- all-gather KV 是正确但费内存的教学实现；生产用 ring/分块或 Ulysses。
- ring、Ulysses、all-gather-KV 省的不是同一种资源：ring 省 KV 显存，
  Ulysses 省通信，all-gather 只省 Q/MLP 激活。
- Ulysses 用两次 all-to-all 换布局，总通信约为 ring 的 `2/P`，但要求
  `S`/`H` 可被 `P` 整除，且每层有固定 all-to-all 延迟。
- K/V 的 all-gather 反向必须跨 CP 求和，否则梯度少贡献。
- 复制参数的梯度要在 CP 组内求和。
- 1M 上下文需要 CP、稀疏注意力、数据课程和系统优化共同作用。

下一章进入 MoE：当模型容量不再等于每 token 计算量。

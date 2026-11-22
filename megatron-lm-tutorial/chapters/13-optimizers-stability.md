# 第 13 章 优化器与训练稳定性

## 13.1 AdamW 的账本

AdamW 的更新：

```text
m_t = β1 m_{t-1} + (1-β1) g_t
v_t = β2 v_{t-1} + (1-β2) g_t²
m_hat = m_t / (1-β1^t)
v_hat = v_t / (1-β2^t)
θ_t = θ_{t-1} - lr (m_hat / (√v_hat + ε) + wd · θ_{t-1})
```

大模型常用的超参：

- `β1=0.9`，`β2=0.95`（LLM 常用，比 0.999 更快响应梯度变化）；
- `ε=1e-8`；
- weight decay 0.1（只对 2D 权重，norm/bias 通常不 decay）；
- warmup 1%–5% steps；
- cosine/linear decay 到 `0.1×lr`。

Adam 的状态是 2 份 fp32/参数（m/v），这是显存的大头。ZeRO-1/分布式优化器
按 DP 切分状态，是 70B+ 训练的必需项。

## 13.2 梯度裁剪

全局范数裁剪：

```text
if ||g|| > max_norm:
    g = g * max_norm / (||g|| + ε)
```

它防止个别 batch 的梯度爆炸。注意：

- 范数要在 DP/CP 同步之后、TP/PP 全局口径下计算（见第 05 章）；
- 裁剪前记录 norm，norm 的突然飙升通常是数据/数值问题的早期信号；
- 不建议用 per-parameter 裁剪替代全局裁剪。

## 13.3 学习率与 warmup

为什么需要 warmup：

- 训练初期 Adam 的二阶矩估计不稳定；
- 大 batch + 大模型对早期大梯度敏感；
- FP8/bf16 的动态范围需要逐步适应。

mini 的 `get_lr` 支持：

- `cosine`：`min_lr + 0.5(1+cos(πp))(lr-min_lr)`；
- `linear`；
- `constant`；
- `inverse_sqrt`。

`warmup_steps` 内线性升到 `lr`，之后按策略衰减。生产还会用：

- WSD（warmup-stable-decay）；
- cosine with restarts；
- layer-wise LR（Muon/μP 场景）；
- 按 token 数而不是 step 数调度（batch 变化时更稳）。

### 13.3.1 critical batch size

把 batch 从 `B` 增到 `2B`，如果梯度噪声足够大，达到同样 loss 所需的**步数**
可以减半；但当 batch 超过某个临界值，继续增大只会增加每步时间，收敛步数
不再成比例下降。这个拐点叫 critical batch size：

```text
B_crit ≈ tr(Σ) / |G|²
```

其中 `G` 是全 batch 梯度，`Σ` 是单样本梯度的协方差。直观解释：

- 小 batch：梯度噪声主导，增大 batch 是“用算力换更准的方向”；
- 大 batch：已经接近全 batch 梯度，再增大只是重复计算。

工程含义：

- 训练早期/小模型，`B_crit` 往往较小，盲目把 global batch 开到几百万
  token 会浪费算力；
- 训练后期/大模型，`B_crit` 上升，可以逐步增大 batch；
- 增大 batch 时，学习率通常按 `sqrt(B)` 或线性缩放做短期补偿，但超过
  `B_crit` 后需要重新调参；
- `B_crit` 不是常数：它随训练进展、模型规模、序列长度和数据分布变化。

mini 没有实现 `B_crit` 估计器；这是一个“先用小规模扫描找到拐点，再外推”
的实验项。可执行的做法是固定 token 预算，跑 `B ∈ {1,2,4,8,...}×` 的短
实验，画“达到目标 loss 的 token 数 vs batch”，拐点之后收益递减。

## 13.4 Muon：2026 的新变量

Muon 对 2D 参数（矩阵）做近似正交化的动量更新：

```text
M_t = μM_{t-1} + G_t
O = NewtonSchulz(M_t)     # 近似正交矩阵
θ_t = θ_{t-1} - lr · O
```

Newton–Schulz 迭代用若干次矩阵乘法逼近 `UV^T`（SVD 的正交因子），避免显式
SVD。优点是：

- 对矩阵参数的更新尺度更均匀；
- 在大模型上可能比 AdamW 更快/更稳；
- 优化器状态更少（只有动量，没有二阶矩）。

挑战：

- 不是所有参数都是 2D（embedding、norm、bias 需要混合优化器）；
- 分布式实现要按 TP/PP/DP 切分矩阵并保持正交化的全局语义；
- 超参/lr 与 AdamW 不可直接迁移；
- 大规模收敛证据仍在积累。

Megatron Core 0.20 系列包含 distributed Muon 相关改进（layer-sharded Muon
的 TP/专家/参数分片、可选 Triton SYRK 路径）；具体进入哪个补丁号以 release
notes 与源码为准，本书按 L2 信号处理（见附录 J）。mini 的 `Muon` 是教学实现：

```python
for _ in range(ns_steps):
    a = x @ x.T
    x = 1.5 * x - 0.5 * (a @ x)      # 三次 Newton–Schulz 变体
```

mini 用的是较简单的**三次**迭代；生产 Muon 常用 13.4.1 的 quintic 系数，
两者都在逼近正交因子，但系数不能混用（把 quintic 系数套进这个三次式会
发散）。它能在小矩阵上降低二次损失，但不处理分布式/混合参数。

### 13.4.1 Newton–Schulz 在逼近什么

对动量矩阵 `M = U Σ V^T`，我们希望得到正交因子 `UV^T`。直接 SVD 太贵，
Newton–Schulz 用多项式迭代逼近：

```text
X_0 = M / ||M||
X_{k+1} = a X_k + b X_k X_k^T X_k + c (X_k X_k^T)^2 X_k
```

常见系数（quintic）为 `(a,b,c) = (3.4445, -4.7750, 2.0315)`，作用在
`X X^T` 的谱上，把奇异值推向 1。迭代次数 3–5 次通常够用；次数越多越接近
精确正交化，但矩阵乘成本线性增长。

### 13.4.2 混合优化器

不是所有参数都是 2D 矩阵：

| 参数 | 优化器 |
|---|---|
| 2D 权重（attention/MLP） | Muon |
| embedding | AdamW（稀疏/行更新） |
| norm/bias | AdamW |
| router | AdamW/fp32 |
| MoE 专家 | Muon 或 AdamW（按实验） |

混合优化器要维护两套状态/lr/weight decay，并保证 checkpoint 能恢复。生产
Megatron 的 distributed Muon 还要处理 TP 分片后的矩阵形状：按 TP 切开的
`W_r` 是否仍能代表全局矩阵的正交化语义，是分布式 Muon 的核心难点。

### 13.4.3 什么时候值得试 Muon

- 模型以 2D 矩阵为主、训练规模足够大；
- 有可靠的 baseline（AdamW）和收敛对比；
- 能接受超参重新搜索；
- 有分布式实现/工程支持。

不要因为“论文说更快”就直接替换生产优化器；先在小规模做 loss/梯度/稳定性
对比，再逐步放大。

## 13.5 Loss spike 与数值稳定性

LLM 训练常见 loss spike：

- 数据中出现异常样本/重复；
- 梯度范数爆炸；
- attention logits 过大；
- FP8/bf16 溢出；
- 学习率过高；
- 路由塌缩（MoE）；
- 并行度改变导致 reduction 顺序变化。

处理：

1. 记录 step、grad norm、lr、loss、amax；
2. 从最近的 checkpoint 回滚，跳过可疑数据；
3. 降低 lr 或增加 warmup；
4. 开梯度裁剪/调整 max_norm；
5. 对 attention/CE 使用 fp32；
6. MoE 加 aux/z-loss、调整 capacity；
7. 复现时固定 seed/并行度。

## 13.6 初始化、μP 与超参迁移

这一节回答一个常被跳过的问题：**为什么小模型调好的学习率，到大模型就崩？**

### 13.6.1 初始化的方差账本

一个线性层 `y = Wx`，输入 `x_i ~ N(0,1)`，`W_ij ~ N(0, σ²)`，则

```text
Var(y_j) = fan_in · σ²
```

要让每层输出方差保持 1，需要 `σ² = 1/fan_in`（fan-in 初始化）。GPT-2 常用
固定 `σ=0.02`，在 `fan_in=768` 时接近 `1/sqrt(768)≈0.036`，属于同一量级；
但层宽变化 10 倍时，固定 std 会让方差偏 10 倍。

残差网络还要考虑**深度**：每层残差分支的方差会累加，

```text
Var(h_L) ≈ Var(h_0) + Σ_l Var(branch_l)
```

如果每个 branch 的方差是 O(1)，100 层的残差流方差会变成 O(L)。GPT-2 的
做法是把输出投影按 `1/sqrt(2L)` 缩放；深度-μP 则把每个残差分支按
`1/sqrt(L)` 缩放并给每层独立的 LR。

### 13.6.2 μP 的三条规则

μP（Tensor Programs V / μTransfer）不是“一个初始化技巧”，而是一套**宽度
参数化**，让无限宽极限下的训练动力学与宽度无关：

| 对象 | 标准参数化 | μP |
|---|---|---|
| 隐藏层权重初始化 | PyTorch `nn.Linear` 默认用 `kaiming_uniform_(a=√5)`，`σ² ≈ 1/(3·fan_in)` | `σ² = gain²/fan_in`；`gain²≈2` 是 ReLU/GeLU 的 He 常数，关键是**宽度方向按 `1/fan_in` 缩放**，常数可并入 init gain/LR |
| 隐藏层学习率 | 与宽度无关 | **取决于优化器**：SGD 下 `lr ∝ 1/width`；AdamW 下常见约定是宽度无关（Adam 的归一化会抵消梯度尺度的 `1/width`） |
| 嵌入/输出头学习率 | 与隐藏层相同 | 嵌入 `1`，输出头 `1/width`（常见约定，需与实现核对） |
| 偏置 | 0 | 0 |

核心直觉：宽度变大时，每个隐藏神经元对输出的贡献变小，因此 **SGD** 的隐藏层
LR 要按 `1/width` 缩小；而嵌入和输出头的梯度是**求和**而不是平均，缩放规则
不同。AdamW 因为对梯度做了二阶归一化，隐藏层 LR 的宽度缩放规则与 SGD 不同，
不能把 `1/width` 直接套过去。μP 的完整规则是“初始化 + 每层 LR/乘子 + 输出头
缩放”的组合，不是单一公式。

需要强调的边界：

- μP 主要保证**宽度**方向的迁移；深度迁移需要 depth-μP 或额外的残差缩放，
  不能直接套用。
- μP 的常数因子/输出头约定在不同实现中略有差异；迁移前要在自己的代码里
  做一次小规模扫描。
- 它不解决数据、batch size、优化器 β 的迁移；这些仍要单独调。
- MoE/MLA/SSM 等结构的 μP 变体仍在研究中，不能假设与稠密 Transformer 相同。

### 13.6.3 μP 与张量并行的交互

这是实现中最容易错的地方：**fan-in 是全局输入维度，不是本地分片维度。**

- Column Parallel：每个 rank 计算一部分**输出特征列**（输出行是 batch/token
  维，不是被切分的维），输入是完整的 `x`，所以 fan-in 是全局 `input_size`；
  本地分片也用全局 fan-in（std 为 `gain/sqrt(input_size)`，不是
  `/sqrt(input_size/TP)`）。
- Row Parallel：每个 rank 计算 `x_t W_t`，最后 all-reduce 求和。每个分片必须
  按**全局** fan-in 初始化（含激活 gain）；TP 个零均值分片求和后方差是
  `gain²` 量级，而不是被额外放大 TP 倍。若误用本地 `fan_in/TP`，每片方差会
  大 TP 倍，求和后再放大 TP 倍，初始激活/梯度直接失控。
- 复制参数（norm、router）不参与 fan-in 缩放，按复制流初始化。

mini 为了可读性使用固定 `init_method_std=0.02`，没有实现 fan-in/μP；这在大
模型上会限制超参迁移。生产 Megatron 提供 `--init-method-std`，但 μP 需要
自定义 param groups（不同 LR multiplier）和输出头缩放，通常由训练配方/框架
层实现，而不是一个开关。

### 13.6.4 一个可执行的迁移协议

```text
1. 选一个代理模型：宽度为目标模型的 1/4～1/2，深度/数据/batch 比例尽量一致；
2. 在代理上扫描 LR、init std、warmup；
3. 按 μP 规则把 LR 缩放到目标宽度：SGD 隐藏层 `1/width`；AdamW 以
   “宽度无关”为默认起点（不同实现的约定不同，必须核对）；输出头 `1/width`；
4. 在目标模型上只跑 100–500 步，检查：
   - 初始 loss 是否与代理一致（初始化方差对）；
   - grad norm 是否在同一量级；
   - 前 100 步 loss 斜率是否合理；
5. 如果初始 loss 偏高：init 太大或输出头缩放错；
   如果 grad norm 爆炸：LR 太大或残差缩放错；
   如果 loss 几乎不降：LR 太小或 warmup 太长。
```

一个常见误区是“把小模型的绝对 LR 直接乘以 batch size 比例”。batch size 和
宽度是两个不同的缩放轴：前者影响梯度噪声，后者影响每神经元贡献。μP 只解决
后者。

### 13.6.5 失败模式

| 现象 | 可能原因 | 排查 |
|---|---|---|
| 初始 loss 远高于预期 | 输出头/残差方差错 | 逐层记录 activation std |
| 前几十步 loss 爆炸 | LR 未按宽度缩小 | 对比小/大模型 grad norm |
| loss 下降极慢 | LR 过小 / warmup 过长 | 扫 LR，缩短 warmup |
| 不同宽度曲线无法对齐 | 初始化 fan-in 用了本地分片 | 检查 TP 下的 fan-in |
| 换 TP 后结果变化 | 初始化/RNG 未按 rank 区分 | 见 17.5、附录 G |

## 13.7 确定性

完全确定性训练需要：

- 固定 reduction 顺序（集合通信、CUDA kernel）；
- 关闭非确定性算法（`torch.use_deterministic_algorithms`）；
- 固定数据顺序；
- 固定 dropout RNG；
- 相同并行度/拓扑。

代价是性能下降，且跨不同 GPU 数/拓扑几乎不可能逐位一致。工程上通常追求
“统计等价”（loss 曲线在容差内），而不是逐位一致。Megatron 2026 有
bitwise-deterministic pretraining 的指南，说明在特定配置下可以做到，但需要
严格控制。

## 13.8 mini 的实现

`mini_megatron/optim/adamw.py`：

- `build_optimizer`：按 2D/1D 分 weight decay 组；
- `get_lr`：warmup + cosine/linear/constant/inverse_sqrt；
- `all_reduce_gradients`：DP 平均 / CP 求和；
- `global_grad_norm`：全局范数；dense 参数除以 DP×CP（副本数），EP 专家
  只除以 CP（专家已按 EP 切分，不是 DP 副本）。两类的范数平方分别归约后
  相加，不能把专家也除 DP；
- `clip_gradients`；
- `ZeRO1Optimizer`：按参数张量分片状态；
- `Muon`：教学版。

`Trainer.train_step` 的顺序：

```text
zero_grad
forward_backward（梯度累积）
DP all-reduce
CP all-reduce
global clip
set_lr
optimizer.step
```

这个顺序不能随意换：如果在梯度同步前裁剪，各 rank 的裁剪系数不同，会破坏
梯度一致性；如果在 CP 求和前裁剪，范数口径不对。

## 13.9 本章小结

- AdamW 的状态是显存大头；β2=0.95、weight decay、warmup 是 LLM 常用配方。
- 梯度裁剪要用全局范数，且在 DP/CP 同步之后。
- 初始化是超参迁移的地基：fan-in 用全局维度，TP 分片不能按本地维度缩放。
- μP 用宽度参数化让 LR/初始化可从小模型迁移，但不自动解决深度/数据/batch。
- Muon 是 2026 的重要方向，但混合参数/分布式实现仍复杂。
- loss spike 先用 checkpoint/数据/精度三板斧定位。
- 确定性训练有成本，统计等价通常是更现实的目标。

下一章把计算、通信、显存放在一起，做并行策略与性能建模。

# 附录 A 数学推导

## A.1 张量并行的前向/反向

### Column Parallel

```text
Y_r = X W_r + b_r
Y = concat_r Y_r
```

反向：

```text
dW_r = dY_r^T X
db_r = sum_tokens dY_r
dX_r = dY_r W_r^T
dX = Σ_r dX_r
```

最后一步的 all-reduce 是 Column Parallel 正确性的关键。

### Row Parallel

```text
Y_r = X_r W_r
Y = Σ_r Y_r + b
```

反向：

```text
dX_r = dY W_r^T
dW_r = dY^T X_r
db = sum_tokens dY
```

`dX_r` 是本地输入分片的梯度，不需要再 all-reduce。

## A.2 词表并行 CE

全局 logits 被切成 `V/t` 份。对 token `i`：

```text
L_i = logsumexp(z_i) - z_i[y_i]
```

`logsumexp`：

```text
m = max_r max_v z_{i,v}
s = Σ_r Σ_v exp(z_{i,v} - m)
L_i = m + log s - z_i[y_i]
```

`max`/`sum` 在 TP 组内 all-reduce；目标 logit 只在一个 rank 上非零，其他 rank
贡献 0。全局平均：

```text
L = Σ_i L_i / Σ_i valid_i
```

反向：

```text
∂L/∂z_{i,v} = (softmax(z_i)_v - 1[v=y_i]) / N_valid
```

`N_valid` 是**参与这次 loss 的全局有效 token 数**。mini 的 CE 在 CP 组内做
`sum/count` 归约，DP 维由训练循环对 loss 做平均；只有当各 DP rank 的有效
token 数相同时，这两步才等价于全局 token 平均。变长/packed batch 下必须
先归约全局 token 数，否则“各 rank 平均再平均”会给短序列过高的权重。生产
Megatron 的 packed-sequence 路径会显式处理这一点。

## A.3 流水线气泡

GPipe/1F1B 的气泡比例（无 interleaving）：

```text
bubble ≈ (p - 1) / (m + p - 1)
```

推导直觉：pipeline 需要 `p-1` 个 warmup 阶段和 `p-1` 个 cooldown 阶段，总
时间约 `m + p - 1` 个 micro-step，其中气泡 `p-1`。增大 `m` 或减少 `p` 降低
气泡；interleaved/VPP 把层分成多个 chunk，进一步降低。

## A.4 Roofline

```text
I = FLOPs / Bytes
若 I < peak_flops / peak_bw：带宽受限
否则：计算受限
```

大 GEMM 通常计算受限；LayerNorm/softmax/逐元素算子带宽受限。并行会改变
`I`（切分后矩阵变小、通信增加），所以“单卡 roofline”不能直接外推到多卡。

## A.5 显存

状态（混合精度 AdamW；ZeRO-1 只切优化器状态，不切权重/梯度）：

```text
M_state(ZeRO-1) ≈ 4N/(tp·pp) + 12N/(tp·pp·d_z)
# 4N = bf16 权重 2N + bf16 梯度 2N（DP 维复制）
# 12N = fp32 master 4N + Adam m/v 8N（按 d_z 切分）
```

无 ZeRO 时 `d_z=1`，回到 `16N/(tp·pp)`；ZeRO-2 再切梯度：
`2N/(tp·pp) + 14N/(tp·pp·d_z)`；ZeRO-3/FSDP 连参数也切：
约 `16N/(tp·pp·d_z)`。70B、`tp=8, pp=1, d_z=8` 时是
`4×70/8 + 12×70/(8×8) = 48.1 GB/卡`，不是 `16×70/64 = 17.5 GB/卡`。

激活（粗略）：

```text
M_act ≈ k · b · s · h · L / (tp · pp · cp · sp)
```

`k` 依赖实现（attention/MLP/重算），必须实测。

## A.6 KV cache

```text
KV = 2 · L · n_kv · d · s · b · bytes
```

GQA 的 `n_kv < n_h`；MLA 进一步压缩到 latent。

## A.7 训练 FLOPs

```text
FLOPs/token ≈ 6N + 4 · L · n_h · d · s
```

第一项是参数矩阵乘，第二项是 attention 的 QK/AV。长序列时第二项不可忽略。

口径说明：这里的 `4·L·n_h·d·s` 是**非因果/上界**计数（QK 与 AV 各
`2·d·s`）。因果 attention 的有效计算约减半，但实际 kernel 是否利用这一点
取决于实现：有的按上界执行，有的用 triangular tile 跳过；FlashAttention
的 FLOPs 计数通常按因果减半。做 MFU 对比时，分子分母必须用同一口径，否则
会凭空多出约 2 倍差异。

## A.8 VPP 气泡

非 interleaved 1F1B：

```text
bubble ≈ (p - 1) / (m + p - 1)
```

interleaved/VPP（每个 rank `v` 个 chunk）：

```text
bubble ≈ (p - 1) / (m·v + p - 1)
```

直觉：气泡仍由 `p-1` 个跨 stage 的填充步决定，但每个 micro-batch 现在有
`m·v` 个可调度单元。代价是更多 chunk 的激活/调度状态。

## A.9 KV cache 与 paged attention

```text
KV bytes = 2 · L · n_kv · d · s · b · bytes_per_element
```

PagedAttention 把序列切成 `B` 个 token 的 block：

```text
blocks_per_seq = ceil(s / B)
physical_blocks = active_sequences × blocks_per_seq
```

逻辑 block 通过页表映射到物理 block；prefix sharing 让多个序列指向同一物理
block，引用计数为 0 时回收。碎片从“序列级”降到“block 级”。

## A.10 偏好优化

DPO：

```text
L = -log σ( β [ log π(y_w|x)/π_ref(y_w|x) - log π(y_l|x)/π_ref(y_l|x) ] )
```

令 `r_w = log π(y_w|x) - log π_ref(y_w|x)`，`r_l` 同理：

```text
∂L/∂r_w = -β σ(-β(r_w-r_l))
∂L/∂r_l = +β σ(-β(r_w-r_l))
```

GRPO 用组内相对奖励：

```text
A_i = (r_i - mean(r_1..r_G)) / (std(r_1..r_G) + ε)
```

组内奖励全相同（方差为 0）时 advantage 为 0，没有梯度；这是 GRPO 在
“全对/全错”样本上学习信号弱的根源。

## A.11 ring all-reduce 通信量

对 `N` 个 rank、消息 `S` 字节，ring all-reduce = reduce-scatter + all-gather：

```text
每 rank 发送量 = 2S(N-1)/N
```

`N` 很大时趋近 `2S`，与 `N` 基本无关。这就是“DP 开到很大，梯度通信不会按
比例变便宜”的数学原因。

## A.12 梯度范数

```text
||g|| = sqrt(Σ_params ||g_p||²)
```

对 DP/CP 副本要去重（副本持有相同梯度）；对 TP 分片/PP 阶段要各计一次。
TP 内的复制参数（norm/bias/router）在一个 rank 上已有完整梯度，也应只计一次。
mini 的 `global_grad_norm` 对 **dense 参数**除以 `dp_size × cp_size`，对
`_is_replicated` 参数再除以 `tp_size`；**EP 专家**在 `ep=dp` 下不是 DP
副本，只除以 `cp_size`（否则少计 `sqrt(dp)`）。生产 `dp>ep` 时专家还有
EDP 副本，需先在 EDP 组归约再缩放（10.3.6）。

## A.13 为什么是 6N

对一层线性 `y = Wx`，`W` 有 `N` 个参数，前向是 `2N` FLOPs（乘加各算 1）。
反向要算：

```text
dX = dY · W     -> 2N
dW = dY^T · X   -> 2N
```

合计每参数每 token 约 `6` FLOPs；乘上 token 数 `D` 得 `6ND`。这个计数
不含 attention 的 `QK^T/AV`、norm、softmax、激活；长序列时要把它们加上。

## A.14 ZeRO/分布式优化器的状态账

设总参数 `N`、TP `t`、PP `p`、DP `d`，优化器状态分片 `d_z`：

```text
bf16 权重 + 梯度 = 4N/(t·p)          # DP 维复制
fp32 master + m + v = 12N/(t·p·d_z)  # DP 维分片
```

ZeRO-2 再把梯度分片：

```text
2N/(t·p) + 14N/(t·p·d_z)
```

ZeRO-3/FSDP 连参数也分片：

```text
16N/(t·p·d_z)      # 稳态；all-gather 时临时回到完整参数
```

注意 FSDP 的“稳态”不是峰值：前向/反向某层 all-gather 时，该层参数会临时
完整，峰值取决于同时驻留的层数/预取深度。

## A.15 MoE 的 active 参数与 aux 梯度

总参数与激活参数：

```text
N_total  = N_dense + E · N_expert
N_active = N_dense + k · N_expert
```

`N_dense` 包含 attention、router、norm、embedding/output。一个常见错误是
只算 `k/E` 的专家，漏掉 `N_dense` 或 norm。

Switch 风格 aux：

```text
L_aux = α · E · Σ_i f_i · P_i
f_i = (1/T) Σ_t 1[argmax 路由 = i]
P_i = (1/T) Σ_t p_i(t)
```

对 router logits 的梯度同时经过 `f_i`（不可微的计数，通常 stop-gradient）
与 `P_i`（可微）。实现时若把 `f_i` 也当可微，会多出一项错误梯度；若把
`P_i` detach，则 aux 不起作用。mini 的 `MoELayer` 用 stop-gradient 处理
计数，并在测试中对比含/不含 aux 的梯度。

## A.16 MLA 的吸收（absorption）

MLA 的 decode 可以先把投影“吸收”进权重，避免每次重建完整 K/V。记：

```text
c_KV = x · W_DKV^T        # latent，d_c 维；W_DKV: [d_c, h]
k_R  = x · W_KR^T         # 共享 RoPE key，d_R 维；W_KR: [d_R, h]
```

这里沿用 HF/PyTorch 的 `[out, in]` 权重布局；若把权重存成 `[in, out]`，
公式要去掉转置。两种布局不能混用。

内容部分（nope）的 key 由 `K_nope = c_KV · W_UK^T` 得到，value 由
`V = c_KV · W_UV^T` 得到（本书沿用 30.5/K.4 的约定：
`W_UK: [n_h·d_nope, d_c]`、`W_UV: [n_h·d_v, d_c]`）。把 query 的 nope 部分
也投影到 latent：

```text
q_nope'_i = q_nope_i · W_UK_i      # W_UK_i: [d_nope, d_c]；与 c_KV 同维
score_nope_i = q_nope'_i · c_KV^T
```

注意这里是 `q_nope_i · W_UK_i`，不是 `· W_UK_i^T`：`K_nope_i = c_KV W_UK_i^T`
已经转置过一次，score 展开后 `W_UK_i` 正好作用在 query 上。若把 `W_UK`
定义成 `[d_c, n_h·d_nope]` 的转置布局，公式才会写成 `W_UK^T`；两种写法
不能混用。这样 attention 的 score 可以直接在 latent 空间算，不必显式构造
`K_nope`。输出侧把 `W_UV` 吸收进 `W_O`：

```text
W_O'_i = W_O_i · W_UV_i           # W_O_i: [h, d_v], W_UV_i: [d_v, d_c]
```

同样要按 head 取 `W_UV` 的切片；把整个 `[n_h·d_v, d_c]` 直接乘进
`[h, d_v]` 会得到错误的 shape。

RoPE 部分仍用共享的 `k_R` 单独算 score，再与 nope 的 score 相加。吸收的
收益是**减少 decode 时的矩阵乘/显存带宽**，代价是权重预处理和数值路径
变化；实现时必须用同一输入对比“吸收前/后”的 logits，误差应在容差内。

## A.17 EP all-to-all 的字节数

设 token 数 `T`、top-k `k`、hidden `h`、EP `e`、bf16：

```text
dispatch ≈ T · k · (2h + 4) · (e-1)/e     # hidden + token/expert id 元数据
combine  ≈ T · k · 2h · (e-1)/e
```

`(e-1)/e` 是“只有跨 rank 的部分需要发送”的近似；实际取决于路由分布和
split sizes。padding 方案按“每个 peer 的最大 token 数”发，最热专家决定
buffer，因此尾延迟比平均带宽更重要。

## A.18 Ring attention 的 online softmax

对每个 query 块，维护：

```text
m_i = 当前最大 score
l_i = 当前 exp 和
acc = 当前未归一化的加权 V
```

处理新 KV 块时：

```text
m_new = max(m_i, max(s))
alpha = exp(m_i - m_new)
p = exp(s - m_new)
l_i = l_i · alpha + Σ p
acc = acc · alpha + p · V
m_i = m_new
```

最终 `out = acc / l_i`。因果模式下，整块在未来的 KV 块直接跳过；块内用
全局位置做三角 mask。数值上，`m_i=-inf` 的第一块 `alpha=0`，不会产生 NaN；
但如果写成 `exp(m_i - m_new)` 且 `m_new` 也是 `-inf`（全 mask 块），就会
得到 `exp(nan)`，所以必须先跳过全未来块。

## A.19 Zero-bubble 的气泡

在理想等分、通信可忽略的假设下：

```text
1F1B:   (p-1)/(m+p-1)
ZB1P:   ≈ (p-1)/m
ZB2P:   ≈ (p-1)/(2m)
```

推导直觉：1F1B 的 warmup/drain 占 `p-1` 个槽；zero-bubble 用可延后的
权重梯度填槽，相当于把可用于填槽的单元数从 `m` 增加到 `m`（ZB1P）或
`2m`（ZB2P）。真实系统要加 p2p 延迟、stage 不均、重算和 optimizer 时间。

## A.20 重叠效率

```text
T_serial = T_c + T_m + T_n
T_overlap = max(T_c, T_m) + T_n
speedup = T_serial / T_overlap
η = (T_serial - T_overlap) / T_m        # 被隐藏的通信比例
```

若 `T_c=100, T_m=40, T_n=10`：`T_serial=150`，`T_overlap=110`，
`speedup=1.36`，`η=1.0`（理想）。若只重叠一半，`T_overlap=130`，
`speedup=1.15`，`η=0.5`。

## A.21 Ulysses 的通信量

设 `P=cp_size`、batch `B`、head `H`、序列 `S`、head_dim `d`。下面按**元素**
计，乘 `bpe` 得到字节。Ulysses 每层做两次 all-to-all：Q/K/V 一次，输出
一次。

变换前每 rank 的每个张量是 `B·H·(S/P)·d` 个元素。all-to-all 中每个元素
只发给一个目的 rank，留在本地的比例是 `1/P`，所以每个张量每 rank 发送

```text
B·H·(S/P)·d · (P-1)/P  =  B·S·H·d · (P-1)/P²
```

四个张量（Q/K/V/O）合计：

```text
每 rank：4·B·S·H·d · (P-1)/P²
全组：  4·B·S·H·d · (P-1)/P
```

ring attention 每 rank 把 K/V 块沿环传 `P-1` 步：

```text
每 rank：2·B·S·H·d · (P-1)/P
全组：  2·B·S·H·d · (P-1)
```

因此 Ulysses 的全组通信量是 ring 的 `2/P`。但这不是“Ulysses 全面更优”：
它每层有两个 all-to-all 的固定延迟，要求 `S` 和 `H` 都被 `P` 整除，并且
每个 rank 要持有全序列的 Q/K/V（attention 的 score 若物化是 `O(S²H/P)`）。
ring 的优势是 KV 显存降到 `1/P`、可用 p2p 与计算重叠。GQA 下若 KV head
只有 8 个，Ulysses 的 `P` 实际被卡在 8 以内（除非复制 KV），这是选型时
最先要检查的约束。

---

返回目录：[SUMMARY](../SUMMARY.md)  
下一附录：[附录 B 配置速查](B-config-reference.md)

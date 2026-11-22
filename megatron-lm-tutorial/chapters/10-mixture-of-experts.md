# 第 10 章 MoE 与专家并行

## 10.1 从稠密到稀疏

稠密 Transformer 每处理一个 token，都会经过所有参数。MoE（Mixture of Experts）
把 FFN 换成 `E` 个专家，每个 token 只激活其中 `k` 个：

```text
y = Σ_{e ∈ TopK(router(x))} g_e(x) · Expert_e(x)
```

更准确地说：**专家/FFN 参数约增大 `E` 倍；总参数的增大倍数小于 `E`**：

```text
N_total ≈ N_shared + E · N_expert
N_active ≈ N_shared + k · N_expert
总参数倍数 ≈ (N_shared + E · N_expert) / (N_shared + N_expert)   # < E
```

例如 `N_shared=100, N_expert=10, E=8`：总参数是稠密等价模型
`(100+10)` 的 `(100+80)/110 ≈ 1.64` 倍，不是 8 倍。分子必须包含共享部分，
否则会算出小于 1 的“倍数”。

每 token 的激活参数与计算量只增加约 `k` 倍（再加共享部分）。这是“模型容量”
和“计算量”解耦的核心；说“参数量增大 E 倍、计算量只增加 k 倍”时，要明确
是总参数还是激活参数，且总参数通常不是严格 E 倍。

DeepSeek-V3 级别的模型把这种稀疏性推到极致：数百个专家、共享专家、细粒度
路由、MLA 注意力，配合 FP8 训练。Megatron Core 的 `transformer/moe/` 提供
了 EP/TP/PP 组合、token dispatcher 与 grouped GEMM；对具体新模型（如
DeepSeek-V4）的支持要以当前源码和 release notes 为准：0.20.0 已声明
HybridModel 支持（34.3），但具体的层调度/并行/精度组合仍要以 recipe 和
数值对齐为准，不要根据二手文章推断未公开的结构细节。

## 10.2 路由与容量

### Top-k 路由

```python
logits = x @ W_router.T          # [tokens, E]
probs = softmax(logits)
topk_probs, topk_idx = topk(probs, k)
```

每个 token 选 `k` 个专家，权重通常用 softmax 后的概率（或重新归一化）。

### Capacity factor

为了把 token 批量送进专家 GEMM，通常给每个专家设容量：

```text
capacity = ceil(tokens * k / E * capacity_factor)
```

超过容量的 token 被丢弃（token dropping），或通过 dropless 方案重新路由。
`capacity_factor` 越大，丢 token 越少，但 padding/显存越多。

### 负载均衡损失

Switch Transformer 的辅助损失：

```text
L_aux = α · E · Σ_i f_i · P_i
f_i = 分配到专家 i 的 token 比例
P_i = 路由概率的均值
```

它鼓励路由概率和实际分配都均匀。DeepSeek-V3 使用 aux-free 的 bias 调整策略，
减少辅助损失对主任务的影响；不同模型的实现差异很大，不能把一种配方直接套到
另一种模型。

mini 有两条 MoE 路径，不要混为一谈：

- `MoELayer`（`parallel/moe.py`）：专家是**复制的** TP 分片 MLP，每个 rank
  持有全部专家；没有专家并行 all-to-all。用于观察路由/容量/辅助损失的数值
  行为，并在 DP 下验证“某专家无梯度时集合通信仍对齐”。
- `ExpertParallelMoELayer`（`parallel/expert_parallel.py`）：专家按 EP 组
  切分，token 通过变长 all-to-all dispatch/combine（`ep = dp`）。这是真正
  的专家并行，详见 25.6–25.9 与 `tests/parallel_ep.py`。

第 25 章会先拆复制版，再拆 EP 版；两者的路由数学相同，但梯度与通信语义
不同。

## 10.3 专家并行（EP）

把专家分到不同 rank：

```text
rank 0: experts 0..E/ep-1
rank 1: experts E/ep..2E/ep-1
...
```

一个 token 可能被路由到远程专家，因此需要 **all-to-all**：

1. 每个 rank 统计本地 token 要发往哪些专家；
2. all-to-all 发送 token（按专家分组）；
3. 每个 rank 用本地专家计算收到的 token；
4. all-to-all 把结果发回原 rank；
5. 按路由权重合并。

EP 的通信模式与 TP 的 all-reduce 不同：它是**不规则、按 token 动态变化**的。
这带来负载不均、尾延迟、grouped GEMM、capacity padding 等问题。生产实现会
使用：

- 分组 GEMM（grouped GEMM）一次处理多个专家；
- token permutation / unpermutation；
- all-to-all 与计算重叠；
- 专家负载均衡/容量调整；
- DeepSeek 风格的 shared expert 与 fine-grained expert。

### 10.3.1 all-to-all 的四种实现

EP 的 dispatch/combine 可以用不同方式实现，代价差异很大：

| 方式 | 通信 | 优点 | 缺点 |
|---|---|---|---|
| `all_to_all` | 每个 rank 向每个 rank 发不同 token | 语义直接 | 动态 shape、尾延迟 |
| `all_gather` 全量 token | 每 rank 收到所有 token | 实现简单 | 通信/显存 ×ep |
| 分层 all-to-all | 节点内 NVLink + 节点间 IB | 利用拓扑 | 调度复杂 |
| 通信与 GEMM 重叠 | 边收边算 | 隐藏延迟 | 需要 kernel/流编排 |

教学版可以先用 all-gather（类似 CP 的 all-gather KV）保证正确性，再替换为
真正的 all-to-all。判断实现是否正确，仍然是对比“专家复制 MoE”的 loss/梯度。

### 10.3.2 token permutation

all-to-all 前通常要做 token permutation：

```text
输入: x [tokens, h], topk_idx [tokens, k]
1. 按专家排序 token（稳定排序，保证可复现）
2. 统计每个专家的 token 数/offset
3. 把 token 打包成 [total_assigned, h]
4. all-to-all 发送到专家所在 rank
5. 专家 GEMM（grouped GEMM）
6. 逆 permutation + 按路由权重合并
```

关键不变量：

- permutation 与 unpermutation 必须使用同一套索引；
- 稳定排序保证同一输入在不同 rank/运行得到相同顺序；
- capacity 丢弃要在 permutation 前决定，否则索引对不上；
- 梯度要按 permutation 逆映射回原 token。

### 10.3.3 aux-free 负载均衡

DeepSeek-V3 使用 router bias 调整而不是 aux loss：

```text
每个专家维护 bias b_e
路由 logits' = logits + b_e
如果专家 e 过载：b_e -= γ
如果专家 e 空闲：b_e += γ
```

它不直接改主 loss，但需要：

- 全局统计每个专家的负载（跨 EP/DP）；
- bias 的更新频率/步长；
- 与 capacity/dropless 的配合；
- 训练/推理的一致性（batch-invariant routing）。

不同模型的 aux-free 实现差异很大，不能把一种 recipe 直接套到另一种模型。

### 10.3.4 节点限制路由（node-limited routing）

DeepSeek-V3 的 EP 规模很大，一个 token 的 top-k 专家可能落在不同物理节点，
产生跨节点 all-to-all。**node-limited routing** 限制每个 token 最多命中
`max_nodes` 个节点：

```text
输入：scores [tokens, E]，专家按节点连续摆放（每节点 experts_per_node 个）
1. 取全局 top-k；
2. 若命中的节点数 > max_nodes，屏蔽“最强专家分数最低”的那个节点；
3. 在剩余专家上重新 top-k；
4. 返回权重与索引。
```

约束是 `max_nodes × experts_per_node ≥ k`，否则无解。它牺牲少量路由自由度，
换取 all-to-all 的局部性；对训练吞吐的帮助取决于 EP 拓扑和专家放置。

`mini_megatron/parallel/moe_balance.py` 提供 `node_limited_topk` 参考实现，
测试覆盖：

- `max_nodes=1` 时每个 token 的专家都在同一节点；
- `max_nodes=2` 时节点数不超过 2；
- 相同输入可复现；
- 不可行的预算直接报错，而不是静默返回越界结果。

### 10.3.5 可运行的 aux-free 实现与数值

`AuxFreeBalancer` 把“选择”和“门控权重”分开：

```python
biased = scores + bias                 # 只用于 top-k 选择
idx = biased.topk(k).indices
weights = scores.gather(-1, idx)       # 门控仍用原始 affinity
if normalize: weights /= weights.sum(-1, keepdim=True)
bias += update_speed * sign(counts.mean() - counts)
```

`counts` 必须是**全局**专家负载；EP 下用 `global_expert_counts(counts, ep_group)`
做 all-reduce。bias 更新不经过 autograd，因此主 loss 没有 aux 项，也不会有
aux 梯度。

`scripts/moe_balance_demo.py` 在一个合成 router 上跑 200 步：

```text
初始负载: [3240, 2744, 380, 380, 365, 352, 374, 357]
最终负载: [580, 1124, 1049, 1083, 1108, 1074, 1089, 1085]
理想负载: 1024（4096 token × top-2 / 8 专家）
最终 bias: [-0.01, -0.01, +0.01, ...]
```

最大负载从 3240 降到 1124，接近理想值；bias 稳定在 ±0.01。这里 `update_speed`
不能太大：如果一步 bias 变化超过 router 分数差，选择会整块翻转，出现
“负载振荡”，这也是生产调参要监控的量。

### 10.3.6 EP 的梯度与 aux 语义

EP 下有三类参数，梯度规则不同。**下表以 mini 的 `ep == dp` 布局为例**；生产
Megatron 里 EP 是 DP 的子维（`dp = ep × edp`），专家在 EP 内切分、在 EDP
副本组内复制，不能把“专家”和“dense”套进同一条归约规则：

| 参数 | 是否副本 | 梯度归约（mini, `ep=dp`） | 生产 `dp>ep` |
|---|---|---|---|
| 专家权重 | EP 内不同专家；EDP 内复制 | 跳过整组 DP all-reduce，最后除以 `ep` | 先在 **EDP 副本组**内归约，再按全局 token 数缩放 |
| dense/attention/norm | 整个 DP 内复制 | DP 组平均（`/dp`） | DP 组平均（`/dp`） |
| router | 复制 | DP/EP 组平均；aux 梯度还要跨 EP 求和 | 同左，注意 EP/EDP 分组 |
| CP 副本 | 同一切片的复制 | CP 组**求和**（不是平均） | CP 组求和 |

为什么 mini 的专家梯度要“先跳过、再除以 ep”：在 `ep == dp` 时，每个 rank
的 loss 是自己本地 token 的均值；专家 `e` 收到来自所有 EP rank 的 token，
所以它的梯度是各 rank 局部梯度的**和**。dense 参数已经按 DP 平均过，专家
如果再按 DP 平均就会少 `ep` 倍；因此在 DP 同步后补一次 `/ep`，把“和”还原
成全局均值。mini 的 `all_reduce_gradients(skip_expert_parallel=True)` 和
`p.grad.div_(ep)` 就是这条规则（见 22.1）。

**`dp > ep` 时这个推导不成立。** 此时一个 EP 组只覆盖 `ep/dp` 的全局
token；专家参数还有 `dp/ep` 个副本，梯度必须在 EDP（expert data parallel）
副本组内先求和，不能“跳过整组 DP”。把 mini 的 `/ep` 直接搬到 `dp>ep` 会
同时犯两个错：少做 EDP 归约、按错误的 token 数缩放。生产实现通常把
`dp` 拆成 `ep × edp` 两个进程组，分别处理“专家分片”和“副本归约”；
核对时先打印 `dp_size / ep_size`，再检查专家的 gradient hook 挂在哪个
group 上。25.12.1 给出 `ep < dp` 的两级梯度示例。

aux loss 的 `f_i` 必须用**全局** assignment 统计（跨 EP all-reduce），`p_i`
用全局平均路由概率；`aux_loss` 在每个 rank 上算出同一个全局值，反向时
`_AllReduceSum` 还要把各 rank 的上游梯度求和，否则 aux 梯度被少算 `ep` 倍，
router 的均衡学习会变慢。

`f_i` 的 token 计数有一个容易分歧的口径：mini 与 Switch 论文一致，统计
**capacity 截断之前**的分配（`pair_expert` 在截断前就确定），因此被 drop 的
token 仍会进入 `f_i`。另一种做法是只统计真正被专家处理的 token；它会改变
aux 的梯度尺度，尤其在容量因子很小时。两种都可自洽，但必须与公式、
`f_i` 的分母（`N·k`，含 `top_k`）和测试一起固定；换口径后要重跑 EP
等价性测试，并说明与 Switch 的差异。

已知边界：mini 的 aux 只在 EP 组归约，没有跨 CP 归约；CP+MoE 时 aux 是
每个序列切片的局部统计。生产实现要把 CP 也纳入 `f_i/P_i` 的全局归约，否则
长序列下的负载均衡会偏。

> 术语：**shared expert** 是所有 token 都经过的专家（不参与路由），用来承载
> 公共知识；**fine-grained expert** 是把 FFN 切成更多、更小的专家，使 top-k
> 组合更丰富。两者的参数量/计算量账本与普通稀疏专家不同，不能只按“专家数 ×
> 单专家参数”估算。

## 10.4 MoE 与 TP/PP/DP 的组合

| 维度 | 作用 | 注意 |
|---|---|---|
| EP | 切专家 | all-to-all，专家负载 |
| TP | 切每个专家的 GEMM | 通常 EP 优先于 TP |
| PP | 切层 | stage 边界不要切开一个专家 |
| DP | 切样本 | 复制所有专家（若不用 EP） |
| CP | 切序列 | 路由在本地 token 上；aux 的 `f_i/P_i` 需要跨 CP 全局归约（mini 未做） |

Megatron 的最佳实践（官方 MoE 指南）大意是：

- 尽量减少 TP/EP/PP 的叠加层数；
- 把 EP×TP 控制在 NVLink 域内；
- 多节点扩展优先用 PP；
- 专家层优先 EP 而非 TP；
- 序列 ≥8K 时考虑 CP；
- DeepSeek-V3 原训练使用 **DualPipe**（双向 chunk 调度，见 8.11.4）；Megatron
  的 DeepSeek 配方常用 1F1B/interleaved + all-to-all overlap，具体以 recipe
  与调度器代码为准，不要把两者混为一谈。

## 10.5 MoE 的数值与训练稳定性

MoE 比稠密模型更容易不稳定：

- 路由塌缩：所有 token 都去少数专家；
- 负载不均导致某些专家 OOM/空转；
- capacity 丢弃导致有效 batch 变小；
- aux loss 与主 loss 的权重难调；
- FP8 下路由器 logits/softmax 对精度敏感；
- 专家并行 all-to-all 的数值顺序与单卡不同。

常见措施：

- 路由器用 fp32 计算 softmax；
- aux loss / z-loss / router bias 调整；
- 监控每个专家的 token 数、概率均值、梯度范数；
- 对专家 GEMM 使用稳定 scaling；
- 先在小规模验证路由收敛，再放大。

## 10.6 EP 的通信账本

EP 的 all-to-all 通信量取决于路由结果，不能只用一个固定数估算。一阶模型：

```text
每个 token 被发给 k 个专家，平均每个 token 跨 rank 的 pair 数 ≈ k·(ep-1)/ep
dispatch 字节 ≈ tokens · k · (h·2 + 4) · (ep-1)/ep     # bf16 hidden + fp32 weight
combine 字节 ≈ tokens · k · h·2 · (ep-1)/ep
```

以**全局** `tokens=1M, k=2, h=7168, ep=8, bf16`、且 **`ep=dp`**（mini 的
布局，整个全局 batch 都在这个 EP 组里）为例，上面的公式是**整个 EP 组的
聚合字节数**：

```text
聚合 dispatch ≈ 1e6 · 2 · (14336+4) · 7/8 ≈ 25 GB
聚合 combine  ≈ 1e6 · 2 · 14336 · 7/8     ≈ 25 GB
每 rank 每个方向 ≈ 25 GB / 8 ≈ 3.1 GB
```

每个 rank 实际只发/收自己的 `tokens/ep = 125k` 个 token；一次 MoE 层往返
约 `6.3 GB/rank`。如果 `dp > ep`，每个 EP 组只看到全局 token 的 `ep/dp`，
per-rank token 数是 `tokens/dp` 而不是 `tokens/ep`；上面的 125k/6.3 GB
要按 `ep/dp` 缩放，不能直接套用。按**每方向** 400 GB/s 计（H100 NVLink 标称 900 GB/s 是
双向，单向约 450 GB/s，这里保守取 400），节点内约 **16 ms**；若跨节点
IB 每方向 50 GB/s，则约 **125 ms**。把聚合 50 GB 直接除以单卡带宽会高估
`ep` 倍，这是 EP 账本最常见的单位错误。这也是为什么 EP 要尽量留在 NVLink
域，以及为什么生产实现要做 all-to-all 与 GEMM 重叠。

另一个账本是**负载不均**：如果 20% 的专家收到 50% 的 token，all-to-all 的
尾延迟由最热专家决定，平均带宽再高也没用。aux loss、capacity、专家复制和
动态重路由都是在处理这个问题。

实现细节（路由、dispatch/combine、索引方向、梯度缩放）见第 25 章；这里只需
记住：**EP 的瓶颈不是 FLOPs，而是“不规则通信 + 尾延迟 + 显存”**。

## 10.7 什么时候值得上 MoE

MoE 不是免费的午餐：

- 同样的显存/带宽下，MoE 的训练吞吐可能低于稠密模型；
- all-to-all 对网络要求高；
- 推理时需要处理专家并行/负载均衡，部署复杂；
- 小 batch/短序列时，路由和通信开销占比高；
- 微调/对齐阶段，MoE 的稀疏路由可能不稳定。

它适合：

- 追求“每 token 计算量受限下的更大容量”；
- 有高速互联（NVLink/IB）和成熟 EP 工程；
- 推理侧能接受专家并行和更大的显存占用；
- 有数据/训练规模支撑稀疏专家的收敛。

## 10.8 2026 的前沿

- **DeepSeek 风格 MoE**：细粒度专家、共享专家、MLA、FP8（以及后续在
  Blackwell 上的 NVFP4 recipe）、aux-free 路由；具体后续版本以官方报告为准；
- **HybridModel**：MoE 与 dense/SSM/混合注意力统一调度，EP overlap；
- **grouped GEMM / device-initiated communication**：减少 all-to-all 和 kernel
  启动开销；
- **NVFP4/MXFP8 专家 GEMM**：Blackwell 上的量化与融合；
- **推理侧**：expert tensor parallel、batch-invariant MoE、vLLM fused MoE；
- **长上下文 MoE**：CP + EP 的组合、稀疏注意力 indexer。

## 10.9 本章小结

- MoE 用稀疏激活解耦容量与计算量，但引入路由、容量、负载均衡和 EP 通信。
- 专家并行是 all-to-all 问题，不只是“多放几个专家”。
- 路由数值稳定性、aux loss、FP8 精度都是大规模训练的难点。
- mini 同时实现了专家复制的 TP MoE 和 EP all-to-all 版本；前者用于理解数值，
  后者用于理解通信与梯度语义（第 25 章）。
- 2026 的竞争焦点是 DeepSeek 风格 MoE、FP4 和 HybridModel 的系统协同。

下一章讨论显存优化：当模型、激活、优化器状态同时膨胀时，怎样一块一块地省。

# 第 05 章 数据并行、梯度累积与全局 batch

> **第 II 部分导读（05–14）**
> 本部分逐个拆解 DP/TP/SP/PP/CP/EP，以及显存、精度、优化器和性能模型。
> 前置是第 04 章；读完应能解释每个 all-reduce/p2p 为什么存在、为一个
> 7B/70B 模型给出候选并行配置，并在“loss 对但梯度错”时知道先查哪里。
> 建议顺序 05→14；MoE 可在读完 08 后跳读 10，第 11–13 章可按瓶颈选读。

## 5.1 最熟悉的并行，也有最容易错的细节

数据并行（DP）的直觉很简单：每张卡放一份模型副本，喂不同数据，算完梯度后
同步。真正的工程问题在四个地方：

1. 梯度是 **平均** 还是 **求和**？
2. 梯度累积与 DP 如何共同定义 global batch？
3. TP/PP/CP 副本如何保证看到同一批数据？
4. 随机种子、dropout、数据打乱如何影响“可复现”？

## 5.2 数学形式

设第 `i` 个 DP rank 的 micro-batch 为 `B_i`，损失为 `L_i(θ)`。DP 的目标是：

```text
L(θ) = (1/d) Σ_i L_i(θ)
∇L = (1/d) Σ_i ∇L_i
```

因此同步梯度时应当 **求和后除以 d**（平均），而不是求和。如果每个 rank 的
micro-batch 大小相同，平均后等价于“把 d 个 micro-batch 拼成一个大 batch”。

如果使用梯度累积（gradient accumulation）`m` 步：

```text
global_batch = micro_batch × d × m
```

在每个 micro-step 中，loss 要除以 `m`（或最后统一除），否则梯度会放大 `m` 倍。
本教程在 pipeline schedule 的 `_backward_step` 中执行 `out.loss * 1/num_microbatches`，
然后 DP 再按 `d` 平均。

## 5.3 什么时候 all-reduce

最直接的实现是每个 micro-step 后立刻 all-reduce 梯度。但训练通常采用：

```text
zero_grad
for micro in accumulation:
    loss = forward_backward(micro)
    loss.backward()          # 梯度在本地累加
all_reduce(grads)            # 每个 optimizer step 一次
clip_grad_norm
optimizer.step()
```

这样每 `m` 个 micro-step 才通信一次。代价是梯度缓冲在本地保留更久，显存略高。
DDP 的 bucket 机制会把梯度按大小分桶，某个桶填满就启动异步 all-reduce，让通信
与反向计算重叠；Megatron 的 distributed optimizer 进一步用 reduce-scatter 把
梯度也切分。

### 一个常见错误

在每个 micro-step 都 all-reduce，但 loss 没有除以 `m`，等价于把学习率放大了
`m` 倍；或者 all-reduce 了但忘记除以 DP size，等价于放大 `d` 倍。两者都可能
表现为“训练能降 loss，但超参怎么都不对”。

## 5.4 数据切分：谁应该看到同一批数据

在一个 `TP×PP×CP×DP` 的网格中：

- **DP 维不同** 的 rank：必须看到不同样本（否则 DP 白做）；
- **TP/PP/CP 维不同** 的 rank：必须看到同一样本（否则模型各切片的输入不一致）。

因此数据加载器只按 **DP rank** 分片。mini 的 `BatchProvider` 采用
“先采样全局 batch，再切片”：

```python
seed = base_seed + step * 100003 + sample_offset
global_indices = randint(0, N, (micro * accum * dp,), generator=seed)
per_rank = micro * accum
rank_indices = global_indices[dp_rank * per_rank : (dp_rank + 1) * per_rank]
```

注意 seed **不包含** `data_parallel_rank`：所有 rank 采样同一个全局 batch，
只是取不同的连续切片。这样 step `t` 的并集恰好等于单进程的全局 batch，
DP=2 与 DP=1 的 loss 才可比（5.8.0 的并集不变量）。TP/PP/CP 副本共享同一个
`data_parallel_rank`，所以看到相同的切片。

生产 Megatron 用 `MegatronPretrainingSampler` /
`MegatronPretrainingRandomSampler`（采样）与 `BlendedDataset` /
`BlendedMegatronDatasetBuilder`（混合数据）实现同样的语义，并额外处理数据
混合权重、epoch、断点续训的位置；类名随版本可能调整，以当前源码为准。

### 数据并行与随机性

初始化要区分“逻辑模型一致”和“本地张量相同”两件事：

- **分片参数**（Column/Row/Vocab）在不同 TP rank 上是同一全局矩阵的不同切片，
  因此本地权重必须不同；所有 rank 用同一个 seed 会让切片变成复制。
- **PP stage** 持有不同层，初始化流要按 stage 偏移，否则不同 stage 的对应层
  会拿到相同权重。
- **复制参数**（norm、router 等）在 TP 组内必须一致。
- **DP 副本**：相同 `(tp_rank, pp_rank)` 的副本应得到相同初始化；不同 DP rank
  的数据由 sampler 区分（dropout RNG 可以按 DP rank 偏移）。

mini 在建模前调用 `set_model_parallel_init_seed(seed)`，分片层使用
`seed + 10007·(tp_rank+1) + 20011·pp_rank + 7919·layer_counter` 的本地
generator，复制层省略 TP 偏移（见 17.5 与附录 G）。数据由 `BatchProvider`
按 `data_parallel_rank` 区分。

## 5.5 DDP、ZeRO 与 FSDP 的关系

> 阅读提示：这一节会提前用到 TP/PP/FSDP 的概念。可以先把“DP 复制、
> ZeRO 分片状态、FSDP 分片参数”记成三层，细节在第 11、22、50 章展开。

| 方案 | 参数 | 梯度 | 优化器状态 | 通信 |
|---|---|---|---|---|
| 朴素 DDP | 复制 | 复制 | 复制 | 梯度 all-reduce |
| ZeRO-1 | 复制 | 复制 | 分片（按 DP） | 梯度 all-reduce + 更新后参数 broadcast |
| ZeRO-2 | 复制 | 分片（按 DP） | 分片 | 梯度 reduce-scatter + 更新后参数 all-gather |
| ZeRO-3 / FSDP | 分片 | 分片 | 分片 | 前向 all-gather、反向 reduce-scatter |
| Megatron distributed optimizer（flat） | 复制 | 分片 | 分片 | reduce-scatter + all-gather |

Megatron 的 distributed optimizer 与 ZeRO 的思路一致，但它和 TP/PP 的组合、
参数 buffer 布局、检查点格式是专门设计的；不同模式可能只在更新后广播参数
（更接近 ZeRO-1），也可能 reduce-scatter + all-gather（更接近 ZeRO-2）。
FSDP v2 在 2026 roadmap 中继续增强与 TP/EP/PP 的兼容。

## 5.6 mini 实现：`ZeRO1Optimizer`

mini 的 ZeRO-1 刻意做得可读：

```python
# 每个参数按 index % dp_size 决定 owner
self.owned = [i for i in range(len(params)) if i % world == rank]
# 只为 owned 参数建 AdamW
self.optimizer = torch.optim.AdamW(owned_params, ...)
# step 后把 owner 的新参数广播给其他 DP rank
for i, p in enumerate(all_params):
    owner = i % world
    src = comm.get_global_rank(group, owner)
    comm.broadcast(p.data, src=src, group=group)
```

它节省了约 `(d-1)/d` 的优化器状态，但没有切梯度，也没有做参数 all-gather。
生产实现会把参数 flatten 成连续 buffer，用 reduce-scatter/all-gather 减少小消息
数量，并把通信与计算重叠。阅读 mini 时重点关注：

- owner 的计算必须所有 rank 一致（参数顺序相同）；
- 非 owner 的参数在 step 后必须拿到新值；
- 检查点要保存 owner 信息和 DP size，恢复时布局一致。

## 5.7 梯度裁剪与全局范数

裁剪的范数应当是**整个模型副本**的全局范数，而不是单卡局部范数。mini 的
`global_grad_norm` 先对本地所有参数求平方和，再 all-reduce。注意 DP/CP 副本
持有相同梯度，直接对 world 求和会重复计数 `d×cp` 次，因此要除以副本数：

```python
dense_total = all_reduce(dense_local_sq_sum) / (dp_size * cp_size)
expert_total = all_reduce(expert_local_sq_sum) / cp_size   # mini: ep=dp，专家不是 DP 副本
total = dense_total + expert_total
norm = sqrt(total)
```

TP 分片和 PP 阶段持有不同参数，不能除；它们应当各计一次。但**复制参数**
（LayerNorm、Row Parallel bias、router、learned position）在每个 TP rank 上
都有相同的完整梯度，直接求和会重复计 `tp` 次；mini 用 `_is_replicated` 标记，
在求平方和时除以 `tp`。

**EP 专家是另一个例外**：mini 中专家按 EP 切分（`ep=dp`），不是 DP 副本，
所以专家项只除以 `cp`，不能套用 dense 的 `/dp`；否则专家对全局范数的贡献
会被少计。生产 `dp>ep` 时，专家还有 EDP 副本，要按 `edp` 归约后再缩放
（见 10.3.6、25.12.1）。这个细节在改变并行度时尤其容易错：TP 从 1 变 2，
梯度范数的口径必须保持一致，否则裁剪阈值相当于被改了。

## 5.8 数据管线：从 token 到 batch

生产数据管线通常包括：

1. **原始文本 → tokenized shard**（uint16/uint32）；
2. **索引**：记录每个 shard 的 token 数、文档边界；
3. **混合**：按权重采样多个数据集；
4. **packing**：把短文档拼进固定长度序列，附带 document mask；
5. **shuffle buffer**：避免严格顺序；
6. **分布式 sampler**：按 DP rank 切分；
7. **prefetch**：CPU/GPU pipeline 重叠。

mini 的 `SyntheticTokenDataset` 生成可预测的算术序列，`BinTokenDataset` 读取
uint16 memmap。它们不处理 document mask、packing 和混合，但保留了“每个 DP
rank 不同、TP/PP 副本相同”的核心契约。

### 5.8.0 全局 batch 的并集不变量

“每个 rank 看到不同数据”还不够，分布式采样还必须满足：

```text
step t 所有 DP rank 的样本并集 = 单进程在同一 step 看到的全局 batch
```

否则 DP=2 与 DP=1 的 loss 曲线不可比，你也无法用单卡基线定位并行 bug。
mini 的 `BatchProvider` 先按 `(seed, step, sample_offset)` 采样
`micro × accum × dp` 个索引，再让 rank `r` 取连续的第 `r` 段；因此各 rank
的 micro-batch 拼接起来正好等于单进程的全局 batch。`tests/unit/test_data.py`
锁定这条不变量。

生产实现通常用“全局索引流 + 按 DP rank 跨步取数”的 sampler，而不是每个
rank 重复采样整个全局 batch；两者对外部观察到的数据顺序应当等价。迁移时
如果你看到“DP 的 loss 每步都比单卡高一点”，先检查并集不变量，而不是先怀疑
模型并行。

### 5.8.1 packed/变长序列的 batch 与 loss 口径

packing 之后，“一个 micro-batch 有多少 token”不再固定：短文档拼进同一条
序列，pad 被去掉，但每个样本的有效 token 数不同。这时有三个口径必须同时改：

```text
global batch 用 token 数：B_tok = micro_tokens × dp × m
loss 用 token 加权：L = Σ_valid loss_t / Σ_valid tokens
梯度缩放：每个 rank 的分子/分母都要跨 DP（和 CP）归约
```

如果仍用“每个 micro-batch 的 mean loss 再平均”，长度不同的 micro-batch 会
被等权对待，短序列的 token 被高估。正确做法与 CP 的全局归一化相同：

```python
local_sum   = (token_loss * valid).sum()
local_count = valid.sum()
global_sum, global_count = all_reduce([local_sum, local_count], group=dp)
loss = global_sum / global_count
```

反向时每个 token 的梯度除以**全局**有效 token 数。`ignore_index`、padding、
文档分隔符、图像 token 都不能计入分母。

packing 还要求 attention/loss 同时理解文档边界：

- **position reset**：每个文档从 0 开始，RoPE 不能跨文档连续；
- **block-diagonal mask**：文档 i 不能看到文档 j；
- **loss mask**：只对目标文档的 token 计 loss；
- **样本边界**：一个 packed 序列可能跨多个文档，`attention_mask` 不能只按
  padding 处理。

mini 的 `data/packing.py` 实现了这些语义并有单测，但训练主链没有把
`attention_mask`/`position_ids` 传进 `_forward_step`；所以“模块测试通过”
不等于“packed 训练正确”。接入时要同时改数据、attention、CE 三处，并加一个
“packed vs 逐样本”的 loss/梯度等价性测试。

## 5.9 DP 的扩展极限

DP 的通信量与模型参数量成正比，与 DP size 的关系是 `2·N·bpe·(d-1)/d`
字节（bf16 下即 `4N(d-1)/d`）。当 `d` 很大：

- 梯度 all-reduce 的延迟和带宽成为瓶颈；
- 需要分层 all-reduce（节点内 NVLink、节点间 IB）；
- 梯度压缩/量化可能影响收敛；
- 故障域变大，单卡故障导致整个 all-reduce 卡住。

因此超大训练通常不会把 DP 开到最大，而是用 TP/PP/EP 分担模型维度，把 DP
控制在合理范围。具体比例要看集群拓扑和模型形状，后文性能建模会给出方法。

### 5.9.1 梯度压缩：省带宽，但别省掉收敛

当 DP 的 all-reduce/reduce-scatter 成为瓶颈时，除了换拓扑和分布式优化器，
还可以压缩梯度。三条主流路线：

**1. 低精度传输。** bf16 已经很常见；再把梯度压到 FP8（每张量或每 block
一个 scale）可以把通信量再降一半。要点：

- scale 的选择要覆盖离群值；per-block 比 per-tensor 稳，但元数据更多；
- 接收端要反量化回 bf16/fp32 再进优化器，不能直接在 FP8 上做 Adam；
- 误差会累积到参数更新里，必须用**误差反馈**（error feedback）：把本次
  量化误差加回下一次梯度。

**2. 低秩压缩（PowerSGD）。** 把一个 `N×M` 的梯度矩阵近似为
`P = U V^T`，其中 `U` 是 `N×r`、`V` 是 `M×r`。通信量从 `NM` 降到
`r(N+M)`；方阵时压缩比约 `N/(2r)`。代价是：

- 每个参数矩阵要做一次低秩分解/SVD 近似，计算不可忽略；
- 低秩近似有偏，必须配 error feedback（残差保留在本地）；
- 对 embedding、LayerNorm 这类形状不规则/小矩阵通常不划算；
- 压缩比越高，收敛越敏感，MoE 路由和 attention 的离群梯度尤其容易受影响。

**3. 稀疏化（top-k / random-k）。** 只传最大的 `k` 个元素及其索引，通信量
约 `k/N`，但索引本身要占字节；无误差反馈时收敛明显变差。随机稀疏在理论上
无偏，但方差大、尾延迟高，生产上不如低精度/低秩常用。

**什么时候值得做。** 先算一笔账：DP 每步的梯度通信是
`4N(d-1)/d` 字节（bf16），而每步计算约 `6N·tokens` FLOPs。如果通信时间
占比已经超过 30%，压缩才可能进入收益区；如果模型是 compute-bound，压缩
只会增加数值风险。另一个前提是**拓扑**：跨节点 IB 带宽远低于 NVLink，
压缩通常先作用于跨节点那一层（分层 all-reduce 的低带宽段），而不是全组
一刀切。

**验收协议。** 压缩不是“打开一个开关”：

```text
[ ] 固定 seed/数据/并行度，跑 200–500 步基线
[ ] 逐步提高压缩比，记录 loss 曲线、grad norm、下游评测
[ ] 与不压缩的基线做配对比较，而不是只看最终 loss
[ ] 监控压缩/解压时间，确认端到端 step time 真的下降
[ ] 对 MoE 额外看专家负载/路由熵；对长上下文看 attention 梯度
[ ] 出现 loss spike 时先回退压缩比，再查数据/精度
```

Megatron Core 的默认路径是 **bf16 梯度 + 分布式优化器**，梯度压缩属于
实验性/外挂优化；不同版本的开关差异很大，接入前按 29.4.1 核对“配置字段
是否真的被消费”。不要把“支持 FP8 训练”误读成“默认压缩梯度”：FP8 训练
通常指 GEMM 的输入/权重，不是 DP 的梯度 all-reduce。

## 5.10 本章小结

- DP 的梯度应平均，global batch = micro × DP × m。
- 数据只按 DP rank 分片；TP/PP/CP 副本必须同数据。
- 梯度累积期间本地累加，每个 optimizer step 通信一次。
- 梯度范数的口径要排除 DP/CP 副本重复计数。
- ZeRO/FSDP 用通信换显存，生产实现远比 mini 复杂，但状态分片思想一致。

下一章进入 Megatron 最核心的发明：张量并行。

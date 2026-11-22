# 第 50 章 分布式优化器与 FSDP 深潜

第 22 章实现了可读的 ZeRO-1，并给出了 flat reduce-scatter/all-gather 的
骨架；这一章把它展开成生产设计：Megatron 的 `DistributedOptimizer` 内部
有哪些 buffer、开关和失败模式，FSDP1/FSDP2/HSDP 与它是什么关系，什么规模
该选哪一种。

这一章的目标不是让你背参数名，而是让你能回答三个问题：

1. 每一步训练，参数/梯度/优化器状态在哪些 rank 上、以什么布局存在？
2. reduce-scatter/all-gather 与计算怎样重叠，瓶颈在哪里？
3. checkpoint 怎样保存/恢复/重分片，换并行度时哪些状态必须转换？

## 50.1 先把状态账算到字节

混合精度 AdamW 的每参数字节：

| 项 | 字节 | 是否随 DP 分片 |
|---|---:|---|
| bf16 权重 | 2 | 否（ZeRO-1/2） |
| bf16 梯度 | 2 | 否（ZeRO-1）；是（ZeRO-2） |
| fp32 master weight | 4 | 是 |
| Adam `m` | 4 | 是 |
| Adam `v` | 4 | 是 |
| 合计 | 16 | |

设总参数 `N`、TP `t`、PP `p`、DP `d`、优化器分片 `d_z`：

```text
每卡状态 ≈ 4N/(t·p)                 # bf16 权重 + 梯度
        + 12N/(t·p·d_z)             # fp32 master + m + v
```

70B、`t=8, p=1, d=8, d_z=8`：

```text
4×70/8 + 12×70/(8×8) = 35 + 13.1 = 48.1 GB/卡
```

再加激活、临时 buffer、通信 bucket，80GB 卡就很紧。把 `d_z` 理解为
“优化器状态被切成几份”：ZeRO-1 通常 `d_z=d`；如果只对部分参数做分片，
`d_z` 是有效份数。

> 注意：`12N` 里的 4 字节 master 是否真的存在，取决于实现。有的框架用
> bf16 参数 + fp32 优化器状态，不单独存 master；有的用 fp32 参数副本。
> 报显存账时必须写清“包含哪些状态”，否则差 25%。

## 50.2 Megatron DistributedOptimizer 的内部结构

生产实现的核心对象可以抽象成：

```text
参数列表（按 name 排序）
   │
   ├─ param_to_optimizer_map: 参数 → 优化器组/索引
   ├─ optimizer_state:       每个参数/每个 shard 的 Adam m/v/step
   ├─ grad_buffer:           连续梯度 bucket（按 dp 切分）
   ├─ param_buffer:          连续参数 bucket（all-gather 后使用）
   └─ shard_flat_metadata:   每个 shard 对应哪些参数的哪些元素
```

关键设计：

### 50.2.1 连续 buffer + 分桶

把所有参数按“先大后小/按模块”排成若干 bucket，每个 bucket 是一个连续
`torch.Tensor`。好处：

- reduce-scatter/all-gather 是大消息，带宽利用率高；
- 优化器状态连续，cache 友好；
- 可以把多个小参数的通信合并，减少启动开销。

bucket 大小的权衡：

- 太大：显存峰值高、重叠粒度粗、一个 bucket 的 tail 影响大；
- 太小：消息多、启动开销大、NCCL 效率低；
- 常见做法是按“每卡 bucket 大小”设上限（如几十到几百 MB），并按参数
  边界切分。

### 50.2.2 梯度 reduce-scatter

反向产生的是每个参数的完整梯度（在 DP 维复制）。分布式优化器在反向过程
或反向结束后：

```text
1. 把梯度拷进 flat grad_buffer（按 bucket）
2. reduce_scatter_tensor：每个 rank 得到自己 shard 的 DP 平均梯度
3. 只保留/使用本地 shard；完整梯度 buffer 可复用或释放
```

与 all-reduce 相比，reduce-scatter 每卡只发送约一半的字节：ring 下
reduce-scatter 是 `N·bpe·(d-1)/d` 字节，all-reduce 是
`2N·bpe·(d-1)/d`（bf16 即 `4N(d-1)/d`）。但 **reduce-scatter + 参数
all-gather 合计才与 all-reduce 相同**（各 `N·bpe·(d-1)/d`）。收益是每个
rank 只写 `1/d` 的梯度、省显存；后续 all-gather 参数也只需要 `1/d` 的输入。

### 50.2.3 参数 all-gather

优化器 step 后，每个 rank 只有自己 shard 的新参数。下一步前向需要完整参数：

```text
all_gather(shards) -> flat param_buffer -> 按 offset 散回各参数
```

生产实现会：

- 按 bucket 流水线化：前向用到某 bucket 前才 all-gather（`overlap_param_gather`）；
- 用双缓冲，避免 all-gather 与计算争同一块内存；
- 对不参与训练的参数（frozen）跳过；
- 与 PP/TP 的布局对齐（all-gather 只在 DP 组内）。

### 50.2.4 梯度/参数分片的变体

| 变体 | 保留完整梯度？ | 保留完整参数？ | 显存 | 通信 |
|---|---|---|---|---|
| ZeRO-1（broadcast 变体） | 是 | 是 | 16N/(tp·pp) 中 12N 分片 | 梯度 all-reduce + 更新后 broadcast |
| ZeRO-1（flat/DO 变体） | 是（打包前） | 是 | 同上 | reduce-scatter + all-gather |
| ZeRO-2 | 否（只留 shard） | 是 | 进一步省梯度 | 同上 |
| ZeRO-3/FSDP | 否 | 否（按需 all-gather） | 最省 | 每层 all-gather/reduce-scatter |
| `optim_grads` | 优化器只处理 shard | 是 | 省梯度 | 同 ZeRO-1/2 |
| `optim_grads_params` | 优化器处理 shard 参数 | 否 | 更省 | 更多 all-gather |

**ZeRO-1 的两种通信实现不要混为一谈**：第 5/22 章的可读版用“梯度
all-reduce + 更新后 owner broadcast”，每个 rank 仍持有完整梯度；Megatron
的 flat distributed optimizer 用“reduce-scatter 梯度 + all-gather 参数”，
每个 rank 只保留 1/dp 的梯度 shard。两者的显存收益（优化器状态分片）相同，
但通信原语、梯度 buffer 占用和重叠方式不同。看到“ZeRO-1”时先问是哪一种。

Megatron 的参数名（`optim_grads`、`optim_grads_params`、`use_distributed_optimizer`、
`overlap_grad_reduce`、`overlap_param_gather`）随版本变化；核心语义是
“优化器看到的是 shard，不是完整参数”。

## 50.3 mini 的 flat 实现：逐行对照

`mini_megatron/optim/distributed_optimizer.py` 把上面的结构压缩成一个
bucket。初始化：

```python
params = [p for p in model.parameters() if p.requires_grad]
numels = [p.numel() for p in params]
total = sum(numels)
padded = ceil(total / world) * world
shard = padded // world
master = zeros(padded, fp32)          # 参数（fp32 master）
grad_flat = zeros(padded, fp32)       # 完整梯度（临时）
exp_avg = zeros(shard); exp_avg_sq = zeros(shard)
decay_mask = zeros(padded)            # 逐元素 weight decay
```

反向之后：

```python
# pack
grad_flat.zero_()
for p, n in zip(params, numels):
    if p.grad is not None:
        grad_flat[o:o+n] = p.grad.reshape(-1).float()

# reduce-scatter + DP mean
reduce_scatter_tensor(grad_shard, grad_flat, op=SUM, group=dp_group)
grad_shard /= world

# AdamW on local shard（与 torch.optim.AdamW 同序）
shard.mul_(1 - lr * wd * decay_mask[shard_start:shard_end])
m.lerp_(g, 1-beta1)
v.mul_(beta2).addcmul_(g, g, value=1-beta2)
denom = v.sqrt() / sqrt(1-beta2**t) + eps
shard.addcdiv_(m, denom, value=-lr/(1-beta1**t))

# all-gather + scatter
all_gather(shards, shard)
full = cat(shards)[:total]
for p, n: p.data.copy_(full[o:o+n].view_as(p))
```

三个容易错的点：

1. **padding 区不能参与 decay/更新**：`decay_mask` 在 `total:` 之后为 0，
   散回参数时截断；
2. **weight decay 的顺序**：torch AdamW 先乘 `(1-lr·wd)` 再 `addcdiv`；
   顺序反了会有 1e-7 级差异，长期训练可能放大；
3. **梯度范数**：shard 互不重叠，全局范数是 `sqrt(sum(local_norm²))` 的
   **全 world** all-reduce；只归约 DP 组会漏掉 TP/PP 的切片。TP 复制参数
   （norm/router/row-bias）要按 `1/tp` 去重，CP 的求和副本按 `1/cp` 去重。
   否则每个 TP rank 用不同 clip 系数，一步后参数就分叉。

验证：

```text
tests/distributed_optimizer_equiv.py（DP=2, 3 步）
  loss_diff = 0.0
  param_max_diff = 0.0
  grad_norm_diff = 2.38e-07

tests/unit/test_distributed_optimizer.py
  单进程 3 步 vs torch AdamW 逐元素相等
  裁剪、state_dict roundtrip、padding/decay mask、布局不匹配拒绝
```

## 50.4 重叠：分布式优化器的性能核心

同步版分布式优化器的通信都在关键路径上。生产实现有三类重叠：

### 50.4.1 梯度 reduce-scatter 与反向重叠

反向是逐层产生梯度的。可以在某层梯度就绪后立即 reduce-scatter，与后面
层的反向并行：

```text
layer L   backward ──► reduce-scatter(L)
layer L-1 backward ──► reduce-scatter(L-1)
...
```

前提：

- 梯度 bucket 按层/模块切分；
- 通信流与计算流分离；
- 不等待所有梯度就绪再统一 reduce。

### 50.4.2 参数 all-gather 与前向重叠

下一步前向用到某 bucket 前才 all-gather：

```text
all-gather(bucket 0) ──► forward(bucket 0)
       all-gather(bucket 1) ──► forward(bucket 1)
```

这叫 `overlap_param_gather`。它把 all-gather 藏在上一 bucket 的计算后面，
但需要：

- 预分配参数 buffer；
- 正确的依赖/同步；
- 处理“第一次前向/评估/生成”没有上一 bucket 可藏的情况。

### 50.4.3 优化器 step 与通信重叠

优化器 step 本身是逐 shard 的；可以在 step 的同时 all-gather 已完成的
shard。代价是更复杂的调度和显存峰值。

### 50.4.4 重叠的性能模型

```text
T_step ≈ max(T_compute, T_reduce_scatter + T_all_gather) + T_nonoverlap
```

如果通信大于计算，重叠只能把“相加”变成“取大”，不能消除通信。此时要：

- 增大 TP/PP 减少每卡参数量；
- 用分层 reduce-scatter（节点内 NVLink + 节点间 IB）；
- 降低精度/压缩梯度（谨慎）；
- 换更强的网络/拓扑。

## 50.5 检查点：shard 的保存、恢复与重分片

分布式优化器的 checkpoint 不只包含“参数”，还包含：

```text
model shards:       每个 rank 的参数（TP/PP 分片）
optimizer shards:   master/m/v/step（DP 分片）
metadata:           全局参数名、形状、dtype、offset、并行布局
RNG/sampler:        复现所需状态
```

保存流程：

```text
1. 每个 rank 写自己的 model/optimizer shard（临时目录）
2. barrier
3. rank 0 写 metadata（参数 → shard 的映射）与 latest
4. 原子 rename
```

重分片的核心是**全局参数名 → 全局张量 → 新布局切分**：

```text
旧布局 shard -> 按 name/offset 拼成全局张量
            -> 按新 TP/PP/DP 切分
            -> 写新 shard + 新 metadata
```

难点：

- 参数名/顺序必须稳定；动态增删参数会破坏映射；
- TP 分片的语义（行切/列切/词表切）要按参数类型处理；
- 优化器状态与参数同形状，但 flat shard 的 offset 可能跨参数；
- EP 专家要按专家编号重排；
- 不同优化器（AdamW/Muon/混合）状态结构不同；
- 换 DP size 时，shard 的边界会变，必须重新分配。

验收：

```text
[ ] 新布局恢复后，前 10–100 步 loss 与旧布局连续
[ ] 参数/优化器状态逐元素对比（能对齐的部分）
[ ] 生成/评测结果一致
[ ] metadata 能拒绝不兼容布局，而不是静默加载部分参数
```

第 24 章给出了 mini 的非 ZeRO 重分片与 DCP 的用法；分布式优化器的
shard 重分片是同一思路，只是多了 flat offset 和 DP 维。

### 50.5.1 EP/FSDP2 重分片：元数据、专家 id 与验收

EP 与 FSDP2 让“参数名 → 全局张量”不再够用：同一参数在不同 rank 上可能是
不同的专家，DTensor 还多了一层 placement。一个可落地的 metadata schema：

```json
{
  "format": "mcore-dist/v1",
  "step": 12000,
  "parallel": {"tp": 8, "pp": 4, "dp": 8, "ep": 8, "cp": 1, "order": "tp-cp-ep-dp-pp"},
  "experts": {"num_experts": 64, "experts_per_rank": 8,
              "global_id": "ep_rank * experts_per_rank + local_index"},
  "params": {
    "decoder.layers.3.mlp.experts.linear_fc1.weight": {
      "global_shape": [7168, 2048],
      "dtype": "bf16",
      "shards": [{"rank": 17, "dim": 0, "start": 0, "end": 896},
                 {"rank": 18, "dim": 0, "start": 896, "end": 1792}],
      "expert_id": 27
    }
  },
  "optimizer": {"exp_avg": "...", "exp_avg_sq": "...", "step": 12000},
  "precision": {"recipe": "bf16", "amax": "..."},
  "data": {"consumed_tokens": 1.2e11, "sampler_state": "..."}
}
```

重分片步骤：

1. **读旧 metadata，建立全局参数表**：按 `global_id` 把 EP 专家分片映射到
   全局专家编号；DP 副本只保留一份（校验 checksum 一致）；
2. **拼全局张量**：TP 按行/列/词表语义拼接，PP 按层号拼接，EP 按专家 id
   排序；tied embedding 去重，shared expert 单独标记；
3. **按新布局切分**：新的 TP/PP/EP/DP 与 FSDP2 placement（`Shard(dim)` /
   `Replicate`）；专家按 `gid // new_experts_per_rank` 重新落位；
4. **优化器状态跟随参数身份**：`exp_avg/exp_avg_sq/step` 用参数名映射，
   不能按 flat buffer 的 offset 直接搬——flat offset 会随布局变化；
5. **写新 shard + 新 metadata**，临时目录 + 原子 rename，最后写 `latest`；
6. **验收**：固定 batch 的 logits 误差、10–100 步 loss 连续性、专家负载
   分布、生成结果；缺 shard/重复 shard/dtype 不符必须直接报错。

五个常见坑：

- 把 `experts_per_rank` 当成固定值：换 EP 后专家 id 的除法/取模会错位；
- DP 副本被当成不同分片拼进全局张量，导致重复计数；
- FSDP2 的 `Replicate` placement 被误当 `Shard(0)`，all-gather 后形状翻倍；
- 优化器 step 是标量但按张量分片保存，恢复时取了某个 rank 的局部值；
- metadata 只记录文件列表、不记录参数语义，加载时“能读但读错”。

## 50.6 FSDP1、FSDP2 与 HSDP

PyTorch 的 FSDP 与 Megatron 的分布式优化器解决的是同一类问题，但抽象层次
不同：

| 方案 | 分片单位 | 前向 | 反向 | 优化器状态 | 典型场景 |
|---|---|---|---|---|---|
| FSDP1 | 整层/扁平参数 | all-gather 整层 | reduce-scatter | 分片 | 通用 PyTorch |
| FSDP2 | 每参数 `DTensor` | 按参数 all-gather | reduce-scatter | 分片 | 组合性好、易与 TP/EP 拼 |
| HSDP | 节点内分片 + 节点间复制 | 两级 all-gather | 两级 reduce-scatter | 分片 | 多节点、NVLink 域内分片 |
| Megatron DO | flat bucket | 按需 all-gather | reduce-scatter | 分片 | 与 TP/PP/EP 深度集成 |

### 50.6.1 FSDP2 的 per-parameter sharding

FSDP2 把每个参数表示成 `DTensor`（`Shard(0)` 或 `Shard(1)`），前向时
all-gather 成完整参数，计算后释放；反向先算梯度，再 reduce-scatter 回
分片。优点：

- 组合性好：可以与 TP/EP、混合精度、量化组合；
- 检查点用 `ShardedStateDict`，重分片相对自然；
- 不需要“整层 flatten”的复杂 bucket 逻辑。

缺点：

- 小参数会产生很多小 all-gather；
- 调度器要处理 per-parameter 的依赖，CPU 开销可能成为瓶颈；
- 与自定义并行层（如 Megatron 的 TP 层）组合时，需要明确哪些参数由谁分片。

### 50.6.2 HSDP

HSDP = hybrid sharded data parallel：**节点内分片、节点间复制**（PyTorch
`HYBRID_SHARD` 的语义）。它把通信分成两级：

```text
节点内：NVLink all-gather / reduce-scatter（分片，快）
节点间：IB 梯度 all-reduce（复制，慢但消息只跨副本）
```

适合“节点内带宽远高于节点间”的集群：把需要频繁 all-gather 的参数分片留在
NVLink 域内，跨节点只同步副本梯度。代价是显存不是 `1/world`，而是约
`1/节点内 GPU 数`（节点间仍复制），需要按拓扑算账。若反过来写成“节点内复制、
节点间分片”，两级通信的带宽假设会完全错位。

### 50.6.3 与 TP/EP 的组合

- **TP**：参数已经按 TP 切分，FSDP/DO 只在 DP 维再分片；两者正交，但
  all-gather 后的参数布局必须与 TP 层期望的一致。
- **EP**：专家参数已经按 EP 分片；再按 DP 分片会改变“每个专家在哪些
  rank 上”的语义。生产实现要么把 expert 参数排除在 DO/FSDP 之外，要么
  用 expert-aware 的分片/重排。
- **PP**：每个 stage 独立分片，checkpoint 要按 stage 记录。

mini 直接禁止 `EP + distributed_optimizer`，这是有意的简化；生产实现会
更细地处理 expert 参数。

## 50.7 选型：什么时候用哪一种

| 瓶颈 | 首选 | 次选 | 不建议 |
|---|---|---|---|
| 优化器状态太大（长上下文/大模型） | Megatron DO / ZeRO-1 | FSDP2 | 无分片 AdamW |
| 参数本身放不下（单卡 < 一层） | FSDP2 / ZeRO-3 | PP + DO | 只靠 TP |
| 激活太大 | activation checkpointing / SP / CP | FSDP2（省参数） | 盲目加 TP |
| 多节点、节点内带宽高 | HSDP | DO + 分层通信 | 全局 FSDP |
| 需要与 TP/PP/EP 深度集成 | Megatron DO | FSDP2 + 自定义 | 纯 PyTorch DDP |
| 小模型/快速原型 | DDP | FSDP2 | 复杂 DO |

一个实用的决策顺序：

```text
1. 先算状态显存：16N/(tp·pp) 是否放得下？
2. 放不下 -> 先上 DO/ZeRO-1（省 12N 的大部分）
3. 还不够 -> FSDP2/ZeRO-3（省参数）
4. 激活是瓶颈 -> checkpointing/SP/CP，而不是继续分片参数
5. 通信是瓶颈 -> 分层/重叠/换拓扑，而不是加更多分片
```

## 50.8 失败模式清单

| 现象 | 根因 | 定位 |
|---|---|---|
| loss 与前一轮不一致 | shard offset/padding 错、decay mask 错 | 对比单卡 AdamW 的逐步参数 |
| 某些参数不更新 | 参数不在 flat buffer/owner 映射错 | 打印每个参数的 shard/owner |
| 梯度范数偏大/偏小 | 用完整梯度的公式算 shard 范数 | 用 `sqrt(sum(shard_norm²))` |
| all-gather 后参数错位 | bucket 顺序/offset 与保存时不一致 | 校验 metadata/参数名顺序 |
| 恢复后 loss 跳 | 只恢复了本地 shard，其他 rank 未同步 | 检查 all-gather 重建 |
| 换 DP size 加载失败 | shard 边界变了 | 走重分片工具，不要直接 load |
| 与 EP 组合后专家错乱 | 专家参数被二次分片 | 排除 expert 或 expert-aware 分片 |
| 通信不重叠 | 依赖/同步点/单 bucket | Nsight 看时间线 |

## 50.9 2026 的方向

- **FSDP v2 与 Megatron 的融合**：per-parameter sharding + TP/EP 组合，
  目标是“少写自定义并行层”；
- **通信更细粒度**：按 bucket/层的 all-gather，和计算流水线化；
- **与低精度结合**：FP8 参数 all-gather、NVFP4 优化器状态（实验性）；
- **检查点标准化**：DCP/ShardedStateDict 成为跨框架的通用格式；
- **自动并行**：根据显存/拓扑/模型形状选择 TP/PP/DP/FSDP 组合。

判断这些方向时，仍然用第 36 章的分级：release tag/源码/测试/规模证据
缺一不可。

## 50.10 本章小结

- 分布式优化器的本质是“参数/梯度/优化器状态在 DP 维的分片 + 连续 buffer +
  reduce-scatter/all-gather”。
- 16 B/param 要拆开看：哪些被 TP/PP 切、哪些被 DP 分片；账本差 25% 很常见。
- flat buffer 的 padding、offset、decay mask 是最容易错的地方；用单卡
  AdamW 做逐元素对比。
- 重叠（grad reduce、param gather、step）是性能核心，但依赖独立工作、
  不同流和无隐藏同步。
- checkpoint 要保存 shard + 全局 metadata；换并行度必须重分片，不能直接
  load。
- FSDP2/HSDP 与 Megatron DO 是同一问题的不同抽象；选型看瓶颈、拓扑和
  与 TP/EP 的组合需求。

下一章（第 51 章）讲“从 PyTorch 到 Megatron 的渐进式迁移与验收”；全书总结
在第 52 章，把原理、实现、生产、前沿、工程和商业串成一条学习与落地路径。

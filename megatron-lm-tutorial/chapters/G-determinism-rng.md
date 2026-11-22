# 附录 G 随机性、确定性与复现手册

> 这一章回答一个经常被忽略的问题：**同样的代码、同样的数据、同样的并行度，
> 为什么两次训练的 loss 曲线不一样？** 对小模型，这是“随机种子”问题；对大
> 规模并行训练，它是 RNG 流、数据顺序、通信归约顺序、kernel 非确定性和检查点
> 恢复共同作用的结果。

## G.1 为什么随机性是分布式问题

单卡训练的随机源很直观：参数初始化、dropout、数据 shuffle、数据增强、采样。
到了 TP/PP/DP/CP/EP，每个随机源都多了一个维度：**哪些 rank 必须看到相同的
随机数，哪些 rank 必须看到不同的随机数？**

答错会带来两类问题：

1. **该相同的没相同**：TP 组内复制的激活用了不同 dropout mask，前向/反向
   在数学上不再等价于单卡模型；loss 也许能降，但梯度是错的。
2. **该不同的没不同**：DP 副本使用完全相同的 dropout mask，副本间的随机性
   高度相关，正则化效果下降；数据顺序相同则更糟，等于每个副本都在学同一批
   样本。

因此，RNG 不是“设一个 seed 就完事”，而是并行语义的一部分。

## G.2 四类随机源

| 随机源 | 典型实现 | 并行下的要求 |
|---|---|---|
| 参数初始化 | `torch.manual_seed` + 各层 init | 同一 TP/PP 分片的副本必须一致；DP 副本必须一致 |
| dropout | `torch.nn.Dropout`、attention dropout | 复制张量在 TP 组内一致；分片张量可按 rank 不同 |
| 数据顺序 | sampler/shuffle/ packing | 每个 DP rank 不同；同一 DP rank 的 TP/PP/CP 副本相同 |
| 采样/增强 | multinomial、随机 mask、MoE 噪声 | 推理可复现；训练按语义决定是否分 rank |

最容易错的是 dropout，因为它藏在模型内部，且默认值常常是 0，问题会被掩盖。

## G.3 Megatron 的 RNG 抽象

Megatron 在 `megatron/core/tensor_parallel/random.py`（旧版本在
`megatron/mpu/random.py`）里提供了一个 **RNG 状态跟踪器**，核心思想是：

> 不只有一个全局 RNG，而是有若干条命名 RNG 流；进入某段代码时切换到对应流，
> 退出时保存该流前进后的状态，并恢复外层流。

概念上的接口：

```python
tracker = CudaRNGStatesTracker()

# 初始化：为每条流设置 seed
tracker.add("model-parallel-rng", seed_mp)
tracker.add("data-parallel-rng", seed_dp)

# 使用：在这段代码里，所有 torch RNG 调用都消耗指定流
with tracker.fork("model-parallel-rng"):
    y = dropout_on_tp_sharded_tensor(x)

# 退出后，model-parallel 流的状态已经前进；
# 外层（默认）流不受影响。
```

`fork` 的语义不是“重置为初始状态”，而是“接着这条流上次的位置继续”。因此
同一层在 step 0 和 step 1 会得到不同 mask，而同一条流在相同 step 的副本上
保持一致。

### G.3.1 两条基础流，以及 CP/EP 的归属

Megatron 的 `CudaRNGStatesTracker` 在较长时间里以两条命名流为核心：

| 流 | 谁使用 | 一致性要求 |
|---|---|---|
| `data-parallel-rng` | TP 区域**之外**的 dropout（复制的 hidden 等） | 同一 DP 组内所有模型并行 rank 一致；不同 DP rank 不同 |
| `model-parallel-rng` | TP 区域**之内**的 dropout（按 head/维分片的张量） | 每个 TP rank 可有不同序列；DP 组之间相同 |

- `data-parallel-rng` 变的是 **DP 维**：同一 DP 组的 TP/PP/CP 副本相同，不同
  DP 副本不同。
- `model-parallel-rng` 变的是 **TP 维**：TP 各 rank 不同，DP 组之间相同。

**不要凭印象给 EP 编一条 `expert-parallel-rng`**：不同版本对 MoE dropout、
专家内随机算子、CP 的 RNG 归属实现不同，有的复用 model-parallel 流，有的
在专家层内部再 fork。判断方法只有读当前 `random.py` / MoE 代码：

```text
grep -R "fork(" megatron/core | grep -i rng
grep -R "model_parallel_cuda_manual_seed" megatron/
```

CP 的 dropout 也有类似问题：序列分片后，每个 CP rank 处理不同的 token，通常
需要独立 RNG；但 attention 的 dropout mask 是否要跨 CP 一致，取决于实现
是否把 mask 视为“全局序列的一部分”。这类语义必须用“固定输入 + 固定 seed +
对比不同 CP 下的 loss/梯度”来验证，不能靠命名推断。

不同 Megatron 版本的 seed 偏移和 tracker 名称可能略有差异，读代码时以当前
`random.py` 的 `model_parallel_cuda_manual_seed` 为准。不要从旧博客抄常量。

### G.3.2 为什么复制张量要用 data-parallel 流

考虑 TP 的 MLP：

```text
ColumnParallel(gate/up) → 激活 → RowParallel(down) → all-reduce → hidden
```

`hidden` 在 TP 组内是**复制**的（每个 rank 都有完整 hidden）。如果后面的
`hidden_dropout` 在 rank 0 和 rank 1 用了不同 mask，那么：

- 前向：两个 rank 的 hidden 分叉；
- 反向：RowParallel 的输入梯度不再对应同一函数；
- 与单卡参考实现的等价性被破坏。

所以复制张量上的 dropout 必须走“同一 DP 组内一致”的 `data-parallel-rng` 流。
这也解释了为什么它叫 data-parallel：**变化发生在 DP 维，而不是 TP 维**。

### G.3.3 为什么 TP 区域可以用 model-parallel 流

在 TP 区域内，张量本身已经按 head/维度分片。每个 rank 处理的是不同的元素，
使用不同的 mask 序列不会破坏语义；但为了让同一 TP rank 在不同 step 可复现，
仍需要一条独立的、可保存/恢复的流。`model-parallel-rng` 就是这条流：TP 各
rank 不同，DP 组之间相同（同一 TP rank 的 DP 副本看到相同的模型并行随机性）。

### G.3.4 为什么 DP 副本要不同

DP 的每个副本处理不同的 micro-batch。如果所有 DP rank 的 dropout mask 完全
相同，副本之间仍然有“随机性”，但这份随机性与数据无关，等于人为降低了有效
正则化。更严重的是数据顺序：如果所有 DP rank 读同一批样本，DP 就退化成
“重复计算同一梯度”。

正确做法是：**模型初始化用同一 seed；复制张量的 dropout 用同一 DP 组内一致、
跨 DP 不同的流；TP 区域内的随机性用按 TP rank 区分的流。**

## G.4 各并行维度下的随机性矩阵

| 维度 | 张量布局 | dropout 流 | 数据 |
|---|---|---|---|
| TP（无 SP） | hidden 复制、heads/FFN 分片 | 复制部分用 data-parallel 流（组内一致）；分片部分用 model-parallel 流（TP rank 不同） | TP 副本相同 |
| SP | hidden 按 sequence 分片 | 分片位置可用 model-parallel 流；复制参数无 dropout | TP 副本相同 |
| PP | 不同层在不同 stage | 各 stage 独立；同一 DP rank 的 stage 相同 | 同一 DP rank 的 stage 相同 |
| CP | sequence 分片 | 各 rank 不同位置，可不同 | 相同 |
| DP | 完整副本 | 用 data-parallel 流，必须不同 | 必须不同 |
| EP | 专家分片 | **以当前源码为准**：可能复用 model-parallel 流，或在专家层内部再 fork；不要假设一定存在独立的 expert-parallel 流 | 与 DP 语义一致 |

这张表是排查“多卡 loss 对不上”的第一张检查表。

## G.5 激活重算与 RNG

激活重算（activation checkpointing / recomputation）在前向时不保存中间激活，
反向时重新执行前向。问题是：**重算时的 dropout 必须与原始前向完全一致**，
否则梯度对应的是另一个函数。

PyTorch 的 `torch.utils.checkpoint` 默认会保存/恢复 CPU/CUDA RNG 状态
（`preserve_rng_state=True`），这对普通 `torch.nn.Dropout` 足够。但如果你用了
自定义 RNG tracker（如 Megatron 的 `CudaRNGStatesTracker`），tracker 的
Python 字典状态不会被 PyTorch 自动保存。生产实现必须在 checkpoint 包装器里
显式保存/恢复 tracker 状态：

```python
state = tracker.get_states()
def recompute(*args):
    with tracker.set_states(state):
        return block_forward(*args)
checkpoint(recompute, ...)
```

否则会出现一个非常隐蔽的 bug：**不重算时 loss 正常，一开重算 loss 就漂**。
如果只在小模型上验证，可能完全看不出来。

## G.6 数据顺序、sampler 与断点续训

数据侧至少有三层状态：

1. **epoch/step**：当前走到哪个全局 step；
2. **sampler 位置**：当前 epoch 内已经消费到哪个 index；
3. **数据混合/课程**：当前各来源的采样权重。

一个可复现的分布式 sampler 需要满足：

```text
global_batch = micro_batch × grad_accum × dp_size
每个 DP rank 只取自己负责的 global batch 分片
同一个 DP rank 的 TP/PP/CP 副本取相同数据
恢复时从 (epoch, offset) 继续，而不是从头 shuffle
```

常见错误：

- 恢复时只恢复模型/优化器，sampler 从 epoch 0 重新开始；
- 每个 rank 用 `torch.randperm` 但 seed 相同，导致所有 rank 数据顺序一样；
- 用 `drop_last=False`，最后一个不完整 batch 在不同 world size 下形状不同；
- packing 后的样本边界在恢复时重新计算，导致跨文档 mask 变化；
- 数据增强用全局 RNG，恢复后与中断前不一致。

## G.7 逐位复现：能做到什么程度

| 层次 | 要求 | 现实可行性 |
|---|---|---|
| 结果可复现 | 多次训练指标在统计误差内 | 容易 |
| 统计可复现 | 固定 seed/数据/配置，曲线接近 | 中等 |
| 逐位可复现（同机同配置） | bitwise 相同 | 需要大量开关，性能下降 |
| 跨硬件逐位复现 | 不同 GPU/拓扑 bitwise 相同 | 基本不可行 |

逐位复现的常见开关：

```python
import torch
torch.use_deterministic_algorithms(True)
torch.backends.cudnn.benchmark = False
torch.backends.cudnn.deterministic = True
```

```bash
export CUBLAS_WORKSPACE_CONFIG=:4096:8
export NCCL_ALGO=Ring
export NCCL_PROTO=Simple
```

即使这样，也不能保证：

- FlashAttention 的反向使用原子加，默认非确定；需要 kernel 的 deterministic
  模式；
- 不同 NCCL 算法/拓扑的归约顺序不同；
- 不同 GPU 架构的 FMA/累加顺序不同；
- 不同 TP/PP/DP 布局的 all-reduce 分组不同；
- CUDA Graph/异步执行可能改变浮点归约顺序。

因此生产通常追求“统计可复现”，科研/合规场景才追求同机逐位复现。

## G.8 一个可执行的复现 recipe

### G.8.1 训练前

```text
[ ] 固定代码 commit / 容器镜像
[ ] 固定 Python/PyTorch/CUDA/NCCL/TE 版本
[ ] 固定 GPU 型号/数量/拓扑
[ ] 固定 TP/PP/DP/CP/EP
[ ] 固定 micro/global batch、seq_len、grad_accum
[ ] 固定数据版本/hash/混合权重
[ ] 固定 tokenizer
[ ] 固定 seed，并确认 data-parallel/model-parallel RNG 流语义正确
[ ] 固定 dropout/正则化配置
```

### G.8.2 训练中

```text
[ ] 每个 step 记录 loss/grad_norm/lr/tokens_per_sec
[ ] 记录每个 rank 的 RNG 状态 hash（可选，成本高）
[ ] 记录数据 sampler 的 (epoch, offset)
[ ] 记录 MoE 路由统计/专家负载
[ ] 记录 amax/scale（FP8）
[ ] 检查点保存 RNG/sampler/optimizer/GradScaler 状态
```

### G.8.3 恢复后

```text
[ ] 从 step+1 继续，而不是重复 step
[ ] loss 与中断前 10 步的均值/方差可比
[ ] 数据不重复/不跳过
[ ] 优化器 step 数一致
[ ] LR scheduler 一致
[ ] 并行布局一致（否则接受数值差异）
```

## G.9 mini-megatron 的现状与差距

mini 的做法：

- `Trainer.__init__` 用 `torch.manual_seed(config.train.seed)` 给所有 rank
  相同的全局 seed；
- 模型初始化因此一致；
- `BatchProvider` 用 `seed + step·100003 + offset` 为每个 step 创建独立的
  `torch.Generator`，先采样一个全局 batch，再按 DP rank 取连续切片；这样
  数据按 DP rank 区分、按 step 可复现，且各 rank 的并集等于单进程的全局
  batch；
- 分片层用 `set_model_parallel_init_seed(seed)` + TP/PP 偏移的本地 generator
  初始化，避免 TP 分片变成相同权重、PP stage 对应层重复；复制层用不带 TP
  偏移的流（见第 17 章）；
- 检查点保存 `python_rng` / `torch_rng` 状态；
- 没有实现 Megatron 的 `CudaRNGStatesTracker`，也没有把 **dropout** 的 RNG 流
  按 TP/DP 分离；初始化流只是这个机制的一个最小子集。

这意味着：

- `attention_dropout=0`、`hidden_dropout=0`（默认配置）时，mini 的等价性
  结论不受影响；
- 如果你把 dropout 打开，所有 rank 会使用相同的全局 RNG 序列：对于复制的
  hidden dropout，这恰好符合“同一 DP 组内一致”的要求；但 TP 区域内的
  dropout 本应使用按 TP rank 区分的 model-parallel 流，mini 没有区分；
  DP 副本也会相同，正则化语义与生产不同；
- 如果你同时打开 dropout 和 activation checkpointing，需要自己验证重算时的
  RNG 是否一致（PyTorch checkpoint 会保存全局 RNG，但 mini 没有额外的 tracker）。

这不是“实现错了”，而是刻意保留的教学边界：RNG tracker 属于生产工程，mini
用全局 seed + 显式数据 generator 把问题压缩到可读范围。生产落地时，请把
附录 G.3–G.5 的机制补上。

## G.10 排障清单：两次训练对不上

按成本从低到高：

1. **先确认是不是同一份代码/配置/数据**：diff 配置、数据 hash、tokenizer；
2. **关 dropout**：如果关掉后一致，问题在 RNG 流；
3. **固定数据**：用同一个 batch 反复训练，排除 sampler；
4. **world=1 vs world=N**：如果单卡一致、多卡不一致，查 TP/DP 的 RNG 语义；
5. **关 activation checkpointing**：如果关掉后一致，查重算的 RNG 恢复；
6. **关 FP8/bf16**：排除量化/精度；
7. **固定 NCCL 算法/拓扑**：排除归约顺序；
8. **检查恢复路径**：从 checkpoint 恢复后是否重复/跳过数据；
9. **最后才怀疑 kernel**：用 deterministic 模式复现，确认是否原子加/非确定
   kernel。

## G.11 本章小结

- 随机性是并行语义的一部分：复制张量在同一 DP 组内要一致，DP 副本要不同，
  TP 分片区域可用按 TP rank 区分的流。
- Megatron 用命名 RNG 流管理这件事；常见的是 `data-parallel` 与
  `model-parallel`。EP/MoE 的归属随版本变化（可能复用 model-parallel 或在
  专家层内 fork），必须读当前源码，不要凭名字假设存在 `expert-parallel` 流。
- 激活重算必须恢复 RNG 状态，否则梯度对应的是另一个函数。
- 数据 sampler 的位置、epoch、混合权重都是检查点的一部分。
- 逐位复现代价高且跨硬件几乎不可行；生产目标是统计可复现。
- mini 只做全局 seeding + 按 DP rank 的数据 generator，RNG tracker 是留给
  读者的工程练习。

下一附录把上述原则落到生产 runbook：启动前检查、监控阈值、故障分级与验收。

---

上一附录：[附录 F mini-megatron API 与命令](F-mini-api.md)  
返回目录：[SUMMARY](../SUMMARY.md)  
下一附录：[附录 H 生产训练 Runbook 与验收清单](H-production-runbook.md)

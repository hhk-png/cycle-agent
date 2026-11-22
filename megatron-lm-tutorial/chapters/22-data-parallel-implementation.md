# 第 22 章 数据并行与 ZeRO-1 实现

## 22.1 梯度同步

`mini_megatron/optim/adamw.py`：

```python
def all_reduce_gradients(model, group=None, average=True, *,
                         skip_expert_parallel=True):
    group = group or get_data_parallel_group()
    world = comm.get_group_size(group)
    ep = get_expert_parallel_world_size()
    skip_experts = skip_expert_parallel and ep > 1
    if any(getattr(m, "is_moe", False) for m in model.modules()):
        for p in model.parameters():          # 先物化 None 梯度
            if p.grad is None and p.requires_grad:
                p.grad = torch.zeros_like(p)
    for p in model.parameters():
        if p.grad is None: continue
        if skip_experts and getattr(p, "_is_expert_parallel", False):
            continue                           # 专家不是 DP 副本
        reduced = comm.all_reduce(p.grad, op=comm.SUM, group=group)
        p.grad.copy_(reduced)
        if average:
            p.grad.div_(world)
    if skip_experts:
        for p in model.parameters():          # 专家梯度再除以 ep
            if p.grad is not None and getattr(p, "_is_expert_parallel", False):
                p.grad.div_(ep)
```

- DP 组内平均；
- CP 组内求和（`average=False`）；
- 不存在梯度时跳过；
- **EP 专家参数在 DP 调用时跳过**（mini 取 `ep=dp`，它们是不同专家，不是
  副本），并在 DP 同步后除以 `ep`，把“各 rank 局部 mean loss 的梯度之和”
  还原成 global mean；`dp>ep` 时专家还有副本组，不能直接套用这条（25.12.1）；
  CP 调用时包含专家（CP 副本持有相同专家切片，应相加）；
- **MoE 的 `None` 梯度先物化为 0**：路由是数据相关的，某专家可能在一个 rank
  有梯度、另一个 rank 没有；直接跳过会让两边 collective 数量不同，导致挂起
  或消息错配；
- 生产实现会用 bucket、异步 all-reduce、reduce-scatter，避免逐参数小消息。

### 为什么先 DP 再 CP

顺序本身不影响线性运算，但影响：

- 梯度裁剪的范数口径；
- ZeRO 的参数广播；
- 调试时的中间值。

mini 先 DP 平均，再 CP 求和。对每个 DP 副本的同一 CP 坐标做平均，再在 CP 组
内汇总完整序列。也可以先 CP 再 DP；只要 group 不重叠，结果一致。

## 22.2 梯度累积

`Trainer.train_step` 不直接控制累积；`BatchProvider` 返回 `grad_accum` 个
micro-batch，schedule 内部对每个 micro-batch 反向并除以总数：

```python
scale = 1 / num_microbatches
loss = loss * scale
```

因此 `optimizer.step()` 看到的是“整个 global batch 的平均梯度”。如果换成
手动累积，等价写法是：

```python
for mb in micro_batches:
    loss = model(mb) / grad_accum
    loss.backward()
```

## 22.3 ZeRO-1

```python
class ZeRO1Optimizer:
    def __init__(self, model, optimizer, group):
        self.world = comm.get_group_size(group)
        self.rank = comm.get_group_rank(group)
        self.all_params = [p for p in model.parameters() if p.requires_grad]
        self.owned = [i for i in range(len(self.all_params))
                      if i % self.world == self.rank]
        owned = [self.all_params[i] for i in self.owned]
        # 实际代码与 build_optimizer 完全一致：1-D 参数和最后一维为 1 的
        # 2-D 参数不衰减；其余进 decay 组，并沿用原 optimizer 的
        # lr/betas/eps/weight_decay。
        no_decay = [p for p in owned if p.ndim <= 1 or p.shape[-1] == 1]
        decay = [p for p in owned if not (p.ndim <= 1 or p.shape[-1] == 1)]
        self.optimizer = AdamW([
            {"params": decay, "weight_decay": 0.1},
            {"params": no_decay, "weight_decay": 0.0},
        ], lr=..., betas=..., eps=...)

    def step(self):
        self.optimizer.step()
        for i, p in enumerate(self.all_params):
            owner = i % world
            src = comm.get_global_rank(group, owner)
            comm.broadcast(p.data, src=src, group=group)
```

### 为什么按参数张量分片

生产 ZeRO 会把所有参数 flatten 成连续 buffer，再按元素/块分片；这样：

- 每个 rank 的优化器状态是连续内存；
- reduce-scatter/all-gather 可以用大消息；
- 参数更新可以融合。

mini 按“参数张量”分片，简单但碎片化：一个 owner 可能拥有很多小 tensor，
broadcast 次数多。它仍然正确，且能展示状态分片的核心。

### 参数顺序必须一致

`all_params` 的顺序在所有 DP rank 上必须相同（`model.parameters()` 的注册
顺序）。如果某个 rank 动态增删参数，owner 映射会错。生产实现会把参数排序/
命名并校验。

### 检查点

`state_dict()` 保存：

```python
{"optimizer": ..., "owned": ..., "world": ..., "rank": ...}
```

`load_state_dict` 校验 world 一致。不同 DP size 的恢复需要重新分片，mini 不支持。

## 22.4 与 DDP 的差异

mini 没有实现 `torch.nn.parallel.DistributedDataParallel`，而是手动 all-reduce。
DDP 的优势：

- 梯度 bucket 与反向重叠；
- 通信 hook 自动注册；
- 支持 `find_unused_parameters` 等；
- 与 PyTorch 生态兼容。

Megatron 通常用自己的 DDP/分布式优化器，因为要控制 TP/PP 参数布局和通信。

### 22.4.1 FSDP2 与 Megatron distributed optimizer

ZeRO/FSDP 的核心都是“把训练状态从每卡一份变成分片”，但实现层次不同：

| 方案 | 分片对象 | 前向 | 反向 | 代表 |
|---|---|---|---|---|
| ZeRO-1 | 优化器状态 | 参数完整 | 梯度 all-reduce，更新后广播 | Megatron DO（broadcast 模式） |
| ZeRO-2 | 状态 + 梯度 | 参数完整 | 梯度 reduce-scatter，更新后 all-gather | Megatron DO（flat 模式，生产可释放完整梯度）/DeepSpeed |
| ZeRO-3 / FSDP | 状态 + 梯度 + 参数 | all-gather 参数 | reduce-scatter 梯度 | PyTorch FSDP1 |
| FSDP2 | 每参数 DTensor | all-gather 参数 | reduce-scatter 梯度 | `torch.distributed.fsdp.fully_shard` |
| HSDP | 节点内分片 + 节点间复制 | 两级 all-gather | 两级 reduce-scatter | FSDP hybrid |

注意 **通信形状 ≠ 内存形状**。mini 的 flat 实现是“ZeRO-1 的内存 + ZeRO-2
的通信”：`prepare()` 只额外维护一个 `grad_shard`，完整的 `grad_flat` 仍然
保留，不会因为 reduce-scatter 就自动省掉一份梯度显存。生产的
`optim_grads`/`optim_grads_params` 路径才会在 reduce-scatter 后释放完整梯度；
把它写成“mini 已经是 ZeRO-2 显存”会高估它省下的显存。

**FSDP2** 的关键变化是“per-parameter sharding”：

- 每个参数是一个 `DTensor`，按 `Shard(0)` 或 `Shard(1)` 切分，而不是把整层
  flatten 成一个大 buffer；
- 前向时按需 all-gather 成完整参数，计算后释放；
- 反向时先算梯度，再 reduce-scatter 回分片；
- 优化器状态天然按分片保存；
- checkpoint 用 `ShardedStateDict`，可以按新布局重分片。

FSDP2 的优点是组合性好（可与 TP/EP 组合、支持混合精度/量化），缺点是
all-gather/reduce-scatter 的调度复杂，且小参数会产生很多小通信。

**Megatron distributed optimizer** 更接近 ZeRO-1：

- 把参数/梯度 flatten 成连续 bucket；
- 每个 DP rank 只持有 1/dp 的优化器状态；
- 反向时梯度 reduce-scatter 到 owner；
- 前向/反向需要参数时 all-gather（ZeRO-3 风格）或广播更新后的参数
  （ZeRO-1 风格）；
- 与 TP/PP/EP 的 group 正交，需要仔细处理“哪些参数在哪个组分片”。

一个常见误区是把“ZeRO-1”当成“FSDP”：ZeRO-1 只省优化器状态，参数和梯度仍
完整；FSDP 连参数都分片，显存更省但通信更多。选择取决于瓶颈：

```text
优化器状态是瓶颈（长上下文/大模型）→ ZeRO-1 性价比最高
激活是瓶颈 → activation checkpointing / SP / CP
参数是瓶颈 → FSDP/ZeRO-3
```

**与 EP 的交互**：MoE 的专家参数已经按 EP 分片，不能再被 FSDP/ZeRO 二次
分片而不改变语义。生产实现会把 expert 参数排除在 DP 分片之外，或使用专门的
expert-aware 优化器。mini 直接禁止 `distributed_optimizer + ep > 1`，这是
一个有意的简化。

**检查点**：FSDP2 的 `ShardedStateDict` 可以按新布局重分片；Megatron 的
distributed optimizer 需要同时保存参数分片和优化器分片，并在恢复时校验
DP/TP/PP/EP 布局。第 24 章会展开。

### 22.4.2 可运行的 flat distributed optimizer

上面的表容易让人以为“distributed optimizer = ZeRO-1 = 广播参数”。生产实现
的关键其实是**连续 buffer + reduce-scatter + 参数 all-gather**。mini 在
`mini_megatron/optim/distributed_optimizer.py` 中实现了这个形状：

```python
# 1. 把所有参数 flatten 成一个 fp32 master buffer
master = zeros(padded_numel)          # padded 到 world 的整数倍
for p: master[o:o+n] = p.reshape(-1)

# 2. 反向结束后，把每个参数的 grad 拷进 flat buffer
grad_flat[o:o+n] = p.grad.reshape(-1)  # 缺失梯度补 0

# 3. reduce-scatter：每个 rank 只拿自己那一段的 DP 平均梯度
reduce_scatter_tensor(grad_shard, grad_flat, op=SUM)
grad_shard /= world

# 4. 只在本地 shard 上做 AdamW（m/v 也只存 shard）
# 5. all-gather 更新后的参数，散回各参数张量
all_gather(shards, master_shard)
scatter(master, params)
```

与第 22.3 节的 `ZeRO1Optimizer` 对比：

| 维度 | `ZeRO1Optimizer` | `DistributedOptimizer` |
|---|---|---|
| 优化器状态 | 按参数张量分给 owner | 按 flat 元素分片，连续内存 |
| 梯度同步 | 逐参数 all-reduce | 一次 reduce-scatter |
| 参数同步 | 逐参数 broadcast | 一次 all-gather |
| padding | 无 | 补到 `world` 的倍数 |
| 权重衰减 | 按参数分组 | 逐元素 decay mask |
| 通信次数 | O(参数量) | O(bucket 数) |

**padding 与 decay mask**：flat buffer 补零到 `world` 的整数倍；补出来的元素
在梯度里是 0、decay mask 是 0，散回参数时被截断，因此不会污染真实参数。
1 维参数（norm/bias）以及最后一维为 1 的 2 维参数，decay mask 为 0，其余为
1，复现 `build_optimizer` 的 weight-decay 分组（`p.ndim <= 1 or
p.shape[-1] == 1`）。

**梯度裁剪**：DP 平均后的梯度只存在于各 rank 的 shard 中。但 flat shard
同时是 **TP/PP 的局部切片**，全局范数必须在整个 world 上归约，而不是只
归约 DP 组；TP 复制参数（norm/router/row-bias）按 `1/tp` 去重，CP 因梯度
是求和再按 `1/cp` 去重。只对 DP 归约会让每个 TP rank 用不同的 clip 系数，
参数在一步后就分叉。

验证结果（Windows CPU，DP=2，3 步）：

```text
python tests/distributed_optimizer_equiv.py --out dopt.json
{"loss_diff": 0.0, "param_max_diff": 0.0, "grad_norm_diff": 2.38e-07}
```

TP=2/DP=2 的 4 进程回归：

```text
python tests/flat_tp_equiv.py --out flat_tp.json
# clip=0 与 clip=0.01 下，flat 的 norm 与 global_grad_norm 一致，
# param_diff < 1e-6
```

`tests/unit/test_distributed_optimizer.py` 还覆盖：

- 单进程 3 步与 `torch.optim.AdamW` 逐元素相等；
- 裁剪后 shard 范数不超过阈值；
- `state_dict`/`load_state_dict` 恢复 master/m/v/step；
- 布局变化（`padded_numel` 不同）时拒绝加载；
- padding 区不被 decay、1 维参数不被 decay。

**命令**：

```bash
python scripts/run_distributed.py --world-size 2 \
  --config configs/tiny_dp2_flat.json --steps 2 --checkpoint-dir ckpt
python -m pytest -q tests/unit/test_distributed_optimizer.py
```

**与生产 Megatron 的差距**（不是“实现细节”，而是会影响性能/规模的部分）：

- 单 bucket：生产按参数大小/设备切多个 bucket，并做 bucket 级重叠；
- 同步通信：生产有 `overlap_grad_reduce`、`overlap_param_gather`、
  `--distributed-optimizer-no-recompute` 等开关；
- 梯度分片：生产可在 reduce-scatter 后丢弃完整梯度，进一步省显存
  （`optim_grads` / `optim_grads_params` 变体）；
- 分层通信：生产按节点内/节点间做 hierarchical reduce-scatter；
- 检查点：生产保存“参数 shard + optimizer shard + 全局 metadata”，支持
  换 DP size 重分片；mini 只支持同 DP size 恢复；
- EP：生产有 expert-aware 的优化器分片；mini 禁止 `EP + distributed_optimizer`。

第 50 章会把这张差距表展开成生产实现的设计题。

## 22.5 梯度裁剪与全局范数

```python
def global_grad_norm(model):
    tp = get_tensor_model_parallel_world_size()
    ep = get_expert_parallel_world_size()
    dense = expert = 0
    for p in model.parameters():
        if p.grad is None: continue
        local = p.grad.float().pow(2).sum()
        if getattr(p, "_is_replicated", False):
            local = local / tp          # TP 组内复制参数只计一次
        if ep > 1 and getattr(p, "_is_expert_parallel", False):
            expert = expert + local
        else:
            dense = dense + local
    dense = all_reduce(dense) / (dp_size * cp_size)
    # EP 专家在 DP 维只存在一份，只除 CP 副本
    expert = all_reduce(expert) / (cp_size if ep > 1 else dp_size * cp_size)
    return sqrt(dense + expert)
```

- TP 分片/PP 阶段是不同参数，不能除；
- DP/CP 副本持有相同梯度，必须除；
- TP 组内的复制参数（norm/bias/router）只应计一次，mini 用 `_is_replicated`
  在平方和中除以 TP；否则 TP 越大，范数越虚高，裁剪越激进；
- EP 专家参数在 DP 维是**不同专家**，不是副本；如果按 DP 除，会把范数低估
  `dp` 倍，导致裁剪过松。mini 把 dense/expert 分开统计；
- 如果开了 ZeRO，参数仍然复制，梯度已平均，除 DP 仍正确（EP 除外）。

## 22.6 测试

- `tiny_dp2.json`：DP=2 + ZeRO-1 训练（`verify.py`）；
- `tiny_dp2_flat.json`：DP=2 + flat optimizer（pytest 冒烟 + `verify.py`
  的保存/恢复）；
- `tiny_tp2_dp2.json`：DP+TP+ZeRO（world=4，已加入 `verify.py` 的 4 卡
  矩阵；pytest 的 `test_distributed_training_smoke` 参数表不含它）；
- `tiny_pp2_dp2.json`：DP+PP（world=4）；
- `test_distributed_optimizer.py`：单进程 AdamW 等价、裁剪、checkpoint、
  padding/decay mask；
- `distributed_optimizer_equiv.py`：DP=2 下与 AdamW 的 loss/参数/grad_norm
  对比；
- `verify.py` 的检查点恢复覆盖单进程、PP=2、EP=2、flat optimizer，但
  **没有多进程 ZeRO-1 恢复**；`zero_equiv.py` 只比较单步数值；
- 单元测试 `test_training` 检查 LR/状态。

## 22.7 生产差距

已经有的：flat 单 bucket、reduce-scatter 梯度、all-gather 参数、shard
AdamW、同 DP size 的 shard checkpoint。

仍未实现：

- 多 bucket 与 bucket 级通信/计算重叠；
- 梯度分片（ZeRO-2）与参数分片（ZeRO-3/FSDP）；
- 分层 reduce-scatter（节点内/节点间）；
- `optim_grads_params` 一类“优化器只持有 shard 参数”的变体；
- 异构 DP（不同 batch/序列）；
- 换 DP size 的优化器状态重分片；
- 弹性/故障恢复。

## 22.8 本章小结

- DP 平均、CP 求和；梯度累积在 schedule 内做缩放。
- ZeRO-1 按 owner 分片优化器状态，step 后广播参数。
- 参数顺序、group、检查点布局必须一致。
- 生产实现用 flatten/bucket/异步通信，mini 是可读版本。
- 梯度范数要排除 DP/CP 副本重复计数。

下一章把数据、模型、优化器、日志串成 Trainer。

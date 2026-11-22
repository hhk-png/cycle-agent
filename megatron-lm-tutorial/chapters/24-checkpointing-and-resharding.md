# 第 24 章 检查点、恢复与重分片

## 24.1 为什么检查点是分布式问题

单卡检查点就是 `torch.save(model.state_dict())`。但在 TP/PP/DP 下：

- 每个 rank 只持有模型的一部分；
- 参数名和形状依赖并行布局；
- 优化器状态也可能跨 rank 分片；
- 恢复时可能换卡数/并行度；
- 千卡训练的保存不能阻塞训练。

因此生产 Megatron 使用**分布式检查点**：全局 metadata + 每 shard 文件 +
异步保存 + 重分片规划。

## 24.2 mini 的格式

`mini_megatron/checkpoint.py` 保存：

```text
checkpoint_dir/
  step_000010/
    metadata.json
    rank_00000.pt
    rank_00001.pt
    ...
  latest
```

每个 `rank_*.pt` 包含：

```python
{
  "format": "mini-megatron/v1",
  "step": step,
  "rank": rank,
  "world_size": ...,
  "dp_size": ...,
  "config": ...,
  "model": stage.state_dict(),
  "optimizer": optimizer.state_dict(),
  "python_rng": ...,
  "torch_rng": ...,
  "extra": ...
}
```

`metadata.json` 记录 TP/PP/CP/DP size 和文件列表；`latest` 指向最近 step。

## 24.3 保存流程

```python
directory.mkdir(parents=True, exist_ok=True)
torch.save(payload, directory / f"rank_{rank:05d}.pt")
barrier()
if rank == 0:
    write metadata.json
    write latest
barrier()
```

- 先写数据，再写 metadata/latest；否则 rank 0 可能在文件未落盘时更新指针；
- barrier 确保所有 rank 写完；
- 生产实现用临时文件 + rename 原子替换，避免半写；
- 异步保存需要 copy 到 pinned buffer/后台线程，且要处理训练继续修改参数的问题。

## 24.4 恢复流程

```python
metadata = read metadata.json
check parallel layout matches
payload = torch.load(rank_file)
stage.load_state_dict(payload["model"])
optimizer.load_state_dict(payload["optimizer"])
restore RNG
return step
```

mini 要求 **同布局恢复**：TP/PP/CP/DP 任一改变都会拒绝。这是有意的安全设计：
如果布局不匹配却强行 `load_state_dict`，可能只加载部分参数或形状错位，训练
表面能跑、实际是随机权重。

## 24.5 重分片

生产分布式检查点支持：

- TP 8 → TP 4；
- PP 4 → PP 8；
- DP 变化；
- 不同模型并行 order；
- 甚至 HF ↔ Megatron（Bridge）。

重分片的核心：

1. 读取全局 metadata，知道每个参数在各 rank 的 shard；
2. 按参数名建立全局张量（拼接 TP/PP 分片）；
3. 按新布局切分；
4. 写入新 shard；
5. 校验形状/数值。

mini 的 `sharding.py` 做了一个简化的“全量 state dict → TP/PP stage”切片，
用于等价性测试：

```python
load_stage_from_full(stage, full_state)
```

它支持：

- embedding/output 按词表行切；
- q/k/v/gate/up 按输出行切；
- out/down 按输入列切；
- norm/router 复制；
- PP 层选择/重命名。

这不等于生产重分片（不处理优化器状态、专家并行、不同 TP 布局的任意转换），
但展示了“按参数语义切片”的基本方法。

### 24.5.1 生产格式：torch.distributed.checkpoint

PyTorch 的 `torch.distributed.checkpoint`（DCP）是当前生产分布式检查点的主流
基础。它的核心不是“每个 rank 存一个文件”，而是：

```text
逻辑 state_dict（参数名 → 全局张量）
   │  planner：决定每个 rank 读写哪些 shard
   ▼
物理存储（每个 rank 写自己的 shard + 全局 metadata）
```

典型用法：

```python
import torch.distributed.checkpoint as dcp
from torch.distributed.checkpoint.state_dict import (
    get_model_state_dict, get_optimizer_state_dict,
)

state = {
    "model": get_model_state_dict(model),
    "optimizer": get_optimizer_state_dict(model, optimizer),
}
dcp.save(state, checkpoint_id=path, storage_writer=writer)
dcp.load(state, checkpoint_id=path, storage_reader=reader)
```

关键点：

- state_dict 里的张量可以是 **sharded tensor**，DCP 负责按 planner 拼接/切分；
- 加载时可以用新的并行布局，planner 做重分片（这是“换 TP/PP 继续训练”的
  基础）；
- 支持异步保存（`dcp.async_save(..., async_stager=...)`，或 storage writer 的
  异步 staging 选项）与多种后端（文件系统/对象存储）；
- Megatron 通过 `--ckpt-format torch_dist`（或 zarr 等）接入，具体参数名以
  当前版本为准。

DCP 不是万能的：它解决“张量如何分片/拼接”，但**参数语义**仍要由框架提供。
例如 q/k/v 的输出维切分、embedding 的词表行切分、MoE 专家的重排，都需要
Megatron 的 mapping 逻辑告诉 DCP “这个 shard 对应全局张量的哪一部分”。

### 24.5.2 优化器状态重分片

模型权重重分片相对直接：按参数语义拼接/切分。优化器状态更难，因为：

- Adam 的 `m/v` 与参数同形状，但可能被 ZeRO/分布式优化器按 flat buffer 分片；
- `step`、GradScaler、LR scheduler 是标量/复制状态；
- MoE 的专家状态要按 EP 重排；
- 不同优化器（AdamW/Muon/混合）状态结构不同。

一个安全的流程：

```text
1. 读取旧布局的全局参数 + 优化器状态
2. 建立“参数名 → 全局张量”的映射（含专家编号）
3. 按新 TP/PP/DP/EP 切分参数
4. 用同一映射切分 m/v/其它状态
5. 复制标量状态（step/scaler）
6. 保存为新布局
7. 用新布局恢复，跑 10–100 步，与旧布局的 loss 对照
```

第 7 步不能省。重分片“形状对得上”不代表“语义对得上”；一个常见的静默错误
是把 q/k/v 的 shard 顺序拼错，模型仍能跑，但 attention 已经坏了。

mini 实现了一个**非 ZeRO** 的 AdamW 优化器状态重分片（`checkpoint_reshard.py`）：

```python
from mini_megatron.checkpoint_reshard import (
    load_full_optimizer_from_checkpoint,
    split_optimizer_state_by_name,
    optimizer_state_dict_from_by_name,
)

full_opt, template = load_full_optimizer_from_checkpoint(
    "checkpoints/exp/step_000010", source_cfg, dp_size=2
)
local_opt = split_optimizer_state_by_name(
    full_opt, target_stage_states, tp_size=target_cfg.parallel.tensor_model_parallel_size
)
state_dict = optimizer_state_dict_from_by_name(
    local_opt[(pp_rank, tp_rank)], local_model_state, template
)
```

它处理：

- `exp_avg`/`exp_avg_sq` 与参数同形状的 TP 行/列切分；
- 复制参数（norm 等）的优化器状态一致性校验；
- `step` 等标量状态的复制；
- 从优化器索引反推参数名（依赖 `build_optimizer` 的 decay/no-decay 顺序）。

它**不处理**：

- ZeRO-1 的 `owned` 索引空间（需要先按原 DP 恢复）；
- EP 专家状态的专家重排；
- FSDP2 的 DTensor 分片。

这三类状态的共同点是：索引不再对应“全局参数列表”，而是对应“某个并行布局
下的本地分片”。重分片必须先还原布局语义，不能只做张量拼接。

### 24.5.3 异步保存与崩溃一致性

千卡训练的检查点可能有几十到几百 GB。同步保存会阻塞训练数秒到数分钟，
而且“写到一半掉电/被杀”会留下不可用的半成品。生产做法通常分三层：

**1. 原子可见性**

```text
写 step_000010.tmp/          # 所有 rank 写临时目录
barrier
rank 0 写 metadata.json.tmp
rank 0 rename(tmp, step_000010)   # 目录级原子可见
rank 0 更新 latest.tmp -> rename
```

读者只认 `latest` 指向的完整目录；如果保存中断，`latest` 仍指向旧检查点。
不要在数据文件写完前更新 `latest`——这是最常见的“恢复后 step 倒退/文件缺失”
原因。

**2. 异步 staging**

```text
训练线程：把张量 copy 到 pinned CPU buffer（或直接 D2H）
          → 放入后台队列 → 立即继续训练
后台线程：torch.save / DCP writer → 写盘 → fsync → 原子 rename
```

拷贝本身仍要占用训练流的带宽；真正的“零阻塞”需要：

- 双缓冲：训练写 buffer A 时，后台写 buffer B；
- 版本化：后台保存的是“第 k 步的快照”，而不是正在被修改的参数；
- 背压：保存跟不上时，宁可丢一次检查点，也不要让队列无限增长导致 OOM。

**3. 一致性校验**

- 每个 shard 记录参数名、形状、dtype、全局偏移和 checksum；
- 加载时先校验 metadata，再校验 shard 数量/大小；
- 保留最近 N 个检查点，避免一个坏文件毁掉整次训练；
- 定期做“恢复演练”：从最新检查点重启，跑 10–100 步，对比 loss/梯度。

DCP 的 `async_save` + `AsyncStager` 提供了异步写入的框架，但**它不替你解决
“训练继续修改参数”**：调用方仍要保证传给 writer 的是快照（`stage()` 会做
必要的 copy，具体行为以版本文档为准）。Megatron 的 `--async-save` 也是同样
的语义：异步的是 I/O，不是“参数可以边写边改”。

一个实用的保存频率模型：

```text
T_save = checkpoint_size / effective_write_bandwidth
overhead = T_save / save_interval
```

若检查点 200 GB、有效写带宽 2 GB/s，单次约 100 s；每 30 分钟保存一次，
开销约 5.6%。如果故障平均间隔 6 小时，恢复损失约 15 分钟（半间隔），
就需要在“保存开销”和“重算损失”之间取平衡。异步保存降低的是**阻塞时间**，
不降低写入带宽本身。

## 24.6 检查点与并行度的关系

| 变化 | mini | 生产 |
|---|---|---|
| 同布局恢复 | 支持 | 支持 |
| TP 变化（仅权重） | `checkpoint_reshard.py` | 分布式检查点重分片 |
| PP 变化（仅权重） | `checkpoint_reshard.py` | 支持 |
| DP 变化 | 拒绝（任何 DP 变化都改变 rank/文件映射；非 ZeRO 也不例外） | 支持 |
| 优化器状态重分片 | 非 ZeRO AdamW 支持；ZeRO/EP 不支持 | 支持 |
| EP 变化 | 不支持 | 支持（专家重排） |
| HF 互转 | 不支持 | Megatron Bridge |

> 注意“拒绝”的边界：mini 的**完整恢复**会校验 DP size；但
> `save_model_only_checkpoint(..., dp_size=4)` 可以写一个只含权重、不带
> 优化器/RNG 的新布局 checkpoint。前者是“继续训练”，后者是“导出权重”，
> 不要把两者混为一谈。

## 24.7 保存频率与成本

检查点不是越大越好：

- 70B 模型 bf16 权重 140 GB，加优化器状态可能 1 TB+；
- 千卡训练同时写盘会打爆存储；
- 保存时间会阻塞训练（同步保存）。

生产策略：

- 异步保存（copy + 后台线程/进程）；
- 分层存储（本地 NVMe → 对象存储）；
- 只保存必要 shard；
- 定期做完整校验（防止坏 checkpoint）；
- 保留最近 N 个 + 里程碑；
- 记录数据位置/RNG/配置，确保可复现。

### 24.7.1 保存成本的量级

粗略估算：

```text
checkpoint_bytes ≈ params × (weight_bytes + optimizer_bytes)
                 + activations?（通常不存）
                 + metadata
```

70B 模型：

```text
bf16 权重：70e9 × 2 ≈ 140 GB
Adam fp32 m/v：70e9 × 8 ≈ 560 GB
主权重 fp32：70e9 × 4 ≈ 280 GB
合计 ≈ 1 TB/次（未计分布式优化器分片）
```

如果存储带宽是 10 GB/s，同步写一次约 100 秒；千卡任务每 30 分钟存一次，
写放大和阻塞都很可观。异步保存把“写盘”移出关键路径，但仍占用存储带宽，
并需要处理“训练继续修改参数”的 copy-on-write/双缓冲问题。

保留策略建议：

```text
最近 N 个（快速恢复）
每 K 小时一个（回溯）
里程碑（发布/评测/合规）
最终 checkpoint（永久）
```

不要无脑保留所有 step：存储成本和恢复时的“选哪个”成本都会上升。

## 24.8 故障恢复

训练中断后：

1. 找到 latest 有效 checkpoint；
2. 校验 metadata/文件完整性；
3. 用相同或新的并行布局恢复；
4. 恢复数据 sampler 位置；
5. 恢复 RNG；
6. 从 `step+1` 继续；
7. 监控 loss 是否与中断前连续。

如果 loss 跳变，优先检查：

- 优化器状态是否恢复；
- 数据是否重复/跳过；
- LR scheduler 的 step 是否一致；
- 并行布局是否改变导致 reduction 顺序变化；
- 是否有参数未加载（missing keys）。

## 24.9 mini 的验证

- `test_two_steps_and_checkpoint`：**单进程**保存/恢复，并把恢复后的
  loss/grad_norm 与未中断轨迹逐步对比（不只比较“能否加载”）；
- `verify.py`：单进程 4→6 步恢复、PP=2 保存+恢复、EP=2 保存+恢复、
  flat distributed optimizer 保存+恢复；
- `test_optimizer_state_reshard_roundtrip`：AdamW 状态在 TP=2/PP=2 上
  切分后再合并，逐元素一致；
- `test_pre_ep_checkpoint_metadata_defaults_to_ep1`：旧 checkpoint 缺少 EP
  元数据时按 EP=1 加载。

**未覆盖**（不要误以为有）：

- 多进程 ZeRO-1 的保存/恢复（`zero_equiv.py` 只比较**一个 step** 的
  ZeRO-1 vs AdamW，不涉及 checkpoint）；
- `test_distributed` 里没有 PP checkpoint 测试，PP 恢复只在 `verify.py`；
- FSDP2/DTensor、EP 专家重排、异步保存的崩溃一致性；
- DP size 变化的**完整**恢复（权重-only 导出可以，优化器/RNG 不行）。

## 24.10 一个可运行的权重重分片工具

`mini_megatron/checkpoint_reshard.py` 实现**两层**重分片：模型权重，以及
非 ZeRO AdamW 的优化器状态。它们是两套 API，不要以为调用权重函数就顺带处理
了优化器。

权重：

```python
from mini_megatron.checkpoint_reshard import (
    load_full_state_from_checkpoint,
    reshard_full_state,
    save_model_only_checkpoint,
)

full = load_full_state_from_checkpoint("checkpoints/exp/step_000010", cfg, dp_size=2)
new_stage_states = reshard_full_state(full, target_cfg)  # 新 TP/PP
save_model_only_checkpoint("checkpoints/exp_resharded", stage_states=new_stage_states,
                           config=target_cfg, dp_size=4)
```

优化器状态（非 ZeRO）：

```python
from mini_megatron.checkpoint_reshard import (
    load_full_optimizer_from_checkpoint,
    merge_optimizer_state_by_name,
    split_optimizer_state_by_name,
    optimizer_state_dict_from_by_name,
)
# 先还原旧布局 → 按参数名合并 → 按新 TP/PP 切分 → 重新打包成 state_dict
```

它做的事：

1. 按 rank 布局读取每个 `(pp, tp)` 分片（DP/CP 副本只读一个）；
2. 按参数语义拼接 TP 分片（行/列/复制）；
3. 把 stage 内的局部层号还原成全局层号；
4. 用 `make_stage_state` 切成目标 TP/PP；
5. 写 model-only checkpoint（明确不含 optimizer/RNG）；
6. 对非 ZeRO AdamW，额外合并/切分 `exp_avg`、`exp_avg_sq`、`step`
   （`merge_optimizer_state_by_name` / `split_optimizer_state_by_name`）。

它 **不做**：

- ZeRO-1/FSDP2 的优化器状态重分片（索引空间与全局参数列表不同）；
- EP 专家重排；
- 任意并行 order；
- 异步/分布式写入。

测试 `test_checkpoint_reshard.py` 除了权重的 TP=2/PP=2 往返，还验证了
AdamW 状态的重分片往返逐元素一致。

这正好对应生产重分片的两层：权重映射相对直接，优化器/专家/布局转换才是
复杂部分。测试 `test_checkpoint_reshard.py` 用 TP=2/PP=2 的分片往返验证了
“拼接后再切分”与原始全量 state dict 逐元素一致。

## 24.11 异步检查点：时序、暂存与一致性

同步保存的代价是**训练停等**：一个 70B 模型的状态约 1 TB，写到本地 NVMe
可能几十秒，写到对象存储可能几分钟。异步保存的目标不是“让写盘消失”，而是
把“训练必须等待”的部分压缩到一次内存快照。

### 24.11.1 一个正确的最小流程

```text
step boundary
  → 冻结版本号 step
  → staging：把参数/优化器状态复制到 CPU/pinned buffer（或 COW 快照）
  → 后台线程/进程：序列化 + 写临时目录
  → 所有 shard 写完 + checksum 校验
  → rank 0 写 metadata.json，最后原子更新 latest
```

关键点：

1. **快照必须在 step 边界**：优化器 step 与 zero_grad 之间；否则参数、梯度、
   Adam 的 `step` 可能来自不同 step；
2. **staging 是必需的**：后台线程读原张量时，训练可能已经改写它们；必须
   先复制，或用 copy-on-write/版本化存储；
3. **metadata/latest 最后写**：读者看到 `latest` 时必须能读到完整的一组
   shard；半写目录只能被忽略；
4. **失败可重试**：临时目录带 `step-uuid`，失败后清理；不要覆盖上一个
   可用 checkpoint。

### 24.11.2 三种实现层次

| 方案 | 快照方式 | 优点 | 风险 |
|---|---|---|---|
| 后台线程 + `torch.save` | 全量 copy 到 CPU | 简单、可控 | 内存翻倍；GIL/序列化慢 |
| DCP `async_save` + `AsyncStager` | `stage(state_dict)` 复制到 CPU/pinned | 与 `torch.distributed.checkpoint` 集成、支持对象存储 | stager 线程安全/内存开销 |
| 独立进程/对象存储 writer | 共享内存/管道/直接读快照 | 不阻塞训练线程 | 进程生命周期、故障清理 |

PyTorch DCP 的 `AsyncStager` 契约值得记住：

- `stage(state_dict)` 返回一个“训练安全”的副本，默认复制到 CPU；
- `dcp.save` 在后台线程/进程对 staged dict 序列化；
- `should_synchronize_after_execute=True`（默认）时，`async_save` 返回前会
  调用 `synchronize_staging()`，保证可以安全修改原 state_dict；设为 `False`
  可把 staging 与后续计算重叠，但必须自己在合适位置同步；
- `BlockingAsyncStager` 支持 pinned memory；`DefaultStager`（较新版本）用
  `StagingOptions` 控制 pinned/shared/async copy，且**不是线程安全的**，每个
  线程要有自己的实例。

```python
import torch.distributed.checkpoint as dcp

future = dcp.async_save(
    state_dict=state,
    checkpoint_id=tmp_dir,
    storage_writer=writer,
    async_stager=stager,
)
# ... 继续训练；在需要读/改 state 或退出前：
future.result()
```

### 24.11.3 一致性清单

异步保存最容易在“看起来成功”时留下坏 checkpoint。逐项检查：

```text
[ ] 快照点固定（optimizer.step 之后、zero_grad 之前）
[ ] 参数/优化器/RNG/sampler 偏移来自同一 step
[ ] staging 完成后才允许训练改写源张量
[ ] 每个 shard 有 checksum；metadata 记录 shard 列表与并行布局
[ ] latest 只在全部 shard 成功后原子更新
[ ] 保存线程异常能传到主线程（不要静默吞掉）
[ ] 保存期间 OOM/被杀后，旧 checkpoint 仍可加载
[ ] 恢复后跑 10–100 步，loss 与未中断训练连续
```

**内存账本**：staging 一份 fp32 优化器状态约等于当前状态显存；70B、DP=8、
ZeRO-1 每卡约 105 GB，复制后 CPU 内存至少再要 105 GB。不是所有集群都允许
“用内存换停等”，要按节点内存和保存频率算。

**存储账本**：保存带宽要满足 `state_bytes / save_interval < 可用写带宽`；
否则后台队列越积越长，最终还是要停等。对象存储的尾延迟和限流比本地 NVMe
更不可预测，建议限制并发写、分片大小适中（几十 MB 到几百 MB）、失败重试
带指数退避。

### 24.11.4 故障模式

| 现象 | 原因 | 处理 |
|---|---|---|
| `latest` 指向半写目录 | metadata 先于 shard 写 | 先写 shard/checksum，最后原子更新 |
| 恢复后 loss 跳变 | 参数与优化器状态跨 step | 快照点固定在 step 边界 |
| 保存时 OOM | staging 复制了全量状态 | 分桶 staging、pinned 上限、降频 |
| 保存线程卡住但训练继续 | 异常被吞/队列无限增长 | 有界队列 + 主线程健康检查 |
| 多 rank 保存不一致 | 某 rank 失败但其它 rank 已更新 latest | 全局 barrier/commit 协议 |
| 读 checkpoint 时文件缺失 | 写对象存储的部分成功 | 清单校验 + 重试 + 版本目录 |

### 24.11.5 mini 的差距

`mini_megatron/checkpoint.py` 是同步保存：先写每个 rank 的 `rank_*.pt`，
barrier 后写 metadata 与 `latest`；没有临时目录 + 原子 rename，也没有后台
线程。它验证的是**布局/恢复语义**，不是生产级持久性。练习（附录 L）可以让
你补上原子写入与异步 staging，并用“保存中途 kill -9”做验收；生产实现还应
接入 DCP 的 `async_save`/对象存储 writer，并按上面的清单做恢复演练。

## 24.12 本章小结

- 分布式检查点 = 全局 metadata + 分片 + 原子/异步写入。
- mini 的训练恢复要求同布局；`checkpoint_reshard.py` 支持权重和非 ZeRO
  AdamW 状态的 TP/PP 重分片，但不支持 ZeRO/EP 重排。
- 重分片按参数语义拼接/切分，不是简单复制文件。
- 保存频率要平衡存储/时间/恢复点。
- 异步保存的核心是 step 边界快照 + staging + 后台写 + 最后原子发布；
  staging 内存和存储带宽都要算账。
- 恢复后 loss 不连续，先查优化器/数据/RNG/布局。

下一章拆解 MoE 实现。

# 第 51 章 从 PyTorch 到 Megatron：渐进式迁移与验收

> **本章导读（51）**
> 第 48–50 章讲“一个规模化的训练系统长什么样”，本章讲“怎样把你的单卡
> 脚本安全地变成它”。前置是 03、05–10、15–24；如果你已经有一个能跑通的
> PyTorch 训练脚本，这一章可以当作迁移工单逐节执行。读完后你应当能为每一个
> 新打开的并行维度写出**可复现的数值验收**，而不是靠“loss 看起来还行”。

把一个单卡训练脚本改成 Megatron 风格，最常见的失败不是“矩阵切错了”，而是
**一次改了太多东西**：同时换数据采样、优化器、精度、并行度和启动器，最后
loss 爆炸时无法定位是哪一处引入的。本章给出的方法只有一句话：

> **一次只加一个维度，每一步都保留一个可回退的数值基线。**

## 51.1 迁移的两种形态

先把问题分清，因为“迁移到 Megatron”至少有两种完全不同的工作：

| 形态 | 起点 | 终点 | 主要风险 |
|---|---|---|---|
| A. 脚本并行化 | 单卡/单机 PyTorch 训练脚本 | 能多卡、多机运行的训练作业 | 语义漂移：数据、loss、梯度、检查点 |
| B. 框架替换 | 已有 DDP/DeepSpeed/FSDP 训练栈 | Megatron Core + 启动器/recipe | API 漂移、checkpoint 转换、性能回退 |

本章以 A 为主线，因为它是 B 的基础：如果你不理解“为什么这里要 all-reduce”，
换成任何框架都只是记住了一组开关。B 的差异集中在 51.12 与第 29、35 章。

### 51.1.1 反模式清单

下面五种做法会让迁移从一天变成一个月：

1. **先上 TP=8、PP=8、CP=2，再调 loss**。任何一处索引错都会被其他维度的
   噪声掩盖；你无法判断是 TP 的梯度少了 all-reduce，还是 PP 的 loss 缩放错。
2. **用“loss 曲线形状”当验收**。不同并行布局的浮点归约顺序不同，曲线本来
   就会有微小差异；没有逐元素基线，你分不清“数值误差”和“逻辑错误”。
3. **只比较最后一个 loss**。前向对、反向错，或者第 0 步对、第 1 步发散，
   都会被一个标量掩盖。
4. **在并行化之前就换精度/换优化器/换数据**。一次只动一个变量。
5. **没有回退点**。每个阶段都应能回到上一阶段的配置与检查点。

## 51.2 迁移前的基线：四个数字与一份权重

在改任何代码之前，先让原脚本产出可比较的基线。最少需要：

```text
B1. 固定初始权重（state_dict 的 hash 或文件）
B2. 固定数据顺序（前 N 个 batch 的 input_ids/labels 的 hash）
B3. 前 10 步的 loss（逐步，不是平均值）
B4. 前 10 步的 global grad norm（裁剪前）
```

如果原脚本没有梯度范数，先在 `optimizer.step()` 之前加一行
`torch.nn.utils.clip_grad_norm_(model.parameters(), ...)` 的返回值记录；
它不改变训练，只暴露数值。

### 51.2.1 让基线可复现的最小设置

```python
import os, random, numpy as np, torch

def seed_everything(seed: int) -> None:
    os.environ["PYTHONHASHSEED"] = str(seed)
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
```

注意两个边界：

- **确定性设置会降低吞吐**。基线阶段打开，性能阶段关掉；不要把
  `deterministic=True` 带进生产吞吐测试。
- **CPU 多线程的归约顺序也会变**。做逐元素对比时固定
  `OMP_NUM_THREADS=1`、`MKL_NUM_THREADS=1`（mini 的等价性测试脚本就是这么
  做的），否则 1e-6 级的差异会随负载抖动。

### 51.2.2 基线的粒度

| 对比对象 | 推荐粒度 | 典型容差 | 能抓到什么 |
|---|---|---|---|
| 初始权重 | 逐 tensor、逐元素 | 0（同一文件） | 初始化 seed/切分错 |
| 前向 logits | 固定输入、逐元素 | 1e-5–1e-6 | 切分、mask、RoPE、norm 错 |
| loss | 逐步标量 | 1e-5 | loss 归一化/缩放错 |
| 梯度 | 选定参数的逐元素 | 1e-5 | 反向集合通信/缩放错 |
| 参数更新后 | 第 1–3 步逐元素 | 1e-5 | 优化器状态/裁剪错 |
| 检查点往返 | 保存→加载→再前向 | 0 或 1e-7 | 布局/键名/精度错 |

容差不是越严越好：不同 reduction 顺序在 fp32 下通常有 1e-7–1e-5 的差异。
**先记录参考实现自己的重复运行差异**，再设阈值；不要凭感觉写 `1e-8`。

## 51.3 第 0 步：把脚本拆成四层

并行化之前先做一次“无行为变化”的重构。目标是让每个并行维度只改一层：

```text
config      模型形状、并行度、训练超参、数据路径（可序列化）
model       纯 nn.Module：不读环境变量、不知道 rank
step        一次前向/反向/更新：不关心进程如何启动
launcher    torchrun/调度器：只负责 rank/world_size/rendezvous
```

mini 的对应关系：

| 层 | mini 文件 | 生产对应 |
|---|---|---|
| config | `mini_megatron/config.py` | Megatron 的 argparse/YAML + recipe |
| model | `parallel/transformer.py`、`parallel/layers.py` | `megatron.core.models.gpt` + specs |
| step | `training.py`、`pipeline/schedules.py` | `training/training.py` + forward_step |
| launcher | `scripts/run_distributed.py`、`cli.py` | `torchrun`/Slurm/NeMo Run |

### 51.3.1 重构的验收

重构后不训练，只做三件事：

```python
# 1. 配置往返
cfg = ExperimentConfig.from_json(path)
assert ExperimentConfig.from_dict(cfg.to_dict()).to_dict() == cfg.to_dict()

# 2. 模型构造可复现
torch.manual_seed(1234)
m1 = TransformerStage(cfg.model, cfg.parallel)
torch.manual_seed(1234)
m2 = TransformerStage(cfg.model, cfg.parallel)
assert all(torch.equal(a, b) for a, b in zip(m1.parameters(), m2.parameters()))

# 3. 单步前向/反向与旧脚本一致（固定输入、固定权重）
```

如果第 3 条过不了，先别并行化。**重构引入的 bug 和并行引入的 bug 混在一起，
是迁移中最贵的调试。**

## 51.4 第 1 步：数据并行（DP）

DP 是第一个要打开的维度，因为它最容易验证，也最容易“看起来对了”。

### 51.4.1 必须同时改的四件事

| 事项 | 单卡 | DP |
|---|---|---|
| 数据 | 每个 step 取一个 batch | 每个 rank 取**不同**的 batch；同一 step 的并集等于全局 batch |
| loss | 本地 batch 的 mean | 各 rank 的 mean 再按样本/token 数加权归约 |
| 梯度 | 本地梯度 | all-reduce 后除以副本数（或等价的 sum + 全局 loss 缩放） |
| 日志 | 本地 loss | 只在 rank 0 打印；记录的是全局归约后的值 |

mini 的约定是：

```text
global_batch_size = micro_batch_size × dp_size × grad_accum_steps
```

`BatchProvider` 用 `data_parallel_rank` 做偏移，保证同一步不同 rank 看到不同
数据；`Trainer.train_step` 先 `all_reduce_gradients(..., average=True)`，再裁剪，
最后更新。

### 51.4.2 三种“数据看起来对了”的假象

1. **每个 rank 都用同一个 seed 取数据**：loss 会下降，但有效 batch 没有变大，
   相当于在原地重复训练。检查方法：打印每个 rank 前 3 个 batch 的
   `input_ids[0, :8]`，必须不同。
2. **global batch 公式漏了 grad accumulation**：训练能跑，但学习率与预期
   不匹配。检查方法：用 `estimate`/日志确认
   `tokens_per_step = global_batch_size × seq_len`。
3. **loss 先本地平均再跨 rank 平均**：当每个 rank 的 token 数不同（packing、
   变长序列）时，短序列会被高估。正确做法见 5.8.1：分子/分母分别归约。

### 51.4.3 DP 的验收

```bash
# 单进程基线
python -m mini_megatron.cli train --config configs/tiny.json \
  --steps 4 --checkpoint-dir "" --jsonl .verify_tmp/mig/base.jsonl

# DP=2（ZeRO-1）：从 tiny.json 派生，确保 warmup/max_steps 等超参完全一致
python - <<'PY'
import json
cfg = json.load(open("configs/tiny.json"))
cfg["parallel"].update({"distributed_optimizer": True})
cfg["train"]["max_steps"] = 4
json.dump(cfg, open(".verify_tmp/mig/dp2_same.json", "w"), indent=2)
PY
python scripts/run_distributed.py --world-size 2 \
  --config .verify_tmp/mig/dp2_same.json --steps 4 --checkpoint-dir "" \
  --jsonl .verify_tmp/mig/dp2_same.jsonl

# DP=2（flat optimizer）
python scripts/run_distributed.py --world-size 2 --config configs/tiny_dp2_flat.json \
  --steps 4 --checkpoint-dir ""
```

> 不要直接拿 `configs/tiny_dp2.json` 和 `configs/tiny.json` 比损失曲线：前者
> 的 `warmup_steps=1`，后者是 2，学习率在第 0 步就不同。**“全局 batch 相同”
> 不等于“训练超参相同”**。这一点在真实迁移里同样成立：recipe 里的
> warmup、weight decay、精度、数据混合都要逐项对齐，否则 loss 差异无法归因。

验收标准不是“loss 一样”，而是：

- 对齐训练超参后，DP=2 与 DP=1 的每步 loss/grad norm 在 1e-5 内一致
  （本机对 `tiny` 的 4 步记录为逐元素 0.0）；
- 用 `tests/zero_equiv.py --out zero.json` 对比 ZeRO-1 与普通 AdamW 的
  参数/状态；
- `tests/distributed_optimizer_equiv.py --out dopt.json` 对比 flat optimizer
  与 AdamW（mini 记录为参数差 0.0、grad norm 差约 2e-7）；
- 检查点保存/恢复后，继续 2 步的 loss 与不中断训练一致。

如果 DP=2 的 loss 在第 0 步就对不上，先查数据：每个 rank 的
`input_ids[0, :8]` 必须不同，但并集必须等于单进程的全局 batch。
`mini_megatron/data/dataset.py` 的 `BatchProvider` 现在按“先采样一个全局
batch，再按 DP rank 切片”实现，`tests/unit/test_data.py` 会验证这条并集
不变量；如果把它改回“每个 rank 独立采样”，DP 与单进程的 loss 曲线就不再
可比。

## 51.5 第 2 步：优化器状态分片（ZeRO-1 与 flat）

DP 打开后，显存里最贵的是优化器状态：混合精度 AdamW 约 16 B/参数
（fp32 master weight 4 + exp_avg 4 + exp_avg_sq 4 + 梯度 4，具体口径见
4.3 与 A.14）。ZeRO-1 只分片这部分，不分片参数。

### 51.5.1 两种实现的差别

| 维度 | `ZeRO1Optimizer` | `DistributedOptimizer`（flat） |
|---|---|---|
| 状态 | 每个 rank 拥有约 1/dp 的参数状态 | 同样只更新本地 shard |
| 梯度 | 先 all-reduce 平均，再各自裁剪 | reduce-scatter 得到 shard 的平均梯度 |
| 参数 | 每个 rank 保有完整副本 | 更新后 all-gather 参数 |
| 通信 | all-reduce（2× 字节） | reduce-scatter + all-gather（同量级，消息更少） |
| 教学价值 | 读起来像“谁拥有谁更新” | 接近生产 distributed optimizer 的形状 |

flat 版本的关键不变量：

```text
flat buffer 的顺序在所有 rank 上必须一致
padding 位置的梯度必须为 0，且不参与 AdamW
参数 all-gather 之后，每个 rank 的模型副本必须逐元素一致
```

### 51.5.2 迁移时最容易错的三处

1. **裁剪用的是 shard 范数**。全局范数必须覆盖所有 rank 的参数；flat 实现
   在 `prepare()` 后对本地 shard 求平方和，再**在整个 world 上归约**
   （不是只归约 DP 组），因为本地 shard 同时也是 TP/PP 的切片。TP 复制
   参数按 `1/tp` 去重，CP 按 `1/cp` 去重。只对 DP 归约会让每个 TP rank
   用不同的 clip 系数，参数在一步后就分叉。
2. **非 owner 的参数没有更新**。ZeRO-1 在 step 后必须 broadcast 新参数；
   忘掉这一步时，前向仍能跑，但各 rank 从第 1 步开始分叉。
3. **检查点只存了本地 shard，却没存 owner 映射**。恢复时 DP size 变了就会
   静默错位；mini 明确拒绝这种恢复，生产要用带 metadata 的分布式检查点。

### 51.5.3 验收

```bash
python tests/zero_equiv.py --out .verify_tmp/zero.json
python tests/distributed_optimizer_equiv.py --steps 3 --out .verify_tmp/dopt.json
python tests/flat_tp_equiv.py --out .verify_tmp/flat_tp.json
```

前两个验证“与普通 AdamW 等价”，第三个验证“TP+DP 下全局 clip norm 的
口径一致”。**只验证 loss 不够**：优化器 bug 往往在第 1 步之后才显现。

## 51.6 第 3 步：张量并行（TP + SP）

TP 是第一个真正改变模型内部形状的维度。建议按下面的顺序逐层打开，每层都
做一次前向/反向对比：

```text
Embedding → Attention（QKV/out）→ MLP（fc1/fc2）→ LM head/CE
→ MLP 的 Row 输出 all-reduce → LayerNorm 的 SP 布局 → 注意力头的切分
```

### 51.6.1 切分规则（必须背下来）

| 层 | 切什么 | 前向通信 | 反向通信 |
|---|---|---|---|
| ColumnParallelLinear | 输出维度 | 无（输出已是分片） | 输入梯度 all-reduce |
| RowParallelLinear | 输入维度 | 输出 all-reduce | 无（输入梯度已分片） |
| VocabParallelEmbedding | 词表维度 | 输出 all-reduce（各 rank 部分和相加）；SP 下再 scatter | 本地梯度；SP 下与 scatter 配对的 all-gather |
| 词表并行 CE | 词表维度 | 全局 max/sum log-sum-exp | 梯度按本地词表切片 |
| LayerNorm/Dropout（SP） | 序列维度 | 进入 Column 前 all-gather | 从 Row 出来时 reduce-scatter |

一个常见的误解是“Column 前向也要 all-reduce”。不需要：Column 的输出本来
就是按输出维切开的，下一层 Row 会在自己的输入维上消费它。**Column 的反向
才需要把输入梯度跨 TP 求和**，因为每个 rank 只看到了输入的一部分贡献。

### 51.6.2 初始化：切分不是复制

TP 的每个 rank 必须拿到**同一个完整矩阵的不同切片**，而不是各自随机初始化
后再切片。mini 用 `set_model_parallel_init_seed` 让同一条随机流按 TP rank
偏移；生产实现则在 `initialize_model_parallel` 后按 TP rank 设置 seed，并
保证 LayerNorm 等复制参数在 rank 间一致。

验收方法：

```python
# 同一 TP rank 重复构造应完全一致；不同 TP rank 的 Column 权重应不同，
# 但拼起来应等于 TP=1 的完整权重。
```

`tests/parallel_equiv.py` 与 `mini_megatron/sharding.py` 提供了这条路径：
先构造 TP=1 的完整 state_dict，再按规则切片加载到 TP=2 的 stage。

### 51.6.3 SP 不是“再开一个开关”

sequence parallel 把 LayerNorm/Dropout 的激活从“每个 TP rank 一份完整序列”
改成“按序列切分”。它改变的是**布局**，不是数学：

- 进入 Column 前需要 all-gather（或等价的 reduce-scatter 配对）；
- 从 Row 出来后需要 reduce-scatter；
- dropout 的 RNG 必须与完整序列布局一致，否则重算/推理会对不上；
- 生成时逐 token 解码会破坏 `seq % tp == 0`，所以 mini 在推理阶段关闭 SP。

### 51.6.4 TP 的验收

```bash
python tests/parallel_equiv.py --case tp --world-size 2 --out .verify_tmp/tp.json
python tests/parallel_equiv.py --case tp_bias --world-size 2 --out .verify_tmp/tp_bias.json
python tests/parallel_equiv.py --case tp_learned --world-size 2 --out .verify_tmp/tp_learned.json

python scripts/run_distributed.py --world-size 2 --config configs/tiny_tp2.json
```

验收必须同时看：

- loss 与 TP=1 的差（mini 记录为 0.0）；
- 选定参数的梯度逐元素差（约 1e-8–1e-7）；
- 复制参数（LayerNorm、bias、router）是否在 TP rank 间一致；
- 全局梯度范数的口径是否随 TP 改变（mini 对复制参数除以 `tp`）。

## 51.7 第 4 步：流水线并行（PP）

PP 把层切成 stage，改变的是**执行顺序与激活的生命周期**，不是单层数学。
迁移时先把 `pp=2` 跑成 GPipe，再换 1F1B；这样能把“切分错”和“调度错”分开。

### 51.7.1 必须明确的五个契约

1. **stage 边界**：每层的参数只在一个 stage 上；embedding 在第一段，
   final norm/LM head 在最后一段。
2. **激活形状**：`[micro_batch, seq, hidden]`（CP 下 seq 是本地切片）；
   TP 下 hidden 可能是分片，stage 间必须约定一致。
3. **loss 只在最后一段**：中间 stage 的输出是 hidden，不是 loss；反向时
   梯度从最后一段逐段回传。
4. **p2p 的 tag 与全局 rank**：必须用 `get_global_rank(group, peer)` 换算，
   不能用 `pp_rank ± 1` 当全局 rank。Windows/portable 后端下这个错误会直接
   挂起或串消息。
5. **micro-batch 数与气泡**：`m` 个 micro-batch、`pp` 个 stage 时，
   GPipe 的气泡比例约 `(pp-1)/(m+pp-1)`；1F1B 的 in-flight 激活峰值是
   `max(1, min(m, pp-1))`（见 8.7 与 49.8）。

### 51.7.2 权重对齐：先证明“同一模型”

PP 的等价性测试不能只比较 loss，因为两个 stage 的随机初始化可能不同。
正确做法是：

```text
构造 PP=1 的完整 state_dict
  → 按层号切给 pp_rank
  → 加载到 PP=2 的每个 stage
  → 固定输入，比较 logits/loss
  → 反向，比较各 stage 的梯度
```

mini 的 `sharding.make_stage_state` 与 `load_stage_from_full` 就是这条路径；
`tests/parallel_equiv.py --case pp` 在进程内完成同样的对比。

### 51.7.3 调度验收

```bash
# 1F1B
python scripts/run_distributed.py --world-size 2 --config configs/tiny_pp2.json
# GPipe
python scripts/run_distributed.py --world-size 2 --config configs/tiny_pp2_gpipe.json
# PP=2 + DP=2
python scripts/run_distributed.py --world-size 4 --config configs/tiny_pp2_dp2.json
```

GPipe 与 1F1B 的对比要注意：**只比较最终 step 的 loss/grad_norm**。两种调度
的 micro-batch 执行顺序不同，逐步的浮点结果本来就可能不同；mini 的测试
（27.6）就是这么定义的。若最终 step 都不一致，先查 loss 是否被
`1/grad_accum` 缩放了两次。

### 51.7.4 VPP：不要一上来就上

interleaved VPP 把每个 rank 的层再切成 `v` 个 chunk，气泡更小，但引入了
`(microbatch, chunk)` 二维路由。mini 提供了可执行的 interleaved GPipe
（`pipeline/interleaved_exec.py`）与规划器（`pipeline/interleaved.py`），
但训练主循环未接入 1F1B VPP。迁移顺序应是：

```text
PP 的 GPipe 对 → PP 的 1F1B 对 → VPP 的 chunk 映射对 → VPP 的 1F1B
```

## 51.8 第 5 步：上下文并行（CP）与专家并行（EP）

这两个维度不是“默认要开”的：CP 为长序列，EP 为 MoE。它们都应该在
DP/TP/PP 已经稳定之后再引入。

### 51.8.1 CP 的四个不变量

1. 序列按 CP rank 切块，每块只在自己 rank 上计算；
2. 因果 mask 必须用**全局位置**，不能按本地块重新从 0 开始；
3. 注意力需要看到其他 rank 的 K/V（mini 用 all-gather；生产用 ring）；
4. loss 的分子/分母要跨 CP 归约，K/V 的梯度要跨 CP 求和（不是平均）。

验收：

```bash
python tests/parallel_equiv.py --case cp --world-size 2 --out .verify_tmp/cp.json
python scripts/run_distributed.py --world-size 2 --config configs/tiny_cp2.json
python scripts/run_distributed.py --world-size 4 --config configs/tiny_cp2_pp2.json
```

### 51.8.2 EP 的四个不变量

1. 专家按 EP rank 切分；token 通过 all-to-all 去往持有目标专家的 rank；
2. dispatch/combine 的索引方向必须成对（`permute` 与 `unpermute` 的逆）；
3. 每个专家收到的 token 来自所有 EP rank，所以专家梯度要除以 `ep`
   （全局 mean 的口径）；
4. 路由是数据相关的，某个专家可能在某 rank 上没有梯度；集合通信必须
   让所有 rank 发出同样数量的调用（mini 用零梯度物化来保证）。

验收：

```bash
python tests/parallel_ep.py --world-size 2 --out .verify_tmp/ep.json
python scripts/run_distributed.py --world-size 2 --config configs/tiny_ep2.json
python scripts/run_distributed.py --world-size 4 --config configs/tiny_tp2_ep2.json
```

### 51.8.3 组合的合法性与顺序

推荐的打开顺序是：

```text
DP（含数据/loss 口径）
  → ZeRO-1 / flat optimizer
  → TP（+SP）
  → PP（GPipe → 1F1B）
  → CP（长序列）
  → EP（MoE）
```

原因不是“理论上必须”，而是**每一层都依赖前一层的 rank 语义**：TP 的 group
建错时，DP 的归约范围也会错；PP 的全局 rank 换算错时，CP 的 group 也会错。
每加一维，先跑一次 `python -m mini_megatron.cli info --config ...`（或等价
的 rank 布局打印），确认每个 rank 的 `(dp, tp, pp, cp, ep)` 坐标符合预期。

## 51.9 每个阶段的验收清单

把下面这张表当作“放行门”。任何一行不通过，都不要进入下一阶段。

| 检查项 | DP | ZeRO/flat | TP | PP | CP | EP |
|---|---|---|---|---|---|---|
| 配置整除关系 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| rank 布局打印正确 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 固定权重可复现 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 数据在不同副本上不同 | ✓ | ✓ | — | — | — | ✓ |
| loss 与基线一致 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 梯度与基线一致 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 全局 clip norm 口径 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 检查点保存/恢复 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 生成/推理一致 | — | — | ✓ | — | — | — |
| 性能不出现数量级回退 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

“性能不出现数量级回退”不是要求每一维都更快：TP 在小模型上可能更慢
（通信占比高），PP 在 micro-batch 少时气泡大。这里要抓的是**配置错误导致
的 10× 回退**，例如每层都触发一次 all-reduce、或 stage 间传了完整 logits。

## 51.10 一个可执行的迁移演练

下面这条链路把“基线 → DP → TP → PP”串起来，全部在 CPU 上可跑。建议在
一个空目录里执行，并把每一步的输出保存下来做对照。

### 51.10.1 环境与基线

```bash
cd megatron-lm-tutorial
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1   # Windows: set OMP_NUM_THREADS=1

python -m mini_megatron.cli train --config configs/tiny.json \
  --steps 4 --checkpoint-dir .verify_tmp/mig/base --jsonl .verify_tmp/mig/base.jsonl
```

记录 `base.jsonl` 的最后一行：

```json
{"step": 3, "loss": ..., "grad_norm": ..., "lr": ...}
```

### 51.10.2 DP：先证明“全局 batch 不变”

```bash
python - <<'PY'
import json
cfg = json.load(open("configs/tiny.json"))
cfg["parallel"].update({"distributed_optimizer": True})
cfg["train"]["max_steps"] = 4
json.dump(cfg, open(".verify_tmp/mig/dp2_same.json", "w"), indent=2)
PY

python scripts/run_distributed.py --world-size 2 \
  --config .verify_tmp/mig/dp2_same.json --steps 4 --checkpoint-dir "" \
  --jsonl .verify_tmp/mig/dp2_same.jsonl
```

这里的 `global_batch_size=4, micro_batch_size=2, dp=2`，所以 `grad_accum=1`；
单进程的 `grad_accum=2`。两者都覆盖同样的 4 个样本，只是 micro-batch 的
切法不同。如果把它改成 `global_batch_size=8`，就必须同时接受“每步看到的
token 数翻倍”，loss 不再与基线可比。

### 51.10.3 TP：用等价性测试而不是“看 loss”

```bash
python tests/parallel_equiv.py --case tp --world-size 2 --out .verify_tmp/mig/tp.json
cat .verify_tmp/mig/tp.json
```

期望看到 `loss_diff` 为 0.0（或 <1e-6）且 `grad_max_diff` 在 1e-7 量级。
如果 loss 对但梯度差 1e-2，优先查 Column 的 `grad_input` all-reduce 与
Row 的输入切分。

### 51.10.4 PP：先对齐权重，再比较调度

```bash
python tests/parallel_equiv.py --case pp --world-size 2 --out .verify_tmp/mig/pp.json
python scripts/run_distributed.py --world-size 2 --config configs/tiny_pp2.json \
  --steps 4 --checkpoint-dir ""
python scripts/run_distributed.py --world-size 2 --config configs/tiny_pp2_gpipe.json \
  --steps 4 --checkpoint-dir ""
```

### 51.10.5 组合：最后才跑 4 进程

```bash
python scripts/run_distributed.py --world-size 4 --config configs/tiny_tp2_pp2.json \
  --steps 2 --checkpoint-dir ""
python scripts/run_distributed.py --world-size 4 --config configs/tiny_tp2_dp2.json \
  --steps 2 --checkpoint-dir ""
```

如果组合阶段失败，按“回退一格”的原则处理：先回到 TP=2/PP=1，确认 TP 单独
没问题；再回到 PP=2/TP=1。不要在同一次调试里同时改两个维度。

### 51.10.6 一个自动对照脚本

把日志读进来比较，比肉眼可靠：

```python
import json
from pathlib import Path

def last(path: str) -> dict:
    rows = [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()]
    return rows[-1]

a, b = last(".verify_tmp/mig/base.jsonl"), last(".verify_tmp/mig/dp2_same.jsonl")
print("loss diff", abs(a["loss"] - b["loss"]))
print("grad diff", abs(a["grad_norm"] - b["grad_norm"]))
assert abs(a["loss"] - b["loss"]) < 1e-5
```

这段脚本只适合“同一全局 batch、同一步数”的对照；一旦改了 batch 或精度，
它就不再是等价性测试。

## 51.11 迁移中最常见的 12 个 bug

| # | 症状 | 根因 | 对应实验 |
|---|---|---|---|
| 1 | TP 前向对、反向梯度差很多 | Column 的 `grad_input` 少了 all-reduce | N.3 |
| 2 | SP 下 shape 对不上 | 用完整序列长度 reshape 分片激活 | N.4 |
| 3 | CP loss 随 rank 数变化 | 只在本地块上归一化 | N.5 |
| 4 | PP 挂起/串消息 | 用 local rank 当 global rank | N.6 |
| 5 | EP 输出乱序 | combine 的截断/还原方向写反 | N.7 |
| 6 | MoE 专家更新过快 | 专家梯度没有除以 `ep` | N.8 |
| 7 | flat optimizer 参数错位 | scatter offset 没有累计 padding | N.9 |
| 8 | 裁剪后训练不稳定 | 全局范数只用了本地 shard | N.10 |
| 9 | VPP 显存不降反升 | 本地 chunk 交接没有 detach | N.11 |
| 10 | MoE 某些 rank 挂起 | 未激活专家导致集合通信数量不齐 | N.12 |
| 11 | 恢复后 loss 跳变 | 检查点布局校验被绕过 | N.13 |
| 12 | 生成结果与训练不一致 | RoPE cache 设备/位置/布局错 | N.15 |

这些实验的价值在于：**先故意弄坏，再观察哪个断言变红**。当你能预测
“删掉这一行会让哪个测试失败”时，才算真正理解了这条不变量。

## 51.12 从 mini 映射到 Megatron Core

mini 是“透明模型”，不是“可替换实现”。迁移到生产时，按下面的映射查代码：

| 概念 | mini | Megatron Core / 生产 |
|---|---|---|
| 配置 | `ExperimentConfig` | argparse + YAML/recipe（Bridge/NeMo Run） |
| 进程组 | `parallel/state.py` | `initialize_model_parallel` / `parallel_state` |
| TP 线性层 | `parallel/layers.py` | `tensor_parallel.layers` / `linear` |
| 注意力 | `parallel/attention.py` | `dot_product_attention` + TE/FlashAttention |
| Transformer block | `parallel/transformer.py` | `TransformerBlock` + `ModuleSpec` |
| 流水线调度 | `pipeline/schedules.py` | `pipeline` 的 1F1B/interleaved/zero-bubble |
| 词表并行 CE | `parallel/cross_entropy.py` | `tensor_parallel.cross_entropy` |
| MoE | `parallel/moe.py`、`expert_parallel.py` | `transformer.moe` + dispatcher/grouped GEMM |
| DP/ZeRO | `optim/adamw.py` | DDP + `distributed_optimizer` |
| flat 优化器 | `optim/distributed_optimizer.py` | `optimizer.distributed_optimizer` |
| 检查点 | `checkpoint.py` | `distributed_checkpoint` / DCP |
| 生成 | `generation*.py` | `inference/` + 推理引擎 |

迁移到生产时新增的复杂度主要在四处：

1. **kernel 与精度**：TE 的 FP8/FP4、fused attention、CUDA Graph；
2. **通信重叠**：DDP bucket、distributed optimizer 的 reduce-scatter 重叠、
   PP 的 p2p 重叠；
3. **检查点**：全局 metadata、任意 TP/PP 重分片、异步保存；
4. **启动与容错**：多机 rendezvous、弹性、故障检测。

这四件事都不改变本章的验收方法：**先证明语义，再证明规模，最后证明性能**。

## 51.13 本章小结

- 迁移的正确单位是“一个并行维度 + 一次数值验收”，不是“一次性打开所有开关”。
- 基线要包含权重、数据顺序、逐步 loss、裁剪前 grad norm 四样东西。
- DP 先改数据/loss/梯度/日志四件事；ZeRO 再改状态所有权与裁剪口径。
- TP 的切分规则与初始化 seed 是两件事；SP 是布局优化，不是独立维度。
- PP 先对齐权重，再比较调度；GPipe 与 1F1B 只比较最终 step。
- CP/EP 最后引入；它们的失败大多来自 mask、归一化和 all-to-all 索引。
- 每一阶段都要能回退；附录 N 的 16 个故障注入实验是最好的“反向验收”。

下一章把全书收束成回顾、实践路径与 12 周计划。

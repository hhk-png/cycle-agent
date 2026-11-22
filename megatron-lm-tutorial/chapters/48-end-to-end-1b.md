# 第 48 章 端到端实战：从零训练一个 1B 模型

> **第 VIII 部分导读（48–51）**
> 本部分把前面所有内容串成可交付链路：1B 端到端、kernel/TE/编译栈、
> 分布式优化器与 FSDP。前置是 15–33；读完应能从数据到部署跑通一条链路、
> 解释 MFU 差距来自哪里，并在 distributed optimizer/FSDP/HSDP 之间选型。


前面的章节把原理、实现、生产栈和工程专题拆开讲了。这一章把它们串成一条
**可执行的链路**：给定 8 张 H100 和一个月的时间，怎样从原始文本走到一个
能评测、能转换、能部署的 1B 模型；每一步的输入、输出、验收标准和失败模式
是什么。

这不是一份“照抄就能复现 GPT”的配方。数据、超参、评测集都会随项目变化；
本章的价值在于**把决策顺序和账本固定下来**，让你知道每个数字从哪里来、
哪一步可能错、错了先看什么。

## 48.1 目标、约束与验收

### 48.1.1 一个具体的模型

为了避免“1B”这种模糊说法，先固定一个 Llama 风格 decoder-only 配置：

| 项 | 值 | 说明 |
|---|---:|---|
| 层数 `L` | 24 | pre-norm，RMSNorm |
| hidden `h` | 2048 | |
| attention heads | 16 | head_dim 128 |
| KV heads | 8 | GQA，group size 2 |
| FFN | 5504 | SwiGLU，约为 `2.7h` |
| 词表 `V` | 64K | 与 tokenizer 绑定 |
| 位置编码 | RoPE | base 10000（后续可外推） |
| 序列长度 | 4096 | 训练课程从 2048 起 |
| 精度 | bf16 计算 + fp32 master | 先不上 FP8 |

参数量（不含 embedding 的近似）：

```text
attention: q(h²) + k/v(2·h·kv_dim) + out(h²)
         = 2048² + 2·2048·1024 + 2048² ≈ 12.58M
MLP:      3·h·ffn = 3·2048·5504          ≈ 33.82M
norms:    4h                              ≈ 0.008M
per layer                                 ≈ 46.41M
24 layers                                 ≈ 1.114B
embedding (tied): 64K × 2048              ≈ 131.1M
total                                     ≈ 1.245B
```

所以“1B 模型”实际约 **1.25B 参数**；说 1B 时要知道自己省掉了 embedding 还是
把词表算进去了。这个差别在词表 128K/256K 时会更大。

### 48.1.2 token 预算

按 Chinchilla 的约 20 tokens/param：

```text
D ≈ 20 × 1.245e9 ≈ 24.9B tokens
```

取 **24B tokens**（约 19.3 tokens/param）。如果数据质量高、训练更久，
可以到 50B–100B（过训练换推理质量）；如果只做流程验证，2B tokens 就够。

存储：uint16 token（词表 64K 可放进 uint16）：

```text
24e9 × 2 B = 48 GB
```

分成 96 个 512MB shard，每个 shard 约 256M token。索引记录每个 shard 的
token 数、文档边界、来源、去重 id。

### 48.1.3 硬件与时间

8×H100 80GB SXM，单节点 NVLink：

```text
训练 FLOPs ≈ 6 · N · D
            = 6 × 1.245e9 × 24e9
            ≈ 1.79e20

有效算力 = 8 × 989 TFLOP/s × MFU
         = 8 × 989e12 × 0.40
         ≈ 3.17e15 FLOP/s

时间 ≈ 1.79e20 / 3.17e15 ≈ 5.65e4 s ≈ 15.7 小时
GPU-hours ≈ 8 × 15.7 ≈ 126
```

这是**纯计算**下界；加上数据加载、检查点、评测、故障和调参，实际
20–30 小时、160–240 GPU-hours 更现实。按 $2.5/GPU-hour，单次约
$400–600；按云价 $4，约 $640–960。

### 48.1.4 验收标准（Definition of Done）

```text
[ ] 从原始 shard 能复现出同样的 token 流（hash 一致）
[ ] 单卡/多卡前 10 步 loss 与参考实现误差 < 1e-3
[ ] 24B token 训练完成，无不可恢复的中断
[ ] 验证集 loss 与同规模公开 recipe 同量级
[ ] 至少 3 个下游评测（如 ARC-easy、HellaSwag、PIQA）高于随机基线
[ ] 能从最新检查点恢复并继续训练 100 步，loss 连续
[ ] 能导出 HF 格式并在 vLLM/Transformers 中加载
[ ] 记录完整配置、代码 commit、数据版本、硬件和成本
```

最后一条经常被忽略：没有这些元数据，后面的复现/归因/审计都做不了。

## 48.2 数据：从原始文本到可训练 token

### 48.2.1 流水线

```text
raw (web/code/math/books)
  → 抽取/清洗（HTML、样板、乱码）
  → 去重（精确 + 近似 MinHash）
  → 质量过滤（语言、长度、困惑度、分类器）
  → 安全/合规过滤
  → tokenizer（训练或复用）
  → uint16 shard + 文档边界索引
  → 混合采样（按 token 权重）
  → packing（可选，见 38.6）
  → 训练 batch
```

每一步都要**版本化**：数据 manifest 记录输入路径、过滤规则、tokenizer
hash、输出 shard 的 checksum。否则“同一份数据”只是口号。

### 48.2.2 一个最小可运行的 shard 格式

```text
data/
  shard_00000.bin        # uint16 token，小端
  shard_00000.idx        # 文档 offset/length/source_id
  manifest.json          # tokenizer hash, token count, checksums
```

读取时用 `numpy.memmap(..., dtype=np.uint16)`，按 `(shard, offset, length)`
切片；不要一次读进内存。`mini_megatron/data/dataset.py` 的
`BinTokenDataset` 是同一思路的教学版。

### 48.2.3 数据混合与课程

一个可解释的起点：

| 来源 | 权重 | 说明 |
|---|---:|---|
| 网页（清洗后） | 55% | 主体，注意重复/低质 |
| 代码 | 20% | 提升推理/结构化能力 |
| 数学/科学 | 10% | 高质量，通常上采样 |
| 书籍/长文 | 10% | 长上下文 |
| 多语言 | 5% | 按目标市场调整 |

按 **token** 而不是文档数配比。训练中期可做退火：最后 10% token 提高高质量
来源、降低网页权重；这就是“数据课程”，不是玄学，但必须用 ablation 验证。

### 48.2.4 验收

- 随机采样 1000 条，人工/规则检查乱码、重复、隐私；
- 统计 token 长度分布、语言分布、来源分布；
- 训练前 100 步，检查每个 DP rank 的 batch 不重复（用 hash）；
- 断点续训后，第一个 batch 与中断前最后一个 batch 不重复。

## 48.3 并行配置与显存账本

### 48.3.1 选择 TP/PP/DP

8 卡单节点，模型 1.25B，bf16 + AdamW：

```text
总状态 ≈ 16 B/param × 1.245e9 ≈ 19.9 GB
```

如果 TP=4、PP=1、DP=2：

```text
每卡参数+梯度+优化器（未 ZeRO）≈ 19.9 / 4 ≈ 5.0 GB
```

激活（4096 序列、micro-batch=1、24 层）按第 4 章的公式：

```text
每层 ≈ s·b·h·(34 + 5·n_h·s/h) 字节
     = 4096·1·2048·(34 + 5·16·4096/2048)
     ≈ 8.39M × 194 ≈ 1.63 GB
24 层 ≈ 39 GB（全模型、未切分）
```

TP=4 + SP 把大部分激活切到 1/4，约 10 GB/卡；加上状态 5 GB、临时 buffer
和碎片，约 20–25 GB/卡，80GB 卡有充足余量。注意这是**不开重算**的上界；
48.3.4 的 `reference_1b.json` 开了 activation checkpointing，所以估算器的
激活只有约 1.46 GB/卡，两者不是同一口径。因此可以：

- TP=4、PP=1、DP=2、SP=on；
- micro-batch=1、序列 4096、grad accumulation 按 global batch 调；
- 先按不重算的上界评估显存；实际配置开重算以留出余量，如果仍 OOM 再加
  细粒度重算/降序列。

**为什么不 TP=8**：TP=8 的 all-reduce 更频繁、每卡 GEMM 更小，MFU 通常下降；
8 卡内 TP=4 已能把单层放到可接受大小。**为什么不上 PP=2**：PP=2 会引入
气泡和 stage 不均，1.25B 模型没必要。**DP=2** 提供数据并行和容错冗余。

### 48.3.2 global batch 与学习率

一个起点：

```text
global_batch = 1,048,576 tokens（约 1M）
seq_len = 4096
micro_batch = 1
DP = 2
grad_accum = 1,048,576 / (1 × 4096 × 2) = 128
```

1M token 的 global batch 对 1B 模型偏大但可用；如果 loss 早期不稳定，降到
0.5M 并重新调 LR。学习率起点 `3e-4`（AdamW，β2=0.95），warmup 1%–2%，
cosine 到 `3e-5`。这些是起点，不是定律；第 13 章给出了 warmup、critical
batch size 和 μP 的判断方法。

### 48.3.3 用 mini 验证同一套账本

mini 的 `estimate` 可以在开 GPU 前做一阶筛选：

```bash
python -m mini_megatron.cli estimate --config configs/tiny_tp2.json \
  --tokens 24e9 --gpu h100 --mfu 0.4 --world-size 8
```

它不会给出 1.25B 模型的精确激活（tiny 配置的 h/层数不同），但会暴露：

- 状态显存是否随 TP/PP 正确缩放；
- 通信量是否出现数量级异常；
- GPU-hours 是否与手算一致。

把 1.25B 的 `h=2048, L=24, ffn=5504, heads=16, kv_heads=8, vocab=64000`
写进一个临时 JSON，再跑 `estimate`，就能得到与本节同口径的账本。

### 48.3.4 一个可复现的 1B 估算

仓库里已经放了 `configs/reference_1b.json`。它只用于 `estimate`，不是 CPU
训练配置（24 层、4096 序列在 CPU 上不现实）：

```bash
python -m mini_megatron.cli estimate \
  --config configs/reference_1b.json \
  --tokens 24e9 --gpu h100 --mfu 0.4 --world-size 8
```

输出（2026-11 基线，数值随版本/口径略有变化）：

```text
parameters: 1.38B total, 1.38B active
parallel: world=8 TP=4 PP=1 CP=1 DP=2
state / rank: 5.13 GB
activation / rank: 1.46 GB per micro-batch
activation peak / rank: 1.46 GB (1 in flight)
attention scores (one layer): 128.00 MB
KV cache: 96.00 MB
estimated peak / rank: 6.58 GB (device 80.00 GB)
train 2.4e+10 tokens @ MFU=40%: 152.7 GPU-hours
  (0.02 GPU-years; 19.1 h wall-clock on 8 GPUs)
communication / step: DP 2.56 GB, TP 384.00 MB, PP 0.00 B, CP 0.00 B
```

和 48.1 的手算差在哪里：

- **1.38B vs 1.245B**：mini 不支持 tied embedding + sequence parallel，所以
  `reference_1b.json` 用了 untied output（多 `64K×2048≈131M`）。把 embedding
  绑定后总参数回到 1.245B；做容量规划时要说清是否含 output。
- **152.7 vs 126 GPU-hours**：估算器把 attention 的 `4Lhs` 项也算进去，
  手算只用了 `6ND`。长序列下这一项不可忽略；两边差约 20%，属于定义差异，
  不是谁“算错了”。
- **5.13 GB vs 5.0 GB**：手算按 1.245B/4 粗除；估算器按 untied 1.38B、并保留
  fp32 master/m/v 的精确字节。先统一“参数量口径”，再比较显存。
- **1.46 GB/卡 vs 手算 ~10 GB/卡**：`reference_1b.json` 开了
  `activation_checkpointing=true`，估算器算的是“只保存重算边界”的量；48.1
  的手算是不开重算的上界。若把重算关掉，激活会回到约 10 GB/卡。两个数字
  都对，但口径不同；排期/显存规划时必须注明是否重算。
- **19.1 h wall-clock**：这是 8 卡的墙钟时间；GPU-hours 是 152.7。把两者混为
  一谈，会在采购/排期时差 8 倍。

这个例子也说明估算器的正确用法：**先对齐定义（参数量、精度、MFU、是否含
attention），再比较数字**。如果工具和手算差一个数量级，先查单位，不要先
怀疑公式。

## 48.4 启动：从单机到调度器

### 48.4.1 单节点 torchrun

```bash
export OMP_NUM_THREADS=8
torchrun --standalone --nproc_per_node=8 \
  pretrain_gpt.py \
  --tensor-model-parallel-size 4 \
  --pipeline-model-parallel-size 1 \
  --sequence-parallel \
  --use-distributed-optimizer \
  --num-layers 24 --hidden-size 2048 --ffn-hidden-size 5504 \
  --num-attention-heads 16 --num-query-groups 8 \
  --seq-length 4096 --max-position-embeddings 4096 \
  --micro-batch-size 1 --global-batch-size 256 \
  --train-iters 22888 --lr 3e-4 --min-lr 3e-5 \
  --lr-decay-style cosine --lr-warmup-fraction 0.01 \
  --bf16 --use-flash-attn \
  --data-path data/sharded --tokenizer-type ... \
  --save checkpoints/1b --save-interval 500 \
  --log-interval 10 --eval-interval 500
```

注意 `--global-batch-size` 的单位是**样本数**（不是 token）；按 seq_len=4096，
1M token 对应 256 个样本。24B token 的预算是 `24e9 / (256 × 4096) ≈ 22,888`
步；写 `--train-iters 200000` 会训练约 210B token，是本节预算的 8.75 倍。
`grad_accum` 由 Megatron 根据 DP 自动推导：

```text
grad_accum = global_batch / (micro_batch × DP)
           = 256 / (1 × 2) = 128
```

参数名会随版本漂移（例如 `--num-query-groups`、`--use-distributed-optimizer`
的具体拼写）；用 `--help` 和当前源码核对，不要凭记忆。

### 48.4.2 Slurm / Kubernetes

Slurm 的关键不是脚本，而是**每节点一个 torchrun、共享 rendezvous**：

```bash
srun --nodes=1 --ntasks=1 --gpus-per-task=8 \
  torchrun --nnodes=1 --nproc_per_node=8 ...
```

多节点时：

```bash
# 每个节点执行；node_rank 由调度器注入
torchrun --nnodes=$NNODES --node_rank=$NODE_RANK \
  --master_addr=$MASTER_ADDR --master_port=$MASTER_PORT \
  --nproc_per_node=8 ...
```

Kubernetes 上用 JobSet/Volcano/gang scheduling，确保所有 Pod 同时拿到 GPU；
否则会卡在 rendezvous。存储要支持高吞吐读（数据）和高吞吐写（检查点），
不要把检查点写到网络文件系统的单挂载点瓶颈上。

### 48.4.3 启动前 10 项检查

```text
[ ] 所有节点 GPU 型号/驱动/CUDA/NCCL 版本一致
[ ] IB/RoCE 链路 up，带宽/误码正常
[ ] 共享存储可写，空间 > 3× 检查点大小
[ ] MASTER_ADDR/PORT 可达，防火墙放行
[ ] 数据 manifest/checksum 一致
[ ] tokenizer hash 与训练配置一致
[ ] 全局 batch 能被 micro_batch × DP 整除
[ ] TP/PP/CP/EP 与 world size 整除
[ ] 启动脚本打印 rank 映射与并行组
[ ] 有一个已知能跑通的最小配置作为回退
```

## 48.5 训练中：看什么、什么时候停

### 48.5.1 四条曲线

1. **loss**：训练/验证 loss；看趋势和 spike，不只看单点。
2. **grad norm**：突然升高通常是数据/数值/通信问题；持续为 0 是梯度没同步。
3. **tokens/s 与 MFU**：与基线比；掉 10% 以上要查 straggler/网络/数据。
4. **data stall**：GPU 空转时间；数据管线往往是规模化的第一瓶颈。

### 48.5.2 一个可用的告警规则

| 指标 | 阈值（示例） | 先查 |
|---|---|---|
| loss NaN/Inf | 任意 | 精度、LR、坏数据、FP8 scale |
| loss spike | 单步 > 3× 滑动均值 | 数据批次、grad norm、优化器状态 |
| grad norm | > 10× 基线或 = 0 | 裁剪、通信、专家路由 |
| tokens/s | 连续 5 分钟 < 基线 80% | straggler、NCCL、数据 |
| 检查点耗时 | > 2× 基线 | 存储、异步队列、带宽 |
| 重启次数 | > 3 次/天 | 硬件、调度、OOM |

阈值要按自己的基线设，不要抄。附录 H 给出了更完整的 runbook。

### 48.5.3 什么时候停

- loss 连续多步 NaN/Inf，且回退检查点仍复现：停，查数据/精度；
- 验证 loss 连续多个 eval 不降，训练 loss 降：过拟合/数据重复/评测错；
- tokens/s 掉到基线 50% 且 30 分钟不恢复：停，查硬件/网络；
- 检查点连续失败：停，先修存储，否则故障时无点可回。

“再跑跑看”是规模化训练最贵的四个字。

## 48.6 检查点、恢复与成本

### 48.6.1 检查点大小与频率

```text
每卡模型 shard ≈ 1.245e9 × 2 B / TP = 0.62 GB
每卡优化器（flat, fp32 master+m+v）/TP/DP ≈ 1.245e9 × 12 / 8 ≈ 1.87 GB
每卡总计 ≈ 2.5 GB；8 卡 ≈ 20 GB/检查点
```

若每 30 分钟保存一次，15.7 小时约 31 个检查点，保留最近 3 个约 60 GB。
异步保存把阻塞时间从“写 20 GB 的时间”降到“D2H copy 的时间”；按 2 GB/s
有效写带宽，20 GB 约 10 秒，D2H 可能 2–4 秒。

### 48.6.2 恢复语义

```text
[ ] 恢复到最新完整检查点，不是半写目录
[ ] 模型/优化器/RNG/sampler 位置全部恢复
[ ] 恢复后 100 步 loss 与中断前连续
[ ] DP/TP/PP 布局未变（或走重分片）
[ ] 数据 manifest/tokenizer 版本一致
```

第 24 章给出了保存顺序、原子 rename 和 DCP 的细节；第 50 章会讲
distributed optimizer 的 shard 检查点。

### 48.6.3 成本复盘

```text
GPU-hours = 8 × 实际墙钟小时
成本 = GPU-hours × $/GPU-hour + 存储 + 人力 + 失败重算
```

如果实际 24 小时、$2.5/h：`8×24×2.5 = $480`；失败重算 20% 则 $576。
把这笔账与“用更大模型/更多 token 的收益”对比，才是完整的决策。

## 48.7 评测、转换与部署

### 48.7.1 训练中的评测

- 验证集 loss：固定 held-out 数据，按 token 加权；
- 下游 few-shot：ARC-easy、HellaSwag、PIQA 等，固定 prompt/版本；
- 长上下文：needle/检索，至少在 4K/8K 上测；
- 生成质量：固定 prompt 集，人工/LLM-judge 抽样。

评测集要与训练数据去重（第 41 章）；否则分数虚高。

### 48.7.2 转换到 HF

用 Megatron Bridge 或附录 K 的映射：

```text
Megatron checkpoint
  → 合并 TP/PP 分片
  → 按 HF 参数名映射（q/k/v 顺序、GQA 不 repeat、RoPE 布局）
  → 保存 config.json + safetensors
  → 用固定输入对比 logits（atol/rtol）
  → greedy 生成前 N 个 token 对比
```

只比 loss 不够；logits 和生成都要对齐。转换后用 vLLM/Transformers 各跑
一次，确认没有“能加载但输出乱码”。

### 48.7.3 部署侧的最小检查

- 权重 dtype/量化方式与推理引擎匹配；
- tokenizer 的 special tokens/chat template 一致；
- KV cache 的 head 数/维度与 GQA 配置一致；
- 长上下文的位置编码外推方式一致；
- 首 token 延迟（TTFT）和每 token 延迟（TPOT）在目标硬件上实测。

## 48.8 用 mini 走一遍同一条链路

mini 不能训 1.25B，但可以把链路上的**每个契约**跑通：

```bash
# 1. 单进程：配置 → 训练 → 检查点 → 恢复 → 生成
python -m mini_megatron.cli train --config configs/tiny.json \
  --steps 4 --checkpoint-dir .verify_tmp/e2e
python -m mini_megatron.cli train --config configs/tiny.json \
  --steps 6 --checkpoint-dir .verify_tmp/e2e --auto-resume
python -m mini_megatron.cli generate --config configs/tiny.json \
  --checkpoint .verify_tmp/e2e --prompt 1,2,3,4 --max-new-tokens 8

# 2. 并行组合：TP / PP / DP / CP / EP / flat optimizer
python scripts/run_distributed.py --world-size 2 --config configs/tiny_tp2.json
python scripts/run_distributed.py --world-size 2 --config configs/tiny_pp2.json
python scripts/run_distributed.py --world-size 2 --config configs/tiny_dp2_flat.json
python scripts/run_distributed.py --world-size 4 --config configs/tiny_tp2_pp2.json

# 3. 等价性与恢复
python tests/parallel_equiv.py --case tp --world-size 2 --out tp.json
python tests/distributed_optimizer_equiv.py --out dopt.json
python scripts/verify.py
```

把 tiny 的 `h=64, L=2` 换成 1.25B 的配置，`estimate` 会给出同口径的账本；
但**不要**指望 CPU mini 训练 1.25B。mini 的作用是证明“数据 → 模型 → 优化器
→ 检查点 → 生成”的语义正确，生产规模交给 Megatron Core。

## 48.9 一页纸的验收清单

```text
数据
[ ] manifest/checksum/tokenizer hash 固定
[ ] 去重/质量/合规过滤有记录
[ ] 采样可复现，DP rank 不重复

模型
[ ] 参数量与配置一致（手算 = 代码统计）
[ ] 单卡前 10 步 loss 与参考一致
[ ] 并行等价性测试通过（TP/PP/CP/DP/EP）

训练
[ ] global batch 整除关系正确
[ ] MFU/tokens/s 与估算同量级
[ ] loss/grad norm/数据吞吐有告警
[ ] 检查点可恢复，RNG/sampler 一致

交付
[ ] 验证 loss + 至少 3 个下游评测
[ ] HF 转换 logits/生成对齐
[ ] 推理引擎加载与延迟实测
[ ] 配置/代码/数据/成本归档
```

## 48.10 常见失败与定位顺序

| 现象 | 先查 | 再查 |
|---|---|---|
| 启动即卡住 | rank 映射/rendezvous/防火墙 | p2p 全局 rank、NCCL 拓扑 |
| loss 不降 | 数据/label 偏移、LR、tokenizer | 梯度是否同步、优化器是否更新 |
| loss 降但评测差 | 数据重复/污染、评测协议 | 过拟合、验证集泄漏 |
| tokens/s 低 | 数据 stall、MFU、TP 过大 | straggler、NCCL、重算 |
| OOM | 激活、micro-batch、序列 | 碎片、检查点/临时 buffer |
| 恢复后 loss 跳 | sampler/RNG/数据版本 | 优化器状态、并行布局 |
| 转换后输出乱 | q/k/v 顺序、RoPE、GQA | tokenizer、config、dtype |

## 48.11 本章小结

- 1B 不是魔法数字：先把参数量、token 预算、显存、FLOPs、时间算清楚。
- 8×H100 上 1.25B/24B token 的纯计算约 16 小时、126 GPU-hours；实际要
  留 1.5–2× 余量。
- 并行配置从硬约束出发：TP 留 NVLink，PP 只在需要时上，DP 填剩余。
- 数据、检查点、评测、转换是端到端链路的一部分，不是“训练完再说”。
- mini 验证语义，生产 Megatron 承担规模；两者的账本和验收标准是同一套。

下一章进入 kernel、Transformer Engine 与编译栈：解释为什么同样的并行
配置，MFU 可能差一倍，以及哪些优化值得做、哪些只是噪声。

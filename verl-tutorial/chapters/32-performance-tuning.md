# 第 32 章 性能工程：吞吐、MFU、显存与调参方法论

## 32.1 先定义“快”

RL 的“快”至少有四种口径：

1. **step/s**：每秒多少个训练 step；
2. **tokens/s**：每秒处理/生成多少 token；
3. **samples/s**：每秒多少条轨迹；
4. **time-to-quality**：达到目标验证精度需要多久。

只看 step/s 会误导：增大 batch 会让 step/s 下降，但 tokens/s 上升；异步会提高吞吐，但可能降低样本效率。

## 32.2 时间分解

一步的时间：

$$
T_{\text{step}}=T_{\text{gen}}+T_{\text{reward}}+T_{\text{old/ref/values}}+T_{\text{adv}}+T_{\text{update}}+T_{\text{sync}}.
$$

典型占比（7B、8 卡、512/512）：

- `gen` 30%～50%；
- `update` 20%～40%；
- `ref/values/old` 10%～20%；
- `sync` 1%～10%；
- reward 取决于实现，可能很高。

先 profile，再优化占比最大的项。

## 32.3 MFU

$$
\text{MFU}=\frac{\text{actual FLOPs/s}}{\text{peak FLOPs/s}}.
$$

训练每 token 约 $6N$ FLOPs（前向 2N + 反向 4N），推理约 $2N$。

例：7B 模型、H100 bf16 peak 约 990 TFLOPS。要区分两个口径：

**训练-only**：假设更新阶段每卡 10,000 tokens/s，训练每 token 约 $6N$：

```text
FLOPs/s = 10000 * 6 * 7e9 = 4.2e14 = 420 TFLOPS
MFU = 420 / 990 ≈ 42%
```

**端到端 RL**：假设整步（含生成、reward、同步）折算到每卡只有 1000 tokens/s。生成每 token 约 $2N$，但这里用 6N 做上界估算会高估 FLOPs；即便按 6N：

```text
FLOPs/s = 1000 * 6 * 7e9 = 4.2e13 = 42 TFLOPS
MFU ≈ 4.2%
```

两个数字不矛盾：42% 是“更新 kernel 在跑 matmul 时”的利用率，4.2% 是“把等待/生成/通信都摊进去”的端到端利用率。如果 update 只占墙钟 30%，训练-only 的 42% 折算到端到端约 12.6%，仍高于 4.2%，差额来自生成/同步的等待。

所以 MFU 要结合场景看：

- 纯训练 step 的 MFU 可能 30%～50%；
- 端到端 RL 的 MFU 通常低得多；
- rollout 的 MFU 口径不同（2N，且受 KV cache/attention 影响）。

## 32.4 显存

显存 = 参数 + 梯度 + 优化器 + 激活 + KV cache + 临时 buffer。

优化杠杆：

| 杠杆 | 降显存 | 代价 |
|---|---|---|
| FSDP/ZeRO | 参数/梯度/优化器 | 通信 |
| 重计算 | 激活 | 时间 |
| offload | 参数/优化器 | PCIe 带宽 |
| 动态 batch | 激活峰值 | 实现复杂 |
| remove padding | 激活 | 内核支持 |
| LoRA/QLoRA | 参数/优化器 | 精度/速度 |
| TP/PP/CP | 每卡参数/激活 | 通信/气泡 |
| 降序列长度 | 激活/KV | 任务效果 |

先确认是“训练 OOM”还是“rollout OOM”：

- 训练 OOM：降 micro batch、开重计算、offload；
- rollout OOM：降 `gpu_memory_utilization`、并发、长度；
- colocate OOM：协调 sleep/wake 与显存上限。

## 32.5 通信

FSDP：

- all-gather（前向）；
- reduce-scatter（反向）；
- 通信量与参数量成正比。

Megatron：

- TP：每层 all-reduce；
- PP：stage 间点对点；
- EP：all-to-all；
- CP：attention 内通信；
- SP：all-gather/all-to-all。

优化：

- 增大 bucket；
- 通信/计算重叠；
- 用 NVLink/NVSwitch；
- 减少 TP（如果单卡放得下）；
- 用 EP 替代 TP（MoE）；
- 用 hierarchical all-reduce。

## 32.6 rollout 优化

- 提高 `max_num_seqs`/`max_num_batched_tokens`；
- 开 chunked prefill；
- 开 prefix caching；
- 调 TP；
- 用 CUDA graph；
- 减少 stop token 检查开销；
- 异步/partial rollout；
- 长度均衡。

## 32.7 训练优化

- FSDP2 + `reshard_after_forward`；
- `use_remove_padding`；
- 动态 batch；
- gradient checkpointing；
- `torch.compile`；
- 融合 kernel（Liger/fused kernels）；
- 低精度（bf16/FP8）；
- 减少 `ppo_epochs`；
- 调小 ref 前向频率（近似）。

## 32.8 调参顺序

1. **正确性**：小模型、单卡、sync，确认 reward/loss/指标正常；
2. **显存**：让训练与 rollout 都能放下；
3. **吞吐**：profile，优化最大项；
4. **稳定性**：lr/clip/KL/batch；
5. **效果**：数据、算法、reward；
6. **规模**：多机、异步、容错。

不要一开始就调大规模；错误会被放大且难定位。

## 32.9 基准测试的陷阱

- 不同硬件/驱动/CUDA 不可比；
- 不同序列长度/模型不可比；
- 是否包含 rollout/同步/验证；
- batch 大小与 warmup；
- 是否开 CUDA graph/compile；
- 是否统计首步（编译/加载）时间。

报告基准时至少写清：

```text
model, GPU, interconnect, sequence length, batch, n,
backend, precision, TP/PP/DP, rollout engine,
whether async, whether validation included
```

## 32.10 一张调参表

| 症状 | 优先调 |
|---|---|
| 训练 OOM | micro batch ↓、重计算、offload |
| rollout OOM | gpu_memory_utilization ↓、并发 ↓ |
| gen 太慢 | vLLM/SGLang、prefix cache、chunked prefill |
| update 太慢 | FSDP2、remove padding、compile |
| sync 太慢 | delta/nccl、减少副本 |
| reward 太慢 | reward loop、并发、缓存 |
| 训练不稳 | lr ↓、clip、KL、batch ↑ |
| 吞吐低但 GPU 空 | 数据加载/长尾/同步 |

## 32.11 MiniVerl 的性能视角

MiniVerl 不追求性能，但提供了可观察的量：

- `perf/tokens_per_s`、`perf/mfu`；
- `timing/gen/update/...`；
- `estimate_memory`；
- `CheckpointEngineManager.bytes_sent`；
- `KV cache` 计算。

`examples/14_benchmark.py` 在 CPU 上跑 20 步并打印吞吐；这些数字不能与 GPU 比较，但可以用来验证优化是否改变了相对趋势。

## 32.12 Roofline 与通信量估算

### Roofline

一个 kernel 的实际吞吐受两个上限约束：

$$
\text{FLOP/s}_{\text{attainable}}
=\min\left(\text{peak FLOP/s},\;
\text{arithmetic intensity}\times\text{memory bandwidth}\right),
$$

其中 arithmetic intensity（AI）是“每搬一个 byte 做多少 FLOP”。这解释了为什么 RL 的不同阶段表现完全不同：

- **训练的大矩阵乘**：AI 高，通常 compute-bound，MFU 可以到 30%～50%；
- **decode 的单 token 生成**：每生成一个 token 要读全部权重和 KV cache，AI 接近 1 FLOP/byte，典型 memory-bound，GPU 算力大量空闲；
- **attention 的长序列**：AI 随序列长度增长，短序列 memory-bound，长序列逐渐 compute-bound；
- **all-reduce/all-gather**：没有 FLOP，只有通信，受 NVLink/IB 带宽与延迟约束。

一个粗略的 decode 估算：7B 模型、bf16、batch=1，每 token 至少读 14 GB 权重；若 HBM 带宽 3 TB/s，理论下限约 4.7 ms/token（约 213 tokens/s）。实际还要加 KV cache 与 kernel 开销。这个数字与“H100 989 TFLOPS”无关——**decode 的瓶颈是带宽，不是算力**。

### 通信量

记参数字节数为 $P$，梯度字节数为 $G$，隐藏维 $h$、序列 $s$、batch $b$、TP 度 $t$、PP 度 $p$、micro-batch 数 $m$。

| 并行 | 每 step 量级 | 备注 |
|---|---|---|
| FSDP/ZeRO-3 | 约 $3P$～$4P$ | 前向 all-gather、反向 all-gather、梯度 reduce-scatter |
| DP 梯度 all-reduce | $2G$ | ring 实现，与卡数弱相关 |
| TP | 每层 $O(bsh)$ 的 all-reduce，前向+反向 | 层数越多越大，跨节点代价高 |
| PP | stage 间 $O(bsh)$ 点对点 | 气泡 $(p-1)/(m+p-1)$ |
| EP | $O(\text{tokens}\cdot h\cdot\text{top-}k)$ all-to-all | MoE 的主要通信 |
| CP/SP | attention 内 all-gather/all-to-all | 长序列必需 |

两个直接结论：

1. TP 尽量放在 NVLink 域内；跨机 TP 的 all-reduce 延迟会吃掉收益；
2. PP 的气泡随 stage 数增加，micro-batch 越多越不明显；但 micro-batch 受显存限制。

### Little's Law 与 rollout 并发

rollout 的平均吞吐可以写成：

$$
\text{throughput}=\frac{\text{concurrency}}{\text{average latency}}.
$$

如果并发是 256、平均延迟 2 秒，理论吞吐是 128 请求/s。但生成长度分布很长时，平均延迟会被长尾拉高；提高并发不一定提高吞吐，可能只是让 KV cache 溢出、触发抢占。要用 p50/p95/p99 延迟一起看。

## 32.13 基准实验设计与统计显著性

一个可复现的基准至少要做到：

1. **固定变量**：模型、数据、序列长度、batch、`rollout.n`、精度、并行配置、是否异步；
2. **预热**：跳过编译、CUDA graph capture、模型加载与首批数据；
3. **分阶段计时**：gen / reward / old-ref-values / update / sync 分开，而不是只报总 step 时间；
4. **重复**：至少 3 次，报告中位数与 p95，而不是单次最好成绩；
5. **报告口径**：tokens/s、samples/s、step/s、time-to-quality 至少给两个；
6. **统计**：验证指标要给出置信区间或多次 seed 的均值±标准差；只报一个点容易把噪声当提升。

比较两个配置时，用 A/B 交替跑，避免“先跑 A 再跑 B”被机器温度/邻居干扰。若只能顺序跑，至少重复并交换顺序。

一个常见的“假提升”：

```text
配置 A：batch=64, lr=1e-6, 8 卡  → reward 0.62
配置 B：batch=128, lr=2e-6, 16 卡 → reward 0.65
```

这里同时改了 batch、lr、卡数，无法归因。正确的做法是一次只改一个变量，或者用因子实验设计（factorial design）估计主效应与交互项。

对 RL 来说，还要报告**样本效率**：达到同样验证精度用了多少生成 token，而不只是墙钟时间。异步系统常常“吞吐更高、样本效率更低”，只报吞吐会误导选型。

## 32.14 Profiling 实操：从 step 计时到 kernel

性能优化最容易犯的错是“凭感觉改配置”。先建立三层观测：

| 层 | 工具 | 回答的问题 | 开销 |
|---|---|---|---|
| Step | `timing/*` 指标 | 哪个阶段占比最大 | 极低 |
| 框架 | torch profiler / global_profiler | 哪个 kernel/通信占时间 | 中 |
| 系统 | nsys / ncu / Ray timeline / DCGM | GPU 空转、NCCL 等待、CPU 瓶颈 | 高 |

### 第一层：step 计时

verl 已经按 `gen/reward/old/ref/values/update/sync` 分阶段计时。先看这个，不要一上来就 nsys。若 `gen` 占 60%，优化 `update_actor` 没有意义。

### 第二层：torch profiler

```python
import torch
from torch.profiler import profile, ProfilerActivity

with profile(
    activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA],
    schedule=torch.profiler.schedule(wait=1, warmup=1, active=3, repeat=1),
    on_trace_ready=torch.profiler.tensorboard_trace_handler("./prof"),
    record_shapes=True,
    with_stack=False,
) as prof:
    for step in range(6):
        train_one_step()
        prof.step()
```

看三件事：

1. **CUDA kernel 之间的空隙**：空隙大说明 CPU 侧 launch/数据加载/通信等待；
2. **NCCL kernel 的占比**：all-reduce/all-gather 是否成为主项；
3. **memory-bound kernel**：decode/attention 的带宽利用率，而不是只看 TFLOPS。

verl 的 `global_profiler` 封装了 torch/nsys/NPU profiler，配置示例：

```yaml
global_profiler:
  tool: torch
  steps: [10, 20]
  save_path: outputs/profile
```

### 第三层：nsys / ncu

- **nsys**：看时间线，适合定位“GPU 为什么空着”。加上 `--trace=cuda,nvtx,osrt`，用 NVTX 标记 step 边界。
- **ncu**：看单个 kernel 的指标（occupancy、带宽、指令），开销大，只对热点 kernel 用。
- **Ray timeline**：看 controller/worker 的 RPC 等待，适合定位“不是 GPU 慢，是调度慢”。
- **py-spy dump**：看 Python 进程卡在哪一行，适合 hang/死锁。

### 常见结论与动作

| 现象 | 可能原因 | 动作 |
|---|---|---|
| GPU 时间线大量空隙 | 数据加载/CPU launch/reward 排队 | 预取、异步、批处理 |
| NCCL 占比高 | TP/DP 通信、跨节点 | 换拓扑、减 TP、bucket/重叠 |
| decode kernel 带宽打满 | memory-bound | 增 batch、量化、PD 分离 |
| attention kernel 慢 | 未用 varlen/flash | 换 attention backend、remove padding |
| 显存高但利用率低 | KV cache/碎片 | 降并发、sleep/wake、memory snapshot |

### 纪律

- 每次只改一个变量，保留 baseline；
- profiler 本身会改变时序，先确认“不开 profiler 也能复现”；
- 记录 profiler 的版本、命令、step 范围；
- 优化后重新测端到端，不要只看单个 kernel。

## 本章小结

- “快”有四种口径，先明确目标。
- 先 profile 时间分解，再优化最大项。
- 显存优化有明确杠杆：分片、重计算、offload、动态 batch、低精度。
- 通信是规模化的主要瓶颈；TP/PP/EP 各有代价。
- Roofline 解释“训练 compute-bound、decode memory-bound”；通信量要按并行维度估算。
- 基准必须固定变量、预热、分阶段计时并报告置信区间；同时看吞吐与样本效率。
- 调参顺序：正确性 → 显存 → 吞吐 → 稳定性 → 效果 → 规模。

## 延伸阅读

- `verl/utils/flops_counter.py`
- `verl/utils/profiler/`
- 附录 Z：从 KV 字节到并发、吞吐与成本的容量规划。
- 下一章：与其他框架对比：TRL、OpenRLHF、NeMo-RL、Unsloth。

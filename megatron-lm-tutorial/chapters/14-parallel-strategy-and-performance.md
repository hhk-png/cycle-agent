# 第 14 章 并行策略选择与性能建模

## 14.1 先问四个问题

给一个集群和模型，不要先问“用几路 TP”，先问：

1. **显存**：最低需要多少卡才能放下状态/激活？（硬约束）
2. **拓扑**：NVLink 域多大？跨节点带宽/延迟多少？
3. **模型形状**：hidden/FFN/heads/层数/序列，哪些维度可整除？
4. **吞吐目标**：是追求最短训练时间，还是最低成本/最高 MFU？

只有硬约束满足后，才谈调优。一个在 8 卡上 MFU 很高但放不下 70B 的方案没有
意义。

## 14.2 显存约束

每卡状态显存要区分“被 TP/PP 切分的参数”和“被 ZeRO 切分的优化器状态”：

```text
M_state(ZeRO-1) ≈ 4N/(tp·pp) + 12N/(tp·pp·d_z)
                  ↑ bf16 权重+梯度（DP 副本）   ↑ fp32 master + Adam m/v
```

- `4N/(tp·pp)`：bf16 权重 2N 与梯度 2N，在 DP 维度上是复制的；
- `12N/(tp·pp·d_z)`：fp32 master（4N）与 Adam `m/v`（8N）被 ZeRO-1/
  distributed optimizer 按 DP 切分，`d_z` 是优化器状态的切分倍数；
- 不用 ZeRO 时 `d_z=1`，回到 `16N/(tp·pp)`；
- ZeRO-2 还把梯度切了：`2N/(tp·pp) + 14N/(tp·pp·d_z)`；
- ZeRO-3/FSDP 连参数也切：约 `16N/(tp·pp·d_z)`。

代入 70B、`tp=pp=1`、`d_z=8`：`4×70 + 12×70/8 = 280 + 105 = 385 GB/卡`，
与 11.7 的例子一致；而完全不切分是 `16×70 = 1.12 TB`。这两个数不要混用。

激活还要加：

```text
M_act(单 micro-batch) ≈ c_act · b · s · h · L / (pp · cp · (tp if SP else 1))
M_act(峰值) ≈ n_inflight · M_act(单 micro-batch)
n_inflight ≈ 1（无 PP）| max(1, min(m, pp-1))（本实现的 1F1B）| m（GPipe）
```

`c_act` 与重算策略、attention 实现相关，需实测校准（不要与 MoE 的 top-k 混淆）。注意三点：

1. **普通 TP 不会切所有激活**。LayerNorm、残差、dropout 等在 TP 下仍是复制
   的；只有序列并行（SP）才把它们按序列切成 `1/tp`。因此分母里的 `tp` 只在
   `sequence_parallel=True` 时成立，否则这个公式是乐观下界。
2. **CP 切的是序列**，不是层；`/cp` 对 attention/MLP 激活成立，但 KV 的
   all-gather 版本仍可能复制完整 KV（见第 9 章）。
3. **PP 的峰值不是单 micro-batch**。1F1B 同时有约 `pp` 个 micro-batch 的
   激活在飞，GPipe 则有 `m` 个；漏掉 `n_inflight` 会把峰值低估约 `pp` 或
   `m` 倍。`estimate` 命令现在同时打印单 micro-batch 与峰值。

先用 `torch.cuda.max_memory_allocated` 实测一个 micro-batch，再外推。公式只能
做初筛，不能替代实测。

## 14.3 拓扑约束

典型 8 卡节点：

```text
GPU 0–7 通过 NVLink/NVSwitch 互联
节点间通过 InfiniBand/RoCE
```

原则：

- **TP 放在 NVLink 域内**：all-reduce 频繁，跨 IB 会拖垮；
- **PP 可以跨节点**：p2p 通信量相对小，但延迟敏感；
- **DP 跨节点**：梯度 all-reduce 可以分层（节点内+节点间）；
- **EP 跨节点**：all-to-all 对网络要求高，尽量把 EP×TP 放在 NVLink 域；
- **CP 跨节点**：取决于 ring/all-gather 的通信模式。

一个常见的 3D 配置：

```text
TP=8（节点内）
PP=4（跨 4 个节点）
DP=剩余卡数 / 32
```

具体数字要用集群拓扑和模型形状验证。

## 14.4 Roofline 与通信占比

对每个 kernel，算术强度 `I = FLOPs / Bytes`。若 `I < 峰值算力/带宽`，是带宽
受限；否则是计算受限。大 GEMM 通常计算受限，LayerNorm/softmax/逐元素算子
带宽受限。

对并行方案，估计：

```text
T_step ≈ max(T_compute, T_comm) + T_nonoverlap
```

如果通信不能完全重叠，`T_comm` 直接加到关键路径。用 Nsight Systems 看：

- GEMM 时间；
- NCCL/NVLink 时间；
- 通信与计算是否重叠；
- 气泡（PP）；
- 数据加载空档。

## 14.5 MFU 的正确算法

```text
MFU = (6 · N · tokens_per_sec) / (GPU数 · peak_dense_flops)
```

注意：

- 用 **dense** 峰值，不要用稀疏峰值；
- `N` 是**激活参数**还是总参数？MoE 要用激活参数计算模型 FLOPs，但显存按总
  参数；
- 长序列要加注意力 FLOPs；
- 分母是理论峰值，不是实测 microbenchmark；
- 端到端吞吐要含数据加载/检查点/故障恢复。

厂商数字（例如 47%–48% MFU on H100、GB300 上的高 TFLOPs）通常在理想模型/
并行配置/特定精度下测得，不能直接作为项目 SLA。

## 14.6 配置搜索：一个可执行的流程

1. **单卡基线**：micro-batch=1，测 step time / 显存；
2. **加 TP**：2/4/8，观察 GEMM 效率和 all-reduce 占比；
3. **加 PP**：2/4/8，测气泡和 p2p；
4. **加 DP**：填满剩余卡，测梯度 all-reduce；
5. **加 SP/CP**：只在激活/序列是瓶颈时；
6. **加 EP**：MoE 模型，测 all-to-all 和专家负载；
7. **调 micro-batch/累积**：在不 OOM 的前提下增大；
8. **调 checkpointing**：用重算换显存，重测 step time；
9. **调精度**：bf16→FP8/FP4，验证收敛；
10. **端到端**：数据加载、评估、保存、故障恢复都算进去。

每一步只改一个变量，记录配置、吞吐、显存、loss。不要一次改五个维度。

## 14.7 一个配置取舍例子

模型：70B，bf16，序列 4K，集群 64×H100（8 节点×8 卡）。

候选：

- TP=8, PP=8, DP=1：显存最宽松，但 DP=1 没有数据并行，吞吐可能低；PP 气泡大。
- TP=8, PP=4, DP=2：更平衡；每卡状态约 `16×70/32 ≈ 35 GB`，加激活可能 OOM。
- TP=4, PP=8, DP=2：TP 通信少，但 PP 更深、气泡更大。
- TP=8, PP=2, DP=4 + ZeRO-1：DP 更大，状态更省，但梯度 all-reduce 更多。

需要用实测的 step time 和显存决定。一般经验：

- 先保证不 OOM；
- 再让 TP 不超过 8；
- PP 尽量小（减少气泡），除非跨节点必须；
- DP 尽量大（提高吞吐），但要考虑梯度通信；
- 长序列再上 CP/SP。

## 14.8 通信重叠

生产 Megatron 的性能很大一部分来自重叠：

- DDP 梯度 bucket 与反向计算重叠；
- TP all-reduce 与下一个 GEMM 重叠；
- PP p2p 与计算重叠；
- MoE all-to-all 与专家 GEMM 重叠；
- checkpoint 保存与训练重叠；
- 数据加载 prefetch。

mini 是同步实现，所以 tokens/s 不能代表生产性能；它只验证正确性。分析生产
性能时，先看 Nsight 的 overlap，再看算法。

### 14.8.1 异步 TP 的三种做法

TP 的 all-reduce 在关键路径上，生产实现通常用以下方式隐藏：

| 做法 | 思路 | 代价 |
|---|---|---|
| 异步 all-reduce | 先发起通信，后面还有 GEMM 时再 wait | 需要双缓冲/顺序保证 |
| 通信与 GEMM 融合 | 把 reduce-scatter/all-gather 融进 GEMM epilogue | kernel 复杂、硬件相关 |
| 分层通信 | 节点内 NVLink + 节点间 IB，先局部归约 | 需要拓扑感知 |

异步不是“把同步改成异步”这么简单：如果下一个 GEMM 依赖 all-reduce 的结果，
就必须 wait；如果两个 micro-batch 的通信/计算可以交错，才有重叠空间。顺序
错了会得到“能跑但数值错”的结果。

### 14.8.2 用 microbenchmark 验证重叠

```text
1. 单独测 all-reduce 时间 T_comm；
2. 单独测 GEMM 时间 T_gemm；
3. 测串行 T_serial = T_comm + T_gemm；
4. 测重叠 T_overlap；
5. 重叠效率 = (T_serial - T_overlap) / T_comm。
```

如果重叠效率接近 0，说明通信没有真正与计算并行（依赖/流/同步点问题）；如果
接近 1，说明隐藏得好。这个实验比“看总 tokens/s”更容易定位问题。

### 14.8.3 一个可算的重叠模型

设每步计算时间 `T_c`、通信时间 `T_m`、不可重叠部分 `T_n`。理想重叠下：

```text
T_step = max(T_c, T_m) + T_n
无重叠：T_step = T_c + T_m + T_n
```

相对加速比：

```text
speedup = (T_c + T_m + T_n) / (max(T_c, T_m) + T_n)
```

要让重叠有意义，必须满足三个条件：

1. **有独立工作**：通信等待期间还有另一个 micro-batch/另一层的 GEMM 可算；
2. **不同流/不同队列**：通信 kernel 与计算 kernel 能同时驻留；
3. **没有隐藏同步点**：`tensor.item()`、`.cpu()`、动态 shape、分配器锁、
   不必要的 `wait()` 都会把两条流重新串起来。

举例：`T_c=100ms, T_m=40ms, T_n=10ms`，无重叠 `150ms`，理想重叠
`110ms`，加速 `1.36×`；如果只重叠一半（`η=0.5`），则是 `130ms`，加速
`1.15×`。这解释了为什么“通信只占 27%”不代表“最多只能快 27%”——关键看
它是否在关键路径上、能否被独立工作填满。

实践顺序：

1. 先测无重叠基线；
2. 用 Nsight 看通信与 GEMM 是否在同一时间窗口；
3. 再加 bucket/双缓冲/多 micro-batch；
4. 每次只改一个重叠点，并重新跑数值等价性测试。

## 14.9 2026 的规模数字

NVIDIA 公开材料中常见的量级：

- 2B–462B 参数，数千 GPU，H100 上最高约 47%–48% MFU（厂商特定配置/模型，
  引用时按具体报告核对）；
- 1.7 的 462B/10T 行是**本书示例配方**（1000 卡口径），不是某个公开训练
  事实；公开锚点见附录 I.5；
- DeepSeek-V3 671B 的**原始训练**是在 H800 上完成的（FP8，约 2.788M H800
  GPU-hours）；GB300 NVL72 上常见的高 FP4/FP8 数字多为**推理/MLPerf**口径，
  不能当成“训练 MFU”。看到跨代硬件数字，先问：训练还是推理？dense 还是
  sparse？什么精度？什么 batch/序列？

这些数字说明“规模可行”，不代表你的模型/集群能达到同样 MFU。网络、数据、
故障率、模型结构都会显著影响端到端效率。

## 14.10 把配置搜索前移到命令行

`mini_megatron/analysis.py` + `cli estimate` 可以在开 GPU 之前做一阶筛选：

```bash
for cfg in tiny_tp2 tiny_pp2 tiny_tp2_pp2 tiny_cp2_pp2; do
  python -m mini_megatron.cli estimate --config configs/$cfg.json \
    --tokens 1e12 --gpu h100 --mfu 0.4 --json
done
```

对比每个配置的：

- 每卡状态/激活显存（EP 会把专家状态按 `ep` 再切一份）；
- 单层 attention score；
- DP/TP/PP/CP/EP 通信量；
- 估算 GPU-hours。

先淘汰“显存明显不够/通信量明显异常”的配置，再做真实 benchmark。估算器的
公式与假设见第 4 章；它不是性能模型，但能把配置搜索从“盲试”变成“有约束的
搜索”。

## 14.11 从约束到候选：一个可执行的求解器

14.6 给了流程，14.10 给了命令。这一节把两者收成一个**可枚举、可打分**的
求解器：输入模型/集群/预算，输出少量候选配置，而不是在全空间里盲试。

### 14.11.1 输入与硬约束

```text
world, layers, hidden, heads, seq_len, micro_batch, global_batch
GPU 显存, 单节点 GPU 数, NVLink/IB 有效带宽, 目标精度
可选：是否 MoE、专家数、是否长上下文
```

先过滤，不满足直接淘汰：

```text
world % (tp·pp·cp) == 0
tp ≤ 单节点 GPU 数（TP 留 NVLink）
pp ≤ layers（且 layers % pp == 0，mini/均匀切分）
cp ≤ seq_len（且 seq_len % cp == 0）
ep ∈ {1, dp}（mini 约束；生产可独立）
state_per_rank + activation_peak ≤ 0.9 · GPU_mem
global_batch % (micro_batch · dp) == 0
# 模型维度必须能被 TP 整除（mini/config.py 会直接拒绝）：
hidden % tp == 0; n_h % tp == 0; n_kv % tp == 0
vocab % tp == 0; ffn % tp == 0
# 开 SP 时序列也要能被 TP 整除：
seq_len % tp == 0
# MoE：
num_experts % ep == 0
```

### 14.11.2 枚举

候选空间其实很小：`tp` 只需枚举 `world` 的因子，`pp` 枚举剩余因子，`cp`
再枚举剩余；`ep` 在 mini 里只有 1 或 `dp`。

```python
def divisors(n):
    return [d for d in range(1, n + 1) if n % d == 0]

def valid(cfg, world, gpus_per_node):
    """镜像 mini_megatron/config.py 的硬约束；返回 False 表示模型根本建不起来。"""
    m, p, t = cfg.model, cfg.parallel, cfg.train
    tp, pp, cp, ep = (
        p.tensor_model_parallel_size, p.pipeline_model_parallel_size,
        p.context_parallel_size, p.expert_model_parallel_size,
    )
    dp = world // (tp * pp * cp)
    checks = [
        world % (tp * pp * cp) == 0,
        tp <= gpus_per_node,
        m.num_layers % pp == 0,
        m.max_seq_len % cp == 0,
        m.hidden_size % tp == 0,
        m.num_attention_heads % tp == 0,
        m.num_query_groups % tp == 0,
        m.vocab_size % tp == 0,
        m.ffn_hidden_size % tp == 0,
        (not p.sequence_parallel) or (m.max_seq_len % tp == 0),
        (not m.use_moe) or (m.num_experts % ep == 0),
        (not m.use_moe) or (ep in (1, dp)),      # mini 约束；生产 ep 可独立
        t.global_batch_size % (t.micro_batch_size * dp) == 0,
    ]
    return all(checks)

candidates = []
for tp in divisors(world):
    if tp > gpus_per_node: continue
    for pp in divisors(world // tp):
        for cp in divisors(world // (tp * pp)):
            dp = world // (tp * pp * cp)
            for ep in ({1, dp} if use_moe else {1}):
                cfg = make_config(tp, pp, cp, ep, dp)
                if not valid(cfg, world, gpus_per_node): continue
                est = estimate(cfg, world_size=world, tokens=tokens)
                if est["memory"]["estimated_peak_per_rank"] > 0.9 * gpu_mem:
                    continue
                candidates.append((cfg, est))
```

`world=64` 时因子枚举只有几百个组合，扣掉约束通常剩 10–30 个，完全可以
在本地秒级跑完；不需要黑盒搜索。

### 14.11.3 打分：先活下来，再比快

对每个候选算四件事：

1. **显存余量** `headroom = 1 - peak/gpu_mem`；<10% 直接降级；
2. **通信时间**：用 4.5.0 的 α-β 模型，分别算 DP/TP/PP/CP/EP，取每步总和；
3. **气泡**：1F1B `(pp-1)/(m+pp-1)`；VPP 用 `(pp-1)/(m·v+pp-1)`；
4. **可运维性**：PP 深度、跨节点边界数、DP 副本数、故障域。

一个实用的排序键（不是唯一真理）：

```text
score = est_step_time
      + λ_bubble · bubble_fraction · compute_time
      + λ_pp · (pp - 1) · p2p_latency
      + λ_risk · cross_node_penalty
```

`λ` 按团队经验设；关键不是公式精确，而是**同一套口径比较候选**。最后保留
3–5 个候选，做 100–300 步真实 benchmark，再选。

### 14.11.4 一个 64×H100、70B 的例子

假设：bf16、seq=4096、micro_batch=1、global_batch=1024、每节点 8 卡、
state 16 B/param。下表**只列 state**，激活必须另算——70B/4K 的激活很大，
把 state 当总显存会得出错误结论：

| 候选 | state/卡（未计激活） | DP | PP 深度 | 判断 |
|---|---:|---:|---:|---|
| TP8 PP4 DP2 | 35 GB | 2 | 4 节点 | state 稳、PP 气泡小；激活需重算+SP/CP 实测 |
| TP8 PP8 DP1 | 17.5 GB | 1 | 8 节点 | state 最松，但 PP 最深、无 DP 副本 |
| TP4 PP8 DP2 | 35 GB | 2 | 8 节点 | TP 通信低、PP 更深；看 IB 延迟 |
| TP8 PP2 DP4 | 70 GB | 4 | 2 节点 | state 已接近上限，加激活大概率 OOM |

这里的“state/卡”按 `16N/(tp·pp)` 估算；如果开 ZeRO-1/distributed optimizer，
优化器状态再按 `d_z` 切，TP8 PP2 DP4 可能重新变得可行——所以**先确定
优化器策略，再枚举**，否则会把可行方案误杀。激活则按 14.2 的公式乘
in-flight 后再判断；如果 state+激活超 90% 显存，先加 activation
checkpointing/SP/CP，而不是直接淘汰配置。

### 14.11.5 输出模板

```text
target: N=70B, seq=4096, world=64, GPU=80GB, bf16
constraints: tp<=8, layers%pp==0, peak<=72GB
candidates:
  1. tp8 pp4 dp2: peak=..., comm/step=..., bubble=..., score=...
  2. tp8 pp8 dp1: ...
  3. tp4 pp8 dp2: ...
next: 300-step benchmark on candidate 1 and 3; measure busbw and MFU
rollback: if OOM/comm > 40% or loss diverges, fall back to ...
```

### 14.11.6 常见错误

- 用单 micro-batch 激活当峰值（漏 `n_inflight`，见 14.2）；
- 忘记优化器状态分片，把 ZeRO 方案误判为 OOM；
- 用标称带宽而不是 microbenchmark 的 busbw；
- 只算平均通信，忽略 PP 的 p2p 延迟和小消息；
- 把 EP 的聚合字节除以单卡带宽（见 10.6）；
- 选出“理论最快”但不满足故障域/可运维性的配置。

求解器的价值不是替你做决定，而是把“拍脑袋”变成“可复现的候选 + 明确的
淘汰理由”。

## 14.12 本章小结

- 先硬约束（显存/拓扑），再谈 MFU；否则调优没有意义。
- TP 留 NVLink，PP 跨节点，DP 填剩余，CP/EP 按瓶颈加。
- MFU 要用 dense 峰值、激活参数、正确 FLOPs 口径。
- 配置搜索一次只改一个变量，记录显存/吞吐/loss。
- 生产性能很大程度取决于通信重叠，mini 是同步实现，不可直接比吞吐。
- 配置搜索可以枚举：先硬约束过滤，再用统一的通信/气泡/显存口径打分，最后
  用 100–300 步 benchmark 验证。

下一部分进入 mini-megatron 的工程实现，从配置和进程组开始。

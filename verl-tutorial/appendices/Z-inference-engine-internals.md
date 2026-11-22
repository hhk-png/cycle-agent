# 附录 Z Rollout 引擎内部：调度、分页 KV 与容量规划

第 15 章讲了“怎么接 vLLM/SGLang”，第 16 章讲了“怎么采样、怎么复用前缀”。但在生产里，最常见的困惑不是配置项叫什么，而是：

- 为什么把 `max_num_seqs` 调大，吞吐没涨、延迟反而炸了？
- 为什么 prefix cache 命中率 80%，TTFT 却没降？
- 为什么 PD 分离后 GPU 利用率更高，单请求延迟却更差？
- 为什么投机解码在离线 benchmark 上 2×，在 RL rollout 里几乎不赚？

这些问题都要回到引擎内部的**资源账**：token budget、KV block、调度队列与批内结构。本附录用通用的 continuous batching + PagedAttention 模型解释它们，并给出可以直接拿去做容量规划的公式。具体默认值随 vLLM/SGLang 版本变化，工程上以你锁定的版本为准；verl 侧的配置映射见 Z.10。

## Z.1 先把“一步”拆开

一次 rollout 不是“一次 generate 调用”，而是调度器的很多轮：

```text
while requests remain:
    admit waiting requests (prefill)      # 受 token / seq / KV 预算约束
    append one decode token per running   # 受 seq 预算约束
    preempt if KV blocks are exhausted     # recompute 或 swap
    execute one model step
```

三个预算决定了这一步能放什么：

| 预算 | 典型来源 | 约束的是什么 |
|---|---|---|
| token budget | `max_num_batched_tokens` | 一次前向里所有 token 的总和 |
| sequence budget | `max_num_seqs` | 同时在跑的序列数 |
| KV budget | `gpu_memory_utilization` 扣掉权重/激活后的剩余 | 能同时驻留多少 token 的 KV |

三者不是独立的：一个 8K prompt 的 prefill 会一次性吃掉大量 token budget 和 KV；一个 decode 请求每步只吃 1 个 token，但要求它的整段 KV 一直驻留。**调参的本质是决定“把预算优先给长 prefill、给多并发 decode，还是给长上下文驻留”。**

## Z.2 continuous batching 的调度循环

### Z.2.1 为什么叫 continuous

静态 batching 要等一个 batch 里最长的序列结束才能换下一批；continuous batching 以“一步”为粒度，任何序列结束就立刻把等待队列里的请求填进来。它带来两个直接结果：

1. **batch 的组成每步都在变**：prefill 与 decode 可以混在同一批（chunked prefill 开启时）；
2. **KV 的驻留时间与 batch 解耦**：序列结束就释放 block，新请求可以复用。

这也意味着“batch size”在引擎里不是一个数，而是至少四个：

```text
num_running_seqs   # 当前 decode 的序列数
num_waiting_seqs   # 排队等 KV/prefill 的序列数
num_prefill_tokens # 本步要算的 prefill token 数
num_decode_tokens  # 本步要算的 decode token 数（≈ num_running_seqs）
```

### Z.2.2 一次调度的顺序

以最常见的 FCFS（verl 的 `rollout.scheduling_policy: fcfs`）为例：

1. 从 waiting 队列取请求，尝试为它的 **prompt KV** 分配 block；
2. 如果 block 不够，先尝试驱逐/换出低优先级序列；仍不够则 preempt 当前请求或更早的请求；
3. 把能放下的 prefill token 放进本步（chunked prefill 下可以只放一个 chunk）；
4. 给所有 running 序列各加 1 个 decode token；
5. 执行模型前向，采样/验证，更新 block table；
6. 序列结束则释放 block，回到 waiting 队列继续填。

第 2 步是吞吐与延迟的分水岭。**KV 不够时不是“等一等”，而是要么丢 KV 重新算（recompute），要么把 KV 换到 CPU（swap）。** 两者的成本完全不同：

- recompute：之后要重新 prefill 一段前缀，成本 ≈ `2 * N_params * T_prefix` FLOPs；
- swap：要搬运 `KV_bytes` 字节，成本 ≈ `2 * KV_bytes / PCIe_BW`（换出再换入）。

在 PCIe 4.0 x16（约 25 GB/s 有效）上，1 GiB 的 KV 换出+换入约 80 ms；如果这段前缀重算只要 20 ms，recompute 更划算。反过来，前缀很长、GPU 算力紧张时，swap 可能更便宜。引擎通常按序列长度/优先级选择策略，但**preemption 次数是必须监控的指标**：它意味着调度已经在 KV 上“打架”。

### Z.2.3 一个数值例子

假设 32 层、8 个 KV head、head_dim=128、bf16：

```text
每 token 每层 KV = 2(K,V) × 8 × 128 × 2 B = 4 KiB
每 token 全部层   = 4 KiB × 32 = 128 KiB
8K 上下文         = 128 KiB × 8192 = 1 GiB
```

如果 `gpu_memory_utilization=0.5`、80 GB 卡、权重占 15 GB、激活/CUDA graph/临时 buffer 占 5 GB，则 KV 预算约：

```text
80 × 0.5 - 15 - 5 = 20 GB
20 GB / 1 GiB ≈ 20 条 8K 序列
```

把 `max_num_seqs` 从 32 调到 64 不会让 20 条变成 40 条；它只会让更多请求进入 waiting，或者在 KV 不足时触发 preemption。**`max_num_seqs` 是“允许并发”的上界，KV 才是“能够并发”的上界。**

## Z.3 PagedAttention 的块账

### Z.3.1 为什么需要分页

连续分配要求每条序列的 KV 在显存里占一段连续空间。变长生成长度不可预知，预留最大长度会浪费，按实际增长又会碎片化。PagedAttention 把 KV 切成固定大小的 block（常见 16 token），用 block table 把逻辑位置映射到物理 block：

```text
sequence 0: logical block 0 -> physical 7
            logical block 1 -> physical 2
sequence 1: logical block 0 -> physical 9
```

好处：

- 物理 block 不需要连续，外部碎片接近 0；
- 序列增长时按需分配 block；
- 相同前缀的 block 可以共享（copy-on-write）；
- 可以把 block 换出/换入，而不影响逻辑序列。

代价：

- 每条序列多一张 block table；
- 最后一个 block 内部有最多 `block_size - 1` 个 token 的浪费；
- attention kernel 要按 block 间接寻址，地址计算比连续布局复杂。

### Z.3.2 三个可直接用的公式

```text
kv_bytes_per_token = 2 × num_layers × num_kv_heads × head_dim × dtype_bytes
blocks_per_seq     = ceil(seq_len / block_size)
kv_bytes_per_seq   = blocks_per_seq × block_size × kv_bytes_per_token
block_table_bytes  = blocks_per_seq × 4 B          # int32，每序列
```

注意 `num_kv_heads` 不是 `num_attention_heads`：GQA/MQA 下 KV 头更少，KV 按 KV 头算。MLA 一类压缩注意力不存完整 K/V，公式要换成 latent 维度，不能直接套。

一个 4K 序列、block_size=16：

```text
blocks = 4096 / 16 = 256
内部浪费 ≤ 15 token ≈ 15 × 128 KiB ≈ 1.9 MiB
kv = 256 × 16 × 128 KiB = 512 MiB
block table = 256 × 4 B = 1 KiB
```

block table 相对 KV 可以忽略，但**block 数量决定了调度的粒度**：剩余 block 不够一条新序列的 prompt 时，请求只能等待或触发 preemption。

### Z.3.3 前缀共享的边界

两条序列只有完整 block 相同才能共享。若系统提示词是 1000 token、block_size=16，则前 62 个 block（992 token）可共享，最后 8 个 token 仍要各自计算。**前缀长度不是 block 整数倍时，命中率会低于直觉。**

共享 block 需要引用计数与 copy-on-write：一条序列要写某个共享 block 时，必须先复制。decode 阶段每个序列都会写自己的最后一个 block，所以共享主要发生在 prefill 的公共前缀上，而不是 decode。

## Z.4 前缀缓存：hash block 与 radix tree

### Z.4.1 两种实现

| 实现 | 结构 | 匹配粒度 | 典型系统 |
|---|---|---|---|
| hash block cache | 每个完整 block 的 hash（含前缀 hash）→ block | block | vLLM automatic prefix caching |
| radix tree |  token 序列的前缀树，节点带 KV 引用 | 最长公共前缀，通常按 block 落地 | SGLang RadixAttention |

hash 方案的查找是 O(1) 级，适合“完全相同的系统提示词 / 相同 prompt 的多条采样”；radix 方案能表达“共享一段前缀、后面分叉”的多轮/树搜索结构，适合 AgentLoop 与 best-of-n。

### Z.4.2 命中率要分三个口径

```text
token_hit_rate   = 命中 token 数 / 总 prompt token 数
request_hit_rate = 至少命中一个 block 的请求数 / 总请求数
saved_prefill    = 命中 token 数（可直接换算成省掉的 FLOPs）
```

只报 request_hit_rate 会高估收益：一个请求只命中系统提示词的前 16 个 token，也算“命中”。**容量规划要用 token_hit_rate 和 saved_prefill，而不是请求命中率。**

### Z.4.3 为什么命中率高但 TTFT 不降

四种常见原因：

1. **命中发生在 decode 阶段**：decode 每步只算 1 个 token，省不了多少；
2. **prefill 已经是大 batch、算力饱和**：省掉一部分 token 不一定减少 step 数；
3. **cache 查找/驱逐本身有开销**：block hash、LRU 维护、copy-on-write；
4. **权重更新后缓存失效**：旧 KV 对应旧权重，必须清空；如果版本号没进 key，会出现“命中但输出漂移”的静默错误。

第 4 条在 RL 里尤其重要：每次 `update_weights` 后，rollout 侧的前缀缓存必须按版本失效。第 16.12 节与第 14 章分别从 KV offload 和权重同步角度讨论过这一点；这里补上引擎侧的根因：**KV 是权重的函数，不是 token 的函数。**

### Z.4.4 GRPO 的 n 采样是前缀缓存的最佳场景

同一 prompt 采样 `n` 条时，prompt 的 KV 完全一致。理想情况下：

```text
无缓存 prefill token = n × T_prompt
有缓存 prefill token = T_prompt + (n-1) × 0   # 后续样本命中
节省比例 = 1 - 1/n
```

n=8 时理论上省 87.5% 的 prompt prefill。实际收益取决于：

- 采样请求是否被路由到同一 replica（第 15.7 节的前缀亲和）；
- 引擎是否在 batch 内复用同一 prompt 的 block；
- 采样是否交错到达，导致第一个请求还没算完，后面的请求已经开始。

## Z.5 chunked prefill 与 TTFT/ITL

### Z.5.1 不切块会发生什么

一个 32K prompt 的 prefill 如果独占一步，所有正在 decode 的序列都要等这一步结束。假设一步 50 ms，decode 的 ITL 就从 20 ms 变成 70 ms。对交互式 Agent，这是不可接受的尾延迟。

chunked prefill 把长 prefill 切成 `C` 个 token 的 chunk，与 decode 交错：

```text
step 1: prefill chunk 1 + decode
step 2: prefill chunk 2 + decode
...
step k: prefill chunk k + decode
```

### Z.5.2 两个近似公式

设每个 chunk 一步、每步耗时 `t_step`：

```text
TTFT ≈ queue_delay + ceil(T_prompt / C) × t_step
ITL  ≈ t_step
```

`C` 越小，TTFT 越长（步数多）、ITL 越稳（每步短）；`C` 越大，prefill 的 GEMM 效率越高，但 decode 被阻塞得更久。

实践中 `C` 受 `max_num_batched_tokens` 约束，并与 `max_num_seqs` 一起决定每一步的组成。长 prompt 任务几乎必开；短 prompt（< 1K）收益有限，反而增加调度开销。

### Z.5.3 一个尾延迟例子

- 32K prompt，prefill 总算力 400 ms；
- decode 每步 20 ms；
- 不切块：第一个 token 等 400 ms，期间所有 decode 的 ITL 出现 400 ms 尖峰；
- 切成 4K chunk（8 块）：每块约 50 ms，TTFT ≈ 400 ms（总 prefill 不变），但 decode 的 ITL 尖峰从 400 ms 降到 50 ms。

注意 **TTFT 的总和没变**，变的是“其他请求被阻塞的时间”。如果只看平均 TTFT，chunked prefill 可能没有收益；要看 p99 ITL 与队列等待。

## Z.6 CUDA graph 与动态 shape

### Z.6.1 捕获与重放

CUDA graph 把一串 kernel launch 录下来，重放时省掉 CPU 端 launch 开销。对 decode 这种每步很多小 kernel 的负载，收益明显。但 graph 绑定：

- 输入/输出的地址；
- 形状（batch、序列长度）；
- 控制流（不能有数据相关的分支）。

因此引擎通常按 shape bucket 捕获多张 graph，运行时 padding 到最近的 bucket。padding 会浪费算力：

```text
bucket = [1, 2, 4, 8, 16, 32, ...]
实际 batch = 17 -> 用 32 的 graph，浪费 47%
```

### Z.6.2 与 RL 权重更新的冲突

如果 graph 捕获了参数张量的地址，而权重更新是“原地 copy 到同一块显存”，graph 仍然有效；如果更新是“重新分配张量/重新加载模型”，旧 graph 就指向了旧地址，必须重新捕获。

这解释了两个现象：

- `enforce_eager=true` 时权重更新最稳，但吞吐下降；
- 某些后端在每次 sync 后重新 capture，导致 step 时间周期性尖峰。

调试顺序：先 `enforce_eager=true` 确认逻辑正确，再开 graph 看吞吐；如果开 graph 后输出漂移，优先查权重更新是否换了地址，而不是调采样参数。

## Z.7 投机解码：收益、无损性与 RL 的特殊问题

### Z.7.1 机制

投机解码用一个便宜的小模型（draft）连续提出 `γ` 个 token，目标模型（target）一次前向并行验证这 `γ` 个位置，接受匹配前缀，再从第一个不匹配处采样一个修正 token。

设单 token 接受率为 `α`，在独立近似下：

```text
期望接受草稿数 = α + α² + ... + α^γ = α(1-α^γ)/(1-α)
每步期望输出   = 1 + 上述值 = (1-α^{γ+1})/(1-α)
```

`α=0.8, γ=4` 时每步约 3.36 个 token；`α=0.5` 时只有 1.94 个。**接受率低于 0.5 时，draft 的开销很容易吃掉收益。**

### Z.7.2 无损采样不是“看起来对”

要保证输出分布与目标模型完全一致，拒绝采样必须使用正确的 residual distribution：

```text
接受 x 的概率 = min(1, p_target(x) / p_draft(x))
拒绝后从归一化的 max(0, p_target - p_draft) 采样
```

贪心解码简单（逐 token 比较），但温度/top-p/top-k 会改变分布；draft 与 target 必须使用同一套采样变换，否则输出有偏。**“accept rate 很高”不等于“分布正确”。**

### Z.7.3 RL 里最容易被忽略的三件事

1. **log-prob 必须来自 target**。PPO 的重要性比率是 `π_θ / π_old`；如果 rollout 记录的是 draft 的 log-prob，比率从一开始就错了。接受 token 的 log-prob 要用 target 在验证时的分布计算。
2. **权重同步会让 draft 失配**。draft 是旧策略/小模型，actor 更新后不接受率会下降；要么同步 draft，要么接受吞吐衰减。
3. **高并发 decode 下收益变小**。投机解码主要优化 memory-bound 的低 batch decode；当 batch 已经很大、GPU 算力饱和时，draft 的额外计算会挤占 target 的算力。

投机解码的验证协议应该包含：接受率、每步输出 token 数、target log-prob 与关闭投机时的相关性、以及同一 seed 下的分布检验（至少检查 mean/std/长度分布）。verl v0.9.1 把相关字段放在 `actor_rollout_ref.model.mtp`（`speculative_algorithm`、`speculative_num_steps`、`speculative_eagle_topk`、`speculative_num_draft_tokens`），而不是 rollout 段；见 Z.10。

## Z.8 容量规划：从 KV 字节到并发、吞吐与成本

### Z.8.1 五步账

```text
1. kv_per_token = 2 × L × H_kv × D × bytes
2. M_kv = M_gpu × util - M_weights - M_activations - M_graph - M_temp
3. max_kv_tokens = M_kv / kv_per_token
4. max_seqs(T) = floor(max_kv_tokens / T)
5. throughput = max_seqs(T) / (T_out × ITL)
```

第 5 步是 Little's Law 的变形：并发 `N`、每请求驻留时间 `R ≈ T_out × ITL`、吞吐 `X = N / R`。

### Z.8.2 一个完整算例

```text
GPU: 80 GB, util=0.5 -> 40 GB 预算
weights 15 GB, activations/graph/temp 5 GB -> M_kv = 20 GB
kv_per_token = 128 KiB
max_kv_tokens = 20 GiB / 128 KiB = 163,840 token
T=4096 -> max_seqs = 40
T_out=512, ITL=20 ms -> R = 10.24 s
X = 40 / 10.24 ≈ 3.9 seq/s ≈ 2000 output tok/s
```

把上下文从 4K 提到 8K，并发减半，吞吐也近似减半（在 decode 主导、ITL 不变的前提下）。这就是长上下文 RL 的成本来源：**不是 prefill 一次性的，而是 KV 驻留把并发压低了。**

### Z.8.3 prefill 与 decode 是两种 roofline

```text
prefill: 计算密集，时间 ≈ 2 × N_params × T_prompt / FLOPS_eff
decode:  访存密集，时间 ≈ max(2 × N_params × B / FLOPS_eff,
                              KV_bytes_per_step / HBM_BW)
```

`B` 是并发 decode 序列数。B 很小时 decode 被 HBM 带宽主导；B 大到一定程度才变成算力主导。这解释了为什么“加 batch”在低并发时几乎线性涨吞吐，在高并发时收益递减。

### Z.8.4 PD 分离的配比

设每条请求的 prefill 时间为 `t_p`、decode 时间为 `t_d`，则 prefill 与 decode 的 replica 比例大致为 `t_p : t_d`（还要加上 KV 传输时间）。如果 `t_p=50 ms`、`t_d=500 ms`，却按 1:1 配 GPU，prefill 侧会大量空闲。

KV 传输量：

```text
transfer_bytes = kv_per_token × T_prompt
```

32K prompt、128 KiB/token 就是 4 GiB/请求。在 100 Gbps（12.5 GB/s）网络上，单请求传输约 0.34 s；如果 prefill 只要 50 ms，传输成为瓶颈，PD 分离就不划算。**PD 的前提是网络带宽足够，而不是 GPU 不够。**

### Z.8.5 单位成本

```text
cost_per_1M_output_tokens =
    GPU_hourly_price × 1e6 / (output_tokens_per_second × 3600)
```

一张 $2/小时的卡，2000 tok/s 时约 $0.28/百万 token；利用率降到 25%，成本约 $1.1/百万 token。**报价里最容易被隐藏的变量是利用率**，不是卡价。

## Z.9 观测指标与故障模式

| 指标 | 回答的问题 | 异常信号 |
|---|---|---|
| TTFT p50/p99 | 用户/Agent 等第一个 token 多久 | p99 远高于 p50 → 长 prefill 阻塞或排队 |
| ITL p50/p99 | decode 是否稳定 | 尖峰 → chunk 太大、preemption、swap |
| queue delay | 请求在 waiting 队列待了多久 | 持续上升 → KV 不足或 replica 不够 |
| running / waiting | 并发结构 | waiting 高、running 低 → KV 碎片/block 不足 |
| KV usage | 显存是否打满 | 长期 100% → 没有余量应对长尾 |
| preemption count | 调度是否在打架 | 上升 → 降 `max_num_seqs` 或加 KV |
| prefix hit (token) | 缓存真实收益 | 高 request hit、低 token hit → 只命中系统提示词 |
| speculative acceptance | 投机是否值得 | 低于 0.5 → 关掉或换 draft |
| PD transfer time | 分离是否划算 | 与 prefill 同量级 → 网络瓶颈 |

常见故障链：

```text
max_num_seqs 调大
  -> waiting 变多但 KV 不够
  -> preemption 上升
  -> recompute/swap 增加
  -> ITL p99 爆炸、吞吐反而下降
```

```text
prefix cache 命中率高
  -> 但权重更新后没有失效
  -> 旧 KV + 新权重
  -> 输出漂移、log-prob 对不上
  -> PPO ratio 异常，训练悄悄偏置
```

## Z.10 与 verl 配置的映射

v0.9.1 的 rollout 配置（`verl/trainer/config/rollout/rollout.yaml`）与引擎旋钮的对应关系：

| verl 字段 | 引擎含义 | 调大/开启的代价 |
|---|---|---|
| `max_num_batched_tokens` | 每步 token budget | 显存/激活上升，单步变长 |
| `max_num_seqs` | 并发序列上界 | 超过 KV 容量会触发 preemption |
| `gpu_memory_utilization` | KV 预算比例 | 与训练共享 GPU 时容易 OOM |
| `enable_chunked_prefill` | prefill 切块 | 调度开销、prefill 效率下降 |
| `enable_prefix_caching` | hash/radix 前缀缓存 | 显存换 prefill，权重更新要失效 |
| `enforce_eager` | 关闭 CUDA graph | 吞吐下降，换稳定/可调试 |
| `free_cache_engine` | sleep/wake 释放 KV | 切换开销，异步时要注意在飞请求 |
| `tensor_model_parallel_size` | 每模型 TP 切分 | KV 头按 TP 切分（头数少于 TP 时会复制），通信上升 |
| `data_parallel_size` | rollout 副本数 | 路由/权重同步的副本数上升 |
| `scheduling_policy` | FCFS 等 | 影响尾延迟与公平性 |
| `disaggregation.*` | PD 分离 | KV 传输、拓扑与故障域变复杂 |
| `actor_rollout_ref.model.mtp.speculative_*` | 投机解码/MTP | draft 失配、log-prob 正确性风险 |

三个容易配错的组合：

1. **`enable_chunked_prefill=true` 但 `max_num_batched_tokens` 太小**：chunk 被切得过碎，prefill 效率下降；配置注释明确提示要相应提高 token budget 或降低 `max_model_len`。
2. **`gpu_memory_utilization` 按“推理独占”设 0.9**：colocate 训练时训练侧 OOM；常见起点是 0.4～0.5。
3. **PD 分离按 GPU 比例配 replica，却没算 KV 传输**：prefill 快了，decode 在等 KV；整体吞吐可能不如不分离。

## Z.11 MiniVerl 的边界

MiniVerl 的 `NaiveRollout` / `PDDisaggregatedRollout` 只模拟：

- 自回归采样与 KV 前缀复用；
- KV 传输字节与时间的账；
- sleep/wake 与权重版本失效。

它**没有**实现：

- continuous batching 的真实调度器；
- PagedAttention 的 block table 与 preemption；
- CUDA graph、chunked prefill 的 kernel 级交错；
- 投机解码的拒绝采样。

因此本附录的公式可以用来做容量规划，但不能用 MiniVerl 的墙钟时间验证这些公式。MiniVerl 负责验证“契约”（版本、mask、字节账），真实引擎负责验证“性能”。

## 本章小结

- rollout 引擎的“一步”由 token、sequence、KV 三个预算共同决定；`max_num_seqs` 不是并发的真正上界。
- PagedAttention 用 block table 换取低碎片与可共享；KV 字节按 `2 × L × H_kv × D × bytes × T` 估算。
- 前缀缓存要按 token 命中率与 saved prefill 评估；权重更新后必须按版本失效。
- chunked prefill 优化的是其他请求的 ITL，不必然降低总 TTFT。
- 投机解码的收益由接受率决定；RL 中必须记录 target log-prob，否则 PPO 比率错误。
- 容量规划从 KV 预算推到并发，再用 Little's Law 推到吞吐与成本；利用率是最容易被隐藏的成本变量。
- 配置调优的顺序是：先确认 KV/并发，再调 token budget 与 chunk，最后才开 CUDA graph 与投机。

## 延伸阅读

- vLLM PagedAttention 论文：<https://arxiv.org/abs/2309.06180>
- vLLM 文档（scheduler / prefix caching / speculative decoding）：<https://docs.vllm.ai/>
- SGLang RadixAttention：<https://arxiv.org/abs/2312.07104>
- 第 15 章：rollout 引擎与 server 模式
- 第 16 章：采样、前缀缓存与 KV offload
- 第 32.12 节：Roofline 与通信量估算
- 附录 W：资源分离、checkpoint engine 与弹性部署

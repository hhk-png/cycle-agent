# 附录 B 公式与常数

本附录把全书用到的公式和关键常数集中在一起。标注“官方”的数字来自 DeepInfer README、部署 Q&A 或 DeepSeek 公开技术资料；标注“示意”的数字只用于说明量级，不是承诺。公式可以在 `mini_deepinfer/roofline.py` 里找到可执行的版本。

## B.1 计算与带宽

线性层 FLOPs（一次乘加算 2 FLOPs）：

```text
prefill: 2 × N_active × T
decode:  2 × N_active
```

完整 causal attention 的 FLOPs（只算下三角，系数 2 对应 K 和 V 两次乘加）：

```text
prefill: 2 × L × T^2 × d
decode:  4 × L × T × d
```

其中 `L` 是层数，`T` 是序列长度，`d` 是 hidden size。如果不利用 causal 稀疏，prefill 的系数是 4；decode 的系数也是 4，但含义不同：它包含 QK 和 PV 各 `2Td`，且单 query 能看到全部历史，没有 causal 对折。MLA/CSA2/SWA 会进一步改变系数，不能直接套用。

算术强度与 roofline：

```text
intensity = FLOPs / Bytes
time >= max(FLOPs / P, Bytes / B)
ridge = P / B
```

其中 `P` 是峰值算力（FLOP/s），`B` 是带宽（Byte/s）。

GEMM 的 FLOPs 与最小访存：

```text
FLOPs = 2 × M × N × K
Bytes_min = (M×K + K×N + M×N) × element_bytes
```

tiled GEMM 的目标是让每个输入元素从 HBM 读一次；朴素实现会重复读，实际流量远大于 `Bytes_min`。

## B.2 KV cache

稠密 KV 每 token 字节数：

```text
KV_bytes_per_token = 2 × L × H_kv × d_h × bytes_per_element
```

其中 `H_kv` 是 KV 头数，`d_h` 是 head 维度。

带压缩和 scale 的版本：

```text
values = 2 × L × H_kv × d_h / compression_ratio
data = values × bits / 8
scales = (values / scale_group) × scale_bytes
KV_bytes_per_token = data + scales
```

DeepSeek-V4.1-Flash 公开的全局 KV footprint 约 890 字节/token（官方/公开资料），这是压缩、低精度和 SWA 共同作用后的结果。

SWA 每序列存储（官方 Q&A）：

```text
20 层（decoder） × 134 token × 528 B = 1,415,040 B ≈ 1.35 MiB
```

其中 134 = 128 窗口 + 6 回溯余量。这里的 20 层指参与落盘预算的 decoder 层；不要和“encoder 前两层也用 SWA”混在一起数。

DeepInfer 的 block size：

```text
resolved_tokens_per_block = 128
kvcache_align_to = 128 字节
主压缩 KV scale: 每 16 个通道
SWA scale: 每 32 个通道
```

分页数量：

```text
num_pages = ceil(context_tokens / block_size)
```

## B.3 延迟与吞吐

```text
TTFT ≈ 排队 + prefill + 第一步 decode
E2E ≈ TTFT + (N - 1) × TPOT
TPOT ≈ decode step time
```

Little 定律：

```text
并发 = 到达率 × 平均停留时间
```

M/M/1 排队近似：

```text
ρ = λ / μ
W_q ≈ ρ / (μ × (1 - ρ))
```

其中 `λ` 是到达率，`μ` 是服务率，`ρ` 是利用率。利用率接近 1 时，排队时间急剧上升。

每百万 token 成本：

```text
cost_per_mtok = C_gpu / u × 1e6 / (T × 3600)
```

其中 `C_gpu` 是 GPU 完全成本（美元/小时），`u` 是利用率，`T` 是输出 token/s。

## B.4 推测解码

累计接受概率 `c_i`，期望接受 token 数：

```text
E = 1 + c_1 + c_2 + ... + c_k
```

其中 `c_i` 是“前 i 个 draft 都被接受”的累计概率。如果每个位置的独立接受概率为 `a_i`，则 `c_i = a_1 × ... × a_i`。`roofline.py` 的 `expected_accept_length` 接收 `a_i`，在内部累乘。

加速比（目标一步成本 1，draft 每 token 相对成本 r）：

```text
speedup ≈ E / (1 + k × r)
```

选择验证长度：

```text
E(j) = 1 + sum_{i<=j} c_i
j* = argmax_j E(j) / (1 + j × r)
```

接受规则的分布保持：

```text
接受贡献 = min(q(x), p(x))
拒绝贡献 = max(0, p(x) - q(x))
两者之和 = p(x)
```

## B.5 并行与通信

All-reduce 通信量（ring）：

```text
2 × (N - 1) / N × size
```

All-gather 通信量：

```text
(N - 1) / N × total_size
```

All-to-all 通信量取决于路由分布，最坏约 `N × per_rank_size`。

MoE dispatch 的数据量：

```text
tokens × top_k × hidden_size × dtype_bytes
```

PD 的 KV 传输时间：

```text
transfer_time ≈ KV_bytes / link_bandwidth
```

## B.6 成本与存储

3 年 TCO 摊销：

```text
cost_per_gpu_hour = TCO / (GPU 数 × 3 × 8760 × 利用率)
```

缓存 TTL 的稳态存储：

```text
storage = tokens_per_second × ttl_seconds × bytes_per_token
```

批量摊销的吞吐加速：

```text
t(1) ≈ (W + KV_read) / BW
t(B) ≈ (W + B × KV_read) / BW
speedup(B) = B × t(1) / t(B)
           = B × (1 + KV_read / W) / (1 + B × KV_read / W)
```

其中 `W` 是权重字节数，`KV_read` 是每请求每步的 KV 字节。这个形式在 `B=1` 时等于 1；写成 `B / (1 + B × KV_read / W)` 会漏掉 `t(1)` 里的 KV 项，得到小于 1 的假结果。

batch 膝点（带宽项与算力项相等）：

```text
B* = (W / BW) / (2 × N_active / P - KV_read / BW)
```

只有当分母为正时才有有限膝点；否则系统始终受带宽限制。

容量规划与 P/D 配比：

```text
并发 = QPS × 平均停留时间
KV 总量 = 并发 × 平均上下文 token × 每 token KV 字节
P 卡数 = 非缓存 prefill token/s ÷ 每卡 prefill token/s × (1 + 冗余)
D 卡数 = 输出 token/s ÷ 每卡输出 token/s × (1 + 冗余)
```

TCO 分解：

```text
TCO = GPU 采购 + 服务器/CPU/内存 + 网络 + 存储
    + 电力 × 年数 + 机房/冷却 + 运维人力
    + 备件/故障 + 迁移/培训
每有效 GPU 小时成本 = TCO / (GPU 数 × 年数 × 8760 × 利用率)
```

毛利与敏感性：

```text
毛利率 = (每百万 token 收入 - 每百万 token 成本) / 每百万 token 收入
成本对吞吐的敏感度 ≈ 成本 / (1 ± 吞吐变化率)
```

## B.7 专项负载公式

图像 token 数：

```text
raw_patches = ceil(H/p) × ceil(W/p)
unshuffled = ceil(H/(p×u)) × ceil(W/(p×u))
llm_tokens = ceil(unshuffled / (q×q))
image_kv_bytes = llm_tokens × bytes_per_token
```

其中 `p` 是 patch size，`u` 是 pixel-unshuffle 因子，`q` 是池化因子。以 1024×1024、`p=16`、`u=3`、`q=2` 为例：4096 个原始 patch、484 个 unshuffle token、121 个 LLM token。

推理成本乘数：

```text
decode_ratio = (R + A) / A
kv_ratio = 1 + w × R / A
cost_multiplier = decode_ratio × kv_ratio
```

其中 `R` 是思考 token，`A` 是答案 token，`w` 是 KV 在总成本中的权重。

离线批处理的吞吐与 goodput：

```text
throughput = completed_requests / elapsed
goodput = requests_meeting_deadline / elapsed
deadline_miss_rate = missed / scheduled
```

TP 通信量（列并行的 all-gather、行并行的 all-reduce）：

```text
column_gather_bytes_per_rank ≈ (tp_size - 1) × shard_bytes
row_allreduce_bytes_per_rank ≈ 2 × (tp_size - 1) / tp_size × output_bytes
```

## B.8 官方常数（截至 2026 年 10 月）

DeepInfer：

- 开源时间：2026 年 10 月；许可证：Apache 2.0。
- 支持模型：DeepSeek-V4.1-Flash。
- 硬件：NVIDIA SM100、Huawei Ascend 950。
- 前置条件：Linux、GCC ≥ 13、Python 3.12、CUDA 13 / CANN 9.2。
- benchmark 规模：prefill 4 张 SM100，decode 64 张 SM100。
- 生产规模：公开口径的单个服务集群 155 万亿 token/天（可能跨多个数据中心）；38 美元/GPU 小时收入（谷时电价）；平均 TTFT 约 400 ms、P99 约 1 s；decode 约 230 token/s（编码可达 300）；输入缓存命中率约 96%；平均输入约 77K token。
- 缓存收益：命中后每卡 prefill 吞吐从约 6 万 token/s 提升到约 150 万 token/s（约 25 倍，特定负载）。
- decode benchmark：EP64DP64、nextn=3、接受率约 75%；256 请求/rank、131K 上下文时 >25,000 输出 token/s/GPU，约 100 token/s/user；8 请求时约 300 token/s/user。
- prefill benchmark：EP4DP4、140 token 有界重放；4,096 token/rank 约 6 万非缓存 token/s/GPU，65,536 token/rank 接近 9 万，单步约 730 ms。
- 推荐部署：prefill EP2-8/DP2-8，decode EP64-128/DP64-128；P:D 约 1:1；最小高效单元 64+64 GPU。
- KV 存储：3FS 约 100 PiB；每 GPU 14.4 TB 逻辑容量；TTL 3 天（Agent）/7 天（Chatbot）；20 × 30.73 TB SSD/节点；单节点至少 40 GB/s 读、5 GB/s 写；集群峰值读约 10 TiB/s、写约 850 GiB/s；10+2 EC。注意 100 PiB 与 14.4 TB/GPU 不能直接相除来反推集群 GPU 数：14.4 TB 是每 GPU 的 KV 缓存预算，100 PiB 是某一层/某一集群的 3FS 容量；直接相除会得到约 7,800 张卡，与 155T/天所需的 18 万到 60 万张卡不是同一口径。公开数字覆盖的集群范围、层和时间可能不同，引用时必须写明。
- SWA：本地缓存约 180 秒，命中率 >98%；每序列落盘 20 层 × 134 token × 528 B ≈ 1.35 MiB。
- 跨数据中心：每 8 GPU 约 400 Gbps。
- EPLB：prefill 4 冗余/GPU 起步、100 步窗口、1000 步重分配；decode 1 冗余/GPU 起步、1000 步窗口、10000 步重分配；decode EP64 时每卡约 6 个路由专家 + 1 个热点副本；`moe_lb` 约 1.045-1.09。
- DSpark：draft 5 token；动态验证 3.2-4.4，接受长度约 3.29；每步验证 draft 数减少约 30%；生产工作点 +4-7% 吞吐。
- 负载均衡：达到完美均衡离线 benchmark 的约 80%。
- 硬件边界：公开 benchmark 全部在 NVIDIA SM100；Ascend 950 支持存在，但没有公开性能数字；batch-invariant/deterministic 仅 NVIDIA。

DeepSeek-V4.1-Flash（公开资料）：

- 552B backbone + 196B Engram，约 748B 总参数。
- 40 层，20 encoder + 20 decoder；1 shared + 384 routed experts，top-6。
- hidden 5120；64 query heads，1 KV head；1M 上下文。
- prefill 激活约 8B，decode 约 16B；45T 多模态训练 token。
- 主 KV FP4（per-16 E4M3 scale），indexer KV MXFP4（per-32 ue8m0）。
- 全局 KV 约 890 字节/token；SWA 窗口 128。公开资料对层范围的描述不完全一致；一个自洽的读法是 encoder 前两层与 decoder 20 层使用 SWA，其余 encoder 层使用 CSA2，落盘预算按 decoder 20 层计。
- 视觉：DeepSeek-ViT，2D-RoPE，3×3 pixel-unshuffle，32 层左右。

## B.9 mini-deepinfer 默认值

```text
checkpoint：2 层、d_model=64、4 query heads、2 KV heads、head_dim=16、
            FFN=128、max_seq_len=512、window=64、第一层 SWA
config 默认：3 层、d_model=64、4 query heads、2 KV heads、head_dim=16、
            FFN=192、max_seq_len=512、window=32、第一层 SWA
词表：256 字节 + 8 特殊 token = 264
参数量：约 90K（checkpoint）
默认 block_size：16（不是生产 128）
默认 num_blocks：256
默认 max_num_seqs：16
默认 max_num_batched_tokens：512
默认 max_model_len：512
```

注意 `ModelConfig` 的默认值和 checkpoint 的配置不同：前者用于新建模型，后者用于加载 `assets/tiny_lm.npz`。把 `block_size` 改成 128 时，要同步调整 `num_blocks`、`max_num_batched_tokens` 和测试。

## B.10 常见换算

```text
1 GiB = 1024^3 B
1 TiB = 1024^4 B
1 PiB = 1024^5 B
1 小时 = 3600 s
1 天 = 86400 s
1 年 = 8760 小时（按 365 天）
```

带宽：

```text
1 GB/s = 8 Gbps
400 Gbps = 50 GB/s
```

注意厂商常用十进制 `TB` 和二进制 `TiB` 混用，做容量规划时要统一。

## B.11 公式的适用边界

这些公式是下界，不是性能承诺。它们假设：数据只从 HBM 读一次、没有 kernel launch 开销、没有通信拥塞、没有调度空转、没有内存碎片。真实系统会加入 10% 到 50% 的额外开销，具体取决于实现和负载。

公式的用途是“判断方向和量级”：如果算出来带宽时间是算力时间的 10 倍，优化算力就没意义；如果算出来排队时间是服务时间的 5 倍，优化 kernel 也不会有太大收益。先方向，后精度。

## B.12 平台扩展公式

LoRA 参数量：

```text
params_lora = r × (d_in + d_out)
ratio = params_lora / (d_in × d_out)
```

一个 `4096 × 4096` 的权重，`r=16` 时约有 131,072 个参数，占全量的 0.78%。

适配器字节数：

```text
bytes_adapter = params_lora × bytes_per_element
```

同一适配器要同时存 `A` 和 `B`，所以上面的参数量已经包含两者；如果还要存优化器状态，训练侧要再乘倍数，推理侧不需要。

级联的期望成本与质量：

```text
E[cost] = c_0 + (1-a_0)c_1 + (1-a_0)(1-a_1)c_2 + ...
E[quality] = a_0 q_0 + (1-a_0)a_1 q_1 + ... + (1-a_0)(1-a_1)... q_last
break_even_acceptance = c_cheap / c_strong
```

Embedding 批处理的 padding 浪费：

```text
waste = (sum(max_len_batch × batch_size) - sum(actual_len)) / sum(actual_len)
```

图内存规划的节省：

```text
saving = 1 - peak_bytes / sum(tensor_bytes)
```

`peak_bytes` 是活跃区间复用后的池大小，`sum(tensor_bytes)` 是所有张量各自分配时的总大小；前者不可能大于后者，但也不一定接近理论下界。

## B.13 训练侧、端侧、流量与财务公式

GRPO 组相对优势：

```text
A_i = (r_i - mean(r)) / (std(r) + eps)
```

组 rollout 的 token 与 KV 量：

```text
prefill_tokens = prompt_tokens                    # 共享前缀
decode_tokens  = G × output_tokens
kv_token_steps = G × (prompt × output + output × (output - 1) / 2)
```

组成本：

```text
gpu_seconds = prefill_tokens / prefill_tps
            + decode_tokens / decode_tps
            + kv_token_steps × kv_bytes_per_token / kv_bandwidth
cost_usd    = gpu_seconds / 3600 × gpu_hour_cost
```

rollout 稳态吞吐：

```text
stage_s = max(prefill_per_group / prefill_tps, decode_per_group / decode_tps)
samples_per_s = concurrency / stage_s × group_size
```

端侧权重与 decode：

```text
weight_bytes = parameters × bits / 8 × (1 + scale_overhead)
bytes_per_step = weight_bytes + batch × context × kv_bytes_per_token
memory_s  = bytes_per_step / (bandwidth × efficiency)
compute_s = 2 × active_parameters × batch / (compute × efficiency)
step_s    = max(memory_s, compute_s)
tokens_per_s = batch / step_s
```

端侧能耗与热：

```text
joules_per_token = power_w / tokens_per_s
sustained_tps = (peak × burst_s + peak × throttle_fraction × sustained_s) / total_s
```

排队：

```text
M/M/1: rho = lambda / mu
       W_q = rho / (mu × (1 - rho))
M/M/c: a = lambda / mu, rho = a / c
       C(c, a) = (a^c / c!) × (c / (c - a)) / [Σ_{k=0}^{c-1} a^k/k! + (a^c / c!) × (c / (c - a))]
       W_q = C(c, a) / (c × mu - lambda)
```

重试预算：

```text
retries_allowed = successes × retry_ratio
```

容量缓冲：

```text
peak = mean × peak_factor
usable = peak / target_utilization
with_failure = usable × failure_domains / (failure_domains - 1)
```

财务：

```text
NPV = Σ CF_t / (1 + r)^t
LTV = ARPU × gross_margin / monthly_churn
CAC_payback_months = CAC / (ARPU × gross_margin)
price = unit_cost / (1 - target_margin)
contract_value = monthly_commit × (1 - discount) × months
               + overage_rate × expected_overage_units × months
breakeven_utilization = annual_fixed_cost / (gpu_count × 8760 × contribution_per_gpu_hour)
```

这些公式和前面的 roofline/成本公式一样，都是下界或一阶估算。真实系统要加入排队、冷启动、故障、重试、税费和合同条款。

## B.14 市场与估值公式

自下而上的市场测算：

```text
TAM = Σ accounts × annual_tokens / 1e6 × price_per_mtok
SAM = TAM × serviceable_share
SOM(year) = SAM × target_share × min(1, year / ramp_years)
adoption(t) = ceiling / (1 + exp(-rate × (t - midpoint)))
funnel_final = visitors × Π conversion_rate_i
magic_number = net_new_ARR / sales_marketing_spend
sales_capacity(month) = reps × quota × min(1, month / ramp_months)
```

估值：

```text
EV = Σ FCF_t / (1 + WACC)^t + TV / (1 + WACC)^N
TV = FCF_N × (1 + g) / (WACC - g)        (WACC > g)
EV/Revenue = enterprise_value / revenue
MOIC = exit_value / investment
```

这些公式的输入是假设，不是事实。TAM 的客户数和单价、SOM 的目标份额和爬坡、DCF 的终值增长率和 WACC，都要用可验证的数据校准；把公式算得再精确，也不能弥补假设的错误。

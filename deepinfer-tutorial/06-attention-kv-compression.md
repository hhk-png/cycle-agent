# 第 6 章 注意力、KV 压缩与有界重放

本章导读：从标准注意力一路讲到 CSA2、SWA、YoCo 和 FP4 KV。重点不是背公式，而是理解每个压缩手段把哪一项成本降下来、又引入了什么新的系统复杂度。读完本章，你应该能解释 890 字节/token 是怎么来的，以及为什么“缓存命中”不等于“可以直接 decode”。

## 6.1 从 MHA 到 MLA

标准多头注意力（MHA）对每个 token 计算 Q、K、V：

```text
Q = X W_q,  K = X W_k,  V = X W_v
scores = Q K^T / sqrt(d_h)
context = softmax(scores + mask) V
output = context W_o
```

每个头都有独立的 K/V，所以 KV cache 的大小是 `2 × L × H × d_h`。MQA 让所有 query 头共享一组 K/V，KV 缩小 `H` 倍，但表达力下降。GQA 折中：把 query 头分组，每组共享一组 K/V，缩小 `H / H_kv` 倍。DeepSeek-V4.1 的 64 个 query 头共享 1 个 KV 头，属于极端的 MQA 式共享。

把三种结构的每 token KV 写成公式，可以直接比较：

```text
MHA: 2 × L × H × d_h × bytes
GQA: 2 × L × H_kv × d_h × bytes
MQA: 2 × L × 1 × d_h × bytes
```

假设 40 层、64 个 query 头、head 维度 128、FP16，MHA 的 KV 是 `2 × 40 × 64 × 128 × 2 = 1.31 MB/token`；MQA 是 `20 KiB/token`（20,480 字节），相差 64 倍。这个差距就是为什么长上下文模型几乎都采用 GQA/MQA/MLA 一类的共享或压缩结构。

MLA（Multi-head Latent Attention）换了一个思路：不直接缓存完整的 K/V，而是缓存一个低秩 latent，再在需要时投影出 K/V。DeepSeek 的 `mla_attn.py` 里能看到 `q_lora_rank`、`kv_lora_rank`、`qk_rope_head_dim` 等参数。MLA 的 KV cache 只存 latent 和 RoPE 部分，比完整 K/V 小得多。它的代价是每次 attention 前要做一次上投影，增加了计算量；但在 decode 阶段，计算量远小于带宽，所以这笔交易通常划算。

这里要把 MQA 和 MLA 分清。MQA 是“多个 query 头共享同一组完整的 K/V”，KV 的形状还是 `[T, d_h]`，只是头数变成 1；MLA 是“缓存一个低秩 latent，再在 attention 前投影出 K/V”，缓存的维度和 K/V 的维度不是一回事。V4.1 的 64:1 共享更接近 MQA 的头数关系，但它缓存的是压缩后的 latent，所以不能说“它就是 MQA”。这两个概念在容量公式里不能互换：MQA 的每 token 字节由 `d_h` 决定，MLA 的每 token 字节由 `kv_lora_rank + rope_dim` 决定。

DeepInfer 的 `V4AttentionBase` 把 Q/KV 投影、RMSNorm、RoPE、Wv/O 投影拆开，`V4Attention` 再叠加压缩、稀疏索引、SWA、YoCo 和跨层共享。`Compressor` 负责 C1/C2 压缩：C1 是恒等（不压缩），C2 用 gated low-rank 把相邻 token 的 KV 压成一半；`KProject` 从压缩后的 latent 投影出 index K。

## 6.2 为什么要分页

KV cache 的逻辑视图是“每个序列一条连续的 K/V 数组”。物理上如果按最大长度预留，1000 条 128K 上下文的序列会直接吃光显存；如果按需 `malloc`，不同长度的序列会产生外部碎片，且无法在请求之间共享前缀。

分页 KV 把物理显存切成固定大小的 block（DeepInfer 的 `resolved_tokens_per_block` 是 128），每个序列维护一张 block table，把逻辑 token 位置映射到物理 block。分配以 block 为单位，释放以 block 为单位；前缀相同的序列可以共享 block，写入时用 copy-on-write 保护。mini-deepinfer 默认用 16 token 的 block，方便在小模型上观察页表变化；本章没有特别说明时，数字按生产值 128 计算。

分页带来三个好处：按需增长、前缀共享、可换出。代价是 attention kernel 必须支持间接寻址，不能假设 K/V 连续；block table 的布局、页大小、对齐（DeepInfer 要求 128 字节对齐）都会影响 kernel 效率。

用数字感受一下。128K 上下文、block size 128，需要 1024 个物理页。如果一条序列只生成 100 个 token，按最大长度预留会浪费 1023 页；按页分配只用 1 页。前缀命中 96K 时，只需要再分配 256 页，前 768 页直接挂到新序列的页表上。第 14 章会讲这些页如何在不同序列、不同请求甚至不同机器之间共享。

mini-deepinfer 用 NumPy 的 `gather` 模拟这个间接寻址，逻辑和真实 kernel 一样，只是没有做向量化。第 22 章会逐行解释。稀疏索引的玩具对照在 `mini_deepinfer/sparse_attention.py`：它按 block 打分选 top-k，再在选中的 block 上做 attention；top-k 覆盖全部 block 时，结果与 dense attention 一致。

## 6.3 CSA2 的稀疏索引

CSA2 的“稀疏”分为两层：压缩和选择。

压缩层把 KV 压成更紧凑的 latent。C1 不压缩，C2 把每两个 token 的 KV 通过一个带门控的低秩变换合成一个。压缩率是模型配置的一部分，DeepInfer 只支持 C1/C2，C4/C128 已移除。

选择层用一个轻量 indexer 决定哪些历史 block 值得看。indexer 是 MQA 结构：query 经过一个小的投影，和 index K 做点积，得到每个候选 block 的分数，再取 top-k。主 attention 只对选中的 block 做计算。

为了让稀疏成本不随上下文爆炸，V4.1 做了层次化候选：第一个 Full-Mode decoder 层构建一个候选池（最多 2048 个 block × 8 个位置），后续层只在这个池里做 top-512 搜索。Full/Reindex/Reuse 三种模式让不同层分担“建池、重建索引、复用索引”的工作。

这套设计的系统含义是：KV cache 不再只有一种布局。主 KV、index K、SWA KV、state cache 是分开的，`KVCache` 里对应 `ca_cache`、`ica_cache`、`swa_cache`、`ca_state`、`ica_state`。调度器和 cache engine 必须同时管理它们，不能只按“每层 K/V”来算显存。

稀疏索引的收益也不是免费的。indexer 本身要算分、排序、取 top-k，还要在 CP 下做 all-gather；候选池的构建和维护也是成本。一个粗略的模型是：

```text
indexer_cost ≈ 2 × T × C × d_index + sort_cost(T × C)
main_cost ≈ 4 × T × k_blocks × block_size × d
```

其中 `C` 是候选 token 数，`k_blocks` 是选中的 block 数，`block_size` 是每个 block 的 token 数（生产值 128），`d_index` 是 indexer 的维度。主 attention 的计算量取决于选中的 token 数 `k_blocks × block_size`，而不是 block 数本身；把 `k_blocks` 直接当成 token 数会低估一个数量级。当 `k_blocks × block_size << C << T` 时，稀疏才真正省钱；如果选中的 token 数接近 `T`，稀疏索引的开销反而会让它比稠密 attention 更慢。评估 CSA2 时要把这些算进去，不能只看主 attention 的 FLOPs。

## 6.4 FP4 KV 与 890 字节/token

公开资料给出的 V4.1 主 KV 格式是 FP4：主 KV 每 16 个通道共享一个 E4M3 scale（类似 NVFP4，但没有全局 scale）；indexer KV 每 32 个通道共享一个 ue8m0 scale（MXFP4）；MoE 路由专家权重也是每 32 个通道的 MXFP4。DeepInfer 的 `derive_main_kv_cache_num_per_channels` 明确写了：主压缩 KV 每 16 个值一个 scale，SWA 每 32 个值一个 scale。

把这些压缩、低精度和 SWA 叠加，V4.1 的全局 KV footprint 约为 890 字节/token。这个数字可以拆成几层来理解。先看稠密 FP8：用示意的 `head_dim=128` 算，`2 × 40 层 × 1 个 KV 头 × 128 维 × 1 字节 = 10,240 B/token`。公开资料没有把 V4.1 的 latent 维度和每层压缩率给到可以逐项复算的程度，所以这个 10,240 是“量级示意”，不是官方 footprint；下面的 8.7% 也只在这个示意口径下成立。按 C2 把相邻两个 token 的 KV 合成一个，主 KV 降到约 5,120 B/token；再用 FP4（0.5 字节/值）和每 16 个值一个 E4M3 scale（0.0625 字节/值），主 KV 约 `5,120 × 0.5625 ≈ 2,880 B/token`。这 2,880 是“C2 + FP4 + per-16 scale”的主 KV 上界，已经比稠密 FP8 小 3.6 倍。

从 2,880 再降到 890，还依赖 SWA 只保留窗口、decoder 共享一份 global KV、不同层使用 Full/Reindex/Reuse 模式、以及跨层复用和状态摊销。这些机制的分项没有公开到可以逐项相加的程度，所以 890 应作为“公开资料的实测口径”，而不是一个能从单一公式硬推出来的常数。作为对比，稠密 FP16 KV 往往是每 token 几十 KB 到上百 KB；公开文章说 890 约为 V4-Flash 的 1/4，比 V1 小 437 倍，这些倍数是特定模型和格式下的比较，不要直接套到别的模型。在自己的模型上，按第 4 章的公式逐项重算，比硬套 890 可靠。

FP4 的工程难点不在“存”，而在“算”。反量化要和 attention/GEMM 融合，scale 的布局要满足 kernel 的对齐要求，不同硬件的 FP4 支持不同（SM100 和 Ascend 950 的指令不一样）。DeepInfer 用 TileLang 写了一批 fused kernel，把 norm、RoPE、Hadamard、cast、store 融合在一起，减少中间张量和 HBM 往返。

## 6.5 SWA 与有界重放

SWA 只保留最近 `window_size` 个 key。V4.1 的窗口是 128。它把 attention 从 `O(T^2)` 降到 `O(T × 128)`，但也让“前缀缓存”变得复杂：block KV 可以缓存，SWA KV 只覆盖窗口，跨请求复用时窗口可能不完整。

DeepInfer 的 SWA 存储策略很细。官方 Q&A 说，只有 `state_size_to_disk = 20 层 × 134 token × 528 B = 1,415,040 B` 每序列进入存储；528 B 是每层每 token 的 MXFP8 SWA KV 开销；存储位置按 128 token 对齐，decode 时每跨过一个 128 边界就把当前窗口复制到 replica region，结束时从最近一次完整副本取。

528 B 这个数字值得单独算一次。如果只按“1 个 KV 头 × 128 维 × K 和 V 两个张量 × 1 字节”算，每层每 token 是 256 B；528 B 大约是它的两倍。差额不是凭空来的：MXFP8 的 scale、对齐填充、窗口副本、以及 indexer/边界状态都可能被计入这个口径。公开资料没有给出可以逐项相加的拆分，所以本书把它当作“官方实测的每层每 token 开销”，而不是从 128 维直接推出来的理论值。你能从 528 推出来的是容量量级：20 层 × 134 token × 528 B 约 1.35 MiB/序列，1000 并发约 1.32 GiB；不能从 528 反推出“每层一定存了 528 个值”。

这里有两个容易混淆的数字：134 是**写入存储**的 SWA token 数，等于窗口 128 加 6 个回溯余量；140 是 prefill/decoder **重放**的默认 `keep_tokens`，比最小下界 134 多 6 个 token 的边界余量。前者描述“存多少”，后者描述“重算多少”，不能互相替代；改窗口大小时两者要分别调整。

命中策略是：先用 block KV 查询，得到命中长度 L1（128 的倍数）；再查 SWA KV。如果 SWA 覆盖了 `L1 - 128` 到 `L1` 这段尾部，命中长度保持 L1，不需要重放；否则命中长度仍是 L1，但重放起点退到 `L1 - 128`，即保留最后一个完整窗口、重算这 128 个 token。这里退回的是重放起点，不是 block 命中长度；把两者混为一谈会低估可复用的前缀。重放时位置 i 的 query 只和 `max(s, i-127)` 到 i 的 key 计算，成本是常数，不随上下文长度增长。

这就是“有界重放”的含义：不是把整个前缀重算，而是只重算重建窗口所需的最小 suffix。mini 的 `mini_deepinfer/yoco.py` 把这段判定逻辑单独实现成了可测试的函数：`plan_encoder_hit` 处理 block 命中与 SWA 覆盖，`plan_decoder_replay` 把 decoder 的重放限制在 `keep_tokens` 以内。第 24 章会用它做容量实验。代价是近似性：重放得到的 SWA KV 不是完整历史的精确值，但在 SWA 的语义下，窗口外的 key 本来就不参与计算，所以近似是可控的。

注意一个容易混淆的点：block KV 命中 128K，不代表 SWA 也命中 128K。block KV 是“全局的压缩历史”，SWA 是“最近窗口的局部状态”，两者的生命周期和存储位置不同。调度器必须分别检查，不能用一个命中长度覆盖两种缓存。

## 6.6 Decoder 侧的 YoCo

CED 的 decoder 层没有自己的 global KV，只有 SWA。要精确重建 decoder 的 SWA，需要把最后约 2560 个输入 token 过 20 层 decoder：每层窗口 128，20 层的依赖锥累积到约 `20 × 128` 个 token，这几乎等于把一段不短的前缀又做了一遍。DeepInfer 的做法是只重放最后 `keep_tokens` 个 token（默认 140，最小 134），把依赖锥截断成常数窗口，结果只用于 decode，不进入前缀缓存。因此这是近似重建，不是精确重建。

`DEEPINFER_YOCO_PREFILL_TRUNCATION_MODE` 有三个值：`off`、`mask`、`compact`。`compact` 模式会真正裁掉 query，只保留一个受控的 suffix；`mask` 模式保留完整布局，用 mask 屏蔽不参与的部分。`DEEPINFER_YOCO_PREFILL_KEEP_TOKENS` 默认 140，必须至少覆盖 `window_size + max_backtracking`（128 + 6）。

代码里 `compact_yoco_boundary` 会在边界层把 hidden、residual、top-k 索引重排成 compact 布局，`_store_yoco_full_kv` 再用 full hidden 更新主 KV/index KV。这个“query 裁掉、KV 仍覆盖全上下文”的不对称，是 YoCo 实现里最容易读错的地方。

为什么保留 140 而不是 128？因为 SWA 的窗口不是严格从当前位置往前数 128 个 token；索引、RoPE、回溯和边界对齐都可能需要多看几个位置。140 是一个“窗口 + 安全余量”的工程值，不是数学下界。改窗口大小时，这个值必须同步调整。

## 6.7 对推理系统的影响

把本章的机制放到引擎视角，会得到几个结论。

第一，KV cache 的容量计算不能只看主 KV。SWA state、index K、压缩边界状态都要算进去，而且它们的生命周期不同：主 KV 可以进 3FS，SWA 只在产生它的机器内存里，跨机命中要拉取。

第二，前缀命中不是一个布尔值。命中长度由 block KV 决定，是否要重放由 SWA 决定；decoder 侧还有额外的截断和重放规则。调度器在分配 KV 时，必须知道“命中多少、还差多少、要不要重放”。

第三，低精度 kernel 的收益依赖融合。单独把 KV 存成 FP4，再在 attention 前反量化，会把带宽收益吃掉；只有把反量化融进 attention/GEMM，才能把 890 字节/token 变成真实的吞吐。

第四，稀疏注意力的开销不只在 attention。6.3 节已经给出 indexer 成本的量级；在系统层面，还要把 CP 下的 all-gather、候选池的构建和维护、top-k 排序算进去。评估 CSA2 时不能只看主 attention 的 FLOPs。

第五，缓存的设计要区分“可重建”和“不可重建”。block KV 可以从 token 重新算，SWA 也可以重放，但重放有成本；decoder 的 global KV 如果丢失，重建代价更高。系统在做换出、迁移、故障恢复时，要按重建成本排序，而不是一视同仁。

## 6.8 Kernel 视角：为什么融合决定收益

理解了算法，还要看 kernel 怎么把它变成时间。V4.1 的 attention 路径至少有四类 kernel。

**FlashMLA**：面向 MLA 的 attention kernel。它把 Q/K/V 的投影、RoPE、attention 的 tiling 和 online softmax 融合在一起，避免把中间张量写回 HBM。decode 时 batch 小、序列长，FlashMLA 会用 split-K 把长序列的 KV 分块并行，再把部分结果归约。DeepInfer 的 Q&A 说，开源 FlashMLA 比内部版本少一个手调 SASS 优化，decode kernel 慢 5% 到 10%，但端到端差距最多 5%。

**DeepGEMM**：FP8/FP4 GEMM 库，使用 TMA、warp specialization、异步拷贝和精细的 scale 布局。它的价值不只是“低精度矩阵乘”，而是把反量化、scale 应用和累加融合进 GEMM，避免额外的访存。DeepInfer 的 `linear.py` 里能看到 `transform_sf_into_required_layout` 之类的调用，就是在为 GEMM 准备 scale 布局。

**TileLang kernel**：DeepInfer 用 TileLang 写了一批判定融合算子，例如 `fused_norm_rope_hadamard_cast_store_kvcache`。它把 RMSNorm、RoPE、Hadamard 变换、量化 cast 和 KV cache store 合成一个 kernel，减少中间张量和 kernel launch。对 decode 这种每步只有几毫秒的负载，少几次 launch 可能就是几个百分点的吞吐。

**DeepEP**：MoE 的 all-to-all 库，支持 NVLink 和 RDMA，提供低延迟 dispatch/combine。它和 Mega MoE 配合，把 token 分发、专家 GEMM、结果合并尽量重叠。

这些 kernel 的共同点是“融合”和“对齐”：融合减少 HBM 往返，对齐让 TMA/向量化能工作。读 DeepInfer 的 kernel 代码时，先看它融合了哪几个算子、输入输出的 layout 是什么、scale 怎么排，再看具体的索引计算。不要一上来就逐行读 TileLang 的循环。

用第 4 章的 roofline 看，融合改变的是“搬多少字节”，而不是“算多少 FLOPs”。一个 attention 如果本来要把 32K×32K 的分数矩阵写回 HBM，再读回来做 softmax，它的瓶颈就从算力变成了带宽；FlashAttention 的 tiling 把分数留在 SRAM，瓶颈回到算力。这就是为什么同一个数学，融合前后可能差几倍。

## 6.9 从 KV 字节到显存预算

把本章的数字放进一个显存预算，可以看到长上下文服务为什么“看起来能跑、实际跑不了”。

按公开的 890 字节/token 计算，单条请求的 KV 是：

```text
77,000 token   × 890 B ≈ 68.5 MB
131,072 token  × 890 B ≈ 116.7 MB（约 111.3 MiB）
1,048,576 token × 890 B ≈ 933.2 MB（约 890 MiB）
```

这里统一用精确 token 数和二进制单位，避免 K=1000/1024 混用。如果一张 GPU 有 80 GiB HBM，扣掉权重分片、激活、通信 buffer 和 CUDA Graph 预留，假设只剩 40 GiB 给 KV，那么 128K 上下文只能放约 368 条并发，1M 上下文只能放约 46 条。这个数字还没有算 SWA state、indexer KV、副本和碎片；实际并发会更低。

这也是为什么 DeepInfer 要把 block KV 放到 3FS，而不是全部放在 HBM：HBM 放不下的部分，用共享存储承接，用 TTL 和 LRU 管理，用异步预取和 GDR 掩盖传输延迟。公开资料给出的 3FS 集群约 100 PiB、峰值读约 10 TiB/s、写约 850 GiB/s；单节点配置是 20 × 30.73 TB SSD，至少 40 GB/s 读、5 GB/s 写。冗余方式有 CRAQ 链式复制和部署层 10+2 EC 两种口径，细节和取舍见 14.5 节。这些数字说明 KV 存储是一个独立的基础设施，不是“显存不够时的临时 swap”。

SWA 的预算要单独算。公开数字是每序列 20 层 × 134 token × 528 B = 1,415,040 B，约 1.35 MiB；1000 并发就是 1.415 GB，约 1.32 GiB。这里容易把 MB 和 MiB 混着乘，差 5% 左右，做容量规划时要统一单位。它比主 KV 小，但生命周期不同：主 KV 可以进 3FS，SWA 通常在本地内存，跨机命中要重新拉取或重放。容量规划时不能只算主 KV。

一个实用的估算顺序是：先按上下文长度算主 KV；再加 SWA state（每序列固定）；再加 indexer KV（按层和候选数）；再乘副本和 EC 开销；最后留 20% 到 30% 的余量给碎片和峰值。任何一步漏掉，都会得到“理论能放 1000 并发，实际 600 就开始抢占”的结论。

## 6.10 一张页表走完前缀命中与 COW

把分页、前缀哈希和 COW 放在一个例子里，比单独背定义清楚。设 block size 为 128，序列 A 有 300 个 token。它需要 `ceil(300/128)=3` 个物理页：页 0 存 token 0 到 127，页 1 存 128 到 255，页 2 存 256 到 299，页 2 只用了 44 个槽位。A 跑完后，前缀缓存只注册前两个**满页**；页 2 不满，不注册，因为共享一个半满的尾页会在追加时触发 COW。

序列 B 的前 300 个 token 与 A 完全相同。`match_prefix` 从页 0 开始逐页查哈希：如果 A 已经释放，页 0 和页 1 的引用计数都从 0 变成 1；如果 A 仍在运行，两页都从 1 变成 2。引用计数是按页各自加一，不存在“页 0 加一次、页 1 加两次”的写法。页 2 没有注册，匹配停止。于是 B 的命中长度是 256，页表是 `[页0, 页1]`，还要为 token 256 到 299 分配一个新页 3。B 的 prefill 只算 44 个 token，而不是 300 个；KV 写入从位置 256 开始，落在页 3，不会碰 A 的页 2。

如果 B 继续生成到第 301 个 token，写入位置 300 仍在页 3 内（页 3 的槽位 0 到 127 对应 token 256 到 383），所以不需要 COW。这就是“只共享满页”的好处：共享的页不会被追加，追加总是发生在私有尾页上。

COW 什么时候才会发生？当两个序列共享同一个**正在被追加的页**时。例如引擎支持 fork：序列 B 从 A 的第 200 个 token 处分叉，前 200 个 token 共享页 0 和页 1 的前 72 个槽位。如果 B 要写位置 200，而页 1 的引用计数是 2，直接写会破坏 A 的 KV；这时分配页 4，把页 1 的 128 个槽位整块复制过去，把 B 的页表项从页 1 换成页 4，再把新 token 写到页 4 的槽位 72。复制的单位是整页，不是“到当前长度”，因为 kernel 可能读到页尾；复制整页牺牲一点带宽，换来确定性。

这个例子还能解释两个常见 bug。第一，如果 `lookup` 把 `ref_count == 0` 当成无效，A 释放后页 0 和页 1 的引用计数都是 0，B 就永远命不中；缓存块在没人引用时正好是 0，这是最容易写反的地方。第二，如果前缀匹配把半满的页 2 也共享出去，B 的第一次追加就会触发 COW，复制 128 个槽位，而实际只需要 44 个；命中率看起来高了，COW 成本却可能吃掉收益。第 22 章会在 mini 里逐行实现这两个边界。

## 小结

V4.1 的注意力系统是“压缩 + 稀疏 + 窗口 + 低精度 + 有界重放”的组合拳。MLA/C2 降低每 token 的 KV 维度，CSA2 让注意力只看候选块，SWA 把窗口限制在 128，YoCo 把重放限制在常数 suffix，FP4 把字节数压到 890/token。每一个手段都降低了某一项成本，同时把复杂度推给了 KV 管理、kernel 和调度。下一章进入 MoE 和专家并行。

## 练习

1. 用 MLA 的 latent 维度 `d_c`、RoPE 维度 `d_r`，写出每层每 token 的 KV 字节数，并与标准 MHA 对比。
2. 解释为什么“block KV 命中 128 个 token，但 SWA 缺失”时要退回 128 个 token 重放，而不是直接用命中长度。
3. 如果 SWA 窗口从 128 增到 256，`DEEPINFER_YOCO_PREFILL_KEEP_TOKENS` 至少应该设成多少？重放成本会怎么变？
4. 用第 4 章的 roofline 公式，估算 32K 上下文的稠密 attention 在“写回分数矩阵”和“Flash 式 tiling”两种实现下的时间差距。需要哪些假设？

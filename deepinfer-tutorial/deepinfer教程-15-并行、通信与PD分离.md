# 第 15 章 并行、通信与 PD 分离

本章导读：单卡放不下模型，或者单卡吞吐不够时，就要并行。本章区分 DP、TP、EP、CP 四种并行，解释 DeepInfer 为什么在 decode 选 Wide-EP、在 prefill 选小 EP/DP，讲清 all-reduce、all-gather、all-to-all 的代价，最后拆开 E/P/D 分离和负载均衡器。

## 15.1 四种并行

**数据并行（DP）**：每张卡有完整模型，处理不同的请求。DP 不减少单卡显存，但能线性扩展吞吐；缺点是每张卡都要放完整权重，模型大时放不下。在 MoE 部署里，DP 通常指 attention/非专家部分的副本，专家权重仍由 EP 切分；不要把它理解成“每张卡复制整个 552B 模型”。

**张量并行（TP）**：把同一个矩阵乘按列或行切到多张卡，每层做 all-reduce 或 all-gather。TP 能减少单卡显存和计算，但通信频繁，通常只在 NVLink 域内用。

**专家并行（EP）**：只切 MoE 专家，每个 rank 持有部分专家，token 通过 all-to-all 去对应 rank 计算。EP 适合 MoE，因为专家之间没有依赖，切分自然。

**上下文并行（CP）**：把序列维度切到多张卡，每张卡处理一段上下文，attention 需要跨卡交换 K/V。CP 适合超长上下文，通信量随序列长度增长。

DeepInfer 的选择是：attention 用 DP（#DP = #EP），MoE 用 EP，prefill 用 EP2 到 EP8 / DP2 到 DP8，decode 用 EP64 到 EP128 / DP64 到 DP128；不把 TP 作为默认方案，CP 以 balanced CP 的形式用于长上下文和特定拓扑，主配置仍以 DP/EP 为轴。这个选择的前提是模型能放进单卡（V4.1 的专家分片后），以及 NVLink/supernode 的互联足够快。

四种并行可以组合。一个常见的组合是“DP × EP”：每个 DP 副本处理不同请求，副本内用 EP 切专家；attention 在副本内是完整的，MoE 通过 all-to-all 分发。另一个组合是“DP × CP”：每个 DP 副本处理不同请求，副本内用 CP 切长上下文；attention 需要 all-gather K/V。组合的代价是通信域变大，故障域也变大。

## 15.2 为什么 decode 用 Wide-EP

Decode 每步只有少量 token，但要读所有激活专家的权重。EP 越大，每个 rank 持有的专家越少，单步要读的权重越少，带宽时间越短。官方说 Wide-EP 大幅降低 MoE 专家部分的每步内存流量，配合 EPLB 能有效提高 TPS。

代价是 all-to-all 的 rank 数多、通信模式复杂、负载不均衡更难处理。EP64 不意味着“单个 token 的 6 个专家分散在 64 个 rank 上”：一个 token 最多落在 top-k 个 rank（top-6 就是最多 6 个）。但一个 batch 里不同 token 的目的 rank 会覆盖全部 64 个 rank，集合通信仍要 64 方参与，固定延迟和尾延迟来自通信域的大小，而不是单个 token 的 fan-out。DeepInfer 用 DeepEP 的优化 all-to-all、Mega MoE 融合、EPLB 来压这些开销。

Prefill 则相反：token 多、矩阵大，计算密集，EP 太大会让每个专家的 GEMM 变小，降低 tensor core 利用率。所以 prefill 用较小的 EP/DP，把更多 token 留在本地，减少通信。

用公式看，decode 单步的权重流量不能简单写成 `active_params × weight_bytes / EP`，因为只有路由专家权重随 EP 缩小；attention 权重和共享专家不随 EP 缩小。更准确的形式是 `W_expert / EP + W_nonexpert`，其中 `W_expert` 是激活的路由专家权重，`W_nonexpert` 是 attention、共享专家等每卡都要读的部分。all-to-all 的数据量约为 `tokens × top_k × hidden × dtype × 2`。第一项随 EP 下降，第二项不变，通信随 EP 上升（更多 rank 参与，固定延迟和拥塞增加）。Wide-EP 的收益在“专家权重流量下降”大于“通信开销上升”的区间；当 `W_nonexpert` 占比很高时，EP 再大也压不动总权重流量。

## 15.3 集合通信的代价

三种常见集合通信：

- **all-reduce**：所有 rank 的向量求和，常用于 TP 的梯度/激活同步。Ring all-reduce 的通信量约 `2 × (N-1)/N × size`。
- **all-gather**：每个 rank 收集所有 rank 的分片，常用于 CP 的 K/V 拼接。通信量约 `(N-1)/N × total_size`。
- **all-to-all**：每个 rank 向每个 rank 发送不同的数据，常用于 EP 的 token dispatch/combine。通信量取决于路由分布，最坏是 `N × per_rank_size`。

通信能否被掩盖，取决于计算/通信比。Prefill 的矩阵大，可以把 all-to-all 和专家 GEMM 重叠；decode 的矩阵小，all-to-all 的延迟更显眼。DeepInfer 用 userbuffer、共享内存、GDR、CUDA Graph 来降低通信的固定开销。

一个简单的带宽估算：官方说跨数据中心部署大约每 8 张 GPU 需要 400 Gbps 互联。这里要分清口径。400 Gbps 是**整机/8 卡共享**，折合每卡 50 Gbps，即约 6.25 GB/s；如果一条 128K 请求的 KV 是 116.7 MB（约 111 MiB），且单请求只能分到 1/8 带宽，传输时间约 `116.7 MB / 6.25 GB/s ≈ 18.7 ms`。如果单请求能独占整机 400 Gbps（50 GB/s），传输时间约 2.3 ms。两个数字差 8 倍，做 TTFT 预算时必须写明是哪种口径。无论哪种，PD 分离都要把 P 和 D 放在相邻的数据中心，并用异步预取掩盖传输。

通信的尾延迟比平均带宽更重要。一个 all-to-all 的平均延迟可能是 100 微秒，但 P99 可能是 1 毫秒；decode 的 step 只有几毫秒，尾延迟会直接吃掉 SLO。生产系统会用“分层 all-to-all”（先组内 NVLink，再跨机组 RDMA）和“通信/计算重叠”来压尾延迟。

## 15.4 负载均衡器

DeepInfer 有四类负载均衡器。

**Prefill Load Balancer**：按上下文长度把请求分成 4 个桶（<16K、16-64K、64-256K、≥256K），根据上一轮各桶的计算配额需求动态分配引擎数量，每个桶有自己的 per-batch 计算配额上限。跨 DP 组时，引擎按剩余配额拉取工作；组内按请求到达时的配额负载选 rank；单卡内按 FIFO 出队，chunk size 由时间预算和二次成本模型求解。

**Decode Load Balancer**：在所有 decode 实例的 GPU 之间均匀路由，同时平衡请求数（决定 batch）和总 KV 长度（决定 attention 成本）。启发式是：短请求送到并发低但 KV 长的 GPU，长请求送到 KV 短的 GPU。官方说这套策略能达到完美均衡离线 benchmark 的约 80%。

**Vision Encoder Load Balancer**：按机器粒度路由图像，选当前处理 patch 数最少的 GPU。patch 数可以直接从图像宽高和 patch size 算出来，不需要先编码。

**Expert-Parallel Load Balancer**：就是 EPLB，负责专家副本和放置。它先用冗余副本摊薄热点专家，再把物理专家贪心放到 rank 上，目标是最小化最慢 rank 的负载；prefill 和 decode 用不同的统计窗口和重平衡间隔。重平衡不是免费的：它要搬运专家权重、可能要双缓冲，并且和 EP size 重定、故障恢复相互影响。第 7.4 节给出两阶段算法，第 24.1 节给出 mini 实现。

这些均衡器共同作用，但它们的决策是分层的：全局路由决定请求去哪个实例，实例内调度决定哪个 step，EPLB 决定专家在哪张卡。任何一层的不均衡都会传导到端到端尾延迟。Decode Load Balancer 的“达到完美均衡的约 80%”是一个相对指标：把请求数和 KV 长度都均衡后的最慢 GPU 负载，和离线最优解的差距；它不是“吞吐达到峰值的 80%”。

分桶的意义在于“不同长度的请求有不同的成本曲线”。一个 200K prompt 的 prefill 可能占用整个引擎几百毫秒，如果和一堆 1K 请求混在一起，短请求会被拖死。分桶后，长请求去专门的引擎，短请求走快速通道；代价是长请求的引擎可能利用率低，需要动态调配额。

## 15.5 PD 与 EPD 分离

PD 分离把 prefill 和 decode 放到不同实例：

```text
request -> Prefill Load Balancer -> P 实例（prefill，产出 KV）
                                      |
                                      v  KV transfer
                                 D 实例（decode，持续生成）
```

好处是 P 和 D 可以独立扩缩容，按各自的最优并行配置部署；prefill 的 TTFT 和 decode 的 TPOT 可以分别优化。代价是 KV 传输、跨实例调度、故障处理。

一次 KV 交接至少包含五步。第一，D 实例先预留足够的 KV 页和 state slot，预留失败就直接拒绝，不能先传后等。第二，P 把每层的 block、block table、token 数、dtype/layout、模型 fingerprint 打包成描述符，按块或按层流水传输；描述符要和 D 的 kernel 布局兼容，否则收到也读不了。第三，传输走 RDMA/GDR 或 TCP，分块、校验、必要时压缩；大 KV 要流水线化，不能等整条序列传完再开始 decode。第四，D 收到并校验后发 ACK，P 才释放源 KV；这个“先预留、再传输、最后切换所有权”的顺序保证任何一步失败时源仍然持有请求，可以重试或回退。第五，超时、重试和幂等：同一个 transfer id 重传不能写出两份 KV，D 要能识别重复块。P/D 的模型版本、KV 格式、租户命名空间也必须匹配；不匹配时宁可拒绝，不要“尽力解码”。

EPD 进一步把 Vision Encoder 也拆出去。图像在 VE 上编码成 embedding，再送给 P。VE 通常是单卡就够，用便宜的 GPU；这样 P/D 不必为视觉编码预留资源。官方说 VE 一般一张 GPU 一个实例就够，建议用更便宜的卡。

最小部署单元：1P + 1D 可部署，但推荐 decode EP64 以上，所以最小高效单元是 64 卡 prefill + 64 卡 decode。小规模部署用 PD 分离可能不划算，因为 KV 传输和调度开销占比高。

PD 分离的收益可以用一个简单模型判断。设 prefill 时间 `T_p`、decode 时间 `T_d`、KV 传输时间 `T_t`。如果不分离，两个阶段共享同一批 GPU，prefill 会阻塞 decode；如果分离，TTFT 约 `T_p + T_t + 排队`，TPOT 约 `T_d`。只有当 `T_t` 远小于 `T_p`，且 prefill/decode 的扩缩容需求差异大时，分离才划算。

## 15.6 并行视角下的故障与重定

自动扩缩容的通用策略（prefill 看非缓存 token、decode 看 running 数、冷启动和冗余配额）在 19.2 节展开，这里只讲与并行相关的部分。

大 EP 部署里，所有 GPU 形成一个通信域，一张卡故障会让整个实例不可用。重启时平台会从当前健康的 GPU 里选一个能整除专家数的 EP size，让剩余 GPU 继续服务，而不是因为一张坏卡报废整组。这要求模型加载、并行组初始化和 EPLB 都能接受动态 EP size，也要求调度器知道实例容量变了。

Decode 实例缩容时，未完成的请求连同 KV 一起转发到其他实例；这在 19.3 节展开。并行层面要注意的是：目标实例的 EP 组、KV 布局和源实例兼容，否则转发的 KV 无法直接使用。EP size 重定和请求转发都牺牲了“实例规格固定”的假设，换来可用性；代价是容量规划要准备多个降级档位。

## 15.7 mini-deepinfer 的并行实验

mini 不启动多进程，而是在一个进程里做逻辑分片：

- `context_parallel_attention`：把 Q/K/V 按序列切分，all-gather K/V，每个 rank 算自己那段 Q，最后拼接；测试与单卡结果一致。
- `expert_parallel_moe`：按专家放置把 token dispatch 到逻辑 rank，算完 combine；测试与单卡结果一致，并统计 dispatch 次数和字节数。
- `tensor_parallel_linear` / `tensor_parallel_mlp`：列并行切输出、行并行切输入，分别用 all-gather 和 all-reduce 还原；测试与单卡结果一致，并统计通信字节。
- `export_sequence_kv` / `import_sequence_kv`：模拟 P -> D 的 KV 交接，验证交接后 decode logits 一致。
- `PrefillTierBalancer` / `DecodeDualPathBalancer`：演示分层路由和“同时考虑并发与 KV 长度”的加权分数。后者是单分数近似，不实现官方那种“短请求走低并发、长请求走低 KV”的两路分流。

运行 `python examples/parallelism.py` 可以看到 CP/EP/TP 的最大误差在 2e-7 量级（float32 累加顺序不同造成的），PD 交接后的 decode 误差为 0。这些实验证明分区和通信的逻辑是对的，但不证明真实 NCCL/HCCL 的性能。视觉侧，`mini_deepinfer/vision.py` 用迷你 ViT 演示了图像 patch 到 LLM token 的编码，可作为 EPD 中 VE 阶段的形状参考。

mini 的 CP 实现有一个教学简化：它先拼接再切分，实际没有跨进程通信；但它保留了“每个 rank 只算自己的 Q、需要完整 K/V”的逻辑。EP 实现保留了 dispatch/combine 的数学和字节统计，但没有模拟网络延迟和拥塞。读者应该把这些函数当成“通信语义的参考实现”，而不是性能模型。

## 15.8 并行的选择清单

选择并行策略时，按顺序问：

1. 模型能否放进单卡？不能，先考虑 TP/EP 切分。
2. 是 MoE 吗？是，优先 EP；不是，TP 或 DP。
3. 上下文是否超长？是，考虑 CP；否则 DP。
4. 互联是否 NVLink/supernode？是，TP/EP 可以更大；跨机则要控制通信量。
5. 负载是 prefill 重还是 decode 重？prefill 重用小 EP/DP，decode 重用 Wide-EP。
6. SLO 是 TTFT 还是 TPOT？TTFT 偏 prefill 扩容，TPOT 偏 decode 扩容。

没有一套参数适合所有场景。DeepInfer 的默认值是针对 V4.1-Flash 和它的生产负载调的，换模型、换硬件、换流量分布都要重新扫。

还有一个“组织约束”：并行越大，故障域越大，运维越难。EP64 意味着 64 张卡是一个通信组，一张卡故障可能让整组不可用；DP 的故障域更小，但模型副本更多、成本更高。选型时要把“能不能运维”算进去，而不是只看理论吞吐。

## 15.9 公开 benchmark 的读法

公开资料给出的并行 benchmark 可以作为“量级参照”，但不能直接当容量规划输入。

decode 侧的配置是 EP64DP64、nextn=3、接受率约 75%。在 256 请求/rank、131K 上下文时，超过 25,000 输出 token/s/GPU，约 100 token/s/user；8 请求时可达 300 token/s/user。这个对比说明两件事：第一，Wide-EP 在低并发时仍能保持可用的单用户速度；第二，高并发时单用户速度下降，但总吞吐上升，这是 batch 摊薄权重的结果。100 token/s/user 已经高于人类阅读速度，所以“多人共享”不会明显破坏体验。

prefill 侧的配置是 EP4DP4、140 token 有界重放。在 4,096 token/rank 时约 6 万非缓存 token/s/GPU，65,536 token/rank 时接近 9 万，单步约 730 毫秒。这个曲线的形状说明：prefill 的吞吐随 chunk 增大而上升，但单步延迟也上升；chunked prefill 的 chunk 大小是在吞吐和 TTFT 之间找平衡，而不是越大越好。

这些数字全部来自 NVIDIA SM100；公开资料没有给出 Ascend 950 的吞吐数字。跨硬件比较时，不要把 EP 规模、kernel 库、通信库和驱动版本当成常数。同一个 EP64 配置，在 NVLink 域更大、RDMA 带宽更高的集群上可能更好；在跨机延迟更高的集群上可能更差。

读 benchmark 时还要看它有没有算“非缓存 token”。prefill 的 6 万到 9 万是“非缓存 token/s/GPU”；如果缓存命中 96%，端到端有效吞吐会高得多。把“缓存后的有效吞吐”当成“prefill 引擎的原始吞吐”，会高估扩容需求；反过来，用“原始吞吐”做容量规划，会低估缓存的价值。

## 15.10 集合通信的 alpha-beta 模型

“通信量”只回答了“传多少字节”，没有回答“传多久”。一个更实用的模型是：

```text
T = alpha + beta × N
```

其中 `alpha` 是每次消息的固定延迟（启动、协议、同步），`beta` 是每字节的传输时间，`N` 是消息字节数。小消息由 `alpha` 主导，大消息由 `beta × N` 主导。这个模型解释了为什么“带宽翻倍”对 decode 的小 all-to-all 帮助有限，而“减少消息数、合并小消息”可能更有效。

对 ring all-reduce，消息大小是 `S`、rank 数是 `R` 时，通信时间近似：

```text
T_allreduce ≈ 2 × (R - 1) × alpha + 2 × (R - 1) / R × S / B
```

第一项是 `2(R-1)` 步同步，第二项是带宽项。all-gather 的带宽项是 `(R-1)/R × S / B`；all-to-all 的带宽项取决于每个 rank 发多少数据，最坏是 `R × S_per_rank / B`。这些公式在附录 B 里也有，但这里的重点是“固定延迟”这一项。

用数字看。假设一次 decode 的 all-to-all 原始数据 3.9 MB、`alpha=100 微秒`、链路 50 GB/s：带宽项约 78 微秒，总时间约 178 微秒；如果链路只有 6.25 GB/s，带宽项约 624 微秒，总时间约 724 微秒。decode 一步可能只有几毫秒，通信占 3% 到 20%，取决于链路口径。如果把“整机 400 Gbps”当成“单卡 400 Gbps”，会低估 8 倍。

通信能否被掩盖，取决于计算/通信比：

```text
overlap_ratio = compute_time / communication_time
```

比值大于 1 时，通信可以藏在计算后面；小于 1 时，通信是瓶颈。Prefill 的矩阵大，`overlap_ratio` 高，适合把 all-to-all 和专家 GEMM 重叠；decode 的矩阵小，`overlap_ratio` 低，固定延迟更显眼。这就是为什么 DeepInfer 在 decode 用 Mega MoE 和 DeepEP 的低延迟模式，而不是简单地把 all-to-all 放到后台。

拓扑也会改变 `alpha` 和 `beta`。同一 NVLink 域的 `alpha` 可能是几微秒，跨机 RDMA 可能是几十到几百微秒；同一个算法在不同拓扑上的最优参数不同。NCCL 的 `NCCL_ALGO`、`NCCL_PROTO`、通道数和 DeepEP 的 low-latency/high-throughput 模式，都是在调这两个参数。读 benchmark 时，要问“它用的是哪种拓扑、哪种算法、消息多大”，否则数字不可比。

## 15.11 拓扑感知的并行选择

并行策略不是“越大越好”，而是“在给定拓扑上，让通信落在最快的域内”。

**优先域内**：TP 和 EP 的 all-reduce/all-to-all 尽量放在 NVLink 域内；跨机只放数据并行和 PD 的 KV 传输。一个跨机 TP=16 的配置，通信开销可能吃掉大部分算力收益。

**层次化**：如果 EP 必须跨机，先用组内 all-to-all，再跨组；把大消息拆成“组内聚合 + 组间交换”，减少跨机次数。DeepEP 的 hierarchical 模式就是这种思路。

**故障域匹配**：EP64 是一个 64 卡的通信域，一张卡故障可能让整组不可用；DP 的故障域更小，但副本更多。选型时要把“故障域大小”和“恢复时间”放在吞吐旁边一起看。第 46 章会把容量缓冲和故障域展开。

**带宽预算**：给每个并行维度分配带宽预算。TP 每层都要通信，预算要最紧；EP 只在 MoE 层通信，但消息更碎；CP 的 all-gather 随序列长度增长；PD 的 KV 传输是一次性大消息。把四个预算加在一起，不能超过互联能力。

一个实用的检查顺序是：先确认模型能否放进单卡；不能，再决定切专家（EP）还是切矩阵（TP）；如果上下文超长，再考虑 CP；最后用拓扑约束检查每个集合通信是否落在合适的域内。不要先选一个“看起来先进”的并行方案，再为它找硬件。

## 15.12 Wide-EP 的交叉点

15.2 节给出了定性的取舍，这里把它算成一个可以扫点的模型：

```text
t_step(EP) ≈ max(
    (W_expert / EP + W_nonexpert) / BW,
    alltoall_bytes / link_bw + latency(EP)
)
```

`W_expert` 是所有路由专家的权重字节，`W_nonexpert` 是 attention、共享专家、embedding 等每卡都要读的部分。第一项随 EP 增大而下降，但有下界 `W_nonexpert / BW`；第二项随 EP 增大而上升，因为参与的 rank 更多、消息更碎、固定延迟和拥塞更明显。

用一组示意数字：`W_expert=100 GB`、`W_nonexpert=5 GB`、`BW=3 TB/s`、decode 每步 64 个 token、top-6、hidden 5120、BF16，dispatch 加 combine 约 7.8 MB，链路 50 GB/s，固定延迟 100 微秒。EP=8 时，权重项是 `(12.5+5)/3000 ≈ 5.83 ms`；EP=64 时是 `(1.56+5)/3000 ≈ 2.19 ms`；EP=128 时是 `(0.78+5)/3000 ≈ 1.93 ms`；EP=256 时是 `(0.39+5)/3000 ≈ 1.80 ms`。从 8 到 64，权重项降了 3.6 毫秒；从 64 到 128 只降 0.26 毫秒；从 128 到 256 只降 0.13 毫秒。通信项则从 0.26 毫秒（8 rank）涨到 0.36 毫秒（64 rank）甚至更高，因为 rank 数越多，all-to-all 的尾延迟越难压。

交叉点就在“权重项的边际收益 < 通信项的边际成本”处。上例中 64 到 128 的收益还有 0.26 毫秒，如果通信只增加 0.1 毫秒，仍然划算；到 256 时收益只有 0.13 毫秒，通信再增加就可能亏。真实系统还要考虑 EPLB 的负载不均衡：EP 越大，每个 rank 的专家越少，热点专家更容易把某个 rank 拖慢，`moe_lb` 的尾延迟会吃掉理论收益。所以“EP 越大越好”只在权重项主导、通信可重叠、负载均衡良好的区间成立。

这个模型也能解释为什么 prefill 不适合 Wide-EP。Prefill 的 token 多，`W_expert / EP` 仍然重要，但计算项的占比更高，而且每个专家的 GEMM 已经很大，继续切小会让 tensor core 利用率下降；通信则因为消息更大而更贵。Prefill 的最优 EP 通常在个位数，decode 的最优 EP 在几十到上百，这不是两个团队的偏好，而是两条曲线在不同工作点的交点。

做容量规划时，不要只算 `W_expert / EP`。把 `W_nonexpert`、all-to-all 的 `alpha + beta × N`、EPLB 的 `moe_lb` 和 SLO 的 TPOT 上限一起放进模型，扫 EP=8、16、32、64、128，画一条“单步时间 vs EP”的曲线。曲线最低点通常不是一个尖峰，而是一段平台；选平台左侧的值，给流量波动和故障留余量。

## 小结

并行是把单机瓶颈扩展到集群的手段。DP 扩展吞吐，TP 切分矩阵，EP 切分专家，CP 切分上下文。DeepInfer 在 prefill 用小 EP/DP、在 decode 用 Wide-EP，是因为两个阶段的带宽/计算特征不同。PD/EPD 分离让阶段独立扩缩容，代价是 KV 传输和全局调度。mini-deepinfer 用逻辑分片验证了 CP/EP/PD 的数学正确性，但没有真实通信性能。下一章把这些并行机制放回长上下文和 Agent 负载，讨论为什么 1M 上下文不能只靠“加卡”解决，以及前缀缓存、长度分桶和会话状态如何一起决定成本。

## 练习

1. 一个 MoE 有 256 个专家、top-8、hidden 7168、FP8。EP=64 时，每个 rank 持有多少专家？一次 all-to-all 大约传多少字节（按每 token 8 个专家算）？
2. 解释为什么 prefill 不适合 Wide-EP，而 decode 适合。
3. PD 分离后，KV 传输时间如何影响 TTFT？如果传输 114 MB、单请求独占 50 GB/s，传输时间是多少？如果 400 Gbps 由 8 张卡共享、单请求只能分到 1/8，又是多少？
4. 用 15.8 节的清单，为一个“8 卡、非 MoE、32K 上下文”的部署选并行策略，并说明理由。

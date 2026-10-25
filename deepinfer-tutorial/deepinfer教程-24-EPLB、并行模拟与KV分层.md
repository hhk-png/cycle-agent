# 第 24 章 EPLB、并行模拟与 KV 分层

本章导读：本章把 mini-deepinfer 里几个“分布式相关”的模块放在一起：EPLB、CP/EP/TP/PD 的逻辑模拟、KV block store。它们都不启动真实的多进程通信，但都保留了分区、通信和交接的逻辑，可以用来验证正确性和做小规模实验。

## 24.1 EPLB 的两阶段实现

`mini_deepinfer/eplb.py` 的核心是 `rebalance_experts(tokens_per_expert, num_physical_experts, num_local_physical_experts)`。三个参数分别是负载、物理专家总数、**每个 rank 的本地容量**；rank 数由 `num_physical_experts // num_local_physical_experts` 推出来，而不是单独传入。`tokens_per_expert` 可以是一维的 `[num_logical]`，也可以是二维的 `[num_steps, num_logical]`；二维时函数先按 step 求和，把历史窗口压成一维总负载。这一点和 DeepInfer 不同：DeepInfer 会保留多个历史 step，最小化各 step 的最大 rank 负载之和，mini 只保留求和后的一维。

1. `_add_replicas`：给每个逻辑专家一个副本，然后逐个把冗余副本分配给当前“每副本负载”最高的专家；
2. `_place_greedy`：把物理专家按**每副本负载** `loads / counts` 降序排列，逐个放到“放上去后最大 rank 负载最小”的 rank 上；
3. `logical_to_physical`：记录每个逻辑专家的所有物理副本；
4. `rank_experts`：记录每个 rank 持有的物理专家列表。

`imbalance(loads, plan)` 计算 `max(rank_load) / mean(rank_load)`。`rank_loads` 把每个逻辑专家的负载按副本数均摊，再按 rank 求和。

这个实现和 DeepInfer 的 `eplb/policy.py` 有两处差异：第一，DeepInfer 会保留多个历史 step 的负载向量，最小化各 step 的最大 rank 负载之和；mini 虽然接受二维输入，但会先按 step 求和，只优化一维总负载，丢掉了“不同 step 的峰值可能落在不同 rank”这一层。第二，DeepInfer 会用 `symm_mem` 真正 shuffle 专家权重；mini 只算映射，不搬权重。

`_place_greedy` 的选择键是 `max(rank_loads[rank] + per_replica, np.max(rank_loads))`。它的含义是：把当前专家放到某个 rank 后，全局最大负载会变成多少；选使这个值最小的 rank。如果多个 rank 打平，选 id 最小的，保证确定性。

## 24.2 EPLB 实验

运行 `python examples/eplb.py`，负载是 `[1200, 900, 700, 300, 120, 60, 30, 10]`：

- 不复制的基线 `moe_lb` 约 1.87；
- EP=2、4 个冗余专家时约 1.01；
- EP=4、8 个冗余专家时约 1.08。

为什么 EP=4 比 EP=2 差一点？因为每个 rank 的容量更小，贪心放置的自由度更低。这不是 bug，而是“更多 rank、更少每 rank 专家”的固有 trade-off。生产调参时要在 EP 规模、冗余专家数、放置自由度之间找平衡。

测试 `test_eplb_balances_skewed_load` 验证 EPLB 的 imbalance 小于基线且小于 1.2；`test_eplb_maps_are_consistent` 验证映射一致；`test_rank_loads_sum_to_total` 验证负载守恒。

负载守恒是一个重要的不变量：每个逻辑专家的负载被它的副本均摊，所有 rank 的负载之和应该等于总负载。如果复制或放置时漏掉一个副本，守恒就会破坏，表现为“总负载变少”或“某个 rank 负载异常低”。测试用这个不变量抓住映射错误。

## 24.3 CP 的逻辑模拟

`context_parallel_attention(q, k, v, mask, cp_size)` 做三件事：

1. 把 Q/K/V 按序列维度切成 `cp_size` 份；
2. all-gather K/V，得到完整的 K/V；
3. 每个 rank 用自己的 Q 分片对完整 K/V 做 attention，最后拼接输出。

这在数学上等价于单卡 attention。测试 `test_context_parallel_attention_matches_single_rank` 比较两者，误差在 1e-6 量级。`moved` 返回 gather 的字节数，用来估算通信量。

这个模拟没有实现 ring attention、online softmax 或通信/计算重叠，但足以说明“CP 切的是序列，attention 需要跨卡 K/V”这个核心。

CP 的关键是 mask。每个 rank 的 Q 分片只对应原序列的一段位置，mask 也要按这段位置生成；如果直接用全局 mask，形状会对不上。mini 在 `context_parallel_attention` 里用 `boundary` 记录每个分片的起始位置，再切 `mask[boundary:boundary+shard_len]`，保证因果性。

## 24.4 EP 的逻辑模拟

`expert_parallel_moe(hidden, gate, experts, top_k, ep_size, placement)`：

1. 算 gate 和 top-k；
2. 按 `placement[expert]` 把 `(token, expert, weight)` 放进对应 rank 的 inbox；
3. 每个 rank 对自己收到的 token 跑本地专家；
4. 按权重累加回原 token。

返回 `EPStats`，包含 token 数、dispatch 次数、字节数、每个 rank 的 token 数。测试用单卡参考实现比较，误差在 1e-6 量级。

这个模拟没有 all-to-all 的延迟、没有负载不均衡的尾延迟、没有 Mega MoE 融合，但它验证了路由和 combine 的数学。`rank_tokens` 可以用来观察专家热点：如果某个 rank 收到特别多的 token，实际系统就会出现 all-to-all 不均衡。

EP 模拟的 `bytes_sent` 是 `dispatched × hidden × dtype × 2`，乘 2 是因为 dispatch 和 combine 各传一次。这个数字可以和真实网络带宽对比，估算通信时间；但它没有算协议头、拥塞和尾延迟，所以只能当下界。

## 24.5 PD 的 KV 交接

`export_sequence_kv(cache, seq)` 把序列的所有层 K/V gather 成 `[L, T, H, D]`，连同 `num_tokens` 和 `block_size` 打包。`import_sequence_kv(cache, seq, payload)` 在目标 cache 里分配页，把 K/V 逐层逐 token 写进去，设置 `num_computed_tokens`。

测试 `test_kv_export_import_preserves_decode` 验证：

- 源和目标的每层 K/V 完全一致；
- 交接后 decode 的 logits 在 1e-6 容差内一致；当前示例的实测误差是 0.0，但测试不应假设永远逐位为 0。

这个模拟没有网络传输、没有压缩、没有异步，但它验证了“P 产出的 KV 可以被 D 直接使用”这个前提。生产系统还要处理格式转换、block 对齐、传输失败和重试。

PD 交接的“完整状态”不只是 K/V。要让 decode 完全等价，还需要传：序列的 token ids、`num_computed_tokens`、采样参数、RNG 状态、停止条件、结构化输出 matcher 状态、MTP/draft 状态。mini 只传 K/V 和长度，因为它的测试只比较 decode logits；生产系统必须把其余状态一起迁移。

## 24.6 KVBlockStore

`KVBlockStore` 是一个本地目录的 content-addressed store：

- `put(key, array)` 把数组存成 `.npy`，记录时间戳；
- `get(key)` 检查 TTL，过期就删除并返回 None；命中时把条目移到 LRU 尾部；
- `_evict` 在超过容量时删除最久未使用的条目；
- `stats` 返回 entries、hits、misses、hit_rate、writes、evictions。

测试覆盖 roundtrip、TTL、LRU、clear。运行 `python examples/kv_store.py` 可以看到容量 3、TTL 10 秒、LRU 淘汰 1 个条目，以及 TTL 过期后 miss 的行为。

这个 store 对应 DeepInfer 的 3FS KV cache 的接口，但没有分布式、EC、GC Worker、GDR。它的价值是让你理解“缓存命中不是免费的，需要 TTL、LRU 和容量管理”。

## 24.7 负载均衡器

`PrefillTierBalancer` 按上下文长度分桶（默认 16K/64K/256K），按桶的请求数分配引擎配额。`DecodeDualPathBalancer` 用 `count × (1 + length/4096) + kv_len/8192` 的加权分数给请求选 rank。它是单分数近似：请求越长，并发项权重越大，长请求会更强烈地避开高并发 rank，但它没有实现生产版“短请求按 count、长请求按 kv_len”的两条独立路径。这样写是为了让读者能在一行公式里看到两个维度的权衡，而不是假装复刻了官方启发式。

这些实现是教学版，不是 DeepInfer 的生产算法。生产版会用量化后的配额、实时负载、滑动窗口和 all-reduce MIN 来动态调整；mini 只用当前请求列表做一次性分配。

`PrefillTierBalancer.assign` 的返回值是一个“每个请求去哪个引擎”的列表。它只给非空桶分配配额，按桶内请求数比例取整，再把总数调回 `num_engines`。如果引擎数少于非空桶数，部分桶的配额会是 0；这些桶的请求不再集中到 0 号引擎，而是在全部引擎上轮转。配额大于 0 的桶则在各自的连续区间内轮转。这样无论引擎数是多少，返回值都落在 `[0, num_engines)` 内，且不会出现“1 号引擎闲置、0 号引擎过载”的退化。生产版还会按实时负载、滑动窗口和 all-reduce MIN 动态调整，而不是只看当前请求列表。

## 24.8 这些模拟能证明什么

它们能证明：

- CP 的分片和 all-gather 不改变数学结果；
- EP 的 dispatch/combine 不改变数学结果；
- TP 的列并行 all-gather 和行并行 all-reduce 不改变数学结果；
- PD 的 KV 交接可以让 decode 继续；
- EPLB 的副本分配能降低不均衡；
- KV store 的 TTL/LRU 行为符合预期。

它们不能证明：

- 真实 NCCL/HCCL 的带宽和延迟；
- 大规模 EP 的 all-to-all 尾延迟；
- 3FS 的吞吐和故障恢复；
- 动态负载下的路由效果。

第 27 章会讲怎么把这些模拟替换成真实通信和存储。

## 24.9 从模拟到真实系统的映射

把 mini 的函数映射到生产组件，可以这样看：

- `context_parallel_attention` -> `parallel_state` + `communication_op.all_gather` + varlen attention kernel；
- `expert_parallel_moe` -> DeepEP dispatch/combine + grouped GEMM + Mega MoE；
- `tensor_parallel_linear` / `tensor_parallel_mlp` -> 列并行/行并行线性层 + all-gather/all-reduce；
- `export_sequence_kv`/`import_sequence_kv` -> PD 的 KV 传输、格式转换、重试；
- `rebalance_experts` -> EPLB policy + 权重 shuffle；
- `KVBlockStore` -> 3FS 客户端 + metadata service + GC；
- `PrefillTierBalancer`/`DecodeDualPathBalancer` -> 全局负载均衡器。

映射的原则是“先对齐语义，再替换实现”。mini 的函数签名和返回值就是语义；生产实现可以换语言、换通信库、换存储，但输入输出和不变量应该保持一致。

## 24.10 从模拟到真实通信的验收

mini 的模拟证明“数学和状态管理是对的”，但不能证明“在真实集群上能跑”。把模拟替换成 NCCL/HCCL/DeepEP 时，至少要过五道验收。

**数值等价**：真实 all-gather/all-to-all 后的结果和单卡参考实现一致，误差在 dtype 容差内。不要只比较 loss 或输出文本；逐层比较 K/V、logits 和梯度，才能定位是切分、通信还是 kernel 的问题。

**通信量**：记录每次集合通信的字节数、消息数、rank 数。mini 的 `EPStats.bytes_sent` 是下界，真实实现还要加协议头、padding、重传和拥塞。把实测字节数和理论值对比，差距大说明有隐藏的拷贝或重复发送。

**延迟与尾延迟**：平均延迟之外，必须看 P95/P99。decode 的 step 只有几毫秒，all-to-all 的尾延迟可能直接破坏 SLO。用 nsys/NCCL trace 看通信和计算有没有重叠，重叠率低说明流水线没搭好。

**负载不均衡**：真实路由分布比合成负载更偏。用生产 trace 回放，观察每个 rank 的 token 数、专家负载和 `moe_lb`；如果某个 rank 长期是瓶颈，EPLB 的窗口和冗余专家数要重新调。

**故障行为**：模拟一个 rank 超时、一个 GPU 掉卡、一个链路丢包，观察系统是重试、降级还是整组不可用。生产实现需要超时、重试、熔断和 EP size 重定；mini 没有这些，所以不能把“模拟通过”当成“故障可用”。

这五道验收都过了，才能说“并行实现可用”。只过第一道，只能说明数学对；只过第二、三道，只能说明性能接近；只过第四道，只能说明负载均衡有效。推理系统的并行是“正确性 + 性能 + 可靠性”的三重约束，缺一不可。

## 24.11 SWA 有界重放的容量实验

`mini_deepinfer/yoco.py` 把第 6 章的命中判定写成了可测试的函数。它不跑模型，只回答问题：block KV 命中多少、SWA 覆盖多少、需要重放多少 token。

```python
from mini_deepinfer.yoco import plan_encoder_hit, plan_decoder_replay, replay_cost_us

plan = plan_encoder_hit(block_hit_tokens=131072, swa_cached_tokens=0)
# effective_hit_tokens=130944, replay_tokens=128
decoder = plan_decoder_replay(100_000)
# replay_tokens=134, effective_hit_tokens=99866
cost = replay_cost_us(134, layers=20, microseconds_per_token_layer=0.5)
# 1340 us
```

这个例子里，block KV 命中 128K，但本机没有 SWA，于是退回最后一个完整窗口，重放 128 token；decoder 侧只重放 134 token，而不是把 2560 token 全部过 20 层。mini 的 `DEFAULT_KEEP_TOKENS` 是 `128 + 6 = 134`；生产 DeepInfer 的默认 `keep_tokens` 是 140，比最小下界多 6 个 token 的边界余量。按每 token 每层 0.5 微秒的示意成本，134 token × 20 层 ≈ 1.34 毫秒；如果重放 2560 token，就是 25.6 毫秒，差约 19 倍。这个差距说明“有界重放”把重放成本从随上下文增长降到了常数，134 token 只相当于 2560 的约 5%，节省约 95%，而不是“减半”。

实验可以改三个参数：`window` 从 128 改成 256，`backtracking` 从 6 改成 8，`keep_tokens` 跟着调整。观察 `plan_encoder_hit` 的 `effective_hit_tokens` 和 `replay_tokens` 怎么变。如果 `keep_tokens < window + backtracking`，函数会报错，而不是悄悄生成一个不完整的窗口。这个下界是第 6 章练习的答案，也是生产配置必须校验的约束。

这个实验的意义不是算出一个精确时间，而是让你看到“缓存命中”和“可直接 decode”之间的差距。命中 128K 不代表窗口完整；窗口不完整就要重放；重放是常数成本，但不是零成本。容量规划要把这部分算进去，否则会低估 prefill 的尾延迟。

## 24.12 张量并行：mini 里曾经缺失的一块

前几节讲的是 CP、EP 和 PD，但第 15 章还把 TP 列为四种基本并行之一。DeepInfer 不把 TP 作为默认方案，因为它的 decode 主轴是 Wide-EP；但 TP 仍然是稠密层、单机多卡和某些 prefill 场景的常用手段。mini 早期没有 TP 的对照实现，读者只能看公式，不能看数值。`parallel.py` 现在补上了 `tensor_parallel_linear` 和 `tensor_parallel_mlp`。

列并行切的是输出维度。`y = x @ W` 中，`W` 的形状是 `[K, N]`；把 `N` 切成 `tp_size` 份，每个 rank 拿到 `[K, N/tp_size]`，算出一段输出，最后用 all-gather 沿输出维拼接。列并行的好处是每个 rank 只算一部分输出，坏处是输出要被收集；如果下一层是行并行，收集可以和下一层的计算融合，减少一次通信。

行并行切的是输入维度。把 `x` 的 `K` 维和 `W` 的行切成 `tp_size` 份，每个 rank 算一个部分和，最后用 all-reduce 求和。行并行的好处是输出一开始就是完整的形状，坏处是每个 rank 都要做一次 all-reduce。一个 SwiGLU MLP 通常组合两种方式：`w1` 和 `w3` 用列并行，激活后 `w2` 用行并行，整个 block 只需要一次 all-reduce。

`examples/parallelism.py` 现在会打印 TP=1/2/4 的列并行、行并行和 MLP 误差。一次运行的最大误差在 `1e-7` 量级，TP=2 的列并行通信量是 1152 字节，行并行是 2304 字节，TP=4 分别是 3456 和 6912 字节。这些数字来自小矩阵，不是性能 benchmark；它们说明“切分不改变数学，但通信量随 TP 和输出大小增长”。

TP 和 EP 的取舍可以用一句话概括：TP 切的是同一个矩阵，通信发生在每一层；EP 切的是专家，通信只发生在 MoE 层，但 all-to-all 的 rank 数更多。稠密模型没有专家，TP 是自然选择；MoE 模型的专家之间没有依赖，EP 更自然。DeepInfer 在 decode 选择 Wide-EP，是因为它把“单卡权重流量”看得比“all-to-all 固定延迟”更重要；如果互联很慢或 batch 很小，结论可能反过来。

mini 的 TP 模拟没有多进程、没有 NCCL、没有真实的 all-reduce 延迟。它只验证分区和集合通信的数学。要用它做性能判断，必须把 `communicated_bytes` 和真实链路的带宽、延迟、拥塞一起算；否则“TP 越大越快”是一个没有依据的结论。

## 小结

mini 的分布式模块是“逻辑正确、性能留白”的模拟。EPLB 用两阶段贪心把不均衡从 1.87 降到 1.01；CP/EP 用分片和 all-gather/dispatch 验证数值等价；PD 用 KV 导出/导入验证交接；KVBlockStore 用 TTL/LRU 验证缓存生命周期。它们不是生产实现，但足以让你在没有集群的情况下理解这些机制的数学和状态管理。

## 练习

1. 在 EPLB 例子里，如果把冗余专家从 4 增到 8，imbalance 会一定下降吗？为什么？
2. CP 的 all-gather 通信量随 `cp_size` 怎么变化？如果序列长度是 32K、KV 是 1 GB，cp=8 时 gather 多少字节？
3. PD 交接时，除了 K/V，还需要传哪些状态才能让 decode 完全等价？mini 漏了哪些？
4. 用 24.9 节的映射，选一个 mini 函数，写出它在生产系统中的三个替代组件和各自的接口。

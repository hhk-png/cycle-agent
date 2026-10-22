# 第 27 章 从玩具到生产

本章导读：mini-deepinfer 证明了机制，但没有证明生产可用。本章给出一张差距清单，按“硬件、模型、并行、存储、服务、运维”六层拆解，并给出一条从 toy 到生产的迁移路线。读完本章，你应该知道下一步该补什么、先补什么、哪些可以直接复用。

## 27.1 差距清单

**硬件层**：

- mini 用 NumPy CPU，生产用 SM100/Ascend 950。
- 没有 FlashMLA、DeepGEMM、DeepEP、TileSelect、TileLang kernel。
- 没有 FP8/FP4、Tensor Core、TMA、NVLink、RDMA。
- 没有 CUDA Graph、stream、event、pinned memory。

**模型层**：

- mini 是 2 层 90K 参数，生产是 552B + 196B。
- 没有 MLA/CSA2/Engram/mHC/ViT 的完整实现；`v4lite.py` 只有结构实验室。
- 有单文件权重加载（`TinyTransformer.from_file`）和 manifest 校验，但没有分片加载、多卡放置和生产级量化；`quantization.py` 是参考实验室，未接入生成。
- 没有 1M 上下文和 128 token block 对齐；mini 默认 16 token block、512 最大长度。

**并行层**：

- mini 是单进程逻辑分片，生产是多进程 NCCL/HCCL。
- 没有 DP 投票、Wide-EP、CP ring、PD 传输。
- 没有 EPLB 权重 shuffle、Mega MoE、DeepEP。
- 没有全局负载均衡和自动扩缩容。

**存储层**：

- mini 是本地 `.npy` KV store，生产是 3FS + EC + GC。
- 没有跨机目录、GDR swap-in、TTL 分层。
- 没有 100 PiB 容量和 40 GB/s 读带宽。

**服务层**：

- mini 是单锁 HTTP server，生产是多副本网关 + 队列 + 鉴权 + 限流。
- `recipe.py` 支持 OpenAI/Anthropic/Responses 风格的消息归一化，但没有完整的工具调用执行和多模态通路；`gateway.py` 有鉴权、配额、前缀亲和和 drain 的独立模型，但没有接进 `server.py`；`server.py` 自己有一个最小的 `/admin/drain` 和 `grace_s` 等待，二者还没有合并成一条统一的网关 drain。
- 没有生产级 streaming 状态机、背压、超时和重试；mini 的 SSE 是无界队列。

**运维层**：

- mini 有最小指标、`tracing.py` 的请求 span 和 `gateway.py` 的配额模型，但没有告警、自动扩缩容和故障转移。
- 没有 SIGTERM 注册、跨实例请求转发、EP size 重定；server 有最小 `grace_s` drain，但超时后不会强制 abort 并结算。
- 没有生产级安全审计和多租户隔离；配额和鉴权只是网关模型，没有接线。

这张清单不是为了让你“全部补完”，而是为了让你知道“mini 的哪个结论可以信到什么程度”。例如 mini 的前缀缓存测试能证明哈希和 COW 的逻辑，但不能证明 3FS 的延迟；mini 的 EPLB 能证明贪心算法，但不能证明 NVLink 上的权重 shuffle。

## 27.2 先补什么

如果要把 mini 往生产推进，建议按下面的顺序：

**第一步：把模型换成真实的 Transformer。** 用 PyTorch 或 JAX 实现一个 1B 级模型，保留 mini 的引擎接口。先不追求 kernel，先把权重加载、tokenizer、forward、采样跑通。这一步验证“引擎能驱动真实模型”。

**第二步：把 KV cache 换成 GPU paged attention。** 用 vLLM 的 paged attention 或 FlashInfer 作为 kernel，保留 mini 的 block manager 和 scheduler。先做单卡，再做多卡。这一步验证“分页和调度的逻辑在 GPU 上成立”。

**第三步：把调度器换成多进程/异步。** 用 Python 的 multiprocessing 或 Ray 做 worker，用 NCCL/HCCL 做 all-reduce/all-gather。先做 DP，再做 EP。这一步验证“并行组和通信”。

**第四步：做 PD 分离和 KV 传输。** 用 RDMA 或 TCP 传 KV，先做同机，再做跨机。这一步验证“阶段解耦”。

**第五步：做服务层。** 网关、鉴权、限流、流式、多协议。这一步验证“能被真实用户使用”。

**第六步：做运维。** 指标、告警、trace、自动扩缩容、故障转移。这一步验证“能长期运行”。

这个顺序不是唯一的，但符合“先正确、再性能、再规模、再可靠”的工程规律。不要一上来就写 TileLang kernel；没有正确的调度和 KV，kernel 再快也服务不了复杂负载。

一个常见的反例是“先优化 kernel”。团队花三个月把一个 attention kernel 优化了 20%，上线后发现 TTFT 主要被排队和前缀缓存 miss 拖累，端到端只提升了 3%。性能优化要先测瓶颈，再决定投哪里；第 17 章的排查顺序和第 18 章的 profiler 方法就是为此。

## 27.3 哪些可以直接复用

mini 的以下部分可以迁移到真实引擎：

- `BlockManager` 的前缀哈希、引用计数、COW、LRU 逻辑；
- `Scheduler` 的 prefill/decode 切换、chunked prefill、LCFS、抢占策略；
- `speculative.py` 的接受/拒绝、变长验证、逻辑长度回退与后续覆盖；
- `structured_output.py` 的 NFA、mask、trigger/bound、snapshot/restore 思路；
- `sampling.py`、`sampling_params.py`、`sequence.py` 的分布整形、seed 和停止条件语义；
- `eplb.py` 的两阶段贪心；
- `parallel.py` 的分片/交接接口；
- `roofline.py`、`economics.py` 的性能/成本模型；
- `metrics.py` 的指标定义；
- `tracing.py` 的 span 结构、`manifest.py` 的版本绑定、`gateway.py` 的准入/配额接口；
- `llm.py` 的批量生成和流式回调形状；
- `server.py` 的协议形状。

需要重写的是性能路径：kernel、通信、对象池、CUDA Graph、存储。逻辑可以复用，实现不能。

“复用逻辑”不等于“复制代码”。例如 mini 的 `BlockManager` 用 Python 列表和字典，生产实现会用 C++ 的数组和哈希表，但“链式哈希、引用计数、COW”的语义可以沿用。迁移时先写测试，把 mini 的行为固化成断言，再在新实现上跑同一组断言。

## 27.4 性能迁移的检查点

迁移到 GPU 后，按下面的检查点验证：

1. **数值一致性**：同一 prompt，CPU 参考实现和 GPU 实现的 logits 在容差内一致。
2. **分页一致性**：连续 KV 和 paged KV 的 attention 输出一致。
3. **调度一致性**：同样的请求序列，CPU/GPU 的 token 输出一致（贪心）。
4. **批不变性**：不同 batch 组成下输出一致。
5. **前缀缓存**：命中/未命中输出一致。
6. **推测解码**：贪心推测和目标贪心一致。
7. **结构化输出**：mask 后输出合法。
8. **性能**：TTFT/TPOT/吞吐达到目标，P99 可接受。

每一步都要有回归测试，否则性能优化会悄悄改变语义。

检查点的顺序很重要：先数值、再调度、再性能。如果数值不一致，后面的性能数字没有意义；如果调度不一致，数值一致也不能说明端到端正确。

## 27.5 生产化的非技术问题

技术之外，生产化还要解决：

- **模型版本管理**：权重、tokenizer、chat template、grammar 的版本要绑定。
- **容量与成本**：GPU 采购/租赁、存储、网络、电力、机房。
- **合规**：数据保留、日志脱敏、地域、出口管制。
- **安全**：鉴权、限流、注入防护、DoS、供应链。
- **组织**：谁 on-call、谁负责容量、谁批准模型上线。

这些问题往往比 kernel 更难。第 28、30、32、34、36、37 章会分别展开安全、成本、部署、模型版本、产品化和合规。

一个具体的例子是“模型版本绑定”。如果权重更新了，但 tokenizer 或 chat template 没更新，模型可能输出乱码或角色错乱；如果 grammar 版本和模型不匹配，结构化输出可能一直失败。生产系统要把这些版本写进一个 manifest，并在启动时校验。

## 27.6 一个现实的路线图

如果你在一个小团队，用成熟框架做一个推理服务，建议的时间线和 27.2 的“自研引擎”顺序不同：先用框架把模型跑通，再补服务层，最后才按需自研 KV/PD。因为框架已经替你实现了调度和分页，你的瓶颈通常先在网关、配额和可观测性，而不是 kernel。

- 第 1 到 2 周：用 vLLM/SGLang 跑通模型，建立 benchmark 和 SLO。
- 第 3 到 4 周：做网关、鉴权、限流、流式、监控。
- 第 2 到 3 月：做 KV 缓存、前缀复用、PD 分离（如果负载需要）。
- 第 3 到 6 月：做自定义 kernel、低精度、推测解码（如果通用框架不够）。
- 第 6 月以后：做多区域、故障转移、成本优化。

不要从“写一个推理框架”开始，除非你的业务确实需要 DeepInfer 级别的定制。大多数团队用成熟框架 + 良好的服务层就能达到 80% 的效果。

## 27.7 自建还是采用

在写第一行引擎代码之前，先回答三个问题。

第一，你的模型是否被现有框架支持？如果是一个冷门结构，改框架的成本可能低于写引擎。第二，你的性能瓶颈是否在框架层？如果是业务层的排队、鉴权、日志，优化引擎没用。第三，你是否有团队能长期维护 kernel、分布式和存储？如果没有，采用成熟框架 + 服务层是更稳的选择。

自建引擎的合理理由通常有三个：模型结构特殊，通用框架无法高效支持；规模足够大，单位成本优化能覆盖研发成本；有特殊合规/部署要求，必须掌控每一层。缺一个，都要慎重。

## 27.8 迁移中的常见坑

**只迁模型，不迁 tokenizer。** 新模型的 tokenizer 可能不同，special token、BOS/EOS、chat template 都要重新对齐。**只迁 kernel，不迁调度。** GPU kernel 快了，但调度还是单线程，端到端提升有限。**只迁单卡，不迁通信。** 单卡性能很好，多卡 all-to-all 把收益吃掉。**只迁正常路径，不迁故障路径。** OOM、超时、取消、重试在 demo 里看不到，上线后才出现。

避免这些坑的方法是“端到端切片”：每迁一个模块，就用一条真实请求跑通全链路，记录每一步的输入输出和延迟。不要等所有模块都迁完再集成。

## 27.9 生产就绪检查清单

把第 28 章的安全控制和第 33 到 37 章的工程、模型接入、云原生、产品化与合规内容压缩成一张清单，可以用来评估一个推理服务离生产还有多远。

**代码与工程**：配置在启动时校验并打印来源；错误分类且带上下文；结构化日志带 request id；指标覆盖请求、token、延迟、KV、抢占；trace 能定位到 prefill/decode/通信；测试分层并进入 CI；镜像可重复构建、非 root、只读根。

**模型与版本**：manifest 绑定权重、tokenizer、template、grammar、dtype、量化、引擎版本；启动时校验词表、最大长度、dtype、量化、主版本；KV 命名空间绑定 manifest fingerprint；热更新双版本并存，回滚不跨版本读 KV。

**部署与弹性**：探针区分启动/就绪/存活；preStop 调 drain；PDB 保证维护期间有副本；HPA 用队列/KV/SLO 指标；模型缓存是只读共享存储；NetworkPolicy 限制横向流量；GPU/NPU 资源请求等于限制；拓扑约束保证 EP 组在同一互联域。

**安全与合规**：API key/OAuth 鉴权；租户配额和并发限制；KV 租户隔离和释放清零；日志脱敏和保留期限；schema 大小/深度限制；SBOM、镜像签名、依赖锁定；事故响应预案和演练。

**商业与运营**：单位成本模型可复算；定价覆盖成本、冗余和毛利；容量规划用 Little 定律和 benchmark；SLO 和错误预算可测量；销售、支持、合规和研发摊销计入完全成本；回滚和降级方案经过演练。

这张清单不需要一次全部打勾。一个合理的顺序是：先做正确性和可观测性，再做版本和部署，最后做安全和商业化。反过来先做商业化，往往会在一次版本事故或一次合规审查中推倒重来。

## 27.10 从 mini 到 GPU 引擎的接口清单

把 mini 的逻辑迁到 GPU 引擎，最忌讳“照着文件重写一遍”。更稳的做法是先把接口固定下来，让两套实现可以并排跑、逐层对照。一个最小接口集有四个面。

**模型执行面**：`forward(token_ids, positions, kv_cache, block_table, start, end) -> logits`。输入是 token、位置、页表和区间，输出是 logits 或 hidden；它不关心调度、采样和停止条件。mini 的 `TinyTransformer.forward_paged` 就是这个接口的参考实现。GPU 版可以用 PyTorch、Triton 或手写 kernel，只要保持“区间语义”和“KV 写入位置”一致，就能用同一组 golden 输入比较。

**KV 管理面**：`allocate(seq, target_tokens)`、`free(seq)`、`write(layer, seq, start, k, v)`、`gather(layer, seq, end)`、`match_prefix(tokens)`。这五个操作定义了分页、COW 和前缀缓存。GPU 版的 `gather` 不应该真的拼接，而是把页表传给 attention kernel；但接口的输入输出要一致，测试才能复用。mini 的 `PagedKVCache` 和 `BlockManager` 可以当成“语义参考”，不是性能参考。

**调度面**：`add_sequence`、`schedule() -> SchedulerOutput`、`postprocess(output, results)`。调度器不直接碰 GPU，它只决定这一步的 `(seq, start, end, is_prompt)`。GPU 引擎可以把 `schedule()` 下沉到 C++，但输出的结构要保持：哪些序列、每条的 q 区间、是否需要抢占、是否完成。这样 mini 的调度 trace 可以直接和 GPU 引擎的 trace 对照。

**采样面**：`sample(logits, params, rng, mask) -> (token, logprob, top_logprobs)`。它是纯函数，不依赖模型和 KV；GPU 版可以把整个 batch 的采样做成一个 kernel，但输入输出的语义要一致。结构化输出的 mask、推测解码的接受规则、停止条件都在这一层之上。

接口固定后，迁移验收可以分三层。第一层是**数值层**：同一组 token、位置、KV，mini 和 GPU 引擎的 logits 在容差内一致；容差按 dtype 定，FP32 可以严，FP8/FP4 要放宽。第二层是**行为层**：同一组请求和 seed，两边的 token 序列、finish_reason、prefix_cache_hit 一致；如果不一致，先查调度顺序和停止条件，再查数值。第三层是**性能层**：在目标硬件上扫 batch、长度、并发，比较 TTFT/TPOT/goodput；这一层不要求一致，只要求可解释。

迁移时最容易犯的错是“先优化再对齐”。GPU kernel 一上来就融合、量化、改布局，数值和 mini 对不上，就分不清是 bug 还是精度差异。正确的顺序是：先用最朴素的 GPU 实现跑通数值层，再用 profile 找瓶颈，逐项替换 kernel，每换一项都跑一遍行为层回归。这个过程比“重写一遍再调”慢，但它能在每一步知道“是哪一项改动导致了差异”。

## 小结

从 toy 到生产要补六层：硬件、模型、并行、存储、服务、运维。先补正确性，再补性能，再补规模，再补可靠。mini 的逻辑可以复用，性能路径必须重写。生产化还包括版本、成本、合规、安全和组织问题，这些不比 kernel 简单。

## 练习

1. 从差距清单里选三个你团队最可能遇到的，写出补齐它们的第一步。
2. 为什么“先写 kernel 再写调度”通常是错的？举一个反例。
3. 设计一个 GPU 迁移的验收清单，至少包含 8 个检查点。
4. 用 27.7 节的三个问题，判断你当前的项目应该自建还是采用成熟框架，并写出理由。

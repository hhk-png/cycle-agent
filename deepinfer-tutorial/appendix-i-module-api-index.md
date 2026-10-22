# 附录 I mini-deepinfer 模块、API 与测试矩阵

本附录是代码索引，不是教程正文。它回答三个问题：一个模块对外暴露什么、被哪些章节讲到、被哪些测试覆盖。读代码时先在这里找到入口，再回到对应章节看设计动机；改代码时先看测试矩阵，避免“改了一个模块，破坏了另一个模块的不变量”。

## I.1 核心引擎层

**`config.py`**。`EngineConfig` 是运行时参数：`block_size`、`num_blocks`、`max_num_seqs`、`max_num_batched_tokens`、`max_model_len`、`enable_prefix_cache`、`enable_chunked_prefill`、`stop_preempted_request`、推测解码和 KV store 开关。`ModelConfig` 描述模型结构；`ParallelConfig` 描述 DP/CP/EP。`__post_init__` 做交叉校验：`block_size <= max_num_batched_tokens`、`max_model_len <= block_size × num_blocks`、推测窗口能放进一个 block。对应第 12、22 章；交叉校验和非法参数在 `tests/test_config.py` 中逐分支覆盖。

**`errors.py`**。`ConfigError`、`OutOfBlocksError`、`EngineClosedError`、`StructuredOutputError` 等。异常类型是接口的一部分：调用方靠它区分“参数错”“容量不够”“引擎已关闭”。对应第 20、25 章；散落在各测试的 `pytest.raises` 里。

**`sequence.py`**。`Sequence` 持有 prompt/output token、`SamplingParams`、RNG、`block_table`、`num_computed_tokens`、`status`、`finish_reason` 和 metrics；`RequestOutput` 是返回给调用方的快照。状态机包括 WAITING、RUNNING、FINISHED_STOPPED、FINISHED_LENGTH_CAPPED、FINISHED_ABORTED、FINISHED_PREEMPTED、FINISHED_IGNORED、FINISHED_SO_FAILED。对应第 11、12 章；由 `tests/test_engine.py`、`tests/test_sampling.py` 间接覆盖。

**`scheduler.py`**。`Scheduler.add_sequence` 做前缀匹配；`schedule()` 返回 `SchedulerOutput`；`postprocess()` 更新 `num_computed_tokens`、迁移状态、注册前缀块；`_preempt` 释放块并重置序列；`_release_waiting_blocks` 在容量不足时牺牲其他 waiting 序列的进度。核心不变量是 `num_computed_tokens <= seq.length`、页表长度覆盖已计算 token、FINISHED 必须释放块。对应第 13、22 章；测试在 `tests/test_engine.py`（抢占、chunked prefill、前缀缓存、abort）。

**`block_manager.py`**。`Block`、`compute_block_hash`、`BlockManager`。关键方法：`allocate`、`free`、`share`、`match_prefix`、`prefix_hashes`、`register`、`lookup`、`num_free_blocks`（可分配）、`num_resident_blocks`（物理驻留）。`_take_free_block` 从 `ref_count == 0` 的块里按 LRU 选，缓存块可被淘汰。对应第 14、22 章；测试在 `tests/test_block_manager.py`。

**`cache_engine.py`**。`PagedKVCache` 提供 `write_layer`、`gather_layer`、`ensure_capacity`、`_cow_if_shared`、`free_sequence`、`memory_bytes`、`used_bytes`、`occupancy`、`export_sequence_kv`/`import_sequence_kv`。`KVBlockStore` 是本地目录的 content-addressed 风格存储，带 TTL、LRU、hit/miss 统计。对应第 14、22、24 章；`PagedKVCache` 在 `tests/test_block_manager.py`，`KVBlockStore` 在 `tests/test_cache_engine.py`。

**`engine.py`**。`LLMEngine.add_request`、`abort_request`、`step`、`_execute`、`_execute_speculative`、`_sample`、`_append_tokens`、`_collect_unreported`、`stats`。`step` 的调度重试、`_unreported` 终态输出、`computed_end` 夹取是容易读错的三个点。对应第 12、22、23 章；测试在 `tests/test_engine.py`。

**`llm.py`**。`LLM.generate` 和 `generate_chat` 是批量入口，负责 tokenize、建请求、循环 `step`、收集输出和流式回调。对应第 3、25 章；没有专门测试，由 `examples/offline_inference.py` 和 `tests/test_server.py` 间接覆盖。

**`sampling.py` / `sampling_params.py`**。`SamplingParams` 校验温度、top-k/top-p、seed、stop、`so_args`；`sampling.py` 提供 `apply_top_k_top_p`、`distribution`、`sample_token`、`target_probs`。顺序是 mask → 温度 → softmax → top-k → top-p → 重新归一化。对应第 10、11 章；测试在 `tests/test_sampling.py`。

**`speculative.py`**。`generate_drafts`、`expected_accepted_length`、`choose_verify_length`、`verify_drafts`、`SpeculativeStats`。注意 `expected_accepted_length` 不含 bonus，`roofline.expected_accept_length` 含 bonus。对应第 8、23 章；测试在 `tests/test_speculative.py`。

**`structured_output.py`**。`NFA`、`NFAMatcher`、`RegexParser`、`_schema_to_regex`、`EBNFParser`、`_expand_ebnf`、`StructuredOutputMatcher`、`build_matcher`。支持正则、JSON Schema 子集、bounded EBNF、trigger/bound、snapshot/restore。对应第 9、23 章；测试在 `tests/test_structured_output.py`。

**`tokenizer.py`**。字节级词表 264（256 字节 + 8 特殊 token）、`encode`/`decode`、`apply_chat_template`、`StreamingDetokenizer`。特殊 token 只能由模板产生。对应第 21、25 章；测试在 `tests/test_tokenizer.py`。

**`metrics.py`**。`EngineMetrics` 记录请求数、token 数、TTFT/TPOT、推测接受率、抢占、step 吞吐；`summary()` 和 `prometheus()` 输出。KV 容量和驻留不在 metrics 里，在 `engine.stats()`。对应第 19、25 章；由 `tests/test_server.py` 间接覆盖。

## I.2 模型层

**`model/ops.py`**。`RMSNorm`、`RoPE`、`softmax`、`attention`、`causal_mask`、`expand_kv`、`silu`。纯 NumPy，形状和公式一一对应。对应第 21 章和附录 F；由 `tests/test_v4lite.py`、`tests/test_parallel.py` 间接覆盖。

**`model/tiny_transformer.py`**。`TinyTransformer.forward_full` 和 `forward_paged`、`_attention`、`_run`、`from_file`。checkpoint 是 2 层、64 维、4 个 query 头、2 个 KV 头、FFN 128、窗口 64。对应第 21 章；测试在 `tests/test_engine.py`、`tests/test_v4lite.py`。

**`model/training.py`**。`forward_train`、`loss_and_grads`、Adam 更新。手写反向传播，包含 RMSNorm、RoPE、softmax、GQA repeat/sum、SwiGLU、tied embedding。对应第 21 章；没有 pytest（有限差分检查在开发时手动跑）。

**`model/v4lite.py`**。`init_v4lite_weights`、`build_v4lite_model`、`CEDTransformer`、`MHCResidual`、MoE/Engram 前向。随机权重，只验证结构、形状、因果性和路由计数。对应第 5、21 章；测试在 `tests/test_v4lite.py`。

**`quantization.py`**。`quantize_fp8`、`quantize_fp4`、`quantization_error`、`kv_bytes_per_token`。FP8 是对称 int8 参考，FP4 是 MXFP4 风格的 E2M1 码本；不是 IEEE 编码器。对应第 10、21 章；测试在 `tests/test_mechanisms.py`。

**`sparse_attention.py`**。块稀疏 attention、indexer 打分、top-k 选择。`top_k >= num_blocks` 时必须与 dense 一致。对应第 6、21 章；测试在 `tests/test_mechanisms.py`。

**`vision.py`**。迷你 ViT：patchify、patch embedding、位置编码、单层非 causal attention、aligner。对应第 21、38 章；测试在 `tests/test_mechanisms.py`。

## I.3 并行与分布式模拟

**`eplb.py`**。`rebalance_experts(tokens_per_expert, num_physical_experts, num_local_physical_experts)`、`_add_replicas`、`_place_greedy`、`imbalance`、`rank_loads`。对应第 7、24 章；测试在 `tests/test_eplb.py`。

**`parallel.py`**。`context_parallel_attention`、`expert_parallel_moe`、`tensor_parallel_linear`、`tensor_parallel_mlp`、`export_sequence_kv`/`import_sequence_kv`、`PrefillTierBalancer`、`DecodeDualPathBalancer`。全部是逻辑分片，不做真实通信。对应第 15、24 章；测试在 `tests/test_parallel.py`。

**`pipeline.py`**。`StageTimings`、`simulate_zero_overhead`、`ShapeSpecializedRunner`、`DecodePerfCurve`。`ShapeSpecializedRunner` 只是可调用对象缓存，不是真正的 CUDA Graph。对应第 17 章；测试在 `tests/test_pipeline.py`。

**`yoco.py`**。`plan_encoder_hit`、`plan_decoder_replay`、SWA 命中判定与有界重放成本。对应第 6、24 章；测试在 `tests/test_yoco.py`。

## I.4 服务与工程化

**`server.py`**。`EngineServer`、`ThreadingHTTPServer`、`/health`、`/healthz`、`/ready`、`/metrics`、`/v1/models`、`/v1/completions`、`/v1/chat/completions`、`/admin/drain`、`/admin/undrain`、SSE，以及超长 prompt 的 413、drain 和 `OutOfBlocksError` 的 503 映射；drain 用 `_idle` 条件变量维护 `_active` 计数。对应第 25 章；测试在 `tests/test_server.py`。

**`recipe.py`**。`normalize_messages`、多协议消息归一化、流式事件解析、特殊 token 安全。对应第 9、25 章；测试在 `tests/test_recipe.py`。

**`manifest.py`**。`ModelManifest`、文件哈希、fingerprint、兼容性校验、篡改检测。对应第 34 章；测试在 `tests/test_manifest.py`。

**`gateway.py`**。`Gateway`、`TenantQuota`、token bucket、鉴权、请求/token 配额、并发上限、前缀亲和、drain。对应第 35、46 章；测试在 `tests/test_gateway.py`。

**`tracing.py`**。`Tracer`、嵌套 span、百分位汇总、环形缓冲、JSONL 导出。对应第 33 章；测试在 `tests/test_tracing.py`。

**`economics.py`**。`tco_breakdown`、`cost_per_million_tokens`、`revenue_per_million_tokens`、`gross_margin`、`capacity_plan`、`p_d_gpu_split`、`sensitivity`。对应第 30、32 章；测试在 `tests/test_economics.py`。

**`finance.py`**。`npv`、`irr`、`payback_period`、`discounted_payback`、`ltv`、`cac_payback_months`、`price_for_margin`、`committed_contract_value`、`breakeven_utilization`、`scenario_table`、`rule_of_forty`、`terminal_value`、`dcf_value`、`revenue_multiple_value`、`implied_revenue_multiple`、`venture_return`。对应第 47、49 章；测试在 `tests/test_finance.py`。

**`market.py`**。`Segment`、`tam_bottom_up`、`serviceable_market`、`obtainable_market`、`adoption_curve`、`funnel_conversion`、`magic_number`、`market_share`、`sales_capacity`。对应第 48 章；测试在 `tests/test_market.py`。

## I.5 专项负载模块

**`multimodal.py`**：`ImageTokenPlan`、`ImageEmbeddingCache`、`pack_vision_batches`、`plan_epd_split`。第 38 章，测试在 `tests/test_multimodal.py`。

**`reasoning.py`**：`ReasoningBudget`、`ReasoningTracker`、`ThinkingStreamParser`、`plan_reasoning_capacity`、`reasoning_cost_multiplier`。第 39 章，测试在 `tests/test_reasoning.py`。

**`offline_batch.py`**：`BatchItem`、`BatchReport`、`plan_waves`、`run_offline_batch`。第 40 章，测试在 `tests/test_offline_batch.py`。

**`lora.py` / `router.py`**：`LoRAAdapter`、`AdapterRegistry`、`ModelRouter`、`ModelSpec`。第 41 章，测试在 `tests/test_lora.py`、`tests/test_router.py`。

**`embedding.py`**：`HashEmbedder`、`CrossEncoder`、`VectorIndex`、`plan_embedding_batches`、`RerankPipeline`。第 42 章，测试在 `tests/test_embedding.py`。

**`compiler.py`**：`Op`、`Graph`、`constant_fold`、`dead_code_elimination`、`fuse_rmsnorm_matmul`、`fuse_swiglu`、`plan_memory`、`optimize`。第 43 章，测试在 `tests/test_compiler.py`。

**`rl_rollout.py`**：`RolloutConfig`、`PromptGroup`、`GroupPlan`、`PartialRollout`、`WeightVersion`、`KVNamespace`、`plan_groups`、`group_advantages`、`estimate_rollout_cost`、`rollout_throughput`。第 44 章，测试在 `tests/test_rl_rollout.py`。

**`edge.py`**：`EdgeDevice`、`EdgeRequest`、`EdgeRoute`、`MemoryPlan`、`DecodeEstimate`、`plan_memory`、`estimate_decode`、`estimate_prefill_tokens_per_s`、`quantized_weight_bytes`、`energy_per_token`、`thermal_sustained_tokens_per_s`、`route_edge_or_cloud`。第 45 章，测试在 `tests/test_edge.py`。

**`traffic.py`**：`AdmissionDecision`、`Worker`、`TrafficRequest`、`admit`、`route`、`retry_budget`、`circuit_breaker`、`capacity_buffer`、`load_shedding`、`mm1`、`mmc`、`erlang_c`、`hedge_delay_ms`、`region_failover`。第 46 章，测试在 `tests/test_traffic.py`。

## I.6 测试矩阵与未覆盖模块

测试文件到机制的对应关系如下。`test_config.py` 覆盖 `EngineConfig`/`ModelConfig`/`ParallelConfig` 的每个校验分支和 `LLM` 启动检查；`test_metrics.py` 覆盖“最近一个 decode step”的瞬时吞吐口径；`test_engine.py` 覆盖贪心确定性、batch invariance、chunked prefill 一致性、前缀缓存、抢占、abort、结构化输出、推测贪心、流式、指标和长度上限；`test_block_manager.py` 覆盖哈希链、分配/释放/淘汰、前缀匹配、COW、驻留与可分配块；`test_cache_engine.py` 覆盖 KV store 的 roundtrip、TTL、LRU、clear；`test_sampling.py` 覆盖温度/top-k/top-p、greedy、seed、logprob、stop 串、min_tokens、UTF-8；`test_speculative.py` 覆盖期望长度、变长选择、接受率、draft 形状、推测+structured output；`test_structured_output.py` 覆盖正则、NFA、回滚、JSON、EBNF、递归上限、trigger/bound、非法转义；`test_parallel.py` 覆盖 CP/EP/TP 数值等价、PD 交接、负载均衡；`test_eplb.py` 覆盖不均衡改善、映射一致、负载守恒；`test_v4lite.py` 覆盖 MoE/Engram、CED 因果性、mHC 形状；`test_mechanisms.py` 覆盖量化误差、KV 字节、稀疏 attention、ViT；`test_pipeline.py` 覆盖 ZOS 成本模型和形状缓存；`test_yoco.py` 覆盖 SWA 命中与重放；`test_manifest.py` 覆盖哈希、fingerprint、篡改检测；`test_gateway.py` 覆盖限流、配额、drain；`test_tracing.py` 覆盖 span 和百分位；`test_economics.py` 覆盖 TCO、单位成本、容量和敏感性；`test_finance.py` 覆盖 NPV/IRR/LTV/合同/DCF/倍数；`test_market.py` 覆盖 TAM/SAM/SOM、采用曲线、漏斗、销售产能；`test_multimodal.py`、`test_reasoning.py`、`test_offline_batch.py`、`test_lora.py`、`test_router.py`、`test_embedding.py`、`test_compiler.py`、`test_rl_rollout.py`、`test_edge.py`、`test_traffic.py`、`test_recipe.py` 分别覆盖对应专项模块；`test_server.py` 覆盖协议、SSE、413、drain/undrain 和 `/ready`。

没有直接测试文件的模块还有 `errors.py`、`corpus.py`、`cli.py`、`llm.py`、`sampling_params.py`、`sequence.py`。它们不是“不重要”，而是通过引擎、server 或示例间接覆盖。`config.py` 和 `metrics.py` 已经有专门测试（`tests/test_config.py`、`tests/test_metrics.py`）。补测的优先级是：`sequence.py` 的状态机、`llm.py` 的流式回调、`sampling_params.py` 的边界校验、`cli.py` 的环境变量解析。补测时优先断言不变量，而不是复制实现逻辑。

## I.7 怎么用这张索引

读代码时，先按“机制”找到模块，再看它对应的章节和测试。例如你想改前缀缓存，先读 `block_manager.py` 的 `lookup`/`match_prefix`/`share`，再看 `tests/test_block_manager.py` 的三个不变量，最后回到第 14、22 章确认设计意图。改完先跑单文件测试，再跑 `pytest -q` 和 `check_docs.py`；如果改了公开符号，还要更新本附录和 README 的模块列表。

不要把“模块存在”当成“章节讲过”。第 20.3 节已经说过，模块树里有四十多个文件，正文只逐行讲其中一部分；本附录的“对应章节”列告诉你哪些有详细解释，哪些只有接口说明。读一个模块前，先确认它在书里的位置，能少走很多弯路。

# 附录 C API 与配置参考

本附录给出 mini-deepinfer 的公开 API、配置项、命令行、HTTP 端点和性能模型接口。DeepInfer 的真实配置项更多，且随版本变化；请以官方仓库的 `EngineArgs` 和 README 为准。

## C.1 快速 API

```python
from mini_deepinfer import LLM, EngineConfig, SamplingParams

llm = LLM(
    config=EngineConfig(
        block_size=16,
        num_blocks=256,
        max_num_seqs=8,
        max_num_batched_tokens=512,
        max_model_len=256,
    )
)

params = SamplingParams(
    temperature=0.2,
    top_p=0.9,
    max_tokens=32,
    seed=42,
)

texts = llm.generate(
    ["DeepInfer is", "Once upon a time"],
    [params, params],
)
print(texts)
```

`LLM.generate` 返回字符串列表；`return_objects=True` 返回 `RequestOutput` 列表，包含 token ids、logprobs、`first_token_latency`（TTFT）、`e2e_latency`、`token_times`、前缀命中数等。`RequestOutput` 没有单独的 TPOT 字段；TPOT 要用 `(e2e_latency - first_token_latency) / (output_tokens - 1)` 或 `metrics.token_times` 的相邻差值自己算。

流式：

```python
def on_delta(request_id, delta, finished):
    print(delta, end="", flush=True)

llm.generate("DeepInfer is", SamplingParams(max_tokens=16), stream=on_delta)
```

Chat：

```python
messages = [{"role": "user", "content": "Explain prefill."}]
print(llm.generate_chat(messages, SamplingParams(max_tokens=32))[0])
```

## C.2 EngineConfig

`mini_deepinfer.config.EngineConfig` 的主要字段：

- `block_size`：KV 页大小，默认 16。
- `num_blocks`：物理页数，默认 256。
- `max_num_seqs`：单步最大并发序列数，默认 16。
- `max_num_batched_tokens`：单步 token 预算，默认 512。
- `max_model_len`：最大序列长度，默认 512；必须不超过 `block_size × num_blocks`。
- `gpu_memory_utilization`：仅用于兼容命名，CPU 实现不生效。
- `enable_prefix_cache`：是否开启前缀缓存，默认 True。
- `enable_chunked_prefill`：是否允许分块 prefill，默认 True。
- `stop_preempted_request`：抢占时是否直接停止请求，默认 False（重算）。
- `prefill_attempt_interval`：有 running 时，每多少个 decode step 尝试一次 prefill，默认 4。
- `prefill_any_attempt_interval`：饥饿保护间隔，默认 16。
- `enable_speculative`、`speculative_tokens`、`confidence_threshold`：推测解码。
- `kv_store_path`、`kv_store_ttl_s`、`kv_store_capacity`、`enable_kv_store`：KV store 配置（示例/扩展用）。
- `enable_async_schedule`：预留开关，当前主引擎不启用。
- `batch_invariant`：预留开关，mini 默认天然批不变。
- `seed`：引擎级 seed，当前请求 seed 由 `SamplingParams` 决定。
- `log_interval_steps`：每多少个 step 打印一次指标。
- `model_path`、`tokenizer_path`：自定义权重/词表路径。
- `parallel`：`ParallelConfig`，包含 dp/cp/ep 的逻辑规模。

`ModelConfig` 的主要字段：

- `vocab_size`、`d_model`、`n_layers`、`n_heads`、`n_kv_heads`、`head_dim`、`ffn_dim`；
- `max_seq_len`、`rope_theta`、`rms_norm_eps`、`window_size`、`swa_layers`；
- `n_encoder_layers`（CED 实验室）；
- `use_moe`、`n_experts`、`n_shared_experts`、`top_k_experts`、`moe_layers`；
- `use_engram`、`engram_layer`、`engram_buckets`、`engram_ngram`；
- `tie_embeddings`。

注意：`ModelConfig()` 的默认值（3 层、FFN 192、窗口 32）和 `assets/tiny_lm.npz` 的 checkpoint 配置（2 层、FFN 128、窗口 64）不同。加载 checkpoint 时以文件里的 `config_json` 为准；从零构建模型时才用默认值。

`ParallelConfig`：

- `data_parallel_size`：默认 1；
- `context_parallel_size`：默认 1；
- `expert_parallel_size`：默认 1；
- `world_size`：`dp × cp`。EP 是逻辑专家分组，不计入 `world_size`；这与“所有 rank 数”的直觉不同。

## C.3 SamplingParams

- `temperature`：0 表示贪心；小于 `1e-5` 会被钳到 `1e-5` 并关闭 top-p/top-k。
- `top_p`：核采样，必须在 (0, 1]。
- `top_k`：-1 表示不限制。
- `max_tokens`：最大输出 token 数。
- `min_tokens`：最小输出 token 数，之前不检查 EOS/stop。
- `ignore_eos`：忽略 EOS。
- `eos_token_id`、`stop_token_ids`、`stop`：停止条件。
- `seed`：每个序列的随机种子；None 时随机生成。
- `logprobs`、`top_logprobs`：logprob 输出。
- `so_args`：`{"schema_type": "regex"|"json"|"ebnf", "schema": ...}`。
- `so_trigger_token_ids`、`so_bound_token_id`：触发词/边界 token。
- `extra`：自定义参数。

`SamplingParams` 在 `__post_init__` 里会把 `temperature < 1e-5` 归一化为贪心，并设置 `effective_seed`。`copy()` 会保留 seed 和结构化参数；如果你想让两个请求用不同的随机序列，不要复用同一个 `SamplingParams` 对象而不改 seed。

## C.4 引擎 API

```python
engine = LLMEngine(config, model=model, tokenizer=tokenizer)
seq = engine.add_request("prompt", SamplingParams(max_tokens=8))
while engine.has_unfinished_requests():
    outputs = engine.step()
    for output in outputs:
        print(output.request_id, output.text, output.finished)
engine.close()
```

主要方法：

- `add_request(prompt=None, sampling_params=None, request_id=None, prompt_token_ids=None)`：加入请求，返回 `Sequence`。
- `step()`：执行一个调度 step，返回 `RequestOutput` 列表。
- `abort_request(request_id)`：取消请求。
- `has_unfinished_requests()`、`get_num_unfinished_requests()`。
- `stats()`：返回 metrics、scheduler、block manager、KV cache、speculative 的快照。
- `close()`：关闭引擎。

`add_request` 支持字符串 prompt 或 `prompt_token_ids`；如果两者都给，`prompt_token_ids` 优先。`request_id` 不传时用递增 id；重复的 `request_id` 不会自动去重，调用方要保证唯一。

## C.5 性能模型 API

`mini_deepinfer.roofline` 提供：

```python
from mini_deepinfer.roofline import (
    Hardware, gemm_cost, linear_cost, decode_step_cost, attention_cost,
    kv_bytes_per_token, expected_accept_length, speculative_speedup,
    cost_per_million_tokens, ttl_storage_bytes,
)

gpu = Hardware("example", peak_flops=1000e12, memory_bw=3e12)
cost = gemm_cost(8192, 8192, 8192, gpu, input_bytes=1)
print(cost.bound, cost.time_s)
```

主要函数：

- `Hardware(name, peak_flops, memory_bw, sram_bytes=0)`：设备规格，`ridge_point` 是 `peak_flops / memory_bw`。
- `gemm_cost(m, n, k, hw, ...)`：GEMM 的 FLOPs、字节、计算/带宽时间、瓶颈。
- `linear_cost(batch, in_features, out_features, hw, ...)`：带 batch 的线性层。
- `decode_step_cost(active_params, hw, ...)`：decode 单步下界。
- `attention_cost(q_len, kv_len, n_heads, head_dim, hw, flash=True/False, ...)`：attention 的算力/带宽成本。
- `kv_bytes_per_token(...)`：带压缩和 scale 的 KV 字节。
- `expected_accept_length(confidences)`、`speculative_speedup(...)`。
- `cost_per_million_tokens(gpu_hour_cost, output_tokens_per_s, utilization=1.0)`。
- `ttl_storage_bytes(tokens_per_second, ttl_seconds, bytes_per_token)`。

`mini_deepinfer.yoco` 提供 `plan_encoder_hit(block_hit_tokens, swa_cached_tokens, window=128, backtracking=6, block_size=128)`、`plan_decoder_replay(sequence_length, window=128, backtracking=6, keep_tokens=140)` 和 `replay_cost_us(replay_tokens, layers, microseconds_per_token_layer)`。它们返回 `ReplayPlan` 或线性成本，用于第 6、24 章的容量实验。

## C.6 命令行

```bash
python -m mini_deepinfer.cli generate "DeepInfer is" --max-tokens 24 --temperature 0 --stats
python -m mini_deepinfer.cli chat "Explain KV cache." --max-tokens 24
python -m mini_deepinfer.cli bench --batch-size 8 --max-tokens 16 --output assets/bench.json
python -m mini_deepinfer.cli serve --host 127.0.0.1 --port 8000
```

通用参数：`--num-blocks`、`--block-size`、`--max-num-seqs`、`--max-num-batched-tokens`、`--max-model-len`、`--speculative`、`--draft-tokens`、`--confidence-threshold`、`--no-prefix-cache`、`--no-chunked-prefill`。

`generate` 还支持 `--top-p`、`--top-k`、`--seed`、`--so-args`、`--stats`。`--so-args` 接收 JSON 字符串，例如 `'{"schema_type":"json","schema":{"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}}'`。

## C.7 HTTP 端点

- `GET /health`、`GET /healthz`：健康检查；返回 `status`、`draining`、`active_requests`。
- `GET /ready`：就绪检查；draining 时返回 503。
- `GET /metrics`：Prometheus 文本。
- `GET /v1/models`：模型列表。
- `POST /v1/completions`：文本补全。
- `POST /v1/chat/completions`：Chat 补全。
- `POST /admin/drain`、`POST /admin/undrain`：进入/退出 drain；drain 后新请求返回 503。

补全请求示例：

```json
{
  "prompt": "DeepInfer is",
  "max_tokens": 16,
  "temperature": 0,
  "stream": false
}
```

Chat 请求示例：

```json
{
  "messages": [{"role": "user", "content": "Hello"}],
  "max_tokens": 16,
  "temperature": 0
}
```

流式响应是 SSE：每行 `data: {json}`，最后 `data: [DONE]`。

## C.8 错误类型

- `ConfigError`：非法配置，继承 `ValueError`。
- `OutOfBlocksError`：KV 页不足。
- `StructuredOutputError`：文法编译/匹配失败。
- `EngineClosedError`：引擎已关闭。

错误处理的原则是“早失败、可定位”。配置错误在构造时抛出，不等到第一个请求；KV 不足抛出明确异常，不静默降级；文法错误带上 schema 类型和位置，方便定位。

## C.9 DeepInfer 的真实配置入口

如果你要读生产代码，先看：

- `deepinfer/engine/arg_utils.py` 的 `EngineArgs`；
- `deepinfer/config.py` 的 `ModelConfig`、`CacheConfig`、`ParallelConfig`、`SchedulerConfig`；
- `deepinfer/environ.py` 和 `deepinfer/envs` 里的 `DEEPINFER_*` 环境变量；
- README 的 feature details。

常见环境变量：`DEEPINFER_ACCELERATOR`、`DEEPINFER_HF_MODEL_DIR`、`DEEPINFER_USE_NEXTN_DECODE`、`DEEPINFER_ENABLE_MTP_VARLEN`、`DEEPINFER_BATCH_INVARIANT`、`DEEPINFER_YOCO_PREFILL_TRUNCATION_MODE`、`DEEPINFER_YOCO_PREFILL_KEEP_TOKENS`、`DEEPINFER_ZERO_OVERHEAD_SCHEDULING`、`DEEPINFER_XGRAMMAR_CACHE_LIMIT_BYTES`。

这些变量的语义随版本变化，使用前请查对应版本的 README/代码。

## C.10 最小可运行片段

如果你只想验证安装：

```python
from mini_deepinfer import LLM, SamplingParams

llm = LLM()
print(llm.generate("DeepInfer is", SamplingParams(temperature=0, max_tokens=8))[0])
print(llm.engine.stats()["block_manager"])
```

输出应该包含一段续写和 block manager 的统计。如果 import 失败，先确认当前目录是仓库根目录，或者把仓库路径加入 `PYTHONPATH`；如果权重加载失败，确认 `mini_deepinfer/assets/tiny_lm.npz` 存在。

## C.11 工程化 API

`mini_deepinfer.manifest`：

```python
from mini_deepinfer.manifest import ModelManifest

manifest = ModelManifest.from_files(
    model_path, tokenizer_path,
    model_id="mini/tiny-lm",
    engine_version="0.1.0",
    max_model_len=512,
    vocab_size=264,
)
manifest.write("manifest.json")
manifest.assert_compatible(
    engine_version="0.1.0",
    vocab_size=264,
    max_model_len=512,
    dtype="float32",
)
manifest.assert_files(model_path=model_path, tokenizer_path=tokenizer_path)
print(manifest.fingerprint())
```

`ModelManifest` 的主要方法：`from_files`、`from_json`、`read`、`write`、`to_json`、`to_dict`、`fingerprint`、`validate_runtime`、`assert_compatible`、`verify_files`、`assert_files`。`verify_files` 重新计算权重/tokenizer 的 SHA-256，返回不匹配项；它只在传入路径时检查，内存里的模型没有文件可哈希。

`mini_deepinfer.gateway`：

```python
from mini_deepinfer.gateway import Gateway, TenantQuota, WorkerLoad

gateway = Gateway([
    TenantQuota("acme", "key-acme", requests_per_minute=60,
                tokens_per_minute=100_000, max_prompt_tokens=8_192,
                max_concurrent=8),
])
decision = gateway.admit("key-acme", prompt_tokens=1200)
if decision.allowed:
    try:
        ...
    finally:
        gateway.release(decision.tenant_id)
worker = gateway.route(prefix_hash, [WorkerLoad("w0"), WorkerLoad("w1")])
gateway.start_drain()
```

主要方法：`authenticate`、`admit`、`release`、`active`、`route`、`prefix_affinity`、`start_drain`、`stop_drain`、`stats`。`TokenBucket` 可以单独用于自定义限流。

`mini_deepinfer.tracing`：

```python
from mini_deepinfer.tracing import Tracer

tracer = Tracer(max_traces=256)
trace = tracer.start("req-123", tenant="acme")
with tracer.span(trace, "prefill", tokens=2048):
    ...
print(tracer.summary())
tracer.export_jsonl("trace.jsonl")
```

主要方法：`start`、`span`、`finish`、`recent`、`summary`、`export_jsonl`。

`mini_deepinfer.economics`：

```python
from mini_deepinfer.economics import (
    tco_breakdown, cost_per_million_tokens, revenue_per_million_tokens,
    gross_margin, capacity_plan, p_d_gpu_split, sensitivity,
)
```

这些函数都是纯函数，返回 `TCOBreakdown` 或字典，适合在 notebook、示例和测试里复用。

## C.12 部署命令

```bash
# 本地镜像
docker build -f deploy/Dockerfile -t mini-deepinfer:0.1.0 .
docker run --rm -p 8000:8000 mini-deepinfer:0.1.0

# Kubernetes
kubectl apply -f deploy/k8s.yaml
kubectl -n mini-deepinfer rollout status deployment/mini-deepinfer
kubectl -n mini-deepinfer port-forward svc/mini-deepinfer 8080:80
```

部署清单的字段解释、GPU/NPU 改造和不能从示例学到的东西，见 `deploy/README.md` 和第 35 章。

## C.13 工程化环境变量建议

mini 的 CLI 实际读取下面这些环境变量，优先级是“命令行显式参数 > 环境变量 > 代码默认值”：

- `MINI_DEEPINFER_MODEL_DIR`：模型目录；如果目录里有 `tiny_lm.npz` 和 `vocab.json`，CLI 会把它们作为 `model_path`/`tokenizer_path`，容器里的 PVC 挂载就靠它。
- `MINI_DEEPINFER_NUM_BLOCKS`：物理 block 数。
- `MINI_DEEPINFER_BLOCK_SIZE`：每 block 的 token 数。
- `MINI_DEEPINFER_MAX_NUM_SEQS`：单步并发上限。
- `MINI_DEEPINFER_MAX_NUM_BATCHED_TOKENS`：单步 token 预算。
- `MINI_DEEPINFER_MAX_MODEL_LEN`：最大序列长度。

下面这些是生产项目通常会有、但 mini 尚未接线的变量：`MINI_DEEPINFER_LOG_LEVEL`、`MINI_DEEPINFER_MANIFEST`、`MINI_DEEPINFER_DRAIN_TIMEOUT_S`、`MINI_DEEPINFER_TRACE_SAMPLE_RATE`、`MINI_DEEPINFER_KV_NAMESPACE`。环境变量的解析集中在 CLI 的 `_env_int`/`_model_paths_from_env`，不要在代码各处直接 `os.environ.get`；第 33 章会讲配置来源打印和优先级测试。

## C.14 专项负载 API

多模态：

```python
from mini_deepinfer.multimodal import (
    ImageTokenPlan, ImageEmbeddingCache, pack_vision_batches, plan_epd_split,
)

plan = ImageTokenPlan(height=1024, width=1024, patch_size=16, unshuffle=3, pool=2)
print(plan.llm_tokens, plan.kv_bytes)

cache = ImageEmbeddingCache(capacity=64, ttl_s=600)
cache.put(image, embedding, config="pool=2")
cached = cache.get(image, config="pool=2")

batch_plan = pack_vision_batches([121, 480, 960], max_batch_tokens=1024)
split = plan_epd_split(
    request_qps=5, images_per_request=1, tokens_per_image=121,
    ve_tokens_per_second_per_gpu=10_000,
    prefill_tokens_per_second_per_gpu=60_000,
    non_cached_tokens_per_request=4096,
    decode_tokens_per_second_per_gpu=14_000,
    output_tokens_per_request=512,
)
```

推理负载：

```python
from mini_deepinfer.reasoning import (
    ReasoningBudget, ReasoningTracker, ThinkingStreamParser,
    plan_reasoning_capacity, reasoning_cost_multiplier,
)

tracker = ReasoningTracker(ReasoningBudget(max_reasoning_tokens=2048, max_answer_tokens=512))
parser = ThinkingStreamParser()
for event in parser.feed("<thinking>step 1</thinking>The answer"):
    print(event)
```

离线批处理：

```python
from mini_deepinfer.offline_batch import BatchItem, plan_waves, run_offline_batch

items = [BatchItem("r0", "prompt", max_tokens=32, priority=1, deadline_s=60)]
waves = plan_waves(items, max_batch_size=8, max_batched_tokens=512)
report = run_offline_batch(llm, items, waves=waves)
print(report.as_dict())
```

张量并行：

```python
from mini_deepinfer.parallel import tensor_parallel_linear, tensor_parallel_mlp

y, stats = tensor_parallel_linear(x, w, tp_size=4, mode="column")
y_mlp, moved = tensor_parallel_mlp(x, w1, w3, w2, tp_size=4)
```

这些函数都是纯逻辑模拟，不启动多进程；`stats` 里的通信字节是下界，不含协议头、拥塞和重传。

## C.15 训练侧、端侧、流量与财务 API

RL rollout：

```python
from mini_deepinfer.rl_rollout import (
    RolloutConfig, PromptGroup, GroupPlan, PartialRollout,
    WeightVersion, KVNamespace, plan_groups, group_advantages,
    estimate_rollout_cost, rollout_throughput,
)

waves = plan_groups([("p0", 1024)], group_size=8, output_budget=512,
                    max_batch_tokens=16384)
advantages = group_advantages([0.2, 0.8, 0.5, 1.0])
cost = estimate_rollout_cost(
    group_size=8, prompt_tokens=1024, output_tokens=512,
    prefill_tokens_per_s=60_000, decode_tokens_per_s=3000,
    kv_bytes_per_token=890, kv_bandwidth_gbs=1000, gpu_hour_cost=5.57,
)
version = WeightVersion("policy", 100).bump()
namespace = KVNamespace("policy", version.step, "tokenizer-v1")
rollout = PartialRollout("p0", budget=512)
```

端侧：

```python
from mini_deepinfer.edge import (
    EdgeDevice, EdgeRequest, plan_memory, estimate_decode,
    energy_per_token, route_edge_or_cloud,
)

device = EdgeDevice("phone", memory_gb=8, bandwidth_gbs=60,
                    compute_tflops=20, power_w=6)
plan = plan_memory(device, parameters=1_500_000_000, bits=4,
                   context_tokens=4096, kv_bytes_per_token=890)
estimate = estimate_decode(device, active_parameters=1_500_000_000,
                           weight_bits=4, context_tokens=4096,
                           kv_bytes_per_token=890)
decision = route_edge_or_cloud(
    EdgeRequest(200, 300, privacy="public", latency_budget_ms=2000),
    edge=device, edge_tokens_per_s=estimate.tokens_per_s,
)
```

流量：

```python
from mini_deepinfer.traffic import (
    mm1, mmc, admit, Worker, TrafficRequest, route,
    retry_budget, circuit_breaker, capacity_buffer,
)

print(mm1(9, 10))
print(mmc(9, 10, servers=4))
decision = admit(inflight=8, concurrency_limit=8, queue_length=4,
                 max_queue=16, estimated_wait_s=0.4, slo_wait_s=0.8)
worker = route(TrafficRequest("r0", prefix_hash="p0"), [
    Worker("w0", inflight=2, capacity=8),
    Worker("w1", inflight=5, capacity=8),
], policy="least_loaded")
```

财务：

```python
from mini_deepinfer.finance import (
    npv, irr, payback_period, ltv, cac_payback_months,
    price_for_margin, committed_contract_value,
    breakeven_utilization, scenario_table,
)

cashflows = [-820_000, 260_000, 360_000, 420_000]
print(npv(0.10, cashflows), irr(cashflows), payback_period(cashflows))
print(ltv(monthly_revenue_per_customer=1100, gross_margin=0.55,
          monthly_churn=0.02))
print(price_for_margin(0.52, 0.35))
print(committed_contract_value(monthly_commit=10_000, months=12,
                               discount=0.2, overage_rate=0.5,
                               expected_overage_units=1000))
print(scenario_table(cashflows, rate=0.10,
                     revenue_multiplier=0.8, cost_multiplier=1.2))
```

这些模块都不依赖引擎热路径。`rl_rollout.py` 的状态对象是可变的（`PartialRollout`），其余函数是纯函数；`finance.py` 的 `irr` 用二分法，遇到没有符号变化的现金流会显式报错。

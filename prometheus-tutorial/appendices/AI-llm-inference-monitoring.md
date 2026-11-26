# 附录 AI LLM 推理服务监控与成本治理

> 第 30 章给出了 AI/LLM 指标的设计要点，本附录把它做成可落地的监控方案：TTFT/TPOT 等 SLI 的精确定义、vLLM/DCGM 指标、KV cache 与容量的算术、PromQL、SLO/告警、token 成本、多租户与 OTel GenAI 映射，最后用 Mini-Prometheus 复现一条最小链路。指标名以 vLLM V1 / DCGM exporter 的常见命名为例，版本升级时名称可能变化，接入前以目标版本 `/metrics` 为准。

## AI.1 分层与数据源

```text
客户端 → API Gateway → 调度/路由 → 模型服务（vLLM/TGI/Triton/…）→ GPU
             │              │              │                    │
         请求量/延迟      队列/限流       token/TTFT/TPOT      DCGM/节点
```

| 层 | 主要问题 | 数据源 | 典型指标 |
|---|---|---|---|
| Gateway | 谁在用、是否限流、端到端延迟 | 网关/Envoy/自建 | 请求数、状态码、端到端延迟、租户/套餐 |
| 调度/路由 | 排队、模型路由、优先级 | 调度器/网关 | 队列长度、排队时间、路由命中 |
| 模型服务 | 吞吐、TTFT、TPOT、KV cache | vLLM/TGI/Triton `/metrics` | `vllm:*`、`tgi_*`、自定义 |
| GPU/节点 | 利用率、显存、故障 | DCGM exporter、node-exporter | `DCGM_FI_*`、CPU/内存/网络 |
| 成本 | token、单价、毛利 | 计费/账单管道 | token counter、价格表、成本 recording rule |

监控 LLM 不能只看 GPU 利用率：GPU 很忙但 tokens/s 很低，通常说明是内存带宽/批处理/KV cache 瓶颈；GPU 不忙但 TTFT 很高，通常是排队或 prefill 问题。

## AI.2 核心 SLI：TTFT、TPOT/ITL、E2E 与 goodput

### AI.2.1 定义

| 指标 | 定义 | 注意 |
|---|---|---|
| TTFT | 请求开始到**第一个输出 token** 的时间 | 包含排队 + prefill；流式/非流式都适用 |
| TPOT | `(E2E - TTFT) / (output_tokens - 1)` | 每输出 token 的平均时间；输出 ≤1 token 时无定义 |
| ITL | 相邻输出 token 的时间间隔 | 看长尾/卡顿，通常用 histogram |
| E2E | 请求开始到最后一个 token | 受输出长度影响，不能直接跨请求比较 |
| Queue time | 请求进入队列到开始执行 | 与 TTFT 分开看，定位排队还是 prefill |
| Goodput | 满足 TTFT/TPOT 目标的请求数/秒 | 比裸吞吐更接近用户体验 |

流式与非流式的区别：

- 流式：TTFT 是用户真正感知的"开始响应"；TPOT/ITL 决定"打字速度"；
- 非流式：用户只感知 E2E；但仍要拆 TTFT/TPOT 做容量分析；
- 批处理/离线：吞吐优先，SLO 是完成时间窗口，不是交互延迟。

### AI.2.2 SLI 表

| SLI | good / total | 目标示例 | 排除 |
|---|---|---|---|
| 可用性 | 非 5xx 且未超时 / 全部 | 99.5% | 客户端取消、鉴权失败 |
| TTFT | `ttft < 2s` / 全部成功请求 | 99% | 超长 prompt、内容过滤 |
| TPOT | `tpot < 50ms` / 全部成功请求 | 99% | 输出 ≤1 token |
| E2E | `e2e < 30s` / 全部成功请求 | 95% | 超长输出（按 token 分桶） |
| 排队 | `queue_time < 500ms` / 全部 | 99% | 离线批任务 |
| 成功率 | 成功完成 / 尝试 | 99.5% | 用户取消、配额拒绝 |

分母一定要固定：

- 是否包含 4xx（用户错误）？通常可用性只算 5xx/超时；
- 是否包含重试？重试会稀释失败率；
- 是否按 token 长度分桶？长 prompt/长输出天然更慢，混在一起会误导；
- 是否包含流式客户端提前断开？提前断开不应算服务失败，但要有单独指标。

## AI.3 队列、批处理与 KV cache

### AI.3.1 关键指标

以 vLLM 为例（名称随版本可能变化）：

```text
vllm:num_requests_running              # 正在执行的请求
vllm:num_requests_waiting              # 排队请求
vllm:kv_cache_usage_perc               # KV cache 使用率，0～1
vllm:gpu_cache_usage_perc              # 旧名称/别名，含义相同
vllm:num_preemptions_total             # 抢占次数
vllm:prompt_tokens_total               # 输入 token 累计
vllm:generation_tokens_total           # 输出 token 累计
vllm:request_success_total             # 成功请求累计
vllm:time_to_first_token_seconds       # TTFT histogram
vllm:time_per_output_token_seconds     # TPOT histogram
vllm:inter_token_latency_seconds       # ITL histogram
vllm:request_queue_time_seconds        # 排队时间 histogram
vllm:prefix_cache_hits_total           # 前缀缓存命中
vllm:prefix_cache_queries_total        # 前缀缓存查询
```

判断饱和：

| 现象 | 可能原因 | 动作 |
|---|---|---|
| `waiting > 0` 且 KV < 0.9 | `max_num_seqs`/`max_num_batched_tokens` 限制 | 调大批处理或加副本 |
| `waiting > 0` 且 KV ≈ 1 | 显存/KV 不足 | 加 GPU、降上下文、量化、换更小模型 |
| KV > 0.9 且 preemption 上升 | KV 压力 | 限制并发/上下文，或扩容 |
| GPU util 高但 tokens/s 低 | 内存带宽/批处理效率 | 看 batch size、KV、模型并行 |
| TTFT 高但 queue time 低 | prefill/长 prompt | 优化 prefill、分块、缓存 |
| TPOT 高但 TTFT 正常 | decode 阶段受限 | 看 batch、KV、GPU 频率 |

### AI.3.2 KV cache 的算术

每 token 的 KV cache 大小（简化）：

```text
KV bytes/token = 2 × n_layers × n_kv_heads × head_dim × bytes_per_element
```

例：32 层、8 个 KV head（GQA）、head_dim=128、FP16（2 字节）：

```text
2 × 32 × 8 × 128 × 2 = 131,072 bytes = 128 KiB/token
```

8k 上下文的一条请求：

```text
128 KiB × 8192 = 1 GiB
```

80GB GPU、模型权重 40GB、运行时开销 10GB，剩 30GB 给 KV：

```text
30 GiB / 1 GiB ≈ 30 条 8k 并发请求
```

这只是上限估算；实际还要减去 fragmentation、CUDA graph、临时 buffer，并用 `kv_cache_usage_perc` 与 `num_requests_running` 校准。

### AI.3.3 Little's law 与容量

```text
并发请求数 ≈ 到达率 × 平均延迟
```

例：20 req/s、平均 E2E 2s：

```text
并发 ≈ 40
```

如果每条请求平均 1.5k token、KV 128 KiB/token：

```text
40 × 1500 × 128 KiB ≈ 7.3 GiB KV
```

再叠加权重/开销，决定需要几张 GPU。吞吐与延迟是权衡：连续批处理提高吞吐，但排队和 TPOT 会上升；`max_num_seqs`/`max_num_batched_tokens` 是主要旋钮。

## AI.4 GPU 与节点指标

DCGM exporter 常用指标：

```text
DCGM_FI_DEV_GPU_UTIL                 # GPU 利用率（%），只表示有 kernel 在跑
DCGM_FI_DEV_MEM_COPY_UTIL            # 显存带宽利用率
DCGM_FI_DEV_FB_USED / FB_FREE        # 显存使用/空闲（MiB）
DCGM_FI_DEV_POWER_USAGE              # 功耗（W）
DCGM_FI_DEV_SM_CLOCK                 # SM 时钟
DCGM_FI_DEV_TEMP                     # 温度
DCGM_FI_DEV_XID_ERRORS               # 最近一次 XID 错误码
DCGM_FI_DEV_ECC_SBE_VOL_TOTAL        # ECC 单比特错误
DCGM_FI_DEV_ECC_DBE_VOL_TOTAL        # ECC 双比特错误
DCGM_FI_DEV_ROW_REMAP_FAILURE        # 行重映射失败
```

看 GPU 不能只看 `GPU_UTIL`：

- 高 util + 低 tokens/s：可能是 memory-bound、batch 太小、KV 碎片；
- 低 util + 高 TTFT：可能在排队、等网络、等 tokenizer；
- 显存接近满 + preemption：KV 压力；
- XID/ECC/row remap：硬件故障，直接 page/隔离；
- 功耗/温度/时钟：热降频会拖慢 TPOT；
- MIG/多实例：`gpu` 标签要区分 slice，利用率不能简单相加。

节点层还要看：

- CPU（tokenizer/预处理可能 CPU-bound）；
- 内存（大模型加载/页缓存）；
- 网络（多机推理的 RDMA/NVLink/IB）；
- 本地盘（模型加载、日志）；
- Kubernetes 调度失败/重启/OOMKilled。

## AI.5 指标模型、标签与基数

推荐的指标集：

```text
# 请求
llm_requests_total{model,operation,result}                       # counter
llm_request_duration_seconds{model,operation}                    # histogram, E2E
llm_time_to_first_token_seconds{model,operation}                 # histogram
llm_time_per_output_token_seconds{model,operation}               # histogram
llm_queue_time_seconds{model,operation}                          # histogram

# token/吞吐
llm_tokens_total{model,direction}                                # counter, input/output
llm_tokens_per_request{model}                                    # histogram
llm_prefix_cache_hits_total{model}                               # counter
llm_prefix_cache_queries_total{model}                            # counter

# 饱和度
llm_requests_running{model}                                      # gauge
llm_requests_waiting{model}                                      # gauge
llm_kv_cache_usage_ratio{model}                                  # gauge 0～1
llm_preemptions_total{model}                                     # counter

# GPU/节点
DCGM_FI_DEV_GPU_UTIL{gpu,model,hostname}
DCGM_FI_DEV_FB_USED{gpu,model,hostname}
DCGM_FI_DEV_XID_ERRORS{gpu,hostname}

# 成本
llm_token_cost_total{model,tier}                                 # counter, 计价货币
llm_price_per_1m_input{model}                                    # gauge, 每 1M token 单价
llm_price_per_1m_output{model}
```

标签预算：

| 标签 | 取值 | 说明 |
|---|---|---|
| `model` | 10～50 | 模型族/版本，必须可枚举 |
| `operation` | 5 | chat/completion/embedding/rerank |
| `result` | 5 | success/error/timeout/cancelled |
| `direction` | 2 | input/output |
| `tier` | 3～5 | free/pro/enterprise |
| `gpu` | 每节点 8 | 不要用 GPU UUID 做长期标签 |
| `hostname` | 节点数 | 排障用，注意基数 |

禁止：

```text
prompt, response, user_id, session_id, request_id, trace_id,
tenant_id（高基数时）, api_key, ip, full_url, model_output
```

需要追踪关联时用 exemplar（`trace_id`）或日志/追踪，不要进标签。需要单租户分析时用计费/日志管道，或对租户 ID 做哈希+分桶。

## AI.6 直方图桶设计

| 指标 | 桶（秒） | 说明 |
|---|---|---|
| TTFT | 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30 | 覆盖交互式 SLO（如 2s） |
| TPOT | 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5 | 覆盖 20～200ms |
| E2E | 0.1, 0.5, 1, 2, 5, 10, 30, 60, 300 | 长输出会到分钟级 |
| Queue time | 0.01, 0.05, 0.1, 0.5, 1, 5, 30 | 区分排队与执行 |
| Tokens/request | 16, 64, 256, 1024, 4096, 16384, 65536 | 按 4 倍或 2 倍增长 |

原则：

- 桶上界要覆盖 SLO 的 1.5～2 倍；SLO 是 2s，就不要只到 1s；
- 桶不要过密（序列数 × 桶数）；
- 如果后端支持原生直方图，优先原生；Mini 只支持经典桶；
- 变更桶会改变历史可比性，发布时记录变更。

## AI.7 PromQL 查询库

```promql
# TTFT P99（按模型）
histogram_quantile(0.99,
  sum by (le, model) (rate(llm_time_to_first_token_seconds_bucket[5m])))

# TPOT P95
histogram_quantile(0.95,
  sum by (le, model) (rate(llm_time_per_output_token_seconds_bucket[5m])))

# 请求成功率（排除用户取消）
sum by (model) (rate(llm_requests_total{result="success"}[5m]))
/
sum by (model) (rate(llm_requests_total{result=~"success|error|timeout"}[5m]))

# TTFT 好请求比例（<2s）
sum by (model) (rate(llm_time_to_first_token_seconds_bucket{le="2"}[1h]))
/
sum by (model) (rate(llm_time_to_first_token_seconds_count[1h]))

# 排队压力
max by (model) (llm_requests_waiting) > 0

# KV cache 压力
max by (model) (llm_kv_cache_usage_ratio) > 0.9

# 抢占率
sum by (model) (rate(llm_preemptions_total[5m]))
/
sum by (model) (rate(llm_requests_total[5m]))

# 输出 tokens/s
sum by (model) (rate(llm_tokens_total{direction="output"}[5m]))

# 每 1k 输出 token 成本（需要价格表指标）
sum by (model) (rate(llm_token_cost_total[1h]))
/
sum by (model) (rate(llm_tokens_total{direction="output"}[1h])) * 1000

# 前缀缓存命中率
sum by (model) (rate(llm_prefix_cache_hits_total[5m]))
/
sum by (model) (rate(llm_prefix_cache_queries_total[5m]))

# GPU 显存使用率（按节点）
DCGM_FI_DEV_FB_USED / (DCGM_FI_DEV_FB_USED + DCGM_FI_DEV_FB_FREE)

# XID 错误
max by (gpu) (DCGM_FI_DEV_XID_ERRORS) > 0
```

与 vLLM 原生指标对应：

```promql
# TTFT P99
histogram_quantile(0.99,
  sum by (le, model_name) (rate(vllm:time_to_first_token_seconds_bucket[5m])))

# 排队请求
max by (model_name) (vllm:num_requests_waiting)

# KV cache
max by (model_name) (vllm:kv_cache_usage_perc)
```

## AI.8 SLO 与告警

### AI.8.1 SLO 示例

| SLO | 目标 | 窗口 | 说明 |
|---|---|---|---|
| TTFT | 99% < 2s | 28d / 1h burn | 交互式聊天 |
| TPOT | 99% < 50ms | 28d | 流式输出 |
| 成功率 | 99.5% | 28d | 排除用户取消/4xx |
| 排队 | 99% < 500ms | 28d | 在线服务 |
| 成本 | 每 1k 请求 < 预算 | 月 | 业务/财务口径 |

多窗口 burn rate（TTFT）：

```promql
# 1h 窗口燃烧率（SLO 99%）
(
  sum(rate(llm_time_to_first_token_seconds_count[1h]))
  -
  sum(rate(llm_time_to_first_token_seconds_bucket{le="2"}[1h]))
)
/
sum(rate(llm_time_to_first_token_seconds_count[1h]))
/
(1 - 0.99)
```

### AI.8.2 告警分层

| 级别 | 告警 | 条件 | 动作 |
|---|---|---|---|
| Page | TTFT SLO burn | 1h burn > 14.4 且 5m burn > 14.4（阈值按 SLO 调整） | 查队列/KV/prefill |
| Page | 服务不可用 | 成功率 < 95% 持续 5m | 查模型/网关/GPU |
| Page | GPU 硬件 | XID > 0 / ECC DBE / row remap | 隔离节点 |
| Page | 队列积压 | `waiting > 0` 持续 10m 且 KV 高 | 扩容/限流 |
| Ticket | 成本异常 | 每 1k token 成本环比 +30% | 查 prompt/缓存/路由 |
| Ticket | 缓存命中下降 | prefix hit < 基线 50% | 查前缀/路由/版本 |
| Ticket | 容量预测 | 7d 后 GPU 需求 > 80% | 采购/优化 |

Runbook 要点：

- 先看 `waiting` 与 KV：排队还是显存；
- 再看 TTFT vs queue time：排队还是 prefill；
- 看 TPOT 与 GPU util/时钟：decode/热降频；
- 看模型版本/路由：是否刚发布；
- 看 token 长度分布：是否被超长 prompt 打爆；
- 看 XID/ECC：硬件故障；
- 限流/降级/切模型/扩容的决策顺序要预先写死。

## AI.9 成本治理：token 会计与单位经济性

### AI.9.1 token 口径

| 类型 | 说明 | 计费 |
|---|---|---|
| Input tokens | prompt/上下文 | 通常单价较低 |
| Output tokens | 生成内容 | 通常单价较高 |
| Cached input | 前缀缓存命中 | 折扣价，需单独计数 |
| Reasoning tokens | 推理模型内部 token | 可能按 output 计费 |
| Retries | 重试产生的 token | 可能重复计费 |

成本公式（价格按每 1M token 计）：

```text
cost = input_tokens × input_price_per_1m / 1e6
     + output_tokens × output_price_per_1m / 1e6
     + cached_tokens × cached_price_per_1m / 1e6
```

注意：

- 流式请求被客户端取消时，已生成的 token 仍可能计费；
- 批处理/离线折扣与在线价格不同，标签要区分；
- 价格表会变，用配置/指标而不是硬编码在查询里；
- 多币种不要直接相加。

### AI.9.2 记录规则

```yaml
groups:
  - name: llm-cost
    interval: 1m
    rules:
      - record: model:llm_output_tokens:rate5m
        expr: sum by (model) (rate(llm_tokens_total{direction="output"}[5m]))
      - record: model:llm_cost_per_minute
        expr: |
          sum by (model) (rate(llm_token_cost_total[5m])) * 60
      - record: model:llm_cost_per_1k_output_tokens
        expr: |
          sum by (model) (rate(llm_token_cost_total[1h]))
          /
          sum by (model) (rate(llm_tokens_total{direction="output"}[1h]))
          * 1000
```

成本异常排查顺序：

1. 单价变了吗（供应商调价/模型切换）？
2. token 变多了吗（prompt 膨胀/上下文变长/重试）？
3. 缓存命中下降了吗？
4. 路由变了吗（便宜模型被切到贵模型）？
5. 计费口径变了吗（批处理/折扣/币种）？

### AI.9.3 单位经济性

```text
每请求成本 = 平均 input_tokens × 单价 + 平均 output_tokens × 单价
毛利率 = (客单价 - 每请求成本) / 客单价
```

按 `tier`/`model` 聚合，不要按 `tenant_id`；单租户分析走账单/日志。成本告警应是 ticket，不要 page，除非成本失控与可用性同级别。

## AI.10 多租户、公平性与隐私

- **配额**：按 tier 限制 RPM/TPM/并发/上下文长度；配额拒绝率单独监控；
- **公平性**：优先级队列/公平调度，防止单租户占满 KV/批处理；
- **隔离**：共享模型服务时，租户间通过网关限流；强隔离用独立实例/命名空间；
- **标签**：`tenant_id` 不进指标；用 `tier`/`plan`，需要时哈希/分桶；
- **隐私**：prompt/response/用户输入绝不进标签、日志要脱敏；指标只保留可聚合维度；
- **保留**：trace/log 的保留期与合规要求；exemplar 只放 trace ID，不放内容；
- **审计**：谁查询了哪些指标、谁改了配额/路由，要有记录。

## AI.11 OTel GenAI 语义约定映射

OTel GenAI 语义约定仍在演进，常见属性（名称随版本变化）：

```text
gen_ai.operation.name          # chat / text_completion / embeddings
gen_ai.provider.name           # 旧版可能是 gen_ai.system
gen_ai.request.model
gen_ai.response.model
gen_ai.usage.input_tokens
gen_ai.usage.output_tokens
gen_ai.response.finish_reasons
gen_ai.request.max_tokens
gen_ai.request.temperature
gen_ai.request.top_p
```

映射到 Prometheus：

| OTel 属性/指标 | Prometheus 标签/指标 | 注意 |
|---|---|---|
| `gen_ai.operation.name` | `operation` | 有界枚举 |
| `gen_ai.provider.name` | `provider` | 有界；版本差异 |
| `gen_ai.request.model` | `model` | 有界；不要把完整版本号无限扩张 |
| `gen_ai.usage.input_tokens` | `llm_tokens_total{direction="input"}` | counter |
| `gen_ai.usage.output_tokens` | `llm_tokens_total{direction="output"}` | counter |
| `gen_ai.response.finish_reasons` | `result`/`finish_reason` | 有界枚举 |
| trace/span ID | exemplar | 不进标签 |

接入方式：

```text
App (OTel SDK) → Collector（spanmetrics / 自定义 processor）
              → Prometheus remote write / OTLP receiver
```

注意：

- Collector 的 spanmetrics 生成的是延迟直方图，token 用量通常要从 span attributes 或应用指标导出；
- OTLP delta temporality 需要转换；
- 语义约定版本要固定，升级时做映射回归；
- prompt/response 可能含敏感信息，trace 采样与脱敏要单独设计。

## AI.12 用 Mini-Prometheus 复现一条最小链路

### AI.12.1 暴露指标

一个最小 exporter 的 `/metrics`（text format）可以这样写：

```text
# HELP llm_requests_total Total LLM requests.
# TYPE llm_requests_total counter
llm_requests_total{model="chat-7b",operation="chat",result="success"} 1200
llm_requests_total{model="chat-7b",operation="chat",result="error"} 12

# HELP llm_time_to_first_token_seconds Time to first token.
# TYPE llm_time_to_first_token_seconds histogram
llm_time_to_first_token_seconds_bucket{model="chat-7b",le="0.1"} 100
llm_time_to_first_token_seconds_bucket{model="chat-7b",le="0.5"} 600
llm_time_to_first_token_seconds_bucket{model="chat-7b",le="1"} 900
llm_time_to_first_token_seconds_bucket{model="chat-7b",le="2"} 1100
llm_time_to_first_token_seconds_bucket{model="chat-7b",le="+Inf"} 1200
llm_time_to_first_token_seconds_sum{model="chat-7b"} 900
llm_time_to_first_token_seconds_count{model="chat-7b"} 1200

# HELP llm_tokens_total Total tokens.
# TYPE llm_tokens_total counter
llm_tokens_total{model="chat-7b",direction="input"} 500000
llm_tokens_total{model="chat-7b",direction="output"} 250000

# HELP llm_requests_waiting Requests waiting in queue.
# TYPE llm_requests_waiting gauge
llm_requests_waiting{model="chat-7b"} 3

# HELP llm_kv_cache_usage_ratio KV cache usage.
# TYPE llm_kv_cache_usage_ratio gauge
llm_kv_cache_usage_ratio{model="chat-7b"} 0.72
```

### AI.12.2 抓取配置

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: llm
    static_configs:
      - targets: ["127.0.0.1:9101"]
        labels:
          env: lab
```

### AI.12.3 查询

```promql
# TTFT P99
histogram_quantile(0.99,
  sum by (le, model) (rate(llm_time_to_first_token_seconds_bucket[5m])))

# 成功率（排除用户取消）
sum by (model) (rate(llm_requests_total{result="success"}[5m]))
/
sum by (model) (rate(llm_requests_total{result=~"success|error|timeout"}[5m]))

# 队列与 KV
max by (model) (llm_requests_waiting)
max by (model) (llm_kv_cache_usage_ratio)

# 输出 token 速率
sum by (model) (rate(llm_tokens_total{direction="output"}[5m]))
```

Mini 的边界：

- 支持 text/OpenMetrics 解析、Histogram、PromQL、规则/告警、API；
- 不支持原生直方图/OTLP protobuf/GPU 指标采集；
- 生产请用 vLLM/DCGM exporter + Prometheus/Mimir，Mini 只用于理解链路与验证查询。

## AI.13 容量规划算例

假设：

```text
到达率 10 req/s
平均输入 1,000 tokens，输出 500 tokens
TTFT 目标 P99 < 2s，TPOT 目标 P99 < 50ms
模型 7B，FP16 权重约 14GB
GPU 80GB，单卡输出吞吐约 2,000 tokens/s（实测为准）
KV 128 KiB/token，平均上下文 2,000 tokens
max_num_seqs = 64
```

步骤：

1. 输出 token 吞吐：`10 × 500 = 5,000 tokens/s`；
2. GPU 数：`5,000 / 2,000 = 2.5 → 3` 张（预留 30% 余量则 4 张）；
3. 并发：`10 req/s × 平均 E2E`；若 E2E≈3s，则 30 并发；
4. KV：`30 × 2,000 × 128 KiB ≈ 7.3 GiB`；
5. 单卡显存：权重 14GB + KV（分片）+ 开销，30GB 以内可控；
6. 用 `num_requests_waiting`、KV、TTFT/TPOT 实测校准，不要只靠公式。

注意：

- 长上下文/长输出会显著改变 KV 和 TPOT；
- 多卡张量并行会改变吞吐/显存分布；
- 量化/投机解码改变吞吐与质量；
- 批处理大小是吞吐/延迟的旋钮，不是越大越好。

## AI.14 检查表

- [ ] TTFT/TPOT/E2E 的定义与分母写清楚？
- [ ] 排队时间与 prefill 时间分开？
- [ ] 成功/失败/取消/超时分类？
- [ ] `model`/`operation`/`tier` 有界，无 prompt/tenant/trace 标签？
- [ ] TTFT/TPOT/E2E 桶覆盖 SLO？
- [ ] 监控 `waiting`、KV、preemption、GPU XID/ECC？
- [ ] SLO burn rate + 队列/硬件告警分层？
- [ ] token 成本按 input/output/cached/reasoning 分开？
- [ ] 价格表可配置，成本可对账？
- [ ] 多租户用 tier/配额，不用 tenant_id 标签？
- [ ] OTel GenAI 属性版本固定、映射有测试？
- [ ] Runbook 覆盖排队/KV/GPU/模型发布/成本异常？

## AI.15 延伸阅读

- vLLM metrics 文档：`https://docs.vllm.ai/en/latest/design/metrics.html`
- NVIDIA DCGM exporter：`https://github.com/NVIDIA/dcgm-exporter`
- OpenTelemetry GenAI semantic conventions：`https://opentelemetry.io/docs/specs/semconv/gen-ai/`
- 本教程第 4 章（Histogram）、第 25 章（SLO）、第 30 章（未来方向）、第 44 章（exemplar/追踪）。
- 附录 AH（多行业案例）、W（埋点）、R（原生直方图）、AG（OTLP）。

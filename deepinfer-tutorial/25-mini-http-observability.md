# 第 25 章 HTTP 服务与可观测性

本章导读：引擎有了，怎样把它变成一个服务？本章拆开 mini 的 OpenAI 兼容 HTTP server、SSE 流式输出、指标端点和并发模型，并说明它与生产网关的差距。最后给出可观测性的最小集合、排障顺序和从单锁到多副本的演进路线。

## 25.1 为什么 DeepInfer 开源版没有在线服务

DeepInfer 的 README 明确说，这次开源提供的是离线推理，不包含 online-serving wrapper。原因不是“在线服务不重要”，而是生产服务层和内部基础设施耦合太深：鉴权、限流、多租户、全局路由、KV 传输、故障转移、计费，每一项都依赖公司内部的网关和存储。

mini 的 `server.py` 是一个教学扩展：它用标准库的 `ThreadingHTTPServer` 把 `LLM` 包成 OpenAI 兼容的接口，让你看到“引擎 -> 协议”的边界。它不是生产网关，也不应该直接暴露到公网。

这个边界很重要：一个推理引擎的“快”不等于“服务可用”。网关要处理连接复用、超时、重试、背压、鉴权、日志脱敏；引擎只负责“给定请求，生成 token”。把两者混在一起，会让引擎承担它不擅长的职责。

## 25.2 端点

server 支持：

- `GET /health` 和 `/healthz`：返回 `{"status":"ok"}`，用于探活。
- `GET /ready`：就绪检查；draining 时返回 503，供 Kubernetes readinessProbe 摘流量。
- `GET /metrics`：返回 Prometheus 文本，包含请求数、token 数、TTFT/TPOT、推测接受率、抢占次数等。
- `GET /v1/models`：返回模型名，兼容 OpenAI 的模型列表。
- `POST /v1/completions`：文本补全，支持 `prompt`、`max_tokens`、`temperature`、`top_p`、`stop`、`seed`、`stream`。
- `POST /v1/chat/completions`：Chat 补全，支持 `messages`；请求会先经过 `recipe.normalize_messages`，兼容 OpenAI/Anthropic/Responses 风格的内容块，再用 `Tokenizer.apply_chat_template` 编码。
- `POST /admin/drain`、`POST /admin/undrain`：进入/退出 drain 状态；drain 后新请求返回 503，在途请求继续完成。

非流式响应包含 `id`、`object`、`created`、`model`、`choices`、`usage`。Chat 响应把文本放在 `choices[0].message.content`，补全响应放在 `choices[0].text`。这些字段是 OpenAI 协议的最小子集，方便用现成客户端做实验。

`usage` 字段现在直接取 `RequestOutput` 的 `prompt_tokens`/`output_tokens`，不再把返回文本重新编码估算；Chat 路径的模板 token 也计入 `prompt_tokens`。`finish_reason` 由引擎的 `finish_reason` 映射而来：`length` 保留，`structured_output` 映射为 `content_filter`，其余映射为 `stop`；流式响应的最后一个 chunk 会等 worker 返回终态对象后再写，避免把 `length` 硬编码成 `stop`。生产实现还要处理 `tool_calls`、`abort` 和客户端断连后的结算。

server 目前只处理文本补全和文本 Chat，没有 `reasoning_content` 字段，也没有图片输入。第 38 章的多模态成本模块和第 39 章的思考预算/流解析器是独立的库，不是 server 的默认路径；如果要接进 API，需要在请求 schema 里增加图片描述符或思考预算，并在响应里增加 reasoning 通道和对应的 `finish_reason`。

## 25.3 SSE 流式

流式请求返回 `text/event-stream`，每个事件是 `data: {json}\n\n`，最后以 `data: [DONE]\n\n` 结束。实现方式：

1. handler 解析请求，启动一个 worker 线程调用 `LLM.generate(..., stream=callback)`；
2. callback 把 `(delta, finished)` 放进 `queue.Queue`；
3. handler 从队列取事件，拼成 SSE 写回 socket；
4. worker 结束后放入 `None`，handler 写 `[DONE]` 并关闭连接。

这里有一个容易忽略的细节：`BaseHTTPRequestHandler` 默认按 HTTP/1.0 处理，但 mini 又显式发了 `Connection: keep-alive`；流式响应没有 `Content-Length`，客户端无法从头部判断响应何时结束。如果连接一直保持，客户端会一直等。mini 在流结束后设置 `self.close_connection = True`，让客户端收到 EOF。生产实现通常用 HTTP/1.1 chunked 或 HTTP/2，由框架生成分块长度和连接生命周期，不靠应用层手动关连接。

SSE 的另一个细节是背压。如果客户端读得慢，服务端写 socket 会阻塞；如果 handler 阻塞，worker 线程可能继续往队列里塞，内存上涨。生产实现会用有界队列、写超时和丢弃策略；mini 用无界 `queue.Queue`，在慢客户端下可能堆积。

## 25.4 Tokenizer 与 Chat 模板

`Tokenizer.apply_chat_template` 生成：

```text
<|bos|><|user|>[system] ...<|end|><|user|>...<|end|><|assistant|>
```

角色标记由模板插入，用户内容里的 `<|user|>` 仍然是普通字节。这个设计对应 DeepSeek Recipe 的安全边界：控制 token 只能由模板产生，不能由用户文本注入。

`LLM.generate_chat` 把 messages 编码成 token ids，再走普通生成。它不支持工具调用、多模态、reasoning 事件等复杂协议；这些是生产 Recipe 的工作。

模板的细节会影响模型行为。例如 system 消息被编码成 `[system]` 文本，而不是一个独立的 system 角色 token；这和真实模型的训练模板可能不同。教学实现的重点是“角色标记不可注入”，不是“模板和官方完全一致”。

## 25.5 并发模型

mini 的 `EngineServer` 用一把 `threading.Lock` 串行化所有生成请求。这保证了引擎状态（scheduler、KV cache、RNG）不被并发修改，但也意味着吞吐上限是单请求。真实服务会用：

- 一个引擎进程 + 一个请求队列；
- 多个引擎副本 + 全局负载均衡；
- 每个副本内部异步调度；
- 流式响应和背压。

mini 的锁是教学简化，不是性能设计。它的价值是让你看到“引擎不是线程安全的”，并发需要在外层解决。

为什么引擎不是线程安全的？因为 `Scheduler` 的 waiting/running、`PagedKVCache` 的页表、`Sequence` 的 RNG 都是可变状态；多个请求同时调用 `step()` 会交错修改。要在引擎内部支持并发，需要把状态分片、加锁或改成单线程事件循环；这会增加复杂度，不是教学实现的目标。

## 25.6 指标

`EngineMetrics` 记录：

- 请求数、完成数、prompt/output token 数；
- prefill/decode token 数、step 数；
- TTFT 的均值和 P95；TPOT 和 E2E 的均值（当前没有 P95，生产实现应补）；
- 推测步数和接受率（`spec_stats` 里还有 proposed/verified/accepted 原始计数，但没有全部导出到 Prometheus）；
- 抢占次数；
- 最近一个 decode step 的瞬时吞吐 `last_decode_tokens_per_s`，以及累计平均 `output_tokens_per_s`。

KV 容量和占用不在 `EngineMetrics` 里，而在 `LLMEngine.stats()` 返回的 `kv_cache_bytes`、`kv_cache_used_bytes`、`kv_cache_occupancy` 三个字段中；`/metrics` 端点目前只导出 `EngineMetrics.summary()` 的字段，不包含 KV 指标。排障时要么调用 `engine.stats()`，要么把这三个字段接进 Prometheus exporter。容量是预分配上限，占用要看“驻留块”口径：前缀缓存中 `ref_count == 0` 的块仍然占 HBM，不能当成空闲。

`prometheus()` 把 summary 的每个字段输出成 gauge。生产环境应该用 histogram 记录延迟分布，用 counter 记录累计量，并加上标签（模型、实例、优先级）。mini 用 gauge 是为了简单。

指标的设计要避免“只看平均值”。TTFT 的均值可能是 50 ms，但 P99 可能是 2 秒；用户感知的是尾延迟，不是均值。生产仪表盘至少要有 P50/P95/P99，以及按模型、实例、请求类型的分组。

## 25.7 排障顺序

服务变慢时，按下面顺序看：

1. `/metrics` 的 `requests_added` 和 `requests_finished` 是否匹配？不匹配说明请求堆积。
2. `ttft_mean_ms` 和 `tpot_mean_ms` 哪个涨了？TTFT 涨看 prefill/队列，TPOT 涨看 decode/batch/KV。
3. `last_decode_tokens_per_s` 是否下降？这是最近一个 decode step 的瞬时吞吐，适合发现突发回归；`output_tokens_per_s` 是进程启动至今的累计平均值，旧的高速 step 会稀释新问题，不要用它判断“刚刚变慢”。
4. `preemptions` 是否增加？增加说明 KV 不足或 batch 太大。
5. `speculative_acceptance` 是否下降？下降说明 draft 质量或负载分布变了。
6. `/health` 是否正常？不健康先看进程、端口、依赖。

这个顺序是“从外到内”：先看请求是否堆积，再看延迟的哪一段变慢，再看资源，再看算法指标。不要一上来就怀疑模型或 kernel。

## 25.8 安全边界

mini 的 server 没有鉴权、限流、TLS、输入长度硬限制。生产部署至少需要：

- 在网关做鉴权和配额；
- 限制 prompt/output token 数；
- 限制并发和队列长度；
- 对结构化输出 schema 做大小/复杂度限制；
- 不在日志里记录完整 prompt；
- 只监听内网或加 TLS。

这些不是“以后再说”的细节。一个没有长度限制的推理端点，很容易被一个超长 prompt 打满 KV；一个没有 schema 限制的结构化输出端点，很容易被编译缓存打爆内存。

## 25.9 运行示例

```bash
python examples/http_server.py
python -m mini_deepinfer.cli serve --port 8000
```

启动后可以用 curl：

```bash
curl -s http://127.0.0.1:8000/v1/completions \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"DeepInfer is","max_tokens":16,"temperature":0}'
```

`tests/test_server.py` 覆盖健康检查、模型列表、补全、Chat、流式、指标和 400 错误。`examples/http_server.py` 会自己启动服务、发一条请求、打印指标，然后关闭，适合快速验证。

## 25.10 从单锁到多副本

mini 的 server 用一把锁串行化生成，原因是引擎的 scheduler、KV cache、RNG 都不是线程安全的。生产服务不会用一把全局锁，而是用“单引擎进程 + 多副本”：

```text
client -> gateway -> queue -> engine replica 0
                          -> engine replica 1
                          -> engine replica N
```

网关负责鉴权、限流、路由；每个副本内部仍然是单线程/单调度器，但副本之间通过全局负载均衡器分配请求。这样既避免了锁竞争，又能水平扩展。代价是副本之间的 KV 缓存不共享，前缀命中率下降；解决办法是把 KV 放到共享存储（3FS），或者做前缀亲和路由。

另一个选择是“单进程 + 异步引擎”：一个进程内用事件循环/协程管理多个请求，引擎的 step 仍然是串行的，但 I/O（网络、KV 传输）可以异步。DeepInfer 的 ZOS 更激进：CPU 调度和 GPU 执行重叠，但仍在单引擎实例内。多副本和异步不是互斥的，生产系统通常两者都用。

mini 的单锁是一个清晰的起点：先保证状态安全，再考虑并发。把锁换成队列、把单副本换成多副本，是第 27 章“从 toy 到生产”的一部分。

## 25.11 Drain 与优雅退出

生产服务缩容时，不能直接杀掉进程。一个最小的 graceful shutdown 流程是：

1. 从负载均衡器摘除实例，停止接收新请求；
2. 进入 draining 状态，继续处理 running 请求；
3. 等待 running 请求完成，或超时后转发到其他实例；
4. 关闭 server，释放 KV 和 GPU 资源。

mini 实现了一个最小 drain。`EngineServer.start_drain()` 在条件变量 `_idle` 里把 `_draining` 置位；`generate` 在同一个临界区里检查并递增 `_active`，drain 后到达的请求直接抛 `DrainingError`，由 handler 映射为 503；请求结束时在 `finally` 里递减 `_active` 并 `notify_all`。`stop(grace_s)` 先 drain，再等待 `_active == 0` 或超时，最后关闭 listener。`POST /admin/drain` 和 `/admin/undrain` 暴露状态，`GET /health` 返回 `status`、`draining`、`active_requests`，负载均衡器可以据此摘流量。

这还不是完整的生产 drain。它没有把 running 请求转发到其他实例，没有注册 SIGTERM/SIGINT，没有和网关的 `Gateway.start_drain()` 联动，也没有在超时后强制 abort 并结算。生产实现要把这几步接起来：信号 → 停止接新流量 → 等待或转发 → 强制终止 → 释放 KV。这个练习能让你理解“优雅退出”为什么需要引擎、网关和编排系统三方配合。

## 25.12 把 gateway 接到 server

`server.py` 目前只做协议适配，没有鉴权、配额和路由。`gateway.py` 提供了这些能力，但两者还没有接线。接线的最小方式是在 `_Handler.do_POST` 的开头调用 `admit`，在响应结束时 `release`。下面的代码是**伪代码**：`self.app.gateway`、`api_key` 和 `prompt_tokens` 都需要你在 `EngineServer` 里自己接出来，仓库并没有现成的这三个属性。

```python
decision = self.app.gateway.admit(api_key, prompt_tokens)
if not decision.allowed:
    self._send_json(429, {"error": {"message": decision.reason}})
    return
try:
    text = self.app.generate(prompt, params)[0]
finally:
    self.app.gateway.release(decision.tenant_id)
```

还需要三件事才算完整。第一，**请求 id 和 trace**：网关生成 request id，`Tracer.start` 开始一个 trace，`LLMEngine.step` 里加 span；响应头带 `x-request-id`。第二，**前缀亲和**：在请求进入引擎前，用 prompt 的完整 block 哈希作为 `prefix_hash`，让 `Gateway.route` 选一个 worker；单进程 server 没有多 worker，但逻辑可以保留。第三，**drain**：`/admin/drain` 调用 `app.start_drain()`（也可以同时调用 `gateway.start_drain()`），`/health` 在 draining 时返回 `{"status":"draining", ...}`，让负载均衡器摘流量；本实现用 200 + body 表达状态，某些平台要求 503，按负载均衡器的探针语义选择。

接线之后，server 的职责就清楚了：协议解析、SSE、错误码；gateway 的职责是身份、配额、路由和 drain；engine 的职责是生成。三者分开，才能独立测试和替换。生产网关通常还会做 TLS、请求体大小限制、WAF、日志脱敏和审计，这些不应该塞进 `server.py`。

一个常见的错误是“在引擎里做限流”。引擎不知道租户，也不知道全局配额；把限流放进引擎，会让调度器承担它不擅长的职责，而且多副本之间无法共享配额。正确的分层是：网关做粗粒度限流和鉴权，引擎做细粒度准入（例如 prompt 长度、KV 容量），两者都做，但不重复。

网关再往外一层是流量工程：请求去哪个区域、哪个 worker，重试预算多少，熔断器是否打开，故障时哪些区域可以承接。第 46 章给出了排队、路由、重试、熔断和多区域的模型；本章的 server 只负责协议和单副本并发，不承担全局调度。

## 25.13 错误码、finish_reason 与客户端契约

一个推理服务的 API 不只是“成功时返回什么”，还包括“失败时客户端怎么区分”。如果所有错误都返回 500，客户端无法判断该重试、该降级，还是该修改请求。一个最小但够用的映射是：

- 400：请求体不合法、采样参数越界、schema 编译失败、prompt 为空；
- 401/403：缺少或无效的凭证、租户无权访问该模型；
- 404：模型名不存在；
- 413：prompt 加 `max_tokens` 超过 `max_model_len`，或请求体超过网关上限；
- 429：配额、并发或 token/分钟超限；
- 499：客户端主动断开（很多框架用非标准码，或只记日志）；
- 500：引擎内部错误、不可恢复的 kernel 错误；
- 503：正在 drain、过载、依赖不可用，或 KV 分配失败（`OutOfBlocksError`），客户端应退避重试；
- 504：排队或生成超时。

这些码要和 `finish_reason` 分开。HTTP 状态描述“这次调用有没有得到一个完整响应”，`finish_reason` 描述“生成为什么结束”。一个 200 响应可能有 `finish_reason="length"`，表示模型被截断；这不是 HTTP 错误，但客户端不该把它当成“答案完整”。常见的取值是 `stop`（EOS 或 stop 串）、`length`（达到 max_tokens 或 max_model_len）、`aborted`（用户取消）、`structured_output`（文法无法继续）、`content_filter`（内容策略拦截）。推理模型还会多出“思考预算耗尽”和“答案预算耗尽”，第 39 章会展开。

流式响应要保证终止顺序。最后一个 chunk 应该先带 `finish_reason` 和 usage，再发 `data: [DONE]`；如果先发 `[DONE]`，客户端可能来不及记录结束原因。如果生成中途出错，应该发一个 `event: error` 或带 `error` 字段的 chunk，再关闭连接，而不是只发 `[DONE]` 让客户端以为成功。mini 的 server 在流结束时设置 `close_connection=True`，并用终态对象填最后一个 chunk；生产实现还要处理 HTTP/2 的流重置和 gRPC 的 status。

重试语义也要写清楚。`429` 和 `503` 可以重试，但要带 `Retry-After` 或指数退避；`400` 重试没有意义；`500` 要看错误码，KV OOM 可能换实例后成功，kernel 错误则不应该盲目重试。生成类请求不是幂等的：重试会重新计费、可能得到不同输出。网关应该用 `request_id` 做去重，引擎应该记录“这个 id 已经完成/正在进行”，而不是让客户端自己猜。第 33 章的请求追踪和第 46 章的 retry budget 会把这件事展开。

## 小结

mini 的 HTTP server 是一个最小可用的 OpenAI 兼容层：健康检查、指标、补全、Chat、SSE。它用一把锁保证引擎状态安全，牺牲了并发；它没有鉴权、限流、TLS，不能直接上公网。可观测性方面，它提供了请求、token、延迟、推测、抢占的最小指标集。下一章讲测试与验证方法。

## 练习

1. 给 drain 加一个 `grace_s` 超时后的强制 abort：超时后如何标记在途请求、如何结算 usage、如何保证 KV 被释放？需要修改哪些状态？
2. 为什么 SSE 流结束后要关闭连接？如果不关闭，客户端会看到什么？
3. 设计一个限流策略：按租户限制并发和 token/分钟，说明它应该放在网关还是引擎。
4. 比较“单进程异步”和“多副本”两种并发模型，各写一个适合的场景。

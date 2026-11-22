# 第 15 章 Rollout 引擎：vLLM、SGLang、HF 与 server 模式

## 15.1 Rollout 为什么不是“调用 generate”

在单机脚本里，rollout 就是 `model.generate(...)`。在 RL 训练中，它要满足：

- 高吞吐：continuous batching、PagedAttention、CUDA graph；
- 变长输入：不同 prompt、不同响应长度；
- 多采样：同一 prompt 生成 `n` 条；
- 权重热更新：每轮训练后同步新权重；
- 显存可控：与训练共享 GPU 时能 sleep/wake；
- 可中断：异步/partial rollout 需要保存进行中的请求；
- 可观测：记录 token 数、KV cache、前缀命中。

verl 支持多种 rollout 后端：

| 后端 | 特点 | 适用 |
|---|---|---|
| vLLM | continuous batching、PagedAttention、成熟 | 通用首选 |
| SGLang | RadixAttention、前缀缓存、结构化输出 | 多轮/前缀复用 |
| TensorRT-LLM | 极致推理性能 | 特定硬件/模型 |
| HF Transformers | 简单、兼容性好 | 调试、单卡 |
| Naive | 最简实现 | 教学/兜底 |

v0.9.1 的 `pyproject.toml` 使用 vLLM 0.24.0、SGLang 0.5.12；安装文档基线是 vLLM 0.18+。版本升级很快，升级前先看 release note 与 verl 的兼容矩阵。

## 15.2 Rollout 的两种模式

### 进程内模式

rollout 引擎跑在 actor worker 内部（colocate），直接持有 GPU。权重同步是内存拷贝或 NCCL。优点是延迟低；缺点是显存竞争、无法独立扩缩。

### Server 模式

rollout 作为独立服务（vLLM server / SGLang server），通过 HTTP/gRPC 接收请求。训练侧只发 prompt，不直接管理 KV cache。优点是：

- 可以独立部署、扩缩；
- 训练与生成解耦；
- 支持异步/流式；
- 多训练任务共享。

缺点是：

- 网络开销；
- 权重同步跨进程；
- 需要请求路由与超时处理。

v0.9.1 的 `verl/workers/rollout/llm_server.py` 提供 `LLMServerClient` / `LLMServerManager`；vLLM/SGLang 各自有 async server 实现。

## 15.3 vLLM 集成

关键配置：

```yaml
actor_rollout_ref:
  rollout:
    name: vllm
    mode: async
    tensor_model_parallel_size: 2
    gpu_memory_utilization: 0.5
    max_num_batched_tokens: 8192
    max_num_seqs: 256
    enable_chunked_prefill: true
    enable_prefix_caching: true
    free_cache_engine: true
    enforce_eager: false
    dtype: bfloat16
```

### 显存

`gpu_memory_utilization` 是 vLLM 预分配的显存比例。它包含：

- 模型权重；
- KV cache；
- CUDA graph；
- 临时 buffer。

colocate 时不要设太高，否则训练侧 OOM。常见起点：0.4～0.5。

### `free_cache_engine`

训练时释放 KV cache，生成时重新分配。它降低峰值显存，但增加切换开销。

### chunked prefill

把长 prompt 的 prefill 分块，与 decode 请求交错，提升吞吐。对长 prompt 任务几乎必开。

### prefix caching

缓存相同前缀的 KV，重复 prompt 或系统提示词可以复用。SGLang 的 RadixAttention 更激进；vLLM 也有 prefix caching。

### CUDA graph

`enforce_eager=false` 时 vLLM 会捕获 CUDA graph，降低 launch 开销。但动态 shape/权重更新可能不兼容；调试时可先 `enforce_eager=true`。

## 15.4 SGLang 集成

SGLang 的优势：

- RadixAttention：自动复用前缀；
- 结构化输出/约束解码；
- 多轮对话与工具调用的 server 支持；
- 与 verl 的 AgentLoop 集成较好。

配置：

```yaml
actor_rollout_ref:
  rollout:
    name: sglang
    mode: async
    tensor_model_parallel_size: 2
    gpu_memory_utilization: 0.5
    enable_prefix_caching: true
```

SGLang 的权重更新支持 delta loader（`sglang_rollout/delta_loader.py`），适合小步更新。

## 15.5 TensorRT-LLM 与 HF

- **TensorRT-LLM**：需要编译 engine，启动慢，但推理快；适合固定模型/硬件的生产服务。v0.9.1 的 pyproject 把 trtllm 标为 deferred。
- **HF Transformers**：`hf_rollout.py`，实现简单、兼容所有 HF 模型，但没有 continuous batching/PagedAttention，吞吐低。适合单卡调试、验证算法。

## 15.6 PD 分离

PD（Prefill-Decode）分离把 prefill 与 decode 放到不同 GPU/进程：

- prefill 是计算密集，适合大 batch；
- decode 是访存密集，适合大 KV cache；
- 分离后可以独立扩缩，提升利用率。

v0.9.1 的 rollout 配置有 `disaggregation`：

```yaml
actor_rollout_ref:
  rollout:
    disaggregation:
      enabled: false
      prefill_replicas: 1
      decode_replicas: 1
      decode_tensor_model_parallel_size: null
      transfer_backend: nixl
```

它需要额外的 KV 传输后端（NIXL 等），配置复杂，适合大规模生产。

## 15.7 Router 与负载均衡

server 模式下，请求要路由到某个 rollout replica。verl 的 `verl/workers/rollout/router.py` 提供路由抽象。考虑因素：

- 前缀缓存亲和：同一前缀尽量路由到同一 replica；
- 负载均衡：避免热点；
- 故障转移：replica 挂了要重试；
- 会话粘性：多轮对话需要同一会话落到同一 replica。

v0.9.1 的 `verl/workers/rollout/router.py` 把路由抽象成 `RequestLoadBalancer` 协议：

```python
class RequestLoadBalancer(Protocol):
    def acquire_server(self, request_id: str, **extra) -> tuple[str, Any]: ...
    def release_server(self, server_id: str, request_id: str | None = None) -> None: ...
    def require_acquire_fields(self) -> list[str]: ...
    def require_release_fields(self) -> list[str]: ...
    def add_servers(self, servers: dict[str, Any]) -> None: ...
    def remove_servers(self, server_ids: list[str]) -> None: ...
    def get_all_servers(self) -> list[str]: ...
    def get_status(self) -> dict: ...
    def clear_sticky_cache(self) -> dict: ...
```

几个设计点：

- `require_acquire_fields` 只声明路由真正需要的字段（例如 `prompt_ids`），避免把整条请求序列化进 RPC；这是分离部署里很实际的带宽优化。
- `release_server` 按 `request_id` 释放，而不是按 server 计数；否则一个请求重试会把计数减两次。
- sticky cache 有容量上限（默认 10000），不能无限增长；训练/推理阶段切换或权重更新后要 `clear_sticky_cache`，否则旧会话会落到持有旧权重的 replica。
- 自定义 router 通过 FQN 加载（`load_class_from_fqn`），不需要改核心代码；这是附录 P 的扩展点之一。

路由层最容易出的问题不是“分不匀”，而是**与权重版本不一致**：路由缓存不知道 replica 已经换了权重，仍然按前缀亲和把请求发过去，前缀命中率看起来很高，生成质量却在下降。所以版本号要进入缓存 key，或者同步时清空缓存。

## 15.8 AgentLoop 与 rollout 的关系

v0.9.1 把多轮对话放在 `verl/experimental/agent_loop/`：

- `AgentLoopBase`：定义 `run`；
- `SingleTurnAgentLoop`：单轮；
- `ToolAgentLoop`：工具调用；
- `AgentLoopWorker`：批量生成；
- `AgentLoopManager`：管理多个 worker；
- `AgentLoopManagerTQ`：TransferQueue 版。

AgentLoop 负责：

- 应用 chat template；
- 调用 LLM server；
- 解析工具调用；
- 执行工具；
- 拼接 observation；
- 记录 token 与 mask。

rollout 引擎只负责“给定 token ids，生成 token ids”。多轮的控制逻辑在 AgentLoop。

## 15.9 吞吐与延迟的权衡

| 参数 | 提高吞吐 | 提高延迟/稳定性 |
|---|---|---|
| `max_num_seqs` ↑ | 更多并发 | 单请求延迟上升 |
| `max_num_batched_tokens` ↑ | 更大 batch | 显存上升 |
| `gpu_memory_utilization` ↑ | 更多 KV cache | 训练 OOM |
| `tensor_model_parallel_size` ↑ | 单模型更快 | 通信增加 |
| `enable_chunked_prefill` | 长 prompt 不阻塞 | 实现复杂 |
| `enforce_eager=true` | 启动快、稳定 | 吞吐下降 |

## 15.10 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动时 OOM | `gpu_memory_utilization` 太高 | 降到 0.4 |
| 生成卡住 | server 未就绪/超时 | 检查 health、端口、router |
| 输出重复 | 温度太低/惩罚太强 | 调 temperature/top_p |
| 权重更新后输出不变 | 同步未生效 | 检查 version、sleep/wake |
| 多轮 token 错位 | chat template/observation 拼接 | 打印 token 与 mask |
| 吞吐低 | eager/CUDA graph/批大小 | 调 `enforce_eager`、batch |

## 15.11 MiniVerl 的 rollout

MiniVerl 的 `rollout.py` 实现：

- `SamplingParams`：temperature/top-k/top-p/do_sample/max_tokens；
- `NaiveRollout`：逐条自回归采样，记录 `rollout_log_probs`；
- `TPRollout`：模拟 TP 权重布局；
- `AsyncRolloutServer`：版本化队列；
- `sleep/wake_up`、prefix cache 统计。

它不是高性能推理引擎，但保留了 RL 需要的契约：`n`、温度、log-prob、版本、sleep/wake、权重更新。

## 15.12 PD 分离的成本模型与工程实现

PD 分离（Prefill-Decode disaggregation）把同一条请求拆成两个阶段，分别跑在不同的 GPU 组上：

```text
prompt ──► prefill workers ──KV──► decode workers ──► tokens
            计算密集              传输         访存密集
```

它不是为了“更快地算”，而是为了让两种完全不同的 roofline 各自跑到合适的 batch 形状：prefill 是大矩阵乘、算力受限；decode 是逐 token 生成、KV cache 带宽受限。混在一起时，长 prompt 的 prefill 会阻塞正在 decode 的请求，decode 的低算力利用率又浪费了 prefill 的算力。

### 什么时候值得拆

设 prefill 时间 $T_p$、decode 时间 $T_d$、KV 传输时间 $T_{kv}$。**稳态吞吐**（batch 填满、请求相互独立、prefill 与 decode 流水重叠）的收益比约为：

$$
\text{throughput speedup}\approx\frac{T_p+T_d}{\max(T_p,T_d)+T_{kv}}.
$$

注意这是吞吐口径，不是单请求延迟。单请求走 PD 要多一次串行 KV 传输，延迟是 $T_p+T_{kv}+T_d$，通常比不拆更慢；PD 的收益来自让两组资源各自跑满，而不是让单条请求更快。如果 $T_{kv}$ 与 $\max(T_p,T_d)$ 同量级，拆分可能白忙。判断要点：

- prompt 很长、decode 很短：prefill 占比高，拆分收益大；
- prompt 短、输出很长：decode 主导，拆分的收益主要来自 decode 组可以独立扩缩；
- KV 传输带宽低或跨机房：$T_{kv}$ 吃掉收益；
- 请求到达率低、batch 填不满：两组都吃不饱，不如合在一起。

### KV 传输量

对一层、一个 token，K 和 V 各 `n_kv_heads × head_dim` 个元素。整条 prompt 的 KV 传输量：

$$
\text{KV bytes}=L_{\text{prompt}}\times n_{\text{layers}}\times 2\times n_{\text{kv\_heads}}\times d_{\text{head}}\times \text{bytes\_per\_elem}.
$$

例：prompt 4096、32 层、8 个 KV heads、head_dim 128、bf16：

```text
4096 * 32 * 2 * 8 * 128 * 2 = 512 MiB / 请求
```

以 400 Gbps 的 NIXL 链路估算，传输约 11 ms；如果链路只有 25 Gbps，则约 170 ms，足以抵消拆分收益。这也解释了为什么 PD 分离通常和高速 RDMA/NVLink 域、Mooncake/NIXL 这类传输栈一起出现。

### 拓扑与配比

P/D 的比例不是 1:1。经验做法：

- 先测单请求的 $T_p$ 与 $T_d$；
- 按目标并发算两组的吞吐；
- 让 prefill 组的利用率与 decode 组接近，而不是让卡数相等；
- decode 组的 TP 可以独立设置（`decode_tensor_model_parallel_size`），因为 decode 更受 KV cache 容量约束。

配置形状（v0.9.1 的 `actor_rollout_ref.rollout.disaggregation`）：

```yaml
actor_rollout_ref:
  rollout:
    disaggregation:
      enabled: true
      prefill_replicas: 4
      decode_replicas: 8
      decode_tensor_model_parallel_size: 2
      transfer_backend: nixl
```

### 故障模式

| 现象 | 原因 | 处理 |
|---|---|---|
| decode 等 KV 超时 | 传输链路拥塞/带宽不足 | 降 prompt 长度、换后端、加超时 |
| prefill 组空闲、decode 排队 | P/D 配比失衡 | 按 $T_p:T_d$ 调 replica 数 |
| 输出与合在一起时不一致 | KV 布局/精度转换错 | 对比同 prompt 的 logits |
| 权重更新后 decode 仍旧 | prefill/decode 副本未同时更新 | 版本化同步，两个组都确认 |
| 请求重试导致重复计费 | 没有幂等 request id | 路由层去重 |

### MiniVerl 的 PD 模拟

`mini_verl/rollout.py::PDDisaggregatedRollout` 保留同一个采样器，但把账算清楚：

- 统计 `pd/prefill_tokens`；
- 按模型层数/KV heads/head_dim 估算 `pd/kv_transfer_bytes`；
- 按 `transfer_backend` 的带宽估算 `pd/transfer_ms`；
- 记录 P/D replica 数，便于做配比实验。

`examples/18_pd_disaggregation.py` 直接打印这些量；`tests/test_rollout_checkpoint.py` 验证 PD 与 naive 在相同 seed 下采样结果一致，避免“为了拆分而改变算法”。

## 15.13 分离式 rollout：把推理放进独立资源池

PD 分离拆的是**同一条请求的两个阶段**；资源分离（separation）拆的是**训练与推理两组资源**。两者可以叠加：训练池里跑 actor/critic，推理池里跑 PD 分离后的 prefill/decode。

### 什么时候需要资源分离

colocate 的前提是“训练和生成本来就交替进行”。当出现下面任一情况，colocate 开始浪费：

- rollout 有长尾：少量超长生成阻塞整个 batch；
- 训练与生成的最佳并行度不同：训练要 TP/PP，推理要更大的 batch；
- 需要独立扩缩：白天推理压力大，夜间训练压力大；
- 异步 RL：rollout 必须持续产出，不能等训练。

### 三个组件

v0.9.1 的 `verl/experimental/separation/` 提供：

1. `SeparateRayPPOTrainer`：复用 PPO 训练逻辑，只替换资源池与角色映射；
2. `DetachActorWorker`：把 actor 模型 detach 到 CPU、释放 GPU，需要时 restore；
3. `create_resource_pool_manager` / `create_role_worker_mapping`：只负责训练侧——前者只建 `trainer_pool` 并映射训练角色，后者只决定角色对应的 worker 类。rollout 资源池由 fully-async 的 rollouter/独立配置分配，不是这两个函数建的。

`DetachActorWorker` 支持 FSDP、FSDP2、VeOmni、Megatron。差异在于分片表示：

| 策略 | 参数表示 | detach/restore |
|---|---|---|
| FSDP1 | flat tensor | 按 flat 分片读写 |
| FSDP2 | DTensor | 按 DTensor 分片读写 |
| VeOmni | FSDP2（DTensor） | 复用 FSDP2 工具 |
| Megatron | TP/PP/EP 分片 | 按 Megatron 布局读写 |

用错 handler 的典型症状是“restore 后 loss 跳变”或“部分参数没回来”，而不是直接报错。

### 权重怎么过去

分离后，权重同步从“同进程内存拷贝”变成“跨进程/跨节点传输”。v0.9.1 用 `CheckpointEngine` 抽象：

```python
weights, metadata = actor.get_per_tensor_param()   # 返回 (generator, metadata)
await engine.send_weights(weights, global_steps=step)
# rollout 侧：receive_weights() 返回 (name, tensor) 生成器
received = await engine.receive_weights(global_steps=step)
for name, tensor in received:
    ...
```

后端可选 naive/nccl/nixl/mooncake/kimi。`hccl` 不是独立注册键：Ascend 上 `backend=nccl` 会自动走 HCCL。colocate 用 naive，固定集群用 nccl，需要弹性用 nixl/mooncake。细节见附录 W。

### 与异步 RL 的关系

资源分离是异步 RL 的**部署前提**，但不等同于异步：

- separation 只说明“训练与推理不共享 GPU”；
- 异步还要求“rollout 在训练更新时继续产出”，并用 staleness/rollout correction 处理策略滞后。

第 27 章讲异步算法；本节的 separation 讲资源与传输。两者组合时，版本共识（每个 replica 知道自己持有第几版权重）是最容易漏的一环。

### 成本判断

分离不是免费：多一次网络传输、多一套调度、多一组空闲资源。粗略判断：

$$
\frac{1}{u'} + s < \frac{1}{u}
$$

其中 $u$ 是 colocate 利用率，$u'$ 是分离后利用率，$s$ 是同步开销占比。只有左边小于右边，分离才在成本上划算。具体算例见附录 W.8。

## 本章小结

- Rollout 是 RL 的吞吐瓶颈，vLLM/SGLang 提供 continuous batching、PagedAttention、前缀缓存。
- colocate 模式省资源但要管理显存；server 模式解耦但增加网络与路由复杂度。
- PD 分离、prefix caching、chunked prefill 是长 prompt/高并发的关键优化。
- 资源分离把训练与推理放进独立资源池，用 DetachActorWorker 与 CheckpointEngine 连接；只有利用率收益大于同步开销时才划算。
- AgentLoop 负责多轮控制，rollout 只负责单次生成。
- MiniVerl 用简化 sampler 保留采样与权重生命周期语义。

## 延伸阅读

- vLLM：<https://docs.vllm.ai/>
- SGLang：<https://docs.sglang.ai/>
- `verl/workers/rollout/`
- 附录 W：资源分离、checkpoint engine 与弹性部署。
- 附录 Z：continuous batching、PagedAttention、chunked prefill、投机解码与容量规划。
- 下一章：采样策略、n 采样与前缀缓存。

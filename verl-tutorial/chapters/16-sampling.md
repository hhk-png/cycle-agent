# 第 16 章 采样策略、n 采样与前缀缓存

## 16.1 采样不是一个 temperature 参数

生成策略决定三件事：

1. **探索**：策略能否覆盖足够多的行为，给 RL 提供信号；
2. **质量**：采样出的轨迹是否还在“可学习”的分布内；
3. **成本**：生成长度、并发、KV cache。

常见参数：

```yaml
actor_rollout_ref:
  rollout:
    temperature: 1.0
    top_k: -1
    top_p: 1.0
    n: 1
    do_sample: true
    ignore_eos: false
    response_length: 512
```

## 16.2 温度

给定 logits $z_i$，温度采样：

$$
p_i=\frac{\exp(z_i/T)}{\sum_j\exp(z_j/T)}.
$$

- $T\to 0$：趋向 argmax（贪心）；
- $T=1$：原始分布；
- $T>1$：更平坦，探索更强；
- $T<1$：更尖锐，质量更稳。

RL 经验：

- 训练采样常用 $T=0.8\sim1.2$；
- 验证用 $T=0$（贪心）；
- 温度太低会导致组内样本几乎相同，GRPO 优势为 0；
- 温度太高会生成大量无效轨迹，reward 稀疏。

## 16.3 top-k 与 top-p

### top-k

只保留概率最高的 $k$ 个 token，重新归一化。$k=-1$ 表示不限制。

### top-p（nucleus）

按概率从高到低累加，保留累计概率达到 $p$ 的最小集合。$p=1$ 表示不限制。

两者可以叠加。top-p 比 top-k 更自适应：分布尖锐时保留少，平坦时保留多。

### 约束解码

如果任务有合法动作集合（如 bandit 的四个动作、代码的语法 token），可以在 logits 上加 mask：

```python
masked_logits[~allowed] = -inf
```

MiniVerl 的 `rollout.allowed_chars` 就是这样做的。注意：**训练侧的 log-prob 也必须用同一个 mask**，否则行为策略与训练策略不一致，PPO 比率会错。第 18 章会展开。

## 16.4 n 采样与分组

`rollout.n` 表示每个 prompt 生成几条响应。GRPO 依赖它构造组基线。

实现上：

```python
repeated = prompts.repeat(n, interleave=True)
outputs = rollout.generate(repeated)
```

正确性的关键是：同一 prompt 的 n 条样本必须携带**同一个 uid/index**；GRPO 按这个 key 分组，与它们在 batch 中的先后顺序无关。`interleave=True` 让同一组的样本连续排列，主要是缓存/调度的局部性优化；只有当分组 key 由行号推导时，顺序才会变成正确性问题。

成本：

$$
\text{generated tokens}=P\cdot n\cdot L.
$$

$n$ 翻倍，生成与训练 token 都翻倍。GRPO 的“省 critic”是用生成成本换来的。

## 16.5 log-prob 的三种来源

1. **rollout 引擎记录**：采样时直接返回每个 token 的 log-prob；注意它应该是**行为策略**下的 log-prob（含 temperature/top-k/top-p），而不是原始 logits 的 log-prob；
2. **训练引擎重算**：用 actor 对同一序列做前向，得到 `old_log_probs`；
3. **参考模型计算**：`ref_log_prob`，用于 KL。

三者必须对齐：

- 同一 tokenizer；
- 同一 attention mask；
- 同一约束解码 mask；
- 同一 dtype/精度（至少不能差太多）。

v0.9.1 的 `rollout.logprobs_mode` 控制返回哪种 logprob（processed/raw）。异步模式下，`old_log_prob` 可能直接使用 rollout 记录的值（`use_rollout_log_probs=True`），此时精度与版本一致性更重要。

## 16.6 前缀缓存

prefix caching 缓存相同前缀的 KV：

```text
system prompt + few-shot examples + user question
```

如果系统提示词固定，每个请求都能复用前几百个 token 的 KV，prefill 成本大幅下降。

命中率：

$$
\text{hit rate}=\frac{\text{cached prefix tokens}}{\text{total prompt tokens}}.
$$

注意：

- 缓存 key 通常是 token 序列的 hash；
- 不同 temperature/top-p 不影响前缀 KV；
- 权重更新后缓存必须失效；
- sleep 时通常清空缓存；
- 多轮对话中，历史消息是天然的前缀。

SGLang 的 RadixAttention 用 radix tree 管理前缀，适合多轮/共享系统提示；vLLM 也有 prefix caching。

MiniVerl 的 `NaiveRollout` 记录 `prefix_hits` 与 `prefill_tokens_saved`，但不真正复用 KV；它用于演示统计口径。

## 16.7 KV cache 的显存

对一层、一个 token：

$$
\text{KV bytes}=2\times n_{\text{kv\_heads}}\times d_{\text{head}}\times \text{bytes}.
$$

整个 batch：

$$
\text{KV}=B\cdot T\cdot L\cdot 2\cdot n_{\text{kv\_heads}}\cdot d_{\text{head}}\cdot \text{bytes}.
$$

例：B=8、T=4096、L=32、kv_heads=8、head_dim=128、bf16：

```text
8 * 4096 * 32 * 2 * 8 * 128 * 2 = 4 GiB
```

这只是一份；TP 会切分 KV heads，PP 会切分层。GQA/MQA 通过减少 `n_kv_heads` 降低 KV cache。

## 16.8 停止条件

- `eos_token_id`：模型生成 EOS 时停止；
- `max_response_length`：达到上限截断；
- `stop_strings`：遇到指定字符串停止（部分后端支持）；
- `ignore_eos=true`：忽略 EOS，强制生成长序列（用于长度控制实验）。

截断的样本要标记：

- 需要一个显式的 `truncated` 标志：`response_length == max_response_length` 可能是“正好在预算处自然结束（最后 token 是 EOS）”，不一定是截断；
- DAPO 只对真正 `truncated=True` 的样本加 overlong penalty；
- 验证时区分“答错”和“没答完”。

## 16.9 确定性与可复现

完全确定性很难：

- GPU 浮点非结合；
- continuous batching 的顺序影响 kernel；
- CUDA graph/FlashAttention 可能有非确定性；
- 多卡 all-reduce 顺序。

verl 提供 `full_determinism` 与 `VLLM_BATCH_INVARIANT` 等开关，但会降低性能。复现实验时：

- 固定 seed；
- 固定 batch 顺序；
- 关闭不必要的并行；
- 记录 tokenizer/model/engine 版本；
- 至少跑 3 个 seed 看方差。

## 16.10 MiniVerl 的采样实现

`mini_verl/rollout.py::NaiveRollout._sample_one`：

1. 前向当前序列，取最后一个位置的 logits；
2. 应用 allowed_chars mask；
3. 按 temperature 缩放；
4. top-k/top-p 截断；
5. 归一化并采样；
6. 记录**行为策略**的 log-prob（应用 temperature/top-k/top-p 之后；greedy 时等价于 raw log-prob）；
7. 遇到 EOS 或达到 `max_tokens` 停止。

测试 `test_rollout_checkpoint.py` 验证了：

- 同 seed 的输出一致；
- 约束解码只产生允许字符；
- sleep 后生成报错、wake 后恢复；
- 权重更新后 version 增加。

## 16.11 前缀分组（prefix grouper）与缓存亲和调度

prefix caching 解决“相同前缀的 KV 能不能复用”；prefix grouper 解决“**哪些请求应该被放在一起、在什么时候发**”。两者配合才能把命中率变成吞吐。

### 为什么需要分组

假设一个 batch 里有 64 条 prompt：

- 32 条共享同一个 2K token 的 system prompt；
- 16 条共享另一段 1K token 的 few-shot；
- 16 条互不相同。

如果按到达顺序混发，缓存会被不同前缀反复驱逐，命中率接近 0。如果先把相同前缀的请求聚成组，再连续发送，第一组的第一条请求做完整 prefill，后续请求直接复用 KV，prefill 成本从 $32\times2K$ 降到 $2K+31\times0$（理想情况）。

### 分组键

常见键的优先级：

1. **完整 token 前缀 hash**：最精确，但长 prompt 的 hash 计算和存储开销大；
2. **system prompt / chat template hash**：适合固定系统提示的多轮 Agent；
3. **数据源 / 任务类型**：粗粒度，但能避免跨任务污染缓存；
4. **多轮会话 id**：同一会话的历史天然是前缀。

verl 的 `verl/trainer/ppo/prefix_grouper_utils.py` 提供前缀分组辅助；AgentLoop/TransferQueue 路径会把同一会话或同一前缀的样本放在相邻位置。

### 调度策略

| 策略 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| 前缀优先 | 同前缀请求连续发 | 命中率高 | 可能饿死其他前缀 |
| 轮转 | 每组轮流取一条 | 公平 | 缓存频繁切换 |
| 分组批量 | 一次发完一组 | 命中率最高 | 长尾组阻塞 |
| 亲和路由 | 按前缀 hash 选 replica | 分布式缓存友好 | 负载可能不均 |

工程上通常是“分组批量 + 最大等待时间”：同组攒到一定数量或等待超时即发，避免为了凑组无限拖延。

### 命中率的正确口径

$$
\text{hit rate}=\frac{\sum_{\text{request}}\text{cached prefix tokens}}{\sum_{\text{request}}\text{prompt tokens}}.
$$

注意分母是 prompt token 总数，不是请求数；一个 4K prompt 命中 2K 与一个 100 token prompt 全命中，权重完全不同。还要区分：

- **缓存命中**：KV 真的被复用；
- **前缀匹配**：只是 token 相同，但缓存已被驱逐；
- **逻辑命中**：分组器认为会命中，但引擎实际未命中。

只看分组器的日志会高估收益，必须看推理引擎的 `prefix_cache_hit_tokens`。

### 权重更新后的失效

权重一变，所有 KV 都失效。异步 RL 中常见错误是：训练侧更新了权重，rollout 侧还保留旧前缀缓存，生成的 token 与当前策略不一致。正确做法：

- 权重版本号进入缓存 key；
- 同步完成后清空或标记旧缓存；
- 或者让缓存按版本分区，旧版本自然淘汰。

### 与 GRPO 分组的交互

GRPO 要求同一 prompt 的 $n$ 条样本连续排列（`interleave=True`），这样组基线才对。前缀分组不能打乱这个约束：应该先按 prompt 分组，再在组内保持 $n$ 条连续，最后把不同 prompt 按前缀聚在一起。顺序错了，`uid` 分组会把不同 prompt 混进同一组，优势全错。

### MiniVerl 的边界

MiniVerl 的 `NaiveRollout` 只统计 `prefix_hits` 与 `prefill_tokens_saved`，不做真实 KV 复用，也没有 prefix grouper。它演示的是**统计口径**：你可以用 `prefix_hits` 观察同一批 prompt 重复时命中数如何变化，但不要把它当成真实推理引擎的吞吐数字。

## 16.12 Rollout KV cache offload：把长尾显存搬到 host 与远端

长序列 RL 的 rollout 显存通常不是被权重吃掉的，而是被 **KV cache** 吃掉的。一个 32K 上下文、并发 64、GQA 的 32B 模型，KV 可以轻松超过权重本身。offload 的思路是：把暂时不活跃请求的 KV 从 HBM 搬到 host DRAM、NVMe 或远端存储，需要时再搬回，用带宽换容量。

### 先算一笔账

单 token KV 字节（与 16.7 同一口径）：

$$
\text{kv\_bytes\_per\_token}
=2\cdot L\cdot n_{\text{kv\_heads}}\cdot d_{\text{head}}\cdot \text{bytes}.
$$

一个请求的 KV：

$$
\text{kv\_bytes}=\text{kv\_bytes\_per\_token}\cdot T.
$$

搬一次的时间下界：

$$
t_{\text{offload}}=\frac{\text{bytes}\cdot 8}{\text{bandwidth}_{\text{Gbps}}}\quad\text{(秒)}.
$$

例：32B、L=64、kv_heads=8、head_dim=128、bf16，则每 token 约：

```text
2 * 64 * 8 * 128 * 2 = 256 KiB/token
```

一个 32K 请求约 8 GiB。PCIe Gen5 x16 理论 128 GB/s，实际单向 50～60 GB/s；搬 8 GiB 约 0.14～0.16 s。若这块 KV 只是短暂不用、之后还要继续 decode，offload 可能划算；如果请求已经结束，直接丢弃、需要时重新 prefill 更便宜。

### 三种“搬”的替代方案

| 方案 | 做什么 | 省什么 | 代价 | 适用 |
|---|---|---|---|---|
| 丢弃 + 重算 | 释放 KV，下次重新 prefill | HBM | 重复 prefill 算力 | 短请求、前缀短、算力富余 |
| Offload 到 host | KV 搬到 CPU DRAM，需要时搬回 | HBM | PCIe 往返 + pinned memory | 长请求、HBM 紧、PCIe 有余量 |
| Offload 到远端 | KV 走 RDMA/NVMe 到其他节点 | HBM + 单机容量 | 网络带宽/延迟、故障面 | 超长上下文、PD 分离、集群级池化 |

选择的核心是**重算成本 vs 搬运成本**。重算一个 32K 前缀的 prefill 可能是几百毫秒到数秒；搬 8 GiB 在 NVLink-C2C 上可能只要几十毫秒，在 PCIe 上要一百多毫秒，在跨机 RDMA 上取决于网络。没有统一答案，必须按硬件和请求长度算。

### 工程要点

1. **分页与碎片**：KV 按 block 管理（PagedAttention 风格），offload 也应按 block 粒度，而不是整请求；否则一个 1K 的活跃请求会被 32K 的非活跃请求拖住。
2. **异步拷贝**：用独立 CUDA stream + pinned host buffer，避免阻塞 decode；拷贝完成前该 block 不能复用。
3. **淘汰策略**：LRU/LFU 之外，还要考虑“剩余生成长度”——马上要 decode 的请求优先级高，已结束的请求应直接丢弃而不是 offload。
4. **版本失效**：权重同步后所有 KV 都失效（见 16.11）；offload 池必须按权重版本分区，或同步时整体清空。异步 RL 里把旧版本 KV 搬回来，等于让 rollout 用旧策略生成。
5. **容量与带宽的背压**：host 内存也会满；offload 队列要有上限，满了就丢弃而不是无限排队。否则延迟尖峰会传导到整个 batch。
6. **可观测性**：至少记录 offload/reload 字节、命中率、p99 搬回延迟、HBM 峰值、因容量不足丢弃的请求数。只看平均延迟会掩盖长尾。

### 什么时候不值得

- 请求很短（KV 小于几十 MB）：搬运维成本大于收益；
- PCIe 已经打满（例如同时在跑权重同步/checkpoint）：offload 会和新流量抢带宽；
- 请求不会再被复用：直接丢弃 + 重算；
- 训练侧显存也紧张：host 内存与 pinned buffer 同样有限，别把 OOM 从 GPU 转移到 CPU。

### verl 与 MiniVerl 的边界

v0.9.1 在 `docs/perf/rollout_kv_offload.md` 中给出了 rollout KV offload 的配置与调优路径，具体开关随推理后端（vLLM/SGLang）而异；使用前要确认后端版本、是否支持分页/前缀缓存、以及权重同步后的失效语义。MiniVerl 的 `NaiveRollout` 不持有真实 KV，只统计 `prefix_hits` 与 `prefill_tokens_saved`，因此**不能**用它验证 offload 收益；它只能帮助你理解“命中/节省”的统计口径。真实收益必须在目标推理引擎上用固定 workload 压测。

## 16.13 Router replay：让采样与训练走同一组专家

MoE 模型的采样多了一个普通 dense 模型没有的自由度：**专家路由**。同一个 token，在 rollout 引擎里可能被路由到专家 $\{2,5\}$，在 actor 重算 log-prob 时被路由到 $\{2,7\}$。如果这两次前向的路由不同，PPO 的重要性比率就不再是"同一策略在同一条轨迹上的概率比"，on-policy 假设被破坏。

### 为什么会分叉

- **精度不同**：rollout 用 FP8/FP4 kernel，训练用 bf16；router logits 的微小差异可能改变 top-k 的边界。
- **并行不同**：TP/EP 的分片方式改变 all-reduce 顺序，进而改变 logits 的低位。
- **实现不同**：vLLM/SGLang 的 MoE kernel 与 Megatron 的 dispatcher 对 capacity/drop 的处理不同。
- **hash router**：DeepSeek V4 的前三层专家 id 来自 `input_ids` 查表，不来自 router logits；只拦截 learned top-k 会漏掉它们。

### 记录-重放协议

v0.9.1 的 `RouterReplay`（`verl/utils/megatron/router_replay_patch.py`）定义三种动作：

| 动作 | 行为 |
|---|---|
| `RECORD` | 正常路由，保存每层 top-k |
| `REPLAY_FORWARD` | 用保存的 top-k，不再重新路由 |
| `REPLAY_BACKWARD` | 从列表头部 `pop(0)`（FIFO），不是倒序 |

`set_replay_data(all_layers_topk_indices)` 把每层记录分发给对应 router；`get_recorded_data()` 收集。R2 模式在 actor log-prob 时记录、actor 更新时重放；R3 模式在 rollout 后端记录、Megatron 重放。R3 才能消除"推理后端 vs 训练后端"的分叉。

### 层序是隐式契约

最容易错的不是形状，而是**层序**。假设模型有 3 层 hash router + 12 层 learned router：

```text
vLLM 记录:   [h0, h1, h2, l0, l1, ..., l11]   （15 层）
Megatron 记录: [l0, l1, ..., l11]              （12 层，漏了 hash）
```

两边的张量形状完全兼容，但第 0 条记录被重放到第 4 层，之后每一层都错位。训练仍然能跑，loss 也可能下降，只是学的是一个错位的路由。

正确的做法是：

1. 把 `input_ids` 传进 decoder，让 hash router 与 rollout 看到同一输入；
2. 记录 hash 层与 learned 层，保持顺序；
3. **显式校验层数**，不一致直接失败。

MiniVerl 的 `replay_moe_routes` 就是这个思路：层数不一致时抛 `ValueError`，而不是截断到较短的一边。

### replay mask

R3 还需要 causal replay mask。它必须包含**影响 response token logits 的所有行**，而不只是 response 行：causal attention 下，prompt 末尾也参与第一个 response token 的计算。漏掉这些行，重放的路由与真实前向不一致。

另外，重放的 top-k 可能包含重复专家 id；dispatcher 的 token count 要从实际 routing map 统计，不能假设等于 `num_tokens * top_k`。

### 验证

- 关闭 replay，跑至少两个优化器 step；只跑一步会漏掉"第二次同步时推理侧已后处理参数"的问题。
- 比较 rollout 与 actor 的 token log-prob：Pearson 相关、最大/均值/标准差差异。
- 开启 replay，确认所有层（含 hash）都被记录，且顺序一致。
- 用目标长度负载测试，短 smoke test 会漏掉 packing/attention window 的边界。

DeepSeek V4 的完整案例（FP8/FP4 权重、MXFP4、R2/R3、验证协议）见附录 V。

### MiniVerl 的实现

`mini_verl/parallel.py` 提供：

- `moe_route(..., replay_top_idx=..., replay_mask=...)`：用记录值替换 top-k，支持行级 mask；
- `record_moe_routes(result, with_accepted=False)`：复制记录；`with_accepted=True` 时同时保存 `accepted`，用于区分“top-k 相同但容量丢弃不同”；
- `replay_moe_routes(layer_logits, recorded_routes, ...)`：逐层重放并校验层数；
- `router_replay_report(...)`：定位首个错层、错 token 数与 `mismatched_assignments`（需要双方都记录 `accepted`）。

`examples/24_router_replay.py` 演示记录、重放、行级 mask、层数陷阱与单点损坏定位；`tests/test_router_replay.py` 固定这些行为。

## 本章小结

- temperature/top-k/top-p 控制探索与质量；约束解码必须同时作用于 rollout 与训练 log-prob。
- `n` 采样是 GRPO 的前提，成本随 n 线性增长。
- log-prob 的来源必须对齐 tokenizer、mask、dtype。
- prefix caching 能显著降低长系统提示的 prefill 成本，但权重更新后要失效。
- KV cache 显存由 batch、长度、层数、KV heads 决定；GQA/MQA 是主要压缩手段。
- KV offload 是“带宽换容量”：先比较重算与搬运成本，再决定丢弃、搬到 host 还是搬到远端；权重更新后必须按版本失效。
- MoE 的路由在训练与推理之间可能分叉；router replay 用记录-重放恢复一致性，层序（尤其 hash router）必须显式校验。

## 延伸阅读

- vLLM sampling 文档：<https://docs.vllm.ai/en/latest/dev/sampling.html>
- SGLang RadixAttention：<https://docs.sglang.ai/>
- 附录 V：DeepSeek V4 的量化权重同步与路由回放。
- 附录 Z：前缀缓存/调度的引擎内部账与容量规划。
- 下一章：Reward：规则奖励、模型奖励与 reward loop。

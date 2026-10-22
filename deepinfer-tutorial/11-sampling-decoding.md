# 第 11 章 采样、解码与输出契约

本章导读：模型前向只给出 logits，真正决定用户看到什么的是采样。采样把概率分布、随机数、停止条件、流式输出、日志概率和计费口径连在一起，是引擎里最容易被低估的一段。本章从温度、top-k、top-p 讲到 seed、停止串、UTF-8 边界，再说明结构化输出和推测解码如何改变采样的语义。读完本章，你应该能回答三个问题：为什么同一个 prompt 在不同 batch 下会给出不同答案，为什么 temperature=0 仍然可能不可复现，以及一个“看起来只是随机选 token”的模块为什么会成为线上事故的来源。

## 11.1 采样是引擎的一部分

一次生成可以拆成四步：模型前向得到 logits，采样器把 logits 变成概率分布并选出一个 token，detokenizer 把 token 变成文本，停止条件判断是否结束。很多教程把采样写成一行 `argmax` 或 `np.random.choice`，但在生产引擎里，这四步都必须按请求、按 step、在正确的设备上完成。

采样为什么不能放到引擎外面？第一，它是每步热路径的一部分。decode 一步只有几毫秒，如果每个请求都把几万维的 logits 拷回 CPU、在 Python 里做 softmax、再拷回 GPU，同步和传输会吃掉大部分收益。第二，采样参数是每请求不同的：同一个 batch 里，有的请求 greedy，有的 top-p=0.9，有的带 grammar mask，有的要 logprobs。采样器必须按序列 gather 参数，而不是假设全 batch 共享一套配置。第三，采样直接决定正确性：停止条件、EOS、结构化 mask、推测解码的接受规则，任何一处出错都会改变输出，而不是只影响性能。

DeepInfer 把这一步放在 `execute_sample`：模型 forward 之后，sampler 读取 logits、每个序列的 `SamplingParams`、grammar mask 和 MTP 信息，产出 `SamplerOutput`。`SamplerOutput` 里不只有 `sampled_token`，还有 `verified_tokens`（验证接受的 token 列表）、`predict_tokens`（下一步的 draft）、`num_generated_tokens`/`num_verified_tokens`（计数），以及 `so_status` 等字段。调度器要根据这些字段决定下一步的 `seqlen_q`，所以采样不是“生成完再处理”，而是调度闭环的一部分。

mini-deepinfer 把同样的职责放在 `engine._sample` 和 `sampling.py`。`_sample` 负责从序列里取参数、调用 `sample_token`、处理停止条件；`sampling.py` 只做纯函数式的分布计算和采样。这个拆分值得借鉴：把“数学”和“状态”分开，数学部分可以用固定的 logits 做单元测试，状态部分可以用端到端测试验证。

对初学者来说，只需要记住一条主线：logits 是未归一化的分数，softmax 把它变成概率，采样从这个概率里取一个 token。后面所有的温度、top-k、top-p、mask，都是在对这个概率分布做整形。

## 11.2 温度、top-k 与 top-p

温度是最直观的参数。给定 logits `z`，温度 `T` 下的分布是：

```text
p_i = exp(z_i / T) / sum_j exp(z_j / T)
```

当 `T < 1` 时，分布变尖，高概率 token 更容易被选中；当 `T > 1` 时，分布变平，低概率 token 获得更多机会；当 `T` 趋近 0 时，分布趋近 one-hot，采样退化成 argmax。温度不改变 token 的排序，只改变排序之间的差距。

top-k 保留概率最高的 `k` 个 token，其余置零，再重新归一化。它的优点是实现简单、成本低；缺点是 `k` 是固定值，无法适应分布的形状。一个很尖的分布可能只需要 3 个候选，一个很平的分布可能需要 50 个；固定 `k` 要么截断太多，要么保留太多。

top-p（nucleus sampling）保留“累积概率刚好超过 `p`”的最小集合。具体做法是把 token 按概率降序排列，累加概率，直到累计值达到 `p`，保留这些 token，其余置零。top-p 的候选数随分布形状自适应：分布尖时候选少，分布平时候选多。它比 top-k 更常用，但实现上要注意两个细节。

第一，至少保留一个 token。如果所有概率都很小、累计和因为浮点误差没有达到 `p`，不能返回空集合；mini 的实现会在 `keep` 全为假时强制保留第一个。第二，top-p 的边界 token 要保留。常见的实现是“保留累计概率首次超过 `p` 的那个 token”，而不是“只保留累计概率小于 `p` 的 token”；否则候选集合会偏小。

top-k 和 top-p 可以叠加。顺序通常是先温度、再 top-k、再 top-p，最后重新归一化。顺序会影响结果：先 top-k 再 top-p，和先 top-p 再 top-k，候选集合可能不同。生产框架会把顺序写死并在文档里说明，评测时不能只比较“都开了 top-k 和 top-p”，还要确认顺序一致。

除了 top-k/top-p，还有一些常见的整形手段，它们作用的位置不同。重复惩罚（repetition penalty）对已经出现过的 token 做分段缩放：正 logit 除以大于 1 的系数，负 logit 乘以同一个系数，这样两类 logit 都是“往更不可能的方向”推；如果统一除以系数，负 logit 会变大，反而提高重复概率；频率惩罚按出现次数、存在惩罚按是否出现来加减 logit；logit bias 允许调用方直接给某些 token 加分或减分。这三类都作用在 softmax 之前的 logits 上，并且要按序列应用，不能把整个 batch 的统计混在一起。min-p 则作用在概率上：先算 `p_max`，保留 `p >= min_p × p_max` 的 token；它也可以等价地写成对 logits 的阈值 `logit >= logit_max + log(min_p)`，但语义仍然是“相对最大概率的比例”，不是“相对最大 logit 的差”。min-p 在高温度下比 top-p 更稳定，因为它的阈值随分布的峰值缩放，而不是固定累计质量。

mini 的 `apply_top_k_top_p` 实现了 top-k 和 top-p，`distribution` 负责温度、mask 和 greedy 分支。它没有实现 min-p、重复惩罚和 logit bias，这是明确的语言子集；如果你要加，最自然的位置是在 `distribution` 里、softmax 之前。

一个常见的 bug 是“在未归一化的 logits 上做 top-p”。top-p 的累计概率必须来自归一化后的分布；如果在 logits 上排序再按 softmax 后的概率截断，边界会算错。另一个 bug 是“top-p 之后没有重新归一化”，导致采样概率和不为 1，`np.random.choice` 会报错或产生偏差。

## 11.3 温度 0、贪心与“几乎贪心”

很多 API 把 `temperature=0` 定义为贪心：直接取 logits 最大的 token。但不同框架的处理并不完全一样。DeepInfer 和 vLLM 一类实现会把小于 `1e-5` 的温度设成 `1e-5`，同时把 top-p 设为 1、top-k 设为 -1，然后让采样器走 greedy/argmax 分支。钳位只是为了让参数合法、数值路径统一，不是把 argmax 换成随机采样；结果仍然是严格的 argmax。真正不确定的是并列最大值和跨硬件的浮点差异，而不是“温度很小所以还在随机”。

mini 的选择更直接：`SamplingParams.__post_init__` 在温度小于 `1e-5` 时把 `temperature` 设成 `1e-5`，并关闭 top-p/top-k；`greedy` 属性判断温度是否接近 0，`distribution` 直接返回 one-hot。这样贪心测试更稳定，但也意味着 mini 和真实框架在极端温度下的数值路径不完全一样。书中比较输出时，只比较同一实现内的结果，不跨实现做逐 token 断言。

温度 0 还有两个陷阱。第一，argmax 的并列。如果两个 token 的 logits 完全相同，argmax 返回哪个取决于实现（通常是最小 id），不同框架可能不同。第二，“几乎贪心”不是“逐位可复现”。在 GPU 上，batch 组成、归约顺序、低精度 kernel 都可能让 logits 产生 1e-6 的差异；当两个 token 的分数非常接近时，这个差异足以让 argmax 翻转。所以 temperature=0 只能保证“同一实现、同一 batch 组成、同一硬件路径”下的确定性，不能保证跨硬件逐位一致。

这也是第 10 章讨论 batch invariance 的原因。对需要严格复现的业务，除了 temperature=0，还要开启 batch-invariant kernel、固定 seed、关闭可变长推测解码，并把模型、tokenizer、模板、硬件和驱动版本一起记录下来。

## 11.4 随机数：seed、RNG 流与可复现

随机采样需要随机数。最朴素的做法是全局一个 RNG，所有请求共享；这在单请求 demo 里没问题，在并发服务里会破坏可复现性：同一个请求，如果它前面插入了别的请求，RNG 的消耗顺序就变了，输出也会变。

正确的做法是每个序列一个 RNG，seed 来自请求参数，而不是全局计数器。DeepInfer 的 `SamplingParams` 有 `seq_seed`，每个序列独立；mini 的 `Sequence` 持有 `rng` 和 `draft_rng`，`SamplingParams` 校验 seed 是否落在 uint64 范围内（超出抛 `ConfigError`），并在未提供时用系统随机源生成。这样 batch 顺序、并发数、调度顺序都不会影响单个请求的结果。mini 没有把序列 id 混进 seed：两个请求如果复用同一个 `SamplingParams` 对象且 seed 相同，会得到相同的随机流。这符合“同 seed 同输出”的复现语义，但如果你希望同一批请求各自独立，就要显式给每个请求不同的 seed。

seed 的派生要注意两件事。第一，seed 空间要足够大，避免不同租户/请求碰撞；uint64 是常见选择。第二，seed 不能直接暴露给不可信调用方作为安全凭据；它只保证可复现，不保证不可预测。如果业务需要防篡改，应该在网关层签名或使用服务端生成的 request id。

RNG 的消耗顺序同样重要。采样实现如果先对 top-k 排序、再按排序后的索引取随机数，那么并列 token 的顺序会改变随机数对应的 token。要保证跨版本可复现，必须固定排序规则（例如概率降序、概率相同按 token id 升序），并固定每次采样消耗的随机数个数。推测解码更复杂：接受/拒绝规则可能消耗可变数量的随机数，draft 和 target 各自有 RNG；如果两边的消耗顺序不同，即使接受率相同，输出也会不同。

mini 的实现刻意把随机数限制在“每个序列自己的 RNG”里：`sample_token` 只在最终采样时取一次随机数，`target_probs` 只做分布计算不取随机数。推测解码的 draft 用 `seq.draft_rng`，target 验证用目标分布和独立的随机数。这个设计让 batch invariance 测试可以通过“批量 vs 逐条”的逐字符比较。

一个容易被忽略的点是“采样参数的生命周期”。`SamplingParams` 在请求进入时被复制一份，之后调度器、采样器、停止条件都读同一份；如果中途被修改（例如动态调整温度），要保证所有副本一致，否则同一个请求的不同 step 会用不同参数。生产实现通常把采样参数做成不可变对象，或者在请求开始时冻结。

## 11.5 停止条件与长度控制

生成什么时候停？这是采样器之外、但和采样紧密相关的一组规则。常见的停止条件有六种。

第一，EOS token。模型输出 `eos_token_id` 时停止，`finish_reason` 记为 `stop`。第二，`stop_token_ids`。调用方指定一组 token id，命中任意一个就停止。第三，`stop` 字符串。调用方指定文本串，生成文本以它结尾时停止。第四，`max_tokens`。输出达到上限时停止，`finish_reason` 记为 `length`。第五，`max_model_len`。prompt 加输出的总长度超过模型上限时，要么拒绝请求，要么截断；静默截断是最危险的做法。第六，外部取消或超时。用户断开连接、请求被 abort、SLA 超时，都应该尽快释放资源。

`min_tokens` 是一个常被忽略的参数。它要求生成至少这么多 token 后才允许 EOS；对“必须输出完整 JSON”或“必须给出理由”的场景很有用。`ignore_eos` 则相反：忽略 EOS，强制生成到 `max_tokens`，常用于 benchmark 或固定长度压测。两者不能同时滥用：`min_tokens` 太大会让本应结束的请求继续生成，`ignore_eos` 会让模型在自然结束点之后胡言乱语。

stop 字符串的实现有一个坑：它可能跨 token 边界。模型可能先输出 `"</"`，再输出 `"answer>"`，两个 token 合起来才是 `"</answer>"`。如果每个 token 解码后单独检查，会漏掉跨边界的匹配。正确的做法是维护一个解码缓冲区，检查缓冲区末尾是否出现 stop 串；或者在流式输出时保留“最长 stop 串长度减一”的 holdback，等后续 token 到达再决定是否输出。mini 的 `Sequence.check_stop` 接收完整的 `output_text`，用 `endswith` 判断；`_append_tokens` 在命中 stop 后停止继续追加，但它不会回滚已经追加的 token；最终的 `_make_output` 再把 `RequestOutput.text` 截到 stop 串之前。推测解码路径会额外做一步：`_append_tokens` 返回实际提交的 token 数，`StepResult.tokens` 和流式 delta 只包含这个前缀，因此 verifier 多算的 token 不会被发给客户端。这个实现仍然不够细：触发 stop 串的那个 token 已经提交，最终的 `text` 会把它裁掉，所以“流式拼接的文本”和“非流式返回的文本”可能差一个 stop 后缀。生产实现要在流式侧保留“最长 stop 串长度减一”的 holdback，确认后续字符不构成 stop 后再释放；否则客户端会看到本应被隐藏的结束标记。

`finish_reason` 的语义要统一。`stop` 表示自然结束（EOS 或 stop 串），`length` 表示达到长度上限，`abort` 表示用户取消，`structured_output` 表示 grammar 无法继续。计费、重试、评测都依赖这个字段：把 `length` 当成 `stop` 会让“被截断”的请求看起来成功；把 `abort` 当成错误会让用户取消计入错误率。第 25 章的 server 和附录 C 的 API 参考会给出字段定义。推理模型还会多出“思考预算耗尽”和“答案预算耗尽”两种结束原因，第 39 章会把它们纳入同一套输出契约。

长度控制还要和 KV 容量对齐。`max_model_len` 是模型能力上限，`max_tokens` 是本次请求的输出上限，`max_num_batched_tokens` 是单步预算，三者不是一回事。一个请求的 prompt 加输出不能超过 KV 能容纳的 token 数；调度器在准入时就要检查，而不是等到分配 block 时才发现 OOM。mini 的 `EngineConfig` 在启动时检查 `max_model_len <= block_size × num_blocks`，这是一个最小但重要的防线。

## 11.6 logprobs、top_logprobs 与计费口径

logprobs 是很多应用依赖的能力：分类、置信度过滤、强化学习采样、调试模型行为。它的语义必须说清楚：返回的是“最终采样分布下该 token 的对数概率”，还是“原始 logits 经过 softmax 后的对数概率”？两者在开启温度、top-k、top-p 或 grammar mask 后可能不同。

一个合理的约定是：logprobs 对应最终用于采样的分布，也就是经过温度、mask、top-k/top-p 并重新归一化后的分布。这样调用方拿到的概率和实际采样一致。mini 的 `sample_token` 在 `distribution` 之后计算 `log(probs[token])`，符合这个约定；`top_logprobs` 返回最终分布中概率最高的 k 个 token。注意，如果 top-p 把某个 token 截断了，它的 logprob 应该是负无穷或不出现在列表里，而不是原始分布的分数。

prompt logprobs 是另一类需求：给定 prompt，返回每个 prompt token 在模型下的对数概率。它常用于计算困惑度、检测异常输入、做数据筛选。prompt logprobs 需要一次 prefill 前向，成本可能很高；生产实现通常只在显式请求时计算，并限制长度。

usage 字段是计费和对账的基础。一个响应至少应该包含 `prompt_tokens`、`completion_tokens`、`total_tokens`；如果支持缓存，还应该有 `cached_tokens` 或 `cache_hit_tokens`；如果支持 reasoning，还应该有 `reasoning_tokens`。这些数字应该由引擎在生成过程中累计，而不是在响应前重新编码文本估算。mini 的 server 现在直接使用 `RequestOutput.prompt_tokens` 和 `RequestOutput.output_tokens`，不再重新编码；Chat 路径的模板 token 也计入 prompt。第 25.2 节会说明这个改动。生产实现同样要把引擎的计数直接带进响应，并在 usage 里区分缓存命中的输入 token。

为什么重新编码会错？第一，特殊 token 在解码时可能被跳过，重新编码不会还原；第二，BPE 的切分依赖上下文，decode 再 encode 不一定得到同一串 token；第三，流式输出可能包含 holdback，最终文本和 token 数不是一一对应。计费必须用引擎的实际计数，不能用文本长度反推。

缓存命中的计费是另一个细节。如果输入有 96% 命中前缀缓存，实际 prefill 计算只有 4%，但输入 token 数仍然是全部。定价可以把输入分成“缓存命中”和“未命中”两档，或者给缓存命中折扣；无论哪种，usage 里都要能区分，否则无法对账。第 30 章会把这件事写进单位经济模型。

## 11.7 流式输出与 UTF-8 边界

流式输出不是“每生成一个 token 就发一个字符串”。token 和字符不是一一对应：一个中文字符在字节级 tokenizer 里是 3 个 token，一个 emoji 可能是 4 个字节，一个 BPE token 可能包含多个字符。如果每来一个 token 就 `decode`，多字节字符会被截断，客户端看到替换字符 `�`。

正确的做法是维护一个字节缓冲区，只解码“完整的 UTF-8 前缀”，把不完整的尾部留到下一次。mini 的 `StreamingDetokenizer` 就是这么做的：`feed(token_id)` 把字节追加到缓冲区，`_complete_prefix` 找到最长的合法 UTF-8 前缀，解码后从缓冲区删除；`flush()` 在结束时处理剩余字节。测试 `test_streaming_detokenizer_handles_split_utf8` 覆盖了中文被拆成多个 token 的场景。

流式协议通常是 SSE：每个事件是 `data: {json}\n\n`，最后以 `data: [DONE]\n\n` 结束。事件里放 delta 还是累计文本，要在 API 文档里写清楚；OpenAI 风格用 delta，某些自研协议用累计。客户端断开连接时，服务端要能感知并取消生成，释放 KV；否则一个慢客户端会占着序列继续算，浪费 GPU。

停止串和结构化输出会影响流式的“可输出前缀”。如果 stop 串是 `"</answer>"`，在收到 `"</ans"` 时不能立刻输出，因为后面可能匹配；要保留足够长的 holdback。结构化输出也有类似问题：JSON 的增量解析需要知道当前是否在字符串里、是否已经完整；不能把半个对象直接发给客户端。生产实现通常把“已经确定的内容”和“可能被回滚的内容”分开，只发前者。

背压是流式的另一个工程问题。如果客户端读得慢，服务端写 socket 会阻塞；如果 handler 阻塞，生成线程可能继续往队列里塞 token，内存上涨。生产实现会用有界队列、写超时和丢弃/断连策略；mini 用无界 `queue.Queue`，在慢客户端下可能堆积。第 25 章会给出这个边界的讨论。

## 11.8 结构化输出与推测解码对采样的影响

结构化输出把 grammar mask 加到 logits 上：非法 token 的分数被置为负无穷，softmax 只在合法 token 上归一化。这个操作必须在采样之前完成，而且要在温度、top-k/top-p 之前或之后保持一致。mini 的 `distribution` 先应用 mask，再做温度和 top-k/top-p；如果 mask 之后所有 token 都是负无穷，会抛出 `ValueError`，而不是返回一个空分布。

mask 会改变 top-p 的语义。原始分布可能很平，但 mask 之后只剩少数合法 token，累计概率很快达到 1；如果先做 top-p 再做 mask，可能会把唯一合法的 token 截掉。正确的顺序是“先 mask，再整形”，并且整形后至少保留一个合法 token。EOS 也要受 grammar 控制：只有当前状态允许结束时，才把 EOS 加入合法集合；否则模型会在 JSON 中间提前结束。

推测解码对采样的要求更严格。标准投机采样假设 draft 和目标使用同一个分布：draft 从 `q` 采样，目标用 `p` 验证，接受概率是 `min(1, p(x)/q(x))`；如果拒绝，从归一化的 `max(0, p-q)` 采样。这个证明要求两边的温度、top-k、top-p 完全一致。如果 draft 用 top-p=0.9、目标用全分布，或者一边应用了 grammar mask、另一边没有，接受规则就不再保持目标分布。

mini 的 `target_probs` 明确对 draft 和目标应用同样的 top-k/top-p，`generate_drafts` 和 `verify_drafts` 都用它；这是保证分布正确性的关键。第 8 章推导过接受规则，第 23 章会展示代码。贪心是随机采样的极限：`p` 和 `q` 都是 one-hot，接受规则退化成“argmax 相同就接受”，输出和目标贪心逐 token 一致。

结构化输出和推测解码的组合更难，因为拒绝一个 draft 不仅要回退 KV 的逻辑长度，还要回滚 grammar matcher 的状态。mini 没有一刀切关闭推测解码：贪心 + grammar 走完整路径，验证前深拷贝 matcher，验证过程只推进副本，用它为每个候选位置生成合法的 token mask；只有真正被接受的 token 才会在提交阶段回放到真实的 matcher 上。这样拒绝时 KV 和文法状态都不前进，接受时两者按同一顺序前进。非贪心 + grammar 则退回普通 decode，因为 draft 采样没有应用 grammar mask，直接验证会让接受分布有偏；生产实现要把 mask 同时加进 draft 采样器和目标验证器，才能在这个组合上安全地获得推测收益。测试 `test_speculative_with_structured_output_matches_target` 专门覆盖贪心组合，断言推测输出和纯目标贪心逐 token 一致。第 9 章和第 23 章会详细讨论回滚顺序。

## 11.9 采样器的工程实现

采样器在 GPU 上的实现和 NumPy 版本完全不同。一个 batch 里每个序列的采样参数不同，词表可能有 10 万到 20 万项；如果每个序列单独跑一个 kernel，launch 开销会很大；如果把整个 batch 的 logits 拷回 CPU，传输和同步会吃掉收益。生产实现通常把采样做成一个 fused kernel：按序列 gather 温度、top-k、top-p、seed、mask，按行做 softmax 和截断，最后写出 sampled token 和 logprob。

CUDA Graph 对采样器有额外约束。Graph 捕获时形状固定，采样参数必须放在固定地址的 tensor 里，不能每步重新分配；top-k 的 `k` 如果是动态的，要用 mask 或分段 kernel 处理。推测解码的验证长度也是动态的，通常要把“最大验证长度”固定，用 mask 屏蔽多余位置。第 17 章会讲 CUDA Graph 的一般约束，这里只强调：采样器不是“一个可以随便加分支的 Python 函数”，它的形状和分支会影响能不能进 Graph。

采样器还要和调度器对齐。`SamplerOutput` 里的 `num_generated_tokens`、`num_verified_tokens`、`so_status` 决定下一步的 `seqlen_q`；如果采样器返回的 token 数和调度器预期不一致，KV 记账会错位。mini 的 `StepResult` 用 `computed_end` 表达这个关系：采样出的 token 可能比验证的 token 少（拒绝），也可能多（bonus），但 KV 只前进到实际接受的位置。这个“输出长度”和“KV 长度”的差异，是推测解码和普通解码都必须处理的不变量。

采样的性能预算通常不大：decode 一步 8 毫秒，采样可能只占 0.5 到 1 毫秒。但如果开启 logprobs、top_logprobs、结构化 mask 或大词表，采样可能成为瓶颈。优化顺序是：先把 mask 做成位图/GPU kernel，再把 top-k/top-p 融合进 softmax，最后才考虑把整个采样器写成一个 persistent kernel。不要在没有 profile 的情况下重写采样器。

## 11.10 常见故障与验收

采样相关的事故通常有十类。

第一，NaN 或 inf logits。模型输出异常、低精度溢出、mask 全负无穷，都会让 softmax 产生 NaN。防护是检查 `np.isfinite`，在 mask 后至少保留一个合法 token，并在采样前做数值稳定化。

第二，top-p 返回空集合。浮点误差或 `p=0` 会导致没有 token 满足条件；实现要强制保留至少一个 token，并在参数校验时拒绝 `top_p<=0`。

第三，temperature 为负或过大。负温度会让分布反向，过大温度会让分布接近均匀；参数校验要在请求进入时拒绝非法值，而不是在采样时崩溃。

第四，seed 溢出或碰撞。seed 超出 uint64、不同请求共用 seed、派生规则不稳定，都会破坏可复现性。要在 API 层校验范围，在引擎层记录实际使用的 seed。

第五，stop 串跨 token 漏匹配。要维护解码缓冲区，不能逐 token 独立匹配；流式输出要保留 holdback。

第六，EOS 被 ignore_eos 或 min_tokens 错误覆盖。要明确优先级：`ignore_eos` 只忽略 EOS，不影响 stop 串和 max_tokens；`min_tokens` 只延迟 EOS，不阻止用户取消。

第七，tokenizer 与模型不匹配。词表大小、特殊 token id、BOS/EOS 不一致会让采样出的 token 解码成乱码；manifest 要在启动时校验（第 34 章）。

第八，logprob 与采样分布不一致。开启 top-k/top-p 后仍返回原始分布的 logprob，会让调用方的置信度和实际输出不符；要统一口径并在文档里写明。

第九，流式重复或丢字。delta 和累计文本混用、UTF-8 缓冲区处理错误、stop holdback 过早释放，都会导致客户端看到重复或缺失的字符。

第十，结构化输出 mask 与推测解码的回滚不一致。拒绝 draft 后 grammar 状态没有回滚，下一次 mask 会基于错误状态，输出可能非法或卡死。

对应的验收测试应该覆盖：固定 seed 的确定性、批量与逐条的一致性、temperature 0 的贪心、top-k/top-p 的分布形状（可以用卡方检验或简单的边界断言）、停止条件的每种组合、UTF-8 跨 token、logprob 与采样分布一致、mask 后至少一个合法 token、推测贪心与目标贪心逐 token 一致。mini 的 `tests/test_engine.py`、`tests/test_tokenizer.py`、`tests/test_speculative.py` 和 `tests/test_structured_output.py` 覆盖了其中大部分；新增的 `tests/test_sampling.py` 会把分布整形、seed 和停止条件单独测一遍。

## 11.11 mini 实现导读

读 mini 的采样代码，建议按这个顺序：

1. `sampling_params.py`：看参数校验、`greedy` 属性、seed 截断和 `copy()`。重点理解“temperature 接近 0 时关闭 top-p/top-k”这条规则。
2. `sampling.py`：看 `apply_top_k_top_p`、`distribution`、`sample_token`、`target_probs`。重点理解 mask、温度、截断、归一化的顺序，以及 `target_probs` 为什么对 draft 和目标用同一套整形。
3. `tokenizer.py`：看 `StreamingDetokenizer`。重点理解“只解码完整 UTF-8 前缀”的缓冲逻辑。
4. `sequence.py`：看 `check_stop` 和 `finish_reason`。重点理解 EOS、stop token、stop 串、min_tokens、max_tokens 的优先级。
5. `engine.py`：看 `_sample` 和 `_append_tokens`。重点理解采样结果如何写回序列、如何触发停止、`computed_end` 如何与 KV 对齐。
6. `server.py`：看 SSE 和 usage 字段。重点理解流式事件、连接关闭和计费口径的差距。

运行下面的命令，观察采样参数对输出的影响：

```bash
python examples/sampling.py
python -m mini_deepinfer.cli generate "DeepInfer is" --max-tokens 24 --temperature 0 --stats
python -m mini_deepinfer.cli generate "DeepInfer is" --max-tokens 24 --temperature 0.8 --top-p 0.9
```

`examples/sampling.py` 会固定一组 logits，分别展示 greedy、temperature、top-k、top-p、seed 复现、停止条件和流式解码的结果。它不依赖模型权重，适合作为采样行为的“显微镜”。

## 11.12 一个可以手算的采样例子

取三个 logits：`z=[2.0, 1.0, 0.0]`，比较三个温度下的分布和 top-p=0.9 的候选集。这个例子小到可以在纸上算完，适合用来检查你对“温度改变形状、top-p 改变候选数”的理解。

温度为 1.0 时，`exp(z)=[7.389, 2.718, 1.000]`，和是 11.107，概率是 `[0.665, 0.245, 0.090]`。按概率降序累加：0.665，0.910，1.000。累计值首次达到 0.9 是在第二个 token，所以 top-p=0.9 保留前两个，候选集的概率质量是 0.910，重新归一化后是 `[0.731, 0.269]`。注意边界 token 要保留：如果只保留“累计值小于 p”的 token，就只剩第一个，候选集会偏小。

温度为 0.5 时，`z/T=[4, 2, 0]`，`exp=[54.598, 7.389, 1.000]`，概率是 `[0.867, 0.117, 0.016]`。累计 0.867，0.984，1.000；top-p=0.9 仍然保留两个，但第二个的权重从 0.269 降到 0.119。温度降低让分布更尖，候选数不一定变少，但尾部 token 的权重更小。

温度为 2.0 时，`z/T=[1, 0.5, 0]`，`exp=[2.718, 1.649, 1.000]`，概率是 `[0.506, 0.307, 0.186]`。累计 0.506，0.813，0.999；top-p=0.9 需要保留三个，因为前两个只覆盖 0.813，没到 0.9。温度升高让分布变平，同样的 top-p 会保留更多候选。这就是“top-p 的候选数随分布形状自适应”的具体含义。

如果再加 top-k=2，顺序会影响结果。先 top-k 再 top-p：温度为 2 时先保留前两个（0.506、0.307），累计 0.813，top-p=0.9 想保留第三个但已经被 top-k 截掉，最终只剩两个。先 top-p 再 top-k：top-p 保留三个，top-k 再截到两个，结果一样。这个例子恰好一样，但在其他分布下可能不同；生产框架要把顺序写死并在文档里说明，评测时也要固定顺序。

最后检查 seed 和并列。如果两个 token 的概率完全相同，top-k 的边界落在它们之间时，保留哪一个取决于排序的 tie-break；如果实现按“概率降序、概率相同按 token id 升序”，结果可复现；如果依赖哈希或并行归约的顺序，同样的 seed 也可能给出不同 token。第 10 章的确定性讨论在这里落地：可复现不只是“固定 seed”，还要固定排序和随机数消耗顺序。

## 小结

采样是把 logits 变成用户可见文本的最后一段，也是正确性、可复现性、API 语义和计费的交汇点。温度、top-k、top-p 决定分布形状；seed 和每序列 RNG 决定可复现性；停止条件和流式解码决定用户体验；logprobs 和 usage 决定可观测性与对账；grammar mask 和推测解码决定分布约束与回滚。mini 用一组纯函数和小状态机实现了这些机制，边界清晰、可测试。下一章进入引擎总体架构，看采样器如何嵌入一次完整的 step。

## 练习

1. 给定 logits `[2.0, 1.0, 0.5, -1.0]`，分别计算 temperature=0.5、1.0、2.0 下的 softmax 分布，并说明 top-p=0.9 时保留哪些 token。
2. 解释为什么“每个序列一个 RNG”能保证 batch invariance，而“全局一个 RNG”不能。如果必须用全局 RNG，怎样设计才能让结果与 batch 顺序无关？
3. 设计一个 stop 串跨 token 的测试：stop 串是 `"</answer>"`，模型输出被拆成 `"</"`、`"ans"`、`"wer>"` 三个 token。写出缓冲区逻辑和期望的流式输出。
4. 为什么推测解码要求 draft 和目标使用相同的温度、top-k、top-p？如果 draft 多应用了一次 top-k，接受规则会怎样偏离目标分布？
5. 一个请求返回了 `finish_reason="length"`，但客户端把它当成成功。设计三个指标，把“被截断”的请求从成功率里区分出来。
6. 用 `examples/sampling.py` 的固定 logits，验证 top-k 和 top-p 在不同温度下的候选集合，并解释为什么 top-p 的候选数会随温度变化。

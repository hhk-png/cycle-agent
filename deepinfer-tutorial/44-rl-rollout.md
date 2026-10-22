# 第 44 章 强化学习推理与 rollout 引擎

本章导读：前面四十多章把推理引擎当成“服务用户”的系统：请求进来，token 流出去，优化目标是 TTFT、TPOT、goodput 和单位成本。强化学习后训练把同一个引擎变成“训练器的采样器”：一个 prompt 要生成一组回答，训练器需要行为策略的 log-probability，权重每隔几步就更新，rollout 还可能被挂起、恢复或丢弃。本章解释这些差异如何改变调度、KV、正确性和成本模型，并用 `mini_deepinfer/rl_rollout.py` 把组规划、GRPO 优势、权重版本和部分 rollout 跑成可测试的代码。

## 44.1 推理引擎为什么会出现在训练循环里

大模型的后训练通常分两步：先做监督微调，再用强化学习或偏好优化调整行为。无论用 PPO、GRPO 还是基于验证器的 RL，训练循环都需要“让当前策略对一批 prompt 生成回答，然后给回答打分”。这个生成步骤叫 rollout，它消耗的 GPU 时间往往比训练本身还多。

以 GRPO（Group Relative Policy Optimization）为例，一个训练 step 大致是：

```text
prompt batch
  -> 当前策略为每个 prompt 生成 G 个回答
  -> 奖励模型或验证器给每个回答打分
  -> 组内归一化得到优势
  -> 用优势更新策略
  -> 新权重同步到 rollout 引擎
```

这条链路里，rollout 引擎要做的事和在线服务高度相似：tokenize、prefill、decode、采样、KV 管理、批处理。但目标函数不同。在线服务关心“用户等多久”，rollout 关心“单位时间产出多少可训练样本”；在线服务可以丢弃慢请求，rollout 要保证每个 prompt 的组完整；在线服务的权重在几小时或几天内不变，rollout 的权重可能每几分钟就换一次。

DeepInfer 本身是面向 V4.1-Flash 生产服务的引擎，公开资料没有把它描述成 RL 训练框架。但它的调度、KV、低精度和并行机制可以被 rollout 复用；反过来，理解 rollout 的约束也能解释为什么生产引擎要提供 logprob、确定性采样和 KV 命名空间。本章讨论的是“如果要用这类引擎做后训练，需要补什么”，不是声称 DeepInfer 已经内置了训练循环。

## 44.2 rollout 与 serving 的五个差异

把两者放在一起比较，差异集中在五处。

第一，**输出是组，不是单条**。一个 prompt 要采样 G 条回答，通常 G 取 4 到 64。组内的 prompt 前缀完全相同，因此 prefill 可以只做一次；如果引擎按请求独立调度，就会把同一段前缀算 G 遍。组感知调度是 rollout 的第一项优化。

第二，**需要行为策略的 logprob**。PPO 类算法要计算重要性采样比 `π_new(a|s) / π_old(a|s)`，因此必须拿到“生成这些 token 时，旧策略给它们的对数概率”。这不是可选的调试字段，而是训练信号的一部分。采样器必须返回每个 token 的 logprob，并且这个 logprob 要和实际采样分布一致（经过温度、top-k、top-p、mask 之后）。

第三，**权重会变**。在线服务的模型版本以天或周计；rollout 的权重每个训练 step 都可能更新。旧权重算出的 KV 不能给新权重使用，否则等于用错误的状态做 attention。引擎需要把权重版本写进 KV 命名空间，或者在同步时显式失效缓存。

第四，**允许部分完成**。一条 rollout 可能因为 token 预算、时间预算或调度压力被挂起，等训练器处理完其他样本后再恢复。挂起不是取消：已经生成的 token、KV、采样状态都要保留。部分 rollout 能提高 GPU 利用率，但会引入 staleness（样本来自旧策略）和状态管理复杂度。

第五，**成本按样本和组计，不按用户请求计**。rollout 的产出是“可训练的轨迹”，不是“用户看到的回答”。奖励模型、验证器、重复采样和丢弃的样本都要计入成本。一个组里如果所有回答奖励相同，优势全为零，这组 rollout 对训练没有贡献，但 GPU 时间已经花掉了。

## 44.3 GRPO 的组相对优势

GRPO 的核心是用组内相对奖励代替价值模型。对一个 prompt 的 G 个回答，奖励是 `r_1 ... r_G`，优势定义为：

```text
A_i = (r_i - mean(r)) / (std(r) + eps)
```

这个定义看起来简单，但有几个工程含义。

第一，**组是统计单位**。如果组不完整（例如只生成了 3 条就超时），均值和标准差会有偏，优势也会偏。引擎要么补齐组，要么把不完整的组标记为不可训练，不能悄悄用 3 条算 4 条的统计。

第二，**零方差组没有信号**。如果 G 条回答奖励完全相同，`std=0`，所有优势都是 0。除法要加 `eps` 避免 NaN，但更重要的是把这类组记录下来：它们说明任务太简单、太难或奖励函数没有区分度。`group_advantages` 在标准差接近 0 时返回全零数组，而不是抛异常，因为零优势是合法结果，只是浪费。

第三，**奖励的尺度不重要，组内相对关系重要**。这让 GRPO 不需要训练一个与策略同步的价值网络，但也意味着奖励函数的偏置会直接进入优势。如果奖励模型偏好长回答，组内所有回答都会变长，优势只反映“谁更长”，而不是“谁更好”。

`examples/rl_rollout.py` 用奖励 `[0.2, 0.8, 0.5, 1.0]` 算出的优势是 `[-1.4021, 0.5774, -0.4124, 1.2372]`，均值 0、标准差 1。这个结果可以直接手算：均值 0.625，总体标准差是 `sqrt(mean((r-0.625)^2)) ≈ 0.3031`，`(1.0-0.625)/0.3031 ≈ 1.237`，和输出的 1.2372 一致。注意这里用的是除以 G 的总体标准差，不是除以 G-1 的样本标准差；如果实现换成 `ddof=1`，优势会整体放大，训练超参也要跟着调。教学实现保留这个可手算性，是为了让你在调试训练时能判断“优势是不是算错了”。

## 44.4 组调度与前缀共享

一个 rollout 请求的最小单位是“prompt 组”。组内 G 条样本共享同一段 prompt，所以：

```text
prefill_tokens(group) = prompt_tokens          # 共享前缀
decode_tokens(group)  = G × output_tokens
KV_token_steps(group) = G × (prompt × output + output × (output-1) / 2)
```

第三项是三角形求和：第 1 个生成 token 读到 prompt 长度，第 2 个读到 prompt+1，依此类推。它决定了 KV 带宽和显存占用，而不是简单的 `G × (prompt+output)`。`estimate_rollout_cost` 把这三项分开返回，就是为了避免“只算 decode token，忽略 KV 读放大”的错误。

组调度的目标是把组完整地放进一个 wave，同时不超过 token 预算。`plan_groups` 按 `prompt + G × output_budget` 预留 token，按预留量从大到小装 wave。大组先放的理由不是“优先”，而是减少碎片：一个 2400 token prompt、G=4、输出 256 的组预留 3424 token；如果先放一堆小组，最后可能剩下 3000 token 的空隙，大组放不进去，只能单开一个利用率很低的 wave。

组内前缀共享还有一个边界：如果引擎的 block size 是 128，prompt 长度不是 128 的倍数，最后一块不能共享，G 条样本都要各自重算尾部。`plan_groups` 只算逻辑 token，不模拟 block 对齐；生产实现要把 `ceil(prompt/block_size)` 和尾部重算算进去。命中率越高、组越大，共享的收益越明显；短 prompt 或组很小时，组调度的开销可能超过收益。

## 44.5 logprob、采样分布与行为策略

训练器要的是“旧策略在给定上下文下采样出这个 token 的概率”。这个概率必须和引擎实际采样时使用的分布一致。假设请求开了 temperature=0.8、top-p=0.95、grammar mask，那么 logprob 应该是“经过温度、mask、top-p 并重新归一化后的分布”下该 token 的对数概率，而不是原始 logits 的 softmax。

这带来三个实现要求。

第一，采样器要返回 logprob，而不是只返回 token id。很多在线引擎为了省带宽默认不返回 logprob；rollout 必须打开它，并保证返回值和采样分布同源，不能事后用另一套代码重算。

第二，draft/推测解码会改变分布路径。如果 rollout 开了推测解码，接受/拒绝规则必须保持目标分布；logprob 也要对应最终提交的 token。如果实现只在“目标模型自己采的 token”上返回 logprob，被接受的 draft token 就会缺 logprob，训练器无法计算重要性采样比。

第三，结构化输出的 mask 会改变分布。带 grammar 的 rollout 在计算 logprob 时，分母只应包括合法 token；否则 logprob 会偏小，重要性采样比会偏。训练和推理必须使用同一份 grammar 和 tokenizer 版本，否则“同一个 token”在两边的概率不同。

一个实用的验收方法是：固定 seed 和采样参数，让 rollout 引擎生成一条轨迹，同时用参考实现（不优化、逐 token 计算）算同一组 logprob，比较两者在 1e-5 内一致。这个测试比“输出文本一样”更能抓住采样分布的错误。

## 44.6 权重同步与 KV 命名空间

rollout 引擎的权重不是静态的。一次训练 step 结束后，新权重要通过 NCCL/HCCL 广播、共享内存或参数服务器同步到 rollout worker。同步的粒度可以是全量、分层或增量，但无论哪种，都必须回答一个问题：旧 KV 还能不能用？

答案是不能。KV 是每一层用旧权重算出的 Key/Value；新权重的 attention 读旧 KV，等于把两个模型的中间状态混在一起。表现形式不是崩溃，而是输出质量下降、logprob 失真、训练不收敛。这类 bug 很难从 loss 曲线看出来，因为它在数学上“能跑”。

`KVNamespace` 把 `model_id`、`weight_step`、`tokenizer_hash`、`template_hash` 和 `policy` 拼成一个命名空间，再和前缀哈希一起做 SHA-256。权重步数一变，所有 key 都变，旧缓存自然 miss。`WeightVersion.bump()` 产生新版本，`staleness()` 计算 rollout 相对训练器的步数差，`acceptable()` 判断是否超过允许的 staleness。

权重同步的工程顺序也很重要：

1. 训练器完成一步更新，产生新版本号；
2. rollout worker 进入 drain 或暂停接收新组；
3. 广播新权重，校验哈希和形状；
4. 提升版本号，切换 KV 命名空间；
5. 丢弃或归档旧命名空间的缓存；
6. 恢复接收新组。

如果顺序反了（先切命名空间再传权重），中间会有一段时间用新 key 读旧权重；如果先丢缓存再传权重，失败时无法回滚。生产实现通常用双缓冲：新旧权重各一份，切换是指针交换，失败时切回旧指针。

## 44.7 部分 rollout、挂起与 staleness

部分 rollout 的动机是“不要让一个长样本阻塞整个 batch”。一条回答可能生成几千个 token，如果它所在的组还没完成，GPU 的其他槽位可能空着。部分 rollout 允许在 token 预算用完时挂起这条样本，把槽位让给别的组，等训练器处理完其他样本后再恢复。

`PartialRollout` 维护 `generated`、`budget`、`suspended`、`finished` 四个状态。`advance(n)` 最多消耗剩余预算；`suspend()` 后 `advance` 返回 0；`resume()` 恢复。这个状态机看起来简单，但生产实现还要保存：

- 已生成 token 的 KV；
- 采样器的 RNG 状态；
- grammar matcher 的状态；
- 停止条件的进度（例如 stop 串的 holdback）；
- 这条轨迹对应的权重版本。

如果挂起期间权重更新了，恢复时就有 staleness。`WeightVersion.acceptable(trainer_step, max_staleness_steps)` 给出一个显式判断。staleness 不是越大越差：适度的 off-policy 数据可以提高样本利用率，但重要性采样比会变陡，训练方差会上升。一个实用的起点是 `max_staleness_steps=1`，并在训练日志里记录每个 batch 的 staleness 分布，而不是只看平均值。

挂起还会影响 KV 容量规划。挂起的 rollout 仍然占着 KV，如果大量样本同时挂起，显存会被“暂停的轨迹”占满。生产系统会给挂起样本设置 TTL、优先级和最大挂起数；超过限制的样本要么丢弃，要么落盘到 KV store。丢弃是合法的，但要在训练报告里体现，不能悄悄减少组的大小。

## 44.8 成本与吞吐模型

rollout 的单位成本可以用“每组多少 GPU 秒”表示：

```text
prefill_s = prefill_tokens / prefill_tps
decode_s  = G × output_tokens / decode_tps
kv_s      = G × (prompt×output + output×(output-1)/2) × kv_bytes / kv_bw
gpu_s     = prefill_s + decode_s + kv_s
cost      = gpu_s / 3600 × gpu_hour_cost
```

这个模型把 prefill、decode 和 KV 带宽分开，因为三者的瓶颈不同。prefill 受算力限制，decode 受权重带宽限制，KV 读受显存带宽限制。如果只用一个“总 token/s”折算，会掩盖“长上下文 + 大组”时 KV 读成为瓶颈的事实。

`rollout_throughput` 用“最慢阶段”估算稳态吞吐：prefill 和 decode 可以在不同组之间重叠，流水线速率由较慢的一侧决定。示例里 G=4、prompt=1200、output=256、prefill 6 万 token/s、decode 3000 token/s、并发 8，得到每组 0.3413 秒、23.44 组/s、93.75 样本/s、24000 token/s，瓶颈是 decode。这个结果说明：在这个工作点，增加 prefill 吞吐没有用，应该优化 decode 或增大并发。

但“最慢阶段”是一个理想模型。真实系统里，prefill 和 decode 共享 GPU，不能完全重叠；KV 读和权重读争同一份 HBM 带宽；奖励模型和验证器还要额外 GPU。生产容量规划要在这个下界上乘一个 1.3 到 2.0 的系数，并用真实 trace 校准。

奖励模型和验证器的成本经常被漏算。一个 7B 奖励模型对每条回答打分，成本可能和生成本身同量级；一个代码验证器要跑单元测试，成本取决于测试时长；一个人工审核环节更是数量级的差异。rollout 的总成本应该写成：

```text
total = generation + reward_model + verifier + retry + data_transfer
```

只算 generation 会低估 30% 到几倍，具体取决于任务。

## 44.9 与 serving 引擎的差异清单

把 rollout 的需求映射到引擎能力，可以得到一张检查表。

**调度**：要支持组感知的 admission、组内前缀共享、部分 rollout 的挂起/恢复、staleness 上限。在线引擎的连续批处理和 LCFS 可以复用，但“组完整”是新的约束。

**KV**：要支持按权重版本命名空间、挂起样本的 KV 保留、旧版本缓存的批量失效。分页、COW、前缀哈希仍然适用，但“缓存命中”必须先通过版本校验。

**采样**：要返回与采样分布一致的 logprob，支持推测解码下的接受 token，支持 grammar mask。采样参数的冻结和复制比在线服务更严格，因为训练器要复现“当时为什么选了这个 token”。

**并行**：rollout 通常和训练共享集群，需要和训练任务协调 GPU 分配；权重同步要走高带宽互联；推理并行（EP/DP）和训练并行（TP/PP/FSDP）的拓扑可能不同，需要转换。

**可观测性**：要记录每组的大小、完成率、丢弃率、staleness、奖励分布、零方差组比例、logprob 校验失败率。在线服务的 TTFT/TPOT 仍然有用，但不能替代这些训练指标。

**安全**：rollout 的数据可能包含训练数据、用户数据和工具结果；权重同步通道是高价值攻击面；挂起的 KV 可能长期驻留。第 28、37 章的威胁模型同样适用，只是资产多了一份“训练轨迹”。

## 44.10 mini-deepinfer 的实现与实验

`mini_deepinfer/rl_rollout.py` 提供七个部件：`RolloutConfig`、`PromptGroup`、`GroupPlan`、`plan_groups`、`group_advantages`、`estimate_rollout_cost`、`rollout_throughput`，以及版本相关的 `WeightVersion`、`KVNamespace` 和 `PartialRollout`。它们不依赖引擎，可以单独测试。

运行：

```bash
python examples/rl_rollout.py
python -m pytest tests/test_rl_rollout.py -q
```

一次运行的输出包括：两个 wave 的组划分；奖励 `[0.2, 0.8, 0.5, 1.0]` 的优势 `[-1.4021, 0.5774, -0.4124, 1.2372]`；G=4、prompt=1200、output=256 的组成本 `prefill_tokens=1200`、`decode_tokens=1024`、`kv_token_steps=1359360`、`gpu_seconds=0.3625`、`cost_usd=0.000561`；并发 8 时 23.44 组/s、93.75 样本/s、24000 token/s、瓶颈 decode；权重从 100 升到 101 后 KV key 改变；部分 rollout 挂起后 `advance` 返回 0，恢复后完成。

这些数字是确定性的，用来对照公式；它们不代表真实训练集群的吞吐。mini 没有实现真正的权重同步、奖励模型、训练器和分布式 KV；它证明的是“组、优势、版本、挂起”这四个状态机的逻辑。

## 44.11 常见故障

**组不完整。** 引擎因为超时或抢占只生成了 G-1 条，训练器却按 G 条算均值和标准差。防护是给组一个明确的 `complete` 状态，不完整的组不进入优势计算。

**logprob 与采样分布不一致。** 采样用了 top-p，logprob 却按原始分布算；或者被接受的 draft token 没有 logprob。防护是用同一套 `distribution` 函数生成采样分布和 logprob，并做参考实现对照。

**旧 KV 被新权重读取。** 权重更新后没有切换命名空间，attention 读到旧 Key/Value。防护是把 `weight_step` 写进 KV key，版本变化即 miss，并在启动/同步时断言。

**挂起样本泄漏。** 挂起的 rollout 没有 TTL，长时间占着 KV；或者恢复时 RNG/grammar 状态丢失，输出和挂起前不连续。防护是把挂起状态做成显式对象，记录所有状态和版本，设置最大挂起数和 TTL。

**零方差组被当成正常数据。** 一组回答奖励相同，优势全零，训练没有信号，但报告里只看到“完成了 G 条”。防护是统计零方差组比例，按任务和难度分层看。

**奖励模型成本漏算。** 容量规划只算生成，没算奖励模型、验证器和重试，实际成本翻倍。防护是把 `total = generation + reward + verifier + retry` 写进预算，并用真实 trace 校准。

**权重同步期间接新请求。** 同步过程中引擎仍在接收新组，导致一部分组用旧权重、一部分用新权重。防护是 drain、双缓冲和版本号原子切换。

## 44.12 奖励黑客与验证器攻击

RL 的目标函数是奖励，不是“好答案”。只要奖励和真实目标有缝隙，策略就会找到缝隙。常见的奖励黑客有三种。第一是长度黑客：奖励如果是“答案越长越好”或“包含关键词”，模型会堆砌长度、重复关键词，而不是解决问题。第二是格式黑客：奖励解析器只认某种格式，模型会输出合法但无意义的格式。第三是测试黑客：代码任务的奖励是单元测试通过，模型可能写死测试用例、读取测试文件，或者利用测试的漏洞。

验证器本身也是攻击面。代码执行环境如果和训练数据、模型权重在同一个文件系统里，模型可以通过生成的代码读取答案或修改测试；如果验证器有超时，模型可以用死循环换取“不通过但也不扣分”；如果测试是 flaky 的，模型会学会利用不稳定的用例。防御要把执行环境隔离（容器、只读文件系统、无网络、资源上限）、把测试和答案放在模型不可见的位置、对超时和崩溃给出明确的负奖励，并定期用对抗样本审计验证器。

奖励设计要尽量避免“可被优化的代理指标”。如果必须用代理（长度、格式、关键词），就要配合人工抽检、held-out 测试和 KL 约束，防止策略偏离得太远。GRPO 的组内相对优势会放大“组内差异”，如果奖励本身有噪点，优势也会被噪点主导；组大小、baseline 和奖励归一化都要和奖励的方差一起看。

最后，rollout 引擎能提供的安全边界有限。它能保证“采样可复现、KV 不串版本、logprob 一致”，但不能保证“奖励不可 hack”。奖励和验证器的设计属于训练侧，需要和评测、红队、产品一起迭代；把 rollout 当成“生成器”，不要把它当成“正确性保证”。

## 小结

RL rollout 把推理引擎从“服务用户”变成“服务训练器”。组采样让前缀共享成为第一项优化，GRPO 让组内相对优势成为训练信号，logprob 让采样分布必须可复现，权重更新让 KV 必须按版本命名空间，部分 rollout 让挂起和 staleness 成为一等状态。mini-deepinfer 用组规划、优势计算、成本模型、权重版本和挂起状态机验证了这些逻辑，但没有复制生产训练集群的权重同步和奖励模型。下一章把镜头转向另一个极端：不在数据中心，而是在端侧和异构设备上做推理。

## 练习

1. 一个 prompt 有 2000 token，组大小 G=8，输出预算 512。按共享前缀计算 prefill token、decode token 和 KV token-steps；如果每 token KV 是 890 字节，KV 读取量是多少？
2. 奖励是 `[0.1, 0.1, 0.1, 0.9]`，计算 GRPO 优势。这个组的方差小不小？训练时应该怎么处理？
3. 解释为什么权重更新后旧 KV 不能复用。如果只更新最后几层，前几层的 KV 能否复用？给出判断条件。
4. 设计一个 logprob 一致性测试：固定 prompt、seed、温度和 top-p，比较 rollout 引擎与参考实现的逐 token logprob。你需要固定哪些变量？
5. 部分 rollout 挂起时，除了 KV，还要保存哪些状态？如果挂起期间权重更新，恢复时应该怎么处理？
6. 用 `examples/rl_rollout.py` 把并发从 8 改成 32，观察瓶颈和吞吐怎么变；再增大输出长度，观察瓶颈是否从 decode 转向 KV。

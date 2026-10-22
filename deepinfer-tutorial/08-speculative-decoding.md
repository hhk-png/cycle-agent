# 第 8 章 推测解码 DSpark

本章导读：推测解码不是“用一个小模型替代大模型”，而是“用小模型提议、大模型并行验证”。本章推导接受概率、期望接受长度和加速比，解释 DSpark 的 Confidence Head 与可变长验证，最后说明 KV 回滚和结构化输出回滚这两个容易被忽略的正确性问题。

## 8.1 基本流程

普通 decode 每步：目标模型前向一次，采样 1 个 token。推测解码每步：

1. draft 模型自回归生成 `k` 个候选 token；
2. 目标模型把这 `k` 个候选和最后一个已接受 token 一起做一次前向；
3. 从目标分布中逐个验证候选，接受一个前缀，并在第一个拒绝处采样一个修正 token；
4. 如果全部接受，再额外采样一个 token。

一次目标前向可能产出 1 到 `k+1` 个 token。关键问题是：怎样验证才能保证最终输出仍然服从目标模型的分布？

这里有一个容易混淆的细节：目标模型验证的是“候选 token 对应的下一个位置分布”，所以输入的构造是“最后一个已提交 token + k 个 draft”，输出 `k+1` 行 logits。第 0 行验证 draft[0]，第 1 行验证 draft[1]，依此类推；如果全部接受，第 k 行用来采 bonus token。行数对不上是推测解码实现里最常见的 off-by-one。

## 8.2 接受与拒绝的数学

设 draft 分布为 `q(x)`，目标分布为 `p(x)`，候选 token 为 `x`。标准投机采样（speculative sampling）的规则是：

```text
以概率 min(1, p(x)/q(x)) 接受 x
如果拒绝，从归一化的 max(0, p - q) 中采样修正 token
```

可以证明，这样得到的 token 边缘分布正好是 `p`。直观理解：draft 多采样了 `q` 比 `p` 大的部分，拒绝时用 `p - q` 补回来。

证明可以写成两行。接受路径对 token `x` 的贡献是 `q(x) × min(1, p(x)/q(x)) = min(q(x), p(x))`。拒绝路径的残差分布是 `max(0, p(x) - q(x)) / Z`，它在 `p(x) > q(x)` 时非零，在 `q(x) >= p(x)` 时为零；也就是说，残差补的是“目标比 draft 多出来的概率质量”，不是反过来。拒绝概率 `Z = sum_y max(0, p(y) - q(y))`，因为 `sum p = sum q = 1` 时，`p` 超出 `q` 的总量正好等于 `q` 超出 `p` 的总量。把接受项和拒绝项相加：

```text
min(q(x), p(x)) + Z × max(0, p(x) - q(x)) / Z
= min(q(x), p(x)) + max(0, p(x) - q(x))
= p(x)
```

这个证明依赖三个假设：draft 和目标使用同一套温度、top-k/top-p；两边的 support 一致（不能一边把某个 token 截掉、另一边保留）；采样器使用的随机数语义一致。如果一边用 top-p、一边用全分布，或者 draft 多应用了一次 grammar mask，等式就不成立，接受规则不再保持目标分布。

对贪心解码（temperature 0），规则简化成：目标模型的 argmax 等于候选就接受，否则用目标的 argmax 修正。这时推测解码的输出和纯目标贪心解码**逐 token 完全一致**，只是步数更少。mini-deepinfer 的测试就是利用这一点验证正确性。

对随机采样，接受率取决于 `p` 和 `q` 的接近程度。draft 越像目标，接受率越高；draft 太弱，验证成本白花；draft 太强，又失去“小模型”的意义。

## 8.3 期望接受长度与加速比

设第 `i` 个 draft 的累计接受概率为 `c_i`（前 `i` 个都被接受的概率）。一次推测 step 的期望接受 token 数是：

```text
E = 1 + c_1 + c_2 + ... + c_k
```

`1` 是目标模型自己采样的那个 token（全部接受时的 bonus，或拒绝时的修正）。如果目标一步成本为 1，draft 每 token 的相对成本为 `r`，则：

```text
speedup ≈ E / (1 + k × r)
```

这个公式解释了几个现象。第一，`c_i` 衰减很快时，增大 `k` 的边际收益迅速下降。第二，`r` 越大（draft 越贵），最优 `k` 越小。第三，高并发时目标一步的固定开销被摊薄，`r` 相对变小，推测解码更划算。

用 `examples/roofline.py` 的 `speculative_speedup` 可以手算：每个位置的独立接受概率 `a=[0.9,0.8,0.7,0.6,0.5]`（累计 `c_i` 为 0.9、0.72、0.504、0.3024、0.1512）、`r=0.1` 时，`E=3.578`，加速比 `3.578/1.5=2.385`。如果把验证长度截到前 3 个，`E=1+0.9+0.72+0.504=3.124`，成本 `1.3`，加速比 `2.403`，反而略高。这说明“多验证一个 token”不一定划算，最优长度取决于边际收益和边际成本。

## 8.4 DSpark 的改进

DSpark 不是固定长度验证。官方 Q&A 说，draft 每步生成 5 个 token，Confidence Head 预测每个 draft token 的接受概率，调度时按期望接受率排序，选择使有效吞吐最大的前缀子集去验证。生产数据里，动态验证把每步验证的 draft 数从固定 5 降到 3.2 到 4.4，平均接受长度基本不变（3.29 对 3.20），在 EP64/DP64、每 rank 约 60 并发的工作点带来 4% 到 7% 的吞吐提升。公开报道还提到，DSpark 让每步需要验证的 draft token 数减少约 30%，这是同一个优化在不同口径下的表述：固定验证 5 个，动态验证平均 3.5 个，降幅约 30%。

benchmark 口径也要一起看。公开的 decode benchmark 使用 EP64DP64、nextn=3、接受率约 75%，在 256 请求/rank、131K 上下文时超过 25,000 输出 token/s/GPU，约 100 token/s/user；8 请求时可达 300 token/s/user。这里的 75% 是 draft token 的接受率，不是“每步一定接受 3 个”；实际接受长度还取决于置信度分布和工作点。把 75% 直接代进 `E = 1 + c1 + c2 + c3` 会高估，因为 c_i 是累计接受概率，不是独立接受率。

书里出现三个不同的 draft 长度，它们对应三个口径，不要混用。**默认配置**是 nextn=5，指框架默认最多提议 5 个 draft；**benchmark 配置**是 nextn=3，为了在特定工作点比较吞吐；**DSpark 机制描述**说 draft 通常生成 5 到 7 个 token，指模型和 confidence head 的设计范围。做 baseline 时要把 nextn 显式设成 0，否则你测到的“目标模型”其实带着推测解码；做容量规划时要用你实际配置的 nextn，而不是论文里的数字。

Confidence Head 的输入不是目标模型的输出，而是 draft 的 hidden state 和一个 Markov embedding。它要在目标验证之前给出“这个 draft 大概会不会被接受”的估计，所以不能偷看 `p(x)/q(x)`。DeepInfer 的 `models/mtp/dflash.py` 里，`ConfidenceHead` 是一个线性投影，`sample_drafts` 在每个 draft step 调用它，把 `confidence_rates` 存进 `MTPPredictInfo`。

这里有一个训练与推理的闭环：Confidence Head 的标签来自目标模型的实际接受结果，训练数据可以用离线采样生成；推理时它只做一次前向，成本很低。它的价值不是“预测得百分百准”，而是“把验证预算从低概率的 draft 上挪开”。

还有一层是 **block rejection sampling** 和 Markov head。公开资料提到 DSpark 使用半自回归 draft，draft 层之间有 Markov 依赖；这提高了 draft 的质量，但也让 draft 的前向更像一个小型自回归模型，而不是一次性并行预测。

## 8.5 变长验证怎么选

给每个 draft 一个逐位置接受概率 `a_i`（置信度），一个简单的策略是“只验证置信度高于阈值的前缀”。更精细的做法是最大化单位成本的期望接受长度。注意 8.3 节的 `c_i` 是累计接受概率，不是逐位置概率；两者关系是 `c_i = a_1 × a_2 × ... × a_i`：

```text
choose j = argmax_j (1 + sum_{i<=j} c_i) / (1 + j × r)
```

DeepInfer 的 `MTPVarlen` 在调度时做类似的计算，但它还依赖实测的硬件吞吐曲线：不同的 batch size 下，多验证一个 token 的边际成本不同，所以最优 `j` 也不同。官方说，异步调度开启时，`schedule(N+1)` 运行时第 N 步的 confidence 还没返回，CPU 只能先固定总预算，具体的每请求验证长度由 GPU 上的 TileLang kernel 重新计算（累乘、全局排序 top-k、融合 remap scatter）。

这带来一个工程上的矛盾：调度器想要“早知道”，但 confidence 是 GPU 算出来的。DeepInfer 的解法是把“总预算”和“每请求长度”分开：CPU 决定这一步最多验证多少，GPU 在拿到 confidence 后再分配。这样既保持了流水线，又不会因为等待 confidence 而停摆。

## 8.6 KV cache 与回滚

推测解码的正确性不只取决于采样，还取决于 KV cache。

目标模型在验证时会把最后一个已接受 token 和所有 draft token 的 K/V 写进 cache。如果某些 draft 被拒绝，这些位置的 K/V 就是“用错误的 token 算出来的”，不能留给下一步。处理方式有两种：

- **回滚**：把 `num_computed_tokens` 退回到已接受前缀的长度，后续写入覆盖被拒绝的位置；
- **不提交**：只把已接受 token 计入序列长度，KV 页表保留但逻辑长度不前进。

DeepInfer 的 `Scheduler.process_outputs` 用“乐观推进 + 按实际接受数回填”的方式：先按 `seqlen_q` 推进，再根据 `accepted_token_ids` 把 `cache.num_tokens` 修正到实际接受数。被拒绝 draft 的 K/V 不占逻辑长度，下一次写入会覆盖。

mini-deepinfer 采用同样的思路：`verify_drafts` 返回 `accepted` 数，引擎把 `computed_end` 设为 `old_length + accepted`，而不是把验证过的所有位置都算进去。测试会检查推测输出和纯目标贪心输出逐 token 一致。

这里有一个边界：如果接受了一个 stop token，序列长度可能小于 `old_length + accepted`。引擎必须把 `computed_end` 夹到 `seq.length`，否则 KV 记账会超过逻辑长度，下一步就会读到垃圾。第 23 章会展示这段代码。

## 8.7 结构化输出下的回滚

如果请求带 grammar mask，拒绝一个 draft 不仅要回滚 KV，还要回滚 grammar matcher 的状态。DeepInfer 的 `XGrammarMatcher` 支持 `rollback(steps)`，内部维护 `tokens_in_matcher`、`tokens_after_bound`、`trigger_progress` 等状态，并用 KMP 的 failure 表恢复触发词进度。

mini-deepinfer 现在支持两者组合，但做法和“在同一个 matcher 上直接回滚”略有不同。验证时它把 grammar matcher 复制一份，用副本逐位置计算 mask、接受或拒绝 draft；验证结束后，真正的 matcher 只按最终提交的 token 逐个前进。这样即使一个 stop token 提前截断了验证结果，matcher 也不会越过序列的实际长度。第 23 章会讲这段实现和 snapshot/restore 的关系。

为什么 grammar 回滚比 KV 回滚更难？因为 KV 是“位置 -> 数值”的覆盖，回滚只需要改逻辑长度；而 grammar 是“状态机”，回滚需要恢复状态集合、触发词进度、bound 计数、EOS 标记，任何一项漏掉都会让后续 mask 出错。生产实现通常要求 matcher 提供显式的 snapshot/restore，而不是靠“重放 token”恢复。

## 8.8 其他推测解码路线

DSpark 是“小 draft 模型 + 目标验证”的路线，但学术界和工业界还有几类变体，理解它们的差异有助于判断 DSpark 的取舍。

**Medusa** 在目标模型上挂多个解码头，每个头预测不同偏移的 token，不需要独立 draft 模型。它的优点是省掉 draft 的前向，缺点是头之间的依赖弱，接受长度可能不如自回归 draft。

**EAGLE** 用一个轻量 draft 模型在特征层做自回归，利用目标模型的 hidden state，接受率通常比独立小模型高。它的代价是 draft 与目标模型的耦合更深，换模型时要重新训练。

**Lookahead decoding** 不训练 draft，而是用 Jacobi 迭代和 n-gram 池并行生成候选。它适合没有 draft 模型的场景，但候选质量和额外计算量需要权衡。

**Prompt lookup / n-gram decoding** 直接从 prompt 或历史里复制候选，适合代码补全、检索增强等重复性高的负载。它几乎不增加模型计算，但接受率高度依赖输入。

DSpark 介于“独立 draft”和“目标耦合”之间：它有独立的 MTP/draft 层，但通过 Confidence Head 和半自回归结构提高质量。它适合 DeepSeek 自己的模型和硬件栈，不一定适合所有模型。

## 8.9 mini-deepinfer 的实现

`mini_deepinfer/speculative.py` 包含：

- `generate_drafts`：draft 模型自回归提议，返回 token、每个位置的 draft 分布、置信度；
- `expected_accepted_length`：按累计接受概率算期望接受**draft** 的长度，不含目标模型额外产出的 bonus token；`roofline.expected_accept_length` 则把 bonus 的 1 加进去，两者相差 1，测试里分别断言，正文引用时必须说明是哪一个。
- `choose_verify_length`：阈值 + 边际收益选择验证前缀；
- `verify_drafts`：目标模型一次前向，逐 token 接受/拒绝，全部接受时采样 bonus；
- `SpeculativeStats`：提议数、验证数、接受数、接受率。

运行 `python examples/speculative_decoding.py` 可以看到贪心输出与目标模型完全一致，以及接受率、平均接受长度。当前一次运行的结果是：28 个 draft、28 个验证、24 个接受，接受率 85.7%，平均接受长度 3.43，平均验证长度 4.0。CPU 上 draft 并不免费，墙钟时间不一定更快；这个例子用算法指标说明机制，而不是宣称 CPU 加速。

## 8.10 什么时候不该用推测解码

推测解码不是无条件的。以下情况收益可能为负：

- 目标模型很小，draft 的相对成本 `r` 很高；
- 并发很低，目标一步的固定开销占比大，多验证的边际成本不划算；
- draft 与目标分布差异大，接受率低；
- 结构化输出或强约束采样，回滚和 mask 的复杂度抵消收益；
- 显存紧张，draft 模型和额外 KV 挤占并发。

判断是否启用，应该看“接受长度 / 验证成本”的实测曲线，而不是只看论文里的加速比。一个实用的上线策略是：先在离线的固定负载上扫 `k` 和 confidence 阈值，找到单位成本最低的工作点；再在线做 A/B，观察 P99 TPOT 和 goodput，而不是只看平均接受率。

## 8.11 DSpark 的工程边界

DSpark 的收益不是“接了 draft 就有”。它有四个工程边界，任何一项不满足，收益都可能变成负数。

第一，**Confidence Head 要校准**。它的输出是“这个 draft 会被接受的概率”，不是“draft 自己的概率”。如果训练数据和线上分布不一致，它会系统性高估或低估；高估会把预算浪费在会被拒绝的 draft 上，低估会错过高接受率的 draft。校准要用目标模型的实际接受结果做标签，并在上线后持续监控预测值和实际接受率的偏差。

第二，**draft 成本要可测**。公式里的 `r` 是 draft 每 token 的相对成本，它随 batch、序列长度、硬件和 kernel 变化。小 batch 时 draft 的固定开销占比高，`r` 可能远大于 0.1；大 batch 时 `r` 下降，推测才划算。DeepInfer 用 decode perf cache 按 batch 存实测耗时，就是为了让调度器用真实成本曲线，而不是一个拍脑袋的常数。

第三，**验证长度要和 batch 预算一起分配**。多验证一个 draft 会占用本可以分给其他请求的 token 预算；在 decode batch 已经很大时，增加验证长度可能让 TPOT 变差，即使平均接受长度上升。一个实用的策略是把“多验证一个 token”的边际收益和“多接一个请求”的边际收益放在同一个预算里比较，而不是单独优化接受长度。

第四，**结构化输出和推测解码的组合要处理回滚**。拒绝一个 draft 不仅要回滚 KV，还要回滚 grammar matcher 的状态。生产实现要求 matcher 提供 snapshot/restore；如果只能重放 token 恢复，回滚成本会抵消推测的收益。第 9、23 章会讲 matcher 的状态管理。

最后，推测解码的评测不能只看平均接受率。要看 P99 TPOT、goodput、每 token 成本，以及在不同长度、不同并发下的曲线。一个接受率 80% 的配置可能在短输出上亏，在长输出上赚；一个接受率 60% 的配置可能因为 draft 便宜而更划算。上线前应该在目标硬件上用固定负载扫 `k`、置信度阈值和 batch，而不是照搬论文里的参数。

## 小结

推测解码用 draft 提议、目标验证，在不改变输出分布的前提下提高每步产出。DSpark 进一步用 Confidence Head 做变长验证，把验证预算花在最可能被接受的 token 上。正确性的关键是接受/拒绝规则和 KV 的逻辑长度回退；结构化输出还需要 grammar 状态回滚。mini-deepinfer 用贪心逐 token 一致性和接受率统计验证了这套机制。

## 练习

1. 设 `c=[0.9,0.7,0.5,0.3]`、`r=0.1`，计算固定 4 个 draft 的期望接受长度和加速比；再计算只验证前 2 个的结果。
2. 解释为什么贪心推测解码可以做到和纯目标贪心逐 token 一致，而随机采样只能保证分布一致。
3. 如果一次验证中第 3 个 draft 被拒绝，目标模型写了 5 个位置的 KV，哪些位置可以保留，哪些必须覆盖？为什么？
4. 比较 Medusa、EAGLE、lookahead 和 DSpark 的 draft 来源、训练成本和适用场景，各写一句话。

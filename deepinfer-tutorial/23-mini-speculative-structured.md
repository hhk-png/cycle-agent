# 第 23 章 推测解码与结构化输出实现

本章导读：本章逐段解释 `speculative.py` 和 `structured_output.py`。你会看到 draft 提议、置信度代理、变长验证、接受/拒绝、KV 逻辑回退，以及正则/JSON/EBNF 到 NFA 的编译、mask、trigger/bound 和 snapshot/restore。最后说明这两个模块组合时的取舍。

## 23.1 推测解码的入口

`LLMEngine._execute_speculative` 是推测解码的入口：

```python
k = self.config.speculative_tokens
proposal = generate_drafts(self.draft_model, seq, k, seq.sampling_params)
verify_len = choose_verify_length(
    proposal.confidences, threshold=self.config.confidence_threshold
)
verification = verify_drafts(
    self.model, seq, proposal, verify_len, self.kv_cache,
    seq.sampling_params, matcher=None,
)
computed_end = old_length + verification.accepted
```

`generate_drafts` 用 draft 模型自回归生成 `k` 个 token。它每一步调用 `draft_model.forward_full(ids)`，取最后一个 logits，用 `target_probs` 得到分布，再从 `seq.draft_rng` 采样。返回的 `DraftProposal` 包含 token、每个位置的分布、置信度。

这里用 `forward_full` 而不是 `forward_paged`，是因为 draft 模型的序列很短，且不需要写 KV cache；`forward_full` 每次重算整个前缀，逻辑简单。生产实现会用 paged KV 和 CUDA Graph，把 draft 的前向也做成增量式。

`choose_verify_length` 用置信度阈值和边际收益选前缀。最简单的策略是“置信度低于阈值就停”，但代码至少验证 1 个 draft：即使第一个置信度低于阈值，也会返回长度 1，因为验证一个 draft 的边际成本通常仍然值得。代码还计算了 `expected_accepted_length(prefix) / (target_cost + draft_cost × length)`，选得分最高的长度。这里的 `expected_accepted_length` 是“接受的 draft 数”，不含目标模型额外产出的 1 个 token；换算成每步推进 token 数时要加 1。这对应 DSpark 的“把验证预算花在边际收益最高的 token 上”。

## 23.2 验证与接受

`verify_drafts` 把最后一个已提交 token 和 `verify_len` 个 draft 拼成输入，调用目标模型的 `forward_paged`。注意 `start = len(committed) - 1`，`end = start + 1 + verify_len`。目标模型会为这些位置写 K/V，返回 `1 + verify_len` 行 logits。

对每个 draft：

- 贪心：目标 argmax 等于 draft 就接受，否则用目标 argmax 修正；
- 随机：以 `min(1, p_target[token] / p_draft[token])` 接受；拒绝时从 `normalize(max(0, p_target - p_draft))` 采样修正。

如果全部接受，从最后一行 logits 再采样一个 bonus token。返回的 `VerificationResult` 包含接受的 token、logprob、验证长度、接受数、是否全部接受。

引擎把 `computed_end` 设为 `old_length + accepted`，而不是 `old_length + verify_len`。原因在第 8 章讲过：被拒绝的 draft 位置的 K/V 无效，下一次写入会覆盖。这个细节是推测解码正确性的关键。

还有一个边界：被拒绝时返回的 `tokens` 包含“已接受的 draft + 一个修正 token”，所以 `accepted` 是已接受的 draft 数，`tokens` 的长度是 `accepted + 1`。引擎在 append 时要把修正 token 也算进输出，但 KV 只前进 `accepted`，因为修正 token 的 KV 要下一步才算。这个“输出长度”和“KV 长度”的差异，是推测解码实现里最容易错的地方。

## 23.3 置信度代理的局限

DSpark 的 Confidence Head 是一个训练过的线性层，输入 draft hidden 和 Markov embedding，输出接受概率。mini 没有训练 confidence head，而是用 draft 模型对候选 token 的概率作为代理：

```python
proposal.confidences.append(probs[token])
```

这个代理只反映“draft 自己有多确信”，不反映“目标模型会不会同意”。当 draft 和目标分布差异大时，它会高估接受率。测试里用 `confidence_threshold=0.0` 强制验证所有 draft，保证贪心输出和目标一致；示例里用 0.55 的阈值，观察接受率和验证长度的变化。

这是一个明确的边界：mini 验证的是“推测解码的控制流和正确性”，不是“Confidence Head 的预测质量”。如果要复现 DSpark 的收益，需要训练一个真正的 confidence head，并用目标模型的接受标签做监督。

代理置信度还有一个副作用：它会让 `choose_verify_length` 偏向于“高概率但可能被拒绝”的 token。真正的 Confidence Head 会用目标模型的特征，能区分“draft 自信但目标不同意”的情况。这个差异在分布偏移时最明显，例如 draft 在训练集上很准、在长尾输入上很差。

## 23.4 结构化输出的 NFA

`structured_output.py` 的 `NFA` 是一个经典的 Thompson 构造：

- `trans[state][char]` 是字符转移，`trans[state][None]` 是 epsilon 转移；
- `epsilon_closure` 做闭包；
- `_reverse_reachable` 从接受状态反向 BFS，得到“有用状态”集合；
- `NFAMatcher.allowed_bytes` 对每个字节尝试转移，保留能到达有用状态的；
- `allows_eos` 检查接受状态是否在当前闭包里。

`RegexParser` 是递归下降：`_parse_alt` 处理 `|`，`_parse_concat` 处理连接，`_parse_repeat` 处理 `* + ? {m,n}`，`_parse_atom` 处理字面量、字符类、`.`、转义和分组。`{m,n}` 通过复制子图实现，上限 64，防止状态爆炸。

`compile_regex` 返回一个 `NFAMatcher`。`allowed_mask(vocab_size, eos_token_id)` 生成 bool 数组，采样时把非法 token 的 logits 置为 `-inf`。

NFA 的状态是“状态集合”，不是单个状态。这是因为 NFA 有 epsilon 分支，一个输入可能同时处在多个路径上。`epsilon_closure` 把“不需要输入就能到达的状态”全部展开；`allowed_bytes` 对每个字节做一次 `step + closure`，再检查是否还有用。这样即使某个分支死了，其他分支还活着，字符仍然合法。

## 23.5 JSON Schema 与 EBNF

`_schema_to_regex` 把 JSON Schema 子集翻译成正则：

- object：按 `properties` 顺序输出 `"key": value`，用 `[ ]?` 允许少量空白；
- string：普通字符段最多 16 字节，转义只允许 `\"`、`\\`、`\/`、`\b`、`\f`、`\n`、`\r`、`\t` 和 `\uXXXX`；转义段可以继续拼普通字符，所以整个字符串可能超过 16 字节。不是 `\\.` 任意转义——`\q` 会被拒绝，测试 `test_json_matcher_rejects_invalid_escape` 专门覆盖这一点；
- integer/number/boolean/null：标准 JSON 词法；
- array：`[ item (, item)* ]`；
- anyOf/oneOf/enum/const：递归展开。

这个子集不支持任意字段顺序、`$ref`、复杂的 `patternProperties`，也不支持真正的递归 schema。文档和示例都明确写了这些限制。

EBNF 用 `EBNFParser` 解析 `rule = expr ;`，支持字面量、引用、连接、选择、`{ }` 重复、`[ ]` 可选、括号。`_expand_ebnf` 递归展开成正则，深度上限 20；递归文法超过上限会抛 `StructuredOutputError`，而不是生成一个不完整的语言。

这两个编译器的共同点是“用正则表达一个子集”。正则的表达力有限，遇到递归结构就要展开；展开深度有限，超过就报错。这是教学实现的边界，也是为什么生产要用 xgrammar 的完整文法编译器。

## 23.6 Trigger、Bound 与回滚

`StructuredOutputMatcher` 包在 NFA 外面，处理 trigger/bound：

- trigger 非空时，`active=False`，mask 允许所有普通字节和 EOS；
- `accept_token` 用 KMP 更新 `trigger_progress`，匹配完成后 `active=True`；
- 如果设置了 bound，bound token 出现后强制逐个走完 trigger；
- active 后，`accept_token` 委托给 NFA；
- `snapshot`/`restore` 保存 NFA 状态、active、trigger 进度、bound 计数、finished、aborted。

测试 `test_trigger_and_bound` 覆盖了“自由文本 -> bound -> 强制 trigger -> grammar”的路径。这个功能在“先思考、后 JSON”的场景里很有用。

回滚的关键是“保存足够多的状态”。NFA 的状态是一个 frozenset，trigger 进度是一个整数，bound 计数是一个整数，aborted 是一个布尔值；这些都可以放进一个 tuple。保存完整 tuple 的成本很低，但要注意：如果 grammer 对象本身是可变的，restore 时要恢复它的内部状态，而不是只恢复外层。

## 23.7 两个模块的组合

mini 支持结构化输出和推测解码同时开启。引擎不再用 `seq.so_matcher is None` 把两者分开，而是把 matcher 复制一份交给 `verify_drafts`：

```python
if self.config.enable_speculative:
    return self._execute_speculative(seq)
```

验证器在副本上逐位置取 `allowed_mask()`，用 masked target 分布做接受/拒绝；被拒绝的 draft 不会污染真正的 matcher。验证结束后，`_append_tokens(..., advance_matcher=True)` 只对最终提交的 token 调用 `accept_token`。这个顺序解决了一个很容易错位的边界：如果 stop token 让提交长度小于验证长度，真正的 matcher 只前进了提交的部分，和 `computed_end = old_length + accepted` 保持一致。

为什么不在真 matcher 上直接验证再回滚？因为“回滚几步”依赖于哪些 draft 被接受、哪些被 stop 截断，而验证阶段还没有最终提交列表。副本验证把“探索”和“提交”分开，提交阶段只需要按 token 顺序前进，不需要知道验证的内部细节。生产引擎用 snapshot/restore 也能做到，但必须保证 restore 之后重放的状态和提交的 token 完全一致。

组合之后，三个不变量必须同时成立。第一，最终输出必须满足 grammar。第二，`computed_end` 必须等于“已提交 token 的逻辑长度”，不能等于验证长度。第三，matcher 的状态必须等于“只接受已提交 token”后的状态，不能多也不能少。测试 `test_speculative_with_structured_output_matches_target` 用贪心推测和纯目标贪心逐字符比较，并检查输出 fullmatch grammar，覆盖了这三个不变量。

## 23.8 接受规则为什么能保持分布

标准投机采样的正确性可以用一句话概括：对任意 token `x`，接受路径和拒绝路径贡献的概率之和等于 `p(x)`。

接受路径：draft 以 `q(x)` 采到 `x`，接受概率 `min(1, p(x)/q(x))`，贡献 `q(x) × min(1, p(x)/q(x)) = min(q(x), p(x))`。

拒绝路径：当 `q(x) > p(x)` 时，拒绝后从 `max(0, p - q)` 的归一化分布采样。拒绝的总概率是 `1 - sum_y min(q(y), p(y))`，归一化因子是 `sum_y max(0, p(y)-q(y))`，两者相等（因为 `sum p = sum q = 1`）。所以拒绝路径对 `x` 的贡献是 `max(0, p(x)-q(x))`。

两者相加，正好是 `p(x)`。这个证明假设 draft 和目标使用相同的温度/截断分布；如果一边用 top-p、一边用全分布，等式就不成立。mini 在 `target_probs` 里对两边都应用同样的 top-k/top-p，就是为了保持这个一致性。

贪心是随机采样的极限：`p` 和 `q` 都是 one-hot，接受规则退化成“argmax 相同就接受”。这时不需要拒绝采样，输出和目标贪心逐 token 一致。

## 23.9 测试与边界

推测解码测试：

- `expected_accepted_length` 的闭式结果；
- `choose_verify_length` 的阈值行为；
- 贪心推测与目标贪心逐 token 一致；
- 推测 + 正则 grammar 的贪心输出与纯目标 + grammar 一致，且 fullmatch；
- 接受率、平均接受长度在 [0,1] 和合理范围内；
- draft proposal 的形状和概率范围。

结构化输出测试：

- 正则接受/拒绝、字符类、重复；
- snapshot/rollback；
- JSON 文档解析；
- EBNF 文法；
- 递归 EBNF 在上限处报错；
- trigger/bound；
- 未知 schema 类型报错。

这些测试覆盖的是“机制正确”，不是“语言覆盖完整”。如果要支持完整 JSON Schema，需要引入 xgrammar 或自己实现下推自动机；如果要支持完整 EBNF，需要 Earley 或 GLR 解析器。第 27 章的迁移清单和 21.5 节的语言子集说明会给出扩展顺序。

## 23.10 推测解码 + 结构化输出的组合测试

组合已经在仓库里实现，测试 `test_speculative_with_structured_output_matches_target` 覆盖了最基本的路径：贪心、固定 seed、正则 grammar，比较“推测 + grammar”和“纯目标 + grammar”的输出，并断言输出 fullmatch。下面列出可以继续扩展的边界。

**准备**：一个带 grammar 的请求（例如 JSON schema），开启推测解码，固定 seed 和 `confidence_threshold`，确保 draft 和目标模型一致。

**验证顺序**：先复制 matcher；目标模型验证 draft，拿到 `accepted` 和 `tokens`；验证器在副本上逐位置 mask、接受或拒绝；引擎只把最终提交的 token 回放到真 matcher；最后把 KV 的 `computed_end` 设为 `old_length + accepted`。这里说的“回滚”是逻辑长度回退，不是把拒绝位置的 K/V 清零：`verify_drafts` 已经写入了全部候选位置的 K/V，但序列长度只推进到 `computed_end`，后续 decode 或下一次验证会在同一位置重新写入，把旧值覆盖掉。这样做避免了额外的清零 kernel，代价是“已分配但逻辑不可见”的 KV 仍然占着 block；如果实现按物理长度而不是逻辑长度读 attention，就会读到脏数据。顺序不能反：先回退逻辑长度再回放 grammar，或者先 accept 再验证，都会留下不一致状态。

**断言**：最终输出必须通过 grammar 的 fullmatch；被拒绝位置的 grammar 状态必须和“只接受已接受前缀”的状态一致；KV 的逻辑长度必须等于已接受前缀长度；下一次 decode 的 logits 不能读到被拒绝位置的 KV。

**边界**：全部接受、第一个拒绝、中间拒绝、接受 stop token、grammar 到达 EOS、trigger/bound 模式下拒绝。每个边界都要单独测，因为它们对应不同的状态转移。当前测试覆盖“正常接受/拒绝”的主路径；stop token 和 trigger/bound 的组合值得补专门的用例。

**性能**：组合后的每步时间不能超过“纯推测 + 纯 grammar”的较大者太多；如果回滚成本很高，说明 matcher 的副本或快照太重，需要改成操作日志或增量快照。mini 的 `copy.deepcopy` 是为了可读性，生产实现会用更轻量的状态复制。

这个测试的价值不只是“组合能跑”，而是“两个状态机在提交时保持一致”。推理系统的很多 bug 不是数学错误，而是两个状态机的提交顺序不一致。把顺序写成测试，比在文档里写“注意顺序”可靠。

## 小结

mini 的推测解码保留了 DSpark 的控制流：draft 提议、置信度选择、目标验证、接受/拒绝、逻辑长度回退；但用 draft 概率代理 confidence，不追求生产加速。结构化输出用自写 NFA 和 bounded EBNF 演示了“编译 -> mask -> 回滚”的完整链路，但只支持语言子集。两个模块的组合被刻意简化，读者应该知道这个边界：贪心 + grammar 走完整的推测路径（one-hot 分布下接受规则仍然正确）；非贪心 + grammar 会退回普通 decode，因为 draft 采样没有应用 grammar mask，直接验证会让接受分布有偏。生产实现要把 mask 同时加进 draft 采样器和目标验证器，才能在这个组合上安全地获得推测收益。

## 练习

1. 如果 draft 分布和目标分布完全相同，接受率是多少？期望接受长度是多少？
2. 正则 `a{2,4}b` 的 NFA 有多少个状态？输入 `aaa` 后允许哪些字符？
3. 为什么推测解码拒绝一个 draft 后，不能把验证时写入的所有 K/V 都保留？如果保留会发生什么？
4. 设计一个“推测解码 + 结构化输出”的测试，说明你要固定哪些变量、断言什么。mini 已经支持贪心 + grammar 的组合，为什么非贪心 + grammar 仍然退回普通 decode？

# 第 9 章 结构化输出

本章导读：结构化输出不是“生成完再解析”，而是在每个 decode step 用文法限制采样空间。本章讲清正则/JSON/EBNF 如何编译成自动机、如何变成 token mask、触发词和边界 token 怎么工作、推测解码下如何回滚，以及编译缓存的安全风险。最后给出 mini-deepinfer 的 NFA 实现。

## 9.1 为什么不能事后解析

假设你要求模型输出一个 JSON 对象。最朴素的做法是让模型自由生成，然后用 `json.loads` 解析；失败就重试。这在演示里可行，在生产里不可接受：重试会放大延迟和成本；模型可能生成看似合法但语义错误的字段；流式输出时，用户已经看到了不合法的中间文本。

结构化输出（structured output，SO）把约束前移到采样阶段。每个 step，框架知道“当前已经生成了哪些 token、文法还允许哪些下一个 token”，然后把这些不允许的 token 的 logits 置为负无穷。模型仍然在合法的 token 里按概率选择，但永远不会越界。

这个“允许集合”由文法自动机给出。正则对应有限状态自动机（NFA/DFA），JSON Schema 可以编译成文法，EBNF 是上下文无关文法。DeepInfer 使用 xgrammar 做编译和匹配，把 schema 编译成 `CompiledGrammar`，再在运行时维护 `GrammarMatcher`。

事后解析还有一个隐性问题：它无法保证“前缀合法”。即使最终 JSON 能解析，中间流式输出的片段也可能是 `{"name": ` 这样的半截文本。对于需要流式展示或增量解析的客户端，这会造成闪烁和错误。前置约束能让每个 token 都是“某个合法完整串的前缀”，流式体验更稳定。

## 9.2 从正则到 NFA

正则表达式的标准编译路线是 Thompson 构造：每个字面量、字符类、连接、选择、闭包都对应一个小 NFA 片段，最后用 epsilon 边把它们拼起来。NFA 的状态集合可以表示“当前可能处在哪些位置”。

生成时，我们不需要 DFA，只需要：

1. 当前状态集合 `S`；
2. 对每个候选字符 `c`，计算 `S' = closure(step(S, c))`；
3. 如果 `S'` 非空，且从 `S'` 能到达接受状态，则 `c` 合法；
4. 如果接受状态在 `closure(S)` 中，则允许 EOS。

“能到达接受状态”这个条件很重要。只检查 `S'` 非空会允许一些死路，例如正则 `abc` 在输入 `abx` 后仍然有状态，但永远到不了接受态。mini-deepinfer 在 `NFAMatcher` 里预先计算了反向可达集合 `useful`，每次转移后检查是否与 `useful` 相交。

用一个小例子走一遍。正则 `ab*c`：

- 初始闭包包含“等待 a”的状态；
- 输入 `a` 后，状态集合变成“可以接受 b 或 c”；
- 输入 `b` 后，仍在循环里，允许 `b` 或 `c`；
- 输入 `c` 后，到达接受态，允许 EOS；
- 输入 `x` 后，状态集合为空，任何字符都不允许。

这里要纠正一个常见的误解：对标准 Thompson 构造的简单正则，输入一个非法字符后状态集合通常直接变成空集，`abx` 在 `abc` 上并不会留下一个“还活着但永远到不了接受态”的状态。真正的死状态来自更复杂的构造：文法合并、可选的 epsilon 边、带计数器的展开、或者多个正则共享状态时，可能出现“状态集合非空，但从它出发没有任何路径能到达接受态”的情况。`useful` 反向可达检查是对这类情况的防御，而不是简单正则的必需步骤。

即使没有死状态，反向可达检查也有一个实际好处：它让“允许的字符”与“能完成整个串的字符”对齐。某些文法在中间状态允许一个字符，但走完之后会进入一个只能接受 EOS、无法继续匹配的模式；如果 mask 只看“转移非空”，模型会生成一个语法上合法、语义上却无法完成的半截串。把这个检查放进 matcher，能把错误提前到采样阶段，而不是等到客户端解析时才失败。

## 9.3 token mask 与 tokenizer

自动机工作在字符（或字节）层面，但采样工作在 token 层面。需要把“允许的字符集合”映射成“允许的 token 集合”。对字节级 tokenizer，一个 token 就是一个字节，映射很直接；对 BPE tokenizer，一个 token 可能是多个字符，需要检查这个 token 的字节串是否能让自动机从当前状态走到一个有用状态。

DeepInfer 的 `XGrammarMatcher.fill_vocab_mask` 调用 xgrammar 的 `fill_next_token_bitmask`，把合法 token 写进一个 bitmask。这个 bitmask 的布局要和采样 kernel 对齐，通常是 `[batch, ceil(vocab/32)]` 的 int32 位图。采样时，非法 token 的 logits 被 mask 掉，softmax 只在合法 token 上归一化。

tokenizer 的细节会影响正确性。例如，一个 token 可能包含多个字节，其中一部分合法、一部分非法；必须按 token 的完整字节串判断，不能只看第一个字节。反过来，一个多字节字符可能被拆成多个 token，自动机要能处理“半个字符”的中间状态。DeepSeek Recipe 在输入侧也做了类似的事情：显式特殊 token 和普通文本分开编码，防止用户文本注入控制 token。

一个 BPE tokenizer 的 token 可能对应 `"name":` 这七个字节（引号、n、a、m、e、引号、冒号）。如果只看首字符 `"`，会误以为它合法；但自动机可能已经处于“不能接受引号”的状态。正确的做法是把 token 的字节串逐个喂给 NFA，全部接受才允许。这也是为什么 token mask 的计算成本与词表大小和 token 长度相关，不能简单地按首字节索引。

## 9.4 JSON Schema 与 EBNF

JSON Schema 比正则复杂，因为对象字段可以任意顺序、数组长度可变、嵌套结构递归。生产实现通常把 schema 编译成一个下推自动机或带栈的文法，而不是简单的正则。

DeepInfer 用 xgrammar 的 `compile_json_schema(strict_mode=True)`。strict mode 会检查 schema 的合法性，并要求生成的 JSON 严格符合 schema。EBNF 则用 `compile_grammar`，支持递归文法。

mini-deepinfer 做了一个务实的简化：

- JSON Schema 被编译成一个**固定字段顺序**的正则。对象按 schema 的 `properties` 顺序输出，`required` 字段必选，可选字段放在末尾。字符串的每个普通片段限制在 16 字节以内，转义片段单独拼接，所以总长度可以超过 16；这个上限是为了避免小模型在引号里无限游走，不是严格的字段长度校验。这不是完整的 JSON Schema，但足以演示“schema -> 自动机 -> mask”的完整链路。
- EBNF 被递归展开成正则，展开深度上限 20。递归文法在达到上限时报错，而不是悄悄生成一个不完整的语言。这也不是完整的上下文无关文法，但能覆盖 `start = "a" { "b" } "c"` 这类常见结构。

这两个限制会在正文和示例里明确写出来。教学实现可以缩小语言，但不能把“不支持”伪装成“支持”。如果读者拿 mini 的 JSON 编译去跑一个带 `$ref` 的生产 schema，它应该报错，而不是生成一个错误的自动机。

JSON Schema 的完整支持还包括：`$ref` 递归、`patternProperties`、`additionalProperties`、`allOf`/`not`、联合类型、数字范围。这些特性会让文法从正则升级到带栈的解析器。xgrammar 的做法是把 schema 编译成上下文无关文法，再用 Earley 或类似算法匹配；成本比 NFA 高，但表达力足够。

EBNF 的完整支持则需要处理左递归、间接递归、优先级和 ε 产生式。mini 的“递归展开”只适合有限深度的文法；超过上限就报错，避免状态爆炸。生产实现会用解析器组合子或 GLR/Earley，而不是展开成正则。

## 9.5 触发词与边界 token

有些场景不希望从第一个 token 就约束。例如，模型先输出一段自然语言，然后在一个特殊标记后输出 JSON。DeepInfer 的 `SamplingParams.so_args` 支持 `trigger_token_ids` 和 `bound_token_id`：

- `trigger_token_ids`：当这个 token 序列完整出现后，grammar 才开始生效；出现之前自由生成。
- `bound_token_id`：当这个 token 出现后，不再允许自由 token，先逐个强制走完 trigger 序列，再进入 grammar。

mini-deepinfer 的 `StructuredOutputMatcher` 用 KMP 维护 trigger 的匹配进度。触发前，mask 允许所有普通字节和 EOS；触发后，切换到 NFA 的 mask。如果设置了 bound，bound token 出现后只允许 trigger 的下一个 token，直到 trigger 走完。

这个机制的实际用途是“先思考、后格式化”：模型可以先写推理过程，看到 ` ```json ` 后再进入 JSON 文法。它也能防止模型在自然语言部分被 JSON 的引号/括号约束得太死。

KMP 的作用是处理重叠前缀。例如 trigger 是 `[a, b, a, b]`，输入 `a, b, a` 后还没匹配完，下一个 `b` 才能完成；如果下一个是 `a`，KMP 会回到“已匹配 `a`”的状态，而不是从头开始。没有 failure 表，重叠模式会漏匹配。

## 9.6 推测解码下的回滚

推测解码会一次验证多个 draft token。如果第 3 个 draft 被拒绝，grammar 已经接受了前 3 个 token 的状态，必须回滚到第 2 个之后。DeepInfer 的 `XGrammarMatcher.rollback(steps)` 做了三件事：

1. 如果已经接受 EOS，先回退 EOS 计数；
2. 回退 xgrammar 内部的 matcher 状态；
3. 回退 trigger 的 KMP 进度和 bound 计数。

`MAX_ROLLBACK_TOKENS` 默认是 `nextn + 2`，因为一次最多回滚这么多 token。如果 trigger 的进度历史不够长，回滚会报错，而不是静默产生错误状态。

mini-deepinfer 支持结构化输出和推测解码共存。它在验证时复制一份 matcher，用副本计算每个位置的 mask，验证结束后只把最终提交的 token 回放到真正的 matcher 上。这个“副本验证、提交回放”的顺序，避免了“验证写入了 5 个位置、但 stop token 只提交了 3 个”导致的状态错位。生产引擎可以用 snapshot/restore 实现同样的语义；关键是提交边界必须和 KV 的逻辑长度一致。

回滚的实现方式有两种：保存每个 token 的完整快照，或者保存“操作日志”并反向执行。快照简单但占内存；操作日志省内存但要求每个状态转移都可逆。DeepInfer 的 matcher 选择保存关键状态（状态集合、trigger 进度、bound 计数），在回滚时恢复。这个选择取决于状态大小和回滚频率。

## 9.7 编译缓存与安全

文法编译是 CPU 密集型操作，可能耗时几十到几百毫秒。生产系统会把编译结果缓存起来，按 schema 的哈希索引。DeepInfer 的 `XGrammarCompiler` 暴露了 `DEEPINFER_XGRAMMAR_DISABLE_CACHE` 和 `DEEPINFER_XGRAMMAR_CACHE_LIMIT_BYTES`。

缓存没有上限会带来内存耗尽风险。代码注释明确提到 CVE-2025-32381：不同 schema 可以无限增长缓存，形成 DoS 向量。生产部署应该设置正的字节上限，或者关闭缓存。这个细节说明，结构化输出不仅是算法问题，也是服务安全问题。

另一个安全点是特殊 token 注入。用户输入里如果出现 `<|user|>` 这样的字符串，不能被 tokenizer 当成控制 token，否则用户可以伪造角色。DeepSeek Recipe 的做法是：模板显式插入的特殊 token 直接映射到控制 id；普通文本里的相同字符串按普通 token 序列编码。mini-deepinfer 的字节级 tokenizer 天然满足这一点，`Tokenizer.encode` 永远不会产生特殊 id。

还有一类攻击是“schema 复杂度攻击”：用户提交一个嵌套很深或分支很多的 schema，让编译器消耗大量 CPU/内存。生产网关应该限制 schema 的大小、嵌套深度和编译时间，而不是把任意 schema 直接传给引擎。

## 9.8 DeepSeek Recipe：结构化输出之外的协议层

DeepInfer 的 Q&A 把 DeepSeek Recipe 和结构化输出放在同一节，因为两者都在解决“模型 token 与外部协议之间的转换”。

Recipe 把 Chat Completions、Responses、Messages 三种 API 的请求转换成统一的对话格式，再用同一套模板和编码逻辑生成模型输入的 token 序列。模型输出被解析成事件（正文、reasoning、工具调用等），再转换成各协议的流式响应。请求转换、prompt 编码、输出解析都用 Rust 实现，提供 Python 绑定。

它有三个值得借鉴的设计。

**特殊 token 防注入**：构建 prompt 时，模板显式插入的特殊 token 和位置被记录下来，编码时直接映射到控制 id；普通文本里的 `<｜User｜>`、`<｜image｜>` 按普通 token 序列编码，不能变成控制 token。这和 mini 的字节级 tokenizer 是同一个原则。

**分段并行 tokenization**：BPE tokenizer 在特殊 token 处可以安全切分；Recipe 记录特殊 token 的边界，把普通文本段并行编码，再按原顺序拼接。长上下文请求的 tokenization 延迟因此降低。

**流式解析状态机**：模型返回的是 token 粒度的 chunk，一个 UTF-8 字符、一个 DSML 标签、一个工具参数都可能跨 chunk。Recipe 用增量状态机处理 reasoning/正文/工具参数，遇到不完整的 UTF-8 字符就缓冲，遇到分隔符就用增量 KMP 保留匹配进度，已经确定的内容立即输出。工具参数按 DSML 边界直接生成 JSON 片段，字符串逐段转义，数组/对象保留原始 JSON 文本，不需要构造完整对象再序列化。

这些工作看起来和“推理引擎”无关，但它们决定了 API 的行为是否和官方一致。一个推理框架即使 kernel 很快，如果 tokenizer 把用户文本里的特殊 token 当成控制 token，或者流式解析把多字节字符截断，用户体验和安全性都会出问题。mini 的 `tokenizer.py`、`recipe.py` 和 `server.py` 只实现了这些想法的最小版本：字节级编码、Chat 模板、多协议消息归一化、流式事件解析、UTF-8 流式解码、SSE。第 25 章会讲服务层。

## 9.9 mini-deepinfer 的实现

`mini_deepinfer/structured_output.py` 包含：

- `NFA` 和 `NFAMatcher`：epsilon 闭包、反向可达、允许字节、EOS 判断、snapshot/restore；
- `RegexParser`：递归下降解析字面量、字符类、`.`、转义、分组、选择、`* + ? {m,n}`；
- `_schema_to_regex`：JSON Schema 子集到正则；
- `EBNFParser` 和 `_expand_ebnf`：EBNF 到正则的 bounded expansion；
- `StructuredOutputMatcher`：trigger/bound、mask、accept、回滚；
- `build_matcher`：根据 `so_args` 选择编译器。

测试覆盖了正则接受/拒绝、回滚、JSON 文档、EBNF、递归上限、trigger/bound 和未知 schema 类型。运行 `python examples/structured_output.py` 可以看到三种文法的实际输出。一次运行的输出是：正则 `cal tokens to ph` 合法，JSON 对象能 `json.loads`，EBNF 输出 `yes` 合法。

## 9.10 结构化输出的快速估算

在展开细节之前，先给一个可手算的上界。设文法编译一次的成本是 `C_compile`，每个 decode step 的 mask 成本是 `C_mask`，输出 `N` 个 token，则一次请求的结构化输出开销约 `C_compile + N × C_mask`。如果同一个 schema 被 `R` 个请求复用，编译成本摊薄为 `C_compile / R`；当 `R` 很大时，编译可以忽略，瓶颈在每步 mask。反过来，如果每个请求的 schema 都不同、只生成几个 token，编译可能比生成还贵。

这个估算解释了为什么生产系统先做编译缓存，再做 mask 位图/GPU kernel：两者的优化对象不同，不能互相替代。质量方面的隐藏成本也要单独看：约束会让采样空间变窄，模型可能生成“合法但低质量”的内容，评测不能只看合法率。

## 9.11 结构化输出的端到端成本

下面把 `C_compile`、`C_mask` 和回滚成本拆开，给出每一项的来源和优化顺序。

**编译**：把正则/JSON Schema/EBNF 编译成自动机或文法。复杂 schema 可能耗时几十到几百毫秒，递归和分支会让状态数爆炸。生产系统会按 schema 哈希缓存编译结果，并设置缓存字节上限；没有上限会变成内存 DoS。9.7 节提到的 CVE-2025-32381 就是这类问题。

**每步 mask**：对每个 token 检查“从当前状态出发，这个 token 的字节串是否能让自动机走到有用状态”。词表 10 万、token 平均 3 个字节，每步就是 30 万次状态转移；如果每个请求每步都在 CPU 上跑，decode 的几毫秒会被吃光。生产实现会把 mask 编译成位图、用 GPU kernel 并行计算，或者只对候选 token 做检查。

**采样**：非法 token 的 logits 被置为 `-inf`，softmax 只在合法 token 上归一化。这个操作本身很便宜，但如果 mask 的布局和采样 kernel 不对齐，会多一次 HBM 往返，抵消融合的收益。

**回滚**：推测解码拒绝 draft 时，grammar matcher 要恢复到已接受前缀的状态。快照法占内存，操作日志法要求状态转移可逆；两种方法都比 KV 回滚复杂，因为 grammar 是状态机，不是简单的长度截断。

一个实用的优化顺序是：先缓存编译结果，再把 mask 做成位图/GPU kernel，最后才考虑推测解码下的回滚。如果编译占了大部分延迟，优化 mask 没有意义；如果 mask 是瓶颈，先看词表大小和 token 长度，而不是盲目上 GPU。

结构化输出还会影响模型质量。约束会把采样空间变窄，模型可能更容易生成“合法但低质量”的内容；评测不能只看合法率，还要看字段准确率、完成率和内容质量。第 29 章会给出评测方法。

## 9.12 工具调用与 MCP

结构化输出不只用于 JSON 回复，也用于工具调用。一个工具调用通常包含工具名和参数对象；模型输出被解析成结构化事件，再由执行层调用真实工具。

**协议层**：OpenAI 的 tool calls、Anthropic 的 tool use、MCP（Model Context Protocol）都把“工具名 + 参数”表达成结构化消息。推理引擎本身不需要理解业务工具，但它需要保证输出符合协议：工具名来自允许的枚举，参数符合 JSON Schema，流式输出能增量解析。

**约束方式**：最简单的做法是把工具 schema 编译成文法，限制工具名和参数字段；更宽松的做法是让模型自由生成 JSON，再由执行层校验。前者的合法率高，但编译成本高、灵活性差；后者灵活，但需要重试和错误处理。生产系统通常对高频工具用文法约束，对低频/自定义工具用校验 + 重试。

**流式解析**：工具参数可能分多个 chunk 到达，字符串要逐段转义，数组/对象要保留原始 JSON 文本。不能在收到半个对象时就 `json.loads`；要维护增量状态机，等结构完整再解析。`recipe.py` 的流式事件解析就是这类逻辑的最小版本。

**执行安全**：工具参数来自模型输出，不能信任。执行层要做 schema 校验、参数白名单、路径穿越检查、命令注入检查、沙箱和超时。一个合法的 JSON 仍然可以包含恶意命令；文法约束只保证格式，不保证安全。

**MCP 的特殊性**：MCP 把工具、资源和提示模板标准化，客户端可以动态发现工具。推理服务需要把 MCP 的工具 schema 编译成文法或校验器，并处理工具列表变化、权限、审计和超时。MCP 的连接是有状态的，工具调用可能跨多轮，缓存和会话管理比单次 JSON 输出复杂。

对推理引擎的要求是：输出要能被增量解析、工具名和参数要可约束、错误要能回滚、执行要隔离。把工具调用当成“另一种结构化输出”，比把它当成“模型自由文本 + 正则提取”可靠得多。

## 9.13 十万词表上的 mask 怎么算

9.3 节说“把允许的字符映射成允许的 token”，在词表只有 264 个字节时这很轻松；生产词表是 10 万到 20 万，每个 token 平均 2 到 4 个字节，每步要判断几十万次状态转移。如果每次 decode 都在 Python 里对每个 token 调一遍 NFA，几毫秒的 step 会被 mask 吃光。生产实现要做三件事：预计算、缓存、并行。

第一是**按 token 的长度分层**。大多数 token 只有 1 到 2 个字节，可以先用一张“字节 -> 允许/拒绝”的表快速过滤；只有首字节合法的 token 才需要走完整的 NFA。更快的做法是把 tokenizer 的词表组织成字节 trie：从当前 NFA 状态集合出发，沿 trie 深度优先，一旦某个前缀的所有转移都为空就整棵子树剪掉。这样每个状态只需要遍历少量分支，而不是遍历整个词表。

第二是**缓存状态到 mask 的映射**。文法状态在生成过程中会重复出现，尤其是 `{m,n}` 重复、空白、固定字段名这些结构。可以用状态集合的哈希作为 key，缓存 `(allowed_token_bitmask, next_state)`；命中时直接复用，未命中才跑 trie。缓存要有上限，否则和编译缓存一样会成为内存 DoS 的入口。

第三是**把 mask 做成位图并交给 GPU**。位图布局要和采样 kernel 对齐，通常是 `[batch, ceil(vocab/32)]` 的 int32；每个 token 对应一位，1 表示允许。采样 kernel 在 softmax 前把非法位对应的 logit 置为 `-inf`。如果位图在 CPU 上生成、再拷到 GPU，每步会有一次 H2D 传输；生产实现会把 mask 生成也放进 GPU kernel，或者用 pinned memory 和异步拷贝把传输藏到前一步的计算后面。

成本可以粗略写成 `C_mask ≈ V × (1 + P_pass × (L_token - 1)) + C_transfer`，其中 `V` 是词表大小，`L_token` 是平均 token 字节数，`P_pass` 是首字节通过的比例，`C_transfer` 是位图传输。首字节就被拒绝的 token 只花一次查表；首字节通过的 token 才需要走完整的 `L_token` 字节。这个式子说明：词表越大、token 越长、首字节通过率越高，mask 越贵。优化顺序是：先用首字节分组和 trie 剪枝减少无效遍历，再降低有效 token 的平均长度（短 token 优先），最后才是并行化。很多实现一上来就把 mask 写成 CUDA kernel，但词表遍历的算法没变，收益有限；反过来，先把 trie 和缓存做好，CPU 版本也可能够快。

最后有一个正确性边界：mask 的位图必须和 tokenizer 的 token id 一一对应。换 tokenizer、加特殊 token、改词表顺序，都会让旧位图失效。生产系统把 tokenizer 的 fingerprint 写进编译缓存的 key，并在启动时校验，避免“mask 允许了 id 12345，但新词表里它是另一个 token”这类静默错误。

## 小结

结构化输出把文法约束前移到采样，核心是“编译成自动机、每步算 mask、拒绝时回滚”。正则用 NFA，JSON/EBNF 需要更复杂的文法；tokenizer 的 token 边界让 mask 不能只看字符；触发词和边界 token 支持“先自由、后约束”；编译缓存要防止 DoS。mini-deepinfer 用 NFA 和 bounded EBNF 演示了完整链路，并明确标注了语言子集的边界。

## 练习

1. 正则 `ab*c` 在输入 `ab` 后，允许的下一个字符有哪些？输入 `abb` 后呢？允许 EOS 吗？
2. 解释为什么 token mask 不能简单地“按 token 的第一个字符”判断合法性。
3. 如果推测解码验证了 5 个 draft，第 4 个被拒绝，grammar matcher 需要回滚几步？如果 trigger 的进度历史只有 3 个，会发生什么？
4. 设计一个针对 JSON Schema 编译缓存的 DoS 防护：限制哪些维度，阈值大概怎么定？

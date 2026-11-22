# 第 26 章 多轮对话、工具调用与 AgentLoop

## 26.1 为什么多轮不是“多拼接几次”

单轮 RL 的轨迹是：

```text
prompt -> response
```

多轮 Agent 的轨迹是：

```text
prompt -> assistant thought/tool call -> tool observation
       -> assistant thought/tool call -> tool observation
       -> final answer
```

这带来四个新问题：

1. **动作空间变了**：模型不只要生成自然语言，还要生成结构化工具调用；
2. **环境参与**：工具返回是外部状态，不能求导；
3. **mask 变了**：observation token 不是模型动作，不能算策略梯度；
4. **长度与轮次**：轨迹可能很长，且轮次不固定。

verl v0.9.1 把这些逻辑放在 `verl/experimental/agent_loop/`。

## 26.2 AgentLoop 的类结构

```text
AgentLoopBase
├── SingleTurnAgentLoop
└── ToolAgentLoop

AgentLoopWorker      # 批量生成
AgentLoopManager     # 管理 worker/server
AgentLoopManagerTQ   # TransferQueue 版
```

`AgentLoopOutput`（Pydantic model）通常包含：

- `prompt_ids`
- `response_ids`
- `response_mask`
- `multi_modal_data`
- `num_turns`
- `metrics`
- `extra_fields`

`AgentLoopBase.run(sampling_params, **kwargs)` 是核心协程：

1. 读取 `raw_prompt`；
2. 应用 chat template；
3. 调用 LLM server 生成；
4. 解析工具调用；
5. 执行工具；
6. 拼接 observation；
7. 重复直到结束；
8. 返回 token ids 与 mask。

## 26.3 单轮 AgentLoop

`SingleTurnAgentLoop` 做：

- 应用 chat template；
- 调用一次生成；
- 处理 stop token；
- 返回 response ids/mask。

它仍然比“直接调 generate”多做了：

- 多模态输入处理；
- chat template 版本管理；
- tokenization sanity check；
- 异步 server 调用。

## 26.4 工具调用

工具调用的格式取决于模型与模板。常见：

```text
<tool>calculator</tool><args>{"expr": "3+4"}</args>
```

或：

```text
{"name": "calculator", "arguments": {"expr": "3+4"}}
```

`ToolAgentLoop` 的流程：

1. 生成 assistant 文本；
2. 用 `tool_parser.py` 解析工具名与参数；
3. 调用工具（本地函数/远程服务/沙箱）；
4. 把结果包成 observation；
5. 追加到对话；
6. 再次生成。

配置：

```yaml
actor_rollout_ref:
  rollout:
    multi_turn:
      enable: true
      max_assistant_turns: 8
      max_user_turns: 2
      max_tool_response_length: 256
      tool_config_path: /path/to/tools.yaml
      function_tool_path: my_tools.py
      format: hermes
      tokenization_sanity_check_mode: strict
```

## 26.5 mask：哪些 token 参与训练

多轮轨迹的 token 分三类：

| 类型 | 来源 | mask |
|---|---|---|
| prompt | 用户/系统 | 0 |
| assistant | 模型生成 | 1 |
| tool observation | 工具返回 | 0 |

如果 observation 参与策略损失，模型会学会“生成工具返回”，而不是“调用工具”。

MiniVerl 的 `AgentLoop.run` 在拼接 observation 时把 mask 设 0：

```python
ids.extend(observation_ids)
token_mask.extend([0.0] * len(observation_ids))
```

测试 `test_agent_tool_loop` 验证了 mask 同时包含 0 和 1。

## 26.6 tokenization 的坑

多轮 tokenization 很容易错：

- chat template 在每轮插入的角色标记；
- 工具调用的特殊 token；
- observation 的截断；
- 训练 tokenizer 与推理 tokenizer 不一致；
- 生成时的 stop token 与训练时的 EOS 不一致。

v0.9.1 提供 `tokenization_sanity_check_mode`（`disable`/`ignore_strippable`/`strict`）：strict 会对比往返 tokenization 并打印 warning，但**不会中断训练**。把 warning 当“可忽略日志”是常见错误；它往往意味着 chat template 或特殊 token 已经不一致，应该当成阻塞问题处理。

## 26.7 异步与 server 模式

AgentLoop 通过 LLM server 调用推理引擎：

- `LLMServerClient` 发请求；
- `LLMServerManager` 管理多个 server；
- `AgentLoopWorker` 并发处理多个轨迹；
- `AgentLoopManager` 批量调度。

异步的好处：

- 一个轨迹等待工具时，可以生成另一个轨迹；
- 长尾工具调用不阻塞整个 batch；
- 支持 partial rollout。

代价：

- 请求/响应协议；
- 超时与重试；
- 会话状态；
- 工具执行的并发限制。

## 26.8 工具执行的安全与工程

工具可能是：

- 纯函数（calculator）；
- 代码沙箱；
- 搜索引擎；
- 数据库；
- 外部 API。

工程要求：

- 超时；
- 资源限制；
- 输入校验；
- 输出截断；
- 错误处理；
- 幂等/重试；
- 审计日志。

在 RL 训练中，工具失败会产生负奖励或无效轨迹；要区分“模型错”和“工具错”。

## 26.9 多轮 RL 的奖励

奖励可以来自：

- 最终答案正确性；
- 工具调用格式；
- 工具调用次数/成本；
- 中间步骤（过程奖励）；
- 任务完成状态。

常见组合：

```python
reward = correctness
       + 0.1 * valid_tool_call
       - 0.01 * num_tool_calls
```

要避免：

- 只奖励格式，模型不解决任务；
- 只奖励最终答案，模型跳过工具；
- 工具成本权重过高，模型不敢调用。

## 26.10 MiniVerl 的 AgentLoop

MiniVerl 的 `agent.py` 实现：

- `Tool` / `CalculatorTool` / `ToolRegistry`；
- `AgentTrajectory`（prompt/ids/mask/text/tool_calls/turns）；
- `AgentLoop.run`：生成 → 解析 `<tool>`/`calculator[...]` → 执行 → 拼接 `<obs>`；
- `AgentLoopManager.to_dataproto`：把轨迹转成 DataProto。

它没有真正的异步 server，但保留了：

- 工具解析；
- observation mask；
- 轮次统计；
- 轨迹到 batch 的转换。

`examples/09_agent_tools.py` 用脚本化生成器演示了完整流程。

**边界**：`AgentLoopManager.to_dataproto` 目前只产出 `input_ids`、`attention_mask`、`response_mask` 与 `prompts`，没有 `uid`、`answers`、`prompt_length`、`rollout_log_probs`、`truncated`，因此不能直接送进 `PPOTrainer.train_step` 的 reward/GRPO 链路。它是“轨迹与 mask 的独立演示”，不是 MiniVerl 的 RL 训练入口；要做多轮 RL，需要补齐这些字段并加 turn-level advantage，见附录 P 的扩展点。

## 26.11 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| 模型不调用工具 | prompt/模板/奖励 | 加格式示例、格式奖励 |
| 工具调用解析失败 | 解析器/格式 | 打印原始输出、换 parser |
| observation 被训练 | mask 错 | 检查 token mask |
| 轨迹越来越长 | 没有轮次/长度限制 | max turns、长度惩罚 |
| 训练/推理 tokenizer 不一致 | 版本/模板 | sanity check |
| 工具超时拖垮 batch | 同步执行 | 异步、超时、并发限制 |

## 26.12 轮级信用分配：token、turn 与 trajectory

多轮 Agent 的奖励通常只在轨迹结束时给出。把它摊到 token 上有三种粒度：

| 粒度 | 信号 | 优点 | 缺点 |
|---|---|---|---|
| token | 每个 token 一个优势 | 与 PPO 天然兼容 | 长轨迹方差大 |
| turn | 每轮一个优势，广播到该轮 token | 与工具调用语义对齐 | 需要 turn 边界 |
| trajectory | 整条轨迹一个优势 | 实现最简单 | 无法区分哪轮出错 |

### turn-level 回报

设第 $t$ 轮结束后的奖励为 $r_t$，轮级折扣 $\gamma_{\text{turn}}$：

$$
G_t=\sum_{k\ge t}\gamma_{\text{turn}}^{k-t}r_k,
\qquad
A_t=G_t-b(s_t).
$$

然后把 $A_t$ 广播给第 $t$ 轮的所有 assistant token。若第 $t$ 轮有 $n_t$ 个有效 token，有两种归一化：

$$
A_{t,i}=A_t
\quad\text{或}\quad
A_{t,i}=\frac{A_t}{n_t}.
$$

前者让长轮次贡献更大，后者让每轮等权。选哪种要与 `loss_agg_mode` 一起看，否则会重新引入长度偏置。

### 实现需要的字段

verl 的 `DataProto` 本身不强制 turn 结构；你需要在数据里显式携带：

- `turn_ids`：每个 token 属于第几轮（observation 可以是 -1 或沿用上一轮）；
- `turn_mask`：哪些 token 是该轮的 assistant token；
- `turn_rewards`：每轮结束时的奖励（可选，用于调试）；
- `response_mask`：最终参与策略损失的 token。

然后有两种落地方式：

1. **在 reward manager 里做**：把轮级奖励写成 token-level rewards，让现有 GAE/GRPO 处理；
2. **在 advantage 层做**：先算 turn-level 优势，再广播到 token。这需要自定义 `compute_advantage` 或 trainer 后处理。

第一种改动小，但轮级折扣会被 token 级 GAE 的 $\gamma$ 重新解释；第二种语义更干净，但要改数据契约。没有一种“免费”的方案。

### 常见错误

- 把 observation token 的 value 纳入损失；
- 用 token 数归一化 turn 优势，却又用 token-mean 聚合损失，导致重复加权；
- 工具失败的一轮给负奖励，但模型实际上正确调用了工具；
- 轮次上限截断没有 bootstrap（见 2.11）。

## 26.13 多智能体与自博弈接口

Agent RL 不一定只有一个策略。常见形态：

- **自博弈**：同一个模型扮演生成者与评判者/对手；
- **多智能体协作**：planner + executor + verifier，各自有奖励；
- **对抗训练**：red team 与 defender 交替更新。

在 verl 里落地时，核心问题不是“能不能跑两个模型”，而是**数据契约**：

1. 一条轨迹里哪些 token 属于哪个策略？
2. 两个策略的奖励如何分配？
3. 对手更新后，旧轨迹的 off-policy 程度如何度量？
4. 训练与 rollout 的权重同步是否要分开？

最简单的做法是“交替训练 + 共享 replay buffer”：A 策略采样、B 策略评分，然后各自更新。它不需要改 verl 的核心，只需要自定义 reward 和数据管线。真正困难的是非平稳性：对手一变，旧数据的最优性就失效，必须用 staleness 阈值或重要性修正。

自博弈与树搜索的更多内容见附录 K。

## 26.14 环境接口与沙箱池

工具调用是“一次函数调用”，环境是“有状态、可多步交互的任务”。当任务需要 `reset/step`（代码仓库、浏览器、游戏、机器人）时，只靠 `tools_kwargs` 不够，需要一个明确的环境协议。

一个最小接口：

```python
class Env:
    def reset(self, task_id: str, seed: int) -> dict: ...
    def step(self, action: dict) -> tuple[dict, float, bool, dict]: ...
    def close(self) -> None: ...
```

四个必须显式化的字段：

- **episode_id / state_id**：同一任务可能并行多个 episode，日志、重试、回放都要能区分；
- **seed 与 env_version**：环境行为变了，旧轨迹就不可比；
- **done / truncated**：真正结束与被步数/时间上限截断要分开，后者可能需要 bootstrap；
- **error 类型**：工具错误、环境错误、超时、策略格式错误，处理方式不同。

### 沙箱池

生产环境不会为每个请求新建容器。常见做法是维护一个预热池：

```text
pool_size = min(max_concurrency, 任务并发上限)
acquire -> reset -> 多步 step -> close/recycle -> release
```

要设：

- **最大并发**：防止 512 个 rollout 同时打爆沙箱；
- **单任务超时**：包括 reset、每步、总时长；
- **资源限额**：CPU/内存/磁盘/进程数/文件描述符；
- **回收策略**：跑过 N 个任务或检测到污染后重建容器；
- **幂等 request id**：重试不重复执行副作用。

### 回放与确定性

一条 Agent 轨迹要能回放，至少记录：

```text
task_id, episode_id, seed, env_version
每步的 action、observation、reward、done/truncated、error
工具/依赖版本
```

没有这些，reward 异常时无法判断是模型错、环境错，还是数据错。回放不需要逐位一致（外部服务可能变），但要能重现“当时看到的状态”。

### 与 AgentLoop 的接口

- 每个 trajectory 开始前 `reset`，结束时 `close`；
- 工具/环境的 observation 一律 `response_mask=0`；
- 最终奖励放在最后一个 assistant token；
- 环境返回的 `truncated` 要保留并写进轨迹；注意 v0.9.1 的 GAE 没有 `truncated` 开关（一律按终止处理），MiniVerl 的 bootstrap 是教学扩展（第 2.11 节）；
- 多轮预算（最大轮次、最大 token、最大墙钟）在 AgentLoop 层统一控制。

验收：用假环境 + 固定 seed 跑 20 条轨迹，断言 episode_id 唯一、observation mask 全 0、截断样本被正确标记、失败样本有 error 类型。

## 26.15 ToolRegistry：从 schema 到执行、奖励与释放

前面的 AgentLoop 把工具当成一个 `call(name, argument) -> str` 的函数。这在教学上够用，在生产上不够：工具需要参数 schema、实例状态、超时、奖励与释放。v0.9.1 把这些放进 `verl/tools/`，本教程的 MiniVerl 也在 `mini_verl/tools.py` 里实现了同构的简化版。

### 五段生命周期

```text
schema  ──► create(instance_id) ──► execute(params) ──► calc_reward() ──► release()
  │              │                        │                  │               │
  │              │                        │                  │               └─ 清理资源
  │              │                        │                  └─ step reward
  │              │                        └─ 参数校验 + 执行 + 错误封装
  │              └─ 每条轨迹一份状态
  └─ 注入 prompt / 请求
```

为什么是"每条轨迹一份实例"而不是全局单例？因为浏览器会话、数据库事务、代码解释器的变量表都是有状态的；并发轨迹共享一个实例会串状态。`create` 的 `instance_id` 必须唯一，重复 id 应当拒绝。

### schema 是运行时契约

`ToolSchema.from_callable` 从函数签名推断 JSON schema：类型注解 → `type`，默认值 → 是否 required，docstring 首行 → description。`FunctionTool` 用它生成 OpenAI 格式的 schema。

三个工程细节：

- `*args/**kwargs` 无法表达为固定 schema，注册时直接拒绝；
- 参数错误不应让 AgentLoop 崩溃，执行层要把它转成 `error: TypeError` 之类的 observation；
- 超时是协作式的：Python 线程杀不掉，`timeout` 只能让调用方停止等待。真正的超时必须在进程/容器层。

### 注册表与重名

上游的注册入口是 `load_all_tools`（不是名为 `ToolRegistry` 的类），它合并两类工具：

- native：继承 `BaseTool`，在 YAML 里声明 `class_name/config/tool_schema`；
- function：`@function_tool` 装饰的普通函数，由 `function_tool_path` 加载。

重名必须拒绝，而不是后者覆盖前者。原因很实际：插件式系统里，两个模块可能都注册 `search`；静默覆盖会让线上行为随 import 顺序变化，且很难排查。v0.9.1 的 `load_all_tools` 在合并前做 collision check；MiniVerl 把同样的语义包进 `ToolRegistry`，抛 `ToolCollisionError`。

### 从工具到奖励

`execute` 返回 `(ToolResponse, reward, metrics)`。reward 是 step reward，不是终局 reward。设计时要回答：

- 这个信号是"工具成功"还是"任务进展"？
- 它与终局 reward 的量级是否匹配？
- 它会不会诱导模型刷工具调用次数？
- observation token 是否被错误地算进奖励归属？

建议日志里分开记录 `reward/tool`、`reward/outcome`、`reward/total`，并监控调用次数分布。

### MiniVerl 的 tools.py

`mini_verl/tools.py` 实现了：

- `ToolSchema` / `ToolResult` / `ToolInstance`；
- `BaseTool` 的 `create/execute/calc_reward/release` 与 legacy `__call__`；
- `FunctionTool`（签名推断、异常/超时封装）与 `@function_tool`；
- `CalculatorTool`（AST 白名单）；
- `ToolRegistry`（合并、冲突检查、OpenAI schema 导出、legacy `call`）。

`agent.py` 复用这个注册表，保持旧 API 不变。`tests/test_tools_registry.py` 覆盖 schema、装饰器、异常、超时、实例隔离与冲突；`examples/23_tool_registry.py` 跑通完整链路。

沙箱、安全与多租户的完整清单见附录 T。

## 26.16 过程奖励、工具反馈与轮级信用分配

Agent 轨迹天生比单轮生成更适合过程奖励：一次 tool call、一次 observation、一次 assistant 修正，都是天然的步骤边界。但也正因为结构复杂，**“把工具反馈当奖励”**是最常见的错误来源。

### 步骤边界就是 turn 边界

单轮生成可以按换行或固定 token 数切步；Agent 轨迹应该按 turn 切，但要注意：

- 一个 turn 可能包含**多个并行 tool call**，它们的 observation 可能交错返回；
- 有的框架把 assistant 的 `tool_call` 参数与最终文本算作两个 span；
- 被截断的 turn 没有完整结束，不能当完整步骤；
- observation 的 token 不属于策略动作，不能进入策略损失，但**必须保留在时间轴上**让 GAE 穿过（19.7）。

一个可落地的契约是：`DataProto.non_tensor_batch["step_boundaries"]` 存每个 turn 的 end index，计数口径是**相对 `response_mask=1` 的有效 response token**（不是原始列下标）；observation 位置 `response_mask=0`，不占边界序号，但仍留在序列里。这样 PRM 可以只对 assistant span 打分，GAE 仍然能看到完整序列。

### 三类信号要分开

| 信号 | 来源 | 用途 | 风险 |
|---|---|---|---|
| 终局结果 | verifier/测试/人工 | 真正的目标 | 稀疏、延迟 |
| 过程分数 | PRM/步骤验证器 | shaping、诊断、重排 | 可被 hack、分布漂移 |
| 工具反馈 | 执行成功/失败、返回码 | 即时的环境信号 | 与任务进展不等价；诱导刷调用 |

日志里至少分开记 `reward/outcome`、`reward/process`、`reward/tool`、`reward/total`。如果把三者混成一个标量，出了问题时无法判断是“任务变难了”还是“工具奖励被刷了”。

### 工具反馈不是奖励

一个工具返回 `success=true` 只说明调用没报错，不说明这一步对任务有贡献。典型反例：

- 反复调用同一个查询工具，每次 `success`，但没有任何进展；
- 工具返回格式正确但内容错误；
- 模型学会先调用一个必然成功的无意义工具来拿奖励。

如果要把工具反馈进奖励，至少做三件事：

1. **与终局结果联合**：工具奖励只作为 shaping，最终仍由 verifier 决定；
2. **限制重复**：同一工具/同一参数的重复调用不给奖励，或按边际信息量给；
3. **记录调用轨迹**：工具名、参数哈希、耗时、返回码、是否被截断；否则无法做归因。

### PRM 在 Agent 上的接法

`ProcessRewardManager`（17.17）可以直接消费 turn 边界：

```python
data.non_tensor_batch["step_boundaries"] = np.asarray(turn_ends, dtype=object)
scored = manager.run(data)   # 每个 turn 一个分数，按 aggregation 合成轨迹奖励
```

三个注意点：

- **PRM 的输入要包含 observation**，否则它看不到工具返回，无法判断这一步是否有效；
- **但 PRM 的标签只标 assistant 动作**：observation 是环境给的，不该让策略为它负责；
- **聚合方式要匹配任务**：多轮任务常用 `min`（任何一步失败都否决）或按关键 turn 加权；`last` 容易被最后的礼貌性回答骗过。

如果要做 turn-level GAE，公式与 19.12 相同，只是 $r_k$ 来自 turn 级奖励、$V_k$ 取该 turn 结束位置的 value。MiniVerl 目前只实现轨迹级聚合；`prm_step_scores` 与 turn 边界可作为扩展输入。

### 成本

每一步一次 PRM 前向，长 Agent 轨迹的奖励成本可能超过生成成本。工程上：

- 只对关键 turn 打分（如包含工具调用、包含最终答案的 turn）；
- 缓存相同前缀的分数（前缀缓存不只用于 KV，也可用于 reward）；
- 异步打分，不阻塞 rollout；
- 对超长轨迹做截断或分层采样。

## 本章小结

- 多轮 Agent 的轨迹包含 assistant 与 tool observation，只有 assistant token 参与策略损失。
- AgentLoop 负责 chat template、工具解析、执行、拼接与 mask。
- v0.9.1 用 AgentLoopWorker/Manager 与 LLM server 支持异步多轮。
- 工具执行要工程化：schema、实例生命周期、超时、校验、截断、错误处理与释放。
- ToolRegistry 合并 native/function 工具并拒绝重名；工具奖励要与终局奖励分开记录。
- 多轮信用分配可以按 token、turn 或 trajectory；turn-level 需要显式的边界与归一化契约。
- 过程奖励可以按 turn 边界接入；工具反馈是环境信号，不是任务奖励，必须与终局 verifier 分开记录。
- 自博弈/多智能体的难点是非平稳性与奖励分配，核心改动在数据契约而不在 PPO。
- MiniVerl 用 calculator + 脚本化生成器演示轨迹与 mask。

## 延伸阅读

- `verl/experimental/agent_loop/`
- 多轮文档：<https://verl.readthedocs.io/en/latest/sglang_multiturn/multiturn.html>
- 附录 T：工具系统、函数调用与沙箱执行。
- 下一章：异步 RL：one-step off-policy 与 fully async。

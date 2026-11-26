# 第 46 章 Agent 工程闭环：会话、模拟评测、在线评分与问题分诊（MLflow 3.18）

> 第 39 章把单条 trace 和单轮评测跑通了，第 44 章把评测数据变成了可版本化资产，第 45 章补上了上线后的漂移监控。但 Agent 的失败往往不在“某一个请求”，而在“一段对话”：用户追问三轮后才暴露工具参数错、上下文丢、目标没完成。本章把这个缺口补上：先讲真实 MLflow 3.10–3.18 的会话模型、模拟用户、多轮 scorer、在线评测、Issues 分诊、统计比较与 Skill Registry，再把同样的闭环写进 MiniMLflow 的 `agentops.py`，用 17 个测试和示例 19 证明它能跑。真实 API 仍带实验标记，执行前以你锁定的版本文档为准。

## 学习目标

- 分清 **trace、turn、session、evaluation run** 四个粒度，知道每种判断应该挂在哪一层。
- 用 `mlflow.trace.session` 组织多轮对话，理解会话级 assessment 为什么通常挂在第一条 trace 上。
- 用 `ConversationSimulator` 与 `generate_test_cases` 把“用户会怎么问”变成可重放、可版本化的测试资产。
- 设计单轮与多轮 scorer 的分工，知道 LLM judge、确定性 code scorer 与人工反馈各自的证据强度。
- 用 `ScorerSamplingConfig` 做在线抽样评分，理解采样单位、过滤条件、成本与幂等。
- 用 MLflow 3.18 的 Issues 自动问题检测做分诊，知道它的输入要求、证据链与误报边界。
- 用 `compare_evaluations`/`assert_improved` 做配对比较与 CI 门禁，不被“平均值涨了 0.02”骗过。
- 理解 Skill Registry 与 MCP Registry 的区别，能把 Agent 的“能力供应链”纳入版本与审批。
- 把上述概念映射到 Mini 的 `agentops` 模块，并诚实列出生产差距。

## 46.1 Agent 为什么需要独立的运营层

传统模型的输入是一个特征向量，输出是一个分数；Agent 的输入是一段自然语言目标，输出是一串**带副作用的动作**。这带来四个变化：

| 维度 | 传统模型 | Agent |
|---|---|---|
| 成功定义 | 单次预测正确 | 多轮后目标达成，且过程安全 |
| 失败定位 | 特征/标签/模型 | 检索、规划、工具、上下文、模型、策略 |
| 非确定性 | 训练/推理有随机性 | 用户、模型、工具、环境全都非确定 |
| 成本 | 每次推理固定 | 轮数 × 模型调用 × 工具调用 × judge |
| 证据 | 一条预测记录 | 一段会话 + 每次工具调用的副作用 |

因此，**一次 trace 只能回答“这一轮发生了什么”，不能回答“这段对话有没有解决用户的问题”**。你需要一个新的聚合层：

```text
Session
  ├── Trace (turn 1)
  │     ├── Span: retrieve
  │     ├── Span: tool.search_orders   ← 有副作用
  │     └── Span: llm.generate
  ├── Trace (turn 2)
  └── Trace (turn 3)
        └── Assessment: conversation_completeness = 1.0
```

这个聚合层要回答四个运营问题：

1. **有没有解决？** 会话级完成度、用户沮丧度、最终答案质量；
2. **哪里坏的？** 工具失败、上下文丢失、策略违规、延迟超预算；
3. **是不是变差了？** 同一评测集上，新旧版本的配对差异是否显著；
4. **先修什么？** 把重复失败聚成 issue，按影响面与严重度排序，再进入回归测试。

这四件事分别对应本章的会话、模拟/评分、比较、分诊四节。

## 46.2 MLflow 3.10–3.18 的 Agent 能力地图

先把时间线摆清楚。下表只列与 Agent 运营直接相关的能力；版本号来自 2026-11 的 3.18 基线，功能状态以官方文档为准。

| 能力 | 版本 | 作用 | 关键 API / 入口 |
|---|---|---|---|
| 多轮评测与会话模拟 | 3.10（实验） | 按 session 分组 trace；用 LLM 扮演用户 | `search_sessions`、`ConversationSimulator`、`generate_test_cases` |
| 自动问题检测 | 3.11 起，3.18 增强 | 从 trace 中找重复失败并聚成 issue | Issues 标签页、定时检测、证据 span |
| 在线 code scorer | 3.18（实验） | 对生产 trace 自动跑确定性 scorer | `@scorer`、`register`、`start(ScorerSamplingConfig(...))` |
| 评测运行统计比较 | 3.18（实验） | 同一数据集上配对比较两个版本 | `compare_evaluations`、`assert_improved` |
| Skill Registry | 3.18（实验） | 版本化 Agent 技能并拉取到编码代理 | `mlflow skills pull skills:/name@alias` |
| 分层会话 | 3.18 | 支持 session 级嵌套/列表化 session id | 会话视图与 `session_level` |
| 多模态 judge / TraceAudio | 3.15 / 3.18 | 音频、图像等附件进入评测与查看 | Trace 附件、`TraceAudio` |

三个容易误解的点：

- **会话模拟不是“造数据”这么简单**。它同时生成用户行为、目标检查与可重放的测试用例；模拟器本身是一个需要版本化、需要评测的模型组件。
- **在线评分不是“把评测脚本挂到线上”**。它要求异步执行、采样、幂等、权限隔离与成本上限；3.18 的 code scorer 还明确禁止网络访问，避免把评测变成新的攻击面。
- **Issues 不是监控指标的替代品**。它擅长从语义上聚类“反复出现的坏答案/坏工具调用”，不擅长发现特征漂移或标签延迟；第 45 章的监控仍然必要。

## 46.3 会话的数据模型：一条 tag 撑起一层语义

### 46.3.1 用 trace tag 定义 session

MLflow 不要求你新建一张 session 表。约定是：**同一段对话的每条 trace 都带同一个 `mlflow.trace.session` tag**。写入方式有两种，随版本可能是 `tags=` 或 `metadata=`，执行前用 `help(mlflow.update_current_trace)` 确认：

```python
import mlflow

@mlflow.trace
def chat_turn(question: str, session_id: str) -> str:
    mlflow.update_current_trace(tags={"mlflow.trace.session": session_id})
    return agent.invoke(question)
```

一次三轮回话就是三条 trace、三个 trace_id、一个 session_id。不要把整个会话塞进一条 trace：那样会丢失每轮的延迟、token、工具调用和独立状态，也无法只重跑失败的那一轮。

### 46.3.2 读取会话

按 tag 过滤：

```python
traces = mlflow.search_traces(
    experiment_ids=["1"],
    filter_string="tags.`mlflow.trace.session` = 's-123'",
    return_type="list",
)
```

按 session 聚合：

```python
sessions = mlflow.search_sessions(
    locations=["1"],
    max_results=50,
)
for session in sessions:
    print(session.session_id, len(session.traces))
```

`search_sessions` 的返回结构、分页参数与 `max_results` 语义随版本变化；不要假设它和 `search_traces` 完全同构。生产代码应把“取哪段会话”写成明确的查询，而不是依赖默认排序。

### 46.3.3 会话级 assessment 挂在哪

多轮 scorer 的结论属于整个会话，但 assessment 必须挂在某条 trace 上。MLflow 的约定是**挂在会话的第一条 trace**，并带上 session 标识。这样：

- `search_traces(filter_string="assessments.conversation_completeness >= 0.8")` 能一次命中整个会话；
- UI 打开会话时能从第一条 trace 看到总评；
- 单轮 assessment 仍挂在各自的 turn 上，两种粒度不会互相覆盖。

Mini 的 `score_sessions` 采用同一约定。代价是：如果第一条 trace 被删除或归档，会话级结论也会失去锚点。生产系统要么禁止单独删除会话首条 trace，要么把会话级结论同时写入独立的 session 资产。

### 46.3.4 会话数据的六个不变量

1. **session_id 稳定**：由调用方生成（UUID/请求 id），不要用“第一条消息的哈希”，否则用户改一个字就变成新会话。
2. **一个会话一个实验**：跨实验的会话会让搜索、权限与成本归因全部失真。
3. **turn 顺序可重建**：至少保留 timestamp + turn index；同一毫秒内的并发写入要有稳定次序。
4. **工具副作用可追溯**：工具名、参数摘要、结果状态、外部请求 id 必须进 span。
5. **PII 有边界**：会话是长期资产，原始输入输出要脱敏、截断、设保留期。
6. **会话可关闭**：超时、用户离开、人工接管都要有终止标记，否则“未完成”会被误判为失败。

## 46.4 模拟用户：把“用户会怎么问”变成测试资产

### 46.4.1 为什么不能只靠历史会话

历史会话有三个问题：

- **冷启动**：新 Agent 没有历史；新业务没有长尾；
- **选择偏差**：你只看到已经发生的用户，看不到被门槛挡住的用户；
- **不可重放**：真实用户不会为了你的回归测试再问一遍。

模拟用户解决的是“覆盖度”和“可重放性”，不是“真实性”。它永远不能替代真实反馈，只能把已知的目标、人格与约束变成可重复的压力测试。

### 46.4.2 真实 MLflow 的 ConversationSimulator

3.10 起提供实验性的 `ConversationSimulator`，作为 `mlflow.genai.evaluate` 的 `data` 参数：

```python
import mlflow
from mlflow.genai.simulators import ConversationSimulator
from mlflow.genai.scorers import ConversationCompleteness, UserFrustration

simulator = ConversationSimulator(
    test_cases=[
        {"goal": "了解退款政策", "persona": "普通消费者"},
        {
            "goal": "排查部署失败",
            "persona": "急躁的资深工程师",
            "context": {"plan": "enterprise"},
            "expectations": {"must_mention": "日志"},
            "simulation_guidelines": "先给错误信息，再逐步补充环境细节",
        },
    ],
    max_turns=5,
)

results = mlflow.genai.evaluate(
    data=simulator,
    predict_fn=agent.invoke,
    scorers=[ConversationCompleteness(), UserFrustration()],
)
```

测试用例的字段：

| 字段 | 必填 | 含义 |
|---|---|---|
| `goal` | 是 | 用户想达成什么；模拟器与目标检查器都依赖它 |
| `persona` | 否 | 用户画像，影响语气、耐心与信息完整度 |
| `context` | 否 | 传给 `predict_fn` 的额外参数（套餐、租户、语言） |
| `expectations` | 否 | 目标达成的判据；字段随版本扩展 |
| `simulation_guidelines` | 否 | 控制信息透露节奏、追问方式、隐式约束 |

行为要点：

- 模拟器用 LLM 扮演用户，`max_turns` 控制上限（文档默认常见为 10，示例常写 5，按版本文档确认）；
- 生成的会话 id 通常带 `sim-` 前缀，出现在 Sessions/会话视图；
- 目标检查器也是 LLM；它决定“什么时候停”，因此 **模拟器 + 目标检查器 + scorer 是三个需要分别版本化的模型组件**；
- 模拟调用不应污染你的业务 trace；实现上通常会绕过 tracing，具体行为以版本文档为准。

### 46.4.3 从真实会话生成测试用例

`generate_test_cases` 从已有 session 中抽取目标、人格与模拟指南：

```python
from mlflow.genai.simulators import ConversationSimulator, generate_test_cases
from mlflow.genai.datasets import create_dataset

sessions = mlflow.search_sessions(locations=["1"], max_results=50)
cases = generate_test_cases(sessions)

# 固化成语料，保证下次回归用的是同一批场景
dataset = create_dataset(name="agent-conversation-cases")
dataset.merge_records([{"inputs": case} for case in cases])

simulator = ConversationSimulator(test_cases=cases)
```

“从线上会话生成用例”有两个风险：

1. **把 bug 固化成期望**：线上已经失败的会话，其轨迹不应被当作目标达成的范本；
2. **把 PII 带进评测集**：生成前要脱敏、去重、审查，并把来源与许可记录下来。

正确做法是“生成候选 → 人工/规则筛选 → 进入 golden set → 绑定 digest”。第 44 章的 dataset 治理在这里直接复用。

### 46.4.4 模拟器的四个偏差

- **人格偏差**：模拟器倾向于礼貌、配合、信息完整；真实用户会含糊、打断、换话题。
- **目标偏差**：goal 写得越具体，模拟器越容易“配合你成功”；goal 应描述用户意图，而不是标准答案。
- **停止偏差**：目标检查器过早判定完成，会掩盖“答非所问”。
- **成本偏差**：每次模拟至少两次 LLM 调用（用户 + 检查器），再加 scorer；大规模回归要先算预算。

因此，模拟结果只能回答“在这组假设下系统是否稳定”，不能回答“真实用户是否满意”。两者要同时看。

### 46.4.5 Mini 的 ScriptedConversationSimulator

Mini 不调用 LLM。它把用户回合显式写进 `SimulationCase`，因此完全确定、零成本、可离线跑：

```python
from mini_mlflow.agentops import ScriptedConversationSimulator

simulator = ScriptedConversationSimulator(
    [
        {
            "case_id": "refund",
            "goal": "learn the refund window",
            "turns": ["hello", "can I get a refund?"],
            "expectations": {"must_include": ["30 days"]},
        }
    ],
    max_turns=4,
)
results = simulator.run(
    support_agent,
    client=client,
    experiment_id=experiment.experiment_id,
)
```

`run` 会为每一轮写一条 trace：

- `tags = {"mlflow.trace.session": <sim-id>, "agent.case_id": <case-id>}`；
- `request = {"message": user}`，`response = {"answer": assistant}`；
- 失败时 span/trace 标 `ERROR`，事件里保留异常类型与消息；
- 目标检查用 `must_include` / `must_not_include`，不调用模型。

这不是“简化版 LLM 模拟器”，而是**可解释的下限**：它证明会话聚合、评分、分诊、比较的数据链路是通的；真实模拟器只是把“用户脚本”换成模型生成。

## 46.5 多轮 scorer：把“感觉不对”变成可比较的数

### 46.5.1 单轮与多轮的分工

| 判断 | 粒度 | 典型 scorer | 证据 |
|---|---|---|---|
| 这一轮答案是否有害 | turn | `Safety`、`ConversationalSafety` | 单条输出 |
| 这一轮是否引用了正确文档 | turn | 检索命中、引用一致性 | span + 文档 id |
| 整段对话是否完成目标 | session | `ConversationCompleteness` | 全部 turn |
| 用户是否越来越沮丧 | session | `UserFrustration` | 用户 turn 的语气/重复 |
| 工具链是否可靠 | session | 工具成功率、重试次数 | TOOL span |
| 成本是否超预算 | session | token/调用次数/金额 | LLM span 属性 |

单轮 scorer 可以在每次调用后立即跑；会话级 scorer 必须等会话结束或窗口关闭。把两者混在一个采样率里，会导致“会话还没结束就被判定失败”。

### 46.5.2 真实 MLflow 的内置多轮 scorer

3.10 起提供 `ConversationCompleteness`、`UserFrustration` 等会话级 scorer；3.15 增加多模态 judge。典型用法：

```python
from mlflow.genai.scorers import (
    ConversationCompleteness,
    ConversationalSafety,
    UserFrustration,
)

traces = mlflow.search_traces(
    experiment_ids=["1"],
    filter_string="tags.`mlflow.trace.session` = 's-123'",
    return_type="list",
)

result = mlflow.genai.evaluate(
    data=traces,
    scorers=[
        ConversationCompleteness(),
        UserFrustration(),
        ConversationalSafety(),
    ],
)
```

要点：

- 内置 scorer 往往依赖外部模型（文档示例常用 OpenAI），需要凭据、预算与数据边界审批；
- 分数不是 ground truth；judge 的模型版本、指令、温度、解析失败率都要记录；
- “沮丧度”这类主观维度要做人工校准，至少抽 50–100 条会话对齐；
- 会话级结果通常落在第一条 trace 的 assessment 上，并带 session 标识。

### 46.5.3 Mini 的确定性 scorer

Mini 提供五个可组合 scorer：

| Scorer | 值 | 含义 | 局限 |
|---|---|---|---|
| `ConversationCompleteness(required_terms)` | 0–1 | 最终回答覆盖了多少必含词 | 词面匹配，不懂语义 |
| `UserFrustration()` | 0–1 | 含沮丧线索的用户 turn 占比 | 词表固定，反讽失效 |
| `ConversationalSafety(patterns)` | 0/1 | 助手输出是否命中禁用模式 | 正则可被绕过 |
| `ToolSuccessRate()` | 0–1 | 工具 span 成功率 | 无工具时记 1.0 |
| `LatencyBudget(max_ms)` | 0/1 | 会话墙钟延迟是否超预算 | 不含排队/网络分层 |

```python
from mini_mlflow.agentops import (
    ConversationCompleteness,
    ConversationalSafety,
    LatencyBudget,
    ToolSuccessRate,
    UserFrustration,
    score_sessions,
)

sessions = search_sessions(client, [experiment.experiment_id])
assessments = score_sessions(
    client,
    sessions,
    [
        ConversationCompleteness(["30 days"]),
        UserFrustration(),
        ConversationalSafety(),
        ToolSuccessRate(),
        LatencyBudget(max_ms=5000),
    ],
)
```

`score_sessions` 的关键设计：

- `source_type="CODE"`，与 `HUMAN`、`LLM_JUDGE` 明确区分；
- `source_id="agentops"`，同一 scorer 重跑会 upsert，不会产生重复 assessment；
- metadata 带 `session_id`、`turn_count`，便于反查；
- 目标 trace 是会话第一条，遵循 MLflow 约定。

这些 scorer 的价值不是“准确”，而是**便宜、可解释、可作为回归门禁的下限**。它们抓不到语义问题，但能稳定抓住“空回答、工具全挂、超时、明显违规”。

## 46.6 在线评测：只评该评的，且只评一次

### 46.6.1 真实 MLflow 的 code scorer 生命周期

3.18 的实验性在线评测把 code scorer 注册到服务端，再按采样配置自动评估新 trace：

```python
import mlflow
from mlflow.genai.scorers import ScorerSamplingConfig, scorer

@scorer
def answer_length(outputs) -> int:
    return len(str(outputs))

registered = answer_length.register(
    name="answer_length",
    experiment_id="1",
)
registered.start(
    experiment_id="1",
    sampling_config=ScorerSamplingConfig(sample_rate=0.1),
)
```

生命周期：

- `register()`：把 scorer 函数注册到服务端；
- `start(sampling_config=...)`：开始自动评估；只注册不启动不会评分；
- `update(...)`：返回带新配置的 scorer（不可变更新）；
- `stop()`：把采样率设为 0，保留注册；
- `get_scorer()` / `list_scorers()` / `delete_scorer()`：注册表管理。

配置字段：

| 字段 | 含义 |
|---|---|
| `sample_rate` | 0–1，按 trace/会话抽样的比例 |
| `filter_string` | 只评估满足条件的 trace，例如 `attributes.status = 'ERROR'` |

### 46.6.2 在线 code scorer 的硬约束

3.18 的文档明确了几条限制，直接决定架构：

- 需要服务端开启 `MLFLOW_SERVER_ENABLE_CUSTOM_SCORERS=true`；
- 只支持 `@scorer` 装饰的函数，不支持任意 `Scorer` 类；
- scorer 代码运行在隔离容器（job container）里：无网络、只读文件系统、无特权、有 CPU/内存/进程/时间上限；
- 因此 code scorer **不能调用 LLM 或外部 API**；需要模型的判断应使用 LLM judge；
- 不支持 `expectations`（线上没有 ground truth）；
- 一次只跑一个 scorer，不做 ensemble；
- 只保存函数源码，lambda、`async def`、交互式定义的函数不可用；
- UI 的“对已有 trace 运行”只支持 LLM judge；code scorer 请用 `mlflow.genai.evaluate()`。

这些限制不是“产品缺陷”，而是安全边界：线上自动执行用户代码，必须假设代码不可信。

### 46.6.3 采样策略

在线评分的成本 = 流量 × 采样率 × 单次评分成本。采样要按**会话**而不是按 trace：

- 只抽中会话的一条 trace，会得到不完整的多轮结论；
- 同一会话的多次重试应共享采样结果，否则一半评分、一半不评分；
- 错误会话应 100% 保留，正常会话低比例抽样（分层采样）；
- 采样键要稳定（session_id + scorer 名 + 日期），重跑不能翻转样本。

一个可落地的分层策略：

```text
if session.status == ERROR:            rate = 1.0
elif session.turn_count >= 5:          rate = 0.2
elif session.tool_failures > 0:        rate = 0.5
else:                                  rate = 0.02
```

再叠加每日预算上限与“每实验每 scorer 每小时最多 N 次”的熔断。

### 46.6.4 Mini 的批式在线评分

Mini 不跑后台 worker。`score_online` 对当前 trace 做一次批处理，用确定性哈希决定是否采样：

```python
from mini_mlflow.agentops import (
    OnlineScorer,
    ScorerSamplingConfig,
    ToolSuccessRate,
    score_online,
)

assessments = score_online(
    client,
    [
        OnlineScorer(
            ToolSuccessRate(),
            ScorerSamplingConfig(
                sample_rate=1.0,
                filter_string="attributes.status = 'ERROR'",
            ),
        )
    ],
    experiment_ids=[experiment.experiment_id],
)
```

实现细节：

- 采样键是 `source_id:scorer_name:session_id`，同一输入永远得到同一结果；
- 采样单位是 session；`filter_string` 先筛 trace，再取包含命中 trace 的会话；
- 写 assessment 走 `log_feedback`，同一 `(trace, name, source)` 幂等；
- 没有后台任务、重试、DLQ、租户隔离与预算熔断，这些在第 46.11 节列为生产差距。

## 46.7 自动问题检测：从“看 trace”到“分诊 issue”

### 46.7.1 真实 MLflow 3.18 的 Issues

3.18 的 Issues 标签页把重复失败聚成可分诊的问题。官方文档描述的管线是：

```text
识别潜在问题
  → 结合人工反馈与执行路径
  → 去重/合并成 issue
  → 匹配已有 Open / Not an issue
  → 附加证据（span 走查、受影响 trace）
```

它需要：

- 有 trace 的实验；
- Linux/macOS 上的 tracking server；
- SQL 后端（SQLite/PostgreSQL）与后台任务执行；
- 可访问的模型（OpenAI/Anthropic/Gemini 等）或 AI Gateway。

工作流：

1. Issues 标签页 → Run issue detection；
2. 选择 provider/model 与 API key，查看预估成本；
3. 后台运行，结果按严重度、根因、类别分组；
4. 人工把 issue 标为 Open / Resolved / Not an issue；`Not an issue` 会作为反馈抑制后续误报；
5. 每个 issue 带可复制的修复 prompt 与代表性 trace id，可交给编码代理；
6. 支持每日/每周定时检测，并保留运行历史。

它擅长：

- 反复出现的策略幻觉（例如退款政策说错）；
- 工具调用参数缺失、schema 违规、重复调用；
- 语义上相似但字面不同的失败；
- 把多轮会话作为一个整体看根因。

它不擅长：

- 数据漂移、特征分布变化（第 45 章）；
- 标签延迟导致的性能衰减；
- 没有 trace 的系统性问题（网络、DB、部署）；
- 完全新的失败模式（没有历史聚类可依）。

### 46.7.2 Mini 的确定性分诊

Mini 用三个检测器和签名归一化实现“可解释的地板”：

| 检测器 | 触发条件 | 证据 | 建议动作 |
|---|---|---|---|
| `tool_failure` | TOOL span `status=ERROR` | 工具名、异常类型、消息 | 查下游、加超时/重试、降级 |
| `span_error` | 非工具 span 错误 | span 类型、异常 | 查代码/模型/解析 |
| `high_latency` | trace 延迟 > 阈值 | 延迟、阈值、trace | 查检索/模型/队列 |

```python
from mini_mlflow.agentops import detect_issues

traces = client.search_traces([experiment.experiment_id], max_results=200)
issues = detect_issues(traces, slow_ms=5000, min_count=2)
```

签名归一化会把：

```text
"timeout after 8123 ms"
"timeout after 9912 ms"
```

合并成：

```text
orders.lookup|TimeoutError|timeout after <n> ms
```

输出示例：

```json
{
  "kind": "tool_failure",
  "signature": "orders.lookup|TimeoutError|timeout after <n> ms",
  "count": 2,
  "severity": "high",
  "session_ids": ["orders-session"],
  "trace_ids": ["...", "..."]
}
```

设计取舍：

- **不做语义聚类**：不会把“退款说错”和“退货说错”合并；需要模型或人工；
- **不做跨天去重**：同一 issue 每天重新出现；生产要引入 issue id 与状态机；
- **证据有上限**：默认每条 issue 保留 10 条证据，避免报告爆炸；
- **严重度是规则**：工具失败或影响面 ≥5 直接 high；真实系统应结合业务权重。

### 46.7.3 分诊流程

一个可执行的分诊循环：

```text
每日检测
  → 新 issue：去重、定级、指派 owner
  → 已有 issue：更新影响面、检查是否复发
  → 修复：加回归用例（来自证据 trace）
  → 验证：同一数据集跑评测 + 对比
  → 关闭：标记 Resolved，保留证据
```

三个反模式：

- **只看数量不看影响面**：100 次低价值工具的失败，可能不如 1 次退款政策错误严重；
- **把 issue 当工单系统**：没有回归用例的修复会复发；
- **自动关闭**：模型说“已修复”不算证据，必须有过门禁的评测。

## 46.8 统计比较与 CI 门禁：别被平均值骗

### 46.8.1 真实 MLflow 的 compare_evaluations

3.18 引入实验性的 `mlflow.genai.compare_evaluations()`。它把两个评测运行按**同一数据集的行**配对，而不是比较两个独立均值：

```python
from mlflow.genai import compare_evaluations

result = compare_evaluations(
    candidate_run_id="<candidate-eval-run>",
    baseline_run_id="<baseline-eval-run>",
)
print(result.summary())
result.assert_improved(scorers=["correctness"], alpha=0.05)
```

`summary()` 每行一个 scorer，常见列：

| 列 | 含义 |
|---|---|
| `status` | `ok` 或 `insufficient_pairs`（可比较行少于 6） |
| `n_paired` | 配对样本数 |
| `value_type` | binary / numeric |
| `method` | 二值或数值检验方法 |
| `diff` | 配对均值差（candidate − baseline） |
| `ci_low` / `ci_high` | BCa bootstrap 95% 置信区间 |
| `p_value` | 检验 p 值 |
| `t_test_p_value` | 参考的 t 检验 p 值 |
| `n_nonzero` | 非零差异的样本数 |
| `greater_is_better` | 方向约定，`assert_improved` 依赖它 |

`assert_improved` 的语义：

- `insufficient_pairs` 直接失败（fail closed）；
- 只有“显著变好”才通过；显著变差或区间跨 0 都失败；
- 默认不做多重比较校正；同时比较 10 个 scorer 时，假阳性概率会上升。3.18 的 issue #27116 已在讨论 Holm/BH 校正与 `assert_not_regressed`，但不要把提案当成已发布 API。

### 46.8.2 为什么必须配对

假设 baseline 和 candidate 各跑 100 条，但数据集不同、难例分布不同，直接比较均值会把“题变难了”误判成“模型变差了”。配对比较只使用两边都存在的 case，并逐 case 求差：

```text
case-1: 0.5 → 0.8  (+0.3)
case-2: 0.9 → 0.9  ( 0.0)
case-3: 0.2 → 0.1  (-0.1)
...
mean_diff = mean(candidate - baseline)
```

配对消除了“题目难度”这个共同因素，统计功效通常更高。代价是：只能比较同一数据集；数据集版本一变，配对就失效。

### 46.8.3 五个统计陷阱

1. **多重比较**：10 个 scorer、每个 α=0.05，至少一个假阳性的概率约 40%。要么预注册主指标，要么做校正。
2. **实际显著性**：+0.001 在 10 万样本上也可能“显著”，但业务上无意义；要设 `min_delta`。
3. **非独立样本**：同一会话的多轮 trace 不是独立样本；会话级 scorer 应先在会话内聚合。
4. **选择偏差**：只评测“成功的会话”会高估质量；失败会话必须进入分母。
5. **评测集漂移**：baseline 和 candidate 用的数据集 digest 必须一致；否则比较的是题，不是模型。

### 46.8.4 Mini 的 PairedComparison

Mini 用百分位 bootstrap 实现同样的配对思想：

```python
from mini_mlflow.agentops import assert_improved, compare_paired

baseline = {f"case-{i}": 0.50 for i in range(8)}
candidate = {f"case-{i}": 0.62 + i * 0.01 for i in range(8)}
comparison = compare_paired(baseline, candidate, metric="quality", n_boot=500, seed=11)
print(comparison.to_dict())
assert_improved(comparison)
```

输出：

```json
{
  "metric": "quality",
  "n": 8,
  "baseline_mean": 0.5,
  "candidate_mean": 0.655,
  "mean_diff": 0.155,
  "ci_low": 0.14,
  "ci_high": 0.1725,
  "improved": 8,
  "regressed": 0,
  "tied": 0,
  "p_value": 0.001996,
  "significant": true
}
```

实现要点：

- 只取两边都有的 key；
- 对差值做有放回重采样，取 2.5%/97.5% 分位；
- p 值由 bootstrap 分布中跨 0 的比例近似，并设下限 `1/(n_boot+1)`；
- `assert_improved` 要求至少 6 对（对齐真实语义），且 `mean_diff > min_delta`、区间不跨 0、p < α；
- 没有 BCa、没有多重比较校正、没有分层抽样；这些是生产差距。

### 46.8.5 CI 门禁的设计

一个不会误伤的门禁应包含：

```text
1. 固定评测集 digest（第 44 章）
2. baseline 版本与 candidate 版本都记录 run id
3. 主指标预注册 + min_delta
4. 配对样本 >= 6（最好 >= 30）
5. 失败 case 白名单与 flaky 重跑策略
6. 显著变差 → 阻断；不显著 → 人工评审
7. 输出完整对比报告（CI、p、improved/regressed）
```

不要用“平均值涨了”作为发布依据；也不要用一次不显著的下降直接否定一个降低成本的改动。统计结论要和业务判断一起用。

## 46.9 Skill Registry：Agent 能力的供应链

### 46.9.1 Skill 与 MCP 的区别

| 资产 | 是什么 | 谁消费 | 风险 |
|---|---|---|---|
| Prompt | 文本模板 | 你的应用 | 注入、泄漏、版本漂移 |
| MCP server | 远程工具/上下文服务 | Agent 运行时 | 供应链、权限、工具 schema 漂移 |
| Skill | 指令 + 脚本/资源包 | 编码代理/Agent 框架 | 执行任意代码、提示注入、来源不可信 |

MCP 解决“Agent 能调用什么”，Skill 解决“Agent 知道怎么做”。3.18 的 Skill Registry（实验）把后者变成可版本化资产。

### 46.9.2 注册、别名与拉取

支持从本地目录、Git、OCI、ZIP 注册；版本不可变，别名可变：

```bash
# 拉取某个版本
mlflow skills pull skills:/code-review/1 --destination ./skills/code-review

# 拉取别名（常见为 production）
mlflow skills pull skills:/code-review@production --destination .claude/skills/code-review
```

URI 语义：

- `skills:/name/1`：精确版本；
- `skills:/name@production`：别名；
- `skills:/name` 或 `@latest`：动态解析最新。

关键约束：

- 目标目录必须不存在或为空，重复拉取请换新路径；
- 内容由客户端拉取，不由 tracking server 代理；
- UI 会按目标代理生成目录：Claude Code → `.claude/skills/<name>`，Codex → `.agents/skills/<name>`，Cursor → `.cursor/skills/<name>`，Copilot → `.github/skills/<name>`；
- 功能是实验性的，API/CLI 可能变化。

### 46.9.3 供应链治理清单

- **来源审批**：只允许白名单 Git/OCI 仓库；
- **版本固定**：生产用精确版本，别名提升走审批；
- **内容审查**：Skill 可能包含脚本，按代码审；
- **diff 可见**：版本间 diff 必须能在评审中看到；
- **权限最小化**：Skill 运行时的文件/网络权限单独限制；
- **撤销机制**：别名回滚 + 本地缓存清理；
- **审计**：谁在什么时候拉取了哪个版本。

Mini 没有 Skill Registry；第 37 章的 MCP Registry 分析同样适用：**版本化只是起点，供应链治理才是难点**。

## 46.10 Mini 实现走读

### 46.10.1 模块结构

```text
mini_mlflow/agentops.py
  Session                  # 会话聚合与统计
  group_sessions           # 按 tag 分组
  search_sessions          # 取 trace → 分组
  SimulationCase           # 脚本化测试用例
  ScriptedConversationSimulator
  generate_test_cases      # 会话 → 用例
  ScorerResult             # 统一评分输出
  ConversationCompleteness / UserFrustration /
  ConversationalSafety / ToolSuccessRate / LatencyBudget
  score_sessions           # 离线批评分
  ScorerSamplingConfig / OnlineScorer / score_online
  Issue / detect_issues    # 确定性分诊
  PairedComparison / compare_paired / assert_improved
  sessions_to_dataset_records
```

没有新表、没有新 REST、没有新 CLI。原因不是偷懒，而是**会话是 trace 的派生视图**：只要 trace 上已有 session tag，聚合就不需要持久化副本。反过来，一旦你要做跨天分页、会话状态机、权限隔离，就必须建独立 session 表；这是生产化的分水岭。

### 46.10.2 与真实 MLflow 的映射

| 真实能力 | Mini 对应 | 差异 |
|---|---|---|
| `search_sessions` | `search_sessions` | 扫最近 trace 后分组，无服务端分页 |
| `ConversationSimulator` | `ScriptedConversationSimulator` | 用户脚本固定，无 LLM 角色扮演 |
| `generate_test_cases` | `generate_test_cases` | 只取首个用户消息与脚本，不做语义推断 |
| 内置多轮 scorer | 五个确定性 scorer | 词表/正则，不是 LLM judge |
| `ScorerSamplingConfig` | 同名 dataclass | 仅 sample_rate + filter_string，无后台任务 |
| Issues 自动检测 | `detect_issues` | 规则签名，无 LLM 聚类与状态机 |
| `compare_evaluations` | `compare_paired` | 百分位 bootstrap，无 BCa/t 检验 |
| `assert_improved` | `assert_improved` | 语义对齐，最小 6 对 |
| Skill Registry | 无 | 第 46.9 节只做分析 |
| 会话级 assessment 落首条 trace | 同 | 幂等 upsert |

### 46.10.3 示例 19 的输出

`examples/19_agent_ops.py` 做了六件事：

1. 用三个脚本用例跑 `ScriptedConversationSimulator`，其中一条故意抛异常；
2. 用 `search_sessions` 读回三个会话；
3. 用五个 scorer 打分，写入 `CODE` assessment；
4. 人为制造两条同签名工具超时，`detect_issues` 聚成一个 `tool_failure`；
5. 用 8 对样本做 `compare_paired`，`assert_improved` 通过；
6. 把前两个会话转成 dataset records，写入 golden 数据集。

运行：

```bash
python examples/19_agent_ops.py
```

你会看到：

```text
simulated_runs: 3 条（refund/password 完成，crash 为 error）
sessions: 3 条 sim-* + orders-session
assessments: 每个会话 5 条 CODE assessment
issues: 1 条 tool_failure，count=2，severity=high
comparison: mean_diff≈0.155，CI 不跨 0，ci_gate=pass
dataset: agent-golden-*，record_count=2
```

### 46.10.4 为什么这些设计可测试

17 个测试覆盖：

- 会话排序、token/工具/错误聚合、空 tag 忽略；
- 模拟器正常/异常/截断、用例校验、从会话生成用例；
- 五个 scorer 的边界；
- assessment 的 CODE 来源与幂等；
- 在线采样率 0/1/0.5 的确定性与 filter；
- issue 签名归一化、min_count、high_latency；
- 配对比较的显著改进、回归、样本不足；
- dataset records 的形状。

这些测试不证明 scorer“准”，只证明**管道、幂等、边界和统计语义**是对的。第 21 章的原则在这里同样适用：测试通过 ≠ 生产正确。

## 46.11 生产差距与落地清单

### 46.11.1 Mini 没有做的事

- **LLM 模拟用户与目标检查器**：没有角色扮演、没有语义停止条件；
- **异步/流式评分**：批处理，不是新 trace 触发的后台任务；
- **服务端会话存储**：没有独立 session 表、分页、状态机、跨天查询；
- **跨服务会话拼接**：只认单进程写入的 tag，没有分布式上下文传播；
- **LLM judge 与校准**：没有 judge builder、人工一致性评估、位置偏差校正；
- **Issues 的语义聚类与状态机**：只有规则签名，没有 Open/Resolved/Not an issue；
- **统计严谨性**：百分位 bootstrap，不是 BCa；没有 t 检验、多重比较校正、分层抽样；
- **权限与保留期**：没有 workspace/RBAC、PII 扫描、TTL、审计；
- **成本控制**：没有预算、熔断、采样配额；
- **Skill Registry**：完全未实现。

### 46.11.2 生产落地清单

- [ ] 统一 session_id 生成与传播规则，跨服务用 trace context 传递；
- [ ] 明确“会话结束”的定义（超时/用户离开/人工接管）；
- [ ] 为每个 scorer 记录版本、模型、指令、温度、解析失败率；
- [ ] 线上评分按会话分层采样，错误会话 100%；
- [ ] 评分任务异步化，幂等键包含 session/scorer/version；
- [ ] 评测集 digest 与 baseline run id 进入发布记录；
- [ ] CI 门禁预注册主指标，设置 `min_delta` 与多重比较策略；
- [ ] Issues 分诊有 owner、SLA、回归用例与关闭证据；
- [ ] 会话数据脱敏、截断、加密、保留期与删除演练；
- [ ] Skill/MCP 资产有来源白名单、版本固定、diff 审查与撤销流程。

### 46.11.3 三个反模式

1. **“全量评分”**：每个 trace 都跑 LLM judge，成本会先于质量崩掉；先分层采样。
2. **“模拟器通过就上线”**：模拟器只覆盖你写下的目标；真实用户的长尾与对抗输入仍需线上反馈。
3. **“显著性等于重要性”**：统计显著只说明“差异不太可能是噪声”，不说明“值得发布”；业务阈值与人工评审不能省。

## 小结

- Agent 的运营粒度是 **session**，不是单条 trace；session tag 是最小、最可移植的约定。
- 模拟用户解决覆盖度与可重放性，不解决真实性；模拟器、目标检查器、scorer 都要版本化。
- 单轮 scorer 看“这一轮”，会话级 scorer 看“有没有解决”；两类要分开采样、分开解释。
- 在线评分的关键不是 scorer 多聪明，而是采样单位、幂等、隔离、预算与权限。
- Issues 把重复失败变成可分诊的证据链；规则检测是可解释的下限，LLM 聚类是增强，不是替代。
- 配对比较比“均值对比”可靠；`assert_improved` 要 fail closed，并警惕多重比较与实际显著性。
- Skill/MCP 把 Agent 的能力变成供应链；版本化只是起点，来源、diff、权限与撤销才是治理。
- Mini 用 `agentops.py`、17 个测试与示例 19 证明了会话 → 模拟 → 评分 → 分诊 → 比较 → 数据集的最小闭环；生产差距在 46.11 节逐项列出。

## 练习

1. 为你的 Agent 设计 session_id 的生成与传播规则：跨服务、跨队列、跨重试时如何保持稳定？写出三个失败场景。
2. 用 `ConversationCompleteness` 的思路，为一个“订票 Agent”写出 5 条会话级完成判据，并说明哪些必须人工标注。
3. 设计一个分层采样策略：错误会话、长会话、含工具失败、普通会话各用什么采样率？给出每日预算公式。
4. 把示例 19 的 `detect_issues` 扩展一个 `policy_violation` 检测器：输入是什么、签名如何归一化、证据保留多少？
5. 用 20 对评测结果跑 `compare_paired`，故意加入 2 个回归 case，观察 `assert_improved` 的行为；解释为什么“平均仍变好”时门禁应该或不应该通过。
6. 为 `ConversationSimulator` 写一份“模拟器偏差审计”清单：目标、人格、停止条件、成本、PII 各检查什么？
7. 比较 Prompt Registry、MCP Registry、Skill Registry 的版本/别名/权限模型，画一张三者共用的资产表。
8. 设计一个“Issues → 回归用例 → CI 门禁”的自动化流程：哪一步必须人工，哪一步可以自动，失败如何回滚？

---

返回：[教程首页](../README.md) · 上一章：[第 45 章 上线之后：模型监控、漂移检测与再训练闭环](45-monitoring-drift.md) · 下一章：[第 47 章 GenAI 综合实战](47-genai-capstone.md)

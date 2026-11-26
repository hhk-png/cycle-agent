# 第 39 章 真实 MLflow 3.x GenAI 实战：Tracing、Prompt、Evaluate 与 CI 回归

> 本章使用真实 MLflow 3.x 的 `mlflow.genai` 与 `mlflow.tracing` API，不在 MiniMLflow 上运行。写作基线为 2026-11 的 3.18；API 仍可能随 minor 调整，执行前以 `mlflow --help`、官方文档和你锁定的版本文档为准。Mini 的对应实现见第 19、28、33 章。

## 学习目标

- 用真实 MLflow 搭一条可运行的 GenAI 观测与评测链路：trace → prompt → evaluate → feedback → CI。
- 分清 `mlflow.trace`、`mlflow.start_span`、自动埋点、`mlflow.genai.evaluate`、scorer、judge 各自解决什么问题。
- 知道评估数据、期望值（expectation）、反馈（feedback）和评测运行（evaluation run）之间的关系。
- 用 `@mlflow.test` 把评测变成 pytest 回归门禁，而不是一次性 notebook。
- 用 `mlflow.trace.session`/`search_sessions` 做会话级评测，用 `optimize_prompts` 把评测反馈变成新 prompt 版本。
- 识别“能跑 demo”与“可上线评测平台”之间的差距：数据治理、judge 校准、成本、权限与版本固定。

## 39.1 安装与连接

最小安装：

```bash
python -m pip install "mlflow>=3.18,<4"
```

如果只需要客户端、不启动本地服务，可以连接团队已有的 tracking server：

```bash
export MLFLOW_TRACKING_URI=http://127.0.0.1:5000
```

本地实验可以用 SQLite 后端：

```bash
mlflow server \
  --backend-store-uri sqlite:///mlflow.db \
  --artifacts-destination ./mlartifacts \
  --host 127.0.0.1 --port 5000
```

注意 3.18 的升级约束：正式环境要求先停写、备份、执行 `mlflow db upgrade`，不支持混合版本运行。第 34、36、37 章已经给出升级顺序；本章假设你已经有一个可用的 3.x tracking server。

Python 侧先确认版本和连接：

```python
import mlflow

print(mlflow.__version__)
mlflow.set_tracking_uri("http://127.0.0.1:5000")
mlflow.set_experiment("genai-support-agent")
```

`set_experiment` 会创建实验（如果不存在）。实验是 trace、evaluation run、prompt 评测的归属边界；不要把“客服问答”和“代码生成”塞进同一个实验，否则搜索和成本归因都会混在一起。

## 39.2 Tracing：先把一次请求看清楚

### 39.2.1 最小 trace

```python
import mlflow

@mlflow.trace
def answer(question: str) -> str:
    docs = retrieve(question)
    return generate(question, docs)

@mlflow.trace(span_type="RETRIEVER")
def retrieve(question: str):
    return vector_store.search(question, k=5)

@mlflow.trace(span_type="LLM")
def generate(question: str, docs):
    prompt = build_prompt(question, docs)
    return llm.invoke(prompt)
```

`@mlflow.trace` 会给函数创建 span；嵌套调用自动形成父子关系。装饰器参数在不同版本可能略有差异，核心是 `name`、`span_type`、`inputs/outputs`。

手动 span 适合包裹不能加装饰器的代码：

```python
with mlflow.start_span(name="rerank", span_type="RERANKER") as span:
    span.set_inputs({"query": question, "docs": [d.id for d in docs]})
    ranked = reranker.rank(question, docs)
    span.set_outputs({"doc_ids": [d.id for d in ranked]})
```

一次 trace 的典型结构：

```text
trace: answer
  span: retrieve            RETRIEVER
  span: rerank              RERANKER
  span: generate            LLM
    event: token_usage      {input: 1200, output: 300}
  span: validate_json       PARSER
```

### 39.2.2 自动埋点

真实 MLflow 为常见框架提供 autolog：

```python
import mlflow

mlflow.openai.autolog()
mlflow.langchain.autolog()
# 其他框架按版本文档启用
```

自动埋点的价值是“少写代码就能看到调用链”，代价是：

- 可能记录完整 prompt/completion，包含 PII 或密钥；
- 高 QPS 下 trace 量迅速膨胀；
- 框架版本变化可能导致字段/埋点行为变化；
- 递归/重复埋点需要框架适配层处理。

生产策略：关键路径全量、其余采样；对输入输出做脱敏/截断；把 trace 存储成本和保留期纳入容量规划。

### 39.2.3 查询与导出

```python
traces = mlflow.search_traces(
    experiment_ids=["1"],
    filter_string="attributes.status = 'ERROR'",
    max_results=50,
)
# search_traces 默认返回 DataFrame；trace 对象在 "trace" 列。
for trace in traces["trace"]:
    print(trace.info.trace_id, trace.info.status)
```

如果你的版本返回的是 list，直接迭代即可；用 `print(type(traces))` 确认，不要假设跨版本一致。

单条 trace：

```python
trace = mlflow.get_trace(trace_id)
for span in trace.data.spans:
    print(span.name, span.span_type, span.status)
```

OpenTelemetry 是互操作层：MLflow 可以把 trace 导出到 OTel 兼容后端，也可以接收 OTel 数据。具体 exporter/环境变量随版本与部署变化；设计时不要把业务逻辑绑死在某个 UI 字段上，保留原始 span 类型与关键属性。

### 39.2.4 会话与多轮评测

3.10 引入了实验性的会话级评测。关键不是给 span 加一个 `session_id` 属性，而是用 **trace tag `mlflow.trace.session`** 把多条 trace 归到同一个 session（部分版本文档示例用 `metadata=`，执行前以 `help(mlflow.update_current_trace)` 为准）：

```python
@mlflow.trace
def chat_turn(question: str, session_id: str) -> str:
    mlflow.update_current_trace(tags={"mlflow.trace.session": session_id})
    return generate_response(question)
```

然后有两种读取方式：

```python
# 方式 A：按 tag 过滤
traces = mlflow.search_traces(
    filter_string="tags.`mlflow.trace.session` = 's-123'",
    return_type="list",
)

# 方式 B：按 session 分组
sessions = mlflow.search_sessions(
    locations=["<experiment_id>"],
    max_results=50,
)
for session in sessions:          # 每个元素是一组 trace
    ...
```

会话级 scorer 直接对整个 session 打分：

```python
from mlflow.genai.scorers import ConversationCompleteness, UserFrustration

result = mlflow.genai.evaluate(
    data=traces,                  # 同一 session 的 trace
    scorers=[ConversationCompleteness(), UserFrustration()],
)
```

也可以让 `ConversationSimulator` 生成多轮对话（测试用例包含目标、persona、`max_turns`），再把模拟轨迹送进同一套 scorer。要点：

- 会话级 assessment 通常挂在**该 session 的第一条 trace** 上，并带 session 标识；
- 单轮 scorer 仍可逐 turn 评估，但“是否解决”“用户是否沮丧”只有看完整对话才有意义；
- 生产 trace 是采样数据，不能直接当全量会话指标；
- 内置 scorer 可能依赖外部模型（常见是 OpenAI），成本与数据边界要提前评估。

第 37 章把 Review Queue 与会话级治理放在平台层讨论；会话 → 模拟 → 评分 → 分诊 → 统计门禁的完整闭环见第 46 章。本节只强调：先有稳定的 session tag，再谈聚合与评测。

## 39.3 Prompt Registry：把提示词当制品

### 39.3.1 文本模板

```python
import mlflow

prompt = mlflow.genai.register_prompt(
    name="support-reply",
    template="You are a support agent. Policy: {{policy}}\nQuestion: {{question}}",
    commit_message="initial",
    tags={"owner": "support", "team": "cx"},
)
print(prompt.version)
```

读取与渲染：

```python
loaded = mlflow.genai.load_prompt("prompts:/support-reply@production")
text = loaded.format(policy="Refunds within 30 days.", question="Can I return this?")
```

`format` 只做受控变量替换；不要用 `eval` 或无约束的 `str.format` 渲染用户可控模板。第 33 章给出了 Mini 的安全实现，以及 `{name.__class__}` 这类属性遍历攻击的原理。

### 39.3.2 Chat 模板

生产应用通常直接持有 messages：

```python
prompt = mlflow.genai.register_prompt(
    name="support-chat",
    template=[
        {"role": "system", "content": "You are a support agent. Policy: {{policy}}"},
        {"role": "user", "content": "{{question}}"},
    ],
    commit_message="chat template",
)
messages = mlflow.genai.load_prompt("prompts:/support-chat@production").format(
    policy="Refunds within 30 days.",
    question="Can I return this?",
)
```

chat 模板的额外契约：

- role 白名单（`system`/`user`/`assistant`/`tool`，以目标 provider 为准）；
- 消息顺序与非空校验；
- 多模态 content parts（3.12 之后）；
- `response_format` / 结构化输出配置；
- 变量可能跨多条消息出现。

如果 Registry 只能存纯文本，团队最终会在应用里手工拼 messages，版本化就名存实亡。选型时要确认你的版本支持 chat 模板。

### 39.3.3 版本与别名

```python
v1 = mlflow.genai.register_prompt("support-reply", "Hello {{name}}")
v2 = mlflow.genai.register_prompt("support-reply", "Hi {{name}}, how can I help?")
mlflow.genai.set_prompt_alias("support-reply", "production", v2.version)
```

发布流程和模型 Registry 同构：

```text
1. 离线评测固定 prompts:/name/3
2. 评测通过后把 production 指向 v3
3. 回滚把 production 指回 v2
4. 旧版本不删除，保留审计与复现
```

trace 里要记录 **prompt name + 解析后的 version**，而不是只记录 alias。alias 会移动；只记 alias 无法回答“这次请求到底用了哪一版”。

### 39.3.4 Prompt 优化（beta）

MLflow 3.5 起提供 `mlflow.genai.optimize_prompts`，把“评测反馈 → 改 prompt → 注册新版本”串成一条可审计的流程。它是 beta，API 与优化器行为可能变化。

```python
import mlflow
from mlflow.genai.optimize import GepaPromptOptimizer
from mlflow.genai.scorers import Correctness

prompt = mlflow.genai.register_prompt(
    name="support-reply",
    template="Answer the customer question: {{question}}",
)

def predict_fn(question: str) -> str:
    # 必须从 Registry 加载并 format，否则优化器改的不是实际使用的模板
    template = mlflow.genai.load_prompt("prompts:/support-reply@latest")
    return llm.invoke(template.format(question=question))

result = mlflow.genai.optimize_prompts(
    predict_fn=predict_fn,
    train_data=[
        {"inputs": {"question": "Can I return a used item?"},
         "expectations": {"expected_response": "Only if defective"}},
    ],
    prompt_uris=[prompt.uri],
    optimizer=GepaPromptOptimizer(reflection_model="openai:/gpt-5"),
    scorers=[Correctness(model="openai:/gpt-5-mini")],
)
print(result)   # 具体字段（优化后 prompt/URI/指标）以版本文档为准
```

两种常见优化器：

| 优化器 | 是否需要训练数据 | 适用 |
|---|---|---|
| `GepaPromptOptimizer` | 是 | 有明确指标、需要迭代反思的复杂任务 |
| `MetaPromptOptimizer` | 零样本可不需要；few-shot 需要 | 快速结构化改写、少量数据 |

工程注意：

- `predict_fn` 必须真的调用 `PromptVersion.format()`；否则优化器评估的是另一段模板；
- `train_data` 用 `inputs` + `expectations`，不要拿线上未标注日志直接训练；
- scorer 的模型/版本/温度要固定，否则优化结果不可比；
- 优化会自动注册新 prompt 版本并记录 run/metrics；发布仍走 alias 审批，不要直接覆盖 `production`；
- 这是“搜索更好的 prompt”，不是“保证更好”；必须用独立评测集回归，防止过拟合训练样本。

## 39.4 Evaluation：从指标到评测运行

### 39.4.1 数据结构

GenAI 评测的输入不是简单的 `X/y`，而是一组“请求 + 期望 + 上下文”：

```python
data = [
    {
        "inputs": {"question": "What is the refund window?"},
        "expectations": {"answer": "30 days"},
    },
    {
        "inputs": {"question": "Can I return a used item?"},
        "expectations": {"answer": "Only if defective; see policy 4.2"},
    },
]
```

运行评测：

```python
from mlflow.genai.scorers import Correctness, RelevanceToQuery

result = mlflow.genai.evaluate(
    data=data,
    predict_fn=agent.invoke,
    scorers=[Correctness(), RelevanceToQuery()],
)
print(result.metrics)
print(result.traces)
```

`predict_fn` 接收 `inputs`，返回输出；`evaluate` 会为每个样本创建 trace/evaluation record，并调用 scorer。不同版本的参数名可能略有差异（例如是否显式传 `experiment_id`），以 `help(mlflow.genai.evaluate)` 为准。

### 39.4.2 内置 scorer 与自定义 scorer

常见 scorer：

| scorer | 关注点 | 需要什么 |
|---|---|---|
| `Correctness` | 答案是否正确 | expectation 或 judge 标准 |
| `RelevanceToQuery` | 是否回答了用户问题 | inputs/outputs |
| `RetrievalGroundedness` | 是否被检索文档支持 | 检索上下文 |
| `Safety` | 是否含不安全内容 | 策略/分类器 |
| `Guidelines` | 是否满足自定义规则 | guidelines 文本 |
| 会话级 scorer | 整段对话是否完成/用户是否沮丧 | session 级 trace |

自定义确定性 scorer：

```python
from mlflow.genai.scorers import scorer

@scorer
def contains_citation(outputs, expectations):
    answer = str(outputs.get("answer", ""))
    return {
        "score": 1.0 if "http" in answer else 0.0,
        "rationale": "answer must contain a citation",
    }
```

自定义 scorer 可以接收 `inputs`、`outputs`、`expectations`、`trace` 等参数（取决于版本），返回值可以是标量/反馈对象/字典；具体 schema 以 `help(scorer)` 和版本文档为准。确定性 scorer 应优先于 judge：它们便宜、可复现、容易解释。只有“语义正确性”这类无法规则化的维度才交给 judge。

### 39.4.3 LLM-as-judge

```python
from mlflow.genai.judges import make_judge

judge = make_judge(
    name="policy_compliance",
    instructions=(
        "Decide whether the answer follows the refund policy. "
        "Return pass/fail with a one-sentence rationale."
    ),
    model="<your-judge-model>",
)
```

judge 的工程化要求：

- 固定 judge 模型版本、温度、prompt；
- 盲评/随机顺序，避免位置偏差；
- 多 judge/多次采样，报告一致性；
- 与人工标注校准（准确率、Cohen's kappa）；
- 记录 judge 的原始输出与解析失败；
- 把 judge 本身当作需要评测的模型，而不是 ground truth。

3.9 的 Judge Builder、3.10 的多轮评测、3.14 的 judge 优化（MemAlign/GePA 一类方法）都是在解决“judge 与人的偏差”。这些能力是否在开源版可用，取决于版本与部署；不要把实验性 API 写进关键发布路径。

### 39.4.4 统计显著性

两个评测运行的分数差 0.01，可能只是样本噪声。至少要：

- 报告样本量与置信区间；
- 同一评测集上做配对比较；
- 多 seed/多 judge 采样；
- 按切片（语言、租户、长度、工具）报告；
- 对“提升”设定最小可接受效应量。

3.18 的 release notes 提到实验性的评测运行统计比较能力；在它稳定之前，用 bootstrap/McNemar 自己算也不难，关键是不要用单点分数做发布决策。涉及具体 API 时以锁定版本文档为准。

## 39.5 Feedback、Expectation 与评测数据集

### 39.5.1 三种“判断”

| 对象 | 回答的问题 | 例子 |
|---|---|---|
| feedback | 这次输出好不好？ | `quality=0.3`、`passed=false` |
| expectation | 正确答案/必须满足什么？ | `answer="30 days"`、`must_cite=true` |
| metric | 聚合后的数值 | `correctness=0.82` |

把三者混在 tag 里会丢失类型、来源和时间语义。第 33 章已经说明为什么 Mini 用 Assessment 表；真实 MLflow 的 assessment 还包含 `valid`、`overrides`、更多 source 类型，并与 labeling session/评测数据集打通。

### 39.5.2 写回 trace

```python
from mlflow.entities.assessment import AssessmentSource, AssessmentSourceType

mlflow.log_feedback(
    trace_id=trace_id,
    name="quality",
    value=0.3,
    rationale="missed the refund policy",
    source=AssessmentSource(
        source_type=AssessmentSourceType.HUMAN,
        source_id="reviewer-7",
    ),
)

mlflow.log_expectation(
    trace_id=trace_id,
    name="answer",
    value="30 days",
    source=AssessmentSource(
        source_type=AssessmentSourceType.HUMAN,
        source_id="reviewer-7",
    ),
)
```

注意真实 API 的 `source` 是一个 `AssessmentSource` 对象，不是两个扁平参数；`source_type` 的枚举常见值为 `HUMAN`/`LLM_JUDGE`/`CODE`。具体参数名以版本文档为准；核心是：**判断要有来源、理由和类型**。没有来源的“好评”无法审计，也无法校准 judge。

### 39.5.3 从 trace 构建评测集

```python
bad = mlflow.search_traces(
    filter_string="assessments.quality < 0.5",
    max_results=200,
)
```

把低分 trace 采样、脱敏、补充 expectation，形成新的 golden set；再让新版本在固定集合上回归。受管数据集（`mlflow.genai.datasets`：`create_dataset` / `merge_records` / `search_datasets`）的数据模型、合并语义与治理见第 44 章。注意：

- 线上数据可能含 PII，进入评测集前必须脱敏/审批；
- 低分样本有选择偏差，不能代表整体分布；
- 评测集要版本化（digest/快照），否则“提升”不可复现；
- 不要把评测集直接当训练集，避免污染。

## 39.6 `@mlflow.test`：把评测变成 CI 门禁

3.14 引入 opt-in pytest 插件。先在 `conftest.py` 启用：

```python
pytest_plugins = ["mlflow.pytest.plugin"]
```

然后写回归测试：

```python
import mlflow
from mlflow.genai.scorers import Guidelines

@mlflow.test
def test_refund_answer_is_english(agent):
    result = mlflow.genai.evaluate(
        predict_fn=agent.invoke,
        data=[{"inputs": {"question": "What is the refund window?"}}],
        scorers=[
            Guidelines(
                name="is_english",
                guidelines="The answer must be written in English.",
            )
        ],
    )
    assert result.passed, result.reason
```

要点：

- `@mlflow.test` 会为测试建立 MLflow run/trace；没有启用插件时应在测试期报错，而不是静默跳过；
- 一次 pytest session 的记录可以汇总到一个 run，便于在 UI 里看回归结果；
- `result.passed` / `result.reason` 把 scorer 结果变成断言；
- 并行测试（pytest-xdist）需要把 worker 的结果归并到同一个 run，通常通过 `MLFLOW_RUN_ID` 等约定实现；
- CI 需要 judge 模型凭证时，用 secret manager 注入，不要写进仓库。

这比“每次发版手工跑 notebook”强在：**失败会阻断合并**，而且评测结果与代码 commit、prompt 版本、数据集版本绑定。

## 39.7 Review Queue、标注与人工闭环

3.14 的 Review Queue 把“人工看一眼”变成可分配、可追踪的任务：

```text
trace 采样
  → 进入队列（按模型/租户/风险分层）
  → 分配给评审人/组
  → 回答预设问题（Pass/Fail、分类、数值、文本）
  → 写入 assessment（feedback 或 expectation）
  → 进入评测集 / 校准 judge
```

设计要点：

- 队列要有状态机（`pending → in_review → completed → skipped`），并明确“一个人完成后是否清空整组任务”；
- 评审问题要版本化，否则不同批次的分数不可比；
- 多人标注同一批样本，计算一致性；
- 评审配额与优先级，避免高价值样本被淹没；
- 访问控制：评审人能看 trace，但未必能看全部生产数据（第 37 章的子资源 DENY）。

Mini 没有 Review Queue；第 37.7 节给出了 `review_queues`/`review_items` 三表的最小改造。

## 39.8 MCP Registry 与 Agent 供应链

Agent 的能力来自工具。工具从“配置文件里的一行 URL”变成“版本化资产”后，需要回答：

- 谁注册了这个 MCP server？
- production 指向哪个版本？
- 工具清单什么时候变的？变了什么？
- 这个 URL 是否指向内网/元数据服务？
- 谁能提升别名？

3.15 的 MCP Registry（实验）给出三个实体：

```text
MCPServer          逻辑名（reverse-DNS）
MCPServerVersion   语义化版本 + server.json + 工具快照
MCPAccessEndpoint  URL + transport，可绑定版本或别名
```

Python 侧入口在 `mlflow.genai`（`register_mcp_server`、`search_mcp_servers`、`set_mcp_server_alias` 等，具体名称以版本文档为准）。关键不是 API，而是治理：

1. 只允许受信来源，记录来源 URL 与内容摘要；
2. 工具清单变化产生 diff 并触发评审；
3. URL 做协议/域名/私网地址校验（与第 38 章 webhook 的 SSRF 同源问题）；
4. 生产固定版本/digest，不读 `latest`；
5. 调用侧记录 server/version/tool，便于事故回溯。

MCP server 是远程代码/服务入口，风险高于 prompt。不要因为“它只是一个工具列表”就跳过供应链审查。

## 39.9 成本、延迟与生产清单

GenAI 可观测性不能只看“有没有 trace”，还要聚合：

- input/output token、每请求成本；
- 缓存命中率、节省金额；
- p50/p95/p99 延迟、首 token 延迟；
- 检索/重排/工具调用耗时；
- 失败/重试/回退次数；
- 按租户/模型/功能/route 的成本。

上线前的最小清单：

- [ ] trace 采样率、保留期、脱敏策略已定义；
- [ ] prompt 版本与解析后的 version 写入 trace；
- [ ] 评测集版本化（数据集 ID + version + digest），包含长尾/对抗/多语言切片；
- [ ] 确定性 scorer 优先，judge 有校准与版本固定；
- [ ] CI 里有 `@mlflow.test` 回归，失败阻断合并；
- [ ] 人工反馈有来源、理由、状态机；
- [ ] MCP/工具 URL 有 SSRF 与来源审查；
- [ ] Gateway/模型调用的密钥不落配置、不进日志；
- [ ] 成本与延迟有预算/告警；
- [ ] 升级前读 release notes，执行数据库迁移与回滚演练。

## 39.10 Mini 的映射与练习

| 真实 MLflow 3.x | Mini 对应 | 差距 |
|---|---|---|
| `@mlflow.trace` / `start_span` | `mini_mlflow.tracing` | 手动 API、无自动埋点/采样 |
| `mlflow.genai.evaluate` | `evaluate.py` | 基础指标，无 scorer 注册表 |
| `search_sessions` / 会话级 scorer | trace tag 可按任意 key 存取，但无 session 聚合 | 需要按 `mlflow.trace.session` 分组与会话级评估 |
| `mlflow.genai.optimize_prompts` | 无 | 需要优化器、评测数据与自动注册 |
| `mlflow.genai` Prompt | `prompts.py` | 文本模板，无 chat/多模态 |
| feedback/expectation | `log_feedback` | 最小 assessment，无 overrides |
| `mlflow.genai.datasets` | `datasets.py`（第 44 章） | 记录/digest/schema/profile/合并；无 inputs 哈希去重与记录级更新 |
| `@mlflow.test` | 无 | 需要 pytest 插件与评测运行 |
| Review Queue | 无 | 需要队列/状态机/权限 |
| MCP Registry | 无 | 需要版本/别名/工具快照 |

建议的练习顺序：

1. 用 `@mlflow.trace` 包一个真实的 RAG 请求，记录检索文档 ID、token、延迟；对照第 19 章的 Mini span 模型。
2. 用 `mlflow.genai.register_prompt` 建文本与 chat 两个版本，跑一次 `evaluate`，把 prompt version 写进 trace tag。
3. 写一个确定性 scorer 和一个 judge，比较两者在同一批样本上的分歧，计算人工抽检一致性。
4. 用 `@mlflow.test` 把一个失败样本变成 CI 回归；故意改坏 prompt，确认测试会红。
5. 为一个 MCP server 设计“工具清单变更”的评审流程，列出必须阻断发布的条件。
6. 把第 33 章的 Mini Gateway 与真实 `mlflow.genai.evaluate` 组合：Gateway 负责路由/成本，evaluate 负责质量。
7. 用 `mlflow.trace.session` 串起一段三到五轮对话，分别用单轮 scorer 与会话级 scorer 评估，比较结论差异。
8. 用 `optimize_prompts` 优化一个 prompt，在独立测试集上比较优化前后；记录是否出现过拟合训练样本。

## 39.11 与真实 MLflow 的差距

- 本章代码是“真实 API 的使用示例”，不随教程的 pytest 运行；版本差异以 `help()` 和官方文档为准。
- 真实 MLflow 的 GenAI 能力在 3.9–3.18 间快速演进，部分 API 标记为 experimental；生产要 pin 版本并隔离实验性调用。
- 评测平台的生产化还需要：数据集治理、judge 校准、人工标注运营、权限审计、成本系统、跨区域存储。
- Mini 提供的是控制面模型，不是 `mlflow.genai` 的兼容实现；不要把 Mini 的 API 当成真实 MLflow API。

## 小结

- GenAI 的资产是 prompt、trace、评测数据、scorer 和反馈，而不是“一个模型文件”。
- Tracing 提供请求级因果；Evaluate 提供质量判断；Prompt Registry 提供版本化；Feedback/Expectation 提供可审计的判断；`@mlflow.test` 提供发布门禁。
- judge 是另一个需要评测的模型：固定版本、校准、报告一致性，不能当 ground truth。
- Review Queue/MCP Registry 把人工闭环与 Agent 工具供应链纳入平台治理。
- 会话级评测用 `mlflow.trace.session` 分组；Prompt 优化用 `optimize_prompts`，但都要在独立评测集上回归。
- 生产化的难点在数据治理、成本、权限和版本固定，而不是“多接一个 SDK”。

---

下一章：[第 40 章 LoggedModel 参考实现](40-logged-model-implementation.md)

---

返回：[教程首页](../README.md) · 上一章：[第 38 章 事件驱动与自动化治理](38-webhooks-events.md)

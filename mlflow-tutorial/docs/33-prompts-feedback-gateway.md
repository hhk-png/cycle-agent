# 第 33 章 Prompt Registry、Feedback 与 AI Gateway：把 GenAI 控制面写出来

## 学习目标

- 理解为什么 prompt、用户反馈和模型路由都应该成为**一等数据**，而不是散落在代码、日志和标签里。
- 读懂 MiniMLflow 新增的 `prompts.py`、`gateway.py` 与 assessment 存储：它们如何复用已有的版本、别名、搜索和安全边界。
- 掌握 prompt 模板渲染的安全做法，知道为什么不能用 `eval` 或无约束的 `str.format`。
- 能够设计一个可测试的 AI Gateway：路由、回退、限流、缓存、成本核算、密钥隔离。
- 看清真实 MLflow 3.x 与 Mini 实现的边界：哪些 API 已变化，哪些原则长期有效。

## 33.1 缺口：只有 Trace 还不够

第 19 章实现了 Trace/Span，第 28 章讨论了 GenAI 能力地图，但此前的 Mini 有一个诚实的缺口：**能观测，不能治理**。

一个 LLM 应用的完整控制面至少需要四类对象：

| 对象 | 回答的问题 | 只靠 Trace 行不行 |
|---|---|---|
| PromptVersion | 这次请求用的是哪一版提示词？ | 不行，Trace 只能记录文本，不能表达版本与别名 |
| Assessment | 这次回答好不好？谁判的？ | 不行，tag 是字符串，缺少类型、来源与更新语义 |
| Route/Provider | 请求应该发给哪个模型？失败怎么办？ | 不行，这是请求前的策略，不是请求后的记录 |
| Trace/Span | 实际发生了什么？慢在哪？ | 这是已有的部分 |

本章把前三类补上。它们不是“AI 专属的新数据库”，而是把已有的工程原则应用到新对象：

- **版本不可变，指针可变**：PromptVersion 像模型版本，alias 像 Registry alias；
- **判断要有来源**：Assessment 记录 `source_type`、`source_id`、`rationale`，不把“好评”写成匿名 tag；
- **策略与执行分离**：Gateway 只做路由与配额，不把业务逻辑塞进 provider 调用；
- **失败要可解释**：回退链上每一次尝试都留在响应里，而不是只返回最后一个错误。

## 33.2 Prompt 是一份制品

### 33.2.1 数据模型

Prompt 的最小可用模型：

```text
prompts(name, creation_timestamp, last_updated_timestamp, description)
prompt_versions(name, version, template, commit_message, creation_timestamp, tags_json)
prompt_aliases(name, alias, version)
```

三个不变量：

1. `(name, version)` 一旦写入不可修改；改一个字就是新版本。
2. alias 是 `(name, alias) -> version` 的可变指针；同一 alias 重新指向新版本是一行更新。
3. 模板大小有上限（Mini 为 1 MiB），变量名必须可静态提取。

Mini 的实体在 `mini_mlflow/entities.py`：

```python
@dataclass
class PromptVersion:
    name: str
    version: int
    template: str
    commit_message: str = ""
    creation_timestamp: int = ...
    tags: dict[str, str] = ...
    aliases: list[str] = ...

    @property
    def variables(self) -> list[str]:
        return _prompt_variables(self.template)
```

`variables` 按首次出现顺序返回，重复变量只算一次。这个顺序会出现在 API、UI 和模板校验里，所以它必须是稳定的。

### 33.2.2 URI 解析

用户会从文档、配置、代码三个地方引用 prompt。Mini 支持：

| 写法 | 含义 |
|---|---|
| `support-reply` | 当前最新版本（读时解析） |
| `support-reply/3` | 精确版本 3 |
| `support-reply@production` | 别名 production 当前指向的版本 |
| `prompts:/support-reply@production` | 同上，显式 scheme |

解析顺序是：显式 `version` 参数 > URI 中的 `@alias` > URI 中的数字尾段 > 最新版本。`@alias` 与显式 version 同时出现会直接报错，避免“到底以哪个为准”的隐式规则。

生产读者应该固定 alias 或 version。裸名字意味着“读时最新”，它适合探索，不适合上线：今天复现的结果，明天可能因为一次发布而变。

### 33.2.3 模板渲染：不要 `eval`，也不要裸 `format`

一个看似无害的实现是：

```python
# 错误示范
return template.format(**variables)
```

问题不在 `str.format` 本身，而在模板来自用户、变量来自请求：

- `{name.__class__}` 可以遍历对象属性；
- `{name[0]}` 可以索引；
- 格式说明符可以触发自定义 `__format__`；
- 模板里的字面量花括号会把 JSON 或代码片段弄坏。

Mini 的做法是**只认 `{{ variable }}`，用正则做纯文本替换**：

```python
_PROMPT_VARIABLE_RE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")

def format(self, **variables):
    declared = self.variables
    missing = [name for name in declared if name not in variables]
    if missing:
        raise InvalidParameter(f"missing prompt variables: {missing}")
    extra = sorted(set(variables) - set(declared))
    if extra:
        raise InvalidParameter(f"unexpected prompt variables: {extra}")

    def replace(match):
        value = variables[match.group(1)]
        if value is None:
            raise InvalidParameter(...)
        return str(value)

    return _PROMPT_VARIABLE_RE.sub(replace, self.template)
```

这带来四个性质：

- **值不会二次解释**：变量值里出现 `{{...}}` 也只是普通文本；
- **缺变量与多变量都报错**：拼写错误不会被静默忽略；
- **不支持控制流**：检测到 `{%` 或 `{#` 直接拒绝，避免“看起来像 Jinja，其实只替换变量”的误解；
- **模板大小有上限**：防止把整个知识库塞进 prompt 记录。

如果确实要交给 LangChain 等使用单花括号的模板系统，调用 `to_single_brace_format()`。它会把 `{{name}}` 变成 `{name}`，并把模板里原有的字面量花括号加倍，保证转换后的字符串仍是合法的 `str.format` 模板：

```python
prompt = registry.register_prompt(
    "json-reply",
    'Return JSON: {"name": "{{name}}", "ok": true}',
)
single = prompt.to_single_brace_format()
# 'Return JSON: {{"name": "{name}", "ok": true}}'
assert single.format(name="Ada") == 'Return JSON: {"name": "Ada", "ok": true}'
```

### 33.2.4 版本与发布

> 下面用的是 Mini 客户端 `client.*`；真实 MLflow 的入口在 `mlflow.genai` 命名空间（`register_prompt`、`load_prompt`、`set_prompt_alias`），语义相同。

```python
v1 = client.register_prompt("support-reply", "Hello {{name}}", commit_message="initial")
v2 = client.register_prompt("support-reply", "Hi {{name}}, how can I help?", commit_message="tone")
client.set_prompt_alias("support-reply", "production", v2.version)
```

发布流程和模型 Registry 同构：

1. 新版本先在实验/评测中固定 `prompts:/name/3`；
2. 评测通过后把 `production` 指向 v3；
3. 回滚只需把 `production` 指回 v2；
4. 旧版本永不删除，审计与复现都有据可查。

差异在于 Prompt 的“制品”是文本，复制成本几乎为零。这反而更容易让人随意改动，所以**提交说明、标签和 alias 的变更记录**比模型场景更重要。

Mini 的存储层保证 alias 不能指向不存在的版本（外键），服务层拒绝裸数字 alias（避免与版本号歧义）。真实 MLflow 的规则更细：`latest` 是 URI 关键字/保留名，不能作为普通 alias 设置；`v1` 这类带字母前缀的名字可以存在。不要把 Mini 的“不能纯数字”当成真实平台的完整规则。并发创建版本时，`MAX(version)+1` 与主键共同裁决；这与 Registry 的版本分配是同一套思路。

### 33.2.5 真实 MLflow：文本模板与 chat 模板

真实 MLflow 3.x 的 Prompt Registry 同时支持两种模板。文本模板的 `template` 是字符串；chat 模板的 `template` 是一个消息列表：

```python
import mlflow

prompt = mlflow.genai.register_prompt(
    name="support-reply",
    template=[
        {"role": "system", "content": "You are a support agent. Policy: {{policy}}"},
        {"role": "user", "content": "{{question}}"},
    ],
    commit_message="initial chat template",
    tags={"owner": "support"},
)

loaded = mlflow.genai.load_prompt("prompts:/support-reply@production")
messages = loaded.format(policy="refund within 30 days", question="Can I return this?")
```

两种模板的差异不只是“字符串 vs 列表”：

| 维度 | 文本模板 | chat 模板 |
|---|---|---|
| 存储 | 一个字符串 | 消息列表（role + content），可含多模态 content parts |
| 变量 | 从字符串提取 | 从所有消息的 content 提取，支持嵌套变量 |
| 渲染结果 | 字符串 | messages，可直接交给 provider SDK |
| 额外契约 | 无 | role 白名单、消息顺序、`response_format` 等 |

Mini 只实现文本模板，是因为“受限替换 + 变量校验 + 版本/别名”的安全核心与 chat 相同；chat 额外需要消息 schema 校验和多模态内容建模。若要把 Mini 扩展成 chat：

1. `prompt_versions` 增加 `template_type` 与 `template_json`；
2. 渲染函数按类型返回 `str` 或 `list[dict]`；
3. 校验 role 只能是 `system`/`user`/`assistant`/`tool`（以目标 provider 为准），拒绝空消息；
4. 测试覆盖“变量跨多条消息”“同一变量出现多次”“嵌套变量”“空模板”等边界。

为什么值得做：生产应用通常直接持有 messages；如果 Registry 只能存文本，团队会在应用里手工拼 messages，版本化就名存实亡。

真实 MLflow 3.x 的 chat 模板、`mlflow.genai.evaluate` 与 `@mlflow.test` 用法见第 39 章；本章的 Mini 实现刻意只保留文本模板，以便把“版本/别名/受控渲染”这三个核心不变量讲清楚。

## 33.3 Feedback 与 Assessment：把“好不好”变成数据

### 33.3.1 为什么 tag 不够

把用户反馈写成 `run.data.tags["feedback"] = "good"` 有五个问题：

- 没有类型：`"0.9"`、`0.9`、`"good"` 混在一起；
- 没有来源：不知道是用户、评审、模型 judge 还是自动规则；
- 没有时间语义：是第一次判断还是修正后的判断？
- 不能多值：同一 trace 上“质量”“安全性”“是否包含下一步”是三个维度；
- 不能查询：tag 是字符串，范围比较、布尔过滤都要靠字符串技巧。

Assessment 把这些拆开：

```text
assessments(
  assessment_id, trace_id, name,
  value_json, value_type,
  source_type, source_id,
  rationale, metadata_json,
  is_expectation, valid,
  create_time, last_update_time
)
```

Mini 的 `log_feedback` 接受：

```python
assessment = client.log_feedback(
    trace_id,
    "quality",
    0.9,
    source_type="HUMAN",      # HUMAN / LLM_JUDGE / CODE / UNKNOWN
    source_id="reviewer-7",
    rationale="accurate and actionable",
    metadata={"channel": "email"},
)
```

`is_expectation=True` 表示“期望答案/要求”，而不是“对本次输出的评价”。两者共用存储，但查询语义不同：

- feedback 回答“这次做得怎么样”；
- expectation 回答“正确答案/必须满足什么”，用于离线评测和标注数据集。

### 33.3.2 幂等与修正

同一 `(trace_id, name, source_type, source_id, is_expectation)` 再次写入时，Mini 更新值、理由和 `last_update_time`，保留 `assessment_id` 与 `create_time`。

这是一个刻意的取舍：

- 重试不会产生重复反馈；
- 评审可以修正自己的判断；
- 审计仍能看到“创建时间”和“最后修改时间”；
- 如果需要保留完整修改历史，生产系统应增加 append-only 的 revision 表。

### 33.3.3 用 Assessment 搜索

Trace 搜索现在支持第四种 scope：

```python
client.search_traces(
    filter_string='assessments.quality >= 0.8 and tags.prompt = "support-reply"',
    max_results=50,
)
```

解析器把 `assessments.quality` 当作字段，服务层在同名、有效的 Assessment 中取最近更新的一条的值。这样可以直接构造“人工低分队列”“安全未通过的请求”“评审已确认的 golden case”等视图。

真实 MLflow 3.x 的 assessment 还包含 `valid`、`overrides`、更多 source 类型，并与 `mlflow.genai.evaluate`、labeling session 打通。Mini 保留了最小闭环：**记录判断 -> 关联 trace -> 可搜索 -> 可用于筛选评测集**。

## 33.4 AI Gateway：产品会消失，原则不会

### 33.4.1 一段必须讲清的历史

很多教程把“MLflow AI Gateway”当成当前功能，这是一个常见的事实错误：

- MLflow 2.9（2024-01）把 AI Gateway 改名为 **MLflow Deployments for LLMs**，旧 `mlflow.gateway` 命名空间进入弃用状态；
- MLflow 3.0（2025-06）**移除了独立 deployments server 与 `start-server` 命令**，旧的 `routes`/`route_type` 配置键也被移除；
- **3.9（2026-01）起，Gateway 以“内嵌在 tracking server”的形式回归**，先加入路由/回退/passthrough/LiteLLM provider；**3.11 加预算，3.12 加 guardrail**。它不再是独立进程，但控制面问题没有消失。
- 即使不用 MLflow 的 Gateway，路由、限流、成本与密钥仍可能由 Databricks AI Gateway、云厂商网关、Kubernetes 网关或应用侧库承担。

所以本章实现 Gateway，不是为了复刻某个已删除的命令，而是因为**路由、回退、限流、成本核算、密钥隔离**这些控制面问题不会随产品名消失。你在真实平台选型时，仍然要问同样的问题。

### 33.4.2 结构

```text
Application
   │  complete(route, messages)
   ▼
Gateway ── RouteSpec(primary, fallbacks, rpm, cache_ttl)
   │
   ├── Provider A (fast/cheap)  ──失败──┐
   ├── Provider B (strong/slow) ◄───────┘
   └── Provider C (last resort)
```

Mini 的配置：

```yaml
providers:
  mock-fast:
    kind: mock
    model: mock-small
    cost_per_1k_input: 0.15
    cost_per_1k_output: 0.60
    timeout_ms: 5000
  mock-strong:
    kind: mock
    model: mock-large
    cost_per_1k_input: 3.00
    cost_per_1k_output: 15.00

routes:
  support:
    primary: mock-fast
    fallbacks: [mock-strong]
    rate_limit_rpm: 120
    cache_ttl_s: 30
```

`kind: mock` 让整个示例离线可跑；真实 SDK 通过 `CallableProvider` 注入：

```python
provider = CallableProvider(
    spec,
    lambda messages, model, **params: my_sdk.chat(messages, model=model, **params),
)
gateway = Gateway(config, providers={"openai": provider})
```

配置文件里只放 `api_key_env: OPENAI_API_KEY` 这样的**变量名**，不放密钥。Mini 的 `to_dict()`/`public_config()` 会对敏感键做 redaction；但这只是最后一道保险，正确做法是密钥根本不进入配置对象。

### 33.4.3 一次请求的完整路径

```python
response = gateway.complete(
    "support",
    [{"role": "user", "content": "Where is order A-1042?"}],
)
```

内部顺序：

1. **路由解析**：route 不存在 -> `RouteNotFound`；
2. **消息校验**：角色必须是 system/user/assistant/tool，content 必须是字符串且有大小上限；
3. **缓存查找**：key 是 `route + messages + params` 的 SHA-256，TTL 内直接返回，标记 `cache_hit=True`；
4. **限流**：每个 route 一个 token bucket，按 `rpm` 补充令牌，拿不到就 `RateLimitExceeded`（HTTP 429）；
5. **按 chain 尝试**：primary 失败则记录 attempt，继续 fallback；
6. **成本计算**：`input_tokens/1000 * cost_per_1k_input + output_tokens/1000 * cost_per_1k_output`；
7. **统计与缓存写回**：成功率、错误、token、成本、延迟、provider 调用次数。

响应里保留 attempts：

```json
{
  "provider": "mock-strong",
  "attempts": [
    {"provider": "mock-fast", "ok": false, "error": "mock provider 'fast' injected failure"},
    {"provider": "mock-strong", "ok": true}
  ],
  "usage": {"input_tokens": 12, "output_tokens": 9},
  "cost_usd": 0.000171,
  "cache_hit": false
}
```

这比只返回“最后一个错误”有用得多：排障时你知道是主 provider 超时、还是密钥失效、还是被限流。

### 33.4.4 失败语义

| 场景 | 行为 | 调用方应该做什么 |
|---|---|---|
| primary 抛 `ProviderError` | 记录 attempt，尝试 fallback | 无需处理，读 `provider` 字段即可 |
| 所有 provider 失败 | 抛 `ProviderError`，details 带 attempts | 告警；检查 provider 健康与配额 |
| 路由不存在 | `RouteNotFound` | 配置错误，不要重试 |
| 超过 rpm | `RateLimitExceeded`，带 retry_after | 退避重试或降级 |
| 缓存命中 | 返回原响应，`cache_hit=True` | 注意缓存不适合个性化/非确定性请求 |

限流是**单进程内存**的。多副本部署时，每个副本各有一份桶，实际配额是 `rpm × 副本数`。生产要用 Redis/网关级配额，或把副本数纳入预算。Mini 明确不假装这是分布式限流。

### 33.4.5 成本与观测

`gateway.stats()` 返回按 route 聚合的：

```json
{
  "support": {
    "requests": 42,
    "successes": 40,
    "errors": 2,
    "cache_hits": 11,
    "input_tokens": 12000,
    "output_tokens": 8000,
    "cost_usd": 0.0123,
    "cache_saved_usd": 0.0031,
    "latency_ms_total": 850.2,
    "latency_ms_max": 120.4,
    "providers": {"mock-fast": {"calls": 39, "cost_usd": 0.009}}
  }
}
```

这些数字可以直接接到 Prometheus：

- `gateway_requests_total{route,provider}`
- `gateway_errors_total{route,provider}`
- `gateway_cost_usd_total{route,provider}`
- `gateway_cache_saved_usd_total{route}`
- `gateway_latency_ms`（histogram）

关键是**按 route 和 provider 两个维度**打标签。只按 route 聚合，你无法判断是主 provider 退化还是 fallback 变多。

## 33.5 端到端闭环

把三块拼起来，才是一条可运营的链路：

```text
prompt v1 ──> trace（记录 prompt 版本）──> human feedback
   ▲                                           │
   │                                           ▼
prompt v2 <── 评测（同一批 golden cases）<── 低分样本 + expectation
```

一个最小操作序列：

```python
# 1. 固定 prompt 版本并记录到 trace tag
prompt = client.load_prompt("support-reply@production")
with mlflow.start_trace("support", tags={"prompt": prompt.name, "prompt_version": prompt.version}):
    answer = call_model(prompt.format(name=name, message=text))

# 2. 人工/模型判断写回 trace
client.log_feedback(trace_id, "quality", 0.3, rationale="missed refund policy")

# 3. 找出低分样本
bad = client.search_traces(filter_string="assessments.quality < 0.5")

# 4. 修改 prompt，注册 v2，先跑离线评测，再把 alias 指向 v2
client.register_prompt("support-reply", v2_template, commit_message="add refund policy")
```

注意第 1 步：**trace 必须记录 prompt name + version（或 alias + resolved version）**。只记录 alias 不够，因为 alias 会移动；只记录模板文本也不够，因为文本 diff 不可查询。

## 33.6 测试策略

新增代码最容易出现“看起来能跑，边界没测”。本章对应的测试在 `tests/test_genai_ops.py`，核心手法：

- **确定性 provider**：`MockProvider` 不联网、不随机；`fail_times` 精确注入失败次数；
- **时钟可控的限流**：token bucket 用 `time.monotonic()`，测试用 `rpm=1` 让第二次必然失败，不依赖 sleep；
- **缓存语义**：同一请求第二次必须 `cache_hit=True`，换内容必须 miss；
- **成本公式**：用响应里的 token 数反算，断言到浮点误差；
- **远端一致性**：同一组操作走本地 service 和 HTTP server 两条路径；
- **安全边界**：非法角色、非字符串 content、未知 route、负成本、未知配置键都要报错。

这类测试的价值不在于覆盖率数字，而在于把**失败路径**变成可回归的行为。Gateway 的 fallback 如果只测成功路径，等于没测。

## 33.7 与真实 MLflow 的差距

| 能力 | Mini | 真实 MLflow 3.x / 平台 |
|---|---|---|
| Prompt 模板 | 文本 `{{var}}`，拒绝控制流 | 支持文本与 chat-style message 列表；与评测/标注联动 |
| Prompt 存储 | SQLite | 数据库；Databricks 上可落到 Unity Catalog |
| Prompt 权限 | 单 token | 工作区/目录级权限、审计 |
| Assessment | feedback/expectation，标量值 | 更丰富的 source、valid/overrides、与 labeling session 集成 |
| Gateway | 教学实现：路由/回退/限流/缓存/成本 | 独立 gateway 在 3.0 移除、3.9 起内嵌 tracking server；另有平台侧网关/应用侧库 |
| 分布式限流 | 无（单进程） | 网关/Redis/服务网格 |
| 密钥管理 | 环境变量名约定 | Secret Manager/KMS/云 IAM |
| 流式响应 | 无 | 普遍支持 SSE/streaming |

这张表要表达的不是“Mini 很弱”，而是：**产品边界会变，控制面问题不变**。今天用 Databricks AI Gateway，明天换成 LiteLLM 或云厂商网关，路由、回退、配额、成本、密钥这五个问题仍然要回答。

## 小结

- Prompt 是版本化制品：版本不可变、alias 可变、渲染只做受控替换。
- Assessment 把“好不好”拆成值、来源、理由、期望/反馈和时间语义，并能参与 trace 搜索。
- AI Gateway 的核心不是某个已删除的产品，而是路由、回退、限流、缓存、成本与密钥隔离。
- 三者与 Trace 组成闭环：prompt 版本 -> 观测 -> 反馈 -> 评测 -> 新版本。
- Prompt/Registry 的变更还可以通过第 38 章的 Webhook 通知 CI 与部署系统；事件名是 `prompt_version.created`、`prompt_alias.created` 等。
- 单进程限流、进程内缓存、mock provider 都是教学取舍；生产要换成分布式配额、共享缓存和真实 provider 适配层。

## 练习

1. 给 Prompt Registry 增加“按标签搜索最新版本”，并解释为什么标签应该放在版本上而不是 name 上。
2. 设计一个 `valid=False` 的 Assessment 语义：它应该从搜索结果里消失，还是保留但标记为无效？给出你的查询规则。
3. 把 Gateway 的 token bucket 换成“每分钟 + 每天”两级配额，说明多副本部署时如何保持一致。
4. 为 Gateway 增加一个 `stream=True` 的接口草图：哪些统计字段在流式场景下必须延迟到流结束才能写入？
5. 用 `CallableProvider` 接一个真实 SDK（不要提交密钥），写出失败重试与超时映射的代码。
6. 解释为什么 `to_single_brace_format()` 要转义字面量花括号；如果不转义，一个包含 JSON 的模板会发生什么？

---

下一章：[第 34 章 MLflow 3 迁移与 Logged Model](34-mlflow3-logged-models.md)

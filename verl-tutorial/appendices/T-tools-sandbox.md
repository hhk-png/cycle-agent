# 附录 T 工具系统、函数调用与沙箱执行

多轮 Agent RL 里，工具不是"给模型加一个 API"，而是**轨迹的一部分**：工具的返回值进入上下文，工具的执行结果可能带奖励，工具调用的 token 与模型自己生成的 token 在 loss mask 上必须区分。把这三件事混在一起，是 Agent RL 最常见的工程错误。

本附录按 verl v0.9.1 的 `verl/tools/` 契约展开：schema、生命周期、注册表、函数工具、执行边界与安全。

## T.1 工具在 RL 系统里的四个位置

| 位置 | 作用 | 出错后果 |
|---|---|---|
| prompt 组装 | 把工具 schema 注入系统提示词或请求参数 | 模型不知道工具存在，或参数格式错误 |
| 轨迹记录 | 记录 tool call、observation、token 边界 | mask 错位，训练学习"如何写工具结果" |
| 奖励计算 | 工具可返回 step reward 或终局 reward | 奖励被重复计入，或信用分配错层 |
| 执行环境 | 真正运行代码/搜索/数据库 | 超时、注入、数据泄漏、集群被打爆 |

verl 把这四层拆开：`verl/tools/` 负责 schema 与实例，AgentLoop 负责轨迹，`RewardManager`/reward loop 负责奖励，sandbox 负责执行。拆开的目的是让每一层都能单独测试。

## T.2 OpenAI function schema

工具对模型暴露的接口是 OpenAI function calling 格式：

```json
{
  "type": "function",
  "function": {
    "name": "search",
    "description": "Search a corpus and return the top passages.",
    "parameters": {
      "type": "object",
      "properties": {
        "query": {"type": "string", "description": "Natural-language query."},
        "limit": {"type": "integer", "description": "Maximum passages."}
      },
      "required": ["query"]
    }
  }
}
```

`verl/tools/schemas.py` 用 pydantic 定义 `OpenAIFunctionToolSchema`、`OpenAIFunctionParametersSchema`、`ToolResponse` 等。几个容易被忽略的约束：

- `required` 可以缺省，表示没有必填参数；不要写成 `null`。
- 参数类型可以是联合，例如 `["integer", "number"]`；`enum` 不限于字符串。
- `strict: true` 要求模型输出严格符合 schema，但后端支持程度不一；开了 strict 不等于可以省掉服务端校验。
- `ToolResponse` 支持 `text`、`image`、`video` 三种内容；图像/视频必须是 list，单张也要包成 `[img]`。

schema 不是文档，是**运行时契约**。服务端仍要做参数校验，因为模型可能输出 schema 之外的字段、缺必填项、或把数字写成字符串。

## T.3 BaseTool 的生命周期

`verl/tools/base_tool.py` 的 `BaseTool` 定义五个方法：

```python
class BaseTool:
    def get_openai_tool_schema(self) -> OpenAIFunctionToolSchema: ...
    async def create(self, instance_id=None, **kwargs) -> tuple[str, ToolResponse]: ...
    async def execute(self, instance_id, parameters, **kwargs) -> tuple[ToolResponse, float, dict]: ...
    async def calc_reward(self, instance_id, **kwargs) -> float: ...
    async def release(self, instance_id, **kwargs) -> None: ...
```

生命周期是**每条轨迹一份实例**：

```text
create(instance_id) ──► execute(...) ──► execute(...) ──► calc_reward() ──► release()
```

为什么需要实例，而不是无状态函数？

- 有状态工具：浏览器会话、数据库事务、代码解释器的变量表、游戏环境；
- 隔离：并发轨迹不能共享同一个浏览器或同一个临时目录；
- 清理：release 要关闭进程、删除临时文件、归还连接；
- 奖励：`calc_reward` 可以读取实例状态（例如测试通过率），而不是只看最后一条 observation。

`create` 返回 `(instance_id, ToolResponse)`。`instance_id` 必须唯一；如果调用方传入重复 id，实现应当拒绝，而不是覆盖。MiniVerl 的 `BaseTool.create` 在 id 已存在时抛 `ValueError`，正是这个契约。

## T.4 FunctionTool：把普通函数变成工具

`verl/tools/function_tool.py` 提供 `@function_tool` 装饰器：

```python
@function_tool
def web_search(query: str, limit: int = 5) -> str:
    """Search the web and return snippets.

    Args:
        query: Natural-language query.
        limit: Maximum number of snippets.
    """
    ...
```

schema 由 `transformers.utils.get_json_schema` 从签名和 Google 风格 docstring 推断。因此函数必须：

- 每个参数有类型注解；
- docstring 有 `Args:` 段；
- 不使用 `*args` / `**kwargs`（无法表达为固定 JSON schema）。

注册时如果同名工具已存在且函数对象不同，装饰器直接抛 `ValueError`，不覆盖。这个"拒绝静默覆盖"的选择很重要：在插件式系统里，两个模块注册同名工具，静默覆盖会让线上行为随 import 顺序变化。

异步函数会被识别为 `is_async=True`，`FunctionTool.call` 用 `asyncio.to_thread` 执行同步函数，避免阻塞事件循环。

`load_function_tools_from_path` 从一个 Python 文件加载所有 `@function_tool`，返回 `list[FunctionTool]`（不是名字列表）。它用路径派生的合成模块名导入文件，避免与已有模块冲突。

## T.5 工具注册：native 与 function tool 的合并

`verl/tools/tool_registry.py` 有两类工具：

- **native tool**：继承 `BaseTool`，在 YAML 里声明 `class_name`、`config`、`tool_schema`；
- **function tool**：Python 文件里的 `@function_tool`，由 `function_tool_path` 指向。

上游没有名为 `ToolRegistry` 的类；注册入口是 `initialize_tools_from_config` 与 `load_all_tools`。MiniVerl 为了教学把它们包成了一个 `ToolRegistry`，名字是 MiniVerl 的，不是上游符号。

`load_all_tools(tool_config_path, function_tool_path)` 合并两者，并检查重名：

```python
if function_tools and native_tools:
    existing = {t.name for t in native_tools}
    collisions = sorted(t.name for t in function_tools if t.name in existing)
    if collisions:
        raise ValueError(...)
```

合并后的列表交给 AgentLoop。AgentLoop 对两类工具有不同的调用分支：`FunctionTool` 走 `tool.call(parameters)`（内部用 `asyncio.to_thread` 执行同步函数），native tool 走 `create/execute/release`。两者共享 `name` 与 `tool_schema`，但生命周期语义不同，不能只按鸭子类型一视同仁。

### 配置形状

```yaml
tools:
  - class_name: verl.tools.example_tool.ExampleTool
    config:
      type: native
      timeout: 30
    tool_schema:
      type: function
      function:
        name: example
        description: ...
        parameters:
          type: object
          properties: {}
          required: []
```

`ToolType` 目前只有 `NATIVE`；MCP 工具在 v0.9.1 被移除。这个细节说明"工具协议"仍在快速演化：不要把内部注册表当成稳定 API，跨版本升级时按附录 H 的迁移清单检查。

## T.6 AgentLoop 如何消费工具

AgentLoop 的职责不是"调用工具"，而是维护**轨迹的顺序与边界**：

```text
prompt tokens        mask=0
assistant tokens     mask=1
tool call tokens     mask=1（模型生成的）
observation tokens   mask=0（环境返回的）
assistant tokens     mask=1
...
```

关键点：

- observation 是环境产生的，不能让策略通过它获得梯度；否则模型会学习"如何编造工具结果"。
- tool call 的参数是模型生成的，应当参与训练；但参数格式错误导致的失败，是否惩罚取决于奖励设计。
- 多轮拼接时，chat template 的分隔符属于哪一段要明确；通常归入 observation 或 system，不应算作 assistant。
- 每一轮都要记录 `turn_index`，否则轮级信用分配无从下手（见第 26 章）。

`verl/experimental/agent_loop/` 里有 `SingleTurnAgentLoop`、`ToolAgentLoop`、`AgentLoopWorker`、`AgentLoopManager`。TransferQueue 版不是 `AgentLoopManagerTQ` 在 `experimental/agent_loop/`，而在 `verl/trainer/ppo/v1/agent_loop_tq.py`。单轮与多轮共享同一个 `AgentLoopOutput` 契约，这是它能被 PPO trainer 统一消费的原因。

## T.7 沙箱执行：真正的边界在进程外

`BaseTool.execute` 是 async 的，但"async"不等于"安全"：一个 `await` 里的 `subprocess.run` 仍然可以跑任意代码。生产环境需要独立的执行边界：

| 方案 | 隔离级别 | 启动成本 | 适用 |
|---|---|---|---|
| 同进程函数调用 | 无 | 纳秒 | 可信的内部工具 |
| 子进程 + 超时 | 进程级 | 毫秒 | 代码执行、脚本工具 |
| 容器 | 文件系统/网络/资源 | 秒 | 多租户代码解释器 |
| 微虚拟机（Firecracker 等） | 内核级 | 百毫秒 | 不可信代码 |
| 远程沙箱服务 | 集群级 | 网络 RTT | 弹性、集中审计 |

verl 的 `verl/utils/reward_score/sandbox_fusion/` 与文档里的 sandbox fusion 示例面向"把代码执行放到远程沙箱服务"：训练进程只发请求，沙箱服务负责隔离、限时、限内存与结果回传。

沙箱设计要回答五个问题：

1. **超时**：墙钟超时 + CPU 超时 + 输出大小上限，三者都要有。
2. **资源**：内存、进程数、文件大小、网络访问。
3. **文件系统**：只读挂载、临时目录、执行后清理。
4. **网络**：默认拒绝；需要联网的工具单独放行并审计。
5. **可观测**：每次执行的 request id、退出码、耗时、截断的输出。

只在应用层做 `asyncio.wait_for` 是不够的：协程被取消后，子进程可能仍在跑。必须把超时传到进程/容器层。

## T.8 工具奖励与信用分配

`execute` 返回的第二个值是 step reward。它可以来自：

- 工具自身的成功/失败（测试通过、搜索命中）；
- 中间检查（代码能否编译、SQL 是否可执行）；
- 过程奖励模型（PRM）对当前步骤的打分；
- 对工具调用的成本惩罚（延迟、token、API 费用）。

三个常见错误：

- **重复计入**：工具 reward 已经加进 return，终局 reward 又加一次同样的信号。
- **尺度失衡**：step reward 量级远大于终局 reward，策略会刷工具调用次数。
- **mask 错位**：把 observation token 也算进 reward 的归属，导致梯度落到环境 token 上。

建议在训练日志里分别记录 `reward/tool`、`reward/outcome`、`reward/total`，并监控工具调用次数的分布。如果调用次数随训练单调上升而任务成功率不涨，通常是 step reward 给多了。

## T.9 安全：工具是最大的攻击面

Agent 的工具调用会把模型输出变成真实动作。威胁模型至少包括：

- **prompt injection**：网页/文档里的恶意文本诱导模型调用危险工具；
- **参数注入**：把 shell 元字符、路径穿越、SQL 片段塞进参数；
- **数据外带**：工具把密钥/用户数据发到外部；
- **权限提升**：工具以训练进程的身份访问文件系统/网络；
- **资源耗尽**：无限循环、超大输出、递归调用。

工程对策：

1. 工具白名单 + 参数 schema 校验 + 服务端二次校验；
2. 最小权限：每个工具独立的服务账号/网络策略；
3. 默认拒绝网络与文件系统，按工具显式放行；
4. 输出截断与脱敏；
5. 审计日志：谁（轨迹 id）、何时、调用了什么、参数摘要、结果摘要；
6. 红队测试：把 injection 样本放进离线评测集。

安全不是"加一个 sandbox"就结束；它是工具设计、prompt 设计、权限模型与审计的组合。附录 N 给出更完整的多租户与合规清单。

## T.10 MiniVerl 的实现与验证

MiniVerl 的 `mini_verl/tools.py` 保留了同一套契约的简化版：

- `ToolSchema.from_callable` 从签名推断 JSON schema；
- `BaseTool` 实现 `create/execute/calc_reward/release` 与 per-instance state；
- `FunctionTool` 包装普通函数，异常转成 `error: <Type>`，超时转成 `timeout`；
- `ToolRegistry` 合并 native/function 工具，重名抛 `ToolCollisionError`；
- `CalculatorTool` 用 AST 白名单求值，拒绝名称、调用、属性访问。

`tests/test_tools_registry.py` 覆盖 schema 推断、装饰器两种写法、异常不逃逸、协作式超时、实例隔离、重名冲突与未知工具。`examples/23_tool_registry.py` 跑通"注册 → 执行 → 冲突 → AgentLoop 消费"的完整链路。

协作式超时要特别说明：Python 线程无法被强制杀死，`FunctionTool(timeout=...)` 只是让调用方停止等待，后台线程仍可能继续跑。这正是为什么生产沙箱要把超时放到进程/容器层。

## T.11 上线检查清单

- [ ] 每个工具有 OpenAI schema，且服务端做参数校验。
- [ ] 工具有唯一 name；合并 native/function 工具时检查冲突。
- [ ] 每条轨迹 create 一次、release 一次；异常路径也 release。
- [ ] observation token 的 mask 为 0；tool call 参数 token 的 mask 为 1。
- [ ] 工具有墙钟/CPU/输出大小三重超时。
- [ ] 不可信代码在容器或远程沙箱执行；训练进程不直接 `exec`。
- [ ] 工具奖励与终局奖励分开记录，尺度经过校准。
- [ ] 有审计日志与 injection 红队样本。
- [ ] 工具版本与 prompt 中的 schema 版本一致。

## 延伸阅读

- `verl/tools/base_tool.py`、`verl/tools/function_tool.py`、`verl/tools/tool_registry.py`、`verl/tools/schemas.py`
- `verl/experimental/agent_loop/`
- `verl/utils/reward_score/sandbox_fusion/`
- 第 26 章：多轮对话、工具调用与 AgentLoop
- 附录 N：安全、沙箱与多租户
- 附录 P：二次开发中的自定义 AgentLoop

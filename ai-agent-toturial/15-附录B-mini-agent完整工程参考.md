# 15 附录 B:mini-agent 完整工程参考

> 把第 07 章的代码走读升级成一份「可上手的工程手册」:
> 完整 API 参考、关键数据约定、验证矩阵、10 个由浅入深的练习、调试指南。

---

## 1. mini-agent 目录总览

> 先看目录再读代码——mini-agent 的每个文件都只有一个职责,与第 07 章 §2 的「9 个目录」一一对应。

```text
mini-agent/
├─ src/                         # 全部源码:零第三方运行时依赖
│  ├─ types.ts                  # 核心类型:Message / ToolCall / AgentResult / AgentRunStats
│  ├─ index.ts                  # 公共出口,统一 re-export
│  ├─ agent.ts                  # Agent 主循环:感知-思考-行动-观察(第 07 章 §3)
│  ├─ planner.ts                # Planner:启发式 + 模型驱动 + 代码块解析(第 07 章 §8)
│  ├─ provider/                 # 大模型接口层
│  │  ├─ types.ts               # ChatProvider 接口 + Completion / StreamEvent
│  │  ├─ mock.ts                # MockProvider:离线可脚本化,测试/评测的主力
│  │  └─ openai.ts              # OpenAIProvider:真实模型,兼容 Ollama / vLLM
│  ├─ tools/                    # 工具系统
│  │  ├─ types.ts               # Tool 接口 + ToolContext + ToolOutput
│  │  ├─ registry.ts            # ToolRegistry:注册 / 校验 / 调用 / 捕获异常
│  │  └─ builtin.ts             # createBuiltinTools:内置工具工厂(calculator 等)
│  ├─ memory/                   # 记忆系统
│  │  ├─ conversation.ts        # ConversationBuffer:短期滑窗
│  │  ├─ vector.ts              # VectorMemory:长期向量(n-gram 哈希嵌入,256 维)
│  │  └─ manager.ts             # MemoryManager:统一门面(短期 + 长期 + 召回拼装)
│  ├─ context/                  # 上下文管理
│  │  └─ window.ts              # ContextWindow:token 预算 + 裁剪(第 05 章)
│  ├─ multiagent/               # 多智能体
│  │  ├─ bus.ts                 # MessageBus:点对点 / 广播 / 黑板(第 08 章 §3)
│  │  └─ team.ts                # Orchestrator + RoleAgent:编排者-工作者
│  ├─ mcp/                      # MCP 协议(第 09 章)
│  │  ├─ client.ts              # StdioMcpClient:握手 / 工具发现 / 调用
│  │  └─ adaptor.ts             # mcpToolsToTools:MCP 工具 → 本地 Tool
│  ├─ telemetry/                # 可观测
│  │  └─ trace.ts               # Tracer:span 树 + createJsonlTraceWriter 落盘
│  └─ util/                     # 自研「迷你实现」,替代第三方依赖
│     ├─ tokens.ts              # token 估算(中文 1 字符≈1,其它 4 字符≈1)
│     ├─ json-schema.ts         # 迷你 JSON Schema 校验器(替代 zod / ajv)
│     └─ calc.ts                # 安全表达式求值器(拒绝 eval 与注入)
├─ tests/                       # 10 个测试文件,node --test 运行(第 07 章 §15)
│  └─ *.test.ts                 # agent / calc / context / json-schema / mcp / memory / ...
├─ examples/                    # 6 个演示 + 1 个 mock 服务器(见 §5 验证矩阵)
│  ├─ chat.ts · math-agent.ts   # 对话 / 自愈
│  ├─ team.ts · mcp-demo.ts     # 多智能体 / MCP(配 mock-mcp-server.ts)
│  └─ eval-agent.ts · eval-support.ts   # 评测 / 客服评测(第 16 章)
├─ package.json                 # 仅元数据,无 dependencies 字段
└─ tsconfig.json                # TypeScript 编译配置
```

**「零依赖」到底是什么**

「零依赖」= **零第三方运行时依赖**:运行时只用到 Node 原生能力,`npm install` 一行都不用执行。
为了不装任何包,mini-agent 自带了三样「迷你实现」:

| 常见第三方库 | mini-agent 的自研替代 | 位置 |
|---|---|---|
| zod / ajv(参数校验) | 迷你 JSON Schema 校验器 | `src/util/json-schema.ts` |
| 嵌入 API / 向量库 | n-gram 哈希嵌入 + 内建向量检索(256 维,无需网络) | `src/memory/vector.ts` |
| math.js(表达式求值) | 安全表达式求值器(拒绝 eval) | `src/util/calc.ts` |

类型检查用的 `tsc` **不是运行时依赖**——它来自全局安装的 TypeScript,
或临时 `npx -y typescript`(不写入 `package.json`)。所以:
**无需 `npm install`,直接 `npx tsc --noEmit` 即可类型检查**(命令见 §2)。

---

## 2. 环境与运行

- **要求**:Node ≥ 23.6(推荐 24);零依赖,无需 `npm install`;
- **目录**:`mini-agent/`,所有命令在其中执行。

```bash
npx tsc --noEmit                      # 类型检查
node --test "tests/*.test.ts"         # 全部测试(Windows 必须带引号)
node examples/chat.ts                 # examples/ 下共六个演示,逐个运行:chat(对话)/ math(自愈)/ team(团队)/ mcp / eval / eval-support
```

---

## 3. 完整 API 参考

### 2.1 核心类:`Agent`

```ts
new Agent({
  provider: ChatProvider,        // 必填:大模型接口
  registry?: ToolRegistry,       // 工具注册中心
  memory?: MemoryManager,        // 记忆管理器
  contextWindow?: ContextWindow, // 上下文窗口
  planner?: Planner,             // 可选规划器
  tracer?: Tracer,               // 可选追踪
  name?: string,                 // 默认 'agent'
  systemPrompt?: string,
  maxIterations?: number,        // 默认 8
  maxTokens?: number,
  temperature?: number,          // 默认 0.7
  maxRecentMessages?: number,    // 默认 10:run 时带回多少条记忆
  usePlanning?: boolean,         // 默认 false
})

agent.run(input: string | Message[], opts?: { signal?: AbortSignal; temperature?: number }): Promise<AgentResult>

// AgentResult
{ output: string; messages: Message[]; stats: AgentRunStats }
// AgentRunStats
{ iterations, llmCalls, toolCalls, toolErrors, totalTokens, startTime, endTime, stopReason }

// 事件订阅(返回解绑函数)
const off = agent.on('text', (t) => ...);
off(); // 解绑
// 事件名:'text' | 'tool_call' | 'tool_result' | 'context_trim' | 'planning' | 'stop' | 'error'
```

### 2.2 大模型接口

```ts
// 接口
interface ChatProvider {
  id: string;
  complete(messages, opts?): Promise<Completion>;
  stream?(messages, opts?): AsyncIterable<StreamEvent>;
}

// MockProvider —— 离线可脚本化
new MockProvider({
  steps?: Array<MockTurn | MockResponder>,
  responder?: MockResponder,
  defaultText?: string,
  autoMath?: boolean,
})
// MockTurn = { text?: string; toolCalls?: {name, arguments}[]; finishReason? }
// MockResponder = (messages, tools?) => MockTurn | Promise<MockTurn>
provider.pushStep(step)  // 运行中追加脚本

// OpenAIProvider —— 真实模型
new OpenAIProvider({
  apiKey?: string,          // 缺省读 env OPENAI_API_KEY
  baseURL?: string,         // 缺省 https://api.openai.com/v1;兼容 Ollama/vLLM 等
  model?: string,           // 缺省 gpt-4o-mini
  fetch?: typeof fetch,     // 可注入
})
```

### 2.3 工具系统

```ts
// Tool 接口
{ name: string; description: string; inputSchema: JsonSchema;
  execute(args, ctx: ToolContext): Promise<ToolOutput> }

// ToolContext
{ log(line): void; signal?: AbortSignal; [key: string]: unknown }  // 可注入 memory/agent 等

// ToolRegistry
registry.register(tool) / registerMany(tools) / unregister(name)
registry.list() / get(name) / has(name) / toDefinitions()
registry.call(name, args, ctx) → Promise<ToolOutput>  // 校验+执行+捕获异常

// 内置工具工厂
createBuiltinTools({ sandboxDir?, memoryManager?, webSearch?, now? }): Tool[]
// 工具:calculator / get_time / web_search(mock) / read_file / write_file / echo / remember(需 memoryManager)
// 前置条件:read_file / write_file 仅当传入 sandboxDir 时才创建(否则文件工具不可用);
// remember 仅当传入 memoryManager 时才创建。
```

### 2.4 记忆

```ts
// ConversationBuffer —— 短期滑窗
new ConversationBuffer({ maxMessages?: number })  // 默认 20
buf.add(message) / addMany(msgs) / messages() / recent(n) / clear() / size

// VectorMemory —— 长期向量(n-gram 哈希嵌入,256 维)
new VectorMemory({ dim?: number })
vm.add(text, meta?) → MemoryEntry
vm.search(query, k=5, threshold=0) → ScoredEntry[]
vm.remove(id) / clear() / size / all()

// MemoryManager —— 统一门面
new MemoryManager({ shortTerm?, longTerm?, maxRecall? })
mm.remember(message)                    // 短期
mm.saveFact(text, meta?)                // 长期
mm.recall(query, k?) → ScoredEntry[]
mm.recallAsContext(query, k?) → string  // 拼成可注入上下文
mm.recent(n) / messages() / clear() / factCount
```

### 2.5 上下文窗口

```ts
new ContextWindow({ maxTokens: number, estimator?: TokenEstimator, maxSystemTokens?: number })
window.trim(messages) → { messages, dropped, droppedTokens }
```

### 2.6 规划

```ts
new Planner({ maxSteps?: number })
planner.createPlan(goal, provider?) → Promise<PlanStep[]>
// PlanStep = { id, description, status, result? }
Planner.summarize(plan) → string   // 渲染成可执行计划文本
```

### 2.7 多智能体

```ts
// MessageBus
const bus = new MessageBus();
bus.attach(name) / send(from, kind, payload, to) / broadcast(from, kind, payload)
bus.receive(name) → BusMessage[] / history() / historyByKind(kind) / clear()

// 编排者-工作者
new Orchestrator(leader: TeamMember, workers: TeamMember[], opts?: {
  planner?: Planner; assign?: 'round-robin' | 'by-role';
})
orch.run(task) → { plan, workerOutputs, final, busHistoryCount }

// TeamMember = { name; role; handle(task, ctx): Promise<string> }
// RoleAgent:把 Agent 包装成成员  new RoleAgent(agent, role)
```

### 2.8 MCP

```ts
// 客户端
const client = new StdioMcpClient(cmd, args, { protocolVersion?, onServerLog? });
await client.connect() → McpServerInfo      // 握手
await client.listTools() → McpToolInfo[]    // 发现工具
await client.callTool(name, args) → McpCallResult  // 调用
await client.close()

// 工具适配
const tools = await mcpToolsToTools(client, { prefix? });  // MCP 工具 → 本地 Tool
```

### 2.9 追踪

```ts
const tracer = new Tracer({ onEvent?: (ev) => void, enabled?: boolean });
const span = tracer.startSpan('name', 'agent');
const child = span.child('tool:x', 'tool');
child.setAttribute('key', value).end('ok');
createJsonlTraceWriter(filePath)  // 落盘 JSONL
```

---

## 4. 关键数据约定

| 约定 | 值 | 说明 |
|---|---|---|
| Message.content | `string \| ContentPart[]` | 多模态预留;工具消息一般是 string |
| ToolCall.arguments | 对象(非字符串) | 内部统一对象;字符串解析在 provider 边界 |
| 工具结果回喂 | `role:'tool', toolCallId, name, content` | toolCallId 必须对应 assistant.toolCalls[].id |
| 工具错误 | `ToolOutput.isError: true` | 绝不抛异常穿透循环 |
| 停止原因 | end_turn / max_iterations / max_tokens / error / interrupted | 在 `AgentRunStats.stopReason` |
| token 估算 | 中文 1 字符≈1,其它 4 字符≈1 | 启发式,仅用于预算 |
| MCP 版本 | 2024-11-05(经典) | 2026-07-28 规范见第 09 章 |

---

## 5. 验证矩阵(2026-08-29 复验)

| 验证项 | 命令 | 结果 |
|---|---|---|
| 类型检查 | `npx tsc --noEmit` | ✅ 0 错误 |
| 全部测试 | `node --test "tests/*.test.ts"` | ✅ 54/54 通过 |
| 对话 demo | `node examples/chat.ts` | ✅ `(2+3)*4`→20;记住生日→检索命中;10-3→7 |
| 自愈 demo | `node examples/math-agent.ts` | ✅ 非法表达式→修正→20;统计 2 工具调用 / 1 错误 |
| 团队 demo | `node examples/team.ts` | ✅ 2 步计划;研究员/写手分派;黑板 5 条 |
| MCP demo | `node examples/mcp-demo.ts` | ✅ 握手 2024-11-05;发现 3 工具;weather_get 成功 |
| 评测 demo | `node examples/eval-agent.ts` | ✅ 4 用例全过;结果+轨迹双重断言;聚合报告 |
| 客服评测 demo(第 16 章) | `node examples/eval-support.ts` | ✅ 4 类用例全过(happy/边界/高危/转人工);安全断言拦截直接退款 |

测试分布(tests/ 目录,10 个文件):

| 文件 | 覆盖 |
|---|---|
| tokens.test.ts | token 估算(中/英/空/汇总) |
| json-schema.test.ts | 类型/required/enum/additionalProperties/嵌套 |
| calc.test.ts | 四则/小数/非法/除零/注入拒绝 |
| tools.test.ts | 注册/重复/校验/异常/未知工具 |
| memory.test.ts | 向量检索排名/阈值/删除/滑窗/system 保留/manager |
| context.test.ts | 不裁剪/裁剪保留 system/超长 system |
| planner.test.ts | 启发式/模型驱动/代码块解析/退化/summarize |
| agent.test.ts | 完整循环/错误自愈/迭代上限/事件流/记忆/多轮/中断 |
| multiagent.test.ts | 总线点对点+广播/编排者全流程/worker 失败隔离 |
| mcp.test.ts | 握手/工具发现/weather_get/echo/适配执行/未知工具 |

---

## 6. 10 个练习(从热身到实战)

> 每个练习都应在 mini-agent 上落地并用测试验证。答案思路在 §5.11。

### 练习 1(热身):看懂一次运行
跑 `node examples/math-agent.ts`,解释输出里为什么有 3 次模型调用、
2 次工具调用、1 次工具错误。说出循环的每一步对应 `agent.ts` 的哪段代码。

### 练习 2:加一个工具
在 `createBuiltinTools` 里加一个 `unit_convert` 工具
(如 `length` 单位换算),注册进 Agent,写一个测试验证参数校验与换算正确性。

### 练习 3:上下文窗口
把 `ContextWindow` 的 `maxTokens` 设成很小的值(如 40),
跑一个长对话,观察 `context_trim` 事件,验证裁剪后仍在预算内。

### 练习 4:记忆检索
向 `VectorMemory` 写入 5 条包含「生日/昵称/地址/爱好/公司」的事实,
写一个测试断言:查询「生日」返回生日相关条目且排第一。

### 练习 5:规划
用 `Planner` 把「调研竞品。分析定价。写报告。」拆成 3 步;
再用 `MockProvider`(输出带代码块的 JSON)验证模型驱动规划。

### 练习 6:事件驱动的可视化
写一个 `examples/trace-run.ts`,用 `agent.on('tool_call'/'tool_result')`
把一次运行的轨迹渲染成「思考→行动→观察」文本,输出到终端。

### 练习 7:多智能体
扩展 `examples/team.ts`:加第三个 worker(「reviewer」,职责是检查成稿),
让 leader 在汇总前先让 reviewer 评审,把评审意见并入最终答复。

### 练习 8:MCP 新工具
在 `examples/mock-mcp-server.ts` 里加一个 `calc_remote` 工具,
在 mcp-demo 里用 `mcpToolsToTools` 适配并调用它,验证跨进程工具调用。

### 练习 9:错误自愈
写一个 `MockResponder`:第一次调用 `read_file`(不存在的文件),
工具返回 isError,模型据此改调用 `write_file`,再 `read_file` 成功。
验证循环最终成功且 `toolErrors === 1`。

### 练习 10:评测脚本
扩展 `examples/eval-agent.ts`:在数据集里加 2 个用例——一个「信息不足应追问」
的边界用例(断言 Agent 没有瞎猜而是询问用户),一个「安全用例」(断言轨迹里
没有调用危险工具)。用 `MockProvider` 固定行为,断言「成功率 100% +
平均迭代轮数 ≤ 3」,并输出每任务的 `llmCalls / toolCalls / token`。

### 5.11 练习思路(不剧透全部)

- **练习 2**:`unit_convert` schema 用 `enum: ['m','cm','mm']` + `additionalProperties: false`;
- **练习 3**:`trim` 返回的 `droppedTokens > 0` 即说明生效;观察事件即可;
- **练习 4**:写入后用 `search('生日', 1, 0)` 断言第一个命中包含「生日」;
- **练习 6**:订阅事件按顺序打印,`tool_call` 与 `tool_result` 用各自载荷里的 `call.id` 配对
  (注意:`tool_call` 载荷是 `{ call, index }`,`tool_result` 载荷是 `{ call, output }`,没有 `index` 字段,
  但两者共享同一个 `call.id`);
- **练习 7**:`makeReviewer()` 返回 `RoleAgent`,workers 数组加进第 3 个成员;
- **练习 9**:responder 里先发 `read_file({path:'nope.txt'})`,看到 isError 后发
  `write_file` 再发 `read_file`,注意用 `lastTool.name` 分支;
- **练习 10**:在 `eval-agent.ts` 的 `cases` 数组里追加用例;断言辅助函数
  `toolCalled / toolNotCalled / outputContains` 已在文件顶部定义好,直接复用。

---

## 7. 调试指南

### 6.1 常见问题速查

| 症状 | 可能原因 | 排查 |
|---|---|---|
| 循环不结束 | 模型一直要调工具 | 检查 responder 是否总会发 tool_calls;`maxIterations` |
| 工具没被调用 | 模型不知道有这个工具 | 检查 `registry.toDefinitions()` 是否含它;描述是否清晰 |
| 参数校验失败 | schema 与模型输出不匹配 | 用 `validate(args, schema)` 单独测;打印校验错误 |
| 工具结果没生效 | tool_call_id 没对上 | 检查回喂消息的 `toolCallId` |
| 记忆检索空 | 没写入 / 阈值太高 | 调 `search(q, k, 0)` 看原始得分 |
| 上下文裁剪过度 | maxTokens 太小 | 观察 `context_trim` 事件;调大预算 |
| MCP 连不上 | 子进程路径/args 错 | 看 `onServerLog` 输出;手动跑 server 看 stderr |
| 测试不定时 | MockProvider 步骤没配对 | 数清楚每次 `complete()` 消耗的 step |

### 6.2 调试工具

- **事件日志**:`agent.on('tool_call'/'tool_result', console.log)` 看决策轨迹;
- **Tracer**:`new Tracer({ onEvent: (ev) => console.log(ev) })` 看 span 树;
- **Mock 确定性**:responder 里 `console.error` 打印 messages 长度与 lastTool;
- **单文件测试**:`node --test tests/agent.test.ts` 只跑一个文件。

### 6.3 心态

Agent 调试的最大陷阱是「黑盒猜测」。mini-agent 给了三件套帮你消灭黑盒:
**消息历史**(`result.messages`)可回放、**事件流**可订阅、**span**可测量。
先看「数据」再改「代码」,不要猜。

---

## 8. 与教程章节的映射

| 你想深入 | 章节 |
|---|---|
| 循环与 ReAct | 02、07 §9 |
| 工具设计与校验 | 03、07 §5 |
| 记忆与向量 | 04、07 §6 |
| 多智能体 | 08、07 §10 |
| MCP | 09、07 §11 |
| 评测(把测试扩展成评测集) | 10 |
| 安全(把校验扩展成护栏) | 11 |
| 生产(把单进程扩展成服务) | 12 |
| 反模式诊断(系统行为怪异时查表) | 19 |

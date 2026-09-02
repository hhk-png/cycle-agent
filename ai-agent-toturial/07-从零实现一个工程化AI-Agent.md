# 07 从零实现一个工程化 AI Agent

> 本章目标:逐行走读 mini-agent 的完整实现——为什么这样分层、每个文件解决什么问题、
> 关键代码的取舍。读完本章,你应该能把「循环 / 工具 / 记忆 / 规划 / 多智能体 / MCP」
> 这些概念与真实代码一一对应,并理解「工程化」到底意味着什么。

---

## 1. 为什么从零写一个 Agent

你可能会问:第 06 章讲了那么多框架,直接拿来用不就行了?为什么还要从零写?

因为教学的目的不是「用框架」,而是「**看清框架**」。当循环、工具注册、记忆管理
都由你亲手写出,再去看 LangGraph 的 `StateGraph`、CrewAI 的 `Crew`、OpenAI 的 `Runner`,
你看到的不再是黑盒,而是「同一种思想的另一种表达」。

同时,「从零写」本身是**一种工程能力的训练**:你会被迫思考
接口怎么分、错误怎么处理、依赖怎么注入、可观测性怎么做——
这些能力在任何框架里都用得上。

**mini-agent 的三个硬约束**:

1. **零 npm 依赖**:不依赖任何第三方库,全部功能(JSON Schema 校验、向量嵌入、
   表达式求值、JSON-RPC 客户端)自己实现。可移植性最高,任何机器都能跑。
2. **Node 原生运行 TypeScript**:Node ≥ 23.6 内置类型剥离(type stripping),
   不需要构建步骤,`.ts` 直接运行。代价是必须遵守 `erasableSyntaxOnly` 限制
   (不能用 enum、namespace、构造器参数属性等需要编译的语法)。
3. **离线可验证**:内置 `MockProvider`,不联网、无 API Key 也能跑通全部测试与演示。

---

## 2. 工程结构总览

```
mini-agent/
├── package.json / tsconfig.json     # 零依赖;type: module;strict
├── src/
│   ├── types.ts                     # ★ 统一契约(一切的地基)
│   ├── agent.ts                     # ★ Agent 主循环
│   ├── provider/                    # 大模型抽象
│   │   ├── types.ts                 #   ChatProvider 接口
│   │   ├── mock.ts                  #   可脚本化离线模型
│   │   └── openai.ts                #   OpenAI 兼容适配器
│   ├── tools/                       # 工具系统
│   │   ├── types.ts                 #   Tool 接口 + ToolContext
│   │   ├── registry.ts              #   注册/校验/执行
│   │   └── builtin.ts               #   内置工具集
│   ├── memory/                      # 记忆
│   │   ├── conversation.ts          #   短期滑窗
│   │   ├── vector.ts                #   长期向量
│   │   └── manager.ts               #   统一门面
│   ├── context/window.ts            # 上下文预算裁剪
│   ├── planner.ts                   # 任务规划
│   ├── multiagent/                  # 多智能体
│   │   ├── bus.ts                   #   消息总线(黑板)
│   │   └── team.ts                  #   编排者-工作者
│   ├── mcp/                         # MCP 协议
│   │   ├── client.ts                #   stdio JSON-RPC 客户端
│   │   └── adaptor.ts               #   工具适配
│   ├── telemetry/trace.ts           # 链路追踪
│   ├── index.ts                     # 公共导出桶(对外稳定 API)
│   └── util/                        # calc / json-schema / tokens
├── examples/                        # 6 个演示(对话/自愈/团队/MCP/评测/客服评测)+ mock MCP 服务器
└── tests/                           # 10 个测试文件,54 个用例
```

**分层原则**:依赖方向从下往上,上层依赖下层接口,下层不依赖上层。
`agent.ts` 依赖 provider / tools / memory / context / planner / telemetry,
但这些模块互相独立、可以单独测试。

---

## 3. 统一契约:`src/types.ts`

工程化的第一件事是**定义数据契约**。如果每个模块自己定义 Message,
就会出现「A 的消息和 B 的消息格式不一样,互相转换出错」的经典问题。

```ts
export type Role = 'system' | 'user' | 'assistant' | 'tool';

export interface ToolCall {
  id: string;               // 唯一 ID,用于调用与结果配对
  name: string;
  arguments: Record<string, unknown>;
}

export interface Message {
  role: Role;
  content: string | ContentPart[];   // 文本或多模态
  name?: string;                    // tool 消息的归属工具名
  toolCallId?: string;              // tool 消息关联的调用 ID
  toolCalls?: ToolCall[];           // assistant 消息的调用请求
  timestamp?: number;
  meta?: Record<string, unknown>;
}
```

**关键设计决策**:

- `Message` 是**所有模块共享的中间格式**。无论背后是 OpenAI、Anthropic 还是 mock,
  在 Agent 内部一律用这个格式;格式转换只发生在 provider 边界。这就是
  第 03 章强调的「不要绑定任何一家格式」的落实。
- `ToolCall.arguments` 用对象而非字符串,因为**入参校验**(第 03 章)需要结构化。
  各家 API 的字符串 arguments 在 provider 边界解析成对象。
- `toolCallId` 是配对契约:工具结果必须挂到正确的调用上。
- 用 `import type` + `verbatimModuleSyntax`,类型不产生运行时开销。

> 为什么把 `ToolDefinition` 和 `JsonSchema` 也放在 types.ts?
> 因为 provider 需要它们(模型侧的工具描述),放 tools/ 会造成循环依赖。

---

## 4. 大模型抽象:`src/provider/`

### 4.1 接口 `ChatProvider`

```ts
export interface ChatProvider {
  readonly id: string;
  complete(messages: Message[], opts?: CompleteOptions): Promise<Completion>;
  stream?(messages: Message[], opts?: CompleteOptions): AsyncIterable<StreamEvent>;
}
```

Agent 只依赖这个接口。换模型 = 换 provider,Agent 代码零改动。

### 4.2 离线模型 `MockProvider`

`MockProvider` 是本实现能「离线可验证」的关键。它模拟一个大模型的输出,
通过两种方式控制:

```ts
export interface MockProviderOptions {
  steps?: Array<MockTurn | MockResponder>;  // 顺序脚本:第 N 次调用消耗第 N 个
  responder?: MockResponder;                 // 完全自定义:拿到全部消息,返回下一回合
  defaultText?: string;                      // 脚本/响应器用尽后的兜底
  autoMath?: boolean;                        // 自动把「数学表达式」交给 calculator
}
```

```ts
const provider = new MockProvider({
  steps: [
    // 第一次调用:输出「思考文本 + 请求调用 calculator」
    { text: '让我计算', toolCalls: [{ name: 'calculator', arguments: { expression: '2+3' } }] },
    // 第二次调用:从消息里找到工具结果,给出最终答案
    (messages) => {
      const lastTool = [...messages].reverse().find((m) => m.role === 'tool');
      return { text: `结果是 ${lastTool?.content}` };
    },
  ],
});
```

**为什么这个设计好?**

1. **确定性**:测试可以精确断言「第 N 次模型调用返回什么」;
2. **可观察**:测试/演示时,你能从响应里看出 Agent 是否正确地把工具结果回喂;
3. **模拟真实**:它走的是与真实模型完全相同的消息往返路径,
   所以「用 mock 验证过的 Agent 逻辑」换成真实 provider 依然成立;
4. **模拟智能**:`autoMath` 让 mock 能「从自然语言里识别数学意图」,
   配合 `chat.ts` 演示,看起来像一个真会推理的小模型。

`MockProvider` 同时实现了 `stream()`(流式),供流式演示使用。

### 4.3 真实模型 `OpenAIProvider`

`OpenAIProvider` 用 Node 内置 `fetch` 实现 OpenAI 兼容的 chat completions:

- 把 `Message[]` 转成 OpenAI 格式(处理 `tool_calls` / `tool_call_id`);
- 把 `ToolDefinition[]` 转成 `tools` 参数;
- 非流式与流式(SSE)两条路径;
- 流式时**增量拼接**工具参数,结束时 `JSON.parse`;

因为只依赖 HTTP,任何 OpenAI 兼容端点都能接:
OpenAI 官方、Ollama、vLLM、DeepSeek、Moonshot、通义等。

```ts
const provider = new OpenAIProvider({
  baseURL: process.env.OPENAI_BASE_URL,   // 默认 https://api.openai.com/v1
  model: process.env.AGENT_MODEL ?? 'gpt-4o-mini',
});
```

> **一个小澄清**:`OpenAIProvider` 自身只会从环境变量读取 `OPENAI_API_KEY` 与
> `OPENAI_BASE_URL`;**不会**读取 `AGENT_MODEL`。上面示例里的 `process.env.AGENT_MODEL`
> 是「调用方自己读环境变量再作为 `opts.model` 传进去」——想省这步,直接把
> 模型名写进构造参数即可。
>
> 一个诚实的说明:OpenAIProvider 不在离线验证范围内(需要 API Key/本地服务)。
> 但它是通往真实世界「唯一的替换点」,且逻辑已被 mock 路径完整验证。

---

## 5. 工具系统:`src/tools/`

### 5.1 接口 `Tool` 与 `ToolContext`

```ts
export interface Tool {
  name: string;
  description: string;
  inputSchema: JsonSchema;
  execute(args: Record<string, unknown>, ctx: ToolContext): Promise<ToolOutput>;
}

export interface ToolContext {
  log(line: string): void;       // 结构化日志
  signal?: AbortSignal;          // 取消信号
  [key: string]: unknown;        // 扩展点:memory / db / config 按需注入
}
```

**依赖注入**:工具需要的外部资源(记忆、文件系统、数据库)通过 `ToolContext`
注入,而不是在工具内部 `new`。这保证了工具是纯逻辑、可单测、可复用。
mini-agent 把 `memoryManager`、`agent` 注入 ctx,`remember` 工具就是这样拿到记忆的。

### 5.2 注册中心 `ToolRegistry`

`ToolRegistry` 干三件事:注册、校验、执行。

```ts
export class ToolRegistry {
  register(tool: Tool): this;          // 重复注册直接抛错(配置期防呆)
  list(): Tool[];                      // 按名字排序,保证确定性
  toDefinitions(): ToolDefinition[];   // 序列化成模型工具清单
  async call(name, args, ctx): Promise<ToolOutput>;  // 校验 + 执行 + 捕获异常
}
```

**核心方法 `call()` 的防御式设计**:

```ts
async call(name, args, ctx): Promise<ToolOutput> {
  // 1. 未知工具
  if (!tool) return { content: `未知工具:${name}`, isError: true };
  // 2. JSON Schema 校验
  const schemaErrors = validate(args, tool.inputSchema);
  if (schemaErrors.length > 0) {
    return { content: `参数校验失败:\n${schemaErrors.join('\n')}`, isError: true };
  }
  // 3. 执行,捕获一切异常 → 转成 isError
  try {
    return await tool.execute(args, ctx);
  } catch (err) {
    return { content: `工具执行异常:${msg}`, isError: true };
  }
}
```

**为什么绝不让异常穿透循环?**
因为 Agent 循环的设计哲学是「**让模型看到错误并自我修正**」。
如果工具抛异常把循环打断,模型就没机会修正了。所有失败都变成可读文本回喂。

### 5.3 内置工具与安全表达式

`src/tools/builtin.ts` 用**工厂函数 + 依赖注入**组装内置工具:

```ts
export function createBuiltinTools(opts: BuiltinToolOptions): Tool[] {
  // calculator / get_time / web_search(mock) / read_file / write_file / echo / remember
}
```

其中 `calculator` 是「安全」的典范。它不用 `eval` / `Function`(那等于把模型变成
RCE 通道),而是自己写了一个**递归下降解析器**(src/util/calc.ts):

```ts
// 只接受数字、+ - * / %、括号、空白
const r = safeEvaluate('(2+3)*4');   // { ok: true, value: 20 }
const bad = safeEvaluate('process.exit()');  // { ok: false, error: "非法的字符 'p'(位置 0)" }
```

`read_file` / `write_file` 还做了**路径穿越防护**:
解析后的路径必须落在沙箱目录内,否则拒绝执行。

---

## 6. 记忆系统:`src/memory/`

### 6.1 短期 `ConversationBuffer`

```ts
const buf = new ConversationBuffer({ maxMessages: 20 });
buf.add(msg);          // 追加,超限自动从最旧丢弃
buf.messages();        // 返回拷贝(system 永远保留,不占配额)
```

实现:内部 `items` 数组,超过上限时把 system 抽出来,其余按 FIFO 裁掉最旧。

### 6.2 长期 `VectorMemory`

零依赖的向量记忆。核心是「n-gram 哈希嵌入」:

```ts
// 把文本切成:字、相邻两字、英文单词 → 哈希到 256 维向量(符号取哈希位)
private embed(text: string): number[] { ... }

add(text, meta);                    // 写入
search(query, k, threshold);        // 余弦相似度 top-k + 阈值过滤
```

教学上它是真实向量库的「替身」:检索-排序-截断的骨架完全一致,
换真实 embedding 模型 + 向量库,Agent 代码零改动。

### 6.3 统一门面 `MemoryManager`

```ts
const memory = new MemoryManager();
memory.remember({ role: 'user', content: '…' });   // 每轮:进短期
memory.saveFact('用户生日是…');                    // 沉淀:进长期
memory.recallAsContext('生日');                    // 查询:语义检索拼上下文
```

Agent 只跟 `MemoryManager` 打交道,不关心背后是环形缓冲还是向量库(门面模式)。

---

## 7. 上下文窗口:`src/context/window.ts`

`ContextWindow` 在**每轮模型调用前**把消息裁剪到 token 预算内:

```ts
const { messages, dropped, droppedTokens } = window.trim(allMessages);
```

算法:
1. 预算够,直接返回;
2. 预算不够,先截断过长的 system;
3. 再从旧到新裁剪非 system 消息,直到预算内。

这是第 04、05 章「上下文治理」里最基础的一层——保证每次调用都不超窗。
第 05 章上下文工程讲透了「预算怎么分、消息怎么排、缓存怎么用、工具子集怎么选」,
`ContextWindow` 只是那个体系里「最后一道保险」的执行器。

---

## 8. 规划器:`src/planner.ts`

```ts
const planner = new Planner();
const plan = await planner.createPlan(goal, provider);  // provider 可选
```

- 有 provider:让模型输出 JSON 步骤数组,`extractJsonArray` 容忍代码块包裹,解析失败退化;
- 无 provider:启发式按中文/英文标点切句,每句一步。

计划在 agent 里被注入 user 消息,让循环按计划执行。注意这个注入有前提:
**仅当构造 Agent 时传入了 `planner`、且 `usePlanning: true`、且计划多于一步**
(`plan.length > 1`)时才会发生(`src/agent.ts` §run)。默认配置**不会**自动规划。
**重规划**留给编排层(第 08 章),避免把单循环复杂化。

---

## 9. 心脏:`src/agent.ts` 主循环

### 9.1 组装消息

```ts
const messages: Message[] = [];
if (this.systemPrompt) messages.push({ role: 'system', content: this.systemPrompt });
for (const m of this.memory.recent(this.maxRecentMessages)) {  // 记忆注入
  if (m.role !== 'system') messages.push(m);
}
messages.push({ role: 'user', content: input });
```

> **对照源码的说明**:上面这段简化代码对应的是 `buildInitialMessages()`
> (src/agent.ts,仅供 `runStream()` 使用)。`run()` 主循环的真实组装
> (src/agent.ts §run)在此基础上还有两个分支:当 `input` 是 `Message[]` 时
> 直接整体追加(多轮延续);当 `usePlanning` 开启且计划多于一步时,
> 会额外注入一条「请严格按计划执行」的 user 消息(见 §8)。结构相同,
> 细节更全。

### 9.2 循环主体

```ts
for (let iter = 0; iter < this.maxIterations; iter++) {
  stats.iterations = iter + 1;
  this.throwIfAborted(opts.signal);                  // 1. 取消检查

  const trimmed = this.contextWindow.trim(messages);  // 2. 上下文裁剪
  if (trimmed.dropped > 0) this.emit('context_trim', ...);

  // 3. 思考:调用大模型
  const completion = await this.provider.complete(trimmed.messages, {
    temperature, maxTokens, tools, signal,
  });

  // 4. 记录 assistant 回合
  messages.push({ role: 'assistant', content: completion.content, toolCalls: ... });
  if (completion.content) { finalOutput = completion.content; this.emit('text', completion.content); }

  // 5. 停止条件:模型不再调用工具
  if (completion.toolCalls.length === 0) {
    stats.stopReason = completion.finishReason === 'length' ? 'max_tokens' : 'end_turn';
    lastTurnHadToolCalls = false;
    break;
  }

  // 6. 行动:并行执行全部工具调用
  const results = await Promise.all(completion.toolCalls.map(async (call, index) => {
    this.emit('tool_call', { call, index });
    const toolSpan = span.child(`tool:${call.name}`, 'tool');
    const ctx: ToolContext = { log: ..., signal, memoryManager: this.memory, agent: this };
    const output = await this.registry.call(call.name, call.arguments, ctx);
    toolSpan.setAttribute('isError', ...).end(...);
    stats.toolCalls++;
    return { call, output };
  }));

  // 7. 观察:把结果回喂
  for (const { call, output } of results) {
    messages.push({ role: 'tool', toolCallId: call.id, name: call.name, content: output.content });
    this.emit('tool_result', { call, output });
  }
}

// 8. 迭代上限兜底
if (lastTurnHadToolCalls && stats.iterations >= this.maxIterations) {
  stats.stopReason = 'max_iterations';
}
```

### 9.3 错误与中断

```ts
} catch (err) {
  if (err.name === 'AbortError') stats.stopReason = 'interrupted';
  else { stats.stopReason = 'error'; this.emit('error', { error }); }
}
```

`AbortSignal` 从 `run(input, { signal })` 传入,用户/超时中断不会留下半吊子状态。

### 9.4 沉淀记忆

```ts
if (typeof input === 'string') this.memory.remember({ role: 'user', content: input });
const lastAssistant = [...messages].reverse().find((m) => m.role === 'assistant');
if (lastAssistant?.content) this.memory.remember({ role: 'assistant', content: lastAssistant.content });
```

### 9.5 事件驱动

```ts
agent.on('tool_call', (p) => ...);
agent.on('tool_result', (p) => ...);
agent.on('text', (t) => ...);
agent.on('stop', (r) => ...);
```

**为什么事件驱动?** UI、日志、追踪、测试都只订阅事件,不侵入循环。
循环是纯逻辑,关注点分离。

### 9.6 流式变体 `runStream()`:打字机效果怎么来

生产 Agent 的第一个用户体验要求通常是「第一个字要快」——这靠流式。
`Agent.runStream()`(src/agent.ts)是 `run()` 的流式变体:

```ts
for await (const ev of agent.runStream('请计算 (2+3)*4')) {
  if (ev.type === 'text_delta') process.stdout.write(ev.delta); // 打字机
  if (ev.type === 'tool_call_delta') { /* 工具参数增量 */ }
  if (ev.type === 'done') { /* 收尾,含最终 Completion */ }
}
```

**工程要点**:

1. **只有「文本生成」阶段流式,工具回环仍是一次性补全**——这是刻意的简化:
   流式的价值在「首 token 延迟」与「打字机体验」,工具调用的往返本身不追求逐字节;
2. **依赖 provider 的 `stream()`**:`MockProvider` 和 `OpenAIProvider` 都实现了
   `stream()`(SSE 解析 + 工具参数增量累积,见第 03 章 §2);provider 不支持流式时
   `runStream()` 自动退化为完整 `run()` 再一次性吐出;
3. **事件流与流式共存**:`runStream()` 内部仍走 `run()` 的事件(工具调用/结果),
   流式只负责文本增量的体验层。

---

## 10. 多智能体:`src/multiagent/`

### 10.1 消息总线 `MessageBus`

「邮箱 + 黑板」模型:

```ts
const bus = new MessageBus();
bus.attach('researcher');                 // 注册成员邮箱
bus.send('leader', 'task', payload, 'researcher');  // 点对点
bus.broadcast('leader', 'plan', plan);    // 广播(进所有邮箱 + 历史)
bus.history();                            // 黑板:全部消息
```

### 10.2 编排者-工作者 `Orchestrator`

```ts
const orch = new Orchestrator(leader, [researcher, writer], { assign: 'round-robin' });
const result = await orch.run(task);
// result: { plan, workerOutputs, final, busHistoryCount }
```

流程(与第 02 章 §5.4 对应):
1. leader 用 planner 拆解任务;
2. 每步分派一个 worker(round-robin 或按角色);
3. **worker 失败不拖垮团队**:catch 后标记 `failed` 继续;
4. leader 汇总所有 worker 结果,产出最终答复。

`RoleAgent` 把一个 Agent 包装成团队成员的适配器(依赖注入的又一例)。

---

## 11. MCP:`src/mcp/`

### 11.1 客户端 `StdioMcpClient`

实现了经典 MCP(2024-11-05)的 stdio 传输 + JSON-RPC 2.0:

- `connect()`:`initialize` 握手 → 读服务器信息 → 发 `notifications/initialized`;
- `listTools()`:`tools/list` 发现工具;
- `callTool(name, args)`:`tools/call` 执行;
- 内部:`pending` Map 记录待决请求,stdout 按行解析 JSON-RPC 响应,id 关联。

```ts
const client = new StdioMcpClient(process.execPath, ['examples/mock-mcp-server.ts']);
await client.connect();
const tools = await client.listTools();          // [{name:'weather_get',...}]
const res = await client.callTool('weather_get', { city: '上海' });
```

### 11.2 工具适配器 `mcpToolsToTools`

```ts
const localTools = await mcpToolsToTools(client);  // MCP 工具 → 本地 Tool
registry.registerMany(localTools);                 // 注册进 Agent
```

适配后,Agent 调用 MCP 工具与调用内置工具**完全无感**。这是「协议适配」的样板:
新增一个 MCP 服务器 = 启动 client + 跑一遍适配器,Agent 零改动。

### 11.3 mock MCP 服务器

`examples/mock-mcp-server.ts` 是一个**真实的子进程**,通过 stdin/stdout 与
mini-agent 通信。它证明了一件事:**Agent 可以通过协议与另一个程序对话**。

---

## 12. 可观测性:`src/telemetry/trace.ts`

极简 Span 树 + 回调输出:

```ts
const tracer = new Tracer({ onEvent: (ev) => console.log(ev) });
const span = tracer.startSpan('agent.run', 'agent');
const toolSpan = span.child('tool:calculator', 'tool');
toolSpan.setAttribute('isError', false).end('ok');
```

- 结构与 OpenTelemetry 对齐(span / kind / duration / status);
- 根 span = 一次 run,子 span = 每次 llm / tool 调用;
- 生产环境把 `onEvent` 换成上报到 LangSmith / OTel / 自建。

**把 trace 落盘**:`createJsonlTraceWriter(filePath)` 把事件逐行追加成 JSONL 文件
(串行队列保证不并发写坏):

```ts
import { Tracer, createJsonlTraceWriter } from '../src/telemetry/trace.ts';
const sink = createJsonlTraceWriter('./traces/agent.jsonl');
const tracer = new Tracer({ onEvent: sink.onEvent });
// 跑完任务后:await sink.flush() 确保全部落盘
```

- 每行一个 span 事件(`traceId` / `spanId` / `parentSpanId` 串成因果树);
- 复盘「为什么 Agent 做了这个决定」= 打开 JSONL 按 traceId 重放;
- 与第 12 章「可观测三支柱」的 Tracing 对应——mini-agent 用 JSONL 代替 OTel
  上报,语义一致。

## 13. 六个演示如何覆盖核心能力

| 演示 | 覆盖的核心能力 |
|---|---|
| `examples/chat.ts` | 主循环 + 工具调用(calculator/remember)+ 长期记忆检索 + 多轮上下文 |
| `examples/math-agent.ts` | 工具错误自愈(模型看到错误→修正) |
| `examples/team.ts` | 规划 + 编排者-工作者 + 消息总线(黑板) |
| `examples/mcp-demo.ts` | MCP 握手 + 工具发现 + 跨进程调用 |
| `examples/eval-agent.ts` | 评测 harness:数据集 + 结果/轨迹断言 + 聚合报告(第 10 章) |
| `examples/eval-support.ts` | 客服 Agent 评测(第 16 章):happy/边界/高危/转人工 4 类用例 + 安全断言 |

每个演示输出都附「统计信息」(模型调用次数 / 工具调用次数 / 迭代轮数 / 停止原因),
让你直观感受 Agent 一次运行内部发生了什么。

---

## 14. 验证矩阵(本章的「可运行」承诺)

在 `mini-agent/` 目录下:

| 验证项 | 命令 | 期望 |
|---|---|---|
| 类型检查 | `npx tsc --noEmit` | 0 错误 |
| 全部测试 | `node --test "tests/*.test.ts"` | 54/54 通过 |
| 对话 demo | `node examples/chat.ts` | `(2+3)*4=20`,生日记忆检索命中 |
| 自愈 demo | `node examples/math-agent.ts` | 先失败后成功,统计含 1 次工具错误 |
| 团队 demo | `node examples/team.ts` | 2 步计划、研究员/写手分派、黑板 5 条 |
| MCP demo | `node examples/mcp-demo.ts` | 握手→发现 3 工具→weather_get 成功 |
| 评测 demo | `node examples/eval-agent.ts` | 4 用例全过,结果+轨迹断言,聚合报告 |

> **测试覆盖的 54 个用例**分布在 10 个文件:
> tokens / json-schema / calc / tools / memory / context / planner / agent / multiagent / mcp。
> 它们验证的不是「代码能跑」,而是「**行为正确**」——每个断言都对应一条设计约束。

---

## 15. 从 mini-agent 到生产

mini-agent 刻意做了简化,替换点就是升级点:

| mini-agent 简化 | 生产升级 |
|---|---|
| MockProvider | 真实模型 + Retry/Timeout 包装 |
| n-gram 哈希嵌入 | 真实 embedding 模型 + 向量数据库 |
| 迷你 JSON Schema 校验 | ajv 等完整实现 |
| 内存 MessageBus | 消息队列(Kafka/RabbitMQ)/ Agent 间 HTTP |
| stdio MCP 客户端 | MCP SDK + HTTP 传输 + OAuth |
| console trace | LangSmith / OpenTelemetry |
| 单进程 | 多副本 + 队列 + 状态存储 |

**关键洞察**:mini-agent 的分层(接口 + 门面 + 依赖注入)让这些升级
都发生在**边界**,而不是改核心循环。这就是「工程化」的回报。

### 15.1 工程化的六个实践(从代码里读出来的)

mini-agent 的每一行代码背后都对应一条工程实践。把它们提炼出来,
比代码本身更有迁移价值——你可以在任何 Agent 项目里复用:

| 实践 | 代码落点 | 解决的问题 |
|---|---|---|
| **先定契约,再写实现** | `types.ts` 定义 Message/ToolCall/ToolOutput | 模块间不互相「猜」数据格式 |
| **面向接口,依赖注入** | `ChatProvider` / `ToolContext` / `MemoryManager` | 换实现不改业务逻辑 |
| **防御式编程** | `ToolRegistry.call()` 捕获一切异常转 isError | 错误不穿透循环,交给模型自愈 |
| **安全边界前移** | `calc.ts` 拒绝 eval、`resolveInSandbox` 防路径穿越 | 把不可信输入挡在工具边界外 |
| **可观测内建** | 事件流 + Span 树 + `AgentRunStats` | 运行过程可见、可回放、可评测 |
| **行为验证而非冒烟测试** | 54 个测试对应 54 条设计约束 | 测试证明「行为正确」而非「能跑」 |

> **核心洞察**:这六条没有一条是 Agent 特有的——它们是**通用软件工程**。
> Agent 工程的第一课不是「学会新框架」,而是「把已知的工程纪律做到位」。

---

## 16. 本节要点

1. 从零实现是为了「看清框架」与训练工程能力;
2. **统一契约**先于一切:`types.ts` 定义所有模块共享的 Message/ToolCall;
3. **Provider 抽象**隔离模型差异,Agent 只依赖接口;
4. **工具系统**三件套:注册 / JSON Schema 校验 / 错误回喂(绝不抛异常穿透循环);
5. **记忆**三件套:短期滑窗 + 长期向量 + 统一门面;
6. **主循环**九个环节:组装消息 → 裁剪 → 思考 → 记录 → 判断停止 → 并行行动 → 观察 → 记忆 → 统计;
7. **事件驱动 + Span 追踪**保证可观测;
8. 多智能体 = 总线(通信)+ 编排者(控制)+ 角色适配(注入);
9. MCP = 协议化工具接入,适配器让 Agent 无感;
10. 验证矩阵是「可运行」的证明,54 个用例对应 54 条设计约束;
11. **工程化的六条实践**(契约先行 / 面向接口 / 防御式 / 安全前移 / 可观测 /
    行为验证)没有一条是 Agent 特有的——把通用工程纪律做到位,
    Agent 项目自然工程化。

> 工程手册(完整 API 参考、数据约定、10 个练习、调试指南)见第 15 章附录 B。

下一章,把视野从单个 Agent 拉远——多智能体协作。

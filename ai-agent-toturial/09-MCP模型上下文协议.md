# 09 MCP 模型上下文协议

> 本章目标:讲透 MCP(Model Context Protocol)是什么、解决什么问题、协议怎么工作、
> 客户端与服务端如何实现、有哪些传输方式、安全边界在哪;
> 最后通过 mini-agent 的 MCP 客户端把协议变成可运行代码。

---

## 1. 背景:为什么需要 MCP

在 MCP 出现之前,让 Agent 接入一个外部系统(数据库、GitHub、日历)是一件
**体力活**:写一个自定义 API 适配器,处理鉴权、格式转换、错误处理。
N 个系统就要写 N 个适配器,且每家模型/框架的接入方式还不同。

2024 年 11 月,Anthropic 发布 MCP(Model Context Protocol,模型上下文协议),
想解决的问题非常直白:

> 让「模型应用」连接「外部工具/数据」的过程,从「N×M 的自定义集成」
> 变成「N+M 的标准接入」。

```
没有 MCP:     应用 × 工具 = N × M 个集成
有 MCP:       应用 × (MCP 客户端) ↔ (MCP 服务器) × 工具
              连接方式 = 标准协议,一次实现、处处复用
```

MCP 由 Anthropic 于 2025 年 12 月捐赠给 Linux 基金会旗下新成立的
**Agentic AI Foundation(AAIF)**,2025–2026 年快速成为
**Agent 工具接入的事实标准**:MCP 服务器的月下载量在 2026 年达到数千万级,
主要模型厂商(Anthropic、OpenAI、Google)与几乎所有 Agent 框架都原生支持。

---

## 2. MCP 的三个角色

MCP 定义了三方角色:

```
┌─────────────┐   协议     ┌─────────────┐   调用     ┌─────────────┐
│   Host      │──────────► │   Client    │──────────► │   Server    │
│ (LLM 应用)  │            │ (连接器)     │            │ (工具提供方) │
└─────────────┘            └─────────────┘            └─────────────┘
   Claude / Agent           应用内嵌的 MCP 客户端          数据库 / GitHub / …
```

| 角色 | 职责 |
|---|---|
| **Host** | 用户面对的 LLM 应用(如 Claude、你的 Agent 产品)。发起连接。 |
| **Client** | Host 内部与 Server 通信的组件。一个 Host 可连多个 Server。 |
| **Server** | 提供能力的一方,通过标准接口暴露工具(tools)、资源(resources)、提示(prompts)。 |

**核心抽象**(MCP 向 LLM 暴露的三类「东西」):

| 抽象 | 是什么 | 用途 |
|---|---|---|
| **工具(Tools)** | 可执行的操作(查询、写入) | Agent 的行动能力(最常用) |
| **资源(Resources)** | 只读的数据(文件、文档、schema) | 给 Agent 提供上下文 |
| **提示(Prompts)** | 预定义的提示模板 | 复用专业流程 |

### 2.1 动态能力变更通知:工具热更新的基础设施

新手容易把 MCP 的能力清单当成**一次性快照**:连上服务器、`tools/list` 拉一份清单、
之后就不变了。真实生产不是这样——MCP 服务器的能力是**动态**的,运行中可能:

- **新增工具**:插件市场安装新插件后,服务器暴露新能力;
- **移除工具**:权限变更、插件下线,某工具不可再用;
- **更新工具**:工具改名、入参 schema 变化、行为升级。

如果客户端只在握手后拉一次清单,就会用到**已失效的工具**——调用报错、参数对不上,
Agent 反复失败而不知原因。MCP 用**服务端→客户端通知**解决「能力变了」的主动告知:

| 通知方法 | 含义 |
|---|---|
| `notifications/tools/list_changed` | 工具清单变了,请重新拉取 |
| `notifications/resources/list_changed` | 资源清单变了,请重新拉取 |
| `notifications/prompts/list_changed` | 提示清单变了,请重新拉取 |

客户端收到通知后的标准行为是「**重发现**」:

1. 重新调 `tools/list` / `resources/list` / `prompts/list` 拉取最新清单;
2. 更新本地缓存,把「旧工具」替换成「新工具」;
3. 必要时刷新 UI / 提示 Agent「当前可用工具集合已变化」。

> 类比:浏览器拿到的网页会过期(HTTP 缓存失效后要重新请求),MCP 的
> `list_changed` 就是「能力缓存失效」的信号——服务器**主动通知**而不是让
> 客户端轮询,省一次请求,也省掉「轮询窗口内用到过期能力」的竞态。

**与 mini-agent 的对应**:mini-agent 的 `StdioMcpClient` 是**一次性**
`listTools()`(第 07 章 §11),握手后拉一次就完事——教学合理、生产不够。
生产升级点很清晰:**订阅 `notifications/tools/list_changed`,收到后重新
`tools/list` 并刷新 `ToolRegistry`**。这把第 07 章的「发现→注册」流程从静态
变成动态——工具热更新,正是多 Agent 系统里「插件化能力」的底层基础设施。

---

## 3. 传输与消息格式

### 3.1 传输(Transports)

MCP 支持两种传输(教学简化;历史上还有一种 **HTTP+SSE** 传输,已在 2026-07-28 版
正式废弃,不建议新实现):

| 传输 | 描述 | 适用 |
|---|---|---|
| **stdio** | 客户端以子进程方式启动服务器,stdin/stdout 传 JSON-RPC | 本地、单机、零网络配置 |
| **HTTP(Streamable HTTP)** | 基于 HTTP 的请求-响应 + SSE 流 | 远程、服务化、跨机器 |

> 2026-07-28 起,HTTP 传输把协议信息放进 HTTP 头(`Mcp-Protocol-Version`、
> `Mcp-Method`、`Mcp-Name`),而不是都挤在 JSON-RPC body 里——这是
> 「无状态核心」迁移的一部分(见 §4.1)。

**stdio 的直观理解**:MCP 服务器是一个「自带 JSON-RPC 接口的命令行程序」。
客户端 `spawn` 它,往里写 JSON 行,从输出读 JSON 行。
mini-agent 的 `StdioMcpClient` 正是这个模式。

### 3.2 JSON-RPC 2.0

MCP 消息格式是 JSON-RPC 2.0,三种消息:

```jsonc
// 请求(request):带 id,必须响应
{ "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": { "name": "weather_get", "arguments": {"city":"上海"} } }

// 响应(response):带 id,对应某个请求
{ "jsonrpc": "2.0", "id": 1, "result": { "content": [{"type":"text","text":"上海:多云,27°C"}] } }

// 通知(notification):无 id,不需要响应
{ "jsonrpc": "2.0", "method": "notifications/initialized" }
```

### 3.3 核心方法

| 方法 | 方向 | 作用 |
|---|---|---|
| `initialize` | C→S | 握手:协商协议版本与能力 |
| `notifications/initialized` | C→S | 通知服务器握手完成 |
| `tools/list` | C→S | 发现可用工具 |
| `tools/call` | C→S | 调用工具 |
| `resources/list` / `resources/read` | C→S | 发现/读取资源 |
| `prompts/list` / `prompts/get` | C→S | 发现/获取提示模板 |

### 3.4 反向请求:协议不是单向的

新手最容易忽略的一点:MCP **不是只有客户端→服务器**。经典协议里服务器也能
反向请求客户端(Server → Client),最常用的两个:

| 反向方法 | 方向 | 作用 |
|---|---|---|
| `roots/list` | S→C | 服务器问「客户端,我的根路径是什么?」——拿到文件系统根,才能做本地工具 |
| `sampling/createMessage` | S→C | 服务器在推理时请求客户端调用模型补上下文(服务器侧 Agent 场景) |
| `completion/complete` | S→C | 请求客户端补全参数(如按已有资源名自动补全 prompt 参数) |
| `elicitation` | S→C | 服务器主动向用户索取缺失的信息(2026 演进方向) |

**工程启示**:
1. **MCP 是「双向对等的 JSON-RPC 通道」**,不是「客户端单向调用服务器的函数库」;
   理解这一点,才能理解为什么「MCP 服务器也可以成为宿主」;
2. **采样(sampling)是「服务器侧模型」的入口**:它允许服务器在必要时用自己的逻辑
   调用「宿主提供的模型」,而宿主可以施加审核与配额——这是安全与成本治理的挂载点;
3. 2026-07-28 的 Multi-Round-Trip Requests(MRTR)在部分场景取代了这些反向能力,
   读官方规范时留意两者的替代关系。

### 3.5 错误码与诊断:JSON-RPC 之外的可观测性

MCP 继承 JSON-RPC 2.0 的标准错误码。排查问题时**先看 `code`,再读 `message`**:

| code | 名称 | 含义 | 常见原因 |
|---|---|---|---|
| `-32700` | Parse error | 解析错误 | 消息不是合法 JSON |
| `-32600` | Invalid Request | 无效请求 | 消息不是合法 JSON-RPC 请求 |
| `-32601` | Method not found | 方法不存在 | 调用了服务器没实现的方法 |
| `-32602` | Invalid params | 无效参数 | 参数类型/个数与 schema 不符 |
| `-32603` | Internal error | 内部错误 | 服务器执行时抛异常 |

**MCP 扩展错误码**:`-32000` 及以后是自定义/应用层错误的约定区间。规范把这一段
留给服务器定义**语义化错误**(如「工具不存在」「权限不足」「配额超限」)。
排查经验:若 `code` 落在 `-32601` 附近,先怀疑「客户端与服务端版本不一致」;
若落在 `-32000` 段,去读服务器文档里的自定义错误码表。

**日志与遥测:看得见失败,才修得了失败**。JSON-RPC 的 error 响应只告诉你
「这一次调用失败了」;但「为什么失败、是否普遍、持续多久」需要另一条通道——
MCP 的 `notifications/message` 通知可以把服务器的**日志/错误**推给客户端:

| 手段 | 回答的问题 |
|---|---|
| JSON-RPC error 响应 | 这一次调用失败的直接原因 |
| `notifications/message` | 服务器运行日志 / 内部错误细节(开发与运维视角) |
| 客户端侧 metrics | 失败率、耗时分布、哪些工具最常出错 |

> 对生产 Agent 的意义:**error 是单点事实,telemetry 才是趋势**。单个
> `tools/call` 失败可以重试,但「某工具 30% 调用超时」只有聚合统计才能发现。
> 这就是第 12 章 §3 可观测性支柱(日志 / trace / metrics)在协议层的入口——
> 把每次调用打点,用 trace 串联「Agent 决策 → 工具调用 → 失败根因」,
> 才能回答「我的 Agent 到底卡在哪」。

**一次「调用失败 → 排查路径」的建议**:

```
① tools/call 失败(先看 error.code)
   ├─ -32602 无效参数   → 检查客户端生成参数与服务器 schema 是否一致
   ├─ -32601 方法不存在 → 检查协议版本 / 能力是否变化(见 §2.1 list_changed)
   ├─ -32000 段         → 查服务器自定义错误码表(权限?配额?)
   └─ -32603 内部错误   → ②
② 查 notifications/message:服务器有没有打印堆栈与上下文?
③ 查客户端 metrics:偶发还是趋势?该工具失败率是否爬升?
④ 查 trace:这次调用在整条 Agent 决策链路里的位置,是否被上游输入污染?
```

---

## 4. 握手流程(2024-11-05 版)

经典 MCP 的连接流程:

```
Client                               Server
  │  initialize {protocolVersion,      │
  │    capabilities, clientInfo}       │
  │─────────────────────────────────►  │
  │                                    │
  │ ◄───────────────────────────────── │  返回 {protocolVersion,
  │                                    │   capabilities, serverInfo}
  │  notifications/initialized         │
  │─────────────────────────────────►  │
  │  tools/list                        │
  │─────────────────────────────────►  │
  │ ◄───────────────────────────────── │  返回工具清单
  │  tools/call {...}                  │
  │─────────────────────────────────►  │
  │ ◄───────────────────────────────── │  返回工具结果
```

**工程要点**:

1. **能力协商**:双方声明自己支持什么(工具/资源/提示/采样),不支持的不强求;
2. **通知是异步的**:`initialized` 不等待响应;
3. **工具结果结构**:`content` 数组,每项有 `type`(text/image/resource/…),
   支持结构化与多模态。

> **2026-07-28 规范的重要变化**:MCP 的第五版规范(2026 年中)把核心改为
> **无状态(stateless)**:去掉常驻会话,每个请求自带协议版本与能力;
> 引入 `server/discover`;支持 Multi-Round-Trip Requests(多轮交互);
> 强化 OAuth 2.0/OIDC 授权;扩展机制(Tasks、MCP Apps、Skills over MCP)。
> 本教程的 mini-agent 实现经典版(2024-11-05),因为它覆盖面最广;
> 读官方规范时注意版本差异。

### 4.1 版本演进:从「有状态」到「无状态核心」

MCP 规范在 2024–2026 年间迭代了多个版本,中间版本各自引入了关键能力。
只看首末两端会误以为只变了两次:

| 版本 | 关键变化 |
|---|---|
| **2024-11-05**(经典版) | 常驻会话 + initialize 握手;tools/resources/prompts;stdio/Streamable HTTP |
| **2025-03-26** | 修订鉴权流程,引入 OAuth 2.0 授权框架雏形 |
| **2025-06-18** | 传输层细节修订;增加服务端生命周期管理 |
| **2025-11-25** | 强化授权与发现;OAuth 细化;扩展分类梳理 |
| **2026-07-28**(当前) | **无状态核心**:去常驻会话与握手、每请求自描述;`server/discover`;MRTR;OAuth/OIDC 强化;Tasks 移入扩展 |

> **强提醒**:表中「2026-07-28」及各 2026 演进细节(无状态核心、MRTR、
> `server/discover`、OAuth/OIDC 强化、扩展机制等)以**官方 changelog 为准**——
> 协议仍在快速迭代,具体字段、方法名与废弃时间可能随版本变化。
> 本书写作时点:2026-08。

> **对实现者的建议**:教学用经典版(握手清晰、覆盖面广),生产新项目看
> 2026-07-28 版——但很多生态工具仍以经典版为主,做兼容时按目标端版本对齐。

---

## 5. 服务端设计:一个 MCP 服务器长什么样

从服务端视角,MCP 服务器 = 「处理 JSON-RPC 的程序」。mini-agent 的
`examples/mock-mcp-server.ts` 用一百多行实现了完整的最小服务端(含注释与导入):

```ts
function handleRequest(msg) {
  switch (msg.method) {
    case 'initialize':     // 握手
      send({ jsonrpc: '2.0', id, result: { protocolVersion, capabilities, serverInfo } });
      break;
    case 'notifications/initialized':
      break;               // 通知,无需响应
    case 'tools/list':     // 暴露工具
      send({ jsonrpc: '2.0', id, result: { tools: [weather_get, echo, read_note] } });
      break;
    case 'tools/call':     // 执行工具
      send({ jsonrpc: '2.0', id, result: await runTool(params.name, params.arguments) });
      break;
  }
}
```

**生产级服务端要考虑的**:
- 鉴权(remote 传输必须有,stdio 靠本地信任);
- 工具执行的沙箱与权限控制;
- 长耗时工具的进度通知与取消;
- 工具描述的维护(与内部 schema 单一来源);
- 可观测(每个 tools/call 的耗时、成功、token 用量)。

### 5.1 从 mock 到生产:MCP 服务器的工程升级路径

mini-agent 的 `mock-mcp-server.ts` 是教学版。把它升级成生产级服务器,
有一条清晰的路径(也适用于任何「先跑通、再加固」的接入):

| 阶段 | 要做什么 | 对应本章/第 11 章 |
|---|---|---|
| **① 协议正确** | 握手、tools/list、tools/call 全部按规范实现 | §4 握手流程 |
| **② 鉴权** | stdio 靠本地信任;HTTP 传输加 OAuth 2.0/OIDC | §7 安全边界 |
| **③ 权限控制** | 工具执行前校验「调用者身份 + 能调哪个工具」 | 第 11 章 §3.4 |
| **④ 沙箱** | 有副作用的工具(写文件/执行命令)隔离执行 | 第 11 章 §3.3 |
| **⑤ 进度与取消** | 长耗时工具发进度通知,支持取消请求 | §3.3 方法表 |
| **⑥ 可观测** | 每个 tools/call 记耗时/成功/错误,trace 串联 | 第 12 章 §3 |
| **⑦ 版本与兼容** | 声明支持的 protocolVersion,平滑升级 | §4 能力协商 |

> **核心心态**:MCP 服务器是「暴露在协议边界上的程序」,它和任何网络服务
> 一样需要安全加固——**协议的标准化降低的是集成成本,不是安全成本**。

---

## 6. 客户端设计:mini-agent 的 `StdioMcpClient`

mini-agent 的客户端(src/mcp/client.ts)是实现标准写法的教学版:

```ts
export class StdioMcpClient {
  private pending = new Map<number, PendingRequest>();  // id → resolve/reject
  private seq = 0;
  private buffer = '';

  async connect(): Promise<McpServerInfo> {
    // spawn 子进程,stdin/stdout 管道
    // 发 initialize,等响应,发 initialized
  }

  private handleLine(line: string): void {
    const msg = JSON.parse(line);
    if (msg.id !== undefined) {          // 这是响应
      const p = this.pending.get(msg.id);
      p.resolve(msg.result);             // 按 id 找回请求
    }
  }
}
```

**核心机制——待决请求表**:
- 发出请求时,把 `{ id, resolve, reject }` 存进 `pending`;
- 收到响应时,按 `id` 找到对应请求并 resolve;
- 这样客户端可以**并发**多个请求,互不阻塞。
- 服务器进程退出时,reject 所有 pending,避免悬挂的 Promise。

**适配器** `mcpToolsToTools` 把 MCP 工具变成本地 `Tool`:

```ts
const tools = await mcpToolsToTools(client);   // MCP 工具 → 本地 Tool
registry.registerMany(tools);                  // Agent 无感使用
```

第 07 章已经演示:Agent 通过 MCP 调用 `weather_get`,和调用内置 `calculator`
完全一样——**这就是协议化的价值**。

---

## 7. 安全边界

MCP 是一个「连接协议」,**它本身不提供安全**。安全是接入方的责任:

1. **工具即代码执行**:MCP 工具代表「可以执行动作」,必须像对待任意代码一样对待。
   Host 必须获得用户同意才调用工具(规范明确要求)。
2. **提示注入通道**:工具返回的内容来自外部系统,可能是恶意数据。
   Agent 要把工具输出当作**不可信输入**(第 11 章)。
3. **权限最小化**:
   - 给 MCP 服务器独立的系统账号/沙箱;
   - 只授予完成任务所需的最小权限;
   - 敏感工具(删除、转账)要求人工确认。
4. **授权(2026 规范)**:remote 服务器接入企业身份体系(OAuth 2.0/OIDC),
   支持 RFC 9728 授权服务器发现与 RFC 9207 iss 校验。
5. **审计**:记录每次工具调用(谁、何时、调了什么、结果如何)。

---

## 8. MCP 生态与趋势(2026)

- **官方 SDK**:TypeScript、Python(官方参考实现);Java、Kotlin、C#、Go 等为社区维护;
- **服务器数量**:Anthropic 目录 950+ 个连接器,涵盖 GitHub、Slack、Postgres、Notion、浏览器;
- **治理**:Linux 基金会下,MCP + A2A 双协议并立;
- **扩展**:Tasks(异步长任务)、MCP Apps(内联交互 UI)、Skills over MCP;
- **框架支持**:Claude Agent SDK(最深)、CrewAI(原生)、LangGraph、OpenAI SDK 等全部支持;
- **发展方向**:无状态核心(serverless 友好)、统一授权、跨模型互操作。

> **趋势判断**:MCP 已经不只是「连工具」——它正在成为「LLM 应用的设备驱动层」。
> 类比 USB:设备无需知道你的电脑是什么牌子,插上就能用。
> MCP 让工具「插上就能用」,这是 2024–2026 年对 Agent 工程影响最深的基础设施之一。

---

## 9. 对照 mini-agent

| 概念 | 代码 | 演示 |
|---|---|---|
| stdio 传输 + JSON-RPC | `src/mcp/client.ts` | `node examples/mcp-demo.ts` |
| 握手/能力协商 | `client.connect()` | 输出协议版本 2024-11-05 |
| 工具发现 | `client.listTools()` | 发现 weather_get/echo/read_note |
| 工具调用 | `client.callTool()` | weather_get(上海)→ 多云,27°C |
| 工具适配 | `src/mcp/adaptor.ts` | Agent 无感调用 MCP 工具 |
| mock 服务端 | `examples/mock-mcp-server.ts` | 独立子进程,证明跨进程通信 |

---

## 10. 本节要点

1. MCP 解决「Agent 连工具」的标准化问题,**一次实现、处处复用**;
2. 三个角色:Host(应用)/ Client(连接器)/ Server(工具提供方);
3. 三类能力:工具(行动)/ 资源(上下文)/ 提示(流程);
4. 传输:stdio(本地子进程)+ HTTP(远程服务);消息是 JSON-RPC 2.0;
5. 客户端核心:待决请求表 + 按 id 关联响应,支持并发;
6. 服务端 = 处理 initialize/tools/list/tools/call 的 JSON-RPC 程序;
7. 从 mock 到生产有七步升级路径:协议正确→鉴权→权限→沙箱→进度→可观测→版本兼容;
8. **MCP 不提供安全,安全是接入方的责任**——工具即代码执行;
9. 2026 规范走向无状态核心 + 统一授权;MCP 正成为 LLM 应用的「设备驱动层」;
10. 与 A2A 的分工:MCP 连工具,A2A 连 Agent。

下一章,回答一个最难的问题:怎么知道你的 Agent 到底行不行——评测。

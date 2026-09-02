# mini-agent —— 零依赖的工程化 AI Agent

《AI Agent 完全教程》第 07 章的配套代码。一个用 TypeScript 编写的、**零 npm 依赖**、
可离线运行验证的简版 AI Agent,覆盖 Agent 的全部核心组件:

| 组件 | 文件 | 说明 |
|---|---|---|
| 主循环 | `src/agent.ts` | 感知→思考→行动→观察(ReAct 风格),事件驱动 |
| 大模型接口 | `src/provider/` | `ChatProvider` 抽象 + `MockProvider`(离线)+ `OpenAIProvider`(真实) |
| 工具系统 | `src/tools/` | 注册中心、JSON Schema 校验、内置工具、安全表达式求值 |
| 记忆 | `src/memory/` | 短期滑窗 + 长期向量记忆(n-gram 哈希嵌入) |
| 上下文 | `src/context/window.ts` | token 预算裁剪 |
| 规划 | `src/planner.ts` | 任务拆解(模型驱动 / 启发式) |
| 多智能体 | `src/multiagent/` | 消息总线(黑板)+ 编排者-工作者团队 |
| MCP | `src/mcp/` | stdio JSON-RPC 客户端 + 工具适配器 |
| 观测 | `src/telemetry/trace.ts` | Span 树 + JSONL 追踪 |

## 环境要求

- **Node ≥ 23.6**(推荐 24),原生运行 TypeScript(类型剥离),**无需构建、无需安装依赖**。

## 快速开始

```bash
# 运行全部测试(54 个用例)
node --test "tests/*.test.ts"

# 六个演示
node examples/chat.ts        # 多轮对话:工具调用 + 长期记忆 + 上下文
node examples/math-agent.ts  # 工具错误自愈(模型看到错误后修正)
node examples/team.ts        # 多智能体协作(编排者-工作者,消息总线黑板)
node examples/mcp-demo.ts    # 通过 MCP 协议调用外部子进程的工具
node examples/eval-agent.ts  # 评测 harness:数据集 + 运行器 + 断言 + 报告
node examples/eval-support.ts # 客服 Agent 评测(第 16 章附录 C):4 类用例 + 安全断言
```

## 验证矩阵(2026-08-29 复验)

| 验证项 | 命令 | 结果 |
|---|---|---|
| 类型检查 | `npx tsc --noEmit` | ✅ 0 错误 |
| 单元/集成测试 | `node --test "tests/*.test.ts"` | ✅ 54/54 通过 |
| 对话 demo | `node examples/chat.ts` | ✅ 计算 `(2+3)*4`→20、记住生日→长期记忆检索命中 |
| 错误自愈 demo | `node examples/math-agent.ts` | ✅ 非法表达式→修正→`(2+3)*4`=20,统计 1 次工具错误 |
| 多智能体 demo | `node examples/team.ts` | ✅ 2 步计划→研究员/写手分派→最终汇总,黑板 5 条消息 |
| MCP demo | `node examples/mcp-demo.ts` | ✅ 握手→`tools/list`→`weather_get` 跨进程调用成功 |
| 评测 demo | `node examples/eval-agent.ts` | ✅ 4 用例全过;结果+轨迹双重断言;聚合报告 |

## 目录结构

```
mini-agent/
├── package.json / tsconfig.json     # 零依赖;Node 原生跑 TS
├── src/
│   ├── agent.ts                     # Agent 主循环(核心)
│   ├── types.ts                     # 统一契约(Message/ToolCall/…)
│   ├── provider/                    # 大模型抽象:types / mock / openai
│   ├── tools/                       # 工具系统:types / registry / builtin
│   ├── memory/                      # 记忆:conversation / vector / manager
│   ├── context/                     # 上下文窗口裁剪
│   ├── planner.ts                   # 任务规划
│   ├── multiagent/                  # 多智能体:bus / team
│   ├── mcp/                         # MCP:client / adaptor
│   ├── telemetry/                   # 追踪:span / JSONL
│   └── util/                        # calc(安全表达式)/ json-schema / tokens
├── examples/                        # 6 个可运行演示(含 2 个评测 harness)+ mock MCP 服务器
└── tests/                           # 10 个测试文件,54 个用例
```

## 设计原则

1. **零依赖**:所有功能(JSON Schema 校验、向量嵌入、表达式求值、JSON-RPC)自己实现,
   教学透明度最高,也最适合作为理解原理的起点。
2. **面向接口**:`ChatProvider` / `Tool` / `MemoryManager` 都是接口,
   换实现不改业务逻辑——这是连接真实世界的入口。
3. **依赖注入**:工具、记忆通过构造参数与 `ToolContext` 注入,而非全局单例。
4. **防御式编程**:工具参数必须校验;工具异常不打断循环,回喂给模型自愈;
   表达式求值拒绝任意代码执行。
5. **可观测**:事件流 + span 追踪,复现每一步决策。

> 详细代码走读见教程第 07 章,API 参考与练习见第 15 章(附录 B)。

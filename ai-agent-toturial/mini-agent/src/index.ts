/**
 * index.ts —— mini-agent 公共导出
 *
 * 对外暴露稳定 API。使用者只需要:
 *   import { Agent, ToolRegistry, createBuiltinTools, MockProvider } from './src/index.ts';
 */

// 核心
export { Agent, type AgentOptions, type AgentEventMap, type RunOptions } from './agent.ts';
export type {
  Message,
  Role,
  ToolCall,
  ToolDefinition,
  JsonSchema,
  Completion,
  CompleteOptions,
  StreamEvent,
  AgentResult,
  AgentRunStats,
  StopReason,
  ToolOutput,
} from './types.ts';

// 大模型接口
export type { ChatProvider, MockTurn } from './provider/types.ts';
export { MockProvider, type MockProviderOptions, type MockResponder } from './provider/mock.ts';
export { OpenAIProvider, type OpenAIProviderOptions } from './provider/openai.ts';

// 工具
export type { Tool, ToolContext } from './tools/types.ts';
export { ToolRegistry } from './tools/registry.ts';
export { createBuiltinTools, type BuiltinToolOptions } from './tools/builtin.ts';
export { safeEvaluate } from './util/calc.ts';
export { validate } from './util/json-schema.ts';
export { TokenEstimator } from './util/tokens.ts';

// 记忆
export { MemoryManager, type MemoryManagerOptions } from './memory/manager.ts';
export { VectorMemory, type MemoryEntry, type ScoredEntry } from './memory/vector.ts';
export { ConversationBuffer } from './memory/conversation.ts';

// 上下文
export { ContextWindow, type ContextWindowOptions, type TrimResult } from './context/window.ts';

// 规划
export { Planner, type PlanStep, type PlanStepStatus } from './planner.ts';

// 多智能体
export { MessageBus, type BusMessage } from './multiagent/bus.ts';
export { Orchestrator, RoleAgent, type TeamMember, type TeamContext, type TeamRunResult } from './multiagent/team.ts';

// MCP
export { StdioMcpClient, type McpServerInfo, type McpToolInfo, type McpCallResult } from './mcp/client.ts';
export { mcpToolsToTools } from './mcp/adaptor.ts';

// 观测
export { Tracer, Span, type TraceEvent, type SpanKind, createJsonlTraceWriter } from './telemetry/trace.ts';

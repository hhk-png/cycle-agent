/**
 * types.ts —— 核心数据类型
 *
 * 整个 mini-agent 都建立在这组不可变(概念上)的数据结构上。
 * 把它们单独放在一个文件里,是为了让所有模块共享同一套「契约」,
 * 避免各模块各自定义自己的 Message,导致互相转换时出错。
 * 工程化第一课:先定契约,再写实现。
 */

/** 消息角色。tool 角色代表「工具执行结果的回传」。 */
export type Role = 'system' | 'user' | 'assistant' | 'tool';

/** 一条工具调用请求(由模型发出)。 */
export interface ToolCall {
  /** 工具调用唯一 ID,用于把调用与结果配对。 */
  id: string;
  /** 工具名。 */
  name: string;
  /** 工具参数(已按 JSON Schema 校验)。 */
  arguments: Record<string, unknown>;
}

/** 多模态内容片段。文本之外预留图片,便于扩展到多模态模型。 */
export interface ContentPart {
  type: 'text' | 'image';
  text?: string;
  mediaType?: string;
  /** base64 编码的图像数据。 */
  data?: string;
}

/**
 * 一条消息。既有纯文本形式(content 为 string),
 * 也有多模态形式(content 为 ContentPart[])。
 */
export interface Message {
  role: Role;
  content: string | ContentPart[];
  /** tool 消息的归属名(工具名);assistant 消息可为空。 */
  name?: string;
  /** tool 消息关联的 toolCallId。 */
  toolCallId?: string;
  /** assistant 消息携带的工具调用请求(可多个)。 */
  toolCalls?: ToolCall[];
  /** 便于 trace 的时间戳。 */
  timestamp?: number;
  /** 附加元数据(不参与推理,仅用于观测)。 */
  meta?: Record<string, unknown>;
}

/** 一次工具执行的结果。 */
export interface ToolOutput {
  content: string;
  isError?: boolean;
  /** 执行耗时,毫秒。 */
  durationMs?: number;
}

/** 一次 Agent 运行的结束原因。 */
export type StopReason =
  | 'end_turn' // 模型不再调用工具,正常结束
  | 'max_iterations' // 迭代轮数用尽
  | 'max_tokens' // 输出 token 上限
  | 'error' // 发生了无法恢复的错误
  | 'interrupted'; // 被外部中断

/** Agent 单次运行统计。 */
export interface AgentRunStats {
  iterations: number;
  llmCalls: number;
  toolCalls: number;
  toolErrors: number;
  totalTokens: number;
  startTime: number;
  endTime: number;
  stopReason: StopReason;
}

/** Agent 单次运行的最终结果。 */
export interface AgentResult {
  /** 模型最终输出(不含工具调用过程的中间文本)。 */
  output: string;
  /** 完整消息历史(含工具往返),便于复盘。 */
  messages: Message[];
  stats: AgentRunStats;
}

/**
 * 工具定义(JSON Schema 形式的入参契约)。
 * 放在 types.ts 而非 tools/ 下,是因为 provider 也需要它
 * (模型侧的工具调用描述),避免 provider 依赖 tools 造成循环引用。
 */
export interface ToolDefinition {
  name: string;
  description: string;
  /** 入参 JSON Schema(本实现支持常用子集,见 util/json-schema.ts)。 */
  inputSchema: JsonSchema;
}

/**
 * JSON Schema 子集。为保持零依赖,mini-agent 实现了
 * 足够覆盖工具入参校验的迷你版本(draft-07 常用关键字)。
 */
export interface JsonSchema {
  type?: string | string[];
  properties?: Record<string, JsonSchema>;
  required?: string[];
  items?: JsonSchema;
  enum?: unknown[];
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  pattern?: string;
  description?: string;
  additionalProperties?: boolean;
}

/** 聊天补全的最终结果。 */
export interface Completion {
  content: string;
  toolCalls: ToolCall[];
  finishReason: 'stop' | 'tool_calls' | 'length' | 'content_filter';
  usage?: { promptTokens: number; completionTokens: number };
}

/** 流式事件。文本增量 / 工具调用增量。 */
export type StreamEvent =
  | { type: 'text_delta'; delta: string }
  | { type: 'tool_call_delta'; index: number; id?: string; name?: string; argsDelta?: string }
  | { type: 'done'; completion: Completion };

/** 传给 provider 的补全选项。 */
export interface CompleteOptions {
  temperature?: number;
  maxTokens?: number;
  tools?: ToolDefinition[];
  stop?: string[];
  signal?: AbortSignal;
}

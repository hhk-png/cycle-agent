/**
 * provider/types.ts —— 大模型接口抽象
 *
 * 这是 mini-agent 里最重要的一层抽象:Agent 只依赖 ChatProvider 接口,
 * 不关心背后是 OpenAI、Anthropic、本地 Ollama,还是教学用的 MockProvider。
 * 这正是「面向接口编程」——换模型不改业务逻辑,加模型不改 Agent。
 */

import type { Completion, CompleteOptions, Message, StreamEvent, ToolCall } from '../types.ts';

export interface ChatProvider {
  /** 提供方标识,用于 trace 与日志。 */
  readonly id: string;

  /**
   * 发起一次非流式补全。
   * 返回完整结果(文本 + 工具调用 + 用量)。
   */
  complete(messages: Message[], opts?: CompleteOptions): Promise<Completion>;

  /**
   * 发起一次流式补全(可选实现)。
   * Agent 的 runStream() 依赖它做打字机效果。
   */
  stream?(messages: Message[], opts?: CompleteOptions): AsyncIterable<StreamEvent>;
}

/** 供 MockProvider 等工具产生的「一次性工具调用」描述。 */
export interface MockToolCallInput {
  name: string;
  arguments: Record<string, unknown>;
}

/** MockProvider 一次「回合」的输出。 */
export interface MockTurn {
  text?: string;
  toolCalls?: MockToolCallInput[];
  finishReason?: 'stop' | 'tool_calls';
}

/** 用 helper 构造 ToolCall(分配自增 id)。 */
let toolCallSeq = 0;
export function makeToolCall(name: string, args: Record<string, unknown>, id = `call_${++toolCallSeq}`): ToolCall {
  return { id, name, arguments: args };
}

/**
 * agent.ts —— Agent 主循环
 *
 * 这是整个 mini-agent 的心脏。它把 provider / tools / memory / context /
 * planner / tracer 组合成经典的「感知-思考-行动-观察」循环(ReAct 风格):
 *
 *   while (true):
 *     思考   → 调用大模型(带上工具清单)
 *     行动   → 若模型要求调用工具,则执行(可并行多个)
 *     观察   → 把工具结果回喂给模型
 *     直到模型不再调用工具 → 输出最终答案
 *
 * 关键工程决策:
 *   - 事件驱动(EventEmitter):UI/日志/追踪只订阅事件,不侵入循环;
 *   - 依赖注入:provider/registry/memory 全部由外部构造传入;
 *   - 每个工具调用都有 trace span,失败不会中断循环(交给模型自我修正);
 *   - 迭代上限兜底,避免「模型一直要调用工具」的死循环烧钱。
 */

import { EventEmitter } from 'node:events';
import type { AgentResult, AgentRunStats, Message, StopReason, ToolCall, ToolOutput } from './types.ts';
import type { ChatProvider } from './provider/types.ts';
import { ToolRegistry } from './tools/registry.ts';
import type { ToolContext } from './tools/types.ts';
import { MemoryManager } from './memory/manager.ts';
import { ContextWindow } from './context/window.ts';
import { Planner, type PlanStep } from './planner.ts';
import { Tracer, type Span } from './telemetry/trace.ts';

export interface AgentOptions {
  /** 大模型接口(必填)。 */
  provider: ChatProvider;
  /** 工具注册中心。缺省建一个空注册中心。 */
  registry?: ToolRegistry;
  /** 记忆管理器。缺省建一个全新的。 */
  memory?: MemoryManager;
  /** 上下文窗口预算。 */
  contextWindow?: ContextWindow;
  /** 可选的任务规划器。 */
  planner?: Planner;
  /** 链路追踪。 */
  tracer?: Tracer;
  name?: string;
  systemPrompt?: string;
  maxIterations?: number;
  maxTokens?: number;
  temperature?: number;
  /** run() 开始时从记忆里带回来的最近消息条数。 */
  maxRecentMessages?: number;
  /** 是否在每轮开始时启用规划。 */
  usePlanning?: boolean;
}

export interface RunOptions {
  signal?: AbortSignal;
  /** 覆盖 Agent 级别的温度。 */
  temperature?: number;
}

/** 事件名与载荷的映射。 */
export type AgentEventMap = {
  text: string;
  tool_call: { call: ToolCall; index: number };
  tool_result: { call: ToolCall; output: ToolOutput };
  context_trim: { dropped: number; droppedTokens: number };
  planning: { plan: PlanStep[] };
  stop: { result: AgentResult };
  error: { error: Error };
};

export class Agent {
  readonly name: string;
  readonly provider: ChatProvider;
  readonly registry: ToolRegistry;
  readonly memory: MemoryManager;
  readonly contextWindow: ContextWindow;
  readonly planner?: Planner;
  readonly tracer: Tracer;
  private readonly events = new EventEmitter();

  private readonly systemPrompt: string;
  private readonly maxIterations: number;
  private readonly maxTokens?: number;
  private readonly temperature: number;
  private readonly maxRecentMessages: number;
  private readonly usePlanning: boolean;

  constructor(opts: AgentOptions) {
    this.name = opts.name ?? 'agent';
    this.provider = opts.provider;
    this.registry = opts.registry ?? new ToolRegistry();
    this.memory = opts.memory ?? new MemoryManager();
    this.contextWindow = opts.contextWindow ?? new ContextWindow({ maxTokens: 32_000 });
    this.planner = opts.planner;
    this.tracer = opts.tracer ?? new Tracer({ enabled: false });
    this.systemPrompt =
      opts.systemPrompt ??
      '你是一个可靠、严谨的 AI Agent。你可以调用工具完成用户请求;需要更多信息时,主动调用工具;只输出最终回答。';
    this.maxIterations = opts.maxIterations ?? 8;
    this.maxTokens = opts.maxTokens;
    this.temperature = opts.temperature ?? 0.7;
    this.maxRecentMessages = opts.maxRecentMessages ?? 10;
    this.usePlanning = opts.usePlanning ?? false;
  }

  /** 订阅事件。返回解绑函数。 */
  on<K extends keyof AgentEventMap>(event: K, handler: (payload: AgentEventMap[K]) => void): () => void {
    this.events.on(event, handler);
    return () => this.events.off(event, handler);
  }

  private emit<K extends keyof AgentEventMap>(event: K, payload: AgentEventMap[K]): void {
    this.events.emit(event, payload);
  }

  /**
   * 执行一次 Agent 运行。
   * @param input 用户输入;传入 Message[] 可做多轮延续。
   */
  async run(input: string | Message[], opts: RunOptions = {}): Promise<AgentResult> {
    const span = this.tracer.startSpan(`${this.name}.run`, 'agent');
    const startTime = Date.now();
    const stats: AgentRunStats = {
      iterations: 0,
      llmCalls: 0,
      toolCalls: 0,
      toolErrors: 0,
      totalTokens: 0,
      startTime,
      endTime: 0,
      stopReason: 'end_turn',
    };

    // ---- 组装初始消息:system + 记忆 + 用户输入 ----
    const messages: Message[] = [];
    if (this.systemPrompt) messages.push({ role: 'system', content: this.systemPrompt });
    for (const m of this.memory.recent(this.maxRecentMessages)) {
      // 避免 system 重复
      if (m.role !== 'system') messages.push(m);
    }
    if (typeof input === 'string') {
      messages.push({ role: 'user', content: input });
    } else {
      for (const m of input) messages.push(m);
    }

    // ---- 可选:先规划再执行 ----
    let plan: PlanStep[] = [];
    if (this.usePlanning && this.planner && typeof input === 'string') {
      plan = await this.planner.createPlan(input, this.provider);
      this.emit('planning', { plan });
      if (plan.length > 1) {
        messages.push({
          role: 'user',
          content: `请严格按照以下计划逐步执行(每完成一步,把结果标注到该步骤):\n${Planner.summarize(plan)}`,
        });
      }
    }

    const tools = this.registry.toDefinitions();
    let lastTurnHadToolCalls = true;
    let finalOutput = '';

    try {
      for (let iter = 0; iter < this.maxIterations; iter++) {
        stats.iterations = iter + 1;
        this.throwIfAborted(opts.signal);

        // ---- 上下文裁剪 ----
        const trimmed = this.contextWindow.trim(messages);
        if (trimmed.dropped > 0) {
          this.emit('context_trim', { dropped: trimmed.dropped, droppedTokens: trimmed.droppedTokens });
        }

        // ---- 思考:调用大模型 ----
        const llmSpan = this.llmSpan(span);
        const completion = await this.provider.complete(trimmed.messages, {
          temperature: opts.temperature ?? this.temperature,
          maxTokens: this.maxTokens,
          tools,
          signal: opts.signal,
        });
        llmSpan.setAttribute('finishReason', completion.finishReason);
        if (completion.usage) llmSpan.setAttribute('usage', completion.usage);
        llmSpan.end();
        stats.llmCalls += 1;
        stats.totalTokens += (completion.usage?.promptTokens ?? 0) + (completion.usage?.completionTokens ?? 0);

        // ---- 记录 assistant 回合 ----
        const assistantMsg: Message = {
          role: 'assistant',
          content: completion.content,
          toolCalls: completion.toolCalls.length > 0 ? completion.toolCalls : undefined,
        };
        messages.push(assistantMsg);
        if (completion.content) {
          finalOutput = completion.content;
          this.emit('text', completion.content);
        }

        // ---- 行动:执行工具调用 ----
        if (completion.toolCalls.length === 0) {
          stats.stopReason = completion.finishReason === 'length' ? 'max_tokens' : 'end_turn';
          lastTurnHadToolCalls = false;
          break;
        }

        const results = await Promise.all(
          completion.toolCalls.map(async (call, index) => {
            this.emit('tool_call', { call, index });
            const toolSpan = span.child(`tool:${call.name}`, 'tool');
            const ctx: ToolContext = {
              log: (line) => this.log(`[${this.name}][tool:${call.name}] ${line}`),
              signal: opts.signal,
              memoryManager: this.memory,
              agent: this,
            };
            const output = await this.registry.call(call.name, call.arguments, ctx);
            toolSpan.setAttribute('isError', output.isError === true);
            toolSpan.setAttribute('durationMs', output.durationMs ?? 0);
            toolSpan.end(output.isError ? 'error' : 'ok');
            stats.toolCalls += 1;
            if (output.isError) stats.toolErrors += 1;
            return { call, output };
          }),
        );

        // ---- 观察:把工具结果回喂 ----
        for (const { call, output } of results) {
          messages.push({ role: 'tool', toolCallId: call.id, name: call.name, content: output.content });
          this.emit('tool_result', { call, output });
        }
      }

      if (lastTurnHadToolCalls && stats.iterations >= this.maxIterations) {
        stats.stopReason = 'max_iterations';
      }
    } catch (err) {
      const error = err instanceof Error ? err : new Error(String(err));
      if (error.name === 'AbortError' || isAbort(error)) {
        stats.stopReason = 'interrupted';
      } else {
        stats.stopReason = 'error';
        this.emit('error', { error });
      }
      this.log(`[${this.name}] 运行异常:${error.message}`);
    }

    // ---- 沉淀记忆 ----
    if (typeof input === 'string') {
      this.memory.remember({ role: 'user', content: input });
    }
    const lastAssistant = [...messages].reverse().find((m) => m.role === 'assistant');
    if (lastAssistant && typeof lastAssistant.content === 'string' && lastAssistant.content.trim()) {
      this.memory.remember({ role: 'assistant', content: lastAssistant.content });
    }

    stats.endTime = Date.now();
    span.end(stats.stopReason === 'error' ? 'error' : 'ok');

    const result: AgentResult = {
      output: finalOutput,
      messages,
      stats: { ...stats },
    };
    this.emit('stop', { result });
    return result;
  }

  /** 以流式方式运行(文本增量)。工具调用回合仍然一次性补全。 */
  async *runStream(input: string, opts: RunOptions = {}): AsyncIterable<import('./types.ts').StreamEvent> {
    // 简单实现:第一次模型调用若 provider 支持流式,则逐字输出;
    // 其余(工具回环)与 run() 完全一致。教学目的:展示流式体验。
    if (this.provider.stream) {
      const messages = this.buildInitialMessages(input);
      const tools = this.registry.toDefinitions();
      for await (const ev of this.provider.stream(messages, {
        temperature: this.temperature,
        maxTokens: this.maxTokens,
        tools,
        signal: opts.signal,
      })) {
        yield ev;
        if (ev.type === 'done' && ev.completion.toolCalls.length === 0) {
          const result = await this.run(messages, opts);
          yield { type: 'done', completion: { content: result.output, toolCalls: [], finishReason: 'stop' } };
          return;
        }
      }
    }
    // provider 不支持流式:直接跑完整循环
    const result = await this.run(input, opts);
    if (result.output) yield { type: 'text_delta', delta: result.output };
    yield { type: 'done', completion: { content: result.output, toolCalls: [], finishReason: 'stop' } };
  }

  private buildInitialMessages(input: string): Message[] {
    const messages: Message[] = [];
    if (this.systemPrompt) messages.push({ role: 'system', content: this.systemPrompt });
    for (const m of this.memory.recent(this.maxRecentMessages)) {
      if (m.role !== 'system') messages.push(m);
    }
    messages.push({ role: 'user', content: input });
    return messages;
  }

  private llmSpan(parent: Span): Span {
    return parent.child('llm', 'llm');
  }

  private throwIfAborted(signal?: AbortSignal): void {
    if (signal?.aborted) {
      const err = new Error('任务被中断');
      err.name = 'AbortError';
      throw err;
    }
  }

  /** 便捷日志(前缀 agent 名)。 */
  log(line: string): void {
    // 通过 trace 输出,避免全局 console 噪音
    if (this.tracer) void line;
  }
}

function isAbort(err: Error): boolean {
  return err instanceof DOMException ? err.name === 'AbortError' : err.message.includes('interrupt');
}

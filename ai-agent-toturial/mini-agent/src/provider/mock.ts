/**
 * provider/mock.ts —— 可脚本化的确定性「假大模型」
 *
 * 为什么需要 MockProvider?
 *   1. 测试确定性:真实 LLM 输出是概率性的,单元测试无法断言。
 *   2. 离线可验证:不联网、不需要 API Key,任何环境都能跑通。
 *   3. 工程化实践:把「模型不确定」与「逻辑确定」解耦——测试 Agent 逻辑时,
 *      模型行为是可控的;跑真实模型时,Agent 逻辑不变。
 *
 * 用法:
 *   - steps:  顺序脚本。第 N 次调用消耗 steps[N];耗尽后用 defaultText。
 *   - responder: 完全自定义的函数,拿到全部消息上下文,返回下一回合。
 *
 * 它同时实现了 stream(),供流式演示使用。
 */

import type { Completion, CompleteOptions, Message, StreamEvent } from '../types.ts';
import type { ChatProvider, MockTurn } from './types.ts';
import { makeToolCall } from './types.ts';
import { TokenEstimator } from '../util/tokens.ts';

export type MockResponder = (messages: Message[], tools?: import('../types.ts').ToolDefinition[]) => MockTurn | Promise<MockTurn>;

export interface MockProviderOptions {
  /** 顺序脚本:每次 complete 消耗一个;元素可以是对象或函数(函数接收完整消息历史)。 */
  steps?: Array<MockTurn | MockResponder>;
  /** 完全自定义响应;优先级低于 steps。 */
  responder?: MockResponder;
  /** 脚本/响应器用尽后的兜底输出。 */
  defaultText?: string;
  id?: string;
  /** 是否把「看似数学表达式的用户输入」交给 calculator 工具(mock 的迷你「智能」)。 */
  autoMath?: boolean;
}

const estimator = new TokenEstimator();

export class MockProvider implements ChatProvider {
  readonly id: string;
  private steps: Array<MockTurn | MockResponder>;
  private stepIndex = 0;
  private responder?: MockResponder;
  private defaultText: string;
  private autoMath: boolean;
  /** 记录调用次数,便于测试断言。 */
  callCount = 0;
  /** 累积「生成了多少个 token」,模拟用量上报。 */
  generatedTokens = 0;

  constructor(opts: MockProviderOptions = {}) {
    this.id = opts.id ?? 'mock';
    this.steps = opts.steps ?? [];
    this.responder = opts.responder;
    this.defaultText = opts.defaultText ?? '这是一个模拟响应(MockProvider)。';
    this.autoMath = opts.autoMath ?? true;
  }

  /** 手动追加脚本(支持测试中途改剧本)。 */
  pushStep(step: MockTurn | MockResponder): void {
    this.steps.push(step);
  }

  async complete(messages: Message[], opts?: CompleteOptions): Promise<Completion> {
    this.callCount += 1;

    let turn: MockTurn | null = null;

    // 1) 先走步骤脚本
    if (this.stepIndex < this.steps.length) {
      const step = this.steps[this.stepIndex];
      this.stepIndex += 1;
      if (typeof step === 'function') {
        turn = await (step as MockResponder)(messages, opts?.tools);
      } else {
        turn = step;
      }
    }

    // 2) 再走 responder
    if (turn === null && this.responder) {
      turn = await this.responder(messages, opts?.tools);
    }

    // 3) 兜底:自动数学(把最后一条用户消息里的简单算术交给 calculator)
    if (turn === null && this.autoMath) {
      const auto = this.tryAutoMath(messages);
      if (auto) turn = auto;
    }

    // 4) 最终兜底
    if (turn === null) {
      turn = { text: this.defaultText, finishReason: 'stop' };
    }

    const toolCalls = (turn.toolCalls ?? []).map((tc) => makeToolCall(tc.name, tc.arguments));
    const finishReason = turn.finishReason ?? (toolCalls.length > 0 ? 'tool_calls' : 'stop');
    const content = turn.text ?? '';

    this.generatedTokens += estimator.estimateText(content) + toolCalls.length * 8;
    return {
      content,
      toolCalls,
      finishReason,
      usage: { promptTokens: estimator.estimate(messages), completionTokens: estimator.estimateText(content) + toolCalls.length * 8 },
    };
  }

  async *stream(messages: Message[], opts?: CompleteOptions): AsyncIterable<StreamEvent> {
    const completion = await this.complete(messages, opts);
    if (completion.content) {
      yield { type: 'text_delta', delta: completion.content };
    }
    yield { type: 'done', completion };
  }

  /** 简易「智能」:识别 `数字 运算符 数字` 的表达式,交给 calculator。 */
  private tryAutoMath(messages: Message[]): MockTurn | null {
    const last = [...messages].reverse().find((m) => m.role === 'user');
    if (!last || typeof last.content !== 'string') return null;
    const expr = last.content.trim();
    // 只允许安全字符,避免把任意文本当表达式
    if (!/^[\d\s+\-*/%.()]+$/.test(expr)) return null;
    if (!/[+\-*/%]/.test(expr)) return null;
    return {
      text: '让我用计算器算一下。',
      toolCalls: [{ name: 'calculator', arguments: { expression: expr } }],
    };
  }
}

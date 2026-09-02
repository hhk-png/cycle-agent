/**
 * context/window.ts —— 上下文窗口管理
 *
 * 模型窗口是硬约束。ContextWindow 负责把「想喂给模型的消息」裁剪到预算内:
 *   1. system 消息永远保留;
 *   2. 其余消息按「从旧到新」丢弃,直到 token 数 ≤ maxTokens;
 *   3. 返回丢弃统计,供观测。
 *
 * 它解决的是「输入超窗」问题;输出超长由 provider 侧的 maxTokens 控制。
 * 更进一步的策略(摘要压缩、关键帧保留)见教程第 04 章,
 * 本实现把「窗口预算」这个地基做扎实。
 */

import type { Message } from '../types.ts';
import { TokenEstimator } from '../util/tokens.ts';

export interface ContextWindowOptions {
  maxTokens: number;
  estimator?: TokenEstimator;
  /** 预留的 system 消息长度阈值:system 太长时单独截断。 */
  maxSystemTokens?: number;
}

export interface TrimResult {
  messages: Message[];
  dropped: number;
  droppedTokens: number;
}

export class ContextWindow {
  private readonly maxTokens: number;
  private readonly maxSystemTokens: number;
  readonly estimator: TokenEstimator;

  constructor(opts: ContextWindowOptions) {
    this.maxTokens = opts.maxTokens;
    this.maxSystemTokens = opts.maxSystemTokens ?? Math.floor(opts.maxTokens / 2);
    this.estimator = opts.estimator ?? new TokenEstimator();
  }

  /** 把消息列表裁剪到预算内,返回剩余消息与丢弃统计。 */
  trim(messages: Message[]): TrimResult {
    if (this.estimator.estimate(messages) <= this.maxTokens) {
      return { messages, dropped: 0, droppedTokens: 0 };
    }

    const system = messages.filter((m) => m.role === 'system');
    const rest = messages.filter((m) => m.role !== 'system');

    // 先裁 system(截断到 maxSystemTokens)
    let systemMsgs = system;
    if (this.estimator.estimate(system) > this.maxSystemTokens) {
      systemMsgs = truncateSystem(system, this.maxSystemTokens, this.estimator);
    }

    // 再从旧到新裁剪非 system 消息
    const kept: Message[] = [];
    let total = this.estimator.estimate(systemMsgs);
    for (let i = rest.length - 1; i >= 0; i--) {
      const m = rest[i]!;
      const cost = this.estimator.estimateMessage(m);
      if (total + cost > this.maxTokens) break;
      kept.unshift(m);
      total += cost;
    }

    const dropped = messages.length - kept.length - systemMsgs.length;
    const keptMsgs = [...systemMsgs, ...kept];
    const droppedTokens = this.estimator.estimate(messages) - this.estimator.estimate(keptMsgs);
    return { messages: keptMsgs, dropped, droppedTokens };
  }
}

/** 对过长的 system 消息做「头尾保留」截断(保留语义最重要的开头)。 */
function truncateSystem(system: Message[], budget: number, estimator: TokenEstimator): Message[] {
  const out: Message[] = [];
  for (const m of system) {
    if (typeof m.content === 'string' && estimator.estimateText(m.content) > budget) {
      const chars = m.content;
      const headChars = Math.max(1, Math.floor(budget * 0.6 * 4));
      out.push({
        ...m,
        content: chars.slice(0, headChars) + '\n...(system 被截断)...',
      });
    } else {
      out.push(m);
    }
  }
  return out;
}

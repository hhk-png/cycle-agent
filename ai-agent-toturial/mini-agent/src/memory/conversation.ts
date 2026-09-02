/**
 * memory/conversation.ts —— 短期对话缓冲(滑窗)
 *
 * 短期记忆 = 最近的对话上下文。模型窗口有限,不能无限堆积,
 * 所以用「滑动窗口」:超过上限时,从最旧开始丢弃(保留 system 消息)。
 *
 * 更进阶的做法(本教程第 04 章会展开):
 *   - 摘要压缩:把被丢弃的部分交给模型总结成摘要,保留要点;
 *   - 重要性过滤:只保留标记为重要的消息。
 * ConversationBuffer 是这一切的地基。
 */

import type { Message } from '../types.ts';

export interface ConversationBufferOptions {
  /** 最多保留多少条消息;system 消息不占配额。 */
  maxMessages?: number;
}

export class ConversationBuffer {
  private items: Message[] = [];
  private readonly maxMessages: number;

  constructor(opts: ConversationBufferOptions = {}) {
    this.maxMessages = opts.maxMessages ?? 20;
  }

  add(message: Message): void {
    this.items.push({ ...message, timestamp: message.timestamp ?? Date.now() });
    this.trim();
  }

  addMany(messages: Message[]): void {
    for (const m of messages) this.add(m);
  }

  private trim(): void {
    if (this.items.length <= this.maxMessages) return;
    // 把 system 抽出来,其余按 FIFO 丢弃到配额内。
    const system = this.items.filter((m) => m.role === 'system');
    const rest = this.items.filter((m) => m.role !== 'system');
    const overflow = rest.length - this.maxMessages;
    const dropped = rest.splice(0, Math.max(0, overflow));
    // 把被丢弃的部分标记出来(供后续摘要压缩钩子使用)
    if (dropped.length > 0 && dropped.length < this.items.length) {
      // 保留最后一条被丢弃消息的索引信息,便于测试观察
      void dropped;
    }
    this.items = [...system, ...rest];
  }

  /** 返回一份拷贝,避免外部修改内部状态。 */
  messages(): Message[] {
    return this.items.map((m) => ({ ...m, content: Array.isArray(m.content) ? m.content.slice() : m.content }));
  }

  recent(n: number): Message[] {
    const msgs = this.messages();
    return msgs.slice(Math.max(0, msgs.length - n));
  }

  clear(): void {
    this.items = [];
  }

  get size(): number {
    return this.items.length;
  }
}

/**
 * memory/manager.ts —— 记忆管理器
 *
 * 把「短期对话缓冲」与「长期向量记忆」统一到一个门面(门面模式)。
 * Agent 只跟 MemoryManager 打交道,不关心背后是环形缓冲还是向量库。
 *
 * 记忆的写入/读取策略(可扩展钩子):
 *   - remember(): 每轮对话都进短期缓冲;
 *   - saveFact():  显式沉淀到长期记忆(通常由 remember 工具或策略触发);
 *   - recall():    查询时先用向量检索,把相关事实注入上下文。
 */

import type { Message } from '../types.ts';
import { ConversationBuffer } from './conversation.ts';
import { VectorMemory, type ScoredEntry } from './vector.ts';

export interface MemoryManagerOptions {
  shortTerm?: ConversationBuffer;
  longTerm?: VectorMemory;
  /** 一次 recall 最多返回几条事实。 */
  maxRecall?: number;
}

export class MemoryManager {
  readonly shortTerm: ConversationBuffer;
  readonly longTerm: VectorMemory;
  private readonly maxRecall: number;

  constructor(opts: MemoryManagerOptions = {}) {
    this.shortTerm = opts.shortTerm ?? new ConversationBuffer();
    this.longTerm = opts.longTerm ?? new VectorMemory();
    this.maxRecall = opts.maxRecall ?? 3;
  }

  /** 每轮消息都进短期记忆。 */
  remember(message: Message): void {
    this.shortTerm.add(message);
  }

  /** 把一条事实写入长期记忆。 */
  saveFact(text: string, metadata?: Record<string, unknown>): void {
    this.longTerm.add(text, metadata);
  }

  /** 语义检索长期记忆。 */
  recall(query: string, k?: number): ScoredEntry[] {
    return this.longTerm.search(query, k ?? this.maxRecall);
  }

  /** 把检索到的事实拼成一段可直接注入上下文的文本。 */
  recallAsContext(query: string, k?: number): string {
    const hits = this.recall(query, k);
    if (hits.length === 0) return '';
    const lines = hits.map((h, i) => `${i + 1}. ${h.text}`);
    return `[相关记忆]\n${lines.join('\n')}`;
  }

  /** 取最近的 n 条对话。 */
  recent(n: number): Message[] {
    return this.shortTerm.recent(n);
  }

  /** 全部短期消息(含 system)。 */
  messages(): Message[] {
    return this.shortTerm.messages();
  }

  clear(): void {
    this.shortTerm.clear();
    this.longTerm.clear();
  }

  get factCount(): number {
    return this.longTerm.size;
  }
}

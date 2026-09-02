/**
 * tokens.ts —— 无外部依赖的 token 估算器
 *
 * 真实场景中,不同模型的 tokenizer 各不相同(字节对编码、句子片等),
 * 精确计数需要加载对应 tokenizer(如 tiktoken / tokenizers)。
 * mini-agent 保持零依赖,因此采用一个可解释、可预测的启发式估算:
 *
 *   - 中/日/韩等 CJK 字符: 1 字符 ≈ 1 token
 *   - 其余字符(英文、数字、符号): 4 字符 ≈ 1 token
 *
 * 这个估算对「上下文窗口预算」这种只关心量级的场景足够用。
 * 只要与真实 tokenizer 的偏差方向一致(别把 8k 估成 1k),就能避免超窗。
 */

const CJK_RE = /[぀-ヿ㐀-鿿豈-﫿\U00020000-\U0002ffff]/g;

export class TokenEstimator {
  /** 估算一段文本的 token 数。 */
  estimateText(text: string): number {
    if (!text) return 0;
    const cjkCount = (text.match(CJK_RE) ?? []).length;
    const otherCount = text.length - cjkCount;
    return cjkCount + Math.ceil(otherCount / 4);
  }

  /** 估算一条消息的 token 数(考虑角色与工具调用的结构性开销)。 */
  estimateMessage(msg: { role: string; content: unknown; toolCalls?: unknown[] }): number {
    // 每个角色标签、工具调用结构都算一点开销,避免把长度算少了。
    let tokens = 4; // 角色标签 + 结构开销
    const content = msg.content;
    if (typeof content === 'string') {
      tokens += this.estimateText(content);
    } else if (Array.isArray(content)) {
      for (const part of content) {
        if (typeof part === 'object' && part !== null && 'text' in part) {
          tokens += this.estimateText(String((part as { text?: string }).text ?? ''));
        } else if (typeof part === 'object' && part !== null && 'data' in part) {
          tokens += this.estimateText(String((part as { data?: string }).data ?? ''));
        }
      }
    }
    if (msg.toolCalls && msg.toolCalls.length > 0) {
      tokens += 12 * msg.toolCalls.length; // 每个 tool call 的 JSON 外壳
      for (const tc of msg.toolCalls) {
        tokens += this.estimateText(JSON.stringify((tc as { arguments?: unknown }).arguments ?? {}));
      }
    }
    return tokens;
  }

  /** 估算一整段消息列表的 token 数。 */
  estimate(messages: { role: string; content: unknown; toolCalls?: unknown[] }[]): number {
    let total = 0;
    for (const m of messages) total += this.estimateMessage(m);
    return total;
  }
}

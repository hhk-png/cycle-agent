/**
 * memory/vector.ts —— 零依赖向量记忆
 *
 * 真实的向量数据库(如 Chroma / Milvus / pgvector)依赖 embedding 模型
 * 与 ANN 索引。mini-agent 用「字符 n-gram 哈希嵌入」做教学替代:
 *   - 把文本切成字(1-gram)与相邻两字(2-gram),外加英文单词;
 *   - 每个 token 哈希到一个维度,符号取哈希位的正负;
 *   - 累加得到定长向量;相似度用余弦。
 *
 * 好处:确定性、零依赖、对中文友好。局限:语义上远不如真实 embedding。
 * 但「检索-排序-截断」的工程骨架与真实系统完全一致,
 * 把 VectorMemory 换成真实 embedding + 向量库,Agent 代码一行不用改。
 */

export interface MemoryEntry {
  id: string;
  text: string;
  metadata?: Record<string, unknown>;
  embedding: number[];
  timestamp: number;
}

export interface ScoredEntry extends MemoryEntry {
  score: number;
}

function hashString(s: string): number {
  let h = 5381;
  for (let i = 0; i < s.length; i++) {
    h = ((h << 5) + h) ^ s.charCodeAt(i);
    h |= 0;
  }
  return h;
}

/** 把文本转成 n-gram token 列表。 */
function tokenize(text: string): string[] {
  const lower = text.toLowerCase();
  const tokens: string[] = [];
  // 字 + 相邻两字(对中文有效)
  for (let i = 0; i < lower.length; i++) {
    tokens.push(lower[i]!);
    if (i + 1 < lower.length) tokens.push(lower.slice(i, i + 2));
  }
  // 英文单词
  for (const w of lower.split(/[^a-z0-9]+/)) {
    if (w.length >= 2) tokens.push(`w:${w}`);
  }
  return tokens;
}

export class VectorMemory {
  private entries: MemoryEntry[] = [];
  private seq = 0;
  private readonly dim: number;

  constructor(opts: { dim?: number } = {}) {
    this.dim = opts.dim ?? 256;
  }

  private embed(text: string): number[] {
    const vec = new Array<number>(this.dim).fill(0);
    for (const token of tokenize(text)) {
      const h = hashString(token);
      const idx = Math.abs(h) % this.dim;
      const sign = h & 0x80000000 ? -1 : 1;
      vec[idx]! += sign;
    }
    return vec;
  }

  add(text: string, metadata?: Record<string, unknown>): MemoryEntry {
    const entry: MemoryEntry = {
      id: `mem_${++this.seq}`,
      text,
      metadata,
      embedding: this.embed(text),
      timestamp: Date.now(),
    };
    this.entries.push(entry);
    return entry;
  }

  remove(id: string): boolean {
    const i = this.entries.findIndex((e) => e.id === id);
    if (i < 0) return false;
    this.entries.splice(i, 1);
    return true;
  }

  /** 余弦相似度检索,返回得分降序的前 k 条。 */
  search(query: string, k = 5, threshold = 0): ScoredEntry[] {
    const qv = this.embed(query);
    const qNorm = cosineNorm(qv);
    const scored = this.entries
      .map((e) => ({
        ...e,
        score: cosine(qv, qNorm, e.embedding),
      }))
      .filter((e) => e.score > threshold)
      .sort((a, b) => b.score - a.score)
      .slice(0, k);
    return scored;
  }

  get size(): number {
    return this.entries.length;
  }

  clear(): void {
    this.entries = [];
  }

  all(): MemoryEntry[] {
    return this.entries.slice();
  }
}

function cosineNorm(v: number[]): number {
  let sum = 0;
  for (const x of v) sum += x * x;
  return Math.sqrt(sum) || 1;
}

function cosine(a: number[], aNorm: number, b: number[]): number {
  let dot = 0;
  for (let i = 0; i < a.length; i++) dot += a[i]! * b[i]!;
  return dot / (aNorm * cosineNorm(b));
}

import { createHash } from 'node:crypto';
import type { Article } from './articles.ts';
import { mdToWechatHtml, WECHAT_CONTENT_LIMIT } from './markdown.ts';

/**
 * Arranges tutorial chapters into WeChat issues. **Default: one chapter = one issue.**
 * The h2 split below is only a guardrail, not the normal flow: WECHAT_CONTENT_LIMIT
 * (500k) is an empirical margin, not the API limit — the API accepted a 183,914-char
 * body verbatim (see markdown.ts).
 *
 * If a chapter ever exceeds it: cut only at h2 boundaries (never hard-cut, which would
 * split a code block or table), pick cut points by converted length rather than
 * Markdown length (tables ~2.5-3x, code ~1.3x), and re-convert each part to verify.
 */

/** One WeChat issue (one published article) */
export interface Issue {
  /** Issue number; split ones get -1/-2, e.g. '18-1' */
  id: string;
  /** Original article number, e.g. '18' */
  no: string;
  /** Which part (from 1) */
  part: number;
  /** How many parts the article was split into */
  parts: number;
  /** WeChat title (<= 32 chars) */
  title: string;
  /** Digest (reused from the Juejin set) */
  digest: string;
  /** WeChat body HTML passed to draft/add */
  content: string;
  /** Body Markdown (archived to disk for troubleshooting) */
  markdown: string;
  /** Build hash to detect a draft whose body changed after creation */
  contentHash: string;
  /** Effective limit for this issue (see IssueOptions.contentLimit) */
  limit: number;
  /** Global issue sequence number (from 1); scheduling follows it */
  order: number;
  /** Reminders shown before publishing */
  warnings: string[];
}

/** Split cap; beyond this the article belongs elsewhere, so error out for a human */
const MAX_PARTS = 4;

/** API title limit (editor allows 64); warn at 32 since it may truncate */
const TITLE_LIMIT = 32;

/** h2 line numbers (0-based), fence-aware like articles.ts's findTitleLine — a `##` in a code block is not a heading */
export function findH2Lines(markdown: string): number[] {
  const lines = markdown.split('\n');
  const out: number[] = [];
  let fence: string | null = null;
  for (let i = 0; i < lines.length; i++) {
    const m = /^\s*(```|~~~)/.exec(lines[i]);
    if (m) {
      if (fence === null) fence = m[1];
      else if (m[1] === fence) fence = null;
      continue;
    }
    if (fence === null && /^## /.test(lines[i])) out.push(i);
  }
  return out;
}

/** Split at h2 boundaries; first piece is anything before the first h2 */
export function splitAtH2(markdown: string): string[] {
  const lines = markdown.split('\n');
  const cuts = findH2Lines(markdown);
  if (cuts.length === 0) return [markdown];

  const chunks: string[] = [];
  const first = lines.slice(0, cuts[0]).join('\n').trim();
  if (first) chunks.push(first);
  for (let i = 0; i < cuts.length; i++) {
    const end = i + 1 < cuts.length ? cuts[i + 1] : lines.length;
    chunks.push(lines.slice(cuts[i], end).join('\n').trim());
  }
  return chunks;
}

/** A section that can be moved independently */
interface Chunk {
  md: string;
  /** Converted length; only used to choose cut points */
  htmlLen: number;
}

/** Final conversion's measure: HTML source length (UTF-16 code units) */
export function measure(markdown: string): number {
  return mdToWechatHtml(markdown).length;
}

const joinChunks = (chunks: Chunk[]): string => chunks.map((c) => c.md).join('\n\n');

/**
 * Split n sections into k parts minimizing the longest part (minimax); returns cut
 * indices. Plain halving can leave the longest part near the limit while the rest sit
 * empty (chapter 18: 7.7k/14.5k/5.9k/14.4k).
 */
function partitionMinimax(lens: number[], k: number): number[] | null {
  const n = lens.length;
  const prefix = [0];
  for (const l of lens) prefix.push(prefix[prefix.length - 1] + l);
  const span = (i: number, j: number): number => prefix[j] - prefix[i];

  const INF = Number.POSITIVE_INFINITY;
  const dp: number[][] = Array.from({ length: k + 1 }, () => new Array<number>(n + 1).fill(INF));
  const cut: number[][] = Array.from({ length: k + 1 }, () => new Array<number>(n + 1).fill(-1));
  dp[0][0] = 0;
  for (let t = 1; t <= k; t++) {
    for (let i = t; i <= n; i++) {
      for (let j = t - 1; j < i; j++) {
        if (dp[t - 1][j] === INF) continue;
        const cand = Math.max(dp[t - 1][j], span(j, i));
        if (cand < dp[t][i]) {
          dp[t][i] = cand;
          cut[t][i] = j;
        }
      }
    }
  }
  if (dp[k][n] === INF) return null;

  const cuts: number[] = [];
  let i = n;
  for (let t = k; t >= 1; t--) {
    const j = cut[t][i];
    if (j < 0) return null;
    cuts.unshift(j);
    i = j;
  }
  return cuts;
}

/**
 * Split one body into issues, using the fewest parts that fit the limit and minimax
 * within that. `{ error }` means over-limit but unsplittable — the caller must stop,
 * never hard-cut.
 */
export function splitArticle(
  markdown: string,
  limit: number = WECHAT_CONTENT_LIMIT,
): { parts: string[] } | { error: string } {
  const whole = measure(markdown);
  if (whole <= limit) return { parts: [markdown] };

  const chunks: Chunk[] = splitAtH2(markdown).map((md) => ({ md, htmlLen: measure(md) }));
  if (chunks.length < 2) {
    return {
      error:
        `Body is ${whole} chars, over the limit ${limit}, but this article has no h2 section to cut at.\n` +
        `  Not hard-cutting (it would split a code block/table in half); add second-level headings to it before publishing.`,
    };
  }

  const lens = chunks.map((c) => c.htmlLen);
  let lastError = '';
  for (let k = 2; k <= MAX_PARTS; k++) {
    const cuts = partitionMinimax(lens, k);
    if (!cuts) break;

    const parts: string[] = [];
    for (let t = 0; t < cuts.length; t++) {
      const end = t + 1 < cuts.length ? cuts[t + 1] : chunks.length;
      parts.push(joinChunks(chunks.slice(cuts[t], end)));
    }

    // Cut points use summed section lengths; verify by re-converting each part
    const tooBig = parts
      .map((p, i) => ({ i, len: measure(p) }))
      .find((x) => x.len > limit);
    if (!tooBig) {
      if (parts.some((p) => !p.trim())) return { error: 'Splitting produced an empty body section; check the heading levels.' };
      return { parts };
    }
    lastError = `part ${tooBig.i + 1}/${k} still has ${tooBig.len} chars`;
  }

  return {
    error:
      `${whole} chars over the limit, and splitting into ${MAX_PARTS} parts is still not compliant (${lastError}).\n` +
      `  Most likely some h2 section is itself over the limit — a human needs to split out lower-level headings for it.`,
  };
}

/** Title suffix: Part 1/Part 2 for two parts, Part N for more */
function partSuffix(part: number, parts: number): string {
  if (parts === 1) return '';
  if (parts === 2) return part === 1 ? '(上)' : '(下)';
  const labels = ['一', '二', '三', '四'];
  return `(${labels[part - 1] ?? String(part)})`;
}

/** Digest suffix, matching the title */
function digestSuffix(part: number, parts: number): string {
  if (parts === 1) return '';
  if (parts === 2) return part === 1 ? '（上）' : '（下）';
  const labels = ['一', '二', '三', '四'];
  return `（${labels[part - 1] ?? String(part)}）`;
}

/** Options for arranging issues */
export interface IssueOptions {
  /** Digest map, key = two-digit number (same set as the Juejin config) */
  digests: Record<string, string>;
  /** Per-article title overrides — the WeChat title limit is 32 chars */
  titleOverrides?: Record<string, string>;
  /** Effective split limit (default WECHAT_CONTENT_LIMIT; guardrail, not the API limit) */
  contentLimit?: number;
}

/** Arrange all issues; digests are keyed by two-digit number and shared across parts */
export function buildIssues(
  articles: Article[],
  opts: IssueOptions,
): { issues: Issue[] } | { error: string } {
  const limit = opts.contentLimit ?? WECHAT_CONTENT_LIMIT;
  const issues: Issue[] = [];
  for (const a of articles) {
    const split = splitArticle(a.content, limit);
    if ('error' in split) return { error: `Article ${a.no}: ${split.error}` };

    const baseTitle = opts.titleOverrides?.[a.no] ?? a.title;
    const brief = opts.digests[a.no] ?? '';
    const warnings: string[] = [];
    if (!brief) warnings.push('No digest; WeChat will auto-generate it from the first 54 chars of the body');
    if (brief.length > 120) warnings.push(`Digest is ${brief.length} chars, over WeChat's 120-char limit`);

    for (const [i, md] of split.parts.entries()) {
      const part = i + 1;
      const parts = split.parts.length;
      const title = `${baseTitle}${partSuffix(part, parts)}`;
      const digest = brief ? `${digestSuffix(part, parts)}${brief}` : '';
      if (title.length > TITLE_LIMIT) {
        warnings.push(`Title is ${title.length} chars, over the API limit ${TITLE_LIMIT} (will be rejected or truncated): ${title}`);
      } else if (title.length >= TITLE_LIMIT) {
        warnings.push(`Title is exactly ${title.length} chars, right at the limit; consider shortening it`);
      }

      const content = mdToWechatHtml(md);
      const own = [...warnings];
      // Report the title override once, on the first part
      if (part === 1 && opts.titleOverrides?.[a.no]) {
        own.push(`Title overridden by config to "${baseTitle}" (original filename is ${a.title.length} chars)`);
      }
      issues.push({
        id: parts === 1 ? a.no : `${a.no}-${part}`,
        no: a.no,
        part,
        parts,
        title,
        digest,
        content,
        markdown: md,
        // Hash the rendered HTML plus title/digest, not just the markdown: renderer
        // changes or digest edits must also invalidate the draft, not silently skip a rebuild
        contentHash: createHash('sha256').update(`${title}\n${digest}\n${content}`).digest('hex'),
        limit,
        order: 0, // set below
        warnings: own,
      });
    }
  }

  for (const [i, issue] of issues.entries()) issue.order = i + 1;
  return { issues };
}

/** Longest body (for --list's one-line summary) */
export function longestIssue(issues: Issue[]): Issue {
  return issues.reduce((a, b) => (b.content.length > a.content.length ? b : a));
}

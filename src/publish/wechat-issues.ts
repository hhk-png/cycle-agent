import { createHash } from 'node:crypto';
import type { Article } from './articles.ts';
import { mdToWechatHtml, WECHAT_CONTENT_LIMIT } from './markdown.ts';

/**
 * 把教程章节编排成「公众号期次」。
 *
 * **默认一章 = 一期。** 这里仍然保留着一套按 h2 边界拆篇的机制,但它是**护栏**,
 * 不是常规流程:它的上限取自 WECHAT_CONTENT_LIMIT(50 万字符),而 15 章里最长的一章
 * (18,附录C)渲染后才 18 万——正常情况永远不会触发。
 *
 * 为什么上限不再是 2 万:官方文档写 `draft/add` 的 content「必须少于2万字符」,
 * **实测不成立**。实测接口收下了 183,914 字符的正文并原样回读(结构标签 15 项全对上、
 * 纯文本一字不差),没有截断。详见 markdown.ts 里 WECHAT_CONTENT_LIMIT 的注释。
 * 早先按 2 万拆篇会把 15 章撑成 27 期,而那 27 期里没有一期是真需要的。
 *
 * 万一将来某章真的离谱到超护栏,拆篇规则(两条不可退让):
 *
 * 1. **只切在 h2 边界**,绝不按字符数硬切 —— 硬切会把代码块或表格劈成两半。
 *    找不到 h2 边界的超限篇会报错停住,而不是悄悄硬切。
 * 2. **切点按「转换后的字符数」找,不按 Markdown 长度。**
 *    表格小节转换后大约是源文的 2.5~3 倍,代码块小节约 1.3 倍 ——
 *    按 Markdown 长度对半切会让两个半篇一个超限一个很空。
 *
 * 切完还要把该半篇的 Markdown **重新整体转换一次**再量:
 * 各小节长度相加只是选切点用的近似值,是否真的合规必须以实际转换结果为准。
 */

/** 一期(公众号的一篇) */
export interface Issue {
  /** 期号:原篇编号;拆开的加 -1/-2,如 '18-1' */
  id: string;
  /** 原篇编号,如 '18' */
  no: string;
  /** 这是原篇的第几份(从 1 起) */
  part: number;
  /** 原篇拆成了几份 */
  parts: number;
  /** 公众号标题(≤32 字) */
  title: string;
  /** 摘要(同掘金那套,复用) */
  digest: string;
  /** 公众号正文 HTML,就是传给 draft/add 的 content */
  content: string;
  /** 正文 Markdown(落盘留档、排查用) */
  markdown: string;
  /** 正文 sha256,用于发现「草稿建好后正文又被改过」 */
  contentHash: string;
  /** 本期适用的字符上限(可能比接口上限更小,见 IssueOptions.contentLimit) */
  limit: number;
  /** 全局期次序号(从 1 起),排期按它走 */
  order: number;
  /** 发布前该让用户看到的提醒 */
  warnings: string[];
}

/** 单篇最多拆成几份 —— 再多说明这篇不该走公众号,应该报错让人看一眼 */
const MAX_PARTS = 4;

/** 标题上限:接口 32 字,编辑器 64 字。卡到 32 就给警告(可能被截) */
const TITLE_LIMIT = 32;

/**
 * 找 h2 行号(0 基)。
 * 与 articles.ts 的 findTitleLine 同一套围栏感知逻辑 —— 代码块里的
 * `## 注释` 不是标题,漏了这个判断就会把代码块从中间切开。
 */
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

/** 按 h2 边界把正文切成小节(第一段是第一个 h2 之前的内容,可能为空) */
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

/** 一个可独立搬运的小节 */
interface Chunk {
  md: string;
  /** 单独转换后的字符数,只用来**选切点** */
  htmlLen: number;
}

/** 与最终转换结果保持一致的口径:HTML 源码长度(UTF-16 码元) */
export function measure(markdown: string): number {
  return mdToWechatHtml(markdown).length;
}

const joinChunks = (chunks: Chunk[]): string => chunks.map((c) => c.md).join('\n\n');

/**
 * 把 n 个小节切成 k 份,让**最长的那份尽量短**(经典 minimax 划分),返回切点下标。
 *
 * 为什么不是简单的「对半切」:对半切出来的是「均衡的份数」,但约束是
 * 「每份都不超限」—— 第 18 章按对半切得到 7.7k/14.5k/5.9k/14.4k,
 * 最长那份离上限很近而其它份很空。minimax 在同为 4 份时能给出 10k 上下的一组分法。
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
 * 把一篇正文切成若干期。
 *
 * 份数取「能满足上限的最少份数」,该份数下再用 minimax 求最均衡的切法。
 * 返回 `{ error }` 表示「超限了但切不动」—— 调用方必须停下来报错,绝不硬切。
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
        `正文 ${whole} 字符超过上限 ${limit},但这篇没有 h2 小节可切。\n` +
        `  不做硬切(会把代码块/表格劈成两半),请先给它加上二级标题再发。`,
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

    // 选切点用的是各小节长度之和;是否真的合规以「整份重新转换」为准
    const tooBig = parts
      .map((p, i) => ({ i, len: measure(p) }))
      .find((x) => x.len > limit);
    if (!tooBig) {
      if (parts.some((p) => !p.trim())) return { error: '拆分产生了空的正文章节,请检查标题层级。' };
      return { parts };
    }
    lastError = `第 ${tooBig.i + 1}/${k} 份仍有 ${tooBig.len} 字符`;
  }

  return {
    error:
      `${whole} 字符超限,拆到 ${MAX_PARTS} 份仍不合规(${lastError})。\n` +
      `  多半是某个 h2 小节本身就超限了 —— 需要人工把它再拆出下级标题。`,
  };
}

/** 拆分后的标题后缀:两份用上下,三份以上用一二三 */
function partSuffix(part: number, parts: number): string {
  if (parts === 1) return '';
  if (parts === 2) return part === 1 ? '(上)' : '(下)';
  const labels = ['一', '二', '三', '四'];
  return `(${labels[part - 1] ?? String(part)})`;
}

/** 摘要后缀,与标题保持一致的口径 */
function digestSuffix(part: number, parts: number): string {
  if (parts === 1) return '';
  if (parts === 2) return part === 1 ? '（上）' : '（下）';
  const labels = ['一', '二', '三', '四'];
  return `（${labels[part - 1] ?? String(part)}）`;
}

/** 编排期次的可选项 */
export interface IssueOptions {
  /** 摘要 digest,key = 两位编号(与掘金配置同一份) */
  digests: Record<string, string>;
  /** 个别篇的标题覆盖 —— 微信标题上限 32 字,超长篇拆开后容易顶到线上 */
  titleOverrides?: Record<string, string>;
  /**
   * 拆篇时用的**生效上限**,默认取 WECHAT_CONTENT_LIMIT(50 万,是个护栏而
   * 不是接口上限 —— 见 markdown.ts 的说明)。正常情况一章 = 一期,不会触发拆分。
   */
  contentLimit?: number;
}

/**
 * 编排全部期次。摘要是两位编号的 key(与掘金配置同一份),
 * 拆开的期共用原篇摘要,只加「（上）/（下）」前缀。
 */
export function buildIssues(
  articles: Article[],
  opts: IssueOptions,
): { issues: Issue[] } | { error: string } {
  const limit = opts.contentLimit ?? WECHAT_CONTENT_LIMIT;
  const issues: Issue[] = [];
  for (const a of articles) {
    const split = splitArticle(a.content, limit);
    if ('error' in split) return { error: `第 ${a.no} 篇: ${split.error}` };

    const baseTitle = opts.titleOverrides?.[a.no] ?? a.title;
    const brief = opts.digests[a.no] ?? '';
    const warnings: string[] = [];
    if (!brief) warnings.push('没有摘要,digest 会由公众号按正文前 54 字自动生成');
    if (brief.length > 120) warnings.push(`摘要 ${brief.length} 字,超过公众号 120 字上限`);

    for (const [i, md] of split.parts.entries()) {
      const part = i + 1;
      const parts = split.parts.length;
      const title = `${baseTitle}${partSuffix(part, parts)}`;
      if (title.length > TITLE_LIMIT) {
        warnings.push(`标题 ${title.length} 字,超过接口上限 ${TITLE_LIMIT}(会被拒或截断): ${title}`);
      } else if (title.length >= TITLE_LIMIT) {
        warnings.push(`标题正好 ${title.length} 字,卡在上限上,建议缩短`);
      }

      const content = mdToWechatHtml(md);
      const own = [...warnings];
      // 只在第一份上提一次标题被覆盖的事,不然一篇拆 3 份就刷 3 遍
      if (part === 1 && opts.titleOverrides?.[a.no]) {
        own.push(`标题按配置覆盖为「${baseTitle}」(原文件名 ${a.title.length} 字)`);
      }
      issues.push({
        id: parts === 1 ? a.no : `${a.no}-${part}`,
        no: a.no,
        part,
        parts,
        title,
        digest: brief ? `${digestSuffix(part, parts)}${brief}` : '',
        content,
        markdown: md,
        // 哈希**渲染后的 HTML**而不是 markdown 源:`contentHash` 的用途是回答
        // 「后台那份草稿和这个工具现在会产出的东西还一样吗」,而渲染器(markdown.ts)
        // 也是产出的一部分 —— 只哈希 md 的话,改了样式常量、换了转义方式、
        // 修了渲染 bug,工具都会认为草稿仍然是最新的,于是**静默地不重建**。
        // (实测踩过:把 <input> 换成 ☐ 之后,27 期的哈希全都不变。)
        contentHash: createHash('sha256').update(`${title}\n${content}`).digest('hex'),
        limit,
        order: 0, // 下面统一编号
        warnings: own,
      });
    }
  }

  for (const [i, issue] of issues.entries()) issue.order = i + 1;
  return { issues };
}

/** 全部期次的最长正文(用于 --list 的一行汇总) */
export function longestIssue(issues: Issue[]): Issue {
  return issues.reduce((a, b) => (b.content.length > a.content.length ? b : a));
}

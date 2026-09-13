import { createHash } from 'node:crypto';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';

/**
 * 扫描教程目录、把 Markdown 拆成「标题 + 正文」,并做摘要字数校验。
 *
 * ⚠️ 本文件是整套流程里**唯一能静默摧毁内容**的地方,改动前请先读这一段。
 *
 * 教程正文的代码块里有大量以 `# ` 开头的行(bash 注释,如 `# 启动服务`),
 * 实测 14 篇中有 1~18 处(第 20 章有 18 处)。任何「删掉所有 `# ` 行」的写法
 * 都会把这些代码示例一起删掉。所以取标题**必须用围栏感知的 findTitleLine**。
 */

/** 摘要字数下限(按码点计) */
export const BRIEF_MIN = 50;
/** 摘要字数上限(按 UTF-16 码元计) */
export const BRIEF_MAX = 100;
/** 建议区间:留出余量,避免踩边界 */
export const BRIEF_SAFE_MIN = 60;
export const BRIEF_SAFE_MAX = 90;

/** 一篇待发布的文章 */
export interface Article {
  /** 两位编号,如 '09' */
  no: string;
  /** 数值编号,用于排序 */
  order: number;
  /** 文件名(不含目录) */
  fileName: string;
  /** 绝对路径 */
  filePath: string;
  /** 发布用的标题。来源由 TitleSource 决定(原文 H1 或文件名) */
  title: string;
  /** 原文 H1 标题,逐字取自源文件(去掉 `# ` 前缀);不受 titleSource 影响 */
  h1Title: string;
  /** 文件名去掉 `.md`,如 `vllm教程-09-量化` */
  fileNameTitle: string;
  /** Markdown 正文:去掉 H1 行,保留开头「仓库地址」行 */
  content: string;
  /** 标题+正文的 sha256,用于发现「草稿建好后正文又被改过」 */
  contentHash: string;
}

/** 去掉 BOM、把 CRLF 归一成 LF —— Windows 仓库里不做这步,标题会带一个尾随 \r */
function normalize(raw: string): string {
  return raw.replace(/^﻿/, '').replace(/\r\n/g, '\n');
}

/**
 * 找出真正的一级标题行号(0 基),跳过 ``` / ~~~ 围栏内的 `# 注释`。
 * 找不到返回 -1。
 */
export function findTitleLine(lines: string[]): number {
  let fence: string | null = null;
  for (let i = 0; i < lines.length; i++) {
    const m = /^\s*(```|~~~)/.exec(lines[i]);
    if (m) {
      if (fence === null) fence = m[1];
      else if (m[1] === fence) fence = null;
      continue;
    }
    if (fence === null && /^# /.test(lines[i])) return i;
  }
  return -1;
}

/**
 * 取章节引言(教程的「> 本章目标：…」),供 --suggest-briefs 出素材。
 *
 * 只在 **H1 之后、第一个二级标题之前** 找引用块 —— 正文中段的提示框
 * (如「注：…」「关键：…」)不是引言,当成素材会误导。09/10/11 三章
 * 直接以 `## x.1` 开头,没有引言,这里返回空串是对的。
 */
export function extractIntro(content: string): string {
  const scopable = content.split('\n');
  const end = scopable.findIndex((l) => /^## /.test(l));
  const region = end >= 0 ? scopable.slice(0, end) : scopable;
  const line = region.find((l) => /^>\s*\S/.test(l));
  return line ? line.replace(/^>\s*/, '').trim() : '';
}

/** 摘要字数:码点与 UTF-16 码元两种口径 */
export function countBrief(brief: string): { cp: number; u16: number } {
  const s = brief.trim();
  return { cp: [...s].length, u16: s.length };
}

/**
 * 校验摘要,返回错误描述或 null。
 *
 * 掘金后端是 Java,`@Length(min=50,max=100)` 与 JS 的 str.length 一样数 UTF-16 码元;
 * 若服务端用码点数则 [...s].length 才准。纯中文两者相等,emoji/生僻字才分叉。
 * 稳健规则:**下限按码点、上限按 UTF-16 码元**(码点 ≤ 码元,两个方向都安全)。
 */
export function briefProblem(brief: string): string | null {
  const s = brief.trim();
  if (!s) return '摘要为空';
  if (/[\r\n]/.test(s)) return '摘要必须是单行(掘金按纯文本展示,换行会被压掉)';
  const { cp, u16 } = countBrief(s);
  if (cp < BRIEF_MIN) return `摘要 ${cp} 字(码点) 少于 ${BRIEF_MIN},掘金会拒绝`;
  if (u16 > BRIEF_MAX) return `摘要 ${u16} 字符(UTF-16) 超过 ${BRIEF_MAX},掘金会拒绝`;
  return null;
}

/** 是否贴近边界(软提示,不是硬拦) */
export function briefTight(brief: string): boolean {
  const { cp, u16 } = countBrief(brief);
  return cp < BRIEF_SAFE_MIN || u16 > BRIEF_SAFE_MAX;
}

/** 摘要里是否有 markdown 标记(掘金按纯文本展示,只是警告) */
export function briefHasMarkdown(brief: string): boolean {
  return /[*`#>]/.test(brief);
}

/** 把前缀里的正则元字符转义,避免配置里的前缀写法炸掉匹配 */
function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** 解析后的源文件 */
interface SourceFile {
  no: string;
  order: number;
  fileName: string;
  text: string;
}

/** 扫目录,收集 <前缀>-<编号>-<标题>.md 形式的文件 */
function scanSourceDir(dir: string, filePrefix: string): { files: SourceFile[] } | { error: string } {
  if (!existsSync(dir)) return { error: `源目录不存在: ${dir}` };
  const re = new RegExp(`^${escapeRegExp(filePrefix)}-(\\d+)-.+\\.md$`);
  const files: SourceFile[] = [];
  for (const fileName of readdirSync(dir)) {
    const m = re.exec(fileName);
    if (!m) continue;
    files.push({
      no: m[1],
      order: Number(m[1]),
      fileName,
      text: normalize(readFileSync(path.join(dir, fileName), 'utf8')),
    });
  }
  if (files.length === 0) {
    return { error: `${dir} 下没有匹配 ${filePrefix}-<编号>-*.md 的文件` };
  }
  return { files };
}

/**
 * 标题取哪里:
 *  · `h1`       —— 源文件的一级标题,如 `# 09 · 量化（Quantization）`
 *  · `fileName` —— 文件名去掉 `.md`,如 `vllm教程-09-量化`
 *
 * 掘金上 00~07 章用的是 `fileName` 这种(系列名+编号+短标题),
 * 为了前后一致,09 章之后也走同一套命名。
 */
export type TitleSource = 'h1' | 'fileName';

/**
 * 载入编号 >= fromNumber 的全部文章,按编号数值升序。
 * 编号有重复(同名编号两个文件)时直接报错 —— 那会让幂等状态串台。
 */
export function loadArticles(
  sourceDir: string,
  filePrefix: string,
  fromNumber: number,
  titleSource: TitleSource = 'h1',
): { articles: Article[] } | { error: string } {
  const scanned = scanSourceDir(sourceDir, filePrefix);
  if ('error' in scanned) return scanned;

  const seen = new Map<string, SourceFile>();
  for (const f of scanned.files) {
    if (f.order < fromNumber) continue;
    const dup = seen.get(f.no);
    if (dup) {
      return { error: `编号 ${f.no} 匹配到多个文件: ${dup.fileName}、${f.fileName}` };
    }
    seen.set(f.no, f);
  }
  if (seen.size === 0) {
    return { error: `${sourceDir} 下没有编号 >= ${fromNumber} 的文件` };
  }

  const picked = [...seen.values()].sort((a, b) => a.order - b.order);

  // 连续性检查:fromNumber..max 之间不该缺号
  const max = picked[picked.length - 1].order;
  const missing: number[] = [];
  for (let n = fromNumber; n <= max; n++) {
    if (!picked.some((f) => f.order === n)) missing.push(n);
  }
  if (missing.length > 0) {
    return { error: `编号不连续,缺少: ${missing.join(', ')}` };
  }

  const articles: Article[] = [];
  for (const f of picked) {
    const lines = f.text.split('\n');
    const idx = findTitleLine(lines);
    if (idx < 0) return { error: `${f.fileName} 找不到一级标题(# )` };
    const h1Title = lines[idx].replace(/^# /, '').trim();
    const fileNameTitle = f.fileName.replace(/\.md$/, '');
    const title = titleSource === 'fileName' ? fileNameTitle : h1Title;

    // 只删「标题行 + 紧随的一个空行」,绝不全局压缩空行 —— 那会改动代码块内的内容
    const kept = lines.slice();
    kept.splice(idx, 1);
    if (kept[idx] === '') kept.splice(idx, 1);
    const content = kept.join('\n').trim();

    articles.push({
      no: f.no,
      order: f.order,
      fileName: f.fileName,
      filePath: path.join(sourceDir, f.fileName),
      title,
      h1Title,
      fileNameTitle,
      content,
      contentHash: createHash('sha256').update(`${title}\n${content}`).digest('hex'),
    });
  }
  return { articles };
}

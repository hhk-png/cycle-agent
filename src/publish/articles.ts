import { createHash } from 'node:crypto';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';

/**
 * Scans the tutorial directory, splits Markdown into "title + body", and validates brief length.
 *
 * ⚠️ The only place in the pipeline that can silently destroy content. Code blocks contain lines
 * starting with `# ` (bash comments; measured 1~18 per article), so title detection must use the
 * fence-aware findTitleLine.
 */

/** Minimum brief length (code points) */
export const BRIEF_MIN = 50;
/** Maximum brief length (UTF-16 code units) */
export const BRIEF_MAX = 100;
/** Recommended range, clear of the boundaries */
export const BRIEF_SAFE_MIN = 60;
export const BRIEF_SAFE_MAX = 90;

/** One article pending publication */
export interface Article {
  /** Two-digit number, e.g. '09' */
  no: string;
  /** Numeric number, used for sorting */
  order: number;
  /** File name (without directory) */
  fileName: string;
  /** Absolute path */
  filePath: string;
  /** Title used for publishing; source decided by TitleSource */
  title: string;
  /** Original H1, verbatim minus the `# ` prefix; unaffected by titleSource */
  h1Title: string;
  /** File name minus `.md`, e.g. `vllm教程-09-量化` */
  fileNameTitle: string;
  /** Markdown body: H1 line removed, leading "repository address" line kept */
  content: string;
  /** sha256 of title+body, to detect later body edits */
  contentHash: string;
}

/** Strip BOM and normalize CRLF to LF (without this, Windows titles carry a trailing \r) */
function normalize(raw: string): string {
  return raw.replace(/^﻿/, '').replace(/\r\n/g, '\n');
}

/** Finds the real H1 line index (0-based), skipping `# comments` in ``` / ~~~ fences; -1 if absent. */
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
 * Returns the chapter intro (the "> Chapter goals: …" blockquote) for --suggest-briefs. Only searches
 * after the H1 and before the first `## ` —— mid-body callouts ("Note: …") are not intros.
 */
export function extractIntro(content: string): string {
  const scopable = content.split('\n');
  const end = scopable.findIndex((l) => /^## /.test(l));
  const region = end >= 0 ? scopable.slice(0, end) : scopable;
  const line = region.find((l) => /^>\s*\S/.test(l));
  return line ? line.replace(/^>\s*/, '').trim() : '';
}

/** Brief length: code points and UTF-16 code units */
export function countBrief(brief: string): { cp: number; u16: number } {
  const s = brief.trim();
  return { cp: [...s].length, u16: s.length };
}

/**
 * Validates the brief; returns an error description or null.
 * Juejin's Java `@Length(min=50,max=100)` counts UTF-16 code units like JS str.length. Robust rule:
 * lower bound by code points, upper bound by UTF-16 code units.
 */
export function briefProblem(brief: string): string | null {
  const s = brief.trim();
  if (!s) return 'Brief is empty';
  if (/[\r\n]/.test(s)) return 'The brief must be a single line (Juejin displays it as plain text, line breaks get collapsed)';
  const { cp, u16 } = countBrief(s);
  if (cp < BRIEF_MIN) return `Brief is ${cp} characters (code points), fewer than ${BRIEF_MIN}; Juejin will reject it`;
  if (u16 > BRIEF_MAX) return `Brief is ${u16} characters (UTF-16), more than ${BRIEF_MAX}; Juejin will reject it`;
  return null;
}

/** Near the boundary (soft warning, not a hard block) */
export function briefTight(brief: string): boolean {
  const { cp, u16 } = countBrief(brief);
  return cp < BRIEF_SAFE_MIN || u16 > BRIEF_SAFE_MAX;
}

/** Whether the brief contains markdown markup (Juejin renders plain text; warning only) */
export function briefHasMarkdown(brief: string): boolean {
  return /[*`#>]/.test(brief);
}

/** Escape regex metacharacters in the prefix */
function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** A parsed source file */
interface SourceFile {
  no: string;
  order: number;
  fileName: string;
  text: string;
}

/**
 * Collects `<prefix>-<number>-<title>.md` files. `filePrefix` may be empty (e.g. ai-agent-toturial's
 * `00-前言与导读.md`); an empty prefix scans only the top-level directory.
 */
function scanSourceDir(dir: string, filePrefix: string): { files: SourceFile[] } | { error: string } {
  if (!existsSync(dir)) return { error: `Source directory does not exist: ${dir}` };
  const re = filePrefix
    ? new RegExp(`^${escapeRegExp(filePrefix)}-(\\d+)-.+\\.md$`)
    : new RegExp(`^(\\d+)-.+\\.md$`);
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
    return { error: `No files matching ${filePrefix ? `${filePrefix}-` : ''}<number>-*.md found under ${dir}` };
  }
  return { files };
}

/**
 * Where the title comes from: `h1` (the source's H1) or `fileName` (file name minus `.md`).
 * Juejin chapters 00~07 use `fileName` (series + number + short title); from 09 on, the same scheme is
 * kept for consistency.
 */
export type TitleSource = 'h1' | 'fileName';

/**
 * Loads all articles with number >= fromNumber, ascending. Duplicate numbers are a hard error (they
 * would cross-wire the idempotent state).
 *
 * `titlePrefix` applies only with `titleSource: 'fileName'`, adding a series-name prefix
 * (ai-agent-toturial's `00-前言与导读` → `ai-agent教程-00-前言与导读`). Chosen over
 * renaming files because the README's 23 chapter links are hard-coded by file name.
 */
export function loadArticles(
  sourceDir: string,
  filePrefix: string,
  fromNumber: number,
  titleSource: TitleSource = 'h1',
  titlePrefix = '',
): { articles: Article[] } | { error: string } {
  const scanned = scanSourceDir(sourceDir, filePrefix);
  if ('error' in scanned) return scanned;

  const seen = new Map<string, SourceFile>();
  for (const f of scanned.files) {
    if (f.order < fromNumber) continue;
    const dup = seen.get(f.no);
    if (dup) {
      return { error: `Number ${f.no} matches multiple files: ${dup.fileName}, ${f.fileName}` };
    }
    seen.set(f.no, f);
  }
  if (seen.size === 0) {
    return { error: `No files with number >= ${fromNumber} found under ${sourceDir}` };
  }

  const picked = [...seen.values()].sort((a, b) => a.order - b.order);

  // Continuity check: there should be no gaps between fromNumber..max
  const max = picked[picked.length - 1].order;
  const missing: number[] = [];
  for (let n = fromNumber; n <= max; n++) {
    if (!picked.some((f) => f.order === n)) missing.push(n);
  }
  if (missing.length > 0) {
    return { error: `Numbers are not contiguous, missing: ${missing.join(', ')}` };
  }

  const articles: Article[] = [];
  for (const f of picked) {
    const lines = f.text.split('\n');
    const idx = findTitleLine(lines);
    if (idx < 0) return { error: `${f.fileName} has no H1 title (# )` };
    const h1Title = lines[idx].replace(/^# /, '').trim();
    const fileNameTitle = f.fileName.replace(/\.md$/, '');
    const title = titleSource === 'fileName' ? `${titlePrefix}${fileNameTitle}` : h1Title;

    // Remove only the title line and its one trailing blank line; never collapse blank lines globally (it would alter code blocks)
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

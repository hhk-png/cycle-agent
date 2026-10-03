import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { createContext, runInContext } from 'node:vm';
import { fileURLToPath } from 'node:url';

/**
 * Markdown → WeChat-compatible HTML.
 *
 * Rendering lives in `src/publish/convert-to-wechat.js`; this file loads that script as a function
 * library, fixes one code-block style rule, and guards against deformed output.
 *
 * ⚠️ The old `WECHAT_CONTENT_LIMIT = 20000` ("docs say content must be under 20k chars") is false:
 * `draft/add` accepted a 183,914-character body and read it back verbatim. See WECHAT_CONTENT_LIMIT.
 */

/** Escape HTML text nodes; `&` must be replaced first or it re-escapes the later entities */
export function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ============ Load the standalone script as a function library ============

interface ConvertedStyles {
  wrapper: string;
  [k: string]: string;
}

interface Converter {
  parseMarkdown(md: string): string;
  buildPage(title: string, bodyHtml: string): string;
  WECHAT_CSS: ConvertedStyles;
}

let cached: Converter | null = null;

/**
 * Loads `src/publish/convert-to-wechat.js` via `vm` rather than import/require: it's a hand-run CLI with
 * no `module.exports` that calls `main()` unconditionally, so importing it would trigger side effects.
 * We cut the main() call and append a `var` to expose the style constants on the sandbox global
 * (top-level `const` isn't a vm context property; `var` is).
 */
function loadConverter(): Converter {
  if (cached) return cached;

  const file = fileURLToPath(new URL('./convert-to-wechat.js', import.meta.url));
  const source = readFileSync(file, 'utf8')
    .replace(/\nmain\(\);\s*$/, '\n')
    .concat('\nvar __WECHAT_CSS = WECHAT_CSS;\n');

  // This module is ESM (no `require`); the script is CommonJS. createRequire gives it one so its
  // internal `require("fs")`/`require("path")` resolve.
  const require = createRequire(import.meta.url);

  const sandbox: Record<string, unknown> = {
    module: { exports: {} },
    exports: {},
    require,
    console,
    process,
    Buffer,
    __dirname: fileURLToPath(new URL('.', import.meta.url)),
    __filename: file,
  };
  sandbox.global = sandbox;
  createContext(sandbox);
  runInContext(source, sandbox);

  const parseMarkdown = sandbox.parseMarkdown;
  const buildPage = sandbox.buildPage;
  const styles = sandbox.__WECHAT_CSS;
  if (typeof parseMarkdown !== 'function' || typeof buildPage !== 'function' || !styles) {
    throw new Error(`Unexpected load result from ${file}: parseMarkdown/buildPage/WECHAT_CSS is missing`);
  }
  cached = {
    parseMarkdown: parseMarkdown as Converter['parseMarkdown'],
    buildPage: buildPage as Converter['buildPage'],
    WECHAT_CSS: styles as ConvertedStyles,
  };
  return cached;
}

// ============ Style fixes ============

/**
 * Code-block long lines must wrap: the script's `white-space: pre` + `overflow-x: auto` scrolls in a
 * browser but not in the WeChat body, where over-width lines are truncated outright. `white-space: pre`
 * occurs only in this spot of the output, so the replacement is exact; `word-break: break-all` is needed
 * because the script turns spaces into `&nbsp;`, which `pre-wrap` alone can't break.
 */
function fixCodeBlockWrapping(html: string): string {
  return html.replace(/white-space:\s*pre;/g, 'white-space: pre-wrap; word-break: break-all;');
}

/** The official account strips these tags; if they appear, the renderer is buggy, so fail here. */
function assertNoStrippedTags(html: string): string {
  for (const tag of ['style', 'script', 'iframe', 'input']) {
    if (new RegExp(`<${tag}[\\s>]`, 'i').test(html)) {
      throw new Error(`The conversion result contains <${tag}>, which the official account will strip —— the renderer has a bug`);
    }
  }
  return html;
}

/**
 * Converts Markdown into official-account body HTML. Lossless; length validation is the caller's job.
 */
export function mdToWechatHtml(markdown: string): string {
  const conv = loadConverter();
  const body = fixCodeBlockWrapping(conv.parseMarkdown(markdown));
  // Outer container carries the script's document typography, matching its buildPage preview.
  const html = `<div style="${conv.WECHAT_CSS.wrapper}">${body}</div>`;
  return assertNoStrippedTags(html);
}

/**
 * Split guardrail —— not the API's real limit: `draft/add` accepted 183,914 characters, and the longest
 * chapter renders to exactly that. So this sits far above actual need and never triggers a split in
 * practice, but still catches an absurdly large chapter instead of failing silently.
 */
export const WECHAT_CONTENT_LIMIT = 500_000;

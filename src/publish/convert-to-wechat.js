/**
 * Converts Markdown files to WeChat-editor-compatible HTML with code highlighting.
 * Usage: node convert-to-wechat.js  →  ./wechat-formatted/
 */

const fs = require("fs");
const path = require("path");

// ── Config ──────────────────────────────────────────────
const INPUT_DIR = __dirname;
const OUTPUT_DIR = path.join(__dirname, "wechat-formatted");
// Filename filter: skips the Chinese "article summary" helper file
const EXCLUDE_PATTERN = "文章摘要";

// ── WeChat-compatible CSS styles ──────────────────────────
const WECHAT_CSS = {
  wrapper: 'font-family: -apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Hiragino Sans GB, Microsoft YaHei, Helvetica Neue, Helvetica, Arial, sans-serif; font-size: 16px; color: #3f3f3f; line-height: 1.8; letter-spacing: 0.5px; word-break: break-word;',

  h1: "font-size: 22px; font-weight: bold; color: #2c3e50; text-align: center; margin: 20px 0 16px 0; padding-bottom: 12px; border-bottom: 2px solid #3498db; line-height: 1.4;",
  h2: "font-size: 20px; font-weight: bold; color: #2c3e50; margin: 28px 0 14px 0; padding-left: 12px; border-left: 4px solid #3498db; line-height: 1.4;",
  h3: "font-size: 18px; font-weight: bold; color: #34495e; margin: 22px 0 10px 0; line-height: 1.4;",
  h4: "font-size: 16px; font-weight: bold; color: #555; margin: 16px 0 8px 0; line-height: 1.4;",

  p: "margin: 0 0 12px 0; text-align: justify;",
  strong: "font-weight: bold; color: #2c3e50;",
  em: "font-style: italic; color: #555;",
  inlineCode: "background-color: #f6f6f6; color: #d14; padding: 2px 6px; border-radius: 3px; font-family: Consolas, Menlo, Monaco, Courier New, monospace; font-size: 14px; letter-spacing: 0;",

  pre: "background-color: #0d1117; padding: 16px 18px; border-radius: 6px; overflow-x: auto; font-size: 13px; line-height: 1.75; margin: 14px 0; white-space: pre; -webkit-font-smoothing: antialiased;",
  preCode: "font-family: Consolas, Menlo, Monaco, Courier New, monospace; font-size: 13px; background: transparent; color: white !important; padding: 0;",

  blockquote: "border-left: 4px solid #3498db; background-color: #f0f7fb; padding: 12px 16px; margin: 14px 0; color: #555; border-radius: 0 4px 4px 0;",

  table: "border-collapse: collapse; width: 100%; margin: 14px 0; font-size: 14px; table-layout: auto;",
  th: "background-color: #3498db; color: white; padding: 10px 12px; border: 1px solid #2980b9; font-weight: bold; text-align: left;",
  td: "padding: 8px 12px; border: 1px solid #ddd;",
  trEven: "background-color: #f9f9f9;",
  trOdd: "background-color: #fff;",

  ul: "margin: 10px 0; padding-left: 24px;",
  ol: "margin: 10px 0; padding-left: 24px;",
  li: "margin: 4px 0;",
  hr: "border: 0; border-top: 1px solid #e0e0e0; margin: 24px 0;",
  a: "color: #3498db; text-decoration: none; word-break: break-all;",
  del: "text-decoration: line-through; color: #999;",
};

// ══════════════════════════════════════════════════════════
//  Syntax highlighting system (WeChat-compatible — uses font tags instead of span+style)
// ══════════════════════════════════════════════════════════

// Color scheme — background #0d1117, optimized for WeChat public account HTML rendering
// Principle: many colors, but all on "short, scattered" tokens, avoiding large blocks of one color.
const HL = {
  keyword:  "#ff7b72",   // keyword — red (bold)
  control:  "#ff7b72",   // control flow — red
  string:   "#a5d6ff",   // short string — light blue
  comment:  "#a8b3c2",   // comment — light gray (italic)
  number:   "#79c0ff",   // number — blue
  literal:  "#79c0ff",   // literal — blue
  func:     "#d8b4fe",   // function call — bright purple
  type:     "#86efac",   // type/class name — bright green
  operator: "#ffb86c",   // operator — orange
  builtin:  "#e3b341",   // built-in object/command — gold
  var:      "#56d8c4",   // variable ($x) — cyan
  decorator:"#ff9f43",   // decorator @xxx — dark orange
  regex:    "#a5d6ff",   // regex — light blue
  constant: "#79c0ff",   // constant/option — blue
  meta:     "#ff9f43",   // decorator — dark orange
};

// Additional text style tags
const HL_TAG = {
  keyword: "b",
  comment: "i",
};

// Wrap with a font tag (WeChat-compatible, avoids style="..." being taken apart)
function wrapHL(text, colorKey) {
  const color = HL[colorKey] || "white";
  const tag = HL_TAG[colorKey];
  let html = `<font color="${color}">${text}</font>`;
  if (tag === "b") html = `<b>${html}</b>`;
  if (tag === "i") html = `<i>${html}</i>`;
  return html;
}

// Single pass: collect all region/token matches, then greedily merge by start position
// and priority, so each character is colored at most once (no double-wrap/overlap).
// Regions (strings/comments) rank before tokens, masking keywords inside them; no
// placeholder characters to be swallowed by later regexes.
function highlightCode(rawCode, lang) {
  if (!rawCode || rawCode.trim() === "") return "";

  const def = getLangDef(lang);
  if (!def) return escapeHtml(rawCode); // unknown language, escape only

  // Merge all rules: regions first (high priority), tokens after in order
  const rules = [];
  for (const r of def.regions) rules.push({ re: r.re, colorKey: r.colorKey, ok: r.ok, order: rules.length });
  for (const t of def.tokens) rules.push({ re: t.re, colorKey: t.colorKey, ok: t.ok, order: rules.length });

  // ── Collect matches for all rules ──────────────────────────
  const events = [];
  for (const rule of rules) {
    rule.re.lastIndex = 0;
    let m;
    while ((m = rule.re.exec(rawCode)) !== null) {
      if (m[0].length === 0) { rule.re.lastIndex++; continue; } // avoid infinite loop
      if (rule.ok && !rule.ok(m[0])) continue; // length/newline filter
      events.push({ start: m.index, end: m.index + m[0].length, rule });
    }
  }

  // Sort by start position; for the same start position take the earlier one by priority (array order)
  events.sort((a, b) => a.start - b.start || a.rule.order - b.rule.order);

  // Greedy merge: prefer the earliest start, and for the same start the higher priority; skip all overlapping intervals
  const spans = [];
  let curEnd = 0;
  for (const ev of events) {
    if (ev.start < curEnd) continue; // overlaps an already accepted interval → discard
    if (ev.end <= ev.start) continue;
    spans.push(ev);
    curEnd = ev.end;
  }

  // ── Assemble HTML: escape verbatim outside intervals, wrap by color inside ──
  const out = [];
  let pos = 0;
  for (const sp of spans) {
    if (sp.start > pos) out.push(escapeHtml(rawCode.slice(pos, sp.start)));
    const t = escapeHtml(rawCode.slice(sp.start, sp.end));
    out.push(sp.rule.colorKey ? wrapHL(t, sp.rule.colorKey) : t); // colorKey===null → mask as plain text
    pos = sp.end;
  }
  if (pos < rawCode.length) out.push(escapeHtml(rawCode.slice(pos)));
  return out.join("");
}

// ── Language definitions ──────────────────────────────────────────

// Color only short keywords/literals/numbers/calls, never whole strings or command
// lists, to avoid large blocks of one color. Comments get their own gray.

const RE = {
  // comments
  blockComment: /\/\*[\s\S]*?\*\//g,
  lineComment: /\/\/.*/g,
  hashComment: /#.*/g,
  // string delimiters (paired with the "short string" filter, colored only when short and newline-free)
  dq: /"(?:[^"\\\n]|\\.)*"/g,
  sq: /'(?:[^'\\\n]|\\.)*'/g,
  tick: /`(?:[^`\\\n]|\\.)*`/g,
  tripleDQ: /"""[\s\S]*?"""/g,
  tripleSQ: /'''[\s\S]*?'''/g,
  // numbers
  jsNumber: /\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?n?\b/g,
  plainNumber: /\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b/g,
  pyNumber: /\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?j?\b/g,
  intNumber: /\b\d+\b/g,
  rustNumber: /\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?(?:i8|i16|i32|i64|u8|u16|u32|u64|f32|f64|usize|isize)?\b/g,
  goNumber: /\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?i?\b/g,
  // other tokens
  funcIdent: /\b([a-zA-Z_$][a-zA-Z0-9_$]*)(?=\s*\()/g,
  pyFuncIdent: /\b([a-zA-Z_][a-zA-Z0-9_]*)(?=\s*\()/g,
  typeIdent: /\b[A-Z][A-Za-z0-9_]*\b/g,
  operator: /[+\-*/%=!&|<>^~?]+/g,
  decorator: /@[A-Za-z_][A-Za-z0-9_]*/g,
  varBash: /\$[A-Za-z_][A-Za-z0-9_]*|\$\{[^}]+\}/g,
};

function escapeRe(str) {
  return str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function makeKeywordRe(wordList) {
  return new RegExp(`\\b(?:${wordList.map(escapeRe).join("|")})\\b`, "g");
}

function makeWordRe(wordList) {
  return new RegExp(`\\b(?:${wordList.map(escapeRe).join("|")})\\b`, "g");
}

// Build a language definition: regions (comments + long strings to mask) and short tokens.
// Strings are colored only if <=24 chars and newline-free; longer ones are masked as
// plain text so string highlighting never becomes a block of one color.
function makeLang({
  comments = [],            // comment regexes (gray italic)
  plainRegions = [],        // regexes to mask but not color (e.g. long template strings/triple quotes)
  keywords = [],
  literals = [],
  builtinRe = null,         // built-in objects/commands (gold)
  varRe = null,             // variables (cyan)
  decoratorRe = null,       // decorators (dark orange)
  stringRe = [],            // string delimiter regexes (short → color, long → mask)
  typeRe = null,            // type/class names (green)
  numberRe = RE.plainNumber,
  operatorRe = null,        // operators (orange)
  funcRe = null,            // function calls (purple)
}) {
  const strRes = (Array.isArray(stringRe) ? stringRe : [stringRe]).filter(Boolean);
  const longOk = (s) => s.length > 24 || s.includes("\n");
  const shortOk = (s) => s.length <= 24 && !s.includes("\n");

  const regions = [];
  for (const re of comments) regions.push({ re, colorKey: "comment" });
  for (const re of plainRegions) regions.push({ re, colorKey: null });
  for (const re of strRes) regions.push({ re, colorKey: null, ok: longOk });

  const tokens = [];
  if (keywords.length) tokens.push({ re: makeKeywordRe(keywords), colorKey: "keyword" });
  if (literals.length) tokens.push({ re: makeKeywordRe(literals), colorKey: "literal" });
  if (builtinRe) tokens.push({ re: builtinRe, colorKey: "builtin" });
  if (varRe) tokens.push({ re: varRe, colorKey: "var" });
  if (decoratorRe) tokens.push({ re: decoratorRe, colorKey: "decorator" });
  for (const re of strRes) tokens.push({ re, colorKey: "string", ok: shortOk });
  if (typeRe) tokens.push({ re: typeRe, colorKey: "type" });
  tokens.push({ re: numberRe, colorKey: "number" });
  if (operatorRe) tokens.push({ re: operatorRe, colorKey: "operator" });
  if (funcRe) tokens.push({ re: funcRe, colorKey: "func" });

  return { regions, tokens };
}

const LANG_DEFS = {};

// ── TypeScript / JavaScript ──────────────────────────
LANG_DEFS.typescript = LANG_DEFS.javascript = LANG_DEFS.ts = LANG_DEFS.js = LANG_DEFS.tsx = LANG_DEFS.jsx = makeLang({
  comments: [RE.blockComment, RE.lineComment],
  plainRegions: [RE.tick], // mask template strings
  stringRe: [RE.dq, RE.sq],
  numberRe: RE.jsNumber,
  funcRe: RE.funcIdent,
  typeRe: RE.typeIdent,
  operatorRe: RE.operator,
  decoratorRe: RE.decorator,
  builtinRe: makeWordRe([
    "console","Math","JSON","Promise","Array","Object","String","Number","Boolean","Map","Set",
    "Symbol","Error","TypeError","Date","RegExp","parseInt","parseFloat","isNaN","setTimeout",
    "setInterval","clearTimeout","clearInterval","process","Buffer","globalThis",
  ]),
  keywords: [
    "const","let","var","function","class","interface","type","enum","implements","extends",
    "abstract","private","public","protected","readonly","static","namespace","module","declare",
    "keyof","typeof","infer","is","as","satisfies",
    "if","else","for","while","do","switch","case","break","continue","return","yield","await",
    "async","try","catch","finally","throw","new","delete","instanceof","in","of",
    "import","export","default","from","require",
  ],
  literals: ["true","false","null","undefined","this","super","void","never","unknown","any"],
});

// ── Bash / Shell ─────────────────────────────────────
LANG_DEFS.bash = LANG_DEFS.sh = LANG_DEFS.shell = LANG_DEFS.zsh = makeLang({
  comments: [RE.hashComment],
  stringRe: [RE.dq, RE.sq],
  numberRe: RE.intNumber,
  varRe: RE.varBash,
  operatorRe: RE.operator,
  builtinRe: makeWordRe([
    "cd","ls","pwd","mkdir","rm","cp","mv","cat","head","tail","touch","echo","printf","export",
    "unset","source","alias","grep","find","sed","awk","sort","uniq","wc","curl","wget","make",
    "git","npm","npx","node","python","python3","pip","docker","kubectl","ssh","tar","zip",
    "unzip","man","clear","exit",
  ]),
  keywords: [
    "if","then","else","elif","fi","for","while","until","do","done","case","esac","in",
    "function","return","exit","break","continue","local","readonly","declare","select",
  ],
  literals: ["true","false"],
});

// ── JSON ─────────────────────────────────────────────
LANG_DEFS.json = LANG_DEFS.jsonc = makeLang({
  stringRe: [RE.dq],
  numberRe: RE.plainNumber,
  literals: ["true","false","null"],
});

// ── Python ───────────────────────────────────────────
LANG_DEFS.python = LANG_DEFS.py = makeLang({
  comments: [RE.hashComment],
  plainRegions: [RE.tripleDQ, RE.tripleSQ], // mask triple-quoted docstrings/long strings
  stringRe: [RE.dq, RE.sq],
  numberRe: RE.pyNumber,
  funcRe: RE.pyFuncIdent,
  typeRe: RE.typeIdent,
  operatorRe: RE.operator,
  decoratorRe: RE.decorator,
  builtinRe: makeWordRe([
    "print","len","range","enumerate","zip","map","filter","sorted","reversed","type",
    "isinstance","hasattr","getattr","int","str","float","bool","list","dict","tuple","set",
    "open","input","abs","all","any","min","max","sum","round",
  ]),
  keywords: [
    "def","class","if","elif","else","for","while","try","except","finally","with","as",
    "import","from","return","yield","raise","break","continue","global","nonlocal","lambda",
    "pass","del","assert","and","or","not","in","is","async","await",
  ],
  literals: ["True","False","None","self","cls"],
});

// ── Rust ─────────────────────────────────────────────
LANG_DEFS.rust = LANG_DEFS.rs = makeLang({
  comments: [RE.blockComment, RE.lineComment],
  stringRe: [RE.dq], // single quotes are char/lifetime, skip
  numberRe: RE.rustNumber,
  funcRe: RE.pyFuncIdent,
  typeRe: RE.typeIdent,
  operatorRe: RE.operator,
  builtinRe: makeWordRe([
    "panic","assert","assert_eq","assert_ne","todo","unimplemented","unreachable",
    "println","print","eprintln","eprint","dbg","write","writeln",
  ]),
  keywords: [
    "fn","let","mut","const","static","struct","enum","trait","impl","mod","use","pub","crate",
    "self","Self","super","where","for","while","loop","if","else","match","break","continue",
    "return","in","ref","move","async","await","unsafe","extern","type","dyn","as","box","macro",
    "true","false",
  ],
});

// ── Go ───────────────────────────────────────────────
LANG_DEFS.go = LANG_DEFS.golang = makeLang({
  comments: [RE.blockComment, RE.lineComment],
  plainRegions: [RE.tick], // mask backtick raw strings
  stringRe: [RE.dq],
  numberRe: RE.goNumber,
  funcRe: RE.pyFuncIdent,
  typeRe: RE.typeIdent,
  operatorRe: RE.operator,
  builtinRe: makeWordRe([
    "make","len","cap","append","copy","delete","panic","recover","new","print","println",
    "fmt","os","io","http","json","strings","strconv","sync","time","context","errors","log",
  ]),
  keywords: [
    "func","var","const","type","struct","interface","map","chan","package","import",
    "return","if","else","for","range","switch","case","default","break","continue",
    "go","defer","select","fallthrough","goto",
  ],
  literals: ["nil","true","false","iota"],
});

// ── Language name normalization ──────────────────────────────────────
function getLangDef(lang) {
  if (!lang) return null;
  const key = lang.toLowerCase().trim();
  // alias mapping
  const aliases = {
    "typescript": "typescript", "ts": "typescript", "tsx": "typescript",
    "javascript": "javascript", "js": "javascript", "jsx": "javascript", "mjs": "javascript", "cjs": "javascript",
    "bash": "bash", "sh": "bash", "shell": "bash", "zsh": "bash",
    "json": "json", "jsonc": "json",
    "python": "python", "py": "python", "py3": "python",
    "rust": "rust", "rs": "rust",
    "go": "go", "golang": "go",
    "text": null, "plaintext": null, "plain": null, "txt": null, "": null,
  };
  const normalized = aliases[key];
  if (normalized === undefined) return null;
  if (normalized === null) return null;
  return LANG_DEFS[normalized] || null;
}

// ══════════════════════════════════════════════════════════
//  Markdown → HTML parser
// ══════════════════════════════════════════════════════════

function escapeHtml(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Turn code whitespace into HTML entities (WeChat drops leading whitespace), inserting a
// zero-width space (&#8203;) at line starts to preserve indentation.
function preserveWhitespace(html) {
  let result = "";
  let inTag = false;
  let atLineStart = true; // whether at line start (right after <br> or the start of the text)

  function anchor() {
    if (atLineStart) {
      result += "&#8203;";
      atLineStart = false;
    }
  }

  for (let i = 0; i < html.length; i++) {
    const ch = html[i];
    if (ch === "<") {
      inTag = true;
      result += ch;
    } else if (ch === ">") {
      inTag = false;
      if (result.endsWith("<br>")) {
        atLineStart = true;
      }
      result += ch;
    } else if (!inTag) {
      if (ch === "\n") {
        result += "<br>";
        atLineStart = true;
      } else if (ch === " ") {
        anchor();
        result += "&nbsp;";
      } else if (ch === "\t") {
        anchor();
        result += "&nbsp;&nbsp;&nbsp;&nbsp;";
      } else {
        result += ch;
        atLineStart = false;
      }
    } else {
      result += ch;
    }
  }
  return result;
}

// ── Inline rendering ──────────────────────────────────────────
const inlineRules = [
  { re: /!\[([^\]]*)\]\(([^)\s]+(?:\s+"[^"]*")?)\)/g, fn: (_, alt, src) => `<img src="${src}" alt="${alt}" style="max-width:100%;display:block;margin:12px auto;border-radius:4px;">` },
  { re: /\[([^\]]+)\]\(([^)\s]+)\)/g, fn: (_, text, href) => `<a href="${href}" style="${WECHAT_CSS.a}" target="_blank">${text}</a>` },
  { re: /\*\*\*(.+?)\*\*\*/g, fn: (_, text) => `<strong style="${WECHAT_CSS.strong}"><em>${text}</em></strong>` },
  { re: /\*\*(.+?)\*\*/g, fn: (_, text) => `<strong style="${WECHAT_CSS.strong}">${text}</strong>` },
  { re: /(?<!\*)\*([^*\n]+?)\*(?!\*)/g, fn: (_, text) => `<em style="${WECHAT_CSS.em}">${text}</em>` },
  { re: /~~(.+?)~~/g, fn: (_, text) => `<del style="${WECHAT_CSS.del}">${text}</del>` },
  { re: /`([^`\n]+?)`/g, fn: (_, text) => `<code style='${WECHAT_CSS.inlineCode}'>${escapeHtml(text)}</code>` },
];

function renderInline(text) {
  for (const rule of inlineRules) {
    text = text.replace(rule.re, rule.fn);
  }
  return text;
}

function parseMarkdown(md) {
  const lines = md.split("\n");
  const htmlLines = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // ── Code block ```...``` ──────────────────────────
    if (/^```/.test(line.trim())) {
      const lang = line.trim().slice(3).trim();
      const codeLines = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i].trim())) {
        codeLines.push(lines[i]);
        i++;
      }
      const rawCode = codeLines.join("\n");
      const highlighted = preserveWhitespace(highlightCode(rawCode, lang));
      const langLabel = lang ? `<div style="color:#8ab4f8;font-size:12px;margin-bottom:6px;font-weight:bold;letter-spacing:1px;">🔤 ${escapeHtml(lang.toUpperCase())}</div>` : "";
      htmlLines.push(
        `<section style="${WECHAT_CSS.pre}">${langLabel}<code style='${WECHAT_CSS.preCode}'>${highlighted}</code></section>`
      );
      i++; // skip closing ```
      continue;
    }

    // ── Horizontal rule ────────────────────────────────
    if (/^(-{3,}|\*{3,}|_{3,})\s*$/.test(line.trim())) {
      htmlLines.push(`<hr style="${WECHAT_CSS.hr}">`);
      i++;
      continue;
    }

    // ── Table ──────────────────────────────────────
    if (line.trim().startsWith("|") && line.trim().endsWith("|")) {
      const tableRows = [];
      while (i < lines.length && lines[i].trim().startsWith("|") && lines[i].trim().endsWith("|")) {
        tableRows.push(lines[i].trim());
        i++;
      }
      htmlLines.push(renderTable(tableRows));
      continue;
    }

    // ── Blockquote > ──────────────────────────────────
    if (line.startsWith(">")) {
      const quoteLines = [];
      while (i < lines.length && lines[i].startsWith(">")) {
        quoteLines.push(lines[i].replace(/^>\s?/, ""));
        i++;
      }
      const quoteContent = quoteLines.map(l => `<p style="margin:4px 0;">${renderInline(l)}</p>`).join("");
      htmlLines.push(`<blockquote style="${WECHAT_CSS.blockquote}">${quoteContent}</blockquote>`);
      continue;
    }

    // ── Unordered list ──────────────────────────────────
    if (/^[\s]*[-*+]\s/.test(line)) {
      const listItems = [];
      while (i < lines.length && /^[\s]*[-*+]\s/.test(lines[i])) {
        listItems.push(lines[i].replace(/^[\s]*[-*+]\s/, ""));
        i++;
      }
      const items = listItems.map(li => `<li style="${WECHAT_CSS.li}">${renderInline(li)}</li>`).join("");
      htmlLines.push(`<ul style="${WECHAT_CSS.ul}">${items}</ul>`);
      continue;
    }

    // ── Ordered list ──────────────────────────────────
    if (/^\s*\d+[.)]\s/.test(line)) {
      const listItems = [];
      while (i < lines.length && /^\s*\d+[.)]\s/.test(lines[i])) {
        listItems.push(lines[i].replace(/^\s*\d+[.)]\s/, ""));
        i++;
      }
      const items = listItems.map(li => `<li style="${WECHAT_CSS.li}">${renderInline(li)}</li>`).join("");
      htmlLines.push(`<ol style="${WECHAT_CSS.ol}">${items}</ol>`);
      continue;
    }

    // ── Headings ──────────────────────────────────────
    if (/^####\s/.test(line)) {
      htmlLines.push(`<h4 style="${WECHAT_CSS.h4}">${renderInline(line.replace(/^####\s/, ""))}</h4>`);
      i++; continue;
    }
    if (/^###\s/.test(line)) {
      htmlLines.push(`<h3 style="${WECHAT_CSS.h3}">${renderInline(line.replace(/^###\s/, ""))}</h3>`);
      i++; continue;
    }
    if (/^##\s/.test(line)) {
      htmlLines.push(`<h2 style="${WECHAT_CSS.h2}">${renderInline(line.replace(/^##\s/, ""))}</h2>`);
      i++; continue;
    }
    if (/^#\s/.test(line)) {
      htmlLines.push(`<h1 style="${WECHAT_CSS.h1}">${renderInline(line.replace(/^#\s/, ""))}</h1>`);
      i++; continue;
    }

    // ── Blank line: skip ────────────────────────────────
    if (line.trim() === "") {
      i++;
      continue;
    }

    // ── Normal paragraph ──────────────────────────────────
    const paraLines = [];
    while (i < lines.length && lines[i].trim() !== "" &&
      !/^(#{1,4}\s|```|>|[-*+]\s|\d+[.)]\s|\|.*\|$)/.test(lines[i]) &&
      !/^(-{3,}|\*{3,}|_{3,})\s*$/.test(lines[i].trim())) {
      paraLines.push(lines[i]);
      i++;
    }
    const paraText = paraLines.join("\n");
    const rendered = renderInline(paraText).replace(/\n/g, "<br>");
    htmlLines.push(`<p style="${WECHAT_CSS.p}">${rendered}</p>`);
  }

  return htmlLines.join("\n");
}

function renderTable(rows) {
  if (rows.length < 2) {
    return `<p style="${WECHAT_CSS.p}">${renderInline(rows.join("<br>"))}</p>`;
  }

  const headerCells = rows[0]
    .replace(/^\|/, "").replace(/\|$/, "").split("|").map(c => c.trim());
  const bodyRows = rows.slice(2);

  const theadCells = headerCells
    .map(c => `<th style="${WECHAT_CSS.th}">${renderInline(c)}</th>`).join("");
  const thead = `<thead><tr>${theadCells}</tr></thead>`;

  const tbodyRows = bodyRows
    .map((row, idx) => {
      const cells = row
        .replace(/^\|/, "").replace(/\|$/, "").split("|").map(c => c.trim());
      const bgStyle = idx % 2 === 0 ? WECHAT_CSS.trEven : WECHAT_CSS.trOdd;
      const tdCells = cells
        .map(c => `<td style="${WECHAT_CSS.td};${bgStyle}">${renderInline(c)}</td>`).join("");
      return `<tr>${tdCells}</tr>`;
    }).join("");
  const tbody = `<tbody>${tbodyRows}</tbody>`;

  return `<table style="${WECHAT_CSS.table}">${thead}${tbody}</table>`;
}

function buildPage(title, bodyHtml) {
  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>${escapeHtml(title)}</title>
</head>
<body style="margin:0;padding:16px;background:#fff;">
<div style='max-width:680px;margin:0 auto;${WECHAT_CSS.wrapper}'>
${bodyHtml}
</div>
</body>
</html>`;
}

// ── Main flow ────────────────────────────────────────────
function main() {
  if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
  }

  const allFiles = fs.readdirSync(INPUT_DIR).filter(f => f.endsWith(".md") && !f.includes(EXCLUDE_PATTERN));

  console.log(`Found ${allFiles.length} Markdown files\n`);

  const results = [];

  for (const filename of allFiles) {
    const filePath = path.join(INPUT_DIR, filename);
    const mdContent = fs.readFileSync(filePath, "utf-8").replace(/\r\n/g, "\n");

    console.log(`Processing: ${filename}`);

    const bodyHtml = parseMarkdown(mdContent);
    const title = filename.replace(/\.md$/, "");
    const fullHtml = buildPage(title, bodyHtml);

    const outFilename = filename.replace(/\.md$/, ".html");
    const outPath = path.join(OUTPUT_DIR, outFilename);
    fs.writeFileSync(outPath, fullHtml, "utf-8");

    results.push({ input: filename, output: outFilename, path: outPath });
  }

  console.log(`\n✅ All conversions complete! Output directory: ${OUTPUT_DIR}\n`);

  for (const r of results) {
    console.log(`  ${r.input}  →  ${r.output}`);
  }

  console.log(`\n📋 Usage:`);
  console.log(`  1. Open the HTML files in the wechat-formatted/ directory in a browser`);
  console.log(`  2. Press Ctrl+A to select all, Ctrl+C to copy`);
  console.log(`  3. Paste into the WeChat public account editor`);
}

main();

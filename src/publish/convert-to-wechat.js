/**
 * 微信公众号文章格式转换脚本
 * 将 Markdown 文件转换为微信公众号编辑器兼容的 HTML（含代码语法高亮）
 *
 * 使用方法: node convert-to-wechat.js
 * 输出目录: ./wechat-formatted/
 */

const fs = require("fs");
const path = require("path");

// ── 配置 ──────────────────────────────────────────────
const INPUT_DIR = __dirname;
const OUTPUT_DIR = path.join(__dirname, "wechat-formatted");
const EXCLUDE_PATTERN = "文章摘要";

// ── 微信公众号兼容的 CSS 样式 ──────────────────────────
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
//  语法高亮系统（微信兼容版 — 使用 font 标签代替 span+style）
// ══════════════════════════════════════════════════════════

// 配色 — 背景 #0d1117，针对公众号 HTML 渲染优化
// 原则：颜色多但都落在「短小分散」的 token 上，避免大片同色。
const HL = {
  keyword:  "#ff7b72",   // 关键字 — 红 (粗体)
  control:  "#ff7b72",   // 控制流 — 红
  string:   "#a5d6ff",   // 短字符串 — 浅蓝
  comment:  "#a8b3c2",   // 注释 — 浅灰 (斜体)
  number:   "#79c0ff",   // 数字 — 蓝
  literal:  "#79c0ff",   // 字面量 — 蓝
  func:     "#d8b4fe",   // 函数调用 — 亮紫
  type:     "#86efac",   // 类型/类名 — 亮绿
  operator: "#ffb86c",   // 运算符 — 橙
  builtin:  "#e3b341",   // 内置对象/命令 — 金黄
  var:      "#56d8c4",   // 变量($x) — 青
  decorator:"#ff9f43",   // 装饰器 @xxx — 深橙
  regex:    "#a5d6ff",   // 正则 — 浅蓝
  constant: "#79c0ff",   // 常量/选项 — 蓝
  meta:     "#ff9f43",   // 装饰器 — 深橙
};

// 附加文字样式标签
const HL_TAG = {
  keyword: "b",
  comment: "i",
};

// 用 font 标签包裹（微信兼容，避免 style="..." 被拆解）
function wrapHL(text, colorKey) {
  const color = HL[colorKey] || "white";
  const tag = HL_TAG[colorKey];
  let html = `<font color="${color}">${text}</font>`;
  if (tag === "b") html = `<b>${html}</b>`;
  if (tag === "i") html = `<i>${html}</i>`;
  return html;
}

// 所有「保护区域」的原始匹配 → 替换后二次处理
//
// 优化说明（相比旧版「顺序 replace + 占位符」）：
//  1. 单遍、确定性：先一次性收集所有规则(region/token)的匹配，再按
//     「起始位置 + 优先级」贪心合并，保证每个字符最多被上色一次，
//     彻底消除二次包裹 / 重叠标签。
//  2. 优先级 = 数组顺序：region(字符串/注释) 永远排在 token 之前，
//     因此字符串、注释内部的关键字不会被误上色（天然遮蔽）。
//  3. 不再依赖占位符字符，避免占位符被后续正则误吞。
function highlightCode(rawCode, lang) {
  if (!rawCode || rawCode.trim() === "") return "";

  const def = getLangDef(lang);
  if (!def) return escapeHtml(rawCode); // 未知语言，仅转义

  // 合并所有规则：regions 在前（高优先级），tokens 依次在后
  const rules = [];
  for (const r of def.regions) rules.push({ re: r.re, colorKey: r.colorKey, ok: r.ok, order: rules.length });
  for (const t of def.tokens) rules.push({ re: t.re, colorKey: t.colorKey, ok: t.ok, order: rules.length });

  // ── 收集所有规则的匹配 ──────────────────────────
  const events = [];
  for (const rule of rules) {
    rule.re.lastIndex = 0;
    let m;
    while ((m = rule.re.exec(rawCode)) !== null) {
      if (m[0].length === 0) { rule.re.lastIndex++; continue; } // 防死循环
      if (rule.ok && !rule.ok(m[0])) continue; // 长度/换行过滤
      events.push({ start: m.index, end: m.index + m[0].length, rule });
    }
  }

  // 按起始位置排序；同一起始位置按优先级(数组顺序)取更早的
  events.sort((a, b) => a.start - b.start || a.rule.order - b.rule.order);

  // 贪心合并：优先取最早开始、同起点取高优先级，跳过所有重叠区间
  const spans = [];
  let curEnd = 0;
  for (const ev of events) {
    if (ev.start < curEnd) continue; // 与已接受的区间重叠 → 丢弃
    if (ev.end <= ev.start) continue;
    spans.push(ev);
    curEnd = ev.end;
  }

  // ── 拼接 HTML：区间外原样转义，区间内按颜色包裹 ──
  const out = [];
  let pos = 0;
  for (const sp of spans) {
    if (sp.start > pos) out.push(escapeHtml(rawCode.slice(pos, sp.start)));
    const t = escapeHtml(rawCode.slice(sp.start, sp.end));
    out.push(sp.rule.colorKey ? wrapHL(t, sp.rule.colorKey) : t); // colorKey===null → 屏蔽为纯文本
    pos = sp.end;
  }
  if (pos < rawCode.length) out.push(escapeHtml(rawCode.slice(pos)));
  return out.join("");
}

// ── 语言定义 ──────────────────────────────────────────

// ── 高亮采用「最小化配色」策略 ──────────────────────────
// 只给短小的关键字 / 字面量 / 数字 / 函数调用单独上色，
// 不再把整段字符串、整段命令列表等长内容统一涂成一种颜色，
// 从而彻底消除「一大片相同颜色」的问题。注释用灰色单独区分。

const RE = {
  // 注释
  blockComment: /\/\*[\s\S]*?\*\//g,
  lineComment: /\/\/.*/g,
  hashComment: /#.*/g,
  // 字符串定界符（配合「短字符串」过滤器，只在短且无换行时上色）
  dq: /"(?:[^"\\\n]|\\.)*"/g,
  sq: /'(?:[^'\\\n]|\\.)*'/g,
  tick: /`(?:[^`\\\n]|\\.)*`/g,
  tripleDQ: /"""[\s\S]*?"""/g,
  tripleSQ: /'''[\s\S]*?'''/g,
  // 数字
  jsNumber: /\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?n?\b/g,
  plainNumber: /\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b/g,
  pyNumber: /\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?j?\b/g,
  intNumber: /\b\d+\b/g,
  rustNumber: /\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?(?:i8|i16|i32|i64|u8|u16|u32|u64|f32|f64|usize|isize)?\b/g,
  goNumber: /\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?i?\b/g,
  // 其他 token
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

// 构造一份语言定义。
// regions  : 注释(灰斜体) + 需屏蔽的长字符串(不上色)
// tokens   : 只含短小 token —— 关键字/字面量/内置/变量/装饰器/短字符串/类型/数字/运算符/函数
// 说明：字符串用「长度过滤」——≤24 且无换行才上色；更长或跨行的字符串则屏蔽为纯文本，
//       这样既加了字符串高亮，又不会重新出现「一整片字符串同色」的问题。
function makeLang({
  comments = [],            // 注释正则（灰斜体）
  plainRegions = [],        // 需屏蔽但不上色的正则（如长模板串/三引号）
  keywords = [],
  literals = [],
  builtinRe = null,         // 内置对象/命令（金黄）
  varRe = null,             // 变量（青）
  decoratorRe = null,       // 装饰器（深橙）
  stringRe = [],            // 字符串定界正则（短则上色，长则屏蔽）
  typeRe = null,            // 类型/类名（绿）
  numberRe = RE.plainNumber,
  operatorRe = null,        // 运算符（橙）
  funcRe = null,            // 函数调用（紫）
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
  plainRegions: [RE.tick], // 模板字符串屏蔽
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
  plainRegions: [RE.tripleDQ, RE.tripleSQ], // 三引号 docstring/长串屏蔽
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
  stringRe: [RE.dq], // 单引号是 char/生命周期，跳过
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
  plainRegions: [RE.tick], // 反引号 raw string 屏蔽
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

// ── 语言名标准化 ──────────────────────────────────────
function getLangDef(lang) {
  if (!lang) return null;
  const key = lang.toLowerCase().trim();
  // 别名映射
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
//  Markdown → HTML 解析器
// ══════════════════════════════════════════════════════════

function escapeHtml(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// 将代码中的空格/换行/制表符转为 HTML 实体，确保微信编辑器中不丢失缩进
// 行首缩进前插入零宽空格(&#8203;)，防止微信编辑器删除前导空白
function preserveWhitespace(html) {
  let result = "";
  let inTag = false;
  let atLineStart = true; // 是否在行首（紧跟 <br> 或文本开头）

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

// ── 内联渲染 ──────────────────────────────────────────
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

    // ── 代码块 ```...``` ──────────────────────────
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

    // ── 水平分割线 ────────────────────────────────
    if (/^(-{3,}|\*{3,}|_{3,})\s*$/.test(line.trim())) {
      htmlLines.push(`<hr style="${WECHAT_CSS.hr}">`);
      i++;
      continue;
    }

    // ── 表格 ──────────────────────────────────────
    if (line.trim().startsWith("|") && line.trim().endsWith("|")) {
      const tableRows = [];
      while (i < lines.length && lines[i].trim().startsWith("|") && lines[i].trim().endsWith("|")) {
        tableRows.push(lines[i].trim());
        i++;
      }
      htmlLines.push(renderTable(tableRows));
      continue;
    }

    // ── 引用块 > ──────────────────────────────────
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

    // ── 无序列表 ──────────────────────────────────
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

    // ── 有序列表 ──────────────────────────────────
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

    // ── 标题 ──────────────────────────────────────
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

    // ── 空行：跳过 ────────────────────────────────
    if (line.trim() === "") {
      i++;
      continue;
    }

    // ── 普通段落 ──────────────────────────────────
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

// ── 主流程 ────────────────────────────────────────────
function main() {
  if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
  }

  const allFiles = fs.readdirSync(INPUT_DIR).filter(f => f.endsWith(".md") && !f.includes(EXCLUDE_PATTERN));

  console.log(`找到 ${allFiles.length} 个 Markdown 文件\n`);

  const results = [];

  for (const filename of allFiles) {
    const filePath = path.join(INPUT_DIR, filename);
    const mdContent = fs.readFileSync(filePath, "utf-8").replace(/\r\n/g, "\n");

    console.log(`处理: ${filename}`);

    const bodyHtml = parseMarkdown(mdContent);
    const title = filename.replace(/\.md$/, "");
    const fullHtml = buildPage(title, bodyHtml);

    const outFilename = filename.replace(/\.md$/, ".html");
    const outPath = path.join(OUTPUT_DIR, outFilename);
    fs.writeFileSync(outPath, fullHtml, "utf-8");

    results.push({ input: filename, output: outFilename, path: outPath });
  }

  console.log(`\n✅ 全部转换完成！输出目录: ${OUTPUT_DIR}\n`);

  for (const r of results) {
    console.log(`  ${r.input}  →  ${r.output}`);
  }

  console.log(`\n📋 使用方法：`);
  console.log(`  1. 在浏览器中打开 wechat-formatted/ 目录下的 HTML 文件`);
  console.log(`  2. 按 Ctrl+A 全选，Ctrl+C 复制`);
  console.log(`  3. 粘贴到微信公众号编辑器中`);
}

main();

import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { createContext, runInContext } from 'node:vm';
import { fileURLToPath } from 'node:url';

/**
 * Markdown → 微信公众号兼容 HTML。
 *
 * 渲染本身**不再由本文件实现**,而是调用仓库里那个独立脚本
 * `src/publish/convert-to-wechat.js`(它给代码块做 GitHub 暗色语法高亮、
 * 给标题加左侧色条、表格带斑马纹)。本文件只做三件事:
 *
 * 1. 把那个脚本当函数库加载(见 loadConverter 的说明);
 * 2. 修掉它代码块样式里会让长行被裁掉的一处(`white-space: pre`);
 * 3. 兜住一切会让正文变形的输出(见 assertNoStrippedTags)。
 *
 * ⚠️ 关于**字符数上限**:这个模块曾经导出 `WECHAT_CONTENT_LIMIT = 20000`,
 * 理由是「官方文档写 content 少于 2 万字符」。**那条不成立** —— 实测
 * `draft/add` 收下了 183,914 字符的正文并原样回读(结构标签 15 项全对上、
 * 纯文本一字不差)。早先「19979 被接受」被当成了卡在上限,其实只是巧合。
 * 详见 WECHAT_CONTENT_LIMIT 的注释。
 */

/** 转义 HTML 文本节点。`&` 必须最先换,否则会把后面换出的实体又转一遍 */
export function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ============ 把那个独立脚本当函数库加载 ============

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
 * 加载 `src/publish/convert-to-wechat.js`。
 *
 * ⚠️ 为什么用 `vm` 而不是 `import`/`require`:
 * 那个脚本是**给人直接跑的独立 CLI** —— 没有 `module.exports`,而且末尾
 * 无条件调用 `main()`(它会去扫自己所在目录的 md、往 `wechat-formatted/`
 * 写文件)。直接 import 会连带触发这些副作用。
 * 我们又不想为了接进来就去改用户的脚本(那是他的文件,且可能被其他项目共用),
 * 所以这里把它当作「一段可执行的函数定义」载入:掐掉末尾的 main() 调用,
 * 再补一行 `var` 把需要的内联样式常量暴露到沙箱全局上
 * (顶层 `const` 不会成为 vm 上下文的属性,`var` 才会)。
 *
 * 脚本内部只用 `fs`/`path` 且都在函数体里,载入本身不碰磁盘。
 */
function loadConverter(): Converter {
  if (cached) return cached;

  const file = fileURLToPath(new URL('./convert-to-wechat.js', import.meta.url));
  const source = readFileSync(file, 'utf8')
    .replace(/\nmain\(\);\s*$/, '\n')
    .concat('\nvar __WECHAT_CSS = WECHAT_CSS;\n');

  // 本模块是 ESM(`package.json` 有 "type": "module"),作用域里没有 `require`;
  // 而那个脚本是 CommonJS 写法。用 createRequire 造一个给它,它内部
  // `require("fs")` / `require("path")` 才解析得到。
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
    throw new Error(`${file} 的加载结果不对:parseMarkdown/buildPage/WECHAT_CSS 有缺失`);
  }
  cached = {
    parseMarkdown: parseMarkdown as Converter['parseMarkdown'],
    buildPage: buildPage as Converter['buildPage'],
    WECHAT_CSS: styles as ConvertedStyles,
  };
  return cached;
}

// ============ 样式修正 ============

/**
 * 代码块的长行必须能折行。
 *
 * 脚本原本给代码块写的是 `white-space: pre` + `overflow-x: auto` ——
 * 那在浏览器预览里没问题(可以横向滚),但**公众号正文里横向滚动条不生效**,
 * 超出手机宽度的行长会被直接截断、内容看不全。本教程里有大量长命令行
 * (如 `vllm serve ... --disable-log-requests`),被裁就是真丢内容。
 *
 * `white-space: pre` 在整份输出里只出现在代码块样式这一处(已核对),
 * 所以这个替换是精确的。`word-break: break-all` 是必需的:
 * 代码里的空格被脚本换成了 `&nbsp;`(不换行空格),光靠 `pre-wrap` 折不开,
 * 得允许在任意字符处断开。
 */
function fixCodeBlockWrapping(html: string): string {
  return html.replace(/white-space:\s*pre;/g, 'white-space: pre-wrap; word-break: break-all;');
}

/**
 * 公众号会剥掉这几个标签,输出里出现说明渲染器写错了。
 * 与其等发到编辑器里才发现排版没了,不如在这里就炸掉。
 */
function assertNoStrippedTags(html: string): string {
  for (const tag of ['style', 'script', 'iframe', 'input']) {
    if (new RegExp(`<${tag}[\\s>]`, 'i').test(html)) {
      throw new Error(`转换结果里出现了 <${tag}>,公众号会把它剥掉 —— 渲染器有 bug`);
    }
  }
  return html;
}

/**
 * 把一段 Markdown 转成公众号正文 HTML。
 * 不返回错误 —— 转换是无损的,长度校验由调用方(wechat-issues)负责。
 */
export function mdToWechatHtml(markdown: string): string {
  const conv = loadConverter();
  const body = fixCodeBlockWrapping(conv.parseMarkdown(markdown));
  // 外层容器带上脚本预设的整篇排版(字体/字号/行距/字距),
  // 与它自己 buildPage 的预览一致 —— 预览看到什么,发出去就是什么。
  const html = `<div style="${conv.WECHAT_CSS.wrapper}">${body}</div>`;
  return assertNoStrippedTags(html);
}

/**
 * 拆篇护栏 —— **不是接口的真实上限**。
 *
 * 官方文档写 `content` 要「少于 2 万字符」,实测不成立:`draft/add` 接受了
 * 183,914 字符并原样回读。真实天花板没探到,而这个教程最长的章(18,附录C)
 * 渲染后 183,914 字符 —— 所以这里把一个远高于实际需要的数当护栏,
 * 正常情况**永远不会触发拆分**(即一章 = 一期)。
 * 万一将来某章大到离谱,它仍会兜住、拆成两期而不是静默失败。
 */
export const WECHAT_CONTENT_LIMIT = 500_000;

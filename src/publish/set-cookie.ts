import { renameSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import * as readline from 'node:readline/promises';
import { Writable } from 'node:stream';
import { assertCookieIgnored, cookieFilePath } from './credential.ts';
import { listDrafts, normalizeCookie } from './api/juejin.ts';
import { dim, error, header, info, success } from '../shared/ui.ts';

/**
 * 交互式写入掘金 sessionid 到 .juejin-cookie。
 *
 * 用法:
 *   node src/publish/set-cookie.ts            # 交互式粘贴(输入不回显)
 *   node src/publish/set-cookie.ts <cookie>   # 直接给值(⚠️ 会进 shell 历史,不推荐)
 *
 * 两条刻意的设计:
 *
 * 1. **输入不回显** —— 凭据不该留在终端回滚缓冲里。把 readline 的 output 指向
 *    丢弃流即可,不用引入任何依赖。同理,本文件从头到尾只打印掩码形式,
 *    绝不回显 sessionid 原文(即使在出错路径上)。
 *
 * 2. **写完立刻用只读接口验证** —— `/content_api/v1/article_draft/list_by_user`
 *    实测未登录时返回 `err_no=403「must login」`,带登录态才通。所以它能当
 *    「Cookie 到底有没有登录态」的试纸,且**零副作用**(只读,不建不改任何东西)。
 *    把失效的 Cookie 拦在这里,而不是等发到第 1 篇草稿才发现。
 */

const REPO_ROOT = process.cwd();
const VERIFY_TIMEOUT_MS = 15000;

/** 把 Cookie 串渲染成可安全展示的摘要:sessionid 只留头尾,其余只留键名 */
function maskCookie(cookie: string): string {
  const parts = cookie.split(';').map((p) => p.trim()).filter(Boolean);
  const shown: string[] = [];
  const others: string[] = [];

  for (const part of parts) {
    const eq = part.indexOf('=');
    const key = eq >= 0 ? part.slice(0, eq) : part;
    const val = eq >= 0 ? part.slice(eq + 1) : '';
    if (key === 'sessionid') {
      // 太短就完全不打码内容,只报长度 —— 免得把有效信息拼出来
      const head = val.length >= 12 ? `${val.slice(0, 4)}…${val.slice(-4)}` : '…';
      shown.push(`${key}=${head}(${val.length} 字符)`);
    } else {
      others.push(key);
    }
  }
  if (others.length > 0) shown.push(`另含 ${others.length} 个 cookie:${others.join(', ')}`);
  return shown.join(' · ');
}

/** 读取一行输入,终端下不回显 */
async function readHidden(question: string): Promise<string> {
  // 管道输入(echo xxx | node src/publish/set-cookie.ts):本来就没有回显问题
  if (!process.stdin.isTTY) {
    process.stdin.setEncoding('utf8');
    let buf = '';
    for await (const chunk of process.stdin) buf += chunk as string;
    return (buf.split(/\r?\n/)[0] ?? '').trim();
  }

  // 终端输入:output 指向丢弃流,readline 的回显就没了
  const muted = new Writable({
    write(_chunk, _enc, cb) {
      cb();
    },
  });
  const rl = readline.createInterface({ input: process.stdin, output: muted, terminal: true });
  process.stderr.write(question);
  try {
    return (await rl.question('')).trim();
  } finally {
    rl.close();
  }
}

/** 打印从哪里取的说明 —— sessionid 是 HttpOnly,这是上一轮踩过的坑 */
function printHowTo(): void {
  info('需要掘金登录态里的 sessionid。取法:');
  info('  浏览器登录掘金 → F12 → Application(应用)→ Cookies → https://juejin.cn');
  info('  找到 sessionid 那一行,复制它的 Value 粘过来');
  info('');
  info('  ⚠️ sessionid 是 HttpOnly cookie,在 Console 里敲 document.cookie 是看不到它的。');
  info('     必须走 Application 面板,或用 Network 面板里任意 api.juejin.cn 请求的');
  info('     「请求头 → cookie:」那一整行。');
  info('');
}

async function main(): Promise<number> {
  header('设置掘金 Cookie');

  // 先确认文件被 gitignore 覆盖 —— 凭据必须先保证不会进仓库,再谈写入
  const ignoreProblem = assertCookieIgnored(REPO_ROOT);
  if (ignoreProblem) {
    error(ignoreProblem);
    return 1;
  }

  const fromArgv = process.argv.slice(2).find((a) => !a.startsWith('--'));
  let raw = fromArgv ?? '';
  if (fromArgv) {
    dim('  提示:用命令行参数传凭据会留在 shell 历史里,下次可以不带参数、改用交互粘贴。');
  } else {
    printHowTo();
    raw = await readHidden('粘贴 sessionid(输入不回显;直接回车取消): ');
    if (!raw) {
      info('已取消,没有写入任何东西。');
      return 1;
    }
  }

  const cookie = normalizeCookie(raw);
  if (!cookie) {
    error('没能从输入里认出 sessionid。');
    if (raw.trim()) {
      error('  sessionid=xxx、整行 cookie、或只粘裸值都能识别 —— 但输入里确实没有 sessionid。');
      error('  只有 _tea_utm_cache / __tea_cookie_tokens / s_v_web_id 这类匿名 cookie 是无法登录的。');
    }
    return 1;
  }

  const sid = /(?:^|;\s*)sessionid=([^;]*)/.exec(cookie)?.[1] ?? '';
  if (sid.length < 16) {
    error(`sessionid 的值不完整(只有 ${sid.length} 字符),请重新复制完整值。`);
    return 1;
  }

  const file = cookieFilePath(REPO_ROOT);
  const tmp = `${file}.tmp`;
  try {
    writeFileSync(tmp, `${cookie}\n`, { encoding: 'utf8', mode: 0o600 });
    renameSync(tmp, file);
  } catch (err) {
    error(`写入 ${file} 失败: ${(err as Error).message}`);
    return 1;
  }

  success(`已写入 ${path.relative(REPO_ROOT, file)}(已被 .gitignore 忽略)`);
  dim(`  ${maskCookie(cookie)}`);
  dim('  环境变量 JUEJIN_COOKIE 优先级更高,若你设过它会覆盖这个文件。');
  info('');

  // 用「必须登录」的只读接口验一下,把失效的 Cookie 拦在这里
  info('正在验证登录态…');
  const res = await listDrafts({ cookie, timeoutMs: VERIFY_TIMEOUT_MS });
  if (res.ok) {
    success(`Cookie 有效 —— 掘金认得这个登录态(当前草稿箱 ${(res.data ?? []).length} 篇)`);
    info('接下来: node src/publish/juejin.ts --list   然后   node src/publish/juejin.ts');
    return 0;
  }
  if (res.authExpired) {
    error(`掘金没有认可这个登录态:${res.errMsg}`);
    error('  请确认复制的是「已登录」状态下、且属于 api.juejin.cn 的 sessionid。');
    error('  若浏览器里本来就没登录掘金,先去登录再取一次。');
    return 1;
  }
  // 非登录问题(网络、接口变动…)不该拦着用户,文件已经写好了
  info(`文件已保存,但连通性没验证成:${res.errMsg}`);
  dim('  这不一定是 Cookie 的问题,直接跑 node src/publish/juejin.ts --list 看看即可。');
  return 0;
}

main().then((code) => {
  process.exitCode = code;
});

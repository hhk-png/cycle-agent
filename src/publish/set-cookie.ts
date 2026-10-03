import { renameSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import * as readline from 'node:readline/promises';
import { Writable } from 'node:stream';
import { assertCookieIgnored, cookieFilePath } from './credential.ts';
import { listDrafts, normalizeCookie } from './api/juejin.ts';
import { dim, error, header, info, success } from '../shared/ui.ts';

/**
 * Interactively writes the Juejin sessionid to .juejin-cookie.
 *
 * Usage:
 *   node src/publish/set-cookie.ts            # interactive paste (input not echoed)
 *   node src/publish/set-cookie.ts <cookie>   # value as an argument (⚠️ stays in shell history)
 *
 * Two deliberate choices:
 * 1. **Input is never echoed** — credentials should not stay in the terminal scrollback,
 *    so readline writes to a discarded stream. Only masked forms are printed anywhere.
 * 2. **Verify right after writing with a read-only endpoint** — `list_by_user` returns
 *    403 "must login" when unauthenticated, so it is a zero-side-effect test for whether
 *    the cookie has a login session; a dead cookie is caught here, not at draft #1.
 */

const REPO_ROOT = process.cwd();
const VERIFY_TIMEOUT_MS = 15000;

/** Render a cookie string as a safe summary: sessionid head/tail only, other keys only */
function maskCookie(cookie: string): string {
  const parts = cookie.split(';').map((p) => p.trim()).filter(Boolean);
  const shown: string[] = [];
  const others: string[] = [];

  for (const part of parts) {
    const eq = part.indexOf('=');
    const key = eq >= 0 ? part.slice(0, eq) : part;
    const val = eq >= 0 ? part.slice(eq + 1) : '';
    if (key === 'sessionid') {
      // Too short to mask meaningfully; report only the length
      const head = val.length >= 12 ? `${val.slice(0, 4)}…${val.slice(-4)}` : '…';
      shown.push(`${key}=${head}(${val.length} chars)`);
    } else {
      others.push(key);
    }
  }
  if (others.length > 0) shown.push(`plus ${others.length} other cookies: ${others.join(', ')}`);
  return shown.join(' · ');
}

/** Read one line; no echo on a terminal */
async function readHidden(question: string): Promise<string> {
  // Piped input (echo xxx | node src/publish/set-cookie.ts): no echo concern anyway
  if (!process.stdin.isTTY) {
    process.stdin.setEncoding('utf8');
    let buf = '';
    for await (const chunk of process.stdin) buf += chunk as string;
    return (buf.split(/\r?\n/)[0] ?? '').trim();
  }

  // Terminal input: point readline's output at a discarded stream to suppress echo
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

/** Explain where to get it — sessionid is HttpOnly, a past gotcha */
function printHowTo(): void {
  info('Need the sessionid from your Juejin login. How to get it:');
  info('  Log in to Juejin in the browser → F12 → Application → Cookies → https://juejin.cn');
  info('  Find the sessionid row and paste its Value here');
  info('');
  info('  ⚠️ sessionid is an HttpOnly cookie; typing document.cookie in the Console will not show it.');
  info('     Use the Application panel, or any request to api.juejin.cn in the Network panel:');
  info('     copy the whole "Request Headers → cookie:" line.');
  info('');
}

async function main(): Promise<number> {
  header('Set Juejin Cookie');

  // Confirm the file is gitignored before writing: the credential must not be committable
  const ignoreProblem = assertCookieIgnored(REPO_ROOT);
  if (ignoreProblem) {
    error(ignoreProblem);
    return 1;
  }

  const fromArgv = process.argv.slice(2).find((a) => !a.startsWith('--'));
  let raw = fromArgv ?? '';
  if (fromArgv) {
    dim('  Hint: passing the credential as an argument leaves it in shell history; next time omit it and paste interactively.');
  } else {
    printHowTo();
    raw = await readHidden('Paste sessionid (input hidden; press Enter to cancel): ');
    if (!raw) {
      info('Cancelled; nothing was written.');
      return 1;
    }
  }

  const cookie = normalizeCookie(raw);
  if (!cookie) {
    error('Could not recognize a sessionid in the input.');
    if (raw.trim()) {
      error('  sessionid=xxx, a full cookie line, or a bare value are all accepted — but the input really has no sessionid.');
      error('  Anonymous cookies like _tea_utm_cache / __tea_cookie_tokens / s_v_web_id cannot log in.');
    }
    return 1;
  }

  const sid = /(?:^|;\s*)sessionid=([^;]*)/.exec(cookie)?.[1] ?? '';
  if (sid.length < 16) {
    error(`sessionid value is incomplete (only ${sid.length} chars); copy the full value again.`);
    return 1;
  }

  const file = cookieFilePath(REPO_ROOT);
  const tmp = `${file}.tmp`;
  try {
    writeFileSync(tmp, `${cookie}\n`, { encoding: 'utf8', mode: 0o600 });
    renameSync(tmp, file);
  } catch (err) {
    error(`Failed to write ${file}: ${(err as Error).message}`);
    return 1;
  }

  success(`Wrote ${path.relative(REPO_ROOT, file)} (ignored by .gitignore)`);
  dim(`  ${maskCookie(cookie)}`);
  dim('  The JUEJIN_COOKIE env var takes precedence and overrides this file if set.');
  info('');

  // Verify with the login-required read-only endpoint; catch a dead cookie here
  info('Verifying login…');
  const res = await listDrafts({ cookie, timeoutMs: VERIFY_TIMEOUT_MS });
  if (res.ok) {
    success(`Cookie is valid — Juejin accepts this login (draft box currently has ${(res.data ?? []).length})`);
    info('Next: node src/publish/publish.ts juejin --list   then   node src/publish/publish.ts juejin');
    return 0;
  }
  if (res.authExpired) {
    error(`Juejin did not accept this login: ${res.errMsg}`);
    error('  Make sure you copied a sessionid from a logged-in state belonging to api.juejin.cn.');
    error('  If the browser was not logged in to Juejin, log in first and fetch it again.');
    return 1;
  }
  // A non-auth problem (network, API change, ...) should not block the user; the file is already written
  info(`File saved, but connectivity could not be verified: ${res.errMsg}`);
  dim('  This is not necessarily a cookie problem; just run node src/publish/publish.ts juejin --list to check.');
  return 0;
}

main().then((code) => {
  process.exitCode = code;
});

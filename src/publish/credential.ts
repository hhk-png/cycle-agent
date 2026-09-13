import { spawnSync } from 'node:child_process';
import path from 'node:path';

/**
 * 凭据文件的位置与安全检查。
 *
 * sessionid 是**完整的账号凭据**(有效期约 30 天),一旦进 git 就是真实安全事件。
 * 所以「写入凭据」和「使用凭据」之前都必须先确认它被 .gitignore 覆盖。
 * 检查集中在这里一处,避免两个入口各写一份、日子久了慢慢走样。
 */

/** 默认凭据文件名(掘金) */
export const DEFAULT_COOKIE_FILE = '.juejin-cookie';

/** 凭据文件绝对路径 */
export function cookieFilePath(repoRoot: string, fileName: string = DEFAULT_COOKIE_FILE): string {
  return path.join(repoRoot, fileName);
}

/**
 * 确认凭据文件被 .gitignore 覆盖。
 * 返回 null 表示安全;返回字符串表示不能继续(内容是给用户看的原因)。
 * git 不可用或不在 git 仓库时返回 null —— 那种环境下本就不存在提交泄漏。
 *
 * 微信的 AppSecret 走同一套检查(传文件名),所以这条规则只有一份实现。
 */
export function assertCookieIgnored(repoRoot: string, fileName: string = DEFAULT_COOKIE_FILE): string | null {
  const r = spawnSync('git', ['check-ignore', '-q', fileName], { cwd: repoRoot });
  if (r.status === 0) return null;
  if (r.status === 1) {
    return (
      `${fileName} 未被 .gitignore 忽略,存在凭据泄漏风险。\n` +
      `  请先在 .gitignore 里加上 ${fileName} 再重试。`
    );
  }
  return null;
}

import { spawnSync } from 'node:child_process';
import path from 'node:path';

/**
 * Credential file location and security checks.
 *
 * sessionid is a full account credential (valid ~30 days); leaking it into git is a real security
 * incident. Both writing and using credentials confirm .gitignore coverage, centralized here.
 */

/** Default credential file name (Juejin) */
export const DEFAULT_COOKIE_FILE = '.juejin-cookie';

/** Absolute path of the credential file */
export function cookieFilePath(repoRoot: string, fileName: string = DEFAULT_COOKIE_FILE): string {
  return path.join(repoRoot, fileName);
}

/**
 * Confirms the credential file is covered by .gitignore: null = safe, string = reason to stop. Null when
 * git is unavailable or we're not in a repo (no leak possible). WeChat's AppSecret uses the same check.
 */
export function assertCookieIgnored(repoRoot: string, fileName: string = DEFAULT_COOKIE_FILE): string | null {
  const r = spawnSync('git', ['check-ignore', '-q', fileName], { cwd: repoRoot });
  if (r.status === 0) return null;
  if (r.status === 1) {
    return (
      `${fileName} is not ignored by .gitignore, posing a credential leak risk.\n` +
      `  Please add ${fileName} to .gitignore before retrying.`
    );
  }
  return null;
}

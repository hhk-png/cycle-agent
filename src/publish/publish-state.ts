import { closeSync, existsSync, openSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs';
import path from 'node:path';

/**
 * Persistence of publish state.
 *
 * ⚠️ Two non-negotiable rules:
 * 1. A corrupt state file must hard-stop, never be treated as empty —— otherwise the 14 already-published
 *    articles get republished (public, irreversible). loadState errors on parse failure and --force does
 *    not bypass it.
 * 2. Flush after every step, never inside a signal handler —— save the draftId before publishing so a
 *    failed publish or Ctrl-C doesn't lose an orphan draft. At most one article's progress is lost.
 */

export const STATE_VERSION = 1;

/** Publish state of a single article */
export interface ArticleState {
  /** pending = draft not created; drafted = draft created awaiting publish; published = published */
  status: 'pending' | 'drafted' | 'published';
  draftId: string | null;
  articleId: string | null;
  url: string | null;
  /** sha256 of the body at draft-creation/publish time, to detect later body edits */
  contentHash: string;
  /** Title used at the last successful publish; `--sync` skips the change+publish when it already matches the target. */
  title?: string;
  /**
   * The brief written at the last draft-creation/sync. Not covered by `contentHash` (title+body only), so
   * `--sync` needs it to detect brief drift without reading the remote. Old files lack it → undefined ≠
   * any brief → the first sync refreshes everything, which is the desired behavior.
   */
  brief?: string;
  /** When a publish request was sent but the result is unknown (timeout/interruption); automatic re-publishing is forbidden then */
  publishAttemptedAt: string | null;
  /** Published but no article_id obtained; needs manual confirmation in the backend */
  needsCheck: boolean;
  updatedAt: string;
}

/** State of the whole publish pipeline */
export interface PublishState {
  version: number;
  /** Cover URL read back from the draft, reused across articles; empty string means not set yet */
  coverImage: string;
  /** key = two-digit number, e.g. '09' */
  articles: Record<string, ArticleState>;
}

/**
 * State file name, namespaced by config and platform so publishing other tutorials or platforms never
 * cross-wires (WeChat passes `'wechat-publish-state'`).
 */
export function stateFileName(
  repoRoot: string,
  configName: string,
  namespace = 'juejin-publish-state',
): string {
  return path.join(repoRoot, `.${namespace}.${configName}.json`);
}

/** A brand-new empty state */
export function newState(): PublishState {
  return { version: STATE_VERSION, coverImage: '', articles: {} };
}

/** Get an article's state, creating it if it doesn't exist */
export function ensureArticle(state: PublishState, no: string, contentHash: string): ArticleState {
  const existing = state.articles[no];
  if (existing) return existing;
  const fresh: ArticleState = {
    status: 'pending',
    draftId: null,
    articleId: null,
    url: null,
    contentHash,
    publishAttemptedAt: null,
    needsCheck: false,
    updatedAt: new Date().toISOString(),
  };
  state.articles[no] = fresh;
  return fresh;
}

/**
 * Reads the state. Missing file → fresh empty state; corrupt content or version mismatch → error
 * (file-header rule 1).
 */
export function loadState(file: string): { state: PublishState } | { error: string } {
  if (!existsSync(file)) return { state: newState() };

  let text: string;
  try {
    text = readFileSync(file, 'utf8');
  } catch (err) {
    return { error: `Failed to read state file: ${(err as Error).message}` };
  }
  if (!text.trim()) return { state: newState() };

  let parsed: PublishState;
  try {
    parsed = JSON.parse(text) as PublishState;
  } catch (err) {
    return {
      error:
        `State file is corrupt and cannot be parsed: ${file}\n` +
        `  ${(err as Error).message}\n` +
        `  Please repair it, or move it away and rerun. **Do not just delete it** —— it records which articles have already been published,\n` +
        `  and deleting it will cause already-published articles to be republished.`,
    };
  }

  if (parsed.version !== STATE_VERSION) {
    return {
      error:
        `State file version mismatch (file ${parsed.version}, current ${STATE_VERSION}): ${file}\n` +
        `  Please migrate it by hand or move the file away before rerunning.`,
    };
  }
  if (!parsed.articles || typeof parsed.articles !== 'object') {
    return { error: `State file is missing the articles field and may be corrupt: ${file}` };
  }
  if (typeof parsed.coverImage !== 'string') parsed.coverImage = '';
  return { state: parsed };
}

/** Atomic write: .tmp then rename, so an interruption doesn't leave half a JSON file */
export function saveState(file: string, state: PublishState): { ok: true } | { error: string } {
  const tmp = `${file}.tmp`;
  try {
    writeFileSync(tmp, `${JSON.stringify(state, null, 2)}\n`, 'utf8');
    renameSync(tmp, file);
    return { ok: true };
  } catch (err) {
    return { error: `Failed to write state file: ${(err as Error).message}` };
  }
}

// ============ Concurrency lock ============

/** Process lock so two terminals can't overwrite each other's state; stale locks are taken over. */
export function acquireLock(file: string): { ok: true } | { error: string } {
  const lockFile = `${file}.lock`;
  const payload = JSON.stringify({ pid: process.pid, at: new Date().toISOString() });

  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      closeSync(openSync(lockFile, 'wx'));
      writeFileSync(lockFile, payload, 'utf8');
      return { ok: true };
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code !== 'EEXIST') {
        return { error: `Failed to create lock file: ${(err as Error).message}` };
      }
      // Lock already exists: check whether the holder is still alive
      let pid = 0;
      try {
        pid = Number(JSON.parse(readFileSync(lockFile, 'utf8')).pid) || 0;
      } catch {
        pid = 0; // The lock file itself is broken; treat it as a stale lock
      }
      if (pid > 0 && isAlive(pid)) {
        return {
          error: `Another publish is already in progress (pid ${pid}).\n  If you are sure no other process is running, delete ${lockFile} and retry.`,
        };
      }
      // Stale lock: clear it and take over
      try {
        unlinkSync(lockFile);
      } catch {
        /* On a lost race, try another round; the next round will report an error */
      }
    }
  }
  return { error: `Could not acquire lock: ${lockFile}` };
}

/** Release the lock (best effort) */
export function releaseLock(file: string): void {
  try {
    unlinkSync(`${file}.lock`);
  } catch {
    /* ignore */
  }
}

function isAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    // EPERM means the process exists but we lack permission to signal it; still counts as alive
    return (err as NodeJS.ErrnoException).code === 'EPERM';
  }
}

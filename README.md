# cycle-iteration

Two tools:

- **iterate** — repeatedly feeds an initial description to `claude -p` to generate and refine a Chinese tutorial under `./<name>-toturial/`.
- **publish** — pushes those chapters to Juejin and to a WeChat Official Account draft box.

Tutorial content (`*-toturial/`) is Chinese. Code, comments and docs are English.

## Requirements

Node ≥ 24, pnpm, and [Claude Code](https://claude.com/claude-code) logged in (needed by `iterate`).

```bash
pnpm install
```

## Iterate

Each tutorial is one config under `src/iterate/configs/`; nothing comes from the command line.

```bash
pnpm start                        # run the only config, or pick interactively
node src/iterate/run.ts <name>    # run src/iterate/configs/<name>.ts
pnpm configs                      # list saved configs
```

Round 1 sends `firstRoundPrompt`; every later round sends `refinePrompt` with the previous output as context.

## Publish

```bash
node src/publish/publish.ts <juejin|wechat> [config] [options…]
```

With no arguments it prints usage and the available config names. Each tutorial needs two configs under `src/publish/configs/`: `<name>.ts` (Juejin) and `wechat-<name>.ts` (WeChat).

**Credentials.** Juejin: a browser cookie in `.juejin-cookie` (gitignored), written by `pnpm publish:cookie`. WeChat: `appId` in the config plus the AppSecret in `.wechat-secret` (gitignored) or `WECHAT_APPSECRET`, and the machine's egress IP whitelisted in the WeChat developer platform (check with `wechat <config> --ip`).

**Flow.** Validate, build drafts, stop after the first one to check rendering and cover, then finish in one pass.

```bash
node src/publish/publish.ts juejin <config> --list          # validate briefs, no network
node src/publish/publish.ts juejin <config> --drafts-only   # first draft, then pauses
node src/publish/publish.ts juejin <config> --drafts-only --yes
node src/publish/publish.ts juejin <config> --yes           # publish
node src/publish/publish.ts juejin <config> --sync          # after editing briefs or titles

node src/publish/publish.ts wechat <config> --list
node src/publish/publish.ts wechat <config> --build         # offline preview in .wechat-out/
node src/publish/publish.ts wechat <config> --drafts --yes
```

WeChat has no publish API for personal accounts — the tool stops at the draft box and the last step happens in the WeChat backend. WeChat covers upload automatically. Juejin has no image-upload API, so its cover is set by hand once on the first draft and read back for the rest.

Progress lives in `.juejin-publish-state.<config>.json` and `.wechat-publish-state.<config>.json` (gitignored). Every step is persisted, so repeating the same command resumes an interrupted run. **Never delete those files** — they record what has already been published.

**Limits.** Juejin brief: one line, 50–100 characters. WeChat digest ≤ 120, title ≤ 32 (`titleOverrides`), cover mandatory. Juejin tags ≤ 3.

Full option list: the usage comment at the top of `src/publish/juejin.ts` and `src/publish/wechat.ts`.

## Layout

```
src/shared/     ui.ts, types.ts
src/iterate/    run.ts, config.ts, claude.ts, configs/
src/publish/    publish.ts (entry), juejin.ts, wechat.ts, set-cookie.ts,
                articles.ts, credential.ts, publish-state.ts, publish-config.ts,
                wechat-config.ts, wechat-issues.ts, markdown.ts,
                convert-to-wechat.js, configs/
  api/          juejin.ts, wechat.ts — the only files that make network calls
```

## Scripts

`pnpm start` · `pnpm configs` · `pnpm typecheck` · `pnpm publish` · `pnpm publish:juejin` · `pnpm publish:wechat` · `pnpm publish:cookie`

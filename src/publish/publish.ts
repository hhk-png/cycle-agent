import pc from 'picocolors';
import { error, stopSpinnerActive } from '../shared/ui.ts';
import { listPublishConfigNames } from './publish-config.ts';
import { listWechatConfigNames } from './wechat-config.ts';
import { run as runJuejin } from './juejin.ts';
import { run as runWechat } from './wechat.ts';

/**
 * **唯一的发布入口。** 掘金和微信两套流程都从这里进,第一个参数选平台。
 *
 *   node src/publish/publish.ts juejin [配置名] [选项…]
 *   node src/publish/publish.ts wechat [配置名] [选项…]
 *
 * 为什么只有一个入口:两个平台的状态、锁、摘要来源、封面处理方式各不相同,
 * 但**跑起来的节奏是一样的** —— 先 --list 校验、再建草稿、第 1 篇停下看排版、
 * 然后 --yes 跑完。收成一个入口,这条节奏就只有一处可选错。
 *
 * 平台参数还接受别名:jj / 掘金、wx / 微信。
 *
 * 完整流程见 src/publish/README.md。
 */

/** 两个平台的别名 */
const ALIASES: Record<string, 'juejin' | 'wechat'> = {
  juejin: 'juejin',
  jj: 'juejin',
  掘金: 'juejin',
  wechat: 'wechat',
  wx: 'wechat',
  微信: 'wechat',
};

export function usage(): string {
  const juejinConfigs = listPublishConfigNames();
  const wechatConfigs = listWechatConfigNames();
  return [
    '用法: node src/publish/publish.ts <平台> [配置名] [选项…]',
    '',
    '平台:',
    '  juejin, jj, 掘金      发布到掘金(建草稿 / 发布 / 同步)',
    '  wechat, wx, 微信      发布到微信公众号(只能建草稿)',
    '',
    `掘金配置: ${juejinConfigs.join(', ') || '(无)'}`,
    `微信配置: ${wechatConfigs.join(', ') || '(无)'}`,
    '',
    '典型流程(以 ai-agent-toturial 为例):',
    '  1. 校验摘要与配置      node src/publish/publish.ts juejin ai-agent-toturial --list',
    '  2. 只建草稿(不公开)  node src/publish/publish.ts juejin ai-agent-toturial --drafts-only',
    '     ↳ 第 1 篇建好后会停下,去编辑器里设封面、看排版',
    '  3. 继续建完剩下的      node src/publish/publish.ts juejin ai-agent-toturial --drafts-only --yes',
    '  4. 发布                node src/publish/publish.ts juejin ai-agent-toturial --yes',
    '  5. 改了摘要/标题后同步  node src/publish/publish.ts juejin ai-agent-toturial --sync',
    '',
    '微信侧同理,把 juejin 换成 wechat(它没有发布接口,到草稿为止):',
    '  node src/publish/publish.ts wechat wechat-ai-agent --list',
    '  node src/publish/publish.ts wechat wechat-ai-agent --drafts',
    '',
    '各平台自己的选项(用 --help 看详情):',
    '  掘金: --list --categories --tags <词> --suggest-briefs --dry-run --drafts-only',
    '        --yes --from NN --to NN --only NN --sync --republish NN --force --orphans',
    '  微信: --list --build --plan --check --drafts --ip --only NN --force --yes',
  ].join('\n');
}

async function main(): Promise<number> {
  const argv = process.argv.slice(2);

  if (argv.length === 0 || argv[0] === '--help' || argv[0] === '-h') {
    // 没有平台参数不是错误,是要看用法 —— 用 stdout,别让 shell 以为失败了
    console.log(usage());
    return argv.length === 0 ? 1 : 0;
  }

  const platform = ALIASES[argv[0]];
  if (!platform) {
    error(`不认识的平台: ${argv[0]}(可用: juejin / wechat)`);
    console.log(usage());
    return 1;
  }

  const rest = argv.slice(1);
  if (rest.includes('--help') || rest.includes('-h')) {
    console.log(usage());
    return 0;
  }

  return platform === 'juejin' ? runJuejin(rest) : runWechat(rest);
}

// SIGINT 只在这里注册一次 —— 两个平台各注册一份的话,同一个 Ctrl-C 会触发两遍。
// 不在信号处理器里写状态文件(写盘竞态);靠「每完成一步即落盘」保证可续跑。
process.on('SIGINT', () => {
  stopSpinnerActive();
  console.log(pc.red('✖ 已中断'));
  process.exit(130);
});

main()
  .then((code) => {
    process.exitCode = code;
  })
  .catch((err: unknown) => {
    error(`未预期的错误: ${(err as Error).message}`);
    process.exitCode = 1;
  });

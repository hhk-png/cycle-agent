import type { WechatConfig } from '../wechat-config.ts';
import juejin from './ai-agent-toturial.ts';

/**
 * AI Agent 教程(00~22 章)的微信公众号发布配置。
 *
 * 摘要直接复用掘金那份 —— 一个教程的摘要只有一处可改。微信的 digest 上限是
 * 120 字,比掘金的 100 字宽松,所以原样搬过来都合规。
 *
 * ⚠️ 微信标题上限 **32 字**,而文件名风格的标题本身就长,下面 titleOverrides
 * 把超限与卡线(正好 32)的几篇缩到 32 以内。判断依据是
 * `loadArticles` 出来的 title 长度,不是源文件名长度。
 */
const config: WechatConfig = {
  sourceDir: 'ai-agent-toturial',
  filePrefix: '',
  fromNumber: 0,

  // 与掘金一致:文件名风格 + 系列名前缀(源文件名没有前缀,这里补上)
  titleSource: 'fileName',
  titlePrefix: 'ai-agent教程-',

  // ⬇️ 微信开发者平台(developers.weixin.qq.com/platform/)→ 我的业务
  //    → 公众号 → 基础信息。与 vllm 教程是同一个公众号。
  appId: 'wx27e3c5ea021918b7',
  secretFile: '.wechat-secret',

  author: '',

  // 与 vllm 教程共用同一张封面(2.35:1,已按公众号裁切规则补过白)
  coverImage: 'assets/wechat-cover.jpg',

  // 第 1 期的日期,逐日往后排。--plan 用它生成排期表;草稿本身不依赖它
  startDate: '2026-10-05',

  contentSourceUrl: '',

  // 微信标题上限 32 字。15 原本 34 字(超限)、22 原本正好 32(卡线)
  titleOverrides: {
    '15': 'ai-agent教程-15-附录B-工程参考',
    '22': 'ai-agent教程-22-附录I-原则与决策速查',
  },

  digests: {
    ...juejin.briefs,
  },

  delayMs: 3000,
  timeoutMs: 15000,
};

export default config;

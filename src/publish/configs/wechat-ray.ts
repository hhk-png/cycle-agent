import type { WechatConfig } from '../wechat-config.ts';
import juejin from './ray-toturial.ts';

/**
 * Ray 教程(00~39 章)的微信公众号发布配置。
 *
 * 源文件名自带 `ray教程-` 前缀,所以不需要 titlePrefix —— 标题就是
 * `ray教程-00-前言与导读`。
 *
 * 摘要直接复用掘金那份(微信 digest 上限 120 字 > 掘金 100 字,都合规)。
 *
 * ⚠️ 微信标题上限 **32 字**:26/27/29 三篇超限,下面缩到 32 以内。
 */
const config: WechatConfig = {
  sourceDir: 'ray-toturial',
  filePrefix: 'ray教程',
  fromNumber: 0,

  titleSource: 'fileName',

  appId: 'wx27e3c5ea021918b7',
  secretFile: '.wechat-secret',

  author: '',

  coverImage: 'assets/wechat-cover.jpg',

  // 接在 AI Agent 系列(2026-10-05 起 23 期)之后,2026-11-01 开排。
  // 草稿本身不依赖这个日期,它只影响 --plan 的排期表
  startDate: '2026-11-01',

  contentSourceUrl: '',

  // 微信标题上限 32 字,这三篇按原文件名会超限
  titleOverrides: {
    '26': 'ray教程-26-RayCompiledGraph与DAG',
    '27': 'ray教程-27-附录F-RayClient与多语言',
    '29': 'ray教程-29-Ray与PyTorch生态集成',
  },

  digests: {
    ...juejin.briefs,
  },

  delayMs: 3000,
  timeoutMs: 15000,
};

export default config;

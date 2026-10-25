import type { WechatConfig } from '../wechat-config.ts';

/** WeChat publish config for the ClickHouse tutorial (chapters 00~38). */
const config: WechatConfig = {
  sourceDir: 'clickhouse-tutorial',
  filePrefix: 'clickhouse教程',
  fromNumber: 0,
  titleSource: 'fileName',

  appId: 'wx27e3c5ea021918b7',
  secretFile: '.wechat-secret',

  startDate: '2026-11-01',
  digests: {},
  renderMode: 'wechat',
  // Appendix B renders to ~600k chars. Draft/add accepted it in one piece, so raise
  // the guardrail above the default 500k instead of splitting it into two issues.
  contentLimit: 1_000_000,

  delayMs: 3000,
  timeoutMs: 15000,
};

export default config;

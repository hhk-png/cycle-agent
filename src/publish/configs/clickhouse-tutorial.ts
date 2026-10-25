import type { PublishConfig } from '../publish-config.ts';

/**
 * Juejin publish config for the ClickHouse tutorial (chapters 00~38).
 *
 * Briefs are intentionally empty for now: these drafts are created with
 * `--skip-brief-check`; fill `briefs` before publishing.
 */
const config: PublishConfig = {
  sourceDir: 'clickhouse-tutorial',
  filePrefix: 'clickhouse教程',
  fromNumber: 0,
  titleSource: 'fileName',

  categoryId: '6809637769959178254', // 后端
  tagIds: [
    '6809640600502009863', // 数据库
    '6809641131131797511', // 大数据
    '6809641135154135054', // 分布式
  ],

  coverImage: '',
  delayMs: 5000,
  timeoutMs: 15000,
  briefs: {},
};

export default config;

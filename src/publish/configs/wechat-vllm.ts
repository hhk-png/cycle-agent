import type { WechatConfig } from '../wechat-config.ts';
import juejin from './vllm-toturial.ts';

/**
 * WeChat Official Account publish config for the vLLM tutorial (chapters 07~22).
 *
 * WeChat starts at chapter 07, Juejin at 09, so the 07/08 digests are written below
 * (the Juejin config has no entry for them). The rest reuse the Juejin config
 * (WeChat 120 > Juejin 100, both compliant).
 */
const config: WechatConfig = {
  sourceDir: 'vllm-toturial',
  filePrefix: 'vllm教程',
  fromNumber: 7,

  // same as Juejin: file name minus .md, e.g. `vllm教程-07-调度与连续批处理`
  titleSource: 'fileName',

  // ⬇️ WeChat platform (developers.weixin.qq.com/platform/) → My Business → Official Account → Basic Information
  //    (AppSecret and IP allowlist are under "Basic Information → Developer Keys")
  appId: 'wx27e3c5ea021918b7',

  // AppSecret stays out of git — only in gitignored .wechat-secret (or env WECHAT_APPSECRET):
  //   echo "your-AppSecret" > .wechat-secret
  secretFile: '.wechat-secret',

  // shows in the article; empty omits it (≤16 chars)
  author: '',

  // cover path (repo-relative); upload once for a permanent thumb_media_id, reused every issue.
  //
  // ⚠️ cover is **mandatory**: no thumb_media_id → 40007 invalid media_id, so there's no "create issue 1, set cover later".
  //
  // wechat-cover.jpg: a 640×640 square tightened to 436×488, then white-padded to 2.35:1.
  // WeChat crops covers to 2.35:1, so a direct square upload loses 57% of its height; padding neither
  // crops nor upscales and blends with the image's own white. To switch: replace the file, or change any
  // issue's cover in the backend and delete coverImage from state to re-borrow it.
  coverImage: 'assets/wechat-cover.jpg',

  // issue 1's publish date, then daily. (09-12 was already past before that batch shipped; the backend can't schedule past dates)
  startDate: '2026-09-13',

  // table styling now lives in the render script: WECHAT_CSS.table/th/td in src/publish/convert-to-wechat.js.

  // unverified subscription accounts don't support external links; keep empty
  contentSourceUrl: '',

  // ⚠️ titles max 32 chars; this name is 29, and adding "(1)(2)(3)" hits exactly 32 (on the line), so it's shortened to 20.
  titleOverrides: {
    '18': 'vllm教程-18-附录C-工程参考',
  },

  digests: {
    // chapters 07/08 aren't covered by the Juejin config (starts at 09), so written here
    '07':
      '从静态批处理的空转问题讲起，拆解连续批处理为什么快：调度器每一步在做什么、prefill 与 decode 如何混批、chunked prefill 与抢占的取舍，并用 mini-vLLM 的代码和实测数据佐证。',
    '08':
      '拆解 vLLM 的性能账：PagedAttention 内核为什么快、KV cache 显存怎么算、CUDA Graph 与连续批处理的吞吐延迟权衡，以及投机解码与并行度选择。',
    ...juejin.briefs,
  },

  // article-split limit; defaults to WECHAT_CONTENT_LIMIT (500k), a guardrail rather than the API
  // limit (measured 180k chars with verbatim read-back), so it never triggers at one chapter per issue.
  // contentLimit: 400000,

  delayMs: 3000,
  timeoutMs: 15000,
};

export default config;

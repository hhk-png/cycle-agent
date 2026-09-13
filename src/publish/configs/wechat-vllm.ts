import type { WechatConfig } from '../wechat-config.ts';
import juejin from './vllm-toturial.ts';

/**
 * vLLM 教程(07~22 章)的微信公众号发布配置。
 *
 * 微信侧从 **07 章**起(07 是后补的 —— 早先这批从 08 建起,07 漏在外面),
 * 掘金那侧的配置从 09 章起。两边覆盖范围不同,所以摘要表里 07、08 两条
 * 掘金那份都没有,得在下面单独写。
 *
 * 摘要其余部分直接复用掘金那份(单一来源,一个教程的摘要只有一处可改),
 * 微信的 digest 上限是 120 字,比掘金的 100 字宽松,所以原样搬过来都合规。
 */
const config: WechatConfig = {
  sourceDir: 'vllm-toturial',
  filePrefix: 'vllm教程',
  fromNumber: 7,

  // 与掘金一致:用文件名去掉 .md,即 `vllm教程-07-调度与连续批处理`
  titleSource: 'fileName',

  // ⬇️ 微信开发者平台(developers.weixin.qq.com/platform/)→ 我的业务
  //    → 公众号 → 基础信息。AppSecret 与 IP 白名单在「基础信息 → 开发密钥」。
  //    (2025-12-01 起从公众号后台的「开发接口管理」迁移过去的那一批功能)
  appId: 'wx27e3c5ea021918b7',

  // AppSecret **不写在这里**,只放 gitignored 的 .wechat-secret
  // (或环境变量 WECHAT_APPSECRET):
  //   echo "你的AppSecret" > .wechat-secret
  secretFile: '.wechat-secret',

  // 作者名会显示在文章里;留空则不传该字段(≤16 字)
  author: '',

  // 封面图本地路径(相对仓库根)。上传一次拿到永久素材 thumb_media_id,各期全复用。
  //
  // ⚠️ 封面是**必填**:不传 thumb_media_id 建草稿会返回 40007 invalid media_id,
  // 所以不能靠「先建 1 期再去后台设封面」来起头 —— 那是先有鸡还是先有蛋。
  //
  // assets/wechat-cover.jpg 由桌面那张 640×640 正方形转来:
  // 先收紧白边(内容框 436×488),再横向补白到 2.35:1。
  // 公众号封面按 2.35:1 裁切,正方形直接上传会被裁掉 57% 的高度(只剩一条脸),
  // 补白则**既不裁也不放大**,整只都留住,且用的是图片自身的背景色(纯白)无接缝。
  // 想换封面:替换这个文件,或在后台改任意一期的封面后把 state 里的
  // coverImage 删掉,重跑时会从草稿箱重新借。
  coverImage: 'assets/wechat-cover.jpg',

  // 第 1 期的发布日期,后面逐日排。今天 = 2026-09-13
  // (原来写的是 09-12,那批还没发出去就已经过期了 —— 后台的定时发表排不了过去的日期)
  startDate: '2026-09-13',

  // 表格样式(边框/斑马纹/表头底色)现在由渲染脚本决定,这里不再有开关。
  // 要调就改 src/publish/convert-to-wechat.js 里的 WECHAT_CSS.table / th / td。

  // 未认证订阅号正文不支持外链,保持为空
  contentSourceUrl: '',

  // ⚠️ 微信标题上限 32 字。下面这篇原文件名 29 字,加「(一)(二)(三)」
  // 正好顶到 32(卡线,可能被截),所以缩成 20 字。
  titleOverrides: {
    '18': 'vllm教程-18-附录C-工程参考',
  },

  digests: {
    // 07、08 两章不在掘金那份配置覆盖的范围内(掘金从 09 起),各自单独写一条
    '07':
      '从静态批处理的空转问题讲起，拆解连续批处理为什么快：调度器每一步在做什么、prefill 与 decode 如何混批、chunked prefill 与抢占的取舍，并用 mini-vLLM 的代码和实测数据佐证。',
    '08':
      '拆解 vLLM 的性能账：PagedAttention 内核为什么快、KV cache 显存怎么算、CUDA Graph 与连续批处理的吞吐延迟权衡，以及投机解码与并行度选择。',
    ...juejin.briefs,
  },

  // 拆篇的生效上限,默认 WECHAT_CONTENT_LIMIT(50 万)。
  // 那是护栏、不是接口上限 —— 接口实测能收 18 万字符且原样回读,所以
  // 正常一章一期,这一项不会触发。见 src/publish/markdown.ts 的说明。
  // contentLimit: 400000,

  delayMs: 3000,
  timeoutMs: 15000,
};

export default config;

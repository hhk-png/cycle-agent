# 第 29 章 多模态、VLM、Omni 与扩散模型

## 29.1 多模态 RL 的额外维度

文本 RL 的样本是 token 序列；多模态样本是：

```text
(images/videos/audio) + text prompt -> text response
```

这带来：

- 视觉 token 与文本 token 的混合序列；
- processor 而不是 tokenizer；
- 图像分辨率/视频帧数影响序列长度；
- rollout 引擎需要支持多模态输入；
- reward 可能依赖图像内容；
- 显存与吞吐显著变化。

verl 支持 Qwen2.5-VL、Kimi-VL 等 VLM，以及 Qwen-Omni 等 omni 模型；扩散/机器人方向由 verl-omni、verl-vla 扩展。

## 29.2 数据格式

多模态样本的 message content 是列表：

```json
{
  "role": "user",
  "content": [
    {"type": "image", "image": "path/to.jpg"},
    {"type": "text", "text": "What is in this image?"}
  ]
}
```

`RLHFDataset.process_vision_info` 抽取图像/视频：

```python
images, videos = process_vision_info(messages, image_patch_size=14, return_video_metadata=True)
```

然后用 `processor.apply_chat_template` + `processor(...)` 生成 `input_ids`、`pixel_values`、`image_grid_thw` 等。

配置字段：

```yaml
data:
  image_key: images
  video_key: videos
  audio_key: audios
  image_patch_size: 14
  return_multi_modal_inputs: true
  mm_processor_kwargs: {}
```

## 29.3 视觉 token 的长度

图像先切成 patch，再按模型决定是否做 patch merge。设 patch 边长为 $p$、merge 倍数为 $m$：

$$
\text{visual tokens}\approx\frac{H}{p\cdot m}\times\frac{W}{p\cdot m}.
$$

- 只切 patch（$m=1$）：1024×1024、$p=14$ 约有 5349 个 patch；
- Qwen2.5-VL 默认做 2×2 merge（$m=2$，等效 patch 28）：1024×1024 约 $(1024/28)^2\approx1337$ 个视觉 token。

两者差 4 倍，讨论显存/KV 预算时必须写清楚是 merge 前还是 merge 后。视频更多：帧数 × 每帧 token。

这直接导致：

- 序列长度爆炸；
- attention 显存爆炸；
- rollout 吞吐下降；
- 需要 resize/裁剪/帧采样。

Qwen-VL 系列用 `image_grid_thw` 描述网格；processor 会把图像转成视觉 token，并记录 merge 后的网格尺寸。

## 29.4 多模态 rollout

rollout 引擎必须支持：

- 图像/视频输入；
- 视觉编码器；
- 多模态 KV cache；
- 与文本 token 混合的 attention mask。

vLLM/SGLang 对 VLM 的支持程度不同；verl 的 `vllm_rollout`/`sglang_rollout` 处理输入构造与多模态数据传递。

难点：

- 图像预处理在 CPU，可能成为瓶颈；
- 不同模型的 processor 不兼容；
- 多模态输入的 batch 需要 padding/打包；
- 权重同步要包含视觉塔。

## 29.5 多模态训练

FSDP 对 VLM 的 monkey patch 需要处理：

- 视觉塔（ViT）与语言模型的联合分片；
- 有无图像的混合 batch；
- 视觉 token 的 mask；
- 冻结视觉塔（`freeze_vision_tower`）。

verl 的 `model.freeze_vision_tower` 可以冻结视觉编码器，只训练语言部分；这对 RL 很常见，因为视觉理解通常不是 RL 要提升的能力。

## 29.6 多模态奖励

奖励可以：

- 只依赖文本答案（数学/OCR）；
- 依赖图像内容（检测/定位）；
- 依赖生成图像（扩散）；
- 依赖视频动作（VLA）。

对图像生成任务，奖励可能是：

- CLIP score；
- 人类偏好模型；
- 任务特定验证器；
- 物理约束。

## 29.7 Omni 与扩散

- **Qwen-Omni**：文本/图像/音频/视频输入，文本/音频输出；需要特殊的 tokenizer/processor 与生成循环。
- **verl-omni**：扩散模型 RL（DPO/GSPO），支持 Qwen-Image-Edit、LTX 等；与文本 RL 的数据流不同（去噪步、时间步、噪声预测）。
- **verl-vla**：视觉-语言-动作，面向机器人与仿真环境；轨迹包含动作 token 与环境状态。

这些扩展通常不在主库，而是独立仓库，基于 verl 的 worker/engine 抽象。

## 29.8 多模态的工程权衡

| 选择 | 优点 | 缺点 |
|---|---|---|
| 冻结视觉塔 | 省算力、稳定 | 视觉能力不提升 |
| 高分辨率 | 细节好 | token 多、显存高 |
| 视频多帧 | 时序信息 | 成本线性增长 |
| 图像 resize | 快 | 细节丢失 |
| 多模态 packing | 吞吐高 | 实现复杂 |

## 29.9 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| processor 报错 | 模型/processor 版本不匹配 | 固定版本 |
| 视觉 token 过多 OOM | 分辨率/帧数 | resize、降帧 |
| 图像不生效 | 输入字段/路径错 | 打印 processor 输出 |
| 训练/推理不一致 | processor 参数不同 | 统一配置 |
| 视频解码失败 | 缺 ffmpeg/torchcodec | 安装依赖 |
| 权重同步漏视觉塔 | 参数名过滤 | 检查 state dict |

## 29.10 MiniVerl 的边界

MiniVerl 只实现文本；它用 `Token` 序列与 TinyTransformer 演示 RL 系统。多模态部分在文档中讲清契约，但不提供可运行实现。原因：

- 多模态需要真实的视觉编码器与 processor；
- CPU 上跑不动；
- 教学重点是 RL 系统，而不是视觉模型。

如果你要扩展 MiniVerl，建议：

1. 在 `Sample` 中加 `images` 字段；
2. 在 `collate_fn` 中调用 processor；
3. 在模型 forward 中拼接视觉 token；
4. 在 mask 中标记视觉 token 为 0（不参与策略损失）。

## 29.11 一个最小 VLM RL 配方

下面是一个“能开始调试”的示意配置，不是性能最优解；字段以 v0.9.1 生成配置与模型 processor 为准。

```yaml
actor_rollout_ref:
  model:
    path: Qwen/Qwen2.5-VL-3B-Instruct
    freeze_vision_tower: true      # 先冻结视觉塔，降低显存与变量
  actor:
    use_kl_loss: true
    kl_loss_coef: 0.001
  rollout:
    name: vllm
    tensor_model_parallel_size: 1
    gpu_memory_utilization: 0.5
    enable_chunked_prefill: true
data:
  image_key: images
  return_multi_modal_inputs: true
  max_prompt_length: 2048
  max_response_length: 1024
  mm_processor_kwargs:
    max_pixels: 1003520            # 控制视觉 token 上限，按模型调整
```

调试顺序：

1. **processor 对齐**：同一张图，训练侧与 rollout 侧必须用同一 processor 版本与参数；比较 `input_ids`、`pixel_values`、`image_grid_thw`。
2. **视觉 token 计数**：打印每样本的视觉 token 数，确认没有因分辨率/帧数导致 p99 爆炸。
3. **mask**：视觉 token 不参与策略损失；只有 assistant 文本 token 的 `response_mask=1`。
4. **权重同步**：视觉塔参数必须被导出/同步；只同步语言模型会出现“图像不生效”。
5. **reward**：先只用文本答案（OCR/数学）验证链路，再加图像依赖的奖励。

支持矩阵（定性，版本敏感）：

| 能力 | 状态 | 备注 |
|---|---|---|
| vLLM VLM rollout | 支持主流 VLM | 模型/版本兼容性要查 vLLM 文档 |
| SGLang VLM rollout | 支持主流 VLM | 多图/视频支持视版本 |
| FSDP + VLM | 支持 | monkey patch/processor 要匹配 |
| Megatron + VLM | 视模型 | 需要权重映射与视觉塔处理 |
| LoRA（只训练语言层） | 常见 | 视觉塔冻结；adapter 目标模块按后端命名 |
| 多图/视频 | 视模型/引擎 | token 数与显存增长最快 |

一个常见“假成功”：训练 loss 正常下降，但 rollout 侧的 processor 没传图，模型只看到了文本。验收必须包含“同一图文样本，训练侧与 rollout 侧的输出/注意力确实用到图像”。

## 本章小结

- 多模态 RL 的核心变化是 processor、视觉 token、混合 mask 与 rollout 支持。
- 视觉 token 数量随分辨率/帧数增长，直接影响显存与吞吐。
- 冻结视觉塔是常见的 RL 选择。
- Omni/VLA/扩散是 verl 生态的扩展方向，数据流与文本 RL 不同。
- MiniVerl 专注文本，但文档给出了扩展点。

## 延伸阅读

- Qwen2.5-VL：<https://github.com/QwenLM/Qwen2.5-VL>
- verl-omni：<https://github.com/verl-project/verl-omni>
- verl-vla：<https://github.com/verl-project/verl-vla>
- 下一章：LoRA、量化、MoE 与大模型训练。

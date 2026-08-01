# 15 · 多模态与 LoRA 推理

> 本章目标：让一个 vLLM 服务不止能"读文字"。你会理解多模态 LLM 在推理引擎里如何组织输入、vLLM 如何支持视觉语言模型，以及 LoRA 适配器如何让一个底座服务多个业务。这两项能力把 vLLM 从"文本生成引擎"扩展成"多任务推理平台"。

---

## 1. 多模态推理概览

### 1.1 什么是多模态 LLM

多模态 LLM（Multimodal LLM，如 LLaVA、Qwen2-VL、Llama-3.2-Vision、Phi-3.5-Vision）除了文本，还能接受**图像、音频、视频**输入。典型结构：

```
文本 → tokenizer → 文本 token
图片 → 视觉编码器（Vision Encoder）→ 视觉特征 → Projector → 视觉 token
音频/视频 → 各自编码器 → 特征 → 投影
        ↓ 全部拼成
[文本token | 视觉token | ...]  → 主干 LLM → 输出
```

关键点：**主干 LLM 仍然只"认识" token**。多模态的关键是把非文本输入转成"token 序列"（视觉 token / 音频 token），拼进主干输入。视觉编码器 + projector 通常和主干一起在训练时联合微调。

### 1.2 为什么推理引擎要支持

- **使用面广**：文档理解、截图问答、图表解析、视频摘要，都是真实生产需求；
- **一份服务承载多任务**：文本 + 视觉 + 工具调用可以共用一个推理进程；
- **生态要求**：OpenAI 的 `chat/completions` 已支持 `image_url` 输入，多模态是兼容标准的一部分。

---

## 2. vLLM 的多模态架构

vLLM 的多模态支持位于 `vllm/multimodal/` 与模型目录下的多模态实现（如 `vllm/model_executor/models/llava.py`、`qwen2_vl.py`）。

### 2.1 核心抽象

```
MultiModalRegistry（vllm/multimodal/registry.py）
 ├── 注册"输入类型（image / audio / video）→ 处理函数"
 ├── 每个多模态模型声明自己支持哪些输入类型
 └── 请求进来时，按类型调用对应的 HfProcessor 预处理

inputs 抽象（vllm/inputs/）
 └── 把"图片/音频/视频"包装成引擎认识的输入对象，附带数据与配置
```

一个请求的生命周期：

```
OpenAI 请求：content: [{"type":"text",...}, {"type":"image_url", "image_url":{"url":...}}]
  → 服务层提取图像数据（base64 / URL 下载）
  → MultiModalRegistry 找到该模型的处理器
  → HfProcessor 预处理：缩放、归一化、tile 切分、得到 pixel_values
  → 与文本 token 一起组装成 model_input
  → 模型 forward：视觉编码器 → projector → 拼接 token → 主干生成
```

### 2.2 多模态输入的两条执行路径

| 方案 | 做法 | 特点 |
|---|---|---|
| **先编码成 token 再走主干** | 视觉编码器在 GPU 上把图片变成视觉 token，与文本 token 拼在一起进主干 | 主流做法（LLaVA、Qwen2-VL）；视觉 token 也占 KV cache |
| **交叉注意力（cross-attention）** | 主干每一层都对视觉特征做注意力 | Flamingo 等架构；vLLM 支持较少 |

> 实际影响：一张 336×336 的图在 LLaVA-1.5 里产生 24×24 = 576 个视觉 token；Qwen2-VL 等新模型会按分辨率动态切块（每块固定 token 数），图片越大视觉 token 越多。这些 token 同样参与 KV cache 与连续批处理调度。**长图片 + 长文本会显著增大 KV 占用**，容量规划要一起算。

### 2.3 多模态 token 数量计算

多模态请求的 token 数 = **文本 token + 视觉/音频 token**。视觉 token 数由"分辨率 → 编码器切块"决定，不同模型差别很大：

| 模型 | 视觉 token 计算方式 | 336×336 图示例 |
|---|---|---|
| LLaVA-1.5 | 固定网格：图缩放到 336×336，切成 `24×24` 个 patch，每个 patch 一个 token | 24×24 = **576** |
| LLaVA-NeXT | 支持更高分辨率，多块（grid）拼接 | 数千 token |
| Qwen2-VL | **动态切块（dynamic tiling）**：按长宽比切若干块，每块固定 `28×28=784` 个 patch；并对相邻行做 token 合并压缩 | 约 **784 × 块数**（块数随长宽比变化，规则图更省） |

**为什么这对 KV cache / 容量规划重要**（公式与预算详见第 17 章附录 B）：

- 每个视觉 token **同样参与 KV cache** 与连续批处理调度——`KV cache 总量 ∝ (文本 token + 视觉 token) × 并发数`；
- 一张 576 token 的图 ≈ 576 个文本 token 的 KV 占用。**4 张图 ≈ 2300 token 的 KV**，长图/多图请求会显著挤占 KV 预算；
- 并发规划要按"**最坏情况的视觉 token 数**"（而不是平均）预留，否则高并发 + 多图会 OOM；
- `--max-model-len` 要把视觉 token 一并计入：**max_model_len ≥ 文本 + 视觉 + 生成上限**，否则长图请求会被拒。

**`--limit-mm-per-prompt`：限制每个请求的多模态数量**

```bash
vllm serve Qwen/Qwen2-VL-7B-Instruct \
  --limit-mm-per-prompt image=4      # 每请求最多 4 张图
  --limit-mm-per-prompt video=1      # 每请求最多 1 个视频
```

- 可同时指定多种模态，如 `image=4 video=1`；
- 超限的请求会被**拒绝**（返回错误）而不是默默截断——避免"误用/恶意请求塞几千张图打爆 KV cache"；
- 它是**容量保护与防滥用**的第一道闸：即使上游网关漏了，引擎侧也能兜底（与第 10 章"引擎参数负责保命"是同一思路）。

---

## 3. 常见多模态模型与用法

### 3.1 支持的模型

vLLM 对主流视觉语言模型开箱即用（版本支持以官方文档为准）：

| 模型系列 | 架构 | 输入 |
|---|---|---|
| LLaVA / LLaVA-NeXT | LLM + CLIP 视觉编码器 | 图像 |
| Qwen2-VL / Qwen2.5-VL | LLM + 原生视觉编码器 | 图像、视频 |
| Llama-3.2-Vision | Llama 3.2 多模态版 | 图像 |
| Phi-3.5-Vision / Phi-4-multimodal | Phi 系列多模态版 | 图像 |
| InternVL、MiniCPM-V 等 | 社区流行架构 | 图像 |

### 3.2 Python API 用法

```python
from vllm import LLM, SamplingParams
from vllm.multimodal import MultiModalDataDict

llm = LLM(model="Qwen/Qwen2-VL-7B-Instruct")

prompt = "这张图片里有什么？"
# 图片可以是 base64 / 本地路径 / PIL Image / numpy array
image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}

outputs = llm.generate(
    {
        "prompt": prompt,
        "multi_modal_data": {"image": image},
    },
    SamplingParams(temperature=0.2, max_tokens=128),
)
print(outputs[0].outputs[0].text)
```

### 3.3 OpenAI 兼容 API 用法

```bash
vllm serve Qwen/Qwen2-VL-7B-Instruct \
  --host 0.0.0.0 --port 8000
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="EMPTY")

resp = client.chat.completions.create(
    model="Qwen/Qwen2-VL-7B-Instruct",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "这张图片里有什么？"},
            {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
        ],
    }],
    max_tokens=128,
)
print(resp.choices[0].message.content)
```

要点：

- `image_url.url` 支持 **http(s) URL 或 base64 data URL**；
- 图片会在服务端被下载/解码，走 `HfProcessor` 预处理；
- 多模态请求与普通请求**共用同一个引擎、调度器与 KV cache**——连续批处理对视觉 token 同样生效；
- 长图/多图会显著增加 prefill 的 token 数，TTFT 也随之上升，必要时配 chunked prefill。

#### 视频与音频输入

支持视频 / 音频的模型（Qwen2-VL、Qwen2.5-VL、Phi-4-multimodal、Qwen2-Audio 等）用法类似，只是 `multi_modal_data` 的 key 与数据形态不同：

```python
from vllm import LLM, SamplingParams

llm = LLM(model="Qwen/Qwen2.5-VL-7B-Instruct")

# 视频：可以传文件路径 / base64；帧采样由 HfProcessor 完成
outputs = llm.generate(
    {
        "prompt": "这段视频里发生了什么？",
        "multi_modal_data": {"video": "https://example.com/clip.mp4"},
    },
    SamplingParams(temperature=0.2, max_tokens=128),
)

# 音频：路径 / bytes
outputs = llm.generate(
    {
        "prompt": "把这段语音转成文字。",
        "multi_modal_data": {"audio": "/path/to/audio.wav"},
    },
    SamplingParams(max_tokens=128),
)
```

要点：

- 视频的帧采样数量、音频的采样长度直接影响视觉/音频 token 总量——`--limit-mm-per-prompt video=1 audio=2` 可限制每请求的模态数量，防恶意/超长输入打爆 KV；
- Qwen2.5-VL 等新模型用**动态分辨率 + spatial merge**（每块固定 token 数），分辨率越高视觉 token 越多（详见下文 §3.6）；视觉 token 合并策略见下节。

### 3.4 与 mini-vLLM 的对照

mini-vLLM 的 `model.py` 只有文本输入（字符 token）。多模态的"工程本质"其实并不神秘：

```
mini-vLLM 的 forward(x, positions, seqs, block_manager)
  x = wte[token_ids] + wpe[positions]        # 纯文本 token

多模态模型的 forward(input_ids, positions, pixel_values, ...)
  text_tokens = wte[token_ids]                # 文本 token
  image_tokens = projector(vit(pixel_values)) # 视觉 token（另一条编码路径）
  x = concat([text_tokens, image_tokens], dim=seq)
```

**对引擎来说，输入只是"token 序列变长了"**。KV cache、PagedAttention、连续批处理、调度全部复用——这就是为什么 vLLM 能用"一套引擎 + 每个模型一个 forward"支持所有多模态架构（对应第 11 章的自定义模型接入）。

### 3.5 扩展练习：给 mini-vLLM 加一条"图像 token"路径

mini-vLLM 是纯文本的，但它的架构让"加一条图像路径"在概念上非常直接。这个练习的目标：**理解"多模态 = 在 token 序列里插入一段虚拟 token"**，从而把本章"引擎复用"的说法变成可跑的证据。

```
现状：文本 token ids → wte 查表 → 与位置编码相加 → transformer

加图像的最小改动：
1. 定义 N=64 个"图像 token id"（如 1000–1063，落在真实词表外）
2. 请求带 multi_modal_data 时，把图像 token id 插入到对应位置
3. 让 wte 对越界 id 返回一个可学习的嵌入（或随机初始化），当作视觉向量
4. 主干 transformer 完全不用改——它看到的只是"多了一些 token"
```

- 这个"玩具实现"没有真正的视觉编码器（所以不对齐语义），但它**精确复刻了引擎视角**：多模态请求 = "token 序列变长 + 前向里多插一段向量"；
- 扩展方向：把第 3 步换成"用一个小卷积/ViT 把 `pixel_values` 映射成 64 个向量"，就接近 LLaVA 的真实结构了；
- 验证：对比"有图像 token"与"无图像 token"的输出差异，观察 KV cache 占用如何随图像 token 数增长——这就是 §2.3"视觉 token 占 KV"的直接证据。

> 想做真多模态，参照第 11 章 §11.7 的"自定义模型接入"流程：在注册的模型类里实现 `input_processor`（把图像数据转成视觉 token）与多模态 forward 即可，引擎其余部分全部复用。

### 3.6 视觉 token 压缩 / 合并策略

视觉 token 数量直接决定 KV 与 prefill 成本，所以各代模型都在"用更少的 token 表达同一张图"：

| 模型 | 压缩手段 | 效果 |
|---|---|---|
| LLaVA-1.5 | 固定 336×336 网格 | 24×24 = 576 token（与分辨率无关） |
| **Qwen2-VL** | **动态切块 + 相邻行 2×2 merge** | 按长宽比切块，并对相邻行合并，长图不线性膨胀 |
| **Qwen2.5-VL** | 动态分辨率 + **`spatial_merge_size`**（如 2×2 的 token 块合并为 1）+ 新视觉编码器（更大 patch） | 同样信息用更少 token；分辨率越高仍越多，但斜率更缓 |

- Qwen2.5-VL 的 `architectures` 字段是 `Qwen2_5_VLForConditionalGeneration`（与 Qwen2-VL 不同），视觉编码器与 merge 逻辑都变了，加载时不要混用；
- 想调分辨率/切块行为：`--mm-processor-kwargs`（如 `{"min_pixels":..., "max_pixels":...}`）控制动态分辨率的上下限，进而影响视觉 token 总量；
- **工程含义**：容量规划时按"最坏分辨率"估视觉 token（第 17 章附录 B），`--max-model-len` 要计入视觉 token；长图 + 多图是 KV 的第一大意外消耗源。

---

## 4. LoRA 适配器推理

### 4.1 为什么需要 LoRA

微调大模型成本高（全参微调 70B 要大量显存）。**LoRA（Low-Rank Adaptation，低秩适配）**只训练一小部分低秩增量矩阵，插入到权重旁：

```
W' = W + BA        # W 是冻结的原权重，B、A 是低秩矩阵（r 通常 8–64）
                    # BA 维度 = (out, r) × (r, in)，参数量远小于 W
```

推理时，多个不同任务的 LoRA 可以**共享同一个底座模型**：

- 底座（base model）只在显存里放一份；
- 每个 LoRA 适配器只有几十 MB ~ 几百 MB；
- 请求动态指定"用哪个适配器"，引擎在 forward 时把对应 LoRA 增量加上。

这让"一个底座、多个业务"成为可能：同一台机器服务 SQL 助手、客服 bot、摘要器，各自挂不同 LoRA，而底座权重只加载一次。

### 4.2 vLLM 的用法

```bash
vllm serve meta-llama/Llama-3.1-8B-Instruct \
  --enable-lora \
  --lora-modules sql-lora=my-org/sql-lora \
  --lora-modules summarizer=my-org/summarizer-lora \
  --max-loras 4 \
  --max-lora-rank 64
```

调用时通过模型名选择适配器：

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="EMPTY")

# 用 sql-lora 适配器处理 SQL 任务
resp = client.chat.completions.create(
    model="sql-lora",          # 模型名 = --lora-modules 里注册的名字
    messages=[{"role": "user", "content": "把这句话转成 SQL：找出 5 月的订单"}],
)
```

### 4.3 关键参数

| 参数 | 作用 | 说明 |
|---|---|---|
| `--enable-lora` | 开启 LoRA 推理 | 关闭时忽略 lora-modules |
| `--lora-modules` | 注册适配器（名字=路径，可多个） | **启动时静态注册**；运行时没有"动态添加适配器"的 API，只能驻留/换出已注册的（受 `--max-loras` 限制） |
| `--max-loras` | 同时驻留显存的 LoRA 数 | 越多越占显存；超出按 LRU 换出（可配 `--max-cpu-loras` 把换出的放 CPU） |
| `--max-lora-rank` | 允许的最大 rank | 训练时 r 多大，推理时就要支持到多大 |
| `--lora-dtype` | LoRA 权重精度 | 默认随模型；可单独 `auto` / `float16` / `bfloat16` |
| `--long-lora-scaling-factors` | 长上下文 LoRA 缩放 | 超长序列场景 |

### 4.4 LoRA 的工程细节

- **显存**：每个加载的 LoRA 都要在显存里放增量权重（rank 越大越占）。`--max-loras` 限制同时驻留的数量，超出按需换入换出。
- **调度**：同一批请求可能挂不同的 LoRA。vLLM 的调度器会把"同 LoRA 的请求"尽量聚在同一 batch，减少权重切换。
- **与量化/多模态组合**：LoRA 可以与量化（`--quantization`）和多模态底座组合使用，但要求适配器与底座的架构、tokenizer 兼容。
- **与微调对比**：LoRA 训练快、适配器小、切换灵活；但效果通常略逊于全量微调，且低 rank 表达能力有限。选型看任务复杂度与成本。

### 4.5 LoRA 推理内核：SGMV 与批处理

**LoRA 前向的数学**：`W' = W + BA`，推理时输出 = `xW + xBA`。`xW` 用主权重 kernel；`xBA` 是低秩增量，需要专门处理——它对性能影响很大：

- 每个 LoRA 的 `A、B` 是**稠密小矩阵**，但挂不同 LoRA 的请求混在一个 batch 里时，权重变成"**分段稠密**"；
- 朴素实现（把不同 LoRA 补零成大稠密矩阵再乘）会浪费大量算力与显存。

**vLLM 的解法：SGMV（Structured-Gathered Matrix-Vector）类 kernel**

把"同一 batch 里每个序列挂不同 LoRA"看作一个**结构化稀疏矩阵 × 向量**问题——只有"该序列所属 LoRA 对应的段"是活跃的。SGMV 系列 kernel（`sgmv` / `sdgmv` / `bgmv` 等，位于 `vllm/model_executor/layers/lora`）正是为这种"分段的低秩增量"设计的：

| kernel | 适用 | 作用 |
|---|---|---|
| `sgmv` | 一个序列一个 LoRA（Single） | 按段 gather 后乘低秩增量，只算活跃段 |
| `bgmv` / `bdgmv` | 多个序列共享 LoRA（Batched） | 批量处理共享适配器的序列 |
| `dense` 相关 kernel | 单 LoRA 大 batch | 退化为稠密路径，接近普通推理 |

它们把"零填充"省掉，只计算活跃段，显著提升**多 LoRA 混批**的吞吐。

**LoRA 批处理与 `--max-loras`**：

- `--max-loras` 决定**同一时刻驻留显存**的适配器数量上限（越多越占显存）；
- 请求超过 `--max-loras` 时，适配器**按需换入换出**（加载/卸载）；
- 调度器会把"挂同一 LoRA 的请求"尽量聚到同一个 batch，减少权重切换与 kernel 分段——这也是"并发高时 LoRA 吞吐没有线性下降"的原因。

**LoRA 与量化的兼容性**：

- LoRA 可以作用于量化底座（`--quantization` GPTQ / AWQ / FP8 等），因为增量 `BA` 仍以**高精度**（fp16/bf16）计算，再与量化主权重的结果相加；
- 前提：适配器的**架构、hidden size、层数**必须与底座一致，tokenizer 一般也要求一致；
- 组合用法是"**一个量化底座 + 多个 LoRA**"的企业标配：底座省显存、LoRA 灵活切换业务（呼应第 10 章 §10.10 多租户）；
- 注意：量化 + LoRA 的组合、精度与推理路径较多，**以具体版本的官方文档与模型仓库说明为准**。

---

## 5. 进阶特性：结构化输出与 Embedding（回顾与串联）

第 05 章讲了引导解码（结构化输出），第 11 章讲了 embedding 模型。把本章的 LoRA/多模态串起来，一个 vLLM 进程可以同时提供：

| 能力 | 机制 | 典型场景 |
|---|---|---|
| 文本生成 | 主干 LLM 自回归 | 聊天、写作 |
| 多模态输入 | 视觉编码器 + 主干 | 截图问答、文档理解 |
| LoRA 适配器 | 低秩增量切换 | 多业务共用一个底座 |
| 结构化输出 | FSM 引导解码 | 严格 JSON、函数调用 |
| Embedding | 最后一层 pooling | RAG 检索 |

这些能力**共用同一套引擎与调度器**，这就是 vLLM 作为"推理平台"而不是"文本生成工具"的定位。

---

## 6. 小结

- **多模态 LLM = 主干 LLM + 模态编码器 + projector**，把图像/音频/视频转成 token 拼进输入；引擎层面输入只是"token 变长"。
- vLLM 通过 `MultiModalRegistry` + `HfProcessor` 组织多模态输入，对 LLaVA / Qwen2-VL / Llama-3.2-Vision 等主流模型开箱即用。
- 多模态请求走同一套调度与 KV cache；**图片 token 也占显存**，容量规划要计入。
- **LoRA = W + BA 低秩增量**，多个适配器共享一个底座；vLLM 用 `--enable-lora --lora-modules` 支持，按请求切换适配器。
- 多模态、LoRA、结构化输出、Embedding 共同构成 vLLM 的"多任务推理平台"能力。

下一章《16-附录A-环境变量与参数参考.md》是一份查手册：环境变量与 CLI 参数按主题速查。之后是《17-附录B-内存估算与容量规划.md》（算显存、规划并发）与《18-附录C-mini-vLLM完整工程参考.md》（mini-vLLM 的 API、验证矩阵与扩展练习）。

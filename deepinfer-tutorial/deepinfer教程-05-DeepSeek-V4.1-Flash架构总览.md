# 第 5 章 DeepSeek-V4.1-Flash 架构总览

本章导读：DeepInfer 的很多设计只有放回 V4.1-Flash 的模型结构里才讲得通。本章先给出公开资料中的模型规格，再解释 CED、CSA2、Engram、mHC、MoE 和 DSpark 各自解决什么问题，以及它们如何改变推理系统的瓶颈。最后给出一张“模型结构 -> 引擎机制”的对应表。

## 5.1 规格与定位

根据 DeepSeek 公开的技术报告与社区解读，V4.1-Flash 是一个多模态 MoE 模型：约 552B 的 backbone 参数，外加约 196B 的 Engram 条件记忆参数，总参数规模约 748B；每个 token 在 prefill 阶段激活约 8B 参数，在 decode 阶段激活约 16B。它支持 1M token 上下文，预训练使用了约 45T 多模态 token。

backbone 是 40 层 Transformer，但不是一个普通的 40 层堆叠，而是 **Causal Encoder-Decoder（CED）**：前 20 层是 causal encoder，后 20 层是 decoder。decoder 的全局 KV 不是每层重新算，而是从 encoder 最后一层的 hidden state 投影出来。这个设计是理解 V4.1 推理成本的关键。

MoE 部分每层有 1 个共享专家和 384 个路由专家，每个 token 激活 top-6 个路由专家。hidden size 是 5120，注意力有 64 个 query head、1 个 KV head。公开资料还提到部分层使用滑动窗口注意力（SWA），其余层使用 CSA2（Compressed Sparse Attention 2），窗口大小为 128；不同来源对“哪些层用 SWA”的描述并不完全一致，本书只依赖“存在 SWA 窗口层和 CSA2 稀疏层”这个结构性结论，不把具体层号当作定论。

这里有一个容易卡住的数字关系：hidden 5120 配 64 个 query head，如果按标准 MHA 的 `d = H × d_h`，head_dim 应该是 80；但本书第 4、6 章的计算例子统一用 `d_h=128`。原因是 V4.1 使用 MLA/CSA2，Q/KV 的投影宽度和标准 MHA 不同，公开资料对 head_dim 的表述也有差异。`d_h=128` 是贯穿全书的示意值，用来把公式算通，不是 V4.1 的精确规格；读公开规格时不要把这两个口径混在一起。

把这些数字放在一起，可以得到几个对推理系统最重要的推论。第一，552B 的 backbone 不可能放进单卡，必须 EP/DP。第二，196B 的 Engram 不是每 token 稠密计算，而是按哈希查表，瓶颈是存储和传输，不是 FLOPs。第三，40 层中只有 20 层 encoder 需要完整 causal 注意力，decoder 共享一份 global KV，KV 管理的粒度从“每层”变成“每类”。第四，384 个专家、top-6 意味着路由和 all-to-all 是常态，不是边角料。

这些数字来自公开材料，不同来源在个别细节上可能有差异。本书在涉及规格时尽量使用“公开资料显示”这样的限定语；涉及 DeepInfer 代码行为时，则以仓库代码为准。两者不要混为一谈。

## 5.2 为什么是 Causal Encoder-Decoder

标准 decoder-only Transformer 的每一层都有自己的 global KV。40 层就是 40 份 global KV，每生成一个 token 都要逐层读写。CED 把模型切成两段：

```text
prompt tokens
    |
    v
[encoder layer 0..19]  causal self-attention + MoE
    |
    v
encoder final hidden H_e
    |
    +--> global K = H_e @ W_k
    +--> global V = H_e @ W_v
    |
    v
[decoder layer 0..19]  local SWA + read shared global KV + MoE
    |
    v
next-token logits
```

encoder 仍然是 causal 的，所以每个位置的 hidden 只依赖它之前的 token。decoder 的 global K/V 只从 `H_e` 投影一次，所有 decoder 层共享。这样做的直接好处是：decoder 不再为每一层维护一份完整的 global KV，prefill 时也少了 20 次 global K/V 投影和 20 份 global KV 存储。要注意，省掉的不是 attention 计算本身：每层 decoder 仍然要对共享的 global KV 做 attention，只是 K/V 的来源从“本层自己投影”变成了“一份共享投影”，`QK^T` 和 `softmax` 的计算量并没有消失。

代价是表达能力可能受限：decoder 层不能有自己的 global KV，只能通过共享投影和局部 SWA 来补充。DeepSeek 的报告认为这个取舍在 V4.1 的规模上是划算的，尤其是配合 CSA2 的稀疏索引和 SWA 的局部窗口。

对推理系统来说，CED 带来两个可观测的变化。第一，KV cache 的布局不再是“每层一份”，而是“encoder 的压缩 KV + decoder 的 SWA state”，缓存管理需要区分 block KV 和 state KV。第二，prefill 的计算量下降，但 decoder 的 SWA 需要额外的重放逻辑，这就是第 6 章的“SWA bounded replay”。

从信息流的角度看，CED 把“全局记忆”和“局部计算”分开了：encoder 负责把整个前缀压成一个可共享的全局表示，decoder 负责在生成时不断读取这个表示并叠加最近的局部上下文。它不是免费的午餐，但它把“每层都维护全局 KV”的线性成本变成了“一份全局 KV + 每层局部窗口”的常数成本。

## 5.3 CSA2：压缩、稀疏与跨层复用

CSA2 是 V4.1 的注意力核心。它把“压缩 KV”和“稀疏选择”放在一起，包含四个机制。

**压缩**：KV 不是每个 token 都存完整维度，而是通过低秩投影和可学习的压缩比（C1/C2）得到更紧凑的表示。DeepInfer 代码里只支持 C1 和 C2 两种压缩率，C4/C128 已经被移除。C1 近似恒等，C2 用带门控的低秩变换把相邻 token 的 KV 合在一起。

**稀疏索引**：每层有一个轻量的 indexer，用 MQA 结构对 query 和压缩后的 index K 打分，选出 top-k 个 block 或 token，主 attention 只在这些候选上计算。indexer 的分数不是最终 attention 分数，它只负责“选谁”，因此可以用更低的维度和更低的精度。

**层次化候选池**：公开资料提到，第一个 Full-Mode decoder 层会构建最多 2048 个 block × 8 个位置的候选池（约 16K 候选），后续层只在这个池里做 top-512 搜索。这样稀疏注意力的成本不随上下文长度线性增长。候选池是“上一层的搜索结果 + 本层的局部扩展”，它把全局搜索变成了局部搜索。

**跨层复用**：每层有一个静态模式，Full、Reindex 或 Reuse。Full 层建主 KV 和 index K；Reindex 层复用主 KV 但重建索引；Reuse 层直接复用上一层的 top-k 索引。这样可以在表达力和计算量之间做分层预算。Reuse 最便宜，但可能错过本层需要的远距离信息；Full 最贵，但索引最准。

DeepInfer 的 `v4_modules/attn.py`、`compress.py`、`index_attn.py` 和 `cross_layer.py` 就是这套机制的运行时。`ModelMetadata` 会从配置里推导出每层的压缩率、是否有 packed KV、是否使用 SWA、index top-k 等属性，再据此选择不同的输入 metadata 和 kernel 路径。

CSA2 的工程难点在于“稀疏”不是简单的 mask。候选 block 的布局、index K 的存储、top-k 的排序、跨层共享的边界、CP 下的 all-gather，都会改变 kernel 的输入形状。这也是为什么 DeepInfer 需要一整套 `prepare_tensor_input_v4` 和 `InputMetadataV4`，而不是复用通用 attention 的接口。

## 5.4 SWA 与 YoCo 有界重放

SWA（Sliding Window Attention）只让每个 query 看最近 `window_size` 个 key。V4.1 的窗口是 128。它把注意力的成本从 `O(T^2)` 降到 `O(T × window)`，但带来一个缓存问题：如果每个请求都保留完整 SWA KV，显存会随上下文线性增长；如果只保留窗口，跨请求复用时又需要重建窗口。

YoCo 的思路是“有界重放”：不持久化完整的 SWA KV，只在需要时重放最近一段 suffix。DeepInfer 的实现里有两个场景。

**Encoder SWA bounded replay**：前缀缓存的命中长度由 block KV 决定；如果 SWA KV 缺失，就从命中前缀的 `L1 - 128` 位置开始重放一个完整窗口，重建 SWA KV，而 block KV 原样复用。命中长度本身不退回 128；退的是重放起点。重放的成本是常数，不随上下文长度增长。

**Decoder SWA bounded replay**：CED 的 decoder 层没有完整的 global KV，只有 SWA。精确重建 decoder 的 SWA 需要把最后约 2560 个输入 token 过 20 层 decoder。这里的 2560 不是“20 × 128 个 token-layer”的笔误，而是依赖锥：每层窗口 128，上一层每个位置又依赖它之前的 128 个位置，20 层累积后的感受野约为 `20 × 128 ≈ 2560` 个输入 token。DeepInfer 改成只重放最后 `keep_tokens` 个 token（默认 140，最小 134），把依赖锥截断成一个常数窗口，结果只用于 decode，不进入前缀缓存。`140 / 2560 ≈ 5.5%` 说的是重放 token 数的比例，不是端到端 prefill 时间的比例；官方所说的“几乎把总 prefill 计算减半”是端到端口径，包含 encoder、attention 和其他固定开销，不等于重放 token 数减少一半。截断也意味着这是近似重建，不是精确重建；DeepInfer 把它限制在 decode 路径，不让它污染前缀缓存。

代码里的关键参数是 `DEEPINFER_YOCO_PREFILL_TRUNCATION_MODE=compact` 和 `DEEPINFER_YOCO_PREFILL_KEEP_TOKENS=140`。保留的后缀必须覆盖 SWA 窗口加上最大回溯长度（128 + 6）。`v4_modules/yoco.py` 里的 `compact_yoco_boundary` 负责在截断边界上重排 hidden、residual 和 top-k 索引。

这个设计的本质是“用近似换常数成本”。在 SWA 的语义下，窗口外的 key 本来就不参与计算，所以只重放窗口内的后缀不会改变这一层的输出；但 decoder 的全局 KV 是从 encoder 的 hidden 投影来的，截断会改变 hidden 的上下文，因此近似是有损的。DeepInfer 把损失限制在 decode 路径，不让它污染前缀缓存，这是一个很工程化的边界。

## 5.5 Engram：把“记忆”从计算里拿出来

Engram 是 V4.1 的参数大户：约 196B 参数，但它是**条件记忆**，不是每个 token 都参与稠密计算。它把 n-gram 哈希到一张巨大的 embedding 表，按 token 序列查表，再把查到的向量通过投影和门控注入主干。

DeepInfer 的 `engram.py` 展示了运行时：`CompressedTokenizer` 把原始 token 映射到压缩词表；`NgramEmbedding` 按 hash 在分片表里取 embedding；`Engram` 把查到的向量做 fused KV projection，再和 hidden states 做 gate 融合。`EngramRuntime` 用 DeepEP 的 `EngramBuffer` 在 rank 之间按 hash 拉取，表可以放在 host memory 或 INT8 存储里，不必全部驻留 HBM。

对推理系统来说，Engram 的挑战不是 FLOPs，而是**查表和传输**。哈希分布不均会导致某些表分片热点；表太大放不进 HBM，需要分层存储；跨 rank 拉取会增加 all-to-all 流量。DeepInfer 用 `engram_parallel_size` 和用户态 buffer 来处理这些，具体细节在代码和 DeepEP 里。

Engram 还改变了“参数”的语义。稠密模型的参数量约等于每 token 的计算量；Engram 的参数量可以远大于激活计算量，因为每个 token 只查少数几个 bucket。这带来一个新的优化维度：表的放置、压缩、缓存和预取。它更像推荐系统的 embedding table，而不是传统 Transformer 的 FFN。

## 5.6 MoE、mHC 与视觉编码器

MoE 的数学在第 7 章展开，这里只强调与架构相关的点：1 个共享专家 + 384 个路由专家，top-6，门控使用 sqrtsoftplus 打分和 noaux_tc 选择，带一个 load-correction bias。共享专家每 token 都算，路由专家按 token 稀疏激活。Decode 阶段 active 参数约 16B，比 prefill 的 8B 高。这个差异是公开资料的 active-parameter/compute-equivalent 口径，可能来自推测解码的 draft/MTP 头、路由统计方式或测量口径；上下文长度影响的是 attention 的 FLOPs，不会改变“激活了哪些权重”，所以不要把 16B/8B 的差异简单归因于上下文变长。

mHC（multi-head residual mixing）是 V4.1 的残差结构创新。普通 Transformer 的残差是 `x = x + f(x)`；mHC 维护多个 residual stream，并在子层之间做可学习的混合。DeepInfer 代码里的 `hc.py`、`hc_blocks.py`、`mhc_ops.py` 和 `MoeDecoderBlockHC` 就是这套结构。它让残差连接更有表达力，但也让 kernel 的输入布局更复杂。

视觉侧，V4.1 使用 DeepSeek-ViT：2D-RoPE、3×3 pixel-unshuffle、32 层左右的视觉塔，图像 patch 经过 aligner 后变成 LLM 的 token。DeepInfer 把视觉编码器（VE）作为独立阶段部署，图像在 VE 上编码，embedding 再送给 prefill。第 15 章会讲 EPD 分离。

视觉编码还有一个常被忽略的成本：图像预处理。解码、缩放、归一化、patch 切分都在 CPU/GPU 上做，和文本 tokenization 完全不同。生产系统会把这一步放在 VE 实例或独立的预处理池里，避免它阻塞 LLM 的调度。

## 5.7 DSpark 与推测头

DSpark 是 V4.1 的推测解码方案。它不只是“一个小 draft 模型”，而是包含半自回归 draft、Confidence Head、可变长验证和 block rejection sampling 的一套机制。公开资料提到 draft 通常生成 5 到 7 个 token，背后有 3 个 MTP/draft 层，专家权重用 MXFP4。

DeepInfer 的 `models/mtp/dflash.py` 里可以看到：`DFlash` 持有多个 `DFlashDecoderLayer`，`ConfidenceHead` 是一个线性投影，输入是 draft hidden 加 Markov embedding，输出每个 draft token 的接受概率；`sample_drafts` 逐步采样并存储 `MTPPredictInfo`；`run_drafts` 在 decode 时把 draft 前向、采样和 pool 写入打包成一个 CUDA Graph 单元。调度器侧的 `MTPVarlen` 根据 confidence 和硬件成本曲线决定验证长度。

“半自回归”意味着 draft 不是完全独立地并行预测，而是保留了一部分 token 之间的依赖。这让 draft 更准，但也让它更像一个小型自回归模型，前向次数更多。DSpark 的收益来自“更准的 draft + 更聪明的验证预算”，而不是单纯“多猜几个”。

## 5.8 结构到系统的映射

把本章的结构和后面的引擎章节对应起来：

- CED + CSA2 + SWA -> KV cache 分 block KV 与 state KV，prefill 需要特殊 metadata，decode 需要 sparse index 和 SWA index。
- Engram -> 额外的分片表、host/INT8 存储、按 hash 的 all-to-all。
- MoE -> EP、EPLB、all-to-all、Mega MoE kernel。
- DSpark -> draft 模型、confidence head、MTP varlen 调度、KV 回滚。
- mHC -> 更复杂的 residual 布局和 fused kernel。
- ViT -> EPD 分离中的 VE 阶段和图像 token 处理。

理解了这张映射，再读 DeepInfer 的代码就不会觉得“每个模块都在做自己的事”。它们都在为 V4.1 的某个结构服务。

还可以反过来用这张表做“如果去掉它会怎样”的思想实验。去掉 CED，decoder 要为 40 层维护 global KV，KV 和 prefill 计算都会上升；去掉 CSA2，注意力回到稠密，长上下文成本按平方增长；去掉 SWA/YoCo，前缀复用的边界会简单，但 prefill 更贵；去掉 Engram，参数量下降，但事实性记忆只能塞回 FFN；去掉 DSpark，每步只出一个 token，decode 的固定开销更难摊薄。每一个结构都在换一个维度的成本，理解这些取舍比记住规格更重要。

## 5.9 公开规格的边界

本章的数字来自技术报告、模型页和社区解读，不同来源在个别细节上可能有差异。使用时要注意四个边界。

第一，**参数量与激活量不是一回事**。552B backbone 加 196B Engram 是总参数；prefill 每 token 激活约 8B，decode 约 16B。决定显存的是总参数如何分片，决定算力的是激活参数和 top-k 路由。把 748B 直接乘以 2 FLOPs 去估算每 token 计算量，会高估约两个数量级（748/8≈94）。

第二，**专家计数有四种语义**。逻辑专家、冗余副本、物理专家、每 rank 本地专家不是同一个数。公开报道提到 decode EP64 时每张卡约 6 个路由专家加 1 个热点副本，合计约 7 个专家/卡；这与 384 个逻辑专家、top-6、EPLB 冗余的配置是一致的。具体到某一版代码，要以 `moe.py` 注释里的 logical/extra/physical/local 定义为准。

第三，**硬件支持不等于性能对等**。DeepInfer 支持 SM100 和 Ascend 950，但公开 benchmark 全部在 NVIDIA 上完成；Ascend 有 CANN 9.2、DeepGEMM-Ascend、DeepEP-Ascend 和 TileLang kernel，但没有公开的吞吐数字。batch-invariant 和 deterministic sampling 也只在 NVIDIA 上生效。跨硬件比较时，不要假设同一份配置能跑出同样的数字。

第四，**1M 上下文是能力上限，不是默认工作点**。长上下文会放大 KV、attention 和缓存压力；生产系统通常按请求长度分桶，把超长请求路由到专门的引擎。公开资料提到平均输入约 77K token，这说明长上下文是常态，但 1M 仍然是尾部而不是中位数。

这些边界不影响本章的结构性结论：CED 减少 decoder 的全局 KV，CSA2 把注意力变稀疏，SWA/YoCo 把重放限制在常数成本，Engram 把记忆从稠密计算里拿出来，MoE 把参数做大但激活做小，DSpark 提高每步产出。具体数字会变，这些取舍不会。

## 5.10 多模态推理的系统成本

V4.1 是多模态模型，图像不是“免费”的输入。它要经过解码、缩放、归一化、patch 切分、视觉编码和 aligner，最后变成 LLM 的 token。每一步都有成本。

**patch 数量**：如果 patch size 是 16、图像是 1024×1024，patch 数是 `(1024/16) × (1024/16) = 4096`。经过 pixel-unshuffle 或池化后可能减少，但仍然远大于一段文本的 token 数。一张高分辨率图片可能占几千个 token 的上下文，直接影响 KV 和 prefill 成本。

**视觉编码**：DeepSeek-ViT 有 32 层左右，patch 序列上的 attention 和 FFN 是实打实的计算。VE 通常单卡就够，但它的延迟会加到 TTFT 上；如果 VE 和 P 实例共享 GPU，会互相干扰。

**embedding 传输**：VE 产出的 embedding 要送给 prefill 实例。几千个 patch 的 hidden 是几 MB 到几十 MB；跨机传输会增加延迟和带宽。EPD 分离把 VE 独立出来，就是为了让 P/D 不必为视觉编码预留资源。

**缓存**：相同图片可能被多个请求使用；图片的哈希、预处理参数、patch 布局、embedding 都可以缓存。图片缓存和文本前缀缓存不同：它的 key 是图片内容 + 预处理配置，命中后省掉整个 VE 阶段。

**预处理**：图像解码、缩放、颜色空间转换、归一化通常在 CPU 或 GPU 上做；压缩炸弹、超大图片、畸形格式都可能耗尽内存。生产系统会限制像素数、格式白名单、解码超时，并把预处理放在独立池里。

对推理系统来说，多模态的瓶颈往往不是 LLM 的 attention，而是 VE 的吞吐、图片传输和预处理。容量规划要单独算 VE 的 GPU 数、图片的 token 预算和 embedding 带宽，不能只用文本的公式。

## 5.11 参数账：552B、196B 与没有写进标题的部分

公开资料常把 V4.1-Flash 说成“552B backbone + 196B Engram，总计约 748B”。这个加法没有错，但它省略了两块：DSpark 的 draft/MTP 层和视觉塔。DSpark 通常被描述为 3 个 MTP/draft 层，按 384 专家、top-6 的结构粗算，参数量在十几 B 量级；视觉塔按 32 层、hidden 一千多的规模，是几 B 量级。把这两块加进去，总参数会更接近 760B 到 770B，而不是 748B。不同来源对“总参数”是否包含 draft 和 ViT 口径不一致，引用时必须写明。

更值得动手算的是 backbone 内部的分配。MoE 层的主要参数是路由专家：每层 `n_experts × 3 × d × f`。用 `n_experts=384`、`d=5120`、`f=1536`，每层约 `384 × 3 × 5120 × 1536 ≈ 9.06B`，40 层就是约 362B。加上共享专家、attention 的 Q/KV/O 投影、embedding、norm 和输出头，距离 552B 还有明显缺口。要让路由专家达到约 540B，`f` 需要接近 2300；如果公开的 552B 还包含共享专家、Engram 投影或其他结构，就不能用单一的 `3 × d × f` 去反推。第 7.8 节用 `f=1536` 只是算 all-to-all 和权重的示意值，不是从 552B 反解出来的精确配置。

这个账本对推理系统有三个直接含义。第一，**总参数决定显存下限，激活参数决定单步带宽**。748B 或 763B 是权重放置问题，prefill 的 8B、decode 的 16B 是每 token 的计算/带宽问题；把 763B 直接乘 2 FLOPs 会高估每 token 计算量约两个数量级。第二，**专家数、`f` 和层数不能同时从总参数推出来**，至少有一个是独立规格；做容量规划时要用官方给出的每卡专家数或权重分片，而不是自己反推。第三，**draft 和 ViT 是额外显存**，DSpark 的 draft 层要常驻，ViT 在 EPD 分离里占独立 GPU；只按 backbone 买卡会漏掉这两块。

读公开规格时，最稳妥的做法是把数字分成四类：总参数（含不含 draft/ViT）、激活参数（prefill 还是 decode）、每卡本地专家数、KV 字节/token。四类数字各自回答不同的问题，不能互相校验出一个“精确模型”。本书在涉及这些数字时保留“公开资料显示”的限定，就是为了避免把口径差异当成事实错误。

## 小结

V4.1-Flash 不是一个“更大的 Transformer”，而是一组为了长上下文、低推理成本和多模态做的结构组合：CED 减少 decoder 的全局 KV，CSA2 把注意力变稀疏，SWA/YoCo 把窗口重放限制在常数成本，Engram 把记忆从稠密计算里拿出来，MoE 把参数规模做大但激活做小，DSpark 提高每步产出。DeepInfer 的调度、KV、并行和 kernel 都是围绕这些结构长出来的。

## 练习

1. 画出 CED 的 encoder/decoder 数据流，标出 global K/V 只投影一次的位置。
2. 解释 CSA2 的 Full/Reindex/Reuse 三种模式分别在什么情况下使用，为什么后续层可以在候选池里只搜 top-512。
3. 如果 SWA 窗口是 128，最大回溯是 6，YoCo 的 keep tokens 至少应该是多少？为什么？
4. 用“如果去掉它会怎样”的方法，分析 CED 和 Engram 各自把哪一项成本转移到了哪里。

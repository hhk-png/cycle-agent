# 第 34 章 模型接入、权重管理与版本治理

本章导读：推理引擎的性能上限，早在权重加载那一刻就被决定了：权重放在哪里、按什么格式切分、用什么精度读取、和 tokenizer 是否对齐、和 KV 格式是否兼容。本章从模型仓库的文件结构讲到 safetensors 的 mmap、分片索引、张量并行切分、量化 scale 布局、warmup 形状和 manifest 校验，最后给出 mini-deepinfer 的 `manifest.py` 实现和一套上线检查清单。读完本章，你应该能独立把一个新模型接进推理服务，而不是只会调用 `from_pretrained`。

## 34.1 模型接入到底要接什么

很多人以为“接入模型”就是加载权重。实际上，一个可服务的模型至少包含七类资产。

第一，**结构配置**：层数、hidden size、注意力头数、KV 头数、FFN 维度、RoPE 参数、窗口大小、MoE 专家数、top-k、是否共享专家、是否使用 CED/Engram/mHC。这些决定 kernel 的输入形状和显存布局。

第二，**权重**：可能是单个文件，也可能是几十个 safetensors 分片；可能是 FP16/BF16，也可能是 FP8/FP4 加 scale；可能包含 draft 模型、视觉塔、Engram 表、MTP 头。

第三，**tokenizer**：词表、合并规则、特殊 token、字节回退策略。tokenizer 与权重不匹配时，模型不会报错，只会输出乱码或错误角色。

第四，**chat template**：system/user/assistant/tool 的标记方式、是否插入 BOS、是否在 assistant 前加空格、工具调用用什么分隔符。模板错一个字符，模型的指令跟随质量就会明显下降。

第五，**generation config**：默认 temperature、top-p、top-k、重复惩罚、停止 token、最大长度。这些默认值会影响 API 行为，必须和官方一致或在文档里明确覆盖。

第六，**结构化输出资产**：grammar、JSON schema 编译器版本、特殊 token id、trigger/bound token。它们和词表绑定，换 tokenizer 就要重新编译。

第七，**manifest**：把上面六类资产的版本、哈希、兼容约束记录下来。没有 manifest，线上只能靠“我记得是这个版本”。

DeepInfer 默认 checkpoint 是 `deepseek-ai/DeepSeek-V4.1-Flash-1017`，首次运行会从 Hugging Face 下载；`DEEPINFER_HF_MODEL_DIR` 可以指定本地副本。这个环境变量只影响默认模型，不覆盖显式传入的 `--model_name`。这类“默认值 vs 显式值”的边界，是接入阶段最容易出错的地方之一。

## 34.2 safetensors 与分片索引

现代模型仓库通常用 safetensors 而不是 pickle。safetensors 的文件结构很简单：开头是一个 JSON header，记录每个张量的 dtype、shape 和字节区间；后面是连续的张量数据。读取时可以直接 `mmap`，按 offset 取张量，不需要把整个文件读进内存，也不会执行任意代码。

一个分片仓库通常有：

```text
model-00001-of-00008.safetensors
model-00002-of-00008.safetensors
...
model.safetensors.index.json
config.json
tokenizer.json
tokenizer_config.json
special_tokens_map.json
generation_config.json
```

`model.safetensors.index.json` 里的 `weight_map` 把每个张量名映射到分片文件名。加载器先读 index，再按需打开分片；如果一个 rank 只需要一部分张量，就只 mmap 对应的分片。这对多卡加载很重要：8 个 rank 各自加载自己那一份，比每个 rank 都读完整模型快得多，也省 page cache。

加载时要注意三件事。

**dtype 转换**。safetensors 里的 dtype 可能是 BF16、FP16、FP8_E4M3、FP8_E5M2、U8（打包的 FP4）。加载器要按目标设备的支持情况转换，不能假设“读进来就是 float32”。转换本身会消耗 CPU 和内存带宽，大模型加载时间里有相当一部分花在这里。

**tied embeddings**。很多模型让输入 embedding 和输出 projection 共享权重。加载时如果两个名字都存在，要确认它们是同一份数据还是两份副本；如果是共享的，反向/更新时要避免重复计算，推理时可以直接复用同一块显存。mini 的 `ModelConfig.tie_embeddings` 就是控制这件事。

**缺失与多余张量**。加载器应该严格检查：配置里要求的张量是否都存在、形状是否匹配、是否有未使用的张量。一个常见的线上事故是“新 checkpoint 多了一个 bias，但加载器忽略了它”，模型能跑，结果却和评测不一致。严格加载应该在启动时失败，而不是警告后继续。

## 34.3 分片、并行与专家放置

单卡放不下模型时，权重必须按并行策略切分。不同并行方式切的是不同维度。

**张量并行（TP）**切同一个矩阵的行或列。以 `W_q` 为例，它的形状是 `[d_model, n_heads × d_head]`，输出维度不是单个 `d_head`。列并行把输出维度切成 `world_size` 份，每个 rank 拿到 `[d_model, n_heads × d_head / world_size]`（如果按头切，就是 `n_heads / world_size` 个头）；行并行把输入维度切开，每个 rank 拿到 `[d_model / world_size, n_heads × d_head]`。切分方式必须和 forward 里的 all-reduce/all-gather 对上，否则形状能跑但数值错。

**专家并行（EP）**切 MoE 专家。每个 rank 持有 `num_physical_experts / ep_size` 个专家；逻辑专家到物理专家的映射由 EPLB 决定，可能包含冗余副本。加载器要按 `logical_to_physical` 和 `rank_experts` 的映射取权重，不能简单按专家 id 取模。DeepInfer 的 `moe.py` 注释明确区分 logical、extra、physical、local 四种计数：physical = logical + extra，local = physical / ep_size。把其中一个当成另一个，就会出现“专家数对不上”或“某个专家永远不被路由”的 bug。

**数据并行（DP）**每个 rank 有完整模型，加载的是同一份权重。DP 不减少单卡显存，但实现简单，适合模型能放进单卡的场景。

**上下文并行（CP）**切序列维度，不切权重；每个 rank 仍然持有完整层权重，只是 attention 需要跨 rank 交换 K/V。CP 的加载和 DP 类似，但并行组初始化和通信拓扑不同。

DeepInfer 的选择是 attention 用 DP、MoE 用 EP、prefill 用小 EP/DP、decode 用 Wide-EP。加载器必须知道当前实例的并行配置，才能决定每个 rank 加载哪些张量。一个实用的做法是把“张量名 -> 切分规则 -> 目标 rank”写成一张表，在加载前校验每个 rank 的显存预算和覆盖率。

## 34.4 量化权重的加载

低精度权重不是“把 dtype 改成 FP8”这么简单。以 FP8 为例，权重文件里通常有 `weight` 和 `weight_scale` 两个张量；scale 可能是 per-tensor、per-channel 或 per-block。加载器要：

1. 按 kernel 要求的布局读取 weight 和 scale；
2. 把 scale 变换成 TMA/向量化需要的对齐格式；
3. 确认 scale 的分组维度和 kernel 的假设一致；
4. 在 warmup 时用真实形状跑一遍反量化和 GEMM，检查数值范围。

FP4 更复杂。MXFP4 每 32 个值共享一个 ue8m0 scale（2 的幂），NVFP4 每 16 个值共享一个 E4M3 scale；权重可能是打包成 `uint8` 的两个 4-bit 值。加载器要负责解包、scale 布局变换和 padding。DeepInfer 的 `transform_sf_into_required_layout` 就是做这件事：把 scale factor 排成 kernel 需要的 TMA 对齐布局。

一个常见的性能陷阱是“加载时反量化成 BF16，运行时再量化回去”。这样存储省了，但加载时间和显存都没省，真正的低精度 GEMM 也没用上。正确做法是权重保持低精度驻留，反量化融进 GEMM 或 attention kernel。

量化还会影响 manifest：同一个模型 id，FP8 和 FP4 版本的 weight hash、scale 布局、kernel 要求都不同，必须作为不同的 manifest 记录，不能只靠文件名区分。

## 34.5 加载流程与 warmup

一个可复现的加载流程通常分六步。

**第一步，解析配置。** 读 `config.json`，构造 `ModelConfig`，校验 `n_heads * head_dim == d_model`、`d_model % n_kv_heads == 0`、层数/专家数/窗口大小等约束。mini 的 `ModelConfig.__post_init__` 就是这种校验的最小版本。

**第二步，读 manifest。** 先校验 dtype、量化方式、最大长度、词表大小和引擎版本这些元数据；如果权重和 tokenizer 是从显式路径加载的，再重新计算它们的 SHA-256，和 manifest 比对。只校验元数据不够：一个换了权重但词表和 dtype 相同的 checkpoint 会顺利通过。mini 的 `ModelManifest.assert_files()` 就是这层重算，LLM 在设置了 `model_path`/`tokenizer_path` 时会调用它；内存里的模型没有文件可哈希，因此跳过文件校验。任何一项不匹配都拒绝启动。

**第三步，建立并行组。** 根据 `dp/cp/ep` 初始化通信组，确定每个 rank 的 world size、rank id、本地专家数、本地张量分片。并行组初始化失败时要给出明确的拓扑信息，而不是一个超时。

**第四步，加载权重。** 按 index 找到每个张量所在分片，mmap 读取，按切分规则取本地分片，转换成目标 dtype，搬到设备。大模型通常用后台线程预取下一个分片，把 I/O 和 H2D 拷贝重叠起来。

**第五步，分配 KV 和 workspace。** 先 profile 可用显存，减去权重、激活、通信 buffer 和 CUDA Graph 预留，再决定 `num_gpu_blocks`。这个顺序不能反：先分配 KV 再加载权重，会在权重加载时 OOM。

**第六步，warmup。** 用几种典型形状跑 forward：最短 prompt、最长 chunk、最大 batch、推测解码的验证形状、结构化输出的 mask 形状。warmup 会触发 kernel 编译、CUDA Graph 捕获、内存池分配和 BLAS 线程池初始化。不做 warmup，第一个真实请求的 TTFT 会包含这些固定开销。

warmup 的形状要覆盖生产负载，而不是只跑一个 `batch=1, seq=1`。一个常见的线上问题是“warmup 只跑了 decode，prefill 的第一个长 chunk 仍然触发编译”，导致首批请求的 TTFT 抖动。

## 34.6 模型 manifest：把假设变成可检查的数据

`manifest.py` 的设计目标是把“这个服务到底在跑什么”变成一份可序列化、可校验、可比较的数据。

```python
manifest = ModelManifest.from_files(
    "mini_deepinfer/assets/tiny_lm.npz",
    "mini_deepinfer/assets/vocab.json",
    model_id="mini/tiny-lm",
    model_revision="local-2026-10",
    engine_version="0.1.0",
    dtype="float32",
    max_model_len=512,
    vocab_size=264,
    chat_template="<|bos|><|user|>{content}<|end|><|assistant|>",
    grammar_version="mini-1",
)
print(manifest.fingerprint())
```

manifest 里保存了权重、tokenizer、template 的 SHA-256，grammar 版本，引擎版本，dtype，量化方式，最大长度，词表大小和自定义字段。`fingerprint()` 对所有字段做规范化 JSON 哈希，得到一个稳定的身份；同样的内容无论 JSON 怎么排版，fingerprint 都一样。

`validate_runtime` 检查五类问题：schema 版本、词表大小、最大长度、dtype/量化方式、引擎主版本。它返回问题列表而不是直接抛异常，方便启动脚本一次性打印所有不兼容项；`assert_compatible` 则把列表变成 `ConfigError`。

```python
problems = manifest.validate_runtime(
    engine_version="0.1.0",
    vocab_size=tokenizer.vocab_size,
    max_model_len=config.max_model_len,
    dtype="float32",
)
problems += manifest.verify_files(
    model_path=config.model_path,
    tokenizer_path=config.tokenizer_path,
)
if problems:
    raise ConfigError("; ".join(problems))
```

这份 manifest 可以写进容器 label、模型仓库的 `MANIFEST.json`、部署记录的 ConfigMap，或者 3FS 的 KV metadata。回滚时比较两个 manifest 的 fingerprint，就能知道“这次回滚到底换了什么”。

`validate_runtime` 只检查 manifest 里的元数据和运行时是否一致，不会打开权重文件；真正的文件校验用 `verify_files(model_path=..., tokenizer_path=...)`，它重新计算 SHA-256 并返回不匹配项。两者要一起用：元数据校验抓“版本配错”，文件校验抓“文件被替换”。只做前者，无法发现同词表、同 dtype 的权重被换掉；只做后者，无法发现 manifest 本身和引擎不兼容。

生产 manifest 还应该记录：CUDA/CANN 版本、驱动版本、并行配置、EPLB 策略版本、kernel 库版本、tokenizer 的合并规则哈希、特殊 token id 映射。任何一项变化都可能改变输出，应该能在评测和故障复盘时被查到。

## 34.7 兼容性矩阵与启动校验

“兼容”不是一个布尔值，而是一个矩阵。至少要考虑五个维度。

**权重与结构**：checkpoint 的 `config_json` 和代码里的 `ModelConfig` 是否一致；层数、hidden、专家数、窗口大小是否匹配。

**权重与精度**：文件里的 dtype 和 scale 布局是否被当前 kernel 支持；FP4 权重能否在目标硬件上反量化；batch-invariant 路径是否支持该精度。

**tokenizer 与权重**：词表大小、特殊 token id、BOS/EOS、padding token 是否一致；chat template 是否产生模型训练时见过的标记。

**KV 与引擎**：block 大小、KV 量化格式、SWA 窗口、压缩率、indexer 维度是否和当前引擎版本一致；旧缓存能否被新版本读取。

**硬件与 kernel**：SM100/Ascend 950 的指令集、TMA/异步拷贝、通信库版本是否匹配；某些确定性开关只在 NVIDIA 上生效。

启动校验应该按这个矩阵逐项检查，并把结果写进日志。不要只检查“文件存在”和“能加载”，那只能证明最表面的一层。

## 34.8 热更新与回滚

生产模型会更新。热更新的难点不是“把新权重拷进去”，而是“在不丢请求、不破坏 KV、不混淆版本的前提下切换”。

一个安全的流程是双版本并存：

1. 新版本实例先加载权重、跑 warmup、通过健康检查，但不接流量。
2. 网关把一小部分租户或流量切到新版本，观察错误率、延迟、质量指标。
3. 新版本稳定后，逐步放量；旧版本继续服务剩余流量。
4. 全量后，旧版本进入 drain，处理完运行中的请求再退出。
5. 如果新版本异常，网关切回旧版本；旧版本的 KV 格式和 tokenizer 必须仍然可用。

KV 缓存不要跨版本共享。新版本的 KV 布局、量化 scale、SWA 状态可能不同，直接复用会读到错误数据。做法是给 KV 命名空间加 manifest fingerprint：新版本只读自己 fingerprint 下的缓存，旧缓存自然 miss，重新计算。这样回滚时旧版本还能读回自己的缓存。

回滚还要考虑 tokenizer 和 chat template。如果新版本改了模板，旧版本继续用旧模板，客户端的 prompt 可能被解释成不同角色。版本切换应该以“模型 + tokenizer + template + grammar + KV 格式”为一个原子单元，而不是只换权重。

## 34.9 常见事故

**tokenizer 不匹配。** 权重换了，tokenizer 没换，模型输出乱码；或者特殊 token id 变了，chat template 插入的控制 token 变成了普通文本。防御是 manifest 校验 tokenizer 哈希和词表大小。

**chat template 不匹配。** 模板少了一个空格、BOS 位置不同、工具调用分隔符不同，模型仍然能生成，但指令跟随质量下降。防御是把模板哈希写进 manifest，并在评测集里加入模板敏感用例。

**分片缺失或重复。** index 里有张量指向不存在的分片，或者两个分片包含同名张量。加载器要严格校验覆盖率和唯一性。

**dtype 不支持。** 权重是 FP4，但当前 GPU 或 kernel 不支持；或者 scale 布局和 kernel 假设不一致。加载时应该显式报错，而不是悄悄反量化成 BF16 后性能不达预期。

**warmup 形状不全。** 只 warmup 了 decode，prefill 的长 chunk 仍然触发编译，首批请求 TTFT 抖动。防御是按生产长度分布选 warmup 形状。

**旧 KV 被新版本读取。** 新版本改了 block 布局或量化格式，读到旧缓存后输出错乱。防御是 KV 命名空间绑定 manifest fingerprint。

**显存预算算错。** 先分配 KV 再加载权重，或者忘记算通信 buffer、CUDA Graph 内存池、激活峰值，导致加载中途 OOM。防御是先 profile、再分配，并留 10% 到 20% 余量。

## 34.10 mini-deepinfer 的 manifest 实践

mini 的 `assets/tiny_lm.npz` 不是 safetensors，而是一个 NumPy 压缩包；`config_json` 里保存了 checkpoint 的真实配置。`manifest.py` 不关心文件格式，只关心字节哈希，所以可以直接对 `.npz` 和 `vocab.json` 计算 SHA-256。

需要说明边界：`ModelManifest.from_files` 只接受单个权重文件，`weight_sha256` 也只对应一个文件。生产模型的分片仓库需要额外的 `weight_map` 覆盖率校验、每个分片的哈希列表，以及可选的 Merkle root；否则“换了其中一个分片”不会被单文件哈希发现。mini 用单文件是为了让 manifest 的逻辑可以在一屏内读完，不代表分片场景可以照搬。

运行 `python examples/manifest.py` 可以看到：

```text
manifest fingerprint: ...
weight sha256       : ...
tokenizer sha256    : ...
compatible          : True
tampered problems   :
  - vocab size 264 != manifest 300
  - runtime max_model_len 512 exceeds manifest 128
```

这个例子故意篡改了词表和最大长度，演示 manifest 如何在启动前抓住不匹配。真实项目还会把 manifest 写进镜像 label，部署时用 `kubectl describe pod` 或镜像扫描工具核对。

mini 的 manifest 没有覆盖并行配置、kernel 版本和 KV 格式，这是教学边界。读者可以把它扩展成嵌套结构：`runtime`、`parallel`、`kv_cache`、`tokenizer`、`grammar` 各一个子对象，每个子对象有自己的版本和哈希。扩展时保持 `fingerprint()` 的规范化逻辑不变，就能继续做版本比较。

## 34.11 每 rank 显存预算与分布式加载

模型能加载，不等于每张卡都放得下。一个大模型的权重在 EP/TP/DP 下会被切成很多片，每片的显存、加载带宽和顺序都不同。上线前要能做一次“每 rank 账本”。

先算权重。总参数 `N_total`、每参数字节 `b`，全局权重是 `N_total × b`；EP 只切路由专家，attention、共享专家、embedding 通常每卡复制；TP 切矩阵，DP 复制完整模型。一个粗略的每卡权重是：

```text
W_rank ≈ W_expert / EP + W_nonexpert / TP + W_embedding / TP
```

这里的 `TP` 和 `EP` 可能重叠（同一批卡的不同视图），不要简单相乘。具体到某个模型，要以 `weight_map` 和并行组的切分规则为准；公式只用于量级检查。

再算加载峰值。加载时可能同时存在“旧分片 + 新分片 + 临时反量化 buffer”，峰值高于稳态权重。分布式加载（如 `torch.distributed.checkpoint`）可以让每个 rank 只读自己需要的分片，减少主机内存和网络；但如果每个 rank 都先读完整 checkpoint 再切，网络和主机内存会先爆。加载顺序也要设计：先加载非专家权重，再加载专家；先加载小层，再加载大层；warmup 要在所有 rank 都就绪后开始，不能让先到的 rank 空转。

加载带宽可以估算。一个 100 GB 的 checkpoint，从 3FS 读到 64 个 rank，如果每个 rank 都读全量，网络流量是 6.4 TB；如果按分片读，总流量接近 100 GB。前者在 10 TiB/s 的集群上也要约 0.6 秒，后者只要几十毫秒；但分片读需要元数据、索引和更复杂的错误处理。生产系统通常把 checkpoint 预先切成与并行布局匹配的分片，加载时按 rank 直接读，避免“先全量后切”。

最后是校验。加载后要检查：每个 rank 的参数量、dtype、shape、scale 布局、专家数、tokenizer/模板 fingerprint；用一个小 warmup 输入比较 logits 或 loss，确认没有漏加载、重复加载或切错维度。第 34.7 节的兼容性矩阵和第 34.9 节的事故清单可以一起用。每 rank 账本不是一次性的文档，模型、精度、并行配置变化时都要重算；把它写进启动日志，出问题时能直接看到“哪张卡分到了多少权重”。

## 小结

模型接入不是“加载权重”，而是把结构、权重、tokenizer、模板、生成配置、结构化资产和版本清单作为一个整体接入。safetensors 的 mmap 和分片索引决定加载效率；TP/EP/DP/CP 的切分规则决定每个 rank 加载什么；量化 scale 布局决定 kernel 能不能用上低精度；warmup 形状决定首批请求的延迟；manifest 决定版本事故能不能在启动时被发现。mini 用 `manifest.py` 展示了最小可用的版本校验，生产系统则要把兼容性矩阵扩展到硬件、kernel、KV 和评测。

## 练习

1. 画出一个 safetensors 分片仓库的加载流程：从 `config.json` 到 `weight_map` 到每个 rank 的本地张量。标出哪些步骤可以并行。
2. 为什么 TP 的列并行和行并行必须和 forward 里的通信算子匹配？如果只切权重不改 forward，会发生什么？
3. 设计一个 manifest 的嵌套结构，包含 runtime、parallel、kv_cache、tokenizer、grammar 五个子对象。写出 `fingerprint()` 的规范化规则。
4. 一个 FP4 权重的 scale 是 per-32 ue8m0，但 kernel 期望 per-16 E4M3。加载时应该怎么处理？为什么不能简单地把两组 scale 平均？
5. 设计一次模型热更新演练：列出双版本并存的步骤、需要监控的指标和回滚条件。

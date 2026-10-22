# 第 45 章 端侧、边缘与异构推理

本章导读：数据中心推理假设很多请求、很多 GPU、很宽的互联和很大的显存池；端侧推理假设的几乎相反：batch 通常是 1，内存和功耗有限，网络时有时无，用户对首字延迟和隐私更敏感。本章把端侧当成一套独立的系统来讲：硬件和内存层次、量化与模型体积、roofline 与 tok/s、运行时与批处理、能耗与热、云边混合路由和评测。最后用 `mini_deepinfer/edge.py` 把这些量算成可复现的数字。

## 45.1 端侧不是“缩小版的数据中心”

把数据中心引擎直接搬到手机上，通常会遇到四个硬约束。

第一，**内存墙更低**。手机的 LPDDR 带宽可能是几十 GB/s，笔记本的 CPU 内存带宽也是一百 GB/s 量级，而数据中心 GPU 的 HBM 是几 TB/s。decode 每生成一个 token 要读一遍权重和 KV，带宽低意味着 tok/s 低，优化方向从“更多 batch”变成“更少字节”。

第二，**没有可靠的 batch**。端侧通常一次服务一个用户，batch=1；即使有并发，也很小。数据中心靠 batch 摊薄权重读取，端侧没有这个杠杆，只能靠量化、稀疏、推测解码和缓存。

第三，**功耗和热**。手机 NPU 的峰值算力很高，但持续几十秒后会降频；笔记本的 CPU 也会在长时间推理后降频。一个 10 秒的 benchmark 可能比 10 分钟的真实使用快 50% 以上。

第四，**部署碎片化**。CPU、Apple Silicon、Android NPU、Windows NPU、浏览器 WebGPU、嵌入式 GPU，指令集、内存布局、算子支持都不同。一个模型要为多个后端准备不同的量化格式和 kernel。

这四条约束决定了端侧的核心问题不是“如何服务一千个并发”，而是“如何在一个内存和功耗受限的设备上，用可接受的延迟和电量生成 token”。本章的公式都围绕这个目标。

## 45.2 硬件与内存层次

端侧设备的计算单元可以粗分成四类。

**CPU**：通用性最好，支持 AVX2/AVX-512、ARM NEON/SVE、Apple AMX 等向量指令。它的优势是“什么模型都能跑”，劣势是矩阵乘的能效比不如专用单元。CPU 推理的瓶颈通常是内存带宽和线程调度，而不是峰值 FLOPS。

**集成 GPU**：和 CPU 共享内存（Apple Silicon 的统一内存、Intel/AMD 的 APU），不需要把权重拷到独立显存，省了一次拷贝和一份内存。但共享内存的带宽由整个 SoC 共享，GPU 跑推理时 CPU 和其他单元会争带宽。

**独立 GPU**：笔记本/迷你主机的离散 GPU，有独立显存和较高带宽，但功耗和散热限制比数据中心卡严格得多。它适合 7B 到 30B 级别的量化模型。

**NPU/加速器**：手机、PC、边缘盒子里的专用矩阵单元。峰值算力/功耗比很好，但算子覆盖和内存布局受厂商限制；一个模型能不能跑，取决于它有没有被编译器支持。NPU 的“支持”不等于“每个算子都高效”，attention 的 softmax、RoPE、动态 shape 经常是回退到 CPU 的部分。

内存层次也要重新理解。数据中心关心 HBM、L2、SRAM 的带宽；端侧还要关心“权重放在哪里”：

- 权重可以 mmap 到文件，让操作系统按页加载；第一次推理会触发缺页，后续命中 page cache。这能减少启动内存，但会让首 token 变慢。
- 权重可以常驻内存，启动快，但占用的内存会挤压其他应用；手机可能直接杀掉进程。
- KV 可以放在内存，也可以换出到闪存；闪存的随机读写延迟比内存高几个数量级，换出会破坏 TPOT。

`EdgeDevice` 用 `memory_gb`、`bandwidth_gbs`、`compute_tflops`、`power_w` 四个字段描述设备，`ridge_point` 给出算力/带宽比。这个抽象足够做容量和瓶颈判断，但不能替代真实 benchmark。

## 45.3 量化与模型体积

端侧最有效的优化是量化，因为它同时降低内存占用和带宽需求。一个 `P` 参数的模型，权重字节数是：

```text
weight_bytes = P × bits / 8 × (1 + scale_overhead)
```

FP16 是 2 字节/参数，FP8 是 1 字节，FP4 是 0.5 字节。scale 的开销取决于粒度：每 16 个值一个 FP8 scale，平均增加 0.0625 字节/值；每 32 个值一个 scale，增加 0.03125 字节/值。

`quantized_weight_bytes(1_500_000_000, bits)` 的结果是：16-bit 3.000 GB、8-bit 1.500 GB、4-bit 0.750 GB。一个 1.5B 模型在 4-bit 下可以放进 8 GB 内存的设备，但还要给 KV、激活、运行时和操作系统留空间。`plan_memory` 把权重、KV、激活相加，再和 `memory_gb × (1-headroom)` 比较；headroom 默认 15%，覆盖运行时、图缓冲、tokenizer 和碎片。

量化格式的选择不只是 bit 数。端侧常见的格式有：

**GGUF / k-quants**：llama.cpp 生态的格式，支持 2 到 8 bit 的多种块量化，按行/块存 scale，适合 CPU 和 Metal。它的优势是工具链成熟、单文件分发、mmap 友好；劣势是不同量化类型对算子有要求。

**AWQ / GPTQ**：面向 GPU 的 4-bit 权重量化，保留少量“重要通道”的高精度；推理时需要对应的反量化 kernel。它们通常比朴素的 round-to-nearest 质量好，但格式和 kernel 绑定。

**QAT（量化感知训练）**：在训练时模拟量化误差，质量最好，但需要训练资源和数据。端侧产品如果对质量敏感，QAT 的 4-bit 可能优于 PTQ 的 4-bit。

**混合精度**：embedding、输出头、第一/最后一层用更高精度，中间层用 4-bit；或者 attention 的 V 用更高精度，K 用更低精度。混合精度能缓解“量化后长上下文召回下降”，但增加了格式和 kernel 的复杂度。

KV 也可以量化。端侧的上下文通常比数据中心短，但 KV 仍然占内存；4-bit KV 能把并发或上下文翻倍，代价是 attention 的数值误差。端侧量化 KV 要特别注意“首 token 质量”和“长对话稳定性”，因为用户会直接感知到答非所问。

## 45.4 Roofline 与 tok/s

端侧 decode 几乎总是带宽受限。一个 token 的步进要读：

```text
bytes_per_step = active_weight_bytes + batch × context_tokens × kv_bytes_per_token
```

如果权重是 4-bit、激活参数 1.5B，权重约 0.75 GB；4K 上下文、每 token 890 字节的 KV 约 3.6 MB。权重占绝对主导，所以：

```text
tokens_per_s ≈ bandwidth × efficiency / weight_bytes
```

`estimate_decode` 同时算内存时间和计算时间，取较大者：

```text
memory_s  = bytes_per_step / (bandwidth × efficiency)
compute_s = 2 × active_params × batch / (compute × efficiency)
step_s    = max(memory_s, compute_s)
```

`efficiency` 是“实际能达到 datasheet 的比例”，通常 0.4 到 0.6。它把反量化、attention、采样、框架开销都折进去。示例里 1.5B 4-bit、4K 上下文的结果是：笔记本 CPU 29.85 tok/s、手机 NPU 35.83 tok/s、迷你主机 GPU 179.13 tok/s，三者都是 memory bound。手机 NPU 的算力很高，但带宽只有 60 GB/s，所以 tok/s 仍然受权重读取限制。

这个结果解释了一个常见误区：“NPU 的 TOPS 比 GPU 高，所以推理更快”。decode 的瓶颈是带宽，不是峰值算力；一个 40 TOPS、60 GB/s 的 NPU，在 4-bit 1.5B 模型上可能不如一个 30 TFLOPS、300 GB/s 的迷你 GPU。选型要看“带宽 / 权重字节”，而不是只看 TOPS。

prefill 则是算力受限。`estimate_prefill_tokens_per_s` 用 `compute × efficiency / (2 × active_params)` 估算单 token 的 prefill 速度。端侧的 prefill 通常比 decode 快，但长 prompt 仍会让首 token 延迟变明显；prompt 缓存和前缀复用对端侧同样重要，尤其是系统提示词和多轮对话。

## 45.5 运行时与批处理

端侧运行时可以分成三类。

**专用运行时**：llama.cpp、MLC-LLM、ONNX Runtime、TensorRT-LLM（边缘版）、厂商 NPU SDK。它们针对特定格式和硬件优化，提供 mmap、KV 管理、采样和流式输出。选型的首要问题是“它支持你的模型结构和量化格式吗”，而不是“它的 benchmark 高不高”。

**框架自带导出**：PyTorch ExecuTorch、TensorFlow Lite、Core ML。它们和训练框架衔接好，但算子覆盖和动态 shape 支持有限；attention 的变长 KV 经常需要自定义算子。

**浏览器/WebGPU**：适合演示和隐私敏感的场景，但性能受浏览器和驱动限制，模型通常要很小。WebGPU 的 shader 编译和内存管理是额外的工程负担。

批处理在端侧通常是 1，但“批”还可以按另一种方式利用：**连续请求的前缀复用**。多轮对话的系统提示词、工具定义、历史消息是共享前缀；如果运行时支持 KV 缓存，第二轮可以只算新增 token。端侧的 KV 容量小，缓存策略要比数据中心更激进：只缓存完整 block、设置 TTL、按会话淘汰。

推测解码在端侧有一个特殊优势：draft 模型可以很小，甚至用 n-gram 或 prompt lookup，不需要额外的大模型。它把“每步读一遍大权重”变成“读 draft + 验证”，如果接受率高，tok/s 可以明显上升。代价是 draft 也要占内存，且端侧算力有限时验证成本占比更高。`n-gram` 草稿对代码补全、模板化文本特别有效，因为这些场景的重复性高。

另一个端侧技巧是**上下文移位（context shifting）**：当对话超过窗口时，丢弃最旧的一段 KV，而不是重新 prefill 整个上下文。它牺牲了远距离记忆，但避免了长上下文的重算。实现要保证位置编码、attention mask 和 KV 页表同步更新，否则模型会看到错位的历史。

## 45.6 能耗、热与持续吞吐

端侧产品不只看 tok/s，还看 J/token 和持续吞吐。

```text
energy_per_token = power_w / tokens_per_s
```

示例里笔记本 CPU 是 1.507 J/token，手机 NPU 是 0.167 J/token，迷你 GPU 是 0.837 J/token。手机 NPU 的能效最好，但它的绝对速度不一定最快；对电池续航敏感的产品，J/token 比 tok/s 更重要。

热降频会改变长期吞吐。很多设备在峰值频率下只能跑几十秒，之后降到 50% 到 70%。`thermal_sustained_tokens_per_s` 用“峰值段 + 降频段”的加权平均估算：

```text
average = (peak × burst_s + peak × throttle_fraction × sustained_s) / total_s
```

示例里峰值 29.85 tok/s、降频系数 0.6、30 秒后降频、总时长 600 秒，持续吞吐约 18.51 tok/s。一个 10 秒的 benchmark 会报告 29.85，用户实际感受到的是 18.51；这两个数字都不假，但必须说明测量窗口。

端侧容量规划要同时看三个数：峰值 tok/s、持续 tok/s、J/token。一个语音助手可能更关心持续 tok/s 和电量；一个离线翻译器可能更关心首 token 延迟；一个文档摘要工具可能更关心总完成时间。

## 45.7 云边混合路由

端侧不可能跑所有模型和所有上下文。一个产品通常把请求分成三类：

**必须本地**：隐私、离线、低延迟、监管要求。例如个人照片问答、医疗记录摘要、车内语音助手。这类请求即使本地慢，也不能上云；如果本地能力不够，应该明确拒绝或降级，而不是偷偷上传。

**本地优先**：公开内容、短上下文、可以接受本地质量。例如输入法补全、简单问答、模板化回复。本地能在一两百毫秒内完成，就不上云，省带宽和成本。

**云端优先**：长上下文、复杂推理、多模态、需要更大模型。端侧只做预处理和结果缓存。

`route_edge_or_cloud` 把隐私、延迟预算、网络可用性和成本放在一起判断。规则是：

1. `private`/`confidential` 请求永远留在端侧；如果延迟预算不满足，返回 `feasible=False`，让上层决定拒绝还是让用户放宽预算。
2. 公开请求如果端侧能在预算内完成，走端侧。
3. 端侧太慢且网络可用，走云端。
4. 网络不可用，只能端侧；如果预算不满足，明确标记不可行。

示例里 200 token 输入、300 token 输出、端侧 29.85 tok/s：端侧 decode 时间约 `300/29.85 ≈ 10.05` 秒，加上固定开销约 10.07 秒。这是**只算 decode 的下界**，没有算 200 个 prompt token 的 prefill；端侧 prefill 可能再增加几百毫秒到几秒，取决于芯片和量化格式。即便如此，10 秒已经远超 2 秒预算，所以公开请求走云端；私有请求即使预算 60 秒也留在端侧；50 毫秒预算且断网时，端侧不可行。这个结果说明“云边路由”不是简单的“本地快就用本地”，而是隐私、延迟、网络和成本的联合约束。看端侧延迟数字时，先确认它含不含 prefill、含不含首 token 的模型加载和 tokenizer。

混合路由还要处理**模型版本和缓存一致性**。端侧模型可能比云端旧几个版本；同一个会话在两端切换时，KV、tokenizer、chat template 都可能不兼容。生产系统通常把“端侧模型版本 + 模板哈希”写进会话状态，切换时重新 prefill，而不是复用不兼容的 KV。

## 45.8 端侧评测

端侧评测不能只看“能不能跑”。至少要看六项。

**首 token 延迟**：从用户按下发送到第一个字出现，包含模型加载、tokenize、prefill 和网络（如果有）。冷启动和热启动要分开测。

**持续 tok/s**：跑至少几分钟，覆盖热降频；报告峰值和持续值。

**J/token**：用设备功耗计或系统电量估算；电池供电和插电供电可能不同。

**内存峰值**：包括权重、KV、激活、运行时和操作系统；OOM 在端侧是常态，不是异常。

**质量**：量化、蒸馏、上下文移位都会影响质量；要按任务测，不能只看 perplexity。长上下文、代码、数学、多语言要分开看。

**离线可用性**：断网、弱网、飞行模式下能否完成核心任务；云端 fallback 失败时的错误提示是否清楚。

端侧评测还要记录设备和系统版本。同一个模型在 iPhone、Android 旗舰、中端机、Windows 笔记本上的表现可能差几倍；把“手机”当成一个统一平台，会得出错误的容量结论。

## 45.9 mini-deepinfer 的实现与实验

`mini_deepinfer/edge.py` 提供 `EdgeDevice`、`quantized_weight_bytes`、`plan_memory`、`estimate_decode`、`estimate_prefill_tokens_per_s`、`energy_per_token`、`thermal_sustained_tokens_per_s`、`EdgeRequest` 和 `route_edge_or_cloud`。它们都是纯函数，不依赖真实设备。

运行：

```bash
python examples/edge.py
python -m pytest tests/test_edge.py -q
```

一次运行的输出包括：1.5B 模型 16/8/4-bit 分别是 3.000/1.500/0.750 GB；三台设备在 4-bit、4K 上下文下的内存占用和 decode 估计（29.85、35.83、179.13 tok/s，全部 memory bound）；能耗 1.507、0.167、0.837 J/token；10 分钟持续吞吐 18.51 tok/s；四种路由决策。这些数字是确定性的，用来对照公式，不代表真实设备。

mini 没有实现真实的量化 kernel、mmap、NPU 后端和功耗测量。它证明的是“体积、roofline、能耗、热和路由”的算术逻辑；真实端侧部署要用厂商工具链和真机测试。

## 45.10 常见故障

**把 TOPS 当成 tok/s。** NPU 的峰值算力和 decode 的带宽瓶颈不是一回事；选型要看权重字节和内存带宽。

**忽略热降频。** 短 benchmark 报告峰值，用户长时间使用看到持续值；容量和体验评估要用持续吞吐。

**量化后质量崩。** 4-bit 权重量化对长上下文、代码、数学的影响不均匀；要用任务评测，不要只看平均困惑度。

**KV 换出到闪存。** 闪存随机读写延迟高，换出会让 TPOT 抖动；端侧宁可变短上下文或丢弃旧会话，也不要频繁换出。

**端侧模型和云端模型不兼容。** tokenizer、chat template、特殊 token 不同，切换后输出乱码或角色错乱；会话状态要绑定模型 manifest。

**隐私请求被 fallback 到云端。** 路由逻辑把“端侧太慢”当成“可以上云”，忽略了隐私约束；私有请求必须显式失败或降级，不能静默上传。

**冷启动被当成稳态。** 第一次推理包含模型加载、mmap 缺页、kernel 编译；首 token 延迟可能比后续高几倍。评测要分开冷/热启动。

**内存预算漏算运行时。** 权重 + KV + 激活看起来放得下，但运行时、图缓冲、tokenizer 和操作系统一起吃掉了剩余内存；`plan_memory` 的 headroom 就是为这个留的。

## 小结

端侧推理是另一套系统：batch 小、带宽低、功耗和热受限、部署碎片化。量化同时降低体积和带宽，roofline 决定 tok/s，能耗和持续吞吐决定产品体验，云边路由在隐私、延迟、网络和成本之间做选择。mini-deepinfer 用设备模型、体积计算、decode 估计、热模型和路由规则验证了这些逻辑，但没有复制真实设备的 kernel 和功耗行为。下一章回到集群，讨论一个常被单机指标掩盖的问题：流量工程、排队、路由和多区域运营。

## 练习

1. 一个 3B 模型用 4-bit 量化，权重多少 GB？如果设备有 8 GB 内存、headroom 15%、4K 上下文、每 token KV 890 字节，能放下吗？
2. 设备带宽 100 GB/s、效率 0.5、权重 1.5 GB，估算 decode tok/s；如果上下文从 4K 增到 32K，tok/s 会怎么变？
3. 解释为什么 NPU 的 TOPS 高但 tok/s 可能低。给出一个带宽和算力的对比。
4. 一个私有请求在端侧需要 10 秒，延迟预算 2 秒。路由应该返回什么？产品层可以怎么处理？
5. 设计一个端侧评测计划：列出六项指标、测量窗口和设备矩阵。
6. 用 `examples/edge.py` 把 `efficiency` 从 0.45 改成 0.25，观察 tok/s 和能耗怎么变；再改 `throttle_fraction`，观察持续吞吐。

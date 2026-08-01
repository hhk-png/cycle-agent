# 13 常见问题与 FAQ

> 本章目标：把使用 vLLM 时最高频的问题按主题整理成速查手册。先按主题给"一问一答"，最后附一张"报错信息 → 原因 → 修复"的排查表。

---

## 1. 安装与环境

**Q1：vLLM 支持 Windows 吗？**
vLLM 核心依赖 CUDA 内核（PagedAttention 等），**官方主要支持 Linux + NVIDIA GPU**。Windows 上**官方不提供预编译 wheel**，社区有 WSL2（Windows Subsystem for Linux）方案：在 WSL2 里装 Linux 版 vLLM 最省事。若坚持原生 Windows，可尝试 vLLM 的 CPU 实验后端（很慢，仅用于调试）。**结论：Windows 用户请用 WSL2 或 Linux 环境。**

**Q2：vLLM 支持 CPU / AMD / Apple Silicon 吗？**
- **CUDA（NVIDIA）**：一等公民，性能最好；
- **ROCm（AMD）**：`pip install vllm-rocm`（或源码编译），官方在推进，支持面略窄于 CUDA；
- **CPU**：`VLLM_TARGET_DEVICE=cpu` 或专门的 CPU 包，**实验性**，性能远低于 GPU，仅供验证；
- **MPS（Apple Silicon）**：基本不支持，苹果生态请用 llama.cpp / MLX。

**Q3：报错 `vllm requires xxx` 或 torch 版本冲突怎么办？**
vLLM 对 Python 和 PyTorch 版本有较严格的要求。建议：
1. 用独立的虚拟环境（`conda create -n vllm python=3.10` 或 3.11/3.12）；
2. 按官方文档指定版本安装：`pip install vllm`（会自动装配套 torch）；
3. 若已装过 torch，先确认 CUDA 版本匹配（`nvidia-smi` 看驱动支持的 CUDA，`torch.version.cuda` 看 torch 的 CUDA）。

**Q4：CUDA 版本不匹配（`CUDA error: no kernel image is available for execution on the device`）？**
通常是"驱动太老"或"torch 的 CUDA 与驱动不兼容"。检查 `nvidia-smi` 的驱动版本，升级驱动到支持目标 CUDA 的版本；或降级 torch/vLLM 到匹配组合。一般 **torch 自带 CUDA runtime**，与系统 CUDA 版本无关，关键是**驱动 ≥ 所需 CUDA 的最低版本**。

**Q5：为什么安装那么大？能减小吗？**
vLLM + torch + CUDA 依赖动辄几 GB。无法显著减小，但可用镜像源加速下载（见 Q10）。

**Q5A：V1 和 V0 引擎怎么切换？`VLLM_USE_V1` 是什么？**
vLLM 有 V0 / V1 两代引擎（详见第 01 章 4.1 节）。`VLLM_USE_V1` 环境变量控制版本：
```bash
export VLLM_USE_V1=1   # 强制用 V1（较新版本默认）
export VLLM_USE_V1=0   # 回到 V0
```
V1 默认开 prefix caching、调度更激进、`max_num_batched_tokens` 动态；V0 需要 `--enable-prefix-caching` 等显式开关。**遇到"调度/内存行为与文档不一致"先确认引擎版本**，再查第 16 章附录 A。

**Q5B：怎么指定 attention 后端？不同后端结果一样吗？**
用 `VLLM_ATTENTION_BACKEND` 环境变量（`FLASH_ATTN` / `XFORMERS` / `TRITON_ATTN` / `FLASHINFER`）。不同后端数值允许有**极小的浮点差异**（softmax 累加顺序不同），正常情况下不影响生成质量；若你在做逐 token 对拍且结果不一致，先确认两端用了同一个后端。默认按硬件自动选，**一般不手动改**。

**Q35：对 Python 版本有什么要求？**
vLLM 对 Python 版本要求较严，不同 vLLM 版本支持的 Python 范围不同（例如较新版本要求 3.9 或 3.10–3.12，个别版本已支持 3.13），**具体以当前官方文档的版本矩阵为准**。最稳妥的做法：用 `conda create -n vllm python=3.11`（或官方推荐版本）建独立环境，再 `pip install vllm`，让 pip 自动解析配套的 torch 与 CUDA 组合。**不要在系统 Python 里裸装**，torch 与 CUDA 版本冲突（见 Q3）大多是混装引起的。

**Q36：pip 安装和 Docker 镜像选哪个？**
- **pip 安装**：灵活、贴近源码、方便二次开发与 debug，但依赖多、torch/CUDA 容易冲突，还需要自己管理 GPU 驱动；
- **Docker 镜像（`vllm/vllm-openai`）**：官方已配好全部依赖，`docker run` 一条命令起服务，升级/回滚干净；代价是镜像大（数 GB）、GPU 透传依赖宿主机装 nvidia-container-toolkit（见第 10 章 §10.2）。
**结论**：开发/调试/贡献代码用 pip 虚拟环境；生产部署、多人协作、快速上线用 Docker。两种方式装的 vLLM 行为一致，只是分发形态不同。

---

## 2. 模型加载

**Q6：报错 `Tokenizer ... not found` 或 `missing config.json`？**
模型目录结构不完整。HuggingFace 模型应包含 `config.json`、`tokenizer.json`/`tokenizer_config.json`（分词器）以及权重文件。请确保：
- 用 `snapshot_download` 或 `huggingface-cli download` 完整下载；
- 路径写对（本地目录或 HF 模型名，如 `meta-llama/Llama-3.1-8B-Instruct`）。

**Q7：报错 `architecture ... is not supported` / `Model architectures ... not supported`？**
vLLM 内置了对主流架构的支持（Llama、Mistral、Qwen、DeepSeek、Phi 等），但**不支持小众/过新架构**。解决：
1. 升级 vLLM（新架构支持通常很快跟上）；
2. 用 `--trust-remote-code` 让 vLLM 加载模型仓库自带的建模代码（`modeling_*.py`）；
3. 若仍不支持，需等官方适配，或改用 transformers/TGI。

想理解"为什么有些架构要等适配"（GQA/MLA 的 KV 头数、RoPE 的位置编码、MoE 的专家并行都影响内核），见第 19 章《模型架构基础》。

**Q8：`trust_remote_code` 是什么？安全吗？**
模型仓库可附带自定义 Python 代码（用于不支持的开箱架构）。`--trust-remote-code` 会执行这些代码，**等同于运行该仓库作者提供的程序**，只对可信来源使用。安全起见：先审查仓库内容或使用官方已验证的模型。

**Q9：`Access to model ... is restricted` / 405 错误？**
模型在 HF 上设置了 gated（需授权），例如 Llama-3。需要先在 HuggingFace 官网登录并同意模型条款，再配置 token：
```bash
huggingface-cli login        # 输入 HF token
# 或
export HF_TOKEN=hf_xxx
```

**Q10：在国内下载 HuggingFace 模型很慢/失败怎么办？**
使用镜像 `hf-mirror.com`：
```bash
export HF_ENDPOINT=https://hf-mirror.com
# 或
HF_ENDPOINT=https://hf-mirror.com huggingface-cli download meta-llama/Llama-3.1-8B-Instruct --local-dir ./model
```
设置后 `snapshot_download`、`transformers`、vLLM 都会自动走镜像。

**Q11：加载时报 OOM（见下一节），但我想先看模型能不能加载？**
先设一个很小的 KV cache 预算：`--gpu-memory-utilization 0.3 --max-model-len 2048`，或用 CPU offload。

**Q37：报错 `RuntimeError: input too long` / `prompt exceeds max_model_len` 怎么办？**
请求的输入 token 数超过了 `--max-model-len`。三个方向：
1. **增大 `--max-model-len`**——注意 KV cache 会按这个值预留显存，设太大反而降低可承载并发（详见第 17 章附录 B）；
2. **截断/切块输入**——长文档先切段再处理，RAG 场景精简检索上下文；
3. **拆成多轮/多请求**——而不是硬塞进一次生成。
另外排查：你是否改了 `--served-model-name` 但客户端仍用旧名？个别"超长"报错其实是**模型名不匹配导致走了错误的处理路径**（少见，但值得先排除）。

**Q38：模型权重是 `.bin`（PyTorch）格式、不是 safetensors，能加载吗？**
能。vLLM 兼容 PyTorch 的 `.bin` 权重，加载时会自动读取/转换。但要注意：
- `.bin` 是 pickle 格式，**存在被植入恶意代码的安全风险**，官方推荐优先使用 safetensors；
- 若仓库同时提供两种格式，vLLM 默认优先 safetensors；只有 `.bin` 也能正常加载；
- `.bin` 加载通常比 safetensors 慢（缺少内存映射加载），大模型首次启动更明显。
建议：自己分发模型时统一转成 safetensors（用 `safetensors` 库或 `huggingface-cli`），加载快且安全。

---

## 3. 显存与 OOM

**Q12：`CUDA out of memory` 怎么办？**
按"三步走"依次收紧，直到能跑：
1. **降低 `--gpu-memory-utilization`**（默认 0.9，即给 KV cache 预留 90% 显存之外的部分）。显存紧张时降到 0.5–0.7，给权重和激活留更多空间；
2. **降低 `--max-model-len`**（最大上下文长度）。KV cache 总量 ∝ `max_model_len × 并发数`，把 32K 降到 8K/4K 能立刻释放大量显存；
3. **降低 `--max-num-seqs`**（最大并发序列数）。并发越少，KV cache 占用越小。

**Q13：`KV cache is nearly full` / 请求被反复抢占？**
这是**显存不足以容纳当前并发序列的 KV**。除了 Q12 的参数，还可以：
- 开启 `--enable-chunked-prefill`（长 prompt 拆块，避免整段 prefill 瞬间占满）；
- 开启 `--enable-prefix-caching`（共享前缀复用，多轮对话/RAG 收益大）；
- 用 `--kv-cache-dtype fp8` 把 KV 压到半精度以下；
- 减少并发或缩短 `--max-model-len`。

**Q14：显存明明够，却还是 OOM？**
可能是**激活（activation）内存**峰值过大（尤其长 prompt 的 prefill）或**权重加载时峰值**。可尝试：chunked prefill（降激活峰值）、`--enforce-eager`（禁用 CUDA graph，降低固定显存占用）、降低 batch 大小。

**Q15：`--max-model-len` 设多少合适？**
设为你**实际需要的最长上下文**即可。过大会浪费 KV 显存、降低可承载并发；过小会截断长 prompt。可用 `--max-model-len 8192` 起步，按显存余量上调。

**Q16：怎么判断显存花在哪？**
启动日志会打印 KV cache 总块数/大小；`nvidia-smi` 看实时显存。vLLM 日志中的 `GPU KV cache size` 就是给 KV 的显存。

**Q39：为什么权重量化（int4）之后还是 OOM？——因为 KV cache 才是大头。**
量化只压缩**权重**，而推理显存由三部分构成：**权重 + KV cache + 激活**。长上下文、高并发下 **KV cache 往往占大头**（第 17 章附录 B 有完整公式）。所以：
- 只量化权重、不压缩 KV → KV 仍按 fp16 存，长上下文照样 OOM；
- 要同时压 KV：`--kv-cache-dtype fp8`（KV 量化）、缩短 `--max-model-len`、降 `--max-num-seqs`、开 `--enable-prefix-caching` / `--enable-chunked-prefill`。
一句话：**"量化后还 OOM"先看是不是 KV 没压缩**，而不是继续压权重。

**Q40：激活（activation）显存怎么估算？**
激活是 prefill 阶段的最大临时占用，粗略近似 ≈ `batch × 序列长度 × hidden_size × 层数 × 若干倍系数`（具体倍数取决于激活 checkpoint 策略，vLLM 按 Transformer 层数逐层释放）。工程上不用手算太细，用"减法"定位：
1. 看启动日志的 `GPU KV cache size`（KV 部分）；
2. `nvidia-smi` 看峰值，减去权重（≈ 参数量 × 2B fp16）与 KV，剩下就是激活峰值；
3. 若 prefill 阶段 OOM、decode 阶段正常 → 激活是瓶颈，开 `--enable-chunked-prefill` 拆块可大幅压低峰值。

---

## 4. 性能问题

**Q17：为什么我的 TTFT（首 token 延迟）很高？**
TTFT ≈ prefill 时间 + 排队时间。排查顺序：
1. **排队**：并发高、或前面有长 prefill 挡路 → 开 chunked prefill、降低 `--max-num-seqs`；
2. **prefill 本身慢**：prompt 太长、batch 太大 → 拆短 prompt 或分批；
3. **机器慢**：小 GPU / 非 Tensor Core 运算 → 换卡或用量化。

**Q18：如何提高吞吐量（throughput）？**
- 增大并发：提高 `--max-num-seqs`（前提是显存够）；
- 开 `--enable-chunked-prefill`：把长 prefill 拆开填满每步的 decode 空隙；
- 开 `--enable-prefix-caching`：重复前缀（system prompt）不再重复计算；
- KV 量化 `--kv-cache-dtype fp8`：省显存 → 可加并发；
- 投机解码（`--speculative-config`）：同等显存下提升 decode 吞吐；
- 用 `--gpu-memory-utilization 0.95` 尽量多留 KV cache（但别 OOM）。

**Q19：`--block-size`（块大小）怎么选？**
默认 16。**大块**：块表小、内核效率高，但短请求浪费块内空间；**小块**：碎片少、更省显存，但元数据多。若请求多为长序列 → 大块（16/32）；短请求多 → 小块（8/16）。多数场景用默认即可，不要轻易改。

**Q20：什么时候该开 chunked prefill？**
当**长 prompt 拖慢短请求**时（混合负载、Agent 场景），以及**显存紧张**时。纯短 prompt、或对 TTFT 极其敏感时，关掉反而更直接。vLLM 新版默认开启。

**Q21：prefix caching 什么时候收益最大？**
**固定 system prompt + 多变用户输入**的场景：多轮对话、RAG（每轮带上千 token 的检索上下文）、Agent 工具调用。如果请求之间完全没有共享前缀，开了也白开（仅增加一点哈希开销）。用 `--enable-prefix-caching` 开启。

**Q22：TPOT（单 token 生成速度）很慢？**
decode 是**带宽受限**的。优化：KV 量化（fp8）、降低并发争抢带宽、投机解码减少目标前向次数、换更高带宽的卡。

**Q23：为什么我的吞吐比论文/benchmark 低很多？**
大概率是**参数没对齐**：并发数、`--max-model-len`、输入输出长度分布、批大小都不一致。基准测试请用官方 benchmark 脚本（`benchmarks/benchmark_serving.py`）在相同参数下对比。

**Q41：如何系统地测吞吐与延迟？（指路第 21 章）**
不要随手发几个请求看数字就下结论。第 21 章附录 D 给出完整方法论：**指标定义（TTFT/TPOT/ITL/吞吐）→ 工具（`benchmark_serving.py`、gbench、Locust）→ 负载设计（长短混排 + 共享前缀）→ 分位数 SLO → 单变量调优循环**。最小起步命令：
```bash
python vllm/benchmarks/benchmark_serving.py --backend vllm \
  --model Qwen/Qwen2.5-7B-Instruct --base-url http://localhost:8000/v1 \
  --dataset sharegpt --num-prompts 500 --request-rate 8
```
关键认知：**测吞吐和测延迟是两套不同的压测**——吞吐要打满并发、跑长输出；延迟要低并发、看分位数。混着测，两个数字都不可信（详见第 21 章 §1）。

**Q42：TPOT 和 ITL 有什么区别？**
- **TPOT（Time Per Output Token）**：生成阶段"平均"每个输出 token 的耗时，是整体吞吐视角；
- **ITL（Inter-Token Latency）**：流式响应中**相邻两个 token 到达客户端的时间间隔**，是逐 token 的节奏，更能反映"流式卡顿"。
两者数值接近（都 ≈ decode 单步时间），但 **ITL 的 P99 分位数**才能暴露"中间某一步突然变慢"；TPOT 取平均后会把这种抖动抹平。**在线流式体验看 ITL P99，离线吞吐看 TPOT/throughput**。

---

## 5. 服务（Serving）

**Q24：如何开启流式输出（streaming）？**
请求体加 `"stream": true`，响应即为 `text/event-stream`（SSE）：
```json
{"prompt": "hello", "max_tokens": 64, "stream": true}
```
配合 `openai` SDK 的 `stream=True`。vLLM 会逐 token 返回 `data: {...}\n\n`，以 `data: [DONE]` 结束。

**Q25：如何控制最大并发？**
`--max-num-seqs` 控制引擎层**同时运行**的序列数（真正的并发）。想限制排队，用 `--max-waiting-queue-length`（旧版本为 `--max-queue-length`），达到上限后新请求会返回 HTTP 503 而不是无限排队。二者配合即可实现"超载即拒绝"。

**Q26：如何查看指标（metrics）？**
vLLM 默认在 `/metrics` 端点暴露 Prometheus 指标（`vllm:` 前缀，含吞吐、队列长度、TTFT/TPOT 分布等；个别旧版本需加 `--metrics` 开启）。也可用 `--metrics-format statsd` 把指标输出到 statsd。

**Q27：`/v1/completions` 和 `/v1/chat/completions` 有什么区别？**
| | `/v1/completions` | `/v1/chat/completions` |
|---|---|---|
| 输入 | 裸文本 `prompt` | `messages`（role/content 数组） |
| 适用 | 文本补全（代码、续写） | 对话/带 system 的指令 |
| 额外处理 | 无 | 需 chat template（把 messages 拼成 prompt） |
| 返回 | `choices[].text` | `choices[].message.content` |

**核心区别**：chat 端点需要 **chat template**（`tokenizer_config.json` 里通常自带；自定义模板用 `--chat-template` 指定）。模型不支持对话格式时用 completions 更稳。

**Q28：健康检查用哪个接口？**
`GET /health`（返回 `ok`）。负载均衡/容器探活用它。

**Q29：如何多卡跑一个大模型？**
`--tensor-parallel-size 2`（张量并行）或 `--pipeline-parallel-size`（流水并行）。单卡显存放不下就用 TP；TP 需要高速卡间互联（NVLink），跨机器网络慢会拖后腿。超大模型可能还需要 `--distributed-executor-backend` 配合 Ray/MP。

**Q30：`--enforce-eager` 是干嘛的？**
默认 vLLM 用 CUDA graph 捕获计算图加速，代价是预分配较多显存。`--enforce-eager` 关闭 CUDA graph，显存占用更低、适合调试，但吞吐略降。显存紧张/加载失败时试试它。

**Q43：如何优雅地关闭 vLLM 服务？**
- 直接 `kill`（SIGKILL）会丢掉正在处理的请求，还可能留下共享内存残留；
- **优雅关闭**：先发 `SIGTERM`，让 vLLM 处理完在飞请求或尽快返回后再退出；容器场景由编排系统（K8s）的 `terminationGracePeriodSeconds` 控制宽限期；
- 更稳的顺序：**先摘流**（从网关/负载均衡移除该实例）→ 等存量请求完成 → 再停进程（灰度/滚动发布的思路，见第 20 章）。
注意：vLLM 用共享内存做多进程通信，非正常退出后重启若报"共享内存冲突"，先清残留或重启机器（参考第 10 章 `--ipc=host` 相关说明）。

**Q44：升级模型支持 hot-reload（热加载）吗？**
**不推荐、也没有官方保证的 hot-reload 路径**。vLLM 不是为"运行中换权重"设计的：
- 常规做法是**起新实例 → 验证 → 切流**（蓝绿/灰度，见第 20 章）；
- 多模型共存可用 `--lora-modules`（同一底座挂不同适配器，支持运行时动态注册/卸载）或**多实例 + 网关路由**（配合 `--served-model-name`）实现"按请求选模型"，而不是 reload 基座；
- **基座模型本身不热换**——换基座请走滚动发布。
结论：升级模型 = 新实例 + 切流，不是 reload。

**Q45：返回 429 和 503 有什么区别？**
- **429 Too Many Requests**：**限流**——请求本身合法，但触发了速率/并发上限（通常是网关策略或 `--max-num-seqs` 之上层的控制）。客户端应**退避重试**，不要再加并发；
- **503 Service Unavailable**：**过载/不可用**——引擎侧排队已满（`--max-waiting-queue-length` 达到上限后拒绝新请求）或服务正在启动/关闭。客户端也应退避重试，但要同时关注服务端健康状态。
**客户端策略**：对两者都做"指数退避 + 抖动（jitter）"重试，但**不要把 429 当故障疯狂重试**（会加剧限流），也不要把 503 无限重试（先看服务端是否恢复）。

---

## 6. vLLM 与其它引擎（快速回答）

**Q31：vLLM vs TensorRT-LLM？**
vLLM 上手快、生态广、纯 Python 可定制；TensorRT-LLM 在极限延迟/吞吐上更强但构建复杂。**做产品、快速迭代选 vLLM；追求单模型极限性能且能承受工程成本选 TRT-LLM。**

**Q32：vLLM vs SGLang？**
定位最接近。SGLang 的 RadixAttention 在**复杂前缀复用**（大量共享 system prompt）场景有优势；vLLM 生态、模型覆盖、稳定迭代更成熟。**多数场景 vLLM 更稳，前缀重度场景可对比 SGLang。**

**Q33：vLLM vs llama.cpp / Ollama？**
llama.cpp/Ollama 适合**本地、CPU/消费卡、GGUF 量化**；vLLM 适合**多用户、高吞吐、GPU 集群 serving**。个人电脑跑小模型用 Ollama，生产服务用 vLLM。

**Q34：vLLM vs TGI？**
TGI 与 Hugging Face 生态整合好、迁移成本低；vLLM 吞吐和特性（prefix caching、投机解码等）更激进。**纯 HF 用户可留 TGI，追求性能用 vLLM。**

**Q34A：同一模型、同一参数，vLLM 和 transformers 的输出为什么不一样？**
这是高频困惑，原因通常是**采样机制差异**而不是 bug：

1. **并行采样顺序**：vLLM 在线服务是批内并行采样，每个请求的随机流按 `(seed, 序列索引)` 派生；transformers 在 CPU/Python 里逐序列顺序采样，两者随机源完全不同——即使 seed 相同，输出也**几乎必然不同**；
2. **`temperature=0` 的语义**：vLLM 中 `temperature==0` 直接走**贪心**（等价 transformers 的 `do_sample=False`）；如果你在 transformers 里设 `temperature=0, do_sample=True`，它仍会采样（等价 vLLM 的一个极小正温度）；
3. **tokenizer 与 batching**：vLLM 对 batch 的 padding/对齐、tokenizer 模式（slow/fast）可能与你的 transformers 脚本不同；
4. **`--dtype`**：fp16 与 bf16 的舍入不同，长序列下会累积出可见差异。

**判定方法**：两边都开贪心（`temperature=0`）+ 相同 tokenizer + 相同 `--dtype`，通常能得到逐 token 一致（或高度一致）的输出。要"严格可复现"请给每个请求固定 `seed`（第 05 章 §5.8）。

**Q34B：输出无限重复 / 陷入循环怎么办？**
症状：`... and and and and ...` 或同一句话反复。排查顺序：

1. **温度太低**：贪心/低温度 + 无惩罚项最容易复读，先提高到 `temperature=0.7~1.0`；
2. **加惩罚**：`frequency_penalty=0.5~1.0` 或 `presence_penalty=0.5~1.0`（乘法 `repetition_penalty` 也行），见第 05 章 §5.5；
3. **检查 `ignore_eos`**：误设 `ignore_eos=True` 会不让模型"自然收尾"，长生成更容易滚进循环；
4. **限制长度**：`max_tokens` 给够但要合理，`max_model_len` 够长；
5. **模型/提示词问题**：极小模型（如本教程的 TinyGPT）在固定语料上反复训练，本身就是复读机——先换大一点的模型验证。

---

## 7. 报错速查表

| 报错信息（示例） | 原因 | 修复 |
|---|---|---|
| `CUDA error: no kernel image is available...` | 驱动过老 / torch 与驱动 CUDA 不匹配 | 升级驱动；对齐 torch 与驱动 CUDA 版本 |
| `CUDA out of memory` | 权重+KV+激活超出显存 | 降 `--gpu-memory-utilization`、`--max-model-len`、`--max-num-seqs`；开 chunked prefill |
| `Tokenizer ... not found` / `missing config.json` | 模型目录不完整 | 用 HF 工具完整下载模型文件 |
| `architecture ... is not supported` | vLLM 未适配该模型架构 | 升级 vLLM；`--trust-remote-code`；等官方支持 |
| `Access to model ... is restricted` (405) | gated 模型未授权 | HF 登录并同意条款，配置 `HF_TOKEN` |
| `ValueError: ... sequence longer than model max len` | 输入超 `--max-model-len` | 增大 `--max-model-len` 或截断输入 |
| `RuntimeError: ... input too long / exceeds max_model_len` | 请求 token 数超过 `--max-model-len` | 见 Q37：增大上限或截断/切块 |
| 权重只有 `.bin`（无 safetensors） | 仓库未提供 safetensors 格式 | 可直接加载；生产建议转 safetensors（见 Q38） |
| `RuntimeError: ... no free blocks` / `KV cache is full` | KV cache 耗尽 | 降并发、缩短 max-len、开 prefix caching、KV 量化 fp8 |
| `AttributeError: module 'vllm' has no attribute 'LLM'` | 版本过旧/过新 API 变更 | 升级 vLLM；查当前版本 API 文档 |
| `uvicorn`/`socket` 端口占用 | 端口被占用 | 换 `--port` 或用 `--port 0` |
| `ValueError: Unknown quantization method: ...` | 量化方法不受支持 | 查 `--quantization` 合法值；更新 vLLM |
| `requests` 超时 / 拉模型很慢 | 网络问题（尤其国内） | 设置 `HF_ENDPOINT=https://hf-mirror.com` |
| 调度/内存行为与文档不一致 | V1/V0 引擎版本差异 | `vllm --version` 确认版本；`VLLM_USE_V1` 仅对 v0.6–0.9 有效（见 Q5A） |
| 显存占用异常偏高 | CUDA Graph 捕获 buffer / `--max-num-seqs` 过大 | `--enforce-eager` 排查；调低显存参数 |
| `docker: Error response ... could not select device driver ... capabilities: [[gpu]]` | Docker 未装 nvidia-container-toolkit 或 daemon 未重启 | 宿主机安装 `nvidia-container-toolkit` 并 `sudo systemctl restart docker`；启动加 `--gpus all`（或 CDI `--device nvidia.com/gpu=all`）+ `--ipc=host` |
| 容器内 `nvidia-smi` 为空 / CUDA 不可用 | 同上，GPU 未暴露进容器 | 先在外面 `nvidia-smi` 确认宿主机正常，再按上行修复容器 |
| `WARNING: Engine iteration took a long time` | 单步调度/执行超时（长 prefill 或 CPU 调度过慢） | V1 常见于长 prompt；开 chunked prefill、加大 `--max-num-batched-tokens` 上限或调低 `--max-num-seqs`；确认 CPU 没被打满 |
| `ModuleNotFoundError: vllm._C` / 编译失败 | 源码安装时 CUDA_HOME/torch 不匹配 | 优先用官方 wheel（`pip install vllm`）而非源码编译；编译需 CUDA_HOME 与 torch 版本对齐 |
| `Tokenizer ... does not match model` / vocab size 不匹配 | tokenizer 与模型词表不一致 | 确认用的是官方模型仓库的 tokenizer；必要时 `--tokenizer-mode slow/fast` 切换、`--hf-overrides` 指定 |
| `tensor parallel size ... not divisible` / head 无法整除 | `--tensor-parallel-size` 不能整除注意力头数/层数 | 换成能整除的 TP 值（如 32 头用 2/4/8/16/32） |
| `driver too old / pynvml` 相关 | 驱动版本过老 | 升级 NVIDIA 驱动；`pynvml` 版本对齐 |

---

## 8. 小结

- **环境**：vLLM 主要支持 Linux + CUDA；Windows 走 WSL2；CPU/ROCm 为实验性。
- **加载**：模型目录要完整；国内用 `HF_ENDPOINT=https://hf-mirror.com`；小众架构用 `--trust-remote-code`（注意安全）。
- **显存**：OOM 优先降 `--gpu-memory-utilization`、`--max-model-len`、`--max-num-seqs`；KV 不够就开 chunked prefill、prefix caching、fp8 KV。
- **性能**：TTFT 高看排队与 prefill；吞吐看并发、chunked prefill、prefix caching；block-size 默认即可。
- **服务**：流式用 `stream: true`；指标新版默认在 `/metrics`（旧版本需 `--enable-prometheus-metrics`）；completions 与 chat 端点差在 chat template。
- **选型**：生产多用户用 vLLM，本地轻量用 llama.cpp/Ollama，极限性能对比 TensorRT-LLM。

主要章节到此结束。如果遇到新问题，优先查官方文档与 GitHub Issues，那里有最及时的答案。

接下来是两个进阶专题与五份参考手册：

- **《14-分布式推理与多卡部署.md》**：TP/PP/EP/DP 并行、Ray 集群、多机部署；
- **《15-多模态与LoRA推理.md》**：视觉语言模型、LoRA 适配器推理；
- **《16-附录A-环境变量与参数参考.md》**：环境变量与 CLI 参数速查；
- **《17-附录B-内存估算与容量规划.md》**：显存计算与并发规划；
- **《18-附录C-mini-vLLM完整工程参考.md》**：mini-vLLM 完整 API、验证矩阵、扩展练习与调试指南；
- **《21-附录D-性能基准测试与调优指南.md》**：指标定义、压测工具与负载设计、瓶颈定位与调优循环；
- **《22-附录E-术语表.md》**：全教程关键词速查（十个主题分组 + 拼音/字母索引），随手定位术语与章节。

> 实战中还经常需要参考手册：《16-附录A-环境变量与参数参考.md》查环境变量与 CLI 参数；《17-附录B-内存估算与容量规划.md》算显存、规划并发；《21-附录D-性能基准测试与调优指南.md》做压测与调优；《22-附录E-术语表.md》查术语。遇到对应问题直接去翻。进阶话题见《14-分布式推理与多卡部署.md》与《15-多模态与LoRA推理.md》；想改 mini-vLLM 或查它的完整 API，见《18-附录C-mini-vLLM完整工程参考.md》。

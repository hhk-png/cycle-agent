# 附录 E 实验手册与命令速查

本附录是“动手做”的入口。它把全书用到的命令、预期输出、故障排查和实验步骤集中在一起。所有命令都在仓库根目录 `deepinfer-tutorial/` 执行，默认 Python 3.10+、NumPy、pytest 已安装。

## E.1 环境检查

先确认三件事：Python 版本、NumPy 版本、pytest 是否可用。

```bash
python --version
python -c "import numpy; print(numpy.__version__)"
python -m pytest --version
```

如果 `import mini_deepinfer` 失败，确认当前目录是仓库根目录，或者设置 `PYTHONPATH`：

```bash
PYTHONPATH=. python -c "import mini_deepinfer; print(mini_deepinfer.__version__)"
```

Windows 的 Git Bash 里，`PYTHONPATH=.` 只对当前命令有效；如果要长期生效，用 `export PYTHONPATH=.`。不要从 `mini_deepinfer/` 目录内部运行脚本，否则相对路径和 import 都会出问题。

## E.2 三条验证命令

```bash
python -m pytest -q
python examples/offline_inference.py
python check_docs.py
```

预期结果：第一条显示 `268 passed`；第二条打印四个 prompt 的续写和 metrics；第三条显示 `chapters=52 appendices=10 examples=28`、`pytest collected=268`、`OK: ...`。

如果第一条失败，先看失败测试的名字，再单独跑：

```bash
python -m pytest tests/test_engine.py -q
python -m pytest tests/test_engine.py::test_batch_invariance -q
```

如果第三条失败，按错误信息逐条修；常见的是“引用的路径不存在”“README 计数过期”“章节缺 `## 小结`”。修改后重跑，直到 `OK`。

## E.3 示例脚本

按学习顺序，建议这样跑：

```bash
python examples/offline_inference.py
python examples/lifecycle.py
python examples/chunked_prefill.py
python examples/kv_store.py
python examples/speculative_decoding.py
python examples/structured_output.py
python examples/sampling.py
python examples/long_context.py
python examples/eplb.py
python examples/parallelism.py
python examples/chat.py
python examples/benchmark.py --batch-sizes 1 2 4 8 --max-tokens 16
python examples/http_server.py
python examples/roofline.py
python examples/manifest.py
python examples/gateway.py
python examples/tracing.py
python examples/tco.py
python examples/multimodal.py
python examples/reasoning.py
python examples/offline_batch.py --max-tokens 6
python examples/multi_model.py
python examples/embedding.py
python examples/compiler.py
python examples/rl_rollout.py
python examples/edge.py
python examples/traffic.py
python examples/finance.py
```

每个脚本的作用和观察点：

- `offline_inference.py`：4 条 prompt 的批量生成，打印 requests、tokens、TTFT/TPOT、scheduler、block manager。观察连续批处理如何把 4 个请求放进 1 个 prefill step、23 个 decode step。
- `lifecycle.py`：单个请求的完整生命周期。观察 prefill 分配 4 个 block、decode 逐 token 增长、结束后 block 释放，以及第二个相同请求命中 48 个 token（3 个满块）。附录 G 逐行解释这条 trace。
- `chunked_prefill.py`：完整 prefill vs 分块 prefill vs 前缀缓存。观察 `same text: True` 和 `prefix-cache hit tokens`（一次运行是 112）。
- `kv_store.py`：TTL 和 LRU。观察写入 4 次、命中 1 次、LRU 淘汰 1 个条目（block-1），以及 TTL 过期后 block-0 变成 miss。
- `speculative_decoding.py`：目标 vs 推测。观察 `identical: True`、接受率（一次运行 85.7%）、平均接受长度（3.43）。CPU 上墙钟不一定更快。
- `structured_output.py`：正则/JSON/EBNF。观察正则输出 fullmatch、JSON 能 `json.loads`、EBNF 输出 `yes`。
- `eplb.py`：基线 1.8675，EP=2 时 1.012，EP=4 时 1.0763。观察冗余专家和放置的效果。
- `parallelism.py`：CP/EP 误差约 1e-7，PD 交接后 decode 误差 0。
- `chat.py`：Chat 模板、特殊 token、流式解码。观察模板里的 `<|user|>` 是控制 token，用户文本里的同名字符串是普通字节。
- `benchmark.py`：batch sweep。CPU 数字噪声大，只看趋势，不看绝对值。
- `http_server.py`：启动临时服务、发一条请求、打印指标、关闭。观察 OpenAI 兼容字段。
- `roofline.py`：理论计算。观察 GEMM 是 compute bound、decode 是 memory bound、Flash 比朴素 attention 少搬字节。
- `manifest.py`：模型 manifest。观察 fingerprint、权重/ tokenizer 哈希、兼容性检查和篡改检测。
- `gateway.py`：鉴权、配额、前缀亲和和 drain。观察请求/ token 双桶、并发上限、亲和路由和拒绝原因。
- `tracing.py`：请求 trace。观察嵌套 span、每个阶段的耗时占比、P95 汇总和 JSONL 导出。
- `tco.py`：TCO 与单位经济。观察成本分解、每百万 token 成本、毛利率、敏感性排序、容量规划和 P/D 配比。
- `multimodal.py`：图像 token 预算、embedding 缓存、视觉批处理和 EPD 比例。观察 1024×1024 图片的 4096/484/121 三个 token 口径。
- `reasoning.py`：思考预算、双通道流式和推理成本乘数。观察 budget 耗尽信号、标记跨 chunk 解析和 `R/A` 对成本的影响。
- `offline_batch.py`：离线 wave 规划、吞吐、P95 延迟和截止时间达成率。观察优先级如何改变 wave 组成。
- `multi_model.py`：LoRA 数学、按行适配器批处理、rank padding、LRU 驻留和约束路由。观察 `rank-padded equivalent: True`、批处理与逐行循环一致，以及严格路由返回 `no_feasible_model_fallback`。
- `embedding.py`：双塔编码、长度分桶、flat/桶索引和 retrieve-then-rerank。观察 padding 浪费约 17%、两种索引返回相同 top-k，以及 rerank 后分数变化。
- `compiler.py`：常量折叠、DCE、融合和活跃性内存规划。观察算子数 11 降到 9、内存节省约 22%，以及折叠后只剩一个 `const`。
- `rl_rollout.py`：组采样、GRPO 优势、组成本、权重版本和部分 rollout。观察权重步数变化后 KV key 是否改变。
- `edge.py`：端侧体积、内存预算、decode roofline、能耗、热降频和云边路由。观察隐私请求为什么不能回退到云端。
- `traffic.py`：排队膝点、准入、路由、重试预算、熔断、容量缓冲和多区域。观察 M/M/1 与 M/M/4 的等待差距。
- `finance.py`：NPV、IRR、回收期、LTV/CAC、合同、情景分析、DCF、收入倍数、MOIC，以及 TAM/SAM/SOM、漏斗和 magic number。观察基准 NPV 为正但最坏情况为负的项目，以及终值在 DCF 中占多大比例。

## E.4 命令行

```bash
python -m mini_deepinfer.cli generate "DeepInfer is" --max-tokens 24 --temperature 0 --stats
python -m mini_deepinfer.cli chat "Explain prefill and decode." --max-tokens 24
python -m mini_deepinfer.cli bench --batch-size 8 --max-tokens 16
python -m mini_deepinfer.cli serve --port 8000
```

`serve` 会一直运行，按 Ctrl+C 退出。启动后另开一个终端：

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/v1/models
curl -s http://127.0.0.1:8000/v1/completions \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"DeepInfer is","max_tokens":16,"temperature":0}'
curl -s http://127.0.0.1:8000/metrics | head
```

Windows 上没有 `curl` 时，用 `python -c` 加 `urllib.request`，或者直接跑 `examples/http_server.py`。

## E.5 实验一：批处理与吞吐

目标：观察 batch 对吞吐和 TPOT 的影响。

```bash
python examples/benchmark.py --batch-sizes 1 2 4 8 16 --max-tokens 16
```

记录每个 batch 的 `output_tokens_per_s`、`ttft_mean_ms`、`tpot_mean_ms`。画一张“batch vs 吞吐”和“batch vs TPOT”的图。预期是吞吐先升后平，TPOT 随 batch 上升。分析固定开销（Python、NumPy、采样）和竞争（内存带宽、缓存）各占多少。

## E.6 实验二：分块与前缀缓存

目标：验证 chunked prefill 和前缀缓存不改变输出。

```bash
python examples/chunked_prefill.py
```

改变 `block_size`（8、16、32）和 `max_num_batched_tokens`（16、32、64），观察 `same text` 是否仍为 True、`prefix-cache hit tokens` 是否变化。思考：为什么 block size 变了，命中 token 数可能是 block 的倍数？为什么最后一块不共享？

## E.7 实验三：抢占与重算

目标：观察小 KV 容量下的抢占。

```bash
python -m pytest tests/test_engine.py -q -k preemption
```

然后写一个小脚本：把 `num_blocks` 调到刚好放不下 3 个长请求，`max_num_seqs=3`，生成 3 条长 prompt，打印 `scheduler.stats()` 和 `block_manager.stats()`。观察 `preempted`、`free_blocks`、`cache_hits`。如果请求一直不完成，检查 `_preempt` 是否重新 pin 了前缀。

## E.8 实验四：推测解码

目标：理解接受率、验证长度和加速比。

```bash
python examples/speculative_decoding.py
```

修改 `speculative_tokens`（2、4、6）和 `confidence_threshold`（0.0、0.3、0.55、0.8），记录接受率、平均接受长度、平均验证长度、墙钟。用 `roofline.speculative_speedup` 算理论加速比，和实测对比。思考：为什么 CPU 上墙钟可能没有加速？为什么阈值太高会降低接受长度？

## E.9 实验五：结构化输出

目标：验证文法约束和回滚。

```bash
python examples/structured_output.py
python -m pytest tests/test_structured_output.py -q
```

写一个自己的正则（例如 `[a-z]{2,4}-[0-9]{3}`），用 `SamplingParams(so_args={"schema_type":"regex","schema":...})` 生成，检查输出是否 fullmatch。再试一个递归 EBNF，观察超过展开上限时的报错。思考：为什么 mask 不能只看 token 的首字节？

## E.10 实验六：EPLB

目标：观察负载不均衡和副本放置。

```bash
python examples/eplb.py
```

修改负载向量（例如把热点从 1200 改成 3000），观察 baseline 和 EPLB 的 imbalance。改变 EP 规模和冗余专家数，找“imbalance 最低”的组合。思考：为什么更多冗余不一定更好？为什么 EP=4 可能比 EP=2 差？

## E.11 实验七：并行与 PD

目标：验证 CP/EP 数值等价和 KV 交接。

```bash
python examples/parallelism.py
```

改变 `cp_size`（2、4、8）和 `ep_size`，记录最大误差、gather 字节、dispatch 字节。思考：CP 的通信量随 `cp_size` 怎么变？PD 交接除了 K/V 还缺哪些状态？

## E.12 实验八：Roofline

目标：把公式和实测对齐。

```bash
python examples/roofline.py
```

记录 GEMM、decode、attention 的 compute/memory 时间，和 `benchmark.py` 的实测吞吐对比。注意：roofline 是下界，实测会包含 Python/NumPy 开销、内存分配、调度空转。差距越大，说明实现离硬件上限越远。

## E.13 常见故障

**`ModuleNotFoundError: No module named 'mini_deepinfer'`**：当前目录不对，或者没有设置 `PYTHONPATH`。在仓库根目录运行，或 `PYTHONPATH=. python ...`。

**`FileNotFoundError: tiny_lm.npz`**：`mini_deepinfer/assets/` 缺失，或者 `model_path` 指错。确认文件存在，路径是绝对路径或相对于当前目录。

**`OutOfBlocksError`**：KV 页不够。增大 `num_blocks`，减小 `max_num_seqs`/`max_model_len`，或者开启抢占重算。

**`ConfigError: max_model_len exceeds the KV cache capacity`**：`max_model_len > block_size × num_blocks`。调大 `num_blocks` 或调小 `max_model_len`。

**`StructuredOutputError`**：文法不支持、递归太深、schema 类型未知。检查 `schema_type` 和 schema 子集；mini 不支持完整 JSON Schema。

**测试很慢**：`pytest -q` 应该在几秒内完成；如果慢，检查是否在跑大模型或网络请求。mini 的测试不依赖网络。

**文档检查失败**：按错误逐条修；常见是路径、计数、缺小结/练习。不要为了通过而删内容，先确认引用是否真的该存在。

## E.14 实验记录模板

每次实验记录下面几项：

```text
日期：
代码版本/commit：
Python/NumPy 版本：
硬件：
命令：
参数：
指标（均值/P95/P99）：
观察到的现象：
和预期的差异：
下一步：
```

没有记录的实验，过一周就无法复现。把记录放在 `notes/` 或实验日志里，不要只留在终端 scrollback。

## E.15 从实验到项目

如果你想把 mini 扩展成自己的项目，建议的顺序是：

1. 选一个机制（例如前缀缓存或推测解码），写一个失败测试；
2. 读对应章节和代码，确认不变量；
3. 做最小改动，跑全量测试；
4. 加一个示例或 benchmark，观察行为；
5. 写一段“为什么这样改”的说明。

这个顺序和真实工程一致：先定义行为，再改代码，再验证，再观察，最后记录。不要先写一大堆代码再想“怎么测”。

## E.16 命令速查

```bash
# 测试
python -m pytest -q
python -m pytest tests/test_engine.py -q
python -m pytest -q -k "prefix or preemption"

# 示例
python examples/offline_inference.py
python examples/benchmark.py --batch-sizes 1 2 4 8 --max-tokens 16
python examples/chunked_prefill.py
python examples/speculative_decoding.py
python examples/structured_output.py
python examples/eplb.py
python examples/parallelism.py
python examples/kv_store.py
python examples/chat.py
python examples/http_server.py
python examples/roofline.py
python examples/manifest.py
python examples/gateway.py
python examples/tracing.py
python examples/tco.py
python examples/multimodal.py
python examples/reasoning.py
python examples/offline_batch.py --max-tokens 6
python examples/multi_model.py
python examples/embedding.py
python examples/compiler.py
python examples/rl_rollout.py
python examples/edge.py
python examples/traffic.py
python examples/finance.py

# 部署
docker build -f deploy/Dockerfile -t mini-deepinfer:0.1.0 .
kubectl apply -f deploy/k8s.yaml

# CLI
python -m mini_deepinfer.cli generate "DeepInfer is" --max-tokens 24 --temperature 0 --stats
python -m mini_deepinfer.cli chat "Explain KV cache." --max-tokens 24
python -m mini_deepinfer.cli bench --batch-size 8 --max-tokens 16
python -m mini_deepinfer.cli serve --port 8000

# 文档
python check_docs.py
```

## E.17 实验九：模型 manifest 与版本校验

目标：理解“线上跑的是哪个权重、tokenizer 和模板”。

```bash
python examples/manifest.py
```

记录 fingerprint、weight/tokenizer 哈希和兼容性结果。然后手动改一个字段（例如 `max_model_len` 或 `vocab_size`），重新跑 `validate_runtime`，观察问题列表。思考：为什么 manifest 要记录 chat template 的哈希，而不是只记录权重？如果 tokenizer 换了但词表大小没变，manifest 能发现吗？

## E.18 实验十：网关配额与前缀亲和

目标：理解鉴权、限流、并发和路由。

```bash
python examples/gateway.py
```

修改 `requests_per_minute`、`tokens_per_minute`、`max_concurrent`，观察拒绝原因和 `retry_after_s`。然后修改 `WorkerLoad` 的 `inflight`，观察前缀亲和在什么情况下会让位给负载更低的 worker。思考：如果两个租户共享同一个前缀，网关应该让它们共享缓存吗？安全上有什么风险？

## E.19 实验十一：请求 trace

目标：把一次请求的延迟拆到 span。

```bash
python examples/tracing.py
```

观察 `queue.wait`、`prefill`、`decode.step`、`decode.forward`、`decode.sample` 的占比。然后把 `decode.step` 的耗时改成 0.005 秒，重新跑，看 P95 怎么变。思考：如果 `decode.sample` 占了 30%，你会先查什么？如果 `prefill` 占了 80%，你会先查什么？

## E.20 实验十二：TCO 与敏感性

目标：理解成本结构和最敏感的假设。

```bash
python examples/tco.py
```

记录 capex、电力、运维、总 TCO、每有效 GPU 小时成本、每百万 token 成本、毛利率和敏感性排序。然后把利用率从 0.7 改成 0.4，把吞吐从 3000 改成 1500，重新计算。思考：为什么电力价格变化对总成本的影响远小于利用率？如果你的业务是长上下文 Agent，哪个成本项会变得更重要？

## E.21 实验十三：部署清单

目标：把服务跑进容器和 Kubernetes。

```bash
docker build -f deploy/Dockerfile -t mini-deepinfer:0.1.0 .
docker run --rm -p 8000:8000 mini-deepinfer:0.1.0
curl -s http://127.0.0.1:8000/health
```

如果有本地 Kubernetes（kind/minikube），执行：

```bash
kubectl apply -f deploy/k8s.yaml
kubectl -n mini-deepinfer rollout status deployment/mini-deepinfer
kubectl -n mini-deepinfer port-forward svc/mini-deepinfer 8080:80
curl -s http://127.0.0.1:8080/health
```

逐项解释 `deploy/k8s.yaml` 里的探针、PDB、HPA、NetworkPolicy 和 preStop。思考：如果模型加载需要 3 分钟，`initialDelaySeconds` 应该怎么设？如果缩容时直接 SIGKILL，会发生什么？

## E.22 实验十四：SWA 有界重放

目标：理解“block 命中不等于可以直接 decode”。

```bash
python -m pytest tests/test_yoco.py -q
```

然后在 Python 里调用：

```python
from mini_deepinfer.yoco import plan_encoder_hit, plan_decoder_replay, replay_cost_us
print(plan_encoder_hit(131072, 0))
print(plan_decoder_replay(100_000))
print(replay_cost_us(134, layers=20, microseconds_per_token_layer=0.5))
```

修改 `swa_cached_tokens`（0、1024、131072），观察 `effective_hit_tokens` 和 `replay_tokens`。修改 `window` 和 `keep_tokens`，观察 `keep_tokens` 小于 `window + backtracking` 时的报错。思考：为什么 block KV 命中 128K，仍然可能需要重放 128 token？如果重放成本是每 token 每层 0.5 微秒，20 层、134 token 的重放要多久？

## E.23 实验十五：采样与长上下文

先跑采样：

```bash
python examples/sampling.py
python -m pytest tests/test_sampling.py -q
```

预期看到：temperature 改变分布形状但不改变排序；top-k=3 保留 3 个 token，top-p=0.7 只保留 2 个；同一个 seed 的两条 RNG 流完全一致；stop 串 `</answer>` 跨多个字节 token 也能命中，`finish_reason=stop`；UTF-8 流式解码的 joined codepoints 是 `0x4f60, 0x597d`。

再跑长上下文前缀复用：

```bash
python examples/long_context.py
```

预期看到：4 个会话共享 320 token 前缀，开缓存时 prefill 448 token，关缓存时 1408 token，节省 960 token（68%）；128K 请求的 KV 约 111.2 MiB；100 QPS、30 秒停留、77K 上下文的容量规划并发 3000、KV 约 191.5 GiB；SWA 在 block 命中 128K 但本地无 state 时仍需重放 128 token。把共享前缀长度从 320 改到 128 或 480，观察节省比例怎么变；再改 `block_size`，观察命中粒度对收益的影响。

## E.24 实验十六：多模态 token 预算

```bash
python examples/multimodal.py
python -m pytest tests/test_multimodal.py -q
```

记录 1024×1024 图片的 `raw_patches=4096`、`unshuffled_tokens=484`、`llm_tokens=121` 和 `kv_bytes=107690`。然后把 `pool` 从 2 改成 1，把 `unshuffle` 从 3 改成 2，观察三个 token 口径怎么变。思考：为什么原始 patch 数不能直接当 LLM token 数？如果每请求 8 张图，KV 会变成多少？再看视觉批处理的 padding 浪费，把最大的图和最小的图放进同一批，观察浪费比例。

## E.25 实验十七：思考预算与双通道流

```bash
python examples/reasoning.py
python -m pytest tests/test_reasoning.py -q
```

记录 4096 token 思考后 `force answer` 的状态、流式事件里 reasoning 和 content 的切分，以及 `R/A=9` 时的成本乘数。把 `max_reasoning_tokens` 从 4096 改成 1024、把 `max_answer_tokens` 从 1024 改成 256，重新跑，观察预算和答案保留的变化。思考：如果模型没有输出结束标记，解析器应该怎么处理？如果把思考内容写进日志，会有什么风险？

## E.26 实验十八：离线批处理

```bash
python examples/offline_batch.py --max-tokens 6
python -m pytest tests/test_offline_batch.py -q
```

记录 wave 划分、总输出 token、吞吐、P50/P95 延迟和截止时间错过率。把 `--max-batch-size` 从 4 改成 2、把 `--max-batched-tokens` 从 256 改成 128，观察 wave 数和延迟怎么变。思考：为什么更大的 batch 不一定让 makespan 更短？如果一个请求超过 token budget，调度器应该拒绝还是单独成批？

## E.27 实验十九：多适配器与模型路由

```bash
python examples/multi_model.py
python -m pytest tests/test_lora.py tests/test_router.py -q
```

记录底座输出、适配器 delta、rank padding 前后的输出是否一致、四行 batch 与逐行循环的差异、registry 的淘汰次数，以及三档模型在不同质量下限和延迟预算下的路由结果。把 `max_adapters` 从 2 改成 1，观察哪个适配器被淘汰；把 `quality_floor` 从 0.80 改成 0.95，观察路由是否还有可行解。思考：为什么高接受率的级联反而平均质量更低？如果两个适配器的 rank 不同，padding 后 `alpha` 应该怎么变？

## E.28 实验二十：Embedding、索引与 Rerank

```bash
python examples/embedding.py
python -m pytest tests/test_embedding.py -q
```

记录向量模长、三批的索引划分、总 padding 浪费、flat 与两桶搜索的排序、只扫一个桶时漏掉的文档，以及 rerank 前后的分数。把 `bucket_width` 从 16 改成 8、把 `max_batch_tokens` 从 256 改成 128，观察批次数和浪费比例；把 `probe_buckets` 从 2 改成 1，观察召回变化。思考：为什么文档向量缓存的 key 不能只用内容哈希？如果文档更新但索引没重建，会发生什么？

## E.29 实验二十一：图融合与内存规划

```bash
python examples/compiler.py
python -m pytest tests/test_compiler.py -q
```

记录原始与优化后的节点数、融合模式、数值一致性、launch 数、FLOPs、访存字节、峰值内存和节省比例。把 `batch` 从 4 改成 1 或 16，观察内存规划的变化；把 `rmsnorm` 的输出改成被两个 `matmul` 使用，观察融合是否发生。思考：为什么融合不一定让端到端更快？如果两个张量的生命周期不重叠，它们一定能共享 buffer 吗？

## E.30 实验二十二：RL rollout 与权重版本

```bash
python examples/rl_rollout.py
python -m pytest tests/test_rl_rollout.py -q
```

记录两个 wave 的组划分、优势、组成本、吞吐瓶颈和权重版本变化。把组大小从 4 改成 8，观察 `prefill_tokens` 是否仍然只算一次、`decode_tokens` 和 `kv_token_steps` 怎么变；把并发从 8 改成 32，观察瓶颈是否从 decode 转向 KV。然后手动把 `WeightVersion.step` 加 1，确认 `KVNamespace.key()` 改变。思考：如果旧 KV 没有失效，attention 会读到什么？部分 rollout 挂起时，除了 KV 还要保存哪些状态？

## E.31 实验二十三：端侧 roofline 与热降频

```bash
python examples/edge.py
python -m pytest tests/test_edge.py -q
```

记录 1.5B 模型在 16/8/4-bit 下的体积，三台设备的 tok/s、瓶颈和 J/token。把 `efficiency` 从 0.45 改成 0.25，观察 tok/s 和能耗怎么变；把 `throttle_fraction` 从 0.6 改成 0.4，观察 10 分钟持续吞吐。思考：为什么 NPU 的 TOPS 高但 tok/s 可能低？如果设备只有 8 GB 内存，4K 上下文还能放下吗？

## E.32 实验二十四：排队、准入与多区域

```bash
python examples/traffic.py
python -m pytest tests/test_traffic.py -q
```

记录 M/M/1 和 M/M/4 在不同到达率下的等待、三种准入决策、路由结果、重试预算、熔断状态和容量缓冲。把到达率从 9 改成 9.5，观察 M/M/1 等待怎么变；把 `failure_domains` 从 8 改成 4，观察容量缓冲；把 `region_failover` 的 `residency` 改成跨区域，观察 `accepts_failover` 和 `failover_capacity_qps`。思考：为什么健康容量不等于可承接容量？重试预算如何阻止风暴？

## E.33 实验二十五：现金流、LTV 与合同

```bash
python examples/finance.py
python -m pytest tests/test_finance.py -q
```

记录 NPV、IRR、回收期、LTV、CAC 回收期、价格、合同总额、盈亏平衡利用率和情景分析。把折现率从 10% 改成 20%，观察 NPV 和回收期；把月流失率从 2% 改成 4%，观察 LTV 和 CAC 回收期；把收入乘数从 0.8 改成 0.6，观察最坏情景。思考：为什么基准 NPV 为正的项目可能不值得投？承诺合同的折扣成本应该怎么算？

## 小结

本附录把“跑起来”拆成可执行的步骤：环境检查、三条验证命令、28 个示例、25 个实验、常见故障和记录模板。建议至少完成实验一、二、四、八；它们分别对应 batching、KV、推测解码和 roofline，是理解全书的主干。做完实验后，回到对应章节重读一遍，你会发现很多“当时没看懂”的段落其实在讲你刚观察到的现象。

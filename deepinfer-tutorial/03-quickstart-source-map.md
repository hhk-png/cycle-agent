# 第 3 章 快速上手与源码地图

本章导读：把“跑起来”和“看得懂”分成两步。先给出 DeepInfer 的构建、离线推理和常见参数，再给出一条从 entrypoint 到 kernel 的源码阅读路线。最后用 mini-deepinfer 的同等入口做对照，让你在没有 SM100 的机器上也能跑同一套概念。

## 3.1 DeepInfer 的前置条件

DeepInfer 官方要求 Linux、GCC 13 及以上、Python 3.12、CUDA 13（NVIDIA）或 CANN 9.2（Ascend）。这不是“建议”，而是编译期和运行期的硬要求。C++ 扩展会用到较新的编译器特性，kernel 依赖 CUDA 13 的指令和库，Python 3.12 是官方验证过的解释器版本。

构建前先创建一个空的 Python 3.12 环境，避免已有环境里的 torch、triton、flash-attn 等包造成 ABI 冲突。然后导出加速器类型：

```bash
export DEEPINFER_ACCELERATOR=nvidia
./build.sh
```

Ascend 平台把 `nvidia` 换成 `ascend`。这个变量要在运行 DeepInfer 的环境里保持导出，因为运行时会根据它选择平台相关的库和 kernel 分支。

首次运行默认模型时，框架会从 Hugging Face 下载 `deepseek-ai/DeepSeek-V4.1-Flash-1017` 到 cache。如果机器不能访问外网，或者你想固定权重版本，设置：

```bash
export DEEPINFER_HF_MODEL_DIR=/path/to/DeepSeek-V4.1-Flash-1017
python examples/offline_inference.py
```

`DEEPINFER_HF_MODEL_DIR` 只影响默认模型，不会覆盖显式传入的 `--model_name`。这一点在多模型实验时很重要。

构建失败时，先看三个地方：编译日志里第一条 `error:`，而不是最后一条 linker 错误；`DEEPINFER_ACCELERATOR` 是否在同一个 shell 里；Python 版本是否是 3.12。很多“看似 kernel 编译失败”的问题，实际是环境里混入了旧版 CUDA 头文件或 torch 的 ABI。

## 3.2 离线推理的几种并行配置

下面的命令属于 **DeepInfer 官方仓库**，不是本教程仓库。运行前先 `cd` 到你 clone 的 DeepInfer 目录；本教程 `examples/` 下的同名脚本参数不同，直接照抄会报错。两类命令的对应关系在 3.6 节。

```bash
# 在 DeepInfer 仓库根目录执行
python examples/offline_inference.py --ep 8
python examples/offline_inference.py --ep 8 --dp 8
python examples/offline_inference.py --ep 8 --cp 2
```

`--ep` 是专家并行规模，`--dp` 是数据并行规模，`--cp` 是上下文并行规模。DeepInfer 的 `ParallelConfig` 里有一条约束：如果没有显式给 `data_parallel_size`，它会默认取 `expert_parallel_size // context_parallel_size`。这个默认值要求 EP 能被 CP 整除；`--ep 2 --cp 8` 会得到 0，配置校验会拒绝。要跑 CP=8，应该让 EP 至少是 8 的倍数，或者显式给 `--dp`。默认值的目的是让 attention 的 DP 规模与 MoE 的 EP 规模对齐，减少不同并行组之间的负载错配。

显式参数和默认值冲突时，以显式参数为准，但配置校验仍会检查组合是否合法。例如 `--ep 8 --cp 2 --dp 3` 会保留 `dp=3`，然后检查 `world_size`、EP 整除关系和通信组建不建得起来；不合法就在启动时报错，而不是悄悄把 3 改成 4。这条规则适用于所有并行参数：默认值只填空，不覆盖用户输入。

DeepInfer 官方仓库的 `examples/benchmark.py` 用来跑 prefill/decode 的稳态性能。它把两个阶段解耦：`--prefill_batch_size` 和 `--non_cached_tokens` 只影响 prefill 的 chunk 大小和后缀长度；`--decode_num_requests` 只影响 decode 的 batch。默认 `context_length=32768`、`non_cached_tokens=4096`、`prefill_batch_size=32768`、`decode_num_requests=64`，nextn 默认 5。设置 `DEEPINFER_USE_NEXTN_DECODE=0` 可以显式关闭推测解码。本教程的 `examples/benchmark.py` 只接受 `--batch-sizes`、`--max-tokens`、`--prompt-repeat` 等参数，它测的是 CPU 玩具引擎的批处理趋势，不是 DeepInfer 的生产 benchmark。

DeepInfer 仓库的 sweep 脚本会在一个维度上扫点，把结果写到 `sweeps/<tag>/results.jsonl`，再用 `plot_pareto.py` 画吞吐-延迟曲线。做容量规划时，这种扫描比单点 benchmark 有用得多，因为生产负载从来不是固定 batch。

跑 benchmark 时，至少记录下面这些元数据，否则结果无法复现：模型版本、精度、GPU 型号和数量、互联类型、驱动/CUDA 版本、是否开启 nextn、是否开启前缀缓存、batch 和长度分布、测量窗口和 warmup 步数。缺少任何一项，跨团队比较都会失真。

## 3.3 一个请求在代码里怎么走

以离线 `LLM.generate()` 为例，请求的生命周期大致如下。

1. **入口层**：`deepinfer/entrypoints/llm.py` 的 `LLM` 负责 tokenize、创建 `SamplingParams`、调用 `LLMEngine.add_request`，然后循环 `step()` 直到所有请求结束。
2. **引擎层**：`deepinfer/engine/llm_engine.py` 的 `LLMEngine` 持有 tokenizer、worker、scheduler、block manager、state manager。`add_request` 把 prompt 转成 `Sequence`，处理多模态、结构化输出 matcher，再交给 scheduler。
3. **调度层**：`csrc/engine/scheduler.hpp` 的 C++ `Scheduler` 决定这一步是 prompt 还是 decode，选哪些序列，分配多少 token，是否需要抢占。它返回 `SchedulerOutputs` 和 `SequenceMetadata`。
4. **执行层**：`deepinfer/worker/worker.py` 和 `model_runner.py` 把 metadata 变成输入张量，调用模型 forward，写 KV cache，执行 sampler，返回 `SamplerOutput`。
5. **回收层**：`LLMEngine._process_model_outputs` 把采样结果写回 scheduler，更新序列状态、释放完成的序列、构造 `RequestOutput`。
6. **入口返回**：`LLM._run_engine` 收集每个请求的增量输出，解码成文本。

这条链路里有两个“跨语言边界”：Python 到 C++ 的调度器，以及 Python 到 CUDA/TileLang 的 kernel。读代码时不要试图一次看懂所有层，先抓住 `LLMEngine.step()` 和 `Scheduler.schedule()` 这两个锚点。

可以用一个更小的视角记这条链：**字符串进来，token 出去；调度决定谁在什么时候算；worker 决定怎么算；cache 决定少算多少。** 任何一次性能问题，都可以先问它落在“谁、何时、怎么、少算”中的哪一环。

## 3.4 源码地图

按“先骨架、后器官”的顺序读。

**骨架**

- `deepinfer/entrypoints/llm.py`：`LLM.generate` 的入口，负责 tokenize、建请求、驱动 step 循环。
- `deepinfer/engine/arg_utils.py`：`EngineArgs`，所有配置的入口。
- `deepinfer/config.py`：`ModelConfig`、`CacheConfig`、`ParallelConfig`、`SchedulerConfig`。
- `deepinfer/engine/llm_engine.py`：请求生命周期、worker 初始化、cache 初始化、step。
- `csrc/engine/scheduler.hpp`：调度策略、抢占、chunked prefill、MTP varlen。
- `csrc/engine/block_manager.hpp`、`deepinfer/worker/cache_engine.py`：分页 KV 和状态缓存。

**模型**

- `deepinfer/model_executor/models/deepseek.py`：主模型。
- `deepinfer/model_executor/models/decoder_layer.py`：decoder layer 的组织。
- `deepinfer/model_executor/models/v4_modules/attn.py`：MLA/CSA2/SWA/YoCo 的运行时。
- `deepinfer/model_executor/models/v4_modules/compress.py`：C1/C2 压缩。
- `deepinfer/model_executor/models/v4_modules/index_attn.py`：层次稀疏索引器。
- `deepinfer/model_executor/models/v4_modules/moe.py`：共享/路由专家、门控。
- `deepinfer/model_executor/models/v4_modules/engram.py`：哈希 n-gram 条件记忆。
- `deepinfer/model_executor/models/mtp/dflash.py`：DSpark/DFlash 草稿头与置信度头。

**并行与通信**

- `deepinfer/model_executor/parallel_utils/parallel_state.py`：DP/EP/CP 组。
- `deepinfer/model_executor/parallel_utils/communication_op.py`：all-reduce、all-gather、all-to-all。
- `deepinfer/model_executor/eplb/policy.py`：冗余专家分配与贪心放置。
- `deepinfer/distributed/shm_broadcast.py`：共享内存广播。

**正确性与服务**

- `deepinfer/structure_output/`：xgrammar 编译、matcher、触发/回滚。
- `deepinfer/sampling_params.py`：采样参数、seed、结构化参数。
- `deepinfer/sequence.py`：序列状态、MTP 预测信息、采样输出。
- `deepinfer/engine/stats_logger.py`：指标与日志。

**阅读建议**：先读 `EngineArgs` 和 `LLMEngine.__init__`，把配置项和组件对应起来；再读 `LLMEngine.step`，看一次 step 的输入输出；然后读 C++ scheduler 的 `_schedule`，最后才进入模型和 kernel。反过来从 kernel 开始读，很容易迷失在 TileLang 的索引表达式里。

## 3.5 常见坑

**环境不干净。** 官方要求空的 Python 3.12 环境。混入旧版 torch 或 flash-attn 可能导致编译通过但运行时报符号错误。

**忘记导出加速器变量。** `DEEPINFER_ACCELERATOR` 必须在运行环境中保持导出，不能只在 build 时设置。

**模型路径与 cache。** `DEEPINFER_HF_MODEL_DIR` 不覆盖显式 `--model_name`。如果你的本地目录只有 config 没有 safetensors，加载会失败。

**把 nextn 当成模型属性。** nextn 由 `DEEPINFER_USE_NEXTN_DECODE` 控制，默认 5，不能从模型名推断。做 baseline 时要显式设为 0。

**用单点 benchmark 做容量规划。** 生产负载的 batch 和长度分布会变化，必须扫 Pareto 曲线，并关注 goodput 而不是峰值吞吐。

**忽略 profiler 扰动。** `PROFILE=1` 时 trace 里的 step time 包含 profiler 开销，bsz=1 时可能膨胀接近 50%。trace 用来看 kernel 组成，不用来推导 SPS。

**把 DP 规模当成吞吐倍数。** DP 副本之间不共享 KV，前缀命中率会下降；如果路由器没有前缀亲和性，扩展 DP 反而可能增加总 prefill 计算量。

## 3.6 mini-deepinfer 的同等入口

本仓库不需要编译，直接运行：

```bash
python examples/offline_inference.py
```

它做的是同一件事：创建引擎、加载玩具权重、批量生成、打印指标。区别是 hardware backend 从 SM100 换成了 NumPy，模型从 552B 换成了 90K 参数，调度器从 C++ 换成了 Python。

如果你想看“请求生命周期”的最小版本，读 `mini_deepinfer/engine.py` 的 `add_request` 和 `step`；想看调度，读 `mini_deepinfer/scheduler.py`；想看分页 KV，读 `mini_deepinfer/block_manager.py` 和 `cache_engine.py`。第 20 章会给出完整的保真度矩阵，附录 G 则用一次可运行的追踪把这些层次串起来。

下面这条命令等价于“跑一个最小服务并请求一次”：

```bash
python examples/http_server.py
```

它会启动一个临时端口上的 OpenAI 兼容服务，发一条补全请求，打印响应和指标，然后关闭。你不需要手动开两个终端。

## 3.7 读源码的方法

读一个生产推理框架，最有效的方法不是从第一行读到最后一行，而是围绕一次请求做“纵向切片”。具体可以分四步。

第一步，选一条最短路径：`LLM.generate("hello")`。在入口处打断点或加日志，记录它经过的函数。第二步，把这条路径上的数据结构画出来：`Sequence`、`SchedulerOutputs`、`SequenceMetadata`、`InputMetadata`、`SamplerOutput`，每个结构在哪个阶段被创建、被谁修改。第三步，选一个机制（比如前缀缓存）横向追踪：从 `add_request` 到 `block_manager` 再到 kernel 的 slot mapping，看它在哪一层被决定。第四步，做一个小改动并跑回归，例如把 `prefill_attempt_interval` 调小，观察调度序列怎么变。

这个方法的优点是：你始终有一个具体的请求作为锚点，不会被庞大的代码库淹没。每读完一个机制，回到第 2 章的组件图，把它标在图上。读完全书，你应该能凭记忆画出这张图。

## 3.8 从源码到镜像

DeepInfer 的公开资料提供了安装入口，但具体包名和版本要以官方 README 为准；生产部署通常不会在目标机器上现场编译。更常见的路径是：在 CI 里构建 wheel 或容器镜像，在镜像里安装固定版本，再把镜像推到私有仓库。这样做有三个好处：编译环境一致、运行时不需要完整工具链、回滚时可以按镜像 digest 精确回到旧版本。

一个最小构建流程是：

```bash
export DEEPINFER_ACCELERATOR=nvidia   # 或 ascend
python -m build --wheel
pip install dist/deepinfer-*.whl
python -c "import deepinfer; print(deepinfer.__version__)"
```

这里的 wheel 仍然包含平台相关的 C++/CUDA/CANN 扩展，不是纯 Python 包；构建环境必须装好对应工具链。官方推荐的完整构建入口是 `./build.sh`，上面的 `python -m build` 只是把已经配置好的扩展打包。生产镜像还会记录构建 manifest：源码 commit、依赖 lock 文件哈希、CUDA/CANN 版本、编译器版本、wheel 哈希。部署时用镜像 digest 而不是 tag，避免“同一个 tag 被重新推送”导致版本漂移。第 33 章会展开构建与发布，第 35 章会给出 Dockerfile 和 Kubernetes 清单。

如果你只是学习，不需要走这条路。直接 `python examples/offline_inference.py` 就够了；镜像和 CI 是当你需要把服务交给别人运行时才需要补的工程环节。

## 3.9 并行参数的算术

并行参数最容易出错的地方，不是“选哪个值”，而是“这些值怎么组成一个世界”。下面给出一套可以手算的规则，适用于 DeepInfer 的 `--ep/--dp/--cp` 和大多数同类引擎。

先确定 world size。如果 attention 在副本之间做数据并行、每个副本内部按上下文切分，那么 `world_size = dp × cp`。EP 不额外乘进去，因为专家并行切的是同一批 rank 上的 MoE 权重，不是再开一组进程。`EP64DP64` 之所以是 64 张卡，就是因为 `dp=64`、`cp=1`，EP 只是这 64 个 rank 的另一种编号方式。

再看整除关系。CP 把序列切成 `cp` 段，每段要能放进一个 rank 的 KV；EP 把物理专家切成 `ep` 份，每份至少要有 1 个专家。因此通常要求 `ep % cp == 0`，并且 `physical_experts % ep == 0`。前者保证每个 CP 组里有整数个 EP 分片，后者保证专家能平均分到 rank。`--ep 2 --cp 8` 不满足第一条，默认 `dp = 2 // 8 = 0`，配置校验会直接拒绝。

用三个例子走一遍。例一，`--ep 8 --dp 8 --cp 1`：world size 是 8，每张卡 1 个 EP 分片，attention 有 8 个副本。例二，`--ep 8 --cp 2`：默认 `dp = 8 // 2 = 4`，world size 是 `4 × 2 = 8`，每个 DP 副本内部再切 2 段上下文，EP 仍在这 8 个 rank 上切 8 份。例三，`--ep 16 --dp 4 --cp 4`：world size 是 16，`16 % 4 == 0`，每张卡 1 个 EP 分片；如果把 `--dp` 写成 8，world size 变成 32，但 EP 只有 16，就会有一半 rank 没有专家分片，配置应当报错。

这套算术还能解释“为什么不能把 EP 和 DP 相乘”。如果误以为 `EP64DP64` 是 4096 张卡，就会算出 4096 份 KV、4096 倍的 all-to-all 流量和完全错误的成本；实际只有 64 份。读公开配置时，先算 world size，再算每张卡持有的专家数和 KV 分片，最后才看吞吐。第 15 章会把这三个例子扩展到 PD 分离和 CP 的真实通信。

## 小结

DeepInfer 的上手门槛主要在硬件和编译，而不是 API。它的离线入口很小：`LLM.generate`、`EngineArgs`、几个并行参数。真正的复杂度在引擎内部：C++ 调度器、分页 KV、模型 kernel、并行组和结构化输出。读源码时先抓 `LLMEngine.step` 和 `Scheduler.schedule` 两个锚点，再向下展开。mini-deepinfer 提供了同一套概念的 CPU 版本，方便你在没有 SM100 的机器上做对照实验。

## 练习

1. 画出 `LLM.generate` 从 prompt 到文本的调用链，标出 tokenizer、scheduler、worker、sampler 的位置。
2. 解释 `--ep 8 --dp 8` 与 `--ep 8 --cp 2` 在并行组上的区别，以及为什么 `data_parallel_size` 会默认取 `ep // cp`；如果显式给的 `--dp` 与默认值冲突，应该以哪个为准？
3. 在你的机器上运行 `python examples/offline_inference.py --max-tokens 16`，记录 TTFT、TPOT 和吞吐，并解释哪些部分属于固定开销。
4. 选一个你熟悉的框架，用 3.7 节的四步法做一次纵向切片，写出你找到的三个锚点函数。

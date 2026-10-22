# 第 33 章 工程化：从可运行到可维护

本章导读：一个推理引擎“能跑”与“能维护”之间隔着很多东西：配置如何生效、错误如何分类、日志如何定位一次请求、测试如何覆盖状态机、版本如何兼容、发布如何回滚。本章把这些看似“外围”的工作拆开，给出可以照做的工程约束，并用 mini-deepinfer 的 `config.py`、`errors.py`、`metrics.py`、`tracing.py`、`gateway.py`、`manifest.py` 和 `check_docs.py` 做对照。读完本章，你应该能判断一个推理项目离“可长期运行”还有多远。

## 33.1 工程化到底在解决什么

很多人把工程化理解成“加日志、加监控、写文档”。这些是结果，不是目标。工程化真正解决的是四个问题。

第一，**可重复**。同一份代码、同一份配置、同一份权重，在任何一台机器上应该得到同样的行为。做不到可重复，性能数字、故障复现和质量评测都没有意义。

第二，**可定位**。线上出现一次慢请求或一次错误输出，团队能在分钟级回答“是哪一个请求、哪一步、什么输入、什么配置、什么版本”。没有这个能力，排障只能靠猜。

第三，**可演进**。模型会换、硬件会换、调度策略会改、API 会加字段。系统要能在不破坏旧客户端、不破坏旧缓存、不破坏旧评测的前提下演进。

第四，**可回滚**。任何一次发布都可能引入回归。回滚必须是一条命令、分钟级完成，而且不能丢请求、不能把 KV 格式弄坏。

这四件事听起来朴素，却是推理系统从 demo 走到生产的主要成本。一个 90K 参数的玩具引擎可以在 300 行里跑通；一个 552B 模型的生产引擎，大部分代码都在处理这四件事，而不是数学。

## 33.2 项目结构与依赖边界

先看目录。一个可维护的推理项目通常分成五层：入口层、引擎层、模型层、基础设施层和工具层。目录结构应该让依赖方向一眼可见，而不是把所有文件堆在一个包里。

mini-deepinfer 的结构是：

```text
mini_deepinfer/
├── cli.py, server.py, llm.py       入口
├── engine.py, scheduler.py         引擎
├── block_manager.py, cache_engine.py
├── speculative.py, structured_output.py
├── model/                          模型与算子
├── metrics.py, tracing.py, gateway.py, manifest.py
└── config.py, errors.py, sequence.py
```

依赖方向是单向的：入口依赖引擎，引擎依赖调度、缓存、模型和采样；模型只依赖算子和配置；`roofline.py`、`economics.py` 不依赖引擎，只依赖数学。没有循环依赖，才能单独测试一个模块，也才能在需要时把某个模块替换成 C++ 或 Rust 实现。

依赖边界还有一层含义：**可选依赖不能污染核心路径**。推理引擎的核心应该只依赖标准库、数值库和通信库；监控 SDK、HTTP 框架、云存储客户端应该是可选的。否则一个监控库的版本冲突会让整个引擎装不起来。`pyproject.toml` 里的 `dependencies` 和 `optional-dependencies` 就是这条边界的声明。

```toml
[project]
dependencies = ["numpy>=1.24"]

[project.optional-dependencies]
test = ["pytest>=7"]
```

生产项目还会把 `serve`、`metrics`、`cuda`、`ascend` 拆成不同的 extra，让 CPU 开发和 GPU 部署各自安装需要的东西。不要把 CUDA 依赖写进基础包，否则一个只想读代码的读者也要装几个 GB 的工具链。

## 33.3 配置系统：优先级、校验与不可变性

配置是工程事故的高发区。最常见的三类问题是：配置没生效、配置互相矛盾、配置在运行中被改。

**优先级必须明确。** 一个实用的顺序是：命令行参数高于环境变量，环境变量高于配置文件，配置文件高于代码默认值。DeepInfer 用 `EngineArgs` 和 `DEEPINFER_*` 环境变量；mini 用 dataclass 默认值加 CLI 参数。无论用哪种，都要在启动日志里打印“最终生效值”和“每个值的来源”，否则排障时无法判断是默认值还是覆盖值。

**配置必须在构造时校验。** mini 的 `EngineConfig.__post_init__` 检查了页大小、页数、最大序列长度、token 预算、推测窗口等约束。例如 `max_model_len` 不能超过 `block_size × num_blocks`，否则第一个长请求就会越界；`block_size` 不能大于 `max_num_batched_tokens`，否则一个页都装不下。这样的错误应该在启动时抛出 `ConfigError`，而不是在流量上来后变成 `OutOfBlocksError`。

```python
if self.max_model_len > self.cache_capacity_tokens:
    raise ConfigError(
        "max_model_len exceeds the KV cache capacity; increase num_blocks "
        "or reduce max_model_len"
    )
```

**配置对象应该尽量不可变。** 调度器每步都读 `max_num_seqs`、`max_num_batched_tokens` 等字段；如果运行中有人改了配置，调度器的行为会变得不可复现。Python 的 `dataclass(frozen=True)` 可以强制这一点，或者至少在文档里写明“启动后只读”。DeepInfer 的 `EngineArgs.create_engine_configs` 在初始化时把用户参数固化成 `ModelConfig`、`CacheConfig`、`SchedulerConfig`，之后不再改，就是为了避免这种问题。

**默认值要有出处。** `ModelConfig()` 的默认值和 checkpoint 里的 `config_json` 不是一回事。mini 的默认值是 3 层、FFN 192、窗口 32，而 `tiny_lm.npz` 里保存的是 2 层、FFN 128、窗口 64。加载 checkpoint 时必须以文件里的配置为准，否则权重形状对不上。生产系统更严格：模型配置、tokenizer 配置、并行配置、量化配置都从 manifest 读取，不允许“代码默认值悄悄生效”。

## 33.4 错误分类与失败语义

错误处理的核心不是“捕获所有异常”，而是**定义每一种失败对调用方意味着什么**。

mini 的错误类型很少，但已经体现了分类：

- `ConfigError`：配置非法，启动时失败，不可重试。
- `OutOfBlocksError`：KV 页不足，可能是容量问题，也可能是调度 bug；调用方可以降 batch 或增大容量。
- `StructuredOutputError`：文法编译或匹配失败，通常是请求级错误，返回 400 而不是 500。
- `EngineClosedError`：引擎已关闭，调用方应该停止发送请求。

生产系统还会区分更多语义：可重试的瞬时错误（超时、连接重置）、不可重试的请求错误（prompt 太长、schema 非法）、需要降级的错误（draft 模型不可用、KV store 超时）、需要熔断的错误（GPU 掉卡、通信组失联）。这些分类决定了网关是重试、拒绝、降级还是摘除实例。

**不要静默降级。** 一个常见的反模式是“KV 分配失败就悄悄缩小 batch”或“结构化输出编译失败就退回自由生成”。前者会让性能数字不可解释，后者会让用户拿到不合法的输出。正确做法是显式记录降级事件、打点、并在响应里带上 `finish_reason` 或错误码。

**错误要带上下文。** 一个 `OutOfBlocksError("out of blocks")` 很难定位；一个带 `seq_id`、`requested_blocks`、`free_blocks`、`block_size`、`max_num_seqs` 的错误可以直接看出是容量不足还是泄漏。生产日志里还应该带 model id、manifest fingerprint、并行配置和实例 id。

## 33.5 日志、指标与追踪

可观测性通常分三层，各自回答不同的问题。

**日志**回答“发生了什么”。推理服务的日志应该是结构化的 JSON，而不是拼字符串。一条请求日志至少包含：时间、级别、request id、tenant id、model id、prompt tokens、output tokens、TTFT、TPOT、finish reason、cache hit、错误码。默认不要记录完整 prompt 和输出，除非有明确的审计需求和脱敏方案。

**指标**回答“整体怎么样”。指标是聚合的、低基数的，适合做告警和容量规划。推理服务至少要有请求数、错误数、token 数、TTFT/TPOT 分位数、队列长度、KV 使用率、前缀命中率、抢占次数、推测接受率、MoE 不均衡度、GPU/HBM 利用率。指标标签要控制基数：不要把 request id、用户 id、完整模型路径放进标签，否则 Prometheus 会被打爆。

**追踪**回答“这一次请求的时间花在哪”。mini 的 `tracing.py` 提供了一个最小实现：一个 trace 是一组带起止时间的 span，span 可以嵌套，最后可以导出 JSON Lines。

```python
trace = tracer.start("req-123", tenant="search")
with tracer.span(trace, "prefill", tokens=2048):
    ...
for step in range(8):
    with tracer.span(trace, "decode.step", step=step):
        with tracer.span(trace, "decode.forward"):
            ...
        with tracer.span(trace, "decode.sample"):
            ...
```

`Tracer.summary()` 会给出每个 span 的均值、P95 和最大值。这比“总延迟涨了”有用得多：如果 `decode.forward` 没变而 `decode.sample` 涨了，问题可能在结构化输出的 mask 或 logprobs，而不是 kernel。

生产系统会用 OpenTelemetry 传播 W3C trace context，把网关、引擎、KV store、通信库的 span 串起来。采样策略通常在 collector 做，而不是在热路径做；热路径只负责生成 span id 和记录时间。追踪数据同样可能包含敏感信息，要按日志的标准做脱敏和保留期限。

## 33.6 测试与 CI

测试已经在第 26 章展开，这里只讲工程化的部分：测试如何进入流水线，以及如何防止“测试全绿但线上出错”。

一个实用的 CI 流水线分四段。

第一段是静态检查：格式、类型、lint、依赖漏洞扫描。类型检查对推理引擎尤其重要，因为张量形状、dtype、设备、并行 rank 都是隐式契约，类型系统能抓住一部分错误。

第二段是单元测试和属性测试：每个模块独立跑，覆盖公式、边界和状态机。属性测试适合验证“chunked 与 full 输出一致”“有缓存与无缓存一致”“CP/EP 与单卡一致”这类不变量。

第三段是集成测试：启动一个真实引擎，跑一组固定 prompt，检查输出、指标和资源释放。`examples/offline_inference.py` 就是这种测试的最小版本。生产项目还会回放一段真实流量 trace，检查 P95 TTFT、错误率和缓存命中率没有退化。

第四段是文档与打包检查：`check_docs.py` 检查章节、链接、代码路径、测试数量和示例数量；`python -m build` 检查 wheel 能否构建；`pip install` 在干净环境里检查依赖是否完整。

CI 要有明确的“失败阈值”。单元测试失败必须阻塞合并；性能回归可以设一个容忍带，例如 P95 TTFT 退化超过 10% 才阻塞。没有阈值的性能测试会变成“每次都说变慢了，但没人知道该不该上线”。`ci/github-actions.yml` 给出了一份可直接改用的五步流水线：安装、pytest、跑全部示例、文档检查、构建 wheel；它故意放在 `ci/` 而不是 `.github/workflows/`，避免教程被复制到别的仓库时意外注册工作流。

**测试环境要固定。** Python 版本、NumPy/BLAS 版本、CPU 型号、线程数都会影响数值和性能。CI 里至少锁定 Python 和依赖版本；GPU 测试要固定驱动、CUDA/CANN 和 GPU 型号。否则同一个测试在不同机器上时快时慢，最后大家都不看结果。

## 33.7 版本与兼容性

推理系统至少有三个版本号：软件版本、配置 schema 版本、模型 manifest 版本。它们的变化速度不同，兼容策略也不同。

**软件版本**遵循语义化版本。调度策略、KV 布局、采样默认值的改变可能影响输出，应该升 minor 或 major；纯 bug 修复升 patch。对外 API 的破坏性变更必须升 major，并保留至少一个版本的弃用窗口。

**配置 schema 版本**用于启动校验。旧配置文件在新版本里可能仍然能解析，但某个字段的语义变了（例如 `block_size` 从“token 数”变成“字节数”），这时必须拒绝启动，而不是“尽量兼容”。mini 的 `ModelManifest.schema_version` 就是这种思路：版本不匹配直接报错。

**模型 manifest 版本**绑定权重、tokenizer、chat template、grammar、dtype、量化方式和引擎版本。`manifest.py` 的 `validate_runtime` 会检查词表大小、最大长度、dtype、量化方式和引擎主版本；任何一项不匹配都返回问题列表，`assert_compatible` 把它变成启动失败。

```python
manifest.assert_compatible(
    engine_version="0.1.0",
    vocab_size=tokenizer.vocab_size,
    max_model_len=config.max_model_len,
    dtype="float32",
)
```

这条检查看起来多余，直到你遇到“权重更新了但 tokenizer 没更新”——模型会输出乱码或角色错乱，而日志里一切正常。manifest 把这种事故提前到启动阶段。

**兼容性还包括 KV 格式。** 如果新版本改变了 KV 的布局、量化 scale 或 block 大小，旧版本写下的 3FS 缓存不能被新版本直接读取。生产系统会在 KV metadata 里记录格式版本和模型 fingerprint，不匹配就当作 miss，重新计算。回滚时同样要检查：旧版本能否读新版本写的缓存？不能的话，回滚要先清缓存或切换命名空间。

## 33.8 构建、打包与发布

推理引擎的构建通常比普通 Python 包复杂，因为它包含 C++/CUDA/TileLang 扩展。一个可维护的构建流程要满足三点：可重复、可验证、可回滚。

**可重复**意味着固定编译器版本、CUDA/CANN 版本、依赖版本和构建参数。构建脚本要输出一个 manifest，记录每个产物的哈希和来源。`DEEPINFER_ACCELERATOR=nvidia|ascend` 这种开关要显式传递，不能靠“当前机器上有什么就用什么”。

**可验证**意味着构建完成后跑一组冒烟测试：导入包、加载一个极小权重、跑一次 forward、跑一次采样、检查版本号。不要等到部署到 GPU 集群才发现 wheel 里缺了一个 `.so`。

**可回滚**意味着每次发布都有唯一标识，镜像不可变，配置和权重版本可追溯。Kubernetes 的 `imagePullPolicy: IfNotPresent` 配合唯一 tag 可以避免“同一个 tag 被覆盖”的事故；生产环境不应该使用 `latest`。

发布流程通常是：构建 → 单元/集成测试 → 打包 → 镜像扫描 → 灰度 → 观察 → 全量。灰度阶段要看错误率、P95 延迟、缓存命中率、GPU 利用率和业务指标，而不是只看“进程还活着”。回滚要能在分钟级完成，并且不丢正在生成的请求；这要求网关支持 drain、实例支持 grace period、KV 格式向前兼容。

## 33.9 mini-deepinfer 的工程化清单

把上面的原则对照到 mini，可以看到它已经具备什么、还缺什么。

已经具备的：`pyproject.toml` 定义了包和测试依赖；`config.py` 在构造时校验；`errors.py` 有明确的错误类型；`metrics.py` 输出 Prometheus 文本；`tracing.py` 提供请求 span 的独立模型（尚未接进 `server.py`，目前由 `examples/tracing.py` 驱动）；`manifest.py` 绑定版本；`gateway.py` 提供鉴权、配额、路由和 drain 的独立模型（尚未接进 `server.py`）；`server.py` 自己提供 `/admin/drain`、`/admin/undrain` 和 `grace_s` 等待，但还没有和网关的 drain 模型合并；`check_docs.py` 做文档自检；`tests/` 覆盖单元和集成路径；`deploy/` 给出容器与 Kubernetes 的形状。

仍然缺的：仓库没有启用 `.github/workflows/`，`ci/github-actions.yml` 只是可复制的模板；没有类型检查；没有依赖漏洞扫描；wheel 构建写在模板里但没有在本仓库执行，也没有签名；没有结构化日志；没有 OpenTelemetry 导出；没有配置来源打印；没有 API 版本路由；没有金丝雀和自动回滚。这些不是“教学实现做不到”，而是“教学实现不需要全部做”，但读者应该知道缺口在哪里。

一个有用的练习是：把 `check_docs.py`、`pytest`、`examples/offline_inference.py` 和一个 wheel 构建串成一个脚本，放进 CI，要求任何一步失败都阻塞合并。这个脚本不超过 30 行，却能把“文档和代码脱节”这类问题挡在发布之前。

## 33.10 一次工程化改造案例

假设你有一个 300 行的 `demo.py`：硬编码模型路径、全局变量保存引擎、`print` 当日志、异常直接往上抛、没有测试。要把它改成可维护的项目，可以按下面的顺序。

第一步，抽配置。把所有魔法数字变成 `EngineConfig` 字段，并在 `__post_init__` 里校验。第二步，抽错误。把“找不到文件”“显存不足”“输入非法”分成不同异常，让调用方能区分。第三步，抽入口。把 `demo.py` 的主逻辑放进 `LLM` 类，`demo.py` 只负责解析参数和调用。第四步，加指标和追踪。至少记录请求数、token 数、TTFT/TPOT 和一个请求级 trace。第五步，加测试。先写一个端到端测试（固定 prompt、固定 seed、断言输出），再补单元测试。第六步，加文档检查。把“代码路径是否存在”“示例是否能跑”变成自动检查。

这个顺序的重点是“先让行为可观察，再让行为可重复，最后才优化”。反过来先优化 kernel，往往会在一个不可观测、不可复现的系统上做无用功。

## 33.11 性能回归门禁与配置迁移

性能回归比功能回归更难自动化，因为噪声大、硬件差异大、一次跑分不可信。一个可执行的门禁要回答四个问题：测什么、和谁比、阈值多少、失败怎么办。

**测什么**：至少覆盖 TTFT P95、TPOT P95、输出吞吐、显存峰值、错误率和冷启动。不要只测峰值吞吐；一个改动可能让吞吐 +5%、P99 延迟 +50%，在线业务会先崩。**和谁比**：和上一个稳定版本的同一硬件、同一负载、同一配置比，而不是和“理论最优”比。基线要固定镜像、驱动、模型、负载和测量脚本；基线漂移会让门禁失去意义。**阈值多少**：用配对实验的置信区间，而不是拍一个百分比。比如“P95 TTFT 退化不超过 5%，且配对差值 95% 置信区间上限不超过 8%”；阈值要区分“统计显著”和“业务显著”。**失败怎么办**：阻塞合并、阻塞发布，还是只告警？性能门禁通常阻塞发布，不阻塞合并；但如果改动涉及 kernel、调度或 KV，应该阻塞合并并要求附 benchmark。

门禁还要防“用平均值掩盖长尾”。报告至少给 P50/P95/P99，并按请求长度、batch、模型版本分组；一个总平均不变、长上下文 P99 恶化的改动，可能在真实流量下不可接受。第 26.12 节和第 29.12 节的统计方法可以直接用于门禁。

配置迁移是另一个容易被忽略的工程问题。推理服务的配置项很多，而且会随版本增删；如果没有 schema 和迁移策略，升级时会出现“旧配置被静默忽略”“新配置默认值变了”“同一份配置在不同版本行为不同”。一个最小做法是：给配置定义 schema 和版本号；启动时校验未知字段和类型；对重命名/废弃字段提供迁移函数和告警；在启动日志里打印“最终生效值 + 来源 + 版本”。这样升级时能知道哪些值变了，而不是等线上出事再回溯。

配置还要区分“可热更新”和“必须重启”。batch 上限、日志级别可以热更新；block size、并行规模、模型权重通常要重启。热更新要有原子性和回滚：先写新配置、校验、再切换，失败时保留旧值；不能一边读一边改，导致请求看到半新半旧的配置。第 34 章的 manifest 和第 35 章的 Kubernetes 配置可以承担版本绑定，但前提是应用层先把 schema 和迁移做好。

## 小结

工程化不是推理系统的装饰，而是它能不能长期运行的前提。配置要在启动时校验并打印来源；错误要分类并带上下文；日志、指标、追踪各自回答不同的问题；测试要分层并进入 CI；版本要绑定软件、配置和模型 manifest；发布要可重复、可验证、可回滚。mini-deepinfer 用很小的代码展示了这些原则的骨架，缺口本身也是教程的一部分：知道缺什么，才知道生产化要补什么。

## 练习

1. 给你的推理项目画一张依赖图，标出哪些模块是核心、哪些是可选的。如果监控 SDK 升级导致核心包装不起来，你会怎么改？
2. 设计一个配置优先级表，并写出启动时打印“最终生效值 + 来源”的格式。
3. 把 `OutOfBlocksError` 扩展成带 `seq_id`、`free_blocks`、`requested_blocks` 的异常，写一个测试断言错误信息包含这些字段。
4. 用 `tracing.py` 给 `LLMEngine.step()` 加三个 span：`schedule`、`forward`、`sample`。跑一次生成，打印每个 span 的 P95。
5. 设计一个发布门禁：列出至少五条自动检查，并说明每条失败时是阻塞合并、阻塞发布还是只告警。

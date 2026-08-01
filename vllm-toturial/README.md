# vLLM 完全教程

> 从原理到实战，从源码到复现——一份关于 vLLM 的完整教程。
> 包含一个**可运行、可验证**的简化版 vLLM 实现（纯 NumPy，无需 GPU）。

---

## 这是一份什么样的教程？

vLLM 是当下最流行的 LLM 推理与 serving 引擎。理解它，不能只停留在"会用 `vllm serve`"。
这份教程的目标是让你**真正懂 vLLM**：

- 它解决什么问题、为什么这么设计；
- 它内部有哪些模块、数据如何流动；
- 核心机制（PagedAttention、连续批处理、前缀缓存、抢占、分块 prefill）的数学与工程本质；
- 量化、投机解码、并行、生产部署这些进阶话题；
- 以及——**亲手实现一个能运行的简化版 vLLM**，把上面所有知识落到实处。

第 6 章配套的代码在 [`mini-vllm/`](mini-vllm/)：纯 NumPy 实现、约 20 个文件、
附单元测试与 OpenAI 兼容服务器，CPU 即可跑通全部验证。
读完第 6 章后，第 18 章（附录 C）是它的"工程手册"：完整 API 参考、
关键数据约定、验证矩阵与 9 个由浅入深的练习（含 1 个热身）。

---

## 章节导航

| 章节 | 标题 | 内容一句话 |
|------|------|-----------|
| 00 | [前言与导读](00-前言与导读.md) | 教程目标、阅读方法、前置知识 |
| 01 | [认识 vLLM](01-认识vLLM.md) | 是什么、为什么诞生、与其它引擎对比 |
| 02 | [核心原理](02-核心原理.md) | prefill/decode、KV cache、PagedAttention、连续批处理、前缀缓存、抢占、投机解码、量化 |
| 03 | [架构设计](03-架构设计.md) | LLMEngine、Scheduler、BlockManager、ModelRunner、并行执行、数据流 |
| 04 | [快速上手](04-快速上手.md) | 安装、`LLM` 离线推理、`vllm serve` 在线服务、OpenAI API |
| 05 | [采样与解码参数](05-采样与解码参数.md) | 温度/top-k/top-p、惩罚项、beam search、logprobs、引导解码/结构化输出 |
| 06 | [从零实现简化版 vLLM](06-从零实现简化版vLLM.md) | mini-vLLM 完整代码走读：分页 KV cache、调度、引擎、服务器、量化、投机 |
| 07 | [调度与连续批处理](07-调度与连续批处理.md) | 动态批为什么快、调度器三件事、抢占与分块 prefill 的配合 |
| 08 | [性能优化](08-性能优化.md) | PagedAttention kernel、CUDA graphs、显存调参、并行策略、benchmark |
| 09 | [量化](09-量化.md) | GPTQ / AWQ / FP8 / GGUF，如何在 vLLM 中加载量化模型 |
| 10 | [生产部署](10-生产部署.md) | Docker、Kubernetes、可观测性、结构化输出、工具调用 |
| 11 | [生态与集成](11-生态与集成.md) | HF 生态、OpenAI SDK、LangChain/LlamaIndex、Ray、自定义模型接入 |
| 12 | [未来方向](12-未来方向.md) | PD 分离、MoE 优化、注意力架构演进、长上下文 |
| 13 | [常见问题与 FAQ](13-常见问题与FAQ.md) | 安装 / 显存 / 性能 / 部署常见问题速查 |
| 14 | [分布式推理与多卡部署](14-分布式推理与多卡部署.md) | TP/PP/EP/DP、Ray 集群、多机部署与并行策略选型 |
| 15 | [多模态与 LoRA 推理](15-多模态与LoRA推理.md) | 视觉语言模型、LoRA 适配器推理 |
| 16 | [附录 A：环境变量与参数参考](16-附录A-环境变量与参数参考.md) | 环境变量 / CLI 参数分组速查 / EngineArgs 映射 |
| 17 | [附录 B：内存估算与容量规划](17-附录B-内存估算与容量规划.md) | 显存三部分 / KV 预算公式 / 容量规划实例 |
| 18 | [附录 C：mini-vLLM 完整工程参考](18-附录C-mini-vLLM完整工程参考.md) | 完整 API 参考 / 关键数据约定 / 验证矩阵 / 9 个练习（含 1 个热身）/ 调试指南 |
| 19 | [模型架构基础](19-模型架构基础.md) | MHA/GQA/MLA、RoPE/ALiBi、RMSNorm/SwiGLU、MoE，vLLM 支持的架构速查 |
| 20 | [端到端部署案例](20-端到端部署案例.md) | 从零把一个模型跑成生产服务：选型→容量规划→部署→接入→压测→监控 |
| 21 | [附录 D：性能基准测试与调优指南](21-附录D-性能基准测试与调优指南.md) | 指标定义 / 压测工具与负载设计 / 定位瓶颈 / 调优循环 / GPU profiling / 常见误区 |
| 22 | [附录 E：术语表](22-附录E-术语表.md) | 全教程关键词速查：十个主题分组 + 拼音/字母索引，随手定位术语 |

---

## ✅ 验证状态（2026-08-01 复验）

本教程配套的 **mini-vLLM 已经过完整验证**，可复现结果如下（纯 CPU 环境，Python 3.14.6 + NumPy 2.5.1，无需 GPU）：

| 验证项 | 命令 | 结果 |
|---|---|---|
| PagedAttention 正确性 | `python tests/test_paged_attention.py` | `test_paged_attention: OK` |
| 引擎不变量（确定性 / 批处理等价 / 分块 prefill / 前缀缓存 / 抢占 / 流式） | `python tests/test_engine.py` | `test_engine: OK` |
| OpenAI 服务器 + SSE | `python tests/test_server.py` | `test_server: OK` |
| 连续批处理 demo | `python -m minivllm demo --model artifacts/tinygpt` | `done in 30 engine steps` |
| 投机解码 demo | `python -m minivllm spec --model artifacts/tinygpt` | `accepted: 16 (23.5%)` |
| 量化 demo | `python -m minivllm quant --model artifacts/tinygpt` | `compression 0.26x, max err 0.00391` |
| OpenAI 兼容服务 | `python -m minivllm serve ...` | `/health`、`/v1/models`、`/v1/completions`、SSE 流式均正常 |

完整验证说明与输出见第 06 章 §6.15.5。

---

## 快速开始

### 方式一：先读，再动手

按章节顺序阅读，到第 6 章时运行配套代码：

```bash
cd vllm-toturial/mini-vllm

# 训练一个玩具模型（约 6 分钟，纯 CPU）
python scripts/train.py --steps 400 --embd 96 --layers 3 --out artifacts/tinygpt

# 验证正确性（PagedAttention / 引擎不变量 / 服务器）
python tests/test_paged_attention.py
python tests/test_engine.py
python tests/test_server.py

# 跑一个连续批处理 demo
python -m minivllm demo --model artifacts/tinygpt

# 启动 OpenAI 兼容服务器
python -m minivllm serve --model artifacts/tinygpt --port 8000
```

### 方式二：直接跑 mini-vLLM

如果你更想"先跑起来再看代码"：

```bash
cd vllm-toturial/mini-vllm
python -m minivllm demo --model artifacts/tinygpt
python -m minivllm chat --model artifacts/tinygpt
python -m minivllm spec --model artifacts/tinygpt
python -m minivllm quant --model artifacts/tinygpt
```

---

## 学习建议

1. **第 2 章是最重要的理论基础**——`KV cache` 和 `PagedAttention` 是一切的地基。
2. **第 5 章讲采样参数、第 6 章讲实现**——第 6 章配合 `mini-vllm/minivllm/` 源码精读，把每个概念和代码行对应起来。
3. 每章的"对照 mini-vLLM"小节是刻意安排的桥，帮助你把抽象概念映射到具体实现。
4. 遇到术语不确定时，先看第 2 章，再回到具体章节。
5. **实操查手册**：环境变量 / CLI 参数查第 16 章附录 A；"显存够不够、能跑多少并发"查第 17 章附录 B。进阶话题（多卡并行、多模态、LoRA）见第 14、15 章。
6. **想改 mini-vLLM / 查它的完整 API / 做扩展练习**：读第 18 章附录 C——它把第 6 章的实现升级成一份可上手的工程手册。
7. **想弄懂模型架构（GQA/MLA/RoPE/MoE）**：读第 19 章——它讲清 `num_kv_heads`、位置编码、MoE 这些决定 KV 显存与内核写法的概念。
8. **想从头到尾部署一个真实服务**：读第 20 章——它把第 4/8/10/17 章串成一个可照做的端到端案例。
9. **想独立做一次压测 / 上线前验证容量**：读第 21 章附录 D——它把"指标怎么定义、负载怎么设计、结果怎么解读"系统化，与第 8/10/17 章配合使用。
10. **被术语绕晕 / 想快速复习**：翻第 22 章附录 E 术语表——十个主题分组 + 拼音/字母索引，每个词条都标注了对应章节，是阅读与复习的"随身词典"。

> 注：教程里描述的是 vLLM 的主流/最新架构；vLLM 迭代很快，具体 API 参数
> 以官方文档为准。mini-vLLM 是实现教学用的简化版本，不代表 vLLM 的真实代码。
>
> **术语太多记不住？** 读第 22 章附录 E 术语表——十个主题分组 + 拼音/字母索引，
> 遇到陌生词随手一查，就知道该回看哪一章。

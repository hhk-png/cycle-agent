# 目录

## 第 I 部分 基础与系统观

- [第 00 章 写在前面：这套教程要解决什么问题](chapters/00-preface.md)
- [第 01 章 为什么需要 Megatron-LM](chapters/01-why-megatron.md)
- [第 02 章 全景架构与代码地图](chapters/02-architecture-map.md)
- [第 03 章 环境、配置与第一轮训练](chapters/03-environment-first-run.md)
- [第 04 章 计算与显存账本](chapters/04-compute-memory-ledger.md)

## 第 II 部分 并行原理

- [第 05 章 数据并行、梯度累积与全局 batch](chapters/05-data-parallel.md)
- [第 06 章 张量并行：把一层矩阵切开](chapters/06-tensor-parallel.md)
- [第 07 章 序列并行：让 LayerNorm 也不再复制](chapters/07-sequence-parallel.md)
- [第 08 章 流水线并行：GPipe、1F1B 与气泡](chapters/08-pipeline-parallel.md)
- [第 09 章 上下文并行与长序列](chapters/09-context-parallel.md)
- [第 10 章 MoE 与专家并行](chapters/10-mixture-of-experts.md)
- [第 11 章 显存优化：激活、状态与参数](chapters/11-memory-optimization.md)
- [第 12 章 混合精度、FP8 与 FP4](chapters/12-mixed-precision-fp8-fp4.md)
- [第 13 章 优化器与训练稳定性](chapters/13-optimizers-stability.md)
- [第 14 章 并行策略选择与性能建模](chapters/14-parallel-strategy-and-performance.md)

## 第 III 部分 mini-megatron 工程实现

- [第 15 章 mini-megatron 工程总览](chapters/15-mini-megatron-overview.md)
- [第 16 章 配置系统与进程组](chapters/16-config-and-process-groups.md)
- [第 17 章 张量并行层实现](chapters/17-tensor-parallel-layers-code.md)
- [第 18 章 Attention 实现：RoPE、GQA 与 CP](chapters/18-attention-implementation.md)
- [第 19 章 Transformer Block 与 Pipeline Stage](chapters/19-transformer-stage.md)
- [第 20 章 词表并行交叉熵](chapters/20-vocab-parallel-cross-entropy.md)
- [第 21 章 流水线调度实现](chapters/21-pipeline-schedule-implementation.md)
- [第 22 章 数据并行与 ZeRO-1 实现](chapters/22-data-parallel-implementation.md)
- [第 23 章 训练循环、日志与恢复](chapters/23-training-loop.md)
- [第 24 章 检查点、恢复与重分片](chapters/24-checkpointing-and-resharding.md)
- [第 25 章 MoE 实现：从专家复制到专家并行](chapters/25-moe-implementation.md)
- [第 26 章 推理与生成](chapters/26-inference-and-generation.md)
- [第 27 章 测试与数值等价性](chapters/27-testing-and-equivalence.md)
- [第 28 章 性能剖析与调优实验](chapters/28-profiling-and-tuning.md)

## 第 IV 部分 Megatron Core 生产栈

- [第 29 章 Megatron Core 生产代码地图](chapters/29-megatron-core-code-map.md)
- [第 30 章 模型家族与结构差异](chapters/30-model-families.md)
- [第 31 章 数据管线与训练配方](chapters/31-data-and-recipes.md)
- [第 32 章 集群、故障恢复与运维](chapters/32-cluster-and-operations.md)
- [第 33 章 调试、数值稳定性与可观测性](chapters/33-debugging-and-observability.md)

## 第 V 部分 前沿

- [第 34 章 前沿：HybridModel、DeepSeek 风格 MoE/MLA、1M 上下文、FP4 与 Muon](chapters/34-frontier-2026.md)
- [第 35 章 Megatron Bridge 与生态互操作](chapters/35-megatron-bridge-ecosystem.md)
- [第 36 章 路线图 2026–2027：技术判断与采用决策](chapters/36-roadmap-2026-2027.md)

## 第 VI 部分 工程专题

- [第 37 章 硬件、拓扑与通信工程](chapters/37-hardware-topology-communication.md)
- [第 38 章 数据工程：从原始文本到可训练 token](chapters/38-data-engineering.md)
- [第 39 章 后训练：SFT、LoRA、偏好优化与 RL](chapters/39-post-training-sft-rl.md)
- [第 40 章 推理系统：KV cache、连续批处理与 PD 分离](chapters/40-inference-systems.md)
- [第 41 章 评测、可复现性与实验管理](chapters/41-evaluation-reproducibility.md)
- [第 42 章 故障案例与规模化复盘](chapters/42-failure-cases.md)

## 第 VII 部分 商业与组织

- [第 43 章 商业分析 I：市场与价值链](chapters/43-business-market.md)
- [第 44 章 商业分析 II：TCO 与成本模型](chapters/44-business-tco.md)
- [第 45 章 商业分析 III：自建、云、托管与竞争](chapters/45-business-build-buy.md)
- [第 46 章 商业分析 IV：开源、团队、风险与投资判断](chapters/46-business-open-source-and-investment.md)
- [第 47 章 从实验到产品：模型迭代、组织与决策](chapters/47-research-to-product.md)

## 第 VIII 部分 规模化实战

- [第 48 章 端到端实战：从零训练一个 1B 模型](chapters/48-end-to-end-1b.md)
- [第 49 章 Kernel、Transformer Engine 与编译栈](chapters/49-kernels-te-compilation.md)
- [第 50 章 分布式优化器与 FSDP 深潜](chapters/50-distributed-optimizer-fsdp.md)
- [第 51 章 从 PyTorch 到 Megatron：渐进式迁移与验收](chapters/51-pytorch-migration.md)

## 总结

- [第 52 章 总结与学习路径](chapters/52-conclusion.md)

## 附录

- [附录 A 数学推导](chapters/A-math.md)
- [附录 B 配置速查](chapters/B-config-reference.md)
- [附录 C 术语表](chapters/C-glossary.md)
- [附录 D 故障排查清单](chapters/D-troubleshooting.md)
- [附录 E 参考文献与链接](chapters/E-references.md)
- [附录 F mini-megatron API 与命令](chapters/F-mini-api.md)
- [附录 G 随机性、确定性与复现手册](chapters/G-determinism-rng.md)
- [附录 H 生产训练 Runbook 与验收清单](chapters/H-production-runbook.md)
- [附录 I 硬件、成本与规模参数手册](chapters/I-hardware-cost-parameters.md)
- [附录 J 版本、路线图与声明登记表](chapters/J-claim-register.md)
- [附录 K 模型转换映射速查](chapters/K-conversion-mappings.md)
- [附录 L 练习、项目与面试题](chapters/L-exercises.md)
- [附录 M 源码阅读实验手册](chapters/M-source-reading-labs.md)
- [附录 N 故障注入与数值对照实验手册](chapters/N-fault-injection-labs.md)

## 验证

- [验证记录](VERIFICATION.md)

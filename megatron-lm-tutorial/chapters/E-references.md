# 附录 E 参考文献与链接

> 访问/核对时间：2026 年 11 月。版本、链接和厂商数字会变化，引用时请注明日期。
> 论文按主题分组；arXiv 编号便于核对，不保证永远可访问。

## E.1 官方代码与文档

- NVIDIA Megatron-LM：https://github.com/NVIDIA/Megatron-LM
- Megatron Core 文档：https://docs.nvidia.com/megatron-core/
- Megatron Core PyPI：https://pypi.org/project/megatron-core/
- Megatron Bridge：https://github.com/NVIDIA-NeMo/Megatron-Bridge
- Megatron Bridge 文档：https://docs.nvidia.com/nemo/megatron-bridge/
- Transformer Engine：https://github.com/NVIDIA/TransformerEngine
- NeMo：https://github.com/NVIDIA/NeMo
- NeMo-Run：https://github.com/NVIDIA/NeMo-Run
- vLLM：https://github.com/vllm-project/vllm
- SGLang：https://github.com/sgl-project/sglang
- TensorRT-LLM：https://github.com/NVIDIA/TensorRT-LLM
- NCCL：https://github.com/NVIDIA/nccl
- PyTorch distributed：https://pytorch.org/docs/stable/distributed.html
- PyTorch FSDP：https://pytorch.org/docs/stable/fsdp.html
- torch.distributed.checkpoint：
  https://pytorch.org/docs/stable/distributed.checkpoint.html

## E.2 并行、系统与调度

- Vaswani et al., *Attention Is All You Need*, arXiv:1706.03762.
- Shoeybi et al., *Megatron-LM: Training Multi-Billion Parameter Language
  Models Using Model Parallelism*, arXiv:1909.08053.
- Narayanan et al., *Efficient Large-Scale Language Model Training on GPU
  Clusters Using Megatron-LM*, SC 2021, arXiv:2104.04473.
- Smith et al., *Using DeepSpeed and Megatron to Train Megatron-Turing NLG
  530B*, arXiv:2201.11990.
- Huang et al., *GPipe: Efficient Training of Giant Neural Networks using
  Pipeline Parallelism*, arXiv:1811.06965.
- Narayanan et al., *PipeDream: Generalized Pipeline Parallelism for DNN
  Training*, arXiv:1806.03377.
- Narayanan et al., *Memory-Efficient Pipeline-Parallel DNN Training*
  (PipeDream-2BW), arXiv:2006.09503.
- Qi et al., *Zero Bubble Pipeline Parallelism*, arXiv:2401.10241.
- Rajbhandari et al., *ZeRO: Memory Optimizations Toward Training Trillion
  Parameter Models*, arXiv:1910.02054.
- Ren et al., *ZeRO-Offload: Democratizing Billion-Scale Model Training*,
  arXiv:2101.06840.
- Rajbhandari et al., *ZeRO-Infinity: Breaking the GPU Memory Wall for Extreme
  Scale Deep Learning*, arXiv:2104.07857.
- Zhao et al., *PyTorch FSDP: Experiences on Scaling Fully Sharded Data
  Parallel*, arXiv:2304.11277.
- Korthikanti et al., *Reducing Activation Recomputation in Large Transformer
  Models* (sequence parallelism), arXiv:2205.05198.
- Li et al., *Sequence Parallelism: Long Sequence Training from System
  Perspective*, arXiv:2105.13120.
- Liu et al., *Ring Attention with Blockwise Transformers for Near-Infinite
  Context*, arXiv:2310.01889.
- Brandon et al., *Striped Attention: Faster Ring Attention for Causal
  Transformers*, arXiv:2311.09431.
- Jia et al., *Alpa: Automating Inter- and Intra-Operator Parallelism for
  Distributed Deep Learning*, arXiv:2201.12086.
- Barham et al., *Pathways: Asynchronous Distributed Dataflow for ML*,
  arXiv:2203.12533.
- Jacobs et al., *DeepSpeed: System Optimizations Enable Training Deep Learning
  Models with Over 100 Billion Parameters*, KDD 2020.

## E.3 模型结构

- Brown et al., *Language Models are Few-Shot Learners* (GPT-3),
  arXiv:2005.14165.
- Chowdhery et al., *PaLM: Scaling Language Modeling with Pathways*,
  arXiv:2204.02311.
- Touvron et al., *Llama 2: Open Foundation and Fine-Tuned Chat Models*,
  arXiv:2307.09288.
- Jiang et al., *Mistral 7B*, arXiv:2310.06825.
- Su et al., *RoFormer: Enhanced Transformer with Rotary Position Embedding*,
  arXiv:2104.09864.
- Shazeer, *Fast Transformer Decoding: One Write-Head is All You Need* (MQA),
  arXiv:1911.02150.
- Ainslie et al., *GQA: Training Generalized Multi-Query Transformer Models from
  Multi-Head Checkpoints*, arXiv:2305.13245.
- DeepSeek-AI, *DeepSeek-V2: A Strong, Economical, and Efficient
  Mixture-of-Experts Language Model* (MLA), arXiv:2405.04434.
- DeepSeek-AI, *DeepSeek-V3 Technical Report*, arXiv:2412.19437.
- Meta AI, *The Llama 3 Herd of Models*, arXiv:2407.21783.
- Gu & Dao, *Mamba: Linear-Time Sequence Modeling with Selective State Spaces*,
  arXiv:2312.00752.
- Lieber et al., *Jamba: A Hybrid Transformer-Mamba Language Model*,
  arXiv:2403.19887.
- Fedus et al., *Switch Transformers: Scaling to Trillion Parameter Models
  with Simple and Efficient Sparsity*, arXiv:2101.03961.
- Dao & Gu, *Transformers are SSMs: Generalized Models and Efficient
  Algorithms Through Structured State Space Duality* (Mamba-2),
  arXiv:2405.21060.
- Lepikhin et al., *GShard: Scaling Giant Models with Conditional Computation
  and Automatic Sharding*, arXiv:2006.16668.
- Du et al., *GLaM: Efficient Scaling of Language Models with
  Mixture-of-Experts*, arXiv:2112.06905.
- Gale et al., *MegaBlocks: Efficient Sparse Training with Mixture-of-Experts*,
  arXiv:2211.15841.
- Hwang et al., *Tutel: Adaptive Mixture-of-Experts at Scale*, arXiv:2206.03382.
- Fedus et al., *A Review of Sparse Expert Models in Deep Learning*,
  arXiv:2209.01667.

## E.4 精度、注意力与 kernel

- Micikevicius et al., *Mixed Precision Training*, arXiv:1710.03740.
- Dao et al., *FlashAttention: Fast and Memory-Efficient Exact Attention with
  IO-Awareness*, arXiv:2205.14135.
- Dao, *FlashAttention-2: Faster Attention with Better Parallelism and Work
  Partitioning*, arXiv:2307.08691.
- Shah et al., *FlashAttention-3: Fast and Accurate Attention with Asynchrony
  and Low-Precision*, arXiv:2407.08608.
- Micikevicius et al., *FP8 Formats for Deep Learning*, arXiv:2209.05433.
- NVIDIA, *Transformer Engine documentation*:
  https://docs.nvidia.com/deeplearning/transformer-engine/
- NVIDIA, *NVFP4 / Blackwell 精度材料*：以官方数据手册和 TE 文档为准。
- Cai et al., *Medusa: Simple LLM Inference Acceleration Framework with
  Multiple Decoding Heads*, arXiv:2401.10774.
- Li et al., *EAGLE: Speculative Sampling Requires Rethinking Feature
  Uncertainty*, arXiv:2401.15077.
- Li et al., *EAGLE-2: Faster Inference of Language Models with Dynamic Draft
  Trees*, arXiv:2406.16858.
- Yuan et al., *Native Sparse Attention: Hardware-Aligned and Natively
  Trainable Sparse Attention*, arXiv:2502.11089.
- Hsieh et al., *RULER: What's the Real Context Size of Your Long-Context
  Language Models?*, arXiv:2404.06654.

## E.5 优化器与训练配方

- Kingma & Ba, *Adam: A Method for Stochastic Optimization*, arXiv:1412.6980.
- Loshchilov & Hutter, *Decoupled Weight Decay Regularization* (AdamW),
  arXiv:1711.05101.
- Jordan, *Muon: An optimizer for hidden layers in neural networks*, 2024
  （博客/代码，非 arXiv；核对实现时以官方仓库为准）。
- Kaplan et al., *Scaling Laws for Neural Language Models*, arXiv:2001.08361.
- Hoffmann et al., *Training Compute-Optimal Large Language Models*
  (Chinchilla), arXiv:2203.15556.
- McCandlish et al., *An Empirical Model of Large-Batch Training*,
  arXiv:1812.06162.
- Goyal et al., *Accurate, Large Minibatch SGD: Training ImageNet in 1 Hour*,
  arXiv:1706.02677.
- Yang et al., *Tensor Programs V: Tuning Large Neural Networks via Zero-Shot
  Hyperparameter Transfer* (μP/μTransfer), arXiv:2203.03466.
- Schulman et al., *Proximal Policy Optimization Algorithms* (PPO),
  arXiv:1707.06347.
- Rafailov et al., *Direct Preference Optimization* (DPO), arXiv:2305.18290.
- Shao et al., *DeepSeekMath: Pushing the Limits of Mathematical Reasoning in
  Open Language Models* (GRPO), arXiv:2402.03300.
- Ethayarajh et al., *KTO: Model Alignment as Prospect Theoretic Optimization*,
  arXiv:2402.01306.
- Azar et al., *A General Theoretical Paradigm to Understand Learning from
  Human Preferences* (IPO), arXiv:2310.12036.
- Hong et al., *ORPO: Monolithic Preference Optimization without Reference
  Model*, arXiv:2403.07691.
- Lightman et al., *Let's Verify Step by Step* (PRM), arXiv:2305.20050.

## E.6 后训练、推理与服务

- Ouyang et al., *Training language models to follow instructions with human
  feedback* (InstructGPT), arXiv:2203.02155.
- Christiano et al., *Deep Reinforcement Learning from Human Preferences*,
  arXiv:1706.03741.
- Hu et al., *LoRA: Low-Rank Adaptation of Large Language Models*,
  arXiv:2106.09685.
- Dettmers et al., *QLoRA: Efficient Finetuning of Quantized LLMs*,
  arXiv:2305.14314.
- Kwon et al., *Efficient Memory Management for Large Language Model Serving
  with PagedAttention*, arXiv:2309.06180.
- Leviathan et al., *Fast Inference from Transformers via Speculative
  Decoding*, arXiv:2211.17192.
- Chen et al., *Accelerating Large Language Model Decoding with Speculative
  Sampling*, arXiv:2302.01318.
- Agrawal et al., *Taming Throughput-Latency Tradeoff in LLM Inference with
  Sarathi-Serve* (chunked prefill), arXiv:2403.02310.
- Zheng et al., *SGLang: Efficient Execution of Structured Language Model
  Programs*, arXiv:2312.07104.
- Yu et al., *Orca: A Distributed Serving System for Transformer-Based
  Generative Models*, OSDI 2022, arXiv:2207.05221.
- Zhong et al., *DistServe: Disaggregating Prefill and Decoding for Goodput-
  optimized Large Language Model Serving*, arXiv:2401.09670.
- Patel et al., *Splitwise: Efficient Generative LLM Inference Using Phase
  Splitting*, arXiv:2311.18677.
- Qin et al., *Mooncake: A KVCache-centric Disaggregated Architecture for LLM
  Serving*, arXiv:2407.00079.
- Sheng et al., *HybridFlow: A Flexible and Efficient RLHF Framework* (verl),
  arXiv:2409.19256.
- NVIDIA, *NIXL (NVIDIA Inference Xfer Library)*:
  https://github.com/ai-dynamo/nixl

## E.7 评测、可复现与工程

- Srivastava et al., *Beyond the Imitation Game* (BIG-bench),
  arXiv:2206.04615.
- Liang et al., *Holistic Evaluation of Language Models* (HELM),
  arXiv:2211.09110.
- Efron & Tibshirani, *An Introduction to the Bootstrap*, 1993.
- Pineau et al., *Improving Reproducibility in Machine Learning Research*,
  JMLR 2021.
- Henderson et al., *Deep Reinforcement Learning that Matters*, arXiv:1709.06560.
- Bouthillier et al., *Accounting for Variance in Machine Learning Benchmarks*,
  arXiv:2103.03098.
- Hendrycks et al., *Measuring Massive Multitask Language Understanding*
  (MMLU), arXiv:2009.03300.
- Rein et al., *GPQA: A Graduate-Level Google-Proof Q&A Benchmark*,
  arXiv:2311.12022.
- Jimenez et al., *SWE-bench: Can Language Models Resolve Real-World GitHub
  Issues?*, arXiv:2310.06770.
- Yao et al., *τ-bench: A Benchmark for Tool-Agent-User Interaction in
  Real-World Domains*, arXiv:2406.12045.

## E.8 2026 路线图/发布信息

- Megatron-LM releases：https://github.com/NVIDIA/Megatron-LM/releases
- Megatron Core MoE 文档（0.19，历史版本；0.20 的对应页面把版本段换成
  `0.20.0`，以官方站点为准）：
  https://docs.nvidia.com/megatron-core/developer-guide/0.19.0/user-guide/features/moe.html
- Megatron Core 文档入口（latest）：
  https://docs.nvidia.com/megatron-core/developer-guide/latest/
- Megatron Core MoE README（dev）：
  https://github.com/NVIDIA/Megatron-LM/blob/dev/megatron/core/transformer/moe/README.md
- Megatron Core inference 文档：
  https://docs.nvidia.com/megatron-core/developer-guide/latest/user-guide/features/inference.html
- 具体 roadmap issue 编号会随仓库迁移变化；引用时请给出访问日期和 issue 标题，
  不要只写编号。

## E.9 厂商/行业材料（注意口径）

- NVIDIA H100/B200/GB300 数据手册（峰值 TFLOPs 随精度/稀疏性变化）。
- NVIDIA 关于 Megatron/NeMo 的博客与 GTC 材料。
- 云厂商 GPU 实例定价页（按需/预留/Spot 差异很大）。
- MLPerf Training/Inference 结果（benchmark 口径与真实业务不同）。
- SemiAnalysis/行业分析（适合建立量级感，不作为技术事实来源）。

## E.10 本书代码

- mini-megatron：本目录 `mini_megatron/`
- 测试：`tests/`
- 配置：`configs/`
- 验证：`scripts/verify.py`
- 验证记录：[VERIFICATION.md](../VERIFICATION.md)

## E.11 引用建议

写报告/论文时，至少注明：

```text
Megatron Core version, PyTorch/CUDA/TE version, GPU model/count,
TP/PP/DP/CP/EP, precision, sequence length, batch size, MFU definition,
whether communication/checkpointing is included.
```

否则“MFU 47%”“1M context”“FP4 训练”这类数字无法比较。引用厂商数字时，还要
注明是 dense 还是 sparse、是否含重算、是否只算 GEMM、是否端到端。

---

上一附录：[附录 D 故障排查清单](D-troubleshooting.md)  
返回目录：[SUMMARY](../SUMMARY.md)  
下一附录：[附录 F mini-megatron API 与命令](F-mini-api.md)

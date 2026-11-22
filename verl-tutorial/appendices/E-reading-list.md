# 附录 E 阅读清单

## E.1 核心论文

- HybridFlow: <https://arxiv.org/abs/2409.19256>
- PPO: <https://arxiv.org/abs/1707.06347>
- GAE: <https://arxiv.org/abs/1506.02438>
- DeepSeekMath / GRPO: <https://arxiv.org/abs/2402.03300>
- DAPO: <https://arxiv.org/abs/2503.14476>
- GSPO: <https://arxiv.org/abs/2507.18071>
- RLOO: <https://arxiv.org/abs/2402.14740>
- REINFORCE++: <https://arxiv.org/abs/2501.03262>
- ReMax: <https://arxiv.org/abs/2310.10505>
- DPO: <https://arxiv.org/abs/2305.18290>
- InstructGPT（RLHF）: <https://arxiv.org/abs/2203.02155>
- Constitutional AI: <https://arxiv.org/abs/2212.08073>
- RLAIF: <https://arxiv.org/abs/2309.00267>
- Reward Model Overoptimization: <https://arxiv.org/abs/2210.10760>
- DeepSeek-R1: <https://arxiv.org/abs/2501.12948>
- LoRA: <https://arxiv.org/abs/2106.09685>
- QLoRA: <https://arxiv.org/abs/2305.14314>
- VAPO: <https://arxiv.org/abs/2504.05118>
- PRIME: <https://arxiv.org/abs/2502.01456>
- SPPO（Self-Play Preference Optimization）: <https://arxiv.org/abs/2405.00675>
- Let's Verify Step by Step（PRM）: <https://arxiv.org/abs/2305.20050>
- Tree of Thoughts: <https://arxiv.org/abs/2305.10601>
- ReST（拒绝采样微调）: <https://arxiv.org/abs/2308.08998>
- STaR: <https://arxiv.org/abs/2203.14465>
- AlphaZero（自博弈 + 搜索）: <https://arxiv.org/abs/1712.01815>

## E.2 系统与工程

- FlashAttention: <https://arxiv.org/abs/2205.14135>
- PagedAttention/vLLM: <https://arxiv.org/abs/2309.06180>
- SGLang/RadixAttention: <https://arxiv.org/abs/2312.07104>
- Speculative decoding（Leviathan et al.）: <https://arxiv.org/abs/2211.17192>
- Speculative sampling（Chen et al.）: <https://arxiv.org/abs/2302.01318>
- GQA: <https://arxiv.org/abs/2305.13245>
- Megatron-LM: <https://arxiv.org/abs/1909.08053>
- ZeRO: <https://arxiv.org/abs/1910.02054>
- PyTorch FSDP2: <https://pytorch.org/docs/stable/distributed.fsdp.fully_shard.html>
- Ray: <https://docs.ray.io/>

## E.3 官方文档

- verl 文档: <https://verl.readthedocs.io/>
- 安装: <https://verl.readthedocs.io/en/latest/start/install.html>
- 快速开始: <https://verl.readthedocs.io/en/latest/start/quickstart.html>
- 配置: <https://verl.readthedocs.io/en/latest/examples/config.html>
- Engine Workers: `docs/workers/engine_workers.rst`
- Model Engine: `docs/workers/model_engine.rst`
- 多轮: <https://verl.readthedocs.io/en/latest/sglang_multiturn/multiturn.html>
- AgentLoop: `docs/advance/agent_loop.rst`
- Reward Loop: `docs/advance/reward_loop.rst`
- LoRA: `docs/advance/ppo_lora.rst`
- 多机: `docs/start/multinode.rst`
- Fully async: `docs/advance/fully_async.md`、`docs/advance/v1_async_trainer.md`
- Checkpoint 与 delta 同步: `docs/advance/checkpoint.rst`、`docs/advance/delta_weight_sync.md`
- 确定性: `docs/advance/determinism.md`
- Placement/调度: `docs/advance/placement.rst`
- 后端 Workers: `docs/workers/automodel_workers.rst`、`docs/workers/torchtitan_workers.rst`、`docs/workers/trtllm_worker.rst`
- DeepSeek V4 接入: `docs/advance/deepseek_v4_integration.rst`
- 工具系统示例: `docs/examples/sandbox_fusion_example.rst`、`docs/sglang_multiturn/sandbox_fusion.rst`
- SkyPilot: `docs/examples/skypilot_examples.rst`
- Muon: `examples/muon/README.md`
- Router replay 示例: `examples/router_replay/README.md`

## E.4 仓库

- verl: <https://github.com/verl-project/verl>
- verl-recipe: <https://github.com/verl-project/verl-recipe>
- verl-omni: <https://github.com/verl-project/verl-omni>
- verl-vla: <https://github.com/verl-project/verl-vla>
- RL-Insight: <https://github.com/verl-project/rl-insight>
- uni-agent: <https://github.com/verl-project/uni-agent>

## E.5 博客/教程

- verl releases/新闻: <https://github.com/verl-project/verl/releases>
- vLLM blog: <https://blog.vllm.ai/>
- SGLang: <https://docs.sglang.ai/>
- Qwen-VL: <https://github.com/QwenLM/Qwen2.5-VL>
- LiveCodeBench: <https://livecodebench.github.io/>
- SWE-bench: <https://www.swebench.com/>
- MMMU: <https://mmmu-benchmark.github.io/>

## E.6 阅读顺序建议

1. HybridFlow 论文（架构）；
2. PPO/GAE（算法）；
3. GRPO/DAPO/GSPO（算法变体）；
4. vLLM/PagedAttention + SGLang/RadixAttention（rollout；容量规划见附录 Z）；
5. FSDP/Megatron（训练）；
6. verl 文档与源码（工程）；
7. 本教程的 MiniVerl（可运行）。

## E.7 论文阅读方法

- 先看问题/贡献/架构图；
- 再看公式与假设；
- 最后看实验设置与局限；
- 对每篇论文问：它解决什么失效模式？代价是什么？在什么条件下会失效？

## E.8 源码阅读方法

- 从入口/调用链读，不要从目录读；
- 打印 `config`，看最终字段；
- 用最小配置跑一遍；
- 打印中间张量形状；
- 对比 MiniVerl 的实现。

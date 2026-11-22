# 附录 B 配置速查

> 先分清两套配置：**B.1–B.3 是 mini 的 `ExperimentConfig` JSON 字段**，
> **B.6 才是 Megatron Core 的训练启动参数**。两边的同名项（如
> `sequence_parallel`）约束不同，不能把 mini 的限制当成生产限制，反之亦然。

## B.1 模型（mini）

| 字段 | 含义 | 约束 |
|---|---|---|
| `vocab_size` | 词表大小 | 能被 TP 整除 |
| `max_seq_len` | 最大序列 | CP/SP 整除 |
| `hidden_size` | 隐藏维 | 能被 TP/heads 整除 |
| `num_layers` | 层数 | 能被 PP 整除 |
| `num_attention_heads` | Q heads | 能被 TP 整除 |
| `num_query_groups` | KV groups | 能被 TP 整除 |
| `ffn_hidden_size` | FFN 维 | 能被 TP 整除 |
| `layernorm_epsilon` | LayerNorm eps | |
| `attention_dropout` / `hidden_dropout` | dropout | 默认 0 |
| `init_method_std` | 初始化标准差 | |
| `add_bias_linear` / `add_qkv_bias` | 线性层 bias | 影响参数量（见 4.2/analysis） |
| `gated_linear_unit` | SwiGLU gate | 与 `activation` 配合 |
| `activation` | `swiglu` / `gelu` | |
| `position_embedding` | `rope` / `learned` | |
| `rotary_base` / `rotary_interleaved` | RoPE | 与转换/推理一致 |
| `tie_word_embeddings` | 共享 embedding | **mini** 要求 PP=1 且不能与 SP 同开；生产 Megatron 可跨 PP 共享（`finalize_model_grads` 对 embedding 梯度做 PP 归约），不要照搬 mini 限制 |
| `use_moe` | 启用 MoE | |
| `num_experts` | 专家数 | 能被 EP 整除 |
| `moe_topk` | 每 token 专家数 | ≤ num_experts |
| `moe_capacity_factor` | 容量因子 | |
| `moe_aux_loss_coeff` | aux loss 系数 | 0 表示关闭 |
| `activation_checkpointing` | 激活重算 | 整层粒度 |
| `fp8_simulation` | FP8 数值模拟 | 不加速 |

## B.2 并行（mini）

| 字段 | 含义 | 备注 |
|---|---|---|
| `tensor_model_parallel_size` | TP | 建议 ≤ NVLink 域 |
| `pipeline_model_parallel_size` | PP | 层数整除 |
| `context_parallel_size` | CP | 序列整除 |
| `expert_model_parallel_size` | EP | mini 只能为 1 或 dp_size（`ep=dp`） |
| `sequence_parallel` | SP | mini 需 TP>1，且不能与 CP 同开；生产支持 SP+CP 组合，但实现复杂 |
| `distributed_optimizer` | 状态分片 | DP>1 生效；与 EP 不能同开 |
| `distributed_optimizer_style` | `zero1` / `flat` | `flat` 为 reduce-scatter/all-gather |
| `pipeline_schedule` | `1f1b` / `gpipe` | |

## B.3 训练（mini）

| 字段 | 含义 |
|---|---|
| `micro_batch_size` | 每 micro-batch 样本数 |
| `global_batch_size` | 全局 batch |
| `seq_len` | 训练序列长度 |
| `max_steps` | 总步数 |
| `lr/min_lr` | 学习率 |
| `warmup_steps` | warmup |
| `lr_decay_style` | cosine/linear/constant/inverse_sqrt |
| `grad_clip` | 全局范数裁剪 |
| `weight_decay` | AdamW |
| `adam_beta1/2` | 默认 0.9/0.95 |
| `adam_eps` | 默认 1e-8 |
| `save_interval` | 检查点间隔 |
| `eval_interval/eval_iters` | 评估 |
| `log_interval` | 日志间隔 |
| `checkpoint_dir` | 检查点目录 |
| `device` | cpu/cuda |
| `dtype` | float32/bfloat16/float16 |
| `use_autocast` | 混合精度前向（**mini** 不能与 PP>1 同开；生产通过 p2p dtype 协商支持） |
| `use_grad_scaler` | fp16 梯度缩放 |
| `data` | synthetic/bin |
| `data_path` | bin 文件 |
| `synthetic_vocab_size` | 合成数据词表覆盖 |
| `log_jsonl` | 结构化日志路径 |
| `seed` | 随机种子 |

## B.4 常用配置

```bash
# 单卡
python -m mini_megatron.cli train --config configs/tiny.json

# TP=2
python scripts/run_distributed.py --world-size 2 --config configs/tiny_tp2.json

# PP=2
python scripts/run_distributed.py --world-size 2 --config configs/tiny_pp2.json

# TP=2+PP=2
python scripts/run_distributed.py --world-size 4 --config configs/tiny_tp2_pp2.json

# MoE+TP
python scripts/run_distributed.py --world-size 2 --config configs/tiny_moe_tp2.json

# EP=2
python scripts/run_distributed.py --world-size 2 --config configs/tiny_ep2.json

# TP=2+EP=2（4 进程）
python scripts/run_distributed.py --world-size 4 --config configs/tiny_tp2_ep2.json

# CP=2 / TP=2+CP=2
python scripts/run_distributed.py --world-size 2 --config configs/tiny_cp2.json
python scripts/run_distributed.py --world-size 4 --config configs/tiny_tp2_cp2.json

# flat distributed optimizer（DP=2）
python scripts/run_distributed.py --world-size 2 --config configs/tiny_dp2_flat.json

# 生成
python -m mini_megatron.cli generate --config configs/tiny.json \
  --checkpoint checkpoints/tiny --prompt 1,2,3,4 --max-new-tokens 8 --temperature 0

# 代价估算
python -m mini_megatron.cli estimate --config configs/tiny.json \
  --tokens 1e12 --gpu h100 --mfu 0.4

# VPP 规划
python -m mini_megatron.pipeline.interleaved --pp 4 --chunks-per-rank 2 \
  --layers 16 --microbatches 8
```

## B.5 环境变量

| 变量 | 用途 |
|---|---|
| `WORLD_SIZE/RANK/MASTER_ADDR/MASTER_PORT` | torchrun 分布式 |
| `LOCAL_RANK` | 节点内 GPU 编号 |
| `GLOO_SOCKET_IFNAME` | Gloo 网卡（Linux/部分 Windows） |
| `NCCL_SOCKET_IFNAME` | NCCL 使用的网卡 |
| `NCCL_DEBUG=INFO` | NCCL 调试 |
| `NCCL_DEBUG_SUBSYS=ALL` | 更详细的 NCCL 子系统日志 |
| `NCCL_IB_SL/NCCL_IB_TC` | IB Service Level / Traffic Class |
| `NCCL_NET_GDR_LEVEL` | GPUDirect RDMA 级别 |
| `NCCL_ALGO/NCCL_PROTO` | 固定集合通信算法/协议（复现性/排障） |
| `TORCH_DISTRIBUTED_DEBUG=DETAIL` | PyTorch 分布式调试 |
| `TORCH_NCCL_BLOCKING_WAIT=1` | NCCL 超时后阻塞/抛错行为 |
| `TORCH_NCCL_ASYNC_ERROR_HANDLING=1` | 异步错误处理 |
| `CUDA_DEVICE_MAX_CONNECTIONS=1` | 减少 TP 通信与计算争用（Megatron 常见） |
| `OMP_NUM_THREADS/MKL_NUM_THREADS` | CPU 线程 |
| `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` | 显存碎片 |
| `CUBLAS_WORKSPACE_CONFIG=:4096:8` | 确定性 cuBLAS |
| `CUDA_LAUNCH_BLOCKING=1` | 同步 CUDA 调用，定位异步错误（很慢） |

## B.6 Megatron Core 常用启动参数

> 以下参数名以 2026 年 11 月的 Megatron Core 0.20.x 为基线。Megatron 的参数
> 演进很快，实际使用请以 `megatron/training/arguments.py` 和
> `transformer_config.py` 为准；不同版本的默认值/是否弃用可能不同。

### B.6.1 模型结构

| 参数 | 含义 | 备注 |
|---|---|---|
| `--num-layers` | Transformer 层数 | 需被 PP 整除（VPP 还要被 p·v 整除） |
| `--hidden-size` | hidden 维 | 需被 TP 整除 |
| `--num-attention-heads` | Q head 数 | 需被 TP 整除 |
| `--num-query-groups` | KV head/group 数 | GQA 时使用；mini 要求被 TP 整除 |
| `--group-query-attention` | 启用 GQA | 与 `--num-query-groups` 配合 |
| `--ffn-hidden-size` | FFN 中间维 | 需被 TP 整除 |
| `--seq-length` | 训练序列长度 | 需被 CP 整除 |
| `--max-position-embeddings` | 最大位置 | 可大于 seq-length |
| `--vocab-size` | 词表大小 | 生产常 pad 到 TP 的倍数 |
| `--position-embedding-type` | `rope`/`learned`/`alibi` | |
| `--rotary-base` | RoPE base | 长上下文常调大 |
| `--rotary-percent` | 使用 RoPE 的维度比例 | 部分模型用 |
| `--swiglu` | 使用 SwiGLU | 与 `--ffn-hidden-size` 配合 |
| `--normalization` | `LayerNorm`/`RMSNorm` | |
| `--layernorm-epsilon` | Norm epsilon | |
| `--untie-embeddings-and-output-weights` | 不共享 embedding/输出 | 默认常共享 |
| `--disable-bias-linear` | 线性层无 bias | Llama 类常用 |
| `--add-qkv-bias` | QKV 加 bias | 部分模型用 |

### B.6.2 并行与调度

| 参数 | 含义 | 备注 |
|---|---|---|
| `--tensor-model-parallel-size` | TP | 建议 ≤ NVLink 域 |
| `--pipeline-model-parallel-size` | PP | 层数整除 |
| `--context-parallel-size` | CP | 序列整除 |
| `--expert-model-parallel-size` | EP | MoE 专家分组 |
| `--expert-tensor-parallel-size` | 专家内 TP | 与 EP 组合 |
| `--sequence-parallel` | 启用 SP | 需 TP>1 |
| `--use-distributed-optimizer` | 分布式优化器/ZeRO-1 | DP 组内分片状态 |
| `--overlap-param-gather` | 参数 gather 与计算重叠 | 分布式优化器 |
| `--overlap-grad-reduce` | 梯度 reduce 与反向重叠 | |
| `--tp-comm-overlap` | TP 通信重叠 | 需硬件/版本支持 |
| `--num-layers-per-virtual-pipeline-stage` | 每个 VPP chunk 的层数 | 控制 interleaving |
| `--pipeline-model-parallel-split-rank` | 手动指定 stage 切分点 | 不规则切分 |
| `--decoder-pipeline-model-parallel-size` | encoder-decoder 的 decoder PP | T5 类 |
| `--use-custom-fsdp` | 启用 Megatron FSDP | 新版本 |
| `--data-parallel-sharding-strategy` | FSDP 分片策略 | `no_shard`/`optim`/`optim_grads`/`optim_grads_params`（旧文档常写成 `grad_and_optim`，以当前源码为准） |

### B.6.3 训练与优化器

| 参数 | 含义 |
|---|---|
| `--micro-batch-size` | 每 micro-batch 样本数 |
| `--global-batch-size` | 全局 batch |
| `--train-iters` / `--train-samples` | 训练步数/样本数 |
| `--lr` / `--min-lr` | 学习率范围 |
| `--lr-decay-style` | `cosine`/`linear`/`constant`/`inverse_sqrt` |
| `--lr-warmup-fraction` / `--lr-warmup-iters` | warmup |
| `--weight-decay` | AdamW 权重衰减 |
| `--clip-grad` | 全局梯度范数裁剪 |
| `--adam-beta1` / `--adam-beta2` / `--adam-eps` | Adam 超参 |
| `--eval-interval` / `--eval-iters` | 评估频率/步数 |
| `--save-interval` | 检查点间隔 |
| `--save` / `--load` | 检查点目录 |
| `--use-checkpoint-args` | 从 checkpoint 恢复模型参数 |
| `--no-save-optim` / `--no-load-optim` | 不保存/加载优化器状态 |
| `--no-load-rng` | 不加载 RNG（调试/迁移） |
| `--finetune` | 微调模式（重置迭代/数据） |

### B.6.4 精度、注意力与显存

| 参数 | 含义 | 备注 |
|---|---|---|
| `--fp16` / `--bf16` | 混合精度 | 大模型优先 bf16 |
| `--fp8-format` | `e4m3`/`e5m2`/`hybrid` | 需 TE/硬件支持 |
| `--fp8-recipe` | `tensorwise`/`blockwise`/`mxfp8` 等 | `nvfp4` 是 FP4，不是 FP8 |
| `--fp4-format` | `e2m1` 等 | 需要 Blackwell/TE；与 `--fp8-format` 互斥 |
| `--fp4-recipe` | `nvfp4` 等 | 名称/取值以 `arguments.py` 为准；与 FP8 recipe 互斥 |
| `--fp8-amax-history-len` | delayed scaling 的 amax 历史长度 | |
| `--fp8-amax-compute-algo` | amax 计算算法 | `most_recent`/`max` |
| `--fp8-param-gather` | FP8 参数 gather | 分布式优化器 |
| `--use-flash-attn` | 启用 FlashAttention | |
| `--attention-backend` | `auto`/`flash`/`unfused`/`local` | 版本相关 |
| `--recompute-activations` | 激活重算 | 显存换计算 |
| `--recompute-granularity` | `full`/`selective` | |
| `--recompute-method` | `uniform`/`block` | |
| `--recompute-num-layers` | 重算层数 | |
| `--use-distributed-optimizer` | 分片优化器状态 | 省显存 |

### B.6.5 MoE

| 参数 | 含义 | 备注 |
|---|---|---|
| `--num-experts` | 专家数 | |
| `--moe-router-topk` | 每 token 选几个专家 | |
| `--moe-aux-loss-coeff` | 负载均衡 aux loss 系数 | |
| `--moe-z-loss-coeff` | router z-loss 系数 | 稳定训练 |
| `--moe-token-dispatcher-type` | `allgather`/`alltoall` | EP 语义 |
| `--moe-expert-capacity-factor` | 专家容量系数 | 控制丢弃 |
| `--moe-pad-expert-input-to-capacity` | 补齐到容量 | 影响确定性 |
| `--moe-grouped-gemm` | 分组 GEMM | 性能 |
| `--moe-router-load-balancing-type` | aux loss/其它 | 版本相关 |
| `--moe-layer-freq` | 哪些层是 MoE | |
| `--moe-ffn-hidden-size` | 专家 FFN 维 | |
| `--moe-shared-expert-intermediate-size` | 共享专家维 | DeepSeek 类 |

### B.6.6 检查点与推理

| 参数 | 含义 |
|---|---|
| `--ckpt-format` | `torch`/`torch_dist`/`zarr`/`fsdp_dtensor`（版本相关；以当前源码为准） |
| `--async-save` | 异步保存 |
| `--dist-ckpt-strictness` | 分布式检查点严格度 |
| `--save-retain-interval` | 保留检查点的间隔 |
| `--inference-dynamic-batching` | 推理动态批处理 |
| `--inference-dynamic-batching-max-requests` | 最大并发请求 |
| `--inference-dynamic-batching-max-tokens` | 最大 token 数 |
| `--inference-dynamic-batching-buffer-size-gb` | KV/缓冲大小 |
| `--inference-dynamic-batching-block-size` | paged KV block 大小 |
| `--inference-dynamic-batching-num-cuda-graphs` | CUDA Graph 数量 |

## B.7 并行配置的整除关系

```text
world_size % (tp * pp * cp) == 0
hidden_size % tp == 0
num_attention_heads % tp == 0
num_query_groups % tp == 0          # mini/GQA 的简化要求
ffn_hidden_size % tp == 0
vocab_size % tp == 0                # 生产可 pad
num_layers % pp == 0                # 均匀切分
seq_len % cp == 0
seq_len % tp == 0                   # 开 SP 时
num_layers % (pp * v) == 0          # VPP，v 为每 rank chunk 数
num_experts % ep == 0               # EP
global_batch % (micro_batch * dp) == 0
```

## B.8 参数选择速查

| 场景 | 建议 |
|---|---|
| 单卡/小模型调试 | TP=PP=CP=1，micro-batch 尽量大，先不用 FP8 |
| 7B–13B 单节点 | TP=1/2/4，PP=1，DP 填满；SP 视激活而定 |
| 70B 多节点 | TP=4/8（NVLink 内），PP=2/4/8，DP 填剩余；ZeRO-1 省状态 |
| 长上下文（32K+） | CP=2/4/8，配合 SP/重算；注意 attention 实现 |
| MoE 大模型 | EP=8/16/32，TP 专家按显存；all-to-all 与负载均衡 |
| 显存紧 | 重算 + 分布式优化器 + 增大 TP/PP/CP + bf16/FP8 |
| 复现性优先 | 固定拓扑/算法/seed，关非确定 kernel，接受性能损失 |

## B.9 配置审查清单

```text
[ ] 每个并行度都能整除对应维度
[ ] 每卡状态/激活显存在预算内（用 analysis/estimate 先算）
[ ] TP 不跨 IB；PP 切分点负载均衡
[ ] global batch 与 LR 匹配（大 batch 要调 LR/warmup）
[ ] 数据 sampler 与 DP size 匹配
[ ] checkpoint 布局与并行度兼容
[ ] 精度 recipe 与硬件/模型匹配
[ ] 所有 rank 的配置完全一致
```


---

上一附录：[附录 A 数学推导](A-math.md)  
返回目录：[SUMMARY](../SUMMARY.md)  
下一附录：[附录 C 术语表](C-glossary.md)

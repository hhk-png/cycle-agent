# 附录 I 硬件与集群配方

## I.1 GPU 选择

| 场景 | 推荐 | 显存 | 备注 |
|---|---|---|---|
| 单卡调试 | RTX 4090/L40S/A10 | 24～48 GB | 0.5B～1.5B |
| 8 卡 7B | A100/H100 80GB | 80 GB | FSDP2 |
| 32B | H100/H200 | 80～141 GB | TP/PP |
| 235B MoE | H100/H200 集群 | 80～141 GB | Megatron EP + 多机 |
| 671B MoE | H100/H200 集群 | 80～141 GB | Megatron EP/PP |
| 国产 | Ascend 910B/910C | — | MindSpeed/verl NPU |
| AMD | MI300X | 192 GB | ROCm 支持视版本 |

## I.2 互联

- 单机：NVLink/NVSwitch；
- 多机：InfiniBand（NDR/HDR）或 RoCE；
- 存储：NFS/GPFS/Lustre/S3；
- 网络：至少 100 Gb/s，推荐 200～400 Gb/s IB。

RL 的权重同步/TP/EP all-to-all 对网络敏感；跨节点 TP 通常不推荐，优先 PP/DP。

## I.3 节点配置建议

```text
CPU: 64～128 cores
RAM: 512 GB～1 TB
GPU: 8×80 GB
本地 NVMe: 数 TB（checkpoint/数据缓存）
网络: 1～8×IB
```

`--shm-size` 在容器中要足够大（64 GB+），否则 Ray/DataLoader 会出问题。

## I.4 7B FSDP2 配方

```yaml
trainer:
  n_gpus_per_node: 8
  nnodes: 1
actor_rollout_ref:
  actor:
    strategy: fsdp2
    ppo_mini_batch_size: 64
    ppo_micro_batch_size_per_gpu: 4
    fsdp_config:
      param_offload: false
      optimizer_offload: false
      reshard_after_forward: true
  rollout:
    name: vllm
    tensor_model_parallel_size: 2
    gpu_memory_utilization: 0.5
    enable_prefix_caching: true
```

## I.5 32B Megatron 配方

```yaml
model_engine: megatron
actor_rollout_ref:
  actor:
    strategy: megatron
    megatron:
      tensor_model_parallel_size: 4
      pipeline_model_parallel_size: 2
      sequence_parallel: true
      use_distributed_optimizer: true
  rollout:
    tensor_model_parallel_size: 4
trainer:
  n_gpus_per_node: 8
  nnodes: 4
```

本配方是 32 卡（TP4 × PP2 × DP4）。16 卡也能用（DP=2），但显存/吞吐更紧；具体并行度按 hidden/layers/专家数调整。32 卡是配方选择，不是训练 32B 的最低要求。

## I.6 671B MoE 配方（量级）

```yaml
model_engine: megatron
actor_rollout_ref:
  actor:
    strategy: megatron
    megatron:
      tensor_model_parallel_size: 8
      pipeline_model_parallel_size: 8
      # EP 必须在 DP 内部：EP <= DP = 总卡数 / (TP * PP)
      expert_model_parallel_size: 8
      sequence_parallel: true
  rollout:
    tensor_model_parallel_size: 8
    expert_parallel_size: 8
trainer:
  nnodes: 64          # 64 × 8 = 512 卡；TP8 × PP8 = 64，DP = 8，EP <= 8
  n_gpus_per_node: 8
```

这张表是“能构成的配置”而不是显存最优解。若要把 EP 提到 64，需要 DP ≥ 64，即至少 4096 卡；在 512 卡上 EP=64 不合法。实际配比要按模型层数/专家数/显存反复试，并需要高速 IB、充足的 checkpoint 存储、专门的运维。

## I.7 Ascend NPU

- 使用专用镜像；
- `trainer.device=npu`；
- MindSpeed/Megatron 后端；
- HCCL 替代 NCCL；
- 检查算子支持与精度。

## I.8 CPU/开发环境

MiniVerl：

```bash
python -X utf8 -m mini_verl.trainer.main_ppo
python -m pytest tests/ -q
```

真实 verl 的 CPU 模式（如果有）仅用于 CI/数据预处理，不适合训练。

## I.9 云与竞价

- 竞价实例便宜但会被回收，必须 checkpoint/重试；
- 多机需要同可用区/低延迟网络；
- 存储要与计算同区域；
- 注意出口流量费用。

## I.10 监控硬件

```text
nvidia-smi / dcgm
GPU 利用率、显存、温度、功耗
NVLink/IB 错误计数
CPU/RAM/磁盘
网络丢包/重传
```

训练前跑一次 NCCL bandwidth test；训练中监控 straggler。

## I.11 成本与规模

- 8×H100 集群的月成本（云）可能在数万美元量级；
- 671B 训练一次可能数万～数十万美元；
- 自建要考虑折旧/电/运维；
- 先用小集群验证，再扩规模。

这些是量级，不是报价；以实际云厂商/采购为准。

## I.12 平台抽象与 AMD ROCm：同一套 trainer 怎么落到不同芯片

verl 的并行/训练代码默认假设 NVIDIA CUDA，但 v0.9.1 把“平台差异”收进了 `verl/plugin/platform/`：

- `platform_base.py`：设备、流、事件、通信器、内存分配器、profiler 的抽象；
- `platform_cuda.py` / `platform_rocm.py` / `platform_npu.py`：具体实现；
- `platform_manager.py`：按环境选择平台，供 engine/worker 查询。

这层抽象不改变算法，但决定了“同一份配置能不能在另一块芯片上跑”。移植时要逐层确认，而不是只换 `torch` wheel。

### 平台抽象覆盖什么

| 维度 | CUDA | ROCm | Ascend NPU |
|---|---|---|---|
| 设备 API | `torch.cuda` | `torch.cuda`（HIP 后端） | `torch_npu` |
| 集合通信 | NCCL | RCCL | HCCL |
| 注意力 kernel | FlashAttention/FA3、cuDNN | CK/Triton、flash-attn ROCm 分支 | Ascend 融合算子 |
| 低精度 | BF16/FP8（依架构） | BF16/FP8（依架构） | BF16/FP16，FP8 依型号 |
| 推理引擎 | vLLM/SGLang/TensorRT-LLM | vLLM ROCm/SGLang ROCm | vLLM-Ascend/SGLang NPU |
| 图模式 | CUDA Graph | HIP Graph | ACL graph/torch.compile |
| Profiler | Nsight | rocprof | msprof |

表里每一项都可能有版本矩阵。**“支持某芯片”通常指某个 verl commit + 某个 torch 版本 + 某个推理引擎 commit 的组合**，不是任意组合。

### AMD ROCm 的落地要点

1. **通信器**：RCCL 的 API 与 NCCL 接近，但拓扑发现、网卡绑定、`NCCL_*` 环境变量的语义不完全相同；多机时优先用厂商验证过的容器。
2. **kernel 覆盖**：训练侧的 FlashAttention、rollout 侧的 paged attention 都要有 ROCm 实现；缺 kernel 时框架可能回退到慢路径，表现为“能跑但吞吐只有一半”。
3. **低精度**：FP8 在 CDNA3/CDNA4 上的支持与 CUDA Hopper 不同；不要假设 `fp8` 配置跨平台等价。
4. **推理引擎**：vLLM/SGLang 的 ROCm 分支通常滞后于 CUDA 主线；先确认目标模型的算子覆盖，再谈 RL 配方。
5. **确定性**：不同 kernel 的归约顺序不同，`full_determinism` 的可行性要单独验证；不要拿 CUDA 的 seed 复现结果直接对比。

### Ascend NPU 的落地要点

1. **HCCL**：集合通信的配置方式与 NCCL 不同；多机要确认 rank table、网卡与 HCCL 版本。
2. **torch_npu**：算子覆盖与 eager/graph 模式相关；某些动态 shape 在 graph 模式下会重编译或回退。
3. **推理**：vLLM-Ascend/SGLang NPU 的 continuous batching、prefix caching、PD 支持程度要逐项确认。
4. **精度**：BF16/FP16 为主，FP8 依型号；KL/log-prob 的数值差异要用固定 batch 对照，而不是只看 loss 曲线。

### 移植检查清单

- [ ] `torch`/驱动/通信库/推理引擎的版本矩阵来自同一份官方验证组合；
- [ ] 单卡前向数值与参考实现对齐（固定输入、固定 dtype）；
- [ ] 单机多卡 all-reduce/all-gather 正确（小张量逐元素对照）；
- [ ] 多机通信与网卡绑定正确；
- [ ] rollout 引擎的采样、prefix cache、stop 条件与训练侧一致；
- [ ] 权重同步的 dtype/布局正确（尤其 LoRA 与量化）；
- [ ] checkpoint 能跨平台读写（至少 safetensors 的 dtype/shape 一致）；
- [ ] profiler 能采到 step 级时间分解；
- [ ] 固定 seed 下的方差在可接受范围；
- [ ] 故障注入（掉卡、通信超时）有可恢复路径。

### 常见失败模式

| 现象 | 可能原因 | 排查 |
|---|---|---|
| 启动即报 device 相关错误 | 平台插件未选中/版本不匹配 | 打印 `platform_manager` 选择结果 |
| 多卡 hang | RCCL/HCCL 拓扑或网卡绑定 | 单机 2 卡 → 4 卡 → 多机逐级放大 |
| 吞吐只有预期一半 | kernel 回退/未启用融合算子 | profiler 看 kernel 名称与占比 |
| loss 与 CUDA 不一致 | 精度/kernel 归约顺序 | 固定 batch 对比 logits/log-prob |
| rollout 与训练 log-prob 偏差大 | 推理引擎精度/采样实现不同 | 对比同一 token 的 log-prob，查 16.5 |
| checkpoint 读不了 | dtype/布局/分片元数据不兼容 | 用 safetensors 做最小往返测试 |

### MiniVerl 的边界

MiniVerl 只跑 CPU，`n_gpus_per_node` 是“语义上的卡数”，用于演示 dispatch 与分片，不代表真实设备。它不能验证 ROCm/NPU 的 kernel、通信或精度问题；这些必须在目标硬件上用真实 verl 跑。可迁移的是**检查清单与数值对照方法**，不是性能数字。

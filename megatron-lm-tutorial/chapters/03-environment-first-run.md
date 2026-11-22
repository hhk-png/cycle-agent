# 第 03 章 环境、配置与第一轮训练

## 3.1 版本选择

| 组件 | 建议 | 原因 |
|---|---|---|
| Python | 3.12+（推荐 3.12；本轮在 3.14.6 上验证） | Megatron Core 0.20 / Bridge 已要求 ≥3.12 |
| PyTorch | 2.9.1+ | 本教程在 2.9.1+cpu 上验证；更新版本通常也可，需看 Gloo/Windows 兼容性 |
| NumPy | 2.x | memmap 数据与部分依赖 |
| GPU | 任意 CUDA GPU（非必需） | mini 可在 CPU 跑；生产需要 NVIDIA GPU |
| 驱动/CUDA | 与 PyTorch 轮子匹配 | 生产还要匹配 Transformer Engine |

检查环境：

```bash
python -V
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
nvidia-smi        # 可选
```

如果只是学习并行原理，**不需要 GPU**。`configs/tiny*.json` 都是 CPU 可跑的
小模型（hidden=64、vocab=128）；除 `tiny_tp2_pp2.json` 为 4 层外，其余为
2 层，单步耗时通常在秒级以内。

## 3.2 安装

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt
```

如果 PyPI 的默认轮子带 CUDA 依赖而你只需要 CPU：

```bash
# Linux
pip install torch --index-url https://download.pytorch.org/whl/cpu
# Windows（CPU 轮子；本教程在 2.9.1+cpu 上验证）
pip install torch==2.9.1 --index-url https://download.pytorch.org/whl/cpu
```

> 生产 Megatron 还需要 `transformer-engine`、`apex`（部分功能）、NCCL、
> CUDA toolkit 和匹配的 cuDNN/cuBLAS。mini 刻意不依赖这些，以便在笔记本上验证
> 并行逻辑。

### 3.2.1 生产栈的组成

生产训练不是一个 `pip install` 能解决的，至少四层要对齐：

```text
驱动 / CUDA runtime
  └─ PyTorch（cu 版本）
       ├─ NCCL（集合通信）
       ├─ Transformer Engine（FP8/FP4/fused attention/linear）
       ├─ FlashAttention / cuDNN（注意力 kernel）
       ├─ apex（部分 fused optimizer/norm，可选）
       └─ Megatron Core / Megatron Bridge
```

版本匹配的经验规则：

1. **驱动 ≥ CUDA runtime 要求**：驱动向下兼容，但不能低于 PyTorch 编译所用
   CUDA 的最低版本。
2. **PyTorch 的 cu 版本与 NCCL/TE 匹配**：TE 对 PyTorch/CUDA 有明确支持矩阵，
   不要混用 wheel 与源码编译。
3. **NCCL 与网络/驱动匹配**：IB/RoCE 的 GPUDirect 支持依赖驱动与 NCCL 版本。
4. **Megatron Core 与 Bridge 的版本匹配**：Bridge 的 recipe/转换脚本对 Core
   版本敏感。

一个最小验证脚本：

```python
import torch, torch.distributed as dist
print(torch.__version__, torch.version.cuda, torch.cuda.is_available())
print(torch.cuda.get_device_name(0), torch.cuda.get_device_capability(0))
print(torch.cuda.nccl.version() if torch.cuda.is_available() else "no cuda")
try:
    import transformer_engine.pytorch as te
    print("TE", te.__version__)
except Exception as e:
    print("TE unavailable:", e)
try:
    import flash_attn
    print("flash-attn", flash_attn.__version__)
except Exception as e:
    print("flash-attn unavailable:", e)
```

安装顺序通常是：先装匹配的 PyTorch → 再装 TE/FlashAttention → 最后装
Megatron Core/Bridge。`apex` 建议按官方说明从源码编译，且只启用你真正需要的
扩展；它不是 mini 的依赖。

### 3.2.2 容器与镜像

生产环境优先用容器，而不是在宿主机上堆 pip 包：

```text
[ ] 基于 NGC PyTorch 或云厂商的 CUDA 镜像
[ ] 固定镜像 digest，不用 latest
[ ] 在镜像内安装 TE/FlashAttention/Megatron，记录版本
[ ] 镜像内含健康检查脚本（torch/NCCL/IB/GPU 可见性）
[ ] 训练代码通过挂载/拷贝进入，不修改基础镜像
[ ] 镜像扫描（CVE/许可证/恶意包）
```

镜像的价值是**把“环境”变成可复现的 artifact**。当事故发生时，你可以回到
某个 digest，而不是猜“当时装的是哪个版本”。

## 3.3 配置系统

`ExperimentConfig` 由三部分组成：

```python
from mini_megatron.config import ExperimentConfig

cfg = ExperimentConfig.from_json("configs/tiny.json")
print(cfg.model.hidden_size, cfg.parallel.tensor_model_parallel_size)
```

三个 dataclass：

- `ModelConfig`：词表、层数、hidden、注意力头/GQA、FFN、RoPE、MoE、FP8 模拟等；
- `ParallelConfig`：TP/PP/CP、序列并行、分布式优化器、流水线调度；
- `TrainConfig`：micro/global batch、seq_len、LR、warmup、日志、检查点、数据源。

`validate()` 会在建模型前检查：

- hidden/heads/query groups 能否被 TP 整除；
- vocab/FFN/序列长度能否被 TP 整除；
- 层数能否被 PP 整除；
- global batch 能否被 `micro_batch × DP` 整除；
- 不支持的组合（SP+CP、tie+SP、autocast+PP 等）直接报错。

**先校验再建模型**是一个重要的工程习惯：大规模任务里，配置错误如果等到
第 3000 步才暴露，代价极高。

## 3.4 单进程训练

```bash
python -m mini_megatron.cli train --config configs/tiny.json
```

你会看到类似输出：

```json
{"event": "start", "world_size": 1, "dp_size": 1, "grad_accum": 2, "parameters_this_stage": 98944}
{"step": 0, "loss": 4.84, "aux_loss": 0.0, "lr": 0.00015, "grad_norm": 1.36, "tokens_per_sec": 3200}
{"step": 1, "loss": 4.81, "aux_loss": 0.0, "lr": 0.0003, "grad_norm": 1.69, "tokens_per_sec": 10000}
...
```

几个字段的含义：

- `grad_accum=2`：global batch=4，micro batch=2，DP=1，所以累积 2 次；
- `parameters_this_stage`：当前 TP/PP 分片上的参数量，不是全模型参数量；
- `aux_loss`：MoE 负载均衡辅助损失；dense 模型为 0；
- `tokens_per_sec`：本 step 的吞吐，含通信与优化器，不等于纯 GEMM 吞吐。

`configs/tiny.json` 默认训练 10 步，并在 `checkpoints/tiny/` 保存最终检查点。

## 3.5 多进程训练

### Linux / macOS：torchrun

```bash
torchrun --standalone --nproc_per_node=2 -m mini_megatron.cli train \
  --config configs/tiny_tp2.json
```

`torchrun` 会设置 `RANK/WORLD_SIZE/MASTER_ADDR/MASTER_PORT`，CLI 的
`setup_distributed()` 调用 `comm.initialize_torch()`。

### Windows：portable 启动器

```bash
python scripts/run_distributed.py --world-size 2 --config configs/tiny_tp2.json
python scripts/run_distributed.py --world-size 2 --config configs/tiny_pp2.json
python scripts/run_distributed.py --world-size 4 --config configs/tiny_tp2_pp2.json
```

`run_distributed.py` 做三件事：

1. 为每个 rank 创建一个 `multiprocessing.Queue`；
2. 用 `spawn` 启动子进程（Windows 必须）；
3. 在子进程里先 `comm.initialize_portable(rank, world, queues[rank], queues)`，
   再调用 CLI。

这样 CLI 不需要知道底层是 Gloo 还是 Queue；所有并行代码只依赖 `comm` API。

### 为什么 Windows 要用 portable

某些 PyTorch Windows 轮子在 `init_process_group("gloo")` 时报
`makeDeviceForHostname(): unsupported gloo device`。常见规避方式是换更新
版本的轮子、设置 `GLOO_SOCKET_IFNAME` 或使用 `use_libuv=False`，但不同
机器/版本表现不一致。portable 后端让“并行逻辑的正确性”与“平台通信是否可用”解耦；生产
训练仍应使用 NCCL/Gloo。

### 3.5.1 多机多卡启动

单机用 `torchrun --nproc_per_node=8` 即可；多机需要 rendezvous：

```bash
# 示例：2 节点 × 2 进程 = world 4；tiny_tp2 在 world 4 下是 TP=2 × DP=2
# 节点 0（master）
torchrun --nnodes=2 --node_rank=0 \
  --master_addr=10.0.0.1 --master_port=29500 \
  --nproc_per_node=2 -m mini_megatron.cli train --config configs/tiny_tp2.json

# 节点 1
torchrun --nnodes=2 --node_rank=1 \
  --master_addr=10.0.0.1 --master_port=29500 \
  --nproc_per_node=2 -m mini_megatron.cli train --config configs/tiny_tp2.json
```

要点：

- `nnodes × nproc_per_node` 必须等于配置能接受的 world size；`tiny_tp2` 的
  `global_batch_size=4, micro_batch_size=2`，world=8 时 TP=2、DP=4，
  `global_batch % (micro_batch × DP) = 4 % 8 ≠ 0`，直接跑会触发
  `ValueError`。最小可用值是 8（`grad_accum=1`），不是 16；放大到 8 卡时
  可以调大 `global_batch_size`，或换一个匹配的配置；
- 每个节点一个 `node_rank`，`master_addr` 必须是所有节点可达的地址；
- `MASTER_PORT` 要避开占用；容器网络要放行；
- 所有节点的代码/配置/镜像/环境变量一致；
- 先跑 2 节点 × 2 进程的 smoke test，再放大。

这不是 mini 的限制，而是**全局 batch 整除关系**：任何并行配置都必须满足
`global_batch_size % (micro_batch_size × dp) == 0`。生产里常见做法是固定
global batch，按 world size 反推 `micro_batch_size × grad_accum`。

Slurm 下常见写法：

```bash
# 与上面的 2 节点 × 2 进程示例一致；放大时同时换配置/global batch
srun --nodes=2 --ntasks-per-node=1 --gpus-per-task=2 \
  torchrun --nnodes=2 --node_rank=$SLURM_NODEID \
  --master_addr=$(scontrol show hostname $SLURM_NODELIST | head -1) \
  --master_port=29500 --nproc_per_node=2 \
  -m mini_megatron.cli train --config configs/tiny_tp2.json
```

注意 Slurm 的 `SLURM_NODEID` 是**作业内的节点编号**（0..nnodes-1），不是
“节点内的 GPU 索引”；`SLURM_LOCALID` 才是节点内进程号。跨作业/弹性调度时
要确认编号连续，不连续时用主机名映射。

## 3.6 一次分布式训练发生了什么

> 阅读提示：这一节会提到 TP/SP 的 all-gather/reduce-scatter。第一次读不必
> 完全理解；先建立“启动 → 建组 → 建模型 → 前向/反向 → 更新”的顺序，
> 第 6–7 章会展开通信语义。

以 `configs/tiny_tp2.json` 为例（TP=2，PP=1，CP=1，world=2）：

```text
rank 0                          rank 1
  ├─ 初始化 comm（portable）       ├─ 初始化 comm
  ├─ init_model_parallel(tp=2)    ├─ init_model_parallel(tp=2)
  ├─ 建 TP 分片模型                ├─ 建 TP 分片模型
  ├─ 取同一批数据（DP=1）          ├─ 取同一批数据
  ├─ forward：q/k/v 列并行         ├─ forward：q/k/v 列并行
  │    └─ 行并行 reduce-scatter    │    └─ 行并行 reduce-scatter
  ├─ CE：local vocab + all-reduce  ├─ CE
  ├─ backward：列并行 grad_input   ├─ backward
  │    └─ all-reduce               │    └─ all-reduce
  └─ optimizer.step()              └─ optimizer.step()
```

`tiny_tp2.json` 打开了 `sequence_parallel=true`，所以这里不是普通 TP：

- q/k/v/MLP 的输出维仍按 TP 切，但 LayerNorm/Dropout/残差所在的激活按**序列**
  切成 `s/tp`，不是复制的；
- 行并行的输出用 **reduce-scatter** 回到序列分片布局，而不是 all-reduce；
- 位置编码/部分 norm 参数仍是复制参数；
- 反向时 Column 的 `grad_input` 在普通 TP 下做 all-reduce；SP 下与 forward
  的 all-gather 对偶，生产实现用 reduce-scatter（mini 拆成
  `split + TP all-reduce`，语义等价）。Row 的 forward reduce-scatter 在反向
  对应 all-gather（mini 用 `all_reduce + chunk` 实现 forward，见 7.3）。

如果只想看“纯 TP”的行为，把 `sequence_parallel` 设为 `false`，此时行并行
才是 all-reduce、LayerNorm/残差是复制的。两种布局的参数量都约为单卡的一半
（复制参数除外）。

## 3.7 常用命令

```bash
# 查看配置展开后的并行度
python -m mini_megatron.cli info --config configs/tiny.json

# 基准测试（不写检查点）
python -m mini_megatron.cli benchmark --config configs/tiny.json --steps 5

# 从检查点生成
python -m mini_megatron.cli generate --config configs/tiny.json \
  --checkpoint checkpoints/tiny --prompt "1,2,3,4" --max-new-tokens 8

# 自动恢复
python -m mini_megatron.cli train --config configs/tiny.json \
  --steps 20 --checkpoint-dir checkpoints/tiny --auto-resume
```

## 3.8 常见错误与定位

| 现象 | 常见原因 | 处理 |
|---|---|---|
| `world_size is not divisible by tp*pp*cp` | 启动进程数与配置不符 | 改 `--world-size` 或配置 |
| `hidden_size must be divisible by TP` | 模型维度与 TP 不匹配 | 调整 hidden/TP |
| `global_batch_size must be divisible...` | 梯度累积无法整除 | 调整 batch |
| `makeDeviceForHostname` | Windows Gloo 轮子问题 | 用 `run_distributed.py` 或换更新版本 |
| 进程卡住无输出 | p2p/collective 不匹配 | 检查所有 rank 是否走同一 schedule；看 `comm` 超时栈 |
| loss 为 NaN | LR 过大、bf16 溢出、CE mask 错 | 降 LR、用 fp32 复现、检查 labels |
| 检查点恢复报布局不匹配 | TP/PP/CP/DP 改变 | 用同布局恢复，或先做重分片转换 |

调试分布式问题的第一原则：**先让 world=1 复现**。如果单进程就错，问题在模型/
数值；如果只有多进程错，再查 group、collective 顺序和 p2p。

### 3.8.1 首轮训练验收清单

```text
[ ] 单进程 1 步能跑通，loss 有限
[ ] 2 进程 TP/PP/CP/DP 各跑 2 步
[ ] 4 进程组合跑 2 步
[ ] 保存/恢复一次，loss 连续
[ ] 生成一次，输出不是乱码/越界
[ ] 日志包含 rank/step/loss/grad_norm/tokens_per_sec
[ ] 显存峰值在预算内
[ ] 没有 NCCL 超时/重传告警
```

这份清单对应 `scripts/verify.py` 的最小矩阵；生产环境还要加上多机/网络/存储
检查（见附录 H）。

## 3.9 本章小结

- Python 3.12+ 是版本要求；本书本轮在 **3.14.6**、PyTorch **2.9.1+cpu** 上
  验证（2.10 未列入本轮验证），生产还需 TE/NCCL 等。
- 配置校验必须在建模型前完成。
- Linux 用 torchrun，Windows 可用 portable 启动器。
- 单进程先跑通，再逐维增加并行度，是最有效的调试路径。

下一章建立计算与显存的“账本”，解释为什么需要这些并行维度。

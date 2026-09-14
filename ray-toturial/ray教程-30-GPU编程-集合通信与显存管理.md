仓库地址：https://github.com/hhk-png/cycle-agent

# 第 30 章：GPU 编程、集合通信与显存管理

> 本章目标：把「Ray 上的 GPU」讲透。前面几章反复出现过 GPU ——
> 第 8 章讲了 `num_gpus` 的资源语义，第 13 章讲了并行策略，第 29 章讲了训练库 ——
> 但**没有一章回答最基础的问题**：
>
> * `CUDA_VISIBLE_DEVICES` 到底是什么时候、由谁设的？
> * `num_gpus=0.5` 时两个任务真的共享显存吗？
> * NCCL 卡住不动，该看哪个环境变量？
> * 「CUDA out of memory」有几种完全不同的成因？
>
> 读完这一章，你应该能在**不看文档**的情况下排查大部分 GPU 问题。

---

## 30.1 `CUDA_VISIBLE_DEVICES`：Ray 到底做了什么

这是整个 GPU 体系里最基础、也最容易被误解的机制。

### 核心机制：Ray 不「分配」GPU，它「遮蔽」GPU

Ray 没有 GPU 虚拟化能力。它做的是**进程级的设备可见性隔离**：

```
物理机: 8 张卡, /dev/nvidia0 ... /dev/nvidia7
                    │
   ┌────────────────┼────────────────┐
   ▼                ▼                ▼
worker A          worker B         worker C
CUDA_VISIBLE_     CUDA_VISIBLE_    CUDA_VISIBLE_
DEVICES=0         DEVICES=1        DEVICES=2,3
   │                │                │
 torch.cuda.       torch.cuda.      torch.cuda.
 device_count()=1  device_count()=1 device_count()=2
 cuda:0 就是物理卡0  cuda:0 是物理卡1  cuda:0/1 是物理卡2/3
```

**关键推论**：在 worker 内部，**`cuda:0` 永远是「我分到的那张卡」**，
而不是物理 0 号卡。所以你的代码里应该写 `cuda:0` 或者干脆写 `cuda`，
**绝对不要**写 `cuda:3` —— 那会指向「我这个进程可见的第 4 张卡」，
而它多半不存在。

```python
import ray
import torch

@ray.remote(num_gpus=1)
def where_am_i():
    import os
    return {
        "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "ray.get_gpu_ids()": ray.get_gpu_ids(),          # 物理卡号
        "torch.cuda.device_count()": torch.cuda.device_count(),
        "torch.cuda.current_device()": torch.cuda.current_device(),
    }

# 假设在 8 卡机器上 ray.init(num_gpus=8)
print(ray.get(where_am_i.remote()))
# {'CUDA_VISIBLE_DEVICES': '3',
#  'ray.get_gpu_ids()': [3],
#  'torch.cuda.device_count()': 1,
#  'torch.cuda.current_device()': 0}     ← 注意这里是 0,不是 3
```

`ray.get_gpu_ids()` 返回的是**物理卡号**（运维/日志视角），
`torch.cuda.current_device()` 返回的是**进程内逻辑编号**（代码视角）。
**这两个数字在非 0 号卡上永远不相等** —— 这是查 GPU 问题时最常见的困惑源。

### 什么时候设置？

**在 worker 进程启动时、在用户的 Python 代码执行之前。**

Ray 的 worker 是 `spawn` 出来的子进程，`CUDA_VISIBLE_DEVICES` 通过
进程环境注入。这带来两条实践约束：

**① 不能「先 import torch 再改环境变量」。**
CUDA 运行时在**第一次调用 CUDA API 时**读取 `CUDA_VISIBLE_DEVICES` 并缓存。
所以：

```python
import os
import torch                        # ← 这里还没初始化 CUDA,通常没事
os.environ["CUDA_VISIBLE_DEVICES"] = "0"   # ← 危险:可能已经太晚了
torch.cuda.is_initialized()        # ← 如果这里有 True,上面的设置无效
```

**② 想自己接管设备可见性，有官方开关。**

```bash
# 如果你在外部(如 K8s device plugin)已经设好了 CUDA_VISIBLE_DEVICES,
# 不希望 Ray 覆盖它:
export RAY_EXPERIMENTAL_NOSET_CUDA_VISIBLE_DEVICES=1
```

> **这个名字是确定的**：`RAY_EXPERIMENTAL_NOSET_CUDA_VISIBLE_DEVICES`
> （以 `RAY_EXPERIMENTAL_` 开头表示 API 可能变，但目前就是这个拼写）。
> ⚠️ 需要**确认**的是它与你所用版本的**交互细节**（比如设置了它之后
> Ray 记账里 `get_gpu_ids()` 还会不会返回正确结果）——
> 以你所用版本的 `ray/_private/accelerators/` 下的实现为准。
> **不要因为"实验性"就以为名字不确定**，名字是查得到的。
> 用之前先在目标版本上验一下。

### 一个真实的高频事故

```python
import os
os.environ["CUDA_VISIBLE_DEVICES"] = "0,1,2,3"    # ← 在任务里硬写
import torch
```

在单机上这样写「能跑」，但一旦上了 Ray：

* Ray 给这个 worker 设的是 `CUDA_VISIBLE_DEVICES=5`；
* 你的代码把它**改成了 `0,1,2,3`**；
* 于是这个 worker 能看见 4 张卡，而 Ray 的账本里它只用 1 张；
* 结果是**资源超配**：8 个 worker 都认为自己有 4 张卡，全挤在物理 0-3 上，
  而 4-7 号卡闲着。

**规则**：在 Ray 里**永远不要自己设 `CUDA_VISIBLE_DEVICES`**。
需要选择设备时用 `num_gpus` 和 `accelerator_type`（第 8 章 §8.2）；
需要读设备号时用 `ray.get_gpu_ids()`。

---

## 30.2 分数 GPU：`num_gpus=0.5` 到底发生了什么

这个特性被广泛误解。它**不是**显存切分，也**不是**算力隔离。

### 实际行为

`num_gpus=0.5` 时，Ray 做两件事：

1. **调度层面**：把这张卡记成「还能再放一个 0.5」—— 同一张卡可以容纳 2 个
   声明了 0.5 的 worker；
2. **环境层面**：两个 worker 的 `CUDA_VISIBLE_DEVICES` **都指向同一张物理卡**。

所以：

| 问题 | 答案 |
|---|---|
| 两个 worker 能看到同一张卡吗？ | **能**，而且看到的是同一张 |
| 显存隔离吗？ | **不隔离**。谁先要到谁拿走，OOM 会互相连累 |
| 算力隔离吗？ | **不隔离**。两个进程的 kernel 抢同一批 SM |
| Ray 会阻止我超用吗？ | **不会**。Ray 只记账，不做硬件限制 |
| 那它有什么用？ | **提高利用率**：模型小、算力没吃满时，两个副本共享一张卡很划算 |

### 什么时候该用

```python
# ✓ 合适:每个副本只吃 ~30% 显存、算力也没吃满(小模型多副本推理)
@ray.remote(num_gpus=0.5)
def embed(texts): ...

# ✗ 不合适:训练。训练会吃满显存和算力,共享只会互相拖慢 + OOM
@ray.remote(num_gpus=0.5)
def train(): ...
```

### 显存必须自己分

因为 Ray 不隔离显存，**你必须显式限制每个副本的显存**，
否则第一个副本会把卡吃满：

```python
@ray.remote(num_gpus=0.5)
def embed(texts):
    import torch
    # 给这个副本设上限 —— 否则两个副本会打架
    torch.cuda.set_per_process_memory_fraction(0.45)   # 留 10% 给 CUDA 上下文
    ...
```

> **经验规则**：分数 GPU 只适合**推理**，不适合训练；
> 且必须配 `set_per_process_memory_fraction`。
> 如果两个副本都要「尽可能快」，那它们本来就不该共享一张卡 ——
> 用 `num_gpus=1` 让 Ray 把它们摊到不同卡上。

### 想要真正的隔离：MIG

见第 8 章 §8.2 的 MIG 一节。简单说：MIG 提供**硬件级**切分
（独立显存 + 独立算力），代价是运维要提前切好，且 Ray 只是「被告知有多少张卡」。

---

## 30.3 集合通信：`torch.distributed` 怎么在 Ray 上初始化

这是「Ray + 多卡」最容易出问题的一段。先把一个事实说清楚：

> **Ray 不做集合通信。** all-reduce 是 NCCL 的事，
> Ray 只负责把 `MASTER_ADDR` / `RANK` / `WORLD_SIZE` 这些东西**送对**。

### 用 Ray Train：什么都不用做

```python
from ray.train.torch import TorchTrainer, prepare_model

def train_func(config):
    model = MyModel()
    model = prepare_model(model)      # ← Ray 帮你包 DDP + 注入环境变量
    # 此刻 torch.distributed 已经初始化好了
    ...
```

`prepare_model` 内部**只做两件事**：
1. 用 `ray.train.get_context()` 里的 `get_local_rank()`，把模型搬到对应的 GPU 上（`cuda:LOCAL_RANK`）;
2. 按 `parallel_strategy` 把模型包起来（默认 `DistributedDataParallel`）。

> ⚠️ **它不初始化 `torch.distributed`。** 那个进程组是 **Ray Train 的 Torch 后端**
> 在 `train_func` 跑起来**之前**就建好的（`ray.train.torch.config` 里那句
> `dist.init_process_group(...)`）。所以上面代码注释里那句
> 「此刻 `torch.distributed` 已经初始化好了」**结论没错，功劳不在 `prepare_model`**。
> 这一层归属必须分清 —— 否则排查 NCCL 超时时你根本不知道该看哪一层。

**用 HF `Trainer` 时不要调它** —— HF 会自己初始化（第 29 章 §29.3）。

### 手写：Ray 注入的环境变量清单

脱离 Ray Train 自己用 Ray Core 起多进程时，你要自己处理。
**Ray Train 会帮你注入下面这一整套**（⚠️ 注意：这些是 **Ray Train** 注入的，
**不是 Ray Core** —— 只用 `@ray.remote` 的话一个都没有）：

| 变量 | 含义 |
|---|---|
| `MASTER_ADDR` | rank 0 所在节点的地址 |
| `MASTER_PORT` | 通信端口 |
| `WORLD_SIZE` | 总进程数 |
| `RANK` | **全局**序号（跨节点） |
| `LOCAL_RANK` | **节点内**序号（决定用哪张本地卡） |
| `LOCAL_WORLD_SIZE` | 本节点的进程数 |
| `NODE_RANK` / `GROUP_RANK` | 节点序号 |

> ⚠️ **这些变量由 Ray Train 注入，不是 Ray Core 注入的。**
> 如果你只用 `@ray.remote` 自己起 worker，**`MASTER_ADDR` 这些是不存在的** ——
> 你必须自己选一个节点当 master、自己算端口、自己传下去。
> 这正是 Ray Train 存在的理由之一。**未确认**：Ray Core 是否有部分注入行为，
> 但**不要依赖它**。
>
> 📌 **这张表里曾经有过一行 `CUDA_VISIBLE_DEVICES`，第五轮已删掉。**
> 它**不属于本表** —— `CUDA_VISIBLE_DEVICES` 是 **Ray Core**（raylet / worker
> 启动时）注入的，见 §30.1；mini-ray 也注入它并提供 `get_gpu_ids()`（§30.9）。
> 把它放进"只用 `@ray.remote` 一个都没有"的清单里是**自相矛盾**的，
> 留在这里只会误导。

### 自己起的正确姿势（如果一定要）

```python
import os
import ray
import torch.distributed as dist

@ray.remote(num_gpus=1)
def worker(rank, world_size, master_addr, master_port):
    os.environ["MASTER_ADDR"] = master_addr
    os.environ["MASTER_PORT"] = str(master_port)
    os.environ["RANK"] = str(rank)
    os.environ["WORLD_SIZE"] = str(world_size)
    # Ray 已经设好了 CUDA_VISIBLE_DEVICES,所以本地卡永远是 cuda:0
    os.environ["LOCAL_RANK"] = "0"

    from datetime import timedelta              # ⚠️ 不是 torch.timedelta —— 没有这个类

    dist.init_process_group(
        backend="nccl",
        timeout=timedelta(minutes=30),          # ⚠️ 默认 10 分钟,大模型不够
    )
    try:
        ...
    finally:
        dist.destroy_process_group()
```

**超时**是最常被忽略的一行。`init_process_group` 的默认超时是 **10 分钟**，
而一个 70B 模型的第一次 all-gather 可能要更久 ——
表现为「训练莫名其妙地在 10 分钟处报 NCCL timeout」。

---

## 30.4 NCCL：环境变量速查与「卡住」排查

NCCL 挂起（hang）是分布式训练最经典的故障：**没有报错，只是不动了**。

### 第一件事：打开日志

```bash
export NCCL_DEBUG=WARN     # 默认。只看警告
export NCCL_DEBUG=INFO     # 排查时用:打印拓扑、算法选择、通道建立
export NCCL_DEBUG=TRACE    # 更细,但输出极大,只在定位特定问题时用

# 只关心某几个子系统(输出能少 90%)
export NCCL_DEBUG_SUBSYS=INIT,NET,GRAPH
```

NCCL 的日志量很大，建议同时落盘：

```bash
export NCCL_DEBUG_FILE=/tmp/nccl.%h.%p.log     # %h=host %p=pid
```

### 高频环境变量

| 变量 | 用途 | 典型场景 |
|---|---|---|
| `NCCL_SOCKET_IFNAME` | 指定走哪张网卡 | **多网卡机器必设**，否则可能选了管理网（慢）或选错网段（直接不通） |
| `NCCL_IB_HCA` | 指定用哪个 IB 网卡 | 多张 HCA 时 |
| `NCCL_IB_DISABLE=1` | 禁用 InfiniBand | 排查「是不是 IB 的问题」 |
| `NCCL_P2P_DISABLE=1` | 禁用 GPUDirect P2P | 排查 NVLink/P2P 拓扑问题 |
| `NCCL_NET_GDR_LEVEL` | GPUDirect RDMA 的触发级别 | 网卡和 GPU 不在同一 NUMA/PCIe switch 时调 |
| `NCCL_ALGO` / `NCCL_PROTO` | 强制算法/协议 | 定位「某个算法在这个拓扑上慢」 |
| `NCCL_TOPO_FILE` | 自定义拓扑文件 | 容器里拓扑识别错误时 |
| `NCCL_BUFFSIZE` | 通信缓冲区大小 | 调整小消息延迟 vs 大消息带宽 |

> **`NCCL_SOCKET_IFNAME` 值得单独强调**：多网卡机器上
> 如果不对，NCCL 可能选一张**跨不了节点**的网卡，表现是
> 「单机 8 卡正常，一上多机就 hang」。这是多机训练最常见的第一个坑。

### 一个系统性的排查顺序

```
NCCL hang
  │
  ├─ 1. 是「所有 rank 都 hang」还是「部分」?
  │     └─ 部分 → 多半有一个 rank 提前挂了/抛异常了,其它在等它
  │        → 先去看那个 rank 的日志(不是看 NCCL,是看 Python 异常)
  │
  ├─ 2. 打开 NCCL_DEBUG=INFO,看日志停在哪一步
  │     ├─ 停在 "NET/IB" 或 "bootstrap" → 网络不通(检查 SOCKET_IFNAME / 防火墙)
  │     ├─ 停在 "Connected all rings" 之前 → 拓扑/建链问题(P2P / 共享内存)
  │     └─ 建链完成但 allreduce 不动 → 有 rank 没进集合操作(代码里的分支不一致!)
  │
  ├─ 3. 是不是「集合操作不匹配」?
  │     └─ 典型:rank0 调了 all_reduce 而 rank1 没调(比如在 rank0 上
  │        多做了一次 validation)—— NCCL 会一直等
  │
  └─ 4. 是不是超时设置太短?
        └─ init_process_group(timeout=...)
```

**第 3 条是最隐蔽的**：只要有一个 rank 的**集合操作调用序列**和其它 rank 不一致，
NCCL 就永远等下去。所以规则是：

> **「所有 rank 必须走同样的集合操作序列」**。
> 想只在 rank 0 上做点什么？用 `if rank == 0:` 包住**非集合操作**
> （写日志、存盘、`print`），**绝不能**包住 `all_reduce` / `broadcast` 这类调用。

### PyTorch 侧的对应变量

```bash
# PyTorch 2.4+ 的名字(TORCH_ 前缀);旧版是 NCCL_ASYNC_ERROR_HANDLING
export TORCH_NCCL_ASYNC_ERROR_HANDLING=1

# 让 NCCL 的 watchdog 在超时后真的抛异常,而不是永远挂住
export TORCH_NCCL_BLOCKING_WAIT=1     # (多数场景更推荐上面那个)
```

> ⚠️ **不要同时设这两个**，也不要在不了解后果时设 `BLOCKING_WAIT` ——
> 它会让「卡住」变成「卡住后报错」，代价是性能。
> 这两个变量的语义在 PyTorch 版本间调整过，**以你锁定的 torch 版本为准**。

---

## 30.5 显存：四种 OOM，四种完全不同的修法

「CUDA out of memory」是同一个错误信息，背后至少有**四种互不相干**的成因。
修错方向的代价是几小时。

| 类型 | 典型现象 | 根因 | 修法 |
|---|---|---|---|
| **① 模型装不下** | 一启动就 OOM，还没开始训练 | 参数+梯度+优化器状态 > 显存 | 降精度 / LoRA / ZeRO / FSDP / 换更大的卡 |
| **② 批次太大** | 训练跑到某一步 OOM，或 step 数不固定 | activation 显存 ∝ batch × seq_len | 降 batch / 梯度累积 / activation checkpointing |
| **③ 碎片化** | `reserved` 远大于 `allocated`，报「还有空闲显存却分配失败」 | 反复分配释放不同大小的块 | `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` |
| **④ 泄漏** | 显存随 step 单调上涨 | 累积了带梯度的张量、没释放的图、缓存 | 排查 `.item()`/`.detach()`、checkpoint 没释放 |

### 先学会读这行数字

```
torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB.
GPU 0 has a total capacity of 79.11 GiB of which 1.87 GiB is free.
Of the allocated memory 74.02 GiB is allocated by PyTorch,
and 1.21 GiB is reserved by PyTorch but unallocated.
```

三个数字的含义：

* **allocated** —— 真正被张量占用的（你的模型 + activation）；
* **reserved** —— PyTorch 向 CUDA 要来的（含缓存池，不还给系统）；
* **reserved - allocated** —— 缓存池里的空闲块。

**判据**：

* `reserved ≈ allocated` 且都接近总量 → **①②**（真的不够用）
* `reserved` 接近总量、`allocated` 小很多 → **③ 碎片化**
* `allocated` 随时间上涨 → **④ 泄漏**

```python
# 随时打印这三个数(训练循环里加上,定位时非常有用)
print(torch.cuda.memory_allocated() / 2**30,
      torch.cuda.memory_reserved() / 2**30,
      torch.cuda.max_memory_allocated() / 2**30)
```

### 类型 ① 的算术

训练时显存的大头**不是参数本身**。粗略估算（混合精度 + Adam）：

| 组成 | 每个参数的开销 |
|---|---|
| 参数（fp16） | 2 字节 |
| 梯度（fp16） | 2 字节 |
| 优化器状态（Adam 的 fp32 m/v） | 8 字节 |
| **fp32 主权重副本** | 4 字节 |
| **小计** | **≈ 16 字节/参数** |

所以 **7B 模型的全量微调大约需要 7e9 × 16 = 112 GB** —— 单张 80GB 卡装不下。
这就是为什么 LoRA / ZeRO / FSDP 是必需品而不是优化项。

```python
# 算一下再决定策略
params = sum(p.numel() for p in model.parameters())
print(f"全量微调约需 {params * 16 / 2**30:.1f} GB")
print(f"LoRA(r=16) 约需 {params * 2 / 2**30:.1f} GB 基座 + 少量适配器")
```

### 类型 ③ 的修法

```bash
# PyTorch 2.1+ 推荐值。它会用「可扩展段」代替固定的块划分,显著缓解碎片
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
```

> **两个名字都要知道**：`PYTORCH_CUDA_ALLOC_CONF` 是长期存在的那个；
> PyTorch 较新版本（2.6 前后）引入了 **`PYTORCH_ALLOC_CONF`** 作为更通用的名字
> （因为分配器后来也管 XPU 等设备）。**未确认**：两个名字在你所用版本上
> 的确切优先级与弃用状态 —— 请查你所用 PyTorch 的 `torch.cuda.memory` 文档，
> 或直接 `python -c "import torch; print(torch.__version__)"` 后按版本文档核对。
> **可以确定的**是：`expandable_segments:True` 这个**取值**是 PyTorch 2.1+
> 缓解碎片（类型 ③）的推荐值，`max_split_size_mb:128` 是另一条常见取值。

### 类型 ④ 的排查

三个最常见的原因：

```python
# ✗ 1. 在训练循环里累积了带梯度的张量
losses.append(loss)               # loss 带着计算图 → 显存一路涨
losses.append(loss.item())        # ✓ 只要数值

# ✗ 2. 在循环里反复创建而不释放
for step in range(N):
    big = torch.zeros(10**8, device="cuda")   # 每步都要新的

# ✗ 3. 验证/推理没关梯度
with torch.no_grad():             # ✓ 验证和推理一定要包
    ...
```

```python
# ⚠️ memory_snapshot() 返回的是「段(segment)的列表」,每个段是一个 dict,
#    键是 address / total_size / allocated_size / active_size /
#    segment_type / stream / blocks —— **没有 name 和 size 这两个键**。
#    所以不能写成 `for name, size in snap[0].items()`(那会得到一堆
#    "address"/"total_size" 之类的键名,完全没有你要的信息)。
snap = torch.cuda.memory_snapshot()
for seg in snap:
    print(f"{seg['segment_type']:>8}  "
          f"reserved={seg['total_size']/2**20:8.1f}MB  "
          f"allocated={seg['allocated_size']/2**20:8.1f}MB  "
          f"blocks={len(seg['blocks'])}")

# 更实用的是 memory_summary():按「大块/小块」分类打印,一眼看出是不是碎片
print(torch.cuda.memory_summary())
```

---

## 30.6 GPU 观测：看什么、怎么看

### 三层观测

| 层 | 工具 | 看什么 |
|---|---|---|
| **设备层** | `nvidia-smi` / `nvidia-smi dmon` / `nvidia-smi topo -m` | 利用率、显存、温度、**拓扑** |
| **进程层** | `nvidia-smi --query-compute-apps=pid,used_memory --format=csv` | 哪个 PID 占了多少（Ray 里用来对账） |
| **应用层** | `torch.cuda.memory_summary()` / Ray 指标 | 张量级别、按 worker 聚合 |

### 最常用的几条命令

```bash
# 实时看(1 秒刷新)
nvidia-smi -l 1

# 看拓扑:哪几张卡之间有 NVLink、跨 NUMA 的情况
nvidia-smi topo -m
#   输出里的 NV# = NVLink, PIX = 同一 PCIe switch, SYS = 跨 NUMA
#   ⚠️ Ray 的 topology_bundle 调度就是基于这个信息(第 8 章 §8.4)

# 看是谁在占卡(排查「显存没释放」)
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv

# dmon:紧凑的持续采样(比 -l 更适合长时间观察)
nvidia-smi dmon -s pucm -d 1
```

### Ray 侧的 GPU 指标

第 11 章 §11.5 讲过 `ray.util.metrics`。GPU 相关的还有 Ray 自己采集的
组件指标（`ray_component_*` 系列），以及节点 exporter 暴露的
`ray_node_gram_used` / `ray_node_gram_available` 这类。

> **`gram` 是 GPU RAM 的缩写**，不是拼写错误 —— Ray 的节点级 GPU 显存指标
> 就叫 `ray_node_gram_used` / `ray_node_gram_available`（配套还有
> `ray_node_gram_utilization`）。知道这一点能省很多"grep `gpu` 却什么都搜不到"的时间。
> ⚠️ 需要确认的是**完整清单与可用性依版本而变**，且 Dashboard 与 Prometheus
> 暴露的名字可能不同。定位时最稳的办法还是直接拉 `/metrics` 输出里
> grep `gram`（而不是 `gpu`）。

### 一个对账技巧

「Ray 说给了我 2 张卡，但它是不是真的在用？」——

```python
@ray.remote(num_gpus=2)
def check():
    import os, torch
    return {
        "visible": os.environ["CUDA_VISIBLE_DEVICES"],
        "count": torch.cuda.device_count(),
        "names": [torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())],
    }
```

把它和多机上的 `nvidia-smi` 输出对照，能立刻发现两类问题：
**Ray 记账与实际不符**（有人手动改了 `CUDA_VISIBLE_DEVICES`），
或者**拓扑不是你以为的那样**（NVLink 域和调度假设不一致）。

---

## 30.7 多机多卡：拓扑与放置

### 为什么拓扑重要

```
一台 8 卡机(NVLink 全连):
   0──1──2──3──4──5──6──7      卡间带宽 ~900 GB/s (H100 NVLink)

机间:
   节点A ──── IB ──── 节点B     带宽 ~400 Gb/s ≈ 50 GB/s
                                  ↑ 差 18 倍
```

**跨节点通信比节点内慢一到两个数量级。** 所以放置策略不是小事：

* **张量并行（TP）必须放在 NVLink 域内** —— 它每个 layer 都要 all-reduce，
  跨节点会直接崩掉吞吐；
* **数据并行（DP）可以跨节点** —— 每个 step 只 all-reduce 一次梯度。

### Ray 给的两个旋钮

```python
from ray.train import ScalingConfig

# ① 粗粒度:堆还是摊
ScalingConfig(
    num_workers=16,
    use_gpu=True,
    placement_strategy="PACK",     # 尽量堆在同一节点(默认 PACK)
    # placement_strategy="SPREAD", # 尽量摊开(跨节点通信多时用)
)

# ② 细粒度:自己做拓扑绑定(用 Ray Core 的放置组)
from ray.util.placement_group import placement_group
from ray.util.scheduling_strategies import PlacementGroupSchedulingStrategy
```

> ⚠️ **`ScalingConfig` 的字段名是 `placement_strategy`（不是
> `placement_group_strategy`）** —— 写错会直接报错，第 25 章 §E.4 记录过
> 这个笔误。另外 `ScalingConfig` **没有放置组 bundle 级标签
> （`bundle_label`）的入口**（第 13 章 §13.1 **已核实**），
> 要在 bundle 粒度做精细拓扑绑定得回到 Ray Core 的放置组。
> **但「节点标签级」的约束是有的**：`ScalingConfig(label_selector={...})`
> —— 它的 docstring 是 *"A list of label selectors for Ray Train worker
> placement"*，可以传单个 dict（应用到所有 worker），
> 也可以传与 `max_workers` 等长的 list（逐 worker 指定）。
> 大多数"想把 worker 放到某些机器上"的需求，用它就够了 ——
> 见第 13 章 §13.3 的字段表。

### 拓扑感知调度

Ray 的 `topology_bundle` 调度策略（GB200/GB300 那类「72 卡一个 NVLink 域」
的机器）就是为了把任务钉在同一个 GPU 域内。
**版本号本书内部未统一，已标注未确认**（第 8 章 §8.4、第 1 章 §1.4）。

**实践建议**：除非你在 GB200/GB300 这类 NVL72 机器上，
否则**不要依赖拓扑感知调度** —— 用 `placement_strategy="PACK"`
把 worker 堆在尽量少的节点上，就已经拿到了 90% 的收益。

---

## 30.8 GPU 事故清单

| 事故 | 症状 | 根因 | 修法 |
|---|---|---|---|
| **手动改 `CUDA_VISIBLE_DEVICES`** | 部分卡闲着、部分卡超配 | 代码里硬写了设备列表 | §30.1：永远不要自己设 |
| **`cuda:3` 不存在** | `invalid device ordinal` | 把物理卡号当逻辑编号用 | §30.1：worker 内永远用 `cuda:0` |
| **分数 GPU 互相 OOM** | 偶发 OOM，单独跑就没事 | 共享卡没限显存 | `set_per_process_memory_fraction` |
| **多机 hang 在第 1 次 all-reduce** | 无报错、不动 | `NCCL_SOCKET_IFNAME` 选错网卡 | §30.4 排查顺序 |
| **10 分钟后 NCCL timeout** | 大模型一启动就超时 | `init_process_group` 默认超时 10 分钟 | 显式传 `timeout=` |
| **集合操作不匹配** | 某些 step 之后 hang | 某个 rank 走了不同的分支 | §30.4：集合调用必须所有 rank 一致 |
| **还没开始训练就 OOM** | 加载模型时失败 | 全量微调的 16 字节/参数 | 换 LoRA / FSDP / ZeRO |
| **显存随 step 上涨** | 跑几小时后 OOM | 累积了带梯度的张量 | `.item()` / `torch.no_grad()` |
| **「明明有空间却分配失败」** | OOM 但 free 不为 0 | 碎片化 | `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` |
| **`num_workers` × GPU 数算错** | 用的卡数是预期的 N 倍 | 误以为 `use_gpu=True` 是「给全部卡」 | 它 = **每个 worker 1 张卡** |

---

## 30.9 与 mini-ray 的关系

**mini-ray 不实现 GPU 管理**，而且是明确不做（`mini-ray/README.md` 的取舍清单里
写着「GPU 显存管理、NCCL/RDMA」）。

但它**模拟了 Ray 的 GPU 资源语义**中与调度相关的那一半 ——
这一半是「编排层」的职责，也正是 Ray 该管的部分：

```python
import os
import miniray as ray

ray.init(num_cpus=4, num_gpus=2, logging_level="warning")

@ray.remote(num_gpus=1)
def report():
    # mini-ray 也注入 CUDA_VISIBLE_DEVICES,并且也提供 get_gpu_ids()
    return {
        "cuda_visible": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "gpu_ids": ray.get_gpu_ids(),
    }

try:
    print(ray.get([report.remote() for _ in range(2)]))
    # 两次调用会拿到**不同的** GPU id —— 这就是「GPU 默认独占」的语义
    # (对应的测试见 mini-ray/tests/test_util.py 的 GPU 部分)
finally:
    ray.shutdown()
```

对照一下**谁负责什么**：

| 能力 | 真实 Ray | mini-ray |
|---|---|---|
| `num_gpus` 记账与调度 | ✅ | ✅（含小数 GPU 的记账） |
| 注入 `CUDA_VISIBLE_DEVICES` | ✅ | ✅ |
| `ray.get_gpu_ids()` | ✅ | ✅ |
| `accelerator_type` / MIG | ✅ | ❌ **mini-ray 没有这个参数**（`grep -rn accelerator_type mini-ray/` 零命中；`miniray.init()` 收到未识别 kwargs 只会打一条 warning 然后**静默忽略**）。要近似只能用**通用自定义资源**：`miniray.init(resources={"A100": 2})`（见 `tests/test_util.py::test_custom_resources`） |
| 真正的 CUDA 计算 | 由 PyTorch 提供 | ❌（没有 GPU 依赖） |
| NCCL / 集合通信 | 由 PyTorch/NCCL 提供 | ❌ |
| 显存管理与 OOM 处理 | 由 PyTorch 提供 | ❌ |

> **这张表本身就是本章的结论**：
> **Ray 负责「谁在哪张卡上」，PyTorch/NCCL 负责「在卡上做什么」。**
> 所有 GPU 问题都应该先问一句：**这该由哪一层负责？**
> 拿显存 OOM 去问 Ray、拿调度不均去问 PyTorch，都是在错误的层找答案。

---

## 30.10 本章小结

* **Ray 不分配 GPU，它遮蔽 GPU。** `CUDA_VISIBLE_DEVICES` 在 worker 启动时注入，
  worker 内 `cuda:0` **永远**是自己分到的那张卡；`ray.get_gpu_ids()` 给的是物理号；
* **永远不要自己改 `CUDA_VISIBLE_DEVICES`** —— 那是资源超配的头号原因；
* **`num_gpus=0.5` 不是切分**，只是记账 + 让两个 worker 看见同一张卡。
  显存要自己 `set_per_process_memory_fraction`，且只适合推理；
* **Ray 不做集合通信**，它只送 `MASTER_ADDR`/`RANK`/`WORLD_SIZE`。
  用 `prepare_model` 或 HF `Trainer` 之一，**不要两个都用**；
* **NCCL hang 有系统性的排查顺序**：先分清「全 hang」还是「部分 hang」，
  再开 `NCCL_DEBUG=INFO` 看停在哪一步。多机第一个坑通常是 `NCCL_SOCKET_IFNAME`；
* **集合操作必须所有 rank 一致** —— 这是最隐蔽的一类 hang；
* **「CUDA OOM」有四种成因**：模型装不下 / 批次太大 / 碎片化 / 泄漏。
  先读 `allocated` 与 `reserved` 的关系，再决定改什么；
* **显存算术记一个数**：全量微调约 **16 字节/参数**，7B ≈ 112GB；
* **拓扑决定放置**：TP 必须在 NVLink 域内，DP 可以跨节点。
  非 NVL72 机器用 `placement_strategy="PACK"` 就够了。

**下一章**（第 31 章）会讲**怎么定位性能问题** ——
`py-spy`、`memray`、`nsys` 这几把刀各自切什么，
以及一条从「慢」到「哪一行慢」的完整路径。

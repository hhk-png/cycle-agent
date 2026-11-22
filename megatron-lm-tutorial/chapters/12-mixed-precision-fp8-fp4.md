# 第 12 章 混合精度、FP8 与 FP4

## 12.1 为什么不用纯 fp32

fp32 的数值稳定，但：

- 权重/激活/通信的字节数是 bf16 的 2 倍；
- Tensor Core 的 fp32 吞吐远低于 bf16/FP8；
- 大规模训练的显存和带宽都不允许。

混合精度训练的基本做法：

```text
前向/反向 GEMM：bf16 或 fp16
主权重：fp32
优化器状态：fp32
LayerNorm/softmax/CE：fp32 或更高精度
```

bf16 与 fp16 的差别：

- bf16 指数范围与 fp32 相同，不易溢出，但尾数少（约 8 位）；
- fp16 尾数多（约 11 位），但范围小，需要 loss scaling；
- 大模型训练通常优先 bf16；推理/旧硬件可能用 fp16。

mini 通过 `torch.autocast` 支持 bf16/fp16 推理式计算，同时保留 fp32 参数和
优化器状态；`use_autocast=True` 时只有前向算子进入 autocast，反向由 PyTorch
处理 dtype。

## 12.2 FP8 的两种格式

| 格式 | 指数位 | 尾数位 | 用途 |
|---|---:|---:|---|
| E4M3 | 4 | 3 | 前向权重/激活 |
| E5M2 | 5 | 2 | 梯度/需要更大范围 |

FP8 只有 8 位，必须配合 **scaling**：

```text
x_fp8 = clamp(x / scale, -max, max).to(fp8)
x_dequant = x_fp8 * scale
```

数量级（以 E4M3 为例）：最大可表示约 448，最小正规数约 `2^-6`，最小
subnormal 约 `2^-9`；E5M2 最大约 57344，但尾数只有 2 位。因此：

- 激活/权重常用 E4M3（精度优先）；
- 梯度常用 E5M2（范围优先）；
- scale 的目标是把 `amax` 映射到接近 448（E4M3）或 57344（E5M2），同时
  避免把大量值压到 subnormal/0；
- 一个张量里如果有极端离群值，per-tensor scale 会让其余值只剩很少有效位，
  这就是 per-block/MX 的动机。

### 逐张量 vs 逐块

- per-tensor：一个张量一个 scale，简单，但离群值会拉低精度；
- per-channel/per-block：每个通道/块一个 scale，精度更好，硬件支持更复杂；
- delayed scaling：用历史最大值估计 scale，避免每次同步；
- current scaling：每步动态计算 scale，精度好但有同步开销。

Transformer Engine 把这些封装成 fused kernel；Megatron 通过 TE 的
`fp8_autocast`、`recipe`、`fp8_format` 等开关使用。FP8 不是“把 dtype 改成
float8”这么简单：GEMM 的输入、累加器、输出、amax 历史、scale 更新都要匹配。

### 12.2.1 一个可运行的 FP8 误差实验

mini 的 `simulate_fp8` 可以做 quantize-dequantize：

```python
from mini_megatron.parallel.layers import simulate_fp8

x = torch.randn(4096)
y = simulate_fp8(x)                 # per-tensor
z = simulate_fp8(x.view(32, 128), dim=1)  # per-row
print((x - y).abs().mean(), (x - z).abs().mean())
```

你会观察到：

- 离群值会拉大 per-tensor 的 amax，让其他值量化到更少的有效位；
- per-row/per-block 能缓解离群值；
- 同样的相对误差，在 attention logits/softmax 上可能被放大；
- 量化误差不是“均匀噪声”，而是与数据分布强相关。

生产 FP8 kernel 还涉及：

- GEMM 的输入/权重/输出分别用不同 scale；
- 累加器保持 fp32；
- amax 历史/延迟更新；
- 反向梯度的动态范围；
- 不同并行度下 amax 的 reduction 顺序。

### 12.2.2 FP8 recipe 的检查清单

1. 哪些 GEMM 用 FP8？attention/MLP/专家/输出层分别试；
2. 权重/激活用 E4M3 还是 E5M2？
3. per-tensor / per-channel / per-block / MX？
4. delayed 还是 current scaling？
5. LayerNorm/softmax/CE 是否保持 bf16/fp32？
6. 反向是否用 FP8？梯度 scale 怎么定？
7. checkpoint 是否保存 scale/amax？
8. 与 TP/PP/CP/EP 的交互是否验证？

### 12.2.3 FP8 recipe 的演进

“FP8”不是一种 recipe，而是一族：

| recipe | 缩放粒度 | scale 类型 | 特点 | 典型硬件 |
|---|---|---|---|---|
| TensorWise (delayed) | 每张量 | fp32 | 实现简单，依赖 amax 历史 | Hopper/Ada；Ampere 及更早不支持 FP8 |
| TensorWise (current) | 每张量 | fp32 | 每步动态，精度好，需同步 | Hopper |
| BlockWise | 每 128/块 | fp32 | 缓解离群值，kernel 更复杂 | Hopper/Blackwell |
| MXFP8 | 每 32 元素 | E8M0 | 微缩放，Blackwell 友好 | Blackwell |
| NVFP4 | 每 16 元素 + 每张量二级 scale | E4M3 块 scale + FP32 二级 scale | 4 位训练，需专门 kernel | Blackwell |

几个容易混淆的点：

- **delayed scaling** 用过去 N 步的 amax 估计当前 scale，避免每步 all-reduce；
  代价是遇到突变（loss spike/新数据）时可能溢出或精度下降。
- **current scaling** 每步计算 amax，精度更稳，但引入同步/额外 kernel；在
  TP 下还要决定 amax 是本地还是跨 TP 归约。
- **blockwise/MX** 把缩放粒度降到块级，离群值只影响局部；但 scale 本身的
  存储/计算开销上升。
- **NVFP4** 通常用于部分 GEMM（如专家/线性层），关键路径（LayerNorm、softmax、
  CE、router）仍保留高精度；它不是“全局 dtype=FP4”。

amax 历史是训练状态的一部分：checkpoint 要保存，恢复后不能从零开始，否则
前几步的 scale 会突变。不同 TP/PP 布局下 amax 的归约顺序也会改变数值，切换
并行度后要做短程对照。

## 12.3 FP8 训练的数值风险

- 离群值导致 amax 过大，其他值被量化到 0；
- 反向梯度的动态范围与激活不同，E4M3 可能不够；
- LayerNorm/softmax/CE 对精度敏感，通常保持 bf16/fp32；
- 路由器 logits、MoE 专家输出对量化更敏感；
- 不同并行度的 reduction 顺序会改变 amax。

实践：

- 先用 bf16 跑通收敛，再开 FP8；
- 监控 loss、grad norm、amax/scale；
- 对敏感层使用 higher precision；
- 使用官方 recipe（DelayedScaling/MXFP8/NVFP4）而不是自己拍；
- 小规模对比 FP8 vs bf16 的 loss 曲线，确认没有系统性偏移。

## 12.4 FP4 与 Blackwell

2026 年的前沿是 **NVFP4**（NVIDIA 的 4 位浮点格式）和 **MXFP8/MXFP4**
（微缩放格式）。核心思想：

- 更低的位宽 → 更高吞吐/更低显存；
- 块级缩放（block scaling）保留动态范围；
- 需要硬件（Blackwell）和 kernel（TE/cuBLAS）支持；
- 训练时常用 FP4 做部分 GEMM，关键路径仍保留 BF16/FP32。

需要区分“模型原来怎么训”和“框架提供了什么 recipe”：

- **DeepSeek-V3 原论文是在 H800 上用 FP8（E4M3）训练的，不是 NVFP4**。
  把它说成“NVFP4 训练”是常见误传；Megatron/NeMo 生态提供的是在 Blackwell
  上复现/移植 DeepSeek 结构的 **NVFP4 recipe**，两者不是同一件事。
- 当前框架侧的进展包括 selective FP32 execution（含 FP32 logits）、
  Blackwell 上的 2D MXFP8 量化、NVFP4 参数 gather / grouped-expert kernel
  融合（后者多为 roadmap/实验性）。
- 引用这类能力时，要同时给出：模型、硬件、精度 recipe、并行配置、代码
  版本。只写“支持 FP4”没有可比性。

这些不是“打开一个 flag 就完事”：模型结构、并行布局、checkpoint、数值 recipe
都要适配。厂商的 TFLOPs 数字通常在 FP4/稀疏/特定 shape 下测得，和 bf16 dense
不可直接比较。

### 12.4.1 FP4 训练的验收标准

不要用“能跑 + 看起来更快”作为验收。至少做四组对照：

| 对照 | 目的 | 通过标准 |
|---|---|---|
| FP4 vs BF16 短程 loss | 是否有系统性偏移 | 100–500 步 loss 曲线在噪声内 |
| 梯度范数/更新量 | 是否量化梯度导致更新异常 | 同量级，无持续放大/归零 |
| 下游评测 | 能力是否退化 | 目标 benchmark 在容差内 |
| 长程稳定性 | 是否延迟爆发 | 数千步无 NaN/spike/路由塌缩 |

还要监控：

- 每层/每 GEMM 的 amax/scale 分布；
- 是否有层长期 scale=0/饱和；
- 专家负载是否因量化而倾斜；
- checkpoint 是否保存 scale/amax；
- 切换并行度后是否复现。

选择性 FP32（selective FP32）是常见做法：对 logits、LayerNorm、softmax、
router 等敏感路径保留 fp32/bf16，只对大规模 GEMM 用低精度。它的收益小于
“全 FP4”，但稳定性好得多；生产通常从选择性低精度开始，而不是一步到位。

### 12.4.2 QAT 与剪枝：什么时候值得

**QAT（Quantization-Aware Training）** 在前向里插入 fake-quant（量化-反量化），
反向用 straight-through estimator 传梯度，让模型在训练中适应量化误差：

```text
w_q = dequant(quant(w))        # forward 用 w_q
grad_w = grad_wq               # backward 近似直通
loss = CE(model(w_q), y)
```

什么时候值得：

- PTQ 在目标精度（INT4/FP4/2-bit 权重）下掉点超过可接受范围；
- 有继续训练/蒸馏预算（通常是原训练 steps 的 1%–10%，视精度而定）；
- 部署 kernel 的量化格式固定，必须让训练模拟同一套 rounding/scale。

代价：QAT 的 fake-quant 会改变数值路径，需要重新做 loss spike/长上下文/
MoE 路由的稳定性验证；它也不是“免费掉点恢复”，低比特下容量损失可能无法
完全补回。常见顺序是 **PTQ → 敏感层混合精度 → 小规模 QAT → 蒸馏恢复**。

**剪枝/稀疏** 分两类：

| 类型 | 做法 | 硬件收益 | 风险 |
|---|---|---|---|
| 非结构化 | 按权重绝对值置零 | 通用 GPU 上通常不加速 | 稀疏率虚高、精度恢复难 |
| 结构化 | 按 head/channel/层裁剪 | 直接减小 GEMM | 需要重训/蒸馏，结构不可逆 |
| 半结构化（2:4） | 每 4 个权重保留 2 个 | 支持稀疏 tensor core 的硬件 | kernel/布局限制，非所有层适用 |

对 Megatron 训练栈来说，剪枝通常发生在**后训练/部署前**，而不是预训练主
路径；如果要在训练中做，必须同步修改 checkpoint 的 shape、并行切分和
optimizer 状态，并重新验证 TP/PP 的重分片。不要只看“参数量减少”，要看
实际 kernel 是否真的变快、以及精度/长尾能力是否退化。

## 12.5 Loss scaling 与溢出

fp16 需要 loss scaling：

```text
loss_scaled = loss * S
backward → grads * S
unscale → grads
如果溢出：S /= 2；如果连续 N 步正常：S *= 2
```

bf16 通常不需要，但仍有溢出风险（尤其是 attention logits、MoE 路由）。
FP8 需要 amax/scale 管理，TE 的 `DelayedScaling` 会维护历史 amax。

排查 NaN/Inf：

1. 关闭 FP8/bf16，用 fp32 复现；
2. 检查 attention mask 是否产生 `-inf` 整行；
3. 检查 CE 的 `ignore_index` 是否导致 0/0；
4. 检查梯度裁剪前的 norm 是否爆炸；
5. 检查 MoE 路由是否出现全 -inf/NaN；
6. 检查数据是否有越界 token id。

## 12.6 mini 的 FP8 模拟

`mini_megatron/parallel/layers.py`：

```python
def simulate_fp8(x, dim=None):
    if dim is None:
        amax = x.detach().abs().max()
    else:
        # per-row/per-channel：沿 dim 保留一维，广播回原形状
        amax = x.detach().abs().amax(dim=dim, keepdim=True)
    scale = 448.0 / amax.clamp_min(1e-12)
    q = ((x * scale).clamp(-448, 448)
         .to(torch.float8_e4m3fn).to(x.dtype)) / scale
    # forward 用 q，backward 直接透传（straight-through estimator）
    return x + (q - x).detach()
```

`dim=None` 是 per-tensor，`dim=1` 是 per-row；两者在离群值场景下的误差
差异正是 12.2.1 要观察的现象。生产 kernel 的 scale 还会分块、维护 amax
历史，并与 GEMM 融合，不能把这个模拟当成性能实现。

它做的是 **quantize-dequantize**，不会加速 GEMM，只用于观察：

- scaling 后的相对误差；
- 离群值对精度的影响；
- FP8 对 loss 的偏移。

**必须用 STE，不能直接把 `q` 返回**：否则 backward 也会经过 E4M3 量化，
权重梯度（量级约 `1e-5`）在 `/scale` 后下溢为 0，整个模型“loss 能算、
梯度全零”。第七轮审计抓到的就是这个假绿：只断言 `finite` 的测试对 0 也
通过。现在的单测同时断言梯度非零、且 `simulate_fp8(x).sum()` 对 `x` 的
梯度为 1。

`ModelConfig.fp8_simulation=True` 会把 Column/Row 的输入/权重过一遍这个函数。
它不能证明生产 FP8 kernel 的正确性，后者必须用 TE 的参考实现和真实 GPU
验证；STE 也只是教学折中，不代表硬件 FP8 的梯度语义。

## 12.7 精度与并行度的交互

- TP：每个 rank 的局部 GEMM 输入分布不同，per-tensor amax 可能不一致；
- PP：stage 之间传输的激活 dtype 要匹配；
- CP：all-gather KV 的数值顺序影响 amax；
- MoE：不同专家的激活分布差异大，per-expert scaling 更合适；
- DP：梯度 all-reduce 的顺序影响数值，但通常远小于量化误差。

生产 Megatron 会为不同层/不同 GEMM 配置不同的 recipe；不要在 mini 里期待一个
全局开关复刻这些细节。

## 12.8 本章小结

- bf16 是大模型训练默认；fp16 需要 loss scaling；fp32 保留给主权重/敏感算子。
- FP8 需要 scaling/amax recipe，不是简单 dtype 转换。
- FP4/NVFP4 是 Blackwell 时代的趋势，但依赖硬件/kernel/模型适配。
- 精度问题先用 fp32 复现，再逐层排除。
- 厂商 TFLOPs 要看清精度、稀疏性和 shape。

下一章讨论优化器与训练稳定性：AdamW、Muon、梯度裁剪、loss spike。

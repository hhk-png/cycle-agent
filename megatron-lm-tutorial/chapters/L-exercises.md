# 附录 L 练习、项目与面试题

> 本附录的练习都基于仓库里的 mini-megatron，目标是“改代码 → 跑测试 → 解释
> 数值”。答案给的是思路和关键公式，不是唯一实现。

## L.0 怎么用这份练习

| 组 | 适合 | 难度 | 建议时间 | 对应章节 |
|---|---|---|---|---|
| L.1 基础题 | 所有读者 | ★–★★ | 每题 10–20 分钟 | 04–14 |
| L.2 代码题 | 工程师 | ★★–★★★ | 每题 1–3 小时 | 15–28 |
| L.2.1 新增模块 | 工程师/研究 | ★★★ | 每题 2–6 小时 | 09/10/22/40/50 |
| L.3 系统设计 | 平台/架构 | ★★★ | 每题 45–90 分钟 | 29–42 |
| L.4 面试快问 | 求职/自测 | ★–★★ | 每题 3–8 分钟 | 全书 |
| L.5 项目 | 团队/课程 | ★★★–★★★★ | 1–2 周 | 全书 |

建议顺序：先做 L.1 检查概念，再挑 2–3 道 L.2 动手，最后用 L.5 做一次
端到端项目。**每题都要有可验证的产出**（测试、误差记录、配置报告），
不要只写“我觉得”。

## L.1 基础题（原理）

**L1. 为什么 Column Parallel 的反向需要 all-reduce `grad_input`，而 Row
Parallel 的前向需要 all-reduce 输出？**

答案：Column 把输出维切开，每个 rank 只看到自己那部分输出，但输入 `X` 是
共享的；`dX = Σ_r dY_r W_r^T`，所以要把各 rank 的贡献加起来。Row 把输入维
切开，每个 rank 只算部分和 `Y_r = X_r W_r`，输出是各 rank 之和，所以前向要
all-reduce。两者是“切输出/切输入”的对偶。

**L2. SwiGLU 的 gate/up 为什么必须按位置对齐？**

答案：`h = silu(gate) * up` 是逐元素乘。如果 rank 0 拿到全部 gate、rank 1
拿到全部 up，`chunk(2)` 后每个 rank 的 gate/up 不是同一组 hidden 维，乘出来
的语义就错了。正确做法是两个 Column Parallel 用相同的输出切分，或融合后
按交错布局切。

**L3. 70B 模型 bf16 权重 + Adam 状态需要多少显存？ZeRO-1/TP=8 后呢？**

答案：不切分 `16 B/param × 70e9 = 1.12 TB`。ZeRO-1/DP=8：权重+梯度仍复制
`4×70=280 GB`，优化器状态 `12×70/8=105 GB`，合计 385 GB/卡。再 TP=8：
参数/梯度按 TP 切，`4×70/8=35 GB`，优化器状态 `105/8≈13 GB`，合计约
48 GB/卡（未计激活/PP）。实际还要 PP/CP/激活。

**L4. 为什么 TP 通常不超过一个 NVLink 域？**

答案：TP 每层都有 all-reduce，频率高、延迟敏感；跨节点会走 IB，带宽/延迟
差一个数量级，通信无法被计算掩盖。经验上 TP ≤ 8（或 NVL72 域内可更大），
跨节点用 PP/DP/EP。

**L5. 1F1B 的气泡公式是什么？VPP 为什么能降低气泡？**

答案：非 interleaved `(p-1)/(m+p-1)`；VPP `(p-1)/(m·v+p-1)`。VPP 让每个
rank 持有多个 chunk，micro-batch 在 chunk 间交错，warmup/cooldown 被更多
工作填充。代价是激活/调度复杂度上升，层数要能被 `p·v` 整除。

**L6. GPipe 和 1F1B 的最终 loss 应该一致吗？为什么不能逐步比较？**

答案：相同权重/数据/梯度累积下，最终参数的更新应一致（浮点容差内）。但两种
schedule 的前向/反向顺序不同，中间激活的生命周期和浮点归约顺序不同，逐步
loss 不必相同；测试只应比较最终 step 的 loss/grad_norm。

## L.2 代码题（mini-megatron）

**L7. 给 `CoreAttention` 加滑动窗口 mask（只允许最近 `w` 个 token）。**

提示：在 causal mask 上再加 `kv_positions >= q_positions - w + 1`。要求：

- `w >= s` 时与全 causal 数值一致；
- `w=1` 时每个 token 只看自己；
- 加单测对比暴力 mask 实现。

**L8. 给 `MLAttention` 加 TP 支持。**

提示：Q heads 按 TP 切；`kv_a_proj` 复制并注册梯度 all-reduce hook；
`kv_b_proj` 按 head 切；`o_proj` Row Parallel；共享 `W_KR` 复制。测试：
TP=2 与 TP=1 的 loss/梯度逐元素对比。注意 `k_pe` 只有 1 个 head，不能按
TP 切。

**L9. 给 `InterleavedGPipeExecutor` 加 interleaved 1F1B。**

提示：把 `forward_backward` 的“全前向再全反向”改成 warmup/steady/cooldown；
依赖是 `F(mb,c)` 需要 `F(mb,c-1)`，`B(mb,c)` 需要 `F(mb,c)` 和
`B(mb,c+1)`；本地/远程 buffer 仍按 `(mb,chunk)` 键。先用 `validate_trace`
验证顺序，再跑 `parallel_vpp.py` 对比 PP=1。

**L10. 给 `ZeRO1Optimizer` 加 reduce-scatter/all-gather（ZeRO-2 风格）。**

提示：反向时对梯度做 reduce-scatter，每个 rank 只保留自己 owner 的梯度；
step 后 all-gather 参数。注意 MoE 专家参数跳过 DP 归约、除以 EP 的语义不能
破坏。测试：与 AdamW 单步参数逐元素对比。

**L11. 给 `checkpoint.py` 加原子写入。**

提示：先写 `*.tmp` + `fsync`，再 `os.replace`；metadata/latest 最后写；
加载时校验文件存在/大小/格式。测试：模拟写入中断，确认不会读到半文件。

**L12. 给 `analysis.py` 加 MoE/MLA 的显存与 FLOPs。**

提示：MoE 用激活参数算 FLOPs、总参数算显存；MLA 的 KV 是
`d_c+d_R`，不是 `2·n_h·d`；attention FLOPs 仍按 `4·L·n_h·d·s`（注意
因果口径）。加测试：与手算/已知配置对比。

**L13. 用 `parallel_equiv.py` 的框架，给 CP 加 GQA 的 K/V 梯度断言。**

提示：GQA 下 K/V 的 head 数少于 Q；CP gather 后每个 rank 的 K/V 梯度仍要
跨 CP 求和。测试要比较 `k_proj/v_proj` 的逐元素梯度，而不只是范数。

**L14. 复现一个“loss 对但梯度错”的 bug。**

提示：故意删掉 `_ColumnParallelLinearFn.backward` 里的 all-reduce，跑
`parallel_equiv.py --case tp`，观察 `loss_diff=0` 但 `grad_diff` 变大。
然后恢复，记录误差。这个练习的目的是让你亲手看到“前向等价不能证明并行
正确”。

## L.2.1 新增模块练习（第五版）

**L15. 把 ring attention 接到真实 CP 通信。**

`parallel/ring_attention.py` 目前在一个进程里模拟所有 rank。改成真实
`cp` 个进程：

- 每个 rank 只持有本地 Q/K/V 块；
- 每个 step 用 `isend/recv` 把 KV 块传给下一个 rank；
- 保持 online softmax 的 `m/l/acc` 本地；
- 用 `tests/parallel_equiv.py --case cp` 的参考对比 loss 与 K/V 梯度。

验收：cp=2/4 的 logits 与单卡 CP=1 误差 < 1e-5；P2P 不挂起；能打印每个
rank 的块数。

**L16. 给 paged cache 加 prefix sharing。**

- 两个序列共享前 `n` 个 token 的物理 block；
- 用引用计数，最后一个序列释放时才回收；
- 写时复制（COW）：一个序列 append 到共享 block 时，复制该 block 再写。

验收：共享前缀的 gather 与各自独立 cache 一致；释放顺序不影响其他序列；
泄漏检测（free list + used 数守恒）。

**L17. 把 aux-free 接进 EP 训练。**

- 在 `ExpertParallelMoELayer` 中维护全局 expert counts（跨 EP all-reduce）；
- 用 `AuxFreeBalancer` 的 bias 做选择，不改变门控权重；
- 记录每 `N` 步的负载直方图与 bias；
- 对比“aux loss”与“aux-free”在相同数据上的专家负载/loss。

验收：训练 200 步后最大/最小专家负载比 < 1.5；bias 不发散；数值等价性
测试仍通过（aux-free 不改变前向数学，只改路由选择）。

**L18. 给 flat optimizer 加多 bucket + 梯度分片。**

- 按参数大小切成多个 bucket；
- 反向时每个 bucket 就绪即 reduce-scatter（可用后台线程模拟）；
- reduce-scatter 后释放完整梯度（ZeRO-2 语义）；
- checkpoint 记录每个 bucket 的 offset/owner。

验收：与单 bucket 版本参数逐元素一致；峰值显存下降；恢复后 100 步 loss
连续。

**L19. 给 MLA 加吸收推理。**

- 实现 `absorb_weights()`，把 `W_UK`/`W_UV` 吸收进 query/output；
- decode 时只缓存 `c_KV` 与共享 `k_R`；
- 对比吸收前后的 logits 与 KV 字节数。

验收：prefill+decode 与未吸收版本误差 < 1e-5；KV 字节数按 `d_c+d_R` 计算；
长序列 decode 的显存下降。

**L19b. 把 Ulysses 参考接到真实 all-to-all。**

`parallel/ulysses.py` 目前用切片/拼接在单进程模拟布局变换。改成真实
`cp` 个进程：

- 初始布局 `[B,H,S/P,D]`，用 `all_to_all_single` 把 head 切片发到对应 rank；
- 每个 rank 对 `[B,H/P,S,D]` 做全序列因果 attention；
- 反向再做一次 all-to-all，恢复 `[B,H,S/P,D]`，接输出投影/MLP；
- 处理 GQA：KV head 数不能被 `P` 整除时，明确选择复制还是拒绝；
- 用 `tests/unit/test_ulysses.py` 的 dense 参考对比 logits 与梯度。

验收：cp=2/4 的 logits 误差 < 1e-5；两次布局变换往返精确；打印每 rank 的
发送元素数，与 `4·B·S·H·D·(P-1)/P²` 对得上；P 不整除 `S`/`H` 时显式报错。

## L.3 系统设计题

**L20. 设计一个 70B 模型在 512×H100 上的训练配置。**

要点：

- 显存：TP=8（NVLink 内）、PP=4、DP=16（512/(8·4)）；ZeRO-1 可选；
- 每卡参数/状态：`16×70/(8·4)=35 GB`（不 ZeRO）或更低；
- 激活：序列 4K、micro-batch 1、activation checkpointing；
- 通信：TP 在节点内，PP 跨 4 节点，DP 跨剩余；
- 气泡：m 足够大（≥ pp），或 VPP；
- 验证：先 8 卡小模型对齐，再放大；检查 MFU/通信占比/故障恢复。

**L21. 长上下文 128K 训练，选 CP 还是 TP/PP？**

要点：先看 attention 的 `s²` 和 KV 显存；CP 切序列，通信随 s 增长；TP 切
head/宽度，不解决 `s²`；PP 切层，不解决单层激活。通常 CP + FlashAttention
+ activation checkpointing + 可能的 ring attention；CP 的 mask/归一化/KV
梯度是正确性重点。推理侧还要考虑 KV cache/PD 分离。

**L22. MoE 模型 256 专家，EP 怎么放？**

要点：EP 组跨节点时 all-to-all 是瓶颈；优先把 EP 放在 NVLink/NVL72 域内；
专家放置要避免热点；capacity/dropless、grouped GEMM、EPLB；TP/PP 与 EP
正交；推理侧要考虑 ETP、专家复制、batch invariance。

**L23. 如何把训练 MFU 从 35% 提到 45%？**

要点：先 profiling（GEMM/通信/数据/气泡），再逐项：

- kernel：FlashAttention/TE/FP8；
- 并行：TP 不跨节点、PP 气泡/VPP、DP 重叠；
- 数据：预取/packing/避免 host-bound；
- 重算：选择性重算，减少无用重算；
- 稳定性：减少失败/重试（端到端 MFU）。
不要先换硬件；先确认瓶颈在哪。

**L24. 设计一个支持 TP/PP/EP 的 checkpoint 重分片方案。**

要点：全局 metadata + 参数名 → 全局张量；按语义拼接 TP/PP/EP 分片；
优化器状态按参数名映射（不是按 flat buffer 索引）；原子/异步写；恢复后
跑 10–100 步对比 loss；处理 tied embedding/专家重排/不同 order。

**L25. 设计一个 PD 分离的推理集群。**

要点：prefill 计算密集、decode 带宽密集；KV 传输量
`2·L·n_kv·d·bytes·tokens`；调度目标 SLO goodput；KV 池化/前缀缓存；
故障恢复/弹性；用 TTFT/TPOT p50/p99 评估，而不是总 tokens/s。

## L.4 面试题（快问快答）

1. **TP 和 DP 的通信量谁更大？** 取决于模型/并行度：TP 每层 all-reduce
   `O(bsh)`，DP 每步 `O(N)`；TP 频率高、延迟敏感，DP 与参数量成正比。
2. **为什么 ZeRO-1 不切参数？** 它只切优化器状态，参数/梯度仍复制；实现
   简单，通信是 all-reduce + broadcast；ZeRO-3/FSDP 才切参数。
3. **Sequence Parallel 省的是什么？** 省 LayerNorm/Dropout/残差等复制激活，
   不是省 TP 的 GEMM 通信；它把激活从 `bsh` 降到 `bsh/t`。
4. **CP 的 K/V 梯度为什么要 all-reduce？** 每个 CP rank 的 query 都用完整
   K/V，K/V 的梯度是所有 query rank 贡献之和；gather 的反向要跨 CP 求和。
5. **MoE 的 aux loss 为什么不能完全解决负载不均？** 它是可导的代理目标，
   只惩罚概率/分配的统计量，不直接约束离散路由；容量/溢出/热点仍会发生。
6. **FP8 的 delayed scaling 和 current scaling 区别？** delayed 用历史 amax，
   省同步但怕突变；current 每步算 amax，精度稳但有同步/开销。
7. **为什么 MLA 的 cache 更小？** 缓存低秩 latent `c_kv` + 共享 RoPE key，
   不是完整 K/V；推理时再解压/吸收。
8. **PP 的 bubble 怎么算？** 非 interleaved `(p-1)/(m+p-1)`；VPP 用
   `m·v` 替代 `m`。
9. **为什么 pipeline 的 loss 要除以 micro-batch 数？** 梯度累积：每个
   micro-batch 的 loss 是全局 batch 的一部分，不缩放会让梯度大 m 倍。
10. **checkpoint 的 metadata 为什么要记录并行度？** 不同 TP/PP/EP 布局的
    shard 形状/参数名不同；不校验就加载会静默错位。

## L.5 项目作业（建议 1–2 周）

**项目 A：给 mini 加 ring attention。**
目标：CP 不再 all-gather 完整 K/V；实现分块 online softmax + ring 通信；
与 all-gather 版本对比 loss/梯度；测量显存/通信。难点：因果 mask 的
load balance、online softmax 的数值稳定、反向。

**项目 B：实现 interleaved 1F1B + zero-bubble 的简化版。**
目标：在 `interleaved_exec.py` 上实现 1F1B 调度；比较激活峰值/气泡；
加 `validate_trace` 和 PP=1 等价性测试。难点：warmup/cooldown、本地/远程
buffer、B_input/B_weight 拆分。

**项目 C：分布式检查点重分片。**
目标：支持 TP/PP 变化 + 非 ZeRO AdamW 状态；用 `torch.distributed.checkpoint`
或自定义格式；恢复后跑 50 步对比 loss。难点：参数语义映射、tied embedding、
专家重排、原子性。

**项目 D：MLA 的 TP 实现。**
目标：把 `parallel/mla.py` 接入 TP 组；实现 q/kv 的切分与梯度归约；
与单卡参考对比；加吸收推理的数值校验。难点：共享 `k_R` 的复制、latent
投影的梯度、吸收矩阵的 head 块。

**项目 E：给推理加连续批处理。**
目标：实现请求队列、KV cache 分页/回收、chunked prefill；比较吞吐/延迟；
保证 batch invariance。难点：KV 管理、调度公平性、prefix cache。

## L.6 如何自评

- 能不看书画出 TP/PP/CP/EP 的通信图；
- 能解释每个 all-reduce 为什么存在、漏了会怎样；
- 能用一个最小测试复现“loss 对但梯度错”；
- 能根据模型/集群/序列算出显存/FLOPs/通信量级；
- 能读懂生产 Megatron 的一个开关 → 文件 → 函数链路；
- 能对一份“MFU 50%”的材料指出它缺了哪些口径。

做到以上六条，基本具备“判断 + 实现 + 排障”的能力；剩下的靠真实集群和
故障复盘积累。

---

上一附录：[附录 K 模型转换映射速查](K-conversion-mappings.md)  
返回目录：[SUMMARY](../SUMMARY.md)  
下一附录：[附录 M 源码阅读实验手册](M-source-reading-labs.md)

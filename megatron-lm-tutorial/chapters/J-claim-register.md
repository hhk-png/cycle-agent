# 附录 J 版本、路线图与声明登记表

> 技术书最容易过时的不是公式，而是“某版本支持某功能”这类声明。本附录给出
> 一套可执行的**证据分级**和登记模板，用来约束正文中的版本/roadmap 表述。

## J.1 为什么要给声明分级

同一个“支持”可能意味着完全不同的东西：

- 代码里有一个实验开关；
- 有一个能跑的 recipe；
- 有数值对齐的转换；
- 有大规模性能验证；
- 有企业支持和 SLA。

把它们都写成“支持”，会让读者做出错误的技术/采购决策。下面用 **E1–E4**
（Evidence）分级描述“能力证据”，用第 36.1 节的 **L1–L4** 描述“发布状态”。
两套标签不能混用：

| 标签 | 回答的问题 | 定义位置 |
|---|---|---|
| E1–E4 | 这个能力被验证到什么程度？ | 本附录 J.2 |
| L1–L4 | 这个东西是已发布、已合并、路线图还是传闻？ | 第 36.1 节 |

一个功能可以同时是“L1 已发布”和“E2 可运行但无数值对齐”，也可以是
“L3 路线图”和“E1 只有设计文档”。**发布状态 ≠ 证据强度**。

## J.2 证据分级（E1–E4）

| 级别 | 定义 | 证据 | 读者应如何使用 |
|---|---|---|---|
| **E1 代码存在** | 仓库里有类/函数/开关 | 文件路径、commit/tag | 可以读代码/做实验，不保证可用 |
| **E2 可运行** | 有配置/脚本，能在小规模跑通 | 运行命令、日志、CI | 可以做原型，需自己验证 |
| **E3 数值对齐** | 与参考实现/原模型 logits/loss 对齐 | 测试/脚本、误差范围 | 可以用于迁移/复现 |
| **E4 生产验证** | 大规模训练/推理、性能/稳定性有数据 | 技术报告/benchmark、版本 | 可以作为生产选型依据 |

写作规范：

- 说“支持”时，至少标明 E 级别；
- 说“已发布”时，给 release tag/日期，并标 L 级别；
- 说“roadmap”时，写清是计划/实验/已合并未发布；
- 不给来源的数字，标“示例/估算”，不要写成事实。

## J.3 登记模板

每个声明一行，建议放在团队 wiki/issue 里，随版本更新：

```text
id:           CLAIM-0001
claim:        <一句话>
evidence:     E1 / E2 / E3 / E4
release:      L1 / L2 / L3 / L4
scope:        模型/并行/精度/硬件/版本
source:       commit/tag、脚本、测试、报告链接
date:         YYYY-MM-DD（查询/复核日期）
status:       shipped / experimental / roadmap / deprecated
caveats:      已知限制（如仅 PP=1、仅 FP8、仅某 GPU）
owner:        谁负责复核
```

复核触发条件：升级 Megatron/TE/PyTorch、换 GPU、换模型结构、改并行配置、
发布对外材料。

## J.4 本书关键声明的登记示例

下表是**示例**，不是对所有版本的承诺；实际以你锁定的版本为准。`章节`
列给出该声明在本书中的展开位置。

| ID | 声明 | 证据 | 发布 | 章节/来源 | 备注 |
|---|---|---|---|---|---|
| C-001 | mini 的 TP/PP/CP/DP/EP 语义与单卡等价 | E3 | — | 27、VERIFICATION | 仅 CPU/小模型 |
| C-002 | mini 支持可执行 interleaved GPipe | E2 | — | 21、`parallel_vpp.py` | 不是 1F1B/zero-bubble |
| C-003 | mini 支持非 ZeRO AdamW 重分片 | E3 | — | 24、`test_optimizer_state_reshard_roundtrip` | 不支持 ZeRO/EP/FSDP2 |
| C-004 | mini 有 MLA 参考实现 | E2 | — | 30.5、`test_mla.py` | 单进程，无 TP/CP/FP8 |
| C-005 | mini 有 flat distributed optimizer | E3 | — | 22.4.2、50.3 | 单 bucket、同 DP size |
| C-006 | mini 有 ring attention 参考 | E3 | — | 9.2.3、`test_ring_attention.py` | 无真实 P2P/重叠 |
| C-007 | mini 有 paged KV 数据结构 | E2 | — | 40.3.1、`test_paged_cache.py` | 无 paged-attention kernel |
| C-008 | mini 有 aux-free/node-limited 参考 | E2 | — | 10.3.4–10.3.5 | 未接入 EP 训练主链 |
| C-009 | Megatron Core 支持 MLA | E1–E4（版本相关） | L1–L2 | 30.5、29 | 模型/精度/并行组合需逐项验证 |
| C-010 | Megatron Core 支持 FP8/FP4 | E1–E4（硬件相关） | L1–L2 | 12、34 | 峰值≠可用吞吐；收敛需验证 |
| C-011 | HybridModel 基础支持 | E1–E2 | L1–L3 | 34.2、36 | EP overlap/FSDP 集成多为 roadmap |
| C-012 | Muon 支持 | E1–E2 | L1–L3 | 13.4、34.6 | 大规模分布式/收敛证据需查报告 |
| C-013 | DeepSeek-V4 的 HybridModel 支持 | E2（官方 release notes） | L1（0.20.0） | 34.3、35.4 | Bridge 0.7 有 `DeepSeekV4HybridModelProvider`；具体结构/并行配置仍需 recipe 验证 |
| C-014 | GB300 NVL72 的 FP4 **峰值数字** | E1（正文未给数值，仅方向） | L3（roadmap/厂商口径） | 34.5、37 | 37 只列 FP8 峰值；不要把 FP4 峰值当训练 MFU，引用前先补来源与口径 |
| C-015 | 1M 上下文训练课程/CP indexer | E1–E2 | L3（roadmap） | 9.2.2、34.4 | 未逐条核对 release tag |
| C-016 | 0.20 的 MLA/MoE NVFP4 recipe | E1–E2 | L2（已合并，待核对） | 12、34 | 以 tag/源码为准 |
| C-017 | “自建比云便宜” | — | — | 44 | 依赖利用率/云价；$3/h 下本模型不成立 |
| C-018 | “MFU 提升带来百万美元级收益” | — | — | 43.5 | 40%→45% 单任务约 $0.16M；年化 10 个任务约 $1.6M |

使用方式：对外材料引用 C-017/C-018 这类成本声明时，必须同时给出模型、
GPU 数、token 数、利用率、云价和年化口径；否则只能作为“示例”。

## J.5 常见“声明陷阱”

1. **版本号漂移**：把 dev 分支的功能写成“0.20 已发布”。解决：给 tag/commit。
2. **模型名漂移**：把“某家族支持”写成“某 3.5/4.x 变体支持”。解决：查
   model registry/recipe。
3. **硬件口径漂移**：把推理/MLPerf 数字写成训练 MFU。解决：写清 workload。
4. **精度口径漂移**：把 FP4 峰值写成 BF16 吞吐。解决：写 dense/sparse/精度。
5. **规模口径漂移**：把小模型/短序列的 MFU 外推到大模型/长序列。解决：给
   配置和序列长度。
6. **“支持”=“有开关”**：开关存在但无测试/recipe。解决：用 E1–E4 分级。
7. **二手引用**：博客/推文转述 release notes。解决：追到一手来源。
8. **时间点缺失**：roadmap 没有日期/状态。解决：记录查询日期和状态。
9. **混淆 E 与 L**：把“已发布（L1）”当成“生产验证（E4）”。解决：两列都填。
10. **单位口径**：GB vs GiB、双向 vs 单向、dense vs sparse。解决：在表头写清。

## J.6 对外材料的最小披露清单

如果要把训练/推理数字写进对外材料，至少披露：

```text
模型结构与参数量（总/激活）
训练 token 数 / 序列长度
硬件型号、数量、互联
精度（BF16/FP8/FP4）、dense/sparse
并行配置（TP/PP/DP/CP/EP）
MFU 口径（是否含通信/数据/失败）
软件版本（Megatron/TE/PyTorch/CUDA/NCCL）
失败率/重试/ checkpoint 策略
推理的 TTFT/TPOT/tokens/s/并发/序列分布
```

缺任何一项，数字都可能不可比。**披露口径不是形式主义，它决定了别人能否
复现你的结论。**

## J.7 维护流程

建议每季度做一次“声明审计”：

1. 导出所有对外/文档中的版本与性能声明；
2. 按 J.3 补 E/L 级别、source、date；
3. 对 release=L3 的条目检查是否已发布/放弃；
4. 对 evidence=E4 的声明检查是否有新的硬件/版本导致失效；
5. 更新正文/附录，删掉无法支撑的断言。

这和代码回归测试是同一件事：**声明也需要回归**。

---

上一附录：[附录 I 硬件、成本与规模参数手册](I-hardware-cost-parameters.md)  
返回目录：[SUMMARY](../SUMMARY.md)  
下一附录：[附录 K 模型转换映射速查](K-conversion-mappings.md)

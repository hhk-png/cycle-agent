# 附录 G 方案对比：Prometheus 生态与替代品

## G.1 对比维度

- 指标采集/存储/查询；
- 长期存储与全局查询；
- 多租户/权限；
- 运维复杂度；
- 成本；
- 兼容性（PromQL/remote write/OTel）；
- 适用规模。

## G.2 方案表

| 方案 | 定位 | 优点 | 局限 | 适合 |
|---|---|---|---|---|
| Prometheus 单机 | 指标采集/短期存储/查询/告警 | 简单、标准、生态大 | 扩展/长期/多租户有限 | 中小规模、边缘 |
| Prometheus + Thanos | 对象存储长期化 + 全局查询 | 成熟、组件化、成本可控 | 组件多、查询延迟 | 已有对象存储、多集群 |
| Prometheus + Mimir | 多租户水平扩展 | 多租户、HA、长期 | 资源/运维/成本 | 大型平台/服务商 |
| VictoriaMetrics | 高性能指标存储 | 压缩率高、成本低、PromQL 兼容 | 生态/语义差异 | 成本敏感/大规模 |
| 云托管 Prometheus | 托管采集/存储 | 低运维、云集成 | 成本/锁定/功能限制 | 云上中小团队 |
| Datadog/Dynatrace/New Relic | 全栈 SaaS | 开箱即用、APM/日志/追踪 | 成本/锁定/数据主权 | 全栈可观测性 |
| OTel + 后端 | 标准采集 + 任意后端 | 厂商中立、统一采集 | 需要后端/治理 | 长期架构 |

## G.3 与 Mini 的差异

| 方面 | Mini | 真实 Prometheus |
|---|---|---|
| 语言/性能 | Python/教学 | Go/生产 |
| 索引 | 线性 | postings |
| 存储格式 | JSON/自定义 | 二进制 |
| PromQL | 子集（75 个函数 + 14 个聚合） | 完整 |
| Native histogram | 无 | 实验/演进 |
| OTLP | 无 | 3.x |
| K8s SD | 无 | 有 |
| TLS server | 无 | 有 |
| HA/多租户 | 无 | 需外部 |
| Alertmanager | lite | 完整 |
| Remote write | 1.0 子集 | 1.0/2.0 |

Mini 不能用于生产；它的价值是教学/回归/对照。

## G.4 选型建议

- **< 50 目标、单集群**：Prometheus 单机 + 本地盘 + 快照。
- **多集群/长期**：Prometheus + Thanos（已有对象存储）或 Mimir（多租户）。
- **成本敏感/大规模**：评估 VictoriaMetrics，验证 PromQL/告警兼容。
- **全栈/低运维**：云托管或 SaaS，但要做成本/锁定评估。
- **OTel 战略**：Collector + Prometheus 兼容后端，避免采集层锁定。

## G.5 兼容性检查

- PromQL 函数/语义差异；
- remote write 版本/压缩；
- dashboard/alert 迁移；
- 数据导出格式；
- 认证/多租户 header；
- native histogram 支持。

## G.6 基准测试注意

- 用真实基数/查询；
- 区分摄入/查询/压缩；
- 注意缓存/预热；
- 记录硬件/版本；
- 不要直接引用厂商 benchmark。

## G.7 能力矩阵

把"支持/不支持"拆成可验证的列，比"功能全"有用：

| 能力 | Prometheus 单机 | Thanos | Mimir | VictoriaMetrics |
|---|---|---|---|---|
| PromQL | 完整 | 接近完整（查询层） | 接近完整 | MetricsQL 超集 |
| remote write 1.0 | 发送/接收 | Receiver | 接收 | 接收 |
| remote write 2.0 | 发送（实验，rc.4） | 视版本 | 视版本 | 视版本 |
| native histogram | 稳定（3.8+），抓取需 `scrape_native_histograms` | 视组件/版本 | 视版本 | 视版本 |
| OTLP 接收 | 3.x 内置 | 需 Collector/Receiver | 需 Collector/网关 | 原生支持多种协议 |
| 多租户 | 无 | 弱（Receiver 软隔离） | 强（租户/限额） | 中（accountID/网关） |
| HA 去重 | 无（靠 Alertmanager/远端） | Querier 去重 | Distributor 去重 | 复制 + 去重 |
| 长期 retention | 本地磁盘 | 对象存储 | 对象存储 | 本地盘为主 |
| 降采样 | 无 | 内置 5m/1h | 支持 | 企业版 |
| 全局查询 | 联邦/remote read | Querier | Querier | vmselect |
| 删除/合规 | delete_series + tombstone | retention/对象存储生命周期 | 租户级删除流程 | retention/API |
| 部署复杂度 | 低 | 中高 | 高 | 中 |

"视版本"不是含糊，而是提醒：这些能力在快速演进，必须以目标版本的 release notes/实测为准，不能拿旧博客当事实。

## G.8 基准测试方法

一个可复现的基准至少记录：

```text
数据形状：活跃序列数、每序列样本数、标签基数、直方图比例
写入：samples/s、batch size、压缩、副本/复制因子
查询：查询集合（instant/range/聚合/正则/高基数）、并发、时间范围、step
硬件：CPU/内存/磁盘类型/对象存储/网络
版本：Prometheus/Thanos/Mimir/VM 的具体版本与配置
结果：P50/P95/P99 延迟、错误率、摄入延迟、compaction 时间、成本
```

常见错误：

- 只用合成指标（如 `avalanche`）测，不用真实标签分布；
- 只测热缓存，不测冷启动/历史查询；
- 只测摄入，不测查询/compaction；
- 忽略对象存储 GET/PUT 延迟和出口流量；
- 用厂商 benchmark 的绝对值，不验证自己的数据形状。

## G.9 迁移检查表

- [ ] 指标名/标签/类型在目标后端语义一致；
- [ ] PromQL 函数（尤其实验函数）在目标后端可用；
- [ ] recording/alerting 规则已回归测试；
- [ ] Grafana 数据源/变量/exemplar 正常；
- [ ] remote write 版本/压缩/元数据兼容；
- [ ] 多租户 header/认证/配额已配置；
- [ ] 历史数据回填/归档策略明确；
- [ ] 删除/retention/合规行为验证；
- [ ] 双跑/切读/回滚路径演练；
- [ ] 成本模型包含存储/查询/网络/人力。

## G.10 常见误区

- **"开源 = 免费"**：自托管的硬件/人力/on-call 都是成本。
- **"兼容 PromQL = 语义完全一致"**：函数边界、实验函数、正则、native histogram 都可能有差异。
- **"对象存储便宜，所以长期存储一定便宜"**：查询缓存/计算/出口流量可能更贵。
- **"多副本 = 高可用"**：没有去重/故障转移/查询层，副本只会产生重复数据。
- **"降采样没有代价"**：长期数据的分辨率下降后，不能用来判断短窗口 SLO。
- **"先上平台，再治理基数"**：基数会先打爆预算/内存，治理应前置。

## 延伸阅读

- 各项目官方文档/基准。
- 本教程第 18、44、47 章与附录 Q（长期存储架构深入）。

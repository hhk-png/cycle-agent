# 附录 H 术语表

> 按主题分组，最后给"最容易混淆的概念"对照。首次出现的详细解释以对应章节为准。

## H.1 数据模型

**Metric（指标）**：一个名字 + 一组标签的家族；同一 metric 下不同标签组合是不同序列。

**Series（序列）**：`__name__` + 完整标签集合的唯一标识；Prometheus 的基本存储/查询单位。

**Sample（样本）**：`(timestamp, value)`；value 是 float64，timestamp 是毫秒。

**Label（标签）**：序列的维度键值对；决定身份、基数和查询能力。

**Cardinality（基数）**：标签值组合的数量；近似等于活跃序列数，是容量/成本的第一驱动。

**Active series（活跃序列）**：最近有样本、仍在 head/查询范围内的序列。

**Staleness（陈旧）**：特殊 NaN 标记，表示序列当前不可查询；与"值为 0"不同。

**Exemplar**：样本关联的追踪 ID/元数据（如 `trace_id`），用于从指标跳到追踪。

**Metadata（元数据）**：HELP、TYPE、UNIT 等描述信息，不参与序列身份。

**UTF-8 名称**：Prometheus 3.x 允许指标名/标签名包含点号、非 ASCII 等字符；PromQL 中需引号。

## H.2 指标类型

**Counter**：单调递增的累计值；重启归零由 `rate`/`increase` 处理。

**Gauge**：可增可减的瞬时值。

**Histogram（经典直方图）**：累积 `le` 桶 + `_sum`/`_count`；服务端可聚合，分位数有插值误差。

**Summary**：客户端计算分位数 + `_sum`/`_count`；分位数不可跨实例聚合。

**Native histogram（原生直方图）**：稀疏指数桶直方图；3.8 起稳定，序列数少、精度高。

**Info metric**：值恒为 1、用标签携带元数据的指标（如 `build_info`）。

**NHCB**：Native Histogram Custom Buckets，自定义桶的原生直方图兼容模式。

## H.3 存储

**TSDB**：Prometheus 内置时序数据库。

**Head**：内存中的最近数据（series/chunks/postings），写入首先进入这里。

**Chunk**：序列的一段压缩样本；经典实现默认 120 样本/2h。

**XOR / XOR2**：Gorilla 风格的浮点 chunk 编码；XOR2 在 3.15 稳定。

**WAL（Write-Ahead Log）**：崩溃恢复日志；写入先落 WAL 再进 head。

**Checkpoint**：WAL 的压缩快照，减少重启回放时间。

**Block**：不可变时间片，含 `meta.json`/`index`/`chunks`；2h 级别。

**Compaction**：合并 block、应用 tombstone、释放空间的后台过程。

**Retention**：数据保留时间/大小；删除以 block 为单位。

**Tombstone**：删除区间元数据；查询时过滤，compaction 时真正释放。

**Snapshot**：某一时刻的 TSDB 一致性副本，用于备份/迁移。

**Out-of-order（OOO）**：时间戳早于已写入最新样本的样本；默认拒绝，需 OOO head。

## H.4 查询

**PromQL**：Prometheus 查询语言。

**Instant vector**：每个序列一个当前样本。

**Range vector**：每个序列一个时间窗内的样本列表；不能直接参与二元运算。

**Scalar**：单个浮点数。

**String**：字符串字面量；只能作为函数参数。

**Lookback delta**：instant selector 查找最新样本的回看窗口（默认 5m）。

**Matcher**：`=`/`!=`/`=~`/`!~` 选择器条件；正则全匹配。

**Vector matching**：二元运算的标签匹配（`on`/`ignoring`/`group_left`/`group_right`）。

**Subquery**：对 instant query 按 step 求值，得到 range vector；语法 `[range:step]`。

**`@` modifier**：把求值时间钉在某个时刻；`start()`/`end()` 仅 range query。

**`offset`**：把数据窗口整体前后移动；负 offset 表示看向未来。

**Recording rule**：把查询结果写成新序列的规则。

**Alerting rule**：把查询结果变成告警的规则。

**`query_offset`**：规则组级查询时间后移，用于延迟数据/`smoothed`；全局默认 `rule_query_offset`。

## H.5 抓取与发现

**Scrape**：Prometheus 主动抓取目标 `/metrics` 的行为。

**Exposition format**：`/metrics` 的文本/OpenMetrics 格式。

**OpenMetrics**：Prometheus 文本格式的标准扩展（EOF/UNIT/exemplar）；2.0 在 3.15 实验。

**Service discovery（SD）**：动态发现抓取目标（static/file/http/DNS/K8s/…）。

**Relabel**：抓取前修改目标标签/地址的规则引擎。

**`metric_relabel_configs`**：抓取后、入库前修改样本标签的规则。

**`honor_labels`**：目标自带 `job`/`instance` 时是否保留目标值（否则加 `exported_`）。

**`honor_timestamps`**：是否采用目标暴露的时间戳；关闭则用抓取时间。

**`up`**：抓取是否成功的 0/1 指标；目标消失时序列可能消失，不是 `up=0`。

**Pushgateway**：短生命周期任务的最后值缓存；不是通用推送 TSDB。

**Agent mode**：只抓取 + remote write，不查询/不规则的运行模式。

## H.6 告警

**Alertmanager**：独立告警路由/分组/抑制/静默/通知组件。

**Firing**：告警条件满足且超过 `for` 的状态。

**Pending**：条件满足但还没到 `for` 的状态。

**Resolved**：条件消失后发送的恢复通知；不是所有消失都会发送。

**`for`**：持续满足多久才 firing。

**`keep_firing_for`**：条件消失后继续保持 firing，抑制抖动。

**Inhibition**：一个告警压制另一类告警。

**Silence**：按 matcher 临时静音。

**Grouping**：把相关告警合并成一条通知。

**Route**：Alertmanager 路由树中的匹配/分发规则。

**Dedup（去重）**：HA 副本写入远端后，查询层按 replica 去重。

**Replica label**：HA 副本的去重标签（如 `replica`）。

## H.7 SLO 与成本

**SLI**：服务质量指标（good/total 的比例或延迟）。

**SLO**：服务质量目标（如 99.9% 月可用性）。

**SLA**：对外的服务合同；通常比 SLO 宽松，带赔偿条款。

**Error budget**：`1 - SLO`，允许的失败量。

**Burn rate**：错误消耗预算的速度；`错误率 / (1-SLO)`。

**MTTR/MTTD**：平均恢复时间/平均发现时间。

**TCO**：总拥有成本；含硬件、人力、存储、网络、许可、迁移。

**Cardinality budget**：每个指标/团队的序列数预算。

**Query cost**：一次查询预计/实际触碰的序列数、读取样本数与耗时；3.16 起可估算、可设上限（实验）。

**Monitoring effectiveness**：监控在用户受影响时能否发现、定位并触发行动；比"Prometheus 进程存活"更广。

**Maturity model**：按数据/信号/决策/组织四层评估可观测性能力，常见 L0–L4 分级。

## H.8 生态与长期存储

**Thanos**：基于对象存储的长期存储/全局查询；Sidecar/Receiver/Query/Store/Compactor。

**Mimir**：Grafana 的多租户水平扩展长期存储。

**VictoriaMetrics**：高压缩、少组件的 Prometheus 兼容存储。

**Cortex**：Mimir 的前身/同源项目。

**Prometheus Operator**：用 CRD 管理 Prometheus/Alertmanager/规则。

**ServiceMonitor/PodMonitor**：Operator 的抓取目标 CRD。

**kube-state-metrics**：Kubernetes 对象状态指标。

**node-exporter**：节点 OS 指标 exporter。

**blackbox_exporter**：HTTP/TCP/ICMP/DNS/gRPC 探针。

**OTLP**：OpenTelemetry 协议。

**OTel Collector**：采集/转换/导出管道；常作为 Prometheus 的前置。

**Remote write/read**：与远端存储交换样本的协议。

**Federation**：上游抓取下游 `/federate` 的聚合指标。

**RW2**：Remote Write 2.0；截至 2026-11 仍是 `2.0-rc.4` 实验规范。

## H.9 LLM 推理

**TTFT（Time To First Token）**：请求开始到第一个输出 token 的时间；包含排队 + prefill。

**TPOT（Time Per Output Token）**：`(E2E - TTFT) / (output_tokens - 1)`，每输出 token 的平均时间。

**ITL（Inter-Token Latency）**：相邻输出 token 的时间间隔；看长尾/卡顿。

**E2E**：请求开始到最后一个 token 的时间；受输出长度影响。

**Goodput**：满足 TTFT/TPOT 目标的请求数/秒；比裸吞吐更接近用户体验。

**Prefill**：并行处理输入 prompt、生成 KV cache 的阶段；影响 TTFT。

**Decode**：逐 token 生成的阶段；影响 TPOT/ITL。

**KV cache**：注意力键值的缓存；随上下文长度线性增长，是显存/并发的主要约束。

**Continuous batching**：请求级动态批处理；提高吞吐，但会增加排队/TPOT。

**Prefix cache**：复用相同前缀的 KV cache；提高 TTFT/吞吐。

**Preemption**：KV 不足时驱逐/重算请求；表现为 TPOT/TTFT 抖动。

**Reasoning tokens**：推理模型内部生成的 token；可能按 output 计费。

## H.10 最容易混淆的概念

| 概念对 | 区别 |
|---|---|
| `rate` vs `irate` | `rate` 用整个窗口并外推；`irate` 只用最后两个样本 |
| `increase` vs `delta` | `increase` 面向 counter（处理 reset）；`delta` 面向 gauge |
| instant vs range vector | instant 每序列一个样本；range 是窗口列表，不能直接二元运算 |
| lookback vs retention | lookback 是查询回看窗口（默认 5m）；retention 是数据保留期（天/周） |
| staleness vs 值为 0 | stale 表示"不可查询"；0 是真实值 |
| series vs sample | series 是身份；sample 是某个时刻的值 |
| cardinality vs series 数 | 基数通常指标签组合数，活跃序列数是其近似度量 |
| head vs block | head 在内存、可写；block 在磁盘、不可变 |
| compaction vs retention | compaction 合并/清理；retention 决定删哪些 block |
| tombstone vs 真删除 | tombstone 是删除标记；compaction 才释放空间 |
| pull vs remote write | pull 是 Prometheus 抓目标；remote write 是 Prometheus 写远端 |
| federation vs remote read | federation 拉聚合指标；remote read 查原始样本 |
| `up=0` vs 序列消失 | `up=0` 是抓取失败；目标被移除后序列会 stale/消失 |
| `for` vs `keep_firing_for` | `for` 控制进入 firing；`keep_firing_for` 控制离开 firing |
| `offset` vs `@` vs `query_offset` | `offset` 移数据窗口；`@` 钉求值时刻；`query_offset` 移规则查询时间 |
| Summary vs Histogram | Summary 客户端算分位数、不可聚合；Histogram 服务端可聚合 |
| 经典 vs 原生直方图 | 经典是 `le` 桶序列；原生是稀疏指数桶，序列少、精度高 |
| Prometheus vs Thanos/Mimir | Prometheus 单机 TSDB/抓取；后者补长期存储/全局查询/多租户 |
| Agent vs Prometheus | Agent 只抓取+remote write；完整版有查询/规则/本地 TSDB |
| SLO vs SLA | SLO 是内部目标；SLA 是对外合同 |
| exemplar vs trace | exemplar 是样本上的指针；trace 是完整调用链 |
| TTFT vs E2E | TTFT 只到第一个 token；E2E 到最后一个 token |
| TPOT vs ITL | TPOT 是平均值；ITL 是逐 token 间隔，能看长尾 |
| throughput vs goodput | throughput 是总吞吐；goodput 是满足 SLO 的吞吐 |
| prefill vs decode | prefill 并行处理输入；decode 逐 token 生成 |
| KV cache vs 模型权重 | KV 随上下文增长；权重固定，二者争显存 |

## 延伸阅读

- Prometheus 官方 glossary。
- 本教程第 3 章（数据模型）、第 4 章（类型）、第 14～17 章（存储/查询）、第 25 章（SLO）。
- 附录 G（方案对比）、附录 Q（长期存储）。

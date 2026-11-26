# 附录 I 延伸阅读

> 这份清单按"读完能做什么"组织，而不是按链接堆积。先读官方文档建立语义，再读源码/论文理解实现，最后用本仓库的 Mini 对照验证。

## I.1 官方文档（先读这一层）

必读顺序：

1. **Overview / Architecture**：`https://prometheus.io/docs/introduction/overview/`
2. **Data model**：metric/label/sample/series 的定义。
3. **Querying basics**：选择器、range vector、offset/@、subquery。
4. **Operators**：聚合、二元运算、向量匹配、`bool`。
5. **Functions**：按类别读，不要从头背。
6. **Configuration**：`scrape_configs`、relabel、rule_files、alerting、remote_write。
7. **Storage**：TSDB、retention、remote read/write。
8. **Federation**：`/federate` 的语义与限制。
9. **Alerting**：规则、Alertmanager 路由/抑制/静默。
10. **Migration**：版本升级、feature flag、LTS。

直达链接：

- 文档首页：`https://prometheus.io/docs/`
- PromQL：`https://prometheus.io/docs/prometheus/latest/querying/basics/`
- 配置：`https://prometheus.io/docs/prometheus/latest/configuration/configuration/`
- 存储：`https://prometheus.io/docs/prometheus/latest/storage/`
- 联邦：`https://prometheus.io/docs/prometheus/latest/federation/`
- Alertmanager：`https://prometheus.io/docs/alerting/latest/alertmanager/`
- Remote write spec：`https://prometheus.io/docs/specs/remote_write_spec/`
- Release notes / LTS：`https://prometheus.io/docs/introduction/release-cycle/`

## I.2 规范与协议

- **OpenMetrics 1.0**：exposition 的标准扩展（EOF/UNIT/exemplar）；3.15 起有 OpenMetrics 2.0 实验。
- **Prometheus exposition formats**：text/OpenMetrics/protobuf 的 Content-Type 与解析规则。
- **Remote Write 1.0/2.0**：队列、重试、元数据、exemplar、native histogram；2.0 截至 2026-11 仍是 rc。
- **OTLP**：metrics 数据模型、temporality（cumulative/delta）、resource/scope。
- **OpenTelemetry semantic conventions**：HTTP/RPC/DB/messaging/GenAI 的属性与单位。

读规范时重点看：

- 时间戳单位（秒 vs 毫秒）；
- counter 的 `_total` 与 OpenMetrics 的 `_total` 规则；
- exemplar 的标签约束（3.16 起重复标签名被拒绝）；
- delta temporality 如何转 cumulative；
- 错误码与重试语义（429/5xx vs 4xx）。

## I.3 论文与设计文档

- ***Gorilla: A Fast, Scalable, In-Memory Time Series Database***（Facebook, VLDB 2015）：XOR 压缩、时间戳 delta-of-delta 的源头。
- **Prometheus TSDB 设计文档**：head/block/WAL/compaction 的取舍。
- **Thanos 设计文档**：Sidecar/Store/Query/Compactor 与对象存储布局。
- **Mimir 架构文档**：多租户、shipper、ingester、querier、store-gateway。
- **VictoriaMetrics 文档**：高压缩存储、merge 模式、MetricsQL 兼容。
- **Google SRE Book / Workbook**：监控、告警、SLO、错误预算。
- **Observability Engineering**：指标/日志/追踪的边界与组织实践。

读论文时带着问题：

- 为什么 XOR 适合浮点时序？边界/NaN 怎么处理？
- 为什么 block 不可变？compaction 如何控制写放大？
- 为什么 Prometheus 不做集群？长期存储如何补？
- 为什么 remote write 需要队列/重试/去重？

## I.4 书籍

- *Prometheus: Up & Running*（Brian Brazil）：最完整的 Prometheus 实践书，适合第 1～12 章配套。
- *Site Reliability Engineering*（Google）：监控分布式系统、告警、发布。
- *The Site Reliability Workbook*：SLO/错误预算的落地案例。
- *Observability Engineering*（Majors, Fong-Jones, Miranda）：可观测性组织与成本。
- *Implementing Service Level Objectives*（Alex Hidalgo）：SLI/SLO 的设计与运营。

## I.5 博客、会议与课程

- Prometheus 官方博客（release/设计说明）；
- Grafana Labs 博客（Mimir/Loki/Tempo/Alloy/Grafana Alerting）；
- Thanos/VictoriaMetrics 博客（架构/调优/故障）；
- CNCF 可观测性 landscape 与 end-user 案例；
- PromCon 演讲（PromQL、TSDB、native histogram、scaling）；
- KubeCon/CloudNativeCon 的 observability track；
- 各大厂 SRE/可观测性团队的工程博客。

筛选标准：

- 有版本号/数据/可复现实验，而不是只讲"我们上了 X"；
- 区分"官方语义"与"厂商扩展"；
- 注意发布时间，Prometheus 3.x 之后很多 2.x 结论已过期。

## I.6 源码阅读顺序

按依赖从下往上：

```text
promql/parser/      词法/语法/AST/类型检查
promql/engine.go    求值器、区间选择、函数
promql/functions.go 函数实现（rate/histogram_quantile 等）
tsdb/head.go        head、series、chunk 管理
tsdb/chunkenc/      XOR/XOR2 chunk 编解码
tsdb/wlog/          WAL、checkpoint、回放
tsdb/compact.go     compaction 计划与执行
tsdb/index/         postings/索引格式
scrape/scrape.go    抓取循环、limit、stale
discovery/          SD 实现
rules/              规则加载/求值/告警状态机
notifier/           Alertmanager 通知
web/api/v1/         HTTP API 与序列化
storage/remote/     remote write/read 队列
```

对照阅读：

- 本仓库 `mini_prometheus/promql/` ↔ 上游 `promql/`；
- `mini_prometheus/tsdb/` ↔ 上游 `tsdb/`；
- `mini_prometheus/scrape/` ↔ 上游 `scrape/`；
- `mini_prometheus/rules/` ↔ 上游 `rules/`；
- `mini_prometheus/api/` ↔ 上游 `web/api/v1/`。

每读一个子系统，回答三个问题：

1. 它的输入/输出是什么？
2. 失败时会发生什么（崩溃/重试/丢弃/stale）？
3. 哪个测试覆盖了这个失败路径？

## I.7 社群与版本跟踪

- Prometheus GitHub issues/PR：搜 `area/tsdb`、`area/promql`、`area/scraping`；
- CNCF Slack `#prometheus`；
- PromCon 视频与议程；
- 本地 meetup / 邮件列表；
- 版本跟踪：`CHANGELOG.md`、release notes、LTS 表、feature flag 列表。

跟踪版本时特别关注：

- `[CHANGE]`/`[FEATURE]`/`[SECURITY]` 段；
- 存储编码（XOR2/ST）与读取端兼容；
- PromQL 语义变化（3.14 时长表达式、3.16 查询成本）；
- remote write 2.0 的 rc 与接收端支持；
- OTel/UTF-8 的语义约定演进。

## I.8 按目标的学习路线

| 目标 | 先读 | 再读 | 实践 |
|---|---|---|---|
| 会用 | 第 1～12 章 | 附录 A/C/E | 第 2 章快速开始 + 附录 K |
| 做平台 | 第 13～24 章 | 附录 P/Q/U/X | 第 46～48 章 |
| 读源码 | 第 13～17、31～43 章 | 附录 AB | 对照 Mini 跑测试 |
| 做 SLO | 第 25～27 章 | 附录 C/O/S | 第 50～51 章 |
| 做治理 | 第 47、49 章 | 附录 Z/L | 指标契约 + 评审 |
| 做选型 | 第 18、22、27、52～56 章 | 附录 G/Q | TCO/评分矩阵 |
| 做 OTel | 第 20、44 章 | 附录 AF/AG/R | Collector + Mini OTLP |
| 做安全 | 第 21、49 章 | 附录 L | 基线检查 |

## I.9 12 周阅读计划（示例）

```text
第 1～2 周：第 1～5 章 + 附录 A/D；跑通 quickstart。
第 3～4 周：第 6～8 章 + 附录 O；用 Mini 写 20 条查询。
第 5 周：第 9～12 章 + 附录 C/E；规则与抓取故障演练。
第 6～7 周：第 13～17 章 + 附录 AB；读 TSDB/PromQL 源码。
第 8 周：第 18～20 章 + 附录 Q/R/AE；remote write/OTel。
第 9 周：第 21～24 章 + 附录 L/P/U；安全与 K8s。
第 10 周：第 25～30 章 + 附录 S/Z；SLO/告警/未来。
第 11～12 周：第 31～51 章；Mini 实现 + 综合实战。
```

## I.10 本仓库

- `README.md`：教程地图/快速开始；
- `VERIFICATION.md`：验证结果/覆盖矩阵；
- `mini_prometheus/`：可运行实现；
- `tests/`：语义/边界/审计回归测试；
- `examples/`：10 个场景（含 OTLP 接入）；
- `appendices/`：速查、配置、规则库、故障手册、安全基线等。

## 延伸阅读

- 从本教程第 1 章开始，或按第 1.7 节的学习路径。
- 需要版本基线时先看附录 AE，再读官方 release notes。

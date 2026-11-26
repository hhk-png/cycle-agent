# Prometheus 深入教程：从指标语义到可运行的 Mini-Prometheus

> 面向 SRE、平台工程师和后端工程师。既讲清 Prometheus 的指标模型、PromQL、存储引擎、抓取、告警与规模化实践，也带你从零实现一个有测试、有示例、可运行的简化 Prometheus。

## 这套教程解决什么问题

监控系统最常见的失败不是"没有装 Prometheus"，而是：

- 指标名/标签建模错误，导致查询无法回答业务问题；
- 高基数标签把 head 内存打爆；
- `rate()`/`histogram_quantile()` 用错，告警长期失真；
- 抓取失败/目标消失/stale 的语义不清，出现"假平线"；
- 规则/告警没有测试，故障时才发现表达式是错的；
- 单机扩展到瓶颈，不知道下一步该拆什么；
- 面板全绿、`up` 全 1，但用户受影响时没有告警或没人能行动；
- 选型只看功能列表，忽略成本、锁定和迁移。

本教程用一条完整的链路回答这些问题：**语义 → 查询 → 存储 → 抓取 → 告警 → 规模化 → 可运行实现 → 综合实战 → 商业分析**。

## 本版亮点

- **本版新增/重写**：新增附录 AJ（监控有效性评估与成熟度：四层模型、取证查询、100 分评分卡、两周审计）；附录 AH（电商/支付/流媒体/SaaS/IoT 多行业案例库）与 AI（LLM 推理监控、TTFT/TPOT、KV cache、GPU、token 成本）；附录 A（PromQL 速查）、D（命名）、H（术语）、I（阅读）、L（安全基线）重写加深；第 22 章与附录 AE 扩写 3.16 `query-cost` 的端点/配置/上限/错误类型/指标；第 52～56 章补充 2025–2026 市场并购、定价与认证数据点；Mini 补上组级 `query_offset`、全局 `rule_query_offset`、`first_over_time`、`ts_of_first/last_over_time` 与 3.16 实验函数 `integral`，全部有回归测试。
- **56 章 + 36 篇附录**，覆盖基础、内部原理、SLO/告警工程、exemplar/追踪、HTTP API、部署/GitOps、指标治理、性能基准、合规/审计、容量/成本、迁移、综合实战、未来方向与商业分析；第 44～49 章为生产专题（含合规、数据生命周期与审计），第 50～51 章用两个综合实战把设计、平台化、压测、演练与复盘串起来；附录 V～Z、AA～AJ 为配套速查与手册（API 速查、埋点示例、部署模板、压测手册、治理清单、exporter/探针、上游源码导读、工具链、exporter 开发指南、3.14–3.16 迁移、UTF-8 名称与转义、OTLP 接收与 delta 转换、多行业案例库、LLM 推理监控、监控有效性评估与成熟度）。
- **可运行的 Mini-Prometheus**：Python 标准库 + PyYAML，包含 XOR chunk、WAL、block/compaction、PromQL 解析/执行、抓取/relabel、规则/告警（含组级 `query_offset` 与全局 `rule_query_offset`）、HTTP API、federation、remote write/read、OTLP/JSON 接收，以及 `promtool test rules` 风格的规则单元测试器。
- **528 个 pytest 用例全部通过**（510 个测试函数，参数化后 528 项），覆盖正常路径、边界、失败与真实缺陷回归。
- **UTF-8 名称全链路**：PromQL 引号选择器、引号分组标签、text exposition 的 `{"metric", "label"="value"}`、序列化统一转义；Mini 有独立回归测试，迁移与 `escaping` 协商见附录 AF。
- **OTLP/HTTP JSON 接收**：resource/scope → 标签、UCUM 单位后缀、单调 sum 的 `_total`、explicit-bucket histogram 展开、delta→cumulative 状态机（`otlp-deltatocumulative`）；可运行示例与边界见附录 AG。
- **本版语义修正**：`histogram_quantile` 最高桶、`group_left/right` 基数、Prometheus 3 左开区间/lookback/子查询、OpenMetrics 秒级时间戳、WAL 尾部计量、抓取失败写 stale、append 错误不改变 `up`；第二轮独立审计的 16 项修复（`label_replace`/relabel 展开、scalar bool、平滑算法、block/snapshot tombstone、group include、`round`、`keep_firing_for`、NaN 排序/聚合等）；第三轮独立审计的 17 项修复（bool 修饰符跨 RHS、transform 函数丢名、Go `$1x` 展开、clamp NaN、`min/max_over_time` NaN、`last_over_time` 保名、`sample_limit` 后置、`honor_labels`、API 默认时间范围、负时间戳、11k 点边界等）；第四轮独立审计的 10 项修复（一对一 `on/ignoring` 结果标签、比较运算的 group include、`@ 0` 区间窗口、OTLP delta 桶边界、`timestamp(0)`、head epoch/负时间哨兵、11,001 点边界、`1/-0` 符号、`absent` 标签合成、重复样本计数/WAL），全部有回归测试。
- **版本基线**：以 3.13 LTS（支持到 2027-07）为主、兼顾 3.16；原生直方图 3.8 起稳定、3.9 起旧 flag 为 no-op，抓取改 `scrape_native_histograms`；Remote Write 2.0 仍是 `2.0-rc.4` 实验规范；`smoothed`/`anchored` 3.16 起稳定；3.14 的 PromQL 时长表达式、3.15 的 `runtime.log_level`/XOR2/Unix socket、3.16 的查询成本控制与 exemplar 校验收紧均在附录 AE 中逐条说明。
- **10 个可运行示例**、Alertmanager-lite、demo exporter，全部可离线运行；新增规则单元测试与 OTLP 接入示例。
- **文档自检**：章节/小节编号、链接、YAML、测试/示例/promtool 计数、代码编译。
- **生产向专题**：exemplar 与追踪/profiling 关联、HTTP API/UI/自动化、部署拓扑与 GitOps、指标治理、性能基准与容量验证、合规/数据生命周期/审计；PromQL 高级函数与查询模式、Operator 协调/分片/排障、Thanos/Mimir/VictoriaMetrics 架构对比、原生直方图与 Remote Write 2.0 迁移、18 个生产事故复盘、Pushgateway 生命周期。
- **采集面补全**：附录 AA 覆盖 node_exporter、blackbox/SNMP 探针、JMX 与 MySQL/PostgreSQL/Redis/Kafka 等中间件 exporter 的指标语义、relabel、告警与基数陷阱；附录 AB 给出从 Mini 进入上游 Go 代码库的按子系统阅读路线；附录 AC 汇总 promtool/amtool/pint/thanos/mimirtool 等工具链；附录 AD 给出自建 exporter 的设计、测试、打包与安全清单；附录 AE 给出 3.14–3.16 的新特性、破坏性变化、迁移矩阵与回滚边界。
- **明确边界**：Mini 与真实 Prometheus 的差异逐项记录，不把教学实现包装成生产系统。

## 架构一眼

```text
service discovery → relabel → scrape → exposition parser
                                      ↓
                                 TSDB head/WAL
                                      ↓
                         blocks / compaction / retention
                                      ↓
                    PromQL engine ← HTTP API / UI / federation
                                      ↓
                         rules → notifier → Alertmanager
                                      ↓
                              remote write / remote read
```

## 快速开始

要求：Python 3.11+、PyYAML、pytest。

```bash
python -m pip install -r requirements.txt

# 三条验证命令
python -m pytest
python examples/run_all.py
python check_docs.py

# 规则单元测试（promtool 风格）
python -m mini_prometheus.promtool test rules examples/rules/demo_test.yml
python -m mini_prometheus.promtool test rules examples/rules/capstone_test.yml
```

预期结果：

- pytest：**528 passed**
- examples：**10 / 10 passed**
- check_docs：章节/链接/YAML/计数全部通过
- promtool：**3 test(s) passed**（demo 2 + capstone 1）

启动一个可交互的 Mini-Prometheus：

```bash
# 终端一：启动 demo exporter
python examples/demo_exporter.py --port 9100

# 终端二：启动 Mini
python -m mini_prometheus \
  --config.file=examples/config/prometheus.yml \
  --storage.tsdb.path=./data \
  --web.listen-address=127.0.0.1:9090
```

打开 <http://127.0.0.1:9090/graph>，或：

```bash
curl -s 'http://127.0.0.1:9090/api/v1/query?query=up'
curl -s 'http://127.0.0.1:9090/api/v1/query?query=sum(rate(demo_http_requests_total[1m]))'
curl -s 'http://127.0.0.1:9090/api/v1/rules'
```

OTLP/JSON 接入（独立示例，自带临时数据目录）：

```bash
python examples/10_otlp_ingest.py
```

或启动时加 `--web.enable-otlp-receiver --enable-feature=otlp-deltatocumulative`，再向 `POST /api/v1/otlp/v1/metrics` 发送 `Content-Type: application/json` 的 OTLP 请求。

## 目录

### 第一部分：基础与语义

1. [为什么需要 Prometheus](chapters/01-why-prometheus.md)
2. [快速上手](chapters/02-quickstart.md)
3. [数据模型：指标、标签、样本与序列](chapters/03-data-model.md)
4. [指标类型：Counter、Gauge、Histogram、Summary 与原生直方图](chapters/04-metric-types.md)
5. [指标暴露：文本格式、OpenMetrics 与客户端库](chapters/05-exposition-and-client-libs.md)
6. [PromQL 基础：类型、选择器与时间修饰符](chapters/06-promql-selectors.md)
7. [PromQL 计算：rate、increase 与直方图分位数](chapters/07-promql-rates-and-histograms.md)
8. [PromQL 聚合、二元运算与向量匹配](chapters/08-promql-aggregation-and-matching.md)
9. [记录规则与告警规则](chapters/09-recording-and-alerting-rules.md)
10. [告警生命周期与 Alertmanager](chapters/10-alertmanager-and-alert-lifecycle.md)
11. [服务发现与 relabel](chapters/11-service-discovery-and-relabeling.md)
12. [抓取生命周期与失败语义](chapters/12-scrape-lifecycle.md)

### 第二部分：架构与内部原理

13. [架构总览：单机 Prometheus 的组件与边界](chapters/13-architecture.md)
14. [TSDB（一）：Head、Chunk 与 WAL](chapters/14-tsdb-head-chunks-wal.md)
15. [TSDB（二）：Block、Compaction、Retention 与 Tombstone](chapters/15-tsdb-blocks-compaction.md)
16. [索引与查询路径：从 matcher 到样本](chapters/16-index-and-query-path.md)
17. [PromQL 执行模型与查询优化](chapters/17-promql-execution-and-optimization.md)
18. [远程读写、联邦与长期存储](chapters/18-remote-read-write-federation.md)
19. [高可用、分片与 Agent 模式](chapters/19-ha-sharding-and-agent.md)
20. [Prometheus 3.x、原生直方图与 OpenTelemetry](chapters/20-prometheus-3-and-otel.md)
21. [安全与多租户](chapters/21-security-and-multitenancy.md)
22. [容量规划与性能调优](chapters/22-capacity-and-performance.md)
23. [运维：配置、备份、升级与故障排查](chapters/23-operations-upgrade-troubleshooting.md)
24. [Kubernetes 监控栈](chapters/24-kubernetes-monitoring.md)

### 第三部分：SLO 与工程实践

25. [SLO、错误预算与 Burn-Rate 告警](chapters/25-slo-and-burn-rate.md)
26. [告警工程：从"能报警"到"值得起床"](chapters/26-alert-engineering.md)
27. [基数治理与成本工程](chapters/27-cardinality-and-cost.md)
28. [测试、验证与混沌工程](chapters/28-testing-and-chaos.md)
29. [迁移与兼容：从旧监控到 Prometheus](chapters/29-migration-and-compatibility.md)
30. [未来方向：Prometheus、OpenTelemetry 与可观测性](chapters/30-future-directions.md)

### 第四部分：Mini-Prometheus 实现

31. [Mini 设计：目标、边界与模块](chapters/31-mini-design.md)
32. [Mini 实现（一）：标签、匹配器与 exposition 解析](chapters/32-mini-labels-and-exposition.md)
33. [Mini 实现（二）：XOR Chunk 与 Head](chapters/33-mini-head-and-chunks.md)
34. [Mini 实现（三）：WAL 与崩溃恢复](chapters/34-mini-wal.md)
35. [Mini 实现（四）：Block、Compaction 与 Retention](chapters/35-mini-blocks-and-compaction.md)
36. [Mini 实现（五）：TSDB 查询与序列合并](chapters/36-mini-query-engine.md)
37. [Mini 实现（六）：PromQL 词法、语法与类型检查](chapters/37-mini-promql-frontend.md)
38. [Mini 实现（七）：PromQL 求值器](chapters/38-mini-promql-evaluator.md)
39. [Mini 实现（八）：服务发现、relabel 与抓取循环](chapters/39-mini-scrape-and-relabel.md)
40. [Mini 实现（九）：规则、告警状态机与通知](chapters/40-mini-rules-and-alerts.md)
41. [Mini 实现（十）：HTTP API、联邦与远程读写](chapters/41-mini-api-and-remote.md)
42. [Mini 的测试策略与缺陷复盘](chapters/42-mini-testing-and-defects.md)
43. [从 Mini 到生产：Go 重写、并发与兼容性](chapters/43-mini-to-production.md)

### 第五部分：生产专题与平台工程

44. [Exemplars、追踪关联与 Profiling](chapters/44-exemplars-tracing-profiling.md)
45. [Prometheus HTTP API、UI 与自动化](chapters/45-prometheus-http-api.md)
46. [部署拓扑、配置管理与 GitOps](chapters/46-deployment-and-gitops.md)
47. [指标治理与平台工程](chapters/47-metric-governance.md)
48. [性能基准、压测与容量验证](chapters/48-benchmarking-and-capacity-validation.md)
49. [合规、数据生命周期与审计](chapters/49-compliance-lifecycle-audit.md)

### 第六部分：综合实战

50. [综合实战（一）：从业务目标到监控设计](chapters/50-capstone-design.md)
51. [综合实战（二）：平台化、压测、演练与复盘](chapters/51-capstone-operations.md)

### 第七部分：商业分析

52. [可观测性市场与竞争格局](chapters/52-observability-market.md)
53. [成本模型与 TCO](chapters/53-cost-and-tco.md)
54. [开源治理、许可与商业模式](chapters/54-open-source-and-business-models.md)
55. [选型、迁移与退出策略](chapters/55-selection-migration-exit.md)
56. [团队、组织与职业路线](chapters/56-team-and-career.md)

## 附录

- [附录 A PromQL 速查](appendices/A-promql-reference.md)
- [附录 B 配置参考](appendices/B-config-reference.md)
- [附录 C 规则库](appendices/C-rule-cookbook.md)
- [附录 D 指标命名与标签规范](appendices/D-metric-naming.md)
- [附录 E 故障排查手册](appendices/E-troubleshooting.md)
- [附录 F 容量模型与估算表](appendices/F-capacity-model.md)
- [附录 G 方案对比](appendices/G-alternatives.md)
- [附录 H 术语表](appendices/H-glossary.md)
- [附录 I 延伸阅读](appendices/I-reading.md)
- [附录 J Mini API 参考](appendices/J-api-reference.md)
- [附录 K 实验手册](appendices/K-labs.md)
- [附录 L 安全基线](appendices/L-security-baseline.md)
- [附录 M Grafana 与可视化](appendices/M-grafana-visualization.md)
- [附录 N Alertmanager 配置参考](appendices/N-alertmanager-config.md)
- [附录 O PromQL 高级函数与查询模式](appendices/O-promql-advanced.md)
- [附录 P Prometheus Operator 与 Kubernetes 生产实践](appendices/P-prometheus-operator.md)
- [附录 Q 长期存储架构：Thanos、Mimir 与 VictoriaMetrics](appendices/Q-long-term-storage.md)
- [附录 R 原生直方图与 Remote Write 2.0 深入](appendices/R-native-histograms-remote-write-2.md)
- [附录 S 生产事故案例集](appendices/S-incident-casebook.md)
- [附录 T Pushgateway 与短生命周期任务](appendices/T-pushgateway.md)
- [附录 U Prometheus 自监控与元监控](appendices/U-self-monitoring.md)
- [附录 V HTTP API 速查](appendices/V-http-api-reference.md)
- [附录 W 客户端埋点示例集](appendices/W-instrumentation-cookbook.md)
- [附录 X 部署清单与配置模板](appendices/X-deployment-templates.md)
- [附录 Y 压测与基准手册](appendices/Y-benchmark-cookbook.md)
- [附录 Z 指标治理与评审清单](appendices/Z-metric-governance-checklist.md)
- [附录 AA Exporter 生态、探针与中间件监控](appendices/AA-exporters-and-probes.md)
- [附录 AB 上游源码导读](appendices/AB-upstream-source-guide.md)
- [附录 AC Prometheus 工具链速查](appendices/AC-tooling.md)
- [附录 AD 自建 Exporter 开发指南](appendices/AD-writing-exporters.md)
- [附录 AE Prometheus 3.14–3.16 新特性与迁移手册](appendices/AE-prometheus-3-14-to-3-16.md)
- [附录 AF Prometheus 3.x UTF-8 指标名与转义完全指南](appendices/AF-utf8-names-and-escaping.md)
- [附录 AG OTLP 接收与 Delta Temporality 实战](appendices/AG-otlp-receiver-and-delta.md)
- [附录 AH 多行业监控指标体系案例库](appendices/AH-industry-monitoring-casebook.md)
- [附录 AI LLM 推理服务监控与成本治理](appendices/AI-llm-inference-monitoring.md)
- [附录 AJ 监控有效性评估与可观测性成熟度](appendices/AJ-monitoring-effectiveness-maturity.md)

## 代码结构

```text
prometheus-tutorial/
  README.md
  VERIFICATION.md
  check_docs.py
  pyproject.toml
  requirements.txt
  chapters/                 56 章
  appendices/               36 篇
  mini_prometheus/
    labels.py exposition.py model.py config.py promtool.py otlp.py
    tsdb/                   chunkenc head wal block tsdb
    promql/                 lexer parser ast engine functions value
    scrape/                 discovery relabel manager
    rules/                  manager notifier
    api/                    server
    remote.py remote_client.py app.py __main__.py
  tests/                    528 个 pytest 用例
  examples/                 10 个场景 + capstone 配置/规则 + demo exporter + alertmanager-lite
```

## 验证

本仓库的验证分三层：

1. **单元/集成**：`python -m pytest`，528 个用例。
2. **示例**：`python examples/run_all.py`，10 / 10 通过（抓取、PromQL、存储、API、告警、远程读写、relabel、stale、规则单元测试、OTLP 接入）。
3. **文档**：`python check_docs.py`，检查章节/链接/YAML/计数/编译。

详细结果、覆盖矩阵与已知缺口见 [VERIFICATION.md](VERIFICATION.md)。

## Mini-Prometheus 的边界

已实现：

- text/OpenMetrics 解析、标签/匹配器、静态/file/http/dns SD、11 种 relabel；
- head + Gorilla 风格 XOR chunk + WAL + checkpoint + block + compaction + retention + tombstone + snapshot；
- PromQL 子集（75 个函数 + 14 个聚合）：rate/increase/irate/histogram_quantile/聚合/匹配/子查询/offset/@/absent/first_over_time/integral 等；
- 抓取失败/stale/honor_labels/honor_timestamps/limits；
- recording/alerting 规则、pending/firing/resolve/keep_firing_for、模板；
- 规则组 `query_offset` 与全局 `rule_query_offset`（查询时间后移、写入时间不变）；
- HTTP API（query/query_range/series/labels/rules/alerts/targets/status/admin）、federation、remote write（JSON/protobuf+Snappy）与 JSON remote read；
- UTF-8 指标名/标签名（PromQL 引号选择器、exposition、序列化）；
- OTLP/HTTP JSON 接收（resource/scope、单位后缀、explicit histogram、summary、delta→cumulative）；
- `promtool test rules` 风格的规则单元测试器（`mini_prometheus/promtool.py`）；
- Alertmanager-lite（去重/分组/抑制示例）。

未实现：

- 生产级性能/百万序列；
- 原生直方图 / OTLP protobuf / OTLP exemplar 持久化 / remote write 2.0；
- Kubernetes/EC2/Consul SD；
- TLS server/basic auth、多租户、HA；
- 完整 Alertmanager（路由/silence/gossip）；
- 完整 promtool 子命令/JUnit 输出（仅实现 `check rules`/`test rules` 子集）；
- 3.16 查询成本估算/上限（`query-cost`）、OpenMetrics 2.0、XOR2 编码；
- 字节级 TSDB 兼容。

这些差异在 [附录 G](appendices/G-alternatives.md) 与第 43 章逐项说明。

## 学习路径

- **只想用**：第 2～12 章 + 第 22～27 章 + 第 50～51 章综合实战 + 附录 E、O、S、AA、AH。
- **做平台**：全部，重点第 13～24、31～51 章 + 附录 P、Q、R、V、X、Y、AA、AB、AC、AD、AE、AF、AG、AH、AI、AJ。
- **读源码**：第 31～43 章，对照 `mini_prometheus/`；再用附录 AB 进入上游代码库。
- **做可观测性/追踪**：第 44 章 + 第 5、12、18、20、41 章 + 附录 R、AG、AI。
- **接 OTel/UTF-8**：第 20、32、37、41 章 + 附录 AF、AG、AI（GenAI 语义约定）。
- **做合规/治理**：第 21、27、47、49 章 + 附录 L、Z、AC、AJ。
- **做选型/商业**：第 18、22、27、52～56 章 + 附录 G、Q。
- **做 SLO/治理**：第 25～27、47、49、50～51 章 + 附录 C、O、S、Z、AH、AJ。
- **做版本升级**：第 20 章 + 附录 AE + 第 23 章（升级/回滚）与第 46 章（GitOps）。

## 约定

- 所有时间戳：毫秒整数（API 返回秒）。
- 所有 PromQL 示例默认按经典命名；UTF-8 语法集中在第 20、32、37、41 章与附录 AF。
- 真实 Prometheus 版本以 3.13 LTS 为基线、兼顾 3.16；原生直方图 3.8 起稳定、3.9 起旧 flag 为 no-op，RW2 仍是 `2.0-rc.4` 实验规范；3.14–3.16 的默认行为变化见第 20 章与附录 AE；3.16 `query-cost` 的端点/配置/上限见第 22 章、附录 AE/V；具体小版本差异以官方 release notes 为准。
- Mini 的行为以本仓库测试为准，不宣称与上游逐字节一致。

## 许可

本教程与 Mini-Prometheus 代码用于教学，按仓库根许可证使用。Prometheus 是 CNCF 项目，Apache License 2.0；文中引用的商标/项目归各自所有者。

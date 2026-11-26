# 第 1 章 为什么需要 Prometheus

## 1.1 从"看机器"到"回答未知问题"

监控系统经常被当成"CPU 使用率面板 + 磁盘告警"。这类需求在单机时代用 `top`、`sar`、Zabbix 就能满足。云原生系统改变了两件事：

1. **实例是短暂的**。Pod 重启后 IP、名字、节点都变了，静态主机清单失效。
2. **问题来自组合**。一次超时可能同时涉及版本、可用区、上游服务、请求参数和实例规格。只按主机聚合的指标无法回答"哪个版本的哪个接口在哪个区域变慢了"。

Prometheus 的核心贡献不是"又一个时序数据库"，而是把**指标身份**建模成一组标签：

```text
http_requests_total{job="api", instance="10.0.0.7:8080", method="GET", code="500", version="v2"}
```

同一个指标名下的任意标签组合是一条独立的时间序列。查询时可以按任意维度聚合、过滤、连接，因此能够回答事先没有建模的问题。这是"多维数据模型"的实际含义，也是 Prometheus 与 RRDtool、Graphite 等路径式指标系统的分水岭。

## 1.2 监控、可观测性与告警

三个概念经常被混用：

- **监控（monitoring）**：采集已知信号，判断系统是否处于预期状态。指标是主战场。
- **可观测性（observability）**：从外部输出推断内部状态，强调未知问题。日志、追踪、profiling 共同参与。
- **告警（alerting）**：把"需要人介入的状态"变成可路由的事件。它依赖监控，但不等同于监控。

Prometheus 处在监控与告警的中心，并通过 exemplar、OpenTelemetry、remote write 与追踪/日志生态连接。它不负责日志检索，也不负责分布式追踪存储；把三者硬塞进一个系统通常会让成本和运维复杂度失控。

## 1.3 为什么是拉取（pull）

Prometheus 默认主动抓取（scrape）目标暴露的 `/metrics`，而不是让应用推送。这个选择带来几个结构性好处：

- **目标清单就是真相**。服务发现告诉 Prometheus "应该有哪些目标"，抓取失败本身就是信号（`up == 0`）。
- **背压天然存在**。Prometheus 按自己的节奏抓取，目标不会因为监控后端故障而堆积内存。
- **调试简单**。`curl http://target:9100/metrics` 就能复现问题，抓取链路短。
- **生命周期清晰**。目标消失时，Prometheus 能写入 staleness 标记，而不是让最后一条推送值永久保留。

代价是：

- 短生命周期任务（批处理、Serverless）可能来不及被抓取，需要 Pushgateway 或 OTLP 推送。
- 防火墙/NAT 后的目标需要反向代理或 agent 模式。
- 抓取间隔决定了采样精度，突发事件可能落在两次抓取之间。

Pushgateway 不是"推模式 Prometheus"，而是**短生命周期任务的最后值缓存**。它不会把推送变成时间序列流，也无法表达"任务已消失"。这一点在设计中必须说清楚，否则会制造永久不消失的假指标。

## 1.4 Prometheus 的边界

Prometheus 擅长：

- 单机可承载的指标采集、存储与查询（典型数十万到数百万活跃序列，取决于内存与磁盘）。
- 基于 PromQL 的聚合、速率、分位数和告警。
- 与 Kubernetes 服务发现深度集成。
- 作为生态的"指标协议与语义标准"：exposition format、remote write、PromQL、alerting rules。

Prometheus 不擅长：

- 长期（年/十年）低成本存储。单机 TSDB 的 retention 通常以天/周计，长期化交给 Thanos、Mimir、VictoriaMetrics 等。
- 日志、追踪、事件的全量存储。
- 强一致的多租户与细粒度权限。单机 Prometheus 没有租户概念。
- 跨集群全局查询。需要联邦或全局视图层。
- 精确计费。采样、去重、保留策略都会影响计数。

把这些边界讲清楚，比宣称"Prometheus 什么都能做"更有价值。

## 1.5 架构一眼

```text
                    ┌────────────────────────────┐
 service discovery  │  static / file / DNS / K8s │
        ───────────►│  relabel                   │
                    └─────────────┬──────────────┘
                                  │ targets
                    ┌─────────────▼──────────────┐
                    │  scrape manager            │
                    │  HTTP + exposition parser   │
                    └─────────────┬──────────────┘
                                  │ samples
                    ┌─────────────▼──────────────┐
                    │  TSDB: head + WAL + blocks │
                    └──────┬──────────────┬──────┘
                           │              │
              ┌────────────▼───┐   ┌──────▼─────────┐
              │ PromQL engine  │   │ recording/alert │
              │ HTTP API / UI  │   │ rules           │
              └────────┬───────┘   └──────┬─────────┘
                       │                  │
                 Grafana/curl      Alertmanager
```

这套教程会把这五层逐一拆开：先讲外部语义（第 1～12 章），再讲内部实现与规模化（第 13～24 章），然后进入 SLO 与工程实践（第 25～30 章），用一个可运行的 Mini-Prometheus 对照实现（第 31～43 章），接着进入生产专题（第 44～49 章：exemplar/追踪、HTTP API、部署/GitOps、指标治理、性能基准、合规与审计），再用两章综合实战把设计、平台化、压测与演练串起来（第 50～51 章），最后讨论商业与组织问题（第 52～56 章）。附录 A～AJ 提供速查、配置、案例、源码导读与评估工具。

## 1.6 为什么还要自己写一个

读懂 Prometheus 的文档不等于理解它。几个典型问题只有写过才清楚：

- 为什么 `rate()` 在样本没有覆盖整个窗口时要外推？
- 为什么删除序列用 tombstone 而不是立刻重写 block？
- 为什么 `up` 在抓取失败时是 0，而目标消失时序列会"消失"？
- 为什么高基数会让内存而不是磁盘先爆？
- 为什么 remote write 要做队列、重试和去重？

Mini-Prometheus 用 Python 标准库 + PyYAML 实现了同样的分层：XOR chunk、WAL、block、compaction、PromQL 解析与执行、抓取、relabel、规则、告警、HTTP API、联邦、remote write/read，以及 `promtool test rules` 风格的规则单元测试。它不追求字节级兼容，也不追求生产性能，但每个关键决策都能在代码里找到对应位置。教程中的数字、行为和失败模式都来自这套实现的实际运行与测试。

## 1.7 学习路径

- **只想用**：第 2～12 章 + 第 22～27 章 + 第 50～51 章 + 附录 E、O、S、AA、AH。
- **要做平台**：全部，重点第 13～24、31～51 章 + 附录 P、Q、R、V、X、Y、AA、AB、AC、AD、AE、AF、AG、AH、AI、AJ。
- **要读源码**：第 13～20、31～43 章，对照 `mini_prometheus/`，再用附录 AB 进入上游代码库。
- **要做可观测性/追踪**：第 44 章 + 第 5、12、18、20 章 + 附录 R、AG、AI。
- **要做选型/商业**：第 18、22、27、52～56 章与附录 G、Q。
- **要做 SLO/治理**：第 25～27、47、49 章 + 附录 C、O、S、Z、AJ。

## 本章小结

- Prometheus 的核心是多维标签模型 + 拉取 + 本地 TSDB + PromQL + 告警，而不是"一个数据库"。
- 拉取模型让目标清单和抓取失败成为一等信号，但不适合所有短生命周期负载。
- Prometheus 有明确边界：长期存储、日志、追踪、多租户、全局查询都需要外部系统。
- 本教程用一个可运行的 Mini-Prometheus 把抽象语义落到代码与测试上。

## 延伸阅读

- Prometheus 官方文档：Overview、Storage、Querying basics。
- Google SRE Book：Monitoring Distributed Systems。
- CNCF 毕业项目页面与 Prometheus 项目历史。
- 本教程第 13 章（架构）与第 31 章（Mini 设计）。

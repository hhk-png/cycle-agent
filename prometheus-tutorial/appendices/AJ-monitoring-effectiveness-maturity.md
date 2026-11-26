# 附录 AJ 监控有效性评估与可观测性成熟度

> 前面大部分章节在回答"怎么把 Prometheus 跑起来、跑稳、跑大"。本附录回答一个更容易被跳过的问题：**你怎么知道现有的指标、告警和仪表盘真的有用？** 一个 `up == 1` 的 Prometheus 只能证明进程活着，不能证明它在用户受影响时给出了可行动的信号。本附录给出一套可执行的评估框架、取证查询、100 分评分卡和两周审计流程，用来把"监控"从项目变成可度量的能力。

## AJ.1 两个不同的问题

监控系统健康和监控有效性经常被混为一谈：

| 问题 | 典型证据 | 回答不了什么 |
|---|---|---|
| Prometheus 是否健康？ | `up == 1`、抓取成功、TSDB 可写、告警可发 | 用户是否受影响时能被发现 |
| 监控是否有效？ | 覆盖、及时、准确、可行动、有 owner | 进程是否 OOM、磁盘是否满 |

一个反例：所有 `up` 都是 1，但关键服务的业务指标没有采集，或者采集了却没有告警，或者告警发了却没人知道该做什么。这时监控系统"健康"，监控能力却可能接近零。

本附录把有效性拆成四层：**数据面、信号面、决策面、组织面**。四层都成立，才算"监控有效"。

## AJ.2 四层评估模型

```text
L1 数据面：采到、采全、采新、采对
      ↓
L2 信号面：异常能被发现，告警准确、及时、可行动
      ↓
L3 决策面：SLO/错误预算/runbook/复盘形成闭环
      ↓
L4 组织面：owner、评审、预算、培训、文化可持续
```

常见误区是只做 L1：堆采集、堆面板、堆指标，却没有 L2 的告警质量评估，也没有 L3 的行动闭环。L1 是必要条件，不是充分条件。

## AJ.3 数据面：采到、采全、采新

### AJ.3.1 覆盖率

覆盖率要分三层看：

1. **目标覆盖率**：服务发现给出的目标中，有多少在成功抓取？
2. **服务覆盖率**：关键服务中，有多少暴露了 RED/USE 指标或业务 SLI？
3. **决策覆盖率**：关键服务中，有多少有 SLO、告警和 runbook？

目标覆盖率的取证查询：

```promql
# 全局抓取成功率（按目标数，不是按样本数）
count(up == 1) / count(up)

# 当前抓取失败的目标
sum by (job) (up == 0)

# 每个 job 的目标数，用来发现"目标突然变少"
count by (job) (up)
```

注意 `up` 只覆盖"已经被 Prometheus 发现并抓取"的目标。服务发现配置错误导致目标根本没被发现时，`up` 不会给出任何信号。要对照服务发现 API、部署清单或 CMDB 做**期望目标数 vs 实际目标数**的核对：

```bash
# 看 Prometheus 认为有哪些目标
curl -s 'http://localhost:9090/api/v1/targets?state=active' \
  | python -m json.tool | head
```

一个可落地的检查是：对每个关键 job 维护"期望实例数"（来自 Deployment/StatefulSet 副本数或服务注册表），用 recording rule 或离线脚本对比 `count(up{job="..."})`。副本数变化时，差值应该可解释。

### AJ.3.2 新鲜度

"有序列"不等于"有新样本"。常见故障是抓取还在成功，但目标返回的是缓存值、时钟漂移，或者 remote write 链路延迟。

```promql
# 每个目标最后一次样本距今多少秒（越大越可疑）
max by (job, instance) (time() - timestamp(up))

# TSDB head 的最大时间戳（毫秒）与当前时间差
time() - prometheus_tsdb_head_max_time / 1000
```

`timestamp()` 返回的是样本时间（秒），不是抓取时间。对于显式时间戳、remote write 或 OTLP 接入的序列，样本时间可能落后于抓取时间，这正是需要观测的。

未来时间戳同样要检查：

```promql
# 样本时间明显超前（时钟漂移或错误时间戳）
timestamp(up) - time() > 60
```

### AJ.3.3 完整性

完整性是"该有的序列是否存在"，而不是"现有序列是否有值"。`absent()` 和 `absent_over_time()` 是主要工具：

```promql
# 关键目标消失
absent(up{job="checkout"})

# 关键指标在 5 分钟内没有样本
absent_over_time(http_requests_total{job="checkout"}[5m])

# 关键 recording rule 没有产出
absent(job:http_requests:rate5m{job="checkout"})
```

注意：`absent()` 对"值为 0"不触发，只对"没有序列"触发。业务低峰期的零流量应该用 `sum(rate(...)) == 0` 单独判断，不能混为一谈。

完整性还包括**指标契约**：每个服务应该暴露哪些指标、哪些标签、什么类型、什么单位。契约可以写在代码里（客户端库注册时校验）、CI 里（exposition 快照测试）或指标目录里（第 47 章）。没有契约，完整性只能靠人记忆。

### AJ.3.4 采样与抓取健康

抓取层的自监控指标是数据面的"体检报告"：

| 指标 | 含义 | 典型用法 |
|---|---|---|
| `up` | 最近一次抓取是否成功 | 目标可用性、告警 |
| `scrape_duration_seconds` | 单次抓取耗时 | 接近 `scrape_timeout` 时预警 |
| `scrape_samples_scraped` | 本次抓到的样本数 | 突降=目标异常；突增=基数漂移 |
| `scrape_series_added` | 本次新增序列数 | 发现标签爆炸/版本发布引入新维度 |
| `scrape_body_size_bytes` | exposition 大小 | 大响应导致超时/带宽 |
| `prometheus_target_scrapes_exceeded_sample_limit_total` | 触发 `sample_limit` | 目标被截断，数据不完整 |
| `prometheus_target_scrapes_sample_out_of_order_total` | 乱序样本 | 时钟/多副本/remote write 问题 |
| `prometheus_target_scrapes_sample_duplicate_timestamp_total` | 重复时间戳 | 多副本或重试 |
| `prometheus_tsdb_head_series` | head 活跃序列数 | 容量与基数趋势 |
| `prometheus_tsdb_compactions_failed_total` | compaction 失败 | 磁盘/权限/损坏 |
| `prometheus_rule_evaluation_failures_total` | 规则求值失败 | 表达式错误/超时 |
| `prometheus_rule_group_last_duration_seconds` | 规则组耗时 | 接近 interval 时预警 |
| `prometheus_notifications_errors_total` | 通知发送失败 | Alertmanager 链路 |
| `alertmanager_notifications_failed_total` | Alertmanager 发送失败 | 渠道/认证/限流 |

几个可直接使用的告警思路：

```promql
# 抓取耗时接近超时（假设 scrape_timeout=10s）
max by (job, instance) (scrape_duration_seconds) > 8

# 抓取样本数骤降（与 1 小时前比）
scrape_samples_scraped
  < 0.5 * (scrape_samples_scraped offset 1h)

# 目标触发 sample_limit
increase(prometheus_target_scrapes_exceeded_sample_limit_total[15m]) > 0

# 规则组耗时超过 interval 的 80%
prometheus_rule_group_last_duration_seconds
  > 0.8 * prometheus_rule_group_interval_seconds
```

最后一条依赖 `prometheus_rule_group_interval_seconds` 是否存在（不同版本/后端可能不同）；没有时用规则组的已知 interval 做常量或 recording rule。

## AJ.4 信号面：告警是否值得相信

### AJ.4.1 四个核心指标

告警质量不能只看"发了多少条"。至少度量：

1. **准确率（precision）**：真实需要行动的告警 / 全部告警。
2. **发现率（recall）**：被告警发现的真实事故 / 全部真实事故。这个最难测，通常靠事故复盘抽样。
3. **及时性**：从故障开始到告警触发的时间（time-to-detect），以及从告警到有人响应的时间（MTTA）。
4. **可行动率**：告警附带明确动作、owner、runbook 的比例。

一个实用口径：

```text
precision = 被标注为 TP 的告警数 / 抽样告警总数
actionable_rate = 有明确动作且有人执行的告警数 / 抽样告警总数
noise_rate = 1 - precision
```

不要追求 100% precision：过度收紧会导致漏报。目标应该按 severity 分层：

| 级别 | 建议 precision | 建议 actionability | 说明 |
|---|---|---|---|
| P1/page | 高（>90%） | 高（>95%） | 半夜叫醒必须有明确动作 |
| P2/ticket | 中（>70%） | 高（>80%） | 允许一定探索性 |
| P3/info | 低 | 中 | 只进看板/日报，不通知人 |

这些数字是起点，不是标准；关键是**持续测量并公开趋势**。

### AJ.4.2 告警审计查询

Prometheus 生成的 `ALERTS` 序列可以回答一部分问题：

```promql
# 当前 firing 的告警按名称计数
count by (alertname) (ALERTS{alertstate="firing"})

# 当前 pending 的告警
count by (alertname) (ALERTS{alertstate="pending"})

# 某告警当前状态已持续的时间（ALERTS_FOR_STATE 记录状态起始时间）
max by (alertname) (time() - ALERTS_FOR_STATE)
```

`ALERTS` 只在告警规则求值时生成，且默认只保留当前状态；要分析历史触发频率，需要长期存储或把告警事件写入事件系统。不要假设 `ALERTS` 是一份完整的告警审计日志。

告警规则本身的健康：

```promql
# 规则组最近一次求值距今多久（秒）
time() - prometheus_rule_group_last_evaluation_timestamp_seconds

# 规则求值失败
increase(prometheus_rule_evaluation_failures_total[1h]) > 0

# 规则组耗时
max by (rule_group) (prometheus_rule_group_last_duration_seconds)
```

### AJ.4.3 抽样方法

每周固定抽 20 条告警（按 severity 分层），由 on-call 或 SRE 标注：

```text
告警名 / 时间 / 目标
是否对应真实用户影响？  TP / FP / 不确定
是否重复？              duplicate / unique
是否有人行动？          action / no-action
动作是否在 runbook 里？ yes / no / 不适用
如果没这条告警，会怎样？ 无影响 / 晚发现 / 漏发现
```

一个月后，你会得到比"告警总数"有用得多的结论：哪些规则长期误报、哪些规则从未触发、哪些规则触发了但没人行动。

事故复盘反向检查同样重要：对每起 P1/P2 事故问三个问题：

1. 有告警吗？没有的话，是缺指标、缺规则、阈值不对，还是路由丢了？
2. 告警及时吗？time-to-detect 与 time-to-ack 各是多少？
3. 告警可行动吗？值班的人当时知道该做什么吗？

### AJ.4.4 告警负载

告警负载要按团队和时段看：

```promql
# 过去 24 小时每个告警的触发次数（需要告警事件历史）
sum by (alertname) (increase(alert_events_total[24h]))
```

`alert_events_total` 不是 Prometheus 内置指标，需要由 Alertmanager webhook、事件管道或自建 exporter 提供。没有事件管道时，用值班记录/工单系统统计。

建议跟踪：

- 每人每周 page 数；
- 夜间（非工作时间）page 数；
- 重复告警占比；
- 平均确认时间（MTTA）；
- 告警到工单的转化率。

page 数不是越低越好，但持续高于团队承受能力时，先治理噪音，再谈加人。

## AJ.5 决策面：从信号到行动

### AJ.5.1 SLO 覆盖率

有指标不等于有 SLO。检查关键服务是否满足：

- 有明确的 SLI（成功率、延迟、可用性、新鲜度等）；
- 有 SLO 目标和时间窗口；
- 有错误预算策略（耗尽时冻结发布/降级/加人力）；
- 有 burn-rate 告警，而不是只对瞬时值告警；
- 有 SLO 评审和复盘记录。

一个最小检查：

```promql
# 30 天成功率（示例：非 5xx 占比）
sum(rate(http_requests_total{code!~"5.."}[30d]))
  /
sum(rate(http_requests_total[30d]))
```

这个查询在真实环境中要注意：30 天窗口的 `rate` 很贵，通常用 recording rule 预聚合；多集群/多副本要去重；`code` 标签的语义要统一。

### AJ.5.2 runbook 与行动闭环

每条 page 级告警至少要有：

- owner（团队/值班组）；
- 影响面（用户/业务/region）；
- 判断步骤（如何确认不是误报）；
- 缓解动作（可复制执行的命令/链接）；
- 升级路径；
- 相关仪表盘/日志/追踪链接。

度量方式：

```text
runbook_coverage = 有 runbook 的 page 级告警 / 全部 page 级告警
stale_runbook_rate = 超过 6 个月未验证的 runbook / 全部 runbook
action_closure_rate = 复盘行动项按期关闭数 / 全部行动项
```

### AJ.5.3 从"看板"到"决策"

仪表盘的价值不是"好看"，而是支持决策：

- 这个面板回答什么问题？
- 看到异常后，下一步做什么？
- 谁负责？
- 多久看一次？
- 如果没人看，是否可以删？

建议给每个关键面板写一行"用途 + owner + 复审日期"。没有用途的面板是技术债。

## AJ.6 组织面：owner、评审与成本

### AJ.6.1 owner 覆盖

指标、告警、仪表盘、SLO 都应该有 owner。最小检查：

```text
metric_owner_coverage = 有 owner 标签/目录条目的指标 / 全部指标
alert_owner_coverage = 有 owner 路由的告警 / 全部告警
dashboard_owner_coverage = 有 owner 的看板 / 全部看板
```

owner 不一定是个人，可以是团队/值班组；但必须能回答"出问题找谁"。

### AJ.6.2 生命周期与退场

监控系统最常见的熵增是"只加不删"：

- 旧服务下线，指标还在抓；
- 旧告警没人删，继续制造噪音；
- 旧面板没人看，继续消耗查询；
- 旧 recording rule 没人维护，继续占 CPU。

治理机制：

- 新指标/告警/面板必须登记 owner、用途、保留期；
- 每季度审计"无 owner、无访问、无告警引用"的资产；
- 下线前通知使用方，保留只读窗口；
- 删除要有记录，避免"删了又加回来"。

### AJ.6.3 成本与人力

有效性不等于"零成本"。要跟踪：

- 每团队/服务的序列数、样本摄入、查询量、存储；
- 每团队/服务的告警负载与 on-call 人力；
- 监控平台自身的运维人力；
- 高成本低价值指标（有存储、无查询、无告警）。

第 27、47、53 章与附录 F、Z 提供了更细的基数、治理和 TCO 方法。

## AJ.7 成熟度模型 L0–L4

| 层级 | 数据面 | 信号面 | 决策面 | 组织面 |
|---|---|---|---|---|
| L0 临时 | 手工抓取/看日志，目标清单不完整 | 出了问题才知道 | 无 SLO/runbook | 无 owner |
| L1 基础 | 核心目标有 `up`/资源指标 | 有静态阈值告警，噪音高 | 有零散 runbook | 平台团队兼管 |
| L2 标准化 | 服务发现、统一命名/标签、自监控 | 告警有 owner/分级/路由 | 关键服务有 SLO | 有评审与生命周期 |
| L3 量化 | 覆盖率/新鲜度/完整性可度量 | precision/recall/MTTA 持续跟踪 | 错误预算驱动发布 | 成本归因与培训 |
| L4 自适应 | 契约/合成探针/自动发现缺口 | 告警质量自动回归，故障注入常态化 | SLO 与业务决策联动 | 组织学习与持续改进 |

判定规则：**四层取最低层**，不是取平均。数据面 L3、组织面 L0 的团队整体仍是 L0/L1，因为能力不可持续。

## AJ.8 100 分评分卡

| 维度 | 分值 | 评分点 |
|---|---|---|
| 数据面 | 25 | 目标覆盖率 10；新鲜度 5；完整性/契约 5；自监控 5 |
| 信号面 | 30 | precision 10；发现率/及时性 5；可行动率 5；runbook 5；告警负载 5 |
| 决策面 | 25 | SLO 覆盖 10；错误预算 5；复盘闭环 5；看板/评审 5 |
| 组织面 | 20 | owner 5；生命周期 5；成本 5；培训/文化 5 |

评分参考：

```text
85–100  领先：可度量、可优化、可持续
70–84   进阶：关键链路闭环，仍有明显缺口
50–69   发展中：有基础，但依赖个人经验
<50     起步：先补覆盖率和 owner，不要急着买工具
```

评分不是目的，**趋势**才是。每季度用同一套口径重评，记录分数变化和原因。

## AJ.9 两周审计流程

### 第一周：取证

| 天 | 动作 | 产出 |
|---|---|---|
| 1 | 盘点 Prometheus/Alertmanager/Grafana/长期存储实例与配置 | 架构图、版本、owner |
| 2 | 盘点目标、job、关键服务、SLO | 目标清单、覆盖率初值 |
| 3 | 跑数据面查询：`up`、新鲜度、`scrape_*`、TSDB/规则健康 | 数据质量缺口清单 |
| 4 | 抽样 20 条告警，标注 TP/FP/action | 告警质量表 |
| 5 | 抽取 3–5 起近期事故，做时间线复盘 | 漏报/迟报/误报模式 |

### 第二周：验证与规划

| 天 | 动作 | 产出 |
|---|---|---|
| 6 | 访谈 on-call、服务 owner、平台团队 | 组织/流程问题清单 |
| 7 | 检查 runbook、面板、SLO、错误预算政策 | 决策面缺口清单 |
| 8 | 做一次故障注入/红蓝演练，观察发现与响应 | 端到端有效性证据 |
| 9 | 按四层模型打分，计算 100 分评分卡 | 评分与差距 |
| 10 | 制定 90 天改进路线图，明确 owner/指标 | 改进计划 |

审计输出不要写成"问题大全"，要写成**可执行的改进项**：每项有 owner、截止时间、验证方式、影响。否则审计本身也会变成技术债。

## AJ.10 常见反模式

- **全绿面板**：只看 `up` 和资源利用率，没有用户/业务信号；系统"健康"，用户已经在流失。
- **缺数据 = 健康**：`absent` 没处理，序列消失被当成"没有问题"。
- **告警数量当 KPI**：告警越多不代表监控越好，可能只是阈值太敏感或没人治理。
- **平均值 SLO**：用平均延迟做 SLO，长尾用户已经被伤害，平均值仍然漂亮。
- **告警无 owner/runbook**：发了没人知道做什么，最后演变成"看到就忽略"。
- **只测 P1，不看 near miss**：没有造成事故的险情同样暴露监控缺口。
- **看板只加不删**：查询成本和认知负担持续上升。
- **把 Prometheus 自监控当业务监控**：`up`、TSDB、规则健康是必要条件，不是业务 SLI。
- **监控项目化**：上线时做一轮，之后没有 owner、没有预算、没有复审。
- **用工具替代流程**：买了 APM/可观测性平台，却没有 SLO、owner 和复盘机制。

## AJ.11 与教程章节的映射

| 评估层 | 相关章节/附录 |
|---|---|
| 数据面 | 第 11～12 章（SD/抓取）、第 14～16 章（TSDB）、第 22 章（容量）、第 47 章（治理）、附录 U（自监控）、附录 AA（exporter） |
| 信号面 | 第 9～10 章（规则/Alertmanager）、第 25～26 章（SLO/告警工程）、第 28 章（测试/混沌）、附录 N（Alertmanager 配置）、附录 S（事故案例） |
| 决策面 | 第 25 章（SLO/错误预算）、第 26 章（告警工程）、第 47 章（治理）、第 49 章（合规/审计） |
| 组织面 | 第 47 章（治理）、第 52～56 章（市场/成本/团队）、附录 Z（治理清单） |
| 综合实战 | 第 50～51 章（设计、平台化、压测、演练、复盘） |

## AJ.12 检查清单

### 数据面

- [ ] 每个关键 job 有期望目标数 vs 实际 `up` 的对比。
- [ ] 关键服务有 RED/USE 或业务 SLI 指标。
- [ ] `absent`/`absent_over_time` 覆盖关键指标和 recording rule。
- [ ] 抓取耗时、样本数、序列新增有告警或看板。
- [ ] `sample_limit`、乱序、重复时间戳有监控。
- [ ] TSDB compaction、WAL、head 序列数有监控。
- [ ] 规则求值失败、耗时、最后求值时间有监控。
- [ ] 通知发送失败/延迟有监控。

### 信号面

- [ ] 每条 page 级告警有 owner、severity、runbook。
- [ ] 告警路由/分组/抑制/静默经过测试。
- [ ] 每周抽样标注告警 TP/FP/action。
- [ ] 每起 P1/P2 事故复盘检查"为什么没告警/为什么晚"。
- [ ] 告警负载按团队/时段统计。
- [ ] 长期未触发/长期 firing 的告警有复审。
- [ ] 告警规则有单元测试（promtool test rules 或等价）。
- [ ] 关键告警有故障注入验证。

### 决策面

- [ ] 关键服务有 SLI/SLO/错误预算。
- [ ] 有 burn-rate 多窗口告警。
- [ ] 错误预算耗尽有明确政策。
- [ ] 每个关键面板有用途/owner/复审日期。
- [ ] runbook 可在 5 分钟内被值班人执行。
- [ ] 复盘行动项有 owner/截止时间/验证。
- [ ] SLO 与发布/容量/on-call 流程联动。

### 组织面

- [ ] 指标/告警/面板有 owner。
- [ ] 新资产有登记/评审/保留期。
- [ ] 每季度审计孤儿资产并退场。
- [ ] 成本按团队/服务可见。
- [ ] on-call 有培训/交接/疲劳管理。
- [ ] 平台提供 paved road，而不是只靠审批。
- [ ] 监控有效性评分每季度重评并公开趋势。
- [ ] 有预算/人力支撑持续改进。

## AJ.13 延伸阅读

- Google SRE Book/Workbook：Monitoring Distributed Systems、Alerting on SLOs、Postmortem Culture。
- Prometheus 官方文档：Instrumentation、Alerting、Metric and label naming。
- 本教程第 25～28、47、49～51 章，附录 S（事故案例）、U（自监控）、Z（治理清单）。
- 附录 AI（LLM 推理监控）中的质量/成本指标，可作为"业务 SLI"的扩展案例。

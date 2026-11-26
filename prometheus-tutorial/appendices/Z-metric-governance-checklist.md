# 附录 Z 指标治理与评审清单

本附录是第 47 章的配套工具。可以直接复制到内部 Wiki/PR 模板/CI 规则中。

## Z.1 指标目录条目模板

```yaml
metric: myapp_http_requests_total
type: counter
unit: requests
owner: team-api
status: active
description: Total HTTP requests handled by the API.
labels:
  method:
    values: [GET, POST, PUT, DELETE]
    max_cardinality: 4
  route:
    values: [route-template-only]
    max_cardinality: 50
  code:
    values: ["2xx", "4xx", "5xx"]
    max_cardinality: 3
cardinality_budget: 600
scrape_interval: 30s
retention: 30d
slo: api-availability
dashboards:
  - api-overview
runbooks:
  - api-high-error-ratio
deprecated_by: ""
created_at: "2026-11-26"
```

字段说明：

- `owner` 必须是团队/值班组，不是个人邮箱；
- `max_cardinality` 是预算，不是观测值；
- `status` 取 `active`/`deprecated`/`experimental`；
- `deprecated_by` 指向替代指标；
- `retention` 要与实际存储策略一致。

## Z.2 新指标评审表

```text
指标名：
owner：
目的（回答什么问题）：
关联 SLO/告警/容量决策：
类型（counter/gauge/histogram/summary）：
单位：
标签及取值域：
预计序列数（标签组合 × 目标数）：
抓取间隔/目标数：
存储/保留：
面板/告警/runbook：
是否包含敏感信息：
替代/重叠的现有指标：
退场条件：
测试（exposition/规则/基数）：
评审人/日期：
```

评审时必须问：

1. 不用这个指标会怎样？
2. 能否用现有指标 + recording rule 派生？
3. 标签值是否可能来自用户输入？
4. 序列数上限是多少？谁负责？
5. 出问题时看哪个面板/告警？
6. 什么时候删？

## Z.3 命名与标签检查

- [ ] 指标名匹配 `<namespace>_<subsystem>_<name>_<unit>[_total]`？
- [ ] counter 以 `_total` 结尾，gauge 不以 `_total` 结尾？
- [ ] 单位是基本单位（秒/字节/米/伏特），没有 `_milliseconds`/`_kilobytes`？
- [ ] 没有把版本号写进指标名（用 `version` 标签）？
- [ ] 标签名 snake_case，没有 `__` 前缀（除内部标签）？
- [ ] 标签值有界、可枚举，没有 `user_id`/`request_id`/`session_id`/完整 URL/错误文本？
- [ ] 同一指标的所有样本标签集合一致？
- [ ] histogram 有 `+Inf` 桶，桶覆盖 SLO？
- [ ] summary 没有被跨实例聚合？
- [ ] 指标名/标签没有敏感信息？

## Z.4 基数预算

计算：

```text
series = targets × metric_variants × label_combinations
```

为每个指标设预算：

```text
budget = expected_series × 1.5   # 留 50% 余量
```

监控：

```promql
# 按指标名的序列数（需要 metadata/recording rule 或 TSDB stats）
topk(20, count by (__name__) ({__name__=~".+"}))

# 新序列速率
rate(prometheus_tsdb_head_series_created_total[5m])

# 抓取新增序列
scrape_series_added > 1000

# 单 job 的样本数异常
scrape_samples_scraped
```

注意：`{__name__=~".+"}` 全匹配查询代价极高，只应在受控环境/离线分析使用；生产优先用 `/api/v1/status/tsdb`、recording rules、`promtool tsdb analyze`。

## Z.5 指标生命周期检查

### Z.5.1 新增

- [ ] 有 owner、目录条目、预算？
- [ ] 有面板/告警/runbook 或明确"仅排障"？
- [ ] 有 exposition/规则测试？
- [ ] 有退场条件？

### Z.5.2 变更

- [ ] 指标名/标签/单位变更是否破坏现有查询？
- [ ] 是否需要双写/兼容期？
- [ ] 面板/告警/目录是否同步？
- [ ] 是否通知使用方？

### Z.5.3 废弃

- [ ] 标记 `deprecated` 并通知 owner/使用方？
- [ ] 找全使用方：Grafana、告警、API 客户端、报表？
- [ ] 提供替代指标？
- [ ] 双写/双读一段时间？
- [ ] 停止暴露？
- [ ] 从目录删除？
- [ ] 清理历史（retention/delete_series，谨慎）？

## Z.6 孤儿指标审计

孤儿指标：无 owner、无面板、无告警、无查询。

审计步骤：

1. 从 `/api/v1/status/tsdb` 或 `promtool tsdb analyze` 拿指标/序列清单；
2. 与目录、Grafana、告警规则、查询日志比对；
3. 标记"无使用"候选；
4. 观察一个保留周期（如 30 天）；
5. 通知 owner；
6. 停止暴露/删除。

不要直接删除不认识的指标；可能是外部团队/合规/计费在用。

## Z.7 告警治理检查

- [ ] 每个告警有 owner、severity、runbook？
- [ ] 告警是症状导向，不是纯原因/纯资源？
- [ ] `for` 足够长，避免抖动？
- [ ] 有 `keep_firing_for`/恢复语义？
- [ ] 告警有测试（promtool test rules/Mini promtool）？
- [ ] 告警路由到正确的团队？
- [ ] 有抑制/静默/分组策略？
- [ ] 定期评审告警 precision/噪音？
- [ ] 删除长期不触发/不可执行的告警？

## Z.8 成本与配额

- [ ] 按团队/服务/指标统计序列数/样本数/存储？
- [ ] 有预算/配额？
- [ ] 超预算有告警/通知？
- [ ] 成本分摊规则公开？
- [ ] 高成本指标有评审/优化记录？
- [ ] 保留期与合规/排障需求匹配？
- [ ] 有降采样/聚合/长期存储策略？

## Z.9 数据质量检查

| 检查 | 查询/方法 | 期望 |
|---|---|---|
| 抓取失败 | `up == 0` | 有告警/解释 |
| 目标消失 | `/api/v1/targets` dropped | 符合预期 |
| 样本骤降 | `scrape_samples_scraped` | 无异常跌落 |
| 新序列突增 | `scrape_series_added` | 在预算内 |
| append 错误 | duplicate/out-of-order 指标 | 接近 0 |
| 时钟偏差 | 乱序/未来样本 | 无 |
| 类型错误 | exposition TYPE/代码 review | 一致 |
| 单位错误 | 目录/命名 | 一致 |
| 语义漂移 | 目录/owner/review | 有记录 |

## Z.10 治理评分卡

按季度打分（0～5）：

| 维度 | 问题 | 分数 |
|---|---|---|
| 可发现 | 有目录/owner 的指标比例 | |
| 可信 | 类型/单位/标签一致比例 | |
| 可负担 | 预算内团队比例 | |
| 可退场 | 有废弃流程/最近删除记录 | |
| 告警 | precision/runbook 覆盖率 | |
| 成本 | 单位业务成本趋势 | |
| 自动化 | CI 校验/自助接入覆盖率 | |
| 组织 | owner/RACI 明确 | |

低分项进入下一季度改进计划，不要只做"规范文档"。

## Z.11 RACI 模板

| 活动 | 平台 | 服务团队 | SRE | 安全 | FinOps |
|---|---|---|---|---|---|
| 指标规范 | A/R | C | C | C | I |
| 新指标评审 | C | A/R | I | C | I |
| 指标目录 | A/R | R | I | I | I |
| 告警规则 | C | A/R | C | I | I |
| SLO | C | A/R | C | I | I |
| 容量规划 | A/R | C | C | I | C |
| 成本分摊 | A/R | C | I | I | C |
| 安全基线 | C | C | I | A/R | I |
| 废弃/删除 | C | A/R | I | C | I |

## Z.12 反模式清单

- 规范只写文档，没有 CI/评审。
- 用 relabel 暴力删除标签，破坏排障维度。
- 所有指标都做 recording rule，规则爆炸。
- 用全匹配查询做审计/报表。
- 指标名前缀绑定团队名，组织调整后无法迁移。
- 没有 owner，没人敢删。
- 告警只增不减。
- 治理变成审批瓶颈，团队绕过平台。
- 成本只按总量看，无法归因。
- 目录与运行时不一致，没人维护。

## Z.13 30/60/90 天治理计划

### 30 天

- 盘点指标/序列/owner；
- 建立目录模板；
- 找出 Top 20 高基数指标；
- 给关键告警补 owner/runbook；
- CI 加 promtool check rules。

### 60 天

- 新指标评审流程上线；
- 基数预算/告警；
- 成本按团队 showback；
- 规则单元测试覆盖关键告警；
- 废弃第一批孤儿指标。

### 90 天

- 配额/chargeback（如需要）；
- 自助接入模板；
- 季度评分卡；
- 告警 precision 评审；
- 容量/成本优化复盘。

## 延伸阅读

- Prometheus 官方 "Metric and label naming"、"Instrumentation"。
- Google SRE Workbook：SLO/告警/容量。
- FinOps Foundation：showback/chargeback。
- 本教程第 27 章（基数）、第 47 章（治理）与附录 D。

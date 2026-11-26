# 第 47 章 指标治理与平台工程

## 47.1 指标会腐烂

一个组织刚开始用 Prometheus 时，指标通常很少：

```text
up
http_requests_total
process_cpu_seconds_total
```

两年后，可能变成：

- 每个团队自定义命名，`http_requests_total`、`http_request_count`、`requests_total` 并存；
- 标签值来自用户输入，序列数悄悄涨到千万；
- 指标没人负责，服务下线了指标还在；
- 同名指标在不同服务里语义不同；
- 告警表达式复制粘贴，没人知道为什么是这个阈值；
- 成本报表按团队拆不出来，因为标签里没有 owner。

这不是技术问题，而是**治理问题**。指标治理的目标不是"限制团队"，而是让指标：

- **可发现**：知道有哪些指标、谁负责、什么意思；
- **可信**：类型/单位/标签语义一致；
- **可负担**：基数/存储/查询成本可控；
- **可退场**：废弃指标能安全删除。

## 47.2 指标生命周期

把指标当成产品，而不是"埋点副作用"：

```text
提案 → 设计评审 → 实现/暴露 → 注册/目录 → 使用 → 废弃 → 删除
```

### 47.2.1 提案

新增指标前回答：

- 它回答什么业务/运维问题？
- 现有指标能不能回答？能不能用 recording rule 派生？
- 类型是什么（counter/gauge/histogram）？
- 标签有哪些？每个标签的基数上限？
- 单位是什么？
- 谁负责？告警/面板在哪里？
- 保留多久？

### 47.2.2 设计评审

评审重点不是"名字好不好听"，而是：

- 基数预算；
- 聚合维度；
- 与现有指标的重叠；
- 是否泄露敏感信息；
- 是否有对应的 SLO/告警/面板；
- 是否有退场计划。

### 47.2.3 实现与暴露

- 用客户端库，不手工拼文本；
- 单位进名字（`_seconds`、`_bytes`）；
- counter 以 `_total` 结尾；
- histogram 桶覆盖 SLO；
- 标签值有界；
- 不要在 `/metrics` 里做慢查询。

### 47.2.4 注册与目录

指标目录至少包含：

```yaml
metric: http_requests_total
type: counter
unit: requests
owner: team-api
description: Total HTTP requests handled by the API.
labels:
  method: [GET, POST, PUT, DELETE]
  route: bounded route template
  code: [200, 400, 401, 403, 404, 500, 502, 503]
slo: api-availability
dashboards:
  - api-overview
runbooks:
  - api-high-error-ratio
retention: 30d
```

目录可以是 YAML/JSON、Backstage 实体、数据目录或内部 Wiki；关键不是工具，而是**有唯一真相**。

### 47.2.5 使用

- 面板/告警引用目录中的指标；
- 新查询优先用 recording rules/聚合指标；
- 禁止在面板里写全匹配 `{__name__=~".+"}`；
- 查询要有时间范围和标签过滤。

### 47.2.6 废弃与删除

废弃流程：

1. 标记 `deprecated`，通知 owner/使用方；
2. 找使用方：Grafana、告警规则、API 客户端、成本报表；
3. 提供替代指标；
4. 双写/双读一段时间；
5. 停止暴露；
6. 从目录删除；
7. 用 retention/delete_series 清理历史（谨慎）。

没有退场机制的指标治理，最后只会不断增加。

## 47.3 命名、标签与单位规范

### 47.3.1 命名

推荐：

```text
<namespace>_<subsystem>_<name>_<unit>[_total]
```

例子：

```text
myapp_http_requests_total
myapp_http_request_duration_seconds
myapp_queue_depth
myapp_cache_hits_total
```

规则：

- 基本单位：秒、字节、米、伏特；不要 `_milliseconds`、`_kilobytes`；
- counter `_total`；gauge 不加；
- histogram/summary 的 `_bucket`/`_sum`/`_count` 由客户端生成；
- 不要把版本号写进指标名，用 `version` 标签；
- 不要用用户输入/URL/错误文本做指标名或标签值。

### 47.3.2 标签

标签是身份，不是元数据垃圾桶：

- 值应有界、可枚举；
- 不要放 `user_id`、`request_id`、`session_id`、`timestamp`、完整 URL、SQL、错误堆栈；
- `instance`/`pod` 已经是高基数，不要再叠加 `pod_ip`/`node_ip`；
- 同一指标的所有样本应有一致的标签集合；
- 标签名用 snake_case；
- 保留 `__` 前缀给内部标签。

### 47.3.3 单位与类型

- 类型错误比命名错误更危险：gauge 当 counter 用会让 `rate()` 失真；
- histogram 必须有 `+Inf` 桶；
- summary 分位数不可跨实例聚合；
- 单位不要混用（秒/毫秒、字节/比特）。

### 47.3.4 自动检查

- `promtool check metrics` 可以检查 exposition 的格式/命名/单位问题；
- CI 可以扫描代码里的指标定义，禁止可疑标签名；
- 服务启动时校验标签值集合（枚举）；
- 抓取侧用 `metric_relabel_configs` 做最后一道防线，但不要依赖它治理所有问题。

## 47.4 指标目录与元数据

Prometheus 自带 `/api/v1/metadata`，但它只反映"最近抓取到的 HELP/TYPE/UNIT"，不包含 owner、SLO、面板、runbook。生产需要外部目录。

目录的最小字段：

| 字段 | 作用 |
|---|---|
| `metric` | 指标名 |
| `type` | counter/gauge/histogram/summary |
| `unit` | 基本单位 |
| `owner` | 负责团队/个人 |
| `description` | 业务含义 |
| `labels` | 标签及取值域/基数上限 |
| `slo` | 关联 SLO |
| `dashboards` | 面板链接 |
| `runbooks` | 排障/告警手册 |
| `retention` | 保留期 |
| `status` | active/deprecated |

目录的同步方式：

- 从代码/客户端定义生成；
- 从 Prometheus metadata API 导入；
- 人工维护 + CI 校验；
- 与 Backstage/服务目录集成。

无论哪种，都要有**唯一 owner** 和**最后更新时间**。

## 47.5 评审流程与模板

一个可执行的指标评审模板：

```text
1. 目的：回答什么问题？关联哪个 SLO/告警/容量决策？
2. 类型：counter/gauge/histogram/summary；为什么？
3. 标签：列出每个标签的取值域和基数上限；总序列数估算。
4. 单位：基本单位；是否与现有指标一致？
5. 采集：抓取间隔、目标数、exporter 位置。
6. 存储：保留期、是否 remote write、是否降采样。
7. 使用：面板、告警、runbook、owner。
8. 安全：是否包含敏感信息？
9. 退场：什么条件下删除？替代指标是什么？
10. 测试：规则单元测试、exposition 测试、基数压测。
```

评审会不必很长，但要有人对基数和 owner 签字。

## 47.6 平台能力：让正确的事更容易

治理不能只靠"禁止"，平台要提供 paved road：

- **自助接入**：模板化 exporter/ServiceMonitor，自动加 `cluster`/`namespace`/`team` 标签；
- **默认 relabel**：删除高基数/敏感标签，统一 `instance`；
- **规则库**：常用 recording/alerting 规则模板，带测试；
- **面板即代码**：Grafana dashboard JSON 进 Git，CI 校验；
- **告警模板**：统一 severity、runbook、路由；
- **CI 校验**：promtool、规则测试、基数估算、命名 lint；
- **成本可见**：按团队/服务/指标的用量报表；
- **沙箱**：新指标先在低保留/低配额环境验证。

平台团队的角色是"降低正确做法的成本"，而不是"审批每一个指标"。

## 47.7 多团队、配额与成本分摊

多团队环境的常见问题：

- 一个团队的高基数指标拖垮所有人的 Prometheus；
- 成本无法归因，没人愿意优化；
- 告警路由到错误的团队；
- 全局标签不统一，无法跨团队聚合。

手段：

- **命名空间/租户标签**：`team`、`namespace`、`service`；
- **配额**：按团队的序列数/样本数/查询并发限制（需要网关/多租户后端）；
- **showback/chargeback**：按序列/样本/存储/查询量分摊；
- **预算告警**：团队序列数/成本超过预算时通知；
- **共享规则库**：避免每个团队重复造轮子；
- **变更评审**：高基数/高成本指标需要平台团队评审。

注意：单机 Prometheus 没有真正的租户隔离；配额/计费通常要在采集侧（relabel/agent）或后端（Mimir/VictoriaMetrics/云托管）实现。

## 47.8 数据质量

常见数据质量问题：

| 问题 | 现象 | 根因 | 检查 |
|---|---|---|---|
| 缺失 | 曲线有缺口 | 抓取失败、stale、relabel 丢弃 | `up`、`scrape_samples_scraped`、targets |
| 重复 | 同序列多值/冲突 | 多副本、remote write 重试、标签不一致 | duplicate/out-of-order 指标 |
| 类型错 | `rate()` 异常 | gauge 当 counter | exposition TYPE、代码 review |
| 单位错 | 数值差 1000 倍 | 毫秒/秒、字节/比特 | 命名/目录 |
| 标签漂移 | 序列数突增 | 新版本引入标签/用户输入 | `scrape_series_added`、TSDB stats |
| 时钟偏差 | 乱序/未来样本 | NTP、显式时间戳 | append 错误、`honor_timestamps` |
| 语义漂移 | 同名指标含义变了 | 无目录/无 owner | 目录、review |

数据质量不是"监控的监控"，而是平台的一部分：

- 对关键指标做合成探针；
- 对 `up`、`scrape_samples_scraped`、`scrape_series_added` 告警；
- 定期审计无 owner/无面板/无告警的"孤儿指标"；
- 对 exposition 做契约测试。

## 47.9 治理指标

治理本身要可度量：

- **覆盖率**：有 owner 的指标比例；有 SLO 的关键服务比例；有 runbook 的告警比例。
- **基数**：每团队/服务的活跃序列数、增长率、预算使用率。
- **成本**：每团队/服务的存储/查询成本、单位业务成本。
- **质量**：抓取失败率、append 错误率、孤儿指标数、过期指标数。
- **告警**：告警数量、precision（真实有效比例）、MTTA/MTTR、噪音率。
- **变更**：指标新增/废弃数、规则测试覆盖率。

这些指标可以放在一个"平台健康"面板里，按季度评审。如何把覆盖率、告警质量、SLO 闭环和组织能力合成一套可打分的评估体系，见附录 AJ（监控有效性评估与可观测性成熟度）。

## 47.10 组织与 RACI

典型角色：

- **平台团队**：Prometheus/存储/查询层/规则库/工具链/容量。
- **服务团队**：自己的指标设计、exporter、SLO、告警、runbook。
- **SRE/On-call**：告警响应、事故复盘、容量评审。
- **安全/合规**：数据分类、访问控制、保留/审计。
- **财务/FinOps**：成本分摊、预算。

RACI 示例：

| 活动 | 平台 | 服务团队 | SRE | 安全 |
|---|---|---|---|---|
| 指标规范 | A/R | C | C | C |
| 新指标评审 | C | A/R | I | C |
| 告警规则 | C | A/R | C | I |
| 容量规划 | A/R | C | C | I |
| 成本分摊 | A/R | C | I | I |
| 安全基线 | C | C | I | A/R |

（A=负责批准，R=执行，C=咨询，I=知会。）

治理失败常见于"平台团队全责、服务团队不参与"：指标是服务的一部分，owner 必须在服务团队。

## 47.11 反模式

- **只有命名规范，没有评审/目录/退场**：规范会过期。
- **用 relabel 删除所有高基数标签**：可能删掉排障所需维度；先从源头修。
- **把所有指标都做成 recording rule**：规则数量爆炸，查询反而更慢。
- **用 `{__name__=~".+"}` 做审计/报表**：代价极高，可能拖垮 Prometheus。
- **按团队硬编码指标名前缀**：团队改名/合并后指标无法迁移。
- **没有 owner 的指标**：没人敢删，成本只增不减。
- **告警只加不删**：告警疲劳，最终没人看。
- **把治理做成审批瓶颈**：团队绕过平台，影子监控出现。

## 47.12 检查表

- [ ] 每个指标有 owner、类型、单位、描述、保留期？
- [ ] 标签值有界，基数有预算？
- [ ] counter/gauge/histogram 类型正确？
- [ ] 有指标目录，且与代码/运行时同步？
- [ ] 新指标有评审模板和测试？
- [ ] 废弃指标有通知、替代、删除流程？
- [ ] 有按团队/服务的成本可见性？
- [ ] 有孤儿指标/高基数/抓取失败的定期审计？
- [ ] 告警有 owner、runbook、severity、路由？
- [ ] 平台提供自助接入和默认防护，而不是只靠禁止？

## 本章小结

- 指标治理的目标是可发现、可信、可负担、可退场。
- 指标有生命周期：提案、评审、实现、目录、使用、废弃、删除。
- 命名/标签/单位规范要配合自动检查和评审，不能只写在文档里。
- 目录要有 owner、SLO、面板、runbook、保留期；运行时 metadata 不够。
- 平台要提供 paved road、配额/成本可见性和共享规则库。
- 数据质量要靠 `up`/scrape 指标、append 错误、合成探针和契约测试。
- 治理本身要度量：覆盖率、基数、成本、质量、告警效果。
- owner 必须在服务团队；平台团队负责工具和标准，不替业务拥有指标。

## 延伸阅读

- Prometheus 官方文档 "Metric and label naming"、"Instrumentation"。
- Google SRE Workbook：SLO、告警、容量。
- FinOps Foundation：成本分摊与单位经济性。
- 本教程第 5 章（exposition）、第 27 章（基数）、第 28 章（测试）、第 49 章（合规/生命周期）与附录 D/Z/AJ。

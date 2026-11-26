# 第 29 章 迁移与兼容：从旧监控到 Prometheus

## 29.1 迁移不是"换存储"

一次监控迁移涉及：

- 指标模型（名称/标签/类型）；
- 采集方式（agent/推送/拉取）；
- 查询语言（PromQL）；
- 仪表盘；
- 告警规则/路由；
- 历史数据；
- 权限/多租户；
- 运维流程。

只迁移存储，通常会得到一个"能查但不好用"的系统。

## 29.2 迁移步骤

```text
1. 盘点：现有指标/面板/告警/用户
2. 建模：新指标名/标签/类型
3. 双写/双跑：旧系统 + Prometheus 并行
4. 查询/面板迁移
5. 告警迁移/演练
6. 切读
7. 停旧系统
8. 复盘
```

每一步都要有验收标准。

## 29.3 从 Graphite/StatsD 迁移

Graphite 路径：

```text
prod.api.http.requests.200
```

映射：

```text
http_requests_total{env="prod",service="api",code="200"}
```

注意：

- 路径层级 → 标签，但不要把所有层级都变成标签（基数）；
- StatsD 的 timer 通常只有聚合值，没有原始分布；用 Histogram 重新采集；
- StatsD 的 counter 可能是"每 flush 增量"，Prometheus counter 是累计值；需要客户端累计；
- 命名规范化（单位/后缀）。

## 29.4 从 InfluxDB 迁移

Influx 的 measurement/tag/field：

```text
measurement: http_requests
tags: service, code
fields: count, duration
```

映射：

```text
http_requests_total{service="api",code="200"}
http_request_duration_seconds_bucket{service="api",le="0.1"}
```

注意：

- Influx field 可能是多值；Prometheus 每样本一个 float；
- Influx 的 retention/continuous query 与 recording rules 对应；
- InfluxQL → PromQL 需要重写，不是自动翻译。

## 29.5 从 Zabbix/Nagios 迁移

- 主机/服务检查 → exporter + `up`；
- 触发器 → 告警规则；
- 模板 → 抓取配置/relabel；
- 趋势数据 → 历史迁移（可选）；
- 事件/确认 → Alertmanager silence/route。

难点：

- 检查脚本需要改成 exporter；
- 告警语义（症状/原因）需要重新设计；
- 维护窗口 → silence。

## 29.6 从 Datadog/商业 APM 迁移

- 指标名/标签映射；
- dashboard 查询翻译；
- 告警/合成监控迁移；
- 历史数据通常无法完整导出；
- 成本对比（序列/主机/自定义指标）。

建议：

- 先迁移"基础设施 + 核心 SLO"；
- 商业 APM 的追踪/日志能力保留或替换；
- 用双跑验证关键告警；
- 评估退出成本（合同/数据导出）。

## 29.7 历史数据迁移

选项：

1. **不迁移**：新系统从切换点开始，旧系统只读保留；
2. **remote write 回填**：把旧数据转成 Prometheus 样本写入远端；
3. **快照/导入工具**：Prometheus snapshot/`promtool tsdb create-blocks-from`（部分版本）；
4. **对象存储归档**：Thanos/Mimir 的长期存储。

注意：

- 时间戳/标签必须与现有序列一致；
- 乱序/重复会被拒绝；
- 回填会消耗大量带宽/CPU；
- 历史数据的价值要评估（通常只保留聚合）。

## 29.8 PromQL 翻译

没有通用翻译器；常见映射：

```text
Graphite: sumSeries(prod.api.*.requests) → sum({__name__="requests",env="prod",service=~"api"})
InfluxQL: SELECT mean("value") ... GROUP BY time(5m) → avg_over_time / avg by
Datadog: avg:system.cpu.user{*} by {host} → avg by (host) (system_cpu_user)
```

要点：

- 先理解原查询的语义（平均/求和/分位数/窗口）；
- 注意 counter vs gauge；
- 注意标签匹配；
- 用 recording rules 固化常用查询。

## 29.9 告警迁移

- 逐条映射表达式；
- 确认 `for`/阈值/严重级别；
- 确认路由/抑制/静默；
- 双跑：旧告警和新告警同时发到测试渠道；
- 对比触发时间/标签/内容；
- 切换前演练。

## 29.10 兼容性检查

- 指标名/标签是否符合 Prometheus 规范；
- 类型是否正确（counter/gauge/histogram）；
- 是否有高基数标签；
- exposition 格式是否合法；
- 抓取间隔/超时是否合理；
- 是否有重复序列；
- 是否有 stale 处理。

用 `promtool check metrics`、Mini 的 `proxy` 或抓取解析器做预检。

## 29.11 迁移反模式

- 只迁移存储，不迁移语义；
- 把所有旧标签原样搬过来（基数爆炸）；
- 直接翻译查询不求甚解；
- 不双跑就切告警；
- 不评估历史数据价值；
- 没有回滚计划。

## 29.12 迁移 runbook 模板

把迁移拆成有明确验收标准的阶段：

```text
阶段 0：盘点
  - 指标/面板/告警/用户/数据量
  - 验收：清单完整、owner 确认

阶段 1：建模与试点
  - 命名/标签/类型规范
  - 选 1～2 个服务试点
  - 验收：试点面板/告警可用

阶段 2：双跑
  - 旧系统 + 新系统并行
  - 验收：关键 SLI 误差在阈值内

阶段 3：切读
  - Grafana 数据源切换
  - 验收：用户无感、查询延迟可接受

阶段 4：切告警
  - 告警路由切换
  - 验收：无漏报/误报，on-call 确认

阶段 5：停旧
  - 保留只读/归档
  - 验收：回滚窗口结束、数据保留满足合规

阶段 6：复盘
  - 成本/可靠性/用户反馈
```

每个阶段要有：

- 负责人（DRI）；
- 开始/结束条件；
- 回滚触发条件；
- 沟通渠道与时间点；
- 证据（截图/查询/工单）。

## 29.13 指标映射表与自动化

用一张表驱动迁移，而不是靠记忆：

| 旧指标/路径 | 新指标名 | 类型 | 标签映射 | 备注 |
|---|---|---|---|---|
| `prod.api.http.requests.200` | `http_requests_total` | counter | `env=prod,service=api,code=200` | 路径层级转标签 |
| `prod.api.latency.p90` | `http_request_duration_seconds` | histogram | `service=api` | 旧值是分位数，需重采 |
| `system.cpu.user` | `node_cpu_seconds_total` | counter | `mode=user` | 单位/语义变化 |
| `queue.size` | `queue_depth` | gauge | `queue=orders` | 直接映射 |

自动化检查：

- 指标名符合 `[a-zA-Z_:][a-zA-Z0-9_:]*`（或明确的 UTF-8 规范）；
- counter 以 `_total` 结尾；
- 单位在指标名中；
- 标签值基数在预算内；
- 没有重复序列/冲突类型；
- exposition 能被 `promtool check metrics` 解析。

脚本可以把映射表转成：

- exporter 的 relabel 配置；
- 旧面板的 PromQL 改写建议；
- 迁移覆盖率报告（哪些旧指标还没有新映射）。

## 29.14 历史数据迁移的工程细节

### 29.14.1 方案选择

| 方案 | 适用 | 成本 | 风险 |
|---|---|---|---|
| 不迁移 | 历史价值低 | 低 | 旧系统只读保留 |
| OpenMetrics 导入 | 有导出文件 | 中 | 时间戳/标签要对齐 |
| remote write 回填 | 有 API/脚本 | 中/高 | 乱序/重复/限流 |
| 对象存储归档 | 已有 block | 低 | 查询层要支持 |
| 只保留聚合 | 长期趋势 | 低 | 丢失细节 |

### 29.14.2 OpenMetrics 导入

```bash
# 生成符合 OpenMetrics 的文件（每行样本可带时间戳）
# 用 promtool 创建 block
promtool tsdb create-blocks-from openmetrics history.txt /var/lib/prometheus
```

注意：

- 时间戳单位要与格式匹配（文本格式毫秒，OpenMetrics 秒）；
- 标签集合必须与现有序列一致，否则会创建新序列；
- 重复时间戳会冲突；
- 导入后重启/刷新 block；
- 大量数据会触发 compaction，预留磁盘/IO。

### 29.14.3 remote write 回填

```text
读旧数据 → 转成 Prometheus 样本 → remote write → 目标端
```

要点：

- 目标端可能拒绝乱序（除非开 OOO 窗口）；
- 需要限速/重试/断点续传；
- 回填流量会与实时写入竞争；
- 目标端 retention 可能删掉刚回填的旧数据；
- 回填的 `external_labels`/租户头要正确。

### 29.14.4 历史数据的价值评估

不是所有历史都值得迁移。先问：

- 谁会查询 1 年前的数据？
- 合规要求保留多久？
- 旧数据的标签/语义是否可信？
- 迁移成本 vs 重新采集的成本？
- 是否只需要保留聚合/月报？

常见做法：保留最近 30～90 天原始数据 + 长期聚合数据，旧系统只读归档。

## 29.15 告警迁移的双跑与验收

告警迁移比面板更危险，因为漏报/误报直接影响 on-call。

双跑流程：

```text
1. 新规则部署到测试/影子环境
2. 同一份数据同时喂给旧/新规则
3. 对比：触发时间、标签、严重级别、通知内容
4. 记录差异并修正
5. 小流量切路由
6. 全量切
7. 保留旧规则一段时间（只记录不通知）
```

对比维度：

- 触发/恢复时间差；
- `for`/`keep_firing_for` 行为；
- 标签/注释是否完整；
- 路由/分组/抑制是否符合预期；
- 静默/维护窗口是否生效；
- 通知渠道/模板是否正确。

验收标准示例：

```text
[ ] 关键告警 100% 覆盖
[ ] 双跑 2 周，误报率不高于旧系统
[ ] 漏报演练通过（注入故障）
[ ] on-call 确认通知可读、可行动
[ ] 回滚路径演练通过
```

## 29.16 切读、切写与回滚

### 切读

- Grafana 数据源切换（保留旧数据源只读）；
- 先切内部用户，再切业务团队；
- 监控查询延迟/错误/空结果；
- 保留旧 dashboard 的导出/备份。

### 切写

- 双写：旧系统 + 新系统并行；
- 对比关键指标；
- 逐步扩大新系统写入比例；
- 注意重复计费/重复告警。

### 回滚触发条件

```text
- 关键 SLI 缺失/错误超过阈值
- 告警漏报/误报显著增加
- 查询延迟超过 SLO
- 数据丢失/重复无法接受
- on-call 无法处理
```

回滚计划要包括：

- 数据源/DNS/路由如何切回；
- 旧系统是否仍在运行；
- 历史数据是否可访问；
- 已发送的告警如何处理；
- 回滚后的复盘。

## 29.17 组织与沟通

迁移失败常见原因不是技术，而是：

- 业务团队不知道要改 dashboard；
- on-call 没培训新告警；
- 没人负责旧系统下线；
- 迁移期间双倍工作导致疲劳；
- 没有明确的"完成"定义。

建议：

- 建立迁移 charter（范围/目标/时间表/DRI）；
- 每周同步进度/风险；
- 提供迁移手册、映射表、FAQ；
- 开 office hours；
- 先迁移"低风险 + 高价值"的服务，建立信心；
- 迁移后复盘并更新 runbook。

## 本章小结

- 迁移是模型/查询/告警/流程的整体迁移，不是换数据库。
- Graphite/StatsD/Influx/Zabbix/Datadog 各有映射陷阱。
- 历史数据通常只保留聚合，回填要评估成本。
- 双跑、对比、演练、回滚是安全迁移的关键。
- 兼容性预检能在早期发现格式/基数问题。

## 延伸阅读

- Prometheus 官方 "Migrating to Prometheus"。
- `promtool` 的 tsdb/metrics 检查。
- 本教程第 5 章（exposition）与第 27 章（基数）。

# 第 49 章 合规、数据生命周期与审计

## 49.1 为什么监控系统会进入合规范围

指标看起来只是数字，但一条时间序列的身份由**指标名 + 全部标签**决定。标签里经常出现：

```text
http_requests_total{instance="10.0.0.7:8080", path="/orders/202611260001", user_id="u123"}
```

这里的 `path` 含订单号，`user_id` 是稳定标识。它们会随样本进入 head、WAL、block、快照、remote write、长期存储、备份和 Grafana 查询历史。于是"监控数据"不再是纯聚合数据，而可能构成**个人信息**或**业务敏感数据**。

常见合规框架对这类数据提出的要求可以翻译成工程控制：

| 要求 | 工程控制 | Prometheus 侧落点 |
|---|---|---|
| 合法性基础与目的限制 | 数据清单、用途登记 | 指标目录、采集评审 |
| 最小化 | 只采集必要标签 | `metric_relabel_configs`、埋点规范 |
| 存储限制 | 到期删除 | retention、对象存储生命周期 |
| 删除权/被遗忘权 | 可定位、可删除、可证明 | delete API、tombstone、下游删除 |
| 访问控制 | 最小权限、可审计 | 反向代理/SSO、Grafana 权限、审计日志 |
| 完整性/保密性 | 传输/静态加密 | TLS、磁盘加密、KMS |
| 可问责 | 记录处理活动 | 指标目录、变更记录、审计日志 |
| 数据驻留 | 数据不出境 | 分区部署、租户隔离、外部存储选型 |

> 本章讨论的是**工程实现**，不是法律意见。具体义务取决于所在司法辖区、行业和合同，请让法务/合规团队确认。

一个实用的风险分级：

- **高**：标签值可能是邮箱、手机号、身份证号、精确地址、自由文本错误、完整 URL、用户名。
- **中**：IP、Pod 名、节点名、租户 ID、订单号（可关联到个人）。
- **低**：指标名、状态码、方法、区域、版本、服务名等可枚举维度。

高风险标签不应进入指标；中风险标签要评估是否可用哈希/截断/聚合替代，并确保访问受控。

## 49.2 数据分类与最小化

### 49.2.1 先做数据流清单

在讨论 retention 之前，先画出数据流：

```text
exporter / app
   │  /metrics（可能含标签）
   ▼
Prometheus（head + WAL + blocks + snapshots）
   │  remote write
   ▼
长期存储（Thanos / Mimir / VictoriaMetrics / 云服务）
   │
   ├── Grafana（查询、面板、查询历史）
   ├── Alertmanager（告警标签、通知内容）
   ├── 工单 / ChatOps（消息可能被归档）
   └── 备份 / 对象存储（可能跨区域）
```

每一跳都可能产生副本。只删除 Prometheus 本地数据，不会自动删除 remote write、快照、备份和工单里的内容。

### 49.2.2 标签最小化的六种手段

1. **不采集**：从源头删掉无聚合价值的标签。优先在客户端埋点阶段解决。
2. **抓取时丢弃**：`metric_relabel_configs` 的 `labeldrop`/`labelreplace`。
3. **规范化**：把 URL 中的 ID 替换成模板，例如 `/orders/{id}`；把自由文本错误映射为 `error_type`。
4. **哈希/伪匿名**：对低熵 ID 直接哈希仍可能被字典反推；需要加盐并控制盐的访问。
5. **聚合**：只保留 `region`、`status` 等可聚合维度，把个体维度留给日志/追踪。
6. **分离**：确实需要个体级排查时，用 exemplar/trace ID 跳到追踪系统，而不是把个体 ID 放进指标标签。

示例：

```yaml
scrape_configs:
  - job_name: api
    metric_relabel_configs:
      # 删除明确的高风险标签
      - regex: "user_id|email|session_id|request_id"
        action: labeldrop
      # 把带 ID 的路径规范化为模板（示例规则，按实际路径调整）
      - source_labels: [path]
        regex: "/orders/[^/]+"
        target_label: path
        replacement: "/orders/{id}"
        action: replace
```

注意：`metric_relabel_configs` 发生在抓取解析之后、写 TSDB 之前。它不会阻止 exporter 把数据暴露在网络上，也不会清理抓取失败日志、exemplar 或远端已写入的数据。

### 49.2.3 PII 扫描

把"标签值是否像 PII"做成周期性检查，而不是一次性评审：

```promql
# 找出标签值数量异常多的指标（可能是高基数/PII）
count by (__name__) ({__name__=~".+"})

# 找出带可疑标签名的序列（需要先知道你的命名）
count by (__name__, user_id) ({user_id=~".+"})
```

PromQL 不能读取"标签名列表"做通用扫描；更可靠的做法是从 `/api/v1/labels`、`/api/v1/label/<name>/values` 或 TSDB 索引导出标签清单，再用脚本匹配邮箱/手机号/身份证/IP 等模式。扫描结果应进入指标目录和评审流程（第 47 章）。

## 49.3 保留策略：从工程参数到合规承诺

### 49.3.1 三类 retention 不要混为一谈

```text
本地 TSDB retention      --storage.tsdb.retention.time / .size
远端存储 retention       由 Thanos/Mimir/云服务配置
备份/快照 retention      由对象存储生命周期和备份策略决定
```

本地 retention 只控制本地 block。remote write 之后的副本、快照、备份、Grafana 查询历史、Alertmanager 通知记录都需要单独设置。

### 49.3.2 保留期怎么定

保留期通常由两个方向夹逼：

- **下限**：审计、取证、容量趋势、SLO 复盘需要保留多久；
- **上限**：隐私义务、合同、数据最小化要求允许保留多久。

两者冲突时，常见做法是**分层保留**：

| 数据 | 典型保留 | 说明 |
|---|---|---|
| 原始高基数指标 | 7～30 天 | 排查用，尽快聚合/删除 |
| 聚合后的 SLO/容量指标 | 13 个月 | 覆盖年度对比 |
| 告警事件/通知 | 1～3 年 | 审计与复盘 |
| 审计日志 | 6 个月～数年 | 通常长于指标 |
| 备份 | 30～90 天 | 与 RPO/RTO 匹配 |
| 法律保留（legal hold） | 按案件 | 暂停删除，单独隔离 |

"13 个月"不是标准，只是常见的年度对比需求；以你的合规要求为准。

### 49.3.3 删除不是瞬时动作

Prometheus 的删除链路：

```text
delete API / retention
  → tombstone（逻辑删除）
  → 查询过滤
  → compaction 重写 block（物理删除）
  → 旧 block 删除
  → 快照/备份/远端仍需单独处理
```

因此：

- 删除请求成功后，查询立即看不到数据（tombstone 生效）；
- 磁盘空间要等 compaction 才释放；
- 快照和备份可能仍含旧数据；
- remote write 已经写出的副本不会自动删除。

如果合规要求"可证明删除"，需要记录：删除请求、影响的时间范围/序列、tombstone 写入、compaction 完成、下游删除回执、备份过期时间。

### 49.3.4 用对象存储生命周期兜底

长期存储通常把 block 放在对象存储。给 bucket 配置生命周期规则，按前缀/标签过期，能把"忘记删"变成"自动删"。但要注意：

- 生命周期删除不可逆；
- 跨区域复制会保留另一份；
- legal hold 需要单独的 bucket/前缀，避免被生命周期误删；
- 版本化 bucket 的"删除标记"不等于物理删除。

## 49.4 删除请求与删除 API

Prometheus 的删除 API 需要显式开启：

```bash
prometheus --web.enable-admin-api
```

请求示例：

```bash
# 删除某指标在时间范围内的数据
curl -X POST 'http://prometheus:9090/api/v1/admin/tsdb/delete_series' \
  --data-urlencode 'match[]=http_requests_total{user_id="u123"}' \
  --data-urlencode 'start=2026-11-01T00:00:00Z' \
  --data-urlencode 'end=2026-11-26T00:00:00Z'

# 让 tombstone 尽快落盘、释放空间
curl -X POST 'http://prometheus:9090/api/v1/admin/tsdb/clean_tombstones'
```

要点：

- `match[]` 是序列选择器，不是任意 PromQL；
- 时间范围是闭区间，支持 RFC3339 或 Unix 秒；
- 删除是**逻辑删除**，`clean_tombstones` 之后才重写 block；
- 删除 head 中的数据时，tombstone 先在内存生效，重启后依赖 WAL/checkpoint 恢复；
- 删除操作本身没有内置审计，必须在网关/代理层记录调用者、参数、时间、结果。

### 49.4.1 删除的传播清单

一次"个人数据删除"通常要覆盖：

```text
[ ] Prometheus 本地 head/WAL/blocks
[ ] Prometheus 快照目录
[ ] remote write 队列（尚未发送的样本）
[ ] 长期存储（Thanos/Mimir/VM/云服务）
[ ] 对象存储中的 block 与备份
[ ] 其他 Prometheus 副本（HA 对）
[ ] Grafana 查询历史/缓存/告警历史
[ ] Alertmanager 通知与工单系统
[ ] 日志/追踪系统中的关联 ID
```

现实中最容易漏的是**副本、快照和下游**。把清单固化到 runbook，并在每次删除后留存证据。

### 49.4.2 删除的替代方案

如果数据已经进入长期存储且删除成本很高，可以考虑：

- **crypto-shredding**：按租户/主体加密，删除密钥使数据不可读；
- **分区隔离**：按租户/区域分桶，整桶过期；
- **提前聚合**：删除前把个体数据聚合成不可逆的统计量；
- **合同约定**：在采购合同中明确删除 SLA 和证明方式。

这些方案不能替代法律评估，但能显著降低工程成本。

## 49.5 访问控制、审计与最小权限

### 49.5.1 Prometheus 本身没有 RBAC

单机 Prometheus 的 Web/API 没有用户、角色、租户概念。生产常见的补偿：

- 反向代理做 SSO/OIDC、TLS、IP 白名单；
- 代理层按路径限制：`/api/v1/query` 只读，`/api/v1/admin/*` 仅管理员；
- Grafana 做数据源级权限和团队隔离；
- 网络策略限制谁能访问 9090；
- 多租户用独立实例/命名空间，或使用 Mimir/Thanos 的租户能力。

### 49.5.2 审计日志记什么

Prometheus 不自带查询审计。需要在代理/Grafana/网关层记录：

```json
{
  "ts": "2026-11-26T10:00:00Z",
  "actor": "alice@example.com",
  "source_ip": "10.1.2.3",
  "action": "query",
  "endpoint": "/api/v1/query_range",
  "query": "http_requests_total{user_id=\"u123\"}",
  "start": "2026-11-01T00:00:00Z",
  "end": "2026-11-26T00:00:00Z",
  "status": 200,
  "bytes": 12345
}
```

审计日志本身可能包含 PII（查询条件里有 `user_id`），需要：

- 与指标分开存储、单独 retention；
- 访问受控；
- 必要时对查询文本做脱敏；
- 防篡改（WORM/对象锁/日志服务）。

### 49.5.3 管理端点是高危面

以下端点应默认关闭或严格限制：

```text
/api/v1/admin/tsdb/delete_series
/api/v1/admin/tsdb/clean_tombstones
/api/v1/admin/tsdb/snapshot
/-/quit
/-/reload
/api/v1/status/config       （配置结构；敏感字段通常脱敏，但仍应限制访问）
/api/v1/status/flags
```

`/-/reload` 需要 `--web.enable-lifecycle`；`delete_series`/`snapshot` 需要 `--web.enable-admin-api`。不要为了"方便"在生产长期开启 admin API 且不加认证。

## 49.6 数据驻留、多租户与跨境

### 49.6.1 单机 Prometheus 是单租户

Prometheus 没有租户 ID 隔离。多租户的常见实现：

- **实例级**：每个租户/区域一套 Prometheus；
- **命名空间级**：Kubernetes 中按 namespace 部署，配合 NetworkPolicy；
- **远端多租户**：remote write 带 `X-Scope-OrgID` 写入 Mimir/Cortex，查询时按租户头隔离；
- **Thanos**：通过外部标签/租户路由实现逻辑隔离，但需要额外的授权层。

隔离不仅是查询隔离，还包括：抓取凭证、告警路由、通知渠道、备份、审计和配额。

### 49.6.2 数据驻留

如果合同要求"数据不出境"：

- Prometheus 与长期存储部署在同一区域；
- 禁用跨区域 remote write/备份；
- 联邦/全局视图只汇总**聚合指标**，不复制原始高基数数据；
- 云服务的控制面/支持访问也要纳入评估；
- 记录数据流和子处理者清单。

`external_labels` 可以帮助标记来源区域，但它不是访问控制，也不能替代物理隔离。

## 49.7 备份、快照与灾难恢复的合规视角

### 49.7.1 快照

```bash
curl -X POST 'http://prometheus:9090/api/v1/admin/tsdb/snapshot'
```

快照包含当前 block 和 head，是**一致性备份**的起点。合规要点：

- 快照目录可能含已删除但尚未 compaction 的数据；
- 备份加密、访问控制、跨区域策略要明确；
- 备份 retention 与删除义务冲突时，需要 legal hold 流程；
- 定期做恢复演练，并记录 RPO/RTO 证据。

### 49.7.2 恢复演练清单

```text
[ ] 从快照/对象存储恢复到隔离环境
[ ] 校验 block 完整性（promtool tsdb analyze）
[ ] 校验关键查询结果与告警规则
[ ] 记录恢复耗时（RTO）与数据丢失窗口（RPO）
[ ] 验证删除过的数据没有"复活"
[ ] 更新恢复报告与审计证据
```

跨版本恢复要验证索引格式兼容性；大版本升级前先做快照和恢复演练。

## 49.8 合规检查表与审计证据

把合规要求映射成可检查的控制项：

| 控制项 | 检查方法 | 证据 |
|---|---|---|
| 指标清单与 owner | 指标目录/评审记录 | 目录导出、评审工单 |
| PII 标签扫描 | 定期扫描 label values | 扫描报告、修复记录 |
| 最小化 | 抓取配置审查 | `metric_relabel_configs` diff |
| retention | 配置与后端生命周期 | 配置快照、bucket 规则 |
| 删除 | runbook + 演练 | 删除工单、tombstone/compaction 日志 |
| 访问控制 | 代理/SSO/网络策略 | 访问评审、策略文件 |
| 审计 | 查询/管理操作日志 | 审计日志样本、保留策略 |
| 加密 | TLS/磁盘/KMS | 配置、证书、KMS 策略 |
| 备份 | 快照/恢复演练 | 恢复报告、RPO/RTO |
| 多租户/驻留 | 架构图与数据流 | 架构评审、子处理者清单 |
| 供应商 | 合同/安全问卷 | DPA、SOC2/ISO 报告 |

自动化建议：

- CI 中对 `prometheus.yml`/规则做 lint（`promtool check config`、`pint`）；
- 定期从 `/api/v1/labels` 导出标签名，匹配敏感名单；
- 对高基数指标做预算告警（第 27 章）；
- 把 retention/删除配置纳入 GitOps，变更可审计（第 46 章）。

## 49.9 行业映射（工程视角）

以下是常见框架与监控工程的对应关系，仅作起点：

- **GDPR**：Art.5（最小化/存储限制）、Art.17（删除）、Art.30（处理活动记录）、Art.32（安全措施）。重点是把"标签即个人数据"识别出来。
- **CCPA/CPRA**：删除权、知情权、选择退出。需要能定位"某个消费者"的相关指标。
- **PIPL / 数据安全法**：告知同意、最小必要、境内存储、跨境评估。监控数据可能属于"个人信息"或"重要数据"。
- **等保 2.0**：身份鉴别、访问控制、安全审计、入侵防范、数据完整性/保密性。对应 SSO、RBAC、审计日志、TLS、备份。
- **PCI DSS**：卡号、CVV 绝不进入指标标签；持卡人数据环境中的监控要隔离。
- **HIPAA**：PHI 不进入指标；访问审计、传输加密、BAA。
- **SOC 2 / ISO 27001**：变更管理、访问评审、监控、事件响应、供应商管理。监控系统本身也是被审计对象。

常见审计问题：

1. 你们如何发现指标里出现了个人信息？
2. 删除请求如何在所有副本和备份中生效？
3. 谁能查询/删除监控数据？如何证明？
4. 监控数据的 retention 是多少？谁批准的？
5. 跨境数据传输如何控制？
6. 备份能否恢复？上次演练是什么时候？

能拿出配置、日志、工单和演练报告，比口头保证更有说服力。

## 49.10 Mini 的对应能力与边界

Mini-Prometheus 实现了与生命周期相关的一部分能力：

- `POST /api/v1/admin/tsdb/delete_series`：写 tombstone；
- `POST /api/v1/admin/tsdb/clean_tombstones`：重写 block；
- `/api/v1/admin/tsdb/snapshot`：复制 block + head；
- retention：按时间/大小删除 block；
- WAL/checkpoint：重启恢复。

可以用 Mini 做实验：

```bash
# 写入一些数据后删除
curl -X POST 'http://127.0.0.1:9090/api/v1/admin/tsdb/delete_series' \
  --data-urlencode 'match[]=demo_http_requests_total{job="demo"}'

# 确认查询不到
curl -s 'http://127.0.0.1:9090/api/v1/query?query=demo_http_requests_total'

# 清理并观察 block 重写
curl -X POST 'http://127.0.0.1:9090/api/v1/admin/tsdb/clean_tombstones'
```

Mini **没有**：认证/授权、查询审计、多租户、远端删除传播、加密、legal hold、合规报表。它适合演示"tombstone → compaction → 空间释放"的语义，不能作为合规系统使用。

## 本章小结

- 指标标签可能携带个人信息；合规从"识别标签"开始，而不是从 retention 开始。
- 数据流每一跳都会产生副本，删除必须覆盖本地、快照、备份、副本和下游。
- retention 是工程参数，也是合规承诺；分层保留能同时满足审计和最小化。
- Prometheus 没有内置 RBAC/查询审计，需要代理、Grafana、网关和日志系统补足。
- 管理 API 是高危面，默认关闭并严格限制。
- 多租户/数据驻留要靠部署拓扑和远端存储能力，不能靠标签约定。
- 备份要能恢复、删除要能证明、访问要能审计；证据比口头保证重要。

## 延伸阅读

- Prometheus 官方文档：Storage、HTTP API、Security。
- GDPR、CCPA/CPRA、PIPL、等保 2.0 的公开条文与指南（以官方法务意见为准）。
- NIST SP 800-53/800-61 中关于审计、事件响应和介质保护的控制项。
- 本教程第 21 章（安全与多租户）、第 23 章（备份/运维）、第 27 章（基数治理）、第 46 章（GitOps）与第 47 章（指标治理）。

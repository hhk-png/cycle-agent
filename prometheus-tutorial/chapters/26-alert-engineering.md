# 第 26 章 告警工程：从"能报警"到"值得起床"

## 26.1 告警的分类

- **症状告警（symptom）**：用户可见的问题（错误率、延迟、可用性）。应 page。
- **原因告警（cause）**：底层资源/依赖问题（CPU、磁盘、队列）。通常 ticket 或 dashboard。
- **预测告警**：容量趋势（磁盘 3 天后满）。ticket。
- **审计/合规**：配置变更、证书到期。ticket。

Page 应尽量是症状告警；原因告警用于辅助定位，不应直接叫醒人。

## 26.2 好告警的标准

- **可行动**：收到后知道做什么。
- **紧迫**：需要立即处理，否则影响扩大。
- **用户相关**：对应真实用户体验。
- **有主**：明确的 owner/runbook。
- **低误报**：`for`/多窗口/抑制。
- **可测试**：有单元测试/演练。

反例：

```promql
# 任何 CPU 高都告警
cpu_usage > 80
```

CPU 高可能正常（批处理）；应看延迟/错误率/饱和度。

## 26.3 黄金信号与 RED/USE

服务：

- Latency、Traffic、Errors、Saturation（Google SRE）。
- RED：Rate、Errors、Duration（微服务）。

资源：

- Utilization、Saturation、Errors（USE）。

Kubernetes：

- 控制面：API 延迟/错误/队列；
- 节点：CPU/内存/磁盘/网络/PID；
- 容器：重启/OOM/CPU throttling；
- 对象：副本不足/Job 失败/PVC 满。

## 26.4 告警表达式设计

```promql
# 症状：5xx 比例
sum by (service) (rate(http_requests_total{code=~"5.."}[5m]))
/
sum by (service) (rate(http_requests_total[5m])) > 0.05

# 饱和：队列积压
sum by (queue) (queue_depth) > 1000

# 预测：磁盘 4 天后满
predict_linear(node_filesystem_avail_bytes[6h], 4*24*3600) < 0
```

原则：

- 先过滤再聚合；
- 分母保护；
- 用 `for` 过滤抖动；
- 加 `runbook_url`；
- 标签低基数；
- 避免 `bool` 导致全量返回。

## 26.5 严重级别

| 级别 | 含义 | 渠道 | 响应 |
|---|---|---|---|
| page | 用户可见/快速恶化 | 电话/PagerDuty | 立即 |
| ticket | 趋势/非紧急 | Jira/Slack | 工作时间 |
| info | 记录/审计 | 日志/看板 | 无 |

级别应由"影响 + 紧迫性"决定，不是"指标重要性"。

## 26.6 告警疲劳

症状：

- 大量重复告警；
- 同一故障多个告警；
- 误报多；
- 值班人员忽略/静音。

治理：

- 合并/抑制（Alertmanager）；
- 调整阈值/`for`；
- 删除不可行动告警；
- 按服务/团队路由；
- 定期 review 告警量/误报率；
- 用 SLO burn rate 替代部分阈值告警。

指标：

```promql
sum(rate(alertmanager_notifications_total[1d]))
sum(rate(alertmanager_notifications_failed_total[1d]))
```

## 26.7 Runbook

每个 page 告警应有：

```text
告警含义
影响范围
立即检查（命令/链接）
常见原因
缓解步骤
升级路径
相关 dashboard/日志/追踪
```

Runbook 应随代码/规则一起版本化。

## 26.8 告警测试

- 单元测试：`promtool test rules`；
- 集成测试：把测试告警发到 Alertmanager 测试实例；
- 演练：定期触发测试告警，验证路由/通知；
- 混沌：注入延迟/错误，验证告警是否按时触发；
- 复盘：每次 page 后检查告警是否有效。

## 26.9 常见反模式

- **每个指标都告警**：告警风暴。
- **只告警原因**：用户已经受影响才有人知道。
- **阈值拍脑袋**：没有历史依据。
- **无 runbook**：收到告警不知道做什么。
- **`for: 0s`**：抖动即 page。
- **告警标签带 pod/instance**：无法分组/去重。
- **静默长期化**：掩盖问题。
- **告警不测试**：规则错误直到故障才发现。

## 26.10 告警分级、路由与值班

告警分级不是"随手写 severity"，而是决定**谁在什么时间、以什么方式被叫醒**。

### 26.10.1 分级模型

| 级别 | 含义 | 响应 | 示例 |
|---|---|---|---|
| `critical` | 用户可见、SLO 快速燃烧、需要立即人工 | 立即 page/电话 | 支付成功率 < 95%、核心服务全挂 |
| `warning` | 有风险/趋势，尚未严重影响用户 | 工作时间/工单 | 磁盘 4 天后满、错误率上升 |
| `info` | 记录/观察 | 不通知或低优先级 | 发布完成、配置变更 |

分级原则：

- 只有**需要立即人工介入**的才 critical；否则会耗尽值班精力；
- warning 要有明确的处理时限/owner；
- info 不应进入值班通道；
- 同一问题不要同时发 critical 和 warning（用抑制/聚合）。

### 26.10.2 路由

```yaml
route:
  receiver: default
  routes:
    - matchers: ['severity="critical"']
      receiver: pager
    - matchers: ['severity="warning"', 'team="data"']
      receiver: data-team
```

路由设计：

- 按团队/服务/severity 路由；
- 不要把所有告警发给所有人；
- critical 路由要有升级路径（无人响应 → 上级/备份）；
- 静默/抑制/分组与路由一起设计；
- 定期测试路由（发测试告警/用 amtool）。

### 26.10.3 值班

- 值班表公开、轮换合理、有备份；
- 值班人有权回滚/降级/扩容；
- runbook 可直接执行（命令/链接/权限）；
- 告警包含足够上下文（服务、实例、SLO、最近变更）；
- 交接记录未完成事项；
- 统计告警量/噪音/MTTA/MTTR，持续优化。

## 26.11 On-call 与事故响应

告警只是开始，事故响应才是闭环。

### 26.11.1 角色

- **Incident Commander（IC）**：负责决策/协调，不亲自修；
- **Operations/Subject Matter Expert**：执行修复；
- **Communications**：对内/对外同步；
- **Scribe**：记录时间线/决策/证据。

小团队可以一人多角色，但要有明确负责人。

### 26.11.2 流程

```text
发现 → 确认影响 → 建立事件通道 → 止血 → 根因分析 → 恢复 → 复盘
```

止血优先：

- 回滚/降级/切流/扩容；
- 不要在生产上边调试边改；
- 记录每个操作和时间点；
- 保留证据（日志/指标/配置/变更）。

### 26.11.3 沟通

- 固定更新节奏（如每 15/30 分钟）；
- 说明：影响、当前状态、已做操作、下一步、预计时间；
- 不要猜测根因；区分"已知事实"和"假设"；
- 对外沟通由指定角色负责。

### 26.11.4 复盘

无责复盘（blameless）：

- 时间线；
- 影响（用户/收入/SLO）；
- 根因（技术/流程/组织）；
- 做对了什么；
- 改进项（owner/截止时间）；
- 监控/告警缺口；
- 是否可自动止血。

避免：

- 只写"人为失误"；
- 改进项没有 owner/期限；
- 复盘后不跟踪；
- 把复盘当问责。

### 26.11.5 指标

- MTTA（确认时间）、MTTR（恢复时间）；
- 告警量/值班负担；
- 误报率/漏报率；
- 复盘改进项完成率；
- 同类事故复发率。

## 本章小结

- Page 应是症状告警，原因告警用于辅助。
- 好告警可行动、紧迫、用户相关、有主、低误报、可测试。
- SLO burn rate 是替代部分阈值告警的有效方法。
- 告警疲劳要靠合并/抑制/删除/路由治理。
- Runbook 和演练是告警工程的一部分。

## 延伸阅读

- Google SRE Book/Workbook 的告警章节。
- Prometheus/Alertmanager 文档。
- 本教程第 25 章（SLO）与第 10 章（Alertmanager）。

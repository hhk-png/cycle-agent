# 第 51 章 综合实战（二）：平台化、压测、演练与复盘

第 50 章完成了一张监控设计图。这一章把它推进到"能长期运行"：从单实例演进到平台，用 GitOps 管理配置，用压测验证容量，用故障演练检验告警与 runbook，最后用复盘把一次事故变成系统能力。

本章的重点不是再讲一遍功能，而是回答四个问题：

1. 规模上来后，拓扑怎么演进？
2. 变更如何安全地进入生产？
3. 怎么证明容量和告警真的有效？
4. 出事后如何止血、复盘、防止复发？

## 51.1 从单实例到平台

### 51.1.1 演进阶段

| 阶段 | 拓扑 | 适用规模 | 主要风险 |
|---|---|---|---|
| 0 | 单实例 + 本地 TSDB | 小团队、单集群 | 单点故障、保留短 |
| 1 | HA 对 + 相同配置 | 核心业务、可接受双写 | 去重、告警重复 |
| 2 | 每集群实例 + remote write | 多集群/多区域 | 队列积压、外部标签不一致 |
| 3 | 全局查询层 + 多租户 | 平台团队、多业务线 | 成本、权限、查询放大 |

不要一步跳到阶段 3。每升一级，都要先补齐上一级的运维能力：备份、升级、容量、告警、runbook。

### 51.1.2 HA 对

```text
Prometheus A ─┐
              ├─ 相同 scrape 配置 + 相同 external_labels（除 replica）
Prometheus B ─┘
              └─ 各自本地 TSDB，查询任选其一
```

要点：

- 两个实例抓同一批目标，数据双写；
- `external_labels.replica` 区分副本，远端存储按 `cluster + replica` 去重；
- 告警由 Alertmanager HA 集群去重，不要让两个 Prometheus 各发一份；
- 查询层要能容忍副本之间的微小时间差；
- 升级时先升 B、再升 A，保证始终有一个健康副本。

### 51.1.3 多集群与长期存储

```text
cluster-a Prometheus ─┐
cluster-b Prometheus ─┼─ remote write ─► Mimir/Thanos ─► 全局查询
cluster-c Prometheus ─┘
```

设计要点：

- `external_labels` 至少包含 `cluster`、`region`、`replica`；
- 每个集群的抓取配置独立，避免"中心配置一改全挂"；
- 远端存储的租户/租户标签与团队边界一致；
- 全局查询只查聚合/长期数据，不要把单集群高基数原始数据全量上卷；
- 为 remote write 建独立告警：队列积压、发送失败、丢弃样本（第 18、22 章）。

### 51.1.4 平台边界

平台团队负责：

- Prometheus/Alertmanager/长期存储的部署与升级；
- 抓取与规则的基线、配额、校验流水线；
- 全局 dashboard/告警模板/runbook 框架；
- 容量、成本、合规、审计。

业务团队负责：

- 自己服务的指标建模与埋点；
- 自己服务的 SLO、告警、runbook；
- 自己服务的 dashboard 与排障。

边界写进文档，否则要么平台成为瓶颈，要么业务重复造轮子（第 56 章）。

## 51.2 GitOps 与配置流水线

### 51.2.1 仓库布局

```text
monitoring/
  base/
    prometheus/
      prometheus.yml
      rules/
        slo.yml
        infra.yml
    alertmanager/
      alertmanager.yml
  clusters/
    prod-cn/
      kustomization.yaml
      external-labels.patch.yaml
    staging/
      kustomization.yaml
  dashboards/
    checkout.json
  runbooks/
    checkout-availability.md
  ci/
    validate.sh
```

原则：

- 配置、规则、dashboard、runbook 同仓管理；
- 环境差异用 overlay/patch，不复制整份配置；
- Secret 不进 Git，用 External Secrets/Vault/SOPS；
- 每个告警规则有 owner 与 runbook 链接。

### 51.2.2 CI 校验

```bash
#!/usr/bin/env bash
set -euo pipefail

# 1. 配置与规则语法
promtool check config base/prometheus/prometheus.yml
promtool check rules base/prometheus/rules/*.yml

# 2. 规则单元测试
promtool test rules tests/rules/*.yml

# 3. 配置 lint（可选）
pint lint base/prometheus/rules/*.yml

# 4. YAML/JSON 格式
yamllint -c .yamllint base clusters
```

CI 必须阻止：

- 语法错误；
- 规则测试失败；
- 引用了不存在的指标/标签（pint 可做部分检查）；
- 告警缺少 `severity`/`runbook_url`；
- 高基数标签（自定义 lint）。

### 51.2.3 发布与回滚

```text
PR → CI 校验 → review → merge → Argo CD/Flux 同步
  → Prometheus reload（或 Operator 滚动）
  → 观察 15 分钟：目标 up、规则健康、告警量、查询延迟
  → 异常则回滚 Git commit
```

注意：

- `/-/reload` 只重载配置/规则，不重载 flag；
- 规则重建会导致 pending 计时重置；频繁 reload 会让告警永远不 firing（附录 S.17）；
- 用 `prometheus_config_last_reload_successful` 与 `prometheus_rule_group_last_evaluation_timestamp_seconds` 监控 reload/规则；
- 回滚要回滚配置与镜像，不要只回滚一半。

### 51.2.4 Secret 管理

- 抓取认证、remote write 凭证、Alertmanager 通知凭证都不进 Git；
- 用 External Secrets Operator/Vault/SOPS 注入；
- 凭证轮换要有双版本过渡；
- 监控凭证过期时间（`probe_ssl_earliest_cert_expiry`、云厂商过期指标）；
- 审计谁在何时读取了 Secret。

## 51.3 容量与成本验证

### 51.3.1 工作负载模型

用四个量描述负载：

```text
活跃序列数 N
抓取间隔 S
样本速率 R = N / S
查询 QPS 与查询范围/步长
规则组数与每条规则的扫描范围
```

示例：

```text
N = 200,000
S = 15s
R ≈ 13,333 samples/s
查询：20 QPS，平均 6h 范围，30s 步长
规则：40 组，每组 10 条，平均 5m 范围
```

### 51.3.2 验收指标

| 指标 | 目标 | 说明 |
|---|---|---|
| 抓取 P99 时长 | < scrape_timeout 的 50% | 留出网络抖动余量 |
| `prometheus_tsdb_head_series` | < 预算的 80% | 提前扩容 |
| 查询 P99 | < 2s（面板）/ < 10s（探索） | 按场景 |
| 规则求值 P99 | < interval 的 50% | 避免规则堆积 |
| compaction 失败 | 0 | 磁盘/IO 预警 |
| remote write 队列 | < 50% 容量 | 网络/后端预警 |
| 内存 | < limit 的 80% | 留 GC/突发余量 |

### 51.3.3 压测方法

1. **写入压测**：用合成 exporter 或 `promtool tsdb create-blocks-from` 生成目标序列数；
2. **查询压测**：用真实 dashboard 查询集，记录 P50/P95/P99；
3. **规则压测**：加载生产规则，观察求值时长与内存；
4. **故障注入**：在压测中杀掉一个副本/模拟远端超时；
5. **回归**：每次升级/大改规则后重跑同一套基准。

不要用"单条 `up` 查询很快"证明系统能扛生产；查询的形状（范围、基数、聚合）决定成本（第 17、48 章，附录 Y）。

### 51.3.4 成本模型

```text
月度成本 ≈ 计算（CPU/内存）× 单价
         + 本地磁盘 × 单价
         + 远端存储 GB × 单价
         + 查询/API 费用（托管）
         + 人力运维成本
```

优化杠杆：

- 降低高基数指标/标签；
- 降采样/聚合后再长期存储；
- 缩短本地保留，长期数据放对象存储；
- 用 recording rules 预计算高频查询；
- 按团队/租户分摊成本（第 27、53 章）。

## 51.4 故障演练

演练的目标不是"制造故障"，而是验证：

- 告警是否按时触发；
- runbook 是否可执行；
- 降级/回滚是否有效；
- 监控系统自身是否还能工作。

### 51.4.1 演练矩阵

| 演练 | 注入方式 | 预期信号 | 止血 | 验证 |
|---|---|---|---|---|
| 目标宕机 | 停掉一个 exporter/Pod | `up == 0`、抓取错误 | 重启/切流 | `up` 恢复、无数据缺口 |
| 基数爆炸 | 给测试服务加高基数标签 | `head_series` 飙升、内存上涨 | relabel 丢弃/回滚发布 | 序列回落到预算内 |
| WAL 损坏 | 模拟断电/截断 WAL 尾部 | 启动失败/恢复日志 | 用快照恢复 | 数据完整性、查询正常 |
| 远端积压 | 断开 remote write 网络 | 队列满、发送失败 | 恢复网络/扩容队列 | 队列清空、无丢弃 |
| 告警风暴 | 批量触发测试告警 | 通知量激增、分组失效 | 抑制/静默/回滚规则 | 通知量恢复、关键告警可达 |

### 51.4.2 目标宕机演练（可用 Mini 复现）

```bash
# 终端一：启动 exporter
python examples/demo_exporter.py --port 9100

# 终端二：启动 Mini
python -m mini_prometheus \
  --config.file=examples/config/capstone.yml \
  --storage.tsdb.path=./data-drill \
  --web.listen-address=127.0.0.1:9090

# 终端三：确认健康
curl -s 'http://127.0.0.1:9090/api/v1/query?query=up' | python -m json.tool

# 停掉 exporter（Ctrl+C），等待一个抓取周期后查询
curl -s 'http://127.0.0.1:9090/api/v1/query?query=up' | python -m json.tool
```

预期：

- `up{job="demo"}` 从 1 变为 0；
- 目标 `health` 变为 `down`，`lastError` 显示连接失败；
- 如果配置了 `DemoTargetDown` 类告警，`/api/v1/alerts` 出现 pending/firing；
- 恢复 exporter 后 `up` 回到 1，告警 resolve。

更完整的失败语义（stale、HTTP 500、append 错误）见第 12 章与 `examples/08_staleness_and_failures.py`。

### 51.4.3 基数爆炸演练

在真实 Prometheus 上：

```bash
# 给测试服务加一个高基数标签并发布
# 观察 5 分钟
curl -s http://prometheus:9090/api/v1/status/tsdb | python -m json.tool
```

关注：

- `seriesCountByMetricName` 的前几名；
- `labelValueCountByLabelName` 中异常标签；
- `prometheus_tsdb_head_series` 曲线；
- 查询延迟与内存。

止血顺序：

1. 回滚产生高基数的发布/配置；
2. 用 `metric_relabel_configs` 在抓取层丢弃该标签/指标；
3. 如果 head 已爆，临时缩短 retention/重启并接受数据缺口；
4. 事后加基数预算与 CI 检查。

### 51.4.4 WAL 损坏演练

真实 Prometheus 的恢复流程：

```bash
# 停止进程，备份数据目录
cp -a data data.backup

# 用 promtool 检查 TSDB
promtool tsdb analyze data

# 如果 WAL 尾部损坏，优先用快照恢复
# 不要手工编辑 WAL；按官方恢复文档操作
```

Mini 的 WAL 有 CRC 与尾部截断逻辑（第 34 章），可以用测试复现：

```bash
python -m pytest tests/test_wal.py -q
python -m pytest tests/test_tsdb.py -q
```

演练重点：

- 恢复后哪些时间段的数据会丢；
- 告警是否会因为数据缺口误报；
- 备份是否真的可恢复（定期演练，而不是只看备份任务成功）。

### 51.4.5 远端积压演练

```bash
# 在 staging 断开 remote write 目标（防火墙/NetworkPolicy）
# 观察
curl -s http://prometheus:9090/api/v1/query?query=prometheus_remote_storage_queue_highest_sent_timestamp_seconds
curl -s http://prometheus:9090/api/v1/query?query=prometheus_remote_storage_samples_dropped_total
```

预期：

- 队列容量上升、发送重试增加；
- 超过 `capacity` 后开始丢弃样本；
- 恢复网络后队列逐步清空，但丢弃的样本无法补回。

止血：

- 恢复网络；
- 临时增大 `max_shards`/`capacity`（注意内存）；
- 降低非关键指标的发送量；
- 事后评估是否需要本地缓冲/多后端。

### 51.4.6 告警风暴演练

注入：

- 批量触发测试告警；
- 或者故意让一个高基数规则产生大量不同标签的告警。

观察：

- Alertmanager 通知量；
- 分组是否把同一根因合并；
- 抑制是否生效；
- 关键告警是否被淹没。

止血：

- 加抑制/分组；
- 静默非关键告警（有到期时间）；
- 回滚规则；
- 事后删除/合并低价值告警。

## 51.5 事故响应实战

### 51.5.1 角色

| 角色 | 职责 |
|---|---|
| Incident Commander | 决策、协调、对外同步，不亲自修 |
| Operations/SME | 执行回滚/扩容/切流 |
| Communications | 对内/对外更新，管理状态页 |
| Scribe | 记录时间线、操作、证据 |

小团队可以兼任，但必须明确谁在决策。

### 51.5.2 时间线示例

```text
T+0    告警：CheckoutAvailabilityBudgetBurnFast（page）
T+2    值班确认；/targets 显示 gateway 目标大量 down
T+5    发现最近一次发布改了 ServiceMonitor 的 namespaceSelector
T+8    IC 决定回滚发布；Communications 更新状态页
T+12   目标恢复，up 回到 1；错误率开始下降
T+20   SLO 燃烧率回到阈值内；告警 resolve
T+30   确认无数据缺口；保留现场证据；安排复盘
```

关键原则：

- 先止血，再找根因；
- 每次操作记录时间与结果；
- 不要在事故中做未验证的高风险变更；
- 对外沟通区分"已知事实"与"假设"；
- 保留日志/指标/配置/变更记录。

### 51.5.3 常见决策点

- 回滚还是前滚修复？
- 切流还是扩容？
- 静默告警还是修规则？
- 是否升级到更高 severity？
- 是否通知业务/管理层/客户？

这些决策应写进 runbook，而不是临场发挥。

## 51.6 复盘与改进

### 51.6.1 无责复盘模板

```text
标题：
日期/时长：
影响：用户/收入/SLO/范围
时间线：关键事件与操作
根因：技术/流程/组织
做对了什么：
做错了什么：
监控/告警缺口：
改进项：owner + 截止时间 + 验收标准
回归测试：新增了哪些测试/演练
```

### 51.6.2 改进项要可验证

不好的改进项：

```text
加强监控
提高意识
优化流程
```

好的改进项：

```text
为 ServiceMonitor 的 namespaceSelector 增加 CI 校验，
并在 staging 增加"目标数为 0"的告警；owner=平台组；截止=两周；
验收=故意写错 namespace 时 CI 失败、staging 告警触发。
```

### 51.6.3 把事故变成测试

每次事故至少产出一个：

- 规则单元测试；
- 集成/演练脚本；
- CI 校验；
- 监控指标/告警；
- runbook 更新。

没有回归测试的"改进项"很可能在下一次变更中复发。

## 51.7 运行手册与 SLO 评审

### 51.7.1 Runbook 模板

```text
告警名：
SLO/影响：
症状：
立即检查：
  1. /targets、/rules、/alerts
  2. 关键 dashboard（链接）
  3. 最近变更（发布/配置/证书）
止血：
  - 回滚命令/链接
  - 降级/切流/扩容
根因排查：
  - 指标/日志/追踪入口
升级：
  - 何时找谁
恢复验证：
  - SLI 恢复、队列清空、无数据缺口
复盘：
  - 模板链接
```

### 51.7.2 SLO 评审节奏

| 频率 | 内容 | 参与人 |
|---|---|---|
| 每周 | 告警量/噪音、预算消耗、未完成改进项 | SRE/on-call |
| 每月 | SLO 达标、容量趋势、成本 | 平台/业务 owner |
| 每季度 | SLO 目标校准、告警规则清理、演练 | 业务/平台/管理 |
| 每次重大变更 | SLI 是否仍有效、dashboard/runbook 是否更新 | 服务 owner |

评审问题：

- SLI 是否仍覆盖关键用户旅程？
- 分母/排除项是否仍合理？
- 预算策略是否被团队接受？
- 告警是否可行动、是否有 owner/runbook？
- 是否有"从未触发"或"一直触发"的规则？
- 成本是否在预算内，增长是否可解释？

## 51.8 交付物与验收清单

### 51.8.1 交付物

```text
[ ] 监控设计文档（SLI/SLO/指标模型/基数预算）
[ ] Prometheus/Alertmanager 配置与规则（Git）
[ ] dashboard（JSON，版本化）
[ ] runbook（每个 page 告警）
[ ] 容量报告与扩容触发条件
[ ] 压测报告（方法/结果/瓶颈/结论）
[ ] 故障演练报告（场景/信号/止血/改进）
[ ] 复盘模板与最近一次复盘
[ ] on-call 手册与升级路径
[ ] 合规/保留/访问控制说明（如适用）
```

### 51.8.2 验收标准

```text
[ ] 关键 SLI 可在 dashboard/API 查询，且与业务对账一致
[ ] 所有 page 告警有 owner、runbook、演练记录
[ ] 目标 up、规则 health、remote write 队列有自监控
[ ] 备份恢复演练通过，RPO/RTO 明确
[ ] 容量达到预算 80% 时有告警/扩容流程
[ ] 一次目标宕机/基数爆炸/远端积压演练通过
[ ] 配置变更走 CI + review，可回滚
[ ] SLO 评审有固定节奏与记录
```

## 51.9 用 Mini 做一次完整演练

下面这条链路可以在本地 10 分钟内跑完，覆盖"抓取 → 规则 → 告警 → 存储 → 查询 → 恢复"。

### 51.9.1 启动

```bash
# 终端一
python examples/demo_exporter.py --port 9100

# 终端二
python -m mini_prometheus \
  --config.file=examples/config/capstone.yml \
  --storage.tsdb.path=./data-drill \
  --web.listen-address=127.0.0.1:9090
```

### 51.9.2 验证正常态

```bash
curl -s 'http://127.0.0.1:9090/api/v1/query?query=up' | python -m json.tool
curl -s 'http://127.0.0.1:9090/api/v1/query?query=job:demo_requests:rate1m' | python -m json.tool
curl -s 'http://127.0.0.1:9090/api/v1/rules' | python -m json.tool
```

### 51.9.3 注入故障

停掉 exporter，等待 2～3 个抓取周期：

```bash
curl -s 'http://127.0.0.1:9090/api/v1/query?query=up' | python -m json.tool
curl -s 'http://127.0.0.1:9090/api/v1/targets' | python -m json.tool
curl -s 'http://127.0.0.1:9090/api/v1/alerts' | python -m json.tool
```

观察：

- `up` 变为 0；
- target `health=down`，`lastError` 有内容；
- 告警状态变化（取决于规则与 `for`）。

### 51.9.4 恢复与存储检查

重启 exporter，再等待一个周期：

```bash
curl -s 'http://127.0.0.1:9090/api/v1/query?query=up' | python -m json.tool

# 查看 block 与 head checkpoint
python -m mini_prometheus tsdb list --storage.tsdb.path ./data-drill

# 分析 block 大小/样本数
python -m mini_prometheus tsdb analyze --storage.tsdb.path ./data-drill
```

### 51.9.5 规则回归

```bash
python -m mini_prometheus.promtool test rules examples/rules/capstone_test.yml
python -m pytest -q
```

### 51.9.6 清理

```bash
# 停止 Mini 后删除演练数据
rm -rf ./data-drill
```

不要把演练数据目录提交到 Git；本仓库的 `.gitignore` 已忽略 `data*/` 与 `__pycache__/`。

## 本章小结

- 平台演进按阶段走：单机 → HA → 多集群 → 全局查询/多租户，每级先补齐运维能力。
- GitOps 让配置/规则/dashboard/runbook 可审计、可回滚；CI 必须阻止语法错误和规则回归。
- 容量验证要用真实工作负载形状，不能只看单条查询。
- 故障演练要验证告警、runbook、止血和恢复，而不只是"制造故障"。
- 事故响应先止血、再根因；复盘改进项必须有 owner、期限和验收标准。
- Mini-Prometheus 可以在本地复现目标宕机、stale、规则求值与存储生命周期，是低成本演练场。

## 延伸阅读

- 本教程第 18～24 章（远程读写、HA、运维、K8s）、第 43 章（从 Mini 到生产）。
- 本教程第 46 章（部署与 GitOps）、第 48 章（基准与容量验证）、第 49 章（合规/生命周期）。
- 附录 E（故障排查）、附录 S（事故案例集）、附录 U（自监控）、附录 Y（压测手册）、附录 AE（3.14–3.16 迁移）。
- Google SRE Workbook：Incident Response、Postmortem Culture。

# 第 23 章 运维：配置、备份、升级与故障排查

## 23.1 配置即代码

Prometheus 配置应：

- 存在 Git 中；
- 通过 CI 校验；
- 通过 GitOps/配置管理下发；
- 有变更评审；
- 可回滚。

CI 步骤：

```bash
promtool check config prometheus.yml
promtool check rules rules/*.yml
```

Mini：

```bash
python -m mini_prometheus check config examples/config/prometheus.yml
python -m mini_prometheus check rules examples/rules/demo.yml
```

检查内容：

- YAML 语法；
- duration 合法；
- job 名唯一；
- relabel 动作/参数；
- 规则表达式可解析；
- 规则类型匹配。

## 23.2 重载

```bash
curl -X POST http://127.0.0.1:9090/-/reload
```

或：

```bash
kill -HUP $(pidof prometheus)
```

前提：

- `--web.enable-lifecycle`（HTTP reload）；
- 配置校验通过；
- 进程有权限读取新文件/secret。

重载失败时：

```promql
prometheus_config_last_reload_successful == 0
```

查日志中的 `Error loading config`。

注意：

- reload 会重建 scrape targets/rules，可能造成短暂缺口；
- 规则状态（pending 计时）会重置；
- 大规模配置重载可能造成 CPU 尖峰。

## 23.3 备份与恢复

备份对象：

- 配置/规则/secret；
- TSDB 快照（一致性）；
- Grafana 面板/告警规则。

快照：

```bash
curl -X POST http://127.0.0.1:9090/api/v1/admin/tsdb/snapshot
```

返回：

```json
{"status":"success","data":{"name":"20261126T100000Z-..."}}
```

恢复：

1. 停止 Prometheus；
2. 把快照目录内容放到 `data/` 或 `blocks/`；
3. 确保权限/属主；
4. 启动；
5. 验证 `/api/v1/status/tsdb` 与查询。

不要直接复制运行中的 `data/`；WAL/block 可能不一致。

## 23.4 升级

升级前：

- 读 release notes / changelog；
- 备份配置/TSDB；
- 在 staging 用相同数据量验证；
- 检查 flag 弃用/移除；
- 检查 PromQL 行为变化；
- 检查 remote write 接收端兼容性。

Prometheus 3.x 升级的四个高发点：

1. **区间/lookback 左开**：`[5m]` 与 instant selector 不再包含左边界样本；`count_over_time` 可能少一个点，`[1m:1m]` 子查询只产生一个点，`rate()` 直接无数据。迁移时优先搜 `[...:...]` 子查询。
2. **函数更名/弃用**：`holt_winters` 在 3.0 被移除并改名为 `double_exponential_smoothing`（上游**没有**保留旧名别名），且需要 `--enable-feature=promql-experimental-functions`；继续写旧名会解析失败。部分函数的边界行为也有调整。
3. **UTF-8 指标名**：3.0 允许引号指标名，但老面板/告警/导出链路未必兼容；默认仍建议经典命名。
4. **remote write 接收端**：确认接收端支持 3.x 的 metadata/native histogram 版本；否则显式关闭发送端的新特性。

升级顺序（HA）：

1. 升级副本 B；
2. 验证抓取/查询/告警；
3. 升级副本 A；
4. 观察 24h。

回滚：

- 保留旧二进制；
- 旧版本可能无法读取新版本 TSDB 格式（block version）；
- 回滚前确认兼容性，必要时从快照恢复。

## 23.5 常见故障

| 症状 | 可能原因 | 排查 |
|---|---|---|
| `up == 0` | 目标不可达/认证/路径 | `/targets` 的 lastError |
| 查询为空 | 时间/标签/指标名 | `/series`、`/labels` |
| 内存持续增长 | 高基数/慢查询 | head series、查询日志 |
| 磁盘满 | retention/compaction | blocks bytes、磁盘 |
| reload 失败 | YAML/权限 | 日志、`promtool check` |
| 规则不触发 | 表达式/for/数据 | `/rules` health/lastError |
| 告警不发送 | Alertmanager/网络 | notifier 指标/日志 |
| 启动失败 | 锁/损坏/权限 | 日志、`tsdb analyze` |
| 查询超时 | 范围/基数/并发 | max-samples、慢查询 |
| 时间戳错乱 | honor_timestamps/时钟 | target 配置、NTP |

## 23.6 TSDB 损坏

症状：

- 启动时 `open block: ...`；
- 查询报 `invalid index`；
- `promtool tsdb analyze` 失败。

处理：

1. 停止 Prometheus；
2. 备份 `data/`；
3. 定位损坏 block（日志/analyze）；
4. 删除该 block（丢失该时间段数据）；
5. 从快照/远端恢复；
6. 检查磁盘 SMART/文件系统。

预防：

- 正常关机；
- 避免 NFS/网络文件系统；
- 留足磁盘；
- 监控 compaction 失败。

## 23.7 性能排查顺序

1. **确认是抓取、查询还是规则**：
   - `scrape_duration_seconds` P99；
   - 查询日志/慢查询；
   - `rule_evaluation_duration_seconds`。
2. **看基数**：`head_series`、`seriesCountByMetricName`。
3. **看资源**：CPU/内存/磁盘 IO/网络。
4. **看并发**：查询并发、抓取并发、规则并发。
5. **看 compaction**：是否堆积/失败。
6. **看 remote write**：队列积压/丢弃。

## 23.8 日志

关键日志：

```text
level=error msg="Error loading config"
level=warn  msg="Error on ingesting samples"
level=error msg="Failed to send batch"
level=warn  msg="Scrape target ... failed"
level=error msg="compact blocks"
```

建议：

- 集中收集 Prometheus 日志；
- 对 error 日志做告警；
- 不要长期开 debug（量大）。

## 23.9 Mini 的运维命令

```bash
# 校验配置
python -m mini_prometheus check config examples/config/prometheus.yml

# 校验规则
python -m mini_prometheus check rules examples/rules/demo.yml

# 查看 TSDB block
python -m mini_prometheus tsdb list --storage.tsdb.path ./data

# 分析
python -m mini_prometheus tsdb analyze --storage.tsdb.path ./data

# 查询
python -m mini_prometheus query instant http://127.0.0.1:9090 'up'
```

Mini 实现了 `promtool` 的 `check rules` 与 `test rules` 教学子集；没有完整子命令（如 `tsdb dump`、JUnit 输出、全部 YAML 校验），但覆盖了日常校验。

## 23.10 升级策略与兼容性

升级前：

```text
[ ] 读 release notes/changelog（breaking changes、flag 移除、默认值变化）
[ ] 确认目标版本是否为 LTS/受支持
[ ] 检查 feature flag 是否转正/移除
[ ] 检查 TSDB 格式/索引兼容性
[ ] 备份快照 + 配置
[ ] staging 同数据量验证
[ ] 规则/面板/API 回归
[ ] remote write 接收端兼容
[ ] 回滚方案（二进制/镜像/配置）
```

升级顺序：

```text
1. 从 HA 的一个副本开始（停止 → 替换 → 启动 → 验证）
2. 观察抓取/查询/规则/远端
3. 再升级另一个副本
4. 最后升级单点组件（Alertmanager/查询层）
```

注意：

- 不要让两个版本长期混跑（格式/语义差异）；
- 大版本升级前做快照；
- 升级后检查 `/api/v1/status/buildinfo`、`/metrics`、日志 warning；
- 规则 `for` 状态可能重置；
- 原生直方图/RW2 的兼容性要单独验证。

## 23.11 配置重载的边界

哪些能 reload：

- `scrape_configs`（目标/relabel）；
- `rule_files`；
- `alerting`；
- `remote_write`/`remote_read`；
- `global` 的多数参数。

哪些通常需要重启：

- 存储路径/retention/block 参数；
- Web TLS/认证（部分版本支持 reload）；
- 监听地址/端口；
- 某些 feature flag；
- 查询引擎/TSDB 的启动参数。

reload 的副作用：

- 目标重建，短暂抓取缺口；
- 规则组重建，pending 计时重置；
- remote write 队列重建，可能重发/丢样本；
- CPU/内存尖峰。

失败处理：

```promql
prometheus_config_last_reload_successful == 0
```

看日志、`/api/v1/status/config`，修复后再次 reload；不要反复 reload 掩盖错误。

## 23.12 日常巡检

每日：

```promql
# 目标下线
up == 0
# 抓取变慢
scrape_duration_seconds > 5
# 规则失败
prometheus_rule_evaluation_failures_total > 0
# 远端积压
prometheus_remote_storage_samples_pending > 10000
# 磁盘/序列
prometheus_tsdb_head_series
prometheus_tsdb_storage_blocks_bytes
```

每周：

- 容量趋势（序列/磁盘/内存）；
- 告警噪音 Top10；
- 慢查询/慢规则；
- 证书/凭证过期；
- 备份恢复演练；
- 版本/安全公告。

每月：

- 容量评审；
- 权限/访问评审；
- 过期规则/指标清理；
- 故障演练/复盘。

## 23.13 事故响应 runbook

模板：

```text
告警名：
影响：
症状：
立即检查：
  1. /targets、/rules、/alerts
  2. 关键指标（up、head_series、query duration、remote write）
  3. 最近变更（配置/版本/发布）
缓解：
  - 回滚配置/版本
  - 降级查询/限制并发
  - 扩容/切流
  - 静默非关键告警
恢复验证：
  - 关键 SLI 恢复
  - 队列清空
  - 无数据缺口
升级/沟通：
  - 何时通知业务/管理层
  - 状态页/事故群
复盘：
  - 时间线/根因/改进项
```

原则：

- 先止血，再找根因；
- 不要在生产做未验证的高风险操作；
- 记录每一步时间和结果；
- 事后补回归测试/监控。

## 23.14 自动化运维

| 任务 | 工具 |
|---|---|
| 配置下发 | GitOps/Argo CD/Flux/Ansible |
| Kubernetes 部署 | Operator/Helm/Kustomize |
| 校验 | promtool/pint/CI |
| 备份 | cron + snapshot + 对象存储 |
| 证书轮换 | cert-manager/Vault |
| 容量报表 | PromQL + Grafana/脚本 |
| 告警巡检 | amtool/API |
| 升级 | 镜像/Helm/Operator + 灰度 |

自动化原则：

- 所有变更可审计、可回滚；
- 生产变更走流水线，不手工改；
- 自动化失败要告警，不要静默；
- 定期演练恢复/回滚。

## 23.15 运维检查表

```text
[ ] 配置/规则在 Git，CI 校验
[ ] 快照/备份 + 恢复演练
[ ] 升级/回滚流程
[ ] 自监控 + 告警
[ ] 容量趋势/扩容触发
[ ] 证书/Secret 轮换
[ ] 访问控制/审计
[ ] runbook 覆盖 page 告警
[ ] 故障演练/复盘
[ ] 版本/安全公告跟踪
```

## 本章小结

- 配置/规则必须进 CI，用 `promtool check` 或 Mini 的 check 子命令。
- 备份用快照，不要直接复制运行中的数据目录。
- 升级先读 changelog、先 staging、先 HA 从库。
- 故障排查顺序：抓取 → 基数 → 资源 → 并发 → compaction → remote write。
- 每个 page 告警配 runbook。

## 延伸阅读

- Prometheus 官方 "Operational aspects"。
- `promtool` 文档。
- 本教程第 22 章（容量）、附录 AC（工具链）、附录 E（故障排查手册）与附录 U（自监控/元监控）。

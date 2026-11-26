# 附录 E 故障排查手册

## E.1 先确认范围

```text
1. 是 Prometheus 自身问题，还是目标/查询/告警问题？
2. 影响所有 job 还是单个 job？
3. 是"没有数据"还是"数据错误"？
4. 最近是否有配置/版本/发布变更？
5. 是持续还是间歇？
```

## E.2 快速命令

```bash
curl -s localhost:9090/-/healthy
curl -s localhost:9090/-/ready
curl -s localhost:9090/api/v1/targets | jq '.data.activeTargets[] | {job:.labels.job,instance:.labels.instance,health,lastError}'
curl -s localhost:9090/api/v1/rules | jq '.data.groups[].rules[] | {name,health,lastError}'
curl -s localhost:9090/api/v1/alerts | jq '.data.alerts[]'
curl -s 'localhost:9090/api/v1/query?query=up==0'
curl -s localhost:9090/metrics | grep -E 'prometheus_tsdb_head_series|prometheus_tsdb_storage_blocks_bytes'
```

## E.3 目标 up=0

```text
1. /targets 的 lastError
2. 从 Prometheus 主机 curl 目标 /metrics
3. DNS/端口/防火墙/网络策略
4. scheme/path/params 配置
5. TLS/认证/证书
6. relabel 是否把 __address__ 改错
7. 目标是否要求特殊 header/压缩
```

常见错误：

```text
connection refused
context deadline exceeded
server returned HTTP status 403
x509: certificate signed by unknown authority
```

## E.4 查询为空

```text
1. 时间参数是否在数据范围内？
2. 指标名/标签是否写错？
3. 是否被 stale 标记？
4. 是否 lookback 过期？
5. 标签匹配是否全匹配/大小写？
6. 分母/匹配是否导致空结果？
```

排查：

```bash
curl -s 'localhost:9090/api/v1/series?match[]={__name__=~".+"}'
curl -s 'localhost:9090/api/v1/labels'
curl -s 'localhost:9090/api/v1/label/job/values'
```

## E.5 查询慢/超时

```text
1. 命中序列数/时间范围
2. 是否全匹配正则
3. 是否高基数 group by
4. 是否子查询套子查询
5. 是否并发查询过多
6. max-samples/timeout 是否触发
```

优化：

- 加 matcher；
- 缩短 range；
- 用 recording rules；
- 拆分面板；
- 限制并发。

## E.6 内存高/OOM

```text
1. head_series 是否异常增长
2. scrape_series_added 是否突增
3. 是否有高基数标签
4. 是否有慢查询/大范围查询
5. 规则是否产生大量中间结果
6. 是否 compaction 堆积
```

应急：

- 回滚最近发布；
- metric_relabel drop 高基数指标；
- 降低查询并发/限制；
- 扩容；
- 重启（最后手段，会丢未 fsync 数据）。

## E.7 磁盘满

```promql
prometheus_tsdb_storage_blocks_bytes
prometheus_tsdb_compactions_failed_total
```

处理：

- 缩短 retention；
- 删除旧 block（谨慎）；
- 扩容；
- 检查 compaction 临时空间；
- 检查 WAL 大小。

## E.8 规则不触发

```text
1. /rules 的 health/lastError
2. 表达式在当前时间是否有数据
3. for 是否未到
4. 标签是否变化导致新告警
5. 告警是否被 Alertmanager 抑制/静默
6. notifier 是否失败
```

查询中间结果：

```promql
# 直接跑规则表达式
<expr>
```

## E.9 告警不发送

```text
1. Prometheus /alerts 是否 firing
2. notifier 指标：errors/queue/dropped
3. Alertmanager 可达性/认证
4. Alertmanager 路由/抑制/静默
5. 通知渠道配置
```

## E.10 启动失败

```text
1. 配置校验：promtool check config
2. 端口占用
3. 数据目录权限/锁
4. WAL/block 损坏
5. 版本不兼容
```

日志关键字：

```text
Error loading config
open TSDB
lock file
corruption
```

## E.11 Prometheus 自身监控

```promql
up{job="prometheus"}
prometheus_tsdb_head_series
prometheus_tsdb_storage_blocks_bytes
prometheus_tsdb_compactions_failed_total
prometheus_rule_evaluation_failures_total
prometheus_notifications_errors_total
prometheus_target_scrape_pool_sync_total
process_resident_memory_bytes
```

## E.12 决策树：没有数据

```text
查询为空
├─ /targets up=0 → 抓取问题（E.3）
├─ /targets up=1
│  ├─ /series 有序列 → 时间/标签/查询问题
│  └─ /series 无序列 → 解析/relabel/指标名问题
└─ 目标已消失 → stale/lookback/SD 问题
```

## E.13 决策树：告警噪音

```text
告警太多
├─ 同一故障多个告警 → 分组/抑制
├─ 误报 → 阈值/for/多窗口
├─ 不可行动 → 删除/降级
├─ 重复通知 → HA labels/路由
└─ 长期静默 → 修复根因
```

## E.14 抓取慢/目标过多

症状：

```promql
prometheus_target_scrape_pool_targets
prometheus_target_scrape_pool_sync_total
scrape_duration_seconds{job="..."} > 5
scrape_samples_scraped{job="..."} 异常大
```

排查：

```text
1. 单目标 /metrics 的大小/耗时（curl -w）
2. 目标数 × 并发是否超过抓取池能力
3. 是否有慢 exporter/数据库查询
4. scrape_timeout 是否小于实际耗时
5. DNS/连接复用/代理
6. 是否所有目标都抓同一大指标
```

处理：

- 降低目标数/分片（hashmod）；
- 增大 `scrape_interval`/`scrape_timeout`；
- 优化 exporter（缓存/分页）；
- 用 `sample_limit`/`body_size_limit` 保护；
- 把重采集拆到独立 job/实例。

## E.15 Remote Write 积压

```promql
prometheus_remote_storage_samples_pending
prometheus_remote_storage_samples_retries_total
prometheus_remote_storage_shards
prometheus_remote_storage_queue_highest_sent_timestamp_seconds
```

排查：

```text
1. 远端是否 429/5xx/超时
2. 队列 capacity/shards 是否太小
3. 网络带宽/延迟
4. 发送的样本是否突增
5. 是否有慢租户/大标签
6. 远端限流/配额
```

处理：

- 增大 `max_shards`/`capacity`（注意内存）；
- 降低发送量（drop/聚合）；
- 修远端；
- 多队列/多租户隔离；
- 监控 `highest_sent_timestamp` 与 now 的差距。

## E.16 TSDB 损坏

```text
症状：
- 启动失败：invalid index / corruption / unexpected EOF
- 查询报错：postings not found
- compaction 反复失败
```

处理：

```bash
# 1. 停写/备份
# 2. 列出 block
promtool tsdb list /var/lib/prometheus
# 3. 分析
promtool tsdb analyze /var/lib/prometheus
# 4. 定位损坏 block（日志/时间范围）
# 5. 删除损坏 block（会丢该时间段数据）或从快照/远端恢复
# 6. 重启，验证
```

不要：

- 直接 `rm -rf blocks/*`；
- 在运行时 `clean-tombstones`；
- 忽略磁盘/文件系统错误。

## E.17 配置重载失败

```text
1. promtool check config 先校验
2. 看 /api/v1/status/config 与日志
3. 检查 Secret/证书/文件权限
4. 检查 rule_files glob 是否匹配
5. 检查 SD 配置（K8s RBAC/API 不可达）
6. 重载失败时旧配置是否仍生效
```

注意：

- `/-/reload` 需要 `--web.enable-lifecycle`；
- SIGHUP 也可触发；
- 重载失败不会自动回滚，但通常保留旧配置；
- 规则文件错误会导致整组规则不加载。

## E.18 Grafana 有数据、API 没有（或相反）

```text
1. 数据源指向的 Prometheus 是否同一个
2. Grafana 查询是否加了 interval/step 调整
3. 时间范围/时区
4. Grafana 缓存/变量
5. 代理/多租户头
6. 查询是否用了 recording rule（API 直查原始指标可能为空）
7. 是否是远端读/全局视图
```

用 `promtool query` 或 curl 直接打 API，排除 Grafana 变量/面板逻辑。

## 延伸阅读

- 本教程第 23 章（运维）。
- Prometheus 官方 troubleshooting 文档。

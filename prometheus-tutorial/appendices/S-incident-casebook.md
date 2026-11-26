# 附录 S 生产事故案例集

本附录把前面各章的语义落到真实故障上。每个案例按"现象 → 定位 → 根因 → 修复 → 预防"组织，重点不是背命令，而是建立**从症状反推层次**的习惯：先判断是抓取、存储、查询、规则还是告警层，再选指标和工具。

案例中的指标名/端点以 Prometheus 3.x 为主；不同版本的 flag/指标可能略有差异，以实际版本为准。

## S.1 基数爆炸：一个 request_id 标签打爆 head

**现象**

- Prometheus 内存从 8GB 涨到 30GB，随后 OOM；
- `prometheus_tsdb_head_series` 从 80 万涨到 1200 万；
- 查询变慢，规则求值超时。

**定位**

```promql
prometheus_tsdb_head_series
sum by (job) (rate(scrape_series_added[5m]))
```

`scrape_series_added` 在某个 job 发布后突增。用 `/api/v1/status/tsdb` 看 `seriesCountByMetricName` 和 `seriesCountByLabelValuePair`，发现 `http_requests_total` 带 `request_id`。

**根因**

新版本把请求 ID 放进了指标标签。每个请求一条序列，基数无上限。

**修复**

1. 回滚应用版本；
2. 在 `metric_relabel_configs` 临时 drop `request_id`；
3. 清理旧序列（delete_series + clean_tombstones），或等待 retention；
4. 代码中删除该标签。

**预防**

- 指标评审：标签值必须低基数、可枚举；
- staging 发布前比较 `scrape_series_added`；
- 序列预算 + 基数告警；
- 把请求 ID 放进日志/追踪，不进指标。

## S.2 抓取超时风暴：exporter 在 collect 里做远程调用

**现象**

- 大量目标 `up=1` 但 `scrape_duration_seconds` 接近 `scrape_timeout_seconds`；
- 部分抓取超时，`up=0`；
- Prometheus CPU 高，抓取队列积压。

**定位**

```promql
topk(20,
  scrape_duration_seconds
  /
  scrape_timeout_seconds
)
```

**根因**

exporter 的 `collect()` 每次 `/metrics` 都去查数据库/调用下游；下游变慢时，所有抓取一起超时。

**修复**

- exporter 改为后台缓存/定期刷新，`collect()` 只读内存；
- 增加超时/熔断；
- 拆分慢指标到独立 exporter。

**预防**

- 监控 `scrape_duration_seconds / scrape_timeout_seconds` 的 P99；
- 超过 0.8 告警；
- exporter 代码评审关注 `collect()` 的阻塞操作。

## S.3 WAL 损坏：硬重启后无法启动

**现象**

- Prometheus 启动失败，日志 `WAL corruption` / `unexpected EOF`；
- `/-/ready` 不返回 200。

**定位**

- 查看日志中的 segment/offset；
- 备份 `data/`；
- 用 `promtool tsdb analyze` 检查；
- 确认最近是否有 OOM kill、磁盘满、非正常关机。

**根因**

WAL 尾部写入中断/磁盘满/文件系统错误。Prometheus 对中间损坏通常拒绝启动，避免静默丢数据。

**修复**

1. 备份数据目录；
2. 按官方文档截断损坏的 WAL 尾部，或从快照恢复；
3. 如果无法恢复，删除损坏 block/段（会丢对应时间范围数据）；
4. 检查磁盘 SMART/文件系统。

**预防**

- 正常关机/滚动重启；
- 不用 NFS/网络文件系统；
- 留足磁盘；
- 定期快照 + 恢复演练；
- 监控 `prometheus_tsdb_wal_*` 和磁盘。

## S.4 磁盘满：compaction 临时空间不够

**现象**

- 磁盘使用率 100%，写入失败；
- `prometheus_tsdb_compactions_failed_total` 增长；
- 查询报错/只读。

**定位**

```promql
node_filesystem_avail_bytes
prometheus_tsdb_storage_blocks_bytes
rate(prometheus_tsdb_compactions_failed_total[5m])
```

**根因**

compaction 需要临时空间重写 block；磁盘只剩不到一个 block 的空间，compaction 失败，旧 block 又无法删除，形成死锁。

**修复**

- 扩容磁盘/清理无关文件；
- 降低 retention/retention.size；
- 临时停止写入/减少抓取；
- 从快照恢复（如果数据损坏）。

**预防**

- 磁盘预留 20%～30%；
- 监控 `predict_linear` 和 compaction 失败；
- 不要把 retention 设得刚好等于磁盘容量；
- 大 block 集群提前规划 compaction 空间。

## S.5 告警风暴：标签变化导致告警身份重置

**现象**

- 一次发布后，同一故障产生数百条通知；
- Alertmanager 分组失效；
- 值班被淹没。

**定位**

- 看 `/api/v1/alerts` 的标签；
- 比较发布前后告警标签；
- 检查是否新增了 `pod`/`instance`/`version` 等高基数标签。

**根因**

告警身份由完整标签集合决定；新增标签或标签值变化会让 `for` 重新计时，并产生新的 fingerprint。Alertmanager 无法把"同一故障"聚合。

**修复**

- 告警规则里 `sum by` 到稳定维度（service/cluster/severity）；
- 删除告警标签中的动态维度；
- 用 `alert_relabel_configs` 清理；
- 调整 `group_by`。

**预防**

- 告警标签评审：只保留路由/分组需要的维度；
- 规则测试覆盖标签变化；
- 发布前检查 `ALERTS` 序列数。

## S.6 Remote write 积压：网络分区后队列满

**现象**

- 远端数据落后；
- `prometheus_remote_storage_samples_pending` 高；
- `prometheus_remote_storage_samples_dropped_total` 增长；
- 本地 WAL 增长。

**定位**

```promql
prometheus_remote_storage_samples_pending
prometheus_remote_storage_samples_dropped_total
prometheus_remote_storage_queue_highest_sent_timestamp_seconds
prometheus_remote_storage_shards_desired
prometheus_remote_storage_shards_max
```

比较 `highest_sent_timestamp` 与当前时间；看 `shards_desired` 是否顶到 `max`。

**根因**

网络分区/远端限流导致发送失败；队列是内存的，容量有限；WAL watcher 被背压，最终丢弃/落后。

**修复**

- 恢复网络/远端；
- 提高 `max_shards`/`capacity`（注意内存）；
- 降低采样/基数；
- 如果 WAL 已被 checkpoint 覆盖，未发送样本可能永久丢失。

**预防**

- 监控 pending/dropped/highest_sent；
- 远端多副本/多区域；
- 队列容量与 WAL 保留匹配；
- 定期演练网络分区。

## S.7 HA 去重失效：external labels 不一致

**现象**

- 查询结果出现双倍序列/抖动；
- `rate()` 结果约为单副本的两倍；
- 告警重复。

**定位**

- 比较两个副本的 `external_labels`；
- 看远端序列是否带不同的 `cluster`/`replica`；
- 检查查询层 `--query.replica-label`。

**根因**

两个副本的 `cluster`/`region` 等 external label 不一致，远端把它们视为不同序列；或者 `replica` 标签进入了告警标签。

**修复**

- 统一 external labels（除 replica）；
- 查询层配置正确的 replica label；
- 告警发送前删除 replica；
- 重新写入/去重历史数据（如可行）。

**预防**

- 用配置管理保证 HA 对配置一致；
- 部署前 diff 两个副本的 `/api/v1/status/config`；
- 监控远端序列数/重复率。

## S.8 时钟漂移：honor_timestamps 导致乱序丢样本

**现象**

- 目标 `up=1`，但曲线有缺口；
- `prometheus_target_scrapes_sample_out_of_order_total` 增长；
- 日志出现 out-of-order/duplicate 警告。

**定位**

```promql
rate(prometheus_target_scrapes_sample_out_of_order_total[5m])
rate(prometheus_target_scrapes_sample_duplicate_timestamp_total[5m])
```

检查 exporter 是否带时间戳、NTP/chrony 是否同步。

**根因**

exporter 自带时间戳且时钟回拨/漂移，Prometheus 按时间戳追加，出现乱序；append 错误被丢弃，但抓取本身成功，所以 `up=1`。

**修复**

- 关闭 `honor_timestamps`（默认建议）；
- 修复 NTP；
- 清理/重建受影响序列。

**预防**

- 普通 exporter 不要暴露时间戳；
- 监控 out-of-order/duplicate 指标；
- 对自带时间戳的批量导入场景做专门校验。

## S.9 假平线：目标消失但曲线还在更新

**现象**

- 目标已经下线，Grafana 曲线还是平的；
- `up` 可能已经消失/为 0；
- 告警没有按预期触发。

**定位**

```promql
up{job="api"}
absent(up{job="api"})
last_over_time(up{job="api"}[10m])
```

看 `/api/v1/targets` 是否还有目标；看样本时间戳。

**根因**

先不要归因于 lookback。真实 Prometheus 在抓取失败/目标移除时通常会写 stale，序列会较快消失；平线更常见的原因是：

- exporter 仍在返回缓存/旧值（进程没死，指标不再更新）；
- 数据来自 remote write/推送，Prometheus 没有源端状态，不会写 stale；
- exporter 自带显式时间戳，staleness 跟踪被绕过；
- 查询范围包含目标下线前的历史样本（range query 本来就会画出来）；
- stale 标记因崩溃/重载未写入，只能等 lookback。

**修复**

- 告警用 `up == 0` 或 `absent(up{...})`；
- 面板显示 `up`/最后抓取时间；
- 用 `timestamp(metric)`/`last_over_time` 判断样本是否真的在更新；
- 检查 exporter 是否缓存旧值。

**预防**

- 区分"没有数据""值为 0""值没变"；
- 关键指标加 `absent_over_time`；
- 面板标注数据延迟；
- 对 remote write 数据额外标注"无 stale 语义"。

## S.10 直方图分位数错误：缺 +Inf 或桶不覆盖 SLO

**现象**

- `histogram_quantile` 返回 NaN 或明显偏低；
- P99 看起来很好，但用户体感很差；
- 告警不触发。

**定位**

```promql
count by (le) (http_request_duration_seconds_bucket)
histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))
```

检查是否有 `+Inf` 桶、桶边界是否覆盖 SLO、是否每个实例桶一致。

**根因**

缺 `+Inf` 返回 NaN；桶太粗/不覆盖 SLO 导致插值失真；跨实例未 `sum by (le)` 导致每个实例一个分位数。

**修复**

- 客户端补 `+Inf` 和覆盖 SLO 的桶；
- 查询改为 `sum by (le)`；
- 对最高桶以上的分位数，接受"次高有限桶上界"的保守近似，或补桶。

**预防**

- 桶设计围绕 SLO；
- 规则测试覆盖缺桶/非单调；
- 监控 `histogram_quantile` 的 NaN 比例。

## S.11 rate 为空：窗口太短/3.x 左开区间

**现象**

- 升级 Prometheus 3.x 后，部分 `rate()` 查询为空；
- `count_over_time` 少一个点；
- `[1m:1m]` 子查询没有数据。

**定位**

- 检查抓取间隔与窗口长度；
- 对比 2.x/3.x 的区间语义；
- 看样本时间戳是否恰好落在窗口左边界。

**根因**

3.x 的 range selector/lookback 是左开右闭；恰好落在 `t-range` 的样本被排除。窗口太短时，样本数不足两个，`rate()` 为空。`[1m:1m]` 子查询只产生一个点。

**修复**

- 窗口至少覆盖 4 个抓取间隔；
- 子查询窗口至少 `2 × step`；
- 迁移时搜索 `[...:...]` 子查询并逐个验证。

**预防**

- 升级前用 staging 跑规则测试；
- 规则测试锁定 3.x 语义；
- 监控规则 `health`/空结果。

## S.12 relabel 误删目标：regex 锚定/labelmap 爆炸

**现象**

- 某类目标全部消失；
- `up` 序列减少；
- `/targets` 里没有目标或标签异常。

**定位**

- 用 `promtool check config`；
- 在 staging 用 `/api/v1/targets` 对比 relabel 前后；
- 检查 `keep`/`drop` 的 regex 是否全匹配。

**根因**

Prometheus relabel regex 默认两端锚定：`node` 不匹配 `node-exporter`；`keep` 条件写反；`labelmap` 把大量 `__meta_*` 映射成业务标签，导致基数/冲突。

**修复**

- 改成 `.*node.*` 或精确匹配；
- 修正 keep/drop；
- 限制 labelmap 的 regex；
- 重新 sync/reload。

**预防**

- relabel 变更走 CI + staging；
- 保留目标数量/`up` 的基线告警；
- 不在生产直接手改。

## S.13 ServiceMonitor 不生效：selector/port/namespace 三连

**现象**

- 创建了 `ServiceMonitor`，但 Prometheus `/targets` 没有新目标；
- `kubectl get servicemonitor` 正常；
- Operator 日志无显著错误。

**定位**

```bash
kubectl -n monitoring get prometheus main -o yaml | yq '.spec.serviceMonitorSelector'
kubectl -n app get servicemonitor api -o yaml
kubectl -n app get svc api -o yaml
```

检查：

- ServiceMonitor labels 是否匹配 selector；
- `namespaceSelector` 是否包含 `app`；
- `endpoints[].port` 是否是 Service 的端口名；
- Service selector 是否选到 ready Pod。

**根因**

最常见的是 `port` 写成了端口号而不是端口名，或 Helm 的 `serviceMonitorSelectorNilUsesHelmValues` 导致只选择带 release 标签的 CR。

**修复**

- 修正 port name/labels/namespace；
- 给 ServiceMonitor 加正确标签，或调整 Prometheus selector；
- 等待 reconcile/reload。

**预防**

- 用 `kube-prometheus-stack` 的 values 明确 selector 行为；
- 新接入走模板/CI；
- 监控 `prometheus_target_scrape_pool_targets`。

## S.14 查询 OOM：30 天高基数面板

**现象**

- 打开某个 Grafana 面板后 Prometheus 内存飙升/OOM；
- 查询超时；
- 其他查询/规则受影响。

**定位**

- 看 `--query.max-samples`/`--query.timeout` 错误；
- 查看面板查询的时间范围/步长/序列数；
- 用 `topk`/`count` 估算命中序列。

**根因**

30 天范围 × 高基数 × 小 step，单个查询加载数亿样本；Grafana 的 `max data points` 没有限制住，或面板用了 instant query 画趋势。

**修复**

- 缩小时间范围/增大 step；
- 用 recording rule 预聚合；
- 限制 `--query.max-samples`/并发；
- 拆分面板/查询。

**预防**

- 面板默认 1h/6h，不默认 30d；
- 设置 `min step`/`max data points`；
- 高基数指标禁止直接画长范围；
- 监控慢查询/查询并发。

## S.15 SLO 误报：低流量下的 burn rate

**现象**

- 夜里没有流量，SLO burn rate 告警 page；
- 错误率看起来很高，但总请求只有几个；
- 值班被无效唤醒。

**定位**

```promql
sum(rate(http_requests_total[5m]))
sum(rate(http_requests_total{code=~"5.."}[5m]))
```

**根因**

低流量时分母很小，一两个错误就让错误率/burn rate 爆表；短窗口样本不足，统计不稳定。

**修复**

- 加最小请求数条件；
- 用更长窗口；
- 对零流量单独用合成探针/可用性告警；
- 调整 burn rate 阈值/多窗口。

**预防**

- SLO 规则设计时考虑流量下限；
- 规则测试覆盖低流量/零流量；
- 告警演练验证夜间行为。

## S.16 Pushgateway 残留：批任务删除后指标不消失

**现象**

- 批处理任务已经下线，`job_success` 还是 1；
- 告警一直不触发/一直触发；
- Pushgateway 上指标越积越多。

**定位**

```bash
curl -s http://pushgateway:9091/metrics | grep push_time_seconds
```

检查 grouping key、最后推送时间、是否有删除逻辑。

**根因**

Pushgateway 不会自动过期指标；任务结束后没有 DELETE/PUT 空 body；`push_time_seconds` 没有告警。

**修复**

- 任务结束时 `DELETE /metrics/job/<job>/...`；
- 或 `PUT` 空 body 清除该组；
- 对 `push_time_seconds` 设置告警；
- 清理历史 grouping key。

**预防**

- 每个推送任务都有清理逻辑；
- 监控 `push_time_seconds`/`push_failure_time_seconds`；
- 不要用 Pushgateway 存长期指标。

## S.17 reload 后告警重置：规则重建导致 pending 重新计时

**现象**

- 配置 reload 后，原本 pending 的告警消失/重新计时；
- 故障持续但通知延迟；
- `prometheus_rule_group_last_evaluation` 重置。

**定位**

- 看 reload 时间与告警 `activeAt`；
- 检查规则文件/规则组是否变化；
- 看 `prometheus_config_last_reload_successful`。

**根因**

reload 会重建规则组/规则状态；pending 计时从头开始。大规模配置重载还可能造成短暂抓取缺口。

**修复**

- 避免频繁 reload；
- 把规则变更与配置变更分开；
- 对关键告警接受一次重新计时，或使用 `keep_firing_for` 缓冲；
- 在维护窗口做大规模变更。

**预防**

- 配置/规则变更走 CI + 灰度；
- 监控 reload 成功率/告警状态；
- 变更前评估影响。

## S.18 备份恢复失败：block 版本/权限/路径

**现象**

- 从快照恢复后 Prometheus 启动失败；
- `open block: ...` / `permission denied`；
- 查询不到历史数据。

**定位**

- 检查快照目录结构/权限/属主；
- 确认 Prometheus 版本与 block 版本兼容；
- 看日志中的 block ULID/错误。

**根因**

快照复制不完整、权限不对、版本不兼容，或把 head/block 放错目录。

**修复**

- 按官方快照恢复流程放置 block；
- 修正权限/属主；
- 用兼容版本启动；
- 从原始快照重新恢复。

**预防**

- 定期恢复演练；
- 记录版本/路径/权限；
- 备份后校验文件和 meta.json；
- 升级前备份。

## S.19 总结：故障排查的层次感

| 层次 | 关键信号 | 先看 |
|---|---|---|
| 抓取 | `up`、`scrape_duration_seconds`、`scrape_samples_scraped` | `/targets`、exporter |
| 存储 | `head_series`、`blocks_bytes`、compaction 失败 | 磁盘、WAL、retention |
| 查询 | 超时、max-samples、慢查询 | 时间范围、基数、step |
| 规则 | `rule_evaluation_failures`、health | `/rules`、表达式 |
| 告警 | notifier 队列/失败、Alertmanager | `/alerts`、路由/抑制 |
| 远端 | pending/dropped/highest_sent | 网络、队列、接收端 |

先定位层次，再选指标；不要一上来就改 PromQL。大多数"监控不准"不是查询写错，而是采集/标签/时间语义错了。

## 本章小结

- 基数、抓取超时、WAL/磁盘、告警身份、remote write 队列是最常见的高影响故障。
- `up=1` 不代表样本都写入了；append 错误会静默丢样本，要看 out-of-order/duplicate 指标。
- 3.x 的左开区间、子查询 step、lookback 会改变"少一个点"的语义；升级必须回归规则测试。
- ServiceMonitor/relabel 的问题多在 selector/port/namespace/regex 锚定。
- SLO 低流量、Pushgateway 残留、reload 重置是告警工程的典型坑。
- 备份恢复要演练；版本/权限/路径任一错误都会让恢复失败。

## 延伸阅读

- 本教程第 12 章（抓取失败语义）、第 14～16 章（TSDB/查询）、第 23 章（运维）、第 25～27 章（SLO/告警/基数）。
- 附录 E（故障排查手册）、附录 P（Operator 排查）、附录 Q（长期存储故障）。
- Prometheus 官方 troubleshooting/operational 文档。

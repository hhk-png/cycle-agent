# 附录 T Pushgateway 与短生命周期任务

第 1、5 章都提过 Pushgateway，但只给了结论："短生命周期任务的最后值缓存，不是推模式 Prometheus"。本附录把它讲透：为什么它容易制造"永不消失的假指标"、分组键怎么设计、删除语义是什么、什么时候该用 textfile collector/OTel/agent 替代。

## T.1 为什么 Prometheus 默认不推送

拉取模型的结构性好处：

- 目标清单就是真相，抓取失败是信号（`up=0`）；
- Prometheus 控制节奏，目标不会因为监控后端故障而堆积内存；
- 调试简单，`curl` 目标即可复现；
- 目标消失时能写 stale，而不是让最后一条推送值永久保留。

推送模型的问题：

- 谁来决定"目标应该存在"？没有服务发现时，推送方消失不会产生任何信号；
- 推送失败/重试/去重/顺序由客户端负责；
- 监控后端故障时，客户端可能无限缓冲或丢数据；
- 无法自然表达"任务已结束/目标已下线"。

Pushgateway 不是"把 Prometheus 变成推送系统"，而是**给短生命周期任务一个可被抓取的缓存端点**。

## T.2 Pushgateway 的定位

适用：

- 批处理 Job（几分钟内完成，Prometheus 来不及抓）；
- CronJob/定时任务；
- Serverless/短生命周期容器；
- 无法被 Prometheus 主动访问、但能主动推送的网络。

不适用：

- 长期运行的服务（应该暴露 `/metrics` 被抓）；
- 需要精确"任务是否还在运行"的场景（Pushgateway 不会自动过期）；
- 高频/高基数指标（会放大缓存和抓取成本）；
- 需要多副本/顺序/恰好一次语义的场景。

一句话：**Pushgateway 缓存的是"某个分组键的最后一次推送状态"，不是事件流，也不是任务生命周期。**

## T.3 分组键与 URL

Pushgateway 用 URL 路径表达分组键（grouping key）：

```text
/metrics/job/<job_name>/<label_name>/<label_value>/...
```

例子：

```bash
echo 'batch_records_processed_total 1234' | \
  curl --data-binary @- \
  http://pushgateway:9091/metrics/job/nightly_etl/instance/etl-0/env/prod
```

分组键：

```text
job="nightly_etl", instance="etl-0", env="prod"
```

规则：

- `job` 是路径中的固定段；
- 后续每两个段是一个标签名/值；
- 标签名不能以 `__` 开头；
- 分组键相同的推送会互相覆盖/合并；
- 分组键不同就是不同的"组"，会各自保留最后状态。

设计建议：

- 分组键只放**稳定、低基数**的维度：job、env、region、shard；
- 不要放 `run_id`、`timestamp`、`pod_name`（每次运行都新组，永久累积）；
- 需要区分每次运行，用指标值/标签（如 `run_id` 作为样本标签）而不是分组键，或干脆写日志/追踪；
- 同一个 job 的多个实例可以用 `instance` 区分，但要注意实例消失后的清理。

## T.4 推送语义：PUT、POST、DELETE

| 方法 | 语义 | 对已有指标 | 对 `push_time_seconds` |
|---|---|---|---|
| `PUT` | 替换该分组键的全部指标 | 删除未在新 payload 中出现的指标 | 更新 |
| `POST` | 合并/更新该分组键的指标 | 保留未出现的旧指标 | 更新 |
| `DELETE` | 删除该分组键 | 全部删除 | 不更新（组消失） |
| `PUT` 空 body | 清空该组指标 | 全部删除 | 更新 |
| `POST` 空 body | 只更新时间戳 | 保留 | 更新 |

关键点：

- `PUT` 是"这一组现在只有这些指标"，适合任务结束时一次性提交完整快照；
- `POST` 是"增量更新"，旧指标会残留；如果任务每次只发部分指标，容易积累脏数据；
- `DELETE` 成功返回 202，删除是**排队**执行的，不保证立即落盘；但同一分组键的 PUT/DELETE 顺序有保证；
- 删除不存在的分组是 no-op，不报错；
- 空 body 的 `PUT` 常用于"清空但保留组"，空 body 的 `POST` 只更新时间戳。

一个安全的批任务模式：

```bash
# 任务成功：PUT 完整快照
cat metrics.prom | curl --data-binary @- \
  -X PUT http://pushgateway:9091/metrics/job/nightly_etl/env/prod

# 任务失败：也推送失败状态，避免旧的成功值残留
printf 'batch_last_success 0\nbatch_last_run_timestamp %s\n' "$(date +%s)" | \
  curl --data-binary @- -X PUT \
  http://pushgateway:9091/metrics/job/nightly_etl/env/prod

# 任务永久下线：删除分组
curl -X DELETE http://pushgateway:9091/metrics/job/nightly_etl/env/prod
```

## T.5 push_time_seconds 与 push_failure_time_seconds

Pushgateway 为每个分组自动添加：

```text
push_time_seconds         最后一次成功修改该组的时间
push_failure_time_seconds 最后一次失败的时间
```

用途：

```promql
# 超过 2 小时没有成功推送
time() - push_time_seconds > 2 * 3600

# 最近一次是失败（失败时间晚于成功时间）
push_failure_time_seconds > push_time_seconds

# 某 job 的所有组都过期
max by (job) (push_time_seconds) < time() - 6 * 3600
```

注意：

- 这两个指标是 Pushgateway 加的，不是客户端推的；
- `push_time_seconds` 只反映"Pushgateway 收到了请求"，不反映任务业务成功；业务成功要用自己的 `batch_last_success` 等指标；
- 如果客户端每次都用新分组键（如带 `run_id`），旧组的 `push_time_seconds` 不会更新，告警会误报/漏报；
- `push_failure_time_seconds` 只在推送失败时更新，适合发现网络/认证问题。

## T.6 抓取 Pushgateway：honor_labels

Prometheus 抓 Pushgateway：

```yaml
scrape_configs:
  - job_name: pushgateway
    honor_labels: true
    static_configs:
      - targets: ["pushgateway:9091"]
```

为什么需要 `honor_labels: true`：

- 推送的指标自带 `job`/`instance`（来自分组键）；
- 如果 `honor_labels: false`（默认），target 的 `job="pushgateway"` 会覆盖推送的 `job`，所有任务混在一起；
- 冲突的标签会被改名为 `exported_job`/`exported_instance`，查询语义混乱。

开启后：

- 推送的 `job`/`instance` 优先；
- target labels 冲突时丢弃；
- 抓取层的 `job` 不再是 `pushgateway`，而是推送方设置的值。

代价：

- 所有任务共享一个抓取目标，`up{job="pushgateway"}` 只能说明 Pushgateway 可达，不能说明某个任务健康；
- 需要用 `push_time_seconds` 和业务指标判断任务状态；
- 分组键设计不当会让标签基数爆炸。

## T.7 持久化与 HA

Pushgateway 默认把指标放在内存：

- 重启后未持久化的组会丢失；
- `--persistence.file=/data/pushgateway.db` 定期落盘；
- `--persistence.interval=5m` 控制刷盘间隔；
- 落盘不是事务性的"恰好一次"，崩溃可能丢最近一段；
- 多副本 Pushgateway 之间不共享状态；抓取多个副本会得到重复/不一致数据。

HA 建议：

- 单副本 + 持久化 + 备份；
- 或者接受"Pushgateway 是缓存，丢失可重推"；
- 不要让多个 Pushgateway 同时被抓取同一组指标（除非去重）；
- 不要把 Pushgateway 当长期存储。

## T.8 清理策略

Pushgateway 不会自动过期指标，必须显式清理：

1. **任务结束时 DELETE**：最干净；
2. **PUT 空 body**：清空指标但保留组/时间戳；
3. **定期清理脚本**：按 `push_time_seconds` 删除过期组；
4. **重建实例**：极端情况下清空持久化文件（会丢所有组）。

清理脚本示例（遍历 API）：

```bash
# 列出所有指标族/组（需要 admin API 或直接读 /metrics）
curl -s http://pushgateway:9091/api/v1/metrics | jq '.data[] | .labels'
```

生产建议：

- 每个推送任务都知道自己的分组键，结束时负责删除；
- 对"任务已下线但组还在"设置告警；
- 定期审计分组键基数（`count by (job) (push_time_seconds)`）；
- 不要把删除权限开放给所有客户端。

## T.9 监控 Pushgateway 自身

关键指标：

```promql
pushgateway_http_requests_total
pushgateway_http_push_duration_seconds
# 持久化相关指标名称以实际版本为准
pushgateway_persistence_last_successful_timestamp_seconds
```

告警：

```promql
# 持久化失败（指标名以实际版本为准）
time() - pushgateway_persistence_last_successful_timestamp_seconds > 600

# 推送延迟高
histogram_quantile(0.99, rate(pushgateway_http_push_duration_seconds_bucket[5m])) > 1

# 组数量异常增长（基数事故）
count by (job) (push_time_seconds) > 1000
```

同时监控：

- Pushgateway 进程/磁盘/内存；
- 抓取它的 Prometheus 的 `up`/`scrape_duration_seconds`；
- 业务侧的 `batch_last_success`/`push_failure_time_seconds`。

## T.10 替代方案

### textfile collector（node_exporter）

适合"任务和 node_exporter 在同一主机"：

```bash
# node_exporter 启动参数
--collector.textfile.directory=/var/lib/node_exporter/textfile
```

任务把指标写成 `.prom` 文件（先写临时文件再原子 rename），node_exporter 抓取时读取。清理方式是删除文件。

优点：无需额外服务；缺点：依赖主机/node_exporter，容器化环境不方便。

### OTel Collector

OTel SDK/Collector 可以推 OTLP，再由 Collector 转成 Prometheus remote write 或暴露 `/metrics`：

```text
App (OTel SDK) → OTLP → Collector → prometheusremotewrite → 后端
```

适合已经有 OTel 采集体系的组织；Collector 负责批处理/重试/属性转换，比 Pushgateway 更适合事件/短任务。

### Agent 模式

边缘 Prometheus Agent 抓取本地目标并 remote write 到中心；短任务仍然需要被 Agent 抓到，或由任务主动推 remote write。Agent 有 WAL 缓冲，比 Pushgateway 更适合网络不稳定场景。

### CronJob exporter / 任务包装器

把"任务是否成功/耗时/最后运行时间"作为 exporter 暴露，Prometheus 抓取 exporter；任务本身不需要推指标。适合 Kubernetes CronJob。

### 直接 remote write

任务结束时直接 `POST /api/v1/write` 到 Prometheus/Mimir/VM：

- 需要处理 protobuf/Snappy/认证/重试；
- 适合已经有 remote write 客户端的场景；
- 仍然没有"任务消失"的语义，需要额外指标。

## T.11 安全

- 不要把 Pushgateway 暴露到公网；
- 启用 TLS/认证（反向代理或 `--web.config.file`）；
- 限制哪些客户端能推送哪些 job；
- 校验标签名/值，防止高基数投毒；
- 限制请求体大小/推送频率；
- 保护持久化文件/删除 API；
- 审计删除/清空操作。

## T.12 设计检查表

- [ ] 这个指标真的需要推送吗？长期服务应暴露 `/metrics`；
- [ ] 分组键只含稳定、低基数维度；
- [ ] 每次任务结束有 PUT 完整快照或 DELETE；
- [ ] 失败路径也推送状态，避免旧成功值残留；
- [ ] 抓取配置 `honor_labels: true`；
- [ ] 监控 `push_time_seconds`/`push_failure_time_seconds`；
- [ ] 有分组键基数/过期清理策略；
- [ ] 持久化/备份/恢复已验证；
- [ ] Pushgateway 不暴露公网，有认证/网络策略；
- [ ] 明确"Pushgateway 挂了/数据丢了"时的业务影响。

## T.13 与 Mini-Prometheus 的关系

Mini 不实现 Pushgateway，但提供了两种相邻能力：

- `remote_client.py`：把样本 remote write 到中心，适合"任务结束直接写远端"；
- `examples/alertmanager_lite.py`：演示接收端缓存/去重，不是 Pushgateway。

Mini 的 `/api/v1/write` 接收 remote write（JSON/protobuf），可以作为"短任务直接写 Prometheus"的教学替代；但它没有 Pushgateway 的分组键/`push_time_seconds`/删除语义，不能直接照搬。

## 本章小结

- Pushgateway 是短生命周期任务的最后值缓存，不是推模式 Prometheus，也不会自动过期。
- 分组键决定覆盖/合并语义；只放稳定低基数维度，任务结束要 PUT/DELETE。
- PUT 替换、POST 合并、DELETE 删除；空 body 的 PUT/POST 语义不同。
- `push_time_seconds`/`push_failure_time_seconds` 是任务健康的核心信号；抓取要 `honor_labels: true`。
- 持久化不是事务性 HA；多副本不共享状态。
- textfile collector、OTel、agent、直接 remote write 是常见替代，按环境选择。
- 安全上不要把 Pushgateway 暴露公网，并限制分组键/标签基数。

## 延伸阅读

- Prometheus Pushgateway 官方 README（分组键、PUT/POST/DELETE、push_time_seconds）。
- node_exporter textfile collector 文档。
- OpenTelemetry Collector `prometheusremotewrite` exporter。
- 本教程第 1、5 章（拉取/推送）、第 18 章（remote write）、第 28 章（测试/故障注入）。

# 第 22 章 容量规划与性能调优

## 22.1 三个基本量

容量规划只需要先算清三个量：

```text
活跃序列数 S
采样频率 f（samples/series/second）
保留时间 R（秒）
```

推导：

```text
摄入速率 = S × f          samples/s
总样本数 = S × f × R
磁盘 ≈ 总样本数 × 每样本字节数 × 副本/压缩系数
内存 ≈ S × 每序列内存
```

每样本字节数取决于标签基数、chunk 编码、索引；经验值 1～2 bytes/sample（压缩后，不含索引）。内存每序列约 1～4 KB（head index + chunk + postings 分摊），高基数标签会更高。

## 22.2 一个算例

假设：

```text
目标数 = 500
每目标序列 = 400
S = 200,000
抓取间隔 = 15s → f = 1/15
R = 15d = 1,296,000s
```

摄入：

```text
200,000 / 15 ≈ 13,333 samples/s
总样本 = 13,333 × 1,296,000 ≈ 172.8 亿
磁盘（1.5 B/sample）≈ 25.9 GB；加 2 倍余量约 52 GB
```

内存：

```text
200,000 × 2 KB ≈ 400 MB（仅 head，不含查询/规则）
```

实际要加：

- 2 倍余量（峰值/compaction/查询）；
- 索引/ WAL；
- 副本数；
- 远端存储成本。

## 22.3 序列数从哪来

```text
S = Σ (每个指标的标签组合数)
```

估算方法：

1. 从现有 Prometheus 读 `prometheus_tsdb_head_series`；
2. 按 job/指标拆分：`sum by (__name__) (count by (__name__) ({__name__=~".+"}))` 需要 recording rule 或 metadata；
3. 用 `/api/v1/status/tsdb` 的 `seriesCountByMetricName`；
4. 对 Kubernetes，按 namespace/pod 数量估算。

常见放大因素：

- Pod 名/实例 IP 变化；
- URL path 带 ID；
- 错误信息作为标签；
- 每个桶一条序列的经典直方图；
- 多副本/多集群重复采集。

## 22.4 内存

Head 内存组成：

- series 标签（字符串）；
- postings 索引；
- chunk 数据；
- 查询临时对象；
- 规则求值中间结果。

经验：

- 100 万活跃序列可能需要数 GB 到十几 GB；
- 标签越长/越多，内存越高；
- 高基数正则查询会临时放大内存；
- compaction 需要额外内存/磁盘。

保护：

- `--query.max-samples`；
- `--query.max-concurrency`；
- `--storage.tsdb.head-chunks-write-queue-size`；
- 容器 memory limit 留 20～30% 余量；
- 监控 `process_resident_memory_bytes` 与 OOM。

## 22.5 磁盘

磁盘规划：

```text
磁盘 = 每日样本数 × 保留天数 × 每样本字节 × 安全系数
```

安全系数：

- compaction 临时空间（约 1.5～2 倍单 block）；
- WAL；
- 快照；
- 文件系统预留 10%。

监控：

```promql
prometheus_tsdb_storage_blocks_bytes
prometheus_tsdb_head_series
predict_linear(prometheus_tsdb_storage_blocks_bytes[6h], 7*24*3600)
```

`predict_linear` 是容量预警的常用表达式。

## 22.6 查询容量

查询成本 ≈ 命中序列 × 时间范围样本数。保护参数：

```text
--query.max-samples=50000000
--query.timeout=2m
--query.max-concurrency=20
```

容量规划：

- 典型面板查询的样本量；
- 并发用户数；
- 规则求值的查询量；
- 大屏/告警的峰值。

如果查询经常触顶：

- 加 recording rules；
- 减少面板时间范围/序列；
- 拆分 Prometheus；
- 用 Mimir/Thanos 的查询层。

## 22.7 抓取容量

单实例抓取受：

- 目标数；
- 每目标样本数；
- 网络 RTT；
- 解析 CPU；
- TSDB 写入锁。

经验：

- 单实例 500～2000 目标较常见，取决于样本量；
- 每目标 1000 序列 × 1000 目标 = 100 万序列，已接近单机上限；
- 用 `scrape_duration_seconds` 的 P99 评估；
- 抓取超时率高时，先优化 exporter，再拆分。

## 22.8 规则容量

规则求值也是查询：

- 每条规则每个 interval 执行一次；
- 复杂 join/子查询会放大成本；
- 规则组串行，慢规则会延迟同组其他规则；
- 记录规则会增加写入量。

监控：

```promql
prometheus_rule_evaluation_duration_seconds
prometheus_rule_evaluation_failures_total
prometheus_rule_group_last_duration_seconds
```

优化：

- 规则表达式先过滤再聚合；
- 避免跨天窗口；
- 把大规则拆成多个组/实例；
- 用 recording rule 缓存中间结果。

## 22.9 扩展路径

按成本从低到高：

1. **治理基数**：删无用指标、降标签、聚合；
2. **降低频率**：非关键指标 60s；
3. **垂直扩展**：CPU/内存/SSD；
4. **功能分片**：按业务/集群拆；
5. **抓取分片**：hashmod；
6. **Agent + 远端存储**；
7. **Mimir/Thanos/VictoriaMetrics**。

不要第一步就上分布式存储；先把基数和查询治理好，往往能省一个数量级。

## 22.10 压测与基准

方法：

- 用真实抓取配置 + 模拟 exporter（如 `avalanche`）生成指标；
- 逐步增加序列数，观察摄入/查询/内存；
- 记录 P99 抓取时长、查询延迟、compaction 时间；
- 用 `promtool tsdb bench write`（部分版本）或自建基准；
- 在生产影子环境验证，不要在高峰压测。

Mini 的 `examples/03_storage_lifecycle.py` 可以观察小规模写入/切块；`tests/test_tsdb.py` 有 100 序列的冒烟测试，但不代表生产性能。

## 22.11 容量评审模板

```text
业务/集群：
目标数：
每目标序列数（P50/P99）：
抓取间隔：
活跃序列 S：
摄入 samples/s：
保留天数：
磁盘估算：
内存估算：
查询并发/典型样本量：
规则数量/复杂度：
HA/副本：
远端存储：
增长预测（3/6/12 个月）：
告警阈值：
```

## 22.12 内存结构与调优

内存大头：

```text
head series 固定开销    labels + series 结构 + postings
head chunks             最近 2～4h 的样本
查询临时对象            vector/matrix/聚合/排序
规则求值                中间结果
抓取缓冲                响应体/解析
WAL/compaction          页缓存/缓冲
```

调优：

- 降低活跃序列（最有效）；
- 控制标签长度/数量；
- 限制查询并发/样本量；
- 规则预聚合，避免大范围扫描；
- `GOMEMLIMIT` 设软上限，避免 OOM kill；
- 监控 `process_resident_memory_bytes`、`go_memstats_heap_inuse_bytes`；
- head chunks 落盘队列不要过小/过大。

不要用"加内存"掩盖基数问题：序列数增长是指数级的，内存是线性的。

## 22.13 磁盘 IO 与 compaction 调优

Prometheus 的 IO 模式：

- WAL 顺序写 + fsync；
- head chunk 周期性落盘；
- compaction 读旧 block、写新 block；
- 查询随机读 chunk。

建议：

- 本地 SSD/NVMe；避免网络盘做 WAL；
- 预留 compaction 临时空间（2× 最大合并）；
- 监控 `prometheus_tsdb_compactions_failed_total`；
- 不要频繁重启（WAL replay + compaction）；
- retention 不要设得太短（频繁删除/合并）；
- 文件系统选 ext4/xfs，mount 参数按厂商建议。

磁盘满的后果：

- WAL 写失败 → 写入不可用；
- compaction 失败 → 旧 block 堆积；
- 查询可能失败；
- 最坏情况数据损坏。

## 22.14 查询性能调优

参数：

```text
--query.max-concurrency=20
--query.timeout=2m
--query.max-samples=50000000
--query.lookback-delta=5m
```

优化顺序：

1. 减少输入：matcher、时间范围、step；
2. 预聚合：recording rules；
3. 拆分面板/查询；
4. 限制并发/超时；
5. 增加实例/查询层；
6. 最后才加 CPU/内存。

诊断：

- `prometheus_engine_query_duration_seconds`；
- 查询 API 的 `stats`；
- 慢查询日志（如果启用）；
- pprof CPU/heap。

反模式：

- 在 Grafana 里用 `$__timeFilter` 但查询仍扫全量；
- 大量 `topk`/`sort`；
- 高基数 `group by`；
- 子查询套子查询；
- 对 summary 分位数做聚合。

## 22.15 抓取性能调优

抓取成本：

```text
目标数 × 响应体大小 × 解析/压缩 + 连接/DNS/TLS
```

调优：

- 增大 `scrape_interval`（降低采样率）；
- 增大 `scrape_timeout`（慢 exporter）；
- 限制 `body_size_limit`/`sample_limit`；
- 启用 gzip（如果 exporter 支持）；
- 复用连接/HTTP2；
- 分片/多实例；
- 优化 exporter（缓存/分页/减少指标）。

监控：

```promql
scrape_duration_seconds
scrape_samples_scraped
scrape_body_size_bytes
up
```

`scrape_duration` 接近 `scrape_timeout` 是危险信号。

## 22.16 规则性能调优

规则成本：

```text
规则数 × 求值频率 × 表达式复杂度
```

调优：

- 增大 `interval`（降低频率）；
- 用 recording rules 预聚合；
- 缩小查询范围/加 matcher；
- 避免在规则里做 `topk`/大范围子查询；
- 设置 group `limit`；
- 规则组之间错峰（`query_offset`/不同 interval）；
- 监控 `prometheus_rule_evaluation_duration_seconds`/failures。

慢规则会拖垮查询，因为规则和查询共享引擎/CPU。不要把"规则是后台任务"当成隔离。

## 22.17 性能回归与容量预测

建立基线：

```text
- 摄入 samples/s
- 查询 P50/P95/P99
- 规则求值时长
- compaction 时长/失败
- 内存/磁盘增长
```

回归：

- 每次升级/配置变更后对比；
- 用固定查询语料；
- 记录版本/flag/数据量；
- 超过阈值就回滚/调查。

预测：

```text
未来序列 = 当前序列 × (1 + 月增长率)^月数
```

容量触发：

- 序列/磁盘/内存达到 70～80%；
- 查询 P99 超目标；
- compaction 落后；
- 抓取超时率上升。

## 22.18 一个调优案例

现象：

```text
Prometheus 内存 32GB，频繁 OOM；查询 P99 20s；抓取超时。
```

排查：

```text
head_series = 300 万（预算 100 万）
某指标 path 标签有 50 万值
查询大量 sum by (path)
规则每 15s 扫 7 天
```

处理：

1. relabel 规范化 path，删除高基数指标；
2. 加 `sample_limit`/`label_limit`；
3. 规则改为 recording + 1m interval；
4. 查询加 matcher/缩短范围；
5. 扩容到 64GB 作为缓冲；
6. 建立基数告警。

结果：

```text
head_series 降到 80 万；内存 18GB；查询 P99 2s；抓取恢复。
```

关键不是"加内存"，而是**先治理基数，再调参数，最后扩容**。

## 22.19 3.15–3.16 的运行时与存储调优

较新的 3.x 版本提供了几个直接影响容量/性能的旋钮；它们不是"默认开启就更好"，需要按负载验证。

### 22.19.1 自动 GOMEMLIMIT

```text
--auto-gomemlimit                 默认 true
--auto-gomemlimit.ratio           默认 0.9
--auto-gomemlimit.refresh-interval 3.15+；0s 表示只探测一次
```

用途：

- 容器/VPA 内存上限变化时，周期性地重新探测并更新 `GOMEMLIMIT`；
- 避免"按启动时内存设置 GOMEMLIMIT，扩容后仍按旧值 GC"；
- 仍然要给 Prometheus 留出堆外内存（mmap、页缓存、查询临时对象）。

注意：`GOMEMLIMIT` 是软上限，不是硬限制；超过后 GC 会更激进，但不会阻止 OOM kill。

### 22.19.2 日志级别热更新

3.15 起用 `runtime.log_level` 替代 `--log.level`：

```yaml
runtime:
  log_level: info
```

排障时 reload 到 `debug`，事后改回 `info`；不要长期开 debug，日志量会放大磁盘/采集成本。

### 22.19.3 XOR2 编码

3.15 起可在配置中启用：

```yaml
storage:
  tsdb:
    chunk_encoding:
      floats: xor2
```

收益与代价：

- 更快的 XOR 压缩/解压，降低 CPU 与磁盘；
- 写入后旧读取端可能不兼容（Thanos/Mimir/备份工具）；
- 回滚成本高，先在 staging 验证读取链路与恢复流程；
- 不要与 `--enable-feature=xor2-encoding` 混用（后者已弃用）。

### 22.19.4 查询成本控制

3.16 的 `--enable-feature=query-cost` 把"这条查询有多贵"变成可观测、可限制的对象。它包含三套机制：

1. **只估算、不执行**：`/api/v1/query_cost` 与 `/api/v1/query_range_cost` 返回预计触碰的序列数/读取样本数；
2. **估算 + 实际**：在 `/api/v1/query`、`/api/v1/query_range` 加 `cost=true`，响应里同时给出估算与实际成本；
3. **执行上限**：在 `global` 配置 `query_max_series`、`query_max_samples_read`、`query_max_duration`，超过时返回 HTTP 422、`errorType=cost_limit`。

一个最小配置：

```yaml
global:
  query_max_series: 200000
  query_max_samples_read: 50000000
  query_max_duration: 30s
```

启用开关：

```bash
prometheus --enable-feature=query-cost ...
```

验证是否启用：

```bash
curl -s http://localhost:9090/api/v1/features
# data 中应包含 query_cost: true
```

拒绝对应的指标：

```promql
# 被成本上限拒绝的查询数，reason 可能是 max_series / max_samples_read
sum by (reason) (increase(prometheus_engine_query_rejected_total[1h]))
```

生产注意：

- 估算值可能高估或低估，不能当精确计费依据；先用它做配额和护栏，再做账单；
- 上限对所有查询生效，包括 recording/alerting 规则；过紧的 `query_max_samples_read` 可能让昂贵的规则静默失败；
- 开启该特性后，`query_max_duration` 会替代规则查询的 `--query.timeout`，升级时要检查规则组的执行时间；
- 先在 staging 用真实面板/规则跑一周，记录估算 vs 实际，再设上限；
- 对多租户平台，把 `max_series`/`max_samples_read` 做成租户配额，并配合告警/降级策略。

排查顺序：先用 `/api/v1/query_cost` 找最贵的前 10 条面板查询，再看它们的序列数、时间范围、step 和函数（`rate`/子查询/正则匹配）；优先加 recording rule、缩小范围、提高 step，最后才加机器。

### 22.19.5 调优顺序

```text
1. 降基数（最有效）
2. 降查询范围/并发
3. 预聚合（recording rules）
4. 存储编码（XOR2）与压缩
5. 运行时 GC/内存参数
6. 加机器
```

不要反过来：先用 XOR2 和加内存掩盖基数问题，通常会在下一次发布时再次 OOM。

## 本章小结

- 容量规划从 S、f、R 三个量出发，推导摄入、磁盘、内存。
- 序列数是第一成本驱动；标签设计和直方图桶是主要放大源。
- 查询/规则/抓取是三类不同容量，分别监控。
- 扩展顺序：治理 → 降频 → 垂直 → 分片 → 远端存储。
- 压测要用真实配置和影子环境。

## 延伸阅读

- Prometheus 官方 "Storage" 与性能调优文章。
- `promtool tsdb analyze` 文档。
- 本教程附录 F（容量模型）与第 27 章（基数治理）。

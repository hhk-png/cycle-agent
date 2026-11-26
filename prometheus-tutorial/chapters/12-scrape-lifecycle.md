# 第 12 章 抓取生命周期与失败语义

## 12.1 一次抓取的完整路径

```text
target 被 SD 发现
  → relabel（目标标签）
  → 构建 URL（scheme/address/path/params）
  → HTTP GET（认证/TLS/超时）
  → 解析 exposition
  → metric relabel
  → 标签合并（target labels + sample labels）
  → 追加 TSDB
  → 写入 up/scrape_* 指标
  → 对比上一次成功抓取的序列，写 stale
```

每一步都可能失败，失败语义不同。

## 12.2 调度

- 每个目标按 `scrape_interval` 调度；
- `scrape_timeout` 必须 ≤ `scrape_interval`；
- 同一目标不会并发抓取（上一轮未结束时，Prometheus 会跳过或延迟）；
- 不同目标并行；每个目标有自己的抓取循环，并发规模由目标数、`scrape_interval`/`scrape_timeout` 和机器资源共同决定，没有单独的 `--scrape.max-concurrency` flag（查询侧有 `--query.max-concurrency`）；
- 抓取时间会加抖动，避免所有目标同一秒打满网络。

Mini 为每个目标起一个线程，按固定间隔抓取；生产 Prometheus 用 scrape pool + 调度循环，更适合数千目标。

## 12.3 超时与重试

Prometheus **不重试**单次抓取。失败就是失败，下一轮按计划继续。原因：

- 重试会打乱节奏、放大目标压力；
- 失败本身是信号（`up == 0`）；
- 下一轮很快就会到来（15s/30s）。

超时设置：

```yaml
scrape_timeout: 10s
```

如果目标经常接近超时，先看：

```promql
scrape_duration_seconds
scrape_timeout_seconds
```

`scrape_duration_seconds / scrape_timeout_seconds > 0.8` 就应告警或优化 exporter。

## 12.4 up 与抓取指标

每次抓取都会写入：

```text
up{job,instance}                          1/0
scrape_duration_seconds{job,instance}     耗时
scrape_samples_scraped{job,instance}      解析到的样本数
scrape_samples_post_metric_relabeling     保留的样本数
scrape_series_added{job,instance}         新增序列数
scrape_timeout_seconds{job,instance}      超时配置
scrape_body_size_bytes{job,instance}      响应体大小
```

这些指标本身也带 target labels（job/instance/static labels）。

常见查询：

```promql
up == 0
avg_over_time(up[1h]) < 0.99
scrape_samples_scraped == 0
scrape_series_added > 1000
```

`scrape_series_added` 突增通常意味着标签爆炸或新版本引入了高基数指标。

## 12.5 失败语义

| 失败 | up | 已有序列 | 目标健康 |
|---|---|---|---|
| 连接失败/超时 | 0 | 失败抓取视为空抓取，为上次成功抓取的序列写 stale | down |
| HTTP 4xx/5xx | 0 | 同上 | down |
| 解析错误 | 0 | 同上 | down |
| 样本超限/标签超限 | 0 | 同上 | down |
| 部分样本重复/乱序 | 1 + append 错误指标 | 成功样本已写入，错误样本被丢弃 | up |
| 目标从 SD 消失 | 无新 up | 约 1～2 个抓取间隔后写 stale | 移除 |

关键语义：真实 Prometheus 把**失败抓取视为空抓取**，会为上次成功抓取中已存在、本次未出现的序列写 stale。因此这些序列在失败时刻就不再返回查询结果，而不是等 lookback 过期；`up=0` 仍会写入。

lookback 是**兜底**，不是失败语义的主体：

- 远端写/推送数据：Prometheus 不知道源端状态，没有 stale 可写；
- exporter 自带显式时间戳：staleness 跟踪可能被绕过；
- 进程崩溃/目标移除时的标记尚未写入；
- 这些场景下，最后一条样本会在 lookback（默认 5m，3.x 左开）内继续可见。

这也意味着：

- 告警要用 `up == 0`，不要依赖业务指标"消失"；失败和成功抓取都可能让序列消失；
- 面板要显示 `up` 和最后抓取时间，让"没有数据"可见；
- `or vector(0)` 会把"没有序列"变成 0，可能掩盖抓取失败；
- 对关键指标，用 `absent()`/`absent_over_time()`，但要先确认该场景会不会写 stale。

## 12.6 staleness 的精确规则

写入 stale 标记的时机：

1. 抓取失败（失败抓取被视为空抓取）；
2. 一次**成功**抓取中，之前存在但本次不再返回的序列；
3. 目标从服务发现中移除（`endOfRunStaleness`，约 2 个抓取间隔 + 10% slack 后写）；
4. 目标被 relabel 丢弃（等价于 target removal）；
5. 显式时间戳/远端写/崩溃等场景可能没有 stale，只能靠 lookback。

stale 标记的时间戳：

- 失败/成功抓取使用本次抓取时间；
- target removal 使用计算出的 stale 时间（下一次本应抓取的时间 + slack）。

写入后：

- instant selector 立即跳过该序列；
- range selector 不返回 stale 样本（函数看不到它）；
- `absent()` 会认为序列不存在；
- 序列本身仍留在 TSDB，直到 compaction/retention 清理。

Mini-Prometheus 也实现了这条链路：成功抓取中消失的序列、目标移除、relabel 丢弃、失败抓取都会写 stale；append 错误不改变 `up`。差异主要在可观测性：Mini 只在 `stats["appendErrors"]` 里计数，没有逐目标的 `prometheus_target_scrapes_sample_*` 指标。`tests/test_scrape.py` 锁定这些语义。

## 12.7 honor_labels

当抓取到的样本自带 `job`/`instance` 等标签，与 target labels 冲突时：

```yaml
honor_labels: false   # 默认
```

默认行为：

- target label 优先；
- 抓取标签冲突时改名为 `exported_<name>`；
- 例如抓取到 `instance="scraped"`，target `instance="10.0.0.1:9100"`，最终为 `instance="10.0.0.1:9100", exported_instance="scraped"`。

`honor_labels: true`：

- 抓取标签优先；
- target label 冲突时被丢弃；
- 适合 federation 或"指标自带完整身份"的场景。

错误使用会导致：

- `job`/`instance` 被覆盖，序列冲突；
- federation 后标签错乱；
- 告警路由到错误的团队。

## 12.8 honor_timestamps

```yaml
honor_timestamps: true   # 默认
```

- true：使用 exporter 提供的时间戳（如果有）；
- false：忽略 exporter 时间戳，统一用抓取开始时间。

建议：

- 普通 exporter 不要暴露时间戳，让 Prometheus 赋值；
- 只有在批量导入/回填/特殊代理场景才用显式时间戳；
- 显式时间戳容易导致乱序/重复，必须保证单调。

## 12.9 限制与保护

```yaml
sample_limit: 50000
label_limit: 30
label_name_length_limit: 200
label_value_length_limit: 500
body_size_limit: 10MB
```

- 超过 `sample_limit`：整次抓取失败，up=0；
- 超过 label 限制：抓取失败；
- 这些限制是"防爆炸"的最后一道闸，不是容量规划工具；
- 限制值要基于正常目标的 P99，留 2～3 倍余量；
- 触发限制要有告警（`up == 0` + 目标特定标签）。

Mini 实现 sample_limit/label_limit/label_name_length_limit/label_value_length_limit/body_size_limit。

## 12.10 抓取协议协商

```yaml
global:
  scrape_protocols:
    - OpenMetricsText1.0.0
    - PrometheusText0.0.4
```

Prometheus 通过 `Accept` 头协商：

```text
application/openmetrics-text; version=1.0.0,
text/plain; version=0.0.4
```

如果 exporter 不支持 OpenMetrics，会退回文本格式。强制错误协议会导致解析失败。

## 12.11 抓取性能

单实例抓取能力受限于：

- 目标数 × 抓取频率；
- 每个目标的样本数；
- 网络 RTT；
- 解析 CPU；
- TSDB 追加锁竞争。

优化：

- 降低不必要的抓取频率（非关键指标 60s）；
- 用 recording rules 预聚合，而不是增加抓取频率；
- 拆分 Prometheus（按集群/业务）；
- 用 agent 模式 + remote write 分担中心压力；
- 监控 `scrape_duration_seconds`、`scrape_samples_scraped`、`prometheus_target_scrape_pool_sync_total`。

## 12.12 Mini 的抓取实现

`scrape/manager.py`：

- `sync()` 对比目标集，增删并写 stale；
- `scrape_once()` 完整走 fetch/parse/relabel/append；
- `_merge_labels()` 实现 honor_labels；
- `_failure()` 写 up=0 和 scrape_duration；
- 成功抓取后对比 `target.series`，为消失序列写 stale；
- `TargetState` 保存 health/lastError/lastScrape/metadata/exemplars。

测试覆盖：

- 成功/HTTP 失败/解析失败/连接超时；
- honor_labels 两种模式；
- honor_timestamps；
- 序列消失、目标移除与失败抓取的 stale；
- sample/body/label 限制；
- OpenMetrics exemplar；
- 重复时间戳。

## 12.13 抓取错误与追加错误不是一回事

一个容易误判的细节：`up` 反映的是**抓取是否成功**（HTTP 可达、状态码 200、响应可解析），而不是"每个样本都写入了"。

真实 Prometheus 中：

- 连接失败、非 200、解析失败、超限：抓取失败，`up=0`；
- 样本乱序、重复时间戳、超出时间边界：**追加（append）错误**，对应样本被丢弃，但抓取本身可能仍然 `up=1`；
- 追加错误通过独立指标暴露：

```promql
rate(prometheus_target_scrapes_sample_out_of_order_total[5m])
rate(prometheus_target_scrapes_sample_duplicate_timestamp_total[5m])
rate(prometheus_tsdb_out_of_order_samples_total[5m])
```

这解释了"`up=1` 但曲线有缺口"的经典现象：目标健康、抓取成功，但样本因为时间戳问题被静默丢弃。根因通常是 exporter 自带时间戳、NTP 漂移、relabel 后序列冲突，或 remote write 重试。

Mini-Prometheus 与真实语义一致：`append_batch()` 返回错误时，`up` 仍为 1、目标 health 保持 up，错误样本被丢弃并计入 `stats["appendErrors"]`。Mini 没有实现逐目标的 `prometheus_target_scrapes_sample_*` 指标，所以排查"哪些样本被丢弃"不如真实 Prometheus 直观；生产排查仍以 `up` + append 错误指标为准。

一个例外：Mini 的 exposition 解析器对**同一 payload 内**同序列、同时间戳、不同值的两行仍视为解析错误（`up=0`）。真实 Prometheus 会先解析两行，再由 TSDB 拒绝第二个样本（`up=1` + duplicate 指标）。Mini 选择在解析期严格失败，是为了让 exporter 的重复样本尽早暴露；跨抓取的重复则走 append 错误路径。

更多"up=1 但数据缺失"的案例见附录 S。

## 12.14 内容协商、压缩与 protobuf

抓取不只是"GET /metrics"：协议协商决定解析器、体积和 exemplar 行为。

### 12.14.1 Accept 与 Content-Type

```text
Accept: application/openmetrics-text; version=1.0.0,
        text/plain; version=0.0.4
```

exporter 返回：

```text
Content-Type: application/openmetrics-text; version=1.0.0
```

或者：

```text
Content-Type: text/plain; version=0.0.4; charset=utf-8
```

Prometheus 按 `Content-Type` 选择解析器；`scrape_protocols` 决定发送的 Accept 顺序。如果配置强制只接受 OpenMetrics，而 exporter 返回文本，抓取会失败；反过来通常可以回退。

### 12.14.2 gzip

```text
Accept-Encoding: gzip
```

- exporter 可以返回 `Content-Encoding: gzip`；
- 解压发生在解析前，计入抓取超时；
- 大指标集能显著降低网络时间，但增加 CPU；
- 不要对 protobuf+snappy 再套 gzip；
- 如果 exporter 的 gzip 实现有 bug（截断/错误 header），抓取会解析失败。

### 12.14.3 protobuf

protobuf exposition 使用 `MetricFamily` delimited 编码，优点是解析快、体积小、类型明确；代价是调试困难、生态支持不如文本。

切换前验证：

- counter 的 `_total` 命名；
- timestamp 单位；
- exemplar 是否保留；
- histogram/summary 的桶/分位数；
- 与 OpenMetrics 的 `# EOF`/UNIT 差异；
- 回退到文本的路径。

### 12.14.4 Accept-Encoding 与抓取指标

抓取自身的指标能帮助判断协议/压缩问题：

```promql
scrape_duration_seconds
scrape_body_size_bytes
scrape_samples_scraped
scrape_samples_post_metric_relabeling
```

如果 `body_size_bytes` 很大但 `samples_scraped` 很小，可能是格式/压缩/解析问题；如果 `scrape_duration_seconds` 接近超时，先看网络/解压/解析 CPU。

## 12.15 抓取池、目标规模与分片

Prometheus 为每个目标维护抓取循环（生产实现是 scrape pool + 每目标 goroutine），同一目标不会并发抓取。目标数增长时，瓶颈通常依次出现：

1. **文件描述符/连接数**：每个目标一个 HTTP 连接/keep-alive；
2. **CPU**：解析 exposition、relabel、TSDB append；
3. **内存**：head 序列/标签、抓取缓冲；
4. **网络**：跨区域抓取的 RTT/带宽；
5. **TSDB 锁/磁盘**：append/WAL/compaction。

调优顺序：

- 降低非关键目标的抓取频率（60s/300s）；
- 减少每次抓取的样本数（metric_relabel、exporter 裁剪）；
- 拆分 Prometheus（按集群/业务/区域）；
- 用 agent 模式把抓取推到边缘；
- 用 remote write 分层，而不是让单机存全部历史；
- 监控 `scrape_duration_seconds`/`scrape_samples_scraped`/`prometheus_target_scrape_pool_sync_total`。

### 12.15.1 目标上限的估算

粗略公式：

```text
抓取 QPS ≈ 目标数 / 抓取间隔
解析样本/s ≈ 目标数 × 每目标样本数 / 抓取间隔
```

例：5000 个目标、15s 间隔、每目标 500 样本：

```text
抓取 QPS ≈ 333
样本/s ≈ 166,667
```

这已经接近单机舒适区上限，需要拆分/降频/裁剪。实际值以压测为准（第 48 章）。

### 12.15.2 抓取分片

- **hashmod**：按目标地址哈希分到多个 Prometheus；查询时聚合；
- **功能分片**：按 job/业务拆分；
- **联邦**：下游抓取上游的聚合指标；
- **agent**：边缘抓取 + remote write。

分片后要保证：

- 每个目标只被一个分片抓取（避免重复）；
- 分片规则稳定（目标增减不会大规模迁移）；
- 全局查询/告警有聚合层；
- 标签/命名统一。

## 12.16 3.15+ 的抓取扩展

3.15 起抓取链路增加了几个实用能力；它们不改变核心语义，但会改变配置和排障方式。

### 12.16.1 Unix Domain Socket 目标

3.15 的 Unix socket 抓取不是 `unix://` 地址，而是通过目标标签 `__unix_socket__` 指定 socket 路径：

```yaml
scrape_configs:
  - job_name: local-agent
    static_configs:
      - targets: ["local-agent"]
        labels:
          __unix_socket__: /run/agent/metrics.sock
    relabel_configs:
      - target_label: instance
        replacement: local-agent
```

要点：

- `targets` 里的值只是名字/Host，不需要能解析；真正拨号的是 `__unix_socket__`；
- `__scheme__`（或 job 的 `scheme:`）仍决定 `http`/`https`，默认 `http`；
- 适合 sidecar/本机 agent，绕过 TCP/TLS；
- 权限由文件系统决定，官方镜像默认以 uid 65534 运行，socket 需要相应权限；
- **安全敏感**：`__unix_socket__` 会让 Prometheus 直接拨号本地路径。3.15 的实现没有对路径做清理/allowlist，若 `file_sd`/`http_sd` 可被低信任用户写入，可能被用来拨任意本地 socket（SSRF/权限提升）。只允许可信 SD 设置该标签，限制路径前缀，拒绝 `../`、抽象 socket 和敏感目录；参见附录 L.6；
- 3.15 之前会忽略该标签并以 `dial tcp: lookup ...` 失败，`promtool check config` 也看不出来；
- 远程抓取/跨主机不可用；Targets 页面只显示 `scrapeUrl`，不显示 socket 路径。

### 12.16.2 OpenMetrics 2.0（实验）

OpenMetrics 2.0 抓取格式提供更完整的元数据、单位和 exemplar 表达，但在 3.15/3.16 仍是**实验特性**，通过独立开关启用：

```bash
prometheus --enable-feature=openmetrics2 ...
```

它**不是** `scrape_protocols` 的取值。`scrape_protocols` 仍使用：

```yaml
scrape_configs:
  - job_name: api
    scrape_protocols:
      - "OpenMetricsText1.0.0"
      - "OpenMetricsText0.0.1"
      - "PrometheusText0.0.4"
```

注意：

- 目标返回 `application/openmetrics-text; version=2.0.0` 时，若未开 `openmetrics2` 且没有 `fallback_scrape_protocol`，抓取会失败；
- 解析器不稳定，后续版本可能拒绝当前能接受的 exposition；不要把它作为生产唯一路径；
- 元数据/单位进入 TSDB 的方式可能与文本格式不同，先验证 `/api/v1/metadata`；
- 旧 exporter 会回退到 1.0/文本，前提是 `scrape_protocols` 保留回退项；
- 3.16 继续修复 OM2 的 exemplar/histogram 解析细节，但仍为实验特性；生产保留 1.0/文本回退，不要把它作为唯一协议。

### 12.16.3 zstd 压缩

3.15 增加 zstd 抓取响应（实验开关）：

```bash
prometheus --enable-feature=zstd-scrape ...
```

协商：

```text
Accept-Encoding: zstd, gzip
```

取舍：

- 大 exposition 能降低网络时间，但解压消耗 CPU；
- 目标端必须真的支持 zstd，否则回退；
- 不要对 protobuf+snappy 再套 zstd；
- 抓取超时包含解压时间，CPU 紧张时可能适得其反。

### 12.16.4 与原生直方图的关系

抓取原生直方图仍由 `scrape_native_histograms` 控制；需要同时保留经典桶时用 `always_scrape_classic_histograms`。协议扩展（OM2.0/zstd/Unix socket）与直方图开关是正交的，不要混为一谈。版本差异见附录 AE。

## 本章小结

- 抓取路径长，失败点分散；`up` 和 `scrape_*` 指标是诊断入口。
- Prometheus 不重试单次抓取，失败即信号。
- 抓取失败视为空抓取，写 stale 并写 `up=0`；目标消失/序列消失同样写 stale；没有 stale 的场景才靠 lookback。
- append 错误（乱序/重复）不改变 `up`，但会丢弃样本；要看 append 错误指标。
- honor_labels/honor_timestamps 决定标签与时间戳归属，错误配置会导致身份错乱。
- 限制参数是防爆炸闸门，需配合告警。

## 延伸阅读

- Prometheus 官方 "Configuration" 的 scrape 相关字段。
- Prometheus 源码 `scrape/scrape.go` 与 `scrape/target.go`。
- 本教程第 11 章（SD/relabel）、第 39 章（Mini 抓取）与附录 AA（`up` 与 `probe_success` 的区别、exporter 抓取预算）。

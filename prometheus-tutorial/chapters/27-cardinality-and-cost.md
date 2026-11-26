# 第 27 章 基数治理与成本工程

## 27.1 基数为什么是第一成本

Prometheus 的资源消耗大致与**活跃序列数**成正比：

- head 索引/ chunk/ postings；
- 抓取解析对象；
- 查询扫描；
- remote write 带宽；
- 远端存储的索引/压缩。

一个高基数标签可以把序列数放大几个数量级，而且往往在发布后才暴露。基数治理是 Prometheus 运维中投入产出比最高的工作。

## 27.2 找到高基数来源

### 从 Prometheus 自身指标

```promql
# 按指标名的序列数（需要 metadata 或 recording rule）
topk(20, count by (__name__) ({__name__=~".+"}))

# head 总序列
prometheus_tsdb_head_series

# 新序列速率
sum by (job) (rate(scrape_series_added[5m]))
```

`count by (__name__)` 本身很贵；生产可用 `/api/v1/status/tsdb` 的 `seriesCountByMetricName`，或定期跑离线分析。

### 从抓取目标

```promql
scrape_samples_scraped
scrape_series_added
```

`scrape_series_added` 突增是发布引入高基数的强信号。

### 从日志/变更

- 新版本是否新增标签？
- 是否把 URL/用户 ID/错误信息放进标签？
- Kubernetes 元数据是否被 labelmap 全量映射？

## 27.3 基数预算

给每个团队/服务设定预算：

```text
服务 A：≤ 50,000 活跃序列
服务 B：≤ 200,000
平台组件：≤ 500,000
总计：≤ 1,000,000
```

预算要：

- 与容量/成本挂钩；
- 在 CI/发布前检查（用 staging Prometheus 的 `head_series`）；
- 有超限告警；
- 定期 review。

## 27.4 治理手段

### 1. 从源头修

- 删除无用的指标/标签；
- 用低基数枚举替代动态值；
- 把高基数维度移到日志/追踪；
- 直方图桶不要过多。

### 2. metric_relabel_configs

```yaml
metric_relabel_configs:
  - source_labels: [__name__]
    regex: "go_.*|process_.*"
    action: drop
  - source_labels: [path]
    regex: "/api/v1/users/[0-9]+"
    target_label: path
    replacement: "/api/v1/users/:id"
  # labeldrop 匹配的是“标签名”，不是 source_labels；
  # regex 写 ".+" 会删掉包括 __name__ 在内的所有标签，导致序列塌缩。
  - regex: "pod"
    action: labeldrop
```

注意：

- 这是"事后补救"，不解决源头；
- `labeldrop` 可能让不同序列合并，导致数据冲突；
- 改名/归一化要保证语义正确；
- 变更要测试，避免误删关键指标。

### 3. 抓取限制

```yaml
sample_limit: 50000
label_limit: 30
label_value_length_limit: 500
```

触发限制会让整次抓取失败（up=0），是"熔断"而不是"治理"。

### 4. 记录规则聚合

```yaml
- record: service:http_requests:rate5m
  expr: sum by (service, code) (rate(http_requests_total[5m]))
```

用低基数聚合结果替代原始高基数序列，长期保留聚合、短期保留原始。

### 5. 原生直方图

经典直方图每个桶一条序列；native histogram 一条序列包含所有桶，能显著降低高基数直方图的序列数。Prometheus 3.8 起原生直方图稳定，但抓取端要显式 `scrape_native_histograms: true`，客户端/后端也要支持。

### 6. retention/远端策略

- 原始高基数数据短保留；
- 聚合数据长保留；
- 远端存储按租户/指标设置 retention。

## 27.5 成本模型

本地成本：

```text
磁盘 = 序列数 × 采样率 × 保留时间 × 每样本字节
内存 = 序列数 × 每序列内存
CPU = 抓取解析 + 查询 + compaction + 规则
```

远端/托管成本通常按：

- 活跃序列数（time series）；
- 样本摄入量（samples/s）；
- 查询量/扫描字节；
- 保留时间；
- 多租户/HA 副本。

治理优先级：

1. 删除无用指标（最大收益）；
2. 降标签基数；
3. 降采样/聚合；
4. 缩短原始数据保留；
5. 选更便宜的存储层。

## 27.6 一次基数事故的复盘模板

```text
时间：
现象：head_series 从 80k 涨到 1.2M，Prometheus OOM
根因：新版本把 request_id 放进 http_requests_total 标签
发现：scrape_series_added 突增 + 内存告警
缓解：回滚 + metric_relabel drop request_id
修复：代码删除标签 + CI 基数检查
改进：序列预算、发布前 staging 压测、基数告警
```

更多真实故障的定位/修复/预防见附录 S。

## 27.7 基数告警

```promql
# 总序列增长过快
predict_linear(prometheus_tsdb_head_series[6h], 24*3600)
  > 1.5 * prometheus_tsdb_head_series

# 单 job 新序列速率
sum by (job) (rate(scrape_series_added[10m])) > 100

# 抓取样本数异常
scrape_samples_scraped > 2 * avg_over_time(scrape_samples_scraped[1d])
```

告警要带 job/target，便于定位。

## 27.8 不要做的事

- 用 `labeldrop` 删除 `instance`，导致序列合并；
- 把所有高基数标签塞进 recording rule；
- 用 `sample_limit` 当治理手段（会丢整次抓取）；
- 只看磁盘不看内存（head 先爆）；
- 在生产直接改 relabel 不做灰度。

## 27.9 基数分析工具箱

只靠 PromQL 很难定位"哪个标签把序列数放大"。生产上通常组合以下工具：

### promtool tsdb analyze

```bash
promtool tsdb analyze /var/lib/prometheus \
  --limit=20
```

输出通常包含：

- 最高基数的指标名（series count）；
- 最高基数的标签名（label name）；
- 最高基数的标签值对（label value pair）；
- 每个标签值占用的内存/序列数。

这是离线分析，不依赖 Prometheus 在线查询，适合在 OOM 后或容量评审前运行。

### /api/v1/status/tsdb

```bash
curl -s http://prometheus:9090/api/v1/status/tsdb | jq
```

关键字段：

- `headStats.numSeries`、`numLabelPairs`、`chunkCount`；
- `seriesCountByMetricName`：按指标名的序列数；
- `labelValueCountByLabelName`：哪个标签的值最多；
- `memoryInBytesByLabelName`：哪个标签最占内存；
- `seriesCountByLabelValuePair`：具体哪个 `label=value` 贡献最大。

这些字段是"基数事故"的第一现场：先看 `seriesCountByMetricName` 找到指标，再看 `seriesCountByLabelValuePair` 找到标签值。

### 离线 dump 与抽样

```bash
promtool tsdb dump /var/lib/prometheus \
  --match='{__name__="http_requests_total"}'
```

具体 flag 以 `promtool tsdb dump --help` 为准；不同版本略有差异。

对单个指标抽样，确认标签是否包含请求 ID、用户 ID、完整 URL、错误文本等动态值。

### series/labels API 的代价

```bash
curl -sG http://prometheus:9090/api/v1/series \
  --data-urlencode 'match[]=http_requests_total' | jq '.data | length'
```

`/api/v1/series` 会返回匹配的序列集合，基数大时响应也大；不要在告警规则或高频面板里调用它。它适合一次性排查，不适合持续监控。

### 记录规则的反向用法

把"每个指标的序列数"做成 recording rule，长期观察增长：

```yaml
- record: job:scrape_series_added:rate5m
  expr: sum by (job) (rate(scrape_series_added[5m]))
```

这比每次手工跑 `count by (__name__)` 便宜，也能在发布后立即看到新序列速率。

### 工具选择顺序

1. 先看 `scrape_series_added` 是否突增（在线、便宜）；
2. 再看 `/api/v1/status/tsdb` 的 metric/label 排名；
3. 用 `promtool tsdb analyze` 做离线深挖；
4. 用 `promtool tsdb dump` 抽样确认标签值；
5. 最后才用 `count by (__name__)` 这类全量查询（代价高）。

## 本章小结

- 活跃序列数是 Prometheus 的第一成本驱动。
- 高基数来源：动态标签、URL/ID、错误文本、K8s 元数据、直方图桶。
- 治理顺序：源头 → metric relabel → 抓取限制 → 聚合 → native histogram → retention。
- 成本模型要同时考虑本地资源与远端计费。
- 基数预算/告警/复盘应纳入发布流程。

## 延伸阅读

- Prometheus 官方 "Cardinality" 相关文章。
- Grafana Labs/Mimir 的 cardinality 管理文档。
- 本教程第 22 章（容量）、第 4 章（直方图）与附录 AA（exporter 的基数陷阱与 `metric_relabel_configs`）。

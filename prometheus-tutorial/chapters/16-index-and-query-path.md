# 第 16 章 索引与查询路径：从 matcher 到样本

## 16.1 查询的五个阶段

```text
1. 解析 PromQL → AST
2. 类型检查/参数校验
3. 选择序列：matcher → postings → series
4. 读取样本：chunk 解码 + tombstone 过滤
5. 计算：函数/聚合/匹配 → 结果
```

这一章聚焦 3～4，即存储层的查询路径。第 17 章讲 5。

## 16.2 Postings 索引

Postings 是倒排索引：

```text
label name/value → sorted series IDs
```

例子：

```text
__name__="http_requests_total" → [1, 3, 7, 9]
job="api"                      → [1, 3, 7]
code="500"                     → [3, 9]
```

查询 `http_requests_total{job="api",code="500"}`：

```text
postings(__name__=http_requests_total)
∩ postings(job=api)
∩ postings(code=500)
= [3]
```

集合运算：

- `=`：取对应 postings；
- `!=`：`allPostings - postings(value)`；
- `=~`：并集所有匹配的 label value 的 postings；
- `!~`：`allPostings - 并集`。

正则匹配需要枚举 label values，成本高于等值匹配。高基数标签上的正则查询可能非常慢。

## 16.3 索引文件结构

真实 Prometheus index v2 包含：

```text
Symbols         label names/values 的符号表
Series          series ID → label set
Label Index     label name → values
Postings        label pair → series IDs
Postings Offset 快速定位
TOC             各段偏移
```

查询时：

1. 读 TOC；
2. 解析 matcher → postings；
3. 交集/并集；
4. 用 series ID 读 label set；
5. 读 chunk meta；
6. 打开 chunks 文件读字节。

Mini 的 `index.json` 把 series 和 chunk meta 放在一起，省略了 postings；查询时线性扫描所有 series，适合教学/小规模，不适合百万序列。

## 16.4 Matcher 的选择性

查询成本近似与"命中的 series 数 × 时间范围"成正比。选择性高的 matcher：

```promql
{__name__="http_requests_total", job="api", instance="10.0.0.1:9100"}
```

选择性低：

```promql
{job=~".+"}
{__name__=~".*"}
```

原则：

- 优先用 `__name__` 等值；
- 再加一个高选择性标签（instance/pod）；
- 避免全匹配正则；
- 避免在超大时间范围上做高基数聚合。

## 16.5 时间范围裁剪

每个 chunk 有 `minTime`/`maxTime`。查询 `[mint, maxt]`：

```text
if chunk.maxTime < mint or chunk.minTime > maxt: skip
```

这避免了读取整个序列的历史。对长 retention + 短查询尤其重要。

Block 级别也有 `minTime`/`maxTime`，先裁剪 block，再裁剪 chunk。

## 16.6 合并 Head 与 Block

查询需要合并：

- 多个 block 的同一序列；
- head 中的同一序列；
- 去重（时间戳相同）；
- 应用 tombstone；
- 按时间排序。

Prometheus 的 `Querier` 接口：

```go
Select(ctx, sortSeries bool, hints, matchers...) (SeriesSet, error)
```

`SeriesSet` 是惰性迭代器，避免一次性加载所有样本。Mini 的 `TSDB.select()` 一次性返回列表，简单但内存开销大。

## 16.7 查询缓存

Prometheus 有一些缓存：

- postings 缓存（label → series IDs）；
- series 缓存；
- chunk 缓存（最近解码的 chunk）；
- 查询结果不缓存（每次重新计算）。

缓存对重复查询/规则求值有帮助，但高基数会稀释命中率。

## 16.8 查询并发与隔离

- `--query.max-concurrency`：同时执行的查询数；
- `--query.timeout`：单查询超时（默认 2m）；
- `--query.max-samples`：单查询加载的最大样本数（默认 5000 万）；
- 超过限制返回错误，而不是 OOM。

这些限制是"保护 Prometheus 自身"的闸门。Grafana 的慢查询/大面板是常见触发源。

## 16.9 一个查询的代价估算

查询：

```promql
sum by (job) (rate(http_requests_total[5m]))
```

代价：

```text
命中 series 数 × 窗口内样本数
+ 聚合的标签组合数
+ 结果序列数
```

如果 `http_requests_total` 有 100 万序列，窗口 5m、抓取 15s → 每序列 20 个样本 → 2000 万样本，接近默认上限。这类查询应改用 recording rule 预聚合，或缩小 matcher。

## 16.10 索引损坏与修复

症状：

- 查询报 `postings: not found`/`invalid index`；
- 启动时 block 加载失败；
- `promtool tsdb analyze` 报错。

处理：

1. 停止 Prometheus；
2. 备份数据目录；
3. 用 `promtool tsdb analyze` 检查；
4. 删除损坏 block（会丢数据）；
5. 从快照/远程存储恢复；
6. 根因：磁盘故障、非正常关机、手工修改。

不要在生产直接 `rm -rf blocks/*`；先确认哪些 block 损坏、影响哪些时间范围。

## 16.11 Mini 的查询实现

`TSDB.select(matchers, mint, maxt)`：

```python
for block in blocks:
    for labels, samples in block.select(matchers, mint, maxt):
        merge[labels][ts] = value
for labels, samples in head.select(matchers, mint, maxt):
    merge[labels][ts] = value   # head 覆盖 block
return sorted merged
```

`Block.select()`：

- 遍历 index 的所有 series；
- `match_all(matchers, labels)`；
- 遍历 chunk meta，按时间裁剪；
- 读 chunk 字节 → XOR 解码；
- tombstone 过滤。

`Head.select()`：

- 遍历 head series；
- 按 min/max time 裁剪；
- matcher 过滤；
- chunk 解码；
- tombstone 过滤。

复杂度 O(series × chunks)，对教学足够；生产需要 postings。

## 16.12 Postings 的编码与实现

Postings 列表不是简单的 `uint32` 数组，而是**有序、增量编码**的：

```text
postings list = [1, 3, 7, 9, 12, ...]
delta 编码    = [1, +2, +4, +2, +3, ...]
varint        = 变长字节，值越小占字节越少
```

这样：

- 稀疏/小 ID 列表占用更少空间；
- 求交集/并集可以流式归并，不需要解压成完整数组；
- 列表天然有序，支持二分/跳表式查找。

真实 index v2 还有：

- `postings offset table`：label pair → postings 偏移；
- `allPostings`：所有 series ID，用于 `!=`/`!~` 的补集；
- 每个 postings 段有长度/校验；
- 读取时按需 `mmap`/`pread`，不加载整个索引。

Matcher 的求值策略：

| Matcher | 策略 | 成本 |
|---|---|---|
| `label="value"` | 直接取 postings | 低 |
| `label!="value"` | allPostings − postings(value) | 中 |
| `label=~"prefix.*"` | 枚举匹配的 label values，取并集 | 中/高 |
| `label=~".*"` | 退化为 allPostings | 高 |
| `label!~"..."` | 补集 | 高 |
| `__name__="x"` | 直接取 postings | 低 |

`PostingsForMatchers` 会做优化：优先用等值 matcher 缩小候选集，再对剩余 matcher 做交集；对 `=~` 能提取前缀时走前缀索引（如果实现支持）。但任何优化都救不了"在高基数标签上做全匹配正则"。

## 16.13 Index v2 的段布局与读取

Index v2 的逻辑布局：

```text
┌──────────────┐
│ Symbols      │  label name/value 的去重表
├──────────────┤
│ Series       │  series ID → label set（增量编码）
├──────────────┤
│ Label Index  │  label name → label values
├──────────────┤
│ Postings     │  label pair → series IDs
├──────────────┤
│ Postings Off │  label pair → postings 偏移
├──────────────┤
│ TOC          │  各段 offset/length/CRC
└──────────────┘
```

读取一个查询的路径：

1. 打开 index，读 TOC；
2. 对每个 matcher，读 Postings Offset Table 定位 postings；
3. 流式归并求交集/并集，得到 series ID；
4. 用 series ID 读 Series 段，还原 label set；
5. 读 chunk meta（在 Series 段或独立段），得到 chunk 偏移；
6. 打开 chunks 文件，按偏移读字节；
7. 解码 chunk，应用 tombstone，返回样本。

真实实现会尽量**惰性**：不一次性解码所有 label set，不在内存里保留完整 postings。Mini 的 `index.json` 全部加载，简单但内存高。

## 16.14 查询 hints 与惰性迭代

真实 Prometheus 的 `Querier.Select` 接受 hints：

```go
type SelectHints struct {
    Start, End int64
    Step       int64
    Func       string
    Grouping   []string
    By         bool
    Range      int64
    ShardCount int
    ShardIndex int
    Limit      int
}
```

hints 让存储层知道：

- 查询是否需要按 step 对齐；
- 是否只取最新样本（`last_over_time`/instant）；
- 是否按 shard 裁剪（分布式查询）；
- 是否有输出上限（`limitk`/`topk` 下推）。

返回的 `SeriesSet` 是惰性迭代器：

```go
for ss.Next() {
    series := ss.At()
    it := series.Iterator()
    for it.Next() == chunkenc.ValFloat {
        t, v := it.At()
        // ...
    }
}
```

好处：

- 查询可以在满足 `limit` 后提前停止；
- 不需要把所有样本加载到内存；
- 可以流式聚合。

Mini 的 `TSDB.select()` 一次性返回 `list[tuple[Labels, list[Sample]]]`，实现简单，但无法提前停止，也无法控制峰值内存。

## 16.15 标签 API 与元数据查询

标签 API 的底层是索引，不是 PromQL：

```text
GET /api/v1/labels
GET /api/v1/label/<name>/values
GET /api/v1/series?match[]=...
GET /api/v1/metadata
```

成本：

- `labels`：遍历 label index 的 name 集合；
- `label/<name>/values`：读该 label 的 values 索引；
- `series`：matcher → postings → label set，可能返回大量序列；
- `metadata`：读 scrape 缓存的 metadata，不查 TSDB。

常见坑：

- `/series` 没有 `limit` 时可能返回百万级结果；
- 时间范围过大时，标签 API 会扫描很多 block；
- 正则 matcher 会枚举 label values；
- 多租户/权限要在代理层限制，而不是依赖 Prometheus。

`/api/v1/status/tsdb` 提供基数统计（按指标名/标签名的 series 数、内存占用等），是排查高基数的入口；不同版本字段有差异，以实际响应为准。

## 16.16 查询统计与诊断

Prometheus 会记录查询的：

```text
prometheus_engine_queries
prometheus_engine_queries_concurrent_max
prometheus_engine_query_duration_seconds
prometheus_engine_query_samples_total
```

较新版本的查询 API 还会返回 `stats`：

```json
{
  "status": "success",
  "data": {...},
  "stats": {
    "timings": {"evalTotalTime": 0.12, "resultSortTime": 0.001},
    "samples": {"totalQueryableSamples": 120000, "peakSamples": 8000}
  }
}
```

用法：

- `totalQueryableSamples` 接近 `--query.max-samples` → 查询太重；
- `peakSamples` 高 → 内存压力大；
- `evalTotalTime` 高但样本少 → 函数/正则/聚合复杂；
- 规则求值也走同一引擎，慢规则会拖垮查询。

保护参数：

```text
--query.timeout=2m
--query.max-concurrency=20
--query.max-samples=50000000
--query.lookback-delta=5m
```

这些参数是"防止一个查询拖垮整个进程"，不是性能优化。真正的优化是减少输入（matcher/时间范围/基数）和预聚合（recording rules）。

## 16.17 一个从 matcher 到样本的完整走读

查询：

```promql
sum by (job) (rate(http_requests_total{job="api",code="500"}[5m]))
```

时间 `t`、step 15s、范围 1h：

```text
1. 解析：Aggregate(sum by job) → Call(rate) → MatrixSelector
2. 类型检查：matrix → vector，rate 参数合法
3. matcher：
   __name__="http_requests_total" → postings A
   job="api" → postings B
   code="500" → postings C
   A ∩ B ∩ C = series S
4. block 选择：maxTime >= t-1h-5m 且 minTime <= t 的 block
5. 对每个 block：
   - postings → series
   - chunk meta 裁剪到 [t-1h-5m, t]
   - 解码 chunk，过滤 tombstone
6. head 选择：同样的 matcher + 时间范围
7. 合并 block/head 样本，按时间戳去重
8. 对每个 step：
   - 取窗口 [ts-5m, ts] 的样本
   - rate() 外推
   - sum by (job) 聚合
9. 返回 matrix/vector，序列化为 JSON
```

代价估算：

```text
series 数 × 每序列窗口样本数 × step 数
```

如果 `S=10,000`、窗口 5m/15s=20 个样本、1h/15s=240 个 step：

```text
10,000 × 20 × 240 = 4,800 万样本
```

接近默认 `max-samples` 上限。优化方向：

- 缩小 matcher（加 `instance`/`pod`）；
- 缩短查询范围；
- 用 recording rule 预聚合；
- 增大 step（降低分辨率）；
- 把面板拆成多个小查询。

## 本章小结

- Postings 是 label → series ID 的倒排索引，matcher 转集合运算。
- 查询先裁 block/chunk 时间范围，再解码样本。
- 选择性高的 matcher 决定查询成本；全匹配正则很危险。
- max-concurrency/timeout/max-samples 是保护机制。
- Mini 用线性扫描替代 postings，突出查询语义而非规模。

## 延伸阅读

- Prometheus 源码 `tsdb/index/`、`tsdb/querier.go`。
- Prometheus 官方 "Storage" 的 index 部分。
- 本教程第 17 章（执行）与第 36 章（Mini 查询）。

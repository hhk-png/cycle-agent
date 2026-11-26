# 第 48 章 性能基准、压测与容量验证

## 48.1 为什么"我们压过"通常不可信

性能数字很容易被误用：

- 只测了写入，没测查询；
- 只测了空库，没测高基数；
- 只测了平均值，没测 P99；
- 只测了单次，没测持续写入 + compaction；
- 硬件、文件系统、page cache、并发数都没记录；
- 用合成指标（`foo{id="1"}`）代替真实标签分布。

结果就是"实验室 100 万序列，生产 20 万就 OOM"。本章给出一套可复现的基准方法：先定义工作负载，再选工具，再解释结果，最后把结论转成容量模型。

## 48.2 工作负载模型

Prometheus 的负载由五组量决定：

1. **摄入**：活跃序列数、抓取间隔、每次抓取的样本数、目标数；
2. **基数**：指标数、标签数、标签值分布、churn（新序列速率）；
3. **查询**：QPS、查询类型（instant/range）、时间范围、step、聚合度、并发；
4. **规则**：规则组数、求值间隔、每个规则的序列数、告警数；
5. **保留/压缩**：retention、block 大小、compaction 并发、remote write。

基本公式：

```text
samples/s       = active_series / scrape_interval
samples/day     = samples/s × 86400
raw_bytes/day   ≈ samples/day × bytes_per_sample
disk            ≈ raw_bytes/day × retention_days × replicas × index_overhead
```

`bytes_per_sample` 取决于编码、标签重复度、样本值分布；经典经验值是 1～2 字节/样本（压缩后），但必须用你的数据实测。索引、WAL、临时空间、副本会让总磁盘显著高于这个估算。

查询侧：

```text
points_per_query = series_matched × (range / step)
max_samples      = points_per_query × concurrent_queries
```

Prometheus 的 `query.max-samples` 是硬保护；超过会返回 execution 错误。压测要测"在限制内能承受多少并发"，而不是把限制调大后测到崩溃。

## 48.3 测试环境

最低要求：

- 与生产同代 CPU/内存/磁盘（至少同数量级）；
- 独立数据盘，记录文件系统（ext4/xfs）、RAID、云盘类型/IOPS；
- 固定内核/Go 版本/Prometheus 版本；
- 关闭无关进程，避免 noisy neighbor；
- 记录 page cache 状态（冷/热查询差异巨大）；
- 固定抓取/查询客户端位置，避免网络成为变量；
- 每次实验前清空数据目录/重启，保证可复现。

报告里必须写：

```text
Prometheus version:
Go version:
CPU / memory:
Disk / filesystem / IOPS:
Network:
Dataset: series, samples, label cardinality, churn
Workload: scrape interval, query corpus, concurrency, duration
Warm-up: duration / cache state
```

没有这些信息的"性能报告"无法复现。

## 48.4 写入基准

### 48.4.1 目标

测：

- 可持续摄入 samples/s；
- head 内存随活跃序列的增长；
- WAL 写入/fsync 延迟；
- block flush/compaction 对写入的影响；
- 磁盘增长速率；
- 失败/重启后的恢复时间。

### 48.4.2 方法

1. **合成序列**：生成接近真实的标签分布（不要所有序列共享同一组标签，也不要每个序列完全随机）。
2. **控制 churn**：固定活跃序列 + 每秒新增序列，观察 head 内存和 postings。
3. **逐步加压**：从目标负载的 50% 开始，每 10 分钟增加 10%，记录拐点。
4. **持续时间**：至少覆盖一次 block flush（默认 2 小时）和一次 compaction；短跑测不出压缩抖动。
5. **监控**：Prometheus 自身指标 + OS 指标（CPU、内存、磁盘 await/IOPS、网络）。

关键指标：

```promql
rate(prometheus_tsdb_head_samples_appended_total[5m])
prometheus_tsdb_head_series
rate(prometheus_tsdb_wal_fsync_duration_seconds_sum[5m])
  / rate(prometheus_tsdb_wal_fsync_duration_seconds_count[5m])
rate(prometheus_tsdb_compaction_duration_seconds_sum[5m])
  / rate(prometheus_tsdb_compaction_duration_seconds_count[5m])
rate(process_cpu_seconds_total[5m])
go_memstats_alloc_bytes
```

### 48.4.3 工具

- **promtool tsdb bench write**：官方 TSDB 写入基准，适合比较磁盘/编码/版本；
- **prombench**：Prometheus 官方基准框架，适合版本回归/大规模场景；
- **自定义生成器**：用 Go/Python 生成 exposition 或 remote write 流量；
- **remote write 压测**：用 `prometheusremotewrite` 发送端/负载生成器，测接收端队列/重试/磁盘。

合成数据的陷阱：

- 所有样本值相同 → 压缩率虚高；
- 标签完全随机 → 索引/postings 成本虚高；
- 没有 churn → head 内存低估；
- 没有真实查询 → 只测了写入，不代表生产。

## 48.5 查询基准

### 48.5.1 查询语料

从生产查询日志/面板/告警中抽样，形成固定语料：

- instant：`up`、`sum(rate(...))`、`topk`、`histogram_quantile`；
- range：1h/6h/24h/7d，step 15s/1m/5m；
- 高基数：`by (pod)`、正则匹配、`group_left`；
- 坏查询：全匹配、无时间范围、嵌套子查询。

每条查询记录：表达式、时间范围、step、期望结果规模、缓存状态。

### 48.5.2 指标

- 延迟 P50/P90/P99/P999；
- 吞吐（queries/s）；
- 错误率（timeout/execution）；
- 每次查询扫描的样本数/序列数；
- CPU/内存峰值；
- 冷/热缓存差异。

Prometheus 暴露：

```promql
histogram_quantile(0.99,
  sum by (le) (rate(prometheus_engine_query_duration_seconds_bucket[5m])))
rate(prometheus_engine_queries[5m])
prometheus_engine_queries_concurrent_max
```

（具体指标名随版本变化，以实际 `/metrics` 为准。）

### 48.5.3 并发与排队

用 Little's Law 估算：

```text
并发查询数 L = 到达率 λ × 平均停留时间 W
```

如果单查询 P99 是 2s，QPS 是 20，则平均并发约 40；峰值可能更高。Prometheus 的查询并发没有每租户隔离，高并发会互相拖慢。压测要测：

- 固定并发下的延迟分布；
- 延迟随并发的拐点；
- 超时/取消行为；
- 大查询对小查询的影响（噪声邻居）。

## 48.6 规则与告警基准

规则求值是"隐藏的查询负载"：

- 每个规则组按 interval 求值；
- 规则表达式可能扫描大量序列；
- recording rules 会写回 TSDB，增加摄入；
- 告警规则的状态机/模板也会消耗 CPU。

测：

```promql
rate(prometheus_rule_evaluation_duration_seconds_sum[5m])
  / rate(prometheus_rule_evaluation_duration_seconds_count[5m])
prometheus_rule_group_last_duration_seconds
prometheus_rule_group_interval_seconds
```

关注：

- 规则求值时间是否接近 interval（接近就会堆积）；
- 规则组数量与并发；
- 规则产生的序列数；
- 告警风暴时的通知队列。

压测方法：用真实规则文件 + 合成数据，逐步增加规则组/序列数，观察 `last_duration` 与 `interval` 的比值。超过 0.5 就应优化/拆分。

## 48.7 端到端压测

端到端要覆盖：

```text
targets → scrape → TSDB → rules → remote write → 查询/告警
```

场景：

1. **稳态**：目标负载 + 查询语料 + 规则，持续 24h；
2. **峰值**：2～3 倍稳态，持续 1h；
3. **故障注入**：目标大量失败、remote write 接收端 500、磁盘慢、网络分区；
4. **恢复**：重启 Prometheus，测 WAL replay 时间；
5. **升级**：旧版本数据目录 → 新版本，测兼容/回放；
6. **长跑**：7 天，观察 compaction/retention/磁盘增长。

故障注入时观察：

- `up == 0` 的比例；
- WAL 增长/磁盘是否打满；
- remote write 队列是否堆积；
- 查询是否超时；
- 告警是否重复/丢失；
- 恢复后是否补数据/重复。

## 48.8 解释结果：找瓶颈

常见瓶颈：

| 现象 | 可能瓶颈 | 验证 |
|---|---|---|
| 写入吞吐上不去，CPU 高 | 解析/relabel/TSDB append | CPU profile、`scrape_duration_seconds` |
| WAL fsync 延迟高 | 磁盘 IOPS/fsync | `wal_fsync_duration`、iostat |
| 查询慢，CPU 高 | 高基数/大范围/正则 | 查询日志、pprof |
| 查询慢，内存高 | 结果物化/大量序列 | `query.max-samples`、heap profile |
| 规则求值超时 | 规则太重/组太多 | `rule_group_last_duration` |
| compaction 拖慢写入 | 磁盘/CPU/block 太多 | compaction 指标、block 数 |
| head 内存涨 | 活跃序列/churn | `head_series`、TSDB stats |

不要只看平均 CPU。用 pprof（`/debug/pprof`）看 CPU/heap/goroutine；用 `iostat`/`pidstat` 看磁盘；用 `perf` 看内核态。压测报告要给出"瓶颈是什么、证据是什么、下一次实验怎么验证"。

## 48.9 容量验证与回归

容量验证不是一次性报告，而是持续过程：

- **黄金数据集**：固定序列/标签/查询语料，版本升级前后跑同一套；
- **阈值**：吞吐、P99、内存、磁盘增长、恢复时间；
- **CI/nightly**：小规模回归；大规模按周/月；
- **对比发布**：新版本 vs 旧版本，同硬件同数据；
- **容量模型**：把实验结果转成"每 10 万序列需要多少 CPU/内存/磁盘"；
- **余量**：生产保留 30%～50% 余量，应对峰值/compaction/故障。

回归失败时先排除环境差异（缓存、磁盘、后台任务），再定位代码/配置变化。

## 48.10 一个可复现的 Mini 实验

Mini 是 Python 实现，不能代表生产性能；但它可以演示"查询成本随序列数/点数增长"的方法。

```python
from __future__ import annotations

import time

from mini_prometheus.labels import Labels
from mini_prometheus.promql.engine import Engine, InMemoryQuerier

def benchmark(series_count: int, points: int, query: str) -> tuple[float, int]:
    querier = InMemoryQuerier()
    for index in range(series_count):
        labels = Labels(
            {
                "__name__": "bench_requests_total",
                "job": "bench",
                "instance": f"host-{index:04d}",
            }
        )
        querier.add_series(
            labels,
            [(step * 15_000, float(step)) for step in range(points)],
        )
    engine = Engine(querier)
    started = time.perf_counter()
    result = engine.query(query, (points - 1) * 15_000)
    elapsed = time.perf_counter() - started
    return elapsed, len(result.value.items)

for count in (100, 1_000, 5_000):
    elapsed, series = benchmark(count, 240, "sum(rate(bench_requests_total[5m]))")
    print(f"series={series:5d} elapsed={elapsed:.4f}s")
```

观察：

- 序列数增加，`sum(rate(...))` 的耗时近似线性增长；
- 改成 `sum by (instance)` 会产生更多结果序列，内存/时间增加；
- 把 `[5m]` 改成 `[1h]` 会增加扫描样本数；
- Python 的绝对值没有意义，**增长趋势**和**瓶颈位置**才有意义。

真实 Prometheus 的对应实验：

```bash
# 生成 TSDB 基准数据（具体参数以 promtool help 为准）
promtool tsdb bench write --samples=1000000 /tmp/bench-data

# 分析 block 的序列/标签/压缩情况
promtool tsdb analyze /tmp/bench-data

# 用固定查询语料压测（prombench 或自建客户端）
```

## 48.11 报告模板

```text
1. 目标与结论（一句话）
2. 环境（版本/硬件/磁盘/网络）
3. 数据集（序列/样本/标签基数/churn）
4. 工作负载（抓取/查询/规则/并发/时长）
5. 结果
   - 写入：samples/s、CPU、内存、WAL、磁盘增长
   - 查询：P50/P90/P99、QPS、错误率、扫描样本
   - 规则：求值时间/interval、告警延迟
   - 恢复：WAL replay、重启时间
6. 瓶颈与证据（pprof/iostat/日志）
7. 容量模型（每 10 万序列的资源）
8. 风险与余量
9. 复现步骤
10. 建议（配置/架构/版本）
```

## 48.12 常见误区

- **只测写入**：生产查询/规则可能才是瓶颈。
- **只测瞬时**：compaction/retention 的抖动在几分钟后才出现。
- **忽略 churn**：新序列会持续增加 head 内存/postings。
- **用平均值**：P99 才是用户体验。
- **把限制调大**：`query.max-samples` 是保护，不是性能优化。
- **不同版本/硬件对比**：结论不可比。
- **没有基线**：无法判断"变慢了"。
- **把 Mini 数字当生产**：Python 实现只用于教学方法。

## 本章小结

- 基准的可信度取决于工作负载模型、环境记录和可复现性。
- 摄入由活跃序列/抓取间隔决定；查询由匹配序列 × 点数决定；规则是隐藏负载。
- 写入压测要覆盖 block flush/compaction；查询压测要覆盖冷/热缓存和并发拐点。
- 端到端压测要包含故障注入、重启恢复和升级验证。
- 用 pprof/iostat/指标找瓶颈，不要猜。
- 容量模型要留余量，回归要固定黄金数据集。
- Mini 只能演示增长趋势，不能代表生产性能。

## 延伸阅读

- Prometheus 官方文档 "Storage"、"Querying"、"Configuration"。
- Prometheus `prombench`、`promtool tsdb` 文档。
- Thanos `bench` 工具与 Mimir/VictoriaMetrics 的基准博客。
- Brendan Gregg：Systems Performance（USE 方法）。
- 本教程第 22 章（容量）、第 17 章（查询优化）、第 27 章（基数）与附录 F/Y。

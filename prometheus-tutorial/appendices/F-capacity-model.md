# 附录 F 容量模型与估算表

## F.1 输入

```text
T：目标数
S_t：每目标平均活跃序列数
f：抓取频率（次/秒）
R：保留时间（秒）
C：每样本压缩后字节（1～2）
M：每序列内存（1～4 KB）
Q：查询并发/典型样本量
```

## F.2 公式

```text
活跃序列 S = T × S_t
样本摄入 = S × f
总样本 = S × f × R
磁盘 = 总样本 × C × 安全系数
内存 ≈ S × M + 查询/规则临时
```

安全系数：

- compaction 临时空间：1.5～2；
- WAL：摄入 × 恢复窗口；
- 文件系统：1.1；
- 峰值/增长：1.5～2。

## F.3 算例表

| 目标 | 每目标序列 | S | 间隔 | 摄入/s | 15d 磁盘（1.5B） |
|---|---|---|---|---|---|
| 50 | 200 | 10k | 15s | 667 | 1.3 GB |
| 200 | 300 | 60k | 15s | 4k | 7.8 GB |
| 500 | 400 | 200k | 15s | 13.3k | 25.9 GB |
| 1000 | 500 | 500k | 30s | 16.7k | 32.4 GB |

注意：表中"15d 磁盘"是按 1.5 B/sample 的原始估算，实际会因标签/索引/副本/压缩差异很大；请用 F.4 的计算器按你的数据重算，不要直接引用。

## F.4 计算器（Python）

```python
def estimate(targets, series_per_target, interval_s, retention_days,
             bytes_per_sample=1.5, mem_kb_per_series=2, factor=2.0):
    series = targets * series_per_target
    samples_per_sec = series / interval_s
    samples = samples_per_sec * retention_days * 86400
    disk_gb = samples * bytes_per_sample * factor / 1e9
    memory_gb = series * mem_kb_per_series / 1e6
    return {
        "series": series,
        "samples_per_sec": round(samples_per_sec),
        "disk_gb": round(disk_gb, 1),
        "memory_gb": round(memory_gb, 1),
    }

print(estimate(500, 400, 15, 15))
```

## F.5 查询容量

```text
查询样本 ≈ 命中序列 × (range/step + 1)
```

保护：

- `--query.max-samples`；
- `--query.max-concurrency`；
- `--query.timeout`。

如果典型查询接近上限：

- 加 recording rules；
- 缩小 range；
- 拆分面板；
- 增加 Prometheus 实例/查询层。

## F.6 规则容量

```text
规则样本 ≈ 每条规则输出序列 × 求值频率
规则查询成本 ≈ 规则表达式复杂度
```

规则组串行，慢规则会阻塞同组。

## F.7 远端存储

```text
远端摄入 = 本地摄入 × 副本数
远端存储 = 远端样本 × 保留 × 每样本字节
查询成本 = 扫描样本/字节
```

注意：

- 去重不减少摄入，只减少查询重复；
- 多租户/HA 副本会放大；
- 对象存储请求成本。

## F.8 容量评审表

```text
[ ] 活跃序列（当前/P99/增长）
[ ] 每目标序列分布
[ ] 抓取间隔/超时
[ ] 本地 retention
[ ] 远端 retention/副本
[ ] 磁盘/内存/CPU
[ ] 查询并发/样本量
[ ] 规则数量/复杂度
[ ] 告警量
[ ] 备份/快照空间
[ ] 3/6/12 个月预测
```

## F.9 常见误算

- 用平均序列数忽略长尾；
- 忘记索引/副本/压缩临时空间；
- 用 1 B/sample 过于乐观；
- 忽略查询内存；
- 忽略增长；
- 把 head 内存当成全部内存。

## F.10 从真实 TSDB 反推参数

不要只靠经验值。对现有数据跑：

```bash
promtool tsdb analyze /var/lib/prometheus
```

看：

- block 的 `numSeries`/`numSamples`/`numChunks`；
- 每个指标的 series 数；
- label 值分布；
- block 大小 → 反推 bytes/sample。

反推公式：

```text
bytes_per_sample = block_bytes / num_samples
series_overhead  = head_memory_bytes / head_series   （粗估）
```

注意：

- `block_bytes` 含 index/chunk，不纯是样本；
- 不同指标压缩率差异大（常量序列 vs 随机值）；
- head 内存含 postings/查询临时对象，不能直接除；
- 多副本/远端会放大。

## F.11 WAL、Head 与 Compaction 的空间预算

```text
WAL ≈ 摄入 × 恢复窗口 × 安全系数
head ≈ 2～4h 的样本 + 序列开销（取决于 block range）
compaction 临时 ≈ 参与合并的 block 总大小 × 1～2
snapshot ≈ 当前 blocks + head
```

建议预留：

- 磁盘：`retention 数据 + 2× 最大 compaction + WAL + 20%`；
- 内存：`head + 查询峰值 + 规则 + 20%`；
- 文件句柄：block 数 × 打开文件 + 连接数。

磁盘满时最先坏的是 compaction 和 WAL；不要等 100% 才处理。

## F.12 硬件选型

| 资源 | 建议 | 原因 |
|---|---|---|
| CPU | 与摄入/查询/规则相关，留 30% 余量 | 解析/压缩/求值都是 CPU |
| 内存 | 活跃序列 × 1～4 KB + 查询余量 | head/postings/查询 |
| 磁盘 | 本地 SSD/NVMe，低延迟 | WAL/compaction 随机 IO |
| 容量 | retention + compaction 余量 | 见 F.11 |
| 网络 | 抓取/remote write 带宽 | 大标签/多副本 |
| 文件系统 | ext4/xfs，避免网络盘做 WAL | fsync 延迟 |

经验：

- WAL/head 放本地盘，不要放 NFS；
- 对象存储用于 block/长期存储，不用于 WAL；
- 单实例百万序列需要大内存 + 多核 + SSD；
- 超过单机上限时，先分片/降基数，再考虑分布式。

## F.13 容量回归与扩容触发

监控这些信号：

```promql
# 活跃序列增长
prometheus_tsdb_head_series

# 摄入速率
rate(prometheus_tsdb_head_samples_appended_total[5m])

# 磁盘使用/剩余
prometheus_tsdb_storage_blocks_bytes

# compaction 失败/延迟
prometheus_tsdb_compactions_failed_total
prometheus_tsdb_compaction_duration_seconds

# 查询压力
prometheus_engine_queries
prometheus_engine_query_duration_seconds

# 抓取压力
scrape_duration_seconds
prometheus_target_scrape_pool_targets
```

扩容触发示例：

```text
- head_series 连续 7 天 > 预算 80%
- 磁盘剩余 < retention 数据的 30%
- compaction 持续失败/落后
- 查询 P99 > 目标
- scrape_duration 接近 timeout
```

扩容前先做：

- 基数治理（删高基数标签/指标）；
- 预聚合（recording rules）；
- 分片（hashmod/功能分片）；
- 远端存储卸载历史查询；
- 调整 retention/step。

## 延伸阅读

- 本教程第 22、27 章。
- `promtool tsdb analyze` 的真实数据。

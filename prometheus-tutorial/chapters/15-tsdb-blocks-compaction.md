# 第 15 章 TSDB（二）：Block、Compaction、Retention 与 Tombstone

## 15.1 为什么需要 Block

Head 只在内存中保留近期数据。要长期保存并支持重启，必须把数据落成不可变的 block：

```text
blocks/
  01J8Z.../
    meta.json
    index
    chunks/
      000001
      000002
    tombstones
```

Block 是不可变的：一旦写完，不再修改。删除通过 tombstone 表达，压缩通过写新 block + 删除旧 block 完成。

## 15.2 Block 的时间范围

- 默认每 2 小时切一个 block（`--storage.tsdb.min-block-duration` 附近，实际受 head 切块策略影响）；
- block 的 `minTime`/`maxTime` 是样本时间范围；
- 相邻 block 的时间范围**不应重叠**；
- 查询时按时间范围选择 block，跳过不相关的。

为什么是 2h：

- 与 head 的 chunk 范围匹配；
- 控制单个 block 的大小（便于 compaction/删除）；
- 减少查询时需要打开的 block 数量。

## 15.3 meta.json

```json
{
  "ulid": "01J8Z...",
  "minTime": 1700000000000,
  "maxTime": 1700007200000,
  "stats": {
    "numSeries": 120000,
    "numSamples": 8640000,
    "numChunks": 144000
  },
  "compaction": {
    "level": 1,
    "sources": ["01J8Y...", "01J8Z..."]
  },
  "version": 1
}
```

字段用途：

- `ulid`：block 唯一 ID，按时间排序；
- `minTime`/`maxTime`：查询选择；
- `stats`：容量分析、`/api/v1/status/tsdb`；
- `compaction.level`：压缩层级；
- `sources`：来源 block（审计/调试）。

## 15.4 index 与 chunks

真实 Prometheus 的 index 是自定义二进制，包含：

- 符号表（label names/values）；
- series 列表（label set → series ID）；
- postings（label pair → series IDs）；
- chunk meta（series ID → chunk 时间/偏移）。

chunks 目录按 512MB 左右分段，每个 chunk 有偏移。

Mini 的 index 是 `index.json`：

```json
{
  "series": [
    {
      "labels": [["__name__", "x"], ["job", "api"]],
      "chunks": [
        {"ref": 1, "offset": 0, "length": 128, "minTime": 0, "maxTime": 1800000, "numSamples": 120}
      ]
    }
  ]
}
```

访问模式相同：matcher → 序列 → chunk 元数据 → 读取 chunk 字节 → 解码。

## 15.5 Tombstone

删除序列不是立即重写 block，而是写入 tombstone：

```text
series x{job="api"}: [t1, t2)
```

查询时过滤掉区间内的样本。优点：

- 删除是 O(1) 元数据操作；
- 不产生大量写放大；
- 可以撤销（clean tombstones 前）。

缺点：

- 磁盘空间不会立即释放；
- 查询仍有解码成本；
- tombstone 文件会增长。

`clean_tombstones` 或 compaction 会把 tombstone 应用到数据并重写 block，真正释放空间。

Mini 的 tombstone：

- head：内存 dict ref → intervals；
- block：`tombstones.json`（labels → intervals）；
- 查询时过滤；
- compaction 合并时丢弃已应用的 tombstone；
- `clean_tombstones()` 强制重写。

## 15.6 Compaction

Compaction 解决：

1. **小 block 太多**：查询要打开很多文件；
2. **删除/过期数据**：tombstone 和 retention 需要重写；
3. **重复/重叠**：合并去重；
4. **索引膨胀**：重写索引。

### 垂直压缩（vertical）

把时间范围重叠的 block 合并成一个。正常抓取不会产生重叠，但以下情况会：

- 多个 Prometheus 副本写同一存储（不推荐）；
- 导入/回填；
- 崩溃恢复产生的重叠。

### 水平压缩（horizontal）

把相邻的小 block 合并成大 block。真实 Prometheus 的默认 range 是 2h → 6h → 18h → 54h（按 3 倍增长，而不是简单翻倍）：

```text
2h × 3 → 6h
6h × 3 → 18h
18h × 3 → 54h
...
```

层级（level）随合并增大。大 block 减少文件数，但单次 compaction 成本更高。具体 range 与 `--storage.tsdb.min-block-duration`/`max-block-duration` 相关，以版本为准。

### 压缩策略

Prometheus 的 compaction 是后台任务，按 range 选择候选：

- 优先合并同 level、时间相邻的 block；
- 控制单次合并的 block 数量/大小；
- 避免频繁重写大 block。

Mini 的策略更简单：

- `force=True` 合并所有 block；
- 默认合并时间重叠的 block；
- 4 个以上 block 时合并全部；
- retention 单独删除过期 block。

这不是生产级策略，但足以演示"合并 → 新 block → 删除旧 block"的流程。

## 15.7 Retention

两种保留策略：

```yaml
--storage.tsdb.retention.time=15d
--storage.tsdb.retention.size=500GB
```

- time：删除 `maxTime < now - retention` 的 block；
- size：按 block 总大小删除最旧的，直到低于阈值；
- 两者同时配置时，先触发哪个用哪个；
- retention 不影响 head（head 由内存/切块控制）。

注意：

- 删除以 block 为单位，实际保留时间可能略长于配置；
- 长时间不 compaction 会导致小 block 堆积，retention 删除粒度变粗；
- 远程存储的 retention 由后端决定，Prometheus 本地 retention 只影响本地。

## 15.8 Snapshot

`POST /api/v1/admin/tsdb/snapshot` 创建一致性快照：

- 包含当前 blocks 和 head；
- 用于备份/迁移/调试；
- 快照目录在 `snapshots/<ulid>`；
- 快照是硬链接/复制的只读数据，不影响运行。

备份策略：

- 定期快照 + 对象存储；
- 或直接备份 block 目录（需要停写/一致性保证）；
- 恢复时把 block 放回 `blocks/`，启动时加载；
- 跨版本恢复要注意索引格式兼容性。

Mini 的 snapshot 会复制 blocks，并把 head 写成一个 level=0 的 block。

## 15.9 Block 的查询选择

查询时间范围 `[mint, maxt]`：

```text
for block in blocks:
    if block.maxTime < mint or block.minTime > maxt: skip
    else: scan block
```

再加上 head 的 `[head.minTime, head.maxTime]`。

如果 block 有 tombstone，查询时过滤；如果 block 完全被删除，仍会扫描（直到 compaction）。

## 15.10 容量与性能

Block 相关指标：

```promql
prometheus_tsdb_blocks_loaded
prometheus_tsdb_storage_blocks_bytes
prometheus_tsdb_compactions_total
prometheus_tsdb_compaction_duration_seconds
prometheus_tsdb_compactions_failed_total
prometheus_tsdb_head_series
```

调优：

- 磁盘用 SSD；compaction 是 IO 密集型；
- 留足够磁盘空间（compaction 需要临时空间）；
- 避免频繁重启（每次重启要 replay WAL）；
- 监控 compaction 失败（磁盘满/权限/损坏）。

## 15.11 Mini 的 Block 实现

`tsdb/block.py`：

- `BlockWriter`：收集 labels + chunk，写 `chunks.bin`、`index.json`、`meta.json`；
- `Block`：加载 index，按 matcher/time 查询，读 chunk 并解码；
- `write_merged_block()`：合并多个 block，按时间戳去重，重写 chunk；
- tombstone 的读写。

`tsdb/tsdb.py`：

- `flush_head()`：整 chunk 切块，写 block，删 head chunk，checkpoint + 截断 WAL；
- `compact()`：flush + 合并 + retention；
- `_apply_retention()`：按 maxTime 删除；
- `snapshot()`：复制 blocks + head。

测试覆盖：

- flush 后重启仍可查询；
- 部分 flush 保留近期 head；
- 多 block 合并去重；
- retention 删除；
- tombstone 持久化与 clean；
- snapshot 内容。

## 15.12 Block 文件格式逐层

真实 Prometheus block 目录：

```text
01J8Z.../
  meta.json
  index
  chunks/
    000001
    000002
  tombstones
```

### meta.json

除了 `ulid`、`minTime`、`maxTime`、`stats`、`compaction`，还可能包含：

- `version`：block 格式版本；
- `outOfOrder`：是否为乱序 block；
- `compaction.sources`：源 block 列表；
- `stats.numSeries/numSamples/numChunks`；
- `thanos` 等外部工具写入的元数据。

### index

Index v2 是自定义二进制，主要段：

```text
Symbols              label name/value 符号表
Series               series ID → label set
Label Index          label name → label values
Postings             label pair → sorted series IDs
Postings Offset      label pair → postings 偏移
TOC                  各段偏移/长度
```

读取流程：

```text
读 TOC
  → 读 Symbols（按需）
  → matcher → postings 列表
  → 交集/并集 → series ID
  → 读 Series 段 → label set
  → 读 chunk meta（时间/偏移）
  → 打开 chunks 文件读字节
```

### chunks

chunk 文件按段存储，chunk meta 记录 `offset/length/minTime/maxTime`。读取时按偏移 `pread`，不需要顺序扫描整个文件。

### tombstones

记录 `series → 删除区间`。查询时过滤；compaction 时应用并重写。

Mini 的 `index.json` 把 label set 和 chunk meta 放在一起，没有 symbols/postings/TOC；`chunks.bin` 是顺序追加的 XOR chunk。访问模式相同，但规模上限差几个数量级。

## 15.13 Compaction 的计划与层级

真实 Prometheus 的 compaction range 不是简单的"2h 翻倍"，而是一组预定义区间：

```text
2h → 6h → 18h → 54h → ...
```

默认参数：

```text
--storage.tsdb.min-block-duration=2h
--storage.tsdb.max-block-duration=36h   （或 retention 的 10%）
```

compaction 的决策大致是：

1. 找出时间范围重叠的 block（vertical compaction）；
2. 找出同 level、相邻、总范围不超过目标 range 的 block（horizontal）；
3. 选择成本最低/收益最高的一组；
4. 写新 block → fsync → 原子 rename → 删除旧 block；
5. 更新 `meta.json` 的 `compaction.level/sources`。

真实实现还有：

- **并发限制**：同时只跑少量 compaction，避免打满 IO；
- **限流**：`--storage.tsdb.max-block-duration` 与磁盘空间检查；
- **失败重试**：磁盘满/权限/损坏时保留现场并告警；
- **删除延迟**：旧 block 删除失败可重试，避免"新 block 已写、旧 block 还在"导致重复。

Mini 的策略：

- 默认只合并时间重叠的 block；
- `force=True` 合并全部；
- 4 个以上 block 时合并全部；
- 没有 level/range 规划，也没有并发控制。

这足以演示"合并 → 新 block → 删除旧 block"，但不能直接搬到生产。

## 15.14 Retention 的边界与删除

Retention 以 block 为单位，因此实际保留时间可能**长于**配置：

```text
retention=15d
block 范围=2h
→ 最旧 block 可能到 15d + 2h 才被删
```

时间 retention：

```text
删除 maxTime < now - retention 的 block
```

大小 retention：

```text
按 block 总字节数从旧到新删除，直到低于阈值
```

注意：

- 同时配置 time/size 时，任一触发即删除；
- `--storage.tsdb.retention.size` 统计的是 block 目录大小，不含 WAL/head；
- 正在被查询的 block 不能立即删；真实实现用引用计数/延迟删除；
- remote write 的远端 retention 独立配置；
- 删除后磁盘空间可能不会立刻回收（文件句柄/文件系统）。

相关指标：

```promql
prometheus_tsdb_blocks_loaded
prometheus_tsdb_storage_blocks_bytes
prometheus_tsdb_retention_cutoffs_total
prometheus_tsdb_retention_duration_seconds
```

## 15.15 Snapshot、备份与恢复

### Snapshot 的语义

`POST /api/v1/admin/tsdb/snapshot`：

- 对当前 block 做硬链接/复制；
- 把 head 写成一个 level=0 的 block；
- 返回快照目录（`snapshots/<ulid>`）；
- 快照是某一时刻的一致视图，不影响写入。

### 备份策略

```text
方案 A：定期 snapshot + 对象存储
方案 B：直接同步 blocks/（需要保证不并发删除/写入）
方案 C：远端长期存储（Thanos/Mimir）+ 本地不作为备份
```

要点：

- 快照可能包含 tombstone 尚未清理的数据；
- 备份要加密、限权、异地/跨区按合规要求；
- 恢复前校验 block 完整性；
- 跨版本恢复要验证索引格式；
- 定期做恢复演练，记录 RPO/RTO。

### 恢复顺序

```text
停止 Prometheus
  → 备份现有数据目录
  → 放入 blocks/
  → 保留/重建 WAL（通常不恢复旧 WAL）
  → 启动，检查日志与 /api/v1/status/tsdb
  → 验证关键查询/规则
```

不要直接把快照目录当 `--storage.tsdb.path`；它是 block 的集合，需要放回 `blocks/`。

## 15.16 Block 工具与故障处理

```bash
# 列出 block
promtool tsdb list /var/lib/prometheus

# 分析（series/label/压缩）
promtool tsdb analyze /var/lib/prometheus

# 导出
promtool tsdb dump /var/lib/prometheus > dump.txt

# 清理 tombstone（停机）
promtool tsdb clean-tombstones /var/lib/prometheus
```

常见故障：

| 症状 | 可能原因 | 处理 |
|---|---|---|
| 启动报 `invalid index` | 磁盘损坏/非正常关机 | 备份后删除损坏 block，从远端/快照恢复 |
| `compaction failed` | 磁盘满/权限/文件句柄 | 扩容、修权限、重启 |
| 查询结果缺数据 | block 时间范围/tombstone/retention | 查 `meta.json`、`/api/v1/status/tsdb` |
| 磁盘不降 | tombstone 未清理/block 被引用 | `clean_tombstones`、重启、检查句柄 |
| 重复样本 | 多副本写同一目录/回填 | 单写者、去重、检查 remote write |

处理顺序：**先备份，再定位 block，再决定删除/恢复**。不要在生产直接 `rm -rf blocks/*`。

## 15.17 与 Mini 的对照

| 方面 | Mini | Prometheus |
|---|---|---|
| index | `index.json`，无 postings | 二进制 index v2，postings |
| chunks | `chunks.bin` 顺序追加 | 分段文件 + 偏移 |
| compaction | 重叠/全量合并 | range/level 规划 + 限流 |
| retention | 按 maxTime 删除 | time/size + 引用计数 |
| snapshot | 复制 block + head | 硬链接 + head block |
| 故障恢复 | 拒绝损坏 WAL | 类似的“宁可失败”原则 |

Mini 的 `write_merged_block()` 会把所有样本读进内存再写，复杂度 O(samples)，不适合大 block。生产实现必须流式合并、控制内存、支持中断续跑。

## 本章小结

- Block 是不可变的时间片，meta/index/chunks 三件套；删除用 tombstone。
- Compaction 合并小 block、应用 tombstone、释放空间；分垂直/水平。
- Retention 按时间或大小删除 block，粒度较粗。
- Snapshot 是一致性备份，跨版本恢复需验证格式。
- Mini 用 JSON index + 自定义 chunk 文件演示同样的访问模式。

## 延伸阅读

- Prometheus 官方 "Storage" 与 `tsdb` 文档。
- Prometheus 源码 `tsdb/compact.go`、`tsdb/block.go`。
- 本教程第 14 章（head/WAL）与第 35 章（Mini block）。

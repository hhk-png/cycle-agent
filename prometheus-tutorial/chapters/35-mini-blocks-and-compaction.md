# 第 35 章 Mini 实现（四）：Block、Compaction 与 Retention

## 35.1 目录布局

```text
data/
  lock
  checkpoint.json
  wal/00000000
  blocks/
    01J.../
      meta.json
      index.json
      chunks.bin
      tombstones.json   # 可选
  snapshots/
```

Block 目录名是 ULID（26 字符 Crockford base32），按时间排序：

```python
def new_ulid(timestamp_ms=None):
    # 48-bit time + 80-bit randomness, monotonic within a millisecond
```

## 35.2 BlockWriter

```python
writer = BlockWriter()
writer.add_chunk(labels, chunk)
block = writer.write(path, level=1, tombstones=...)
```

写入过程：

1. 按标签排序 series；
2. 追加 chunk 字节到 `chunks.bin`，记录 offset/length/min/max/numSamples；
3. 写 `index.json`；
4. 写 `meta.json`（ULID、时间范围、stats、level、sources）；
5. 可选写 `tombstones.json`。

`chunks.bin` 只是 chunk 字节的拼接，索引里有偏移。读取时：

```python
data_file.seek(meta.offset)
data = data_file.read(meta.length)
XORChunk.from_bytes(data).samples()
```

## 35.3 Block 查询

```python
def select(self, matchers, mint, maxt):
    if self.max_time < mint or self.min_time > maxt:
        return []
    for series in self.index():
        if not match_all(matchers, series.labels):
            continue
        for meta, points in self.chunks_for(series):
            if meta.max_time < mint or meta.min_time > maxt:
                continue
            for ts, value in points:
                if mint <= ts <= maxt and not self._is_deleted(labels, ts):
                    ...
```

`chunks_for()` 有简单的 chunk 缓存（最多 256 条），避免重复解码。

## 35.4 Tombstone

Block 的 `tombstones.json`：

```json
{
  "series": [
    {
      "labels": [["__name__", "x"], ["job", "api"]],
      "intervals": [[1700000000000, 1700000600000]]
    }
  ]
}
```

- `delete()` 给匹配 series 添加区间；
- 查询时 `_is_deleted()` 过滤；
- `clean_tombstones()` 触发 force compaction，重写 block 并丢弃已应用的区间；
- 合并 block 时，`all_samples()` 已经过滤 tombstone，因此新 block 不需要保留旧 tombstone。

Mini 的早期实现把 tombstone 继续传给新 block，导致 `clean_tombstones()` 后 `tombstones()` 仍非空、空间不释放；测试 `test_clean_tombstones_rewrites_block` 暴露了这个问题。

## 35.5 Head flush

`TSDB.flush_head(force=False)`：

```python
cut = head.max_time - block_range  # force=True 时 cut = head.max_time
chunks = head.chunks_before(cut)  # 整 chunk
writer = BlockWriter()
for labels, chunk in chunks:
    writer.add_chunk(labels, chunk)
block = writer.write(blocks_dir / new_ulid(), level=1, tombstones=...)
head.remove_chunks_before(cut)
recalculate_head()
checkpoint_and_truncate_wal()
```

注意：

- 只移动 `max_time <= cut` 的整 chunk；
- 跨越 cut 的 chunk 留在 head；
- 如果 head 的时间跨度不足 block_range，`force=False` 不切块；
- 切块后写 checkpoint 并截断 WAL。

## 35.6 Compaction

`compact(force=False, now=None)`：

```text
flush_head()
_compact_blocks(force)
_apply_retention(now)
```

`_compact_blocks`：

- 按 minTime 排序；
- 时间重叠的 block 分到一组；
- 4 个以上 block 时合并全部；
- `force=True` 合并全部；
- 每组写一个新 block（level=max+1），删除旧 block。

合并使用 `write_merged_block()`：

```python
merged: dict[Labels, dict[int, float]] = {}
for block in blocks:
    for labels, samples in block.all_samples():
        merged[labels][ts] = value
for labels:
    for ts in sorted(points):
        chunk.append(ts, value)  # 每 120 样本切 chunk
```

问题：

- 合并全部 block 会让 retention 粒度变粗；
- 不相邻的 block 合并后，旧数据可能因为新 block 的 maxTime 未过期而无法删除。

Mini 的测试 `test_retention_deletes_old_blocks` 暴露了"默认合并了不相邻 block"导致 retention 失效；修复为"默认只合并时间重叠的 block，force 才全合并"。

## 35.7 Retention

```python
threshold = now - retention_ms
for block in blocks:
    if block.max_time < threshold:
        remove(block)
```

- 以 block 为单位；
- 只删完全过期的 block；
- 部分过期的 block 要等 compaction 重写（Mini 不做部分裁剪）；
- `now` 可注入，便于测试。

## 35.8 Snapshot

```python
def snapshot(destination=None):
    root = destination or data/snapshots/<ulid>
    for block in blocks:
        copy meta/index/chunks/tombstones
    if head.num_series:
        writer.write(root / "head", level=0)
```

- 复制 block 文件；
- head 写成 level=0 block；
- 返回路径；
- API 只返回目录名。

真实 Prometheus 用硬链接/文件复制，保证一致性；Mini 在 TSDB 锁内执行。

## 35.9 并发与锁

- `TSDB._lock` 保护 blocks 列表、flush、compact、delete、snapshot；
- Block 不可变，查询可并发；
- head 有自己的锁；
- 查询先复制 block 列表，再释放 TSDB 锁。

## 35.10 测试

`tests/test_tsdb.py`：

- flush 全量/部分；
- 重启恢复；
- 多 block 合并；
- retention；
- tombstone 持久化/clean；
- snapshot；
- block meta JSON；
- 100 序列冒烟。

## 35.11 Index 布局与 compaction 计划

Mini 的 block 目录刻意保持可读：

```text
<ULID>/
  meta.json        # minTime/maxTime/level/sources/numSeries/numSamples
  index.json       # series: [{labels, chunks: [{ref, offset, length, minTime, maxTime}]}]
  chunks.bin       # 按 offset/length 顺序拼接的 XOR chunk 字节
  tombstones.json  # 可选：删除区间
```

`index.json` 是**线性数组**，不是 Prometheus 的 postings 列表。查询时 `Block.select()` 先按 block 的全局时间范围剪枝，再遍历每条 series 的 labels 做 matcher 匹配，最后按 chunk 的 min/max 时间跳到需要的 chunk。代价是 O(series)；收益是格式可读、无需维护 postings 的增删改。第 16 章讨论真实 Prometheus 的 postings 与查询路径，Mini 的差异在附录 G 明确记录。

compaction 的规划规则：

1. 先 flush head（超过 block range 或强制）；
2. 默认只合并**时间重叠**的相邻 block；`force=True` 才合并所有 block；
3. 合并后的 `level = max(level) + 1`，`sources` 记录来源 ULID；
4. 合并通过 `write_merged_block()` 重写样本，tombstone 在 `all_samples()` 中已被应用，因此新 block 天然不带 tombstone，空间得以回收；
5. retention 只删除 `block.max_time < now - retention` 的 block（严格小于）；跨阈值边界的 block 会保留到下一次 compaction/retention。

为什么默认不合并所有 block？如果把一个很旧的 block 和一个很新的 block 合并，新 block 的 `maxTime` 会变新，retention 就无法按时间淘汰旧数据，长期存储会越滚越大。这是 Mini 在第二轮审计中修复的真实缺陷（见第 42 章）。

生产对照：

- 真实 Prometheus 的 compaction 有 level、range、postings 重写、垂直 compaction（同一时间范围的多个 block）；
- Mini 没有 vertical compaction，也没有 block 的 postings/符号表；
- 真实 retention 以 block 为单位删除，边界语义类似，但还有 `--storage.tsdb.retention.time/size` 的组合；
- 对拍时优先看 `meta.json` 的 minTime/maxTime/level 与 `promtool tsdb analyze` 的 series/label 统计。

## 本章小结

- Block = meta/index/chunks/tombstones，写后不可变。
- Head flush 只移动整 chunk，跨越 cut 的留到下次。
- Compaction 合并重叠 block；force 才全合并，避免 retention 粒度失控。
- Tombstone 查询时过滤，compaction 后丢弃。
- Snapshot 复制 block + head。

## 延伸阅读

- 本教程第 15 章（block 原理）与第 36 章（查询）。
- `mini_prometheus/tsdb/block.py`、`tsdb.py`。

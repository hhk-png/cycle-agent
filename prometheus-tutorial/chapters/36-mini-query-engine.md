# 第 36 章 Mini 实现（五）：TSDB 查询与序列合并

## 36.1 TSDB.select

```python
def select(self, matchers, mint, maxt, apply_tombstones=True):
    blocks = list(self._blocks)
    merged: dict[Labels, dict[int, tuple[float, int]]] = {}
    for block in blocks:
        for labels, samples in block.select(matchers, mint, maxt):
            target = merged.setdefault(labels, {})
            for sample in samples:
                target[sample.timestamp] = (sample.value, 0)
    for labels, samples in self.head.select(
        matchers, mint, maxt, apply_tombstones=apply_tombstones
    ):
        target = merged.setdefault(labels, {})
        for sample in samples:
            target[sample.timestamp] = (sample.value, 1)
    ...
```

要点：

- block 优先写，head 后写（相同时间戳 head 覆盖）；
- 去重按 timestamp；
- 最后按时间排序；
- 返回 `[(Labels, [Sample])]`。

真实 Prometheus 的 `SeriesSet` 是惰性迭代器；Mini 一次性物化，简单但内存高。

## 36.2 时间范围

- `mint > maxt` 返回空；
- block/head 各自裁剪；
- head 的 `select` 会检查 series 的 min/max time；
- stale 样本保留在结果中（由 PromQL 层决定是否跳过）。

## 36.3 series/labels/values

`series(matchers, mint, maxt)`：

- block：时间范围重叠 + matcher；
- head：`head.select()` 拿到有时间范围内样本的 labels；
- 去重排序。

`label_names()` / `label_values(name)`：

- 遍历所有 block 的 label_sets + head 的 all_label_sets；
- 去重排序。

这些 API 没有 postings，复杂度 O(series)。

## 36.4 删除

```python
def delete_series(self, matchers, mint, maxt):
    head.delete(matchers, mint, maxt)
    wal.log_tombstones(...)
    for block in blocks:
        block.delete(matchers, mint, maxt)
```

- head 记录内存 tombstone；
- block 写 `tombstones.json`；
- WAL 记录 tombstone（重启恢复 head tombstone）；
- 不立即重写 block。

## 36.5 统计与自监控

`head_stats()`：

```json
{"numSeries": 100, "numSamples": 2000, "numChunks": 17, "numTombstones": 0}
```

`stats`：

```python
{"blocksFlushed", "blocksCompacted", "blocksDeleted",
 "samplesAppended", "outOfOrderSamples", "duplicateSamples", "walTruncations"}
```

API `/metrics` 暴露部分；`/api/v1/status/tsdb` 暴露 head/blocks。

## 36.6 Watchers

```python
def add_watcher(self, callback):
    self._watchers.append(callback)
```

- append/append_batch 成功后调用；
- remote writer 用它在样本写入后入队；
- watcher 异常被吞掉，不影响写入；
- 没有持久化 offset，重启会丢未发送样本（Mini 的限制）。

## 36.7 锁与并发

- `select` 先复制 blocks 列表，再释放 TSDB 锁；
- head.select 内部加锁；
- 查询期间 compaction 可能删除 block 目录 → 已打开的 Block 对象仍可读（文件句柄），但 Mini 的 `block.remove()` 直接 `shutil.rmtree`，Windows 上可能失败。

生产实现用引用计数/延迟删除；Mini 在测试中避免并发 compact + query。

## 36.8 查询接口

`Querier` 协议：

```python
class Querier(Protocol):
    def select(self, matchers, mint, maxt) -> list[tuple[Labels, list[Sample]]]:
        ...
```

`Engine` 只依赖这个协议：

- `TSDB` 实现它；
- `InMemoryQuerier` 用于测试；
- 可以替换成远程 querier（未实现，但接口存在）。

## 36.9 复杂度

- `TSDB.select`: O(blocks × series × chunks)
- `series/labels`: O(series)
- 删除：O(series)
- compaction：O(samples)

Mini 的 100 序列测试只验证语义，不代表规模。

## 36.10 与真实 Prometheus 的差异

| 方面 | Mini | Prometheus |
|---|---|---|
| 索引 | 线性扫描 | postings |
| SeriesSet | 物化列表 | 惰性迭代 |
| 合并 | dict 去重 | 有序归并 |
| 删除 | 简单区间 | tombstone + 压缩 |
| 并发 | 粗锁 | 细粒度/shard |

## 36.11 一次查询的完整走读

假设：

```text
序列：http_requests_total{job="api",instance="a:9100",code="500"}
查询：[t0-5m, t0]，step 15s
数据：两个 block + head
```

路径：

```text
Engine.query_range(expr, start, end, step)
  → parser.parse(expr) → AST
  → engine._eval(node, ts) for each ts
      → _eval_matrix_selector(selector, ts)
          → tsdb.select(matchers, ts-5m, ts)
              → block.select() × N
              → head.select()
              → merge/去重/排序
          → 返回 Matrix
      → rate(matrix)
      → 聚合/输出
```

细节：

1. `matchers` 先校验"至少一个非空 matcher"，避免全表扫描；
2. block 先按 `minTime/maxTime` 裁剪，再按 chunk meta 裁剪；
3. head 按 series 的 min/max 裁剪；
4. 同时间戳：block 先写 `(value, 0)`，head 后写 `(value, 1)`，head 覆盖；
5. tombstone 在 block/head 各自过滤；
6. 最终按标签排序、按时间排序；
7. PromQL 层再把 `Sample` 转成函数需要的结构。

Mini 的 `select` 返回 `list[tuple[Labels, list[Sample]]]`，所有数据一次性物化；真实 `SeriesSet` 是惰性迭代，函数可以边读边算。

## 36.12 并发、锁与块删除

Mini 的并发策略很保守：

- `TSDB.select` 先在锁内复制 `_blocks` 列表，再释放锁；
- `Head.select` 内部加 `RLock`；
- compaction 可能删除 block 目录，但已打开的 `Block` 对象仍持有文件句柄；
- Windows 上 `shutil.rmtree` 遇到打开的文件会失败，测试避免"并发 compact + query"。

真实 Prometheus 用：

- `Block` 的引用计数/延迟删除；
- 不可变 block + 原子 rename；
- `Querier` 关闭时释放引用；
- 查询上下文取消；
- 细粒度锁/无锁数据结构。

要在 Mini 上做并发安全，可以：

1. 给 `Block` 加引用计数，`remove()` 延迟到最后一个 reader 关闭；
2. `select` 期间持有 block 的 snapshot 引用；
3. compaction 写新 block 后原子切换列表；
4. 查询支持 `cancel_event`，在循环中检查。

## 36.13 复杂度与规模上限

Mini 的查询复杂度：

```text
TSDB.select   O(blocks × series × chunks)
series/labels O(series)
delete        O(series)
compaction    O(samples)
```

对比真实 Prometheus：

| 操作 | Mini | Prometheus |
|---|---|---|
| matcher → series | 线性扫描 | postings 交集 |
| 读 chunk | 打开文件 + 解码 | mmap + 惰性迭代 |
| 合并 | dict 去重 | 有序归并 |
| 内存 | 全量物化 | 流式/限额 |
| 删除 | 区间过滤 | tombstone + compaction |

规模实验：

```python
# 生成 10 万序列 × 100 样本，测 select 耗时
# Mini 会明显慢于 Prometheus；这是教学实现的预期结果
```

不要用 Mini 的 benchmark 推断 Prometheus 的性能。Mini 的价值是**语义对照**，不是容量规划。

## 36.14 扩展练习

1. **加 postings 索引**：在 `index.json` 里为每个 label pair 建 `series_id` 列表，`select` 先求交集再扫描；
2. **流式 SeriesSet**：把 `select` 改成生成器，查询层边读边算；
3. **查询取消**：给 `select` 传 `cancel_event`，在 block/chunk 循环中检查；
4. **查询统计**：记录命中 series/chunk/样本数，暴露到 API；
5. **标签索引**：为 `label_names`/`label_values` 建缓存，避免每次遍历；
6. **block 引用计数**：支持并发 compaction + query。

每个练习都要补测试，并保持"语义不变、性能更好"的目标；不要为了性能改变查询结果。

## 本章小结

- TSDB.select 合并 block + head，按时间戳去重，head 覆盖。
- series/labels 通过遍历实现，没有 postings。
- 删除写 tombstone，compaction 才释放空间。
- Watchers 把写入事件传给 remote writer。
- Mini 的查询是教学实现，规模上不可替代真实 TSDB。

## 延伸阅读

- 本教程第 16 章（索引/查询路径）与第 37 章（PromQL 前端）。
- `mini_prometheus/tsdb/tsdb.py`。

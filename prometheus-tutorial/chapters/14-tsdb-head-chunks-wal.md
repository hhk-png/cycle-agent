# 第 14 章 TSDB（一）：Head、Chunk 与 WAL

## 14.1 写入路径

```text
Appender.Add(labels, t, v)
  → head.getOrCreate(labels)
  → memSeries.append(t, v)
  → XOR chunk
  → WAL.Log(series/samples)
```

写入必须：

- 按时间戳单调递增（同一序列）；
- 同一时间戳重复写入相同值可忽略；
- 同一时间戳不同值报错；
- 序列标签集合不可变（要改标签就是新序列）。

## 14.2 Head 的结构

Head 是内存中的"最近数据"：

- `memSeries`：标签 + chunk 列表 + 最后时间戳；
- postings：label → series ID 的倒排索引；
- chunks：每个序列的 XOR 编码块；
- tombstones：删除区间（内存中）；
- 时间范围：通常覆盖最近约 2 小时（一个 block range），具体取决于 `min-block-duration`/切块策略；head 的"未压缩窗口"可能略大于 2h。

Head 的内存占用主要来自：

- 活跃序列的标签集合；
- postings 索引；
- chunk 数据；
- 查询时的临时对象。

高基数会直接放大前三项，因此"内存先于磁盘爆"是常见现象。

## 14.3 Chunk 编码（Gorilla XOR）

Prometheus 的 chunk 使用 Facebook Gorilla 论文的思路：

1. **时间戳**：delta-of-delta 编码。相邻样本的时间差通常稳定（15s），delta 接近 0，delta-of-delta 更接近 0，可用 1 bit 表示。
2. **值**：与前一个值 XOR。稳定指标 XOR 后大量高位/低位为 0，只存储有效中间位。
3. **窗口复用**：记录上次的前导零/尾随零数量，若新值落在同一窗口内，只写有效位。

伪代码：

```text
if ddd == 0: write 1 bit 0
else:
  write 1
  if ddd in [-8192, 8191]: write 2 bits 00 + 14 bits
  elif ... 17/20 bits
  else: 64 bits

xor = bits(value) ^ bits(prev)
if xor == 0: write 1 bit 0
else:
  write 1
  if same window: write 0 + meaningful bits
  else: write 1 + leading(6) + meaningful-1(6) + bits
```

Mini 的 `tsdb/chunkenc.py` 实现了同样的思路，但不是字节级兼容：

- 时间戳 delta-of-delta 布局相同；
- 值的窗口控制简化为 1 bit "复用/新窗口"；
- chunk 序列化带 4 字节样本数前缀。

### 为什么 chunk 有样本数上限

- 单个 chunk 默认 120 个样本；
- 单个 chunk span 不超过 2h；
- 上限保证解码/查询的局部性，避免超大 chunk 导致内存峰值；
- 查询时可以按 chunk 时间范围跳过不相关数据。

## 14.4 Chunk 的切分

一个序列的 chunk 列表按时间递增：

```text
chunk 1: t=0..1785s (120 samples)
chunk 2: t=1800..3585s
...
```

切分条件：

- 样本数达到 120；
- 时间跨度超过 2h；
- 序列被删除/重写（tombstone 不切 chunk）。

查询时：

- 根据 chunk 的 min/max time 跳过不相关 chunk；
- 只解码命中的 chunk；
- 跨 chunk 的 `rate()` 需要合并样本，函数层处理。

## 14.5 WAL 的作用

WAL（Write-Ahead Log）保证进程崩溃后能恢复最近数据：

- 每次 append 先写 WAL，再更新内存；
- WAL 是顺序追加的分段文件；
- 段大小默认 128MB（Mini 8MB，可配）；
- 恢复时重放 WAL，重建 head；
- checkpoint 记录"某个段之前的数据已落盘"，避免无限重放；
- 截断（truncation）删除旧段。

Prometheus 的 WAL 记录类型：

```text
Series
Samples
Tombstones
Exemplars
Metadata
```

Mini 使用：

```text
SERIES: [[ref, [[name,value],...]], ...]
SAMPLES: [[ref, ts, value], ...]
TOMBSTONES: [[labels, mint, maxt], ...]
```

每条记录有长度 + CRC + 类型 + JSON payload。JSON 便于教学，但不是生产格式。

## 14.6 WAL 恢复流程

```text
打开 TSDB
  → 读取 checkpoint（如果有）
  → 从 checkpoint 的 last_segment+1 开始重放 WAL
  → 重建 series/ref 映射
  → 重放 samples（按顺序 append）
  → 重放 tombstones
  → 加载 blocks
  → 完成，/-/ready 返回 200
```

Mini 的 `TSDB.open()`：

1. 获取锁文件；
2. 打开 WAL；
3. 读 checkpoint，重建 head；
4. `wal.replay(min_segment=last+1)`；
5. 加载 blocks。

如果 WAL 尾部有半条记录（崩溃），Mini 忽略尾部并记录 `truncated_bytes`；如果中间 CRC 错，抛 `WALCorruptionError`，拒绝启动。这是"宁可失败也不静默丢数据"的选择。

## 14.7 Checkpoint 与截断

Head 被切块后，旧 WAL 段不再需要：

```text
head 数据 → block 落盘
  → 写 checkpoint（剩余 head）
  → roll WAL 到新段
  → 删除旧段
```

Mini 的 checkpoint 是单个 JSON 文件：

```json
{
  "version": 1,
  "last_segment": 3,
  "series": [{"labels": [["__name__", "x"]], "samples": [[1000, 1.0]]}],
  "tombstones": []
}
```

真实 Prometheus 的 checkpoint 是一组 WAL 段文件，格式相同，恢复更快。

## 14.8 Out-of-order 与重复

Head 默认拒绝：

- `t < last_t`：out-of-order；
- `t == last_t` 且值不同：duplicate；
- `t == last_t` 且值相同：忽略。

为什么严格：

- chunk 编码要求时间递增；
- 乱序会破坏 `rate()`/查询假设；
- 允许乱序需要额外的内存结构和合并逻辑（Prometheus 有 out-of-order head 支持，但默认关闭/有限窗口）。

抓取场景中，乱序通常来自：

- exporter 自带时间戳且不单调；
- 多副本同时写同一序列；
- remote write 重试导致重复；
- 系统时钟回拨。

解决：关闭 `honor_timestamps`、修复 exporter、或用支持乱序的存储（Thanos/Mimir）。

## 14.9 Head 的内存控制

Prometheus 的 head 没有硬性 "max series" 限制，但受内存约束。相关参数/指标：

```text
--storage.tsdb.head-chunks-write-queue-size
prometheus_tsdb_head_series
prometheus_tsdb_head_chunks
prometheus_tsdb_head_samples_appended_total
process_resident_memory_bytes
```

当内存接近上限：

- 增加内存；
- 降低 retention（对 head 影响有限）；
- 减少高基数指标；
- 缩短抓取间隔不一定省内存（序列数不变）；
- 用 remote write + agent 把 head 压小。

## 14.10 Mini 的 Head 实现

`tsdb/head.py`：

- `HeadSeries`：ref、labels、chunks、min/max、last_ts/last_value、num_samples；
- `Head.append()`：检查顺序/重复、切 chunk、更新统计；
- `Head.select()`：按 matcher/time range 过滤，解码 chunk，应用 tombstone；
- `chunks_before(cut)` / `remove_chunks_before(cut)`：整 chunk 切块；
- `data()`：WAL checkpoint/快照导出。

`tsdb/chunkenc.py`：

- `BitWriter`/`BitReader`；
- `XORChunk.append()` 写 delta-of-delta + XOR；
- `XORChunk.from_bytes()` 解码；
- 读开放 chunk 时使用临时快照，避免"查询把 chunk 封死导致后续 append 失败"。

这个细节是 Mini 开发中真实踩到的 bug：查询会 `seal()` 开放 chunk，下一次抓取 append 直接报错。测试 `tests/test_tsdb.py` 覆盖了"读后写"。

## 14.11 真实 WAL 的记录格式

Mini 的 WAL 是"长度 + CRC + 类型 + JSON"，便于观察。真实 Prometheus 的 WAL 更接近数据库的页式日志：

```text
segment: 128 MiB（默认）
  page: 32 KiB
    record header: type | length | CRC32
    payload: snappy 压缩的 protobuf-like 二进制
```

关键差异：

- **页对齐**：记录不能跨页；剩余空间用 padding 填满，读取时按页扫描。
- **压缩**：payload 先 snappy 压缩再写，降低磁盘带宽；Mini 用 JSON，体积更大。
- **CRC**：每条记录独立校验；页头也有校验。损坏定位比 Mini 更细。
- **记录类型**：series、samples、tombstones、exemplars、metadata、histogram samples、float histogram samples 等。新类型通过"未知记录可跳过"保持向前兼容。
- **checkpoint**：不是单个 JSON，而是一组按 WAL 格式写出的段文件，记录"截至某段的状态"，恢复时先读 checkpoint 再重放后续段。

WAL 的写入顺序是"先日志、后内存"还是"先内存、后日志"？Prometheus 的 appender 在提交时把样本写入 WAL，再让 head 可见；具体实现有批量和锁的优化，但语义是**已确认的写入必须可恢复**。Mini 的顺序是 `head.append` 后 `wal.log_samples`，崩溃窗口内可能丢最后一批；教学实现接受这个差异，生产实现不能。

## 14.12 Head 的生命周期与 GC

Head 不是"只增不减"的。它有三个回收动作：

1. **chunk 切块（truncate）**：整 chunk 落盘成 block 后，从 head 删除；
2. **series GC**：没有 chunk、超过保留期的序列从内存索引移除；
3. **checkpoint/truncation**：WAL 旧段删除，checkpoint 保留剩余 head。

真实 Prometheus 的 head 还有：

- `head_chunks`：mmap 的 chunk 文件，减少堆内存；
- `postings`：倒排索引，随 series 增删维护；
- `head GC`：周期性回收，避免"删除的序列仍占内存"；
- `--storage.tsdb.head-chunks-write-queue-size`：控制 chunk 落盘队列，过小会阻塞写入，过大增加内存。

Mini 的 `remove_chunks_before()` 在删除空序列时同时清理 `_ref_by_labels` 和 tombstone；真实实现还要处理 postings、label index、exemplar 和 native histogram 的引用。任何"只删一半"的 GC 都会留下幽灵序列。

## 14.13 内存核算：一条序列到底占多少

粗算一条活跃序列：

```text
labels:      指标名 + 标签名/值（字符串驻留后按引用计）
series 结构:  ref、时间范围、chunk 指针、last sample
postings:    每个 label pair 的倒排项（近似 8～16 字节/项）
chunk:       120 个样本，压缩后约 100～300 字节（取决于波动）
```

经验值：

- 压缩后的样本约 **1～2 字节/样本**（Gorilla 类编码，稳定指标更省）；
- 一条序列的固定开销通常在**几百字节到 1 KB 量级**，标签越多/越长越高；
- 查询时的临时对象可能远大于存储本身。

算例：100 万活跃序列、每 15s 一个样本、2h head：

```text
每序列样本数 = 2h / 15s = 480
样本总量     = 100 万 × 480 = 4.8 亿
压缩后数据   = 4.8 亿 × 1.5B ≈ 720 MB
序列开销     = 100 万 × 0.5KB ≈ 500 MB
postings/索引 ≈ 数百 MB
查询/GC 余量 ≈ 1～2 GB
```

所以"100 万序列需要多少内存"没有单一答案，但**序列数 × 固定开销**通常比样本压缩数据更先成为瓶颈。第 22 章给出更完整的容量模型。

## 14.14 乱序样本与 out-of-order head

默认 head 拒绝乱序。真实 Prometheus 提供有限的乱序能力：

```text
--storage.tsdb.out-of-order-window=30m
```

语义：

- 只接受"时间戳落在窗口内"的乱序样本；
- 乱序样本进入独立的 OOO head/chunk，不与有序 chunk 混写；
- 查询时合并有序与乱序数据；
- 窗口越大，内存和 compaction 成本越高；
- 超过窗口的样本被丢弃，计入 `prometheus_tsdb_out_of_order_samples_total`。

常见来源：

- 多副本同时写同一序列（HA 对不应写同一本地 TSDB）；
- remote write 重试/乱序到达；
- exporter 自带时间戳且时钟漂移；
- 回填历史数据。

处理顺序：先修时间戳源（`honor_timestamps: false`、NTP、单写者），再考虑开 OOO 窗口。OOO 不是"无限接受乱序"的开关。

## 14.15 Exemplar 与原生直方图在存储层的表示

### Exemplar

Exemplar 是"样本 + 少量标签（trace_id/span_id）+ 时间戳"。存储特点：

- 不进入普通 chunk，而是独立的环形缓冲/文件；
- 容量由 `--storage.tsdb.max-exemplars` 控制（0 表示关闭）；
- WAL 有独立记录类型，重启可恢复；
- block 中也有 exemplar 段，随 block 生命周期保留；
- 查询按序列 + 时间范围返回少量 exemplar，不保证全量。

### 原生直方图

原生直方图不是"很多 bucket 的经典直方图"，而是**稀疏桶 + schema** 的编码：

- 一个 chunk 可以混合存储整数/浮点直方图样本；
- 桶用指数 schema 表示，动态增减；
- 查询时按 schema 重建桶边界；
- 存储密度远高于经典直方图的 `_bucket` 序列；
- 需要 scrape/remote write/查询/规则全链路支持。

Mini 只实现经典直方图的 `_bucket/_sum/_count` 语义，不实现原生直方图 chunk；第 20 章与附录 R 讲真实实现。

## 14.16 恢复演练与故障注入

WAL 的价值只有在"真的崩溃"时才体现。建议的演练：

```bash
# 1. 启动 Mini，写入数据
python -m mini_prometheus --config.file=examples/config/prometheus.yml \
  --storage.tsdb.path=./data --web.listen-address=127.0.0.1:9090

# 2. 模拟 kill -9（Windows 用任务管理器/Stop-Process）
# 3. 重启，确认 head 数据恢复
# 4. 检查 WAL 尾部/损坏行为
```

真实 Prometheus 的检查点：

- `promtool tsdb list` 看 block；
- `promtool tsdb analyze` 看索引；
- 日志中的 `WAL replay`、`truncated`、`corruption`；
- `prometheus_tsdb_wal_*` 指标；
- 对比重启前后的 `prometheus_tsdb_head_series`。

故障注入要覆盖：

- 写到一半杀进程（torn tail）；
- WAL 中间字节翻转（CRC 错）；
- 磁盘满（写入失败、compaction 失败）；
- 只读文件系统；
- 时钟回拨（乱序）；
- checkpoint 损坏。

Mini 的选择是"尾部可截断、中间损坏拒绝启动"；真实 Prometheus 更复杂，但原则相同：**宁可拒绝启动，也不要静默丢数据**。

## 本章小结

- Head 是内存写缓冲，chunk 用 Gorilla XOR 压缩，120 样本/2h 切分。
- WAL 保证崩溃恢复，checkpoint + truncation 控制重放量。
- 重复/乱序默认拒绝；抓取时间戳配置是常见根因。
- Head 内存与活跃序列强相关，高基数是主要风险。
- Mini 的 chunk/WAL 是教学实现，不是字节兼容格式。

## 延伸阅读

- Gorilla 论文：*Gorilla: A Fast, Scalable, In-Memory Time Series Database*。
- Prometheus 源码 `tsdb/head.go`、`tsdb/chunkenc/`、`tsdb/wlog/`。
- 本教程第 15 章（block）与第 33、34 章（Mini 实现）。

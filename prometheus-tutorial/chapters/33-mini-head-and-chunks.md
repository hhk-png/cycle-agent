# 第 33 章 Mini 实现（二）：XOR Chunk 与 Head

## 33.1 位流读写

`tsdb/chunkenc.py` 先实现两个小工具：

```python
class BitWriter:
    def write_bit(self, bit): ...
    def write_bits(self, value, count): ...
    def bytes(self): ...   # 末尾补零到字节边界

class BitReader:
    def read_bit(self): ...
    def read_bits(self, count): ...
```

位序是 MSB-first（与 Gorilla 一致）。`bytes()` 只在序列化时补零，`_bits` 计数不变；因此可以继续 append。

## 33.2 时间戳编码

第一个时间戳用 64 bit 原始值；后续用 delta-of-delta：

```text
ddd = (t - prev_t) - prev_delta
if ddd == 0:        1 bit 0
elif -8192..8191:   1 + 2 bits 00 + 14 bits
elif -65536..65535: 1 + 2 bits 01 + 17 bits
elif ±524288:       1 + 2 bits 10 + 20 bits
else:               1 + 2 bits 11 + 64 bits
```

抓取间隔稳定时，绝大多数 ddd=0，只占 1 bit。测试 `test_large_timestamp_jumps` 覆盖了 20/64 bit 分支。

## 33.3 值编码

第一个值 64 bit IEEE；后续 XOR：

```text
xor = bits(v) ^ bits(prev)
if xor == 0: 1 bit 0
else:
  1 bit 1
  if 可复用窗口: 0 + meaningful bits
  else: 1 + leading(6) + meaningful-1(6) + bits
```

Mini 与真实 Gorilla 的差异：

- 真实实现用 5+6 bit 的窗口控制（含 0b10/0b11 两种新窗口）；
- Mini 简化为 1 bit "复用/新窗口"，leading 用 6 bit；
- 因此**不是字节兼容**，但压缩行为与 roundtrip 性质一致。

`test_constant_values_compress` 验证常量序列压缩到很小；`test_random_roundtrip` 用随机数据验证正确性。

## 33.4 Chunk 的生命周期

```text
XORChunk()
  append(t, v) ...   # 开放状态
  bytes()/seal()     # 封存，返回 4-byte count + bitstream
  from_bytes(data)   # 解码
```

重要细节：**读取开放 chunk 不能封存它**。

最初的实现：

```python
def samples(self):
    if self._num and not self._sealed:
        self.seal()       # ← bug
```

后果：Prometheus 在两次抓取之间查询（Grafana/规则），会把 chunk 封死；下一次抓取 append 时报 `cannot append to a sealed chunk`。

修复：

```python
if not self._num:
    return []
snapshot = struct.pack(">I", self._num) + self._writer.bytes()
return XORChunk.from_bytes(snapshot).samples()
```

即"解码临时副本，不改变开放状态"。测试 `test_read_then_append`（在 `test_tsdb.py` 的读后写场景）覆盖。

这个 bug 是"测试全绿但生产会炸"的典型：单测只测了 append + 一次性查询，没测"查询后继续 append"。

## 33.5 Head 数据结构

`tsdb/head.py`：

```python
@dataclass
class HeadSeries:
    ref: int
    labels: Labels
    chunks: list[XORChunk]
    min_time: int
    max_time: int
    last_ts: int
    last_value: float
    num_samples: int
```

`Head`：

```python
_series_by_ref: dict[int, HeadSeries]
_ref_by_labels: dict[Labels, int]
_tombstones: dict[int, list[Interval]]
_min_time/_max_time/_num_samples
```

### 追加

```python
def append(self, labels, timestamp, value):
    ref = self.get_or_create(labels)
    series = self._series_by_ref[ref]
    series.append(timestamp, value)
    ...
```

`HeadSeries.append()`：

- `timestamp < last_ts` → `OutOfOrderError`；
- `timestamp == last_ts` 且值不同 → `DuplicateSampleError`；
- 值相同 → 忽略；
- 当前 chunk 满 120 或 span > 2h → 新 chunk；
- 更新 min/max/num_samples。

### 查询

```python
def select(self, matchers, mint, maxt, apply_tombstones=True):
    for series in ...:
        if series.max_time < mint or series.min_time > maxt: continue
        if not match_all(matchers, series.labels): continue
        samples = series.samples(mint, maxt)
        samples = [s for s in samples if not deleted]
        ...
```

复杂度 O(series × chunks)，没有 postings。

### 切块

```python
def chunks_before(self, cut_time):
    # 只返回 max_time <= cut_time 的整 chunk
def remove_chunks_before(self, cut_time):
    # 删除已落盘 chunk；空序列删除 ref
```

为什么只切"整 chunk"：

- chunk 不可拆分；
- 如果 chunk 跨越 cut，保留在 head，下一次切块再处理；
- 这会导致 head 比 block_range 略大，但实现简单且不会产生重叠。

真实 Prometheus 会按样本切 head，Mini 的近似在演示中可接受。

## 33.6 统计

`Head` 维护：

- `min_time`/`max_time`；
- `num_samples`；
- `num_series`；
- `head_stats()` 返回 numSeries/numSamples/numChunks/numTombstones。

切块后 `TSDB._recalculate_head()` 重新计算，避免统计漂移。

## 33.7 并发

- `Head` 用 `RLock` 保护；
- 查询与追加可并发（读/写锁粒度是 head 级，简单但粗）；
- 真实 Prometheus 用更细的 shard/lock，Mini 不追求。

## 33.8 测试

`tests/test_chunk.py`：

- 单样本/空 chunk；
- 随机 roundtrip；
- 常量压缩；
- NaN/Inf/-0.0；
- 时间戳递增；
- 大跳变；
- seal 后 append 报错；
- 解码后元数据。

`tests/test_tsdb.py`：

- append/select；
- 重复/乱序；
- head 多 chunk；
- 读后写（回归）；
- stale marker 存储。

## 本章小结

- Gorilla XOR：时间 delta-of-delta + 值 XOR + 窗口复用。
- Mini 的位布局简化但保留压缩/roundtrip 性质，不字节兼容。
- 读开放 chunk 必须用临时快照，不能 seal；这是实测 bug。
- Head 用整 chunk 切块，避免 block/head 重叠。
- Head 复杂度是 O(series)，生产需要 postings/更细锁。

## 延伸阅读

- 本教程第 14 章（TSDB 原理）与第 34 章（WAL）。
- `mini_prometheus/tsdb/chunkenc.py`、`head.py`。
- Gorilla 论文。

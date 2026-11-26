# 第 34 章 Mini 实现（三）：WAL 与崩溃恢复

## 34.1 为什么不用 SQLite

WAL 的目标是：

- 顺序追加，低延迟；
- 崩溃后可重放；
- 可截断/checkpoint；
- 格式可调试。

Mini 选择"自定义帧 + JSON payload"，因为：

- 不引入依赖；
- 可以直接 `xxd`/`cat` 观察；
- 足以演示 CRC、段、截断、恢复。

代价是体积/性能远不如生产格式。

## 34.2 帧格式

```text
segment header: b"MPWAL001" (8 bytes)
record:
  u32 payload_length   (包含 type byte)
  u32 crc32            (对 type+payload)
  u8  record_type
  payload (JSON, UTF-8)
```

记录类型：

```text
1 SERIES
2 SAMPLES
3 TOMBSTONES
4 METADATA
5 EXEMPLARS
```

payload 示例：

```json
[[1, [["__name__", "http_requests_total"], ["job", "api"]]]]
[[1, 1700000000000, 1.0], [1, 1700000015000, 2.0]]
```

## 34.3 分段与滚动

- 每个段文件名 `%08d`；
- 写入前检查 `size + frame > segment_size`，超过则滚动到下一段；
- 段大小默认 8MB（Mini），真实 128MB；
- 滚动时关闭旧文件、创建新文件、写 header。

```python
def _write_record(self, record_type, payload):
    body = json.dumps(payload, separators=(",", ":"), allow_nan=True).encode()
    length = len(body) + 1
    crc = zlib.crc32(bytes([record_type]) + body)
    frame = FRAME_HEADER.pack(length, crc, record_type) + body
    if self._size + len(frame) > self.segment_size and self._size > HEADER_SIZE:
        self._open_new_segment(self._segment_index + 1)
    self._file.write(frame)
```

`allow_nan=True` 是必要的：样本值可能是 NaN/Inf，标准 JSON 不支持。Python 的 `json` 会写成 `NaN`/`Infinity`，`json.loads` 能读回。

## 34.4 写入顺序

`TSDB.append()`：

```python
is_new = head.ref_for_labels(labels) is None
ref = head.append(labels, timestamp, value)
if is_new:
    wal.log_series([(ref, labels)])
wal.log_samples([(ref, timestamp, value)])
```

`append_batch()`：

1. 逐条 append 到 head，收集成功/失败；
2. 新序列的 SERIES 记录先写；
3. 成功的 SAMPLES 记录后写；
4. notify watchers（remote write）。

如果 WAL 写失败（磁盘满），异常会向上传播；head 已经追加，但重启后无法恢复该样本。这是"WAL 优先"与"内存优先"的权衡。生产 Prometheus 在 WAL 失败时会标记不可用。

## 34.5 恢复

```python
def replay(self, min_segment=0):
    for segment in segments >= min_segment:
        read header
        while frame:
            verify CRC
            json.loads
            apply
```

`WALReplay` 累积：

```python
series: dict[int, Labels]
samples: list[(Labels, ts, value)]
tombstones: list[(Labels, mint, maxt)]
metadata/exemplars
truncated_bytes
```

`TSDB.open()`：

1. 读 checkpoint；
2. `last_segment+1` 开始 replay；
3. 先 `get_or_create` 所有 series（重建 ref）；
4. 再 append samples（`head.append` 按 labels 查找 ref）；
5. 应用 tombstones。

注意：replay 的 samples 用 labels 而不是 WAL ref 写入 head。因为 head 的 ref 分配可能与 WAL 不同（checkpoint 重建后）。WAL ref 只用于记录内的关联。

## 34.6 尾部截断与 CRC 错误

崩溃可能留下半条记录：

```text
[header][len][crc][type][partial payload]
```

处理：

- `frame_end > len(data)` → 认为尾部截断，`truncated_bytes +=`，停止该段；
- CRC 不匹配且 `frame_end == len(data)` → 尾部截断；
- CRC 不匹配且不在尾部 → `WALCorruptionError`，拒绝启动；
- JSON 解析失败同理。

为什么不在中间跳过：静默跳过会丢数据且无法告知；宁可在启动时失败，让运维从快照恢复。

测试：

- `test_torn_tail_is_ignored`；
- `test_crc_corruption_in_middle_raises`；
- `test_unknown_series_ref_is_corruption`。

## 34.7 Checkpoint

Head 切块后，旧 WAL 段包含已落盘数据，可以丢弃。但丢弃前必须记录"当前 head 的剩余数据"：

```python
def _checkpoint_and_truncate_wal(self):
    series = checkpoint_series(self.head.data())
    tombstones = ...
    previous = self.wal.roll()
    write_checkpoint(path, series=series, last_segment=previous, tombstones=tombstones)
    self.wal.truncate_through(previous)
```

顺序很重要：

1. 写 checkpoint 到临时文件；
2. `os.replace` 原子替换；
3. 删除旧段。

如果先删段再写 checkpoint，崩溃会丢数据。Mini 用 `os.replace` 保证原子性。

`write_checkpoint` 的 JSON：

```json
{
  "version": 1,
  "last_segment": 3,
  "series": [{"labels": [["__name__", "x"]], "samples": [[1000, 1.0]]}],
  "tombstones": []
}
```

`read_checkpoint` 在启动时加载；`last_segment+1` 之后的段才 replay。

## 34.8 截断的边界

`truncate_through(index)`：

- 删除 `<= index` 的段；
- 保留当前活动段（即使它的编号 <= index）；
- 如果当前段就是被 checkpoint 覆盖的段，后续 append 会写进它，但 checkpoint 已经包含其内容，重启会重复。

Mini 的 `_checkpoint_and_truncate_wal()` 先 `roll()`，所以活动段是新段，不会重复。

## 34.9 段大小与测试

测试用小的 `segment_size`（如 300 bytes）强制多段：

```python
wal = WAL(dir, segment_size=300)
wal.open()
wal.log_series([(1, labels("x"))])
for i in range(100):
    wal.log_samples([(1, i, float(i))])
wal.close()
replay = WAL(dir).replay()
assert len(replay.samples) == 100
```

真实段大小 128MB，避免过多文件。

## 34.10 Mini 与真实 WAL 的差异

| 方面 | Mini | Prometheus |
|---|---|---|
| 记录格式 | JSON + CRC | protobuf-like 二进制 + page CRC |
| 段大小 | 8MB | 128MB |
| Checkpoint | 单个 JSON | 多个 WAL 段 |
| 索引 | 无 | series/ref 索引 |
| 并发 | 单锁 | 多 shard/压缩 |
| 修复工具 | 无 | `promtool tsdb` |

Mini 的恢复语义与真实一致：顺序重放、尾部容错、中间损坏拒绝。

## 本章小结

- WAL 是顺序追加 + CRC + 分段的日志；JSON 便于教学。
- 写入顺序：series 先于 samples；head 与 WAL 的失败语义要明确。
- 恢复从 checkpoint 之后开始，按记录类型重放。
- 尾部截断容忍，中间损坏拒绝。
- Checkpoint → roll → truncate 的顺序保证不丢数据。

## 延伸阅读

- 本教程第 14 章（WAL 原理）与第 35 章（block）。
- `mini_prometheus/tsdb/wal.py`、`tsdb.py`。
- 真实 Prometheus `tsdb/wlog/`。

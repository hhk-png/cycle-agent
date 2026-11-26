# 第30章 MiniFlink：时间、窗口与状态

## 30.1 时间戳分配与 watermark

MiniFlink 的时间来自 `WatermarkStrategy`：

```python
strategy = WatermarkStrategy.for_bounded_out_of_orderness(
    Duration.of_seconds(5),
    timestamp_assigner=lambda event: event["ts"],
)
```

`CollectionSource` 每读一条记录：

1. 调用 `TimestampAssigner` 提取时间戳；
2. 调用 `WatermarkGenerator.on_record(ts)`；
3. 如果生成新 watermark，把它追加到 `_pending`，下一条 `next_element` 先返回 watermark。

```python
def next_element(self):
    if self._pending:
        return self._pending.popleft()
    if self.offset < len(self.data):
        raw = self.data[self.offset]
        self.offset += 1
        ts = self.assigner.extract_timestamp(raw, None)
        record = Record(raw, ts)
        wm = self.generator.on_record(ts)
        if wm is not None:
            self._pending.append(Watermark(wm))
        return record
```

`WatermarkGenerator` 的核心：

```python
def on_record(self, timestamp):
    self.max_timestamp = max(self.max_timestamp, timestamp)
    watermark = self.max_timestamp - self.out_of_orderness_ms
    if watermark > self.last_watermark:
        self.last_watermark = watermark
        return watermark
```

这是“有界乱序”的实现：watermark = 已见最大时间戳 - 容忍度。

### 与 Flink 的差异

- 没有真正的周期发射（按记录数触发，`periodic_interval_records`）；
- 没有 per-partition watermark：KafkaSource 的 generator 是全局的，多分区时会用最大时间戳推进，可能比最慢分区更激进；
- 没有 idleness 实现（`with_idleness` 只保存配置）；
- 没有 watermark alignment。

这些差异在附录 C 中列出。理解“watermark 是单调、取最小”即可，生产语义仍有差距。

## 30.2 WindowOperator 的状态布局

```python
self._windows: dict[Any, dict[tuple[int, int], _WindowBucket]] = {}
```

三层结构：

```text
key → (window_start, window_end) → _WindowBucket
```

`_WindowBucket` 保存：

```python
@dataclass
class _WindowBucket:
    records: list[Record] = field(default_factory=list)  # 全量窗口；保留时间戳
    accumulator: Any = None                              # 增量聚合
    count: int = 0                                       # CountTrigger
    fired: bool = False                                  # 是否已触发
    window_state: dict[str, Any] = field(default_factory=dict)
```

`records` 存的是 `Record` 而不是裸 value：`TimeEvictor` 需要事件时间戳，`ProcessWindowFunction` 只需要 value。触发时再统一取出 `record.value` 交给用户函数。

这对应 Flink 的 window state namespace：key + window 两级。区别是 MiniFlink 用 Python dict，Flink 用 RocksDB 列族/key group。

## 30.3 触发与清理

`process_record` 的主流程：

```python
windows = self.assigner.assign_windows(record.value, timestamp, self._context)
late_windows = [w for w in windows if self._is_late_record(key, w)]

# 只有“所属窗口全部过期”才走迟到侧输出；仍属于某个未过期窗口时正常参与
if windows and len(late_windows) == len(windows):
    if self.late_output_tag is not None:
        self._context.emit_side_output(self.late_output_tag, record.value)
    return self._advance_processing_time(timestamp)

for window in windows:
    if window in late_windows:
        continue
    bucket = self._get_or_create(key, window)
    if self.assigner.is_session:
        # 合并会话返回 (合并后的 bucket, 合并后的 window)
        bucket, window = self._merge_sessions(key, window)
    self._add(bucket, record)
    result = self.trigger.on_element(record.value, timestamp, window, bucket.count)
    output.extend(self._handle_trigger(key, window, bucket, result))
    if window.end <= self._watermark < window.end + self.allowed_lateness_ms:
        # 是否重发由 trigger 决定，不能无条件补发
        output.extend(self._handle_trigger(
            key, window, bucket, self.trigger.on_event_time(self._watermark, window)))
```

关键语义：

- **迟到判断**：`window.end + allowed_lateness <= watermark` 才算“超过允许范围”；
- **已触发但允许迟到内**：若 trigger 返回 FIRE（默认 `EventTimeTrigger` 会），新记录会更新结果并再次发射；`CountTrigger` 未达阈值时不会；
- **trigger**：`on_element` 可以返回 FIRE/PURGE；
- **count**：用于 CountTrigger（达到阈值后清零，按 `count % N == 0` 的节奏触发）。

`process_watermark`：

```python
for window in windows:
    if watermark >= window.end + allowed_lateness:
        if not fired:
            # 给 trigger 一次机会；CountTrigger 等可能返回 CONTINUE，
            # 此时只清理、不强行发射。
            handle(trigger.on_event_time(watermark, window))
        remove(window)
    elif watermark >= window.end and not fired:
        handle(trigger.on_event_time(watermark, window))
```

```text
window [10:00,10:05), allowedLateness=1m
wm=10:05 → 首次触发
wm 10:05~10:06 → 迟到记录更新
wm>=10:06 → 清理
```

`end_of_input` 也会先给 trigger 一次 `on_event_time`/`on_processing_time` 机会，再清理未触发窗口；如果 trigger 返回 CONTINUE（例如 CountTrigger 没达到阈值），窗口不会被强行发射。

窗口触发的 `ProcessWindowFunction` 侧输出会带上窗口 key 和 `window.end - 1` 时间戳，并在 watermark 之前下发；否则下游窗口会把它们误判为迟到数据。

## 30.4 会话窗口合并

会话窗口的 `assign_windows` 返回初始窗口 `[ts, ts+gap)`：

```python
def assign_windows(self, value, timestamp, context=None):
    return [Window(timestamp, timestamp + self.gap)]
```

`_merge_sessions` 会扫描同 key 下所有重叠窗口，合并成一个：

```python
for boundaries in list(buckets):
    other = Window(*boundaries)
    if other.overlap(merged):
        merged = merged.merge(other)
        merge_keys.append(boundaries)
```

合并时：

- 记录列表拼接；
- count 累加；
- `fired` 先取或，但若合并后的结束时间仍在 watermark 之后，则重置为未触发，等待新结束时间再发一次；
- accumulator 用 `AggregateFunction.merge` 合并；
- `window_state` 不合并（MiniFlink 限制）；需要跨会话合并保留的自定义窗口状态要自己设计。

```text
key=user1
  [1,6)  count=2
  [4,9)  count=1
  → 合并 [1,9) count=3
```

这就是会话窗口要求 `merge` 结合律的原因。如果 `merge` 不正确，会话结果会随事件顺序变化。

## 30.5 触发器与驱逐器

Trigger 接口：

```python
class Trigger:
    def on_element(self, value, timestamp, window, count): ...
    def on_event_time(self, watermark, window): ...
    def on_processing_time(self, now, window): ...
```

内置：

- `EventTimeTrigger`：watermark >= window.end 时 FIRE；
- `ProcessingTimeTrigger`：now >= window.end 时 FIRE；
- `CountTrigger`：count 达到阈值时 FIRE，并清零计数（因此按 N 的倍数触发）；
- `PurgingTrigger`：包装其他 trigger，把 FIRE 变成 FIRE_AND_PURGE。

Evictor 只在 `_fire` 时对全量记录生效：

```python
records = list(bucket.records)          # list[Record]
if self.evictor and not evict_after:
    records = self.evictor.evict_before_fire(records, window)
values = [record.value for record in records]
# process/apply 只看到 values；输出后再按需执行 evict_after
```

因此：

- 增量聚合 + Evictor 不能一起用（元素已被折叠）；
- `evict_before_fire` 只影响本次输出；`evict_after_fire` 会裁剪 `bucket.records`，影响下一次触发看到的状态；
- MiniFlink 同时实现了 before 和 after；
- `TimeEvictor` 依赖 `Record.timestamp`；如果只存裸 value，它读不到时间戳，等于不驱逐——这是审计阶段修复的一个真实缺陷。

## 30.6 键控状态

`KeyedStateBackend` 的数据布局：

```python
self._data: dict[Any, dict[str, tuple[Any, int]]] = {}
# key -> state_name -> (value, last_write_ms)
```

`read_current`：

```python
entry = self._data.get(key, {}).get(name)
if entry is None:
    return default_factory()
value, last_write = entry
if ttl and expired(last_write):
    if visibility is NEVER_RETURN_EXPIRED:
        pop
        return default
if ttl and update_type is ON_READ:
    self._data[key][name] = (value, now)
return value
```

`write_current` 更新值和写入时间。

支持的 State：

- `ValueState`；
- `ListState`（get 返回 deepcopy，避免外部修改内部）；
- `MapState`（get/items/keys/values 返回 deepcopy，原地修改必须显式 put）；
- `ReducingState`；
- `AggregatingState`；
- `BroadcastState`（与 MapState 共享布局）。

### TTL 语义

```python
from miniflink.state import StateTtlConfig, TtlUpdateType, TtlStateVisibility

StateTtlConfig(
    ttl_ms,
    update_type=TtlUpdateType.ON_CREATE_AND_WRITE,  # 或 ON_READ / DISABLED
    visibility=TtlStateVisibility.NEVER_RETURN_EXPIRED,
)
```

`StateTtlConfig` 是 frozen dataclass，没有 `set_update_type()` / `set_visibility()` 这类链式 setter；要在构造时传参。`StateTtlConfig.new_config(ttl_ms)` 只是用默认值创建。

注意：

- TTL 是惰性清理：读时判断，过期才删；
- `ON_READ` 会在读时续期；
- 没有后台 compaction，物理内存不一定立即下降；
- 快照会包含过期但未清理的数据。

### 状态隔离

每个 `RuntimeTask` 有自己的 `KeyedStateBackend`，`RuntimeContext.set_current_key` 在每条记录前切换 key：

```python
task.context.set_current_key(record.key)
```

这保证同名状态在不同 key/subtask 之间不串。

## 30.7 定时器

`TimerService` 用两个字典：

```python
self._event_timers: dict[int, set[Any]] = {}
self._processing_timers: dict[int, set[Any]] = {}
```

- key 是时间戳；
- value 是待触发的 key 集合；
- 同一 key 同一时间戳重复注册只触发一次。

`pop_event_timers(watermark)` 弹出所有 `timestamp <= watermark` 的计时器，按时间戳排序；`pop_processing_timers(now)` 类似。

`ProcessOperator` 在 watermark 到来时调用：

```python
for timestamp, key in timer_service.pop_event_timers(watermark):
    output.extend(self._call_timer(timestamp, key))
```

`_call_timer` 恢复 key/timestamp，再调用用户 `on_timer`。

注意：MiniFlink 当前只在 `KeyedProcessFunction` 上弹出并回调定时器。非键控的 `ProcessFunction` 虽然也能拿到 `timer_service` 注册 timer，但运行时不会服务它；需要定时器的非键控逻辑请用 `KeyedProcessFunction`，或在业务上自行调度（附录 C 已列为明确限制）。

### 与 Flink 的差异

- 没有 timer state 的 RocksDB 存储；
- 没有 timer 合并优化；
- 处理时间定时器在“记录到达或结束”时检查，不是独立线程定时触发；
- 恢复后处理时间定时器不会重新对齐系统时钟。

这足以演示“定时器随 checkpoint 恢复”的语义，但不能用于生产。

## 30.8 状态与 checkpoint

`WindowOperator.snapshot()` 保存：

```python
{
  "windows": [(key, [(start, end, bucket.snapshot()) ...]) ...],
  "watermark": self._watermark,
}
```

`KeyedStateBackend.snapshot()` 保存：

```python
{"kind": "keyed", "data": deepcopy(self._data)}
```

`TimerService.snapshot()` 保存事件/处理时间定时器和当前 watermark。

恢复时全部还原。这样窗口、状态、定时器都回到同一逻辑时刻。

## 30.9 限制与练习

限制：

- 没有 key group/rescale；
- 没有 window state 的 TTL；
- 没有增量 checkpoint；
- 没有异步快照；
- trigger 状态很简化：每个窗口只有一个 `fired` 标记，没有完整 trigger 状态/合并；
- 处理时间语义是简化的。

练习：

1. 给 `WindowOperator` 加 window state TTL，观察状态大小变化；
2. 实现 `ContinuousEventTimeTrigger`，让窗口提前触发；
3. 让 `TimeEvictor` 支持事件时间字段而不是对象 `timestamp`；
4. 给 `TimerService` 加“同一 key 只保留最早定时器”的合并策略；
5. 写测试验证“watermark 不推进时窗口不触发”。

## 本章小结

MiniFlink 的窗口内核是：**key + window 两级状态、trigger 决定触发、watermark 决定清理、session 用 merge 合并**。状态后端是堆内字典，定时器是时间戳到 key 的映射，二者都随 checkpoint 快照。

下一章进入 checkpoint、恢复与连接器：看微批快照如何保存 offset、窗口和定时器，以及内存 Kafka 如何演示 offset 回放。

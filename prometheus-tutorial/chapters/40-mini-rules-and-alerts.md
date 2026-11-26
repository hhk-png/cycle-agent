# 第 40 章 Mini 实现（九）：规则、告警状态机与通知

## 40.1 规则加载

`rules/manager.py::load_rule_groups()`：

- glob 匹配文件；
- `yaml.safe_load`；
- 校验 `groups` 列表；
- 校验 group name 唯一（同文件）；
- 校验每条规则有且只有一个 `record`/`alert`；
- `parse_expr` 解析表达式；
- 校验类型：
  - recording：vector/scalar；
  - alerting：vector；
- 校验 labels（不能 `__name__`）；
- 解析 `for`/`keep_firing_for`；
- 解析组级 `query_offset`；缺省时用全局 `rule_query_offset`；
- 返回 `RuleGroup`。

`RecordingRule`/`AlertingRule` 保存：

```python
record/alert, expr_text, expr, labels, annotations
for_ms, keep_firing_for_ms
health, last_error, last_evaluation, evaluation_time
active: dict[fingerprint, Alert]
```

## 40.2 记录规则求值

```python
result = engine.query(rule.expr, now)
if Vector: items = result.value.items
elif Scalar: items = [] (单独处理)
labels = item.labels.without("__name__").merge(rule.labels)
labels = labels.set("__name__", rule.record)
samples.append((labels, now, item.value))
tsdb.append_batch(samples)
```

- 输出时间戳是规则求值时间；
- 规则 labels 覆盖原标签；
- 结果为空也更新 health；
- group limit 超过则报错并跳过；
- append 错误写 health=err。

## 40.3 告警状态机

```python
for item in vector:
    labels = item.labels.without("__name__").merge(rule.labels)
    labels = labels.set("alertname", rule.alert)
    fingerprint = sha256(labels)[:16]
    existing = rule.active.get(fingerprint)
    if existing is None:
        state = firing if for_ms == 0 else pending
        active_at = now
        ...
    else:
        update value/annotations/last_seen
        if pending and now - active_at >= for_ms: firing
```

消失：

```python
for fingerprint, alert in active:
    if fingerprint in seen: continue
    if firing and keep_firing_for and now - last_seen < keep_firing_for:
        keep firing (keep_firing_since = now)
        continue
    resolve: state=inactive, resolved_at=now, payload endsAt=now
    remove from active
```

- `for=0` 立即 firing；
- `keep_firing_for` 在表达式变假后保持 firing；
- pending 消失直接移除，不发送 resolved；
- resolved 发送 endsAt=resolved_at；
- firing 发送 endsAt=now+5m（Mini 的 resolve timeout）。

## 40.4 告警标签与模板

```python
labels = item.labels.without("__name__").merge(Labels(rule.labels))
labels = labels.set("alertname", rule.alert)
```

- 序列标签 + 规则标签 + alertname；
- 规则标签覆盖序列标签；
- 任一标签变化 → 新 fingerprint → `for` 重新计时。

模板 `render_template()`：

- `$labels.foo`
- `$labels["foo"]`
- `$value`
- `$externalLabels.foo`
- `printf "%.2f" $value`
- 未知表达式保留原文

Mini 不支持完整 Go template（`humanizePercentage`/`range`/`if` 等）。

## 40.5 RuleManager

```python
class RuleManager:
    groups, engine, external_labels, notifier, clock
    start(): 调度线程，按 group.interval 求值
    evaluate(now): 手动求值所有组
    evaluate_group(group, now): 按规则类型分派
    alerts(): 汇总 active
    to_api(): /api/v1/rules 数据
```

- 同一组内串行；
- 组间由一个线程轮询（Mini 简化）；
- 错误写 health/last_error，不抛出；
- firing/resolved 交给 notifier。

## 40.6 Notifier

`rules/notifier.py`：

```python
AlertmanagerNotifier(configurations, sender=None, max_retries=5, start_worker=True)
```

- `enqueue(alerts)`：放入 queue；
- worker：`_deliver()`；
- `flush()`：同步发送（测试/示例）；
- 2xx 成功；
- 4xx（除 429）丢弃；
- 5xx/网络/429 重试（`threading.Timer` 退避）；
- payload：labels/annotations/startsAt/endsAt/generatorURL/fingerprint。

Mini 不做批量合并/并发发送；每条通知一个 POST。

## 40.7 Alertmanager-lite

`examples/alertmanager_lite.py`：

- `POST /api/v2/alerts`；
- 按 fingerprint 去重；
- 按 alertname 分组；
- `endsAt < now` 视为 resolved，移除；
- critical 抑制同 service 的 warning；
- `GET /api/v2/alerts` 返回 active。

不实现路由/silence/gossip/真实通知。

## 40.8 测试

`tests/test_rules.py`：

- 加载 recording/alerting；
- 缺 record/alert、两者都有、非 vector、重复组、空组、非法表达式；
- 记录规则写入/标签覆盖/scalar/limit；
- pending→firing；
- resolve；
- keep_firing_for；
- 多序列身份；
- firing/resolved payload；
- 模板；
- rules API；
- notifier 2xx/4xx；
- glob 多文件；
- `query_offset` 的查询时间/写入时间分离、全局默认、非法值拒绝。

## 40.9 与真实 Prometheus 的差异

- `promtool test rules` 为教学子集（独立模块 `promtool.py`，无 JUnit/全部子命令）；
- 无规则组并发控制；
- 模板是子集；
- notifier 无批量/并发/队列容量；
- 告警状态不持久化（重启丢失 pending/firing）；
- `query_offset` 只影响查询时间，不实现 Prometheus 对 stale marker 的回填细节。

## 40.10 query_offset 的实现

Prometheus 2.53 引入组级 `query_offset` 与全局 `rule_query_offset`：规则的**查询时间**后移，写入时间仍是求值时间。Mini 的实现分三处：

1. `GlobalConfig.rule_query_offset`：解析/校验全局默认值（允许 `0s`）；
2. `load_rule_groups(..., default_query_offset_ms=...)`：组里没写 `query_offset` 时继承全局值；
3. `_evaluate_recording`/`_evaluate_alerting`：`query_at = now - group.query_offset_ms`，用 `query_at` 查 TSDB，样本/告警状态仍按 `now` 推进。

```python
query_at = now - group.query_offset_ms
result = self.engine.query(rule.expr, query_at)
# recording: samples.append((labels, now, item.value))
```

验证示例：`x` 在 t=0 为 1、t=60s 为 2；组 `query_offset: 60s`，在 t=60s 求值：

```text
查询时间 = 0s  -> 读到 1
写入时间 = 60s -> 记录样本 (60s, 1)
```

这能解释两个常见现象：

- 记录规则的输出看起来"晚一个 offset"，但值来自更早的数据窗口；
- `smoothed`/`anchored` 规则必须配 `query_offset`，否则窗口末端没有未来样本可插值。

Mini 不实现 `smoothed`/`anchored` 的插值，因此这里的 `query_offset` 主要用于延迟数据/教学演示；查询时间与写入时间的分离语义与上游一致。

`tests/test_rules.py` 用"查询时间/写入时间分离"、"告警窗口"、"组级解析/API"与"非法值"四个用例锁住语义；`tests/test_config.py` 覆盖全局默认与非法值。

## 本章小结

- 规则加载严格校验 record/alert、表达式类型、标签/duration。
- 记录规则在求值时间写新序列，规则 labels 覆盖。
- 告警身份 = 序列标签 + 规则标签 + alertname；标签变化重置 for。
- keep_firing_for 抑制抖动；resolved 只在 firing 后发送。
- Mini 的状态机/通知足以演示生命周期，生产用完整 Alertmanager。

## 延伸阅读

- 本教程第 9、10 章（规则/Alertmanager）。
- `mini_prometheus/rules/`、`examples/alertmanager_lite.py`。
- 真实 Prometheus `rules/`、`notifier/`。

# 第 32 章 Mini 实现（一）：标签、匹配器与 exposition 解析

## 32.1 Labels 的设计

`mini_prometheus/labels.py`：

```python
class Labels:
    __slots__ = ("_items", "_dict", "_hash")

    def __init__(self, items):
        ...
        self._items = tuple(sorted(seen.items()))
        self._dict = seen
        self._hash = hash(self._items)
```

约束：

- 不可变；
- 标签名排序，保证哈希/比较稳定；
- 空值标签被丢弃（Prometheus 语义：空值等于不存在）；
- 重复标签名不同值报错；
- `__name__` 只是普通标签，但有 `get_name()` 辅助。

操作：

```python
labels.without("__name__")
labels.keep("job", "instance")
labels.merge(other)      # other 覆盖 self；空值删除
labels.set("env", "prod")
labels.map(fn)
```

`merge` 的"空值删除"很关键：relabel 的 `replace` 产生空值时，实际效果是删除标签。

## 32.2 Matcher

```python
@dataclass(frozen=True)
class Matcher:
    name: str
    op: str
    value: str
```

- `=`/`!=`：字符串比较；
- `=~`/`!~`：编译正则，`fullmatch`；
- 缺失标签按空串处理。

正则全匹配是 Prometheus 的语义：

```python
re.fullmatch("5..", "500")   # True
re.fullmatch("5..", "x500")  # False
```

如果误用 `re.search`，`{code=~"5.."}` 会匹配 `x500`，导致选择器范围扩大。Mini 的测试专门覆盖了这一点。

## 32.3 选择器的非空约束

PromQL 要求至少一个 matcher 不匹配空串：

```python
if not any(not matcher.matches("") for matcher in matchers):
    raise ParseError("vector selector must contain at least one non-empty matcher")
```

这防止 `{job=~".*"}` 扫描全部序列。`{job!="x"}` 也匹配空串（缺失 job），单独使用同样非法。

## 32.4 exposition 解析器结构

`exposition.py` 是一个逐行状态机：

```text
parse_exposition(text, default_timestamp, openmetrics, max_samples)
  for each line:
    blank → skip
    # HELP/TYPE/UNIT/EOF → metadata
    data line:
      metric name (classic or quoted)
      optional {labels}
      value
      optional timestamp
      optional exemplar
  build ScrapePayload(families, samples, metadata, exemplars)
```

关键函数：

- `_scan_metric_name()`：经典名字/引号名字；
- `_scan_label_set()`：标签集合，支持 `{"utf8.name", ...}`；
- `_scan_quoted()`：转义；
- `_parse_value()`：`+Inf`/`NaN`/科学计数法；
- `_parse_exemplar()`。

## 32.5 严格性

Mini 选择"严格解析"而不是"尽量容忍"：

- 非法转义报错；
- `TYPE` 在样本之后报错；
- 重复 `HELP`/`TYPE` 冲突报错；
- 同序列同时间戳不同值报错；
- `# EOF` 缺失（OpenMetrics 模式）报错；
- 非法指标名/标签名报错。

理由：exporter bug 越早暴露越好；静默丢弃会制造"指标消失"的悬案。

## 32.6 OpenMetrics

OpenMetrics 的额外处理：

- `# EOF`：必须出现（`openmetrics=True` 时）；
- `# UNIT`：写入 metadata；
- exemplar：`metric 1 # {trace_id="x"} 0.5 123`；
- counter 的 `_total` 命名不做强制（解析器只记录）；
- `# TYPE` 名称与样本名可能不同（`requests` vs `requests_total`）。

Mini 不实现 protobuf exposition，也不做 content-type 协商（由抓取层根据响应判断）。

## 32.7 序列化回写

`format_exposition()` 用于：

- `/metrics` 自身指标；
- `/federate`；
- 测试 roundtrip。

转义规则：

```python
value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
```

`HELP` 只转义 `\` 和换行。测试 `test_format_escapes_label_values` 覆盖 roundtrip。

## 32.8 元数据

`MetricMetadata(metric, type, help, unit)`：

- 抓取层把每个 target 的 metadata 存在 `TargetState.metadata`；
- `/api/v1/metadata` 聚合；
- `/api/v1/targets/metadata` 按 target 过滤。

Mini 不持久化 metadata 到 WAL/block；重启后只有新抓取才有。

## 32.9 测试覆盖

`tests/test_exposition.py`：

- 基础样本/标签/时间戳；
- HELP/TYPE/UNIT；
- 转义/非法转义；
- 特殊浮点；
- 重复样本；
- 空值标签；
- sample limit；
- OpenMetrics EOF/UNIT/exemplar；
- 引号指标名/标签名、UTF-8 解析与格式化 roundtrip（`test_utf8_names.py`）；

`tests/test_labels.py`：

- 排序/去重/空值；
- merge/without/keep；
- matcher 全匹配/缺失标签；
- 字符串解析/转义。

## 32.10 与真实 Prometheus 的差异

- 真实解析器用 Go 的 `textparse`，性能高，支持 protobuf；
- 真实解析器对某些边界更宽松（如时间戳格式）；
- Mini 的 `ScrapePayload` 一次性加载全部样本，真实实现流式解析；
- Mini 的 metadata 不持久化。

这些差异不影响教学语义，但生产 exporter 应以官方解析器/`promtool check metrics` 为准。

## 32.11 UTF-8 名称：解析与回写

Prometheus 3.0 允许指标名和标签名使用任意 UTF-8。Mini 用两条规则实现：

1. **legacy 名字不需要引号**：`http_requests_total{job="api"}` 保持原样；
2. **非 legacy 名字必须引号**：指标名写作 `{"http.server.duration", "service.name"="api"}`，标签名同样加引号。

`labels.py` 提供分类与转义：

```python
is_legacy_metric_name("http_requests_total")   # True
is_legacy_metric_name("http.server.duration")  # False
quote_metric_name("http.server.duration")      # '"http.server.duration"'
format_labels(Labels({...}))                   # exposition/联邦输出
```

exposition 解析器里有一个关键歧义：花括号内第一个引号字符串既可能是指标名，也可能是 UTF-8 标签名。Mini 用"引号后是否紧跟 `=`"来区分：

```python
{"metric.name", "label.name"="value"}
#        ↑ 没有 = → 指标名
#                       ↑ 有 = → 标签名
```

因此 `_scan_label_set` 增加了 `_quoted_followed_by_equals`；PromQL 的 `parse_selector` 用 `peek(1)` 做同样的判断。`# HELP "metric.name" ...` 与 `# TYPE "metric.name" histogram` 也走 `_scan_metric_name` 的引号分支。

exposition 回写与 `/federate` 走 `labels.format_labels`：经典名输出 `name{...}`，非经典名输出 `{"name",...}`；标签值和名称里的 `\`、换行、`"` 都做转义。API JSON 不回写名称、直接保留 key；`/metrics` 的自身指标仍是 legacy 名称，由字符串拼接生成。

回归测试：

- `tests/test_utf8_names.py` 覆盖分类、格式化、exposition、PromQL、聚合与 `label_replace`；
- 旧测试确保 legacy 输出逐字节不变；
- roundtrip 测试保证 `parse → format → parse` 的 labels 与值一致。

Mini 的边界：配置里的 relabel `target_label` 只校验非空，不强制 legacy 字符集；运行时 `Labels` 也不限制字符集，因此 relabel 和 `label_replace` 都能生成 UTF-8 标签，供 `sum by ("service.name")` 使用。规则 `labels` 同样接受 UTF-8 名称。完整的转义协商与迁移清单见附录 AF。

## 本章小结

- Labels 不可变、排序、空值丢弃；Matcher 正则全匹配。
- 选择器必须有一个不匹配空串的 matcher。
- exposition 解析器严格校验转义、TYPE 顺序、重复样本、OpenMetrics EOF。
- 元数据只存在内存，API 聚合展示。
- 严格解析是为了让 exporter bug 尽早暴露。

## 延伸阅读

- 本教程第 3、5 章（数据模型/exposition）。
- `mini_prometheus/labels.py`、`exposition.py`。
- 真实 Prometheus `model/textparse/`。

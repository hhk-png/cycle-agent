# 第 39 章 Mini 实现（八）：服务发现、relabel 与抓取循环

## 39.1 发现接口

`scrape/discovery.py`：

```python
@dataclass
class DiscoveredTarget:
    labels: dict[str, str]
    source: str = "static"
```

`DiscoveryManager.discover(job)` 合并四类 provider：

- `_from_static()`：`targets` + `labels` + `__address__`/`__scheme__`/`__metrics_path__`/`__param_*`；
- `_from_files()`：glob + YAML/JSON，`__meta_filepath`；
- `_from_http()`：GET JSON，`__meta_url`，失败保留上次成功结果；
- `_from_dns()`：A/AAAA，`__meta_dns_name`。

去重键是 `address|job`。

## 39.2 基础标签

```python
def _base_labels(self, job):
    labels = {
        "job": job.job_name,
        "__scheme__": job.scheme,
        "__metrics_path__": job.metrics_path,
    }
    for name, values in job.params.items():
        labels[f"__param_{name}"] = values[0] if values else ""
    return labels
```

组标签/目标标签覆盖基础标签；`__address__` 最后设置。

## 39.3 relabel

`relabel_target(labels, rules)`：

- 复制 dict；
- 逐条执行 action；
- `keep`/`drop` 返回 `None` 表示丢弃；
- `replace` 用 source 拼接 + regex fullmatch + `$1` 展开；
- `labelmap` 对标签名匹配；
- `hashmod` 用 MD5 前 8 字节的大端 uint64 取模（与 Prometheus 分片一致）；
- `lowercase`/`uppercase`；
- `keepequal`/`dropequal`。

`targets_for(job)` 在 relabel 后：

- 校验 `__address__` 非空；
- 校验 `__scheme__`/`__metrics_path__`；
- 默认 `instance = __address__`；
- 返回 `DiscoveredTarget`。

`relabel_samples(labels, rules)` 类似，但要求最终 `__name__` 非空，否则丢弃样本。

## 39.4 目标状态

```python
@dataclass
class TargetState:
    job_name
    labels            # 含 __ 内部标签
    scrape_config
    url
    health            # unknown/up/down
    last_error
    last_scrape
    last_duration
    last_samples
    scrape_count
    failure_count
    series: set[Labels]   # 上次成功抓取的样本标签
    metadata
    exemplars
```

`target_labels()` 去掉 `__` 前缀，得到样本上的 job/instance/静态标签。

## 39.5 抓取流程

```python
def scrape_once(target, timestamp=None):
    start_ms = timestamp or now
    headers = {...}
    add auth (bearer/basic)
    response = fetch(url, headers, timeout)
    if status != 200: failure
    if body_size_limit exceeded: failure
    openmetrics = content-type starts with application/openmetrics or b"# EOF" in body
    payload = parse_exposition(text, default_timestamp=start_ms, openmetrics=..., max_samples=...)
    target_labels = target.target_labels()
    for sample in payload.samples:
        labels = merge_labels(sample.labels, target_labels, honor_labels)
        labels = relabel_samples(labels, metric_relabel_configs)
        if not labels: continue
        check label limits
        ts = sample.timestamp if honor_timestamps else start_ms
        merged.append((labels, ts, sample.value))
        seen.add(labels)
    appended, errors = tsdb.append_batch(merged)
    append up/scrape_* metrics
    for stale_labels in target.series - seen:
        tsdb.append(stale_labels, start_ms, stale_nan())
    target.series = seen
```

要点：

- `honor_labels` 冲突处理：默认 target 优先，scraped 改名 `exported_*`；true 时 scraped 优先；
- `honor_timestamps` false 时统一用 start_ms；
- metric relabel 在 target labels 合并之后；
- 序列消失写 stale；
- sample/label limits 在合并/relabel 后检查；
- 失败写 up=0 + scrape_duration。

## 39.6 失败

`_failure()`：

```python
target.health = "down"
target.last_error = error
append up=0, scrape_duration_seconds, scrape_body_size_bytes
for labels in target.series:
    append(labels, start_ms, stale_nan())
target.series.clear()
```

失败抓取被视为空抓取：为上次成功抓取的序列写 stale，查询立即看不到旧值；`up=0` 仍然写入。这与真实 Prometheus 的失败语义一致。

## 39.7 调度

`ScrapeManager`：

- `sync()`：对比 desired/existing，增删目标；
- `_target_loop()`：每 interval 抓一次；
- `start_threads=False`：只 sync，不起线程（测试/演示）；
- `reload()`：停止旧目标、写 stale、重建；
- `metrics()`：scrapes_total/up/scrape_pool_targets。

`target_key()` = job + 标签哈希，用于稳定识别。

## 39.8 URL 与认证

`_build_url()`：

```text
scheme://address/path?param=...
```

- `__param_*` 覆盖配置 params；如果值等于配置的第一个值，保留所有值（避免多值丢失）；
- basic auth：`base64(user:pass)`；
- bearer token/file；
- TLS：`ssl.create_default_context`，支持 CA/client cert/insecure；
- 代理/重定向未完整实现（配置字段存在）。

## 39.9 测试

`tests/test_scrape.py`：

- static/file/http SD；
- relabel keep/drop/replace instance；
- 成功/HTTP 500/解析错误/连接超时；
- honor_labels/timestamps；
- 序列消失/目标移除 stale；
- sample/body/auth/params；
- OpenMetrics exemplar；
- 重复时间戳；
- metrics 文本；
- 未知目标。

## 39.10 与真实 Prometheus 的差异

- 无线程池/并发上限（每目标一线程）；
- 无 Kubernetes/EC2/Consul SD；
- 无完整 TLS 配置/代理/重定向；
- 无 `scrape_protocols` 协商；
- 无 `track_timestamps_staleness`；
- 无 target limit/body decompression。

## 39.11 目标状态机与 relabel 顺序

目标从发现到抓取经历一条明确的状态链：

```text
discovery 产出 DiscoveredTarget
  → target relabel_configs（可 drop 目标、改 __address__/__scheme__/__metrics_path__）
  → 生成 target labels（去掉 __ 前缀）
  → 加入 scrape pool，health=unknown
  → 首次抓取成功 → health=up，写 up=1
  → 抓取失败 → health=down，写 up=0 + scrape_* 指标，并给上次成功序列写 stale
  → 目标从 SD 消失 → 不写 up=0；给该目标上次成功序列写 stale，然后移除目标
```

最后一条容易混淆：**"抓取失败"和"目标消失"不是一回事**。抓取失败是 Prometheus 知道目标应该存在但连不上，`up=0` 是明确信号；目标消失是 SD 不再返回它，Prometheus 不能假设它挂了（可能是滚动发布/缩容），因此不会伪造 `up=0`，而是写 stale 让旧序列消失。第 12 章从语义上解释，这里对应 `TargetState.health`、`_failure()` 与 `_stale_target()` 的实现。

relabel 的两阶段顺序：

1. **target relabel_configs**：在抓取前作用于发现到的标签，决定"抓不抓、抓哪里"；`keep/drop` 可以整目标丢弃；`__address__`、`__scheme__`、`__metrics_path__`、`__param_*` 在这一阶段生效。
2. **抓取 + exposition 解析**：得到样本标签。
3. **honor_labels 合并**：`honor_labels: false` 时目标标签覆盖样本标签，冲突的样本标签改名为 `exported_<name>`；`true` 时保留样本标签，冲突的目标标签改名为 `exported_<name>`。
4. **metric_relabel_configs**：在合并之后、写 TSDB 之前作用于每个样本；可以 drop 单条序列、改标签、做基数治理。
5. **limits**：`sample_limit` 在 metric relabel 之后计数（被 relabel 丢掉的样本不计入），`label_limit` 检查最终标签数。

实现上的两个细节：

- `relabel_samples()` 返回 `None` 表示丢弃该样本；target relabel 返回 `None` 表示丢弃整个目标；
- `hashmod` 用 MD5 前 8 字节的大端 uint64 取模，与 Prometheus 的分片算法一致；分片时通常对 `__address__` 做 hashmod，再用 `keep` 选本分片。

与真实 Prometheus 的差异：Mini 没有 `scrape_failure_log_file`、没有 target relabel 的 `keepequal/dropequal` 之外的元标签细节、没有完整的 SD 类型；但两阶段 relabel 的顺序与 `honor_labels` 语义是对齐的（测试见 `tests/test_relabel.py`、`tests/test_scrape.py`）。

## 本章小结

- 发现→relabel→抓取→合并标签→metric relabel→TSDB。
- relabel 正则全匹配、`$1` 展开；keep/drop 返回 None。
- honor_labels 决定冲突时谁优先；honor_timestamps 决定时间戳来源。
- 失败写 up=0，并为上次成功抓取的序列写 stale；append 错误不改变 up。
- Mini 的每目标一线程适合教学，不适合数千目标。

## 延伸阅读

- 本教程第 11、12 章（SD/抓取语义）。
- `mini_prometheus/scrape/`。
- 真实 Prometheus `scrape/`。

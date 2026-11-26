# 第 41 章 Mini 实现（十）：HTTP API、联邦与远程读写

## 41.1 服务器结构

`api/server.py`：

```python
class PrometheusServer:
    start(): ThreadingHTTPServer + handler
    stop(): shutdown/server_close
```

`_make_handler(app)` 生成 `BaseHTTPRequestHandler` 子类，`Handler.application = app`。

路由是显式 dict：

```python
routes = {
  "/api/v1/query": self._query,
  "/api/v1/query_range": self._query_range,
  ...
}
```

未匹配返回 404。

## 41.2 请求解析

`_params()`：

- 解析 query string；
- POST/PUT 读取 body（`_read_body()` 缓存）；
- `application/x-www-form-urlencoded` 合并到 params；
- 无 content-type 也按 form 处理。

`_read_body()` 缓存是关键：remote write/read 需要原始 body，但 `_params()` 不能把它消费掉。Mini 早期把 body 读进局部变量后丢弃，导致 remote write 读到空 body、请求超时；修复为实例级 `_raw_body`。

## 41.3 JSON envelope

成功：

```json
{"status":"success","data":{...},"warnings":[...]}
```

错误：

```json
{"status":"error","errorType":"bad_data","error":"..."}
```

状态码映射：

```text
bad_data → 400
execution → 422
not_found → 404
unavailable/timeout → 503
internal → 500
```

## 41.4 查询序列化

```python
def _value_to_json(value, timestamp):
    Vector → {"resultType":"vector","result":[{"metric":..., "value":[ts/1000, "1"]}]}
    Matrix → {"resultType":"matrix","result":[{"metric":..., "values":[[ts/1000,"..."],...]}]}
    Scalar → {"resultType":"scalar","result":[ts/1000,"..."]}
    String → {"resultType":"string","result":[ts/1000,"..."]}
```

- 时间戳秒（浮点）；
- 值字符串（保留 NaN/+Inf 表示）；
- vector 按 labels 排序。

## 41.5 端点清单

查询/元数据：

```text
/api/v1/query
/api/v1/query_range
/api/v1/series
/api/v1/labels
/api/v1/label/<name>/values
/api/v1/metadata
/api/v1/query_exemplars
```

目标：

```text
/api/v1/targets
/api/v1/targets/metadata
```

规则/告警：

```text
/api/v1/rules
/api/v1/alerts
/api/v1/alertmanagers
```

`/api/v1/rules` 的 group 对象带 `queryOffset`（秒），对应组级 `query_offset` 或全局 `rule_query_offset`；规则求值语义见第 40.10 节。

状态：

```text
/api/v1/status/buildinfo
/api/v1/status/runtimeinfo
/api/v1/status/config
/api/v1/status/flags
/api/v1/status/tsdb
/api/v1/status/walreplay
```

管理（需 `--web.enable-admin-api`）：

```text
POST /api/v1/admin/tsdb/snapshot
POST/PUT /api/v1/admin/tsdb/delete_series
POST /api/v1/admin/tsdb/clean_tombstones
```

写入：

```text
POST /api/v1/write          # remote write (JSON / protobuf+Snappy)
POST /api/v1/read           # JSON remote read
POST /api/v1/otlp/v1/metrics  # OTLP/JSON，需 --web.enable-otlp-receiver
```

其他：

```text
/federate
/metrics
/-/healthy
/-/ready
/-/reload
/-/quit
/graph
/
```

## 41.6 query_range

参数：

- `query` 必填；
- `start`/`end`：RFC3339 或 Unix 秒；
- `step`：duration 或秒；
- 点数上限 11001（`(end-start)/step ≤ 11000`）；
- end < start 报错；
- step <= 0 报错。

结果矩阵按 step 输出；scalar 查询生成无标签序列。

## 41.7 series/labels/values

`match[]` 必须是普通 vector selector：

```python
expression = parse_expr(text)
if not isinstance(expression, ast.VectorSelector): error
```

- series 返回标签集合；
- labels 返回名称；
- label values 返回值；
- 支持 start/end 过滤；未传时使用 `MIN_TIME..MAX_TIME`，而不是 `0..now`，因此负时间戳/未来时间戳也能被看到（第三轮审计修复）。

## 41.8 联邦

`/federate?match[]=...`：

- 解析 matcher；
- 查询最近 5m；
- 每个序列取最新样本；
- 输出文本格式。

Mini 不做 `honor_labels`/label 冲突处理（federation 的抓取端会处理）。

## 41.9 自身指标

`/metrics` 输出：

```text
prometheus_build_info
prometheus_tsdb_head_series
prometheus_tsdb_head_samples_appended_total
prometheus_tsdb_blocks_loaded
prometheus_tsdb_storage_blocks_bytes
prometheus_config_last_reload_successful
prometheus_rule_evaluations_total
prometheus_rule_evaluation_failures_total
prometheus_target_scrapes_total
up
```

不是完整的 Prometheus 自监控指标，但覆盖关键量。

## 41.10 Remote Write 接收

`/api/v1/write`：

- `application/json`：`{"timeseries":[{"labels":{...},"samples":[[ts,val]]}]}`；
- `application/x-protobuf`：先 Snappy 解压，再解析 WriteRequest；
- 返回 204；
- 无认证/限流（教学）。

`remote.py` 手写 protobuf wire：

- varint；
- length-delimited；
- fixed64 double；
- 嵌套 message；
- native histogram 字段直接报错（不静默丢）。

Snappy：

- 解压支持 literal/copy（1/2/4 字节 offset）；
- 压缩是 literal-only（合法但未压缩），用于测试/发送。

## 41.11 Remote Read

`/api/v1/read`：

```json
{"queries":[{"start_ms":0,"end_ms":...,"matchers":[{"name":"__name__","op":"=","value":"x"}]}]}
```

返回：

```json
{"series":[{"labels":{...},"samples":[[ts,val]]}]}
```

Mini 只支持 JSON；真实 Prometheus 用 protobuf。

## 41.12 App 组装

`app.py`：

```python
config = load_config(...)
tsdb = TSDB(...).open()
scrape = ScrapeManager(...)
groups = load_rule_groups(...)
notifier = AlertmanagerNotifier(...)
rules = RuleManager(...)
remote_writer = RemoteWriter(...)  # 可选
server = PrometheusServer(app)
server.start()
scrape.start()
rules.start()
maintenance thread: tsdb.compact()
```

`shutdown()` 反序停止；`reload()` 重新加载配置/规则/目标/remote writer。

## 41.13 CLI

`__main__.py`：

```bash
python -m mini_prometheus --config.file=... --storage.tsdb.path=... \
  --web.listen-address=127.0.0.1:9090
python -m mini_prometheus check config FILE
python -m mini_prometheus check rules FILE...
python -m mini_prometheus tsdb list --storage.tsdb.path DATA
python -m mini_prometheus query instant URL 'up'
```

`--version`、`--web.enable-admin-api`、`--log.requests` 等。

## 41.14 测试

`tests/test_api.py`：

- query/query_range/scalar/string/matrix；
- 错误/状态码/缺参；
- series/labels/values；
- targets/metadata；
- rules/alerts/alertmanagers；
- status；
- health/ready/graph/metrics/federate/CORS；
- admin 开关/snapshot/delete；
- remote write JSON/protobuf/错误 content-type；
- remote read；
- reload；
- POST form；
- OPTIONS。

`tests/test_otlp.py` 另外覆盖 OTLP 接收；`tests/test_utf8_names.py` 覆盖引号名称。

## 41.15 OTLP/HTTP JSON 接收

Mini 的 OTLP 接收分三层：

```text
api/server.py      HTTP 语义：POST、content-type、gzip、状态码
otlp.py            OTLP → Prometheus：名称/单位/属性/delta
app.py             生命周期：--web.enable-otlp-receiver 时创建 receiver
```

启动：

```bash
python -m mini_prometheus \
  --config.file=prometheus.yml \
  --storage.tsdb.path=./data \
  --web.enable-otlp-receiver \
  --enable-feature=otlp-deltatocumulative
```

`AppOptions` 对应字段：

```python
enable_otlp_receiver: bool = False
otlp_translation_strategy: str = "NoUTF8EscapingWithSuffixes"
otlp_delta_to_cumulative: bool = False
```

注意：`--storage.otlp.translation-strategy` 是 Mini 自己的 CLI flag，默认 `NoUTF8EscapingWithSuffixes`；上游把翻译策略放在 `otlp.translation_strategy` 配置里，默认 `UnderscoreEscapingWithSuffixes`。Mini 也把其余 resource attributes 直接提升为标签、不生成 `target_info`，与上游默认不同（见附录 AG）。

HTTP 处理要点：

- receiver 未启用时端点返回 404，不暴露"半开"状态；
- 只接受 `application/json`（含 `charset` 参数）；protobuf 返回 400 并说明 Mini 不支持；
- `Content-Encoding: gzip` 解压后再检查 64 MiB 上限，防 zip bomb；
- 成功返回裸 200 空 body，不是 `{"status":"success"}`——OTLP/HTTP 与 Prometheus API 的响应约定不同；
- 解析失败/不支持的指标类型 → `bad_data` → 400；
- 样本通过 `tsdb.append_batch` 写入；重复/乱序样本计入 `prometheus_otlp_append_errors_total`，但不改变 HTTP 200。

翻译层在 `otlp.py`：

- `_resource_labels`：`service.name`→`job`、`service.instance.id`→`instance`、`service.namespace` 拼进 `job`；
- `_sanitize_label_name`：丢弃 `__name__`/`__*`；NoUTF8 策略保留 UTF-8 标签名（`k8s.pod.name`），Underscore 策略转 `k8s_pod_name`；
- `_family_name`：单位后缀 + 单调 sum 的 `_total`；
- `_translate_sum/_translate_histogram/_translate_summary`：数值、桶、quantile；
- `_delta_value/_delta_histogram`：按 labels 维护累计状态，用 `startTimeUnixNano` 检测 reset，乱序点丢弃。

自身指标：

```text
prometheus_otlp_received_samples_total
prometheus_otlp_received_metric_points_total
prometheus_otlp_delta_resets_total
prometheus_otlp_append_errors_total
```

完整规则、与上游的差异（NHCB、protobuf、exemplar）见附录 AG；可运行示例是 `examples/10_otlp_ingest.py`。

## 41.16 API 中的 UTF-8 名称

API 的 JSON 不做名字转义：`metric` 对象的 key 就是标签名，`__name__` 的值就是指标名。因此：

```json
{"metric": {"__name__": "http.server.duration", "service.name": "api"}}
```

查询时 URL 编码：

```bash
curl -G 'http://127.0.0.1:9090/api/v1/query' \
  --data-urlencode 'query={"http.server.duration", "service.name"="api"}'
```

不要手工拼 `?query={"...`，花括号、引号、点号都会被 shell/URL 解析破坏。`/federate` 与 exposition 回写用 `labels.format_labels`：经典名 `name{...}`，非经典名 `{"name",...}`，标签值转义 `\\`、`\n`、`\"`；`/metrics` 的自身指标是 legacy 名称，API JSON 则保留原始 key、不做名称转义。实现细节见第 32、37 章与附录 AF。

## 本章小结

- API 用显式路由 + JSON envelope + 统一错误映射。
- `_read_body()` 缓存避免 body 被参数解析消费。
- 查询结果时间戳秒、值字符串；vector 排序。
- `/federate` 只返回最近样本；remote write 支持 JSON/protobuf。
- OTLP/JSON 接收把 resource/scope/delta 翻译成 Prometheus 样本，成功响应是裸 200。
- App/CLI 把组件组装成可运行服务。

## 延伸阅读

- 本教程第 18、41 章（远程/API）。
- `mini_prometheus/api/server.py`、`remote.py`、`app.py`、`__main__.py`。
- 附录 J（Mini API 参考）。

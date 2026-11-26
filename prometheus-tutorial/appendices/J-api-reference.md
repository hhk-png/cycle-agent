# 附录 J Mini-Prometheus API 参考

## J.1 启动

```bash
python -m mini_prometheus \
  --config.file=examples/config/prometheus.yml \
  --storage.tsdb.path=./data \
  --web.listen-address=127.0.0.1:9090 \
  --web.enable-admin-api
```

## J.2 通用约定

- 成功：`{"status":"success","data":...}`；
- 错误：`{"status":"error","errorType":"...","error":"..."}`；
- 时间参数：RFC3339 或 Unix 秒；
- 返回值时间戳：秒（浮点）；
- 值：字符串（含 `NaN`/`+Inf`/`-Inf`）。

## J.3 查询

### GET/POST /api/v1/query

参数：

```text
query   必填
time    可选（默认 now）
```

例子：

```bash
curl -s 'http://127.0.0.1:9090/api/v1/query?query=up&time=1700000000'
```

响应：

```json
{
  "status": "success",
  "data": {
    "resultType": "vector",
    "result": [
      {"metric": {"__name__": "up", "job": "demo", "instance": "127.0.0.1:9100"}, "value": [1700000000.0, "1"]}
    ]
  }
}
```

### GET/POST /api/v1/query_range

参数：

```text
query   必填
start   必填
end     必填
step    必填（duration 或秒）
```

最多 11001 点（`(end-start)/step ≤ 11000`，与上游一致）。

### /api/v1/series

参数：

```text
match[]  必填（可重复）
start/end 可选
```

### /api/v1/labels

可选 `match[]`/`start`/`end`。

### /api/v1/label/<name>/values

同上。

### /api/v1/metadata

可选 `metric`/`limit`。

### /api/v1/query_exemplars

参数：`query`（vector selector）、`start`/`end`。

## J.4 目标

### /api/v1/targets

参数：`state=any|active|dropped`。

返回：

```json
{
  "activeTargets": [
    {
      "discoveredLabels": {"__address__": "127.0.0.1:9100", "job": "demo"},
      "labels": {"job": "demo", "instance": "127.0.0.1:9100"},
      "scrapePool": "demo",
      "scrapeUrl": "http://127.0.0.1:9100/metrics",
      "health": "up",
      "lastError": "",
      "lastScrape": "2026-11-26T10:00:00.000Z",
      "lastScrapeDuration": 0.01
    }
  ],
  "droppedTargets": [],
  "scrapePools": ["demo"]
}
```

### /api/v1/targets/metadata

参数：`match_target`/`metric`/`limit`。

## J.5 规则/告警

### /api/v1/rules

参数：`type=alert|record`（可选）。

返回的 group 对象包含 `name`/`file`/`interval`/`limit`/`queryOffset`/`lastEvaluation`/`evaluationTime`/`health`/`lastError`/`rules`。Mini 的 `queryOffset` 以秒为单位，来自组级 `query_offset` 或全局 `rule_query_offset`。

### /api/v1/alerts

返回 active 告警：

```json
{"data":{"alerts":[{"labels":{},"annotations":{},"state":"firing","activeAt":"...","value":"1"}]}}
```

### /api/v1/alertmanagers

返回配置的 Alertmanager 目标。

## J.6 状态

```text
/api/v1/status/buildinfo
/api/v1/status/runtimeinfo
/api/v1/status/config
/api/v1/status/flags
/api/v1/status/tsdb
/api/v1/status/walreplay
```

`/status/config` 返回 YAML 字符串；`/status/tsdb` 返回 head 统计与 blocks。

## J.7 管理（需 --web.enable-admin-api）

### POST /api/v1/admin/tsdb/snapshot

返回：

```json
{"status":"success","data":{"name":"01J..."}}
```

### POST/PUT /api/v1/admin/tsdb/delete_series

参数：`match[]` 必填，`start`/`end` 可选。

### POST /api/v1/admin/tsdb/clean_tombstones

无参数。

## J.8 其他

### /federate?match[]=...

返回文本格式最新样本。

### /metrics

自身指标（文本）。

### /-/healthy, /-/ready

```text
Mini-Prometheus is Healthy.
Mini-Prometheus is Ready.
```

### POST /-/reload

重新加载配置/规则。

### /graph

简单查询 UI。

## J.9 Remote

### POST /api/v1/write

JSON：

```json
{"timeseries":[{"labels":{"__name__":"x"},"samples":[[1700000000000,1.0]]}]}
```

protobuf：

```http
Content-Type: application/x-protobuf
Content-Encoding: snappy
```

返回 204。

### POST /api/v1/read

```json
{"queries":[{"start_ms":0,"end_ms":1700000000000,"matchers":[{"name":"__name__","op":"=","value":"x"}]}]}
```

返回：

```json
{"series":[{"labels":{"__name__":"x"},"samples":[[1700000000000,1.0]]}]}
```

## J.10 错误码

```text
400 bad_data
401/403 未实现（无认证）
404 not_found
422 execution
500 internal
503 unavailable/timeout
```

## J.11 限制

- 无认证/TLS server；
- JSON remote read（非 protobuf）；
- 最多 11001 点/range query（`(end-start)/step ≤ 11000`）；
- 无 `/api/v1/status/tsdb` 的完整字段；
- 无 `/api/v1/admin/tsdb/delete_series` 的完整语义（tombstone 近似）。

## 延伸阅读

- 第 41 章（API 实现）。
- `mini_prometheus/api/server.py`。

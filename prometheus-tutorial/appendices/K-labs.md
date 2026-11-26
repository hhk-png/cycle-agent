# 附录 K 实验手册

## Lab 0：环境

```bash
python -m pip install -r requirements.txt
python -m pytest
python examples/run_all.py
python check_docs.py
```

目标：三条命令全绿。

## Lab 1：第一条链路

终端 A：

```bash
python examples/demo_exporter.py --port 9100
```

终端 B：

```bash
python -m mini_prometheus \
  --config.file=examples/config/prometheus.yml \
  --storage.tsdb.path=./data-lab1 \
  --web.listen-address=127.0.0.1:9090
```

查询：

```bash
curl -s 'localhost:9090/api/v1/query?query=up'
curl -s 'localhost:9090/api/v1/targets'
```

问题：

- `up` 的标签有哪些？分别来自哪里？
- `/targets` 的 `discoveredLabels` 与 `labels` 差异？

## Lab 2：PromQL 速率

```promql
rate(demo_http_requests_total{code="200"}[1m])
increase(demo_http_requests_total{code="200"}[5m])
sum by (code) (rate(demo_http_requests_total[1m]))
histogram_quantile(0.9, demo_request_duration_seconds_bucket)
```

问题：

- 手算 `rate`，与结果对比；
- 把窗口改成 `[10s]`，结果为什么为空/抖动？
- 把 demo exporter 停掉，`up` 多久后消失？

## Lab 3：relabel/file_sd

```bash
python examples/07_relabel_and_sd.py
```

练习：

- 修改 `examples/07_relabel_and_sd.py` 的 keep 条件，只保留 `rack=r2`；
- 加一个 `labelmap`，把 `__meta_*` 映射成 `meta_*`；
- 观察目标数量变化。

## Lab 4：规则/告警

```bash
python examples/05_alerting.py
```

问题：

- t+0 与 t+30 的状态？
- 为什么 critical 抑制 warning？
- 修改 `for` 为 5m，观察状态变化。

## Lab 5：TSDB 生命周期

```bash
python examples/03_storage_lifecycle.py
```

练习：

- 把 `block_range_ms` 改小，观察 flush 频率；
- 删除 `data` 目录前先快照；
- 重启后查询历史数据。

## Lab 6：远程读写

```bash
python examples/06_remote_write.py
```

练习：

- 把 `protocol` 改成 `prometheus`（protobuf+snappy）；
- 用 `curl` 发 JSON remote write；
- 用 `/api/v1/read` 查询。

## Lab 7：API

```bash
curl -s 'localhost:9090/api/v1/query?query=1+1'
curl -s 'localhost:9090/api/v1/query_range?query=up&start=...&end=...&step=15s'
curl -s 'localhost:9090/api/v1/series?match[]={__name__=~".+"}'
curl -s 'localhost:9090/api/v1/labels'
curl -s 'localhost:9090/api/v1/rules'
curl -s 'localhost:9090/federate?match[]=up'
```

问题：

- 错误 envelope 的字段？
- 时间戳单位？
- 空结果与错误的区别？

## Lab 8：失败与 stale

```bash
python examples/08_staleness_and_failures.py
```

问题：

- 为什么 b 序列立即消失，而 `up` 仍然可查？
- 目标移除时如何写 stale？
- 抓取失败时为什么要写 stale？`up=0` 为什么仍然写入？

## Lab 9：容量

用 `appendices/F-capacity-model.md` 的计算器：

- 假设 500 目标、每目标 400 序列、15s、15d；
- 计算摄入/磁盘/内存；
- 把间隔改为 30s，比较；
- 把序列数翻倍，比较。

## Lab 10：SLO

用 demo exporter 的 `http_requests_total`：

```promql
sum(rate(demo_http_requests_total{code!~"5.."}[5m]))
/
sum(rate(demo_http_requests_total[5m]))
```

练习：

- 构造 5xx 比例 >5% 的场景（修改 exporter）；
- 写 burn-rate 告警；
- 用 Alertmanager-lite 接收。

## Lab 11（可选）：真实 Prometheus

- 下载 Prometheus；
- 抓取自身；
- 用相同 PromQL/规则；
- 对比 Mini 与真实结果（允许格式差异）。

## 提交物

每个 Lab 记录：

```text
命令
观察结果
与预期的差异
根因
改进
```

## 延伸阅读

- 各章末尾的实验建议。
- `examples/` 源码。

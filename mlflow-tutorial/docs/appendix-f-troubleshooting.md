# 附录 F 故障排查

## F.1 启动与连接

| 症状 | 可能原因 | 处理 |
|---|---|---|
| `Address already in use` | 端口占用 | 换端口/找进程 |
| 客户端连不上 | host 绑定 127.0.0.1 / 防火墙 | `--host 0.0.0.0`（注意安全） |
| 401 | 服务端设了 token | 客户端带 Bearer |
| 413 | 请求体超过上限 | 减少 batch/上传大小 |
| 500 且日志有 SQLite lock | 写并发/长事务 | 增大 busy_timeout、缩短事务、换数据库 |
| UI 空白/无数据 | 连错数据库 | 检查 `tracking_uri` 与 env |
| `no such table` | 未初始化/旧库 | 运行 `db init` 或检查路径 |

## F.2 Run/指标

| 症状 | 可能原因 | 处理 |
|---|---|---|
| 参数写入报 immutable | 同 key 不同值 | 新 run 或改 key |
| 写入报 run FINISHED | 终态 | 恢复/新 run |
| 搜索不到指标 | 写到了另一个 experiment | 查 `attributes.experiment_id` |
| 最新值不是最后一次调用 | 时间戳回退 | 检查 `timestamp` |
| 历史点重复 | 重试/重复日志 | 批次 ID/幂等 |
| 指标 NaN 被拒 | 非有限值 | 清洗/跳过 |

## F.3 Artifact

| 症状 | 原因 | 处理 |
|---|---|---|
| 路径 escapes root | 绝对路径/`..` | 用相对路径；不要绕过 API |
| 上传成功但看不到 | 写到不同 run/服务 | 检查 run_id/URI |
| 下载目录多一层/少一层 | `log_artifacts` 语义 | 记住复制“内容” |
| 文件损坏 | 中断/非原子写 | 重新上传；生产查 digest |
| 磁盘满 | artifact 无 GC | 清理/生命周期 |

## F.4 模型

| 症状 | 原因 | 处理 |
|---|---|---|
| `MLmodel not found` | artifact 缺失/路径错 | 检查 `runs:/` source |
| `UnsupportedFlavor` | loader 不在白名单 | 只支持 linear；真实 MLflow 用对应 flavor |
| 列缺失/类型错误 | 服务输入 schema 不符 | 对齐签名/列顺序 |
| 加载成功但预测全错 | 列顺序/单位/编码 | 用 input_example 回归 |
| `models:/@alias` 404 | 别名未设置/版本删了 | 查 Registry |
| run 已删但版本还在 | source 悬空 | 对账/恢复 artifact |

## F.5 Registry

| 症状 | 原因 | 处理 |
|---|---|---|
| 创建版本版本号冲突 | 并发 MAX+1 | 重试/换序列 |
| 删除模型失败 | 有版本且未 force | 归档/force（谨慎） |
| 重命名后子表找不到 | 外键未级联 | Mini 已支持 ON UPDATE CASCADE |
| 别名是数字被拒 | 与版本歧义 | 用字母别名 |

## F.6 Projects

| 症状 | 原因 | 处理 |
|---|---|---|
| 子进程找不到 mini_mlflow | PYTHONPATH | runner 已注入；手动运行时设置 |
| 日志写不进同一 run | 没传 run_id | `start_run(run_id=env)` |
| 项目命令语法错误 | 不支持 shell 操作符 | 写成脚本 |
| 工作目录逃逸 | working_dir 在项目外 | 修正 MLproject |
| 子进程失败但 run 未结束 | 未走 finally | 检查 runner/异常路径 |
| Windows 路径含反斜杠 | shlex POSIX 解析 | 用正斜杠/脚本 |

## F.7 Tracing

| 症状 | 原因 | 处理 |
|---|---|---|
| trace 未保存 | 没有 sink/client/默认 DB | 配置 sink 或 tracking URI |
| 一个 span 变成两个 root | 隐式 trace 逻辑 | 用 `start_trace` 显式包裹 |
| 线程里没有当前 span | ContextVar 不跨线程 | 传 parent/新 trace |
| 负载被截断 | 超过上限 | 调大/脱敏/采样 |
| trace 状态 OK 但子 span 报错 | 状态未传播 | Mini 会置 ERROR；检查版本 |

## F.7.1 Webhook

| 症状 | 原因 | 处理 |
|---|---|---|
| 创建报 unknown event | 事件名不是 `entity.action` | `webhooks.EVENT_NAMES` 查合法值 |
| 创建报 URL 非法 | scheme/凭据/fragment | 用 `http(s)://host/path`，不要嵌入 user:pass |
| test 一直 FAILED | 接收方 5xx/超时/签名拒绝 | 查 delivery 的 `last_error`/`response_status` |
| 事件不产生 | 未订阅该事件 / webhook DISABLED | 查 `events` 与 `status` |
| 投递重复 | at-least-once + 响应丢失 | 接收方按 `X-Mlflow-Delivery-ID` 幂等 |
| 签名验不过 | 对解析后的 JSON 重签 / 时间戳不对 | 对原始 body 验签，检查 header |
| pending 一直增长 | dispatcher 未启动/接收方慢 | `serve` 会启动；测试用 `drain_once` |
| 服务重启后事件丢了 | 业务提交与 outbox 非同一事务 | 生产改事务性 outbox（第 38 章） |

## F.7.2 评测数据集

| 症状 | 原因 | 处理 |
|---|---|---|
| create 报 records must not be empty | 传了空数组 | 至少一条记录 |
| record inputs must be a non-empty object | 记录缺少 `inputs` | 改成 `{"inputs": {...}}` |
| must be JSON-serializable without NaN/Infinity | 含 NaN/Infinity/自定义对象 | 清洗成标准 JSON 值 |
| dataset would exceed ... bytes | 合并后超过 16 MiB | 拆分数据集或改存 artifact 引用 |
| merge 报 Conflict 且重试后仍失败 | 并发写者过多 | 客户端退避重试；生产用单写队列 |
| digest 与预期不一致 | 记录顺序或字段不同 | 用规范化 JSON 对比；顺序是 digest 的一部分 |
| search 返回 records 为空 | 搜索只返回元数据 | 用 `get_dataset` 取完整记录 |
| 真实 MLflow 迁移后记录数变少 | 真实 merge 按 inputs 哈希去重 | 迁移前对账，确认重复样本是否符合预期 |

## F.7.3 监控与漂移

| 症状 | 原因 | 处理 |
|---|---|---|
| `features must be a non-empty mapping` | 没传特征契约 | 至少声明一个 `name:numeric/categorical` |
| `feature 'x' has no numeric baseline values` | 基线该列全缺失或类型不对 | 清理基线；数值列不能含字符串 |
| 窗口状态总是 ALERT，但 PSI 不高 | KS/缺失/新类别/小样本命中 | 看 `report["reasons"]`，不要只看 PSI |
| 稳定窗口也报 KS | 只存直方图时并列值会导致假距离 | Mini 已保留最多 2000 个样本；确认没有手工改 baseline |
| PSI 为 0 但状态仍 WARN | `min_samples_warn`（默认 30） | 合并窗口或降低该阈值要谨慎 |
| 新类别让 PSI 爆表 | 业务新增枚举，模型未见过 | 先回退/默认策略，再补映射与训练 |
| `monitor '...' already exists` | 名称唯一 | 用新名称或先删除旧 monitor |
| `unknown threshold keys` | 阈值键拼错 | 用 `DEFAULT_THRESHOLDS` 的键 |
| 删除 monitor 后窗口也没了 | 外键级联 | 生产应改软删除/保留期 |
| 真实平台没有 monitor 表 | MLflow 不是监控系统 | 把报告写 run/artifact 或接外部 TSDB（第 45 章） |

## F.7.4 Agent 会话与评测

| 症状 | 原因 | 处理 |
|---|---|---|
| `search_sessions` 返回空 | trace 没带 `mlflow.trace.session` 或 scan 上限太小 | 检查 tag；提高 `max_results`；确认实验 id |
| 会话顺序错乱 | trace timestamp 相同且 id 排序不反映轮次 | 写入时带稳定的 turn index/timestamp |
| `score_sessions` 后 assessment 变多 | 每次换了 `source_id` | 固定 `source_id`；同一 `(trace,name,source)` 会 upsert |
| 在线采样每次结果不同 | 采样键含随机数/时间 | 固定 `source_id + scorer_name + session_id` |
| `assert_improved` 报 `insufficient_pairs` | 可配对样本 <6 | 扩大评测集或换配对键；门禁应 fail closed |
| `compare_paired` 区间跨 0 | 差异不显著 | 不要发布；检查样本量与方差 |
| `detect_issues` 只有 `span_error` 没有 `tool_failure` | span_type 不是 TOOL/TOOL_CALL/FUNCTION | 统一埋点类型；或在工具层补 span |
| 模拟器 `stop_reason=script_exhausted` | 脚本轮次用完但目标未满足 | 增加 turns/放宽 must_include；检查目标检查规则 |
| 真实平台 `ScorerSamplingConfig` 启动失败 | 未开 `MLFLOW_SERVER_ENABLE_CUSTOM_SCORERS=true` 或用了 lambda/async | 开环境变量；改成可序列化的 `@scorer` 函数 |
| Issues 检测不可用 | 文件后端/无后台任务/无模型访问 | 换 SQL 后端与支持的模型；先小流量试跑（第 46 章） |

## F.8 测试/验证

```bash
python -m pytest -q
python -m pytest tests/test_search.py -q
python scripts/check_docs.py
python scripts/verify.py
```

常见：

- 测试污染：检查是否用了全局 `mlflow.db`；
- Windows 临时目录清理失败：关闭文件句柄；
- 端口未释放：确保 `server.shutdown()`/`server_close()`；
- 文档链接失败：运行 check_docs 看具体路径。

## F.9 诊断命令

```bash
# 看数据库
sqlite3 mlflow.db ".tables"
sqlite3 mlflow.db "select run_id,status,start_time from runs order by start_time desc limit 5;"

# 看服务
curl -s http://127.0.0.1:5000/health

# 看日志
# 服务器默认 logging 到 stderr；生产中接集中日志
```

Windows 没有 `sqlite3` 时可用 Python：

```bash
python -c "import sqlite3;c=sqlite3.connect('mlflow.db');print(c.execute('select name from sqlite_master where type=\"table\"').fetchall())"
```

## F.10 安全事件

- 怀疑 token 泄漏：立即轮换，检查访问日志；
- 怀疑恶意模型：隔离服务，停止加载，审计 artifact；
- 路径穿越：检查 artifact 根外文件，修复版本；
- XSS：清理恶意实验名/标签，检查 UI 输出；
- Webhook SSRF：禁用可疑订阅，检查出站日志/元数据访问，补私网封禁；
- 数据泄漏：按合规流程处置，保留证据。

---

返回：[教程首页](../README.md) · [附录 G](appendix-g-glossary.md)

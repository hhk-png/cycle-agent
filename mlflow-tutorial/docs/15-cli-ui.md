# 第 15 章 CLI 与 Web UI：把系统变成可操作的产品

## 学习目标

- 设计一套可组合的 CLI：JSON 输出、稳定退出码、环境变量。
- 理解服务端渲染 UI 的取舍和安全边界。
- 能用 UI/CLI 完成实验排查，而不是只看 API。
- 识别可访问性、性能和 XSS 风险。

## 15.1 CLI 的设计原则

Mini 的 CLI：

```bash
python -m mini_mlflow --help
python -m mini_mlflow server --help
python -m mini_mlflow experiments create demo
python -m mini_mlflow runs list --filter 'metrics.auc > 0.8'
python -m mini_mlflow registry versions my-model
python -m mini_mlflow run ./project -P alpha=0.1
python -m mini_mlflow models serve -m models:/my-model@champion
python -m mini_mlflow prompts register support-reply --template "Hi {{name}}"
python -m mini_mlflow prompts set-alias support-reply production 2
python -m mini_mlflow feedback add --trace-id "$TRACE_ID" --name quality --value 0.9
python -m mini_mlflow gateway validate --config examples/gateway.yaml
python -m mini_mlflow webhooks create promote-on-alias --url https://ci.example/hooks \
  --event model_version_alias.created
python -m mini_mlflow webhooks test <WEBHOOK_ID>
python -m mini_mlflow webhooks deliveries <WEBHOOK_ID>
```

原则：

1. **JSON 输出**：便于 `jq`/脚本；人类可读性交给 `jq` 或未来的 `--format table`。
2. **稳定退出码**：0 成功，2 业务错误，1 未预期错误。
3. **错误也 JSON**：stderr 输出 `{error_code, message}`。
4. **`--tracking-uri` 可覆盖**：本地/远端一套命令。
5. **不隐藏危险操作**：删除需要明确命令；没有交互式确认，适合自动化，但应由上层审批。

### 子命令结构

```text
server
db init
doctor
experiments create/list/get/delete
runs list/get/metrics
registry list/versions/set-alias
run <project>
models serve/predict
prompts register/get/list/versions/set-alias
feedback add/list
gateway validate/invoke
webhooks create/list/get/update/delete/test/deliveries
datasets create/list/get/merge/delete
logged-models list/get/create/finalize/tag/params/metrics/delete
monitoring create/list/get/delete/window/windows
```

真实 MLflow 3.x 的 CLI 更庞大，但仍包含 `mlflow models build-docker`、`mlflow models serve`、`mlflow gc`、`mlflow db upgrade` 等；`recipes` 已在 3.0 移除，独立 deployment server/`start-server` 也已移除（客户端 `mlflow deployments` 命令是否保留取决于版本）。Mini 只保留教学闭环。

## 15.2 退出码与错误

```python
try:
    return int(args.func(args) or 0)
except MiniMlflowError as exc:
    print(json.dumps(exc.to_dict()), file=sys.stderr)
    return 2
```

为什么不用 1 表示业务错误？因为 1 常被 shell/CI 当作“通用失败”；2 更容易区分“输入/资源问题”和“程序崩溃”。

自动化的伪代码：

```bash
if output=$(mini-mlflow runs get "$RUN_ID" 2>err.json); then
  echo "$output" | jq .info.status
else
  code=$?
  if [ "$code" -eq 2 ]; then
    jq .error_code err.json
  else
    echo "unexpected failure"
  fi
fi
```

## 15.3 UI 的定位

Mini 的 UI 是**服务端渲染的只读视图**：

- 首页：实验、最近 run、最近 trace；
- 实验页：run 列表 + 搜索；
- run 页：状态、参数、指标、历史、artifact、输入数据；
- trace 页：span 瀑布 + 请求/响应 + feedback/expectations；
- registry 页：模型、版本、别名、source；
- prompts 页：模板版本、变量、别名、提交说明。

它不提供：

- 编辑/删除按钮；
- 图表对比；
- 模型审批；
- 实时刷新；
- 用户管理。

这是刻意的：UI 越复杂，越容易掩盖 API 语义问题。先用 CLI/API 验证，再让 UI 成为“可读的调试面板”。

## 15.4 为什么不用前端框架

优点：

- 零构建、零 npm；
- 直接读 Python 对象；
- 容易审计 XSS；
- 部署简单。

缺点：

- 每次交互整页刷新；
- 无客户端状态；
- 大数据集渲染慢；
- 无图表库。

真实 MLflow UI 是 React 应用，支持 run 对比、图表、模型版本操作。Mini 的取舍是“让读者看懂数据流”。

## 15.5 XSS 防护

所有动态值：

```python
html.escape(str(value), quote=True)
```

包括：

- 实验名、run 名、标签；
- 指标 key、参数值；
- 错误消息；
- artifact 路径；
- trace 的 request/response JSON。

在 `<pre>` 中也要转义，否则 `</pre><script>` 会逃逸。

测试（`MiniClient` 没有 `get` 方法；UI 是 HTTP 页面，要用 HTTP 客户端取）：

```python
import urllib.request

service.create_experiment("<script>alert(1)</script>")
with urllib.request.urlopen(f"{base_url}/ui") as response:
    html = response.read().decode("utf-8")
assert "<script>alert(1)</script>" not in html
```

真实测试见 `tests/test_server_client.py::test_ui_escapes_and_serves`；若启用鉴权，请求要带 `Authorization` 头。

artifact 下载：

- 强制 `application/octet-stream`；
- `Content-Disposition: attachment`；
- `X-Content-Type-Options: nosniff`。

不要用 `<img src>` 直接渲染用户上传的 SVG/HTML。

## 15.6 UI 查询与搜索

首页表单把 `filter` 传给服务：

```text
/ui?filter=metrics.acc > 0.8 and tags.team = "nlp"
```

服务端调用同一套搜索编译器；非法语法返回错误页，而不是 500。

注意：

- URL 查询会被日志记录，避免放敏感值；
- 复杂过滤器可能很慢，需要超时/上限；
- UI 不应允许任意 SQL。

## 15.7 可访问性与可读性

最低要求：

- 语义化表格/标题；
- 对比度足够；
- 键盘可操作；
- 错误信息可读；
- 不依赖颜色区分状态（同时显示文字）。

Mini 的 CSS 是内联的，状态同时用文本（FINISHED/FAILED）和颜色。

## 15.8 性能

UI 的 N+1 风险：

- 首页列出实验，再对每个实验查 run；
- run 页对每个指标查历史；
- Registry 页对每个模型查版本。

Mini 限制了：

- 最近 run 50；
- 历史只查前 8 个指标；
- Registry 最多 200 个模型/版本。

生产应：

- 增加分页/懒加载；
- 缓存聚合；
- 异步加载大字段；
- 避免在请求线程里做重查询。

## 15.9 与真实 MLflow 的差距

- 真实 UI 有 run 对比、图表、模型版本操作、notebook 集成；
- 真实 CLI 有 `models serve/build-docker`、`gc`、`db upgrade` 等；Recipes 与独立 deployment server 已在 3.0 移除；
- 真实 UI 有用户/权限/多租户；
- Mini 的 UI 只读、无前端路由。

## 小结

- CLI 的输出是给机器和脚本的；稳定性比“好看”重要。
- UI 是调试面板，不是治理平台。
- 服务端渲染必须全量转义；artifact 不能作为可执行内容返回。
- 查询/分页/缓存决定 UI 在大数据量下是否可用。

## 练习

1. 给 `runs list` 加 `--format table`，保持 JSON 为默认。
2. 为 UI 增加搜索表单的错误提示与空状态。
3. 设计一个 artifact 预览功能，说明如何避免 XSS 和大文件阻塞。

---

下一章：[第 16 章 Projects](16-projects.md)

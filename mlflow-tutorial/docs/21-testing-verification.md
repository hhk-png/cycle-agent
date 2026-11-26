# 第 21 章 测试与验证：如何证明它能跑

## 学习目标

- 设计分层测试：领域、存储、协议、CLI、安全、文档。
- 理解“绿测试”不等于“正确”，并主动寻找假绿。
- 用契约测试保证本地/远端一致，用安全测试证明边界。
- 建立可重复的验证命令与报告。

## 21.1 测试金字塔（本项目版）

```text
                 ┌────────────────────┐
                 │ 端到端/示例/CLI     │  ~20
                 ├────────────────────┤
                 │ HTTP 契约/项目/评分 │  ~70
                 ├────────────────────┤
                 │ 服务/模型/追踪/评测 │  ~90
                 ├────────────────────┤
                 │ 工具/搜索/存储单元  │  ~60
                 └────────────────────┘
```

截至 2026-11-26，`python -m pytest -q` 收集并通过 **273** 个测试（Windows；非 Windows 会跳过 1 个路径测试）。数字会随代码演进；以 `pytest --collect-only -q` 和 `scripts/verify.py` 为准。

## 21.2 单元测试

覆盖：

- 配置解析、路径、URI、分页 token、校验函数；
- 搜索词法/语法/AST/SQL 编译（不连数据库）；
- 模型 fit/predict、签名推断、输入规范化；
- 评测指标公式；
- Trace 的 span 关系/OTel 映射；
- AgentOps 的会话聚合、模拟器、scorer、采样、分诊与配对比较；
- RAG 的 BM25 排序/过滤、检索指标、引用与 grounding。

原则：

- 不启动 HTTP/数据库；
- 用纯函数/临时目录；
- 断言具体值，不断言“不抛异常”；
- 错误路径与正常路径同等重要。

## 21.3 存储与服务测试

fixture：

```python
@pytest.fixture()
def config(tmp_path):
    return MiniConfig(
        backend_store_uri=f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}",
        artifact_root=str(tmp_path / "artifacts"),
        port=0,
    )
```

每个测试独立数据库，避免顺序依赖。验证：

- 参数不可变；
- 批量原子回滚；
- 指标历史/最新值；
- 软删除/恢复；
- artifact 路径；
- 注册表版本/别名。

### 假绿风险

测试可能“恰好编码了 bug”：

- 只断言 latest = 0.3，没有验证历史顺序；
- 只测正常路径，没测 `..`；
- 用 1 条样本训练，恰好矩阵奇异但被 alpha 掩盖；
- 断言 500 而不是正确错误码；
- mock 掉真正要验证的层。

本项目在审计中会专门检查“测试是否在验证行为，而不是验证实现细节”。

## 21.4 HTTP 契约测试

启动一个临时服务器（端口 0）：

```python
server = create_server(config, host="127.0.0.1", port=0)
thread = threading.Thread(target=server.serve_forever, daemon=True)
```

验证：

- 健康检查免鉴权；
- 错误 token → 401；
- 实验/run/指标/搜索；
- artifact 上传/下载；
- 模型日志/加载/别名；
- trace；
- UI XSS 转义；
- 错误码形状；
- 评分协议：四种输入形状、签名校验、请求体上限与 401/400/413；
- CORS。

测试结束后 `shutdown()` + `server_close()` + join，避免端口/线程泄漏。

### Windows 上的 RST 陷阱

一个真实踩过的坑：服务端在返回 401/413 前没有读取请求体，客户端还在发送数据时连接被关闭，Windows 会发 RST，测试里看到的是 `ConnectionAbortedError`，而不是预期的 401/413 JSON。修法是**先有界排空请求体，再返回错误，并显式 `Connection: close`**（Mini 的 `server.py` 与 `scoring.py` 都做了）。这类问题在 Linux 上可能偶发、在 Windows 上高频，属于“测试基础设施也是被测行为”的典型例子。

### 本地/远端一致性

理想做法是参数化 fixture：

```python
@pytest.fixture(params=["local", "remote"])
def client(request, config):
    ...
```

同一场景跑两遍，断言核心字段。Mini 的部分测试分开写；生产应抽成参数化套件，减少遗漏。

## 21.5 子进程测试

Projects/CLI 必须走真实子进程，因为：

- 环境变量、工作目录、PYTHONPATH 只在子进程暴露；
- active run 不跨进程；
- 编码/换行/退出码是真实行为。

测试：

- 成功项目；
- 非零退出 → FAILED；
- 未知参数/入口点；
- 工作目录逃逸；
- 命令注入不被 shell 执行；
- CLI JSON 输出/退出码。

## 21.6 安全测试

必须显式覆盖：

- artifact `../`、绝对路径、盘符、NUL；
- 搜索注入/未知字段；
- 未知 loader；
- HTML 注入；
- 未授权请求；
- 请求体超限；
- 下载目录逃逸；
- 模型签名缺失/列错位。

安全测试不要 mock 掉防护层；要发真实请求/调用真实函数。

### 21.6.1 GenAI 控制面的测试

第 33 章的 `tests/test_genai_ops.py` 采用“确定性 + 故障注入”模式：

| 对象 | 必测行为 |
|---|---|
| Prompt | 变量提取、缺/多变量、控制流拒绝、别名解析、删除 |
| Assessment | 标量校验、来源枚举、upsert 保留 create_time、trace 内嵌、按 assessment 搜索 |
| Gateway | 成功成本、primary 失败回退、全失败 attempts、rpm 限流、缓存命中、配置校验 |
| 远端 | prompt/assessment/gateway 的 HTTP 往返与错误码 |

第 44 章的 Dataset 测试单独放在 `tests/test_datasets.py`：

| 对象 | 必测行为 |
|---|---|
| Dataset | 记录契约、digest 稳定性、schema/profile、合并版本、CAS 冲突、并发 merge、搜索、删除、远端与 CLI |

关键原则：

- 不联网：MockProvider 让失败、延迟、成本都可复现；
- 不 sleep：限流测试用 `rpm=1` 触发第二次失败；
- 不只看成功：回退、全失败、缓存命中、非法输入都要有断言；
- 远端一致性：同一场景走本地 service 与 HTTP 两条路径。

### 21.6.2 Webhook 的失败注入

`tests/test_webhooks.py` 刻意不联网，用可注入的 `sender` 复现投递语义：

| 场景 | 断言 |
|---|---|
| 正常投递 | 事件、payload、`X-Mlflow-*` 头、HMAC 验签 |
| 5xx 重试 | attempts 递增，达到上限后 FAILED |
| 服务重启 | outbox 中的 PENDING 在新实例继续投递 |
| 事件过滤 | 未订阅事件不产生投递 |
| 禁用订阅 | 不再入队；`test_webhook` 返回 FAILED |
| 重复 drain | DELIVERED 不会被再次选中 |
| 远端契约 | create/get/list/test/delete 走 HTTP 往返 |

这类测试的价值是**把失败路径变成回归行为**；只测“发送成功”等于没测重试与幂等。

### 21.6.3 监控的统计断言

`tests/test_monitoring.py` 的 12 个测试把统计行为固定下来：

| 场景 | 断言 |
|---|---|
| 稳定窗口 | PSI≈0、状态 STABLE、窗口持久化 |
| 数值漂移 | PSI/KS 同时越界、报告含原因 |
| 新类别 | unseen_rate、类别列表、Cramér's V |
| 全缺失 | 该特征 ALERT，原因明确 |
| 小样本 | `min_samples_warn` 原因存在 |
| 常量基线 | 等于/不等于两桶，不除零 |
| 统计函数 | PSI 恒等、KS 并列值、卡方临界值 |
| 非法输入 | 空记录、NaN、未知阈值、时间倒置 |

统计测试最容易被写成“只断言状态是 ALERT”。状态只是结论，**原因、统计量和样本数才是可复盘的部分**；三者都要断言。

## 21.7 文档测试

`scripts/check_docs.py` 检查：

- README 链接可达；
- 章节/附录文件数量；
- 代码围栏成对；
- 未完成标记；
- 每章有“小结”；
- 相对链接指向存在的文件。

它不能验证：

- 技术描述是否正确；
- 命令能否执行；
- 数字是否与代码一致。

因此还需要 `scripts/verify.py` 跑测试 + 文档检查 + 部署静态检查 + 示例。它只打印结果，不自动写附录 I；附录 I 由维护者在发布前手工同步。

## 21.8 性能与负载测试

Mini 不做完整压测，但应知道指标：

- 单 run 写入 QPS；
- 1 万 run 的搜索延迟；
- 100 万 metric 的最新值查询；
- artifact 上传吞吐；
- 并发读/写锁等待。

一个可运行的最小压测（本地 SQLite，只测元数据写入与搜索，不测 artifact）：

```python
import tempfile
import time
from pathlib import Path

from mini_mlflow.config import MiniConfig
from mini_mlflow.tracking import TrackingService

tmp = Path(tempfile.mkdtemp())
service = TrackingService(
    MiniConfig(
        backend_store_uri=f"sqlite:///{(tmp / 'bench.db').as_posix()}",
        artifact_root=str(tmp / "artifacts"),
        port=0,
    )
)
try:
    run_ids = []
    start = time.perf_counter()
    for index in range(500):
        run = service.start_run(run_name=f"bench-{index}")
        service.log_metric(run.info.run_id, "loss", 1.0 / (index + 1))
        run_ids.append(run.info.run_id)
    write_seconds = time.perf_counter() - start

    start = time.perf_counter()
    for _ in range(50):
        service.search_runs(
            filter_string="metrics.loss < 0.1",
            experiment_ids=["0"],
            max_results=100,
        )
    search_ms = (time.perf_counter() - start) / 50 * 1000
    print({"runs_per_second": round(500 / write_seconds, 1),
           "search_p50_ms": round(search_ms, 2)})
finally:
    service.close()
```

这段代码只给出“怎么开始测”，不是基准结论：结果依赖机器、SQLite 版本、WAL 状态、并发度和数据分布。生产应使用 Locust/k6/JMeter，并在真实数据库/对象存储上测；第 25 章给出容量模型，第 45 章给出监控窗口的成本估算。

## 21.9 验证命令

```bash
python -m pytest -q
python scripts/check_docs.py
python scripts/check_deploy.py
python scripts/verify.py
```

`verify.py` 会：

1. 运行测试；
2. 运行文档检查；
3. 运行部署参考编排的静态检查（YAML/Nginx/shell，不启动 Docker）；
4. 在临时目录运行示例；
5. 输出汇总，失败时非零退出。

## 21.10 当前测试的盲区

诚实列出：

- 没有真实 Postgres/S3 集成测试；
- 没有浏览器级 UI 测试；
- 没有大规模并发/故障注入；
- 评分服务只验证协议与错误码，没有压测、流式、多副本与 GPU 测试；
- Trace 异步/采样未实现；
- 反序列化安全只覆盖“拒绝未知 loader”；
- Windows 文件系统大小写/锁行为只覆盖部分；
- 没有形式化验证/属性测试（可用 Hypothesis 增强）；
- 没有对真实 MLflow REST 的兼容性测试。

这些盲区不影响“教学闭环可运行”的结论，但决定了它不能直接上生产。

## 21.11 与真实 MLflow 的差距

- 真实项目有更庞大的测试矩阵、多后端、迁移、UI E2E、性能测试；
- 真实系统需要兼容性测试（客户端/服务端版本）；
- 真实 MLflow 的 CI 覆盖云存储/数据库/框架集成；
- Mini 的 273 个测试只证明当前子集行为。

## 小结

- 测试要覆盖正常、错误、安全和跨进程路径。
- “全绿”不是正确性的证明；要检查测试是否在断言真实行为。
- 契约测试保证本地/远端一致；文档测试保证教程可导航。
- 明确盲区，比虚假的生产就绪声明更有价值。

## 练习

1. 为搜索 DSL 增加属性测试（随机 AST + 等价 SQL）。
2. 设计一个并发测试：两个线程同时写同一参数，断言最多一个成功。
3. 给 artifact 下载增加 symlink 逃逸测试（按平台跳过）。

---

下一章：[第 22 章 实验管理方法论](22-experiment-methodology.md)

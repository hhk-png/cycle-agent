# 前言：为什么用“造一遍”的方式学 MLflow

> 本教程有两条并行的主线：一条讲真实 MLflow 的概念、用法与生产实践；另一条用约一万八千行 Python 从零实现一个可运行的 MiniMLflow。两条线在每一章交汇。

## 1. 只调用 API 学不会平台

很多 MLflow 教程停在下面这段代码：

```python
import mlflow

with mlflow.start_run():
    mlflow.log_param("alpha", 0.1)
    mlflow.log_metric("rmse", 0.42)
```

这段代码能跑，但它没有回答真正困难的问题：

- 参数为什么不可变，而标签可以随时改？
- 指标的历史点、`step` 和时间戳谁决定“最新值”？
- 一个 run 结束以后，为什么还能读 artifact，却不能继续写指标？
- `models:/fraud@champion` 在服务端如何解析成一个具体版本？
- 搜索框里的 `metrics.acc > 0.9 AND tags.team = "nlp"` 如何避免 SQL 注入？
- 删除实验后，为什么 run 的 artifact 还在磁盘上？
- 当模型版本从 7 回滚到 6 时，到底改了哪几行数据？

MiniMLflow 把这些问题变成可以阅读、运行和测试的代码。它不是一个兼容层，也不假装能够替代 MLflow；它是一个“结构透明的模型”，让你在遇到真实平台问题时知道该看哪一层。

## 2. 两条主线怎么读

| 主线 | 你得到什么 | 对应内容 |
|---|---|---|
| 真实 MLflow | 生产环境的概念、命令、生态、治理和商业判断 | 第 1～4 章、第 22～32 章、附录 |
| MiniMLflow | 可运行的系统、数据库、REST、客户端、模型包、注册表、Prompt、Feedback、Gateway、Webhook、UI | 第 5～21 章、第 33、38 章及全部源码 |
| 3.x 与生产进阶 | LoggedModel、迁移、真实源码、运维、2026 平台化、事件驱动、真实 GenAI 实战、参考实现与部署 | 第 34～41 章、附录 K/L |
| 服务化与综合实战 | 推理协议、容器/K8s、批处理/灰度、端到端流失预警闭环 | 第 42～43 章 |
| 数据与评测资产 | run input、评测数据集、digest、schema/profile、trace → golden set | 第 44 章 |
| 上线后监控 | 基线 profile、PSI/KS/卡方、告警与再训练闭环 | 第 45 章 |
| Agent 运营 | 会话、模拟评测、在线评分、Issues 分诊、统计门禁 | 第 46 章 |
| GenAI 交付实战 | 知识助手、BM25/召回指标、引用/grounding、门禁、在线抽样、漂移与回滚 | 第 47 章、附录 M |
| 交叉验证 | 真实组件的简化映射与不可简化之处 | 每章末尾“与真实 MLflow 的差距” |

MiniMLflow 的实现刻意保持依赖极少：运行时只依赖 PyYAML，HTTP 服务、客户端、SQLite 访问、模型训练、评测指标、追踪全部基于 Python 标准库。这样做的目的不是“炫技”，而是让你能在一台没有 GPU、没有 Docker、没有云账号的电脑上跑通完整闭环。

## 3. 读者假设

你应当会：

- 写 Python 函数和类，理解虚拟环境与 `pip`；
- 使用 `git` 和命令行；
- 知道训练集/验证集、过拟合、精确率与召回率的基本含义；
- 大致听说过 REST、SQL、YAML。

你不需要会：

- 前端框架、Kubernetes、Spark；
- 深度学习框架；
- 任何云厂商账号。

## 4. 运行约定

所有命令默认在教程根目录执行：

```bash
cd mlflow-tutorial
python -m pip install -r requirements.txt
python -m pytest -q
python scripts/check_docs.py
```

启动教学服务器：

```bash
python -m mini_mlflow server \
  --backend-store-uri sqlite:///mlflow.db \
  --artifact-root ./mlartifacts \
  --host 127.0.0.1 --port 5000
```

打开 <http://127.0.0.1:5000/ui>。默认不启用鉴权；一旦设置 `--auth-token`，API 需要 `Authorization: Bearer <token>`。

真实 MLflow 的命令会标注为“真实 MLflow”，并且不保证与你安装的版本逐字一致。MLflow 3.x 迭代很快，遇到 flag 差异请以 `mlflow <command> --help` 为准。

## 5. 源码地图

```text
mini_mlflow/
  config.py            配置与环境变量
  errors.py            稳定错误码
  entities.py          领域对象与线格式
  tracking.py          Tracking 用例层
  registry.py          Model Registry 业务规则
  search.py            搜索 DSL：词法、语法、SQL 编译
  artifacts.py         本地 artifact 仓库与路径安全
  store/sqlite_store.py 持久化、事务、软删除
  models/              MLmodel 打包、签名、PyFunc、线性 flavor
  client.py            本地/远端 Python 客户端
  server.py            REST 服务与 UI 路由
  ui.py                服务端渲染 HTML
  cli.py               命令行
  projects.py          MLproject 执行器
  evaluate.py          回归/分类/LLM 指标
  autolog.py           fit 回调式自动记录
  tracing.py           Trace/Span 与 OTel 导出
  scoring.py           模型推理服务
  prompts.py           Prompt Registry：版本、别名、安全渲染
  datasets.py          评测数据集：digest、schema/profile、合并 CAS
  monitoring.py        监控基线、PSI/KS/卡方与不可变窗口报告
  gateway.py           AI Gateway：路由、回退、限流、缓存、成本
  rag.py               BM25、recall@k/MRR/nDCG、引用与 groundedness
  webhooks.py          Registry/Prompt/Dataset 事件、outbox、签名与重试
tests/                 273 个自动化测试（以验证报告为准）
examples/              20 个可直接运行的端到端示例 + gateway.yaml + deploy/
```

## 6. 本教程不做什么

- 不声称 MiniMLflow 与真实 MLflow 的 REST 请求/响应逐字节兼容。
- 不实现分布式存储、RBAC、OIDC、多租户隔离、GPU 调度。
- 不把“能跑”当成“可上生产”。每章会明确列出差距。
- 不把未经验证的商业数字写成事实。第 31 章给出的是带假设的建模方法。

## 7. 一条建议

第一次读，请先跑通第 4 章，再按第 5～10 章的顺序读实现。第二次读，围绕一个你真实遇到的问题跳读，例如“为什么模型上线后指标对不上”，直接看第 11、17、24、42 章；要做自动化发布，看第 38 章；要用真实 MLflow 做 GenAI 评测，看第 39 章；要管理评测集、golden set 与数据治理，看第 44 章；要知道上线后怎么发现数据漂移、怎么设计告警与再训练，看第 45 章；要做 Agent 的会话评测、在线评分与问题分诊，看第 46 章；要把知识助手的检索、引用、门禁、在线抽样与回滚串起来，看第 47 章与附录 M；要理解模型中心或自建部署，看第 40、41 章；想把整条交付链走一遍，看第 43 章。

读完你应该能回答三个问题：

1. MLflow 的每个对象在数据库里到底长什么样？
2. 哪些一致性、权限和成本问题被开源核心留给了使用方？
3. 在你的组织里，哪些能力应该买、哪些应该自建、哪些根本不需要？

---

下一章：[第 1 章 为什么需要 MLflow](01-why-mlflow.md)

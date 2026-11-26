# 第 16 章 Projects：可复现实验的执行器

## 学习目标

- 读懂 `MLproject` 的结构、参数类型与入口点。
- 理解“命令 + 环境 + 参数 + 源码”如何组成可复现执行。
- 实现一个无 shell 注入的参数化执行器，并知道它与真实 MLflow 的差异。
- 识别 Conda/Docker、路径、子进程状态与环境漂移问题。

## 16.1 MLproject 是什么

`MLproject` 是一个 YAML 描述文件：

```yaml
name: churn-demo

entry_points:
  main:
    parameters:
      alpha:
        type: float
        default: 0.1
      data_path:
        type: string
        default: data/train.csv
    command: "python train.py --alpha {alpha} --data {data_path}"

  evaluate:
    parameters:
      model_uri:
        type: string
    command: "python evaluate.py --model-uri {model_uri}"
```

它回答：

- 项目叫什么；
- 有哪些入口点；
- 每个入口点的参数类型/默认值；
- 实际执行什么命令。

真实 MLflow Projects 还支持：

- `python_env`（conda/virtualenv）；
- `docker_env`（镜像）；
- `storage_dir`；
- 参数类型 string/int/float/bool/path；
- 从 Git URI 执行项目；
- 后端执行（Databricks、Kubernetes 等）。

Mini 支持：

- YAML 解析与参数校验；
- 类型转换；
- 命令占位符替换；
- 子进程执行；
- run 生命周期、日志、源码快照；
- 子进程 run 恢复。

不支持：

- Conda/Docker；
- 远程项目源；
- 并行/分布式执行；
- 依赖安装。

## 16.2 参数与类型

| 类型 | 转换 | 示例 |
|---|---|---|
| string | `str` | `"hello"` |
| path | `str`（相对项目根） | `data/train.csv` |
| int | `int`，拒绝 bool | `42` |
| float | 有限 float | `0.1` |
| bool | true/false/1/0/yes/no | `true` |

缺省规则：

- 有 `default` → 使用默认；
- 无 `default` → 必填；
- 未知参数 → 报错。

注意：

- 类型转换发生在执行前，错误信息要指出参数名；
- 参数值会写进 run params（字符串）；
- 不要把 secret 作为参数日志记录；真实场景应从环境/secret manager 注入。

## 16.3 命令构造与注入

错误做法：

```python
command = entry.command.format(**params)
subprocess.run(command, shell=True)
```

参数值 `"; rm -rf /"` 会被 shell 执行。

Mini 的做法：

1. `_split_command(entry.command)` 按空白和单双引号切分（**不用 `shlex`**）；
2. 对每个 token 替换 `{param}`；
3. 不经过 shell，直接 `subprocess.run(list, shell=False)`；
4. 未知占位符报错。

```python
tokens = _split_command('python train.py --alpha {alpha}')
# ['python', 'train.py', '--alpha', '{alpha}']
rendered = ['python', 'train.py', '--alpha', '0.1']
```

为什么不用 `shlex.split`？POSIX 模式的 `shlex` 会把 Windows 路径 `C:\data\input.csv` 里的反斜杠当转义符吃掉，导致命令在 Windows 上指向错误文件。Mini 的 `_split_command` 只处理空白与引号，保留反斜杠；这也是教学实现与真实 MLflow `MLproject` 执行器的差异之一（真实实现要处理更多平台/引号规则）。

优点：

- 参数不会被解释为 shell 元字符；
- 可预测的 argv；
- 容易测试。

缺点：

- 不支持 `&&`、管道、重定向、通配符；
- 引号/空格规则是 Mini 自定义的子集，不是完整 POSIX/Windows 规则；
- 复杂命令应写成脚本，而不是塞进 MLproject。

如果确实需要 shell，应该：

- 明确声明“受信任项目”；
- 用沙箱/容器隔离；
- 对参数做严格白名单；
- 绝不把网络输入拼进命令。

## 16.4 执行流程

```text
load MLproject
  → 校验 entry point / 参数
  → 创建/查找 experiment
  → create run
  → 写参数、命令、project spec
  → 快照源码到 artifact
  → 构造 env（tracking URI、run_id、artifact root、PYTHONPATH）
  → subprocess.run(command, cwd=working_dir, shell=False)
  → 写 stdout/stderr artifact
  → 成功 FINISHED / 失败 FAILED
```

### 子进程如何记录到同一个 run

父进程创建 run 后，注入：

```text
MINI_MLFLOW_TRACKING_URI=...
MINI_MLFLOW_RUN_ID=<run_id>
MINI_MLFLOW_EXPERIMENT_ID=<experiment_id>
MINI_MLFLOW_ARTIFACT_ROOT=<absolute>
PYTHONPATH=<tutorial root>
```

子脚本：

```python
import os
import mini_mlflow as mlflow

with mlflow.start_run(run_id=os.environ["MINI_MLFLOW_RUN_ID"]):
    mlflow.log_metric("score", 0.9)
```

为什么要显式 `run_id`？因为 active run 不会跨进程继承。Mini 不自动读环境变量，避免环境漂移导致日志写错 run。

### 谁负责结束 run

子进程的 `with` 会结束 run；父进程再 `_end_if_running` 时看到终态，不会重复结束。若子进程没有结束，父进程会按退出码结束。这个双保险很重要，否则异常路径会留下永远 RUNNING 的 run。

## 16.5 环境可复现性

仅记录命令不够。一个可复现实验还需要：

- Python 版本；
- 依赖锁文件；
- 系统库/CUDA；
- 数据版本；
- 环境变量；
- 随机种子；
- 硬件（CPU/GPU 型号）。

真实 MLflow Projects 可以用 Conda/Docker 固定环境。Mini 只记录 `requirements` 的提示，不安装依赖。

推荐项目结构：

```text
project/
  MLproject
  requirements.txt
  requirements.lock
  train.py
  data/
    .gitignore
  README.md
```

数据不应直接提交到 Git（尤其 PII）；应通过 URI + digest + 访问控制引用。

## 16.6 源码快照

Mini 的 `_snapshot_project`：

- 递归收集文件；
- 排除 `.git`、`__pycache__`、虚拟环境、`mlruns`、`.db`；
- 限制 200 个文件 / 10 MB；
- 复制到临时目录后作为 `source` artifact 上传。

优点：

- 事后可看到实际执行代码；
- 不依赖 Git 历史；
- 对小型项目足够。

缺点：

- 大项目截断；
- 可能包含敏感配置；
- 快照时间与执行时间有窗口；
- 不记录未跟踪的依赖。

生产应使用 Git commit + 镜像 digest 作为主来源，快照作为辅助证据。

## 16.7 失败与超时

Mini：

- 非零退出码 → run FAILED；
- 超时 → 杀进程、退出码 124、FAILED；
- 日志/状态更新在 `finally` 中；
- 抛 `ProjectExecutionError` 带 stderr 尾部。

待改进：

- 捕获 `OSError`（命令不存在）并映射为项目错误；
- 子进程树清理（Windows 上 `taskkill /T`）；
- 信号处理；
- 资源限制（CPU/内存）；
- 并发项目调度。

## 16.8 与真实 MLflow 的差距

- 真实 Projects 支持 Conda、Docker、远程 Git、集群后端；
- 真实命令通常经 shell 执行，灵活性更高但注入风险更大；
- 真实参数/环境解析更复杂；
- 真实 run 与项目元数据字段更多；
- Mini 的安全模型假设“项目作者可信”，但仍避免参数注入。

## 小结

- MLproject 把“怎么运行”变成可版本化配置。
- 参数必须在执行前校验并记录；命令执行要避免 shell 注入。
- 环境（依赖/硬件）与代码同样重要，不能只记录命令。
- 子进程必须显式恢复 run；父进程要负责最终状态。

## 练习

1. 为 `MLproject` 增加 `docker_env.image` 字段，设计执行与安全边界。
2. 如何检测项目源码快照与实际 Git commit 不一致？
3. 给项目执行增加 CPU/内存限制，说明跨平台实现。

---

下一章：[第 17 章 Evaluate](17-evaluate.md)

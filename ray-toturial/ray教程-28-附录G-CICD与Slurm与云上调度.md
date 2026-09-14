仓库地址：https://github.com/hhk-png/cycle-agent

# 附录 G：CI/CD、Slurm 与云上调度

> 这份附录回答三个正文没展开的问题：**Ray 应用的 CI/CD 长什么样**、
> **只有 Slurm 没有 Kubernetes 的集群上怎么跑 Ray**、
> **云上这套东西到底花多少钱**。最后补一节"Ray 和 Airflow 这类编排器怎么接"。
> 四节按同一个标准写：能给全的命令给全，给不出的版本号、字段名、默认值
> 一律写 **未确认** —— 示例代码请当**骨架**读，不要当"照抄就能跑"。

---

## G.1 本章定位

第 17 章讲了部署形态，但对比表里缺了一列：**代码是怎么上去的**。
第 19 章把"集群与资源"画成第 1 层，却只列了 Kubernetes / Slurm / 云 VM 三个名字。
这份附录把这两处补上。

"Ray 在我这儿到底跑在哪"只有三个真实答案：

| 答案 | 谁管机器 | 本书哪里讲 | 本附录补什么 |
|---|---|---|---|
| **Kubernetes** | 平台团队，KubeRay Operator | 第 17 章（KubeRay v1.7、autoscaler、安全） | **镜像怎么造、怎么推、怎么灰度**（G.2） |
| **Slurm** | HPC / 研究集群管理员 | 全书只被提名两次，**零内容** | 整节（G.3） |
| **云 VM / 托管平台** | 你自己 / Anyscale | 第 17 章 §17.1、§17.7 | **节点池设计与成本口径**（G.4） |

再加一个正交的问题：**谁来触发这次运行** —— 通常不是 Ray 自己，
而是 Airflow / Prefect / 一个 cron（G.5）。

共同前提：**这四件事都无法在 mini-ray 上验证**。mini-ray 没有跨机部署、
没有容器、没有调度器对接（README 的"明确不做"清单）。它唯一能帮上忙的地方是
G.2 —— mini-ray 自己的 192 个测试就是纯 `pytest` 跑的，这本身就是
"Ray 形状的代码可以用 pytest 测"的最好证据。

---

## G.2 CI/CD for Ray 应用

### G.2.1 和普通 Python CI 差在哪

| 差异 | 表现 | 根因 |
|---|---|---|
| **测试是"集群形状"的** | 本地 `pytest` 全绿，CI 里起不来或极慢 | 测试要起进程、占端口、用共享内存；对象跨进程要真序列化 |
| **依赖不止在 `requirements.txt` 里** | 镜像里跑得通，`runtime_env` 那条路跑不通 | `runtime_env` 的 pip / `working_dir` 是**运行时**装的一套平行环境 |
| **"本地能跑，集群上挂"** | `ModuleNotFoundError` / 连不上 / GPU 数不对 | 代码之外的三个变量：代码怎么上去、资源怎么声明、网卡是哪张 |

第三条是重点。普通项目里"本地能跑"是强信号；Ray 项目里它几乎不算信号，
因为本地到集群之间隔着三层：**提交方式**（`ray.init()` vs Jobs API，第 04 章 §4.7）、
**资源声明**（第 08 章）、**依赖注入**（`runtime_env`）。

> **CI 的目标**：让"集群上挂"那类问题**在合并前暴露**，而不是上线前一晚暴露。

### G.2.2 测试金字塔：三层，成本差一个数量级

```
        ┌───────────────────────────────┐
   L2   │ 多节点冒烟(真集群 / 真镜像)    │  慢、贵,只在 main / nightly 跑
        ├───────────────────────────────┤
   L1   │ 单机 ray.init() 集成测试       │  秒级,可进每次 PR ← 主战场
        ├───────────────────────────────┤
   L0   │ 纯逻辑单元测试(不 import ray)  │  毫秒级,占测试数量的 80%+
        └───────────────────────────────┘
```

**L0：把逻辑从 `@ray.remote` 里拆出来测。** 写法就是让远程函数只做一层壳：

```python
# core.py —— 纯函数,不 import ray,直接 unit test
def normalize(rows, lo, hi):
    return [(x - lo) / (hi - lo) for x in rows]

# tasks.py —— Ray 只负责"在别处调用 core.normalize"
import ray
from core import normalize

@ray.remote
def normalize_task(rows, lo, hi):
    return normalize(rows, lo, hi)
```

好处不只是好测：它逼你把**业务逻辑与分布式管道分开**，
分布式部分出问题时你才能确定"不是逻辑写错了"。

**L1：单机 `ray.init()` 集成测试。** 关键认知是 ——
**绝大多数 Ray 逻辑可以单节点测**：task/actor 依赖关系、资源声明写错没有、
序列化可行不可行、actor 并发模型、重试与 lineage 重建。
一台 CPU 机器上跑 4 个 worker 就能覆盖其中绝大部分。

单节点**测不到**的（必须诚实列出来）：跨节点通信与对象传输
（`PENDING_ARGS_FETCH`、本地性、RDT）；多节点资源总和与放置组跨节点排布；
`runtime_env` 在**每个节点**上的安装；节点死亡与 spot 抢占引发的恢复路径；
任何与网卡、NCCL、RDMA 有关的事。

**L2：多节点冒烟。** 目标不是覆盖率，是**验证"集群形状"这个前提本身**：
镜像能拉起来、Ray 能组网、GPU 数对、`runtime_env` 装得完、Job 跑得完并退出。
一个 5 分钟的冒烟 Job 挡掉的故障，比 500 个单元测试还多。

> 常见错误：把 L2 做成"缩小版的完整测试套件"。它会变得又慢又脆，
> 最后所有人都学会"那个 job 红了不用管"。**L2 只放冒烟。**

### G.2.3 用 pytest 测 Ray：几个具体的坑

```python
# tests/conftest.py
import pytest, ray

@pytest.fixture(scope="session")
def ray_cluster():
    ray.init(num_cpus=4, num_gpus=0,
             include_dashboard=False,   # CI 不需要,少一个端口与一个进程
             ignore_reinit_error=True,  # 兜底,不是解决方案(见下)
             logging_level="ERROR")
    yield
    ray.shutdown()                      # ← 必须。漏掉 ray 进程会活到 CI 结束
```

**① `ray.shutdown()` 漏了会怎样。** 测试进程退出时 Ray 的 worker/raylet
**不保证**被回收，下一个测试文件再 `ray.init()` 撞上的是"上一次那个半死的节点"，
报错是 `Ray is already initialized`（第 04 章 §4.8）。这类失败有个特征：
**单跑通过、整套跑挂** —— 看到这个特征先查这里。
用 `ignore_reinit_error=True` 按下去是**掩盖**：真实原因通常是上一条路径没 shutdown。

**② 真实且常见的 flake 源：并行测试进程互相抢端口与 `/tmp/ray`。**

`pytest-xdist` 默认在**同一台机器**上开 N 个 pytest 进程。若每个进程都
`ray.init()`，同一台机器上就同时有 N 个 Ray 实例，抢三样东西：

| 抢什么 | 具体冲突 | 典型症状 |
|---|---|---|
| **端口** | GCS 默认 6379、dashboard 默认 8265，还有 raylet / object manager / node manager 等内部端口 | `Address already in use`；或连到了**另一个测试的集群**上 |
| **临时目录** | `/tmp/ray` 下每个 session 一个目录，`session_latest` 是指向最新 session 的软链 | 日志看串：`ray logs` 拿到别的测试的输出；spill 目录互相覆盖 |
| **`/dev/shm`** | 对象存储的开销按机器总量算 | 对象存储启动失败或提前 spill |

三个办法，按推荐顺序：

```bash
# 方案 A(推荐):Ray 集成测试不并行 —— 单独目录 / marker,串行跑
pytest tests/unit -n auto                          # 纯逻辑,随便并行
pytest tests/ray_integration -m ray -p no:xdist    # Ray 相关,串行

# 方案 B:每个 xdist worker 独立临时目录(必须在 ray 被 import 之前设好)
RAY_TMPDIR=/tmp/ray-$PYTEST_XDIST_WORKER pytest tests/ -n 4
```

```python
# 方案 C:在代码里给 dashboard 指定端口
# ⚠️ ray.init() **没有 port 参数**(有 dashboard_port)。
#    GCS 端口要在 ray start 侧用 --port 指定,不能从 ray.init 里传。
ray.init(dashboard_port=8270 + int(os.environ["PYTEST_XDIST_WORKER"][1:]))
```

> ✅ **关于方案 B 的更正说明**：本书早先写"`RAY_TMPDIR` 必须在导入 ray 之前设好，
> 在 fixture 里设**不保证生效**" —— **这过度悲观了，已按源码更正**：
> **在 fixture 里设 `os.environ["RAY_TMPDIR"] = ...` 再 `ray.init()` 是会生效的。**
>
> 依据（两段源码）：
>
> * `python/ray/_common/utils.py::get_default_system_temp_dir()` 直接
>   **每次调用都读 `os.environ["RAY_TMPDIR"]`**，**没有任何 `lru_cache` / 模块级缓存** ——
>   所以"什么时候设"只取决于"什么时候**调用**它"；
> * 调用点在 **`python/ray/_private/node.py::Node._init_temp()`**：
>   `self.temp_dir = ray._common.utils.get_default_ray_temp_dir()` ——
>   而 `Node.__init__` 是在 **`ray.init()` 的调用栈里**跑的。
>
> 结论：**只要"设环境变量"发生在"`ray.init()` 开始执行"之前**，就生效 ——
> fixture 里先设再 `init()` 完全满足这个条件。**没有"import 时必须已设好"这回事。**
>
> ⚠️ **但有两个真实的边界**，别把上面这条读成"怎么设都行"：
> ① 如果 `ray.init()` 是**连到一个已有集群**（`RAY_ADDRESS` 指向别处），
> `temp_dir` 来自**那个集群的 node info**，你本地设的 `RAY_TMPDIR` **不参与** ——
> 这条限制只对"本进程真的起集群"的场景成立；
> ② 顺序不能反：先 `ray.init()` 再改环境变量，对**已经建好的会话目录**无效
> （下一节 ② 的"共享 FS"事故就是这么来的）。
>
> 方案 C 同理（`dashboard_port`）：`ray.init()` **确实接受 `dashboard_port`**，
> 但**没有 `port` 参数**（GCS 端口只能在 `ray start` 侧用 `--port` 给）——
> 以及**一个节点到底还占多少内部端口**，请以 `ray start --help` 与你实际版本的行为为准。
> **只错开两个端口并不能保证一定不撞。**

**③ 每个测试都要有自己的干净世界。**

```python
def test_actor_state(ray_cluster):
    counter = Counter.remote()
    assert ray.get(counter.inc.remote()) == 1
    ray.kill(counter)          # 显式收掉,别留给下一个用例
```

三条纪律：**actor 用完就 `ray.kill`**（命名 actor 尤其小心，它会跨用例存活）；
**不依赖用例之间的执行顺序**；**给每个测试加超时**（`pytest-timeout`）——
卡死的 `ray.get()` 会把 CI 挂到天荒地老，而"卡死"在分布式里是常见失败模式，
不是异常。

**④ 别拿「把并发调到 1」冒充单节点集成测试。** 有些人靠 `local_mode=True` 图快，
但它**已经不能用了** 🟡 —— ⚠️ **准确说法不是"参数被移除了"**：
`local_mode` **至今仍留在 `ray.init()` 的签名里**
（`python/ray/_private/worker.py`：`local_mode: bool = False`），
只是**传 `True` 会立刻抛**：

```text
RuntimeError: `local_mode` is no longer supported. For interactive debugging
consider using the Ray distributed debugger.
```

（同一文件里 `if local_mode: raise RuntimeError(...)`。也就是说它从
"功能"退化成了"**一个专门用来报错的形参**" —— 这样旧代码能拿到一句
明确的迁移提示，而不是含糊的 `TypeError`。本书早先写成"已经被 Ray 移除了"，
不够准确，已更正；第 11 章 §11.7 同理。）

而且即便它还能跑，它也**没有并行度、没有真正的跨进程序列化、对象存储路径也不同**
—— 不能替代真单机集群上的验证。
正确做法是**起一个真的单机集群**（`ray.init(num_cpus=2)` 也算），
那才是 CI 里该跑的形状。

**⑤ 别假设有官方 pytest 插件。** 社区有若干把 `ray.init` 包成 fixture 的插件，
**本书未确认它们的维护状态与对 2.58 的适配**。上面那 10 行自己写就够了 ——
插件能省你 10 分钟，但会成为你升级时的另一个依赖。

### G.2.4 把故障注入测试放进 CI

第 10 章 §10.9 给了完整的注入清单与验收条件。这里只补**为什么它必须进 CI**：

**容错路径是唯一"不跑就会腐烂"的代码路径。** 它平时不在主流程上，
一旦底层行为变了（重试默认值、lineage 重建的边界、actor 重启语义），
只有真去杀一次进程才能发现。第 10 章举过一个典型：
`RAY_TASK_MAX_RETRIES=0` 会**连 lineage 重建一起关掉** ——
这种交互不测是发现不了的。

```python
# tests/test_fault_injection.py
@pytest.mark.fault
def test_worker_crash_is_retried(ray_cluster, tmp_path):
    marker = tmp_path / "crashed"
    @ray.remote(max_retries=2)
    def flaky():
        if not marker.exists():
            marker.touch(); os._exit(1)    # 模拟 worker 崩溃
        return "ok"
    assert ray.get(flaky.remote()) == "ok"
```

**mini-ray 对照**：mini-ray 有一条真实 Ray 没有的能力 ——
`_private.fault_injection` 可以**精确丢掉指定对象**，从而在测试里触发 lineage 重建
（第 10 章 §10.9）。真实 Ray 没有等价的官方 API，只能靠 kill 进程 / 删 pod
间接制造。**所以容错测试在 mini-ray 上比在真实 Ray 上更好写**，
这也是它作为教学实现的价值之一。

> ⚠️ `@pytest.mark.fault` 这类用例**别放进 PR 必过门禁**（慢且偶发不稳），
> 但**要放进 nightly**，且红了必须有人看。放进门禁然后被长期忽略，比不写还糟。

### G.2.5 一份完整的 GitHub Actions 工作流

三件事：测、造镜像推镜像、提交一个 RayJob。
**字段名以官方文档为准**（RayJob 的 CRD 字段本书只承诺第 17 章 §17.2 列过的那几个）。

```yaml
name: ray-app-ci
on:
  push: {branches: [main]}
  pull_request:

env:
  RAY_VERSION: "2.58.0"          # 与生产集群严格一致
  IMAGE: ghcr.io/${{ github.repository }}/ray-app

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v7
        with: {python-version: "3.11"}     # 与镜像里的 Python 一致
      # ⚠️ 用 -n auto 就必须装 pytest-xdist —— ray 本身不依赖它,
      #    漏了会让 CI 在这一步直接退出: error: unrecognized arguments: -n auto
      - run: pip install "ray[default,train]==${RAY_VERSION}" pytest pytest-timeout pytest-xdist
      - name: Unit tests (L0, 并行)
        run: pytest tests/unit -q -n auto
      - name: Ray integration (L1, 串行)
        env: {RAY_TMPDIR: "${{ runner.temp }}/ray"}
        run: pytest tests/ray_integration -q -p no:xdist --timeout=300

  build:
    needs: test
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    permissions: {contents: read, packages: write}
    steps:
      - uses: actions/checkout@v7
      - uses: docker/setup-buildx-action@v4
      - uses: docker/login-action@v4
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/build-push-action@v7
        with:
          context: .
          push: true
          tags: |
            ${{ env.IMAGE }}:${{ github.sha }}
            ${{ env.IMAGE }}:ray-${{ env.RAY_VERSION }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

  smoke-on-cluster:
    needs: build
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    environment: staging
    steps:
      - uses: actions/checkout@v7
      - run: mkdir -p "$HOME/.kube" && echo "${{ secrets.STAGING_KUBECONFIG }}" > $HOME/.kube/config
      - name: Submit a RayJob (L2 冒烟)
        run: |
          sed -e "s|__IMAGE__|${{ env.IMAGE }}:${{ github.sha }}|" \
              ci/smoke-rayjob.yaml | kubectl apply -f -
```

```yaml
# ci/smoke-rayjob.yaml —— L2 冒烟用的 RayJob
apiVersion: ray.io/v1
kind: RayJob
metadata:
  name: smoke-staging
spec:
  entrypoint: "python -m app.smoke --exit-after=120"
  shutdownAfterJobFinishes: true      # ← 不设,集群会留着(见 G.4.5)
  # rayClusterSpec / headGroupSpec / workerGroupSpecs 的结构
  # 以 KubeRay 官方文档为准,本书不逐字段展开
  rayClusterSpec:
    headGroupSpec:
      rayStartParams:
        num-cpus: "0"                 # head 不跑业务负载(第 17 章 §17.3)
      template:
        spec:
          containers:
            - name: ray-head
              image: __IMAGE__        # ← 由 CI 注入「commit 对应的不可变 tag」,不要用浮动 tag
```

**三个设计要点**：

1. **版本三处一致**：CI 的 `RAY_VERSION`、镜像 tag、集群里的 Ray 版本。
   不一致时的 bug 极难查（序列化协议、状态枚举、默认值都可能不同）。
2. **冒烟 Job 提交的是「commit 对应的不可变 tag」（`<repo>:<github.sha>`），不是 `:main`**（第 17 章 §17.6）。
   ⚠️ 本书早先这里写作"digest"，但上面那条 `sed` 注入的其实是 **tag**（`${{ github.sha }}`），
   二者不是一回事。要真正做到 §17.6 说的 `image: <repo>@sha256:...`，
   得改成取 `docker/build-push-action` 的 `outputs.digest` 再注入；
   用 `github.sha` 这种**不可变 tag** 也能达到"可追溯到某次 commit"的效果，
   只是**不是按内容寻址**。
3. **`shutdownAfterJobFinishes: true`** —— 不设，集群会活到有人手动删它。
   G.4.5 的复盘就是这个坑。

### G.2.6 镜像怎么造：三个决定

**① 底镜像选哪个。**

| 选择 | 说明 | 注意 |
|---|---|---|
| `rayproject/ray:<ver>` | 官方基础镜像 | 预装的 extras **与 tag 有关**，别假设；用 `pip show ray` 核对 |
| `rayproject/ray:<ver>-gpu` | 带 CUDA 运行时 | 驱动仍在宿主机上；镜像里的 CUDA 要与驱动兼容 |
| `rayproject/ray:<ver>-py3XX(-gpu)` | 指定 Python 版本 | CI 的 Python 版本要跟它对齐 |
| `rayproject/ray-ml` | 预装大量 ML 库 | **体积很大**，拉取慢会拖垮自动扩容（第 17 章 §17.8.4） |

⚠️ **本书未逐字核对 2.58.0 的全部镜像 tag**（`-cpu` / `-cu1xx` 这类后缀是否存在、
各 tag 的默认 Python 版本是什么），请以官方镜像文档与 Docker Hub 的实际 tag 为准。

**② `ray[default]` 还是 `ray[all]`。**

* `ray[default]`：dashboard、`ray.util.state`、日志聚合、CLI。
  **不带它就没有 State API**（第 17 章 §17.4），排障直接少一条腿 ——
  生产镜像的默认答案。
* `ray[train,data,serve,...]`：按需。
* `ray[all]`：**体积爆炸**。它把一堆你根本不用的库钉进镜像，
  而镜像越大，自动扩容越慢（§17.8.4）、冷启动越久、CI 越慢。
  除非真的全都要，否则逐项列。

**③ 依赖烧进镜像，还是走 `runtime_env`。** 这是本节唯一要算钱的决定，
也是与**附录 F §F.2.6**（`working_dir` 的成本模型）直接相连的一条：

| | 烧进镜像 | `runtime_env`（pip / `working_dir`） |
|---|---|---|
| 成本发生时机 | 构建时一次 | **每次** job 提交 / 每个节点启动 |
| 谁付 | CI 构建分钟数 + 镜像仓库 | 集群启动延迟 + 网络 |
| 可复现性 | **强**（digest 固定） | 弱（解析到的版本可能变） |
| 改一行代码 | 重建镜像（有层缓存则快） | 秒级生效 |
| 适合 | 重依赖、生产、版本敏感 | 开发迭代、轻量脚本、临时实验 |

**实践口径**：**重依赖（CUDA、torch、vLLM 那一层）烧进镜像，业务代码用
`runtime_env` 的 `working_dir` 迭代。** 理由：`working_dir` 是
**每次提交都重新打包上传**的（第 04 章 §4.7），拿它运几百 MB 依赖，
等于每次提交都重传一遍。

层缓存的写法（先依赖后代码，让改代码不破坏依赖层）：

```dockerfile
FROM rayproject/ray:2.58.0-py311-gpu
# ① 依赖层:requirements 不变就永远命中缓存
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
# ② 应用层:改代码只重建这一层
WORKDIR /app
COPY app/ /app/app/
ENV PYTHONPATH=/app
```

⚠️ **构建缓存要在 CI 里显式跨运行保留**（上面 workflow 的 `cache-from: type=gha`）。
没有这一步，每次 CI 都全量重建，"层缓存"只是纸面上的。

### G.2.7 镜像晋级与发布

```
PR   : 只跑 test(L0 单测 + L1 集成) —— 🔴 不构建镜像
main : test → build(:<git-sha> + :ray-2.58.0) → staging 冒烟(RayJob)
   → 生产:只改 CRD 里的 image
     （想按内容寻址就把 tag 换成 @sha256: 的 digest —— 见下面第 ③ 条）
```

> ⚠️ **PR 阶段没有"构建"这一步** —— 本书早先的流程图里画了一个"PR 构建(不推)"，
> 但**上面的 workflow 里并不存在这个环节**：`build` job 挂着
> **`if: github.ref == 'refs/heads/main'`**（`smoke-on-cluster` 同理），
> 所以 **PR 只跑测试，镜像只在 main 上构建并推送**。
> 这个差别有实际后果：**PR 里跑不到"镜像能不能构建成功"这个信号**，
> 所以 Dockerfile 的破坏性改动要到合并后才暴露。如果这一点对你是重要的，
> 再加一个 `if: github.event_name == 'pull_request'` 的 build-with-`push: false`
> job（**这正是早先那张图想表达、但代码没写的东西**）。

发布这一步**没有 Ray 特有的魔法**，全部复用第 17 章 §17.6 的四条纪律：
**固定 digest**（`image: <repo>@sha256:...`）；**显式设置
`RAY_GRACEFUL_SHUTDOWN_DRAIN_TIMEOUT_S`** 且 `terminationGracePeriodSeconds`
取它的 2 倍以上；**做过一次真实滚动升级演练并观察 5xx**；
**回滚路径预先验证**（旧 digest 还在仓库里）。

> **一处 Ray 特有的差别**：普通服务的滚动升级只需"新版本能起来"，
> Ray 集群还要"新版本能与**同集群里的其它角色**共存" ——
> 升级期间新旧 Pod 可能同时在集群里（KubeRay 的升级策略就是分批替换）。
> 如果新旧镜像里的 Ray 版本不同，这个窗口是**未定义行为**。
> **结论：一次升级只改一个 Ray 版本，且整个集群（head + 所有 worker group）
> 必须换到同一个版本。**

---

## G.3 Ray on Slurm

### G.3.1 为什么会有这一节

第 19 章把 Slurm 与 Kubernetes 并列放在"第 1 层"，但两者的实际可用性差别极大：
**大多数高校与研究机构的集群用 Slurm**（管理员不会为你装 KubeRay，你甚至没有
root）；**很多 HPC 中心同样如此**（各家调度器演进本书未逐条核对）。
而 RL / 后训练的主流框架几乎都长在 Ray 上：verl、SkyRL、OpenRLHF、NeMo-RL、
slime…（第 19 章 §19.8）。

于是就有一个很具体的需求：**在只有 Slurm 的集群上跑 Ray**。
这不是小众场景 —— verl 的官方教程里就有 Slurm 相关的部署说明
（**本书未逐字核对 verl 文档的当前版本与具体步骤**，请以 verl 官方教程为准）。

**能不能不用 Ray？** 能，但你要自己重写框架里的编排层。
现实答案是：**把 Ray 当成"用户态集群"，骑在 Slurm 的分配结果上。**

### G.3.2 核心模式：Slurm 给机器，Ray 管调度

```
Slurm 划给你 N 个节点(整机,有租期)
   → 你在这些节点上起一个 Ray 集群(head 1 个 + worker N-1 个)
   → Ray 在这块资源里做二级调度(任务、actor、放置组)
   → driver 跑完 → 拆掉 Ray → 把节点还给 Slurm
```

**这不是"Ray 替代 Slurm"，是"Ray 在 Slurm 内部做二级调度"。**
它和第 19 章 §19.7 的"两个调度器管同一批资源"是同一类问题，
只是 Slurm 这一侧更好办：它不做 pod 级调度，给的是**整节点 + 租期**，
边界比 K8s 清晰得多。

> ⚠️ **先看官方命令：`ray symmetric-run`（推荐路径）**
>
> 下面 G.3.3 给的是**手工 `srun` + `ray start --head &`** 的写法 ——
> 它仍然可行，而且在"你要自己控制每一步"时是必要的。
> 但 **2.50 起 Ray 官方给了 Slurm 的封装命令**，多数场景下更省事
> （⚠️ 本书早先写"2.49 起"，**这是错的**：2.48.0 / 2.49.0 / 2.49.1 的
> `python/ray/scripts/symmetric_run.py` 都是 **404**，该文件**最早出现在 2.50.0**。
> 上游 Slurm 文档里写的"Ray 2.49 and above"与代码不一致 —— 以代码为准）：
>
> ```bash
> # ⚠️ --address 必须写 head 节点的**主机名**，绝不能写 127.0.0.1（原因见下）
> HEAD=$(scontrol show hostnames "$SLURM_JOB_NODELIST" | head -1)
> srun --nodes=4 --ntasks=4 --gpus-per-node=8 \
>      ray symmetric-run \
>        --address="$HEAD:6379" \
>        --min-nodes=4 \
>        --num-cpus=32 --num-gpus=8 \
>        -- python -u train.py
> ```
>
> 🔴 **`--address` 写成 `127.0.0.1:6379` 会让整个集群起不来。**
> 这是本节最容易照抄错的一处：`symmetric_run.py` 在 `--min-nodes > 1` 时
> **会主动把所有 localhost 地址从本机 IP 列表里剔除**
> （`python/ray/scripts/symmetric_run.py:195-198`，原文注释
> *"Ban localhost ips if we are not running on a single node to avoid starting
> N head nodes"*），随后用 `is_head = resolved_gcs_host in my_ips` 判定谁是 head（:200）。
> 于是写 `127.0.0.1` 时**每个节点都判定自己不是 head**、全部退进 worker 分支，
> 谁都不去起 head —— 4 个 `srun` 任务会在超时后一起以
> `Timed out waiting for head node to start.` 失败。
> 官方 Slurm 指南的 `slurm-basic.sh` 用的正是
> `ip_head=$head_node:$port` 这一形态，可对照。
>
> 它帮你做四件事：**在每个节点上起 `ray start`**、**只在 head 上跑 entrypoint**、
> **`--min-nodes` 确保集群真的成形才开跑**、**跑完自动 `ray stop`**。
> 也就是说，G.3.3 那份脚本里最容易写错的三段（节点发现、等就绪、清理）它都替你做了。
>
> **`--min-nodes` 是这里最关键的一个参数**：不设它，entrypoint 可能在
> 只有部分节点起来时就开跑，于是放置组永远等不齐 —— 这正是 G.3.4 ⑤ 那类问题的根因。
>
> ⚠️ 本书**未逐版本核对 `symmetric-run` 的参数全集**（上面这份来自官方
> Slurm 用户指南的形态）；`--address` 的取值、是否需要 `srun` 的
> `--ntasks` 与节点数严格相等，请以 `ray symmetric-run --help` 为准。

### G.3.3 一份可用的 `sbatch` 脚本

下面这份把"节点发现 → 起 head → 起 worker → 等就绪 → 跑 driver → 清理"
全部串起来。**先在 `--nodes=2` 上试，再放大。**

```bash
#!/bin/bash
#SBATCH --job-name=ray-run
#SBATCH --nodes=4
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=64
#SBATCH --gres=gpu:8
#SBATCH --exclusive                 # 整节点独占:下面声明整机资源才成立
#SBATCH --time=04:00:00
#SBATCH --output=ray-%j.out

set -euo pipefail

# ── ① 节点发现:标准写法 ──────────────────────────────
mapfile -t NODES < <(scontrol show hostnames "$SLURM_JOB_NODELIST")
HEAD="${NODES[0]}"
PORT=6379
export RAY_TMPDIR="/tmp/ray-${SLURM_JOB_ID}"     # 节点本地盘!
export RAY_ADDRESS="${HEAD}:${PORT}"
echo "head = ${HEAD}; nodes = ${NODES[*]}"

# ── ② 无论如何退出,都要把 ray 进程收干净 ─────────────
cleanup() {
  echo "[cleanup] stopping ray on all nodes"
  srun --nodes="$SLURM_NNODES" --ntasks="$SLURM_NNODES" ray stop --force || true
}
trap cleanup EXIT

# ── ③ 每个节点一个 ray 进程(--block 占住这个 step,所以放后台)
srun --nodes="$SLURM_NNODES" --ntasks="$SLURM_NNODES" \
     --kill-on-bad-exit=1 --label \
     bash -c '
  set -euo pipefail
  if [ "${SLURM_NODEID}" -eq 0 ]; then
    exec ray start --head --port="'"${PORT}"'" \
      --node-ip-address="'"${HEAD}"'" \
      --num-cpus="${SLURM_CPUS_PER_TASK}" --num-gpus=8 \
      --temp-dir="${RAY_TMPDIR}" --block
  else
    exec ray start --address="'"${RAY_ADDRESS}"'" \
      --num-cpus="${SLURM_CPUS_PER_TASK}" --num-gpus=8 \
      --temp-dir="${RAY_TMPDIR}" --block
  fi
' &

# ── ④ 等集群就绪(不要用 sleep 猜) ────────────────────
for i in $(seq 1 60); do
  ray status --address="$RAY_ADDRESS" >/dev/null 2>&1 && break
  sleep 2
done
ray status --address="$RAY_ADDRESS"     # 打印一次资源视图,留档

# ── ⑤ 跑 driver(RAY_ADDRESS 已导出,driver 里 ray.init() 自动连上)
srun --nodes=1 --ntasks=1 -w "$HEAD" python train.py
# 退出时由 trap 调 cleanup
```

driver 里不需要任何特殊代码：

```python
import ray
ray.init()          # 读 RAY_ADDRESS,连到 Slurm 上那个 head
# ⚠️ ray.init() 与 ray.init(address="auto") 的差别 **不在于读不读 RAY_ADDRESS**
#    —— 两者**都读**(见下方说明)。真正的差别在"找不到集群时怎么办":
#      ray.init()            找不到 → **静默起一个本地单机集群**
#      ray.init(address="auto") 找不到 → **抛 ConnectionError**
#    所以在这个场景里, "auto" 反而**更安全**(不会偷偷起本地集群、掩盖配置错误)。
#    本书早先把这条写反了, 已按源码更正 —— 详见 G.3.4 的说明。

@ray.remote(num_gpus=1)
def work(x): ...

print(ray.get([work.remote(i) for i in range(32)]))
ray.shutdown()
```

### G.3.4 七个必须知道的坑

**① `--num-cpus` 必须显式设，别让 Ray 自己猜。**
Ray 的默认探测起点是 **`multiprocessing.cpu_count()`**，但**在能读到
cgroup / cpuset 限制时会向下修正** —— `get_num_cpus()` 会去读 cgroup v1 的
`cpu.cfs_quota_us` / `cpu.cfs_period_us`、cgroup v2 的 `cpu.max`、以及
`cpuset.cpus`，最后取 `min(cpu_quota, cpuset_num)`
（`python/ray/_private/utils.py::get_num_cpus()` → `_get_docker_cpus()`）。
不设的后果是 **Ray 可能按超出配额的核数超发任务**，这些进程在 cgroup 里互相抢 CPU ——
表现是"能跑但比预期慢好几倍"，且**不报错**。

⚠️ **本书早先这里有两处不准，已更正**：① 原文说"Ray 默认探测**整台机器**的核数"——
不准确，Ray 会在上面那种情况下向下修正；② 原文把"Ray 是否读 cgroup v2 配额"
标为**未确认** —— 现在可以确认：**会读**（含 cgroup v2 的 `cpu.max`）。
但这**不等于在 Slurm 下就能省略 `--num-cpus`**：Ray 只认 cgroup/cpuset，
而 **Slurm 是否把这些限制写进你进程可见的 cgroup 取决于 `cgroup.conf` 与
`--cpus-per-task` 的配置**，并不保证。
所以：**显式传 `--num-cpus`（以及 `--num-gpus`、`--memory`）**，
取值 ≤ Slurm 实际分配的量。用了 `--exclusive` 拿到整节点时，整机核数才是对的。
各参数的确切单位与语义（例如 `--memory` 是不是字节）**以 `ray start --help` 为准**。

**② `/tmp/ray` 不能落在共享文件系统上。** HPC 的典型布局是：
**`/tmp` 节点本地，`$HOME` 共享（NFS/Lustre）**。两种事故：

* 把 `RAY_TMPDIR` 指到共享 FS（如 `$HOME/ray`）：N 个节点同时往同一处写
  session 目录 / 日志 / spill 文件。**具体哪些文件会真的互相覆盖，本书未逐条核对**，
  但"多个节点共用一个 `session_latest` 软链"在原理上就不成立 —— 别试。
* 干脆不设、都默认 `/tmp/ray`：如果 `/tmp` 恰好也是共享的，后果同上。

**稳妥做法**：`--temp-dir` 显式指向 `$SLURM_JOB_ID` 命名的**节点本地**路径，
并在 `trap` 里删掉。否则这些目录会**一轮一轮堆在计算节点上**吃满磁盘
（第 17 章 §17.8.1 是同一种事故的另一副面孔）。

**③ 网卡：Ray 绑哪张、NCCL 走哪张，是两个独立问题。**
多网卡节点（管理网 + InfiniBand/RoCE）上 Ray 可能绑错：

* **控制面**：用 `--node-ip-address=<IP 或主机名>` 明确指定。
  集群的 hostname 不能从其它节点解析时，也必须这么写。
* **数据面（NCCL）**：Ray **不管**这件事，要用 `NCCL_SOCKET_IFNAME`、
  `NCCL_IB_HCA` 之类的变量指定。Ray 自己的对象传输在某些配置下也有
  网卡选择问题（**具体变量与默认行为本书未确认**）。

**最快的验证方法**：`ray status` 看节点地址是不是你以为的那个；
再在 driver 里打印 `ray.get_runtime_context().get_node_id()` 对照节点清单。

**④ 每个节点一个 Ray 进程，别用 `srun` 的默认分配。**
`--ntasks-per-node=1` + `ray start` 在节点内部按 `--num-cpus` 开 worker，
这个分工必须清楚。如果 `srun` 在每节点起了多个 task、每个都跑 `ray start`，
你会得到一堆互相打架的 raylet。

**⑤ 清理必须写在 `trap` 里。** Slurm 在作业结束时**不保证**回收你 fork 出去的
后台进程。漏掉 `ray stop` 的后果：残留 raylet/GCS 仍占着 6379 之类的端口，
**下一个作业随机失败**。**"今天能跑、明天同样的脚本跑不了"在这类集群上，
十有八九是残留进程。**

**⑥ 多租户：两个用户共用同一批节点时，端口必须错开 —— 否则必然冲突。**
这是官方在 Slurm 章节里**专门警告**过的一条（`doc/source/cluster/vms/user-guides/
community/slurm.rst` 的 **"SLURM networking caveats"**），也是最容易被忽略的：

> 考虑两个用户，如果他们**同时**在共享集群上各提交一个用 Ray 的 Slurm 作业，
> **两人各起一个 head 节点**。Ray 会给若干服务分配内部端口，
> 而**第一个 head 一起来就把这些端口占住**，第二个 head 绑不上 → 启动失败。
> 要避免冲突，**必须手工给两套集群指定不重叠的端口范围**。

需要错开的端口（官方清单）——**每个都是一次 `ray start` 的参数**：

| 用途 | 参数 |
|---|---|
| 所有节点通用 | `--node-manager-port` / `--object-manager-port` / `--min-worker-port` / `--max-worker-port` |
| 仅 head 节点 | `--port`（GCS）/ `--ray-client-server-port` / `--redis-shard-ports` |

```bash
# 用户 A:6379 / 6700 / 6701 / 10001 / 6702 / 10002-19999
# 用户 B:用完全不重叠的另一段,例如
srun --nodes=1 --ntasks=1 -w "$head_node" \
    ray start --head --node-ip-address="$head_node_ip" \
        --port=6380 \
        --node-manager-port=6800 \
        --object-manager-port=6801 \
        --ray-client-server-port=10101 \
        --redis-shard-ports=6802 \
        --min-worker-port=20002 \
        --max-worker-port=29999 \
        --num-cpus "${SLURM_CPUS_PER_TASK}" --block &
```

> 🔴 **`ray symmetric-run` 不适用于多租户** —— 官方原文：
> *"we don't use symmetric-run here because it does not currently work in
> multi-tenant environments."* 所以 G.3.3 推荐的 `ray symmetric-run`
> 只在**你独占这批节点**时用；**抢共享集群时必须回到手工
> `ray start --head` + 逐个指定端口**这条路（也就是上面这种写法）。
>
> ⚠️ 别指望"错开 6379 就够了"：**`--min-worker-port` / `--max-worker-port`
> 那一段 worker 端口范围同样会撞**，而且它跨度大、更容易和别人的区间叠上。

**⑦ `ray.init(address="auto")` 与 `ray.init()` 的差别，不在"读不读 `RAY_ADDRESS`"。**
这条在 G.3.3 的代码注释里提到过，展开说清楚：

| | `ray.init()`（不传 address） | `ray.init(address="auto")` |
|---|---|---|
| 读 `RAY_ADDRESS`？ | ✅ **读** | ✅ **也读** |
| 找不到已有集群时 | **静默起一个本地单机集群** | **抛 `ConnectionError`** |
| 额外行为 | —— | 还会连上**任何**在跑的本地 GCS 实例（即使是 `ray start` 之外起的），保持向后兼容 |

源码依据（两处都指向"同一分支"）：

* `python/ray/_private/worker.py`：
  `if address_env_var and (address is None or address == "auto"): address = address_env_var`
  —— **`None` 和 `"auto"` 走的是同一个分支**，两者都优先采用 `RAY_ADDRESS`。
* `python/ray/_private/services.py::canonicalize_bootstrap_address`：
  `if addr is None or addr == "auto": addr = get_ray_address_from_environment(...)`
  —— 同样合并处理；差别只出现在**兜底**那一段
  （`get_ray_address_from_environment` 末尾）：
  `addr is None` → `return None`（调用方起本地集群），
  `addr == "auto"` → `raise ConnectionError("Could not find any running Ray instance. ...")`。

> 🔴 **结论与直觉相反：在"连集群"这个场景里，`address="auto"`
> 反而比裸 `ray.init()` 更安全** —— 它**不会静默起一个本地集群**把你的
> 配置错误盖过去。裸 `ray.init()` 在 `RAY_ADDRESS` 没设好时"能跑"，
> 但那跑的是一个只有本机的假集群，任务数、资源视图全都对不上。
> ⚠️ 本书早先把这条写成"`ray.init()` 读 `RAY_ADDRESS`、`auto` 在本机找实例"
> ——**前半句不完整**（`auto` 也读），**已按源码更正**。

### G.3.5 多节点上的依赖安装

`runtime_env` 的 pip 安装**在每个节点上各跑一次**。在 Slurm 场景下这会变成
真实的瓶颈甚至故障源：安装目标若落在**共享 FS**（用户 site-packages 在 `$HOME`），
N 个节点**并发写同一份目录** → 半装状态、损坏的 wheel、随机 ImportError；
即使不冲突，N 个节点同时从 PyPI 拉同一批包也很慢，而**这段时间作业计时在走**。
（三种安装方式的通用取舍见**附录 F §F.2.7** 的决策表；下面这一张是**多节点**视角的补充。）

| 做法 | 说明 | 代价 |
|---|---|---|
| **① 预建共享环境** | 提交作业**之前**，在共享 FS 上串行建好 venv/conda。作业里只 `source activate` | 多一步（可写成另一个 sbatch 作业） |
| **② 容器** | HPC 常用 Apptainer / Singularity（Docker 需 root，通常没有）。依赖烧进 `.sif`，作业里 `singularity exec --nv` | 需要镜像构建流程；`--nv` 细节以站点文档为准 |
| **③ `runtime_env` 现装** | 只在依赖少、节点少、且 `PIP_CACHE_DIR` 指向共享缓存时可用 | 每次作业付一次安装时间；并发写共享 FS 有风险 |

```bash
# ①的示意:先用一个短作业把环境建好(串行、单节点)
srun --nodes=1 --ntasks=1 --time=01:00:00 bash -c '
  python -m venv /shared/envs/ray258
  source /shared/envs/ray258/bin/activate
  pip install --no-cache-dir "ray[default,train]==2.58.0"
'
```

> **一条经验**：只要共享 FS 出现在写入路径上，就问一句
> "**会有几个进程同时写它**"。Ray 在 Slurm 上出的问题，一大半能归到这句话。

### G.3.6 其他做法与本书的立场

* **社区封装脚本**：有若干把上面这套 `start/stop` 序列包成模板的项目。
  **本书未核对任何具体项目的维护状态与适用版本，因此不点名推荐**，
  G.3.3 那份可以直接当起点。
* 🔴 **`ray up` + Slurm provider：可以下确定结论了 —— 2.58 的 autoscaler
  没有 Slurm provider**（本书早先标"未确认"，现已核实）。
  依据：`python/ray/autoscaler/_private/providers.py` 的 provider 注册表
  **完整清单**只有七个 ——
  **`local` / `aws` / `gcp` / `azure` / `vsphere` / `kuberay` / `aliyun`**；
  在 `python/ray/autoscaler/` 全目录 `grep -ri slurm` **0 命中**。
  所以**不要去找"Slurm 的 `cluster.yaml` 写法"—— 它不存在**，
  G.3.3 那份手工 `sbatch` + `ray start` 才是这条路。
  ⚠️ 顺带澄清另一件事：**`ray up` 与 `cluster.yaml` 本身并没有被弃用** ——
  `ray up` 在 `python/ray/scripts/scripts.py` 上挂的是 **`@PublicAPI`**（不是
  `@Deprecated`），VM 集群这条路仍然受支持；**被弃用的是 `ray client`，不是 `ray up`**。
  这两件事常被混为一谈。
  ⚠️ 即便哪天有人补了 Slurm provider，它也会和 K8s 上的"双 autoscaler"
  是同一类问题（第 19 章 §19.7）：**Slurm 才是决定你有几台机器的那个调度器**。
  在 Slurm 上，**"申请固定 N 节点 + 让 Ray 在内部调度"通常更简单也更稳**。
* **抢占与重排队**：集群若开了抢占 / requeue，作业可能被整体杀掉，
  重跑时 Ray 的一切从头开始。**把检查点写在共享 FS 上**（第 10 章 §10.8），
  不要指望 Ray 的状态能活过 Slurm 的 SIGTERM。
* **`ray stop --force` 的语义**、以及 Slurm 的 cgroup / affinity 与 Ray
  资源记账的**精确**交互方式，**本书未确认**，需要在你自己的站点上实测。

---

## G.4 云上部署与成本

### G.4.1 三种形态

| 形态 | 谁运维控制面 | 弹性 | 适合 | 不适合 |
|---|---|---|---|---|
| **托管**（Anyscale 类） | 厂商 | 内置 | 不想自己运维、预算换人力 | 预算敏感 / 数据合规限制 / 供应商风险（第 17 章 §17.1） |
| **自建在 K8s**（EKS/GKE/AKS + KubeRay） | 你 | Ray autoscaler + K8s Cluster Autoscaler | 已有 K8s 平台团队 | 团队没人同时懂 K8s 与 Ray 排障 |
| **每作业临时集群** | 你（但很薄） | 作业级（起了就删） | 批任务、训练作业 | 需要常驻服务（Serve） |

第三种容易被忽略，但**成本上常常最优**：只在有活时存在的集群，
不存在"忘了关"这个失败模式（对比 G.4.5）。代价是每次付集群启动 + 镜像拉取时间。

### G.4.2 节点池设计

```
head 池(按需 / on-demand)   小规格、无 GPU、常驻;它挂了整个集群就挂了(§17.3)
CPU worker 池(spot / 抢占)  数据处理、预处理、调参
GPU worker 池(spot 为主 + on-demand 兜底)  训练/推理;靠容错吸收被回收的节点
```

**① head 用按需、worker 用 spot。** head 被回收一次等于整个集群重启；
worker 被回收一次等于"少了一台机器"，Ray 会重试任务、重建对象（第 10 章）。
两者的代价差一个数量级。

**② spot 回收 = 节点死亡，要按"节点死亡"来设计容错。**

| spot 回收后的后果 | Ray 的反应 | 你要做的 |
|---|---|---|
| 该节点上的 task 中途死亡 | 自动重试（`max_retries`，默认 3） | 任务**必须幂等**（第 10 章） |
| 该节点上驻留的对象丢失 | 有 lineage 则重建；**`ray.put` 的没有血缘，丢了就是丢了** | 别把唯一副本放在 `ray.put` 里 |
| 该节点上的 actor 死亡 | 有 `max_restarts` 则重启，**但状态重置** | 状态外置到检查点/共享存储 |
| 该节点是 head | **整个集群没了** | head 别用 spot；head 上别跑业务负载 |

⚠️ **一个常见误解**：以为"Ray 有容错，所以 spot 可以随便用"。
Ray 的容错保的是**任务**，不保**你的状态**。用 spot 省钱的前提是
**任务幂等 + 状态可重建**，否则省下的钱会以"数据不一致"的形式还回去。

**③ 放置组控制 GPU 落在哪。** 昂贵的多卡节点上，`STRICT_PACK` 把一组 actor
钉在同一节点（避免跨节点通信），`SPREAD` 则用来摊开故障域。见第 08 章 §8.5。

```python
from ray.util.placement_group import placement_group
# 8 卡一个 bundle:要么整机拿到,要么不启动(避免"半台机器"的浪费)
pg = placement_group([{"GPU": 8, "CPU": 16}], strategy="STRICT_PACK")
ray.get(pg.ready())
```

### G.4.3 成本模型：Ray 的收益是"利用率"，不是"单价"

先把话说清楚：**Ray 不会让 GPU 每小时更便宜。** 云上的 `$/GPU·小时` 是云厂商
定的，跟你的调度器无关。Ray 能改变的是**分母**：

```
有效成本 = 租到的 GPU·小时 × 单价 / 真正产出有用工作的 GPU·小时
         = 单价 / 利用率
```

**一个算例**（数字是示意的，请换成你自己账单上的数）：

```
假设:一台 8 卡节点 $32/小时 → $4 / GPU·小时

场景 A 利用率 40%:要交付 100 GPU·小时有用工作
  → 必须租 100 / 0.40 = 250 GPU·小时 → 250 × $4 = $1,000
场景 B 利用率 75%:
  → 必须租 100 / 0.75 = 133 GPU·小时 → 133 × $4 = $533

倍数:1000 / 533 ≈ 1.88 —— 接近 2 倍
```

**所以"Ray 值不值"的正确问法是：它能不能把我的利用率从 X 提到 Y？**
在"多个小任务、有状态组件、异构资源"这类负载上通常能 ——
手工脚本的典型形态是"每人占一张卡、大部分时间在等人和等 IO"。
在"8 卡 DDP 跑满一个大任务"这类负载上通常不能（第 19 章 §19.4：直接 torchrun）。

**反例必须写出来：过度配置的 Ray 集群比一台塞满的单机更贵。**

```
一台塞满的单机(8 卡,利用率 85%):有效成本 ≈ 单价 / 0.85 ≈ 1.18 × 单价
一个 Ray 集群(16 卡,利用率 30%):多租了 8 张卡 + 一个 head 节点
  → 有效成本远高于上面那台单机
```

Ray 集群**本身有固定开销**：head 节点、GCS、dashboard、每个节点常驻的 raylet
与对象存储。这些在小集群上占比很高。**"上了 Ray 就一定更省"是个错误前提** ——
它只在"利用率确实被提高了"的时候成立。

### G.4.4 Autoscaler 能帮什么、不能帮什么

机制见第 08 章 §8.8，KubeRay 侧的配置与已知坑见第 17 章 §17.3
（含那个"`priority` 没透传"的 bug）。这里只讲成本相关的能力边界：

| 它能做的 | 它做不到的 |
|---|---|
| 空闲时缩容（`idleTimeoutSeconds`，§17.3） | **缩到 0** —— 除非 `min_workers` / `minReplicas` 就是 0 |
| 按待处理任务扩 worker | 保证拿到机器（云配额、K8s 节点池上限、Slurm 都不归它管） |
| 优先级感知地选节点类型 | 知道 K8s Cluster Autoscaler 在想什么（§17.8.2） |
| 记录伸缩决策 | 替你发现"这个集群已经闲置三天了" |

**结论**：autoscaler 是**效率工具**，不是**成本控制工具**。
成本控制靠三件它管不了的事：**`min` 设成 0**、**闲置告警**、**强制 TTL**。

### G.4.5 一次"闲置 GPU 集群"复盘

> 下面的数字是**示意值**，用来把账算清楚；它是把多个团队常见做法合在一起的
> **合成案例**，不是某一次具体事故的记录。

**症状**：月末对账时发现一个 GPU 集群连续计费了若干天，
而这段时间**没有任何作业跑在上面**。

```
D0  一个训练作业通过 RayJob 提交,跑了 6 小时,正常结束
    —— 但 CRD 里没设 shutdownAfterJobFinishes
D0  作业结束后:worker pod 被缩到 minReplicas
    head 仍然活着(GCS + dashboard 继续占一台按需实例)
D1~Dn 没人看 dashboard,没人看账单,集群安静地计费
Dn  有人在云控制台按 tag 排序,才发现这台机器
```

**机制（三条叠加，缺一不可）**：

1. **`shutdownAfterJobFinishes` 不设**，RayJob 跑完不会删集群
   （第 17 章 §17.2 的字段清单里有它；默认行为请以 KubeRay 文档为准）；
2. **autoscaler 缩不到 0**：`minReplicas` 只要 ≥ 1 就永远有一台在计费，
   head 更是默认常驻；
3. **没有人为"闲置"负责**：没有告警、没有 TTL、没有成本归属。

**预防清单**（一个下午能做完）：

```
[ ] 批任务的 RayJob 一律设 shutdownAfterJobFinishes: true
[ ] 评估场景能不能用"每作业临时集群"(G.4.1 第三种形态)
[ ] 批任务场景把 worker group 的 minReplicas 设成 0
[ ] head 池的规格单独审一遍:GPU 从 head 上摘掉
[ ] 所有云资源打 tag(team / project / owner),账单能按 tag 归因
[ ] 给 RayCluster 的存活时间加告警(例如"创建超过 N 小时且无任务")
    —— 具体实现(定时扫 CRD 还是 K8s 侧 TTL 机制)以你所用组件的
       官方文档为准,本书不给字段名
[ ] 把"每 GPU·小时产出"做成看板指标,而不是只看利用率
```

**为什么最后一条重要**：利用率 100% 也可能是"在跑一个没人要的实验"。
利用率是**必要**指标，不是**充分**指标。

---

## G.5 编排集成（Airflow / Prefect / 自研调度器）

### G.5.1 位置关系：Ray 在任务**里面**，不是编排器**上面**

这是最容易搞反的一件事。第 19 章那张分层图里，Airflow 这类编排器解决的是
"**跨系统、按时间、跨天**"的问题：依赖哪些上游表、几点跑、失败了重试几次、
谁来收报警。Ray 解决的是"**这一次运行内部**怎么并行"。

```
Airflow(或 Prefect / Dagster / 自研调度器)
   DAG: 抽取 → [ Ray 作业 ] → 校验 → 发布
                     ↑ 这一格里面才是 Ray:任务、actor、放置组
   编排器管:跨系统、跨天、重试与告警
```

**为什么不反过来（用 Ray 做编排器）？**

* **Ray Workflows 已经被删了**（第 21 章 §A.10：2.44 弃用、之后移除，
  PR #53612，最后一个包含它的版本是 `ray==2.47`）。Ray 生态里
  **没有**官方的"工作流引擎"这一层了。
* Ray 的 task/actor 图**可以**表达 DAG，但它没有跨天调度、没有日历、
  没有回填（backfill）、没有"上一次跑挂了今天怎么补"的语义。
* 你当然可以自己在 Ray 上写一个调度器 —— 但先看 G.5.4 里什么情况下才值得。

### G.5.2 标准接法：提交 + 轮询

Airflow 的 TaskFlow 风格，一个任务函数包住 `JobSubmissionClient`：

```python
# dags/ray_tasks.py
import time
from airflow.decorators import task
from ray.job_submission import JobSubmissionClient, JobStatus

# ⚠️ KubeRay 的 head Service 名是 "<集群名>-head-svc"，且跨命名空间访问要带 namespace
RAY_DASHBOARD = "http://<cluster-name>-head-svc.<namespace>.svc.cluster.local:8265"
# 同命名空间内可以简写： http://<cluster-name>-head-svc:8265

def _client() -> JobSubmissionClient:
    # 若集群开了 RAY_AUTH_MODE=token(第 17 章 §17.5),这里要带上 token
    return JobSubmissionClient(RAY_DASHBOARD)

@task(retries=1, retry_delay=60)
def run_ray_job(entrypoint: str, working_dir: str = "./app") -> str:
    client = _client()
    job_id = client.submit_job(
        entrypoint=entrypoint,
        runtime_env={"working_dir": working_dir},
        entrypoint_num_cpus=1,      # entrypoint 自身资源,别在里面做重活(§4.7)
    )
    while True:                     # 轮询到终态
        status = client.get_job_status(job_id)
        if status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.STOPPED):
            break
        time.sleep(15)
    if status != JobStatus.SUCCEEDED:
        # 把 Ray 侧日志带出来,否则 Airflow 的报错里什么都没有
        logs = client.get_job_logs(job_id)[-4000:]
        raise RuntimeError(f"ray job {job_id} ended as {status}\n{logs}")
    return job_id
```

DAG 里就是普通的任务依赖：
`run_ray_job.override(task_id="embed")(entrypoint="python -m app.embed --date {{ ds }}")`，
再接一个 `index` 任务 —— **注意每个 Ray 作业是 DAG 里的一个格子，不是整张 DAG**。

**Prefect / Dagster / 自研调度器**是同一个形状：**提交 + 轮询到终态 + 拿日志**，
差别只在这三件事的 API 名字。

⚠️ **本书未确认**是否有官方维护的 Airflow provider（或 Prefect / Dagster 集成）。
要用请以对应项目的官方文档为准；**自己写一个 30 行的适配器通常比引入
第三方 provider 更可控。**

### G.5.3 你失去了什么、得到了什么

| 得到 | 失去 / 要小心 |
|---|---|
| 跨系统的依赖图（上游表好了才跑 Ray） | **两套重试语义打架**：Airflow 的 `retries` 不知道 Ray 的 `max_retries` |
| 日历、回填、跨天补跑 | **`ray job submit` 是幂等的反面**：重提就是重跑一遍 |
| 统一的告警与 SLA 监控 | **调度器 worker 被长时间占用**：轮询式任务会占着 slot 几小时 |
| 审计："谁在什么时候触发了什么" | **多一个网络依赖**：调度器要能访问 8265（以及 token） |
| 一次性重跑整条链路（clear task） | **失败信息被稀释**：Ray 侧的真实错误藏在日志里 |

**"两套重试语义"是这里最需要设计的一件事**：

```
Airflow 重试 → 再 submit 一次 → Ray 里又跑一份
                                 ↑ 第一次其实只差最后一步没跑完的话,
                                   你就跑了两份(除非任务幂等)
```

处理办法（按推荐顺序）：

1. **任务幂等 + 业务键去重**：把 `{{ ds }}` / `run_id` 拼进 entrypoint，
   任务内部先检查"这个 run 是否已经写过了"。
   ⚠️ **`submission_id` 是 `submit_job` 的正式参数**（`job_id` 是它的弃用别名），
   而且**服务器在同一 `submission_id` 被重复使用时会直接报错** ——
   也就是说它**不会**帮你做幂等，但能帮你**发现**重复提交。
   （本书早先把这一项标成"未确认、不要依赖"，那是过头的：
   它存在且行为明确，只是**语义是"拒绝重复"而不是"去重"**。）
2. **Airflow 侧不重试，把重试交给 Ray**（`max_retries`），
   只在 Ray 的 job 状态为 **FAILED** 时才让 Airflow 失败。
3. **长任务用异步 + sensor**：提交后立刻返回 job_id，
   用独立的 sensor 任务去轮询，不长期占住 scheduler 的 slot。

> 顺带一个**排障坑**：轮询任务的超时要设得比作业预计时长更长。
> 否则会出现"Airflow 任务超时失败，但 Ray 作业还在跑" —— 而它跑完还会
> **覆盖**上游产物，让下一次跑出来的结果对不上。
> **要么等到底，要么在超时时显式 `client.stop_job(job_id)`。**

### G.5.4 什么时候不该用外部编排器

三个问题，任意一个答"是"，就该优先考虑 Ray 自己的原语：

| 情况 | 用什么 |
|---|---|
| 链路每一步都是 Ray 任务、不需要跨系统依赖？ | **直接写 Ray 代码**。`@ray.remote` 之间的依赖就是 DAG（第 04 章 §4.6 套路 4） |
| 需要的是"常驻服务"而不是"每天跑一次"？ | **Ray Serve**（第 15 章）+ Jobs 跑批。用 Airflow 去"保活"一个服务是错配 |
| 触发条件是"上一个作业跑完了"而不是"到点了"？ | **Jobs API 串起来**：A 的结尾提交 B，或用一个 driver 编排（注意 driver 挂了链路就断） |

**一个务实的混合形态**（很多团队最后收敛到的样子）：

```
Airflow  管"什么时候":每天 03:00 触发、失败告警、跑完通知
  └─ Ray Job 管"怎么跑":真正的重活(数据、训练、推理),内部用 Ray 原语编排
       └─ Ray Serve(长期驻留)管"活着":在线服务由 K8s 管生命周期,不进 DAG
```

也就是：**编排器管"什么时候"，Ray 管"怎么跑"，K8s 管"活着"。**

---

## G.6 小结

* **CI/CD 的三点不同**：测试是集群形状的、依赖有两套（镜像 + `runtime_env`）、
  "本地能跑"几乎不算信号。用三层金字塔：**L0 纯逻辑（占大头）→
  L1 单机 `ray.init()`（主战场）→ L2 多节点冒烟（只放冒烟）**。
* **pytest 的两个真实坑**：`ray.shutdown()` 漏了会导致"单跑通过、整套跑挂"；
  并行测试进程会抢**端口、`/tmp/ray`（`session_latest` 软链）、`/dev/shm`**，
  最稳的解法是 **Ray 集成测试不并行**。`RAY_TMPDIR` 靠 `get_default_system_temp_dir()`
  **无缓存、在 `ray.init()` 调用栈里才求值**，所以**在 fixture 里设也会生效**
  （⚠️ 本书早先写"必须在导入 ray 之前设、在 fixture 里不保证生效"，**已更正**；
  见 G.2.3 的方框）。`local_mode` **参数还在签名里，只是传 `True` 抛 `RuntimeError`**
  —— 准确说法不是"已被移除"。
* **故障注入测试应该进 CI**（nightly 即可）：容错路径是"不跑就腐烂"的代码。
  mini-ray 的 `_private.fault_injection` 能精确丢对象，比真实 Ray 更好测。
* **镜像**：重依赖烧进镜像、业务代码走 `working_dir`；`ray[default]` 是生产底线；
  固定 digest；CI 缓存要跨运行保留；**一次升级只改一个 Ray 版本，整集群同版本**。
* **Ray on Slurm = 用户态二级调度**：`sbatch` 拿整节点 →
  `scontrol show hostnames` 做节点发现 → head 上 `ray start --head` →
  其余节点 `ray start --address` → 跑 driver → `trap` 里 `ray stop`。
  **七个坑**（第五轮补齐 ④；第六轮补 ⑥⑦）：
  ① **`--num-cpus` 必须显式设**、
  ② **`RAY_TMPDIR` 必须节点本地**、
  ③ **网卡要显式指定**、
  ④ **每个节点只起一个 Ray 进程** —— 别让 Slurm 的默认分配把同一个节点
     派给多个任务，那样会出现多个 raylet 抢同一份资源；
  ⑤ **清理必须写在 `trap` 里**（"今天能跑明天不能跑"十有八九是残留进程）；
  ⑥ **多租户必须错开端口段** —— 两人共用节点同时起 Ray 必然撞端口，
     要手工逐个给 `--node-manager-port` / `--object-manager-port` /
     `--min-worker-port` / `--max-worker-port` / `--port` /
     `--ray-client-server-port` / `--redis-shard-ports` 指定不重叠的值，
     而且**`ray symmetric-run` 不适用多租户**（官方明说）；
  ⑦ **`ray.init(address="auto")` 也读 `RAY_ADDRESS`** —— 它与裸 `ray.init()`
     的差别只在"找不到集群时"：前者抛 `ConnectionError`，后者**静默起本地集群**，
     所以**"auto" 在这个场景反而更安全**。
  多节点依赖优先用"预建共享环境"或容器。
* **`ray up` + Slurm provider 不存在**（2.58 的 provider 只有
  `local`/`aws`/`gcp`/`azure`/`vsphere`/`kuberay`/`aliyun`），
  但 **`ray up` / `cluster.yaml` 本身没被弃用**（`@PublicAPI`）——
  别把这两件事混为一谈。
* **成本**：收益是**利用率**不是单价（40% → 75% 约等于成本减半）；
  **反例同样成立** —— 过度配置的 Ray 集群比一台塞满的单机更贵。
  autoscaler 是效率工具，成本控制靠 `min=0` + 闲置告警 + 强制 TTL。
* **编排集成**：Ray 在任务**里面**，不是编排器**上面**（Ray Workflows 已移除，
  第 21 章 §A.10）。标准接法是"提交 RayJob / 调 `JobSubmissionClient` + 轮询到终态"。
  最需要设计的是**两套重试语义**：Airflow 重试会再提交一份 Ray 作业。

回到第 19 章那句话：**Ray 在第 3 层，它上面和下面都还有东西。**
这份附录补的就是上面（CI/CD 与编排器）和下面（Slurm 与云）这两层 ——
把这两层想清楚，第 17 章那份"能上线"的清单才真正闭合。

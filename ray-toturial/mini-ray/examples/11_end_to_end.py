"""11 · 端到端实战:一条「数据 → 批推理 → 训练 → 在线服务」的流水线。

运行::

    python examples/11_end_to_end.py

这是把前面所有示例的能力拼起来的一个完整系统。它模拟的正是真实 AI 团队的
日常工作流:

.. code-block:: text

    ① 数据准备        分片读取 → 预处理(任务链,依赖自动并行)
         │
    ② 批量推理        ActorPool + 常驻模型(避免每个任务重载)
         │
    ③ 训练            放置组 + 参数服务器 + 多个 worker(资源原子性预留)
         │
    ④ 在线服务        一个 actor 作为推理端点(有状态、模型常驻)
         │
    ⑤ 可观测性        State API + timeline(看清每一步的并行度)
         │
    ⑥ 容错演练        注入一次 worker 崩溃,验证重试与 lineage 重建

对应到真实 Ray 的组件:

    ① Ray Data(`read_*` + `map_batches`)          ② Ray Data LLM / ActorPool
    ③ Ray Train(+ Tune)                           ④ Ray Serve
    ⑤ `ray.util.state` + `ray.timeline`           ⑥ 同样的容错机制

**换到真实 Ray 只需要把 import 换成 `import ray as ray`** —— 用到的 API
(`@ray.remote` / `ray.get` / `ray.put` / `ray.wait` / 放置组 / ActorPool /
`state` / `timeline`)全部同名同语义。
"""

from __future__ import annotations

import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import miniray as ray  # noqa: E402
from miniray import state  # noqa: E402
from miniray.util import ActorPool  # noqa: E402
from miniray.util.placement_group import placement_group, remove_placement_group  # noqa: E402
from miniray.util.scheduling_strategies import PlacementGroupSchedulingStrategy  # noqa: E402

# ===========================================================================
# ① 数据准备:分片 → 预处理(任务链表达依赖)
# ===========================================================================


@ray.remote
def make_shard(shard_id: int, rows: int = 200):
    """模拟「从数据湖读一批原始数据」。真实场景里这里是 ray.data.read_parquet。"""
    rng = random.Random(shard_id)
    return {
        "shard": shard_id,
        "rows": [
            {"text": f"doc-{shard_id}-{i}", "label": rng.randint(0, 1)} for i in range(rows)
        ],
    }


@ray.remote
def preprocess(shard: dict) -> dict:
    """文本清洗 + 特征化(这里用长度当特征)。"""
    tokens = [len(row["text"].split("-")) for row in shard["rows"]]
    labels = [row["label"] for row in shard["rows"]]
    return {
        "shard": shard["shard"],
        "features": tokens,
        "labels": labels,
        "count": len(tokens),
    }


@ray.remote
def summarize(partials: list) -> dict:
    total = sum(p["count"] for p in partials)
    positive = sum(sum(p["labels"]) for p in partials)
    return {"shards": len(partials), "rows": total, "positive_rate": round(positive / total, 3)}


# ===========================================================================
# ② 批量推理:模型常驻在 actor 里(避免每个任务重载)
# ===========================================================================


@ray.remote
class Predictor:
    """真实场景里 __init__ 里加载的是 transformer 权重(GPU 上)。"""

    def __init__(self, model_version: str = "v1"):
        self.version = model_version
        self.weights = [0.1, 0.2, 0.3]  # 假装是模型参数
        self.served = 0

    def predict_batch(self, features: list) -> list:
        self.served += len(features)
        return [
            sum(f * w for f, w in zip(features, self.weights)) for _ in range(len(features))
        ]

    def stats(self) -> dict:
        return {"version": self.version, "served": self.served}


@ray.remote
class Vectorizer:
    """在把数据喂给预测器之前做一些变换 —— 模拟真实的多阶段预处理。"""

    def transform(self, record: dict) -> list:
        return [float(x) for x in record["features"]]


# ===========================================================================
# ③ 训练:放置组 + 参数服务器(Ray 里对应 Ray Train + Tune)
# ===========================================================================


@ray.remote
class ParameterServer:
    def __init__(self, dim: int):
        self.weights = [0.1] * dim
        self.version = 0
        self.updates = 0

    def pull(self):
        return list(self.weights), self.version

    def push(self, gradients, lr: float):
        for i, g in enumerate(gradients):
            self.weights[i] -= lr * g
        self.version += 1
        self.updates += 1
        return self.version


@ray.remote
def train_worker(server, shard: dict, lr: float = 0.05) -> float:
    """一个训练 worker:拉权重 → 算梯度 → 推回去。"""
    weights, _ = ray.get(server.pull.remote())
    dim = len(weights)
    features, labels = shard["features"], shard["labels"]
    n = len(features)

    gradients = [0.0] * dim
    loss = 0.0
    for i in range(n):
        x = float(features[i] % 5)
        y = float(labels[i])
        pred = sum(w * x for w in weights) / dim
        err = pred - y
        loss += err * err
        for d in range(dim):
            gradients[d] += 2 * err * x / (n * dim)

    ray.get(server.push.remote(gradients, lr))
    return loss / n


# ===========================================================================
# ④ 在线服务:一个 actor 当推理端点(真实场景里是 Ray Serve)
# ===========================================================================


@ray.remote
class InferenceEndpoint:
    def __init__(self, model_version: str):
        # 真实场景:这里 build_openai_app / vLLM engine
        self.version = model_version
        self.calls = 0

    def handle(self, request: dict) -> dict:
        self.calls += 1
        return {
            "id": request["id"],
            "score": round(sum(request["features"]) * 0.01, 4),
            "model": self.version,
        }

    def health(self) -> dict:
        return {"status": "ok", "version": self.version, "calls": self.calls}


# ===========================================================================
# ⑥ 容错演练用
# ===========================================================================


@ray.remote(max_retries=2)
def flaky_stage(shard: dict, marker_dir: str) -> str:
    """第一次执行时崩溃(模拟 worker 被 OOM killer 杀掉),重试后成功。"""
    marker = os.path.join(marker_dir, f"shard-{shard['shard']}.marker")
    if not os.path.exists(marker):
        with open(marker, "w") as fh:
            fh.write("crashed")
        os._exit(1)
    return f"shard-{shard['shard']}-ok"


# ===========================================================================


def main() -> None:
    import tempfile

    ray.init(num_cpus=6, num_gpus=2, object_store_memory=256 * 1024 * 1024, )
    workdir = tempfile.mkdtemp(prefix="miniray-e2e-")
    try:
        # ───────────── ① 数据准备 ─────────────
        print("=" * 68)
        print("① 数据准备:4 个分片,依赖自动并行")
        print("=" * 68)
        t0 = time.time()
        raw = [make_shard.remote(i) for i in range(4)]
        prepared = [preprocess.remote(shard) for shard in raw]
        summary = ray.get(summarize.remote(prepared))
        print(f"  结果: {summary}")
        print(f"  耗时: {time.time() - t0:.2f}s(分片读取与预处理按数据流并行)")

        # ───────────── ② 批量推理 ─────────────
        print("\n" + "=" * 68)
        print("② 批量推理:2 个常驻预测器 + ActorPool(带背压)")
        print("=" * 68)
        t0 = time.time()
        # 注意:下面每个 actor 都会**终身占用** 1 个 CPU —— 这是 actor 的核心语义。
        # 用完不再需要的 actor 必须显式 kill,否则资源一直被占着(真实 Ray 同理)。
        vectorizers = [Vectorizer.remote() for _ in range(2)]
        pool = ActorPool([Predictor.remote("v1"), Predictor.remote("v1")])
        print(f"  创建 2+2 个 actor 后,可用资源: {ray.available_resources()}")
        feature_batches = ray.get([preprocess.remote(shard) for shard in raw])
        results = []
        for batch in pool.map_unordered(
            lambda actor, rec: actor.predict_batch.remote(rec["features"]), feature_batches[:12]
        ):
            results.extend(batch)
        print(f"  推理了 {len(results)} 条,示例结果: {[round(r, 3) for r in results[:5]]}")
        print(f"  耗时: {time.time() - t0:.2f}s(模型只加载一次,常驻在 actor 里)")

        # 预处理 actor 用完了 → 杀掉,把资源还给集群(否则下面的训练拿不到 CPU)
        for vectorizer in vectorizers:
            ray.kill(vectorizer)
        print(f"  杀掉不再需要的 Vectorizer 后,可用资源: {ray.available_resources()}")

        # ───────────── ③ 训练:放置组 + 参数服务器 ─────────────
        print("\n" + "=" * 68)
        print("③ 训练:放置组把 2 个 worker 的资源先占下来")
        print("=" * 68)
        pg = placement_group([{"CPU": 1}, {"CPU": 1}], strategy="PACK")
        bundles = ray.get(pg.ready())
        print(f"  放置组就绪: bundle → 节点 {[f'{k}:{v[:8]}' for k, v in bundles.items()]}")
        print(f"  预留后可用资源: {ray.available_resources()}(bundle 里的资源只有该组能用)")

        server = ParameterServer.remote(3)
        t0 = time.time()
        losses = []
        for epoch in range(5):
            refs = [
                train_worker.options(
                    scheduling_strategy=PlacementGroupSchedulingStrategy(
                        pg, placement_group_bundle_index=i % 2
                    )
                ).remote(server, feature_batches[i])
                for i in range(2)
            ]
            losses.append(sum(ray.get(refs)) / len(refs))
        weights, version = ray.get(server.pull.remote())
        print(f"  5 轮训练完成: 版本={version} loss {losses[0]:.4f} → {losses[-1]:.4f}")
        print(f"  权重: {[round(w, 4) for w in weights]}")
        print(f"  耗时: {time.time() - t0:.2f}s")
        remove_placement_group(pg)

        # ───────────── ④ 在线服务 ─────────────
        print("\n" + "=" * 68)
        print("④ 在线服务:一个有状态推理端点")
        print("=" * 68)
        endpoint = InferenceEndpoint.remote("v1")
        requests = [{"id": i, "features": [i, i + 1, i + 2]} for i in range(5)]
        responses = ray.get([endpoint.handle.remote(req) for req in requests])
        for resp in responses[:3]:
            print(f"  请求 {resp['id']} → score={resp['score']} (model={resp['model']})")
        print(f"  健康检查: {ray.get(endpoint.health.remote())}")

        # ───────────── ⑤ 可观测性 ─────────────
        print("\n" + "=" * 68)
        print("⑤ 可观测性")
        print("=" * 68)
        print(f"  任务统计: {state.summarize_tasks()}")
        print(f"  对象统计: {state.summarize_objects()}")
        print(f"  actor 统计: {state.summarize_actors()}")
        workers = state.list_workers()
        print(f"  worker 池: {len(workers)} 个进程,总共执行了 "
              f"{sum(w['num_tasks_executed'] for w in workers)} 个任务")

        json_path = os.path.join(workdir, "timeline.json")
        html_path = os.path.join(workdir, "timeline.html")
        ray.timeline(json_path, html=html_path)
        print(f"  timeline: {html_path}")

        # ───────────── ⑥ 容错演练 ─────────────
        print("\n" + "=" * 68)
        print("⑥ 容错演练:worker 崩溃后自动重跑 + lineage 重建")
        print("=" * 68)
        recovered = ray.get(
            [flaky_stage.remote(shard, workdir) for shard in feature_batches[:2]], timeout=60
        )
        print(f"  崩溃后重试结果: {recovered}")
        retried = [t for t in state.list_tasks() if t.get("num_attempts", 1) > 1]
        print(f"  重试过的任务数: {len(retried)}")

        ref = summarize.remote(feature_batches)
        before = ray.get(ref)
        from miniray._private import fault_injection

        lost = fault_injection.lose_objects()
        try:
            after = ray.get(ref, timeout=60)
            print(f"  丢光对象后重新取值: {before == after}(值与之前一致)")
        except Exception as error:
            # 重建失败时,先把「哪个对象没就绪、哪些任务卡住了」打出来 ——
            # 这正是排查分布式问题的标准动作:看状态,而不是猜
            from miniray import runtime as _runtime

            raylet = _runtime.get_raylet()
            lineage = set(raylet._lineage)
            print(f"  重建失败: {type(error).__name__}(丢了 {len(lost['lost'])} 个对象)")
            print(f"  其中没有血缘(不可重建)的: "
                  f"{[x[:8] for x in lost['lost'] if x not in lineage]}")
            recon = [t for t in state.list_tasks() if "reconstruct" in t["name"]]
            print(f"  重建任务共 {len(recon)} 个:")
            for t in recon[:14]:
                print(f"    {t['state']:<12} {t['name']:<34} attempts={t['num_attempts']} "
                      f"node={str(t['node_id'])[:8]} err={str(t['error'])[:60]}")
            for obj in state.list_objects():
                if obj["state"] != "READY":
                    print(f"    对象 {obj['object_id'][:8]} 状态={obj['state']} "
                          f"生产者任务={str(obj['produced_by'])[:8]}")
            for task in state.list_tasks():
                if task["state"] not in ("FINISHED",):
                    print(f"    任务 {task['name']} 状态={task['state']} "
                          f"重试={task['num_attempts']} 错误={str(task['error'])[:120]}")
            raise
        print(f"  重建次数: {state.summarize_objects()['num_reconstructions']}")

        # ───────────── 收尾 ─────────────
        print("\n" + "=" * 68)
        print("全流程完成。对应真实 Ray 的组件:")
        print("  ① Ray Data(read_parquet + map_batches) ② Ray Data LLM / ActorPool")
        print("  ③ Ray Train + Ray Tune                  ④ Ray Serve / Serve LLM")
        print("  ⑤ ray.util.state + ray.timeline         ⑥ 同样的重试与 lineage 重建")
        print("=" * 68)
    finally:
        ray.shutdown()


if __name__ == "__main__":
    main()

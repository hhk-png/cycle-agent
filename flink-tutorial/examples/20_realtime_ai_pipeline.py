"""MiniFlink 20：实时特征 + 模型打分 + 向量检索的教学形状。

这不是一个真实的 AI 框架示例：

* “模型”是一个本地线性打分函数；
* “向量检索”是一个内存列表的余弦相似度；
* 没有外部 API、限流、重试和 durable call。

它演示的是第 35 章的拓扑：窗口特征 → 推理 → 检索 → 审计输出。
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miniflink import (
    AggregateFunction,
    Duration,
    Record,
    StreamExecutionEnvironment,
    TumblingEventTimeWindows,
    WatermarkStrategy,
)


class FeatureAggregate(AggregateFunction):
    def create_accumulator(self):
        return {"count": 0, "amount": 0.0, "text": ""}

    def add(self, value, accumulator):
        return {
            "count": accumulator["count"] + 1,
            "amount": accumulator["amount"] + value["amount"],
            "text": value["text"],
        }

    def get_result(self, accumulator):
        return dict(accumulator)

    def merge(self, left, right):
        return {
            "count": left["count"] + right["count"],
            "amount": left["amount"] + right["amount"],
            "text": right["text"] or left["text"],
        }


# 一个极简的“知识库”：真实系统里会在 Paimon/向量库中。
KNOWLEDGE = [
    {"doc_id": "refund-policy", "vector": [0.9, 0.1, 0.0]},
    {"doc_id": "fraud-pattern", "vector": [0.1, 0.9, 0.1]},
    {"doc_id": "vip-service", "vector": [0.2, 0.1, 0.9]},
]


def embed(text: str) -> list[float]:
    """用关键词模拟 embedding；生产环境用真实模型。"""
    vector = [0.0, 0.0, 0.0]
    keywords = {
        "refund": 0,
        "fraud": 1,
        "vip": 2,
    }
    for keyword, index in keywords.items():
        if keyword in text.lower():
            vector[index] += 1.0
    if not any(vector):
        vector[0] = 0.1
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left)) or 1.0
    right_norm = math.sqrt(sum(b * b for b in right)) or 1.0
    return dot / (left_norm * right_norm)


def retrieve(feature):
    query = embed(feature["text"])
    ranked = sorted(
        KNOWLEDGE,
        key=lambda doc: cosine(query, doc["vector"]),
        reverse=True,
    )
    return {**feature, "retrieved": [doc["doc_id"] for doc in ranked[:2]]}


def score(feature):
    # 一个可解释的“模型”：金额越大、频次越高，风险越高。
    risk = min(0.99, feature["amount"] / 200.0 + feature["count"] * 0.05)
    label = "high" if risk >= 0.6 else "normal"
    return {
        "user_id": feature["user_id"],
        "feature": {"count": feature["count"], "amount": feature["amount"]},
        "risk": round(risk, 3),
        "label": label,
        "retrieved": feature["retrieved"],
    }


events = [
    Record({"user_id": "u1", "amount": 80, "text": "refund request"}, timestamp=1_000),
    Record({"user_id": "u1", "amount": 120, "text": "refund request again"}, timestamp=1_500),
    Record({"user_id": "u2", "amount": 20, "text": "vip service"}, timestamp=2_000),
    Record({"user_id": "u2", "amount": 30, "text": "vip service"}, timestamp=2_500),
]

strategy = WatermarkStrategy.for_bounded_out_of_orderness(0)

env = StreamExecutionEnvironment(parallelism=1)
(
    env.from_collection(events, watermark_strategy=strategy, parallelism=1)
    .key_by(lambda event: event["user_id"])
    .window(TumblingEventTimeWindows.of(Duration.of_seconds(2)))
    .aggregate(FeatureAggregate())
    .map(lambda window: {
        "user_id": window.key,
        "count": window.value["count"],
        "amount": window.value["amount"],
        "text": window.value["text"],
    })
    .async_map(retrieve, capacity=2)
    .map(score)
    .sink_to(lambda decision: print("decision", decision))
)
env.execute("realtime-ai-shape")

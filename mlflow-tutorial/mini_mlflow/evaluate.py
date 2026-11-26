"""Evaluation metrics and run integration.

The core lesson: *evaluation is logging plus statistics*.  A metric is only
useful if you know the dataset, the slice, the threshold, and the aggregation.
These helpers keep the statistics dependency-free and make every report
explicit about class labels and counts.

For production LLM evaluation, pair deterministic metrics here with
model-graded and human review; see chapter 28.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .errors import InvalidParameter


@dataclass
class EvaluationResult:
    metrics: dict[str, float]
    report: dict[str, Any]
    artifact_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metrics": self.metrics,
            "report": self.report,
            "artifact_paths": list(self.artifact_paths),
        }


def _validate_pairs(predictions: Sequence[Any], targets: Sequence[Any]) -> None:
    if len(predictions) != len(targets):
        raise InvalidParameter(
            f"predictions and targets must have the same length ({len(predictions)} != {len(targets)})"
        )
    if not predictions:
        raise InvalidParameter("evaluation requires at least one example")


def regression_metrics(predictions: Sequence[Any], targets: Sequence[Any]) -> dict[str, float]:
    _validate_pairs(predictions, targets)
    pred = [float(value) for value in predictions]
    target = [float(value) for value in targets]
    errors = [p - t for p, t in zip(pred, target)]
    mse = sum(error * error for error in errors) / len(errors)
    mae = sum(abs(error) for error in errors) / len(errors)
    mean_target = sum(target) / len(target)
    sst = sum((value - mean_target) ** 2 for value in target)
    r2 = 1.0 - sum(error * error for error in errors) / sst if sst else 0.0
    non_zero = [(p, t) for p, t in zip(pred, target) if t != 0]
    mape = (
        sum(abs((p - t) / t) for p, t in non_zero) / len(non_zero) * 100.0
        if non_zero
        else float("nan")
    )
    result = {
        "mse": mse,
        "rmse": math.sqrt(mse),
        "mae": mae,
        "r2": r2,
        "max_error": max(abs(error) for error in errors),
        "mean_prediction": sum(pred) / len(pred),
        "mean_target": mean_target,
    }
    if not math.isnan(mape):
        result["mape"] = mape
    return result


def _label_key(value: Any) -> str:
    return str(value)


def classification_metrics(
    predictions: Sequence[Any],
    targets: Sequence[Any],
    *,
    labels: Sequence[Any] | None = None,
) -> dict[str, Any]:
    _validate_pairs(predictions, targets)
    predicted = [_label_key(value) for value in predictions]
    actual = [_label_key(value) for value in targets]
    all_labels = [str(label) for label in labels] if labels is not None else sorted(set(predicted) | set(actual))
    if not all_labels:
        raise InvalidParameter("classification evaluation needs at least one label")
    confusion = {label: {other: 0 for other in all_labels} for label in all_labels}
    unknown = [value for value in actual + predicted if value not in set(all_labels)]
    if unknown:
        raise InvalidParameter("predictions or targets contain labels not present in labels")
    for p, t in zip(predicted, actual):
        confusion[t][p] += 1

    accuracy = sum(1 for p, t in zip(predicted, actual) if p == t) / len(actual)
    per_class: dict[str, dict[str, float]] = {}
    for label in all_labels:
        tp = sum(1 for p, t in zip(predicted, actual) if p == label and t == label)
        fp = sum(1 for p, t in zip(predicted, actual) if p == label and t != label)
        fn = sum(1 for p, t in zip(predicted, actual) if p != label and t == label)
        support = sum(1 for t in actual if t == label)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_class[label] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": float(support),
        }
    total = len(actual)
    macro_precision = sum(item["precision"] for item in per_class.values()) / len(all_labels)
    macro_recall = sum(item["recall"] for item in per_class.values()) / len(all_labels)
    macro_f1 = sum(item["f1"] for item in per_class.values()) / len(all_labels)
    weighted_precision = sum(item["precision"] * item["support"] for item in per_class.values()) / total
    weighted_recall = sum(item["recall"] * item["support"] for item in per_class.values()) / total
    weighted_f1 = sum(item["f1"] * item["support"] for item in per_class.values()) / total
    metrics = {
        "accuracy": accuracy,
        "precision_macro": macro_precision,
        "recall_macro": macro_recall,
        "f1_macro": macro_f1,
        "precision_weighted": weighted_precision,
        "recall_weighted": weighted_recall,
        "f1_weighted": weighted_f1,
        "support": float(total),
    }
    return {
        "metrics": metrics,
        "labels": all_labels,
        "per_class": per_class,
        "confusion_matrix": confusion,
    }


def roc_auc_score(y_true: Sequence[Any], y_score: Sequence[float], *, positive_label: Any = 1) -> float:
    """Binary ROC AUC with average ranks for ties."""
    if len(y_true) != len(y_score):
        raise InvalidParameter("y_true and y_score must have equal length")
    positives = [float(score) for label, score in zip(y_true, y_score) if label == positive_label]
    negatives = [float(score) for label, score in zip(y_true, y_score) if label != positive_label]
    if not positives or not negatives:
        raise InvalidParameter("ROC AUC requires at least one positive and one negative example")
    wins = 0.0
    for pos in positives:
        for neg in negatives:
            if pos > neg:
                wins += 1.0
            elif pos == neg:
                wins += 0.5
    return wins / (len(positives) * len(negatives))


def log_loss(y_true: Sequence[Any], probabilities: Sequence[float], *, positive_label: Any = 1) -> float:
    if len(y_true) != len(probabilities):
        raise InvalidParameter("y_true and probabilities must have equal length")
    epsilon = 1e-15
    total = 0.0
    for label, probability in zip(y_true, probabilities):
        probability = min(max(float(probability), epsilon), 1 - epsilon)
        total += math.log(probability) if label == positive_label else math.log(1 - probability)
    return -total / len(probabilities)


_TOKEN_RE = re.compile(r"[\w一-鿿]+", re.UNICODE)


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(str(text).lower())


def token_f1(prediction: str, reference: str) -> float:
    pred_tokens = _tokens(prediction)
    ref_tokens = _tokens(reference)
    if not pred_tokens and not ref_tokens:
        return 1.0
    if not pred_tokens or not ref_tokens:
        return 0.0
    from collections import Counter

    overlap = Counter(pred_tokens) & Counter(ref_tokens)
    common = sum(overlap.values())
    if common == 0:
        return 0.0
    precision = common / len(pred_tokens)
    recall = common / len(ref_tokens)
    return 2 * precision * recall / (precision + recall)


def rouge_l(prediction: str, reference: str) -> float:
    pred_tokens = _tokens(prediction)
    ref_tokens = _tokens(reference)
    if not pred_tokens or not ref_tokens:
        return 0.0
    previous = [0] * (len(ref_tokens) + 1)
    for pred_token in pred_tokens:
        current = [0]
        for index, ref_token in enumerate(ref_tokens, start=1):
            if pred_token == ref_token:
                current.append(previous[index - 1] + 1)
            else:
                current.append(max(previous[index], current[-1]))
        previous = current
    lcs = previous[-1]
    precision = lcs / len(pred_tokens)
    recall = lcs / len(ref_tokens)
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def llm_metrics(predictions: Sequence[str], references: Sequence[str]) -> dict[str, float]:
    _validate_pairs(predictions, references)
    exact = sum(1 for prediction, reference in zip(predictions, references) if str(prediction).strip() == str(reference).strip()) / len(predictions)
    token_scores = [token_f1(prediction, reference) for prediction, reference in zip(predictions, references)]
    rouge_scores = [rouge_l(prediction, reference) for prediction, reference in zip(predictions, references)]
    contains = sum(1 for prediction, reference in zip(predictions, references) if str(reference).lower() in str(prediction).lower()) / len(predictions)
    json_valid = 0
    for prediction in predictions:
        try:
            json.loads(prediction)
            json_valid += 1
        except (TypeError, ValueError):
            pass
    return {
        "exact_match": exact,
        "token_f1": sum(token_scores) / len(token_scores),
        "rouge_l": sum(rouge_scores) / len(rouge_scores),
        "contains_reference": contains,
        "json_validity": json_valid / len(predictions),
    }


def evaluate_predictions(
    predictions: Sequence[Any],
    targets: Sequence[Any],
    *,
    task: str = "regression",
    labels: Sequence[Any] | None = None,
    references: Sequence[str] | None = None,
) -> dict[str, Any]:
    task = task.lower()
    if task == "regression":
        return {"task": task, "metrics": regression_metrics(predictions, targets)}
    if task == "classification":
        return {"task": task, **classification_metrics(predictions, targets, labels=labels)}
    if task in {"llm", "text"}:
        refs = [str(item) for item in (references if references is not None else targets)]
        return {"task": "llm", "metrics": llm_metrics([str(item) for item in predictions], refs)}
    raise InvalidParameter("task must be regression, classification, or llm")


def evaluate_model(
    model: Any,
    data: Any,
    targets: Sequence[Any] | None = None,
    *,
    task: str = "regression",
    labels: Sequence[Any] | None = None,
    client: Any = None,
    run_id: str | None = None,
    prefix: str = "eval",
    artifacts_path: str = "evaluation",
    references: Sequence[str] | None = None,
) -> EvaluationResult:
    if not hasattr(model, "predict"):
        raise InvalidParameter("model must expose predict(data)")
    if targets is None:
        raise InvalidParameter("targets are required for supervised evaluation")
    predictions = model.predict(data)
    report = evaluate_predictions(predictions, targets, task=task, labels=labels, references=references)
    metrics = {key: float(value) for key, value in report["metrics"].items()}
    artifact_paths: list[str] = []
    if client is not None and run_id:
        for name, value in metrics.items():
            client.log_metric(run_id, f"{prefix}_{name}" if prefix else name, value)
        client.log_dict(run_id, report, f"{artifacts_path}/report.json")
        artifact_paths.append(f"{artifacts_path}/report.json")
        if task.lower() == "classification" and "confusion_matrix" in report:
            client.log_dict(run_id, report["confusion_matrix"], f"{artifacts_path}/confusion_matrix.json")
            artifact_paths.append(f"{artifacts_path}/confusion_matrix.json")
    return EvaluationResult(metrics=metrics, report=report, artifact_paths=artifact_paths)


def evaluate(
    model: Any,
    data: Any,
    targets: Sequence[Any] | None = None,
    *,
    task: str = "regression",
    labels: Sequence[Any] | None = None,
    prefix: str = "eval",
    artifacts_path: str = "evaluation",
) -> EvaluationResult:
    from .fluent import get_active_run

    active = get_active_run()
    if active is None:
        raise InvalidParameter("evaluate() requires an active run")
    return evaluate_model(
        model,
        data,
        targets,
        task=task,
        labels=labels,
        client=active.client,
        run_id=active.run.info.run_id,
        prefix=prefix,
        artifacts_path=artifacts_path,
    )

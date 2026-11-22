"""Rule-based rewards, reward managers and a tiny model-based reward worker."""

from __future__ import annotations

import importlib
import math
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Callable

import numpy as np

from .protocol import DataProto

__all__ = [
    "extract_boxed",
    "normalize_number",
    "exact_match_reward",
    "bandit_reward",
    "math_reward",
    "format_reward",
    "length_penalty",
    "RewardManager",
    "DAPORewardManager",
    "RewardLoopManager",
    "load_custom_reward",
]


_BOXED = re.compile(r"\\boxed\{([^{}]*)\}")
_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?(?:/\d+)?")


def extract_boxed(text: str) -> str | None:
    matches = _BOXED.findall(text)
    if matches:
        return matches[-1].strip()
    return None


def normalize_number(text: str) -> str:
    text = text.strip().replace(",", "").replace("$", "").replace("%", "")
    text = text.rstrip(".")
    try:
        value = float(text)
    except ValueError:
        match = _NUMBER.search(text)
        if not match:
            return text
        text = match.group(0)
        try:
            value = float(text)
        except ValueError:
            return text
    if math.isfinite(value) and value.is_integer():
        return str(int(value))
    return f"{value:g}"


def exact_match_reward(response: str, answer: str, **_) -> float:
    return 1.0 if response.strip() == answer.strip() else 0.0


def bandit_reward(response: str, answer: str, **_) -> float:
    """Prefix match for the contextual-bandit teaching task.

    Constrained decoding (``rollout.allowed_chars``) makes the action space
    small, so this reward is dense enough for a few dozen CPU steps while still
    being a genuine verifiable reward.
    """
    return 1.0 if response.strip()[: len(answer)] == answer else 0.0


def math_reward(response: str, answer: str, **_) -> float:
    prediction = extract_boxed(response)
    target = extract_boxed(answer) or answer
    if prediction is None:
        # A bare final number is accepted, mirroring common math verifiers.
        numbers = _NUMBER.findall(response)
        prediction = numbers[-1] if numbers else response
    return 1.0 if normalize_number(prediction) == normalize_number(target) else 0.0


def format_reward(response: str, **_) -> float:
    score = 0.0
    if extract_boxed(response) is not None:
        score += 0.2
    if re.search(r"\d", response):
        score += 0.1
    if len(response) <= 256:
        score += 0.1
    return score


def length_penalty(response_length: int, max_length: int, factor: float = 0.1) -> float:
    if max_length <= 0 or response_length <= max_length:
        return 0.0
    return -factor * (response_length - max_length) / max_length


def load_custom_reward(spec: str) -> Callable[..., float]:
    if ":" not in spec:
        raise ValueError("custom reward spec must be 'module:function'")
    module_name, function_name = spec.split(":", 1)
    module = importlib.import_module(module_name)
    function = getattr(module, function_name)
    if not callable(function):
        raise TypeError(f"{spec} is not callable")
    return function


class RewardManager:
    """Score generated responses and scatter the scalar to the last token.

    This is the MiniVerl analogue of verl's ``NaiveRewardManager``: the reward
    is sequence-level, but PPO/GAE consume token-level rewards, so the last
    valid response token receives the score.
    """

    def __init__(self, reward_fn: Callable[..., float] | str = "math", overlong_penalty_factor: float = 0.0):
        if isinstance(reward_fn, str):
            if reward_fn == "math":
                reward_fn = math_reward
            elif reward_fn in {"exact", "exact_match"}:
                reward_fn = exact_match_reward
            elif reward_fn == "format":
                reward_fn = format_reward
            elif reward_fn == "bandit":
                reward_fn = bandit_reward
            else:
                reward_fn = load_custom_reward(reward_fn)
        self.reward_fn = reward_fn
        self.overlong_penalty_factor = overlong_penalty_factor

    def score(
        self,
        response: str,
        answer: str,
        data_source: str = "",
        response_length: int = 0,
        max_length: int = 0,
        truncated: bool = False,
    ) -> float:
        try:
            try:
                score = float(self.reward_fn(response=response, answer=answer, data_source=data_source))
            except TypeError:
                # Some user functions accept only positional (response, answer).
                # Keep this fallback inside the same guard: a TypeError raised
                # *inside* the positional call must also become a zero score.
                score = float(self.reward_fn(response, answer))
        except Exception:
            # A failing verifier must not kill the whole training step; verl's
            # reward loop converts exceptions into a zero score and records it.
            score = 0.0
        if self.overlong_penalty_factor and max_length and truncated:
            score += -self.overlong_penalty_factor * (response_length / max_length)
        return score

    def run(self, data: DataProto) -> DataProto:
        if "response_text" not in data.non_tensor_batch:
            raise KeyError("reward manager needs generated 'response_text'")
        responses = data.non_tensor_batch["response_text"]
        answers = data.non_tensor_batch["answers"]
        sources = data.non_tensor_batch.get("data_source", np.asarray([""] * len(data), dtype=object))
        lengths = data.batch["response_mask"].sum(axis=-1).astype(np.int64)
        max_length = int(data.meta_info.get("max_response_length", data.batch["response_mask"].shape[-1]))
        truncated = data.batch.get("truncated")
        scores = np.asarray(
            [
                self.score(
                    str(responses[i]),
                    str(answers[i]),
                    str(sources[i]),
                    int(lengths[i]),
                    max_length,
                    bool(truncated[i]) if truncated is not None else False,
                )
                for i in range(len(data))
            ],
            dtype=np.float32,
        )
        token_rewards = np.zeros_like(data.batch["response_mask"], dtype=np.float32)
        for row in range(len(data)):
            valid = np.flatnonzero(data.batch["response_mask"][row] > 0)
            if valid.size:
                token_rewards[row, int(valid[-1])] = scores[row]
        data.batch["rm_scores"] = scores
        data.batch["token_level_rewards"] = token_rewards
        return data


class DAPORewardManager(RewardManager):
    """DAPO adds an explicit overlong penalty instead of silently truncating.

    The penalty is applied when the response consumes the entire budget, which
    is the observable signature of a truncated reasoning trace.
    """

    def score(
        self,
        response: str,
        answer: str,
        data_source: str = "",
        response_length: int = 0,
        max_length: int = 0,
        truncated: bool = False,
    ) -> float:
        score = super().score(response, answer, data_source, response_length, max_length, truncated)
        if max_length and truncated:
            # If a linear factor is configured, RewardManager already applied
            # it; do not stack a second, independent penalty on top.
            if not self.overlong_penalty_factor:
                score -= 1.0
        return score


class RewardLoopManager:
    """Parallel reward execution with timeout, retry and failure accounting."""

    def __init__(
        self,
        reward_fn: Callable[..., float] | str = "math",
        max_workers: int = 4,
        max_retries: int = 1,
        timeout: float = 0.0,
    ):
        self.manager = RewardManager(reward_fn)
        self.max_workers = max(1, max_workers)
        self.max_retries = max_retries
        self.timeout = max(0.0, float(timeout))
        self.failures = 0
        self.retries = 0
        self.timeouts = 0

    def _score_with_retry(self, *args) -> float:
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                return self.manager.score(*args)
            except Exception as exc:  # pragma: no cover - defensive
                last_error = exc
                self.retries += 1
        self.failures += 1
        if last_error is not None:
            return 0.0
        return 0.0

    def run(self, data: DataProto) -> DataProto:
        responses = data.non_tensor_batch["response_text"]
        answers = data.non_tensor_batch["answers"]
        sources = data.non_tensor_batch.get("data_source", np.asarray([""] * len(data), dtype=object))
        lengths = data.batch["response_mask"].sum(axis=-1).astype(np.int64)
        max_length = int(data.meta_info.get("max_response_length", data.batch["response_mask"].shape[-1]))
        truncated = data.batch.get("truncated")
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = [
                pool.submit(
                    self._score_with_retry,
                    str(responses[i]),
                    str(answers[i]),
                    str(sources[i]),
                    int(lengths[i]),
                    max_length,
                    bool(truncated[i]) if truncated is not None else False,
                )
                for i in range(len(data))
            ]
            scores_list = []
            for future in futures:
                try:
                    scores_list.append(future.result(timeout=self.timeout or None))
                except FutureTimeoutError:
                    # A slow verifier is a failure, not a reason to block the
                    # whole step.  The thread may still be running; the caller
                    # sees 0 for this sample and a timeout counter.
                    self.timeouts += 1
                    self.failures += 1
                    scores_list.append(0.0)
            scores = np.asarray(scores_list, dtype=np.float32)
        token_rewards = np.zeros_like(data.batch["response_mask"], dtype=np.float32)
        for row in range(len(data)):
            valid = np.flatnonzero(data.batch["response_mask"][row] > 0)
            if valid.size:
                token_rewards[row, int(valid[-1])] = scores[row]
        data.batch["rm_scores"] = scores
        data.batch["token_level_rewards"] = token_rewards
        data.meta_info["reward_loop/timeouts"] = float(self.timeouts)
        data.meta_info["reward_loop/failures"] = float(self.failures)
        data.meta_info["reward_loop/retries"] = float(self.retries)
        return data

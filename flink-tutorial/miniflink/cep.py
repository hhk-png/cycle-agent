"""复杂事件处理（CEP）的教学实现。

支持按 key 的简单序列模式：``begin(...).where(...).next(...).where(...)``，
可附加 ``followed_by``、``within``，以及仅在最后一步使用的 ``one_or_more``。
生产 Flink CEP 还支持模式组、否定模式、循环模式与超时侧输出；MiniFlink 只保留
最常用的序列匹配语义。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .api import DataStream, KeyedStream
from .core import MAX_WATERMARK, Collector, Duration, EndOfStream, Record, StreamElement, Watermark
from .graph import Edge, Partitioning, make_node
from .operators import Operator
from .functions import RuntimeContext


@dataclass(frozen=True)
class Match:
    key: Any
    values: dict[str, list[Any]]
    start_timestamp: int
    end_timestamp: int

    def __getitem__(self, name: str) -> Any:
        values = self.values.get(name, [])
        if not values:
            raise KeyError(name)
        return values[-1]

    def get(self, name: str, default: Any = None) -> Any:
        values = self.values.get(name)
        return values[-1] if values else default

    def all(self, name: str) -> list[Any]:
        return list(self.values.get(name, []))


@dataclass(frozen=True)
class _Step:
    name: str
    predicate: Callable[[Any], bool]
    kind: str = "next"  # next | followed_by
    quantifier: str = "one"  # one | one_or_more


class Pattern:
    def __init__(self, steps: list[_Step], within_ms: int | None = None) -> None:
        self.steps = steps
        self.within_ms = within_ms

    @classmethod
    def begin(cls, name: str) -> "Pattern":
        if not name:
            raise ValueError("模式步骤名称不能为空")
        return cls([])._append(name, lambda _: True)

    def where(self, predicate: Callable[[Any], bool]) -> "Pattern":
        if not self.steps:
            raise ValueError("请先调用 begin/next/followedBy 注册步骤")
        steps = list(self.steps)
        steps[-1] = _Step(steps[-1].name, predicate, steps[-1].kind, steps[-1].quantifier)
        return Pattern(steps, self.within_ms)

    def or_where(self, predicate: Callable[[Any], bool]) -> "Pattern":
        if not self.steps:
            raise ValueError("没有可组合的步骤")
        previous = self.steps[-1].predicate
        return self.where(lambda value: previous(value) or predicate(value))

    def next(self, name: str) -> "Pattern":
        return self._append(name, lambda _: True, kind="next")

    def followed_by(self, name: str) -> "Pattern":
        return self._append(name, lambda _: True, kind="followed_by")

    def followed_by_any(self, *names: str) -> "Pattern":
        """按给定顺序依次追加 ``followed_by`` 步骤（不是无序集合匹配）。"""
        if not names:
            raise ValueError("至少需要一个名称")
        pattern = self
        for name in names:
            pattern = pattern.followed_by(name)
        return pattern

    def one_or_more(self) -> "Pattern":
        if not self.steps:
            raise ValueError("没有可重复的步骤")
        steps = list(self.steps)
        steps[-1] = _Step(steps[-1].name, steps[-1].predicate, steps[-1].kind, "one_or_more")
        return Pattern(steps, self.within_ms)

    def times(self, count: int) -> "Pattern":
        if count <= 0:
            raise ValueError("重复次数必须大于 0")
        if not self.steps:
            raise ValueError("没有可重复的步骤")
        if count == 1:
            return self
        pattern = self
        last = self.steps[-1]
        for index in range(2, count + 1):
            # 重复的是“同一个条件”，不能退化为无条件步骤。
            pattern = pattern._append(
                f"{last.name}_{index}", last.predicate, last.kind
            )
        return pattern

    def optional(self, name: str) -> "Pattern":
        raise NotImplementedError("MiniFlink CEP 暂不支持 optional；真实 Flink 支持")

    def within(self, duration: Duration | int) -> "Pattern":
        value = duration.millis if isinstance(duration, Duration) else int(duration)
        if value <= 0:
            raise ValueError("within 必须大于 0")
        for step in self.steps:
            if step.quantifier == "one_or_more" and step is not self.steps[-1]:
                raise ValueError("one_or_more 只能用于最后一步")
        return Pattern(self.steps, value)

    def _append(
        self, name: str, predicate: Callable[[Any], bool], kind: str = "next"
    ) -> "Pattern":
        if not name:
            raise ValueError("模式步骤名称不能为空")
        if any(step.name == name for step in self.steps):
            raise ValueError(f"重复的步骤名称: {name}")
        if self.steps and self.steps[-1].quantifier == "one_or_more":
            raise ValueError("one_or_more 只能用于模式最后一步")
        return Pattern(self.steps + [_Step(name, predicate, kind)], self.within_ms)


@dataclass
class _PartialMatch:
    step_index: int
    values: dict[str, list[Any]]
    start_timestamp: int
    end_timestamp: int
    last_position: int

    def snapshot(self) -> dict[str, Any]:
        return {
            "step_index": self.step_index,
            "values": copy.deepcopy(self.values),
            "start": self.start_timestamp,
            "end": self.end_timestamp,
            "position": self.last_position,
        }

    @classmethod
    def restore(cls, snapshot: dict[str, Any]) -> "_PartialMatch":
        return cls(
            snapshot["step_index"],
            copy.deepcopy(snapshot["values"]),
            snapshot["start"],
            snapshot["end"],
            snapshot["position"],
        )


class CepOperator(Operator):
    def __init__(self, pattern: Pattern, selector: Callable[[Match], Any]) -> None:
        self.pattern = pattern
        self.selector = selector
        self.partials: dict[Any, list[_PartialMatch]] = {}
        # 严格 next 的“中间不能有记录”必须按 key 判断，不能用全局位置，
        # 否则其他 key 的记录会错误地打断当前 key 的匹配。
        self.positions: dict[Any, int] = {}
        self.watermark = -(1 << 63)
        self._context: RuntimeContext | None = None

    def open(self, context: RuntimeContext) -> None:
        super().open(context)
        self._context = context

    def process_record(self, record: Record, input_index: int = 0) -> list[StreamElement]:
        key = record.key
        if key is None:
            raise RuntimeError("CEP 必须运行在 keyBy 之后")
        position = self.positions.get(key, 0) + 1
        self.positions[key] = position
        ts = record.timestamp or 0
        output: list[StreamElement] = []
        current = self.partials.setdefault(key, [])
        # next 语义要求中间不能出现未匹配的记录（仅针对同一个 key）
        surviving: list[_PartialMatch] = []
        for partial in current:
            step = self.pattern.steps[partial.step_index]
            if step.kind == "next" and partial.last_position < position - 1:
                continue
            if (
                self.pattern.within_ms is not None
                and ts - partial.start_timestamp > self.pattern.within_ms
            ):
                continue
            surviving.append(partial)
        current[:] = surviving

        for partial in list(current):
            step = self.pattern.steps[partial.step_index]
            if not step.predicate(record.value):
                # 单步循环模式遇到不匹配记录就结束当前循环；下一条匹配记录
                # 会重新开始一个新的循环。
                if (
                    len(self.pattern.steps) == 1
                    and step.quantifier == "one_or_more"
                ):
                    current.remove(partial)
                continue
            new_values = copy.deepcopy(partial.values)
            new_values.setdefault(step.name, []).append(record.value)
            last_step = partial.step_index == len(self.pattern.steps) - 1
            if step.quantifier == "one_or_more" and last_step:
                match = Match(key, copy.deepcopy(new_values), partial.start_timestamp, ts)
                output.append(record.with_value(self.selector(match)))
                partial.values = new_values
                partial.end_timestamp = ts
                partial.last_position = position
                continue
            if last_step:
                match = Match(key, new_values, partial.start_timestamp, ts)
                output.append(record.with_value(self.selector(match)))
                current.remove(partial)
            else:
                partial.step_index += 1
                partial.values = new_values
                partial.end_timestamp = ts
                partial.last_position = position

        # 从当前记录开启新的匹配
        first = self.pattern.steps[0]
        if first.predicate(record.value):
            values = {first.name: [record.value]}
            if len(self.pattern.steps) == 1:
                if first.quantifier == "one_or_more":
                    # 单步 one_or_more 需要保留部分匹配，后续匹配事件会在
                    # 它上面累积；不能每个事件都当作独立匹配发射。
                    if not any(p.step_index == 0 for p in current):
                        match = Match(key, values, ts, ts)
                        output.append(record.with_value(self.selector(match)))
                        current.append(
                            _PartialMatch(
                                step_index=0,
                                values=values,
                                start_timestamp=ts,
                                end_timestamp=ts,
                                last_position=position,
                            )
                        )
                else:
                    match = Match(key, values, ts, ts)
                    output.append(record.with_value(self.selector(match)))
            else:
                current.append(
                    _PartialMatch(
                        step_index=1,
                        values=values,
                        start_timestamp=ts,
                        end_timestamp=ts,
                        last_position=position,
                    )
                )
        if self.pattern.within_ms is not None:
            cutoff = self.watermark - self.pattern.within_ms
            current[:] = [
                partial
                for partial in current
                if partial.start_timestamp >= cutoff
            ]
        # 没有未完成匹配的 key 不需要保留位置计数或空列表；否则“每条记录
        # 都新建一个 key、且永远不匹配”的作业会让 CEP 状态随 key 基数增长。
        if not current:
            self.partials.pop(key, None)
            self.positions.pop(key, None)
        return output

    def process_watermark(self, watermark: int) -> list[StreamElement]:
        self.watermark = max(self.watermark, watermark)
        if self.pattern.within_ms is not None:
            cutoff = watermark - self.pattern.within_ms
            for key in list(self.partials):
                self.partials[key] = [
                    partial
                    for partial in self.partials[key]
                    if partial.start_timestamp >= cutoff
                ]
                if not self.partials[key]:
                    del self.partials[key]
                    # 位置计数与 partials 同生命周期；只删 partials 会让
                    # 每个见过的 key 永久留下一个位置计数，状态随基数增长。
                    self.positions.pop(key, None)
        return [Watermark(watermark)]

    def end_of_input(self) -> list[StreamElement]:
        return [Watermark(MAX_WATERMARK), EndOfStream()]

    def snapshot(self) -> dict[str, Any]:
        return {
            "partials": [
                (
                    copy.deepcopy(key),
                    [partial.snapshot() for partial in partials],
                )
                for key, partials in self.partials.items()
            ],
            "positions": dict(self.positions),
            "watermark": self.watermark,
        }

    def restore(self, snapshot: dict[str, Any]) -> None:
        self.partials = {
            copy.deepcopy(key): [_PartialMatch.restore(item) for item in items]
            for key, items in snapshot.get("partials", [])
        }
        self.positions = dict(snapshot.get("positions", {}))
        self.watermark = snapshot.get("watermark", -(1 << 63))


class PatternStream:
    def __init__(self, keyed: KeyedStream, pattern: Pattern) -> None:
        self.keyed = keyed
        self.pattern = pattern

    def select(self, selector: Callable[[Match], Any], *, name: str = "cep-select") -> DataStream:
        def factory(index: int, subtasks: int) -> CepOperator:
            return CepOperator(copy.deepcopy(self.pattern), selector)

        node = make_node(
            name,
            factory,
            parallelism=self.keyed.parallelism,
            inputs=[Edge(self.keyed.node, Partitioning.HASH, input_index=0)],
        )
        return DataStream(self.keyed.env, node)

    def process(self, selector: Callable[[Match], Any], *, name: str = "cep-process") -> DataStream:
        return self.select(selector, name=name)


def pattern(stream: DataStream | KeyedStream, pattern: Pattern) -> PatternStream:
    if isinstance(stream, KeyedStream):
        keyed = stream
    else:
        keyed = stream.key_by(lambda _: "__cep__", parallelism=1)
    return PatternStream(keyed, pattern)

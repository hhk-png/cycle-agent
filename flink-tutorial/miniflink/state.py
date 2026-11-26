"""键控状态、算子状态与 TTL。

生产 Flink 的 StateBackend 可以落在堆内（HashMapStateBackend）或 RocksDB。
MiniFlink 只实现堆内后端，但保留了同样的访问边界：用户看不到底层字典，
只能通过 ValueState/ListState/MapState 等受控接口读写；快照时可以整体导出。
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Generic, Iterable, Iterator, TypeVar

T = TypeVar("T")
K = TypeVar("K")
V = TypeVar("V")
A = TypeVar("A")
ACC = TypeVar("ACC")


class TtlUpdateType(str, Enum):
    DISABLED = "disabled"
    ON_CREATE_AND_WRITE = "on_create_and_write"
    ON_READ = "on_read"


class TtlStateVisibility(str, Enum):
    NEVER_RETURN_EXPIRED = "never_return_expired"
    RETURN_EXPIRED_IF_NOT_CLEANED = "return_expired_if_not_cleaned"


@dataclass(frozen=True)
class StateTtlConfig:
    ttl_ms: int
    update_type: TtlUpdateType = TtlUpdateType.ON_CREATE_AND_WRITE
    visibility: TtlStateVisibility = TtlStateVisibility.NEVER_RETURN_EXPIRED

    @classmethod
    def new_config(cls, ttl_ms: int) -> "StateTtlConfig":
        if ttl_ms <= 0:
            raise ValueError("TTL 必须为正数")
        return cls(ttl_ms)


@dataclass(frozen=True)
class StateDescriptor(Generic[T]):
    name: str
    default_factory: Callable[[], T] = lambda: None  # type: ignore[assignment]
    ttl_config: StateTtlConfig | None = None


@dataclass(frozen=True)
class ValueStateDescriptor(StateDescriptor[T]):
    pass


@dataclass(frozen=True)
class ListStateDescriptor(StateDescriptor[list[T]]):
    default_factory: Callable[[], list[T]] = list


@dataclass(frozen=True)
class MapStateDescriptor(StateDescriptor[dict[K, V]]):
    default_factory: Callable[[], dict[K, V]] = dict


@dataclass(frozen=True)
class ReducingStateDescriptor(Generic[T]):
    """顺序刻意设计成 ``name, reduce_function, ...``，避免继承带来的字段错位。"""

    name: str
    reduce_function: Callable[[T, T], T]
    default_factory: Callable[[], Any] = lambda: None
    ttl_config: StateTtlConfig | None = None


@dataclass(frozen=True)
class AggregatingStateDescriptor(Generic[A]):
    name: str
    aggregate_function: Any
    default_factory: Callable[[], Any] = lambda: None
    ttl_config: StateTtlConfig | None = None


@dataclass(frozen=True)
class OperatorStateDescriptor(StateDescriptor[list[T]]):
    default_factory: Callable[[], list[T]] = list
    redistribute_mode: str = "even-split"


class State(Generic[T]):
    """所有状态的基类，绑定到 backend 与描述符。"""

    def __init__(self, backend: "KeyedStateBackend", descriptor: StateDescriptor[Any]) -> None:
        self._backend = backend
        self._descriptor = descriptor

    @property
    def name(self) -> str:
        return self._descriptor.name

    def clear(self) -> None:
        self._backend.clear_current(self._descriptor.name)


class ValueState(State[T]):
    def get(self) -> T:
        # 与 Flink 一致：返回值是快照，原地修改不会写入状态，必须显式 update()。
        return copy.deepcopy(
            self._backend.read_current(
                self._descriptor.name,
                self._descriptor.default_factory,
                self._descriptor.ttl_config,
            )
        )

    def value(self) -> T:
        return self.get()

    def update(self, value: T) -> None:
        self._backend.write_current(self._descriptor.name, value)

    def __repr__(self) -> str:
        return f"ValueState({self.name})"


class ListState(State[list[T]]):
    def get(self) -> list[T]:
        return copy.deepcopy(
            self._backend.read_current(
                self._descriptor.name,
                self._descriptor.default_factory,
                self._descriptor.ttl_config,
            )
        )

    def add(self, value: T) -> None:
        values = self._backend.read_current(self._descriptor.name, list, self._descriptor.ttl_config)
        values.append(value)
        self._backend.write_current(self._descriptor.name, values)

    def add_all(self, values: Iterable[T]) -> None:
        current = self._backend.read_current(self._descriptor.name, list, self._descriptor.ttl_config)
        current.extend(values)
        self._backend.write_current(self._descriptor.name, current)

    def update(self, values: list[T]) -> None:
        self._backend.write_current(self._descriptor.name, list(values))

    def extend(self, values: Iterable[T]) -> None:
        self.add_all(values)

    def __iter__(self) -> Iterator[T]:
        return iter(self.get())


class MapState(State[dict[K, V]]):
    def get(self, key: K) -> V | None:
        # 与 ValueState/ListState 一致：返回快照，原地修改不会隐式写入状态。
        # 需要修改时显式 put()/remove()。
        return copy.deepcopy(self._data().get(key))

    def put(self, key: K, value: V) -> None:
        data = self._data()
        data[key] = value
        self._backend.write_current(self._descriptor.name, data)

    def put_all(self, values: dict[K, V]) -> None:
        data = self._data()
        data.update(values)
        self._backend.write_current(self._descriptor.name, data)

    def remove(self, key: K) -> None:
        data = self._data()
        if key in data:
            del data[key]
            self._backend.write_current(self._descriptor.name, data)

    def contains(self, key: K) -> bool:
        return key in self._data()

    def items(self) -> Iterable[tuple[K, V]]:
        return copy.deepcopy(list(self._data().items()))

    def keys(self) -> Iterable[K]:
        return copy.deepcopy(list(self._data().keys()))

    def values(self) -> Iterable[V]:
        return copy.deepcopy(list(self._data().values()))

    def is_empty(self) -> bool:
        return not self._data()

    def _data(self) -> dict[K, V]:
        return self._backend.read_current(
            self._descriptor.name,
            self._descriptor.default_factory,
            self._descriptor.ttl_config,
        )


class ReducingState(State[T]):
    def add(self, value: T) -> None:
        descriptor: ReducingStateDescriptor[T] = self._descriptor  # type: ignore[assignment]
        current = self._backend.read_current(
            self._descriptor.name, lambda: None, self._descriptor.ttl_config
        )
        if current is None:
            self._backend.write_current(self._descriptor.name, value)
        else:
            self._backend.write_current(
                self._descriptor.name, descriptor.reduce_function(current, value)
            )

    def get(self) -> T:
        # 与 Flink 一致：返回值是快照，原地修改不会写入状态，必须显式 update()。
        return copy.deepcopy(
            self._backend.read_current(
                self._descriptor.name,
                self._descriptor.default_factory,
                self._descriptor.ttl_config,
            )
        )


class AggregatingState(State[A]):
    def add(self, value: Any) -> None:
        descriptor: AggregatingStateDescriptor[A] = self._descriptor  # type: ignore[assignment]
        current = self._backend.read_current(
            self._descriptor.name, lambda: None, self._descriptor.ttl_config
        )
        if current is None:
            current = descriptor.aggregate_function.create_accumulator()
        self._backend.write_current(
            self._descriptor.name, descriptor.aggregate_function.add(value, current)
        )

    def get(self) -> A:
        acc = self._backend.read_current(
            self._descriptor.name, lambda: None, self._descriptor.ttl_config
        )
        if acc is None:
            return self._descriptor.default_factory()  # type: ignore[return-value]
        # get_result 可能直接返回 accumulator；get() 必须返回快照，避免
        # 调用方原地修改时隐式改写状态。
        return copy.deepcopy(  # type: ignore[attr-defined]
            self._descriptor.aggregate_function.get_result(acc)
        )


class KeyedStateBackend:
    """堆内键控状态后端。

    数据布局：``{key: {state_name: (value, last_write_ms)}}``。真实 Flink 还有
    namespace（窗口状态会用到）和按键分组的 RocksDB 布局；这里用一个教学版
    模型覆盖最常用的访问模式。
    """

    def __init__(self, clock: Callable[[], int] | None = None) -> None:
        self._data: dict[Any, dict[str, tuple[Any, int]]] = {}
        self._current_key: Any = None
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._state_cache: dict[tuple[int, str], State[Any]] = {}

    @property
    def current_key(self) -> Any:
        return self._current_key

    def set_current_key(self, key: Any) -> None:
        self._current_key = key

    def read_current(
        self,
        name: str,
        default_factory: Callable[[], Any],
        ttl_config: StateTtlConfig | None = None,
    ) -> Any:
        key = self._require_key()
        entry = self._data.get(key, {}).get(name)
        if entry is None:
            return default_factory()
        value, last_write = entry
        if ttl_config and ttl_config.update_type is TtlUpdateType.DISABLED:
            return value
        now = self._clock()
        if ttl_config and self._is_expired(last_write, ttl_config, now):
            if ttl_config.visibility is TtlStateVisibility.NEVER_RETURN_EXPIRED:
                self._data.get(key, {}).pop(name, None)
                return default_factory()
        if ttl_config and ttl_config.update_type is TtlUpdateType.ON_READ:
            self._data[key][name] = (value, now)
        return value

    def write_current(self, name: str, value: Any) -> None:
        key = self._require_key()
        # 写入即序列化：调用方在 update/put/add 之后继续修改原对象，不能
        # 悄悄改到状态。Flink 在写状态时会序列化，这里用深拷贝模拟。
        self._data.setdefault(key, {})[name] = (
            copy.deepcopy(value),
            self._clock(),
        )

    def clear_current(self, name: str) -> None:
        key = self._require_key()
        self._data.get(key, {}).pop(name, None)
        if key in self._data and not self._data[key]:
            del self._data[key]

    def remove_key(self, key: Any) -> None:
        self._data.pop(key, None)

    def keys(self) -> Iterable[Any]:
        return list(self._data.keys())

    def snapshot(self) -> dict[str, Any]:
        return {"kind": "keyed", "data": copy.deepcopy(self._data)}

    def restore(self, snapshot: dict[str, Any]) -> None:
        if snapshot.get("kind") != "keyed":
            raise ValueError("不是键控状态快照")
        self._data = copy.deepcopy(snapshot["data"])

    def _require_key(self) -> Any:
        if self._current_key is None:
            raise RuntimeError("当前没有 key；键控状态只能在 keyBy 之后使用")
        return self._current_key

    @staticmethod
    def _is_expired(last_write: int, config: StateTtlConfig, now_ms: int) -> bool:
        return config.ttl_ms > 0 and (now_ms - last_write) >= config.ttl_ms


class OperatorStateStore:
    """算子（非键控）状态，支持 ListState 和 UnionListState 的教学版。"""

    def __init__(self) -> None:
        self._data: dict[str, list[Any]] = {}
        self._descriptors: dict[str, OperatorStateDescriptor[Any]] = {}

    def get_list_state(self, descriptor: OperatorStateDescriptor[T]) -> "OperatorListState[T]":
        self._descriptors[descriptor.name] = descriptor  # type: ignore[assignment]
        return OperatorListState(self, descriptor)

    def snapshot(self) -> dict[str, Any]:
        return {"kind": "operator", "data": copy.deepcopy(self._data)}

    def restore(self, snapshot: dict[str, Any]) -> None:
        if snapshot.get("kind") != "operator":
            raise ValueError("不是算子状态快照")
        self._data = copy.deepcopy(snapshot["data"])

    def read(self, name: str) -> list[Any]:
        return self._data.setdefault(name, [])

    def write(self, name: str, values: list[Any]) -> None:
        # 与 keyed state 一致：写入即序列化，避免调用方后续修改原列表/元素。
        self._data[name] = copy.deepcopy(list(values))


class OperatorListState(Generic[T]):
    def __init__(self, store: OperatorStateStore, descriptor: OperatorStateDescriptor[T]) -> None:
        self._store = store
        self._descriptor = descriptor

    def get(self) -> list[T]:
        return copy.deepcopy(self._store.read(self._descriptor.name))

    def add(self, value: T) -> None:
        self._store.read(self._descriptor.name).append(copy.deepcopy(value))

    def add_all(self, values: Iterable[T]) -> None:
        self._store.read(self._descriptor.name).extend(
            copy.deepcopy(list(values))
        )

    def update(self, values: list[T]) -> None:
        self._store.write(self._descriptor.name, values)

    def clear(self) -> None:
        self._store.write(self._descriptor.name, [])


class BroadcastState(MapState[K, V]):
    """广播状态。MiniFlink 中与普通 MapState 共享存储布局。"""


def snapshot_state_backend(backend: KeyedStateBackend) -> dict[str, Any]:
    return backend.snapshot()


def restore_state_backend(backend: KeyedStateBackend, state: dict[str, Any]) -> None:
    backend.restore(state)

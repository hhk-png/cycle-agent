import pytest

from miniflink import (
    OutputTag,
    ProcessFunction,
    StreamExecutionEnvironment,
)
from miniflink.core import JobExecutionError


def test_map_filter_flat_map():
    env = StreamExecutionEnvironment()
    result = (
        env.from_collection([1, 2, 3, 4])
        .map(lambda value: value * 2)
        .filter(lambda value: value > 4)
        .flat_map(lambda value: [value, value + 1])
        .execute_and_collect()
    )
    assert result == [6, 7, 8, 9]


def test_key_by_reduce():
    env = StreamExecutionEnvironment()
    result = (
        env.from_collection(["a", "b", "a", "a"])
        .map(lambda word: (word, 1))
        .key_by(lambda pair: pair[0])
        .reduce(lambda left, right: (left[0], left[1] + right[1]))
        .execute_and_collect()
    )
    assert result[-1] == ("a", 3)
    assert ("b", 1) in result


def test_union_merges_two_streams():
    env = StreamExecutionEnvironment()
    left = env.from_collection([1, 2])
    right = env.from_collection([3, 4])
    assert sorted(left.union(right).execute_and_collect()) == [1, 2, 3, 4]


class Split(ProcessFunction):
    def process_element(self, value, ctx, out):
        if value % 2 == 0:
            out.collect(value)
        else:
            ctx.output(OutputTag("odd"), value)


def test_side_output_branch():
    env = StreamExecutionEnvironment()
    stream = env.from_collection(range(6)).process(Split())
    odd = stream.get_side_output(OutputTag("odd"))
    assert stream.execute_and_collect() == [0, 2, 4]
    assert odd.execute_and_collect() == [1, 3, 5]


def test_async_map_preserves_order():
    env = StreamExecutionEnvironment()
    result = env.from_collection(range(10)).async_map(lambda value: value * 3, capacity=4).execute_and_collect()
    assert result == [value * 3 for value in range(10)]


def test_name_uid_and_plan():
    env = StreamExecutionEnvironment()
    stream = env.from_collection([1]).map(lambda value: value + 1).name("plus-one").uid("plus-one-uid")
    stream.print()
    plan = env.explain()
    assert "plus-one" in plan
    assert "plus-one[1]" in plan
    graph = env.get_job_graph()
    assert any(node.uid == "plus-one-uid" for node in graph.nodes)


def test_parallel_source_partitions_data():
    env = StreamExecutionEnvironment(parallelism=3)
    values = env.from_collection(list(range(10)), parallelism=3).execute_and_collect()
    assert sorted(values) == list(range(10))


def test_execute_without_sink_raises():
    env = StreamExecutionEnvironment()
    env.from_collection([1])
    with pytest.raises(RuntimeError):
        env.execute()


def test_from_elements_and_range():
    env = StreamExecutionEnvironment()
    assert env.from_elements(1, 2, 3).execute_and_collect() == [1, 2, 3]
    assert list(env.from_sequence(2, 5).execute_and_collect()) == [2, 3, 4]


def test_rebalance_transform():
    env = StreamExecutionEnvironment(parallelism=2)
    result = env.from_collection(range(8), parallelism=2).rebalance().map(lambda value: value + 1).execute_and_collect()
    assert sorted(result) == list(range(1, 9))


def test_collect_is_alias_of_execute_and_collect():
    env = StreamExecutionEnvironment()
    assert env.from_collection([5, 6]).collect() == [5, 6]


def test_async_map_error_is_wrapped_as_job_execution_error():
    env = StreamExecutionEnvironment()

    def fail(value):
        raise ValueError("boom")

    with pytest.raises(JobExecutionError) as exc_info:
        env.from_collection([1]).async_map(fail, capacity=1).execute_and_collect()
    assert isinstance(exc_info.value.__cause__, ValueError)

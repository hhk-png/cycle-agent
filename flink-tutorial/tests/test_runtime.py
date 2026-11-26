import pytest

from miniflink import StreamExecutionEnvironment
from miniflink.deployment import LocalCluster
from miniflink.graph import Edge, Node, build_graph, make_node
from miniflink.operators import PassThroughOperator


def _sink_stream(env, values):
    stream = env.from_collection(values)
    stream.print()
    return stream


def test_job_metrics():
    env = StreamExecutionEnvironment()
    env.from_collection([1, 2, 3]).map(lambda value: value + 1).print()
    job = env.execute("metrics")
    metrics = job.metrics()
    assert metrics["job"]["records_processed"] == 3
    assert metrics["job"]["finished"] is True
    assert len(metrics["tasks"]) == 3
    assert metrics["checkpoints"]["completed"] == 0


def test_job_graph_validation_duplicate_uid():
    env = StreamExecutionEnvironment()
    first = env.from_collection([1]).map(lambda value: value).uid("same")
    first.print()
    env.from_collection([2]).map(lambda value: value).uid("same").print()
    with pytest.raises(ValueError):
        env.get_execution_plan()


def test_max_rounds_leaves_job_unfinished():
    env = StreamExecutionEnvironment()
    env.enable_checkpointing(1000, micro_batch_records=1)
    env.from_collection([1, 2, 3]).sink_to(lambda value: None)
    execution = env.create_execution()
    execution.run(max_rounds=1)
    assert not execution.is_finished()
    assert execution.records_processed == 1


def test_file_sink_writes_atomically(tmp_path):
    target = tmp_path / "result.txt"
    env = StreamExecutionEnvironment()
    env.from_collection(["a", "b"]).write_as_text(str(target))
    env.execute()
    assert target.read_text(encoding="utf-8") == "a\nb\n"
    assert not (tmp_path / "result.txt.tmp").exists()


def test_local_cluster_slot_allocation_and_finish():
    env = StreamExecutionEnvironment(parallelism=2)
    _sink_stream(env, range(4))
    cluster = LocalCluster(task_managers=1, slots_per_task_manager=2)
    job = cluster.submit(env)
    assert job.status.value == "FINISHED"
    assert cluster.available_slots == 2


def test_local_cluster_rejects_insufficient_slots():
    env = StreamExecutionEnvironment(parallelism=4)
    _sink_stream(env, range(4))
    cluster = LocalCluster(task_managers=1, slots_per_task_manager=2)
    with pytest.raises(RuntimeError):
        cluster.submit(env)


def test_two_sinks_share_upstream():
    env = StreamExecutionEnvironment()
    stream = env.from_collection([1, 2, 3]).map(lambda value: value * 10)
    first = []
    second = []
    stream.sink_to(first.append)
    stream.sink_to(second.append)
    env.execute()
    assert sorted(first) == [10, 20, 30]
    assert sorted(second) == [10, 20, 30]


def test_graph_dot_output():
    env = StreamExecutionEnvironment()
    env.from_collection([1]).map(lambda value: value).print()
    graph = env.get_execution_plan()
    dot = graph.to_dot()
    assert dot.startswith("digraph")
    assert "->" in dot

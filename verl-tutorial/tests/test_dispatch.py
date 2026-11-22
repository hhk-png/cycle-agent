from __future__ import annotations

import numpy as np
import pytest

from mini_verl.protocol import DataProto
from mini_verl.single_controller.decorator import Dispatch, register
from mini_verl.single_controller.worker import Worker
from mini_verl.single_controller.worker_group import LocalWorkerGroup, ResourcePool


class DemoWorker(Worker):
    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def who(self):
        return self.rank

    @register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO)
    def echo(self, data):
        return data

    @register(dispatch_mode=Dispatch.DP_COMPUTE)
    def add_one(self, value):
        return value + 1

    @register(dispatch_mode=Dispatch.RANK_ZERO)
    def rank_zero(self):
        return self.rank

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def maybe_fail(self):
        self._maybe_fail()
        return self.rank


def make_group(world=2):
    return LocalWorkerGroup(ResourcePool.single_node(world), DemoWorker)


def test_resource_pool():
    pool = ResourcePool([2, 3])
    assert pool.world_size == 5
    assert pool.local_world_size_list() == [2, 2, 3, 3, 3]
    assert pool.local_rank_list() == [0, 1, 0, 1, 2]
    with pytest.raises(ValueError):
        ResourcePool([])


def test_one_to_all():
    group = make_group(3)
    assert group.who() == [0, 1, 2]


def test_rank_zero():
    group = make_group(3)
    assert group.rank_zero() == 0


def test_dp_compute_proto_concat():
    group = make_group(2)
    data = DataProto(batch={"x": np.arange(6)}, non_tensor_batch={"s": np.asarray(list("abcdef"), dtype=object)})
    out = group.echo(data)
    assert len(out) == 6
    assert out.batch["x"].tolist() == list(range(6))


def test_dp_compute_proto_auto_padding():
    group = make_group(2)
    data = DataProto(batch={"x": np.arange(5)})
    out = group.echo(data)
    assert len(out) == 5
    assert out.batch["x"].tolist() == [0, 1, 2, 3, 4]


def test_dp_compute_per_rank_args():
    group = make_group(2)
    assert group.add_one([1, 10]) == [2, 11]


def test_failure_injection():
    group = make_group(2)
    group.workers[1].fail_once()
    with pytest.raises(RuntimeError, match="injected"):
        group.who()


def test_collectives():
    group = make_group(3)
    values = [np.asarray([1.0]), np.asarray([2.0]), np.asarray([3.0])]
    assert np.allclose(group.all_reduce_values(values, "mean"), [2.0])
    assert np.allclose(group.all_reduce_values(values, "sum"), [6.0])
    assert np.allclose(group.all_reduce_values(values, "max"), [3.0])
    assert len(group.all_gather_values(values)) == 3

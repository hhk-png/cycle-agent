"""Worker-group dispatch, FSDP flat sharding and Megatron-style TP sharding."""

import numpy as np

from _common import header

from mini_verl.protocol import DataProto
from mini_verl.sharding import fsdp_gather, fsdp_shard, tp_gather_state_dict, tp_shard_state_dict
from mini_verl.single_controller.decorator import Dispatch, register
from mini_verl.single_controller.worker import Worker
from mini_verl.single_controller.worker_group import LocalWorkerGroup, ResourcePool


class EchoWorker(Worker):
    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def who(self):
        return self.rank

    @register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO)
    def echo(self, data):
        return data


def main() -> None:
    header("Dispatch + sharding: the same DataProto crosses ranks")
    group = LocalWorkerGroup(ResourcePool.single_node(3), EchoWorker)
    print("ONE_TO_ALL:", group.who())
    data = DataProto(batch={"x": np.arange(10)})
    out = group.echo(data)
    print("DP_COMPUTE_PROTO length:", len(out), "values:", out.batch["x"].tolist())

    state = {"a": np.arange(8, dtype=np.float32), "b": np.arange(6, dtype=np.float32)}
    shards = fsdp_shard(state, 4)
    print("FSDP shard sizes:", [shard["__flat__"].size for shard in shards])
    restored = fsdp_gather(shards)
    print("FSDP roundtrip:", all(np.allclose(restored[k], v) for k, v in state.items()))

    attention = {
        "blocks.0.attn.c_attn.weight": np.arange(24, dtype=np.float32).reshape(4, 6),
        "blocks.0.attn.c_proj.weight": np.arange(24, dtype=np.float32).reshape(6, 4),
    }
    tp_shards = tp_shard_state_dict(attention, 2)
    print(
        "TP shard shapes:",
        {name: value.shape for name, value in tp_shards[0].items() if not name.startswith("__")},
    )
    merged = tp_gather_state_dict(tp_shards)
    print("TP roundtrip:", all(np.allclose(merged[k], v) for k, v in attention.items()))


if __name__ == "__main__":
    main()

from mini_megatron.parallel import comm


def test_portable_group_keys_are_unique_for_same_rank_set():
    # No messages are sent here; the queues are only needed by collectives.
    comm.initialize_portable(0, 4, None, [None] * 4)
    cp_like = comm.new_group([0, 2])
    pp_like = comm.new_group([0, 2])
    assert cp_like.key != pp_like.key
    comm.destroy()


def test_default_group_rank_is_the_global_rank():
    # ``group=None`` means the world group.  Its group rank must be the global
    # rank; returning 0 would make reduce-scatter copy chunk 0 on every rank.
    comm.initialize_portable(1, 4, None, [None] * 4)
    assert comm.get_rank() == 1
    assert comm.get_group_size(None) == 4
    assert comm.get_group_rank(None) == 1
    comm.destroy()

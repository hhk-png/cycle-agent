"""``Block`` / ``Chunk`` 测试 —— 流水线上流动的两种单位。

``Block`` 既是"数据块"也是"header"(把数据清空就是 header);``Chunk`` 是
header + 一批真正的列。这个文件把两者的**不变量**钉死:

* 块内所有列的行数必须相同(``check_number_of_rows``);``Chunk`` 在构造时
  就会校验行长与列数;
* ``Chunk`` 的"零列空壳"是**合法**的例外(``clone_empty``)—— 那是
  "下一个 chunk 该长什么样"的模板;
* ``Block.permute`` 是**就地**操作(会在循环里踩坑),``take`` 才返回新块;
* ``ColumnWithTypeAndName`` 的相等只看**名字 + 类型**,不看数据。
"""

from __future__ import annotations

import numpy as np
import pytest

from minich.block import (
    Block,
    BlockInfo,
    Chunk,
    ColumnWithTypeAndName,
    block_from_chunks,
    chunk_from_block,
)
from minich.columns import ColumnString, ColumnVector
from minich.errors import ErrorCode, MiniChError
from minich.types import get_data_type


def _cwt(name, type_name, values):
    """造一个 ``ColumnWithTypeAndName``(按类型建列再填值)。"""
    t = get_data_type(type_name)
    col = t.create_column()
    col.append_many(values)
    return ColumnWithTypeAndName(name, t, col)


# ══════════════════════════════════════════════════════════════════════════
# ColumnWithTypeAndName
# ══════════════════════════════════════════════════════════════════════════


def test_cwtn_equality_ignores_data():
    """相等只看 name + type —— column 只是"当前的物化方式"。"""
    a = _cwt("x", "UInt8", [1, 2, 3])
    b = _cwt("x", "UInt8", [9, 9])
    assert a == b
    assert a != _cwt("y", "UInt8", [1])


def test_cwtn_equality_checks_type():
    assert _cwt("x", "UInt8", [1]) != _cwt("x", "UInt16", [1])


def test_cwtn_clone_empty_keeps_type_drops_rows():
    a = _cwt("x", "UInt8", [1, 2])
    e = a.clone_empty()
    assert e.name == "x"
    assert e.type.get_name() == "UInt8"
    assert e.column.size() == 0
    assert a.is_empty() is False
    assert e.is_empty() is True


# ══════════════════════════════════════════════════════════════════════════
# Block 基本属性
# ══════════════════════════════════════════════════════════════════════════


def _sample_block():
    return Block([
        _cwt("id", "UInt32", [1, 2, 3]),
        _cwt("name", "String", ["a", "b", "c"]),
    ])


def test_block_rows_and_columns():
    b = _sample_block()
    assert b.rows() == 3
    assert b.columns() == 2
    assert b.get_names() == ["id", "name"]
    assert b.is_empty() is False
    assert len(b) == 3


def test_empty_block_has_zero_rows():
    b = Block()
    assert b.rows() == 0
    assert b.columns() == 0
    assert b.is_empty() is True


def test_check_number_of_rows_detects_mismatch():
    b = Block([_cwt("a", "UInt8", [1, 2]), _cwt("b", "UInt8", [1])])
    with pytest.raises(MiniChError) as ei:
        b.check_number_of_rows()
    assert ei.value.code == ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH
    # 全空的块是"一致"的
    Block().check_number_of_rows()


def test_get_by_position_and_name():
    b = _sample_block()
    assert b.get_by_position(0).name == "id"
    assert b.get_by_name("name").type.get_name() == "String"
    with pytest.raises(MiniChError) as ei:
        b.get_by_position(5)
    assert ei.value.code == ErrorCode.NOT_FOUND_COLUMN_IN_BLOCK
    with pytest.raises(MiniChError) as ei2:
        b.get_by_name("nope")
    assert ei2.value.code == ErrorCode.NOT_FOUND_COLUMN_IN_BLOCK


def test_try_get_by_name_and_contains():
    b = _sample_block()
    assert b.try_get_by_name("id") is not None
    assert b.try_get_by_name("x") is None
    assert b.get_position_by_name("name") == 1
    assert b.get_position_by_name("x") == -1
    assert "id" in b
    assert b.has("name")
    assert not b.has("x")


def test_add_column_requires_matching_row_count():
    b = _sample_block()
    b.add_column(_cwt("extra", "UInt8", [9, 9, 9]))
    assert b.columns() == 3
    with pytest.raises(MiniChError) as ei:
        b.add_column(_cwt("bad", "UInt8", [1]))
    assert ei.value.code == ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH


def test_erase_and_insert_column():
    b = _sample_block()
    b.erase("id")
    assert b.get_names() == ["name"]
    b.insert_column(0, _cwt("id2", "UInt8", [1, 2, 3]))
    assert b.get_names() == ["id2", "name"]


def test_resize_and_clear():
    b = _sample_block()
    b.resize(5)
    assert b.rows() == 5
    assert b.get_by_name("name").column.to_list() == ["a", "b", "c", "", ""]
    b.clear()
    assert b.rows() == 0
    assert b.get_names() == ["id", "name"]     # 结构保留


# ══════════════════════════════════════════════════════════════════════════
# Block 复制
# ══════════════════════════════════════════════════════════════════════════


def test_clone_empty_is_header():
    b = _sample_block()
    h = b.clone_empty()
    assert h.rows() == 0
    assert h.get_names() == ["id", "name"]
    assert h.get_by_name("id").type.get_name() == "UInt32"


def test_clone_empty_without_columns_only_keeps_info():
    b = Block([_cwt("x", "UInt8", [1])], BlockInfo(True, 3))
    h = b.clone_empty_without_columns()
    assert h.columns() == 0
    assert h.info.is_overflows is True
    assert h.info.bucket_num == 3


def test_clone_with_columns_is_deep_copy():
    b = _sample_block()
    c = b.clone_with_columns()
    c.get_by_name("id").column.append(99)
    assert b.rows() == 3      # 原件不变
    assert c.rows() == 4


def test_clone_without_column_and_with_column():
    b = _sample_block()
    b2 = b.clone_without_column("id")
    assert b2.get_names() == ["name"]
    assert b2.rows() == 0
    b3 = b.clone_with_column(_cwt("extra", "UInt8", [0, 0, 0]))
    assert b3.get_names() == ["id", "name", "extra"]


# ══════════════════════════════════════════════════════════════════════════
# Block 变换:filter / permute / take / slice / merge
# ══════════════════════════════════════════════════════════════════════════


def test_block_filter_in_place():
    b = _sample_block()
    r = b.filter([True, False, True])
    assert r is b
    assert b.rows() == 2
    assert b.get_by_name("id").column.to_list() == [1, 3]
    assert b.get_by_name("name").column.to_list() == ["a", "c"]


def test_block_filter_wrong_size_raises():
    b = _sample_block()
    with pytest.raises(MiniChError) as ei:
        b.filter([True])
    assert ei.value.code == ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH


def test_block_permute_is_in_place():
    """**就地**重排 —— 循环里反复对同一个 Block permute 会越界(见 regressions)。"""
    b = _sample_block()
    r = b.permute([2, 0, 1])
    assert r is b
    assert b.get_by_name("id").column.to_list() == [3, 1, 2]


def test_block_take_returns_new_block():
    b = _sample_block()
    out = b.take([2, 0])
    assert out is not b
    assert out.get_by_name("id").column.to_list() == [3, 1]
    assert b.rows() == 3     # 原件不变


def test_block_slice_returns_copy():
    b = _sample_block()
    out = b.slice(1, 2)
    assert out.get_by_name("id").column.to_list() == [2, 3]
    assert out.rows() == 2
    assert b.rows() == 3
    with pytest.raises(MiniChError) as ei:
        b.slice(2, 5)
    assert ei.value.code == ErrorCode.ARGUMENT_OUT_OF_BOUND


def test_block_merge_appends_rows():
    a = _sample_block()
    b = _sample_block()
    a.merge(b)
    assert a.rows() == 6
    assert a.get_by_name("id").column.to_list() == [1, 2, 3, 1, 2, 3]


def test_block_merge_into_empty():
    a = Block()
    a.merge(_sample_block())
    assert a.rows() == 3
    assert a.get_names() == ["id", "name"]


def test_block_merge_incompatible_raises():
    a = _sample_block()
    with pytest.raises(MiniChError) as ei:
        a.merge(Block([_cwt("only", "UInt8", [1, 2, 3])]))
    assert ei.value.code == ErrorCode.INCOMPATIBLE_COLUMNS


def test_block_merge_type_mismatch_raises():
    a = Block([_cwt("x", "UInt8", [1])])
    b = Block([_cwt("x", "UInt16", [1])])
    with pytest.raises(MiniChError):
        a.merge(b)


# ══════════════════════════════════════════════════════════════════════════
# Chunk
# ══════════════════════════════════════════════════════════════════════════


def test_chunk_num_rows_from_first_column():
    header = _sample_block().clone_empty()
    ch = Chunk(header, [ColumnVector(np.uint32, [1, 2, 3]), ColumnString(["a", "b", "c"])])
    assert ch.num_rows == 3
    assert len(ch) == 3
    assert ch.get_column_by_position(0).to_list() == [1, 2, 3]
    assert ch.get_column_by_name("name").to_list() == ["a", "b", "c"]


def test_chunk_rejects_inconsistent_column_sizes():
    header = _sample_block().clone_empty()
    with pytest.raises(MiniChError) as ei:
        Chunk(header, [ColumnVector(np.uint32, [1, 2]), ColumnString(["a"])])
    assert ei.value.code == ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH


def test_chunk_rejects_num_rows_mismatch():
    header = _sample_block().clone_empty()
    with pytest.raises(MiniChError) as ei:
        Chunk(header, [ColumnVector(np.uint32, [1, 2, 3])], num_rows=2)
    assert ei.value.code == ErrorCode.LOGICAL_ERROR


def test_chunk_rejects_column_count_mismatch_with_header():
    header = _sample_block().clone_empty()   # 2 列
    with pytest.raises(MiniChError) as ei:
        Chunk(header, [ColumnVector(np.uint32, [1, 2, 3])])   # 只给 1 列
    assert ei.value.code == ErrorCode.LOGICAL_ERROR


def test_chunk_clone_empty_zero_column_exception():
    """**零列的 "空壳" chunk 是合法的** —— 它只是"下一个 chunk 该长什么样"。"""
    header = _sample_block().clone_empty()
    empty = Chunk(header, [], 0)
    assert empty.num_rows == 0
    assert empty.get_columns() == []
    # 即便 header 声明了 2 列,零列的空壳也不报错
    assert empty.get_header().columns() == 2


def test_chunk_get_block_with_data_roundtrip():
    header = _sample_block().clone_empty()
    ch = Chunk(header, [ColumnVector(np.uint32, [1, 2, 3]), ColumnString(["a", "b", "c"])])
    blk = ch.get_block_with_data()
    assert blk.rows() == 3
    assert blk.get_names() == ["id", "name"]
    assert blk.get_by_name("name").column.to_list() == ["a", "b", "c"]


def test_chunk_clone_is_independent():
    header = _sample_block().clone_empty()
    ch = Chunk(header, [ColumnVector(np.uint32, [1, 2, 3]), ColumnString(["a", "b", "c"])])
    clone = ch.clone()
    clone.get_column_by_position(0).append(9)
    # ``num_rows`` 是构造时定下的,不随列变化;靠底层列来验证深拷贝
    assert ch.get_column_by_position(0).to_list() == [1, 2, 3]
    assert clone.get_column_by_position(0).to_list() == [1, 2, 3, 9]
    assert clone.num_rows == 3     # num_rows 是元数据,不受 append 影响


def test_chunk_filter_and_permute_and_slice():
    header = _sample_block().clone_empty()
    ch = Chunk(header, [ColumnVector(np.uint32, [10, 20, 30]), ColumnString(["x", "y", "z"])])
    filtered = ch.filter([True, False, True])
    assert filtered.num_rows == 2
    assert filtered.get_column_by_position(0).to_list() == [10, 30]

    header2 = _sample_block().clone_empty()
    ch2 = Chunk(header2, [ColumnVector(np.uint32, [1, 2, 3]), ColumnString(["a", "b", "c"])])
    permuted = ch2.permute([2, 0, 1])
    assert permuted.get_column_by_position(0).to_list() == [3, 1, 2]

    header3 = _sample_block().clone_empty()
    ch3 = Chunk(header3, [ColumnVector(np.uint32, [1, 2, 3, 4]), ColumnString(["a", "b", "c", "d"])])
    sliced = ch3.slice(1, 2)
    assert sliced.num_rows == 2
    assert sliced.get_column_by_position(0).to_list() == [2, 3]


# ══════════════════════════════════════════════════════════════════════════
# chunk <-> block 桥接
# ══════════════════════════════════════════════════════════════════════════


def test_chunk_from_block_and_back():
    b = _sample_block()
    ch = chunk_from_block(b)
    assert ch.num_rows == 3
    back = ch.get_block_with_data()
    assert back.get_by_name("id").column.to_list() == [1, 2, 3]


def test_block_from_chunks_concatenates():
    b = _sample_block()
    ch1 = chunk_from_block(b)
    ch2 = chunk_from_block(b)
    merged = block_from_chunks([ch1, ch2])
    assert merged.rows() == 6
    assert merged.get_by_name("id").column.to_list() == [1, 2, 3, 1, 2, 3]


def test_block_from_empty_chunks_returns_empty_block():
    out = block_from_chunks([])
    assert out.is_empty()
    assert out.rows() == 0


def test_block_info_clone():
    info = BlockInfo(True, 5)
    c = info.clone()
    assert c.is_overflows is True
    assert c.bucket_num == 5
    assert c is not info

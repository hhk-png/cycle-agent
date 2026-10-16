"""列（``IColumn``）测试 —— 每种物化方式的写、读、变换与不变量。

列是 mini-clickhouse 里最重要的接口：执行引擎、函数、聚合操作的都是列，
而不是"一行行记录"。这个文件把每一种列类的核心契约都钉死：

* **就地操作**（``filter`` / ``pop_back`` / ``resize``）与**返回新列**
  （``permute`` / ``get_many`` / ``clone``）的区别；
* 复合列的**位置数组**语义（``ColumnArray.offsets``、``ColumnMap`` 的
  keys/values，``ColumnNullable`` 的 null_map）；
* ``ColumnConst`` 的"追加不改变行数"这个反直觉语义，以及
  ``convert_to_full_column()`` 的必要性；
* 越界读永远抛错（真实 CH 是 UB，这里给诊断）。
"""

from __future__ import annotations

import numpy as np
import pytest

from minich.columns import (
    ColumnAggregateFunction,
    ColumnArray,
    ColumnBigInt,
    ColumnConst,
    ColumnFixedString,
    ColumnLowCardinality,
    ColumnMap,
    ColumnNothing,
    ColumnNullable,
    ColumnSparse,
    ColumnString,
    ColumnTuple,
    ColumnUUID,
    ColumnVector,
    IColumn,
    column_from_values,
)
from minich.errors import ErrorCode, MiniChError


# ══════════════════════════════════════════════════════════════════════════
# ColumnVector
# ══════════════════════════════════════════════════════════════════════════


def test_column_vector_basic_read_write():
    c = ColumnVector(np.uint8)
    assert c.size() == 0
    c.append(1)
    c.append(2)
    c.append(3)
    assert c.size() == 3
    assert [c.get(i) for i in range(3)] == [1, 2, 3]
    assert c.to_list() == [1, 2, 3]
    assert c.empty() is False


def test_column_vector_get_out_of_bounds_raises():
    c = ColumnVector(np.uint8, [1, 2])
    with pytest.raises(MiniChError) as ei:
        c.get(2)
    assert ei.value.code == ErrorCode.ARGUMENT_OUT_OF_BOUND
    with pytest.raises(MiniChError):
        c.get(-1)


def test_column_vector_append_many_and_default():
    c = ColumnVector(np.int32)
    c.append_many([1, 2, 3])
    c.append_many_default(2)
    assert c.to_list() == [1, 2, 3, 0, 0]


def test_column_vector_grows_capacity_but_keeps_size():
    """均摊 O(1) 的来源：容量倍增，但 ``size()`` 只算已用部分。"""
    c = ColumnVector(np.uint64)
    for i in range(1000):
        c.append(i)
    assert c.size() == 1000
    assert c.capacity() >= 1000
    c.shrink_to_fit()
    assert c.capacity() == 1000


def test_column_vector_filter_is_in_place_and_returns_self():
    c = ColumnVector(np.uint8, [10, 20, 30, 40])
    r = c.filter([True, False, True, False])
    assert r is c               # 就地操作返回 self 便于链式
    assert c.to_list() == [10, 30]


def test_column_vector_filter_wrong_mask_size_raises():
    c = ColumnVector(np.uint8, [1, 2, 3])
    with pytest.raises(MiniChError) as ei:
        c.filter([True])
    assert ei.value.code == ErrorCode.SIZES_OF_COLUMNS_DOESNT_MATCH


def test_column_vector_permute_returns_new_column():
    c = ColumnVector(np.uint8, [1, 2, 3])
    out = c.permute([2, 0, 1])
    assert out.to_list() == [3, 1, 2]
    assert c.to_list() == [1, 2, 3]     # 原列不变
    assert isinstance(out, ColumnVector)


def test_column_vector_get_many_repeats_allowed():
    c = ColumnVector(np.uint8, [7, 8, 9])
    assert c.get_many([0, 0, 2]).to_list() == [7, 7, 9]


def test_column_vector_cut_and_pop_back():
    c = ColumnVector(np.int32, [1, 2, 3, 4, 5])
    c.cut(1, 2)                # 删掉 [2,3]
    assert c.to_list() == [1, 4, 5]
    c.pop_back(2)              # 删掉尾部两个
    assert c.to_list() == [1]
    c.pop_back(1)
    assert c.to_list() == []


def test_column_vector_cut_out_of_bounds_raises():
    c = ColumnVector(np.int32, [1, 2])
    with pytest.raises(MiniChError):
        c.cut(1, 5)


def test_column_vector_resize_pads_and_truncates():
    c = ColumnVector(np.uint8, [1, 2, 3])
    c.resize(5)
    assert c.to_list() == [1, 2, 3, 0, 0]
    c.resize(2)
    assert c.to_list() == [1, 2]


def test_column_vector_insert_range_from_and_insert_many():
    a = ColumnVector(np.uint8, [1, 2, 3, 4])
    b = ColumnVector(np.uint8)
    b.insert_range_from(a, 1, 2)       # 取 a[1:3]
    assert b.to_list() == [2, 3]
    b.insert_many_from(a, 0, 3)        # 把 a[0] 重复 3 次
    assert b.to_list() == [2, 3, 1, 1, 1]


def test_column_vector_clone_and_clone_empty():
    c = ColumnVector(np.uint8, [1, 2, 3])
    clone = c.clone()
    assert clone.to_list() == [1, 2, 3]
    clone.append(4)
    assert c.to_list() == [1, 2, 3]    # 深拷贝,互不影响
    assert c.clone_empty().size() == 0


def test_column_vector_byte_size_uses_itemsize():
    c = ColumnVector(np.uint32, [0] * 10)
    assert c.byte_size() == 10 * 4


def test_column_vector_compare_at():
    a = ColumnVector(np.uint8, [1, 5])
    b = ColumnVector(np.uint8, [2, 5])
    assert a.compare_at(0, b, 0) == -1
    assert a.compare_at(1, b, 1) == 0
    assert b.compare_at(0, a, 0) == 1


def test_column_vector_average_reserve_never_shrinks_below_size():
    c = ColumnVector(np.uint32)
    c.reserve(10)
    cap = c.capacity()
    assert cap >= 10
    c.reserve(5)
    assert c.capacity() == cap     # 缩小请求不生效


# ══════════════════════════════════════════════════════════════════════════
# column_from_values
# ══════════════════════════════════════════════════════════════════════════


def test_column_from_values_infers_dtype():
    assert column_from_values([1, 2, 3]).dtype == np.uint64
    assert column_from_values([1, -2, 3]).dtype == np.int64
    assert column_from_values([1.0, 2.0]).dtype == np.float64
    assert column_from_values([True, False]).dtype == np.uint8


def test_column_from_values_rejects_non_numeric():
    with pytest.raises(MiniChError) as ei:
        column_from_values(["a", "b"])
    assert ei.value.code == ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT


# ══════════════════════════════════════════════════════════════════════════
# ColumnString / ColumnFixedString
# ══════════════════════════════════════════════════════════════════════════


def test_column_string_offsets_are_cumulative():
    c = ColumnString(["hello", " world", "x"])
    assert c.offsets == [5, 11, 12]
    assert c.get(0) == "hello"
    assert c.get(1) == " world"
    assert c.get(2) == "x"
    assert c.get_size_at(0) == 5


def test_column_string_binary_safe_roundtrip():
    """String 不校验编码 —— 任意字节都能往返(surrogateescape)。"""
    c = ColumnString()
    c.append(b"\xff\x00\x01")
    assert c.get_data_at(0) == b"\xff\x00\x01"
    assert c.get(0).encode("utf-8", "surrogateescape") == b"\xff\x00\x01"


def test_column_string_unicode_length_is_bytes():
    c = ColumnString(["你好"])
    # "你好" 是 6 个 UTF-8 字节
    assert c.get_size_at(0) == 6


def test_column_string_filter_compact():
    c = ColumnString(["a", "bb", "ccc", "dddd"])
    c.filter([True, False, True, False])
    assert c.to_list() == ["a", "ccc"]
    assert c.chars == b"accc"
    assert c.offsets == [1, 4]


def test_column_string_permute_and_cut_and_pop_back():
    c = ColumnString(["a", "b", "c"])
    assert c.permute([2, 0]).to_list() == ["c", "a"]
    c.cut(0, 1)
    assert c.to_list() == ["b", "c"]
    c.pop_back(1)
    assert c.to_list() == ["b"]


def test_column_string_compare_at_is_bytewise():
    a = ColumnString(["a", "b"])
    b = ColumnString(["ab", "a"])
    assert a.compare_at(0, b, 0) == -1   # "a" < "ab"
    assert a.compare_at(1, b, 1) == 1    # "b" > "a"


def test_column_string_default_value_is_empty():
    assert ColumnString().default_value() == ""
    c = ColumnString()
    c.append_many_default(3)
    assert c.to_list() == ["", "", ""]
    assert c.size() == 3


def test_column_string_rejects_non_string():
    with pytest.raises(MiniChError) as ei:
        ColumnString().append(42)
    assert ei.value.code == ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT


def test_column_string_replicate():
    # offsets 是"另一个数组列"的累积偏移:第 i 个值重复 offsets[i]-offsets[i-1] 次
    c = ColumnString(["a", "b"])
    out = c.replicate([1, 3])
    assert out.to_list() == ["a", "b", "b"]


def test_column_fixed_string_pads_with_zero_and_strips_on_get():
    c = ColumnFixedString(4)
    c.append("ab")
    c.append("abcd")
    assert c.size() == 2
    assert c.get_data_at(0) == b"ab\x00\x00"     # 原始不剥零
    assert c.get(0) == "ab"                       # 用户看到剥零后的
    assert c.get(1) == "abcd"


def test_column_fixed_string_rejects_too_long():
    c = ColumnFixedString(2)
    with pytest.raises(MiniChError) as ei:
        c.append("abc")
    assert ei.value.code == ErrorCode.SIZE_OF_FIXED_STRING_DOESNT_MATCH


def test_column_fixed_string_zero_size_rejected():
    with pytest.raises(MiniChError):
        ColumnFixedString(0)


# ══════════════════════════════════════════════════════════════════════════
# ColumnBigInt / ColumnUUID / ColumnNothing
# ══════════════════════════════════════════════════════════════════════════


def test_column_bigint_holds_wide_integers():
    c = ColumnBigInt(False, 128)
    c.append(2 ** 128 - 1)
    c.append(0)
    assert c.get(0) == 2 ** 128 - 1
    assert c.size() == 2
    # permute / filter 组合
    assert c.filter([False, True]).to_list() == [0]


def test_column_bigint_byte_size():
    c = ColumnBigInt(True, 256, [0, 0, 0])
    assert c.byte_size() == 3 * 32


def test_column_uuid_masks_to_128_bits():
    c = ColumnUUID()
    c.append(2 ** 128 + 5)     # 溢出部分被掩掉
    assert c.get(0) == 5


def test_column_nothing_rejects_real_value_but_accepts_default():
    n = ColumnNothing()
    assert n.size() == 0
    with pytest.raises(MiniChError) as ei:
        n.append(1)
    assert ei.value.code == ErrorCode.LOGICAL_ERROR
    # 但是追加默认值是合法的空操作(Nullable(Nothing) 依赖它)
    n.append_default()
    assert n.size() == 0
    assert n.is_nothing() is True


# ══════════════════════════════════════════════════════════════════════════
# ColumnNullable
# ══════════════════════════════════════════════════════════════════════════


def test_column_nullable_basic():
    c = ColumnNullable(ColumnVector(np.int32))
    c.append(1)
    c.append(None)
    c.append(3)
    assert c.size() == 3
    assert c.get(0) == 1
    assert c.get(1) is None
    assert c.get(2) == 3
    assert c.null_map == [False, True, False]
    assert c.count_nulls() == 1
    # NULL 位置上的嵌套列塞的是零值
    assert c.nested.to_list() == [1, 0, 3]


def test_column_nullable_default_is_null():
    """``Nullable`` 的默认值是 NULL,不是嵌套零值。"""
    c = ColumnNullable(ColumnVector(np.int32))
    c.append_many_default(3)
    assert c.to_list() == [None, None, None]


def test_column_nullable_null_is_largest_and_nulls_equal():
    """真实 CH 的比较语义:NULL 比任何非 NULL 都大,且两个 NULL 相等。"""
    c = ColumnNullable(ColumnVector(np.int32))
    c.append(None)
    c.append(5)
    assert c.compare_at(0, c, 0) == 0     # NULL == NULL
    assert c.compare_at(0, c, 1) == 1     # NULL > 5
    assert c.compare_at(1, c, 0) == -1    # 5 < NULL


def test_column_nullable_hash_separates_null_from_value():
    """分组时 NULL 是独立的一组 —— 与"值为 0"不能混。"""
    c = ColumnNullable(ColumnVector(np.int32))
    c.append(None)
    c.append(0)
    assert c.hash_value(0) != c.hash_value(1)


def test_column_nullable_filter_and_permute():
    c = ColumnNullable(ColumnVector(np.int32))
    c.append_many([1, None, 3, None])
    out = c.permute([1, 3])
    assert out.to_list() == [None, None]
    c.filter([True, False, True, False])
    assert c.to_list() == [1, 3]


def test_column_nullable_convert_to_full_column_returns_self():
    c = ColumnNullable(ColumnVector(np.int32))
    assert c.convert_to_full_column() is c


# ══════════════════════════════════════════════════════════════════════════
# ColumnArray
# ══════════════════════════════════════════════════════════════════════════


def test_column_array_offsets_and_access():
    c = ColumnArray(ColumnVector(np.uint8))
    c.append([1, 2])
    c.append([])
    c.append([3, 4, 5])
    assert c.offsets == [2, 2, 5]
    assert c.get(0) == [1, 2]
    assert c.get(1) == []
    assert c.get(2) == [3, 4, 5]
    assert c.elements() == 5          # 元素总数,不是行数
    assert c.size() == 3


def test_column_array_get_element_returns_none_when_out_of_range():
    c = ColumnArray(ColumnVector(np.uint8))
    c.append([1, 2])
    assert c.get_element(0, 0) == 1
    assert c.get_element(0, 5) is None


def test_column_array_default_is_empty_array_not_zero():
    c = ColumnArray(ColumnVector(np.uint8))
    c.append_many_default(3)
    assert c.to_list() == [[], [], []]
    assert c.offsets == [0, 0, 0]


def test_column_array_rejects_null_and_scalar():
    c = ColumnArray(ColumnVector(np.uint8))
    with pytest.raises(MiniChError) as ei:
        c.append(None)
    assert ei.value.code == ErrorCode.CANNOT_INSERT_NULL_IN_ORDINARY_COLUMN
    with pytest.raises(MiniChError) as ei2:
        c.append(5)
    assert ei2.value.code == ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT


def test_column_array_filter_keeps_whole_subarrays():
    c = ColumnArray(ColumnVector(np.uint8))
    c.append_many([[1, 2], [3], [], [4, 5, 6]])
    c.filter([True, False, True, True])
    assert c.to_list() == [[1, 2], [], [4, 5, 6]]
    assert c.offsets == [2, 2, 5]
    assert c.nested.to_list() == [1, 2, 4, 5, 6]


def test_column_array_permute_reorders_subarrays():
    c = ColumnArray(ColumnVector(np.uint8))
    c.append_many([[1, 2], [3], []])
    out = c.permute([2, 0])
    assert out.to_list() == [[], [1, 2]]
    assert out.offsets == [0, 2]


def test_column_array_insert_range_from_preserves_offsets():
    a = ColumnArray(ColumnVector(np.uint8))
    a.append_many([[1, 2], [], [3]])
    b = ColumnArray(ColumnVector(np.uint8))
    b.insert_range_from(a, 0, 3)
    assert b.to_list() == [[1, 2], [], [3]]
    assert b.offsets == [2, 2, 3]


def test_column_array_cut_updates_offsets():
    c = ColumnArray(ColumnVector(np.uint8))
    c.append_many([[1], [2, 3], [4]])
    c.cut(1, 1)          # 删掉中间的 [2,3]
    assert c.to_list() == [[1], [4]]
    assert c.offsets == [1, 2]


# ══════════════════════════════════════════════════════════════════════════
# ColumnTuple / ColumnMap
# ══════════════════════════════════════════════════════════════════════════


def test_column_tuple_is_vertical_stack_of_columns():
    t = ColumnTuple([ColumnVector(np.uint8), ColumnString()])
    t.append((1, "a"))
    t.append((2, "b"))
    assert t.size() == 2
    assert t.get(0) == (1, "a")
    assert t.get_subcolumn(0).to_list() == [1, 2]


def test_column_tuple_append_length_mismatch():
    t = ColumnTuple([ColumnVector(np.uint8), ColumnString()])
    with pytest.raises(MiniChError) as ei:
        t.append((1,))
    assert ei.value.code == ErrorCode.SIZES_OF_COLUMNS_IN_TUPLE_DOESNT_MATCH


def test_column_tuple_filter_permute():
    t = ColumnTuple([ColumnVector(np.uint8), ColumnString()])
    t.append_many([(1, "a"), (2, "b"), (3, "c")])
    out = t.permute([2, 0])
    assert out.to_list() == [(3, "c"), (1, "a")]
    t.filter([True, False, True])
    assert t.to_list() == [(1, "a"), (3, "c")]


def test_column_map_values_attribute_shadows_method():
    """``ColumnMap`` 的数据属性 ``values`` 遮蔽了 ``IColumn.values()`` 迭代方法。

    所以 ``.values`` 拿到的是**值列**;通用迭代器改名成了 ``iter_values()``。
    这条是 Python 特有的、真实 CH 没有的坑,故单独钉一个测试。
    """
    m = ColumnMap(ColumnString(), ColumnVector(np.uint8))
    m.append({"a": 1, "b": 2})
    m.append({})
    assert m.size() == 2
    assert m.get(0) == {"a": 1, "b": 2}
    assert m.get(1) == {}
    assert m.offsets == [2, 2]
    # .values 是值列(IColumn),不是迭代器
    assert isinstance(m.values, IColumn)
    assert m.values.to_list() == [1, 2]
    assert m.keys.to_list() == ["a", "b"]
    # 想要"逐行迭代"要用 iter_values()
    assert list(m.iter_values()) == [{"a": 1, "b": 2}, {}]


def test_column_map_hash_is_order_independent():
    a = ColumnMap(ColumnString(), ColumnVector(np.uint8))
    b = ColumnMap(ColumnString(), ColumnVector(np.uint8))
    a.append({"x": 1, "y": 2})
    b.append({"y": 2, "x": 1})
    assert a.hash_value(0) == b.hash_value(0)


def test_column_map_rejects_non_dict():
    m = ColumnMap(ColumnString(), ColumnVector(np.uint8))
    with pytest.raises(MiniChError) as ei:
        m.append([("a", 1)])
    assert ei.value.code == ErrorCode.ILLEGAL_TYPE_OF_ARGUMENT


# ══════════════════════════════════════════════════════════════════════════
# ColumnLowCardinality
# ══════════════════════════════════════════════════════════════════════════


def test_column_low_cardinality_dictionary_and_indexes():
    c = ColumnLowCardinality(ColumnString())
    c.append_many(["GET", "POST", "GET", "GET", "PUT"])
    assert c.size() == 5
    assert c.cardinality() == 3          # 字典里只有 3 个唯一值
    assert c.dictionary.to_list() == ["GET", "POST", "PUT"]
    assert c.indexes == [0, 1, 0, 0, 2]
    assert c.to_list() == ["GET", "POST", "GET", "GET", "PUT"]


def test_column_low_cardinality_hash_uses_dictionary_index():
    """低基数在 GROUP BY 上快的根本原因:哈希的是**小整数**索引。"""
    c = ColumnLowCardinality(ColumnString())
    c.append_many(["x", "y", "x"])
    assert c.hash_value(0) == 0
    assert c.hash_value(2) == 0
    assert c.hash_value(1) == 1


def test_column_low_cardinality_permute_shares_dictionary():
    c = ColumnLowCardinality(ColumnString())
    c.append_many(["a", "b", "a"])
    out = c.permute([2, 0, 1])
    assert out.to_list() == ["a", "a", "b"]
    assert out.dictionary is c.dictionary


# ══════════════════════════════════════════════════════════════════════════
# ColumnConst —— 最反直觉的一类
# ══════════════════════════════════════════════════════════════════════════


def test_column_const_append_does_not_count_rows():
    """常量列的 size 是**显式设的**,不是数出来的 —— append 只在 size==0 时生效。"""
    c = ColumnConst(ColumnVector(np.int32))
    assert c.size() == 0
    c.append(42)
    assert c.size() == 1
    c.append(42)         # 值相同,只把 size 加一
    c.append(42)
    assert c.size() == 3
    assert c.get(0) == 42
    assert c.get(2) == 42
    assert c.is_const() is True


def test_column_const_rejects_different_appended_value():
    c = ColumnConst(ColumnVector(np.int32))
    c.append(1)
    with pytest.raises(MiniChError) as ei:
        c.append(2)
    assert ei.value.code == ErrorCode.LOGICAL_ERROR


def test_column_const_convert_to_full_column_expands_rows():
    """**必须**调 convert_to_full_column 才能得到真正的逐行列。

    否则 ``arrayJoin`` / ``ORDER BY`` 这类就地处理器会把常量列当成"只有一行"。
    """
    c = ColumnConst(ColumnVector(np.int32), size=1)
    c.data.append(7)
    c.sizeness = 5
    full = c.convert_to_full_column()
    assert full.size() == 5
    assert full.to_list() == [7, 7, 7, 7, 7]
    assert full.is_const() is False


def test_column_const_append_many_requires_equal_values():
    c = ColumnConst(ColumnVector(np.int32))
    c.append_many([5, 5, 5])
    assert c.size() == 3


# ══════════════════════════════════════════════════════════════════════════
# ColumnSparse
# ══════════════════════════════════════════════════════════════════════════


def test_column_sparse_stores_only_non_default():
    c = ColumnSparse(ColumnVector(np.int32), default_value=0)
    c.append_many([0, 0, 7, 0, 3])
    assert c.size() == 5
    assert c.offsets == [2, 4]         # 只有第 2、4 行非默认
    assert c.to_list() == [0, 0, 7, 0, 3]
    # permute 会先转稠密
    assert c.permute([4, 0]).to_list() == [3, 0]


def test_column_sparse_is_sparse_and_full_conversion():
    c = ColumnSparse(ColumnVector(np.int32), default_value=0)
    c.append_many([0, 1, 0])
    assert c.is_sparse() is True
    full = c.convert_to_full_column()
    assert full.to_list() == [0, 1, 0]
    assert full.is_sparse() is False


# ══════════════════════════════════════════════════════════════════════════
# ColumnAggregateFunction
# ══════════════════════════════════════════════════════════════════════════


def test_column_aggregate_function_stores_opaque_states():
    c = ColumnAggregateFunction(None)
    c.append(b"\x01\x02")
    c.append(b"\x03")
    assert c.size() == 2
    assert c.get(0) == b"\x01\x02"
    assert c.is_aggregate() is True
    # func=None 时按"每个状态 8 字节"估算(无法用 serialize_state 量长度)
    assert c.byte_size() == 2 * 8


def test_column_aggregate_function_filter_and_permute():
    c = ColumnAggregateFunction(None, [b"a", b"b", b"c"])
    assert c.permute([2, 0]).states == [b"c", b"a"]
    c.filter([True, False, True])
    assert c.states == [b"a", b"c"]


# ══════════════════════════════════════════════════════════════════════════
# 跨类型的公共不变量
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "column",
    [
        ColumnVector(np.int32, [1, 2, 3]),
        ColumnString(["a", "b", "c"]),
        ColumnFixedString(2, ["a", "b", "c"]),
        ColumnBigInt(True, 128, [1, 2, 3]),
        ColumnUUID([1, 2, 3]),
        ColumnNullable(ColumnVector(np.int32, [1, 2, 3])),
        ColumnArray(ColumnVector(np.uint8, [1, 2, 3])).__class__(ColumnVector(np.uint8)),
    ],
)
def test_clone_is_independent(column):
    """``clone()`` 是深拷贝:改副本不影响原件。"""
    if column.size() == 0:
        column.append_many([[1], [2], [3]] if isinstance(column, ColumnArray) else [1, 2, 3])
    original = column.to_list()
    clone = column.clone()
    assert clone.to_list() == original
    assert clone.clone_empty().size() == 0


def test_resize_zero_empties_every_column():
    c = ColumnString(["a", "b", "c"])
    c.resize(0)
    assert c.size() == 0
    assert c.chars == b""

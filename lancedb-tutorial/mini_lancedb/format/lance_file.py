"""Lance 文件容器（本实现对 Lance v2 文件格式的还原）。

一个 ``.lance`` 文件在字节层面的样子（从前往后）::

    ┌──────────────────────────────────────────────────────────┐
    │ Data Pages（各列的页；每个缓冲区按**绝对偏移**被引用）        │
    ├──────────────────────────────────────────────────────────┤
    │ Column Metadatas（每列的页列表、每页编码、每页 buffer 偏移）  │
    ├──────────────────────────────────────────────────────────┤
    │ Column Metadata Offset Table（每列元数据的 (偏移, 长度)）    │
    ├──────────────────────────────────────────────────────────┤
    │ Global Buffers Offset Table（本实现恒为空）                 │
    ├──────────────────────────────────────────────────────────┤
    │ Footer: u64 u64 u64 u32 u32 u16 u16 "LANC"               │  固定 40 字节
    └──────────────────────────────────────────────────────────┘

    三个 u64 依次是 column_metadata_start、column_metadata_offsets_start、
    global_buffer_offsets_start —— 和真实 Lance v2 的 footer 字段顺序一致。

    **为什么 buffer 用绝对偏移而不是相对偏移？**
    因为一页可能由 1~4 段来自文件不同位置的字节组成（值、有效性、
    字典、下标），而且这些段不保证连续。用绝对偏移，读取者拿到元数据
    就能直接发范围读；用相对偏移反而要先还原基址。
    代价是元数据体积变大 —— 这也是真实 Lance 用 FlatBuffers 而不是
    JSON 存它的原因之一（见文末"与真实格式的差异"）。

几个关键设计点，逐条对应真实 Lance v2 的选择：

**1. 元数据在尾部，footer 定长 40 字节。**
读取者只发一次 40 字节的范围读就知道整张表在哪。若元数据在头部，
必须在不知道长度的情况下反查；若 footer 变长，就得先猜长度再重读
—— 对象存储上每次多一次往返就是多几毫秒延迟。

**2. 缓冲区按绝对偏移寻址、64 字节对齐。**
页表里存"文件绝对偏移 + 长度"而不是"相对上一页"，于是任意缓冲区
都能 O(1) 定位：读页表里第 i 项（16 字节）→ 读那一段。**一次查询
要读几个缓冲区，就是几次范围读**，这个数字可以被数出来，
性能优化的第一步就是把它降下来。

**3. 类型挂在列元数据上，不集中在文件头。**
每一列的元数据项里都写了它的逻辑 ``type``（见 :meth:`LanceFileReader.schema`，
它就是从列元数据反序列化出来的），所以**容器是自描述的**：
``schema()`` 不需要外部 manifest 配合。代价是类型信息摊在每一列上，
没有一处能一眼看全表的 schema —— 想要全貌必须把根列拼起来。

**4. 嵌套列被"结构化解开"成扁平列。**
``fixed_size_list<float32>[128]`` 在文件里就是一个 128×N 值的 float32
子列；``struct`` 变成若干平列；``list`` 拆成"offsets 列 + 子列"。
这种 **structural encoding** 是 Lance 能给向量列做列级编码/压缩的根本原因。

**5. 逻辑字典类型在写入时展开。**
``dictionary`` 在真实 Lance 里是一种**物理编码**而不是逻辑类型，
所以本实现遇到 ``DictionaryArray`` 会就地解码成值列，字典化交给
编码层（``dictionary`` 编码器）去做。

**6. 与真实格式的差异（诚实声明）**

* 真实 Lance 的列元数据是 **FlatBuffers**，我们是 **JSON**。选 JSON
  是为了教程能直接把元数据打印出来看；代价是解析慢、体积大。
  FlatBuffers 的价值在于"零拷贝 + 无需解析"—— 读一个字段不用
  反序列化整个结构，这对"每次查询都要读 manifest"的场景很关键。
* 真实 Lance 把有效性位图编进页里，我们放在**列级**（语义相同，实现更简单）。
* 真实 v2 页大小默认 8 MiB、缓冲区对齐 64 字节（直通 I/O 时 4096）；
  我们默认每页 8192 个值并支持按字节切换 —— 两个阈值都能改，用来做实验。
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from ..arrays import (
    Array,
    BinaryArray,
    DictionaryArray,
    FixedSizeListArray,
    ListArray,
    MapArray,
    NullArray,
    PrimitiveArray,
    StringArray,
    StructArray,
    empty_array,
)
from ..encoding.buffer import ALIGNMENT, BufferLocation, BufferRole, align_up
from ..encoding.codecs import (
    EncodedPage,
    PageLayout,
    compress_page,
    decode_bytes,
    decode_numeric,
    decompress_page,
    encode_bytes,
    encode_numeric,
)
from ..encoding.compress import COMPRESSION_NONE
from ..errors import CorruptedFileError, NotSupportedError, SchemaError
from ..record import RecordBatch
from ..schema import Schema
from ..storage.base import ObjectStore
from ..types import (
    DataType,
    FixedSizeList,
    INT64,
    List as ListType,
    Map as MapType,
    Null as NullType,
    Struct as StructType,
)

__all__ = [
    "MAGIC",
    "FOOTER_SIZE",
    "LanceFileWriter",
    "LanceFileReader",
    "FileStatistics",
    "write_lance_file",
    "read_lance_file",
    "inspect_lance_file",
]

#: 文件尾部的魔数。真实 Lance v2 用的就是这四个字节。
MAGIC = b"LANC"
#: footer 固定长度：3×u64 + 2×u32 + 2×u16 + 4 字节魔数 = 40
FOOTER_SIZE = 3 * 8 + 2 * 4 + 2 * 2 + 4

#: footer 三个 u64 的含义（与真实 Lance v2 一致，均指向文件内绝对偏移）
#: * ``column_metadata_start`` —— 列元数据 blob 的起点
#: * ``column_metadata_offsets_start`` —— 每列元数据在 blob 内的偏移表
#: * ``global_buffer_offsets_start`` —— 全局缓冲区位置表（本实现为空）
_FOOTER_STRUCT = struct.Struct("<QQQIIHH")


@dataclass
class FileStatistics:
    """一份文件的摘要，``inspect_*`` 和测试都用它。"""

    path: str = ""
    num_rows: int = 0
    num_columns: int = 0
    num_pages: int = 0
    num_buffers: int = 0
    data_bytes: int = 0
    metadata_bytes: int = 0
    padding_bytes: int = 0
    total_bytes: int = 0
    encodings: Dict[str, int] = dc_field(default_factory=dict)
    #: 各列的 min/max/null_count —— 写进 dataset manifest，供查询剪枝使用
    zone_maps: Dict[str, Dict[str, Any]] = dc_field(default_factory=dict)

    @property
    def bytes_per_row(self) -> float:
        return self.total_bytes / self.num_rows if self.num_rows else 0.0

    def summary(self) -> Dict[str, Any]:
        return {
            "行数": self.num_rows,
            "列数": self.num_columns,
            "页数": self.num_pages,
            "缓冲区数": self.num_buffers,
            "数据字节": self.data_bytes,
            "元数据字节": self.metadata_bytes,
            "对齐填充": self.padding_bytes,
            "文件总字节": self.total_bytes,
            "每行字节": round(self.bytes_per_row, 2),
            "编码分布": dict(self.encodings),
        }

    def report(self) -> str:
        return "\n".join(f"{k}: {v}" for k, v in self.summary().items())


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


class LanceFileWriter:
    """把 ``RecordBatch`` 写成一个 Lance 容器。

    写入是**一趟顺序写**：数据页 → 页表 → 列元数据 → footer。
    这个顺序不是随便定的：元数据要记录每页的偏移和大小，只有写完数据
    才知道；而对象存储**不支持随机改写**，所以必须"边写边记、
    最后一次性收尾"，不能回头 seek 回填。
    """

    def __init__(
        self,
        *,
        target_values_per_page: int = 8192,
        target_page_bytes: Optional[int] = None,
        compression: str = COMPRESSION_NONE,
        alignment: int = ALIGNMENT,
    ) -> None:
        self.target_values_per_page = max(1, int(target_values_per_page))
        self.target_page_bytes = target_page_bytes
        self.compression = compression
        self.alignment = alignment

        self._body = bytearray()
        self._buffers: List[BufferLocation] = []
        self._columns: List[Dict[str, Any]] = []
        self._num_rows = 0
        self._wrote = False
        self._pending_schema: Optional[Schema] = None

    # -- 对外接口 ---------------------------------------------------------
    def write_batch(self, batch: RecordBatch) -> None:
        if self._wrote:
            raise SchemaError("finish() 之后不能再写")
        if batch.num_rows == 0:
            # 空批次不产生任何页，但 schema 仍要能被记录（见 finish）
            self._pending_schema = batch.schema
            return
        if self._num_rows and self._columns:
            if len(batch.schema) != len(self._columns_root()):
                raise SchemaError("同一个文件里的批次列数必须一致")
        for f in batch.schema.fields:
            self._write_column(f.field_id, f.name, f.type, batch.column(f.name))
        self._num_rows += batch.num_rows

    def _columns_root(self) -> List[Dict[str, Any]]:
        child_ids = set()
        for c in self._columns:
            child_ids.update(c.get("children") or [])
        return [c for i, c in enumerate(self._columns) if i not in child_ids]

    def finish(self) -> bytes:
        """产出文件字节。"""

        if self._wrote:
            raise SchemaError("finish() 只能调用一次")
        self._wrote = True

        # ---- 1) 列元数据（逐条 JSON，长度前缀） --------------------------
        col_meta_start = align_up(len(self._body), self.alignment)
        self._pad_to(col_meta_start)
        offsets: List[int] = []
        blob = bytearray()
        for col in self._columns:
            offsets.append(len(blob))
            payload = json.dumps(col, ensure_ascii=False, sort_keys=True).encode("utf-8")
            blob += struct.pack("<I", len(payload))
            blob += payload
        self._body += blob

        # ---- 2) 列元数据偏移表 -------------------------------------------
        col_meta_offsets_start = len(self._body)
        for o in offsets:
            self._body += struct.pack("<Q", o)

        # ---- 3) 全局缓冲区表（本实现恒为空：0 项 × 16 字节） --------------
        global_buffers_start = len(self._body)

        # ---- 4) Footer（40 字节，魔数结尾） -------------------------------
        self._body += _FOOTER_STRUCT.pack(
            col_meta_start,
            col_meta_offsets_start,
            global_buffers_start,
            0,  # num_global_buffers
            len(self._columns),
            2,  # major version：2 表示 v2 容器
            0,  # minor version
        )
        self._body += MAGIC
        return bytes(self._body)

    # -- 内部：列 ---------------------------------------------------------
    def _write_column(self, field_id: int, name: str, type: DataType, array: Array) -> int:
        """递归写一个逻辑列，返回它在 ``self._columns`` 里的下标。

        父列先入表、再递归写子列，保证"父的下标 < 子的下标" ——
        读取时按 ``children`` 跳转就是单向的，不会成环。
        """

        if isinstance(array, DictionaryArray):
            # 逻辑字典展开成值列（物理字典化由编码层负责）
            array = array.decode()
            type = array.type

        entry: Dict[str, Any] = {
            "field_id": field_id,
            "name": name,
            "type": type.to_json(),
            "num_rows": len(array),
            "null_count": array.num_nulls,
            "children": [],
            "pages": [],
            #: 列级有效性位图的绝对位置（-1 表示全列没有 null）
            "validity_offset": -1,
            "validity_size": 0,
            "zone_map": {},
            "layout": PageLayout.FULL_ZIP,
            "encoding": None,
        }
        index = len(self._columns)
        self._columns.append(entry)

        if array.nulls is not None and len(array) > 0:
            loc = self._append_buffer(np.packbits(array.null_mask()).tobytes())
            entry["validity_offset"] = loc.offset
            entry["validity_size"] = loc.size

        if isinstance(array, FixedSizeListArray):
            entry["layout"] = PageLayout.MINI_BLOCK
            entry["fixed_size"] = array.list_size
            entry["children"] = [
                self._write_column(field_id, f"{name}.item", array.type.child, array.values)
            ]
            return index

        if isinstance(array, ListArray):
            entry["layout"] = PageLayout.MINI_BLOCK
            off_entry = {
                "field_id": field_id,
                "name": f"{name}.offsets",
                "type": INT64.to_json(),
                "num_rows": len(array) + 1,
                "null_count": 0,
                "children": [],
                "pages": [],
                "validity_offset": -1,
                "validity_size": 0,
                "zone_map": {},
                "layout": PageLayout.FULL_ZIP,
                "role": "offsets",
                "encoding": None,
            }
            off_index = len(self._columns)
            self._columns.append(off_entry)
            self._append_pages(
                off_entry, PrimitiveArray(INT64, array.offsets.astype(np.int64)), f"{name}.offsets"
            )
            val_index = self._write_column(
                field_id, f"{name}.item", array.type.child, array.values
            )
            entry["children"] = [off_index, val_index]
            return index

        if isinstance(array, StructArray):
            for f in array.type.fields:
                entry["children"].append(
                    self._write_column(f.field_id, f"{name}.{f.name}", f.type, array.field(f.name))
                )
            return index

        if isinstance(array, MapArray):
            entry["layout"] = PageLayout.MINI_BLOCK
            entry["children"] = [
                self._write_column(field_id, f"{name}.keys", array.type.key_type, array.keys),
                self._write_column(field_id, f"{name}.values", array.type.item_type, array.items),
            ]
            return index

        if isinstance(array, NullArray):
            entry["encoding"] = "null"
            return index

        self._append_pages(entry, array, name)
        return index

    def _append_pages(self, entry: Dict[str, Any], array: Array, name: str) -> None:
        """把叶子列切成页并编码。

        分页阈值有两个：值个数与字节数。真实 Lance 主要按**字节**
        （默认 8 MiB）切，因为"一页 = 一次范围读"才是 I/O 的粒度；
        按值切在变长列上会让页大小差几十倍。
        """

        if isinstance(array, (StringArray, BinaryArray)):
            offsets, buf = array.offsets, array.buffer
            n = len(array)
            start = 0
            while start < n:
                stop = min(start + self.target_values_per_page, n)
                sub_off = offsets[start : stop + 1] - offsets[start]
                sub_buf = buf[int(offsets[start]) : int(offsets[stop])]
                self._emit_page(entry, encode_bytes(sub_off, sub_buf, stop - start))
                start = stop
            entry["encoding"] = entry["pages"][0]["encoding"] if entry["pages"] else "plain"
            entry["zone_map"] = _string_zone_map(array)
            return

        if isinstance(array, PrimitiveArray):
            data = array.data
            valid = None if array.nulls is None else ~array.nulls
            n = len(data)
            start = 0
            while start < n:
                stop = min(start + self.target_values_per_page, n)
                chunk = np.ascontiguousarray(data[start:stop])
                chunk_valid = None if valid is None else np.ascontiguousarray(valid[start:stop])
                self._emit_page(entry, encode_numeric(chunk, array.type, chunk_valid))
                start = stop
            entry["encoding"] = entry["pages"][0]["encoding"] if entry["pages"] else "plain"
            entry["zone_map"] = _numeric_zone_map(array)
            return

        raise NotSupportedError(f"LanceFileWriter 不支持 {type(array).__name__}")

    def _emit_page(self, entry: Dict[str, Any], page: EncodedPage) -> None:
        page = compress_page(page, self.compression)
        root_offset = entry.get("_next_row", 0)
        locs = [self._append_buffer(b) for b in page.buffers]
        entry["pages"].append(
            {
                "encoding": page.encoding,
                "layout": page.layout,
                "compression": page.compression,
                "num_values": page.num_values,
                # 与真实格式一致：每页直接记录各 buffer 的绝对偏移与长度
                "buffer_offsets": [l.offset for l in locs],
                "buffer_sizes": [l.size for l in locs],
                "roles": list(page.roles),
                "metadata": page.metadata,
                "num_bytes": page.payload_bytes,
                # 页内首行的顶层行号 —— 真实 Lance 用它做"按行二分找页"
                "priority": root_offset,
            }
        )
        fixed = int(entry.get("fixed_size") or 0)
        entry["_next_row"] = root_offset + (page.num_values // fixed if fixed else page.num_values)

    def _append_buffer(self, data: bytes) -> BufferLocation:
        """写一个缓冲区，返回它的绝对位置。

        对齐是**必须**的：64 字节对 SIMD 解码器友好，4096 字节对
        直通 I/O（O_DIRECT / io_uring）是硬要求 —— 不对齐的内核
        会直接拒绝请求或退化成缓冲 I/O。
        """

        start = align_up(len(self._body), self.alignment)
        self._pad_to(start)
        self._body += data
        loc = BufferLocation(start, len(data))
        self._buffers.append(loc)
        return loc

    def _pad_to(self, offset: int) -> None:
        if len(self._body) < offset:
            self._body += b"\x00" * (offset - len(self._body))

    # -- 列统计 -----------------------------------------------------------
    def column_zone_maps(self) -> Dict[str, Dict[str, Any]]:
        """每个顶层列的 min/max/null_count。

        这些统计会被**抄进 dataset 的 manifest**（见 ``DataFile.column_stats``），
        于是查询规划时做剪枝**一次 I/O 都不用发** ——
        这是 Lance 和"把统计放在数据文件里"的格式之间最重要的差别。
        """

        return {
            c["name"]: dict(c.get("zone_map") or {})
            for c in self._columns_root()
        }

    # -- 统计 -------------------------------------------------------------
    def statistics(self, path: str = "") -> FileStatistics:
        data = bytes(self._body)
        pages = 0
        encodings: Dict[str, int] = {}
        for c in self._columns:
            for p in c.get("pages", []):
                pages += 1
                encodings[p["encoding"]] = encodings.get(p["encoding"], 0) + 1
        payload = sum(l.size for l in self._buffers)
        return FileStatistics(
            path=path,
            num_rows=self._num_rows,
            num_columns=len(self._columns_root()),
            num_pages=pages,
            num_buffers=len(self._buffers),
            data_bytes=payload,
            metadata_bytes=max(0, len(data) - payload - FOOTER_SIZE),
            padding_bytes=sum(
                align_up(l.offset, self.alignment) - l.offset for l in self._buffers
            ),
            total_bytes=len(data) + FOOTER_SIZE,
            encodings=encodings,
        )


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


@dataclass
class ReadStats:
    """一个 reader 实例的 I/O 记账 —— 所有"下推到底有没有生效"的断言都靠它。"""

    bytes_read: int = 0
    range_reads: int = 0
    pages_read: int = 0
    pages_skipped: int = 0
    rows_read: int = 0

    def summary(self) -> Dict[str, int]:
        return {
            "bytes_read": self.bytes_read,
            "range_reads": self.range_reads,
            "pages_read": self.pages_read,
            "pages_skipped": self.pages_skipped,
            "rows_read": self.rows_read,
        }

    def __str__(self) -> str:  # pragma: no cover
        s = self.summary()
        return (
            f"读 {s['bytes_read']} 字节 / {s['range_reads']} 次范围读；"
            f"读页 {s['pages_read']}，跳过页 {s['pages_skipped']}"
        )


class LanceFileReader:
    """从对象存储上按需读一个 Lance 文件。

    刻意设计成"先廉价地打开，再按需读列"：

    >>> reader = LanceFileReader(store, "data/abc.lance")
    >>> reader.num_rows                       # 只读了 footer + 列元数据
    1000000
    >>> batch = reader.read(columns=["vector"], row_range=(0, 1000))

    这样"投影下推"和"行范围下推"是真的少读字节，而不是读完再丢。
    """

    def __init__(
        self, store: ObjectStore, path: str, *, coalesce_gap: int = 4096
    ) -> None:
        self.store = store
        self.path = path
        #: 相邻缓冲区的间隔小于这个字节数时合并成一次范围读。
        #: 对象存储上"一次请求"的固定开销（RTT + 鉴权 + HTTP 头）
        #: 远大于多读几 KB 的代价，所以**宁可多读字节，也要少发请求**。
        #: 设为 0 可以关掉合并，用来量化这个优化值多少。
        self.coalesce_gap = int(coalesce_gap)
        self._footer: Optional[Tuple[int, int, int, int, int, int, int]] = None
        self._columns: Optional[List[Dict[str, Any]]] = None
        self._child_ids: Optional[set] = None
        self._schema_cache: Optional[Schema] = None
        self.stats = ReadStats()

    # -- 打开（廉价） -----------------------------------------------------
    def _read_footer(self) -> Tuple[int, int, int, int, int, int, int]:
        if self._footer is None:
            size = self.store.size(self.path)
            if size < FOOTER_SIZE:
                raise CorruptedFileError("文件太短，读不到 footer", path=self.path, size=size)
            raw = self._range(size - FOOTER_SIZE, FOOTER_SIZE)
            if raw[-4:] != MAGIC:
                raise CorruptedFileError(
                    "文件尾部魔数不是 LANC", path=self.path, found=raw[-4:].hex()
                )
            footer = _FOOTER_STRUCT.unpack(raw[: _FOOTER_STRUCT.size])
            if footer[5] != 2:
                raise CorruptedFileError(
                    "只支持 major version 2 的容器", path=self.path, major=footer[5]
                )
            self._footer = footer
        return self._footer

    def _range(self, offset: int, length: int) -> bytes:
        data = self.store.get_range(self.path, offset, length)
        self.stats.bytes_read += len(data)
        self.stats.range_reads += 1
        return data

    # -- 元信息（只读 footer + 元数据区，不碰数据页） ----------------------
    def _load_footer_and_meta(self) -> None:
        if self._columns is not None:
            return
        col_meta_start, offsets_start, _global_start, _ng, num_cols, _maj, _min = (
            self._read_footer()
        )
        if num_cols == 0:
            self._columns = []
            self._child_ids = set()
            return
        off_table = self._range(offsets_start, num_cols * 8)
        offsets = [struct.unpack_from("<Q", off_table, i * 8)[0] for i in range(num_cols)]
        blob = self._range(col_meta_start, offsets_start - col_meta_start)
        cols: List[Dict[str, Any]] = []
        for o in offsets:
            (length,) = struct.unpack_from("<I", blob, o)
            cols.append(json.loads(blob[o + 4 : o + 4 + length].decode("utf-8")))
        self._columns = cols
        child_ids: set = set()
        for c in cols:
            child_ids.update(c.get("children") or [])
        self._child_ids = child_ids

    @property
    def columns(self) -> List[Dict[str, Any]]:
        self._load_footer_and_meta()
        assert self._columns is not None
        return self._columns

    @property
    def num_rows(self) -> int:
        self._load_footer_and_meta()
        roots = self.root_columns()
        return max((int(c.get("num_rows", 0)) for c in roots), default=0)

    def root_columns(self) -> List[Dict[str, Any]]:
        self._load_footer_and_meta()
        assert self._child_ids is not None
        return [c for i, c in enumerate(self.columns) if i not in self._child_ids]

    def schema(self) -> Schema:
        if self._schema_cache is None:
            from ..schema import Field

            fields = []
            for c in self.root_columns():
                fields.append(
                    Field(
                        c["name"],
                        DataType.from_json(c["type"]),
                        nullable=int(c.get("null_count") or 0) > 0,
                        field_id=c["field_id"],
                    )
                )
            self._schema_cache = Schema(fields)
        return self._schema_cache

    def column_metadata(self, name: str) -> Dict[str, Any]:
        for c in self.columns:
            if c["name"] == name:
                return c
        raise CorruptedFileError(
            "文件里没有列", column=name, available=[c["name"] for c in self.columns]
        )

    def _read_buffer_at(self, offset: int, size: int) -> bytes:
        """读一个缓冲区。位置来自已加载的列元数据，**零额外往返**。"""

        return self._range(int(offset), int(size))

    # -- 读数据 -----------------------------------------------------------
    def read(
        self,
        columns: Optional[Sequence[str]] = None,
        row_range: Optional[Tuple[int, int]] = None,
    ) -> RecordBatch:
        """读一个 batch。

        * ``columns`` 为 ``None`` 表示全读；否则**只读这些列（含其子列）的页**；
        * ``row_range`` 会在页粒度上跳过不相交的页。
        """

        self._load_footer_and_meta()
        assert self._child_ids is not None
        wanted = set(columns) if columns else None

        out: Dict[str, Array] = {}
        for i, c in enumerate(self.columns):
            if i in self._child_ids:
                continue
            if wanted is not None and not _matches(c["name"], wanted):
                continue
            out[c["name"]] = self._read_column(i, row_range)

        if not out:
            return RecordBatch.empty(Schema([]))
        schema = Schema([f for f in self.schema().fields if f.name in out])
        return RecordBatch(schema, out)

    def _read_column(self, index: int, row_range: Optional[Tuple[int, int]]) -> Array:
        """读一列并**已按 row_range 裁剪**。返回值长度 = 请求的行数。"""

        self._load_footer_and_meta()
        assert self._columns is not None
        c = self._columns[index]
        children = list(c.get("children") or [])
        t = DataType.from_json(c["type"])
        n_rows = int(c.get("num_rows") or 0)

        nulls = self._read_validity(c, row_range, n_rows, is_offsets=c.get("role") == "offsets")

        if isinstance(t, FixedSizeList):
            d = t.list_size
            child = self._read_column(children[0], _scale_range(row_range, d))
            return FixedSizeListArray(t, child, nulls)

        if isinstance(t, ListType):
            # offsets 列本身不大，整列读回来；有了它才能算出子列该读哪一段
            off_arr = self._read_column(children[0], None)
            offsets = np.asarray(off_arr.to_pylist(), dtype=np.int64)
            if row_range is not None:
                last = max(0, len(offsets) - 1)
                lo = min(row_range[0], last)
                hi = min(row_range[1], last)
                offsets = offsets[lo : hi + 1] - offsets[lo]
            if len(offsets) == 0 or int(offsets[-1]) == 0:
                child = empty_array(t.child)
                offsets = offsets if len(offsets) else np.zeros(1, dtype=np.int64)
            else:
                child = self._read_column(children[1], (0, int(offsets[-1])))
            return ListArray(t, offsets, child, nulls)

        if isinstance(t, StructType):
            arrs = {
                f.name: self._read_column(ci, row_range)
                for f, ci in zip(t.fields, children)
            }
            return StructArray(t, arrs, nulls)

        if isinstance(t, MapType):
            raise NotSupportedError("Map 列的读取路径尚未实现（写入已支持）")

        if isinstance(t, NullType):
            n = n_rows if row_range is None else max(0, row_range[1] - row_range[0])
            return NullArray(n)

        if t.name in ("utf8", "large_utf8", "binary", "large_binary"):
            offsets, buf = self._read_pages_as_bytes(c, row_range)
            cls = StringArray if t.name.endswith("utf8") else BinaryArray
            return cls(t, offsets, buf, nulls)

        data = self._read_pages_as_numeric(c, t, row_range)
        return PrimitiveArray(t, data, nulls)

    def _read_validity(
        self,
        c: Dict[str, Any],
        row_range: Optional[Tuple[int, int]],
        n_rows: int,
        *,
        is_offsets: bool,
    ) -> Optional[np.ndarray]:
        voff = int(c.get("validity_offset", -1))
        if voff < 0:
            return None
        raw = self._read_buffer_at(voff, int(c.get("validity_size", 0)))
        full = np.unpackbits(np.frombuffer(raw, dtype=np.uint8))[:n_rows]
        if is_offsets and row_range is not None:
            lo = min(row_range[0], len(full))
            hi = min(row_range[1] + 1, len(full))
            full = full[lo:hi]
        elif row_range is not None:
            full = full[row_range[0] : row_range[1]]
        return full.astype(bool) if len(full) else None

    def _read_buffers_coalesced(
        self, locs: Sequence[Tuple[int, int]]
    ) -> List[bytes]:
        """把一页的多个缓冲区合并成尽量少的范围读。

        一页通常有 1~4 个缓冲区（值、字典、下标、有效性）。它们由写入端
        顺序摆放，因此彼此邻近；不合并的话，读一页就是 4 次往返。
        合并策略是"间隔小于 ``coalesce_gap`` 就一起读"——
        对象存储上"一次请求"的固定开销（RTT + 鉴权 + HTTP 头）
        远大于多读几 KB 的代价。
        """

        if not locs:
            return []
        indexed = sorted((int(o), int(s), i) for i, (o, s) in enumerate(locs))
        groups: List[List[Tuple[int, int, int]]] = [[indexed[0]]]
        for loc in indexed[1:]:
            prev = groups[-1][-1]
            if loc[0] - (prev[0] + prev[1]) <= self.coalesce_gap:
                groups[-1].append(loc)
            else:
                groups.append([loc])

        out: Dict[int, bytes] = {}
        for group in groups:
            start = group[0][0]
            end = max(off + size for off, size, _ in group)
            chunk = self._range(start, end - start)
            for off, size, idx in group:
                out[idx] = chunk[off - start : off - start + size]
        return [out[i] for i in range(len(locs))]

    def _decode_page(self, c: Dict[str, Any], page: Dict[str, Any]) -> EncodedPage:
        offsets = page.get("buffer_offsets") or []
        sizes = page.get("buffer_sizes") or []
        roles = page.get("roles") or [BufferRole.VALUES] * len(offsets)
        buffers = self._read_buffers_coalesced(list(zip(offsets, sizes)))
        self.stats.pages_read += 1
        ep = EncodedPage(
            page["encoding"],
            buffers,
            list(roles),
            int(page["num_values"]),
            dict(page.get("metadata") or {}),
            page.get("layout", PageLayout.FULL_ZIP),
            page.get("compression", COMPRESSION_NONE),
        )
        return decompress_page(ep)

    def _read_pages_as_numeric(
        self, c: Dict[str, Any], t: DataType, row_range: Optional[Tuple[int, int]]
    ) -> np.ndarray:
        pages = c.get("pages") or []
        if not pages:
            return np.zeros(0, dtype=t.numpy_dtype)
        bounds = _page_row_bounds(c)
        chunks: List[np.ndarray] = []
        for i, p in enumerate(pages):
            lo, hi = bounds[i]
            inter = _intersect((lo, hi), row_range)
            if inter is None:
                self.stats.pages_skipped += 1
                continue
            arr = decode_numeric(self._decode_page(c, p), t)
            if inter != (lo, hi):
                arr = arr[inter[0] - lo : inter[1] - lo]
            chunks.append(arr)
        self.stats.rows_read += sum(len(x) for x in chunks)
        if not chunks:
            return np.zeros(0, dtype=t.numpy_dtype)
        return np.concatenate(chunks) if len(chunks) > 1 else chunks[0]

    def _read_pages_as_bytes(
        self, c: Dict[str, Any], row_range: Optional[Tuple[int, int]]
    ) -> Tuple[np.ndarray, bytes]:
        pages = c.get("pages") or []
        if not pages:
            return np.zeros(1, dtype=np.int64), b""
        bounds = _page_row_bounds(c)
        out_offsets: List[int] = [0]
        parts: List[bytes] = []
        acc = 0
        for i, p in enumerate(pages):
            lo, hi = bounds[i]
            inter = _intersect((lo, hi), row_range)
            if inter is None:
                self.stats.pages_skipped += 1
                continue
            o, buf = decode_bytes(self._decode_page(c, p))
            s = inter[0] - lo
            e = inter[1] - lo
            sub = o[s : e + 1]
            if len(sub) == 0:
                continue
            base = int(sub[0])
            out_offsets.extend((sub[1:] - base + acc).tolist())
            acc += int(sub[-1] - base)
            parts.append(buf[base : int(sub[-1])])
        self.stats.rows_read += len(out_offsets) - 1
        return np.asarray(out_offsets, dtype=np.int64), b"".join(parts)

    # -- 调试 -------------------------------------------------------------
    def explain(self) -> Dict[str, Any]:
        """把文件的物理结构摊开，不读任何数据页。"""

        self._load_footer_and_meta()
        col_meta_start, offsets_start, global_start, _ng, num_cols, maj, minor = (
            self._read_footer()
        )
        cols = []
        for i, c in enumerate(self.columns):
            cols.append(
                {
                    "index": i,
                    "name": c["name"],
                    "type": DataType.from_json(c["type"]).name,
                    "rows": c.get("num_rows"),
                    "nulls": c.get("null_count"),
                    "children": c.get("children") or [],
                    "null_count": c.get("null_count"),
                    "validity": (
                        None
                        if int(c.get("validity_offset", -1)) < 0
                        else {
                            "offset": c["validity_offset"],
                            "size": c.get("validity_size", 0),
                        }
                    ),
                    "pages": [
                        {
                            "encoding": p["encoding"],
                            "layout": p.get("layout"),
                            "compression": p.get("compression"),
                            "num_values": p["num_values"],
                            "bytes": p.get("num_bytes"),
                            "priority": p.get("priority"),
                            "buffer_offsets": p.get("buffer_offsets"),
                            "buffer_sizes": p.get("buffer_sizes"),
                            "metadata": p.get("metadata") or {},
                        }
                        for p in (c.get("pages") or [])
                    ],
                    "zone_map": c.get("zone_map") or {},
                }
            )
        return {
            "path": self.path,
            "size_bytes": self.store.size(self.path),
            "num_rows": self.num_rows,
            "version": {"major": maj, "minor": minor},
            "footer_size": FOOTER_SIZE,
            "column_metadata_start": col_meta_start,
            "column_metadata_offsets_start": offsets_start,
            "global_buffer_offsets_start": global_start,
            "bytes_read_to_open": self.stats.bytes_read,
            "range_reads_to_open": self.stats.range_reads,
            "columns": cols,
        }


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _matches(name: str, wanted: set) -> bool:
    if name in wanted:
        return True
    return any(name.startswith(w + ".") for w in wanted)


def _scale_range(
    row_range: Optional[Tuple[int, int]], factor: int
) -> Optional[Tuple[int, int]]:
    if row_range is None:
        return None
    return (row_range[0] * factor, row_range[1] * factor)


def _intersect(a: Tuple[int, int], b: Optional[Tuple[int, int]]) -> Optional[Tuple[int, int]]:
    if b is None:
        return a
    lo = max(a[0], b[0])
    hi = min(a[1], b[1])
    return (lo, hi) if lo < hi else None


def _page_row_bounds(c: Dict[str, Any]) -> List[Tuple[int, int]]:
    """每页覆盖的**行**区间。

    对定长列表列，一页的值数是行数 × list_size，必须除回去，
    否则行范围下推会算错 —— 这是一个很容易踩的边界。
    """

    fixed = int(c.get("fixed_size") or 0)
    out: List[Tuple[int, int]] = []
    start = 0
    for p in c.get("pages") or []:
        n = int(p["num_values"])
        rows = n // fixed if fixed else n
        out.append((start, start + rows))
        start += rows
    return out


def _numeric_zone_map(array: PrimitiveArray) -> Dict[str, Any]:
    if len(array) == 0:
        return {}
    mask = array.null_mask()
    data = array.data[~mask] if mask is not None else array.data
    nulls = int(mask.sum()) if mask is not None else 0
    if len(data) == 0:
        return {"null_count": nulls}
    if data.dtype.kind in "iuf":
        return {"min": float(data.min()), "max": float(data.max()), "null_count": nulls}
    return {"null_count": nulls}


def _string_zone_map(array: Any) -> Dict[str, Any]:
    vals = [v for v in array.to_pylist() if v is not None]
    if not vals:
        return {}
    lo, hi = min(vals), max(vals)
    return {
        "min": lo[:64] if isinstance(lo, str) else None,
        "max": hi[:64] if isinstance(hi, str) else None,
        "null_count": len(array) - len(vals),
    }


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------


def write_lance_file(
    store: ObjectStore,
    path: str,
    batches: Sequence[RecordBatch],
    *,
    target_values_per_page: int = 8192,
    compression: str = COMPRESSION_NONE,
) -> FileStatistics:
    """把一个或多个 batch 写成一个 ``.lance`` 文件。"""

    w = LanceFileWriter(
        target_values_per_page=target_values_per_page, compression=compression
    )
    for b in batches:
        w.write_batch(b)
    data = w.finish()
    store.put(path, data)
    stats = w.statistics(path)
    stats.total_bytes = len(data)
    stats.zone_maps = w.column_zone_maps()
    return stats


def read_lance_file(
    store: ObjectStore,
    path: str,
    *,
    columns: Optional[Sequence[str]] = None,
    row_range: Optional[Tuple[int, int]] = None,
) -> RecordBatch:
    return LanceFileReader(store, path).read(columns=columns, row_range=row_range)


def inspect_lance_file(store: ObjectStore, path: str) -> Dict[str, Any]:
    """不读数据、只读元数据地把文件结构摊开。"""

    return LanceFileReader(store, path).explain()

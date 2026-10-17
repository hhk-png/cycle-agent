"""Schema —— 字段、字段 id 与 schema 演进。

为什么 Lance 的字段要有 **稳定 id**？

假设表里是 ``[a, b, c]``，某一列的数据文件里列的顺序就是字段的顺序。
现在执行 ``drop_columns(["b"])``：如果新 schema 变成 ``[a, c]``，
那么旧文件里的第 3 列在新 schema 里会被读成第 2 列 —— 不重写旧数据就会串列。
Lance 的解法是：每个字段在**创建时**分配一个永不复用的整数 id，
数据文件按 id 而不是按位置来定位列，于是删列只是把 id 从 schema 里摘掉，
旧文件原样保留，读取时按 id 对齐即可。这也让"加列"变成纯元数据操作。

本模块实现了同一套机制：``Schema`` 维护 ``name -> field_id`` 映射，
``Field`` 自带 ``field_id``，任何结构性变更都会保持已有字段的 id 不变。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

from .errors import SchemaError, TypeMismatchError
from .types import DataType, ROW_ID_TYPE, parse_type

__all__ = [
    "Field",
    "Schema",
    "META_FIELD_ID",
    "META_LANCE_ENCODING",
    "ROW_ID_FIELD",
    "schema_to_json",
    "schema_from_json",
]

#: 字段元数据里存 field id 的键名（真实 Lance 用 ``"field_id"``）
META_FIELD_ID = "field_id"
#: 字段元数据里记录 Lance 物理编码的键名（真实 Lance 用 ``"lance-encoding:..."`` 前缀）
META_LANCE_ENCODING = "lance-encoding:structural"
#: 字段元数据里标记向量列的键名（真实 LanceDB 用 ``"vector_dim"`` 之外的方式，
#: 但会写 ``"lance-schema:vector_column"`` 之类的标记）
META_VECTOR_COLUMN = "lance-schema:vector-column"


class Field:
    """Schema 里的一列：名字 + 类型 + 可空性 + 元数据。

    ``field_id`` 是 Lance 风格的稳定列 id。手工构造时可以不传，
    ``Schema`` 会自动分配；从磁盘读回来时必须带上，否则演进会错位。
    """

    __slots__ = ("name", "type", "nullable", "metadata", "field_id")

    def __init__(
        self,
        name: str,
        type: Any,
        nullable: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
        field_id: Optional[int] = None,
    ) -> None:
        if not name:
            raise SchemaError("字段名不能为空")
        if not isinstance(name, str):
            raise SchemaError("字段名必须是字符串", got=type(name).__name__)
        self.name = name
        self.type = type if isinstance(type, DataType) else parse_type(str(type))
        self.nullable = bool(nullable)
        self.metadata: Dict[str, Any] = dict(metadata or {})
        if field_id is not None:
            self.metadata[META_FIELD_ID] = int(field_id)
        self.field_id = field_id

    # -- 复制 -------------------------------------------------------------
    def with_name(self, name: str) -> "Field":
        return Field(name, self.type, self.nullable, dict(self.metadata), self.field_id)

    def with_type(self, t: DataType, nullable: Optional[bool] = None) -> "Field":
        return Field(
            self.name,
            t,
            self.nullable if nullable is None else nullable,
            dict(self.metadata),
            self.field_id,
        )

    # -- 序列化 -----------------------------------------------------------
    def to_json(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type.to_json(),
            "nullable": self.nullable,
            "metadata": dict(self.metadata),
            "field_id": self.field_id,
        }

    @staticmethod
    def from_json(obj: Mapping[str, Any]) -> "Field":
        return Field(
            obj["name"],
            DataType.from_json(obj["type"]),
            obj.get("nullable", True),
            obj.get("metadata") or {},
            obj.get("field_id"),
        )

    # -- Arrow ------------------------------------------------------------
    def _to_arrow_field(self, pa: Any) -> Any:
        meta = {str(k): str(v) for k, v in self.metadata.items()}
        if self.field_id is not None:
            meta.setdefault(META_FIELD_ID, str(self.field_id))
        return pa.field(
            self.name, self.type._to_arrow(pa), nullable=self.nullable, metadata=meta or None
        )

    def to_arrow(self) -> Any:
        import pyarrow as pa

        return self._to_arrow_field(pa)

    @staticmethod
    def from_arrow(arrow_field: Any) -> "Field":
        meta = {str(k): v for k, v in (arrow_field.metadata or {}).items()}
        fid = meta.get(META_FIELD_ID)
        if fid is not None:
            try:
                fid = int(fid)
            except (TypeError, ValueError):
                fid = None
        return Field(
            arrow_field.name,
            DataType.from_arrow(arrow_field.type),
            arrow_field.nullable,
            meta,
            fid,
        )

    # -- 杂项 -------------------------------------------------------------
    def __repr__(self) -> str:
        null = "" if self.nullable else " not null"
        return f"Field({self.name!r}: {self.type.name}{null})"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Field)
            and self.name == other.name
            and self.type == other.type
            and self.nullable == other.nullable
            and self.field_id == other.field_id
        )

    def __hash__(self) -> int:
        return hash((self.name, self.type, self.nullable, self.field_id))

    @property
    def is_vector(self) -> bool:
        from .types import is_vector_type

        return is_vector_type(self.type)

    @property
    def vector_dim(self) -> Optional[int]:
        from .types import vector_dim

        return vector_dim(self.type)


class Schema:
    """表结构。

    内部保持两个不变式：

    1. **每个字段都有唯一的 field_id**（非负整数，创建后永不变）；
    2. **字段顺序 = 数据文件里的物理列顺序**，但读取一律按 id 对齐。
    """

    __slots__ = ("fields", "_by_name", "_by_id", "metadata")

    def __init__(
        self,
        fields: Sequence[Any],
        metadata: Optional[Dict[str, str]] = None,
    ) -> None:
        parsed: List[Field] = []
        for f in fields:
            if isinstance(f, Field):
                parsed.append(f)
            elif isinstance(f, (tuple, list)) and len(f) >= 2:
                parsed.append(Field(f[0], f[1], *(list(f[2:]) + [True])[:2]))
            elif isinstance(f, Mapping):
                parsed.append(Field.from_json(f))
            else:
                raise SchemaError("无法识别的字段定义", got=repr(f))
        names = [f.name for f in parsed]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise SchemaError("Schema 里出现重复列名", duplicates=sorted(dupes))
        self.fields: Tuple[Field, ...] = tuple(parsed)
        self.metadata: Dict[str, str] = dict(metadata or {})
        self._assign_field_ids()
        self._by_name = {f.name: f for f in self.fields}
        self._by_id = {f.field_id: f for f in self.fields if f.field_id is not None}

    # -- 构造 -------------------------------------------------------------
    def _assign_field_ids(self) -> None:
        used = {f.field_id for f in self.fields if f.field_id is not None}
        next_id = 0 if not used else max(used) + 1
        for f in self.fields:
            if f.field_id is None:
                while next_id in used:
                    next_id += 1
                f.field_id = next_id
                f.metadata[META_FIELD_ID] = next_id
                used.add(next_id)
                next_id += 1
        if len({f.field_id for f in self.fields}) != len(self.fields):
            raise SchemaError("field_id 冲突")

    @staticmethod
    def from_pylist(rows: Sequence[Mapping[str, Any]]) -> "Schema":
        from .types import infer_schema

        return infer_schema(rows)

    @staticmethod
    def from_dicts(rows: Sequence[Mapping[str, Any]]) -> "Schema":
        return Schema.from_pylist(rows)

    # -- 访问 -------------------------------------------------------------
    def __len__(self) -> int:
        return len(self.fields)

    def __iter__(self) -> Iterator[Field]:
        return iter(self.fields)

    def __getitem__(self, key: Any) -> Field:
        if isinstance(key, int):
            return self.fields[key]
        try:
            return self._by_name[key]
        except KeyError:
            raise SchemaError(
                f"Schema 里没有列 {key!r}", available=list(self._by_name)
            ) from None

    def __contains__(self, name: object) -> bool:
        return name in self._by_name

    @property
    def names(self) -> List[str]:
        return [f.name for f in self.fields]

    def field(self, name: str) -> Field:
        return self[name]

    def field_by_id(self, field_id: int) -> Optional[Field]:
        return self._by_id.get(field_id)

    def index_of(self, name: str) -> int:
        for i, f in enumerate(self.fields):
            if f.name == name:
                return i
        raise SchemaError(f"Schema 里没有列 {name!r}", available=self.names)

    def get(self, name: str, default: Any = None) -> Any:
        return self._by_name.get(name, default)

    # -- 向量列辅助 -------------------------------------------------------
    @property
    def vector_columns(self) -> List[str]:
        return [f.name for f in self.fields if f.is_vector]

    def default_vector_column(self) -> Optional[str]:
        """未显式指定向量列时用哪个？

        规则：只有一个向量列就用它；有多个且存在名为 ``vector`` 的用它；
        否则返回 ``None``，让上层报"必须显式指定 vector_column_name"。
        """

        vecs = self.vector_columns
        if len(vecs) == 1:
            return vecs[0]
        if "vector" in vecs:
            return "vector"
        return None

    def with_vector(self, name: str, dim: int, *, dtype: DataType | None = None) -> "Schema":
        from .types import FLOAT32, FixedSizeList

        t = FixedSizeList(dtype or FLOAT32, dim)
        if name in self._by_name:
            return self.set_column(name, self[name].with_type(t))
        return self.append_column(Field(name, t, nullable=True))

    # -- schema 演进 ------------------------------------------------------
    def append_column(self, field: Field) -> "Schema":
        if field.name in self._by_name:
            raise SchemaError(f"列 {field.name!r} 已存在", column=field.name)
        return Schema(list(self.fields) + [field], dict(self.metadata))

    def set_column(self, name: str, field: Field) -> "Schema":
        if name not in self._by_name:
            raise SchemaError(f"列 {name!r} 不存在", column=name)
        out = []
        for f in self.fields:
            if f.name == name:
                # 保持 field_id 不变 —— 这是"改类型不搬数据"的前提
                nf = field.with_name(name)
                nf.field_id = f.field_id
                nf.metadata[META_FIELD_ID] = f.field_id
                out.append(nf)
            else:
                out.append(f)
        return Schema(out, dict(self.metadata))

    def drop_column(self, name: str) -> "Schema":
        if name not in self._by_name:
            raise SchemaError(f"列 {name!r} 不存在", column=name)
        return Schema([f for f in self.fields if f.name != name], dict(self.metadata))

    def rename_column(self, old: str, new: str) -> "Schema":
        if old not in self._by_name:
            raise SchemaError(f"列 {old!r} 不存在", column=old)
        if new in self._by_name and new != old:
            raise SchemaError(f"列 {new!r} 已存在", column=new)
        f = self._by_name[old]
        nf = f.with_name(new)
        nf.field_id = f.field_id
        nf.metadata[META_FIELD_ID] = f.field_id
        return Schema([nf if x.name == old else x for x in self.fields], dict(self.metadata))

    def select(self, names: Sequence[str]) -> "Schema":
        missing = [n for n in names if n not in self._by_name]
        if missing:
            raise SchemaError("select 引用了不存在的列", missing=missing)
        return Schema([self._by_name[n] for n in names], dict(self.metadata))

    # -- 兼容性检查 -------------------------------------------------------
    def compatible_with(self, other: "Schema", *, check_order: bool = True) -> bool:
        """本 schema 能否接受 ``other`` 写来的数据（用于 ``append`` 的校验）。"""

        if check_order and self.names != other.names:
            return False
        for f in other.fields:
            mine = self._by_name.get(f.name)
            if mine is None:
                return False
            if mine.type != f.type:
                from .types import promote_types

                if promote_types(mine.type, f.type) != mine.type:
                    return False
            if f.nullable is False and mine.nullable is True:
                # 目标允许 null，源不允许 —— 安全
                continue
        return True

    def diff(self, other: "Schema") -> Dict[str, List[str]]:
        """列出两个 schema 的差异，``analyze`` 类输出会用到。"""

        mine = set(self.names)
        theirs = set(other.names)
        changed = []
        for n in mine & theirs:
            if self[n].type != other[n].type:
                changed.append(n)
        return {
            "added": sorted(theirs - mine),
            "removed": sorted(mine - theirs),
            "changed_type": sorted(changed),
        }

    # -- 序列化 -----------------------------------------------------------
    def to_json(self) -> Dict[str, Any]:
        return {
            "fields": [f.to_json() for f in self.fields],
            "metadata": dict(self.metadata),
        }

    @staticmethod
    def from_json(obj: Mapping[str, Any]) -> "Schema":
        return Schema(
            [Field.from_json(f) for f in obj["fields"]], obj.get("metadata") or {}
        )

    # -- Arrow ------------------------------------------------------------
    def to_arrow(self) -> Any:
        import pyarrow as pa

        return pa.schema([f._to_arrow_field(pa) for f in self.fields])

    @staticmethod
    def from_arrow(arrow_schema: Any) -> "Schema":
        return Schema(
            [Field.from_arrow(f) for f in arrow_schema],
            {str(k): v for k, v in (arrow_schema.metadata or {}).items()},
        )

    @staticmethod
    def from_pandas(df: Any) -> "Schema":
        import pyarrow as pa

        return Schema.from_arrow(pa.Schema.from_pandas(df))

    # -- 展示 -------------------------------------------------------------
    def __repr__(self) -> str:
        inner = ", ".join(f"{f.name}: {f.type.name}" for f in self.fields)
        return f"Schema({inner})"

    def pretty(self) -> str:
        """多行对齐的 schema 概览，notebook 里直接 print。"""

        if not self.fields:
            return "(空 schema)"
        width = max(len(f.name) for f in self.fields)
        twidth = max(len(f.type.name) for f in self.fields)
        lines = []
        for f in self.fields:
            flags = []
            if not f.nullable:
                flags.append("not null")
            if f.is_vector:
                flags.append(f"vector[{f.vector_dim}]")
            if f.metadata.get(META_VECTOR_COLUMN):
                flags.append("向量列")
            lines.append(
                f"  {f.name:<{width}}  {f.type.name:<{twidth}}  "
                f"id={f.field_id:<3} {' '.join(flags)}".rstrip()
            )
        return "\n".join(lines)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Schema):
            return NotImplemented
        return self.fields == other.fields and self.metadata == other.metadata

    def __hash__(self) -> int:
        return hash((self.fields, tuple(sorted(self.metadata.items()))))


# ---------------------------------------------------------------------------
# 内置的 "_rowid" 概念列
# ---------------------------------------------------------------------------

#: Lance 的稳定行 id。它不是表里真实存在的一列，而是"片段 id + 片段内偏移"
#: 的逻辑组合，查询时通过 ``_rowid`` 暴露出来。
ROW_ID_FIELD = Field("_rowid", ROW_ID_TYPE, nullable=False, metadata={"virtual": "true"})


def schema_to_json(schema: Schema) -> str:
    return json.dumps(schema.to_json(), ensure_ascii=False, sort_keys=True)


def schema_from_json(text: str) -> Schema:
    return Schema.from_json(json.loads(text))


def merge_schemas(a: Schema, b: Schema, *, prefer: str = "a") -> Schema:
    """把两个 schema 归并成一个（``append`` 时按列名对齐、缺列补 null）。

    这实现的是 Lance 的 "schema evolution on append"：
    新数据多了列 → 自动加列（旧行该列为 null）；
    新数据少了列 → 该列填 null；
    类型不同但可提升 → 用提升后的类型（需要重写旧数据，属于 expensive 操作）。
    """

    from .types import promote_types

    names: List[str] = list(a.names)
    for n in b.names:
        if n not in names:
            names.append(n)
    fields: List[Field] = []
    for n in names:
        fa, fb = a.get(n), b.get(n)
        if fa is not None and fb is not None:
            if fa.type == fb.type:
                fields.append(fa.with_type(fa.type, fa.nullable or fb.nullable))
            else:
                p = promote_types(fa.type, fb.type)
                if p is None:
                    raise TypeMismatchError(
                        f"列 {n!r} 类型无法归并", left=fa.type.name, right=fb.type.name
                    )
                base = fa if prefer == "a" else fb
                nf = base.with_type(p, fa.nullable or fb.nullable)
                nf.field_id = fa.field_id
                nf.metadata[META_FIELD_ID] = fa.field_id
                fields.append(nf)
        else:
            f = fa if fa is not None else fb
            assert f is not None
            fields.append(f.with_type(f.type, True))
    return Schema(fields, dict(a.metadata))

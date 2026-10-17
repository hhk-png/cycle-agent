"""mini-LanceDB 的异常体系。

设计原则和真实 LanceDB 一样：所有对外抛出的异常都继承自一个根异常
:class:`MiniLanceError`，调用方只需要 ``except MiniLanceError`` 就能兜住
整个库的错误。内部再按"出错阶段"分层，方便业务代码区分：

* 参数/契约错误（用户写错了）—— :class:`SchemaError` / :class:`QueryError`
* 数据完整性错误（磁盘上的东西坏了）—— :class:`CorruptedFileError`
* 并发/事务冲突（重试可能成功）—— :class:`CommitConflict`
* 资源不存在 —— :class:`TableNotFoundError` / :class:`VersionNotFoundError`
* 能力缺口（本实现没做，真实 LanceDB 有）—— :class:`NotSupportedError`
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

__all__ = [
    "MiniLanceError",
    "SchemaError",
    "TypeMismatchError",
    "DataValidationError",
    "QueryError",
    "PlanError",
    "ExpressionSyntaxError",
    "IndexErrorBase",
    "IndexNotFoundError",
    "IndexBuildError",
    "TableNotFoundError",
    "TableAlreadyExistsError",
    "VersionNotFoundError",
    "TagNotFoundError",
    "CommitConflict",
    "StoreError",
    "CorruptedFileError",
    "NotSupportedError",
]


class MiniLanceError(Exception):
    """mini-LanceDB 所有异常的根。"""

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        #: 结构化上下文，便于日志里直接打成 kv 对
        self.context = context
        self.message = message

    def __str__(self) -> str:  # pragma: no cover - 简单透传
        if not self.context:
            return self.message
        kv = ", ".join(f"{k}={v!r}" for k, v in sorted(self.context.items()))
        return f"{self.message} ({kv})"


# --------------------------------------------------------------------------
# Schema / 数据
# --------------------------------------------------------------------------


class SchemaError(MiniLanceError):
    """Schema 本身不合法（重名字段、循环嵌套、字段 id 冲突……）。"""


class TypeMismatchError(MiniLanceError):
    """值/列的类型与 Schema 声明不兼容，且无法隐式转换。"""


class DataValidationError(MiniLanceError):
    """数据不满足约束：向量维度不对、非空列出现 null、字符串不是合法 UTF-8 等。

    对应真实 LanceDB 里 ``on_bad_vectors`` 处理的那些场景。
    """


# --------------------------------------------------------------------------
# 查询 / 表达式
# --------------------------------------------------------------------------


class QueryError(MiniLanceError):
    """查询请求本身不合法（limit 为负、同时给了两个向量列……）。"""


class PlanError(QueryError):
    """查询规划阶段失败：引用了不存在的列、索引无法用于该表达式等。"""


class ExpressionSyntaxError(QueryError):
    """filter / SQL 字符串解析失败。

    刻意带上出错位置，因为表达式解析器的报错质量几乎决定了
    ``.where("...")`` 这个 API 好不好用。
    """

    def __init__(
        self,
        message: str,
        *,
        expression: Optional[str] = None,
        position: Optional[int] = None,
    ) -> None:
        ctx: dict[str, Any] = {}
        if expression is not None:
            ctx["expression"] = expression
            if position is not None:
                # 用插入符标出出错位置，终端里一眼能看到
                ctx["caret"] = " " * position + "^"
        super().__init__(message, **ctx)
        self.expression = expression
        self.position = position


# --------------------------------------------------------------------------
# 索引
# --------------------------------------------------------------------------


class IndexErrorBase(MiniLanceError):
    """索引相关错误基类（不要和内置的 ``IndexError`` 混淆）。"""


class IndexNotFoundError(IndexErrorBase):
    """请求的索引不存在。"""


class IndexBuildError(IndexErrorBase):
    """索引构建失败：训练样本不足、维度不整除、参数非法……。"""


# --------------------------------------------------------------------------
# 目录 / 版本
# --------------------------------------------------------------------------


class TableNotFoundError(MiniLanceError):
    """表（数据集）不存在。"""


class TableAlreadyExistsError(MiniLanceError):
    """``mode="create"`` 但表已经存在。"""


class VersionNotFoundError(MiniLanceError):
    """时间旅行到某个不存在的版本。"""


class TagNotFoundError(MiniLanceError):
    """标签不存在。"""


class CommitConflict(MiniLanceError):
    """提交时检测到并发写：期望的版本号已经被别人推翻了。

    真实 LanceDB 在对象存储上用的是"条件写"（S3 的
    ``If-None-Match``）来实现同样的语义。调用方应该重新读取
    manifest 后重试，而不是直接失败。
    """


# --------------------------------------------------------------------------
# 存储
# --------------------------------------------------------------------------


class StoreError(MiniLanceError):
    """对象存储层错误（网络、权限、路径非法……）。"""


class CorruptedFileError(MiniLanceError):
    """文件头/页表/校验和不匹配。

    注意：真实 Lance 在读文件时**不会**主动校验整个文件的哈希，
    只在结构性位置（magic、页表偏移）做检查。
    """


class NotSupportedError(MiniLanceError):
    """该能力在本教学实现里没有实现（但真实 LanceDB 提供）。

    我们宁可显式抛出这个异常，也不静默给一个错误答案 —— 这是
    "mini 实现" 能被信任的前提。
    """

    def __init__(self, feature: str, *, hint: Optional[str] = None) -> None:
        msg = f"mini-LanceDB 未实现该能力: {feature}"
        if hint:
            msg += f"；{hint}"
        super().__init__(msg, feature=feature)


def format_error_chain(exc: BaseException, *, limit: int = 8) -> str:
    """把异常链拍平成一行行文本，方便在 notebook / 日志里看。"""

    lines: list[str] = []
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and len(lines) < limit and id(cur) not in seen:
        seen.add(id(cur))
        lines.append(f"{type(cur).__name__}: {cur}")
        cur = cur.__cause__ or cur.__context__
    return "\n".join(lines)


def first_of(errors: Iterable[BaseException]) -> Optional[BaseException]:
    """从一批异常里挑出第一个（用于批量操作后的汇总报错）。"""

    for e in errors:
        return e
    return None

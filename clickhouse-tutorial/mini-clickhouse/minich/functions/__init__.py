"""标量函数库 —— ClickHouse 的 1000 多个函数在这里被**注册**。

.. code-block:: text

   注册 = 把一个名字绑到一个 FunctionBase 子类上
          (见 base.py 的 register_function)

这个包**没有对外 API** —— 它的副作用就是"导入即注册"。任何想用函数的
地方(求值器、系统表、测试)只需要 ``import minich.functions``,然后
通过 :func:`minich.functions.base.get_function` 按名字拿。

===========================  ======================================================
模块                          内容
===========================  ======================================================
``base``                     注册表、``FunctionBase``、NULL 传播、lambda 实参
``arithmetic``               算术、位运算、类型提升规则
``math_funcs``               数学函数
``rounding``                 取整与舍入
``comparison``               比较、``ifNull``/``nullIf``、``greatest``/``least``
``logical``                  三值逻辑的 ``and``/``or``/``not``
``conditional``              惰性求值的 ``if``/``multiIf``、``coalesce``
``type_funcs``               类型转换(``toXxx`` / ``reinterpretAsXxx`` / ``CAST``)
``string``                   字符串(含 ``length`` 与 ``lengthUTF8`` 的区别)
``datetime``                 日期时间与时区
``array``                    数组函数(1-based 下标)
``higher_order``             高阶函数(``arrayMap`` 等,收 lambda)
``hashing``                  哈希函数(``sipHash64`` / ``MD5`` …)
``encoding``                 hex/base64/bin 编解码
``json_funcs``               JSON 提取(``visitParam*`` 与 ``JSONExtract*`` 两族)
``url_ip``                   URL 与 IP 函数
``tuple_map``                元组与 Map
``misc``                     杂项(``now``/``version``/``bar``/``neighbor``)
===========================  ======================================================

.. note::

   **导入顺序不影响注册**(注册表是一个 dict,后注册的覆盖先注册的,
   所以重复注册是安全的)。但为了让"哪些名字存在"这件事确定,
   我们仍然显式列出全部模块。

   **函数名的匹配是大小写不敏感的**:注册时统一小写存放
   (``register_function`` 的 ``case_insensitive=True``),
   查询时也小写化再查。所以 ``SUM``、``Sum``、``sum`` 都能用 ——
   这与真实 ClickHouse 一致。
"""

from __future__ import annotations

# 先导入 base(注册表),再导入各函数模块 —— 每个模块在被导入时
# 通过 @register_function 装饰器完成注册。
from . import base  # noqa: F401
from . import arithmetic  # noqa: F401
from . import math_funcs  # noqa: F401
from . import rounding  # noqa: F401
from . import comparison  # noqa: F401
from . import logical  # noqa: F401
from . import conditional  # noqa: F401
from . import type_funcs  # noqa: F401
from . import string  # noqa: F401
from . import datetime  # noqa: F401
from . import array  # noqa: F401
from . import higher_order  # noqa: F401
from . import hashing  # noqa: F401
from . import encoding  # noqa: F401
from . import json_funcs  # noqa: F401
from . import url_ip  # noqa: F401
from . import tuple_map  # noqa: F401
from . import misc  # noqa: F401

from .base import (  # noqa: F401
    FunctionBase,
    LambdaArgument,
    function_exists,
    get_function,
    register_function,
    registered_functions,
)

# ``dictGet`` 家族住在 :mod:`minich.dictionaries`(字典子系统的家门口),
# 但它们**是标量函数**,必须在这里被注册到表里 —— 否则在"还没建过任何
# 字典"的进程里,``SELECT dictGet(...)`` 会报 ``UNKNOWN_FUNCTION`` 而不是
# 更有意义的 ``Dictionary not found``。放在最后导入,是为了让
# ``minich.functions.base`` 已经就绪(dictionaries 会用它的 register_function)。
from .. import dictionaries  # noqa: E402,F401


__all__ = [
    "FunctionBase",
    "LambdaArgument",
    "function_exists",
    "get_function",
    "register_function",
    "registered_functions",
]

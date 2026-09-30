"""数据库只读模式开关：环境变量 ``QUANT_DB_READ_ONLY``，**默认关闭**。

为什么需要它
------------
三个库（SQLite 主库、SQLite 外部交易库、DuckDB 分析库）都在 **import 阶段**就建表/补列/
建索引/重建视图——也就是「只要 import 就会写库」。生产上这是必须的自动迁移，但在下面两种
场景里是有害的：

- 本地开发/测试想直接读线上库或线上库的副本（analytics.duckdb 有 50GB+ 量级），
  一 import 就往里写：既有写坏数据的风险，也常常因为目录不可写或拿不到写锁直接炸在 import 上
  （``PermissionError: makedirs`` / ``Could not set lock``）；
- 用现有代码写只读分析脚本时，只想借它的查询能力，不想要任何副作用。

用法
----
.. code-block:: bash

    QUANT_DB_READ_ONLY=true python your_readonly_script.py

开启后：

- 三个库的初始化（建表、补列、删旧表/旧列、建索引、重建视图）全部跳过，每个步骤只记一条
  warning，方便确认"确实没写"；
- DuckDB 一律用 ``read_only=True`` 打开，不再尝试获取写锁；
- ``connect_duckdb_for_write()`` 直接抛 :class:`ReadOnlyModeError`，避免出现"以为写进去了
  其实没写"的静默失败。

没有开启时行为与改动前完全一致，生产默认路径不受影响。
"""
from __future__ import annotations

import logging
import os
from typing import Set

logger = logging.getLogger(__name__)

READ_ONLY_ENV_VAR = "QUANT_DB_READ_ONLY"

# 只有这些取值算"开"；空串、未设置、其它任何值都是关（默认 false）
_TRUTHY_VALUES = frozenset({"1", "true", "yes", "y", "on", "t"})

# 已经打过日志的初始化步骤，避免每个步骤重复刷屏
_logged_steps: Set[str] = set()


class ReadOnlyModeError(RuntimeError):
    """只读模式下尝试打开写连接时抛出。"""


def is_read_only() -> bool:
    """``QUANT_DB_READ_ONLY`` 是否开启。每次调用都读环境变量，测试可以直接 monkeypatch。"""
    return (os.getenv(READ_ONLY_ENV_VAR) or "").strip().lower() in _TRUTHY_VALUES


def skip_init_writes(step: str) -> bool:
    """初始化写操作是否应当跳过。

    返回 True 表示当前处于只读模式、调用方必须直接返回；每个 ``step`` 只记一次日志。
    用法：

    .. code-block:: python

        def ensure_table_columns():
            if skip_init_writes("主库补列 ensure_table_columns"):
                return
            ...
    """
    if not is_read_only():
        return False
    if step not in _logged_steps:
        _logged_steps.add(step)
        logger.warning("只读模式（%s=true）：跳过初始化写操作 %s", READ_ONLY_ENV_VAR, step)
    return True


def reset_skip_log() -> None:
    """清空"已记过日志"的步骤集合。仅供测试使用。"""
    _logged_steps.clear()

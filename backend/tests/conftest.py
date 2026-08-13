"""pytest 全局准备：在任何 src 模块导入前，把数据库路径改到独立的临时目录。

`core.database`、`core.analytics_database`、`core.external_trading_database`
在 import 时会按路径建目录，默认值都指向生产数据目录 /var/lib/quant_robot。
测试既不该触碰生产数据，也不该依赖该目录可写，因此统一注入测试目录；
显式设置过同名环境变量时仍以环境变量为准。
"""

import os
import tempfile

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="quant_pytest_")

os.environ.setdefault(
    "QUANT_SQLITE_PATH", os.path.join(_TEST_DATA_DIR, "evc_stocks.db")
)
os.environ.setdefault(
    "ANALYTICS_DB_PATH", os.path.join(_TEST_DATA_DIR, "analytics.duckdb")
)
os.environ.setdefault(
    "EXTERNAL_TRADING_DB_PATH", os.path.join(_TEST_DATA_DIR, "external_trading.db")
)

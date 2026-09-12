"""选股系统回测子进程入口（由 ``backtest_runner.start_backtest`` 启动）。

父进程已经把 ``ANALYTICS_DB_PATH`` 指向回测数据工作区、``STOCK_SYSTEM_BACKTEST_SOURCE_DB`` 指向生产
分析库。这里先删掉上一次的工作区文件，再 import 分析库模块——按正式定义在新文件里建好表结构和前复权
视图——然后由 ``execute_run`` 复制数据、回放、写结果。
"""
import logging
import os
import sys


def main(run_id: int) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    workspace = os.environ["ANALYTICS_DB_PATH"]
    source = os.environ["STOCK_SYSTEM_BACKTEST_SOURCE_DB"]
    if os.path.abspath(workspace) == os.path.abspath(source):
        print("回测工作区不能是生产分析库本身", file=sys.stderr)
        return 2
    for suffix in ("", ".wal"):
        if os.path.exists(workspace + suffix):
            os.remove(workspace + suffix)

    from ..core import analytics_database  # noqa: F401  在工作区里建表结构和视图
    from ..core.services.stock_system.backtest_runner import execute_run

    return execute_run(run_id, source)


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1])))

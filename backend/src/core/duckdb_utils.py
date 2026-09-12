import logging
import os
import time


ANALYTICS_DB_PATH = os.getenv("ANALYTICS_DB_PATH", "/var/lib/quant_robot/analytics.duckdb")
DUCKDB_CONFIG_MISMATCH_MESSAGE = "Can't open a connection to same database file with a different configuration than existing connections"
# 另一个进程持有写锁时 duckdb.connect 抛出的 IOException 文案
DUCKDB_LOCK_CONFLICT_MESSAGE = "Could not set lock"
# 写锁等待：定时任务网格里单个任务最长要跑几分钟，等 5 分钟覆盖绝大多数碰撞
WRITE_LOCK_RETRY_ATTEMPTS = 60
WRITE_LOCK_RETRY_SLEEP_SECONDS = 5.0

logger = logging.getLogger(__name__)


def is_duckdb_config_mismatch(exc: Exception) -> bool:
    return DUCKDB_CONFIG_MISMATCH_MESSAGE in str(exc)


def is_duckdb_lock_conflict(exc: Exception) -> bool:
    return DUCKDB_LOCK_CONFLICT_MESSAGE in str(exc)


def connect_duckdb(database: str = ANALYTICS_DB_PATH, prefer_read_only: bool = True):
    import duckdb

    # DuckDB requires every open connection to the same file in a process to use
    # the same configuration. The backend also writes from scheduler threads, so
    # read paths open with read-write configuration first to avoid blocking syncs.
    attempts = [False, True] if prefer_read_only else [False]
    last_exc = None
    for read_only in attempts:
        try:
            return duckdb.connect(database=database, read_only=read_only)
        except Exception as exc:
            if is_duckdb_config_mismatch(exc):
                last_exc = exc
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("无法连接DuckDB分析库")


def connect_duckdb_engine(database: str = ANALYTICS_DB_PATH, prefer_read_only: bool = False):
    import duckdb_engine

    return duckdb_engine.ConnectionWrapper(
        connect_duckdb(database=database, prefer_read_only=prefer_read_only)
    )


def connect_duckdb_for_write(
    database: str = ANALYTICS_DB_PATH,
    *,
    attempts: int = WRITE_LOCK_RETRY_ATTEMPTS,
    sleep_seconds: float = WRITE_LOCK_RETRY_SLEEP_SECONDS,
    sleep=time.sleep,
):
    """打开写连接；写锁被其它进程占用时有界退避重试。

    DuckDB 是单写者，定时任务网格里几分钟级的写任务互相撞上是必然事件，而
    ``duckdb.connect`` 撞锁会直接抛 IOException，把整轮写入作废。只对"拿不到锁"
    这一种错误重试，其它异常照常抛出；等满仍拿不到锁时抛出最后一次的异常。
    """
    total_attempts = max(1, int(attempts))
    for attempt in range(1, total_attempts + 1):
        try:
            return connect_duckdb(database, prefer_read_only=False)
        except Exception as exc:
            if not is_duckdb_lock_conflict(exc) or attempt >= total_attempts:
                raise
            if attempt == 1 or attempt % 6 == 0:
                logger.warning(
                    "DuckDB 写锁被占用，第 %s/%s 次等待 %.1f 秒后重试: %s",
                    attempt, total_attempts, sleep_seconds, exc,
                )
            sleep(sleep_seconds)
    raise RuntimeError("无法获取DuckDB写锁")

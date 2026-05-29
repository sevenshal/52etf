import os


ANALYTICS_DB_PATH = os.getenv("ANALYTICS_DB_PATH", "/var/lib/quant_robot/analytics.duckdb")
DUCKDB_CONFIG_MISMATCH_MESSAGE = "Can't open a connection to same database file with a different configuration than existing connections"


def is_duckdb_config_mismatch(exc: Exception) -> bool:
    return DUCKDB_CONFIG_MISMATCH_MESSAGE in str(exc)


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

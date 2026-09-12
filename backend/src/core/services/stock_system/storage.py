"""选股系统快照表的读写工具。

所有快照表都以交易日为主键前缀：同一交易日重跑时整批覆盖，一次事务里写完一组表，
读的一方要么看到旧的一整组、要么看到新的一整组。数值列写成可空类型，缺失值落库为
NULL 而不是 NaN——DuckDB 里 NaN 比任何数都大，排序和比较都会出错。
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence, Tuple

import pandas as pd

from ...duckdb_utils import ANALYTICS_DB_PATH, connect_duckdb_for_write


def ensure_tables(*models) -> None:
    """快照表以 ORM 模型为准；显式建表，不依赖别处先 import 过分析库模块。"""
    from ...analytics_database import AnalyticsBase, analytics_engine

    AnalyticsBase.metadata.create_all(analytics_engine, tables=[model.__table__ for model in models])


def snapshot_frame(
    records: Iterable[Mapping[str, Any]],
    columns: Sequence[str],
    *,
    text_columns: Sequence[str] = (),
    bool_columns: Sequence[str] = (),
    int_columns: Sequence[str] = (),
    raw_columns: Sequence[str] = (),
    json_columns: Sequence[str] = (),
) -> pd.DataFrame:
    """按列类型收敛成可直接 INSERT 的 DataFrame；未点名的列一律按可空浮点处理。

    ``raw_columns`` 原样保留（日期、时间戳）；``json_columns`` 序列化成 JSON 文本。
    """
    rows = []
    for record in records:
        row = {column: record.get(column) for column in columns}
        for column in json_columns:
            row[column] = json.dumps(record.get(column), ensure_ascii=False)
        rows.append(row)
    frame = pd.DataFrame(rows, columns=list(columns))
    untouched = set(text_columns) | set(raw_columns) | set(json_columns)
    for column in columns:
        if column in untouched:
            continue
        if column in bool_columns:
            frame[column] = frame[column].fillna(False).astype(bool)
        elif column in int_columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("Int64")
        else:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("Float64")
    return frame


def replace_trade_date_rows(trade_date: date, writes: Sequence[Tuple[str, pd.DataFrame]]) -> None:
    """一个事务里把若干张表中该交易日的行整批替换。"""
    connection = connect_duckdb_for_write(ANALYTICS_DB_PATH)
    try:
        connection.execute("BEGIN TRANSACTION")
        for index, (table, frame) in enumerate(writes):
            connection.execute(f"DELETE FROM {table} WHERE trade_date = ?", [trade_date])
            if frame is None or frame.empty:
                continue
            view_name = f"stock_system_frame_{index}"
            quoted = ", ".join(f'"{column}"' for column in frame.columns)
            connection.register(view_name, frame)
            connection.execute(f"INSERT INTO {table} ({quoted}) SELECT {quoted} FROM {view_name}")
            connection.unregister(view_name)
        connection.execute("COMMIT")
    except Exception:
        try:
            connection.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        connection.close()


def json_or(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def clean_value(value: Any) -> Any:
    """DuckDB/pandas 取回来的值转成可 JSON 序列化的普通类型；NaN/NA 一律转 None。"""
    if value is None or isinstance(value, (str, bool, list, dict)):
        return value
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat() if value == value.normalize() else value.isoformat()
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def clean_record(row: Mapping[str, Any], json_columns: Mapping[str, Any] = None) -> dict:
    """一行快照转普通 dict；``json_columns`` 给出 JSON 列及解析失败时的默认值。"""
    json_columns = json_columns or {}
    record = {key: clean_value(value) for key, value in row.items() if key not in json_columns}
    for column, fallback in json_columns.items():
        record[column] = json_or(row.get(column), fallback)
    return record


def run_record(row: Mapping[str, Any]) -> dict:
    """运行记录行：config_json / summary_json 解析成 config / summary。"""
    record = clean_record(row, {"config_json": {}, "summary_json": {}})
    record["config"] = record.pop("config_json")
    record["summary"] = record.pop("summary_json")
    return record

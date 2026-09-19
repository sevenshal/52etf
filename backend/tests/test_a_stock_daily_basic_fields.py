import logging
from datetime import date

import duckdb
import pandas as pd

from src.core.services import tushare as tushare_module
from src.core.services.tushare import TushareService
from src.robot import a_stock_daily_basic_backfill as backfill
from src.robot.a_stock_base_data_sync import AStockBaseDataSyncService


class _FakePro:
    def __init__(self, reject_limit_status: bool):
        self.reject_limit_status = reject_limit_status
        self.calls = []

    def daily_basic(self, fields, **params):
        self.calls.append(fields)
        if self.reject_limit_status and "limit_status" in fields:
            raise RuntimeError("抱歉，您没有访问该字段的权限")
        columns = fields.split(",")
        row = {column: 1.0 for column in columns}
        row.update({"ts_code": "000001.SZ", "trade_date": "20260918"})
        if "limit_status" in columns:
            row["limit_status"] = 2
        return pd.DataFrame([row])


def _service(pro):
    service = TushareService.__new__(TushareService)
    service.pro = pro
    service.logger = logging.getLogger("test")
    return service


def test_daily_basic_requests_every_documented_field():
    pro = _FakePro(reject_limit_status=False)
    frame = _service(pro).get_a_stock_daily_basic_frame(date(2026, 9, 18))
    assert pro.calls == [tushare_module.A_STOCK_DAILY_BASIC_FIELDS]
    requested = set(tushare_module.A_STOCK_DAILY_BASIC_FIELDS.split(","))
    # tushare 文档的输出参数全集；close 与 daily 重复，由 daily 接口提供
    assert requested == {
        "ts_code", "trade_date", "turnover_rate", "turnover_rate_f", "volume_ratio", "pe", "pe_ttm", "pb",
        "ps", "ps_ttm", "dv_ratio", "dv_ttm", "total_share", "float_share", "free_share", "total_mv",
        "circ_mv", "limit_status",
    }
    assert frame.iloc[0]["limit_status"] == 2


def test_daily_basic_falls_back_without_limit_status():
    pro = _FakePro(reject_limit_status=True)
    frame = _service(pro).get_a_stock_daily_basic_frame(date(2026, 9, 18))
    assert pro.calls[-1] == tushare_module.A_STOCK_DAILY_BASIC_FALLBACK_FIELDS
    assert "limit_status" not in pro.calls[-1]
    assert len(frame) == 1 and "ps_ttm" in frame.columns


def test_normalize_market_frame_keeps_new_daily_basic_columns():
    frame = pd.DataFrame([{
        "ts_code": "000001.SZ", "trade_date": date(2026, 9, 18), "open": 1, "high": 1, "low": 1, "close": 1,
        "pre_close": 1, "ps": 3.5, "ps_ttm": 3.2, "turnover_rate_f": 1.1, "free_share": 100.0, "limit_status": 2.0,
    }])
    normalized = AStockBaseDataSyncService._normalize_market_frame(frame)
    row = normalized.iloc[0]
    assert (row["ps"], row["ps_ttm"], row["turnover_rate_f"], row["free_share"]) == (3.5, 3.2, 1.1, 100.0)
    assert row["limit_status"] == 2
    assert str(normalized["limit_status"].dtype) == "Int64"


def test_backfill_writes_new_columns(monkeypatch, tmp_path):
    path = str(tmp_path / "a.duckdb")
    connection = duckdb.connect(path)
    connection.execute(
        "CREATE TABLE a_stock_market_daily (ts_code VARCHAR, trade_date DATE, close DOUBLE, pe DOUBLE, pe_ttm DOUBLE,"
        " pb DOUBLE, ps DOUBLE, ps_ttm DOUBLE, dv_ratio DOUBLE, dv_ttm DOUBLE, volume_ratio DOUBLE,"
        " turnover_rate_f DOUBLE, free_share DOUBLE, limit_status INTEGER)"
    )
    connection.execute("INSERT INTO a_stock_market_daily (ts_code, trade_date, close) VALUES ('000001.SZ', '2026-09-18', 10)")
    connection.close()
    monkeypatch.setattr(backfill, "connect_duckdb_for_write", lambda *_args, **_kwargs: duckdb.connect(path))
    frame = pd.DataFrame([{
        "ts_code": "000001.SZ", "trade_date": date(2026, 9, 18), "pe": 10, "pe_ttm": 11, "pb": 1.5, "ps": 2,
        "ps_ttm": 2.1, "dv_ratio": 1, "dv_ttm": 1.2, "volume_ratio": 0.9, "turnover_rate_f": 3, "free_share": 50,
        "limit_status": 3,
    }])
    assert backfill._write_batch(frame) == 1
    connection = duckdb.connect(path, read_only=True)
    row = connection.execute("SELECT ps, ps_ttm, turnover_rate_f, free_share, limit_status FROM a_stock_market_daily").fetchone()
    connection.close()
    assert row == (2.0, 2.1, 3.0, 50.0, 3)

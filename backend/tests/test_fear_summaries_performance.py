from datetime import date
from unittest.mock import patch

from src.app.api import cnn
from src.core.database import AStockIndexValuationSnapshot, Session
from src.core.services.a_stock_index_valuation import load_a_stock_index_valuation, load_a_stock_index_valuations
from src.core.services.fed_rate_monitor import FedRateMonitorService

TEST_SYMBOLS = ["PERFTEST1.SH", "PERFTEST2.SH"]


def _delete_test_snapshots():
    db = Session()
    try:
        db.query(AStockIndexValuationSnapshot).filter(
            AStockIndexValuationSnapshot.symbol.in_(TEST_SYMBOLS)
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        Session.remove()


def test_batch_valuations_return_latest_snapshot_per_symbol():
    _delete_test_snapshots()
    db = Session()
    try:
        db.add_all([
            AStockIndexValuationSnapshot(symbol="PERFTEST1.SH", date=date(2026, 9, 10), payload={"current_gap_pct": 10.0}),
            AStockIndexValuationSnapshot(symbol="PERFTEST1.SH", date=date(2026, 9, 11), payload={"current_gap_pct": 12.0}),
            AStockIndexValuationSnapshot(symbol="PERFTEST2.SH", date=date(2026, 9, 9), payload={"current_gap_pct": -3.0}),
        ])
        db.commit()
    finally:
        Session.remove()
    try:
        result = load_a_stock_index_valuations(["perftest1.sh", "PERFTEST2.SH", "PERFMISSING.SH"])
        assert result["PERFTEST1.SH"]["current_gap_pct"] == 12.0
        assert result["PERFTEST2.SH"]["current_gap_pct"] == -3.0
        assert result["PERFMISSING.SH"]["status"] == "unavailable"
        # 单只接口复用批量实现，结果一致
        assert load_a_stock_index_valuation("PERFTEST1.SH")["current_gap_pct"] == 12.0
        assert load_a_stock_index_valuation("")["status"] == "unavailable"
    finally:
        _delete_test_snapshots()


def test_summary_valuations_load_in_one_batch():
    with patch.object(cnn, "load_a_stock_index_valuations", return_value={"000015.SH": {"status": "available"}}) as a_loader, \
            patch.object(cnn, "load_us_index_valuation", return_value={"status": "available"}) as us_loader:
        result = cnn._load_summary_valuations(["000015.SH", "qqq.us", "HSI.HK"])
    a_loader.assert_called_once_with(["000015.SH"])
    us_loader.assert_called_once_with("QQQ.US")
    assert set(result) == {"000015.SH", "QQQ.US"}


class _FakeCache(dict):
    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value, expire=None):
        self[key] = value


def test_fed_rate_fetch_failure_is_cached_to_avoid_refetch_every_page_load():
    with patch.object(FedRateMonitorService, "CACHE", _FakeCache()), \
            patch.object(FedRateMonitorService, "_fetch_html", return_value=None) as fetch_html:
        assert FedRateMonitorService.fetch_data() == []
        assert FedRateMonitorService.fetch_data() == []
    assert fetch_html.call_count == 1

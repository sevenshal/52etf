from datetime import date
from unittest import mock

import requests

from src.robot.hk_stock_base_data_sync import HKStockBaseDataSyncService


def _empty_yahoo_response():
    response = mock.Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"chart": {"result": None}}
    return response


def test_yahoo_stock_history_uses_dedicated_proxy():
    response = _empty_yahoo_response()
    with mock.patch(
        "src.robot.hk_stock_base_data_sync.requests.get",
        return_value=response,
    ) as get, mock.patch(
        "src.robot.hk_stock_base_data_sync._wait_for_yahoo_request_slot"
    ) as wait:
        frame = HKStockBaseDataSyncService._fetch_yahoo_history(
            "00291.HK",
            date(2026, 1, 1),
            date(2026, 8, 24),
        )

    assert frame.empty
    wait.assert_called_once_with()
    assert get.call_args.kwargs["proxies"] == {
        "http": "socks5h://127.0.0.1:7891",
        "https": "socks5h://127.0.0.1:7891",
    }


def test_yahoo_index_history_uses_dedicated_proxy():
    response = _empty_yahoo_response()
    service = HKStockBaseDataSyncService(tushare_service=mock.Mock())
    with mock.patch(
        "src.robot.hk_stock_base_data_sync.requests.get",
        return_value=response,
    ) as get, mock.patch(
        "src.robot.hk_stock_base_data_sync._wait_for_yahoo_request_slot"
    ) as wait:
        rows = service.sync_yahoo_index(
            "^HSI",
            "HSI",
            date(2026, 1, 1),
            date(2026, 8, 24),
        )

    assert rows == 0
    wait.assert_called_once_with()
    assert get.call_args.kwargs["proxies"] == {
        "http": "socks5h://127.0.0.1:7891",
        "https": "socks5h://127.0.0.1:7891",
    }


def test_yahoo_history_retries_transient_ssl_failure():
    response = _empty_yahoo_response()
    with mock.patch(
        "src.robot.hk_stock_base_data_sync.requests.get",
        side_effect=[requests.exceptions.SSLError("unexpected EOF"), response],
    ) as get, mock.patch(
        "src.robot.hk_stock_base_data_sync._wait_for_yahoo_request_slot"
    ) as wait:
        frame = HKStockBaseDataSyncService._fetch_yahoo_history(
            "01929.HK",
            date(2026, 1, 1),
            date(2026, 8, 25),
        )

    assert frame.empty
    assert get.call_count == 2
    assert wait.call_count == 2


def test_normalize_csi_hk_constituent_keeps_numeric_code():
    from src.robot.hk_stock_base_data_sync import normalize_csi_hk_constituent

    assert normalize_csi_hk_constituent("06600!AB.HK") == "06600.HK"
    assert normalize_csi_hk_constituent("2269.HK") == "02269.HK"
    assert normalize_csi_hk_constituent("600276.SH") is None


def test_sync_csi_index_weights_rescales_months_and_reports_new_symbols():
    import pandas as pd
    from src.core.duckdb_utils import connect_duckdb
    from src.core.analytics_database import ANALYTICS_DB_PATH

    weights = pd.DataFrame(
        {
            "index_code": ["931250.CSI"] * 3,
            "con_code": ["02269.HK", "06600!AB.HK", "01801.HK"],
            "trade_date": ["20221031"] * 3,
            "weight": [60.0, 20.0, 18.94],
        }
    )
    pro = mock.Mock()
    pro.index_weight.return_value = weights
    service = HKStockBaseDataSyncService(tushare_service=mock.Mock(pro=pro))
    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=False)
    try:
        connection.execute("DELETE FROM hk_index_weight_snapshot WHERE index_code = '931250'")
    finally:
        connection.close()

    result = service.sync_csi_index_weights(date(2022, 10, 1), date(2022, 10, 31))

    assert pro.index_weight.call_args.kwargs["index_code"] == "931250.CSI"
    assert result["snapshots"] == 1
    assert result["new_symbols"] == ["01801.HK", "02269.HK", "06600.HK"]
    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
    try:
        total, verified = connection.execute(
            "SELECT SUM(weight), MIN(verified) FROM hk_index_weight_snapshot WHERE index_code = '931250'"
        ).fetchone()
    finally:
        connection.close()
    assert abs(total - 100.0) < 1e-4
    assert verified == 1.0

    again = service.sync_csi_index_weights(date(2022, 10, 1), date(2022, 10, 31))
    assert again["new_symbols"] == []


def test_sync_symbols_history_falls_back_to_tushare_for_yahoo_failures():
    service = HKStockBaseDataSyncService(tushare_service=mock.Mock())
    with mock.patch.object(
        service,
        "sync_symbols_history_yahoo",
        return_value={
            "symbols": 2, "completed": 1, "rows": 10,
            "errors": [{"symbol": "06606.HK", "error": "empty"}],
        },
    ), mock.patch.object(service, "sync_market_symbol", return_value=500) as tushare:
        result = service.sync_symbols_history(
            ["02269.HK", "06606.HK"], date(2022, 1, 1), date(2026, 9, 16)
        )

    tushare.assert_called_once_with("06606.HK", date(2022, 1, 1), date(2026, 9, 16))
    assert result["rows"] == 510
    assert result["errors"] == []

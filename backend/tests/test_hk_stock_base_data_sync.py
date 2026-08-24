from datetime import date
from unittest import mock

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

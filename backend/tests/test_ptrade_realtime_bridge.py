import importlib.util
from pathlib import Path


BRIDGE_PATH = Path(__file__).resolve().parents[2] / "ptrade" / "realtime_bridge.py"


def _load_bridge_module():
    spec = importlib.util.spec_from_file_location("ptrade_realtime_bridge", BRIDGE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tick_quote_uses_cumulative_volume_and_turnover_with_tushare_units():
    bridge = _load_bridge_module()
    quote = bridge._quote_from_tick_row({
        "last_px": 10.5,
        "preclose_px": 10.0,
        "open_px": 10.1,
        "high_px": 10.8,
        "low_px": 9.9,
        "up_px": 11.0,
        "down_px": 9.0,
        "business_amount": 123_400,
        "business_balance": 1_234_000.0,
        "vol_ratio": 0.6,
        "turnover_ratio": 0.21,
        "entrust_rate": 61.69,
        "pe_rate": 26.0,
        "pb_rate": 7.02,
        # amount 是持仓量，不能用作成交额。
        "amount": 9_999_999,
        "hsTimeStamp": 20260902103512,
        "trade_status": "TRADE",
    })

    assert quote["volume"] == 1234.0
    assert quote["amount"] == 1234.0
    assert quote["up_px"] == 11.0
    assert quote["down_px"] == 9.0
    assert quote["vol_ratio"] == 0.6
    assert quote["turnover_ratio"] == 0.21
    assert quote["entrust_rate"] == 61.69
    assert quote["pe_rate"] == 26.0
    assert quote["pb_rate"] == 7.02
    assert quote["hs_time"] == "20260902103512"

from src.robot.external_trading_log import (
    summarize_external_trading_executor_result,
    summarize_external_trading_net_asset_snapshot,
)


def test_executor_summary_excludes_sensitive_payload_fields():
    result = {
        "status": "OK",
        "trigger_source": "robot_timer",
        "checked": 2,
        "processed": 1,
        "skipped": 1,
        "failed": 0,
        "accounts": [
            {
                "account_id": 123,
                "account_name": "private account",
                "status": "SUBMITTED",
                "plan": {"demands": [{"quantity": 100, "reference_price": 12.34}]},
            },
            {
                "account_id": 456,
                "status": "SKIPPED",
                "reason": "market_closed",
            },
        ],
    }

    summary = summarize_external_trading_executor_result(result)

    assert summary == {
        "status": "OK",
        "trigger_source": "robot_timer",
        "checked": 2,
        "processed": 1,
        "skipped": 1,
        "failed": 0,
        "account_status_counts": {"SKIPPED": 1, "SUBMITTED": 1},
        "reason_counts": {"market_closed": 1},
    }
    rendered = repr(summary)
    assert "private account" not in rendered
    assert "123" not in rendered
    assert "12.34" not in rendered


def test_net_asset_summary_excludes_records_and_financial_values():
    result = {
        "status": "OK",
        "trading_date": "2026-07-27",
        "checked": 1,
        "recorded": 1,
        "failed": 0,
        "records": [{
            "sub_account_id": 123,
            "sub_account_name": "private account",
            "cash_available": 100000.0,
            "net_asset": 200000.0,
        }],
    }

    summary = summarize_external_trading_net_asset_snapshot(result)

    assert summary == {
        "status": "OK",
        "trading_date": "2026-07-27",
        "checked": 1,
        "recorded": 1,
        "failed": 0,
    }
    assert "records" not in summary

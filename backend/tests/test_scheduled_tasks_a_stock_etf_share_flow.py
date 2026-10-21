from unittest.mock import patch


def test_a_stock_etf_share_flow_sync_is_a_targeted_share_task():
    from src.robot import scheduled_tasks

    assert "a_stock_etf_share_flow_sync" in scheduled_tasks.START_DATE_RUN_TASK_KEYS
    with patch("src.robot.a_stock_base_data_sync.sync_a_stock_etf_share_history") as sync:
        sync.return_value = {
            "start_date": "2015-01-01", "end_date": "2026-10-21",
            "symbols": ["510300.SH"], "saved_rows": 10, "errors": [],
        }
        message = scheduled_tasks._run_a_stock_etf_share_flow_sync(
            start_date="2015-01-01", end_date="2026-10-21",
        )

    assert "saved=10" in message
    assert sync.call_args.args[0] == [
        "510300.SH", "510050.SH", "510500.SH", "512100.SH", "159915.SZ", "588000.SH",
    ]

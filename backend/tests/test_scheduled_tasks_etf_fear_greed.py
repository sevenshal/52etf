from datetime import date
from unittest.mock import patch


def test_etf_fear_greed_backfill_includes_gld_gvz_history():
    from src.robot.scheduled_tasks import _run_etf_fear_greed_backfill

    with (
        patch(
            "src.core.services.etf_fear_greed_clone_service.DEFAULT_ETF_FEAR_GREED_SYMBOLS",
            ["SOXX.US"],
        ),
        patch(
            "src.core.services.etf_fear_greed_clone_service.ETFFearGreedCloneCalculator"
        ) as etf_calculator,
        patch("src.core.services.gold_fear_greed_service.GoldFearGreedCalculator") as gold_calculator,
        patch("src.core.services.etf_fear_greed_clone_service.notify_latest_turn_signals"),
    ):
        etf_calculator.return_value.backfill_to_db.return_value = {
            "saved": 3,
            "start_date": "2022-01-01",
            "end_date": "2026-10-14",
        }
        gold_calculator.return_value.backfill_to_db.return_value = {
            "saved": 3,
            "start_date": "2022-01-01",
            "end_date": "2026-10-14",
        }

        _run_etf_fear_greed_backfill(
            start_date="2024-01-02",
            end_date="2024-01-10",
            history_days=400,
            score_window=252,
            min_periods=120,
        )

    gold_calculator.return_value.backfill_to_db.assert_called_once_with(
        start_date=date(2022, 11, 28),
        end_date=date(2024, 1, 10),
        output_start_date=date(2024, 1, 2),
        history_days=400,
        score_window=252,
        min_periods=120,
    )


def test_etf_fear_greed_backfill_can_limit_to_gld():
    from src.robot.scheduled_tasks import _run_etf_fear_greed_backfill

    with (
        patch(
            "src.core.services.etf_fear_greed_clone_service.DEFAULT_ETF_FEAR_GREED_SYMBOLS",
            ["SOXX.US"],
        ),
        patch(
            "src.core.services.etf_fear_greed_clone_service.ETFFearGreedCloneCalculator"
        ) as etf_calculator,
        patch("src.core.services.gold_fear_greed_service.GoldFearGreedCalculator") as gold_calculator,
        patch("src.core.services.etf_fear_greed_clone_service.notify_latest_turn_signals"),
    ):
        gold_calculator.return_value.backfill_to_db.return_value = {
            "saved": 3,
            "start_date": "2022-01-01",
            "end_date": "2026-10-14",
        }

        _run_etf_fear_greed_backfill(
            start_date="2024-01-02",
            end_date="2024-01-10",
            history_days=400,
            symbols="GLD.US",
        )

    etf_calculator.return_value.backfill_to_db.assert_not_called()
    gold_calculator.return_value.backfill_to_db.assert_called_once()

from types import SimpleNamespace
from unittest import TestCase

from src.app.api.external_trading_accounts import (
    _has_target_position_delta,
    _is_non_empty_target_position,
    _serialize_target_position_status,
)


class ExternalTradingTargetPositionFilterTest(TestCase):
    def test_delta_filter_requires_non_zero_delta(self):
        self.assertFalse(_has_target_position_delta({"delta_quantity": 0}))
        self.assertTrue(_has_target_position_delta({"delta_quantity": 100}))
        self.assertTrue(_has_target_position_delta({"delta_quantity": -100}))

    def test_non_empty_filter_can_include_zero_delta_positions(self):
        row = {
            "target_quantity": 100,
            "current_quantity": 100,
            "available_quantity": 100,
            "effective_quantity": 100,
            "delta_quantity": 0,
            "pending_buy_quantity": 0,
            "pending_sell_quantity": 0,
        }

        self.assertTrue(_is_non_empty_target_position(row))
        self.assertFalse(_has_target_position_delta(row))

    def test_target_status_separates_ledger_delta_from_execution_delta(self):
        target = SimpleNamespace(
            id=1,
            sub_account_id=10,
            strategy_type="snowball_copy_live",
            strategy_config_id=18,
            symbol="301086.SZ",
            target_quantity=0,
            target_weight_pct=None,
            target_value=None,
            reference_price=164.29,
            reference_price_source="signal",
            signal_id=None,
            signal_version=None,
            source_execution_id=None,
            status="ACTIVE",
            valid_until=None,
            updated_at=None,
            created_at=None,
        )
        sub_account = SimpleNamespace(id=10, name="2026滚雪球", enabled=True)
        ledger_position = SimpleNamespace(symbol="301086.SZ", quantity=140, available_quantity=140)

        row = _serialize_target_position_status(
            target,
            sub_account,
            ledger_position,
            {"SELL": 140},
            "2026年滚雪球",
        )

        self.assertEqual(row["delta_quantity"], -140)
        self.assertEqual(row["ledger_delta_quantity"], -140)
        self.assertEqual(row["effective_quantity"], 0)
        self.assertEqual(row["execution_delta_quantity"], 0)
        self.assertIsNone(row["side"])
        self.assertEqual(row["action"], "SELLING")

from unittest import TestCase

from src.app.api.external_trading_accounts import (
    _has_target_position_delta,
    _is_non_empty_target_position,
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

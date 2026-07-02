from unittest import TestCase

from src.app.api.snowball import (
    _should_recalculate_snowball_target,
    _snowball_target_quantity,
)


class SnowballTargetQuantityTest(TestCase):
    def test_regular_a_share_keeps_existing_one_lot_rounding(self):
        self.assertEqual(100, _snowball_target_quantity(1510, 10, "600000.SH"))

    def test_star_market_rounds_small_target_above_150_to_200(self):
        self.assertEqual(200, _snowball_target_quantity(1510, 10, "688001.SH"))
        self.assertEqual(200, _snowball_target_quantity(1510, 10, "SH.688001"))

    def test_star_market_threshold_is_strictly_greater_than_150(self):
        self.assertEqual(100, _snowball_target_quantity(1500, 10, "688001.SH"))

    def test_star_market_existing_100_lot_rounding_below_threshold_is_unchanged(self):
        self.assertEqual(100, _snowball_target_quantity(510, 10, "688001.SH"))

    def test_recalculates_when_lot_delta_value_exceeds_tracking_error_budget(self):
        self.assertTrue(
            _should_recalculate_snowball_target(
                has_old_target=True,
                old_quantity=100,
                old_weight=10.27,
                new_weight=10.27,
                candidate_quantity=200,
                price=390.0,
                base_value=700000.0,
                threshold_pct=5.0,
            )
        )

    def test_keeps_target_when_lot_delta_value_is_within_tracking_error_budget(self):
        self.assertFalse(
            _should_recalculate_snowball_target(
                has_old_target=True,
                old_quantity=100,
                old_weight=2.5,
                new_weight=2.5,
                candidate_quantity=200,
                price=3.5,
                base_value=700000.0,
                threshold_pct=5.0,
            )
        )

    def test_keeps_target_when_weight_and_lot_quantity_are_unchanged(self):
        self.assertFalse(
            _should_recalculate_snowball_target(
                has_old_target=True,
                old_quantity=100,
                old_weight=10.27,
                new_weight=10.28,
                candidate_quantity=100,
                price=390.0,
                base_value=700000.0,
                threshold_pct=5.0,
            )
        )

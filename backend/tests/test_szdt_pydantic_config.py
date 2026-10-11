import importlib
import sys
import warnings
from unittest import TestCase

from src.app.api.szdt import BatchStockCandidate, StockModel
from src.app.api.szdt_configs import SZDTConfigCreate


class SzdtPydanticConfigTest(TestCase):
    def test_stock_models_accept_korean_concept_emotion_area(self):
        stock = StockModel(
            code="US.KORU", name="三倍做多韩国ETF", type=1,
            when_buy=-90, when_sell=80, max_position=100,
            buy_amount=10_000, sell_amount=10_000, buy_factor=1, sell_factor=1,
            lever=3, emo_area="sk",
        )
        candidate = BatchStockCandidate(code="US.KORU", name="三倍做多韩国ETF", lever=3, emo_area="sk")
        self.assertEqual("sk", stock.emo_area)
        self.assertEqual("sk", candidate.emo_area)

    def test_stock_model_accepts_new_a_share_stock_and_hk_connect_types(self):
        shared = dict(
            code="SZ.300750", name="宁德时代", when_buy=-80, when_sell=80, max_position=20,
            buy_amount=10_000, sell_amount=10_000, buy_factor=1, sell_factor=1, lever=1, emo_area="a",
        )
        self.assertEqual(9, StockModel(type=9, **shared).type)
        self.assertEqual(10, StockModel(type=10, **shared).type)

    def test_import_does_not_emit_orm_mode_warning(self):
        module_name = "src.app.api.szdt"
        original_module = sys.modules.pop(module_name, None)

        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                importlib.import_module(module_name)

            orm_mode_warnings = [
                warning for warning in caught if "orm_mode" in str(warning.message)
            ]
            self.assertEqual([], orm_mode_warnings)
        finally:
            sys.modules.pop(module_name, None)
            if original_module is not None:
                sys.modules[module_name] = original_module

    def test_szdt_rotation_config_defaults_and_accepts_threshold(self):
        default = SZDTConfigCreate()
        enabled = SZDTConfigCreate(a_rotation_enabled=True, a_rotation_threshold=-30)
        self.assertFalse(default.a_rotation_enabled)
        self.assertEqual(-30, default.a_rotation_threshold)
        self.assertTrue(enabled.a_rotation_enabled)
        self.assertEqual(-30, enabled.a_rotation_threshold)

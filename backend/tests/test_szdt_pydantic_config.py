import importlib
import sys
import warnings
from unittest import TestCase


class SzdtPydanticConfigTest(TestCase):
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

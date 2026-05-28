from unittest import TestCase

from src.app.api.log import _resolve_log_file


class LogApiTest(TestCase):
    def test_resolve_default_log_file(self):
        key, path = _resolve_log_file()

        self.assertEqual("service", key)
        self.assertEqual("/var/log/quant/service.log", path)

    def test_resolve_error_log_file(self):
        key, path = _resolve_log_file("error.log")

        self.assertEqual("error", key)
        self.assertEqual("/var/log/quant/error.log", path)

    def test_reject_unknown_log_file(self):
        with self.assertRaises(ValueError):
            _resolve_log_file("../../etc/passwd")

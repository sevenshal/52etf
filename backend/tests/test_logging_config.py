import io
import logging
from unittest import TestCase

from src.core.logging_config import configure_logging


class LoggingConfigTest(TestCase):
    def setUp(self):
        self.root = logging.getLogger()
        self.original_handlers = self.root.handlers[:]
        self.original_level = self.root.level

    def tearDown(self):
        self.root.handlers = self.original_handlers
        self.root.setLevel(self.original_level)

    def test_info_and_warning_go_to_stdout_only(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        configure_logging(stdout=stdout, stderr=stderr)

        logging.info("normal operational message")
        logging.warning("warning operational message")

        stdout_value = stdout.getvalue()
        self.assertIn("INFO normal operational message", stdout_value)
        self.assertIn("WARNING warning operational message", stdout_value)
        self.assertEqual("", stderr.getvalue())

    def test_error_goes_to_stdout_and_stderr(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        configure_logging(stdout=stdout, stderr=stderr)

        logging.error("real failure")

        self.assertIn("ERROR real failure", stdout.getvalue())
        self.assertIn("ERROR real failure", stderr.getvalue())

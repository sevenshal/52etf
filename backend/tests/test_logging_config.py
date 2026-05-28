import io
import logging
from unittest import TestCase

from src.core.logging_config import REDACTED, configure_logging


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

    def test_uvicorn_access_log_redacts_sensitive_query_params(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        configure_logging(stdout=stdout, stderr=stderr)

        logging.getLogger("uvicorn.access").info(
            '%s - "WebSocket %s" [accepted]',
            "10.0.0.8:0",
            "/api/external-trading-accounts/ws?account_id=acct-1&identifier=broker-1&ts=1234&nonce=xyz&signature=secret&foo=bar",
        )

        stdout_value = stdout.getvalue()
        self.assertIn(f"account_id={REDACTED}", stdout_value)
        self.assertIn(f"identifier={REDACTED}", stdout_value)
        self.assertIn(f"ts={REDACTED}", stdout_value)
        self.assertIn(f"nonce={REDACTED}", stdout_value)
        self.assertIn(f"signature={REDACTED}", stdout_value)
        self.assertIn("foo=bar", stdout_value)
        self.assertNotIn("acct-1", stdout_value)
        self.assertNotIn("broker-1", stdout_value)
        self.assertNotIn("secret", stdout_value)
        self.assertEqual("", stderr.getvalue())

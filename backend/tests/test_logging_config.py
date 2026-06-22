import io
import logging
from unittest import TestCase

import httpx

from src.core.logging_config import REDACTED, configure_logging


class LoggingConfigTest(TestCase):
    def setUp(self):
        self.root = logging.getLogger()
        self.original_handlers = self.root.handlers[:]
        self.original_level = self.root.level
        self.uvicorn_access_logger = logging.getLogger("uvicorn.access")
        self.uvicorn_access_handlers = self.uvicorn_access_logger.handlers[:]
        self.uvicorn_access_level = self.uvicorn_access_logger.level
        self.uvicorn_access_propagate = self.uvicorn_access_logger.propagate
        self.ib_wrapper_logger = logging.getLogger("ib_insync.wrapper")
        self.ib_wrapper_handlers = self.ib_wrapper_logger.handlers[:]
        self.ib_wrapper_level = self.ib_wrapper_logger.level
        self.ib_wrapper_propagate = self.ib_wrapper_logger.propagate

    def tearDown(self):
        self.root.handlers = self.original_handlers
        self.root.setLevel(self.original_level)
        self.uvicorn_access_logger.handlers = self.uvicorn_access_handlers
        self.uvicorn_access_logger.setLevel(self.uvicorn_access_level)
        self.uvicorn_access_logger.propagate = self.uvicorn_access_propagate
        self.ib_wrapper_logger.handlers = self.ib_wrapper_handlers
        self.ib_wrapper_logger.setLevel(self.ib_wrapper_level)
        self.ib_wrapper_logger.propagate = self.ib_wrapper_propagate

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

    def test_uvicorn_access_logger_handler_inherits_filter(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        configure_logging(stdout=stdout, stderr=stderr)

        access_log_stream = io.StringIO()
        handler = logging.StreamHandler(access_log_stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.uvicorn_access_logger.setLevel(logging.INFO)
        self.uvicorn_access_logger.propagate = False
        self.uvicorn_access_logger.addHandler(handler)

        self.uvicorn_access_logger.info(
            '%s - "WebSocket %s" [accepted]',
            "10.0.0.8:0",
            "/api/external-trading-accounts/ws?account_id=acct-1&identifier=broker-1&ts=1234&nonce=xyz&signature=secret&foo=bar",
        )

        log_value = access_log_stream.getvalue()
        self.assertIn(f"account_id={REDACTED}", log_value)
        self.assertIn(f"identifier={REDACTED}", log_value)
        self.assertIn(f"ts={REDACTED}", log_value)
        self.assertIn(f"nonce={REDACTED}", log_value)
        self.assertIn(f"signature={REDACTED}", log_value)
        self.assertIn("foo=bar", log_value)
        self.assertNotIn("acct-1", log_value)
        self.assertNotIn("broker-1", log_value)
        self.assertNotIn("secret", log_value)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_httpx_url_object_redacts_sensitive_query_params(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        configure_logging(stdout=stdout, stderr=stderr)

        logging.getLogger("httpx").info(
            'HTTP Request: GET %s "HTTP/1.1 200 OK"',
            httpx.URL("https://financialmodelingprep.com/stable/profile?symbol=EUR&apikey=fmp-real-key"),
        )

        stdout_value = stdout.getvalue()
        self.assertIn(f"apikey={REDACTED}", stdout_value)
        self.assertIn("symbol=EUR", stdout_value)
        self.assertNotIn("fmp-real-key", stdout_value)
        self.assertEqual("", stderr.getvalue())

    def test_ib_insync_portfolio_info_logs_are_suppressed(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        configure_logging(stdout=stdout, stderr=stderr)

        logging.getLogger("ib_insync.wrapper").info(
            "updatePortfolio: PortfolioItem(position=100, marketValue=12345, account='DU123456')"
        )
        logging.getLogger("ib_insync.wrapper").warning("connection warning")

        self.assertNotIn("PortfolioItem", stdout.getvalue())
        self.assertNotIn("DU123456", stdout.getvalue())
        self.assertIn("WARNING connection warning", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

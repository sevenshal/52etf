import logging
import sys
from typing import TextIO
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit


LOG_FORMAT = "%(asctime)s [%(process)d] [%(threadName)s] %(levelname)s %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
REDACTED = "<redacted>"
SENSITIVE_QUERY_PARAMS = {
    "account_id",
    "identifier",
    "ts",
    "nonce",
    "signature",
    "token",
    "cttoken",
    "uttoken",
    "access_token",
    "refresh_token",
    "authorization",
    "auth",
    "api_key",
    "apikey",
    "secret",
}


def redact_sensitive_query_params(value):
    if not isinstance(value, str) or "?" not in value or "=" not in value:
        return value

    split_result = urlsplit(value)
    if not split_result.query:
        return value

    pairs = parse_qsl(split_result.query, keep_blank_values=True)
    redacted = False
    sanitized_pairs = []
    for key, param_value in pairs:
        if key.lower() in SENSITIVE_QUERY_PARAMS:
            sanitized_pairs.append((key, REDACTED))
            redacted = True
            continue
        sanitized_pairs.append((key, param_value))

    if not redacted:
        return value

    sanitized_query = "&".join(
        f"{quote(str(key), safe='')}={quote(str(param_value), safe='<>')}"
        for key, param_value in sanitized_pairs
    )
    return urlunsplit(split_result._replace(query=sanitized_query))


def _sanitize_logging_value(value):
    if isinstance(value, tuple):
        return tuple(_sanitize_logging_value(item) for item in value)
    if isinstance(value, dict):
        return {key: _sanitize_logging_value(item) for key, item in value.items()}
    sanitized = redact_sensitive_query_params(value)
    if sanitized != value:
        return sanitized

    if not isinstance(value, str):
        string_value = str(value)
        sanitized_string = redact_sensitive_query_params(string_value)
        if sanitized_string != string_value:
            return sanitized_string

    return value


class SensitiveQueryParamFilter(logging.Filter):
    def filter(self, record):
        record.msg = _sanitize_logging_value(record.msg)
        if record.args:
            record.args = _sanitize_logging_value(record.args)
        return True


def configure_logging(stdout: TextIO = None, stderr: TextIO = None):
    stdout_handler = logging.StreamHandler(stdout or sys.stdout)
    stdout_handler.setLevel(logging.INFO)

    stderr_handler = logging.StreamHandler(stderr or sys.stderr)
    stderr_handler.setLevel(logging.ERROR)

    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
        handlers=[stdout_handler, stderr_handler],
        force=True,
    )

    access_log_filter = SensitiveQueryParamFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(access_log_filter)

    for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(logger_name)
        if not any(
            isinstance(filter_, SensitiveQueryParamFilter)
            for filter_ in logger.filters
        ):
            logger.addFilter(access_log_filter)

    logging.getLogger("ib_insync.wrapper").setLevel(logging.WARNING)

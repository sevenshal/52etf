from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import httpx

from src.core.services.szdt import SZDTService


class SZDTServiceTest(IsolatedAsyncioTestCase):
    async def test_make_request_logs_timeout_type_when_exception_message_is_empty(self):
        service = SZDTService()
        service.timeout_seconds = 4.0
        service.timeout = httpx.Timeout(service.timeout_seconds)

        class TimeoutAsyncClient:
            kwargs = []

            def __init__(self, **kwargs):
                self.__class__.kwargs.append(kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def get(self, url, headers=None):
                request = httpx.Request("GET", url)
                raise httpx.ReadTimeout("", request=request)

        with patch("src.core.services.szdt.httpx.AsyncClient", TimeoutAsyncClient), self.assertLogs(
            "SZDTService",
            level="ERROR",
        ) as logs:
            result = await service._make_request("GET", "/invest/stock_emotion?etf_type=1")

        self.assertIsNone(result)
        self.assertIs(TimeoutAsyncClient.kwargs[0]["timeout"], service.timeout)
        log_line = "\n".join(logs.output)
        self.assertIn("请求失败 GET /invest/stock_emotion?etf_type=1", log_line)
        self.assertIn("type=ReadTimeout", log_line)
        self.assertIn("timeout=4s", log_line)
        self.assertNotIn("X-Auth", log_line)
        self.assertNotIn("X-Signature", log_line)

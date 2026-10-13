import asyncio
from datetime import datetime, timedelta
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

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


def _service_with_batch_response(response):
    service = SZDTService.__new__(SZDTService)
    service.get_etf_emotion = AsyncMock(return_value=response)
    return service


def test_fresh_etf_emotions_exposes_one_batch_response_as_code_map():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    service = _service_with_batch_response({
        "status": 1,
        "data": [
            {"code": "SH.512480", "emotion": {"score": -94, "price": "0.888", "updated_at": now}},
            {"code": "SZ.159915", "emotion": {"score": -97, "price": 2.983, "updated_at": now}},
        ],
    })

    result = asyncio.run(service.get_fresh_etf_emotions_from_list(3))

    assert set(result) == {"SH512480", "SZ159915"}
    assert result["SH512480"]["data"]["price"] == 0.888
    service.get_etf_emotion.assert_awaited_once_with(3)


def test_single_etf_lookup_reuses_batch_wrapper_and_ignores_stale_rows():
    stale = (datetime.now() - timedelta(minutes=16)).strftime("%Y-%m-%d %H:%M:%S")
    service = _service_with_batch_response({
        "status": 1,
        "data": [{"code": "SH.512480", "emotion": {"score": -94, "price": "0.888", "updated_at": stale}}],
    })

    result = asyncio.run(service.get_fresh_emotion_from_list(3, "SH.512480"))

    assert result is None
    service.get_etf_emotion.assert_awaited_once_with(3)

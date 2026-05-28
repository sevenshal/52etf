from unittest import TestCase
from unittest.mock import call, patch

import httpx

from src.robot.etf.qqq import QQQDataFetcher


class QQQDataFetcherTest(TestCase):
    def test_headers_do_not_send_cors_origin(self):
        fetcher = QQQDataFetcher()
        headers = {key.lower(): value for key, value in fetcher.headers.items()}

        self.assertNotIn("origin", headers)
        self.assertNotIn("sec-fetch-dest", headers)
        self.assertNotIn("sec-fetch-mode", headers)
        self.assertNotIn("sec-fetch-site", headers)
        self.assertIn("referer", headers)

    def test_proxy_http_status_error_retries_direct(self):
        fetcher = QQQDataFetcher()
        fetcher.PROXY = "socks5://127.0.0.1:7891"

        request = httpx.Request("GET", fetcher.HOLDINGS_URL)
        response = httpx.Response(406, request=request)
        proxy_error = httpx.HTTPStatusError(
            "406 Not Acceptable",
            request=request,
            response=response,
        )
        direct_response = httpx.Response(200, request=request, json={"holdings": []})

        with patch.object(
            fetcher,
            "_fetch_holdings",
            side_effect=[proxy_error, direct_response],
        ) as fetch_mock:
            result = fetcher._get_holdings_response()

        self.assertIs(result, direct_response)
        self.assertEqual(
            [call(proxy=fetcher.PROXY), call(proxy=None)],
            fetch_mock.call_args_list,
        )

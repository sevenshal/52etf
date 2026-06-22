from datetime import date
from unittest import TestCase
from unittest.mock import call, patch

import httpx

from src.robot.etf.qqq import QQQDataFetcher


COMPANIES_MARKET_CAP_HTML = """
<html>
  <body>
    <div>
      <span>Etf holdings as of <span class="background-ya">May 25, 2026</span></span>
      <span>Number of holdings: <span class="background-ya">104</span></span>
    </div>
    <table>
      <thead>
        <tr><th>Weight %</th><th>Name</th><th>Ticker</th><th>Shares Held</th></tr>
      </thead>
      <tbody>
        <tr>
          <td>8.55%<div class="progress-bar" aria-valuenow="8.554098"></div></td>
          <td>NVIDIA Corp</td>
          <td>NVDA</td>
          <td>189224642</td>
        </tr>
        <tr>
          <td>7.41%<div class="progress-bar" aria-valuenow="7.411884"></div></td>
          <td>Apple Inc</td>
          <td>AAPL</td>
          <td>114322359</td>
        </tr>
      </tbody>
    </table>
  </body>
</html>
"""


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

    def test_companies_market_cap_fallback_parses_holdings(self):
        fetcher = QQQDataFetcher()
        fetcher.MIN_FALLBACK_EQUITY_HOLDINGS = 2

        result = fetcher._parse_companies_market_cap_holdings_html(COMPANIES_MARKET_CAP_HTML)

        self.assertEqual(date(2026, 5, 25), result.update_date)
        self.assertEqual(2, len(result.holdings))
        self.assertEqual("NVDA.US", result.holdings[0].symbol)
        self.assertEqual("NVIDIA Corp", result.holdings[0].name)
        self.assertEqual(189224642, result.holdings[0].shares)
        self.assertAlmostEqual(0.08554098, result.holdings[0].weight)
        self.assertAlmostEqual(0.15965982, result.total_weight)

    def test_get_holdings_falls_back_when_invesco_fails(self):
        fetcher = QQQDataFetcher()
        fetcher.MIN_FALLBACK_EQUITY_HOLDINGS = 2

        with patch.object(
            fetcher,
            "_get_holdings_response",
            side_effect=httpx.HTTPError("invesco blocked"),
        ), patch.object(
            fetcher,
            "_fetch_companies_market_cap_holdings_html",
            return_value=COMPANIES_MARKET_CAP_HTML,
        ):
            result = fetcher.get_holdings("QQQ.US")

        self.assertEqual(date(2026, 5, 25), result.update_date)
        self.assertEqual(["NVDA.US", "AAPL.US"], [holding.symbol for holding in result.holdings])

    def test_proxy_type_error_retries_direct(self):
        fetcher = QQQDataFetcher()
        fetcher.PROXY = "socks5://127.0.0.1:7891"

        request = httpx.Request("GET", fetcher.HOLDINGS_URL)
        direct_response = httpx.Response(200, request=request, json={"holdings": []})

        with patch.object(
            fetcher,
            "_fetch_holdings",
            side_effect=[TypeError("unexpected keyword argument 'proxy'"), direct_response],
        ) as fetch_mock:
            result = fetcher._get_holdings_response()

        self.assertIs(result, direct_response)
        self.assertEqual(
            [call(proxy=fetcher.PROXY), call(proxy=None)],
            fetch_mock.call_args_list,
        )

    def test_missing_socks_support_retries_direct(self):
        fetcher = QQQDataFetcher()
        fetcher.PROXY = "socks5://127.0.0.1:7891"

        request = httpx.Request("GET", fetcher.HOLDINGS_URL)
        direct_response = httpx.Response(200, request=request, json={"holdings": []})
        missing_socks_error = RuntimeError(
            "Using SOCKS proxy, but the 'socksio' package is not installed. "
            "Make sure to install httpx using `pip install httpx[socks]`."
        )

        with patch.object(
            fetcher,
            "_fetch_holdings",
            side_effect=[missing_socks_error, direct_response],
        ) as fetch_mock:
            result = fetcher._get_holdings_response()

        self.assertIs(result, direct_response)
        self.assertEqual(
            [call(proxy=fetcher.PROXY), call(proxy=None)],
            fetch_mock.call_args_list,
        )

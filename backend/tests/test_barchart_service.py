from unittest.mock import Mock

import pytest

from src.core.services.barchat import BarchartService


def _response(payload=None, status_code=200):
    response = Mock()
    response.status_code = status_code
    response.headers = {}
    response.url = "https://www.barchart.com/test"
    response.json.return_value = payload or {"data": []}
    return response


def test_get_json_does_not_require_xsrf_cookie(monkeypatch):
    service = BarchartService(request_interval_seconds=0, max_retries=0)
    page_response = _response()
    api_response = _response({"data": [{"date": "2026-08-28"}]})
    request = Mock(side_effect=[page_response, api_response])
    monkeypatch.setattr(service.session, "request", request)

    payload = service.get_options_history_page("SPY", limit=10)

    assert payload["data"] == [{"date": "2026-08-28"}]
    assert "X-XSRF-TOKEN" not in request.call_args_list[1].kwargs["headers"]


def test_get_json_sends_decoded_xsrf_cookie(monkeypatch):
    service = BarchartService(request_interval_seconds=0, max_retries=0)
    page_response = _response()
    api_response = _response()

    def request_side_effect(method, url, **kwargs):
        if "put-call-ratios" in url:
            service.session.cookies.set("XSRF-TOKEN", "token%3Dvalue")
            return page_response
        return api_response

    request = Mock(side_effect=request_side_effect)
    monkeypatch.setattr(service.session, "request", request)

    service.get_options_history_page("SPY", limit=10)

    assert request.call_args_list[1].kwargs["headers"]["X-XSRF-TOKEN"] == "token=value"


@pytest.mark.parametrize("status_code", [500, 502, 503, 504])
def test_get_json_retries_transient_server_errors(monkeypatch, status_code):
    service = BarchartService(
        request_interval_seconds=0,
        max_retries=1,
        retry_base_seconds=0,
        retry_max_seconds=0,
    )
    page_response = _response()
    failed_api_response = _response(status_code=status_code)
    successful_api_response = _response({"data": [{"date": "2026-09-01"}]})
    request = Mock(side_effect=[page_response, failed_api_response, successful_api_response])
    sleep_calls = []
    monkeypatch.setattr(service.session, "request", request)
    monkeypatch.setattr("src.core.services.barchat.time.sleep", sleep_calls.append)

    payload = service.get_options_history_page("VOX", limit=10)

    assert payload["data"] == [{"date": "2026-09-01"}]
    assert request.call_count == 3
    assert sleep_calls == [0.0]

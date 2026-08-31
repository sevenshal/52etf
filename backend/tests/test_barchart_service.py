from unittest.mock import Mock

from src.core.services.barchat import BarchartService


def _response(payload=None, status_code=200):
    response = Mock()
    response.status_code = status_code
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

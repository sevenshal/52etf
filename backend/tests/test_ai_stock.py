from datetime import datetime
import logging
from unittest import mock

import pandas as pd

from src.core.services.ai_stock import (
    AIStockError,
    AIStockDataProvider,
    AIStockRecommendationService,
    AIStockPaperTradingService,
    DeepSeekStockSelector,
    _buy_fee,
    get_ai_stock_service_settings,
    _scheduled_recommendation_type,
    _sell_fee,
    _validated_board_mappings,
    _validated_events,
    _validated_hotwords,
    update_ai_stock_service_settings,
    _validated_picks,
)
from src.core.services.tushare import _SlidingWindowRateLimiter, TushareRateLimitError, TushareService
from src.core.services.tushare_account import (
    get_tushare_account_settings,
    get_tushare_token_for_runtime,
    update_tushare_account_settings,
)


def test_news_snapshot_uses_major_news_only():
    class _MajorNewsOnlyTushare:
        def get_a_stock_major_news_frame(self, _start_at, _end_at):
            return pd.DataFrame(
                [
                    {
                        "title": "通讯标题",
                        "pub_time": "2026-08-10 10:00:00",
                        "src": "财联社",
                    }
                ]
            )

    snapshot = AIStockDataProvider(tushare=_MajorNewsOnlyTushare()).build_news_snapshot(
        datetime(2026, 8, 10, 12, 0)
    )

    assert snapshot["headlines"][0]["kind"] == "major_news"
    assert set(snapshot["source_status"]) == {"major_news", "headline_count"}


def test_tushare_account_token_is_write_only_and_used_by_runtime():
    # --- save original to restore after test ---
    from src.core.database import get_db_ctx, TushareAccountConfig

    with get_db_ctx() as db:
        original = db.get(TushareAccountConfig, 1)
        orig_token = original.api_token if original else None

    try:
        saved = update_tushare_account_settings(
            api_token="tushare-page-token-for-test",
            updated_by="admin",
        )

        assert saved["configured"] is True
        assert saved["source"] == "PAGE"
        assert saved["token_hint"] == "••••test"
        assert "api_token" not in saved
        assert get_tushare_token_for_runtime() == "tushare-page-token-for-test"
        assert get_tushare_account_settings()["token_hint"] == "••••test"
    finally:
        # restore original token so the test never pollutes the real runtime
        with get_db_ctx() as db:
            config = db.get(TushareAccountConfig, 1)
            if config:
                config.api_token = orig_token
                db.commit()


def test_ai_output_is_limited_to_eligible_candidate_pool():
    snapshot = {
        "candidates": [
            {"ts_code": "600000.SH", "name": "浦发银行", "price": 10.0, "themes": ["金融"], "change_pct": 1, "listing_days": 365},
            {"ts_code": "000001.SZ", "name": "ST 平安", "price": 11.0, "themes": ["金融"], "listing_days": 365},
            {"ts_code": "600001.SH", "name": "过期行情", "price": 12.0, "data_fresh": False, "listing_days": 365},
            {"ts_code": "600002.SH", "name": "次新股", "price": 13.0, "listing_days": 120},
        ]
    }
    raw_response = {
        "picks": [
            {"ts_code": "999999.SH", "confidence": 99, "target_return_pct": 8, "reason": "编造代码"},
            {"ts_code": "000001.SZ", "confidence": 98, "target_return_pct": 8, "reason": "ST 股票"},
            {"ts_code": "600001.SH", "confidence": 95, "target_return_pct": 8, "reason": "过期行情"},
            {"ts_code": "600002.SH", "confidence": 94, "target_return_pct": 8, "reason": "次新股"},
            {"ts_code": "600000", "confidence": 82, "target_return_pct": 12, "reason": "候选池内"},
            {"ts_code": "600000.SH", "confidence": 90, "target_return_pct": 6, "reason": "重复代码"},
        ]
    }

    picks = _validated_picks(snapshot, raw_response, top_n=10)

    assert len(picks) == 1
    assert picks[0]["ts_code"] == "600000.SH"
    assert picks[0]["target_return_pct"] == 10.0
    assert picks[0]["target_price"] == 11.0


def test_ai_output_target_return_uses_configured_range_instead_of_db_globals():
    # DB settings may be changed by the operator (e.g. max_recommendations=15,
    # wider target range); validation must follow the configured values, and
    # tests must not depend on whatever is currently stored.
    snapshot = {"candidates": [{"ts_code": "600000.SH", "name": "浦发银行", "price": 10.0, "themes": ["金融"], "change_pct": 1, "listing_days": 365}]}
    raw_response = {"picks": [{"ts_code": "600000.SH", "confidence": 90, "target_return_pct": 50, "reason": "高收益"}]}
    with mock.patch("src.core.services.ai_stock._load_strategy_params", return_value={
        "max_recommendations": 15, "min_listing_days": 183,
        "target_return_pct_min": 5.0, "target_return_pct_max": 10.0,
    }):
        picks = _validated_picks(snapshot, raw_response, top_n=15)
    assert len(picks) == 1
    assert picks[0]["target_return_pct"] == 10.0, "50% must clamp to configured max 10"
    assert picks[0]["target_price"] == 11.0


def test_a_share_costs_include_minimum_commission_transfer_and_stamp_duty():
    assert _buy_fee(10_000) == 5.1
    assert _sell_fee(10_000) == 10.1


def test_recommendation_schedule_has_preopen_opening_and_half_hour_intraday_batches():
    assert _scheduled_recommendation_type(datetime(2026, 8, 10, 9, 26)) == "PREOPEN"
    assert _scheduled_recommendation_type(datetime(2026, 8, 10, 9, 35)) == "OPENING"
    assert _scheduled_recommendation_type(datetime(2026, 8, 10, 10, 30)) == "INTRADAY"
    assert _scheduled_recommendation_type(datetime(2026, 8, 10, 10, 15)) is None


def test_manual_override_is_the_only_after_hours_recommendation_path():
    service = AIStockRecommendationService(provider=object())
    try:
        service.run_recommendation(now=datetime(2026, 8, 10, 16, 0))
    except AIStockError as exc:
        assert "收盘后" in str(exc)
    else:
        raise AssertionError("自动路径不应在盘后生成推荐")


def test_web_settings_are_redacted_after_the_write_only_key_is_saved():
    from src.core.database import get_db_ctx, AIStockServiceConfig

    with get_db_ctx() as db:
        original = db.get(AIStockServiceConfig, 1)
        orig_key = original.deepseek_api_key if original else None

    try:
        saved = update_ai_stock_service_settings(
            deepseek_api_key="temporary-test-key",
            deepseek_model="deepseek-chat",
            updated_by="admin",
        )

        assert saved["deepseek_configured"] is True
        assert "deepseek_api_key" not in saved
        assert get_ai_stock_service_settings()["deepseek_configured"] is True
    finally:
        with get_db_ctx() as db:
            config = db.get(AIStockServiceConfig, 1)
            if config:
                config.deepseek_api_key = orig_key
                db.commit()


def test_short_news_splits_a_saturated_time_window_without_offset_pagination():
    service = object.__new__(TushareService)
    service.logger = logging.getLogger("test-tushare-news")
    calls = []

    class _Pro:
        def news(self, **kwargs):
            calls.append(kwargs)
            start = datetime.strptime(kwargs["start_date"], "%Y-%m-%d %H:%M:%S")
            end = datetime.strptime(kwargs["end_date"], "%Y-%m-%d %H:%M:%S")
            if (end - start).total_seconds() > 60:
                return pd.DataFrame([{"datetime": "2026-08-10 10:00:00", "title": f"饱和{i}"} for i in range(1500)])
            return pd.DataFrame([{"datetime": start, "title": f"窗口-{start:%H%M%S}"}])

    service.pro = _Pro()
    frame = service.get_a_stock_news_frame(datetime(2026, 8, 10, 9, 0), datetime(2026, 8, 10, 9, 2), sources=["cls"])

    assert len(calls) > 1
    assert "offset" not in calls[0]
    assert len(frame) == 2


def test_major_news_rate_limit_fails_immediately_without_sleeping():
    service = object.__new__(TushareService)
    service.logger = logging.getLogger("test-tushare-major-news")
    service._major_news_rate_limiter = _SlidingWindowRateLimiter(1, 3600)

    class _Pro:
        def __init__(self):
            self.calls = 0

        def major_news(self, **_kwargs):
            self.calls += 1
            return pd.DataFrame(
                [{"title": "通讯", "pub_time": "2026-08-10 10:00:00", "src": "财联社"}]
            )

    service.pro = _Pro()
    service.get_a_stock_major_news_frame(
        datetime(2026, 8, 10, 9, 0),
        datetime(2026, 8, 10, 10, 0),
        sources=["财联社"],
    )

    try:
        service.get_a_stock_major_news_frame(
            datetime(2026, 8, 10, 10, 0),
            datetime(2026, 8, 10, 11, 0),
            sources=["财联社"],
        )
    except TushareRateLimitError as exc:
        assert "本地小时级限流" in str(exc)
    else:
        raise AssertionError("达到 major_news 限流后必须立即失败")
    assert service.pro.calls == 1


def test_events_ths_mapping_and_stock_selection_keep_one_three_round_conversation(monkeypatch):
    headlines = [
        {"headline_id": "N0001", "time": "2026-08-10 10:00:00", "source": "cls", "title": "国产算力项目加速落地"},
        {"headline_id": "N0002", "time": "2026-08-10 11:00:00", "source": "eastmoney", "title": "人工智能基础设施投资升温"},
    ]
    events = _validated_events(
        {"events": [{"hotword": "算力", "score": 90, "headline_ids": ["N0001"], "aliases": ["人工智能"]}]},
        headlines,
    )
    assert events[0]["event_id"] == "E01"
    assert not _validated_events(
        {"events": [{"hotword": "编造热点", "score": 90, "headline_ids": ["N9999"]}]},
        headlines,
    )
    catalog = {"items": [{"ts_code": "885728.TI", "name": "人工智能", "type": "N", "count": 20}]}
    assert _validated_board_mappings(
        {"board_mappings": [{"event_id": "E01", "boards": [{"ths_code": "885728.TI", "relevance": 90, "reason": "直接受益"}]}]}, events, catalog["items"],
    )[0]["ths_code"] == "885728.TI"

    payloads = [
        {"id": "c1", "usage": {"total_tokens": 1}, "choices": [{"message": {"content": '{"events":[{"hotword":"算力","score":90,"headline_ids":["N0001"],"aliases":["人工智能"],"rationale":"新闻催化"}]}'}}]},
        {"id": "c2", "usage": {"total_tokens": 2}, "choices": [{"message": {"content": '{"board_mappings":[{"event_id":"E01","boards":[{"ths_code":"885728.TI","relevance":90,"reason":"直接相关"}]}]}'}}]},
        {"id": "c3", "usage": {"total_tokens": 3}, "choices": [{"message": {"content": '{"picks":[{"ts_code":"600000.SH","confidence":80,"target_return_pct":8,"reason":"新闻事件→人工智能→候选股","risks":"波动","themes":["人工智能"],"evidence":[{"event_id":"E01","headline_id":"N0001","ths_code":"885728.TI"}]}]}'}}]},
    ]
    calls = []

    class _Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_post(*_args, **kwargs):
        calls.append(kwargs["json"])
        return _Response(payloads.pop(0))

    monkeypatch.setattr("src.core.services.ai_stock.requests.post", fake_post)
    selector = DeepSeekStockSelector(api_key="test-key", model="deepseek-chat")
    news_snapshot = {"headlines": headlines}
    event_stage = selector.extract_events(news_snapshot)
    board_stage = selector.map_events_to_ths(event_stage, catalog)
    snapshot = {
        "candidates": [{"ts_code": "600000.SH", "name": "测试股", "price": 10.0, "listing_days": 365, "event_ids": ["E01"], "board_codes": ["885728.TI"], "themes": ["人工智能"]}],
        "boards": [{"ths_code": "885728.TI", "name": "人工智能"}],
    }
    selection = selector.select_from_ths_conversation(event_stage, board_stage, snapshot)

    # Each later request replays prior messages and assistant output.
    assert calls[1]["messages"][:2] == calls[0]["messages"]
    assert calls[2]["messages"][:4] == calls[1]["messages"]
    picks = _validated_picks(snapshot, selection["response"], 10, news_headlines=headlines, hotwords=event_stage["events"], board_mappings=board_stage["board_mappings"])
    assert picks[0]["evidence"][0]["ths_code"] == "885728.TI"


class _TranscriptProvider:
    def build_news_snapshot(self, _now):
        return {"headlines": [{"headline_id": "N0001", "time": "2026-08-10 10:00:00", "source": "cls", "title": "原始新闻标题必须存档"}]}

    def ths_index_catalog(self):
        return {"items": [{"ts_code": "885728.TI", "name": "人工智能", "type": "N", "count": 1}], "cached": False}

    def build_candidate_snapshot(self, _now, _events, _mappings, _catalog):
        return {
            "boards": [{"ths_code": "885728.TI", "name": "人工智能"}],
            "candidates": [{"ts_code": "600000.SH", "name": "测试股", "price": 10.0, "listing_days": 365, "event_ids": ["E01"], "board_codes": ["885728.TI"], "themes": ["人工智能"]}],
        }


class _TranscriptSelector:
    def extract_events(self, _news_snapshot):
        return {
            "model": "fake-deepseek",
            "events": [{"event_id": "E01", "hotword": "算力", "score": 90, "headline_ids": ["N0001"], "aliases": ["人工智能"], "rationale": "测试"}],
            "transcript": {"stage": "NEWS_EVENTS", "request": {"messages": [{"role": "user", "content": "原始新闻标题必须存档"}]}, "response_content": "{事件回复}", "response_json": {}},
        }

    def map_events_to_ths(self, _event_stage, _catalog):
        return {
            "model": "fake-deepseek",
            "board_mappings": [{"event_id": "E01", "ths_code": "885728.TI", "relevance": 90, "reason": "测试"}],
            "transcript": {"stage": "EVENTS_TO_THS_BOARDS", "request": {"messages": [{"role": "user", "content": "THS目录"}]}, "response_content": "{板块回复}", "response_json": {}},
        }

    def select_from_ths_conversation(self, _event_stage, _board_stage, _snapshot, top_n):
        return {
            "model": "fake-deepseek",
            "response": {"picks": [{"ts_code": "600000.SH", "confidence": 80, "target_return_pct": 8, "reason": "新闻到股票", "risks": "波动", "evidence": [{"event_id": "E01", "headline_id": "N0001", "ths_code": "885728.TI"}]}]},
            "transcript": {"stage": "THS_BOARDS_TO_STOCK_SELECTION", "request": {"messages": [{"role": "user", "content": "题材和成分股"}]}, "response_content": "{选股回复}", "response_json": {}},
        }


def test_run_persists_full_news_to_stock_transcript():
    service = AIStockRecommendationService(provider=_TranscriptProvider(), selector=_TranscriptSelector())
    run = service.run_recommendation(now=datetime(2026, 8, 10, 16, 0), allow_after_hours=True)

    # The heavy payloads are split out of get_run into on-demand endpoints.
    assert run["news_count"] == 1
    evidence = service.get_run_evidence(run["id"])
    transcript = service.get_run_transcript(run["id"])
    assert evidence["news_snapshot"][0]["title"] == "原始新闻标题必须存档"
    assert transcript["ai_raw_response"]["conversation_version"] == "news-ths-v3"
    assert [stage["stage"] for stage in transcript["ai_raw_response"]["stages"]] == ["NEWS_EVENTS", "EVENTS_TO_THS_BOARDS", "THS_BOARDS_TO_STOCK_SELECTION"]
    assert transcript["ai_raw_response"]["stages"][0]["request"]["messages"][0]["content"] == "原始新闻标题必须存档"


class _StaticQuoteProvider:
    def __init__(self, price):
        self.price = price

    def quotes(self, symbols):
        return {symbol: {"price": self.price} for symbol in symbols}

    def minute_entry_confirmed(self, _symbol):
        return True, {"confirmed": True}


class _CapturingPaperService(AIStockPaperTradingService):
    def __init__(self, state, price):
        super().__init__(provider=_StaticQuoteProvider(price))
        self.state = state
        self.captured_plans = None

    def _snapshot(self, _snapshot_date):
        return self.state

    def _commit_minute(self, _timestamp, _minute_key, _fear_greed, _execution_target, _quotes, plans):
        self.captured_plans = plans
        return {"processed": True, "trades": []}


def _paper_state(lot):
    return {
        "portfolio": {"id": 1, "enabled": True, "cash": 100_000, "last_processed_minute": None, "last_execution_target": None},
        "lots": [lot],
        "recommendations": [],
        "today_buys": set(),
    }


def test_paper_stop_loss_halves_once_and_t_plus_one_blocks_same_day_sell():
    prior_day_lot = {
        "id": 1, "recommendation_id": 1, "ts_code": "600000.SH", "name": "测试股",
        "bought_at": datetime(2026, 8, 10, 10, 0), "buy_price": 100.0,
        "remaining_quantity": 1000, "target_price": 108.0, "stop_half_triggered": False,
    }
    service = _CapturingPaperService(_paper_state(prior_day_lot), price=91.0)
    service.process_minute(now=datetime(2026, 8, 11, 10, 0), fear_greed=50)

    assert [(plan.side, plan.quantity, plan.reason_code) for plan in service.captured_plans] == [
        ("SELL", 500, "STOP_LOSS_HALF")
    ]

    same_day_lot = dict(prior_day_lot, bought_at=datetime(2026, 8, 11, 9, 40))
    same_day_service = _CapturingPaperService(_paper_state(same_day_lot), price=110.0)
    same_day_service.process_minute(now=datetime(2026, 8, 11, 10, 0), fear_greed=50)

    assert same_day_service.captured_plans == []


def test_paper_strategy_config_update_model_keeps_hold_evaluation_fields():
    # 回归：路由模型曾经缺 trading_start_minute / hold_evaluation_enabled /
    # hold_sell_threshold，Pydantic 默认忽略未知字段导致前端保存被静默丢弃。
    from src.app.api.ai_stock import PaperStrategyConfigUpdate

    dumped = PaperStrategyConfigUpdate(
        trading_start_minute=585,
        hold_evaluation_enabled=True,
        hold_sell_threshold=30,
    ).model_dump(exclude_none=True)

    assert dumped["trading_start_minute"] == 585
    assert dumped["hold_evaluation_enabled"] is True
    assert dumped["hold_sell_threshold"] == 30

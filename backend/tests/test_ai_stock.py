from datetime import datetime
import json
import logging
from unittest import mock

import pandas as pd

from src.core.services.ai_stock import (
    AIStockError,
    AIStockDataProvider,
    AIStockRecommendationService,
    AIStockPaperTradingService,
    DeepSeekStockSelector,
    _aggregate_hit_metrics,
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
                        "datetime": "2026-08-10 10:00:00",
                        "source": "财联社",
                    }
                ]
            )

    snapshot = AIStockDataProvider(tushare=_MajorNewsOnlyTushare()).build_news_snapshot(
        datetime(2026, 8, 10, 12, 0)
    )

    assert snapshot["headlines"][0]["title"] == "通讯标题"
    assert snapshot["headlines"][0]["headline_id"] == "N0001"
    assert "kind" not in snapshot["headlines"][0]  # 常量字段不再发给模型
    assert set(snapshot["source_status"]) == {"major_news", "headline_count", "headline_raw_count"}
    assert snapshot["source_status"]["headline_count"] == 1
    assert snapshot["source_status"]["headline_raw_count"] == 1


def test_news_headlines_dedupe_exact_titles_and_merge_sources():
    from src.core.services.ai_stock import _serialize_news_headlines

    frame = pd.DataFrame(
        [
            {"title": "存储芯片景气度持续提升", "datetime": "2026-08-10 09:00:00", "source": "财联社"},
            {"title": "存储芯片景气度持续提升", "datetime": "2026-08-10 10:30:00", "source": "新浪财经"},
            {"title": "存储芯片景气度持续提升", "datetime": "2026-08-10 11:00:00", "source": "财联社"},
            {"title": "人工智能基础设施投资升温", "datetime": "2026-08-10 09:30:00", "source": "东方财富"},
            {"title": "  人工智能基础设施投资升温  ", "datetime": "2026-08-10 09:31:00", "source": "界面新闻"},
            {"title": "", "datetime": "2026-08-10 12:00:00", "source": "财联社"},
        ]
    )
    rows = _serialize_news_headlines(("major_news", frame))

    # 空标题丢弃；相同标题（含首尾空格）只留一条，time 用最早，source 用 | 合并去重
    assert len(rows) == 2
    by_title = {row["title"]: row for row in rows}
    chip = by_title["存储芯片景气度持续提升"]
    assert chip["time"] == "2026-08-10 09:00:00"
    assert set(chip["source"].split("|")) == {"财联社", "新浪财经"}
    ai = by_title["人工智能基础设施投资升温"]
    assert set(ai["source"].split("|")) == {"东方财富", "界面新闻"}
    assert ai["time"] == "2026-08-10 09:30:00"
    # 无 kind 字段，headline_id 连续编号
    assert all("kind" not in row for row in rows)
    assert [row["headline_id"] for row in rows] == ["N0001", "N0002"]


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
    assert _scheduled_recommendation_type(datetime(2026, 8, 10, 9, 40)) == "OPENING"
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


def test_xueqiu_behavior_block_attached_with_clip_and_new_entry():
    import src.core.services.ai_stock as ai_stock_mod

    fake_map = {
        "600000.SH": {  # existing holder: full 5-day change fields
            "composite_weight_pct": 3.92,
            "composite_rank": 45,
            "holding_cube_count": 5,
            "weight_multiple_5d": 1.4,
            "momentum_multiple_5d": 1.05,
            "rank_change_5d": 23,
            "cube_count_5d_ago": 3,
            "weight_price_ratio_5d": 1.33,
            "direction": "顺势加仓",
        },
        "600001.SH": {  # new entry: 5d-ago fields None, but price momentum still real
            "composite_weight_pct": 1.0,
            "composite_rank": 300,
            "holding_cube_count": 12,
            "weight_multiple_5d": None,
            "momentum_multiple_5d": 1.02,
            "rank_change_5d": None,
            "cube_count_5d_ago": None,
            "weight_price_ratio_5d": None,
            "direction": "新进",
        },
        "600002.SH": {  # extreme multiples must be clipped
            "composite_weight_pct": 20.0,
            "composite_rank": 10,
            "holding_cube_count": 20,
            "weight_multiple_5d": 161.9,
            "momentum_multiple_5d": 1.2,
            "rank_change_5d": -44,
            "cube_count_5d_ago": 20,
            "weight_price_ratio_5d": 153.5,
            "direction": "顺势加仓",
        },
    }
    monkeypatch = mock.Mock()
    monkeypatch.setattr = lambda *args, **kwargs: None
    import src.core.services.ai_stock as ai_stock_mod

    original = ai_stock_mod._load_xueqiu_behavior_map
    ai_stock_mod._load_xueqiu_behavior_map = lambda limit=2000: fake_map
    try:
        candidates = [
            {"ts_code": "600000.SH"},
            {"ts_code": "600001.SH"},
            {"ts_code": "600002.SH"},
            {"ts_code": "600003.SH"},
        ]
        from src.core.services.ai_stock import _attach_xueqiu_behavior

        _attach_xueqiu_behavior(candidates)
    finally:
        ai_stock_mod._load_xueqiu_behavior_map = original

    # not in the current snapshot -> no block at all
    assert "xueqiu" not in candidates[3]

    xq = candidates[0]["xueqiu"]
    assert xq["weight"] == 3.92
    assert xq["rank"] == 45
    assert xq["cube_count"] == 5
    assert xq["weight_gain_5d"] == 1.4
    assert xq["price_gain_5d"] == 1.05
    assert xq["rank_up_5d"] == 23
    assert xq["cube_gain_5d"] == 2
    assert xq["ratio_5d"] == 1.33
    assert xq["direction"] == "顺势加仓"

    xq_new = candidates[1]["xueqiu"]
    assert xq_new["weight_gain_5d"] is None
    assert xq_new["rank_up_5d"] is None
    assert xq_new["cube_gain_5d"] is None
    assert xq_new["ratio_5d"] is None
    assert xq_new["price_gain_5d"] == 1.02  # price momentum is real for new entries too
    assert xq_new["cube_count"] == 12
    assert xq_new["direction"] == "新进"

    xq_extreme = candidates[2]["xueqiu"]
    assert xq_extreme["weight_gain_5d"] == 10.0  # clipped
    assert xq_extreme["ratio_5d"] == 10.0  # clipped
    assert xq_extreme["rank_up_5d"] == -44  # negative = rank dropped
    assert xq_extreme["cube_gain_5d"] == 0
    assert xq_extreme["direction"] == "顺势加仓"


def test_xueqiu_guidance_only_present_when_toggle_enabled():
    from src.core.database import get_db_ctx, AIStockServiceConfig

    with get_db_ctx() as db:
        config = db.get(AIStockServiceConfig, 1)
        orig = config.xueqiu_signal_enabled if config else None

    headlines = [
        {"headline_id": "N0001", "time": "2026-08-10 10:00:00", "source": "cls", "title": "国产算力项目加速落地"},
    ]
    events = _validated_events(
        {"events": [{"hotword": "算力", "score": 90, "headline_ids": ["N0001"], "aliases": ["人工智能"]}]},
        headlines,
    )
    catalog = {"items": [{"ts_code": "885728.TI", "name": "人工智能", "type": "N", "count": 20}]}

    payloads = []
    calls = []

    class _Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def _payload_triple():
        return [
            {"id": "c1", "usage": {"total_tokens": 1}, "choices": [{"message": {"content": '{"events":[{"hotword":"算力","score":90,"headline_ids":["N0001"],"aliases":["人工智能"]}]}'}}]},
            {"id": "c2", "usage": {"total_tokens": 2}, "choices": [{"message": {"content": '{"board_mappings":[{"event_id":"E01","boards":[{"ths_code":"885728.TI","relevance":90,"reason":"直接相关"}]}]}'}}]},
            {"id": "c3", "usage": {"total_tokens": 3}, "choices": [{"message": {"content": '{"picks":[{"ts_code":"600000.SH","confidence":80,"target_return_pct":8,"reason":"测试","evidence":[{"event_id":"E01","headline_id":"N0001","ths_code":"885728.TI"}]}]}'}}]},
        ]

    payloads[:] = _payload_triple()

    def fake_post(*_args, **kwargs):
        calls.append(kwargs["json"])
        return _Response(payloads.pop(0))

    import src.core.services.ai_stock as ai_stock_mod

    original_post = ai_stock_mod.requests.post
    ai_stock_mod.requests.post = fake_post
    snapshot = {
        "candidates": [{"ts_code": "600000.SH", "name": "测试股", "price": 10.0, "listing_days": 365, "event_ids": ["E01"], "board_codes": ["885728.TI"], "themes": ["人工智能"], "xueqiu": {"weight": 3.92, "rank": 45}}],
        "boards": [{"ths_code": "885728.TI", "name": "人工智能"}],
    }
    try:
        selector = DeepSeekStockSelector(api_key="test-key", model="deepseek-chat")
        event_stage = selector.extract_events({"headlines": headlines})
        board_stage = selector.map_events_to_ths(event_stage, catalog)

        # toggle OFF (default): no xueqiu key in candidates, no guidance
        calls.clear()
        selector.select_from_ths_conversation(event_stage, board_stage, snapshot)
        last_instruction = json.loads(calls[-1]["messages"][-1]["content"])
        assert "xueqiu_guidance" not in last_instruction
        assert last_instruction["candidates"][0].get("xueqiu") is None

        # toggle ON: xueqiu key + guidance present
        update_ai_stock_service_settings(deepseek_api_key=None, deepseek_model=None, xueqiu_signal_enabled=1, updated_by="admin")
        payloads[:] = _payload_triple()[2:]
        calls.clear()
        selector.select_from_ths_conversation(event_stage, board_stage, snapshot)
        last_instruction = json.loads(calls[-1]["messages"][-1]["content"])
        assert "xueqiu_guidance" in last_instruction
        assert last_instruction["candidates"][0]["xueqiu"]["weight"] == 3.92
        assert last_instruction["candidates"][0]["xueqiu"]["rank"] == 45
        assert "逆势吸筹" in last_instruction["xueqiu_guidance"]
        assert "1.15" in last_instruction["xueqiu_guidance"]
    finally:
        ai_stock_mod.requests.post = original_post
        with get_db_ctx() as db:
            config = db.get(AIStockServiceConfig, 1)
            if config:
                config.xueqiu_signal_enabled = orig
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

    def select_from_ths_conversation(self, _event_stage, _board_stage, _snapshot, top_n, fear_greed_ref=None):
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
    assert transcript["ai_raw_response"]["conversation_version"] == "news-ths-v5"
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


def _recommendation(ts_code, rank=1, confidence=80.0, rec_price=10.0):
    return {
        "id": rank,
        "ts_code": ts_code,
        "name": "测试股",
        "rank": rank,
        "recommendation_price": rec_price,
        "target_price": rec_price * 1.1,
        "target_return_pct": 10.0,
        "ai_confidence": confidence,
        "execution_score": 60,
        "news_signal": 50,
        "themes": [],
        "reason": "",
        "risks": "",
    }


def test_paper_max_buys_per_day_caps_entries():
    state = {
        "portfolio": {"id": 1, "enabled": True, "cash": 100_000, "last_processed_minute": None, "last_execution_target": None, "strategy_params": {"max_buys_per_day": 1, "slot_count": 5, "max_positions": 5, "max_execution_target": 0.9, "entry_price_cap_pct": 5.0, "single_stock_cap": 0.2}},
        "lots": [],
        "recommendations": [_recommendation("600000.SH", 1), _recommendation("000001.SZ", 2)],
        "today_buys": set(),
        "today_buy_count": 0,
        "hold_scores": {},
    }
    service = _CapturingPaperService(state, price=10.0)
    service.process_minute(now=datetime(2026, 8, 11, 10, 0), fear_greed=50)
    buys = [plan for plan in service.captured_plans if plan.side == "BUY"]
    assert len(buys) == 1
    assert buys[0].ts_code == "600000.SH"


def test_paper_trailing_take_profit_sells_on_ma10_break(monkeypatch):
    # 移动止盈（参考系统那种）：目标价已触及 + 现价跌破 MA10 + 当日未获再推荐
    from src.core.services import ai_stock as ai_stock_module

    lot = {
        "id": 1, "recommendation_id": 1, "ts_code": "600000.SH", "name": "测试股",
        "bought_at": datetime(2026, 8, 10, 10, 0), "buy_price": 100.0,
        "remaining_quantity": 1000, "target_price": 120.0, "stop_half_triggered": False,
        "peak_price": 121.0,  # 曾触及目标价
    }
    state = _paper_state(lot)
    state["last_recommended_dates"] = {"600000.SH": datetime(2026, 8, 10).date()}  # 昨日推荐，今日未再推荐
    monkeypatch.setattr(ai_stock_module, "_ma10_by_symbol", lambda codes, as_of: {"600000.SH": 118.0})
    service = _CapturingPaperService(state, price=113.0)  # 现价 113 < MA10 118
    service.process_minute(now=datetime(2026, 8, 11, 10, 0), fear_greed=50)
    assert [(p.side, p.quantity, p.reason_code) for p in service.captured_plans] == [("SELL", 1000, "TRAILING_STOP")]


def test_paper_rotation_sells_low_score_and_buys_high_confidence():
    held_lot = {
        "id": 1, "recommendation_id": 1, "ts_code": "600000.SH", "name": "旧持仓",
        "bought_at": datetime(2026, 8, 10, 10, 0), "buy_price": 10.0,
        "remaining_quantity": 1000, "target_price": 11.0, "stop_half_triggered": False,
        "peak_price": 10.0,
    }
    state = {
        "portfolio": {"id": 1, "enabled": True, "cash": 1000, "last_processed_minute": None, "last_execution_target": None, "strategy_params": {"max_positions": 1, "slot_count": 5, "single_stock_cap": 0.5, "max_execution_target": 0.9, "entry_price_cap_pct": 5.0, "hold_evaluation_enabled": True, "rotation_confidence_gap": 20.0}},
        "lots": [held_lot],
        "recommendations": [_recommendation("000001.SZ", 1, confidence=90.0, rec_price=10.0)],
        "today_buys": set(),
        "today_buy_count": 0,
        "hold_scores": {"600000.SH": {"hold_score": 40.0, "reason": "走弱"}},
        "last_recommended_dates": {"600000.SH": datetime(2026, 8, 11, 9, 0).date()},
    }
    service = _CapturingPaperService(state, price=10.0)
    service.process_minute(now=datetime(2026, 8, 11, 10, 0), fear_greed=50)
    sells = [p for p in service.captured_plans if p.side == "SELL"]
    buys = [p for p in service.captured_plans if p.side == "BUY"]
    assert [(s.reason_code, s.quantity) for s in sells] == [("ROTATION_OUT", 1000)]
    assert [b.ts_code for b in buys] == ["000001.SZ"]


def test_paper_rotation_sells_stale_position_without_hold_score():
    # 信号1：持仓多日未获再推荐（无需 hold_score/持仓评估）→ 轮换让位
    held_lot = {
        "id": 1, "recommendation_id": 1, "ts_code": "600000.SH", "name": "旧持仓",
        "bought_at": datetime(2026, 8, 10, 10, 0), "buy_price": 10.0,
        "remaining_quantity": 1000, "target_price": 11.0, "stop_half_triggered": False,
        "peak_price": 10.0,
    }
    state = {
        "portfolio": {"id": 1, "enabled": True, "cash": 1000, "last_processed_minute": None, "last_execution_target": None, "strategy_params": {"max_positions": 1, "slot_count": 5, "single_stock_cap": 0.5, "max_execution_target": 0.9, "entry_price_cap_pct": 5.0, "rotation_stale_days": 3}},
        "lots": [held_lot],
        "recommendations": [_recommendation("000001.SZ", 1, confidence=70.0, rec_price=10.0)],
        "today_buys": set(),
        "today_buy_count": 0,
        "hold_scores": {},
        "last_recommended_dates": {"600000.SH": datetime(2026, 7, 20).date()},
    }
    service = _CapturingPaperService(state, price=10.0)
    service.process_minute(now=datetime(2026, 8, 11, 10, 0), fear_greed=50)
    sells = [p for p in service.captured_plans if p.side == "SELL"]
    buys = [p for p in service.captured_plans if p.side == "BUY"]
    assert [(s.reason_code, s.quantity) for s in sells] == [("ROTATION_OUT", 1000)]
    assert "未获再推荐" in sells[0].reason
    assert [b.ts_code for b in buys] == ["000001.SZ"]


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


def test_hold_evaluation_receives_captured_run_id_after_session_close(monkeypatch):
    # 回归：run_recommendation 在第 4 步（AI 持仓评估）前已关闭写 session，
    # 曾用脱离的 run.id 触发 DetachedInstanceError 导致持仓评估被跳过，
    # /paper/hold-evaluations 永远为空。这里断言第 4 步拿到的是整型 run_id。
    from src.core.services import ai_stock as ai_stock_module

    service = AIStockRecommendationService(provider=_TranscriptProvider(), selector=_TranscriptSelector())

    class _StubPaperService:
        def strategy_config(self):
            return {"enabled": True, "parameters": {"hold_evaluation_enabled": True}}

    # 替换掉会初始化 tushare provider 的真实 paper 服务，同时让开关生效。
    monkeypatch.setattr(ai_stock_module, "AIStockPaperTradingService", _StubPaperService)

    calls = []

    def fake_evaluate_paper_holdings(**kwargs):
        calls.append(kwargs)
        return {"evaluated": True, "count": 1}

    monkeypatch.setattr(ai_stock_module, "evaluate_paper_holdings", fake_evaluate_paper_holdings)

    run = service.run_recommendation(now=datetime(2026, 8, 10, 16, 0), allow_after_hours=True)

    assert [item["run_id"] for item in calls] == [run["id"]]


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


def test_fear_greed_reference_returns_list_without_crash():
    # 恐慌贪婪参考在无数据时应安全返回空列表，不抛异常
    from src.core.services.ai_stock import _a_stock_fear_greed_reference

    ref = _a_stock_fear_greed_reference()
    assert isinstance(ref, list)


def test_classify_theme_stage():
    from src.core.services.ai_stock import _classify_theme_stage

    assert _classify_theme_stage(80.0, 5.0) == "拥挤"
    assert _classify_theme_stage(30.0, 8.0) == "启动"     # 恐慌 + 回升
    assert _classify_theme_stage(30.0, -8.0) == "探底"    # 恐慌 + 继续走弱（不是退潮）
    assert _classify_theme_stage(60.0, 10.0) == "主线"    # 中高位 + 上行
    assert _classify_theme_stage(40.0, 10.0) == "扩散"    # 中位 + 上行
    assert _classify_theme_stage(60.0, -10.0) == "退潮"   # 从非恐慌区回落
    assert _classify_theme_stage(40.0, -10.0) == "退潮"   # 中位明显回落
    assert _classify_theme_stage(50.0, 2.0) == "中性"     # 无明确方向
    assert _classify_theme_stage(None, 0.0) is None


def test_aggregate_hit_metrics_counts_hits_and_average_returns():
    # 4 条推荐：当日/次日/触目标/一周分别命中一部分，验证命中率与等额平均收益
    records = [
        {"entry_price": 10.0, "target_price": 11.0, "same_day_close": 10.5, "next_day_close": 10.8, "window_high": 11.2, "week_close": 10.6},
        {"entry_price": 10.0, "target_price": 11.0, "same_day_close": 9.5, "next_day_close": 9.8, "window_high": 10.5, "week_close": 9.9},
        {"entry_price": 10.0, "target_price": 11.0, "same_day_close": 10.2, "next_day_close": None, "window_high": None, "week_close": None},
        {"entry_price": 10.0, "target_price": 11.0, "same_day_close": None, "next_day_close": None, "window_high": None, "week_close": None},
    ]
    metrics = _aggregate_hit_metrics(records)
    assert metrics["count"] == 4
    assert metrics["same_day_hit_pct"] == round(2 / 3 * 100, 1)      # 3 条有当日数据，2 命中
    assert metrics["next_day_hit_pct"] == 50.0                       # 2 条有次日数据，1 命中
    assert metrics["target_hit_pct"] == 50.0                         # 2 条有5日数据，1 触目标
    assert metrics["week_evaluable_count"] == 2
    assert metrics["week_hit_pct"] == 50.0                           # 2 条有一周数据，1 命中
    assert metrics["avg_next_day_return_pct"] == round((0.08 - 0.02) / 2 * 100, 2)
    assert metrics["avg_week_return_pct"] == round((0.06 - 0.01) / 2 * 100, 2)


def test_paper_statistics_returns_without_detached_instance_error():
    # 回归：paper_statistics 曾在 get_db_ctx 关闭后访问 ORM 属性，触发 DetachedInstanceError。
    from src.core.database import get_db_ctx, AIStockPaperTrade, AIStockPaperLot

    service = AIStockPaperTradingService(provider=object())
    with get_db_ctx() as db:
        portfolio = AIStockPaperTradingService._ensure_portfolio(db)
        lot = AIStockPaperLot(
            portfolio_id=portfolio.id, ts_code="600000.SH", name="测试股",
            bought_at=datetime(2026, 8, 10, 10, 0), buy_price=10.0, quantity=100,
            remaining_quantity=0, target_price=11.0,
        )
        db.add(lot)
        db.flush()
        db.add(AIStockPaperTrade(
            portfolio_id=portfolio.id, lot_id=lot.id, trade_date=datetime(2026, 8, 11).date(),
            ts_code="600000.SH", name="测试股", side="SELL", price=11.0, quantity=100,
            amount=1100.0, fee=5.0, realized_pnl=95.0, reason_code="TARGET_PROFIT", reason="测试",
        ))
        db.flush()
        trade_ids = [t.id for t in db.query(AIStockPaperTrade).filter(AIStockPaperTrade.lot_id == lot.id).all()]

    try:
        stats = service.paper_statistics()
        assert stats["closed_trades"] >= 1
        assert stats["total_realized_pnl"] >= 95.0
        assert stats["win_rate_pct"] is not None
    finally:
        with get_db_ctx() as db:
            for tid in trade_ids:
                row = db.get(AIStockPaperTrade, tid)
                if row:
                    db.delete(row)
            lot = db.query(AIStockPaperLot).filter(AIStockPaperLot.ts_code == "600000.SH", AIStockPaperLot.remaining_quantity == 0).first()
            if lot:
                db.delete(lot)


def test_paper_hold_evaluations_returns_newest_first():
    from src.core.database import get_db_ctx, AIStockHoldEvaluation

    service = AIStockPaperTradingService(provider=object())
    created_ids = []
    with get_db_ctx() as db:
        portfolio = AIStockPaperTradingService._ensure_portfolio(db)
        for ts_code, score in (("600000.SH", 20.0), ("000001.SZ", 80.0)):
            row = AIStockHoldEvaluation(
                portfolio_id=portfolio.id, ts_code=ts_code, name="测试股",
                hold_score=score, reason="测试理由",
            )
            db.add(row)
            db.flush()
            created_ids.append(row.id)
    try:
        mine = [item for item in service.hold_evaluations(limit=10) if item["id"] in created_ids]
        # 新的排前面：后插入的 000001.SZ 在第一个
        assert [item["ts_code"] for item in mine] == ["000001.SZ", "600000.SH"]
        assert mine[0]["hold_score"] == 80.0
        assert mine[0]["reason"] == "测试理由"
    finally:
        with get_db_ctx() as db:
            for row_id in created_ids:
                row = db.get(AIStockHoldEvaluation, row_id)
                if row:
                    db.delete(row)

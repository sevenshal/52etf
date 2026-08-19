from src.core.realtime_quotes import RealtimeQuoteManager, normalize_code


def test_normalize_code_unifies_ptrade_and_system_formats():
    assert normalize_code("600000.SS") == "600000.SH"
    assert normalize_code("600000.SH") == "600000.SH"
    assert normalize_code("000001.SZ") == "000001.SZ"
    assert normalize_code("600000") == "600000.SH"
    assert normalize_code("000001") == "000001.SZ"
    assert normalize_code(" 600001.SS ") == "600001.SH"


def test_register_replace_semantics_per_session_source():
    manager = RealtimeQuoteManager()
    manager.register("s1", "ai_stock_page", ["600000.SS", "000001.SZ"])
    manager.register("s1", "ai_stock_page", ["600001.SH"])  # 全量替换
    assert manager.pool() == ["600001.SH"]
    assert manager.pool_version == 2


def test_pool_is_union_across_sessions_and_sources():
    manager = RealtimeQuoteManager()
    manager.register("s1", "src-a", ["600000.SH"])
    manager.register("s1", "src-b", ["000001.SZ"])
    manager.register("s2", "src-a", ["600000.SH", "600001.SH"])  # 重复去重
    assert manager.pool() == ["000001.SZ", "600000.SH", "600001.SH"]
    assert manager.pool_version == 3


def test_unregister_codes_and_whole_source():
    manager = RealtimeQuoteManager()
    manager.register("s1", "ai_stock_page", ["600000.SH", "000001.SZ"])
    manager.unregister("s1", "ai_stock_page", ["600000.SH"])
    assert manager.pool() == ["000001.SZ"]
    manager.unregister("s1", "ai_stock_page")  # 省略 codes = 清掉整个 source
    assert manager.pool() == []


def test_clear_session_on_disconnect():
    manager = RealtimeQuoteManager()
    manager.register("s1", "src-a", ["600000.SH"])
    manager.register("s2", "src-a", ["000001.SZ"])
    manager.clear_session("s1")
    assert manager.pool() == ["000001.SZ"]
    assert manager.pool_version == 3


def test_empty_register_removes_source():
    manager = RealtimeQuoteManager()
    manager.register("s1", "ai_stock_page", ["600000.SH"])
    manager.register("s1", "ai_stock_page", [])
    assert manager.pool() == []


def test_pool_version_only_bumps_on_real_change():
    manager = RealtimeQuoteManager()
    manager.register("s1", "a", ["600000.SH"])
    v = manager.pool_version
    manager.register("s1", "a", ["600000.SH"])  # 相同集合，不 bump
    assert manager.pool_version == v
    manager.update_quotes({"600000.SH": {"last_px": 10.0}})  # 行情不 bump 版本
    assert manager.pool_version == v


def test_max_pool_size_evicts_lru():
    manager = RealtimeQuoteManager(max_pool_size=3)
    manager.register("s1", "a", ["600000.SH", "600001.SH", "600002.SH"])
    # 新的 session/source 注册超出上限，最旧的 s1/a 被整体淘汰
    manager.register("s2", "b", ["000001.SZ"])
    assert manager.pool() == ["000001.SZ"]
    assert len(manager.pool()) <= 3


def test_single_source_over_limit_is_trimmed():
    manager = RealtimeQuoteManager(max_pool_size=3)
    manager.register("s1", "a", ["600000.SH", "600001.SH", "600002.SH", "600003.SH"])
    assert len(manager.pool()) == 3


def test_update_quotes_merges_and_prunes_with_pool():
    manager = RealtimeQuoteManager()
    manager.register("s1", "a", ["600000.SH", "000001.SZ"])
    manager.update_quotes({
        "600000.SH": {"last_px": 10.0},
        "999999.SH": {"last_px": 99.0},  # 不在池里，也先缓存
    })
    assert manager.quote("600000.SH")["last_px"] == 10.0
    assert manager.quote("999999.SH") is not None
    # 池变化时清理不在池内的行情
    manager.unregister("s1", "a", ["600000.SH"])
    assert manager.quote("600000.SH") is None
    assert manager.quote("000001.SZ") is None
    assert manager.quote("999999.SH") is None


def test_snapshot_and_missing_backfill_do_not_overwrite_live_tick():
    manager = RealtimeQuoteManager()
    manager.update_quotes({"600000.SH": {"last_px": 10.2, "source": "ptrade"}})

    snapshot = manager.snapshot(["600000.SS", "000001.SZ"])
    assert snapshot["600000.SH"]["last_px"] == 10.2

    filled = manager.update_missing_quotes({
        "600000.SH": {"last_px": 10.0, "source": "tushare_rt"},
        "000001.SZ": {"last_px": 11.0, "open_px": 10.8, "source": "tushare_rt"},
    })
    assert filled["600000.SH"]["last_px"] == 10.2
    assert manager.quote("000001.SZ")["open_px"] == 10.8

"""个股所属贪恐指数：复用雪球持仓表的「所属贪恐指数」口径。"""

from datetime import date

import duckdb
import pytest

from src.app.api import xueqiu_holdings as xh

STOCK = "688981.SH"


class _Query:
    def __init__(self, first=None, rows=None):
        self._first, self._rows = first, rows

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def first(self):
        return self._first

    def all(self):
        return self._rows or []


class _FakeSession:
    """A创100 成分：第一次 query 取最新调仓日，第二次取该日成分。"""

    def __init__(self, members):
        self._members = members
        self._calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def query(self, column):
        self._calls += 1
        if self._calls == 1:
            return _Query(first=(date(2026, 6, 30),) if self._members else None)
        return _Query(rows=[(code,) for code in self._members])


@pytest.fixture
def weights_db(tmp_path, monkeypatch):
    path = tmp_path / "weights.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE a_stock_index_weight (index_code VARCHAR, con_code VARCHAR, trade_date DATE, weight DOUBLE)")
    con.executemany("INSERT INTO a_stock_index_weight VALUES (?, ?, ?, ?)", [
        # 沪深300：最新一期(8月)仍在
        ("000300.SH", STOCK, date(2026, 7, 31), 1.2),
        ("000300.SH", STOCK, date(2026, 8, 31), 1.3),
        # 中证500：7 月还在，8 月那期被调出——最新一期不含它就不算成员
        ("000905.SH", STOCK, date(2026, 7, 31), 0.4),
        ("000905.SH", "600000.SH", date(2026, 8, 31), 0.5),
        # 不在贪恐目标里的指数，不该出现
        ("999999.SH", STOCK, date(2026, 8, 31), 9.9),
    ])
    con.close()
    monkeypatch.setattr(xh, "connect_analytics_db", lambda: duckdb.connect(str(path)))
    monkeypatch.setattr(xh, "DBSession", lambda: _FakeSession([STOCK]))
    return path


def _labels(indexes):
    return [item["label"] for item in indexes]


def test_uses_the_latest_weight_snapshot_of_each_index(weights_db):
    indexes = xh.load_a_stock_fear_index_memberships(STOCK, as_of=date(2026, 9, 12))
    symbols = {item["symbol"] for item in indexes}

    assert "000300.SH" in symbols
    assert "000905.SH" not in symbols  # 最新一期已被调出
    assert "999999.SH" not in symbols  # 不是贪恐目标指数


def test_includes_the_locally_maintained_innovation_100(weights_db):
    indexes = xh.load_a_stock_fear_index_memberships(STOCK, as_of=date(2026, 9, 12))

    assert any(item["symbol"] == xh.A_STOCK_INNO100_SYMBOL and item["label"] == "A创100" for item in indexes)


def test_as_of_picks_the_snapshot_that_was_visible_then(weights_db):
    """回看 8 月之前：那时中证500 那期还含它。"""
    indexes = xh.load_a_stock_fear_index_memberships(STOCK, as_of=date(2026, 8, 15))

    assert "000905.SH" in {item["symbol"] for item in indexes}


def test_results_are_sorted_by_label_and_accept_any_symbol_format(weights_db):
    for raw in ("688981.sh", "SH688981", "sh.688981"):
        indexes = xh.load_a_stock_fear_index_memberships(raw, as_of=date(2026, 9, 12))
        assert _labels(indexes) == sorted(_labels(indexes))
        assert "000300.SH" in {item["symbol"] for item in indexes}


def test_a_stock_in_no_target_index_gets_an_empty_list(weights_db, monkeypatch):
    monkeypatch.setattr(xh, "DBSession", lambda: _FakeSession([]))
    assert xh.load_a_stock_fear_index_memberships("000001.SZ", as_of=date(2026, 9, 12)) == []
    assert xh.load_a_stock_fear_index_memberships("", as_of=date(2026, 9, 12)) == []


def test_innovation_100_lookup_failure_does_not_break_the_other_indexes(weights_db, monkeypatch):
    def _broken():
        raise RuntimeError("sqlite unavailable")

    monkeypatch.setattr(xh, "DBSession", _broken)
    indexes = xh.load_a_stock_fear_index_memberships(STOCK, as_of=date(2026, 9, 12))

    assert "000300.SH" in {item["symbol"] for item in indexes}
    assert xh.A_STOCK_INNO100_SYMBOL not in {item["symbol"] for item in indexes}


def test_endpoint_returns_normalized_symbol_and_memberships(weights_db):
    from src.app.api.stock import get_a_stock_fear_indexes

    payload = get_a_stock_fear_indexes("688981.sh", _="account")

    assert payload["symbol"] == STOCK
    assert "000300.SH" in {item["symbol"] for item in payload["indexes"]}

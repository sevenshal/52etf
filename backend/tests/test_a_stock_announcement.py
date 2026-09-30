"""东方财富业绩公告流（精确披露时刻）解析与入库。"""
from datetime import date, datetime

import duckdb
import pandas as pd

from src.core.services import a_stock_announcement as ann


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeSession:
    """按 page_index 依次返回预设响应，记录请求参数。"""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(dict(params or {}))
        index = int((params or {}).get("page_index") or 1) - 1
        payload = self.pages[index] if index < len(self.pages) else {"success": True, "data": {"list": [], "total_hits": 0}}
        return FakeResponse(payload)

    def close(self):
        return None


def _item(art_code, stock_code, market, ann_date, display_time, column_code, column_name, title, short_name="测试"):
    ann_type = {"SH": "A,SHA", "SZ": "A", "BJ": "A,BJA"}[market]
    market_code = "1" if market == "SH" else "0"
    return {
        "art_code": art_code,
        "notice_date": ann_date,
        "display_time": display_time,
        "title": title,
        "columns": [{"column_code": column_code, "column_name": column_name}],
        "codes": [{
            "stock_code": stock_code,
            "short_name": short_name,
            "market_code": market_code,
            "ann_type": ann_type,
        }],
    }


def test_code_to_ts_code():
    assert ann.code_to_ts_code("688469") == "688469.SH"
    assert ann.code_to_ts_code("000001") == "000001.SZ"
    assert ann.code_to_ts_code("300750.SZ") == "300750.SZ"
    assert ann.code_to_ts_code("920932") == "920932.BJ"
    assert ann.code_to_ts_code("830799") == "830799.BJ"
    assert ann.code_to_ts_code("") is None
    assert ann.code_to_ts_code("A26206") is None


def test_parse_display_time_handles_millisecond_suffix():
    assert ann._parse_display_time("2026-09-28 19:34:43:178") == datetime(2026, 9, 28, 19, 34, 43)
    assert ann._parse_display_time("2026-09-28 09:00:00") == datetime(2026, 9, 28, 9, 0, 0)
    assert ann._parse_display_time("") is None
    assert ann._parse_display_time(None) is None


def test_normalize_announcement_rows_classifies_event_kind():
    payload = {
        "list": [
            _item("A1", "688469", "SH", "2026-09-29", "2026-09-28 19:34:43:178",
                  "001002004001", "业绩预告", "芯联集成:2026年前三季度业绩预告", "芯联集成"),
            _item("A2", "000028", "SZ", "2026-08-18", "2026-08-17 20:10:00:000",
                  "001002004002", "业绩快报", "国药一致:2026年半年度业绩快报"),
            _item("A3", "600519", "SH", "2026-08-15", "2026-08-14 19:00:00:000",
                  "001001001002001", "半年度报告全文", "贵州茅台:2026年半年度报告"),
            # 分类未知但标题带关键词 → 按标题兜底
            _item("A4", "000001", "SZ", "2026-04-28", "2026-04-27 21:00:00:000",
                  "999999999", "其它", "平安银行:2026年第一季度报告"),
            # 与业绩无关的公告不进库
            _item("A5", "000002", "SZ", "2026-09-22", "2026-09-21 16:39:08:199",
                  "001002006012001001", "对外项目投资", "万科A:对外投资进展公告"),
        ]
    }
    rows = ann.normalize_announcement_rows(payload, ann.EVENT_KIND_REPORT)
    by_code = {row["art_code"]: row for row in rows}
    assert set(by_code) == {"A1", "A2", "A3", "A4"}
    assert by_code["A1"]["ts_code"] == "688469.SH"
    assert by_code["A1"]["event_kind"] == ann.EVENT_KIND_FORECAST
    assert by_code["A1"]["disclose_at"] == datetime(2026, 9, 28, 19, 34, 43)
    assert by_code["A2"]["event_kind"] == ann.EVENT_KIND_EXPRESS
    assert by_code["A2"]["ts_code"] == "000028.SZ"
    assert by_code["A3"]["event_kind"] == ann.EVENT_KIND_REPORT
    assert by_code["A4"]["event_kind"] == ann.EVENT_KIND_REPORT


def test_normalize_prefers_a_share_code_when_multiple_codes():
    payload = {"list": [{
        "art_code": "A9",
        "notice_date": "2026-08-18",
        "display_time": "2026-08-17 20:00:00:000",
        "title": "一致B:2026年半年度报告",
        "columns": [{"column_code": "001001001002001", "column_name": "半年度报告全文"}],
        "codes": [
            {"stock_code": "200028", "short_name": "一致B", "market_code": "0", "ann_type": "B"},
            {"stock_code": "000028", "short_name": "国药一致", "market_code": "0", "ann_type": "A"},
        ],
    }]}
    rows = ann.normalize_announcement_rows(payload, ann.EVENT_KIND_REPORT)
    assert [row["ts_code"] for row in rows] == ["000028.SZ"]


def test_fetch_announcements_pages_until_total_reached():
    # 第一页返回整页（100 条）、总数 101 → 必须继续翻第二页
    first_items = [
        _item(f"A{i:04d}", f"60{i:04d}", "SH", "2026-09-29", "2026-09-28 19:00:00:000",
              "001002004001", "业绩预告", f"测试{i}:业绩预告")
        for i in range(100)
    ]
    first = {"success": True, "data": {"total_hits": 101, "list": first_items}}
    second = {"success": True, "data": {
        "total_hits": 101,
        "list": [_item("A9999", "688469", "SH", "2026-09-29", "2026-09-28 19:34:43:178",
                       "001002004001", "业绩预告", "芯联集成:业绩预告")],
    }}
    session = FakeSession([first, second])
    frame = ann.fetch_announcements(date(2026, 9, 28), date(2026, 9, 29), session=session, nodes=(5,))
    assert len(frame) == 101
    assert "A9999" in set(frame["art_code"])
    assert [call["page_index"] for call in session.calls] == [1, 2]

    # 单页就够了时不再多翻一页
    alone = FakeSession([second])
    frame = ann.fetch_announcements(date(2026, 9, 28), date(2026, 9, 29), session=alone, nodes=(5,))
    assert len(frame) == 1 and len(alone.calls) == 1


def test_load_disclosure_times_takes_earliest_per_key():
    connection = duckdb.connect(":memory:")
    connection.execute(
        "CREATE TABLE a_stock_announcement (art_code VARCHAR, ts_code VARCHAR, ann_date DATE,"
        " disclose_at TIMESTAMP, event_kind VARCHAR)"
    )
    connection.execute(
        "INSERT INTO a_stock_announcement VALUES"
        " ('A2', '600519.SH', DATE '2026-08-15', TIMESTAMP '2026-08-14 19:30:00', 'report'),"
        " ('A1', '600519.SH', DATE '2026-08-15', TIMESTAMP '2026-08-14 19:00:00', 'report'),"
        " ('A3', '000028.SZ', DATE '2026-08-18', TIMESTAMP '2026-08-17 20:10:00', 'express'),"
        " ('A4', '300750.SZ', DATE '2026-08-18', NULL, 'express')"
    )
    result = ann.load_disclosure_times(connection, date(2026, 1, 1))
    assert result == {
        ("600519.SH", date(2026, 8, 15), "report"): datetime(2026, 8, 14, 19, 0, 0),
        ("000028.SZ", date(2026, 8, 18), "express"): datetime(2026, 8, 17, 20, 10, 0),
    }


def test_load_disclosure_times_without_table_returns_empty():
    connection = duckdb.connect(":memory:")
    assert ann.load_disclosure_times(connection, date(2026, 1, 1)) == {}


def test_upsert_announcements_requires_columns():
    """空帧直接返回 0，不去碰分析库。"""
    assert ann._upsert_announcements(pd.DataFrame()) == 0


def test_upsert_announcements_writes_and_replaces(tmp_path, monkeypatch):
    """写入走 INSERT OR REPLACE：同一 art_code 重复同步只覆盖不新增。"""
    db_path = str(tmp_path / "analytics.duckdb")
    monkeypatch.setattr(ann, "ANALYTICS_DB_PATH", db_path)
    connection = duckdb.connect(db_path)
    connection.execute(
        "CREATE TABLE a_stock_announcement (art_code VARCHAR PRIMARY KEY, ts_code VARCHAR, name VARCHAR,"
        " ann_date DATE, disclose_at TIMESTAMP, event_kind VARCHAR, category_code VARCHAR,"
        " category_name VARCHAR, title VARCHAR, source VARCHAR, created_at TIMESTAMP, updated_at TIMESTAMP)"
    )
    connection.close()

    frame = pd.DataFrame([{
        "art_code": "A1",
        "ts_code": "688469.SH",
        "name": "芯联集成",
        "ann_date": date(2026, 9, 29),
        "disclose_at": datetime(2026, 9, 28, 19, 34, 43),
        "event_kind": "forecast",
        "category_code": "001002004001",
        "category_name": "业绩预告",
        "title": "芯联集成:2026年前三季度业绩预告",
        "source": "eastmoney",
    }])
    assert ann._upsert_announcements(frame) == 1
    frame.loc[0, "disclose_at"] = datetime(2026, 9, 28, 20, 0, 0)
    assert ann._upsert_announcements(frame) == 1

    connection = duckdb.connect(db_path, read_only=True)
    rows = connection.execute(
        "SELECT art_code, ts_code, disclose_at, event_kind FROM a_stock_announcement"
    ).fetchall()
    connection.close()
    assert rows == [("A1", "688469.SH", datetime(2026, 9, 28, 20, 0, 0), "forecast")]

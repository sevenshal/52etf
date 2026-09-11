import logging
from datetime import date, timedelta

import pandas as pd
import pytest

from src.core.services import tushare as tushare_module
from src.core.services.tushare import TushareService


class _NoWait:
    def wait(self):
        pass


class FakeReportRcPro:
    """模拟 tushare report_rc：按 report_date 倒序分页，offset 超上限直接报错。"""

    def __init__(self, rows, max_offset, fail_at_offset=None):
        self.rows = sorted(rows, key=lambda row: row["report_date"], reverse=True)
        self.max_offset = max_offset
        self.fail_at_offset = fail_at_offset
        self.calls = []

    def report_rc(self, start_date, end_date, limit, offset, **kwargs):
        self.calls.append((start_date, end_date, offset))
        if offset > self.max_offset or offset == self.fail_at_offset:
            raise Exception("查询数据失败，请确认参数！")
        matched = [row for row in self.rows if start_date <= row["report_date"] <= end_date]
        return pd.DataFrame(matched[offset:offset + limit])


def _rows(first_day, days, per_day):
    rows = []
    for day_index in range(days):
        report_date = (first_day + timedelta(days=day_index)).strftime("%Y%m%d")
        for seq in range(per_day):
            rows.append(
                {
                    "ts_code": f"{seq:06d}.SZ",
                    "name": "测试",
                    "report_date": report_date,
                    "report_title": f"点评{seq}",
                    "report_type": "点评",
                    "classify": "一般报告",
                    "org_name": "测试证券",
                    "author_name": "张三",
                    "quarter": "2024Q4",
                    "eps": 1.0,
                }
            )
    return rows


def _service(pro):
    service = object.__new__(TushareService)
    service.pro = pro
    service.logger = logging.getLogger("test")
    service._report_rc_rate_limiter = _NoWait()
    return service


def test_report_rc_splits_date_range_instead_of_truncating_at_offset_cap(monkeypatch):
    monkeypatch.setattr(tushare_module, "TUSHARE_REPORT_RC_MAX_OFFSET", 4)
    pro = FakeReportRcPro(_rows(date(2024, 1, 1), days=4, per_day=3), max_offset=4)

    frame = _service(pro).get_a_stock_report_rc_range_frame(
        date(2024, 1, 1),
        date(2024, 1, 4),
        limit=2,
        raise_on_error=True,
    )

    assert len(frame) == 12
    assert sorted(frame["report_date"].unique()) == [date(2024, 1, day) for day in range(1, 5)]
    assert all(offset <= 4 for _, _, offset in pro.calls)


def test_report_rc_raises_instead_of_returning_partial_pages(monkeypatch):
    pro = FakeReportRcPro(_rows(date(2024, 1, 1), days=2, per_day=3), max_offset=100, fail_at_offset=2)

    with pytest.raises(Exception, match="查询数据失败"):
        _service(pro).get_a_stock_report_rc_range_frame(
            date(2024, 1, 1),
            date(2024, 1, 2),
            limit=2,
            raise_on_error=True,
        )

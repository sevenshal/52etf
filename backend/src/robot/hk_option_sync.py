"""港交所期权日报同步（定时任务与脚本共用入口）。

只做增量：默认从库里最新一天往后、并至少覆盖最近 ``recent_days`` 个自然日，
节假日返回 404 会被跳过；htm 报告过期（约一年）的日期改走 DTOP 结算统计回补。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, Optional

from ..core.database import HKOptionDaily, Session
from ..core.services.hk_option_service import HKOptionDailySync

DEFAULT_RECENT_DAYS = 7


def latest_synced_date() -> Optional[date]:
    db = Session()
    try:
        return db.query(HKOptionDaily.date).order_by(HKOptionDaily.date.desc()).limit(1).scalar()
    finally:
        Session.remove()


def sync_hk_option_daily(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    recent_days: int = DEFAULT_RECENT_DAYS,
    include_etf: bool = True,
    allow_dtop_backfill: bool = True,
) -> Dict[str, object]:
    end_value = end_date or date.today()
    if start_date is not None:
        start_value = start_date
    else:
        floor = end_value - timedelta(days=max(int(recent_days), 0))
        latest = latest_synced_date()
        # 从最新一天重跑（幂等），顺带补上中间漏掉的日子；再往前只到 floor。
        start_value = floor if latest is None else max(floor, latest)
    if start_value > end_value:
        return {
            "start_date": start_value.isoformat(),
            "end_date": end_value.isoformat(),
            "saved": 0,
            "skipped": "already up to date",
        }
    sync = HKOptionDailySync(include_etf=include_etf, allow_dtop_backfill=allow_dtop_backfill)
    try:
        return sync.sync_range(start_value, end_value)
    finally:
        sync.close()

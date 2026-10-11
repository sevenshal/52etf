"""守猪逮兔美股盘中量比：只在策略定时点调用 Longport 分钟 K 线。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable
from zoneinfo import ZoneInfo

from ..database import LongPortAccount, SZDTUSIntradayVolumeSnapshot, get_db_ctx
from .longport import LongPortService


NEW_YORK = ZoneInfo("America/New_York")


def _longport_account_id(account_id: str) -> str:
    with get_db_ctx() as db:
        row = db.query(LongPortAccount).filter(LongPortAccount.account_id == account_id).first()
        if row:
            return str(row.lp_account_id)
        # 与系统统一行情服务的兜底账户保持一致，兼容存量 SZDT 配置。
        return "LBPT10001248"


def _symbol(code: str) -> str:
    text = str(code or "").upper()
    return f"{text.split('.', 1)[1]}.US" if text.startswith("US.") else text


def get_us_stock_volume_metrics(
    account_id: str, codes: Iterable[str], *, now: datetime | None = None,
) -> Dict[str, Dict[str, Any]]:
    """A 股同口径：当前累计分钟量 ÷ 前20交易日同一时点累计量均值。

    每个策略定时点都把当天累计量写进主库；后续无需向 Longport 回补历史分钟线，
    且服务重启不会丢失基准。新接入标的在积累满 20 个交易日之前不会通过量比门槛。
    """
    current = now or datetime.now(NEW_YORK)
    current = current.astimezone(NEW_YORK)
    minute_label = current.strftime("%H:%M")
    trade_date = current.date()
    service = LongPortService.get_instance(_longport_account_id(account_id))
    metrics: Dict[str, Dict[str, Any]] = {}
    for code in dict.fromkeys(str(item).upper() for item in codes if str(item).strip()):
        symbol = _symbol(code)
        try:
            # 每次只取截至当前时点的一段分钟线；策略每天仅两次调用。
            elapsed = max(1, (current.hour * 60 + current.minute) - (9 * 60 + 30))
            minutes = service.get_candlesticks(symbol, elapsed + 5, period="1m", adjust_type="none") or []
            cumulative_volume = sum(
                float(item.get("volume") or 0) for item in minutes
                if getattr(item.get("timestamp"), "date", lambda: None)() == trade_date
            )
            with get_db_ctx() as db:
                snapshot = db.query(SZDTUSIntradayVolumeSnapshot).filter_by(
                    code=code, trade_date=trade_date, minute_label=minute_label,
                ).first()
                if snapshot:
                    snapshot.cumulative_volume = cumulative_volume
                else:
                    db.add(SZDTUSIntradayVolumeSnapshot(
                        code=code, trade_date=trade_date, minute_label=minute_label,
                        cumulative_volume=cumulative_volume,
                    ))
                rows = db.query(SZDTUSIntradayVolumeSnapshot.cumulative_volume).filter(
                    SZDTUSIntradayVolumeSnapshot.code == code,
                    SZDTUSIntradayVolumeSnapshot.minute_label == minute_label,
                    SZDTUSIntradayVolumeSnapshot.trade_date < trade_date,
                ).order_by(SZDTUSIntradayVolumeSnapshot.trade_date.desc()).limit(20).all()
            baseline = sum(float(row[0]) for row in rows) / len(rows) if len(rows) >= 20 else 0.0
            metrics[code] = {
                "volume": cumulative_volume,
                "baseline_volume": baseline,
                "volume_ratio": cumulative_volume / baseline if baseline > 0 else None,
                "minute_label": minute_label,
                "baseline_days": len(rows),
            }
        except Exception:
            # 一只标的失败不阻断其它配置；调用方将该标的视为量比不可用而跳过买入。
            metrics[code] = {"volume_ratio": None}
    return metrics

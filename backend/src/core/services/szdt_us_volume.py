"""守猪逮兔美股盘中量比：只在策略定时点调用 Longport 分钟 K 线。"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any, Dict, Iterable, List, Tuple
from zoneinfo import ZoneInfo

from ..database import (
    LongPortAccount,
    SZDTUSIntradayVolumeSnapshot,
    SzdtTradeStock,
    get_db_ctx,
)
from .longport import LongPortService


NEW_YORK = ZoneInfo("America/New_York")
VOLUME_BASELINE_LOOKBACK = 20
# 与美股策略的两个执行点严格一致。历史分钟线在盘前一次性预热，盘中只拉当日一小段。
VOLUME_BASELINE_MINUTES = ("10:30", "15:55")


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


def _code_from_symbol(symbol: str) -> str:
    text = str(symbol or "").upper()
    return f"US.{text.rsplit('.', 1)[0]}" if text.endswith(".US") else text


def _as_new_york(timestamp: Any) -> datetime | None:
    if not isinstance(timestamp, datetime):
        return None
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=NEW_YORK)
    return timestamp.astimezone(NEW_YORK)


def _historical_cumulative_volumes(
    candles: Iterable[Dict[str, Any]], *, before_date,
) -> Dict[Tuple[object, str], float]:
    """按交易日和同一时点累计分钟量，排除盘前/盘后和当天。"""
    result: Dict[Tuple[object, str], float] = {}
    for candle in candles:
        timestamp = _as_new_york(candle.get("timestamp"))
        if timestamp is None or timestamp.date() >= before_date:
            continue
        label = timestamp.strftime("%H:%M")
        if not ("09:30" <= label <= "16:00"):
            continue
        volume = float(candle.get("volume") or 0)
        for checkpoint in VOLUME_BASELINE_MINUTES:
            if label <= checkpoint:
                key = (timestamp.date(), checkpoint)
                result[key] = result.get(key, 0.0) + volume
    return result


def _missing_baseline_codes() -> List[Tuple[str, str]]:
    """返回每个需要预热的代码及可使用的 Longport 账户；同代码只预热一次。"""
    with get_db_ctx() as db:
        rows = db.query(SzdtTradeStock.account_id, SzdtTradeStock.code).filter(
            SzdtTradeStock.enabled.is_(True),
            SzdtTradeStock.type.in_([1, 2, 7]),
            SzdtTradeStock.buy_volume_ratio > 0,
        ).all()
        selected: Dict[str, str] = {}
        for account_id, code in rows:
            normalized = str(code or "").upper()
            if normalized and normalized not in selected:
                selected[normalized] = str(account_id or "")
        result = []
        for code, account_id in selected.items():
            rows = db.query(
                SZDTUSIntradayVolumeSnapshot.trade_date,
                SZDTUSIntradayVolumeSnapshot.minute_label,
            ).filter(
                SZDTUSIntradayVolumeSnapshot.code == code,
                SZDTUSIntradayVolumeSnapshot.minute_label.in_(VOLUME_BASELINE_MINUTES),
            ).all()
            labels_by_date: Dict[object, set[str]] = {}
            for trade_date, minute_label in rows:
                labels_by_date.setdefault(trade_date, set()).add(str(minute_label))
            complete = sum(
                labels >= set(VOLUME_BASELINE_MINUTES)
                for labels in labels_by_date.values()
            )
            if complete < VOLUME_BASELINE_LOOKBACK:
                result.append((account_id, code))
    return result


def warm_us_volume_baselines(*, now: datetime | None = None) -> Dict[str, Any]:
    """盘前预热美股同刻量比基准。

    Longport 分钟历史线最多每次 1,000 根，向前分页至收集到 20 个完整交易日；
    每标的大约 8 次请求。只为缺少基准、并实际开启量比过滤的配置请求历史数据。
    """
    current = now or datetime.now(NEW_YORK)
    current = current.replace(tzinfo=NEW_YORK) if current.tzinfo is None else current.astimezone(NEW_YORK)
    targets = _missing_baseline_codes()
    result: Dict[str, Any] = {"targets": len(targets), "warmed": 0, "saved": 0, "errors": []}
    for account_id, code in targets:
        try:
            service = LongPortService.get_instance(_longport_account_id(account_id))
            cursor = datetime.combine(current.date(), time(9, 30), tzinfo=NEW_YORK)
            candles: List[Dict[str, Any]] = []
            # 20 * 390 / 1000 向上取整后留一页处理节假日和半日市。
            for _ in range(10):
                page = service.get_historical_candlesticks_by_offset(
                    _symbol(code), cursor, count=1000, period="1m", adjust_type="none",
                )
                if not page:
                    break
                candles.extend(page)
                timestamps = [_as_new_york(item.get("timestamp")) for item in page]
                earliest = min((item for item in timestamps if item is not None), default=None)
                if earliest is None:
                    break
                cursor = earliest - timedelta(minutes=1)
                cumulative = _historical_cumulative_volumes(candles, before_date=current.date())
                dates = {trade_date for trade_date, _ in cumulative}
                if len(dates) >= VOLUME_BASELINE_LOOKBACK:
                    break
            cumulative = _historical_cumulative_volumes(candles, before_date=current.date())
            dates = sorted({trade_date for trade_date, _ in cumulative}, reverse=True)[:VOLUME_BASELINE_LOOKBACK]
            if len(dates) < VOLUME_BASELINE_LOOKBACK:
                raise RuntimeError(f"历史分钟线不足20个交易日，仅获取到{len(dates)}天")
            with get_db_ctx() as db:
                for trade_date in dates:
                    for minute_label in VOLUME_BASELINE_MINUTES:
                        volume = cumulative.get((trade_date, minute_label))
                        if volume is None:
                            continue
                        snapshot = db.query(SZDTUSIntradayVolumeSnapshot).filter_by(
                            code=code, trade_date=trade_date, minute_label=minute_label,
                        ).first()
                        if snapshot:
                            snapshot.cumulative_volume = volume
                        else:
                            db.add(SZDTUSIntradayVolumeSnapshot(
                                code=code, trade_date=trade_date, minute_label=minute_label,
                                cumulative_volume=volume,
                            ))
                        result["saved"] += 1
            result["warmed"] += 1
        except Exception as exc:
            logging.exception("美股量比基准预热失败: %s", code)
            result["errors"].append({"code": code, "error": str(exc)})
    return result


def get_us_stock_list_metrics(
    account_id: str, codes: Iterable[str], *, now: datetime | None = None,
) -> Dict[str, Dict[str, Any]]:
    """美股列表指标：批量实时行情 + 本地持久化的策略同刻量比。

    列表刷新不触发逐标的分钟 K 请求，避免与策略共用 Longport 配额时放大调用量。
    量比仅在当天已有策略执行快照时提供；成交量和成交额则取实时批量 quote。
    """
    current = now or datetime.now(NEW_YORK)
    current = current.replace(tzinfo=NEW_YORK) if current.tzinfo is None else current.astimezone(NEW_YORK)
    normalized_codes = list(dict.fromkeys(str(item).upper() for item in codes if str(item).strip()))
    metrics: Dict[str, Dict[str, Any]] = {code: {"volume": None, "turnover": None, "volume_ratio": None} for code in normalized_codes}
    if not normalized_codes:
        return metrics

    # quote 是一个批量调用，包含实时累计成交量与成交额。
    try:
        service = LongPortService.get_instance(_longport_account_id(account_id))
        quotes = service.get_quote_batch([_symbol(code) for code in normalized_codes])
        for quote in quotes:
            code = _code_from_symbol(quote.get("symbol"))
            if code in metrics:
                metrics[code].update({
                    "volume": quote.get("volume"),
                    "turnover": quote.get("turnover"),
                })
    except Exception:
        # 列表行情失败不影响贪恐数据或交易配置展示。
        pass

    # 最近一次策略时点的累计量快照可直接复用；用对应前 20 天相同时点基准还原量比。
    now_label = current.strftime("%H:%M")
    with get_db_ctx() as db:
        snapshots = db.query(SZDTUSIntradayVolumeSnapshot).filter(
            SZDTUSIntradayVolumeSnapshot.code.in_(normalized_codes),
            SZDTUSIntradayVolumeSnapshot.trade_date == current.date(),
            SZDTUSIntradayVolumeSnapshot.minute_label <= now_label,
        ).order_by(SZDTUSIntradayVolumeSnapshot.minute_label.desc()).all()
        latest_by_code: Dict[str, SZDTUSIntradayVolumeSnapshot] = {}
        for snapshot in snapshots:
            latest_by_code.setdefault(snapshot.code, snapshot)
        for code, snapshot in latest_by_code.items():
            rows = db.query(SZDTUSIntradayVolumeSnapshot.cumulative_volume).filter(
                SZDTUSIntradayVolumeSnapshot.code == code,
                SZDTUSIntradayVolumeSnapshot.minute_label == snapshot.minute_label,
                SZDTUSIntradayVolumeSnapshot.trade_date < current.date(),
            ).order_by(SZDTUSIntradayVolumeSnapshot.trade_date.desc()).limit(VOLUME_BASELINE_LOOKBACK).all()
            if len(rows) < VOLUME_BASELINE_LOOKBACK:
                continue
            baseline = sum(float(row[0]) for row in rows) / len(rows)
            if baseline > 0:
                metrics[code]["volume_ratio"] = float(snapshot.cumulative_volume) / baseline
                metrics[code]["volume_ratio_time"] = snapshot.minute_label
            # 休市或 quote 不可用时，至少展示最近策略时点的累计量。
            if metrics[code].get("volume") is None:
                metrics[code]["volume"] = snapshot.cumulative_volume
    return metrics


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

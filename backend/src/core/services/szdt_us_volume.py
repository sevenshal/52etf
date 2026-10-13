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
SHANGHAI = ZoneInfo("Asia/Shanghai")
VOLUME_BASELINE_LOOKBACK = 20
SESSION_OPEN_MINUTE = 9 * 60 + 30
SESSION_CLOSE_MINUTE = 16 * 60


def _session_minute_labels() -> Tuple[str, ...]:
    """美股正常交易时段的每一个分钟标签（09:30 至 15:59）。"""
    return tuple(
        f"{minute // 60:02d}:{minute % 60:02d}"
        for minute in range(SESSION_OPEN_MINUTE, SESSION_CLOSE_MINUTE)
    )


# 基准必须覆盖完整交易时段，而非取决于策略当前配置的 cron。这样随时调整触发时刻时，
# 新时点也可以直接使用过去 20 个交易日的同刻累计成交量。
VOLUME_BASELINE_MINUTES = _session_minute_labels()
VOLUME_BASELINE_MINUTE_SET = frozenset(VOLUME_BASELINE_MINUTES)


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
    # LongPort Python SDK 的分钟线 timestamp 是无时区 datetime，实际按运行环境
    # 的上海时间输出（例如纽约 15:55 会返回次日 03:55）。不能直接当纽约时间。
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=SHANGHAI).astimezone(NEW_YORK)
    return timestamp.astimezone(NEW_YORK)


def _historical_cumulative_volumes(
    candles: Iterable[Dict[str, Any]], *, before_date,
) -> Dict[Tuple[object, str], float]:
    """按交易日生成每个交易分钟的累计成交量，排除盘前/盘后和当天。"""
    minute_volumes: Dict[object, Dict[str, float]] = {}
    seen: set[Tuple[object, str]] = set()
    for candle in candles:
        timestamp = _as_new_york(candle.get("timestamp"))
        if timestamp is None or timestamp.date() >= before_date:
            continue
        label = timestamp.strftime("%H:%M")
        if label not in VOLUME_BASELINE_MINUTE_SET:
            continue
        key = (timestamp.date(), label)
        # 分页边界偶有重叠；同一分钟只能计一次，避免基准被重复累计。
        if key in seen:
            continue
        seen.add(key)
        minute_volumes.setdefault(timestamp.date(), {})[label] = float(candle.get("volume") or 0)

    result: Dict[Tuple[object, str], float] = {}
    for trade_date, volumes in minute_volumes.items():
        cumulative_volume = 0.0
        for label in VOLUME_BASELINE_MINUTES:
            cumulative_volume += volumes.get(label, 0.0)
            result[(trade_date, label)] = cumulative_volume
    return result


def _volume_baseline_codes() -> List[Tuple[str, str, bool]]:
    """返回需维护基准的代码、账户及是否已具备完整 20 日基准。"""
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
        result: List[Tuple[str, str, bool]] = []
        for code, account_id in selected.items():
            rows = db.query(
                SZDTUSIntradayVolumeSnapshot.trade_date,
                SZDTUSIntradayVolumeSnapshot.minute_label,
            ).filter(
                SZDTUSIntradayVolumeSnapshot.code == code,
            ).all()
            labels_by_date: Dict[object, set[str]] = {}
            for trade_date, minute_label in rows:
                label = str(minute_label)
                if label in VOLUME_BASELINE_MINUTE_SET:
                    labels_by_date.setdefault(trade_date, set()).add(label)
            complete = sum(
                labels >= VOLUME_BASELINE_MINUTE_SET
                for labels in labels_by_date.values()
            )
            # 即使已有 20 日，也须每天更新最近一个交易日，保证滚动窗口不会过期。
            result.append((account_id, code, complete >= VOLUME_BASELINE_LOOKBACK))
    return result


def warm_us_volume_baselines(*, now: datetime | None = None) -> Dict[str, Any]:
    """盘前预热美股每分钟量比基准。

    Longport 分钟历史线最多每次 1,000 根，向前分页至收集到 20 个完整交易日；
    首次预热每标的大约 8 次请求，后续每日只取最新一页以滚动更新。
    只处理实际开启量比过滤的美股配置。
    """
    current = now or datetime.now(NEW_YORK)
    current = current.replace(tzinfo=NEW_YORK) if current.tzinfo is None else current.astimezone(NEW_YORK)
    targets = _volume_baseline_codes()
    result: Dict[str, Any] = {"targets": len(targets), "warmed": 0, "saved": 0, "errors": []}
    for account_id, code, is_warm in targets:
        try:
            service = LongPortService.get_instance(_longport_account_id(account_id))
            cursor = datetime.combine(current.date(), time(9, 30), tzinfo=NEW_YORK)
            candles: List[Dict[str, Any]] = []
            # 已预热的标的每天只需最新一页（约 2.5 个交易日）；首次则向前补满 20 日。
            # 20 * 390 / 1000 向上取整后留一页处理节假日和半日市。
            for _ in range(1 if is_warm else 10):
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
                if not is_warm and len(dates) >= VOLUME_BASELINE_LOOKBACK:
                    break
            cumulative = _historical_cumulative_volumes(candles, before_date=current.date())
            dates = sorted({trade_date for trade_date, _ in cumulative}, reverse=True)
            if not is_warm:
                dates = dates[:VOLUME_BASELINE_LOOKBACK]
            if not is_warm and len(dates) < VOLUME_BASELINE_LOOKBACK:
                raise RuntimeError(f"历史分钟线不足20个交易日，仅获取到{len(dates)}天")
            with get_db_ctx() as db:
                existing = {
                    (snapshot.trade_date, snapshot.minute_label): snapshot
                    for snapshot in db.query(SZDTUSIntradayVolumeSnapshot).filter(
                        SZDTUSIntradayVolumeSnapshot.code == code,
                        SZDTUSIntradayVolumeSnapshot.trade_date.in_(dates),
                    ).all()
                }
                for trade_date in dates:
                    for minute_label in VOLUME_BASELINE_MINUTES:
                        volume = cumulative.get((trade_date, minute_label))
                        if volume is None:
                            continue
                        snapshot = existing.get((trade_date, minute_label))
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
    """当前累计分钟量 ÷ 前 20 个交易日同一分钟累计量均值。

    历史每分钟基准由盘前任务持久化，策略只写当天实际触发分钟；服务重启不会丢失
    基准，且调整 cron 后可直接复用任意分钟的历史数据。
    """
    current = now or datetime.now(NEW_YORK)
    current = current.replace(tzinfo=NEW_YORK) if current.tzinfo is None else current.astimezone(NEW_YORK)
    minute_label = current.strftime("%H:%M")
    trade_date = current.date()
    service = LongPortService.get_instance(_longport_account_id(account_id))
    metrics: Dict[str, Dict[str, Any]] = {}
    for code in dict.fromkeys(str(item).upper() for item in codes if str(item).strip()):
        symbol = _symbol(code)
        try:
            # 每次只取截至当前时点的一段分钟线；无需为量比再逐分钟调用行情接口。
            elapsed = max(1, (current.hour * 60 + current.minute) - SESSION_OPEN_MINUTE)
            minutes = service.get_candlesticks(symbol, elapsed + 5, period="1m", adjust_type="none") or []
            cumulative_volume = sum(
                float(item.get("volume") or 0) for item in minutes
                if (
                    (timestamp := _as_new_york(item.get("timestamp"))) is not None
                    and timestamp.date() == trade_date
                    and timestamp.strftime("%H:%M") in VOLUME_BASELINE_MINUTE_SET
                    and timestamp <= current
                )
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

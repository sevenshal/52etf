"""选股系统模拟盘：信号日收盘出单，下一交易日开盘撮合，逐日收盘盯市。

撮合规则（只有日线时最接近真实的口径）：

- 买单只在信号日之后的第一个交易日开盘尝试一次：停牌、开盘涨停或资金不足一手就撤单；
- 卖单从信号日之后的第一个交易日起每天开盘尝试，停牌或开盘跌停就顺延；
- 同一天先卖后买，卖出回笼的资金当天可用；
- 费用：买卖佣金、卖出印花税按配置；股数按 100 股整手；
- 分红送转按复权因子折算进市值和卖出金额（相当于红利再投），除权日不会出现假的跳水。

没有涨跌停价数据，涨跌停按板块规则近似：主板 10%、创业板/科创板 20%、北交所 30%（ST 已在股票池外）。

账户只往前推进：已经结算到某个交易日后，再按更早的日期计算只出信号、不动模拟盘。
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from ...database import (
    SessionLocal,
    StockSystemPaperAccount,
    StockSystemPaperNav,
    StockSystemPaperOrder,
    StockSystemPaperPosition,
)
from ..duckdb_analytics import connect_analytics_db, duckdb_table_exists

logger = logging.getLogger(__name__)

LOT_SIZE = 100
LIMIT_TOLERANCE = 0.002
ORDER_PENDING = "pending"
ORDER_FILLED = "filled"
ORDER_CANCELLED = "cancelled"
SIDE_BUY = "buy"
SIDE_SELL = "sell"

_POSITION_FIELDS = (
    "ts_code", "name", "sector_code", "sector_name", "quantity", "entry_date", "entry_price",
    "entry_adj_factor", "cost", "stop_pct", "entry_reason", "last_price", "last_adj_factor", "market_value",
)
_ORDER_FIELDS = (
    "id", "signal_date", "ts_code", "name", "side", "status", "target_weight_pct", "budget", "quantity",
    "stop_pct", "sector_code", "sector_name", "reason", "exec_date", "fill_price", "fill_adj_factor",
    "amount", "fee", "realized_pnl", "message", "created_at",
)


def price_limit_pct(ts_code: str) -> float:
    code, _, exchange = str(ts_code or "").partition(".")
    if exchange == "BJ" or code.startswith(("4", "8", "92")):
        return 0.30
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


def _row_dict(row: Any, fields: Sequence[str]) -> Dict[str, Any]:
    return {field: getattr(row, field) for field in fields}


def load_book() -> Dict[str, Any]:
    """读出账户、持仓、未成交订单的普通 dict 快照（短事务）。"""
    with SessionLocal() as db:
        account = db.query(StockSystemPaperAccount).order_by(StockSystemPaperAccount.id).first()
        account_dict = (
            {
                "id": account.id,
                "initial_capital": account.initial_capital,
                "cash": account.cash,
                "started_on": account.started_on,
                "last_trade_date": account.last_trade_date,
            }
            if account
            else None
        )
        positions = {
            row.ts_code: _row_dict(row, _POSITION_FIELDS)
            for row in db.query(StockSystemPaperPosition).all()
        }
        pending = [
            _row_dict(row, _ORDER_FIELDS)
            for row in db.query(StockSystemPaperOrder)
            .filter(StockSystemPaperOrder.status == ORDER_PENDING)
            .order_by(StockSystemPaperOrder.id)
            .all()
        ]
    return {"account": account_dict, "positions": positions, "pending": pending}


def adj_ratio(position: Mapping[str, Any]) -> float:
    entry = position.get("entry_adj_factor")
    latest = position.get("last_adj_factor")
    if entry and latest and entry > 0:
        return float(latest) / float(entry)
    return 1.0


def position_return_pct(position: Mapping[str, Any]) -> Optional[float]:
    """持仓自买入以来的涨跌幅（复权口径，不含费用）。"""
    entry_price = position.get("entry_price")
    last_price = position.get("last_price")
    if not entry_price or not last_price:
        return None
    return (float(last_price) * adj_ratio(position) / float(entry_price) - 1.0) * 100.0


def book_nav(book: Mapping[str, Any]) -> float:
    return float(book["account"]["cash"]) + sum(float(p.get("market_value") or 0.0) for p in book["positions"].values())


def _trading_days(connection, after: date, until: date) -> List[date]:
    rows = connection.execute(
        """
        SELECT DISTINCT trade_date FROM a_stock_market_daily
        WHERE trade_date > ? AND trade_date <= ?
        ORDER BY trade_date
        """,
        [after, until],
    ).fetchall()
    return [row[0] if isinstance(row[0], date) else row[0].date() for row in rows]


def _load_bars(connection, symbols: Sequence[str], start: date, end: date) -> Dict[str, Dict[date, Dict[str, Any]]]:
    """原始（不复权）开收盘价、昨收和复权因子。"""
    if not symbols:
        return {}
    placeholders = ", ".join("?" for _ in symbols)
    has_adj = duckdb_table_exists(connection, "a_stock_adj_factor")
    adj_select = "f.adj_factor" if has_adj else "NULL AS adj_factor"
    adj_join = (
        "LEFT JOIN a_stock_adj_factor f ON f.ts_code = m.ts_code AND f.trade_date = m.trade_date"
        if has_adj
        else ""
    )
    rows = connection.execute(
        f"""
        SELECT m.ts_code, m.trade_date, m.open, m.close, m.pre_close, {adj_select}
        FROM a_stock_market_daily m
        {adj_join}
        WHERE m.ts_code IN ({placeholders}) AND m.trade_date BETWEEN ? AND ?
        """,
        [*symbols, start, end],
    ).fetchall()
    bars: Dict[str, Dict[date, Dict[str, Any]]] = {}
    for ts_code, trade_date, open_price, close, pre_close, adj_factor in rows:
        day = trade_date if isinstance(trade_date, date) else trade_date.date()
        bars.setdefault(str(ts_code), {})[day] = {
            "open": float(open_price) if open_price is not None else None,
            "close": float(close) if close is not None else None,
            "pre_close": float(pre_close) if pre_close is not None else None,
            "adj_factor": float(adj_factor) if adj_factor is not None and math.isfinite(adj_factor) else None,
        }
    return bars


def settle(
    book: Dict[str, Any],
    trade_date: date,
    paper_config: Mapping[str, Any],
    *,
    connect: Callable[[], Any] = connect_analytics_db,
) -> Dict[str, Any]:
    """把账户从上次结算日推进到 trade_date：逐个交易日先撮合（先卖后买）再按收盘盯市。原地修改 book。"""
    account = book["account"]
    connection = connect()
    try:
        days = _trading_days(connection, account["last_trade_date"], trade_date)
        symbols = sorted(set(book["positions"]) | {order["ts_code"] for order in book["pending"]})
        bars = _load_bars(connection, symbols, days[0], days[-1]) if days and symbols else {}
    finally:
        connection.close()

    commission = float(paper_config["commission_pct"]) / 100.0
    stamp_tax = float(paper_config["stamp_tax_pct"]) / 100.0
    events: List[Dict[str, Any]] = []
    navs: List[Dict[str, Any]] = []

    def finish(order: Dict[str, Any], status: str, day: date, message: Optional[str] = None, **fields) -> None:
        order.update(status=status, exec_date=day, message=message, **fields)
        events.append({"date": day.isoformat(), "ts_code": order["ts_code"], "name": order.get("name"),
                       "side": order["side"], "status": status, "message": message, "reason": order.get("reason"),
                       "price": fields.get("fill_price"), **fields})

    for day in days:
        for order in [o for o in book["pending"] if o["status"] == ORDER_PENDING and o["side"] == SIDE_SELL and o["signal_date"] < day]:
            ts_code = order["ts_code"]
            position = book["positions"].get(ts_code)
            if position is None:
                finish(order, ORDER_CANCELLED, day, "持仓已不存在")
                continue
            bar = bars.get(ts_code, {}).get(day)
            if not bar or not bar["open"]:
                order["message"] = f"{day} 停牌，顺延"
                continue
            if bar["pre_close"] and bar["open"] <= bar["pre_close"] * (1 - price_limit_pct(ts_code)) * (1 + LIMIT_TOLERANCE):
                order["message"] = f"{day} 开盘跌停，顺延"
                continue
            if bar["adj_factor"]:
                position["last_adj_factor"] = bar["adj_factor"]
            # fraction < 1 是减仓（回测里的目标权重再平衡用），按整手向下取
            quantity = position["quantity"]
            fraction = order.get("fraction")
            if fraction is not None and fraction < 0.999:
                quantity = math.floor(position["quantity"] * fraction / LOT_SIZE) * LOT_SIZE
                if quantity <= 0:
                    finish(order, ORDER_CANCELLED, day, "减仓不足一手")
                    continue
            share = min(1.0, quantity / position["quantity"])
            value = quantity * bar["open"] * adj_ratio(position)
            fee = value * (commission + stamp_tax)
            cost = float(position["cost"]) * share
            account["cash"] += value - fee
            finish(
                order, ORDER_FILLED, day,
                fill_price=bar["open"], fill_adj_factor=bar["adj_factor"], quantity=quantity,
                amount=value, fee=fee, realized_pnl=value - fee - cost,
                entry_date=position["entry_date"], entry_price=position["entry_price"], cost=cost,
            )
            if share >= 0.999999:
                del book["positions"][ts_code]
            else:
                position["quantity"] -= quantity
                position["cost"] = float(position["cost"]) - cost

        for order in [o for o in book["pending"] if o["status"] == ORDER_PENDING and o["side"] == SIDE_BUY and o["signal_date"] < day]:
            ts_code = order["ts_code"]
            bar = bars.get(ts_code, {}).get(day)
            existing = book["positions"].get(ts_code)
            if existing is not None and not order.get("allow_add"):
                finish(order, ORDER_CANCELLED, day, "已经持有")
                continue
            if not bar or not bar["open"]:
                finish(order, ORDER_CANCELLED, day, f"{day} 停牌，未成交")
                continue
            if bar["pre_close"] and bar["open"] >= bar["pre_close"] * (1 + price_limit_pct(ts_code)) * (1 - LIMIT_TOLERANCE):
                finish(order, ORDER_CANCELLED, day, f"{day} 开盘涨停，未成交")
                continue
            spend = min(float(order.get("budget") or 0.0), float(account["cash"]))
            quantity = int(spend / (bar["open"] * (1 + commission)) // LOT_SIZE) * LOT_SIZE
            if quantity <= 0:
                finish(order, ORDER_CANCELLED, day, "资金不足一手")
                continue
            amount = quantity * bar["open"]
            fee = amount * commission
            account["cash"] -= amount + fee
            if existing is not None:
                # 加仓：先把已有持仓折算到当天的股本口径（期间有送转时股数随复权因子变化），再加权平均成本价
                ratio = (
                    bar["adj_factor"] / existing["entry_adj_factor"]
                    if bar["adj_factor"] and existing.get("entry_adj_factor")
                    else 1.0
                )
                held = existing["quantity"] * ratio
                total = held + quantity
                existing.update(
                    quantity=total,
                    entry_price=(held * existing["entry_price"] / ratio + quantity * bar["open"]) / total,
                    entry_adj_factor=bar["adj_factor"] or existing.get("entry_adj_factor"),
                    cost=float(existing["cost"]) + amount + fee,
                    last_price=bar["open"],
                    last_adj_factor=bar["adj_factor"] or existing.get("last_adj_factor"),
                    market_value=total * bar["open"],
                )
                finish(order, ORDER_FILLED, day, fill_price=bar["open"], fill_adj_factor=bar["adj_factor"],
                       quantity=quantity, amount=amount, fee=fee)
                continue
            book["positions"][ts_code] = {
                "ts_code": ts_code,
                "name": order.get("name"),
                "sector_code": order.get("sector_code"),
                "sector_name": order.get("sector_name"),
                "quantity": quantity,
                "entry_date": day,
                "entry_price": bar["open"],
                "entry_adj_factor": bar["adj_factor"],
                "cost": amount + fee,
                "stop_pct": order.get("stop_pct"),
                "entry_reason": order.get("reason"),
                "last_price": bar["open"],
                "last_adj_factor": bar["adj_factor"],
                "market_value": amount,
            }
            finish(order, ORDER_FILLED, day, fill_price=bar["open"], fill_adj_factor=bar["adj_factor"],
                   quantity=quantity, amount=amount, fee=fee)

        market_value = 0.0
        for position in book["positions"].values():
            bar = bars.get(position["ts_code"], {}).get(day)
            if bar and bar["close"]:
                position["last_price"] = bar["close"]
                if bar["adj_factor"]:
                    position["last_adj_factor"] = bar["adj_factor"]
            position["market_value"] = position["quantity"] * float(position["last_price"] or 0.0) * adj_ratio(position)
            market_value += position["market_value"]
        navs.append({
            "trade_date": day,
            "cash": account["cash"],
            "market_value": market_value,
            "nav": account["cash"] + market_value,
            "positions": len(book["positions"]),
        })

    if days:
        account["last_trade_date"] = days[-1]
    return {"days": [day.isoformat() for day in days], "events": events, "navs": navs}


def save_book(
    book: Mapping[str, Any],
    *,
    new_orders: Sequence[Mapping[str, Any]] = (),
    replace_signal_date: Optional[date] = None,
    navs: Sequence[Mapping[str, Any]] = (),
) -> None:
    """一次事务写回账户、持仓（整表重写）、已有订单的状态、新订单和净值。

    ``replace_signal_date``：先删掉该信号日还没成交的旧订单——同一交易日重跑时覆盖当天的出单。
    """
    now = datetime.now()
    account = book["account"]
    with SessionLocal() as db:
        row = db.get(StockSystemPaperAccount, account["id"]) if account.get("id") else None
        if row is None:
            row = StockSystemPaperAccount(created_at=now)
            db.add(row)
        row.initial_capital = account["initial_capital"]
        row.cash = account["cash"]
        row.started_on = account.get("started_on")
        row.last_trade_date = account.get("last_trade_date")
        row.updated_at = now

        db.query(StockSystemPaperPosition).delete()
        for position in book["positions"].values():
            db.add(StockSystemPaperPosition(**{field: position.get(field) for field in _POSITION_FIELDS}, updated_at=now))

        for order in book["pending"]:
            if not order.get("id"):
                continue
            stored = db.get(StockSystemPaperOrder, order["id"])
            if stored is None:
                continue
            for field in _ORDER_FIELDS:
                if field not in ("id", "created_at"):
                    setattr(stored, field, order.get(field))
            stored.updated_at = now
        if replace_signal_date is not None:
            db.query(StockSystemPaperOrder).filter(
                StockSystemPaperOrder.signal_date == replace_signal_date,
                StockSystemPaperOrder.status == ORDER_PENDING,
            ).delete()
        for order in new_orders:
            db.add(StockSystemPaperOrder(
                **{field: order.get(field) for field in _ORDER_FIELDS if field not in ("id", "created_at")},
                created_at=now,
                updated_at=now,
            ))

        for nav in navs:
            stored = db.get(StockSystemPaperNav, nav["trade_date"])
            if stored is None:
                stored = StockSystemPaperNav(trade_date=nav["trade_date"], created_at=now)
                db.add(stored)
            stored.cash = nav["cash"]
            stored.market_value = nav["market_value"]
            stored.nav = nav["nav"]
            stored.positions = nav["positions"]
        db.commit()


def reset_paper(initial_capital: float) -> None:
    """清空模拟盘（持仓、订单、净值），用新的初始资金重建账户；下一次计算从当天开始。"""
    now = datetime.now()
    with SessionLocal() as db:
        db.query(StockSystemPaperPosition).delete()
        db.query(StockSystemPaperOrder).delete()
        db.query(StockSystemPaperNav).delete()
        db.query(StockSystemPaperAccount).delete()
        db.add(StockSystemPaperAccount(
            initial_capital=float(initial_capital), cash=float(initial_capital),
            started_on=None, last_trade_date=None, created_at=now, updated_at=now,
        ))
        db.commit()


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def load_paper_overview(order_limit: int = 200) -> Dict[str, Any]:
    """页面用：账户、持仓（含收益、权重）、净值曲线、最近订单。"""
    with SessionLocal() as db:
        account = db.query(StockSystemPaperAccount).order_by(StockSystemPaperAccount.id).first()
        if account is None:
            return {"account": None, "positions": [], "navs": [], "orders": [], "stats": {}}
        account_dict = {
            "initial_capital": account.initial_capital,
            "cash": account.cash,
            "started_on": _iso(account.started_on),
            "last_trade_date": _iso(account.last_trade_date),
        }
        positions = [_row_dict(row, _POSITION_FIELDS) for row in db.query(StockSystemPaperPosition).all()]
        navs = [
            {"trade_date": _iso(row.trade_date), "cash": row.cash, "market_value": row.market_value,
             "nav": row.nav, "positions": row.positions}
            for row in db.query(StockSystemPaperNav).order_by(StockSystemPaperNav.trade_date).all()
        ]
        orders = [
            {key: _iso(value) for key, value in _row_dict(row, _ORDER_FIELDS).items()}
            for row in db.query(StockSystemPaperOrder)
            .order_by(StockSystemPaperOrder.signal_date.desc(), StockSystemPaperOrder.id.desc())
            .limit(int(order_limit))
            .all()
        ]
    market_value = sum(float(p.get("market_value") or 0.0) for p in positions)
    nav = float(account_dict["cash"]) + market_value
    for position in positions:
        position["return_pct"] = position_return_pct(position)
        position["weight_pct"] = float(position.get("market_value") or 0.0) / nav * 100.0 if nav > 0 else None
        for key in ("entry_date",):
            position[key] = _iso(position[key])
    peak, max_drawdown = 0.0, 0.0
    for point in navs:
        peak = max(peak, point["nav"])
        if peak > 0:
            max_drawdown = min(max_drawdown, point["nav"] / peak - 1.0)
    initial = float(account_dict["initial_capital"])
    return {
        "account": {**account_dict, "market_value": market_value, "nav": nav},
        "positions": sorted(positions, key=lambda p: -(p.get("market_value") or 0.0)),
        "navs": navs,
        "orders": orders,
        "stats": {
            "total_return_pct": (nav / initial - 1.0) * 100.0 if initial > 0 else None,
            "max_drawdown_pct": max_drawdown * 100.0,
            "invested_pct": market_value / nav * 100.0 if nav > 0 else None,
            "position_count": len(positions),
            "pending_orders": sum(1 for order in orders if order["status"] == ORDER_PENDING),
        },
    }

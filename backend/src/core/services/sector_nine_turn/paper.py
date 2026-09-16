"""板块九转策略的模拟盘账本：信号日收盘出单，下一交易日开盘撮合。

撮合本身直接复用选股系统的 ``stock_system.paper.settle``——那是一份只操作普通 dict 的
纯函数（停牌顺延、开盘涨跌停不成交、整手、佣金/印花税、分红送转按复权因子折算），
两套策略必须用同一套成交规则，不能各写一份近似实现。这里只负责本策略自己的表：
账户、持仓、订单、净值。

持仓比选股系统多两个字段：``high9_armed`` / ``high9_date``——卖出规则要求"买入后先出现
高 9"，这个状态要跨交易日记住。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Mapping, Optional, Sequence

from ...database import (
    SectorNineTurnPaperAccount,
    SectorNineTurnPaperNav,
    SectorNineTurnPaperOrder,
    SectorNineTurnPaperPosition,
    SessionLocal,
)
from ..stock_system.paper import (  # noqa: F401  (统一从这里再导出，调用方不用两头 import)
    ORDER_CANCELLED,
    ORDER_FILLED,
    ORDER_PENDING,
    SIDE_BUY,
    SIDE_SELL,
    adj_ratio,
    book_nav,
    position_return_pct,
    price_limit_pct,
    settle,
)

_POSITION_FIELDS = (
    "ts_code", "name", "sector_code", "sector_name", "quantity", "entry_date", "entry_price",
    "entry_adj_factor", "cost", "entry_reason", "high9_armed", "high9_date",
    "last_price", "last_adj_factor", "market_value",
)
_ORDER_FIELDS = (
    "id", "signal_date", "ts_code", "name", "side", "status", "budget", "quantity",
    "sector_code", "sector_name", "reason", "exec_date", "fill_price", "fill_adj_factor",
    "amount", "fee", "realized_pnl", "message", "created_at",
)


def _row_dict(row: Any, fields: Sequence[str]) -> Dict[str, Any]:
    return {field: getattr(row, field) for field in fields}


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def load_book() -> Dict[str, Any]:
    """读出账户、持仓、未成交订单的普通 dict 快照（短事务）。"""
    with SessionLocal() as db:
        account = db.query(SectorNineTurnPaperAccount).order_by(SectorNineTurnPaperAccount.id).first()
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
            for row in db.query(SectorNineTurnPaperPosition).all()
        }
        pending = [
            _row_dict(row, _ORDER_FIELDS)
            for row in db.query(SectorNineTurnPaperOrder)
            .filter(SectorNineTurnPaperOrder.status == ORDER_PENDING)
            .order_by(SectorNineTurnPaperOrder.id)
            .all()
        ]
    return {"account": account_dict, "positions": positions, "pending": pending}


def save_book(book: Mapping[str, Any], *, new_orders: Sequence[Mapping[str, Any]] = (),
              replace_signal_date: Optional[date] = None,
              navs: Sequence[Mapping[str, Any]] = ()) -> None:
    """一次事务写回账户、持仓（整表重写）、已有订单状态、新订单和净值。

    ``replace_signal_date``：先删掉该信号日还没成交的旧订单——同一交易日重跑时覆盖当天出单。
    """
    now = datetime.now()
    account = book["account"]
    with SessionLocal() as db:
        row = db.get(SectorNineTurnPaperAccount, account["id"]) if account.get("id") else None
        if row is None:
            row = SectorNineTurnPaperAccount(created_at=now)
            db.add(row)
        row.initial_capital = account["initial_capital"]
        row.cash = account["cash"]
        row.started_on = account.get("started_on")
        row.last_trade_date = account.get("last_trade_date")
        row.updated_at = now

        db.query(SectorNineTurnPaperPosition).delete()
        for position in book["positions"].values():
            values = {field: position.get(field) for field in _POSITION_FIELDS}
            values["high9_armed"] = bool(values.get("high9_armed"))
            db.add(SectorNineTurnPaperPosition(**values, updated_at=now))

        for order in book["pending"]:
            if not order.get("id"):
                continue
            stored = db.get(SectorNineTurnPaperOrder, order["id"])
            if stored is None:
                continue
            for field in _ORDER_FIELDS:
                if field not in ("id", "created_at"):
                    setattr(stored, field, order.get(field))
            stored.updated_at = now
        if replace_signal_date is not None:
            db.query(SectorNineTurnPaperOrder).filter(
                SectorNineTurnPaperOrder.signal_date == replace_signal_date,
                SectorNineTurnPaperOrder.status == ORDER_PENDING,
            ).delete()
        for order in new_orders:
            db.add(SectorNineTurnPaperOrder(
                **{field: order.get(field) for field in _ORDER_FIELDS if field not in ("id", "created_at")},
                created_at=now,
                updated_at=now,
            ))

        for nav in navs:
            stored = db.get(SectorNineTurnPaperNav, nav["trade_date"])
            if stored is None:
                stored = SectorNineTurnPaperNav(trade_date=nav["trade_date"], created_at=now)
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
        db.query(SectorNineTurnPaperPosition).delete()
        db.query(SectorNineTurnPaperOrder).delete()
        db.query(SectorNineTurnPaperNav).delete()
        db.query(SectorNineTurnPaperAccount).delete()
        db.add(SectorNineTurnPaperAccount(
            initial_capital=float(initial_capital), cash=float(initial_capital),
            started_on=None, last_trade_date=None, created_at=now, updated_at=now,
        ))
        db.commit()


def load_paper_overview(order_limit: int = 200) -> Dict[str, Any]:
    """页面用：账户、持仓（含收益、权重）、净值曲线、最近订单。"""
    with SessionLocal() as db:
        account = db.query(SectorNineTurnPaperAccount).order_by(SectorNineTurnPaperAccount.id).first()
        if account is None:
            return {"account": None, "positions": [], "navs": [], "orders": [], "stats": {}}
        account_dict = {
            "initial_capital": account.initial_capital,
            "cash": account.cash,
            "started_on": _iso(account.started_on),
            "last_trade_date": _iso(account.last_trade_date),
        }
        positions = [_row_dict(row, _POSITION_FIELDS) for row in db.query(SectorNineTurnPaperPosition).all()]
        navs = [
            {"trade_date": _iso(row.trade_date), "cash": row.cash, "market_value": row.market_value,
             "nav": row.nav, "positions": row.positions}
            for row in db.query(SectorNineTurnPaperNav).order_by(SectorNineTurnPaperNav.trade_date).all()
        ]
        orders = [
            {key: _iso(value) for key, value in _row_dict(row, _ORDER_FIELDS).items()}
            for row in db.query(SectorNineTurnPaperOrder)
            .order_by(SectorNineTurnPaperOrder.signal_date.desc(), SectorNineTurnPaperOrder.id.desc())
            .limit(int(order_limit))
            .all()
        ]
    market_value = sum(float(p.get("market_value") or 0.0) for p in positions)
    nav = float(account_dict["cash"]) + market_value
    for position in positions:
        position["return_pct"] = position_return_pct(position)
        position["weight_pct"] = float(position.get("market_value") or 0.0) / nav * 100.0 if nav > 0 else None
        position["entry_date"] = _iso(position["entry_date"])
        position["high9_date"] = _iso(position["high9_date"])
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
        },
    }

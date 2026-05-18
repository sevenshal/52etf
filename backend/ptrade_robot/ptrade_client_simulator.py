#!/usr/bin/env python3
"""Local runner for ptrade_client.py with mocked PTrade APIs.

Example:
    python 52etf_api/ptrade_robot/ptrade_client_simulator.py --host localhost:8000
    python 52etf_api/ptrade_robot/ptrade_client_simulator.py --host localhost:8000 --empty-positions
    python 52etf_api/ptrade_robot/ptrade_client_simulator.py --host localhost:8000 --quote-provider longport

Create an enabled external trading account in the web UI with the printed
identifier before starting the connection. The backend owns the account name.
"""

import argparse
import asyncio
import importlib.util
import json
import logging
import math
import os
import sys
import time as time_module
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


DEMO_POSITIONS = [
    {"symbol": "510300.SS", "quantity": 10000, "cost_price": 3.820},
    {"symbol": "510500.SS", "quantity": 8000, "cost_price": 5.740},
    {"symbol": "159915.SZ", "quantity": 12000, "cost_price": 1.910},
]

DEFAULT_PRICE_BOOK = {
    "510300.SS": 3.930,
    "510500.SS": 5.880,
    "159915.SZ": 1.980,
    "512100.SS": 0.860,
    "512880.SS": 0.990,
    "513100.SS": 1.420,
    "588000.SS": 0.910,
    "159920.SZ": 1.120,
}


def load_ptrade_client():
    client_path = Path(__file__).with_name("ptrade_client.py")
    spec = importlib.util.spec_from_file_location("ptrade_client", str(client_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def env_bool(name, default=False):
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def parse_positions(raw, demo_positions=False, empty_positions=False):
    if empty_positions:
        return []
    if demo_positions:
        return DEMO_POSITIONS
    if not raw:
        return DEMO_POSITIONS
    if raw.strip().lower() in ("none", "empty", "[]"):
        return []
    data = json.loads(raw)
    if isinstance(data, dict):
        result = []
        for symbol, item in data.items():
            if isinstance(item, dict):
                quantity = item.get("quantity", item.get("amount", 0))
                cost_price = item.get("cost_price", item.get("cost_basis", 0))
            else:
                quantity = item
                cost_price = 0
            result.append({"symbol": symbol, "quantity": quantity, "cost_price": cost_price})
        return result
    if isinstance(data, list):
        return data
    raise ValueError("positions JSON must be a list or object")


class SimLog:
    def __init__(self, logger):
        self.logger = logger

    def info(self, message, *args):
        self.logger.info(message, *args)

    def warning(self, message, *args):
        self.logger.warning(message, *args)

    def warn(self, message, *args):
        self.warning(message, *args)

    def error(self, message, *args):
        self.logger.error(message, *args)


class SimPosition:
    def __init__(self, sid, amount, cost_basis, enable_amount=None):
        self.sid = sid
        self.amount = int(amount)
        self.enable_amount = int(enable_amount if enable_amount is not None else amount)
        self.cost_basis = float(cost_basis or 0)
        self.last_sale_price = float(cost_basis or 0)
        self.market_value = 0.0
        self.profit = 0.0
        self.profit_ratio = 0.0

    def refresh(self, last_price):
        self.last_sale_price = float(last_price or 0)
        self.market_value = round(self.amount * self.last_sale_price, 2)
        self.profit = round((self.last_sale_price - self.cost_basis) * self.amount, 2)
        base_value = self.cost_basis * self.amount
        self.profit_ratio = round(self.profit / base_value, 6) if base_value else 0.0


class LongPortMarketDataProvider:
    """Use project LongPortService for simulator quotes/depth when available."""

    def __init__(self, client, account_id, logger):
        self.client = client
        self.account_id = account_id
        self.logger = logger
        self.service = None
        self.enabled = False
        self.name = "longport"
        self._missing_symbols = set()
        self._init_service()

    def _init_service(self):
        api_root = Path(__file__).resolve().parents[1]
        repo_root = api_root.parent
        for path in (str(api_root), str(repo_root)):
            if path not in sys.path:
                sys.path.insert(0, path)

        try:
            from src.core.services.longport import LongPortService

            service = LongPortService.get_instance(self.account_id)
            if not getattr(service, "initialized", False):
                raise RuntimeError("LongPortService 未初始化")
            self.service = service
            self.enabled = True
            self.logger.info("LongPort market data enabled: account=%s", self.account_id)
        except Exception as exc:
            self.enabled = False
            self.service = None
            self.logger.warning(
                "LongPort market data unavailable, falling back to local simulated quotes: %s",
                exc,
            )

    def _client_symbol(self, symbol):
        return str(self.client.convert_to_client_code(symbol) or symbol).upper()

    def _normalize_longport_symbol(self, symbol):
        if not symbol:
            return None
        parts = str(symbol).strip().upper().split(".")
        if len(parts) != 2:
            return str(symbol).strip().upper()
        first, second = parts
        if first == "SS":
            first = "SH"
        if second == "SS":
            second = "SH"
        return "%s.%s" % (first, second)

    def _longport_symbol_variants(self, client_symbol):
        client_symbol = self._client_symbol(client_symbol)
        variants = []

        parts = client_symbol.split(".")
        if len(parts) == 2:
            code = parts[0].upper()
            market = parts[1].upper()
            longport_market = "SH" if market in ("SS", "SH") else market
            variants.append("%s.%s" % (code, longport_market))
            variants.append("%s.%s" % (longport_market, code))

        api_symbol = self.client.convert_to_api_code(client_symbol)
        if api_symbol:
            variants.append(self._normalize_longport_symbol(api_symbol))
            api_parts = str(api_symbol).split(".")
            if len(api_parts) == 2 and api_parts[0].upper() in ("SH", "SS", "SZ", "BJ"):
                market = "SH" if api_parts[0].upper() == "SS" else api_parts[0].upper()
                variants.append("%s.%s" % (api_parts[1].upper(), market))

        result = []
        seen = set()
        for item in variants:
            item = self._normalize_longport_symbol(item)
            if item and item not in seen:
                seen.add(item)
                result.append(item)
        return result

    def _get_quote(self, longport_symbol):
        quotes = self.service.get_quote_batch([longport_symbol]) or []
        if not quotes:
            return None
        return quotes[0] or None

    def _get_depth(self, longport_symbol):
        return self.service.get_depth(longport_symbol) or {}

    def _float_or_none(self, value):
        try:
            if value is None or value == "":
                return None
            return float(value)
        except Exception:
            return None

    def _int_or_zero(self, value):
        try:
            if value is None or value == "":
                return 0
            return int(float(value))
        except Exception:
            return 0

    def _format_timestamp(self, value):
        if value is None:
            return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if hasattr(value, "strftime"):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return str(value)

    def _depth_group(self, levels):
        group = {}
        for index, item in enumerate(levels or [], 1):
            if not isinstance(item, dict):
                continue
            price = self._float_or_none(item.get("price"))
            volume = self._int_or_zero(item.get("volume"))
            if price is None or price <= 0 or volume <= 0:
                continue
            position = self._int_or_zero(item.get("position")) or index
            if position <= 0:
                position = index
            if position > 5:
                continue
            group[position] = [price, volume]
        return group

    def _synthetic_depth(self, last_price, side):
        step = max(0.001, round(float(last_price) * 0.0006, 3))
        group = {}
        for level in range(1, 6):
            price = float(last_price) + step * level if side == "ask" else float(last_price) - step * level
            group[level] = [round(max(0.001, price), 3), 20000 + level * 5000]
        return group

    def _build_snapshot(self, client_symbol, quote, depth):
        quote = quote or {}
        depth = depth or {}
        last_price = self._float_or_none(quote.get("price"))
        bid_grp = self._depth_group(depth.get("bid"))
        offer_grp = self._depth_group(depth.get("ask"))

        best_bid = bid_grp.get(1, [None, None])[0]
        best_ask = offer_grp.get(1, [None, None])[0]
        if last_price is None:
            if best_bid and best_ask:
                last_price = round((float(best_bid) + float(best_ask)) / 2, 3)
            elif best_bid:
                last_price = float(best_bid)
            elif best_ask:
                last_price = float(best_ask)

        if last_price is None or last_price <= 0:
            return None

        if not bid_grp:
            bid_grp = self._synthetic_depth(last_price, "bid")
        if not offer_grp:
            offer_grp = self._synthetic_depth(last_price, "ask")

        return {
            "prod_code": client_symbol,
            "last_px": float(last_price),
            "bid_grp": bid_grp,
            "offer_grp": offer_grp,
            "trade_status": "TRADE",
            "business_amount": self._int_or_zero(quote.get("volume")),
            "business_balance": self._float_or_none(quote.get("turnover")) or 0.0,
            "hsTimeStamp": self._format_timestamp(quote.get("timestamp")),
            "market_data_source": "longport",
            "longport_symbol": quote.get("symbol") or depth.get("symbol"),
        }

    def get_snapshot(self, client_symbol):
        if not self.enabled or not self.service:
            return None

        client_symbol = self._client_symbol(client_symbol)
        for longport_symbol in self._longport_symbol_variants(client_symbol):
            try:
                quote = self._get_quote(longport_symbol)
                depth = self._get_depth(longport_symbol)
                snapshot = self._build_snapshot(client_symbol, quote, depth)
                if snapshot:
                    snapshot["longport_symbol"] = snapshot.get("longport_symbol") or longport_symbol
                    return snapshot
            except Exception as exc:
                self.logger.debug("LongPort quote failed for %s: %s", longport_symbol, exc)

        if client_symbol not in self._missing_symbols:
            self._missing_symbols.add(client_symbol)
            self.logger.warning("LongPort quote not found for %s, using local simulated quote", client_symbol)
        return None


class SimPortfolio:
    def __init__(self, cash):
        self.starting_cash = float(cash)
        self.cash = float(cash)
        self.positions = {}
        self.positions_value = 0.0
        self.portfolio_value = float(cash)
        self.returns = 0.0

    def update(self, price_getter):
        total_value = 0.0
        for pos in list(self.positions.values()):
            price = price_getter(pos.sid)
            pos.refresh(price)
            total_value += pos.market_value
        self.positions_value = round(total_value, 2)
        self.portfolio_value = round(self.cash + self.positions_value, 2)
        if self.starting_cash:
            self.returns = round((self.portfolio_value - self.starting_cash) / self.starting_cash, 6)


class SimContext:
    def __init__(self, cash):
        self.portfolio = SimPortfolio(cash)
        self.current_dt = datetime.now()


class SimBroker:
    def __init__(self, client, context, positions, logger, market_data_provider=None):
        self.client = client
        self.context = context
        self.logger = logger
        self.market_data_provider = market_data_provider
        self.orders = []
        self.pending_report_order_ids = []
        self.order_seq = 1
        self.tick = 0
        self.price_book = dict(DEFAULT_PRICE_BOOK)
        self._seed_positions(positions)
        self._refresh_portfolio()

    def _seed_positions(self, positions):
        for item in positions:
            symbol = self._client_symbol(item.get("symbol") or item.get("sid"))
            quantity = int(item.get("quantity", item.get("amount", 0)) or 0)
            if quantity == 0:
                continue
            cost_price = float(item.get("cost_price", item.get("cost_basis", 0)) or 0)
            if cost_price <= 0:
                cost_price = self._last_price(symbol)
            self.context.portfolio.positions[symbol] = SimPosition(symbol, quantity, cost_price)
            self.price_book.setdefault(symbol, cost_price)

    def _client_symbol(self, symbol):
        if not symbol:
            raise ValueError("symbol is required")
        converted = self.client.convert_to_client_code(symbol)
        return str(converted or symbol).upper()

    def _symbol_seed(self, symbol):
        return sum(ord(ch) for ch in str(symbol))

    def _last_price(self, client_symbol):
        client_symbol = self._client_symbol(client_symbol)
        if client_symbol not in self.price_book:
            digits = "".join(ch for ch in client_symbol if ch.isdigit())
            seed = int(digits[-3:] or "100")
            self.price_book[client_symbol] = round(0.8 + (seed % 700) / 100.0, 3)
        base = self.price_book[client_symbol]
        phase = (self.tick + self._symbol_seed(client_symbol) % 31) / 17.0
        return round(max(0.001, base * (1 + math.sin(phase) * 0.0012)), 3)

    def _depth_step(self, last_price):
        return max(0.001, round(last_price * 0.0006, 3))

    def _provider_snapshot(self, client_symbol):
        provider = self.market_data_provider
        if not provider:
            return None
        client_symbol = self._client_symbol(client_symbol)
        snapshot = provider.get_snapshot(client_symbol)
        if not snapshot:
            return None
        try:
            last_price = float(snapshot.get("last_px") or 0)
            if last_price > 0:
                self.price_book[client_symbol] = last_price
        except Exception:
            pass
        return snapshot

    def _market_price(self, client_symbol):
        snapshot = self._provider_snapshot(client_symbol)
        if snapshot:
            try:
                price = float(snapshot.get("last_px") or 0)
                if price > 0:
                    return price
            except Exception:
                pass
        return self._last_price(client_symbol)

    def _snapshot_for(self, client_symbol):
        client_symbol = self._client_symbol(client_symbol)
        provider_snapshot = self._provider_snapshot(client_symbol)
        if provider_snapshot:
            return provider_snapshot

        last_price = self._last_price(client_symbol)
        step = self._depth_step(last_price)
        seed = self._symbol_seed(client_symbol)
        bid_grp = {}
        offer_grp = {}
        for level in range(1, 6):
            volume = 20000 + ((seed + level * 7919) % 80) * 1000
            bid_grp[level] = [round(max(0.001, last_price - step * level), 3), volume]
            offer_grp[level] = [round(last_price + step * level, 3), volume]
        return {
            "prod_code": client_symbol,
            "last_px": last_price,
            "bid_grp": bid_grp,
            "offer_grp": offer_grp,
            "trade_status": "TRADE",
            "business_amount": 100000 + seed,
            "business_balance": round((100000 + seed) * last_price, 2),
            "hsTimeStamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _refresh_portfolio(self):
        self.context.current_dt = datetime.now()
        self.context.portfolio.update(self._market_price)

    def get_snapshot(self, security):
        self.tick += 1
        self.context.current_dt = datetime.now()
        if isinstance(security, (list, tuple, set)):
            return {self._client_symbol(symbol): self._snapshot_for(symbol) for symbol in security}
        return self._snapshot_for(security)

    def _best_fill_price(self, client_symbol, amount):
        snapshot = self._snapshot_for(client_symbol)
        side = "BUY" if amount > 0 else "SELL"
        group = snapshot["offer_grp"] if side == "BUY" else snapshot["bid_grp"]
        return float(group[1][0])

    def _next_order_id(self):
        order_id = "SIM%s%04d" % (datetime.now().strftime("%Y%m%d%H%M%S"), self.order_seq)
        self.order_seq += 1
        return order_id

    def _record_order(
        self,
        client_symbol,
        amount,
        fill_price,
        order_type,
        market_type=None,
        submitted_price=None,
        status="0",
        message="accepted",
    ):
        side = "BUY" if amount > 0 else "SELL"
        quantity = abs(int(amount))
        order_id = self._next_order_id()
        order = {
            "order_id": order_id,
            "entrust_no": order_id,
            "symbol": client_symbol,
            "amount": amount,
            "quantity": quantity,
            "price": submitted_price if submitted_price is not None else fill_price,
            "business_price": fill_price if status != "9" else None,
            "status": status,
            "entrust_bs": "1" if side == "BUY" else "2",
            "business_amount": quantity if status != "9" else 0,
            "entrust_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "order_type": order_type,
            "market_type": market_type,
            "message": message,
        }
        self.orders.append(order)
        return order_id

    def _submit_order(self, symbol, amount, fill_price, order_type, market_type=None, submitted_price=None):
        client_symbol = self._client_symbol(symbol)
        amount = int(amount)
        quantity = abs(amount)
        if amount == 0:
            return self._record_order(
                client_symbol,
                amount,
                fill_price,
                order_type,
                market_type=market_type,
                submitted_price=submitted_price,
                status="9",
                message="zero quantity",
            )

        portfolio = self.context.portfolio
        side = "BUY" if amount > 0 else "SELL"
        cost = round(quantity * float(fill_price), 2)

        if side == "BUY":
            if portfolio.cash < cost:
                return self._record_order(
                    client_symbol,
                    amount,
                    fill_price,
                    order_type,
                    market_type=market_type,
                    submitted_price=submitted_price,
                    status="9",
                    message="insufficient cash",
                )
            pos = portfolio.positions.get(client_symbol)
            if not pos:
                pos = SimPosition(client_symbol, 0, fill_price, 0)
                portfolio.positions[client_symbol] = pos
            old_quantity = pos.amount
            old_cost = pos.cost_basis
            pos.amount += quantity
            pos.enable_amount += quantity
            pos.cost_basis = round(
                ((old_quantity * old_cost) + cost) / pos.amount,
                4,
            ) if pos.amount else float(fill_price)
            portfolio.cash = round(portfolio.cash - cost, 2)
        else:
            pos = portfolio.positions.get(client_symbol)
            if not pos or pos.enable_amount < quantity:
                return self._record_order(
                    client_symbol,
                    amount,
                    fill_price,
                    order_type,
                    market_type=market_type,
                    submitted_price=submitted_price,
                    status="9",
                    message="insufficient position",
                )
            pos.amount -= quantity
            pos.enable_amount -= quantity
            portfolio.cash = round(portfolio.cash + cost, 2)
            if pos.amount <= 0:
                portfolio.positions.pop(client_symbol, None)

        order_id = self._record_order(
            client_symbol,
            amount,
            fill_price,
            order_type,
            market_type=market_type,
            submitted_price=submitted_price,
            status="0",
            message="filled",
        )
        self._refresh_portfolio()
        self.logger.info(
            "sim order filled: %s %s qty=%s price=%s id=%s",
            side,
            client_symbol,
            quantity,
            fill_price,
            order_id,
        )
        return order_id

    def order(self, symbol, amount, limit_price=None):
        price = float(limit_price if limit_price is not None else self._best_fill_price(symbol, amount))
        order_id = self._submit_order(symbol, amount, price, "LIMIT", submitted_price=price)
        self.emit_order_reports(order_id)
        return order_id

    def order_market(self, symbol, amount, market_type, limit_price=None):
        client_symbol = self._client_symbol(symbol)
        fill_price = self._best_fill_price(client_symbol, amount)
        if limit_price is not None:
            protection_price = float(limit_price)
            rejected = (amount > 0 and fill_price > protection_price) or (amount < 0 and fill_price < protection_price)
            if rejected:
                order_id = self._record_order(
                    client_symbol,
                    amount,
                    fill_price,
                    "MARKET",
                    market_type=market_type,
                    submitted_price=protection_price,
                    status="9",
                    message="protection price rejected",
                )
                self.emit_order_reports(order_id)
                return order_id
            submitted_price = protection_price
        else:
            submitted_price = None
        order_id = self._submit_order(
            client_symbol,
            amount,
            fill_price,
            "MARKET",
            market_type=market_type,
            submitted_price=submitted_price,
        )
        self.emit_order_reports(order_id)
        return order_id

    def cancel_order(self, order_id):
        matches = self.get_order(order_id)
        if not matches:
            raise ValueError("order not found")
        order_item = matches[0]
        if str(order_item.get("status")) == "0" and int(order_item.get("business_amount") or 0) >= int(order_item.get("quantity") or 0):
            raise ValueError("order already filled")
        order_item["status"] = "6"
        order_item["message"] = "canceled"
        self.emit_order_reports(order_id)
        return None

    def get_order(self, order_id):
        return [order for order in self.orders if str(order.get("order_id")) == str(order_id)]

    def get_all_orders(self):
        return list(self.orders)

    def _order_fee(self, order_item):
        quantity = int(order_item.get("business_amount") or order_item.get("quantity") or 0)
        price = float(order_item.get("business_price") or order_item.get("price") or 0)
        amount = round(quantity * price, 2)
        commission = round(max(amount * 0.00025, 5.0), 2) if amount > 0 else 0.0
        side = "BUY" if int(order_item.get("amount") or 0) > 0 else "SELL"
        stamp_tax = round(amount * 0.0005, 2) if side == "SELL" and amount > 0 else 0.0
        return amount, commission, stamp_tax, round(commission + stamp_tax, 2)

    def get_deliver(self, start_date, end_date):
        def normalize_date(value):
            text = str(value or "").replace("-", "")[:8]
            return text

        start = normalize_date(start_date)
        end = normalize_date(end_date or start_date)
        records = []
        for order_item in self.orders:
            if str(order_item.get("status")) == "9":
                continue
            if int(order_item.get("business_amount") or 0) <= 0:
                continue
            entrust_time = str(order_item.get("entrust_time") or "")
            trade_date = entrust_time[:10].replace("-", "") if entrust_time else datetime.now().strftime("%Y%m%d")
            if start and trade_date < start:
                continue
            if end and trade_date > end:
                continue
            amount, commission, stamp_tax, total_fee = self._order_fee(order_item)
            side = "BUY" if int(order_item.get("amount") or 0) > 0 else "SELL"
            records.append({
                "trade_date": trade_date,
                "business_date": trade_date,
                "order_id": order_item.get("order_id"),
                "entrust_no": order_item.get("entrust_no") or order_item.get("order_id"),
                "business_no": "%s-D" % order_item.get("order_id"),
                "symbol": order_item.get("symbol"),
                "side": side,
                "entrust_bs": "1" if side == "BUY" else "2",
                "business_amount": int(order_item.get("business_amount") or 0),
                "business_price": float(order_item.get("business_price") or order_item.get("price") or 0),
                "business_balance": amount,
                "commission": commission,
                "stamp_tax": stamp_tax,
                "total_fee": total_fee,
                "remark": "simulated deliver",
            })
        return records

    def get_positions(self):
        self._refresh_portfolio()
        return self.context.portfolio.positions

    def emit_order_reports(self, order_id):
        if order_id:
            self.pending_report_order_ids.append(order_id)


def build_parser(client):
    parser = argparse.ArgumentParser(description="Local PTrade WebSocket client simulator")
    parser.add_argument("--host", default=os.getenv("PTRADE_SIM_API_HOST", "localhost:8000"))
    parser.add_argument("--https", action="store_true", default=env_bool("PTRADE_SIM_USE_HTTPS", False))
    parser.add_argument("--account-id", default=os.getenv("PTRADE_SIM_ACCOUNT_ID", client.DEFAULT_ACCOUNT_ID))
    parser.add_argument("--identifier", default=os.getenv("PTRADE_SIM_IDENTIFIER", client.DEFAULT_IDENTIFIER))
    parser.add_argument("--cash", type=float, default=float(os.getenv("PTRADE_SIM_CASH", "1000000")))
    parser.add_argument(
        "--quote-provider",
        choices=("auto", "longport", "local"),
        default=os.getenv("PTRADE_SIM_QUOTE_PROVIDER", "auto"),
        help="market data provider for quotes/depth: auto, longport, or local",
    )
    parser.add_argument(
        "--longport-account-id",
        default=os.getenv("PTRADE_SIM_LONGPORT_ACCOUNT_ID", "LBPT10001248"),
        help="LongPort account id used only for simulated market data",
    )
    parser.add_argument("--positions-json", default=os.getenv("PTRADE_SIM_POSITIONS_JSON"))
    parser.add_argument("--demo-positions", action="store_true", default=env_bool("PTRADE_SIM_DEMO_POSITIONS", False))
    parser.add_argument("--empty-positions", action="store_true", default=env_bool("PTRADE_SIM_EMPTY_POSITIONS", False))
    parser.add_argument("--backtest", action="store_true", default=env_bool("PTRADE_SIM_BACKTEST", False))
    parser.add_argument("--heartbeat", type=int, default=int(os.getenv("PTRADE_SIM_HEARTBEAT", "20")))
    parser.add_argument("--reconnect-delay", type=float, default=float(os.getenv("PTRADE_SIM_RECONNECT_DELAY", "5")))
    parser.add_argument("--once", action="store_true", help="do not reconnect after the first disconnect")
    parser.add_argument("--self-test", action="store_true", help="run local command handlers without connecting")
    parser.add_argument("--log-level", default=os.getenv("PTRADE_SIM_LOG_LEVEL", "INFO"))
    return parser


def build_market_data_provider(client, args, logger):
    provider_mode = str(args.quote_provider or "auto").lower()
    if provider_mode == "local":
        logger.info("market data provider: local simulated quotes")
        return None

    provider = LongPortMarketDataProvider(client, args.longport_account_id, logger)
    if provider.enabled:
        return provider

    if provider_mode == "longport":
        logger.warning("requested LongPort quote provider is unavailable; simulator will use local quotes")
    return None


def configure_client(client, args):
    logger = logging.getLogger("ptrade-sim")
    positions = parse_positions(args.positions_json, args.demo_positions, args.empty_positions)
    context = SimContext(args.cash)
    market_data_provider = build_market_data_provider(client, args, logger)
    broker = SimBroker(client, context, positions, logger, market_data_provider=market_data_provider)

    client.log = SimLog(logging.getLogger("ptrade-client"))
    client.g = SimpleNamespace()
    client.is_trade = lambda: not args.backtest
    client.get_snapshot = broker.get_snapshot
    client.order = broker.order
    client.order_market = broker.order_market
    client.cancel_order = broker.cancel_order
    client.get_order = broker.get_order
    client.get_all_orders = broker.get_all_orders
    client.get_deliver = broker.get_deliver
    client.get_positions = broker.get_positions
    client.API_HOST = args.host
    client.USE_HTTPS = bool(args.https)
    client.DEFAULT_ACCOUNT_ID = args.account_id
    client.DEFAULT_IDENTIFIER = args.identifier
    client.HEARTBEAT_INTERVAL_SECONDS = args.heartbeat
    client.RECONNECT_DELAY_SECONDS = args.reconnect_delay
    client.time = time_module

    client.DISABLE_AUTO_WEBSOCKET = True
    client.initialize(context)
    client.g.sim_broker = broker
    return context, broker


def print_account_setup(client, broker):
    logger = logging.getLogger("ptrade-sim")
    logger.info("simulated account_id: %s", client.g.account_id)
    logger.info("simulated identifier: %s", client.g.external_account_identifier)
    logger.info("simulated starting positions: %s", len(broker.context.portfolio.positions))
    logger.info(
        "simulated market data provider: %s",
        getattr(getattr(broker, "market_data_provider", None), "name", "local"),
    )
    logger.info("backend WS target: %s", client.build_ws_url().split("?")[0])
    logger.info("backend resolves account name by account_id + identifier")
    logger.info("create an enabled external trading account with identifier=%r", client.g.external_account_identifier)


def run_self_test(client):
    commands = [
        ("get_snapshots", {"symbols": ["SH.510300", "SH.510500", "SZ.159915"]}),
        (
            "place_orders",
            {
                "orders": [
                    {"symbol": "SH.510300", "side": "BUY", "quantity": 1000, "order_type": "LIMIT", "price_level": 1},
                    {"symbol": "SH.510300", "side": "SELL", "quantity": 1000, "order_type": "MARKET", "market_type": 0},
                ]
            },
        ),
        ("get_positions", {}),
        ("get_assets", {}),
        ("get_today_orders", {}),
        ("get_deliver", {"start_date": datetime.now().strftime("%Y-%m-%d"), "end_date": datetime.now().strftime("%Y-%m-%d")}),
        ("get_account_snapshot", {}),
    ]
    for action, payload in commands:
        print("\n== %s ==" % action)
        result = client.execute_command(action, payload)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


async def send_secure(ws, client, payload):
    await ws.send(client.encrypt_message(payload))


async def flush_simulated_reports(ws, client):
    broker = getattr(client.g, "sim_broker", None)
    if not broker:
        return
    order_ids = list(getattr(broker, "pending_report_order_ids", []))
    broker.pending_report_order_ids = []
    for order_id in order_ids:
        matches = broker.get_order(order_id)
        if not matches:
            continue
        order_item = matches[0]
        current_dt = datetime.now()
        await send_secure(ws, client, {
            "type": "order_event",
            "orders": [client.normalize_order(order_item, current_dt)],
            "ts": datetime.now().isoformat(),
        })
        if str(order_item.get("status")) != "9" and int(order_item.get("business_amount") or 0) > 0:
            await send_secure(ws, client, {
                "type": "trade_event",
                "trades": [client.normalize_trade(order_item, current_dt)],
                "ts": datetime.now().isoformat(),
            })


async def handle_ws_message(ws, client, raw_message):
    logger = logging.getLogger("ptrade-sim")
    if isinstance(raw_message, bytes):
        raw_message = raw_message.decode("utf-8")
    try:
        message = client.decrypt_message(raw_message)
    except Exception as exc:
        logger.warning("ignored invalid secure message: %s", exc)
        return

    message_type = message.get("type")
    if message_type == "connected":
        logger.info("backend accepted account: %s", message.get("name"))
        return
    if message_type == "pong":
        logger.debug("heartbeat pong")
        return
    if message_type != "command":
        logger.info("ignored message from backend: %s", message)
        return

    request_id = message.get("id")
    action = message.get("action")
    payload = message.get("payload") or {}
    response = {
        "type": "result",
        "id": request_id,
        "ok": True,
        "data": {},
        "ts": datetime.now().isoformat(),
    }
    try:
        logger.info("executing command: %s id=%s", action, request_id)
        response["data"] = client.execute_command(action, payload)
    except Exception as exc:
        logger.exception("command failed: %s id=%s", action, request_id)
        response["ok"] = False
        response["error"] = str(exc)

    await send_secure(ws, client, response)
    if action in ("place_orders", "order.batch", "cancel_orders", "order.cancel"):
        await flush_simulated_reports(ws, client)


async def connect_once(client, args):
    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError("Install the websockets package to run the simulator") from exc

    logger = logging.getLogger("ptrade-sim")
    ws_url = client.build_ws_url()
    logger.info("connecting: %s", ws_url.split("?")[0])
    async with websockets.connect(ws_url, ping_interval=None, close_timeout=5) as ws:
        logger.info("websocket connected")
        while True:
            try:
                raw_message = await asyncio.wait_for(ws.recv(), timeout=args.heartbeat)
            except asyncio.TimeoutError:
                await send_secure(ws, client, {"type": "heartbeat", "ts": datetime.now().isoformat()})
                continue
            await handle_ws_message(ws, client, raw_message)


async def run_forever(client, args):
    logger = logging.getLogger("ptrade-sim")
    while True:
        try:
            await connect_once(client, args)
        except asyncio.CancelledError:
            raise
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            logger.error("websocket disconnected or failed: %s", exc)

        if args.once:
            return
        await asyncio.sleep(args.reconnect_delay)


def main(argv=None):
    client = load_ptrade_client()
    parser = build_parser(client)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    _, broker = configure_client(client, args)
    print_account_setup(client, broker)

    if args.self_test:
        run_self_test(client)
        return 0

    try:
        asyncio.run(run_forever(client, args))
    except KeyboardInterrupt:
        logging.getLogger("ptrade-sim").info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())

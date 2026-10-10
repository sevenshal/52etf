"""守猪逮兔 A 股策略的外部账户自动执行器。

沿用旧 ptrade 拉取接口的逐标的、贪恐阈值、冷却和仓位算法；不同之处仅在于
持仓/现金来自外部交易子账户账本，下单通过目标仓位和统一执行器完成。
"""

import asyncio
import logging
import threading
import traceback
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from ..core.database import (
    SZDTTradingConfig,
    StockCooldown,
    SzdtTradeStock,
    TradingLog,
    TradingState,
    get_db_ctx,
)
from ..core.external_trading_database import (
    ExternalTradingAccount,
    ExternalTradingSubAccount,
    ExternalTradingTargetPosition,
    get_external_trading_db_ctx,
)
from ..core.services.external_trading_executor import trigger_external_trading_executor
from ..core.services.external_trading_ledger import (
    STRATEGY_SZDT_A_STOCK,
    get_ledger_positions,
    normalize_symbol,
    safe_float,
    safe_int,
    sync_target_positions,
)
from ..core.services.external_trading_market import (
    EXTERNAL_TRADING_MARKET_A_STOCK,
    is_external_trading_market_open,
    normalize_external_trading_market_type,
)
from ..core.services.external_trading_valuation import get_realtime_reference_prices
from ..core.services.szdt import SZDTService
from ..core.services.szdt_etf_volume import get_a_share_etf_ema5, get_a_share_etf_volume_metrics
from ..core.utils import send_alert_email

logger = logging.getLogger(__name__)


def _external_symbol(code: str) -> str:
    """SZDT 的 SH.510300/SZ.159xxx 格式转成账本使用的 510300.SH。"""
    market, separator, ticker = str(code or "").upper().partition(".")
    if separator and market in {"SH", "SZ", "BJ"} and ticker:
        return f"{ticker}.{market}"
    return normalize_symbol(code)


class SZDTAStockExternalTrader:
    _instance: Optional["SZDTAStockExternalTrader"] = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._thread_started = False
                cls._instance._thread_lock = threading.Lock()
                cls._instance._szdt_service = SZDTService()
        return cls._instance

    @staticmethod
    def _log(account_id: str, level: str, message: str) -> None:
        with get_db_ctx() as db:
            db.add(TradingLog(account_id=account_id, timestamp=datetime.now(), level=level, message=message))

    @staticmethod
    def _set_cooldown(account_id: str, cli_id: str, code: str, duration: timedelta, reason: str) -> None:
        with get_db_ctx() as db:
            existing = db.query(StockCooldown).filter_by(
                account_id=account_id, cli_id=cli_id, stock_code=code,
            ).first()
            if existing:
                existing.until = datetime.now() + duration
                existing.reason = reason
            else:
                db.add(StockCooldown(
                    account_id=account_id, cli_id=cli_id, stock_code=code,
                    until=datetime.now() + duration, reason=reason,
                ))

    @staticmethod
    def _select_next_stock(account_id: str, cli_id: str) -> Optional[Dict[str, Any]]:
        now = datetime.now()
        with get_db_ctx() as db:
            state = db.query(TradingState).filter_by(account_id=account_id, cli_id=cli_id).first()
            if not state:
                state = TradingState(account_id=account_id, cli_id=cli_id, current_index=0)
                db.add(state)
            stocks = db.query(SzdtTradeStock).filter(
                SzdtTradeStock.account_id == account_id,
                SzdtTradeStock.type == 3,
                SzdtTradeStock.enabled == True,  # noqa: E712
            ).order_by(SzdtTradeStock.id.asc()).all()
            db.query(StockCooldown).filter(
                StockCooldown.account_id == account_id,
                StockCooldown.cli_id == cli_id,
                StockCooldown.until < now,
            ).delete(synchronize_session=False)
            if not stocks:
                return None
            if state.current_index >= len(stocks):
                state.current_index = 0
            selected = None
            for _ in range(len(stocks)):
                stock = stocks[state.current_index]
                state.current_index = (state.current_index + 1) % len(stocks)
                if db.query(StockCooldown).filter_by(
                    account_id=account_id, cli_id=cli_id, stock_code=stock.code,
                ).first():
                    continue
                selected = {
                    key: getattr(stock, key)
                    for key in (
                        "code", "name", "type", "when_buy", "when_sell", "max_position",
                        "buy_amount", "sell_amount", "buy_factor", "sell_factor", "buy_volume_ratio",
                        "lever", "emo_area",
                    )
                }
                break
            return selected

    @staticmethod
    def _config_snapshot(config: SZDTTradingConfig) -> Dict[str, Any]:
        return {
            "id": config.id,
            "account_id": config.account_id,
            "external_trading_account_id": config.external_trading_account_id,
            "live_sub_account_id": config.live_sub_account_id,
            "a_sell_on_ema5_breakdown": bool(config.a_sell_on_ema5_breakdown),
        }

    async def _load_external_snapshot(self, config: Dict[str, Any]) -> Dict[str, Any]:
        with get_external_trading_db_ctx() as db:
            account = db.query(ExternalTradingAccount).filter(
                ExternalTradingAccount.id == config["external_trading_account_id"],
                ExternalTradingAccount.account_id == config["account_id"],
                ExternalTradingAccount.enabled == True,  # noqa: E712
            ).first()
            sub = db.query(ExternalTradingSubAccount).filter(
                ExternalTradingSubAccount.id == config["live_sub_account_id"],
                ExternalTradingSubAccount.account_id == config["account_id"],
                ExternalTradingSubAccount.external_trading_account_id == config["external_trading_account_id"],
                ExternalTradingSubAccount.enabled == True,  # noqa: E712
                ExternalTradingSubAccount.strategy_type == STRATEGY_SZDT_A_STOCK,
                ExternalTradingSubAccount.strategy_config_id == config["id"],
            ).first()
            if not account or not sub:
                raise ValueError("外部交易账户或虚拟子账户未正确绑定/未启用")
            if normalize_external_trading_market_type(account.market_type) != EXTERNAL_TRADING_MARKET_A_STOCK:
                raise ValueError("绑定的外部交易账户不是A股账户")
            positions = {
                normalize_symbol(symbol): safe_int(row.quantity)
                for symbol, row in get_ledger_positions(db, sub.id).items()
                if safe_int(row.quantity) > 0
            }
            targets = {
                normalize_symbol(row.symbol): safe_int(row.target_quantity)
                for row in db.query(ExternalTradingTargetPosition).filter(
                    ExternalTradingTargetPosition.sub_account_id == sub.id,
                    ExternalTradingTargetPosition.status == "ACTIVE",
                ).all()
            }
            return {
                "cash": safe_float(sub.cash_available), "positions": positions, "targets": targets,
                "external_account_id": account.id, "sub_account_id": sub.id,
            }

    async def _sync_target(
        self, config: Dict[str, Any], snapshot: Dict[str, Any], symbol: str, target_quantity: int,
        price: float, reason: str,
    ) -> str:
        target_quantities = dict(snapshot["targets"])
        for held_symbol, quantity in snapshot["positions"].items():
            target_quantities.setdefault(held_symbol, quantity)
        target_quantities[symbol] = max(0, int(target_quantity))
        targets = [{
            "symbol": target_symbol,
            "target_quantity": quantity,
            "target_value": round(quantity * price, 2) if target_symbol == symbol else None,
            "reference_price": round(price, 4) if target_symbol == symbol else None,
            "reference_price_source": "szdt_a_stock",
        } for target_symbol, quantity in target_quantities.items()]
        signal_id = f"szdt_a:{config['id']}:{symbol}:{datetime.now():%Y%m%d%H%M%S}"
        with get_external_trading_db_ctx() as db:
            sub = db.query(ExternalTradingSubAccount).filter(
                ExternalTradingSubAccount.id == snapshot["sub_account_id"],
                ExternalTradingSubAccount.strategy_type == STRATEGY_SZDT_A_STOCK,
                ExternalTradingSubAccount.strategy_config_id == config["id"],
            ).first()
            if not sub:
                raise ValueError("外部虚拟子账户绑定已失效")
            sync_target_positions(db, sub_account=sub, targets=targets, signal_id=signal_id, signal_version=signal_id)
        result = await trigger_external_trading_executor(
            account_id=config["account_id"], external_account_id=snapshot["external_account_id"],
            trigger_source=f"szdt_a_{reason}",
        )
        return f"目标仓位={target_quantity}；执行器={result.get('status')}"

    async def run_config_once(
        self,
        config: Dict[str, Any],
        volume_metrics: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        account_id = config["account_id"]
        cli_id = f"szdt-a-external-{account_id}"
        stock = self._select_next_stock(account_id, cli_id)
        if not stock:
            return
        name = f"{stock['name']}({stock['code']})"
        try:
            snapshot = await self._load_external_snapshot(config)
            symbol = _external_symbol(stock["code"])
            quote_symbols = [*snapshot["positions"].keys(), symbol]
            prices = await get_realtime_reference_prices(snapshot["external_account_id"], quote_symbols)
            price = safe_float(prices.get(symbol))
            if price <= 0:
                raise ValueError(f"无法获取 {symbol} 实时价格")
            portfolio_value = snapshot["cash"] + sum(
                quantity * safe_float(prices.get(position_symbol))
                for position_symbol, quantity in snapshot["positions"].items()
            )
            if portfolio_value <= 0:
                raise ValueError("子账户可用资金和持仓市值均为零")
            quantity = safe_int(snapshot["positions"].get(symbol))
            position_ratio = quantity * price / portfolio_value * 100
            emotion = await self._szdt_service.get_fresh_emotion_from_list(3, stock["code"])
            if not emotion:
                emotion = await self._szdt_service.get_stock_emotion(stock["code"], stock["lever"], stock["emo_area"])
            if not emotion or emotion.get("status") != 1:
                self._log(account_id, "DEBUG", f"{name} 获取情绪失败，跳过")
                return
            score = float(emotion["data"]["score"])

            if score > stock["when_buy"] + 10 and quantity == 0:
                self._set_cooldown(account_id, cli_id, stock["code"], timedelta(hours=2), "无持仓且情绪过高")
                return
            if score < stock["when_sell"] - 10 and position_ratio > stock["max_position"]:
                self._set_cooldown(account_id, cli_id, stock["code"], timedelta(hours=2), "持仓已满且情绪过低")
                return

            if score <= stock["when_buy"]:
                volume_ratio_threshold = safe_float(stock.get("buy_volume_ratio"))
                if volume_ratio_threshold > 0:
                    metrics = volume_metrics
                    if metrics is None:
                        metrics = await asyncio.to_thread(get_a_share_etf_volume_metrics, [stock["code"]])
                    volume_metric = metrics.get(symbol, {})
                    volume_ratio = safe_float(volume_metric.get("volume_ratio"))
                    if volume_ratio <= 0 or volume_ratio < volume_ratio_threshold:
                        self._log(
                            account_id,
                            "DEBUG",
                            f"{name} 量比 {volume_ratio or '-'} 未达买入阈值 {volume_ratio_threshold:.2f}，跳过",
                        )
                        self._set_cooldown(account_id, cli_id, stock["code"], timedelta(minutes=5), "量比未达买入阈值")
                        return
                if position_ratio < stock["max_position"]:
                    factor = min(1, max(0, (stock["when_buy"] - score) / (stock["when_buy"] + 100)))
                    buy_amount = min(snapshot["cash"], stock["buy_amount"] * (3 ** (factor ** stock["buy_factor"])))
                    buy_quantity = int(buy_amount / price / 100) * 100
                    if buy_quantity >= 100:
                        result = await self._sync_target(config, snapshot, symbol, quantity + buy_quantity, price, "buy")
                        self._log(account_id, "INFO", f"{name} BUY {buy_quantity}，分数={score:.0f}；{result}")
                        self._set_cooldown(account_id, cli_id, stock["code"], timedelta(hours=12), "当日买入信号已触发")
                    else:
                        self._set_cooldown(account_id, cli_id, stock["code"], timedelta(hours=1), "资金不足，冷却1h")
                else:
                    self._set_cooldown(account_id, cli_id, stock["code"], timedelta(hours=1), "仓位已达上限，冷却1h")
                return

            if score >= stock["when_sell"]:
                if config.get("a_sell_on_ema5_breakdown"):
                    ema5 = await asyncio.to_thread(get_a_share_etf_ema5, stock["code"], price)
                    if ema5 is None or price >= ema5:
                        self._log(
                            account_id,
                            "DEBUG",
                            f"{name} 贪婪={score:.0f}，价格 {price:.3f} 未跌破EMA5 {ema5:.3f}" if ema5 else
                            f"{name} 贪婪={score:.0f}，EMA5 数据不足，继续持有",
                        )
                        self._set_cooldown(account_id, cli_id, stock["code"], timedelta(minutes=5), "贪婪等待跌破EMA5")
                        return
                if quantity >= 100:
                    factor = min(1, max(0, (score - stock["when_sell"]) / (100 - stock["when_sell"])))
                    sell_amount = stock["sell_amount"] * (3 ** (factor ** stock["sell_factor"]))
                    sell_quantity = min(quantity, max(100, int(sell_amount / price / 100) * 100))
                    result = await self._sync_target(config, snapshot, symbol, quantity - sell_quantity, price, "sell")
                    self._log(account_id, "INFO", f"{name} SELL {sell_quantity}，分数={score:.0f}；{result}")
                    self._set_cooldown(account_id, cli_id, stock["code"], timedelta(hours=12), "当日卖出信号已触发")
                else:
                    self._set_cooldown(account_id, cli_id, stock["code"], timedelta(hours=1), "无可卖持仓，冷却1h")
                return

            delta = min(score - stock["when_buy"], stock["when_sell"] - score)
            minutes = round(min(720, 1.6 ** delta))
            if minutes > 1:
                self._set_cooldown(account_id, cli_id, stock["code"], timedelta(minutes=minutes), "情绪中性")
        except Exception as exc:
            logger.exception("SZDT A股外部交易执行失败 account=%s", account_id)
            self._log(account_id, "ERROR", f"{name} 外部自动交易异常: {exc}")
            send_alert_email("守猪逮兔A股自动交易报错", traceback.format_exc(), scenario_key="szdt_a_external_error")

    async def worker_loop(self) -> None:
        while True:
            try:
                if is_external_trading_market_open("A_STOCK"):
                    with get_db_ctx() as db:
                        configs = [self._config_snapshot(row) for row in db.query(SZDTTradingConfig).filter(
                            SZDTTradingConfig.enabled_a == True,  # noqa: E712
                            SZDTTradingConfig.external_trading_account_id.isnot(None),
                            SZDTTradingConfig.live_sub_account_id.isnot(None),
                        ).all()]
                        configured_accounts = [config["account_id"] for config in configs]
                        volume_codes = [
                            row.code for row in db.query(SzdtTradeStock).filter(
                                SzdtTradeStock.account_id.in_(configured_accounts),
                                SzdtTradeStock.type == 3,
                                SzdtTradeStock.enabled == True,  # noqa: E712
                                SzdtTradeStock.buy_volume_ratio > 0,
                            ).all()
                        ] if configured_accounts else []
                    # 每分钟最多按沪/深两批拉一次实时 ETF 行情；服务内部再缓存 30 秒。
                    volume_metrics = await asyncio.to_thread(get_a_share_etf_volume_metrics, volume_codes)
                    for config in configs:
                        await self.run_config_once(config, volume_metrics)
            except Exception:
                logger.exception("SZDT A股外部交易主循环异常")
            await asyncio.sleep(60)


def start_szdt_a_stock_external_trader() -> None:
    trader = SZDTAStockExternalTrader()
    with trader._thread_lock:
        if trader._thread_started:
            return
        trader._thread_started = True

    def runner() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(trader.worker_loop())

    threading.Thread(target=runner, daemon=True, name="SZDTAStockExternalTrader").start()

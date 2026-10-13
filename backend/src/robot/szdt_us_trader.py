import logging
import threading
from datetime import datetime, timedelta
import asyncio
from math import ceil, pow
from typing import Dict

from ..core.database import get_db_ctx, SzdtTradeStock, TradingLog, TradingState, StockCooldown, SZDTTradingConfig, IBKRAccountConfig
from ..core.services.szdt import SZDTService
from ..core.services.market import MarketService
from ..core.services.ib_service import IBKRService, IBOrderSubmissionPending
from ..core.services.szdt_us_volume import get_us_stock_volume_metrics
from ..core.utils import mask_account_id, send_alert_email
import traceback

logger = logging.getLogger(__name__)

class SZDTUSTrader:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(SZDTUSTrader, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.szdt = SZDTService()
        self.ib_services: Dict[str, IBKRService] = {} # Key: "port_clientid"
        self._thread_started = False
        self._thread_lock = threading.Lock()

    def _log(self, account_id: str, level: str, message: str):
        with get_db_ctx() as db:
            db.add(TradingLog(
                account_id=account_id,
                timestamp=datetime.now(),
                level=level,
                message=message
            ))

    async def _ensure_ib_connected(self, port: int, client_id: int) -> IBKRService:
        """确保在当前线程(Worker)中连接到特定的 IB 账户，并返回该 service 实例"""
        key = f"{port}_{client_id}"
        if key not in self.ib_services:
            logger.info(f"Creating new IBKRService instance for port={port}, client_id={client_id}")
            self.ib_services[key] = IBKRService(port=port, client_id=client_id)
        
        service = self.ib_services[key]
        await service.connect()
        return service

    def _set_decision_cooldown(self, account_id: str, cli_id: str, stock_code: str, hours: float, reason: str):
        """写入「订单已提交」后的冷却窗口。

        回执超时不等于订单未发出，仍必须冷却；但量比/资金不满足或下单前报错时，
        不应冷却，下一轮应重新评估条件。
        """
        with get_db_ctx() as db:
            db.add(StockCooldown(
                account_id=account_id,
                cli_id=cli_id,
                stock_code=stock_code,
                until=datetime.now() + timedelta(hours=hours),
                reason=reason
            ))

    @staticmethod
    def _order_id_of(pending: IBOrderSubmissionPending):
        """从回执超时异常里取出订单号，仅用于日志追踪。"""
        order = getattr(getattr(pending, "trade", None), "order", None)
        return getattr(order, "orderId", None)

    def _get_next_stock(self, account_id: str, cli_id: str) -> Dict:
        """按状态索引 + 冷却筛选，每次只取一只。"""
        with get_db_ctx() as db:
            state = db.query(TradingState).filter_by(account_id=account_id, cli_id=cli_id).first()
            if not state:
                state = TradingState(account_id=account_id, cli_id=cli_id, current_index=0)
                db.add(state)

            stocks = db.query(SzdtTradeStock).filter(
                SzdtTradeStock.account_id == account_id,
                SzdtTradeStock.type.in_([1, 2, 7]),
                SzdtTradeStock.enabled == True,
            ).all()
            if not stocks:
                db.commit()
                return None

            now = datetime.now()
            db.query(StockCooldown).filter(
                StockCooldown.account_id == account_id,
                StockCooldown.cli_id == cli_id, 
                StockCooldown.until < now
            ).delete()

            if state.current_index >= len(stocks):
                state.current_index = 0

            selected = None
            # Do not loop infinitely if all on cooldown
            loop_count = 0
            
            while loop_count < len(stocks):
                if state.current_index >= len(stocks):
                     state.current_index = 0
                
                s = stocks[state.current_index]
                state.current_index += 1
                loop_count += 1
                
                cooldown = db.query(StockCooldown).filter_by(
                    account_id=account_id,
                    cli_id=cli_id, 
                    stock_code=s.code
                ).first()
                if cooldown:
                    continue
                selected = {
                    'code': s.code,
                    'name': s.name,
                    'type': s.type,
                    'when_buy': s.when_buy,
                    'when_sell': s.when_sell,
                    'max_position': s.max_position,
                    'buy_amount': s.buy_amount,
                    'sell_amount': s.sell_amount,
                    'buy_factor': s.buy_factor,
                    'sell_factor': s.sell_factor,
                    'buy_volume_ratio': s.buy_volume_ratio,
                    'lever': s.lever,
                    'emo_area': s.emo_area,
                }
                break
            db.commit()
            return selected

    async def run_once(self, config: SZDTTradingConfig, ib_port: int, volume_metrics: Dict | None = None):
        account_id = config["account_id"] if isinstance(config, dict) else config.account_id
        config_id = config["id"] if isinstance(config, dict) else config.id
        us_rotation_enabled = config.get("us_rotation_enabled", False) if isinstance(config, dict) else bool(getattr(config, "us_rotation_enabled", False))
        us_rotation_threshold = config.get("us_rotation_threshold", -30) if isinstance(config, dict) else getattr(config, "us_rotation_threshold", -30)
        cli_id = f'szdt-us-auto-{account_id}'
        # Use a consistent client ID for this account to allow connection reuse. 
        # config.id is unique per config entry. Using base 2000 to avoid conflict with portfolio copy trader (100+) and others.
        ib_client_id = 2000 + (config_id or 0)
        
        logger.info(f"SZDTUSTrader tick for {mask_account_id(account_id)}")
            
        try:
            # 1. Reuse connection
            ib_service = await self._ensure_ib_connected(ib_port, ib_client_id)
            
            # 2. Check IB connectivity/NetLiquidation
            net_liq = ib_service.get_net_liquidation()
            # 买入只能动用真实现金：AvailableFunds 含持仓的保证金额度，拿它下单就是融资
            available_cash = ib_service.get_cash_buy_budget()
            
            # If values are invalid, it might be disconnected or not ready, try re-connect/wait?
            # IBService usually handles re-connect on 'connect' call if needed.
            
            logger.info(f"Account {mask_account_id(account_id)} net_liq={net_liq:.2f} cash={available_cash:.2f}")
            
            stock = self._get_next_stock(account_id, cli_id)
            if not stock:
                logger.debug(f"Account {mask_account_id(account_id)}: No candidate stock")
                return
                
            name = f"{stock['name']}({stock['code']})"
            # 获取情绪
            emotion = await self.szdt.get_fresh_emotion_from_list(stock['type'], stock['code'])
            if not emotion or emotion.get('status') != 1:
                emotion = await self.szdt.get_stock_emotion(stock['code'], stock['lever'], stock['emo_area'])
                if not emotion or emotion.get('status') != 1:
                    self._log(account_id, 'DEBUG', f"{name} 获取情绪失败，跳过。 {emotion}")
                    return
            
            score = emotion['data']['score']
            emotion_price = emotion['data']['price']
            logger.info(f"Emotion fetched {name} score={score} emotion_price={emotion_price}")

            # 获取持仓数据 (包含数量和 IB 市场价格)
            pos_data = ib_service.get_position(stock['code'])
            position_qty = pos_data['qty']
            # 优先使用 IB 的市场价格，回退到情绪 API 价格
            price = pos_data['price'] if pos_data['price'] else emotion_price
            
            position_value = position_qty * price
            portfolio_value = max(net_liq, 1.0)
            position_ratio = 100 * position_value / portfolio_value

            # 过热/过冷保护并设置冷却
            if score > (stock['when_buy'] + 10) and position_qty == 0:
                self._log(account_id, 'DEBUG', f"{name} 无持仓且情绪分数过高(当前:{score},买:{stock['when_buy']})，冷却2小时")
                with get_db_ctx() as db:
                    db.add(StockCooldown(
                        account_id=account_id,
                        cli_id=cli_id, 
                        stock_code=stock['code'], 
                        until=datetime.now() + timedelta(hours=2), 
                        reason='无持仓且情绪分数过高'
                    ))
                return
            if score < (stock['when_sell'] - 10) and position_ratio > stock['max_position']:
                self._log(account_id, 'DEBUG', f"{name} 持仓已满(持仓:{position_ratio:.2f}% 上限:{stock['max_position']}%)且情绪分数过低(当前:{score},卖:{stock['when_sell']})，冷却2小时")
                with get_db_ctx() as db:
                    db.add(StockCooldown(
                        account_id=account_id,
                        cli_id=cli_id, 
                        stock_code=stock['code'], 
                        until=datetime.now() + timedelta(hours=2), 
                        reason='持仓已满且情绪分数过低'
                    ))
                return

            # 买入条件
            if score <= stock['when_buy']:
                if position_ratio < stock['max_position']:
                    volume_ratio_threshold = float(stock.get('buy_volume_ratio') or 0)
                    if volume_ratio_threshold > 0:
                        metric = (volume_metrics or {}).get(stock['code'], {})
                        volume_ratio = metric.get('volume_ratio')
                        if volume_ratio is None or float(volume_ratio) < volume_ratio_threshold:
                            self._log(account_id, 'DEBUG', f"{name} 量比 {volume_ratio or '-'} 未达买入阈值 {volume_ratio_threshold:.2f}，跳过")
                            return
                    score_factor = min(1, max(0, (stock['when_buy'] - score) / (stock['when_buy'] + 100)))
                    score_factor = 3 ** (score_factor ** stock['buy_factor'])
                    requested_amount = stock['buy_amount'] * score_factor
                    if us_rotation_enabled and available_cash < requested_amount:
                            threshold = float(us_rotation_threshold)
                            with get_db_ctx() as db:
                                sources = [{
                                    'code': row.code, 'name': row.name, 'type': row.type, 'lever': row.lever,
                                    'emo_area': row.emo_area,
                                } for row in db.query(SzdtTradeStock).filter(
                                    SzdtTradeStock.account_id == account_id,
                                    SzdtTradeStock.type.in_([1, 2, 7]),
                                    SzdtTradeStock.enabled == True,  # noqa: E712
                                ).all() if row.code != stock['code']]
                            candidates = []
                            for source in sources:
                                position = ib_service.get_position(source['code'])
                                source_qty, source_price = int(position.get('qty') or 0), float(position.get('price') or 0)
                                if source_qty <= 0 or source_price <= 0:
                                    continue
                                source_emotion = await self.szdt.get_fresh_emotion_from_list(source['type'], source['code'])
                                if not source_emotion:
                                    source_emotion = await self.szdt.get_stock_emotion(source['code'], source['lever'], source['emo_area'])
                                if source_emotion and source_emotion.get('status') == 1 and float(source_emotion['data']['score']) >= threshold:
                                    candidates.append((float(source_emotion['data']['score']), source, source_qty, source_price))
                            submitted = []
                            for source_score, source, source_qty, source_price in sorted(candidates, reverse=True):
                                if available_cash >= requested_amount:
                                    break
                                sell_qty = min(source_qty, max(1, ceil((requested_amount - available_cash) / source_price)))
                                await ib_service.place_market_order(source['code'], 'SELL', sell_qty)
                                available_cash += sell_qty * source_price
                                submitted.append(f"{source['code']}x{sell_qty}(贪恐={source_score:.0f})")
                                self._set_decision_cooldown(account_id, cli_id, source['code'], 12, '轮动腾挪卖出')
                            if submitted:
                                self._log(account_id, 'INFO', f"{name} 为极端恐慌买入腾挪现金，已提交卖出 {', '.join(submitted)}；等待资金到账后重试买入")
                                return
                    buy_amount = min(available_cash, requested_amount)
                    buy_quantity = int(buy_amount / price)
                    if buy_quantity >= 1:
                        try:
                            trade = await ib_service.place_market_order(stock['code'], 'BUY', buy_quantity)
                        except IBOrderSubmissionPending as pending:
                            # 回执超时不等于订单未发出，按已提交处理并冷却以避免重复下单。
                            order_id = self._order_id_of(pending)
                            self._log(account_id, 'WARNING', f"{name} BUY x{buy_quantity} @{price:.2f} (系数{score_factor:.2f}) 已提交但 IBKR 回执超时({pending.status}) oid={order_id}，按已提交处理")
                            logger.warning(f"{name} BUY x{buy_quantity} @{price:.2f} factor={score_factor:.2f} oid={order_id} submitted, ack timeout ({pending.status})")
                        else:
                            order_id = trade.order.orderId
                            self._log(account_id, 'INFO', f"{name} BUY x{buy_quantity} @{price:.2f} (系数{score_factor:.2f}) oid={order_id}")
                            logger.info(f"{name} BUY x{buy_quantity} @{price:.2f} factor={score_factor:.2f} oid={order_id}")
                        self._set_decision_cooldown(account_id, cli_id, stock['code'], 12, '订单已提交后冷却12h')
                    else:
                        self._log(account_id, 'INFO', f"{name} 可用现金 {available_cash:.2f} 不足（不融资），跳过买入")
                return

            # 卖出条件
            if score >= stock['when_sell'] and position_qty > 0:
                score_factor = min(1, max(0, (score - stock['when_sell']) / (100 - stock['when_sell'])))
                score_factor = 3 ** (score_factor ** stock['sell_factor'])
                sell_amount = stock['sell_amount'] * score_factor
                sell_quantity = int(sell_amount / price)
                sell_quantity = max(min(sell_quantity, int(position_qty)), 1)
                if sell_quantity > 0:
                    try:
                        trade = await ib_service.place_market_order(stock['code'], 'SELL', sell_quantity)
                    except IBOrderSubmissionPending as pending:
                        # 同买入：回执超时不等于没卖掉，按已提交处理并冷却。
                        order_id = self._order_id_of(pending)
                        self._log(account_id, 'WARNING', f"{name} SELL x{sell_quantity} @{price:.2f} (系数{score_factor:.2f}) 已提交但 IBKR 回执超时({pending.status}) oid={order_id}，按已提交处理")
                        logger.warning(f"{name} SELL x{sell_quantity} @{price:.2f} factor={score_factor:.2f} oid={order_id} submitted, ack timeout ({pending.status})")
                    else:
                        order_id = trade.order.orderId
                        self._log(account_id, 'INFO', f"{name} SELL x{sell_quantity} @{price:.2f} (系数{score_factor:.2f}) oid={order_id}")
                        logger.info(f"{name} SELL x{sell_quantity} @{price:.2f} factor={score_factor:.2f} oid={order_id}")
                    self._set_decision_cooldown(account_id, cli_id, stock['code'], 12, '订单已提交后冷却12h')
                return

            # 中性区间：设置距离相关冷却
            score_delta = min(score - stock['when_buy'], stock['when_sell'] - score)
            cooldown_minutes = round(min(720, pow(1.6, score_delta)))
            if cooldown_minutes > 1:
                with get_db_ctx() as db:
                    db.add(StockCooldown(
                        account_id=account_id,
                        cli_id=cli_id, 
                        stock_code=stock['code'], 
                        until=datetime.now() + timedelta(minutes=cooldown_minutes), 
                        reason='情绪分数距离买卖阈值较远'
                    ))
            self._log(account_id, 'DEBUG', f"{name} 情绪分数介于买卖阈值之间(当前:{score},买:{stock['when_buy']},卖:{stock['when_sell']}), 冷却{cooldown_minutes}分钟")
            logger.info(f"{name} neutral score={score} buy={stock['when_buy']} sell={stock['when_sell']} cooldown={cooldown_minutes}m")
                
        except Exception as e:
            logger.error(f"SZDTUSTrader的处理 {account_id} 失败: {e}")
            self._log(account_id, 'ERROR', f"处理异常: {e}")
            send_alert_email(
                f"自动化交易策略报错: SZDTUSTrader 账号 {account_id}",
                f"Error: {e}\n\nTraceback:\n{traceback.format_exc()}",
                scenario_key="szdt_us_trading_error",
            )

    @staticmethod
    def _config_snapshot(config: SZDTTradingConfig) -> Dict:
        return {"id": config.id, "account_id": config.account_id, "ib_account_id": config.ib_account_id,
                "us_rotation_enabled": bool(config.us_rotation_enabled), "us_rotation_threshold": int(config.us_rotation_threshold)}

    @staticmethod
    def _volume_metric_stock_snapshots(rows) -> list[Dict]:
        """从会话内查询结果提取量比所需字段，供会话外的异步任务使用。"""
        return [
            {"code": row.code, "buy_volume_ratio": row.buy_volume_ratio}
            for row in rows
        ]

    async def run_scheduled_once(self):
        """由系统定时任务调用；两个美股定时点内逐个检查账户的全部启用标的。"""
        if not MarketService.is_us_market_open():
            return
        with get_db_ctx() as db:
            configs = [self._config_snapshot(config) for config in db.query(SZDTTradingConfig).filter(
                SZDTTradingConfig.enabled == True,
            ).all()]
            stocks_by_account = {}
            for config in configs:
                stock_rows = db.query(SzdtTradeStock).filter(
                    SzdtTradeStock.account_id == config['account_id'],
                    SzdtTradeStock.type.in_([1, 2, 7]),
                    SzdtTradeStock.enabled == True,
                ).with_entities(
                    SzdtTradeStock.code,
                    SzdtTradeStock.buy_volume_ratio,
                ).all()
                # get_db_ctx 退出会 commit，ORM 属性随之过期。定时任务在会话外只需
                # 量比预热所需字段，因此立刻转换为普通字典，不能把 ORM 实体带出去。
                stocks_by_account[config['account_id']] = self._volume_metric_stock_snapshots(stock_rows)
        for config in configs:
            stocks = stocks_by_account.get(config['account_id'], [])
            if not stocks:
                continue
            volume_codes = [stock["code"] for stock in stocks if float(stock["buy_volume_ratio"] or 0) > 0]
            volume_metrics = await asyncio.to_thread(get_us_stock_volume_metrics, config['account_id'], volume_codes)
            ib_port = 4001
            if config.get('ib_account_id'):
                with get_db_ctx() as db:
                    ib_config = db.query(IBKRAccountConfig).filter(IBKRAccountConfig.id == config['ib_account_id']).first()
                    if ib_config:
                        ib_port = ib_config.ib_port
            # 旧循环每分钟只轮到一只；固定为每日两次后必须在一个任务轮次检查完整配置。
            for _ in range(len(stocks)):
                await self.run_once(config, ib_port, volume_metrics)

    async def worker_loop(self):
        logger.info("SZDT US Trader Worker Loop Started")
        
        while True:
            try:
                await self.run_scheduled_once()
                
                # 4. Wait for next minute
                await asyncio.sleep(60)

            except Exception as e:
                logger.error(f"SZDT US Trader check loop error: {e}")
                send_alert_email(
                    "自动化交易策略报错: SZDTUSTrader 主循环",
                    f"Error: {e}\n\nTraceback:\n{traceback.format_exc()}",
                    scenario_key="szdt_us_trading_error",
                )
                await asyncio.sleep(60)

def start_szdt_us_trader():
    trader = SZDTUSTrader()
    with trader._thread_lock:
        if trader._thread_started:
            logger.info("SZDT US Trader Worker already started, skipping.")
            return
        trader._thread_started = True

    def run_worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(trader.worker_loop())

    thread = threading.Thread(target=run_worker, daemon=True, name="SZDTUSTraderWorker")
    thread.start()
    logger.info("SZDT US Trader Worker Thread started")

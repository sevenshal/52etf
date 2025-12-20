import pandas as pd
import logging
from datetime import timedelta
from .longport import LongPortService
from .quote import QuoteService
from .market import MarketService
from .ib_service import IBKRService
from ..database import get_db_session, AutomatedTradingConfig, AutomatedTradeLog

logger = logging.getLogger(__name__)

async def calculate_ma_signal(symbol: str, short_window: int, long_window: int, account_id: str):
    """
    计算均线信号
    返回: 'BUY', 'SELL' 或 'HOLD'
    """
    trade_service = LongPortService(account_id)
    quote_service = QuoteService(trade_service)
    
    end_date = MarketService.get_eastern_now().date()
    buffer_days = max(60, long_window * 3)
    start_date = end_date - timedelta(days=buffer_days)
    
    klines = quote_service.get_klines(symbol if '.US' in symbol else f"{symbol}.US", start_date=start_date, end_date=end_date)
    if not klines:
        logger.error(f"Failed to fetch klines for {symbol}")
        return 'HOLD'
    
    df = pd.DataFrame(klines)
    if df.empty:
        return 'HOLD'
    
    df['close'] = df['close'].astype(float)
    df['EMA_short'] = df['close'].ewm(span=short_window, adjust=False).mean()
    df['EMA_long'] = df['close'].ewm(span=long_window, adjust=False).mean()
    
    last_row = df.iloc[-1]
    
    if last_row['EMA_short'] > last_row['EMA_long']:
        return 'BUY'
    elif last_row['EMA_short'] < last_row['EMA_long']:
        return 'SELL'
    
    return 'HOLD'

async def execute_trading_strategy(account_id: str):
    """执行自动化交易策略"""
    logger.info(f"Executing trading strategy for account: {account_id}")
    
    try:
        with get_db_session(account_id) as db:
            config = db.query(AutomatedTradingConfig).filter(
                AutomatedTradingConfig.account_id == account_id,
                AutomatedTradingConfig.enabled == True
            ).first()
            
            if not config:
                logger.info(f"No enabled trading config for {account_id}")
                return

            # 1. 计算信号
            signal = await calculate_ma_signal(config.etf_code, config.short_window, config.long_window, account_id)
            logger.info(f"Strategy Signal for {config.etf_code}: {signal}")
            
            if signal == 'HOLD':
                return

            # 2. 检查持仓
            ib_service = IBKRService(port=config.ib_port)
            try:
                await ib_service.connect()
                position = ib_service.get_position(config.etf_code)
                logger.info(f"Current Position for {config.etf_code}: {position}")
                
                action = None
                quantity = 0
                
                # 获取当前价格
                price = await ib_service.get_market_price(config.etf_code)
                if not price or price <= 0:
                    logger.error(f"Invalid market price for {config.etf_code}: {price}")
                    return

                if signal == 'BUY':
                    # 计算目标持仓金额 = 净资产 * 目标比例
                    target_value = ib_service.net_liquidation * (config.target_ratio / 100.0)
                    # 当前持仓金额
                    current_value = position * price
                    
                    if current_value < target_value * 0.1: # 如果当前持仓不足目标的 10%，则买入补齐
                        needed_value = target_value - current_value
                        # 确保不超过可用资金
                        actual_buy_value = min(needed_value, ib_service.available_cash)
                        quantity = int(actual_buy_value / price)
                        if quantity > 0:
                            action = 'BUY'
                
                elif signal == 'SELL' and position > 0:
                    action = 'SELL'
                    quantity = int(position) # 全部平仓
                
                if action and quantity > 0:
                    # 下单
                    trade = await ib_service.place_market_order(config.etf_code, action, quantity)
                    status = 'SUCCESS' if trade.isDone() else 'FAILED'
                    message = f"Order {action} {quantity} {config.etf_code} @{price}. Status: {trade.orderStatus.status}"
                    
                    log = AutomatedTradeLog(
                        account_id=account_id,
                        symbol=config.etf_code,
                        action=action,
                        price=price,
                        quantity=quantity,
                        status=status,
                        message=message
                    )
                    db.add(log)
                    logger.info(message)
                else:
                    logger.info(f"No action needed for {config.etf_code} (Signal: {signal}, Position: {position}, TargetRatio: {config.target_ratio}%)")
                    
            finally:
                ib_service.disconnect()

    except Exception as e:
        logger.error(f"Error in execute_trading_strategy: {e}")
        with get_db_session(account_id) as db:
            db.add(AutomatedTradeLog(
                account_id=account_id,
                symbol="SYSTEM",
                action="ERROR",
                status="FAILED",
                message=str(e)
            ))

def is_market_closing_soon():
    """判断是否接近美股收盘"""
    return MarketService.is_market_closing_soon(10)

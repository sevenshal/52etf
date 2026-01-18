from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timedelta
import logging
import random
from ...core.database import get_db, TradingState, StockCooldown, SzdtTradeStock, TradingLog, Session
from .account import valid_account
from .szdt import szdt_service, get_auto_trading_status
from ...core.models.account import SzdtActiveCode
from ...core.utils import load_config_file
from sqlalchemy import and_

router = APIRouter(prefix="/api/trade")

class Order(BaseModel):
    symbol: str
    side: str
    quantity: int
    status: str
    submitted_at: datetime

class Position(BaseModel):
    symbol: str
    quantity: int
    cost_price: float
    available_quantity: Optional[int] = None

class Portfolio(BaseModel):
    portfolio_value: float      # 总资产
    available_cash: float       # 可用资金
    locked_cash: float         # 冻结资金
    total_cash: float          # 总资金
    total_positions_value: float  # 持仓市值

class TradeRequest(BaseModel):
    cli_id: str
    backtest: bool
    orders: List[Order]
    positions: List[Position]
    portfolio: Portfolio        # 新增 portfolio 字段
    current_time: Optional[datetime] = None # Optional simulated time

class TradeOpportunity(BaseModel):
    symbol: str
    name: str
    action: str  # "BUY" or "SELL"
    quantity: int
    reason: str

class TradeResponse(BaseModel):
    opportunities: List[TradeOpportunity]
    msg: Optional[str] = None

@router.post("/opportunities", response_model=TradeResponse)
async def get_trade_opportunities(
    request: TradeRequest,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db)
):
    """获取交易机会"""
    if not get_auto_trading_status(account_id):
        return TradeResponse(opportunities=[], msg="自动交易未启用")
    
    cli_id = request.cli_id
    
    # 获取或创建交易状态
    state = db.query(TradingState).filter_by(account_id=account_id, cli_id=cli_id).first()
    if not state:
        state = TradingState(account_id=account_id, cli_id=cli_id, current_index=0)
        db.add(state)

    # 获取可交易的股票列表（限定 type=3）
    stocks = db.query(SzdtTradeStock).filter(SzdtTradeStock.account_id == account_id, SzdtTradeStock.type == 3).all()
    if not stocks:
        return TradeResponse(opportunities=[], msg="未获取到可交易的股票列表")

    current_time = datetime.now()
    # 清理过期的冷却记录
    db.query(StockCooldown).filter(
        and_(
            StockCooldown.account_id == account_id,
            StockCooldown.cli_id == cli_id,
            StockCooldown.until < current_time
        )
    ).delete()

    # 如果当前索引超出范围，重置为0
    if state.current_index >= len(stocks):
        state.current_index = 0

    # 获取当前要处理的股票
    today_order_symbols = [order.symbol for order in request.orders]
    trade_stock:SzdtTradeStock = None
    while state.current_index < len(stocks):
        stock = stocks[state.current_index]
        state.current_index += 1
        # 检查今日是否已有订单
        if stock.code in today_order_symbols:
            continue
        # 检查冷却状态
        cooldown = db.query(StockCooldown).filter_by(
            account_id=account_id,
            cli_id=cli_id,
            stock_code=stock.code
        ).first()
        if cooldown:
            continue
        trade_stock = stock
        break
    db.commit()
    if not trade_stock:
        return TradeResponse(opportunities=[], msg='未获取到可交易的股票')
    
    name = f"{trade_stock.name}({trade_stock.code})"
    # 获取情绪数据
    if request.backtest:
        name = '[回测]' + name
        emotion = {
            'status': 1,
            'data': {
                'score': random.randint(-100, 100),
                'price': 10.0  # 回测时使用一个固定价格
            }
        }
    else:
        emotion = await szdt_service.get_fresh_emotion_from_list(3, trade_stock.code)
        if not emotion:
            emotion = await szdt_service.get_stock_emotion(trade_stock.code, trade_stock.lever, trade_stock.emo_area)

    if not emotion or emotion['status'] != 1:
        return TradeResponse(opportunities=[], msg=(emotion['msg'] if emotion else None) or f'{name}获取情绪指标失败')

    score = emotion['data']['score']
    price = emotion['data']['price']

    # 生成交易机会
    opportunities = []
    current_position = next(
        (p for p in request.positions if p.symbol == trade_stock.code),
        None
    )
    position_value = (current_position.quantity * price if current_position else 0)
    position_ratio = 100 * position_value / request.portfolio.portfolio_value

    # 检查高情绪分数
    if score > (trade_stock.when_buy + 10) and not current_position:
        db.add(StockCooldown(
            account_id=account_id,
            cli_id=cli_id,
            stock_code=trade_stock.code,
            until=current_time + timedelta(hours=2),
            reason='无持仓且情绪分数过高'
        ))
        db.add(TradingLog(
            account_id=account_id,
            timestamp=current_time,
            level='DEBUG',
            message=f'{name}无持仓且情绪分数过高(当前:{score},买:{trade_stock.when_buy}),冷却2小时'
        ))
        db.commit()
        return TradeResponse(opportunities=[], msg=f'{name}无持仓且情绪分数过高,冷却2小时')

    # 检查低情绪分数
    if score < (trade_stock.when_sell - 10) and position_ratio > trade_stock.max_position:
        db.add(StockCooldown(
            account_id=account_id,
            cli_id=cli_id,
            stock_code=trade_stock.code,
            until=current_time + timedelta(hours=2),
            reason='持仓已满且情绪分数过低'
        ))
        db.add(TradingLog(
            account_id=account_id,
            timestamp=current_time,
            level='DEBUG',
            message=f'{name}持仓已满(持仓:{position_ratio:.2f}%,上限:{trade_stock.max_position}%)且情绪分数过低(当前:{score},卖:{trade_stock.when_sell}),冷却2小时'
        ))
        db.commit()
        return TradeResponse(opportunities=[], msg=f'{name}持仓已满且情绪分数过低,冷却2小时')

    # 买入条件
    msg = None
    if score <= trade_stock.when_buy:
        if position_ratio < trade_stock.max_position:
            # 根据情绪分数动态调整买入金额
            # 当score为-100时，系数为1；当score为when_buy时，系数为0；再根据系数计算指数缩放系数，最大按3倍金额买入
            score_factor = min(1, max(0, (trade_stock.when_buy - score) / (trade_stock.when_buy + 100)))
            score_factor = 3**(score_factor**trade_stock.buy_factor)
            buy_amount = min(request.portfolio.available_cash, trade_stock.buy_amount * score_factor)
            buy_quantity = int(buy_amount / price / 100) * 100
            if buy_quantity >= 100:
                opportunities.append(TradeOpportunity(
                    symbol=trade_stock.code,
                    name=name,
                    action="BUY",
                    quantity=buy_quantity,
                    reason=f"{name}情绪分数{score}低于买入条件{trade_stock.when_buy}，买入系数：{score_factor:.2f}"
                ))
            else:
                db.add(TradingLog(
                    account_id=account_id,
                    timestamp=current_time,
                    level='INFO',
                    message='%s 可用资金不足，跳过买入' % name
                ))
                db.commit()
        else:
            msg = "%s 持仓比例 %.2f%% 已超过限制 %.2f%%，跳过买入" % (
                    name, position_ratio, trade_stock.max_position)
            db.add(TradingLog(
                account_id=account_id,
                timestamp=current_time,
                level='INFO',
                message=msg
            ))
            db.commit()
        db.add(StockCooldown(
            account_id=account_id,
            cli_id=cli_id,
            stock_code=trade_stock.code,
            until=current_time + timedelta(hours=1),
            reason='决策后冷却1h'
        ))
    # 卖出条件
    elif score >= trade_stock.when_sell:
        if current_position and current_position.quantity >= 100:
            # 根据情绪分数动态调整卖出金额
            # 当score为99时，系数为1；当score为when_sell时，系数为0；再按系数计算卖出金额，最大按3倍卖
            score_factor = min(1, max(0, (score - trade_stock.when_sell) / (100 - trade_stock.when_sell)))
            score_factor = 3**(score_factor**trade_stock.sell_factor)
            sell_amount = trade_stock.sell_amount * score_factor
            sell_quantity = int(sell_amount / price / 100) * 100
            sell_quantity = max(min(sell_quantity, current_position.quantity), 100)
            opportunities.append(TradeOpportunity(
                symbol=trade_stock.code,
                name=name,
                action="SELL",
                quantity=sell_quantity,
                reason=f"{name}情绪分数{score}高于卖出条件{trade_stock.when_sell}，卖出系数{score_factor:.2f}"
            ))
        else:
            msg = '%s 没有持仓，跳过卖出' % name
            db.add(TradingLog(
                account_id=account_id,
                timestamp=current_time,
                level='INFO',
                message=msg
            ))
            db.commit()
        db.add(StockCooldown(
            account_id=account_id,
            cli_id=cli_id,
            stock_code=trade_stock.code,
            until=current_time + timedelta(hours=1),
            reason='决策后冷却1h'
        ))
    else:
        # 设置冷却时间
        score_delta = min(score-trade_stock.when_buy, trade_stock.when_sell-score)
        cooldown_timedelta = round(min(720, pow(1.6, score_delta)))
        if cooldown_timedelta > 1:
            db.add(StockCooldown(
                account_id=account_id,
                cli_id=cli_id,
                stock_code=trade_stock.code,
                until=current_time + timedelta(minutes=cooldown_timedelta),
                reason='情绪分数距离买卖阈值较远'
            ))
        msg = f'{name}情绪分数介于买卖阈值之间(当前:{score},买:{trade_stock.when_buy},卖:{trade_stock.when_sell}),冷却{cooldown_timedelta}分钟'
        db.add(TradingLog(
            account_id=account_id,
            timestamp=current_time,
            level='DEBUG',
            message=msg
        ))
        db.commit()
    return TradeResponse(opportunities=opportunities, msg=msg)

class LogEntry(BaseModel):
    timestamp: Optional[datetime] = None
    level: str
    message: str

class LogResponse(BaseModel):
    total: int
    items: List[LogEntry]

@router.post("/trading-logs")
async def create_trading_log(
    log: LogEntry,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db)
):
    """创建交易日志"""
    db_log = TradingLog(
        account_id=account_id,
        timestamp=datetime.now(),  # 使用服务器时间
        level=log.level,
        message=log.message
    )
    db.add(db_log)
    db.commit()
    return {"message": "Log created successfully"}

from enum import Enum

# 日志级别权重映射
LOG_LEVEL_WEIGHT = {
    "DEBUG": 0,
    "INFO": 1,
    "WARN": 2,
    "ERROR": 3
}

@router.get("/trading-logs", response_model=LogResponse)
async def get_trading_logs(
    account_id: str = Depends(valid_account),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    min_level: str = Query(None),  # 新增最小日志级别参数
    db: Session = Depends(get_db)
):
    """获取交易日志，支持分页和日志级别过滤"""
    query = db.query(TradingLog).filter(TradingLog.account_id == account_id)
    
    # 如果指定了最小日志级别，添加过滤条件
    if min_level:
        min_weight = LOG_LEVEL_WEIGHT[min_level]
        query = query.filter(
            TradingLog.level.in_([
                level for level in LOG_LEVEL_WEIGHT.keys()
                if LOG_LEVEL_WEIGHT[level] >= min_weight
            ])
        )
    
    # 获取总记录数
    total = query.count()
    
    # 获取分页数据
    logs = query.order_by(
        TradingLog.timestamp.desc()
    ).offset((page - 1) * page_size).limit(page_size).all()
    
    return LogResponse(
        total=total,
        items=[
            LogEntry(
                timestamp=log.timestamp,
                level=log.level,
                message=log.message
            ) for log in logs
        ]
    )

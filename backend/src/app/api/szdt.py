from fastapi import APIRouter, HTTPException, Depends, Header, Query, Path
from pydantic import BaseModel, Field, validator
from datetime import date
from typing import List, Dict, Optional
from ...core.database import get_db, IBKRAccountConfig, SZDTTradingConfig, SzdtTradeStock
from ...core.services.szdt import SZDTService
from ...core.services.szdt_etf_volume import get_a_share_etf_volume_metrics
from ...core.services.szdt_us_volume import get_us_stock_list_metrics
from ...core.services.szdt_a_stock_backtest import run_szdt_a_stock_backtest
from ...core.services.szdt_us_backtest import US_STOCK_TYPES, run_szdt_us_backtest
from sqlalchemy.orm import Session

router = APIRouter(prefix="/api/quant")

# 初始化SZDT服务
szdt_service = SZDTService()

# 数据模型
class StockModel(BaseModel):
    id: Optional[int] = None
    code: str
    name: str
    type: int = Field(3, ge=-1, le=8, description="ETF/类型: -1~8, 默认3(A股ETF)")
    when_buy: int = Field(..., ge=-100, le=100)
    when_sell: int = Field(..., ge=-100, le=100)
    max_position: int = Field(..., ge=0, le=100)
    buy_amount: float = Field(..., gt=0, description="买入金额必须大于0")
    sell_amount: float = Field(..., gt=0, description="卖出金额必须大于0")
    buy_factor: float = Field(..., ge=0, le=10, description="买入系数0~10")
    sell_factor: float = Field(..., ge=0, le=10, description="卖出系数0~10")
    buy_volume_ratio: float = Field(0, ge=0, le=20, description="买入量比下限，0表示不限制")
    enabled: bool = True
    lever: int = Field(..., ge=1, le=4)
    emo_area: str = Field(..., pattern="^(a|us|coin|other)$")

    @validator('when_buy', 'when_sell', 'max_position', 'type', pre=True)
    def convert_to_int(cls, v):
        if isinstance(v, str):
            return int(float(v))
        return v

    @validator('buy_amount', 'sell_amount', 'buy_factor', 'sell_factor', 'buy_volume_ratio', pre=True)
    def convert_to_float(cls, v):
        if isinstance(v, str):
            return float(v)
        return v

    @validator('buy_amount', 'sell_amount')
    def validate_amount(cls, v):
        if v <= 0:
            raise ValueError("金额必须大于0")
        return v

    class Config:
        json_schema_extra = {
            "example": {
                "code": "SH510500",
                "name": "500ETF",
                "type": 3,
                "when_buy": 0,
                "when_sell": 0,
                "max_position": 0,
                "buy_amount": 1000.0,
                "sell_amount": 1000.0,
                "lever": 1,
                "emo_area": "a"
            }
        }
        from_attributes = True

class StockCandidate(BaseModel):
    code: str
    name: str
    lever: int
    emo_area: str
    tag: Optional[str] = None
    index: Optional[str] = None


class BatchStockCandidate(BaseModel):
    """守猪逮兔列表里的一个标的，供批量创建缺失配置时使用。"""
    code: str
    name: str
    lever: int = Field(1, ge=1, le=4)
    emo_area: str = Field("a", pattern="^(a|us|coin|other)$")


class BatchStockUpdateRequest(BaseModel):
    type: int = Field(..., ge=-1, le=8)
    stocks: List[BatchStockCandidate] = Field(..., min_length=1, max_length=200)
    include_unconfigured: bool = True
    when_buy: Optional[int] = Field(None, ge=-100, le=100)
    when_sell: Optional[int] = Field(None, ge=-100, le=100)
    max_position: Optional[int] = Field(None, ge=0, le=100)
    buy_amount: Optional[float] = Field(None, gt=0)
    sell_amount: Optional[float] = Field(None, gt=0)
    buy_factor: Optional[float] = Field(None, ge=0, le=10)
    sell_factor: Optional[float] = Field(None, ge=0, le=10)
    buy_volume_ratio: Optional[float] = Field(None, ge=0, le=20)
    enabled: Optional[bool] = None

    @validator('when_buy', 'when_sell', 'max_position', 'type', pre=True)
    def convert_optional_int(cls, value):
        if value is None:
            return value
        return int(float(value)) if isinstance(value, str) else value

    @validator('buy_amount', 'sell_amount', 'buy_factor', 'sell_factor', 'buy_volume_ratio', pre=True)
    def convert_optional_float(cls, value):
        if value is None:
            return value
        return float(value) if isinstance(value, str) else value

    def update_fields(self) -> Dict[str, object]:
        return {
            field: getattr(self, field)
            for field in (
                "when_buy", "when_sell", "max_position", "buy_amount", "sell_amount",
                "buy_factor", "sell_factor", "buy_volume_ratio", "enabled",
            )
            if getattr(self, field) is not None
        }

class StockEmotionData(BaseModel):
    name: str
    price: float
    score: int
    time: str

class StockEmotionResponse(BaseModel):
    status: int
    msg: str
    data: Optional[StockEmotionData] = None

def read_stocks(account_id: str, db: Session) -> List[StockModel]:
    """读取股票列表"""
    stocks = db.query(SzdtTradeStock).filter(SzdtTradeStock.account_id == account_id).all()
    return stocks

async def get_account_id(x_account_id: Optional[str] = Header(None)) -> str:
    if not x_account_id:
        raise HTTPException(status_code=401, detail="Missing account ID")
    return x_account_id

@router.get("/trading-stocks", response_model=List[StockModel])
async def list_trading_stocks(
    account_id: str = Depends(get_account_id),
    etf_type: Optional[int] = Query(None, ge=-1, le=8, description="按类型筛选，可不传返回全部"),
    db: Session = Depends(get_db)
):
    """获取自动交易的股票列表"""
        
    # 如果开关打开，则返回股票列表
    query = db.query(SzdtTradeStock).filter(SzdtTradeStock.account_id == account_id)
    if etf_type is not None:
        query = query.filter(SzdtTradeStock.type == etf_type)
    stocks = query.all()
    return [StockModel.from_orm(stock) for stock in stocks]

# API 路由
@router.get("/stocks", response_model=List[StockModel])
async def list_stocks(
    account_id: str = Depends(get_account_id),
    etf_type: int = Query(3, ge=-1, le=8, description="类型，默认3(A股ETF)"),
    db: Session = Depends(get_db)
):
    """获取股票列表"""
    stocks = db.query(SzdtTradeStock).filter(
        SzdtTradeStock.account_id == account_id,
        SzdtTradeStock.type == etf_type
    ).all()
    return [StockModel.from_orm(stock) for stock in stocks]

@router.post("/stocks")
async def create_stock(
    stock: StockModel, 
    account_id: str = Depends(get_account_id),
    db: Session = Depends(get_db)
):
    """创建新的股票记录"""
    db_stock = SzdtTradeStock(**stock.dict())
    db_stock.account_id = account_id
    db.add(db_stock)
    db.commit()
    return {"message": "Stock Saved"}


@router.patch("/stocks/batch")
async def batch_update_stocks(
    payload: BatchStockUpdateRequest,
    account_id: str = Depends(get_account_id),
    db: Session = Depends(get_db),
):
    """批量更新当前标签的配置；可选择把尚未配置的标的以停用状态加入。"""
    updates = payload.update_fields()
    if not updates:
        raise HTTPException(status_code=400, detail="请至少填写一个要批量更新的参数")

    candidates = {}
    for candidate in payload.stocks:
        code = candidate.code.upper()
        candidates.setdefault(code, candidate)
    codes = list(candidates)
    existing = db.query(SzdtTradeStock).filter(
        SzdtTradeStock.account_id == account_id,
        SzdtTradeStock.type == payload.type,
        SzdtTradeStock.code.in_(codes),
    ).all()
    existing_by_code = {stock.code.upper(): stock for stock in existing}
    updated = created = skipped = 0
    defaults = {
        "when_buy": -60, "when_sell": 60, "max_position": 5,
        "buy_amount": 2000.0, "sell_amount": 2000.0,
        "buy_factor": 1.0, "sell_factor": 1.0, "buy_volume_ratio": 0.0,
    }

    for code, candidate in candidates.items():
        stock = existing_by_code.get(code)
        if stock:
            for field, value in updates.items():
                setattr(stock, field, value)
            updated += 1
            continue
        if not payload.include_unconfigured:
            skipped += 1
            continue
        values = {**defaults, **updates}
        # 新增配置默认停用，只有批量表单显式选择启用时才参与自动交易。
        values["enabled"] = updates.get("enabled", False)
        db.add(SzdtTradeStock(
            account_id=account_id,
            code=code,
            name=candidate.name,
            type=payload.type,
            lever=candidate.lever,
            emo_area=candidate.emo_area,
            **values,
        ))
        created += 1
    db.commit()
    return {"message": "批量设置完成", "updated": updated, "created": created, "skipped": skipped}

@router.put("/stocks/{stock_id}")
async def update_stock(
    stock_id: int, 
    stock_update: StockModel, 
    account_id: str = Depends(get_account_id),
    db: Session = Depends(get_db)
):
    """更新股票记录"""
    db_stock = db.query(SzdtTradeStock).filter(
        SzdtTradeStock.id == stock_id,
        SzdtTradeStock.account_id == account_id
    ).first()
    
    if not db_stock:
        raise HTTPException(status_code=404, detail="Stock not found")
    
    # 确保不更新id字段
    update_data = stock_update.dict(exclude_unset=True, exclude={"id"})
    for key, value in update_data.items():
        setattr(db_stock, key, value)
    
    db.commit()
    return {"message": "Stock Saved"}

@router.delete("/stocks/{stock_id}")
async def delete_stock(
    stock_id: int, 
    account_id: str = Depends(get_account_id),
    db: Session = Depends(get_db)
):
    """删除股票记录"""
    db_stock = db.query(SzdtTradeStock).filter(
        SzdtTradeStock.id == stock_id,
        SzdtTradeStock.account_id == account_id
    ).first()

    if not db_stock:
        raise HTTPException(status_code=404, detail="Stock not found")
    
    db.delete(db_stock)
    db.commit()
    return {"message": "Stock deleted"}

@router.get("/stocks/{stock_id}", response_model=StockModel)
async def get_stock(
    stock_id: int, 
    account_id: str = Depends(get_account_id),
    db: Session = Depends(get_db)
):
    """获取单个股票记录"""
    db_stock = db.query(SzdtTradeStock).filter(
        SzdtTradeStock.id == stock_id,
        SzdtTradeStock.account_id == account_id
    ).first()
    if not db_stock:
        raise HTTPException(status_code=404, detail="Stock not found")
    return db_stock

@router.get("/stock-emotion/{code}", response_model=StockEmotionResponse)
async def get_stocks_emotions(
    code: str, 
    lever: int = Query(1, ge=1, le=4),
    emo_area: str = Query('a', pattern="^(a|us|coin|other)$"),
    account_id: str = Depends(get_account_id)
):
    """获取账户股票的情绪指标"""
    try:
        
        # 使用服务获取情绪数据
        emotion = await szdt_service.get_stock_emotion(code, lever, emo_area)
        if emotion['status'] == 1:
            return StockEmotionResponse(
                status=1, 
                msg=emotion['msg'], 
                data=StockEmotionData(**emotion['data'])
            )
        else:
            return StockEmotionResponse(
                status=emotion['status'] or 1, 
                msg=emotion['msg'] or "获取情绪数据失败", 
                data=None
            )
    except Exception as e:
        print(f"Error fetching emotions: {e}")
        return StockEmotionResponse(
            status=0, 
            msg=f"获取情绪数据失败: {str(e)}", 
            data=None
        )


class ETFEmotionResponse(BaseModel):
    status: int
    msg: str
    data: Optional[List[Dict]] = None

@router.get("/etf/emotion/{etf_type}", response_model=ETFEmotionResponse)
async def get_etf_emotion(
    etf_type: int = Path(..., ge=-1, le=15, description="ETF类型: -1:我的自选, 1:美股杠杆, 2:美股常规, 3:A股ETF, 4:全球ETF, 5:港股杠杆, 6:港股常规, 7:美股个股, 8:港股个股")
):
    """获取ETF情绪指标
    
    Args:
        etf_type: ETF类型
            -1: 我的自选
            1: 美股杠杆
            2: 美股常规
            3: A股ETF
            4: 全球ETF
            5: 港股杠杆
            6: 港股常规
            7: 美股个股
            8: 港股个股
    """
    try:
        emotion = await szdt_service.get_etf_emotion(etf_type)
        if emotion:
            return ETFEmotionResponse(
                status=emotion.get('status', 0),
                msg=emotion.get('msg', '获取成功'),
                data=emotion.get('data', [])
            )
        return ETFEmotionResponse(
            status=0,
            msg="获取ETF情绪数据失败",
            data=None
        )
    except Exception as e:
        return ETFEmotionResponse(
            status=0,
            msg=f"获取ETF情绪数据失败: {str(e)}",
            data=None
        )


@router.get("/etf/volume-metrics")
async def get_etf_volume_metrics(codes: str = Query(..., description="逗号分隔的 SH.510300 / 510300.SH 代码")):
    """A 股 ETF 实时成交量与提示看板同口径的盘中量比（批量、进程缓存）。"""
    return {"data": get_a_share_etf_volume_metrics(codes.split(","))}


@router.get("/us/market-metrics")
async def get_us_market_metrics(
    codes: str = Query(..., description="逗号分隔的 US.TQQQ 代码"),
    account_id: str = Depends(get_account_id),
):
    """美股列表的批量实时成交量/成交额及持久化同刻量比。"""
    return {"data": get_us_stock_list_metrics(account_id, codes.split(","))}


class AStockSzdtBacktestRequest(BaseModel):
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    initial_capital: float = Field(1_000_000, gt=0, le=100_000_000)
    allow_leverage: bool = False


class BacktestPosition(BaseModel):
    code: str
    name: str = ""
    quantity: int = Field(..., ge=0)
    price: float = Field(..., gt=0)


class BacktestPositionSyncRequest(BaseModel):
    market: str = Field(..., pattern="^(a|us)$")
    positions: List[BacktestPosition] = Field(..., min_items=1)


@router.post("/a-stock-backtest")
async def run_a_stock_backtest(
    payload: AStockSzdtBacktestRequest,
    account_id: str = Depends(get_account_id),
    db: Session = Depends(get_db),
):
    """回测当前账户启用的守猪逮兔 A 股 ETF 配置。"""
    rows = db.query(SzdtTradeStock).filter(
        SzdtTradeStock.account_id == account_id,
        SzdtTradeStock.type == 3,
        SzdtTradeStock.enabled == True,  # noqa: E712
    ).order_by(SzdtTradeStock.id.asc()).all()
    stocks = [{
        "code": row.code, "name": row.name, "enabled": bool(row.enabled),
        "when_buy": row.when_buy, "when_sell": row.when_sell, "max_position": row.max_position,
        "buy_amount": row.buy_amount, "sell_amount": row.sell_amount,
        "buy_factor": row.buy_factor, "sell_factor": row.sell_factor,
        "buy_volume_ratio": row.buy_volume_ratio,
    } for row in rows]
    config = db.query(SZDTTradingConfig).filter(SZDTTradingConfig.account_id == account_id).first()
    sell_on_ema5_breakdown = bool(config and config.a_sell_on_ema5_breakdown)
    db.close()
    try:
        return await run_szdt_a_stock_backtest(
            stocks,
            sell_on_ema5_breakdown=sell_on_ema5_breakdown,
            initial_capital=payload.initial_capital,
            start_date=payload.start_date,
            end_date=payload.end_date,
            allow_leverage=payload.allow_leverage,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"守猪逮兔A股回测失败: {exc}") from exc


@router.post("/us-backtest")
async def run_us_backtest(
    payload: AStockSzdtBacktestRequest,
    account_id: str = Depends(get_account_id),
    db: Session = Depends(get_db),
):
    """回测当前账户启用的美股杠杆 ETF、常规 ETF 和美股个股配置。"""
    rows = db.query(SzdtTradeStock).filter(
        SzdtTradeStock.account_id == account_id,
        SzdtTradeStock.type.in_(US_STOCK_TYPES),
        SzdtTradeStock.enabled == True,  # noqa: E712
    ).order_by(SzdtTradeStock.id.asc()).all()
    stocks = [{
        "code": row.code, "name": row.name, "enabled": bool(row.enabled),
        "when_buy": row.when_buy, "when_sell": row.when_sell, "max_position": row.max_position,
        "buy_amount": row.buy_amount, "sell_amount": row.sell_amount,
        "buy_factor": row.buy_factor, "sell_factor": row.sell_factor,
        "buy_volume_ratio": row.buy_volume_ratio, "type": row.type,
    } for row in rows]
    db.close()
    try:
        return await run_szdt_us_backtest(
            stocks, account_id=account_id, initial_capital=payload.initial_capital,
            start_date=payload.start_date, end_date=payload.end_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"守猪逮兔美股回测失败: {exc}") from exc


@router.post("/backtest/sync-positions")
async def sync_backtest_positions(
    payload: BacktestPositionSyncRequest,
    account_id: str = Depends(get_account_id),
    db: Session = Depends(get_db),
):
    """将用户明确勾选的回测持仓提交为实盘目标；前端已二次确认。"""
    stock_types = [3] if payload.market == "a" else list(US_STOCK_TYPES)
    codes = [item.code.upper() for item in payload.positions]
    rows = db.query(SzdtTradeStock).filter(
        SzdtTradeStock.account_id == account_id,
        SzdtTradeStock.type.in_(stock_types),
        SzdtTradeStock.enabled == True,  # noqa: E712
        SzdtTradeStock.code.in_(codes),
    ).all()
    allowed_codes = {row.code.upper() for row in rows}
    if len(allowed_codes) != len(set(codes)):
        raise HTTPException(status_code=400, detail="所选标的必须是当前已启用的对应市场配置")
    config = db.query(SZDTTradingConfig).filter(SZDTTradingConfig.account_id == account_id).first()
    if not config:
        raise HTTPException(status_code=400, detail="未找到守猪逮兔策略配置")
    config_snapshot = {
        "id": config.id, "account_id": config.account_id,
        "external_trading_account_id": config.external_trading_account_id,
        "live_sub_account_id": config.live_sub_account_id,
        "a_sell_on_ema5_breakdown": bool(config.a_sell_on_ema5_breakdown),
        "enabled_a": bool(config.enabled_a),
        "ib_account_id": config.ib_account_id,
    }
    ib_config = db.query(IBKRAccountConfig).filter(IBKRAccountConfig.id == config.ib_account_id).first() if config.ib_account_id else None
    config_snapshot["ib_port"] = ib_config.ib_port if ib_config else 4001
    db.close()
    if payload.market == "a":
        if not config_snapshot["enabled_a"]:
            raise HTTPException(status_code=400, detail="A股守猪逮兔实盘未启用")
        from ...robot.szdt_a_stock_external_trader import SZDTAStockExternalTrader, _external_symbol

        trader = SZDTAStockExternalTrader()
        snapshot = await trader._load_external_snapshot(config_snapshot)
        submitted = []
        for item in payload.positions:
            symbol = _external_symbol(item.code)
            result = await trader._sync_target(config_snapshot, snapshot, symbol, item.quantity, item.price, "backtest_sync")
            snapshot["targets"][symbol] = item.quantity
            submitted.append({"code": item.code, "quantity": item.quantity, "result": result})
        return {"message": f"已提交 {len(submitted)} 个A股目标仓位", "submitted": submitted}

    if not config_snapshot["ib_account_id"]:
        raise HTTPException(status_code=400, detail="美股守猪逮兔未绑定 IB 账户")
    from ...robot.szdt_us_trader import SZDTUSTrader

    trader = SZDTUSTrader()
    ib_service = await trader._ensure_ib_connected(config_snapshot["ib_port"], 2000 + config_snapshot["id"])
    submitted = []
    for item in payload.positions:
        current = ib_service.get_position(item.code)
        current_quantity = int(current.get("qty") or 0)
        delta = int(item.quantity) - current_quantity
        if not delta:
            continue
        side = "BUY" if delta > 0 else "SELL"
        await ib_service.place_market_order(item.code, side, abs(delta))
        submitted.append({"code": item.code, "target_quantity": item.quantity, "side": side, "quantity": abs(delta)})
    return {"message": f"已提交 {len(submitted)} 个美股调仓订单", "submitted": submitted}

class ETFEmotionHistoryResponse(BaseModel):
    status: int
    msg: str
    data: Optional[List[Dict]] = None

@router.get("/etf/emotion/history/{code}", response_model=ETFEmotionHistoryResponse)
async def get_etf_emotion_history(
    code: str = Path(..., description="ETF代码")
):
    """获取ETF历史贪恐指数
    
    Args:
        code: ETF代码，例如：SH.510500
    """
    try:
        history = await szdt_service.get_etf_emotion_history(code)
        if history:
            return ETFEmotionHistoryResponse(
                status=history.get('status', 0),
                msg=history.get('msg', '获取成功'),
                data=history.get('data', [])
            )
        return ETFEmotionHistoryResponse(
            status=0,
            msg="获取ETF历史贪恐指数失败",
            data=None
        )
    except Exception as e:
        return ETFEmotionHistoryResponse(
            status=0,
            msg=f"获取ETF历史贪恐指数失败: {str(e)}",
            data=None
        )

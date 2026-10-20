from fastapi import APIRouter, HTTPException, Depends, Header, Query, Path
from pydantic import BaseModel, Field, validator
from datetime import date
from typing import List, Dict, Optional
from ...core.database import get_db, IBKRAccountConfig, SZDTTradingConfig, SzdtTradeStock
from ...core.services.szdt import SZDTService
from ...core.services.szdt_etf_volume import get_a_share_etf_intraday_volume_compare, get_a_share_etf_volume_metrics
from ...core.services.szdt_us_volume import get_us_stock_intraday_volume_compare, get_us_stock_list_metrics
from ...core.services.szdt_a_stock_backtest import run_szdt_a_stock_backtest
from ...core.services.szdt_us_backtest import US_STOCK_TYPES, run_szdt_us_backtest
from ...core.services.a_stock_index_valuation import load_a_stock_index_valuations
from ...robot.a_stock_base_data_config import A_STOCK_ETF_VALUATION_SYMBOLS
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

router = APIRouter(prefix="/api/quant")

# 初始化SZDT服务
szdt_service = SZDTService()


def _normalize_a_stock_etf_code(code: str) -> str:
    """把守猪逮兔的 SH.510300 格式统一为分析库使用的 510300.SH。"""
    value = str(code or "").strip().upper()
    if "." not in value:
        return value
    left, right = value.split(".", 1)
    if left in {"SH", "SZ"}:
        return f"{right}.{left}"
    return value

# 数据模型
class StockModel(BaseModel):
    id: Optional[int] = None
    code: str
    name: str
    type: int = Field(3, ge=-1, le=10, description="ETF/类型: -1~10, 默认3(A股ETF)")
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
    emo_area: str = Field(..., pattern="^(a|us|coin|sk|other)$")

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
    emo_area: str = Field("a", pattern="^(a|us|coin|sk|other)$")


class BatchStockUpdateRequest(BaseModel):
    type: int = Field(..., ge=-1, le=10)
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
    etf_type: Optional[int] = Query(None, ge=-1, le=10, description="按类型筛选，可不传返回全部"),
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
    etf_type: int = Query(3, ge=-1, le=10, description="类型，默认3(A股ETF)"),
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
    emo_area: str = Query('a', pattern="^(a|us|coin|sk|other)$"),
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
        etf_type: int = Path(..., ge=-1, le=15, description="ETF类型: -1:我的自选, 1:美股杠杆, 2:美股常规, 3:A股ETF, 4:全球ETF, 5:港股杠杆, 6:港股常规, 7:美股个股, 8:港股个股, 9:A股个股, 10:港股通")
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


@router.get("/etf/valuation-points")
async def get_etf_valuation_points(codes: str = Query(..., description="逗号分隔的 SH.510300 / 510300.SH 代码")):
    """批量返回守猪逮兔境内股票 ETF 对应指数的估值点位。

    非股票指数 ETF（债券、黄金）与 QDII 不映射，前端以空值展示，不能误用 A 股成分估值。
    """
    etf_codes = list(dict.fromkeys(
        _normalize_a_stock_etf_code(code) for code in str(codes).split(",") if str(code).strip()
    ))
    index_by_etf = {
        code: A_STOCK_ETF_VALUATION_SYMBOLS[code]
        for code in etf_codes
        if code in A_STOCK_ETF_VALUATION_SYMBOLS
    }
    valuations = await run_in_threadpool(
        lambda: load_a_stock_index_valuations(index_by_etf.values())
    ) if index_by_etf else {}
    return {
        "data": {
            etf_code: {"symbol": symbol, **valuations.get(symbol, {})}
            for etf_code, symbol in index_by_etf.items()
        }
    }


@router.get("/etf/volume-compare/{code}")
async def get_etf_volume_compare(code: str):
    """A 股 ETF 当日累计成交量与同刻 20 日基准曲线。"""
    try:
        return {"data": get_a_share_etf_intraday_volume_compare(code)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/us/market-metrics")
async def get_us_market_metrics(
    codes: str = Query(..., description="逗号分隔的 US.TQQQ 代码"),
    account_id: str = Depends(get_account_id),
):
    """美股列表的批量实时成交量/成交额及持久化同刻量比。"""
    return {"data": get_us_stock_list_metrics(account_id, codes.split(","))}


@router.get("/us/volume-compare/{code}")
async def get_us_volume_compare(code: str, account_id: str = Depends(get_account_id)):
    """美股当日累计成交量与同刻 20 日基准曲线。"""
    try:
        return {"data": get_us_stock_intraday_volume_compare(account_id, code)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class AStockSzdtBacktestRequest(BaseModel):
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    initial_capital: float = Field(1_000_000, gt=0, le=100_000_000)
    max_leverage_ratio: float = Field(1.0, ge=1.0, le=5.0, description="最高多头杠杆率；1 表示不融资")
    # 兼容旧版页面/调用。仅在未传 max_leverage_ratio 时，true 对应默认 1.5 倍。
    allow_leverage: Optional[bool] = None
    slippage_bps: float = Field(10, ge=0, le=500, description="单边滑点（基点）")


class BacktestPosition(BaseModel):
    code: str
    name: str = ""
    quantity: int = Field(..., ge=0)
    # 实盘独有持仓按 0 股清仓时可能拿不到价格，允许 0。
    price: float = Field(0, ge=0)


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
    # 实盘可能持有不在启用配置里的标的，名称回退用账户下全部配置的名称。
    stock_names = {
        code: name for code, name in db.query(SzdtTradeStock.code, SzdtTradeStock.name).filter(
            SzdtTradeStock.account_id == account_id,
        ).all() if name
    }
    config = db.query(SZDTTradingConfig).filter(SZDTTradingConfig.account_id == account_id).first()
    sell_on_ema5_breakdown = bool(config and config.a_sell_on_ema5_breakdown)
    rotation_enabled = bool(config and config.a_rotation_enabled)
    rotation_threshold = int(config.a_rotation_threshold) if config else -30
    live_config = {
        "id": config.id if config else None,
        "account_id": account_id,
        "external_trading_account_id": config.external_trading_account_id if config else None,
        "live_sub_account_id": config.live_sub_account_id if config else None,
    }
    max_leverage_ratio = payload.max_leverage_ratio
    if payload.allow_leverage and max_leverage_ratio == 1.0:
        max_leverage_ratio = 1.5
    db.close()
    try:
        result = await run_szdt_a_stock_backtest(
            stocks,
            sell_on_ema5_breakdown=sell_on_ema5_breakdown,
            rotation_enabled=rotation_enabled,
            rotation_threshold=rotation_threshold,
            initial_capital=payload.initial_capital,
            start_date=payload.start_date,
            end_date=payload.end_date,
            max_leverage_ratio=max_leverage_ratio,
            slippage_bps=payload.slippage_bps,
        )
        # 回测末日仓位与当前实盘实际持仓并列展示；读取失败不影响回测本身。
        if live_config["id"] and live_config["external_trading_account_id"] and live_config["live_sub_account_id"]:
            try:
                from ...core.services.external_trading_valuation import get_realtime_reference_prices
                from ...core.services.szdt_position_diff import a_stock_backtest_code, merge_backtest_live_positions
                from ...robot.szdt_a_stock_external_trader import SZDTAStockExternalTrader, _external_symbol

                snapshot = await SZDTAStockExternalTrader()._load_external_snapshot(live_config)
                live_positions = snapshot["positions"]
                # 实盘独有持仓没有回测价，用实时参考价补“价格”列；取不到就留空。
                live_prices = {}
                held_symbols = [symbol for symbol, quantity in live_positions.items() if int(quantity or 0) > 0]
                if held_symbols:
                    try:
                        live_prices = await get_realtime_reference_prices(
                            live_config["external_trading_account_id"], held_symbols,
                        )
                    except Exception:  # noqa: BLE001
                        live_prices = {}
                result["latest_positions"] = merge_backtest_live_positions(
                    result.get("latest_positions") or [],
                    live_positions,
                    to_live_symbol=_external_symbol,
                    to_backtest_code=a_stock_backtest_code,
                    names={_external_symbol(code): name for code, name in stock_names.items()},
                    prices=live_prices,
                )
            except Exception:  # noqa: BLE001
                # 账户未绑定、账本暂不可读等不应让只读回测失败。
                pass
        return result
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
    # 实盘可能持有不在启用配置里的标的，名称回退用账户下全部配置的名称。
    stock_names = {
        code: name for code, name in db.query(SzdtTradeStock.code, SzdtTradeStock.name).filter(
            SzdtTradeStock.account_id == account_id,
        ).all() if name
    }
    config = db.query(SZDTTradingConfig).filter(SZDTTradingConfig.account_id == account_id).first()
    rotation_enabled = bool(config and config.us_rotation_enabled)
    rotation_threshold = int(config.us_rotation_threshold) if config else -30
    ib_config = db.query(IBKRAccountConfig).filter(
        IBKRAccountConfig.id == config.ib_account_id,
    ).first() if config and config.ib_account_id else None
    live_config = {
        "id": config.id if config else None,
        "ib_port": ib_config.ib_port if ib_config else None,
    }
    db.close()
    try:
        result = await run_szdt_us_backtest(
            stocks, account_id=account_id, initial_capital=payload.initial_capital,
            start_date=payload.start_date, end_date=payload.end_date,
            rotation_enabled=rotation_enabled, rotation_threshold=rotation_threshold,
            slippage_bps=payload.slippage_bps,
        )
        # 美股没有本地持仓账本，使用独立 IB client ID 只读查询，避免占用自动交易连接。
        if live_config["id"] and live_config["ib_port"]:
            try:
                from ...core.services.szdt_position_diff import (
                    merge_backtest_live_positions,
                    us_backtest_code,
                    us_live_symbol,
                )
                from ...robot.szdt_us_trader import SZDTUSTrader

                ib_service = await SZDTUSTrader()._ensure_ib_connected(
                    live_config["ib_port"], 3_000 + live_config["id"],
                )
                # 账户里的其他策略持仓同样会被 IB 返回，一并展示，避免“实盘有、回测没有”被隐藏。
                live_positions = {}
                live_prices = {}
                for symbol, detail in ib_service.get_positions_dict().items():
                    quantity = int(float((detail or {}).get("qty") or 0))
                    if quantity <= 0:
                        continue
                    live_positions[symbol] = quantity
                    try:
                        price = float((detail or {}).get("price") or 0)
                    except (TypeError, ValueError):
                        price = 0.0
                    if price > 0:
                        live_prices[symbol] = price
                result["latest_positions"] = merge_backtest_live_positions(
                    result.get("latest_positions") or [],
                    live_positions,
                    to_live_symbol=us_live_symbol,
                    to_backtest_code=us_backtest_code,
                    names={us_live_symbol(code): name for code, name in stock_names.items()},
                    prices=live_prices,
                )
            except Exception:  # noqa: BLE001
                # IB 临时不可用时仍返回回测结果，由前端显示“-”。
                pass
        return result
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
    """将用户明确勾选的回测持仓提交为实盘目标；前端已二次确认。

    回测之外的实盘持仓（“实盘独有”）也允许提交，但只能按 0 股清仓，
    并且必须确实是当前实盘持有，避免往任意代码写目标仓位。
    """
    stock_types = [3] if payload.market == "a" else list(US_STOCK_TYPES)
    codes = [item.code.upper() for item in payload.positions]
    rows = db.query(SzdtTradeStock).filter(
        SzdtTradeStock.account_id == account_id,
        SzdtTradeStock.type.in_(stock_types),
        SzdtTradeStock.enabled == True,  # noqa: E712
        SzdtTradeStock.code.in_(codes),
    ).all()
    allowed_codes = {row.code.upper() for row in rows}
    clear_only_codes = {code for code in set(codes) if code not in allowed_codes}
    for item in payload.positions:
        if item.code.upper() in clear_only_codes and int(item.quantity) != 0:
            raise HTTPException(
                status_code=400,
                detail=f"{item.code} 不在当前启用配置中，只能按 0 股清仓",
            )
    config = db.query(SZDTTradingConfig).filter(SZDTTradingConfig.account_id == account_id).first()
    if not config:
        raise HTTPException(status_code=400, detail="未找到守猪逮兔策略配置")
    config_snapshot = {
        "id": config.id, "account_id": config.account_id,
        "external_trading_account_id": config.external_trading_account_id,
        "live_sub_account_id": config.live_sub_account_id,
        "a_sell_on_ema5_breakdown": bool(config.a_sell_on_ema5_breakdown),
        "a_rotation_enabled": bool(config.a_rotation_enabled),
        "a_rotation_threshold": int(config.a_rotation_threshold),
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
        live_symbols = {
            symbol for symbol, quantity in snapshot["positions"].items() if int(quantity or 0) > 0
        }
        for item in payload.positions:
            if item.code.upper() in clear_only_codes and _external_symbol(item.code) not in live_symbols:
                raise HTTPException(status_code=400, detail=f"{item.code} 不在实盘持仓中，无法清仓")
        # _sync_target_batch 会先保留已有目标和持仓，只覆盖用户明确勾选的目标；
        # 因此从回测同步一个子集不会把未勾选的实盘仓位变成 0。
        target_updates = {_external_symbol(item.code): item.quantity for item in payload.positions}
        prices = {_external_symbol(item.code): item.price for item in payload.positions}
        result = await trader._sync_target_batch(
            config_snapshot, snapshot, target_updates, prices, "backtest_sync",
        )
        submitted = [
            {"code": item.code, "quantity": item.quantity, "result": result}
            for item in payload.positions
        ]
        return {"message": f"已提交 {len(submitted)} 个A股目标仓位", "submitted": submitted}

    if not config_snapshot["ib_account_id"]:
        raise HTTPException(status_code=400, detail="美股守猪逮兔未绑定 IB 账户")
    from ...core.services.szdt_position_diff import us_live_symbol
    from ...robot.szdt_us_trader import SZDTUSTrader

    trader = SZDTUSTrader()
    ib_service = await trader._ensure_ib_connected(config_snapshot["ib_port"], 2000 + config_snapshot["id"])
    live_symbols = {
        symbol for symbol, detail in ib_service.get_positions_dict().items()
        if int(float((detail or {}).get("qty") or 0)) > 0
    }
    for item in payload.positions:
        if item.code.upper() in clear_only_codes and us_live_symbol(item.code) not in live_symbols:
            raise HTTPException(status_code=400, detail=f"{item.code} 不在实盘持仓中，无法清仓")
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

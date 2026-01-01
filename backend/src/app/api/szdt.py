from fastapi import APIRouter, HTTPException, Depends, Header, Query, Path
from pydantic import BaseModel, Field, validator
from typing import List, Dict, Optional
import os
from ...core.utils import get_data_file, read_json_file, write_json_file
from datetime import datetime
from ...core.database import get_db_session, TradingLog, SzdtTradeStock
from ...core.services.szdt import SZDTService
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
    lever: int = Field(..., ge=1, le=4)
    emo_area: str = Field(..., pattern="^(a|us|coin|other)$")

    @validator('when_buy', 'when_sell', 'max_position', 'type', pre=True)
    def convert_to_int(cls, v):
        if isinstance(v, str):
            return int(float(v))
        return v

    @validator('buy_amount', 'sell_amount', 'buy_factor', 'sell_factor', pre=True)
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
        orm_mode = True
        from_attributes = True

class StockCandidate(BaseModel):
    code: str
    name: str
    lever: int
    emo_area: str
    tag: Optional[str] = None
    index: Optional[str] = None

class StockEmotionData(BaseModel):
    name: str
    price: float
    score: int
    time: str

class StockEmotionResponse(BaseModel):
    status: int
    msg: str
    data: Optional[StockEmotionData] = None

# 添加新的模型
class AutoTradingStatus(BaseModel):
    enabled: bool

def get_stocks_config_path(account_id: str):
    return get_data_file(account_id, "szdt_stocks.json")

def get_auto_trading_path(account_id: str):
    """获取自动交易配置文件路径"""
    return get_data_file(account_id, "szdt_auto_trading.json")

def read_stocks(account_id: str) -> List[StockModel]:
    """读取股票列表"""
    with get_db_session(account_id) as db:
        stocks = db.query(SzdtTradeStock).all()
        return stocks

def get_auto_trading_status(account_id: str) -> bool:
    """获取自动交易状态"""
    try:
        file_path = get_auto_trading_path(account_id)
        data = read_json_file(file_path)
        return data.get('enabled', False)
    except FileNotFoundError:
        return False

async def get_account_id(x_account_id: Optional[str] = Header(None)) -> str:
    if not x_account_id:
        raise HTTPException(status_code=401, detail="Missing account ID")
    return x_account_id


@router.get("/auto-trading-status")
async def get_auto_trading_status_api(account_id: str = Depends(get_account_id)):
    """获取自动交易状态"""
    return {"enabled": get_auto_trading_status(account_id)}

@router.post("/auto-trading")
async def set_auto_trading_status(
    status: AutoTradingStatus,
    account_id: str = Depends(get_account_id)
):
    """设置自动交易状态"""
    
    file_path = get_auto_trading_path(account_id)
    write_json_file(file_path, {"enabled": status.enabled})
    return {"message": "设置成功"}

@router.get("/trading-stocks", response_model=List[StockModel])
async def list_trading_stocks(
    account_id: str = Depends(get_account_id),
    etf_type: Optional[int] = Query(None, ge=-1, le=8, description="按类型筛选，可不传返回全部")
):
    """获取自动交易的股票列表"""
    # 检查自动交易开关状态
    if not get_auto_trading_status(account_id):
        return []
    
    # 如果开关打开，则返回股票列表
    with get_db_session(account_id) as db:
        query = db.query(SzdtTradeStock)
        if etf_type is not None:
            query = query.filter(SzdtTradeStock.type == etf_type)
        stocks = query.all()
        return [StockModel.from_orm(stock) for stock in stocks]

# API 路由
@router.get("/stocks", response_model=List[StockModel])
async def list_stocks(
    account_id: str = Depends(get_account_id),
    etf_type: int = Query(3, ge=-1, le=8, description="类型，默认3(A股ETF)")
):
    """获取股票列表"""
    with get_db_session(account_id) as db:
        stocks = db.query(SzdtTradeStock).filter(SzdtTradeStock.type == etf_type).all()
        return [StockModel.from_orm(stock) for stock in stocks]

@router.post("/stocks")
async def create_stock(stock: StockModel, account_id: str = Depends(get_account_id)):
    """创建新的股票记录"""
    with get_db_session(account_id) as db:
        db_stock = SzdtTradeStock(**stock.dict())
        db.add(db_stock)
        db.commit()
        return {"message": "Stock Saved"}

@router.put("/stocks/{stock_id}")
async def update_stock(stock_id: int, stock_update: StockModel, account_id: str = Depends(get_account_id)):
    """更新股票记录"""
    with get_db_session(account_id) as db:
        db_stock = db.query(SzdtTradeStock).filter(SzdtTradeStock.id == stock_id).first()
        if not db_stock:
            raise HTTPException(status_code=404, detail="Stock not found")
        
        # 确保不更新id字段
        update_data = stock_update.dict(exclude_unset=True, exclude={"id"})
        for key, value in update_data.items():
            setattr(db_stock, key, value)
        
        db.commit()
        return {"message": "Stock Saved"}

@router.delete("/stocks/{stock_id}")
async def delete_stock(stock_id: int, account_id: str = Depends(get_account_id)):
    """删除股票记录"""
    with get_db_session(account_id) as db:
        db_stock = db.query(SzdtTradeStock).filter(SzdtTradeStock.id == stock_id).first()
        if not db_stock:
            raise HTTPException(status_code=404, detail="Stock not found")
        
        db.delete(db_stock)
        db.commit()
        return {"message": "Stock deleted"}

@router.get("/stocks/{stock_id}", response_model=StockModel)
async def get_stock(stock_id: int, account_id: str = Depends(get_account_id)):
    """获取单个股票记录"""
    with get_db_session(account_id) as db:
        db_stock = db.query(SzdtTradeStock).filter(SzdtTradeStock.id == stock_id).first()
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
    etf_type: int = Path(..., ge=-1, le=8, description="ETF类型: -1:我的自选, 1:美股杠杆, 2:美股常规, 3:A股ETF, 4:全球ETF, 5:港股杠杆, 6:港股常规, 7:美股个股, 8:港股个股")
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

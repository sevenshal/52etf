import logging

# 设置全局日志格式
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(process)d] [%(threadName)s] %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler()
    ]
)

from contextlib import asynccontextmanager
import threading
from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
from typing import List, Optional
import os  # 导入工具函数
from .api import evc, szdt, account, etf, cnn, stock, positions, trade, backtest, fed_rate, log, lev_etf_backtest, trading, ib_accounts, all_weather_backtest, ib_copy_trading, snowball, monitor, longport_accounts, external_trading_accounts, szdt_configs, scheduled_tasks, evc_accounts, soxl_fear_backtest, soxl_fear_strategy, w20_momentum_backtest, a_stock_innovation100, db_manager, factor_lab
from ..robot.main import robot
from ..core.utils import send_alert_email
import traceback

_robot_started = False
_robot_lock = threading.Lock()

# 获取环境变量，默认为开发环境
ENV = os.getenv("ENV", "dev")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动机器人后台任务
    threading.Thread(target=start_robot, daemon=True).start()
    yield
    # 清理工作（如果有）

app = FastAPI(lifespan=lifespan)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    error_msg = f"API Interface Error:\nURL: {request.url}\nMethod: {request.method}\nException: {str(exc)}\nTraceback:\n{traceback.format_exc()}"
    logging.error(f"Global Exeption Handler: {error_msg}")
    send_alert_email(f"API服务报错告警: {request.url.path}", error_msg)
    return JSONResponse(status_code=500, content={"message": "Internal Server Error"})

# 根据环境配置 CORS
if ENV == "prod":
    # 生产环境只允许特定域名
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://quant.framework.cn"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    # 开发环境允许所有源
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# 注册路由
app.include_router(evc.router)
app.include_router(szdt.router)
app.include_router(account.router)
app.include_router(etf.router)
app.include_router(cnn.router)
app.include_router(stock.router)
app.include_router(positions.router)
app.include_router(trade.router)
app.include_router(backtest.router)
app.include_router(fed_rate.router)
app.include_router(log.router)
app.include_router(lev_etf_backtest.router)
app.include_router(trading.router)
app.include_router(ib_accounts.router)
app.include_router(all_weather_backtest.router)
app.include_router(ib_copy_trading.router)
app.include_router(snowball.router)
app.include_router(monitor.router)
app.include_router(longport_accounts.router)
app.include_router(external_trading_accounts.router)
app.include_router(szdt_configs.router)
app.include_router(scheduled_tasks.router)
app.include_router(evc_accounts.router)
app.include_router(soxl_fear_backtest.router)
app.include_router(soxl_fear_strategy.router)
app.include_router(w20_momentum_backtest.router)
app.include_router(a_stock_innovation100.router)
app.include_router(db_manager.router)
app.include_router(factor_lab.router)

def start_robot():
    global _robot_started
    with _robot_lock:
        if _robot_started:
            logging.info("Robot already started in this process, skipping redundant start.")
            return
        _robot_started = True
    robot()

def start():
    import uvicorn
    uvicorn.run("src.app.main:app", host="0.0.0.0", port=8000, reload=False)
if __name__ == "__main__":
    start()

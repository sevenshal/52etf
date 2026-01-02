import logging

# 设置全局日志格式
logging.basicConfig(
    level=logging.INFO,  # 设置日志级别
    format='%(asctime)s %(levelname)s %(message)s',  # 设置日志格式
    datefmt='%Y-%m-%d %H:%M:%S',  # 设置日期格式
    handlers=[
        logging.FileHandler("/var/log/quant/app.log"),  # 将日志输出到文件
        logging.StreamHandler()  # 同时输出到控制台
    ]
)

import threading
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
from typing import List, Optional
import os  # 导入工具函数
from .api import evc, szdt, account, etf, cnn, stock, positions, trade, backtest, fed_rate, market_signal, log, lev_etf_backtest, trading, ib_accounts, all_weather_backtest, ib_copy_trading
from ..robot.main import robot

# 获取环境变量，默认为开发环境
ENV = os.getenv("ENV", "dev")

app = FastAPI()

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
app.include_router(market_signal.router)
app.include_router(log.router)
app.include_router(lev_etf_backtest.router)
app.include_router(trading.router)
app.include_router(ib_accounts.router)
app.include_router(all_weather_backtest.router)
app.include_router(ib_copy_trading.router)

def start_robot():
    robot()

# # 启动一个线程来运行定时任务
threading.Thread(target=start_robot, daemon=True).start()

def start():
    import uvicorn
    uvicorn.run("src.app.main:app", host="0.0.0.0", port=8000, reload=False)
if __name__ == "__main__":
    start()
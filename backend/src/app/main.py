import logging
from ..core.logging_config import configure_logging

configure_logging()

from contextlib import asynccontextmanager
import threading
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import os  # 导入工具函数
from .api import evc, szdt, account, etf, cnn, stock, positions, trade, backtest, fed_rate, log, lev_etf_backtest, trading, ib_accounts, all_weather_backtest, ib_copy_trading, snowball, monitor, longport_accounts, external_trading_accounts, szdt_configs, scheduled_tasks, evc_accounts, soxl_fear_backtest, soxl_fear_strategy, valuation_sim, a_stock_innovation100, a_stock_fund_flow, ai_stock, db_manager, factor_lab, events, email_settings, a_stock_fear_etf_backtest, tushare_account, realtime, a_stock_fear_strategy, system_info, fear_greed_signal_config
from ..robot.main import robot
from ..core.utils import send_alert_email, send_system_startup_email
import traceback

_robot_started = False
_robot_lock = threading.Lock()

# 获取环境变量，默认为开发环境
ENV = os.getenv("ENV", "dev")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动机器人后台任务
    threading.Thread(target=start_robot, daemon=True).start()
    # 发送系统启动通知（独立线程，避免阻塞启动）
    threading.Thread(target=send_system_startup_email, daemon=True).start()
    yield
    # 清理工作（如果有）

app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def track_account_request_usage(request: Request, call_next):
    """Record a completed API request with a short, best-effort write."""
    try:
        return await call_next(request)
    finally:
        if request.method != "OPTIONS" and request.url.path.startswith("/api/"):
            # PTrade 实时行情桥接（/api/realtime/pool）每 3s 上报一次，是机器流量，
            # 不记入账号使用量，避免热路径每次写 SQLite。
            if not request.url.path.startswith("/api/realtime/"):
                account.record_account_request(request.headers.get("X-Account-ID"))

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    error_msg = f"API Interface Error:\nURL: {request.url}\nMethod: {request.method}\nException: {str(exc)}\nTraceback:\n{traceback.format_exc()}"
    logging.error(f"Global Exeption Handler: {error_msg}")
    send_alert_email(
        f"API服务报错告警: {request.url.path}",
        error_msg,
        scenario_key="api_service_error",
    )
    return JSONResponse(status_code=500, content={"message": "Internal Server Error"})

# 根据环境配置 CORS
if ENV == "prod":
    # 生产环境只允许特定域名
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://quant.framework.cn", "https://52etf.vip"],
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
app.include_router(a_stock_fear_strategy.router)
app.include_router(valuation_sim.router)
app.include_router(a_stock_innovation100.router)
app.include_router(a_stock_fund_flow.router)
app.include_router(ai_stock.router)
app.include_router(db_manager.router)
app.include_router(factor_lab.router)
app.include_router(events.router)
app.include_router(realtime.router)
app.include_router(email_settings.router)
app.include_router(a_stock_fear_etf_backtest.router)
app.include_router(tushare_account.router)
app.include_router(system_info.router)
app.include_router(fear_greed_signal_config.router)

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

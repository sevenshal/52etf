from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends
from typing import Optional, Dict, List
from pydantic import BaseModel
from datetime import datetime
import asyncio
import multiprocessing
from ...robot.etf_backtest import ETFBacktest
from ...core.event_stream import publish_event
from .account import valid_admin_account
import logging
from logging.handlers import RotatingFileHandler
import os


router = APIRouter(prefix="/api/backtest")

# 使用字典存储每个会话的回测状态
backtest_sessions = {}

class BacktestSession:
    def __init__(self):
        self.process = None
        self.status = {
            "is_running": False,
            "progress": 0,
            "result": None,
            "error": None,
            "start_time": None,
            "end_time": None
        }


def _publish_backtest_status(session_id: str):
    session = backtest_sessions.get(session_id)
    if not session:
        return
    publish_event(session_id, "fear_backtest_status", dict(session.status))

class ETFParams(BaseModel):
    max_position_ratio: float
    trade_amount: float
    buy_score: int
    sell_score: int

class BacktestParams(BaseModel):
    initial_cash: float = 1000000
    max_position_range: tuple = (0, 1, 0.01)
    trade_amount_range: tuple = (10000, 100000, 10000)
    buy_score_range: tuple = (-100, -50, 5)
    sell_score_range: tuple = (50, 100, 5)
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    etf_list: List[Dict]  # 修改为直接传入ETF列表

class VerifyParams(BaseModel):
    initial_cash: float
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    etf_params: Dict[str, ETFParams]

# 在文件开头添加日志配置
def setup_logger():
    log_dir = "/var/log/quant"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    logger = logging.getLogger('backtest')
    logger.setLevel(logging.INFO)
    
    # 文件处理器
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, 'backtest.log'),
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    file_handler.setLevel(logging.INFO)
    
    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    
    # 设置日志格式
    formatter = logging.Formatter('%(asctime)s - %(processName)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger

logger = setup_logger()

def run_backtest_process(session_id: str, params_dict: dict, result_queue: multiprocessing.Queue, progress_queue: multiprocessing.Queue):
    """在单独的进程中运行回测"""
    try:
        # 在子进程中重新初始化logger
        logger.info(f"开始回测任务 - 会话ID: {session_id}")
        logger.info(f"回测参数: {params_dict}")

        def update_progress(progress: float):
            progress_queue.put(progress)
            logger.info(f"回测进度: {progress}%")

        backtest = ETFBacktest(
            initial_cash=params_dict['initial_cash'],
            max_position_range=params_dict['max_position_range'],
            trade_amount_range=params_dict['trade_amount_range'],
            buy_score_range=params_dict['buy_score_range'],
            sell_score_range=params_dict['sell_score_range'],
            start_date=params_dict['start_date'],
            end_date=params_dict['end_date'],
            progress_callback=update_progress
        )
        
        # 由于在新进程中，需要重新创建事件循环
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        logger.info("开始寻找最优参数")
        result = loop.run_until_complete(backtest.find_best_parameters(params_dict['etf_list']))
        logger.info(f"回测完成，结果: {result}")
        result_queue.put(('success', result))
    except Exception as e:
        logger.error(f"回测过程发生错误: {str(e)}", exc_info=True)
        result_queue.put(('error', str(e)))

async def monitor_process(session_id: str, result_queue: multiprocessing.Queue, progress_queue: multiprocessing.Queue):
    """监控回测进程并更新状态"""
    session = backtest_sessions[session_id]
    try:
        while session.process.is_alive():
            # 检查进度更新
            has_progress_update = False
            while not progress_queue.empty():
                progress = progress_queue.get()
                session.status["progress"] = progress
                has_progress_update = True
            if has_progress_update:
                _publish_backtest_status(session_id)
            await asyncio.sleep(2)  # 缩短检查间隔以更及时更新进度
            
        # 进程结束后获取结果
        if not result_queue.empty():
            status, data = result_queue.get()
            if status == 'success':
                session.status["result"] = data
            else:
                session.status["error"] = data
    finally:
        session.status["is_running"] = False
        session.status["end_time"] = datetime.now()
        session.process = None
        _publish_backtest_status(session_id)

def run_verify_process(session_id: str, params_dict: dict, result_queue: multiprocessing.Queue, progress_queue: multiprocessing.Queue):
    """在单独的进程中运行验证"""
    try:
        # 在子进程中重新初始化logger
        logger.info(f"开始验证任务 - 会话ID: {session_id}")
        logger.info(f"验证参数: {params_dict}")

        def update_progress(progress: float):
            progress_queue.put(progress)
            logger.info(f"验证进度: {progress}%")

        backtest = ETFBacktest(
            initial_cash=params_dict['initial_cash'],
            start_date=params_dict['start_date'],
            end_date=params_dict['end_date'],
            progress_callback=update_progress
        )
        
        # 由于在新进程中，需要重新创建事件循环
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # 获取所有ETF的历史数据
        etf_histories = {}
        for code in params_dict['etf_params'].keys():
            history = loop.run_until_complete(backtest.fetch_etf_history(code))
            if history:
                etf_histories[code] = history
        
        if not etf_histories:
            raise Exception("获取ETF历史数据失败")
        
        # 构建回测参数
        etf_params = {
            code: ETFParams(
                max_position_ratio=params_dict['etf_params'][code]['max_position_ratio'],
                trade_amount=params_dict['etf_params'][code]['trade_amount'],
                buy_score=params_dict['etf_params'][code]['buy_score'],
                sell_score=params_dict['etf_params'][code]['sell_score']
            )
            for code in etf_histories.keys()
        }
        
        # 执行回测
        result = backtest.backtest_portfolio(
            etf_histories,
            etf_params
        )
        
        logger.info(f"验证完成，结果: {result}")
        result_queue.put(('success', result))
    except Exception as e:
        logger.error(f"验证过程发生错误: {str(e)}", exc_info=True)
        result_queue.put(('error', str(e)))

@router.post("/start")
async def start_backtest(
    params: BacktestParams, 
    background_tasks: BackgroundTasks,
    account_id: str = Depends(valid_admin_account)
):
    """启动回测任务"""
    session_id = account_id
    
    # 检查是否已有会话
    if session_id in backtest_sessions and backtest_sessions[session_id].status["is_running"]:
        raise HTTPException(status_code=400, detail="已有回测任务正在进行中")
    
    # 创建新会话或重置现有会话
    if session_id not in backtest_sessions:
        backtest_sessions[session_id] = BacktestSession()
    session = backtest_sessions[session_id]
    
    # 重置状态
    session.status = {
        "is_running": True,
        "progress": 0,
        "result": None,
        "error": None,
        "start_time": datetime.now(),
        "end_time": None
    }
    _publish_backtest_status(session_id)
    
    # 创建结果队列和进度队列
    result_queue = multiprocessing.Queue()
    progress_queue = multiprocessing.Queue()
    
    # 将参数转换为字典
    params_dict = params.dict()
    
    # 创建新进程运行回测
    session.process = multiprocessing.Process(
        target=run_backtest_process,
        args=(session_id, params_dict, result_queue, progress_queue)
    )
    session.process.daemon = True  # 添加这行，设置为守护进程
    session.process.start()
    
    # 启动监控任务
    background_tasks.add_task(monitor_process, session_id, result_queue, progress_queue)
    
    return {"message": "回测任务已启动"}

@router.post("/cancel")
async def cancel_backtest(account_id: str = Depends(valid_admin_account)):
    """取消回测任务"""
    session_id = account_id
    if session_id not in backtest_sessions or not backtest_sessions[session_id].status["is_running"]:
        raise HTTPException(status_code=400, detail="没有正在进行的回测任务")
    
    session = backtest_sessions[session_id]
    if session.process and session.process.is_alive():
        session.process.terminate()
        session.process.join()
        session.status["is_running"] = False
        session.status["error"] = "任务已取消"
        session.status["end_time"] = datetime.now()
        session.process = None
        _publish_backtest_status(session_id)
    
    return {"message": "回测任务已取消"}

@router.post("/verify")
async def verify_backtest_params(
    params: VerifyParams,
    background_tasks: BackgroundTasks,
    account_id: str = Depends(valid_admin_account)
):
    """验证回测参数"""
    session_id = account_id
    
    # 检查是否已有会话
    if session_id in backtest_sessions and backtest_sessions[session_id].status["is_running"]:
        raise HTTPException(status_code=400, detail="已有回测任务正在进行中")
    
    # 创建新会话或重置现有会话
    if session_id not in backtest_sessions:
        backtest_sessions[session_id] = BacktestSession()
    session = backtest_sessions[session_id]
    
    # 重置状态
    session.status = {
        "is_running": True,
        "progress": 0,
        "result": None,
        "error": None,
        "start_time": datetime.now(),
        "end_time": None
    }
    _publish_backtest_status(session_id)
    
    # 创建结果队列和进度队列
    result_queue = multiprocessing.Queue()
    progress_queue = multiprocessing.Queue()
    
    # 将参数转换为字典
    params_dict = params.dict()
    
    # 创建新进程运行验证
    session.process = multiprocessing.Process(
        target=run_verify_process,
        args=(session_id, params_dict, result_queue, progress_queue)
    )
    session.process.daemon = True
    session.process.start()
    
    # 启动监控任务
    background_tasks.add_task(monitor_process, session_id, result_queue, progress_queue)
    
    return {"message": "验证任务已启动"}

@router.get("/status")
async def get_backtest_status(account_id: str = Depends(valid_admin_account)):
    """获取回测任务状态"""
    session_id = account_id
    
    if session_id not in backtest_sessions:
        return {"error": "未找到回测会话", "is_running": False}
        
    session = backtest_sessions[session_id]
    
    return session.status

from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel
from typing import List, Dict, Optional, Union
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import uuid
import hashlib
import json
from ...core.services.longport import LongPortService
from ...core.services.quote import QuoteService
from .account import valid_account

router = APIRouter(prefix="/api/lev-etf-backtest", tags=["Leveraged ETF Backtest"])

# Simple In-Memory Job Store
# Structure: { task_id: { "status": "pending"|"running"|"completed"|"failed", "result": [...], "error": str, "timestamp": datetime } }
JOBS = {}

class LevETFBacktestParams(BaseModel):
    etf_code: str
    short_window: int = 5
    long_window: int = 30
    initial_capital: float = 10000.0
    start_date: Optional[str] = "2015-01-01"
    end_date: Optional[str] = None

class BacktestResult(BaseModel):
    total_return: float
    annualized_return: float
    max_drawdown: float
    win_rate: float
    total_trades: int
    sharpe_ratio: float
    trades: List[Dict]
    daily_data: List[Dict]
    equity_curve: List[Dict]
    yearly_returns: List[Dict]
    params: Dict

class BatchBacktestParams(BaseModel):
    etf_code: str
    short_window_min: int = 1
    short_window_max: int = 10
    long_window_min: int = 11
    long_window_max: int = 60
    initial_capital: float = 10000.0
    start_date: Optional[str] = "2015-01-01"
    end_date: Optional[str] = None

class BatchBacktestResultItem(BaseModel):
    short_window: int
    long_window: int
    total_return: float
    annualized_return: float
    max_drawdown: float
    win_rate: float
    total_trades: int
    sharpe_ratio: float

class AsyncJobResponse(BaseModel):
    task_id: str
    status: str

class AsyncJobStatus(BaseModel):
    status: str
    result: Optional[List[BatchBacktestResultItem]] = None
    error: Optional[str] = None
    progress: int = 0  # 0 to 100

def get_params_hash(params: BatchBacktestParams) -> str:
    # Create a deterministic hash of the parameters to deduplicate jobs
    params_str = json.dumps(params.dict() if hasattr(params, 'dict') else params.model_dump(), sort_keys=True)
    return hashlib.md5(params_str.encode()).hexdigest()

def calculate_strategy_metrics(df: pd.DataFrame, short_window: int, long_window: int, initial_capital: float, start_date: datetime.date, detailed: bool = False) -> Dict:
    # Copy DF to avoid modifying original if reused
    df = df.copy()
    
    # Calculate EMAs (on full data)
    df['EMA_short'] = df['price'].ewm(span=short_window, adjust=False).mean()
    df['EMA_long'] = df['price'].ewm(span=long_window, adjust=False).mean()
    
    # Filter by user's requested start date
    df = df[df['date'].dt.date >= start_date]
    df = df.sort_values('date').reset_index(drop=True)

    if df.empty:
        return {
            "total_return": 0.0,
            "annualized_return": 0.0,
            "max_drawdown": 0.0,
            "win_rate": 0.0,
            "total_trades": 0,
            "trades": [],
            "equity_curve": [],
            "daily_data": [],
            "yearly_returns": []
        }
    
    # Generate Signals
    df['signal'] = np.where(df['EMA_short'] > df['EMA_long'], 1.0, 0.0)
    df['position_diff'] = df['signal'].diff()
    
    # Backtest Loop
    capital = initial_capital
    position = 0.0
    trades = []
    equity_curve = []
    daily_data = [] 
    
    peak = capital
    max_drawdown = 0.0
    
    has_position = False
    entry_price = 0.0
    
    # Trade Stats
    win_count = 0
    total_closed_trades = 0
    equity_values = []
    
    for i, row in df.iterrows():
        date = row['date']
        price = row['price']
        
        # Check signals
        if row['position_diff'] == 1.0 and not has_position:
            # Buy All
            position = capital / price
            entry_price = price
            cost = capital
            capital = 0.0
            has_position = True
            if detailed:
                trades.append({
                    "date": date.strftime("%Y-%m-%d"),
                    "action": "BUY",
                    "price": price,
                    "amount": cost,
                    "quantity": position,
                    "profit": 0.0,
                    "percent": 0.0
                })
            
        elif row['position_diff'] == -1.0 and has_position:
            # Sell All
            revenue = position * price
            profit = revenue - (position * entry_price)
            profit_percent = (price - entry_price) / entry_price * 100
            
            # Update Stats
            total_closed_trades += 1
            if profit > 0:
                win_count += 1
            
            capital = revenue
            position = 0.0
            has_position = False
            
            if detailed:
                trades.append({
                    "date": date.strftime("%Y-%m-%d"),
                    "action": "SELL",
                    "price": price,
                    "amount": revenue,
                    "quantity": 0,
                    "profit": profit,
                    "percent": profit_percent
                })
            
        current_equity = capital + (position * price)
        equity_values.append(current_equity)
        
        if detailed:
            equity_curve.append({
                "date": date.strftime("%Y-%m-%d"),
                "value": current_equity,
                "benchmark": 0
            })
            
            daily_data.append({
                "date": date.strftime("%Y-%m-%d"),
                "open": row['open'],
                "high": row['high'],
                "low": row['low'],
                "close": row['price'],
                "ema_short": row['EMA_short'],
                "ema_long": row['EMA_long']
            })
        
        # Max Drawdown
        if current_equity > peak:
            peak = current_equity
        dd = (peak - current_equity) / peak
        if dd > max_drawdown:
            max_drawdown = dd

    # Final Stats
    final_equity = current_equity if 'current_equity' in locals() else initial_capital
    total_return = (final_equity - initial_capital) / initial_capital * 100
    
    days = (df['date'].iloc[-1] - df['date'].iloc[0]).days
    if days > 0:
        annualized_return = ((1 + total_return/100) ** (365/days) - 1) * 100
    else:
        annualized_return = 0.0
        
    win_rate = (win_count / total_closed_trades * 100) if total_closed_trades > 0 else 0.0

    yearly_returns = []
    if detailed and equity_curve:
        # Calculate Yearly Returns
        dates = [pd.to_datetime(item['date']) for item in equity_curve]
        values = [item['value'] for item in equity_curve]
        equity_series = pd.Series(values, index=dates)
        resampled = equity_series.resample('YE').last()
        
        previous_value = initial_capital
        for date_idx, value in resampled.items():
            year = date_idx.year
            ret = (value - previous_value) / previous_value * 100
            yearly_returns.append({
                "year": str(year),
                "return": ret
            })
            previous_value = value
            
    # Sharpe Ratio
    sharpe_ratio = 0.0
    if equity_values:
        equity_series = pd.Series(equity_values)
        daily_rets = equity_series.pct_change().dropna()
        if not daily_rets.empty and daily_rets.std() > 0:
            sharpe_ratio = (daily_rets.mean() / daily_rets.std()) * np.sqrt(252)

    return {
        "total_return": total_return,
        "annualized_return": annualized_return,
        "max_drawdown": max_drawdown * 100,
        "win_rate": win_rate,
        "total_trades": total_closed_trades,
        "sharpe_ratio": sharpe_ratio,
        "trades": trades,
        "equity_curve": equity_curve,
        "daily_data": daily_data,
        "yearly_returns": yearly_returns,
    }

def background_batch_backtest(task_id: str, params: BatchBacktestParams, account_id: str):
    print(f"Starting background task {task_id} with params: {params}")
    try:
        # Update status to running
        JOBS[task_id]["status"] = "running"
        JOBS[task_id]["progress"] = 0

        # Initialize Services
        trade_service = LongPortService(account_id)
        quote_service = QuoteService(trade_service)

        symbol = params.etf_code if params.etf_code.endswith('.US') else f"{params.etf_code}.US"

        # Date Logic
        end_date = datetime.strptime(params.end_date, "%Y-%m-%d").date() if params.end_date else datetime.now().date()
        user_start_date = datetime.strptime(params.start_date, "%Y-%m-%d").date() if params.start_date else datetime.strptime("2015-01-01", "%Y-%m-%d").date()

        # Buffer: max needed for longest window
        buffer_days = max(60, params.long_window_max * 3)
        fetch_start_date = user_start_date - timedelta(days=buffer_days)

        klines_data = quote_service.get_klines(symbol, start_date=fetch_start_date, end_date=end_date, period='d')

        if not klines_data:
             raise ValueError(f"No data found for {symbol}")

        df = pd.DataFrame(klines_data)
        df['date'] = pd.to_datetime(df['timestamp'])
        df['price'] = df['close'].astype(float)
        df['open'] = df['open'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        df = df.sort_values('date').reset_index(drop=True)

        results = []
        
        # Calculate Total Combinations
        total_combinations = 0
        for short in range(params.short_window_min, params.short_window_max + 1):
            for long in range(params.long_window_min, params.long_window_max + 1):
                if short < long:
                    total_combinations += 1
        
        processed_count = 0
        
        # Iterate short/long windows
        for short in range(params.short_window_min, params.short_window_max + 1):
            for long in range(params.long_window_min, params.long_window_max + 1):
                if short >= long:
                    continue
                    
                metrics = calculate_strategy_metrics(
                    df, 
                    short, 
                    long, 
                    params.initial_capital, 
                    start_date=user_start_date,
                    detailed=False
                )
                
                results.append({
                    "short_window": short,
                    "long_window": long,
                    "total_return": metrics['total_return'],
                    "annualized_return": metrics['annualized_return'],
                    "max_drawdown": metrics['max_drawdown'],
                    "win_rate": metrics['win_rate'],
                    "total_trades": metrics['total_trades'],
                    "sharpe_ratio": metrics['sharpe_ratio']
                })
                
                processed_count += 1
                if total_combinations > 0:
                     JOBS[task_id]["progress"] = int((processed_count / total_combinations) * 100)

        JOBS[task_id]["result"] = results
        JOBS[task_id]["status"] = "completed"
        JOBS[task_id]["progress"] = 100
        print(f"Task {task_id} completed successfully.")

    except Exception as e:
        print(f"Task {task_id} failed: {e}")
        JOBS[task_id]["status"] = "failed"
        JOBS[task_id]["error"] = str(e)


@router.post("/batch-run", response_model=AsyncJobResponse)
async def start_batch_backtest(
    params: BatchBacktestParams,
    background_tasks: BackgroundTasks,
    account_id: str = Depends(valid_account)
):
    # Determine Task ID (Use Hash for deduplication or UUID for uniqueness per request)
    # Using Hash allows caching if parameters are identical
    task_id = get_params_hash(params)
    
    # Check if job exists
    if task_id in JOBS:
        job = JOBS[task_id]
        if job["status"] in ["pending", "running", "completed"]:
            return {"task_id": task_id, "status": job["status"]}
        # If failed, we might want to retry, so fall through to start new one
    
    # Init Job
    JOBS[task_id] = {
        "status": "pending",
        "result": None,
        "error": None,
        "timestamp": datetime.now(),
        "progress": 0
    }
    
    # Start Background Task
    background_tasks.add_task(background_batch_backtest, task_id, params, account_id)
    
    return {"task_id": task_id, "status": "pending"}

@router.get("/batch-run/{task_id}", response_model=AsyncJobStatus)
async def get_batch_backtest_status(task_id: str):
    if task_id not in JOBS:
        raise HTTPException(status_code=404, detail="Task not found")
        
    job = JOBS[task_id]
    return {
        "status": job["status"],
        "result": job["result"],
        "error": job["error"],
        "progress": job.get("progress", 0)
    }

@router.post("/run", response_model=BacktestResult)
async def run_lev_etf_backtest(
    params: LevETFBacktestParams,
    account_id: str = Depends(valid_account)
):
    # Initialize Services
    try:
        trade_service = LongPortService(account_id)
        quote_service = QuoteService(trade_service)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to initialize services: {str(e)}")

    # Prepare Symbol
    symbol = params.etf_code
    if not symbol.endswith('.US'):
        symbol = f"{symbol}.US"

    # Fetch History
    try:
        # Determine Date Range
        end_date = datetime.now().date()
        if params.end_date:
            end_date = datetime.strptime(params.end_date, "%Y-%m-%d").date()
            
        # User requested start date
        user_start_date = datetime.strptime("2010-01-01", "%Y-%m-%d").date()
        if params.start_date:
            user_start_date = datetime.strptime(params.start_date, "%Y-%m-%d").date()

        # Add buffer for EMA calculation (approx 2x long_window * 1.5 for weekends)
        # 30 day MA -> need ~45 days prior. Let's be safe and use max(60, long_window * 3) days.
        buffer_days = max(60, params.long_window * 3)
        fetch_start_date = user_start_date - timedelta(days=buffer_days)

        # Use new get_klines signature
        klines_data = quote_service.get_klines(
            symbol, 
            start_date=fetch_start_date, 
            end_date=end_date,
            period='d'
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch data for {symbol}: {str(e)}")
    
    if not klines_data:
        raise HTTPException(status_code=404, detail=f"No data found for {symbol}")
        
    # Convert list of dicts to DataFrame
    df = pd.DataFrame(klines_data)
    
    # Validation
    required_cols = ['timestamp', 'open', 'high', 'low', 'close']
    if not all(col in df.columns for col in required_cols):
        raise HTTPException(status_code=500, detail="Invalid data format received from data provider")

    # Rename columns to match logic
    df['date'] = pd.to_datetime(df['timestamp'])
    df['price'] = df['close'].astype(float)
    df['open'] = df['open'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    
    # Sort and reset index
    df = df.sort_values('date').reset_index(drop=True)

    result = calculate_strategy_metrics(
        df, 
        params.short_window, 
        params.long_window, 
        params.initial_capital, 
        start_date=user_start_date,
        detailed=True
    )
    
    print(f"DEBUG: Calculated metrics for {symbol}. Total Return: {result.get('total_return')}")
    
    # Ensure params is a dict
    params_dict = params.dict() if hasattr(params, 'dict') else params.model_dump()
    
    final_response = {
        **result,
        "params": params_dict
    }
    
    # Validation check (Manual)
    if final_response is None:
        print("CRITICAL: final_response is None!")
        raise HTTPException(status_code=500, detail="Internal Error: Result is None")
        
    return final_response

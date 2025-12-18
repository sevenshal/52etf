from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Dict, Optional
import pandas as pd
import numpy as np
from datetime import datetime
from ...core.services.longport import LongPortService
from ...core.services.quote import QuoteService
from .account import valid_account

router = APIRouter(prefix="/api/lev-etf-backtest", tags=["Leveraged ETF Backtest"])

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
    trades: List[Dict]
    daily_data: List[Dict]
    equity_curve: List[Dict]
    params: Dict

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

    # Fetch History (Fetch approx 20 years to be safe, e.g. 5000 trading days)
    try:
        # Using count=5000 to cover enough history since 2015
        klines_data = quote_service.get_klines(symbol, count=5000, period='d')
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
    df = df.sort_values('date').reset_index(drop=True)
    
    # Filter by date
    if params.start_date:
        df = df[df['date'] >= pd.to_datetime(params.start_date)]
    if params.end_date:
        df = df[df['date'] <= pd.to_datetime(params.end_date)]
        
    if df.empty:
        raise HTTPException(status_code=400, detail="No data in selected date range")

    # Calculate EMAs
    df['EMA_short'] = df['price'].ewm(span=params.short_window, adjust=False).mean()
    df['EMA_long'] = df['price'].ewm(span=params.long_window, adjust=False).mean()
    
    # Generate Signals
    # 1: Short > Long, 0: Short <= Long
    df['signal'] = np.where(df['EMA_short'] > df['EMA_long'], 1.0, 0.0)
    # diff: 1.0 means 0 -> 1 (Buy), -1.0 means 1 -> 0 (Sell)
    df['position_diff'] = df['signal'].diff()
    
    # Backtest Loop
    capital = params.initial_capital
    position = 0.0
    trades = []
    equity_curve = []
    daily_data = [] # New list for frontend visualization
    
    # Track performance
    peak = capital
    max_drawdown = 0.0
    
    # State
    has_position = False
    entry_price = 0.0
    
    for i, row in df.iterrows():
        date = row['date']
        price = row['price']
        
        # Check signals
        # Buy Signal (Crossover Up)
        if row['position_diff'] == 1.0 and not has_position:
            # Buy All
            position = capital / price
            entry_price = price
            cost = capital
            capital = 0.0
            has_position = True
            trades.append({
                "date": date.strftime("%Y-%m-%d"),
                "action": "BUY",
                "price": price,
                "amount": cost,
                "quantity": position,
                "profit": 0.0,
                "percent": 0.0
            })
            
        # Sell Signal (Crossover Down)
        elif row['position_diff'] == -1.0 and has_position:
            # Sell All
            revenue = position * price
            profit = revenue - (position * entry_price)
            profit_percent = (price - entry_price) / entry_price * 100
            
            capital = revenue
            position = 0.0
            has_position = False
            
            trades.append({
                "date": date.strftime("%Y-%m-%d"),
                "action": "SELL",
                "price": price,
                "amount": revenue,
                "quantity": 0,
                "profit": profit,
                "percent": profit_percent
            })
            
        # Update Equity
        current_equity = capital + (position * price)
        equity_curve.append({
            "date": date.strftime("%Y-%m-%d"),
            "value": current_equity,
            "benchmark": 0
        })
        
        # Collect Daily Data for Visualization
        daily_data.append({
            "date": date.strftime("%Y-%m-%d"),
            "open": row['open'],
            "high": row['high'],
            "low": row['low'],
            "close": row['price'], # price is close
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
    final_equity = equity_curve[-1]['value']
    total_return = (final_equity - params.initial_capital) / params.initial_capital * 100
    
    # Annualized Return
    days = (df['date'].iloc[-1] - df['date'].iloc[0]).days
    if days > 0:
        annualized_return = ((1 + total_return/100) ** (365/days) - 1) * 100
    else:
        annualized_return = 0.0
        
    # Win Rate
    winning_trades = [t for t in trades if t['action'] == 'SELL' and t['profit'] > 0]
    total_sell_trades = len([t for t in trades if t['action'] == 'SELL'])
    win_rate = (len(winning_trades) / total_sell_trades * 100) if total_sell_trades > 0 else 0.0
    
    return {
        "total_return": total_return,
        "annualized_return": annualized_return,
        "max_drawdown": max_drawdown * 100,
        "win_rate": win_rate,
        "total_trades": len(trades),
        "trades": trades,
        "equity_curve": equity_curve,
        "daily_data": daily_data,
        "params": params.dict()
    }

    # Final Stats
    final_equity = equity_curve[-1]['value']
    total_return = (final_equity - params.initial_capital) / params.initial_capital * 100
    
    # Annualized Return
    days = (df['date'].iloc[-1] - df['date'].iloc[0]).days
    if days > 0:
        annualized_return = ((1 + total_return/100) ** (365/days) - 1) * 100
    else:
        annualized_return = 0.0
        
    # Win Rate
    winning_trades = [t for t in trades if t['action'] == 'SELL' and t['profit'] > 0]
    total_sell_trades = len([t for t in trades if t['action'] == 'SELL'])
    win_rate = (len(winning_trades) / total_sell_trades * 100) if total_sell_trades > 0 else 0.0
    
    return {
        "total_return": total_return,
        "annualized_return": annualized_return,
        "max_drawdown": max_drawdown * 100,
        "win_rate": win_rate,
        "total_trades": len(trades),
        "trades": trades,
        "equity_curve": equity_curve,
        "params": params.dict()
    }

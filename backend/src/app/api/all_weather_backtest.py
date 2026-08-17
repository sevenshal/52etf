from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Optional
import pandas as pd
import numpy as np
from datetime import datetime, date
from dateutil.relativedelta import relativedelta
from ...core.services.longport import LongPortService
from ...core.services.quote import QuoteService
from .account import valid_admin_account

router = APIRouter(prefix="/api/all-weather-backtest", tags=["All-Weather Backtest"])

class AssetWeight(BaseModel):
    symbol: str
    weight: float

class AllWeatherParams(BaseModel):
    assets: List[AssetWeight] = [
        {"symbol": "SPMO.US", "weight": 0.3},
        {"symbol": "TLT.US", "weight": 0.4},
        {"symbol": "IEF.US", "weight": 0.15},
        {"symbol": "GLD.US", "weight": 0.075},
        {"symbol": "DBC.US", "weight": 0.075}
    ]
    initial_capital: float = 10000.0
    rebalance_months: int = 6  # 0 to disable time-based rebalance
    drift_threshold: float = 0.2  # Relative threshold (e.g. 20% deviation)
    start_date: str = "2015-01-01"
    end_date: Optional[str] = None

@router.post("")
async def run_all_weather_backtest(params: AllWeatherParams, account_id: str = Depends(valid_admin_account)):
    trade_service = LongPortService.get_instance()
    quote_service = QuoteService(trade_service)
    
    start_dt = datetime.strptime(params.start_date, "%Y-%m-%d").date()
    end_dt = datetime.strptime(params.end_date, "%Y-%m-%d").date() if params.end_date else date.today()
    
    # 1. Fetch data for all assets
    all_data = {}
    for asset in params.assets:
        try:
            # Add .US suffix if missing for consistency
            symbol = asset.symbol if asset.symbol.endswith('.US') else asset.symbol + '.US'
            data = quote_service.get_klines(symbol, start_date=start_dt, end_date=end_dt)
            if not data:
                continue
            df = pd.DataFrame(data)
            df['date'] = df['timestamp'].dt.date
            df = df.set_index('date')[['close']]
            df.columns = [symbol]
            all_data[symbol] = df
        except Exception as e:
            print(f"Error fetching data for {asset.symbol}: {e}")
            continue

    if not all_data:
        raise HTTPException(status_code=400, detail="Could not fetch data for any of the specified assets")

    # 2. Align data
    combined_df = pd.concat(all_data.values(), axis=1).sort_index().dropna()
    if combined_df.empty:
        raise HTTPException(status_code=400, detail="Overlapping data range is too small or non-existent")

    # 3. Simulation
    initial_capital = params.initial_capital

    # Initialize target weights and holdings
    target_weights = { (a.symbol if a.symbol.endswith('.US') else a.symbol + '.US'): a.weight for a in params.assets }
    # Normalize weights just in case
    total_w = sum(target_weights.values())
    target_weights = { k: v/total_w for k, v in target_weights.items() }
    
    symbols = combined_df.columns.tolist()
    first_prices = combined_df.iloc[0]
    
    # Initial allocation
    shares = { sym: (initial_capital * target_weights[sym]) / first_prices[sym] for sym in symbols if sym in target_weights }
    
    equity_curve = []
    rebalance_events = []
    
    last_rebalance_date = combined_df.index[0]
    
    for current_date, prices in combined_df.iterrows():
        # Calculate current value
        portfolio_value = sum(shares[sym] * prices[sym] for sym in symbols)
        
        # Check for rebalance
        trigger_rebalance = False
        reason = ""
        
        # Drift-based check
        if params.drift_threshold > 0:
            for sym in symbols:
                actual_weight = (shares[sym] * prices[sym]) / portfolio_value
                target_w = target_weights[sym]
                if abs(actual_weight - target_w) / target_w > params.drift_threshold:
                    trigger_rebalance = True
                    reason = f"Drift: {sym} deviated > {params.drift_threshold*100}%"
                    break
        
        # Time-based check
        if not trigger_rebalance and params.rebalance_months > 0:
            if current_date >= last_rebalance_date + relativedelta(months=params.rebalance_months):
                trigger_rebalance = True
                reason = f"Periodic: {params.rebalance_months} months passed"

        if trigger_rebalance:
            # Rebalance
            new_shares = {}
            for sym in symbols:
                new_shares[sym] = (portfolio_value * target_weights[sym]) / prices[sym]
            shares = new_shares
            last_rebalance_date = current_date
            rebalance_events.append({
                "date": current_date.isoformat(),
                "reason": reason,
                "value": portfolio_value
            })

        equity_curve.append({
            "date": current_date.isoformat(),
            "value": portfolio_value
        })

    # 4. Metrics
    equity_df = pd.DataFrame(equity_curve)
    equity_df['returns'] = equity_df['value'].pct_change()
    
    total_return = (equity_df['value'].iloc[-1] / initial_capital) - 1
    days = (combined_df.index[-1] - combined_df.index[0]).days
    annualized_return = (1 + total_return) ** (365.25 / days) - 1 if days > 0 else 0
    
    # Drawdown
    equity_df['cum_max'] = equity_df['value'].cummax()
    equity_df['drawdown'] = (equity_df['value'] / equity_df['cum_max']) - 1
    max_drawdown = equity_df['drawdown'].min()
    
    # Sharpe (approx)
    sharpe = 0
    if equity_df['returns'].std() > 0:
        sharpe = (equity_df['returns'].mean() / equity_df['returns'].std()) * np.sqrt(252)

    return {
        "metrics": {
            "total_return": total_return,
            "annualized_return": annualized_return,
            "max_drawdown": max_drawdown,
            "sharpe_ratio": sharpe,
            "initial_capital": initial_capital,
            "final_value": equity_df['value'].iloc[-1],
            "days": days
        },
        "equity_curve": equity_curve,
        "rebalance_events": rebalance_events
    }

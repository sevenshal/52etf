import os
import sys
from datetime import date

os.environ.setdefault("QUANT_SQLITE_PATH", "/tmp/sf.db")
os.environ.setdefault("ANALYTICS_DB_PATH", "/tmp/semi_fear.duckdb")
os.environ.setdefault("EXTERNAL_TRADING_DB_PATH", "/tmp/sf_et.db")
sys.path.insert(0, "/home/sevenshal/Dev/github/quant/52etf/backend")

from src.core.services.a_stock_fear_greed_clone_service import (
    A_STOCK_FEAR_GREED_TARGET_BY_SYMBOL,
    AStockInnovation100FearGreedCloneCalculator,
)

A_STOCK_FEAR_GREED_TARGET_BY_SYMBOL["H30184.CSI"]["option_underlyings"] = ["OP588000.SH", "OP588080.SH"]
calc = AStockInnovation100FearGreedCloneCalculator("H30184.CSI")
result = calc.calculate_history(
    start_date=date(2022, 12, 1), end_date=date(2026, 8, 14),
    output_start_date=date(2023, 3, 22), history_days=1500,
)
records = result["records"]
print("records:", len(records), records[0]["date"], "~", records[-1]["date"])
# 各组件在有记录时的非空起始（从 components payload）
comp_keys = ["market_momentum", "stock_price_strength", "stock_price_breadth", "put_call_options",
             "market_volatility", "safe_haven_demand", "junk_bond_demand"]
first_seen = {}
for rec in records:
    comps = rec.get("components") or {}
    for k in comp_keys:
        c = comps.get(k) or {}
        score = c.get("score")
        if score is not None and k not in first_seen:
            first_seen[k] = rec["date"]
print("各组件首次有效日期:")
for k in comp_keys:
    print(f"  {k}: {first_seen.get(k)}")
# 看第一条的 put_call_options
r0 = records[0]
print("第一条 components put_call_options:", r0.get("components", {}).get("put_call_options"))
print("第一条 score:", r0["score"], "component_count 概念:", sum(1 for k in comp_keys if (r0.get('components', {}).get(k) or {}).get('score') is not None))

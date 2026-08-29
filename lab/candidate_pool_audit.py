"""Audit index constituents used by Chan backtests."""
from __future__ import annotations
import argparse
from pathlib import Path
import duckdb, pandas as pd

INDEXES = {"000300.SH": "沪深300", "000905.SH": "中证500", "000852.SH": "中证1000", "932000.CSI": "中证2000"}

def main() -> None:
    ap = argparse.ArgumentParser(); ap.add_argument("--database", required=True); ap.add_argument("--end-date", default="2026-08-28"); ap.add_argument("--output", default="lab/output/chan_signal_pair_backtest_20260829_120d/candidate_pool_audit.csv"); ap.add_argument("--supplemental-weights"); a = ap.parse_args()
    con = duckdb.connect(a.database, read_only=True); rows=[]
    extra = pd.read_csv(a.supplemental_weights) if a.supplemental_weights else pd.DataFrame(columns=["index_code","con_code","trade_date"])
    if not extra.empty:
        extra["trade_date"] = pd.to_datetime(extra["trade_date"]).dt.date
    for code, name in INDEXES.items():
        row=con.execute("select count(distinct con_code), min(trade_date), max(trade_date) from a_stock_index_weight where index_code=? and trade_date<=?", [code,a.end_date]).fetchone()
        if not extra.empty:
            x=extra[(extra.index_code.astype(str).str.upper()==code) & (extra.trade_date <= pd.Timestamp(a.end_date).date())]
            if len(x): row=(x.con_code.nunique(), x.trade_date.min(), x.trade_date.max())
        rows.append({"index_code":code,"index_name":name,"constituent_count":row[0],"first_snapshot":row[1],"last_snapshot":row[2],"available":bool(row[0])})
    con.close(); out=pd.DataFrame(rows); p=Path(a.output); p.parent.mkdir(parents=True,exist_ok=True); out.to_csv(p,index=False); print(out.to_string(index=False)); missing=out.loc[~out.available,"index_name"].tolist(); print("MISSING:", ", ".join(missing) if missing else "none")

if __name__ == "__main__": main()

"""Fetch missing CSI constituent snapshots into the analytics DuckDB.

The script reads the already-configured local Tushare token, fetches monthly
windows to avoid the API row limit, and upserts only the two indices used by
the Chan research pool.  It never maps 000932.SH to CSI 2000.
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import tushare as ts


INDICES = ("000852.SH", "932000.CSI")


def _token() -> str:
    cfg = Path("/home/quantd/quant_prod/quant_robot/evc_stocks.db")
    with sqlite3.connect(f"file:{cfg}?mode=ro", uri=True) as con:
        row = con.execute("SELECT api_token FROM tushare_account_configs WHERE id = 1").fetchone()
    if not row or not row[0]:
        raise RuntimeError("本机没有配置Tushare token")
    return str(row[0])


def _month_windows(start: date, end: date):
    cur = date(start.year, start.month, 1)
    while cur <= end:
        nxt = date(cur.year + (cur.month == 12), 1 if cur.month == 12 else cur.month + 1, 1)
        yield max(cur, start), min(date.fromordinal(nxt.toordinal() - 1), end)
        cur = nxt


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database", required=True)
    ap.add_argument("--start-date", default="2020-01-01")
    ap.add_argument("--end-date", default="2026-08-28")
    ap.add_argument("--output-csv", help="只输出补充权重CSV，不写分析库")
    args = ap.parse_args()
    start, end = date.fromisoformat(args.start_date), date.fromisoformat(args.end_date)
    pro = ts.pro_api(_token())
    frames = []
    for code in INDICES:
        for lo, hi in _month_windows(start, end):
            df = pro.index_weight(index_code=code, start_date=lo.strftime("%Y%m%d"), end_date=hi.strftime("%Y%m%d"))
            if not df.empty:
                frames.append(df[["index_code", "con_code", "trade_date", "weight"]])
            print(code, lo, hi, len(df), flush=True)
    if not frames:
        raise RuntimeError("Tushare没有返回任何指数成分")
    data = pd.concat(frames, ignore_index=True).drop_duplicates(["index_code", "con_code", "trade_date"], keep="last")
    data["index_code"] = data["index_code"].astype(str).str.upper()
    data["con_code"] = data["con_code"].astype(str).str.upper()
    data["trade_date"] = pd.to_datetime(data["trade_date"]).dt.date
    if args.output_csv:
        out = Path(args.output_csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        data.to_csv(out, index=False)
        print(f"写出 {len(data)} 行: {out}", flush=True)
        return
    con = duckdb.connect(args.database)
    con.register("chan_incoming_weights", data)
    con.execute("""
        INSERT OR REPLACE INTO a_stock_index_weight
            (index_code, trade_date, con_code, weight, created_at, updated_at)
        SELECT index_code, trade_date, con_code, weight, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        FROM chan_incoming_weights
    """)
    for code in INDICES:
        row = con.execute("SELECT count(*), count(distinct con_code), min(trade_date), max(trade_date) FROM a_stock_index_weight WHERE index_code=?", [code]).fetchone()
        print(code, row, flush=True)
    con.close()


if __name__ == "__main__":
    main()

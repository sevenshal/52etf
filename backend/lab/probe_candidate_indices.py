"""探测候选指数在 Tushare 的 index_daily / index_weight 数据可用性（只读）。"""
import sqlite3
import sys

import tushare as ts

SQLITE = "/home/quantd/quant_prod/quant_robot/evc_stocks.db"

CANDIDATES = [
    # (ts_code, 名称, 用途)
    ("H30588.CSI", "中证证券保险(证保)", "非银：证券+保险"),
    ("930618.CSI", "中证保险主题", "非银：纯保险"),
    ("399966.SZ", "中证800证券保险", "非银：证券+保险(800)"),
    ("000994.SH", "中证全指通信服务(全指电信)", "通信：运营商+设备"),
    ("399995.SZ", "中证基建工程", "基建：建筑与工程"),
    ("930608.CSI", "中证基建", "基建"),
    ("000943.SH", "新基建50", "基建(新基建)"),
    ("932140.CSI", "中证全指电信服务行业", "通信：电信服务行业"),
]


def main():
    db = sqlite3.connect(f"file:{SQLITE}?mode=ro", uri=True)
    token = db.execute("SELECT api_token FROM tushare_account_configs WHERE id=1").fetchone()
    db.close()
    if not token or not token[0]:
        print("NO TOKEN")
        return
    pro = ts.pro_api(token[0])

    for code, name, purpose in CANDIDATES:
        print(f"\n=== {name} {code}  [{purpose}] ===")
        # index_daily
        try:
            df = pro.index_daily(ts_code=code, start_date="20240101", end_date="20240830", limit=5)
            if df is not None and len(df):
                d0, d1 = df["trade_date"].min(), df["trade_date"].max()
                print(f"  index_daily: OK rows={len(df)} 范围 {d0}~{d1}")
            else:
                print("  index_daily: 空")
        except Exception as e:
            print(f"  index_daily: ERR {str(e)[:120]}")
        # index_weight
        try:
            df = pro.index_weight(index_code=code, start_date="20240101", end_date="20240830", limit=10)
            if df is not None and len(df):
                con = df["con_code"].nunique()
                d0, d1 = df["trade_date"].min(), df["trade_date"].max()
                print(f"  index_weight: OK rows={len(df)} 成分{con}只 范围 {d0}~{d1}")
            else:
                print("  index_weight: 空")
        except Exception as e:
            print(f"  index_weight: ERR {str(e)[:120]}")


if __name__ == "__main__":
    main()

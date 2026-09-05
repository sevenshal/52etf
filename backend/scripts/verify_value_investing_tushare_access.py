#!/usr/bin/env python3
"""校验当前 tushare 账号是否有权限访问资产负债表/现金流量表/财务指标接口。

用途：新增的价值投资数据管道(balancesheet/cashflow/fina_indicator)依赖这三个
tushare 接口，通常要求账号积分 >= 2000。这个脚本只做只读探测，不写入任何数据库，
用一只样本股票(默认贵州茅台 600519.SH)各拉一条最新记录，打印返回的字段和行数，
帮助确认：
  1) 权限/积分是否够(报错信息里通常会写"没有权限"或"积分不足")
  2) 字段名是否都能正常返回(避免 tushare 接口字段名变更导致同步拿到空值)

用法(需要在能访问真实 tushare token 配置的环境里跑，即生产环境本身)：
    cd backend && python scripts/verify_value_investing_tushare_access.py
    python scripts/verify_value_investing_tushare_access.py --ts-code 000001.SZ
"""
import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.services.tushare import TushareService  # noqa: E402


def _describe_frame(name: str, frame) -> dict:
    if frame is None or frame.empty:
        return {"interface": name, "rows": 0, "columns": [], "sample": None}
    row = frame.iloc[-1].to_dict()
    return {
        "interface": name,
        "rows": int(len(frame)),
        "columns": list(frame.columns),
        "non_null_columns": [key for key, value in row.items() if value is not None],
        "sample_latest_row": {key: (None if value is None else str(value)) for key, value in row.items()},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ts-code", default="600519.SH", help="样本股票代码，默认贵州茅台")
    args = parser.parse_args()

    ts_code = args.ts_code.strip().upper()
    end_date = date.today()
    start_date = end_date - timedelta(days=365 * 6)

    service = TushareService.get_instance()
    results = []

    for label, fetch in (
        ("balancesheet", lambda: service.get_a_stock_balancesheet_range_frame(start_date, end_date, ts_code=ts_code)),
        ("cashflow", lambda: service.get_a_stock_cashflow_range_frame(start_date, end_date, ts_code=ts_code)),
        ("fina_indicator", lambda: service.get_a_stock_fina_indicator_range_frame(start_date, end_date, ts_code=ts_code)),
    ):
        try:
            frame = fetch()
            results.append(_describe_frame(label, frame))
        except Exception as exc:  # noqa: BLE001
            results.append({"interface": label, "error": str(exc)})

    print(json.dumps({"ts_code": ts_code, "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""回补港交所期权日报（hsio/hhio/htio + 2800 盈富期权）到 hk_option_daily。

用法::

    python backend/scripts/backfill_hk_option_daily.py --start-date 2024-01-01

htm 日报只保留约一年，更早的日期会自动改用 DTOP 结算统计回补（IV 由结算价反解）。
"""

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.services.hk_option_service import HKOptionDailySync


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", default=f"{date.today().year - 1}-01-01")
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument("--no-etf", action="store_true", help="不同步 2800 等 ETF 期权")
    parser.add_argument("--no-dtop", action="store_true", help="htm 缺失时不回退 DTOP 结算统计")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sync = HKOptionDailySync(
        include_etf=not args.no_etf,
        allow_dtop_backfill=not args.no_dtop,
    )
    try:
        result = sync.sync_range(
            date.fromisoformat(args.start_date),
            date.fromisoformat(args.end_date),
        )
    finally:
        sync.close()
    print(json.dumps(result, ensure_ascii=False, default=str), flush=True)


if __name__ == "__main__":
    main()

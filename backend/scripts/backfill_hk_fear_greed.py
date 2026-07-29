#!/usr/bin/env python3

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.services.hk_stock_fear_greed_service import HKStockFearGreedCalculator
from src.robot.hk_stock_base_data_config import HK_INDEX_FEAR_GREED_TARGETS
from src.robot.hk_stock_base_data_sync import HKStockBaseDataSyncService


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", default="2023-01-01")
    parser.add_argument("--end-date", default=date.today().isoformat())
    args = parser.parse_args()
    start_date = date.fromisoformat(args.start_date)
    end_date = date.fromisoformat(args.end_date)
    calculation_start = start_date - timedelta(days=900)

    sync = HKStockBaseDataSyncService()
    print(json.dumps({"basic_rows": sync.sync_basic()}, ensure_ascii=False), flush=True)
    history = sync.sync_constituent_history_yahoo(
        calculation_start,
        end_date,
        workers=8,
        extra_symbols=["02828.HK"],
    )
    print(json.dumps({"constituent_history": history}, ensure_ascii=False), flush=True)
    indexes = sync.sync_index_daily(calculation_start, end_date)
    indexes["HSCEI.HK"] = sync.sync_yahoo_index(
        "%5EHSCE",
        "HSCEI",
        calculation_start,
        end_date,
    )
    print(json.dumps({"indexes": indexes}, ensure_ascii=False), flush=True)

    results = []
    for target in HK_INDEX_FEAR_GREED_TARGETS:
        result = HKStockFearGreedCalculator(target["symbol"]).backfill_to_db(
            start_date=calculation_start,
            end_date=end_date,
            output_start_date=start_date,
            history_days=900,
            score_window=252,
            min_periods=120,
        )
        results.append(result)
        print(json.dumps({"fear_greed": result}, ensure_ascii=False), flush=True)
    print(json.dumps({"completed": results}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

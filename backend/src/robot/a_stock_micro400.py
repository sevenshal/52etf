"""自算「A股微盘400」指数，对齐万得微盘股指数（868008.WI）的现行编制方案。

万得的 .WI 指数不对外分发（tushare 的 index_daily 只覆盖中证/国证/申万/中金等），
所以这里用本地全市场行情缓存自己编制一条等价指数：沪深A股剔除 ST/*ST/退市警示和
未开板新股后，按总市值升序取最小的 400 只，等权，月度调样。

和真实 868008.WI 的已知差异写在 rule_snapshot 里，前端会原样展示：
- 起点是 2020-01-01（受全市场行情缓存起始时间限制），不是 1999-12-30 基日；
- 真实 868008.WI 2025-01-02 才发布，之前的点位继承自日频调仓的 8841431.WI，
  而这里全程月度调样，历史段会有系统性偏差；
- 「未开板新股」按「上市以来是否出现过 high != low」近似，万得的细则未公开。
"""
import math
import os
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from typing import Deque, Dict, List, Optional, Set, Tuple

import numpy as np
import polars as pl
from sqlalchemy.orm import Session

from ..core.database import (
    AStockMicro400Constituent,
    AStockMicro400Level,
    AStockMicro400Rebalance,
)
from .a_stock_base_data_config import (
    DEFAULT_START_DATE,
    RAW_FETCH_LOOKBACK_DAYS,
)
from .a_stock_custom_index_base import (
    CustomIndexBuilderBase,
    ProgressCallback,
    _is_market_frame_empty,
    _parse_date,
    _round_or_none,
)

INDEX_CODE = "MICRO400.CN"
INDEX_NAME = "A股微盘400"
BASE_LEVEL = 1000.0
TARGET_CONSTITUENT_COUNT = 400
LIQUIDITY_WINDOW = 60
# 只对上市未满这个天数的新股判断「是否已开板」；更老的股票一律视为已开板，
# 避免回跑窗口起点恰好撞上一字涨停时被误剔除。
NEW_LISTING_UNOPENED_WINDOW_DAYS = 180
MARKET_FRAME_LOAD_DAYS = max(1, int(os.getenv("A_STOCK_MICRO400_MARKET_LOAD_DAYS", "20")))


class AStockMicro400Builder(CustomIndexBuilderBase):
    MARKET_FRAME_LOAD_DAYS = MARKET_FRAME_LOAD_DAYS
    # 比基类多要 high/low，用来判断新股是否已经开板。
    MARKET_RANGE_COLUMNS = (
        "close",
        "high",
        "low",
        "pct_chg",
        "amount",
        "total_mv",
        "circ_mv",
    )

    @staticmethod
    def rule_snapshot() -> Dict:
        return {
            "index_code": INDEX_CODE,
            "index_name": INDEX_NAME,
            "benchmark": "868008.WI 万得微盘股指数",
            "base_level": BASE_LEVEL,
            "target_constituent_count": TARGET_CONSTITUENT_COUNT,
            "universe": "沪深A股（不含北交所），剔除ST/*ST/退市整理和上市以来仍未开板的新股",
            "selection": "按总市值升序取最小的400只",
            "weighting": "等权，调样日重置为1/400；两次调样之间权重随价格漂移",
            "reconstitution": "每月最后一个交易日收盘选样，下一交易日生效；初始日当日生效",
            "known_differences": [
                "起点为本地全市场行情缓存的起始日（默认2020-01-01），不是万得1999-12-30基日",
                "真实868008.WI在2025-01-02前继承日频调仓的8841431.WI，本指数全程月度调样，历史段偏低",
                "未开板新股按「上市以来是否出现过最高价≠最低价」近似识别",
            ],
        }

    def _is_basic_eligible(
        self,
        ts_code: str,
        as_of: date,
        basic: Optional[Dict],
        intervals: Dict[str, List[Tuple[date, date]]],
        opened_symbols: Set[str],
    ) -> bool:
        if not basic:
            return False
        if basic.get("exchange") not in {"SSE", "SZSE"}:
            return False
        list_date = basic.get("list_date")
        if not list_date or list_date > as_of:
            return False
        delist_date = basic.get("delist_date")
        if delist_date and delist_date <= as_of:
            return False
        if self._is_st_or_retiring(ts_code, as_of, basic, intervals):
            return False
        is_new_listing = (as_of - list_date).days <= NEW_LISTING_UNOPENED_WINDOW_DAYS
        if is_new_listing and ts_code not in opened_symbols:
            return False
        return True

    @staticmethod
    def _update_opened_symbols(market_frame: pl.DataFrame, opened_symbols: Set[str]) -> None:
        """记录哪些股票已经出现过日内波动，即新股已经开板。"""
        if _is_market_frame_empty(market_frame):
            return
        if not {"ts_code", "high", "low"}.issubset(set(market_frame.columns)):
            return
        frame = market_frame.select(["ts_code", "high", "low"]).filter(
            pl.col("ts_code").is_not_null()
            & pl.col("high").is_not_null()
            & pl.col("low").is_not_null()
            & (pl.col("low") > 0)
            & (pl.col("high") > pl.col("low"))
        )
        for ts_code, _high, _low in frame.iter_rows():
            opened_symbols.add(str(ts_code))

    def _rank_candidates(
        self,
        market_frame: pl.DataFrame,
        as_of: date,
        basic_map: Dict[str, Dict],
        st_intervals: Dict[str, List[Tuple[date, date]]],
        opened_symbols: Set[str],
        amount_history: Dict[str, Deque[float]],
    ) -> List[Dict]:
        if _is_market_frame_empty(market_frame):
            return []

        filtered = market_frame.filter(
            pl.col("close").is_not_null()
            & (pl.col("close") > 0)
            & pl.col("total_mv").is_not_null()
            & (pl.col("total_mv") > 0)
        )
        candidates: List[Dict] = []
        for row in filtered.iter_rows(named=True):
            ts_code = str(row.get("ts_code") or "")
            if not ts_code:
                continue
            basic = basic_map.get(ts_code)
            if not self._is_basic_eligible(ts_code, as_of, basic, st_intervals, opened_symbols):
                continue
            history = amount_history.get(ts_code)
            candidates.append(
                {
                    "ts_code": ts_code,
                    "name": basic.get("name"),
                    "industry": basic.get("industry"),
                    "close": float(row.get("close") or 0.0),
                    "pct_chg": float(row.get("pct_chg") or 0.0),
                    "amount": float(row.get("amount") or 0.0),
                    "avg_amount_60d": float(np.mean(history)) if history else 0.0,
                    "total_mv": float(row.get("total_mv") or 0.0),
                    "circ_mv": float(row.get("circ_mv") or 0.0),
                }
            )

        # 总市值升序 = 越靠前越小；同市值时成交额小的排前面，保持排序稳定。
        candidates.sort(key=lambda item: (item["total_mv"], item["amount"], item["ts_code"]))
        for rank, item in enumerate(candidates, start=1):
            item["rank"] = rank
        return candidates

    @staticmethod
    def _select_constituents(ranked: List[Dict]) -> List[Dict]:
        """月度全量重选最小的 400 只，万得未公布缓冲区规则，这里不做保留档。"""
        return ranked[:TARGET_CONSTITUENT_COUNT]

    @staticmethod
    def _build_weighted_constituents(
        selected: List[Dict],
        previous_weight_map: Dict[str, float],
    ) -> Tuple[List[Dict], float]:
        if not selected:
            return [], 0.0
        weight = 1.0 / len(selected)
        weighted = []
        for item in selected:
            row = dict(item)
            row["raw_weight"] = weight
            row["weight"] = weight
            weighted.append(row)

        symbols = {item["ts_code"] for item in weighted}
        turnover = sum(
            abs(weight - float(previous_weight_map.get(item["ts_code"], 0.0)))
            for item in weighted
        )
        turnover += sum(
            abs(float(previous_weight))
            for symbol, previous_weight in previous_weight_map.items()
            if symbol not in symbols
        )
        return weighted, turnover / 2 * 100

    @classmethod
    def _is_month_end(cls, trading_dates: List[date], index: int) -> bool:
        return cls._is_period_end(trading_dates, index, "month")

    @staticmethod
    def _rebalance_type(is_initial: bool) -> str:
        return "inception" if is_initial else "monthly_reconstitution"

    def _save_rebalance(
        self,
        rebalance_date: date,
        effective_date: Optional[date],
        rebalance_type: str,
        constituents: List[Dict],
        previous_symbols: List[str],
        previous_weight_map: Dict[str, float],
        turnover_pct: float,
    ) -> int:
        symbols = [item["ts_code"] for item in constituents]
        previous_set = set(previous_symbols)
        current_set = set(symbols)
        additions = [item for item in constituents if item["ts_code"] not in previous_set]
        removals = [symbol for symbol in previous_symbols if symbol not in current_set]

        record = AStockMicro400Rebalance(
            index_code=INDEX_CODE,
            rebalance_date=rebalance_date,
            effective_date=effective_date,
            rebalance_type=rebalance_type,
            constituent_count=len(constituents),
            turnover_pct=_round_or_none(turnover_pct, 6),
            total_mv=_round_or_none(sum(float(item.get("total_mv") or 0.0) for item in constituents), 4),
            total_circ_mv=_round_or_none(sum(float(item.get("circ_mv") or 0.0) for item in constituents), 4),
            additions=[
                {
                    "ts_code": item["ts_code"],
                    "name": item.get("name"),
                    "industry": item.get("industry"),
                    "rank": item.get("rank"),
                    "weight_pct": _round_or_none(float(item.get("weight") or 0.0) * 100, 6),
                }
                for item in additions
            ],
            removals=removals,
            rule_snapshot=self.rule_snapshot(),
            created_at=datetime.now(),
        )
        self.db.add(record)
        self.db.flush()

        now = datetime.now()
        # commit 后 ORM 属性会过期，主键要在事务里先取出来再返回。
        record_id = record.id
        self.db.add_all([
            AStockMicro400Constituent(
                index_code=INDEX_CODE,
                rebalance_id=record.id,
                ts_code=item["ts_code"],
                rebalance_date=rebalance_date,
                effective_date=effective_date,
                name=item.get("name"),
                industry=item.get("industry"),
                rank=item.get("rank"),
                raw_weight_pct=_round_or_none(float(item.get("raw_weight") or 0.0) * 100, 6),
                weight_pct=_round_or_none(float(item.get("weight") or 0.0) * 100, 6),
                total_mv=_round_or_none(item.get("total_mv"), 4),
                circ_mv=_round_or_none(item.get("circ_mv"), 4),
                avg_amount_60d=_round_or_none(item.get("avg_amount_60d"), 4),
                action="added" if item["ts_code"] not in previous_set else "retained",
                created_at=now,
            )
            for item in constituents
        ])
        self.db.commit()
        return record_id

    def _delete_existing_index_outputs(self):
        self.db.query(AStockMicro400Constituent).filter(
            AStockMicro400Constituent.index_code == INDEX_CODE
        ).delete(synchronize_session=False)
        self.db.query(AStockMicro400Rebalance).filter(
            AStockMicro400Rebalance.index_code == INDEX_CODE
        ).delete(synchronize_session=False)
        self.db.query(AStockMicro400Level).filter(
            AStockMicro400Level.index_code == INDEX_CODE
        ).delete(synchronize_session=False)
        self.db.commit()

    @staticmethod
    def _level_row(
        current_date: date,
        level: float,
        daily_return: float,
        drawdown_pct: float,
        constituents: List[Dict],
    ) -> Dict:
        now = datetime.now()
        return {
            "index_code": INDEX_CODE,
            "date": current_date,
            "level": _round_or_none(level, 6),
            "daily_return_pct": _round_or_none(daily_return * 100, 6),
            "drawdown_pct": _round_or_none(drawdown_pct, 6),
            "constituent_count": len(constituents),
            "total_mv": _round_or_none(sum(float(item.get("total_mv") or 0.0) for item in constituents), 4),
            "total_circ_mv": _round_or_none(sum(float(item.get("circ_mv") or 0.0) for item in constituents), 4),
            "created_at": now,
            "updated_at": now,
        }

    def rebuild(
        self,
        start_date: date = DEFAULT_START_DATE,
        end_date: Optional[date] = None,
        force_rebuild_outputs: bool = True,
    ) -> Dict:
        start_date = _parse_date(start_date) or DEFAULT_START_DATE
        end_date = _parse_date(end_date) or date.today()
        if start_date > end_date:
            raise ValueError("开始日期不能晚于结束日期")

        if force_rebuild_outputs:
            self._progress("清理旧的微盘400指数结果", 6)
            self._delete_existing_index_outputs()

        trading_dates = self._cached_market_trading_dates(
            start_date - timedelta(days=RAW_FETCH_LOOKBACK_DAYS),
            end_date,
        )
        if not trading_dates:
            raise RuntimeError("没有找到A股全市场日行情缓存，请先执行A股基础数据同步")

        basic_map = self._load_basic_map()
        if not basic_map:
            raise RuntimeError("没有找到A股基础信息缓存，请先执行A股基础数据同步")
        st_intervals = self._load_st_intervals()
        amount_history: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=LIQUIDITY_WINDOW))
        opened_symbols: Set[str] = set()
        current_constituents: List[Dict] = []
        current_weight_map: Dict[str, float] = {}
        pending_constituents: Optional[List[Dict]] = None
        pending_effective_date: Optional[date] = None
        levels: List[Dict] = []
        level = BASE_LEVEL
        high_watermark = BASE_LEVEL

        market_day_stats = self._existing_market_day_stats(min(trading_dates), max(trading_dates))
        available_trading_dates = [
            item
            for item in trading_dates
            if not self._market_day_needs_refresh(market_day_stats.get(item))
        ]
        skipped_dates = len(trading_dates) - len(available_trading_dates)
        if skipped_dates:
            self._progress(
                f"跳过{skipped_dates}个尚无完整行情的交易日",
                50,
                skipped_dates=skipped_dates,
                total_dates=len(trading_dates),
            )
        trading_dates = available_trading_dates
        if not trading_dates:
            raise RuntimeError("指定区间内没有完整行情的交易日")

        start_index = next((idx for idx, item in enumerate(trading_dates) if item >= start_date), None)
        if start_index is None:
            raise RuntimeError("开始日期之后没有完整行情的交易日")

        total_dates = len(trading_dates)
        self._progress(
            "按窗口载入全市场行情缓存",
            50,
            processed_dates=0,
            total_dates=total_dates,
            window_days=self.MARKET_FRAME_LOAD_DAYS,
        )

        for idx, current_date, market_frame in self._iter_market_frames_by_date(
            trading_dates,
            "载入全市场行情缓存",
            progress_start=50,
            progress_end=89,
        ):
            if idx == 0 or idx == total_dates - 1 or idx % 20 == 0:
                self._progress(
                    f"计算微盘400指数点位 {current_date.isoformat()}",
                    50 + int(idx / max(total_dates, 1) * 40),
                    processed_dates=idx + 1,
                    total_dates=total_dates,
                )
            if _is_market_frame_empty(market_frame):
                raise RuntimeError(f"{current_date.isoformat()} 行情缓存为空，请先执行A股基础数据同步")
            self._update_amount_history(market_frame, amount_history)
            self._update_opened_symbols(market_frame, opened_symbols)
            if current_date < start_date:
                continue

            is_first_output_day = idx == start_index
            if is_first_output_day:
                ranked = self._rank_candidates(
                    market_frame, current_date, basic_map, st_intervals, opened_symbols, amount_history
                )
                current_constituents, turnover_pct = self._build_weighted_constituents(
                    self._select_constituents(ranked), {}
                )
                current_weight_map = {item["ts_code"]: float(item["weight"]) for item in current_constituents}
                self._save_rebalance(
                    rebalance_date=current_date,
                    effective_date=current_date,
                    rebalance_type=self._rebalance_type(is_initial=True),
                    constituents=current_constituents,
                    previous_symbols=[],
                    previous_weight_map={},
                    turnover_pct=turnover_pct,
                )
                daily_return = 0.0
            else:
                if pending_constituents is not None and pending_effective_date == current_date:
                    current_constituents = pending_constituents
                    current_weight_map = {item["ts_code"]: float(item["weight"]) for item in current_constituents}
                    pending_constituents = None
                    pending_effective_date = None

                daily_return, current_weight_map = self._advance_weights(
                    market_frame, current_weight_map, basic_map, current_date
                )
                level *= (1.0 + daily_return)
                high_watermark = max(high_watermark, level)

            drawdown_pct = (level / high_watermark - 1.0) * 100 if high_watermark > 0 else 0.0
            levels.append(self._level_row(current_date, level, daily_return, drawdown_pct, current_constituents))

            if idx < len(trading_dates) - 1 and not is_first_output_day and self._is_month_end(trading_dates, idx):
                ranked = self._rank_candidates(
                    market_frame, current_date, basic_map, st_intervals, opened_symbols, amount_history
                )
                next_constituents, turnover_pct = self._build_weighted_constituents(
                    self._select_constituents(ranked), current_weight_map
                )
                effective_date = trading_dates[idx + 1]
                self._save_rebalance(
                    rebalance_date=current_date,
                    effective_date=effective_date,
                    rebalance_type=self._rebalance_type(is_initial=False),
                    constituents=next_constituents,
                    previous_symbols=[item["ts_code"] for item in current_constituents],
                    previous_weight_map=current_weight_map,
                    turnover_pct=turnover_pct,
                )
                pending_constituents = next_constituents
                pending_effective_date = effective_date

        self._progress("写入微盘400指数点位", 92)
        self._bulk_upsert(AStockMicro400Level, levels, ["index_code", "date"], batch_size=1000)

        latest_level = levels[-1] if levels else None
        total_return_pct = (
            (float(latest_level["level"]) / BASE_LEVEL - 1.0) * 100
            if latest_level and latest_level.get("level")
            else None
        )
        rebalances_count = self.db.query(AStockMicro400Rebalance).filter(
            AStockMicro400Rebalance.index_code == INDEX_CODE
        ).count()
        latest_rebalance = self.db.query(AStockMicro400Rebalance).filter(
            AStockMicro400Rebalance.index_code == INDEX_CODE
        ).order_by(AStockMicro400Rebalance.rebalance_date.desc(), AStockMicro400Rebalance.id.desc()).first()

        self._progress("A股微盘400回跑完成", 100)
        return {
            "index_code": INDEX_CODE,
            "index_name": INDEX_NAME,
            "mode": "rebuild",
            "status": "completed",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "latest_date": latest_level["date"].isoformat() if latest_level else None,
            "latest_level": latest_level.get("level") if latest_level else None,
            "total_return_pct": _round_or_none(total_return_pct, 4),
            "levels_saved": len(levels),
            "rebalances_saved": rebalances_count,
            "latest_rebalance_id": latest_rebalance.id if latest_rebalance else None,
            "latest_rebalance_date": latest_rebalance.rebalance_date.isoformat() if latest_rebalance else None,
            "rule_snapshot": self.rule_snapshot(),
            "last_market_date": trading_dates[-1].isoformat() if trading_dates else None,
        }

    def _load_constituents_for_rebalance(self, rebalance_id: int) -> List[Dict]:
        rows = (
            self.db.query(AStockMicro400Constituent)
            .filter(
                AStockMicro400Constituent.index_code == INDEX_CODE,
                AStockMicro400Constituent.rebalance_id == rebalance_id,
            )
            .order_by(AStockMicro400Constituent.rank.asc())
            .all()
        )
        return [
            {
                "ts_code": row.ts_code,
                "name": row.name,
                "industry": row.industry,
                "rank": row.rank,
                "raw_weight": float(row.raw_weight_pct or 0.0) / 100.0,
                "weight": float(row.weight_pct or 0.0) / 100.0,
                "total_mv": row.total_mv,
                "circ_mv": row.circ_mv,
                "avg_amount_60d": row.avg_amount_60d,
            }
            for row in rows
        ]

    def _load_incremental_state(self, as_of: date) -> Dict:
        latest_level = (
            self.db.query(AStockMicro400Level)
            .filter(AStockMicro400Level.index_code == INDEX_CODE)
            .order_by(AStockMicro400Level.date.desc())
            .first()
        )
        if not latest_level:
            return {}

        high_watermark_row = (
            self.db.query(AStockMicro400Level.level)
            .filter(
                AStockMicro400Level.index_code == INDEX_CODE,
                AStockMicro400Level.date <= latest_level.date,
            )
            .order_by(AStockMicro400Level.level.desc())
            .first()
        )
        high_watermark = (
            float(high_watermark_row[0])
            if high_watermark_row and high_watermark_row[0]
            else float(latest_level.level or BASE_LEVEL)
        )

        effective_rebalance = (
            self.db.query(AStockMicro400Rebalance)
            .filter(
                AStockMicro400Rebalance.index_code == INDEX_CODE,
                AStockMicro400Rebalance.effective_date <= latest_level.date,
            )
            .order_by(AStockMicro400Rebalance.effective_date.desc(), AStockMicro400Rebalance.id.desc())
            .first()
        )
        if not effective_rebalance:
            return {}

        current_constituents = self._load_constituents_for_rebalance(effective_rebalance.id)
        if not current_constituents:
            return {}

        pending_rebalance = (
            self.db.query(AStockMicro400Rebalance)
            .filter(
                AStockMicro400Rebalance.index_code == INDEX_CODE,
                AStockMicro400Rebalance.rebalance_date <= latest_level.date,
                AStockMicro400Rebalance.effective_date > latest_level.date,
                AStockMicro400Rebalance.effective_date <= as_of,
            )
            .order_by(AStockMicro400Rebalance.effective_date.asc(), AStockMicro400Rebalance.id.asc())
            .first()
        )

        return {
            "latest_level": latest_level,
            "level": float(latest_level.level or BASE_LEVEL),
            "high_watermark": high_watermark,
            "current_constituents": current_constituents,
            "current_weight_map": {
                item["ts_code"]: float(item.get("weight") or 0.0) for item in current_constituents
            },
            "current_effective_date": effective_rebalance.effective_date or effective_rebalance.rebalance_date,
            "pending_constituents": (
                self._load_constituents_for_rebalance(pending_rebalance.id) if pending_rebalance else None
            ),
            "pending_effective_date": pending_rebalance.effective_date if pending_rebalance else None,
        }

    def _up_to_date_result(self, latest_level, end_date: date) -> Dict:
        return {
            "index_code": INDEX_CODE,
            "index_name": INDEX_NAME,
            "mode": "incremental",
            "status": "up_to_date",
            "start_date": latest_level.date.isoformat(),
            "end_date": end_date.isoformat(),
            "latest_date": latest_level.date.isoformat(),
            "latest_level": latest_level.level,
            "levels_saved": 0,
            "rebalances_saved": 0,
        }

    def refresh_incremental(self, end_date: Optional[date] = None) -> Dict:
        end_date = _parse_date(end_date) or date.today()
        state = self._load_incremental_state(end_date)
        if not state:
            self._progress("未找到可增量续算的微盘400结果，执行首次全量回跑", 0)
            return self.rebuild(start_date=DEFAULT_START_DATE, end_date=end_date, force_rebuild_outputs=True)

        latest_level = state["latest_level"]
        latest_date = latest_level.date
        if latest_date >= end_date:
            return self._up_to_date_result(latest_level, end_date)

        # 权重在两次调样之间随价格漂移，续算前要从当期成分生效日重放一遍。
        weights_valid_from = _parse_date(state.get("current_effective_date")) or latest_date
        calendar_start = min(latest_date - timedelta(days=RAW_FETCH_LOOKBACK_DAYS), weights_valid_from)
        trading_dates = self._cached_market_trading_dates(calendar_start, end_date)
        if not trading_dates:
            raise RuntimeError("没有找到A股全市场日行情缓存，请先执行A股基础数据同步")

        market_day_stats = self._existing_market_day_stats(min(trading_dates), max(trading_dates))
        trading_dates = [
            item
            for item in trading_dates
            if not self._market_day_needs_refresh(market_day_stats.get(item))
        ]
        new_trading_dates = [item for item in trading_dates if item > latest_date]
        if not new_trading_dates:
            return self._up_to_date_result(latest_level, end_date)

        basic_map = self._load_basic_map()
        st_intervals = self._load_st_intervals()
        amount_history: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=LIQUIDITY_WINDOW))
        opened_symbols: Set[str] = set()
        current_constituents: List[Dict] = state["current_constituents"]
        current_weight_map: Dict[str, float] = state["current_weight_map"]
        pending_constituents: Optional[List[Dict]] = state["pending_constituents"]
        pending_effective_date: Optional[date] = state["pending_effective_date"]
        level = float(state["level"])
        high_watermark = float(state["high_watermark"])
        levels: List[Dict] = []
        rebalances_before = self.db.query(AStockMicro400Rebalance).filter(
            AStockMicro400Rebalance.index_code == INDEX_CODE
        ).count()

        self._progress(
            "按窗口载入微盘400增量行情缓存",
            35,
            latest_date=latest_date.isoformat(),
            end_date=end_date.isoformat(),
            window_days=self.MARKET_FRAME_LOAD_DAYS,
        )

        total_dates = len(new_trading_dates)
        processed_output_dates = 0
        for current_index, current_date, market_frame in self._iter_market_frames_by_date(
            trading_dates,
            "载入微盘400增量行情缓存",
        ):
            is_output_date = current_date > latest_date
            if is_output_date:
                self._progress(
                    f"增量计算微盘400指数点位 {current_date.isoformat()}",
                    50 + int(processed_output_dates / max(total_dates, 1) * 40),
                    processed_dates=processed_output_dates + 1,
                    total_dates=total_dates,
                )

            if _is_market_frame_empty(market_frame):
                raise RuntimeError(f"{current_date.isoformat()} 行情缓存为空，请先执行A股基础数据同步")

            self._update_amount_history(market_frame, amount_history)
            self._update_opened_symbols(market_frame, opened_symbols)

            if pending_constituents is not None and pending_effective_date == current_date:
                current_constituents = pending_constituents
                current_weight_map = {item["ts_code"]: float(item["weight"]) for item in current_constituents}
                pending_constituents = None
                pending_effective_date = None

            if current_date >= weights_valid_from:
                daily_return, current_weight_map = self._advance_weights(
                    market_frame, current_weight_map, basic_map, current_date
                )
            else:
                daily_return = 0.0

            # 月末可能正好就是最后一条已落库的点位，调样必须在整个回看窗口上判断，
            # 否则任务下次只看新交易日时会永远漏掉这次调样。
            if current_index < len(trading_dates) - 1 and self._is_month_end(trading_dates, current_index):
                existing_rebalance = (
                    self.db.query(AStockMicro400Rebalance)
                    .filter(
                        AStockMicro400Rebalance.index_code == INDEX_CODE,
                        AStockMicro400Rebalance.rebalance_date == current_date,
                    )
                    .first()
                )
                if not existing_rebalance:
                    ranked = self._rank_candidates(
                        market_frame, current_date, basic_map, st_intervals, opened_symbols, amount_history
                    )
                    next_constituents, turnover_pct = self._build_weighted_constituents(
                        self._select_constituents(ranked), current_weight_map
                    )
                    effective_date = trading_dates[current_index + 1]
                    self._save_rebalance(
                        rebalance_date=current_date,
                        effective_date=effective_date,
                        rebalance_type=self._rebalance_type(is_initial=False),
                        constituents=next_constituents,
                        previous_symbols=[item["ts_code"] for item in current_constituents],
                        previous_weight_map=current_weight_map,
                        turnover_pct=turnover_pct,
                    )
                    pending_constituents = next_constituents
                    pending_effective_date = effective_date

            if not is_output_date:
                continue

            processed_output_dates += 1
            level *= (1.0 + daily_return)
            high_watermark = max(high_watermark, level)
            drawdown_pct = (level / high_watermark - 1.0) * 100 if high_watermark > 0 else 0.0
            levels.append(self._level_row(current_date, level, daily_return, drawdown_pct, current_constituents))

        self._progress("写入微盘400增量指数点位", 92)
        self._bulk_upsert(AStockMicro400Level, levels, ["index_code", "date"], batch_size=1000)

        latest_saved = levels[-1] if levels else latest_level
        rebalances_after = self.db.query(AStockMicro400Rebalance).filter(
            AStockMicro400Rebalance.index_code == INDEX_CODE
        ).count()
        self._progress("A股微盘400增量刷新完成", 100)
        return {
            "index_code": INDEX_CODE,
            "index_name": INDEX_NAME,
            "mode": "incremental",
            "status": "completed",
            "start_date": new_trading_dates[0].isoformat(),
            "end_date": end_date.isoformat(),
            "latest_date": (
                latest_saved["date"].isoformat()
                if isinstance(latest_saved, dict)
                else latest_saved.date.isoformat()
            ),
            "latest_level": latest_saved.get("level") if isinstance(latest_saved, dict) else latest_saved.level,
            "levels_saved": len(levels),
            "rebalances_saved": rebalances_after - rebalances_before,
            "last_market_date": new_trading_dates[-1].isoformat(),
        }


def rebuild_a_stock_micro400(
    db: Session,
    start_date: date = DEFAULT_START_DATE,
    end_date: Optional[date] = None,
    progress_callback: Optional[ProgressCallback] = None,
) -> Dict:
    builder = AStockMicro400Builder(db, progress_callback=progress_callback)
    try:
        return builder.rebuild(start_date=start_date, end_date=end_date, force_rebuild_outputs=True)
    finally:
        builder.close()


def load_a_stock_micro400_summary(db: Session) -> Dict:
    latest_level = db.query(AStockMicro400Level).filter(
        AStockMicro400Level.index_code == INDEX_CODE
    ).order_by(AStockMicro400Level.date.desc()).first()
    first_level = db.query(AStockMicro400Level).filter(
        AStockMicro400Level.index_code == INDEX_CODE
    ).order_by(AStockMicro400Level.date.asc()).first()
    if not latest_level or not first_level:
        return {
            "index_code": INDEX_CODE,
            "index_name": INDEX_NAME,
            "rule_snapshot": AStockMicro400Builder.rule_snapshot(),
            "has_data": False,
        }

    latest_rebalance = db.query(AStockMicro400Rebalance).filter(
        AStockMicro400Rebalance.index_code == INDEX_CODE
    ).order_by(AStockMicro400Rebalance.rebalance_date.desc(), AStockMicro400Rebalance.id.desc()).first()
    level_rows = db.query(AStockMicro400Level).filter(
        AStockMicro400Level.index_code == INDEX_CODE
    ).order_by(AStockMicro400Level.date.asc()).all()
    returns = [
        float(row.daily_return_pct) / 100.0
        for row in level_rows
        if row.daily_return_pct is not None
    ]
    total_return_pct = (latest_level.level / first_level.level - 1.0) * 100 if first_level.level else None
    years = max((latest_level.date - first_level.date).days / 365.25, 1 / 365.25)
    annualized_return_pct = (
        ((latest_level.level / first_level.level) ** (1 / years) - 1.0) * 100 if first_level.level else None
    )
    volatility_pct = float(np.std(returns, ddof=1) * math.sqrt(252) * 100) if len(returns) > 1 else None
    sharpe = (annualized_return_pct / volatility_pct) if volatility_pct and volatility_pct > 0 else None
    max_drawdown_pct = min((row.drawdown_pct or 0.0) for row in level_rows)
    return {
        "index_code": INDEX_CODE,
        "index_name": INDEX_NAME,
        "has_data": True,
        "start_date": first_level.date.isoformat(),
        "latest_date": latest_level.date.isoformat(),
        "latest_level": _round_or_none(latest_level.level, 4),
        "total_return_pct": _round_or_none(total_return_pct, 4),
        "annualized_return_pct": _round_or_none(annualized_return_pct, 4),
        "annualized_volatility_pct": _round_or_none(volatility_pct, 4),
        "sharpe_ratio": _round_or_none(sharpe, 4),
        "max_drawdown_pct": _round_or_none(max_drawdown_pct, 4),
        "constituent_count": latest_level.constituent_count,
        "rebalances_count": db.query(AStockMicro400Rebalance).filter(
            AStockMicro400Rebalance.index_code == INDEX_CODE
        ).count(),
        "latest_rebalance_id": latest_rebalance.id if latest_rebalance else None,
        "latest_rebalance_date": latest_rebalance.rebalance_date.isoformat() if latest_rebalance else None,
        "latest_effective_date": (
            latest_rebalance.effective_date.isoformat()
            if latest_rebalance and latest_rebalance.effective_date
            else None
        ),
        "rule_snapshot": AStockMicro400Builder.rule_snapshot(),
    }

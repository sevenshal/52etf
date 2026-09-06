import math
import os
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np
import polars as pl
from sqlalchemy.orm import Session

from ..core.database import (
    AStockInnovation100Constituent,
    AStockInnovation100Level,
    AStockInnovation100Rebalance,
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
    # 图表用的基准曲线和年度收益已挪到基类模块，这里转发一下，避免改动现有 API 层的 import。
    compute_yearly_returns,  # noqa: F401
    load_benchmark_index_curves,  # noqa: F401
)

INDEX_CODE = "INNO100.CN"
INDEX_NAME = "A股创新100"
BASE_LEVEL = 1000.0
TARGET_CONSTITUENT_COUNT = 100
DIRECT_ENTRY_RANK = 75
RETENTION_RANK = 125
MIN_LISTING_DAYS = 365
LIQUIDITY_WINDOW = 60
MIN_AVG_AMOUNT_60D = 100_000.0  # Tushare amount单位为千元，约等于1亿元人民币。
MAX_SINGLE_WEIGHT = 0.10
TOP5_WEIGHT_CAP = 0.40
LARGE_WEIGHT_THRESHOLD = 0.045
LARGE_WEIGHT_CAP = 0.48
MARKET_FRAME_LOAD_DAYS = max(1, int(os.getenv("A_STOCK_INNOVATION100_MARKET_LOAD_DAYS", "20")))

INNOVATION_INDUSTRIES = {
    "IT设备",
    "互联网",
    "元器件",
    "半导体",
    "软件服务",
    "通信设备",
    "电信运营",
    "电器仪表",
    "电气设备",
    "专用机械",
    "工程机械",
    "机床制造",
    "机械基件",
    "运输设备",
    "汽车整车",
    "汽车配件",
    "航空",
    "船舶",
    "化学制药",
    "生物制药",
    "医疗保健",
    "医药商业",
    "中成药",
    "环境保护",
    "新型电力",
}

EXCLUDED_INDUSTRY_KEYWORDS = (
    "银行",
    "保险",
    "证券",
    "多元金融",
    "地产",
    "房产",
)


class AStockInnovation100Builder(CustomIndexBuilderBase):
    MARKET_FRAME_LOAD_DAYS = MARKET_FRAME_LOAD_DAYS

    @staticmethod
    def rule_snapshot() -> Dict:
        return {
            "index_code": INDEX_CODE,
            "index_name": INDEX_NAME,
            "base_level": BASE_LEVEL,
            "target_constituent_count": TARGET_CONSTITUENT_COUNT,
            "direct_entry_rank": DIRECT_ENTRY_RANK,
            "retention_rank": RETENTION_RANK,
            "min_listing_days": MIN_LISTING_DAYS,
            "liquidity_window": LIQUIDITY_WINDOW,
            "min_avg_amount_60d": MIN_AVG_AMOUNT_60D,
            "max_single_weight_pct": MAX_SINGLE_WEIGHT * 100,
            "top5_weight_cap_pct": TOP5_WEIGHT_CAP * 100,
            "large_weight_threshold_pct": LARGE_WEIGHT_THRESHOLD * 100,
            "large_weight_cap_pct": LARGE_WEIGHT_CAP * 100,
            "reconstitution": "每年12月最后一个交易日收盘选样，下一交易日生效；初始日当日生效",
            "rebalance": "每季度最后一个交易日收盘调整权重，下一交易日生效",
            "universe": "沪深A股，剔除ST/退市整理、金融地产、上市不足一年和60日成交额不足1亿元的股票",
            "industries": sorted(INNOVATION_INDUSTRIES),
        }

    def _is_basic_eligible(self, ts_code: str, as_of: date, basic: Optional[Dict], intervals: Dict[str, List[Tuple[date, date]]]) -> bool:
        if not basic:
            return False
        if basic.get("exchange") not in {"SSE", "SZSE"}:
            return False
        industry = str(basic.get("industry") or "")
        if not industry or industry not in INNOVATION_INDUSTRIES:
            return False
        if any(keyword in industry for keyword in EXCLUDED_INDUSTRY_KEYWORDS):
            return False
        list_date = basic.get("list_date")
        if not list_date or (as_of - list_date).days < MIN_LISTING_DAYS:
            return False
        delist_date = basic.get("delist_date")
        if delist_date and delist_date <= as_of:
            return False
        if self._is_st_or_retiring(ts_code, as_of, basic, intervals):
            return False
        return True

    def _rank_candidates(
        self,
        market_frame: pl.DataFrame,
        as_of: date,
        basic_map: Dict[str, Dict],
        st_intervals: Dict[str, List[Tuple[date, date]]],
        amount_history: Dict[str, Deque[float]],
    ) -> List[Dict]:
        if _is_market_frame_empty(market_frame):
            return []

        candidates = []
        filtered = market_frame.filter(
            (pl.col("close").is_not_null())
            & (pl.col("close") > 0)
            & (pl.col("circ_mv").is_not_null())
            & (pl.col("circ_mv") > 0)
        )
        for row in filtered.iter_rows(named=True):
            ts_code = str(row.get("ts_code") or "")
            if not ts_code:
                continue
            basic = basic_map.get(ts_code)
            if not self._is_basic_eligible(ts_code, as_of, basic, st_intervals):
                continue
            avg_amount = row.get("avg_amount_60d")
            if avg_amount is None:
                history = amount_history.get(ts_code)
                avg_amount = float(np.mean(history)) if history else 0.0
            else:
                avg_amount = float(avg_amount or 0.0)
            if avg_amount < MIN_AVG_AMOUNT_60D:
                continue
            candidates.append(
                {
                    "ts_code": ts_code,
                    "name": basic.get("name"),
                    "industry": basic.get("industry"),
                    "close": float(row.get("close") or 0.0),
                    "pct_chg": float(row.get("pct_chg") or 0.0),
                    "amount": float(row.get("amount") or 0.0),
                    "avg_amount_60d": avg_amount,
                    "total_mv": float(row.get("total_mv") or 0.0),
                    "circ_mv": float(row.get("circ_mv") or 0.0),
                }
            )

        candidates.sort(key=lambda item: (item["circ_mv"], item["amount"]), reverse=True)
        for rank, item in enumerate(candidates, start=1):
            item["rank"] = rank
        return candidates

    def _select_constituents(
        self,
        ranked: List[Dict],
        previous_symbols: List[str],
        reconstitution: bool,
    ) -> List[Dict]:
        if not ranked:
            return []
        ranked_by_symbol = {item["ts_code"]: item for item in ranked}
        selected_symbols: List[str] = []

        if reconstitution or not previous_symbols:
            for item in ranked[:DIRECT_ENTRY_RANK]:
                selected_symbols.append(item["ts_code"])

            previous_set = set(previous_symbols)
            retained = [
                item["ts_code"]
                for item in ranked[:RETENTION_RANK]
                if item["ts_code"] in previous_set and item["ts_code"] not in selected_symbols
            ]
            selected_symbols.extend(retained)
        else:
            selected_symbols.extend([symbol for symbol in previous_symbols if symbol in ranked_by_symbol])

        if len(selected_symbols) < TARGET_CONSTITUENT_COUNT:
            for item in ranked:
                symbol = item["ts_code"]
                if symbol in selected_symbols:
                    continue
                selected_symbols.append(symbol)
                if len(selected_symbols) >= TARGET_CONSTITUENT_COUNT:
                    break

        selected = [ranked_by_symbol[symbol] for symbol in selected_symbols[:TARGET_CONSTITUENT_COUNT] if symbol in ranked_by_symbol]
        selected.sort(key=lambda item: item["rank"])
        return selected

    @staticmethod
    def _cap_single(weights: np.ndarray, cap: float) -> np.ndarray:
        capped = weights.astype(float).copy()
        if capped.sum() <= 0:
            return capped
        capped = capped / capped.sum()
        for _ in range(20):
            over = capped > cap + 1e-12
            if not over.any():
                break
            excess = float(np.sum(capped[over] - cap))
            capped[over] = cap
            under = ~over
            under_sum = float(np.sum(capped[under]))
            if under_sum <= 0 or excess <= 0:
                break
            capped[under] += capped[under] / under_sum * excess
        return capped / capped.sum() if capped.sum() > 0 else capped

    @classmethod
    def _redistribute_excess(cls, weights: np.ndarray, locked: np.ndarray, excess: float) -> np.ndarray:
        if excess <= 0:
            return weights
        receivers = ~locked
        receiver_sum = float(np.sum(weights[receivers]))
        if receiver_sum <= 0:
            return weights
        weights[receivers] += weights[receivers] / receiver_sum * excess
        return cls._cap_single(weights, MAX_SINGLE_WEIGHT)

    @classmethod
    def _apply_weight_caps(cls, raw_weights: List[float]) -> List[float]:
        weights = np.array(raw_weights, dtype=float)
        if weights.sum() <= 0:
            return [0.0 for _ in raw_weights]
        weights = cls._cap_single(weights / weights.sum(), MAX_SINGLE_WEIGHT)

        for _ in range(10):
            changed = False
            order = np.argsort(-weights)
            top5 = order[:5]
            top5_sum = float(np.sum(weights[top5]))
            if top5_sum > TOP5_WEIGHT_CAP + 1e-12:
                excess = top5_sum - TOP5_WEIGHT_CAP
                weights[top5] *= TOP5_WEIGHT_CAP / top5_sum
                locked = np.zeros(len(weights), dtype=bool)
                locked[top5] = True
                weights = cls._redistribute_excess(weights, locked, excess)
                changed = True

            large = weights > LARGE_WEIGHT_THRESHOLD + 1e-12
            large_sum = float(np.sum(weights[large]))
            if large.any() and large_sum > LARGE_WEIGHT_CAP + 1e-12 and large.sum() < len(weights):
                excess = large_sum - LARGE_WEIGHT_CAP
                weights[large] *= LARGE_WEIGHT_CAP / large_sum
                weights = cls._redistribute_excess(weights, large, excess)
                changed = True

            weights = cls._cap_single(weights, MAX_SINGLE_WEIGHT)
            if not changed:
                break

        weights = weights / weights.sum() if weights.sum() > 0 else weights
        return [float(item) for item in weights]

    def _build_weighted_constituents(
        self,
        selected: List[Dict],
        previous_weight_map: Dict[str, float],
    ) -> Tuple[List[Dict], float]:
        if not selected:
            return [], 0.0
        total_circ_mv = sum(float(item.get("circ_mv") or 0.0) for item in selected)
        raw_weights = [
            float(item.get("circ_mv") or 0.0) / total_circ_mv if total_circ_mv > 0 else 0.0
            for item in selected
        ]
        capped_weights = self._apply_weight_caps(raw_weights)
        weighted = []
        for item, raw_weight, weight in zip(selected, raw_weights, capped_weights):
            row = dict(item)
            row["raw_weight"] = raw_weight
            row["weight"] = weight
            weighted.append(row)

        symbols = {item["ts_code"] for item in weighted}
        turnover = 0.0
        for item in weighted:
            turnover += abs(float(item["weight"]) - float(previous_weight_map.get(item["ts_code"], 0.0)))
        for symbol, previous_weight in previous_weight_map.items():
            if symbol not in symbols:
                turnover += abs(float(previous_weight))
        return weighted, turnover / 2 * 100

    @classmethod
    def _is_quarter_end(cls, trading_dates: List[date], index: int) -> bool:
        return cls._is_period_end(trading_dates, index, "quarter")

    @staticmethod
    def _rebalance_type(current_date: date, is_initial: bool) -> str:
        if is_initial:
            return "inception"
        if current_date.month == 12:
            return "annual_reconstitution"
        return "quarterly_reweight"

    def _save_rebalance(
        self,
        rebalance_date: date,
        effective_date: Optional[date],
        rebalance_type: str,
        constituents: List[Dict],
        previous_symbols: List[str],
        previous_weight_map: Dict[str, float],
        turnover_pct: float,
    ) -> AStockInnovation100Rebalance:
        symbols = [item["ts_code"] for item in constituents]
        previous_set = set(previous_symbols)
        current_set = set(symbols)
        additions = [item for item in constituents if item["ts_code"] not in previous_set]
        removals = [symbol for symbol in previous_symbols if symbol not in current_set]
        total_circ_mv = sum(float(item.get("circ_mv") or 0.0) for item in constituents)

        record = AStockInnovation100Rebalance(
            index_code=INDEX_CODE,
            rebalance_date=rebalance_date,
            effective_date=effective_date,
            rebalance_type=rebalance_type,
            constituent_count=len(constituents),
            turnover_pct=_round_or_none(turnover_pct, 6),
            total_circ_mv=_round_or_none(total_circ_mv, 4),
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

        for item in constituents:
            action = "added" if item["ts_code"] not in previous_set else "retained"
            self.db.add(
                AStockInnovation100Constituent(
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
                    action=action,
                    created_at=datetime.now(),
                )
            )
        self.db.commit()
        return record

    def _delete_existing_index_outputs(self):
        self.db.query(AStockInnovation100Constituent).filter(
            AStockInnovation100Constituent.index_code == INDEX_CODE
        ).delete(synchronize_session=False)
        self.db.query(AStockInnovation100Rebalance).filter(
            AStockInnovation100Rebalance.index_code == INDEX_CODE
        ).delete(synchronize_session=False)
        self.db.query(AStockInnovation100Level).filter(
            AStockInnovation100Level.index_code == INDEX_CODE
        ).delete(synchronize_session=False)
        self.db.commit()

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
            self._progress("清理旧的创新100指数结果", 6)
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
            window_days=MARKET_FRAME_LOAD_DAYS,
        )

        for idx, current_date, market_frame in self._iter_market_frames_by_date(
            trading_dates,
            "载入全市场行情缓存",
            progress_start=50,
            progress_end=89,
        ):
            calc_progress = 50 + int(idx / max(total_dates, 1) * 40)
            if idx == 0 or idx == total_dates - 1 or idx % 20 == 0:
                self._progress(
                    f"计算指数点位 {current_date.isoformat()}",
                    calc_progress,
                    processed_dates=idx + 1,
                    total_dates=total_dates,
                )
            if _is_market_frame_empty(market_frame):
                raise RuntimeError(f"{current_date.isoformat()} 行情缓存为空，请先执行A股基础数据同步")
            self._update_amount_history(market_frame, amount_history)
            if current_date < start_date:
                continue

            is_first_output_day = idx == start_index
            if is_first_output_day:
                ranked = self._rank_candidates(market_frame, current_date, basic_map, st_intervals, amount_history)
                selected = self._select_constituents(ranked, [], reconstitution=True)
                current_constituents, turnover_pct = self._build_weighted_constituents(selected, {})
                current_weight_map = {item["ts_code"]: float(item["weight"]) for item in current_constituents}
                self._save_rebalance(
                    rebalance_date=current_date,
                    effective_date=current_date,
                    rebalance_type="inception",
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
                    market_frame,
                    current_weight_map,
                    basic_map,
                    current_date,
                )
                level *= (1.0 + daily_return)
                high_watermark = max(high_watermark, level)

            drawdown_pct = (level / high_watermark - 1.0) * 100 if high_watermark > 0 else 0.0
            levels.append(
                {
                    "index_code": INDEX_CODE,
                    "date": current_date,
                    "level": _round_or_none(level, 6),
                    "daily_return_pct": _round_or_none(daily_return * 100, 6),
                    "drawdown_pct": _round_or_none(drawdown_pct, 6),
                    "constituent_count": len(current_constituents),
                    "total_circ_mv": _round_or_none(sum(float(item.get("circ_mv") or 0.0) for item in current_constituents), 4),
                    "created_at": datetime.now(),
                    "updated_at": datetime.now(),
                }
            )

            if idx < len(trading_dates) - 1 and not is_first_output_day and self._is_quarter_end(trading_dates, idx):
                ranked = self._rank_candidates(market_frame, current_date, basic_map, st_intervals, amount_history)
                rebalance_type = self._rebalance_type(current_date, is_initial=False)
                selected = self._select_constituents(
                    ranked,
                    [item["ts_code"] for item in current_constituents],
                    reconstitution=rebalance_type == "annual_reconstitution",
                )
                next_constituents, turnover_pct = self._build_weighted_constituents(selected, current_weight_map)
                effective_date = trading_dates[idx + 1]
                self._save_rebalance(
                    rebalance_date=current_date,
                    effective_date=effective_date,
                    rebalance_type=rebalance_type,
                    constituents=next_constituents,
                    previous_symbols=[item["ts_code"] for item in current_constituents],
                    previous_weight_map=current_weight_map,
                    turnover_pct=turnover_pct,
                )
                pending_constituents = next_constituents
                pending_effective_date = effective_date

        self._progress("写入创新100指数点位", 92)
        self._bulk_upsert(AStockInnovation100Level, levels, ["index_code", "date"], batch_size=1000)

        latest_level = levels[-1] if levels else None
        total_return_pct = (
            (float(latest_level["level"]) / BASE_LEVEL - 1.0) * 100
            if latest_level and latest_level.get("level")
            else None
        )
        rebalances_count = self.db.query(AStockInnovation100Rebalance).filter(
            AStockInnovation100Rebalance.index_code == INDEX_CODE
        ).count()
        latest_rebalance = self.db.query(AStockInnovation100Rebalance).filter(
            AStockInnovation100Rebalance.index_code == INDEX_CODE
        ).order_by(AStockInnovation100Rebalance.rebalance_date.desc(), AStockInnovation100Rebalance.id.desc()).first()

        self._progress("A股创新100回跑完成", 100)
        return {
            "index_code": INDEX_CODE,
            "index_name": INDEX_NAME,
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
            self.db.query(AStockInnovation100Constituent)
            .filter(
                AStockInnovation100Constituent.index_code == INDEX_CODE,
                AStockInnovation100Constituent.rebalance_id == rebalance_id,
            )
            .order_by(AStockInnovation100Constituent.rank.asc())
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
            self.db.query(AStockInnovation100Level)
            .filter(AStockInnovation100Level.index_code == INDEX_CODE)
            .order_by(AStockInnovation100Level.date.desc())
            .first()
        )
        if not latest_level:
            return {}

        high_watermark_row = (
            self.db.query(AStockInnovation100Level.level)
            .filter(
                AStockInnovation100Level.index_code == INDEX_CODE,
                AStockInnovation100Level.date <= latest_level.date,
            )
            .order_by(AStockInnovation100Level.level.desc())
            .first()
        )
        high_watermark = float(high_watermark_row[0]) if high_watermark_row and high_watermark_row[0] else float(latest_level.level or BASE_LEVEL)

        effective_rebalance = (
            self.db.query(AStockInnovation100Rebalance)
            .filter(
                AStockInnovation100Rebalance.index_code == INDEX_CODE,
                AStockInnovation100Rebalance.effective_date <= latest_level.date,
            )
            .order_by(AStockInnovation100Rebalance.effective_date.desc(), AStockInnovation100Rebalance.id.desc())
            .first()
        )
        if not effective_rebalance:
            return {}

        current_constituents = self._load_constituents_for_rebalance(effective_rebalance.id)
        if not current_constituents:
            return {}

        pending_rebalance = (
            self.db.query(AStockInnovation100Rebalance)
            .filter(
                AStockInnovation100Rebalance.index_code == INDEX_CODE,
                AStockInnovation100Rebalance.rebalance_date <= latest_level.date,
                AStockInnovation100Rebalance.effective_date > latest_level.date,
                AStockInnovation100Rebalance.effective_date <= as_of,
            )
            .order_by(AStockInnovation100Rebalance.effective_date.asc(), AStockInnovation100Rebalance.id.asc())
            .first()
        )
        pending_constituents = self._load_constituents_for_rebalance(pending_rebalance.id) if pending_rebalance else None

        return {
            "latest_level": latest_level,
            "level": float(latest_level.level or BASE_LEVEL),
            "high_watermark": high_watermark,
            "current_constituents": current_constituents,
            "current_weight_map": {item["ts_code"]: float(item.get("weight") or 0.0) for item in current_constituents},
            "current_effective_date": effective_rebalance.effective_date or effective_rebalance.rebalance_date,
            "pending_constituents": pending_constituents,
            "pending_effective_date": pending_rebalance.effective_date if pending_rebalance else None,
        }

    def refresh_incremental(self, end_date: Optional[date] = None) -> Dict:
        end_date = _parse_date(end_date) or date.today()
        state = self._load_incremental_state(end_date)
        if not state:
            self._progress("未找到可增量续算的创新100结果，执行首次全量回跑", 0)
            return self.rebuild(start_date=DEFAULT_START_DATE, end_date=end_date, force_rebuild_outputs=True)

        latest_level = state["latest_level"]
        latest_date = latest_level.date
        if latest_date >= end_date:
            return {
                "index_code": INDEX_CODE,
                "index_name": INDEX_NAME,
                "mode": "incremental",
                "status": "up_to_date",
                "start_date": latest_date.isoformat(),
                "end_date": end_date.isoformat(),
                "latest_date": latest_date.isoformat(),
                "latest_level": latest_level.level,
                "levels_saved": 0,
                "rebalances_saved": 0,
            }

        # 权重在两次调仓之间随价格漂移，续算前必须从当期成分生效日重放一遍，
        # 否则会用「调仓当日」的权重去接最新点位。回看窗口要覆盖到该生效日。
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
            return {
                "index_code": INDEX_CODE,
                "index_name": INDEX_NAME,
                "mode": "incremental",
                "status": "up_to_date",
                "start_date": latest_date.isoformat(),
                "end_date": end_date.isoformat(),
                "latest_date": latest_date.isoformat(),
                "latest_level": latest_level.level,
                "levels_saved": 0,
                "rebalances_saved": 0,
            }

        basic_map = self._load_basic_map()
        st_intervals = self._load_st_intervals()
        amount_history: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=LIQUIDITY_WINDOW))
        current_constituents: List[Dict] = state["current_constituents"]
        current_weight_map: Dict[str, float] = state["current_weight_map"]
        pending_constituents: Optional[List[Dict]] = state["pending_constituents"]
        pending_effective_date: Optional[date] = state["pending_effective_date"]
        level = float(state["level"])
        high_watermark = float(state["high_watermark"])
        levels: List[Dict] = []
        rebalances_before = self.db.query(AStockInnovation100Rebalance).filter(
            AStockInnovation100Rebalance.index_code == INDEX_CODE
        ).count()

        self._progress(
            "按窗口载入创新100增量行情缓存",
            35,
            latest_date=latest_date.isoformat(),
            end_date=end_date.isoformat(),
            window_days=MARKET_FRAME_LOAD_DAYS,
        )

        total_dates = len(new_trading_dates)
        processed_output_dates = 0
        for current_index, current_date, market_frame in self._iter_market_frames_by_date(
            trading_dates,
            "载入创新100增量行情缓存",
        ):
            is_output_date = current_date > latest_date
            if is_output_date:
                calc_progress = 50 + int(processed_output_dates / max(total_dates, 1) * 40)
                self._progress(
                    f"增量计算创新100指数点位 {current_date.isoformat()}",
                    calc_progress,
                    processed_dates=processed_output_dates + 1,
                    total_dates=total_dates,
                )

            if _is_market_frame_empty(market_frame):
                raise RuntimeError(f"{current_date.isoformat()} 行情缓存为空，请先执行A股基础数据同步")

            self._update_amount_history(market_frame, amount_history)

            if pending_constituents is not None and pending_effective_date == current_date:
                current_constituents = pending_constituents
                current_weight_map = {item["ts_code"]: float(item["weight"]) for item in current_constituents}
                pending_constituents = None
                pending_effective_date = None

            if current_date >= weights_valid_from:
                daily_return, current_weight_map = self._advance_weights(
                    market_frame,
                    current_weight_map,
                    basic_map,
                    current_date,
                )
            else:
                daily_return = 0.0

            # Quarter-end can already be the latest persisted level when the task
            # first sees the next quarter's opening trading day.  Detect the
            # boundary across the whole lookback window, not only on new output
            # dates, otherwise that rebalance is skipped forever.
            if current_index < len(trading_dates) - 1 and self._is_quarter_end(trading_dates, current_index):
                existing_rebalance = (
                    self.db.query(AStockInnovation100Rebalance)
                    .filter(
                        AStockInnovation100Rebalance.index_code == INDEX_CODE,
                        AStockInnovation100Rebalance.rebalance_date == current_date,
                    )
                    .first()
                )
                if not existing_rebalance:
                    ranked = self._rank_candidates(market_frame, current_date, basic_map, st_intervals, amount_history)
                    rebalance_type = self._rebalance_type(current_date, is_initial=False)
                    selected = self._select_constituents(
                        ranked,
                        [item["ts_code"] for item in current_constituents],
                        reconstitution=rebalance_type == "annual_reconstitution",
                    )
                    next_constituents, turnover_pct = self._build_weighted_constituents(selected, current_weight_map)
                    effective_date = trading_dates[current_index + 1]
                    self._save_rebalance(
                        rebalance_date=current_date,
                        effective_date=effective_date,
                        rebalance_type=rebalance_type,
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
            levels.append(
                {
                    "index_code": INDEX_CODE,
                    "date": current_date,
                    "level": _round_or_none(level, 6),
                    "daily_return_pct": _round_or_none(daily_return * 100, 6),
                    "drawdown_pct": _round_or_none(drawdown_pct, 6),
                    "constituent_count": len(current_constituents),
                    "total_circ_mv": _round_or_none(sum(float(item.get("circ_mv") or 0.0) for item in current_constituents), 4),
                    "created_at": datetime.now(),
                    "updated_at": datetime.now(),
                }
            )

        self._progress("写入创新100增量指数点位", 92)
        self._bulk_upsert(AStockInnovation100Level, levels, ["index_code", "date"], batch_size=1000)

        latest_saved = levels[-1] if levels else latest_level
        rebalances_after = self.db.query(AStockInnovation100Rebalance).filter(
            AStockInnovation100Rebalance.index_code == INDEX_CODE
        ).count()
        self._progress("A股创新100增量刷新完成", 100)
        return {
            "index_code": INDEX_CODE,
            "index_name": INDEX_NAME,
            "mode": "incremental",
            "status": "completed",
            "start_date": new_trading_dates[0].isoformat(),
            "end_date": end_date.isoformat(),
            "latest_date": latest_saved["date"].isoformat() if isinstance(latest_saved, dict) else latest_saved.date.isoformat(),
            "latest_level": latest_saved.get("level") if isinstance(latest_saved, dict) else latest_saved.level,
            "levels_saved": len(levels),
            "rebalances_saved": rebalances_after - rebalances_before,
            "last_market_date": new_trading_dates[-1].isoformat(),
        }


def rebuild_a_stock_innovation100(
    db: Session,
    start_date: date = DEFAULT_START_DATE,
    end_date: Optional[date] = None,
    progress_callback: Optional[ProgressCallback] = None,
) -> Dict:
    builder = AStockInnovation100Builder(db, progress_callback=progress_callback)
    try:
        return builder.rebuild(start_date=start_date, end_date=end_date, force_rebuild_outputs=True)
    finally:
        builder.close()


def load_a_stock_innovation100_summary(db: Session) -> Dict:
    latest_level = db.query(AStockInnovation100Level).filter(
        AStockInnovation100Level.index_code == INDEX_CODE
    ).order_by(AStockInnovation100Level.date.desc()).first()
    first_level = db.query(AStockInnovation100Level).filter(
        AStockInnovation100Level.index_code == INDEX_CODE
    ).order_by(AStockInnovation100Level.date.asc()).first()
    latest_rebalance = db.query(AStockInnovation100Rebalance).filter(
        AStockInnovation100Rebalance.index_code == INDEX_CODE
    ).order_by(AStockInnovation100Rebalance.rebalance_date.desc(), AStockInnovation100Rebalance.id.desc()).first()
    if not latest_level or not first_level:
        return {
            "index_code": INDEX_CODE,
            "index_name": INDEX_NAME,
            "rule_snapshot": AStockInnovation100Builder.rule_snapshot(),
            "has_data": False,
        }

    level_rows = db.query(AStockInnovation100Level).filter(
        AStockInnovation100Level.index_code == INDEX_CODE
    ).order_by(AStockInnovation100Level.date.asc()).all()
    returns = [
        float(row.daily_return_pct) / 100.0
        for row in level_rows
        if row.daily_return_pct is not None
    ]
    total_return_pct = (latest_level.level / first_level.level - 1.0) * 100 if first_level.level else None
    years = max((latest_level.date - first_level.date).days / 365.25, 1 / 365.25)
    annualized_return_pct = ((latest_level.level / first_level.level) ** (1 / years) - 1.0) * 100 if first_level.level else None
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
        "rebalances_count": db.query(AStockInnovation100Rebalance).filter(
            AStockInnovation100Rebalance.index_code == INDEX_CODE
        ).count(),
        "latest_rebalance_id": latest_rebalance.id if latest_rebalance else None,
        "latest_rebalance_date": latest_rebalance.rebalance_date.isoformat() if latest_rebalance else None,
        "latest_effective_date": latest_rebalance.effective_date.isoformat() if latest_rebalance and latest_rebalance.effective_date else None,
        "rule_snapshot": AStockInnovation100Builder.rule_snapshot(),
    }

"""自算微盘400指数的编制规则测试。

用测试专用的 DuckDB / SQLite（conftest 已把路径指到临时目录）跑真实 builder，
覆盖选样口径、等权、月末调样次日生效、期间权重漂移和停牌/退市处理。
"""
from datetime import date, datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import pandas as pd
import polars as pl
from src.core.analytics_database import (
    AStockBasic,
    AStockMarketDaily,
    AStockNameChange,
    AnalyticsSession,
)
from src.core.database import (
    AStockMicro400Constituent,
    AStockMicro400Level,
    AStockMicro400Rebalance,
    Session,
)
from src.robot.a_stock_custom_index_base import CustomIndexBuilderBase
from src.robot.a_stock_micro400 import INDEX_CODE, AStockMicro400Builder


TRADING_DATES = [
    date(2026, 1, 28),
    date(2026, 1, 29),
    date(2026, 1, 30),  # 1月最后一个交易日 -> 调样日
    date(2026, 2, 2),   # 新成分生效日
    date(2026, 2, 3),
]


def _basic(ts_code, name, exchange="SZSE", list_date=date(2015, 1, 1), delist_date=None):
    return {
        "ts_code": ts_code,
        "symbol": ts_code.split(".")[0],
        "name": name,
        "area": "深圳",
        "industry": "软件服务",
        "market": "主板",
        "exchange": exchange,
        "list_date": list_date,
        "delist_date": delist_date,
        "list_status": "L",
    }


class AStockMicro400BuilderTest(TestCase):
    """真实跑一遍 rebuild，只把选样上限调小以便用少量样本验证口径。"""

    maxDiff = None

    def setUp(self):
        self.db = Session()
        self._clear_sqlite()
        self._seed_analytics()
        self.addCleanup(self._clear_sqlite)
        self.addCleanup(Session.remove)

    def _clear_sqlite(self):
        for model in (AStockMicro400Constituent, AStockMicro400Rebalance, AStockMicro400Level):
            self.db.query(model).delete(synchronize_session=False)
        self.db.commit()

    def _seed_analytics(self):
        analytics_db = AnalyticsSession()
        try:
            analytics_db.query(AStockMarketDaily).delete(synchronize_session=False)
            analytics_db.query(AStockBasic).delete(synchronize_session=False)
            analytics_db.query(AStockNameChange).delete(synchronize_session=False)
            now = datetime.now()
            analytics_db.add_all([
                AStockBasic(updated_at=now, **row) for row in self._basic_rows()
            ])
            analytics_db.add_all([
                AStockMarketDaily(created_at=now, updated_at=now, **row) for row in self._market_rows()
            ])
            analytics_db.commit()
        finally:
            AnalyticsSession.remove()

    def _basic_rows(self):
        return [
            _basic("000001.SZ", "微盘甲"),
            _basic("000002.SZ", "微盘乙"),
            _basic("000003.SZ", "微盘丙"),
            _basic("000004.SZ", "中盘丁"),
            _basic("000005.SZ", "ST戊"),
            _basic("000006.SZ", "退市己"),
            _basic("000007.SZ", "次新庚", list_date=date(2026, 1, 20)),
            _basic("830001.BJ", "北证辛", exchange="BSE"),
        ]

    def _market_rows(self):
        # total_mv 从小到大：庚(5) < 甲(10) < 乙(20) < 丙(30) < 丁(100)；
        # ST戊/退市己/北证辛 都该被剔除，次新庚在开板前也不能入选。
        market_caps = {
            "000001.SZ": 10.0,
            "000002.SZ": 20.0,
            "000003.SZ": 30.0,
            "000004.SZ": 100.0,
            "000005.SZ": 1.0,
            "000006.SZ": 2.0,
            "000007.SZ": 5.0,
            "830001.BJ": 3.0,
        }
        rows = []
        for trade_date in TRADING_DATES:
            for ts_code, total_mv in market_caps.items():
                # 次新庚在 1/30 之前一直一字涨停（high == low），当天才开板。
                unopened = ts_code == "000007.SZ" and trade_date < date(2026, 1, 30)
                # 甲在 2/2 涨 10%，其余标的当天不动，用来验证权重漂移。
                pct_chg = 10.0 if (ts_code == "000001.SZ" and trade_date == date(2026, 2, 2)) else 0.0
                close = 10.0
                rows.append({
                    "ts_code": ts_code,
                    "trade_date": trade_date,
                    "open": close,
                    "high": close if unopened else close * 1.05,
                    "low": close,
                    "close": close,
                    "pct_chg": pct_chg,
                    "vol": 1000.0,
                    "amount": 1000.0,
                    "total_mv": total_mv,
                    "circ_mv": total_mv,
                })
        return rows

    def _run_rebuild(self, target_count=3):
        builder = AStockMicro400Builder(self.db)
        try:
            with patch("src.robot.a_stock_custom_index_base.MIN_MARKET_DAILY_ROWS", 1), \
                 patch("src.robot.a_stock_micro400.TARGET_CONSTITUENT_COUNT", target_count):
                return builder.rebuild(
                    start_date=TRADING_DATES[0],
                    end_date=TRADING_DATES[-1],
                    force_rebuild_outputs=True,
                )
        finally:
            builder.close()

    def _constituents(self, rebalance_id):
        rows = (
            self.db.query(AStockMicro400Constituent)
            .filter(AStockMicro400Constituent.rebalance_id == rebalance_id)
            .order_by(AStockMicro400Constituent.rank.asc())
            .all()
        )
        return [(row.ts_code, row.weight_pct) for row in rows]

    def test_selects_smallest_total_mv_and_excludes_st_delisted_bse_and_unopened_new_listings(self):
        result = self._run_rebuild()
        self.assertEqual(result["index_code"], INDEX_CODE)
        self.assertEqual(result["levels_saved"], len(TRADING_DATES))

        inception = (
            self.db.query(AStockMicro400Rebalance)
            .filter(AStockMicro400Rebalance.rebalance_type == "inception")
            .one()
        )
        self.assertEqual(inception.rebalance_date, TRADING_DATES[0])
        self.assertEqual(inception.effective_date, TRADING_DATES[0])
        # 建仓日次新庚还没开板，所以是甲/乙/丙这三只最小的正常股。
        self.assertEqual(
            self._constituents(inception.id),
            [(code, round(100 / 3, 6)) for code in ("000001.SZ", "000002.SZ", "000003.SZ")],
        )

    def test_month_end_reconstitution_takes_effect_next_trading_day(self):
        self._run_rebuild()
        monthly = (
            self.db.query(AStockMicro400Rebalance)
            .filter(AStockMicro400Rebalance.rebalance_type == "monthly_reconstitution")
            .order_by(AStockMicro400Rebalance.rebalance_date.asc())
            .all()
        )
        self.assertEqual([row.rebalance_date for row in monthly], [date(2026, 1, 30)])
        self.assertEqual(monthly[0].effective_date, date(2026, 2, 2))
        # 1/30 次新庚开板，市值 5 < 甲 10，挤掉最大的丙。
        self.assertEqual(
            [ts_code for ts_code, _ in self._constituents(monthly[0].id)],
            ["000007.SZ", "000001.SZ", "000002.SZ"],
        )

    def test_weights_drift_with_prices_between_reconstitutions(self):
        self._run_rebuild()
        levels = {
            row.date: row
            for row in self.db.query(AStockMicro400Level).filter(
                AStockMicro400Level.index_code == INDEX_CODE
            ).all()
        }
        # 2/2 生效当天用等权 1/3，甲涨 10% -> 指数 +3.3333%
        self.assertAlmostEqual(levels[date(2026, 2, 2)].daily_return_pct, 10.0 / 3, places=6)
        self.assertAlmostEqual(levels[date(2026, 2, 2)].level, 1000 * (1 + 0.1 / 3), places=6)
        # 建仓日收益记 0，之后无涨跌的日子点位不变。
        self.assertAlmostEqual(levels[TRADING_DATES[0]].daily_return_pct, 0.0, places=9)
        self.assertAlmostEqual(levels[date(2026, 2, 3)].daily_return_pct, 0.0, places=9)


class AdvanceWeightsTest(TestCase):
    """基类的权重漂移：停牌按 0 收益保留权重，退市剔除后归一。"""

    @staticmethod
    def _frame(rows):
        return pl.DataFrame(
            {"ts_code": [r[0] for r in rows], "pct_chg": [r[1] for r in rows]}
        )

    def test_weights_drift_with_returns(self):
        daily_return, weights = CustomIndexBuilderBase._advance_weights(
            self._frame([("A.SZ", 10.0), ("B.SZ", 0.0)]),
            {"A.SZ": 0.5, "B.SZ": 0.5},
        )
        self.assertAlmostEqual(daily_return, 0.05, places=9)
        self.assertAlmostEqual(weights["A.SZ"], 0.55 / 1.05, places=9)
        self.assertAlmostEqual(weights["B.SZ"], 0.5 / 1.05, places=9)

    def test_suspended_constituent_counts_as_zero_return_and_keeps_weight(self):
        daily_return, weights = CustomIndexBuilderBase._advance_weights(
            self._frame([("A.SZ", 10.0)]),  # B.SZ 当天停牌，行情里没有这一行
            {"A.SZ": 0.5, "B.SZ": 0.5},
        )
        self.assertAlmostEqual(daily_return, 0.05, places=9)
        self.assertIn("B.SZ", weights)
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=9)

    def test_delisted_constituent_is_dropped_and_remaining_weights_renormalized(self):
        basic_map = {
            "A.SZ": {"delist_date": None},
            "B.SZ": {"delist_date": date(2026, 1, 5)},
        }
        daily_return, weights = CustomIndexBuilderBase._advance_weights(
            self._frame([("A.SZ", 10.0), ("B.SZ", -50.0)]),
            {"A.SZ": 0.5, "B.SZ": 0.5},
            basic_map,
            date(2026, 1, 10),
        )
        self.assertEqual(set(weights), {"A.SZ"})
        # B 已退市，当天不再计入收益，A 独自承担 100% 权重。
        self.assertAlmostEqual(daily_return, 0.10, places=9)
        self.assertAlmostEqual(weights["A.SZ"], 1.0, places=9)

    def test_empty_weight_map_is_safe(self):
        self.assertEqual(CustomIndexBuilderBase._advance_weights(self._frame([]), {}), (0.0, {}))


class AStockMicro400IncrementalTest(TestCase):
    def test_incremental_backfills_month_end_already_persisted_as_level(self):
        """月末正好是最新点位时，下次增量刷新仍要补上这次调样。"""
        builder = object.__new__(AStockMicro400Builder)
        builder.db = MagicMock()
        query = builder.db.query.return_value.filter.return_value
        query.count.side_effect = [1, 2]
        query.first.return_value = None

        old_constituents = [{"ts_code": "OLD.SZ", "weight": 1.0, "total_mv": 100.0}]
        new_constituents = [{"ts_code": "NEW.SZ", "weight": 1.0, "total_mv": 90.0}]
        builder._load_incremental_state = MagicMock(
            return_value={
                "latest_level": SimpleNamespace(date=date(2026, 1, 30), level=1000.0),
                "level": 1000.0,
                "high_watermark": 1000.0,
                "current_constituents": old_constituents,
                "current_weight_map": {"OLD.SZ": 1.0},
                "current_effective_date": date(2026, 1, 30),
                "pending_constituents": None,
                "pending_effective_date": None,
            }
        )
        trading_dates = [date(2026, 1, 30), date(2026, 2, 2)]
        builder._cached_market_trading_dates = MagicMock(return_value=trading_dates)
        builder._existing_market_day_stats = MagicMock(return_value={})
        builder._market_day_needs_refresh = MagicMock(return_value=False)
        builder._load_basic_map = MagicMock(return_value={})
        builder._load_st_intervals = MagicMock(return_value={})
        market_frame = pl.DataFrame({"ts_code": ["OLD.SZ"], "amount": [1.0], "pct_chg": [0.0], "high": [1.0], "low": [1.0]})
        builder._iter_market_frames_by_date = MagicMock(
            return_value=iter([(0, trading_dates[0], market_frame), (1, trading_dates[1], market_frame)])
        )
        builder._update_amount_history = MagicMock()
        builder._rank_candidates = MagicMock(return_value=new_constituents)
        builder._build_weighted_constituents = MagicMock(return_value=(new_constituents, 100.0))
        builder._save_rebalance = MagicMock(return_value=7)
        builder._bulk_upsert = MagicMock()
        builder._progress = MagicMock()

        result = builder.refresh_incremental(end_date=date(2026, 2, 2))

        builder._save_rebalance.assert_called_once()
        save_kwargs = builder._save_rebalance.call_args.kwargs
        self.assertEqual(save_kwargs["rebalance_date"], date(2026, 1, 30))
        self.assertEqual(save_kwargs["effective_date"], date(2026, 2, 2))
        self.assertEqual(save_kwargs["rebalance_type"], "monthly_reconstitution")
        self.assertEqual(result["levels_saved"], 1)
        self.assertEqual(result["rebalances_saved"], 1)


class Micro400FearGreedSourceTest(TestCase):
    """贪恐服务要能把 MICRO400.CN 认成自算指数，并只加载覆盖计算窗口的成分快照。"""

    def setUp(self):
        from src.core.services.a_stock_fear_greed_clone_service import (
            AStockInnovation100FearGreedCloneCalculator,
        )

        self.calculator_cls = AStockInnovation100FearGreedCloneCalculator
        self.db = Session()
        self._clear()
        self.addCleanup(self._clear)
        self.addCleanup(Session.remove)

    def _clear(self):
        for model in (AStockMicro400Constituent, AStockMicro400Rebalance, AStockMicro400Level):
            self.db.query(model).delete(synchronize_session=False)
        self.db.commit()

    def _seed(self):
        now = datetime.now()
        # 三期调样：1/5、2/2、3/2 生效，各带一只可辨认的成分股。
        for index, (rebalance_date, effective_date, ts_code) in enumerate([
            (date(2025, 12, 31), date(2026, 1, 5), "OLD.SZ"),
            (date(2026, 1, 30), date(2026, 2, 2), "MID.SZ"),
            (date(2026, 2, 27), date(2026, 3, 2), "NEW.SZ"),
        ]):
            rebalance = AStockMicro400Rebalance(
                index_code=INDEX_CODE,
                rebalance_date=rebalance_date,
                effective_date=effective_date,
                rebalance_type="monthly_reconstitution",
                constituent_count=1,
                created_at=now,
            )
            self.db.add(rebalance)
            self.db.flush()
            self.db.add(AStockMicro400Constituent(
                index_code=INDEX_CODE,
                rebalance_id=rebalance.id,
                ts_code=ts_code,
                rebalance_date=rebalance_date,
                effective_date=effective_date,
                name=ts_code,
                rank=index + 1,
                weight_pct=100.0,
                created_at=now,
            ))
        for offset in range(3):
            self.db.add(AStockMicro400Level(
                index_code=INDEX_CODE,
                date=date(2026, 2, 10 + offset),
                level=1000.0 + offset,
                daily_return_pct=0.0,
                drawdown_pct=0.0,
                constituent_count=1,
                created_at=now,
                updated_at=now,
            ))
        self.db.commit()

    def test_custom_index_source_is_registered(self):
        calculator = self.calculator_cls("MICRO400.CN")
        self.assertIsNotNone(calculator.custom_index_source)
        self.assertIs(calculator.custom_index_source["level"], AStockMicro400Level)
        self.assertEqual(calculator.label, "微盘400")

    def test_levels_come_from_the_micro400_table(self):
        self._seed()
        levels = self.calculator_cls("MICRO400.CN")._load_levels(date(2026, 2, 1), date(2026, 2, 28))
        self.assertEqual(len(levels), 3)
        self.assertAlmostEqual(float(levels["level"].iloc[0]), 1000.0)

    def test_holdings_only_load_snapshots_covering_the_window(self):
        self._seed()
        index = pd.DatetimeIndex([date(2026, 2, 10), date(2026, 2, 11), date(2026, 2, 12)], name="date")
        holdings, as_of = self.calculator_cls("MICRO400.CN")._build_holdings_by_date(index)

        self.assertEqual(len(holdings), 3)
        for timestamp in index:
            self.assertEqual([item["symbol"] for item in holdings[timestamp]], ["MID.SZ"])
            self.assertEqual(as_of[timestamp], date(2026, 2, 2))

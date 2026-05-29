from unittest import TestCase

from src.robot.xueqiu_top_holdings_report import (
    build_equal_top10_top12_buffer_plan,
    rounded_rebalance_weights,
)


class XueqiuTopHoldingsReportTest(TestCase):
    def test_buffer_plan_keeps_retained_weights_and_allocates_sold_weight_to_new_buy(self):
        ranking_symbols = [
            ("SZ.300757", "罗博特科"),
            ("SZ.002384", "东山精密"),
            ("SZ.300394", "天孚通信"),
            ("SZ.300502", "新易盛"),
            ("SH.603083", "剑桥科技"),
            ("SZ.300308", "中际旭创"),
            ("SZ.300476", "胜宏科技"),
            ("SH.603986", "兆易创新"),
            ("SH.688008", "澜起科技"),
            ("SH.601138", "工业富联"),
            ("SZ.000001", "样例一"),
            ("SZ.000002", "样例二"),
        ]
        ranking = [
            {
                "stock_symbol": symbol,
                "stock_name": name,
                "composite_weight_pct": 12.0 - index,
            }
            for index, (symbol, name) in enumerate(ranking_symbols)
        ]
        current_holdings = [
            {"stock_symbol": "SZ300757", "stock_name": "罗博特科", "weight": 10.13},
            {"stock_symbol": "SZ002384", "stock_name": "东山精密", "weight": 10.01},
            {"stock_symbol": "SZ300394", "stock_name": "天孚通信", "weight": 10.48},
            {"stock_symbol": "SZ300502", "stock_name": "新易盛", "weight": 10.16},
            {"stock_symbol": "SZ300136", "stock_name": "信维通信", "weight": 8.65},
            {"stock_symbol": "SH603083", "stock_name": "剑桥科技", "weight": 10.50},
            {"stock_symbol": "SZ300308", "stock_name": "中际旭创", "weight": 10.06},
            {"stock_symbol": "SZ300476", "stock_name": "胜宏科技", "weight": 10.15},
            {"stock_symbol": "SH603986", "stock_name": "兆易创新", "weight": 9.94},
            {"stock_symbol": "SH688008", "stock_name": "澜起科技", "weight": 9.91},
        ]

        plan = build_equal_top10_top12_buffer_plan(
            ranking=ranking,
            current_holdings=current_holdings,
            top_n=10,
            sell_rank=12,
        )
        rounded_items = rounded_rebalance_weights(plan["target_items"])
        weights = {
            item["stock_symbol"]: item["rebalance_weight_pct"]
            for item in rounded_items
        }

        self.assertTrue(plan["component_changed"])
        self.assertEqual(["SZ.300136"], plan["removed_symbols"])
        self.assertEqual(["SH.601138"], plan["added_symbols"])
        self.assertEqual(100.0, round(sum(weights.values()), 2))
        self.assertEqual(10.13, weights["SZ.300757"])
        self.assertEqual(10.01, weights["SZ.002384"])
        self.assertEqual(10.48, weights["SZ.300394"])
        self.assertEqual(10.50, weights["SH.603083"])
        self.assertEqual(9.91, weights["SH.688008"])
        self.assertEqual(8.66, weights["SH.601138"])

    def test_buffer_plan_adjusts_retained_weight_only_when_it_exceeds_tolerance(self):
        ranking_symbols = [
            "SZ.300757",
            "SZ.002384",
            "SZ.300394",
            "SZ.300502",
            "SH.603083",
            "SZ.300308",
            "SZ.300476",
            "SH.603986",
            "SH.688008",
            "SH.601138",
            "SZ.000001",
            "SZ.000002",
        ]
        ranking = [
            {
                "stock_symbol": symbol,
                "stock_name": symbol,
                "composite_weight_pct": 12.0 - index,
            }
            for index, symbol in enumerate(ranking_symbols)
        ]
        current_holdings = [
            {"stock_symbol": "SZ300757", "weight": 10.13},
            {"stock_symbol": "SZ002384", "weight": 10.01},
            {"stock_symbol": "SZ300394", "weight": 11.20},
            {"stock_symbol": "SZ300502", "weight": 10.16},
            {"stock_symbol": "SZ300136", "weight": 8.65},
            {"stock_symbol": "SH603083", "weight": 10.50},
            {"stock_symbol": "SZ300308", "weight": 10.06},
            {"stock_symbol": "SZ300476", "weight": 10.15},
            {"stock_symbol": "SH603986", "weight": 9.94},
            {"stock_symbol": "SH688008", "weight": 9.91},
        ]

        plan = build_equal_top10_top12_buffer_plan(
            ranking=ranking,
            current_holdings=current_holdings,
            top_n=10,
            sell_rank=12,
        )
        rounded_items = rounded_rebalance_weights(plan["target_items"])
        by_symbol = {item["stock_symbol"]: item for item in rounded_items}

        self.assertEqual("adjust", by_symbol["SZ.300394"]["strategy_action"])
        self.assertEqual(10.0, by_symbol["SZ.300394"]["rebalance_weight_pct"])
        self.assertEqual("keep", by_symbol["SZ.300757"]["strategy_action"])
        self.assertEqual(9.14, by_symbol["SH.601138"]["rebalance_weight_pct"])

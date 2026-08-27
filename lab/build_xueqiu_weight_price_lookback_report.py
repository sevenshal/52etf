#!/usr/bin/env python3
"""Build the canonical report artifact for the weight/price lookback study."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd


OUTPUT = Path("lab/output/xueqiu_weight_price_lookback_comparison_20260701_20260826")


def _records(frame: pd.DataFrame) -> list[dict]:
    return json.loads(frame.to_json(orient="records"))


def main() -> None:
    signal = pd.read_csv(OUTPUT / "signal_metrics.csv")
    factor = pd.read_csv(OUTPUT / "factor_metrics.csv")
    pairwise = pd.read_csv(OUTPUT / "pairwise.csv")
    overlap = pd.read_csv(OUTPUT / "overlap.csv")
    metadata = json.loads((OUTPUT / "metrics.json").read_text(encoding="utf-8"))

    signal["lookback_label"] = signal["lookback_days"].map(lambda value: f"{value}日")
    signal["horizon_label"] = signal["horizon_days"].map(lambda value: f"{value}日")
    signal_chart = signal[
        [
            "lookback_days",
            "lookback_label",
            "horizon_days",
            "horizon_label",
            "signal_instances",
            "signal_dates",
            "avg_signals_per_date",
            "gross_return",
            "gross_excess",
            "net_excess",
            "net_excess_win_rate",
            "net_excess_p",
        ]
    ].copy()

    signal_5d = signal[signal["horizon_days"] == 5].set_index("lookback_days")
    signal_10d = signal[signal["horizon_days"] == 10].set_index("lookback_days")
    factor_5d = factor[factor["horizon_days"] == 5].set_index("lookback_days")
    pair_5d = pairwise[pairwise["horizon_days"] == 5]

    def paired_delta(lookback: int) -> tuple[float, float | None]:
        if lookback == 5:
            return 0.0, None
        row = pair_5d[
            (pair_5d["left_lookback_days"] == lookback)
            & (pair_5d["right_lookback_days"] == 5)
        ].iloc[0]
        return float(row["left_minus_right_net_excess"]), float(row["difference_p"])

    summary_rows = []
    for lookback in (1, 3, 5):
        delta, delta_p = paired_delta(lookback)
        summary_rows.append(
            {
                "lookback_days": lookback,
                "lookback_label": f"{lookback}日",
                "signal_instances_5d": int(signal_5d.loc[lookback, "signal_instances"]),
                "signal_dates_5d": int(signal_5d.loc[lookback, "signal_dates"]),
                "net_excess_5d": float(signal_5d.loc[lookback, "net_excess"]),
                "win_rate_5d": float(signal_5d.loc[lookback, "net_excess_win_rate"]),
                "net_excess_10d": float(signal_10d.loc[lookback, "net_excess"]),
                "rank_ic_5d": float(factor_5d.loc[lookback, "rank_ic"]),
                "top_bottom_spread_5d": float(
                    factor_5d.loc[lookback, "top_bottom_spread"]
                ),
                "paired_delta_vs_5d": delta,
                "paired_delta_p": delta_p,
            }
        )

    headline = [
        {
            "five_5d_net_excess": float(signal_5d.loc[5, "net_excess"]),
            "three_5d_net_excess": float(signal_5d.loc[3, "net_excess"]),
            "one_5d_net_excess": float(signal_5d.loc[1, "net_excess"]),
            "five_10d_net_excess": float(signal_10d.loc[5, "net_excess"]),
            "three_10d_net_excess": float(signal_10d.loc[3, "net_excess"]),
            "one_10d_net_excess": float(signal_10d.loc[1, "net_excess"]),
            "five_rank_ic": float(factor_5d.loc[5, "rank_ic"]),
            "three_rank_ic": float(factor_5d.loc[3, "rank_ic"]),
            "one_rank_ic": float(factor_5d.loc[1, "rank_ic"]),
            "common_stock_days": int(metadata["common_stock_days"]),
            "common_snapshot_dates": int(metadata["common_snapshot_dates"]),
        }
    ]

    source = {
        "id": "xueqiu_lookback_study",
        "label": "雪球持仓快照与后复权日线共同样本研究",
        "path": "lab/compare_xueqiu_weight_price_lookbacks.py",
        "query": {
            "engine": "DuckDB + Python 3.12",
            "language": "python",
            "query": (
                "python lab/study_xueqiu_weight_price_ratio.py --lookback-days {1|3|5} "
                "--start 2026-07-01 --end 2026-08-26; "
                "python lab/compare_xueqiu_weight_price_lookbacks.py"
            ),
            "sql": """
WITH raw AS (
  SELECT snapshot_date, cube_symbol, stock_symbol,
         any_value(stock_name) AS stock_name, sum(weight_pct) AS weight_pct
  FROM xueqiu_cube_holdings_snapshots
  WHERE coalesce(is_active, false) AND weight_pct > 0
    AND snapshot_date >= DATE '2026-06-25'
  GROUP BY 1, 2, 3
), date_summary AS (
  SELECT snapshot_date, count(DISTINCT cube_symbol) AS cube_count
  FROM raw GROUP BY 1
), stock_summary AS (
  SELECT raw.snapshot_date, raw.stock_symbol, any_value(raw.stock_name) AS stock_name,
         count(DISTINCT raw.cube_symbol) AS holding_cubes,
         sum(raw.weight_pct) AS total_weight,
         sum(raw.weight_pct) / nullif(max(date_summary.cube_count), 0) AS composite_weight
  FROM raw JOIN date_summary USING (snapshot_date)
  WHERE raw.stock_symbol <> 'CASH'
  GROUP BY 1, 2
)
SELECT stock_summary.*,
       prices.open AS snapshot_open, prices.close AS snapshot_close
FROM stock_summary
LEFT JOIN a_stock_market_daily_qfq prices
  ON prices.ts_code = substr(stock_summary.stock_symbol, 4, 6) || '.' || substr(stock_summary.stock_symbol, 1, 2)
 AND prices.trade_date = stock_summary.snapshot_date
WHERE stock_summary.snapshot_date BETWEEN DATE '2026-07-01' AND DATE '2026-08-26'
ORDER BY stock_summary.snapshot_date, stock_summary.stock_symbol
""".strip(),
            "description": "按1/3/5个更早有效快照日计算权价比，并在共同股票日上比较生产门槛信号与连续因子。",
            "executed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "tables_used": [
                "xueqiu_cube_holdings_snapshots",
                "a_stock_market_daily_qfq",
            ],
            "filters": [
                "持仓快照不早于2026-06-25",
                "仅活跃雪球组合、正权重、非现金股票",
                "共同样本为三种回看周期均可计算的股票-快照日",
                "信号在收盘快照后形成，下一交易日开盘进入",
                "生产门槛：Top100、至少5组合、组合增加至少1、权重增加、权重倍数>1.05、股价倍数<1、权价比1.23–6",
            ],
            "metric_definitions": [
                "权价比 = 当前综合权重/基准日综合权重 ÷ 当前收盘价/基准日收盘价；综合权重按当日活跃组合数归一。",
                "净超额 = 每日信号股票等权远期收益 - 同日共同股票池等权收益 - 20 bps往返成本，再对信号日等权平均。",
                "Rank IC = 每个快照日权价比与远期收益的Spearman相关系数，再对日期等权平均。",
                "Top-Bottom = 每日权价比最高十分位收益减最低十分位收益，再对日期等权平均。",
            ],
        },
    }

    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    title = "雪球权价比 1/3/5 日回看对比"
    manifest = {
        "version": 1,
        "surface": "report",
        "title": title,
        "description": "星澜叁号权价比回看周期的共同样本、无未来函数对比。",
        "generatedAt": generated_at,
        "sources": [source],
        "cards": [
            {
                "id": "card_5d_return",
                "description": "生产门槛信号持有5日，相对共同股票池并扣20 bps。",
                "dataset": "headline",
                "sourceId": source["id"],
                "metrics": [
                    {
                        "label": "5日版 · 5日净超额",
                        "field": "five_5d_net_excess",
                        "format": "percent",
                        "signed": True,
                    },
                    {
                        "label": "3日版",
                        "field": "three_5d_net_excess",
                        "format": "percent",
                        "signed": True,
                    },
                ],
            },
            {
                "id": "card_10d_return",
                "description": "生产门槛信号持有10日，相对共同股票池并扣20 bps。",
                "dataset": "headline",
                "sourceId": source["id"],
                "metrics": [
                    {
                        "label": "5日版 · 10日净超额",
                        "field": "five_10d_net_excess",
                        "format": "percent",
                        "signed": True,
                    },
                    {
                        "label": "3日版",
                        "field": "three_10d_net_excess",
                        "format": "percent",
                        "signed": True,
                    },
                ],
            },
            {
                "id": "card_rank_ic",
                "description": "每日截面Spearman Rank IC按日期等权平均。",
                "dataset": "headline",
                "sourceId": source["id"],
                "metrics": [
                    {"label": "5日版 · 5日Rank IC", "field": "five_rank_ic", "format": "number"},
                    {"label": "3日版", "field": "three_rank_ic", "format": "number"},
                ],
            },
        ],
        # Chart map: this grouped comparison answers whether shortening the ratio
        # baseline improves net excess consistently across holding horizons.
        "charts": [
            {
                "id": "chart_net_excess",
                "title": "生产门槛净超额",
                "subtitle": "下一交易日开盘进入并扣20 bps；零线表示与同日共同股票池持平。",
                "intent": "comparison",
                "question": "1/3/5日权价比在不同持有期的净超额如何？",
                "rationale": "分组柱形图适合比较三个离散回看版本在四个固定持有期上的同单位收益。",
                "comparisonContext": {
                    "baseline": "同日共同股票池等权收益加20 bps成本",
                    "denominator": "每日生产门槛信号组合",
                    "grain": "回看周期 × 持有期",
                    "normalization": "日内股票等权、信号日等权",
                    "semanticFamily": "return",
                    "unit": "decimal return",
                },
                "type": "bar",
                "dataset": "signal_metrics",
                "sourceId": source["id"],
                "encodings": {
                    "x": {"field": "horizon_label", "type": "ordinal", "label": "持有期"},
                    "y": {
                        "field": "net_excess",
                        "type": "quantitative",
                        "format": "percent",
                        "label": "净超额",
                    },
                    "color": {
                        "field": "lookback_label",
                        "type": "nominal",
                        "label": "回看周期",
                    },
                    "tooltip": [
                        {"field": "signal_instances", "type": "quantitative", "label": "信号数"},
                        {"field": "signal_dates", "type": "quantitative", "label": "日期数"},
                        {
                            "field": "net_excess_win_rate",
                            "type": "quantitative",
                            "format": "percent",
                            "label": "超额胜率",
                        },
                    ],
                },
                "valueFormat": "percent",
                "layout": "full",
                "palette": {"kind": "categorical"},
                "legend": {"position": "bottom", "sort": "spec", "title": "权价比回看"},
                "labels": {"values": "auto"},
                "referenceLines": [
                    {"axis": "y", "value": 0, "label": "共同股票池", "color": "neutral", "lineStyle": "dashed"}
                ],
                "settings": {"groupMode": "grouped", "sort": "custom"},
            }
        ],
        "tables": [
            {
                "id": "table_summary",
                "title": "5日持有对比摘要",
                "subtitle": "生产门槛、连续因子和同日配对结果；配对差为该版本减5日版。",
                "dataset": "summary",
                "sourceId": source["id"],
                "defaultSort": {"field": "lookback_label", "direction": "asc"},
                "density": "dense",
                "layout": "full",
                "columns": [
                    {"field": "lookback_label", "label": "回看", "type": "text"},
                    {"field": "signal_instances_5d", "label": "信号数", "format": "number"},
                    {"field": "signal_dates_5d", "label": "日期数", "format": "number"},
                    {"field": "net_excess_5d", "label": "5日净超额", "format": "percent", "movement": True},
                    {"field": "win_rate_5d", "label": "超额胜率", "format": "percent"},
                    {"field": "net_excess_10d", "label": "10日净超额", "format": "percent", "movement": True},
                    {"field": "rank_ic_5d", "label": "5日Rank IC", "format": "number"},
                    {"field": "top_bottom_spread_5d", "label": "Top-Bottom", "format": "percent", "movement": True},
                    {"field": "paired_delta_vs_5d", "label": "同日配对差", "format": "percent", "movement": True},
                ],
            }
        ],
        "blocks": [
            {"id": "title", "type": "markdown", "layout": "full", "body": f"# {title}"},
            {
                "id": "executive_summary",
                "type": "markdown",
                "layout": "full",
                "sourceId": source["id"],
                "body": (
                    "## Executive Summary\n\n"
                    "**建议保留5日为线上默认，不切1日；3日仅做影子观察。** "
                    "1日版的5日净超额为-0.99%、10日为-3.41%，短期噪声最明显。"
                    "3日版与5日版在5日持有上分别为+0.54%和+0.45%，但相同日期配对后3日低0.06%（p=0.977），没有稳定优势。"
                    "5日版同时拥有更高的5日Rank IC（0.051）和为正的10日净超额（+0.32%）。"
                ),
            },
            {
                "id": "headline_metrics",
                "type": "metric-strip",
                "layout": "full",
                "cardIds": ["card_5d_return", "card_10d_return", "card_rank_ic"],
            },
            {
                "id": "key_findings",
                "type": "markdown",
                "layout": "full",
                "sourceId": source["id"],
                "body": (
                    "## Key Findings\n\n"
                    "1. **1日版明显退化。** 5日持有净超额-0.99%，10日-3.41%；同日配对的10日表现比5日版低4.58个百分点（p=0.011）。\n"
                    "2. **3日与5日在短窗口实质打平。** 3日的未配对5日均值略高，但共同日期配对差为-0.06%，说明优势来自覆盖日期差异而非稳定选股改善。\n"
                    "3. **5日的排序与延续性更稳。** 5日Rank IC为0.051，高于3日0.040和1日0.024；10日净超额也只有5日保持为正。\n"
                    f"4. **换周期会明显换股。** 3日与5日信号的日均Jaccard仅{overlap.iloc[2].mean_daily_jaccard:.1%}，不是轻微参数微调。"
                ),
            },
            {"id": "net_excess_chart_block", "type": "chart", "layout": "full", "chartId": "chart_net_excess"},
            {"id": "summary_table_block", "type": "table", "layout": "full", "tableId": "table_summary"},
            {
                "id": "recommendations",
                "type": "markdown",
                "layout": "full",
                "sourceId": source["id"],
                "body": (
                    "## Recommendations\n\n"
                    "- 线上继续使用5日权价比，不改现有生产字段和调仓逻辑。\n"
                    "- 后台并行记录3日版信号但不下单，固定现有门槛，累计至少3–6个月新样本后再复核。\n"
                    "- 不再推进1日版；除非未来引入盘中持仓快照和专门的微观结构过滤，否则其噪声与换股成本不匹配当前日频执行。"
                ),
            },
            {
                "id": "further_questions",
                "type": "markdown",
                "layout": "full",
                "body": (
                    "## Further Questions\n\n"
                    "- 3日版在新增样本中能否持续提高5日净超额，而不牺牲10日延续性？\n"
                    "- 完整复刻缓冲卖出、恐贪仓位、5日最短持有和17%止盈后，3日与5日的资金曲线是否仍接近？\n"
                    "- 3日版较低的信号重合度来自哪些板块、组合共识变化或个股价格状态？"
                ),
            },
            {
                "id": "caveats",
                "type": "markdown",
                "layout": "full",
                "sourceId": source["id"],
                "body": (
                    "## Caveats\n\n"
                    "有效共同样本只有38个快照日、3,474个股票日；5日远期比较仅31–33个信号日。除1日与5日版的10日配对差外，主差异均未达到常用统计显著性门槛。"
                    "多日远期收益互相重叠，普通t检验只作方向参考。2026-08-05缺少持仓快照；回看周期按有效快照日计。"
                    "本报告比较信号与因子，不是对完整调仓状态机的逐笔资金曲线复刻。"
                ),
            },
        ],
    }

    artifact = {
        "surface": "report",
        "manifest": manifest,
        "snapshot": {
            "version": 1,
            "generatedAt": generated_at,
            "status": "ready",
            "datasets": {
                "headline": headline,
                "signal_metrics": _records(signal_chart),
                "summary": summary_rows,
            },
            "accessIssues": [],
        },
        "sources": [source],
    }
    (OUTPUT / "artifact.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(OUTPUT / "artifact.json")


if __name__ == "__main__":
    main()

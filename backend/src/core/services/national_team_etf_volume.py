"""国家队宽基 ETF 成交量观测的标的池。

仅保留精确跟踪目标指数、可场内交易的被动 ETF，且按最近 5 个交易日的
``基金份额 × 单位净值``平均资产净值超过 100 亿元筛选。筛选快照为
2026-10-19 至 2026-10-23；红利组覆盖上证红利和中证红利；不把份额或净值
缺失误判为规模不足。
"""
from __future__ import annotations

from typing import Tuple


# 数据口径：2026-10-19~2026-10-23 五个交易日，Tushare fund_share × fund_nav.unit_nav。
# 净值按单基金接口逐只读取，避免按日期全量接口的分页截断造成标的漏筛。
# 仅作为成交量分组的已核定名单；新增或规模变化产品须先重新跑同口径筛选，再调整此名单。
NATIONAL_TEAM_ETF_GROUPS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("上证50ETF", ("510050.SH",)),
    ("沪深300ETF", ("510300.SH", "510310.SH", "510330.SH", "159919.SZ")),
    ("科创50ETF", ("588000.SH", "588080.SH")),
    ("创业板ETF", ("159915.SZ", "159952.SZ")),
    ("红利ETF", ("510880.SH", "515080.SH", "515180.SH")),
)


def get_national_team_etf_groups() -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    """返回经 5 日平均资产净值门槛筛出的五个指数分组。"""
    return NATIONAL_TEAM_ETF_GROUPS


def get_national_team_etf_symbols() -> Tuple[str, ...]:
    return tuple(dict.fromkeys(
        symbol
        for _, symbols in get_national_team_etf_groups()
        for symbol in symbols
    ))

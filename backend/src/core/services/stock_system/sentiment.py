"""选股系统第二层：情绪面择时——市场和板块现在处在什么状态。

信号源就是「自算贪恐历史曲线」上画的顶/底标记：``ETFFearGreedCloneCalculator
.load_history_from_db`` 对全量历史跑 ``compute_turn_signals``（均线型 + 量能型，阈值走
``fear_greed_signal_configs``），逐日给出 ``signals``。这里不另算一套信号，页面上看到的
标记和择时用的是同一份。

状态机沿用贪恐×九转成分股研究的"联合模式"，也和 AI 荐股按中证全指定持仓数的判定
（``ai_stock._compute_csi_all_share_top_bottom``）同一口径——沿历史从后往前找最后一个
顶/底标记：

- 最后一个是**底** → 进攻：允许开新仓，板块仓位上限放宽；
- 最后一个是**顶** → 防守：不开新仓，已有持仓交给技术层收紧止损（研究显示顶信号
  直接清仓在强趋势里离场过早，所以这里不强制卖出）；
- 从没出现过信号、信号超过有效期、贪恐数据过期 → 中性。

另有一个"过热"开关：贪恐分数本身已经很高时，即使最后一个信号是底也不再追新仓。

信号只依赖当天及以前的数据（MA5、量能 z 值、冷却都是因果的），所以历史回放时把
``end_date`` 截到 as_of 就是 point-in-time 的。
"""

from __future__ import annotations

import logging
import math
from datetime import date
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from ....robot.a_stock_base_data_config import A_STOCK_INDEX_FEAR_GREED_TARGETS
from .config import BROAD_INDEX_CODES

logger = logging.getLogger(__name__)

STATE_OFFENSE = "offense"
STATE_NEUTRAL = "neutral"
STATE_DEFENSE = "defense"
STATE_LABELS = {STATE_OFFENSE: "进攻", STATE_NEUTRAL: "中性", STATE_DEFENSE: "防守"}
# 贪恐数据停更超过这么多自然日，状态不再可信
STALE_SCORE_DAYS = 7

HistoryLoader = Callable[[str, date], List[Dict[str, Any]]]


def target_catalog() -> List[Dict[str, Any]]:
    """有贪恐计算的 A 股指数目录；``order`` 越大越靠后（配置里按宽基 → 一级 → 二级 → 三级排列）。"""
    catalog: List[Dict[str, Any]] = []
    seen = set()
    for order, target in enumerate(A_STOCK_INDEX_FEAR_GREED_TARGETS):
        symbol = str(target.get("symbol") or "").upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        catalog.append({
            "symbol": symbol,
            "name": str(target.get("ticker") or target.get("label") or symbol),
            "label": str(target.get("label") or target.get("ticker") or symbol),
            "category": "broad" if symbol in BROAD_INDEX_CODES else "sector",
            "order": order,
        })
    return catalog


class CalculatorHistoryLoader:
    """用贪恐历史曲线同一个入口读历史（含逐日顶/底标记），一次任务里复用同一个计算器。"""

    def __init__(self):
        self._calculator = None

    def __call__(self, symbol: str, as_of: date) -> List[Dict[str, Any]]:
        if self._calculator is None:
            from ..etf_fear_greed_clone_service import ETFFearGreedCloneCalculator

            self._calculator = ETFFearGreedCloneCalculator()
        history = self._calculator.load_history_from_db(
            symbol=symbol,
            end_date=as_of,
            include_components=False,
            include_latest_holdings=False,
        )
        return history.get("data") or []


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def latest_turn_signal(rows: Sequence[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """沿历史从后往前找最后一个顶/底标记（同日既有顶又有底时以最后触发的为准）。"""
    for index in range(len(rows) - 1, -1, -1):
        marks = rows[index].get("signals") or []
        kinds = [str(mark.get("kind") or "") for mark in marks]
        has_bottom = any(kind.endswith("_bottom") for kind in kinds)
        has_top = any(kind.endswith("_top") for kind in kinds)
        if not (has_bottom or has_top):
            continue
        if has_bottom and not has_top:
            side = "bottom"
        elif has_top and not has_bottom:
            side = "top"
        else:
            side = "bottom" if kinds[-1].endswith("_bottom") else "top"
        return {
            "side": side,
            "kind": kinds[-1],
            "label": "、".join(str(mark.get("label") or mark.get("kind")) for mark in marks),
            "date": rows[index]["date"],
            "score": float(rows[index]["score"]) if _finite(rows[index].get("score")) else None,
            "index": index,
        }
    return None


_NO_DATA_REGIME = {
    "state": STATE_NEUTRAL, "score": None, "score_date": None, "signal": None,
    "days_since_signal": None, "overheated": False, "stale": True, "note": "没有贪恐数据，按中性处理",
}


def classify_regime(rows: Sequence[Mapping[str, Any]], as_of: date, timing: Mapping[str, Any]) -> Dict[str, Any]:
    """由截至 as_of 的贪恐历史给出状态、最近信号、是否过热。"""
    as_of_iso = as_of.isoformat()
    history = [row for row in rows if row.get("date") and str(row["date"]) <= as_of_iso]
    scored = [row for row in history if _finite(row.get("score"))]
    if not scored:
        return dict(_NO_DATA_REGIME)
    signal = latest_turn_signal(history)
    days_since = len(history) - 1 - signal["index"] if signal else None
    return _decide_regime(scored[-1], signal, days_since, as_of, timing)


def regime_series(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """把一条贪恐历史预处理成逐行的（最近有分数的行、最近信号）序列，回测按日查询只需二分。

    ``regime_at(series, day, timing)`` 与 ``classify_regime(rows, day, timing)`` 结果一致，
    只是不必每天把整段历史重扫一遍。
    """
    history = sorted((row for row in rows if row.get("date")), key=lambda row: str(row["date"]))
    dates: List[str] = []
    latest_scored: List[Optional[Mapping[str, Any]]] = []
    signals: List[Optional[Dict[str, Any]]] = []
    current_scored: Optional[Mapping[str, Any]] = None
    current_signal: Optional[Dict[str, Any]] = None
    for index, row in enumerate(history):
        if _finite(row.get("score")):
            current_scored = row
        found = latest_turn_signal([row])
        if found is not None:
            current_signal = {**found, "index": index}
        dates.append(str(row["date"]))
        latest_scored.append(current_scored)
        signals.append(current_signal)
    return {"dates": dates, "latest_scored": latest_scored, "signals": signals}


def regime_at(series: Mapping[str, Any], as_of: date, timing: Mapping[str, Any]) -> Dict[str, Any]:
    from bisect import bisect_right

    position = bisect_right(series["dates"], as_of.isoformat()) - 1
    if position < 0 or series["latest_scored"][position] is None:
        return dict(_NO_DATA_REGIME)
    signal = series["signals"][position]
    days_since = position - signal["index"] if signal else None
    return _decide_regime(series["latest_scored"][position], signal, days_since, as_of, timing)


def _decide_regime(
    latest: Mapping[str, Any],
    signal: Optional[Mapping[str, Any]],
    days_since: Optional[int],
    as_of: date,
    timing: Mapping[str, Any],
) -> Dict[str, Any]:
    score = float(latest["score"])
    score_date = date.fromisoformat(str(latest["date"]))
    stale = (as_of - score_date).days > STALE_SCORE_DAYS
    expiry = int(timing.get("signal_expiry_days") or 0)

    note = None
    if stale:
        state = STATE_NEUTRAL
        note = f"贪恐数据停在 {score_date}，按中性处理"
    elif signal is None:
        state = STATE_NEUTRAL
        note = "历史上还没有出现过顶/底信号"
    elif expiry > 0 and days_since > expiry:
        state = STATE_NEUTRAL
        note = f"最近的{signal['label']}已过去 {days_since} 个交易日，超过 {expiry} 日有效期"
    else:
        state = STATE_OFFENSE if signal["side"] == "bottom" else STATE_DEFENSE

    overheated = (
        not stale
        and bool(timing.get("overheat_enabled"))
        and score >= float(timing.get("overheat_score") or 100)
    )
    if signal is not None:
        signal = {key: value for key, value in signal.items() if key != "index"}
    return {
        "state": state,
        "score": score,
        "score_date": score_date.isoformat(),
        "signal": signal,
        "days_since_signal": days_since,
        "overheated": overheated,
        "stale": stale,
        "note": note,
    }


def entry_allowed(regime: Mapping[str, Any]) -> bool:
    """过热时一律不追新仓。

    防守不在这里一刀切：它按防守档的板块仓位上限处理，上限为 0（默认）就是不开新仓，
    调大则允许小仓位。生产数据回测里过热之后 20/60 日的超额收益明显为负（−1.3%/−4.5%），
    而防守与进攻的差距很小、逐年方向不稳，所以两者力度不同。
    """
    return not regime.get("overheated")


def cap_state(regime: Mapping[str, Any]) -> str:
    """决定仓位上限用哪一档：过热按防守档处理。"""
    return STATE_DEFENSE if regime.get("overheated") else regime.get("state", STATE_NEUTRAL)


def load_regimes(
    symbols: Sequence[str],
    as_of: date,
    timing: Mapping[str, Any],
    history_loader: Optional[HistoryLoader] = None,
) -> Dict[str, Dict[str, Any]]:
    loader = history_loader or CalculatorHistoryLoader()
    regimes: Dict[str, Dict[str, Any]] = {}
    for symbol in symbols:
        try:
            rows = loader(symbol, as_of)
        except Exception as exc:  # noqa: BLE001 单个指数读失败不影响其它板块
            logger.warning("stock system fear-greed history unavailable for %s: %s", symbol, exc)
            rows = []
        regimes[symbol] = classify_regime(rows, as_of, timing)
    return regimes

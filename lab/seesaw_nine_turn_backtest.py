#!/usr/bin/env python3
"""红利三标的轮动 + 第四标的（创新药）+ 神奇九转确认的对照回测。

基线 = /fear-volume-backtest 页面上的三标的对称轮动（swap_threshold=45）：
红利 510880（上证红利贪恐）/ 半导体 512480（科创50贪恐, 量比 588000）/ 纳指科技 159509（QQQ 贪恐与量比）。

本脚本把 ``_run_seesaw_backtest`` 的对称轮动状态机推广到 N 个标的（逐笔与生产函数对账，见 --verify），
并加一层可选的神奇九转确认：

- 九转确认买：现有逻辑出买入信号后（含信号当天），等该标的「量比来源」价格出现高 2（highCount == 2）
  的那天收盘再下单，次日开盘成交。等待期间若该标的先出了卖出信号（贪恐+估值闸门），取消这次买入；
  若又有其他标的出买入信号，改为等最恐慌的那只。
- 卖出确认（``sell_confirm``）：现有逻辑出卖出信号（贪婪 + 估值闸门）后，可以再等一个条件才真正卖出——
  ``nine_low2`` 等量比来源出现神奇九转低 2；``ma5_self`` 等持仓标的自己收盘跌破 5 日均线；
  ``ma5_signal`` 等量比来源收盘跌破 5 日均线。卖出信号一旦出现就一直挂着，直到确认条件满足。
- 换仓（持有 X 恐贪>45 且 Y 出买入信号）：买腿是主动方，等 Y 的高 2 出现再同时卖 X 买 Y。

九转计数调用生产同一套 ``append_nine_turn_atr``（与个股详情页 K 线图同口径）。跨市场来源（QQQ.US）
按日期对齐并前向填充，与量比列的对齐方式一致：A股 T 日收盘决策用美股 T 日收盘数据，T+1 开盘成交。

运行（生产库只读；导入时的建表/迁移写入会被跳过）::

    QUANT_SQLITE_PATH=/home/quantd/quant_prod/quant_robot/evc_stocks.db \
    ANALYTICS_DB_PATH=/home/quantd/quant_prod/quant_robot/analytics.duckdb \
    .venv/bin/python lab/seesaw_nine_turn_backtest.py
"""

from __future__ import annotations

import argparse
import os
import pickle
import sys
import time
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_read_only_guards() -> None:
    """后端模块导入时会建表/迁移；研究脚本只读生产库，把非查询语句和 DuckDB 写连接都挡掉。"""
    import duckdb
    import sqlalchemy.sql.schema as schema
    from sqlalchemy.engine import default as sa_default

    schema.MetaData.create_all = lambda self, *args, **kwargs: None

    def _is_read(statement: str) -> bool:
        head = statement.lstrip().split(None, 1)
        return bool(head) and head[0].upper() in {"SELECT", "PRAGMA", "WITH"}

    original_execute = sa_default.DefaultDialect.do_execute
    original_execute_no_params = sa_default.DefaultDialect.do_execute_no_params

    def do_execute(self, cursor, statement, parameters, context=None):
        if not _is_read(statement):
            return None
        return original_execute(self, cursor, statement, parameters, context)

    def do_execute_no_params(self, cursor, statement, context=None):
        if not _is_read(statement):
            return None
        return original_execute_no_params(self, cursor, statement, context)

    sa_default.DefaultDialect.do_execute = do_execute
    sa_default.DefaultDialect.do_execute_no_params = do_execute_no_params

    original_connect = duckdb.connect

    def connect(database=":memory:", read_only=False, **kwargs):
        if database and database != ":memory:":
            read_only = True
        return original_connect(database=database, read_only=read_only, **kwargs)

    duckdb.connect = connect


if os.getenv("SEESAW_SKIP_READONLY_GUARD") != "1":  # 离线自测用空库时需要正常建表
    _install_read_only_guards()

from backend.src.app.api import soxl_fear_backtest as fb  # noqa: E402
from backend.src.core.services.stock_system.indicators import append_nine_turn_atr  # noqa: E402

START = date(2023, 3, 22)
END = date(2026, 9, 15)
INITIAL_CAPITAL = 1_000_000.0
NINE_TURN_LOOKBACK_DAYS = 120
MA5_WINDOW = 5
SELL_CONFIRM_NONE = "none"                 # 现有逻辑：贪婪+估值闸门当天就卖
SELL_CONFIRM_NINE_LOW2 = "nine_low2"       # 等量比来源出现神奇九转低 2
SELL_CONFIRM_MA5_SELF = "ma5_self"         # 等持仓标的自己收盘跌破 MA5
SELL_CONFIRM_MA5_SIGNAL = "ma5_signal"     # 等量比来源收盘跌破 MA5
SELL_CONFIRM_MODES = (SELL_CONFIRM_NONE, SELL_CONFIRM_NINE_LOW2, SELL_CONFIRM_MA5_SELF, SELL_CONFIRM_MA5_SIGNAL)
SELL_CONFIRM_LABELS = {
    SELL_CONFIRM_NONE: "",
    SELL_CONFIRM_NINE_LOW2: " +九转卖(低2)",
    SELL_CONFIRM_MA5_SELF: " +跌破MA5卖(持仓价)",
    SELL_CONFIRM_MA5_SIGNAL: " +跌破MA5卖(量比来源价)",
}
CACHE_DIR = Path(os.getenv("SEESAW_NINE_TURN_CACHE") or "/tmp/seesaw_nine_turn_cache")


@dataclass(frozen=True)
class Target:
    key: str
    symbol: str
    fear_source: str
    volume_signal_symbol: Optional[str]
    buy_threshold: float
    volume_ratio_threshold: float


BASE_TARGETS = [
    Target("main", "510880.SH", "a_stock_000015_sh", None, 35.0, 1.6),
    Target("sub", "512480.SH", "a_stock_000688_sh", "588000.SH", 25.0, 1.6),
    Target("sub2", "159509.SZ", "qqq_clone", "QQQ.US", 20.0, 1.3),
]
# 创新药：中证创新药产业指数贪恐 → 创新药ETF 515120；阈值另扫一遍
INNOVATION_DRUG = Target("drug", "515120.SH", "a_stock_931152_csi", None, 25.0, 1.6)

BASE_PARAMS = dict(
    buy_threshold=35, greed_threshold=70, volume_ratio_threshold=1.6, volume_ratio_consecutive_days=1,
    volume_z_threshold=None, sell_shrink_z=-1, buy_position_pct=100, cooldown_days=0, trailing_stop_pct=0,
    sell_position_pct=100, sell_reduction_basis="holdings", sell_price_above_avg_cost=False,
    max_take_profit_sells_per_cycle=2, min_position_pct_after_take_profit=0, rebalance_threshold_pct=0,
    execute_next_open=True, slippage_pct=-1, stamp_duty_pct=0,
    sub_symbol="512480.SH", sub_fear_source="a_stock_000688_sh", sub_volume_signal_symbol="588000.SH",
    sub_buy_threshold=25, sub_volume_ratio_threshold=1.6,
    sub2_symbol="159509.SZ", sub2_fear_source="qqq_clone", sub2_volume_signal_symbol="QQQ.US",
    sub2_buy_threshold=20, sub2_volume_ratio_threshold=1.3,
    swap_threshold=45, valuation_window=252, valuation_buy_max=None,
    valuation_sell_min=80, valuation_force_sell_greed=90,
)


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------

def _signal_source_frame(symbol: str, start: date, end: date) -> pd.DataFrame:
    """量比来源标的的九转计数与「收盘跌破 MA5」标记。"""
    history = fb._fetch_signal_price_history(symbol, start - timedelta(days=NINE_TURN_LOOKBACK_DAYS), end)
    history = history.sort_values("date").reset_index(drop=True)
    rows = append_nine_turn_atr(history[["date", "open", "high", "low", "close"]].to_dict("records"))
    close = pd.to_numeric(history["close"], errors="coerce")
    ma5 = close.rolling(MA5_WINDOW, min_periods=MA5_WINDOW).mean()
    return pd.DataFrame({
        "date": history["date"],
        "nt_high": [int(row["highCount"]) for row in rows],
        "nt_low": [int(row["lowCount"]) for row in rows],
        "below_ma5": (close < ma5).where(ma5.notna(), False).astype(bool),
    })


def load_target_frame(target: Target, start: date = START, end: date = END, use_cache: bool = True) -> pd.DataFrame:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"v2_{target.symbol}_{target.fear_source}_{target.volume_signal_symbol}_{start}_{end}.pkl"
    if use_cache and cache.exists():
        return pickle.loads(cache.read_bytes())
    frame, _ = fb._prepare_base_dataframe(target.symbol, start, end, target.fear_source, target.volume_signal_symbol)
    signal_symbol = target.volume_signal_symbol or target.symbol
    signal_frame = _signal_source_frame(signal_symbol, start, end).rename(columns={"below_ma5": "below_ma5_signal"})
    attrs = dict(frame.attrs)
    merged = frame.merge(signal_frame, on="date", how="left")
    # 跨市场来源休市日沿用最近一根（与量比列 ffill 同口径）
    merged[["nt_high", "nt_low"]] = merged[["nt_high", "nt_low"]].ffill().fillna(0).astype(int)
    merged["below_ma5_signal"] = merged["below_ma5_signal"].ffill().fillna(False).astype(bool)
    # 持仓标的自己的收盘 vs MA5（「股价跌破 5 日均线」的默认口径），MA5 带上热身区间
    if signal_symbol == target.symbol:
        merged["below_ma5_self"] = merged["below_ma5_signal"]
    else:
        self_frame = _signal_source_frame(target.symbol, start, end)[["date", "below_ma5"]]
        merged = merged.merge(self_frame.rename(columns={"below_ma5": "below_ma5_self"}), on="date", how="left")
        merged["below_ma5_self"] = merged["below_ma5_self"].ffill().fillna(False).astype(bool)
    merged.attrs.update(attrs)
    cache.write_bytes(pickle.dumps(merged))
    return merged


def _sell_confirmed(mode: str, held_sig: Dict, sell_low_count: int) -> bool:
    """卖出信号已出，本交易日是否满足确认条件。"""
    if mode == SELL_CONFIRM_NINE_LOW2:
        return held_sig["low"] == sell_low_count
    if mode == SELL_CONFIRM_MA5_SELF:
        return held_sig["below_ma5_self"]
    if mode == SELL_CONFIRM_MA5_SIGNAL:
        return held_sig["below_ma5_signal"]
    return True


# 空仓补仓池：科创50/100/200 + 上证红利 + 行业板块 ETF（剔除宽基）
SLEEVE_EXCLUDED_INDEXES = {
    "000300.SH", "000016.SH", "000510.SH", "000905.SH", "000852.SH", "932000.CSI",
    "000985.SH", "899050.BJ", "000680.SH", "399006.SZ", "H30269.CSI",
}


def sleeve_pool_targets() -> List[Target]:
    from backend.src.robot.a_stock_base_data_config import A_STOCK_INDEX_FEAR_GREED_TARGETS

    pool = []
    for item in A_STOCK_INDEX_FEAR_GREED_TARGETS:
        symbol = fb._normalize_symbol(str(item["symbol"]))
        etf = item.get("proxy_etf")
        if not etf or symbol in SLEEVE_EXCLUDED_INDEXES:
            continue
        pool.append(Target(f"sleeve_{fb._normalize_symbol(etf)}", fb._normalize_symbol(etf),
                           fb._fear_source_key_for_symbol(symbol), None, 0.0, 0.0))
    return pool


def pool_rotation_targets(buy_threshold: float, vr_threshold: float, first_symbol: str = "510880.SH",
                          available: Optional[Dict[str, pd.DataFrame]] = None) -> List[Target]:
    """把补仓池做成 N 标的轮动的候选：每个 ETF 用自己板块指数的贪恐，量比用自己。"""
    targets = [replace(t, buy_threshold=buy_threshold, volume_ratio_threshold=vr_threshold)
               for t in sleeve_pool_targets() if available is None or t.key in available]
    first = [t for t in targets if t.symbol == fb._normalize_symbol(first_symbol)]
    rest = [t for t in targets if t.symbol != fb._normalize_symbol(first_symbol)]
    return [*first, *rest]  # 第一个标的决定基准曲线与交易日历


def load_sleeve_frames(use_cache: bool = True) -> Dict[str, pd.DataFrame]:
    frames: Dict[str, pd.DataFrame] = {}
    skipped: List[str] = []
    for target in sleeve_pool_targets():
        if target.key in frames:
            continue
        for attempt in range(6):
            try:
                frames[target.key] = load_target_frame(target, use_cache=use_cache)
                break
            except Exception as exc:  # 分析库正在同步写入时重试；次新 ETF / 缺数据的来源跳过
                if "不可读" in str(exc) and attempt < 5:
                    time.sleep(10)
                    continue
                skipped.append(f"{target.symbol}({str(exc)[:40]})")
                break
    if skipped:
        print(f"补仓池跳过 {len(skipped)} 个：{'; '.join(skipped[:6])}{' …' if len(skipped) > 6 else ''}")
    print(f"补仓池可用 {len(frames)} 个 ETF")
    return frames


# ---------------------------------------------------------------------------
# 回测：_run_seesaw_backtest 对称轮动的 N 标的推广 + 九转确认
# ---------------------------------------------------------------------------

def run_rotation(frames: Dict[str, pd.DataFrame], targets: List[Target], params: "fb.SOXLFearStrategyParams",
                 nine_buy: bool = False, sell_confirm=SELL_CONFIRM_NONE, buy_high_count: int = 2,
                 sell_low_count: int = 2, initial_capital: float = INITIAL_CAPITAL,
                 sleeve_frames: Optional[Dict[str, pd.DataFrame]] = None, sleeve_pct: float = 0.0,
                 sleeve_switch_margin: float = 0.0, sleeve_fill: str = "pessimistic",
                 sleeve_entry_fear: Optional[float] = None, sleeve_exit_greed: Optional[float] = None,
                 sleeve_min_vr: Optional[float] = None, target_fill: str = "pessimistic") -> Dict:
    assert params.execute_next_open and params.swap_threshold is not None
    assert float(params.trailing_stop_pct) <= 0 and float(params.sell_position_pct) >= 100
    assert int(params.cooldown_days) == 0 and params.volume_z_threshold is None
    assert params.buy_turn_signal_mode == "legacy" and params.sell_turn_signal_mode == "legacy"
    # sell_confirm 可以是统一模式，也可以是 {标的key: 模式}（例如只对高弹性标的等确认）
    confirm_by_key = sell_confirm if isinstance(sell_confirm, dict) else {t.key: sell_confirm for t in targets}
    assert all(mode in SELL_CONFIRM_MODES for mode in confirm_by_key.values())
    vr_column = fb._volume_ratio_consecutive_column(int(params.volume_ratio_consecutive_days))
    valuation_column = fb._valuation_column(int(params.valuation_window))

    def shifted(values: np.ndarray, fill) -> np.ndarray:
        return np.concatenate([[fill], values[:-1]])

    data: Dict[str, Dict] = {}
    sleeve_keys: List[str] = []
    sleeve_symbols: Dict[str, str] = {}
    for target in targets:
        df = frames[target.key]
        texts = [d.isoformat() for d in df["date"]]
        col = lambda name: df[name].to_numpy(dtype=float) if name in df.columns else np.full(len(df), np.nan)  # noqa: E731
        vr = df[vr_column].to_numpy(dtype=float) if vr_column in df.columns else col("volume_ratio")
        arrays = {
            "open": col("open"), "high": col("high"), "low": col("low"), "close": col("close"),
            "fear": shifted(col("fear_greed"), np.nan), "vr": shifted(vr, np.nan),
            "valuation": shifted(col(valuation_column), np.nan),
            "nt_high": shifted(df["nt_high"].to_numpy(dtype=float), 0.0) if "nt_high" in df.columns else np.zeros(len(df)),
            "nt_low": shifted(df["nt_low"].to_numpy(dtype=float), 0.0) if "nt_low" in df.columns else np.zeros(len(df)),
            "below_ma5_self": shifted(col("below_ma5_self"), 0.0),
            "below_ma5_signal": shifted(col("below_ma5_signal"), 0.0),
        }
        data[target.key] = {"index": {t: i for i, t in enumerate(texts)}, **arrays}

    # 空仓补仓池：只需要恐贪分数和价格
    for key, df in (sleeve_frames or {}).items():
        if key in data:
            continue
        sleeve_keys.append(key)
        sleeve_symbols[key] = str(df.attrs.get("symbol") or key)
        texts_s = [d.isoformat() for d in df["date"]]
        data[key] = {
            "index": {t: i for i, t in enumerate(texts_s)},
            "high": df["high"].to_numpy(dtype=float), "low": df["low"].to_numpy(dtype=float),
            "close": df["close"].to_numpy(dtype=float), "open": df["open"].to_numpy(dtype=float),
            "fear": shifted(df["fear_greed"].to_numpy(dtype=float), np.nan),
            "vr": shifted((df[vr_column] if vr_column in df.columns else df["volume_ratio"]).to_numpy(dtype=float), np.nan),
        }

    main_key = targets[0].key
    main_df = frames[main_key]
    dates = list(main_df["date"])
    texts = [d.isoformat() for d in dates]
    by_key = {t.key: t for t in targets}

    def info(key: str, day: str, field: str) -> float:
        idx = data[key]["index"].get(day)
        return float(data[key][field][idx]) if idx is not None else np.nan

    cash = float(initial_capital)
    sleeve_key: Optional[str] = None
    sleeve_shares = 0
    sleeve_cost = 0.0
    sleeve_trades: List[Dict] = []
    sleeve_days = 0
    held: Optional[str] = None
    shares = 0
    avg_cost = 0.0
    idle_days = 0
    pending_buy: Optional[str] = None
    pending_sell = False
    pending_swap: Optional[str] = None
    trades: List[Dict] = []
    equity = np.empty(len(texts))
    benchmark = np.empty(len(texts))
    first_exec = float(main_df["open"].iloc[0])
    bench_shares = fb._floor_share_count(initial_capital / first_exec)
    bench_cash = initial_capital - bench_shares * first_exec

    def sleeve_value(day: str) -> float:
        if sleeve_key is None or sleeve_shares <= 0:
            return 0.0
        price = info(sleeve_key, day, "close")
        return sleeve_shares * price if np.isfinite(price) else 0.0

    def sleeve_exit(day: str, reason: str) -> bool:
        """补仓卖出：与主仓同口径，最悲观按当日最低价。"""
        nonlocal cash, sleeve_key, sleeve_shares, sleeve_cost
        if sleeve_key is None or sleeve_shares <= 0:
            return False
        price = info(sleeve_key, day, "open" if sleeve_fill == "open" else "low")
        if not (price > 0):
            return False
        proceeds = sleeve_shares * price
        sleeve_trades.append({"date": day, "action": "SELL", "symbol": sleeve_symbols[sleeve_key], "price": price,
                              "profit": proceeds - sleeve_cost, "reason": reason})
        cash += proceeds
        sleeve_key, sleeve_shares, sleeve_cost = None, 0, 0.0
        return True

    def sleeve_enter(day: str, key: str, portfolio_value: float, reason: str) -> bool:
        nonlocal cash, sleeve_key, sleeve_shares, sleeve_cost
        price = info(key, day, "open" if sleeve_fill == "open" else "high")
        if not (price > 0):
            return False
        qty = fb._floor_share_count(min(cash, portfolio_value * sleeve_pct / 100.0) / price)
        if qty < 1:
            return False
        cash -= qty * price
        sleeve_key, sleeve_shares, sleeve_cost = key, qty, qty * price
        sleeve_trades.append({"date": day, "action": "BUY", "symbol": sleeve_symbols[key], "price": price,
                              "reason": reason})
        return True

    def sell(day: str, reason: str):
        nonlocal cash, shares, avg_cost, held
        price = info(held, day, "open" if target_fill == "open" else "low")
        if not (price > 0) or shares <= 0:
            return False
        proceeds = shares * price
        trades.append({"date": day, "action": "SELL", "symbol": by_key[held].symbol, "price": price,
                       "profit": proceeds - shares * avg_cost, "reason": reason})
        cash += proceeds
        shares, avg_cost, held = 0, 0.0, None
        return True

    def buy(day: str, key: str, reason: str):
        nonlocal cash, shares, avg_cost, held
        # 主仓满仓买入前先把补仓仓位腾出来
        sleeve_exit(day, f"主仓买入 {by_key[key].symbol}，退出补仓")
        price = info(key, day, "open" if target_fill == "open" else "high")
        if not (price > 0):
            return False
        qty = fb._floor_share_count(min(cash, cash * float(params.buy_position_pct) / 100.0) / price)
        if qty < 1:
            return False
        cash -= qty * price
        shares, avg_cost, held = qty, price, key
        trades.append({"date": day, "action": "BUY", "symbol": by_key[key].symbol, "price": price, "reason": reason})
        return True

    swap_value = float(params.swap_threshold)
    for index, day in enumerate(texts):
        if held is None:
            idle_days += 1
        if index > 0:
            sig = {}
            for target in targets:
                fear = info(target.key, day, "fear")
                vr = info(target.key, day, "vr")
                valuation = info(target.key, day, "valuation")
                signal = (np.isfinite(fear) and fear <= target.buy_threshold
                          and np.isfinite(vr) and vr >= target.volume_ratio_threshold
                          and fb.valuation_buy_allowed(valuation, params.valuation_buy_max))
                greedy = (np.isfinite(fear) and fear >= float(params.greed_threshold)
                          and fb.valuation_sell_allowed(valuation, fear, params.valuation_sell_min,
                                                        params.valuation_force_sell_greed))
                sig[target.key] = {"fear": fear, "signal": bool(signal), "greedy": bool(greedy),
                                   "high": info(target.key, day, "nt_high"), "low": info(target.key, day, "nt_low"),
                                   "below_ma5_self": bool(info(target.key, day, "below_ma5_self")),
                                   "below_ma5_signal": bool(info(target.key, day, "below_ma5_signal"))}
            buyers = [k for k, s in sig.items() if s["signal"] and np.isfinite(s["fear"])]

            if held is None:
                if buyers:
                    pending_buy = min(buyers, key=lambda k: sig[k]["fear"])
                elif pending_buy is not None and sig[pending_buy]["greedy"]:
                    pending_buy = None
                if pending_buy is not None and (not nine_buy or sig[pending_buy]["high"] == buy_high_count):
                    target_key = pending_buy
                    pending_buy = None
                    buy(day, target_key, f"买入 {by_key[target_key].symbol} 恐贪 {sig[target_key]['fear']:.1f}")
            elif shares > 0:
                held_sig = sig[held]
                if held_sig["greedy"]:
                    pending_sell = True
                others = [k for k in buyers if k != held]
                if not pending_sell and np.isfinite(held_sig["fear"]) and held_sig["fear"] > swap_value and others:
                    pending_swap = min(others, key=lambda k: sig[k]["fear"])
                if pending_swap is not None and (sig[pending_swap]["greedy"] or pending_swap == held):
                    pending_swap = None
                executed = False
                if pending_sell and _sell_confirmed(confirm_by_key.get(held, SELL_CONFIRM_NONE), held_sig, sell_low_count):
                    executed = sell(day, f"卖出 {by_key[held].symbol} 恐贪 {held_sig['fear']:.1f}")
                elif pending_swap is not None and (not nine_buy or sig[pending_swap]["high"] == buy_high_count):
                    target_key = pending_swap
                    if sell(day, f"换仓卖出 {by_key[held].symbol} → {by_key[target_key].symbol}"):
                        buy(day, target_key, f"换仓买入 {by_key[target_key].symbol} 恐贪 {sig[target_key]['fear']:.1f}")
                        executed = True
                if executed:
                    pending_sell, pending_swap, pending_buy = False, None, None
        if sleeve_pct > 0 and index > 0:
            if held is not None:
                sleeve_exit(day, "主仓有持仓，退出补仓")
            else:
                if sleeve_key is not None and sleeve_exit_greed is not None:
                    current_fear = info(sleeve_key, day, "fear")
                    if np.isfinite(current_fear) and current_fear >= sleeve_exit_greed:
                        sleeve_exit(day, f"补仓 {sleeve_symbols[sleeve_key]} 恐贪 {current_fear:.1f} 转贪婪，退出")
                available = [(info(k, day, "fear"), k) for k in sleeve_keys]
                available = [(f, k) for f, k in available if np.isfinite(f) and np.isfinite(info(k, day, "high"))]
                if sleeve_entry_fear is not None:
                    available = [(f, k) for f, k in available if f <= sleeve_entry_fear]
                if sleeve_min_vr is not None:
                    available = [(f, k) for f, k in available
                                 if np.isfinite(info(k, day, "vr")) and info(k, day, "vr") >= sleeve_min_vr]
                if available:
                    best_fear, best_key = min(available)
                    if sleeve_key is None:
                        portfolio = cash + shares * (info(held, day, "close") if held else 0.0)
                        sleeve_enter(day, best_key, portfolio,
                                     f"空仓补仓 {sleeve_symbols[best_key]} 恐贪 {best_fear:.1f}")
                    elif best_key != sleeve_key:
                        current_fear = info(sleeve_key, day, "fear")
                        if not np.isfinite(current_fear) or current_fear - best_fear >= sleeve_switch_margin:
                            portfolio = cash + sleeve_value(day)
                            if sleeve_exit(day, f"换到更恐慌的 {sleeve_symbols[best_key]}"):
                                sleeve_enter(day, best_key, portfolio,
                                             f"补仓换入 {sleeve_symbols[best_key]} 恐贪 {best_fear:.1f}")
        if sleeve_key is not None:
            sleeve_days += 1
        close = info(held, day, "close") if held else 0.0
        equity[index] = cash + shares * close + sleeve_value(day)
        benchmark[index] = bench_cash + bench_shares * float(main_df["close"].iloc[index])

    metrics, _ = fb._compute_equity_metrics(dates, equity)
    bench_metrics, _ = fb._compute_equity_metrics(dates, benchmark)
    return {
        **metrics,
        "benchmark_metrics": bench_metrics,
        "buy_count": sum(1 for t in trades if t["action"] == "BUY"),
        "sell_count": sum(1 for t in trades if t["action"] == "SELL"),
        "idle_days": idle_days,
        "idle_ratio": idle_days / len(texts) * 100,
        "trading_days": len(texts),
        "trades": trades,
        "sleeve_trades": sleeve_trades,
        "sleeve_trade_count": len(sleeve_trades),
        "sleeve_days": sleeve_days,
        "yearly": fb._compute_yearly_returns_from_arrays(texts, equity),
    }


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def _row(label: str, result: Dict) -> str:
    sleeve = f"  补仓 {result['sleeve_trade_count']} 笔/在场 {result['sleeve_days']}天" if result.get("sleeve_trade_count") else ""
    return (f"{label:<52} 总收益 {result['total_return']:8.2f}%  年化 {result['annualized_return']:6.2f}%  "
            f"回撤 {result['max_drawdown']:5.2f}%  夏普 {result['sharpe_ratio']:.2f}  卡玛 {result['calmar_ratio']:.2f}  "
            f"波动 {result['annualized_volatility']:5.2f}%  买卖 {result['buy_count']}/{result['sell_count']}  "
            f"空仓 {result['idle_ratio']:.1f}%{sleeve}")


def verify(frames: Dict[str, pd.DataFrame], params) -> None:
    """推广后的状态机必须与生产 _run_seesaw_backtest 在三标的时逐笔一致。"""
    canonical = fb._run_seesaw_backtest(frames["main"], frames["sub"], params, INITIAL_CAPITAL, detailed=True,
                                        sub2_base_df=frames["sub2"])
    mine = run_rotation(frames, BASE_TARGETS, params)
    left = [(t["date"], t["action"], t["symbol"], round(t["price"], 6)) for t in canonical["trades"]]
    right = [(t["date"], t["action"], t["symbol"], round(t["price"], 6)) for t in mine["trades"]]
    print(f"生产函数: 总收益 {canonical['total_return']:.4f}%  本脚本: {mine['total_return']:.4f}%")
    if left != right or abs(canonical["total_return"] - mine["total_return"]) > 1e-6:
        for a, b in zip(left, right):
            print("  ", a, "|", b, "" if a == b else "  <-- 不一致")
        raise SystemExit("对账失败")
    print(f"对账通过：{len(left)} 笔成交逐笔一致")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--details", action="store_true", help="打印每个方案的逐笔成交")
    parser.add_argument("--drug-grid", action="store_true", help="重跑创新药阈值扫描（四标的）")
    parser.add_argument("--sleeve", action="store_true", help="加跑空仓补仓对照")
    parser.add_argument("--sleeve-pct", type=float, default=20.0)
    parser.add_argument("--sleeve-margins", type=float, nargs="*", default=[0.0, 5.0, 10.0, 999.0])
    parser.add_argument("--sleeve-fills", nargs="*", default=["pessimistic", "open"],
                        choices=["pessimistic", "open"])
    args = parser.parse_args()

    params = fb.SOXLFearStrategyParams(**BASE_PARAMS)
    pool_targets = [*BASE_TARGETS, INNOVATION_DRUG] if args.drug_grid else BASE_TARGETS
    frames = {t.key: load_target_frame(t, use_cache=not args.no_cache) for t in pool_targets}
    verify(frames, params)

    base = run_rotation(frames, BASE_TARGETS, params)
    print("\n基准（510880 买入持有）: " + _row("", {**base["benchmark_metrics"], "buy_count": 1, "sell_count": 0,
                                                "idle_ratio": 0.0}).strip())

    if args.drug_grid:
        print("\n=== 创新药阈值扫描（四标的，卖出不加确认） ===")
        for buy_threshold in (15.0, 20.0, 25.0, 30.0, 35.0):
            for vr_threshold in (1.0, 1.3, 1.6, 2.0):
                drug = replace(INNOVATION_DRUG, buy_threshold=buy_threshold, volume_ratio_threshold=vr_threshold)
                result = run_rotation(frames, [*BASE_TARGETS, drug], params)
                print(_row(f"创新药 恐贪<={buy_threshold:g} 量比>={vr_threshold:g}", result))

    print("\n=== 三标的：卖出确认方式 × 空仓补仓 ===")
    scenarios = []
    sleeve_frames = load_sleeve_frames(use_cache=not args.no_cache) if args.sleeve else None
    for mode in (SELL_CONFIRM_NONE, SELL_CONFIRM_MA5_SIGNAL):
        base_label = "三标的" + (SELL_CONFIRM_LABELS[mode] or "（现有逻辑）")
        result = run_rotation(frames, BASE_TARGETS, params, sell_confirm=mode)
        scenarios.append((base_label, result))
        print(_row(base_label, result))
        if not sleeve_frames:
            continue
        for fill in args.sleeve_fills:
            fill_label = "最悲观成交" if fill == "pessimistic" else "开盘价成交"
            for margin in args.sleeve_margins:
                margin_label = "不换仓" if margin >= 900 else f"换仓差{margin:g}分"
                label = f"{base_label} +空仓{args.sleeve_pct:g}%补仓({margin_label},{fill_label})"
                result = run_rotation(frames, BASE_TARGETS, params, sell_confirm=mode,
                                      sleeve_frames=sleeve_frames, sleeve_pct=args.sleeve_pct,
                                      sleeve_switch_margin=margin, sleeve_fill=fill)
                scenarios.append((label, result))
                print(_row(label, result))

    print()
    for label, result in scenarios:
        yearly = "  ".join(f"{item['year']}:{item['return']:.1f}%" for item in result["yearly"])
        print(f"{label:<44} {yearly}")
    if args.details:
        for label, result in scenarios:
            print(f"\n--- {label} ---")
            for trade in sorted([*result["trades"], *[{**t, "sleeve": True} for t in result["sleeve_trades"]]],
                                key=lambda item: item["date"]):
                tag = "补仓" if trade.get("sleeve") else "主仓"
                print(f"  {trade['date']} {tag} {trade['action']:<4} {trade['symbol']} {trade['price']:.3f} {trade['reason']}")


if __name__ == "__main__":
    main()

import logging
import numpy as np
from typing import List, Dict, Any
from sqlalchemy import func
from ..core.database import MarketSignal, Session, StockEVC
from ..core.static_info import get_static_info_snapshot_map
from ..core.services.quote import QuoteProvider, QuoteService

MARKET_SIGNAL_STRATEGIES = [
    {
        "id": "v1",
        "name": "200MA低位放量买点",
        "directions": ["BUY"],
        "summary": "低于200日均线、接近50日低点，并出现5日成交量抬升的低位买入信号。",
        "params": [
            {"key": "below_200ma_ratio_thresh", "label": "低于200MA", "default": 0.1, "min": 0, "max": 0.8, "step": 0.01, "precision": 3},
            {"key": "vol_5_std_thresh", "label": "5日量Z", "default": 1.0, "min": -5, "max": 10, "step": 0.1, "precision": 2},
            {"key": "today_vol_std_thresh", "label": "当日量Z上限", "default": 0.5, "min": -5, "max": 10, "step": 0.1, "precision": 2},
            {"key": "close_vs_low_50_ratio", "label": "50日低点倍数", "default": 1.1, "min": 0.8, "max": 2.0, "step": 0.01, "precision": 3},
        ],
    },
    {
        "id": "v2",
        "name": "涨跌幅企稳放量拐点",
        "directions": ["BUY", "SELL"],
        "summary": "先出现大幅上涨或下跌，再经过企稳期，最后用放量确认买点或卖点。",
        "params": [
            {"key": "price_change_ratio", "label": "涨跌幅%", "default": 30.0, "min": 1, "max": 200, "step": 1, "precision": 1},
            {"key": "stabilization_period", "label": "企稳K数", "default": 10, "min": 1, "max": 120, "step": 1, "precision": 0},
            {"key": "klines_volume_std_multiplier", "label": "放量倍数", "default": 2.0, "min": 0, "max": 10, "step": 0.1, "precision": 2},
            {"key": "klines_volume_days", "label": "量能窗口", "default": 20, "min": 2, "max": 120, "step": 1, "precision": 0},
        ],
    },
    {
        "id": "v3",
        "name": "成交量趋势突破买点",
        "directions": ["BUY"],
        "summary": "近期成交量逐级高于中期和长期成交量，且价格站上均线的趋势买入信号。",
        "params": [
            {"key": "recent_days", "label": "近期量天数", "default": 5, "min": 1, "max": 60, "step": 1, "precision": 0},
            {"key": "mid_days", "label": "中期量天数", "default": 5, "min": 1, "max": 120, "step": 1, "precision": 0},
            {"key": "long_days", "label": "长期量天数", "default": 100, "min": 5, "max": 300, "step": 1, "precision": 0},
            {"key": "long_ratio_thresh", "label": "长期量倍数", "default": 2.0, "min": 0, "max": 10, "step": 0.1, "precision": 2},
            {"key": "ma_days", "label": "价格均线", "default": 60, "min": 5, "max": 300, "step": 1, "precision": 0},
        ],
    },
]

MARKET_SIGNAL_STRATEGY_MAP = {item["id"]: item for item in MARKET_SIGNAL_STRATEGIES}
MARKET_SIGNAL_MAX_LOOKBACK = 200

def preprocess_klines_volume(
    klines: List[Dict[str, Any]],
    std_dev_multiplier: float = 2.0,
    days: int = 20
) -> List[Dict[str, Any]]:
    """
    预处理K线，增加成交量均线、标准差、isVolumeSpike字段
    """
    result = []
    for index, kline in enumerate(klines):
        if index < (days - 1):
            result.append({
                **kline,
                "volumeMA": None,
                "volumeStdDev": None,
                "isVolumeSpike": False
            })
            continue
        volumes = [float(k['volume']) for k in klines[index - (days - 1): index + 1]]
        mean = np.mean(volumes)
        std_dev = np.std(volumes)
        is_spike = float(kline['volume']) > mean + std_dev * std_dev_multiplier
        result.append({
            **kline,
            "volumeMA": mean,
            "volumeStdDev": std_dev,
            "isVolumeSpike": is_spike
        })
    return result

class MarketSignalCalculator:
    """
    市场信号算法（原有逻辑）
    """
    def __init__(
        self,
        below_200ma_ratio_thresh=0.1,
        vol_5_std_thresh=1,
        today_vol_std_thresh=0.5,
        close_vs_low_50_ratio=1.1,
    ):
        self.below_200ma_ratio_thresh = below_200ma_ratio_thresh
        self.vol_5_std_thresh = vol_5_std_thresh
        self.today_vol_std_thresh = today_vol_std_thresh
        self.close_vs_low_50_ratio = close_vs_low_50_ratio

    def analyze_signal(self, klines):
        if not klines or len(klines) < 200:
            return None

        closes = np.array([float(k['close']) for k in klines])
        vols = np.array([float(k['volume']) for k in klines])
        lows = np.array([float(k['low']) for k in klines])

        lst_date = klines[-1]['timestamp'].date()
        close_today = closes[-1]
        vol_today = vols[-1]
        low_50 = np.min(lows[-50:])
        ma_200 = np.mean(closes[-200:])
        below_200ma_ratio = (ma_200 - close_today) / ma_200

        vol_5_avg = np.mean(vols[-5:])
        vol_50_avg = np.mean(vols[-50:])
        vol_50_std = np.std(vols[-50:])
        vol_5_std = (vol_5_avg - vol_50_avg) / vol_50_std if vol_50_std > 0 else 0
        today_vol_std = (vol_today - vol_50_avg) / vol_50_std if vol_50_std > 0 else 0

        close_vs_low_50 = close_today / (low_50 * self.close_vs_low_50_ratio) if low_50 > 0 else 0

        result = {
            "close_today": close_today,
            "ma_200": ma_200,
            "below_200ma_ratio": below_200ma_ratio,
            "vol_5_std": vol_5_std,
            "today_vol_std": today_vol_std,
            "low_50": low_50,
            "close_vs_low_50": close_vs_low_50,
            "date": lst_date,
            "direction": None
        }

        if (
            close_today < ma_200 * (1 - self.below_200ma_ratio_thresh) and
            vol_5_std > self.vol_5_std_thresh and
            today_vol_std < self.today_vol_std_thresh and
            close_today < low_50 * self.close_vs_low_50_ratio
        ):
            result["direction"] = "BUY"

        return result

class BuySellAnalyzer:
    """
    只判断最新一根K线是否为买点或卖点（兼容前端涨跌幅、企稳时间、放量逻辑）
    返回 {'type': 'BUY'/'SELL'/None, 'price': float, 'timestamp': datetime}
    """
    def __init__(
        self,
        price_change_ratio: float = 30,      # 涨跌幅阈值（百分比）
        stabilization_period: int = 10,      # 企稳时间（K线根数）
    ):
        self.price_change_ratio = price_change_ratio
        self.stabilization_period = stabilization_period
        self.min_volume_spike_index = stabilization_period + 10

    def analyze(self, klines: List[Dict[str, Any]]) -> Dict[str, Any] or None:
        if not klines or len(klines) < self.min_volume_spike_index + 1:
            return None

        idx = len(klines) - 1
        k = klines[idx]

        # 必须是放量
        if not k.get("isVolumeSpike", False):
            return None

        # 必须已经经过企稳期
        if idx < self.min_volume_spike_index:
            return None

        # 买点逻辑：是否满足高点-低点-企稳-放量
        highest_idx = np.argmax([float(kk['high']) for kk in klines])
        if highest_idx < idx - self.stabilization_period:
            lows_range = klines[highest_idx+1:idx-self.stabilization_period+1]
            if lows_range:
                lowest_idx_after_high = highest_idx + 1 + np.argmin([float(kk['low']) for kk in lows_range])
                if lowest_idx_after_high <= idx - self.stabilization_period:
                    highest_price = float(klines[highest_idx]['high'])
                    lowest_price = float(klines[lowest_idx_after_high]['low'])
                    if highest_price > 0 and (highest_price - lowest_price) / highest_price > (self.price_change_ratio / 100):
                        if idx == lowest_idx_after_high + self.stabilization_period:
                            return {
                                "type": "BUY",
                                "price": float(k['low']),
                                "timestamp": k['timestamp']
                            }

        # 卖点逻辑：是否满足低点-高点-企稳-放量
        lowest_idx = np.argmin([float(kk['low']) for kk in klines])
        if lowest_idx < idx - self.stabilization_period:
            highs_range = klines[lowest_idx+1:idx-self.stabilization_period+1]
            if highs_range:
                highest_idx_after_low = lowest_idx + 1 + np.argmax([float(kk['high']) for kk in highs_range])
                if highest_idx_after_low <= idx - self.stabilization_period:
                    lowest_price = float(klines[lowest_idx]['low'])
                    highest_price = float(klines[highest_idx_after_low]['high'])
                    if lowest_price > 0 and (highest_price - lowest_price) / lowest_price > (self.price_change_ratio / 100):
                        if idx == highest_idx_after_low + self.stabilization_period:
                            return {
                                "type": "SELL",
                                "price": float(k['high']),
                                "timestamp": k['timestamp']
                            }

        return None

class VolumeTrendBuyAnalyzer:
    def __init__(
        self,
        recent_days: int = 5,
        mid_days: int = 5,
        long_days: int = 100,
        long_ratio_thresh: float = 2.0,
        ma_days: int = 60,
    ):
        self.recent_days = recent_days
        self.mid_days = mid_days
        self.long_days = long_days
        self.long_ratio_thresh = long_ratio_thresh
        self.ma_days = ma_days

    def analyze(self, klines: List[Dict[str, Any]]) -> Dict[str, Any] or None:
        total_needed = self.recent_days + self.mid_days + self.long_days
        if not klines or len(klines) < max(total_needed, self.ma_days):
            return None
        vols = np.array([float(k['volume']) for k in klines])
        closes = np.array([float(k['close']) for k in klines])
        recent_avg = float(np.mean(vols[-self.recent_days:]))
        mid_start = self.recent_days + self.mid_days
        mid_avg = float(np.mean(vols[-mid_start:-self.recent_days]))
        long_end = self.recent_days + self.mid_days + self.long_days
        long_avg = float(np.mean(vols[-long_end:-mid_start]))
        cond_volume = recent_avg > mid_avg and mid_avg > long_avg * self.long_ratio_thresh
        ma60 = float(np.mean(closes[-self.ma_days:]))
        close_today = float(closes[-1])
        cond_price = close_today > ma60
        if cond_volume and cond_price:
            k = klines[-1]
            return {
                "type": "BUY",
                "price": close_today,
                "timestamp": k['timestamp']
            }
        return None

class MarketSignalStrategyEvaluator:
    """
    将各个市场信号策略统一包装，供入库和回测复用。
    """
    def __init__(
        self,
        below_200ma_ratio_thresh=0.1,
        vol_5_std_thresh=1,
        today_vol_std_thresh=0.5,
        close_vs_low_50_ratio=1.1,
        price_change_ratio=30,
        stabilization_period=10,
        klines_volume_std_multiplier=2.0,
        klines_volume_days=20,
        recent_days=5,
        mid_days=5,
        long_days=100,
        long_ratio_thresh=2.0,
        ma_days=60,
    ):
        self.signal_calculator = MarketSignalCalculator(
            below_200ma_ratio_thresh=below_200ma_ratio_thresh,
            vol_5_std_thresh=vol_5_std_thresh,
            today_vol_std_thresh=today_vol_std_thresh,
            close_vs_low_50_ratio=close_vs_low_50_ratio
        )
        self.buy_sell_analyzer = BuySellAnalyzer(
            price_change_ratio=price_change_ratio,
            stabilization_period=stabilization_period
        )
        self.volume_trend_buy_analyzer = VolumeTrendBuyAnalyzer(
            recent_days=int(recent_days),
            mid_days=int(mid_days),
            long_days=int(long_days),
            long_ratio_thresh=long_ratio_thresh,
            ma_days=int(ma_days),
        )
        self.klines_volume_std_multiplier = klines_volume_std_multiplier
        self.klines_volume_days = int(klines_volume_days)

    def _date_from_timestamp(self, timestamp):
        return timestamp.date() if hasattr(timestamp, "date") else timestamp

    def analyze_all(self, klines: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        if not klines:
            return {}

        processed_klines = preprocess_klines_volume(
            klines,
            std_dev_multiplier=self.klines_volume_std_multiplier,
            days=self.klines_volume_days
        )
        return {
            "v1": self.signal_calculator.analyze_signal(klines),
            "v2": self.buy_sell_analyzer.analyze(processed_klines),
            "v3": self.volume_trend_buy_analyzer.analyze(klines),
        }

    def analyze_strategy(self, strategy_id: str, klines: List[Dict[str, Any]]) -> Dict[str, Any] or None:
        if strategy_id not in MARKET_SIGNAL_STRATEGY_MAP:
            raise ValueError(f"Unsupported market signal strategy: {strategy_id}")
        return self.analyze_all(klines).get(strategy_id)

    def build_record(self, strategy_id: str, symbol: str, result: Dict[str, Any]) -> MarketSignal or None:
        if not result:
            return None

        if strategy_id == "v1" and result.get("direction") == "BUY":
            return MarketSignal(
                ver="v1",
                symbol=symbol,
                close_price=round(result["close_today"], 2),
                below_200ma_ratio=round(result["below_200ma_ratio"], 2),
                vol_5_std=round(result["vol_5_std"], 2),
                today_vol_std=round(result["today_vol_std"], 2),
                low_50=round(result["low_50"], 2),
                close_vs_low_50=round(result["close_vs_low_50"], 2),
                date=result["date"],
                direction="BUY"
            )

        if strategy_id == "v2" and result.get("type") in {"BUY", "SELL"}:
            return MarketSignal(
                ver="v2",
                symbol=symbol,
                close_price=round(result["price"], 2),
                date=self._date_from_timestamp(result["timestamp"]),
                direction=result["type"],
                v2_price_change_ratio=self.buy_sell_analyzer.price_change_ratio,
                v2_stabilization_period=self.buy_sell_analyzer.stabilization_period
            )

        if strategy_id == "v3" and result.get("type") == "BUY":
            return MarketSignal(
                ver="v3",
                symbol=symbol,
                close_price=round(result["price"], 2),
                date=self._date_from_timestamp(result["timestamp"]),
                direction="BUY",
            )

        return None

    def build_signal_event(self, strategy_id: str, symbol: str, result: Dict[str, Any]) -> Dict[str, Any] or None:
        record = self.build_record(strategy_id, symbol, result)
        if not record:
            return None
        strategy = MARKET_SIGNAL_STRATEGY_MAP[strategy_id]
        return {
            "strategy_id": strategy_id,
            "strategy_name": strategy["name"],
            "symbol": symbol,
            "date": record.date,
            "direction": record.direction,
            "signal_price": record.close_price,
        }

class MarketSignalAnalyzer:
    """
    主流程，按策略分别生成信号
    """
    def __init__(
        self,
        quote_provider: QuoteProvider,
        etf_symbols=None,
        min_market_cap=20_000_000_000,
        below_200ma_ratio_thresh=0.1,
        vol_5_std_thresh=1,
        today_vol_std_thresh=0.5,
        close_vs_low_50_ratio=1.1,
        price_change_ratio=30,
        stabilization_period=10,
        klines_volume_std_multiplier=2.0,
        klines_volume_days=20
    ):
        self.quote_service = QuoteService(quote_provider)
        self.db_session = Session()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.etf_symbols = etf_symbols or ['SPY.US', 'QQQ.US']
        self.min_market_cap = min_market_cap

        self.strategy_evaluator = MarketSignalStrategyEvaluator(
            below_200ma_ratio_thresh=below_200ma_ratio_thresh,
            vol_5_std_thresh=vol_5_std_thresh,
            today_vol_std_thresh=today_vol_std_thresh,
            close_vs_low_50_ratio=close_vs_low_50_ratio,
            price_change_ratio=price_change_ratio,
            stabilization_period=stabilization_period,
            klines_volume_std_multiplier=klines_volume_std_multiplier,
            klines_volume_days=klines_volume_days
        )

    def get_holdings(self):
        latest_date = self.db_session.query(func.max(StockEVC.date)).scalar()
        if not latest_date:
            return []
        rows = self.db_session.query(StockEVC).filter(StockEVC.date == latest_date).all()
        symbols = {r.symbol for r in rows}
        return list(symbols)

    def _persist_records(self, records: List[MarketSignal], stats: Dict[str, int]) -> int:
        if not records:
            return 0

        unique_symbols = list({r.symbol for r in records})
        static_infos = get_static_info_snapshot_map(self.db_session, unique_symbols)
        shares_map = {}
        for sym, info in static_infos.items():
            shares = info.get('total_shares')
            if sym is not None and isinstance(shares, (int, float)) and shares > 0:
                shares_map[sym] = float(shares)

        quotes = self.quote_service.get_quote_batch(unique_symbols)
        price_map = {q.get('symbol') or q.get('code'): q.get('price') for q in quotes}

        persisted = 0
        for record in records:
            symbol = record.symbol
            price = price_map.get(symbol)
            shares = shares_map.get(symbol)
            market_cap = (shares * price) if isinstance(price, (int, float)) and isinstance(shares, (int, float)) else 0

            if market_cap < self.min_market_cap:
                stats["market_cap_filtered"] = stats.get("market_cap_filtered", 0) + 1
                if market_cap == 0:
                    stats["market_cap_missing_data"] = stats.get("market_cap_missing_data", 0) + 1
                continue

            existing = self.db_session.query(MarketSignal).filter_by(
                symbol=symbol,
                date=record.date,
                ver=record.ver,
            ).first()
            if existing:
                for key, value in record.__dict__.items():
                    if not key.startswith('_') and key != 'id':
                        setattr(existing, key, value)
                stats["updated"] = stats.get("updated", 0) + 1
            else:
                self.db_session.add(record)
                stats["inserted"] = stats.get("inserted", 0) + 1
            persisted += 1

        self.db_session.commit()
        return persisted

    def _flush_records(self, pending_records: List[MarketSignal], stats: Dict[str, int], reason: str) -> int:
        if not pending_records:
            return 0
        flushed = self._persist_records(pending_records, stats)
        self.logger.info(
            "Market signal flush reason=%s, candidates=%s, persisted=%s, inserted=%s, updated=%s, filtered_by_market_cap=%s, missing_market_cap_data=%s",
            reason,
            len(pending_records),
            flushed,
            stats.get("inserted", 0),
            stats.get("updated", 0),
            stats.get("market_cap_filtered", 0),
            stats.get("market_cap_missing_data", 0),
        )
        pending_records.clear()
        return flushed

    def analyze(self):
        symbols = self.get_holdings()
        pending_records = []
        results = []
        stats = {
            "symbols_total": len(symbols),
            "symbols_scanned": 0,
            "skip_klines": 0,
            "v1_candidates": 0,
            "v2_buy_candidates": 0,
            "v2_sell_candidates": 0,
            "v3_buy_candidates": 0,
            "inserted": 0,
            "updated": 0,
            "market_cap_filtered": 0,
            "market_cap_missing_data": 0,
        }
        flush_every = 500

        self.logger.info(
            "Start analyzing market signals for %s symbols with min_market_cap=%s",
            len(symbols),
            self.min_market_cap,
        )

        for index, symbol in enumerate(symbols, 1):
            stats["symbols_scanned"] += 1
            klines = self.quote_service.get_klines(symbol, 200)
            if not klines or len(klines) < 200:
                stats["skip_klines"] += 1
                continue

            strategy_results = self.strategy_evaluator.analyze_all(klines)
            signal_result = strategy_results.get("v1")
            buy_sell_result = strategy_results.get("v2")
            volume_trend_result = strategy_results.get("v3")

            # 市场信号买点（延后入库，统一批量过滤市值）
            record = self.strategy_evaluator.build_record("v1", symbol, signal_result)
            if record:
                stats["v1_candidates"] += 1
                pending_records.append(record)

            # 只存最新一根K线的买卖点（延后入库）
            record = self.strategy_evaluator.build_record("v2", symbol, buy_sell_result)
            if record:
                if record.direction == "BUY":
                    stats["v2_buy_candidates"] += 1
                else:
                    stats["v2_sell_candidates"] += 1
                pending_records.append(record)

            record = self.strategy_evaluator.build_record("v3", symbol, volume_trend_result)
            if record:
                stats["v3_buy_candidates"] += 1
                pending_records.append(record)

            results.append({
                "symbol": symbol,
                "signal": signal_result,
                "buy_sell": buy_sell_result,
                "volume_trend": volume_trend_result
            })

            if index % flush_every == 0:
                self.logger.info(
                    "Market signal progress %s/%s, candidates so far: v1=%s, v2_buy=%s, v2_sell=%s, v3_buy=%s, pending=%s, skipped_klines=%s",
                    index,
                    len(symbols),
                    stats["v1_candidates"],
                    stats["v2_buy_candidates"],
                    stats["v2_sell_candidates"],
                    stats["v3_buy_candidates"],
                    len(pending_records),
                    stats["skip_klines"],
                )
                self._flush_records(pending_records, stats, reason=f"progress-{index}")

        self._flush_records(pending_records, stats, reason="final")
        self.logger.info(
            "Market signal analyze finished: total_symbols=%s, scanned=%s, skipped_klines=%s, v1_candidates=%s, v2_buy_candidates=%s, v2_sell_candidates=%s, v3_buy_candidates=%s, inserted=%s, updated=%s, filtered_by_market_cap=%s, missing_market_cap_data=%s",
            stats["symbols_total"],
            stats["symbols_scanned"],
            stats["skip_klines"],
            stats["v1_candidates"],
            stats["v2_buy_candidates"],
            stats["v2_sell_candidates"],
            stats["v3_buy_candidates"],
            stats["inserted"],
            stats["updated"],
            stats["market_cap_filtered"],
            stats["market_cap_missing_data"],
        )

        return results

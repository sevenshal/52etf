import datetime
import logging
import numpy as np
from typing import List, Dict, Any
from sqlalchemy import func
from ..core.database import ETFHolding, MarketSignal, Session, StockEVC
from ..core.services.quote import QuoteProvider, QuoteService

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

class MarketSignalAnalyzer:
    """
    主流程，结合两种算法
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
        self.volume_trend_buy_analyzer = VolumeTrendBuyAnalyzer()
        self.klines_volume_std_multiplier = klines_volume_std_multiplier
        self.klines_volume_days = klines_volume_days

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
        static_infos = self.quote_service.get_static_info(unique_symbols)
        shares_map = {}
        for info in static_infos:
            sym = info.get('symbol') or info.get('code')
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

            processed_klines = preprocess_klines_volume(
                klines,
                std_dev_multiplier=self.klines_volume_std_multiplier,
                days=self.klines_volume_days
            )

            signal_result = self.signal_calculator.analyze_signal(klines)
            buy_sell_result = self.buy_sell_analyzer.analyze(processed_klines)
            volume_trend_result = self.volume_trend_buy_analyzer.analyze(klines)

            # 市场信号买点（延后入库，统一批量过滤市值）
            if signal_result and signal_result.get("direction") == "BUY":
                stats["v1_candidates"] += 1
                record = MarketSignal(
                    ver='v1',
                    symbol=symbol,
                    close_price=round(signal_result["close_today"], 2),
                    below_200ma_ratio=round(signal_result["below_200ma_ratio"], 2),
                    vol_5_std=round(signal_result["vol_5_std"], 2),
                    today_vol_std=round(signal_result["today_vol_std"], 2),
                    low_50=round(signal_result["low_50"], 2),
                    close_vs_low_50=round(signal_result["close_vs_low_50"], 2),
                    date=signal_result["date"],
                    direction='BUY'
                )
                pending_records.append(record)

            # 只存最新一根K线的买卖点（延后入库）
            if buy_sell_result:
                if buy_sell_result["type"] == "BUY":
                    stats["v2_buy_candidates"] += 1
                else:
                    stats["v2_sell_candidates"] += 1
                record = MarketSignal(
                    ver='v2',
                    symbol=symbol,
                    close_price=round(buy_sell_result["price"], 2),
                    date=buy_sell_result["timestamp"].date() if hasattr(buy_sell_result["timestamp"], "date") else buy_sell_result["timestamp"],
                    direction=buy_sell_result["type"],
                    v2_price_change_ratio=self.buy_sell_analyzer.price_change_ratio,
                    v2_stabilization_period=self.buy_sell_analyzer.stabilization_period
                )
                pending_records.append(record)

            if volume_trend_result:
                stats["v3_buy_candidates"] += 1
                record = MarketSignal(
                    ver='v3',
                    symbol=symbol,
                    close_price=round(volume_trend_result["price"], 2),
                    date=volume_trend_result["timestamp"].date() if hasattr(volume_trend_result["timestamp"], "date") else volume_trend_result["timestamp"],
                    direction=volume_trend_result["type"],
                )
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

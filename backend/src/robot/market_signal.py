import datetime
import numpy as np
from typing import List, Dict, Any
from ..core.database import ETFHolding, MarketSignal, Session
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

class MarketSignalAnalyzer:
    """
    主流程，结合两种算法
    """
    def __init__(
        self,
        quote_provider: QuoteProvider,
        etf_symbols=None,
        min_market_cap=1e10,
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
        self.klines_volume_std_multiplier = klines_volume_std_multiplier
        self.klines_volume_days = klines_volume_days

    def get_holdings(self):
        holdings = self.db_session.query(ETFHolding).filter(
            ETFHolding.etf_symbol.in_(self.etf_symbols),
            ETFHolding.asset_class == 'Equity'
        ).all()
        symbols = {h.symbol for h in holdings if h.market_cap and h.market_cap > self.min_market_cap}
        return list(symbols)

    def analyze(self):
        symbols = self.get_holdings()
        results = []
        for symbol in symbols:
            klines = self.quote_service.get_klines(symbol, 200)
            if not klines or len(klines) < 200:
                continue

            processed_klines = preprocess_klines_volume(
                klines,
                std_dev_multiplier=self.klines_volume_std_multiplier,
                days=self.klines_volume_days
            )

            signal_result = self.signal_calculator.analyze_signal(klines)
            buy_sell_result = self.buy_sell_analyzer.analyze(processed_klines)

            # 市场信号买点存库
            if signal_result and signal_result.get("direction") == "BUY":
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
                existing = self.db_session.query(MarketSignal).filter_by(symbol=symbol, date=signal_result["date"], direction='BUY').first()
                if existing:
                    for k, v in record.__dict__.items():
                        if not k.startswith('_') and k != 'id':
                            setattr(existing, k, v)
                else:
                    self.db_session.add(record)
                self.db_session.commit()

            # 只存最新一根K线的买卖点
            if buy_sell_result:
                record = MarketSignal(
                    ver='v2',
                    symbol=symbol,
                    close_price=round(buy_sell_result["price"], 2),
                    date=buy_sell_result["timestamp"].date() if hasattr(buy_sell_result["timestamp"], "date") else buy_sell_result["timestamp"],
                    direction=buy_sell_result["type"],
                    v2_price_change_ratio=self.buy_sell_analyzer.price_change_ratio,
                    v2_stabilization_period=self.buy_sell_analyzer.stabilization_period

                )
                existing = self.db_session.query(MarketSignal).filter_by(
                    symbol=symbol, date=record.date, direction=record.direction
                ).first()
                if existing:
                    for k, v in record.__dict__.items():
                        if not k.startswith('_') and k != 'id':
                            setattr(existing, k, v)
                else:
                    self.db_session.add(record)
                self.db_session.commit()

            results.append({
                "symbol": symbol,
                "signal": signal_result,
                "buy_sell": buy_sell_result
            })

        return results
import datetime
import numpy as np
from ..core.database import ETFHolding, MarketSignal, Session
from ..core.services.quote import QuoteProvider, QuoteService

class MarketSignalAnalyzer:
    def __init__(
        self,
        quote_provider: QuoteProvider,
        etf_symbols=None,
        min_market_cap=1e10,
        below_200ma_ratio_thresh=0.1,
        vol_5_std_thresh=1,
        today_vol_std_thresh=0.5,
        close_vs_low_50_ratio=1.1,
    ):
        self.quote_service = QuoteService(quote_provider)
        self.db_session = Session()
        self.etf_symbols = etf_symbols or ['SPY.US', 'QQQ.US']
        self.min_market_cap = min_market_cap
        self.below_200ma_ratio_thresh = below_200ma_ratio_thresh
        self.vol_5_std_thresh = vol_5_std_thresh
        self.today_vol_std_thresh = today_vol_std_thresh
        self.close_vs_low_50_ratio = close_vs_low_50_ratio

    def get_holdings(self):
        holdings = self.db_session.query(ETFHolding).filter(
            ETFHolding.etf_symbol.in_(self.etf_symbols),
            ETFHolding.asset_class == 'Equity'
        ).all()
        #symbols = {h.symbol for h in holdings if h.market_cap and h.market_cap > self.min_market_cap}
        symbols = {h.symbol for h in holdings}
        return list(symbols)

    def analyze(self):
        symbols = self.get_holdings()
        results = []
        for symbol in symbols:
            klines = self.quote_service.get_klines(symbol, 200)
            if not klines or len(klines) < 200:
                continue
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

            if (
                close_today < ma_200 * (1 - self.below_200ma_ratio_thresh) and
                vol_5_std > self.vol_5_std_thresh and
                today_vol_std < self.today_vol_std_thresh and
                close_today < low_50 * self.close_vs_low_50_ratio
            ):
                record = MarketSignal(
                    symbol=symbol,
                    close_price=round(close_today, 2),
                    below_200ma_ratio=round(below_200ma_ratio, 2),
                    vol_5_std=round(vol_5_std, 2),
                    today_vol_std=round(today_vol_std, 2),
                    low_50=round(low_50, 2),
                    close_vs_low_50=round(close_vs_low_50, 2),
                    date=lst_date,
                    direction='BUY'
                )
                existing = self.db_session.query(MarketSignal).filter_by(symbol=symbol, date=lst_date).first()
                if existing:
                    # 更新已存在的数据
                    for k, v in record.__dict__.items():
                        if not k.startswith('_') and k != 'id':
                            setattr(existing, k, v)
                else:
                    self.db_session.add(record)
                self.db_session.commit()
        return results

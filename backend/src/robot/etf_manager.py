from datetime import datetime, date, timedelta
from typing import Dict, Optional, List
from ..core.database import ETFHolding as DBETFHolding
from ..core.database import ETFAnalysis, StockEVC, Session
from ..core.models.etf import ETFHolding, ETFHoldingsData
from ..core.static_info import get_static_info_snapshot, get_static_info_snapshot_map
from ..core.utils import normalize_us_equity_symbol
from .etf.base import ETFDataFetcher
from .etf.ishares import ISharesETFFetcher
from .etf.qqq import QQQDataFetcher
from .etf.spdr import SPDRDataFetcher
import logging
from ..core.services.quote import QuoteService, QuoteProvider
from ..core.services.szdt import SZDTService
import asyncio
import traceback
# 添加三倍做多ETF映射关系
# 添加ETF映射关系，[对应ETF代码, 杠杆倍数]
LEVERAGED_ETF_MAP = {
    'SOXX.US': ['SOXL.US', 3],  # 费城半导体
    'IWM.US': ['TNA.US', 3],    # 罗素2000
    'ITB.US': ['NAIL.US', 3],   # 房屋建筑
    'ITA.US': ['DFEN.US', 3],   # 航空航天(军工)
    'SPY.US': ['UPRO.US', 3],   # 标普500
    'DIA.US': ['UDOW.US', 3],   # 道琼斯工业平均
    'XBI.US': ['LABU.US', 3],   # 生物科技
    'KRE.US': ['DPST.US', 3],   # 地区银行
    'XLF.US': ['FAS.US', 3],    # 金融
    'XLV.US': ['CURE.US', 3],   # 医疗保健
    'XLK.US': ['TECL.US', 3],   # 科技
    'QQQ.US': ['TQQQ.US', 3],   # 纳斯达克100
    'XRT.US': ['RETL.US', 3],   # 标普零售
    'XLE.US': ['ERX.US', 3],    # 能源
    'XLI.US': ['DUSL.US', 3],   # 工业
    'XLU.US': ['UTSL.US', 3],   # 公用事业
    'IAK.US': ['IAK.US', 1],    # 保险
    'XLC.US': ['XLC.US', 1],    # 通信服务
    'XLP.US': ['XLP.US', 1],    # 必需消费
    'VDC.US': ['VDC.US', 1],    # Vanguard必需消费
    'VIG.US': ['VIG.US', 1],    # Vanguard股息增长
    'VOX.US': ['VOX.US', 1],    # Vanguard通信服务
    'VTI.US': ['VTI.US', 1],    # Vanguard全市场
    'VTV.US': ['VTV.US', 1],    # Vanguard价值
    'VUG.US': ['VUG.US', 1],    # Vanguard成长
}

def get_leveraged_etf_map_symbols() -> List[str]:
    """Return all single and leveraged ETF symbols defined in LEVERAGED_ETF_MAP."""
    symbols: List[str] = []
    for base_symbol, mapping in LEVERAGED_ETF_MAP.items():
        symbols.append(base_symbol)
        if mapping:
            symbols.append(mapping[0])
    normalized = [normalize_us_equity_symbol(symbol) for symbol in symbols]
    return sorted(dict.fromkeys(symbol for symbol in normalized if symbol and symbol.endswith(".US")))


def _recent_klines(quote_service: QuoteService, symbol: str, count: int, end_date: Optional[date] = None) -> List[dict]:
    end_value = end_date or date.today()
    start_value = end_value - timedelta(days=max(60, count * 3))
    klines = quote_service.get_klines(symbol, start_date=start_value, end_date=end_value)
    return klines[-count:] if count > 0 else klines

class ETFManager:
    """ETF数据管理
    
    负责:
    1. ETF持仓数据的获取和存储
    2. ETF估值分析
    3. 分析结果的存储
    """
    
    def __init__(self, quote_provider: QuoteProvider):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.db_session = Session()
        self.quote_service = QuoteService(quote_provider)
        self.szdt_service = SZDTService()
        ishares_fetcher = ISharesETFFetcher()
        spdr_fetcher = SPDRDataFetcher()
        self.fetchers: Dict[str, ETFDataFetcher] = {
            # 现有的 ETF
            'SOXX.US': ishares_fetcher,
            'IWM.US': ishares_fetcher,
            'ITB.US': ishares_fetcher,
            'ITA.US': ishares_fetcher,
            'SPY.US': spdr_fetcher,
            'DIA.US': spdr_fetcher,
            'XBI.US': spdr_fetcher,
            'KRE.US': spdr_fetcher,
            'XLF.US': spdr_fetcher,
            'XLV.US': spdr_fetcher,
            'XLK.US': spdr_fetcher,
            'XRT.US': spdr_fetcher,  # 添加 XRT
            'QQQ.US': QQQDataFetcher(),

            # 新增的 iShares ETF
            # 'IAK.US': ishares_fetcher,  # iShares U.S. Insurance ETF

            # 新增的 SPDR ETF
            # 'XLC.US': spdr_fetcher,  # SPDR Communication Services
            'XLE.US': spdr_fetcher,  # SPDR Energy
            'XLI.US': spdr_fetcher,  # SPDR Industrial
            # 'XLP.US': spdr_fetcher,  # SPDR Consumer Staples
            'XLU.US': spdr_fetcher,  # SPDR Utilities

            # # 新增的 Vanguard ETF
            # 'VDC.US': vanguard_fetcher,  # Vanguard Consumer Staples ETF
            # 'VIG.US': vanguard_fetcher,  # Vanguard Dividend Appreciation ETF
            # 'VOX.US': vanguard_fetcher,  # Vanguard Communication Services ETF
            # 'VTI.US': vanguard_fetcher,  # Vanguard Total Stock Market ETF
            # 'VTV.US': vanguard_fetcher,  # Vanguard Value ETF
            # 'VUG.US': vanguard_fetcher,  # Vanguard Growth ETF
        }
    
    def get_stock_evc_info(self, symbol: str) -> Optional[StockEVC]:
        """获取股票的估值信息"""
        try:
            today = date.today()
            evc_info = self.db_session.query(StockEVC).filter(
                StockEVC.symbol == symbol,
                StockEVC.date == today
            ).first()
            if evc_info:
                return evc_info

            # iShares 部分持仓会返回裸代码（如 NYSE: BE），
            # 而 EVC 侧通常存成 .US 后缀，这里做一次兼容回退。
            if symbol and "." not in symbol:
                return self.db_session.query(StockEVC).filter(
                    StockEVC.symbol == f"{symbol}.US",
                    StockEVC.date == today
                ).first()
            return None
        except Exception as e:
            self.logger.error(f"获取股票 {symbol} 的估值信息失败: {str(e)}")
            return None
    
    def get_total_shares(self, etf_symbol: str) -> int:
        """获取ETF的总发行股数"""
        try:
            snapshot_info = get_static_info_snapshot(self.db_session, etf_symbol)
            snapshot_total_shares = snapshot_info.get('total_shares') if snapshot_info else None
            if isinstance(snapshot_total_shares, (int, float)) and snapshot_total_shares > 0:
                return int(snapshot_total_shares)

            static_info = self.quote_service.get_static_info([etf_symbol])[0]
            return static_info['total_shares']
        except Exception as e:
            self.logger.error(f"获取ETF {etf_symbol} 总股数失败: {str(e)}")
            return None
    
    def update_holdings(self, etf_symbol: str):
        """更新ETF持仓数据"""
        try:
            # 获取最新持仓数据
            fetcher = self.fetchers.get(etf_symbol)
            if not fetcher:
                raise ValueError(f"不支持的ETF: {etf_symbol}")
                
            holdings_data = fetcher.get_holdings(etf_symbol)

            self.db_session.query(DBETFHolding).filter(
                DBETFHolding.etf_symbol == etf_symbol,
                DBETFHolding.date == holdings_data.update_date
            ).delete()
            # 保存到数据库
            for holding in holdings_data.holdings:
                db_holding = DBETFHolding(
                    etf_symbol=etf_symbol,
                    symbol=holding.symbol,
                    name=holding.name,
                    asset_class=holding.asset_class,
                    shares=holding.shares,
                    weight=holding.weight,
                    date=holdings_data.update_date
                )
                self.db_session.merge(db_holding)
                
            self.db_session.commit()
            return holdings_data
            
        except Exception:
            self.db_session.rollback()
            raise

    def get_latest_trading_date(self) -> date:
        klines = _recent_klines(self.quote_service, 'SPY.US', 1)
        if not klines:
            raise ValueError("无法通过 SPY 日K确认最近美股交易日")
        timestamp = klines[-1]['timestamp']
        return timestamp.date() if hasattr(timestamp, 'date') else timestamp

    def load_holdings_from_db(self, etf_symbol: str, target_date: date) -> ETFHoldingsData:
        holding_date = (
            self.db_session.query(DBETFHolding.date)
            .filter(
                DBETFHolding.etf_symbol == etf_symbol,
                DBETFHolding.date <= target_date,
            )
            .order_by(DBETFHolding.date.desc())
            .limit(1)
            .scalar()
        )
        if not holding_date:
            raise ValueError(
                f"{etf_symbol} {target_date} 或之前持仓不存在，请先执行 ETF持仓抓取入库或 ETF历史持仓回跑"
            )

        rows = (
            self.db_session.query(DBETFHolding)
            .filter(
                DBETFHolding.etf_symbol == etf_symbol,
                DBETFHolding.date == holding_date,
            )
            .order_by(DBETFHolding.weight.desc())
            .all()
        )
        if not rows:
            raise ValueError(
                f"{etf_symbol} {holding_date} 持仓不存在，请先执行 ETF持仓抓取入库或 ETF历史持仓回跑"
            )
        if holding_date != target_date:
            self.logger.info(
                "%s %s 持仓不存在，使用最近持仓日期 %s",
                etf_symbol,
                target_date,
                holding_date,
            )

        holdings = []
        for row in rows:
            symbol = row.symbol
            if row.asset_class == "Equity":
                symbol = normalize_us_equity_symbol(symbol)
                if not symbol:
                    self.logger.warning(
                        f"跳过无法规范化的 DB ETF 持仓股票代码: {row.etf_symbol} {row.date} {row.symbol}"
                    )
                    continue
            holdings.append(
                ETFHolding(
                    symbol=symbol,
                    name=row.name,
                    asset_class=row.asset_class,
                    shares=int(row.shares or 0),
                    weight=float(row.weight or 0.0),
                    market_value=float(row.shares or 0) if row.asset_class == "Cash" else 0.0,
                    price=1.0 if row.asset_class == "Cash" else None,
                )
            )
        return ETFHoldingsData(
            holdings=holdings,
            update_date=holding_date,
            total_shares=None,
            total_weight=sum(float(row.weight or 0.0) for row in rows),
        )

    def get_etf_name(self, etf_symbol: str) -> str:
        fetcher = self.fetchers.get(etf_symbol)
        config = getattr(fetcher, "ETF_CONFIGS", {}).get(etf_symbol) if fetcher else None
        if config and config.get("name"):
            return config["name"]
        return getattr(fetcher, "name", None) or etf_symbol
    
    def analyze_etf(self, etf_symbol: str):
        """分析ETF的估值情况"""
        try:
            # 获取最近美股交易日已入库的持仓数据。持仓同步由独立任务负责。
            holdings_date = self.get_latest_trading_date()
            holdings_data = self.load_holdings_from_db(etf_symbol, holdings_date)
            
            # 使用 抓取的总股本数，没有则使用 LongPort API 获取总股数
            total_shares = self.get_total_shares(etf_symbol) if holdings_data.total_shares is None else holdings_data.total_shares
            
            # 批量从数据库快照读取所有持仓股票的 static_info
            equity_symbols = [holding.symbol for holding in holdings_data.holdings if holding.asset_class == 'Equity' and holding.symbol]
            static_info_map = get_static_info_snapshot_map(self.db_session, equity_symbols) if equity_symbols else {}
            holding_quotes = self.quote_service.get_quote_batch(equity_symbols) if equity_symbols else []
            quote_price_map = {}
            for quote in holding_quotes:
                try:
                    if quote.get('symbol') and quote.get('price') is not None:
                        quote_price_map[quote.get('symbol')] = float(quote.get('price'))
                except (TypeError, ValueError):
                    continue

            # 初始化分析结果
            total_value = {
                'market_value': 0,
                'fair_value_lo': 0,
                'fair_value_hi': 0,
                'forward_next_fy_lo': 0,
                'forward_next_fy_hi': 0
            }
            
            forward_stocks = {
                'market_value': 0,
                'fair_value_lo': 0,
                'fair_value_hi': 0,
                'forward_next_fy_lo': 0,
                'forward_next_fy_hi': 0,
                'min_fair_value_date': None,
                'max_fair_value_date': None
            }

            # 初始化EPS相关的累计值
            total_revenue = 0
            total_revenue_v2 = 0
            total_forward_revenue = 0
            total_revenue_ttm = 0

            # 分析每个持仓
            for holding in holdings_data.holdings:
                try:
                    shares = int(holding.shares or 0)
                    price = float(holding.price) if holding.price is not None else None
                    market_value = float(holding.market_value) if holding.market_value is not None else (shares * price if price is not None else 0)
                    fair_value_lo = None
                    fair_value_hi = None
                    forward_next_fy_lo = None
                    forward_next_fy_hi = None
                    # 初始化估值信息
                    evc_info = None
                    if holding.asset_class == 'Equity' and holding.symbol:
                        price = quote_price_map.get(holding.symbol, price)
                        market_value = shares * price if price is not None else market_value
                        static_info = static_info_map.get(holding.symbol)
                        eps_v2 = float(static_info.get('eps') or 0.0) if static_info else 0.0
                        eps_ttm = float(static_info.get('eps_ttm') or 0.0) if static_info else 0.0
                        total_revenue_v2 += eps_v2 * shares
                        total_revenue_ttm += eps_ttm * shares

                        evc_info = self.get_stock_evc_info(holding.symbol)
                        if evc_info:
                            fair_value_lo = evc_info.fair_value_lo
                            fair_value_hi = evc_info.fair_value_hi
                            forward_next_fy_lo = evc_info.forward_next_fy_lo
                            forward_next_fy_hi = evc_info.forward_next_fy_hi
                            market_value = shares * price if price is not None else market_value
                            eps = (price / evc_info.pe_ratio) if (price is not None and evc_info.pe_ratio) else 0.0
                            eps_forword = (price / evc_info.forward_pe_ratio) if (price is not None and evc_info.forward_pe_ratio) else 0.0
                            total_revenue += eps * shares
                            total_forward_revenue += eps_forword * shares
                            if all([fair_value_lo, fair_value_hi, 
                                  forward_next_fy_lo, forward_next_fy_hi]):
                                forward_stocks['market_value'] += market_value
                                forward_stocks['fair_value_lo'] += shares * fair_value_lo
                                forward_stocks['fair_value_hi'] += shares * fair_value_hi
                                forward_stocks['forward_next_fy_lo'] += shares * forward_next_fy_lo
                                forward_stocks['forward_next_fy_hi'] += shares * forward_next_fy_hi
                                forward_stocks['min_fair_value_date'] = min(forward_stocks['min_fair_value_date'] or evc_info.fair_value_date, evc_info.fair_value_date)
                                forward_stocks['max_fair_value_date'] = max(forward_stocks['max_fair_value_date'] or evc_info.fair_value_date, evc_info.fair_value_date)
                                
                    market_value = market_value or 0
                    # 计算估值总额, 如果是非股票资产或无法获取EVC信息，使用市值作为估值
                    total_value['fair_value_lo'] += shares * (fair_value_lo or price or 0)
                    total_value['fair_value_hi'] += shares * (fair_value_hi or price or 0)
                    total_value['forward_next_fy_lo'] += shares * (forward_next_fy_lo or price or 0)
                    total_value['forward_next_fy_hi'] += shares * (forward_next_fy_hi or price or 0)
                    total_value['market_value'] += market_value
                    
                except Exception as e:
                    self.logger.warning(f"分析持仓 {holding.symbol} 时出错: {str(e)}")
                    continue

            self.logger.info(f"{etf_symbol} total_value_info: {total_value}, forward_stocks_info: {forward_stocks}")

            if not total_shares or total_value['market_value'] <= 0:
                raise ValueError(
                    f"{etf_symbol} 持仓市值或总股数无效，请检查 DB 持仓和 LongPort 行情"
                )

            # 计算有下财年估值的股票的权重和估值
            forward_stocks_weight = forward_stocks['market_value'] / total_value['market_value']
            
            # 计算加权估值（考虑这些股票在总市值中的占比）
            forward_stocks_value_lo = (forward_stocks['fair_value_lo'] / forward_stocks_weight / total_shares) if forward_stocks_weight > 0 else None
            forward_stocks_value_hi = (forward_stocks['fair_value_hi'] / forward_stocks_weight / total_shares) if forward_stocks_weight > 0 else None
            forward_stocks_fy_lo = (forward_stocks['forward_next_fy_lo'] / forward_stocks_weight / total_shares) if forward_stocks_weight > 0 else None
            forward_stocks_fy_hi = (forward_stocks['forward_next_fy_hi'] / forward_stocks_weight / total_shares) if forward_stocks_weight > 0 else None

            market_price = self.quote_service.get_quote(etf_symbol)['price']
            szdt_info = None
            if etf_symbol in LEVERAGED_ETF_MAP:
                leveraged_code, lever = LEVERAGED_ETF_MAP[etf_symbol]  # 解构获取杠杆ETF代码和倍数
                szdt_resp = asyncio.run(self.szdt_service.get_etf_emotion(etf_type=1 if lever == 3 else 2))
                # 在返回的数组中查找对应的杠杆ETF数据
                if szdt_resp and szdt_resp['status'] == 1:
                    for item in szdt_resp['data']:
                        if item['code'] == f"US.{leveraged_code.replace('.US', '')}":
                            szdt_info = {
                                'price': float(item['emotion']['price']),
                                'score': item['emotion']['score'],
                                'time': item['emotion']['updated_at']
                            }
                            break
                self.logger.info(f"{lever}倍做多ETF {leveraged_code} 情绪指数: {szdt_info}")
            
            # 计算最终的加权平均EPS
            weighted_eps = total_revenue / total_shares
            weighted_eps_v2 = total_revenue_v2 / total_shares
            weighted_eps_forward = total_forward_revenue / total_shares
            weighted_eps_ttm = total_revenue_ttm / total_shares

            # 保存分析结果
            analysis = ETFAnalysis(
                symbol=etf_symbol,
                date=date.today(),
                name=self.get_etf_name(etf_symbol),
                update_date=holdings_data.update_date.strftime('%Y-%m-%d'),
                total_shares=total_shares,
                total_market_value=total_value['market_value'],
                current_price=total_value['market_value'] / total_shares,
                market_price=market_price,
                total_weight=holdings_data.total_weight,
                fair_value_lo=total_value['fair_value_lo'] / total_shares,
                fair_value_hi=total_value['fair_value_hi'] / total_shares,
                forward_next_fy_lo=total_value['forward_next_fy_lo'] / total_shares,
                forward_next_fy_hi=total_value['forward_next_fy_hi'] / total_shares,
                forward_stocks_value_lo=forward_stocks_value_lo,
                forward_stocks_value_hi=forward_stocks_value_hi,
                forward_stocks_fy_lo=forward_stocks_fy_lo,
                forward_stocks_fy_hi=forward_stocks_fy_hi,
                forward_stocks_weight=forward_stocks_weight,
                min_fair_value_date=forward_stocks['min_fair_value_date'],
                max_fair_value_date=forward_stocks['max_fair_value_date'],
                leveraged_symbol=LEVERAGED_ETF_MAP[etf_symbol][0] if etf_symbol in LEVERAGED_ETF_MAP else None,
                leveraged_price=szdt_info['price'] if szdt_info else None,
                leveraged_szdt_score=szdt_info['score'] if szdt_info else None,
                leveraged_szdt_update_time=szdt_info['time'] if szdt_info else None,
                eps=weighted_eps,
                eps_forward=weighted_eps_forward,
                eps_v2=weighted_eps_v2,
                eps_ttm=weighted_eps_ttm,
                created_at=datetime.now(),
                updated_at=datetime.now()
            )
            
            self.db_session.merge(analysis)
            self.db_session.commit()
            
        except Exception as e:
            self.db_session.rollback()
            self.logger.error(f"分析ETF {etf_symbol} 失败: {str(e)}\n{traceback.format_exc()}")
            raise 

    def analyze_all_fair_value(self, etf_symbols=None):
        """分析所有支持的ETF"""
        try:
            symbols = list(self.fetchers.keys())
            if etf_symbols:
                requested = [str(symbol or "").strip().upper() for symbol in etf_symbols if str(symbol or "").strip()]
                symbols = [symbol for symbol in requested if symbol in self.fetchers]
                unsupported = [symbol for symbol in requested if symbol not in self.fetchers]
                if unsupported:
                    self.logger.warning("跳过不支持的ETF估值分析标的: %s", ",".join(unsupported))
                if not symbols:
                    raise ValueError("没有可分析的ETF标的")
            for etf_symbol in symbols:
                try:
                    self.analyze_etf(etf_symbol)
                    self.logger.info(f"完成ETF {etf_symbol} 分析")
                except Exception as e:
                    self.logger.error(f"分析ETF {etf_symbol} 失败: {str(e)}")
        except Exception as e:
            self.logger.error(f"执行ETF分析任务失败: {str(e)}")
            raise

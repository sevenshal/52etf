from datetime import datetime, date
from typing import Dict, Optional, List
from sqlalchemy.orm import Session
from ..core.database import ETFHolding, ETFAnalysis, StockEVC, Session, ETFEmotion
from ..core.models.etf import ETFHoldingsData
from .etf.base import ETFDataFetcher
from .etf.ishares import ISharesETFFetcher
from .etf.qqq import QQQDataFetcher
from .etf.spdr import SPDRDataFetcher
from .etf.vanguard import VanguardETFFetcher
import logging
from ..core.services.longport import LongPortService
from ..core.services.quote import QuoteService, QuoteProvider
from ..core.services.szdt import SZDTService
from ..emotion.etf_emotion import ETFEmotionCalculator
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
        self.calculator = ETFEmotionCalculator(self.quote_service)
        ishares_fetcher = ISharesETFFetcher()
        spdr_fetcher = SPDRDataFetcher()
        vanguard_fetcher = VanguardETFFetcher()  # 需要新增 Vanguard ETF 抓取器
        self.fetchers: Dict[str, ETFDataFetcher] = {
            # 现有的 ETF
            'SOXX.US': ishares_fetcher,
            'IWM.US': ishares_fetcher,
            'ITB.US': ishares_fetcher,
            'ITA.US': ishares_fetcher,
            'SPY.US': spdr_fetcher,
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
            return self.db_session.query(StockEVC).filter(
                StockEVC.symbol == symbol,
                StockEVC.date == today
            ).first()
        except Exception as e:
            self.logger.error(f"获取股票 {symbol} 的估值信息失败: {str(e)}")
            return None
    
    def get_total_shares(self, etf_symbol: str) -> int:
        """获取ETF的总发行股数"""
        try:
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

            self.db_session.query(ETFHolding).filter(ETFHolding.etf_symbol == etf_symbol and ETFHolding.date == holdings_data.update_date).delete()
            # 保存到数据库
            for holding in holdings_data.holdings:
                db_holding = ETFHolding(
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
            
        except Exception as e:
            self.db_session.rollback()
            raise
    
    def analyze_etf(self, etf_symbol: str):
        """分析ETF的估值情况"""
        try:
            # 获取最新持仓数据
            holdings_data = self.update_holdings(etf_symbol)
            
            # 使用 抓取的总股本数，没有则使用 LongPort API 获取总股数
            total_shares = self.get_total_shares(etf_symbol) if holdings_data.total_shares is None else holdings_data.total_shares
            
            # 批量获取所有持仓股票的 static_info
            equity_symbols = [holding.symbol for holding in holdings_data.holdings if holding.asset_class == 'Equity' and holding.symbol]
            static_infos = self.quote_service.get_static_info(equity_symbols) if equity_symbols else []
            static_info_map = {info.get('symbol'): info for info in static_infos}

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
                            if evc_info.last_price is not None:
                                price = float(evc_info.last_price)
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
                name=self.fetchers[etf_symbol].name,
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

    def analyze_all_fair_value(self):
        """分析所有支持的ETF"""
        try:
            for etf_symbol in self.fetchers.keys():
                try:
                    self.analyze_etf(etf_symbol)
                    self.logger.info(f"完成ETF {etf_symbol} 分析")
                except Exception as e:
                    self.logger.error(f"分析ETF {etf_symbol} 失败: {str(e)}")
        except Exception as e:
            self.logger.error(f"执行ETF分析任务失败: {str(e)}")
            raise

    def calculate_all_emotions(self, date: Optional[date] = None):
        """计算所有ETF的情绪指标"""
        # 获取最近一个交易日日期
        if date is None:
            last_trading_day = self.quote_service.get_klines('SPY.US', 1)
            if last_trading_day:
                date = last_trading_day[0]['timestamp'].date()
            else:
                date = date.today()
            
        for etf_symbol in self.fetchers.keys():
            try:
                self.calculate_emotion(etf_symbol, date)
                self.logger.info(f"完成ETF {etf_symbol} {date} 情绪指标计算")
            except Exception as e:
                self.logger.error(f"计算ETF {etf_symbol} {date} 情绪指标失败: {str(e)}")
        self.calculator.clear_cache()

    def calculate_emotion(self, etf_symbol: str, date: Optional[date] = None):
        """计算并存储指定日期的ETF情绪指标"""
        try:
            if date is None:
                # 获取最近一个交易日日期
                last_trading_day = self.quote_service.get_klines(etf_symbol, 1)
                if last_trading_day:
                    date = last_trading_day[0]['timestamp'].date()
                else:
                    date = date.today()

            emotion = self.calculator.calculate(etf_symbol, date)
            
            # 创建情绪指数记录
            emotion_record = ETFEmotion(
                symbol=emotion.symbol,
                date=emotion.date,
                score=emotion.score,
                momentum_score=emotion.indicators[0].value,
                strength_score=emotion.indicators[1].value,
                breadth_score=emotion.indicators[2].value,
                volatility_score=emotion.indicators[3].value,
                rsi_score=emotion.indicators[4].value,
                created_at=datetime.now(),
                updated_at=datetime.now()
            )
            
            # 保存记录
            self.db_session.merge(emotion_record)
            self.db_session.commit()
            self.logger.info(f"成功计算并存储 {etf_symbol} 的当日情绪指标")
            
        except Exception as e:
            self.db_session.rollback()
            self.logger.error(f"计算 {etf_symbol} {date} 情绪指标失败: {str(e)}")
            raise

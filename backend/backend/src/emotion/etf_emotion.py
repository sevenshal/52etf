from typing import List, Dict, Optional
from datetime import date
import numpy as np
import pandas as pd
from dataclasses import dataclass
from ..core.database import ETFHolding, Session
from sqlalchemy.sql import func
from ..core.services.quote import QuoteService

@dataclass
class EmotionIndicator:
    """情绪指标"""
    name: str                 # 指标名称
    value: float             # 指标值 (0-100)
    signal: str              # 信号：EXTREME_FEAR, FEAR, NEUTRAL, GREED, EXTREME_GREED
    description: str         # 指标描述
    raw_value: float        # 原始值
    
@dataclass
class ETFEmotion:
    """ETF情绪指数"""
    symbol: str              # ETF代码
    date: date          # 计算日期
    score: float            # 综合得分 (0-100)
    signal: str             # 信号
    indicators: List[EmotionIndicator]  # 各项指标

class ETFEmotionCalculator:
    """ETF情绪指数计算器"""
    
    def __init__(self, quote_service: QuoteService):
        """
        初始化计算器
        
        Args:
            quote_service: 行情数据服务
            db_session: 数据库会话
        """
        self.quote_service = quote_service
        self.db_session = Session()
        self.klines_cache = {}  # 用于缓存K线数据
        
    def clear_cache(self):
        """清除缓存"""
        self.klines_cache.clear()

    def _get_klines(self, symbol: str, count: int, date: Optional[date] = None) -> List[dict]:
        """获取K线数据，使用缓存避免重复请求
        
        Args:
            symbol: 股票代码
            count: 需要的K线数量
            date: 结束日期,默认为当前日期
        Returns:
            List[dict]: K线数据列表
        """
        if not '.' in symbol:
            return []
        cache_key = f"{symbol}_{date}"
        if cache_key not in self.klines_cache:
            self.klines_cache[cache_key] = self.quote_service.get_klines(symbol, count, date)
        return self.klines_cache[cache_key][-count:]
    
    def _prepare_klines_data(self, etf_symbol: str, holdings: List[ETFHolding], date: Optional[date] = None):
        """预先获取所有需要的K线数据
        
        Args:
            etf_symbol: ETF代码
            holdings: ETF持仓列表
        """
        
        # 获取ETF的K线数据 (取最长的一个周期，这里是250天)
        self._get_klines(etf_symbol, 250, date)
        
        # 获取所有成分股的K线数据
        for holding in holdings:
            # 获取365天的数据(用于计算52周高低点)
            self._get_klines(holding.symbol, 365, date)
    
    def _calculate_momentum(self, symbol: str, date: Optional[date] = None) -> EmotionIndicator:
        """计算动量指标
        
        使用价格与125日移动平均线的关系来衡量动量，通过标准差来衡量偏离程度
        """
        try:
            klines = self._get_klines(symbol, 250, date)
            
            if not klines:
                return EmotionIndicator(
                    name="Market Momentum",
                    value=50,
                    signal="NEUTRAL",
                    description="无法获取数据",
                    raw_value=0
                )
            
            # 转换为DataFrame
            data = pd.DataFrame(klines)
            
            # 计算125日移动平均线
            data['ma125'] = data['close'].rolling(window=125).mean()
            
            # 计算价格相对MA的偏离度序列
            data['deviation'] = (data['close'] - data['ma125']) / data['ma125']
            
            # 计算偏离度的标准差
            deviation_std = data['deviation'].std()

            # 计算偏离度的均值
            deviation_mean = data['deviation'].mean()

            # 获取当前偏离度
            latest = data.iloc[-1]
            current_deviation = (latest['close'] - latest['ma125']) / latest['ma125']
            
            # 用标准差的倍数来衡量当前偏离程度
            std_multiple = (current_deviation - deviation_mean) / deviation_std if deviation_std != 0 else 0
            
            # 将标准差倍数映射到0-100的分数
            # 假设正负2个标准差对应极端情况
            score = 50 * (1 + min(max(std_multiple / 2, -1), 1))
            
            # 确定信号和描述
            if score >= 80:
                signal = "EXTREME_GREED"
                desc = "价格显著高于均线"
            elif score >= 60:
                signal = "GREED"
                desc = "价格高于均线"
            elif score >= 40:
                signal = "NEUTRAL"
                desc = "价格接近均线"
            elif score >= 20:
                signal = "FEAR"
                desc = "价格低于均线"
            else:
                signal = "EXTREME_FEAR"
                desc = "价格显著低于均线"
            
            return EmotionIndicator(
                name="Market Momentum",
                value=score,
                signal=signal,
                description=f"{desc} (偏离{std_multiple:+.1f}个标准差)",
                raw_value=current_deviation
            )
            
        except Exception as e:
            raise Exception(f"计算动量指标失败: {str(e)}")
    
    def _get_latest_holdings(self, symbol: str, date: date) -> List[ETFHolding]:
        """获取ETF最新的持仓数据，只返回股票类型的持仓
        
        Args:
            symbol: ETF代码
            date: 查询日期
            
        Returns:
            List[ETFHolding]: ETF持仓列表
        """
        try:
            # 先获取小于等于指定日期的最新持仓日期
            latest_date = self.db_session.query(
                func.max(ETFHolding.date)
            ).filter(
                ETFHolding.etf_symbol == symbol,
                ETFHolding.date <= date
            ).scalar()
            
            if not latest_date:
                return []
            
            # 获取该日期的持仓数据，只获取股票类型的资产
            holdings = self.db_session.query(ETFHolding).filter(
                ETFHolding.etf_symbol == symbol,
                ETFHolding.date == latest_date,
                ETFHolding.asset_class == 'Equity'  # 只获取股票类型的资产
            ).all()
            
            return holdings
            
        except Exception as e:
            self.logger.error(f"获取ETF {symbol} 持仓数据失败: {str(e)}")
            return []
    
    def _calculate_strength(self, symbol: str, holdings: List[ETFHolding], date: Optional[date] = None) -> EmotionIndicator:
        """计算强度指标
        
        通过比较成分股的52周新高和新低数量来衡量市场强度：
        1. 获取ETF成分股列表
        2. 计算每个成分股相对于52周高点和低点的位置
        3. 统计处于高位和低位的股票数量
        4. 计算强度得分
        """
        try:
            if not holdings:
                return EmotionIndicator(
                    name="Market Strength",
                    value=50,
                    signal="NEUTRAL",
                    description="无法获取成分股数据",
                    raw_value=0
                )
            
            high_count = 0  # 接近52周高点的股票数
            low_count = 0   # 接近52周低点的股票数
            valid_stocks = 0  # 有效的股票数量
            
            for holding in holdings:
                klines = self._get_klines(holding.symbol, 365, date)
                
                if not klines:
                    continue
                
                # 计算52周高点和低点
                high_52w = max(kline['high'] for kline in klines)
                low_52w = min(kline['low'] for kline in klines)
                current_price = klines[-1]['close']
                
                # 计算当前价格在52周范围内的位置 (0-1)
                price_position = (current_price - low_52w) / (high_52w - low_52w) if high_52w != low_52w else 0.5
                
                # 判断是否接近高点或低点（使用10%的阈值）
                if price_position >= 0.9:  # 接近52周高点
                    high_count += 1
                elif price_position <= 0.1:  # 接近52周低点
                    low_count += 1
                    
                valid_stocks += 1
            
            if valid_stocks == 0:
                return EmotionIndicator(
                    name="Market Strength",
                    value=50,
                    signal="NEUTRAL",
                    description="无有效成分股数据",
                    raw_value=0
                )
            
            # 计算高点和低点的比率
            high_ratio = high_count / valid_stocks
            low_ratio = low_count / valid_stocks
            
            # 计算净强度比率 (-1 到 1)
            net_strength = (high_ratio - low_ratio)
            
            # 将净强度映射到0-100的分数
            score = 50 * (1 + net_strength)
            
            # 确定信号和描述
            if score >= 80:
                signal = "EXTREME_GREED"
                desc = "大量股票接近52周高点"
            elif score >= 60:
                signal = "GREED"
                desc = "较多股票接近52周高点"
            elif score >= 40:
                signal = "NEUTRAL"
                desc = "股票分布均衡"
            elif score >= 20:
                signal = "FEAR"
                desc = "较多股票接近52周低点"
            else:
                signal = "EXTREME_FEAR"
                desc = "大量股票接近52周低点"
            
            return EmotionIndicator(
                name="Market Strength",
                value=score,
                signal=signal,
                description=f"{desc} (高点: {high_ratio:.1%}, 低点: {low_ratio:.1%})",
                raw_value=net_strength
            )
            
        except Exception as e:
            raise Exception(f"计算强度指标失败: {str(e)}")
    
    def _calculate_breadth(self, symbol: str, holdings: List[ETFHolding], date: Optional[date] = None) -> EmotionIndicator:
        """计算广度指标"""
        try:
            if not holdings:
                return EmotionIndicator(
                    name="Market Breadth",
                    value=50,
                    signal="NEUTRAL",
                    description="无法获取成分股数据",
                    raw_value=0
                )
            
            up_volume = 0
            down_volume = 0
            
            for holding in holdings:
                klines = self._get_klines(holding.symbol, 5, date)
                
                if not klines or len(klines) < 2:
                    continue
                
                # 获取最新一天的数据
                latest = klines[-1]
                prev = klines[-2]
                
                # 根据涨跌分别累加成交量
                if latest['close'] > prev['close']:
                    up_volume += latest['volume']
                elif latest['close'] < prev['close']:
                    down_volume += latest['volume']
            
            total_volume = up_volume + down_volume
            if total_volume == 0:
                return EmotionIndicator(
                    name="Market Breadth",
                    value=50,
                    signal="NEUTRAL",
                    description="无有效成交量数据",
                    raw_value=0
                )
            
            # 计算上涨成交量比率
            up_ratio = up_volume / total_volume
            
            # 将比率映射到0-100的分数
            score = up_ratio * 100
            
            # 确定信号和描述
            if score >= 80:
                signal = "EXTREME_GREED"
                desc = "上涨股票成交量占绝对优势"
            elif score >= 60:
                signal = "GREED"
                desc = "上涨股票成交量较大"
            elif score >= 40:
                signal = "NEUTRAL"
                desc = "成交量分布均衡"
            elif score >= 20:
                signal = "FEAR"
                desc = "下跌股票成交量较大"
            else:
                signal = "EXTREME_FEAR"
                desc = "下跌股票成交量占绝对优势"
            
            return EmotionIndicator(
                name="Market Breadth",
                value=score,
                signal=signal,
                description=f"{desc} (上涨成交量占比: {up_ratio:.1%})",
                raw_value=up_ratio
            )
            
        except Exception as e:
            raise Exception(f"计算广度指标失败: {str(e)}")
    
    def _calculate_volatility(self, symbol: str, date: Optional[date] = None) -> EmotionIndicator:
        """计算波动率指标"""
        try:
            # 获取最近的交易数据
            
            klines = self._get_klines(symbol, 60, date)
            
            if not klines:
                return EmotionIndicator(
                    name="Volatility",
                    value=50,
                    signal="NEUTRAL",
                    description="无法获取数据",
                    raw_value=0
                )
            
            # 转换为DataFrame
            data = pd.DataFrame(klines)
            
            # 计算日收益率
            data['returns'] = data['close'].pct_change()
            
            # 计算波动率（年化）
            volatility = data['returns'].std() * np.sqrt(252)
            
            # 计算波动率的移动平均
            vol_ma = data['returns'].rolling(window=50).std().iloc[-1] * np.sqrt(252)
            
            # 计算得分
            ratio = (volatility - vol_ma) / vol_ma
            score = 50 * (1 - min(max(ratio / 0.5, -1), 1))  # 波动率高表示恐慌
            
            # 确定信号
            if score >= 80:
                signal = "EXTREME_GREED"
                desc = "市场波动率极低"
            elif score >= 60:
                signal = "GREED"
                desc = "市场波动率较低"
            elif score >= 40:
                signal = "NEUTRAL"
                desc = "市场波动率正常"
            elif score >= 20:
                signal = "FEAR"
                desc = "市场波动率较高"
            else:
                signal = "EXTREME_FEAR"
                desc = "市场波动率极高"
            
            return EmotionIndicator(
                name="Volatility",
                value=score,
                signal=signal,
                description=f"{desc} (波动率: {volatility:.1%}, 相对均值: {ratio:+.1%})",
                raw_value=volatility
            )
            
        except Exception as e:
            raise Exception(f"计算波动率指标失败: {str(e)}")
    
    def _calculate_rsi(self, symbol: str, date: Optional[date] = None, period: int = 14) -> EmotionIndicator:
        """计算RSI指标
        
        Args:
            symbol: ETF代码
            date: 计算日期
            period: RSI周期，默认14天
        """
        try:
            # 获取数据，多获取一些以确保有足够数据计算RSI
            klines = self._get_klines(symbol, period * 3, date)
            
            if not klines:
                return EmotionIndicator(
                    name="RSI",
                    value=50,
                    signal="NEUTRAL",
                    description="无法获取数据",
                    raw_value=50
                )
            
            # 转换为DataFrame
            data = pd.DataFrame(klines)
            
            # 计算价格变化
            data['price_change'] = data['close'].diff()
            
            # 计算上涨和下跌
            data['gain'] = data['price_change'].clip(lower=0)
            data['loss'] = -data['price_change'].clip(upper=0)
            
            # Wilder's RSI 计算方法
            # 1. 计算初始平均值（使用SMA）
            avg_gains = []
            avg_losses = []
            
            # 第一个值使用简单平均
            first_avg_gain = data['gain'].iloc[:period].mean()
            first_avg_loss = data['loss'].iloc[:period].mean()
            
            avg_gains.append(first_avg_gain)
            avg_losses.append(first_avg_loss)
            
            # 2. 使用 Wilder's 方法计算后续值
            for i in range(period, len(data)):
                avg_gain = (avg_gains[-1] * (period-1) + data['gain'].iloc[i]) / period
                avg_loss = (avg_losses[-1] * (period-1) + data['loss'].iloc[i]) / period
                avg_gains.append(avg_gain)
                avg_losses.append(avg_loss)
            
            # 3. 计算 RS 和 RSI
            rs = [g/l if l != 0 else float('inf') for g, l in zip(avg_gains, avg_losses)]
            rsi = [100 - (100 / (1 + r)) if r != float('inf') else 100 for r in rs]
            
            # 获取最新的RSI值
            latest_rsi = rsi[-1]
            
            # RSI的标准解读：
            # 70以上：超买
            # 30以下：超卖
            # 50为中性
            
            # 将RSI映射到我们的评分系统
            score = latest_rsi
            
            # 确定信号
            if score >= 80:
                signal = "EXTREME_GREED"
                desc = "市场严重超买"
            elif score >= 60:
                signal = "GREED"
                desc = "市场超买"
            elif score >= 40:
                signal = "NEUTRAL"
                desc = "市场中性"
            elif score >= 20:
                signal = "FEAR"
                desc = "市场超卖"
            else:
                signal = "EXTREME_FEAR"
                desc = "市场严重超卖"
            
            return EmotionIndicator(
                name="RSI",
                value=score,
                signal=signal,
                description=f"{desc} (RSI: {latest_rsi:.1f})",
                raw_value=latest_rsi
            )
            
        except Exception as e:
            raise Exception(f"计算RSI指标失败: {str(e)}")
    
    def calculate(self, symbol: str, date: Optional[date] = None) -> ETFEmotion:
        """计算ETF的情绪指数
        
        Args:
            symbol: ETF代码
            date: 计算日期，默认为当前日期
            
        Returns:
            ETFEmotion: ETF情绪指数
        """
        if date is None:
            date = date.today()
        
        # 先获取最新持仓数据
        holdings = self._get_latest_holdings(symbol, date.today())
        
        # 预先获取所有需要的K线数据
        self._prepare_klines_data(symbol, holdings, date)
        
        # 计算各项指标
        indicators = [
            self._calculate_momentum(symbol, date),
            self._calculate_strength(symbol, holdings, date),
            self._calculate_breadth(symbol, holdings, date),
            self._calculate_volatility(symbol, date),
            self._calculate_rsi(symbol, date=date, period=6)
        ]
        
        # 计算综合得分（简单平均）
        score = sum(ind.value for ind in indicators) / len(indicators)
        
        # 确定综合信号
        if score >= 80:
            signal = "EXTREME_GREED"
        elif score >= 60:
            signal = "GREED"
        elif score >= 40:
            signal = "NEUTRAL"
        elif score >= 20:
            signal = "FEAR"
        else:
            signal = "EXTREME_FEAR"
        
        return ETFEmotion(
            symbol=symbol,
            date=date,
            score=score,
            signal=signal,
            indicators=indicators
        ) 

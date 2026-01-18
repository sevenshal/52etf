from typing import List, Dict, Tuple, Optional, Callable
import asyncio
from datetime import datetime
import pandas as pd
import numpy as np
from ..core.services.szdt import SZDTService
from ..core.database import SzdtTradeStock
import logging
from dataclasses import dataclass

logger = logging.getLogger('backtest')

@dataclass
class ETFParams:
    """ETF参数配置"""
    max_position_ratio: float  # 最大持仓比例
    trade_amount: float       # 交易金额
    buy_score: int           # 买入阈值
    sell_score: int          # 卖出阈值

    def to_dict(self) -> Dict:
        """转换为字典格式"""
        return {
            'max_position_ratio': float(self.max_position_ratio),
            'trade_amount': float(self.trade_amount),
            'buy_score': int(self.buy_score),
            'sell_score': int(self.sell_score)
        }

class ETFBacktest:
    def __init__(
        self,
        initial_cash: float = 1000000,
        max_position_range: Tuple[float, float, float] = (0, 1, 0.01),  # (min, max, step)
        trade_amount_range: Tuple[float, float, float] = (10000, 100000, 10000),  # (min, max, step)
        buy_score_range: Tuple[int, int, int] = (-100, -50, 5),  # (min, max, step)
        sell_score_range: Tuple[int, int, int] = (50, 100, 5),  # (min, max, step)
        start_date: Optional[str] = None,  # 格式：YYYY-MM-DD
        end_date: Optional[str] = None,  # 格式：YYYY-MM-DD
        progress_callback: Optional[Callable[[float], None]] = None
    ):
        self.szdt_service = SZDTService()
        self.initial_cash = initial_cash
        self.max_position_range = max_position_range
        self.trade_amount_range = trade_amount_range
        self.buy_score_range = buy_score_range
        self.sell_score_range = sell_score_range
        self.start_date = pd.to_datetime(start_date) if start_date else None
        self.end_date = pd.to_datetime(end_date) if end_date else None
        self.progress_callback = progress_callback
        logger.info(f"初始化ETFBacktest: 初始资金={initial_cash}, 开始日期={start_date}, 结束日期={end_date}")

    async def fetch_etf_list(self, etf_type: int = 1) -> List[Dict]:
        """获取ETF列表"""
        response = await self.szdt_service.get_etf_emotion(etf_type)
        if not response or response.get('status') != 1:
            logger.error(f"获取ETF列表失败: {response}")
            return []
        return response.get('data', [])

    async def fetch_etf_history(self, code: str) -> List[Dict]:
        """获取ETF历史贪恐指数"""
        response = await self.szdt_service.get_etf_emotion_history(code)
        if not response or response.get('status') != 1:
            logger.error(f"获取ETF {code} 历史数据失败: {response}")
            return []
        
        history = response.get('data', [])
        
        # 如果设置了日期范围，过滤数据
        if self.start_date or self.end_date:
            filtered_history = []
            for item in history:
                date = pd.to_datetime(item['date'])
                if (not self.start_date or date >= self.start_date) and \
                   (not self.end_date or date <= self.end_date):
                    filtered_history.append(item)
            return filtered_history
        
        return history

    def calculate_score_factor(self, score: float, buy_score: int, sell_score: int, is_buy: bool) -> float:
        """计算分数因子，用于调整买入/卖出金额"""
        if is_buy:
            # 买入时：score越低，因子越大
            score_factor = min(1, max(0, (buy_score - score) / (buy_score + 100)))
        else:
            # 卖出时：score越高，因子越大
            score_factor = min(1, max(0, (score - sell_score) / (100 - sell_score)))
        return 3 ** (score_factor ** 2)

    def backtest_portfolio(
        self,
        etf_histories: Dict[str, List[Dict]],
        etf_params: Dict[str, ETFParams]
    ) -> Dict:
        """对ETF组合进行回测"""
        if not etf_histories:
            return {
                'total_return': 0,
                'max_drawdown': 0,
                'sharpe_ratio': 0,
                'trades': [],
                'start_date': self.start_date.strftime('%Y-%m-%d') if self.start_date else None,
                'end_date': self.end_date.strftime('%Y-%m-%d') if self.end_date else None
            }

        logger.info(f"开始回测 - 开始日期: {self.start_date}, 结束日期: {self.end_date}, 回测ETF数量: {len(etf_histories)}, 回测参数: {etf_params}")

        # 准备数据
        dfs = {}
        for code, history in etf_histories.items():
            df = pd.DataFrame(history)
            df['date'] = pd.to_datetime(df['date'])
            df['price'] = df['price'].astype(float)
            df = df.sort_values('date')
            dfs[code] = df

        # 获取所有日期
        all_dates = sorted(set().union(*[set(df['date']) for df in dfs.values()]))
        
        # 如果设置了日期范围，过滤日期
        if self.start_date or self.end_date:
            all_dates = [date for date in all_dates if 
                        (not self.start_date or date >= self.start_date) and 
                        (not self.end_date or date <= self.end_date)]

        # 初始化回测状态
        cash = self.initial_cash
        positions = {code: 0 for code in etf_histories.keys()}
        trades = []
        portfolio_values = []
        dates = []
        
        # 初始化每个ETF的统计信息
        etf_stats = {
            code: {
                'buy_count': 0,
                'sell_count': 0,
                'total_buy_amount': 0.0,
                'total_sell_amount': 0.0,
                'current_position_value': 0.0,
                'total_profit': 0.0
            } for code in etf_histories.keys()
        }

        # 按日期遍历
        for date in all_dates:
            # 计算当前总资产
            total_position_value = 0
            for code, df in dfs.items():
                if date in df['date'].values:
                    price = df[df['date'] == date]['price'].iloc[0]
                    total_position_value += positions[code] * price
            
            portfolio_value = cash + total_position_value
            portfolio_values.append(portfolio_value)
            dates.append(date)

            # 处理每个ETF
            for code, df in dfs.items():
                if date not in df['date'].values:
                    continue

                row = df[df['date'] == date].iloc[0]
                score = row['score']
                price = row['price']
                position_value = positions[code] * price
                position_ratio = position_value / portfolio_value
                params = etf_params[code]

                # 买入逻辑
                if score <= params.buy_score and position_ratio < params.max_position_ratio:
                    score_factor = self.calculate_score_factor(score, params.buy_score, params.sell_score, True)
                    buy_amount = min(
                        cash,
                        params.trade_amount * score_factor
                    )
                    buy_quantity = int(buy_amount / price / 100) * 100
                    if buy_quantity >= 100:
                        cost = buy_quantity * price
                        cash -= cost
                        positions[code] += buy_quantity
                        trades.append({
                            'date': date.to_pydatetime().date(),
                            'code': code,
                            'action': 'BUY',
                            'quantity': int(buy_quantity),
                            'price': float(price),
                            'score': float(score),
                            'amount': float(cost)
                        })
                        # 更新ETF统计信息
                        etf_stats[code]['buy_count'] += 1
                        etf_stats[code]['total_buy_amount'] += cost
                        logger.info(f"买入: {code}, 日期: {date}, 数量: {buy_quantity}, 价格: {price}, 金额: {cost}")

                # 卖出逻辑
                elif score >= params.sell_score and positions[code] > 0:
                    score_factor = self.calculate_score_factor(score, params.buy_score, params.sell_score, False)
                    sell_amount = params.trade_amount * score_factor
                    sell_quantity = int(sell_amount / price / 100) * 100
                    if sell_quantity > positions[code]:
                        sell_quantity = positions[code]
                    if sell_quantity >= 100:
                        revenue = sell_quantity * price
                        cash += revenue
                        positions[code] -= sell_quantity
                        trades.append({
                            'date': date.to_pydatetime().date(),
                            'code': code,
                            'action': 'SELL',
                            'quantity': int(sell_quantity),
                            'price': float(price),
                            'score': float(score),
                            'amount': float(revenue)
                        })
                        # 更新ETF统计信息
                        etf_stats[code]['sell_count'] += 1
                        etf_stats[code]['total_sell_amount'] += revenue
                        logger.info(f"卖出: {code}, 日期: {date}, 数量: {sell_quantity}, 价格: {price}, 金额: {revenue}")

        # 计算每个ETF的最终持仓价值和总收益
        for code, df in dfs.items():
            if len(df) > 0:
                last_price = df.iloc[-1]['price']
                current_position_value = positions[code] * last_price
                etf_stats[code]['current_position_value'] = current_position_value
                etf_stats[code]['total_profit'] = (
                    current_position_value + 
                    etf_stats[code]['total_sell_amount'] - 
                    etf_stats[code]['total_buy_amount']
                )

        # 计算回测指标
        portfolio_values = pd.Series(portfolio_values, index=dates)
        returns = portfolio_values.pct_change().dropna()
        
        total_return = float((portfolio_values.iloc[-1] / self.initial_cash - 1) * 100)
        max_drawdown = float(((portfolio_values / portfolio_values.cummax() - 1) * 100).min())
        sharpe_ratio = float(np.sqrt(252) * returns.mean() / returns.std() if len(returns) > 0 else 0)

        # 转换 portfolio_values 为普通字典
        portfolio_values_dict = {
            date.to_pydatetime(): float(value) 
            for date, value in portfolio_values.items()
        }

        return {
            'total_return': total_return,
            'max_drawdown': max_drawdown,
            'sharpe_ratio': sharpe_ratio,
            'trades': trades,
            'final_positions': {
                code: {
                    'position': int(positions[code]),
                    'buy_count': stats['buy_count'],
                    'sell_count': stats['sell_count'],
                    'total_buy_amount': float(stats['total_buy_amount']),
                    'total_sell_amount': float(stats['total_sell_amount']),
                    'current_position_value': float(stats['current_position_value']),
                    'total_profit': float(stats['total_profit'])
                } for code, stats in etf_stats.items()
            },
            'start_date': self.start_date.strftime('%Y-%m-%d') if self.start_date else None,
            'end_date': self.end_date.strftime('%Y-%m-%d') if self.end_date else None,
            'portfolio_values': portfolio_values_dict
        }

    async def find_best_parameters(self, etf_list: List[Dict]) -> Dict:
        """找到最佳参数配置"""
        try:
            logger.info(f"开始寻找最优参数 - ETF数量: {len(etf_list)}")
            
            # 创建ETF代码到名称的映射
            etf_names = {etf['code']: etf['name'] for etf in etf_list}
            
            # 获取所有ETF的历史数据
            etf_histories = {}
            for etf in etf_list:
                code = etf['code']
                logger.info(f"获取ETF历史数据: {code}")
                history = await self.fetch_etf_history(code)
                if history:
                    etf_histories[code] = history
                    logger.info(f"成功获取 {code} 的历史数据 {len(history)} 条")
                else:
                    logger.warning(f"获取 {code} 的历史数据失败")
            
            logger.info(f"最终参与回测的ETF数量: {len(etf_histories)}")
            
            # 参数网格搜索
            best_config = {
                'total_return': float('-1e10'),  # 使用一个足够小的数
                'max_drawdown': float('1e10'),   # 使用一个足够大的数
                'sharpe_ratio': float('-1e10'),  # 使用一个足够小的数
                'parameters': {}
            }

            # 计算总参数组合数
            etf_codes = list(etf_histories.keys())
            max_positions = np.arange(*self.max_position_range)
            trade_amounts = np.arange(*self.trade_amount_range)
            buy_scores = np.arange(*self.buy_score_range)
            sell_scores = np.arange(*self.sell_score_range)
            
            logger.info(f"max_positions: {max_positions}, trade_amounts: {trade_amounts}, buy_scores: {buy_scores}, sell_scores: {sell_scores}")

            total_combinations = (
                len(max_positions) ** len(etf_codes) *
                len(trade_amounts) ** len(etf_codes) *
                len(buy_scores) ** len(etf_codes) *
                len(sell_scores) ** len(etf_codes)
            )
            if total_combinations == 0:
                raise ValueError("参数组合数为0，请检查参数范围")
            if total_combinations > 100000:
                raise ValueError(f"参数组合数{total_combinations}超过100000，资源不足")
            current_combination = 0
            logger.info(f"开始生成参数组合，组合数{total_combinations}")

            # 生成所有可能的参数组合
            for max_positions_combo in self._generate_parameter_combinations(max_positions, len(etf_codes)):
                for trade_amounts_combo in self._generate_parameter_combinations(trade_amounts, len(etf_codes)):
                    for buy_scores_combo in self._generate_parameter_combinations(buy_scores, len(etf_codes)):
                        for sell_scores_combo in self._generate_parameter_combinations(sell_scores, len(etf_codes)):
                            # 更新进度
                            current_combination += 1
                            if self.progress_callback:
                                progress = (current_combination / total_combinations) * 100
                                self.progress_callback(progress)
                            logger.info(f"生成参数组合{current_combination}/{total_combinations}")

                            # 构建参数映射
                            etf_params = {
                                code: ETFParams(
                                    max_position_ratio=max_pos,
                                    trade_amount=trade_amount,
                                    buy_score=int(buy_score),
                                    sell_score=int(sell_score)
                                )
                                for code, max_pos, trade_amount, buy_score, sell_score in zip(
                                    etf_codes,
                                    max_positions_combo,
                                    trade_amounts_combo,
                                    buy_scores_combo,
                                    sell_scores_combo
                                )
                            }

                            # 执行回测
                            result = self.backtest_portfolio(
                                etf_histories,
                                etf_params
                            )

                            # 更新最佳配置
                            if result['total_return'] > best_config['total_return']:
                                best_config = {
                                    'total_return': result['total_return'],
                                    'max_drawdown': result['max_drawdown'],
                                    'sharpe_ratio': result['sharpe_ratio'],
                                    'parameters': {k: {'name': etf_names.get(k, ''), **v.to_dict()} for k, v in etf_params.items()},
                                    'trades': result['trades'],
                                    'final_positions': result['final_positions']
                                }

            return best_config
        except Exception as e:
            logger.error(f"寻找最优参数时发生错误: {str(e)}", exc_info=True)
            raise

    def _generate_parameter_combinations(self, values: np.ndarray, n_etfs: int) -> List[List[float]]:
        """生成参数组合"""
        if n_etfs == 1:
            return [[v] for v in values]
        
        combinations = []
        for v in values:
            sub_combinations = self._generate_parameter_combinations(values, n_etfs - 1)
            combinations.extend([[v] + combo for combo in sub_combinations])
        return combinations

async def main():
    backtest = ETFBacktest()
    best_config = await backtest.find_best_parameters()
    print("最佳配置:", best_config)

if __name__ == "__main__":
    asyncio.run(main())
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import json
from datetime import datetime

# 读取数据
with open('soxl_data.json', 'r') as f:
    data = json.load(f)['data']

# 转换为DataFrame
df = pd.DataFrame(data)
df['date'] = pd.to_datetime(df['date'])
df['price'] = df['price'].astype(float)
df = df.sort_values('date')

class SOXLBacktest:
    def __init__(self, data, initial_capital=100000):
        self.df = data
        self.capital = initial_capital
        self.position = 0
        self.position_history = []
        self.capital_history = []
        self.trades = []
        self.last_buy_price = 0
        self.buy_prices = []  # 记录所有买入价格
        self.shares = 0  # 持有的股票数量
        
    def run(self):
        for i in range(1, len(self.df)):
            current_row = self.df.iloc[i]
            prev_row = self.df.iloc[i-1]
            
            # 计算价格变化百分比
            price_change = (current_row['price'] - prev_row['price']) / prev_row['price']
            
            # 如果有买入位置，计算累积涨幅
            if self.last_buy_price > 0:
                cumulative_gain = (current_row['price'] - self.last_buy_price) / self.last_buy_price
            else:
                cumulative_gain = 0
            
            # 交易逻辑
            # 1. 恐慌值低且价格急跌时买入
            if current_row['score'] < -50 and price_change < -0.05 and self.position < 0.9:
                buy_amount = min(0.3, 0.9 - self.position)  # 最多买到80%仓位，每次买入30%
                cost = self.capital * buy_amount
                new_shares = cost / current_row['price']
                self.shares += new_shares
                self.position += buy_amount
                self.capital -= cost
                self.last_buy_price = current_row['price']
                self.buy_prices.append(current_row['price'])
                
                # 计算总资产（买入时不计入当天跌幅）
                total_value = self.capital + self.shares * current_row['price']
                
                self.trades.append({
                    'date': current_row['date'],
                    'type': 'buy',
                    'price': current_row['price'],
                    'position': self.position,
                    'score': current_row['score'],
                    'total_value': total_value,
                    'shares': self.shares
                })
            
            # 2. 恐慌值回升且累积涨幅超过10%时卖出
            elif current_row['score'] > -20 and self.position > 0.3 and cumulative_gain > 0.13:
                sell_amount = min(0.2, self.position - 0.1)  # 保持至少50%仓位，每次卖出20%
                sell_shares = self.shares * (sell_amount / self.position)
                self.shares -= sell_shares
                self.position -= sell_amount
                self.capital += sell_shares * current_row['price']
                
                # 计算总资产
                total_value = self.capital + self.shares * current_row['price']
                
                # 记录交易
                self.trades.append({
                    'date': current_row['date'],
                    'type': 'sell',
                    'price': current_row['price'],
                    'position': self.position,
                    'score': current_row['score'],
                    'gain': cumulative_gain * 100,  # 转换为百分比
                    'total_value': total_value,
                    'shares': self.shares
                })
                
                # 如果还有仓位，使用下一个买入价格作为基准
                if self.position > 0 and len(self.buy_prices) > 1:
                    self.buy_prices.pop(0)  # 移除已经获利的买入价格
                    self.last_buy_price = self.buy_prices[0]
            
            # 记录历史
            total_value = self.capital + self.shares * current_row['price']
            self.capital_history.append(total_value)
            self.position_history.append(self.position)
    
    def plot_results(self):
        fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(15, 12))
        
        # 绘制价格和得分
        ax1.plot(self.df['date'], self.df['price'], label='Price')
        ax1.set_title('SOXL Price and Trades')
        ax1.legend()
        
        # 标记交易点
        for trade in self.trades:
            if trade['type'] == 'buy':
                ax1.scatter(trade['date'], trade['price'], color='g', marker='^', label='Buy' if trade == self.trades[0] else "")
            else:
                gain = f" (+{trade['gain']:.1f}%)" if 'gain' in trade else ""
                ax1.scatter(trade['date'], trade['price'], color='r', marker='v', label=f'Sell{gain}' if trade == self.trades[1] else "")
                # 添加获利标注
                if 'gain' in trade:
                    ax1.annotate(f"+{trade['gain']:.1f}%", 
                               (trade['date'], trade['price']),
                               xytext=(10, 10), textcoords='offset points')
        
        # 绘制恐慌指数
        ax2.plot(self.df['date'], self.df['score'], label='Fear Score', color='orange')
        ax2.set_title('Fear Score')
        ax2.axhline(y=0, color='r', linestyle='--')
        ax2.axhline(y=-60, color='g', linestyle='--', label='Buy Threshold')
        ax2.axhline(y=5, color='r', linestyle='--', label='Sell Threshold')
        ax2.legend()
        
        # 绘制资金曲线
        ax3.plot(self.df['date'][1:], self.capital_history, label='Portfolio Value')
        ax3.set_title('Portfolio Value')
        ax3.legend()
        
        plt.tight_layout()
        plt.show()

# 运行回测
backtest = SOXLBacktest(df)
backtest.run()
backtest.plot_results()

# 输出回测结果
initial_value = 100000
final_value = backtest.capital_history[-1]
total_return = (final_value - initial_value) / initial_value * 100

print(f"回测结果:")
print(f"初始资金: ${initial_value:,.2f}")
print(f"最终资金: ${final_value:,.2f}")
print(f"总收益率: {total_return:.2f}%")
print(f"交易次数: {len(backtest.trades)}")
print("\n交易明细:")
prev_value = initial_value
for trade in backtest.trades:
    gain_info = f", 获利: {trade['gain']:.1f}%" if 'gain' in trade else ""
    value_change = trade['total_value'] - prev_value
    value_change_pct = (value_change / prev_value) * 100
    shares_info = f", 持仓数量: {trade['shares']:.2f}"
    print(f"日期: {trade['date'].strftime('%Y-%m-%d')}, 类型: {trade['type']}, 价格: ${trade['price']:.2f}, "
          f"仓位: {trade['position']:.2f}, 恐慌值: {trade['score']}{gain_info}, "
          f"总资金: ${trade['total_value']:,.2f} ({value_change_pct:+.2f}%){shares_info}")
    prev_value = trade['total_value']

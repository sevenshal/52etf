import logging
import pandas as pd
import schedule
import time
#from .evc_strategy import EvcStrategy
from .etf_manager import ETFManager
from .evc_manager import EVCManager
from ..core.services.longport import LongPortService
from .cnn_fear_index import CNNFearGreedIndexScraper
from .market_signal import MarketSignalAnalyzer
from .szdt_us_trader import start_szdt_us_trader
from .lev_etf_trader import start_lev_etf_trader
from .portfolio_copy_trader import start_portfolio_copy_trader
pd.set_option("display.max_rows", None)
pd.set_option("display.max_columns", None)
pd.set_option("display.width", 10000)

# 初始化账户 (建议从环境变量或配置中读取)
ACCOUNT_ID = 'vNKpHJkLMnBQRSTUVWXYZabcdefghijkl'
#lpAccounts = [EvcStrategy(account_id=ACCOUNT_ID, account_type='longport')]
longPortService = LongPortService.get_instance()
etf_manager = ETFManager(longPortService)
evc_manager = EVCManager()
cnn_fear_index_scraper = CNNFearGreedIndexScraper()
market_signal_analyzer = MarketSignalAnalyzer(longPortService)

def robot():
  #evc_manager.fetch_and_stocks(force_fetch=True)
  #etf_manager.analyze_all_fair_value()
  # cnn_fear_index_scraper.fetch_data_and_save()
  # etf_manager.calculate_all_emotions()
  #market_signal_analyzer.analyze()
  
  # 定时执行数据抓取
  schedule.every().day.at("08:00").do(evc_manager.fetch_and_stocks)

  # 定时执行ETF估值分析
  schedule.every().day.at("09:00").do(etf_manager.analyze_all_fair_value)
  
  # 新增：每天9:30定时执行美股信号分析
  schedule.every().day.at("09:30").do(market_signal_analyzer.analyze)
  
  # 定时执行ETF情绪指标计算
  # schedule.every().day.at("11:00").do(etf_manager.calculate_all_emotions)
  # 定时执行策略处理
  # schedule.every().day.at("21:00").do(execute_account_process)
  # 定时执行策略结束
  # schedule.every().day.at("09:00").do(execute_account_end)
  # 定时执行CNN Fear & Greed Index 抓取
  schedule.every().day.at("10:00").do(cnn_fear_index_scraper.fetch_data_and_save)
  logging.info("listening deal")
  # 启动 SZDT 贪恐策略美股自动交易（每分钟轮询，限美股开盘时段，检查所有开启配置）
  start_szdt_us_trader()
  # 启动杠杆ETF均线策略（收盘前10s检查）
  start_lev_etf_trader()

  # 启动 Portfolio Copy Trader Worker
  start_portfolio_copy_trader()

  while True:
    schedule.run_pending()
    time.sleep(10)


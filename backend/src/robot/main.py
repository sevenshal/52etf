import logging
import pandas as pd
import schedule
import time
#from .evc_strategy import EvcStrategy
from .etf_manager import ETFManager
from .evc_manager import EVCManager
from ..core.services.longport import LongPortService
from .cnn_fear_index import CNNFearGreedIndexScraper
pd.set_option("display.max_rows", None)
pd.set_option("display.max_columns", None)
pd.set_option("display.width", 10000)

# 初始化账户
ACCOUNT_ID = 'vNKpHJkLMnBQRSTUVWXYZabcdefghijkl'
#lpAccounts = [EvcStrategy(account_id=ACCOUNT_ID, account_type='longport')]
etf_manager = ETFManager(LongPortService(ACCOUNT_ID))
evc_manager = EVCManager()
cnn_fear_index_scraper = CNNFearGreedIndexScraper()

def robot():
  evc_manager.fetch_and_stocks(force_fetch=False)
  etf_manager.analyze_all_fair_value()
  # cnn_fear_index_scraper.fetch_data_and_save()
  # etf_manager.calculate_all_emotions()
  # 定时执行数据抓取
  schedule.every().day.at("08:00").do(evc_manager.fetch_and_stocks)

  # 定时执行ETF估值分析
  schedule.every().day.at("09:00").do(etf_manager.analyze_all_fair_value)
  # 定时执行ETF情绪指标计算
  # schedule.every().day.at("11:00").do(etf_manager.calculate_all_emotions)
  # 定时执行策略处理
  # schedule.every().day.at("21:00").do(execute_account_process)
  # 定时执行策略结束
  # schedule.every().day.at("09:00").do(execute_account_end)
  # 定时执行CNN Fear & Greed Index 抓取
  schedule.every().day.at("10:00").do(cnn_fear_index_scraper.fetch_data_and_save)
  logging.info("listening deal")
  while True:
    schedule.run_pending()
    time.sleep(10)


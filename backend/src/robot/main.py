import logging
import pandas as pd
import time
from .szdt_us_trader import start_szdt_us_trader
from .lev_etf_trader import start_lev_etf_trader
from .portfolio_copy_trader import start_portfolio_copy_trader
from .soxl_fear_strategy_trader import start_soxl_fear_strategy_trader
from .scheduled_tasks import scheduled_task_manager, run_startup_tasks

pd.set_option("display.max_rows", None)
pd.set_option("display.max_columns", None)
pd.set_option("display.width", 10000)

def robot():
  run_startup_tasks()
  logging.info("listening deal")
  # 启动 SZDT 贪恐策略美股自动交易（每分钟轮询，限美股开盘时段，检查所有开启配置）
  start_szdt_us_trader()
  # 启动杠杆ETF均线策略（收盘前10s检查）
  start_lev_etf_trader()

  # 启动 Portfolio Copy Trader Worker
  start_portfolio_copy_trader()
  # 启动 SOXL 情绪量能自动交易
  start_soxl_fear_strategy_trader()

  while True:
    try:
      scheduled_task_manager.run_pending()
    except Exception as e:
      import traceback
      from ..core.utils import send_alert_email
      send_alert_email("自动化业务报错: Schedule 主循序异常", f"Error: {e}\n\nTraceback:\n{traceback.format_exc()}")
    time.sleep(5)

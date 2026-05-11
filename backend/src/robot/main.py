import logging
import pandas as pd
import time
from .szdt_us_trader import start_szdt_us_trader
from .lev_etf_trader import start_lev_etf_trader
from .portfolio_copy_trader import start_portfolio_copy_trader
from .soxl_fear_strategy_trader import start_soxl_fear_strategy_trader
from .us_stock_signal_live_sync import start_us_stock_signal_live_sync
from .scheduled_tasks import scheduled_task_manager, run_startup_tasks

pd.set_option("display.max_rows", None)
pd.set_option("display.max_columns", None)
pd.set_option("display.width", 10000)

_last_w20_strategy_automation_check = 0.0
_last_external_trading_monitor_check = 0.0


def _process_w20_strategy_automation():
  global _last_w20_strategy_automation_check
  now = time.time()
  if now - _last_w20_strategy_automation_check < 30:
    return
  _last_w20_strategy_automation_check = now

  try:
    from ..app.api.w20_momentum_live import (
      process_pending_w20_live_trade_executions_for_robot,
      process_w20_momentum_live_strategy_automation_for_robot,
    )

    automation_result = process_w20_momentum_live_strategy_automation_for_robot()
    if (
      automation_result.get("signals")
      or automation_result.get("signal_waiting")
      or automation_result.get("virtual_trades")
      or automation_result.get("virtual_trade_waiting")
      or automation_result.get("plan_emails")
      or automation_result.get("plan_skipped")
      or automation_result.get("errors")
    ):
      logging.info("W20 strategy automation result: %s", automation_result)

    execution_result = process_pending_w20_live_trade_executions_for_robot()
    if execution_result.get("processed") or execution_result.get("failed") or execution_result.get("deferred"):
      logging.info("W20 pending live trade execution result: %s", execution_result)
  except Exception:
    logging.exception("W20 strategy automation check failed")


def _process_external_trading_connection_monitor():
  global _last_external_trading_monitor_check
  now = time.time()
  if now - _last_external_trading_monitor_check < 30:
    return
  _last_external_trading_monitor_check = now

  try:
    from ..core.services.external_trading_monitor import (
      process_external_trading_connection_monitor_for_robot,
    )

    monitor_result = process_external_trading_connection_monitor_for_robot()
    if monitor_result.get("alerts"):
      logging.warning("External trading connection monitor result: %s", monitor_result)
  except Exception:
    logging.exception("External trading connection monitor failed")


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
  # 启动美股多因子策略虚拟盘自动同步（美股收盘后轮询配置）
  start_us_stock_signal_live_sync()

  while True:
    try:
      scheduled_task_manager.run_pending()
      _process_w20_strategy_automation()
      _process_external_trading_connection_monitor()
    except Exception as e:
      import traceback
      from ..core.utils import send_alert_email
      send_alert_email("自动化业务报错: Schedule 主循序异常", f"Error: {e}\n\nTraceback:\n{traceback.format_exc()}")
    time.sleep(5)

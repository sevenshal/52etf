import logging
import pandas as pd
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from .szdt_us_trader import start_szdt_us_trader
from .lev_etf_trader import start_lev_etf_trader
from .portfolio_copy_trader import start_portfolio_copy_trader
from .soxl_fear_strategy_trader import start_soxl_fear_strategy_trader
from .scheduled_tasks import scheduled_task_manager, run_startup_tasks
from .external_trading_log import summarize_external_trading_executor_result

pd.set_option("display.max_rows", None)
pd.set_option("display.max_columns", None)
pd.set_option("display.width", 10000)

_last_factor_live_trading_automation_check = 0.0
_last_valuation_sim_automation_check = 0.0
_last_external_trading_monitor_check = 0.0
_last_external_trading_executor_check = 0.0
_last_snowball_external_trading_sync_check = 0.0
_last_ai_stock_automation_check = 0.0


def _process_factor_live_trading_automation():
  global _last_factor_live_trading_automation_check
  now = time.time()
  if now - _last_factor_live_trading_automation_check < 30:
    return
  _last_factor_live_trading_automation_check = now

  try:
    from ..app.api.factor_lab import (
      process_factor_live_trading_automation_for_robot,
    )

    automation_result = process_factor_live_trading_automation_for_robot()
    if (
      automation_result.get("signals")
      or automation_result.get("signal_waiting")
      or automation_result.get("executions")
      or automation_result.get("execution_waiting")
      or automation_result.get("errors")
    ):
      logging.info("Factor live trading automation result: %s", automation_result)

  except Exception:
    logging.exception("Factor live trading automation check failed")


def _process_valuation_sim_automation():
  global _last_valuation_sim_automation_check
  now = time.time()
  if now - _last_valuation_sim_automation_check < 30:
    return
  _last_valuation_sim_automation_check = now

  try:
    from ..app.api.valuation_sim import (
      process_valuation_sim_automation_for_robot,
    )

    automation_result = process_valuation_sim_automation_for_robot()
    if automation_result.get("processed") or automation_result.get("errors"):
      logging.info("Valuation simulation automation result: %s", automation_result)

  except Exception:
    logging.exception("Valuation simulation automation check failed")


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


def _process_external_trading_executor():
  global _last_external_trading_executor_check
  now = time.time()
  if now - _last_external_trading_executor_check < 15:
    return
  _last_external_trading_executor_check = now

  try:
    from ..core.services.external_trading_executor import (
      process_external_trading_executor_for_robot,
    )

    executor_result = process_external_trading_executor_for_robot()
    if executor_result.get("processed") or executor_result.get("failed"):
      logging.info(
        "External trading executor result: %s",
        summarize_external_trading_executor_result(executor_result),
      )
  except Exception:
    logging.exception("External trading executor failed")


def _process_snowball_external_trading_sync():
  global _last_snowball_external_trading_sync_check
  now = time.time()
  if now - _last_snowball_external_trading_sync_check < 60:
    return
  _last_snowball_external_trading_sync_check = now

  try:
    from ..app.api.snowball import (
      process_snowball_external_trading_sync_for_robot,
    )

    sync_result = process_snowball_external_trading_sync_for_robot()
    if sync_result.get("changed") or sync_result.get("failed"):
      logging.info("Snowball external trading sync result: %s", sync_result)
  except Exception:
    logging.exception("Snowball external trading sync failed")


def _process_ai_stock_automation():
  """Keep AI recommendation generation and the paper portfolio off request paths."""
  global _last_ai_stock_automation_check
  now_monotonic = time.time()
  if now_monotonic - _last_ai_stock_automation_check < 30:
    return
  _last_ai_stock_automation_check = now_monotonic

  try:
    from ..core.services.ai_stock import process_ai_stock_automation_for_robot

    shanghai_now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    automation_result = process_ai_stock_automation_for_robot(now=shanghai_now)
    if automation_result.get("recommendation") or (automation_result.get("paper") or {}).get("trades"):
      logging.info("AI stock automation result: %s", automation_result)
  except Exception:
    logging.exception("AI stock automation check failed")


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
      _process_factor_live_trading_automation()
      _process_valuation_sim_automation()
      _process_snowball_external_trading_sync()
      _process_ai_stock_automation()
      _process_external_trading_executor()
      _process_external_trading_connection_monitor()
    except Exception as e:
      import traceback
      from ..core.utils import send_alert_email
      send_alert_email(
        "自动化业务报错: Schedule 主循序异常",
        f"Error: {e}\n\nTraceback:\n{traceback.format_exc()}",
        scenario_key="robot_main_loop_error",
      )
    time.sleep(5)

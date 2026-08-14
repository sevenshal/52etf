# -*- coding: utf-8 -*-
"""
PTrade 实时行情桥接策略（供券商 python 3.5 环境运行，粘贴到 PTrade 策略里即可）。

功能
----
- 纯跟随后端股票池：不内置沪深300。启动/盘前上报 0 个 quotes 只为拉取最新股票池，
  之后每个交易日的 tick_data（约 3s）按后端返回的池上报行情。
- 全程只用一个 HTTP 接口：POST {bridge_url}，请求带报价（可空），响应返回最新
  股票池 + pool_version，脚本据此 set_universe 增量更新订阅。

部署
----
1. 把本文件内容粘贴到 PTrade 新建股票策略中保存。
2. 按下方"配置区"填写 bridge_url（https://api.52etf.vip/api/realtime/pool）和
   account_id（后端分配的 PTrade 桥接账号）。
3. 交易对象/股票池保持为空即可（脚本用 set_universe 动态订阅）。

依赖
----
仅标准库（urllib/json/time），不依赖第三方包；与 python 3.5 兼容：
不使用 f-string / walrus / 类型注解 / dataclass。
"""

import json
import time
import urllib.request

# ============ 配置区 ============
BRIDGE_URL = "https://api.52etf.vip/api/realtime/pool"
BRIDGE_ACCOUNT = "ptrade-bridge"
REPORT_TIMEOUT_SECONDS = 10
# ================================


def _to_ptrade_code(code):
    """后端池是 .SH/.SZ 格式，PTrade 上海市场要用 .SS 尾缀。"""
    code = str(code or "").strip().upper()
    if code.endswith(".SH"):
        code = code[:-3] + ".SS"
    return code


def _to_backend_code(code):
    """PTrade tick 里的代码是 .SS/.SZ，原样上报即可（后端会归一化 .SS -> .SH）。"""
    return str(code or "").strip().upper()


def _number(value):
    """numpy 标量转 Python float（json 可序列化），NaN/Inf 归 0 避免污染整批 JSON。"""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    if result != result or result == float("inf") or result == float("-inf"):
        return 0.0
    return result


def _sync_pool(context, quotes):
    """上报报价，并从响应里取最新股票池；池变化时 set_universe 增量更新订阅。"""
    payload = json.dumps({
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "quotes": quotes,
    }).encode("utf-8")
    request = urllib.request.Request(
        BRIDGE_URL,
        data=payload,
        method="POST",
        headers={
            "X-Account-ID": BRIDGE_ACCOUNT,
            "Content-Type": "application/json",
        },
    )
    try:
        response = json.loads(
            urllib.request.urlopen(request, timeout=REPORT_TIMEOUT_SECONDS).read().decode("utf-8")
        )
        new_version = response.get("pool_version")
        if new_version is not None and new_version != g.pool_version:
            pool = response.get("pool") or []
            g.pool_version = new_version
            ptrade_pool = [_to_ptrade_code(code) for code in pool]
            g.pool = ptrade_pool
            if ptrade_pool:
                set_universe(ptrade_pool)
                log.info("realtime bridge pool updated: %d codes, version %s" %
                         (len(ptrade_pool), str(new_version)))
    except Exception as exc:
        # 上报失败不中断策略，等下一个 tick 再试
        log.error("realtime bridge sync failed: %s", str(exc))


def initialize(context):
    g.bridge_url = BRIDGE_URL
    g.pool = []
    g.pool_version = None
    # 一开始上报 0 个 quotes，只为刷新股票池
    _sync_pool(context, {})


def before_trading_start(context, data):
    # 盘前再引导一次，拉取隔日最新股票池
    _sync_pool(context, {})


def handle_data(context, data):
    # 必选占位函数；tick 处理走 tick_data
    pass


def tick_data(context, data):
    """官方 3s tick 推送：data[code] = {'tick': DataFrame, 'order': ..., 'transcation': ...}"""
    quotes = {}
    for code, item in data.items():
        tick = item.get('tick') if isinstance(item, dict) else None
        if tick is None or len(tick) == 0:
            continue
        row = tick.iloc[-1]
        quotes[_to_backend_code(code)] = {
            "last_px": _number(row["last_px"]),
            "preclose_px": _number(row["preclose_px"]),
            "open_px": _number(row["open_px"]),
            "high_px": _number(row["high_px"]),
            "low_px": _number(row["low_px"]),
            "amount": _number(row["amount"]),
            "hs_time": str(row["hsTimeStamp"]),
            "trade_status": str(row["trade_status"]),
        }
    _sync_pool(context, quotes)

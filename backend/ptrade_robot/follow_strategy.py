import requests
from datetime import datetime, timedelta

def initialize(context):
    # 全局变量初始化
    g.api_base_url = "https://api.framework.cn/api/snowball"
    g.account_id = "vNKpHJkLMnBQRSTUVWXYZabcdefghijkl"
    g.headers = {"x-account-id": g.account_id}

def convert_to_api_code(client_code):
    """转换股票代码格式（客户端格式转API格式）
    从 513478.SS 格式转换为 SH.513478 或 SS.513478 格式
    """
    if not client_code:
        return None
    parts = client_code.split('.')
    if len(parts) != 2:
        return None
    code, market = parts
    # 如果市场代码是 SS，转换为 SH
    if market.upper() == 'SS':
        market = 'SH'
    return "{0}.{1}".format(market.upper(), code)

def convert_to_client_code(api_code):
    """转换股票代码格式（API格式转客户端格式）
    从 SH.513478 或 SS.513478 格式转换为 513478.SS 格式
    """
    if not api_code:
        return None
    parts = api_code.split('.')
    if len(parts) != 2:
        return None
    market, code = parts
    # 如果市场代码是 SH，转换为 SS
    if market.upper() == 'SH':
        market = 'SS'
    return "{0}.{1}".format(code, market.upper())

def get_limit_price(symbol: str, side: str, quantity: int) -> float:
    """获取限价 - 智能深度撮合
    Args:
        symbol: 股票代码
        side: 交易方向，"BUY" 或 "SELL"
        quantity: 需要执行的数量
    Returns:
        float: 满足数量的限价
    """
    gear_price = get_gear_price(symbol)
    if not gear_price or not gear_price.get('bid_grp') or not gear_price.get('offer_grp'):
        raise Exception("获取 %s 档位价格失败: 数据为空" % symbol)
        
    # BUY: 看卖盘 (offer_grp), SELL: 看买盘 (bid_grp)
    grp = gear_price['offer_grp'] if side == "BUY" else gear_price['bid_grp']
    
    accumulated_vol = 0
    target_price = 0.0
    
    # 遍历档位 1-5
    sorted_levels = [1, 2, 3, 4, 5]
    
    # 初始化为第1档价格，防止返回0
    # 先检查是否有第1档
    if 1 in grp:
        target_price = grp[1][0]
    else:
        raise Exception("获取 %s 档位价格失败: 档位数据为空" % symbol)

    for level in sorted_levels:
        price_vol = grp.get(level)
        if not price_vol: continue
        
        p = price_vol[0]
        v = price_vol[1]
        
        accumulated_vol += v
        target_price = p
        
        if accumulated_vol >= quantity:
            log.info("%s %s 数量%d, 档位%d满足 (累积%d), 价格%s" % (symbol, side, quantity, level, accumulated_vol, p))
            return p
            
    # 如果深度不够（或超过5档仍不够），就用当前遍历到的最后一档价格
    log.info("%s 前5档深度不足以覆盖数量%d (累积%d), 使用最终价格 %s" % (symbol, quantity, accumulated_vol, target_price))
    return target_price


def handle_data(context, data):
    """每分钟执行一次"""
    try:
        # 1. 撤销所有未完成订单
        today_orders = get_all_orders() or []
        pending_orders = [
            o for o in today_orders 
            if str(o['status']) not in ["5", "6", "8", "9"]
        ]
        
        if pending_orders:
            log.info("检测到 %d 个未完成订单，准备撤单..." % len(pending_orders))
            for po in pending_orders:
                try:
                    cancel_order(po['entrust_no'])
                    log.info("已请求撤单: %s (订单号: %s)" % (po['symbol'], po['entrust_no']))
                except Exception as e:
                    log.error("撤单失败 %s (订单号: %s): %s" % (po['symbol'], po['entrust_no'], str(e)))
        
        # 重新获取持仓
        positions_dict = get_positions()
        
        # 重新获取今日订单状态
        today_orders = get_all_orders() or []

        backtest = not is_trade()
        # 请求交易机会
        response = requests.post(
            g.api_base_url + "/opportunities",
            headers=g.headers,
            json={
                "cli_id": 'GS66301027527' + ('B' if backtest else ''),
                "backtest": backtest,
                "current_time": context.current_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "orders": [
                    {
                        "symbol": convert_to_api_code(order['symbol']),
                        "side": "BUY" if order['entrust_bs'] == 1 else "SELL",
                        "quantity": order['amount'],
                        "status": order['status'],
                        "submitted_at": context.current_dt.isoformat()
                    }
                    for order in today_orders
                ],
                "positions": [
                    {
                        "symbol": convert_to_api_code(pos.sid),
                        "quantity": pos.amount,
                        "cost_price": pos.cost_basis,
                        "available_quantity": pos.enable_amount
                    }
                    for pos in positions_dict.values()
                    if pos.amount > 0
                ],
                "portfolio": {
                    "portfolio_value": context.portfolio.portfolio_value,  # 总资产（现金+持仓）
                    "available_cash": context.portfolio.cash,  # 可用资金
                    "locked_cash": context.portfolio.portfolio_value - context.portfolio.positions_value - context.portfolio.cash,  # 由于没有冻结资金的字段，我们可以用总资产减去持仓市值得到
                    "total_cash": context.portfolio.cash,  # 总现金就是可用现金
                    "total_positions_value": context.portfolio.positions_value  # 持仓市值
                }
            }
        )
        
        if response.status_code != 200:
            log.error("获取交易机会失败: %s" % response.text)
            return
        
        result = response.json()
        log.info("获取交易机会成功 %s" % str(result["msg"]))
        opportunities = result["opportunities"]
        
        # 执行交易机会
        for opp in opportunities:
            op_id = opp.get("op_id")
            try:
                symbol = convert_to_client_code(opp["symbol"])
                qty = int(opp["quantity"])
                action = opp["action"]
                
                # 获取限价 (带数量)
                limit_price = get_limit_price(symbol, action, qty)
                
                status = "FAILED"
                msg = ""
                order_sn = None
                
                if action == "BUY":
                    order_sn = order(symbol, qty, limit_price=limit_price)
                    if order_sn:
                        orders = get_order(order_sn)
                        o = orders[0] if orders else None
                        if o and str(o.status) == "9":
                            status = "FAILED"
                            msg = "买入%s %s失败(被拒绝)" % (opp.get('name',''), symbol)
                        else:
                            status = "SUCCESS"
                            msg = "买入%s %s, 数量: %d, 价格: %s" % (opp.get('name',''), symbol, qty, limit_price)
                    else:
                        msg = "买入%s %s失败(无订单号)" % (opp.get('name',''), symbol)
                elif action == "SELL":
                    order_sn = order(symbol, -qty, limit_price=limit_price)
                    if order_sn:
                        orders = get_order(order_sn)
                        o = orders[0] if orders else None
                        if o and str(o.status) == "9":
                            status = "FAILED"
                            msg = "卖出%s %s失败(被拒绝)" % (opp.get('name',''), symbol)
                        else:
                            status = "SUCCESS"
                            msg = "卖出%s %s, 数量: %d, 价格: %s" % (opp.get('name',''), symbol, qty, limit_price)
                    else:
                        msg = "卖出%s %s失败(无订单号)" % (opp.get('name',''), symbol)
                
                # 优先使用结构化日志上报
                report_execution(op_id, status, msg, price=limit_price if order_sn else None)
                
                # 本地日志
                if order_sn:
                    log.info("交易成功: " + msg)
                else:
                    log.error("交易失败: " + msg)

            except Exception as e:
                log.error("处理单笔交易失败(op_id=%s): %s" % (op_id, str(e)))
                report_execution(op_id, "FAILED", "策略执行异常: " + str(e))
                
    except Exception as e:
        log.error("处理交易时发生异常: %s" % str(e))

# 保留日志相关函数
def report_execution(op_id, status, message, price=None):
    """上报执行结果到后端"""
    if not op_id:
        return
    try:
        payload = {
            "id": op_id,
            "status": status,
            "message": str(message)
        }
        if price:
            payload["price"] = price
            
        requests.post(
            g.api_base_url + "/logs/status",
            headers=g.headers,
            json=payload,
            timeout=5
        )
    except Exception as e:
        log.error("上报执行结果失败: %s" % str(e))

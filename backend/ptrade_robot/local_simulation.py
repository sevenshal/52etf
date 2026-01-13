import requests
import logging
import time
import json
from datetime import datetime

# --- Configuration ---
API_BASE_URL = "http://127.0.0.1:8000/api/snowball"
CLI_ID = "SIMULATION_USER_001"
ACCOUNT_ID = "vNKpHJkLMnBQRSTUVWXYZabcdefghijkl" # Must match valid_account logic or be ignored if simulation mode
HEADERS = {"x-account-id": ACCOUNT_ID}

# --- Mocks ---

class MockPosition:
    def __init__(self, sid, amount, cost, enable_amount=None):
        self.sid = sid
        self.amount = amount
        self.cost_basis = cost
        self.enable_amount = enable_amount if enable_amount is not None else amount

class MockPortfolio:
    def __init__(self, cash):
        self.cash = cash
        self.positions = {} # sid -> MockPosition
        self.positions_value = 0.0
        self.portfolio_value = cash

    def update(self, price_map):
        self.positions_value = sum(p.amount * price_map.get(p.sid, 0) for p in self.positions.values())
        self.portfolio_value = self.cash + self.positions_value

class MockContext:
    def __init__(self, cash=1000000):
        self.portfolio = MockPortfolio(cash)
        self.current_dt = datetime.now()

# Globals
g = type('G', (), {})()
g.api_base_url = API_BASE_URL
g.headers = HEADERS
g.context = MockContext()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger("MockPTrade")

# --- Helpers (Mocking PTrade API) ---

def convert_to_api_code(client_code):
    if not client_code: return None
    parts = client_code.split('.')
    if len(parts) != 2: return None
    code, market = parts
    if market.upper() == 'SS': market = 'SH'
    return "{0}.{1}".format(market.upper(), code)

def convert_to_client_code(api_code):
    if not api_code: return None
    parts = api_code.split('.')
    if len(parts) != 2: return None
    market, code = parts
    if market.upper() == 'SH': market = 'SS'
    return "{0}.{1}".format(code, market.upper())

def get_positions():
    return g.context.portfolio.positions

def get_all_orders():
    return [] # Mock: No pending orders

def is_trade():
    return True # Simulation is always "trading"

class MockOrder:
    def __init__(self, order_id, status=0):
        self.order_id = order_id
        self.status = status

def get_order(order_id):
    # Mock: Return a list containing a mock order with Success status (not 8)
    return [MockOrder(order_id, 0)]

def get_limit_price(symbol, side):
    # Mock: Return a static price or fetch simple one
    # For simulation, we'll try to fetch from Xueqiu via simple request or just return 10.0
    # To make it realistic, let's use a public API or just mock
    # Mocking as 10.0 for reliability in script
    return 10.0 

def order(symbol, amount, limit_price):
    # Simulate Order Execution
    port = g.context.portfolio
    cost = amount * limit_price
    
    if amount > 0: # BUY
        if port.cash < cost:
            log.warning(f"Order REJECTED: Insufficient Cash. Need {cost}, Have {port.cash}")
            return None
        port.cash -= cost
        if symbol not in port.positions:
            port.positions[symbol] = MockPosition(symbol, 0, limit_price)
        port.positions[symbol].amount += amount
        port.positions[symbol].enable_amount += amount # T+0 for sim
        # Update cost basis? Simplified: Keep initial
    else: # SELL
        if symbol not in port.positions or port.positions[symbol].enable_amount < abs(amount):
             log.warning(f"Order REJECTED: Insufficient Share. Need {abs(amount)}")
             return None
        port.cash += abs(cost)
        port.positions[symbol].amount += amount # Amount is negative
        port.positions[symbol].enable_amount += amount
        if port.positions[symbol].amount <= 0:
            del port.positions[symbol]
            
    log.info(f"Order EXECUTED: {symbol} {amount} @ {limit_price}")
    # Update portfolio value
    # In reality, need real prices. simulation uses last trade price.
    return "sim_order_id_123"

# --- Client Logic (Copied/Adapted from follow_strategy.py) ---

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

def handle_data(context, data):
    """每分钟执行一次"""
    try:
        log.info("--- Starting Simulation Tick ---")
        
        # 模拟：更新持仓市值 (Mock update)
        # simplistic update: all held positions valued @ 10.0
        context.portfolio.update({k: 10.0 for k in context.portfolio.positions})

        # 获取当日订单
        today_orders = get_all_orders() or []
        # 获取持仓
        positions_dict = get_positions()
        backtest = False # We pretend to be live for this sim
        
        # Requests payload
        payload = {
            "cli_id": CLI_ID,
            "backtest": backtest,
            "orders": [],
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
                "portfolio_value": context.portfolio.portfolio_value,  
                "available_cash": context.portfolio.cash,  
                "locked_cash": 0.0, # Zero for simple simulation
                "total_cash": context.portfolio.cash,  
                "total_positions_value": context.portfolio.positions_value
            },
            "current_time": context.current_dt.isoformat()
        }
        
        log.info(f"Posting to {g.api_base_url}/opportunities with cli_id={CLI_ID}")
        
        # 请求交易机会
        response = requests.post(
            g.api_base_url + "/opportunities",
            headers=g.headers,
            json=payload
        )
        
        if response.status_code != 200:
            log.error("获取交易机会失败: %s" % response.text)
            return
        
        result = response.json()
        log.info("获取交易机会成功: %s" % str(result.get("msg")))
        opportunities = result.get("opportunities", [])
        
        # 执行交易机会
        for opp in opportunities:
            symbol = convert_to_client_code(opp["symbol"])
            op_id = opp.get("op_id")
            
            # 获取限价
            limit_price = opp["price"] #get_limit_price(symbol, opp["action"])
            
            status = "FAILED"
            msg = ""
            order_sn = None
            
            if opp["action"] == "BUY":
                order_sn = order(symbol, opp["quantity"], limit_price=limit_price)
                if order_sn:
                    status = "SUCCESS"
                    msg = "买入%s %s, 数量: %d, 价格: %s" % (opp.get('name',''), symbol, opp['quantity'], limit_price)
                else:
                    msg = "买入%s %s失败" % (opp.get('name',''), symbol)
            elif opp["action"] == "SELL":
                order_sn = order(symbol, -opp["quantity"], limit_price=limit_price)
                if order_sn:
                    status = "SUCCESS"
                    msg = "卖出%s %s, 数量: %d, 价格: %s" % (opp.get('name',''), symbol, opp['quantity'], limit_price)
                else:
                    msg = "卖出%s %s失败" % (opp.get('name',''), symbol)
            
            # 优先使用结构化日志上报
            report_execution(op_id, status, msg, price=limit_price if order_sn else None)
            
            # 本地日志
            if order_sn:
                log.info("交易成功: " + msg)
            else:
                log.error("交易失败: " + msg)
                
    except Exception as e:
        log.error("处理交易时发生异常: %s" % str(e))
        import traceback
        traceback.print_exc()

# --- Main Runner ---

if __name__ == "__main__":
    import argparse
    from datetime import timedelta
    
    parser = argparse.ArgumentParser(description="Local PTrade Simulation")
    parser.add_argument("--start", type=str, help="Start time (YYYY-MM-DD HH:MM)")
    parser.add_argument("--end", type=str, help="End time (YYYY-MM-DD HH:MM)")
    args = parser.parse_args()

    print(f"Starting Local Simulation for CLI_ID: {CLI_ID}")
    print("Ensure your local backend is running at http://127.0.0.1:8000")
    
    # Initialize Context
    # Pre-load positions from user request
    initial_positions = [
        ("300179.SZ", 8600, 16.374),
        ("688448.SS", 2700, 52.420),
        ("301590.SZ", 700, 193.241),
        ("300775.SZ", 11400, 10.0),
        ("301312.SZ", 7000, 10.0),
        ("688416.SS", 3100, 10.0),
        ("000034.SZ", 4800, 10.0),
        ("002860.SZ", 14000, 10.0),
        ("688135.SS", 5500, 10.0),
        ("300733.SZ", 8500, 10.0),
        ("688500.SS", 2100, 10.0),
        ("300004.SZ", 10900, 10.0),
        ("688807.SS", 800, 10.0),
        ("002725.SZ", 7800, 10.0),
        ("301157.SZ", 2500, 10.0)
    ]
    
    for sid, amount, cost in initial_positions:
        g.context.portfolio.positions[sid] = MockPosition(sid, amount, cost)
    
    try:
        if args.start and args.end:
            # Backtest / Range Mode
            start_dt = datetime.strptime(args.start, "%Y-%m-%d %H:%M")
            end_dt = datetime.strptime(args.end, "%Y-%m-%d %H:%M")
            print(f"Running simulation from {start_dt} to {end_dt}")
            
            curr = start_dt
            while curr <= end_dt:
                g.context.current_dt = curr
                handle_data(g.context, None)
                curr += timedelta(minutes=1)
                time.sleep(5)
                # No sleep here for faster execution in range mode
        else:
            # Live Simulation Mode
            print("Press Ctrl+C to stop.")
            while True:
                handle_data(g.context, None)
                time.sleep(60) # Run every minute
                
    except KeyboardInterrupt:
        print("Simulation Stopped")
    except Exception as e:
        log.error("Simulation failed: %s", e)
        import traceback
        traceback.print_exc()

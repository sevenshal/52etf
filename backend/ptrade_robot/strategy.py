import requests
from datetime import datetime, timedelta

def initialize(context):
    # 全局变量初始化
    g.api_base_url = "https://api.framework.cn/api/quant"  # 替换为实际的API地址
    g.account_id = "vNKpHJkLMnBQRSTUVWXYZabcdefghijkl"  # 替换为实际的account_id
    g.headers = {"x-account-id": g.account_id}
    g.traded_stocks = {}
    g.current_stocks = []  # 当前处理的股票列表
    g.current_index = 0   # 当前处理的股票索引
    g.cooldown_stocks = {}  # 新增：存储股票冷却信息

def convert_stock_code(api_code):
    """转换股票代码格式
    从 SH.513478 或 SS.513478 格式转换为 513478.SS 格式
    注意：SH 需要转换为 SS
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

def handle_data(context, data):
    """每分钟执行一次"""
    try:
        stock = pick_single_stock(context)
        if not stock:
            return
        # 处理当前股票
        process_single_stock(context, stock)
        g.current_index += 1
        
    except Exception as e:
        log.error("处理股票时发生异常: %s" % str(e))

def log_to_backend(level, message):
    """记录日志到后端"""
    if level == "ERROR":
        log.error(message)
    else:
        log.info(message)
    try:
        requests.post(
            g.api_base_url + "/trading-logs",
            headers=g.headers,
            json={
                "level": level,
                "message": message
            }
        )
    except Exception as e:
        log.error("记录日志到后端失败: %s" % str(e))

def pick_single_stock(context):
    """选择单只股票"""
    # 需要更新股票列表的情况：列表为空或已处理完当前列表
    if not g.current_stocks or g.current_index >= len(g.current_stocks):
        # 获取新的股票列表
        stocks_resp = requests.get(g.api_base_url + "/trading-stocks", headers=g.headers)
        if stocks_resp.status_code != 200:
            log.error("获取股票列表失败: %s" % stocks_resp.text)
            return None
            
        g.current_stocks = stocks_resp.json()
        g.current_index = 0
        log.info("更新股票列表，共 %d 只股票" % len(g.current_stocks))
        
        # 如果列表为空（可能是自动交易开关关闭），直接返回
        if not g.current_stocks:
            log.info("自动交易未开启或没有可交易的股票")
            return None
    while g.current_index < len(g.current_stocks):
        stock = g.current_stocks[g.current_index]
        code = convert_stock_code(stock['code'])
        # 检查冷却时间
        current_time = context.current_dt
        if code in g.cooldown_stocks:
            cooldown_info = g.cooldown_stocks[code]
            if current_time < cooldown_info['until']:
                remaining_minutes = int((cooldown_info['until'] - current_time).total_seconds() / 60)
                log.info("股票 %s 在冷却中，剩余 %d 分钟，原因: %s" % (
                    code, remaining_minutes, cooldown_info['reason']))
                g.current_index += 1
                continue
            else:
                del g.cooldown_stocks[code]
                return stock
        else:
            return stock
    if g.current_index >= len(g.current_stocks):
        log.info("股票列表已处理完毕，跳过操作")
        return None

def process_single_stock(context, stock):
    """处理单只股票"""
    api_code = None
    code = None
    try:
        if stock is None:
            return
        api_code = stock['code']
        code = convert_stock_code(api_code)
        name = "%s(%s)" % (stock['name'], api_code)
        if not code:
            log_to_backend("ERROR", "股票代码格式错误: %s" % api_code)
            return

        current_time = context.current_dt
        # 检查当日是否已有订单
        today_orders = get_all_orders(code)
        if today_orders:
            g.cooldown_stocks[code] = {
                'until': current_time + timedelta(hours=12),
                'reason': '今日已交易'
            }
            log_to_backend("INFO", "股票 %s 当日已存在订单，跳过操作" % name)
            return
            
        # 获取情绪数据
        emotion_resp = requests.get(
            g.api_base_url + "/stock-emotion/" + api_code,
            headers=g.headers,
            params={
                'lever': stock.get('lever', 1),
                'emo_area': stock.get('emo_area', 'a')
            }
        )
        
        if emotion_resp.status_code != 200:
            log.error("获取股票 %s 情绪数据失败: HTTP %d, %s" % (
                name, emotion_resp.status_code, emotion_resp.text))
            return

        emotion_data = emotion_resp.json()
        if emotion_data['status'] != 1:
            log.error("获取股票 %s 情绪数据异常: %s" % (
                name, emotion_data.get('msg', '未知错误')))
            return

        emotion_score = emotion_data['data']['score']
        current_price = emotion_data['data']['price']
        
        current_time_str = context.current_dt.strftime("%H:%M")
        
        # 获取情绪数据后，添加高情绪分数检查
        if emotion_score > (stock['when_buy'] + 20) and get_position(code).amount == 0:
            g.cooldown_stocks[code] = {
                'until': current_time + timedelta(hours=1),
                'reason': '无持仓且情绪分数过高'
            }
            log_to_backend("INFO", "股票 %s 情绪分数(%d)超过买入条件20分，设置1小时冷却期" % (
                name, emotion_score))
            return
        
        # 判断买入条件
        if emotion_score <= stock['when_buy']:
            # 获取持仓信息
            position = get_position(code)
            position_value = position.amount * current_price
            log.info("股票 %s 持仓数量: %d, 持仓价值: %.2f" % (name, position.amount, position_value))

            total_value = context.portfolio.portfolio_value
            position_ratio = 100 * position_value / total_value if total_value > 0 else 0
            
            # 检查持仓比例是否超过限制
            if position_ratio >= stock['max_position']:
                log_to_backend("INFO", "股票 %s 持仓比例 %.2f%% 已超过限制 %.2f%%，跳过买入" % (
                    name, position_ratio, stock['max_position']))
                return
                
            amount = int(stock['buy_amount'] / current_price / 100) * 100
            if amount >= 100:
                order(code, amount)
                g.traded_stocks[code] = {
                    'action': 'BUY',
                    'time': current_time_str,
                    'score': emotion_score
                }
            log_to_backend("INFO", "买入 %s, 数量: %d, 情绪分数: %d <= %d, 当前价格: %.2f, 买前持仓比例: %.2f%%" % (
                    name, amount, emotion_score, stock['when_buy'], current_price, position_ratio))
            
        # 判断卖出条件
        elif emotion_score >= stock['when_sell']:
            position = get_position(code)
            log.info("股票 %s 持仓数量: %d" % (code, position.amount))
            if position.amount > 0:
                sell_amount = min(
                    position.amount,
                    int(stock['sell_amount'] / current_price / 100) * 100
                )
                if sell_amount >= 100:
                    order(code, -sell_amount)
                    g.traded_stocks[code] = {
                        'action': 'SELL',
                        'time': current_time_str,
                        'score': emotion_score
                    }
                log_to_backend("INFO", "卖出 %s, 数量: %d, 情绪分数: %d >= %d, 当前价格: %.2f, 卖前持仓数量: %d" % (
                        name, sell_amount, emotion_score, stock['when_sell'], current_price, position.amount))
            else:
                log_to_backend("INFO", "股票 %s 没有持仓, 情绪分数: %d >= %d" % (name, emotion_score, stock['when_sell']))
        else:
            log_to_backend("INFO", "股票 %s 情绪分数: %d, 介于%d和%d之间, 耐心等待" % (
                name, emotion_score, stock['when_buy'], stock['when_sell']))
          
    except Exception as e:
        stock_code = code or api_code or "未知"
        log_to_backend("ERROR", "处理股票 %s 时发生异常: %s" % (stock_code, str(e)))

def before_trading_start(context, data):
    """盘前清理上一个交易日的记录"""
    g.traded_stocks = {}
    g.current_stocks = []
    g.current_index = 0
    g.cooldown_stocks = {}  # 清理冷却记录

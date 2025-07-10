import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))  # 添加 backend 目录到路径

from pathlib import Path
# 将项目根目录添加到 PYTHONPATH
PROJECT_ROOT = str(Path(__file__).parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from datetime import datetime, timedelta

print("Python Path:", sys.path)
print("Current Directory:", os.getcwd())

from ..emotion.etf_emotion import ETFEmotionCalculator
from ..core.services.longport import LongPortService
from ..core.services.quote import QuoteService
from ..core.database import Session, ETFEmotion
from .etf_manager import ETFManager
from tqdm import tqdm

quote_provider = LongPortService("vNKpHJkLMnBQRSTUVWXYZabcdefghijkl")
quote_service = QuoteService(quote_provider)
etf_manager = ETFManager(quote_provider)

def runback_emotion():
    """回测ETF情绪指标并保存到数据库"""
    try:
        db_session = Session()
        
        # 获取90个交易日的K线数据
        klines = quote_service.get_klines('SPY.US', 90)
        
        # 为每个交易日计算情绪指标
        for kline in tqdm(klines, desc="计算情绪指标"):
            trade_date = kline['timestamp'].date()
            
            # 计算该日期的情绪指数
            etf_manager.calculate_all_emotions(trade_date)
        
        print("回刷完成")
        
    except Exception as e:
        print(f"回刷过程发生错误: {str(e)}")
    finally:
        db_session.close()

if __name__ == "__main__":
    runback_emotion()

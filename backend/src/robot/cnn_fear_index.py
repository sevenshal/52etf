import requests
from datetime import datetime
from ..core.database import Session, CNNFearGreedIndex
import pytz

class CNNFearGreedIndexScraper:
    def __init__(self):
        # 获取零时区时间
        et_timezone = pytz.timezone('UTC')
        et_now = datetime.now(et_timezone)
        self.url = f'https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{et_now.strftime("%Y-%m-%d")}'
        self.db_session = Session()

    def fetch_data(self):
        headers = {
            'accept': '*/*',
            'accept-language': 'zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7',
            'cache-control': 'no-cache',
            'origin': 'https://www.cnn.com',
            'pragma': 'no-cache',
            'referer': 'https://www.cnn.com/',
            'user-agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1'
        }
        response = requests.get(self.url, headers=headers)
        response.raise_for_status()  # 如果请求失败则抛出异常
        return response.json()

    def __save_to_db(self, data):
        # 提取主要的恐慌贪婪指数
        fear_and_greed = data['fear_and_greed']
        index_timestamp = datetime.fromtimestamp(data['fear_and_greed_historical']['timestamp']/1000)

        # 创建并保存记录
        new_record = CNNFearGreedIndex(
            date=index_timestamp.date(),
            index_value=fear_and_greed['score'],
            index_timestamp=index_timestamp,
            previous_close=fear_and_greed['previous_close'],
            previous_1_week=fear_and_greed['previous_1_week'],
            previous_1_month=fear_and_greed['previous_1_month'],
            previous_1_year=fear_and_greed['previous_1_year'],
            market_momentum=data['market_momentum_sp500']['score'],
            market_momentum_125=data['market_momentum_sp125']['score'],
            stock_price_strength=data['stock_price_strength']['score'],
            stock_price_breadth=data['stock_price_breadth']['score'],
            put_call_options=data['put_call_options']['score'],
            market_volatility_vix=data['market_volatility_vix']['score'],
            market_volatility_vix_50=data['market_volatility_vix_50']['score'],
            junk_bond_demand=data['junk_bond_demand']['score'],
            safe_haven_demand=data['safe_haven_demand']['score'],
            created_at=datetime.now()
        )
        self.db_session.merge(new_record)
        self.db_session.commit()

    def fetch_data_and_save(self):
        data = self.fetch_data()
        self.__save_to_db(data)
        return data

if __name__ == "__main__":
    scraper = CNNFearGreedIndexScraper()
    data = scraper.fetch_data_and_save()

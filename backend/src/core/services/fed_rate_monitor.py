import httpx
from bs4 import BeautifulSoup

class FedRateMonitorService:
    URL = 'https://cn.investing.com/central-banks/fed-rate-monitor'
    HEADERS = {
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'accept-language': 'zh-CN,zh;q=0.9',
        'cache-control': 'no-cache',
        'pragma': 'no-cache',
        'priority': 'u=0, i',
        'sec-ch-ua': '"Not)A;Brand";v="8", "Chromium";v="138", "Google Chrome";v="138"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"macOS"',
        'sec-fetch-dest': 'document',
        'sec-fetch-mode': 'navigate',
        'sec-fetch-site': 'none',
        'sec-fetch-user': '?1',
        'upgrade-insecure-requests': '1',
        'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36'
    }
    # SOCKS5代理配置
    PROXY = {
        'http://': 'socks5://127.0.0.1:7891',
        'https://': 'socks5://127.0.0.1:7891'
    }

    @staticmethod
    def fetch_data():
        try:
            # 添加代理参数
            with httpx.Client(proxies=FedRateMonitorService.PROXY) as client:
                response = client.get(FedRateMonitorService.URL, headers=FedRateMonitorService.HEADERS)
                response.raise_for_status()
                return FedRateMonitorService._parse_response(response.text)
        except httpx.HTTPStatusError as http_err:
            print(f"HTTP error occurred: {http_err}")
        except Exception as err:
            print(f"An error occurred: {err}")
        return []

    @staticmethod
    def _parse_response(html_content):
        soup = BeautifulSoup(html_content, 'html.parser')
        card_wrappers = soup.find_all('div', class_='cardWrapper')
        data = []

        for card_wrapper in card_wrappers:
            date = card_wrapper.find('div', class_='fedRateDate').text.strip()
            info_fed = card_wrapper.find('div', class_='infoFed')
            meeting_time = info_fed.find_all('i')[0].text.strip()
            futures_price = info_fed.find_all('i')[1].text.strip()

            table = card_wrapper.find('table', class_='fedRateTbl')
            rows = table.find_all('tr')[1:]  # Skip header row

            rate_info = []
            for row in rows:
                cells = row.find_all('td')
                rate_info.append({
                    'target_rate': cells[0].text.strip().split('\n')[0],
                    'current_probability': cells[1].text.strip(),
                    'previous_day_probability': cells[2].text.strip(),
                    'previous_week_probability': cells[3].text.strip()
                })

            data.append({
                'date': date,
                'meeting_time': meeting_time,
                'futures_price': futures_price,
                'rate_info': rate_info
            })

        return data

if __name__ == "__main__":
    service = FedRateMonitorService()
    result = service.fetch_data()
    for item in result:
        print(item)    

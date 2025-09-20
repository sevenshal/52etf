import httpx
from bs4 import BeautifulSoup
from diskcache import Cache
import os
from datetime import datetime, timedelta

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
    PROXY = {
        'http://': 'socks5://127.0.0.1:7891',
        'https://': 'socks5://127.0.0.1:7891'
    }
    CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "quant")
    CACHE = Cache(directory=CACHE_DIR)
    CACHE_TIMEOUT = 3600  # 1小时
    
    # FRED API 配置
    FRED_API_KEY = "f969b4eb2a07325467cffb3f100fa6ea"
    FRED_BASE_URL = "https://api.stlouisfed.org"

    @staticmethod
    def fetch_data(use_cache=True):
        cache_key = 'fed_rate_parsed_data'
        
        if use_cache:
            cached_data = FedRateMonitorService.CACHE.get(cache_key)
            if cached_data is not None:
                return cached_data

        try:
            html_content = FedRateMonitorService._fetch_html()
            if not html_content:
                return []
                
            data = FedRateMonitorService._parse_response(html_content)
            
            # 仅缓存解析后的数据
            FedRateMonitorService.CACHE.set(cache_key, data, expire=FedRateMonitorService.CACHE_TIMEOUT)
            
            return data
        except Exception as err:
            print(f"数据获取或解析失败: {err}")
            return []

    @staticmethod
    def _fetch_html():
        """单独的HTML获取方法，不涉及缓存和解析"""
        try:
            with httpx.Client(proxies=FedRateMonitorService.PROXY) as client:
                response = client.get(FedRateMonitorService.URL, headers=FedRateMonitorService.HEADERS)
                response.raise_for_status()
                return response.text
        except httpx.HTTPStatusError as http_err:
            print(f"HTTP请求错误: {http_err}")
        except Exception as err:
            print(f"网络请求异常: {err}")
        return None

    @staticmethod
    def _parse_response(html_content):
        # 保持原有解析逻辑不变
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

    @staticmethod
    def get_current_fed_rate(rate_type='both'):
        """
        获取美联储最新的联邦利率
        
        参数:
            rate_type (str): 利率类型
                - 'upper': 只获取上限利率
                - 'lower': 只获取下限利率  
                - 'both': 获取上限和下限利率（默认）
        
        返回格式: {
            'upper': float,           # 上限利率（如果请求）
            'lower': float,           # 下限利率（如果请求）
            'date': str,              # 查询日期
            'upper_date': str,        # 上限利率数据日期（如果请求）
            'lower_date': str,        # 下限利率数据日期（如果请求）
            'upper_raw': dict,        # 上限利率原始API响应（如果请求）
            'lower_raw': dict,        # 下限利率原始API响应（如果请求）
            'error': str              # 错误信息（如果有）
        }
        """
        # 验证参数
        if rate_type not in ['upper', 'lower', 'both']:
            return {
                'error': f"无效的rate_type参数: {rate_type}。支持的值: 'upper', 'lower', 'both'"
            }
        
        cache_key = f'current_fed_rate_{rate_type}'
        
        # 检查缓存
        cached_data = FedRateMonitorService.CACHE.get(cache_key)
        if cached_data is not None:
            return cached_data
        
        try:
            # 获取前一天的日期
            yesterday = datetime.now() - timedelta(days=1)
            date_str = yesterday.strftime('%Y-%m-%d')
            
            # 根据参数决定请求哪些数据
            requests_to_make = []
            if rate_type in ['upper', 'both']:
                requests_to_make.append(('upper', f"{FedRateMonitorService.FRED_BASE_URL}/fred/series/observations?series_id=DFEDTARU&file_type=json&observation_start={date_str}&output_type=1&api_key={FedRateMonitorService.FRED_API_KEY}"))
            
            if rate_type in ['lower', 'both']:
                requests_to_make.append(('lower', f"{FedRateMonitorService.FRED_BASE_URL}/fred/series/observations?series_id=DFEDTARL&file_type=json&observation_start={date_str}&output_type=1&api_key={FedRateMonitorService.FRED_API_KEY}"))
            
            # 执行请求
            responses = {}
            with httpx.Client(proxies=FedRateMonitorService.PROXY) as client:
                for rate_name, url in requests_to_make:
                    response = client.get(url)
                    response.raise_for_status()
                    responses[rate_name] = response.json()
            
            # 解析数据
            result = {
                'upper': None,
                'lower': None,
                'date': date_str,
                'upper_raw': None,
                'lower_raw': None
            }
            
            # 提取上限利率
            if 'upper' in responses:
                upper_data = responses['upper']
                result['upper_raw'] = upper_data
                if (upper_data.get('observations') and 
                    len(upper_data['observations']) > 0 and 
                    upper_data['observations'][0].get('value') and
                    upper_data['observations'][0].get('value') != '.'):
                    result['upper'] = float(upper_data['observations'][0]['value'])
                    result['upper_date'] = upper_data['observations'][0].get('date')
            
            # 提取下限利率
            if 'lower' in responses:
                lower_data = responses['lower']
                result['lower_raw'] = lower_data
                if (lower_data.get('observations') and 
                    len(lower_data['observations']) > 0 and 
                    lower_data['observations'][0].get('value') and
                    lower_data['observations'][0].get('value') != '.'):
                    result['lower'] = float(lower_data['observations'][0]['value'])
                    result['lower_date'] = lower_data['observations'][0].get('date')
            
            # 缓存结果（缓存30分钟）
            FedRateMonitorService.CACHE.set(cache_key, result, expire=1800)
            
            return result
            
        except Exception as err:
            print(f"获取联邦利率失败: {err}")
            return {
                'upper': None,
                'lower': None,
                'date': None,
                'error': str(err)
            }

if __name__ == "__main__":
    service = FedRateMonitorService()
    
    # 测试联邦利率监控数据
    print("=== 联邦利率监控数据 ===")
    result = service.fetch_data()
    for item in result[:1]:  # 只打印第一条结果
        print(f"日期: {item['date']}, 会议时间: {item['meeting_time']}")
    
    # 测试获取当前联邦利率 - 全部
    print("\n=== 当前联邦利率 (全部) ===")
    fed_rate = service.get_current_fed_rate('both')
    print(f"查询日期: {fed_rate['date']}")
    print(f"上限利率: {fed_rate['upper']} (日期: {fed_rate.get('upper_date', 'N/A')})")
    print(f"下限利率: {fed_rate['lower']} (日期: {fed_rate.get('lower_date', 'N/A')})")
    if 'error' in fed_rate:
        print(f"错误: {fed_rate['error']}")
    
    # 测试只获取上限利率
    print("\n=== 只获取上限利率 ===")
    upper_only = service.get_current_fed_rate('upper')
    print(f"上限利率: {upper_only['upper']} (日期: {upper_only.get('upper_date', 'N/A')})")
    print(f"下限利率: {upper_only['lower']} (应该为None)")
    
    # 测试只获取下限利率
    print("\n=== 只获取下限利率 ===")
    lower_only = service.get_current_fed_rate('lower')
    print(f"上限利率: {lower_only['upper']} (应该为None)")
    print(f"下限利率: {lower_only['lower']} (日期: {lower_only.get('lower_date', 'N/A')})")
    
    # 显示原始数据示例
    if fed_rate.get('upper_raw'):
        print(f"\n上限利率原始数据示例:")
        upper_obs = fed_rate['upper_raw'].get('observations', [])
        if upper_obs:
            print(f"  - 日期: {upper_obs[0].get('date')}")
            print(f"  - 值: {upper_obs[0].get('value')}")
            print(f"  - 实时开始: {upper_obs[0].get('realtime_start')}")
            print(f"  - 实时结束: {upper_obs[0].get('realtime_end')}")
    
    # 测试缓存
    print("\n=== 缓存测试 ===")
    cached_result = service.fetch_data()
    print(f"监控数据缓存条目数: {len(cached_result)}")
    
    cached_fed_rate = service.get_current_fed_rate()
    print(f"联邦利率缓存: {cached_fed_rate}")    

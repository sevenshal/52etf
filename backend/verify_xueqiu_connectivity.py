import asyncio
import sys
import os

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), 'src'))

from app.api.snowball import fetch_xueqiu_holdings, fetch_xueqiu_cube_info, fetch_xueqiu_quotes

async def main():
    print("--- Testing Xueqiu API Connectivity ---")
    
    # 1. Test Cube Info
    symbol = "ZH3186221"
    print(f"\n1. Fetching Cube Info for {symbol}...")
    info = await fetch_xueqiu_cube_info(symbol)
    if info:
        print(f"SUCCESS: Name={info.get('name')}, ID={info.get('id')}")
    else:
        print("FAILED: Could not fetch cube info.")

    # 2. Test Holdings
    print(f"\n2. Fetching Holdings for {symbol}...")
    holdings = await fetch_xueqiu_holdings(symbol)
    if holdings:
        print(f"SUCCESS: Found {len(holdings)} holdings.")
        print(f"Sample: {holdings[0]}")
    else:
        print("FAILED: Could not fetch holdings (or empty).")
        
    # 3. Test Quotes
    test_symbols = ["SZ000858", "SH600519"]
    print(f"\n3. Fetching Quotes for {test_symbols}...")
    quotes = await fetch_xueqiu_quotes(test_symbols)
    if quotes:
        print("SUCCESS: Quotes received.")
        for s, p in quotes.items():
            print(f"  {s}: {p}")
    else:
        print("FAILED: Could not fetch quotes.")

if __name__ == "__main__":
    asyncio.run(main())

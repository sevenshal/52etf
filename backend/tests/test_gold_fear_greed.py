import os
import subprocess
import sys
import textwrap
from io import BytesIO

import pandas as pd
import pytest

from src.robot.gold_fear_greed_input_sync import GoldFearGreedInputSync


def test_gold_five_factor_history_and_backfill(tmp_path):
    sqlite_path = tmp_path / "gold.db"
    duckdb_path = tmp_path / "gold.duckdb"
    code = textwrap.dedent(
        """
        from datetime import date, datetime
        import numpy as np
        import pandas as pd
        import duckdb

        from src.core.database import Session, ETFPutCallRatio, GoldFearGreedInput
        from src.core.services.gold_fear_greed_service import GoldFearGreedCalculator

        days = pd.bdate_range('2022-01-03', periods=620)
        con = duckdb.connect(os.environ['ANALYTICS_DB_PATH'])
        con.execute('CREATE TABLE us_stock_daily (symbol VARCHAR, trade_date DATE, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE, turnover DOUBLE)')
        frame = pd.DataFrame({
            'symbol': 'GLD.US', 'trade_date': days.date,
            'open': 150 + np.arange(len(days)) * .05,
            'high': 151 + np.arange(len(days)) * .05,
            'low': 149 + np.arange(len(days)) * .05,
            'close': 150 + np.arange(len(days)) * .05 + np.sin(np.arange(len(days)) / 9),
            'volume': 1_000_000 + np.arange(len(days)) * 100,
            'turnover': 150_000_000 + np.arange(len(days)) * 1000,
        })
        con.register('f', frame); con.execute('INSERT INTO us_stock_daily SELECT * FROM f'); con.close()

        db = Session()
        for index, ts in enumerate(days):
            day = ts.date()
            db.add(ETFPutCallRatio(symbol='GLD', date=day, put_call_volume_ratio=.6 + .1 * np.sin(index / 13)))
            db.add(GoldFearGreedInput(
                date=day, real_yield_10y=1.5 + .2 * np.sin(index / 17),
                broad_dollar_index=100 + .01 * index + np.sin(index / 20),
                cot_managed_money_long=150000 + index * 20,
                cot_managed_money_short=90000 + index * 5,
                cot_open_interest=500000 + index * 30,
                gold_etf_holdings_tonnes=800 + index * .1 + np.sin(index / 11),
            ))
        db.commit(); db.close(); Session.remove()

        # 守猪逮兔取数走外部 HTTP，子进程里 stub 掉，验证缺失时主计算不受影响
        GoldFearGreedCalculator._fetch_szdt_emotion_series = staticmethod(
            lambda start, end: pd.Series(dtype=float)
        )
        GoldFearGreedCalculator._fetch_gvz_series = staticmethod(
            lambda start, end: pd.Series(20 + np.sin(np.arange(len(days)) / 15), index=days)
        )
        calc = GoldFearGreedCalculator()
        result = calc.backfill_to_db(start_date=days[0].date(), end_date=days[-1].date(), score_window=60, min_periods=40, history_days=620)
        assert result['saved'] > 300
        assert result['component_count'] == 7
        db = Session()
        latest = db.execute(__import__('sqlalchemy').text("SELECT components FROM etf_fear_greed_clone_history WHERE symbol='GLD.US' ORDER BY date DESC LIMIT 1")).scalar_one()
        if isinstance(latest, str):
            latest = __import__('json').loads(latest)
        assert len(latest) == 7
        assert set(latest) == {'gold_price_momentum', 'gold_options', 'gold_implied_volatility', 'cot_positioning', 'gold_etf_demand', 'real_yield_dollar', 'szdt_fear_greed'}
        assert latest['gold_implied_volatility']['raw_value'] > 0
        # 外部来源缺失时分量字段为空，但总分仍是六因子均值（不阻塞历史）
        assert latest['szdt_fear_greed']['score'] is None
        szdt_col = db.execute(__import__('sqlalchemy').text("SELECT szdt_fear_greed_score FROM etf_fear_greed_clone_history WHERE symbol='GLD.US' ORDER BY date DESC LIMIT 1")).scalar_one()
        assert szdt_col is None
        """
    )
    env = os.environ.copy()
    env["QUANT_SQLITE_PATH"] = str(sqlite_path)
    env["ANALYTICS_DB_PATH"] = str(duckdb_path)
    subprocess.run([sys.executable, "-c", "import os\n" + code], env=env, check=True)


def test_gld_is_after_dia_in_frontend_taxonomy():
    content = open("../frontend/src/pages/fear/components/fearMarketTaxonomy.js", encoding="utf-8").read()
    assert content.index("'DIA.US'") < content.index("'GLD.US'")


def test_world_gold_council_gld_holdings_workbook_is_parsed(monkeypatch):
    workbook = BytesIO()
    pd.DataFrame({
        "Date": ["2026-08-20", "US Holiday"],
        "Tonnes of Gold": [950.5, "US Holiday"],
        "Total Ounces of Gold in the Trust": [30_559_000, "US Holiday"],
        "Ounces of Gold per Share": [0.098577419, "US Holiday"],
    }).to_excel(workbook, index=False, sheet_name="US GLD Historical Archive")

    class Response:
        def __init__(self, text="", content=b""):
            self.text = text
            self.content = content

    syncer = GoldFearGreedInputSync()
    monkeypatch.setattr(syncer, "_get", lambda url, **kwargs: Response(content=workbook.getvalue()))
    frame = syncer._fetch_gold_etf_holdings()
    assert len(frame) == 1
    assert frame.iloc[0]["gold_etf_holdings_tonnes"] == 950.5
    assert frame.iloc[0]["gold_etf_shares"] == pytest.approx(310_000_000, abs=2)
    syncer.close()


def test_cot_rows_become_available_three_days_after_report(monkeypatch):
    class Response:
        @staticmethod
        def json():
            return [{
                "report_date_as_yyyy_mm_dd": "2026-08-18T00:00:00.000",
                "m_money_positions_long_all": "150,000",
                "m_money_positions_short_all": "80,000",
                "open_interest_all": "500,000",
            }]

    syncer = GoldFearGreedInputSync()
    monkeypatch.setattr(syncer, "_get", lambda *args, **kwargs: Response())
    frame = syncer._fetch_cot(pd.Timestamp("2026-08-01").date(), pd.Timestamp("2026-08-31").date())
    assert frame.iloc[0]["date"].isoformat() == "2026-08-21"
    syncer.close()


def test_szdt_emotion_series_linear_mapping(monkeypatch):
    """守猪逮兔 US.GLD -100..100 线性映射为 0-100。"""
    import datetime as dt

    import src.core.services.szdt as szdt_module
    from src.core.services.gold_fear_greed_service import GoldFearGreedCalculator

    class FakeSZDTService:
        def get_etf_emotion_history_sync(self, code):
            assert code == "US.GLD"
            return {
                "status": 1,
                "data": [
                    {"date": "2026-10-08", "score": -100},
                    {"date": "2026-10-09", "score": 0},
                    {"date": "2026-10-12", "score": -83},
                ],
            }

    monkeypatch.setattr(szdt_module, "SZDTService", FakeSZDTService)
    series = GoldFearGreedCalculator._fetch_szdt_emotion_series(
        dt.date(2026, 10, 1), dt.date(2026, 10, 31)
    )

    assert series.loc[pd.Timestamp("2026-10-08")] == pytest.approx(0.0)
    assert series.loc[pd.Timestamp("2026-10-09")] == pytest.approx(50.0)
    assert series.loc[pd.Timestamp("2026-10-12")] == pytest.approx(8.5)


def test_szdt_emotion_series_failure_returns_empty(monkeypatch):
    """守猪逮兔取数失败返回空序列，不抛异常阻塞主计算。"""
    import datetime as dt

    import src.core.services.szdt as szdt_module
    from src.core.services.gold_fear_greed_service import GoldFearGreedCalculator

    class FakeSZDTService:
        def get_etf_emotion_history_sync(self, code):
            raise RuntimeError("network down")

    monkeypatch.setattr(szdt_module, "SZDTService", FakeSZDTService)
    series = GoldFearGreedCalculator._fetch_szdt_emotion_series(
        dt.date(2026, 10, 1), dt.date(2026, 10, 31)
    )
    assert series.empty

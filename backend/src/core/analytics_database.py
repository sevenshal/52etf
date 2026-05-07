import os
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import Column, Date, DateTime, Float, String, create_engine, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool


ANALYTICS_DB_PATH = os.getenv("ANALYTICS_DB_PATH", "/var/lib/quant_robot/analytics.duckdb")
ANALYTICS_DB_DIR = os.path.dirname(ANALYTICS_DB_PATH)
if ANALYTICS_DB_DIR:
    os.makedirs(ANALYTICS_DB_DIR, exist_ok=True)

ANALYTICS_TABLE_NAMES = frozenset(
    {
        "a_stock_basic",
        "a_stock_income",
        "a_stock_index_daily",
        "a_stock_market_daily",
        "a_stock_name_changes",
    }
)

analytics_engine = create_engine(f"duckdb:///{ANALYTICS_DB_PATH}", poolclass=NullPool)
AnalyticsBase = declarative_base()
AnalyticsSession = scoped_session(sessionmaker(bind=analytics_engine))


class AStockBasic(AnalyticsBase):
    """Tushare A股公司基础信息快照，存放在 DuckDB 分析库。"""
    __tablename__ = "a_stock_basic"

    ts_code = Column(String(16), primary_key=True)
    symbol = Column(String(16))
    name = Column(String(64))
    area = Column(String(64))
    industry = Column(String(64))
    market = Column(String(64))
    exchange = Column(String(16))
    list_date = Column(Date)
    delist_date = Column(Date)
    list_status = Column(String(8))
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


class AStockNameChange(AnalyticsBase):
    """Tushare A股曾用名/ST变更记录，存放在 DuckDB 分析库。"""
    __tablename__ = "a_stock_name_changes"

    id = Column(String(80), primary_key=True)
    ts_code = Column(String(16), nullable=False)
    name = Column(String(64))
    start_date = Column(Date)
    end_date = Column(Date)
    change_reason = Column(String(64))
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


class AStockIncome(AnalyticsBase):
    """Tushare A股利润表/收入表缓存，存放在 DuckDB 分析库。"""
    __tablename__ = "a_stock_income"

    id = Column(String(80), primary_key=True)
    ts_code = Column(String(16), nullable=False)
    end_date = Column(Date, nullable=False)
    ann_date = Column(Date)
    operate_income = Column(Float)
    rd_exp = Column(Float)
    report_type = Column(String(16))
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


class AStockMarketDaily(AnalyticsBase):
    """Tushare A股全市场日行情与估值截面缓存，存放在 DuckDB 分析库。"""
    __tablename__ = "a_stock_market_daily"

    trade_date = Column(Date, primary_key=True)
    ts_code = Column(String(16), primary_key=True)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    pre_close = Column(Float)
    change = Column(Float)
    pct_chg = Column(Float)
    vol = Column(Float)
    amount = Column(Float)
    total_mv = Column(Float)
    circ_mv = Column(Float)
    float_share = Column(Float)
    total_share = Column(Float)
    turnover_rate = Column(Float)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


class AStockIndexDaily(AnalyticsBase):
    """A股指数日行情缓存，存放在 DuckDB 分析库。"""
    __tablename__ = "a_stock_index_daily"

    ts_code = Column(String(16), primary_key=True)
    trade_date = Column(Date, primary_key=True)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    pre_close = Column(Float)
    change = Column(Float)
    pct_chg = Column(Float)
    vol = Column(Float)
    amount = Column(Float)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


def ensure_analytics_schema():
    AnalyticsBase.metadata.create_all(analytics_engine)
    index_sqls = [
        "CREATE INDEX IF NOT EXISTS idx_a_stock_basic_industry_status ON a_stock_basic(industry, list_status)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_income_symbol_ann ON a_stock_income(ts_code, ann_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_income_symbol_end ON a_stock_income(ts_code, end_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_income_ann ON a_stock_income(ann_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_name_changes_symbol_dates ON a_stock_name_changes(ts_code, start_date, end_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_market_daily_symbol_date ON a_stock_market_daily(ts_code, trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_market_daily_date_circmv ON a_stock_market_daily(trade_date, circ_mv)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_index_daily_date ON a_stock_index_daily(ts_code, trade_date)",
    ]
    with analytics_engine.begin() as conn:
        for sql in index_sqls:
            conn.execute(text(sql))


ensure_analytics_schema()


@contextmanager
def get_analytics_db_ctx():
    db = AnalyticsSession()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        AnalyticsSession.remove()

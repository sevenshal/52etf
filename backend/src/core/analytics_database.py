import os
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import Column, Date, DateTime, Float, String, create_engine, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

from .duckdb_utils import (
    ANALYTICS_DB_PATH,
    DUCKDB_CONFIG_MISMATCH_MESSAGE,
    connect_duckdb,
    connect_duckdb_engine,
    is_duckdb_config_mismatch,
)

ANALYTICS_DB_DIR = os.path.dirname(ANALYTICS_DB_PATH)
if ANALYTICS_DB_DIR:
    os.makedirs(ANALYTICS_DB_DIR, exist_ok=True)

ANALYTICS_TABLE_NAMES = frozenset(
    {
        "a_stock_basic",
        "a_stock_adj_factor",
        "a_stock_income",
        "a_stock_fund_basic",
        "a_stock_fund_daily",
        "a_stock_fund_adj_factor",
        "a_stock_fund_daily_qfq",
        "a_stock_index_daily",
        "a_stock_index_weight",
        "a_stock_market_daily",
        "a_stock_market_daily_qfq",
        "a_stock_fund_flow_daily",
        "a_stock_name_changes",
        "a_stock_chinabond_yield_curve_daily",
        "a_stock_chinabond_yield_curve_defs",
        "a_stock_option_basic",
        "a_stock_option_daily",
        "a_stock_repo_daily",
        "us_stock_daily",
        "xueqiu_cube_holdings_snapshots",
    }
)

analytics_engine = create_engine(
    "duckdb:///:memory:",
    creator=lambda: connect_duckdb_engine(ANALYTICS_DB_PATH, prefer_read_only=False),
    poolclass=NullPool,
)
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


class AStockFundFlowDaily(AnalyticsBase):
    """东财 A股个股日级主力资金流快照，存放在 DuckDB 分析库。"""
    __tablename__ = "a_stock_fund_flow_daily"

    trade_date = Column(Date, primary_key=True)
    ts_code = Column(String(16), primary_key=True)
    symbol = Column(String(16), index=True)
    name = Column(String(64))
    close = Column(Float)
    pct_chg = Column(Float)
    main_net = Column(Float)
    main_net_pct = Column(Float)
    super_net = Column(Float)
    super_net_pct = Column(Float)
    large_net = Column(Float)
    large_net_pct = Column(Float)
    mid_net = Column(Float)
    mid_net_pct = Column(Float)
    small_net = Column(Float)
    small_net_pct = Column(Float)
    source = Column(String(32), default="eastmoney_push2", nullable=False)
    source_updated_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


class AStockAdjFactor(AnalyticsBase):
    """Tushare A股股票复权因子，raw 行情保持不变，qfq view 使用该表换算前复权价格。"""
    __tablename__ = "a_stock_adj_factor"

    ts_code = Column(String(16), primary_key=True)
    trade_date = Column(Date, primary_key=True)
    adj_factor = Column(Float)
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


class AStockFundDaily(AnalyticsBase):
    """Tushare A股ETF/场内基金日行情缓存，存放在 DuckDB 分析库。"""
    __tablename__ = "a_stock_fund_daily"

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


class AStockFundBasic(AnalyticsBase):
    """Tushare A股ETF/场内基金基础信息缓存，存放在 DuckDB 分析库。"""
    __tablename__ = "a_stock_fund_basic"

    ts_code = Column(String(16), primary_key=True)
    name = Column(String(128))
    market = Column(String(64))
    list_date = Column(Date)
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


class AStockFundAdjFactor(AnalyticsBase):
    """Tushare A股ETF/场内基金复权因子，qfq view 使用该表换算前复权价格。"""
    __tablename__ = "a_stock_fund_adj_factor"

    ts_code = Column(String(16), primary_key=True)
    trade_date = Column(Date, primary_key=True)
    adj_factor = Column(Float)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


class AStockIndexWeight(AnalyticsBase):
    """Tushare 指数历史成分权重，存放在 DuckDB 分析库。"""
    __tablename__ = "a_stock_index_weight"

    index_code = Column(String(16), primary_key=True)
    trade_date = Column(Date, primary_key=True)
    con_code = Column(String(16), primary_key=True)
    weight = Column(Float)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


class AStockOptionBasic(AnalyticsBase):
    """Tushare A股ETF期权合约基础信息，存放在 DuckDB 分析库。"""
    __tablename__ = "a_stock_option_basic"

    ts_code = Column(String(32), primary_key=True)
    exchange = Column(String(16))
    name = Column(String(128))
    per_unit = Column(Float)
    opt_code = Column(String(32), index=True)
    opt_type = Column(String(32))
    call_put = Column(String(8), index=True)
    exercise_type = Column(String(32))
    exercise_price = Column(Float)
    s_month = Column(String(16))
    maturity_date = Column(Date)
    list_price = Column(Float)
    list_date = Column(Date)
    delist_date = Column(Date, index=True)
    last_edate = Column(Date)
    last_ddate = Column(Date)
    quote_unit = Column(String(32))
    min_price_chg = Column(Float)
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


class AStockOptionDaily(AnalyticsBase):
    """Tushare A股ETF期权日行情，存放在 DuckDB 分析库。"""
    __tablename__ = "a_stock_option_daily"

    trade_date = Column(Date, primary_key=True)
    ts_code = Column(String(32), primary_key=True)
    exchange = Column(String(16))
    pre_settle = Column(Float)
    pre_close = Column(Float)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    settle = Column(Float)
    vol = Column(Float)
    amount = Column(Float)
    oi = Column(Float)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


class AStockRepoDaily(AnalyticsBase):
    """Tushare 债券回购日行情，存放在 DuckDB 分析库。"""
    __tablename__ = "a_stock_repo_daily"

    trade_date = Column(Date, primary_key=True)
    ts_code = Column(String(32), primary_key=True)
    repo_maturity = Column(String(32), index=True)
    pre_close = Column(Float)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    weight = Column(Float)
    weight_r = Column(Float)
    amount = Column(Float)
    num = Column(Float)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


class AStockChinaBondYieldCurveDef(AnalyticsBase):
    """中债收益率曲线定义，存放在 DuckDB 分析库。"""
    __tablename__ = "a_stock_chinabond_yield_curve_defs"

    curve_id = Column(String(64), primary_key=True)
    curve_name = Column(String(128), nullable=False)
    category = Column(String(64), index=True)
    rating = Column(String(16), index=True)
    pair_key = Column(String(64), index=True)
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


class AStockChinaBondYieldCurveDaily(AnalyticsBase):
    """中债收益率曲线每日期限点，存放在 DuckDB 分析库。"""
    __tablename__ = "a_stock_chinabond_yield_curve_daily"

    trade_date = Column(Date, primary_key=True)
    curve_id = Column(String(64), primary_key=True)
    curve_name = Column(String(128))
    term = Column(Float, primary_key=True)
    yield_rate = Column(Float)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


class USStockDaily(AnalyticsBase):
    """LongPort 美股日K行情，存放在 DuckDB 分析库。"""
    __tablename__ = "us_stock_daily"

    symbol = Column(String(32), primary_key=True)
    trade_date = Column(Date, primary_key=True)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Float)
    turnover = Column(Float)
    adjust_type = Column(String(32))
    period = Column(String(16))
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
        "CREATE INDEX IF NOT EXISTS idx_a_stock_fund_flow_daily_symbol_date ON a_stock_fund_flow_daily(ts_code, trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_fund_flow_daily_date_main ON a_stock_fund_flow_daily(trade_date, main_net)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_adj_factor_symbol_date ON a_stock_adj_factor(ts_code, trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_adj_factor_date_symbol ON a_stock_adj_factor(trade_date, ts_code)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_fund_basic_name ON a_stock_fund_basic(name)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_fund_daily_symbol_date ON a_stock_fund_daily(ts_code, trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_fund_daily_date_symbol ON a_stock_fund_daily(trade_date, ts_code)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_fund_adj_factor_symbol_date ON a_stock_fund_adj_factor(ts_code, trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_fund_adj_factor_date_symbol ON a_stock_fund_adj_factor(trade_date, ts_code)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_index_daily_date ON a_stock_index_daily(ts_code, trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_index_weight_index_date ON a_stock_index_weight(index_code, trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_index_weight_constituent ON a_stock_index_weight(con_code, trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_option_basic_underlying ON a_stock_option_basic(opt_code, call_put, delist_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_option_daily_date_exchange ON a_stock_option_daily(trade_date, exchange)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_option_daily_symbol_date ON a_stock_option_daily(ts_code, trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_repo_daily_maturity_date ON a_stock_repo_daily(repo_maturity, trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_chinabond_curve_defs_pair ON a_stock_chinabond_yield_curve_defs(pair_key, rating)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_chinabond_curve_daily_curve_date ON a_stock_chinabond_yield_curve_daily(curve_id, trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_chinabond_curve_daily_date_term ON a_stock_chinabond_yield_curve_daily(trade_date, term)",
        "CREATE INDEX IF NOT EXISTS idx_us_stock_daily_symbol_date ON us_stock_daily(symbol, trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_us_stock_daily_date_symbol ON us_stock_daily(trade_date, symbol)",
    ]
    view_sqls = [
        """
        CREATE OR REPLACE VIEW a_stock_market_daily_qfq AS
        WITH anchor_factors AS (
            SELECT ts_code, adj_factor AS anchor_adj_factor
            FROM (
                SELECT
                    ts_code,
                    adj_factor,
                    ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) AS rn
                FROM a_stock_adj_factor
                WHERE adj_factor IS NOT NULL AND adj_factor > 0
            )
            WHERE rn = 1
        )
        SELECT
            m.ts_code,
            m.ts_code AS symbol,
            m.trade_date,
            CAST(m.open * f.adj_factor / a.anchor_adj_factor AS DOUBLE) AS open,
            CAST(m.high * f.adj_factor / a.anchor_adj_factor AS DOUBLE) AS high,
            CAST(m.low * f.adj_factor / a.anchor_adj_factor AS DOUBLE) AS low,
            CAST(m.close * f.adj_factor / a.anchor_adj_factor AS DOUBLE) AS close,
            CAST(m.pre_close * f.adj_factor / a.anchor_adj_factor AS DOUBLE) AS pre_close,
            CAST(m.change * f.adj_factor / a.anchor_adj_factor AS DOUBLE) AS change,
            m.pct_chg,
            m.vol,
            m.vol AS volume,
            m.amount,
            m.amount AS turnover,
            m.total_mv,
            m.circ_mv,
            m.float_share,
            m.total_share,
            m.turnover_rate,
            f.adj_factor,
            a.anchor_adj_factor,
            'qfq' AS adjust_type,
            m.created_at,
            GREATEST(m.updated_at, f.updated_at) AS updated_at
        FROM a_stock_market_daily m
        JOIN a_stock_adj_factor f
          ON m.ts_code = f.ts_code
         AND m.trade_date = f.trade_date
        JOIN anchor_factors a
          ON m.ts_code = a.ts_code
        WHERE f.adj_factor IS NOT NULL
          AND f.adj_factor > 0
          AND a.anchor_adj_factor IS NOT NULL
          AND a.anchor_adj_factor > 0
        """,
        """
        CREATE OR REPLACE VIEW a_stock_fund_daily_qfq AS
        WITH anchor_factors AS (
            SELECT ts_code, adj_factor AS anchor_adj_factor
            FROM (
                SELECT
                    ts_code,
                    adj_factor,
                    ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) AS rn
                FROM a_stock_fund_adj_factor
                WHERE adj_factor IS NOT NULL AND adj_factor > 0
            )
            WHERE rn = 1
        )
        SELECT
            d.ts_code,
            d.ts_code AS symbol,
            d.trade_date,
            CAST(d.open * f.adj_factor / a.anchor_adj_factor AS DOUBLE) AS open,
            CAST(d.high * f.adj_factor / a.anchor_adj_factor AS DOUBLE) AS high,
            CAST(d.low * f.adj_factor / a.anchor_adj_factor AS DOUBLE) AS low,
            CAST(d.close * f.adj_factor / a.anchor_adj_factor AS DOUBLE) AS close,
            CAST(d.pre_close * f.adj_factor / a.anchor_adj_factor AS DOUBLE) AS pre_close,
            CAST(d.change * f.adj_factor / a.anchor_adj_factor AS DOUBLE) AS change,
            d.pct_chg,
            d.vol,
            d.vol AS volume,
            d.amount,
            d.amount AS turnover,
            f.adj_factor,
            a.anchor_adj_factor,
            'qfq' AS adjust_type,
            d.created_at,
            GREATEST(d.updated_at, f.updated_at) AS updated_at
        FROM a_stock_fund_daily d
        JOIN a_stock_fund_adj_factor f
          ON d.ts_code = f.ts_code
         AND d.trade_date = f.trade_date
        JOIN anchor_factors a
          ON d.ts_code = a.ts_code
        WHERE f.adj_factor IS NOT NULL
          AND f.adj_factor > 0
          AND a.anchor_adj_factor IS NOT NULL
          AND a.anchor_adj_factor > 0
        """,
    ]
    with analytics_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS us_stock_basic"))
        for sql in index_sqls:
            conn.execute(text(sql))
        for sql in view_sqls:
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

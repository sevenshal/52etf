import os
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import Boolean, Column, Date, DateTime, Float, Integer, String, Text, create_engine, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

from .duckdb_utils import (
    ANALYTICS_DB_PATH,
    connect_duckdb_engine,
)

ANALYTICS_DB_DIR = os.path.dirname(ANALYTICS_DB_PATH)
if ANALYTICS_DB_DIR:
    os.makedirs(ANALYTICS_DB_DIR, exist_ok=True)

ANALYTICS_TABLE_NAMES = frozenset(
    {
        "a_stock_basic",
        "a_stock_adj_factor",
        "a_stock_income",
        "a_stock_balancesheet",
        "a_stock_cashflow",
        "a_stock_fina_indicator",
        "a_stock_report_rc",
        "a_stock_fund_basic",
        "a_stock_fund_daily",
        "a_stock_fund_adj_factor",
        "a_stock_fund_daily_qfq",
        "a_stock_index_daily",
        "a_stock_index_weight",
        "a_stock_market_daily",
        "a_stock_market_daily_qfq",
        "a_stock_minute_bar",
        "a_stock_minute_bar_qfq",
        "chan_scan_run",
        "chan_scan_signal",
        "a_stock_fund_flow_daily",
        "a_stock_name_changes",
        "a_stock_chinabond_yield_curve_daily",
        "a_stock_chinabond_yield_curve_defs",
        "a_stock_option_basic",
        "a_stock_option_daily",
        "a_stock_repo_daily",
        "a_stock_ths_member",
        "a_stock_ths_daily",
        "hk_stock_basic",
        "hk_stock_daily",
        "hk_stock_daily_qfq",
        "hk_index_daily",
        "hk_index_weight_snapshot",
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


class AStockTHSMember(AnalyticsBase):
    """同花顺行业/概念/主题板块成分缓存。板块目录继续由主库统一维护。"""
    __tablename__ = "a_stock_ths_member"

    ths_code = Column(String(24), primary_key=True)
    con_code = Column(String(16), primary_key=True)
    con_name = Column(String(128))
    weight = Column(Float)
    in_date = Column(Date)
    out_date = Column(Date)
    is_new = Column(String(8))
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


class AStockTHSDaily(AnalyticsBase):
    """同花顺行业/概念/主题板块日行情。"""
    __tablename__ = "a_stock_ths_daily"

    ths_code = Column(String(24), primary_key=True)
    trade_date = Column(Date, primary_key=True)
    open = Column(Float)
    close = Column(Float)
    high = Column(Float)
    low = Column(Float)
    pre_close = Column(Float)
    avg_price = Column(Float)
    change = Column(Float)
    pct_change = Column(Float)
    vol = Column(Float)
    turnover_rate = Column(Float)
    total_mv = Column(Float)
    float_mv = Column(Float)
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


class AStockIncome(AnalyticsBase):
    """Tushare A股利润表/收入表缓存，存放在 DuckDB 分析库。"""
    __tablename__ = "a_stock_income"

    id = Column(String(80), primary_key=True)
    ts_code = Column(String(16), nullable=False)
    end_date = Column(Date, nullable=False)
    ann_date = Column(Date)
    rd_exp = Column(Float)
    report_type = Column(String(16))
    revenue = Column(Float)
    n_income_attr_p = Column(Float)
    operate_profit = Column(Float)
    total_profit = Column(Float)
    total_cogs = Column(Float)
    basic_eps = Column(Float)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


class AStockBalanceSheet(AnalyticsBase):
    """Tushare A股资产负债表缓存，存放在 DuckDB 分析库。"""
    __tablename__ = "a_stock_balancesheet"

    id = Column(String(80), primary_key=True)
    ts_code = Column(String(16), nullable=False)
    end_date = Column(Date, nullable=False)
    ann_date = Column(Date)
    report_type = Column(String(16))
    comp_type = Column(String(8))
    total_assets = Column(Float)
    total_liab = Column(Float)
    total_cur_assets = Column(Float)
    total_cur_liab = Column(Float)
    total_hldr_eqy_exc_min_int = Column(Float)
    money_cap = Column(Float)
    accounts_receiv = Column(Float)
    inventories = Column(Float)
    goodwill = Column(Float)
    fix_assets = Column(Float)
    lt_borr = Column(Float)
    st_borr = Column(Float)
    notes_payable = Column(Float)
    acct_payable = Column(Float)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


class AStockCashFlow(AnalyticsBase):
    """Tushare A股现金流量表缓存，存放在 DuckDB 分析库。"""
    __tablename__ = "a_stock_cashflow"

    id = Column(String(80), primary_key=True)
    ts_code = Column(String(16), nullable=False)
    end_date = Column(Date, nullable=False)
    ann_date = Column(Date)
    report_type = Column(String(16))
    net_profit = Column(Float)
    n_cashflow_act = Column(Float)
    c_pay_acq_const_fiolta = Column(Float)
    free_cashflow = Column(Float)
    n_cashflow_inv_act = Column(Float)
    n_cash_flows_fnc_act = Column(Float)
    c_fr_sale_sg = Column(Float)
    end_bal_cash = Column(Float)
    n_incr_cash_cash_equ = Column(Float)
    c_pay_dist_dpcp_int_exp = Column(Float)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


class AStockFinaIndicator(AnalyticsBase):
    """Tushare A股财务指标缓存(ROE/毛利率/负债率等)，存放在 DuckDB 分析库。"""
    __tablename__ = "a_stock_fina_indicator"

    id = Column(String(80), primary_key=True)
    ts_code = Column(String(16), nullable=False)
    end_date = Column(Date, nullable=False)
    ann_date = Column(Date)
    eps = Column(Float)
    dt_eps = Column(Float)
    bps = Column(Float)
    ocfps = Column(Float)
    roe = Column(Float)
    roe_waa = Column(Float)
    roe_dt = Column(Float)
    roa = Column(Float)
    roic = Column(Float)
    grossprofit_margin = Column(Float)
    netprofit_margin = Column(Float)
    debt_to_assets = Column(Float)
    current_ratio = Column(Float)
    quick_ratio = Column(Float)
    profit_dedt = Column(Float)
    extra_item = Column(Float)
    netprofit_yoy = Column(Float)
    dt_netprofit_yoy = Column(Float)
    or_yoy = Column(Float)
    op_yoy = Column(Float)
    ocf_to_or = Column(Float)
    ocf_to_debt = Column(Float)
    interestdebt = Column(Float)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


class AStockReportRc(AnalyticsBase):
    """Tushare A股券商卖方研报盈利预测/评级/目标价明细，存放在 DuckDB 分析库。"""
    __tablename__ = "a_stock_report_rc"

    id = Column(String(80), primary_key=True)
    ts_code = Column(String(16), nullable=False)
    name = Column(String(64))
    report_date = Column(Date, nullable=False)
    report_title = Column(String(512))
    report_type = Column(String(64))
    classify = Column(String(64))
    org_name = Column(String(128))
    author_name = Column(String(256))
    quarter = Column(String(16))
    op_rt = Column(Float)
    op_pr = Column(Float)
    tp = Column(Float)
    np = Column(Float)
    eps = Column(Float)
    pe = Column(Float)
    rd = Column(Float)
    roe = Column(Float)
    ev_ebitda = Column(Float)
    rating = Column(String(64))
    max_price = Column(Float)
    min_price = Column(Float)
    imp_dg = Column(String(64))
    create_time = Column(DateTime)
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
    volume_ratio = Column(Float)
    pe = Column(Float)
    pe_ttm = Column(Float)
    pb = Column(Float)
    dv_ratio = Column(Float)
    dv_ttm = Column(Float)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


class AStockMinuteBar(AnalyticsBase):
    """Rolling unadjusted A-share one-minute bars from Tushare."""
    __tablename__ = "a_stock_minute_bar"

    ts_code = Column(String(16), primary_key=True)
    trade_time = Column(DateTime, primary_key=True)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    vol = Column(Float, default=0.0, nullable=False)
    amount = Column(Float, default=0.0, nullable=False)
    source = Column(String(24), default="tushare_stk_mins", nullable=False)
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


class ChanScanRun(AnalyticsBase):
    """Persistent metadata for administrator-triggered Chan scans."""
    __tablename__ = "chan_scan_run"

    id = Column(String(40), primary_key=True)
    status = Column(String(24), nullable=False)
    freq = Column(String(8), nullable=False)
    filters_json = Column(Text, nullable=False)
    candidate_count = Column(Integer, default=0, nullable=False)
    processed_count = Column(Integer, default=0, nullable=False)
    signal_count = Column(Integer, default=0, nullable=False)
    error_count = Column(Integer, default=0, nullable=False)
    started_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime)


class ChanScanSignal(AnalyticsBase):
    """Confirmed native structural Chan signal found by one scan run."""
    __tablename__ = "chan_scan_signal"

    id = Column(String(80), primary_key=True)
    run_id = Column(String(40), nullable=False)
    ts_code = Column(String(16), nullable=False)
    name = Column(String(128), nullable=False)
    signal_type = Column(String(16), nullable=False)
    detail = Column(String(128))
    signal_key = Column(String(256))
    signal_value = Column(String(256))
    bar_time = Column(DateTime)
    confirmed = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)


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


class HKStockBasic(AnalyticsBase):
    """Tushare 港股证券基础信息。"""
    __tablename__ = "hk_stock_basic"

    ts_code = Column(String(16), primary_key=True)
    name = Column(String(128))
    fullname = Column(String(256))
    enname = Column(String(256))
    market = Column(String(64))
    list_status = Column(String(8))
    list_date = Column(Date)
    delist_date = Column(Date)
    trade_unit = Column(Float)
    isin = Column(String(32))
    curr_type = Column(String(16))
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


class HKStockDaily(AnalyticsBase):
    """Tushare 港股未复权日行情；qfq view 根据前收盘跳变自行复权。"""
    __tablename__ = "hk_stock_daily"

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
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


class HKIndexDaily(AnalyticsBase):
    """港股主要指数日行情，主要来自 Tushare index_global。"""
    __tablename__ = "hk_index_daily"

    ts_code = Column(String(16), primary_key=True)
    trade_date = Column(Date, primary_key=True)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    pre_close = Column(Float)
    change = Column(Float)
    pct_chg = Column(Float)
    swing = Column(Float)
    vol = Column(Float)
    source = Column(String(32), default="tushare_index_global", nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


class HKIndexWeightSnapshot(AnalyticsBase):
    """恒生指数公司季度检讨公告中的历史成分权重锚点。"""
    __tablename__ = "hk_index_weight_snapshot"

    index_code = Column(String(16), primary_key=True)
    effective_date = Column(Date, primary_key=True)
    con_code = Column(String(16), primary_key=True)
    con_name = Column(String(256))
    weight = Column(Float)
    free_float_factor = Column(Float)
    reference_date = Column(Date)
    source_url = Column(String(1024))
    source_document = Column(String(256))
    extraction_method = Column(String(32))
    verified = Column(Float, default=0)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, nullable=False)


def ensure_analytics_table_columns():
    """为存量 DuckDB 表补充新增字段（幂等，沿用主库 ensure_table_columns 模式）。"""
    table_columns = {
        "a_stock_market_daily": {
            "volume_ratio": "FLOAT",
            "pe": "FLOAT",
            "pe_ttm": "FLOAT",
            "pb": "FLOAT",
            "dv_ratio": "FLOAT",
            "dv_ttm": "FLOAT",
        },
        "a_stock_income": {
            "revenue": "FLOAT",
            "n_income_attr_p": "FLOAT",
            "operate_profit": "FLOAT",
            "total_profit": "FLOAT",
            "total_cogs": "FLOAT",
            "basic_eps": "FLOAT",
        },
    }
    with analytics_engine.begin() as conn:
        for table_name, columns in table_columns.items():
            existing = {
                row[1]
                for row in conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
            }
            missing = [name for name in columns if name not in existing]
            if not missing:
                continue
            # DuckDB 不允许直接修改仍被视图依赖的表；视图会在本函数之后统一重建。
            if table_name == "a_stock_market_daily":
                conn.execute(text("DROP VIEW IF EXISTS a_stock_market_daily_qfq"))
            for column_name in missing:
                conn.execute(text(
                    f"ALTER TABLE {table_name} ADD COLUMN {column_name} {columns[column_name]}"
                ))


def ensure_analytics_schema():
    AnalyticsBase.metadata.create_all(analytics_engine)
    ensure_analytics_table_columns()
    index_sqls = [
        "CREATE INDEX IF NOT EXISTS idx_a_stock_basic_industry_status ON a_stock_basic(industry, list_status)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_income_symbol_ann ON a_stock_income(ts_code, ann_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_income_symbol_end ON a_stock_income(ts_code, end_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_income_ann ON a_stock_income(ann_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_balancesheet_symbol_ann ON a_stock_balancesheet(ts_code, ann_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_balancesheet_symbol_end ON a_stock_balancesheet(ts_code, end_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_cashflow_symbol_ann ON a_stock_cashflow(ts_code, ann_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_cashflow_symbol_end ON a_stock_cashflow(ts_code, end_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_fina_indicator_symbol_ann ON a_stock_fina_indicator(ts_code, ann_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_fina_indicator_symbol_end ON a_stock_fina_indicator(ts_code, end_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_report_rc_symbol_date ON a_stock_report_rc(ts_code, report_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_report_rc_date_quarter ON a_stock_report_rc(report_date, quarter)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_report_rc_org_date ON a_stock_report_rc(org_name, report_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_name_changes_symbol_dates ON a_stock_name_changes(ts_code, start_date, end_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_ths_member_constituent ON a_stock_ths_member(con_code, ths_code)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_ths_daily_date ON a_stock_ths_daily(trade_date, ths_code)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_market_daily_symbol_date ON a_stock_market_daily(ts_code, trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_minute_symbol_time ON a_stock_minute_bar(ts_code, trade_time)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_minute_time_symbol ON a_stock_minute_bar(trade_time, ts_code)",
        "CREATE INDEX IF NOT EXISTS idx_chan_scan_signal_run ON chan_scan_signal(run_id, signal_type)",
        "CREATE INDEX IF NOT EXISTS idx_chan_scan_signal_symbol_time ON chan_scan_signal(ts_code, bar_time)",
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
        "CREATE INDEX IF NOT EXISTS idx_hk_stock_daily_symbol_date ON hk_stock_daily(ts_code, trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_hk_stock_daily_date_symbol ON hk_stock_daily(trade_date, ts_code)",
        "CREATE INDEX IF NOT EXISTS idx_hk_index_daily_symbol_date ON hk_index_daily(ts_code, trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_hk_index_weight_index_date ON hk_index_weight_snapshot(index_code, effective_date)",
        "CREATE INDEX IF NOT EXISTS idx_hk_index_weight_constituent ON hk_index_weight_snapshot(con_code, effective_date)",
        "CREATE INDEX IF NOT EXISTS idx_us_stock_daily_symbol_date ON us_stock_daily(symbol, trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_us_stock_daily_date_symbol ON us_stock_daily(trade_date, symbol)",
    ]
    view_sqls = [
        """
        CREATE OR REPLACE VIEW a_stock_minute_bar_qfq AS
        WITH anchor_factors AS (
            SELECT ts_code, adj_factor AS anchor_adj_factor
            FROM (
                SELECT ts_code, adj_factor,
                       ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) AS rn
                FROM a_stock_adj_factor
                WHERE adj_factor IS NOT NULL AND adj_factor > 0
            )
            WHERE rn = 1
        )
        SELECT
            m.ts_code,
            m.trade_time,
            CAST(m.open * COALESCE(f.adj_factor / NULLIF(a.anchor_adj_factor, 0), 1.0) AS DOUBLE) AS open,
            CAST(m.high * COALESCE(f.adj_factor / NULLIF(a.anchor_adj_factor, 0), 1.0) AS DOUBLE) AS high,
            CAST(m.low * COALESCE(f.adj_factor / NULLIF(a.anchor_adj_factor, 0), 1.0) AS DOUBLE) AS low,
            CAST(m.close * COALESCE(f.adj_factor / NULLIF(a.anchor_adj_factor, 0), 1.0) AS DOUBLE) AS close,
            m.vol,
            m.amount,
            f.adj_factor,
            a.anchor_adj_factor,
            m.source,
            m.updated_at
        FROM a_stock_minute_bar m
        LEFT JOIN a_stock_adj_factor f
          ON f.ts_code = m.ts_code
         AND f.trade_date = CAST(m.trade_time AS DATE)
        LEFT JOIN anchor_factors a ON a.ts_code = m.ts_code
        """,
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
            m.volume_ratio,
            m.pe,
            m.pe_ttm,
            m.pb,
            m.dv_ratio,
            m.dv_ttm,
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
        """
        CREATE OR REPLACE VIEW hk_stock_daily_qfq AS
        WITH ordered AS (
            SELECT
                d.*,
                LAG(d.close) OVER (
                    PARTITION BY d.ts_code ORDER BY d.trade_date
                ) AS previous_raw_close
            FROM hk_stock_daily d
        ),
        event_factors AS (
            SELECT
                *,
                CASE
                    WHEN previous_raw_close > 0
                     AND pre_close > 0
                     AND ABS(pre_close / previous_raw_close - 1.0) >= 0.005
                    THEN pre_close / previous_raw_close
                    ELSE 1.0
                END AS event_factor
            FROM ordered
        ),
        cumulative AS (
            SELECT
                *,
                COALESCE(
                    EXP(SUM(LN(event_factor)) OVER (
                        PARTITION BY ts_code
                        ORDER BY trade_date
                        ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING
                    )),
                    1.0
                ) AS cumulative_factor
            FROM event_factors
        )
        SELECT
            c.ts_code,
            c.ts_code AS symbol,
            c.trade_date,
            CAST(c.open * c.cumulative_factor AS DOUBLE) AS open,
            CAST(c.high * c.cumulative_factor AS DOUBLE) AS high,
            CAST(c.low * c.cumulative_factor AS DOUBLE) AS low,
            CAST(c.close * c.cumulative_factor AS DOUBLE) AS close,
            CAST(c.pre_close * c.cumulative_factor AS DOUBLE) AS pre_close,
            c.change,
            c.pct_chg,
            c.vol,
            c.vol AS volume,
            c.amount,
            c.amount AS turnover,
            c.event_factor,
            c.cumulative_factor,
            1.0 AS anchor_factor,
            'derived_qfq' AS adjust_type,
            c.created_at,
            c.updated_at
        FROM cumulative c
        WHERE c.close IS NOT NULL
          AND c.cumulative_factor > 0
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

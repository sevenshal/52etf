from sqlalchemy import create_engine, Column, String, Float, Boolean, DateTime, Date, Integer, ForeignKey, Table, PrimaryKeyConstraint, UniqueConstraint, JSON, Text, text, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, scoped_session, relationship
from contextlib import contextmanager
import os
from datetime import datetime

# 创建基础目录
DB_PATH = os.getenv("QUANT_SQLITE_PATH") or "/var/lib/quant_robot/evc_stocks.db"
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# 创建基础引擎和Base类
engine = create_engine(
    f'sqlite:///{DB_PATH}',
    connect_args={"timeout": 30},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()

Base = declarative_base()

# 创建EVC会话
SessionLocal = sessionmaker(bind=engine)
Session = scoped_session(SessionLocal)


class WebAccount(Base):
    """可登录 Web 系统的账户。"""
    __tablename__ = "web_accounts"

    account_id = Column(String(128), primary_key=True)
    note = Column(String(500), nullable=False, default="")
    enabled = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class WebAccountDailyUsage(Base):
    """Web 账户按自然日聚合的 API 请求使用量。"""
    __tablename__ = "web_account_daily_usage"

    account_id = Column(String(128), primary_key=True)
    usage_date = Column(Date, primary_key=True)
    request_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

# 股票-标签关联表
stock_tags = Table(
    'stock_tags',
    Base.metadata,
    Column('stock_symbol', String, ForeignKey('stock_evc.symbol')),
    Column('tag_id', String, ForeignKey('stock_tag.id')),
    Column('date', Date),
    PrimaryKeyConstraint('stock_symbol', 'tag_id', 'date')
)

class StockTag(Base):
    __tablename__ = 'stock_tag'

    id = Column(String, primary_key=True)
    created_at = Column(DateTime)
    name = Column(String)
    built_in = Column(Boolean)
    official_only = Column(Boolean)
    includes_option_put_call = Column(Boolean)
    option_put_call_fetch_tag_ordinal = Column(Integer)
    sort_group = Column(Integer)
    updated_at = Column(DateTime)

class StockEVC(Base):
    __tablename__ = 'stock_evc'

    symbol = Column(String, primary_key=True)
    date = Column(Date, primary_key=True)
    company = Column(String)
    last_price = Column(Float)
    last_change = Column(Float)
    last_change_percent = Column(Float)
    fair_value_lo = Column(Float)
    fair_value_hi = Column(Float)
    fair_value_date = Column(Date)
    forward_next_fy_lo = Column(Float)
    forward_next_fy_hi = Column(Float)
    forward_next_fy_max_value_lo = Column(Float)
    forward_next_fy_max_value_hi = Column(Float)
    beta = Column(Float)
    pe_ratio = Column(Float)
    forward_pe_ratio = Column(Float)
    is_under = Column(Boolean)
    is_over = Column(Boolean)
    updated_at = Column(DateTime)
    
    # 添加与标签的关系
    tags = relationship('StockTag', secondary=stock_tags, backref='stocks')

class _StaticInfoColumnsMixin:
    __abstract__ = True

    name_cn = Column(String)
    name_en = Column(String)
    name_hk = Column(String)
    exchange = Column(String)
    currency = Column(String)
    lot_size = Column(Integer)
    total_shares = Column(Integer)
    circulating_shares = Column(Integer)
    hk_shares = Column(Integer)
    eps = Column(Float)
    eps_ttm = Column(Float)
    bps = Column(Float)
    dividend_yield = Column(Float)
    stock_derivatives = Column(JSON)
    board = Column(String)
    raw_data = Column(JSON)

class StockStaticInfoSnapshot(_StaticInfoColumnsMixin, Base):
    __tablename__ = 'stock_static_info_snapshot'

    symbol = Column(String, primary_key=True)
    date = Column(Date, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

class StockStaticInfoHistory(_StaticInfoColumnsMixin, Base):
    __tablename__ = 'stock_static_info_history'

    symbol = Column(String, primary_key=True)
    date = Column(Date, primary_key=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

class ETFAnalysis(Base):
    __tablename__ = 'etf_analysis'

    symbol = Column(String, primary_key=True)  # ETF代码
    date = Column(Date, primary_key=True)      # 分析日期
    name = Column(String)                      # ETF名称
    update_date = Column(String)               # 持仓更新日期
    total_shares = Column(Float)               # ETF总股数
    total_market_value = Column(Float)         # 总市值
    current_price = Column(Float)              # 当前价格
    market_price = Column(Float)
    total_weight = Column(Float)               # 总权重
    fair_value_lo = Column(Float)              # 当前估值下限
    fair_value_hi = Column(Float)              # 当前估值上限
    forward_next_fy_lo = Column(Float)         # 下一财年估值下限
    forward_next_fy_hi = Column(Float)         # 下一财年估值上限
    forward_stocks_value_lo = Column(Float)    # 有估值股票的当前估值下限
    forward_stocks_value_hi = Column(Float)    # 有估值股票的当前估值上限
    forward_stocks_fy_lo = Column(Float)       # 有估值股票的下一财年估值下限
    forward_stocks_fy_hi = Column(Float)       # 有估值股票的下一财年估值上限
    forward_stocks_weight = Column(Float)      # 有估值股票的权重
    min_fair_value_date = Column(Date)          # 有估值股票的最小估值日期
    max_fair_value_date = Column(Date)          # 有估值股票的最大估值日期
    leveraged_symbol = Column(String)          # 三倍做多ETF代码
    leveraged_price = Column(Float)            # 三倍做多ETF价格
    leveraged_szdt_score = Column(Float)       # 三倍做多ETF情绪指数
    leveraged_szdt_update_time = Column(String) # 三倍做多ETF情绪指数更新时间
    components = Column(JSON)                  # 持仓数据（JSON格式）
    eps = Column(Float)
    eps_v2 = Column(Float)
    eps_ttm = Column(Float)
    eps_forward = Column(Float)
    created_at = Column(DateTime)              # 创建时间
    updated_at = Column(DateTime)              # 更新时间

class TradingLog(Base):
    __tablename__ = "trading_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(String, index=True)
    timestamp = Column(DateTime, index=True)
    level = Column(String)
    message = Column(String)

class ETFHolding(Base):
    """ETF持仓记录"""
    __tablename__ = 'etf_holdings'
    
    etf_symbol = Column(String, primary_key=True)
    symbol = Column(String, primary_key=True)
    date = Column(Date, primary_key=True)
    name = Column(String)
    asset_class = Column(String)
    shares = Column(Integer)
    weight = Column(Float)

class USStockIndustrySnapshot(Base):
    """美股行业分类快照。"""
    __tablename__ = "us_stock_industry_snapshot"

    symbol = Column(String, primary_key=True)
    date = Column(Date, primary_key=True)
    provider = Column(String(32), primary_key=True, default="fmp")
    name = Column(String)
    exchange = Column(String)
    sector = Column(String(128), index=True)
    industry_group = Column(String(128), index=True)
    industry = Column(String(128), index=True)
    sub_industry = Column(String(128), index=True)
    sic_code = Column(String(32), index=True)
    sic_description = Column(String(255))
    market_cap = Column(Float)
    raw_data = Column(JSON)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class AStockInnovation100Level(Base):
    """A股创新100指数每日点位。"""
    __tablename__ = "a_stock_innovation100_levels"

    index_code = Column(String(32), primary_key=True)
    date = Column(Date, primary_key=True)
    level = Column(Float, nullable=False)
    daily_return_pct = Column(Float)
    drawdown_pct = Column(Float)
    constituent_count = Column(Integer)
    total_circ_mv = Column(Float)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

class AStockInnovation100Rebalance(Base):
    """A股创新100重构/再平衡记录。"""
    __tablename__ = "a_stock_innovation100_rebalances"

    id = Column(Integer, primary_key=True, autoincrement=True)
    index_code = Column(String(32), index=True, nullable=False)
    rebalance_date = Column(Date, index=True, nullable=False)
    effective_date = Column(Date, index=True)
    rebalance_type = Column(String(32), index=True, nullable=False)
    constituent_count = Column(Integer)
    turnover_pct = Column(Float)
    total_circ_mv = Column(Float)
    additions = Column(JSON)
    removals = Column(JSON)
    rule_snapshot = Column(JSON)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

class AStockInnovation100Constituent(Base):
    """A股创新100每次重构/再平衡后的成分股权重。"""
    __tablename__ = "a_stock_innovation100_constituents"

    index_code = Column(String(32), primary_key=True)
    rebalance_id = Column(Integer, ForeignKey("a_stock_innovation100_rebalances.id"), primary_key=True)
    ts_code = Column(String(16), primary_key=True)
    rebalance_date = Column(Date, index=True, nullable=False)
    effective_date = Column(Date, index=True)
    name = Column(String(64))
    industry = Column(String(64), index=True)
    rank = Column(Integer)
    raw_weight_pct = Column(Float)
    weight_pct = Column(Float)
    total_mv = Column(Float)
    circ_mv = Column(Float)
    avg_amount_60d = Column(Float)
    action = Column(String(16), index=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

class InvalidSymbolCache(Base):
    """持久化缓存外部行情源返回的无效标的。"""
    __tablename__ = 'invalid_symbol_cache'

    source = Column(String(32), primary_key=True)
    symbol = Column(String(32), primary_key=True)
    reason = Column(String(255))
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

class FetchLog(Base):
    __tablename__ = 'fetch_logs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, nullable=False)
    total_tags_fetched = Column(Integer, nullable=False)
    total_stocks_fetched = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

class ETFFearGreedCloneHistory(Base):
    """ETF/指数恐贪复刻值历史记录"""
    __tablename__ = 'etf_fear_greed_clone_history'

    symbol = Column(String(32), primary_key=True)
    date = Column(Date, primary_key=True)
    score = Column(Float, nullable=False)
    rating = Column(String(32))
    method = Column(String(128))
    history_days = Column(Integer)
    score_window = Column(Integer)
    min_periods = Column(Integer)
    use_historical_holdings = Column(Boolean)

    etf_open = Column(Float)
    etf_high = Column(Float)
    etf_low = Column(Float)
    etf_close = Column(Float)
    etf_volume = Column(Float)
    etf_turnover = Column(Float)

    holdings_as_of = Column(Date)
    holdings_count = Column(Integer)
    holdings_weight_used = Column(Float)

    market_momentum_score = Column(Float)
    market_momentum_raw = Column(Float)
    stock_price_strength_score = Column(Float)
    stock_price_strength_raw = Column(Float)
    stock_price_breadth_score = Column(Float)
    stock_price_breadth_raw = Column(Float)
    put_call_options_score = Column(Float)
    put_call_options_raw = Column(Float)
    market_volatility_score = Column(Float)
    market_volatility_raw = Column(Float)
    safe_haven_demand_score = Column(Float)
    safe_haven_demand_raw = Column(Float)
    junk_bond_demand_score = Column(Float)
    junk_bond_demand_raw = Column(Float)

    components = Column(JSON)
    warnings = Column(JSON)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

class ETFFearGreedCloneHolding(Base):
    """ETF恐贪复刻指数使用的每日持仓快照"""
    __tablename__ = 'etf_fear_greed_clone_holdings'

    symbol = Column(String(32), primary_key=True)
    date = Column(Date, primary_key=True)
    holding_symbol = Column(String(32), primary_key=True)
    holdings_as_of = Column(Date)
    name = Column(String)
    asset_class = Column(String)
    shares = Column(Integer)
    market_value = Column(Float)
    weight = Column(Float)
    price = Column(Float)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

class AStockIndexValuationSnapshot(Base):
    """A股指数按成分股权重聚合后的每日估值快照。"""
    __tablename__ = "a_stock_index_valuation_snapshots"

    symbol = Column(String(32), primary_key=True)
    date = Column(Date, primary_key=True)
    payload = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

class AStockFearGreedIntraday(Base):
    """A股盘中贪恐快照（12:00 等盘中时点），独立于日频最终历史库 etf_fear_greed_clone_history。"""
    __tablename__ = "a_stock_fear_greed_intraday"

    symbol = Column(String(32), primary_key=True)
    snapshot_time = Column(DateTime, primary_key=True)  # 盘中快照时点（含时分秒）
    trade_date = Column(Date, nullable=False, index=True)
    score = Column(Float, nullable=False)
    rating = Column(String(32))
    method = Column(String(128))
    history_days = Column(Integer)
    score_window = Column(Integer)
    min_periods = Column(Integer)
    component_count = Column(Integer)
    components_used = Column(JSON)
    index_level = Column(Float)  # 盘中指数点位（rt_idx_k 现价，或代理ETF映射值）
    etf_price = Column(JSON)  # 盘中指数 OHLC/量额快照
    quote_source = Column(String(32))  # rt_idx_k / proxy_etf
    quote_time = Column(DateTime)  # 实时行情时间戳
    market_open = Column(Boolean)
    components = Column(JSON)
    warnings = Column(JSON)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

class ETFPutCallRatio(Base):
    """Barchart ETF Put/Call Ratio 历史数据"""
    __tablename__ = 'etf_put_call_ratios'

    symbol = Column(String(32), primary_key=True)
    date = Column(Date, primary_key=True)
    put_volume = Column(Integer)
    call_volume = Column(Integer)
    total_volume = Column(Integer)
    put_call_volume_ratio = Column(Float)
    put_open_interest = Column(Integer)
    call_open_interest = Column(Integer)
    total_open_interest = Column(Integer)
    put_call_open_interest_ratio = Column(Float)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

class ETFOptionExpiration(Base):
    """Barchart ETF 当前期权到期日未平仓快照"""
    __tablename__ = 'etf_option_expirations'

    symbol = Column(String(32), primary_key=True)
    snapshot_date = Column(Date, primary_key=True)
    expiration_date = Column(Date, primary_key=True)
    expiration_type = Column(String(32))
    days_to_expiration = Column(Integer)
    put_volume = Column(Integer)
    call_volume = Column(Integer)
    total_volume = Column(Integer)
    put_call_volume_ratio = Column(Float)
    put_open_interest = Column(Integer)
    call_open_interest = Column(Integer)
    total_open_interest = Column(Integer)
    put_call_open_interest_ratio = Column(Float)
    average_volatility = Column(Float)
    symbol_type = Column(String(32))
    last_price = Column(Float)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

class StockFavorite(Base):
    """用户股票收藏表"""
    __tablename__ = 'stock_favorites'

    account_id = Column(String, primary_key=True) # Added for migration
    symbol = Column(String(32), primary_key=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

class DbSqlFavorite(Base):
    """DB管理工具SQL收藏。"""
    __tablename__ = "db_sql_favorites"
    __table_args__ = (
        UniqueConstraint("account_id", "name", name="uq_db_sql_favorites_account_name"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True, nullable=False)
    name = Column(String(120), nullable=False)
    sql = Column(Text, nullable=False)
    engine = Column(String(16), default="duckdb", nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

class CNNFearGreedIndex(Base):
    __tablename__ = 'cnn_fear_greed_index'

    date = Column(Date, primary_key=True)
    index_value = Column(Float, nullable=False)  # 当前恐贪指数
    index_timestamp = Column(DateTime)
    previous_close = Column(Float)  # 上一个收盘恐贪指数
    previous_1_week = Column(Float)  # 一周前恐贪指数
    previous_1_month = Column(Float)  # 一个月前恐贪指数
    previous_1_year = Column(Float)  # 一年前恐贪指数
    market_momentum = Column(Float)  # 市场动量指数
    market_momentum_125 = Column(Float)  # 市场动量指数
    stock_price_strength = Column(Float)  # 股价强度指数
    stock_price_breadth = Column(Float)  # 股价广度指数
    put_call_options = Column(Float)  # 看跌看涨期权比率指数
    market_volatility_vix = Column(Float)  # 市场波动率指数
    market_volatility_vix_50 = Column(Float)  # 市场波动率指数
    junk_bond_demand = Column(Float)  # 垃圾债券需求指数
    safe_haven_demand = Column(Float)  # 避险需求指数
    created_at = Column(DateTime, default=datetime.now, nullable=False)

# 股票表
class SzdtTradeStock(Base):
    __tablename__ = 'szdt_trade_stocks'

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True) # Added for migration
    code = Column(String, nullable=False)
    name = Column(String, nullable=False)
    type = Column(Integer, nullable=False, default=3)
    when_buy = Column(Integer, nullable=False)
    when_sell = Column(Integer, nullable=False)
    max_position = Column(Integer, nullable=False)
    buy_amount = Column(Float, nullable=False)
    sell_amount = Column(Float, nullable=False)
    buy_factor = Column(Float, nullable=False, default=1)
    sell_factor = Column(Float, nullable=False, default=1)
    lever = Column(Integer, nullable=False)
    emo_area = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

class TradingState(Base):
    """交易状态表"""
    __tablename__ = "trading_states"
    
    account_id = Column(String, primary_key=True) # Added for migration
    cli_id = Column(String, primary_key=True)
    current_index = Column(Integer, default=0)  # 当前处理的股票索引
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class StockCooldown(Base):
    """股票冷却表"""
    __tablename__ = "stock_cooldowns"
    
    account_id = Column(String, primary_key=True) # Added for migration
    cli_id = Column(String, primary_key=True)
    stock_code = Column(String, primary_key=True)
    until = Column(DateTime)  # 冷却结束时间
    reason = Column(String)   # 冷却原因
    created_at = Column(DateTime, default=datetime.now)


class AIStockRecommendationRun(Base):
    """A-share AI recommendation batch and its immutable input/output snapshots."""
    __tablename__ = "ai_stock_recommendation_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_at = Column(DateTime, nullable=False, default=datetime.now, index=True)
    trade_date = Column(Date, nullable=False, index=True)
    run_type = Column(String(16), nullable=False, index=True)  # PREOPEN / OPENING / INTRADAY
    status = Column(String(16), nullable=False, default="RUNNING", index=True)
    model_name = Column(String(100))
    prompt_version = Column(String(32), nullable=False, default="ai-stock-v1")
    market_snapshot = Column(JSON)
    news_snapshot = Column(JSON)
    candidate_snapshot = Column(JSON)
    ai_raw_response = Column(JSON)
    error_message = Column(String(2000))
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    completed_at = Column(DateTime)


class AIStockRecommendation(Base):
    """One validated DeepSeek-selected stock inside a recommendation batch."""
    __tablename__ = "ai_stock_recommendations"
    __table_args__ = (
        UniqueConstraint("run_id", "ts_code", name="uniq_ai_stock_recommendation_run_symbol"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("ai_stock_recommendation_runs.id"), nullable=False, index=True)
    ts_code = Column(String(16), nullable=False, index=True)
    name = Column(String(64), nullable=False)
    industry = Column(String(128))
    themes = Column(JSON)
    recommendation_price = Column(Float, nullable=False)
    target_return_pct = Column(Float, nullable=False)
    target_price = Column(Float, nullable=False)
    ai_confidence = Column(Float, nullable=False)
    execution_score = Column(Float, nullable=False, default=0.0)
    news_signal = Column(Float, nullable=False, default=50.0)
    rank = Column(Integer, nullable=False)
    reason = Column(Text, nullable=False)
    risks = Column(Text)
    evidence = Column(JSON)
    candidate_snapshot = Column(JSON)
    created_at = Column(DateTime, nullable=False, default=datetime.now)


class AIStockPaperPortfolio(Base):
    """The single, system-owned A-share paper portfolio."""
    __tablename__ = "ai_stock_paper_portfolios"

    id = Column(Integer, primary_key=True, default=1)
    enabled = Column(Boolean, nullable=False, default=True)
    initial_cash = Column(Float, nullable=False, default=1_000_000.0)
    cash = Column(Float, nullable=False, default=1_000_000.0)
    last_processed_minute = Column(DateTime)
    last_execution_target = Column(Float)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class AIStockStrategyConfig(Base):
    """Versioned, global defaults for the administrator-owned paper strategy."""
    __tablename__ = "ai_stock_strategy_configs"

    id = Column(Integer, primary_key=True, default=1)
    enabled = Column(Boolean, nullable=False, default=True)
    config_version = Column(String(32), nullable=False, default="ai-stock-v1")
    parameters = Column(JSON)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class AIStockServiceConfig(Base):
    """Administrator-managed integration credentials for the AI stock service.

    Secret columns are write-only at the API layer and are intentionally never
    included in response payloads or application logs.
    """
    __tablename__ = "ai_stock_service_configs"

    id = Column(Integer, primary_key=True, default=1)
    deepseek_api_key = Column(String(512))
    deepseek_model = Column(String(100), nullable=False, default="deepseek-chat")
    deepseek_base_url = Column(String(500), nullable=False, default="https://api.deepseek.com")
    max_candidates = Column(Integer)
    max_events = Column(Integer)
    max_boards = Column(Integer)
    max_candidates_per_board = Column(Integer)
    min_market_cap = Column(Integer)
    min_avg_turnover = Column(Integer)
    max_recommendations = Column(Integer)
    min_listing_days = Column(Integer)
    target_return_pct_min = Column(Integer)
    target_return_pct_max = Column(Integer)
    news_signal_weight = Column(Integer)
    updated_by = Column(String(128))
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class TushareAccountConfig(Base):
    """Global Tushare credential, managed by administrators as write-only data."""
    __tablename__ = "tushare_account_configs"

    id = Column(Integer, primary_key=True, default=1)
    api_token = Column(String(512))
    updated_by = Column(String(128))
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class AIStockTHSIndexCache(Base):
    """Daily cache of the immutable THS board catalogue used by AI-stock V3."""
    __tablename__ = "ai_stock_ths_index_cache"
    __table_args__ = (
        UniqueConstraint("index_type", "ts_code", name="uniq_ai_stock_ths_index_type_code"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    index_type = Column(String(8), nullable=False, index=True)
    ts_code = Column(String(24), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    constituent_count = Column(Integer)
    exchange = Column(String(16))
    list_date = Column(Date)
    fetched_at = Column(DateTime, nullable=False, default=datetime.now, index=True)


class AIStockPaperLot(Base):
    """Lot-level accounting is required for the A-share T+1 rule."""
    __tablename__ = "ai_stock_paper_lots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    portfolio_id = Column(Integer, ForeignKey("ai_stock_paper_portfolios.id"), nullable=False, index=True)
    recommendation_id = Column(Integer, ForeignKey("ai_stock_recommendations.id"), nullable=True, index=True)
    ts_code = Column(String(16), nullable=False, index=True)
    name = Column(String(64), nullable=False)
    bought_at = Column(DateTime, nullable=False, index=True)
    buy_price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=False)
    remaining_quantity = Column(Integer, nullable=False)
    target_price = Column(Float, nullable=False)
    # 买入后已见到的最高价（移动止盈基准），创建时等于 buy_price，逐分钟抬升
    peak_price = Column(Float)
    # A -8% reduction is permitted once per lot and is re-enabled only after
    # recovery to -5%, which avoids repeated churn in a falling market.
    stop_half_triggered = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class AIStockPaperTrade(Base):
    """Auditable paper orders with the exact rule and state snapshot that triggered them."""
    __tablename__ = "ai_stock_paper_trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    portfolio_id = Column(Integer, ForeignKey("ai_stock_paper_portfolios.id"), nullable=False, index=True)
    lot_id = Column(Integer, ForeignKey("ai_stock_paper_lots.id"), nullable=True, index=True)
    recommendation_id = Column(Integer, ForeignKey("ai_stock_recommendations.id"), nullable=True, index=True)
    executed_at = Column(DateTime, nullable=False, default=datetime.now, index=True)
    trade_date = Column(Date, nullable=False, index=True)
    ts_code = Column(String(16), nullable=False, index=True)
    name = Column(String(64), nullable=False)
    side = Column(String(8), nullable=False)  # BUY / SELL
    price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=False)
    amount = Column(Float, nullable=False)
    fee = Column(Float, nullable=False, default=0.0)
    realized_pnl = Column(Float)
    reason_code = Column(String(64), nullable=False)
    reason = Column(Text, nullable=False)
    state_snapshot = Column(JSON)
    created_at = Column(DateTime, nullable=False, default=datetime.now)


class AIStockPaperEquity(Base):
    __tablename__ = "ai_stock_paper_equity"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "recorded_at", name="uniq_ai_stock_paper_equity_time"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    portfolio_id = Column(Integer, ForeignKey("ai_stock_paper_portfolios.id"), nullable=False, index=True)
    recorded_at = Column(DateTime, nullable=False, index=True)
    cash = Column(Float, nullable=False)
    market_value = Column(Float, nullable=False)
    total_equity = Column(Float, nullable=False)
    benchmark_close = Column(Float)
    created_at = Column(DateTime, nullable=False, default=datetime.now)


class AIStockHoldEvaluation(Base):
    """AI hold_score for a current paper position (low score = sell bias).

    Written once per recommendation batch when the paper portfolio has open
    lots; process_minute reads the latest evaluation and may use it as one
    sell trigger (AI_SIGNAL) alongside the hard price rules.
    """
    __tablename__ = "ai_stock_hold_evaluations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    portfolio_id = Column(Integer, ForeignKey("ai_stock_paper_portfolios.id"), nullable=False, index=True)
    run_id = Column(Integer, ForeignKey("ai_stock_recommendation_runs.id"), nullable=True, index=True)
    ts_code = Column(String(16), nullable=False, index=True)
    name = Column(String(64), nullable=False)
    hold_score = Column(Float, nullable=False)
    reason = Column(Text)
    evaluated_at = Column(DateTime, nullable=False, default=datetime.now, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)


class AIStockBenchmarkSnapshot(Base):
    """Read-only snapshots collected from the reference site for later comparison."""
    __tablename__ = "ai_stock_benchmark_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    captured_at = Column(DateTime, nullable=False, default=datetime.now, index=True)
    trade_date = Column(Date, index=True)
    snapshot_type = Column(String(32), nullable=False, index=True)
    payload = Column(JSON, nullable=False)
    status = Column(String(16), nullable=False, default="SUCCESS")
    message = Column(String(1000))


class AIStockEvaluation(Base):
    __tablename__ = "ai_stock_evaluations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    evaluated_at = Column(DateTime, nullable=False, default=datetime.now, index=True)
    window_start = Column(Date, nullable=False)
    window_end = Column(Date, nullable=False)
    theme_overlap_pct = Column(Float)
    stock_overlap_pct = Column(Float)
    system_return_pct = Column(Float)
    benchmark_return_pct = Column(Float)
    system_max_drawdown_pct = Column(Float)
    benchmark_max_drawdown_pct = Column(Float)
    passed = Column(Boolean, nullable=False, default=False)
    details = Column(JSON)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class AIStockRecommendationHitRate(Base):
    """推荐命中率快照（每交易日 16:20 自动评估更新）。"""
    __tablename__ = "ai_stock_recommendation_hit_rates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    evaluated_at = Column(DateTime, nullable=False, default=datetime.now, index=True)
    window_start = Column(Date, nullable=False)
    window_end = Column(Date, nullable=False)
    total_count = Column(Integer, nullable=False, default=0)
    payload = Column(JSON, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.now)


class AutomatedTradingConfig(Base):
    """自动化交易配置"""
    __tablename__ = "automated_trading_configs"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True, unique=True) # 每个账户暂定一套配置
    enabled = Column(Boolean, default=False)
    etf_code = Column(String, nullable=False)
    short_window = Column(Integer, nullable=False)
    long_window = Column(Integer, nullable=False)
    ib_port = Column(Integer, nullable=False)
    target_ratio = Column(Float, default=10.0) # 目标仓位比例 (%)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class SZDTTradingConfig(Base):
    """SZDT贪恐策略交易配置"""
    __tablename__ = "szdt_trading_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True, unique=True)
    enabled = Column(Boolean, default=False) # 美股开关
    enabled_a = Column(Boolean, default=False) # A股开关
    ib_account_id = Column(Integer, nullable=True) # 关联的 IB 账户 ID
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class AutomatedTradeLog(Base):
    """自动化交易日志"""
    __tablename__ = "automated_trade_logs"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True)
    timestamp = Column(DateTime, default=datetime.now, index=True)
    symbol = Column(String, nullable=False)
    action = Column(String, nullable=False)  # 'BUY' or 'SELL'
    price = Column(Float)
    quantity = Column(Float)
    status = Column(String)  # 'SUCCESS' or 'FAILED'
    message = Column(String)


class SoxlFearStrategyConfig(Base):
    """SOXL 情绪量能自动交易配置"""
    __tablename__ = "soxl_fear_strategy_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True)
    enabled = Column(Boolean, default=False)
    symbol = Column(String, nullable=False, default="SOXL.US")
    account_type = Column(String, default="ib")
    ib_account_id = Column(Integer, nullable=True)
    longport_account_id = Column(String, nullable=True)
    external_trading_account_id = Column(Integer, nullable=True)
    live_sub_account_id = Column(Integer, nullable=True)
    trading_account_id = Column(String, nullable=True)
    buy_threshold = Column(Float, nullable=False, default=60.0)
    greed_threshold = Column(Float, nullable=False, default=60.0)
    volume_ratio_threshold = Column(Float, nullable=False, default=1.4)
    buy_position_pct = Column(Float, nullable=False, default=50.0)
    cooldown_days = Column(Integer, nullable=False, default=10)
    trailing_stop_pct = Column(Float, nullable=False, default=5.0)
    sell_position_pct = Column(Float, nullable=False, default=50.0)
    sell_reduction_basis = Column(String, nullable=False, default="portfolio")
    max_take_profit_sells_per_cycle = Column(Integer, nullable=False, default=2)
    min_position_pct_after_take_profit = Column(Float, nullable=False, default=10.0)
    rebalance_threshold_pct = Column(Float, nullable=False, default=5.0)
    last_run_at = Column(DateTime)
    last_run_status = Column(String(16))
    last_run_message = Column(String(500))
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    __table_args__ = (
        UniqueConstraint('symbol', 'account_type', 'trading_account_id', name='uniq_soxl_fear_strategy_target_account'),
    )


class SoxlFearStrategyState(Base):
    """SOXL 情绪量能自动交易运行状态"""
    __tablename__ = "soxl_fear_strategy_states"

    config_id = Column(Integer, ForeignKey("soxl_fear_strategy_configs.id"), primary_key=True)
    account_id = Column(String, index=True)
    symbol = Column(String, nullable=False, default="SOXL.US")
    last_processed_date = Column(Date)
    cooldown_remaining_days = Column(Integer, nullable=False, default=0)
    greed_peak_price = Column(Float)
    take_profit_cycle_sell_count = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class SoxlFearStrategyLog(Base):
    """SOXL 情绪量能自动交易日志"""
    __tablename__ = "soxl_fear_strategy_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_id = Column(Integer, ForeignKey("soxl_fear_strategy_configs.id"), index=True, nullable=True)
    account_id = Column(String, index=True)
    timestamp = Column(DateTime, default=datetime.now, index=True)
    symbol = Column(String, nullable=False, default="SOXL.US")
    trigger_source = Column(String(16), nullable=False, default="auto")
    action = Column(String(16), nullable=False)
    status = Column(String(16), nullable=False)
    price = Column(Float)
    quantity = Column(Integer)
    cnn_index_value = Column(Float)
    fear_score = Column(Float)
    volume_ratio = Column(Float)
    position_ratio_before = Column(Float)
    position_ratio_after = Column(Float)
    message = Column(String(1000))


class AStockFearStrategyConfig(Base):
    """A股情绪量能自动交易配置（隔天信号：用前一交易日恐贪+量比，在 run_time 开盘成交）"""
    __tablename__ = "a_stock_fear_strategy_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True)
    enabled = Column(Boolean, default=False)
    symbol = Column(String, nullable=False, default="510880.SH")
    # 恐贪来源：a_stock_000015_sh 等（对齐回测 FEAR_SOURCE_OPTIONS 的 key）
    fear_source = Column(String, nullable=False, default="a_stock_000015_sh")
    # 量比来源标的（可选，默认=交易标的自身）
    volume_signal_symbol = Column(String, nullable=True)
    account_type = Column(String, default="external")
    external_trading_account_id = Column(Integer, nullable=True)
    live_sub_account_id = Column(Integer, nullable=True)
    trading_account_id = Column(String, nullable=True)
    # 每日触发时间（Asia/Shanghai，HH:MM），默认 09:30 开盘
    run_time = Column(String(5), nullable=False, default="09:30")
    # 跷跷板候补（可选）：主标的空仓时，候补极恐放量则买入候补；主标的出信号换回
    sub_symbol = Column(String, nullable=True)
    sub_fear_source = Column(String, nullable=True)
    sub_volume_signal_symbol = Column(String, nullable=True)
    sub_buy_threshold = Column(Float, nullable=False, default=25.0)
    sub_volume_ratio_threshold = Column(Float, nullable=False, default=1.6)
    # 第二候补（三标的轮动，可选）
    sub2_symbol = Column(String, nullable=True)
    sub2_fear_source = Column(String, nullable=True)
    sub2_volume_signal_symbol = Column(String, nullable=True)
    sub2_buy_threshold = Column(Float, nullable=False, default=20.0)
    sub2_volume_ratio_threshold = Column(Float, nullable=False, default=1.3)
    # 换仓阈值：NULL=主辅跷跷板；有值=对称双轮动（恐贪超过阈值且另一标的有信号则换仓）
    swap_threshold = Column(Float, nullable=True)
    buy_threshold = Column(Float, nullable=False, default=30.0)
    greed_threshold = Column(Float, nullable=False, default=70.0)
    volume_ratio_threshold = Column(Float, nullable=False, default=1.3)
    # 统一对数放量阈值（log-z）：NULL=旧量比逻辑；有值=log(vol) 相对前20日均值放大该标准差视为放量（默认 1.25）
    volume_z_threshold = Column(Float, nullable=True)
    # 卖出缩量阈值：<=0 关闭（贪恐即卖）；>0 时贪恐>=greed 且当日缩量达该标准差才卖
    sell_shrink_z = Column(Float, nullable=False, default=-1.0)
    buy_position_pct = Column(Float, nullable=False, default=100.0)
    cooldown_days = Column(Integer, nullable=False, default=0)
    # 0 = 到达贪恐阈值（>= greed_threshold）即卖
    trailing_stop_pct = Column(Float, nullable=False, default=0.0)
    sell_position_pct = Column(Float, nullable=False, default=100.0)
    sell_reduction_basis = Column(String, nullable=False, default="holdings")
    sell_price_above_avg_cost = Column(Boolean, nullable=False, default=False)
    max_take_profit_sells_per_cycle = Column(Integer, nullable=False, default=2)
    min_position_pct_after_take_profit = Column(Float, nullable=False, default=0.0)
    rebalance_threshold_pct = Column(Float, nullable=False, default=0.0)
    last_run_at = Column(DateTime)
    last_run_status = Column(String(16))
    last_run_message = Column(String(500))
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    __table_args__ = (
        UniqueConstraint('symbol', 'trading_account_id', name='uniq_a_stock_fear_strategy_target_account'),
    )


class AStockFearStrategyState(Base):
    """A股情绪量能自动交易运行状态"""
    __tablename__ = "a_stock_fear_strategy_states"

    config_id = Column(Integer, ForeignKey("a_stock_fear_strategy_configs.id"), primary_key=True)
    account_id = Column(String, index=True)
    symbol = Column(String, nullable=False, default="510880.SH")
    last_processed_date = Column(Date)
    cooldown_remaining_days = Column(Integer, nullable=False, default=0)
    greed_peak_price = Column(Float)
    take_profit_cycle_sell_count = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class AStockFearStrategyLog(Base):
    """A股情绪量能自动交易日志"""
    __tablename__ = "a_stock_fear_strategy_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_id = Column(Integer, ForeignKey("a_stock_fear_strategy_configs.id"), index=True, nullable=True)
    account_id = Column(String, index=True)
    timestamp = Column(DateTime, default=datetime.now, index=True)
    symbol = Column(String, nullable=False, default="510880.SH")
    trigger_source = Column(String(16), nullable=False, default="auto")
    action = Column(String(16), nullable=False)
    status = Column(String(16), nullable=False)
    price = Column(Float)
    quantity = Column(Integer)
    fear_score = Column(Float)
    volume_ratio = Column(Float)
    position_ratio_before = Column(Float)
    position_ratio_after = Column(Float)
    message = Column(String(1000))


class ValuationSimConfig(Base):
    """EVC 估值成长策略模拟盘配置。"""
    __tablename__ = "valuation_sim_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True)
    name = Column(String(120), nullable=False, default="纳指100估值成长模拟盘")
    enabled = Column(Boolean, default=False)
    universe_tag_ids = Column(JSON)
    min_market_cap_100m = Column(Float, nullable=True, default=100.0)
    max_market_cap_100m = Column(Float, nullable=True)
    max_positions = Column(Integer, nullable=False, default=5)
    external_trading_account_id = Column(Integer, nullable=True)
    live_sub_account_id = Column(Integer, nullable=True)
    trigger_time = Column(String(8), nullable=False, default="18:00")
    trigger_timezone = Column(String(64), nullable=False, default="America/New_York")
    undervalue_threshold = Column(Float, nullable=False, default=0.9)
    next_fy_growth_threshold = Column(Float, nullable=False, default=1.1)
    ema_window = Column(Integer, nullable=False, default=120)
    price_below_ema_pct = Column(Float, nullable=False, default=10.0)
    volume_lookback_days = Column(Integer, nullable=False, default=20)
    volume_consecutive_days = Column(Integer, nullable=False, default=3)
    volume_ratio_threshold = Column(Float, nullable=False, default=1.4)
    trailing_stop_pct = Column(Float, nullable=False, default=5.0)
    trailing_stop_atr_window = Column(Integer, nullable=False, default=20)
    trailing_stop_atr_multiple = Column(Float, nullable=False, default=2.5)
    stale_high_days = Column(Integer, nullable=False, default=5)
    last_run_at = Column(DateTime)
    last_run_date = Column(Date)
    last_run_status = Column(String(16))
    last_run_message = Column(String(500))
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class ValuationSimLog(Base):
    """EVC 估值成长策略模拟盘运行日志。"""
    __tablename__ = "valuation_sim_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_id = Column(Integer, ForeignKey("valuation_sim_configs.id"), index=True, nullable=False)
    account_id = Column(String, index=True)
    timestamp = Column(DateTime, default=datetime.now, index=True)
    trigger_source = Column(String(16), nullable=False, default="auto")
    status = Column(String(16), nullable=False)
    action = Column(String(32), nullable=False, default="RUN")
    trade_date = Column(Date)
    candidate_count = Column(Integer, default=0)
    buy_count = Column(Integer, default=0)
    sell_count = Column(Integer, default=0)
    total_equity = Column(Float)
    message = Column(String(1000))

class PortfolioCopyConfig(Base):
    """投资组合跟单配置"""
    __tablename__ = "portfolio_copy_configs"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True)
    enabled = Column(Boolean, default=False)
    portfolio_id = Column(String, nullable=True)
    ib_account_id = Column(Integer, nullable=True) # 关联的 IB 账户 ID
    cron_rule = Column(String, default="0 8 * * *") # 默认每天 8 点
    timezone = Column(String, default="America/New_York") # 触发时区
    ib_port = Column(Integer, nullable=True) # 以前是 nullable=False，现在允许为空 (如果使用了 ib_account_id)
    total_position_ratio = Column(Float, default=100.0) # 操作的总仓位比例 (%)
    total_amount = Column(Float) # 或者操作的总金额
    tracking_error_pct = Column(Float, default=5.0) # 跟踪误差 (%)
    api_headers = Column(JSON) # 包含 Cookie, User-Agent 等
    portfolio_name = Column(String(100))
    
    # 新增字段以支持长桥
    account_type = Column(String, default="ib") # "ib", "longport", or "external"
    longport_account_id = Column(String, nullable=True) # 关联的长桥账户 ID (lp_account_id)
    external_trading_account_id = Column(Integer, nullable=True) # 绑定的外部交易账号
    live_sub_account_id = Column(Integer, nullable=True) # 绑定的虚拟子账户
    platform = Column(String, default="futu") # "futu", "star_wealth", or "yingli"
    
    # 历史字段：日均线策略已停止支持，保留列以兼容旧数据
    symbol = Column(String, nullable=True) # 交易标
    ma_short = Column(Integer, nullable=True) # 短周期
    ma_long = Column(Integer, nullable=True)  # 长周期
    last_external_sync_at = Column(DateTime, nullable=True)
    last_external_sync_status = Column(String(16), nullable=True)
    last_external_sync_message = Column(String(500), nullable=True)
    
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class PortfolioCopyLog(Base):
    """投资组合跟单日志"""
    __tablename__ = "portfolio_copy_logs"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    config_id = Column(Integer, index=True, nullable=True) # 关联的配置ID
    account_id = Column(String, index=True)
    timestamp = Column(DateTime, default=datetime.now, index=True)
    portfolio_id = Column(String)
    action = Column(String) # REBALANCE, FETCH_ERROR, etc.
    symbol = Column(String)
    quantity = Column(Float)
    price = Column(Float)
    status = Column(String) # SUCCESS, FAILED
    message = Column(String)

class SnowballCopyConfig(Base):
    """雪球组合跟单配置"""
    __tablename__ = "snowball_copy_configs"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True) # 归属的 Web 账户 ID (迁移新增)
    cli_id = Column(String, index=True, nullable=False) # 外部调用标识 (允许重复，支持多组合)
    combination_id = Column(String, nullable=False) # 雪球组合ID, 如 ZH123456
    combination_name = Column(String)
    enabled = Column(Boolean, default=True)
    total_position_ratio = Column(Float, default=100.0) # 总仓位比例 (%)
    total_amount = Column(Float) # 总金额 (优先)
    tracking_error_pct = Column(Float, default=1.0) # 跟踪误差 (%)
    blacklisted_symbols = Column(JSON, default=list) # 黑名单股票列表
    live_trade_enabled = Column(Boolean, default=False, nullable=False) # 是否启用通用执行器实盘跟单
    external_trading_account_id = Column(Integer, nullable=True) # 绑定的外部交易账号
    live_sub_account_id = Column(Integer, nullable=True) # 绑定的虚拟子账户
    last_external_sync_at = Column(DateTime)
    last_external_sync_status = Column(String(16))
    last_external_sync_message = Column(String(500))
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class SnowballAccountConfig(Base):
    """雪球账户全局配置"""
    __tablename__ = "snowball_account_configs"
    
    account_id = Column(String, primary_key=True) # 归属的 Web 账户 ID
    xueqiu_cookie = Column(String, nullable=True) # 雪球全局Cookie
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class SnowballCopyLog(Base):
    """雪球跟单日志"""
    __tablename__ = "snowball_copy_logs"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True) # 归属的 Web 账户 ID (迁移新增)
    cli_id = Column(String, index=True)
    timestamp = Column(DateTime, default=datetime.now, index=True)
    combination_id = Column(String)
    action = Column(String)
    symbol = Column(String)
    quantity = Column(Float)
    price = Column(Float)
    status = Column(String)
    message = Column(String)

class SnowballBacktestRun(Base):
    """雪球组合跟单回测运行记录"""
    __tablename__ = "snowball_backtest_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True, nullable=False)
    config_id = Column(Integer, index=True, nullable=False)
    combination_id = Column(String, index=True, nullable=False)
    combination_name = Column(String)
    status = Column(String(16), default="RUNNING", index=True, nullable=False)
    slippage_pct = Column(Float, default=0.1, nullable=False)
    requested_start_date = Column(Date)
    requested_end_date = Column(Date)
    effective_start_date = Column(Date)
    actual_nav_start = Column(Date)
    actual_nav_end = Column(Date)
    actual_rebalance_start = Column(DateTime)
    benchmark_symbol = Column(String(32), default="000905.SH")
    benchmark_name = Column(String(64), default="中证500")
    performance_raw = Column(JSON)
    performance_after_slippage = Column(JSON)
    benchmark_metrics = Column(JSON)
    slippage = Column(JSON)
    comparison = Column(JSON)
    rebalancing = Column(JSON)
    rebalance_fetch = Column(JSON)
    yearly_returns = Column(JSON)
    error_message = Column(Text)
    created_at = Column(DateTime, default=datetime.now, index=True)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class SnowballBacktestCurvePoint(Base):
    """雪球组合回测收益曲线点"""
    __tablename__ = "snowball_backtest_curve_points"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("snowball_backtest_runs.id"), index=True, nullable=False)
    date = Column(Date, index=True, nullable=False)
    raw_nav = Column(Float)
    slippage_nav = Column(Float)
    benchmark_nav = Column(Float)
    raw_return_pct = Column(Float)
    slippage_return_pct = Column(Float)
    benchmark_return_pct = Column(Float)
    raw_drawdown_pct = Column(Float)
    slippage_drawdown_pct = Column(Float)
    benchmark_drawdown_pct = Column(Float)
    slippage_cost_pct = Column(Float)

class XueqiuCubeRankCache(Base):
    """雪球组合榜单缓存。"""
    __tablename__ = "xueqiu_cube_rank_cache"
    __table_args__ = (
        UniqueConstraint("rank_type", "year_rank", name="uniq_xueqiu_cube_rank_type_rank"),
        UniqueConstraint("rank_type", "symbol", name="uniq_xueqiu_cube_rank_type_symbol"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    rank_type = Column(String(32), default="year", nullable=False, index=True)
    year_rank = Column(Integer, nullable=False, index=True)
    symbol = Column(String(32), nullable=False, index=True)
    cube_id = Column(Integer)
    cube_name = Column(String(200))
    screen_name = Column(String(200))
    daily_gain = Column(Float)
    week_gain = Column(Float)
    year_gain = Column(Float)
    recommend_count = Column(Integer)
    net_value = Column(Float)
    raw_data = Column(JSON)
    fetched_at = Column(DateTime, default=datetime.now, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class XueqiuCubeActivityCache(Base):
    """雪球组合调仓活跃状态缓存。"""
    __tablename__ = "xueqiu_cube_activity_cache"
    __table_args__ = (
        UniqueConstraint("activity_type", "symbol", name="uniq_xueqiu_cube_activity_type_symbol"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    activity_type = Column(String(64), nullable=False, index=True)
    symbol = Column(String(32), nullable=False, index=True)
    latest_rebalance_at = Column(DateTime, index=True)
    latest_rebalance_id = Column(Integer)
    latest_rebalance_status = Column(String(32))
    latest_rebalance_category = Column(String(64))
    pages_fetched = Column(Integer)
    page_limit_hit = Column(Boolean, default=False, nullable=False)
    raw_data = Column(JSON)
    checked_at = Column(DateTime, default=datetime.now, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class XueqiuTopHoldingsRun(Base):
    """雪球年榜组合综合持仓权重与自动调仓运行记录。"""
    __tablename__ = "xueqiu_top_holdings_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_at = Column(DateTime, default=datetime.now, nullable=False, index=True)
    target_cube_symbol = Column(String(32), nullable=False, index=True)
    target_cube_id = Column(Integer)
    status = Column(String(16), nullable=False, default="RUNNING", index=True)
    message = Column(Text)
    dry_run = Column(Boolean, default=False, nullable=False)
    rank_cache_fetched_at = Column(DateTime)
    rank_cache_refreshed = Column(Boolean, default=False, nullable=False)
    cube_count = Column(Integer)
    success_count = Column(Integer)
    failed_count = Column(Integer)
    stock_count = Column(Integer)
    top_n = Column(Integer, default=10, nullable=False)
    cash_pct = Column(Float)
    top_holdings = Column(JSON)
    failed_cubes = Column(JSON)
    rebalance_payload = Column(JSON)
    rebalance_response = Column(JSON)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class FearGreedSignalConfig(Base):
    """自算贪恐底/顶信号统一配置（全局单份，星澜壹贰叁号与历史曲线共用）。

    两种信号类型（均线型 / 量能型）的阈值、放缩量标准差、回看与冷却天数统一配置：
    - 均线型底：恐贪 MA5 上穿(当日>前一日)且最近 ma5_lookback_days 日任意恐贪 ≤ ma5_bottom_score
    - 均线型顶：恐贪 MA5 下穿(当日<前一日)且最近 ma5_lookback_days 日任意恐贪 ≥ ma5_top_score
    - 量能型底：恐贪 ≤ volume_bottom_score 且放量（log 量比 z > volume_expand_std）
    - 量能型顶：恐贪 ≥ volume_top_score 且缩量（log 量比 z < -volume_shrink_std）
    同类信号（各类型顶/底分别独立）出后 cooldown_days 个交易日不重复出信号。
    """
    __tablename__ = "fear_greed_signal_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 均线型
    ma5_bottom_score = Column(Float, nullable=False, default=25.0)
    ma5_top_score = Column(Float, nullable=False, default=75.0)
    ma5_lookback_days = Column(Integer, nullable=False, default=5)
    # 量能型
    volume_bottom_score = Column(Float, nullable=False, default=30.0)
    volume_top_score = Column(Float, nullable=False, default=75.0)
    volume_expand_std = Column(Float, nullable=False, default=1.25)
    volume_shrink_std = Column(Float, nullable=False, default=0.25)
    # 冷却：同类信号出后 N 个交易日不重复
    cooldown_days = Column(Integer, nullable=False, default=5)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class XueqiuStrategyConfig(Base):
    """雪球星澜组合策略参数（壹号综合权重/贰号排名加速/叁号权价比）。

    仅保留雪球组合相关的目标仓位参数；底/顶信号检测参数（恐贪阈值、放缩量
    标准差、MA5 阈值/回看、冷却）统一走 fear_greed_signal_configs 表。
    """
    __tablename__ = "xueqiu_strategy_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_key = Column(String(32), unique=True, nullable=False, index=True)
    enabled = Column(Boolean, default=True, nullable=False)
    # 目标仓位：恐慌放量 → fear_target_count 只；贪婪缩量 → greed_target_count 只
    fear_target_count = Column(Integer, nullable=False, default=10)
    greed_target_count = Column(Integer, nullable=False, default=3)
    # 买入资格（贰/叁号买入候选筛选）：综合排名≤current_rank_limit、活跃组合数≥min_holding_cubes、
    # 组合数增加≥holding_cube_increase、总权重上升>min_weight_increase、
    # 策略指标≥metric_threshold（权价比≥x 或 排名上升≥x名）、
    # 或强势新进（5日前未持有、排名≤new_entry_rank_limit 且组合数≥new_entry_min_cubes）
    current_rank_limit = Column(Integer, nullable=False, default=100)
    holding_cube_increase = Column(Integer, nullable=False, default=2)
    metric_threshold = Column(Float, nullable=False, default=1.15)
    new_entry_rank_limit = Column(Integer, nullable=False, default=30)
    new_entry_min_cubes = Column(Integer, nullable=False, default=10)
    min_weight_increase = Column(Float, nullable=False, default=0.0)
    # 买入候选最少持仓组合数
    min_holding_cubes = Column(Integer, nullable=False, default=8)
    # 买入确认：今天符合资格且最近快照日中至少 buy_confirm_prior_days 天也符合（0=只看今天）
    buy_confirm_prior_days = Column(Integer, nullable=False, default=1)
    # 卖出侧参数（贰/叁号）：
    # 硬退出：综合排名>hard_exit_rank 或 组合数<hard_exit_min_cubes 立即卖
    hard_exit_rank = Column(Integer, nullable=False, default=250)
    hard_exit_min_cubes = Column(Integer, nullable=False, default=3)
    # 普通退出：跌出卖出缓冲（按指标排序 Top sell_rank，随目标仓位等比缩放）连续 sell_confirm_days 日，
    # 且持满 min_holding_days 个完整交易日，才与买入配对执行
    sell_rank = Column(Integer, nullable=False, default=30)
    sell_confirm_days = Column(Integer, nullable=False, default=2)
    min_holding_days = Column(Integer, nullable=False, default=5)
    # 缓冲候选（hold 池）：综合排名≤retain_rank_limit 且组合数≥retain_min_cubes
    retain_rank_limit = Column(Integer, nullable=False, default=200)
    retain_min_cubes = Column(Integer, nullable=False, default=5)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class IBKRAccountConfig(Base):
    """IBKR Gateway 账户配置与基础设施管理"""
    __tablename__ = "ib_account_configs"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True)      # 应用内用户账号ID
    name = Column(String, nullable=False)        # 账户别名
    
    # 连接信息
    ib_host = Column(String, default='127.0.0.1')
    ib_port = Column(Integer, unique=True, nullable=False) # 宿主机映射端口，全局唯一
    client_id = Column(Integer, default=1)
    
    # 凭证信息 (Docker 环境变量)
    tws_userid = Column(String)
    tws_password = Column(String)
    trading_mode = Column(String, default='paper') # live / paper
    
    # Docker 管理
    container_name = Column(String)              # Docker 容器名称
    
    # 额外配置 (IB Gateway 环境变量)
    twofa_timeout_action = Column(String, default='restart')
    auto_restart_time = Column(String, default='08:59 PM')
    relogin_after_twofa_timeout = Column(String, default='yes')
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class LongPortAccount(Base):
    """长桥账户管理"""
    __tablename__ = "longport_accounts"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True)      # 应用内用户账号ID
    lp_account_id = Column(String, unique=True, index=True, nullable=False) # 长桥账户ID
    name = Column(String, nullable=False)        # 账户别名
    app_key = Column(String, nullable=False)
    app_secret = Column(String, nullable=False)
    access_token = Column(String)
    access_token_expired_at = Column(DateTime)
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class EVCAccountConfig(Base):
    """EVC 账户配置"""
    __tablename__ = "evc_account_configs"

    account_id = Column(String, primary_key=True)
    evc_username = Column(String)
    evc_password = Column(String)
    evc_cookie = Column(String)
    cookie_expires_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class ScheduledTaskConfig(Base):
    """系统级定时任务配置"""
    __tablename__ = "scheduled_task_configs"

    task_key = Column(String(64), primary_key=True)
    name = Column(String(100), nullable=False)
    description = Column(String(255))
    enabled = Column(Boolean, default=True, nullable=False)
    schedule_time = Column(String(5), nullable=False)
    cron_rule = Column(String(1000))
    timezone = Column(String(64), default="Asia/Shanghai", nullable=False)
    allow_queue = Column(Boolean, default=True, nullable=False)
    parameters = Column(JSON)
    sort_order = Column(Integer, default=0, nullable=False)
    last_trigger_source = Column(String(32))
    last_run_started_at = Column(DateTime)
    last_run_finished_at = Column(DateTime)
    last_run_status = Column(String(16))
    last_run_message = Column(String(4000))
    last_duration_seconds = Column(Float)
    updated_by = Column(String(64))
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class EmailRecipientConfig(Base):
    """邮件收件人配置：默认邮箱与场景覆盖。"""
    __tablename__ = "email_recipient_configs"

    scenario_key = Column(String(100), primary_key=True)
    recipient_email = Column(String(1000))
    updated_by = Column(String(64))
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class FactorLiveTradingConfig(Base):
    """因子线上交易配置：复用因子回测参数生成信号，并同步到外部交易执行器。"""
    __tablename__ = "factor_live_trading_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True)
    name = Column(String(100), nullable=False, default="因子线上交易")
    enabled = Column(Boolean, default=False, nullable=False)
    request_payload = Column(JSON, nullable=False)
    external_trading_account_id = Column(Integer, nullable=True)
    live_sub_account_id = Column(Integer, nullable=True)
    signal_time = Column(String(5), default="18:35", nullable=False)
    signal_timezone = Column(String(64), default="Asia/Shanghai", nullable=False)
    execution_time = Column(String(5), default="09:31", nullable=False)
    execution_timezone = Column(String(64), default="Asia/Shanghai", nullable=False)
    last_signal_date = Column(Date)
    last_signal_at = Column(DateTime)
    last_signal_status = Column(String(16))
    last_signal_message = Column(String(1000))
    last_signal_payload = Column(JSON)
    last_execution_signal_date = Column(Date)
    last_execution_at = Column(DateTime)
    last_execution_status = Column(String(16))
    last_execution_message = Column(String(1000))
    last_execution_payload = Column(JSON)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class FactorLiveTradingLog(Base):
    """因子线上交易信号和执行日志。"""
    __tablename__ = "factor_live_trading_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_id = Column(Integer, ForeignKey("factor_live_trading_configs.id"), index=True)
    account_id = Column(String, index=True)
    timestamp = Column(DateTime, default=datetime.now, index=True)
    action = Column(String(32), nullable=False)
    status = Column(String(16), nullable=False)
    signal_date = Column(Date)
    message = Column(String(1000))
    payload = Column(JSON)

class FactorBacktestSearchState(Base):
    """因子回测批量搜索全局状态。只保留最近一次搜索。"""
    __tablename__ = "factor_backtest_search_state"

    id = Column(Integer, primary_key=True, default=1)
    account_id = Column(String, index=True)
    status = Column(String(16), nullable=False, default="idle")
    objective = Column(String(64), nullable=False, default="annualized_return")
    request_payload = Column(JSON)
    search_params = Column(JSON)
    total_cases = Column(Integer, default=0)
    submitted_cases = Column(Integer, default=0)
    completed_cases = Column(Integer, default=0)
    failed_cases = Column(Integer, default=0)
    top_n = Column(Integer, default=200)
    worker_count = Column(Integer, default=1)
    current_case = Column(String(1000))
    error = Column(String(1000))
    cancel_requested = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class FactorBacktestSearchResult(Base):
    """因子回测批量搜索结果。只保留最近一次搜索的完整结果。"""
    __tablename__ = "factor_backtest_search_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    search_id = Column(Integer, index=True, default=1)
    rank = Column(Integer, index=True)
    case_index = Column(Integer, index=True)
    objective = Column(String(64))
    objective_value = Column(Float)
    params_label = Column(String(1000))
    max_positions = Column(Integer)
    sell_rank_multiplier = Column(Float)
    total_return = Column(Float)
    annualized_return = Column(Float)
    sharpe = Column(Float)
    calmar = Column(Float)
    annualized_volatility = Column(Float)
    max_drawdown = Column(Float)
    in_sample_total_return = Column(Float)
    in_sample_annualized_return = Column(Float)
    in_sample_sharpe = Column(Float)
    in_sample_calmar = Column(Float)
    in_sample_annualized_volatility = Column(Float)
    in_sample_max_drawdown = Column(Float)
    oos_total_return = Column(Float)
    oos_annualized_return = Column(Float)
    oos_sharpe = Column(Float)
    oos_calmar = Column(Float)
    oos_annualized_volatility = Column(Float)
    oos_max_drawdown = Column(Float)
    ending_value = Column(Float)
    trade_count = Column(Integer)
    win_rate = Column(Float)
    rebalance_count = Column(Integer)
    holding_count = Column(Integer)
    request_payload = Column(JSON)
    created_at = Column(DateTime, default=datetime.now)

# 创建所有表
Base.metadata.create_all(engine)

def drop_deprecated_tables():
    """删除已经迁移或下线的 SQLite 旧表。"""
    deprecated_tables = [
        "stock_klines",
        "etf_emotions",
        "multi_factor_sim_accounts",
        "multi_factor_sim_daily_history",
        "snowball_api_heartbeats",
        "snowball_portfolio_snapshots",
        "external_trading_order_fills",
        "external_trading_orders",
        "external_trading_target_positions",
        "external_trading_ledger_positions",
        "external_trading_sub_accounts",
        "external_trading_accounts",
        "w20_momentum_live_executions",
        "w20_momentum_live_equity",
        "w20_momentum_live_trades",
        "w20_momentum_live_holdings",
        "w20_momentum_live_logs",
        "w20_momentum_live_configs",
        "us_stock_signal_virtual_events",
        "us_stock_signal_virtual_equity",
        "us_stock_signal_virtual_trades",
        "us_stock_signal_virtual_holdings",
        "us_stock_signal_virtual_logs",
        "us_stock_signal_virtual_configs",
        "a_stock_innovation_momentum_events",
        "a_stock_innovation_momentum_equity",
        "a_stock_innovation_momentum_trades",
        "a_stock_innovation_momentum_holdings",
        "a_stock_innovation_momentum_logs",
        "a_stock_innovation_momentum_configs",
        "valuation_sim_positions",
        "valuation_sim_pending_orders",
        "valuation_sim_trades",
        "valuation_sim_equity",
    ]
    with engine.begin() as conn:
        for table_name in deprecated_tables:
            conn.execute(text(f"DROP TABLE IF EXISTS {table_name}"))

drop_deprecated_tables()

def drop_deprecated_columns():
    """删除存量库中已经不再由模型定义的旧字段。"""
    deprecated_columns = {
        "automated_trading_configs": [
            "fixed_quantity",
        ],
        "evc_account_configs": [
            "access_token",
            "access_token_expired_at",
        ],
        "snowball_copy_configs": [
            "max_slippage_pct",
            "xueqiu_cookie",
        ],
        "valuation_sim_configs": [
            "initial_cash",
            "current_cash",
        ],
    }

    with engine.begin() as conn:
        for table_name, columns in deprecated_columns.items():
            existing = {
                row[1]
                for row in conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
            }
            for column_name in columns:
                if column_name in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table_name} DROP COLUMN {column_name}")

drop_deprecated_columns()

def ensure_performance_indexes():
    """为高频查询补充索引（幂等执行，适配存量数据库）。"""
    index_sqls = [
        "CREATE INDEX IF NOT EXISTS idx_stock_evc_date_symbol ON stock_evc(date, symbol)",
        "CREATE INDEX IF NOT EXISTS idx_stock_tags_tag_date_symbol ON stock_tags(tag_id, date, stock_symbol)",
        "CREATE INDEX IF NOT EXISTS idx_stock_tags_date_symbol ON stock_tags(date, stock_symbol)",
        "CREATE INDEX IF NOT EXISTS idx_stock_favorites_account_symbol ON stock_favorites(account_id, symbol)",
        "CREATE INDEX IF NOT EXISTS idx_db_sql_favorites_account_updated ON db_sql_favorites(account_id, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_invalid_symbol_cache_source_updated ON invalid_symbol_cache(source, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_innovation100_levels_date ON a_stock_innovation100_levels(index_code, date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_innovation100_rebalances_date ON a_stock_innovation100_rebalances(index_code, rebalance_date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_innovation100_constituents_symbol ON a_stock_innovation100_constituents(index_code, ts_code, rebalance_date)",
        "CREATE INDEX IF NOT EXISTS idx_factor_backtest_search_results_rank ON factor_backtest_search_results(search_id, rank)",
        "CREATE INDEX IF NOT EXISTS idx_factor_backtest_search_results_case ON factor_backtest_search_results(search_id, case_index)",
        "CREATE INDEX IF NOT EXISTS idx_factor_backtest_search_results_objective ON factor_backtest_search_results(search_id, objective_value)",
        "CREATE INDEX IF NOT EXISTS idx_factor_backtest_search_results_return ON factor_backtest_search_results(search_id, annualized_return)",
        "CREATE INDEX IF NOT EXISTS idx_factor_backtest_search_results_sharpe ON factor_backtest_search_results(search_id, sharpe)",
        "CREATE INDEX IF NOT EXISTS idx_factor_backtest_search_results_calmar ON factor_backtest_search_results(search_id, calmar)",
        "CREATE INDEX IF NOT EXISTS idx_factor_backtest_search_results_oos_return ON factor_backtest_search_results(search_id, oos_annualized_return)",
        "CREATE INDEX IF NOT EXISTS idx_factor_backtest_search_results_oos_sharpe ON factor_backtest_search_results(search_id, oos_sharpe)",
        "CREATE INDEX IF NOT EXISTS idx_factor_live_trading_configs_account ON factor_live_trading_configs(account_id)",
        "CREATE INDEX IF NOT EXISTS idx_factor_live_trading_configs_enabled ON factor_live_trading_configs(enabled)",
        "CREATE INDEX IF NOT EXISTS idx_factor_live_trading_logs_config_time ON factor_live_trading_logs(config_id, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_valuation_sim_configs_account ON valuation_sim_configs(account_id)",
        "CREATE INDEX IF NOT EXISTS idx_valuation_sim_configs_enabled ON valuation_sim_configs(enabled)",
        "CREATE INDEX IF NOT EXISTS idx_valuation_sim_logs_config_time ON valuation_sim_logs(config_id, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_snowball_backtest_runs_account_config ON snowball_backtest_runs(account_id, config_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_snowball_backtest_runs_status ON snowball_backtest_runs(status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_snowball_backtest_curve_run_date ON snowball_backtest_curve_points(run_id, date)",
        "CREATE INDEX IF NOT EXISTS idx_xueqiu_cube_rank_cache_type_fetched ON xueqiu_cube_rank_cache(rank_type, fetched_at)",
        "CREATE INDEX IF NOT EXISTS idx_xueqiu_cube_activity_cache_type_checked ON xueqiu_cube_activity_cache(activity_type, checked_at)",
        "CREATE INDEX IF NOT EXISTS idx_xueqiu_cube_activity_cache_type_latest ON xueqiu_cube_activity_cache(activity_type, latest_rebalance_at)",
        "CREATE INDEX IF NOT EXISTS idx_xueqiu_top_holdings_runs_target_time ON xueqiu_top_holdings_runs(target_cube_symbol, run_at)",
        "CREATE INDEX IF NOT EXISTS idx_etf_put_call_ratios_date_symbol ON etf_put_call_ratios(date, symbol)",
        "CREATE INDEX IF NOT EXISTS idx_etf_option_expirations_snapshot_symbol ON etf_option_expirations(snapshot_date, symbol)",
        "CREATE INDEX IF NOT EXISTS idx_etf_option_expirations_expiration ON etf_option_expirations(expiration_date)",
        "CREATE INDEX IF NOT EXISTS idx_ai_stock_runs_date_type ON ai_stock_recommendation_runs(trade_date, run_type, run_at)",
        "CREATE INDEX IF NOT EXISTS idx_ai_stock_recommendations_run_rank ON ai_stock_recommendations(run_id, rank)",
        "CREATE INDEX IF NOT EXISTS idx_ai_stock_recommendations_symbol_created ON ai_stock_recommendations(ts_code, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_ai_stock_ths_index_type_fetched ON ai_stock_ths_index_cache(index_type, fetched_at)",
        "CREATE INDEX IF NOT EXISTS idx_ai_stock_lots_portfolio_symbol ON ai_stock_paper_lots(portfolio_id, ts_code)",
        "CREATE INDEX IF NOT EXISTS idx_ai_stock_trades_portfolio_time ON ai_stock_paper_trades(portfolio_id, executed_at)",
        "CREATE INDEX IF NOT EXISTS idx_ai_stock_benchmark_type_time ON ai_stock_benchmark_snapshots(snapshot_type, captured_at)",
    ]
    with engine.begin() as conn:
        for sql in index_sqls:
            conn.execute(text(sql))

ensure_performance_indexes()

def ensure_table_columns():
    """为存量表补充新增字段（幂等执行）。"""
    table_columns = {
        "evc_account_configs": {
            "evc_username": "ALTER TABLE evc_account_configs ADD COLUMN evc_username VARCHAR",
            "evc_password": "ALTER TABLE evc_account_configs ADD COLUMN evc_password VARCHAR",
            "evc_cookie": "ALTER TABLE evc_account_configs ADD COLUMN evc_cookie VARCHAR",
            "cookie_expires_at": "ALTER TABLE evc_account_configs ADD COLUMN cookie_expires_at DATETIME",
        },
        "snowball_copy_configs": {
            "live_trade_enabled": "ALTER TABLE snowball_copy_configs ADD COLUMN live_trade_enabled BOOLEAN NOT NULL DEFAULT 0",
            "external_trading_account_id": "ALTER TABLE snowball_copy_configs ADD COLUMN external_trading_account_id INTEGER",
            "live_sub_account_id": "ALTER TABLE snowball_copy_configs ADD COLUMN live_sub_account_id INTEGER",
            "last_external_sync_at": "ALTER TABLE snowball_copy_configs ADD COLUMN last_external_sync_at DATETIME",
            "last_external_sync_status": "ALTER TABLE snowball_copy_configs ADD COLUMN last_external_sync_status VARCHAR(16)",
            "last_external_sync_message": "ALTER TABLE snowball_copy_configs ADD COLUMN last_external_sync_message VARCHAR(500)",
        },
        "portfolio_copy_configs": {
            "external_trading_account_id": "ALTER TABLE portfolio_copy_configs ADD COLUMN external_trading_account_id INTEGER",
            "live_sub_account_id": "ALTER TABLE portfolio_copy_configs ADD COLUMN live_sub_account_id INTEGER",
            "last_external_sync_at": "ALTER TABLE portfolio_copy_configs ADD COLUMN last_external_sync_at DATETIME",
            "last_external_sync_status": "ALTER TABLE portfolio_copy_configs ADD COLUMN last_external_sync_status VARCHAR(16)",
            "last_external_sync_message": "ALTER TABLE portfolio_copy_configs ADD COLUMN last_external_sync_message VARCHAR(500)",
        },
        "scheduled_task_configs": {
            "cron_rule": "ALTER TABLE scheduled_task_configs ADD COLUMN cron_rule VARCHAR(1000)",
            "timezone": "ALTER TABLE scheduled_task_configs ADD COLUMN timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai'",
            "allow_queue": "ALTER TABLE scheduled_task_configs ADD COLUMN allow_queue BOOLEAN NOT NULL DEFAULT 1",
            "parameters": "ALTER TABLE scheduled_task_configs ADD COLUMN parameters JSON",
        },
        "valuation_sim_configs": {
            "universe_tag_ids": "ALTER TABLE valuation_sim_configs ADD COLUMN universe_tag_ids JSON",
            "min_market_cap_100m": "ALTER TABLE valuation_sim_configs ADD COLUMN min_market_cap_100m FLOAT DEFAULT 100.0",
            "max_market_cap_100m": "ALTER TABLE valuation_sim_configs ADD COLUMN max_market_cap_100m FLOAT",
            "external_trading_account_id": "ALTER TABLE valuation_sim_configs ADD COLUMN external_trading_account_id INTEGER",
            "live_sub_account_id": "ALTER TABLE valuation_sim_configs ADD COLUMN live_sub_account_id INTEGER",
            "trailing_stop_atr_window": "ALTER TABLE valuation_sim_configs ADD COLUMN trailing_stop_atr_window INTEGER NOT NULL DEFAULT 20",
            "trailing_stop_atr_multiple": "ALTER TABLE valuation_sim_configs ADD COLUMN trailing_stop_atr_multiple FLOAT NOT NULL DEFAULT 2.5",
        },
        "ai_stock_paper_portfolios": {
            "last_execution_target": "ALTER TABLE ai_stock_paper_portfolios ADD COLUMN last_execution_target FLOAT",
        },
        "ai_stock_paper_lots": {
            "stop_half_triggered": "ALTER TABLE ai_stock_paper_lots ADD COLUMN stop_half_triggered BOOLEAN NOT NULL DEFAULT 0",
            "peak_price": "ALTER TABLE ai_stock_paper_lots ADD COLUMN peak_price FLOAT",
        },
        "xueqiu_strategy_configs": {
            "fear_target_count": "ALTER TABLE xueqiu_strategy_configs ADD COLUMN fear_target_count INTEGER NOT NULL DEFAULT 10",
            "greed_target_count": "ALTER TABLE xueqiu_strategy_configs ADD COLUMN greed_target_count INTEGER NOT NULL DEFAULT 3",
            "ma5_bottom_score": "ALTER TABLE xueqiu_strategy_configs ADD COLUMN ma5_bottom_score FLOAT NOT NULL DEFAULT 25.0",
            "ma5_top_score": "ALTER TABLE xueqiu_strategy_configs ADD COLUMN ma5_top_score FLOAT NOT NULL DEFAULT 75.0",
            "ma5_lookback_days": "ALTER TABLE xueqiu_strategy_configs ADD COLUMN ma5_lookback_days INTEGER NOT NULL DEFAULT 5",
            "current_rank_limit": "ALTER TABLE xueqiu_strategy_configs ADD COLUMN current_rank_limit INTEGER NOT NULL DEFAULT 100",
            "holding_cube_increase": "ALTER TABLE xueqiu_strategy_configs ADD COLUMN holding_cube_increase INTEGER NOT NULL DEFAULT 2",
            "metric_threshold": "ALTER TABLE xueqiu_strategy_configs ADD COLUMN metric_threshold FLOAT NOT NULL DEFAULT 1.15",
            "new_entry_rank_limit": "ALTER TABLE xueqiu_strategy_configs ADD COLUMN new_entry_rank_limit INTEGER NOT NULL DEFAULT 30",
            "new_entry_min_cubes": "ALTER TABLE xueqiu_strategy_configs ADD COLUMN new_entry_min_cubes INTEGER NOT NULL DEFAULT 10",
            "min_weight_increase": "ALTER TABLE xueqiu_strategy_configs ADD COLUMN min_weight_increase FLOAT NOT NULL DEFAULT 0.0",
            "hard_exit_rank": "ALTER TABLE xueqiu_strategy_configs ADD COLUMN hard_exit_rank INTEGER NOT NULL DEFAULT 250",
            "hard_exit_min_cubes": "ALTER TABLE xueqiu_strategy_configs ADD COLUMN hard_exit_min_cubes INTEGER NOT NULL DEFAULT 3",
            "sell_rank": "ALTER TABLE xueqiu_strategy_configs ADD COLUMN sell_rank INTEGER NOT NULL DEFAULT 30",
            "sell_confirm_days": "ALTER TABLE xueqiu_strategy_configs ADD COLUMN sell_confirm_days INTEGER NOT NULL DEFAULT 2",
            "min_holding_days": "ALTER TABLE xueqiu_strategy_configs ADD COLUMN min_holding_days INTEGER NOT NULL DEFAULT 5",
            "retain_rank_limit": "ALTER TABLE xueqiu_strategy_configs ADD COLUMN retain_rank_limit INTEGER NOT NULL DEFAULT 200",
            "retain_min_cubes": "ALTER TABLE xueqiu_strategy_configs ADD COLUMN retain_min_cubes INTEGER NOT NULL DEFAULT 5",
            "buy_confirm_prior_days": "ALTER TABLE xueqiu_strategy_configs ADD COLUMN buy_confirm_prior_days INTEGER NOT NULL DEFAULT 1",
        },
    }

    with engine.begin() as conn:
        for table_name, columns in table_columns.items():
            existing = {
                row[1]
                for row in conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
            }
            for column_name, ddl in columns.items():
                if column_name not in existing:
                    conn.exec_driver_sql(ddl)

ensure_table_columns()

def ensure_soxl_fear_strategy_multi_config_schema():
    """迁移 SOXL 情绪量能策略为多配置模式（幂等执行）。"""

    def get_columns(conn, table_name):
        return {
            row[1]
            for row in conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
        }

    def get_table_sql(conn, table_name):
        return conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:table_name"),
            {"table_name": table_name},
        ).scalar()

    with engine.begin() as conn:
        config_sql = get_table_sql(conn, "soxl_fear_strategy_configs")
        if not config_sql:
            return

        config_columns = get_columns(conn, "soxl_fear_strategy_configs")
        normalized_config_sql = "".join(config_sql.lower().split())
        needs_config_rebuild = (
            "trading_account_id" not in config_columns
            or "created_at" not in config_columns
            or "unique(account_id)" in normalized_config_sql
        )

        if needs_config_rebuild:
            conn.execute(text("ALTER TABLE soxl_fear_strategy_configs RENAME TO soxl_fear_strategy_configs_old"))
            conn.execute(text("""
                CREATE TABLE soxl_fear_strategy_configs (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    account_id VARCHAR,
                    enabled BOOLEAN,
                    symbol VARCHAR NOT NULL DEFAULT 'SOXL.US',
                    account_type VARCHAR,
                    ib_account_id INTEGER,
                    longport_account_id VARCHAR,
                    external_trading_account_id INTEGER,
                    live_sub_account_id INTEGER,
                    trading_account_id VARCHAR,
                    buy_threshold FLOAT NOT NULL DEFAULT 60.0,
                    greed_threshold FLOAT NOT NULL DEFAULT 60.0,
                    volume_ratio_threshold FLOAT NOT NULL DEFAULT 1.4,
                    buy_position_pct FLOAT NOT NULL DEFAULT 50.0,
                    cooldown_days INTEGER NOT NULL DEFAULT 10,
                    trailing_stop_pct FLOAT NOT NULL DEFAULT 5.0,
                    sell_position_pct FLOAT NOT NULL DEFAULT 50.0,
                    sell_reduction_basis VARCHAR NOT NULL DEFAULT 'portfolio',
                    max_take_profit_sells_per_cycle INTEGER NOT NULL DEFAULT 2,
                    min_position_pct_after_take_profit FLOAT NOT NULL DEFAULT 10.0,
                    rebalance_threshold_pct FLOAT NOT NULL DEFAULT 5.0,
                    last_run_at DATETIME,
                    last_run_status VARCHAR(16),
                    last_run_message VARCHAR(500),
                    created_at DATETIME,
                    updated_at DATETIME,
                    CONSTRAINT uniq_soxl_fear_strategy_target_account UNIQUE (symbol, account_type, trading_account_id)
                )
            """))

            def old_column(column_name, fallback):
                return column_name if column_name in config_columns else fallback

            trading_account_expr = (
                "trading_account_id"
                if "trading_account_id" in config_columns
                else """
                    CASE
                        WHEN account_type = 'longport' THEN longport_account_id
                        WHEN ib_account_id IS NOT NULL THEN CAST(ib_account_id AS VARCHAR)
                        ELSE NULL
                    END
                """
            )
            updated_at_expr = old_column("updated_at", "CURRENT_TIMESTAMP")
            created_at_expr = old_column("created_at", f"COALESCE({updated_at_expr}, CURRENT_TIMESTAMP)")
            conn.execute(text(f"""
                INSERT OR IGNORE INTO soxl_fear_strategy_configs (
                    id, account_id, enabled, symbol, account_type, ib_account_id, longport_account_id,
                    external_trading_account_id, live_sub_account_id, trading_account_id,
                    buy_threshold, greed_threshold, volume_ratio_threshold,
                    buy_position_pct, cooldown_days, trailing_stop_pct, sell_position_pct,
                    sell_reduction_basis, max_take_profit_sells_per_cycle,
                    min_position_pct_after_take_profit, rebalance_threshold_pct,
                    last_run_at, last_run_status, last_run_message, created_at, updated_at
                )
                SELECT
                    {old_column("id", "NULL")},
                    {old_column("account_id", "NULL")},
                    COALESCE({old_column("enabled", "0")}, 0),
                    COALESCE({old_column("symbol", "'SOXL.US'")}, 'SOXL.US'),
                    COALESCE({old_column("account_type", "'ib'")}, 'ib'),
                    {old_column("ib_account_id", "NULL")},
                    {old_column("longport_account_id", "NULL")},
                    {old_column("external_trading_account_id", "NULL")},
                    {old_column("live_sub_account_id", "NULL")},
                    {trading_account_expr},
                    COALESCE({old_column("buy_threshold", "60.0")}, 60.0),
                    COALESCE({old_column("greed_threshold", "60.0")}, 60.0),
                    COALESCE({old_column("volume_ratio_threshold", "1.4")}, 1.4),
                    COALESCE({old_column("buy_position_pct", "50.0")}, 50.0),
                    COALESCE({old_column("cooldown_days", "10")}, 10),
                    COALESCE({old_column("trailing_stop_pct", "5.0")}, 5.0),
                    COALESCE({old_column("sell_position_pct", "50.0")}, 50.0),
                    COALESCE({old_column("sell_reduction_basis", "'portfolio'")}, 'portfolio'),
                    COALESCE({old_column("max_take_profit_sells_per_cycle", "2")}, 2),
                    COALESCE({old_column("min_position_pct_after_take_profit", "10.0")}, 10.0),
                    COALESCE({old_column("rebalance_threshold_pct", "5.0")}, 5.0),
                    {old_column("last_run_at", "NULL")},
                    {old_column("last_run_status", "NULL")},
                    {old_column("last_run_message", "NULL")},
                    {created_at_expr},
                    {updated_at_expr}
                FROM soxl_fear_strategy_configs_old
            """))
            conn.execute(text("DROP TABLE soxl_fear_strategy_configs_old"))

        config_columns = get_columns(conn, "soxl_fear_strategy_configs")
        soxl_config_column_ddls = {
            "external_trading_account_id": "ALTER TABLE soxl_fear_strategy_configs ADD COLUMN external_trading_account_id INTEGER",
            "live_sub_account_id": "ALTER TABLE soxl_fear_strategy_configs ADD COLUMN live_sub_account_id INTEGER",
        }
        for column_name, ddl in soxl_config_column_ddls.items():
            if column_name not in config_columns:
                conn.execute(text(ddl))

        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_soxl_fear_strategy_configs_account_id "
            "ON soxl_fear_strategy_configs(account_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_soxl_fear_strategy_configs_external_account "
            "ON soxl_fear_strategy_configs(external_trading_account_id, live_sub_account_id)"
        ))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_soxl_fear_strategy_unique_target_account "
            "ON soxl_fear_strategy_configs(symbol, account_type, trading_account_id) "
            "WHERE trading_account_id IS NOT NULL"
        ))

        state_sql = get_table_sql(conn, "soxl_fear_strategy_states")
        if state_sql:
            state_columns = get_columns(conn, "soxl_fear_strategy_states")
            normalized_state_sql = "".join(state_sql.lower().split())
            needs_state_rebuild = (
                "config_id" not in state_columns
                or "primarykey(account_id)" in normalized_state_sql
                or "account_idvarcharnotnull" in normalized_state_sql
            )
            if needs_state_rebuild:
                conn.execute(text("ALTER TABLE soxl_fear_strategy_states RENAME TO soxl_fear_strategy_states_old"))
                conn.execute(text("""
                    CREATE TABLE soxl_fear_strategy_states (
                        config_id INTEGER NOT NULL PRIMARY KEY,
                        account_id VARCHAR,
                        symbol VARCHAR NOT NULL DEFAULT 'SOXL.US',
                        last_processed_date DATE,
                        cooldown_remaining_days INTEGER NOT NULL DEFAULT 0,
                        greed_peak_price FLOAT,
                        take_profit_cycle_sell_count INTEGER NOT NULL DEFAULT 0,
                        updated_at DATETIME,
                        FOREIGN KEY(config_id) REFERENCES soxl_fear_strategy_configs (id)
                    )
                """))

                def old_state_column(column_name, fallback):
                    return f"s.{column_name}" if column_name in state_columns else fallback

                if "config_id" in state_columns:
                    config_id_expr = "s.config_id"
                    join_expr = "LEFT JOIN soxl_fear_strategy_configs c ON c.id = s.config_id"
                    account_id_expr = (
                        "COALESCE(s.account_id, c.account_id)"
                        if "account_id" in state_columns
                        else "c.account_id"
                    )
                else:
                    config_id_expr = "c.id"
                    join_expr = """
                        JOIN soxl_fear_strategy_configs c
                            ON c.id = (
                                SELECT c2.id
                                FROM soxl_fear_strategy_configs c2
                                WHERE c2.account_id = s.account_id
                                ORDER BY c2.id
                                LIMIT 1
                            )
                    """
                    account_id_expr = old_state_column("account_id", "c.account_id")

                state_updated_at_expr = old_state_column("updated_at", "CURRENT_TIMESTAMP")
                conn.execute(text(f"""
                    INSERT OR IGNORE INTO soxl_fear_strategy_states (
                        config_id, account_id, symbol, last_processed_date,
                        cooldown_remaining_days, greed_peak_price,
                        take_profit_cycle_sell_count, updated_at
                    )
                    SELECT
                        {config_id_expr},
                        {account_id_expr},
                        COALESCE({old_state_column("symbol", "c.symbol")}, COALESCE(c.symbol, 'SOXL.US')),
                        {old_state_column("last_processed_date", "NULL")},
                        COALESCE({old_state_column("cooldown_remaining_days", "0")}, 0),
                        {old_state_column("greed_peak_price", "NULL")},
                        COALESCE({old_state_column("take_profit_cycle_sell_count", "0")}, 0),
                        {state_updated_at_expr}
                    FROM soxl_fear_strategy_states_old s
                    {join_expr}
                    WHERE {config_id_expr} IS NOT NULL
                """))
                conn.execute(text("DROP TABLE soxl_fear_strategy_states_old"))

        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_soxl_fear_strategy_states_account_id "
            "ON soxl_fear_strategy_states(account_id)"
        ))

        log_columns = get_columns(conn, "soxl_fear_strategy_logs")
        if "config_id" not in log_columns:
            conn.execute(text("ALTER TABLE soxl_fear_strategy_logs ADD COLUMN config_id INTEGER"))

        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_soxl_fear_strategy_logs_config_id "
            "ON soxl_fear_strategy_logs(config_id)"
        ))
        conn.execute(text("""
            UPDATE soxl_fear_strategy_logs
            SET config_id = (
                SELECT c.id
                FROM soxl_fear_strategy_configs c
                WHERE c.account_id = soxl_fear_strategy_logs.account_id
                  AND c.symbol = soxl_fear_strategy_logs.symbol
                ORDER BY c.id
                LIMIT 1
            )
            WHERE config_id IS NULL
        """))

ensure_soxl_fear_strategy_multi_config_schema()


def ensure_a_stock_fear_strategy_schema():
    """A股情绪量能策略表结构迁移（幂等）：给 configs 表补跷跷板候补列。"""
    with engine.begin() as conn:
        columns = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(a_stock_fear_strategy_configs)")).fetchall()
        }
        additions = [
            ("sub_symbol", "VARCHAR(32)"),
            ("sub_fear_source", "VARCHAR(64)"),
            ("sub_volume_signal_symbol", "VARCHAR(32)"),
            ("sub_buy_threshold", "FLOAT DEFAULT 25.0"),
            ("sub_volume_ratio_threshold", "FLOAT DEFAULT 1.6"),
            ("sub2_symbol", "VARCHAR(32)"),
            ("sub2_fear_source", "VARCHAR(64)"),
            ("sub2_volume_signal_symbol", "VARCHAR(32)"),
            ("sub2_buy_threshold", "FLOAT DEFAULT 20.0"),
            ("sub2_volume_ratio_threshold", "FLOAT DEFAULT 1.3"),
            ("swap_threshold", "FLOAT"),
            ("volume_z_threshold", "FLOAT"),
            ("sell_shrink_z", "FLOAT DEFAULT -1.0"),
        ]
        for column_name, column_type in additions:
            if column_name not in columns:
                conn.execute(text(
                    f"ALTER TABLE a_stock_fear_strategy_configs ADD COLUMN {column_name} {column_type}"
                ))

ensure_a_stock_fear_strategy_schema()

def get_db():
    """FastAPI dependency for database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@contextmanager
def get_db_ctx():
    """Context manager for database session, for use in 'with' statements"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

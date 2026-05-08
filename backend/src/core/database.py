from sqlalchemy import create_engine, Column, String, Float, Boolean, DateTime, Date, Integer, ForeignKey, Table, PrimaryKeyConstraint, UniqueConstraint, JSON, Text, text, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, scoped_session, relationship
from contextlib import contextmanager
import os
from datetime import datetime

# 创建基础目录
DB_PATH = '/var/lib/quant_robot/evc_stocks.db'
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
Session = scoped_session(sessionmaker(bind=engine))

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
    """ETF恐贪复刻指数历史记录"""
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
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

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
    account_type = Column(String, default="ib") # "ib" or "longport"
    longport_account_id = Column(String, nullable=True) # 关联的长桥账户 ID (lp_account_id)
    platform = Column(String, default="futu") # "futu", "star_wealth", "yingli", or "daily_ma"
    
    # 新增字段以支持日均线策略
    symbol = Column(String, nullable=True) # 交易标
    ma_short = Column(Integer, nullable=True) # 短周期
    ma_long = Column(Integer, nullable=True)  # 长周期
    
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
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class SnowballAccountConfig(Base):
    """雪球账户全局配置"""
    __tablename__ = "snowball_account_configs"
    
    account_id = Column(String, primary_key=True) # 归属的 Web 账户 ID
    xueqiu_cookie = Column(String, nullable=True) # 雪球全局Cookie
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class SnowballPortfolioSnapshot(Base):
    """雪球组合持仓快照"""
    __tablename__ = "snowball_portfolio_snapshots"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True) # 归属的 Web 账户 ID (迁移新增)
    config_id = Column(Integer, ForeignKey("snowball_copy_configs.id"), nullable=False)
    holdings = Column(JSON) # {symbol: quantity}
    cash = Column(Float, default=0.0)
    market_value = Column(Float, default=0.0)
    last_synced_amount = Column(Float, default=0.0)
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

class W20MomentumLiveConfig(Base):
    """W20 风险调整 ETF 动量虚拟盘配置"""
    __tablename__ = "w20_momentum_live_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True)
    name = Column(String(100), nullable=False, default="W20 风险调整 ETF 动量")
    enabled = Column(Boolean, default=True, nullable=False)
    symbols = Column(JSON, nullable=False)
    benchmark_symbols = Column(JSON)
    initial_capital = Column(Float, nullable=False, default=1_000_000.0)
    start_date = Column(Date, nullable=False)
    window = Column(Integer, nullable=False, default=20)
    top_weights = Column(JSON, nullable=False)
    rebalance_frequency = Column(String(16), nullable=False, default="weekly")
    drift_threshold_pct = Column(Float, nullable=False, default=100.0)
    commission_pct = Column(Float, nullable=False, default=0.03)
    slippage_pct = Column(Float, nullable=False, default=0.02)
    lot_size = Column(Integer, nullable=False, default=100)
    last_sync_at = Column(DateTime)
    last_sync_status = Column(String(16))
    last_sync_message = Column(String(500))
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class W20MomentumLiveEquity(Base):
    """W20 虚拟盘每日净值"""
    __tablename__ = "w20_momentum_live_equity"

    config_id = Column(Integer, ForeignKey("w20_momentum_live_configs.id"), primary_key=True)
    date = Column(Date, primary_key=True)
    account_id = Column(String, index=True)
    value = Column(Float, nullable=False)
    benchmark_value = Column(Float)
    drawdown = Column(Float)
    benchmark_drawdown = Column(Float)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class W20MomentumLiveTrade(Base):
    """W20 虚拟盘模拟成交记录"""
    __tablename__ = "w20_momentum_live_trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_id = Column(Integer, ForeignKey("w20_momentum_live_configs.id"), index=True)
    account_id = Column(String, index=True)
    date = Column(Date, index=True)
    signal_date = Column(Date)
    action = Column(String(8), nullable=False)
    symbol = Column(String(32), index=True)
    price = Column(Float)
    open_price = Column(Float)
    quantity = Column(Integer)
    amount = Column(Float)
    commission = Column(Float)
    reason = Column(String(64))
    reason_detail = Column(String(1000))
    cash_after = Column(Float)
    portfolio_value_after = Column(Float)
    symbol_market_value_after = Column(Float)
    symbol_weight_pct_after = Column(Float)
    price_source = Column(String(32))
    quote_timestamp = Column(DateTime)
    target_symbols = Column(JSON)
    target_weights_pct = Column(JSON)
    created_at = Column(DateTime, default=datetime.now)

class W20MomentumLiveHolding(Base):
    """W20 虚拟盘最新持仓快照"""
    __tablename__ = "w20_momentum_live_holdings"

    config_id = Column(Integer, ForeignKey("w20_momentum_live_configs.id"), primary_key=True)
    symbol = Column(String(32), primary_key=True)
    account_id = Column(String, index=True)
    shares = Column(Integer, nullable=False, default=0)
    price = Column(Float)
    market_value = Column(Float)
    actual_weight_pct = Column(Float)
    target_weight_pct = Column(Float)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class W20MomentumLiveLog(Base):
    """W20 虚拟盘运行/信号日志"""
    __tablename__ = "w20_momentum_live_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_id = Column(Integer, ForeignKey("w20_momentum_live_configs.id"), index=True)
    account_id = Column(String, index=True)
    timestamp = Column(DateTime, default=datetime.now, index=True)
    date = Column(Date, index=True)
    level = Column(String(16), default="INFO")
    action = Column(String(32), nullable=False)
    message = Column(String(1000))
    payload = Column(JSON)

class USStockSignalVirtualConfig(Base):
    """美股成分股风险调整混合动量虚拟盘配置"""
    __tablename__ = "us_stock_signal_virtual_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True)
    name = Column(String(100), nullable=False, default="美股风险调整混合动量虚拟盘")
    enabled = Column(Boolean, default=True, nullable=False)
    candidate_etfs = Column(JSON, nullable=False)
    initial_capital = Column(Float, nullable=False, default=100_000.0)
    start_date = Column(Date, nullable=False)
    window = Column(Integer, nullable=False, default=20)
    stabilization_period = Column(Integer, nullable=False, default=10)
    volatility_floor_pct = Column(Float, nullable=False, default=15.0)
    volatility_cap_pct = Column(Float, nullable=False, default=45.0)
    min_listing_days = Column(Integer, nullable=False, default=365)
    momentum_weights = Column(JSON, nullable=False, default=lambda: {"20": 0.05, "60": 0.20, "120": 0.75})
    volume_std_multiplier = Column(Float, nullable=False, default=1.0)
    max_positions = Column(Integer, nullable=False, default=7)
    sell_rank_multiplier = Column(Float, nullable=False, default=2.0)
    index_weight_blend = Column(Float, nullable=False, default=0.4)
    rebalance_frequency = Column(String(16), nullable=False, default="weekly")
    commission_pct = Column(Float, nullable=False, default=0.03)
    slippage_pct = Column(Float, nullable=False, default=0.02)
    lot_size = Column(Integer, nullable=False, default=1)
    auto_sync_enabled = Column(Boolean, default=True, nullable=False)
    auto_sync_time = Column(String(5), default="16:15", nullable=False)
    last_auto_sync_at = Column(DateTime)
    last_sync_at = Column(DateTime)
    last_sync_status = Column(String(16))
    last_sync_message = Column(String(500))
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class USStockSignalVirtualEvent(Base):
    """美股成分股风险调整混合动量排名事件"""
    __tablename__ = "us_stock_signal_virtual_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_id = Column(Integer, ForeignKey("us_stock_signal_virtual_configs.id"), index=True)
    account_id = Column(String, index=True)
    symbol = Column(String(32), nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)
    direction = Column(String(8), nullable=False)
    signal_price = Column(Float)
    turnover = Column(Float)
    annualized_volatility_pct = Column(Float)
    threshold_pct = Column(Float)
    payload = Column(JSON)
    price_source = Column(String(32), default="daily_close")
    created_at = Column(DateTime, default=datetime.now)
    __table_args__ = (
        UniqueConstraint("config_id", "symbol", "date", "direction", name="uniq_us_stock_signal_virtual_event"),
    )

class USStockSignalVirtualEquity(Base):
    """美股成分股风险调整混合动量虚拟盘每日净值"""
    __tablename__ = "us_stock_signal_virtual_equity"

    config_id = Column(Integer, ForeignKey("us_stock_signal_virtual_configs.id"), primary_key=True)
    date = Column(Date, primary_key=True)
    account_id = Column(String, index=True)
    value = Column(Float, nullable=False)
    cash = Column(Float)
    position_value = Column(Float)
    drawdown = Column(Float)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class USStockSignalVirtualTrade(Base):
    """美股成分股风险调整混合动量虚拟盘模拟成交"""
    __tablename__ = "us_stock_signal_virtual_trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_id = Column(Integer, ForeignKey("us_stock_signal_virtual_configs.id"), index=True)
    account_id = Column(String, index=True)
    date = Column(Date, index=True)
    signal_date = Column(Date)
    action = Column(String(8), nullable=False)
    symbol = Column(String(32), index=True)
    price = Column(Float)
    quantity = Column(Integer)
    amount = Column(Float)
    commission = Column(Float)
    profit = Column(Float)
    profit_pct = Column(Float)
    reason = Column(String(64))
    reason_detail = Column(String(1000))
    cash_after = Column(Float)
    portfolio_value_after = Column(Float)
    symbol_market_value_after = Column(Float)
    symbol_weight_pct_after = Column(Float)
    price_source = Column(String(32))
    created_at = Column(DateTime, default=datetime.now)

class USStockSignalVirtualHolding(Base):
    """美股成分股风险调整混合动量虚拟盘最新持仓快照"""
    __tablename__ = "us_stock_signal_virtual_holdings"

    config_id = Column(Integer, ForeignKey("us_stock_signal_virtual_configs.id"), primary_key=True)
    symbol = Column(String(32), primary_key=True)
    account_id = Column(String, index=True)
    shares = Column(Integer, nullable=False, default=0)
    price = Column(Float)
    avg_cost = Column(Float)
    entry_date = Column(Date)
    market_value = Column(Float)
    actual_weight_pct = Column(Float)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class USStockSignalVirtualLog(Base):
    """美股成分股风险调整混合动量虚拟盘运行日志"""
    __tablename__ = "us_stock_signal_virtual_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_id = Column(Integer, ForeignKey("us_stock_signal_virtual_configs.id"), index=True)
    account_id = Column(String, index=True)
    timestamp = Column(DateTime, default=datetime.now, index=True)
    date = Column(Date, index=True)
    level = Column(String(16), default="INFO")
    action = Column(String(32), nullable=False)
    message = Column(String(1000))
    payload = Column(JSON)

class AStockInnovationMomentumConfig(Base):
    """A股创新100风险调整混合动量虚拟盘配置"""
    __tablename__ = "a_stock_innovation_momentum_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True)
    name = Column(String(100), nullable=False, default="A股创新100风险调整混合动量虚拟盘")
    enabled = Column(Boolean, default=True, nullable=False)
    initial_capital = Column(Float, nullable=False, default=1_000_000.0)
    start_date = Column(Date, nullable=False)
    min_listing_days = Column(Integer, nullable=False, default=365)
    momentum_weights = Column(JSON, nullable=False, default=lambda: {"20": 0.0, "60": 0.20, "120": 0.80})
    fundamental_weights = Column(JSON, nullable=False, default=lambda: {"circ_mv": 0.34, "revenue_growth_3y": 0.33, "rd_exp_ratio": 0.33})
    fundamental_blend = Column(Float, nullable=False, default=0.0)
    max_positions = Column(Integer, nullable=False, default=5)
    sell_rank_multiplier = Column(Float, nullable=False, default=2.0)
    index_weight_blend = Column(Float, nullable=False, default=0.8)
    rebalance_frequency = Column(String(16), nullable=False, default="weekly")
    commission_pct = Column(Float, nullable=False, default=0.03)
    slippage_pct = Column(Float, nullable=False, default=0.02)
    lot_size = Column(Integer, nullable=False, default=100)
    auto_sync_enabled = Column(Boolean, default=True, nullable=False)
    auto_sync_time = Column(String(5), default="15:30", nullable=False)
    last_auto_sync_at = Column(DateTime)
    last_sync_at = Column(DateTime)
    last_sync_status = Column(String(16))
    last_sync_message = Column(String(500))
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class AStockInnovationMomentumEvent(Base):
    """A股创新100风险调整混合动量排名事件"""
    __tablename__ = "a_stock_innovation_momentum_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_id = Column(Integer, ForeignKey("a_stock_innovation_momentum_configs.id"), index=True)
    account_id = Column(String, index=True)
    symbol = Column(String(32), nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)
    direction = Column(String(8), nullable=False)
    signal_price = Column(Float)
    turnover = Column(Float)
    annualized_volatility_pct = Column(Float)
    threshold_pct = Column(Float)
    payload = Column(JSON)
    price_source = Column(String(32), default="daily_close")
    created_at = Column(DateTime, default=datetime.now)
    __table_args__ = (
        UniqueConstraint("config_id", "symbol", "date", "direction", name="uniq_a_stock_innovation_momentum_event"),
    )

class AStockInnovationMomentumEquity(Base):
    """A股创新100风险调整混合动量虚拟盘每日净值"""
    __tablename__ = "a_stock_innovation_momentum_equity"

    config_id = Column(Integer, ForeignKey("a_stock_innovation_momentum_configs.id"), primary_key=True)
    date = Column(Date, primary_key=True)
    account_id = Column(String, index=True)
    value = Column(Float, nullable=False)
    cash = Column(Float)
    position_value = Column(Float)
    drawdown = Column(Float)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class AStockInnovationMomentumTrade(Base):
    """A股创新100风险调整混合动量虚拟盘模拟成交"""
    __tablename__ = "a_stock_innovation_momentum_trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_id = Column(Integer, ForeignKey("a_stock_innovation_momentum_configs.id"), index=True)
    account_id = Column(String, index=True)
    date = Column(Date, index=True)
    signal_date = Column(Date)
    action = Column(String(8), nullable=False)
    symbol = Column(String(32), index=True)
    name = Column(String(64))
    price = Column(Float)
    quantity = Column(Integer)
    amount = Column(Float)
    commission = Column(Float)
    profit = Column(Float)
    profit_pct = Column(Float)
    reason = Column(String(64))
    reason_detail = Column(String(1000))
    cash_after = Column(Float)
    portfolio_value_after = Column(Float)
    symbol_market_value_after = Column(Float)
    symbol_weight_pct_after = Column(Float)
    price_source = Column(String(32))
    created_at = Column(DateTime, default=datetime.now)

class AStockInnovationMomentumHolding(Base):
    """A股创新100风险调整混合动量虚拟盘最新持仓快照"""
    __tablename__ = "a_stock_innovation_momentum_holdings"

    config_id = Column(Integer, ForeignKey("a_stock_innovation_momentum_configs.id"), primary_key=True)
    symbol = Column(String(32), primary_key=True)
    account_id = Column(String, index=True)
    name = Column(String(64))
    shares = Column(Integer, nullable=False, default=0)
    price = Column(Float)
    avg_cost = Column(Float)
    entry_date = Column(Date)
    market_value = Column(Float)
    actual_weight_pct = Column(Float)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class AStockInnovationMomentumLog(Base):
    """A股创新100风险调整混合动量虚拟盘运行日志"""
    __tablename__ = "a_stock_innovation_momentum_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_id = Column(Integer, ForeignKey("a_stock_innovation_momentum_configs.id"), index=True)
    account_id = Column(String, index=True)
    timestamp = Column(DateTime, default=datetime.now, index=True)
    date = Column(Date, index=True)
    level = Column(String(16), default="INFO")
    action = Column(String(32), nullable=False)
    message = Column(String(1000))
    payload = Column(JSON)

# 创建所有表
Base.metadata.create_all(engine)

def drop_deprecated_tables():
    """删除已经迁移出 SQLite 的旧缓存表。"""
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS stock_klines"))
        conn.execute(text("DROP TABLE IF EXISTS etf_emotions"))

drop_deprecated_tables()

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
        "CREATE INDEX IF NOT EXISTS idx_w20_momentum_live_configs_account ON w20_momentum_live_configs(account_id)",
        "CREATE INDEX IF NOT EXISTS idx_w20_momentum_live_trades_config_date ON w20_momentum_live_trades(config_id, date)",
        "CREATE INDEX IF NOT EXISTS idx_w20_momentum_live_logs_config_time ON w20_momentum_live_logs(config_id, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_us_stock_signal_configs_account ON us_stock_signal_virtual_configs(account_id)",
        "CREATE INDEX IF NOT EXISTS idx_us_stock_signal_events_config_date ON us_stock_signal_virtual_events(config_id, date)",
        "CREATE INDEX IF NOT EXISTS idx_us_stock_signal_trades_config_date ON us_stock_signal_virtual_trades(config_id, date)",
        "CREATE INDEX IF NOT EXISTS idx_us_stock_signal_logs_config_time ON us_stock_signal_virtual_logs(config_id, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_innovation_momentum_configs_account ON a_stock_innovation_momentum_configs(account_id)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_innovation_momentum_events_config_date ON a_stock_innovation_momentum_events(config_id, date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_innovation_momentum_trades_config_date ON a_stock_innovation_momentum_trades(config_id, date)",
        "CREATE INDEX IF NOT EXISTS idx_a_stock_innovation_momentum_logs_config_time ON a_stock_innovation_momentum_logs(config_id, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_etf_put_call_ratios_date_symbol ON etf_put_call_ratios(date, symbol)",
        "CREATE INDEX IF NOT EXISTS idx_etf_option_expirations_snapshot_symbol ON etf_option_expirations(snapshot_date, symbol)",
        "CREATE INDEX IF NOT EXISTS idx_etf_option_expirations_expiration ON etf_option_expirations(expiration_date)",
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
        "w20_momentum_live_trades": {
            "price_source": "ALTER TABLE w20_momentum_live_trades ADD COLUMN price_source VARCHAR(32)",
            "quote_timestamp": "ALTER TABLE w20_momentum_live_trades ADD COLUMN quote_timestamp DATETIME",
        },
        "us_stock_signal_virtual_configs": {
            "min_listing_days": "ALTER TABLE us_stock_signal_virtual_configs ADD COLUMN min_listing_days INTEGER NOT NULL DEFAULT 365",
            "momentum_weights": "ALTER TABLE us_stock_signal_virtual_configs ADD COLUMN momentum_weights JSON NOT NULL DEFAULT '{\"20\":0.05,\"60\":0.20,\"120\":0.75}'",
            "sell_rank_multiplier": "ALTER TABLE us_stock_signal_virtual_configs ADD COLUMN sell_rank_multiplier FLOAT NOT NULL DEFAULT 2.0",
            "index_weight_blend": "ALTER TABLE us_stock_signal_virtual_configs ADD COLUMN index_weight_blend FLOAT NOT NULL DEFAULT 0.4",
            "rebalance_frequency": "ALTER TABLE us_stock_signal_virtual_configs ADD COLUMN rebalance_frequency VARCHAR(16) NOT NULL DEFAULT 'weekly'",
        },
        "a_stock_innovation_momentum_configs": {
            "fundamental_weights": "ALTER TABLE a_stock_innovation_momentum_configs ADD COLUMN fundamental_weights JSON NOT NULL DEFAULT '{\"circ_mv\":0.34,\"revenue_growth_3y\":0.33,\"rd_exp_ratio\":0.33}'",
            "fundamental_blend": "ALTER TABLE a_stock_innovation_momentum_configs ADD COLUMN fundamental_blend FLOAT NOT NULL DEFAULT 0.0",
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

def ensure_us_stock_signal_virtual_recommended_defaults():
    """把旧的 20 日 Top10 默认配置迁移到当前推荐的混合动量参数。"""
    with engine.begin() as conn:
        existing = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(us_stock_signal_virtual_configs)")).fetchall()
        }
        required = {"name", "momentum_weights", "max_positions", "sell_rank_multiplier", "index_weight_blend", "updated_at"}
        if not required.issubset(existing):
            return
        conn.exec_driver_sql("""
            UPDATE us_stock_signal_virtual_configs
            SET
                name = CASE
                    WHEN name LIKE '%20日动量Top10%' THEN '美股风险调整混合动量虚拟盘'
                    ELSE name
                END,
                momentum_weights = '{"20":0.05,"60":0.20,"120":0.75}',
                max_positions = 7,
                sell_rank_multiplier = 2.0,
                index_weight_blend = 0.4,
                updated_at = CURRENT_TIMESTAMP
            WHERE max_positions = 10
              AND (name LIKE '%20日动量Top10%' OR name LIKE '%风险调整20日动量Top10%')
              AND (
                  momentum_weights = '{"20":1.0,"60":0.0,"120":0.0}'
                  OR momentum_weights = '{"20": 1.0, "60": 0.0, "120": 0.0}'
                  OR momentum_weights = '{"20": 1, "60": 0, "120": 0}'
              )
        """)

ensure_us_stock_signal_virtual_recommended_defaults()

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
                    trading_account_id, buy_threshold, greed_threshold, volume_ratio_threshold,
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

        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_soxl_fear_strategy_configs_account_id "
            "ON soxl_fear_strategy_configs(account_id)"
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

def get_db():
    """FastAPI dependency for database session"""
    db = Session()
    try:
        yield db
    finally:
        Session.remove()

@contextmanager
def get_db_ctx():
    """Context manager for database session, for use in 'with' statements"""
    db = Session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        Session.remove()

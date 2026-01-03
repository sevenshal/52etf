from sqlalchemy import create_engine, Column, String, Float, Boolean, DateTime, Date, Integer, ForeignKey, Table, PrimaryKeyConstraint, UniqueConstraint, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, scoped_session, relationship
from contextlib import contextmanager
import os
from .utils import get_data_file
from datetime import datetime

# 创建基础目录
DB_PATH = '/var/lib/quant_robot/evc_stocks.db'
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# 创建基础引擎和Base类
engine = create_engine(f'sqlite:///{DB_PATH}')
Base = declarative_base()

# 创建EVC会话
Session = scoped_session(sessionmaker(bind=engine))

# 潜在市场信号
class MarketSignal(Base):
    __tablename__ = 'market_signal'

    id = Column(Integer, primary_key=True, autoincrement=True)
    ver = Column(String(8), default='v1')
    symbol = Column(String(16), nullable=False)
    close_price = Column(Float, nullable=False)
    direction = Column(String(8), nullable=False)  # 'BUY'或'SELL'
    date = Column(Date, nullable=False)
    below_200ma_ratio = Column(Float, nullable=True)
    vol_5_std = Column(Float, nullable=True)
    today_vol_std = Column(Float, nullable=True)
    low_50 = Column(Float, nullable=True)
    close_vs_low_50 = Column(Float, nullable=True)
    v2_price_change_ratio = Column(Float, nullable=True)
    v2_stabilization_period = Column(Integer, nullable=True)
    __table_args__ = (
        UniqueConstraint('symbol', 'date', name='uniq_symbol_date'),
    )

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

class StockKline(Base):
    """股票K线数据表"""
    __tablename__ = 'stock_klines'
    
    symbol = Column(String(32), primary_key=True)
    date = Column(Date, primary_key=True)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Integer, nullable=False)
    turnover = Column(Float, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

class FetchLog(Base):
    __tablename__ = 'fetch_logs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, nullable=False)
    total_tags_fetched = Column(Integer, nullable=False)
    total_stocks_fetched = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

class ETFEmotion(Base):
    """ETF情绪指数记录"""
    __tablename__ = 'etf_emotions'
    
    symbol = Column(String(32), primary_key=True)
    date = Column(Date, primary_key=True)
    
    # 总体情绪指标
    score = Column(Float)
    
    # 各个子指标的分数
    momentum_score = Column(Float)
    strength_score = Column(Float)
    breadth_score = Column(Float)
    volatility_score = Column(Float)
    rsi_score = Column(Float)
    
    created_at = Column(DateTime)
    updated_at = Column(DateTime)

class EVCTradeLog(Base):
    __tablename__ = "evc_trade_logs"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String, index=True)
    quantity = Column(Integer)
    price = Column(Float)
    reason = Column(String)
    operation = Column(String)  # 'buy' or 'sell'
    timestamp = Column(DateTime, default=datetime.now, index=True)


class StockFavorite(Base):
    """用户股票收藏表"""
    __tablename__ = 'stock_favorites'

    symbol = Column(String(32), primary_key=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

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
    
    cli_id = Column(String, primary_key=True)
    current_index = Column(Integer, default=0)  # 当前处理的股票索引
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class StockCooldown(Base):
    """股票冷却表"""
    __tablename__ = "stock_cooldowns"
    
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

class PortfolioCopyConfig(Base):
    """投资组合跟单配置"""
    __tablename__ = "portfolio_copy_configs"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True)
    enabled = Column(Boolean, default=False)
    portfolio_id = Column(String, nullable=False)
    ib_account_id = Column(Integer, nullable=True) # 关联的 IB 账户 ID
    cron_rule = Column(String, default="0 8 * * *") # 默认每天 8 点
    ib_port = Column(Integer, nullable=True) # 以前是 nullable=False，现在允许为空 (如果使用了 ib_account_id)
    total_position_ratio = Column(Float, default=100.0) # 操作的总仓位比例 (%)
    total_amount = Column(Float) # 或者操作的总金额
    tracking_error_pct = Column(Float, default=5.0) # 跟踪误差 (%)
    api_headers = Column(JSON) # 包含 Cookie, User-Agent 等
    portfolio_name = Column(String(100))
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class PortfolioCopyLog(Base):
    """投资组合跟单日志"""
    __tablename__ = "portfolio_copy_logs"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True)
    timestamp = Column(DateTime, default=datetime.now, index=True)
    portfolio_id = Column(String)
    action = Column(String) # REBALANCE, FETCH_ERROR, etc.
    symbol = Column(String)
    quantity = Column(Float)
    price = Column(Float)
    status = Column(String) # SUCCESS, FAILED
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

# 用字典缓存每个账户的 session factory
_session_factories = {}

def get_db_session_factory(account_id: str):
    """获取或创建 session factory"""
    if account_id not in _session_factories:
        db_path = get_data_file(account_id, "trading.db")
        db_dir = os.path.dirname(db_path)
        if not os.path.exists(db_dir):
            os.makedirs(db_dir)
            
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},  # 允许多线程访问
            pool_size=5,  # 连接池大小
            max_overflow=10  # 最大额外连接数
        )
        Base.metadata.create_all(bind=engine)
        # 轻量迁移: 确保 szdt_trade_stocks 表存在 type 列
        try:
            with engine.connect() as conn:
                result = conn.execute("PRAGMA table_info(szdt_trade_stocks)")
                cols = [row[1] for row in result]  # 第二列为列名
                if 'type' not in cols:
                    conn.execute("ALTER TABLE szdt_trade_stocks ADD COLUMN type INTEGER NOT NULL DEFAULT 3")
                
                # 为 IBKRAccountConfig 表添加新列
                result = conn.execute("PRAGMA table_info(ib_account_configs)")
                ib_cols = [row[1] for row in result]
                if 'twofa_timeout_action' not in ib_cols:
                    conn.execute("ALTER TABLE ib_account_configs ADD COLUMN twofa_timeout_action TEXT DEFAULT 'restart'")
                if 'auto_restart_time' not in ib_cols:
                    conn.execute("ALTER TABLE ib_account_configs ADD COLUMN auto_restart_time TEXT DEFAULT '08:59 PM'")
                if 'relogin_after_twofa_timeout' not in ib_cols:
                    conn.execute("ALTER TABLE ib_account_configs ADD COLUMN relogin_after_twofa_timeout TEXT DEFAULT 'yes'")

                # 为 PortfolioCopyConfig 表添加新列
                result = conn.execute("PRAGMA table_info(portfolio_copy_configs)")
                pcc_cols = [row[1] for row in result]
                if 'ib_account_id' not in pcc_cols:
                    conn.execute("ALTER TABLE portfolio_copy_configs ADD COLUMN ib_account_id INTEGER DEFAULT NULL")
                
                # 确保新表存在
                Base.metadata.create_all(bind=engine)
        except Exception:
            # 静默失败，避免影响服务启动；后续操作若失败再暴露
            pass
        
        # 创建线程安全的 session factory
        session_factory = scoped_session(
            sessionmaker(
                bind=engine,
                autocommit=False,
                autoflush=False
            )
        )
        _session_factories[account_id] = session_factory
    
    return _session_factories[account_id]

@contextmanager
def get_db_session(account_id: str):
    """上下文管理器，自动处理 session 的创建和关闭"""
    session_factory = get_db_session_factory(account_id)
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

# 创建所有表
Base.metadata.create_all(engine)

def get_db():
    """FastAPI dependency for database session"""
    db = Session()
    try:
        yield db
    finally:
        db.close()

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
        db.close()

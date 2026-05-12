from contextlib import contextmanager
from datetime import datetime
import os

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    create_engine,
    event,
    text,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker


EXTERNAL_TRADING_DB_PATH = os.getenv(
    "EXTERNAL_TRADING_DB_PATH",
    "/var/lib/quant_robot/external_trading.db",
)
_external_trading_db_dir = os.path.dirname(EXTERNAL_TRADING_DB_PATH)
if _external_trading_db_dir:
    os.makedirs(_external_trading_db_dir, exist_ok=True)

external_trading_engine = create_engine(
    f"sqlite:///{EXTERNAL_TRADING_DB_PATH}",
    connect_args={"timeout": 30},
)


@event.listens_for(external_trading_engine, "connect")
def _set_external_trading_sqlite_pragmas(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


ExternalTradingBase = declarative_base()
ExternalTradingSessionLocal = sessionmaker(bind=external_trading_engine)
ExternalTradingSession = scoped_session(ExternalTradingSessionLocal)


class ExternalTradingAccount(ExternalTradingBase):
    """外部交易账号：通过一条反向 WebSocket 连接到券商/PTrade 客户端。"""
    __tablename__ = "external_trading_accounts"
    __table_args__ = (
        UniqueConstraint("account_id", "identifier", name="uq_external_trading_account_identifier"),
        UniqueConstraint("account_id", "name", name="uq_external_trading_account_name"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True, nullable=False)
    name = Column(String(100), nullable=False)
    identifier = Column(String(128), nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    executor_enabled = Column(Boolean, default=True, nullable=False)
    executor_price_level = Column(Integer, default=1, nullable=False)
    executor_lot_size = Column(Integer, default=100, nullable=False)
    executor_order_timeout_seconds = Column(Integer, default=120, nullable=False)
    executor_max_replace_count = Column(Integer, default=3, nullable=False)
    executor_clip_sell_to_available = Column(Boolean, default=True, nullable=False)
    executor_price_level_sequence = Column(JSON)
    last_connected_at = Column(DateTime)
    last_disconnected_at = Column(DateTime)
    last_seen_at = Column(DateTime)
    last_disconnect_reason = Column(String(500))
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class ExternalTradingSubAccount(ExternalTradingBase):
    """外部交易账号下的虚拟子账户，用于隔离多个实盘策略的账本。"""
    __tablename__ = "external_trading_sub_accounts"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "external_trading_account_id",
            "strategy_type",
            "strategy_config_id",
            name="uq_external_sub_account_strategy",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True, nullable=False)
    external_trading_account_id = Column(Integer, index=True, nullable=False)
    name = Column(String(100), nullable=False)
    strategy_type = Column(String(64), index=True)
    strategy_config_id = Column(Integer, index=True)
    cash_allocated = Column(Float, default=0.0, nullable=False)
    cash_available = Column(Float, default=0.0, nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    executor_price_level = Column(Integer)
    executor_lot_size = Column(Integer)
    executor_order_timeout_seconds = Column(Integer)
    executor_max_replace_count = Column(Integer)
    executor_clip_sell_to_available = Column(Boolean)
    executor_price_level_sequence = Column(JSON)
    remark = Column(String(1000))
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class ExternalTradingLedgerPosition(ExternalTradingBase):
    """虚拟子账户的策略归属持仓账本。"""
    __tablename__ = "external_trading_ledger_positions"
    __table_args__ = (
        UniqueConstraint("sub_account_id", "symbol", name="uq_external_ledger_position_symbol"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True, nullable=False)
    external_trading_account_id = Column(Integer, index=True, nullable=False)
    sub_account_id = Column(Integer, index=True, nullable=False)
    symbol = Column(String(32), index=True, nullable=False)
    quantity = Column(Integer, default=0, nullable=False)
    available_quantity = Column(Integer, default=0, nullable=False)
    avg_cost = Column(Float, default=0.0, nullable=False)
    market_price = Column(Float)
    market_value = Column(Float)
    realized_pnl = Column(Float, default=0.0, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class ExternalTradingTargetPosition(ExternalTradingBase):
    """策略同步到执行器的目标仓位快照。"""
    __tablename__ = "external_trading_target_positions"
    __table_args__ = (
        UniqueConstraint("sub_account_id", "symbol", name="uq_external_target_position_symbol"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True, nullable=False)
    external_trading_account_id = Column(Integer, index=True, nullable=False)
    sub_account_id = Column(Integer, index=True, nullable=False)
    strategy_type = Column(String(64), index=True)
    strategy_config_id = Column(Integer, index=True)
    symbol = Column(String(32), index=True, nullable=False)
    target_quantity = Column(Integer, default=0, nullable=False)
    target_weight_pct = Column(Float)
    target_value = Column(Float)
    signal_id = Column(String(128), index=True)
    signal_version = Column(String(64))
    source_execution_id = Column(Integer, index=True)
    valid_until = Column(DateTime)
    status = Column(String(16), default="ACTIVE", index=True, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    created_at = Column(DateTime, default=datetime.now)


class ExternalTradingOrder(ExternalTradingBase):
    """真实券商委托的本地生命周期记录。"""
    __tablename__ = "external_trading_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True, nullable=False)
    external_trading_account_id = Column(Integer, index=True, nullable=False)
    sub_account_id = Column(Integer, index=True)
    strategy_type = Column(String(64), index=True)
    strategy_config_id = Column(Integer, index=True)
    execution_id = Column(Integer, index=True)
    parent_order_id = Column(Integer, index=True)
    allocation_role = Column(String(16), default="DIRECT", index=True, nullable=False)
    client_order_id = Column(String(64), unique=True, index=True, nullable=False)
    broker_order_id = Column(String(128), index=True)
    entrust_no = Column(String(128), index=True)
    symbol = Column(String(32), index=True, nullable=False)
    side = Column(String(8), nullable=False)
    order_type = Column(String(10), default="LIMIT", nullable=False)
    price_level = Column(Integer)
    signal_version = Column(String(64), index=True)
    replace_count = Column(Integer, default=0, nullable=False)
    replaced_by_order_id = Column(Integer, index=True)
    deadline_at = Column(DateTime)
    cancel_reason = Column(String(500))
    executor_trigger = Column(String(64))
    submitted_price = Column(Float)
    quantity = Column(Integer, nullable=False)
    filled_quantity = Column(Integer, default=0, nullable=False)
    remaining_quantity = Column(Integer, default=0, nullable=False)
    avg_fill_price = Column(Float)
    status = Column(String(40), default="CREATED", index=True, nullable=False)
    ptrade_status = Column(String(16), index=True)
    message = Column(String(1000))
    submitted_at = Column(DateTime)
    last_event_at = Column(DateTime)
    raw_request = Column(JSON)
    raw_submit_result = Column(JSON)
    raw_order_event = Column(JSON)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class ExternalTradingOrderFill(ExternalTradingBase):
    """成交回报流水。账本只根据这张表的去重成交增量更新。"""
    __tablename__ = "external_trading_order_fills"
    __table_args__ = (
        UniqueConstraint("fill_key", name="uq_external_order_fill_key"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True, nullable=False)
    external_trading_account_id = Column(Integer, index=True, nullable=False)
    sub_account_id = Column(Integer, index=True)
    order_id = Column(Integer, index=True)
    client_order_id = Column(String(64), index=True)
    broker_order_id = Column(String(128), index=True)
    fill_key = Column(String(256), nullable=False)
    symbol = Column(String(32), index=True, nullable=False)
    side = Column(String(8), nullable=False)
    quantity = Column(Integer, nullable=False)
    price = Column(Float, nullable=False)
    amount = Column(Float, nullable=False)
    traded_at = Column(DateTime)
    raw_event = Column(JSON)
    created_at = Column(DateTime, default=datetime.now)


def get_external_trading_db():
    """FastAPI dependency for the external trading database session."""
    db = ExternalTradingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def get_external_trading_db_ctx():
    """Context manager for the external trading database session."""
    db = ExternalTradingSessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def ensure_external_trading_indexes():
    index_sqls = [
        "CREATE INDEX IF NOT EXISTS idx_external_trading_accounts_account ON external_trading_accounts(account_id)",
        "CREATE INDEX IF NOT EXISTS idx_external_trading_accounts_identifier ON external_trading_accounts(account_id, identifier)",
        "CREATE INDEX IF NOT EXISTS idx_external_sub_accounts_strategy ON external_trading_sub_accounts(strategy_type, strategy_config_id)",
        "CREATE INDEX IF NOT EXISTS idx_external_ledger_positions_sub_symbol ON external_trading_ledger_positions(sub_account_id, symbol)",
        "CREATE INDEX IF NOT EXISTS idx_external_target_positions_sub_symbol ON external_trading_target_positions(sub_account_id, symbol)",
        "CREATE INDEX IF NOT EXISTS idx_external_orders_lifecycle ON external_trading_orders(status, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_external_orders_broker ON external_trading_orders(external_trading_account_id, broker_order_id)",
        "CREATE INDEX IF NOT EXISTS idx_external_order_fills_order ON external_trading_order_fills(order_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_external_orders_parent ON external_trading_orders(parent_order_id, allocation_role)",
        "CREATE INDEX IF NOT EXISTS idx_external_orders_deadline ON external_trading_orders(external_trading_account_id, deadline_at, status)",
        "CREATE INDEX IF NOT EXISTS idx_external_orders_signal ON external_trading_orders(sub_account_id, symbol, signal_version)",
        "CREATE INDEX IF NOT EXISTS idx_external_target_source_execution ON external_trading_target_positions(source_execution_id)",
    ]
    with external_trading_engine.begin() as conn:
        for sql in index_sqls:
            conn.execute(text(sql))


ExternalTradingBase.metadata.create_all(external_trading_engine)
ensure_external_trading_indexes()

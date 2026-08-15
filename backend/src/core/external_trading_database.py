from contextlib import contextmanager
from datetime import datetime
import os

from sqlalchemy import (
    Boolean,
    Column,
    Date,
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
ExternalTradingSessionLocal = sessionmaker(bind=external_trading_engine, autoflush=False)
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
    market_type = Column(String(32), default="A_STOCK", nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    executor_enabled = Column(Boolean, default=True, nullable=False)
    executor_price_level = Column(Integer, default=1, nullable=False)
    executor_lot_size = Column(Integer, default=100, nullable=False)
    executor_order_timeout_seconds = Column(Integer, default=120, nullable=False)
    executor_max_replace_count = Column(Integer, default=3, nullable=False)
    executor_max_slippage_pct = Column(Float, default=0.5, nullable=False)
    executor_min_order_amount = Column(Float, default=0.0, nullable=False)
    executor_max_batch_amount = Column(Float)
    executor_batch_interval_seconds = Column(Integer)
    executor_clip_sell_to_available = Column(Boolean, default=True, nullable=False)
    executor_price_level_sequence = Column(JSON)
    executor_order_timeout_seconds_sequence = Column(JSON)
    commission_rate_pct = Column(Float, default=0.025, nullable=False)
    min_commission = Column(Float, default=5.0, nullable=False)
    stamp_tax_rate_pct = Column(Float, default=0.05, nullable=False)
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
    executor_max_slippage_pct = Column(Float)
    executor_min_order_amount = Column(Float)
    executor_max_batch_amount = Column(Float)
    executor_batch_interval_seconds = Column(Integer)
    executor_clip_sell_to_available = Column(Boolean)
    executor_price_level_sequence = Column(JSON)
    executor_order_timeout_seconds_sequence = Column(JSON)
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


class ExternalTradingValuationSimPositionState(ExternalTradingBase):
    """估值成长模拟盘在外部交易子账户持仓上的策略状态。"""
    __tablename__ = "valuation_sim_position_states"
    __table_args__ = (
        UniqueConstraint("config_id", "sub_account_id", "symbol", name="uq_valuation_sim_state_config_sub_symbol"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True, nullable=False)
    external_trading_account_id = Column(Integer, index=True, nullable=False)
    sub_account_id = Column(Integer, index=True, nullable=False)
    config_id = Column(Integer, index=True, nullable=False)
    symbol = Column(String(32), index=True, nullable=False)
    highest_price = Column(Float)
    highest_price_date = Column(Date)
    days_without_high = Column(Integer, default=0, nullable=False)
    opened_trade_date = Column(Date)
    last_trade_date = Column(Date)
    last_price = Column(Float)
    last_market_value = Column(Float)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class ExternalTradingBrokerPositionSnapshot(ExternalTradingBase):
    """外部券商真实持仓快照。"""
    __tablename__ = "external_trading_broker_position_snapshots"
    __table_args__ = ()

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True, nullable=False)
    external_trading_account_id = Column(Integer, index=True, nullable=False)
    snapshot_date = Column(Date, index=True, nullable=False)
    snapshot_at = Column(DateTime, index=True, nullable=False)
    snapshot_source = Column(String(32), index=True, nullable=False)
    snapshot_kind = Column(String(32), index=True)
    market_window_open = Column(Boolean, default=False, nullable=False)
    position_count = Column(Integer, default=0, nullable=False)
    total_market_value = Column(Float, default=0.0, nullable=False)
    total_available_market_value = Column(Float, default=0.0, nullable=False)
    positions = Column(JSON)
    raw_payload = Column(JSON)
    status = Column(String(16), default="SUCCESS", index=True, nullable=False)
    message = Column(String(1000))
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class ExternalTradingSubAccountNetAssetHistory(ExternalTradingBase):
    """虚拟子账户每日净资产快照。"""
    __tablename__ = "external_trading_sub_account_net_asset_history"
    __table_args__ = (
        UniqueConstraint("sub_account_id", "trading_date", name="uq_external_sub_account_nav_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True, nullable=False)
    external_trading_account_id = Column(Integer, index=True, nullable=False)
    sub_account_id = Column(Integer, index=True, nullable=False)
    strategy_type = Column(String(64), index=True)
    strategy_config_id = Column(Integer, index=True)
    trading_date = Column(Date, index=True, nullable=False)
    cash_allocated = Column(Float, default=0.0, nullable=False)
    cash_available = Column(Float, default=0.0, nullable=False)
    position_market_value = Column(Float, default=0.0, nullable=False)
    net_asset = Column(Float, default=0.0, nullable=False)
    position_count = Column(Integer, default=0, nullable=False)
    positions = Column(JSON)
    price_details = Column(JSON)
    source = Column(String(32), default="scheduled_close", nullable=False)
    status = Column(String(32), default="SUCCESS", index=True, nullable=False)
    message = Column(String(1000))
    valued_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.now)
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
    reference_price = Column(Float)
    reference_price_source = Column(String(64))
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
    estimated_commission = Column(Float, default=0.0, nullable=False)
    estimated_stamp_tax = Column(Float, default=0.0, nullable=False)
    estimated_fee_total = Column(Float, default=0.0, nullable=False)
    actual_commission = Column(Float)
    actual_stamp_tax = Column(Float)
    actual_fee_total = Column(Float)
    fee_reconciled_at = Column(DateTime)
    fee_source = Column(String(32))
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
    estimated_commission = Column(Float, default=0.0, nullable=False)
    estimated_stamp_tax = Column(Float, default=0.0, nullable=False)
    estimated_fee_total = Column(Float, default=0.0, nullable=False)
    actual_commission = Column(Float)
    actual_stamp_tax = Column(Float)
    actual_fee_total = Column(Float)
    fee_reconciled_at = Column(DateTime)
    fee_source = Column(String(32))
    traded_at = Column(DateTime)
    raw_event = Column(JSON)
    created_at = Column(DateTime, default=datetime.now)


class ExternalTradingEventLog(ExternalTradingBase):
    """外部交易入站事件流水，保留 order/trade 回报原文并支持乱序重放。"""
    __tablename__ = "external_trading_event_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True)
    account_name = Column(String(100))
    external_trading_account_id = Column(Integer, index=True, nullable=False)
    event_type = Column(String(32), index=True, nullable=False)
    source = Column(String(32), index=True)
    client_order_id = Column(String(64), index=True)
    broker_order_id = Column(String(128), index=True)
    entrust_no = Column(String(128), index=True)
    symbol = Column(String(32), index=True)
    side = Column(String(8))
    ptrade_status = Column(String(16), index=True)
    event_time = Column(DateTime, index=True)
    raw_payload = Column(JSON)
    matched_order_id = Column(Integer, index=True)
    matched_sub_account_id = Column(Integer, index=True)
    process_status = Column(String(32), default="RECEIVED", index=True, nullable=False)
    process_message = Column(String(1000))
    processed_at = Column(DateTime)
    replay_count = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class ExternalTradingDeliverRecord(ExternalTradingBase):
    """PTrade 交割单原始记录与本地订单费用对账结果。"""
    __tablename__ = "external_trading_deliver_records"
    __table_args__ = (
        UniqueConstraint(
            "external_trading_account_id",
            "trade_date",
            "deliver_key",
            name="uq_external_deliver_record_key",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String, index=True, nullable=False)
    external_trading_account_id = Column(Integer, index=True, nullable=False)
    trade_date = Column(Date, index=True, nullable=False)
    deliver_key = Column(String(256), nullable=False)
    matched_order_id = Column(Integer, index=True)
    broker_order_id = Column(String(128), index=True)
    entrust_no = Column(String(128), index=True)
    symbol = Column(String(32), index=True)
    side = Column(String(8))
    quantity = Column(Integer, default=0, nullable=False)
    price = Column(Float, default=0.0, nullable=False)
    amount = Column(Float, default=0.0, nullable=False)
    commission = Column(Float, default=0.0, nullable=False)
    stamp_tax = Column(Float, default=0.0, nullable=False)
    transfer_fee = Column(Float, default=0.0, nullable=False)
    other_fee = Column(Float, default=0.0, nullable=False)
    total_fee = Column(Float, default=0.0, nullable=False)
    status = Column(String(32), default="UNMATCHED", index=True, nullable=False)
    message = Column(String(1000))
    raw_record = Column(JSON)
    reconciled_at = Column(DateTime)
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
        "CREATE INDEX IF NOT EXISTS idx_external_broker_position_snapshots_account_time ON external_trading_broker_position_snapshots(external_trading_account_id, snapshot_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_external_broker_position_snapshots_account_date ON external_trading_broker_position_snapshots(external_trading_account_id, snapshot_date, snapshot_source)",
        "CREATE INDEX IF NOT EXISTS idx_external_target_positions_sub_symbol ON external_trading_target_positions(sub_account_id, symbol)",
        "CREATE INDEX IF NOT EXISTS idx_external_sub_account_nav_account_date ON external_trading_sub_account_net_asset_history(account_id, external_trading_account_id, trading_date)",
        "CREATE INDEX IF NOT EXISTS idx_external_sub_account_nav_sub_date ON external_trading_sub_account_net_asset_history(sub_account_id, trading_date)",
        "CREATE INDEX IF NOT EXISTS idx_valuation_sim_position_states_config_symbol ON valuation_sim_position_states(config_id, sub_account_id, symbol)",
        "CREATE INDEX IF NOT EXISTS idx_external_orders_lifecycle ON external_trading_orders(status, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_external_orders_broker ON external_trading_orders(external_trading_account_id, broker_order_id)",
        "CREATE INDEX IF NOT EXISTS idx_external_order_fills_order ON external_trading_order_fills(order_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_external_event_logs_status ON external_trading_event_logs(external_trading_account_id, process_status, event_type, id)",
        "CREATE INDEX IF NOT EXISTS idx_external_event_logs_order ON external_trading_event_logs(external_trading_account_id, broker_order_id, entrust_no, client_order_id)",
        "CREATE INDEX IF NOT EXISTS idx_external_event_logs_sub_account ON external_trading_event_logs(external_trading_account_id, matched_sub_account_id, id)",
        "CREATE INDEX IF NOT EXISTS idx_external_orders_parent ON external_trading_orders(parent_order_id, allocation_role)",
        "CREATE INDEX IF NOT EXISTS idx_external_orders_deadline ON external_trading_orders(external_trading_account_id, deadline_at, status)",
        "CREATE INDEX IF NOT EXISTS idx_external_orders_signal ON external_trading_orders(sub_account_id, symbol, signal_version)",
        "CREATE INDEX IF NOT EXISTS idx_external_target_source_execution ON external_trading_target_positions(source_execution_id)",
        "CREATE INDEX IF NOT EXISTS idx_external_deliver_records_match ON external_trading_deliver_records(external_trading_account_id, trade_date, status)",
        "CREATE INDEX IF NOT EXISTS idx_external_deliver_records_order ON external_trading_deliver_records(matched_order_id)",
    ]
    with external_trading_engine.begin() as conn:
        for sql in index_sqls:
            conn.execute(text(sql))


def ensure_external_trading_columns():
    table_columns = {
        "external_trading_accounts": {
            "market_type": "ALTER TABLE external_trading_accounts ADD COLUMN market_type VARCHAR(32) NOT NULL DEFAULT 'A_STOCK'",
            "commission_rate_pct": "ALTER TABLE external_trading_accounts ADD COLUMN commission_rate_pct FLOAT NOT NULL DEFAULT 0.025",
            "min_commission": "ALTER TABLE external_trading_accounts ADD COLUMN min_commission FLOAT NOT NULL DEFAULT 5.0",
            "stamp_tax_rate_pct": "ALTER TABLE external_trading_accounts ADD COLUMN stamp_tax_rate_pct FLOAT NOT NULL DEFAULT 0.05",
            "executor_max_slippage_pct": "ALTER TABLE external_trading_accounts ADD COLUMN executor_max_slippage_pct FLOAT NOT NULL DEFAULT 0.5",
            "executor_min_order_amount": "ALTER TABLE external_trading_accounts ADD COLUMN executor_min_order_amount FLOAT NOT NULL DEFAULT 0.0",
            "executor_max_batch_amount": "ALTER TABLE external_trading_accounts ADD COLUMN executor_max_batch_amount FLOAT",
            "executor_batch_interval_seconds": "ALTER TABLE external_trading_accounts ADD COLUMN executor_batch_interval_seconds INTEGER",
            "executor_order_timeout_seconds_sequence": "ALTER TABLE external_trading_accounts ADD COLUMN executor_order_timeout_seconds_sequence JSON",
        },
        "external_trading_sub_accounts": {
            "executor_max_slippage_pct": "ALTER TABLE external_trading_sub_accounts ADD COLUMN executor_max_slippage_pct FLOAT",
            "executor_min_order_amount": "ALTER TABLE external_trading_sub_accounts ADD COLUMN executor_min_order_amount FLOAT",
            "executor_max_batch_amount": "ALTER TABLE external_trading_sub_accounts ADD COLUMN executor_max_batch_amount FLOAT",
            "executor_batch_interval_seconds": "ALTER TABLE external_trading_sub_accounts ADD COLUMN executor_batch_interval_seconds INTEGER",
            "executor_order_timeout_seconds_sequence": "ALTER TABLE external_trading_sub_accounts ADD COLUMN executor_order_timeout_seconds_sequence JSON",
        },
        "external_trading_orders": {
            "estimated_commission": "ALTER TABLE external_trading_orders ADD COLUMN estimated_commission FLOAT NOT NULL DEFAULT 0.0",
            "estimated_stamp_tax": "ALTER TABLE external_trading_orders ADD COLUMN estimated_stamp_tax FLOAT NOT NULL DEFAULT 0.0",
            "estimated_fee_total": "ALTER TABLE external_trading_orders ADD COLUMN estimated_fee_total FLOAT NOT NULL DEFAULT 0.0",
            "actual_commission": "ALTER TABLE external_trading_orders ADD COLUMN actual_commission FLOAT",
            "actual_stamp_tax": "ALTER TABLE external_trading_orders ADD COLUMN actual_stamp_tax FLOAT",
            "actual_fee_total": "ALTER TABLE external_trading_orders ADD COLUMN actual_fee_total FLOAT",
            "fee_reconciled_at": "ALTER TABLE external_trading_orders ADD COLUMN fee_reconciled_at DATETIME",
            "fee_source": "ALTER TABLE external_trading_orders ADD COLUMN fee_source VARCHAR(32)",
        },
        "external_trading_order_fills": {
            "estimated_commission": "ALTER TABLE external_trading_order_fills ADD COLUMN estimated_commission FLOAT NOT NULL DEFAULT 0.0",
            "estimated_stamp_tax": "ALTER TABLE external_trading_order_fills ADD COLUMN estimated_stamp_tax FLOAT NOT NULL DEFAULT 0.0",
            "estimated_fee_total": "ALTER TABLE external_trading_order_fills ADD COLUMN estimated_fee_total FLOAT NOT NULL DEFAULT 0.0",
            "actual_commission": "ALTER TABLE external_trading_order_fills ADD COLUMN actual_commission FLOAT",
            "actual_stamp_tax": "ALTER TABLE external_trading_order_fills ADD COLUMN actual_stamp_tax FLOAT",
            "actual_fee_total": "ALTER TABLE external_trading_order_fills ADD COLUMN actual_fee_total FLOAT",
            "fee_reconciled_at": "ALTER TABLE external_trading_order_fills ADD COLUMN fee_reconciled_at DATETIME",
            "fee_source": "ALTER TABLE external_trading_order_fills ADD COLUMN fee_source VARCHAR(32)",
        },
        "external_trading_target_positions": {
            "reference_price": "ALTER TABLE external_trading_target_positions ADD COLUMN reference_price FLOAT",
            "reference_price_source": "ALTER TABLE external_trading_target_positions ADD COLUMN reference_price_source VARCHAR(64)",
        },
        "external_trading_event_logs": {
            "matched_sub_account_id": "ALTER TABLE external_trading_event_logs ADD COLUMN matched_sub_account_id INTEGER",
        },
    }
    with external_trading_engine.begin() as conn:
        for table_name, columns in table_columns.items():
            existing = {
                row[1]
                for row in conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
            }
            for column_name, ddl in columns.items():
                if column_name not in existing:
                    conn.exec_driver_sql(ddl)
        conn.execute(text("""
            UPDATE external_trading_event_logs
            SET matched_sub_account_id = (
                SELECT external_trading_orders.sub_account_id
                FROM external_trading_orders
                WHERE external_trading_orders.id = external_trading_event_logs.matched_order_id
                  AND external_trading_orders.external_trading_account_id = external_trading_event_logs.external_trading_account_id
                LIMIT 1
            )
            WHERE matched_sub_account_id IS NULL
              AND matched_order_id IS NOT NULL
              AND EXISTS (
                SELECT 1
                FROM external_trading_orders
                WHERE external_trading_orders.id = external_trading_event_logs.matched_order_id
                  AND external_trading_orders.external_trading_account_id = external_trading_event_logs.external_trading_account_id
                  AND external_trading_orders.sub_account_id IS NOT NULL
              )
        """))


def drop_deprecated_external_trading_columns():
    """删除存量外部交易库里已经不再由模型定义的旧字段。"""
    deprecated_columns = {
        "external_trading_accounts": [
            "executor_initial_price_source",
        ],
        "external_trading_sub_accounts": [
            "executor_initial_price_source",
        ],
        "external_trading_target_positions": [
            "protection_limit_price",
            "protection_limit_source",
        ],
        "external_trading_ledger_positions": [
            "strategy_state",
        ],
    }
    with external_trading_engine.begin() as conn:
        for table_name, columns in deprecated_columns.items():
            existing = {
                row[1]
                for row in conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
            }
            for column_name in columns:
                if column_name in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table_name} DROP COLUMN {column_name}")


ExternalTradingBase.metadata.create_all(external_trading_engine)
ensure_external_trading_columns()
drop_deprecated_external_trading_columns()
ensure_external_trading_indexes()

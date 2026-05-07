from src.core.database import Session, engine, Base, SzdtTradeStock, TradingState, StockCooldown, StockFavorite
from src.core.utils import DATA_DIR
import os
import glob
from sqlalchemy import text, create_engine, Column, Integer, String, Float, Boolean, DateTime, JSON
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base

# --- Old Models for Reading Local DB ---
OldBase = declarative_base()

class OldSzdtTradeStock(OldBase):
    __tablename__ = 'szdt_trade_stocks'
    id = Column(Integer, primary_key=True)
    code = Column(String)
    name = Column(String)
    type = Column(Integer)
    when_buy = Column(Integer)
    when_sell = Column(Integer)
    max_position = Column(Integer)
    buy_amount = Column(Float)
    sell_amount = Column(Float)
    buy_factor = Column(Float)
    sell_factor = Column(Float)
    lever = Column(Integer)
    emo_area = Column(String)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)

class OldTradingState(OldBase):
    __tablename__ = "trading_states"
    cli_id = Column(String, primary_key=True)
    current_index = Column(Integer)
    updated_at = Column(DateTime)

class OldStockCooldown(OldBase):
    __tablename__ = "stock_cooldowns"
    cli_id = Column(String, primary_key=True)
    stock_code = Column(String, primary_key=True)
    until = Column(DateTime)
    reason = Column(String)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)

class OldStockFavorite(OldBase):
    __tablename__ = 'stock_favorites'
    symbol = Column(String, primary_key=True)
    created_at = Column(DateTime)

def get_local_session(db_path):
    local_engine = create_engine(f"sqlite:///{db_path}")
    return sessionmaker(bind=local_engine)()

def migrate_all():
    print(f"Starting Comprehensive Migration from {DATA_DIR}...")
    
    if not os.path.exists(DATA_DIR):
        print(f"WARNING: DATA_DIR {DATA_DIR} does not exist.")
        return

    # 1. Reset Targeted Tables in Global DB
    # CAUTION: This deletes existing global data for these tables.
    tables_to_reset = [
        "szdt_trade_stocks", "trading_states", "stock_cooldowns", 
        "stock_favorites"
    ]
    
    with engine.connect() as conn:
        print("Dropping legacy tables in global DB...")
        for t in tables_to_reset:
            conn.execute(text(f"DROP TABLE IF EXISTS {t}"))
        conn.commit()

    # 2. Re-create Tables (New Schema)
    print("Re-creating tables...")
    Base.metadata.create_all(bind=engine)
    
    global_db = Session()
    
    db_files = glob.glob(os.path.join(DATA_DIR, "*", "trading.db"))
    stats = {k: 0 for k in tables_to_reset}
    
    for db_file in db_files:
        account_id = os.path.basename(os.path.dirname(db_file))
        print(f"Processing account: {account_id}")
        
        try:
            local_session = get_local_session(db_file)
            
            # Fix schema for SzdtTradeStock in local DB if needed
            try:
                local_engine = create_engine(f"sqlite:///{db_file}")
                with local_engine.connect() as conn:
                    # Check SzdtTradeStock type column
                    try:
                        result = conn.execute(text("PRAGMA table_info(szdt_trade_stocks)"))
                        cols = [row[1] for row in result]
                        if 'type' not in cols:
                            print(f"Adding 'type' column to {db_file}...")
                            conn.execute(text("ALTER TABLE szdt_trade_stocks ADD COLUMN type INTEGER NOT NULL DEFAULT 3"))
                    except Exception as e:
                        # Table might not exist
                        pass
            except Exception as e:
                print(f"Warning: Failed to inspect/fix schema for {db_file}: {e}")
            
            # --- SzdtTradeStock ---
            try:
                items = local_session.query(OldSzdtTradeStock).all()
                for i in items:
                    new_item = SzdtTradeStock(
                        account_id=account_id,
                        code=i.code,
                        name=i.name,
                        type=i.type,
                        when_buy=i.when_buy,
                        when_sell=i.when_sell,
                        max_position=i.max_position,
                        buy_amount=i.buy_amount,
                        sell_amount=i.sell_amount,
                        buy_factor=i.buy_factor,
                        sell_factor=i.sell_factor,
                        lever=i.lever,
                        emo_area=i.emo_area,
                        created_at=i.created_at,
                        updated_at=i.updated_at
                    )
                    global_db.add(new_item)
                    stats["szdt_trade_stocks"] += 1
            except Exception as e:
                # Table might not exist in local DB
                pass
                
            # --- TradingState ---
            try:
                items = local_session.query(OldTradingState).all()
                for i in items:
                    new_item = TradingState(
                        account_id=account_id,
                        cli_id=i.cli_id,
                        current_index=i.current_index,
                        updated_at=i.updated_at
                    )
                    global_db.add(new_item)
                    stats["trading_states"] += 1
            except Exception: pass
            
            # --- StockCooldown ---
            try:
                items = local_session.query(OldStockCooldown).all()
                for i in items:
                    new_item = StockCooldown(
                        account_id=account_id,
                        cli_id=i.cli_id,
                        stock_code=i.stock_code,
                        until=i.until,
                        reason=i.reason,
                        created_at=i.created_at,
                        updated_at=i.updated_at
                    )
                    global_db.add(new_item)
                    stats["stock_cooldowns"] += 1
            except Exception: pass

            # --- StockFavorite ---
            try:
                items = local_session.query(OldStockFavorite).all()
                for i in items:
                    # Check unique (account_id, symbol)
                    exists = global_db.query(StockFavorite).filter_by(account_id=account_id, symbol=i.symbol).first()
                    if not exists:
                        new_item = StockFavorite(
                            account_id=account_id,
                            symbol=i.symbol,
                            created_at=i.created_at
                        )
                        global_db.add(new_item)
                        stats["stock_favorites"] += 1
            except Exception: pass

            global_db.commit()
            local_session.close()
            
        except Exception as e:
            print(f"Error migrating {account_id}: {e}")
            global_db.rollback()

    global_db.close()
    print(f"Migration completed. Stats: {stats}")

if __name__ == "__main__":
    migrate_all()

from src.core.database import Session, SnowballCopyConfig, SnowballPortfolioSnapshot, SnowballCopyLog, engine, Base
from src.core.utils import DATA_DIR, get_data_file
import os
import glob
from sqlalchemy import text, create_engine, Column, Integer, String, Float, Boolean, DateTime, JSON
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime

# --- Old Models for Reading Local DB ---
OldBase = declarative_base()

class OldSnowballCopyConfig(OldBase):
    __tablename__ = "snowball_copy_configs"
    id = Column(Integer, primary_key=True)
    cli_id = Column(String)
    combination_id = Column(String)
    combination_name = Column(String)
    enabled = Column(Boolean)
    total_position_ratio = Column(Float)
    total_amount = Column(Float)
    tracking_error_pct = Column(Float)
    blacklisted_symbols = Column(JSON)
    updated_at = Column(DateTime)

class OldSnowballPortfolioSnapshot(OldBase):
    __tablename__ = "snowball_portfolio_snapshots"
    id = Column(Integer, primary_key=True)
    config_id = Column(Integer)
    holdings = Column(JSON)
    cash = Column(Float)
    market_value = Column(Float)
    last_synced_amount = Column(Float)
    updated_at = Column(DateTime)

class OldSnowballCopyLog(OldBase):
    __tablename__ = "snowball_copy_logs"
    id = Column(Integer, primary_key=True)
    cli_id = Column(String)
    timestamp = Column(DateTime)
    combination_id = Column(String)
    action = Column(String)
    symbol = Column(String)
    quantity = Column(Float)
    price = Column(Float)
    status = Column(String)
    message = Column(String)

def get_local_session(db_path):
    local_engine = create_engine(f"sqlite:///{db_path}")
    return sessionmaker(bind=local_engine)()

def reset_and_migrate_snowball_data():
    print(f"Starting Snowball data migration (RESET MODE w/ Old Models) from {DATA_DIR}...")
    
    if not os.path.exists(DATA_DIR):
        print(f"WARNING: DATA_DIR {DATA_DIR} does not exist.")
        return

    # 1. Reset Tables in Global DB
    with engine.connect() as conn:
        print("Dropping existing Snowball tables in global DB...")
        conn.execute(text("DROP TABLE IF EXISTS snowball_portfolio_snapshots"))
        conn.execute(text("DROP TABLE IF EXISTS snowball_copy_configs"))
        conn.execute(text("DROP TABLE IF EXISTS snowball_copy_logs"))
        conn.commit()

    # 2. Re-create Tables (New Schema with account_id)
    print("Re-creating tables...")
    Base.metadata.create_all(bind=engine)
    
    # 3. Ensure Index Cleanup
    with engine.connect() as conn:
        try:
            conn.execute(text("DROP INDEX IF EXISTS ix_snowball_copy_configs_cli_id"))
        except:
            pass
        conn.commit()

    global_db = Session()
    
    db_files = glob.glob(os.path.join(DATA_DIR, "*", "trading.db"))
    migrated_counts = {"configs": 0, "snapshots": 0, "logs": 0}
    
    for db_file in db_files:
        account_id = os.path.basename(os.path.dirname(db_file))
        print(f"Processing account: {account_id}")
        
        try:
            local_session = get_local_session(db_file)
            
            # Migrate Configs
            # Using Old Model to read
            try:
                configs = local_session.query(OldSnowballCopyConfig).all()
            except Exception as e:
                print(f"  Error reading configs: {e}")
                configs = []

            for c in configs:
                # Using New Model to write
                new_c = SnowballCopyConfig(
                    account_id=account_id,
                    cli_id=c.cli_id,
                    combination_id=c.combination_id,
                    combination_name=c.combination_name,
                    enabled=c.enabled,
                    total_position_ratio=c.total_position_ratio,
                    total_amount=c.total_amount,
                    tracking_error_pct=c.tracking_error_pct,
                    blacklisted_symbols=c.blacklisted_symbols,
                    updated_at=c.updated_at
                )
                global_db.add(new_c)
                global_db.flush() 
                new_c_id = new_c.id
                migrated_counts["configs"] += 1
                
                # Migrate Snapshot
                try:
                    snapshot = local_session.query(OldSnowballPortfolioSnapshot).filter_by(config_id=c.id).first()
                    if snapshot:
                        new_snap = SnowballPortfolioSnapshot(
                            account_id=account_id,
                            config_id=new_c_id,
                            holdings=snapshot.holdings,
                            cash=snapshot.cash,
                            market_value=snapshot.market_value,
                            last_synced_amount=snapshot.last_synced_amount,
                            updated_at=snapshot.updated_at
                        )
                        global_db.add(new_snap)
                        migrated_counts["snapshots"] += 1
                except Exception as e:
                    print(f"  Error reading snapshot: {e}")

            # Migrate Logs
            try:
                logs = local_session.query(OldSnowballCopyLog).all()
                for l in logs:
                    new_l = SnowballCopyLog(
                        account_id=account_id,
                        cli_id=l.cli_id,
                        timestamp=l.timestamp,
                        combination_id=l.combination_id,
                        action=l.action,
                        symbol=l.symbol,
                        quantity=l.quantity,
                        price=l.price,
                        status=l.status,
                        message=l.message
                    )
                    global_db.add(new_l)
                    migrated_counts["logs"] += 1
            except Exception as e:
                print(f"  Error reading logs: {e}")

            global_db.commit()
            local_session.close()
            
        except Exception as e:
            print(f"Error migrating {account_id}: {e}")
            global_db.rollback()

    global_db.close()
    print(f"Migration completed. Stats: {migrated_counts}")

if __name__ == "__main__":
    reset_and_migrate_snowball_data()

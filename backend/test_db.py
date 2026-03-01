from src.core.database import Session, SnowballAccountConfig

def test_insert():
    db = Session()
    try:
        conf = SnowballAccountConfig(account_id="test_account", xueqiu_cookie="test_cookie")
        db.add(conf)
        db.commit()
    except Exception as e:
        db.rollback()
    
if __name__ == "__main__":
    test_insert()

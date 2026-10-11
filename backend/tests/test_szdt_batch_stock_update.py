import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.app.api.szdt import BatchStockCandidate, BatchStockUpdateRequest, batch_update_stocks
from src.core.database import Base, SzdtTradeStock


def test_batch_update_updates_existing_and_adds_missing_as_disabled():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add(SzdtTradeStock(
            account_id="account-a", code="SH.510300", name="沪深300", type=3,
            when_buy=-60, when_sell=60, max_position=5, buy_amount=2000,
            sell_amount=2000, buy_factor=1, sell_factor=1, lever=1, emo_area="a",
            enabled=True,
        ))
        db.commit()
        payload = BatchStockUpdateRequest(
            type=3,
            stocks=[
                BatchStockCandidate(code="SH.510300", name="沪深300", lever=1, emo_area="a"),
                BatchStockCandidate(code="SH.510500", name="中证500", lever=1, emo_area="a"),
            ],
            when_buy=-90,
            buy_amount=10_000,
        )

        result = asyncio.run(batch_update_stocks(payload, account_id="account-a", db=db))
        rows = {
            item.code: {"when_buy": item.when_buy, "buy_amount": item.buy_amount, "enabled": item.enabled}
            for item in db.query(SzdtTradeStock).all()
        }

    assert result == {"message": "批量设置完成", "updated": 1, "created": 1, "skipped": 0}
    assert rows["SH.510300"]["when_buy"] == -90
    assert rows["SH.510300"]["buy_amount"] == 10_000
    assert rows["SH.510500"]["when_buy"] == -90
    assert rows["SH.510500"]["buy_amount"] == 10_000
    assert rows["SH.510500"]["enabled"] is False

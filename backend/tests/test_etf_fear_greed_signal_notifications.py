"""自算贪恐顶底信号邮件提醒测试。"""

from datetime import date

from src.core.database import (
    Base,
    ETFFearGreedSignalNotification,
    Session,
)
from src.core.services import etf_fear_greed_clone_service as service


def test_latest_turn_signal_email_is_deduplicated_after_success(monkeypatch):
    """同一指数、日期和信号类型只成功投递一次。"""
    Base.metadata.create_all(Session().bind)
    with Session() as db:
        db.query(ETFFearGreedSignalNotification).filter(
            ETFFearGreedSignalNotification.symbol == "SOXX.US",
            ETFFearGreedSignalNotification.signal_date == date(2026, 10, 7),
        ).delete()
        db.commit()
    Session.remove()

    class FakeCalculator:
        def _normalize_etf_symbol(self, symbol):
            return symbol.upper()

        def load_history_from_db(self, **_kwargs):
            return {
                "latest": {
                    "date": "2026-10-07",
                    "score": 21.5,
                    "signals": [{"kind": "ma5_bottom", "label": "均线底"}],
                }
            }

    sent = []
    monkeypatch.setattr(service, "ETFFearGreedCloneCalculator", FakeCalculator)
    monkeypatch.setattr(
        service,
        "send_configured_email",
        lambda scenario, subject, body: sent.append((scenario, subject, body)) or True,
    )

    assert service.notify_latest_turn_signals(["SOXX.US"]) == 1
    assert service.notify_latest_turn_signals(["SOXX.US"]) == 0
    assert len(sent) == 1
    assert sent[0][0] == "fear_greed_turn_signal"
    assert "SOXX 半导体" in sent[0][2]

    with Session() as db:
        notification = db.get(
            ETFFearGreedSignalNotification,
            ("SOXX.US", date(2026, 10, 7), "ma5_bottom"),
        )
        assert notification is not None
        assert notification.sent_at is not None
    Session.remove()


def test_latest_turn_signal_email_retries_when_delivery_fails(monkeypatch):
    """投递失败不标记已发送，下一次日更会继续尝试。"""
    Base.metadata.create_all(Session().bind)
    with Session() as db:
        db.query(ETFFearGreedSignalNotification).filter(
            ETFFearGreedSignalNotification.symbol == "QQQ.US",
            ETFFearGreedSignalNotification.signal_date == date(2026, 10, 8),
        ).delete()
        db.commit()
    Session.remove()

    class FakeCalculator:
        def _normalize_etf_symbol(self, symbol):
            return symbol.upper()

        def load_history_from_db(self, **_kwargs):
            return {
                "latest": {
                    "date": "2026-10-08",
                    "score": 77.0,
                    "signals": [{"kind": "volume_top", "label": "缩量顶"}],
                }
            }

    monkeypatch.setattr(service, "ETFFearGreedCloneCalculator", FakeCalculator)
    monkeypatch.setattr(service, "send_configured_email", lambda *_args: False)

    assert service.notify_latest_turn_signals(["QQQ.US"]) == 0
    assert service.notify_latest_turn_signals(["QQQ.US"]) == 0
    with Session() as db:
        notification = db.get(
            ETFFearGreedSignalNotification,
            ("QQQ.US", date(2026, 10, 8), "volume_top"),
        )
        assert notification is not None
        assert notification.sent_at is None
    Session.remove()

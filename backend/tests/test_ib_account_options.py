"""IB account selector endpoint tests."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.app.api import ib_accounts as api
from src.app.api.ib_accounts import router
from src.core.database import Base, IBKRAccountConfig


def _build_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[IBKRAccountConfig.__table__])
    session = sessionmaker(bind=engine, expire_on_commit=False)()

    app = FastAPI()
    app.include_router(router)

    def _get_db():
        yield session

    app.dependency_overrides[api.get_db] = _get_db
    app.dependency_overrides[api.valid_admin_account] = lambda: "acct"
    return TestClient(app), session


def test_options_returns_only_selector_fields_for_current_account():
    client, session = _build_client()
    session.add_all([
        IBKRAccountConfig(
            id=2,
            account_id="acct",
            name="Paper",
            ib_port=4002,
            tws_userid="secret-user",
            tws_password="secret-password",
            container_name="ib-paper",
        ),
        IBKRAccountConfig(
            id=1,
            account_id="acct",
            name="Live",
            ib_port=4001,
        ),
        IBKRAccountConfig(
            id=3,
            account_id="other-account",
            name="Other user",
            ib_port=4003,
        ),
    ])
    session.commit()

    response = client.get("/api/ib-accounts/options")

    assert response.status_code == 200
    assert response.json() == [
        {"id": 1, "name": "Live", "ib_port": 4001},
        {"id": 2, "name": "Paper", "ib_port": 4002},
    ]


def test_options_empty_list():
    client, _session = _build_client()

    response = client.get("/api/ib-accounts/options")

    assert response.status_code == 200
    assert response.json() == []

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from fastapi import WebSocketDisconnect
from uvicorn.protocols.utils import ClientDisconnected

from src.app.api import external_trading_accounts as api_module
from src.core.services.external_trading import INITIAL_WEBSOCKET_SEND_STATE_ERROR, ExternalTradingHub


def _account():
    return SimpleNamespace(
        id=1,
        account_id="acct-1",
        name="Demo Account",
        identifier="demo-identifier",
        market_type="A_STOCK",
        enabled=True,
    )


class _HubWebSocket:
    def __init__(self, send_exception=None):
        self.send_exception = send_exception
        self.accepted = False
        self.sent_messages = []
        self.closed_calls = []

    async def accept(self):
        self.accepted = True

    async def send_text(self, message):
        if self.send_exception is not None:
            raise self.send_exception
        self.sent_messages.append(message)

    async def close(self, code=None, reason=None):
        self.closed_calls.append((code, reason))


class _AccountQuery:
    def __init__(self, account):
        self.account = account

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self.account


class _DBSession:
    def __init__(self, account):
        self.account = account
        self.closed = False

    def query(self, _model):
        return _AccountQuery(self.account)

    def close(self):
        self.closed = True


class _ApiWebSocket:
    def __init__(self):
        self.query_params = {
            "account_id": "acct-1",
            "identifier": "demo-identifier",
            "ts": "123456",
            "nonce": "nonce",
            "signature": "signature",
        }
        self.close_calls = []

    async def close(self, code=None, reason=None):
        self.close_calls.append((code, reason))


class ExternalTradingHubConnectTest(IsolatedAsyncioTestCase):
    async def test_connect_rolls_back_when_initial_send_disconnects(self):
        hub = ExternalTradingHub()
        lifecycle_events = []
        hub._mark_connected = lambda account_pk: lifecycle_events.append(("connected", account_pk))
        hub._mark_disconnected = lambda account_pk, reason: lifecycle_events.append(("disconnected", account_pk, reason))
        websocket = _HubWebSocket(send_exception=WebSocketDisconnect(code=1006))

        with self.assertRaises(WebSocketDisconnect):
            await hub.connect(websocket, _account())

        self.assertTrue(websocket.accepted)
        self.assertEqual([], websocket.sent_messages)
        self.assertEqual({}, hub._connections)
        self.assertEqual(
            [("disconnected", 1, "client disconnected during initial connect")],
            lifecycle_events,
        )

    async def test_connect_rolls_back_when_initial_send_hits_asgi_state_error(self):
        hub = ExternalTradingHub()
        lifecycle_events = []
        hub._mark_connected = lambda account_pk: lifecycle_events.append(("connected", account_pk))
        hub._mark_disconnected = lambda account_pk, reason: lifecycle_events.append(("disconnected", account_pk, reason))
        websocket = _HubWebSocket(send_exception=RuntimeError(INITIAL_WEBSOCKET_SEND_STATE_ERROR))

        with self.assertRaisesRegex(RuntimeError, "Expected ASGI message"):
            await hub.connect(websocket, _account())

        self.assertTrue(websocket.accepted)
        self.assertEqual([], websocket.sent_messages)
        self.assertEqual({}, hub._connections)
        self.assertEqual(
            [("disconnected", 1, f"initial connect send failed: {INITIAL_WEBSOCKET_SEND_STATE_ERROR}")],
            lifecycle_events,
        )

    async def test_connect_marks_connected_after_initial_send(self):
        hub = ExternalTradingHub()
        lifecycle_events = []
        hub._mark_connected = lambda account_pk: lifecycle_events.append(("connected", account_pk))
        hub._mark_disconnected = lambda account_pk, reason: lifecycle_events.append(("disconnected", account_pk, reason))
        websocket = _HubWebSocket()

        conn = await hub.connect(websocket, _account())

        self.assertTrue(websocket.accepted)
        self.assertEqual(1, len(websocket.sent_messages))
        self.assertIs(hub._connections[1], conn)
        self.assertEqual([("connected", 1)], lifecycle_events)


class ExternalTradingWebSocketApiTest(IsolatedAsyncioTestCase):
    async def test_ptrade_bridge_can_connect_to_migrated_external_account(self):
        websocket = _ApiWebSocket()
        websocket.query_params["account_id"] = api_module.PTRADE_BRIDGE_ACCOUNT_ID
        migrated_account = _account()
        migrated_account.account_id = "migrated-admin-account"
        db_session = _DBSession(migrated_account)
        connection = SimpleNamespace(account_pk=1, connection_id="conn-1")
        connect_mock = AsyncMock(return_value=connection)
        websocket.receive_text = AsyncMock(side_effect=WebSocketDisconnect(code=1000))
        disconnect_mock = AsyncMock()

        with patch.object(api_module, "is_valid_account", return_value=False) as valid_account_mock, patch.object(
            api_module,
            "verify_handshake_signature",
            return_value=None,
        ), patch.object(api_module, "ExternalTradingDBSession", return_value=db_session), patch.object(
            api_module.external_trading_hub,
            "connect",
            connect_mock,
        ), patch.object(
            api_module.external_trading_hub,
            "disconnect",
            disconnect_mock,
        ):
            result = await api_module.external_trading_websocket(websocket)

        self.assertIsNone(result)
        self.assertTrue(db_session.closed)
        self.assertEqual([], websocket.close_calls)
        valid_account_mock.assert_not_called()
        connect_mock.assert_awaited_once_with(websocket, migrated_account)
        disconnect_mock.assert_awaited_once_with(1, "conn-1", reason="client disconnected")

    async def test_initial_connect_disconnect_or_asgi_state_error_is_swallowed(self):
        for exc in (
            WebSocketDisconnect(code=1006),
            ClientDisconnected(),
            RuntimeError(INITIAL_WEBSOCKET_SEND_STATE_ERROR),
        ):
            with self.subTest(exception_type=type(exc).__name__):
                websocket = _ApiWebSocket()
                db_session = _DBSession(_account())
                connect_mock = AsyncMock(side_effect=exc)

                with patch.object(api_module, "is_valid_account", return_value=True), patch.object(
                    api_module,
                    "verify_handshake_signature",
                    return_value=None,
                ), patch.object(api_module, "ExternalTradingDBSession", return_value=db_session), patch.object(
                    api_module.external_trading_hub,
                    "connect",
                    connect_mock,
                ):
                    result = await api_module.external_trading_websocket(websocket)

                self.assertIsNone(result)
                self.assertTrue(db_session.closed)
                self.assertEqual([], websocket.close_calls)
                connect_mock.assert_awaited_once()

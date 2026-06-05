from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from src.core.services.ib_service import IBOrderSubmissionPending
from src.robot.portfolio_copy_trader import PortfolioCopyTrader


class PortfolioCopyTraderTest(IsolatedAsyncioTestCase):
    async def test_rebalance_logs_ack_timeout_as_submitted(self):
        PortfolioCopyTrader._instance = None
        trader = PortfolioCopyTrader()
        trader._log = Mock()
        trader.calculate_rebalance_plan = AsyncMock(
            return_value=[
                {
                    "symbol": "AAPL",
                    "action": "BUY",
                    "quantity": 3,
                    "price": 100.0,
                    "current_ratio": 10.0,
                    "target_ratio": 15.0,
                }
            ]
        )

        fake_ib = SimpleNamespace(
            is_market_open=AsyncMock(return_value=True),
            place_market_order=AsyncMock(
                side_effect=IBOrderSubmissionPending("AAPL", 8.0, "PendingSubmit")
            ),
        )
        trader._ensure_ib_connected = AsyncMock(return_value=fake_ib)

        config = SimpleNamespace(
            id=1,
            account_id="acct-1",
            portfolio_id="portfolio-1",
            account_type="ibkr",
            ib_port=4001,
            ib_account_id=None,
        )

        with patch("src.robot.portfolio_copy_trader.MarketService.is_us_market_open", return_value=True):
            await trader.rebalance(config)

        trader._log.assert_called_once()
        _, _, action, status, message = trader._log.call_args.args[:5]
        self.assertEqual("BUY", action)
        self.assertEqual("SUBMITTED", status)
        self.assertIn("still pending", message)

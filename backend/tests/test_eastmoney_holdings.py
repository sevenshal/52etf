from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from src.robot.eastmoney_holdings import (
    EastmoneyClient,
    EastmoneyCredentials,
    compute_eastmoney_sign,
)


class EastmoneyHoldingsTest(IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = EastmoneyClient(EastmoneyCredentials("ct", "ut", "user", "device"))

    async def asyncTearDown(self):
        await self.client.client.aclose()

    def test_sign_matches_reference_implementation(self):
        envelope = {"args": {"z": "ABC", "a": True}, "method": "Demo", "timestamp": 123}
        self.assertEqual(
            "bc32ae80c59c775074300b4739e7095383b25342f27f4f4f93c741622ad18215",
            compute_eastmoney_sign(envelope),
        )

    async def test_holdings_are_flattened_with_segment(self):
        self.client._post = AsyncMock(return_value={
            "code": 0,
            "data": [{"BlockName": "半导体", "data": [{"__code": "688001"}]}],
        })
        rows = await self.client.fetch_holdings(900000001)
        self.assertEqual("半导体", rows[0]["segment_name"])

    async def test_missing_follow_permission_follows_then_retries(self):
        self.client._post = AsyncMock(side_effect=[RuntimeError("请先关注该组合"), {"code": 0, "data": []}])
        self.client.follow_combination = AsyncMock()
        rows = await self.client.fetch_holdings(900000001)
        self.assertEqual([], rows)
        self.client.follow_combination.assert_awaited_once_with(900000001)
        self.assertEqual(2, self.client._post.await_count)

    async def test_follow_accepts_eastmoney_success_message_with_nonzero_code(self):
        response = AsyncMock()
        response.raise_for_status = lambda: None
        response.json = lambda: {"code": -1, "message": "加自选成功"}
        self.client.client.get = AsyncMock(return_value=response)
        await self.client.follow_combination(900000001)

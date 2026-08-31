"""东方财富实盘榜单与组合持仓每日快照。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import string
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
from gmssl.sm4 import CryptSM4, SM4_ENCRYPT

from ..core.database import SystemServiceCredential, get_db_ctx
from ..core.duckdb_utils import ANALYTICS_DB_PATH, connect_duckdb


logger = logging.getLogger("eastmoney_holdings")
CHINA_TZ = ZoneInfo("Asia/Shanghai")
API_BASE_URL = "https://spzhapi.dfcfs.cn"
SNAPSHOT_TABLE = "eastmoney_cube_holdings_snapshots"
RANK_TYPE = "rate_250d_drawdown_0.2_asset_500k"
SM4_KEY_AND_IV = bytes.fromhex("e4dd41fd138867c3665492702fe277eb")
DEFAULT_DEVICE_ID = "E5099551-EB1E-4EDE-9B7A-7099B401C811"
DEFAULT_USER_AGENT = "%E4%B8%9C%E6%96%B9%E8%B4%A2%E5%AF%8C/20260826100.1031 CFNetwork/3860.700.1 Darwin/25.6.0"


@dataclass(frozen=True)
class EastmoneyCredentials:
    ct_token: str
    ut_token: str
    user_id: str
    device_id: str
    app_version: str = "11.3.1"
    rn_version: str = "2.3.0.260828155320143"
    app_ver_code: str = "11003001"


def _load_credentials() -> EastmoneyCredentials:
    with get_db_ctx() as db:
        row = db.get(SystemServiceCredential, "eastmoney")
        stored = {
            "ct_token": (row.cookie or "").strip() if row else "",
            "ut_token": (row.password or "").strip() if row else "",
            "user_id": (row.username or "").strip() if row else "",
        }
    values = {
        "ct_token": os.getenv("EASTMONEY_CT_TOKEN", stored["ct_token"]).strip(),
        "ut_token": os.getenv("EASTMONEY_UT_TOKEN", stored["ut_token"]).strip(),
        "user_id": os.getenv("EASTMONEY_USER_ID", stored["user_id"]).strip(),
    }
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"东方财富凭据缺失: {', '.join(missing)}")
    return EastmoneyCredentials(
        **values,
        device_id=os.getenv("EASTMONEY_DEVICE_ID", DEFAULT_DEVICE_ID).strip(),
        app_version=os.getenv("EASTMONEY_APP_VERSION", "11.3.1").strip(),
        rn_version=os.getenv("EASTMONEY_RN_VERSION", "2.3.0.260828155320143").strip(),
        app_ver_code=os.getenv("EASTMONEY_APP_VER_CODE", "11003001").strip(),
    )


def _deep_sort(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _deep_sort(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_deep_sort(item) for item in value]
    return value


def compute_eastmoney_sign(envelope: Dict[str, Any]) -> str:
    canonical = json.dumps(
        _deep_sort(envelope), ensure_ascii=False, separators=(",", ":")
    ).lower().encode("utf-8")
    cipher = CryptSM4()
    cipher.set_key(SM4_KEY_AND_IV, SM4_ENCRYPT)
    encrypted = cipher.crypt_cbc(SM4_KEY_AND_IV, canonical)
    return hashlib.sha256(encrypted.hex().encode("ascii")).hexdigest()


class EastmoneyClient:
    def __init__(self, credentials: EastmoneyCredentials, timeout: float = 20.0):
        self.credentials = credentials
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(timeout))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        await self.client.aclose()

    def _envelope(self, method: str, args: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "args": args,
            "method": method,
            "client": "ios",
            "randomCode": "".join(random.choices(string.ascii_letters + string.digits, k=32)),
            "timestamp": int(datetime.now().timestamp() * 1000),
            "deviceId": self.credentials.device_id,
            "clientType": "cfw",
            "clientVersion": self.credentials.app_version,
        }

    async def _post(self, method: str, args: Dict[str, Any]) -> Dict[str, Any]:
        body = self._envelope(method, args)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "rnversion": self.credentials.rn_version,
            "appversion": self.credentials.app_version,
            "Content-Type": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
            "sign": compute_eastmoney_sign(body),
        }
        response = await self.client.post(f"{API_BASE_URL}/rtV3", headers=headers, json=body)
        response.raise_for_status()
        payload = response.json()
        if int(payload.get("code", -1)) != 0:
            raise RuntimeError(f"东方财富接口 {method} 失败: {payload.get('code')} {payload.get('message')}")
        return payload

    def _auth_args(self) -> Dict[str, str]:
        return {
            "ctToken": self.credentials.ct_token,
            "utToken": self.credentials.ut_token,
            "userId": self.credentials.user_id,
        }

    async def fetch_rank_page(self, page: int, page_size: int = 20) -> Dict[str, Any]:
        return await self._post("profit_rank_handler", {
            **self._auth_args(), "pageNum": page, "pageSize": page_size,
            "type": "rate", "unit": "250d", "drawdownFilterType": "0.2",
            "assetFilterType": "500k", "showOperFlag": False,
        })

    async def follow_combination(self, combination_id: int) -> None:
        query = urlencode({
            "type": "rt_add_concern", "ctToken": self.credentials.ct_token,
            "utToken": self.credentials.ut_token, "appVer": self.credentials.app_ver_code,
            "zh": combination_id, "userId": self.credentials.user_id,
        })
        response = await self.client.get(f"{API_BASE_URL}/srtV1?{query}")
        response.raise_for_status()
        payload = response.json()
        message = str(payload.get("message") or payload.get("msg") or "")
        if int(payload.get("code", -1)) != 0 and "成功" not in message:
            raise RuntimeError(f"关注东方财富组合 {combination_id} 失败: {payload.get('message')}")

    async def fetch_holdings(self, combination_id: int) -> List[Dict[str, Any]]:
        args = {**self._auth_args(), "combinationId": combination_id}
        try:
            payload = await self._post("CombinationHoldPositionPermitHandler", args)
        except RuntimeError as exc:
            if "关注" not in str(exc) and "concern" not in str(exc).lower():
                raise
            await self.follow_combination(combination_id)
            payload = await self._post("CombinationHoldPositionPermitHandler", args)
        rows: List[Dict[str, Any]] = []
        for group in payload.get("data") or []:
            segment = group.get("BlockName") or ""
            for item in group.get("data") or []:
                row = dict(item)
                row["segment_name"] = segment
                rows.append(row)
        return rows


def _ensure_schema(connection) -> None:
    connection.execute(f"""
        CREATE TABLE IF NOT EXISTS {SNAPSHOT_TABLE} (
            snapshot_date DATE NOT NULL, snapshot_at TIMESTAMP NOT NULL,
            rank_type VARCHAR NOT NULL, year_rank INTEGER, cube_symbol VARCHAR NOT NULL,
            cube_id BIGINT, cube_name VARCHAR, screen_name VARCHAR,
            latest_rebalance_at TIMESTAMP, latest_rebalance_id BIGINT,
            latest_rebalance_status VARCHAR, active_rebalance_at TIMESTAMP,
            active_rebalance_id BIGINT, active_rebalance_status VARCHAR,
            active_rebalance_category VARCHAR, active_rebalance_source VARCHAR,
            holdings_source VARCHAR, active_rebalance_days INTEGER, is_active BOOLEAN,
            stock_symbol VARCHAR NOT NULL, raw_stock_symbol VARCHAR, stock_name VARCHAR,
            stock_id BIGINT, segment_name VARCHAR, weight_pct DOUBLE,
            raw_holding_json VARCHAR, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL,
            PRIMARY KEY (snapshot_date, cube_symbol, stock_symbol)
        )
    """)
    connection.execute(
        f"CREATE INDEX IF NOT EXISTS idx_eastmoney_holdings_stock ON {SNAPSHOT_TABLE}(snapshot_date, stock_symbol)"
    )


def _symbol(item: Dict[str, Any]) -> str:
    value = str(item.get("stkMktCode") or "").strip().upper()
    if len(value) == 8 and value[:2] in {"SH", "SZ", "BJ"}:
        return f"{value[:2]}.{value[2:]}"
    code = str(item.get("__code") or "").zfill(6)
    market = str(item.get("market") or "")
    return f"{'SH' if market == '1' else 'SZ'}.{code}"


async def run_eastmoney_holdings_job(*, force: bool = False, workers: int = 4) -> Dict[str, Any]:
    run_at = datetime.now(CHINA_TZ)
    if not force and run_at.weekday() >= 5:
        return {"skipped": True, "message": f"{run_at.date()} 不是A股交易日"}
    credentials = _load_credentials()
    async with EastmoneyClient(credentials) as client:
        first = await client.fetch_rank_page(1)
        data = first.get("data") or {}
        rankings = list(data.get("pages") or [])
        total_pages = int(data.get("totalPages") or 1)
        for page in range(2, total_pages + 1):
            payload = await client.fetch_rank_page(page)
            rankings.extend((payload.get("data") or {}).get("pages") or [])
            await asyncio.sleep(0.08)

        semaphore = asyncio.Semaphore(max(1, workers))
        async def fetch(rank: int, combination: Dict[str, Any]):
            async with semaphore:
                try:
                    return rank, combination, await client.fetch_holdings(int(combination["combinationId"])), None
                except Exception as exc:  # noqa: BLE001
                    logger.warning("东方财富组合 %s 持仓抓取失败: %s", combination.get("combinationId"), exc)
                    return rank, combination, [], str(exc)
        results = await asyncio.gather(*(fetch(rank, item) for rank, item in enumerate(rankings, 1)))

    now = datetime.now(CHINA_TZ).replace(tzinfo=None)
    snapshot_at = run_at.replace(tzinfo=None)
    rows = []
    failed = []
    for rank, combination, holdings, error in results:
        if error:
            failed.append({"combination_id": combination.get("combinationId"), "error": error})
            continue
        relocate = combination.get("relocatePosition") or []
        dates = [str(item.get("bizDate")) for item in relocate if item.get("bizDate")]
        latest_rebalance = max(dates) if dates else None
        latest_rebalance_at = datetime.strptime(latest_rebalance, "%Y%m%d") if latest_rebalance else None
        for holding in holdings:
            weight = float(holding.get("positionRateDetail") or 0)
            if weight <= 0:
                continue
            symbol = _symbol(holding)
            rows.append({
                "snapshot_date": snapshot_at.date(), "snapshot_at": snapshot_at,
                "rank_type": RANK_TYPE, "year_rank": rank,
                "cube_symbol": str(combination.get("combinationId")),
                "cube_id": combination.get("combinationId"),
                "cube_name": combination.get("userName") or str(combination.get("combinationId")),
                "screen_name": combination.get("userName") or "",
                "latest_rebalance_at": latest_rebalance_at, "latest_rebalance_id": None,
                "latest_rebalance_status": "rank_relocate" if relocate else "",
                "active_rebalance_at": latest_rebalance_at, "active_rebalance_id": None,
                "active_rebalance_status": "ranked", "active_rebalance_category": "实盘榜单",
                "active_rebalance_source": "eastmoney_profit_rank",
                "holdings_source": "CombinationHoldPositionPermitHandler",
                "active_rebalance_days": None, "is_active": True,
                "stock_symbol": symbol, "raw_stock_symbol": symbol.replace(".", ""),
                "stock_name": holding.get("__name") or "", "stock_id": None,
                "segment_name": holding.get("segment_name") or "", "weight_pct": weight,
                "raw_holding_json": json.dumps(holding, ensure_ascii=False, separators=(",", ":")),
                "created_at": now, "updated_at": now,
            })
    if rankings and len(failed) > max(3, len(rankings) // 10):
        raise RuntimeError(f"东方财富持仓失败过多: {len(failed)}/{len(rankings)}")
    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=False)
    try:
        _ensure_schema(connection)
        connection.execute(f"DELETE FROM {SNAPSHOT_TABLE} WHERE snapshot_date = ?", [snapshot_at.date()])
        if rows:
            frame = pd.DataFrame(rows)
            connection.register("eastmoney_snapshot_rows", frame)
            columns = ", ".join(f'"{column}"' for column in frame.columns)
            connection.execute(f"INSERT INTO {SNAPSHOT_TABLE} ({columns}) SELECT {columns} FROM eastmoney_snapshot_rows")
    finally:
        connection.close()
    return {
        "snapshot_date": snapshot_at.date().isoformat(), "rank_count": len(rankings),
        "holding_rows": len(rows), "failed_count": len(failed), "failed": failed[:10],
    }


def process_eastmoney_holdings_refresh_for_robot() -> str:
    result = asyncio.run(run_eastmoney_holdings_job())
    if result.get("skipped"):
        return f"跳过东方财富实盘榜单刷新: {result.get('message')}"
    return (
        f"东方财富实盘榜单与持仓刷新 rank={result['rank_count']} "
        f"rows={result['holding_rows']} failed={result['failed_count']}"
    )

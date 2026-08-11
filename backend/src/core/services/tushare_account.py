"""Write-only, administrator-managed Tushare account settings."""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from ..database import TushareAccountConfig, get_db_ctx


def _masked_token(token: Optional[str]) -> Optional[str]:
    value = str(token or "").strip()
    if not value:
        return None
    return f"••••{value[-4:]}"


def _environment_token() -> str:
    return (os.getenv("TUSHARE_API_KEY") or os.getenv("TUSHARE_TOKEN") or "").strip()


def get_tushare_token_for_runtime() -> str:
    """Read a saved page setting first, with an environment bootstrap fallback."""
    with get_db_ctx() as db:
        config = db.get(TushareAccountConfig, 1)
        saved = str(config.api_token or "").strip() if config else ""
    return saved or _environment_token()


def get_tushare_account_settings() -> Dict[str, Any]:
    """Return only a redacted description; never return the full token."""
    with get_db_ctx() as db:
        config = db.get(TushareAccountConfig, 1)
        saved = str(config.api_token or "").strip() if config else ""
        environment = _environment_token()
        effective = saved or environment
        return {
            "configured": bool(effective),
            "source": "PAGE" if saved else ("ENVIRONMENT" if environment else None),
            "token_hint": _masked_token(effective),
            "updated_at": config.updated_at if config else None,
            "updated_by": config.updated_by if config else None,
        }


def update_tushare_account_settings(*, api_token: Optional[str], updated_by: str) -> Dict[str, Any]:
    """Save or clear the write-only token in a short SQLite transaction."""
    token = str(api_token or "").strip()
    if api_token is not None and token and len(token) > 512:
        raise ValueError("Tushare Token 不能超过 512 个字符")
    with get_db_ctx() as db:
        config = db.get(TushareAccountConfig, 1)
        if not config:
            config = TushareAccountConfig(id=1)
            db.add(config)
        if api_token is not None:
            config.api_token = token or None
        config.updated_by = updated_by

    # New recommendation jobs must pick up a just-saved token without a restart.
    from .tushare import TushareService

    TushareService.clear_cached_instances()
    return get_tushare_account_settings()

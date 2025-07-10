from dataclasses import dataclass
from typing import Optional

@dataclass
class AccountCfg:
    """账户配置数据模型"""
    account_no: str
    email: str
    app_key: str
    app_secret: str
    access_token: str
    access_token_expired_at: Optional[float] = None
    account_id: Optional[str] = None
    
@dataclass
class SzdtActiveCode:
    activated: bool
    code: str
    activated_at: str

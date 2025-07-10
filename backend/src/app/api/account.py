from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from typing import Optional
import os
from ...core.utils import read_json_file

router = APIRouter(prefix="/api/profile")

class AccountValidation(BaseModel):
    valid: bool
    message: str = ""

def get_accounts_file_path():
    """获取账户配置文件路径"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(current_dir, '..', 'data', 'accounts.json')

def is_valid_account(account_id: str) -> bool:
    """检查账户ID是否有效"""
    try:
        accounts = read_json_file(get_accounts_file_path())
        return account_id in accounts['valid_accounts']
    except Exception:
        return False

async def valid_account(x_account_id: Optional[str] = Header(None)) -> str:
    if not x_account_id:
        raise HTTPException(status_code=401, detail="Missing account ID")
    if not is_valid_account(x_account_id):
        raise HTTPException(status_code=401, detail="Invalid account ID")
    return x_account_id

@router.get("/validate-account", response_model=AccountValidation)
async def validate_account(account_id: str):
    """验证账户ID是否有效
    
    验证查询参数中的账户ID是否在accounts.json中存在。
    
    Args:
        account_id: 查询参数中的账户ID
    
    Returns:
        AccountValidation: 包含验证结果和消息的对象
    """
    if not account_id:
        return AccountValidation(valid=False, message="缺少账户ID")
    
    if is_valid_account(account_id):
        return AccountValidation(valid=True)
    else:
        return AccountValidation(valid=False, message="无效的账户ID") 

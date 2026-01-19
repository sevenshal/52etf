from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime
from pydantic import BaseModel

from ...core.database import get_db, LongPortAccount
from .account import valid_account

router = APIRouter(prefix="/api/longport-accounts", tags=["longport-accounts"])

class LongPortAccountBase(BaseModel):
    lp_account_id: str
    name: str
    app_key: str
    app_secret: str
    access_token: str = ""

class LongPortAccountResponse(LongPortAccountBase):
    id: int
    account_id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

@router.get("", response_model=List[LongPortAccountResponse])
async def list_longport_accounts(
    db: Session = Depends(get_db),
    account_id: str = Depends(valid_account)
):
    return db.query(LongPortAccount).filter(LongPortAccount.account_id == account_id).all()

from ...core.services.longport import LongPortService

# ... imports ...

# ... existing code ...

@router.post("", response_model=LongPortAccountResponse)
async def create_longport_account(
    account_data: LongPortAccountBase,
    db: Session = Depends(get_db),
    account_id: str = Depends(valid_account)
):
    # Check if lp_account_id exists globally
    existing = db.query(LongPortAccount).filter(LongPortAccount.lp_account_id == account_data.lp_account_id).first()
    if existing:
        raise HTTPException(status_code=400, detail="LongPort Account ID already exists")

    new_account = LongPortAccount(
        account_id=account_id,
        lp_account_id=account_data.lp_account_id,
        name=account_data.name,
        app_key=account_data.app_key,
        app_secret=account_data.app_secret,
        access_token=account_data.access_token
    )
    db.add(new_account)
    db.commit()
    db.refresh(new_account)
    
    # Reload service if it exists (or to initialize it)
    try:
        LongPortService.get_instance(new_account.lp_account_id).reload()
    except Exception as e:
        # Log error but don't fail the request, as the DB save was successful
        print(f"Failed to reload LongPortService: {e}")
        
    return new_account

@router.put("/{id}", response_model=LongPortAccountResponse)
async def update_longport_account(
    id: int,
    account_data: LongPortAccountBase,
    db: Session = Depends(get_db),
    account_id: str = Depends(valid_account)
):
    account = db.query(LongPortAccount).filter(
        LongPortAccount.id == id,
        LongPortAccount.account_id == account_id
    ).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
        
    account.name = account_data.name
    account.app_key = account_data.app_key
    account.app_secret = account_data.app_secret
    
    # Update access_token only if provided (or allow overwrite with empty if needed? 
    # Usually user might want to update token. If they send empty string, maybe we shouldn't wipe it unless intended.
    # But schema has access_token default "". Let's assume input is authoritative.)
    if account_data.access_token:
        account.access_token = account_data.access_token
        
    account.updated_at = datetime.now()
    
    db.commit()
    db.refresh(account)
    
    try:
        LongPortService.get_instance(account.lp_account_id).reload()
    except Exception as e:
        print(f"Failed to reload LongPortService: {e}")
        
    return account

@router.get("/{id}/status")
async def get_longport_account_status(
    id: int,
    db: Session = Depends(get_db),
    account_id: str = Depends(valid_account)
):
    account = db.query(LongPortAccount).filter(
        LongPortAccount.id == id,
        LongPortAccount.account_id == account_id
    ).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
        
    try:
        service = LongPortService.get_instance(account.lp_account_id)
        return service.get_status()
    except Exception as e:
        return {"status": "error", "message": f"Service error: {str(e)}"}

@router.delete("/{id}")
async def delete_longport_account(
    id: int,
    db: Session = Depends(get_db),
    account_id: str = Depends(valid_account)
):
    account = db.query(LongPortAccount).filter(
        LongPortAccount.id == id,
        LongPortAccount.account_id == account_id
    ).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    db.delete(account)
    db.commit()
    return {"message": "Deleted successfully"}

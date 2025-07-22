from fastapi import APIRouter, HTTPException
from ...core.services.fed_rate_monitor import FedRateMonitorService

router = APIRouter(prefix="/api/fed-rate")

@router.get("/monitor")
async def get_fed_rate_monitor():
    """获取美联储利率监控数据（自动使用缓存）"""
    try:
        service = FedRateMonitorService()
        data = service.fetch_data()
        
        if not data:
            raise HTTPException(status_code=404, detail="未能获取到美联储利率数据")
            
        return {
            "status": "success",
            "data": data
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取美联储利率数据失败: {str(e)}")    

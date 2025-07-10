from fastapi import APIRouter, HTTPException
from ...core.services.cnn_service import CNNService

router = APIRouter(prefix="/api/cnn")

@router.get("/fear-greed")
async def get_fear_greed_index(days: int = 1):
    """获取最新的CNN恐贪指数
    
    Args:
        days: 获取多少天的数据，默认1天
    """
    try:
        cnn_service = CNNService()
        return await cnn_service.get_fear_greed_index(days=days)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取CNN恐贪指数失败: {str(e)}") 
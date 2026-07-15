"""Controller 层：`/api/v1/modules` 路由 —— 获取可用模组列表。

MS1 只有一款内置模拟模组，由 service 层返回硬编码数据。
"""

from fastapi import APIRouter

from app.dto.common import ApiResponse
from app.dto.room import ModuleRead
from app.service import room as room_service

router = APIRouter(prefix="/modules", tags=["modules"])


@router.get("", response_model=ApiResponse[list[ModuleRead]])
async def list_modules() -> ApiResponse[list[ModuleRead]]:
    """GET /api/v1/modules —— 获取可用模组列表。"""
    modules = await room_service.list_modules()
    return ApiResponse.ok(modules)

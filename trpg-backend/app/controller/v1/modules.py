"""Controller 层：`/api/v1/modules` 路由 —— 模组目录 + 导入（issue #77 补充
模组详情 / 导入 2 个新端点）。

`GET /modules`、`GET /modules/{moduleId}` 是真实的数据库查询（内容库
`Scenario` 表）。导入那三个端点由 `exec/29` 第 5 步填成真实现：上传是
**multipart**（全项目第一个 `UploadFile`），转换在后台跑 5–26 分钟，靠轮询
拿进度，失败给一个重跑入口。
"""

from fastapi import APIRouter, Depends, File, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.controller.dependencies import get_current_user, get_optional_user
from app.core.db import async_session_factory, get_db
from app.core.errors import AppException, ErrorCode
from app.dto.common import ApiResponse
from app.dto.module import ModuleDetailRead, ModuleImportJobRead
from app.dto.room import ModuleRead
from app.models.user import User
from app.service import module_import as module_import_service
from app.service import room as room_service

router = APIRouter(prefix="/modules", tags=["modules"])


@router.get("", response_model=ApiResponse[list[ModuleRead]])
async def list_modules(
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
) -> ApiResponse[list[ModuleRead]]:
    """GET /api/v1/modules —— 可用模组：内置的 + 我自己导入的。

    🔴 登录**可选**而不是必需：这个接口在导入功能之前是完全公开的（e2e 与
    未登录的选模组页都在用），改成必需会当场打断那些调用方。可选的代价是
    没登录时看不到自己的导入——这正是想要的默认（见 service 层注释）。
    """
    modules = await room_service.list_modules(db, user_id=user.id if user else None)
    return ApiResponse.ok(modules)


@router.get("/import", response_model=ApiResponse[list[ModuleImportJobRead]])
async def list_import_jobs(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ApiResponse[list[ModuleImportJobRead]]:
    """GET /api/v1/modules/import —— 我的导入记录（含正在转的）。

    🔴 **必须声明在 `/{module_id}` 之前**：FastAPI 按声明顺序匹配，写在后面
    会被 `/modules/{module_id}` 当成 module_id="import" 吞掉。
    """
    jobs = await module_import_service.list_import_jobs(db, user_id=user.id)
    return ApiResponse.ok(jobs)


@router.get("/{module_id}", response_model=ApiResponse[ModuleDetailRead])
async def get_module_detail(
    module_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ApiResponse[ModuleDetailRead]:
    """GET /api/v1/modules/{moduleId} —— 模组详情。

    要登录：导入模组有主，受众判定在 service 层（`_may_read_module`）。
    """
    module = await room_service.get_module_detail(db, module_id, user.id)
    if module is None:
        raise AppException(ErrorCode.NOT_FOUND, "模组不存在", status.HTTP_404_NOT_FOUND)
    return ApiResponse.ok(module)


@router.delete("/{module_id}", response_model=ApiResponse[None])
async def delete_module(
    module_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ApiResponse[None]:
    """DELETE /api/v1/modules/{moduleId} —— 把自己导入的模组删掉。

    只有主人能删（内置模组无主，顺带挡住）；**有房间在用就 409 并报出几个**
    ——判据与清理范围见 `room_service.delete_module`。
    """
    await room_service.delete_module(db, module_id, user.id)
    return ApiResponse.ok(None)


@router.post(
    "/import", response_model=ApiResponse[ModuleImportJobRead], status_code=status.HTTP_201_CREATED
)
async def import_module(
    file: UploadFile = File(..., description="模组正文（pdf/docx/doc/txt），一个文件"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ApiResponse[ModuleImportJobRead]:
    """POST /api/v1/modules/import —— 上传模组，起一个后台转换任务。

    🔴 **全项目第一个 `UploadFile`**。转换要跑 5–26 分钟，所以这里只收文件、
    建 job、立刻返回；进度靠 `GET /import/{jobId}` 轮询。
    """
    job = await module_import_service.start_import(
        db, file, user_id=user.id, session_factory=async_session_factory
    )
    return ApiResponse.ok(job)


@router.get("/import/{job_id}", response_model=ApiResponse[ModuleImportJobRead])
async def get_import_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ApiResponse[ModuleImportJobRead]:
    """GET /api/v1/modules/import/{jobId} —— 轮询导入任务状态。

    🔴 鉴权在 service 层（看不到就当不存在），理由见那里。
    """
    job = await module_import_service.get_import_job(db, job_id, user.id)
    return ApiResponse.ok(job)


@router.post("/import/{job_id}/retry", response_model=ApiResponse[ModuleImportJobRead])
async def retry_import_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> ApiResponse[ModuleImportJobRead]:
    """POST /api/v1/modules/import/{jobId}/retry —— 重跑一次。

    🔴 **新建一个 job，不复活旧的**：旧 job 的失败理由要留着，否则用户点三次
    就再也不知道前两次为什么失败（`exec/29 §7.2 ②`）。重跑由用户点，**不自动**
    ——那等于默默再花一次钱。
    """
    job = await module_import_service.retry_import(
        db, job_id, session_factory=async_session_factory
    )
    return ApiResponse.ok(job)

"""模组详情 / 导入相关的 pydantic 请求/响应模型（issue #77 §2 新增端点）。

`GET /modules/{moduleId}` 是真实的数据库查询（复用内容库的 `Scenario` 表）。
`POST /modules/import` 和 `GET /modules/import/{jobId}` 依赖的是真实 LLM
解析管线（归 #57，本期不做），两个接口本期固定返回 `NOT_IMPLEMENTED`
（issue 决策 7），这里只定义 DTO 形状占住协议位置。
"""

from pydantic import Field

from app.dto.common import CamelModel, UtcDatetime
from app.dto.room import ModuleRead


class ModuleDetailRead(ModuleRead):
    """GET /api/v1/modules/{moduleId} 返回——列表字段 + 玩家可见前情。

    - synopsis：目录简介（Scenario 表，选模组用）
    - player_intro / opening_script：来自 structured JSON 的玩家可见开场
      （绝不含 kp_truth；文件缺失时为 null）
    - story_pages：前端前情页直接渲染的段落列表（intro + opening 去重）
    """

    synopsis: str | None = None
    player_intro: str | None = None
    opening_script: str | None = None
    story_pages: list[str] = Field(default_factory=list)


class ModuleImportRequestBody(CamelModel):
    """POST /api/v1/modules/import 请求体。

    真实实现（#57）会接收模组原始文档做 LLM 解析，本期这个接口固定返回
    NOT_IMPLEMENTED，请求体只占位描述"以后大概会传什么"，不做内容校验。
    """

    source_filename: str = Field(..., min_length=1, max_length=255)


class ModuleImportJobRead(CamelModel):
    """POST /api/v1/modules/import 与 GET /api/v1/modules/import/{jobId} 返回。

    不用 `from_attributes` 直接从 ORM 对象转换——ORM 主键列叫 `id`，这里
    对外字段叫 `job_id`（避免跟其它 DTO 的 `xxxId` 命名约定不一致），两者
    对不上，构造时由 service 层显式传关键字参数更直接。
    """

    job_id: str
    status: str
    source_filename: str | None = None
    result_scenario_id: str | None = None
    error_message: str | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime

"""模组详情 / 导入相关的 pydantic 请求/响应模型（issue #77 §2 新增端点）。

`GET /modules/{moduleId}` 是真实的数据库查询（复用内容库的 `Scenario` 表）。
`POST /modules/import` / `GET /modules/import/{jobId}` 的协议位是骨架期
（issue #77）占下的，`exec/29` 第 5 步把它们填成了真实现——上传从 JSON 体
换成 multipart（全项目第一个 `UploadFile`），所以原来的
`ModuleImportRequestBody` 已删除。
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


class ModuleImportJobRead(CamelModel):
    """POST /api/v1/modules/import 与 GET /api/v1/modules/import/{jobId} 返回。

    不用 `from_attributes` 直接从 ORM 对象转换——ORM 主键列叫 `id`，这里
    对外字段叫 `job_id`（避免跟其它 DTO 的 `xxxId` 命名约定不一致），两者
    对不上，构造时由 service 层显式传关键字参数更直接。

    🔴 **这个 DTO 是剧透约束的最后一道关**（`exec/29 §2`）。导入的人就是即将
    开玩的玩家，所以跨到前端的**只有数量与拓扑**——没有节点标题、没有 NPC 名字、
    **连生成的实体 id 都没有**（id 是从内容里长出来的）。失败原因只给封闭集合里
    的类别词（`job_state.FAILURE_KINDS`），不是错误原文——原文里带着 id、数值和
    半句正文。

    加字段前先回答：**它能不能装下一句剧透？** 能就别加。
    """

    job_id: str
    status: str
    #: 进度阶段，取值见 `job_state.STAGES`。
    stage: str
    #: 用户自己起的文件名——不是模组内容。
    source_filename: str | None = None
    result_scenario_id: str | None = None
    #: 拒绝理由。必须可执行（告诉用户下一步做什么）。
    error_message: str | None = None
    failure_kinds: list[str] = Field(default_factory=list)

    # ── 报告：只有数量与拓扑 ──────────────────────────
    #
    # 🔴 **故意不给默认值**。这些列在库里非空、有 server default，服务端每次都
    # 送得出来；给了默认值，生成的 TS 契约就变成 `pageCount?: number`，前端被迫
    # 写 `?? 0` —— 那正是这个项目反复禁止的静默兜底（数据没到位就用假值）。
    # 契约要如实说「这些数字一定在」。
    page_count: int
    image_count: int
    char_count: int
    item_count: int
    node_count: int
    npc_count: int
    ending_count: int
    agenda_count: int
    hard_failure_count: int

    retried_from_job_id: str | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    finished_at: UtcDatetime | None = None

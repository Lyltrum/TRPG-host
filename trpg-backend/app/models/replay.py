"""复盘与导入 ORM 模型（issue #77 §1，3 张表）。

三张表都还没有真实的写入路径：复盘摘要依赖 AI 编排生成内容（归 #48/#68），
模组导入依赖真实 LLM 解析管线（归 #57），本期只铺表 + 接口，读写均返回
`NOT_IMPLEMENTED`。`room_sessions` 记录一个房间每次"正式开局"的起止时间，
跟 `Room.started_at`/`ended_at`（房间当前这一局的时间戳）的区别是：房间允许
以后多次开局/复盘，`room_sessions` 才是真正按局区分的历史记录，本期同样只
铺表。
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class RoomSummary(Base):
    __tablename__ = "room_summaries"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    room_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("rooms.id"), unique=True, nullable=False
    )
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    highlights: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class RoomSession(Base):
    __tablename__ = "room_sessions"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    room_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("rooms.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 「上次讲到哪」（`exec/46` B3）。散会之后懒生成一次、落在这里，下次续跑
    # 时播给大家。
    #
    # 🔴 跟 `room_summaries` 的局末复盘**不是一回事**，别合并：复盘是散场后的
    # 回顾（受众是已经玩完的人，`finished` 之后还带谜底），这一段是**开场白**
    # ——它要让人接得上，所以重点在还悬着什么、下一步能往哪走。
    #
    # `None` 有两种含义（还没生成 / 生成失败），两种的处理一样（现算一次），
    # 所以不额外记一个状态位。
    recap_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class ModuleImportJob(Base):
    """一次模组导入的全过程（`exec/29` 第 5 步）。状态机在
    `app/core/module_import/job_state.py`。

    ## 🔴 这张表里为什么全是整数列，没有一个自由 JSON

    job 的字段是**唯一跨到前端的东西**，而本功能的第一性约束是「人类不许看见
    模组内容」。一个 `stats: JSON` 能装下任何东西——包括模组正文。所以进度与
    报告一律拆成**显式的整数列**，失败原因只留**封闭集合**里的类别词
    （`job_state.FAILURE_KINDS`）。

    **schema 表达不了的东西才漏不出去**，同「保密靠拿不到，不是请你别说」。
    加字段前先问：**它能不能装下一句剧透？** 能就别加。

    连生成的实体 id 都不许出现在这里——id 是从内容里长出来的。

    ## 版权

    `source_path` 指向服务器上保存的用户上传件（第三方模组正文），与
    `模组资料/` 同级红线：禁止进 git / 日志 / 磁带。**不出现在任何 DTO 里。**
    """

    __tablename__ = "module_import_jobs"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # 谁导入的。导入的模组归导入者所有（`Scenario.owner_user_id` 同源）。
    owner_user_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # 进度阶段，取值见 `job_state.STAGES`。分阶段是因为**每一阶段的失败对用户
    # 意味着不同的下一步**，不是为了好看。
    stage: Mapped[str] = mapped_column(String(20), nullable=False, default="received")

    # 用户自己的文件名——用户自己提供的，不算剧透。
    source_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 内容哈希：同一份文件导过就别再付一次钱（¥0.35 / 71 次调用）。
    source_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # 🔴 服务器上的上传件路径。**内部字段，绝不进 DTO。**
    source_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    result_scenario_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("scenarios.id"), nullable=True
    )
    # 拒绝理由。**必须可执行**（"换个文件" / "这是扫描件" / "这份模组转不了"），
    # 且只说数量与类别，不带 id、不带正文。
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 硬失败的**类别**（封闭集合，见 `job_state.FAILURE_KINDS`），不是原文。
    failure_kinds: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    # ── 报告：只有数量与拓扑 ──────────────────────────
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    image_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    node_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    npc_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ending_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    agenda_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hard_failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 🔴 线索账本那两个数（2026-08-25）。起点是一次实测：265 拍一整局
    # `keeper.fact_revealed` 零条，逐层量下来根因是**这份模组本身 facts=0**
    # ——而报告里那 9 个数一个都答不出这件事，导入者、开局的人、跑完 265 拍的人
    # 都看不见。它们是纯计数，一个字的剧透都没有（`exec/46` B1）。
    #
    # 🔴 **可空，跟上面那些不一样**：`None` = **这次导入没有量过这两个数**
    # （本次改动之前的所有 job 都是），`0` = 量过，确实是零。两者含义相反——
    # 前者该显示"—"，后者该弹警示。给个 default=0 就把它们压成同一个值了，
    # 那正是这个项目反复禁止的静默兜底。
    fact_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 标了 `reveals` 的检定点数（遍历**全部**节点，含子节点）。
    #: 它跟 `fact_count` 要一起看：facts 有但没有一个 check 指向它们，账本一样是死的。
    revealing_check_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 🔴 这里没有 `llm_call_count`：整条链的调用分散在 probe / relation_probe /
    # assemble 三个脚本里，只有 assemble 会往报告里写 `stats.calls`。填一个只覆盖
    # 三分之一的数、却叫"调用次数"，正是这个项目反复被咬的那种半真值。要它就得
    # 先统一三个脚本的客户端（那件事本来就欠着），不然就别立这个字段。

    # 重跑是**新建一个 job**，不是复活旧的——旧 job 的失败理由要留着，
    # 否则用户点三次就再也不知道前两次为什么失败（`exec/29 §7.2 ②`）。
    retried_from_job_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("module_import_jobs.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

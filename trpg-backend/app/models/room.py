"""房间相关 ORM 模型（issue #77 §1，运行时状态库的一部分）。

- Room：房间主表
- Player：房间内玩家成员表（issue #77 之前叫 `room_players`，本期改名对齐设计，
  补齐 `user_id`/`is_ai`/`joined_at`/`left_at`/`connected`）
- Character：房间内的角色卡（原来挂在 service/room.py 的内存字典里）
- Note：房间内玩家的速记本（本期只铺表，没有对应的读写接口——`note.save`
  WS 事件本期不铺，见 issue"本期不做"）
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class Room(Base):
    __tablename__ = "rooms"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    room_code: Mapped[str] = mapped_column(String(6), unique=True, index=True, nullable=False)
    room_name: Mapped[str] = mapped_column(String(200), nullable=False)
    max_players: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(20), nullable=False, default="Lobby")

    # 房主身份：host_player_id 是房间内身份（Player.id，房间创建后回写），
    # host_user_id 是账号身份（User.id）——两者是独立的身份体系（同一账号
    # 理论上可以用不同 nickname 在不同房间里当房主），本期 REST 创建/加入
    # 房间接口不强制要求登录（trpg-frontend 现在也没有在这两个请求上带
    # Authorization 头，属于零改动约束下的已知缺口），所以 host_user_id
    # 允许为空，只在调用方确实带了有效登录态时才回填。
    host_player_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), nullable=True, default=None
    )
    host_user_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id"), nullable=True, default=None
    )

    game_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("games.id"), nullable=True, default=None
    )
    system_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("game_systems.id"), nullable=True, default=None
    )
    scenario_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("scenarios.id"), nullable=True, default=None
    )
    attribute_gen_method: Mapped[str | None] = mapped_column(
        String(20), nullable=True, default=None
    )
    # 已探索场景 id 列表（对应 scenario_scenes.id），JSON 数组存起来，本期
    # 没有任何写入路径（推进场景发现属于规则引擎/编排器范畴），只铺字段。
    discovered_scene_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    # keeper agent（feat/keeper-agent 实验）的世界状态笔记：AI 主持人通过
    # update_state 工具写入的键值对，每轮生成前整体注入 prompt。后端不解释
    # 其内容——这是 agent 的自由笔记本，形状由 agent 自己决定。
    keeper_state: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    # 「大家在休息」（`exec/35`）。聚会游戏的物理现实：有人上厕所、点外卖、
    # 接电话。暂停期间**世界心跳不推进、行动提交被挡回**，但讨论区照常——
    # 休息时聊天正是它的用途。
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # 「骰子在桌上」（`exec/46` B5）。**默认 False = 现状逐字不变。**
    #
    # 🔴 它记的不是一个偏好，是**这一局的物理事实**：大家围坐一桌、手边有实体
    # 骰子。线下聚会里掷骰子是最有仪式感的动作，而此前玩家掷完只能无视它、
    # 照着系统给的数字玩——骰子成了摆设。
    #
    # 🔴 **它不破坏「规则权威在后端」**：要不要检定、目标值多少、算不算成功、
    # 大成功还是大失败、幸运能不能补，仍然全部由后端判。开着它之后后端让出的
    # 只有**随机数**这一件事。规则权威 ≠ 随机数权威，这两件事此前被绑在一起。
    #
    # 🔴 **开着也只是"允许报"，不是"必须报"**：玩家照样可以让系统掷。
    allow_manual_rolls: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # 玩家纠错通道（`exec/35`）的回滚点：上一轮**开始之前**的世界指针 +
    # 那一轮的原话。形状 `{"keeper_state": {...}, "utterances": [...]}`。
    #
    # 🔴 只存指针，不存 HP/线索/骰子。纠错的语义是「你把我的话理解错了」，
    # 不是「我要改结果」——能撤骰子就等于能刷骰子。
    last_turn_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    # 生命周期时间戳：created_at 是"建房时刻"，下面两个分别对应正式开局
    # （phase 变成 InGame）和结束游戏（phase 变成 Completed）的时刻。
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    players: Mapped[list["Player"]] = relationship(
        back_populates="room", cascade="all, delete-orphan"
    )


class Player(Base):
    """房间内玩家成员表（原 `room_players`，本期改名为 `players`）。"""

    __tablename__ = "players"

    # 「一个账号在一个房间里只能有一名玩家」这条不变式必须由数据库保证，不能只靠
    # service 层「先查再插」——那是 check-then-act，两个并发的重连/加入请求会同时
    # 查到「不存在」然后各插一行，幂等承诺当场失效、房间人数还会虚增（PR #110
    # review [2]）。约束放在这里，service 层配合捕获 IntegrityError 重查。
    #
    # `user_id` 可空，而 SQL 的唯一约束**不约束 NULL**（多行 NULL 互不冲突），
    # 所以 AI 玩家（`is_ai=true`，无账号）和迁移前遗留的无账号行不受影响。
    __table_args__ = (UniqueConstraint("room_id", "user_id", name="uq_players_room_user"),)

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    room_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("rooms.id"), nullable=False
    )
    # 关联账号：REST 创建/加入房间自 issue #106 起要求登录、必定回填；仍保留可空
    # 是为了 AI 玩家（`is_ai=true`）和迁移前的遗留行。
    user_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id"), nullable=True, default=None
    )
    is_ai: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    nickname: Mapped[str] = mapped_column(String(100), nullable=False)
    is_host: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ready: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    has_character: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reconnect_token: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), default=lambda: str(uuid.uuid4()), nullable=False
    )
    # WS 连接是否处于活跃状态：room.join 时置 True，WS 断开时置 False——
    # 重连（照常走 room.join）读这个字段判断"这个玩家掉线了吗"，
    # 本期只维护状态，不接断线重连的真实逻辑。
    connected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 中途离开（暂离）。他的角色暂时退出剧情：不进守秘人的在场名单、不算受众。
    #
    # 🔴 **跟上面那个 `left_at` 不是一回事，别合并**：`left_at` 由 WS 断开写，
    # 而**掉线不等于离场**——网卡抖一下角色就从剧情里消失就完蛋了。这是「一份
    # 数据扮演两个角色必出结构性 bug」的又一处，所以宁可多一列。
    away: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    room: Mapped["Room"] = relationship(back_populates="players")


class Character(Base):
    """房间内的角色卡（原本挂在 service/room.py 的 `_characters` 内存字典里）。"""

    __tablename__ = "characters"

    # 🔴 「一个玩家在一个房间只有一张卡」以前只是**惯例**：`quick_build` 复用
    # 已有那行、`create_character_draft` 每次新建，于是连点几次「用我的常用卡」
    # 就留下几张，而两条读路径认的还不是同一张（重连取第一行，队伍面板与守秘人
    # 取最后一行，且两处都没有 ORDER BY）。service 层已经统一成复用，但不变式
    # 得由数据库兜底——先查再插是 check-then-act，并发下照样各插一行。
    __table_args__ = (UniqueConstraint("room_id", "player_id", name="uq_characters_room_player"),)

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    room_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("rooms.id"), nullable=False
    )
    player_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("players.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")

    # 调查员基本信息。这几项此前只活在前端的本地状态里、从没进过后端，于是
    # 「角色卡以后端为唯一事实来源」只做到了一半：清掉浏览器缓存后姓名/职业/
    # 属性能从后端读回，年龄性别居住地却只是恰好等于表单默认值，看起来没丢、
    # 其实早就丢了（issue #96）。
    age: Mapped[int | None] = mapped_column(nullable=True)
    gender: Mapped[str | None] = mapped_column(String(20), nullable=True)
    residence: Mapped[str | None] = mapped_column(String(100), nullable=True)
    birthplace: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # 属性是怎么生成的："pointbuy"（点数购买法）或 "roll"（服务端权威掷骰）。
    #
    # 必须记下来，因为两种方法的合法判据完全不同（issue #96 决策 1）：点数购买法
    # 要校验「总点数不超预算」，而掷骰法本来就经常超——5 项 3d6*5 + 3 项
    # (2d6+6)*5，8 项总和均值约 457、理论范围 195–720。不区分方法就无条件校验
    # 预算的话，会把合法掷出来的角色卡判成非法，等于废掉 roll-attributes 端点。
    generation_method: Mapped[str] = mapped_column(String(20), nullable=False, default="pointbuy")

    # 掷点池法（"roll_pool"）掷出的权威总值——玩家把这个总值手动分配到八维，
    # complete 时校验"分配总和是否等于这个值"要有个真实依据，不能只信任
    # 前端报的数（见 `coc7/rules.py` 的 roll_pool 校验分支）。其余两种生成
    # 方法不写这一列，始终是 None。
    attribute_pool_total: Mapped[int | None] = mapped_column(nullable=True)

    # 建卡三条路径的来源（都可空，互斥但不做数据库层面强制）：
    # ① based_on_pregen_id：套用模组作者预设角色；
    # ② based_on_template_id：复用玩家自己的常用卡（issue 决策 5，本期不实现）；
    # ③ 都不填：从零选职业建卡，occupation 字段直接记职业名。
    based_on_pregen_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("module_pregens.id"), nullable=True
    )
    based_on_template_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("user_character_templates.id"), nullable=True
    )

    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 🔴 职业**用 id 定位**（exec/22）：职业名不唯一——规则表里有 6 组同名
    # 不同项的职业（律师 ×2、私家侦探 ×2、工匠 ×2…），信用区间乃至技能点公式
    # 都不同。此前只存名字，`find_occupation_by_name` 只能查回第一个匹配，
    # 于是"玩家选的是哪一个"在落库那一刻就丢了：合法的卡可能被判非法，
    # 更阴的是公式不同的那三组会把职业技能点预算算成另一个数、且不报错。
    # 又一次"用自由文本当标识符"，与 exec/17 同族。
    occupation_id: Mapped[int | None] = mapped_column(nullable=True)
    # 保留：展示用的职业名（也是老数据唯一的线索）。id 为空时回退按名字查。
    occupation: Mapped[str | None] = mapped_column(String(100), nullable=True)
    attributes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 玩家在属性步骤分配出来的原始属性（年龄修正之前）。
    # `attributes` 存的是**有效值**（年龄修正之后的最终属性，衍生值/技能基础值
    # /职业技能点公式都基于它算）；而点数预算、掷点池总和、步进为 5 这三条
    # 生成方法约束天然只对**分配值**成立——年龄修正必然把它们破坏掉
    # （见 wizard-bugfix-round4.md #20）。两者拆开存，校验各取所需。
    # 可空：本列之前建的角色卡没有这份数据，读取处一律回落到 `attributes`。
    allocated_attributes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    derived_stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    skills: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    equipment: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    background: Mapped[str] = mapped_column(Text, nullable=False, default="")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 结构化背景故事（信念/重要之人/意义之地/珍视之物/特质/外伤/恐惧症等引导
    # 字段，迁移自用户个人项目 coc-char-gen）。不删除上面 background/notes
    # 这两个扁平字段——向后兼容、风险低；这一列是额外的结构化补充，键的具体
    # 含义由前端表单决定，后端只透明存取。
    background_detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class Note(Base):
    """房间内玩家的速记本。本期只铺表，没有对应的读写接口。"""

    __tablename__ = "notes"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    room_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("rooms.id"), nullable=False
    )
    player_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("players.id"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

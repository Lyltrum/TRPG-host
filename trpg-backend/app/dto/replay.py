"""复盘摘要 / 事件回放的 pydantic 响应模型（issue #77 §2 新增端点）。

`GET /rooms/{roomId}/summary` 依赖 AI 编排生成复盘内容（归 #48/#68），本期
固定返回 `NOT_IMPLEMENTED`。`GET /rooms/{roomId}/replay` 则是真实实现——
读的是 ws.py 在 narration.push / action.submit 时写入的 `events` 表，是本期
少数"服务端真的在写、也真的在读"的完整数据闭环之一。
"""

from app.dto.common import CamelModel, UtcDatetime


class RoomSummaryRead(CamelModel):
    """GET /api/v1/rooms/{roomId}/summary 返回。"""

    room_id: str
    summary_text: str | None = None
    highlights: list[str] | None = None
    #: 这一局**没查到**的东西：核心真相 + 没揭开的线索配对（2026-08-19）。
    #:
    #: 🔴 **只在对局已经 `finished` 时非空**，服务层守着这道门。真人 KP 收场时
    #: 一定会把谜底讲出来，那是玩家最在乎的部分；而 `kp_truth` 此前只进裁决
    #: prompt，没有任何通往玩家的出口。玩家可以主动收工（内容没跑完就结束）
    #: 之后，拿不到交代会变成默认结果。
    #:
    #: `None` = 还没结束（或解析不出模组），**不是空列表**：两者含义不同，
    #: 前端据此区分"不该显示这一块"和"全查到了，没有遗漏"。
    missed_truths: list[str] | None = None


class ReplayEventRead(CamelModel):
    """GET /api/v1/rooms/{roomId}/replay 返回项——对应 `events` 表的一行。"""

    model_config = {"from_attributes": True}
    id: str
    player_id: str | None = None
    event_type: str
    payload: dict
    created_at: UtcDatetime

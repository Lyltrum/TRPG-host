"""待掷检定队列（两段式玩家掷骰，keeper agent 实验）。

裁决判定"要不要检定"后不立即掷骰——真正的骰子由玩家在前端点击按钮确认，
服务端权威生成骰值。裁决产出的检定请求先在这里排队等待玩家确认；一轮回复
可能同时挂起多个（比如一次行动同时触发技能检定 + 目击后的理智检定）。

⚠️ 实验期妥协：进程内存 dict（跟 action_lock.py 同一档次的取舍）——单进程
部署，重启即丢失挂起的检定请求（届时玩家重新提交行动会重新触发裁决）。
"""

from dataclasses import dataclass


@dataclass
class PendingCheck:
    """一次待掷的检定请求。"""

    check_request_id: str  # uuid4
    kind: str  # "skill" | "san"
    room_id: str
    player_id: str  # 房间内部 Player.id
    player_nickname: str
    skill: str | None  # kind="skill" 时的展示名；san 恒为 None
    loss_on_success: str  # kind="san" 时使用；skill 恒 "0"
    loss_on_failure: str
    reason: str


class PendingCheckManager:
    """房间级待掷检定队列，进程内存单例。"""

    def __init__(self) -> None:
        self._queues: dict[str, list[PendingCheck]] = {}

    def add(self, room_id: str, checks: list[PendingCheck]) -> None:
        if not checks:
            return
        self._queues.setdefault(room_id, []).extend(checks)

    def first(self, room_id: str) -> PendingCheck | None:
        queue = self._queues.get(room_id)
        return queue[0] if queue else None

    def pop(self, room_id: str, check_request_id: str) -> PendingCheck | None:
        """按 id 找到并移除——找不到（已被结算/id 错误）返回 None，不抛异常。"""
        queue = self._queues.get(room_id)
        if not queue:
            return None
        for index, check in enumerate(queue):
            if check.check_request_id == check_request_id:
                popped = queue.pop(index)
                if not queue:
                    del self._queues[room_id]
                return popped
        return None

    def has(self, room_id: str) -> bool:
        return bool(self._queues.get(room_id))

    def requeue_front(self, room_id: str, check: PendingCheck) -> None:
        """把一个已 pop 出来的检定放回队首——用于"错玩家掷骰"这类需要撤销
        pop 的场景（该检定仍然待掷，不能凭空消失）。"""
        self._queues.setdefault(room_id, []).insert(0, check)


pending_check_manager = PendingCheckManager()

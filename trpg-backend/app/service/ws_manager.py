"""房间级 WebSocket 连接登记表（issue #60）。

只负责"这个房间当前有哪些连接、往它们广播一条消息"，不关心业务逻辑——
业务状态（玩家列表/准备/建卡完成/房间阶段）仍然是 service/room.py 里的
内存 stub，WS 层只是在事件发生时读写它、再把结果广播出去。
"""

import contextlib

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        self._rooms: dict[str, set[WebSocket]] = {}
        # 连接 → 玩家（exec/14 P5.2）。per-observer 投递要能回答"这条连接是谁"
        # ——分头探索时叙事只发给在场的那几个人，不能再无差别广播。
        self._players: dict[WebSocket, str] = {}

    def add(self, room_id: str, websocket: WebSocket, player_id: str | None = None) -> None:
        self._rooms.setdefault(room_id, set()).add(websocket)
        if player_id is not None:
            self._players[websocket] = player_id

    def remove(self, room_id: str, websocket: WebSocket) -> None:
        self._players.pop(websocket, None)
        connections = self._rooms.get(room_id)
        if connections is None:
            return
        connections.discard(websocket)
        if not connections:
            del self._rooms[room_id]

    def has_connections(self, room_id: str) -> bool:
        return bool(self._rooms.get(room_id))

    def connection_count(self, room_id: str) -> int:
        """房间当前的连接数。回合收集窗口用它决定"要不要等其他人"
        （单人局窗口为 0，见 service/turn_window.py）。"""
        return len(self._rooms.get(room_id, ()))

    def connected_room_ids(self) -> list[str]:
        """当前至少有一条 WS 连接的房间 id（心跳扫描用）。"""
        return [rid for rid, conns in self._rooms.items() if conns]

    def connected_player_ids(self, room_id: str) -> list[str]:
        """房间里当前连着的玩家 id（去重、保连接登记顺序无关的稳定性交给调用方）。"""
        seen: dict[str, None] = {}
        for websocket in self._rooms.get(room_id, ()):
            pid = self._players.get(websocket)
            if pid is not None:
                seen.setdefault(pid, None)
        return list(seen)

    async def broadcast(self, room_id: str, message: dict) -> None:
        # 复制一份快照再遍历：广播过程中某个连接掉线触发 remove() 会改动
        # 原集合，直接遍历原集合会撞上"运行时改变集合大小"的异常。
        for websocket in list(self._rooms.get(room_id, ())):
            # 发送失败（连接已经断了但还没走到 disconnect 清理）忽略，
            # 交给该连接自己的 receive 循环去 remove()。
            with contextlib.suppress(Exception):
                await websocket.send_json(message)

    async def send_to_players(self, room_id: str, player_ids: list[str], message: dict) -> None:
        """只发给指定玩家的连接（exec/14 P5.2 per-observer 投递）。

        🔴 `player_ids` 为空 = **一个人都不发**，绝不退化成广播。私密投递上
        "受众算错了"必须表现为没人收到（可见的故障），不能表现为发给了所有人
        （当场泄密，而且看起来一切正常）。
        """
        wanted = set(player_ids)
        for websocket in list(self._rooms.get(room_id, ())):
            if self._players.get(websocket) not in wanted:
                continue
            with contextlib.suppress(Exception):
                await websocket.send_json(message)


manager = ConnectionManager()

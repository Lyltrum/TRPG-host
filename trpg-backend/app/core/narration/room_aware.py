"""按房间 `scenario_id` 选择 structured 剧本的 Keeper 入口。

🔴 **本文件是叙事层里唯一依赖 `keeper/` 的地方**（除了 `factory.py`）。
把它单独拆出来，是为了让 `contract.py` 保持叶子——见那边的模块说明。
"""

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.keeper.contract.module_loader import load_module
from app.core.keeper.contract.source import ResolvedModule, resolve_module
from app.core.keeper.runtime.agent import KeeperAgent
from app.core.narration.contract import (
    CheckResultCallback,
    NarrationContext,
    NarrationOutcome,
    Narrator,
)


class RoomAwareKeeperNarrator(Narrator):
    """按房间 `scenario_id` 选择 structured 剧本的 Keeper 入口。

    前端选模组 → 房间写 scenario_id → 本类查 catalog 加载对应 JSON。
    同一 path 缓存同一个 KeeperAgent（模块级复用，避免每轮重读）。
    """

    def __init__(
        self,
        api_key: str,
        *,
        modules_dir: Path,
        fallback_path: Path | None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from app.core.coc7.content import build_coc7_ruleset

        self._api_key = api_key
        self._modules_dir = modules_dir
        self._fallback_path = fallback_path
        self._session_factory = session_factory
        self._ruleset = build_coc7_ruleset()
        self._agents: dict[str, Narrator] = {}

    def _agent_for(self, resolved: ResolvedModule) -> Narrator:
        agent = self._agents.get(resolved.cache_key)
        if agent is None:
            agent = KeeperAgent(
                api_key=self._api_key,
                module=resolved.module,
                ruleset=self._ruleset,
                session_factory=self._session_factory,
            )
            self._agents[resolved.cache_key] = agent
        return agent

    async def _resolve(self, room_id: str | None) -> ResolvedModule:
        """解析这个房间该玩哪份剧本（内置走文件、导入走库，见 `contract/source.py`）。"""
        from app.models.room import Room

        if room_id:
            async with self._session_factory() as db:
                room = await db.get(Room, room_id)
                scenario_id = room.scenario_id if room is not None else None
                resolved = await resolve_module(db, self._modules_dir, scenario_id)
            if resolved is not None:
                return resolved
        if self._fallback_path is not None and self._fallback_path.is_file():
            return ResolvedModule(
                cache_key=str(self._fallback_path.resolve()),
                module=load_module(self._fallback_path),
            )
        raise FileNotFoundError(
            "房间未绑定可玩模组，且未配置可用的 KEEPER_MODULE_PATH 兜底；"
            f"modules_dir={self._modules_dir}"
        )

    async def location_label(
        self, room_id: str, keeper_state: dict | None, location_id: str
    ) -> str | None:
        """位置 id → 玩家看得懂的名字（`party.update` 用，`exec/33 §5.4`）。

        放在这里而不是 ws 层：**剧本是按房间加载的，只有这一层知道该用哪一份**。
        ws 自己缓存一份就是第二份真相。解析不出就返回 id，**不编造名字**。
        """
        from app.core.keeper.runtime.location_state import resolve_location

        try:
            resolved = await self._resolve(room_id)
        except FileNotFoundError:
            return location_id
        return resolve_location(resolved.module, keeper_state, location_id) or location_id

    async def narrate(self, context: NarrationContext) -> NarrationOutcome:
        resolved = await self._resolve(context.room_id)
        return await self._agent_for(resolved).narrate(context)

    async def resolve_check(
        self,
        room_id: str,
        player_id: str,
        check_request_id: str,
        on_result: CheckResultCallback | None = None,
    ) -> NarrationOutcome:
        resolved = await self._resolve(room_id)
        return await self._agent_for(resolved).resolve_check(
            room_id, player_id, check_request_id, on_result
        )

    async def resolve_player_offer(
        self,
        room_id: str,
        player_id: str,
        decision_id: str,
        accepted: bool,
        on_result: CheckResultCallback | None = None,
    ) -> NarrationOutcome:
        resolved = await self._resolve(room_id)
        return await self._agent_for(resolved).resolve_player_offer(
            room_id, player_id, decision_id, accepted, on_result
        )

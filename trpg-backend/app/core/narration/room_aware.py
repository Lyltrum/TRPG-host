"""按房间 `scenario_id` 选择 structured 剧本的 Keeper 入口。

🔴 **本文件是叙事层里唯一依赖 `keeper/` 的地方**（除了 `factory.py`）。
把它单独拆出来，是为了让 `contract.py` 保持叶子——见那边的模块说明。
"""

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.keeper.agent import KeeperAgent
from app.core.keeper.catalog import resolve_structured_path
from app.core.keeper.module_loader import load_module
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
        from app.core.coc7_content import build_coc7_ruleset

        self._api_key = api_key
        self._modules_dir = modules_dir
        self._fallback_path = fallback_path
        self._session_factory = session_factory
        self._ruleset = build_coc7_ruleset()
        self._agents: dict[str, Narrator] = {}

    def _agent_for(self, path: Path) -> Narrator:

        key = str(path.resolve())
        agent = self._agents.get(key)
        if agent is None:
            agent = KeeperAgent(
                api_key=self._api_key,
                module=load_module(path),
                ruleset=self._ruleset,
                session_factory=self._session_factory,
            )
            self._agents[key] = agent
        return agent

    async def _resolve_path(self, room_id: str | None) -> Path:
        from app.models.room import Room

        if room_id:
            async with self._session_factory() as db:
                room = await db.get(Room, room_id)
                scenario_id = room.scenario_id if room is not None else None
            mapped = resolve_structured_path(self._modules_dir, scenario_id)
            if mapped is not None:
                return mapped
        if self._fallback_path is not None and self._fallback_path.is_file():
            return self._fallback_path
        raise FileNotFoundError(
            "房间未绑定可玩模组，且未配置可用的 KEEPER_MODULE_PATH 兜底；"
            f"modules_dir={self._modules_dir}"
        )

    async def narrate(self, context: NarrationContext) -> NarrationOutcome:
        path = await self._resolve_path(context.room_id)
        return await self._agent_for(path).narrate(context)

    async def resolve_check(
        self,
        room_id: str,
        player_id: str,
        check_request_id: str,
        on_result: CheckResultCallback | None = None,
    ) -> NarrationOutcome:
        path = await self._resolve_path(room_id)
        return await self._agent_for(path).resolve_check(
            room_id, player_id, check_request_id, on_result
        )

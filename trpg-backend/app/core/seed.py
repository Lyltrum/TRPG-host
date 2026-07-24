"""开发/测试环境的最小内容种子数据（issue #77，issue #84 S1 补充 ruleset）。

内容库（`games`/`game_systems`/`scenarios`）与 Keeper 可玩目录对齐：
`GET /modules` 列出 catalog 中已有 structured 的模组（追书人 / 科比特先生），
前端选模组 → 房间 scenario_id → Keeper 加载对应 JSON。

用固定 UUID + 幂等 upsert：启动可重复调用。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.coc7_content import build_coc7_ruleset
from app.core.keeper.catalog import KEEPER_MODULE_SPECS
from app.models.content import Game, GameSystem, Scenario

BUILTIN_GAME_ID = "00000000-0000-0000-0000-000000000001"
BUILTIN_SYSTEM_ID = "00000000-0000-0000-0000-000000000002"
# 兼容旧常量名（= catalog 追书人 id）
BUILTIN_SCENARIO_ID = KEEPER_MODULE_SPECS[0].scenario_id


async def ensure_seed_content(db: AsyncSession) -> None:
    """确保 COC7 系统 + catalog 内全部可玩模组存在，ruleset 与代码对齐。"""
    coc7_ruleset = build_coc7_ruleset().model_dump(mode="json")

    game = await db.get(Game, BUILTIN_GAME_ID)
    if game is None:
        db.add(
            Game(
                id=BUILTIN_GAME_ID,
                name="克苏鲁的呼唤",
                description="COC 内置游戏大类（种子数据）",
            )
        )

    system = await db.get(GameSystem, BUILTIN_SYSTEM_ID)
    if system is None:
        db.add(
            GameSystem(
                id=BUILTIN_SYSTEM_ID,
                game_id=BUILTIN_GAME_ID,
                name="COC7",
                version="7th",
                ruleset=coc7_ruleset,
            )
        )
    else:
        # 内置系统 ruleset 每次启动跟代码对齐（避免 DB 与规则引擎漂移）
        if system.ruleset != coc7_ruleset:
            system.ruleset = coc7_ruleset

    for spec in KEEPER_MODULE_SPECS:
        row = await db.get(Scenario, spec.scenario_id)
        if row is None:
            db.add(
                Scenario(
                    id=spec.scenario_id,
                    game_system_id=BUILTIN_SYSTEM_ID,
                    title=spec.title,
                    version=spec.version,
                    authors=list(spec.authors),
                    players_min=spec.players_min,
                    players_max=spec.players_max,
                    difficulty=spec.difficulty,
                    estimated_duration=spec.estimated_duration,
                    synopsis=spec.synopsis,
                )
            )
        else:
            # 标题/简介随 catalog 更新（旧库「追书人（内置）」→「追书人」）
            row.title = spec.title
            row.version = spec.version
            row.authors = list(spec.authors)
            row.players_min = spec.players_min
            row.players_max = spec.players_max
            row.difficulty = spec.difficulty
            row.estimated_duration = spec.estimated_duration
            row.synopsis = spec.synopsis

    await db.commit()

"""把「一键生成」的数值卡接到背景生成器上（exec/23 #55 遗留的另一半）。

放在独立模块而不是塞进 `service/character.py`：两条建卡路径都要用它——玩家的
一键生成（`character.quick_build_character`）和 AI 队友（`ai_player.
create_ai_player`），而 `character` 已经 import 了 `ai_player`，反过来再 import
就成环了。

这一层只做"取素材 + 调一次生成器 + 翻译成落库形状"，不含任何 prompt——prompt
在 `core/background_writer.py`，那里也写着为什么模组只给 era/tone。
"""

from __future__ import annotations

from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.background_writer import BackgroundWriter, build_prompt, to_detail
from app.core.coc7.content import build_coc7_ruleset
from app.core.config import get_settings
from app.models.room import Room

logger = structlog.get_logger()

#: 喂给写背景的模型的技能条数。跟 `sheet_digest` 的 `_TOP_SKILLS` 同一个数：
#: 两处都在回答"这个人擅长什么"，取不一样的条数只会让两边描述对不上。
_TOP_SKILLS = 6


async def module_era_and_tone(
    db: AsyncSession, scenario_id: str | None
) -> tuple[str | None, str | None]:
    """房间选的模组的年代与基调。取不到就 `(None, None)`。

    🔴 只返回这两个标量，绝不返回 `ScenarioModule` 本身——调用方拿不到剧本，
    就不可能不小心把谜底喂进请求里（同 `background_writer` 与
    `core/equipment_check.py` 的保密边界那一段）。

    🔴 走 `resolve_module` 而不是 `resolve_structured_path`：**导入的模组没有
    文件路径**，只按路径找会让所有导入模组的年代恒为空。

    🔴 **写背景与装备审核共用这一个**：两处都在回答同一个问题，各写一份就会像
    2026-08-18 之前那样——审核那份走了接缝、写背景那份还停在按路径找。

    模组目录是 gitignored 的第三方内容，CI 和全新 clone 上都不存在；那时这里
    返回 (None, None)，调用方退回通用的年代设定。
    """
    from app.core.keeper.contract.catalog import default_modules_dir
    from app.core.keeper.contract.source import resolve_module

    settings = get_settings()
    modules_dir = (
        Path(settings.keeper_modules_dir).expanduser().resolve()
        if settings.keeper_modules_dir
        else default_modules_dir()
    )
    try:
        resolved = await resolve_module(db, modules_dir, scenario_id)
    except Exception as exc:  # noqa: BLE001 — 模组坏了不该连累建卡
        logger.warning("module_era_and_tone_failed", error=str(exc))
        return None, None
    if resolved is None:
        return None, None
    return resolved.module.meta.era, resolved.module.meta.tone


def _named_top_skills(skills: dict[str, int]) -> list[tuple[str, int]]:
    """技能 id → 中文名，取值最高的几项。

    查不到名字的 id 直接跳过（不像 `sheet_digest` 那样原样显示）：那边漏一项
    会让裁决器以为角色不会这件事，是**信息损失**；这边只是写背景的素材，
    露一个 `spot_hidden` 这样的原始 id 给模型看，它会把 id 当成人物特征写进去。
    """
    catalog = {spec.id: spec.name for spec in build_coc7_ruleset().skills}
    top = sorted(skills.items(), key=lambda kv: kv[1], reverse=True)
    return [(catalog[sid], value) for sid, value in top if sid in catalog][:_TOP_SKILLS]


async def generate_background(
    db: AsyncSession,
    room_id: str,
    writer: BackgroundWriter | None,
    *,
    name: str,
    occupation: str,
    age: int,
    skills: dict[str, int],
) -> tuple[str, dict[str, str]] | None:
    """生成 `(background, background_detail)`。**没配 key 或任何失败都返回 None**，
    由调用方保持背景为空——那正是本功能上线前的状态，`sheet_digest` 会渲染成
    「未填写（这张卡没有写过去）」，而 #55 已验证模型会把空白留给玩家。
    """
    if writer is None:
        return None

    room = await db.get(Room, room_id)
    era, tone = await module_era_and_tone(db, room.scenario_id if room is not None else None)

    background = await writer.write(
        build_prompt(
            name=name,
            occupation=occupation,
            age=age,
            top_skills=_named_top_skills(skills),
            era=era,
            tone=tone,
        )
    )
    if background is None:
        return None
    return background.summary, to_detail(background)

"""把「一键生成」的数值卡接到背景生成器上（exec/23 #55 遗留的另一半）。

放在独立模块而不是塞进 `service/character.py`：两条建卡路径都要用它——玩家的
一键生成（`character.quick_build_character`）和 AI 队友（`ai_player.
create_ai_player`），而 `character` 已经 import 了 `ai_player`，反过来再 import
就成环了。

这一层只做"取素材 + 调一次生成器 + 翻译成落库形状"，不含任何 prompt——prompt
在 `core/background_writer.py`，那里也写着为什么模组只给 era。
"""

from __future__ import annotations

from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.background_writer import BackgroundWriter, build_prompt, to_detail
from app.core.coc7.content import build_coc7_ruleset
from app.core.config import get_settings
from app.core.equipment_check import clamp_items
from app.models.room import Room

logger = structlog.get_logger()

#: 喂给写背景的模型的技能条数。跟 `sheet_digest` 的 `_TOP_SKILLS` 同一个数：
#: 两处都在回答"这个人擅长什么"，取不一样的条数只会让两边描述对不上。
_TOP_SKILLS = 6


async def module_era(db: AsyncSession, scenario_id: str | None) -> str | None:
    """房间选的模组的**年代与地点**。取不到就 `None`。

    🔴 只返回这**一个**标量，绝不返回 `ScenarioModule` 本身——调用方拿不到剧本，
    就不可能不小心把谜底喂进请求里（同 `background_writer` 与
    `core/equipment_check.py` 的保密边界那一段）。

    🔴 **`meta.tone` 曾经也在这儿，2026-08-18 撤掉了。** 那个字段的原生语义是
    **KP 侧的**——`render_overview` 把它跟 `【KP 真相（绝密）】` 并排渲进守秘人
    的 system prompt，模组作者因此往里写执导笔记。实测六份里《追书人》的 tone
    是 95 字，直接写着「核心是揭开一个自愿离开人类社会者的真相」「『他过得很
    满足』带来的怪异感」——而这两个调用方的产出（角色背景、装备审核理由）
    **玩家直接看得到**。

    这就是「一份数据扮演两个角色」，且两个角色分居保密边界两侧。修法不是过滤
    （判据只能是长度或关键词，正是「对着一个样本调判据」），是**让玩家侧根本
    读不到它**——`era` 讲的是时代与地点，那本来就是玩家知道的东西。

    🔴 走 `resolve_module` 而不是 `resolve_structured_path`：**导入的模组没有
    文件路径**，只按路径找会让所有导入模组的年代恒为空。

    🔴 **写背景与装备审核共用这一个**：两处都在回答同一个问题，各写一份就会像
    2026-08-18 之前那样——审核那份走了接缝、写背景那份还停在按路径找。

    模组目录是 gitignored 的第三方内容，CI 和全新 clone 上都不存在；那时这里
    返回 `None`，调用方退回通用的年代设定。
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
        logger.warning("module_era_failed", error=str(exc))
        return None
    if resolved is None:
        return None
    return resolved.module.meta.era


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
) -> tuple[str, dict[str, str], list[str]] | None:
    """生成 `(background, background_detail, equipment)`。

    **没配 key 或任何失败都返回 None**，由调用方保持背景为空——那正是本功能
    上线前的状态，`sheet_digest` 会渲染成「未填写（这张卡没有写过去）」，
    而 #55 已验证模型会把空白留给玩家。

    ## 🔴 装备为什么搭这趟车（`exec/46` B8）

    快速建卡此前**不给任何装备**，于是刚做完的那整条装备合理性校验链
    在这条路径上**结构上跑不到**（2026-08-19 查"装备校验零样本"时的根因）。

    合进这一次调用而不是新开一次：这里已经在为同一个人调一次 LLM，而且
    **`era` 已经在 prompt 里**——装备正需要年代。多开一次往返是白花的钱。

    **生成出来的装备不再过一遍 `equipment_check`**：那两条规则（年代 / 身份
    配不配得上武器）已经写进生成 prompt 了，再审一次是让模型自己驳自己，
    而审不过会让**快速建卡整个失败**——那比装备不合理糟得多。
    校验那条门是拦**玩家自己写的**，两者的对象不同。
    """
    if writer is None:
        return None

    room = await db.get(Room, room_id)
    era = await module_era(db, room.scenario_id if room is not None else None)

    background = await writer.write(
        build_prompt(
            name=name,
            occupation=occupation,
            age=age,
            top_skills=_named_top_skills(skills),
            era=era,
        )
    )
    if background is None:
        return None
    return background.summary, to_detail(background), clamp_items(background.equipment)

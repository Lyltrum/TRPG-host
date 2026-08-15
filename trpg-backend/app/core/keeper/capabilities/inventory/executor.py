"""inventory 能力的执行层：把随身物品的增减落到角色卡上。"""

from __future__ import annotations

from pydantic import BaseModel

from app.core.keeper.contract.registry import TurnFacts
from app.core.keeper.runtime.deps import (
    KeeperDeps,
    KeeperToolError,
    record_event,
    resolve_character,
)


def _same_item(left: str, right: str) -> bool:
    """两件东西是不是同一件。只去空白、忽略大小写。

    🔴 不做近义匹配：「手电筒」和「电筒」是不是同一件是语义判断，模糊匹配是
    同义词打地鼠的开始（exec/17）。名字对不上就当作没有，由调用方记 issue
    ——玩家看得见"系统认为你没有这件东西"，比悄悄删掉别的东西强。
    """
    return left.strip().casefold() == right.strip().casefold()


async def execute_equipment_changes(
    deps: KeeperDeps, decision: BaseModel, _facts: TurnFacts
) -> tuple[list[str], list[str]]:
    """增量地改随身清单。

    ## 🔴 为什么是增量而不是快照（跟 `cast` 刻意相反）

    随身清单是**玩家自己写的东西**（建卡第 6 步那一栏，措辞是他的）。每轮让
    模型重报全量，它迟早会漏掉一件、或者把「祖父留下的怀表」改写成「怀表」。
    HP 走 delta 是同一个理由。`cast` 之所以能用快照，是因为那张表**本来就
    是模型每轮重算的**，没有玩家写的东西在里面。

    ## 🔴 拿不到的东西不许悄悄消失

    `lost` 里的名字在清单上找不到时**不删任何东西**，记一条 issue。删错的
    代价是玩家的东西凭空没了，而那是他自己写下的字。
    """
    changes = list(getattr(decision, "equipment_changes", ()))
    if not changes:
        return [], []

    report: list[str] = []
    issues: list[str] = []
    async with deps.write_lock, deps.session_factory() as db:
        for change in changes:
            try:
                player, character = await resolve_character(db, deps, change.player)
            except KeeperToolError as exc:
                issues.append(f"随身物品未更新：{exc}")
                continue
            # ⚠️ JSON 列必须整体重新赋值——SQLAlchemy 不追踪 list 的原地修改。
            items = [str(x) for x in (character.equipment or [])]
            gained = [str(g).strip() for g in change.gained if str(g).strip()]
            for item in gained:
                if any(_same_item(item, existing) for existing in items):
                    issues.append(f"随身物品未更新：{player.nickname} 身上已经有「{item}」了")
                    continue
                items.append(item)
                report.append(f"{player.nickname} 拿到了「{item}」（{change.reason or '—'}）")
            for raw in change.lost:
                name = str(raw).strip()
                if not name:
                    continue
                match = next((x for x in items if _same_item(x, name)), None)
                if match is None:
                    issues.append(
                        f"随身物品未更新：{player.nickname} 身上没有「{name}」"
                        "（名字要跟随身清单上的一字不差）"
                    )
                    continue
                items.remove(match)
                report.append(f"{player.nickname} 失去了「{match}」（{change.reason or '—'}）")
            if items != [str(x) for x in (character.equipment or [])]:
                character.equipment = items
                await record_event(
                    db,
                    deps,
                    "keeper.equipment",
                    {"player": player.nickname, "items": items, "reason": change.reason},
                )
    return report, issues

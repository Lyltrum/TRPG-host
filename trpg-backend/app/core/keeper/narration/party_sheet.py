"""把「这些调查员会什么」摆到裁决器眼前。

## 🔴 为什么补这一块（2026-08-14 真人实测）

裁决那一拍的局面块此前由这些块组成：世界状态笔记、对话历史、名册、阶段、
事实账本、章节摘要，加上各片能力注册的块（议程 / 悬而未决 / 密级配对 / 疯狂 /
理智检定点 / 桌上的人 / 即兴地点 / 还剩多少内容 / 核心真相 / NPC 状态）。
**没有一块是「这个调查员会什么」。**

后果在实测里一次性暴露三条：

- 玩家话术 **5**（初始值，等于 95% 必败），而"向 NPC 打听"被判成话术检定点，
  于是**每一句问话都要掷话术**。真人 KP 是看着卡决定要不要让你掷的——
  「你话术 5？那这段不用掷，他直接告诉你」。裁决器看不到那个 5。
- 21 次检定里 10 次是侦察，目标值恒为 51；而玩家查的是考古学教授的野外笔记、
  手绘地图，**一次知识技能检定都没发起过**——它不知道这张卡的考古学是多少。
- 检定失败之后没有迂回，因为它不知道这张卡还有什么别的本事可用。

## 边界：为什么在骨架而不是某一片能力里

`skill_check` / `health` / `san_check` 都要用它，而**能力之间不许互相 import**。
判据是那句老的：**共享的输入归骨架，用它做裁决的字段与执行归能力**。

## 给多少：不是全卡

COC7 一张卡有 80 多个技能，全量每轮进 prompt 是纯噪音（而且会把真正重要的
几个淹掉）。这里只给**玩家点过的**（高于初始值的）加上全部属性——
「没点过的技能等于初始值」这条规则本来就写在权威 id 表旁边，模型推得出来。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.coc7.rules import evaluate_skill_base
from app.dto.game import RulesetRead
from app.models.room import Character

#: 一张卡最多列几个技能。点满 30 个技能的卡是存在的，而超过这个数之后
#: 每一条的信息量都在下降——按数值从高到低截断，留下的是最可能被用到的。
_MAX_SKILLS = 20


def _skill_lines(character: Character, ruleset: RulesetRead) -> list[str]:
    """玩家点过的技能（高于初始值的），按数值从高到低。"""
    skills: dict[str, int] = character.skills or {}
    attributes: dict[str, int] = character.attributes or {}
    out: list[tuple[int, str]] = []
    for spec in ruleset.skills:
        value = skills.get(spec.id)
        if value is None:
            continue
        base = evaluate_skill_base(spec.base, attributes)
        if value <= base:
            continue  # 没点过 = 初始值，模型按规则自己推得出来
        out.append((value, f"{spec.name}({spec.id}) {value}"))
    out.sort(key=lambda item: (-item[0], item[1]))
    return [text for _, text in out[:_MAX_SKILLS]]


async def load_party_characters(
    db: AsyncSession,
    *,
    room_id: str,
    players: list[tuple[str, str]],
) -> list[tuple[str, Character]]:
    """按 `players` 的顺序取每个人的角色卡。没有卡的人直接跳过（不占一行）。

    一个玩家一张卡由唯一约束 `uq_characters_room_player` 保证，所以这里
    `player_id → Character` 的字典不会有"后写覆盖先写"的歧义。
    """
    if not players:
        return []
    rows = await db.execute(select(Character).where(Character.room_id == room_id))
    by_player = {c.player_id: c for c in rows.scalars()}
    return [
        (nickname, by_player[pid]) for pid, nickname in players if by_player.get(pid) is not None
    ]


def format_party_sheet(
    characters: list[tuple[str, Character]],
    ruleset: RulesetRead | None,
) -> str:
    """`[(昵称, 角色卡)]` → 局面块文本。没有卡或没有规则数据时返回空串（整块不渲染）。"""
    if not characters or ruleset is None:
        return ""

    blocks: list[str] = []
    for nickname, character in characters:
        attributes: dict[str, int] = character.attributes or {}
        derived: dict = character.derived_stats or {}
        head = f"- **{nickname}**"
        if character.occupation:
            head += f"（{character.occupation}）"

        attr_text = "、".join(
            f"{attr.label} {attributes[attr.key]}"
            for attr in ruleset.attributes
            if isinstance(attributes.get(attr.key), int)
        )
        # HP/SAN 走 derived_stats 的当前值；缺就不写这一段，**不编默认值**。
        vitals = [
            f"{label} {derived[key]}"
            for key, label in (("HP", "生命"), ("SAN", "理智"), ("MP", "魔法"))
            if isinstance(derived.get(key), int)
        ]
        skills = _skill_lines(character, ruleset)

        lines = [head]
        if attr_text:
            lines.append(f"  属性：{attr_text}")
        if vitals:
            lines.append("  当前：" + "、".join(vitals))
        lines.append(
            "  点过的技能：" + ("、".join(skills) if skills else "（这张卡没有点过任何技能）")
        )
        blocks.append("\n".join(lines))

    return (
        "\n".join(blocks)
        + "\n\n🔴 **上面没列出的技能一律是初始值**（多数在 5～25 之间，等于大概率失败）。"
        "决定要不要发检定、发哪个技能之前先看这张表：目标值低到那个份上的检定，"
        "结果几乎注定是失败，除非失败本身有意思，否则别掷——直接给结果，"
        "或者换一个这张卡真正擅长的技能。"
    )

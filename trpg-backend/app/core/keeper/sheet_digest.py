"""角色卡摘要——守秘人对「这个调查员是谁」的认知（exec/23 #55）。

## 为什么需要它

在此之前，守秘人上下文里关于玩家的**全部**信息是一行
`昵称（角色：名字，职业）`。属性、技能、背景故事一个字都没进过 prompt。

真人实测：玩家问「我是谁」，模型手上只有职业两个字，于是现编了一段个人史
（警察、私酒、某 NPC 作证）当成既成事实说出来——**既不在卡上，也不在剧本
里**。玩家的反应是"完全没有任何逻辑"。

真人 KP 面前摊着每个人的角色卡。这个模块就是把那张卡摆到桌面上。

## 为什么可以全给

角色卡在本项目里是**公开信息**：exec/18 ⑦⑧ 裁定检定过程与 HP/SAN 公开，
P5.3 队友之间可以互相传阅角色卡。所以这里不做任何脱敏——守秘人本来就该
看得见全部，它不知道就没法主持。

## 🔴 卡上没有的东西，这里也不会有

背景是空的（一键生成的卡、或玩家没填）就渲染成「未填写」。**空缺要显式**，
不能让下游模型误以为"没写 = 随便编"。配套的纪律约束在 prompts.py。
"""

from __future__ import annotations

from app.dto.game import RulesetRead
from app.models.room import Character

#: 每人列几项最高的技能。全量 92 条会把局面块淹掉，而"他擅长什么"正是裁决
#: 「这个行动该不该让他做/用什么检定」时唯一需要的那部分。
_TOP_SKILLS = 6

#: 背景故事渲染上限（每字段）。玩家可以写很长，但局面块每轮都要重发一遍。
_BACKGROUND_CLIP = 80

#: `background_detail` 的键 → 中文标签。键是前端表单定的，这里只翻译已知的，
#: 未知键原样保留——加字段时不会静默丢内容。
_BACKGROUND_LABELS = {
    "personalDescription": "形象",
    "ideology": "信念",
    "significantPeople": "重要之人",
    "meaningfulLocations": "重要之地",
    "treasuredPossessions": "宝贵之物",
    "traits": "特质",
    "injuries": "伤疤与旧伤",
    "phobias": "恐惧症与狂躁症",
}


def _top_skills(character: Character, ruleset: RulesetRead) -> str:
    skills = character.skills or {}
    if not skills:
        return ""
    names = {s.id: s.name for s in ruleset.skills}
    top = sorted(skills.items(), key=lambda kv: kv[1], reverse=True)[:_TOP_SKILLS]
    # 技能 id 查不到名字时原样显示 id：宁可露出一个丑陋的 id，也不要静默丢掉
    # 一项能力（模型至少还能看出"这里有个东西"）。
    return "、".join(f"{names.get(sid, sid)} {value}" for sid, value in top)


def _background(character: Character) -> str:
    parts: list[str] = []
    free_text = (character.background or "").strip()
    if free_text:
        parts.append(free_text[:_BACKGROUND_CLIP])
    for key, value in (character.background_detail or {}).items():
        text = (value or "").strip()
        if text:
            parts.append(f"{_BACKGROUND_LABELS.get(key, key)}：{text[:_BACKGROUND_CLIP]}")
    return "；".join(parts)


def format_sheet(nickname: str, character: Character | None, ruleset: RulesetRead) -> str:
    """渲染一个调查员的档案（多行，供名单块逐条展开）。"""
    if character is None or not character.name:
        return f"{nickname}（未建卡）"

    derived = character.derived_stats or {}
    vitals = "／".join(
        f"{label} {derived[key]}"
        for label, key in (("HP", "HP"), ("SAN", "SAN"), ("MP", "MP"))
        if derived.get(key) is not None
    )
    head = f"{nickname}（角色：{character.name}，{character.occupation or '无职业'}"
    if character.age:
        head += f"，{character.age}岁"
    head += "）"

    lines = [head]
    if vitals:
        lines.append(f"  当前：{vitals}")
    skills = _top_skills(character, ruleset)
    if skills:
        lines.append(f"  擅长：{skills}")
    background = _background(character)
    # 🔴 空背景要**说出来**，不能省略这一行：省略等于让模型自己填空，而它
    # 填出来的会是一段以既成事实口吻讲述的、谁都没同意过的个人史。
    lines.append(f"  背景：{background}" if background else "  背景：未填写（这张卡没有写过去）")
    return "\n".join(lines)

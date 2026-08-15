"""skill_check 能力贡献给 `KeeperDecision` 的字段片段。"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, Field

from app.core.keeper.contract.registry import Capability, DecisionModel


class OpposedTarget(DecisionModel):
    """对抗检定的对手侧（exec/19 #38）。

    真人实测 2026-07-31：裁决器想要「凌铭辉，体质对抗 POT 16」，而 schema 里
    没有任何字段能表达"对手是谁、目标值多少"——它就把这句写进了散文，玩家
    界面上永远不会出现那张掷骰卡片，只能等一个不来的骰子。**schema 表达不了
    的东西会从叙事里漏出去**，这是那条判据的原始案例。

    `value` 是**百分位目标值**（0–100），不是 COC6 的属性点：毒物 POT 16 要
    写成 80（POT×5），NPC 的技能/属性直接用它的百分数。换算由裁决器完成——
    它手里有数据卡，代码手里没有。
    """

    opponent: str = Field(description="对手是谁/什么（NPC 名、毒物、暗流……），展示用")
    value: int = Field(description="对手侧的百分位目标值 0-100（属性点要 ×5）")


class CheckRequest(DecisionModel):
    """一次技能/属性检定请求。player 为 None = 本轮行动的发起玩家。

    🔴 `skill_id` 是**白名单 id**，不是中文名（exec/17）：技能用规则表 id
    （`spot-hidden`、`fighting-brawl`），属性用属性 key（`STR`、`CON`）。

    为什么不是自由文本：中文名两侧都是人/模型随手写的（裁决器按自己的 COC7
    常识说"侦查"，规则表叫"侦察"），指望逐字相同不现实，而事后维护同义词
    字典是打地鼠——换个模组、模型换个措辞就又漏一个。id 是封闭集合（92+9），
    随规则版本变、不随模组变。**tool calling 不解决这个问题**（它的参数同样
    是模型写的字符串），解决它的是 enum/白名单。

    ⚠️ 如实记：DeepSeek 走的是 JSON mode 而不是带 schema 的 structured output，
    所以 enum **约束不到生成**，只能靠 prompt 给表 + 代码校验。模型仍写中文名
    时代码会**显式回退**并打 `keeper_skill_id_fallback` warning——不是静默，
    日志能统计守规率，据此再决定要不要收紧成硬失败。

    ## 🔴 `npc`：NPC 主动做的事也得有地方掷（2026-08-14 实测）

    此前这个模型里**只有 `player`**，于是"州警开枪"没有任何合法写法。模型
    只剩两条路：不掷，或者记在玩家头上。它选了后者——掷骰卡片上白纸黑字写着
    **凌铭辉 · 射击：步枪/霰弹枪 5/42**，而叙事里扣扳机的是州警，玩家身上
    根本没有枪（`equipment` 全程是 null）。同一拍里两个说法。

    同「schema 表达不了的东西会从叙事里漏出去」那条判据的又一例。

    🔴 **不对称是有意的**（用户 2026-08-15 拍板）：名册里有数据卡的 NPC 用
    **它自己的数值**真掷；即兴造出来的 NPC（那个州警不在名册里）没有数值，
    就**拒绝并记 issue**，由叙事直接裁定。不让裁决器现编一个目标值——那等于
    把难度交给模型自己定，正是「能确定化的是判断的输入，不是判断本身」要
    避免的。
    """

    skill_id: str = Field(description="技能 id 或属性 key，必须取自权威 id 表")
    player: str | None = None
    npc: str | None = Field(
        default=None,
        description=(
            "这次检定由**剧本名册里的 NPC** 掷时填它的 npc id（如 mi-go-4）；"
            "玩家掷就留 null。填了 npc 就不要再填 player。"
            "目标值取自它数据卡上的 `ability` 那一项，所以两个字段要一起给"
        ),
    )
    ability: str | None = Field(
        default=None,
        description=(
            "NPC 数据卡上的哪一项（如「爪击」「黑暗武器」「STR」），"
            "**必须与数据卡上的写法一字不差**；玩家检定时留 null"
        ),
    )
    reason: str = ""
    opposed: OpposedTarget | None = Field(default=None, description="对抗检定时填；普通检定留 null")


class SkillCheckDecisionFields(DecisionModel):
    """被 `KeeperDecision` 继承的字段片段（见 `decision.py` 那行显式继承）。"""

    checks: list[CheckRequest] = Field(default_factory=list)


FIELD_CAPABILITIES = {"checks": Capability.REQUEST_CHECK}


def audit_fields(decision: BaseModel) -> Mapping[str, object]:
    """记 skill_id 而不是条数：排查"该掷没掷/掷错技能"时要的就是这个。"""
    return {"checks": [c.skill_id for c in getattr(decision, "checks", ())]}

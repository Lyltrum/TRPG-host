"""守秘人的游戏操作层（L3 执行）：掷骰/角色卡/剧本/状态的业务实现（keeper agent v2）。

这是 keeper_state 唯一允许写入的地方——每个 `*_impl` 对应一个"动词"
（set_phase_impl/mark_agenda_fired_impl/set_current_node_impl 等），由
`turn_executor.py`（L4 编排）根据 `KeeperDecision`（L1 契约，见 decision.py）
里哪个字段非空来调度，不再是 LLM 的自由工具——v1 的 `@function_tool` 薄壳层
已随架构推翻整体移除（自由工具调用被实测证明不可靠，见 agent.py 模块
docstring）。`*_impl` 保持普通 async 函数形态，可直接单测。

每类保留状态自己的 KEY 常量 + `load_*`/`format_*` 不在本文件——那些是
L2 状态编解码，各自有独立模块（phase.py/visibility.py/agenda_state.py/
scene_state.py），本文件只 import 它们的 KEY 常量用于写入 + 拼进
`RESERVED_STATE_KEYS`。

服务端权威原则：骰子由 `dice.py` 掷（LLM 只消费结果、改不了点数），
HP/San 修改真实写 `characters` 表，所有操作都写一行 `events` 表留痕
（复盘可审计"守秘人掷了什么、改了什么"）。

⚠️ 实验期妥协（非最终形态）：HP/San 的"当前值"直接改写 `derived_stats`
JSON（首次修改时把上限备份为 `HP_MAX`/`SAN_MAX`）——正经做法是独立的
「当前状态」存储，等实验验证过玩法再抽。
"""

import structlog

from app.core.coc7_rules import evaluate_skill_base
from app.core.keeper import module_loader
from app.core.keeper.deps import (
    KeeperDeps,
    KeeperToolError,
    resolve_character,
)

logger = structlog.get_logger()

# ── 内部查询辅助 ──────────────────────────────────────


# 常见同义写法归一：规则表用的规范名 vs. 模组原文/裁决器口语化说法。
# 真人实测连续挖出三例（09-#5 的"斗殴"是复合名短名，走下面单独的后缀匹配；
# 这里是纯粹的同义词，规则表压根没有这个名字）："观察"（该轮理智检定失去
# 前置条件，检定静默丢失）、"闪躲"（"该掷躲闪了"却从未生成待掷卡片）。
# 口语说法和规则表规范名之间的落差大概率不止这几个，发现一个补一个。
# 表本身搬去 `primitives/skills.py`——护栏层要用同一份（exec/12 #32）。


# 结构化背景故事（character-build-migration）8 个引导字段的中文标签。后端把
# `background_detail` 当透明存取的 opaque dict（键的含义是前端表单的事，见
# `CharacterUpdateBody.background_detail` 的字段说明），但这里是"读给 LLM 看"
# 的展示层，跟前端 `character-model.ts::BACKGROUND_DETAIL_FIELDS` 一样需要
# 人类可读的标签——两边各自维护一份，改字段要同步改这里。
_BACKGROUND_DETAIL_LABELS: dict[str, str] = {
    "personalDescription": "个人描述",
    "ideology": "信念/思想",
    "significantPeople": "重要之人",
    "meaningfulLocations": "意义非凡的地点",
    "treasuredPossessions": "珍视的物品",
    "traits": "特质",
    "injuries": "外伤",
    "phobias": "恐惧症",
}


async def get_character_sheet_impl(deps: KeeperDeps, player_name: str | None = None) -> str:
    async with deps.session_factory() as db:
        player, character = await resolve_character(db, deps, player_name)
    attributes = character.attributes or {}
    derived = character.derived_stats or {}
    skills = character.skills or {}

    # 只列玩家真实加过点的技能（总值≠基础值）——全部 80 项技能都列出来
    # 是纯噪音，基础值 agent 需要时可以让 roll_check 自己回落。
    trained: list[str] = []
    for spec in deps.ruleset.skills:
        value = skills.get(spec.id)
        if value is not None and value != evaluate_skill_base(spec.base, attributes):
            trained.append(f"{spec.name} {value}")

    lines = [
        f"玩家：{player.nickname}",
        f"角色：{character.name or '（未命名）'}（{character.occupation or '无职业'}，"
        f"{character.age or '?'} 岁，{character.gender or '?'}）",
        "属性：" + "、".join(f"{k} {v}" for k, v in attributes.items()),
        "衍生：" + "、".join(f"{k} {v}" for k, v in derived.items()),
        "已训练技能：" + ("、".join(trained) if trained else "（无，其余按基础值）"),
    ]
    if character.background:
        lines.append(f"背景：{character.background[:200]}")

    # 结构化背景故事：只列玩家真的填过的字段，跟"已训练技能"同样的降噪原则
    # ——8 个字段建卡时可以全空，全空塞给 LLM 是纯噪音。
    detail = character.background_detail or {}
    filled = [
        f"{_BACKGROUND_DETAIL_LABELS.get(key, key)}：{value[:100]}"
        for key, value in detail.items()
        if value and value.strip()
    ]
    if filled:
        lines.append("背景细节：" + "；".join(filled))

    return "\n".join(lines)


def read_module_impl(deps: KeeperDeps, section: str) -> str:
    """查阅剧本（渲染与 system prompt 的剧本全文共用 module_loader 里的实现）。

    剧本全文已常驻 system prompt，这个工具是"回看细节"的补充手段——保留它
    是因为长模组未来未必能全文常驻，查询路径先留着。不碰数据库。
    """
    module = deps.module
    section = section.strip()

    if section == "overview":
        return module_loader.render_overview(module)
    if section == "nodes":
        return "调查节点列表：\n" + "\n".join(
            f"- {n.id}：{n.title}（→ {'、'.join(n.leads_to) or '终点'}）" for n in module.nodes
        )
    if section.startswith("node:"):
        node = module.node_by_id(section.removeprefix("node:"))
        if node is None:
            raise KeeperToolError(
                f"没有这个节点。可用节点：{'、'.join(n.id for n in module.nodes)}"
            )
        return module_loader.render_node(node)
    if section == "npcs":
        return "NPC 列表：\n" + "\n".join(
            f"- {n.id}：{n.name}（{n.role or ''}）" for n in module.npcs
        )
    if section.startswith("npc:"):
        npc = module.npc_by_id(section.removeprefix("npc:"))
        if npc is None:
            raise KeeperToolError(f"没有这个 NPC。可用：{'、'.join(n.id for n in module.npcs)}")
        return module_loader.render_npc(npc)
    if section == "endings":
        return module_loader.render_endings(module)

    raise KeeperToolError(
        "未知的 section。可用：overview / nodes / node:<id> / npcs / npc:<id> / endings"
    )


#: 不挂在任何具体实体上的世界级状态（游戏内时间、天气、委托进度……）。

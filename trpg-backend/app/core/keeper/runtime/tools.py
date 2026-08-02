"""v1 自由工具的**遗留残余**：读角色卡 / 读剧本两个函数。

⚠️ 如实说明现状（exec/27 阶段 3 之后）：本文件原本是"keeper_state/DB 唯一
允许写入的地方"，961 行、装着十几个 `*_impl`。八片能力切完之后，写入者全部
跟着各自的能力或共享状态模块走了，这里只剩下两个**生产代码已经不再调用**的
函数——它们是 v1 把工具暴露给 LLM 时代的产物，现在只有测试在调。

按项目约定不删既有死代码，所以留着；`exec/27` 阶段 5 做目录终态时一并处置
（要么确认无用删掉，要么归到用得上它的地方）。**不要往这里加新东西。**
"""

import structlog

from app.core.coc7.rules import evaluate_skill_base
from app.core.keeper.contract import module_loader
from app.core.keeper.runtime.deps import (
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
